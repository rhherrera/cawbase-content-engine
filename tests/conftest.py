"""Shared fixtures. PostgreSQL tests run against throw-away databases created on a test server.

``CCE_TEST_POSTGRES_URL`` points at an *admin* URL of a disposable server (never a real database).
Every test gets its own freshly created database (``cce_t_<uuid>``) that is dropped afterwards;
nothing else on the server is touched. Without the variable PostgreSQL tests are skipped (a skip is
not a pass) unless ``CCE_REQUIRE_POSTGRES=1`` is set, in which case they fail.
"""

from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


def pytest_report_header(config):
    """Print what the tests really run on (the sqlite3 library is the one aiosqlite uses)."""
    import platform
    import sqlite3

    return [
        f"platform: {platform.platform()} | python {platform.python_version()}",
        f"sqlite3.sqlite_version (in this process): {sqlite3.sqlite_version}",
        f"postgres required: {os.environ.get('CCE_REQUIRE_POSTGRES') == '1'} | url set: {bool(os.environ.get('CCE_TEST_POSTGRES_URL'))}",
    ]


PG_ADMIN_URL = os.environ.get("CCE_TEST_POSTGRES_URL")
REQUIRE_PG = os.environ.get("CCE_REQUIRE_POSTGRES") == "1"


@pytest.fixture(params=["sqlite", "postgresql"])
async def db(request, tmp_path):
    """Yields ``(backend, url)`` of an isolated, empty database."""
    if request.param == "sqlite":
        yield "sqlite", f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}"
        return
    if not PG_ADMIN_URL:
        if REQUIRE_PG:
            pytest.fail("CCE_TEST_POSTGRES_URL is required (CCE_REQUIRE_POSTGRES=1)")
        pytest.skip("CCE_TEST_POSTGRES_URL not set")
    name = f"cce_t_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = make_url(PG_ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    try:
        yield "postgresql", url
    finally:
        async with admin.connect() as c:
            await c.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.fixture
async def migrated(db):
    from cawbase_content_engine import EngineConfig
    from cawbase_content_engine.migrations import upgrade

    backend, url = db
    await upgrade(EngineConfig(database_url=url))
    return backend, url


@pytest.fixture
async def engine(migrated):
    from cawbase_content_engine import ContentEngine, EngineConfig

    _, url = migrated
    eng = ContentEngine(EngineConfig(database_url=url))
    await eng.start()
    yield eng
    await eng.aclose()


@pytest.fixture
async def pg_url():
    """A fresh, empty, disposable PostgreSQL database (PostgreSQL-only tests)."""
    if not PG_ADMIN_URL:
        if REQUIRE_PG:
            pytest.fail("CCE_TEST_POSTGRES_URL is required (CCE_REQUIRE_POSTGRES=1)")
        pytest.skip("CCE_TEST_POSTGRES_URL not set")
    name = f"cce_t_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(PG_ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        async with admin.connect() as c:
            await c.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
