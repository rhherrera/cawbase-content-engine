"""A07 binding cleanup (docs/docx-protection.md, section 11): explicit, bounded, repeatable; needs no keys.

A binding whose context is no longer alive (expired, deleted or purged) is marked ``ended``: its occurrences and
MAC are dropped and a metadata tombstone stays for ``metadata_retention_s``; due tombstones are purged. It never
touches A05's ``run_cleanup`` nor A06's ``run_protection_cleanup``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..jobs import db as jdb
from .types import DocxCleanupSummary

if TYPE_CHECKING:
    from ..engine import ContentEngine

DEFAULT_BATCH = 200
DEFAULT_RETENTION_S = 30 * 86400


async def run_docx_cleanup(engine: ContentEngine, *, batch_size: int | None = None) -> DocxCleanupSummary:
    t = engine.tables
    d, c = t.docx_derivatives, t.protection_contexts
    n = int(batch_size or DEFAULT_BATCH)
    setup = engine.protection
    retention = int(getattr(setup, "metadata_retention_s", DEFAULT_RETENTION_S) or DEFAULT_RETENTION_S)

    async def candidates(conn: AsyncConnection) -> list[tuple[str, str]]:
        rows = (
            await conn.execute(
                sa.select(d.c.scope_id, d.c.derivative_id).where(d.c.state == "active").order_by(d.c.derivative_id)
            )
        ).all()
        return [(r[0], r[1]) for r in rows]

    ended = 0
    for scope_id, did in await engine._run(candidates, write=False, tx=None):
        if ended >= n:
            break

        async def end_one(conn: AsyncConnection, scope_id: str = scope_id, did: str = did) -> int:
            await jdb.lock_scope(conn, t, scope_id, exclusive=False)  # 1
            b = (await conn.execute(sa.select(d).where(d.c.scope_id == scope_id, d.c.derivative_id == did))).first()
            if b is None:
                return 0
            ctx = (
                await conn.execute(
                    sa.select(c)
                    .where(c.c.scope_id == scope_id, c.c.context_id == b.context_id)
                    .with_for_update(read=True)
                )
            ).first()  # 12
            row = (
                await conn.execute(
                    sa.select(d).where(d.c.scope_id == scope_id, d.c.derivative_id == did).with_for_update()
                )
            ).first()  # 14
            now = await jdb.db_now(conn, engine.backend)
            alive = ctx is not None and ctx.state == "active" and now < ctx.expires_at
            if row is None or row.state != "active" or alive:
                return 0
            await conn.execute(
                sa.update(d)
                .where(d.c.scope_id == scope_id, d.c.derivative_id == did)
                .values(
                    state="ended",
                    occurrences=None,
                    binding_mac=None,
                    ended_at=now,
                    metadata_expires_at=now + timedelta(seconds=retention),
                )
            )
            return 1

        ended += await engine._run(end_one, write=True, tx=None)

    async def purge(conn: AsyncConnection) -> int:
        now = await jdb.db_now(conn, engine.backend)
        ids = (
            await conn.execute(
                sa.select(d.c.scope_id, d.c.derivative_id)
                .where(d.c.state == "ended", d.c.metadata_expires_at <= now)
                .limit(n)
            )
        ).all()
        for sid, did in ids:
            await conn.execute(sa.delete(d).where(d.c.scope_id == sid, d.c.derivative_id == did, d.c.state == "ended"))
        return len(ids)

    purged = await engine._run(purge, write=True, tx=None)
    return DocxCleanupSummary(bindings_ended=ended, tombstones_purged=purged)


_ = Any
