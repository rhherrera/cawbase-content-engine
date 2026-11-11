"""Embedded host example: no CAWbase, no HTTP, no model.

    uv run python examples/python_host.py            # temporary SQLite file
    uv run python examples/python_host.py PATH.db    # or a file of your choice

Shows: explicit migration, explicit lifecycle, scopes, isolation, own and host-provided transactions.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from cawbase_content_engine import (
    AdminPermission,
    ContentEngine,
    EngineConfig,
    HostTransaction,
    ResourceNotAccessibleError,
    ScopePermission,
    create_initial_admin_key,
    install_transaction_control,
)
from cawbase_content_engine.migrations import upgrade


async def main(db_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{db_path}"

    # 1) Migrations are an explicit deployment step; importing/starting the engine never runs them.
    print("migrated:", (await upgrade(EngineConfig(database_url=url))).applied)

    # 2) The engine owns its engine and pool here (URL mode). Explicit start / close.
    async with ContentEngine(EngineConfig(database_url=url)) as engine:
        operator = await create_initial_admin_key(engine)  # operator-local, secret shown once
        principal = await engine.authenticate_api_key(operator.secret)  # API keys are for HTTP; shown for completeness

        # 3) The HOST authorizes the caller and builds the contexts. Here we act as the host.
        admin = engine.admin(principal.admin_context())
        await admin.create_scope("project-a", "Project A")
        await admin.create_scope("project-b", "Project B")

        ctx_a = engine.trusted_scope_context("project-a", [ScopePermission.SCOPE_READ, ScopePermission.SCOPE_WRITE])
        scope = await engine.scoped(ctx_a).update_scope("project-a", expected_revision=1, display_name="Alpha")
        print("updated:", scope.scope_id, scope.display_name, "revision", scope.revision)
        try:
            await engine.scoped(ctx_a).get_scope("project-b")
        except ResourceNotAccessibleError:
            print("isolation: project-a cannot read project-b")

    # 4) Injected engine + host transaction: the host owns the engine and decides commit/rollback.
    host_engine = create_async_engine(url)
    install_transaction_control(host_engine)  # explicit, host-invoked (SQLite needs a real BEGIN)
    async with ContentEngine(EngineConfig(async_engine=host_engine)) as engine:
        ops = engine.admin(
            engine.trusted_admin_context([AdminPermission.ADMIN_SCOPES_WRITE, AdminPermission.ADMIN_SCOPES_READ])
        )
        try:
            async with host_engine.begin() as conn:
                tx = HostTransaction(conn)
                await engine.admin(
                    engine.trusted_admin_context([AdminPermission.ADMIN_SCOPES_WRITE]), transaction=tx
                ).create_scope("project-c", "Project C")
                raise RuntimeError("the host aborts its transaction")
        except RuntimeError:
            pass
        page = await ops.list_scopes()
        print("after host rollback:", [s.scope_id for s in page.items])  # project-c is not there
    await host_engine.dispose()  # the host closes what it owns
    _ = (sa, uuid)


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp()) / "example.db"
    asyncio.run(main(target))
