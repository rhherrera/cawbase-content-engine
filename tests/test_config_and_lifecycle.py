from __future__ import annotations

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from cawbase_content_engine import (
    ContentEngine,
    EngineConfig,
    EngineStateError,
    InvalidConfigurationError,
    Limits,
    SchemaIncompatibleError,
    SchemaMissingError,
    install_transaction_control,
)
from cawbase_content_engine.migrations import upgrade

from .helpers import make_host_engine, table_names

URL = "sqlite+aiosqlite:////tmp/never-opened.db"


class TestConfig:  # AC-12, AC-13
    def test_ambiguous_url_and_engine(self):
        eng = create_async_engine(URL)
        with pytest.raises(InvalidConfigurationError, match="Ambiguous"):
            EngineConfig(database_url=URL, async_engine=eng)

    def test_none_given(self):
        with pytest.raises(InvalidConfigurationError):
            EngineConfig()

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p@h/db",
            "mysql+aiomysql://u:p@h/db",
            "sqlite:///x.db",
            "postgresql+psycopg://u:p@h/d",
            "sqlite+aiosqlite:///:memory:",
            "not a url",
        ],
    )
    def test_unsupported_urls(self, url):
        with pytest.raises(InvalidConfigurationError):
            EngineConfig(database_url=url)

    def test_pool_options_with_injected_engine(self):
        with pytest.raises(InvalidConfigurationError, match="Pool options"):
            EngineConfig(async_engine=create_async_engine(URL), pool_size=3)

    def test_sync_engine_rejected(self):
        with pytest.raises(InvalidConfigurationError):
            EngineConfig(async_engine=sa.create_engine("sqlite://"))  # type: ignore[arg-type]

    def test_limits_validated(self):
        with pytest.raises(InvalidConfigurationError):
            Limits(max_body_bytes=0)
        with pytest.raises(InvalidConfigurationError):
            EngineConfig(database_url=URL, lock_timeout_s=0)

    def test_secret_never_in_repr_or_errors(self):
        secret = "sup3r-s3cret-pw"
        cfg = EngineConfig(database_url=f"postgresql+asyncpg://user:{secret}@db.example/x")
        assert secret not in repr(cfg) and secret not in str(cfg)
        assert isinstance(cfg.database_url, SecretStr) and secret not in repr(cfg.database_url)
        with pytest.raises(InvalidConfigurationError) as e:
            EngineConfig(database_url=f"mysql://user:{secret}@db.example/x")
        assert secret not in str(e.value) and secret not in repr(e.value)

    def test_from_env_precedence_and_file(self, tmp_path):
        f = tmp_path / "u"
        f.write_text("sqlite+aiosqlite:////tmp/a.db\n")
        env = {"CAWBASE_CONTENT_ENGINE_DATABASE_URL_FILE": str(f), "CAWBASE_CONTENT_ENGINE_LOCK_TIMEOUT_S": "7"}
        cfg = EngineConfig.from_env(env)
        assert cfg.database_url.get_secret_value().endswith("a.db") and cfg.lock_timeout_s == 7
        assert EngineConfig.from_env(env, lock_timeout_s=9).lock_timeout_s == 9  # explicit > env
        with pytest.raises(InvalidConfigurationError):
            EngineConfig.from_env({**env, "CAWBASE_CONTENT_ENGINE_DATABASE_URL": URL})  # both URL and _FILE

    def test_construction_does_no_io(self, tmp_path):
        path = tmp_path / "nope" / "x.db"
        ContentEngine(EngineConfig(database_url=f"sqlite+aiosqlite:///{path}"))
        assert not path.parent.exists()


