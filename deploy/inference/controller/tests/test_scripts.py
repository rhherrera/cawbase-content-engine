"""Host-side scripts and the operator CLI, exercised for real (shell + subprocesses) with a fake model server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import DEPLOY, HERE, Q4, Q8, SERVER, free_port, install_model, make_test_manifest, server_env

CLI = [str(DEPLOY / "bin" / "cce-inference")]


def sh(*args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, env={**os.environ, **(env or {})}, check=check)


def test_install_model_verifies_hash_size_and_is_atomic(tmp_path: Path):
    manifest, sources = make_test_manifest(tmp_path)
    env = {"CCE_MANIFEST": str(manifest)}
    entry = json.loads(manifest.read_text())["models"][Q8]
    d = tmp_path / "models"
    im = str(SERVER / "install-model.sh")
    r = sh(im, "--model", Q8, "--dir", str(d), "--from", str(sources[Q8]), env=env)
    assert "installed" in r.stdout and (d / "Qwen3.6-27B-Q8_0.gguf").exists() and not list(d.glob(".*partial"))
    record = json.loads((d / "Qwen3.6-27B-Q8_0.gguf.verified.json").read_text())
    assert (record["model"], record["sha256"], record["bytes"]) == (Q8, entry["sha256"], entry["bytes"])
    assert "sha256" in sh(im, "--model", Q8, "--dir", str(d), "--verify", env=env).stdout
    assert "quick" in sh(im, "--model", Q8, "--dir", str(d), "--verify", "--quick", env=env).stdout
    # download path (the pinned URL of the manifest; a file:// URL here)
    r = sh(im, "--model", Q4, "--dir", str(tmp_path / "dl"), env=env)
    assert "installed" in r.stdout and (tmp_path / "dl" / "Qwen3.6-27B-Q4_K_M.gguf").exists()
    # tampered installed file: full verification fails
    with open(d / "Qwen3.6-27B-Q8_0.gguf", "r+b") as fh:
        fh.write(b"XXXX")
    r = sh(im, "--model", Q8, "--dir", str(d), "--verify", env=env, check=False)
    assert r.returncode != 0 and "sha256 mismatch" in r.stderr


def test_run_server_command_line_is_the_reference_profile(tmp_path: Path, fake_model: dict[str, str]):
    env = server_env(tmp_path, fake_model["path"], 8080)
    out = sh(str(SERVER / "run-server.sh"), "--print-command", env={"CCE_SERVER_ENV": str(env)}).stdout.split()
    joined = " ".join(out)
    for frag in (
        "-c 8192",
        "--parallel 1",
        "-ngl all",
        "--fit off",
        "--reasoning on",
        "--reasoning-format deepseek",
        "--reasoning-budget 512",
        "--metrics",
        "--no-webui",
        f"--alias {Q8}",
        "-m " + fake_model["path"],
    ):
        assert frag in joined, frag
    two = server_env(tmp_path, fake_model["path"], 8080, "candidate-16k-2slot")
    joined = " ".join(
        sh(str(SERVER / "run-server.sh"), "--print-command", env={"CCE_SERVER_ENV": str(two)}).stdout.split()
    )
    assert "-c 16384" in joined and "--parallel 2" in joined


@pytest.mark.parametrize("target", ["aws-q8_0", "onprem-legacy-q4km"])
def test_cli_start_stop_status_cycle_with_a_simulated_host(tmp_path: Path, fake_model: dict[str, str], target: str):
    """The same cycle for the AWS target (server.env written with CCE_MODEL_ID = Q8_0) and for an existing on-premise
    Q4_K_M installation whose server.env predates the model selection (no CCE_MODEL_ID): both pass the artifact
    check and serve their own alias."""
    port = free_port()
    if target == "aws-q8_0":
        alias = Q8
        server_env(tmp_path, fake_model["path"], port)
    else:
        alias = Q4
        q4 = install_model(Path(fake_model["manifest"]), Q4, tmp_path / "onprem", Path(fake_model["q4_src"]))
        server_env(tmp_path, str(q4), port, model_id=None, alias=Q4)
    host = HERE / "sim_host.sh"
    cfg = tmp_path / "controller.toml"
    cfg.write_text(
        f"""
