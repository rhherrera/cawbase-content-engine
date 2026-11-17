"""A06 with the A05 result as the analysis source (``JobAnalysis``): identity of the text, vigencia, scope,
operation, permission and the re-check in the decision transaction. AC-A06-11, 12, 13 (+ lock rank 8)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from cawbase_content_engine import ContentEngine, EngineConfig, ScopePermission
from cawbase_content_engine.errors import (
    AnalysisSourceInvalidError,
    AnalysisTextMismatchError,
    JobDataExpiredError,
    JobNotSucceededError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ValidationFailedError,
)
from cawbase_content_engine.inference import InferenceSetup
from cawbase_content_engine.jobs.setup import JobsSetup, Retention, StorageSetup
from cawbase_content_engine.jobs.storage import FilesystemStorage
from cawbase_content_engine.protection import ops as pops
from cawbase_content_engine.protection.types import JobAnalysis, ProtectionSetup

from .a03_helpers import build_profile
from .a05_helpers import FRAGMENTS, FULL, TEXT, StubAdapter, fast_limits, run_all
from .a06_helpers import PSEUDO, Keys, capture_sql

SP = ScopePermission
ALL = [*FULL, SP.TEXT_PROTECT, SP.TEXT_RESTORE, SP.CONTEXTS_READ, SP.CONTEXTS_DELETE]


@pytest.fixture
async def jh(migrated, tmp_path):
    _, url = migrated
    keys = Keys()
    eng = ContentEngine(
        EngineConfig(database_url=url),
        inference=InferenceSetup(StubAdapter(), timeout_s=10.0),
        storage=StorageSetup(managed=FilesystemStorage(tmp_path / "objects")),
        jobs=JobsSetup(limits=fast_limits(), retention=Retention()),
        protection=ProtectionSetup(keys=keys),
    )
    await eng.start()
    await build_profile(eng, "a")
    ops = eng.scoped(eng.trusted_scope_context("a", ALL, actor="host"))
    yield eng, ops, keys
    await eng.aclose()


async def done_job(eng, ops, text=TEXT, key="k1"):
    acc = await ops.submit_text_analysis("a", text=text, idempotency_key=key)
    await run_all(eng)
    return acc.job.job_id


def protect(ops, cid, job_id, text=TEXT, **kw):
    return ops.protect_text(
        "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=JobAnalysis(job_id), **kw
    )


async def test_the_result_of_an_a05_job_protects_and_restores(jh):  # AC-A06-11
    eng, ops, _ = jh
    job_id = await done_job(eng, ops)
    cid = (await ops.create_context("a", kind="conversation")).context_id
    res = await protect(ops, cid, job_id)
    assert (
        res.analysis.source == "job" and res.analysis.job_id == job_id and res.analysis.detection_status == "completed"
    )
    assert res.analysis.profile.profile_id == "contratos" and res.analysis.provenance.adapter_id == "cce-a05-stub"
    assert res.outcome == "protected" and len(res.groups) == 3 and all(f not in res.text for f in FRAGMENTS)
    # the response is evaluated by ANOTHER A05 job (over the response text, markers in place)
    reply_id = await done_job(eng, ops, res.text, key="k2")
    back = await ops.restore_text(
        "a",
        context_id=cid,
        kind="conversation",
        text=res.text,
        analysis=JobAnalysis(reply_id),
        delivered_markers=res.markers,
    )
    assert back.text == TEXT and back.analysis.source == "job" and back.analysis.job_id == reply_id


async def test_a_result_of_another_text_is_refused(jh):  # AC-A06-12
    eng, ops, _ = jh
    job_id = await done_job(eng, ops)
    cid = (await ops.create_context("a", kind="conversation")).context_id
    for other in (TEXT + "!", TEXT.replace("Ana", "Eva"), "x" * len(TEXT.encode()), TEXT[:-5], "otro texto distinto"):
        with pytest.raises(AnalysisTextMismatchError):
            await protect(ops, cid, job_id, other)
    assert await rows(eng, "protection_mappings") == []


async def test_text_length_inside_the_artifact_must_match_too(jh):  # AC-A06-12
    eng, ops, _ = jh
    job_id = await done_job(eng, ops)
    cid = (await ops.create_context("a", kind="conversation")).context_id
    other = "Texto de OTRO largo, con Ana Ejemplo y más palabras que el original."
    import hashlib

    t = eng.tables.jobs
    async with eng._engine.begin() as c:  # make the pinned identity (hash and size) claim to be that other text
        row = (await c.execute(sa.select(t.c.execution_snapshot).where(t.c.job_id == job_id))).scalar_one()
        snap = dict(row if not isinstance(row, str) else __import__("json").loads(row))
        snap["input"] = {
            **snap["input"],
            "sha256": hashlib.sha256(other.encode()).hexdigest(),
            "size_bytes": len(other.encode()),
        }
        await c.execute(sa.update(t).where(t.c.job_id == job_id).values(execution_snapshot=snap))
    with pytest.raises(AnalysisTextMismatchError):  # the artifact says its text had another length
        await protect(ops, cid, job_id, other)


@pytest.mark.parametrize(
    "column,value",
    [
        ("profile_content_hash", "0" * 64),
        ("version_id", "pvr_other"),
        ("version_number", 99),
        ("selection", "explicit"),
        ("profile_id", "otro"),
    ],
)
async def test_a_profile_that_differs_from_the_pinned_one_is_refused(jh, column, value):  # AC-A06-12
    eng, ops, _ = jh
    job_id = await done_job(eng, ops)
    cid = (await ops.create_context("a", kind="conversation")).context_id
    t = eng.tables.jobs
    async with eng._engine.begin() as c:
        await c.execute(sa.update(t).where(t.c.job_id == job_id).values(**{column: value}))
    with pytest.raises(AnalysisSourceInvalidError):
        await protect(ops, cid, job_id)


async def rows(eng, table):
    t = getattr(eng.tables, table)
    async with eng._engine.connect() as c:
        return list((await c.execute(sa.select(t))).all())


async def test_jobs_that_cannot_be_a_source(jh):  # AC-A06-13
    eng, ops, _ = jh
    cid = (await ops.create_context("a", kind="conversation")).context_id
    done = await done_job(eng, ops)
    queued = (await ops.submit_text_analysis("a", text="Otro texto en cola", idempotency_key="q")).job.job_id
    cancelled = (await ops.submit_text_analysis("a", text="Cancelado luego", idempotency_key="c")).job.job_id
    await ops.cancel_job("a", cancelled)
    with pytest.raises(JobNotSucceededError):
        await protect(ops, cid, queued, "Otro texto en cola")
    with pytest.raises(JobNotSucceededError):
        await protect(ops, cid, cancelled, "Cancelado luego")
    with pytest.raises(ResourceNotFoundError):
        await protect(ops, cid, "job_" + "0" * 32)
    with pytest.raises(ValidationFailedError):
        await protect(ops, cid, "not-a-job-id")
    other = eng.scoped(eng.trusted_scope_context("a", [SP.TEXT_PROTECT, SP.RESULTS_READ], actor="x"))
    assert (await protect(other, cid, done)).outcome == "protected"
    # a document job is never a source (its units are documental)
    t = eng.tables.jobs
    async with eng._engine.begin() as c:
        await c.execute(sa.update(t).where(t.c.job_id == done).values(operation="document_analysis"))
    with pytest.raises(AnalysisSourceInvalidError):
        await protect(ops, cid, done)
    async with eng._engine.begin() as c:
        await c.execute(sa.update(t).where(t.c.job_id == done).values(operation="text_analysis"))
    # permissions: results:read is needed on top of text:protect (and analysis:execute does not replace it)
    for perms in ([SP.TEXT_PROTECT], [SP.TEXT_PROTECT, SP.ANALYSIS_EXECUTE], [SP.RESULTS_READ]):
        with pytest.raises(PermissionDeniedError):
            await protect(eng.scoped(eng.trusted_scope_context("a", perms, actor="x")), cid, done)


async def test_expired_deleted_and_other_scope(jh):  # AC-A06-13
    from datetime import UTC, datetime, timedelta

    eng, ops, _ = jh
    cid = (await ops.create_context("a", kind="conversation")).context_id
    expired = await done_job(eng, ops, key="e")
    deleted = await done_job(eng, ops, "Texto que se elimina luego", key="d")
    t = eng.tables.jobs
    async with eng._engine.begin() as c:
        await c.execute(
            sa.update(t).where(t.c.job_id == expired).values(result_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
    with pytest.raises(JobDataExpiredError):
        await protect(ops, cid, expired)
    await ops.delete_job_data("a", deleted)
    with pytest.raises(JobDataExpiredError):
        await protect(ops, cid, deleted, "Texto que se elimina luego")
    live = await done_job(eng, ops, "Un tercer texto", key="z")
    await build_profile(eng, "b", create_scope=True)
    ob = eng.scoped(eng.trusted_scope_context("b", ALL, actor="x"))
    cb = (await ob.create_context("b", kind="conversation")).context_id
    with pytest.raises(ResourceNotFoundError):
        await ob.protect_text(
            "b", context_id=cb, kind="conversation", text="Un tercer texto", policy=PSEUDO, analysis=JobAnalysis(live)
        )


async def test_the_job_is_rechecked_in_the_decision_transaction(jh, monkeypatch):  # AC-A06-13, docs 4.2
    eng, ops, _ = jh
    cid = (await ops.create_context("a", kind="conversation")).context_id
    job_id = await done_job(eng, ops)
    real = pops.protect_decide

    async def delete_then_decide(*a, **k):
        await ops.delete_job_data("a", job_id)  # the data is deleted AFTER it was read and BEFORE the decision
        return await real(*a, **k)

    monkeypatch.setattr(pops, "protect_decide", delete_then_decide)
    with pytest.raises(JobDataExpiredError):
        await protect(ops, cid, job_id)
    assert await rows(eng, "protection_mappings") == []
    monkeypatch.setattr(pops, "protect_decide", real)
    # a restore is re-checked the same way
    job2 = await done_job(eng, ops, "Respuesta limpia", key="r")
    real_restore = pops.restore_decide

    async def expire_then_decide(*a, **k):
        await ops.delete_job_data("a", job2)
        return await real_restore(*a, **k)

    monkeypatch.setattr(pops, "restore_decide", expire_then_decide)
    with pytest.raises(JobDataExpiredError):
        await ops.restore_text(
            "a",
            context_id=cid,
            kind="conversation",
            text="Respuesta limpia",
            analysis=JobAnalysis(job2),
            delivered_markers=[],
        )


async def test_job_source_takes_the_job_lock_before_the_context_lock(jh):  # AC-A06-49 (jobs rank 8)
    eng, ops, _ = jh
    cid = (await ops.create_context("a", kind="conversation")).context_id
    job_id = await done_job(eng, ops)
    with capture_sql(eng) as stmts:
        await protect(ops, cid, job_id)
    names = []
    for s in stmts:
        for table in ("scopes", "jobs", "protection_contexts", "protection_mappings"):
            if (
                f"from {'cawbase_content_engine.' if eng.backend.value == 'postgresql' else 'cawbase_content_engine_'}{table} "
                in s
                and (" for share" in s or " for update" in s or " for no key update" in s)
            ):
                names.append(table)
    if eng.backend.value == "postgresql":  # row locks exist only there; SQLite serializes with its single writer
        assert names[:3] == ["scopes", "jobs", "protection_contexts"], names
    assert stmts  # the statement log is real on both backends
