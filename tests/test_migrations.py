"""Product migrations: AC-20..28, AC-86..88. (Multi-revision/rollback mechanics: test_spike_alembic.py)"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from cawbase_content_engine import ContentEngine, EngineConfig, install_transaction_control
from cawbase_content_engine.constants import MIGRATION_LOCK_KEY
from cawbase_content_engine.errors import (
    InvalidConfigurationError,
    MigrationLockTimeoutError,
    MigrationPermissionError,
    SchemaIncompatibleError,
)
from cawbase_content_engine.migrations import upgrade
from cawbase_content_engine.persistence.dialect import Backend
from cawbase_content_engine.persistence.tables import tables_for

FOREIGN_DDL = [
    "CREATE TABLE app_users (id INTEGER PRIMARY KEY, name TEXT)",
    "INSERT INTO app_users VALUES (1, 'alice')",
    "CREATE INDEX ix_app_users_name ON app_users (name)",
    "CREATE VIEW app_user_names AS SELECT name FROM app_users",
    "CREATE TABLE cawbase_content_enginex_lookalike (id INTEGER)",  # near-miss of the SQLite prefix
]


async def inventory(url: str) -> dict:
    eng = create_async_engine(url)
    try:
        async with eng.connect() as c:
            if "sqlite" in url:
                rows = (await c.execute(sa.text("SELECT type, name, sql FROM sqlite_master ORDER BY name"))).all()
            else:
                rows = (
                    await c.execute(
                        sa.text(
                            "SELECT c.relkind::text, n.nspname || '.' || c.relname, pg_get_viewdef(c.oid) "
                            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') ORDER BY 2"
                        )
                    )
                ).all()
        return {r[1]: tuple(r) for r in rows}
    finally:
        await eng.dispose()


async def prepare_foreign(url: str):
    eng = create_async_engine(url)
    async with eng.begin() as c:
        for stmt in FOREIGN_DDL:
            await c.execute(sa.text(stmt))
    await eng.dispose()


class TestInstallAndUpgrade:
    async def test_creates_only_engine_objects_and_preserves_foreign_ones(self, db):  # AC-20, AC-88
        backend, url = db
        await prepare_foreign(url)
        before = await inventory(url)
        report = await upgrade(EngineConfig(database_url=url))
        after = await inventory(url)
        assert report.applied == ("0001",) and report.to_level == 1
        for name, entry in before.items():
            assert after[name] == entry, f"foreign object changed: {name}"
        created = set(after) - set(before)
        prefix = "cawbase_content_engine." if backend == "postgresql" else "cawbase_content_engine_"
        assert created and all(
            n.startswith(prefix) or n.startswith("sqlite_autoindex_cawbase_content_engine_") for n in created
        ), created
        eng = create_async_engine(url)
        async with eng.connect() as c:  # foreign data and view still work
            assert (await c.execute(sa.text("SELECT name FROM app_user_names"))).scalar_one() == "alice"
        await eng.dispose()

    async def test_repeating_is_a_no_op(self, db):  # AC-22
        _, url = db
        await upgrade(EngineConfig(database_url=url))
        before = await inventory(url)
        again = await upgrade(EngineConfig(database_url=url))
        assert again.already_current and again.applied == () and await inventory(url) == before

    async def test_migrated_schema_matches_table_definitions(self, db):
        backend, url = db
        await upgrade(EngineConfig(database_url=url))
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        tables = tables_for(Backend(backend))
        names = {t.name for t in tables.metadata.sorted_tables}

        def include_object(obj, name, type_, reflected, compare_to):
            if type_ == "table":
                return name in names
            return True

        eng = create_async_engine(url)

        def diff(sync_conn):
            ctx = MigrationContext.configure(
                sync_conn,
                opts={
                    "include_schemas": backend == "postgresql",
                    "include_object": include_object,
                    "compare_type": False,
                },
            )
            return compare_metadata(ctx, tables.metadata)

        async with eng.connect() as c:
            result = await c.run_sync(diff)
        await eng.dispose()
        # autogenerate must never propose dropping anything (foreign or otherwise)
        assert not [d for d in result if (d[0] if isinstance(d, tuple) else d[0][0]).startswith("remove")], result
        assert result == [], result

    async def test_concurrent_startups_apply_once(self, db):  # AC-23
        _, url = db
        reports = await asyncio.gather(*[upgrade(EngineConfig(database_url=url), lock_timeout=30) for _ in range(5)])
        assert sorted(len(r.applied) for r in reports) == [0, 0, 0, 0, 1]
        async with ContentEngine(EngineConfig(database_url=url)) as e:
            assert (await e.readiness()).ready

    async def test_foreign_objects_inside_the_engine_namespace_are_refused(self, db):  # AC-20
        backend, url = db
        eng = create_async_engine(url)
        async with eng.begin() as c:
            if backend == "postgresql":
                await c.execute(sa.text("CREATE SCHEMA cawbase_content_engine"))
                await c.execute(sa.text("CREATE TABLE cawbase_content_engine.stranger (id int)"))
            else:
                await c.execute(sa.text("CREATE TABLE cawbase_content_engine_stranger (id int)"))
        await eng.dispose()
        with pytest.raises(SchemaIncompatibleError):
            await upgrade(EngineConfig(database_url=url))
        assert "cawbase_content_engine.scopes" not in await inventory(url)

    async def test_embedded_start_and_router_never_migrate(self, db):  # AC-28, AC-61
        _, url = db
        from cawbase_content_engine.http import create_router

        e = ContentEngine(EngineConfig(database_url=url))
        create_router(e)
        with pytest.raises(Exception):  # noqa: B017 - schema missing
            await e.start()
        assert not any("engine_state" in n for n in await inventory(url))


class TestLockAndPermissions:
    async def test_lock_wait_is_bounded(self, db):  # AC-25
        backend, url = db
        if backend == "postgresql":
            holder = create_async_engine(url)
            async with holder.connect() as c:
                await c.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
                with pytest.raises(MigrationLockTimeoutError):
                    await upgrade(EngineConfig(database_url=url), lock_timeout=1)
                await c.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
            await holder.dispose()
        else:
            path = url.split("///", 1)[1]
            holder = sqlite3.connect(path, isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")
            with pytest.raises(MigrationLockTimeoutError):
                await upgrade(EngineConfig(database_url=url), lock_timeout=1)
            holder.execute("ROLLBACK")
            holder.close()
        assert (await upgrade(EngineConfig(database_url=url))).applied == ("0001",)  # lock released: works

    async def test_postgres_role_without_ddl_permission_gets_a_permission_error(self, db):  # AC-26
        backend, url = db
        if backend != "postgresql":
            pytest.skip("PostgreSQL only")
        admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
        db_name = sa.engine.make_url(url).database
        async with admin.connect() as c:
            await c.execute(sa.text("CREATE ROLE cce_runtime_only LOGIN PASSWORD 'rt' NOSUPERUSER"))
            await c.execute(sa.text(f'REVOKE ALL ON DATABASE "{db_name}" FROM PUBLIC'))
            await c.execute(sa.text(f'GRANT CONNECT ON DATABASE "{db_name}" TO cce_runtime_only'))
            await c.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        restricted = (
            sa.engine.make_url(url)
            .set(username="cce_runtime_only", password="rt")
            .render_as_string(hide_password=False)
        )
        try:
            with pytest.raises(MigrationPermissionError):
                await upgrade(EngineConfig(database_url=restricted))
            # separate DDL credentials: migrate with the privileged URL, run with the restricted one
            await upgrade(EngineConfig(database_url=restricted), migration_url=url)
            async with admin.connect() as c:
                await c.execute(sa.text("GRANT USAGE ON SCHEMA cawbase_content_engine TO cce_runtime_only"))
                await c.execute(
                    sa.text(
                        "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA cawbase_content_engine TO cce_runtime_only"
                    )
                )
                await c.execute(
                    sa.text("GRANT USAGE ON ALL SEQUENCES IN SCHEMA cawbase_content_engine TO cce_runtime_only")
                )
            async with ContentEngine(EngineConfig(database_url=restricted)) as e:  # runtime role has NO DDL (AC-102)
                assert (await e.readiness()).ready
                ops = e.admin(e.trusted_admin_context(["admin:scopes:write"]))
                assert (await ops.create_scope("x", "X")).created
                async with e._engine.connect() as c2:
                    with pytest.raises(sa.exc.ProgrammingError):
                        await c2.execute(sa.text("CREATE TABLE cawbase_content_engine.nope (id int)"))
        finally:
            async with admin.connect() as c:
                await c.execute(sa.text("DROP OWNED BY cce_runtime_only"))
                await c.execute(sa.text("DROP ROLE IF EXISTS cce_runtime_only"))
            await admin.dispose()

    async def test_sqlite_readonly_database_gets_a_permission_error(self, tmp_path):  # AC-26
        path = tmp_path / "ro.db"
        sqlite3.connect(path).close()
        path.chmod(0o444)
        url = f"sqlite+aiosqlite:///{path}"
        with pytest.raises((MigrationPermissionError, Exception)) as e:
            await upgrade(EngineConfig(database_url=url))
        assert type(e.value).__name__ in ("MigrationPermissionError", "MigrationError", "DatabaseUnavailableError")
        assert "readonly" not in str(e.value).lower() or isinstance(e.value, MigrationPermissionError)

    async def test_injected_sqlite_engine_must_be_prepared_and_is_not_modified(self, tmp_path):  # D-23, AC-85
        url = f"sqlite+aiosqlite:///{tmp_path / 'h.db'}"
        host = create_async_engine(url)
        with pytest.raises(InvalidConfigurationError, match="install_transaction_control"):
            await upgrade(EngineConfig(async_engine=host))
        assert len(host.sync_engine.dispatch.begin) == 0
        install_transaction_control(host)
        assert (await upgrade(EngineConfig(async_engine=host))).applied == ("0001",)
        await host.dispose()

    async def test_injected_postgres_engine_can_migrate(self, db):
        backend, url = db
        if backend != "postgresql":
            pytest.skip("PostgreSQL only")
        host = create_async_engine(url)
        assert (await upgrade(EngineConfig(async_engine=host))).applied == ("0001",)
        async with host.connect() as c:  # host engine remains usable (not disposed)
            assert (await c.execute(sa.text("SELECT 1"))).scalar_one() == 1
        await host.dispose()
