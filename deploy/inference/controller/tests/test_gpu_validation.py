"""The GPU-validation sequence (A09-1 review, finding 3), run end to end with simulated tools and no AWS resource:
a fake `aws` CLI, SSM commands executed locally, the simulated host of `sim_host.sh` (fake llama-server through the
real run-server.sh) and the real controller CLI. It must start from `stopped`, be started by a durable job, run the
profiles while the host is up, restore the original profile while the instance is still running, and always end
with the instance verified `stopped`, or say loudly that it is not. The fake `aws` rejects Run Command on an instance
that is not running, as the real service does."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import DEPLOY, HERE, Q8, free_port, server_env

SIM_HOST = HERE / "sim_host.sh"
SCRIPT = DEPLOY / "bench" / "gpu-validation.py"
IID = "i-0sim"


class Sim:
    def __init__(self, tmp: Path, fake_model: dict[str, str]) -> None:
        self.tmp = tmp
        self.port = free_port()
        self.env_file = server_env(tmp, fake_model["path"], self.port)
        self.ev = tmp / "evidence"
        aws = tmp / "aws"
        aws.write_text(f'#!/bin/sh\nexec {sys.executable} {HERE / "fake_aws.py"} "$@"\n')
        aws.chmod(0o755)
        smi = tmp / "nvidia-smi"
        smi.write_text(
            '#!/bin/sh\ncase "$*" in *name*) echo "NVIDIA L40S (simulated)";; *) echo "30500, 46068";; esac\n'
        )
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
model_alias = "{Q8}"
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
            "CCE_MEASURE_SCRIPT": str(HERE / "fake_measure.py"),
            "FAKE_MEASURE_DIR": str(tmp),
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
            "CCE_SERVER_LOG_CMD": f"cat {tmp / 'server.log'}",  # the journal of the simulated host
            "FAKE_AWS_DIR": str(tmp / "fake-aws"),
            "CCE_MANIFEST": fake_model["manifest"],
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
    s = Sim(tmp_path, fake_model)
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
    assert summary["verdicts"]["reference-8k-1slot"]["runs"] == {"c1": "pass", "overload": "pass"}
    assert summary["verdicts"]["candidate-16k-2slot"]["runs"] == {"c1": "pass", "c2": "pass"}
    # restored and verified through Run Command BEFORE the controller stop (the fake rejects it on a stopped host)
    assert summary["steps"]["profile_restore"] == "restored" and summary["steps"]["controller_stop"]["exit"] == 0
    final = sim.evidence("final-state.json")
    assert final["errors"] == [] and final["state"] == "stopped" and final["instance_stopped"] is True
    assert final["fallback_stop"] is False  # stopped by the controller, not by the cleanup
    profile = final["profile"]
    assert profile["status"] == "restored" and profile["original"] == profile["current"] == "reference-8k-1slot"
    assert len(profile["attempts"]) == 1
    assert sim.host_state() == "stopped" and sim.profile() == "reference-8k-1slot"  # original profile restored
    py = sim.evidence("preflight.json")["python"]
    assert py["executable"] == sys.executable  # every child runs on the controller interpreter
    # the AWS target artifact (Q8_0) end to end: controller alias, host record, every load and the verdicts
    assert sim.evidence("preflight.json")["target"]["model_id"] == Q8
    artifact = summary["steps"]["host_evidence"]["artifact"]
    assert artifact["match"] is True and artifact["served"]["model_id"] == Q8
    assert artifact["served"]["file"] == "Qwen3.6-27B-Q8_0.gguf" and artifact["served"]["alias"] == Q8
    assert sim.evidence("model-record.json")["sha256"] == artifact["expected"]["record_sha256"]
    load = sim.evidence("load-reference-8k-1slot.json")
    assert load["model"] == Q8 and load["model_ok"] is True and load["models_listed"] == [Q8]
    for v in summary["verdicts"].values():
        assert v["expected_model"] == Q8 and v["criteria"]["model"] == "pass"
    assert (sim.ev / "host-facts.json").exists() and (sim.ev / "boot-timings.json").exists()
    # the server journal of the boot proves every load put all layers on the GPU (cold start and campaign)
    offload = summary["steps"]["gpu_offload"]
    assert offload["job-cycle"]["status"] == "verified" and offload["job-cycle"]["offloaded"] == ["65/65"]
    assert offload["campaign"]["all_layers_on_gpu"] is True
    assert "offloaded 65/65 layers to GPU" in (sim.ev / "server-journal-job-cycle.log").read_text()


def test_memory_without_samples_does_not_pass_the_profile(sim: Sim):
    r = sim.run("--profiles", "reference-8k-1slot candidate-16k-1slot", env={"CCE_NVIDIA_SMI": "/nonexistent"})
    assert r.returncode == 1, r.stdout + r.stderr
    summary = sim.evidence("summary.json")
    verdict = summary["verdicts"]["reference-8k-1slot"]
    assert verdict["verdict"] == "incomplete" and verdict["criteria"]["memory"] == "unavailable"
    assert "candidate-16k-1slot" not in summary["verdicts"] and "D-A09-05" in summary["escalation"]
    assert sim.evidence("final-state.json")["state"] == "stopped" and sim.host_state() == "stopped"


def test_a_run_that_fails_is_recorded_and_fails_the_verdict(sim: Sim):
    """The c2 run of the two-slot profile runs and fails: its failure is recorded, never omitted from the verdict
    (de3e8e2 left it out and the profile passed on c1 alone)."""
    r = sim.run(
        "--profiles",
        "reference-8k-1slot candidate-16k-2slot",
        env={"FAKE_MEASURE_FAIL": "run:run-candidate-16k-2slot-c2"},
    )
    assert r.returncode == 1, r.stdout + r.stderr
    rec = sim.evidence("run-candidate-16k-2slot-c2.json")
    assert rec["status"] == "failed" and rec["exit"] == 1 and rec["tag"] == "c2" and "simulated" in rec["error"]
    verdict = sim.evidence("summary.json")["verdicts"]["candidate-16k-2slot"]
    assert verdict["verdict"] == "fail" and verdict["runs"] == {"c1": "pass", "c2": "fail"}
    assert verdict["failures"][0]["label"] == "run-candidate-16k-2slot-c2"
    assert "FAILED" in r.stderr and "D-A09-05" in r.stderr
    # the profile was changed: the cleanup restored it while the host was running, then stopped the instance
    final = sim.evidence("final-state.json")
    assert final["state"] == "stopped" and final["fallback_stop"] is True  # stopped by the cleanup, not the controller
    assert final["profile"]["status"] == "restored" and final["errors"] == []
    assert sim.host_state() == "stopped" and sim.profile() == "reference-8k-1slot"


def test_profile_restore_failure_still_stops_and_is_never_validated(sim: Sim):
    r = sim.run("--profiles", "reference-8k-1slot candidate-16k-1slot", env={"FAKE_AWS_RUN_FAIL": "--no-restart"})
    assert r.returncode == 1, r.stdout + r.stderr
    summary = sim.evidence("summary.json")
    assert {v["verdict"] for v in summary["verdicts"].values()} == {"pass"}  # the measurements themselves passed
    assert summary["outcome"].startswith("failed: profile restore")
    final = sim.evidence("final-state.json")
    # recorded apart: EC2 stopped (by the cleanup, the run never reached the controller stop), profile not restored
    assert final["instance_stopped"] is True and final["state"] == "stopped" and final["fallback_stop"] is True
    assert final["profile"]["status"] == "not_restored" and len(final["profile"]["attempts"]) == 2
    assert any(e.startswith("profile_restore_failed") for e in final["errors"])
    assert "controller_stop" not in summary["steps"] and sim.host_state() == "stopped"
    assert sim.profile() == "candidate-16k-1slot"  # visible: the host keeps the last profile measured


def test_interruption_during_the_campaign_restores_then_stops(sim: Sim):
    env = {**sim.env, "FAKE_MEASURE_HANG": "limits:candidate-16k-1slot"}
    p = subprocess.Popen(
        sim.argv("--profiles", "reference-8k-1slot candidate-16k-1slot"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        wait_until(lambda: (sim.tmp / "hang").exists(), timeout=120)
        assert sim.profile() == "candidate-16k-1slot"
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=120)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 130, out + err
    final = sim.evidence("final-state.json")
    assert final["profile"]["status"] == "restored" and final["instance_stopped"] is True and final["errors"] == []
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


def test_refuses_a_controller_alias_that_is_not_the_aws_target(sim: Sim):
    sim.cfg.write_text(sim.cfg.read_text().replace(f'model_alias = "{Q8}"', 'model_alias = "qwen3.6-27b-q4km"'))
    r = sim.run()
    assert r.returncode == 2 and "artifact mismatch" in r.stderr and Q8 in r.stderr
    assert sim.host_state() == "stopped" and sim.calls("stop-instances") == 0 and not sim.ev.exists()


def test_a_host_serving_another_artifact_fails_and_still_stops(sim: Sim, tmp_path: Path):
    """The host verified and serves its file, but it is not the artifact the manifest pins for AWS (another
    SHA-256): the validation fails explicitly after the job cycle and the instance ends stopped."""
    other = json.loads(Path(sim.env["CCE_MANIFEST"]).read_text())
    other["models"][Q8]["sha256"] = "0" * 64
    alt = tmp_path / "operator-manifest.json"
    alt.write_text(json.dumps(other))
    r = sim.run(env={"CCE_MANIFEST": str(alt)})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "artifact mismatch on the host" in r.stderr
    summary = sim.evidence("summary.json")
    assert summary["steps"]["host_evidence"]["artifact"]["match"] is False and summary["verdicts"] == {}
    assert sim.evidence("final-state.json")["state"] == "stopped" and sim.host_state() == "stopped"


def test_refuses_unknown_profiles_and_another_interpreter(sim: Sim):
    r = sim.run("--profiles", "reference-8k-1slot no-such-profile")
    assert r.returncode == 2 and "unknown profile 'no-such-profile'" in r.stderr
    r = sim.run(env={"CCE_PYTHON": "/usr/bin/false"})
    assert r.returncode == 2 and "controller interpreter" in r.stderr
    assert sim.host_state() == "stopped" and sim.calls("stop-instances") == 0 and not sim.ev.exists()


def test_refuses_evidence_inside_the_repository(sim: Sim):
    argv = sim.argv()
    argv[argv.index("--evidence") + 1] = str(DEPLOY / "evidence-should-not-be-here")
    r = subprocess.run(argv, capture_output=True, text=True, env=sim.env, check=False)
    assert r.returncode == 2 and "outside the repository" in r.stderr
    assert not (DEPLOY / "evidence-should-not-be-here").exists()


def test_a_load_that_does_not_offload_every_layer_fails_and_stops(sim: Sim):
    """D-A09-08: all weights on the GPU. A server log that shows a partial offload fails the run (the instance is
    still stopped and verified)."""
    r = sim.run("--profiles", "reference-8k-1slot", env={"FAKE_LLAMA_OFFLOAD": "40/65"})
    assert r.returncode == 1, r.stdout + r.stderr
    offload = sim.evidence("gpu-offload-job-cycle.json")
    assert (
        offload["status"] == "partial" and offload["all_layers_on_gpu"] is False and offload["offloaded"] == ["40/65"]
    )
    summary = sim.evidence("summary.json")
    assert summary["outcome"].startswith("failed: GPU offload") and "verdicts" in summary and not summary["verdicts"]
    assert sim.evidence("final-state.json")["instance_stopped"] is True and sim.host_state() == "stopped"


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("load_tensors: offloaded 65/65 layers to GPU\nload_tensors: offloaded 65/65 layers to GPU", "verified"),
        ("load_tensors: offloaded 65/65 layers to GPU\nload_tensors: offloaded 64/65 layers to GPU", "partial"),
        ("main: server is listening", "no_evidence"),
    ],
)
def test_gpu_offload_evidence_requires_every_load_complete(text: str, status: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location("gv", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    got = mod.gpu_offload(text)
    assert got["status"] == status and got["all_layers_on_gpu"] is (status == "verified")
