"""Migration 0004, compatibility and public surface of A06. AC-A06-50, 51, 54."""

from __future__ import annotations

import inspect

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from cawbase_content_engine import ContentEngine, EngineConfig, ScopePermission
from cawbase_content_engine.errors import SchemaIncompatibleError
from cawbase_content_engine.migrations import upgrade
from cawbase_content_engine.persistence.tables import tables_for

from .a05_helpers import TEXT, make_engine, ready_scope, run_all
from .helpers import issue
from .test_migrations import inventory

A06_TABLES = {"protection_contexts", "protection_mappings", "protection_scope_usage"}


async def dump_all(url: str, backend: str, tables) -> dict[str, list[str]]:
    """Every row of every pre-existing engine table, as a sorted list of reprs (content and counts)."""
    from cawbase_content_engine.persistence.dialect import Backend

    eng = create_async_engine(url)
    out: dict[str, list[str]] = {}
    try:
        async with eng.connect() as c:
            for table in tables.metadata.sorted_tables:
                name = table.name
                if name.removeprefix("cawbase_content_engine_") in A06_TABLES:
                    continue
                full = f'"cawbase_content_engine"."{name}"' if backend == "postgresql" else name
                rows = (await c.execute(sa.text(f"SELECT * FROM {full}"))).all()  # noqa: S608 - constant names
                out[name] = sorted(repr(tuple(r)) for r in rows)
    finally:
        await eng.dispose()
    _ = Backend
    return out


async def test_upgrade_from_0003_keeps_every_a01_to_a05_row_and_object(db, tmp_path, monkeypatch):  # AC-A06-50
    backend, url = db
    cfg = EngineConfig(database_url=url)
    r3 = await upgrade(cfg, target="0003")
    assert r3.applied == ("0001", "0002", "0003") and r3.to_level == 3
    monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 3)  # a 0.4.0 runtime on its own schema
    eng = await make_engine(url, tmp_path)
    try:
        ops = await ready_scope(eng, "a")
        _, key = await issue(eng, "svc", scopes={"a": ["scope:read", "profiles:read"]})
        acc = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k1")
        await run_all(eng)
        assert (await ops.get_job("a", acc.job.job_id)).state == "succeeded"
        up = await ops.upload_file("a", _one(b"hello"), idempotency_key="f1")
        assert up.value.state == "stored"
    finally:
        await eng.aclose()
    monkeypatch.undo()
    tables = tables_for(eng.backend)
    before_rows = await dump_all(url, backend, tables)
    before_objects = await inventory(url)
    assert sum(len(v) for v in before_rows.values()) > 20  # a real, populated installation
    report = await upgrade(cfg)
    assert report.applied == ("0004",) and report.from_revision == "0003" and report.to_level == 4
    after_objects = await inventory(url)
    for name, entry in before_objects.items():  # nothing that existed was altered or removed
        if name.endswith("engine_state"):
            continue  # the only existing row that changes is the level; its DDL is checked below
        assert after_objects[name] == entry, name
    assert (
        before_objects[next(n for n in before_objects if n.endswith("engine_state"))]
        == after_objects[next(n for n in after_objects if n.endswith("engine_state"))]
    )
    created = {n for n in set(after_objects) - set(before_objects) if "autoindex" not in n}
    assert created and all(any(t in n for t in A06_TABLES) for n in created), created
    after_rows = await dump_all(url, backend, tables)
    for name, rows in before_rows.items():
        if name.endswith("engine_state"):
            continue
        assert after_rows[name] == rows, f"data changed in {name}"
    state_before = before_rows[next(n for n in before_rows if n.endswith("engine_state"))][0]
    state_after = after_rows[next(n for n in after_rows if n.endswith("engine_state"))][0]
    assert state_before != state_after and "4" in state_after  # only the level (and its bookkeeping) moved
    again = await upgrade(cfg)
    assert again.already_current and again.applied == ()
    assert await dump_all(url, backend, tables) == after_rows  # re-running changes nothing
    async with ContentEngine(cfg) as e2:
        st = await e2.schema_status()
        assert st.compatible and st.schema_level == 4 and st.min_runtime_level == 1
        assert (await e2.authenticate_api_key(key.secret)).scope_grants["a"]  # old key, old grants, still work
        counts = {}
        async with e2._engine.connect() as c:  # type: ignore[union-attr]
            for n in A06_TABLES:
                counts[n] = (
                    await c.execute(sa.select(sa.func.count()).select_from(getattr(e2.tables, n)))
                ).scalar_one()
        assert counts == {n: 0 for n in A06_TABLES}  # no context or mapping is created for existing scopes


async def _one(data: bytes):
    yield data


async def test_older_runtime_keeps_working_on_level_4_and_a_new_one_refuses_level_3(db, monkeypatch):  # AC-A06-51
    backend, url = db
    cfg = EngineConfig(database_url=url)
    await upgrade(cfg, target="0003")
    with pytest.raises(SchemaIncompatibleError):  # 0.5.0 on a level-3 schema: outdated, refuses to start
        await ContentEngine(cfg).start()
    st = await _status(cfg)
    assert not st.compatible and st.reason == "schema_outdated" and st.schema_level == 3
    await upgrade(cfg)
    monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 3)  # what a 0.4.0 runtime requires
    async with ContentEngine(cfg) as old:  # it accepts level 4 (min_runtime_level stays 1) and serves A01-A05 traffic
        assert (await old.readiness()).ready
        from .helpers import make_scope

        await make_scope(old, "legacy")
        ops = old.scoped(old.trusted_scope_context("legacy", [ScopePermission.SCOPE_READ], actor="t"))
        assert (await ops.get_scope("legacy")).scope_id == "legacy"
        assert old.protection is None
    monkeypatch.undo()
    async with ContentEngine(cfg) as new:  # without ProtectionSetup A06 is simply not available; nothing else changes
        assert (await new.readiness()).ready and new.protection is None


async def _status(cfg):
    from cawbase_content_engine import check_schema

    return await check_schema(cfg)


async def test_a06_adds_no_http_route_and_no_queue_operation(migrated):  # AC-A06-54
    from cawbase_content_engine.http import create_router
    from cawbase_content_engine.jobs.ops import OPERATIONS

    engine = ContentEngine(EngineConfig(database_url=migrated[1]))
    routes = {(m, r.path) for r in create_router(engine).routes for m in r.methods}
    assert len(routes) == 61  # exactly the A05 route table: nothing was added
    assert not [p for _, p in routes if any(w in p for w in ("protect", "restore", "context", "marker", "mapping"))]
    assert OPERATIONS == ("document_analysis", "text_analysis")
    from cawbase_content_engine.operations.analysis import AnalysisOperations

    assert list(inspect.signature(AnalysisOperations.analyze_text).parameters) == [
        "self",
        "scope_id",
        "text",
        "profile_id",
        "version_number",
        "capabilities",
        "context",
        "request_id",
    ]
