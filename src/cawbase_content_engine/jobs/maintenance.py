"""Trusted local maintenance: lease recovery, deadlines, bounded cleanup, purges (physical, metadata, admin_events)."""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import ValidationFailedError
from . import db as jdb
from . import files as fl
from .models import CleanupSummary, PurgeSummary
from .setup import StorageError
from .worker import JobFailure, JobWorker

if TYPE_CHECKING:
    from ..engine import ContentEngine

log = logging.getLogger("cawbase_content_engine.maintenance")


async def list_runnable_jobs(
    engine: ContentEngine, *, limit: int = 100, older_than_s: float = 60.0
) -> list[tuple[str, str]]:
    """Eligible jobs whose notice may have been lost: the host can re-notify its orchestrator."""
    ctx = fl.Ctx(engine)
    j = ctx.t.jobs

    async def fn(conn: AsyncConnection) -> list[tuple[str, str]]:
        now = await jdb.db_now(conn, ctx.backend)
        rows = (
            await conn.execute(
                sa.select(j.c.scope_id, j.c.job_id)
                .where(j.c.state.in_(("queued", "retry_wait")), j.c.available_at <= jdb.plus(now, -older_than_s))
                .order_by(j.c.available_at, j.c.job_id)
                .limit(limit)
            )
        ).all()
        return [(r.scope_id, r.job_id) for r in rows]

    return await ctx.run(fn, write=False)  # type: ignore[no-any-return]


async def recover_expired_leases(engine: ContentEngine, *, limit: int = 100) -> int:
    """T5b/T6/T7/T8 for a lease or attempt deadline that expired: recovery, never revival (docs 6.7)."""
    ctx = fl.Ctx(engine)
    t, j = ctx.t, ctx.t.jobs
    worker = JobWorker(engine, "recovery")

    async def cands(conn: AsyncConnection) -> list[tuple[str, str]]:
        now = await jdb.db_now(conn, ctx.backend)
        rows = (
            await conn.execute(
                sa.select(j.c.scope_id, j.c.job_id)
                .where(j.c.state == "running", sa.or_(j.c.lease_expires_at <= now, j.c.attempt_deadline_at <= now))
                .order_by(j.c.job_id)
                .limit(limit)
            )
        ).all()
        return [(r.scope_id, r.job_id) for r in rows]

    done = 0
    for scope_id, job_id in await ctx.run(cands, write=False):

        async def one(conn: AsyncConnection, scope_id: str = scope_id, job_id: str = job_id) -> bool:
            t0 = await jdb.db_now(conn, ctx.backend)  # bookkeeping only
            await jdb.lock_scope(conn, t, scope_id, exclusive=False)
            await jdb.lock_usage(conn, t, scope_id, t0)
            row = (
                await conn.execute(
                    sa.select(j)
                    .where(j.c.scope_id == scope_id, j.c.job_id == job_id, j.c.state == "running")
                    .with_for_update(skip_locked=True)
                )
            ).first()
            now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
            if row is None or not (
                (row.lease_expires_at is not None and row.lease_expires_at <= now)
                or (row.attempt_deadline_at is not None and row.attempt_deadline_at <= now)
            ):
                return False
            if row.cancel_requested_at is not None:
                await jdb.terminate_job(
                    conn,
                    t,
                    now,
                    ctx.ret,
                    row,
                    state="cancelled",
                    outcome="cancelled",
                    cancel_reason=row.cancel_reason or "user_requested",
                )
            else:
                await worker._decide(conn, row, JobFailure("lease_lost"), now)
            return True

        if await ctx.run(one):
            done += 1
    return done


