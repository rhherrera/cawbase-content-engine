"""Job operations of the scoped surface: submit (idempotent), read, cancel, results, data deletion."""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import AsyncIterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import (
    CapabilitiesNotConfiguredError,
    DocumentsNotConfiguredError,
    FileExpiringError,
    FileNotUsableError,
    IdempotencyConflictError,
    InferenceNotConfiguredError,
    JobDataExpiredError,
    JobNotSucceededError,
    JobNotTerminalError,
    QuotaExceededError,
    ResourceNotFoundError,
    ScopeDisabledError,
    StoredObjectUnavailableError,
    ValidationFailedError,
)
from ..inference.service import check_capabilities, check_text
from ..operations.common import insert_ignore
from ..operations.selection import select_published_version
from . import db as jdb
from . import files as fl
from .ident import EXTRACTOR_ID, EXTRACTOR_VERSION, config_sha, validation_identity
from .models import FileRef, JobAccepted, JobInfo
from .spool import SpooledStream

if TYPE_CHECKING:
    from ..engine import ContentEngine

OPERATIONS = ("document_analysis", "text_analysis")
_JOB_ID_PREFIX = "job_"


def check_job_id(value: Any) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(r"job_[0-9a-f]{32}", value):
        raise ValidationFailedError("Invalid job_id.", field="job_id")
    return value


def requirements_key(engine: ContentEngine, operation: str) -> str:
    """What a worker must be able to reproduce exactly (docs 8.5)."""
    inf = engine.inference
    conf = inf.descriptor.configuration_id if inf else None
    if operation == "text_analysis":
        return jdb.sha256_hex("text", conf or "")
    docs = engine.documents
    profile = getattr(docs.runner, "profile", "") if docs else ""
    lim = engine.jobs_setup.limits
    return jdb.sha256_hex("document", EXTRACTOR_ID, EXTRACTOR_VERSION, config_sha(lim), conf or "", profile)


def job_info(row: Any, *, cancel_outcome: str | None = None) -> JobInfo:
    m = row._mapping
    snap = m["execution_snapshot"]
    if isinstance(snap, str):
        snap = json.loads(snap)
    inp = snap["input"]
    ctxs = snap.get("context")
    return JobInfo(
        scope_id=m["scope_id"],
        job_id=m["job_id"],
        operation=m["operation"],
        state=m["state"],
        phase=m["phase"],
        attempt_count=m["attempt_no"],
        max_attempts=m["max_attempts"],
        cancel_requested_at=m["cancel_requested_at"],
        cancel_reason=m["cancel_reason"],
        failure_code=m["failure_code"],
        last_error_code=m["last_error_code"],
        accepted_at=m["accepted_at"],
        started_at=m["started_at"],
        terminated_at=m["terminated_at"],
        deadline_at=m["deadline_at"],
        input=FileRef(file_id=inp["file_id"], sha256=inp.get("sha256"), size_bytes=inp.get("size_bytes")),
        context=FileRef(file_id=ctxs["file_id"], sha256=ctxs.get("sha256"), size_bytes=ctxs.get("size_bytes"))
        if ctxs
        else None,
        profile={
            "profile_id": m["profile_id"],
            "version_id": m["version_id"],
            "version_number": m["version_number"],
            "content_hash": m["profile_content_hash"],
            "selection": m["selection"],
        },
        data_state=m["data_state"],
        result_expires_at=m["result_expires_at"],
        metadata_expires_at=m["metadata_expires_at"],
        cancel_outcome=cancel_outcome,
    )


async def _job_row(conn: AsyncConnection, t: Any, scope_id: str, job_id: str, *, lock: bool = False) -> Any:
    q = sa.select(t.jobs).where(t.jobs.c.scope_id == scope_id, t.jobs.c.job_id == job_id)
    return (await conn.execute(q.with_for_update() if lock else q)).first()


