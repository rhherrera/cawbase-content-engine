"""Database primitives shared by every A05 operation: clock, lock order, counters, fence, terminal transitions.

Lock order (docs/jobs-files-and-lifecycle.md, 6.2), strictly ascending within one transaction:
1 scopes, 2 scope_job_fences, 3 scope_usage, 4 engine_usage, 5 idempotency_records, 6 stored_files,
7 file_validations, 8 jobs, 9 job_attempts/job_artifacts, 10 storage_tombstones.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import EngineError, QuotaExceededError, ResourceNotAccessibleError
from ..operations.common import insert_ignore
from ..persistence.dialect import Backend
from ..persistence.tables import Tables
from .setup import JobsLimits, Retention

TERMINAL = ("succeeded", "failed", "cancelled")
NON_TERMINAL = ("queued", "running", "retry_wait")
COUNTERS = (
    "reserved_bytes",
    "stored_bytes",
    "uncertain_bytes",
    "open_reservations",
    "pending_seals",
    "sealed_keys",
    "active_jobs",
    "running_jobs",
)


def sha256_hex(*parts: bytes | str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def db_now(conn: AsyncConnection, backend: Backend) -> datetime:
    """The database clock is the only authority for lease, deadline and expiry decisions (E-A05-04)."""
    if backend is Backend.POSTGRESQL:
        value = (await conn.execute(sa.text("SELECT clock_timestamp()"))).scalar_one()
        return value.astimezone(UTC)  # type: ignore[no-any-return]
    raw = (await conn.execute(sa.text("SELECT strftime('%Y-%m-%d %H:%M:%f','now')"))).scalar_one()
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)


def plus(now: datetime, seconds: float) -> datetime:
    return now + timedelta(seconds=seconds)


# ---- level 1: scope ---------------------------------------------------------------------------
async def lock_scope(conn: AsyncConnection, t: Tables, scope_id: str, *, exclusive: bool) -> Any:
    q = sa.select(t.scopes.c.status, t.scopes.c.revision).where(t.scopes.c.scope_id == scope_id)
    q = q.with_for_update(key_share=True) if exclusive else q.with_for_update(read=True)
    row = (await conn.execute(q)).first()
    if row is None:
        raise ResourceNotAccessibleError()
    return row


# ---- levels 3-4: counters ---------------------------------------------------------------------
async def ensure_usage(conn: AsyncConnection, t: Tables, scope_id: str, now: datetime) -> None:
    await insert_ignore(conn, t, t.scope_usage, {"scope_id": scope_id, "updated_at": now})
    await insert_ignore(conn, t, t.engine_usage, {"id": 1, "updated_at": now})


async def lock_usage(conn: AsyncConnection, t: Tables, scope_id: str, now: datetime) -> tuple[Any, Any]:
    await ensure_usage(conn, t, scope_id, now)
    s = (
        await conn.execute(sa.select(t.scope_usage).where(t.scope_usage.c.scope_id == scope_id).with_for_update())
    ).one()
    e = (await conn.execute(sa.select(t.engine_usage).where(t.engine_usage.c.id == 1).with_for_update())).one()
    return s, e


async def bump(conn: AsyncConnection, t: Tables, scope_id: str, now: datetime, **deltas: int) -> None:
    """Counter changes always happen in the transaction that changes the state (usage rows already locked)."""
    deltas = {k: v for k, v in deltas.items() if v}
    if not deltas:
        return
    sv = {k: getattr(t.scope_usage.c, k) + v for k, v in deltas.items()}
    await conn.execute(
        sa.update(t.scope_usage).where(t.scope_usage.c.scope_id == scope_id).values(**sv, updated_at=now)
    )
    ev = {k: getattr(t.engine_usage.c, k) + v for k, v in deltas.items() if k in COUNTERS}
    if ev:
        await conn.execute(sa.update(t.engine_usage).where(t.engine_usage.c.id == 1).values(**ev, updated_at=now))


def storage_charge(row: Any) -> int:
    return int(row.reserved_bytes + row.stored_bytes + row.uncertain_bytes)


def check_storage(s: Any, e: Any, add: int, lim: JobsLimits, *, seal: bool = True) -> None:
    if (
        storage_charge(s) + add > lim.max_storage_bytes_per_scope
        or storage_charge(e) + add > lim.max_storage_bytes_total
    ):
        raise QuotaExceededError("Storage quota exceeded.")
    if seal:
        sc = s.sealed_keys + s.pending_seals + s.open_reservations + 1
        ec = e.sealed_keys + e.pending_seals + e.open_reservations + 1
        if sc > lim.max_seals_per_scope or ec > lim.max_seals_total:
            exc = QuotaExceededError("Seal capacity exceeded.")
            exc.reason = "seal_capacity"  # type: ignore[attr-defined]
            raise exc


# ---- level 2: fence (I1-I7) -------------------------------------------------------------------
class NeedReconcile(EngineError):
    """A shared-lock reader found the fence missing or diverged (I6): reconcile in a transaction of its own.

    Internal control flow: an ``EngineError`` so that ``translate`` lets it pass; it never reaches a caller."""

    code = "fence_reconcile_required"
    http_status = None


async def read_fence(conn: AsyncConnection, t: Tables, scope_id: str) -> Any:
    return (
        await conn.execute(
            sa.select(t.scope_job_fences).where(t.scope_job_fences.c.scope_id == scope_id).with_for_update()
        )
    ).first()


async def reconcile_locked(
    conn: AsyncConnection, t: Tables, scope_id: str, now: datetime, ret: Retention, *, create: bool = False
) -> Any:
    """Scope already locked exclusively. Reconcile BEFORE any other write (I2); idempotent (I4); lazy init (I5)."""
    sc = (await conn.execute(sa.select(t.scopes.c.revision).where(t.scopes.c.scope_id == scope_id))).one()
    fence = await read_fence(conn, t, scope_id)
    j = t.jobs
    if fence is None:
        maxg = (
            await conn.execute(sa.select(sa.func.max(j.c.fence_generation)).where(j.c.scope_id == scope_id))
        ).scalar_one()
        gen = 0 if maxg is None else maxg + 1
        if maxg is None and not create:
            return None
        await conn.execute(
            sa.insert(t.scope_job_fences).values(
                scope_id=scope_id, observed_scope_revision=sc.revision, disable_generation=gen, updated_at=now
            )
        )
        if maxg is not None:  # jobs of unknown generation are never treated as new
            await cancel_nonterminal(conn, t, scope_id, gen, now, ret)
        return await read_fence(conn, t, scope_id)
    if fence.observed_scope_revision != sc.revision:
        gen = fence.disable_generation + 1
        await conn.execute(
            sa.update(t.scope_job_fences)
            .where(t.scope_job_fences.c.scope_id == scope_id)
            .values(observed_scope_revision=sc.revision, disable_generation=gen, updated_at=now)
        )
        await cancel_nonterminal(conn, t, scope_id, gen, now, ret)
        return await read_fence(conn, t, scope_id)
    return fence


async def after_scope_mutation(
    conn: AsyncConnection, t: Tables, scope_id: str, now: datetime, ret: Retention, *, disabled: bool
) -> None:
    """After an effective A01 mutation (reconcile already ran): observe the new revision (I3)."""
    fence = await read_fence(conn, t, scope_id)
    if fence is None:
        return
    rev = (await conn.execute(sa.select(t.scopes.c.revision).where(t.scopes.c.scope_id == scope_id))).scalar_one()
    gen = fence.disable_generation + (1 if disabled else 0)
    await conn.execute(
        sa.update(t.scope_job_fences)
        .where(t.scope_job_fences.c.scope_id == scope_id)
        .values(observed_scope_revision=rev, disable_generation=gen, updated_at=now)
    )
    if disabled:
        await cancel_nonterminal(conn, t, scope_id, gen, now, ret)


async def cancel_nonterminal(
    conn: AsyncConnection, t: Tables, scope_id: str, generation: int, now: datetime, ret: Retention
) -> int:
    """T10: every non-terminal job of an older generation is cancelled (scope lock + fence held; usage next)."""
    await lock_usage(conn, t, scope_id, now)
    j = t.jobs
    rows = (
        await conn.execute(
            sa.select(j)
            .where(j.c.scope_id == scope_id, j.c.state.in_(NON_TERMINAL), j.c.fence_generation < generation)
            .order_by(j.c.job_id)
            .with_for_update()
        )
    ).all()
    for row in rows:
        await terminate_job(
            conn, t, now, ret, row, state="cancelled", cancel_reason="scope_disabled", outcome="cancelled"
        )
    return len(rows)


# ---- terminal transitions (T3 is in the worker) -----------------------------------------------
async def retire_object(
    conn: AsyncConnection,
    t: Tables,
    now: datetime,
    ret: Retention,
    table: sa.Table,
    row: Any,
    new_state: str,
    *,
    delay_s: float,
) -> None:
    """Moves a stored/reserved/written/published object to a terminal data state. The charge becomes *uncertain*
    until release is confirmed (section 12.3). Usage rows must be locked by the caller."""
    m = row._mapping
    sid, st = m["scope_id"], m["state"]
    is_file = table is t.stored_files
    values: dict[str, Any] = {"state": new_state, "abandoned_at": now, "delete_after": plus(now, delay_s)}
    has_storage = m["storage_key"] is not None
    d: dict[str, int] = {}
    if has_storage:
        if st == "reserved":
            charge = int(m["reserved_bytes"])
            d = {"reserved_bytes": -charge, "uncertain_bytes": charge, "open_reservations": -1, "pending_seals": 1}
            values["write_state"] = "unknown"
        else:
            charge = int(m["size_bytes"] or 0)
            d = {"stored_bytes": -charge, "uncertain_bytes": charge}
            values["write_state"] = "completed"
        values["charged_bytes"] = charge
    else:  # external reference: nothing physical to purge
        values["physically_deleted_at"] = now
        values["delete_after"] = None
    if is_file:
        values["end_of_life_at"] = now
        values["file_metadata_expires_at"] = plus(now, ret.metadata_s)
        if new_state in ("deleted",):
            values["deleted_at"] = now
        kwargs = dict(d)
        if st in ("reserved", "stored"):
            kwargs["live_files"] = -1
        await bump(conn, t, sid, now, **kwargs)
        pk = (table.c.scope_id == sid) & (table.c.file_id == m["file_id"])
    else:
        await bump(conn, t, sid, now, **d)
        pk = (table.c.scope_id == sid) & (table.c.artifact_id == m["artifact_id"])
    await conn.execute(sa.update(table).where(pk).values(**values))


async def terminate_job(
    conn: AsyncConnection,
    t: Tables,
    now: datetime,
    ret: Retention,
    job: Any,
    *,
    state: str,
    outcome: str,
    cancel_reason: str | None = None,
    failure_code: str | None = None,
    last_error_code: str | None = None,
) -> None:
    """Every terminal transition: timestamps, attempt closure, artifact discard and counters in ONE transaction.
    Preconditions: scope usage locked (levels 3-4) and the job row locked (level 8)."""
    m = job._mapping
    sid, jid = m["scope_id"], m["job_id"]
    j = t.jobs
    values: dict[str, Any] = {
        "state": state,
        "terminated_at": now,
        "metadata_expires_at": plus(now, m["metadata_retention_s"]),
        "lease_token": None,
        "lease_expires_at": None,
        "updated_at": now,
    }
    if cancel_reason:
        values["cancel_reason"] = cancel_reason
        if m["cancel_requested_at"] is None:
            values["cancel_requested_at"] = now
    if failure_code:
        values["failure_code"] = failure_code
    if last_error_code:
        values["last_error_code"] = last_error_code
    await conn.execute(sa.update(j).where(j.c.scope_id == sid, j.c.job_id == jid).values(**values))
    if m["attempt_no"] > 0:
        a = t.job_attempts
        await conn.execute(
            sa.update(a)
            .where(a.c.scope_id == sid, a.c.job_id == jid, a.c.attempt_no == m["attempt_no"], a.c.ended_at.is_(None))
            .values(ended_at=now, outcome=outcome, error_code=failure_code or last_error_code)
        )
    await discard_artifacts(conn, t, now, ret, sid, jid, keep=None)
    await bump(conn, t, sid, now, active_jobs=-1, running_jobs=-1 if m["state"] == "running" else 0)


async def discard_artifacts(
    conn: AsyncConnection,
    t: Tables,
    now: datetime,
    ret: Retention,
    scope_id: str,
    job_id: str,
    *,
    keep: set[str] | None,
) -> None:
    ar = t.job_artifacts
    rows = (
        await conn.execute(
            sa.select(ar)
            .where(ar.c.scope_id == scope_id, ar.c.job_id == job_id, ar.c.state.in_(("reserved", "written")))
            .order_by(ar.c.artifact_id)
            .with_for_update()
        )
    ).all()
    for r in rows:
        if keep and r.artifact_id in keep:
            continue
        await retire_object(conn, t, now, ret, ar, r, "discarded", delay_s=ret.temporary_s)


async def recompute_usage(conn: AsyncConnection, t: Tables, scope_id: str, now: datetime) -> None:
    """Rebuilds a scope's counters from its rows under the scope/engine usage locks (no concurrent change can be
    lost: every change takes the same locks). ``sealed_keys`` is preserved: sealed rows may already be purged."""
    s, _ = await lock_usage(conn, t, scope_id, now)
    f, ar, j, tb = t.stored_files, t.job_artifacts, t.jobs, t.storage_tombstones

    async def total(col: sa.ColumnElement[Any], where: sa.ColumnElement[bool], table: sa.Table) -> int:
        return int(
            (
                await conn.execute(sa.select(sa.func.coalesce(sa.func.sum(col), 0)).where(where).select_from(table))
            ).scalar_one()
        )

    async def count(where: sa.ColumnElement[bool], table: sa.Table) -> int:
        return int((await conn.execute(sa.select(sa.func.count()).where(where).select_from(table))).scalar_one())

    managed = f.c.storage_key.is_not(None)
    new = {
        "reserved_bytes": await total(
            f.c.reserved_bytes, (f.c.scope_id == scope_id) & (f.c.state == "reserved") & managed, f
        )
        + await total(ar.c.reserved_bytes, (ar.c.scope_id == scope_id) & (ar.c.state == "reserved"), ar),
        "stored_bytes": await total(f.c.size_bytes, (f.c.scope_id == scope_id) & (f.c.state == "stored") & managed, f)
        + await total(ar.c.size_bytes, (ar.c.scope_id == scope_id) & ar.c.state.in_(("written", "published")), ar),
        "uncertain_bytes": await total(
            f.c.charged_bytes,
            (f.c.scope_id == scope_id)
            & f.c.state.in_(("abandoned", "deleted", "expired", "lost"))
            & (f.c.physically_deleted_at.is_(None)),
            f,
        )
        + await total(
            ar.c.charged_bytes,
            (ar.c.scope_id == scope_id)
            & ar.c.state.in_(("discarded", "expired", "deleted", "lost"))
            & (ar.c.physically_deleted_at.is_(None)),
            ar,
        )
        + await total(tb.c.charged_bytes, tb.c.scope_id == scope_id, tb),
        "open_reservations": await count((f.c.scope_id == scope_id) & (f.c.state == "reserved") & managed, f)
        + await count((ar.c.scope_id == scope_id) & (ar.c.state == "reserved"), ar),
        "pending_seals": await count(
            (f.c.scope_id == scope_id)
            & (f.c.write_state == "unknown")
            & f.c.state.in_(("abandoned", "deleted", "expired", "lost"))
            & f.c.seal_confirmed_at.is_(None)
            & managed,
            f,
        )
        + await count(
            (ar.c.scope_id == scope_id)
            & (ar.c.write_state == "unknown")
            & (ar.c.state != "reserved")
            & ar.c.seal_confirmed_at.is_(None),
            ar,
        )
        + await count(
            (tb.c.scope_id == scope_id) & (tb.c.write_state == "unknown") & tb.c.seal_confirmed_at.is_(None), tb
        ),
        "active_jobs": await count((j.c.scope_id == scope_id) & j.c.state.in_(NON_TERMINAL), j),
        "running_jobs": await count((j.c.scope_id == scope_id) & (j.c.state == "running"), j),
    }
    live = await count((f.c.scope_id == scope_id) & f.c.state.in_(("reserved", "stored")), f)
    await conn.execute(
        sa.update(t.scope_usage)
        .where(t.scope_usage.c.scope_id == scope_id)
        .values(**new, live_files=live, updated_at=now)
    )
    # engine counters = sum of every scope's counters (sealed_keys included)
    sums = {k: sa.func.coalesce(sa.func.sum(getattr(t.scope_usage.c, k)), 0) for k in COUNTERS}
    row = (await conn.execute(sa.select(*[v.label(k) for k, v in sums.items()]))).one()
    await conn.execute(
        sa.update(t.engine_usage)
        .where(t.engine_usage.c.id == 1)
        .values(**{k: int(row._mapping[k]) for k in COUNTERS}, updated_at=now)
    )
    _ = s
