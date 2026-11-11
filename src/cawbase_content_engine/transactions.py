"""Transaction ownership: engine-owned transactions and host-provided ones (D-13, D-23)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .errors import (
    ConflictError,
    DatabaseUnavailableError,
    EngineError,
    IncompatibleTransactionError,
    InternalError,
    TransactionBrokenError,
    TransactionStateError,
)
from .persistence.dialect import Backend
from .persistence.sqlite_support import BEGIN_OPTION, driver_in_transaction

T = TypeVar("T")
_BUSY = "_cce_host_connection_busy"


class HostTransaction:
    """Wraps an ``AsyncConnection`` whose transaction the host has already begun.

    The engine only ever issues ``SAVEPOINT`` / ``RELEASE`` / ``ROLLBACK TO`` on it; it never commits,
    rolls back, closes or begins the outer transaction. The host must not use the connection
    concurrently while an operation is running.
    """

    __slots__ = ("_connection",)

    def __init__(self, connection: AsyncConnection) -> None:
        if not isinstance(connection, AsyncConnection):
            raise TransactionStateError("HostTransaction requires a SQLAlchemy AsyncConnection.")
        if not connection.in_transaction():
            raise TransactionStateError("The host connection has no active transaction.")
        self._connection = connection

    @property
    def connection(self) -> AsyncConnection:
        return self._connection


def translate(exc: BaseException) -> EngineError:
    """Database failure -> typed error, never exposing driver text."""
    if isinstance(exc, EngineError):
        return exc
    if isinstance(exc, sa.exc.IntegrityError):
        return ConflictError()
    if isinstance(exc, (sa.exc.OperationalError, sa.exc.InterfaceError, sa.exc.TimeoutError, OSError, TimeoutError)):
        return DatabaseUnavailableError()
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return DatabaseUnavailableError()
    return InternalError()


def _is_connection_lost(exc: BaseException) -> bool:
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


async def run_own(
    engine: AsyncEngine, backend: Backend, fn: Callable[[AsyncConnection], Awaitable[T]], *, write: bool
) -> T:
    """Short engine-owned transaction: commit on success, rollback on any exception."""
    try:
        conn = await engine.connect()
    except Exception as exc:
        raise translate(exc) from None
    try:
        if backend is Backend.SQLITE and write:
            conn = await conn.execution_options(**{BEGIN_OPTION: "IMMEDIATE"})
        try:
            await conn.begin()
            result = await fn(conn)
            await conn.commit()
            return result
        except BaseException as exc:
            try:
                await asyncio.shield(conn.rollback())
            except Exception:
                pass
            if isinstance(exc, Exception):
                raise translate(exc) from None
            raise
    finally:
        try:
            await asyncio.shield(conn.close())
        except Exception:
            pass


async def run_in_host(
    injected: AsyncEngine | None,
    backend: Backend,
    tx: HostTransaction,
    fn: Callable[[AsyncConnection], Awaitable[T]],
) -> T:
    """Run ``fn`` inside a SAVEPOINT of the host's transaction, on the host's connection."""
    conn = tx.connection
    if injected is None or conn.sync_engine is not injected.sync_engine:
        raise IncompatibleTransactionError(
            "The host connection must come from the same injected AsyncEngine the engine was built with."
        )
    if not conn.in_transaction():
        raise TransactionStateError("The host connection has no active transaction.")
    if conn.invalidated:
        raise TransactionBrokenError()
    raw = await conn.get_raw_connection()
    if raw.info.get(_BUSY):
        raise TransactionStateError("The host connection is already in use by another operation.")
    raw.info[_BUSY] = True
    try:
        if backend is Backend.SQLITE:
            try:
                real_begin = await driver_in_transaction(conn)
            except Exception:  # the driver connection is gone
                raise TransactionBrokenError() from None
            if not real_begin:
                raise IncompatibleTransactionError(
                    "SQLite needs a real BEGIN before the savepoint; see install_transaction_control()."
                )
        try:
            savepoint = await conn.begin_nested()
        except Exception:
            # The transaction was active a moment ago: failing to open a savepoint means the
            # connection was lost or the transaction was invalidated (e.g. PostgreSQL "aborted").
            raise TransactionBrokenError() from None
        try:
            result = await fn(conn)
        except BaseException as exc:
            lost = _is_connection_lost(exc) or conn.invalidated
            if lost:
                raise TransactionBrokenError() from None
            try:
                await asyncio.shield(savepoint.rollback())
            except Exception:
                raise TransactionBrokenError() from None
            if isinstance(exc, Exception):
                raise translate(exc) from None
            raise
        try:
            await savepoint.commit()
        except Exception:
            raise TransactionBrokenError() from None
        return result
    finally:
        raw.info.pop(_BUSY, None)