[controller]
db_path = "{tmp_path / "state.db"}"
tick_interval_s = 0.2
lease_ttl_s = 5
[policy]
readiness_timeout_s = 20
drain_timeout_s = 20
[executor]
inference_timeout_s = 10
[endpoint]
base_url = "http://127.0.0.1:{port}"
model_alias = "{alias}"
api_key_file = "{tmp_path / "api-key"}"
canary_timeout_s = 10
[backend]
kind = "command"
describe = "{host} {tmp_path} describe"
start = "{host} {tmp_path} start"
stop = "{host} {tmp_path} stop"
"""
    )
    env = {"CCE_INFERENCE_CONFIG": str(cfg)}

    def cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return sh(*CLI, *args, env=env, check=check)

    try:
        st = json.loads(cli("status", "--json").stdout)
        assert (st["instance"], st["phase"], st["autostart_paused"]) == ("stopped", "stopped", False)
        job = tmp_path / "job.json"
        job.write_text(
            json.dumps(
                {
                    "messages": [{"role": "user", "content": "texto sintético"}],
                    "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                    "max_tokens": 32,
                }
            )
        )
        jid = cli("submit", str(job)).stdout.strip()
        t0 = time.monotonic()
        r = cli("start", "--wait", "20")
        assert "phase=ready" in r.stdout, r.stdout + r.stderr
        assert time.monotonic() - t0 < 20
        st = json.loads(cli("status", "--json").stdout)
        assert st["instance"] == "running" and st["readiness"].startswith("ready") and st["dispatch_allowed"]
        assert cli("work", "--once").stdout.strip() == "succeeded"
        assert json.loads(cli("job", jid).stdout)["status"] == "succeeded"
        assert cli("probe", "--full").stdout.startswith("ready canary_ok")
        # stop: pause auto-start, drain, stop; work submitted afterwards stays pending and does not start the host
        assert "phase=stopped" in cli("stop", "--wait", "20").stdout
        late = cli("submit", str(job)).stdout.strip()
        cli("tick")
        st = json.loads(cli("status", "--json").stdout)
        assert (st["instance"], st["autostart_paused"], st["pending"]) == ("stopped", True, 1)
        assert json.loads(cli("job", late).stdout)["status"] == "pending"
        # start resumes and the pending work runs
        assert "phase=ready" in cli("start", "--wait", "20").stdout
        assert cli("work", "--once").stdout.strip() == "succeeded"
        kinds = [json.loads(line)["kind"] for line in cli("events", "--limit", "200").stdout.splitlines()]
        assert kinds.count("manual_start") == 2 and "manual_stop" in kinds and "ready" in kinds
    finally:
        sh(str(host), str(tmp_path), "stop", check=False)


def test_cloudformation_template_is_generated_from_the_sources():
    r = sh(sys.executable, str(DEPLOY / "aws" / "build_template.py"), "--check")
    assert "up to date" in r.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="needs /proc/uptime (Linux host)")
def test_idle_guard_dry_run(tmp_path: Path):
    from fake_llama_server import FakeLlama

    fake = FakeLlama(api_key="k").start()
    key = tmp_path / "key"
    key.write_text("k\n")
    env = {
        "CCE_GUARD_METRICS_URL": fake.base_url + "/metrics",
        "CCE_GUARD_STATE": str(tmp_path / "state"),
        "CCE_API_KEY_FILE": str(key),
        "CCE_GUARD_DRY_RUN": "1",
        "CCE_GUARD_IDLE_MIN": "0",
    }
    try:
        r = sh(str(SERVER / "idle-guard.sh"), env=env)
        assert "DRY RUN: poweroff" in r.stdout
        env["CCE_GUARD_IDLE_MIN"] = "120"
        r = sh(str(SERVER / "idle-guard.sh"), env=env)
        assert "DRY RUN" not in r.stdout
    finally:
        fake.stop()
