"""The GPU-validation sequence (A09-1 review, finding 3), run end to end with simulated tools and no AWS resource:
a fake `aws` CLI, SSM commands executed locally, the simulated host of `sim_host.sh` (fake llama-server through the
real run-server.sh) and the real controller CLI. It must start from `stopped`, be started by a durable job, run the
profiles while the host is up and always end with the instance verified `stopped`, or say loudly that it is not."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import DEPLOY, HERE, free_port, server_env

SIM_HOST = HERE / "sim_host.sh"
SCRIPT = DEPLOY / "bench" / "gpu-validation.py"
IID = "i-0sim"


class Sim:
    def __init__(self, tmp: Path, model: str) -> None:
        self.tmp = tmp
        self.port = free_port()
        self.env_file = server_env(tmp, model, self.port)
        self.ev = tmp / "evidence"
        aws = tmp / "aws"
        aws.write_text(f'#!/bin/sh\nexec {sys.executable} {HERE / "fake_aws.py"} "$@"\n')
        aws.chmod(0o755)
        smi = tmp / "nvidia-smi"
        smi.write_text('#!/bin/sh\ncase "$*" in *name*) echo "NVIDIA L4 (simulated)";; *) echo "17000, 23034";; esac\n')
        smi.chmod(0o755)
        (tmp / "meminfo").write_text("MemTotal:       16000000 kB\nMemAvailable:    4000000 kB\n")
        (tmp / "dmi").mkdir()
        (tmp / "dmi" / "board_asset_tag").write_text(IID + "\n")
        self.cfg = tmp / "controller.toml"
        self.cfg.write_text(
            f"""
