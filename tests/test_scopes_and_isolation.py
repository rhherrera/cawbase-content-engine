"""Scope resource, isolation, strict optimistic concurrency (P-20): AC-40..42, AC-70, AC-72, AC-75."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

from cawbase_content_engine import (
    AdminPermission,
    ConflictError,
    PermissionDeniedError,
    ResourceNotAccessibleError,
    ResourceNotFoundError,
    RevisionConflictError,
    ScopeDisabledError,
    ValidationFailedError,
)

from .helpers import admin_ctx, make_scope


class TestScopeCreation:
    async def test_idempotent_by_scope_id_and_conflicting_attributes(self, engine):  # AC-70
        admin = engine.admin(await admin_ctx(engine))
        first = await admin.create_scope("a", "A", metadata={"k": 1})
        again = await admin.create_scope("a", "A", metadata={"k": 1})
        assert first.created and not again.created and first.value == again.value
        with pytest.raises(ConflictError):
            await admin.create_scope("a", "Other", metadata={"k": 1})

    async def test_concurrent_creation_makes_one_row(self, engine):  # AC-70
        admin = engine.admin(await admin_ctx(engine))
        results = await asyncio.gather(*[admin.create_scope("race", "R") for _ in range(8)], return_exceptions=True)
        assert sum(1 for r in results if not isinstance(r, Exception) and r.created) == 1
        assert not [r for r in results if isinstance(r, Exception)]

    @pytest.mark.parametrize("bad", ["", " a", "a b", "-a", ".a", "a/b", "é", "x" * 129, "a;drop"])
    async def test_scope_id_format_is_strict(self, engine, bad):
        with pytest.raises(ValidationFailedError):
            await engine.admin(await admin_ctx(engine)).create_scope(bad, "x")

    async def test_metadata_limits(self, engine):
        admin = engine.admin(await admin_ctx(engine))
        with pytest.raises(ValidationFailedError):
            await admin.create_scope("m", "M", metadata={"x": "y" * 5000})
        with pytest.raises(ValidationFailedError):
            await admin.create_scope("m", "M", metadata=[1])  # type: ignore[arg-type]
        with pytest.raises(ValidationFailedError):
            await admin.create_scope("m", "")

    async def test_scope_id_is_immutable_and_knowing_it_grants_nothing(self, engine):
        await make_scope(engine, "secret-scope")
        ctx_other = engine.trusted_scope_context("other", ["scope:read"])
        with pytest.raises(ResourceNotAccessibleError):
            await engine.scoped(ctx_other).get_scope("secret-scope")
        assert not hasattr(engine.scoped(ctx_other), "rename_scope")


class TestIsolation:
    async def test_two_scopes_cannot_cross(self, engine):  # AC-40
        a = await make_scope(engine, "a")
        b = await make_scope(engine, "b")
        ctx_a = engine.trusted_scope_context("a", ["scope:read", "scope:write"])
        assert (await engine.scoped(ctx_a).get_scope("a")).scope_id == a.scope_id
        with pytest.raises(ResourceNotAccessibleError):
            await engine.scoped(ctx_a).get_scope("b")
        with pytest.raises(ResourceNotAccessibleError):
            await engine.scoped(ctx_a).update_scope("b", expected_revision=1, display_name="hijack")
        untouched = await engine.admin(await admin_ctx(engine)).get_scope("b")
        assert untouched == b
        with pytest.raises(ResourceNotAccessibleError):  # nonexistent == not accessible
            await engine.scoped(ctx_a).get_scope("zzz")

    async def test_permissions_are_enforced(self, engine):  # AC-41
        await make_scope(engine, "a")
        ro = engine.trusted_scope_context("a", ["scope:read"])
        with pytest.raises(PermissionDeniedError):
            await engine.scoped(ro).update_scope("a", expected_revision=1, display_name="x")
        none = engine.trusted_scope_context("a", [])
        with pytest.raises(PermissionDeniedError):
            await engine.scoped(none).get_scope("a")

    async def test_context_from_another_engine_instance_is_rejected(self, engine, migrated):  # AC-41
        from cawbase_content_engine import ContentEngine, EngineConfig

        other = ContentEngine(EngineConfig(database_url=migrated[1]))
        await other.start()
        foreign = other.trusted_scope_context("a", ["scope:read"])
        with pytest.raises(PermissionDeniedError):
            engine.scoped(foreign)
        await other.aclose()

    async def test_admin_and_scope_operations_are_separate(self, engine):  # AC-42
        await make_scope(engine, "a")
        limited = engine.trusted_admin_context([AdminPermission.ADMIN_SCOPES_READ])
        with pytest.raises(PermissionDeniedError):
            await engine.admin(limited).create_scope("x", "X")
        scope_ctx = engine.trusted_scope_context("a", ["scope:read", "scope:write"])
        with pytest.raises(AttributeError):
            engine.scoped(scope_ctx).create_scope  # type: ignore[attr-defined]  # noqa: B018
        with pytest.raises(AttributeError):
            engine.scoped(scope_ctx).list_scopes  # type: ignore[attr-defined]  # noqa: B018

    async def test_listing_and_pagination(self, engine):
        admin = engine.admin(await admin_ctx(engine))
        for i in range(5):
            await admin.create_scope(f"p{i}", f"P{i}")
        page1 = await admin.list_scopes(limit=2)
        page2 = await admin.list_scopes(limit=2, cursor=page1.next_cursor)
        page3 = await admin.list_scopes(limit=2, cursor=page2.next_cursor)
        assert [s.scope_id for s in page1.items + page2.items + page3.items] == [f"p{i}" for i in range(5)]
        assert page3.next_cursor is None
        with pytest.raises(ValidationFailedError):
            await admin.list_scopes(limit=10_000)
        await admin.set_scope_status("p0", "disabled")
        assert [s.scope_id for s in (await admin.list_scopes(status="disabled")).items] == ["p0"]


class TestStrictOptimisticConcurrency:
    """P-20 (adopted): expected_revision must always match; 412 even if the values are already applied."""

    async def test_matching_revision_applies_and_bumps(self, engine):  # AC-72
        await make_scope(engine, "a")
        ops = engine.scoped(engine.trusted_scope_context("a", ["scope:read", "scope:write"]))
        s = await ops.update_scope("a", expected_revision=1, display_name="B", metadata={"x": 1})
        assert (s.revision, s.display_name, s.metadata) == (2, "B", {"x": 1})

    async def test_stale_retry_gets_412_even_when_values_are_already_applied(self, engine):  # AC-72
        await make_scope(engine, "a")
        ops = engine.scoped(engine.trusted_scope_context("a", ["scope:read", "scope:write"]))
        await ops.update_scope("a", expected_revision=1, display_name="B")  # response "lost"
        with pytest.raises(RevisionConflictError):
            await ops.update_scope("a", expected_revision=1, display_name="B")  # blind retry
        current = await ops.get_scope("a")  # consumer re-reads and reconciles
        assert current.revision == 2 and current.display_name == "B"

    async def test_matching_revision_without_effective_change_does_not_write(self, engine):  # AC-72
        await make_scope(engine, "a")
        ops = engine.scoped(engine.trusted_scope_context("a", ["scope:read", "scope:write"]))
        before = await ops.get_scope("a")
        same = await ops.update_scope("a", expected_revision=1, display_name=before.display_name)
        assert same == before and same.revision == 1 and same.updated_at == before.updated_at
        async with engine._engine.connect() as c:
            n = (
                await c.execute(
                    sa.select(sa.func.count())
                    .select_from(engine.tables.admin_events)
                    .where(engine.tables.admin_events.c.action == "scope.updated")
                )
            ).scalar_one()
        assert n == 0

    async def test_concurrent_updates_with_same_revision_one_wins(self, engine):  # AC-72
        await make_scope(engine, "a")
        ops = engine.scoped(engine.trusted_scope_context("a", ["scope:read", "scope:write"]))
        results = await asyncio.gather(
            *[ops.update_scope("a", expected_revision=1, display_name=f"N{i}") for i in range(6)],
            return_exceptions=True,
        )
        ok = [r for r in results if not isinstance(r, Exception)]
        conflicts = [r for r in results if isinstance(r, RevisionConflictError)]
        assert len(ok) == 1 and len(conflicts) == 5, results
        assert (await ops.get_scope("a")).revision == 2

    async def test_disabled_scope_blocks_writes_but_allows_reads(self, engine):
        await make_scope(engine, "a")
        admin = engine.admin(await admin_ctx(engine))
        ops = engine.scoped(engine.trusted_scope_context("a", ["scope:read", "scope:write"]))
        await admin.set_scope_status("a", "disabled")
        with pytest.raises(ScopeDisabledError):
            await ops.update_scope("a", expected_revision=2, display_name="x")
        assert (await ops.get_scope("a")).status == "disabled"
        again = await admin.set_scope_status("a", "disabled")  # target state: repeatable, no new revision
        assert again.revision == 2

    async def test_invalid_inputs(self, engine):
        await make_scope(engine, "a")
        ops = engine.scoped(engine.trusted_scope_context("a", ["scope:read", "scope:write"]))
        for bad in (0, -1, "1", True):
            with pytest.raises(ValidationFailedError):
                await ops.update_scope("a", expected_revision=bad, display_name="x")  # type: ignore[arg-type]
        with pytest.raises(ValidationFailedError):
            await ops.update_scope("a", expected_revision=1)


class TestAdminEvents:
    async def test_events_hold_names_not_values(self, engine):  # AC-75
        admin = engine.admin(await admin_ctx(engine))
        await admin.create_scope("a", "VeryDistinctiveName", metadata={"leak": "MetadataCanary"})
        ops = engine.scoped(engine.trusted_scope_context("a", ["scope:read", "scope:write"]))
        await ops.update_scope("a", expected_revision=1, display_name="NewDistinctiveName")
        await admin.set_scope_status("a", "disabled")
        async with engine._engine.connect() as c:
            rows = (
                await c.execute(sa.select(engine.tables.admin_events).order_by(engine.tables.admin_events.c.event_id))
            ).all()
        assert [r.action for r in rows] == ["scope.created", "scope.updated", "scope.disabled"]
        blob = repr([tuple(r) for r in rows])
        for canary in ("VeryDistinctiveName", "NewDistinctiveName", "MetadataCanary"):
            assert canary not in blob
        assert rows[1].detail == {"changed_fields": ["display_name"]}

    async def test_missing_resources_in_admin_routes(self, engine):
        admin = engine.admin(await admin_ctx(engine))
        with pytest.raises(ResourceNotFoundError):
            await admin.get_scope("nope")
        with pytest.raises(ResourceNotFoundError):
            await admin.get_client("cli_nope")
