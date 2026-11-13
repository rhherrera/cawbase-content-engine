"""A05 jobs: text analysis end to end, idempotency, state machine, fence. Real engine, both backends."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from cawbase_content_engine import IdempotencyConflictError
from cawbase_content_engine.errors import JobNotSucceededError, PermissionDeniedError

from .a05_helpers import FULL, SP, TEXT, StubAdapter, fast_limits, make_engine, raw, ready_scope, run_all


@pytest.fixture
async def eng(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path)
    yield e
    await e.aclose()


async def test_text_analysis_end_to_end(eng):
    ops = await ready_scope(eng)
    acc = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k1")
    assert acc.job.state == "queued" and not acc.replayed and acc.job.operation == "text_analysis"
    assert await run_all(eng) == 1
    job = await ops.get_job("a", acc.job.job_id)
    assert job.state == "succeeded" and job.data_state == "available" and job.attempt_count == 1
    result = await ops.get_job_result("a", acc.job.job_id)
    analysis = result["analysis"]
    assert result["operation"] == "text_analysis" and analysis["status"] == "completed"
    assert [f["type_id"] for f in analysis["detection"]["findings"]] == ["personal_data", "personal_data", "password"]
    # counters: the job is no longer active; the artifact bytes are accounted as stored
    usage = (await raw(eng, "SELECT active_jobs, running_jobs, stored_bytes, reserved_bytes FROM {scope_usage}"))[0]
    assert usage.active_jobs == 0 and usage.running_jobs == 0 and usage.reserved_bytes == 0 and usage.stored_bytes > 0


async def test_replay_does_not_reselect_the_profile_or_run_again(eng):
    ops = await ready_scope(eng)
    a = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="same")
    # the default profile is disabled afterwards: a replay still returns the original job (no re-selection)
    await ops.create_profile("a", "other", "Other", initial_catalog="empty")
    b = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="same")
    assert b.replayed and b.job.job_id == a.job.job_id
    with pytest.raises(IdempotencyConflictError):
        await ops.submit_text_analysis("a", text=TEXT + "!", idempotency_key="same")
    with pytest.raises(IdempotencyConflictError):  # the auxiliary context is part of the fingerprint
        await ops.submit_text_analysis("a", text=TEXT, idempotency_key="same", context="ctx")
    assert len(await raw(eng, "SELECT job_id FROM {jobs}")) == 1


async def test_concurrent_first_requests_create_one_job(eng):
    ops = await ready_scope(eng)
    res = await asyncio.gather(
        *[ops.submit_text_analysis("a", text=TEXT, idempotency_key="race") for _ in range(4)], return_exceptions=True
    )
    ok = [r for r in res if not isinstance(r, Exception)]
    assert ok and len({r.job.job_id for r in ok}) == 1
    assert len(await raw(eng, "SELECT job_id FROM {jobs}")) == 1


async def test_pinned_version_survives_profile_deactivation(eng):
    ops = await ready_scope(eng)
    acc = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="pin")
    d = await ops.get_default_profile("a")
    await ops.clear_default_profile("a", expected_revision=d.revision)
    await ops.disable_profile("a", "contratos")
    await run_all(eng)
    assert (await ops.get_job("a", acc.job.job_id)).state == "succeeded"
    assert (await ops.get_job("a", acc.job.job_id)).profile["version_number"] == 1


async def test_cancel_queued_and_terminal(eng):
    ops = await ready_scope(eng)
    acc = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="c1")
    c = await ops.cancel_job("a", acc.job.job_id)
    assert c.state == "cancelled" and c.cancel_outcome == "cancelled" and c.cancel_reason == "user_requested"
    assert (await ops.cancel_job("a", acc.job.job_id)).cancel_outcome == "already_terminal"
    assert await run_all(eng) == 0
    u = (await raw(eng, "SELECT active_jobs FROM {scope_usage}"))[0]
    assert u.active_jobs == 0


async def test_result_of_unfinished_job_is_a_typed_conflict(eng):
    ops = await ready_scope(eng)
    acc = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="u1")
    with pytest.raises(JobNotSucceededError):
        await ops.get_job_result("a", acc.job.job_id)


async def test_permissions_are_separate(eng):
    await ready_scope(eng)
    only_exec = eng.scoped(eng.trusted_scope_context("a", [SP.ANALYSIS_EXECUTE]))
    acc = await only_exec.submit_text_analysis("a", text=TEXT, idempotency_key="p1")
    with pytest.raises(PermissionDeniedError):
        await only_exec.get_job("a", acc.job.job_id)
    with pytest.raises(PermissionDeniedError):
        await only_exec.get_job_result("a", acc.job.job_id)
    with pytest.raises(PermissionDeniedError):
        await only_exec.cancel_job("a", acc.job.job_id)
    assert set(SP) - set(FULL) == {SP.SCOPE_READ, SP.SCOPE_WRITE}


async def test_retry_then_success_and_exhaustion(eng, migrated, tmp_path):
    from cawbase_content_engine.errors import InferenceUnavailableError

    ops = await ready_scope(eng)
    fails = {"n": 2}

    async def flaky(req):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise InferenceUnavailableError("loading")

    eng._inference.adapter.on_infer = flaky  # type: ignore[union-attr]
    acc = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="r1")
    for _ in range(20):
        await run_all(eng)
        if (await ops.get_job("a", acc.job.job_id)).state == "succeeded":
            break
        await asyncio.sleep(0.1)
    job = await ops.get_job("a", acc.job.job_id)
    assert job.state == "succeeded" and job.attempt_count == 3
    assert [r.outcome for r in await raw(eng, "SELECT outcome FROM {job_attempts} ORDER BY attempt_no")] == [
        "retryable_error",
        "retryable_error",
        "succeeded",
    ]

    async def always(req):
        raise InferenceUnavailableError("stopped")

    eng._inference.adapter.on_infer = always  # type: ignore[union-attr]
    acc2 = await ops.submit_text_analysis("a", text=TEXT + " 2", idempotency_key="r2")
    for _ in range(40):
        await run_all(eng)
        if (await ops.get_job("a", acc2.job.job_id)).state == "failed":
            break
        await asyncio.sleep(0.1)
    j2 = await ops.get_job("a", acc2.job.job_id)
    assert (
        j2.state == "failed"
        and j2.failure_code == "retries_exhausted"
        and j2.last_error_code == "inference_unavailable"
    )
    assert j2.attempt_count == j2.max_attempts


async def test_deterministic_failures_are_not_retried(eng):
    from cawbase_content_engine.errors import InferenceOutputInvalidError

    ops = await ready_scope(eng)

    async def bad(req):
        raise InferenceOutputInvalidError([{"code": "invalid_type"}])

    eng._inference.adapter.on_infer = bad  # type: ignore[union-attr]
    acc = await ops.submit_text_analysis("a", text=TEXT, idempotency_key="d1")
    await run_all(eng)
    j = await ops.get_job("a", acc.job.job_id)
    assert j.state == "failed" and j.failure_code == "inference_output_invalid" and j.attempt_count == 1


_ = (json, UTC, datetime, timedelta, StubAdapter, fast_limits)
