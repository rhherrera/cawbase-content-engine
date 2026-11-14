from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import (
    ConflictError,
    ResourceNotAccessibleError,
    ResourceNotFoundError,
    RevisionConflictError,
    ScopeDisabledError,
    ValidationFailedError,
)
from ..models import Page, Scope, UpsertResult
from ..persistence.tables import Tables
from .common import add_event, decode_cursor, encode_cursor, insert_ignore, now, scope_from_row

UNSET: Any = object()


async def get_scope_row(conn: AsyncConnection, t: Tables, scope_id: str) -> Any:
    return (await conn.execute(sa.select(t.scopes).where(t.scopes.c.scope_id == scope_id))).first()


async def create_scope(
    conn: AsyncConnection,
    t: Tables,
    *,
    scope_id: str,
    display_name: str,
    description: str | None,
    metadata: dict[str, Any],
    actor_key_id: str | None,
    actor_label: str | None,
) -> UpsertResult[Scope]:
    ts = now()
    created = await insert_ignore(
        conn,
        t,
        t.scopes,
        dict(
            scope_id=scope_id,
            display_name=display_name,
            description=description,
            metadata=metadata,
            status="active",
            revision=1,
            created_at=ts,
            updated_at=ts,
        ),
    )
    scope = scope_from_row(await get_scope_row(conn, t, scope_id))
    if created:
        await add_event(
            conn,
            t,
            action="scope.created",
            target_type="scope",
            target_id=scope_id,
            actor_key_id=actor_key_id,
            actor_label=actor_label,
            scope_id=scope_id,
        )
        return UpsertResult(value=scope, created=True)
    if (scope.display_name, scope.description, scope.metadata) != (display_name, description, metadata):
        raise ConflictError("The scope already exists with different attributes.")
    return UpsertResult(value=scope, created=False)


async def admin_get_scope(conn: AsyncConnection, t: Tables, scope_id: str) -> Scope:
    row = await get_scope_row(conn, t, scope_id)
    if row is None:
        raise ResourceNotFoundError()
    return scope_from_row(row)


async def list_scopes(
    conn: AsyncConnection, t: Tables, *, status: str | None, limit: int, cursor: str | None
) -> Page[Scope]:
    if status not in (None, "active", "disabled"):
        raise ValidationFailedError("Invalid status.", field="status")
    q = sa.select(t.scopes).order_by(t.scopes.c.scope_id).limit(limit + 1)
    if status:
        q = q.where(t.scopes.c.status == status)
    after = decode_cursor(cursor)
    if after is not None:
        q = q.where(t.scopes.c.scope_id > after)
    rows = (await conn.execute(q)).all()
    items = [scope_from_row(r) for r in rows[:limit]]
    return Page(items=items, next_cursor=encode_cursor(items[-1].scope_id) if len(rows) > limit else None)


async def set_scope_status(
    conn: AsyncConnection,
    t: Tables,
    *,
    scope_id: str,
    status: str,
    actor_key_id: str | None,
    actor_label: str | None,
    retention: Any = None,
) -> Scope:
    row = await get_scope_row(conn, t, scope_id)
    if row is None:
        raise ResourceNotFoundError()
    from ..jobs import db as jdb  # A05 fence (lazy import): reconcile BEFORE any other write (I2)

    ret = retention or _default_retention()
    fence_now = await jdb.db_now(conn, t.backend)
    await jdb.lock_scope(conn, t, scope_id, exclusive=True)
    await jdb.reconcile_locked(conn, t, scope_id, fence_now, ret)
    row = await get_scope_row(conn, t, scope_id)
    if row._mapping["status"] == status:
        return scope_from_row(row)
    res = await conn.execute(
        sa.update(t.scopes)
        .where(t.scopes.c.scope_id == scope_id, t.scopes.c.status != status)
        .values(status=status, revision=t.scopes.c.revision + 1, updated_at=now())
    )
    if res.rowcount == 1:
        await jdb.after_scope_mutation(conn, t, scope_id, fence_now, ret, disabled=status == "disabled")
        await add_event(
            conn,
            t,
            action="scope.enabled" if status == "active" else "scope.disabled",
            target_type="scope",
            target_id=scope_id,
            actor_key_id=actor_key_id,
            actor_label=actor_label,
            scope_id=scope_id,
        )
    return scope_from_row(await get_scope_row(conn, t, scope_id))


async def update_scope(
    conn: AsyncConnection,
    t: Tables,
    *,
    scope_id: str,
    expected_revision: int,
    changes: dict[str, Any],
    actor_key_id: str | None,
    actor_label: str | None,
    retention: Any = None,
) -> Scope:
    """Strict optimistic concurrency (P-20 / D-30).

    ``expected_revision`` must always equal the current revision, even if the requested values are
    already applied. Matching revision + no effective change => no write, no revision bump. The
    write itself is one conditional UPDATE (revision *and* status in the WHERE), so there is no
    window between check and write.
    """
    if await get_scope_row(conn, t, scope_id) is None:
        raise ResourceNotAccessibleError()
    from ..jobs import db as jdb  # A05 fence (lazy import): reconcile BEFORE any other write (I2)

    ret = retention or _default_retention()
    fence_now = await jdb.db_now(conn, t.backend)
    await jdb.lock_scope(conn, t, scope_id, exclusive=True)
    await jdb.reconcile_locked(conn, t, scope_id, fence_now, ret)
    row = await get_scope_row(conn, t, scope_id)
    m = row._mapping
    if m["status"] == "disabled":
        raise ScopeDisabledError()
    if m["revision"] != expected_revision:
        raise RevisionConflictError()
    effective = {k: v for k, v in changes.items() if m[k] != v}
    if not effective:
        return scope_from_row(row)
    res = await conn.execute(
        sa.update(t.scopes)
        .where(t.scopes.c.scope_id == scope_id, t.scopes.c.revision == expected_revision, t.scopes.c.status == "active")
        .values(**effective, revision=t.scopes.c.revision + 1, updated_at=now())
    )
    if res.rowcount != 1:  # lost a race between our read and the conditional write
        fresh = await get_scope_row(conn, t, scope_id)
        if fresh is None:
            raise ResourceNotAccessibleError()
        if fresh._mapping["status"] == "disabled":
            raise ScopeDisabledError()
        raise RevisionConflictError()
    await jdb.after_scope_mutation(conn, t, scope_id, fence_now, ret, disabled=False)
    await add_event(
        conn,
        t,
        action="scope.updated",
        target_type="scope",
        target_id=scope_id,
        actor_key_id=actor_key_id,
        actor_label=actor_label,
        scope_id=scope_id,
        detail={"changed_fields": sorted(effective)},
    )
    return scope_from_row(await get_scope_row(conn, t, scope_id))


def _default_retention() -> Any:
    from ..jobs.setup import Retention

    return Retention()
