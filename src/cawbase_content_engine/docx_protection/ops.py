"""A07 operations of the scoped surface: submissions (idempotent, durable A05 jobs) and the dedicated readers.

Lock order (docs/docx-protection.md 10.1, extends A05 6.2 and A06 11.2): 1 scopes, 2 fences, 3-4 usage,
5 idempotency, 6 stored_files, 7 file_validations, 8 jobs (ascending job_id), 9 artifacts, 11 protection usage,
12 protection_contexts, 13 protection_mappings, 14 docx_derivatives. The hour that decides is read after the locks.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import (
    AnalysisExpiringError,
    AnalysisSourceInvalidError,
    ContextExpiringError,
    DatabaseUnavailableError,
    DerivativeBindingUnavailableError,
    DerivativeMismatchError,
    FileExpiringError,
    FileNotUsableError,
    IdempotencyConflictError,
    JobDataExpiredError,
    JobNotSucceededError,
    QuotaExceededError,
    ResourceNotFoundError,
    ScopeDisabledError,
    ValidationFailedError,
)
from ..jobs import db as jdb
from ..jobs import files as fl
from ..jobs.ident import EXTRACTOR_ID, EXTRACTOR_VERSION, config_sha
from ..jobs.models import JobAccepted
from ..jobs.ops import check_job_id, job_info, published_artifact, requirements_key, spool_artifact
from ..jobs.spool import SpooledStream
from ..operations.common import insert_ignore
from ..protection import ops as pops
from ..protection.service import check_context_id, check_kind, check_policy
from ..protection.types import ProtectionPolicy, ProtectionSetup
from .types import (
    DocxDerivativeInfo,
    DocxProtectionAccepted,
    DocxProtectionResult,
    DocxRestorationResult,
    ExistingContext,
    NewContext,
)

if TYPE_CHECKING:
    from ..engine import ContentEngine

OP_PROTECT = "docx_protection"
OP_RESTORE = "docx_restoration"


def _snap(row: Any) -> dict[str, Any]:
    s = row.execution_snapshot
    return json.loads(s) if isinstance(s, str) else dict(s)


def check_derivative_id(value: Any) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(r"ddv_[0-9a-f]{32}", value):
        raise ValidationFailedError("Invalid derivative_id.", field="derivative_id")
    return value


async def _lookup(conn: AsyncConnection, ctx: fl.Ctx, scope_id: str, op: str, kh: str, fp: str) -> Any:
    t = ctx.t
    rec = (
        await conn.execute(
            sa.select(t.idempotency_records).where(
                t.idempotency_records.c.scope_id == scope_id,
                t.idempotency_records.c.operation == op,
                t.idempotency_records.c.key_hash == kh,
            )
        )
    ).first()
    if rec is None:
        return None
    if rec.request_fingerprint != fp:
        raise IdempotencyConflictError()
    return (
        await conn.execute(sa.select(t.jobs).where(t.jobs.c.scope_id == scope_id, t.jobs.c.job_id == rec.job_id))
    ).first()


async def _accepted(conn: AsyncConnection, engine: ContentEngine, row: Any, replayed: bool) -> DocxProtectionAccepted:
    snap = _snap(row)
    crow = await pops._context_row(conn, engine, row.scope_id, snap["protection_context"]["context_id"])
    return DocxProtectionAccepted(
        job=job_info(row),
        replayed=replayed,
        context_id=snap["protection_context"]["context_id"],
        context_kind=snap["protection_context"]["kind"],
        context_expires_at=crow.expires_at,
    )


async def _begin(conn: AsyncConnection, ctx: fl.Ctx, scope_id: str, op: str, kh: str, fp: str, job_id: str) -> Any:
    """Ranks 1-5 of an A07 submission. Returns ``(usage, replay_row)``: a concurrent first request may have won."""
    t = ctx.t
    t0 = await jdb.db_now(conn, ctx.backend)
    scope = await jdb.lock_scope(conn, t, scope_id, exclusive=False)
    if scope.status != "active":
        raise ScopeDisabledError()
    fence = await jdb.read_fence(conn, t, scope_id)
    if fence is None or fence.observed_scope_revision != scope.revision:
        raise jdb.NeedReconcile()
    s, e = await jdb.lock_usage(conn, t, scope_id, t0)
    inserted = await insert_ignore(
        conn,
        t,
        t.idempotency_records,
        {
            "scope_id": scope_id,
            "operation": op,
            "key_hash": kh,
            "request_fingerprint": fp,
            "job_id": job_id,
            "upload_generation": 1,
            "created_at": t0,
        },
    )
    if not inserted:
        replay = await _lookup(conn, ctx, scope_id, op, kh, fp)
        if replay is None:
            raise IdempotencyConflictError()
        return fence, replay
    if s.active_jobs + 1 > ctx.lim.max_active_jobs_per_scope or e.active_jobs + 1 > ctx.lim.max_active_jobs_total:
        raise QuotaExceededError("Active jobs quota exceeded.")
    return fence, None


async def _retrying(ctx: fl.Ctx, scope_id: str, fn: Any) -> Any:
    from ..jobs.ops import reconcile_scope

    for attempt in (1, 2):
        try:
            return await ctx.run(fn)
        except jdb.NeedReconcile:
            if attempt == 2:
                raise DatabaseUnavailableError() from None
            await ctx.run(lambda c: reconcile_scope(c, ctx, scope_id))
    raise AssertionError("unreachable")


def _common_snapshot(ctx: fl.Ctx, engine: ContentEngine) -> dict[str, Any]:
    from ..documents.docx_transform import TRANSFORMER_ID, TRANSFORMER_VERSION

    lim, ret = ctx.lim, ctx.ret
    docs = engine.documents
    return {
        "contracts": {"job": 1, "docx": 1, "document_structures": "document-structures/1"},
        "extractor": {"id": EXTRACTOR_ID, "version": EXTRACTOR_VERSION, "config_sha256": config_sha(lim)},
        "transformer": {"id": TRANSFORMER_ID, "version": TRANSFORMER_VERSION},
        "isolation_profile": getattr(docs.runner, "profile", None) if docs else None,
        "limits": {k: getattr(lim, k) for k in ("max_attempts", "max_job_duration_s", "max_file_bytes")},
        "retention": {"results_s": ret.results_s, "metadata_s": ret.metadata_s},
    }


async def _insert_job(
    conn: AsyncConnection,
    ctx: fl.Ctx,
    engine: ContentEngine,
    *,
    scope_id: str,
    job_id: str,
    op: str,
    input_file_id: str,
    validation_identity: str | None,
    profile: dict[str, Any],
    snapshot: dict[str, Any],
    fingerprint: str,
    fence_generation: int,
    now: datetime,
    deadline: datetime,
) -> Any:
    t, lim, ret = ctx.t, ctx.lim, ctx.ret
    await conn.execute(
        sa.insert(t.jobs).values(
            scope_id=scope_id,
            job_id=job_id,
            operation=op,
            state="queued",
            phase="validation",
            input_file_id=input_file_id,
            context_file_id=None,
            validation_identity=validation_identity,
            profile_id=profile["profile_id"],
            version_id=profile["version_id"],
            version_number=profile["version_number"],
            profile_content_hash=profile["content_hash"],
            selection=profile["selection"],
            capabilities=["detection"],
            execution_snapshot=snapshot,
            execution_snapshot_hash=jdb.sha256_hex(jdb.canonical(snapshot)),
            requirements_key=requirements_key(engine, op),
            request_fingerprint=fingerprint,
            fence_generation=fence_generation,
            attempt_no=0,
            max_attempts=lim.max_attempts,
            available_at=now,
            accepted_at=now,
            deadline_at=deadline,
            data_state="none",
            result_retention_s=ret.results_s,
            metadata_retention_s=ret.metadata_s,
            updated_at=now,
        )
    )
    await jdb.bump(conn, t, scope_id, now, active_jobs=1)
    return (await conn.execute(sa.select(t.jobs).where(t.jobs.c.scope_id == scope_id, t.jobs.c.job_id == job_id))).one()


# ---- protection submission ------------------------------------------------------------------------
async def submit_protection(
    engine: ContentEngine,
    setup: ProtectionSetup,
    scope_id: str,
    *,
    analysis_job_id: str,
    policy: ProtectionPolicy,
    context: ExistingContext | NewContext,
    idempotency_key: str,
) -> DocxProtectionAccepted:
    ctx = fl.Ctx(engine)
    ctx.storage  # noqa: B018
    key = fl.check_idem_key(idempotency_key)
    check_job_id(analysis_job_id)
    pol = check_policy(policy)
    if isinstance(context, ExistingContext):
        spec_ctx: dict[str, Any] = {
            "existing": True,
            "context_id": check_context_id(context.context_id),
            "kind": check_kind(context.kind),
        }
    elif isinstance(context, NewContext):
        ttl = setup.context_ttl_s if context.ttl_s is None else context.ttl_s
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not (60 <= ttl <= setup.max_context_ttl_s):
            raise ValidationFailedError("Invalid ttl_s.", field="ttl_s")
        spec_ctx = {"new": True, "ttl_s": context.ttl_s}
    else:
        raise ValidationFailedError("Invalid context.", field="context")
    policy_doc = {"default_action": pol.default_action, "actions": dict(sorted(pol.actions.items()))}
    fp = jdb.sha256_hex(
        jdb.canonical(
            {"operation": OP_PROTECT, "analysis_job_id": analysis_job_id, "policy": policy_doc, "context": spec_ctx}
        )
    )
    kh = jdb.sha256_hex(key)

    async def peek(conn: AsyncConnection) -> Any:
        return await _lookup(conn, ctx, scope_id, OP_PROTECT, kh, fp)

    replay = await ctx.run(peek, write=False)
    if replay is not None:
        return await ctx.run(lambda c: _accepted(c, engine, replay, True), write=False)  # type: ignore[no-any-return]
    job_id = "job_" + uuid.uuid4().hex

    async def accept(conn: AsyncConnection) -> DocxProtectionAccepted:
        t = ctx.t
        fence, won = await _begin(conn, ctx, scope_id, OP_PROTECT, kh, fp, job_id)
        if won is not None:
            return await _accepted(conn, engine, won, True)
        j = t.jobs
        a_peek = (
            await conn.execute(sa.select(j).where(j.c.scope_id == scope_id, j.c.job_id == analysis_job_id))
        ).first()
        if a_peek is None:
            raise ResourceNotFoundError()
        f_row = (  # 6: the original, locked BEFORE the job rows
            await conn.execute(
                sa.select(t.stored_files)
                .where(t.stored_files.c.scope_id == scope_id, t.stored_files.c.file_id == a_peek.input_file_id)
                .with_for_update(read=True)
            )
        ).first()
        a_row = (  # 8: the source analysis, shared
            await conn.execute(
                sa.select(j).where(j.c.scope_id == scope_id, j.c.job_id == analysis_job_id).with_for_update(read=True)
            )
        ).first()
        if spec_ctx.get("existing"):
            c_row = await pops._context_row(conn, engine, scope_id, spec_ctx["context_id"], "share")  # 12
        else:
            c_row = None
        now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
        deadline = jdb.plus(now, ctx.lim.max_job_duration_s)
        if a_row is None:
            raise ResourceNotFoundError()
        if a_row.operation != "document_analysis":  # S1
            raise AnalysisSourceInvalidError()
        if a_row.state != "succeeded":  # S2
            raise JobNotSucceededError()
        if a_row.data_state != "available" or a_row.result_expires_at is None or a_row.result_expires_at <= now:
            raise JobDataExpiredError()
        if a_row.result_expires_at < deadline:  # S3
            raise AnalysisExpiringError()
        if f_row is None or f_row.state != "stored" or f_row.expires_at is None or f_row.expires_at <= now:  # S4
            raise FileNotUsableError()
        if f_row.expires_at < deadline:
            raise FileExpiringError()
        vr = (
            await conn.execute(
                sa.select(t.file_validations.c.result).where(
                    t.file_validations.c.scope_id == scope_id,
                    t.file_validations.c.file_id == f_row.file_id,
                    t.file_validations.c.validation_identity == a_row.validation_identity,
                )
            )
        ).scalar_one_or_none()
        if vr != "passed":
            raise FileNotUsableError()
        if c_row is not None or spec_ctx.get("existing"):
            pops._check_usable(c_row, spec_ctx["kind"], now)
            ctx_id, kind, c_exp = c_row.context_id, c_row.kind, c_row.expires_at
        else:
            ttl = setup.context_ttl_s if spec_ctx["ttl_s"] is None else spec_ctx["ttl_s"]
            new_row, _ = await pops.insert_context(conn, engine, setup, scope_id, "job", ttl)  # 11 -> 12
            ctx_id, kind, c_exp = new_row.context_id, new_row.kind, new_row.expires_at
        if c_exp < deadline:
            raise ContextExpiringError()
        a_snap = _snap(a_row)
        snapshot = {
            **_common_snapshot(ctx, engine),
            "input": dict(a_snap["input"]),
            "protection_context": {"context_id": ctx_id, "kind": kind},
            "analysis_job_id": analysis_job_id,
            "validation_identity": a_row.validation_identity,
            "policy": policy_doc,
            "derivative_id": "ddv_" + uuid.uuid4().hex,
        }
        row = await _insert_job(
            conn,
            ctx,
            engine,
            scope_id=scope_id,
            job_id=job_id,
            op=OP_PROTECT,
            input_file_id=a_row.input_file_id,
            validation_identity=a_row.validation_identity,
            profile={
                "profile_id": a_row.profile_id,
                "version_id": a_row.version_id,
                "version_number": a_row.version_number,
                "content_hash": a_row.profile_content_hash,
                "selection": a_row.selection,
            },
            snapshot=snapshot,
            fingerprint=fp,
            fence_generation=fence.disable_generation,
            now=now,
            deadline=deadline,
        )
        return DocxProtectionAccepted(
            job=job_info(row), replayed=False, context_id=ctx_id, context_kind=kind, context_expires_at=c_exp
        )

    return await _retrying(ctx, scope_id, accept)  # type: ignore[no-any-return]


# ---- restoration submission -----------------------------------------------------------------------
async def submit_restoration(
    engine: ContentEngine,
    scope_id: str,
    *,
    derivative_id: str,
    file_id: str,
    context_id: str,
    kind: str,
    idempotency_key: str,
) -> JobAccepted:
    ctx = fl.Ctx(engine)
    ctx.storage  # noqa: B018
    key = fl.check_idem_key(idempotency_key)
    did = check_derivative_id(derivative_id)
    fl.check_file_id(file_id)
    cid, knd = check_context_id(context_id), check_kind(kind)
    fp = jdb.sha256_hex(
        jdb.canonical(
            {"operation": OP_RESTORE, "derivative_id": did, "file_id": file_id, "context_id": cid, "kind": knd}
        )
    )
    kh = jdb.sha256_hex(key)

    async def peek(conn: AsyncConnection) -> Any:
        return await _lookup(conn, ctx, scope_id, OP_RESTORE, kh, fp)

    replay = await ctx.run(peek, write=False)
    if replay is not None:
        return JobAccepted(job=job_info(replay), replayed=True)
    job_id = "job_" + uuid.uuid4().hex

    async def accept(conn: AsyncConnection) -> JobAccepted:
        t = ctx.t
        fence, won = await _begin(conn, ctx, scope_id, OP_RESTORE, kh, fp, job_id)
        if won is not None:
            return JobAccepted(job=job_info(won), replayed=True)
        f_row = (
            await conn.execute(
                sa.select(t.stored_files)
                .where(t.stored_files.c.scope_id == scope_id, t.stored_files.c.file_id == file_id)
                .with_for_update(read=True)
            )
        ).first()  # 6
        c_row = await pops._context_row(conn, engine, scope_id, cid, "share")  # 12
        d = t.docx_derivatives
        b_row = (
            await conn.execute(
                sa.select(d).where(d.c.scope_id == scope_id, d.c.derivative_id == did).with_for_update(read=True)
            )
        ).first()  # 14
        now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
        deadline = jdb.plus(now, ctx.lim.max_job_duration_s)
        if b_row is None or b_row.context_id != cid:  # T1: another scope or another context: indistinguishable
            raise ResourceNotFoundError()
        if b_row.state != "active":
            raise DerivativeBindingUnavailableError()
        if b_row.context_kind != knd:
            pops._check_usable(c_row, knd, now)  # -> context_kind_mismatch (or unavailable first)
        if f_row is None or f_row.state != "stored" or f_row.expires_at is None or f_row.expires_at <= now:
            raise FileNotUsableError()
        if f_row.expires_at < deadline:
            raise FileExpiringError()
        if f_row.sha256 is None or bytes(f_row.sha256).hex() != b_row.sha256 or f_row.size_bytes != b_row.size_bytes:
            raise DerivativeMismatchError()  # T2 at submission (metadata)
        pops._check_usable(c_row, knd, now)  # T3
        if c_row.expires_at < deadline:
            raise ContextExpiringError()
        snapshot = {
            **_common_snapshot(ctx, engine),
            "input": {
                "file_id": file_id,
                "sha256": bytes(f_row.sha256).hex(),
                "size_bytes": f_row.size_bytes,
                "origin": f_row.origin,
                "version_id": f_row.external_version,
            },
            "protection_context": {"context_id": cid, "kind": knd},
            "derivative_id": did,
            "validation_identity": None,
        }
        row = await _insert_job(
            conn,
            ctx,
            engine,
            scope_id=scope_id,
            job_id=job_id,
            op=OP_RESTORE,
            input_file_id=file_id,
            validation_identity=None,
            profile={
                "profile_id": b_row.profile_id,
                "version_id": b_row.version_id,
                "version_number": b_row.version_number,
                "content_hash": b_row.profile_content_hash,
                "selection": b_row.selection,
            },
            snapshot=snapshot,
            fingerprint=fp,
            fence_generation=fence.disable_generation,
            now=now,
            deadline=deadline,
        )
        return JobAccepted(job=job_info(row), replayed=False)

    return await _retrying(ctx, scope_id, accept)  # type: ignore[no-any-return]


# ---- readers ---------------------------------------------------------------------------------------
async def _read_json(ctx: fl.Ctx, scope_id: str, job_id: str, art: Any) -> dict[str, Any]:
    stream = await spool_artifact(ctx, scope_id, job_id, art)
    data = b"".join([c async for c in stream])
    return json.loads(data)  # type: ignore[no-any-return]


async def get_protection_result(engine: ContentEngine, scope_id: str, job_id: str) -> DocxProtectionResult:
    ctx = fl.Ctx(engine)
    ctx.storage  # noqa: B018
    check_job_id(job_id)
    art = await published_artifact(ctx, scope_id, job_id, "docx_protection_report", OP_PROTECT)
    doc = await _read_json(ctx, scope_id, job_id, art)
    return DocxProtectionResult(**doc)


async def open_protected(engine: ContentEngine, scope_id: str, job_id: str) -> SpooledStream:
    ctx = fl.Ctx(engine)
    ctx.storage  # noqa: B018
    check_job_id(job_id)
    art = await published_artifact(ctx, scope_id, job_id, "docx_protected", OP_PROTECT)
    return await spool_artifact(ctx, scope_id, job_id, art)


async def _authorize_restored(engine: ContentEngine, scope_id: str, job_id: str) -> None:
    """P-A07-03: ONE instant after the object was verified in the spool and right before it is handed out."""
    ctx = fl.Ctx(engine)

    async def fn(conn: AsyncConnection) -> None:
        t = ctx.t
        now = await jdb.db_now(conn, ctx.backend)
        scope = (await conn.execute(sa.select(t.scopes.c.status).where(t.scopes.c.scope_id == scope_id))).first()
        row = (
            await conn.execute(sa.select(t.jobs).where(t.jobs.c.scope_id == scope_id, t.jobs.c.job_id == job_id))
        ).first()
        if row is None or row.operation != OP_RESTORE:
            raise ResourceNotFoundError()
        if row.state != "succeeded":
            raise JobNotSucceededError()
        if row.data_state != "available" or row.result_expires_at is None or row.result_expires_at <= now:
            raise JobDataExpiredError()
        snap = _snap(row)
        c_row = await pops._context_row(conn, engine, scope_id, snap["protection_context"]["context_id"])
        pops._check_usable(c_row, snap["protection_context"]["kind"], now)
        if scope is None or scope.status != "active":
            raise ScopeDisabledError()

    await ctx.run(fn, write=False)


async def _restored_artifact(engine: ContentEngine, scope_id: str, job_id: str, role: str) -> SpooledStream:
    ctx = fl.Ctx(engine)
    ctx.storage  # noqa: B018
    check_job_id(job_id)
    art = await published_artifact(ctx, scope_id, job_id, role, OP_RESTORE)
    stream = await spool_artifact(ctx, scope_id, job_id, art)
    try:
        await _authorize_restored(engine, scope_id, job_id)
    except BaseException:
        stream.close()  # nothing is handed out; the spool budget is released
        raise
    return stream


async def open_restored(engine: ContentEngine, scope_id: str, job_id: str) -> SpooledStream:
    return await _restored_artifact(engine, scope_id, job_id, "docx_restored")


async def get_restoration_result(engine: ContentEngine, scope_id: str, job_id: str) -> DocxRestorationResult:
    stream = await _restored_artifact(engine, scope_id, job_id, "docx_restoration_report")
    data = b"".join([c async for c in stream])
    return DocxRestorationResult(**json.loads(data))


async def get_derivative(engine: ContentEngine, scope_id: str, derivative_id: str) -> DocxDerivativeInfo:
    ctx = fl.Ctx(engine)
    did = check_derivative_id(derivative_id)

    async def fn(conn: AsyncConnection) -> DocxDerivativeInfo:
        d = ctx.t.docx_derivatives
        r = (await conn.execute(sa.select(d).where(d.c.scope_id == scope_id, d.c.derivative_id == did))).first()
        if r is None:
            raise ResourceNotFoundError()
        return DocxDerivativeInfo(
            scope_id=r.scope_id,
            derivative_id=r.derivative_id,
            protection_job_id=r.protection_job_id,
            context_id=r.context_id,
            context_kind=r.context_kind,
            sha256=r.sha256,
            size_bytes=int(r.size_bytes),
            occurrence_count=int(r.occurrence_count),
            state=r.state,
            created_at=r.created_at,
            ended_at=r.ended_at,
        )

    return await ctx.run(fn, write=False)  # type: ignore[no-any-return]
