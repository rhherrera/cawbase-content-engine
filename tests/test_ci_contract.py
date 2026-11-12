"""The CI workflow and the "missing PostgreSQL must fail" rule are themselves tested."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
SHA = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+@[0-9a-f]{40}$")


def steps(job: str) -> list[dict]:
    return WORKFLOW["jobs"][job]["steps"]


def test_every_action_is_pinned_to_a_full_commit_sha():
    uses = [s["uses"] for job in WORKFLOW["jobs"].values() for s in job["steps"] if "uses" in s]
    assert uses and all(SHA.match(u) for u in uses), uses


def test_postgres_jobs_require_postgres_and_declare_the_service():
    for name in ("tests", "tests-lowest-bounds", "distribution"):
        job = WORKFLOW["jobs"][name]
        assert job["env"]["CCE_REQUIRE_POSTGRES"] == "1", name  # a missing PostgreSQL fails the job
        assert "CCE_TEST_POSTGRES_URL" in job["env"] and job["services"]["postgres"]["options"], name
    versions = WORKFLOW["jobs"]["tests"]["strategy"]["matrix"]["postgres"]
    assert versions == ["15", "16", "17", "18"]


def test_commands_install_what_they_use_and_test_outside_the_tree():
    run = lambda job: "\n".join(s.get("run", "") for s in steps(job))  # noqa: E731
    assert "uv sync --group dev --frozen" in run("tests")  # dev group carries the drivers/extras the tests need
    assert "--resolution lowest-direct" in run("tests-lowest-bounds")
    dist = run("distribution")
    assert "uv build" in dist and "audit_artifacts.py" in dist and "pytest -m dist" in dist
    assert "measure_distribution.py" in dist


def test_results_are_kept_as_artifacts_even_when_a_job_fails():
    for name in ("tests", "tests-lowest-bounds", "distribution"):
        uploads = [s for s in steps(name) if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
        assert uploads and all(u.get("if") == "always()" for u in uploads), name
        assert all(u["with"]["if-no-files-found"] == "error" for u in uploads)


def test_dev_group_contains_every_runtime_extra_needed_by_the_tests():
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dev = " ".join(data["dependency-groups"]["dev"])
    for dep in ("asyncpg", "aiosqlite", "alembic", "fastapi", "uvicorn", "httpx", "pytest-asyncio", "pyyaml"):
        assert dep in dev, dep
    uvicorn = next(d for d in data["project"]["optional-dependencies"]["http"] if d.startswith("uvicorn"))
    assert ">=0.50" in uvicorn  # supervisor stops on a worker startup failure only from 0.50 (release notes)


@pytest.mark.parametrize("url", [None, "postgresql+asyncpg://nobody:x@127.0.0.1:1/postgres"])
def test_missing_or_unreachable_postgres_fails_when_required(url, tmp_path):
    """Run a tiny PostgreSQL-parametrized test in a subprocess exactly like the CI job would."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CCE_")}
    env["CCE_REQUIRE_POSTGRES"] = "1"
    if url:
        env["CCE_TEST_POSTGRES_URL"] = url
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
            "tests/test_config_and_lifecycle.py::TestLifecycle::test_url_mode_start_and_close_dispose_pool",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0, r.stdout[-800:]
    assert re.search(r"\d+ (failed|error)", r.stdout), r.stdout[-400:]  # a fixture failure is reported as an error
    if url is None:
        assert "CCE_TEST_POSTGRES_URL is required" in r.stdout


def test_without_the_require_flag_a_missing_postgres_is_reported_as_skipped_not_passed():
    env = {k: v for k, v in os.environ.items() if not k.startswith("CCE_")}
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-rs",
            "tests/test_config_and_lifecycle.py::TestLifecycle::test_url_mode_start_and_close_dispose_pool",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert "CCE_TEST_POSTGRES_URL not set" in r.stdout and "1 skipped" in r.stdout
