"""Single-transaction, single-lock migration runner (D-24).

One upgrade = one transaction and one lock for the whole pending sequence: lock acquisition,
schema check, plan, every revision and the history table. There are no intermediate commits,
autocommit blocks or independent connections. The lock is exclusive among cooperating migrators
only; it does not stop an administrator from running arbitrary SQL.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from ..constants import MIGRATION_LOCK_KEY, SCHEMA_NAME, SQLITE_PREFIX
from ..errors import (
    InvalidConfigurationError,
    MigrationError,
    MigrationLockTimeoutError,
    MigrationPermissionError,
    SchemaIncompatibleError,
)
from ..persistence.dialect import Backend, Namespace

DEFAULT_SCRIPT_LOCATION = Path(__file__).parent

_PERMISSION_MARKERS = ("permission denied", "must be owner", "readonly database", "read-only")
_LOCK_MARKERS = ("database is locked", "database table is locked")


@dataclass(frozen=True)
class MigrationReport:
    from_revision: str | None
    to_revision: str | None
    applied: tuple[str, ...] = field(default_factory=tuple)
    already_current: bool = False
    to_level: int = 0
    duration_s: float = 0.0


def classify_db_error(exc: BaseException) -> MigrationError:
    text = str(getattr(exc, "orig", exc)).lower()
    if any(m in text for m in _LOCK_MARKERS):
        return MigrationLockTimeoutError()
    if any(m in text for m in _PERMISSION_MARKERS):
        return MigrationPermissionError()
    return MigrationError()


async def _acquire_postgres_lock(conn: AsyncConnection, lock_timeout: float) -> None:
    deadline = time.monotonic() + lock_timeout
    while True:
        got = (
            await conn.execute(sa.text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
        ).scalar_one()
        if got:
            return
        if time.monotonic() >= deadline:
            raise MigrationLockTimeoutError()
        await asyncio.sleep(0.1)


async def _check_isolation(conn: AsyncConnection) -> None:
    level = (await conn.execute(sa.text("SHOW transaction_isolation"))).scalar_one()
    if str(level).lower() != "read committed":
        raise InvalidConfigurationError(
            "Migrations require READ COMMITTED so the post-lock schema re-read sees committed state."
        )


async def _check_namespace(conn: AsyncConnection, ns: Namespace) -> None:
    """Refuse to adopt a schema/prefix that holds objects the engine does not own."""
    if ns.backend is Backend.POSTGRESQL:
        rows = (
            (
                await conn.execute(
                    sa.text(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = :s AND c.relkind IN ('r','v','m','S','f','p')"
                    ),
                    {"s": SCHEMA_NAME},
                )
            )
            .scalars()
            .all()
        )
        names = set(rows)
    else:
        rows = (
            (
                await conn.execute(
                    sa.text("SELECT name FROM sqlite_master WHERE name LIKE :p ESCAPE '\\' AND type = 'table'"),
                    {"p": SQLITE_PREFIX.replace("_", "\\_") + "%"},
                )
            )
            .scalars()
            .all()
        )
        names = set(rows)
    if names and ns.version_table not in names:
        raise SchemaIncompatibleError(
            "The engine namespace already contains objects that this engine does not own; refusing to adopt it."
        )


def _alembic_upgrade(
    sync_conn: Connection, ns: Namespace, location: Path, target: str
) -> tuple[str | None, str | None, list[str]]:
    from alembic.config import Config
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(location))
    script = ScriptDirectory.from_config(cfg)
    applied: list[str] = []

    def fn(rev: Any, context: Any) -> Any:
        steps = script._upgrade_revs(target, rev)
        applied.extend(s.revision.revision for s in steps)
        return steps

    with EnvironmentContext(cfg, script, fn=fn, as_sql=False, destination_rev=target) as env:
        env.configure(
            connection=sync_conn,
            target_metadata=None,
            transaction_per_migration=False,
            transactional_ddl=True,
            version_table=ns.version_table,
            version_table_schema=ns.schema,
        )
        ctx = env.get_context()
        current = ctx.get_current_revision()
        with env.begin_transaction():
            env.run_migrations()
        return current, ctx.get_current_revision(), applied


async def run_upgrade(
    conn: AsyncConnection,
    backend: Backend,
    *,
    lock_timeout: float,
    script_location: Path | None = None,
    target: str = "head",
) -> MigrationReport:
    """Run on a connection whose transaction is already open (SQLite: ``BEGIN IMMEDIATE``).

    The caller owns commit/rollback so that the whole sequence is atomic.
    """
    started = time.monotonic()
    ns = Namespace(backend)
    try:
        if backend is Backend.POSTGRESQL:
            await _check_isolation(conn)
            await _acquire_postgres_lock(conn, lock_timeout)
        await _check_namespace(conn, ns)
        if backend is Backend.POSTGRESQL:
            await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"'))
        # The plan is computed here, *after* the lock, by Alembic reading the history table.
        current, new, applied = await conn.run_sync(
            _alembic_upgrade, ns, script_location or DEFAULT_SCRIPT_LOCATION, target
        )
    except (MigrationError, InvalidConfigurationError, SchemaIncompatibleError):
        raise
    except DBAPIError as exc:
        raise classify_db_error(exc) from None
    except Exception:
        raise MigrationError() from None
    return MigrationReport(
        from_revision=current,
        to_revision=new,
        applied=tuple(applied),
        already_current=not applied,
        duration_s=time.monotonic() - started,
    )
