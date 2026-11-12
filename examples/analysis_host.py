"""A03 example: text analysis through the deterministic MOCK adapter (no CAWbase, no model).

    uv run python examples/analysis_host.py            # temporary SQLite file
    uv run python examples/analysis_host.py PATH.db    # or a SQLite file of your choice

The mock replays a synthetic fixture bound to this exact text and profile definitions; it detects
nothing by itself and proves nothing about real inference quality (that is A09). All text is synthetic.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from cawbase_content_engine import (
    AdminPermission,
    ContentEngine,
    EngineConfig,
    InferenceFixtureNotFoundError,
    InferenceSetup,
    MockFixture,
    MockInferenceAdapter,
    PermissionDeniedError,
    ScopePermission,
    build_inference_request,
)
from cawbase_content_engine.inference import full_coverage, locate
from cawbase_content_engine.migrations import upgrade

TEXT = "Contactar a Ana Ejemplo (ana.ejemplo@example.test). Mi contraseña es EjemploNoReal-123."


async def main(target: str) -> None:
    url = target if "://" in target else f"sqlite+aiosqlite:///{target}"
    await upgrade(EngineConfig(database_url=url))
    perms = [ScopePermission.PROFILES_READ, ScopePermission.PROFILES_WRITE]

    # 1) Configure a published profile (A02). The host builds the contexts.
    async with ContentEngine(EngineConfig(database_url=url)) as setup_engine:
        await setup_engine.admin(setup_engine.trusted_admin_context(list(AdminPermission))).create_scope(
            "team-a", "Team A"
        )
        ops = setup_engine.scoped(setup_engine.trusted_scope_context("team-a", perms))
        await ops.create_profile("team-a", "general", "General", initial_catalog="empty")
        for cid, prio in (("finance", 1), ("legal", 2), ("hr", 3)):
            await ops.create_category("team-a", "general", cid, cid.title(), f"Texts about {cid}.", priority=prio)
        for tid in ("password", "personal_data"):
            await ops.create_sensitive_type("team-a", "general", tid, tid.title(), f"Detects {tid}.")
        rk = await ops.get_ranking_config("team-a", "general")
        await ops.update_ranking_config(
            "team-a",
            "general",
            expected_revision=rk.revision,
            mode="resolution",
            tie_tolerance=0.05,
            eligibility_threshold=0.5,
        )
        p = await ops.get_profile("team-a", "general")
        await ops.publish_profile("team-a", "general", expected_draft_revision=p.draft_revision)
        d = await ops.get_default_profile("team-a")
        await ops.set_default_profile("team-a", "general", expected_revision=d.revision)

        # 2) Author a mock fixture for this exact input: the request the engine would send fixes its key.
        resolved = await ops.resolve_profile("team-a")
        request = build_inference_request(resolved, text=TEXT)
        output = {
            "contract_version": 1,
            "classification": {
                "status": "completed",
                "reason": None,
                "coverage": full_coverage(["finance", "hr", "legal"], TEXT),
                "scores": [
                    {"category_id": "finance", "score": 0.82},
                    {"category_id": "hr", "score": 0.31},
                    {"category_id": "legal", "score": 0.80},
                ],
            },
            "detection": {
                "status": "completed",
                "reason": None,
                "coverage": full_coverage(["password", "personal_data"], TEXT),
                "findings": [
                    {"type_id": "personal_data", **locate(TEXT, "Ana Ejemplo")},
                    {"type_id": "personal_data", **locate(TEXT, "ana.ejemplo@example.test")},
                    {"type_id": "password", **locate(TEXT, "EjemploNoReal-123")},
                ],
            },
        }
        fixture = MockFixture.for_request("example", request, {"kind": "output", "output": output})

    # 3) Compose the engine with the adapter (the HOST owns it). `analysis:execute` is enough: no profiles:read.
    adapter = MockInferenceAdapter([fixture], acknowledge_simulated=True)
    async with ContentEngine(EngineConfig(database_url=url), inference=InferenceSetup(adapter, timeout_s=10)) as engine:
        analyst = engine.scoped(engine.trusted_scope_context("team-a", [ScopePermission.ANALYSIS_EXECUTE]))
        result = await analyst.analyze_text("team-a", text=TEXT)
        print(
            "status:",
            result.status,
            "| provenance:",
            result.provenance.kind,
            "simulated =",
            result.provenance.simulated,
        )
        print("version:", result.profile.profile_id, "v", result.profile.version_number, "|", result.profile.selection)
        for r in result.classification.ranking:
            print(f"  rank {r.rank}: {r.category_id} score={r.score} eligible={r.eligible}")
        res = result.classification.resolution
        print(
            "selected:", res.selected_category_id, "| reason:", res.reason, "| candidates:", res.candidate_category_ids
        )
        for f in result.detection.findings:
            print(f"  finding {f.type_id} [{f.start}, {f.end}) = {TEXT[f.start : f.end]!r}")
        print("coverage complete:", result.classification.coverage.complete, result.detection.coverage.complete)

        # `analysis:execute` does not read profiles, and an input with no fixture fails: never an empty success.
        try:
            await analyst.resolve_profile("team-a")
        except PermissionDeniedError:
            print("analysis:execute cannot read profiles: denied")
        try:
            await analyst.analyze_text("team-a", text="otro texto sin fixture")
        except InferenceFixtureNotFoundError as exc:
            print("no fixture:", exc.code)
        print("engine inference state:", (await engine.inference_status()).state)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else str(Path(tempfile.mkdtemp()) / "analysis.db")))
