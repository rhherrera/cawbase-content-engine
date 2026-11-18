"""A08 lifecycle, failures and access rules on PostgreSQL and SQLite (real storage, child, cryptography)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from cawbase_content_engine.errors import (
    ContextExpiringError,
    ContextKindMismatchError,
    ContextUnavailableError,
    DerivativeBindingUnavailableError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ScopeDisabledError,
)
from cawbase_content_engine.pdf_protection.types import ExistingContext, NewContext

from .a05_helpers import fast_limits
from .a08_helpers import FULL, POLICY, SP, World, analyse, mock_for, ops, page_texts, read_all, run, setup_scope, upload
from .helpers import admin_ctx
from .test_a08_access import MAIL, NAME, SYN, syn_findings


@pytest.fixture
async def world(migrated, tmp_path):
    _, url = migrated
    w = World(url=url, tmp=tmp_path)
    yield w
    await w.close()


async def exec_sql(eng: Any, stmt: Any) -> Any:
    async with eng._engine.begin() as c:
        res = await c.execute(stmt)
        return list(res.all()) if res.returns_rows else None


async def protected(world: World, *, context: Any = None, key: str = "f1", limits: Any = None) -> dict[str, Any]:
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, syn_findings(SYN)), limits=limits)
    fid, aid = await analyse(eng, SYN, key=key)
    o = ops(eng, FULL)
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=context or NewContext(), idempotency_key="p-" + key
    )
    out = await run(eng, acc.job.job_id)
    return {"eng": eng, "o": o, "fid": fid, "aid": aid, "acc": acc, "out": out, "base": base}


async def restore(s: dict[str, Any], key: str = "r") -> Any:
    o, acc = s["o"], s["acc"]
    res = await o.get_pdf_protection_result("a", acc.job.job_id)
    derivative = await read_all(await o.open_pdf_protected("a", acc.job.job_id))
    cid = await upload(o, derivative, "copy-" + key)
    racc = await o.submit_pdf_restoration(
        "a",
        derivative_id=res.derivative_id,
        file_id=cid,
        context_id=acc.context_id,
        kind=acc.context_kind,
        idempotency_key=key,
    )
    return racc, await run(s["eng"], racc.job.job_id), res


async def code(s: dict[str, Any], job_id: str) -> str | None:
    return (await s["o"].get_job("a", job_id)).failure_code


async def test_restore_after_purging_original_analysis_and_protection_job_data(world):  # AC-A08-37, 40
    s = await protected(world)
    eng, o, acc = s["eng"], s["o"], s["acc"]
    derivative = await read_all(await o.open_pdf_protected("a", acc.job.job_id))
    res = await o.get_pdf_protection_result("a", acc.job.job_id)
    copy_id = await upload(o, derivative, "copy-early")
    await o.delete_file("a", s["fid"])
    await o.delete_job_data("a", s["aid"])
    await o.delete_job_data("a", acc.job.job_id)  # delete_job_data keeps the binding (D-A08-06)
    await eng.maintenance().run_cleanup()
    assert (await o.get_pdf_derivative("a", res.derivative_id)).state == "active"
    racc = await o.submit_pdf_restoration(
        "a",
        derivative_id=res.derivative_id,
        file_id=copy_id,
        context_id=acc.context_id,
        kind="job",
        idempotency_key="after",
    )
    assert (await run(eng, racc.job.job_id)).state == "succeeded"
    restored = await read_all(await o.open_pdf_restored("a", racc.job.job_id))
    assert NAME in page_texts(restored)[0] and MAIL in page_texts(restored)[0]
    jobs = eng.tables.jobs
    await exec_sql(eng, sa.delete(jobs).where(jobs.c.job_id == acc.job.job_id))  # even its metadata
    racc2 = await o.submit_pdf_restoration(
        "a",
        derivative_id=res.derivative_id,
        file_id=copy_id,
        context_id=acc.context_id,
        kind="job",
        idempotency_key="after2",
    )
    assert (await run(eng, racc2.job.job_id)).state == "succeeded"


async def test_deleting_the_context_ends_restorability_and_cleanup_ends_bindings(world):  # AC-A08-40
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, syn_findings(SYN)))
    o = ops(eng, FULL)
    ctx = await o.create_context("a", kind="job")
    derived = []
    for key in ("one", "two"):
        _f, aid = await analyse(eng, SYN, key=key)
        acc = await o.submit_pdf_protection(
            "a", analysis_job_id=aid, policy=POLICY, context=ExistingContext(ctx.context_id, "job"), idempotency_key=key
        )
        assert (await run(eng, acc.job.job_id)).state == "succeeded"
        res = await o.get_pdf_protection_result("a", acc.job.job_id)
        copy = await upload(o, await read_all(await o.open_pdf_protected("a", acc.job.job_id)), "c-" + key)
        derived.append((res.derivative_id, copy))
    accepted = await o.submit_pdf_restoration(
        "a",
        derivative_id=derived[0][0],
        file_id=derived[0][1],
        context_id=ctx.context_id,
        kind="job",
        idempotency_key="before",
    )
    await o.delete_context("a", ctx.context_id)
    await run(eng, accepted.job.job_id)
    assert (await o.get_job("a", accepted.job.job_id)).failure_code == "context_unavailable"
    for did, copy in derived:
        with pytest.raises(ContextUnavailableError):
            await o.submit_pdf_restoration(
                "a",
                derivative_id=did,
                file_id=copy,
                context_id=ctx.context_id,
                kind="job",
                idempotency_key="x" + did[-4:],
            )
    summary = await eng.maintenance().run_pdf_cleanup()
    assert summary.bindings_ended == 2
    d = eng.tables.pdf_derivatives
    for row in await exec_sql(eng, sa.select(d)):
        assert row.state == "ended" and row.occurrences is None and row.layout is None and row.binding_mac is None
    for did, copy in derived:
        with pytest.raises(DerivativeBindingUnavailableError):
            await o.submit_pdf_restoration(
                "a",
                derivative_id=did,
                file_id=copy,
                context_id=ctx.context_id,
                kind="job",
                idempotency_key="y" + did[-4:],
            )
    assert not hasattr(o, "revoke_pdf_derivative")  # no per-derivative revocation


async def test_context_rules_on_submission(world):  # AC-A08-38
    s = await protected(world)
    o, acc = s["o"], s["acc"]
    res = await o.get_pdf_protection_result("a", acc.job.job_id)
    copy = await upload(o, await read_all(await o.open_pdf_protected("a", acc.job.job_id)), "c")
    with pytest.raises(ContextKindMismatchError):
        await o.submit_pdf_restoration(
            "a",
            derivative_id=res.derivative_id,
            file_id=copy,
            context_id=acc.context_id,
            kind="conversation",
            idempotency_key="k",
        )
    other = await o.create_context("a", kind="job")
    with pytest.raises(ResourceNotFoundError):
        await o.submit_pdf_restoration(
            "a",
            derivative_id=res.derivative_id,
            file_id=copy,
            context_id=other.context_id,
            kind="job",
            idempotency_key="k2",
        )
    short = await o.create_context("a", kind="job", ttl_s=60)
    _f, aid = await analyse(s["eng"], SYN, key="short")
    with pytest.raises(ContextExpiringError):
        await o.submit_pdf_protection(
            "a",
            analysis_job_id=aid,
            policy=POLICY,
            context=ExistingContext(short.context_id, "job"),
            idempotency_key="short",
        )


async def test_reading_the_restored_pdf_needs_context_and_active_scope(world):  # AC-A08-40
    s = await protected(world)
    racc, out, _res = await restore(s)
    assert out.state == "succeeded"
    o, eng = s["o"], s["eng"]
    assert await read_all(await o.open_pdf_restored("a", racc.job.job_id))
    with pytest.raises(PermissionDeniedError):
        await ops(eng, [SP.PDF_RESTORE, SP.RESULTS_READ]).open_pdf_restored("a", racc.job.job_id)
    admin = eng.admin(await admin_ctx(eng))
    await admin.set_scope_status("a", "disabled")
    for reader in (o.open_pdf_restored, o.get_pdf_restoration_result):
        with pytest.raises(ScopeDisabledError):
            await reader("a", racc.job.job_id)
    assert await read_all(await o.open_pdf_protected("a", s["acc"].job.job_id))  # A05 reads: allowed
    await admin.set_scope_status("a", "active")
    await o.delete_context("a", s["acc"].context_id)
    for reader in (o.open_pdf_restored, o.get_pdf_restoration_result):
        with pytest.raises(ContextUnavailableError):
            await reader("a", racc.job.job_id)


async def test_authorization_instant_of_the_restored_download(world, monkeypatch):  # AC-A08-40
    s = await protected(world)
    racc, out, _res = await restore(s)
    o = s["o"]
    from cawbase_content_engine.pdf_protection import ops as pops

    original_spool = pops.spool_artifact
    calls = {"n": 0}

    async def slow_spool(ctx, scope_id, job_id, art):  # the context is deleted WHILE storage is read
        stream = await original_spool(ctx, scope_id, job_id, art)
        if calls["n"] == 0:
            calls["n"] += 1
            await o.delete_context("a", s["acc"].context_id)
        return stream

    monkeypatch.setattr(pops, "spool_artifact", slow_spool)
    with pytest.raises(ContextUnavailableError):
        await o.open_pdf_restored("a", racc.job.job_id)
    monkeypatch.setattr(pops, "spool_artifact", original_spool)
    s2 = await protected(world, key="second")
    racc2, _o2, _ = await restore(s2, key="r2")
    stream = await s2["o"].open_pdf_restored("a", racc2.job.job_id)
    await s2["o"].delete_context("a", s2["acc"].context_id)  # handed out before: served from the verified spool
    assert NAME in page_texts(await read_all(stream))[0]


async def test_context_deleted_before_publication_and_immediate_purge(world, monkeypatch):  # AC-A08-38, 40
    s = await protected(world)
    o, eng = s["o"], s["eng"]
    from cawbase_content_engine.pdf_protection import execute as pex

    real_child = pex._child

    async def child(w, data, mode, a08=None):
        out = await real_child(w, data, mode, a08)
        if mode == "pdf_verify" and a08 and a08.get("mode") == "restore":
            await o.delete_context("a", s["acc"].context_id)  # after decrypting, before publishing
        return out

    monkeypatch.setattr(pex, "_child", child)
    racc, out, _res = await restore(s)
    info = await o.get_job("a", racc.job.job_id)
    assert info.state == "failed" and info.failure_code == "context_unavailable"
    arts = eng.tables.job_artifacts
    rows = await exec_sql(eng, sa.select(arts).where(arts.c.job_id == racc.job.job_id))
    restored = [x for x in rows if x.kind == "pdf_restored"]
    assert restored and all(x.state == "discarded" and x.delete_after <= x.abandoned_at for x in restored)
    assert all(x.delete_after > x.abandoned_at for x in rows if x.kind != "pdf_restored")
    await eng.maintenance().run_cleanup()
    rows = await exec_sql(eng, sa.select(arts).where(arts.c.job_id == racc.job.job_id, arts.c.kind == "pdf_restored"))
    assert all(x.physically_deleted_at is not None for x in rows)


async def test_purge_of_a_restored_pdf_discarded_by_an_older_runtime(world):  # D-A08-06
    s = await protected(world)
    racc, out, _res = await restore(s)
    eng = s["eng"]
    arts = eng.tables.job_artifacts
    row = (await exec_sql(eng, sa.select(arts).where(arts.c.job_id == racc.job.job_id, arts.c.kind == "pdf_restored")))[
        0
    ]
    await exec_sql(
        eng,
        sa.update(arts)
        .where(arts.c.artifact_id == row.artifact_id)
        .values(
            state="discarded", abandoned_at=datetime.now(UTC), delete_after=datetime.now(UTC) + timedelta(hours=24)
        ),
    )
    await eng.maintenance().run_cleanup()
    assert (await exec_sql(eng, sa.select(arts).where(arts.c.artifact_id == row.artifact_id)))[0].physically_deleted_at


async def _due_now(eng: Any, job_id: str) -> None:
    jobs = eng.tables.jobs
    await exec_sql(
        eng,
        sa.update(jobs).where(jobs.c.job_id == job_id).values(available_at=sa.func.now())
        if eng.backend.value == "postgresql"
        else sa.update(jobs)
        .where(jobs.c.job_id == job_id)
        .values(available_at=sa.text("strftime('%Y-%m-%d %H:%M:%f','now','-1 second')")),
    )


async def test_retry_reuses_markers_and_publishes_once(world, monkeypatch):  # AC-A08-38
    from cawbase_content_engine.jobs.worker import JobFailure
    from cawbase_content_engine.pdf_protection import execute as pex

    real_child = pex._child
    state = {"failed": False}

    async def flaky(w, data, mode, a08=None):
        if mode == "pdf_verify" and not state["failed"]:
            state["failed"] = True
            raise JobFailure("extraction_process_failed")  # retryable, after the mappings were created
        return await real_child(w, data, mode, a08)

    monkeypatch.setattr(pex, "_child", flaky)
    base = await setup_scope(world)
    eng = await world.engine(
        await mock_for(base, syn_findings(SYN)), limits=fast_limits(lease_ttl_s=30.0, heartbeat_interval_s=1.0)
    )
    _f, aid = await analyse(eng, SYN)
    o = ops(eng, FULL)
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=NewContext(), idempotency_key="rt"
    )
    await run(eng, acc.job.job_id)
    assert (await o.get_job("a", acc.job.job_id)).state == "retry_wait"
    m = eng.tables.protection_mappings
    before = len(await exec_sql(eng, sa.select(m.c.token)))
    await _due_now(eng, acc.job.job_id)
    assert (await run(eng, acc.job.job_id)).state == "succeeded"
    res = await o.get_pdf_protection_result("a", acc.job.job_id)
    assert res.counts["reused_markers"] == before and res.counts["new_markers"] == 0
    arts = eng.tables.job_artifacts
    rows = await exec_sql(eng, sa.select(arts).where(arts.c.job_id == acc.job.job_id))
    assert sum(1 for x in rows if x.state == "published") == 2
    d = eng.tables.pdf_derivatives
    assert len(await exec_sql(eng, sa.select(d).where(d.c.protection_job_id == acc.job.job_id))) == 1


async def test_storage_failure_is_retryable_without_double_publication(world, monkeypatch):  # AC-A08-38
    from cawbase_content_engine.jobs.setup import StorageError
    from cawbase_content_engine.jobs.storage import FilesystemStorage

    base = await setup_scope(world)
    eng = await world.engine(
        await mock_for(base, syn_findings(SYN)), limits=fast_limits(lease_ttl_s=30.0, heartbeat_interval_s=1.0)
    )
    _f, aid = await analyse(eng, SYN)
    o = ops(eng, FULL)
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=NewContext(), idempotency_key="st"
    )
    real_put = FilesystemStorage.put_new
    state = {"n": 0}

    async def put_new(self, key, chunks, *, max_bytes, abort_after):
        if state["n"] == 0:
            state["n"] += 1
            raise StorageError
        return await real_put(self, key, chunks, max_bytes=max_bytes, abort_after=abort_after)

    monkeypatch.setattr(FilesystemStorage, "put_new", put_new)
    await run(eng, acc.job.job_id)
    info = await o.get_job("a", acc.job.job_id)
    assert info.state == "retry_wait" and info.last_error_code == "storage_unavailable"
    d = eng.tables.pdf_derivatives
    assert not await exec_sql(eng, sa.select(d))
    await _due_now(eng, acc.job.job_id)
    assert (await run(eng, acc.job.job_id)).state == "succeeded"
    assert len(await exec_sql(eng, sa.select(d))) == 1


async def test_lease_lost_before_publication_publishes_nothing(world, monkeypatch):  # AC-A08-38
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, syn_findings(SYN)))
    _f, aid = await analyse(eng, SYN)
    o = ops(eng, FULL)
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=NewContext(), idempotency_key="ll"
    )
    from cawbase_content_engine.pdf_protection import execute as pex

    real_child = pex._child
    jobs = eng.tables.jobs

    async def steal(w, data, mode, a08=None):
        out = await real_child(w, data, mode, a08)
        if mode == "pdf_verify":  # another worker took the job over (its lease expired and was recovered)
            await exec_sql(
                eng, sa.update(jobs).where(jobs.c.job_id == acc.job.job_id).values(lease_token="someone-else")
            )
        return out

    monkeypatch.setattr(pex, "_child", steal)
    await run(eng, acc.job.job_id)
    d, arts = eng.tables.pdf_derivatives, eng.tables.job_artifacts
    assert not await exec_sql(eng, sa.select(d))
    rows = await exec_sql(eng, sa.select(arts).where(arts.c.job_id == acc.job.job_id))
    assert all(x.state != "published" for x in rows)


async def test_key_absent_and_tampered_rows_fail_closed(world):  # AC-A08-38 (authenticated binding)
    s = await protected(world)
    eng = s["eng"]
    d, m = eng.tables.pdf_derivatives, eng.tables.protection_mappings
    row = (await exec_sql(eng, sa.select(d)))[0]
    import json

    layout = json.loads(row.layout) if isinstance(row.layout, str) else row.layout
    tampered = json.loads(json.dumps(layout))
    tampered[0]["sels"][0]["x_orig"] += 3.0  # geometry altered in the database
    await exec_sql(eng, sa.update(d).where(d.c.derivative_id == row.derivative_id).values(layout=tampered))
    racc, out, _ = await restore(s, key="tamper")
    assert await code(s, racc.job.job_id) == "binding_integrity_failed"
    await exec_sql(eng, sa.update(d).where(d.c.derivative_id == row.derivative_id).values(layout=layout))
    occ = json.loads(row.occurrences) if isinstance(row.occurrences, str) else row.occurrences
    moved = [{**occ[0], "start": occ[0]["start"] + 1}, *occ[1:]]
    await exec_sql(eng, sa.update(d).where(d.c.derivative_id == row.derivative_id).values(occurrences=moved))
    racc, out, _ = await restore(s, key="tamper2")
    assert await code(s, racc.job.job_id) in ("binding_integrity_failed", "derivative_markers_invalid")
    await exec_sql(eng, sa.update(d).where(d.c.derivative_id == row.derivative_id).values(occurrences=occ))
    mrow = (await exec_sql(eng, sa.select(m)))[0]
    await exec_sql(
        eng, sa.update(m).where(m.c.token == mrow.token).values(ciphertext=bytes(mrow.ciphertext)[:-1] + b"\x00")
    )
    racc2, _o2, _ = await restore(s, key="cipher")
    assert await code(s, racc2.job.job_id) == "mapping_integrity_failed"
    await exec_sql(eng, sa.update(m).where(m.c.token == mrow.token).values(ciphertext=mrow.ciphertext))
    world.keys.ring.pop("v1")
    racc3, _o3, _ = await restore(s, key="nokey")
    assert await code(s, racc3.job.job_id) == "protection_key_unavailable"


async def test_no_values_in_logs_errors_or_reports(world, caplog):  # AC-A08-42
    caplog.set_level(logging.DEBUG, logger="cawbase_content_engine")
    s = await protected(world)
    racc, _out, res = await restore(s)
    text = "\n".join(
        r.getMessage() + str(getattr(r, "__dict__", {}))
        for r in caplog.records
        if r.name.startswith("cawbase_content_engine")
    )
    assert NAME not in text and MAIL not in text and "[[CCE1:" not in text and "00000000-X" not in text
    rres = await s["o"].get_pdf_restoration_result("a", racc.job.job_id)
    assert NAME not in str(rres) and "00000000" not in str(res)


async def test_concurrent_protection_and_context_deletion(world):  # AC-A08-38 (race, both orders)
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, syn_findings(SYN)))
    o = ops(eng, FULL)
    _f, aid = await analyse(eng, SYN)
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=NewContext(), idempotency_key="race"
    )
    job = asyncio.ensure_future(run(eng, acc.job.job_id))
    await asyncio.sleep(0.05)
    await o.delete_context("a", acc.context_id)
    await job
    info = await o.get_job("a", acc.job.job_id)
    d = eng.tables.pdf_derivatives
    bindings = await exec_sql(eng, sa.select(d).where(d.c.protection_job_id == acc.job.job_id))
    if info.state == "succeeded":
        assert len(bindings) == 1
    else:
        assert info.failure_code == "context_unavailable" and not bindings
    m = eng.tables.protection_mappings
    assert not await exec_sql(eng, sa.select(m).where(m.c.context_id == acc.context_id))


async def test_context_expiry_while_publication_waits_for_its_lock(world, monkeypatch, db):  # AC-A08-38 (PostgreSQL)
    if db[0] != "postgresql":
        pytest.skip("row-lock waits are observed with PostgreSQL's pg_stat_activity; SQLite serializes writers")
    from .a06_helpers import wait_for_lock_wait

    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, syn_findings(SYN)))
    o = ops(eng, FULL)
    _f, aid = await analyse(eng, SYN)
    ctxr = await o.create_context("a", kind="job")
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=ExistingContext(ctxr.context_id, "job"), idempotency_key="lw"
    )
    from cawbase_content_engine.pdf_protection import execute as pex

    t = eng.tables.protection_contexts
    held: dict[str, Any] = {}
    real_child = pex._child

    async def child(w, data, mode, a08=None):
        out = await real_child(w, data, mode, a08)
        if mode == "pdf_verify":  # just before publishing: a host transaction holds the context row
            holder = await eng._engine.connect()
            await holder.begin()
            soon = (await holder.execute(sa.text("SELECT clock_timestamp() + interval '1 second'"))).scalar_one()
            await holder.execute(sa.update(t).where(t.c.context_id == ctxr.context_id).values(expires_at=soon))
            held.update(conn=holder, soon=soon)
        return out

    monkeypatch.setattr(pex, "_child", child)
    job = asyncio.ensure_future(run(eng, acc.job.job_id))
    for _ in range(1000):  # until the publication step has taken the context row (bounded)
        if "conn" in held:
            break
        await asyncio.sleep(0.02)
    await wait_for_lock_wait(eng)  # the publication really waits for the row
    holder = held["conn"]
    for _ in range(400):  # the database clock passes the new expiry while the publication waits
        if (await holder.execute(sa.text("SELECT clock_timestamp() > :s"), {"s": held["soon"]})).scalar_one():
            break
        await asyncio.sleep(0.02)
    await holder.commit()
    await holder.close()
    await job
    info = await o.get_job("a", acc.job.job_id)
    assert info.state == "failed" and info.failure_code == "context_unavailable"
    d = eng.tables.pdf_derivatives
    assert not await exec_sql(eng, sa.select(d))
