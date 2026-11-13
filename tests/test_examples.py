"""The documented examples run as written (AC-07)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("CAWBASE")}


def test_python_host_example(tmp_path):
    r = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "python_host.py"), str(tmp_path / "e.db")],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "migrated: ('0001', '0002', '0003')" in r.stdout
    assert "isolation: project-a cannot read project-b" in r.stdout
    assert "project-c" not in r.stdout.split("after host rollback:")[1]
    assert "cce1." not in r.stdout + r.stderr


def test_profiles_example(tmp_path):  # AC-A02-01
    r = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "profiles_host.py"), str(tmp_path / "p.db")],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "migrated: ('0001', '0002', '0003')" in out and "| 6 types" in out
    assert "published: version 1 {'classification': True, 'detection': True}" in out
    assert "version 1 unchanged after editing: True" in out
    assert "no default profile" in out and "resolved: default -> version 1" in out
    assert "disabled profile: not selectable" in out and "history readable: True" in out
    assert "cce1." not in out + r.stderr


def test_standalone_service_example():
    env = {**clean_env(), "PORT": "18931", "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}
    r = subprocess.run(
        ["bash", str(ROOT / "examples" / "standalone_service.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        cwd=ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"scope_id":"demo"' in r.stdout and "unauthenticated -> 401" in r.stdout
    assert "cce1." not in r.stdout + r.stderr


def test_analysis_example(tmp_path):  # AC-A03-01, 02, 53
    r = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "analysis_host.py"), str(tmp_path / "a.db")],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "status: completed | provenance: mock simulated = True" in r.stdout
    assert "selected: finance | reason: priority_tiebreak" in r.stdout
    assert (
        "analysis:execute cannot read profiles: denied" in r.stdout
        and "no fixture: inference_fixture_not_found" in r.stdout
    )
    assert "cce1." not in r.stdout + r.stderr
