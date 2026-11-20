"""Model selection (AWS target g6e.xlarge + Qwen3.6-27B Q8_0, on-premise Q4_K_M), without AWS or a GPU: the
manifest is the only source of the artifact data; the template, bootstrap, installer, server.env, unit, server
check, controller alias and measurement tools carry the selection; any artifact mismatch fails explicitly; the
existing on-premise Q4_K_M installation keeps working with no AWS dependency."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import math
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

from conftest import DEPLOY, Q4, Q8, SERVER, install_model, make_test_manifest, server_env
from fake_llama_server import FakeLlama

GIB = 1024**3
MANIFEST = json.loads((DEPLOY / "manifest.json").read_text())
TEMPLATE = (DEPLOY / "aws" / "cloudformation.yaml").read_text()
INSTALL_MODEL = str(SERVER / "install-model.sh")
RUN_SERVER = str(SERVER / "run-server.sh")


def sh(*args: str, env: dict[str, str] | None = None, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=timeout, check=False
    )


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = load("build_template", DEPLOY / "aws" / "build_template.py")
measure = load("measure", DEPLOY / "bench" / "measure.py")


# --- the pinned artifacts ----------------------------------------------------------------------------------------


def test_manifest_pins_q8_0_for_aws_and_keeps_q4_k_m_for_on_premise():
    assert MANIFEST["targets"] == {"aws": Q8, "onprem": Q4}
    rev = "fec6ff7c0c86cf07f6e5ae0a1391c3a042f2017f"
    q8, q4 = MANIFEST["models"][Q8], MANIFEST["models"][Q4]
    assert (q8["alias"], q8["quantization"], q8["revision"], q8["file"]) == (Q8, "Q8_0", rev, "Qwen3.6-27B-Q8_0.gguf")
    assert q8["url"] == f"https://huggingface.co/ggml-org/Qwen3.6-27B-GGUF/resolve/{rev}/Qwen3.6-27B-Q8_0.gguf"
    assert (q8["bytes"], q8["sha256"]) == (
        28595762752,
        "7cdc25ae00036f204a5748a63456f81ea11eec1225c2336064db157adc6f1c7c",
    )
    # Q4_K_M unchanged: on-premise target and the artifact of the historical 43/50
    assert (q4["alias"], q4["file"], q4["bytes"], q4["sha256"]) == (
        Q4,
        "Qwen3.6-27B-Q4_K_M.gguf",
        19403683392,
        "f3ab00cc923590947877bad882e09ada67789f747f5d3cb3c178fdf91b9aaab9",
    )
    assert "Q4_K_M ONLY" in MANIFEST["reference_profile"]["external_result"]
    assert MANIFEST["aws"]["instance_type"] == "g6e.xlarge" and "L40S" in MANIFEST["aws"]["gpu"]
    assert MANIFEST["aws"]["model"] == Q8 and MANIFEST["aws"]["historical"]["instance_type"] == "g6.xlarge"


# --- AWS: template, bundle, bootstrap ------------------------------------------------------------------------------


def param_block(name: str) -> str:
    m = re.search(rf"^  {name}:\n((?:    .*\n|      .*\n)+)", TEMPLATE, re.M)
    assert m, name
    return m.group(1)


def code(text: str) -> str:
    """The script without comment lines (documentation may name both artifacts)."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def bundled() -> dict[str, bytes]:
    b64 = re.search(r"- Bundle: (\S+)", TEMPLATE)
    assert b64
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64.group(1))), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}  # type: ignore[union-attr]


def test_template_selects_g6e_and_q8_0_with_a_volume_that_fits_it():
    assert "Default: g6e.xlarge" in param_block("InstanceType")
    assert f"--model {Q8} " in TEMPLATE and "Qwen3.6-27B Q8_0" in TEMPLATE
    vol = param_block("WeightsVolumeGiB")
    min_gib = int(re.search(r"MinValue: (\d+)", vol).group(1))  # type: ignore[union-attr]
    default_gib = int(re.search(r"Default: (\d+)", vol).group(1))  # type: ignore[union-attr]
    q8 = MANIFEST["models"][Q8]["bytes"]
    assert min_gib > 25 and min_gib * GIB * build.FS_USABLE_FRACTION >= q8 + GIB  # file + its temporary + headroom
    assert min_gib == math.ceil(q8 / GIB / build.FS_USABLE_FRACTION + build.HEADROOM_GIB) and default_gib >= min_gib
    report = build.report()
    assert report["user_data_worst_case_bytes"] <= report["user_data_limit_bytes"] == 16384
    assert sh(sys.executable, str(DEPLOY / "aws" / "build_template.py"), "--check").returncode == 0


