"""A06 contexts: creation, fixed validity, effective expiry, explicit delete, cleanup, scope status, isolation,
permissions. AC-A06-16..25."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from cawbase_content_engine import AdminPermission, ScopePermission
from cawbase_content_engine.errors import (
    ContextKindMismatchError,
    ContextUnavailableError,
    PermissionDeniedError,
    ResourceNotAccessibleError,
    ResourceNotFoundError,
    ScopeDisabledError,
    ValidationFailedError,
)
from cawbase_content_engine.jobs import db as jdb
from cawbase_content_engine.protection.markers import find_markers

from .a06_helpers import (
    A06_ALL,
    INLINE,
    NAME,
    PSEUDO,
    TEXT,
    Harness,
    expire_context,
    f,
    make_harness,
)
from .helpers import issue, make_scope

SP = ScopePermission


async def test_creation_defaults_ttl_and_opaque_id(a06: Harness):  # AC-A06-16
    ops = a06.ops()
    c = await ops.create_context("a", kind="conversation")
    assert c.context_id.startswith("ctx_") and len(c.context_id) == 36 and int(c.context_id[4:], 16) >= 0
    assert c.state == "active" and c.mapping_count == 0 and c.deleted_at is None and c.kind == "conversation"
    assert c.expires_at - c.created_at == dt.timedelta(days=7) and c.scope_id == "a"
    j = await ops.create_context("a", kind="job", ttl_s=3600)
    assert j.expires_at - j.created_at == dt.timedelta(seconds=3600) and j.kind == "job"
    assert c.context_id != j.context_id
    for bad in (59, 30 * 86400 + 1, True, "3600", 3600.0, -1):
        with pytest.raises(ValidationFailedError):
            await ops.create_context("a", kind="conversation", ttl_s=bad)  # type: ignore[arg-type]
    for bad_kind in ("other", None, "Conversation"):
        with pytest.raises(ValidationFailedError):
            await ops.create_context("a", kind=bad_kind)  # type: ignore[arg-type]
    ok = await ops.create_context("a", kind="conversation", ttl_s=60)
    assert ok.expires_at - ok.created_at == dt.timedelta(seconds=60)


async def test_configurable_default_validity(migrated):  # AC-A06-16
    backend, url = migrated
    h = await make_harness(backend, url, setup=dict(context_ttl_s=3600, max_context_ttl_s=7200))
    try:
        c = await h.ops().create_context("a", kind="conversation")
        assert c.expires_at - c.created_at == dt.timedelta(seconds=3600)
        with pytest.raises(ValidationFailedError):
            await h.ops().create_context("a", kind="conversation", ttl_s=7201)
        earlier = c
        assert (await h.ops().get_context("a", c.context_id)).expires_at == earlier.expires_at  # not retroactive
    finally:
        await h.engine.aclose()


async def test_no_use_ever_renews_or_extends_the_validity(a06: Harness):  # AC-A06-17
    cid = await a06.context()
    fixed = (await a06.ops().get_context("a", cid)).expires_at
    text = f"Hola {NAME}"
    for _ in range(3):
        a06.adapter.on(text, f(text, NAME))
        res = await a06.ops().protect_text(
            "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
        )
        assert res.context_expires_at == fixed
        out = await a06.restore(cid, res.text)
        assert out.text == text
        await a06.ops().list_contexts("a")
    after = await a06.ops().get_context("a", cid)
    assert after.expires_at == fixed and after.mapping_count == 1
    stored = (await a06.rows("protection_contexts"))[0]
    assert stored.expires_at == fixed and stored.ttl_s == 7 * 86400
    names = {n.lower() for n in dir(a06.ops())}
    assert not {n for n in names if any(w in n for w in ("renew", "extend", "touch", "refresh", "reactivate"))}


async def test_boundary_uses_the_clock_read_at_the_decision(a06: Harness, monkeypatch):  # AC-A06-18
    cid = await a06.context()
    text = f"Hola {NAME}"
    a06.adapter.on(text, f(text, NAME))
    edge = dt.datetime(2031, 5, 1, 12, 0, 0, tzinfo=dt.UTC)
    await expire_context(a06, cid, when=edge)

    def clock_at(moment):
        async def now(conn, backend):
            return moment

        monkeypatch.setattr(jdb, "db_now", now)

    async def attempt():
        return await a06.ops().protect_text(
            "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
        )

    clock_at(edge - dt.timedelta(microseconds=1))
    res = await attempt()  # one instant before: still valid
    assert (await a06.ops().get_context("a", cid)).state == "active"
    clock_at(edge)  # at expires_at the context is already expired
    with pytest.raises(ContextUnavailableError):
        await attempt()
    with pytest.raises(ContextUnavailableError):
        await a06.restore(cid, res.text)
    assert (await a06.ops().get_context("a", cid)).state == "expired"
    clock_at(edge + dt.timedelta(seconds=1))
    with pytest.raises(ContextUnavailableError):
        await attempt()
    clock_at(edge - dt.timedelta(seconds=1))
    assert (await attempt()).markers == res.markers  # reading the clock BEFORE the edge: valid again (no state change)


async def test_expired_but_not_cleaned_is_unavailable_and_data_stays_until_cleanup(a06: Harness):  # AC-A06-19
    cid, res = await a06.protect(TEXT, f(TEXT, NAME))
    await expire_context(a06, cid)
    with pytest.raises(ContextUnavailableError):
        await a06.restore(cid, res.text)
    a06.adapter.on(TEXT, f(TEXT, NAME))
    with pytest.raises(ContextUnavailableError):
        await a06.ops().protect_text(
            "a", context_id=cid, kind="conversation", text=TEXT, policy=PSEUDO, analysis=INLINE
        )
    info = await a06.ops().get_context("a", cid)
    assert info.state == "expired"
    assert len(await a06.rows("protection_mappings")) == 1  # still physically there until the cleanup runs
    assert [c.state for c in (await a06.ops().list_contexts("a", state="expired")).items] == ["expired"]
    assert (await a06.ops().list_contexts("a", state="active")).items == []


async def test_cleanup_is_explicit_bounded_repeatable_and_needs_no_keys(a06: Harness):  # AC-A06-20
    ids = []
    for _ in range(3):
        cid, _res = await a06.protect(TEXT, f(TEXT, NAME), f(TEXT, "ana.ejemplo@example.test"))
        ids.append(cid)
    keep = await a06.context()
    for cid in ids:
        await expire_context(a06, cid)
    keys = a06.keys.key_calls
    maint = a06.engine.maintenance()
    first = await maint.run_protection_cleanup(batch_size=2)
    assert (first.contexts_expired, first.mappings_deleted, first.contexts_purged) == (2, 4, 0)  # bounded batch
    second = await maint.run_protection_cleanup(batch_size=2)
    assert (second.contexts_expired, second.mappings_deleted) == (1, 2)
    third = await maint.run_protection_cleanup()
    assert (third.contexts_expired, third.mappings_deleted, third.contexts_purged) == (0, 0, 0)  # repeatable
    assert a06.keys.key_calls == keys  # no keys were requested
    rows = {r.context_id: r for r in await a06.rows("protection_contexts")}
    assert await a06.rows("protection_mappings") == []
    for cid in ids:
        assert rows[cid].state == "expired" and rows[cid].index_key_wrapped is None and rows[cid].mapping_count == 0
        assert rows[cid].metadata_expires_at == rows[cid].expires_at + dt.timedelta(days=30)
        with pytest.raises(ContextUnavailableError):  # no resurrection
            await a06.restore(cid, "x", delivered_markers=[])
    assert rows[keep].state == "active"
    usage = (await a06.rows("protection_scope_usage"))[0]
    assert usage.active_contexts == 1
    assert set(vars(first)) == {"contexts_expired", "mappings_deleted", "contexts_purged"}  # counts only
    # tombstones are purged after the metadata retention, not before
    t = a06.engine.tables.protection_contexts
    async with a06.engine._engine.begin() as c:  # type: ignore[union-attr]
        await c.execute(
            sa.update(t)
            .where(t.c.context_id == ids[0])
            .values(metadata_expires_at=dt.datetime(2000, 1, 1, tzinfo=dt.UTC))
        )
    purged = await maint.run_protection_cleanup()
    assert purged.contexts_purged == 1 and purged.contexts_expired == 0
    with pytest.raises(ResourceNotFoundError):
        await a06.ops().get_context("a", ids[0])
    assert {r.context_id for r in await a06.rows("protection_contexts")} == {ids[1], ids[2], keep}


async def test_cleanup_runs_on_an_engine_without_protection_configured(a06: Harness):  # AC-A06-20
    from cawbase_content_engine import ContentEngine, EngineConfig

    cid, _res = await a06.protect(TEXT, f(TEXT, NAME))
    await expire_context(a06, cid)
    bare = ContentEngine(EngineConfig(database_url=a06.url))
    await bare.start()
    try:
        summary = await bare.maintenance().run_protection_cleanup()
        assert summary.contexts_expired == 1 and summary.mappings_deleted == 1
    finally:
        await bare.aclose()


async def test_explicit_delete_is_immediate_irreversible_and_idempotent(a06: Harness):  # AC-A06-21
    cid, res = await a06.protect(TEXT, f(TEXT, NAME))
    ops = a06.ops()
    info = await ops.delete_context("a", cid)
    assert info.state == "deleted" and info.deleted_at is not None and info.mapping_count == 0
    assert await a06.rows("protection_mappings") == []
    row = (await a06.rows("protection_contexts"))[0]
    assert row.index_key_wrapped is None and row.state == "deleted"
    again = await ops.delete_context("a", cid)
    assert again == info  # idempotent: unchanged
    with pytest.raises(ContextUnavailableError):  # even though every key is still available
        await a06.restore(cid, res.text)
    assert (await ops.get_context("a", cid)).state == "deleted"
    await a06.engine.maintenance().run_protection_cleanup()
    assert (await ops.get_context("a", cid)).state == "deleted"  # cleanup never revives or rewrites it
    usage = (await a06.rows("protection_scope_usage"))[0]
    assert usage.active_contexts == 0
    fresh = {(await ops.create_context("a", kind="conversation")).context_id for _ in range(5)}
    assert cid not in fresh and len(fresh) == 5  # ids are random, never reassigned
    with pytest.raises(ResourceNotFoundError):
        await ops.delete_context("a", "ctx_" + "f" * 32)


async def test_an_expired_context_that_was_not_cleaned_can_be_deleted(a06: Harness):  # AC-A06-21
    cid, _res = await a06.protect(TEXT, f(TEXT, NAME))
    await expire_context(a06, cid)
    info = await a06.ops().delete_context("a", cid)
    assert info.state == "deleted" and await a06.rows("protection_mappings") == []
    assert (await a06.rows("protection_scope_usage"))[0].active_contexts == 0


async def test_disabled_scope_blocks_create_protect_restore_but_not_delete_or_metadata(a06: Harness):  # AC-A06-22
    cid, res = await a06.protect(TEXT, f(TEXT, NAME))
    admin = a06.engine.admin(a06.engine.trusted_admin_context(list(AdminPermission), actor="t"))
    await admin.set_scope_status("a", "disabled")
    ops = a06.ops()
    with pytest.raises(ScopeDisabledError):
        await ops.create_context("a", kind="conversation")
    with pytest.raises(ScopeDisabledError):
        await ops.protect_text("a", context_id=cid, kind="conversation", text=TEXT, policy=PSEUDO, analysis=INLINE)
    with pytest.raises(ScopeDisabledError):
        await a06.restore(cid, res.text)
    assert (await ops.get_context("a", cid)).state == "active" and len((await ops.list_contexts("a")).items) == 1
    assert (await ops.delete_context("a", cid)).state == "deleted"
    await admin.set_scope_status("a", "active")
    assert (await ops.create_context("a", kind="job")).state == "active"


async def test_isolation_between_scopes_and_kinds(migrated):  # AC-A06-23
    backend, url = migrated
    h = await make_harness(backend, url, scopes=("a", "b"))
    try:
        ca = await h.context("a")
        cb = await h.context("b")
        job = await h.context("a", "job")
        for call in (h.ops("b").get_context("b", ca), h.ops("b").delete_context("b", ca)):
            with pytest.raises(
                ResourceNotFoundError
            ):  # another scope's context is indistinguishable from a missing one
                await call
        with pytest.raises(ResourceNotAccessibleError):
            await h.ops("b").get_context("a", ca)
        text = f"Hola {NAME}"
        h.adapter.on(text, f(text, NAME))
        res = await h.ops("a").protect_text(
            "a", context_id=ca, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
        )
        with pytest.raises(ResourceNotFoundError):
            await h.ops("b").protect_text(
                "b", context_id=ca, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
            )
        h.adapter.on(res.text)
        with pytest.raises(Exception) as ei:  # a marker of context ca does not restore under context cb
            await h.ops("b").restore_text(
                "b", context_id=cb, kind="conversation", text=res.text, analysis=INLINE, delivered_markers=res.markers
            )
        assert type(ei.value).__name__ == "MarkerUnknownError"
        with pytest.raises(ContextKindMismatchError):
            await h.ops("a").protect_text(
                "a", context_id=job, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
            )
        listing = await h.ops("b").list_contexts("b")
        assert [c.context_id for c in listing.items] == [cb]
        assert [c.context_id for c in (await h.ops("a").list_contexts("a", kind="job")).items] == [job]
    finally:
        await h.engine.aclose()


async def test_list_contexts_paginates_and_validates(a06: Harness):
    ids = sorted([await a06.context() for _ in range(5)])
    ops = a06.ops()
    p1 = await ops.list_contexts("a", limit=2)
    p2 = await ops.list_contexts("a", limit=2, cursor=p1.next_cursor)
    p3 = await ops.list_contexts("a", limit=2, cursor=p2.next_cursor)
    assert [c.context_id for c in p1.items + p2.items + p3.items] == ids and p3.next_cursor is None
    for kw in ({"kind": "x"}, {"state": "gone"}, {"limit": 0}, {"cursor": "!!!"}):
        with pytest.raises(ValidationFailedError):
            await ops.list_contexts("a", **kw)


async def test_permission_matrix_is_separated(a06: Harness):  # AC-A06-24
    cid = await a06.context()
    full = a06.ops()
    only = lambda p: a06.ops(perms=[p])  # noqa: E731
    with pytest.raises(PermissionDeniedError):
        await only(SP.TEXT_RESTORE).create_context("a", kind="conversation")
    with pytest.raises(PermissionDeniedError):
        await only(SP.TEXT_PROTECT).get_context("a", cid)
    with pytest.raises(PermissionDeniedError):
        await only(SP.TEXT_PROTECT).list_contexts("a")
    with pytest.raises(PermissionDeniedError):
        await only(SP.CONTEXTS_READ).delete_context("a", cid)
    with pytest.raises(PermissionDeniedError):
        await only(SP.CONTEXTS_DELETE).get_context("a", cid)
    assert (await only(SP.CONTEXTS_READ).get_context("a", cid)).context_id == cid
    assert (await only(SP.TEXT_PROTECT).create_context("a", kind="job")).kind == "job"
    # protect needs text:protect AND the source permission; restore needs text:restore (not text:protect)
    text = f"Hola {NAME}"
    a06.adapter.on(text, f(text, NAME))
    for perms in ([SP.TEXT_RESTORE, SP.ANALYSIS_EXECUTE], [SP.TEXT_PROTECT], [SP.TEXT_PROTECT, SP.RESULTS_READ]):
        with pytest.raises(PermissionDeniedError):
            await a06.ops(perms=perms).protect_text(
                "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
            )
    res = await full.protect_text("a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE)
    a06.adapter.on(res.text)
    for perms in ([SP.TEXT_PROTECT, SP.ANALYSIS_EXECUTE], [SP.TEXT_RESTORE], [SP.CONTEXTS_READ, SP.ANALYSIS_EXECUTE]):
        with pytest.raises(PermissionDeniedError):
            await a06.ops(perms=perms).restore_text(
                "a", context_id=cid, kind="conversation", text=res.text, analysis=INLINE, delivered_markers=res.markers
            )
    # an administrator has no path to contexts, mappings or protected text
    admin = a06.engine.admin(a06.engine.trusted_admin_context(list(AdminPermission), actor="t"))
    assert not [n for n in dir(admin) if "context" in n.lower() and not n.startswith("_") and "client" not in n]
    # the permission of a different scope's grant does not carry over
    with pytest.raises(ResourceNotAccessibleError):
        await full.get_context("zzz", cid)
    assert find_markers(res.text) == res.markers


async def test_catalog_has_21_permissions_and_delegation_follows_d27(a06: Harness):  # AC-A06-25
    assert len(ScopePermission) + len(AdminPermission) == 25 and len(ScopePermission) == 19  # A07 adds 4 (docx:*)
    assert {p.value for p in ScopePermission} >= {"text:protect", "text:restore", "contexts:read", "contexts:delete"}
    await make_scope(a06.engine, "b")
    issuer = a06.engine.trusted_admin_context(
        [AdminPermission.ADMIN_CLIENTS_WRITE], scope_grants={"a": [SP.TEXT_PROTECT, SP.CONTEXTS_READ]}, actor="t"
    )
    ops = a06.engine.admin(issuer)
    client = (await ops.create_client("c1", "C1")).value
    ok = await ops.issue_api_key(
        client.client_id,
        request_id="00000000-0000-4000-8000-000000000001",
        label="k",
        grants={"scopes": {"a": [SP.TEXT_PROTECT]}},
    )
    assert ok.info.grants.scopes["a"] == frozenset({SP.TEXT_PROTECT})
    for escalated in ({"a": [SP.TEXT_RESTORE]}, {"a": [SP.CONTEXTS_DELETE]}, {"b": [SP.TEXT_PROTECT]}):
        with pytest.raises(PermissionDeniedError):
            await ops.issue_api_key(
                client.client_id,
                request_id="00000000-0000-4000-8000-000000000002",
                label="k",
                grants={"scopes": escalated},
            )
    # a previously issued key keeps exactly the grants it had (no automatic gain of the new permissions)
    _, legacy = await issue(a06.engine, "legacy", scopes={"a": [SP.SCOPE_READ, SP.PROFILES_READ]})
    assert legacy.info.grants.scopes["a"] == frozenset({SP.SCOPE_READ, SP.PROFILES_READ})
    assert set(A06_ALL) >= {SP.TEXT_PROTECT}
