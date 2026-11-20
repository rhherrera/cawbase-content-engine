"""A08 operations of the scoped surface: submissions (idempotent, durable A05 jobs) and the dedicated readers.

Same lock order as A07 (docs/docx-protection.md 10.1), with rank 14 = ``pdf_derivatives``: 1 scopes, 2 fences,
3-4 usage, 5 idempotency, 6 stored_files, 7 file_validations, 8 jobs, 9 artifacts, 11 protection usage,
12 protection_contexts, 13 protection_mappings, 14 pdf_derivatives. The hour that decides is read after the locks.
The generic pieces (idempotency, ranks 1-5, job insertion, retries) are A07's own functions, not copies.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..docx_protection.ops import _begin, _insert_job, _lookup, _read_json, _retrying, _snap
from ..errors import (
    AnalysisExpiringError,
    AnalysisSourceInvalidError,
    ContextExpiringError,
    DerivativeBindingUnavailableError,
    DerivativeMismatchError,
    FileExpiringError,
    FileNotUsableError,
    JobDataExpiredError,
    JobNotSucceededError,
    ResourceNotFoundError,
    ScopeDisabledError,
    ValidationFailedError,
)
from ..jobs import db as jdb
from ..jobs import files as fl
from ..jobs.models import JobAccepted
from ..jobs.ops import check_job_id, job_info, published_artifact, spool_artifact
from ..jobs.spool import SpooledStream
from ..protection import ops as pops
from ..protection.service import check_context_id, check_kind, check_policy
from ..protection.types import ProtectionPolicy, ProtectionSetup
from .types import (
    ExistingContext,
    NewContext,
    PdfDerivativeInfo,
    PdfProtectionAccepted,
    PdfProtectionResult,
    PdfRestorationResult,
)

if TYPE_CHECKING:
    from ..engine import ContentEngine

OP_PROTECT = "pdf_protection"
OP_RESTORE = "pdf_restoration"


def check_derivative_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"pdv_[0-9a-f]{32}", value):
        raise ValidationFailedError("Invalid derivative_id.", field="derivative_id")
    return value


async def _accepted(conn: AsyncConnection, engine: ContentEngine, row: Any, replayed: bool) -> PdfProtectionAccepted:
    snap = _snap(row)
    crow = await pops._context_row(conn, engine, row.scope_id, snap["protection_context"]["context_id"])
    return PdfProtectionAccepted(
        job=job_info(row),
        replayed=replayed,
        context_id=snap["protection_context"]["context_id"],
        context_kind=snap["protection_context"]["kind"],
        context_expires_at=crow.expires_at,
    )


def _common_snapshot(ctx: fl.Ctx, engine: ContentEngine) -> dict[str, Any]:
    from ..documents.pdf_extract import DOCUMENT_STRUCTURES, identity
    from ..documents.pdf_transform import TRANSFORMER_ID, TRANSFORMER_VERSION

    lim, ret = ctx.lim, ctx.ret
    docs = engine.documents
    return {
        "contracts": {"job": 1, "pdf": 1, "document_structures": DOCUMENT_STRUCTURES},
        "extractor": identity(lim),
        "transformer": {"id": TRANSFORMER_ID, "version": TRANSFORMER_VERSION},
        "isolation_profile": getattr(docs.runner, "profile", None) if docs else None,
        "limits": {k: getattr(lim, k) for k in ("max_attempts", "max_job_duration_s", "max_file_bytes")},
        "retention": {"results_s": ret.results_s, "metadata_s": ret.metadata_s},
    }


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
) -> PdfProtectionAccepted:
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

    async def accept(conn: AsyncConnection) -> PdfProtectionAccepted:
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
        c_row = (
            await pops._context_row(conn, engine, scope_id, spec_ctx["context_id"], "share")
            if spec_ctx.get("existing")
            else None
        )  # 12
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
        if spec_ctx.get("existing"):
            pops._check_usable(c_row, spec_ctx["kind"], now)
            assert c_row is not None
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
            "derivative_id": "pdv_" + uuid.uuid4().hex,
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
        return PdfProtectionAccepted(
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
        d = t.pdf_derivatives
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
async def get_protection_result(engine: ContentEngine, scope_id: str, job_id: str) -> PdfProtectionResult:
    ctx = fl.Ctx(engine)
    ctx.storage  # noqa: B018
    check_job_id(job_id)
    art = await published_artifact(ctx, scope_id, job_id, "pdf_protection_report", OP_PROTECT)
    doc = await _read_json(ctx, scope_id, job_id, art)
    return PdfProtectionResult(**doc)


async def open_protected(engine: ContentEngine, scope_id: str, job_id: str) -> SpooledStream:
    ctx = fl.Ctx(engine)
    ctx.storage  # noqa: B018
    check_job_id(job_id)
    art = await published_artifact(ctx, scope_id, job_id, "pdf_protected", OP_PROTECT)
    return await spool_artifact(ctx, scope_id, job_id, art)


async def _authorize_restored(engine: ContentEngine, scope_id: str, job_id: str) -> None:
    """D-A08-06: ONE instant after the object was verified in the spool and right before it is handed out."""
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
    return await _restored_artifact(engine, scope_id, job_id, "pdf_restored")


async def get_restoration_result(engine: ContentEngine, scope_id: str, job_id: str) -> PdfRestorationResult:
    stream = await _restored_artifact(engine, scope_id, job_id, "pdf_restoration_report")
    data = b"".join([c async for c in stream])
    return PdfRestorationResult(**json.loads(data))


async def get_derivative(engine: ContentEngine, scope_id: str, derivative_id: str) -> PdfDerivativeInfo:
    ctx = fl.Ctx(engine)
    did = check_derivative_id(derivative_id)

    async def fn(conn: AsyncConnection) -> PdfDerivativeInfo:
        d = ctx.t.pdf_derivatives
        r = (await conn.execute(sa.select(d).where(d.c.scope_id == scope_id, d.c.derivative_id == did))).first()
        if r is None:
            raise ResourceNotFoundError()
        return PdfDerivativeInfo(
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