# ---- submit --------------------------------------------------------------------------------------
async def submit(
    engine: ContentEngine,
    scope_id: str,
    *,
    operation: str,
    idempotency_key: str,
    file_id: str | None = None,
    text: str | None = None,
    profile_id: str | None = None,
    version_number: int | None = None,
    capabilities: Any = ("classification", "detection"),
    context: str | None = None,
    tx: Any = None,
) -> JobAccepted:
    ctx = fl.Ctx(engine)
    if engine.inference is None:
        raise InferenceNotConfiguredError()
    ctx.storage  # noqa: B018
    if operation == "document_analysis" and engine.documents is None:
        raise DocumentsNotConfiguredError()
    key = fl.check_idem_key(idempotency_key)
    lim = engine.config.limits
    requested = check_capabilities(capabilities)
    from .. import validation as v

    pid = None if profile_id is None else v.slug(profile_id, "profile_id")
    num = None if version_number is None else v.int_in(version_number, "version_number", 1)
    if num is not None and pid is None:
        raise ValidationFailedError("version_number needs a profile_id.", field="version_number")
    ctx_text = (
        None
        if context is None
        else check_text(context, "context", lim.max_analysis_context_chars, lim.max_analysis_context_bytes)
    )
    if operation == "text_analysis":
        body = check_text(text, "text", lim.max_analysis_text_chars, lim.max_analysis_text_bytes)
        subject: dict[str, Any] = {"text_sha256": jdb.sha256_hex(body.encode("utf-8"))}
    else:
        fl.check_file_id(file_id)
        body = ""
        subject = {"file_id": file_id}
    fingerprint = jdb.sha256_hex(
        jdb.canonical(
            {
                "operation": operation,
                **subject,
                "profile_id": pid,
                "version_number": num,
                "capabilities": sorted(requested),
                "context": {"present": False}
                if ctx_text is None
                else {"present": True, "sha256": jdb.sha256_hex(ctx_text.encode("utf-8"))},
            }
        )
    )
    kh = jdb.sha256_hex(key)

    # step 2: replay BEFORE materializing objects or re-selecting profile/version (docs 8.3)
    replay: JobAccepted | None = await ctx.run(
        lambda c: _lookup(c, ctx, scope_id, operation, kh, fingerprint), write=False, tx=tx
    )
    if replay is not None:
        return replay

    # step 3: text / context objects through the five-step protocol, unlinked until accepted
    input_id = file_id
    ctx_id: str | None = None
    unlinked: list[str] = []
    if operation == "text_analysis":
        input_id = await fl.store_text_object(ctx, scope_id, body.encode("utf-8"))
        unlinked.append(input_id)
    if ctx_text is not None:
        ctx_id = await fl.store_text_object(ctx, scope_id, ctx_text.encode("utf-8"))
        unlinked.append(ctx_id)
    job_id = _JOB_ID_PREFIX + uuid.uuid4().hex
    spec = {
        "operation": operation,
        "kh": kh,
        "fingerprint": fingerprint,
        "input_id": input_id,
        "ctx_id": ctx_id,
        "unlinked": unlinked,
        "pid": pid,
        "num": num,
        "requested": requested,
        "job_id": job_id,
    }
    host = tx is not None
    for attempt in (1, 2):
        try:
            accepted: JobAccepted = await ctx.run(lambda c: _accept(c, ctx, scope_id, spec, host=host), tx=tx)
            return accepted
        except jdb.NeedReconcile:
            if attempt == 2:
                from ..errors import DatabaseUnavailableError

                raise DatabaseUnavailableError() from None
            await ctx.run(lambda c: reconcile_scope(c, ctx, scope_id))
    raise AssertionError("unreachable")


async def reconcile_scope(conn: AsyncConnection, ctx: fl.Ctx, scope_id: str) -> None:
    """I6: a transaction of its own (never from inside a host transaction)."""
    await jdb.lock_scope(conn, ctx.t, scope_id, exclusive=True)
    now = await jdb.db_now(conn, ctx.backend)  # decision time: after the lock wait
    await jdb.reconcile_locked(conn, ctx.t, scope_id, now, ctx.ret, create=True)


async def _lookup(
    conn: AsyncConnection, ctx: fl.Ctx, scope_id: str, operation: str, kh: str, fingerprint: str
) -> JobAccepted | None:
    t = ctx.t
    rec = (
        await conn.execute(
            sa.select(t.idempotency_records).where(
                t.idempotency_records.c.scope_id == scope_id,
                t.idempotency_records.c.operation == operation,
                t.idempotency_records.c.key_hash == kh,
            )
        )
    ).first()
    if rec is None:
        return None
    if rec.request_fingerprint != fingerprint:
        raise IdempotencyConflictError()
    row = await _job_row(conn, t, scope_id, rec.job_id)
    return JobAccepted(job=job_info(row), replayed=True) if row is not None else None