async def expire_deadlines(engine: ContentEngine, *, limit: int = 100) -> int:
    """T9: waiting jobs past their deadline, or whose input is gone."""
    ctx = fl.Ctx(engine)
    t, j = ctx.t, ctx.t.jobs
    worker = JobWorker(engine, "recovery")

    async def cands(conn: AsyncConnection) -> list[tuple[str, str]]:
        rows = (
            await conn.execute(
                sa.select(j.c.scope_id, j.c.job_id)
                .where(j.c.state.in_(("queued", "retry_wait")))
                .order_by(j.c.job_id)
                .limit(limit * 5)
            )
        ).all()
        return [(r.scope_id, r.job_id) for r in rows]

    done = 0
    for scope_id, job_id in await ctx.run(cands, write=False):

        async def one(conn: AsyncConnection, scope_id: str = scope_id, job_id: str = job_id) -> bool:
            t0 = await jdb.db_now(conn, ctx.backend)  # bookkeeping only
            await jdb.lock_scope(conn, t, scope_id, exclusive=False)
            await jdb.lock_usage(conn, t, scope_id, t0)
            peek = (
                await conn.execute(
                    sa.select(j.c.state, j.c.execution_snapshot).where(j.c.scope_id == scope_id, j.c.job_id == job_id)
                )
            ).first()
            if peek is None or peek.state not in ("queued", "retry_wait"):
                return False
            locked = await worker._lock_inputs(conn, scope_id, peek)  # level 6 BEFORE the job row (8)
            row = (
                await conn.execute(
                    sa.select(j)
                    .where(j.c.scope_id == scope_id, j.c.job_id == job_id, j.c.state.in_(("queued", "retry_wait")))
                    .with_for_update(skip_locked=True)
                )
            ).first()
            now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
            if row is None:
                return False
            if row.deadline_at <= now:
                code = "executor_unavailable" if row.attempt_no == 0 else "deadline_exceeded"
                await jdb.terminate_job(conn, t, now, ctx.ret, row, state="failed", outcome="failed", failure_code=code)
                return True
            if not worker._inputs_valid(locked, now):
                await jdb.terminate_job(
                    conn, t, now, ctx.ret, row, state="failed", outcome="failed", failure_code="input_unavailable"
                )
                return True
            return False

        if await ctx.run(one):
            done += 1
    return done


# ---- cleanup -----------------------------------------------------------------------------------------
async def run_cleanup(engine: ContentEngine, *, batch_size: int | None = None) -> CleanupSummary:
    ctx = fl.Ctx(engine)
    n = batch_size or ctx.lim.cleanup_batch_size
    counts: dict[str, int] = {}
    diag: dict[str, int] = {}
    touched: set[str] = set()

    counts["leases_recovered"] = await recover_expired_leases(engine, limit=n)
    counts["deadlines_expired"] = await expire_deadlines(engine, limit=n)
    counts["abandoned"] = await _mark_abandoned(ctx, n, touched)
    counts["expired"] = await _mark_expired(ctx, n, touched)
    await _purge_physical(ctx, n, counts, diag, touched)
    counts["metadata_purged"] = await _purge_metadata(ctx, n, touched)
    for sid in sorted(touched):
        await ctx.run(lambda c, s=sid: _recount(c, ctx, s))
    counts["scopes_recounted"] = len(touched)
    if engine.storage is not None:
        try:
            counts["spool_residue_removed"] = fl.spool_for(ctx).sweep()
        except OSError:
            diag["spool_sweep_error"] = 1
    return CleanupSummary(counts=counts, diagnostics=diag)


async def _recount(conn: AsyncConnection, ctx: fl.Ctx, scope_id: str) -> None:
    now = await jdb.db_now(conn, ctx.backend)
    await jdb.lock_scope(conn, ctx.t, scope_id, exclusive=False)
    await jdb.recompute_usage(conn, ctx.t, scope_id, now)


async def _each(ctx: fl.Ctx, rows: list[tuple[str, str]], fn: Any, touched: set[str]) -> int:
    done = 0
    for scope_id, key in rows:

        async def tx(conn: AsyncConnection, scope_id: str = scope_id, key: str = key) -> bool:
            t0 = await jdb.db_now(conn, ctx.backend)  # bookkeeping only; `fn` reads its own decision time
            await jdb.lock_scope(conn, ctx.t, scope_id, exclusive=False)
            await jdb.lock_usage(conn, ctx.t, scope_id, t0)
            return bool(await fn(conn, scope_id, key))

        if await ctx.run(tx):
            done += 1
            touched.add(scope_id)
    return done


