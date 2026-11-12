"""Helpers for the A02 tests (profiles, categories, sensitive types)."""

from __future__ import annotations

import sqlalchemy as sa

from cawbase_content_engine import ContentEngine, ScopePermission

from .helpers import make_scope

PR, PW = ScopePermission.PROFILES_READ, ScopePermission.PROFILES_WRITE
ALL_PROFILE_PERMS = (PR, PW)
NO_CATALOG = {"initial_catalog": "empty"}


async def ops_for(engine: ContentEngine, scope_id: str = "a", perms=ALL_PROFILE_PERMS, *, create_scope: bool = True):
    """ScopedOperations for ``scope_id`` (creating the scope on first use)."""
    if create_scope:
        try:
            await make_scope(engine, scope_id)
        except Exception:
            pass
    return engine.scoped(engine.trusted_scope_context(scope_id, list(perms), actor="test-host"))


async def published(ops, scope_id: str, profile_id: str, **kw):
    """Create a profile with one category, publish it and return (profile, version)."""
    await ops.create_profile(scope_id, profile_id, profile_id.title(), **kw)
    await ops.create_category(scope_id, profile_id, "billing", "Billing", "Invoices and payments.", priority=1)
    p = await ops.get_profile(scope_id, profile_id)
    v = (await ops.publish_profile(scope_id, profile_id, expected_draft_revision=p.draft_revision)).value
    return await ops.get_profile(scope_id, profile_id), v


async def events(engine: ContentEngine, prefix: str = "", scope_id: str | None = None):
    t = engine.tables.admin_events
    q = sa.select(t).order_by(t.c.event_id)
    if scope_id:
        q = q.where(t.c.scope_id == scope_id)
    async with engine._engine.connect() as c:  # type: ignore[union-attr]
        rows = (await c.execute(q)).all()
    return [r for r in rows if r.action.startswith(prefix)]


async def count_rows(engine: ContentEngine, table: str) -> int:
    t = getattr(engine.tables, table)
    async with engine._engine.connect() as c:  # type: ignore[union-attr]
        return int((await c.execute(sa.select(sa.func.count()).select_from(t))).scalar_one())
