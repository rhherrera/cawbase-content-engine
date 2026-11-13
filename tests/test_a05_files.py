"""A05 files and storage: five-step protocol, seals, writer-release evidence, quotas, spool, external references."""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cawbase_content_engine.errors import (
    ContentMismatchError,
    IdempotencyConflictError,
    PayloadTooLargeError,
    QuotaExceededError,
    ResourceNotFoundError,
    StoredObjectUnavailableError,
    UploadAbortedError,
    UploadInProgressError,
)
from cawbase_content_engine.jobs.setup import ExternalObjectInfo, ObjectMissing, ResolverDescriptor, StorageKeyExists
from cawbase_content_engine.jobs.storage import FilesystemStorage, new_key

from .a05_helpers import chunked, fast_limits, make_docx, make_engine, make_pdf, raw, ready_scope

PDF = make_pdf([["Hola mundo"]])


@pytest.fixture
async def eng(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path)
    yield e
    await e.aclose()


async def valid(eng, ops, file_id):
    """Marks the validation of the current identity as passed (what a document job would register)."""
    from cawbase_content_engine.jobs.ident import validation_identity

    f = await ops.get_file("a", file_id)
    ident = validation_identity(f.sha256, eng.jobs_setup.limits)
    import sqlalchemy as sa

    from cawbase_content_engine.jobs import db as jdb

    async def ins(conn):
        await conn.execute(
            sa.insert(eng.tables.file_validations).values(
                scope_id="a",
                file_id=file_id,
                validation_identity=ident,
                validator_id="v",
                validator_version="1",
                validator_config_sha256="c",
                result="passed",
                validated_at=await jdb.db_now(conn, eng.backend),
            )
        )

    await eng._run(ins, write=True, tx=None)


async def test_upload_roundtrip_is_verified_and_accounted(eng):
    ops = await ready_scope(eng)
    res = await ops.upload_file("a", chunked(PDF), idempotency_key="u1")
    f = res.value
    assert (
        res.created and f.state == "stored" and f.sha256 == hashlib.sha256(PDF).hexdigest() and f.size_bytes == len(PDF)
    )
    assert f.expires_at - f.received_at == timedelta(days=7)  # D-A05-01: 7 days from the durable receipt
    u = (await raw(eng, "SELECT reserved_bytes, stored_bytes, open_reservations, live_files FROM {scope_usage}"))[0]
    assert (u.reserved_bytes, u.stored_bytes, u.open_reservations, u.live_files) == (0, len(PDF), 0, 1)
    # not downloadable while in quarantine (no validation yet): same 404 as a missing file
    with pytest.raises(ResourceNotFoundError):
        await ops.open_file_content("a", f.file_id)
    await valid(eng, ops, f.file_id)
    stream = await ops.open_file_content("a", f.file_id)
    assert b"".join([c async for c in stream]) == PDF
    assert list((eng.storage.managed.root / "spool").glob("*/dl-*")) == []  # spool file removed


async def test_replay_hashes_without_writing_and_conflicts_on_other_content(eng):
    ops = await ready_scope(eng)
    first = (await ops.upload_file("a", chunked(PDF), idempotency_key="u1")).value
    before = len(list((eng.storage.managed.root / "objects").rglob("*")))
    again = await ops.upload_file("a", chunked(PDF), idempotency_key="u1")
    assert not again.created and again.value.file_id == first.file_id
    assert len(list((eng.storage.managed.root / "objects").rglob("*"))) == before
    with pytest.raises(IdempotencyConflictError):
        await ops.upload_file("a", chunked(PDF + b"x"), idempotency_key="u1")


async def test_false_declared_size_never_exceeds_its_reservation(eng):
    ops = await ready_scope(eng)
    with pytest.raises(ContentMismatchError):  # declares less than it sends: aborted at the declared bound
        await ops.upload_file("a", chunked(PDF * 3), idempotency_key="small", declared_size=len(PDF))
    with pytest.raises(ContentMismatchError):  # declares more than it sends
        await ops.upload_file("a", chunked(PDF), idempotency_key="big", declared_size=len(PDF) + 50)
    u = (
        await raw(
            eng,
            "SELECT reserved_bytes, stored_bytes, uncertain_bytes, pending_seals, open_reservations, live_files FROM {scope_usage}",
        )
    )[0]
    # nothing was registered; both writes are *uncertain* (not freed) until a seal and release are confirmed
    assert u.reserved_bytes == 0 and u.stored_bytes == 0 and u.open_reservations == 0 and u.live_files == 0
    assert u.uncertain_bytes == (len(PDF) + (len(PDF) + 50)) and u.pending_seals == 2


