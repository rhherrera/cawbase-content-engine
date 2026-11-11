"""Creation of engine-owned AsyncEngines (URL mode and migrations)."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ..config import EngineConfig
from ..errors import InvalidConfigurationError
from .dialect import Backend
from .sqlite_support import install_transaction_control


def create_owned_engine(config: EngineConfig, *, for_migration: bool = False) -> AsyncEngine:
    """The engine owns this engine, so it may (and does) configure it."""
    assert config.database_url is not None
    url = config.database_url.get_secret_value()
    backend = config.backend
    kwargs: dict[str, Any] = {}
    if backend is Backend.POSTGRESQL:
        server_settings = {"application_name": "cawbase-content-engine"}
        if not for_migration:
            server_settings["statement_timeout"] = str(config.statement_timeout_ms)
        kwargs["connect_args"] = {"timeout": config.connect_timeout_s, "server_settings": server_settings}
        if for_migration:
            kwargs["poolclass"] = NullPool
        else:
            kwargs["pool_pre_ping"] = True
            for opt, key in (
                ("pool_size", "pool_size"),
                ("max_overflow", "max_overflow"),
                ("pool_timeout_s", "pool_timeout"),
            ):
                value = getattr(config, opt)
                if value is not None:
                    kwargs[key] = value
    else:
        timeout = config.lock_timeout_s if for_migration else config.statement_timeout_ms / 1000
        kwargs["connect_args"] = {"timeout": timeout}
        if for_migration:
            kwargs["poolclass"] = NullPool
    try:
        engine = create_async_engine(url, **kwargs)
    except ModuleNotFoundError:
        raise InvalidConfigurationError(
            "The database driver is not installed; install the 'postgres' or 'sqlite' extra."
        ) from None
    except Exception:
        raise InvalidConfigurationError("The database URL could not be used.") from None
    if backend is Backend.SQLITE:
        install_transaction_control(engine)
        busy_ms = int(timeout * 1000)

        @sa.event.listens_for(engine.sync_engine, "connect")
        def _pragmas(dbapi_connection: Any, _record: Any) -> None:
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute(f"PRAGMA busy_timeout={busy_ms}")
            if not for_migration:
                # WAL is persistent in the file. Switching needs an exclusive lock, so it is
                # best-effort: if another connection (e.g. a migrator) holds the lock, a later
                # connection of the pool will set it.
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                except Exception:
                    pass
            cur.close()

    return engine
