"""Build wheel + sdist and install them into CLEAN environments, outside the source tree.

Marked ``dist``: slow and needs a package index. Run with ``pytest -m dist``.
Covers AC-01..AC-03, AC-04 (in installed form), migration resources in the distribution (AC-20/22).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.dist
ROOT = Path(__file__).resolve().parent.parent
UV = shutil.which("uv")
PY = "3.13"

HOST_SCRIPT = r"""
import asyncio, json, sys
from cawbase_content_engine import (ContentEngine, EngineConfig, create_initial_admin_key, __version__)
from cawbase_content_engine.migrations import upgrade
import cawbase_content_engine, pathlib

async def main(db):
    cfg = EngineConfig(database_url=f"sqlite+aiosqlite:///{db}")
    first = await upgrade(cfg)
    second = await upgrade(cfg)
    async with ContentEngine(cfg) as engine:
        key = await create_initial_admin_key(engine)
        principal = await engine.authenticate_api_key(key.secret)
        admin = engine.admin(principal.admin_context())
        await admin.create_scope("a", "A")
        ctx = engine.trusted_scope_context("a", ["scope:read"])
        scope = await engine.scoped(ctx).get_scope("a")
    print(json.dumps({"version": __version__, "applied": list(first.applied), "again": second.already_current,
                      "scope": scope.scope_id, "installed_from": str(pathlib.Path(cawbase_content_engine.__file__))}))