async def test_failed_uploads_keep_their_charge_until_release_is_confirmed(eng):
    ops = await ready_scope(eng)
    with pytest.raises(UploadAbortedError):

        async def boom():
            yield b"abc"
            raise ConnectionError

        await ops.upload_file("a", boom(), idempotency_key="f1")
    s1 = (await raw(eng, "SELECT uncertain_bytes, pending_seals FROM {scope_usage}"))[0]
    assert s1.uncertain_bytes > 0 and s1.pending_seals == 1
    m = eng.maintenance()
    # abandoned rows only become purgeable after the 24 h temporary retention: nothing is released early
    summary = await m.run_cleanup()
    assert summary.counts.get("purged_physically", 0) == 0
    await raw(
        eng,
        "UPDATE {stored_files} SET delete_after = :t",
        t=datetime(2020, 1, 1) if eng.backend.value == "sqlite" else datetime(2020, 1, 1, tzinfo=UTC),
    )
    summary = await m.run_cleanup()
    assert summary.counts["purged_physically"] == 1 and not summary.diagnostics
    s2 = (await raw(eng, "SELECT uncertain_bytes, pending_seals, sealed_keys FROM {scope_usage}"))[0]
    assert (s2.uncertain_bytes, s2.pending_seals, s2.sealed_keys) == (0, 0, 1)  # the permanent seal stays counted


async def test_seal_capacity_is_finite_and_rejects_without_removing_seals(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path, limits=fast_limits(max_seals_per_scope=2))
    try:
        ops = await ready_scope(e)
        for i in range(2):
            with pytest.raises(UploadAbortedError):

                async def boom():
                    yield b"x"
                    raise ConnectionError

                await ops.upload_file("a", boom(), idempotency_key=f"f{i}")
        with pytest.raises(QuotaExceededError) as ei:
            await ops.upload_file("a", chunked(PDF), idempotency_key="ok")
        assert getattr(ei.value, "reason", None) == "seal_capacity"
        assert (await raw(e, "SELECT pending_seals FROM {scope_usage}"))[0].pending_seals == 2
    finally:
        await e.aclose()


async def test_oversized_upload_is_cut_at_the_limit(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path, limits=fast_limits(max_file_bytes=1000))
    try:
        ops = await ready_scope(e)
        with pytest.raises(PayloadTooLargeError):
            await ops.upload_file("a", chunked(b"x" * 5000, 100), idempotency_key="big")
    finally:
        await e.aclose()


async def test_in_progress_then_restart_with_generation(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path)
    try:
        ops = await ready_scope(e)
        gate = asyncio.Event()

        async def slow():
            yield b"abc"
            await gate.wait()
            yield b"def"

        t1 = asyncio.create_task(ops.upload_file("a", slow(), idempotency_key="g"))
        await asyncio.sleep(0.3)
        with pytest.raises(UploadInProgressError):
            await ops.upload_file("a", chunked(PDF), idempotency_key="g")
        # the first upload's deadline passes: a restart under the same key takes a new generation
        await raw(
            e,
            "UPDATE {stored_files} SET upload_deadline_at = :t",
            t=datetime(2020, 1, 1) if e.backend.value == "sqlite" else datetime(2020, 1, 1, tzinfo=UTC),
        )
        res = await ops.upload_file("a", chunked(PDF), idempotency_key="g")
        assert res.created and res.value.size_bytes == len(PDF)
        gate.set()
        with pytest.raises(UploadAbortedError):  # the OLD writer can no longer finalize anything
            await t1
        files = await raw(e, "SELECT state FROM {stored_files} ORDER BY created_at")
        assert sorted(r.state for r in files) == ["abandoned", "stored"]
    finally:
        await e.aclose()


