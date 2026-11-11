"""Explicit migrations (extra ``migrations``). Never invoked implicitly by the library."""

from __future__ import annotations

import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from ..config import EngineConfig
from ..errors import (
    DatabaseUnavailableError,
    EngineError,
    InvalidConfigurationError,
    MigrationError,
    MigrationLockTimeoutError,
)
from ..persistence.dialect import Backend
from ..persistence.engines import create_owned_engine
from ..persistence.sqlite_support import BEGIN_OPTION, is_prepared
from ..schema import read_schema_status
from .runner import DEFAULT_SCRIPT_LOCATION, MigrationReport, classify_db_error, run_upgrade

__all__ = ["DEFAULT_SCRIPT_LOCATION", "MigrationReport", "upgrade"]


async def upgrade(
    config: EngineConfig,
    *,
    migration_url: str | SecretStr | None = None,
    lock_timeout: float | None = None,
    target: str = "head",
    script_location: object = None,
) -> MigrationReport:
    """Apply every pending revision in ONE transaction under ONE lock (D-24).

    ``migration_url`` (DDL credentials) may differ from the runtime URL. With an injected engine the
    host's credentials must allow DDL; a SQLite engine must have been prepared with
    ``install_transaction_control`` (the engine does not reconfigure engines it does not own).
    """
    from pathlib import Path

    try:
        import alembic  # noqa: F401
    except ModuleNotFoundError:
        raise InvalidConfigurationError("Migrations need the 'migrations' extra (alembic).") from None
    backend = config.backend
    timeout = lock_timeout if lock_timeout is not None else config.lock_timeout_s
    owned: AsyncEngine | None = None
    if config.async_engine is not None:
        if migration_url is not None:
            raise InvalidConfigurationError("'migration_url' cannot be combined with an injected engine.")
        engine = config.async_engine
        if backend is Backend.SQLITE and not is_prepared(engine):
            raise InvalidConfigurationError(
                "A SQLite engine must be prepared with install_transaction_control() before migrating."
            )
    else:
        cfg = config.with_url(migration_url) if migration_url is not None else config
        cfg = EngineConfig(database_url=cfg.database_url, lock_timeout_s=timeout, migration_mode=cfg.migration_mode)
        engine = owned = create_owned_engine(cfg, for_migration=True)
    try:
        try:
            conn = await engine.connect()
        except Exception as exc:
            # e.g. SQLite: another migrator holds the write lock while we set WAL on connect
            if isinstance(exc, sa.exc.DBAPIError) and isinstance(classify_db_error(exc), MigrationLockTimeoutError):
                raise MigrationLockTimeoutError() from None
            raise DatabaseUnavailableError() from None
        try:
            if backend is Backend.SQLITE:
                conn = await conn.execution_options(**{BEGIN_OPTION: "IMMEDIATE"})
            try:
                await conn.begin()
            except sa.exc.DBAPIError as exc:
                raise classify_db_error(exc) from None
            try:
                report = await run_upgrade(
                    conn,
                    backend,
                    lock_timeout=timeout,
                    script_location=Path(str(script_location)) if script_location else None,
                    target=target,
                )
                status = await read_schema_status(conn, backend)
                await conn.commit()
            except BaseException as exc:
                await conn.rollback()
                if isinstance(exc, sa.exc.DBAPIError):
                    raise classify_db_error(exc) from None
                raise
            return MigrationReport(
                from_revision=report.from_revision,
                to_revision=report.to_revision,
                applied=report.applied,
                already_current=report.already_current,
                to_level=status.schema_level or 0,
                duration_s=report.duration_s,
            )
        finally:
            await conn.close()
    except (EngineError, MigrationError, MigrationLockTimeoutError):
        raise
    except Exception:
        raise MigrationError() from None
    finally:
        if owned is not None:
            await owned.dispose()
