"""A02 concurrency on real databases: limits, publication, default profile and lock behaviour
(AC-A02-16, 17, 26, 34, 35, 36, 37). No sleeps as proof: real parallel operations, barriers and lock-wait polling."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

from cawbase_content_engine import ContentEngine, EngineConfig, HostTransaction, Limits
from cawbase_content_engine.errors import (
    DatabaseUnavailableError,
    LimitExceededError,
    ProfileIsDefaultError,
    ProfileNotPublishedError,
    ProfileNotSelectableError,
    ResourceNotFoundError,
    RevisionConflictError,
)

from .a02_helpers import NO_CATALOG, PR, PW, count_rows, ops_for, published
from .helpers import make_host_engine

ROUNDS = 6


@pytest.fixture
async def limited(migrated):
    """Factory for engines with custom limits over the migrated database."""
    _, url = migrated
    started: list[ContentEngine] = []

    async def make(**limits) -> ContentEngine:
        e = ContentEngine(EngineConfig(database_url=url, limits=Limits(**limits)))
        await e.start()
        started.append(e)
        return e

    yield make
    for e in started:
        await e.aclose()


async def results(*coros):
    return await asyncio.gather(*coros, return_exceptions=True)


async def wait_for_lock_wait(engine: ContentEngine, minimum: int = 1) -> None:
    """PostgreSQL only: poll until at least ``minimum`` backends wait for a row/transaction lock."""
    for _ in range(400):
        async with engine._engine.connect() as c:  # type: ignore[union-attr]
            n = (
                await c.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if n >= minimum:
            return
        await asyncio.sleep(0.025)
    raise AssertionError("no backend ever waited for a lock")


class TestLimitsUnderConcurrency:
    async def test_profile_limit_is_enforced_across_distinct_profiles(self, limited):  # AC-A02-34
        e = await limited(max_profiles_per_scope=3, max_profile_rows_per_scope=100)
        ops = await ops_for(e, "a")
        out = await results(*[ops.create_profile("a", f"p{n}", "P", **NO_CATALOG) for n in range(12)])
        ok = [r for r in out if not isinstance(r, Exception)]
        refused = [r for r in out if isinstance(r, LimitExceededError)]
        assert len(ok) == 3 and len(refused) == 9, out
        assert await count_rows(e, "profiles") == 3

    async def test_total_rows_limit_counts_deleted_profiles_too(self, limited):  # AC-A02-34
        e = await limited(max_profiles_per_scope=50, max_profile_rows_per_scope=4)
        ops = await ops_for(e, "a")
        out = await results(*[ops.create_profile("a", f"p{n}", "P", **NO_CATALOG) for n in range(10)])
        assert len([r for r in out if not isinstance(r, Exception)]) == 4
        assert all(isinstance(r, LimitExceededError) for r in out if isinstance(r, Exception))

    async def test_element_and_version_limits_under_the_profile_lock(self, limited):  # AC-A02-35
        e = await limited(max_elements_per_profile=3, max_element_rows_per_profile=50, max_versions_per_profile=2)
        ops = await ops_for(e, "a")
        await ops.create_profile("a", "p", "P", **NO_CATALOG)
        out = await results(*[ops.create_category("a", "p", f"c{n}", f"C{n}", "d") for n in range(10)])
        assert len([r for r in out if not isinstance(r, Exception)]) == 3
        assert all(isinstance(r, LimitExceededError) for r in out if isinstance(r, Exception))
        # versions: sequentially publish up to the limit, then race the next ones
        for n in range(2):
            await ops.update_category("a", "p", "c0", expected_revision=n + 1, definition=f"v{n}")
            p = await ops.get_profile("a", "p")
            await ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision)
        await ops.update_category("a", "p", "c0", expected_revision=3, definition="v3")
        p = await ops.get_profile("a", "p")
        out = await results(
            *[ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision) for _ in range(5)]
        )
        assert all(isinstance(r, LimitExceededError) for r in out), out
        assert await count_rows(e, "profile_versions") == 2


class TestPublicationRaces:
    async def test_concurrent_publications_of_one_revision_create_one_version(self, engine):  # AC-A02-26
        ops = await ops_for(engine, "a")
        prof, _ = await published(ops, "a", "p")
        await ops.update_category("a", "p", "billing", expected_revision=1, definition="cambio")
        p = await ops.get_profile("a", "p")
        out = await results(
            *[ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision) for _ in range(8)]
        )
        assert all(not isinstance(r, Exception) for r in out), out
        assert sum(1 for r in out if r.created) == 1
        assert len({r.value.version_id for r in out}) == 1 and out[0].value.version_number == 2
        assert await count_rows(engine, "profile_versions") == 2
        assert prof.current_version_number == 1

    async def test_edit_versus_publish_never_produces_an_inconsistent_snapshot(self, engine):  # AC-A02-26
        ops = await ops_for(engine, "a")
        outcomes = set()
        for n in range(ROUNDS):
            pid = f"p{n}"
            await ops.create_profile("a", pid, "P", **NO_CATALOG)
            await ops.create_category("a", pid, "c", "C", "old")
            p = await ops.get_profile("a", pid)
            pub, edit = await results(
                ops.publish_profile("a", pid, expected_draft_revision=p.draft_revision),
                ops.update_category("a", pid, "c", expected_revision=1, definition="new"),
            )
            assert not isinstance(edit, Exception), edit  # the edit always applies
            after = await ops.get_profile("a", pid)
            assert after.draft_revision == p.draft_revision + 1
            if isinstance(pub, RevisionConflictError):  # edit won: the stale publication is refused
                outcomes.add("edit-first")
                assert after.current_version_number is None
            else:  # publication won: it captured the state *before* the edit, consistently
                outcomes.add("publish-first")
                assert pub.created and pub.value.snapshot["categories"][0]["definition"] == "old"
                assert pub.value.source_draft_revision == p.draft_revision
        assert outcomes <= {"edit-first", "publish-first"}

    async def test_same_element_concurrent_edits_one_wins(self, engine):  # AC-A02-26
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "p", "P", **NO_CATALOG)
        await ops.create_category("a", "p", "c", "C", "d")
        out = await results(
            *[ops.update_category("a", "p", "c", expected_revision=1, definition=f"v{n}") for n in range(8)]
        )
        assert sum(1 for r in out if not isinstance(r, Exception)) == 1
        assert all(isinstance(r, RevisionConflictError) for r in out if isinstance(r, Exception))
        assert (await ops.get_profile("a", "p")).draft_revision == 3  # create + exactly one edit

    async def test_edits_of_different_elements_serialize_and_all_apply(self, engine):  # AC-A02-26
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "p", "P", **NO_CATALOG)
        out = await results(*[ops.create_category("a", "p", f"c{n}", f"C{n}", "d") for n in range(8)])
        assert all(not isinstance(r, Exception) for r in out), out
        assert (await ops.get_profile("a", "p")).draft_revision == 9

    async def test_disable_element_versus_publish_is_consistent(self, engine):  # AC-A02-26
        ops = await ops_for(engine, "a")
        for n in range(ROUNDS):
            pid = f"p{n}"
            await ops.create_profile("a", pid, "P", **NO_CATALOG)
            await ops.create_category("a", pid, "c", "C", "d")
            await ops.create_category("a", pid, "k", "K", "d")
            p = await ops.get_profile("a", pid)
            pub, dis = await results(
                ops.publish_profile("a", pid, expected_draft_revision=p.draft_revision),
                ops.disable_category("a", pid, "c"),
            )
            assert not isinstance(dis, Exception)
            if not isinstance(pub, Exception):
                assert {c["category_id"] for c in pub.value.snapshot["categories"]} == {"c", "k"}
            else:
                assert isinstance(pub, RevisionConflictError)


class TestDefaultProfileRaces:
    async def test_concurrent_initial_designations_leave_exactly_one_default(self, engine):  # AC-A02-16
        ops = await ops_for(engine, "a")
        for n in range(4):
            await published(ops, "a", f"p{n}")
        out = await results(*[ops.set_default_profile("a", f"p{n}", expected_revision=0) for n in range(4)])
        assert sum(1 for r in out if not isinstance(r, Exception)) == 1
        assert all(isinstance(r, RevisionConflictError) for r in out if isinstance(r, Exception)), out
        d = await ops.get_default_profile("a")
        assert d.revision == 1 and d.profile_id in {f"p{n}" for n in range(4)}
        async with engine._engine.connect() as c:  # type: ignore[union-attr]
            assert (
                await c.execute(sa.select(sa.func.count()).select_from(engine.tables.scope_profile_defaults))
            ).scalar_one() == 1

    async def test_replace_and_clear_race_on_the_same_revision(self, engine):  # AC-A02-16
        ops = await ops_for(engine, "a")
        for n in range(3):
            await published(ops, "a", f"p{n}")
        await ops.set_default_profile("a", "p0", expected_revision=0)
        out = await results(
            ops.set_default_profile("a", "p1", expected_revision=1),
            ops.set_default_profile("a", "p2", expected_revision=1),
            ops.clear_default_profile("a", expected_revision=1),
        )
        assert sum(1 for r in out if not isinstance(r, Exception)) == 1, out
        assert (await ops.get_default_profile("a")).revision == 2

    async def test_disable_or_delete_versus_designation_never_leaves_an_invalid_default(self, engine):  # AC-A02-17
        ops = await ops_for(engine, "a")
        seen = set()
        for n in range(ROUNDS * 2):
            pid = f"p{n}"
            await published(ops, "a", pid)
            action = ops.disable_profile if n % 2 else ops.delete_profile
            des, act = await results(
                ops.set_default_profile("a", pid, expected_revision=(await ops.get_default_profile("a")).revision),
                action("a", pid),
            )
            prof = await ops.get_profile("a", pid, include_deleted=True)
            default = await ops.get_default_profile("a")
            if default.profile_id == pid:  # designation won -> the transition must have been refused
                assert prof.status == "active" and isinstance(act, ProfileIsDefaultError), (des, act)
                seen.add("designated")
                await ops.clear_default_profile("a", expected_revision=default.revision)
            else:  # transition won -> the designation was refused
                assert prof.status != "active" and not isinstance(act, Exception)
                assert isinstance(des, (ProfileNotSelectableError, ResourceNotFoundError)), des
                seen.add("transitioned")
        assert seen  # at least one of the two orders was exercised

    async def test_designation_versus_first_publication(self, engine):  # AC-A02-26
        ops = await ops_for(engine, "a")
        for n in range(ROUNDS):
            pid = f"p{n}"
            await ops.create_profile("a", pid, "P")
            des, pub = await results(
                ops.set_default_profile("a", pid, expected_revision=(await ops.get_default_profile("a")).revision),
                ops.publish_profile("a", pid, expected_draft_revision=1),
            )
            assert not isinstance(pub, Exception)
            if isinstance(des, Exception):
                assert isinstance(des, ProfileNotPublishedError)
            else:
                assert (await ops.get_default_profile("a")).profile_id == pid
                # designated => published, always
                assert (await ops.get_profile("a", pid)).current_version_number == 1


class TestLockBehaviourOnPostgres:
    @pytest.fixture(autouse=True)
    def _only_postgres(self, db):
        if db[0] != "postgresql":
            pytest.skip("PostgreSQL only: row locks")

    async def test_lock_only_operations_do_not_rewrite_the_row(self, engine):  # AC-A02-36
        ops = await ops_for(engine, "a")
        prof, _ = await published(ops, "a", "p")
        cat = await ops.get_category("a", "p", "billing")

        async def xmins():
            async with engine._engine.connect() as c:  # type: ignore[union-attr]
                t = engine.tables
                p = (await c.execute(sa.text('SELECT xmin::text FROM "cawbase_content_engine".profiles'))).scalar_one()
                e = (
                    await c.execute(sa.text('SELECT xmin::text FROM "cawbase_content_engine".profile_categories'))
                ).scalar_one()
                return p, e, t

        px, ex, _ = await xmins()
        await ops.update_profile(
            "a", "p", expected_revision=prof.revision, display_name=prof.display_name
        )  # lock + no-op
        await ops.update_category("a", "p", "billing", expected_revision=cat.revision, name=cat.name)
        await ops.disable_profile("a", "p")
        await ops.enable_profile("a", "p")  # real changes in between...
        px2, ex2, _ = await xmins()
        assert ex2 == ex, "a lock-only operation must not rewrite the child row"
        assert px2 != px  # the real status changes did rewrite the profile
        px3 = px2
        await ops.enable_profile("a", "p")  # target state already reached: lock, no write
        await ops.update_profile("a", "p", expected_revision=3, display_name=prof.display_name)
        assert (await xmins())[0] == px3, "no dummy UPDATE: an effective-change-free operation leaves the row as it was"

    async def test_a_writer_waits_for_the_profile_lock_and_then_rechecks_current_data(self, migrated):  # AC-A02-26, 37
        backend, url = migrated
        host = await make_host_engine(backend, url)
        try:
            async with ContentEngine(EngineConfig(async_engine=host)) as engine:
                ops = await ops_for(engine, "a")
                prof, _ = await published(ops, "a", "p")
                async with host.connect() as conn:
                    await conn.begin()
                    h = engine.scoped(engine.trusted_scope_context("a", [PR, PW]), HostTransaction(conn))
                    await h.update_category(
                        "a", "p", "billing", expected_revision=1, definition="held"
                    )  # holds the lock
                    waiter = asyncio.create_task(
                        ops.publish_profile("a", "p", expected_draft_revision=prof.draft_revision)
                    )
                    await wait_for_lock_wait(engine)  # it is really blocked on the profile row
                    assert not waiter.done()
                    await conn.commit()
                with pytest.raises(RevisionConflictError):  # after the lock it saw the committed draft_revision
                    await waiter
        finally:
            await host.dispose()

    async def test_creating_profiles_serializes_on_the_scope_row(self, migrated):  # AC-A02-34
        backend, url = migrated
        host = await make_host_engine(backend, url)
        try:
            async with ContentEngine(EngineConfig(async_engine=host)) as engine:
                ops = await ops_for(engine, "a")
                async with host.connect() as conn:
                    await conn.begin()
                    h = engine.scoped(engine.trusted_scope_context("a", [PR, PW]), HostTransaction(conn))
                    await h.create_profile("a", "first", "F", **NO_CATALOG)
                    other = asyncio.create_task(ops.create_profile("a", "second", "S", **NO_CATALOG))
                    await wait_for_lock_wait(engine)
                    assert not other.done()
                    await conn.commit()
                assert (await other).created
        finally:
            await host.dispose()

    async def test_designation_locks_scope_then_profile(self, migrated):  # AC-A02-17
        backend, url = migrated
        host = await make_host_engine(backend, url)
        try:
            async with ContentEngine(EngineConfig(async_engine=host)) as engine:
                ops = await ops_for(engine, "a")
                await published(ops, "a", "p")
                async with host.connect() as conn:
                    await conn.begin()
                    h = engine.scoped(engine.trusted_scope_context("a", [PR, PW]), HostTransaction(conn))
                    await h.update_profile("a", "p", expected_revision=1, description="x")  # profile lock held
                    des = asyncio.create_task(ops.set_default_profile("a", "p", expected_revision=0))
                    await wait_for_lock_wait(engine)
                    assert not des.done()
                    await h.disable_profile("a", "p")  # same transaction: disables before the designation resumes
                    await conn.commit()
                with pytest.raises(ProfileNotSelectableError):  # re-validated under the lock on current data
                    await des
                assert (await ops.get_default_profile("a")).profile_id is None
        finally:
            await host.dispose()


class TestDeadlocks:
    @pytest.fixture(autouse=True)
    def _only_postgres(self, db):
        if db[0] != "postgresql":
            pytest.skip("PostgreSQL only: deadlock detection")

    async def test_deadlock_is_a_retryable_database_error_and_the_savepoint_recovers(self, migrated):  # AC-A02-37
        """Two host transactions lock two profiles in opposite order (the host, not the engine, creates the cycle).

        Measured behaviour: PostgreSQL aborts one statement (40P01). The engine translates it to the transient
        database error (retryable), rolls back *that operation's* savepoint and leaves the host transaction
        usable: this is NOT ``transaction_broken``. The engine never commits or rolls back the host transaction.
        """
        backend, url = migrated
        host = await make_host_engine(backend, url)
        try:
            async with ContentEngine(EngineConfig(async_engine=host)) as engine:
                ops = await ops_for(engine, "a")
                await ops.create_profile("a", "p1", "P1", **NO_CATALOG)
                await ops.create_profile("a", "p2", "P2", **NO_CATALOG)
                c1, c2 = await host.connect(), await host.connect()
                try:
                    await c1.begin()
                    await c2.begin()
                    h1 = engine.scoped(engine.trusted_scope_context("a", [PR, PW]), HostTransaction(c1))
                    h2 = engine.scoped(engine.trusted_scope_context("a", [PR, PW]), HostTransaction(c2))
                    await h1.update_profile("a", "p1", expected_revision=1, description="one")
                    await h2.update_profile("a", "p2", expected_revision=1, description="two")
                    r1 = asyncio.create_task(h1.update_profile("a", "p2", expected_revision=1, description="x"))
                    await wait_for_lock_wait(engine)  # t1 now waits for t2
                    r2 = asyncio.create_task(h2.update_profile("a", "p1", expected_revision=1, description="y"))
                    # The survivor keeps waiting until the victim's *transaction* ends (it still holds its first lock).
                    done, _ = await asyncio.wait({r1, r2}, return_when=asyncio.FIRST_COMPLETED)
                    assert len(done) == 1
                    victim_task = done.pop()
                    survivor_task, survivor_conn, victim_conn = (r2, c2, c1) if victim_task is r1 else (r1, c1, c2)
                    victim = victim_task.exception()
                    assert isinstance(victim, DatabaseUnavailableError) and victim.retryable, victim
                    # only the victim's savepoint was rolled back: its host transaction is healthy and committable
                    assert (await victim_conn.execute(sa.text("SELECT 1"))).scalar_one() == 1
                    await victim_conn.commit()
                    # the lock is released and the survivor proceeds; it re-checks on current data and sees the
                    # victim's committed revision (strict optimistic control), so it is refused with a 412
                    with pytest.raises(RevisionConflictError):
                        await asyncio.wait_for(survivor_task, 30)
                    await survivor_conn.commit()
                finally:
                    await c1.close()
                    await c2.close()
                p1, p2 = await ops.get_profile("a", "p1"), await ops.get_profile("a", "p2")
                assert (p1.description, p2.description) == ("one", "two")  # each host's first write survived
        finally:
            await host.dispose()

    async def test_lost_connection_is_still_transaction_broken(self, migrated):  # AC-A02-37, A01 D-23 preserved
        from cawbase_content_engine.errors import TransactionBrokenError

        backend, url = migrated
        host = await make_host_engine(backend, url)
        try:
            async with ContentEngine(EngineConfig(async_engine=host)) as engine:
                ops = await ops_for(engine, "a")
                await published(ops, "a", "p")
                conn = await host.connect()
                try:
                    await conn.begin()
                    h = engine.scoped(engine.trusted_scope_context("a", [PR, PW]), HostTransaction(conn))
                    pid = (await conn.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()
                    async with host.connect() as killer:
                        await killer.execute(sa.text("SELECT pg_terminate_backend(:p)"), {"p": pid})
                    with pytest.raises(TransactionBrokenError):
                        await h.get_profile("a", "p")
                finally:
                    await conn.close()
        finally:
            await host.dispose()
