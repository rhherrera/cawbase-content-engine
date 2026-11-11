from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import ConflictError, ResourceNotFoundError, ValidationFailedError
from ..models import Client, Page, UpsertResult
from ..persistence.tables import Tables
from .common import add_event, client_from_row, decode_cursor, encode_cursor, insert_ignore, now


async def get_client_row(conn: AsyncConnection, t: Tables, client_id: str) -> Any:
    return (await conn.execute(sa.select(t.clients).where(t.clients.c.client_id == client_id))).first()


async def create_client(
    conn: AsyncConnection,
    t: Tables,
    *,
    name: str,
    display_name: str,
    actor_key_id: str | None,
    actor_label: str | None,
    event_action: str = "client.created",
) -> UpsertResult[Client]:
    ts = now()
    client_id = "cli_" + uuid.uuid4().hex
    created = await insert_ignore(
        conn,
        t,
        t.clients,
        dict(client_id=client_id, name=name, display_name=display_name, status="active", created_at=ts, updated_at=ts),
    )
    row = (await conn.execute(sa.select(t.clients).where(t.clients.c.name == name))).first()
    client = client_from_row(row)
    if created:
        await add_event(
            conn,
            t,
            action=event_action,
            target_type="client",
            target_id=client_id,
            actor_key_id=actor_key_id,
            actor_label=actor_label,
        )
        return UpsertResult(value=client, created=True)
    if client.display_name != display_name:
        raise ConflictError("The client already exists with different attributes.")
    return UpsertResult(value=client, created=False)


async def get_client(conn: AsyncConnection, t: Tables, client_id: str) -> Client:
    row = await get_client_row(conn, t, client_id)
    if row is None:
        raise ResourceNotFoundError()
    return client_from_row(row)


async def list_clients(conn: AsyncConnection, t: Tables, *, limit: int, cursor: str | None) -> Page[Client]:
    q = sa.select(t.clients).order_by(t.clients.c.client_id).limit(limit + 1)
    after = decode_cursor(cursor)
    if after is not None:
        q = q.where(t.clients.c.client_id > after)
    rows = (await conn.execute(q)).all()
    items = [client_from_row(r) for r in rows[:limit]]
    return Page(items=items, next_cursor=encode_cursor(items[-1].client_id) if len(rows) > limit else None)


async def set_client_status(
    conn: AsyncConnection,
    t: Tables,
    *,
    client_id: str,
    status: str,
    actor_key_id: str | None,
    actor_label: str | None,
) -> Client:
    if status not in ("active", "disabled"):
        raise ValidationFailedError("Invalid status.", field="status")
    row = await get_client_row(conn, t, client_id)
    if row is None:
        raise ResourceNotFoundError()
    if row._mapping["status"] != status:
        res = await conn.execute(
            sa.update(t.clients)
            .where(t.clients.c.client_id == client_id, t.clients.c.status != status)
            .values(status=status, updated_at=now())
        )
        if res.rowcount == 1:
            await add_event(
                conn,
                t,
                action=f"client.{'enabled' if status == 'active' else 'disabled'}",
                target_type="client",
                target_id=client_id,
                actor_key_id=actor_key_id,
                actor_label=actor_label,
            )
    return client_from_row(await get_client_row(conn, t, client_id))
