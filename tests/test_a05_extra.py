"""A05: key revocation, no content in logs, static boundaries, mixed concurrency, deadlines, validation identities, matrix edges."""

from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import zipfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from cawbase_content_engine.errors import DatabaseUnavailableError, EngineError
from cawbase_content_engine.http import create_router

from .a05_helpers import (
    FULL,
    TEXT,
    at,
    chunked,
    fast_limits,
    make_docx,
    make_engine,
    make_pdf,
    raw,
    ready_scope,
    run_all,
)
from .helpers import admin_ctx, issue
from .test_a05_documents import doc_job, rejected, zip_with

SRC = Path(__file__).resolve().parent.parent / "src" / "cawbase_content_engine"
MARK = "SYNTHETIC-CONTENT-MARKER-7f3a"


@pytest.fixture
async def eng(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path)
    yield e
    await e.aclose()


async def test_revoking_a_key_stops_access_but_not_accepted_jobs(eng):
    ops = await ready_scope(eng)
    client, issued = await issue(eng, "svc", scopes={"a": ["analysis:execute", "jobs:read"]})
    app = FastAPI()
    app.include_router(create_router(eng))
    auth = {"Authorization": f"Bearer {issued.secret.get_secret_value()}", "Idempotency-Key": "k"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        sub = await c.post("/v1/scopes/a/text-analysis-jobs", json={"text": TEXT}, headers=auth)
        jid = sub.json()["job"]["job_id"]
        await eng.admin(await admin_ctx(eng)).revoke_api_key(issued.info.key_id)
        assert (await c.get(f"/v1/scopes/a/jobs/{jid}", headers=auth)).status_code == 401  # later access is refused
    await run_all(eng)
    assert (await ops.get_job("a", jid)).state == "succeeded"  # the accepted job was not cancelled


async def test_logs_errors_and_events_never_carry_content(eng, caplog):
    caplog.set_level(logging.DEBUG, logger="cawbase_content_engine")
    ops = await ready_scope(eng)
    text = f"Contactar a Ana Ejemplo {MARK} ana.ejemplo@example.test"
    docx = make_docx([text], comments=[MARK + "-comment"])
    f = (await ops.upload_file("a", chunked(docx), idempotency_key="u")).value
    acc = await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="j", context=MARK + "-ctx")
    await run_all(eng)
    await ops.submit_text_analysis("a", text=text, idempotency_key="t")
    with pytest.raises(EngineError) as ei:
        await ops.upload_file("a", chunked(b"abc"), idempotency_key="u", expected_sha256="0" * 64)
    assert MARK not in str(ei.value) and MARK not in repr(ei.value)
    logs = "\n".join(r.getMessage() + repr(getattr(r, "__dict__", {})) for r in caplog.records)
    assert MARK not in logs and "Ana Ejemplo" not in logs and "example.test" not in logs
    events = await raw(eng, "SELECT action, detail FROM {admin_events}")
    assert MARK not in repr([tuple(e) for e in events])
    tombstones = repr(await raw(eng, "SELECT * FROM {storage_tombstones}"))
    assert MARK not in tombstones
    job = await ops.get_job("a", acc.job.job_id)
    assert MARK not in job.model_dump_json()  # the job metadata carries identities and hashes, never text or context


def test_static_boundaries_experiments_not_imported_and_no_protection_operations():
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        assert not re.search(r"^\s*(from|import)\s+experiments", text, re.M), path
        assert "experiments/a04" not in text.replace('"""', ""), path
    from cawbase_content_engine.engine import ScopedOperations

    names = {n.lower() for n in dir(ScopedOperations)}
    assert not {n for n in names if any(w in n for w in ("protect", "redact", "restore", "pseudonym", "mapping"))}


