"""A05 failures at the database/storage boundaries, unknown commits, input loss during execution, quotas under load."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from cawbase_content_engine import ContentEngine, EngineConfig
from cawbase_content_engine.errors import DatabaseUnavailableError, QuotaExceededError, StoredObjectUnavailableError
from cawbase_content_engine.inference import InferenceSetup
from cawbase_content_engine.jobs.setup import JobsSetup, StorageError, StorageSetup
from cawbase_content_engine.jobs.storage import FilesystemStorage

from .a05_helpers import TEXT, StubAdapter, at, chunked, fast_limits, make_docx, make_engine, raw, ready_scope, run_all
from .test_a05_files import valid


@pytest.fixture
async def eng(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path)
    yield e
    await e.aclose()


class FlakyStorage(FilesystemStorage):
    """Fails transiently on demand (the host's storage misbehaving), otherwise a real filesystem backend."""

    def __init__(self, root) -> None:
        super().__init__(root)
        self.fail_put = 0
        self.fail_delete = False

    async def put_new(self, key, chunks, *, max_bytes, abort_after):
        if self.fail_put > 0:
            self.fail_put -= 1
            raise StorageError
        return await super().put_new(key, chunks, max_bytes=max_bytes, abort_after=abort_after)

    async def delete(self, key):
        if self.fail_delete:
            raise StorageError
        await super().delete(key)

    async def delete_and_seal(self, key):
        if self.fail_delete:
            raise StorageError
        await super().delete_and_seal(key)


async def flaky_engine(url, tmp_path, **limits):
    st = FlakyStorage(tmp_path / "flaky")
    e = ContentEngine(
        EngineConfig(database_url=url),
        inference=InferenceSetup(StubAdapter(), timeout_s=5),
        storage=StorageSetup(managed=st),
        jobs=JobsSetup(limits=fast_limits(**limits)),
    )
    await e.start()
    return e, st


async def test_transient_storage_failure_retries_and_accounts_the_discarded_attempt(migrated, tmp_path):
    _, url = migrated
    e, st = await flaky_engine(url, tmp_path)
    try:
        ops = await ready_scope(e)
        jid = (await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k")).job.job_id
        # the input text is already stored; only the RESULT write fails once
        st.fail_put = 1
        for _ in range(15):
            await run_all(e)
            if (await ops.get_job("a", jid)).state == "succeeded":
                break
            await asyncio.sleep(0.1)
        job = await ops.get_job("a", jid)
        assert job.state == "succeeded" and job.attempt_count == 2
        attempts = await raw(e, "SELECT outcome, error_code FROM {job_attempts} ORDER BY attempt_no")
        assert [(a.outcome, a.error_code) for a in attempts] == [
            ("retryable_error", "storage_unavailable"),
            ("succeeded", None),
        ]
        arts = await raw(e, "SELECT attempt_no, state FROM {job_artifacts} ORDER BY attempt_no")
        assert [(a.attempt_no, a.state) for a in arts] == [(1, "discarded"), (2, "published")]
        u = (await raw(e, "SELECT uncertain_bytes, pending_seals, reserved_bytes FROM {scope_usage}"))[0]
        assert (
            u.uncertain_bytes > 0 and u.pending_seals == 1 and u.reserved_bytes == 0
        )  # not freed just because it failed
    finally:
        await e.aclose()


async def test_failing_cleanup_never_releases_the_charge_and_reports_diagnostics(migrated, tmp_path):
    _, url = migrated
    e, st = await flaky_engine(url, tmp_path)
    try:
        ops = await ready_scope(e)
        for i in range(3):
            from cawbase_content_engine.errors import UploadAbortedError

            with pytest.raises(UploadAbortedError):

                async def boom():
                    yield b"abc"
                    raise ConnectionError

                await ops.upload_file("a", boom(), idempotency_key=f"f{i}")
        await raw(e, "UPDATE {stored_files} SET delete_after = :t", t=at(e, delta=-timedelta(seconds=1)))
        st.fail_delete = True
        s = await e.maintenance().run_cleanup()
        assert s.diagnostics["purge_pending_backend_error"] == 3 and s.counts.get("purged_physically", 0) == 0
        u = (await raw(e, "SELECT uncertain_bytes, pending_seals, sealed_keys FROM {scope_usage}"))[0]
        assert u.uncertain_bytes > 0 and u.pending_seals == 3 and u.sealed_keys == 0
        # repeated failed uploads keep growing the charge (they cannot exceed the quota by escaping it)
        st.fail_delete = False
        s2 = await e.maintenance().run_cleanup()
        assert s2.counts["purged_physically"] == 3
        assert (await raw(e, "SELECT uncertain_bytes, sealed_keys FROM {scope_usage}"))[0].sealed_keys == 3
    finally:
        await e.aclose()


async def test_concurrent_uploads_cannot_exceed_the_storage_quota(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path, limits=fast_limits(max_storage_bytes_per_scope=3500, max_file_bytes=1000))
    try:
        ops = await ready_scope(e)
        res = await asyncio.gather(
            *[
                ops.upload_file("a", chunked(b"x" * 900 + bytes([i]), 100), idempotency_key=f"q{i}", declared_size=901)
                for i in range(8)
            ],
            return_exceptions=True,
        )
        ok = [r for r in res if not isinstance(r, Exception)]
        denied = [r for r in res if isinstance(r, QuotaExceededError)]
        assert len(ok) == 3 and len(denied) == 5, [type(r).__name__ for r in res]
        u = (await raw(e, "SELECT reserved_bytes + stored_bytes + uncertain_bytes AS charge FROM {scope_usage}"))[0]
        assert u.charge <= 3500 and u.charge == 3 * 901
    finally:
        await e.aclose()


async def test_publication_with_unknown_commit_result_is_reconciled_not_repeated(eng):
    ops = await ready_scope(eng)
    jid = (await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k")).job.job_id
    w = eng.job_worker("w")
    real = w.publish
    calls = {"n": 0}

    async def lost_answer(c, ids):
        calls["n"] += 1
        await real(c, ids)  # the transaction COMMITS ...
        raise DatabaseUnavailableError()  # ... but the connection dies before the answer arrives

    w.publish = lost_answer  # type: ignore[method-assign]
    [c] = await w.claim(1)
    outcome = await w.run_claimed(c)
    assert calls["n"] == 1 and outcome.state == "succeeded"  # reconciled by reading the job, never published twice
    assert len(await raw(eng, "SELECT 1 FROM {job_artifacts} WHERE state = 'published'")) == 1
    assert (await ops.get_job("a", jid)).attempt_count == 1


async def test_input_deleted_or_expired_while_running_prevents_publication(eng):
    ops = await ready_scope(eng)
    f = (await ops.upload_file("a", chunked(make_docx([TEXT])), idempotency_key="u")).value
    j1 = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="j1")).job.job_id
    w = eng.job_worker("w")
    [c] = await w.claim(1)
    art = await w.write_artifact(c, "result", b"{}", 1000)
    await valid(eng, ops, f.file_id)
    await ops.delete_file("a", f.file_id)  # the original is deleted while the attempt runs
    assert await w.publish(c, {"result": art}) == "failed"  # delete won: nothing is published
    j = await ops.get_job("a", j1)
    assert j.state == "failed" and j.failure_code == "input_unavailable" and j.data_state == "none"
    # the opposite order: publication first, a later deletion of the original never reaches the result
    f2 = (await ops.upload_file("a", chunked(make_docx([TEXT + "2"])), idempotency_key="u2")).value
    j2 = (await ops.submit_document_analysis("a", file_id=f2.file_id, idempotency_key="j2")).job.job_id
    await run_all(eng)
    assert (await ops.get_job("a", j2)).state == "succeeded"
    await ops.delete_file("a", f2.file_id)
    assert (await ops.get_job("a", j2)).data_state == "available"
    assert b"document_analysis" in b"".join([x async for x in await ops.open_job_artifact("a", j2, "result")])


async def test_corrupt_published_result_is_never_delivered(eng):
    ops = await ready_scope(eng)
    jid = (await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k")).job.job_id
    await run_all(eng)
    key = (await raw(eng, "SELECT storage_key FROM {job_artifacts} WHERE state = 'published'"))[0].storage_key
    path = eng.storage.managed.root / "objects" / key.split("/")[1] / key.split("/")[2]
    path.write_bytes(path.read_bytes()[:-3] + b"???")
    with pytest.raises(StoredObjectUnavailableError):
        await ops.get_job_result("a", jid)
    assert (await ops.get_job("a", jid)).data_state == "lost"


async def test_a_worker_that_cannot_reproduce_the_pinned_configuration_never_claims(migrated, tmp_path):
    _, url = migrated
    e1 = await make_engine(url, tmp_path)
    ops = await ready_scope(e1)
    jid = (await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k")).job.job_id
    other = await make_engine(url, tmp_path, adapter=StubAdapter(config_id="a-different-configuration"), name="o2")
    try:
        assert await other.job_worker("w").claim(5) == []  # different adapter configuration: not claimable
        assert await run_all(e1) == 1  # the matching runtime runs it
        assert (await ops.get_job("a", jid)).state == "succeeded"
        # a job nobody compatible claims before its deadline fails explicitly
        j2 = (await ops.submit_text_analysis("a", text=TEXT + "!", idempotency_key="k2")).job.job_id
        await raw(
            e1, "UPDATE {jobs} SET deadline_at = :t WHERE job_id = :j", t=at(e1, delta=-timedelta(seconds=1)), j=j2
        )
        await other.maintenance().run_cleanup()
        j = await ops.get_job("a", j2)
        assert j.state == "failed" and j.failure_code == "executor_unavailable"
    finally:
        await other.aclose()
        await e1.aclose()


_ = make_docx