async def _mark_abandoned(ctx: fl.Ctx, n: int, touched: set[str]) -> int:
    t, f = ctx.t, ctx.t.stored_files

    async def cands(conn: AsyncConnection) -> list[tuple[str, str]]:
        now = await jdb.db_now(conn, ctx.backend)
        rows = (
            await conn.execute(
                sa.select(f.c.scope_id, f.c.file_id)
                .where(
                    sa.or_(
                        (f.c.state == "reserved") & (f.c.upload_deadline_at <= now),
                        (f.c.state == "stored")
                        & (f.c.origin == "text_input")
                        & f.c.link_job_id.is_(None)
                        & (f.c.link_deadline_at <= now),
                    )
                )
                .order_by(f.c.file_id)
                .limit(n)
            )
        ).all()
        return [(r.scope_id, r.file_id) for r in rows]

    async def fn(conn: AsyncConnection, scope_id: str, file_id: str) -> bool:
        row = await fl.get_file_row(conn, t, scope_id, file_id, lock=True)
        now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
        if row is None:
            return False
        due = (row.state == "reserved" and row.upload_deadline_at is not None and row.upload_deadline_at <= now) or (
            row.state == "stored"
            and row.origin == "text_input"
            and row.link_job_id is None
            and row.link_deadline_at is not None
            and row.link_deadline_at <= now
        )
        if not due:
            return False
        await jdb.retire_object(conn, t, now, ctx.ret, f, row, "abandoned", delay_s=ctx.ret.temporary_s)
        return True

    return await _each(ctx, await ctx.run(cands, write=False), fn, touched)


async def _mark_expired(ctx: fl.Ctx, n: int, touched: set[str]) -> int:
    t, f, j = ctx.t, ctx.t.stored_files, ctx.t.jobs

    async def cands(conn: AsyncConnection) -> list[tuple[str, str]]:
        now = await jdb.db_now(conn, ctx.backend)
        rows = (
            await conn.execute(
                sa.select(f.c.scope_id, f.c.file_id)
                .where(f.c.state == "stored", f.c.expires_at <= now)
                .order_by(f.c.file_id)
                .limit(n)
            )
        ).all()
        return [(r.scope_id, r.file_id) for r in rows]

    async def fn(conn: AsyncConnection, scope_id: str, file_id: str) -> bool:
        row = await fl.get_file_row(conn, t, scope_id, file_id, lock=True)
        now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
        if row is None or row.state != "stored" or row.expires_at is None or row.expires_at > now:
            return False
        await jdb.retire_object(conn, t, now, ctx.ret, f, row, "expired", delay_s=ctx.lim.purge_grace_s)
        return True

    total = await _each(ctx, await ctx.run(cands, write=False), fn, touched)

    async def jcands(conn: AsyncConnection) -> list[tuple[str, str]]:
        now = await jdb.db_now(conn, ctx.backend)
        rows = (
            await conn.execute(
                sa.select(j.c.scope_id, j.c.job_id)
                .where(j.c.data_state == "available", j.c.result_expires_at <= now)
                .order_by(j.c.job_id)
                .limit(n)
            )
        ).all()
        return [(r.scope_id, r.job_id) for r in rows]

    async def jfn(conn: AsyncConnection, scope_id: str, job_id: str) -> bool:
        row = (
            await conn.execute(sa.select(j).where(j.c.scope_id == scope_id, j.c.job_id == job_id).with_for_update())
        ).first()
        now = await jdb.db_now(conn, ctx.backend)  # decision time: after the row lock
        if row is None or row.data_state != "available" or row.result_expires_at is None or row.result_expires_at > now:
            return False
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
            await jdb.retire_object(conn, t, now, ctx.ret, ar, a, "expired", delay_s=ctx.lim.purge_grace_s)
        await conn.execute(
            sa.update(j)
            .where(j.c.scope_id == scope_id, j.c.job_id == job_id)
            .values(data_state="expired", updated_at=now)
        )
        return True

    return total + await _each(ctx, await ctx.run(jcands, write=False), jfn, touched)


# ---- physical purge (docs 4.3, 4.4) ------------------------------------------------------------------
_TERMINAL_DATA = ("abandoned", "deleted", "expired", "lost", "discarded")


