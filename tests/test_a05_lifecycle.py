"""A05 possession, fencing, cancellation, scope fence (mixed runtimes), counters, HostTransaction. Real engine, both backends."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

from cawbase_content_engine import HostTransaction
from cawbase_content_engine.jobs.worker import LeaseLost

from .a05_helpers import (
    FULL,
    SP,
    TEXT,
    at,
    chunked,
    make_docx,
    make_engine,
    make_host_engine_a05,
    raw,
    ready_scope,
    run_all,
)
from .helpers import admin_ctx


@pytest.fixture
async def eng(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path)
    yield e
    await e.aclose()


async def submit(ops, key="k", text=TEXT):
    return (await ops.submit_text_analysis("a", text=text, idempotency_key=key)).job.job_id


async def row(eng, job_id):
    return (await raw(eng, "SELECT * FROM {jobs} WHERE job_id = :j", j=job_id))[0]


# ---- possession and fencing -------------------------------------------------------------------------
async def test_stale_worker_cannot_register_or_publish_after_losing_possession(eng):
    ops = await ready_scope(eng)
    jid = await submit(ops)
    w1, w2 = eng.job_worker("w1"), eng.job_worker("w2")
    [c1] = await w1.claim(1)
    art = await w1.write_artifact(c1, "result", b'{"stale":true}', 1000)  # written while it still owned the attempt
    # the worker is suspended longer than its lease: the DATABASE clock says the lease is over
    await raw(
        eng,
        "UPDATE {jobs} SET lease_expires_at = :t WHERE job_id = :j",
        t=at(eng, delta=-__import__("datetime").timedelta(seconds=5)),
        j=jid,
    )
    assert await eng.maintenance().recover_expired_leases() == 1  # recovery, not revival
    assert (await row(eng, jid)).state == "retry_wait"
    with pytest.raises(LeaseLost):
        await w1.write_artifact(c1, "result", b"{}", 1000)
    await asyncio.sleep(0.3)
    [c2] = await w2.claim(1)
    assert c2.attempt_no == 2 and c2.lease_token != c1.lease_token
    # the obsolete worker finishes its computation and tries to publish its own (valid-looking) artifact
    assert await w1.publish(c1, {"result": art}) == "lease_lost"
    await w2.run_claimed(c2)
    r = await row(eng, jid)
    assert r.state == "succeeded" and r.published_attempt_no == 2
    states = {a.attempt_no: a.state for a in await raw(eng, "SELECT attempt_no, state FROM {job_artifacts}")}
    assert states[1] == "discarded" and states[2] == "published"
    assert [a.outcome for a in await raw(eng, "SELECT outcome FROM {job_attempts} ORDER BY attempt_no")] == [
        "lease_lost",
        "succeeded",
    ]
    # counters coherent after all this
    u = (await raw(eng, "SELECT active_jobs, running_jobs, reserved_bytes, open_reservations FROM {scope_usage}"))[0]
    assert (u.active_jobs, u.running_jobs, u.reserved_bytes, u.open_reservations) == (0, 0, 0, 0)


async def test_heartbeat_never_extends_beyond_the_attempt_deadline_nor_revives(eng):
    ops = await ready_scope(eng)
    jid = await submit(ops)
    w = eng.job_worker("w")
    [c] = await w.claim(1)
    r = await row(eng, jid)
    assert r.lease_expires_at <= r.attempt_deadline_at <= r.deadline_at
    await raw(
        eng,
        "UPDATE {jobs} SET lease_expires_at = :t WHERE job_id = :j",
        t=at(eng, delta=-__import__("datetime").timedelta(seconds=1)),
        j=jid,
    )
    assert (
        await eng._run(lambda conn: w._beat(conn, c), write=True, tx=None) == "lost"
    )  # an expired lease is never renewed


async def test_cancel_wins_over_publication_and_success_is_not_rewritten(eng):
    ops = await ready_scope(eng)
    w = eng.job_worker("w")
    c1, c2 = await w.claim(2)
    a1 = await w.write_artifact(c1, "result", b"{}", 1000)
    a2 = await w.write_artifact(c2, "result", b"{}", 1000)
    # job 1: the cancellation is confirmed first -> nothing of the attempt is published
    assert (await ops.cancel_job("a", c1.job_id)).cancel_outcome == "requested"
    assert await w.publish(c1, {"result": a1}) == "cancelled"
    r1 = await row(eng, c1.job_id)
    assert r1.state == "cancelled" and r1.result_artifact_id is None and r1.data_state == "none"
    # job 2: the success is confirmed first -> cancelling afterwards does not rewrite it
    assert await w.publish(c2, {"result": a2}) == "succeeded"
    done = await ops.cancel_job("a", c2.job_id)
    assert done.state == "succeeded" and done.cancel_outcome == "already_terminal"


async def test_cancelling_a_running_job_with_an_expired_lease_is_defined(eng):
    ops = await ready_scope(eng)
    jid = await submit(ops)
    [c] = await eng.job_worker("w").claim(1)
    await raw(
        eng,
        "UPDATE {jobs} SET lease_expires_at = :t WHERE job_id = :j",
        t=at(eng, delta=-__import__("datetime").timedelta(seconds=1)),
        j=jid,
    )
    out = await ops.cancel_job("a", jid)  # T5b: recovered as cancelled, never left dangling
    assert out.state == "cancelled" and out.cancel_outcome == "cancelled"
    u = (await raw(eng, "SELECT active_jobs, running_jobs FROM {scope_usage}"))[0]
    assert (u.active_jobs, u.running_jobs) == (0, 0)
    _ = c


async def test_one_claimant_wins_a_job(eng):
    ops = await ready_scope(eng)
    await submit(ops)
    got = await asyncio.gather(*[eng.job_worker(f"w{i}").claim(1) for i in range(6)])
    assert sum(len(g) for g in got) == 1
    assert (await raw(eng, "SELECT running_jobs FROM {scope_usage}"))[0].running_jobs == 1


async def test_counters_stay_exact_under_concurrent_activity(eng):
    ops = await ready_scope(eng)
    await asyncio.gather(*[submit(ops, f"k{i}", TEXT + str(i)) for i in range(8)])
    ws = [eng.job_worker(f"w{i}") for i in range(4)]
    await asyncio.gather(*[run_all(eng, f"r{i}") for i in range(4)])
    before = (await raw(eng, "SELECT * FROM {scope_usage}"))[0]
    from cawbase_content_engine.jobs import db as jdb
    from cawbase_content_engine.jobs import files as fl

    async def recount(conn):
        now = await jdb.db_now(conn, eng.backend)
        await jdb.lock_scope(conn, eng.tables, "a", exclusive=False)
        await jdb.recompute_usage(conn, eng.tables, "a", now)

    await eng._run(recount, write=True, tx=None)
    after = (await raw(eng, "SELECT * FROM {scope_usage}"))[0]
    for col in (
        "reserved_bytes",
        "stored_bytes",
        "uncertain_bytes",
        "open_reservations",
        "pending_seals",
        "active_jobs",
        "running_jobs",
        "live_files",
    ):
        assert getattr(before, col) == getattr(after, col), col
    eu = (await raw(eng, "SELECT stored_bytes, active_jobs FROM {engine_usage}"))[0]
    assert eu.active_jobs == 0 and eu.stored_bytes == after.stored_bytes
    _, _ = ws, fl


# ---- scope fence -----------------------------------------------------------------------------------------
async def old_runtime_flip(eng, *, times=2):
    """Exactly what runtimes 0.1.0-0.3.0 did: a plain UPDATE that bumps ``scopes.revision`` and ignores any fence."""
    for status in ("disabled", "active")[:times]:
        await raw(eng, "UPDATE {scopes} SET status = :s, revision = revision + 1 WHERE scope_id = 'a'", s=status)


async def test_disable_cancels_non_terminal_jobs_and_rehabilitating_does_not_resurrect(eng):
    ops = await ready_scope(eng)
    admin = eng.admin(await admin_ctx(eng))
    queued, running = await submit(ops, "q"), await submit(ops, "r", TEXT + "r")
    [c] = await eng.job_worker("w").claim(1)
    done_id = None
    await admin.set_scope_status("a", "disabled")
    await admin.set_scope_status("a", "active")
    assert {(await row(eng, j)).state for j in (queued, running)} == {"cancelled"}
    assert {(await row(eng, j)).cancel_reason for j in (queued, running)} == {"scope_disabled"}
    assert await run_all(eng) == 0  # nothing resurrected
    assert (await raw(eng, "SELECT active_jobs, running_jobs FROM {scope_usage}"))[0].running_jobs == 0
    _ = (c, done_id)


async def test_published_result_survives_a_later_disable(eng):
    ops = await ready_scope(eng)
    jid = await submit(ops)
    await run_all(eng)
    await eng.admin(await admin_ctx(eng)).set_scope_status("a", "disabled")
    assert (await row(eng, jid)).state == "succeeded"
    assert (await ops.get_job("a", jid)).data_state == "available"  # reads on a disabled scope stay allowed


async def test_old_runtime_disable_enable_between_accept_and_publish_never_publishes(eng):
    ops = await ready_scope(eng)
    jid = await submit(ops)
    w = eng.job_worker("w")
    [c] = await w.claim(1)
    art = await w.write_artifact(c, "result", b"{}", 1000)
    await old_runtime_flip(eng)  # status is `active` again: only the revision betrays it
    assert await w.publish(c, {"result": art}) in ("lease_lost", "cancelled")
    r = await row(eng, jid)
    assert r.state == "cancelled" and r.cancel_reason == "scope_disabled" and r.result_artifact_id is None


async def test_unobserved_change_is_reconciled_before_the_revision_is_advanced(eng):
    """The counterexample of the review: R -> old runtime flips twice (R+2) -> A05 edits metadata (R+3)."""
    ops = await ready_scope(eng)
    jid = await submit(ops)
    w = eng.job_worker("w")
    [c] = await w.claim(1)
    art = await w.write_artifact(c, "result", b"{}", 1000)
    await old_runtime_flip(eng)
    sops = eng.scoped(eng.trusted_scope_context("a", [SP.SCOPE_READ, SP.SCOPE_WRITE]))
    sc = await sops.get_scope("a")
    await sops.update_scope("a", expected_revision=sc.revision, display_name="Edited")  # public D-30 semantics intact
    assert (await sops.get_scope("a")).revision == sc.revision + 1
    assert (await row(eng, jid)).state == "cancelled"  # invalidated BEFORE the fence observed the new revision
    assert await w.publish(c, {"result": art}) == "lease_lost"
    fence = (await raw(eng, "SELECT * FROM {scope_job_fences}"))[0]
    assert fence.observed_scope_revision == sc.revision + 1


async def test_reconciliation_is_idempotent_and_serialized(eng):
    ops = await ready_scope(eng)
    await submit(ops)
    admin = eng.admin(await admin_ctx(eng))
    g0 = (await raw(eng, "SELECT disable_generation FROM {scope_job_fences}"))[0].disable_generation
    await old_runtime_flip(eng)
    await asyncio.gather(
        *[admin.set_scope_status("a", "active") for _ in range(5)]
    )  # no effective change, each reconciles
    g1 = (await raw(eng, "SELECT disable_generation FROM {scope_job_fences}"))[0].disable_generation
    assert g1 == g0 + 1  # one invalidation for one divergence, however many reconcilers raced
    await admin.set_scope_status("a", "active")
    assert (await raw(eng, "SELECT disable_generation FROM {scope_job_fences}"))[0].disable_generation == g1


async def test_lazy_fence_never_adopts_jobs_of_unknown_generation(eng):
    ops = await ready_scope(eng)
    jid = await submit(ops)
    await raw(eng, "DELETE FROM {scope_job_fences}")  # e.g. a restored copy: the generation of the job is unknown
    await eng.admin(await admin_ctx(eng)).set_scope_status("a", "active")
    assert (await row(eng, jid)).state == "cancelled"
    # a scope WITHOUT jobs gets no fence from a plain update; the first submit creates it
    await ready_scope(eng, "b")
    await eng.admin(await admin_ctx(eng)).set_scope_status("b", "active")
    assert (await raw(eng, "SELECT 1 FROM {scope_job_fences} WHERE scope_id = 'b'")) == []
    b = eng.scoped(eng.trusted_scope_context("b", FULL))
    await b.submit_text_analysis("b", text=TEXT, idempotency_key="first")
    assert len(await raw(eng, "SELECT 1 FROM {scope_job_fences} WHERE scope_id = 'b'")) == 1


# ---- HostTransaction (I7) --------------------------------------------------------------------------------
@pytest.fixture
async def host(migrated, tmp_path):
    backend, url = migrated
    eng, host_engine = await make_host_engine_a05(backend, url, tmp_path)
    yield backend, eng, host_engine
    await eng.aclose()
    await host_engine.dispose()


async def host_setup(eng):
    ops = await ready_scope(eng)
    f = (await ops.upload_file("a", chunked(make_docx([TEXT])), idempotency_key="up")).value
    return ops, f


async def test_first_submit_inside_host_transaction_creates_the_fence_on_the_same_connection(host):
    backend, eng, host_engine = host
    ops, f = await host_setup(eng)
    assert (await raw(eng, "SELECT 1 FROM {scope_job_fences}")) == []
    async with host_engine.begin() as conn:
        acc = await ops.__class__(eng, ops._ctx, HostTransaction(conn)).submit_document_analysis(
            "a", file_id=f.file_id, idempotency_key="h1"
        )
        assert acc.job.state == "queued" and not acc.replayed
        # still inside the host's transaction: the engine used ONLY this connection (otherwise a SQLite writer would block)
        again = await ops.__class__(eng, ops._ctx, HostTransaction(conn)).submit_document_analysis(
            "a", file_id=f.file_id, idempotency_key="h1"
        )
        assert again.replayed and again.job.job_id == acc.job.job_id
    assert (
        len(await raw(eng, "SELECT 1 FROM {jobs}")) == 1
        and len(await raw(eng, "SELECT 1 FROM {scope_job_fences}")) == 1
    )
    assert (await raw(eng, "SELECT active_jobs FROM {scope_usage}"))[0].active_jobs == 1


async def test_host_rollback_reverts_reconciliation_and_acceptance_together(host):
    backend, eng, host_engine = host
    ops, f = await host_setup(eng)
    jid = (
        await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="own")
    ).job.job_id  # creates the fence
    await old_runtime_flip(eng)  # a diverged fence
    g0 = (await raw(eng, "SELECT disable_generation, observed_scope_revision FROM {scope_job_fences}"))[0]

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with host_engine.begin() as conn:
            h = ops.__class__(eng, ops._ctx, HostTransaction(conn))
            await h.submit_document_analysis("a", file_id=f.file_id, idempotency_key="in-host", profile_id="contratos")
            raise Boom
    # nothing of the host transaction survives: no new job, the divergence is still there for the next reconciler
    assert len(await raw(eng, "SELECT 1 FROM {jobs}")) == 1
    assert (await raw(eng, "SELECT disable_generation, observed_scope_revision FROM {scope_job_fences}"))[0] == g0
    assert (await raw(eng, "SELECT 1 FROM {idempotency_records} WHERE operation = 'document_analysis'")).__len__() == 1
    # I1 still protects the pre-existing job: it cannot be claimed until somebody reconciles (which cancels it)
    assert await run_all(eng) == 0
    assert (await row(eng, jid)).state == "cancelled"


async def test_divergence_inside_host_transaction_is_reconciled_in_the_same_savepoint(host):
    backend, eng, host_engine = host
    ops, f = await host_setup(eng)
    first = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="one")).job.job_id
    await old_runtime_flip(eng)
    async with host_engine.begin() as conn:
        h = ops.__class__(eng, ops._ctx, HostTransaction(conn))
        acc = await h.submit_document_analysis("a", file_id=f.file_id, idempotency_key="two")
        assert acc.job.state == "queued"
    assert (await row(eng, first)).state == "cancelled"  # invalidated by the in-host reconciliation
    assert (await row(eng, acc.job.job_id)).state == "queued"  # the new job carries the new generation
    assert await run_all(eng) == 1


async def test_host_holding_locks_before_the_savepoint_does_not_deadlock_the_engine(host):
    backend, eng, host_engine = host
    ops, f = await host_setup(eng)
    async with host_engine.begin() as conn:
        # the host already touched the scope row (PostgreSQL row lock / SQLite writer) BEFORE calling the engine
        await conn.execute(
            sa.text(
                f"UPDATE {'cawbase_content_engine.' if backend == 'postgresql' else 'cawbase_content_engine_'}scopes SET description = 'host' WHERE scope_id = 'a'"
            )
        )
        h = ops.__class__(eng, ops._ctx, HostTransaction(conn))
        acc = await asyncio.wait_for(h.submit_document_analysis("a", file_id=f.file_id, idempotency_key="held"), 10)
        assert acc.job.state == "queued"
    assert len(await raw(eng, "SELECT 1 FROM {jobs}")) == 1


async def test_storage_operations_are_rejected_before_any_effect_in_a_host_transaction(host):
    from cawbase_content_engine import IncompatibleTransactionError

    backend, eng, host_engine = host
    ops, f = await host_setup(eng)
    async with host_engine.begin() as conn:
        h = ops.__class__(eng, ops._ctx, HostTransaction(conn))
        for call in (
            h.upload_file("a", chunked(b"x"), idempotency_key="z"),
            h.submit_text_analysis("a", text=TEXT, idempotency_key="z"),
            h.submit_document_analysis("a", file_id=f.file_id, idempotency_key="z", context="ctx"),
            h.open_file_content("a", f.file_id),
            h.get_job_result("a", "job_" + "0" * 32),
        ):
            with pytest.raises(IncompatibleTransactionError):
                await call
        assert (await h.list_files("a")).items[0].file_id == f.file_id  # short DB-only reads are fine
    assert len(await raw(eng, "SELECT 1 FROM {jobs}")) == 0