# ---- filesystem storage: seals and writer-release evidence ----------------------------------------
async def test_seal_blocks_a_suspended_writer_and_release_needs_evidence(tmp_path):
    fs = FilesystemStorage(tmp_path / "s")
    key = new_key()
    started, proceed = asyncio.Event(), asyncio.Event()

    async def chunks():
        yield b"a" * 10
        started.set()
        await proceed.wait()  # suspended with the temporary's descriptor OPEN
        yield b"b" * 10

    far = datetime.now(UTC) + timedelta(hours=1)
    put = asyncio.create_task(fs.put_new(key, chunks(), max_bytes=1000, abort_after=far))
    await started.wait()
    await fs.delete_and_seal(key)
    assert await fs.writers_released(key) is False  # the seal alone proves nothing about the live descriptor
    await fs.delete_and_seal(key)  # idempotent
    proceed.set()
    with pytest.raises(StorageKeyExists):  # the late publish by path fails on the seal
        await put
    assert await fs.stat(key) is None
    assert await fs.writers_released(key) is True  # evidence only after the writer is gone
    await fs.delete(key)  # a normal delete never removes a seal
    assert (fs.root / "objects" / key.split("/")[1] / key.split("/")[2]).is_dir()
    with pytest.raises(StorageKeyExists):
        await fs.put_new(key, _one(b"zz"), max_bytes=10, abort_after=far)


async def test_pause_between_deadline_check_and_publish_cannot_materialize(tmp_path):
    fs = FilesystemStorage(tmp_path / "s")
    key = new_key()
    after_check = asyncio.Event()
    far = datetime.now(UTC) + timedelta(hours=1)
    orig_link = os.link

    def slow_link(src, dst, *a, **k):  # the process is "suspended" right before it makes the name visible
        import time

        after_check_loop.call_soon_threadsafe(after_check.set)
        while not release_flag["go"]:
            time.sleep(0.01)
        return orig_link(src, dst, *a, **k)

    release_flag = {"go": False}
    after_check_loop = asyncio.get_running_loop()
    import unittest.mock as um

    with um.patch("os.link", slow_link):
        put = asyncio.create_task(fs.put_new(key, _one(b"data"), max_bytes=100, abort_after=far))
        await after_check.wait()
        await fs.delete_and_seal(key)
        release_flag["go"] = True
        with pytest.raises(StorageKeyExists):
            await put
    assert await fs.stat(key) is None
    assert await fs.writers_released(key) is True


async def test_concurrent_cleaners_on_a_sealed_key_are_harmless(tmp_path):
    fs = FilesystemStorage(tmp_path / "s")
    key = new_key()
    await asyncio.gather(*[fs.delete_and_seal(key) for _ in range(8)])
    rel = await asyncio.gather(*[fs.writers_released(key) for _ in range(8)])
    assert all(rel)
    await asyncio.gather(*[fs.delete(key) for _ in range(4)])
    with pytest.raises(ObjectMissing):
        await fs.open_read(key).__anext__()
    await fs.delete_and_seal(key)  # still sealed afterwards


async def _one(data: bytes):
    yield data


# ---- verified download of a corrupt object ----------------------------------------------------------
async def test_corrupt_object_is_never_delivered_and_is_marked_lost(eng):
    ops = await ready_scope(eng)
    f = (await ops.upload_file("a", chunked(PDF), idempotency_key="c1")).value
    await valid(eng, ops, f.file_id)
    key = (await raw(eng, "SELECT storage_key FROM {stored_files}"))[0].storage_key
    path = eng.storage.managed.root / "objects" / key.split("/")[1] / key.split("/")[2]
    path.write_bytes(PDF[:-1] + b"X")
    with pytest.raises(StoredObjectUnavailableError):
        await ops.open_file_content("a", f.file_id)
    got = await ops.get_file("a", f.file_id)
    assert got.state == "lost" and got.integrity_failure == "hash_mismatch"
    assert list((eng.storage.managed.root / "spool").glob("*/dl-*")) == []


# ---- external references -------------------------------------------------------------------------------
class Resolver:
    def __init__(self, data: bytes, *, stable: bool) -> None:
        self.data, self.stable, self.deleted, self.describes, self.version = data, stable, 0, 0, "v1"

    @property
    def descriptor(self):
        return ResolverDescriptor(resolver_id="host", stable_versions=self.stable)

    async def describe(self, ref):
        self.describes += 1
        return ExternalObjectInfo(version_id=self.version, size=len(self.data))

    async def open_read(self, ref, version_id):
        if version_id != self.version:
            raise ObjectMissing
        yield self.data


