"""Migration 0006 and compatibility with earlier runtimes (AC-A08-32, 43): additive, forward only, every row and
object kept; a 0.6.0 runtime keeps running on schema level 6 (``min_runtime_level`` stays 1) and never claims an
A08 job nor serves its artifacts through the generic readers."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from cawbase_content_engine import ContentEngine, EngineConfig
from cawbase_content_engine.errors import SchemaIncompatibleError
from cawbase_content_engine.migrations import upgrade
from cawbase_content_engine.persistence.tables import tables_for

from .a05_helpers import TEXT, make_engine, ready_scope, run_all
from .test_a06_migration import dump_all as _dump_all
from .test_migrations import inventory


async def dump_all(url: str, backend: str, tables) -> dict[str, list[str]]:
    md = sa.MetaData()
    for tbl in tables.metadata.sorted_tables:
        if not tbl.name.endswith("pdf_derivatives"):
            tbl.to_metadata(md)

    class _T:
        metadata = md

    return await _dump_all(url, backend, _T)


async def test_upgrade_from_0005_keeps_every_row_and_object(db, tmp_path, monkeypatch):
    backend, url = db
    cfg = EngineConfig(database_url=url)
    assert (await upgrade(cfg, target="0005")).to_level == 5
    monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 5)  # a 0.6.0 runtime on its own schema
    eng = await make_engine(url, tmp_path)
    try:
        ops = await ready_scope(eng, "a")
        acc = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k1")
        await run_all(eng)
        assert (await ops.get_job("a", acc.job.job_id)).state == "succeeded"
    finally:
        await eng.aclose()
    monkeypatch.undo()
    tables = tables_for(eng.backend)
    before_rows = await dump_all(url, backend, tables)
    before_objects = await inventory(url)
    report = await upgrade(cfg)
    assert report.applied == ("0006",) and report.from_revision == "0005" and report.to_level == 6
    after_objects = await inventory(url)
    for name, entry in before_objects.items():
        assert after_objects[name] == entry, name
    created = {n for n in set(after_objects) - set(before_objects) if "autoindex" not in n}
    assert created and all("pdf_derivatives" in n for n in created), created
    after_rows = await dump_all(url, backend, tables)
    for name, rows in before_rows.items():
        if name.endswith("engine_state"):
            continue
        assert after_rows[name] == rows, f"data changed in {name}"
    assert (await upgrade(cfg)).already_current
    async with ContentEngine(cfg) as e2:
        st = await e2.schema_status()
        assert st.compatible and st.schema_level == 6 and st.min_runtime_level == 1


async def test_older_runtime_on_level_6_and_new_runtime_on_level_5(db, monkeypatch):
    _backend, url = db
    cfg = EngineConfig(database_url=url)
    await upgrade(cfg, target="0005")
    with pytest.raises(SchemaIncompatibleError):  # 0.7.0 on level 5: outdated
        await ContentEngine(cfg).start()
    await upgrade(cfg)
    monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 5)  # what a 0.6.0 runtime requires
    async with ContentEngine(cfg) as old:
        assert (await old.readiness()).ready


def test_version_level_and_catalog():
    from cawbase_content_engine._version import __version__
    from cawbase_content_engine.constants import RUNTIME_LEVEL, SCHEMA_LEVEL
    from cawbase_content_engine.permissions import ALL_ADMIN_PERMISSIONS, ALL_SCOPE_PERMISSIONS

    assert (__version__, SCHEMA_LEVEL, RUNTIME_LEVEL) == ("0.7.0", 6, 1)
    assert len(ALL_SCOPE_PERMISSIONS) + len(ALL_ADMIN_PERMISSIONS) == 29
    assert {p.value for p in ALL_SCOPE_PERMISSIONS} >= {
        "pdf:protect",
        "pdf:restore",
        "pdf:protected:read",
        "pdf:restored:read",
    }