async def _accept(
    conn: AsyncConnection, ctx: fl.Ctx, scope_id: str, spec: dict[str, Any], *, host: bool
) -> JobAccepted:
    t, lim, ret = ctx.t, ctx.lim, ctx.ret
    engine = ctx.e
    operation = spec["operation"]
    now = await jdb.db_now(conn, ctx.backend)  # bookkeeping until the inputs are locked; re-read below for decisions
    scope = await jdb.lock_scope(conn, t, scope_id, exclusive=host)  # I7: the host path takes it exclusively up front
    if scope.status != "active":
        raise ScopeDisabledError()
    if host:
        fence = await jdb.reconcile_locked(conn, t, scope_id, now, ret, create=True)
    else:
        fence = await jdb.read_fence(conn, t, scope_id)
        if fence is None or fence.observed_scope_revision != scope.revision:
            raise jdb.NeedReconcile()
    s, e = await jdb.lock_usage(conn, t, scope_id, now)
    inserted = await insert_ignore(
        conn,
        t,
        t.idempotency_records,
        {
            "scope_id": scope_id,
            "operation": operation,
            "key_hash": spec["kh"],
            "request_fingerprint": spec["fingerprint"],
            "job_id": spec["job_id"],
            "upload_generation": 1,
            "created_at": now,
        },
    )
    if not inserted:  # a concurrent first request won: answer from its record, abandon our unlinked objects
        for fid in spec["unlinked"]:
            row = await fl.get_file_row(conn, t, scope_id, fid, lock=True)
            if row is not None and row.state == "stored":
                await jdb.retire_object(conn, t, now, ret, t.stored_files, row, "abandoned", delay_s=ret.temporary_s)
        replay = await _lookup(conn, ctx, scope_id, operation, spec["kh"], spec["fingerprint"])
        if replay is None:
            raise IdempotencyConflictError()
        return replay
    if s.active_jobs + 1 > lim.max_active_jobs_per_scope or e.active_jobs + 1 > lim.max_active_jobs_total:
        raise QuotaExceededError("Active jobs quota exceeded.")

    async def lock_input(file_id: str) -> Any:  # level 6: lock, THEN read what decides
        return (
            await conn.execute(
                sa.select(t.stored_files)
                .where(t.stored_files.c.scope_id == scope_id, t.stored_files.c.file_id == file_id)
                .with_for_update(read=True)
            )
        ).first()

    locked_input = await lock_input(spec["input_id"])
    locked_ctx = await lock_input(spec["ctx_id"]) if spec["ctx_id"] else None
    now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
    deadline = jdb.plus(now, lim.max_job_duration_s)

    async def need(row: Any, *, document: bool) -> Any:
        if row is None or row.state != "stored" or (document and row.origin == "text_input"):
            raise FileNotUsableError()
        if row.expires_at is None or row.expires_at <= now:
            raise FileNotUsableError()
        if row.expires_at < deadline:
            raise FileExpiringError()
        return row

    input_row = await need(locked_input, document=operation == "document_analysis")
    ctx_row = await need(locked_ctx, document=False) if spec["ctx_id"] else None
    identity: str | None = None
    if operation == "document_analysis":
        identity = validation_identity(bytes(input_row.sha256).hex(), lim)
        vr = (
            await conn.execute(
                sa.select(t.file_validations.c.result).where(
                    t.file_validations.c.scope_id == scope_id,
                    t.file_validations.c.file_id == spec["input_id"],
                    t.file_validations.c.validation_identity == identity,
                )
            )
        ).scalar_one_or_none()
        if vr == "rejected":
            raise FileNotUsableError()
    resolved = await select_published_version(
        conn, t, scope_id=scope_id, profile_id=spec["pid"], version_number=spec["num"]
    )
    from ..inference.prepared import prepare

    prepared = prepare(resolved)
    executable = tuple(c for c in spec["requested"] if prepared.configured(c))
    if not executable:
        raise CapabilitiesNotConfiguredError()
    inf = engine.inference
    assert inf is not None
    desc = dataclasses.asdict(inf.descriptor)
    isolation = getattr(engine.documents.runner, "profile", None) if engine.documents else None
    snapshot: dict[str, Any] = {
        "input": {
            "file_id": spec["input_id"],
            "sha256": bytes(input_row.sha256).hex(),
            "size_bytes": input_row.size_bytes,
            "origin": input_row.origin,
            "version_id": input_row.external_version,
        },
        "context": None
        if ctx_row is None
        else {"file_id": spec["ctx_id"], "sha256": bytes(ctx_row.sha256).hex(), "size_bytes": ctx_row.size_bytes},
        "validation_identity": identity,
        "profile": {
            "version_id": resolved.version.version_id,
            "content_hash": resolved.version.content_hash,
            "selection": resolved.selection,
        },
        "capabilities_requested": list(spec["requested"]),
        "capabilities_executable": list(executable),
        "contracts": {
            "analysis": 1,
            "inference": 1,
            "job": 1,
            "document_structures": "document-structures/1",
            "result": 1,
        },
        "extractor": {"id": EXTRACTOR_ID, "version": EXTRACTOR_VERSION, "config_sha256": config_sha(lim)}
        if operation == "document_analysis"
        else None,
        "inference_provenance": desc,
        "inference_timeout_s": inf.timeout_s,
        "isolation_profile": isolation if operation == "document_analysis" else None,
        "limits": {
            k: getattr(lim, k)
            for k in (
                "max_attempts",
                "max_job_duration_s",
                "max_attempt_duration_s",
                "max_units_per_document",
                "max_document_text_chars",
                "max_result_bytes",
                "max_source_map_bytes",
            )
        },
        "retention": {"results_s": ret.results_s, "metadata_s": ret.metadata_s},
    }
    await conn.execute(
        sa.insert(t.jobs).values(
            scope_id=scope_id,
            job_id=spec["job_id"],
            operation=operation,
            state="queued",
            phase="validation" if operation == "document_analysis" else "analysis",
            input_file_id=spec["input_id"],
            context_file_id=spec["ctx_id"],
            validation_identity=identity,
            profile_id=resolved.version.profile_id,
            version_id=resolved.version.version_id,
            version_number=resolved.version.version_number,
            profile_content_hash=resolved.version.content_hash,
            selection=resolved.selection,
            capabilities=list(executable),
            execution_snapshot=snapshot,
            execution_snapshot_hash=jdb.sha256_hex(jdb.canonical(snapshot)),
            requirements_key=requirements_key(engine, operation),
            request_fingerprint=spec["fingerprint"],
            fence_generation=fence.disable_generation,
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
    for fid in spec["unlinked"]:
        await conn.execute(
            sa.update(t.stored_files)
            .where(t.stored_files.c.scope_id == scope_id, t.stored_files.c.file_id == fid)
            .values(link_job_id=spec["job_id"], link_deadline_at=None)
        )
    await jdb.bump(conn, t, scope_id, now, active_jobs=1)
    row = await _job_row(conn, t, scope_id, spec["job_id"])
    return JobAccepted(job=job_info(row), replayed=False)


# ---- reads / cancel / results ----------------------------------------------------------------------
async def get_job(engine: ContentEngine, scope_id: str, job_id: str, *, tx: Any = None) -> JobInfo:
    ctx = fl.Ctx(engine)
    check_job_id(job_id)

    async def fn(conn: AsyncConnection) -> JobInfo:
        row = await _job_row(conn, ctx.t, scope_id, job_id)
        if row is None:
            raise ResourceNotFoundError()
        return job_info(row)

    return await ctx.run(fn, write=False, tx=tx)  # type: ignore[no-any-return]


async def list_jobs(
    engine: ContentEngine,
    scope_id: str,
    *,
    state: str | None,
    operation: str | None,
    limit: int,
    cursor: str | None,
    tx: Any = None,
) -> tuple[list[JobInfo], str | None]:
    ctx = fl.Ctx(engine)
    j = ctx.t.jobs

    async def fn(conn: AsyncConnection) -> tuple[list[JobInfo], str | None]:
        q = sa.select(j).where(j.c.scope_id == scope_id)
        if state:
            q = q.where(j.c.state == state)
        if operation:
            q = q.where(j.c.operation == operation)
        if cursor:
            q = q.where(j.c.job_id > cursor)
        rows = (await conn.execute(q.order_by(j.c.job_id).limit(limit + 1))).all()
        return [job_info(r) for r in rows[:limit]], (rows[limit - 1].job_id if len(rows) > limit else None)

    return await ctx.run(fn, write=False, tx=tx)  # type: ignore[no-any-return]


async def cancel_job(engine: ContentEngine, scope_id: str, job_id: str, *, tx: Any = None) -> JobInfo:
    """T4 / T5 / T5b (docs 5.3). Order 1 -> 3 -> 4 -> 8 -> 9."""
    ctx = fl.Ctx(engine)
    check_job_id(job_id)

    async def fn(conn: AsyncConnection) -> JobInfo:
        t = ctx.t
        t0 = await jdb.db_now(conn, ctx.backend)  # bookkeeping only
        await jdb.lock_scope(conn, t, scope_id, exclusive=False)
        await jdb.lock_usage(conn, t, scope_id, t0)
        row = await _job_row(conn, t, scope_id, job_id, lock=True)
        now = await jdb.db_now(conn, ctx.backend)  # decision time (stale lease?): after every lock wait
        if row is None:
            raise ResourceNotFoundError()
        if row.state in jdb.TERMINAL:
            return job_info(row, cancel_outcome="already_terminal")
        if row.state in ("queued", "retry_wait"):
            await jdb.terminate_job(
                conn, t, now, ctx.ret, row, state="cancelled", outcome="cancelled", cancel_reason="user_requested"
            )
            return job_info(await _job_row(conn, t, scope_id, job_id), cancel_outcome="cancelled")
        stale = (row.lease_expires_at is not None and row.lease_expires_at <= now) or (
            row.attempt_deadline_at is not None and row.attempt_deadline_at <= now
        )
        if stale:  # T5b: an expired lease is recovered here, never revived
            await jdb.terminate_job(
                conn, t, now, ctx.ret, row, state="cancelled", outcome="cancelled", cancel_reason="user_requested"
            )
            return job_info(await _job_row(conn, t, scope_id, job_id), cancel_outcome="cancelled")
        if row.cancel_requested_at is None:
            await conn.execute(
                sa.update(t.jobs)
                .where(t.jobs.c.scope_id == scope_id, t.jobs.c.job_id == job_id)
                .values(cancel_requested_at=now, cancel_reason="user_requested", updated_at=now)
            )
        return job_info(await _job_row(conn, t, scope_id, job_id), cancel_outcome="requested")

    return await ctx.run(fn, tx=tx)  # type: ignore[no-any-return]


async def _published_artifacts(ctx: fl.Ctx, scope_id: str, job_id: str) -> dict[str, Any]:
    async def fn(conn: AsyncConnection) -> dict[str, Any]:
        t = ctx.t
        now = await jdb.db_now(conn, ctx.backend)
        row = await _job_row(conn, t, scope_id, job_id)
        if row is None:
            raise ResourceNotFoundError()
        if row.state != "succeeded":
            raise JobNotSucceededError()
        if row.data_state != "available" or (row.result_expires_at is not None and row.result_expires_at <= now):
            raise JobDataExpiredError()
        ar = t.job_artifacts
        arts = (
            await conn.execute(
                sa.select(ar).where(
                    ar.c.scope_id == scope_id,
                    ar.c.artifact_id.in_([a for a in (row.result_artifact_id, row.source_map_artifact_id) if a]),
                )
            )
        ).all()
        return {a.kind: a for a in arts}

    return await ctx.run(fn, write=False)  # type: ignore[no-any-return]


async def open_job_artifact(engine: ContentEngine, scope_id: str, job_id: str, kind: str) -> SpooledStream:
    ctx = fl.Ctx(engine)
    ctx.storage  # noqa: B018
    check_job_id(job_id)
    if kind not in ("result", "source_map"):
        raise ValidationFailedError("Invalid artifact kind.", field="kind")
    arts = await _published_artifacts(ctx, scope_id, job_id)
    art = arts.get(kind)
    if art is None:
        raise ResourceNotFoundError()
    spool = fl.spool_for(ctx)
    size = int(art.size_bytes)
    spool.reserve(size)
    path = spool.new_path()
    import asyncio
    import hashlib

    digest = hashlib.sha256()
    got = 0
    try:
        with open(path, "xb") as fh:  # noqa: ASYNC230
            try:
                async for chunk in ctx.storage.open_read(art.storage_key):
                    got += len(chunk)
                    digest.update(chunk)
                    await asyncio.to_thread(fh.write, chunk)
            except Exception as exc:
                from .setup import ObjectMissing, StorageError

                if isinstance(exc, ObjectMissing):
                    await _mark_artifact_lost(ctx, scope_id, job_id, art.artifact_id)
                    raise StoredObjectUnavailableError() from None
                if isinstance(exc, StorageError):
                    from ..errors import StorageUnavailableError

                    raise StorageUnavailableError() from None
                raise
        if got != size or digest.digest() != bytes(art.sha256):
            await _mark_artifact_lost(ctx, scope_id, job_id, art.artifact_id)
            raise StoredObjectUnavailableError()
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        spool.release(size)
        raise
    return SpooledStream(spool, path, size)


async def _mark_artifact_lost(ctx: fl.Ctx, scope_id: str, job_id: str, artifact_id: str) -> None:
    async def fn(conn: AsyncConnection) -> None:
        t = ctx.t
        t0 = await jdb.db_now(conn, ctx.backend)
        await jdb.lock_scope(conn, t, scope_id, exclusive=False)
        await jdb.lock_usage(conn, t, scope_id, t0)
        row = await _job_row(conn, t, scope_id, job_id, lock=True)
        ar = t.job_artifacts
        art = (
            await conn.execute(
                sa.select(ar).where(ar.c.scope_id == scope_id, ar.c.artifact_id == artifact_id).with_for_update()
            )
        ).first()
        now = await jdb.db_now(conn, ctx.backend)
        if row is not None and art is not None and art.state == "published":
            await jdb.retire_object(conn, t, now, ctx.ret, ar, art, "lost", delay_s=ctx.lim.purge_grace_s)
            await conn.execute(
                sa.update(t.jobs)
                .where(t.jobs.c.scope_id == scope_id, t.jobs.c.job_id == job_id)
                .values(data_state="lost", updated_at=now)
            )

    await ctx.run(fn)


async def get_job_result(engine: ContentEngine, scope_id: str, job_id: str) -> dict[str, Any]:
    stream = await open_job_artifact(engine, scope_id, job_id, "result")
    data = b"".join([c async for c in stream])
    return json.loads(data)  # type: ignore[no-any-return]


async def delete_job_data(engine: ContentEngine, scope_id: str, job_id: str, *, tx: Any = None) -> JobInfo:
    """Terminal jobs only (no race with publication). Inputs are not touched: other jobs may share them."""
    ctx = fl.Ctx(engine)
    check_job_id(job_id)

    async def fn(conn: AsyncConnection) -> JobInfo:
        t = ctx.t
        t0 = await jdb.db_now(conn, ctx.backend)
        await jdb.lock_scope(conn, t, scope_id, exclusive=False)
        await jdb.lock_usage(conn, t, scope_id, t0)
        row = await _job_row(conn, t, scope_id, job_id, lock=True)
        now = await jdb.db_now(conn, ctx.backend)
        if row is None:
            raise ResourceNotFoundError()
        if row.state not in jdb.TERMINAL:
            raise JobNotTerminalError()
        if row.data_state == "available":
            ar = t.job_artifacts
            arts = (
                await conn.execute(
                    sa.select(ar)
                    .where(ar.c.scope_id == scope_id, ar.c.job_id == job_id, ar.c.state == "published")
                    .order_by(ar.c.artifact_id)
                    .with_for_update()
                )
            ).all()
            for a in arts:
                await jdb.retire_object(conn, t, now, ctx.ret, ar, a, "deleted", delay_s=ctx.lim.purge_grace_s)
            await conn.execute(
                sa.update(t.jobs)
                .where(t.jobs.c.scope_id == scope_id, t.jobs.c.job_id == job_id)
                .values(data_state="deleted", updated_at=now)
            )
            row = await _job_row(conn, t, scope_id, job_id)
        return job_info(row)

    return await ctx.run(fn, tx=tx)  # type: ignore[no-any-return]


_ = (datetime, AsyncIterable)
