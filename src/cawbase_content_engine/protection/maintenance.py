"""Explicit, bounded, repeatable maintenance of A06 (docs/text-protection.md, 8.5 and 9.4). Needs no keys.

Never automatic: not at import, not at start. Each context is handled in its own short transaction. Nothing here
touches A05 (``run_cleanup`` and its summary are unchanged).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import ValidationFailedError
from ..jobs import db as jdb
from .ops import _context_row, _drop_context_data, _ensure_usage
from .types import DAY, KeyVersionUsage, ProtectionCleanupSummary

if TYPE_CHECKING:
    from ..engine import ContentEngine

log = logging.getLogger("cawbase_content_engine.protection")
DEFAULT_RETENTION_S = 30 * DAY
DEFAULT_BATCH = 200


def _settings(engine: ContentEngine, batch_size: int | None) -> tuple[int, int]:
    setup = engine.protection
    retention = setup.metadata_retention_s if setup else DEFAULT_RETENTION_S
    batch = batch_size if batch_size is not None else (setup.limits.cleanup_batch_size if setup else DEFAULT_BATCH)
    if not isinstance(batch, int) or isinstance(batch, bool) or batch < 1:
        raise ValidationFailedError("Invalid batch_size.", field="batch_size")
    return retention, batch


async def run_protection_cleanup(engine: ContentEngine, *, batch_size: int | None = None) -> ProtectionCleanupSummary:
    """Phase 1: expire up to ``batch_size`` contexts (delete mappings and the wrapped index key, keep a tombstone).
    Phase 2: purge up to ``batch_size`` tombstones whose metadata retention passed. A second pass finds nothing."""
    retention_s, batch = _settings(engine, batch_size)
    t = engine.tables
    c = t.protection_contexts

    async def candidates(conn: AsyncConnection) -> list[tuple[str, str]]:
        now = await jdb.db_now(conn, engine.backend)
        rows = (
            await conn.execute(
                sa.select(c.c.scope_id, c.c.context_id)
                .where(c.c.state == "active", c.c.expires_at <= now)
                .order_by(c.c.expires_at, c.c.context_id)
                .limit(batch)
            )
        ).all()
        return [(r.scope_id, r.context_id) for r in rows]

    expired = deleted_rows = 0
    for scope_id, context_id in await engine._run(candidates, write=False, tx=None):

        async def one(conn: AsyncConnection, scope_id: str = scope_id, context_id: str = context_id) -> int | None:
            await jdb.lock_scope(conn, t, scope_id, exclusive=False)  # a disabled scope is cleaned too
            t0 = await jdb.db_now(conn, engine.backend)
            usage = await _ensure_usage(conn, engine, scope_id, t0)  # rank 11
            row = await _context_row(conn, engine, scope_id, context_id, "update")  # rank 12
            now = await jdb.db_now(conn, engine.backend)
            if row is None or row.state != "active" or now < row.expires_at:
                return None  # changed meanwhile (explicit delete): never resurrect, never touch
            n = await _drop_context_data(conn, engine, scope_id, context_id)
            await conn.execute(
                sa.update(c)
                .where(c.c.scope_id == scope_id, c.c.context_id == context_id)
                .values(
                    state="expired",
                    index_key_wrapped=None,
                    index_key_version=None,
                    mapping_count=0,
                    mapping_bytes=0,
                    metadata_expires_at=row.expires_at + timedelta(seconds=retention_s),
                    updated_at=now,
                )
            )
            await conn.execute(
                sa.update(t.protection_scope_usage)
                .where(t.protection_scope_usage.c.scope_id == scope_id)
                .values(active_contexts=max(0, int(usage.active_contexts) - 1), updated_at=now)
            )
            return n

        n = await engine._run(one, write=True, tx=None)
        if n is not None:
            expired += 1
            deleted_rows += n

    async def purge_candidates(conn: AsyncConnection) -> list[tuple[str, str]]:
        now = await jdb.db_now(conn, engine.backend)
        rows = (
            await conn.execute(
                sa.select(c.c.scope_id, c.c.context_id)
                .where(c.c.state.in_(("expired", "deleted")), c.c.metadata_expires_at <= now)
                .order_by(c.c.metadata_expires_at, c.c.context_id)
                .limit(batch)
            )
        ).all()
        return [(r.scope_id, r.context_id) for r in rows]

    purged = 0
    for scope_id, context_id in await engine._run(purge_candidates, write=False, tx=None):

        async def purge(conn: AsyncConnection, scope_id: str = scope_id, context_id: str = context_id) -> bool:
            await jdb.lock_scope(conn, t, scope_id, exclusive=False)
            row = await _context_row(conn, engine, scope_id, context_id, "update")
            now = await jdb.db_now(conn, engine.backend)
            if (
                row is None
                or row.state not in ("expired", "deleted")
                or row.metadata_expires_at is None
                or row.metadata_expires_at > now
            ):
                return False
            await _drop_context_data(conn, engine, scope_id, context_id)
            await conn.execute(sa.delete(c).where(c.c.scope_id == scope_id, c.c.context_id == context_id))
            return True

        if await engine._run(purge, write=True, tx=None):
            purged += 1
    summary = ProtectionCleanupSummary(contexts_expired=expired, mappings_deleted=deleted_rows, contexts_purged=purged)
    log.info(
        "protection_cleanup",
        extra={"contexts_expired": expired, "mappings_deleted": deleted_rows, "contexts_purged": purged},
    )
    return summary


async def protection_key_usage(engine: ContentEngine) -> list[KeyVersionUsage]:
    """Per key version: how many live contexts reference it (as index-key wrapper or as record key) and the latest
    ``expires_at`` among them. Counts and labels only; no context ids, no content (docs 9.4)."""
    t = engine.tables
    c, m = t.protection_contexts, t.protection_mappings

    async def fn(conn: AsyncConnection) -> list[Any]:
        now = await jdb.db_now(conn, engine.backend)
        live = (c.c.state == "active") & (c.c.expires_at > now)
        by_index = sa.select(
            c.c.index_key_version.label("version"), c.c.scope_id, c.c.context_id, c.c.expires_at
        ).where(live, c.c.index_key_version.is_not(None))
        by_record = (
            sa.select(m.c.key_version.label("version"), c.c.scope_id, c.c.context_id, c.c.expires_at)
            .select_from(
                m.join(c, sa.and_(m.c.scope_id == c.c.scope_id, m.c.context_id == c.c.context_id)),
            )
            .where(live)
        )
        both = sa.union(by_index, by_record).subquery()  # UNION removes duplicates: a context counts once
        q = sa.select(
            both.c.version, sa.func.count().label("n"), sa.func.max(both.c.expires_at).label("latest")
        ).group_by(both.c.version)
        return list((await conn.execute(q.order_by(both.c.version))).all())

    rows = await engine._run(fn, write=False, tx=None)
    return [
        KeyVersionUsage(version=r.version, live_contexts=int(r.n), latest_expires_at=_aware(r.latest)) for r in rows
    ]


def _aware(value: Any) -> Any:
    """``max(expires_at)`` loses the column type's conversion on SQLite: restore the UTC-aware datetime."""
    from datetime import UTC, datetime

    if value is None or (isinstance(value, datetime) and value.tzinfo is not None):
        return value
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC)
