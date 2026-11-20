"""Memory evidence of measure.py (A09-1 review, finding 2), without a GPU: the sampler runs on the measured host
(`mem-sample.sh`), the result records the host and the sample counts, and missing samples are `unavailable`, never a
fictitious 0 that would pass the memory criterion."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from conftest import DEPLOY
from fake_llama_server import FakeLlama

BENCH = DEPLOY / "bench"


def load_measure() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure", BENCH / "measure.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


measure = load_measure()


def host_env(tmp_path: Path, *, gpu: bool, instance_id: str = "i-0sim") -> dict[str, str]:
    smi = tmp_path / "nvidia-smi"
    smi.write_text('#!/bin/sh\ncase "$*" in *name*) echo "NVIDIA L4 (simulated)";; *) echo "17000, 23034";; esac\n')
    smi.chmod(0o755)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16000000 kB\nMemAvailable:    4000000 kB\n")
    dmi = tmp_path / "dmi"
    dmi.mkdir(exist_ok=True)
    (dmi / "board_asset_tag").write_text(instance_id + "\n")
    return {
        "CCE_BENCH_DIR": str(tmp_path / "bench"),
        "CCE_NVIDIA_SMI": str(smi) if gpu else str(tmp_path / "no-nvidia-smi"),
        "CCE_MEMINFO": str(meminfo),
        "CCE_DMI_DIR": str(dmi),
        "CCE_IMDS_BASE": "http://127.0.0.1:9",
    }


def run_measure(tmp_path: Path, base_url: str, env: dict[str, str], *extra: str) -> dict:
    out = tmp_path / "run.json"
    key = tmp_path / "key"
    key.write_text("k\n")
    cmd = [
        sys.executable,
        str(BENCH / "measure.py"),
        "run",
        "--base-url",
        base_url,
        "--key-file",
        str(key),
        "--requests",
        "3",
        "--concurrency",
        "1",
        "--prompt-tokens",
        "200",
        "--max-tokens",
        "64",
        "--out",
        str(out),
        *extra,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **env}, check=False)
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text())


@pytest.fixture
def fake_server():
    fake = FakeLlama(api_key="k", gen_s=0.6).start()  # the load lasts long enough for several samples
    yield fake
    fake.stop()


def test_memory_sampled_on_the_host_during_the_same_load(tmp_path: Path, fake_server: FakeLlama):
    env = host_env(tmp_path, gpu=True)
    sampler = BENCH / "mem-sample.sh"
    res = run_measure(
        tmp_path,
        fake_server.base_url,
        env,
        "--memory-start-cmd",
        f"{sampler} start t1 0.2",
        "--memory-stop-cmd",
        f"{sampler} stop t1",
        "--expect-instance-id",
        "i-0sim",
    )
    mem = res["memory"]
    assert mem["status"] == "measured" and mem["criterion"] == "pass"
    assert mem["host"]["instance_id"] == "i-0sim" and mem["host"]["gpu_name"] == "NVIDIA L4 (simulated)"
    assert mem["samples"]["vram_samples"] >= 3 and mem["samples"]["ram_samples"] >= 3
    assert (mem["vram_peak_mib"], mem["vram_total_mib"], mem["vram_free_min_mib"]) == (17000, 23034, 6034)
    assert mem["ram_used_peak_kib"] == 12_000_000
    lw, sw = res["load_window"], mem["sampler_window"]
    assert sw[0] <= lw["start_epoch"] + 2 and sw[1] >= lw["end_epoch"] - 2


def test_memory_local_mode_and_no_gpu_is_unavailable_not_zero(tmp_path: Path, fake_server: FakeLlama):
    res = run_measure(tmp_path, fake_server.base_url, host_env(tmp_path, gpu=False), "--memory-local")
    mem = res["memory"]
    assert mem["status"] == "unavailable" and mem["reason"] == "no_samples" and mem["criterion"] == "unavailable"
    assert mem["samples"]["vram_samples"] == 0 and mem["samples"]["ram_samples"] >= 1
    assert "vram_peak_mib" not in mem and "vram_free_min_mib" not in mem
    verdict = measure.profile_verdict("p", [{"kind": "load", "ready_s": 3.0, "memory": mem}, res])
    assert verdict["criteria"]["memory"] == "unavailable" and verdict["verdict"] == "incomplete"


def test_memory_not_requested_from_the_operator_is_unavailable(tmp_path: Path, fake_server: FakeLlama):
    res = run_measure(tmp_path, fake_server.base_url, host_env(tmp_path, gpu=True))
    assert res["memory"] == {"status": "unavailable", "reason": "not_requested", "criterion": "unavailable"}


def summary(**over) -> str:
    base = {
        "label": "x",
        "host": {"hostname": "h", "instance_id": "i-0sim", "instance_type": "g6.xlarge", "gpu_name": "NVIDIA L4"},
        "started_epoch": 100,
        "stopped_epoch": 200,
        "vram_samples": 10,
        "ram_samples": 10,
        "vram_peak_mib": 22500,
        "vram_total_mib": 23034,
        "ram_used_peak_kib": 1,
        "status": "ok",
    }
    return json.dumps({**base, **over})


WINDOW = {"start_epoch": 110.0, "end_epoch": 190.0}


@pytest.mark.parametrize(
    ("raw", "expect", "reason"),
    [
        (None, None, "no_summary"),
        ("not json", None, "invalid_summary"),
        (summary(vram_samples=0, vram_peak_mib=None, status="unavailable"), None, "no_samples"),
        (summary(ram_samples=0), None, "no_samples"),
        (summary(), "i-0other", "host_mismatch"),
        (summary(started_epoch=150), None, "window_not_covered"),
        (summary(stopped_epoch=150), None, "window_not_covered"),
    ],
)
def test_memory_evidence_unavailable_cases(raw, expect, reason):
    mem = measure.memory_evidence(raw, WINDOW, expect)
    assert (mem["status"], mem["reason"], mem["criterion"]) == ("unavailable", reason, "unavailable")


def test_memory_criterion_uses_the_free_vram_at_the_peak():
    tight = measure.memory_evidence(summary(), WINDOW, "i-0sim")
    assert tight["status"] == "measured" and tight["vram_free_min_mib"] == 534 and tight["criterion"] == "fail"
    roomy = measure.memory_evidence(summary(vram_peak_mib=21000), WINDOW, "i-0sim")
    assert roomy["criterion"] == "pass"


def test_verdict_passes_only_with_every_criterion_evidenced():
    mem_ok = measure.memory_evidence(summary(vram_peak_mib=20000), WINDOW, None)
    load = {"kind": "load", "ready_s": 80.0, "memory": mem_ok}
    limits = {
        "kind": "limits",
        "fit": {"result": {"status": 200, "json_ok": True}},
        "over_context": {"server_result": {"status": 400, "error_type": "exceed_context_size_error"}},
    }
    run = {"kind": "run", "requests": 5, "ok": 5, "json_ok": 5, "finish_reason_length": 0, "memory": mem_ok}
    assert measure.profile_verdict("p", [load, limits, run])["verdict"] == "pass"
    no_mem = {**run, "memory": measure.memory_evidence(None, WINDOW, None, reason="not_requested")}
    assert measure.profile_verdict("p", [load, limits, no_mem])["verdict"] == "incomplete"
    assert measure.profile_verdict("p", [load, limits])["verdict"] == "incomplete"  # no run at all
    truncated = {**run, "finish_reason_length": 1}
    assert measure.profile_verdict("p", [load, limits, truncated])["verdict"] == "fail"
