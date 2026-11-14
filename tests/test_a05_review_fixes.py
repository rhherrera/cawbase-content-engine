"""A05 review blockers: decision time after lock waits, exact artifact sets, per-instance isolation, lock order.

The time tests coordinate two INDEPENDENT connections: the operation enters with valid possession, waits for a lock held
by another connection, the lease/deadline/expiry passes on the DATABASE clock during the wait, and the mutation must be
refused once the lock is released. SQLite has no row locks: its equivalent is the single writer (the second connection
holds the write lock; the operation's ``BEGIN IMMEDIATE`` waits for it).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from cawbase_content_engine import ContentEngine, EngineConfig
from cawbase_content_engine.documents.runner import UnconfinedDevelopmentRunner
from cawbase_content_engine.errors import FileNotUsableError
from cawbase_content_engine.inference import InferenceSetup
from cawbase_content_engine.jobs.setup import DocumentSetup, JobsSetup, StorageSetup
from cawbase_content_engine.jobs.storage import FilesystemStorage
from cawbase_content_engine.jobs.worker import LeaseLost

from .a05_helpers import (
    FULL,
    TEXT,
    StubAdapter,
    chunked,
    fast_limits,
    make_docx,
    make_engine,
    raw,
    ready_scope,
    run_all,
)
from .test_a05_files import valid

LEASE_MS = 1200


@pytest.fixture
async def eng(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path)
    e._test_url = url  # type: ignore[attr-defined]
    yield e
    await e.aclose()


async def submit(ops, key="k", text=TEXT):
    return (await ops.submit_text_analysis("a", text=text, idempotency_key=key)).job.job_id


async def row(eng, job_id):
    return (await raw(eng, "SELECT * FROM {jobs} WHERE job_id = :j", j=job_id))[0]


def qn(eng: ContentEngine, name: str) -> str:
    t = eng.tables
    return (f'"{t.ns.schema}".' if t.ns.schema else "") + t.ns.table(name)


class Gate:
    """A second, independent connection that holds a lock while a deadline passes on the database clock."""

    def __init__(self, eng: ContentEngine) -> None:
        self.eng, self.url = eng, eng._test_url  # type: ignore[attr-defined]
        self.pg = eng.backend.value == "postgresql"
        self._conn: Any = None
        self._sqlite: sqlite3.Connection | None = None
        self._engine: Any = None

    async def hold(self, table: str, where: str, setter: str, **params: Any) -> None:
        """Locks the row(s) and moves ``setter`` (a SET clause) so that something expires LEASE_MS from now."""
        if self.pg:
            self._engine = create_async_engine(self.url)
            self._conn = await self._engine.connect()
            await self._conn.begin()
            await self._conn.execute(sa.text(f"SELECT 1 FROM {qn(self.eng, table)} WHERE {where} FOR UPDATE"), params)
            await self._conn.execute(
                sa.text(
                    f"UPDATE {qn(self.eng, table)} SET {setter.replace(':soon', 'clock_timestamp() + interval ' + repr(f'{LEASE_MS} milliseconds'))}"
                    f" WHERE {where}"
                ),
                params,
            )
        else:
            path = self.url.split("///", 1)[1]
            self._sqlite = sqlite3.connect(path, timeout=30, isolation_level=None)
            self._sqlite.execute("BEGIN IMMEDIATE")  # the single writer
            soon = (datetime.now(UTC) + timedelta(milliseconds=LEASE_MS)).replace(tzinfo=None).isoformat(sep=" ")
            sql = f"UPDATE {self.eng.tables.ns.table(table)} SET {setter.replace(':soon', repr(soon))} WHERE {where}"
            self._sqlite.execute(sql, {k: v for k, v in params.items()})

    async def waiting(self) -> None:
        """Waits until the operation under test is really blocked, then until the DB clock passed the deadline."""
        if self.pg:
            for _ in range(200):
                n = (
                    await self._conn.execute(
                        sa.text(
                            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
                        )
                    )
                ).scalar_one()
                if n >= 1:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("the operation never blocked on the lock")
        await asyncio.sleep(LEASE_MS / 1000 + 0.6)  # the (database) clock passes the deadline while it waits

    async def release(self) -> None:
        if self.pg:
            await self._conn.commit()
            await self._conn.close()
            await self._engine.dispose()
        else:
            assert self._sqlite is not None
            self._sqlite.execute("COMMIT")
            self._sqlite.close()


async def blocked_call(eng, gate: Gate, coro_factory):
    """Runs the operation, lets it block on the gate, lets the deadline pass, releases, returns (result, exception)."""
    task = asyncio.ensure_future(coro_factory())
    await asyncio.sleep(0.3)  # the SQLite writer lock is requested here
    await gate.waiting()
    assert not task.done(), "the operation finished while the lock was held: it did not wait"
    await gate.release()
    try:
        return await asyncio.wait_for(task, 30), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


JOB_LOCK = "job_id = :j"
LEASE_SET = "lease_expires_at = :soon"


async def claimed(eng, key="k"):
    ops = await ready_scope(eng)
    jid = await submit(ops, key)
    [c] = await eng.job_worker("w").claim(1)
    return ops, jid, c


# ---- 1. decision time is taken AFTER the lock wait ----------------------------------------------------
async def test_publish_is_refused_when_the_lease_expired_while_waiting_for_the_job_lock(eng):
    ops, jid, c = await claimed(eng)
    w = eng.job_worker("w")
    art = await w.write_artifact(c, "result", b"{}", 1000)
    g = Gate(eng)
    await g.hold("jobs", JOB_LOCK, LEASE_SET, j=jid)
    out, exc = await blocked_call(eng, g, lambda: w.publish(c, {"result": art}))
    assert exc is None and out == "lease_lost"
    r = await row(eng, jid)
    assert r.state == "running" and r.result_artifact_id is None and r.data_state == "none"
    assert (await raw(eng, "SELECT state FROM {job_artifacts} WHERE artifact_id = :a", a=art))[0].state == "written"


async def test_heartbeat_cannot_extend_a_lease_that_expired_during_the_wait(eng):
    _, jid, c = await claimed(eng)
    w = eng.job_worker("w")
    before = await row(eng, jid)
    g = Gate(eng)
    await g.hold("jobs", JOB_LOCK, LEASE_SET, j=jid)
    out, exc = await blocked_call(eng, g, lambda: eng._run(lambda conn: w._beat(conn, c), write=True, tx=None))
    assert exc is None and out == "lost"
    after = await row(eng, jid)
    assert after.state == "running" and after.heartbeat_at == before.heartbeat_at  # nothing was written
    # not extended: the lease is still the value the other connection wrote (already in the past on the DB clock)
    lease = (
        after.lease_expires_at
        if isinstance(after.lease_expires_at, datetime)
        else datetime.fromisoformat(str(after.lease_expires_at))
    )
    if lease.tzinfo is None:
        lease = lease.replace(tzinfo=UTC)
    assert lease < datetime.now(UTC)


async def test_phase_change_is_refused_when_the_lease_expired_during_the_wait(eng):
    _, jid, c = await claimed(eng)
    w = eng.job_worker("w")
    g = Gate(eng)
    await g.hold("jobs", JOB_LOCK, LEASE_SET, j=jid)
    out, exc = await blocked_call(eng, g, lambda: w.set_phase(c, "publication"))
    assert out is None and isinstance(exc, LeaseLost)
    assert (await row(eng, jid)).phase != "publication"


async def test_failure_report_is_refused_when_the_lease_expired_during_the_wait(eng):
    from cawbase_content_engine.jobs.worker import JobFailure

    _, jid, c = await claimed(eng)
    w = eng.job_worker("w")
    g = Gate(eng)
    await g.hold("jobs", JOB_LOCK, LEASE_SET, j=jid)
    out, exc = await blocked_call(eng, g, lambda: w._finish_failure(c, JobFailure("internal_error"), cancelled=False))
    assert exc is None and out == "lease_lost"
    assert (await row(eng, jid)).state == "running"  # recovery decides, not the stale owner


async def test_artifact_reservation_is_refused_when_the_lease_expired_during_the_wait(eng):
    _, jid, c = await claimed(eng)
    w = eng.job_worker("w")
    g = Gate(eng)
    await g.hold("jobs", JOB_LOCK, LEASE_SET, j=jid)
    out, exc = await blocked_call(eng, g, lambda: w.write_artifact(c, "result", b"{}", 1000))
    assert out is None and isinstance(exc, LeaseLost)
    assert (await raw(eng, "SELECT count(*) AS n FROM {job_artifacts}"))[0].n == 0


async def test_artifact_registration_is_refused_when_the_lease_expired_during_the_wait(eng, migrated, tmp_path):
    """The wait is injected between the storage write and the registration (step 4) through the storage backend."""
    ops, jid, c = await claimed(eng)
    w = eng.job_worker("w")
    g = Gate(eng)
    real = eng.storage.managed.put_new

    state: dict[str, Any] = {}

    async def hooked(key, chunks, *, max_bytes, abort_after):
        receipt = await real(key, chunks, max_bytes=max_bytes, abort_after=abort_after)
        await g.hold("jobs", JOB_LOCK, LEASE_SET, j=jid)  # the registration transaction will wait for it
        state["release"] = asyncio.ensure_future(_release_later(g))
        return receipt

    async def _release_later(gate: Gate) -> None:
        await asyncio.sleep(0.3)
        await gate.waiting()
        await gate.release()

    eng.storage.managed.put_new = hooked  # type: ignore[method-assign]
    try:
        with pytest.raises(LeaseLost):
            await w.write_artifact(c, "result", b"{}", 1000)
    finally:
        eng.storage.managed.put_new = real  # type: ignore[method-assign]
        if "release" in state:
            await state["release"]
    a = (await raw(eng, "SELECT state FROM {job_artifacts}"))[0]
    assert a.state == "reserved"  # never registered as written; cleanup seals and releases it


async def test_validation_registration_is_refused_when_the_lease_expired_during_the_wait(eng):
    _, jid, c = await claimed(eng)
    w = eng.job_worker("w")
    g = Gate(eng)
    await g.hold("jobs", JOB_LOCK, LEASE_SET, j=jid)
    out, exc = await blocked_call(eng, g, lambda: w.record_validation(c, "file_x", "ident", "passed", None))
    assert out is None and isinstance(exc, LeaseLost)
    assert (await raw(eng, "SELECT count(*) AS n FROM {file_validations}"))[0].n == 0


async def test_cancel_treats_a_lease_that_expired_during_the_wait_as_stale(eng):
    ops, jid, c = await claimed(eng)
    g = Gate(eng)
    await g.hold("jobs", JOB_LOCK, LEASE_SET, j=jid)
    out, exc = await blocked_call(eng, g, lambda: ops.cancel_job("a", jid))
    assert exc is None
    assert out.state == "cancelled" and out.cancel_outcome == "cancelled"  # T5b, not "requested" against a dead lease
    _ = c


async def test_claim_rechecks_the_input_expiry_after_waiting_for_its_lock(eng):
    ops = await ready_scope(eng)
    jid = await submit(ops)
    fid = (await row(eng, jid)).input_file_id
    g = Gate(eng)
    await g.hold("stored_files", "file_id = :f", "expires_at = :soon", f=fid)
    w = eng.job_worker("w")
    out, exc = await blocked_call(eng, g, lambda: w.claim(1))
    assert exc is None and out == []
    r = await row(eng, jid)
    assert r.state == "failed" and r.failure_code == "input_unavailable" and r.attempt_no == 0


async def test_submit_rechecks_the_input_expiry_after_waiting_for_its_lock(eng):
    ops = await ready_scope(eng)
    f = (await ops.upload_file("a", chunked(make_docx([TEXT])), idempotency_key="u")).value
    g = Gate(eng)
    await g.hold("stored_files", "file_id = :f", "expires_at = :soon", f=f.file_id)
    out, exc = await blocked_call(
        eng, g, lambda: ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="j")
    )
    assert out is None and isinstance(exc, FileNotUsableError)
    assert (await raw(eng, "SELECT count(*) AS n FROM {jobs}"))[0].n == 0


# ---- 2. the published set is exactly the contract ---------------------------------------------------
async def _published_nothing(eng, jid):
    r = await row(eng, jid)
    assert r.state != "succeeded" and r.result_artifact_id is None and r.source_map_artifact_id is None
    assert r.data_state == "none"
    assert (await raw(eng, "SELECT count(*) AS n FROM {job_artifacts} WHERE state = 'published'"))[0].n == 0


async def test_text_job_rejects_every_wrong_artifact_set(eng):
    ops = await ready_scope(eng)
    w = eng.job_worker("w")
    for i, build in enumerate(("empty", "extra_role", "unknown_id", "duplicate_via_second_role")):
        jid = await submit(ops, f"k{i}", TEXT + str(i))
        [c] = await w.claim(1)
        a = await w.write_artifact(c, "result", b"{}", 1000)
        sets = {
            "empty": {},
            "extra_role": {"result": a, "source_map": await w.write_artifact(c, "source_map", b"{}", 1000)},
            "unknown_id": {"result": "art_" + "0" * 32},
            "duplicate_via_second_role": {"result": a, "source_map": a},
        }
        assert await w.publish(c, sets[build]) == "failed", build
        r = await row(eng, jid)
        assert r.state == "failed" and r.failure_code == "internal_error", build
        await _published_nothing(eng, jid)
    u = (await raw(eng, "SELECT active_jobs, running_jobs FROM {scope_usage}"))[0]
    assert (u.active_jobs, u.running_jobs) == (0, 0)


async def test_a_wrongly_typed_or_unwritten_or_foreign_artifact_is_never_published(eng):
    ops = await ready_scope(eng)
    w = eng.job_worker("w")
    jid1 = await submit(ops, "k1", TEXT + "1")
    jid2 = await submit(ops, "k2", TEXT + "2")
    c1, c2 = await w.claim(2)
    mine = await w.write_artifact(c1, "result", b"{}", 1000)
    wrong_kind = await w.write_artifact(c1, "source_map", b"{}", 1000)
    other_job = await w.write_artifact(c2, "result", b"{}", 1000)
    assert await w.publish(c2, {"result": mine}) == "failed"  # belongs to another job (still `written`)
    r2 = await row(eng, c2.job_id)
    assert r2.state == "failed" and r2.result_artifact_id is None
    assert await w.publish(c1, {"result": wrong_kind}) == "failed"  # registered as source_map, claimed as result
    assert (await row(eng, c1.job_id)).state == "failed"
    _ = (other_job, jid1, jid2)
    assert (await raw(eng, "SELECT count(*) AS n FROM {job_artifacts} WHERE state = 'published'"))[0].n == 0


async def test_an_unwritten_reservation_cannot_be_published(eng):
    from cawbase_content_engine.jobs.setup import StorageError

    ops = await ready_scope(eng)
    jid = await submit(ops)
    w = eng.job_worker("w")
    [c] = await w.claim(1)
    real = eng.storage.managed.put_new

    async def failing(key, chunks, *, max_bytes, abort_after):
        raise StorageError

    eng.storage.managed.put_new = failing  # type: ignore[method-assign]
    try:
        with pytest.raises(Exception):  # noqa: B017  (storage_unavailable)
            await w.write_artifact(c, "result", b"{}", 1000)
    finally:
        eng.storage.managed.put_new = real  # type: ignore[method-assign]
    a = (await raw(eng, "SELECT artifact_id, state FROM {job_artifacts}"))[0]
    assert a.state == "reserved"  # a reservation whose bytes were never registered
    assert await w.publish(c, {"result": a.artifact_id}) == "failed"
    await _published_nothing(eng, jid)


async def test_normal_text_and_document_paths_publish_exactly_their_set(eng):
    ops = await ready_scope(eng)
    t = await submit(ops, "t")
    f = (await ops.upload_file("a", chunked(make_docx([TEXT])), idempotency_key="u")).value
    d = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="d")).job.job_id
    await run_all(eng)
    rt, rd = await row(eng, t), await row(eng, d)
    assert rt.state == "succeeded" and rt.result_artifact_id and rt.source_map_artifact_id is None
    assert rd.state == "succeeded" and rd.result_artifact_id and rd.source_map_artifact_id
    kinds = {(r.job_id, r.kind): r.state for r in await raw(eng, "SELECT job_id, kind, state FROM {job_artifacts}")}
    assert kinds == {(t, "result"): "published", (d, "result"): "published", (d, "source_map"): "published"}


async def test_document_job_rejects_swapped_missing_and_extra_roles(eng):
    ops = await ready_scope(eng)
    w = eng.job_worker("w")
    for i, build in enumerate(("swapped", "missing_source_map", "missing_result", "extra_role", "duplicate")):
        f = (await ops.upload_file("a", chunked(make_docx([TEXT + str(i)])), idempotency_key=f"u{i}")).value
        jid = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key=f"d{i}")).job.job_id
        await valid(eng, ops, f.file_id)  # the validation of the current identity passed (as a real run registers it)
        [c] = await w.claim(1)
        r_id = await w.write_artifact(c, "result", b"{}", 1000)
        s_id = await w.write_artifact(c, "source_map", b"{}", 1000)
        sets = {
            "swapped": {"result": s_id, "source_map": r_id},
            "missing_source_map": {"result": r_id},
            "missing_result": {"source_map": s_id},
            "extra_role": {"result": r_id, "source_map": s_id, "debug": r_id},
            "duplicate": {"result": r_id, "source_map": r_id},
        }
        assert await w.publish(c, sets[build]) == "failed", build
        await _published_nothing(eng, jid)
        rj = await row(eng, jid)
        assert rj.failure_code == "internal_error", build


# ---- 3. isolation approval belongs to the engine instance and its configuration -------------------------
class ToggleRunner:
    """host_provided runner whose verification result and use are observable; extraction is the real child."""

    profile = "host_provided"

    def __init__(self, ok: bool = True) -> None:
        self.ok, self.runs, self.verified_with = ok, 0, []
        self._inner = UnconfinedDevelopmentRunner()

    async def verify(self, limits):
        self.verified_with.append(limits)
        return self.ok

    async def run(self, data, params, *, timeout_s, limits):
        self.runs += 1
        return await self._inner.run(data, params, timeout_s=timeout_s, limits=limits)


async def _doc_engine(url, tmp_path, docs, name, **limits):
    e = ContentEngine(
        EngineConfig(database_url=url),
        inference=InferenceSetup(StubAdapter(), timeout_s=5),
        storage=StorageSetup(managed=FilesystemStorage(tmp_path / name)),
        documents=docs,
        jobs=JobsSetup(limits=fast_limits(**limits)),
    )
    await e.start()
    return e


async def _one_doc_job(e, key):
    if not await raw(e, "SELECT 1 FROM {scopes}"):
        ops = await ready_scope(e)
    else:
        ops = e.scoped(e.trusted_scope_context("a", FULL, actor="test-host"))
    f = (await ops.upload_file("a", chunked(make_docx([TEXT + key])), idempotency_key="u" + key)).value
    jid = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="d" + key)).job.job_id
    await run_all(e)
    return await ops.get_job("a", jid)


async def test_a_new_engine_does_not_inherit_the_approval_of_another_one(migrated, tmp_path):
    _, url = migrated
    runner = ToggleRunner(ok=True)
    docs = DocumentSetup(runner=runner, untrusted_input=True)
    e1 = await _doc_engine(url, tmp_path, docs, "o1")
    try:
        assert (await _one_doc_job(e1, "1")).state == "succeeded"
        assert runner.runs == 1 and e1._isolation_approved is not None
    finally:
        await e1.aclose()
    runner.ok = False  # the very same DocumentSetup / runner: a new engine must verify again and fail
    e2 = await _doc_engine(url, tmp_path, docs, "o2")
    try:
        assert e2._isolation_approved is None
        j = await _one_doc_job(e2, "2")
        assert j.state == "failed" and j.failure_code == "configuration_unavailable"
        assert runner.runs == 1, "a failed verification must not invoke the extraction"
    finally:
        await e2.aclose()


async def test_verification_uses_the_limits_of_the_job_and_is_repeated_when_they_change(migrated, tmp_path):
    _, url = migrated
    runner = ToggleRunner(ok=True)
    docs = DocumentSetup(runner=runner, untrusted_input=True)
    e = await _doc_engine(url, tmp_path, docs, "o1", extraction_memory_bytes=300 * 1024 * 1024, extraction_max_pids=40)
    try:
        assert (await _one_doc_job(e, "1")).state == "succeeded"
        lim = runner.verified_with[-1]
        assert lim.extraction_memory_bytes == 300 * 1024 * 1024 and lim.extraction_max_pids == 40
        assert (await _one_doc_job(e, "2")).state == "succeeded"
        assert len(runner.verified_with) == 1  # same instance, same configuration: verified once
        from cawbase_content_engine.jobs.execute import _isolation_key

        assert _isolation_key(docs, lim) == e._isolation_approved
        assert _isolation_key(docs, fast_limits(extraction_memory_bytes=1)) != e._isolation_approved
    finally:
        await e.aclose()


async def test_a_verification_that_raises_is_configuration_unavailable_without_fallback(migrated, tmp_path):
    _, url = migrated

    class Boom(ToggleRunner):
        async def verify(self, limits):
            raise OSError("docker is not there")

    runner = Boom()
    e = await _doc_engine(url, tmp_path, DocumentSetup(runner=runner, untrusted_input=True), "o1")
    try:
        j = await _one_doc_job(e, "1")
        assert j.state == "failed" and j.failure_code == "configuration_unavailable" and runner.runs == 0
        assert e._isolation_approved is None
    finally:
        await e.aclose()


def test_there_is_no_module_level_approval_cache():
    import cawbase_content_engine.jobs.execute as ex

    assert not hasattr(ex, "_ISOLATION_VERIFIED")


# ---- 4. lock order (docs 6.2) ---------------------------------------------------------------------------
RANK = {
    "scopes": 1,
    "scope_job_fences": 2,
    "scope_usage": 3,
    "engine_usage": 4,
    "idempotency_records": 5,
    "stored_files": 6,
    "file_validations": 7,
    "jobs": 8,
    "job_artifacts": 9,
}


class LockLog:
    """Records, per transaction, the table of every row-locking statement (PostgreSQL ``FOR ...``)."""

    def __init__(self, eng: ContentEngine) -> None:
        self.eng, self.txs = eng, []
        self.names = {eng.tables.ns.table(n): n for n in RANK}

    def __enter__(self):
        sync = self.eng._engine.sync_engine
        sa.event.listen(sync, "begin", self._begin)
        sa.event.listen(sync, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *a):
        sync = self.eng._engine.sync_engine
        sa.event.remove(sync, "begin", self._begin)
        sa.event.remove(sync, "before_cursor_execute", self._hook)

    def _begin(self, conn):
        self.txs.append([])

    def _hook(self, conn, cursor, statement, parameters, context, executemany):
        if not self.txs or not re.search(r"\bFOR (?:KEY SHARE|SHARE|NO KEY UPDATE|UPDATE)\b", statement):
            return
        m = re.search(r'FROM\s+(?:"?\w+"?\.)?"?(\w+)"?', statement)
        if m and m.group(1) in self.names:
            self.txs[-1].append(RANK[self.names[m.group(1)]])

    def ranks(self) -> list[list[int]]:
        return [t for t in self.txs if t]


def assert_ordered(log: LockLog, min_txs: int = 1):
    seqs = [s for s in log.ranks() if s]
    assert len(seqs) >= min_txs
    for s in seqs:
        assert s == sorted(s), f"lock order inverted: {s}"


@pytest.fixture
async def pg_eng(pg_url, tmp_path):
    from cawbase_content_engine.migrations import upgrade

    await upgrade(EngineConfig(database_url=pg_url))
    e = await make_engine(pg_url, tmp_path)
    e._test_url = pg_url  # type: ignore[attr-defined]
    yield e
    await e.aclose()


async def test_claim_publish_cancel_and_deadline_expiry_take_locks_in_the_documented_order(pg_eng):
    from cawbase_content_engine.jobs.maintenance import expire_deadlines

    eng = pg_eng
    ops = await ready_scope(eng)
    await submit(ops, "a")
    await submit(ops, "b", TEXT + "b")
    w = eng.job_worker("w")
    with LockLog(eng) as log:
        c1, c2 = await w.claim(2)
    assert_ordered(log, 2)
    assert any(6 in s and 8 in s for s in log.ranks()), "the claim must lock the inputs (6) before the job (8)"
    art = await w.write_artifact(c1, "result", b"{}", 1000)
    with LockLog(eng) as log:
        assert await w.publish(c1, {"result": art}) == "succeeded"
    assert_ordered(log)
    assert any(6 in s and 8 in s and 9 in s for s in log.ranks())
    with LockLog(eng) as log:
        await ops.cancel_job("a", c2.job_id)
    assert_ordered(log)
    await submit(ops, "c", TEXT + "c")
    with LockLog(eng) as log:
        await expire_deadlines(eng)
    assert_ordered(log)
    with LockLog(eng) as log:
        await submit(ops, "d", TEXT + "d")
    assert_ordered(log)


async def test_a_claim_waiting_for_an_input_does_not_hold_the_job_row(pg_eng):
    """Race claim vs. a change of the input: while the claim waits for the input (6), the job row (8) is still free,
    so a writer that takes 6 -> 8 (delete / expiry / cancel paths) cannot be blocked behind it in the reverse order."""
    eng = pg_eng
    ops = await ready_scope(eng)
    jid = await submit(ops)
    fid = (await row(eng, jid)).input_file_id
    other = create_async_engine(eng._test_url)  # type: ignore[attr-defined]
    async with other.connect() as b:
        await b.begin()
        await b.execute(sa.text(f"SELECT 1 FROM {qn(eng, 'stored_files')} WHERE file_id = :f FOR UPDATE"), {"f": fid})
        claim = asyncio.ensure_future(eng.job_worker("w").claim(1))
        for _ in range(200):
            n = (
                await b.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
                    )
                )
            ).scalar_one()
            if n:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the claim never blocked on the input")
        await b.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
        # the writer's second lock (job row) is granted at once: the claim does not hold it
        await b.execute(sa.text(f"SELECT 1 FROM {qn(eng, 'jobs')} WHERE job_id = :j FOR UPDATE"), {"j": jid})
        await b.execute(
            sa.text(f"UPDATE {qn(eng, 'stored_files')} SET state = 'deleted' WHERE file_id = :f"), {"f": fid}
        )
        await b.commit()
    await other.dispose()
    assert await asyncio.wait_for(claim, 30) == []
    r = await row(eng, jid)
    assert r.state == "failed" and r.failure_code == "input_unavailable" and r.attempt_no == 0


async def test_claims_racing_with_input_deletion_never_deadlock_and_never_run_a_deleted_input(pg_eng):
    """Supplementary (a race is not a proof of absence of deadlocks; the order tests above are the evidence)."""
    eng = pg_eng
    ops = await ready_scope(eng)
    ids = [await submit(ops, f"k{i}", TEXT + str(i)) for i in range(8)]
    files = [(await row(eng, j)).input_file_id for j in ids]

    from cawbase_content_engine.jobs import db as jdb
    from cawbase_content_engine.jobs import files as fl

    ctx = fl.Ctx(eng)

    async def deleter(fid):
        """What delete_file does for an input: 1 -> 3,4 -> 6 (the text inputs are internal, not user-deletable)."""
        await asyncio.sleep(0.01)

        async def fn(conn):
            now = await jdb.db_now(conn, ctx.backend)
            await jdb.lock_scope(conn, ctx.t, "a", exclusive=False)
            await jdb.lock_usage(conn, ctx.t, "a", now)
            r = await fl.get_file_row(conn, ctx.t, "a", fid, lock=True)
            if r is not None and r.state == "stored":
                await jdb.retire_object(conn, ctx.t, now, ctx.ret, ctx.t.stored_files, r, "deleted", delay_s=60)

        await ctx.run(fn)

    results = await asyncio.gather(
        *[eng.job_worker(f"w{i}").claim(2) for i in range(4)], *[deleter(f) for f in files], return_exceptions=True
    )
    assert not [r for r in results if isinstance(r, BaseException)], results
    states = {(await row(eng, j)).state for j in ids}
    assert states <= {"queued", "running", "failed", "cancelled"}
