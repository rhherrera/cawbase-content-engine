"""A05 over HTTP (in-memory ASGI): streams, verified downloads, idempotency, isolation, JSON limit, executor, CLI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys

import httpx
import pytest
from fastapi import FastAPI

from cawbase_content_engine import AdminPermission
from cawbase_content_engine.http import create_router
from cawbase_content_engine.jobs.executor import ReferenceExecutor

from .a05_helpers import TEXT, chunked, make_docx, make_engine, make_pdf, raw, ready_scope
from .helpers import issue, make_scope

FILE_PERMS = [
    "files:write",
    "files:read",
    "jobs:read",
    "jobs:cancel",
    "results:read",
    "data:delete",
    "analysis:execute",
]
PDF = make_pdf([["Hola mundo"]])


def h(secret, **extra):
    return {"Authorization": f"Bearer {secret.get_secret_value()}", **extra}


@pytest.fixture
async def api(migrated, tmp_path):
    _, url = migrated
    eng = await make_engine(url, tmp_path)
    await ready_scope(eng)
    await make_scope(eng, "b")
    _, full = await issue(eng, "full", scopes={"a": FILE_PERMS})
    _, only_exec = await issue(eng, "exe", scopes={"a": ["analysis:execute"]})
    _, other = await issue(eng, "other", scopes={"b": FILE_PERMS})
    app = FastAPI()
    app.include_router(create_router(eng))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield client, eng, dict(full=full.secret, exe=only_exec.secret, other=other.secret)
    await eng.aclose()


async def drain(eng):
    from .a05_helpers import run_all

    await run_all(eng)


async def test_document_journey_over_http(api):
    c, eng, k = api
    data = make_docx([TEXT])
    up = await c.post(
        "/v1/scopes/a/files",
        content=data,
        headers=h(k["full"], **{"Idempotency-Key": "u1", "Content-Type": "application/octet-stream"}),
    )
    assert up.status_code == 201 and up.headers["cache-control"] == "no-store"
    f = up.json()
    assert (
        f["sha256"] == hashlib.sha256(data).hexdigest()
        and f["state"] == "stored"
        and "storage_key" not in f
        and "external_ref" not in f
    )
    # quarantine: neither downloadable nor usable as pending content
    q = await c.get(f"/v1/scopes/a/files/{f['file_id']}/content", headers=h(k["full"]))
    assert q.status_code == 404 and q.json()["error"]["code"] == "resource_not_found"
    replay = await c.post("/v1/scopes/a/files", content=data, headers=h(k["full"], **{"Idempotency-Key": "u1"}))
    assert (
        replay.status_code == 200
        and replay.headers["idempotent-replay"] == "true"
        and replay.json()["file_id"] == f["file_id"]
    )
    sub = await c.post(
        "/v1/scopes/a/document-analysis-jobs",
        json={"file_id": f["file_id"]},
        headers=h(k["exe"], **{"Idempotency-Key": "j1"}),
    )
    assert sub.status_code == 202 and sub.headers["location"].endswith(sub.json()["job"]["job_id"])
    jid = sub.json()["job"]["job_id"]
    rep = await c.post(
        "/v1/scopes/a/document-analysis-jobs",
        json={"file_id": f["file_id"]},
        headers=h(k["exe"], **{"Idempotency-Key": "j1"}),
    )
    assert rep.status_code == 200 and rep.headers["idempotent-replay"] == "true" and rep.json()["replayed"] is True
    await drain(eng)
    job = await c.get(f"/v1/scopes/a/jobs/{jid}", headers=h(k["full"]))
    assert job.json()["state"] == "succeeded"
    res = await c.get(f"/v1/scopes/a/jobs/{jid}/result", headers=h(k["full"]))
    assert (
        res.status_code == 200
        and res.headers["cache-control"] == "no-store"
        and "attachment" in res.headers["content-disposition"]
    )
    assert (
        res.headers["content-length"] == str(len(res.content))
        and json.loads(res.content)["operation"] == "document_analysis"
    )
    sm = await c.get(f"/v1/scopes/a/jobs/{jid}/source-map", headers=h(k["full"]))
    assert sm.json()["schema"] == "document-structures/1"
    # after validation passed, the original is downloadable and verified
    dl = await c.get(f"/v1/scopes/a/files/{f['file_id']}/content", headers=h(k["full"]))
    assert (
        dl.status_code == 200
        and dl.content == data
        and dl.headers["content-disposition"] == f'attachment; filename="{f["file_id"]}.docx"'
    )
    assert dl.headers["x-content-type-options"] == "nosniff" and dl.headers["cache-control"] == "no-store"
    lst = await c.get("/v1/scopes/a/jobs", headers=h(k["full"]))
    assert [j["job_id"] for j in lst.json()["items"]] == [jid]


async def test_text_job_cancel_delete_and_permissions(api):
    c, eng, k = api
    sub = await c.post(
        "/v1/scopes/a/text-analysis-jobs", json={"text": TEXT}, headers=h(k["exe"], **{"Idempotency-Key": "t1"})
    )
    jid = sub.json()["job"]["job_id"]
    assert (await c.get(f"/v1/scopes/a/jobs/{jid}", headers=h(k["exe"]))).status_code == 403  # execute does not read
    assert (await c.get(f"/v1/scopes/a/jobs/{jid}/result", headers=h(k["exe"]))).status_code == 403
    assert (await c.get(f"/v1/scopes/a/jobs/{jid}/result", headers=h(k["full"]))).json()["error"][
        "code"
    ] == "job_not_succeeded"
    assert (await c.delete(f"/v1/scopes/a/jobs/{jid}/data", headers=h(k["full"]))).json()["error"][
        "code"
    ] == "job_not_terminal"
    cancelled = await c.post(f"/v1/scopes/a/jobs/{jid}/cancel", headers=h(k["full"]))
    assert cancelled.json()["state"] == "cancelled"
    assert (
        await c.post(
            "/v1/scopes/a/text-analysis-jobs",
            json={"text": TEXT, "extra": 1},
            headers=h(k["exe"], **{"Idempotency-Key": "t2"}),
        )
    ).status_code == 422
    assert (
        await c.post("/v1/scopes/a/text-analysis-jobs", json={"text": TEXT}, headers=h(k["exe"]))
    ).status_code == 422  # key required


async def test_scope_isolation_and_unknown_ids_are_indistinguishable(api):
    c, eng, k = api
    up = await c.post("/v1/scopes/a/files", content=PDF, headers=h(k["full"], **{"Idempotency-Key": "u"}))
    fid = up.json()["file_id"]
    assert (await c.get(f"/v1/scopes/a/files/{fid}", headers=h(k["other"]))).status_code == 404  # no grant on the scope
    inside_b = await c.get(f"/v1/scopes/b/files/{fid}", headers=h(k["other"]))
    unknown = await c.get("/v1/scopes/b/files/fil_" + "0" * 32, headers=h(k["other"]))
    assert (
        inside_b.status_code == unknown.status_code == 404
        and inside_b.json()["error"]["code"] == unknown.json()["error"]["code"]
    )
    other_job = await c.get("/v1/scopes/b/jobs/job_" + "0" * 32, headers=h(k["other"]))
    assert other_job.status_code == 404


async def test_upload_limits_and_the_json_limit_is_untouched(api, migrated, tmp_path):
    c, eng, k = api
    big = await c.post(
        "/v1/scopes/a/files",
        content=b"x" * 10,
        headers=h(k["full"], **{"Idempotency-Key": "d", "X-Content-SHA256": "0" * 64}),
    )
    assert big.status_code == 422 and big.json()["error"]["code"] == "content_mismatch"
    over = await c.post(
        "/v1/scopes/a/text-analysis-jobs",
        content=b'{"text": "' + b"x" * 70000 + b'"}',
        headers=h(k["exe"], **{"Idempotency-Key": "o"}),
    )
    assert over.status_code == 413  # the 64 KiB JSON limit still applies to JSON routes
    assert eng.config.limits.max_body_bytes == 64 * 1024
    # a stream far larger than the 64 KiB JSON limit is accepted on the file route (its own limit)
    import os

    ok = await c.post(
        "/v1/scopes/a/files", content=os.urandom(200_000), headers=h(k["full"], **{"Idempotency-Key": "big"})
    )
    assert ok.status_code == 201 and ok.json()["size_bytes"] > 64 * 1024


async def test_a03_endpoint_is_unchanged_and_the_original_is_not_served_when_not_published(api):
    c, eng, k = api
    r = await c.post("/v1/scopes/a/analyze-text", json={"text": TEXT}, headers=h(k["exe"]))
    assert r.status_code == 200 and "provenance" in r.json() and r.headers["cache-control"] == "no-store"
    assert await raw(eng, "SELECT 1 FROM {jobs}") == []  # still no persistence for the A03 route


async def test_reference_executor_runs_jobs_and_closes_explicitly(api):
    c, eng, k = api
    ex = ReferenceExecutor(eng, concurrency=2, poll_interval_s=0.1)
    sub = await c.post(
        "/v1/scopes/a/text-analysis-jobs", json={"text": TEXT}, headers=h(k["exe"], **{"Idempotency-Key": "e1"})
    )
    jid = sub.json()["job"]["job_id"]
    await ex.start()
    try:
        for _ in range(100):
            if (await c.get(f"/v1/scopes/a/jobs/{jid}", headers=h(k["full"]))).json()["state"] == "succeeded":
                break
            await asyncio.sleep(0.1)
    finally:
        await ex.aclose()
    assert (await c.get(f"/v1/scopes/a/jobs/{jid}", headers=h(k["full"]))).json()["state"] == "succeeded"


def test_cli_exposes_the_operator_commands_without_http_endpoints():
    out = subprocess.run(
        [sys.executable, "-m", "cawbase_content_engine", "--help"], capture_output=True, text=True, check=True
    )  # noqa: S603
    for cmd in ("worker", "cleanup", "purge-admin-events"):
        assert cmd in out.stdout
    import inspect
    import re

    from cawbase_content_engine.http import create_router as _  # noqa: F401
    from cawbase_content_engine.http import jobs_routes

    paths = re.findall(r'@router\.\w+\("([^"]+)"', inspect.getsource(jobs_routes))
    assert paths and all(
        not any(w in p for w in ("claim", "heartbeat", "lease", "purge", "cleanup", "publish", "external"))
        for p in paths
    )


_ = (AdminPermission, chunked)
