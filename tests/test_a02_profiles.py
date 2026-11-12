"""A02 over the Python API, on real PostgreSQL and SQLite (AC-A02-01, 03-05, 07-25, 27-33, 36, 40-41, 45-46, 50-51)."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid

import pytest
import sqlalchemy as sa

from cawbase_content_engine import (
    AdminPermission,
    ContentEngine,
    HostTransaction,
    Limits,
    ScopePermission,
    starter_catalog,
)
from cawbase_content_engine.errors import (
    ConflictError,
    DefaultProfileNotSetError,
    InvalidStateTransitionError,
    LimitExceededError,
    NameConflictError,
    PermissionDeniedError,
    PriorityConflictError,
    ProfileDisabledError,
    ProfileInvalidError,
    ProfileIsDefaultError,
    ProfileNotPublishedError,
    ProfileNotSelectableError,
    ResourceNotAccessibleError,
    ResourceNotFoundError,
    RevisionConflictError,
    ScopeDisabledError,
    TransactionBrokenError,
    ValidationFailedError,
)
from cawbase_content_engine.operations.versions import canonical, effective_projection

from .a02_helpers import NO_CATALOG, PR, PW, count_rows, events, ops_for, published
from .helpers import admin_ctx, issue, make_host_engine, make_scope


class TestJourney:
    async def test_full_python_journey_without_cawbase_model_or_mock(self, engine):  # AC-A02-01
        ops = await ops_for(engine, "a")
        prof = (await ops.create_profile("a", "general", "General", language="es")).value
        assert prof.status == "active" and prof.current_version_number is None and not prof.is_default
        assert prof.ranking.mode == "ranking" and prof.ranking.eligibility_threshold == 0.0
        assert (await ops.create_category("a", "general", "billing", "Facturación", "Facturas y pagos.")).created
        ranking = await ops.update_ranking_config("a", "general", expected_revision=1, tie_tolerance=0.05)
        assert ranking.revision == 2 and ranking.tie_tolerance == 0.05
        prof = await ops.get_profile("a", "general")
        pub = await ops.publish_profile("a", "general", expected_draft_revision=prof.draft_revision)
        assert pub.created and pub.value.version_number == 1 and pub.value.version_id.startswith("pvr_")
        assert pub.value.capabilities == {"classification": True, "detection": True}
        d = await ops.set_default_profile("a", "general", expected_revision=0)
        assert d.profile_id == "general" and d.revision == 1
        res = await ops.resolve_profile("a")
        assert res.selection == "default" and res.version.version_id == pub.value.version_id
        assert (await ops.resolve_profile("a", profile_id="general", version_number=1)).selection == "explicit"
        assert (await ops.get_profile("a", "general")).is_default

    async def test_nothing_is_created_with_a_scope_and_a_scope_without_profiles_is_valid(self, engine):  # AC-A02-19
        ops = await ops_for(engine, "empty-scope")
        assert (await ops.list_profiles("empty-scope")).items == []
        assert (await ops.get_default_profile("empty-scope")).profile_id is None
        with pytest.raises(DefaultProfileNotSetError):
            await ops.resolve_profile("empty-scope")


class TestIsolationAndPermissions:
    async def test_two_scopes_never_cross(self, engine):  # AC-A02-03, AC-A02-04
        a = await ops_for(engine, "a")
        b = await ops_for(engine, "b")
        await published(a, "a", "one")
        await a.create_profile("a", "two", "Two", **NO_CATALOG)
        await published(b, "b", "one")  # same id in another scope is a different profile
        await b.create_profile("b", "other", "Other", **NO_CATALOG)
        assert [p.profile_id for p in (await a.list_profiles("a")).items] == ["one", "two"]
        assert [p.profile_id for p in (await b.list_profiles("b")).items] == ["one", "other"]
        for call in (
            lambda: a.get_profile("b", "one"),
            lambda: a.list_profiles("b"),
            lambda: a.create_profile("b", "x", "X"),
            lambda: a.publish_profile("b", "one", expected_draft_revision=1),
            lambda: a.get_profile_version("b", "one", 1),
            lambda: a.resolve_profile("b"),
            lambda: a.get_default_profile("b"),
        ):
            with pytest.raises(ResourceNotAccessibleError):
                await call()
        # inside the scope, a category of ANOTHER profile is indistinguishable from a missing one
        with pytest.raises(ResourceNotFoundError):
            await a.get_category("a", "two", "billing")
        with pytest.raises(ResourceNotFoundError):
            await a.update_category("a", "two", "billing", expected_revision=1, name="Z")
        with pytest.raises(ResourceNotFoundError):
            await a.get_profile_version("a", "two", 1)
        # a's default is independent of b's
        await a.set_default_profile("a", "one", expected_revision=0)
        assert (await b.get_default_profile("b")).profile_id is None

    async def test_permission_matrix(self, engine):  # AC-A02-05
        full = await ops_for(engine, "a")
        await published(full, "a", "p")
        reader = await ops_for(engine, "a", [PR], create_scope=False)
        writer = await ops_for(engine, "a", [PW], create_scope=False)
        scope_only = await ops_for(
            engine, "a", [ScopePermission.SCOPE_READ, ScopePermission.SCOPE_WRITE], create_scope=False
        )
        reads = [
            lambda o: o.get_profile("a", "p"),
            lambda o: o.list_profiles("a"),
            lambda o: o.get_ranking_config("a", "p"),
            lambda o: o.get_category("a", "p", "billing"),
            lambda o: o.list_categories("a", "p"),
            lambda o: o.get_sensitive_type("a", "p", "password"),
            lambda o: o.list_sensitive_types("a", "p"),
            lambda o: o.list_profile_versions("a", "p"),
            lambda o: o.get_profile_version("a", "p", 1),
            lambda o: o.get_default_profile("a"),
            lambda o: o.resolve_profile("a", profile_id="p"),
        ]
        writes = [
            lambda o: o.create_profile("a", "w", "W"),
            lambda o: o.update_profile("a", "p", expected_revision=1, display_name="Q"),
            lambda o: o.disable_profile("a", "p"),
            lambda o: o.enable_profile("a", "p"),
            lambda o: o.delete_profile("a", "p"),
            lambda o: o.update_ranking_config("a", "p", expected_revision=1, tie_tolerance=0.1),
            lambda o: o.create_category("a", "p", "c2", "C2", "d"),
            lambda o: o.update_category("a", "p", "billing", expected_revision=1, name="Q"),
            lambda o: o.disable_category("a", "p", "billing"),
            lambda o: o.create_sensitive_type("a", "p", "t2", "T2", "d"),
            lambda o: o.delete_sensitive_type("a", "p", "password"),
            lambda o: o.publish_profile("a", "p", expected_draft_revision=1),
            lambda o: o.set_default_profile("a", "p", expected_revision=0),
            lambda o: o.clear_default_profile("a", expected_revision=0),
        ]
        for call in reads:
            await call(reader)  # profiles:read reads
            with pytest.raises(PermissionDeniedError):
                await call(writer)  # no write -> read implication
            with pytest.raises(PermissionDeniedError):
                await call(scope_only)  # scope:read/write do not grant profiles:*
        for call in writes:
            with pytest.raises(PermissionDeniedError):
                await call(reader)
            with pytest.raises(PermissionDeniedError):
                await call(scope_only)
        # profiles:write does not read, and an AdminContext has no access to profiles at all
        admin = engine.admin(await admin_ctx(engine))
        assert not hasattr(admin, "create_profile") and not hasattr(admin, "get_profile")

    async def test_grants_delegation_and_pre_existing_keys(self, engine):  # AC-A02-07
        await make_scope(engine, "a")
        await make_scope(engine, "b")
        # a key issued with scope:* only does not gain profiles:* (grants are immutable and never reinterpreted)
        _, old = await issue(engine, "legacy", scopes={"a": ["scope:read", "scope:write"]})
        principal = await engine.authenticate_api_key(old.secret)
        assert principal.scope_grants["a"] == {ScopePermission.SCOPE_READ, ScopePermission.SCOPE_WRITE}
        ctx = principal.scope_context("a")
        with pytest.raises(PermissionDeniedError):
            await engine.scoped(ctx).list_profiles("a")
        # delegation: subset rule without admin:grants:write, any existing scope with it
        _, issuer = await issue(
            engine,
            "issuer",
            admin=[AdminPermission.ADMIN_CLIENTS_WRITE],
            scopes={"a": ["profiles:read", "profiles:write"]},
        )
        ictx = (await engine.authenticate_api_key(issuer.secret)).admin_context()
        ops = engine.admin(ictx)
        client = (await ops.create_client("delegate", "Delegate")).value
        ok = await ops.issue_api_key(
            client.client_id, request_id=str(uuid.uuid4()), label="ro", grants={"scopes": {"a": ["profiles:read"]}}
        )
        assert ok.info.grants.scopes["a"] == {ScopePermission.PROFILES_READ}
        for bad in ({"a": ["scope:read"]}, {"b": ["profiles:read"]}):
            with pytest.raises(PermissionDeniedError):
                await ops.issue_api_key(
                    client.client_id, request_id=str(uuid.uuid4()), label="x", grants={"scopes": bad}
                )
        _, grantor = await issue(
            engine,
            "grantor",
            admin=[AdminPermission.ADMIN_CLIENTS_WRITE, AdminPermission.ADMIN_GRANTS_WRITE],
        )
        gops = engine.admin((await engine.authenticate_api_key(grantor.secret)).admin_context())
        gclient = (await gops.create_client("delegate-b", "B")).value
        assert (
            await gops.issue_api_key(
                gclient.client_id, request_id=str(uuid.uuid4()), label="g", grants={"scopes": {"b": ["profiles:write"]}}
            )
        ).info.grants.scopes["b"] == {ScopePermission.PROFILES_WRITE}
        with pytest.raises(ValidationFailedError):
            await gops.issue_api_key(
                gclient.client_id,
                request_id=str(uuid.uuid4()),
                label="g2",
                grants={"scopes": {"nope": ["profiles:read"]}},
            )

    async def test_disabled_scope_reads_history_but_refuses_writes_and_resolution(self, engine):  # AC-A02-08
        ops = await ops_for(engine, "a")
        await published(ops, "a", "p")
        await ops.set_default_profile("a", "p", expected_revision=0)
        admin = engine.admin(await admin_ctx(engine))
        await admin.set_scope_status("a", "disabled")
        assert (await ops.get_profile("a", "p")).profile_id == "p"
        assert (await ops.get_profile_version("a", "p", 1)).version_number == 1
        assert len((await ops.list_profile_versions("a", "p")).items) == 1
        for call in (
            lambda: ops.create_profile("a", "n", "N"),
            lambda: ops.update_profile("a", "p", expected_revision=1, display_name="Z"),
            lambda: ops.create_category("a", "p", "c", "C", "d"),
            lambda: ops.publish_profile("a", "p", expected_draft_revision=2),
            lambda: ops.disable_profile("a", "p"),
            lambda: ops.clear_default_profile("a", expected_revision=1),
            lambda: ops.resolve_profile("a"),
            lambda: ops.resolve_profile("a", profile_id="p", version_number=1),
        ):
            with pytest.raises(ScopeDisabledError):
                await call()


class TestCrudValidationAndStates:
    async def test_profile_crud_patch_and_idempotent_create(self, engine):  # AC-A02-10, 11, 15
        ops = await ops_for(engine, "a")
        first = await ops.create_profile("a", "p", "Perfil", description="d", language="en", **NO_CATALOG)
        again = await ops.create_profile("a", "p", "Perfil", description="d", language="en", **NO_CATALOG)
        assert first.created and not again.created and again.value == first.value
        for diff in (
            {"display_name": "Otro"},
            {"language": "es"},
            {"description": None},
            {"initial_catalog": "starter"},
        ):
            args = {"display_name": "Perfil", "description": "d", "language": "en", **NO_CATALOG, **diff}
            with pytest.raises(ConflictError):
                await ops.create_profile("a", "p", args.pop("display_name"), **args)
        p = first.value
        assert p.catalog_provenance is None and p.revision == 1
        # omitted != null: description cleared by None, kept when omitted
        p2 = await ops.update_profile("a", "p", expected_revision=1, display_name="Nuevo")
        assert p2.description == "d" and p2.revision == 2 and p2.draft_revision == 2
        p3 = await ops.update_profile("a", "p", expected_revision=2, description=None)
        assert p3.description is None and p3.display_name == "Nuevo"
        with pytest.raises(ValidationFailedError):
            await ops.update_profile("a", "p", expected_revision=3, display_name=None)
        with pytest.raises(ValidationFailedError):
            await ops.update_profile("a", "p", expected_revision=3)
        with pytest.raises(RevisionConflictError):
            await ops.update_profile("a", "p", expected_revision=1, display_name="Nuevo")  # stale, values applied

    @pytest.mark.parametrize(
        "bad",
        ["", "UPPER", "-lead", "x" * 65, "has space", "ñ", "a/b"],
    )
    async def test_id_validation(self, engine, bad):  # AC-A02-10
        ops = await ops_for(engine, "a")
        with pytest.raises(ValidationFailedError):
            await ops.create_profile("a", bad, "N")
        await ops.create_profile("a", "ok", "N", **NO_CATALOG)
        with pytest.raises(ValidationFailedError):
            await ops.create_category("a", "ok", bad, "N", "d")

    async def test_text_examples_translations_and_priority_validation(self, engine):  # AC-A02-10
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "p", "P", language="es", **NO_CATALOG)

        async def cat(cid="c", **kw):
            kw.setdefault("name", "Nombre")
            kw.setdefault("definition", "Definición")
            return await ops.create_category("a", "p", cid, kw.pop("name"), kw.pop("definition"), **kw)

        for bad in (
            {"name": " "},
            {"name": 5},
            {"definition": "x\x00y"},
            {"name": "a\tb"},
            {"examples": {"positive": "x"}},
            {"examples": {"other": []}},
            {"examples": {"positive": [""]}},
            {"examples": {"positive": [1]}},
            {"translations": {"es": {"name": "a", "definition": "b"}}},  # same language as the profile
            {"translations": {"en": {"name": "a"}}},
            {"translations": {"en": {"name": "a", "definition": "b", "x": "c"}}},
            {"translations": {}},
            {"priority": 0},
            {"priority": 10001},
            {"priority": True},
            {"priority": "1"},
        ):
            with pytest.raises(ValidationFailedError):
                await cat(**bad)
        for too_big in (
            {"name": "n" * 101},
            {"definition": "d" * 1501},
            {"examples": {"positive": ["e"] * 6}},
            {"examples": {"negative": ["e" * 301]}},
            {"definition": ("€" * 1500), "examples": {"positive": ["€" * 300] * 5}},  # total bytes > 8 KiB
        ):
            with pytest.raises(LimitExceededError):
                await cat(**too_big)
        ok = (
            await cat(
                examples={"positive": ["uno"], "negative": ["dos"]},
                translations={"en": {"name": "Name", "definition": "Definition"}},
                priority=3,
                definition="Línea 1\nLínea 2",
            )
        ).value
        assert ok.examples.positive == ("uno",) and ok.translations["en"].name == "Name" and ok.priority == 3
        # no translation is required
        assert (await cat("c2", name="Otro", definition="d")).value.translations is None
        # NFC + trimmed
        assert (await cat("c3", name="  Café  ", definition="d")).value.name == "Café"

    async def test_element_patch_semantics_and_conflicts(self, engine):  # AC-A02-11, 15
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "p", "P", **NO_CATALOG)
        c1 = (await ops.create_category("a", "p", "c1", "Alpha", "d", priority=1)).value
        await ops.create_category("a", "p", "c2", "Beta", "d", priority=2)
        with pytest.raises(NameConflictError):
            await ops.create_category("a", "p", "c3", "  alpha ", "d")  # NFKC + casefold
        with pytest.raises(PriorityConflictError):
            await ops.create_category("a", "p", "c3", "Gamma", "d", priority=2)
        with pytest.raises(NameConflictError):
            await ops.update_category("a", "p", "c1", expected_revision=1, name="BETA")
        with pytest.raises(PriorityConflictError):
            await ops.update_category("a", "p", "c1", expected_revision=1, priority=2)
        up = await ops.update_category("a", "p", "c1", expected_revision=1, priority=None, definition="nueva")
        assert up.priority is None and up.revision == 2 and up.definition == "nueva" and up.name == "Alpha"
        for bad in ({"name": None}, {"definition": None}, {"state": "enabled"}, {}):
            with pytest.raises((ValidationFailedError, TypeError)):
                await ops.update_category("a", "p", "c1", expected_revision=2, **bad)
        # translations null clears; omitted keeps
        await ops.update_category(
            "a", "p", "c1", expected_revision=2, translations={"en": {"name": "A", "definition": "B"}}
        )
        assert (await ops.update_category("a", "p", "c1", expected_revision=3, name="Alpha2")).translations
        assert (await ops.update_category("a", "p", "c1", expected_revision=4, translations=None)).translations is None
        # idempotent create by natural key; different attributes or a reserved (deleted) id conflict
        again = await ops.create_category("a", "p", "c2", "Beta", "d", priority=2)
        assert not again.created
        with pytest.raises(ConflictError):
            await ops.create_category("a", "p", "c2", "Beta", "otra", priority=2)
        await ops.delete_category("a", "p", "c2")
        with pytest.raises(ConflictError):
            await ops.create_category("a", "p", "c2", "Beta", "d", priority=2)
        # the name of the deleted element is free again; its priority too (only enabled categories count)
        assert (await ops.create_category("a", "p", "c9", "Beta", "d", priority=2)).created
        assert c1.priority == 1

    async def test_state_machine_filters_and_deleted_visibility(self, engine):  # AC-A02-12, 13, 14
        ops = await ops_for(engine, "a")
        await published(ops, "a", "keep")
        await ops.create_profile("a", "p", "P", **NO_CATALOG)
        await ops.create_category("a", "p", "c", "C", "d")
        await ops.create_sensitive_type("a", "p", "t", "T", "d")
        # profile: active -> disabled -> active; delete is terminal; repeated target states are no-ops
        d = await ops.disable_profile("a", "p")
        assert d.status == "disabled" and d.revision == 2
        assert (await ops.disable_profile("a", "p")).revision == 2
        for call in (
            lambda: ops.create_category("a", "p", "c2", "C2", "d"),
            lambda: ops.update_profile("a", "p", expected_revision=2, display_name="Z"),
            lambda: ops.publish_profile("a", "p", expected_draft_revision=3),
            lambda: ops.disable_category("a", "p", "c"),
        ):
            with pytest.raises(ProfileDisabledError):
                await call()
        assert (await ops.get_category("a", "p", "c")).state == "enabled"  # reads work
        assert (await ops.enable_profile("a", "p")).status == "active"
        deleted = await ops.delete_profile("a", "p")
        assert deleted.status == "deleted" and deleted.deleted_at is not None
        assert (await ops.delete_profile("a", "p")).revision == deleted.revision
        for call in (lambda: ops.enable_profile("a", "p"), lambda: ops.disable_profile("a", "p")):
            with pytest.raises(InvalidStateTransitionError):
                await call()
        with pytest.raises(ResourceNotFoundError):
            await ops.get_profile("a", "p")
        assert (await ops.get_profile("a", "p", include_deleted=True)).status == "deleted"
        assert [x.profile_id for x in (await ops.list_profiles("a")).items] == ["keep"]
        assert [x.profile_id for x in (await ops.list_profiles("a", status=["deleted"])).items] == ["p"]
        assert [x.profile_id for x in (await ops.list_profiles("a", status="active,disabled".split(","))).items] == [
            "keep"
        ]
        with pytest.raises(ValidationFailedError):
            await ops.list_profiles("a", status=["nope"])
        for call in (
            lambda: ops.create_category("a", "p", "c3", "X", "d"),
            lambda: ops.list_categories("a", "p"),
            lambda: ops.update_profile("a", "p", expected_revision=3, display_name="Z"),
        ):
            with pytest.raises(ResourceNotFoundError):
                await call()
        with pytest.raises(ConflictError):  # the id stays reserved
            await ops.create_profile("a", "p", "P", **NO_CATALOG)
        # elements: enabled <-> disabled, delete terminal and logical
        await ops.create_profile("a", "q", "Q", **NO_CATALOG)
        await ops.create_category("a", "q", "c", "C", "d")
        assert (await ops.disable_category("a", "q", "c")).state == "disabled"
        assert (await ops.disable_category("a", "q", "c")).revision == 2
        assert (await ops.enable_category("a", "q", "c")).state == "enabled"
        await ops.delete_category("a", "q", "c")
        with pytest.raises(InvalidStateTransitionError):
            await ops.enable_category("a", "q", "c")
        with pytest.raises(ResourceNotFoundError):
            await ops.get_category("a", "q", "c")
        assert (await ops.get_category("a", "q", "c", include_deleted=True)).state == "deleted"
        assert (await ops.list_categories("a", "q")).items == []
        assert [c.category_id for c in (await ops.list_categories("a", "q", state=["deleted"])).items] == ["c"]
        with pytest.raises(ResourceNotFoundError):
            await ops.update_category("a", "q", "c", expected_revision=4, name="Z")

    async def test_enabling_a_category_cannot_break_priority_uniqueness(self, engine):  # AC-A02-15
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "p", "P", **NO_CATALOG)
        await ops.create_category("a", "p", "a", "A", "d", priority=1)
        await ops.disable_category("a", "p", "a")
        await ops.create_category("a", "p", "b", "B", "d", priority=1)  # free while 'a' is disabled
        with pytest.raises(PriorityConflictError):
            await ops.enable_category("a", "p", "a")

    async def test_pagination_cursor(self, engine):  # AC-A02-10
        ops = await ops_for(engine, "a")
        for n in range(5):
            await ops.create_profile("a", f"p{n}", f"P{n}", **NO_CATALOG)
        seen, cursor = [], None
        while True:
            page = await ops.list_profiles("a", limit=2, cursor=cursor)
            seen += [p.profile_id for p in page.items]
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == [f"p{n}" for n in range(5)]
        with pytest.raises(ValidationFailedError):
            await ops.list_profiles("a", limit=0)
        with pytest.raises(ValidationFailedError):
            await ops.list_profiles("a", cursor="@@@")

    async def test_limits(self, migrated):  # AC-A02-30
        from cawbase_content_engine import EngineConfig

        _, url = migrated
        limits = Limits(
            max_profiles_per_scope=2,
            max_profile_rows_per_scope=3,
            max_elements_per_profile=2,
            max_element_rows_per_profile=3,
            max_versions_per_profile=2,
            max_snapshot_bytes=1200,
        )
        async with ContentEngine(EngineConfig(database_url=url, limits=limits)) as e:
            ops = await ops_for(e, "a")
            await ops.create_profile("a", "p1", "P", **NO_CATALOG)
            await ops.create_profile("a", "p2", "P", **NO_CATALOG)
            with pytest.raises(LimitExceededError):
                await ops.create_profile("a", "p3", "P", **NO_CATALOG)
            # a valid retry of an existing profile is never blocked by the limit
            assert not (await ops.create_profile("a", "p1", "P", **NO_CATALOG)).created
            await ops.delete_profile("a", "p2")
            await ops.create_profile("a", "p3", "P", **NO_CATALOG)  # live count 2 again, rows 3
            with pytest.raises(LimitExceededError):  # total rows (deleted ones count) reached
                await ops.create_profile("a", "p4", "P", **NO_CATALOG)
            await ops.create_category("a", "p1", "c1", "C1", "d")
            await ops.create_category("a", "p1", "c2", "C2", "d")
            with pytest.raises(LimitExceededError):
                await ops.create_category("a", "p1", "c3", "C3", "d")
            assert not (await ops.create_category("a", "p1", "c1", "C1", "d")).created  # retry at the limit
            await ops.update_category("a", "p1", "c1", expected_revision=1, definition="x" * 1400)
            p = await ops.get_profile("a", "p1")
            with pytest.raises(LimitExceededError):  # snapshot > 1200 bytes
                await ops.publish_profile("a", "p1", expected_draft_revision=p.draft_revision)
            await ops.update_category("a", "p1", "c1", expected_revision=2, definition="x")
            p = await ops.get_profile("a", "p1")
            v1 = await ops.publish_profile("a", "p1", expected_draft_revision=p.draft_revision)
            await ops.update_category("a", "p1", "c1", expected_revision=3, definition="y")
            p = await ops.get_profile("a", "p1")
            v2 = await ops.publish_profile("a", "p1", expected_draft_revision=p.draft_revision)
            assert (v1.value.version_number, v2.value.version_number) == (1, 2)
            # at the version limit, republishing an unchanged configuration still returns the current version
            same = await ops.publish_profile("a", "p1", expected_draft_revision=p.draft_revision)
            assert not same.created and same.value == v2.value
            await ops.update_category("a", "p1", "c1", expected_revision=4, definition="z")
            p = await ops.get_profile("a", "p1")
            with pytest.raises(LimitExceededError):  # versions per profile
                await ops.publish_profile("a", "p1", expected_draft_revision=p.draft_revision)


class TestRankingConfig:
    async def test_defaults_and_validation(self, engine):  # AC-A02-29
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "p", "P", **NO_CATALOG)
        cfg = await ops.get_ranking_config("a", "p")
        assert (cfg.mode, cfg.eligibility_threshold, cfg.tie_tolerance, cfg.revision) == ("ranking", 0.0, 0.0, 1)
        for ok in (0, 1, 0.0, 1.0, 0.5):
            r = await ops.update_ranking_config(
                "a", "p", expected_revision=(await ops.get_ranking_config("a", "p")).revision, eligibility_threshold=ok
            )
            assert isinstance(r.eligibility_threshold, float) and r.eligibility_threshold == float(ok)
        rev = (await ops.get_ranking_config("a", "p")).revision
        for bad in (-0.1, 1.1, float("nan"), float("inf"), -float("inf"), True, "0.5", None, [0.1]):
            with pytest.raises(ValidationFailedError):
                await ops.update_ranking_config("a", "p", expected_revision=rev, tie_tolerance=bad)
        with pytest.raises(ValidationFailedError):
            await ops.update_ranking_config("a", "p", expected_revision=rev, mode="winner")
        with pytest.raises(ValidationFailedError):
            await ops.update_ranking_config("a", "p", expected_revision=rev)
        with pytest.raises(RevisionConflictError):
            await ops.update_ranking_config("a", "p", expected_revision=rev - 1, tie_tolerance=0.2)

    async def test_resolution_requires_unique_priorities_ranking_does_not(self, engine):  # AC-A02-28
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "p", "P", **NO_CATALOG)
        with pytest.raises(ProfileInvalidError) as e:  # no categories at all
            await ops.update_ranking_config("a", "p", expected_revision=1, mode="resolution")
        assert e.value.problems == [{"code": "resolution_requires_categories"}]
        await ops.create_category("a", "p", "a", "A", "d")
        await ops.create_category("a", "p", "b", "B", "d", priority=2)
        with pytest.raises(ProfileInvalidError) as e:
            await ops.update_ranking_config("a", "p", expected_revision=1, mode="resolution")
        assert e.value.problems == [{"code": "priority_missing", "category_id": "a"}]
        # ranking publishes without priorities
        p = await ops.get_profile("a", "p")
        assert (await ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision)).created
        await ops.update_category("a", "p", "a", expected_revision=1, priority=1)
        cfg = await ops.update_ranking_config("a", "p", expected_revision=1, mode="resolution", tie_tolerance=0.1)
        assert cfg.mode == "resolution"
        # the draft may become temporarily invalid (swap through null); publishing refuses it
        await ops.update_category("a", "p", "a", expected_revision=2, priority=None)
        p = await ops.get_profile("a", "p")
        with pytest.raises(ProfileInvalidError) as e:
            await ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision)
        assert e.value.problems == [{"code": "priority_missing", "category_id": "a"}]
        assert "A" not in json.dumps(e.value.problems)  # codes and ids only
        await ops.update_category("a", "p", "b", expected_revision=1, priority=1)
        await ops.update_category("a", "p", "a", expected_revision=3, priority=2)
        p = await ops.get_profile("a", "p")
        v = (await ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision)).value
        assert v.snapshot["ranking"] == {"mode": "resolution", "eligibility_threshold": 0.0, "tie_tolerance": 0.1}
        assert {c["category_id"]: c["priority"] for c in v.snapshot["categories"]} == {"a": 2, "b": 1}


class TestVersions:
    async def test_snapshot_is_self_contained_and_immutable(self, engine):  # AC-A02-20, 21
        ops = await ops_for(engine, "a")
        prof, v1 = await published(ops, "a", "p")
        raw = canonical(v1.snapshot)
        assert hashlib.sha256(raw).hexdigest() == v1.content_hash and v1.snapshot_bytes == len(raw)
        snap = v1.snapshot
        assert snap["snapshot_schema_version"] == 1 and snap["profile"]["profile_id"] == "p"
        assert [c["category_id"] for c in snap["categories"]] == ["billing"]
        assert {s["type_id"] for s in snap["sensitive_types"]} == {s.entry for s in starter_catalog().entries}
        assert snap["catalog_provenance"]["catalog_version"] == 1
        assert all(c["source_revision"] == 1 for c in snap["categories"])
        # edit everything, disable and delete elements and the profile: the published version never changes
        await ops.update_profile("a", "p", expected_revision=prof.revision, display_name="Cambiado")
        await ops.update_category("a", "p", "billing", expected_revision=1, definition="otra", name="Otro")
        await ops.update_ranking_config("a", "p", expected_revision=1, tie_tolerance=0.3)
        await ops.delete_category("a", "p", "billing")
        await ops.disable_sensitive_type("a", "p", "password")
        await ops.delete_sensitive_type("a", "p", "personal_data")
        await ops.delete_profile("a", "p")
        again = await ops.get_profile_version("a", "p", 1)
        assert again == v1 and again.content_hash == v1.content_hash
        assert (await ops.list_profile_versions("a", "p")).items[0].version_id == v1.version_id

    async def test_effective_comparison_cases_a_to_e(self, engine):  # AC-A02-25
        ops = await ops_for(engine, "a")
        prof, v1 = await published(ops, "a", "p")
        draft = prof.draft_revision
        # (a) publish again without editing
        a = await ops.publish_profile("a", "p", expected_draft_revision=draft)
        assert not a.created and a.value == v1
        # (c) change only a DISABLED element: draft_revision moves, the effective configuration does not
        await ops.create_category("a", "p", "dormant", "Dormant", "d")
        await ops.disable_category("a", "p", "dormant")
        # that sequence changed the enabled set transiently, so publish to a baseline first
        p = await ops.get_profile("a", "p")
        v2 = (await ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision)).value
        assert v2.version_number == 1 or v2.version_number == 2
        base = await ops.get_profile("a", "p")
        before = base.draft_revision
        await ops.update_category("a", "p", "dormant", expected_revision=2, definition="solo desactivada")
        mid = await ops.get_profile("a", "p")
        assert mid.draft_revision == before + 1
        c = await ops.publish_profile("a", "p", expected_draft_revision=mid.draft_revision)
        assert not c.created and c.value.version_id == v2.version_id
        assert (
            c.value.source_draft_revision == v2.source_draft_revision < mid.draft_revision
        )  # not the current revision
        # (b) edit a definition and restore it before publishing
        orig = (await ops.get_category("a", "p", "billing")).definition
        await ops.update_category("a", "p", "billing", expected_revision=1, definition="temporal")
        await ops.update_category("a", "p", "billing", expected_revision=2, definition=orig)
        cur = await ops.get_profile("a", "p")
        b = await ops.publish_profile("a", "p", expected_draft_revision=cur.draft_revision)
        assert not b.created and b.value.version_id == v2.version_id
        assert b.value.snapshot == v2.snapshot  # untouched, original source revisions preserved
        assert (await ops.get_profile("a", "p")).current_version_number == v2.version_number
        # (d) really change an enabled element
        await ops.update_category("a", "p", "billing", expected_revision=3, definition="distinta")
        cur = await ops.get_profile("a", "p")
        d = await ops.publish_profile("a", "p", expected_draft_revision=cur.draft_revision)
        assert d.created and d.value.version_number == v2.version_number + 1
        assert d.value.source_draft_revision == cur.draft_revision
        assert d.value.content_hash != v2.content_hash
        # the untouched element keeps its original source_revision in the new complete snapshot
        types = {s["type_id"]: s["source_revision"] for s in d.value.snapshot["sensitive_types"]}
        assert set(types.values()) == {1}
        # (e) stale expected_draft_revision -> 412 even though the configuration coincides
        with pytest.raises(RevisionConflictError):
            await ops.publish_profile("a", "p", expected_draft_revision=cur.draft_revision - 1)
        with pytest.raises(RevisionConflictError):
            await ops.publish_profile("a", "p", expected_draft_revision=cur.draft_revision + 5)
        # a unique source revision per version is enforced by the database as well
        async with engine._engine.connect() as conn:  # type: ignore[union-attr]
            rows = (await conn.execute(sa.select(engine.tables.profile_versions.c.source_draft_revision))).all()
        assert len({r[0] for r in rows}) == len(rows) == 2

    async def test_projection_ignores_revisions_provenance_and_timestamps(self, engine):  # AC-A02-25
        ops = await ops_for(engine, "a")
        _, v1 = await published(ops, "a", "p")
        snap = json.loads(json.dumps(v1.snapshot))
        snap["categories"][0]["source_revision"] = 99
        snap["catalog_provenance"]["copied_at"] = "2000-01-01T00:00:00.000000Z"
        for s in snap["sensitive_types"]:
            s["origin_entry"] = None
        assert effective_projection(snap) == effective_projection(v1.snapshot)
        assert canonical(snap) != canonical(v1.snapshot)
        snap["categories"][0]["definition"] += "!"
        assert effective_projection(snap) != effective_projection(v1.snapshot)

    async def test_publishable_and_unpublishable_configurations(self, engine):  # AC-A02-22
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "empty", "Vacío", **NO_CATALOG)
        with pytest.raises(ProfileInvalidError) as e:
            await ops.publish_profile("a", "empty", expected_draft_revision=1)
        assert e.value.problems == [{"code": "no_enabled_elements"}]
        # detection only: starter catalog, no categories
        await ops.create_profile("a", "detect", "Detección")
        v = (await ops.publish_profile("a", "detect", expected_draft_revision=1)).value
        assert v.capabilities == {"classification": False, "detection": True} and v.snapshot["categories"] == []
        # classification only
        await ops.create_profile("a", "classify", "Clasificación", **NO_CATALOG)
        await ops.create_category("a", "classify", "c", "C", "d")
        p = await ops.get_profile("a", "classify")
        v = (await ops.publish_profile("a", "classify", expected_draft_revision=p.draft_revision)).value
        assert v.capabilities == {"classification": True, "detection": False} and v.snapshot["sensitive_types"] == []
        # disabling every element makes a new version unpublishable
        await ops.disable_category("a", "classify", "c")
        p = await ops.get_profile("a", "classify")
        with pytest.raises(ProfileInvalidError):
            await ops.publish_profile("a", "classify", expected_draft_revision=p.draft_revision)

    async def test_version_listing_and_lookup(self, engine):  # AC-A02-21
        ops = await ops_for(engine, "a")
        _, v1 = await published(ops, "a", "p")
        for n in range(2, 5):
            await ops.update_category("a", "p", "billing", expected_revision=n - 1, definition=f"v{n}")
            p = await ops.get_profile("a", "p")
            assert (
                await ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision)
            ).value.version_number == n
        page = await ops.list_profile_versions("a", "p", limit=3)
        assert [x.version_number for x in page.items] == [4, 3, 2] and page.next_cursor
        rest = await ops.list_profile_versions("a", "p", limit=3, cursor=page.next_cursor)
        assert [x.version_number for x in rest.items] == [1] and rest.next_cursor is None
        assert not hasattr(page.items[0], "snapshot")
        with pytest.raises(ResourceNotFoundError):
            await ops.get_profile_version("a", "p", 9)
        assert len({x.version_id for x in page.items + rest.items}) == 4


class TestDefaultAndResolution:
    async def test_default_lifecycle(self, engine):  # AC-A02-16, 17, 18
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "draft", "Draft")  # never published
        await published(ops, "a", "one")
        await published(ops, "a", "two")
        d0 = await ops.get_default_profile("a")
        assert (d0.profile_id, d0.revision, d0.updated_at) == (None, 0, None)
        with pytest.raises(ProfileNotPublishedError):
            await ops.set_default_profile("a", "draft", expected_revision=0)
        with pytest.raises(ResourceNotFoundError):
            await ops.set_default_profile("a", "nope", expected_revision=0)
        with pytest.raises(RevisionConflictError):
            await ops.set_default_profile("a", "one", expected_revision=1)
        d1 = await ops.set_default_profile("a", "one", expected_revision=0)
        assert (d1.profile_id, d1.revision) == ("one", 1)
        assert (await ops.set_default_profile("a", "one", expected_revision=1)).revision == 1  # no-op
        with pytest.raises(RevisionConflictError):
            await ops.set_default_profile("a", "two", expected_revision=0)  # lost-update protection
        # a default cannot be disabled or deleted until it is replaced or cleared explicitly
        for call in (lambda: ops.disable_profile("a", "one"), lambda: ops.delete_profile("a", "one")):
            with pytest.raises(ProfileIsDefaultError):
                await call()
        d2 = await ops.set_default_profile("a", "two", expected_revision=1)
        assert (d2.profile_id, d2.revision) == ("two", 2)
        assert (await ops.disable_profile("a", "one")).status == "disabled"
        with pytest.raises(ProfileNotSelectableError):
            await ops.set_default_profile("a", "one", expected_revision=2)
        cleared = await ops.clear_default_profile("a", expected_revision=2)
        assert (cleared.profile_id, cleared.revision) == (None, 3)
        assert (await ops.clear_default_profile("a", expected_revision=3)).revision == 3  # no-op
        with pytest.raises(RevisionConflictError):
            await ops.clear_default_profile("a", expected_revision=2)
        assert (await ops.delete_profile("a", "two")).status == "deleted"
        with pytest.raises(DefaultProfileNotSetError):
            await ops.resolve_profile("a")

    async def test_resolution_rules(self, engine):  # AC-A02-18
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "draft", "Draft")
        _, v1 = await published(ops, "a", "p")
        await ops.update_category("a", "p", "billing", expected_revision=1, definition="v2")
        p = await ops.get_profile("a", "p")
        v2 = (await ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision)).value
        r = await ops.resolve_profile("a", profile_id="p")
        assert (r.selection, r.version.version_number) == ("explicit", 2)
        assert (await ops.resolve_profile("a", profile_id="p", version_number=1)).version == v1
        with pytest.raises(ValidationFailedError):
            await ops.resolve_profile("a", version_number=1)
        with pytest.raises(ProfileNotPublishedError):
            await ops.resolve_profile("a", profile_id="draft")
        with pytest.raises(ResourceNotFoundError):
            await ops.resolve_profile("a", profile_id="p", version_number=7)
        with pytest.raises(ResourceNotFoundError):
            await ops.resolve_profile("a", profile_id="nope")
        await ops.disable_profile("a", "p")
        with pytest.raises(ProfileNotSelectableError):
            await ops.resolve_profile("a", profile_id="p")
        with pytest.raises(ProfileNotSelectableError):  # even with an explicit version
            await ops.resolve_profile("a", profile_id="p", version_number=1)
        assert (await ops.get_profile_version("a", "p", 1)).version_id == v1.version_id  # history stays readable
        await ops.enable_profile("a", "p")
        assert (await ops.resolve_profile("a", profile_id="p")).version.version_id == v2.version_id
        await ops.delete_profile("a", "p")
        with pytest.raises(ProfileNotSelectableError):
            await ops.resolve_profile("a", profile_id="p", version_number=2)
        assert (await ops.get_profile_version("a", "p", 2)).version_id == v2.version_id


class TestStarterCatalog:
    def test_catalog_is_pure_data_with_six_synthetic_entries(self):  # AC-A02-33
        cat = starter_catalog()
        assert (cat.catalog_id, cat.catalog_version) == ("cce-starter-sensitive-types", 1)
        assert [e.entry for e in cat.entries] == [
            "personal_data",
            "government_id",
            "salary_compensation",
            "financial_information",
            "password",
            "api_key_secret",
        ]
        lim = Limits()
        for e in cat.entries:
            for lang in ("es", "en"):
                assert 0 < len(e.names[lang]) <= lim.max_element_name
                assert 0 < len(e.definitions[lang]) <= lim.max_element_definition
                assert e.examples[lang].positive and e.examples[lang].negative
        text = json.dumps(cat.model_dump(mode="json"))
        assert "example.test" in text and "NOT_A_REAL" in text or "NO_REAL" in text
        pw = next(e for e in cat.entries if e.entry == "password")
        # password / api_key_secret are separated by function and context, not by who generated the secret
        assert "gestor" in pw.definitions["es"] and "password manager" in pw.definitions["en"]
        assert "no en quién generó" in pw.definitions["es"] and "not who generated" in pw.definitions["en"]
        api = next(e for e in cat.entries if e.entry == "api_key_secret")
        assert (
            "sin importar su origen" in api.definitions["es"] and "regardless of their origin" in api.definitions["en"]
        )

    async def test_profile_creation_copies_with_provenance_and_empty_starts_clean(self, engine):  # AC-A02-31
        ops = await ops_for(engine, "a")
        es = (await ops.create_profile("a", "es", "ES", language="es")).value
        await ops.create_profile("a", "en", "EN", language="en")
        empty = (await ops.create_profile("a", "empty", "E", **NO_CATALOG)).value
        assert es.catalog_provenance and es.catalog_provenance.catalog_id == "cce-starter-sensitive-types"
        assert empty.catalog_provenance is None
        assert (await ops.list_categories("a", "es")).items == []  # thematic categories start empty
        types_es = (await ops.list_sensitive_types("a", "es")).items
        assert len(types_es) == 6 and all(t.origin_entry == t.type_id and t.state == "enabled" for t in types_es)
        pw_es = next(t for t in types_es if t.type_id == "password")
        pw_en = next(t for t in (await ops.list_sensitive_types("a", "en")).items if t.type_id == "password")
        assert pw_es.name == "Contraseñas" and pw_es.translations["en"].name == "Passwords"
        assert pw_en.name == "Passwords" and pw_en.translations["es"].name == "Contraseñas"
        assert (await ops.list_sensitive_types("a", "empty")).items == []
        assert len(await events(engine, "profile.created")) == 3  # one event per profile, not per copied type
        assert await events(engine, "sensitive_type.") == []

    async def test_catalog_update_never_mutates_existing_profiles_or_versions(self, engine, monkeypatch):  # AC-A02-32
        ops = await ops_for(engine, "a")
        _, v1 = await published(ops, "a", "old")
        before = (await ops.list_sensitive_types("a", "old")).items
        from cawbase_content_engine import catalog as cat

        monkeypatch.setattr(cat, "CATALOG_VERSION", 2)
        patched = {k: dict(v) for k, v in cat._RAW.items()}  # noqa: SLF001
        patched["password"] = {**patched["password"], "names": {"es": "CAMBIADO", "en": "CHANGED"}}
        monkeypatch.setattr(cat, "_RAW", patched)
        newer = (await ops.create_profile("a", "new", "New")).value
        assert newer.catalog_provenance.catalog_version == 2
        assert (await ops.get_sensitive_type("a", "new", "password")).name == "CAMBIADO"
        assert (await ops.list_sensitive_types("a", "old")).items == before  # existing profile untouched
        assert (await ops.get_profile("a", "old")).catalog_provenance.catalog_version == 1
        assert await ops.get_profile_version("a", "old", 1) == v1

    async def test_copied_types_are_ordinary_editable_types(self, engine):  # AC-A02-33
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "p", "P")
        t = await ops.update_sensitive_type("a", "p", "password", expected_revision=1, definition="mía")
        assert t.definition == "mía" and t.origin_entry == "password"
        await ops.disable_sensitive_type("a", "p", "government_id")
        await ops.delete_sensitive_type("a", "p", "salary_compensation")
        assert len((await ops.list_sensitive_types("a", "p")).items) == 5
        with pytest.raises(TypeError):  # sensitive types have no priority (HTTP: unknown field -> 422)
            await ops.update_sensitive_type("a", "p", "password", expected_revision=2, priority=1)  # type: ignore[call-arg]


class TestNoEffectiveChange:
    async def test_noops_write_nothing_bump_nothing_emit_nothing(self, engine):  # AC-A02-23, 24, 36
        ops = await ops_for(engine, "a")
        prof, v1 = await published(ops, "a", "p")
        await ops.set_default_profile("a", "p", expected_revision=0)
        prof = await ops.get_profile("a", "p")
        cat = await ops.get_category("a", "p", "billing")
        typ = await ops.get_sensitive_type("a", "p", "password")
        cfg = await ops.get_ranking_config("a", "p")
        default = await ops.get_default_profile("a")
        n_events = len(await events(engine))
        n_versions = await count_rows(engine, "profile_versions")

        async def snapshot_state():
            return (
                await ops.get_profile("a", "p"),
                await ops.get_category("a", "p", "billing"),
                await ops.get_sensitive_type("a", "p", "password"),
                await ops.get_ranking_config("a", "p"),
                await ops.get_default_profile("a"),
            )

        before = await snapshot_state()
        assert (
            await ops.update_profile("a", "p", expected_revision=prof.revision, display_name=prof.display_name)
        ) == prof
        assert (
            await ops.update_category("a", "p", "billing", expected_revision=cat.revision, name=cat.name, priority=1)
        ) == cat
        assert (
            await ops.update_sensitive_type("a", "p", "password", expected_revision=typ.revision, name=typ.name)
        ) == typ
        assert (await ops.update_ranking_config("a", "p", expected_revision=cfg.revision, mode="ranking")) == cfg
        assert (await ops.enable_category("a", "p", "billing")) == cat
        assert (await ops.enable_profile("a", "p")) == prof
        assert (await ops.set_default_profile("a", "p", expected_revision=default.revision)) == default
        assert (
            await ops.create_category("a", "p", "billing", "Billing", "Invoices and payments.", priority=1)
        ).value == cat
        assert not (await ops.create_profile("a", "p", "P")).created  # idempotent create: no write, no event
        again = await ops.publish_profile("a", "p", expected_draft_revision=prof.draft_revision)
        assert not again.created and again.value == v1
        assert await snapshot_state() == before  # revisions, draft_revision and updated_at untouched
        assert len(await events(engine)) == n_events and await count_rows(engine, "profile_versions") == n_versions
        # a stale revision is rejected even though the values are already applied
        with pytest.raises(RevisionConflictError):
            await ops.update_profile("a", "p", expected_revision=prof.revision + 1, display_name=prof.display_name)
        with pytest.raises(RevisionConflictError):
            await ops.update_category("a", "p", "billing", expected_revision=cat.revision + 1, name=cat.name)
        # an effective change bumps exactly once, and only the right counters
        up = await ops.update_category("a", "p", "billing", expected_revision=cat.revision, name="Billing 2")
        after = await ops.get_profile("a", "p")
        assert up.revision == cat.revision + 1 and after.draft_revision == prof.draft_revision + 1
        assert after.revision == prof.revision and after.ranking.revision == prof.ranking.revision

    async def test_revision_counters_move_only_where_documented(self, engine):  # AC-A02-24
        ops = await ops_for(engine, "a")
        prof, _ = await published(ops, "a", "p")
        await ops.set_default_profile("a", "p", expected_revision=0)
        after_default = await ops.get_profile("a", "p")
        assert (after_default.revision, after_default.draft_revision) == (prof.revision, prof.draft_revision)
        await ops.update_ranking_config("a", "p", expected_revision=1, tie_tolerance=0.2)
        r = await ops.get_profile("a", "p")
        assert (r.revision, r.draft_revision, r.ranking.revision) == (1, prof.draft_revision + 1, 2)
        await ops.update_profile("a", "p", expected_revision=1, description="x")
        r = await ops.get_profile("a", "p")
        assert (r.revision, r.draft_revision) == (2, prof.draft_revision + 2)
        await ops.set_default_profile("a", "p", expected_revision=1)
        await ops.clear_default_profile("a", expected_revision=1)
        await ops.disable_profile("a", "p")  # status change: revision only
        r2 = await ops.get_profile("a", "p")
        assert (r2.revision, r2.draft_revision) == (3, r.draft_revision)
        p = await ops.enable_profile("a", "p")
        pub = await ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision)
        assert (
            pub.created and (await ops.get_profile("a", "p")).draft_revision == p.draft_revision
        )  # publishing does not bump


class TestTransactions:
    async def test_own_transaction_rolls_back_without_partial_writes(self, engine, monkeypatch):  # AC-A02-40
        ops = await ops_for(engine, "a")
        from cawbase_content_engine.operations import profiles as profiles_module

        async def boom(*a, **k):
            raise RuntimeError("after the inserts")

        monkeypatch.setattr(profiles_module, "event", boom)
        with pytest.raises(Exception):  # noqa: B017 - InternalError after translation
            await ops.create_profile("a", "p", "P")  # profile + 6 copied types, then the event fails
        monkeypatch.undo()
        assert await count_rows(engine, "profiles") == 0 and await count_rows(engine, "sensitive_types") == 0
        assert (await ops.create_profile("a", "p", "P")).created

    async def test_host_transaction_commit_rollback_and_recoverable_errors(self, migrated):  # AC-A02-41
        from cawbase_content_engine import EngineConfig

        backend, url = migrated
        host = await make_host_engine(backend, url)
        try:
            async with ContentEngine(EngineConfig(async_engine=host)) as engine:
                ops_own = await ops_for(engine, "a")
                await published(ops_own, "a", "base")

                async def hops(conn):
                    return engine.scoped(engine.trusted_scope_context("a", [PR, PW]), HostTransaction(conn))

                class Boom(Exception):
                    pass

                # host rollback reverts everything A02 did, savepoints included
                with pytest.raises(Boom):
                    async with host.begin() as conn:
                        h = await hops(conn)
                        await h.create_profile("a", "ghost", "Ghost")
                        await h.create_category("a", "ghost", "c", "C", "d")
                        raise Boom
                with pytest.raises(ResourceNotFoundError):
                    await ops_own.get_profile("a", "ghost")
                # host commit persists, and a recoverable error only rolls back its own savepoint
                async with host.begin() as conn:
                    h = await hops(conn)
                    await h.create_profile("a", "kept", "Kept", **NO_CATALOG)
                    await h.create_category("a", "kept", "c1", "C1", "d", priority=1)
                    with pytest.raises(PriorityConflictError):
                        await h.create_category("a", "kept", "c2", "C2", "d", priority=1)
                    with pytest.raises(NameConflictError):
                        await h.create_category("a", "kept", "c3", "C1", "d")
                    await h.create_category("a", "kept", "c4", "C4", "d", priority=2)  # transaction still healthy
                    # visible inside the host transaction, invisible outside until commit
                    assert len((await h.list_categories("a", "kept")).items) == 2
                assert [c.category_id for c in (await ops_own.list_categories("a", "kept")).items] == ["c1", "c4"]
                # one DB error inside the savepoint does not abort the outer transaction either
                async with host.begin() as conn:
                    h = await hops(conn)
                    with pytest.raises(RevisionConflictError):
                        await h.update_profile("a", "kept", expected_revision=9, display_name="X")
                    assert (await h.get_profile("a", "kept")).display_name == "Kept"
                with pytest.raises(TransactionBrokenError):
                    async with host.begin() as conn:
                        h = await hops(conn)
                        await conn.invalidate()
                        await h.get_profile("a", "kept")
        finally:
            await host.dispose()


class TestObservability:
    async def test_no_content_in_events_errors_or_logs(self, engine, caplog):  # AC-A02-50, 51
        canary = "CANARY" + uuid.uuid4().hex
        caplog.set_level(logging.DEBUG, logger="cawbase_content_engine")
        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "p", f"Name {canary}", description=f"Desc {canary}", language="es", **NO_CATALOG)
        await ops.create_category(
            "a",
            "p",
            "c",
            f"Cat {canary}",
            f"Def {canary}",
            examples={"positive": [f"pos {canary}"], "negative": [f"neg {canary}"]},
            translations={"en": {"name": f"EN {canary}", "definition": f"EN def {canary}"}},
        )
        await ops.update_category("a", "p", "c", expected_revision=1, definition=f"New {canary}")
        await ops.create_sensitive_type("a", "p", "t", f"T {canary}", f"TDef {canary}")
        await ops.update_ranking_config("a", "p", expected_revision=1, tie_tolerance=0.5)
        p = await ops.get_profile("a", "p")
        await ops.publish_profile("a", "p", expected_draft_revision=p.draft_revision)
        await ops.set_default_profile("a", "p", expected_revision=0)
        raised = []
        for call in (
            lambda: ops.create_category("a", "p", "c", f"Cat {canary}", "other"),
            lambda: ops.create_category("a", "p", "c9", f"Cat {canary}", "x"),
            lambda: ops.update_category("a", "p", "c", expected_revision=99, name=f"Z {canary}"),
            lambda: ops.create_category("a", "p", "c8", "n", "d" * 2000 + canary),
            lambda: ops.update_ranking_config("a", "p", expected_revision=2, mode="resolution"),
            lambda: ops.disable_profile("a", "p"),
        ):
            with pytest.raises(Exception) as e:  # noqa: B017
                await call()
            raised.append(f"{e.value!r} {e.value}")
            raised.append(json.dumps(getattr(e.value, "problems", [])))
        evs = await events(engine, scope_id="a")
        assert evs, "events must exist"
        dump = json.dumps([[e.action, e.target_type, e.target_id, e.detail, e.actor_label] for e in evs])
        assert canary not in dump and canary not in "".join(raised) and canary not in caplog.text
        actions = {e.action for e in evs}
        assert {
            "profile.created",
            "category.created",
            "category.updated",
            "sensitive_type.created",
            "profile.ranking_updated",
            "profile.published",
            "profile.default_set",
        } <= actions
        upd = next(e for e in evs if e.action == "category.updated")
        assert upd.detail["changed_fields"] == ["definition"] and upd.detail["revision"] == [1, 2]
        assert (await ops.get_profile("a", "p")).display_name == f"Name {canary}"  # the content itself is stored


class TestStaticChecks:
    def test_queries_always_scope_and_versions_are_never_updated(self):  # AC-A02-45
        import pathlib

        base = pathlib.Path(__file__).resolve().parent.parent / "src" / "cawbase_content_engine" / "operations"
        text = "".join(
            (base / n).read_text() for n in ("profiles.py", "elements.py", "versions.py", "profile_common.py")
        )
        assert "update(t.profile_versions)" not in text and "delete(t.profile_versions)" not in text
        assert "sa.delete(" not in text, "A02 never deletes rows (logical deletion only)"
        for name in ("profiles.py", "elements.py", "versions.py"):
            src = (base / name).read_text()
            assert "FOR UPDATE" not in src  # row locks go through with_for_update(), never raw SQL
        assert "with_for_update(key_share=True)" in text and "with_for_update()" in text

    def test_locks_render_as_select_for_update_on_postgres_and_nothing_on_sqlite(self):  # AC-A02-37
        from sqlalchemy.dialects import postgresql, sqlite

        from cawbase_content_engine.persistence.dialect import Backend
        from cawbase_content_engine.persistence.tables import tables_for

        pg, lite = tables_for(Backend.POSTGRESQL), tables_for(Backend.SQLITE)
        profile = sa.select(pg.profiles.c.status).with_for_update()
        scope = sa.select(pg.scopes.c.status).with_for_update(key_share=True)
        assert str(profile.compile(dialect=postgresql.dialect())).rstrip().endswith("FOR UPDATE")
        assert str(scope.compile(dialect=postgresql.dialect())).rstrip().endswith("FOR NO KEY UPDATE")
        # SQLite has no row locks: the write transaction A01 opens (BEGIN IMMEDIATE / the host's) serializes
        assert "FOR" not in str(sa.select(lite.profiles.c.status).with_for_update().compile(dialect=sqlite.dialect()))
