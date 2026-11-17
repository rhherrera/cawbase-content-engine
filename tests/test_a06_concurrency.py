"""A06 concurrency with CONTROLLED synchronization (no sleeps as proof): a gate that holds both analyses in flight,
an independent connection that retains a row lock while PostgreSQL's ``pg_stat_activity`` shows the wait, and the
statement log for the lock order. AC-A06-46..49."""

from __future__ import annotations

import asyncio
import datetime as dt
import re

import pytest
import sqlalchemy as sa

from cawbase_content_engine.errors import ContextUnavailableError

from .a06_helpers import (
    INLINE,
    NAME,
    PSEUDO,
    Harness,
    capture_sql,
    expire_context,
    f,
    make_harness,
    wait_for_lock_wait,
)

ROUNDS = 5


async def overlapped(h: Harness, *coros, inferences: int | None = None):
    """Starts every coroutine, lets all of them reach the adapter (gate closed), then opens the gate."""
    h.adapter.gate = asyncio.Event()
    tasks = [asyncio.create_task(c) for c in coros]
    for _ in range(400):
        if len(h.adapter.calls) >= (len(tasks) if inferences is None else inferences):
            break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError("not every call reached the adapter")
    h.adapter.gate.set()
    out = await asyncio.gather(*tasks, return_exceptions=True)
    h.adapter.gate = None
    return out


async def test_concurrent_protections_of_the_same_value_share_one_marker_and_one_row(a06: Harness):  # AC-A06-46
    for _ in range(ROUNDS):
        cid = await a06.context()
        t1, t2 = f"Hola {NAME}", f"Adiós {NAME}, otra vez"
        a06.adapter.on(t1, f(t1, NAME))
        a06.adapter.on(t2, f(t2, NAME))
        a06.adapter.calls.clear()
        res = await overlapped(
            a06,
            *[
                a06.ops().protect_text("a", context_id=cid, kind="conversation", text=t, policy=PSEUDO, analysis=INLINE)
                for t in (t1, t2, t1, t2)
            ],
        )
        assert all(not isinstance(r, Exception) for r in res), res
        assert len({r.markers[0] for r in res}) == 1  # every text carries the same marker
        mappings = [r for r in await a06.rows("protection_mappings") if r.context_id == cid]
        assert len(mappings) == 1
        assert sum(r.counts["new_markers"] for r in res) == 1 and sum(r.counts["reused_markers"] for r in res) == 3


async def test_protection_serializes_on_the_context_row_while_it_is_locked(pg_a06: Harness):  # AC-A06-46 (PostgreSQL)
    h = pg_a06
    cid = await h.context()
    t = h.engine.tables.protection_contexts
    text = f"Hola {NAME}"
    h.adapter.on(text, f(text, NAME))
    async with h.engine._engine.connect() as holder:  # type: ignore[union-attr]
        await holder.begin()
        await holder.execute(sa.select(t).where(t.c.context_id == cid).with_for_update())  # the "other" operation
        tasks = [
            asyncio.create_task(
                h.ops().protect_text(
                    "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
                )
            )
            for _ in range(2)
        ]
        await wait_for_lock_wait(h.engine, minimum=2)  # both are really waiting for the row lock
        assert not any(task.done() for task in tasks)
        await holder.rollback()
    res = await asyncio.gather(*tasks)
    assert res[0].markers == res[1].markers and len(await h.rows("protection_mappings")) == 1


async def test_restore_versus_delete_is_whole_or_closed_never_partial(a06: Harness):  # AC-A06-47
    for _ in range(ROUNDS):
        text = f"Hola {NAME}"
        cid, res = await a06.protect(text, f(text, NAME))
        a06.adapter.on(res.text)
        a06.adapter.calls.clear()
        out = await overlapped(
            a06,
            a06.ops().restore_text(
                "a", context_id=cid, kind="conversation", text=res.text, analysis=INLINE, delivered_markers=res.markers
            ),
            a06.ops().delete_context("a", cid),
            inferences=1,
        )
        restored, deleted = out
        assert not isinstance(deleted, Exception) and deleted.state == "deleted"
        assert (not isinstance(restored, Exception) and restored.text == text) or isinstance(
            restored, ContextUnavailableError
        ), restored
        assert await a06.rows("protection_mappings") == []


