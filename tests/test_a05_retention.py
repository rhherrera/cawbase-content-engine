"""A05 retention, expiry, bounded cleanup, tombstones and the admin_events purge (D-A05-01)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cawbase_content_engine import EngineConfig
from cawbase_content_engine.errors import (
    FileExpiringError,
    JobDataExpiredError,
    JobNotTerminalError,
    ResourceNotFoundError,
    UploadAbortedError,
    ValidationFailedError,
)
from cawbase_content_engine.migrations import upgrade

from .a05_helpers import TEXT, at, chunked, make_docx, make_engine, raw, ready_scope, run_all
from .test_a05_files import valid

DAY = timedelta(days=1)


@pytest.fixture
async def eng(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path)
    yield e
    await e.aclose()


async def shift(eng, table: str, column: str, delta: timedelta, where: str = "1=1") -> None:
    """Moves a stored timestamp (tests only: stands in for the passing of time on the database clock)."""
    now = at(eng, delta=delta)
    await raw(eng, f"UPDATE {{{table}}} SET {column} = :t WHERE {where}", t=now)


async def test_original_retention_is_seven_days_from_the_durable_receipt_and_expiry_denies_access(eng):
    ops = await ready_scope(eng)
    f = (await ops.upload_file("a", chunked(make_docx([TEXT])), idempotency_key="u")).value
    assert f.expires_at - f.received_at == 7 * DAY
    await valid(eng, ops, f.file_id)
    await shift(eng, "stored_files", "expires_at", -timedelta(seconds=1))
    # logical expiry denies access even though nothing was purged yet
    with pytest.raises(ResourceNotFoundError):
        await ops.open_file_content("a", f.file_id)
    summary = await eng.maintenance().run_cleanup()
    assert summary.counts["expired"] == 1
    assert (await ops.get_file("a", f.file_id)).state == "expired"
    u = (await raw(eng, "SELECT stored_bytes, uncertain_bytes, live_files FROM {scope_usage}"))[0]
    assert u.stored_bytes == 0 and u.uncertain_bytes > 0 and u.live_files == 0  # bytes count until released
    await shift(eng, "stored_files", "delete_after", -timedelta(seconds=1))
    s2 = await eng.maintenance().run_cleanup()
    assert s2.counts["purged_physically"] == 1 and not s2.diagnostics
    assert (await raw(eng, "SELECT uncertain_bytes FROM {scope_usage}"))[0].uncertain_bytes == 0
    assert list((eng.storage.managed.root / "objects").rglob("*")) == [] or all(
        p.is_dir() for p in (eng.storage.managed.root / "objects").rglob("*")
    )
    # file metadata + upload idempotency are purged 30 days after the end of life; afterwards the key creates a new file
    await shift(eng, "stored_files", "file_metadata_expires_at", -timedelta(seconds=1))
    assert (await eng.maintenance().run_cleanup()).counts["metadata_purged"] == 1
    assert (
        await raw(eng, "SELECT 1 FROM {stored_files}") == []
        and await raw(eng, "SELECT 1 FROM {idempotency_records}") == []
    )
    again = await ops.upload_file("a", chunked(make_docx([TEXT])), idempotency_key="u")
    assert again.created and again.value.file_id != f.file_id


async def test_results_expire_seven_days_after_termination_metadata_thirty(eng):
    ops = await ready_scope(eng)
    jid = (await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k")).job.job_id
    await run_all(eng)
    j = await ops.get_job("a", jid)
    assert j.result_expires_at - j.terminated_at == 7 * DAY and j.metadata_expires_at - j.terminated_at == 30 * DAY
    await shift(eng, "jobs", "result_expires_at", -timedelta(seconds=1))
    with pytest.raises(JobDataExpiredError):  # denied before any purge ran
        await ops.get_job_result("a", jid)
    s = await eng.maintenance().run_cleanup()
    assert s.counts["expired"] >= 1 and (await ops.get_job("a", jid)).data_state == "expired"
    # the key still maps to the same job during the 30 days: no new execution
    same = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k")
    assert same.replayed and same.job.job_id == jid
    # after purging the metadata there is no historical deduplication: the key may create another job
    await shift(eng, "stored_files", "delete_after", -timedelta(seconds=1))
    await shift(eng, "job_artifacts", "delete_after", -timedelta(seconds=1))
    await eng.maintenance().run_cleanup()
    await shift(eng, "jobs", "metadata_expires_at", -timedelta(seconds=1))
    assert (await eng.maintenance().run_cleanup()).counts["metadata_purged"] >= 1
    assert await raw(eng, "SELECT 1 FROM {jobs}") == [] and await raw(eng, "SELECT 1 FROM {job_artifacts}") == []
    third = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k")
    assert not third.replayed and third.job.job_id != jid


async def test_in_flight_download_completes_while_new_access_is_denied(eng):
    ops = await ready_scope(eng)
    jid = (await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k")).job.job_id
    await run_all(eng)
    stream = await ops.open_job_artifact("a", jid, "result")  # verified into the private spool BEFORE expiry
    await shift(eng, "jobs", "result_expires_at", -timedelta(seconds=1))
    await eng.maintenance().run_cleanup()
    await shift(eng, "job_artifacts", "delete_after", -timedelta(seconds=1))
    await eng.maintenance().run_cleanup()
    assert b"analysis" in b"".join([c async for c in stream])  # the started download finishes
    with pytest.raises(JobDataExpiredError):
        await ops.open_job_artifact("a", jid, "result")


async def test_admission_requires_the_input_to_outlive_the_job_and_expiry_fails_waiting_jobs(eng):
    ops = await ready_scope(eng)
    f = (await ops.upload_file("a", chunked(make_docx([TEXT])), idempotency_key="u")).value
    await shift(eng, "stored_files", "expires_at", timedelta(hours=1))  # less than the 24 h job deadline
    with pytest.raises(FileExpiringError):
        await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="j")
    await shift(eng, "stored_files", "expires_at", timedelta(days=6))
    jid = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="j")).job.job_id
    await shift(eng, "stored_files", "expires_at", -timedelta(seconds=1))  # expires while the job waits
    assert await run_all(eng) == 0  # never executed with an input whose authorized availability ended
    await eng.maintenance().run_cleanup()
    j = await ops.get_job("a", jid)
    assert j.state == "failed" and j.failure_code == "input_unavailable"


async def test_delete_job_data_is_terminal_only_and_leaves_inputs_alone(eng):
    ops = await ready_scope(eng)
    f = (await ops.upload_file("a", chunked(make_docx([TEXT])), idempotency_key="u")).value
    jid = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="j")).job.job_id
    with pytest.raises(JobNotTerminalError):
        await ops.delete_job_data("a", jid)
    await run_all(eng)
    out = await ops.delete_job_data("a", jid)
    assert out.data_state == "deleted"
    assert (await ops.delete_job_data("a", jid)).data_state == "deleted"
    with pytest.raises(JobDataExpiredError):
        await ops.get_job_result("a", jid)
    assert (await ops.get_file("a", f.file_id)).state == "stored"  # shared inputs are not removed


async def test_tombstone_survives_the_metadata_purge_and_sealed_count_is_preserved(eng):
    ops = await ready_scope(eng)
    with pytest.raises(UploadAbortedError):

        async def boom():
            yield b"abc"
            raise ConnectionError

        await ops.upload_file("a", boom(), idempotency_key="f")
    # the abandoned reservation's metadata expires BEFORE its physical release was confirmed
    await shift(eng, "stored_files", "file_metadata_expires_at", -timedelta(seconds=1))
    await shift(eng, "stored_files", "delete_after", timedelta(hours=1))
    s = await eng.maintenance().run_cleanup()
    assert s.counts["metadata_purged"] == 1 and await raw(eng, "SELECT 1 FROM {stored_files}") == []
    tomb = await raw(eng, "SELECT * FROM {storage_tombstones}")
    assert len(tomb) == 1 and tomb[0].write_state == "unknown" and tomb[0].charged_bytes > 0  # intent kept, no content
    assert "sha256" not in tomb[0]._fields and "external_ref" not in tomb[0]._fields
    assert (await raw(eng, "SELECT uncertain_bytes, pending_seals FROM {scope_usage}"))[0].uncertain_bytes > 0
    await shift(eng, "storage_tombstones", "delete_after", -timedelta(seconds=1))
    s2 = await eng.maintenance().run_cleanup()
    assert s2.counts["purged_physically"] == 1 and await raw(eng, "SELECT 1 FROM {storage_tombstones}") == []
    u = (await raw(eng, "SELECT uncertain_bytes, pending_seals, sealed_keys FROM {scope_usage}"))[0]
    assert (u.uncertain_bytes, u.pending_seals, u.sealed_keys) == (0, 0, 1)  # the permanent seal remains counted
    await eng.maintenance().run_cleanup()  # a later recount does not forget it either
    assert (await raw(eng, "SELECT sealed_keys FROM {scope_usage}"))[0].sealed_keys == 1
    assert (await raw(eng, "SELECT sealed_keys FROM {engine_usage}"))[0].sealed_keys == 1


async def test_cleanup_is_bounded_repeatable_and_survives_a_dead_cleaner(eng):
    ops = await ready_scope(eng)
    for i in range(5):
        await ops.upload_file("a", chunked(make_docx([TEXT + str(i)])), idempotency_key=f"u{i}")
    await shift(eng, "stored_files", "expires_at", -timedelta(seconds=1))
    s1 = await eng.maintenance().run_cleanup(batch_size=2)
    assert s1.counts["expired"] == 2  # bounded by the batch
    s2 = await eng.maintenance().run_cleanup(batch_size=10)
    assert s2.counts["expired"] == 3
    await shift(eng, "stored_files", "delete_after", -timedelta(seconds=1))
    # a cleaner claimed one row and died: its claim is still valid, so nobody else touches it...
    await raw(
        eng,
        "UPDATE {stored_files} SET purge_token = 'dead', purge_claim_expires_at = :t WHERE file_id = (SELECT min(file_id) FROM {stored_files})",
        t=at(eng, delta=timedelta(minutes=5)),
    )
    s3 = await eng.maintenance().run_cleanup()
    assert s3.counts["purged_physically"] == 4
    # ...until its claim expires; then a later cleaner finishes the job
    await shift(eng, "stored_files", "purge_claim_expires_at", -timedelta(seconds=1), "purge_token = 'dead'")
    assert (await eng.maintenance().run_cleanup()).counts["purged_physically"] == 1
    assert (await raw(eng, "SELECT uncertain_bytes FROM {scope_usage}"))[0].uncertain_bytes == 0


# ---- admin_events (Q-04) ---------------------------------------------------------------------------------
async def test_admin_events_purge_is_explicit_previewed_and_touches_nothing_else(eng):
    ops = await ready_scope(eng)
    old = at(eng, delta=-timedelta(days=100))
    for i in range(5):
        await raw(
            eng,
            "INSERT INTO {admin_events} (occurred_at, action, target_type, target_id, detail) VALUES (:t, 'scope.created', 'scope', :i, '{{}}')",
            t=old,
            i=f"old{i}",
        )
    await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k")
    before = {
        t: len(await raw(eng, f"SELECT 1 FROM {{{t}}}"))
        for t in ("scopes", "jobs", "stored_files", "idempotency_records")
    }
    total = len(await raw(eng, "SELECT 1 FROM {admin_events}"))
    m = eng.maintenance()
    cutoff = datetime.now(UTC) - timedelta(days=90)
    pre = await m.purge_admin_events(older_than=cutoff)  # preview by default
    assert pre.dry_run and pre.matched >= 5 and pre.deleted == 0
    assert len(await raw(eng, "SELECT 1 FROM {admin_events}")) == total
    with pytest.raises(ValidationFailedError):  # a cutoff newer than the retention is refused
        await m.purge_admin_events(older_than=datetime.now(UTC) - timedelta(days=1), dry_run=False)
    res = await m.purge_admin_events(older_than=cutoff, batch_size=2, dry_run=False)
    assert res.deleted == pre.matched and not res.dry_run
    assert len(await raw(eng, "SELECT 1 FROM {admin_events} WHERE target_id LIKE 'old%'")) == 0
    assert len(await raw(eng, "SELECT 1 FROM {admin_events}")) == total - 5  # recent events untouched
    assert before == {t: len(await raw(eng, f"SELECT 1 FROM {{{t}}}")) for t in before}  # no other table affected


async def test_the_migration_does_not_purge_events(db):
    backend, url = db
    cfg = EngineConfig(database_url=url)
    await upgrade(cfg, target="0002")
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    ev = "cawbase_content_engine.admin_events" if backend == "postgresql" else "cawbase_content_engine_admin_events"
    eng = create_async_engine(url)
    async with eng.begin() as c:
        await c.execute(
            sa.text(
                f"INSERT INTO {ev} (occurred_at, action, target_type, target_id, detail) VALUES (:t, 'scope.created', 'scope', 'x', '{{}}')"
            ),
            {"t": datetime(2020, 1, 1, tzinfo=UTC) if backend == "postgresql" else datetime(2020, 1, 1)},
        )
    assert (await upgrade(cfg)).applied == ("0003",)
    async with eng.connect() as c:
        assert (await c.execute(sa.text(f"SELECT count(*) FROM {ev}"))).scalar_one() == 1
    await eng.dispose()


_ = (make_docx, valid)
