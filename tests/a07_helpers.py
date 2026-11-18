"""Harness of the A07 tests: REAL database (PostgreSQL and SQLite), storage, isolated child, cryptography and
permissions; ONLY the inference is simulated, and with the A03 mock adapter itself (fixtures bound to the exact
unit texts), so the analyses carry the genuine simulated provenance that S10b requires."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cawbase_content_engine import ContentEngine, EngineConfig, ScopePermission
from cawbase_content_engine.documents import docx
from cawbase_content_engine.documents.common import Limits
from cawbase_content_engine.documents.runner import UnconfinedDevelopmentRunner
from cawbase_content_engine.inference import (
    AdapterDescriptor,
    AdapterState,
    InferenceSetup,
    MockInferenceAdapter,
    full_coverage,
)
from cawbase_content_engine.jobs.setup import DocumentSetup, JobsLimits, JobsSetup, Retention, StorageSetup
from cawbase_content_engine.jobs.storage import FilesystemStorage
from cawbase_content_engine.protection.types import ProtectionPolicy, ProtectionSetup

from .a03_helpers import AE, PR, PW, fixture_for
from .a05_helpers import chunked, fast_limits
from .a06_helpers import Keys
from .helpers import make_scope

SP = ScopePermission
DOCX_ALL = [SP.DOCX_PROTECT, SP.DOCX_RESTORE, SP.DOCX_PROTECTED_READ, SP.DOCX_RESTORED_READ]
FULL = [
    PR,
    PW,
    AE,
    SP.FILES_WRITE,
    SP.FILES_READ,
    SP.JOBS_READ,
    SP.JOBS_CANCEL,
    SP.RESULTS_READ,
    SP.DATA_DELETE,
    SP.TEXT_PROTECT,
    SP.TEXT_RESTORE,
    SP.CONTEXTS_READ,
    SP.CONTEXTS_DELETE,
    *DOCX_ALL,
]
PSEUDO = ProtectionPolicy("pseudonymize")
LIM = Limits.from_params({k: getattr(JobsLimits(), k) for k in Limits.__dataclass_fields__})
CAPS = ("detection",)


def unit_texts(data: bytes) -> list[str]:
    return [u["text"] for u in docx.extract(data, LIM)["units"] if not u["empty"]]


def detection(text: str, picks: list[tuple[str, str, int]]) -> dict[str, Any]:
    """A raw A03 output with the findings at the given (type_id, fragment, occurrence) of THIS text."""
    found = []
    for type_id, frag, occ in picks:
        pos = -1
        for _ in range(occ + 1):
            pos = text.index(frag, pos + 1)
        found.append({"type_id": type_id, "start": pos, "end": pos + len(frag), "text": frag})
    return {
        "contract_version": 1,
        "detection": {
            "status": "completed",
            "reason": None,
            "coverage": full_coverage(["password", "personal_data"], text),
            "findings": found,
        },
    }


class ModelLikeAdapter:
    """A deterministic adapter that declares itself a MODEL (no real inference): its analyses must be refused."""

    def __init__(self, outputs: dict[str, dict[str, Any]]) -> None:
        self.outputs = outputs
        self._d = AdapterDescriptor(
            kind="model",
            adapter_id="cce-test-model",
            adapter_version="1",
            configuration_id="m-1",
            model_id="synthetic-model",
            model_version="0",
        )

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._d

    async def probe(self) -> AdapterState:
        return AdapterState.READY

    async def infer(self, request: Any) -> dict[str, Any]:
        return self.outputs[request.text]


@dataclass
class World:
    """One scope with a published profile, a storage root and the engines composed over the same database."""

    url: str
    tmp: Path
    keys: Keys = field(default_factory=lambda: Keys("v1"))
    engines: list[ContentEngine] = field(default_factory=list)
    limits: JobsLimits = field(default_factory=fast_limits)
    retention: Retention = field(default_factory=Retention)

    async def engine(
        self, adapter: Any = None, *, protection: bool = True, limits: JobsLimits | None = None
    ) -> ContentEngine:
        kw: dict[str, Any] = dict(
            storage=StorageSetup(managed=FilesystemStorage(self.tmp / "objects")),
            jobs=JobsSetup(limits=limits or self.limits, retention=self.retention),
            documents=DocumentSetup(runner=UnconfinedDevelopmentRunner(), untrusted_input=False),
        )
        if adapter is not None:
            kw["inference"] = InferenceSetup(adapter, timeout_s=10.0)
        if protection:
            kw["protection"] = ProtectionSetup(keys=self.keys)
        eng = ContentEngine(EngineConfig(database_url=self.url), **kw)
        await eng.start()
        self.engines.append(eng)
        return eng

    async def close(self) -> None:
        for e in self.engines:
            await e.aclose()
        self.engines.clear()


def ops(eng: ContentEngine, perms: list[Any] | None = None, scope_id: str = "a") -> Any:
    return eng.scoped(eng.trusted_scope_context(scope_id, perms or FULL, actor="test-host"))


async def setup_scope(w: World, scope_id: str = "a") -> ContentEngine:
    """Scope + published default profile (types password, personal_data); returns an engine without inference."""
    from .a03_helpers import build_profile

    base = await w.engine(protection=True)
    try:
        await make_scope(base, scope_id)
    except Exception:
        pass
    try:
        await build_profile(base, scope_id, create_scope=False, mode="ranking", categories=False)
    except Exception:  # noqa: S110 - already built by an earlier call in the same test
        pass
    return base


async def mock_for(
    base: ContentEngine, texts_findings: dict[str, list[tuple[str, str, int]]], scope_id: str = "a"
) -> MockInferenceAdapter:
    o = ops(base, [PR, PW, AE], scope_id)
    fxs = []
    for i, (text, picks) in enumerate(texts_findings.items()):
        fxs.append(
            await fixture_for(
                o, scope_id, {"kind": "output", "output": detection(text, picks)}, text=text, fid=f"fx{i}", caps=CAPS
            )
        )
    return MockInferenceAdapter(fxs, acknowledge_simulated=True)


async def run(eng: ContentEngine, job_id: str) -> Any:
    out = await eng.job_worker("w-a07").run_job(job_id)
    return out


async def upload(o: Any, data: bytes, key: str, scope_id: str = "a") -> str:
    res = await o.upload_file(scope_id, chunked(data), idempotency_key=key)
    return res.item.file_id if hasattr(res, "item") else res.value.file_id


async def analyse(eng: ContentEngine, data: bytes, key: str = "f1", scope_id: str = "a") -> tuple[str, str]:
    """Upload + document_analysis (detection only) run to its outcome; returns (file_id, analysis job id)."""
    o = ops(eng, scope_id=scope_id)
    fid = await upload(o, data, key, scope_id)
    acc = await o.submit_document_analysis(scope_id, file_id=fid, idempotency_key="an-" + key, capabilities=CAPS)
    out = await run(eng, acc.job.job_id)
    assert out.state == "succeeded", out
    return fid, acc.job.job_id


async def read_all(stream: Any) -> bytes:
    return b"".join([c async for c in stream])


def random_key() -> bytes:
    return os.urandom(32)