def test_bundle_carries_the_q8_0_entry_and_valid_scripts(tmp_path: Path):
    files = bundled()
    host = json.loads(files["manifest.json"])
    assert host["targets"] == {"aws": Q8} and list(host["models"]) == [Q8]
    for key in ("file", "url", "bytes", "sha256", "alias", "revision"):
        assert host["models"][Q8][key] == MANIFEST["models"][Q8][key], key
    for name, data in files.items():
        if name.endswith(".sh"):
            script = tmp_path / name.replace("/", "_")
            script.write_bytes(data)
            assert sh("bash", "-n", str(script)).returncode == 0, name
    boot = files["aws/bootstrap.sh"].decode()
    assert "CCE_MODEL_ID=$MODEL" in boot and "CCE_MODEL_PATH=/srv/cce-inference/models/$MODEL_FILE" in boot
    assert "CCE_MODEL_ALIAS=$MODEL_ALIAS" in boot and '--model "$MODEL"' in boot and 'run-server.sh" --check' in boot
    assert "q4km" not in boot and "Q4_K_M" not in boot  # nothing in the AWS path names the on-premise artifact


def test_bootstrap_needs_an_explicit_model():
    r = sh(str(DEPLOY / "aws" / "bootstrap.sh"), "--volume-id", "vol-1", "--region", "r", "--api-key-param", "/p")
    assert r.returncode == 2 and "missing arguments" in r.stderr


def test_unit_checks_the_artifact_before_every_start():
    unit = (SERVER / "cce-inference.service").read_text()
    assert "ExecStartPre=/opt/cce-inference/src/server/run-server.sh --check" in unit and "Q4_K_M" not in unit
    run = code((SERVER / "run-server.sh").read_text())
    assert "-ngl all --fit off" in run and "q4km" not in run  # no hardcoded alias: it comes from the manifest


def test_controller_and_tools_use_the_served_alias():
    aws = (DEPLOY / "controller" / "controller.aws.example.toml").read_text()
    onprem = (DEPLOY / "controller" / "controller.onprem.example.toml").read_text()
    assert f'model_alias = "{Q8}"' in aws and f'model_alias = "{Q4}"' in onprem
    parity = code((DEPLOY / "bench" / "parity.sh").read_text())
    assert '--model "$MODEL" --output' in parity and "--model qwen3.6" not in parity
    assert (
        sh(
            "bash",
            str(DEPLOY / "bench" / "parity.sh"),
            "--pkg",
            "x",
            "--base-url",
            "u",
            "--key-file",
            "k",
            "--out",
            "/nonexistent",
        ).returncode
        == 2
    )  # --model is required


# --- explicit failures on an artifact mismatch ---------------------------------------------------------------------


@pytest.fixture
def installed(tmp_path: Path) -> dict[str, Path]:
    manifest, sources = make_test_manifest(tmp_path)
    q8 = install_model(manifest, Q8, tmp_path / "models", sources[Q8])
    q4 = install_model(manifest, Q4, tmp_path / "models", sources[Q4])
    return {"manifest": manifest, "q8": q8, "q4": q4, "q8_src": sources[Q8], "q4_src": sources[Q4]}


