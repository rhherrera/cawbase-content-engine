"""SQLite transaction control.

The stdlib ``sqlite3`` driver (and therefore ``aiosqlite``) emits ``BEGIN`` lazily in its legacy
mode, so a ``SAVEPOINT`` issued before the first DML statement would open (and ``RELEASE`` would
commit) a transaction of its own. The engine therefore requires a *real* ``BEGIN`` before any
savepoint and verifies it on the driver connection, not on SQLAlchemy's logical state.

``install_transaction_control`` is an explicit, host-invoked helper; the engine never applies it
to an engine it does not own.
"""

from __future__ import annotations

import weakref
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

BEGIN_OPTION = "sqlite_begin"

_PREPARED: weakref.WeakSet[Engine] = weakref.WeakSet()


def install_transaction_control(engine: AsyncEngine) -> None:
    """Make an ``aiosqlite`` engine emit a real ``BEGIN`` (or ``BEGIN IMMEDIATE``).

    Must be called before the engine opens its first connection. Connections already pooled keep
    the legacy behaviour and are rejected by the engine's runtime checks.
    """
    sync_engine = engine.sync_engine
    if sync_engine in _PREPARED:
        return

    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(sync_engine, "begin")
    def _on_begin(conn: Any) -> None:
        mode = conn.get_execution_options().get(BEGIN_OPTION)
        conn.exec_driver_sql("BEGIN IMMEDIATE" if mode == "IMMEDIATE" else "BEGIN")

    _PREPARED.add(sync_engine)


def is_prepared(engine: AsyncEngine) -> bool:
    return engine.sync_engine in _PREPARED


async def driver_in_transaction(conn: AsyncConnection) -> bool:
    """Real transaction state of the underlying sqlite3 connection."""
    raw = await conn.get_raw_connection()
    return bool(raw.driver_connection.in_transaction)  # type: ignore[union-attr]