async def _purge_physical(ctx: fl.Ctx, n: int, counts: dict[str, int], diag: dict[str, int], touched: set[str]) -> None:
    storage = ctx.storage if ctx.e.storage is not None else None
    if storage is None:
        return
    t = ctx.t
    for table, idcol in (
        (t.stored_files, "file_id"),
        (t.job_artifacts, "artifact_id"),
        (t.storage_tombstones, "tombstone_id"),
    ):
        tomb = table is t.storage_tombstones
        extra: list[Any] = (
            []
            if tomb
            else [
                table.c.state.in_(_TERMINAL_DATA),
                table.c.physically_deleted_at.is_(None),
                table.c.storage_key.is_not(None),
            ]
        )

        async def cands(
            conn: AsyncConnection, table: sa.Table = table, idcol: str = idcol, extra: list[Any] = extra
        ) -> list[tuple[str, str]]:
            now = await jdb.db_now(conn, ctx.backend)
            q = sa.select(table.c.scope_id, getattr(table.c, idcol)).where(table.c.delete_after <= now, *extra)
            rows = (await conn.execute(q.order_by(getattr(table.c, idcol)).limit(n))).all()
            return [(r[0], r[1]) for r in rows]

        for scope_id, rid in await ctx.run(cands, write=False):
            token = secrets.token_hex(8)

            async def claim(
                conn: AsyncConnection,
                table: sa.Table = table,
                idcol: str = idcol,
                extra: list[Any] = extra,
                scope_id: str = scope_id,
                rid: str = rid,
                token: str = token,
            ) -> Any:
                now = await jdb.db_now(conn, ctx.backend)
                res = await conn.execute(
                    sa.update(table)
                    .where(
                        table.c.scope_id == scope_id,
                        getattr(table.c, idcol) == rid,
                        table.c.delete_after <= now,
                        sa.or_(table.c.purge_token.is_(None), table.c.purge_claim_expires_at <= now),
                        *extra,
                    )
                    .values(purge_token=token, purge_claim_expires_at=jdb.plus(now, ctx.lim.purge_claim_ttl_s))
                )
                if res.rowcount != 1:
                    return None
                return (
                    await conn.execute(
                        sa.select(table).where(table.c.scope_id == scope_id, getattr(table.c, idcol) == rid)
                    )
                ).first()

            row = await ctx.run(claim)
            if row is None:
                continue
            await _purge_one(ctx, table, idcol, row, token, storage, counts, diag, touched)


async def _purge_one(
    ctx: fl.Ctx,
    table: sa.Table,
    idcol: str,
    row: Any,
    token: str,
    storage: Any,
    counts: dict[str, int],
    diag: dict[str, int],
    touched: set[str],
) -> None:
    t = ctx.t
    key = row.storage_key
    rid = getattr(row, idcol)
    unknown = row.write_state == "unknown"
    seal_now = False
    try:
        if unknown:
            await storage.delete_and_seal(key)
            seal_now = row.seal_confirmed_at is None
            released = await storage.writers_released(key)
        else:
            await storage.delete(key)
            info = await storage.stat(key)
            released = info is None
    except StorageError:
        diag["purge_pending_backend_error"] = diag.get("purge_pending_backend_error", 0) + 1
        await _release_claim(ctx, table, idcol, row, token)
        return
    except NotImplementedError:
        diag["purge_pending_backend_error"] = diag.get("purge_pending_backend_error", 0) + 1
        return

    async def reg(conn: AsyncConnection) -> bool:
        t0 = await jdb.db_now(conn, ctx.backend)  # bookkeeping only
        await jdb.lock_scope(conn, t, row.scope_id, exclusive=False)
        await jdb.lock_usage(conn, t, row.scope_id, t0)
        cur = (
            await conn.execute(
                sa.select(table.c.purge_token, table.c.purge_claim_expires_at, table.c.seal_confirmed_at)
                .where(table.c.scope_id == row.scope_id, getattr(table.c, idcol) == rid)
                .with_for_update()
            )
        ).first()
        now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
        if (
            cur is None
            or cur.purge_token != token
            or cur.purge_claim_expires_at is None
            or cur.purge_claim_expires_at <= now
        ):
            return False  # this cleaner lost possession: storage ops are idempotent and never retract a seal
        deltas: dict[str, int] = {}
        vals: dict[str, Any] = {}
        if seal_now and cur.seal_confirmed_at is None:
            vals["seal_confirmed_at"] = now
            deltas.update(pending_seals=-1, sealed_keys=1)
        if released:
            if table is t.storage_tombstones:
                await conn.execute(
                    sa.delete(table).where(table.c.scope_id == row.scope_id, getattr(table.c, idcol) == rid)
                )
            else:
                vals.update(
                    writers_released_at=now, physically_deleted_at=now, purge_token=None, purge_claim_expires_at=None
                )
                await conn.execute(
                    sa.update(table)
                    .where(table.c.scope_id == row.scope_id, getattr(table.c, idcol) == rid)
                    .values(**vals)
                )
            deltas["uncertain_bytes"] = -int(row.charged_bytes)
        else:
            vals.update(purge_token=None, purge_claim_expires_at=None)
            await conn.execute(
                sa.update(table).where(table.c.scope_id == row.scope_id, getattr(table.c, idcol) == rid).values(**vals)
            )
        await jdb.bump(conn, t, row.scope_id, now, **deltas)
        return True

    ok = await ctx.run(reg)
    if ok and released:
        counts["purged_physically"] = counts.get("purged_physically", 0) + 1
        touched.add(row.scope_id)
    elif ok:
        diag["purge_pending_writer_active"] = diag.get("purge_pending_writer_active", 0) + 1
        touched.add(row.scope_id)