def check(env_file: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
    return sh(RUN_SERVER, "--check", env={"CCE_SERVER_ENV": str(env_file), "CCE_MANIFEST": str(manifest)})


def test_server_check_accepts_the_q8_0_target(tmp_path: Path, installed: dict[str, Path]):
    r = check(server_env(tmp_path, str(installed["q8"]), 8080), installed["manifest"])
    assert r.returncode == 0 and f"model {Q8} ok" in r.stdout


@pytest.mark.parametrize(
    ("case", "expect"),
    [
        ("q4_file_for_q8_id", "artifact mismatch: CCE_MODEL_PATH is Qwen3.6-27B-Q4_K_M.gguf"),
        ("q4_alias_for_q8_id", f"artifact mismatch: CCE_MODEL_ALIAS is {Q4}"),
        ("unknown_id", "unknown model 'qwen3.6-27b-bf16'"),
        ("no_record", "no verification record"),
        ("size_changed", "model size mismatch"),
    ],
)
def test_server_check_refuses_artifact_mismatches(tmp_path: Path, installed: dict[str, Path], case: str, expect: str):
    path, model_id, alias = str(installed["q8"]), Q8, Q8
    if case == "q4_file_for_q8_id":
        path = str(installed["q4"])
    elif case == "q4_alias_for_q8_id":
        alias = Q4
    elif case == "unknown_id":
        model_id = "qwen3.6-27b-bf16"
    elif case == "no_record":
        Path(path + ".verified.json").unlink()
    elif case == "size_changed":
        with open(path, "ab") as fh:
            fh.write(b"x")
    env_file = server_env(tmp_path, path, 8080, model_id=model_id, alias=alias)
    r = check(env_file, installed["manifest"])
    assert r.returncode != 0 and expect in r.stderr, r.stderr
    # the normal start runs the same check and never reaches the server binary
    r = sh(RUN_SERVER, env={"CCE_SERVER_ENV": str(env_file), "CCE_MANIFEST": str(installed["manifest"])}, timeout=30)
    assert r.returncode != 0 and expect in r.stderr and "cce-inference: model=" not in r.stderr


def test_install_refuses_a_file_that_is_not_the_pinned_artifact(tmp_path: Path, installed: dict[str, Path]):
    d = tmp_path / "fresh"
    r = sh(
        INSTALL_MODEL,
        "--model",
        Q8,
        "--dir",
        str(d),
        "--from",
        str(installed["q4_src"]),
        env={"CCE_MANIFEST": str(installed["manifest"])},
    )
    assert r.returncode != 0 and ("size mismatch" in r.stderr or "sha256 mismatch" in r.stderr)
    assert not any(d.iterdir())  # no final file, no partial, no record
    r = sh(
        INSTALL_MODEL, "--model", "qwen3.6-27b-bf16", "--dir", str(d), env={"CCE_MANIFEST": str(installed["manifest"])}
    )
    assert r.returncode != 0 and "unknown model" in r.stderr


def test_install_fails_explicitly_without_space_for_the_artifact(tmp_path: Path, installed: dict[str, Path]):
    huge = json.loads(installed["manifest"].read_text())
    huge["models"][Q8]["bytes"] = 10**15  # 1 PB: no volume has room for it
    m = tmp_path / "huge.json"
    m.write_text(json.dumps(huge))
    d = tmp_path / "small"
    r = sh(INSTALL_MODEL, "--model", Q8, "--dir", str(d), env={"CCE_MANIFEST": str(m)})
    assert r.returncode != 0 and "not enough space" in r.stderr and not any(d.iterdir())


def test_measurements_require_the_served_alias(tmp_path: Path):
    fake = FakeLlama(api_key="k", alias=Q8).start()
    try:
        key = tmp_path / "key"
        key.write_text("k\n")
        docs = {}
        for alias in (Q8, Q4):
            out = tmp_path / f"load-{alias}.json"
            r = sh(
                sys.executable,
                str(DEPLOY / "bench" / "measure.py"),
                "load",
                "--base-url",
                fake.base_url,
                "--key-file",
                str(key),
                "--model",
                alias,
                "--restart-cmd",
                "true",
                "--timeout",
                "20",
                "--out",
                str(out),
            )
            assert r.returncode == 0, r.stderr
            docs[alias] = json.loads(out.read_text())
    finally:
        fake.stop()
    assert docs[Q8]["model_ok"] is True and docs[Q8]["ready_s"] and docs[Q8]["model"] == Q8
    assert docs[Q4]["model_ok"] is False and "ready_s" not in docs[Q4]  # another alias is never "ready"
    v = measure.profile_verdict("p", [docs[Q4]], slots=1, model=Q4)
    assert v["criteria"]["model"] == "fail" and v["verdict"] == "fail"
    v = measure.profile_verdict("p", [{**docs[Q8], "model": Q4}], slots=1, model=Q8)
    assert v["criteria"]["model"] == "fail"


# --- on-premise Q4_K_M keeps working -------------------------------------------------------------------------------


def test_existing_on_premise_q4_k_m_installation_keeps_working(tmp_path: Path, installed: dict[str, Path]):
    """A server.env written before the model selection (no CCE_MODEL_ID) and a verification record in the old
    format (no "model" key): the check resolves the manifest's on-premise target (Q4_K_M) and passes."""
    record = Path(str(installed["q4"]) + ".verified.json")
    old = json.loads(record.read_text())
    record.write_text(
        f'{{"file": "{old["file"]}", "bytes": {old["bytes"]}, "sha256": "{old["sha256"]}", '
        f'"verified_at": "2026-11-20T00:00:00Z"}}\n'
    )
    env_file = server_env(tmp_path, str(installed["q4"]), 8080, model_id=None, alias=Q4)
    assert "CCE_MODEL_ID" not in env_file.read_text()
    r = check(env_file, installed["manifest"])
    assert r.returncode == 0 and f"model {Q4} ok" in r.stdout, r.stderr
    cmd = sh(
        RUN_SERVER, "--print-command", env={"CCE_SERVER_ENV": str(env_file), "CCE_MANIFEST": str(installed["manifest"])}
    )
    assert f"--alias {Q4}" in cmd.stdout and "-ngl all --fit off" in cmd.stdout


def test_on_premise_installer_defaults_to_q4_k_m_without_aws():
    script = (DEPLOY / "onprem" / "install.sh").read_text()
    assert "--target onprem" in script and '--model "$MODEL"' in script and "CCE_MODEL_ID=$MODEL" in script
    assert not re.search(r"\baws\b|SSM|169\.254\.169\.254", code(script))  # no AWS CLI, Parameter Store or IMDS
    assert sh(INSTALL_MODEL, "--target", "onprem").stdout.strip() == Q4
