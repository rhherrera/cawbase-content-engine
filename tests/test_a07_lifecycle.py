"""A07 lifecycle, failures and access rules on PostgreSQL and SQLite (real storage, child, cryptography)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

import pytest
import sqlalchemy as sa

from cawbase_content_engine.docx_protection.types import ExistingContext, NewContext
from cawbase_content_engine.errors import (
    ContextExpiringError,
    ContextKindMismatchError,
    ContextUnavailableError,
    DerivativeBindingUnavailableError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ScopeDisabledError,
)

from .a05_helpers import fast_limits
from .a07_fixtures import MAIL, NAME, Doc, p, r
from .a07_helpers import (
    CAPS,
    PSEUDO,
    SP,
    ModelLikeAdapter,
    World,
    analyse,
    detection,
    mock_for,
    ops,
    read_all,
    run,
    setup_scope,
    unit_texts,
    upload,
)
from .helpers import admin_ctx


@pytest.fixture
async def world(migrated, tmp_path):
    _, url = migrated
    w = World(url=url, tmp=tmp_path)
    yield w
    await w.close()


DOC = Doc(body=[p(r(f"Uno {NAME} y {MAIL}.")), p(r(f"Mencion {NAME}."))], comments=["c"], core={"dc:title": "t"})


def picks(data: bytes) -> dict[str, list[tuple[str, str, int]]]:
    return {
        t: ([("personal_data", NAME, 0), ("personal_data", MAIL, 0)] if t.startswith("Uno") else [])
        for t in unit_texts(data)
    }


async def table(eng: Any, name: str) -> Any:
    return getattr(eng.tables, name)


async def exec_sql(eng: Any, stmt: Any) -> Any:
    async with eng._engine.begin() as c:
        res = await c.execute(stmt)
        return list(res.all()) if res.returns_rows else None


async def protected(
    world: World, data: bytes | None = None, *, context: Any = None, adapter: Any = None, key: str = "f1"
) -> dict[str, Any]:
    data = data or DOC.build()
    base = await setup_scope(world)
    eng = await world.engine(adapter or await mock_for(base, picks(data)))
    fid, aid = await analyse(eng, data, key=key)
    o = ops(eng)
    acc = await o.submit_docx_protection(
        "a", analysis_job_id=aid, policy=PSEUDO, context=context or NewContext(), idempotency_key="p-" + key
    )
    out = await run(eng, acc.job.job_id)
    return {"eng": eng, "o": o, "fid": fid, "aid": aid, "acc": acc, "out": out, "base": base, "data": data}


async def restore(s: dict[str, Any], key: str = "r") -> Any:
    o, acc = s["o"], s["acc"]
    res = await o.get_docx_protection_result("a", acc.job.job_id)
    derivative = await read_all(await o.open_docx_protected("a", acc.job.job_id))
    cid = await upload(o, derivative, "copy-" + key)
    racc = await o.submit_docx_restoration(
        "a",
        derivative_id=res.derivative_id,
        file_id=cid,
        context_id=acc.context_id,
        kind=acc.context_kind,
        idempotency_key=key,
    )
    return racc, await run(s["eng"], racc.job.job_id), res


# ---- S10b ---------------------------------------------------------------------------------------
async def test_model_analysis_and_provenance_mismatches_are_refused(world):  # AC-A07-82
    data = DOC.build()
    await setup_scope(world)
    outputs = {}
    for t, pk in picks(data).items():
        outputs[t] = detection(t, pk)
    model_eng = await world.engine(ModelLikeAdapter(outputs))
    _fid, aid = await analyse(model_eng, data, key="m")
    o = ops(model_eng)
    acc = await o.submit_docx_protection(
        "a", analysis_job_id=aid, policy=PSEUDO, context=NewContext(), idempotency_key="pm"
    )
    out = await run(model_eng, acc.job.job_id)
    assert (await o.get_job("a", acc.job.job_id)).failure_code == "analysis_provenance_unsupported", out
    # positive case with the real A05 analysis produced by the A03 mock
    s = await protected(world, data)
    assert s["out"].state == "succeeded"
    eng = s["eng"]
    jobs = await table(eng, "jobs")
    # header != pinned snapshot: alter the pinned descriptor of a SECOND analysis (same mock)
    _f2, aid2 = await analyse(eng, data, key="h")
    snap = (await exec_sql(eng, sa.select(jobs.c.execution_snapshot).where(jobs.c.job_id == aid2)))[0][0]
    snap = json.loads(snap) if isinstance(snap, str) else dict(snap)
    snap["inference_provenance"]["configuration_id"] = "fxs-ffffffffffffffff"
    await exec_sql(eng, sa.update(jobs).where(jobs.c.job_id == aid2).values(execution_snapshot=snap))
    acc2 = await s["o"].submit_docx_protection(
        "a", analysis_job_id=aid2, policy=PSEUDO, context=NewContext(), idempotency_key="ph"
    )
    await run(eng, acc2.job.job_id)
    assert (await s["o"].get_job("a", acc2.job.job_id)).failure_code == "analysis_provenance_unsupported"
    # one unit's provenance != projection: rewrite the stored result (kept consistent with its hash)
    _f3, aid3 = await analyse(eng, data, key="u")
    await rewrite_artifact(eng, aid3, "result", lambda doc: _flip_unit(doc))
    acc3 = await s["o"].submit_docx_protection(
        "a", analysis_job_id=aid3, policy=PSEUDO, context=NewContext(), idempotency_key="pu"
    )
    await run(eng, acc3.job.job_id)
    assert (await s["o"].get_job("a", acc3.job.job_id)).failure_code == "analysis_provenance_unsupported"


def _flip_unit(doc: dict[str, Any]) -> dict[str, Any]:
    for u in doc["units"]:
        if u["status"] == "analyzed":
            u["analysis"]["provenance"]["simulated"] = False
            break
    return doc


async def rewrite_artifact(eng: Any, job_id: str, kind: str, fn: Any) -> None:
    """Rewrites a published artifact AND its registered hash/size (so integrity checks pass): only the semantic
    checks of A07 can catch the change."""
    arts = await table(eng, "job_artifacts")
    row = (await exec_sql(eng, sa.select(arts).where(arts.c.job_id == job_id, arts.c.kind == kind)))[0]
    path = eng.storage.managed._paths(row.storage_key)[0]
    body = json.loads(path.read_bytes())
    new = fn(body)
    data = json.dumps(new, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    path.chmod(0o600)
    path.write_bytes(data)
    await exec_sql(
        eng,
        sa.update(arts)
        .where(arts.c.artifact_id == row.artifact_id)
        .values(sha256=hashlib.sha256(data).digest(), size_bytes=len(data)),
    )


async def test_stale_map_and_inconclusive_detection(world):  # AC-A07-06, 07, 08
    data = DOC.build()
    s = await protected(world, data)
    eng, o = s["eng"], s["o"]
    _f, aid = await analyse(eng, data, key="map")
    await rewrite_artifact(
        eng, aid, "source_map", lambda m: {**m, "units": m["units"][:-1] + [m["units"][-1] | {"segments": []}]}
    )
    acc = await o.submit_docx_protection(
        "a", analysis_job_id=aid, policy=PSEUDO, context=NewContext(), idempotency_key="sm"
    )
    await run(eng, acc.job.job_id)
    assert (await o.get_job("a", acc.job.job_id)).failure_code == "analysis_source_stale"
    # other extraction configuration on the worker: the analysis is stale, never recomputed
    other = await world.engine(await mock_for(s["base"], picks(data)), limits=fast_limits(max_xml_depth=63))
    o2 = ops(other)
    acc2 = await o2.submit_docx_protection(
        "a", analysis_job_id=s["aid"], policy=PSEUDO, context=NewContext(), idempotency_key="cfg"
    )
    await run(other, acc2.job.job_id)
    assert (await o2.get_job("a", acc2.job.job_id)).failure_code == "analysis_source_stale"
    # inconclusive detection in one unit
    texts = unit_texts(data)
    out = {t: detection(t, pk) for t, pk in picks(data).items()}
    out[texts[0]]["detection"]["status"] = "inconclusive"
    out[texts[0]]["detection"]["reason"] = "abstained"
    from cawbase_content_engine.inference import MockFixture, MockInferenceAdapter

    from .a03_helpers import request_of

    fx = []
    for i, t in enumerate(texts):
        req = await request_of(ops(s["base"], [SP.PROFILES_READ]), "a", t, caps=CAPS)
        fx.append(MockFixture.for_request(f"i{i}", req, {"kind": "output", "output": out[t]}))
    inc = await world.engine(MockInferenceAdapter(fx, acknowledge_simulated=True))
    _f4, aid4 = await analyse(inc, data, key="inc")
    acc4 = await ops(inc).submit_docx_protection(
        "a", analysis_job_id=aid4, policy=PSEUDO, context=NewContext(), idempotency_key="inc"
    )
    await run(inc, acc4.job.job_id)
    assert (await o.get_job("a", acc4.job.job_id)).failure_code == "detection_inconclusive"


# ---- restoration after deletions, purge; denial after deleting the context -------------------------
async def test_restore_after_purging_original_analysis_and_protection_job_data(world):  # AC-A07-42, 51
    s = await protected(world)
    eng, o, acc = s["eng"], s["o"], s["acc"]
    derivative = await read_all(await o.open_docx_protected("a", acc.job.job_id))
    res = await o.get_docx_protection_result("a", acc.job.job_id)
    copy_id = await upload(o, derivative, "copy-early")
    await o.delete_file("a", s["fid"])  # the original
    await o.delete_job_data("a", s["aid"])  # the source analysis
    await o.delete_job_data("a", acc.job.job_id)  # the protection job's own artifacts (P-A07-06 keeps the binding)
    jobs = await table(eng, "jobs")
    old = (await exec_sql(eng, sa.select(sa.func.now())))[0][0]
    _ = old
    await eng.maintenance().run_cleanup()
    info = await o.get_docx_derivative("a", res.derivative_id)
    assert info.state == "active"
    racc = await o.submit_docx_restoration(
        "a",
        derivative_id=res.derivative_id,
        file_id=copy_id,
        context_id=acc.context_id,
        kind="job",
        idempotency_key="after",
    )
    out = await run(eng, racc.job.job_id)
    assert out.state == "succeeded", await o.get_job("a", racc.job.job_id)
    restored = await read_all(await o.open_docx_restored("a", racc.job.job_id))
    assert unit_texts(restored) == unit_texts(s["data"])
    # purge the metadata of the protection job too: the binding is independent of it
    await exec_sql(eng, sa.delete(jobs).where(jobs.c.job_id == acc.job.job_id))
    racc2 = await o.submit_docx_restoration(
        "a",
        derivative_id=res.derivative_id,
        file_id=copy_id,
        context_id=acc.context_id,
        kind="job",
        idempotency_key="after2",
    )
    assert (await run(eng, racc2.job.job_id)).state == "succeeded"


async def test_deleting_the_context_ends_restorability_of_every_derivative(world):  # AC-A07-83
    data = DOC.build()
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, picks(data)))
    o = ops(eng)
    ctx = await o.create_context("a", kind="job")
    derived = []
    for key in ("one", "two"):
        _f, aid = await analyse(eng, data, key=key)
        acc = await o.submit_docx_protection(
            "a", analysis_job_id=aid, policy=PSEUDO, context=ExistingContext(ctx.context_id, "job"), idempotency_key=key
        )
        assert (await run(eng, acc.job.job_id)).state == "succeeded"
        res = await o.get_docx_protection_result("a", acc.job.job_id)
        copy = await upload(o, await read_all(await o.open_docx_protected("a", acc.job.job_id)), "c-" + key)
        derived.append((res.derivative_id, copy))
    accepted = await o.submit_docx_restoration(
        "a",
        derivative_id=derived[0][0],
        file_id=derived[0][1],
        context_id=ctx.context_id,
        kind="job",
        idempotency_key="before",
    )
    await o.delete_context("a", ctx.context_id)
    out = await run(eng, accepted.job.job_id)  # accepted before the deletion: no restored DOCX is published
    assert (await o.get_job("a", accepted.job.job_id)).failure_code == "context_unavailable", out
    for did, copy in derived:
        with pytest.raises(ContextUnavailableError):
            await o.submit_docx_restoration(
                "a",
                derivative_id=did,
                file_id=copy,
                context_id=ctx.context_id,
                kind="job",
                idempotency_key="x" + did[-4:],
            )
    summary = await eng.maintenance().run_docx_cleanup()
    assert summary.bindings_ended == 2
    for did, copy in derived:
        assert (await o.get_docx_derivative("a", did)).state == "ended"
        with pytest.raises(DerivativeBindingUnavailableError):
            await o.submit_docx_restoration(
                "a",
                derivative_id=did,
                file_id=copy,
                context_id=ctx.context_id,
                kind="job",
                idempotency_key="y" + did[-4:],
            )
    assert not hasattr(o, "revoke_docx_derivative")  # no per-derivative revocation (P-A07-06)


async def test_context_rules_on_submission(world):  # AC-A07-45
    s = await protected(world)
    o, acc = s["o"], s["acc"]
    res = await o.get_docx_protection_result("a", acc.job.job_id)
    copy = await upload(o, await read_all(await o.open_docx_protected("a", acc.job.job_id)), "c")
    with pytest.raises(ContextKindMismatchError):
        await o.submit_docx_restoration(
            "a",
            derivative_id=res.derivative_id,
            file_id=copy,
            context_id=acc.context_id,
            kind="conversation",
            idempotency_key="k",
        )
    other = await o.create_context("a", kind="job")
    with pytest.raises(ResourceNotFoundError):
        await o.submit_docx_restoration(
            "a",
            derivative_id=res.derivative_id,
            file_id=copy,
            context_id=other.context_id,
            kind="job",
            idempotency_key="k2",
        )
    short = await o.create_context("a", kind="job", ttl_s=60)
    _f, aid = await analyse(s["eng"], s["data"], key="short")
    with pytest.raises(ContextExpiringError):
        await o.submit_docx_protection(
            "a",
            analysis_job_id=aid,
            policy=PSEUDO,
            context=ExistingContext(short.context_id, "job"),
            idempotency_key="short",
        )


# ---- reading the restored DOCX (P-A07-03) --------------------------------------------------------------
async def test_reading_the_restored_docx_needs_context_and_active_scope(world):  # AC-A07-57
    s = await protected(world)
    racc, out, _res = await restore(s)
    assert out.state == "succeeded"
    o, eng = s["o"], s["eng"]
    assert await read_all(await o.open_docx_restored("a", racc.job.job_id))
    with pytest.raises(PermissionDeniedError):
        await ops(eng, [SP.DOCX_RESTORE, SP.RESULTS_READ]).open_docx_restored("a", racc.job.job_id)
    admin = eng.admin(await admin_ctx(eng))
    await admin.set_scope_status("a", "disabled")
    for reader in (o.open_docx_restored, o.get_docx_restoration_result):
        with pytest.raises(ScopeDisabledError):
            await reader("a", racc.job.job_id)
    assert await read_all(await o.open_docx_protected("a", s["acc"].job.job_id))  # A05 reads: allowed
    await admin.set_scope_status("a", "active")
    await o.delete_context("a", s["acc"].context_id)
    for reader in (o.open_docx_restored, o.get_docx_restoration_result):
        with pytest.raises(ContextUnavailableError):
            await reader("a", racc.job.job_id)


async def test_authorization_instant_of_the_restored_download(world, monkeypatch):  # AC-A07-84
    s = await protected(world)
    racc, out, _res = await restore(s)
    o, eng = s["o"], s["eng"]
    from cawbase_content_engine.docx_protection import ops as dops

    original_spool = dops.spool_artifact
    calls = {"n": 0}

    async def slow_spool(ctx, scope_id, job_id, art):  # the context is deleted WHILE storage is read
        stream = await original_spool(ctx, scope_id, job_id, art)
        if calls["n"] == 0:
            calls["n"] += 1
            await o.delete_context("a", s["acc"].context_id)
        return stream

    monkeypatch.setattr(dops, "spool_artifact", slow_spool)
    spool = eng.storage  # noqa: F841
    with pytest.raises(ContextUnavailableError):
        await o.open_docx_restored("a", racc.job.job_id)
    monkeypatch.setattr(dops, "spool_artifact", original_spool)
    # a download handed out BEFORE the deletion completes from the verified spool
    s2 = await protected(world, key="second")
    racc2, _out2, _ = await restore(s2, key="r2")
    stream = await s2["o"].open_docx_restored("a", racc2.job.job_id)
    await s2["o"].delete_context("a", s2["acc"].context_id)
    assert unit_texts(await read_all(stream)) == unit_texts(s2["data"])


# ---- failures between steps; purge of a discarded restored DOCX (P-A07-04) --------------------------
async def test_context_deleted_before_publication_and_immediate_purge(world, monkeypatch):  # AC-A07-61, 64, 87
    s = await protected(world)
    o, eng = s["o"], s["eng"]
    from cawbase_content_engine.docx_protection import execute as dex

    real_child = dex._child

    async def child(w, data, mode, a07=None):
        out = await real_child(w, data, mode, a07)
        if mode == "docx_verify" and a07 and a07.get("mode") == "restore":
            await o.delete_context("a", s["acc"].context_id)  # after decrypting, before publishing
        return out

    monkeypatch.setattr(dex, "_child", child)
    racc, out, _res = await restore(s)
    info = await o.get_job("a", racc.job.job_id)
    assert info.state == "failed" and info.failure_code == "context_unavailable"
    arts = await table(eng, "job_artifacts")
    rows = await exec_sql(eng, sa.select(arts).where(arts.c.job_id == racc.job.job_id))
    restored = [x for x in rows if x.kind == "docx_restored"]
    assert restored and all(x.state == "discarded" and x.delete_after <= x.abandoned_at for x in restored)
    others = [x for x in rows if x.kind != "docx_restored"]
    assert all(x.delete_after > x.abandoned_at for x in others)
    await eng.maintenance().run_cleanup()
    rows = await exec_sql(eng, sa.select(arts).where(arts.c.job_id == racc.job.job_id, arts.c.kind == "docx_restored"))
    assert all(x.physically_deleted_at is not None for x in rows)


async def test_purge_of_a_restored_docx_discarded_by_an_older_runtime(world):  # AC-A07-87
    s = await protected(world)
    racc, out, _res = await restore(s)
    eng = s["eng"]
    arts = await table(eng, "job_artifacts")
    from datetime import UTC, datetime, timedelta

    row = (
        await exec_sql(eng, sa.select(arts).where(arts.c.job_id == racc.job.job_id, arts.c.kind == "docx_restored"))
    )[0]
    # what a 0.5.0 runtime would leave: discarded with the generic 24 h delay
    await exec_sql(
        eng,
        sa.update(arts)
        .where(arts.c.artifact_id == row.artifact_id)
        .values(
            state="discarded", abandoned_at=datetime.now(UTC), delete_after=datetime.now(UTC) + timedelta(hours=24)
        ),
    )
    await eng.maintenance().run_cleanup()
    row = (await exec_sql(eng, sa.select(arts).where(arts.c.artifact_id == row.artifact_id)))[0]
    assert row.physically_deleted_at is not None  # 0.6.0 cleanup applies P-A07-04 to it


async def test_retry_reuses_markers_and_publishes_once(world, monkeypatch):  # AC-A07-62, 63
    from cawbase_content_engine.docx_protection import execute as dex
    from cawbase_content_engine.jobs.worker import JobFailure

    real_child = dex._child
    state = {"failed": False}

    async def flaky(w, data, mode, a07=None):
        if mode == "docx_verify" and not state["failed"]:
            state["failed"] = True
            raise JobFailure("extraction_process_failed")  # retryable: the attempt is lost after the mappings
        return await real_child(w, data, mode, a07)

    monkeypatch.setattr(dex, "_child", flaky)
    data = DOC.build()
    base = await setup_scope(world)
    # This test exercises the injected retryable failure, the retry, marker reuse and a single publication; it is
    # NOT a lease-loss test. A five-child attempt on a slow runner can go 3 s without a committed heartbeat
    # (observed once in GitHub Actions with SQLite: the lease expired before the failure was decided, so the
    # attempt ended `lease_lost`, the fail-safe path). Only here the lease gets a margin; backoff stays fast.
    eng = await world.engine(
        await mock_for(base, picks(data)), limits=fast_limits(lease_ttl_s=30.0, heartbeat_interval_s=1.0)
    )
    _f, aid = await analyse(eng, data)
    o = ops(eng)
    acc = await o.submit_docx_protection(
        "a", analysis_job_id=aid, policy=PSEUDO, context=NewContext(), idempotency_key="rt"
    )
    first = await run(eng, acc.job.job_id)
    assert (await o.get_job("a", acc.job.job_id)).state == "retry_wait", first
    m = eng.tables.protection_mappings
    before = len(await exec_sql(eng, sa.select(m.c.token)))
    jobs = eng.tables.jobs
    await exec_sql(
        eng,
        sa.update(jobs).where(jobs.c.job_id == acc.job.job_id).values(available_at=sa.func.now())
        if eng.backend.value == "postgresql"
        else sa.update(jobs)
        .where(jobs.c.job_id == acc.job.job_id)
        .values(available_at=sa.text("strftime('%Y-%m-%d %H:%M:%f','now','-1 second')")),
    )
    second = await run(eng, acc.job.job_id)
    assert second.state == "succeeded", second
    res = await o.get_docx_protection_result("a", acc.job.job_id)
    assert res.counts["reused_markers"] == before and res.counts["new_markers"] == 0
    assert len(await exec_sql(eng, sa.select(m.c.token))) == before
    arts = eng.tables.job_artifacts
    rows = await exec_sql(eng, sa.select(arts).where(arts.c.job_id == acc.job.job_id))
    assert sum(1 for x in rows if x.state == "published") == 2
    d = eng.tables.docx_derivatives
    assert len(await exec_sql(eng, sa.select(d).where(d.c.protection_job_id == acc.job.job_id))) == 1


async def test_key_absent_and_tampered_rows_fail_closed(world):  # AC-A07-46, 47, 44(b)
    s = await protected(world)
    eng, o = s["eng"], s["o"]
    d, m = eng.tables.docx_derivatives, eng.tables.protection_mappings
    # tampered binding (occurrence moved in the database) -> MAC fails before any value is read
    row = (await exec_sql(eng, sa.select(d)))[0]
    occ = list(row.occurrences)
    occ[0] = {**occ[0], "start": occ[0]["start"] + 1}
    await exec_sql(eng, sa.update(d).where(d.c.derivative_id == row.derivative_id).values(occurrences=occ))
    racc, out, _ = await restore(s, key="tamper")
    assert (await o.get_job("a", racc.job.job_id)).failure_code in (
        "binding_integrity_failed",
        "derivative_markers_invalid",
    )
    await exec_sql(eng, sa.update(d).where(d.c.derivative_id == row.derivative_id).values(occurrences=row.occurrences))
    # tampered mapping ciphertext
    mrow = (await exec_sql(eng, sa.select(m)))[0]
    await exec_sql(
        eng, sa.update(m).where(m.c.token == mrow.token).values(ciphertext=bytes(mrow.ciphertext)[:-1] + b"\x00")
    )
    racc2, _o2, _ = await restore(s, key="cipher")
    assert (await o.get_job("a", racc2.job.job_id)).failure_code == "mapping_integrity_failed"
    await exec_sql(eng, sa.update(m).where(m.c.token == mrow.token).values(ciphertext=mrow.ciphertext))
    # the key version is gone
    world.keys.ring.pop("v1")
    racc3, _o3, _ = await restore(s, key="nokey")
    assert (await o.get_job("a", racc3.job.job_id)).failure_code == "protection_key_unavailable"


async def test_no_values_in_logs(world, caplog):  # AC-A07-69
    caplog.set_level(logging.DEBUG, logger="cawbase_content_engine")
    s = await protected(world)
    await restore(s)
    text = "\n".join(
        r.getMessage() + str(getattr(r, "__dict__", {}))
        for r in caplog.records
        if r.name.startswith("cawbase_content_engine")
    )
    assert NAME not in text and MAIL not in text and "[[CCE1:" not in text


async def test_concurrent_protection_and_context_deletion(world):  # AC-A07-61 (race, both orders)
    data = DOC.build()
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, picks(data)))
    o = ops(eng)
    _f, aid = await analyse(eng, data)
    acc = await o.submit_docx_protection(
        "a", analysis_job_id=aid, policy=PSEUDO, context=NewContext(), idempotency_key="race"
    )
    job = asyncio.ensure_future(run(eng, acc.job.job_id))
    await asyncio.sleep(0.05)
    await o.delete_context("a", acc.context_id)
    await job
    info = await o.get_job("a", acc.job.job_id)
    d = eng.tables.docx_derivatives
    bindings = await exec_sql(eng, sa.select(d).where(d.c.protection_job_id == acc.job.job_id))
    if info.state == "succeeded":
        assert len(bindings) == 1  # published before the deletion; restoration now fails (context gone)
    else:
        assert info.failure_code == "context_unavailable" and not bindings
    m = eng.tables.protection_mappings
    assert not await exec_sql(eng, sa.select(m).where(m.c.context_id == acc.context_id))
