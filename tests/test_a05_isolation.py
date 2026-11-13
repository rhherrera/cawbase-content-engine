"""A05 isolation profiles: no silent fallback, canary, and the Linux reference container (Docker required)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from cawbase_content_engine import ContentEngine, EngineConfig
from cawbase_content_engine.documents.runner import LinuxContainerRunner, UnconfinedDevelopmentRunner
from cawbase_content_engine.errors import InvalidConfigurationError
from cawbase_content_engine.inference import InferenceSetup
from cawbase_content_engine.jobs.setup import DocumentSetup, JobsSetup, StorageSetup
from cawbase_content_engine.jobs.storage import FilesystemStorage

from .a05_helpers import TEXT, StubAdapter, chunked, fast_limits, make_docx, make_engine, make_pdf, ready_scope, run_all

IMAGE = "cce-a05-docs:test3"


def test_unconfined_runner_is_refused_for_untrusted_input_by_default():
    with pytest.raises(InvalidConfigurationError):
        DocumentSetup(runner=UnconfinedDevelopmentRunner())  # untrusted_input defaults to True: no silent fallback
    DocumentSetup(runner=UnconfinedDevelopmentRunner(), untrusted_input=False)  # explicit, development only


class Unverifiable:
    profile = "host_provided"

    async def verify(self) -> bool:
        return False

    async def run(self, *a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("an unverified runner must not process documents")


async def test_unverifiable_isolation_fails_explicitly_and_processes_nothing(migrated, tmp_path):
    _, url = migrated
    eng = ContentEngine(
        EngineConfig(database_url=url),
        inference=InferenceSetup(StubAdapter(), timeout_s=5),
        storage=StorageSetup(managed=FilesystemStorage(tmp_path / "o")),
        documents=DocumentSetup(runner=Unverifiable(), untrusted_input=True),
        jobs=JobsSetup(limits=fast_limits()),
    )
    await eng.start()
    try:
        ops = await ready_scope(eng)
        f = (await ops.upload_file("a", chunked(make_pdf([["Hola"]])), idempotency_key="u")).value
        jid = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="j")).job.job_id
        await run_all(eng)
        j = await ops.get_job("a", jid)
        assert j.state == "failed" and j.failure_code == "configuration_unavailable"
    finally:
        await eng.aclose()


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=30).returncode == 0  # noqa: S603,S607


@pytest.fixture(scope="module")
def image():
    if not _docker_ok():
        pytest.skip("Docker is not available: the Linux reference container profile was NOT exercised")
    have = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False)  # noqa: S603,S607
    if have.returncode != 0:
        dockerfile = "FROM python:3.13-bookworm\nRUN pip install --no-cache-dir 'sqlalchemy[asyncio]>=2.0.36,<2.2' 'pydantic>=2.9,<3' 'lxml>=6.1,<7' 'pikepdf>=10.18,<11'\n"
        build = subprocess.run(
            ["docker", "build", "-t", IMAGE, "-"], input=dockerfile.encode(), capture_output=True, check=False
        )  # noqa: S603,S607
        if build.returncode != 0:
            pytest.skip("could not build the test image (no network?): " + build.stderr.decode()[-200:])
    return IMAGE


async def test_container_canary_is_blocked_on_every_probe(image):
    runner = LinuxContainerRunner(image)
    import json

    from cawbase_content_engine.jobs.setup import JobsLimits

    raw = await runner._exec(b'{"canary": true}\n', {"canary": True}, JobsLimits(), 90.0)  # noqa: SLF001
    out = json.loads(raw)
    assert out["all_blocked"] is True, out  # no network, no writes outside tmpfs, clean environment, bounded processes
    assert await runner.verify() is True


async def test_documents_are_processed_inside_the_container_end_to_end(migrated, tmp_path, image):
    _, url = migrated
    eng = ContentEngine(
        EngineConfig(database_url=url),
        inference=InferenceSetup(StubAdapter(), timeout_s=5),
        storage=StorageSetup(managed=FilesystemStorage(tmp_path / "o")),
        documents=DocumentSetup(runner=LinuxContainerRunner(image), untrusted_input=True),
        jobs=JobsSetup(limits=fast_limits(extraction_timeout_s=120.0)),
    )
    await eng.start()
    try:
        ops = await ready_scope(eng)
        for name, data in (("docx", make_docx([TEXT])), ("pdf", make_pdf([[TEXT]]))):
            f = (await ops.upload_file("a", chunked(data), idempotency_key="u" + name)).value
            jid = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="j" + name)).job.job_id
            await run_all(eng)
            j = await ops.get_job("a", jid)
            assert j.state == "succeeded", (name, j.failure_code, j.last_error_code)
            res = await ops.get_job_result("a", jid)
            assert res["provenance"]["isolation_profile"] == "linux_container_reference"
    finally:
        await eng.aclose()


_ = make_engine
