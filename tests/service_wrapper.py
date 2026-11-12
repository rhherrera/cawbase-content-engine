"""Test-only launcher: the REAL ``serve()`` with one extra step after preparation.

Run as ``python tests/service_wrapper.py``. After the real preparation (migrate + verify) succeeds and
before workers start, it makes the schema incompatible with this runtime (``min_runtime_level = 99``),
simulating another deployment migrating underneath. Everything else is the product's code path.
The ``__main__`` guard matters: multiprocessing "spawn" re-imports this module in every worker.
"""

from __future__ import annotations

import sys

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

import cawbase_content_engine.http.service as service


async def _break_schema(settings: service.ServiceSettings) -> None:
    assert settings.config.database_url is not None
    url = settings.config.database_url.get_secret_value()
    table = (
        "cawbase_content_engine.engine_state" if url.startswith("postgresql") else "cawbase_content_engine_engine_state"
    )
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"UPDATE {table} SET min_runtime_level = 99"))
    await engine.dispose()


if __name__ == "__main__":
    real_prepare = service.prepare

    async def prepare_then_break(settings: service.ServiceSettings) -> None:
        await real_prepare(settings)
        await _break_schema(settings)

    service.prepare = prepare_then_break
    sys.exit(service.serve(service.ServiceSettings.from_env()))