async def test_delete_that_commits_before_the_restore_lock_makes_it_fail(pg_a06: Harness):  # AC-A06-47 (PostgreSQL)
    h = pg_a06
    text = f"Hola {NAME}"
    cid, res = await h.protect(text, f(text, NAME))
    h.adapter.on(res.text)
    t, m = h.engine.tables.protection_contexts, h.engine.tables.protection_mappings
    async with h.engine._engine.connect() as holder:  # type: ignore[union-attr]
        await holder.begin()
        await holder.execute(sa.select(t).where(t.c.context_id == cid).with_for_update())  # a delete in progress
        task = asyncio.create_task(
            h.ops().restore_text(
                "a", context_id=cid, kind="conversation", text=res.text, analysis=INLINE, delivered_markers=res.markers
            )
        )
        await wait_for_lock_wait(h.engine)
        await holder.execute(sa.delete(m).where(m.c.context_id == cid))
        await holder.execute(sa.update(t).where(t.c.context_id == cid).values(state="deleted"))
        await holder.commit()
    with pytest.raises(ContextUnavailableError):
        await task


async def test_a_restore_already_inside_its_lock_finishes_and_delete_waits(pg_a06: Harness):  # AC-A06-47 (PostgreSQL)
    h = pg_a06
    text = f"Hola {NAME}"
    cid, res = await h.protect(text, f(text, NAME))
    t = h.engine.tables.protection_contexts
    async with h.engine._engine.connect() as holder:  # type: ignore[union-attr]
        await holder.begin()
        await holder.execute(
            sa.select(t).where(t.c.context_id == cid).with_for_update(read=True)
        )  # a restore, deciding
        deleting = asyncio.create_task(h.ops().delete_context("a", cid))
        await wait_for_lock_wait(h.engine)
        assert not deleting.done()  # the deletion (FOR UPDATE) waits for the restoration that holds FOR SHARE
        await holder.commit()
    assert (await deleting).state == "deleted"


async def test_expiry_during_the_wait_is_decided_with_the_clock_read_after_it(
    pg_a06: Harness,
):  # AC-A06-47 (PostgreSQL)
    h = pg_a06
    text = f"Hola {NAME}"
    cid, res = await h.protect(text, f(text, NAME))
    h.adapter.on(res.text)
    t = h.engine.tables.protection_contexts
    async with h.engine._engine.connect() as holder:  # type: ignore[union-attr]
        await holder.begin()
        soon = (await holder.execute(sa.text("SELECT clock_timestamp() + interval '1 second'"))).scalar_one()
        await holder.execute(sa.select(t).where(t.c.context_id == cid).with_for_update())
        await holder.execute(sa.update(t).where(t.c.context_id == cid).values(expires_at=soon))
        await holder.commit()  # now: valid for one more second
        await holder.begin()
        await holder.execute(sa.select(t).where(t.c.context_id == cid).with_for_update())
        task = asyncio.create_task(
            h.ops().restore_text(
                "a", context_id=cid, kind="conversation", text=res.text, analysis=INLINE, delivered_markers=res.markers
            )
        )
        await wait_for_lock_wait(h.engine)
        for _ in range(400):  # poll the DATABASE clock (no fixed sleep) until the context's time has come
            if (await holder.execute(sa.text("SELECT clock_timestamp() > :s"), {"s": soon})).scalar_one():
                break
            await asyncio.sleep(0.02)
        await holder.rollback()  # the restore resumes: it was valid when it began waiting, it is not valid now
    with pytest.raises(ContextUnavailableError):
        await task


