"""Own and host-provided transactions (D-13, D-23): AC-14..19, AC-80..85."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from cawbase_content_engine import (
    ConflictError,
    ContentEngine,
    EngineConfig,
    HostTransaction,
    IncompatibleTransactionError,
    ResourceNotFoundError,
    TransactionBrokenError,
    TransactionStateError,
)
from cawbase_content_engine.transactions import run_in_host

from .helpers import admin_ctx, make_host_engine


@pytest.fixture
async def host(migrated):
    """Engine in injected mode + the host's own engine and a host-owned table."""
    backend, url = migrated
    host_engine = await make_host_engine(backend, url)
    eng = ContentEngine(EngineConfig(async_engine=host_engine))
    await eng.start()
    async with host_engine.begin() as c:
        await c.execute(sa.text("CREATE TABLE host_items (v INTEGER)"))
    yield backend, eng, host_engine
    await eng.aclose()
    await host_engine.dispose()


async def host_count(host_engine) -> int:
    async with host_engine.connect() as c:
        return (await c.execute(sa.text("SELECT count(*) FROM host_items"))).scalar_one()


async def scope_exists(eng: ContentEngine, scope_id: str) -> bool:
    try:
        await eng.admin(await admin_ctx(eng)).get_scope(scope_id)
        return True
    except ResourceNotFoundError:
        return False


class TestOwnTransactions:
    async def test_commit_visible_to_other_connections(self, engine):  # AC-14
        admin = engine.admin(await admin_ctx(engine))
        await admin.create_scope("a", "A")
        other = ContentEngine(engine.config)
        await other.start()
        assert await scope_exists(other, "a")
        await other.aclose()

    async def test_exception_rolls_back_everything(self, engine):  # AC-14
        from cawbase_content_engine.operations import scopes as sc

        async def boom(c):
            await sc.create_scope(
                c,
                engine.tables,
                scope_id="half",
                display_name="H",
                description=None,
                metadata={},
                actor_key_id=None,
                actor_label=None,
            )
            raise RuntimeError("after write")

        with pytest.raises(Exception):  # noqa: B017 - translated to InternalError
            await engine._run(boom, write=True, tx=None)
        assert not await scope_exists(engine, "half")

    async def test_concurrent_tasks_are_independent(self, engine):  # AC-19
        admin = engine.admin(await admin_ctx(engine))
        results = await asyncio.gather(*[admin.create_scope(f"s{i}", f"S{i}") for i in range(12)])
        assert all(r.created for r in results)
        assert len((await admin.list_scopes(limit=50)).items) == 12


