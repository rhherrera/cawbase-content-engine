"""A06 example: protect a text, let a "model" answer with the markers, restore the answer. Python only.

    uv run --extra protection python examples/protection_host.py            # temporary SQLite file
    uv run --extra protection python examples/protection_host.py PATH.db    # or a SQLite file of your choice

Detection comes from a synthetic stand-in adapter (declared as a mock: it finds three fixed, evidently fake fragments).
It proves the engineering of protection and restoration, never the quality of detection (that is A09).
The 32-byte key below is generated for this run only; a real host reads its keys from its own secret store.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from cawbase_content_engine import (
    AdapterDescriptor,
    AdapterState,
    AdminPermission,
    ContentEngine,
    EngineConfig,
    InferenceSetup,
    InlineAnalysis,
    KeyRing,
    MarkerNotAuthorizedError,
    ProtectionPolicy,
    ProtectionSetup,
    ScopePermission,
    find_markers,
)
from cawbase_content_engine.errors import ContextUnavailableError
from cawbase_content_engine.inference import full_coverage, locate
from cawbase_content_engine.migrations import upgrade

TEXT = "Contactar a Ana Ejemplo (ana.ejemplo@example.test). Mi contraseña es EjemploNoReal-123."
FRAGMENTS = (
    ("personal_data", "Ana Ejemplo"),
    ("personal_data", "ana.ejemplo@example.test"),
    ("password", "EjemploNoReal-123"),
)
SP = ScopePermission


class SyntheticDetector:
    """A stand-in for the model: it reports exactly the fixed synthetic fragments present in the text."""

    descriptor = AdapterDescriptor(
        kind="mock", adapter_id="example-detector", adapter_version="1", configuration_id="ex-1"
    )

    async def probe(self) -> AdapterState:
        return AdapterState.READY

    async def infer(self, request):  # type: ignore[no-untyped-def]
        text, ids = request.text, [t.element_id for t in request.sensitive_types]
        found = [{"type_id": t, **locate(text, frag)} for t, frag in FRAGMENTS if frag in text and t in ids]
        return {
            "contract_version": 1,
            "detection": {
                "status": "completed",
                "reason": None,
                "coverage": full_coverage(ids, text),
                "findings": found,
            },
        }


async def main(target: str) -> None:
    url = target if "://" in target else f"sqlite+aiosqlite:///{target}"
    await upgrade(EngineConfig(database_url=url))
    keys = KeyRing(active="2026-01", keys={"2026-01": os.urandom(32)})  # a real host reads these from its secret store
    async with ContentEngine(
        EngineConfig(database_url=url),
        inference=InferenceSetup(SyntheticDetector(), timeout_s=10),
        protection=ProtectionSetup(keys=keys),
    ) as engine:
        # 1) a published profile with detection (A02) and the scope
        await engine.admin(engine.trusted_admin_context(list(AdminPermission))).create_scope("team-a", "Team A")
        setup = engine.scoped(engine.trusted_scope_context("team-a", [SP.PROFILES_READ, SP.PROFILES_WRITE]))
        await setup.create_profile("team-a", "general", "General", initial_catalog="empty")
        for tid in ("password", "personal_data"):
            await setup.create_sensitive_type("team-a", "general", tid, tid.title(), f"Detects {tid}.")
        p = await setup.get_profile("team-a", "general")
        await setup.publish_profile("team-a", "general", expected_draft_revision=p.draft_revision)
        d = await setup.get_default_profile("team-a")
        await setup.set_default_profile("team-a", "general", expected_revision=d.revision)

        # 2) a context, then protect: the strict policy redacts the password for good and pseudonymizes the rest
        perms = [SP.TEXT_PROTECT, SP.TEXT_RESTORE, SP.CONTEXTS_READ, SP.CONTEXTS_DELETE, SP.ANALYSIS_EXECUTE]
        host = engine.scoped(engine.trusted_scope_context("team-a", perms, actor="example-host"))
        ctx = await host.create_context("team-a", kind="conversation")
        print("context:", ctx.state, "| kind:", ctx.kind, "| lives until", ctx.expires_at.date())
        policy = ProtectionPolicy("pseudonymize", {"password": "redact"})
        protected = await host.protect_text(
            "team-a",
            context_id=ctx.context_id,
            kind="conversation",
            text=TEXT,
            policy=policy,
            analysis=InlineAnalysis(),
        )
        print("protected:", protected.text)
        print("analysis:", protected.analysis.provenance.kind, "simulated =", protected.analysis.provenance.simulated)

        # 3) the host sends `protected.text` to the model; the model answers using the markers it was given
        reply = f"Entendido: escribiré a {protected.markers[0]} hoy."
        # 4) restoration: only markers the host DELIVERED, after the reply was evaluated for new sensitive data
        restored = await host.restore_text(
            "team-a",
            context_id=ctx.context_id,
            kind="conversation",
            text=reply,
            analysis=InlineAnalysis(),
            delivered_material=[protected.text],
        )
        print("restored:", restored.text, "|", restored.outcome)

        # a marker the model invented (or that was not delivered) is never restored
        forged = reply.replace(protected.markers[0], "[[CCE1:" + "Q" * 16 + "]]")
        try:
            await host.restore_text(
                "team-a",
                context_id=ctx.context_id,
                kind="conversation",
                text=forged,
                analysis=InlineAnalysis(),
                delivered_markers=find_markers(protected.text),
            )
        except MarkerNotAuthorizedError as exc:
            print("forged marker:", exc.code)

        # 5) explicit, irreversible deletion: nothing can be restored afterwards, even with the keys
        await host.delete_context("team-a", ctx.context_id)
        try:
            await host.restore_text(
                "team-a",
                context_id=ctx.context_id,
                kind="conversation",
                text=reply,
                analysis=InlineAnalysis(),
                delivered_material=[protected.text],
            )
        except ContextUnavailableError as exc:
            print("after delete:", exc.code)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else str(Path(tempfile.mkdtemp()) / "protection.db")))