asyncio.run(main(sys.argv[1]))
"""


def run(cmd, **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=kw.pop("check", True), **kw)


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory) -> dict[str, Path]:
    assert UV, "uv is required"
    out = tmp_path_factory.mktemp("dist")
    run([UV, "build", "--out-dir", str(out), str(ROOT)])
    wheel = next(out.glob("*.whl"))
    sdist = next(out.glob("*.tar.gz"))
    return {"wheel": wheel, "sdist": sdist}


def make_env(tmp_path: Path, name: str, spec: str) -> Path:
    venv = tmp_path / name
    run([UV, "venv", "--python", PY, str(venv)])
    run([UV, "pip", "install", "--python", str(venv / "bin" / "python"), spec])
    return venv / "bin"


def py(bin_dir: Path, *args: str, cwd: Path, check=True) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "CAWBASE", "VIRTUAL_ENV"))}
    return run([str(bin_dir / "python"), "-I", *args], cwd=cwd, env=env, check=check)


def installed(bin_dir: Path, cwd: Path) -> set[str]:
    out = py(
        bin_dir,
        "-c",
        "import importlib.metadata as m, json; print(json.dumps(sorted(d.metadata['Name'].lower() for d in m.distributions())))",
        cwd=cwd,
    )
    return set(json.loads(out.stdout))


def test_wheel_and_sdist_contents(artifacts):
    with zipfile.ZipFile(artifacts["wheel"]) as z:
        names = set(z.namelist())
    assert "cawbase_content_engine/migrations/versions/0001_initial.py" in names  # migration resources shipped
    assert "cawbase_content_engine/py.typed" in names
    for shipped in (
        "migrations/versions/0003_jobs_files.py",
        "jobs/storage.py",
        "documents/child.py",
        "http/jobs_routes.py",
    ):
        assert f"cawbase_content_engine/{shipped}" in names, shipped  # A05 modules ship in the wheel
    assert not [n for n in names if "experiments" in n]  # the A04 experiments never do
    assert not [n for n in names if n.startswith("tests/")]
    import tarfile

    with tarfile.open(artifacts["sdist"]) as t:
        s = set(t.getnames())
    assert any(n.endswith("migrations/versions/0001_initial.py") for n in s) and any("/tests/" in n for n in s)
    assert not any(n.endswith((".DS_Store", ".env")) for n in s)


def test_base_install_is_light_and_separate_from_http(artifacts, tmp_path):  # AC-01, AC-02
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    base = make_env(tmp_path, "base", str(artifacts["wheel"]))
    pkgs = installed(base, cwd)
    assert not ({"fastapi", "uvicorn", "starlette", "alembic", "asyncpg", "aiosqlite", "httpx"} & pkgs), pkgs
    assert py(base, "-c", "import cawbase_content_engine as c; print(c.__version__)", cwd=cwd).stdout.strip() == "0.4.0"
    for mod in ("fastapi", "alembic", "cawbase_content_engine.http"):
        assert py(base, "-c", f"import {mod}", cwd=cwd, check=False).returncode != 0, mod
    # the CLI is available and degrades with a clear message instead of a traceback
    cli = [str(base / "cawbase-content-engine")]
    assert run([*cli, "version"], cwd=cwd).stdout.strip() == "0.4.0"
    r = run(
        [*cli, "migrate"],
        cwd=cwd,
        check=False,
        env={"PATH": os.environ["PATH"], "CAWBASE_CONTENT_ENGINE_DATABASE_URL": f"sqlite+aiosqlite:///{cwd}/x.db"},
    )
    assert r.returncode == 2 and "Traceback" not in r.stderr
    # base import in the INSTALLED form is light too (AC-04)
    probe = "import sys, cawbase_content_engine; print(sorted({m.split('.')[0] for m in sys.modules} & {'fastapi','alembic','asyncpg','aiosqlite','uvicorn','starlette'}))"
    assert py(base, "-c", probe, cwd=cwd).stdout.strip() == "[]"

    http = make_env(tmp_path, "http", f"cawbase-content-engine[http,sqlite] @ {artifacts['wheel'].as_uri()}")
    assert {"fastapi", "uvicorn", "alembic", "aiosqlite"} <= installed(http, cwd)
    assert (
        py(
            http, "-c", "import cawbase_content_engine.http as h; print(h.create_router.__name__)", cwd=cwd
        ).stdout.strip()
        == "create_router"
    )


def test_documents_extra_is_separate_and_extracts_from_the_installed_wheel(artifacts, tmp_path):  # AC-A05-27
    cwd = tmp_path / "elsewhere-docs"
    cwd.mkdir()
    base = make_env(tmp_path, "docs-base", f"cawbase-content-engine[sqlite] @ {artifacts['wheel'].as_uri()}")
    assert not {"lxml", "pikepdf"} & installed(base, cwd)  # the light install never pulls the document libraries
    docs = make_env(tmp_path, "docs", f"cawbase-content-engine[sqlite,documents] @ {artifacts['wheel'].as_uri()}")
    assert {"lxml", "pikepdf"} <= installed(docs, cwd)
    script = (
        "import io, json, dataclasses, pikepdf\n"
        "from cawbase_content_engine.documents.child import process\n"
        "from cawbase_content_engine.jobs.setup import JobsLimits\n"
        "from cawbase_content_engine.documents.common import Limits\n"
        "pdf = pikepdf.new()\n"
        "font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding))\n"
        "page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 612, 792], Contents=pikepdf.Stream(pdf, b'BT /F1 12 Tf 72 720 Td (Hola) Tj ET'), Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)))\n"
        "pdf.pages.append(pikepdf.Page(page))\n"
        "buf = io.BytesIO(); pdf.save(buf, object_stream_mode=pikepdf.ObjectStreamMode.disable)\n"
        "p = dataclasses.asdict(JobsLimits())\n"
        "out = process(buf.getvalue(), {k: p[k] for k in Limits.__dataclass_fields__ if k in p})\n"
        "print(json.dumps([u['text'] for u in out['units']]))\n"
    )
    assert py(docs, "-c", script, cwd=cwd).stdout.strip() == '["Hola"]'


def test_python_host_from_installed_wheel_without_http(artifacts, tmp_path):  # AC-03, AC-07, AC-51
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    env = make_env(tmp_path, "host", f"cawbase-content-engine[sqlite,migrations] @ {artifacts['wheel'].as_uri()}")
    pkgs = installed(env, cwd)
    assert not ({"fastapi", "uvicorn", "starlette", "asyncpg"} & pkgs), pkgs
    (cwd / "host.py").write_text(HOST_SCRIPT)
    out = json.loads(py(env, str(cwd / "host.py"), str(cwd / "db.sqlite"), cwd=cwd).stdout)
    assert out["applied"] == ["0001", "0002", "0003"] and out["again"] is True and out["scope"] == "a"
    assert "site-packages" in out["installed_from"] and str(ROOT) not in out["installed_from"]


def test_postgres_extra_alone_has_no_sqlite_driver(artifacts, tmp_path):  # AC-03
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    env = make_env(tmp_path, "pg", f"cawbase-content-engine[postgres] @ {artifacts['wheel'].as_uri()}")
    pkgs = installed(env, cwd)
    assert "asyncpg" in pkgs and "aiosqlite" not in pkgs
    code = (
        "from cawbase_content_engine import ContentEngine, EngineConfig, InvalidConfigurationError\n"
        "e = ContentEngine(EngineConfig(database_url='sqlite+aiosqlite:////tmp/x.db'))\n"
        "import asyncio\n"
        "try:\n    asyncio.run(e.start())\nexcept InvalidConfigurationError as x:\n    print('config-error', 'extra' in str(x))\n"
    )
    assert py(env, "-c", code, cwd=cwd).stdout.strip() == "config-error True"


def test_sdist_builds_and_installs(artifacts, tmp_path):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    env = make_env(tmp_path, "sdist", f"cawbase-content-engine[sqlite,migrations] @ {artifacts['sdist'].as_uri()}")
    (cwd / "host.py").write_text(HOST_SCRIPT)
    out = json.loads(py(env, str(cwd / "host.py"), str(cwd / "db.sqlite"), cwd=cwd).stdout)
    assert out["applied"] == ["0001", "0002", "0003"]


def test_installed_service_starts_and_stops(artifacts, tmp_path):  # AC-02
    import socket
    import time

    import httpx

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    env_dir = make_env(tmp_path, "svc", f"cawbase-content-engine[http,sqlite] @ {artifacts['wheel'].as_uri()}")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {"PATH": os.environ["PATH"], "CAWBASE_CONTENT_ENGINE_DATABASE_URL": f"sqlite+aiosqlite:///{cwd}/svc.db"}
    proc = subprocess.Popen(
        [str(env_dir / "cawbase-content-engine"), "serve", "--port", str(port)],
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise AssertionError("not ready")
    finally:
        proc.terminate()
        proc.wait(15)


def test_python_example_from_installed_wheel_runs_on_sqlite_and_postgres(artifacts, tmp_path, pg_url):  # AC-07
    """The documented host example, from the INSTALLED wheel, outside the source tree, without HTTP."""
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    env = make_env(
        tmp_path, "example", f"cawbase-content-engine[postgres,sqlite,migrations] @ {artifacts['wheel'].as_uri()}"
    )
    assert not ({"fastapi", "uvicorn", "starlette"} & installed(env, cwd))  # no HTTP stack at all
    example = cwd / "python_host.py"
    shutil.copy(ROOT / "examples" / "python_host.py", example)
    for target in (str(cwd / "example.db"), pg_url):
        out = py(env, str(example), target, cwd=cwd).stdout
        assert "migrated: ('0001', '0002', '0003')" in out and "isolation: project-a cannot read project-b" in out
        assert "project-c" not in out.split("after host rollback:")[1] and "cce1." not in out
    # A02: the profiles example also runs from the installed wheel (SQLite file), without HTTP
    prof_example = cwd / "profiles_host.py"
    shutil.copy(ROOT / "examples" / "profiles_host.py", prof_example)
    out = py(env, str(prof_example), str(cwd / "profiles.db"), cwd=cwd).stdout
    assert "published: version 1" in out and "resolved: default -> version 1" in out and "cce1." not in out
    where = py(env, "-c", "import cawbase_content_engine as c; print(c.__file__)", cwd=cwd).stdout
    assert "site-packages" in where and str(ROOT) not in where
    # PostgreSQL really received the engine namespace, not just the SQLite file
    import asyncio

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    async def tables() -> list[str]:
        eng = create_async_engine(pg_url)
        async with eng.connect() as c:
            rows = (
                (
                    await c.execute(
                        sa.text(
                            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'cawbase_content_engine'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        await eng.dispose()
        return sorted(rows)

    assert "scopes" in asyncio.run(tables())


def test_installed_import_cost_meets_the_decided_references(artifacts, tmp_path):  # AC-04, D-33
    """Gate decided in D-33: WARM median <= 300 ms and installed size <= 40 MiB. The COLD first import is
    measured and reported but has no threshold. Samples are logged in the failure message, never relaxed."""
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    env = make_env(tmp_path, "measure", str(artifacts["wheel"]))
    script = cwd / "measure_import.py"
    shutil.copy(ROOT / "scripts" / "measure_import.py", script)
    data = json.loads(py(env, str(script), "--cold-runs", "3", "--warm-runs", "15", cwd=cwd).stdout)
    warm = data["warm_import (new processes, bytecode available)"]
    cold = data["cold_first_import (bytecode cleared before each sample)"]
    assert warm["samples"] == 15 and cold["samples"] == 3
    assert warm["median_ms"] <= 300, data
    assert data["installed_size_kib_excluding_pycache"] <= 40 * 1024, data
    forbidden = {"fastapi", "starlette", "uvicorn", "alembic", "asyncpg", "aiosqlite", "httpx"}
    assert not forbidden & set(data["third_party_top_level_modules_loaded_on_import"])