async def _release_claim(ctx: fl.Ctx, table: sa.Table, idcol: str, row: Any, token: str) -> None:
    async def fn(conn: AsyncConnection) -> None:
        await conn.execute(
            sa.update(table)
            .where(
                table.c.scope_id == row.scope_id,
                getattr(table.c, idcol) == getattr(row, idcol),
                table.c.purge_token == token,
            )
            .values(purge_token=None, purge_claim_expires_at=None)
        )

    await ctx.run(fn)


# ---- metadata purge (docs 13.3) --------------------------------------------------------------------------
async def _purge_metadata(ctx: fl.Ctx, n: int, touched: set[str]) -> int:
    t = ctx.t
    f, j, ar, tb = t.stored_files, t.jobs, t.job_artifacts, t.storage_tombstones
    total = 0

    async def jcands(conn: AsyncConnection) -> list[tuple[str, str]]:
        now = await jdb.db_now(conn, ctx.backend)
        rows = (
            await conn.execute(
                sa.select(j.c.scope_id, j.c.job_id)
                .where(j.c.state.in_(jdb.TERMINAL), j.c.metadata_expires_at <= now)
                .order_by(j.c.job_id)
                .limit(n)
            )
        ).all()
        return [(r.scope_id, r.job_id) for r in rows]

    for scope_id, job_id in await ctx.run(jcands, write=False):

        async def fn(conn: AsyncConnection, scope_id: str = scope_id, job_id: str = job_id) -> bool:
            await jdb.lock_scope(conn, t, scope_id, exclusive=False)
            await conn.execute(
                sa.delete(t.idempotency_records).where(
                    t.idempotency_records.c.scope_id == scope_id, t.idempotency_records.c.job_id == job_id
                )
            )
            row = (
                await conn.execute(sa.select(j).where(j.c.scope_id == scope_id, j.c.job_id == job_id).with_for_update())
            ).first()
            now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
            if (
                row is None
                or row.metadata_expires_at is None
                or row.metadata_expires_at > now
                or row.state not in jdb.TERMINAL
            ):
                return False
            arts = (
                await conn.execute(
                    sa.select(ar)
                    .where(ar.c.scope_id == scope_id, ar.c.job_id == job_id)
                    .order_by(ar.c.artifact_id)
                    .with_for_update()
                )
            ).all()
            for a in arts:
                await _to_tombstone(conn, ctx, a, a.artifact_id, now)
            await conn.execute(sa.delete(ar).where(ar.c.scope_id == scope_id, ar.c.job_id == job_id))
            await conn.execute(
                sa.delete(t.job_attempts).where(
                    t.job_attempts.c.scope_id == scope_id, t.job_attempts.c.job_id == job_id
                )
            )
            await conn.execute(sa.delete(j).where(j.c.scope_id == scope_id, j.c.job_id == job_id))
            return True

        if await ctx.run(fn):
            total += 1
            touched.add(scope_id)

    async def fcands(conn: AsyncConnection) -> list[tuple[str, str]]:
        now = await jdb.db_now(conn, ctx.backend)
        rows = (
            await conn.execute(
                sa.select(f.c.scope_id, f.c.file_id)
                .where(f.c.state.in_(("abandoned", "deleted", "expired", "lost")), f.c.file_metadata_expires_at <= now)
                .order_by(f.c.file_id)
                .limit(n)
            )
        ).all()
        return [(r.scope_id, r.file_id) for r in rows]

    for scope_id, file_id in await ctx.run(fcands, write=False):

        async def ffn(conn: AsyncConnection, scope_id: str = scope_id, file_id: str = file_id) -> bool:
            await jdb.lock_scope(conn, t, scope_id, exclusive=False)
            await conn.execute(
                sa.delete(t.idempotency_records).where(
                    t.idempotency_records.c.scope_id == scope_id, t.idempotency_records.c.file_id == file_id
                )
            )
            row = await fl.get_file_row(conn, t, scope_id, file_id, lock=True)
            now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
            if row is None or row.file_metadata_expires_at is None or row.file_metadata_expires_at > now:
                return False
            if row.storage_key is not None and row.physically_deleted_at is None:
                await _to_tombstone(conn, ctx, row, file_id, now)
            await conn.execute(
                sa.delete(t.file_validations).where(
                    t.file_validations.c.scope_id == scope_id, t.file_validations.c.file_id == file_id
                )
            )
            await conn.execute(sa.delete(f).where(f.c.scope_id == scope_id, f.c.file_id == file_id))
            return True

        if await ctx.run(ffn):
            total += 1
            touched.add(scope_id)
    _ = tb
    return total