async def test_mixed_concurrency_never_deadlocks_between_engine_operations(eng):
    ops = await ready_scope(eng)
    admin = eng.admin(await admin_ctx(eng))
    f = (await ops.upload_file("a", chunked(make_docx([TEXT])), idempotency_key="u")).value
    rnd = random.Random(7)  # noqa: S311

    async def submitter(i: int) -> None:
        for n in range(3):
            try:
                await ops.submit_text_analysis("a", text=f"{TEXT} {i}-{n}", idempotency_key=f"s{i}-{n}")
            except (EngineError, TimeoutError):
                pass

    async def toggler() -> None:
        for _ in range(4):
            await admin.set_scope_status("a", "disabled")
            await asyncio.sleep(0.02)
            await admin.set_scope_status("a", "active")

    async def worker(i: int) -> None:
        for _ in range(6):
            await run_all(eng, f"w{i}", rounds=2)
            await asyncio.sleep(rnd.random() * 0.02)

    async def other() -> None:
        for n in range(4):
            try:
                await ops.cancel_job("a", "job_" + "0" * 32)
            except EngineError:
                pass
            await ops.list_jobs("a")
            await ops.delete_file("a", f.file_id) if n == 3 else None

    res = await asyncio.wait_for(
        asyncio.gather(
            *[submitter(i) for i in range(3)],
            toggler(),
            *[worker(i) for i in range(2)],
            other(),
            return_exceptions=True,
        ),
        90,
    )
    unexpected = [r for r in res if isinstance(r, Exception) and not isinstance(r, DatabaseUnavailableError)]
    assert not unexpected, unexpected
    await run_all(eng)
    u = (await raw(eng, "SELECT active_jobs, running_jobs FROM {scope_usage}"))[0]
    left = await raw(eng, "SELECT 1 FROM {jobs} WHERE state IN ('queued','running','retry_wait')")
    assert u.active_jobs == len(left) and u.running_jobs == len(
        await raw(eng, "SELECT 1 FROM {jobs} WHERE state = 'running'")
    )


async def test_publication_checks_every_deadline_not_only_the_heartbeat(eng):
    ops = await ready_scope(eng)
    j1 = (await ops.submit_text_analysis("a", text=TEXT, idempotency_key="k1")).job.job_id
    j2 = (await ops.submit_text_analysis("a", text=TEXT + "2", idempotency_key="k2")).job.job_id
    w = eng.job_worker("w")
    c1, c2 = await w.claim(2)
    a1 = await w.write_artifact(c1, "result", b"{}", 1000)
    a2 = await w.write_artifact(c2, "result", b"{}", 1000)
    far = timedelta(hours=1)
    # the attempt deadline passed while the lease value still looks valid: no heartbeat was needed to notice
    await raw(
        eng,
        "UPDATE {jobs} SET attempt_deadline_at = :t, lease_expires_at = :l WHERE job_id = :j",
        t=at(eng, delta=-timedelta(seconds=1)),
        l=at(eng, delta=far),
        j=c1.job_id,
    )
    assert await w.publish(c1, {"result": a1}) == "lease_lost"
    # the job deadline passed
    await raw(
        eng,
        "UPDATE {jobs} SET deadline_at = :t, lease_expires_at = :l WHERE job_id = :j",
        t=at(eng, delta=-timedelta(seconds=1)),
        l=at(eng, delta=far),
        j=c2.job_id,
    )
    assert await w.publish(c2, {"result": a2}) == "lease_lost"
    assert {(await ops.get_job("a", j)).data_state for j in (j1, j2)} == {"none"}


async def test_unlinked_text_objects_of_a_never_accepted_request_are_abandoned(eng):
    ops = await ready_scope(eng)
    # a request materialized its input but never reached the acceptance transaction (e.g. the process died)
    from cawbase_content_engine.jobs import files as fl

    fid = await fl.store_text_object(fl.Ctx(eng), "a", TEXT.encode())
    assert (await raw(eng, "SELECT state FROM {stored_files} WHERE file_id = :f", f=fid))[0].state == "stored"
    await raw(eng, "UPDATE {stored_files} SET link_deadline_at = :t", t=at(eng, delta=-timedelta(seconds=1)))
    s = await eng.maintenance().run_cleanup()
    assert s.counts["abandoned"] == 1
    r = (await raw(eng, "SELECT state FROM {stored_files} WHERE file_id = :f", f=fid))[0]
    assert r.state == "abandoned"
    assert (await raw(eng, "SELECT uncertain_bytes FROM {scope_usage}"))[0].uncertain_bytes == len(TEXT.encode())
    _ = ops