async def test_protect_versus_delete_leaves_no_orphan_rows(a06: Harness):  # AC-A06-48
    for _ in range(ROUNDS):
        cid = await a06.context()
        text = f"Hola {NAME}"
        a06.adapter.on(text, f(text, NAME))
        a06.adapter.calls.clear()
        out = await overlapped(
            a06,
            a06.ops().protect_text("a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE),
            a06.ops().delete_context("a", cid),
            inferences=1,
        )
        protected, deleted = out
        assert not isinstance(deleted, Exception) and deleted.state == "deleted"
        assert not isinstance(protected, Exception) or isinstance(protected, ContextUnavailableError), protected
        assert [r for r in await a06.rows("protection_mappings") if r.context_id == cid] == []  # never orphaned


async def test_protect_versus_expiry_is_all_or_unavailable(pg_a06: Harness):  # AC-A06-48 (PostgreSQL)
    h = pg_a06
    cid = await h.context()
    text = f"Hola {NAME}"
    h.adapter.on(text, f(text, NAME))
    t = h.engine.tables.protection_contexts
    async with h.engine._engine.connect() as holder:  # type: ignore[union-attr]
        await holder.begin()
        await holder.execute(sa.select(t).where(t.c.context_id == cid).with_for_update())
        task = asyncio.create_task(
            h.ops().protect_text("a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE)
        )
        await wait_for_lock_wait(h.engine)
        await holder.execute(
            sa.update(t).where(t.c.context_id == cid).values(expires_at=dt.datetime(2000, 1, 1, tzinfo=dt.UTC))
        )
        await holder.commit()
    with pytest.raises(ContextUnavailableError):
        await task
    assert await h.rows("protection_mappings") == []


# ---- lock order (AC-A06-49) ------------------------------------------------------------------------------------
RANK = {"scopes": 1, "jobs": 8, "protection_scope_usage": 11, "protection_contexts": 12, "protection_mappings": 13}
LOCKING = re.compile(r"\bfor (update|share|no key update)\b")


def lock_sequence(stmts: list[str]) -> list[tuple[int, str]]:
    seq: list[tuple[int, str]] = []
    for s in stmts:
        m = re.search(r"(?:insert into|\bfrom|\bupdate|delete from)\s+(?:\"?cawbase_content_engine\"?\.)?(\w+)", s)
        if not m:
            continue
        table = m.group(1)
        is_lock = bool(LOCKING.search(s)) or s.startswith(("insert into", "delete from"))
        if table in RANK and is_lock:
            seq.append((RANK[table], table))
    return seq


async def test_every_operation_acquires_locks_in_ascending_rank(pg_a06: Harness):  # AC-A06-49
    h = pg_a06
    text = f"Hola {NAME} y Zoe"
    h.adapter.on(text, f(text, NAME), f(text, "Zoe"))
    ops = h.ops()
    cid = (await ops.create_context("a", kind="conversation")).context_id
    sequences: dict[str, list[tuple[int, str]]] = {}
    with capture_sql(h.engine) as stmts:
        await ops.create_context("a", kind="job")
        sequences["create"] = lock_sequence(stmts)
    with capture_sql(h.engine) as stmts:
        res = await ops.protect_text(
            "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
        )
        sequences["protect"] = lock_sequence(stmts)
    h.adapter.on(res.text)
    with capture_sql(h.engine) as stmts:
        await ops.restore_text(
            "a", context_id=cid, kind="conversation", text=res.text, analysis=INLINE, delivered_markers=res.markers
        )
        sequences["restore"] = lock_sequence(stmts)
    with capture_sql(h.engine) as stmts:
        await ops.delete_context("a", cid)
        sequences["delete"] = lock_sequence(stmts)
    expired, _ = await h.protect("Hola Ana", f("Hola Ana", "Ana"))
    await expire_context(h, expired)
    with capture_sql(h.engine) as stmts:
        await h.engine.maintenance().run_protection_cleanup()
        sequences["cleanup"] = lock_sequence(stmts)
    for name, seq in sequences.items():
        ranks = [r for r, _ in seq]
        assert ranks and ranks == sorted(ranks), (name, seq)
    assert [t for _, t in sequences["protect"]][:3] == ["scopes", "protection_contexts", "protection_mappings"]
    assert {r for r, _ in sequences["delete"]} >= {1, 11, 12, 13}


@pytest.fixture
async def pg_a06(pg_url):
    from cawbase_content_engine import EngineConfig
    from cawbase_content_engine.migrations import upgrade

    await upgrade(EngineConfig(database_url=pg_url))
    h = await make_harness("postgresql", pg_url)
    yield h
    await h.engine.aclose()
