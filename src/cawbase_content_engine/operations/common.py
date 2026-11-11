from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import ValidationFailedError
from ..models import Client, Scope
from ..persistence.dialect import Backend
from ..persistence.tables import Tables


def now() -> datetime:
    return datetime.now(UTC)


async def insert_ignore(conn: AsyncConnection, tables: Tables, table: sa.Table, values: dict[str, Any]) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING; True when a row was inserted."""
    ins = postgresql.insert if tables.backend is Backend.POSTGRESQL else sqlite.insert
    result = await conn.execute(ins(table).values(**values).on_conflict_do_nothing())
    return bool(result.rowcount == 1)


async def add_event(
    conn: AsyncConnection,
    tables: Tables,
    *,
    action: str,
    target_type: str,
    target_id: str,
    actor_key_id: str | None,
    actor_label: str | None,
    scope_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Details carry field *names* and ids only: never values, secrets or hashes."""
    await conn.execute(
        sa.insert(tables.admin_events).values(
            occurred_at=now(),
            action=action,
            actor_key_id=actor_key_id,
            actor_label=actor_label,
            target_type=target_type,
            target_id=target_id,
            scope_id=scope_id,
            detail=detail or {},
        )
    )


def scope_from_row(row: Any) -> Scope:
    m = row._mapping
    return Scope(
        scope_id=m["scope_id"],
        display_name=m["display_name"],
        description=m["description"],
        metadata=m["metadata"],
        status=m["status"],
        revision=m["revision"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


def client_from_row(row: Any) -> Client:
    m = row._mapping
    return Client(
        client_id=m["client_id"],
        name=m["name"],
        display_name=m["display_name"],
        status=m["status"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    try:
        return base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True).decode()
    except Exception:
        raise ValidationFailedError("Invalid cursor.", field="cursor") from None


def page_size(limit: int | None, default: int, maximum: int) -> int:
    if limit is None:
        return default
    if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= maximum):
        raise ValidationFailedError("Invalid limit.", field="limit")
    return limit