async def test_a_second_validator_configuration_gets_its_own_authoritative_result(migrated, tmp_path):
    _, url = migrated
    data = make_pdf([[TEXT], [TEXT]])
    e1 = await make_engine(url, tmp_path)
    ops = await ready_scope(e1)
    f, job = await doc_job(e1, ops, data, "v1")
    assert job.state == "succeeded"
    # the same file under a stricter configuration (1 page max): a different identity, rejected, coexisting with the first
    e2 = await make_engine(url, tmp_path, limits=replace(fast_limits(), max_pdf_pages=1), name="o2")
    try:
        ops2 = e2.scoped(e2.trusted_scope_context("a", FULL))
        # the original is only reachable through its own storage root: register the same bytes in the second engine
        f2 = (await ops2.upload_file("a", chunked(data), idempotency_key="v2-up")).value
        acc = await ops2.submit_document_analysis("a", file_id=f2.file_id, idempotency_key="v2")
        await run_all(e2)
        assert (await ops2.get_job("a", acc.job.job_id)).failure_code == "file_rejected"
        rows = await raw(
            e1, "SELECT file_id, result, validation_identity FROM {file_validations} ORDER BY validated_at"
        )
        by_file = {r.file_id: r for r in rows}
        assert by_file[f.file_id].result == "passed" and by_file[f2.file_id].result == "rejected"
        assert (
            by_file[f.file_id].validation_identity != by_file[f2.file_id].validation_identity
        )  # config is part of identity
        # a rejection under one configuration does not invalidate the other file's pass
        assert (await ops.get_file("a", f.file_id)).download_validation == "passed"
        assert (await ops2.get_file("a", f2.file_id)).download_validation == "rejected"
    finally:
        await e2.aclose()
        await e1.aclose()


async def test_per_capability_states_with_one_capability_not_configured(eng, migrated, tmp_path):
    ops = await ready_scope(eng, "b", categories=False, mode="ranking")  # a profile with detection only
    f = (await ops.upload_file("b", chunked(make_docx([TEXT])), idempotency_key="u")).value
    acc = await ops.submit_document_analysis("b", file_id=f.file_id, idempotency_key="j")
    await run_all(eng)
    res = await ops.get_job_result("b", acc.job.job_id)
    pc = res["coverage"]["inference"]["per_capability"]
    assert pc["classification"] == {"status": "not_configured"} and pc["detection"]["status"] == "completed"
    assert res["coverage"]["extraction"]["textual_subset_complete"] is True  # driven by the executed capability only


async def test_matrix_edges_table_nesting_and_sections(eng):
    ops = await ready_scope(eng)
    nested = (
        b"<w:tbl><w:tr><w:tc>" * 3 + b"<w:p><w:r><w:t>x</w:t></w:r></w:p>" + b"</w:tc></w:tr></w:tbl>" * 3 + b"<w:p/>"
    )

    def deep(n, b):
        return b.replace(b"<w:body>", b"<w:body>" + nested) if n == "word/document.xml" else b

    assert await rejected(eng, ops, zip_with(make_docx([TEXT]), deep), "deep") == "unsupported_structure"
    two = lambda n, b: (
        b.replace(b"<w:body>", b"<w:body><w:p><w:pPr><w:sectPr/></w:pPr></w:p>") if n == "word/document.xml" else b
    )  # noqa: E731
    assert await rejected(eng, ops, zip_with(make_docx([TEXT]), two), "two") == "unsupported_structure"
    # depth 2 (a table inside a cell) is inside the matrix
    ok = b"<w:tbl><w:tr><w:tc><w:tbl><w:tr><w:tc><w:p><w:r><w:t>hi</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p/></w:tc></w:tr></w:tbl><w:p/>"
    f, job = await doc_job(
        eng,
        ops,
        zip_with(
            make_docx([TEXT]), lambda n, b: b.replace(b"<w:body>", b"<w:body>" + ok) if n == "word/document.xml" else b
        ),
        "d2",
    )
    assert job.state == "succeeded"


_ = (io, zipfile)
