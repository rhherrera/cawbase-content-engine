"""Memory evidence of measure.py (A09-1 review, finding 2), without a GPU: the sampler runs on the measured host
(`mem-sample.sh`), the result records the host and the sample counts, and missing samples are `unavailable`, never a
fictitious 0 that would pass the memory criterion. The verdict needs evidence of every expected run of the profile
(A09-1 review 2): a missing run is `incomplete`, a run that ran and failed is `fail`."""

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
    load = {"kind": "load", "ready_s": 3.0, "total_slots": 1, "memory": mem}
    verdict = measure.profile_verdict("p", [load, res], slots=1)
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


MEM_OK = measure.memory_evidence(summary(vram_peak_mib=20000), WINDOW, None)
LIMITS = {
    "kind": "limits",
    "fit": {"result": {"status": 200, "json_ok": True}},
    "over_context": {"server_result": {"status": 400, "error_type": "exceed_context_size_error"}},
}


def load_doc(slots: int) -> dict:
    return {"kind": "load", "ready_s": 80.0, "total_slots": slots, "memory": MEM_OK}


def run_doc(tag: str, conc: int, **over) -> dict:
    base = {"kind": "run", "tag": tag, "concurrency": conc, "requests": 5, "ok": 5, "json_ok": 5, "memory": MEM_OK}
    return {**base, "finish_reason_length": 0, **over}


def failed_doc(kind: str, tag: str | None = None) -> dict:
    return {"kind": kind, "tag": tag, "label": f"{kind}-{tag}", "status": "failed", "exit": 1, "error": "boom"}


def test_verdict_passes_only_with_every_criterion_evidenced():
    load, run = load_doc(1), run_doc("c1", 1)
    assert measure.profile_verdict("p", [load, LIMITS, run], slots=1)["verdict"] == "pass"
    no_mem = {**run, "memory": measure.memory_evidence(None, WINDOW, None, reason="not_requested")}
    assert measure.profile_verdict("p", [load, LIMITS, no_mem], slots=1)["verdict"] == "incomplete"
    assert measure.profile_verdict("p", [load, LIMITS], slots=1)["verdict"] == "incomplete"  # no run at all
    truncated = {**run, "finish_reason_length": 1}
    assert measure.profile_verdict("p", [load, LIMITS, truncated], slots=1)["verdict"] == "fail"


def test_expected_runs_per_profile():
    assert measure.expected_runs(1, False) == {"c1": 1}
    assert measure.expected_runs(2, False) == {"c1": 1, "c2": 2}
    assert measure.expected_runs(1, True) == {"c1": 1, "overload": 3}


def run_verdict_cli(tmp_path: Path, docs: list[dict], *flags: str) -> tuple[int, dict]:
    files = []
    for i, d in enumerate(docs):
        f = tmp_path / f"e{i}.json"
        f.write_text(json.dumps(d))
        files.append(str(f))
    out = tmp_path / "verdict.json"
    cmd = [sys.executable, str(BENCH / "measure.py"), "verdict", "--profile", "p", *flags, "--out", str(out), *files]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert out.exists(), r.stderr
    return r.returncode, json.loads(out.read_text())


def test_two_slot_profile_with_concurrency_1_only_is_incomplete(tmp_path: Path):
    """Regression (review 2): on de3e8e2 this evidence gave `pass` (exit 0) for a two-slot profile."""
    code, v = run_verdict_cli(tmp_path, [load_doc(2), LIMITS, run_doc("c1", 1)], "--slots", "2")
    assert (code, v["verdict"], v["criteria"]["run"]) == (3, "incomplete", "unavailable")
    assert v["runs"] == {"c1": "pass", "c2": "missing"} and v["expected_runs"] == {"c1": 1, "c2": 2}
    code, v = run_verdict_cli(tmp_path, [load_doc(2), LIMITS, run_doc("c1", 1), run_doc("c2", 2)], "--slots", "2")
    assert (code, v["verdict"]) == (0, "pass")


def test_reference_profile_needs_the_overload_run(tmp_path: Path):
    code, v = run_verdict_cli(tmp_path, [load_doc(1), LIMITS, run_doc("c1", 1)], "--slots", "1", "--reference")
    assert (code, v["verdict"], v["runs"]) == (3, "incomplete", {"c1": "pass", "overload": "missing"})
    docs = [load_doc(1), LIMITS, run_doc("c1", 1), run_doc("overload", 3, requests=3, ok=3, json_ok=3)]
    assert run_verdict_cli(tmp_path, docs, "--slots", "1", "--reference")[1]["verdict"] == "pass"


def test_a_run_that_ran_and_failed_is_a_recorded_fail(tmp_path: Path):
    """Regression (review 2): a failed measurement is a `status: failed` record that fails the verdict."""
    docs = [load_doc(2), LIMITS, run_doc("c1", 1), failed_doc("run", "c2")]
    code, v = run_verdict_cli(tmp_path, docs, "--slots", "2")
    assert (code, v["verdict"], v["runs"]) == (1, "fail", {"c1": "pass", "c2": "fail"})
    assert v["failures"] == [{"kind": "run", "tag": "c2", "label": "run-c2", "exit": 1, "error": "boom"}]
    _, v = run_verdict_cli(tmp_path, [load_doc(1), failed_doc("limits"), run_doc("c1", 1)], "--slots", "1")
    assert v["criteria"]["limits"] == "fail" and v["verdict"] == "fail"
    _, v = run_verdict_cli(tmp_path, [failed_doc("load")], "--slots", "1")
    assert v["criteria"]["loaded"] == "fail" and v["verdict"] == "fail"


def test_wrong_concurrency_slots_or_unreadable_evidence_never_pass(tmp_path: Path):
    docs = [load_doc(2), LIMITS, run_doc("c1", 1), run_doc("c2", 1)]  # "c2" measured at concurrency 1
    assert run_verdict_cli(tmp_path, docs, "--slots", "2")[1]["runs"]["c2"] == "fail"
    docs = [load_doc(1), LIMITS, run_doc("c1", 1), run_doc("c2", 2)]  # the server runs 1 slot, the profile says 2
    assert run_verdict_cli(tmp_path, docs, "--slots", "2")[1]["criteria"]["slots"] == "fail"
    files = [tmp_path / "a.json", tmp_path / "b.json", tmp_path / "c.json"]
    for f, d in zip(files, [load_doc(1), LIMITS, run_doc("c1", 1)], strict=True):
        f.write_text(json.dumps(d))
    out = tmp_path / "v.json"
    cmd = [sys.executable, str(BENCH / "measure.py"), "verdict", "--profile", "p", "--slots", "1", "--out", str(out)]
    r = subprocess.run([*cmd, *map(str, files), str(tmp_path / "missing.json")], capture_output=True, check=False)
    v = json.loads(out.read_text())
    assert r.returncode == 3 and v["verdict"] == "incomplete" and v["unreadable"] == [str(tmp_path / "missing.json")]
