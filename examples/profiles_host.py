"""A02 example: profiles, categories, sensitive types, versions, default profile and resolution.

    uv run python examples/profiles_host.py            # temporary SQLite file
    uv run python examples/profiles_host.py PATH.db    # or a SQLite file of your choice
    uv run python examples/profiles_host.py postgresql+asyncpg://user:pass@host/db   # an EMPTY, disposable database

No CAWbase, no model, no mock: this only configures and versions the analysis configuration. All sample
text is synthetic.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from cawbase_content_engine import (
    AdminPermission,
    ContentEngine,
    DefaultProfileNotSetError,
    EngineConfig,
    ProfileNotSelectableError,
    ScopePermission,
)
from cawbase_content_engine.migrations import upgrade


async def main(target: str) -> None:
    url = target if "://" in target else f"sqlite+aiosqlite:///{target}"
    print("migrated:", (await upgrade(EngineConfig(database_url=url))).applied)

    async with ContentEngine(EngineConfig(database_url=url)) as engine:
        # The HOST authenticates its caller and builds the contexts (here we play the host).
        admin = engine.admin(engine.trusted_admin_context(list(AdminPermission)))
        await admin.create_scope("team-a", "Team A")
        ctx = engine.trusted_scope_context(
            "team-a", [ScopePermission.PROFILES_READ, ScopePermission.PROFILES_WRITE], actor="example-host"
        )
        ops = engine.scoped(ctx)

        # 1) A profile copies the packaged starter catalog of sensitive types (six types) and records its origin.
        profile = (await ops.create_profile("team-a", "general", "General profile", language="en")).value
        types = (await ops.list_sensitive_types("team-a", "general")).items
        print(
            "profile:",
            profile.profile_id,
            "| catalog v",
            profile.catalog_provenance.catalog_version,
            "|",
            len(types),
            "types",
        )

        # 2) Edit the draft: a thematic category (priority: lower number wins in resolution mode) and ranking settings.
        await ops.create_category(
            "team-a",
            "general",
            "billing",
            "Billing",
            "Invoices, payments and refunds.",
            examples={"positive": ["Please resend invoice 0000."], "negative": ["Our office opens at nine."]},
            priority=1,
        )
        await ops.update_ranking_config("team-a", "general", expected_revision=1, mode="resolution", tie_tolerance=0.05)

        # 3) Publish: an immutable snapshot. Editing afterwards never changes it.
        draft = (await ops.get_profile("team-a", "general")).draft_revision
        v1 = (await ops.publish_profile("team-a", "general", expected_draft_revision=draft)).value
        print("published: version", v1.version_number, v1.capabilities)
        await ops.update_category("team-a", "general", "billing", expected_revision=1, definition="Edited later.")
        again = await ops.get_profile_version("team-a", "general", 1)
        print(
            "version 1 unchanged after editing:",
            again.snapshot["categories"][0]["definition"] == "Invoices, payments and refunds.",
        )

        # 4) Default profile and resolution (no analysis runs here).
        try:
            await ops.resolve_profile("team-a")
        except DefaultProfileNotSetError:
            print("no default profile: resolving without one is a typed error")
        await ops.set_default_profile("team-a", "general", expected_revision=0)
        resolved = await ops.resolve_profile("team-a")
        print(
            "resolved:",
            resolved.selection,
            "-> version",
            resolved.version.version_number,
            resolved.version.version_id[:8] + "...",
        )

        # 5) A disabled profile cannot be selected for new analysis, but its versions stay readable.
        await ops.clear_default_profile("team-a", expected_revision=1)
        await ops.disable_profile("team-a", "general")
        try:
            await ops.resolve_profile("team-a", profile_id="general", version_number=1)
        except ProfileNotSelectableError:
            print("disabled profile: not selectable, even with an explicit version")
        print("history readable:", (await ops.get_profile_version("team-a", "general", 1)).version_number == 1)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else str(Path(tempfile.mkdtemp()) / "engine.db")))
