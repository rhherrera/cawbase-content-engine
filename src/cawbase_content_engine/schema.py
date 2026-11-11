"""Schema status and compatibility (D-16, persistence "Compatibilidad")."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from .constants import RUNTIME_LEVEL, SCHEMA_LEVEL, SCHEMA_NAME, SQLITE_PREFIX
from .models import SchemaStatus
from .persistence.dialect import Backend

if TYPE_CHECKING:
    from .config import EngineConfig


async def read_schema_status(conn: AsyncConnection, backend: Backend) -> SchemaStatus:
    if backend is Backend.POSTGRESQL:
        present = (
            await conn.execute(sa.text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f'"{SCHEMA_NAME}".engine_state'})
        ).scalar_one()
        table = f'"{SCHEMA_NAME}".engine_state'
    else:
        present = (
            await conn.execute(
                sa.text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name = :n"),
                {"n": SQLITE_PREFIX + "engine_state"},
            )
        ).scalar_one() > 0
        table = SQLITE_PREFIX + "engine_state"
    if not present:
        return SchemaStatus(present=False, reason="schema_missing")
    row = (await conn.execute(sa.text(f"SELECT schema_level, min_runtime_level FROM {table} WHERE id = 1"))).first()  # noqa: S608 - constant name
    if row is None:
        return SchemaStatus(present=True, reason="schema_incompatible")
    level, min_runtime = int(row[0]), int(row[1])
    if level < SCHEMA_LEVEL:
        reason = "schema_outdated"
    elif min_runtime > RUNTIME_LEVEL:
        reason = "runtime_too_old"
    else:
        return SchemaStatus(present=True, schema_level=level, min_runtime_level=min_runtime, compatible=True)
    return SchemaStatus(present=True, schema_level=level, min_runtime_level=min_runtime, reason=reason)


async def check_schema(config: EngineConfig) -> SchemaStatus:
    """Read-only schema inspection (never migrates). Creates and disposes an engine in URL mode."""
    from .persistence.engines import create_owned_engine
    from .transactions import run_own

    engine = config.async_engine or create_owned_engine(config)
    try:
        return await run_own(engine, config.backend, lambda c: read_schema_status(c, config.backend), write=False)
    finally:
        if config.owns_engine:
            await engine.dispose()
