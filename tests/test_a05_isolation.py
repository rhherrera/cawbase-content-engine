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

from .a05_helpers import (
    FULL,
    TEXT,
    StubAdapter,
    chunked,
    fast_limits,
    make_docx,
    make_pdf,
    ready_scope,
    run_all,
)

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


# ---- memory and process limits applied INSIDE the reference container (AC-A05-20) ----------------------------
MiB = 1024 * 1024


def _limits(**kw):
    return fast_limits(extraction_memory_bytes=192 * MiB, extraction_max_pids=40, **kw)


async def test_the_container_reports_the_effective_memory_limit_it_was_given(image):
    import json

    runner = LinuxContainerRunner(image)
    lim = _limits()
    out = json.loads(await runner._exec(b'{"canary": true, "expected_pids": 40}\n', {"canary": True}, lim, 90.0))  # noqa: SLF001
    assert out["all_blocked"] is True, out
    assert out["memory_limit_bytes"] == 192 * MiB, out  # the cgroup limit seen from inside, not the configured number


async def test_allocating_beyond_the_limit_is_killed_and_below_it_is_not(image):
    from cawbase_content_engine.documents.runner import ChildExit, _header

    runner = LinuxContainerRunner(image)
    lim = _limits()
    ok = await runner._exec(_header({"canary_memory": 96 * MiB}), {"canary": True}, lim, 120.0)  # noqa: SLF001
    assert b"allocated" in ok  # control: the probe itself works within the limit
    with pytest.raises(ChildExit) as exc:
        await runner._exec(_header({"canary_memory": 192 * MiB + 64 * MiB}), {"canary": True}, lim, 120.0)  # noqa: SLF001
    assert exc.value.code in (137, 3)  # OOM-killed (or MemoryError): the limit is applied


async def test_verify_uses_the_limits_it_is_given_and_detects_a_looser_container(image):
    lim = _limits()
    assert await LinuxContainerRunner(image).verify(lim) is True

    class Loose(LinuxContainerRunner):  # same container but with 4x the memory: must NOT be approved
        def _args(self, name, limits, *, interactive=True):
            args = super()._args(name, limits, interactive=interactive)
            mem = f"{int(limits.extraction_memory_bytes) // MiB * 4}m"
            i = args.index("--memory")
            args[i + 1] = mem
            args[args.index("--memory-swap") + 1] = mem
            return args

    class Unlimited(LinuxContainerRunner):  # no memory control at all
        def _args(self, name, limits, *, interactive=True):
            args = super()._args(name, limits, interactive=interactive)
            for flag in ("--memory", "--memory-swap"):
                i = args.index(flag)
                del args[i : i + 2]
            return args

    assert await Loose(image).verify(lim) is False
    assert await Unlimited(image).verify(lim) is False


async def test_jobs_with_non_default_limits_are_verified_with_those_limits_and_fail_closed(migrated, tmp_path, image):
    _, url = migrated
    docs = DocumentSetup(runner=LinuxContainerRunner(image), untrusted_input=True)
    eng = ContentEngine(
        EngineConfig(database_url=url),
        inference=InferenceSetup(StubAdapter(), timeout_s=5),
        storage=StorageSetup(managed=FilesystemStorage(tmp_path / "o")),
        documents=docs,
        jobs=JobsSetup(limits=_limits(extraction_timeout_s=120.0)),
    )
    await eng.start()
    try:
        ops = await ready_scope(eng)
        f = (await ops.upload_file("a", chunked(make_docx([TEXT])), idempotency_key="u")).value
        jid = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="j")).job.job_id
        await run_all(eng)
        assert (await ops.get_job("a", jid)).state == "succeeded"
        assert eng._isolation_approved is not None and 192 * MiB in eng._isolation_approved  # noqa: SLF001
    finally:
        await eng.aclose()

    broken = DocumentSetup(runner=LinuxContainerRunner(image, docker="/nonexistent/docker"), untrusted_input=True)
    eng2 = ContentEngine(
        EngineConfig(database_url=url),
        inference=InferenceSetup(StubAdapter(), timeout_s=5),
        storage=StorageSetup(managed=FilesystemStorage(tmp_path / "o2")),
        documents=broken,
        jobs=JobsSetup(limits=_limits()),
    )
    await eng2.start()
    try:
        ops = eng2.scoped(eng2.trusted_scope_context("a", FULL, actor="test-host"))
        f = (await ops.upload_file("a", chunked(make_docx([TEXT + "2"])), idempotency_key="u2")).value
        jid = (await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="j2")).job.job_id
        await run_all(eng2)
        j = await ops.get_job("a", jid)
        assert j.state == "failed" and j.failure_code == "configuration_unavailable"  # no fallback
    finally:
        await eng2.aclose()