[controller]
db_path = "{tmp / "state.db"}"
tick_interval_s = 0.2
lease_ttl_s = 5
[policy]
readiness_timeout_s = 30
drain_timeout_s = 20
[executor]
inference_timeout_s = 10
[endpoint]
base_url = "http://127.0.0.1:{self.port}"
model_alias = "qwen3.6-27b-q4km"
api_key_file = "{tmp / "api-key"}"
canary_timeout_s = 10
[backend]
kind = "command"
describe = "{SIM_HOST} {tmp} describe"
start = "{SIM_HOST} {tmp} start"
stop = "{SIM_HOST} {tmp} stop"
"""
        )
        self.env = {
            **os.environ,
            "CCE_AWS_BIN": str(aws),
            "CCE_HOST_SRC": str(DEPLOY),
            "CCE_HOST_SUDO": "",
            "CCE_POLL_S": "0.2",
            "CCE_SSM_POLL_S": "0.05",
            "CCE_SERVER_ENV": str(self.env_file),
            "CCE_RESTART_CMD": f"{SIM_HOST} {tmp} restart",
            "CCE_BENCH_DIR": str(tmp / "bench"),
            "CCE_NVIDIA_SMI": str(smi),
            "CCE_MEMINFO": str(tmp / "meminfo"),
            "CCE_DMI_DIR": str(tmp / "dmi"),
            "CCE_IMDS_BASE": "http://127.0.0.1:9",
            "FAKE_AWS_DIR": str(tmp / "fake-aws"),
            "FAKE_AWS_SIM_DIR": str(tmp),
            "FAKE_AWS_SIM_HOST": str(SIM_HOST),
        }

    def argv(self, *extra: str) -> list[str]:
        return [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(self.cfg),
            "--instance-id",
            IID,
            "--key-file",
            str(self.tmp / "api-key"),
            "--evidence",
            str(self.ev),
            "--base-url",
            f"http://127.0.0.1:{self.port}",
            "--requests",
            "2",
            "--prompt-tokens",
            "200",
            "--job-timeout",
            "60",
            "--load-timeout",
            "30",
            "--stop-timeout",
            "30",
            *extra,
        ]

    def run(self, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.argv(*extra), capture_output=True, text=True, env={**self.env, **(env or {})}, timeout=600, check=False
        )

    def host_state(self) -> str:
        return subprocess.run([str(SIM_HOST), str(self.tmp), "describe"], capture_output=True, text=True).stdout.strip()

    def profile(self) -> str:
        line = next(x for x in self.env_file.read_text().splitlines() if x.startswith("CCE_PROFILE="))
        return Path(line.split("=", 1)[1]).stem

    def calls(self, op: str) -> int:
        path = self.tmp / "fake-aws" / "calls.jsonl"
        if not path.exists():
            return 0
        return sum(1 for line in path.read_text().splitlines() if op in json.loads(line)["argv"])

    def evidence(self, name: str) -> dict:
        return json.loads((self.ev / name).read_text())


@pytest.fixture
def sim(tmp_path: Path, fake_model: dict[str, str]):
    s = Sim(tmp_path, fake_model["path"])
    yield s
    subprocess.run([str(SIM_HOST), str(tmp_path), "stop"], check=False)


def test_job_driven_cycle_profiles_and_verified_stop(sim: Sim):
    assert sim.host_state() == "stopped"
    r = sim.run("--profiles", "reference-8k-1slot candidate-16k-2slot")
    assert r.returncode == 0, r.stdout + r.stderr
    summary = sim.evidence("summary.json")
    assert summary["outcome"] == "validated" and summary["exit"] == 0
    checks = summary["steps"]["job_cycle"]["checks"]
    assert all(checks.values()) and checks["start_requested_by_demand"] and checks["no_manual_start"]
    tl = summary["steps"]["job_cycle"]["timeline"]
    assert tl["submitted"] <= tl["start_requested"] <= tl["ready"] <= tl["job_finished"]
    assert sim.calls("start-instances") == 0  # the start came from the controller, triggered by the job
    assert {p: v["verdict"] for p, v in summary["verdicts"].items()} == {
        "reference-8k-1slot": "pass",
        "candidate-16k-2slot": "pass",
    }
    for name in ("load-reference-8k-1slot.json", "run-reference-8k-1slot-c1.json", "run-candidate-16k-2slot-c2.json"):
        mem = sim.evidence(name)["memory"]
        assert mem["status"] == "measured" and mem["host"]["instance_id"] == IID and mem["samples"]["vram_samples"] > 0
    assert sim.evidence("run-reference-8k-1slot-overload.json")["ok"] == 3
    assert summary["steps"]["controller_stop"]["exit"] == 0
    final = sim.evidence("final-state.json")
    assert final == {"instance_id": IID, "state": "stopped", "fallback_stop": False, "errors": []}
    assert sim.host_state() == "stopped" and sim.profile() == "reference-8k-1slot"  # original profile restored
    assert (sim.ev / "host-facts.json").exists() and (sim.ev / "boot-timings.json").exists()


def test_memory_without_samples_does_not_pass_the_profile(sim: Sim):
    r = sim.run("--profiles", "reference-8k-1slot candidate-16k-1slot", env={"CCE_NVIDIA_SMI": "/nonexistent"})
    assert r.returncode == 1, r.stdout + r.stderr
    summary = sim.evidence("summary.json")
    verdict = summary["verdicts"]["reference-8k-1slot"]
    assert verdict["verdict"] == "incomplete" and verdict["criteria"]["memory"] == "unavailable"
    assert "candidate-16k-1slot" not in summary["verdicts"] and "D-A09-05" in summary["escalation"]
    assert sim.evidence("final-state.json")["state"] == "stopped" and sim.host_state() == "stopped"


def test_failure_in_the_campaign_still_stops_and_restores_the_profile(sim: Sim):
    r = sim.run("--profiles", "reference-8k-1slot no-such-profile")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAILED" in r.stderr and "load no-such-profile" in r.stderr
    final = sim.evidence("final-state.json")
    assert final["state"] == "stopped" and final["fallback_stop"] is True  # stopped by the cleanup, not the controller
    assert sim.host_state() == "stopped" and sim.profile() == "reference-8k-1slot"


def wait_until(pred, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError("condition not reached")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_interruption_while_waiting_for_readiness_stops_the_instance(sim: Sim, sig: signal.Signals):
    env = {**sim.env, "FAKE_LLAMA_LOAD_S": "60"}  # the model never becomes ready during the test
    p = subprocess.Popen(sim.argv(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        wait_until(lambda: sim.host_state() == "running")
        p.send_signal(sig)
        out, err = p.communicate(timeout=120)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 130, out + err
    summary = sim.evidence("summary.json")
    assert summary["outcome"].startswith("interrupted") and summary["final"]["state"] == "stopped"
    assert summary["final"]["fallback_stop"] is True and sim.calls("stop-instances") == 1  # stopped by the cleanup
    assert sim.host_state() == "stopped"


def test_cleanup_failure_is_loud_and_nonzero(sim: Sim):
    env = {**sim.env, "FAKE_LLAMA_LOAD_S": "60", "FAKE_AWS_STOP_FAIL": "1"}
    p = subprocess.Popen(
        sim.argv("--stop-timeout", "2"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )
    try:
        wait_until(lambda: sim.host_state() == "running")
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=120)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 4, out + err
    assert f"CLEANUP FAILED: instance {IID} is running, NOT stopped" in err
    assert "stop_instances_failed" in err
    final = sim.evidence("final-state.json")
    assert final["state"] == "running" and final["fallback_stop"] is True and final["errors"]


def test_refuses_to_start_unless_the_instance_is_stopped(sim: Sim):
    subprocess.run([str(SIM_HOST), str(sim.tmp), "start"], check=True)
    wait_until(lambda: sim.host_state() == "running")
    r = sim.run()
    assert r.returncode == 2 and "must be stopped" in r.stderr
    assert sim.host_state() == "running" and sim.calls("stop-instances") == 0  # nothing was touched


def test_refuses_evidence_inside_the_repository(sim: Sim):
    argv = sim.argv()
    argv[argv.index("--evidence") + 1] = str(DEPLOY / "evidence-should-not-be-here")
    r = subprocess.run(argv, capture_output=True, text=True, env=sim.env, check=False)
    assert r.returncode == 2 and "outside the repository" in r.stderr
    assert not (DEPLOY / "evidence-should-not-be-here").exists()