class TestLifecycle:
    async def test_start_on_empty_database_fails_without_migrating(self, db):  # AC-28
        backend, url = db
        eng = ContentEngine(EngineConfig(database_url=url))
        with pytest.raises(SchemaMissingError):
            await eng.start()
        assert not any("engine_state" in n for n in await table_names(url)), "start() must never migrate"
        await eng.aclose()

    async def test_url_mode_start_and_close_dispose_pool(self, migrated):  # AC-10
        _, url = migrated
        eng = ContentEngine(EngineConfig(database_url=url))
        await eng.start()
        await eng.start()  # second start is a no-op
        sync_engine = eng._engine.sync_engine
        await eng.schema_status()
        await eng.aclose()
        await eng.aclose()  # repeatable
        pool = sync_engine.pool
        if hasattr(pool, "checkedout"):  # NullPool (SQLAlchemy 2.0 + aiosqlite file) has no checked-out accounting
            assert pool.checkedout() == 0
        assert eng._engine is None
        with pytest.raises(EngineStateError):
            await eng.start()

    async def test_injected_engine_is_never_closed(self, migrated):  # AC-11
        backend, url = migrated
        host = await make_host_engine(backend, url)
        eng = ContentEngine(EngineConfig(async_engine=host))
        await eng.start()
        await eng.aclose()
        async with host.connect() as c:  # still fully usable by the host
            assert (await c.execute(sa.text("SELECT 1"))).scalar_one() == 1
        await host.dispose()

    async def test_operations_after_close_fail(self, migrated):
        _, url = migrated
        eng = ContentEngine(EngineConfig(database_url=url))
        await eng.start()
        ctx = eng.trusted_admin_context(["admin:scopes:read"])
        ops = eng.admin(ctx)
        await eng.aclose()
        with pytest.raises(EngineStateError):
            await ops.list_scopes()

    async def test_incompatible_schema_levels(self, migrated, monkeypatch):  # AC-27
        backend, url = migrated
        eng = ContentEngine(EngineConfig(database_url=url))
        await eng.start()
        await eng.aclose()
        # runtime requires a newer schema than installed -> outdated
        monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 2)
        with pytest.raises(SchemaIncompatibleError):
            await ContentEngine(EngineConfig(database_url=url)).start()
        monkeypatch.undo()
        # schema declares it needs a newer runtime than this one
        adm = create_async_engine(url)
        tbl = (
            "cawbase_content_engine.engine_state" if backend == "postgresql" else "cawbase_content_engine_engine_state"
        )
        async with adm.begin() as c:
            await c.execute(sa.text(f"UPDATE {tbl} SET min_runtime_level = 99"))
        await adm.dispose()
        e2 = ContentEngine(EngineConfig(database_url=url))
        with pytest.raises(SchemaIncompatibleError):
            await e2.start()
        assert (await e2.readiness()).ready is False

    async def test_newer_schema_with_compatible_runtime_is_ready(self, migrated):  # rolling deploys
        backend, url = migrated
        adm = create_async_engine(url)
        tbl = (
            "cawbase_content_engine.engine_state" if backend == "postgresql" else "cawbase_content_engine_engine_state"
        )
        async with adm.begin() as c:
            await c.execute(sa.text(f"UPDATE {tbl} SET schema_level = 5"))
        await adm.dispose()
        async with ContentEngine(EngineConfig(database_url=url)) as e:
            assert (await e.readiness()).ready

    async def test_sqlite_injected_engine_without_real_begin_is_rejected(self, tmp_path):  # AC-84/85
        url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
        await upgrade(EngineConfig(database_url=url))
        legacy = create_async_engine(url)  # legacy sqlite3 transaction control
        before = len(legacy.sync_engine.dispatch.begin)
        eng = ContentEngine(EngineConfig(async_engine=legacy))
        with pytest.raises(InvalidConfigurationError, match="real BEGIN"):
            await eng.start()
        assert len(legacy.sync_engine.dispatch.begin) == before, "the host's engine must not be modified"
        # after the HOST opts in, it works
        install_transaction_control(legacy)
        legacy.sync_engine.dispose()
        await ContentEngine(EngineConfig(async_engine=legacy)).start()
        await legacy.dispose()

    async def test_postgres_injected_engine_with_wrong_isolation_is_rejected(self, db):  # AC-85
        backend, url = db
        if backend != "postgresql":
            pytest.skip("PostgreSQL only")
        await upgrade(EngineConfig(database_url=url))
        host = create_async_engine(url, isolation_level="REPEATABLE READ")
        with pytest.raises(InvalidConfigurationError, match="READ COMMITTED"):
            await ContentEngine(EngineConfig(async_engine=host)).start()
        await host.dispose()


async def test_sqlite_below_the_documented_floor_is_rejected(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 37, 2))
    eng = ContentEngine(EngineConfig(database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}"))
    with pytest.raises(InvalidConfigurationError, match="SQLite 3.38"):
        await eng.start()
