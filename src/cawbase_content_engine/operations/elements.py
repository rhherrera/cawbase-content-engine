"""Categories and sensitive types: one implementation parameterized by kind (A02)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from .. import validation as v
from ..config import Limits
from ..errors import (
    ConflictError,
    InvalidStateTransitionError,
    LimitExceededError,
    NameConflictError,
    PriorityConflictError,
    ResourceNotFoundError,
    RevisionConflictError,
)
from ..models import Page, UpsertResult
from ..persistence.tables import Tables
from .common import decode_cursor, encode_cursor, now
from .profile_common import (
    Actor,
    begin_profile_write,
    bump_draft,
    category_from_row,
    event,
    type_from_row,
    visible_profile_row,
)


@dataclass(frozen=True)
class Kind:
    key: str  # "category" | "sensitive_type"
    id_col: str
    has_priority: bool


CATEGORY = Kind("category", "category_id", True)
SENSITIVE_TYPE = Kind("sensitive_type", "type_id", False)


def _table(t: Tables, kind: Kind) -> sa.Table:
    return t.categories if kind.has_priority else t.sensitive_types


def _convert(kind: Kind, row: Any) -> Any:
    return category_from_row(row) if kind.has_priority else type_from_row(row)


def _where(t: Tables, kind: Kind, scope_id: str, profile_id: str, element_id: str | None = None) -> list[Any]:
    tbl = _table(t, kind)
    cond = [tbl.c.scope_id == scope_id, tbl.c.profile_id == profile_id]
    if element_id is not None:
        cond.append(tbl.c[kind.id_col] == element_id)
    return cond


async def _element_row(
    conn: AsyncConnection, t: Tables, kind: Kind, scope_id: str, profile_id: str, element_id: str
) -> Any:
    tbl = _table(t, kind)
    return (await conn.execute(sa.select(tbl).where(*_where(t, kind, scope_id, profile_id, element_id)))).first()


async def _check_unique(
    conn: AsyncConnection,
    t: Tables,
    kind: Kind,
    scope_id: str,
    profile_id: str,
    element_id: str,
    *,
    name_key: str | None,
    priority: int | None,
    enabled: bool,
) -> None:
    tbl = _table(t, kind)
    base = _where(t, kind, scope_id, profile_id) + [tbl.c[kind.id_col] != element_id]
    if name_key is not None:
        q = sa.select(sa.literal(1)).where(*base, tbl.c.name_key == name_key, tbl.c.state != "deleted").limit(1)
        if (await conn.execute(q)).first() is not None:
            raise NameConflictError()
    if kind.has_priority and enabled and priority is not None:
        q = sa.select(sa.literal(1)).where(*base, tbl.c.priority == priority, tbl.c.state == "enabled").limit(1)
        if (await conn.execute(q)).first() is not None:
            raise PriorityConflictError()


def _values_equal(row: Any, kind: Kind, new: dict[str, Any]) -> bool:
    m = row._mapping
    keys = ["name", "definition", "examples", "translations"] + (["priority"] if kind.has_priority else [])
    return all((m[k] or None) == (new[k] or None) if k == "translations" else m[k] == new[k] for k in keys)


async def create_element(
    conn: AsyncConnection,
    t: Tables,
    limits: Limits,
    actor: Actor,
    kind: Kind,
    *,
    scope_id: str,
    profile_id: str,
    element_id: str,
    values: dict[str, Any],
) -> UpsertResult[Any]:
    prow = await begin_profile_write(conn, t, scope_id, profile_id)
    tbl = _table(t, kind)
    existing = await _element_row(conn, t, kind, scope_id, profile_id, element_id)
    if existing is not None:  # idempotent retry: never blocked by the limits
        if existing._mapping["state"] == "deleted" or not _values_equal(existing, kind, values):
            raise ConflictError("The element already exists with different attributes or its id is reserved.")
        return UpsertResult(value=_convert(kind, existing), created=False)
    live = (
        await conn.execute(
            sa.select(sa.func.count()).where(*_where(t, kind, scope_id, profile_id), tbl.c.state != "deleted")
        )
    ).scalar_one()
    total = (await conn.execute(sa.select(sa.func.count()).where(*_where(t, kind, scope_id, profile_id)))).scalar_one()
    if live >= limits.max_elements_per_profile or total >= limits.max_element_rows_per_profile:
        raise LimitExceededError("Too many elements in the profile.", field=kind.id_col)
    nk = v.name_key(values["name"])
    await _check_unique(
        conn,
        t,
        kind,
        scope_id,
        profile_id,
        element_id,
        name_key=nk,
        priority=values.get("priority"),
        enabled=True,
    )
    ts = now()
    row_values: dict[str, Any] = dict(
        scope_id=scope_id,
        profile_id=profile_id,
        name=values["name"],
        name_key=nk,
        definition=values["definition"],
        examples=values["examples"],
        translations=values["translations"],
        state="enabled",
        deleted_at=None,
        revision=1,
        created_at=ts,
        updated_at=ts,
    )
    row_values[kind.id_col] = element_id
    if kind.has_priority:
        row_values["priority"] = values.get("priority")
    else:
        row_values["origin_entry"] = None
    await conn.execute(sa.insert(tbl).values(**row_values))
    await bump_draft(conn, t, scope_id, profile_id)
    await event(
        conn,
        t,
        actor,
        f"{kind.key}.created",
        kind.key,
        f"{profile_id}/{element_id}",
        scope_id,
        {
            "profile_id": profile_id,
            kind.id_col: element_id,
            "draft_revision": [prow._mapping["draft_revision"], prow._mapping["draft_revision"] + 1],
        },
    )
    return UpsertResult(
        value=_convert(kind, await _element_row(conn, t, kind, scope_id, profile_id, element_id)), created=True
    )


async def get_element(
    conn: AsyncConnection,
    t: Tables,
    kind: Kind,
    *,
    scope_id: str,
    profile_id: str,
    element_id: str,
    include_deleted: bool,
) -> Any:
    await visible_profile_row(conn, t, scope_id, profile_id)
    row = await _element_row(conn, t, kind, scope_id, profile_id, element_id)
    if row is None or (row._mapping["state"] == "deleted" and not include_deleted):
        raise ResourceNotFoundError()
    return _convert(kind, row)


async def list_elements(
    conn: AsyncConnection,
    t: Tables,
    kind: Kind,
    *,
    scope_id: str,
    profile_id: str,
    states: tuple[str, ...],
    limit: int,
    cursor: str | None,
) -> Page[Any]:
    await visible_profile_row(conn, t, scope_id, profile_id)
    tbl = _table(t, kind)
    q = (
        sa.select(tbl)
        .where(*_where(t, kind, scope_id, profile_id), tbl.c.state.in_(states))
        .order_by(tbl.c[kind.id_col])
        .limit(limit + 1)
    )
    after = decode_cursor(cursor)
    if after is not None:
        q = q.where(tbl.c[kind.id_col] > after)
    rows = (await conn.execute(q)).all()
    items = [_convert(kind, r) for r in rows[:limit]]
    last = getattr(items[-1], kind.id_col) if items else None
    return Page(items=items, next_cursor=encode_cursor(str(last)) if len(rows) > limit else None)


async def update_element(
    conn: AsyncConnection,
    t: Tables,
    actor: Actor,
    kind: Kind,
    *,
    scope_id: str,
    profile_id: str,
    element_id: str,
    expected_revision: int,
    changes: dict[str, Any],
) -> Any:
    prow = await begin_profile_write(conn, t, scope_id, profile_id)
    tbl = _table(t, kind)
    row = await _element_row(conn, t, kind, scope_id, profile_id, element_id)
    if row is None or row._mapping["state"] == "deleted":
        raise ResourceNotFoundError()
    m = row._mapping
    if m["revision"] != expected_revision:
        raise RevisionConflictError()
    effective = {k: val for k, val in changes.items() if (m[k] or None) != (val or None)}
    if not effective:
        return _convert(kind, row)
    values = dict(effective)
    if "name" in effective:
        values["name_key"] = v.name_key(effective["name"])
    await _check_unique(
        conn,
        t,
        kind,
        scope_id,
        profile_id,
        element_id,
        name_key=values.get("name_key"),
        priority=effective.get("priority") if kind.has_priority and "priority" in effective else None,
        enabled=m["state"] == "enabled",
    )
    res = await conn.execute(
        sa.update(tbl)
        .where(*_where(t, kind, scope_id, profile_id, element_id), tbl.c.revision == expected_revision)
        .values(**values, revision=tbl.c.revision + 1, updated_at=now())
    )
    if res.rowcount != 1:
        raise RevisionConflictError()
    await bump_draft(conn, t, scope_id, profile_id)
    await event(
        conn,
        t,
        actor,
        f"{kind.key}.updated",
        kind.key,
        f"{profile_id}/{element_id}",
        scope_id,
        {
            "profile_id": profile_id,
            kind.id_col: element_id,
            "changed_fields": sorted(effective),
            "revision": [expected_revision, expected_revision + 1],
            "draft_revision": [prow._mapping["draft_revision"], prow._mapping["draft_revision"] + 1],
        },
    )
    return _convert(kind, await _element_row(conn, t, kind, scope_id, profile_id, element_id))


async def set_element_state(
    conn: AsyncConnection,
    t: Tables,
    actor: Actor,
    kind: Kind,
    *,
    scope_id: str,
    profile_id: str,
    element_id: str,
    target: str,
) -> Any:
    """Target-state and repeatable; ``deleted`` is logical and terminal. Affects later publications only."""
    prow = await begin_profile_write(conn, t, scope_id, profile_id)
    tbl = _table(t, kind)
    row = await _element_row(conn, t, kind, scope_id, profile_id, element_id)
    if row is None:
        raise ResourceNotFoundError()
    m = row._mapping
    if m["state"] == target:
        return _convert(kind, row)
    if m["state"] == "deleted":
        raise InvalidStateTransitionError()
    if target == "enabled":
        await _check_unique(
            conn,
            t,
            kind,
            scope_id,
            profile_id,
            element_id,
            name_key=None,
            priority=m["priority"] if kind.has_priority else None,
            enabled=True,
        )
    ts = now()
    await conn.execute(
        sa.update(tbl)
        .where(*_where(t, kind, scope_id, profile_id, element_id))
        .values(
            state=target,
            deleted_at=ts if target == "deleted" else None,
            revision=tbl.c.revision + 1,
            updated_at=ts,
        )
    )
    await bump_draft(conn, t, scope_id, profile_id)
    await event(
        conn,
        t,
        actor,
        f"{kind.key}.{target}",
        kind.key,
        f"{profile_id}/{element_id}",
        scope_id,
        {
            "profile_id": profile_id,
            kind.id_col: element_id,
            "revision": [m["revision"], m["revision"] + 1],
            "draft_revision": [prow._mapping["draft_revision"], prow._mapping["draft_revision"] + 1],
        },
    )
    return _convert(kind, await _element_row(conn, t, kind, scope_id, profile_id, element_id))
