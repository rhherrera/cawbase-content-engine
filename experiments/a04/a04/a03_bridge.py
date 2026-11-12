"""EXP-04: pass extraction units through the ACCEPTED A03 contract with the deterministic MOCK (no changes to A03).

Everything here is simulated: the mock replays findings that the generator's manifest declares; it measures no detection quality.
"""

from __future__ import annotations

import tempfile
from typing import Any

from cawbase_content_engine import (
    AdminPermission,
    ContentEngine,
    EngineConfig,
    InferenceSetup,
    MockFixture,
    MockInferenceAdapter,
    ScopePermission,
    build_inference_request,
)
from cawbase_content_engine.inference import full_coverage, locate
from cawbase_content_engine.migrations import upgrade

TYPES = ("password", "personal_data")
TYPE_OF_VALUE = {
    "v1": "personal_data",
    "v2": "personal_data",
    "v3": "password",
    "v4": "personal_data",
    "v5": "personal_data",
    "v6": "personal_data",
}


class Bridge:
    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="cce_a04_")  # disposable SQLite file, never an existing database
        self.url = f"sqlite+aiosqlite:///{self.dir}/a04.db"
        self.fixtures: list[MockFixture] = []
        self.resolved: Any = None

    async def setup(self) -> None:
        await upgrade(EngineConfig(database_url=self.url))
        perms = [ScopePermission.PROFILES_READ, ScopePermission.PROFILES_WRITE]
        async with ContentEngine(EngineConfig(database_url=self.url)) as e:
            await e.admin(e.trusted_admin_context(list(AdminPermission))).create_scope("a04", "A04 experiments")
            ops = e.scoped(e.trusted_scope_context("a04", perms))
            await ops.create_profile("a04", "docs", "Docs", initial_catalog="empty")
            await ops.create_category("a04", "docs", "generic", "Generic", "Generic synthetic text.", priority=1)
            for tid in TYPES:
                await ops.create_sensitive_type("a04", "docs", tid, tid.title(), f"Detects {tid}.")
            p = await ops.get_profile("a04", "docs")
            await ops.publish_profile("a04", "docs", expected_draft_revision=p.draft_revision)
            d = await ops.get_default_profile("a04")
            await ops.set_default_profile("a04", "docs", expected_revision=d.revision)
            self.resolved = await ops.resolve_profile("a04")

    def author(self, fid: str, text: str, findings: list[tuple[str, int, int]], *, partial: bool = False) -> None:
        """Author a mock fixture bound to this exact text. ``partial`` declares incomplete coverage of personal_data."""
        req = build_inference_request(self.resolved, text=text, capabilities=("detection",))
        located = []
        for tid, s, e in findings:
            frag = text[s:e]
            k = 0
            idx = -1
            while True:
                idx = text.index(frag, idx + 1)
                if idx == s:
                    break
                k += 1
            loc = locate(text, frag, occurrence=k)
            assert loc["start"] == s and loc["end"] == e
            located.append({"type_id": tid, **loc})
        if partial:
            half = max(1, len(text) // 2)
            cov = {
                "groups": [
                    {"ids": ["password"], "regions": [{"start": 0, "end": len(text)}]},
                    {"ids": ["personal_data"], "regions": [{"start": 0, "end": half}]},
                ]
            }
            located = [f for f in located if f["type_id"] == "password" or f["end"] <= half]
            det = {"status": "inconclusive", "reason": "partial_coverage", "coverage": cov, "findings": located}
        else:
            det = {
                "status": "completed",
                "reason": None,
                "coverage": full_coverage(list(TYPES), text),
                "findings": located,
            }
        out = {"contract_version": 1, "detection": det}
        self.fixtures.append(MockFixture.for_request(fid, req, {"kind": "output", "output": out}))

    async def analyze(self, texts: list[str]) -> list[Any]:
        adapter = MockInferenceAdapter(self.fixtures, acknowledge_simulated=True)
        async with ContentEngine(
            EngineConfig(database_url=self.url), inference=InferenceSetup(adapter, timeout_s=20)
        ) as e:
            an = e.scoped(e.trusted_scope_context("a04", [ScopePermission.ANALYSIS_EXECUTE]))
            return [await an.analyze_text("a04", text=t, capabilities=("detection",)) for t in texts]