async def _to_tombstone(conn: AsyncConnection, ctx: fl.Ctx, row: Any, rid: str, now: datetime) -> None:
    """Only the minimal intent survives (no content, hash or reference) until release is confirmed (E-A05-08)."""
    if row.storage_key is None or row.physically_deleted_at is not None:
        return
    await conn.execute(
        sa.insert(ctx.t.storage_tombstones).values(
            scope_id=row.scope_id,
            tombstone_id="tmb_" + uuid.uuid4().hex,
            storage_backend_id=row.storage_backend_id,
            storage_key=row.storage_key,
            write_state=row.write_state or "unknown",
            seal_confirmed_at=row.seal_confirmed_at,
            charged_bytes=row.charged_bytes,
            delete_after=row.delete_after or now,
            created_at=now,
        )
    )


# ---- admin_events purge (docs 13.6) -----------------------------------------------------------------------
async def purge_admin_events(
    engine: ContentEngine, *, older_than: datetime, batch_size: int | None = None, dry_run: bool = True
) -> PurgeSummary:
    ctx = fl.Ctx(engine)
    if not isinstance(older_than, datetime) or older_than.tzinfo is None:
        raise ValidationFailedError("older_than must be a timezone-aware datetime.", field="older_than")
    n = batch_size or ctx.lim.admin_events_purge_batch_size
    if isinstance(n, bool) or not (1 <= n <= 10_000):
        raise ValidationFailedError("Invalid batch_size.", field="batch_size")
    e = ctx.t.admin_events

    async def guard(conn: AsyncConnection) -> None:
        now = await jdb.db_now(conn, ctx.backend)
        # 60 s of tolerance between the operator's clock and the database clock (the cutoff is an operator input)
        if older_than > jdb.plus(now, -ctx.ret.admin_events_s + 60):
            raise ValidationFailedError(
                "The cutoff is more recent than the admin_events retention.", field="older_than"
            )

    await ctx.run(guard, write=False)

    async def stats(conn: AsyncConnection) -> Any:
        return (
            await conn.execute(
                sa.select(
                    sa.func.count(),
                    sa.func.min(e.c.occurred_at),
                    sa.func.max(e.c.occurred_at),
                    sa.func.min(e.c.event_id),
                    sa.func.max(e.c.event_id),
                ).where(e.c.occurred_at < older_than)
            )
        ).one()

    count, oldest, newest, lo, hi = await ctx.run(stats, write=False)
    if dry_run:
        return PurgeSummary(
            dry_run=True,
            matched=int(count),
            deleted=0,
            oldest_occurred_at=oldest,
            newest_occurred_at=newest,
            min_event_id=lo,
            max_event_id=hi,
        )
    deleted = 0
    while True:

        async def batch(conn: AsyncConnection) -> int:
            ids = (
                (
                    await conn.execute(
                        sa.select(e.c.event_id).where(e.c.occurred_at < older_than).order_by(e.c.event_id).limit(n)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                return 0
            res = await conn.execute(sa.delete(e).where(e.c.event_id.in_(ids)))
            return int(res.rowcount)

        got = await ctx.run(batch)
        deleted += got
        if got < n:
            break
    return PurgeSummary(
        dry_run=False,
        matched=int(count),
        deleted=deleted,
        oldest_occurred_at=oldest,
        newest_occurred_at=newest,
        min_event_id=lo,
        max_event_id=hi,
    )
