"""Trusted local worker surface: claim, heartbeat, execute, publish with fencing (docs 5-7). Never exposed by HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import secrets
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import (
    CapabilitiesNotConfiguredError,
    DatabaseUnavailableError,
    EngineError,
    InferenceFixtureNotFoundError,
    InferenceOutputInvalidError,
    InferenceTimeoutError,
    InferenceUnavailableError,
    IsolationUnavailableError,
    LimitExceededError,
    PayloadTooLargeError,
    StorageUnavailableError,
    StoredObjectUnavailableError,
)
from . import db as jdb
from . import files as fl
from .ident import validation_identity
from .models import ClaimedJob, JobRunOutcome
from .ops import requirements_key
from .setup import StorageError, StorageKeyExists, StorageLimitExceeded

if TYPE_CHECKING:
    from ..engine import ContentEngine

log = logging.getLogger("cawbase_content_engine.jobs")

RETRYABLE = {
    "inference_unavailable",
    "inference_timeout",
    "database_unavailable",
    "storage_unavailable",
    "lease_lost",
    "extraction_process_failed",
}


class LeaseLost(EngineError):
    """The worker no longer owns the attempt (docs 6.3): it stops and publishes nothing.

    An ``EngineError`` so that it crosses transaction boundaries unchanged; never reaches an API caller."""

    code = "lease_lost"
    http_status = None
    retryable = True


class JobFailure(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code, self.detail = code, detail


# Artifact roles of each operation (docs 6.5): exactly these, each registered with the same `kind`.
EXPECTED_ARTIFACTS: dict[str, frozenset[str]] = {
    "text_analysis": frozenset({"result"}),
    "document_analysis": frozenset({"result", "source_map"}),
}


def backoff(lim: Any, attempt_no: int) -> float:
    d = min(lim.backoff_base_s * (2 ** (attempt_no - 1)), lim.backoff_max_s)
    return random.uniform(d / 2, d)  # noqa: S311  (jitter, not security)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class JobWorker:
    def __init__(self, engine: ContentEngine, worker_id: str) -> None:
        self.e = engine
        self.worker_id = worker_id
        self.ctx = fl.Ctx(engine)

    # ---- claim (T2) ------------------------------------------------------------------------------
    def supported_keys(self) -> list[str]:
        keys = []
        if self.e.inference is not None and self.e.storage is not None:
            keys.append(requirements_key(self.e, "text_analysis"))
            if self.e.documents is not None:
                keys.append(requirements_key(self.e, "document_analysis"))
        return keys

    async def claim(self, limit: int = 1) -> list[ClaimedJob]:
        from .maintenance import recover_expired_leases

        ctx = self.ctx
        keys = self.supported_keys()
        if not keys:
            return []
        await recover_expired_leases(self.e, limit=limit)
        t = ctx.t

        async def candidates(conn: AsyncConnection) -> list[tuple[str, str]]:
            now = await jdb.db_now(conn, ctx.backend)
            j = t.jobs
            rows = (
                await conn.execute(
                    sa.select(j.c.scope_id, j.c.job_id)
                    .where(
                        j.c.state.in_(("queued", "retry_wait")), j.c.available_at <= now, j.c.requirements_key.in_(keys)
                    )
                    .order_by(j.c.available_at, j.c.accepted_at, j.c.job_id)
                    .limit(max(limit * 4, 8))
                )
            ).all()
            return [(r.scope_id, r.job_id) for r in rows]

        out: list[ClaimedJob] = []
        for scope_id, job_id in await ctx.run(candidates, write=False):
            got = await self._claim_one(scope_id, job_id)
            if got is not None:
                out.append(got)
                if len(out) >= limit:
                    break
        return out

    async def _claim_one(self, scope_id: str, job_id: str) -> ClaimedJob | None:
        ctx = self.ctx
        for attempt in (1, 2):
            try:
                return await ctx.run(lambda c: self._claim_tx(c, scope_id, job_id))  # type: ignore[no-any-return]
            except jdb.NeedReconcile:
                if attempt == 2:
                    return None
                from .ops import reconcile_scope

                await ctx.run(lambda c: reconcile_scope(c, ctx, scope_id))
        return None

    async def _claim_tx(self, conn: AsyncConnection, scope_id: str, job_id: str) -> ClaimedJob | None:
        """Lock order of docs 6.2: scope(1) -> fence(2) -> usage(3,4) -> inputs(6) -> job(8, SKIP LOCKED).
        The job row is read first WITHOUT a lock only to learn which inputs to lock (non-authoritative); everything
        is revalidated after the job row is locked and the DB clock is read after every wait."""
        ctx = self.ctx
        t, lim, ret = ctx.t, ctx.lim, ctx.ret
        j = t.jobs
        t0 = await jdb.db_now(conn, ctx.backend)  # bookkeeping columns only; never a decision
        scope = await jdb.lock_scope(conn, t, scope_id, exclusive=False)  # 1
        fence = await jdb.read_fence(conn, t, scope_id)  # 2
        if fence is None or fence.observed_scope_revision != scope.revision:
            raise jdb.NeedReconcile()
        s, _e = await jdb.lock_usage(conn, t, scope_id, t0)  # 3, 4
        peek = (
            await conn.execute(
                sa.select(j.c.state, j.c.execution_snapshot, j.c.execution_snapshot_hash).where(
                    j.c.scope_id == scope_id, j.c.job_id == job_id
                )
            )
        ).first()
        if peek is None or peek.state not in ("queued", "retry_wait"):
            return None
        locked = await self._lock_inputs(conn, scope_id, peek)  # 6, before the job row
        row = (
            await conn.execute(
                sa.select(j)
                .where(j.c.scope_id == scope_id, j.c.job_id == job_id, j.c.state.in_(("queued", "retry_wait")))
                .with_for_update(skip_locked=True)  # 8
            )
        ).first()
        if row is None or row.execution_snapshot_hash != peek.execution_snapshot_hash:
            return None
        now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
        if row.available_at > now:
            return None
        if scope.status != "active" or row.fence_generation != fence.disable_generation:
            await jdb.terminate_job(
                conn, t, now, ret, row, state="cancelled", cancel_reason="scope_disabled", outcome="cancelled"
            )
            return None
        if row.deadline_at <= now:
            await jdb.terminate_job(
                conn, t, now, ret, row, state="failed", outcome="failed", failure_code="deadline_exceeded"
            )
            return None
        if row.cancel_requested_at is not None:
            return None
        if not self._inputs_valid(locked, now):
            await jdb.terminate_job(
                conn, t, now, ret, row, state="failed", outcome="failed", failure_code="input_unavailable"
            )
            return None
        if s.running_jobs + 1 > lim.max_running_jobs_per_scope:
            return None
        attempt_no = row.attempt_no + 1
        token = secrets.token_hex(16)
        attempt_deadline = min(jdb.plus(now, lim.max_attempt_duration_s), row.deadline_at)
        lease = min(jdb.plus(now, lim.lease_ttl_s), attempt_deadline)
        await conn.execute(
            sa.update(j)
            .where(j.c.scope_id == scope_id, j.c.job_id == job_id)
            .values(
                state="running",
                attempt_no=attempt_no,
                lease_token=token,
                lease_owner=self.worker_id,
                attempt_started_at=now,
                attempt_deadline_at=attempt_deadline,
                lease_expires_at=lease,
                heartbeat_at=now,
                started_at=row.started_at or now,
                updated_at=now,
            )
        )
        await conn.execute(
            sa.insert(t.job_attempts).values(
                scope_id=scope_id,
                job_id=job_id,
                attempt_no=attempt_no,
                worker_id=self.worker_id,
                lease_token_hash=token_hash(token),
                attempt_started_at=now,
                attempt_deadline_at=attempt_deadline,
            )
        )
        await jdb.bump(conn, t, scope_id, now, running_jobs=1)
        return ClaimedJob(
            job_id=job_id,
            scope_id=scope_id,
            attempt_no=attempt_no,
            lease_token=token,
            lease_expires_at=lease,
            attempt_deadline_at=attempt_deadline,
            phase=row.phase,
        )

    async def _lock_inputs(self, conn: AsyncConnection, scope_id: str, job: Any) -> list[tuple[dict[str, Any], Any]]:
        """Level 6: takes the shared lock on the principal input and the context file named by the pinned snapshot
        (immutable, so reading it unlocked is safe). Validation against a clock happens later, after every wait."""
        t = self.ctx.t
        snap = (
            job.execution_snapshot
            if not isinstance(job.execution_snapshot, str)
            else json.loads(job.execution_snapshot)
        )
        out: list[tuple[dict[str, Any], Any]] = []
        for part in ("input", "context"):
            ref = snap.get(part)
            if not ref:
                continue
            f = (
                await conn.execute(
                    sa.select(t.stored_files)
                    .where(t.stored_files.c.scope_id == scope_id, t.stored_files.c.file_id == ref["file_id"])
                    .with_for_update(read=True)
                )
            ).first()
            out.append((ref, f))
        return out

    @staticmethod
    def _inputs_valid(locked: list[tuple[dict[str, Any], Any]], now: datetime) -> bool:
        for ref, f in locked:
            if f is None or f.state != "stored" or f.expires_at is None or f.expires_at <= now:
                return False
            if f.sha256 is None or bytes(f.sha256).hex() != ref["sha256"] or f.size_bytes != ref["size_bytes"]:
                return False
        return True

    # ---- run -------------------------------------------------------------------------------------
    async def run_job(self, job_id: str, scope_id: str | None = None) -> JobRunOutcome:
        """Claims THAT job if eligible and runs it to its outcome (the host's own worker entry point)."""
        ctx = self.ctx
        if scope_id is None:

            async def find(conn: AsyncConnection) -> str | None:
                return (
                    await conn.execute(sa.select(ctx.t.jobs.c.scope_id).where(ctx.t.jobs.c.job_id == job_id))
                ).scalar_one_or_none()

            scope_id = await ctx.run(find, write=False)
        if scope_id is None:
            return JobRunOutcome(job_id=job_id, state="unknown", attempt_no=0, outcome_code="not_found")
        claimed = await self._claim_one(scope_id, job_id)
        if claimed is None:
            row = await self._state(scope_id, job_id)
            return JobRunOutcome(job_id=job_id, state=row[0], attempt_no=row[1], outcome_code="not_claimed")
        return await self.run_claimed(claimed)

    async def _state(self, scope_id: str, job_id: str) -> tuple[str, int]:
        async def fn(conn: AsyncConnection) -> tuple[str, int]:
            r = (
                await conn.execute(
                    sa.select(self.ctx.t.jobs.c.state, self.ctx.t.jobs.c.attempt_no).where(
                        self.ctx.t.jobs.c.scope_id == scope_id, self.ctx.t.jobs.c.job_id == job_id
                    )
                )
            ).first()
            return (r.state, r.attempt_no) if r else ("unknown", 0)

        return await self.ctx.run(fn, write=False)  # type: ignore[no-any-return]

    async def run_claimed(self, claimed: ClaimedJob) -> JobRunOutcome:
        from .execute import execute_job

        flags: dict[str, bool] = {"lost": False, "cancel": False}
        work = asyncio.ensure_future(execute_job(self, claimed))
        hb = asyncio.ensure_future(self._heartbeat(claimed, work, flags))
        outcome = "succeeded"
        code: str | None = None
        try:
            outcome = await work
        except asyncio.CancelledError:
            if flags["lost"]:
                outcome, code = "lease_lost", "lease_lost"
            elif flags["cancel"]:
                outcome = await self._finish_failure(claimed, JobFailure("cancelled"), cancelled=True)
            else:
                raise
        except LeaseLost:
            outcome, code = "lease_lost", "lease_lost"
        except Exception as exc:
            outcome = await self._finish_failure(claimed, _map_error(exc), cancelled=False)
        finally:
            hb.cancel()
            try:
                await hb
            except (asyncio.CancelledError, Exception):
                pass
        state, attempt = await self._state(claimed.scope_id, claimed.job_id)
        return JobRunOutcome(
            job_id=claimed.job_id, state=state, attempt_no=claimed.attempt_no, outcome_code=code or outcome
        )

    async def _heartbeat(self, c: ClaimedJob, work: asyncio.Future[Any], flags: dict[str, bool]) -> None:
        ctx, t = self.ctx, self.ctx.t
        while True:
            await asyncio.sleep(ctx.lim.heartbeat_interval_s)
            try:
                res = await ctx.run(lambda conn: self._beat(conn, c))
            except Exception:
                log.warning("heartbeat_failed", extra={"job_id": c.job_id})  # transient: the lease protects us
                continue
            if res == "lost":
                flags["lost"] = True
                work.cancel()
                return
            if res == "cancel":
                flags["cancel"] = True
                work.cancel()
                return
        _ = t

    async def _beat(self, conn: AsyncConnection, c: ClaimedJob, phase: str | None = None) -> str:
        """Heartbeat / phase change. The row is locked FIRST and the DB clock read AFTER the wait, so a lease that
        expired while this call waited for the lock can never be extended (docs 6.3)."""
        t = self.ctx.t
        j = t.jobs
        row: Any = (
            await conn.execute(sa.select(j).where(j.c.scope_id == c.scope_id, j.c.job_id == c.job_id).with_for_update())
        ).first()  # 8
        now = await jdb.db_now(conn, self.ctx.backend)  # decision time: after every wait
        if not self._owner_ok(row, c, now) or row.attempt_deadline_at is None:
            return "lost"
        vals: dict[str, Any] = {
            "heartbeat_at": now,
            "updated_at": now,
            # the lease may never exceed the attempt deadline (docs 6.3)
            "lease_expires_at": min(jdb.plus(now, self.ctx.lim.lease_ttl_s), row.attempt_deadline_at),
        }
        if phase:
            vals["phase"] = phase
        await conn.execute(sa.update(j).where(j.c.scope_id == c.scope_id, j.c.job_id == c.job_id).values(**vals))
        return "cancel" if row.cancel_requested_at is not None else "ok"

    async def set_phase(self, c: ClaimedJob, phase: str) -> None:
        res = await self.ctx.run(lambda conn: self._beat(conn, c, phase))
        if res == "lost":
            raise LeaseLost
        if res == "cancel":
            raise asyncio.CancelledError

    # ---- failure / retry / cancellation (T6, T7, T8) ---------------------------------------------
    async def _finish_failure(self, c: ClaimedJob, failure: JobFailure, *, cancelled: bool) -> str:
        ctx = self.ctx

        async def fn(conn: AsyncConnection) -> str:
            t, ret = ctx.t, ctx.ret
            t0 = await jdb.db_now(conn, ctx.backend)  # only for bookkeeping columns of the locks below
            await jdb.lock_scope(conn, t, c.scope_id, exclusive=False)
            await jdb.lock_usage(conn, t, c.scope_id, t0)
            j = t.jobs
            row: Any = (
                await conn.execute(
                    sa.select(j).where(j.c.scope_id == c.scope_id, j.c.job_id == c.job_id).with_for_update()
                )
            ).first()
            now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
            if not self._owner_ok(row, c, now):
                return "lease_lost"  # the owner no longer decides; recovery will
            if cancelled or row.cancel_requested_at is not None:
                await jdb.terminate_job(
                    conn,
                    t,
                    now,
                    ret,
                    row,
                    state="cancelled",
                    outcome="cancelled",
                    cancel_reason=row.cancel_reason or "user_requested",
                )
                return "cancelled"
            return await self._decide(conn, row, failure, now)

        return await ctx.run(fn)  # type: ignore[no-any-return]

    async def _decide(self, conn: AsyncConnection, row: Any, failure: JobFailure, now: datetime) -> str:
        ctx = self.ctx
        t, lim, ret = ctx.t, ctx.lim, ctx.ret
        code = failure.code
        if code in RETRYABLE:
            delay = backoff(lim, row.attempt_no)
            if row.attempt_no >= row.max_attempts:
                await jdb.terminate_job(
                    conn,
                    t,
                    now,
                    ret,
                    row,
                    state="failed",
                    outcome="failed",
                    failure_code="retries_exhausted",
                    last_error_code=code,
                )
                return "failed"
            if jdb.plus(now, delay) >= row.deadline_at:
                await jdb.terminate_job(
                    conn,
                    t,
                    now,
                    ret,
                    row,
                    state="failed",
                    outcome="failed",
                    failure_code="deadline_exceeded",
                    last_error_code=code,
                )
                return "failed"
            await conn.execute(
                sa.update(t.jobs)
                .where(t.jobs.c.scope_id == row.scope_id, t.jobs.c.job_id == row.job_id)
                .values(
                    state="retry_wait",
                    available_at=jdb.plus(now, delay),
                    last_error_code=code,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            await conn.execute(
                sa.update(t.job_attempts)
                .where(
                    t.job_attempts.c.scope_id == row.scope_id,
                    t.job_attempts.c.job_id == row.job_id,
                    t.job_attempts.c.attempt_no == row.attempt_no,
                )
                .values(
                    ended_at=now, outcome="lease_lost" if code == "lease_lost" else "retryable_error", error_code=code
                )
            )
            await jdb.discard_artifacts(conn, t, now, ret, row.scope_id, row.job_id, keep=None)
            await jdb.bump(conn, t, row.scope_id, now, running_jobs=-1)
            return "retry_wait"
        await jdb.terminate_job(
            conn, t, now, ret, row, state="failed", outcome="failed", failure_code=code, last_error_code=failure.detail
        )
        return "failed"

    # ---- artifacts and publication ------------------------------------------------------------------
    def _owner_ok(self, row: Any, c: ClaimedJob, now: datetime) -> bool:
        return bool(
            row is not None
            and row.state == "running"
            and row.attempt_no == c.attempt_no
            and row.lease_token == c.lease_token
            and row.lease_expires_at is not None
            and row.lease_expires_at > now
            and row.attempt_deadline_at > now
            and row.deadline_at > now
        )

    async def write_artifact(self, c: ClaimedJob, kind: str, data: bytes, reserve_bytes: int) -> str:
        """Five-step protocol for one artifact (docs 4): never overwrites, key private to this intent."""
        ctx = self.ctx
        t, lim = ctx.t, ctx.lim
        artifact_id = "art_" + uuid.uuid4().hex
        box: dict[str, Any] = {}

        async def reserve(conn: AsyncConnection) -> None:
            t0 = await jdb.db_now(conn, ctx.backend)
            await jdb.lock_scope(conn, t, c.scope_id, exclusive=False)
            s, e = await jdb.lock_usage(conn, t, c.scope_id, t0)
            row: Any = (
                await conn.execute(
                    sa.select(t.jobs)
                    .where(t.jobs.c.scope_id == c.scope_id, t.jobs.c.job_id == c.job_id)
                    .with_for_update()
                )
            ).first()
            now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
            if not self._owner_ok(row, c, now):
                raise LeaseLost
            charge = reserve_bytes * ctx.factor
            jdb.check_storage(s, e, charge, lim)
            from .storage import new_key

            box["key"], box["abort"], box["charge"] = new_key(), row.attempt_deadline_at, charge
            await conn.execute(
                sa.insert(t.job_artifacts).values(
                    scope_id=c.scope_id,
                    artifact_id=artifact_id,
                    job_id=c.job_id,
                    attempt_no=c.attempt_no,
                    kind=kind,
                    state="reserved",
                    storage_backend_id=ctx.backend_id,
                    storage_key=box["key"],
                    write_state="unknown",
                    reserved_bytes=charge,
                    abort_after=row.attempt_deadline_at,
                    reserved_at=now,
                )
            )
            await jdb.bump(conn, t, c.scope_id, now, reserved_bytes=charge, open_reservations=1)

        await ctx.run(reserve)

        async def one() -> Any:
            yield data

        try:
            receipt = await ctx.storage.put_new(box["key"], one(), max_bytes=reserve_bytes, abort_after=box["abort"])
            info = await ctx.storage.stat(box["key"])
            if info is None or info.size != receipt.size or receipt.size != len(data):
                raise StoredObjectUnavailableError()
        except StorageLimitExceeded:
            raise LimitExceededError("The result exceeds its limit.") from None
        except StorageKeyExists:
            raise StorageUnavailableError() from None
        except StorageError:
            raise StorageUnavailableError() from None

        async def register(conn: AsyncConnection) -> None:
            t0 = await jdb.db_now(conn, ctx.backend)
            await jdb.lock_scope(conn, t, c.scope_id, exclusive=False)
            await jdb.lock_usage(conn, t, c.scope_id, t0)
            row: Any = (
                await conn.execute(
                    sa.select(t.jobs)
                    .where(t.jobs.c.scope_id == c.scope_id, t.jobs.c.job_id == c.job_id)
                    .with_for_update()
                )
            ).first()
            a = t.job_artifacts
            art = (
                await conn.execute(
                    sa.select(a).where(a.c.scope_id == c.scope_id, a.c.artifact_id == artifact_id).with_for_update()
                )
            ).one()  # 9
            now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
            if not self._owner_ok(row, c, now):
                raise LeaseLost  # the row stays `reserved`: cleanup seals and releases it
            if art.state != "reserved":
                raise LeaseLost
            await conn.execute(
                sa.update(a)
                .where(a.c.scope_id == c.scope_id, a.c.artifact_id == artifact_id)
                .values(
                    state="written",
                    write_state="completed",
                    size_bytes=receipt.size,
                    sha256=receipt.sha256,
                    written_at=now,
                )
            )
            await jdb.bump(
                conn,
                t,
                c.scope_id,
                now,
                reserved_bytes=-int(art.reserved_bytes),
                stored_bytes=receipt.size,
                open_reservations=-1,
            )

        await ctx.run(register)
        return artifact_id

    async def publish(self, c: ClaimedJob, artifact_ids: dict[str, str]) -> str:
        """T3 with every precondition of docs 6.5. Returns the outcome; state changes made here are committed."""
        ctx = self.ctx

        async def fn(conn: AsyncConnection) -> str:
            t, ret = ctx.t, ctx.ret
            t0 = await jdb.db_now(conn, ctx.backend)  # bookkeeping columns only; never a decision
            scope = await jdb.lock_scope(conn, t, c.scope_id, exclusive=False)  # 1
            fence = await jdb.read_fence(conn, t, c.scope_id)  # 2
            if fence is None or fence.observed_scope_revision != scope.revision:
                raise jdb.NeedReconcile()
            await jdb.lock_usage(conn, t, c.scope_id, t0)  # 3, 4
            j = t.jobs
            peek = (await conn.execute(sa.select(j).where(j.c.scope_id == c.scope_id, j.c.job_id == c.job_id))).first()
            if peek is None:
                return "lease_lost"
            # level 6 BEFORE the job (8): inputs are locked shared so delete_file / expiry serialize with us
            locked = await self._lock_inputs(conn, c.scope_id, peek)
            row: Any = (
                await conn.execute(
                    sa.select(j).where(j.c.scope_id == c.scope_id, j.c.job_id == c.job_id).with_for_update()
                )
            ).first()  # 8
            a = t.job_artifacts
            arts = (
                await conn.execute(
                    sa.select(a)
                    .where(a.c.scope_id == c.scope_id, a.c.artifact_id.in_(list(artifact_ids.values())))
                    .order_by(a.c.artifact_id)
                    .with_for_update()
                )
            ).all()  # 9
            now = await jdb.db_now(conn, ctx.backend)  # decision time: after EVERY lock wait
            if not self._owner_ok(row, c, now):
                return "lease_lost"
            if row.cancel_requested_at is not None:
                await jdb.terminate_job(
                    conn,
                    t,
                    now,
                    ret,
                    row,
                    state="cancelled",
                    outcome="cancelled",
                    cancel_reason=row.cancel_reason or "user_requested",
                )
                return "cancelled"
            if scope.status != "active" or row.fence_generation != fence.disable_generation:
                await jdb.terminate_job(
                    conn, t, now, ret, row, state="cancelled", outcome="cancelled", cancel_reason="scope_disabled"
                )
                return "cancelled"
            validation_ok = True
            if row.validation_identity:
                vr = (
                    await conn.execute(
                        sa.select(t.file_validations.c.result).where(
                            t.file_validations.c.scope_id == c.scope_id,
                            t.file_validations.c.file_id == row.input_file_id,
                            t.file_validations.c.validation_identity == row.validation_identity,
                        )
                    )
                ).scalar_one_or_none()
                validation_ok = vr == "passed"
            if not self._inputs_valid(locked, now) or not validation_ok:
                await jdb.terminate_job(
                    conn, t, now, ret, row, state="failed", outcome="failed", failure_code="input_unavailable"
                )
                return "failed"
            expected = EXPECTED_ARTIFACTS.get(row.operation)
            ids = list(artifact_ids.values())
            by_id = {x.artifact_id: x for x in arts}
            if (
                expected is None
                or set(artifact_ids) != expected  # exactly the roles of the operation
                or len(set(ids)) != len(ids)  # no artifact in two roles
                or len(by_id) != len(ids)  # every reference exists
                or any(
                    x.state != "written"
                    or x.attempt_no != c.attempt_no
                    or x.job_id != c.job_id
                    or x.kind != role  # registered type = role
                    or x.sha256 is None
                    or x.size_bytes is None
                    for role, aid in artifact_ids.items()
                    for x in (by_id[aid],)
                )
            ):
                # An executor bug, not a transient condition: nothing is published, the attempt's artifacts are
                # discarded and the job fails without a retry (a retry would repeat the same wrong set).
                await jdb.terminate_job(
                    conn, t, now, ret, row, state="failed", outcome="failed", failure_code="internal_error"
                )
                return "failed"
            expires = jdb.plus(now, row.result_retention_s)
            for x in arts:
                await conn.execute(
                    sa.update(a)
                    .where(a.c.scope_id == c.scope_id, a.c.artifact_id == x.artifact_id)
                    .values(state="published", expires_at=expires)
                )
            await conn.execute(
                sa.update(j)
                .where(j.c.scope_id == c.scope_id, j.c.job_id == c.job_id)
                .values(
                    state="succeeded",
                    phase="publication",
                    terminated_at=now,
                    result_artifact_id=artifact_ids.get("result"),
                    source_map_artifact_id=artifact_ids.get("source_map"),
                    published_attempt_no=c.attempt_no,
                    data_state="available",
                    result_expires_at=expires,
                    metadata_expires_at=jdb.plus(now, row.metadata_retention_s),
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            await conn.execute(
                sa.update(t.job_attempts)
                .where(
                    t.job_attempts.c.scope_id == c.scope_id,
                    t.job_attempts.c.job_id == c.job_id,
                    t.job_attempts.c.attempt_no == c.attempt_no,
                )
                .values(ended_at=now, outcome="succeeded")
            )
            await jdb.discard_artifacts(conn, t, now, ret, c.scope_id, c.job_id, keep=set(artifact_ids.values()))
            await jdb.bump(conn, t, c.scope_id, now, active_jobs=-1, running_jobs=-1)
            return "succeeded"

        for attempt in (1, 2):
            try:
                return await ctx.run(fn)  # type: ignore[no-any-return]
            except jdb.NeedReconcile:
                if attempt == 2:
                    raise DatabaseUnavailableError() from None
                from .ops import reconcile_scope

                await ctx.run(lambda conn: reconcile_scope(conn, ctx, c.scope_id))
        raise AssertionError

    async def record_validation(
        self, c: ClaimedJob, file_id: str, identity: str, result: str, reason: str | None
    ) -> None:
        """Only the current owner may register a validation (docs 10.7); the first result per identity is final."""
        ctx = self.ctx

        async def fn(conn: AsyncConnection) -> None:
            t = ctx.t
            await jdb.lock_scope(conn, t, c.scope_id, exclusive=False)
            row: Any = (
                await conn.execute(
                    sa.select(t.jobs)
                    .where(t.jobs.c.scope_id == c.scope_id, t.jobs.c.job_id == c.job_id)
                    .with_for_update()
                )
            ).first()
            now = await jdb.db_now(conn, ctx.backend)  # decision time: after every lock wait
            if not self._owner_ok(row, c, now):
                raise LeaseLost
            from .. import validation as _v  # noqa: F401
            from ..operations.common import insert_ignore
            from .ident import VALIDATOR_ID, VALIDATOR_VERSION, config_sha

            inserted = await insert_ignore(
                conn,
                t,
                t.file_validations,
                {
                    "scope_id": c.scope_id,
                    "file_id": file_id,
                    "validation_identity": identity,
                    "validator_id": VALIDATOR_ID,
                    "validator_version": VALIDATOR_VERSION,
                    "validator_config_sha256": config_sha(ctx.lim),
                    "result": result,
                    "reason": reason,
                    "job_id": c.job_id,
                    "attempt_no": c.attempt_no,
                    "validated_at": now,
                },
            )
            if not inserted:
                prev = (
                    await conn.execute(
                        sa.select(t.file_validations.c.result).where(
                            t.file_validations.c.scope_id == c.scope_id,
                            t.file_validations.c.file_id == file_id,
                            t.file_validations.c.validation_identity == identity,
                        )
                    )
                ).scalar_one()
                if prev != result:
                    log.warning("validation_divergence", extra={"job_id": c.job_id})

        await ctx.run(fn)


def _map_error(exc: BaseException) -> JobFailure:
    if isinstance(exc, JobFailure):
        return exc
    if isinstance(exc, InferenceUnavailableError):
        return JobFailure("inference_unavailable")
    if isinstance(exc, InferenceTimeoutError):
        return JobFailure("inference_timeout")
    if isinstance(exc, InferenceOutputInvalidError):
        return JobFailure("inference_output_invalid")
    if isinstance(exc, InferenceFixtureNotFoundError):
        return JobFailure("inference_fixture_not_found")
    if isinstance(exc, CapabilitiesNotConfiguredError):
        return JobFailure("capabilities_not_configured")
    if isinstance(exc, DatabaseUnavailableError):
        return JobFailure("database_unavailable")
    if isinstance(exc, StorageUnavailableError):
        return JobFailure("storage_unavailable")
    if isinstance(exc, (StoredObjectUnavailableError, fl._Lost)):
        return JobFailure("stored_object_corrupt")
    if isinstance(exc, IsolationUnavailableError):
        return JobFailure("configuration_unavailable")
    if isinstance(exc, (LimitExceededError, PayloadTooLargeError)):
        return JobFailure("limit_exceeded")
    return JobFailure("internal_error")


_ = (validation_identity,)
