"""Helpers for the A03 tests (text analysis with the deterministic mock)."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import sqlalchemy as sa

from cawbase_content_engine import ContentEngine, EngineConfig, ScopePermission
from cawbase_content_engine.inference import (
    AdapterDescriptor,
    AdapterState,
    InferenceRequest,
    InferenceSetup,
    MockFixture,
    MockInferenceAdapter,
    full_coverage,
    locate,
    request_for,
)

from .helpers import make_scope

PR, PW, AE = ScopePermission.PROFILES_READ, ScopePermission.PROFILES_WRITE, ScopePermission.ANALYSIS_EXECUTE
CAPS = ("classification", "detection")
TEXT = "Contactar a Ana Ejemplo (ana.ejemplo@example.test). Mi contraseña es EjemploNoReal-123."
CATS = {"finance": 1, "legal": 2, "hr": 3}  # category -> priority (lower number = higher priority)
TYPES = ("password", "personal_data")
DEF_MARKER = "SYNTHETIC-DEFINITION-MARKER"


async def build_profile(
    engine: ContentEngine,
    scope_id: str = "a",
    profile_id: str = "contratos",
    *,
    mode: str = "resolution",
    threshold: float = 0.5,
    tolerance: float = 0.05,
    categories: bool = True,
    types: bool = True,
    default: bool = True,
    create_scope: bool = True,
):
    """A published profile (version 1) in ``scope_id``; returns a full-permission ops object."""
    if create_scope:
        try:
            await make_scope(engine, scope_id)
        except Exception:
            pass
    ops = engine.scoped(engine.trusted_scope_context(scope_id, [PR, PW, AE], actor="test-host"))
    await ops.create_profile(scope_id, profile_id, profile_id.title(), initial_catalog="empty")
    if categories:
        for cid, prio in CATS.items():
            await ops.create_category(scope_id, profile_id, cid, cid.title(), f"{DEF_MARKER} {cid}.", priority=prio)
    if types:
        for tid in TYPES:
            await ops.create_sensitive_type(scope_id, profile_id, tid, tid.title(), f"{DEF_MARKER} {tid}.")
    rk = await ops.get_ranking_config(scope_id, profile_id)
    await ops.update_ranking_config(
        scope_id,
        profile_id,
        expected_revision=rk.revision,
        mode=mode,
        eligibility_threshold=threshold,
        tie_tolerance=tolerance,
    )
    p = await ops.get_profile(scope_id, profile_id)
    await ops.publish_profile(scope_id, profile_id, expected_draft_revision=p.draft_revision)
    if default:
        d = await ops.get_default_profile(scope_id)
        await ops.set_default_profile(scope_id, profile_id, expected_revision=d.revision)
    return ops


def ok_output(
    text: str = TEXT,
    *,
    scores: Mapping[str, float] | None = None,
    classification: bool = True,
    detection: bool = True,
    findings: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """A valid raw output for ``text`` and the standard profile (all categories/types, full coverage)."""
    scores = scores or {"finance": 0.82, "hr": 0.31, "legal": 0.80}
    out: dict[str, Any] = {"contract_version": 1}
    if classification:
        out["classification"] = {
            "status": "completed",
            "reason": None,
            "coverage": full_coverage(list(scores), text),
            "scores": [{"category_id": k, "score": v} for k, v in scores.items()],
        }
    if detection:
        found = (
            findings
            if findings is not None
            else [
                ("personal_data", "Ana Ejemplo"),
                ("personal_data", "ana.ejemplo@example.test"),
                ("password", "EjemploNoReal-123"),
            ]
        )
        out["detection"] = {
            "status": "completed",
            "reason": None,
            "coverage": full_coverage(list(TYPES), text),
            "findings": [{"type_id": t, **locate(text, frag)} for t, frag in found],
        }
    return out


async def request_of(ops, scope_id: str, text: str = TEXT, *, profile_id=None, version=None, caps=CAPS, context=None):
    resolved = await ops.resolve_profile(scope_id, profile_id=profile_id, version_number=version)
    return request_for(resolved, text=text, context=context, capabilities=tuple(caps))


async def fixture_for(
    ops,
    scope_id: str,
    outcome: Mapping[str, Any],
    *,
    text: str = TEXT,
    fid: str = "fx",
    profile_id=None,
    version=None,
    caps=CAPS,
    context=None,
) -> MockFixture:
    req = await request_of(ops, scope_id, text, profile_id=profile_id, version=version, caps=caps, context=context)
    return MockFixture.for_request(fid, req, outcome)


def mock(*fixtures: MockFixture) -> MockInferenceAdapter:
    return MockInferenceAdapter(list(fixtures), acknowledge_simulated=True)


@asynccontextmanager
async def inference_engine(
    base: ContentEngine, adapter: Any, *, timeout_s: float = 5.0
) -> AsyncIterator[ContentEngine]:
    """A second engine over the same database, composed with ``adapter`` (the host owns the adapter)."""
    eng = ContentEngine(
        EngineConfig(database_url=base.config.database_url), inference=InferenceSetup(adapter, timeout_s=timeout_s)
    )
    await eng.start()
    try:
        yield eng
    finally:
        await eng.aclose()


def analysis_ops(eng: ContentEngine, scope_id: str = "a", perms=(AE,), tx=None):
    return eng.scoped(eng.trusted_scope_context(scope_id, list(perms), actor="test-host"), tx)


class CustomAdapter:
    """A test adapter: counts calls, optionally blocks, records the request."""

    def __init__(self, output: Any = None, *, kind: str = "model", fn=None) -> None:
        self.output = output
        self.fn = fn
        self.calls = 0
        self.requests: list[InferenceRequest] = []
        self.cancelled = False
        self.descriptor = AdapterDescriptor(
            kind=kind,  # type: ignore[arg-type]
            adapter_id="test-adapter",
            adapter_version="1",
            configuration_id="cfg-1",
            model_id=None if kind == "mock" else "test-model",
            model_version=None if kind == "mock" else "0",
        )

    async def probe(self) -> AdapterState:
        return AdapterState.READY

    async def infer(self, request: InferenceRequest) -> Mapping[str, Any]:
        self.calls += 1
        self.requests.append(request)
        try:
            if self.fn is not None:
                return await self.fn(request)  # type: ignore[no-any-return]
            return copy.deepcopy(self.output)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def row_counts(engine: ContentEngine) -> dict[str, int]:
    out = {}
    async with engine._engine.connect() as c:  # type: ignore[union-attr]
        for name, table in vars(engine.tables).items():
            if isinstance(table, sa.Table):
                out[name] = int((await c.execute(sa.select(sa.func.count()).select_from(table))).scalar_one())
    return out