class TestHostTransactions:
    async def test_commit_confirms_both(self, host):  # AC-15
        backend, eng, host_engine = host
        admin = eng.admin(
            await admin_ctx(eng),
        )
        async with host_engine.begin() as conn:
            tx = HostTransaction(conn)
            await eng.admin(await admin_ctx(eng), transaction=tx).create_scope("h1", "H1")
            await conn.execute(sa.text("INSERT INTO host_items VALUES (1)"))
            assert await scope_exists(eng, "h1") is False, (
                "uncommitted work is not visible to other connections (AC-18)"
            )
        assert await scope_exists(eng, "h1") and await host_count(host_engine) == 1
        _ = admin

    async def test_host_rollback_reverts_the_engines_work_after_savepoint_release(self, host):  # AC-15, AC-80
        backend, eng, host_engine = host
        with pytest.raises(RuntimeError):
            async with host_engine.begin() as conn:
                tx = HostTransaction(conn)
                await eng.admin(await admin_ctx(eng), transaction=tx).create_scope("h2", "H2")  # savepoint released
                await conn.execute(sa.text("INSERT INTO host_items VALUES (1)"))
                raise RuntimeError("host decides to roll back")
        assert not await scope_exists(eng, "h2") and await host_count(host_engine) == 0

    async def test_recoverable_error_is_contained_and_host_can_continue(self, host):  # AC-16, AC-81
        backend, eng, host_engine = host
        ops = lambda tx: eng.admin(
            eng.trusted_admin_context(list(__import__("cawbase_content_engine").AdminPermission)), transaction=tx
        )  # noqa: E731
        async with host_engine.begin() as conn:
            tx = HostTransaction(conn)
            await ops(tx).create_scope("dup", "One")
            with pytest.raises(ConflictError):
                await ops(tx).create_scope("dup", "Different")  # recoverable: savepoint rolled back
            await conn.execute(sa.text("INSERT INTO host_items VALUES (7)"))  # host transaction still healthy
            await ops(tx).create_scope("after", "After")
        assert (
            await scope_exists(eng, "dup") and await scope_exists(eng, "after") and await host_count(host_engine) == 1
        )

    async def test_db_level_error_does_not_poison_the_host_transaction(self, host):  # AC-81 (PostgreSQL aborts txns)
        backend, eng, host_engine = host

        async def bad_sql(c: AsyncConnection):
            await c.execute(sa.text("SELECT * FROM table_that_does_not_exist"))

        async with host_engine.begin() as conn:
            tx = HostTransaction(conn)
            with pytest.raises(Exception):  # noqa: B017
                await eng._run(bad_sql, write=True, tx=tx)
            await conn.execute(sa.text("INSERT INTO host_items VALUES (1)"))  # still usable
        assert await host_count(host_engine) == 1

    async def test_inactive_transaction_is_rejected(self, host):  # AC-17
        _, eng, host_engine = host
        async with host_engine.connect() as conn:
            with pytest.raises(TransactionStateError):
                HostTransaction(conn)
        with pytest.raises(TransactionStateError):
            HostTransaction(object())  # type: ignore[arg-type]

    async def test_transaction_ended_before_use(self, host):  # AC-17
        _, eng, host_engine = host
        async with host_engine.connect() as conn:
            await conn.begin()
            tx = HostTransaction(conn)
            await conn.rollback()
            with pytest.raises(TransactionStateError):
                await eng.admin(await admin_ctx(eng), transaction=tx).list_scopes()

    async def test_connection_from_another_engine_and_url_mode_are_incompatible(self, host, migrated):  # AC-17
        backend, eng, host_engine = host
        other = await make_host_engine(backend, migrated[1])
        async with other.connect() as conn:
            await conn.begin()
            tx = HostTransaction(conn)
            with pytest.raises(IncompatibleTransactionError):
                await eng.admin(await admin_ctx(eng), transaction=tx).list_scopes()
            url_mode = ContentEngine(EngineConfig(database_url=migrated[1]))
            await url_mode.start()
            with pytest.raises(IncompatibleTransactionError):
                await url_mode.admin(await admin_ctx(url_mode), transaction=tx).list_scopes()
            await url_mode.aclose()
            await conn.rollback()
        await other.dispose()

    async def test_two_wrappers_of_the_same_connection_cannot_run_simultaneously(self, host):  # AC-83
        _, eng, host_engine = host
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(c: AsyncConnection):
            entered.set()
            await release.wait()

        async def fast(c: AsyncConnection):
            return 1

        async with host_engine.begin() as conn:
            tx1, tx2 = HostTransaction(conn), HostTransaction(conn)
            first = asyncio.create_task(eng._run(slow, write=True, tx=tx1))
            await entered.wait()
            with pytest.raises(TransactionStateError):
                await eng._run(fast, write=True, tx=tx2)
            release.set()
            await first
            assert await eng._run(fast, write=True, tx=tx2) == 1  # free again afterwards

    async def test_lost_connection_yields_transaction_broken(self, host):  # AC-82
        backend, eng, host_engine = host
        async with host_engine.connect() as conn:
            await conn.begin()
            tx = HostTransaction(conn)
            raw = await conn.get_raw_connection()
            if backend == "postgresql":
                killer = create_async_engine(str(host_engine.url.render_as_string(hide_password=False)))
                async with killer.connect() as k:
                    pid = (await conn.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()
                    await k.execute(sa.text("SELECT pg_terminate_backend(:p)"), {"p": pid})
                    await k.commit()
                await killer.dispose()
                await asyncio.sleep(0.2)
            else:
                await raw.driver_connection.close()  # connection vanishes under the host
            with pytest.raises(TransactionBrokenError):
                await eng.admin(await admin_ctx(eng), transaction=tx).list_scopes()
            await conn.invalidate()  # the host discards the dead connection (older SQLAlchemy raises on close)

    async def test_failing_savepoint_rollback_yields_transaction_broken(self, host, monkeypatch):  # AC-82
        _, eng, host_engine = host
        from sqlalchemy.ext.asyncio import AsyncTransaction

        async def explode(self):
            raise RuntimeError("cannot roll back savepoint")

        async def bad(c):
            raise ValueError("domain failure")

        async with host_engine.connect() as conn:
            await conn.begin()
            tx = HostTransaction(conn)
            monkeypatch.setattr(AsyncTransaction, "rollback", explode)
            with pytest.raises(TransactionBrokenError):
                await eng._run(bad, write=True, tx=tx)
            monkeypatch.undo()
            await conn.rollback()

    async def test_aborted_postgres_transaction_is_reported_broken(self, host):  # AC-82
        backend, eng, host_engine = host
        if backend != "postgresql":
            pytest.skip("PostgreSQL only")
        async with host_engine.connect() as conn:
            await conn.begin()
            tx = HostTransaction(conn)
            with pytest.raises(Exception):  # noqa: B017
                await conn.execute(sa.text("SELECT * FROM nope"))  # host's own mistake aborts the txn
            with pytest.raises(TransactionBrokenError):
                await eng.admin(await admin_ctx(eng), transaction=tx).list_scopes()
            await conn.rollback()

    async def test_sqlite_legacy_transaction_control_is_rejected_without_side_effects(self, tmp_path):  # AC-84
        from cawbase_content_engine.migrations import upgrade
        from cawbase_content_engine.persistence.dialect import Backend

        url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
        await upgrade(EngineConfig(database_url=url))
        legacy = create_async_engine(url)  # not prepared: BEGIN is lazy
        ran = []

        async def fn(c):
            ran.append(1)

        async with legacy.connect() as conn:
            await conn.begin()
            tx = HostTransaction(conn)
            with pytest.raises(IncompatibleTransactionError):
                await run_in_host(legacy, Backend.SQLITE, tx, fn)
            await conn.rollback()
        assert ran == []
        await legacy.dispose()

    async def test_no_second_connection_is_taken_in_host_mode(self, migrated):  # pool of one: would deadlock
        backend, url = migrated
        kwargs = {"pool_size": 1, "max_overflow": 0, "pool_timeout": 2} if backend == "postgresql" else {}
        host_engine = await make_host_engine(backend, url, **kwargs)
        eng = ContentEngine(EngineConfig(async_engine=host_engine))
        await eng.start()
        async with host_engine.begin() as conn:
            tx = HostTransaction(conn)
            await eng.admin(await admin_ctx(eng), transaction=tx).create_scope("solo", "Solo")
            assert (await eng.admin(await admin_ctx(eng), transaction=tx).get_scope("solo")).scope_id == "solo"
        await eng.aclose()
        await host_engine.dispose()
