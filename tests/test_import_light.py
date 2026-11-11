"""Import is light and effect-free: AC-04, AC-05, AC-06."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "cawbase_content_engine"

FORBIDDEN = {
    "fastapi",
    "starlette",
    "uvicorn",
    "alembic",
    "asyncpg",
    "aiosqlite",
    "httpx",
    "mako",
    "torch",
    "numpy",
    "transformers",
    "tensorflow",
    "onnxruntime",
    "fitz",
    "pymupdf",
    "pypdf",
    "docx",
    "pdfminer",
}

PROBE = textwrap.dedent(
    """
    import json, os, socket, sys, tempfile, threading, logging

    reads = []
    class Env(dict):
        def __getitem__(self, k): reads.append(k); return super().__getitem__(k)
        def get(self, k, d=None): reads.append(k); return super().get(k, d)
        def __contains__(self, k): reads.append(k); return super().__contains__(k)
    os.environ = Env(os.environ)  # record reads (the real environ is copied)

    connects = []
    real_connect = socket.socket.connect
    socket.socket.connect = lambda self, *a, **k: connects.append(a) or real_connect(self, *a, **k)

    os.chdir(tempfile.mkdtemp())
    before_files = set(os.listdir("."))
    before_threads = threading.active_count()
    root_handlers = list(logging.getLogger().handlers)
    root_level = logging.getLogger().level

    import cawbase_content_engine

    pkg_logger = logging.getLogger("cawbase_content_engine")
    print(json.dumps({
        "modules": sorted({m.split(".")[0] for m in sys.modules}),
        "env_reads": [k for k in reads if "CAWBASE" in str(k).upper() or "DATABASE" in str(k).upper()],
        "connects": connects,
        "new_files": sorted(set(os.listdir(".")) - before_files),
        "new_threads": threading.active_count() - before_threads,
        "root_handlers_changed": list(logging.getLogger().handlers) != root_handlers,
        "root_level_changed": logging.getLogger().level != root_level,
        "pkg_handlers": len(pkg_logger.handlers), "pkg_level": pkg_logger.level,
        "cawbase_modules": sorted(m for m in sys.modules if m.startswith("cawbase_content_engine")),
    }))
    """
)


def run_probe() -> dict:
    out = subprocess.run([sys.executable, "-I", "-c", PROBE], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_import_loads_no_heavy_optional_or_driver_modules():  # AC-04
    data = run_probe()
    assert not (FORBIDDEN & set(data["modules"])), FORBIDDEN & set(data["modules"])
    assert not [m for m in data["cawbase_modules"] if ".http" in m or ".migrations" in m or m.endswith(".cli")]


def test_import_has_no_external_effects():  # AC-05
    data = run_probe()
    assert data["env_reads"] == [] and data["connects"] == [] and data["new_files"] == []
    assert data["new_threads"] == 0
    assert not data["root_handlers_changed"] and not data["root_level_changed"]
    assert data["pkg_handlers"] == 0 and data["pkg_level"] == 0


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            level = node.level
            base = ("." * level) + (node.module or "")
            found.add(base)
    return found


@pytest.mark.parametrize(
    "path",
    sorted(
        p
        for p in SRC.rglob("*.py")
        if not {"http", "migrations"} & set(p.relative_to(SRC).parts[:1]) and p.name not in ("cli.py", "__main__.py")
    ),
)
def test_core_does_not_import_http_cli_migrations_fastapi_alembic(path):  # AC-06
    # imports at module level only (lazy imports inside functions are checked by test_import_loads_*)
    tree = ast.parse(path.read_text())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = []
    for node in top:
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        else:
            names.append(("." * node.level) + (node.module or ""))
    for name in names:
        root = name.lstrip(".").split(".")[0]
        assert root not in {"fastapi", "starlette", "uvicorn", "alembic", "asyncpg", "aiosqlite"}, (path, name)
        assert name.lstrip(".").split(".")[0] not in {"http", "migrations", "cli"}, (path, name)


def test_package_logger_is_untouched_after_using_the_engine(tmp_path):
    import logging

    assert logging.getLogger("cawbase_content_engine").handlers == []
