from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from cawbase_content_engine import AdminPermission, ContentEngine, ScopePermission, create_initial_admin_key

ALL_ADMIN = list(AdminPermission)
SECRET_CANARIES: list[str] = []


async def admin_ctx(engine: ContentEngine, *perms: AdminPermission, scope_grants=None):
    return engine.trusted_admin_context(perms or ALL_ADMIN, scope_grants=scope_grants, actor="test")


async def make_scope(engine: ContentEngine, scope_id: str, name: str | None = None):
    admin = engine.admin(await admin_ctx(engine))
    return (await admin.create_scope(scope_id, name or scope_id.upper())).value


async def issue(engine: ContentEngine, client_name: str, *, admin=(), scopes=None, label="k"):
    """Create a client and issue a key through the real, delegating API using a full-admin context."""
    import uuid

    ops = engine.admin(await admin_ctx(engine))
    client = (await ops.create_client(client_name, client_name)).value
    issued = await ops.issue_api_key(
        client.client_id,
        request_id=str(uuid.uuid4()),
        label=label,
        grants={"admin": list(admin), "scopes": scopes or {}},
    )
    return client, issued


async def bootstrap(engine: ContentEngine):
    return await create_initial_admin_key(engine)


async def table_names(url: str) -> set[str]:
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(url)
    try:
        async with eng.connect() as c:
            names = await c.run_sync(lambda s: set(sa.inspect(s).get_table_names()))
            if "postgresql" in url:
                names |= await c.run_sync(
                    lambda s: {
                        f"cawbase_content_engine.{n}" for n in sa.inspect(s).get_table_names("cawbase_content_engine")
                    }
                )
        return names
    finally:
        await eng.dispose()


async def make_host_engine(backend: str, url: str, **kwargs) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine

    from cawbase_content_engine import install_transaction_control

    eng = create_async_engine(url, **kwargs)
    if backend == "sqlite":
        install_transaction_control(eng)
    return eng


def scope_perms(*p: str) -> list[ScopePermission]:
    return [ScopePermission(x) for x in p]
