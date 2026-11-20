"""Migration 0005, compatibility with older runtimes and public surface of A07. AC-A07-73, 74, 55, 77."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from cawbase_content_engine import ContentEngine, EngineConfig
from cawbase_content_engine.errors import ResourceNotFoundError, SchemaIncompatibleError
from cawbase_content_engine.migrations import upgrade
from cawbase_content_engine.persistence.tables import tables_for

from .a05_helpers import TEXT, make_engine, ready_scope, run_all
from .a07_helpers import World
from .test_a06_migration import dump_all as _dump_all
from .test_migrations import inventory


async def dump_all(url: str, backend: str, tables) -> dict[str, list[str]]:
    """The A06 dump over the tables that existed before 0005."""
    md = sa.MetaData()
    for tbl in tables.metadata.sorted_tables:
        if not tbl.name.endswith(("docx_derivatives", "pdf_derivatives")):
            tbl.to_metadata(md)

    class _T:
        metadata = md

    return await _dump_all(url, backend, _T)


async def test_upgrade_from_0004_keeps_every_row_and_object(db, tmp_path, monkeypatch):  # AC-A07-73
    backend, url = db
    cfg = EngineConfig(database_url=url)
    r4 = await upgrade(cfg, target="0004")
    assert r4.to_level == 4
    monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 4)  # a 0.5.0 runtime on its own schema
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
    assert report.applied == ("0005", "0006") and report.from_revision == "0004" and report.to_level == 6
    after_objects = await inventory(url)
    for name, entry in before_objects.items():
        assert after_objects[name] == entry, name
    created = {n for n in set(after_objects) - set(before_objects) if "autoindex" not in n}
    assert created and all("docx_derivatives" in n or "pdf_derivatives" in n for n in created), created
    after_rows = await dump_all(url, backend, tables)
    for name, rows in before_rows.items():
        if name.endswith("engine_state") or name.endswith(("docx_derivatives", "pdf_derivatives")):
            continue
        assert after_rows[name] == rows, f"data changed in {name}"
    assert (await upgrade(cfg)).already_current
    async with ContentEngine(cfg) as e2:
        st = await e2.schema_status()
        assert st.compatible and st.schema_level == 6 and st.min_runtime_level == 1


async def test_older_runtimes_and_a_new_one_on_an_old_schema(db, tmp_path, monkeypatch):  # AC-A07-74, 55
    _backend, url = db
    cfg = EngineConfig(database_url=url)
    await upgrade(cfg, target="0004")
    with pytest.raises(SchemaIncompatibleError):  # 0.7.0 on level 4: outdated
        await ContentEngine(cfg).start()
    await upgrade(cfg)
    monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 4)  # what a 0.5.0 runtime requires
    async with ContentEngine(cfg) as old:
        assert (await old.readiness()).ready
    monkeypatch.undo()
    # an A07 job, then what the OLDER worker and the OLDER generic readers do with it
    from .test_a07_jobs import DOC, protect_world

    w = World(url=url, tmp=tmp_path)
    try:
        eng, o, _fid, _aid, acc, out = await protect_world(w, DOC.build())
        assert out.state == "succeeded"
        no_protection = await w.engine(protection=False)
        keys = no_protection.job_worker("old").supported_keys()
        jobs = eng.tables.jobs
        async with eng._engine.connect() as c:
            key = (
                await c.execute(sa.select(jobs.c.requirements_key).where(jobs.c.job_id == acc.job.job_id))
            ).scalar_one()
        assert key not in keys  # a runtime without A07 never claims A07 jobs (H-A07-02)
        from cawbase_content_engine.jobs import files as fl
        from cawbase_content_engine.jobs.ops import _published_artifacts

        arts = await _published_artifacts(fl.Ctx(eng), "a", acc.job.job_id, generic=False)  # 0.5.0 logic: by kind
        assert "result" not in arts and "source_map" not in arts  # H-A07-03: nothing generic to serve
        with pytest.raises(ResourceNotFoundError):
            await o.get_job_result("a", acc.job.job_id)
    finally:
        await w.close()


def test_public_surface_has_no_new_http_routes():  # AC-A07-77
    import inspect

    import cawbase_content_engine.http.jobs_routes as jr
    import cawbase_content_engine.http.router as rt

    for src in (inspect.getsource(jr), inspect.getsource(rt)):
        assert "docx_protect" not in src and "docx_restor" not in src and "/docx" not in src
