"""Foundational verification #1: SQLite real BEGIN, savepoints and outer rollback."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from cawbase_content_engine.persistence.sqlite_support import (
    driver_in_transaction,
    install_transaction_control,
    is_prepared,
)

pytestmark = pytest.mark.spike


async def _setup(url: str, prepared: bool):
    eng = create_async_engine(url)
    if prepared:
        install_transaction_control(eng)
    async with eng.begin() as c:
        await c.exec_driver_sql("CREATE TABLE IF NOT EXISTS t (v INTEGER)")
    return eng


async def _count(url: str) -> int:
    eng = create_async_engine(url)
    async with eng.connect() as c:
        n = (await c.execute(sa.text("SELECT count(*) FROM t"))).scalar_one()
    await eng.dispose()
    return int(n)


@pytest.fixture
def url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'spike.db'}"


async def test_legacy_mode_has_no_real_begin_before_savepoint(url):
    eng = await _setup(url, prepared=False)
    async with eng.connect() as c:
        await c.begin()
        assert await driver_in_transaction(c) is False  # SQLAlchemy says "in transaction", driver does not
        assert c.in_transaction()
    await eng.dispose()


async def test_legacy_savepoint_release_commits_outer_rollback_is_lost(url):
    """Documents why the engine must reject legacy mode: outer rollback does not undo the work."""
    eng = await _setup(url, prepared=False)
    async with eng.connect() as c:
        await c.begin()
        sp = await c.begin_nested()
        await c.execute(sa.text("INSERT INTO t VALUES (1)"))
        await sp.commit()  # RELEASE of an implicit outermost savepoint => COMMIT
        await c.rollback()
    await eng.dispose()
    assert await _count(url) == 1  # the "rolled back" row survived


async def test_prepared_mode_real_begin_and_outer_rollback(url):
    eng = await _setup(url, prepared=True)
    assert is_prepared(eng)
    async with eng.connect() as c:
        await c.begin()
        assert await driver_in_transaction(c) is True
        sp = await c.begin_nested()
        await c.execute(sa.text("INSERT INTO t VALUES (1)"))
        await sp.commit()
        assert await driver_in_transaction(c) is True  # RELEASE did not end the outer transaction
        await c.rollback()
    await eng.dispose()
    assert await _count(url) == 0


async def test_prepared_savepoint_rollback_keeps_outer_usable(url):
    eng = await _setup(url, prepared=True)
    async with eng.connect() as c:
        await c.begin()
        await c.execute(sa.text("INSERT INTO t VALUES (1)"))
        sp = await c.begin_nested()
        await c.execute(sa.text("INSERT INTO t VALUES (2)"))
        await sp.rollback()
        await c.execute(sa.text("INSERT INTO t VALUES (3)"))
        await c.commit()
    await eng.dispose()
    assert await _count(url) == 2


async def test_begin_immediate_takes_write_lock(url):
    eng = await _setup(url, prepared=True)
    async with eng.connect() as c1:
        await (await c1.execution_options(sqlite_begin="IMMEDIATE")).begin()
        eng2 = create_async_engine(url, connect_args={"timeout": 0.2})
        install_transaction_control(eng2)
        async with eng2.connect() as c2:
            c2o = await c2.execution_options(sqlite_begin="IMMEDIATE")
            with pytest.raises(sa.exc.OperationalError):
                await c2o.begin()
        await eng2.dispose()
        await c1.rollback()
    await eng.dispose()