async def test_external_reference_is_pinned_never_deleted_and_detects_change(migrated, tmp_path):
    from cawbase_content_engine import ContentEngine, EngineConfig
    from cawbase_content_engine.jobs.setup import JobsSetup, StorageSetup

    _, url = migrated
    res = Resolver(PDF, stable=True)
    base = await make_engine(url, tmp_path)
    await base.aclose()
    e = ContentEngine(
        EngineConfig(database_url=url),
        inference=base.inference,
        storage=StorageSetup(managed=FilesystemStorage(tmp_path / "o2"), external_resolvers={"host": res}),
        jobs=JobsSetup(limits=fast_limits()),
    )
    await e.start()
    try:
        ops = await ready_scope(e)
        r1 = await ops.register_external_file("a", resolver_id="host", ref="opaque-ref", idempotency_key="e1")
        assert (
            r1.created
            and r1.value.origin == "external_reference"
            and r1.value.sha256 == hashlib.sha256(PDF).hexdigest()
        )
        r2 = await ops.register_external_file("a", resolver_id="host", ref="opaque-ref", idempotency_key="e1")
        assert not r2.created and res.describes == 1  # replay: the pinned version is NOT re-resolved
        assert (await raw(e, "SELECT stored_bytes FROM {scope_usage}"))[0].stored_bytes == 0  # no managed bytes
        # the host changes its object: the pinned version is gone / different
        res.version = "v2"
        from cawbase_content_engine.jobs import files as fl

        row = await e._run(lambda c: fl.get_file_row(c, e.tables, "a", r1.value.file_id), write=False, tx=None)
        with pytest.raises(Exception):
            await fl.read_verified(fl.Ctx(e), row)
        # deleting the file never touches the host's object
        await ops.delete_file("a", r1.value.file_id)
        await e.maintenance().run_cleanup()
        assert res.deleted == 0 and res.data == PDF
    finally:
        await e.aclose()


async def test_unstable_resolver_is_materialized(migrated, tmp_path):
    from cawbase_content_engine import ContentEngine, EngineConfig
    from cawbase_content_engine.jobs.setup import JobsSetup, StorageSetup

    _, url = migrated
    res = Resolver(PDF, stable=False)
    base = await make_engine(url, tmp_path)
    await base.aclose()
    e = ContentEngine(
        EngineConfig(database_url=url),
        inference=base.inference,
        storage=StorageSetup(managed=FilesystemStorage(tmp_path / "o3"), external_resolvers={"host": res}),
        jobs=JobsSetup(limits=fast_limits()),
    )
    await e.start()
    try:
        ops = await ready_scope(e)
        r = (await ops.register_external_file("a", resolver_id="host", ref="r", idempotency_key="m1")).value
        assert r.origin == "external_materialized"
        assert (await raw(e, "SELECT stored_bytes FROM {scope_usage}"))[0].stored_bytes == len(PDF)
    finally:
        await e.aclose()


# ---- spool residue ------------------------------------------------------------------------------------------
def test_spool_sweeps_dead_owners_but_never_a_live_one(tmp_path):
    from cawbase_content_engine.jobs.spool import Spool

    code = (
        "import sys, time\nfrom cawbase_content_engine.jobs.spool import Spool\n"
        "s = Spool(sys.argv[1], 1000); d = s.start(); (d / 'dl-1-x').write_bytes(b'x' * 10); print(d, flush=True); time.sleep(60)\n"
    )
    live = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)  # noqa: S603
    dead = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)  # noqa: S603
    live_dir, dead_dir = live.stdout.readline().strip(), dead.stdout.readline().strip()
    os.kill(dead.pid, signal.SIGKILL)
    dead.wait()
    try:
        mine = Spool(tmp_path, 1000)
        mine.start()
        assert not os.path.exists(dead_dir)  # residue of the dead process was swept at startup
        assert os.path.isdir(live_dir) and os.path.exists(os.path.join(live_dir, "dl-1-x"))  # the live one is untouched
        assert mine.sweep() == 0
        mine.close()
    finally:
        live.kill()
        live.wait()
        live.stdout.close()
        dead.stdout.close()


_ = (replace, make_docx)
