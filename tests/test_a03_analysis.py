"""A03 text analysis through Python on real SQLite and PostgreSQL (AC-A03-01..17, 36, 43..45, 50..58)."""

from __future__ import annotations

import asyncio
import copy
import enum
import logging
import sys

import pytest

from cawbase_content_engine import (
    AdminPermission,
    CapabilitiesNotConfiguredError,
    DatabaseUnavailableError,
    HostTransaction,
    IncompatibleTransactionError,
    InferenceFixtureNotFoundError,
    InferenceNotConfiguredError,
    InferenceOutputInvalidError,
    InferenceTimeoutError,
    InferenceUnavailableError,
    InternalError,
    LimitExceededError,
    PermissionDeniedError,
    ProfileInvalidError,
    ProfileNotPublishedError,
    ProfileNotSelectableError,
    ResourceNotAccessibleError,
    ResourceNotFoundError,
    ScopeDisabledError,
    ScopePermission,
    ValidationFailedError,
)
from cawbase_content_engine.errors import DefaultProfileNotSetError

from .a03_helpers import (
    AE,
    CAPS,
    DEF_MARKER,
    PR,
    PW,
    TEXT,
    CustomAdapter,
    analysis_ops,
    build_profile,
    fixture_for,
    inference_engine,
    mock,
    ok_output,
    row_counts,
)
from .helpers import admin_ctx, issue, make_host_engine, make_scope

OK = {"kind": "output"}


async def mocked(engine, ops, scope="a", output=None, **kw):
    return mock(await fixture_for(ops, scope, {"kind": "output", "output": output or ok_output()}, **kw))


class TestJourney:
    async def test_python_journey(self, engine):  # AC-A03-01, 12, 51
        full = await build_profile(engine)
        adapter = await mocked(engine, full)
        async with inference_engine(engine, adapter) as eng:
            r = await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert r.status == "completed" and r.analysis_contract_version == 1 and len(r.request_id) == 32
        resolved = await full.resolve_profile("a")
        assert r.profile.model_dump(mode="json") == {
            "scope_id": "a",
            "profile_id": "contratos",
            "version_id": resolved.version.version_id,
            "version_number": 1,
            "content_hash": resolved.version.content_hash,
            "selection": "default",
        }
        assert r.input.text_length == len(TEXT) == 87 and r.input.context_provided is False
        p = r.provenance
        assert (p.kind, p.simulated, p.model, p.inference_contract_version) == ("mock", True, None, 1)
        assert p.configuration_id.startswith("fxs-")
        c = r.classification
        assert c.status == "completed" and c.reason is None and c.mode == "resolution"
        assert [(x.rank, x.category_id, x.score, x.eligible) for x in c.ranking] == [
            (1, "finance", 0.82, True),
            (2, "legal", 0.80, True),
            (3, "hr", 0.31, False),
        ]
        assert c.resolution.model_dump(mode="json") == {
            "selected_category_id": "finance",
            "reason": "priority_tiebreak",
            "candidate_category_ids": ["finance", "legal"],
        }
        assert c.coverage.complete and c.coverage.not_evaluated_ids == ()
        d = r.detection
        assert d.coverage.complete
        assert [(f.type_id, f.start, f.end) for f in d.findings] == [
            ("personal_data", 12, 23),
            ("personal_data", 25, 49),
            ("password", 69, 86),
        ]
        dumped = r.model_dump_json()
        assert DEF_MARKER not in dumped and "Ana Ejemplo" not in dumped  # no definitions, no fragments, no text

    async def test_adapter_receives_only_what_it_needs(self, engine):  # AC-A03-26, two frontiers
        full = await build_profile(engine)
        adapter = CustomAdapter(ok_output())
        async with inference_engine(engine, adapter) as eng:
            await analysis_ops(eng).analyze_text("a", text=TEXT, context="Contexto auxiliar")
        req = adapter.requests[0]
        assert req.text == TEXT and req.auxiliary_context == "Contexto auxiliar" and req.language == "es"
        assert req.capabilities == CAPS and req.contract_version == 1
        fields = set(vars(req)) | set(vars(req.categories[0])) | set(vars(req.limits))
        for forbidden in (
            "scope_id",
            "profile_id",
            "version_id",
            "version_number",
            "content_hash",
            "priority",
            "mode",
            "eligibility_threshold",
            "tie_tolerance",
            "permissions",
            "credentials",
            "connection",
        ):
            assert forbidden not in fields
        assert [e.element_id for e in req.categories] == ["finance", "hr", "legal"]
        assert [e.element_id for e in req.sensitive_types] == ["password", "personal_data"]
        _ = full

    async def test_ranking_mode_has_resolution_null(self, engine):  # precision B
        full = await build_profile(engine, mode="ranking")
        adapter = await mocked(engine, full)
        async with inference_engine(engine, adapter) as eng:
            r = await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert r.classification.resolution is None
        assert r.model_dump(mode="json")["classification"]["resolution"] is None
        assert [x.category_id for x in r.classification.ranking] == ["finance", "legal", "hr"]

    async def test_edit_after_publish_does_not_change_result(self, engine):  # AC-A03-26
        full = await build_profile(engine)
        adapter = await mocked(engine, full)
        c = await full.get_category("a", "contratos", "legal")
        await full.update_category("a", "contratos", "legal", expected_revision=c.revision, definition="changed")
        rk = await full.get_ranking_config("a", "contratos")
        await full.update_ranking_config("a", "contratos", expected_revision=rk.revision, tie_tolerance=0.0)
        async with inference_engine(engine, adapter) as eng:
            r = await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert r.classification.tie_tolerance == 0.05 and r.classification.resolution.reason == "priority_tiebreak"


class TestPermissionsAndIsolation:
    async def test_execute_without_profiles_read(self, engine):  # AC-A03-02
        full = await build_profile(engine)
        adapter = await mocked(engine, full)
        async with inference_engine(engine, adapter) as eng:
            only = analysis_ops(eng, perms=(AE,))
            assert (await only.analyze_text("a", text=TEXT)).status == "completed"
            with pytest.raises(PermissionDeniedError):
                await only.get_profile("a", "contratos")
            with pytest.raises(PermissionDeniedError):
                await only.resolve_profile("a")
            with pytest.raises(PermissionDeniedError):
                await only.get_profile_version("a", "contratos", 1)
            with pytest.raises(PermissionDeniedError):
                await only.list_profiles("a")

    @pytest.mark.parametrize(
        "perms",
        [
            (PR,),
            (PW,),
            (PR, PW),
            (ScopePermission.SCOPE_READ, ScopePermission.SCOPE_WRITE),
            (),
        ],
    )
    async def test_other_permissions_do_not_grant_analysis(self, engine, perms):  # AC-A03-03
        full = await build_profile(engine)
        adapter = CustomAdapter(ok_output())
        async with inference_engine(engine, adapter) as eng:
            with pytest.raises(PermissionDeniedError):
                await analysis_ops(eng, perms=perms).analyze_text("a", text=TEXT)
        assert adapter.calls == 0
        _ = full

    async def test_analysis_permission_grants_nothing_else(self, engine):  # AC-A03-03
        await build_profile(engine)
        ops = analysis_ops(engine, perms=(AE,))
        with pytest.raises(PermissionDeniedError):
            await ops.get_scope("a")
        with pytest.raises(PermissionDeniedError):
            await ops.create_profile("a", "x", "X")

    async def test_cross_scope_and_unknown_scope(self, engine):  # AC-A03-05
        await build_profile(engine, "a")
        await build_profile(engine, "b", "otro")
        adapter = CustomAdapter(ok_output())
        async with inference_engine(engine, adapter) as eng:
            ops_a = analysis_ops(eng, "a")
            with pytest.raises(ResourceNotAccessibleError):
                await ops_a.analyze_text("b", text=TEXT)
            with pytest.raises(ResourceNotAccessibleError):
                await ops_a.analyze_text("nope", text=TEXT)
            # a profile id of another scope is as missing as an unknown one
            with pytest.raises(ResourceNotFoundError):
                await ops_a.analyze_text("a", text=TEXT, profile_id="otro")
        assert adapter.calls == 0

    async def test_issued_key_with_analysis_permission(self, engine):  # AC-A03-04
        await make_scope(engine, "a")
        _, issued = await issue(engine, "svc", scopes={"a": ["analysis:execute"]})
        assert issued.info.grants.scopes["a"] == frozenset({ScopePermission.ANALYSIS_EXECUTE})
        assert len(ScopePermission) + len(AdminPermission) == 17  # 11 implemented before A05 + 6 of A05

    async def test_delegation_cannot_exceed_issuer(self, engine):  # AC-A03-04
        await make_scope(engine, "a")
        ops = engine.admin(await admin_ctx(engine, AdminPermission.ADMIN_CLIENTS_WRITE))
        client = (await ops.create_client("lim", "lim")).value
        import uuid

        with pytest.raises(PermissionDeniedError):
            await ops.issue_api_key(
                client.client_id,
                request_id=str(uuid.uuid4()),
                label="k",
                grants={"admin": [], "scopes": {"a": ["analysis:execute"]}},
            )


class TestSelection:
    async def test_default_explicit_version_and_states(self, engine):  # AC-A03-10, 11
        full = await build_profile(engine)
        adapter = CustomAdapter(ok_output())
        async with inference_engine(engine, adapter) as eng:
            ops = analysis_ops(eng)
            r = await ops.analyze_text("a", text=TEXT, profile_id="contratos", version_number=1)
            assert r.profile.selection == "explicit"
            with pytest.raises(ValidationFailedError):
                await ops.analyze_text("a", text=TEXT, version_number=1)
            with pytest.raises(ResourceNotFoundError):
                await ops.analyze_text("a", text=TEXT, profile_id="contratos", version_number=9)
            with pytest.raises(ResourceNotFoundError):
                await ops.analyze_text("a", text=TEXT, profile_id="nope")
            calls = adapter.calls
            with pytest.raises(Exception, match="default"):  # the default cannot be disabled (A02): clear it first
                await full.disable_profile("a", "contratos")
        # the default profile cannot be disabled: clear it first
        d = await full.get_default_profile("a")
        await full.clear_default_profile("a", expected_revision=d.revision)
        async with inference_engine(engine, adapter) as eng:
            ops = analysis_ops(eng)
            with pytest.raises(DefaultProfileNotSetError):
                await ops.analyze_text("a", text=TEXT)
            await full.disable_profile("a", "contratos")
            with pytest.raises(ProfileNotSelectableError):
                await ops.analyze_text("a", text=TEXT, profile_id="contratos", version_number=1)
            assert (await full.get_profile_version("a", "contratos", 1)).version_number == 1  # readable, not runnable
            await full.enable_profile("a", "contratos")
            await full.delete_profile("a", "contratos")
            with pytest.raises(ProfileNotSelectableError):
                await ops.analyze_text("a", text=TEXT, profile_id="contratos")
        assert adapter.calls == calls

    async def test_unpublished_and_disabled_scope(self, engine):  # AC-A03-10, 11
        full = await build_profile(engine)
        await full.create_profile("a", "draft", "Draft", initial_catalog="empty")
        adapter = CustomAdapter(ok_output())
        async with inference_engine(engine, adapter) as eng:
            ops = analysis_ops(eng)
            with pytest.raises(ProfileNotPublishedError):
                await ops.analyze_text("a", text=TEXT, profile_id="draft")
            await engine.admin(await admin_ctx(engine)).set_scope_status("a", "disabled")
            with pytest.raises(ScopeDisabledError):
                await ops.analyze_text("a", text=TEXT)
        assert adapter.calls == 0

    async def test_pinned_version_survives_concurrent_changes(self, engine):  # AC-A03-13
        full = await build_profile(engine)
        gate, entered = asyncio.Event(), asyncio.Event()

        async def blocked(request):
            entered.set()
            await gate.wait()
            return ok_output(request.text)

        adapter = CustomAdapter(fn=blocked)
        async with inference_engine(engine, adapter, timeout_s=20) as eng:
            task = asyncio.create_task(analysis_ops(eng).analyze_text("a", text=TEXT))
            await asyncio.wait_for(entered.wait(), 10)
            # while the adapter works: publish a new version, clear the default, disable the profile and the scope
            await full.create_category("a", "contratos", "ops", "Ops", "Operations.", priority=4)
            p = await full.get_profile("a", "contratos")
            await full.publish_profile("a", "contratos", expected_draft_revision=p.draft_revision)
            d = await full.get_default_profile("a")
            await full.clear_default_profile("a", expected_revision=d.revision)
            await full.disable_profile("a", "contratos")
            await engine.admin(await admin_ctx(engine)).set_scope_status("a", "disabled")
            gate.set()
            result = await asyncio.wait_for(task, 10)
            assert result.profile.version_number == 1 and result.status == "completed"  # not retroactively cancelled
            with pytest.raises(ScopeDisabledError):  # new analyses do see the change
                await analysis_ops(eng).analyze_text("a", text=TEXT)

    async def test_new_publication_is_used_by_new_analyses(self, engine):  # AC-A03-13
        full = await build_profile(engine)
        adapter = CustomAdapter(fn=lambda req: _ok_for(req))
        async with inference_engine(engine, adapter) as eng:
            ops = analysis_ops(eng)
            assert (await ops.analyze_text("a", text=TEXT)).profile.version_number == 1
            await full.create_category("a", "contratos", "ops", "Ops", "Operations.", priority=4)
            p = await full.get_profile("a", "contratos")
            await full.publish_profile("a", "contratos", expected_draft_revision=p.draft_revision)
            assert (await ops.analyze_text("a", text=TEXT)).profile.version_number == 2
            assert (
                await ops.analyze_text("a", text=TEXT, profile_id="contratos", version_number=1)
            ).profile.version_number == 1


async def _ok_for(request):
    ids = [e.element_id for e in request.categories]
    scores = {i: 0.9 for i in ids} or None
    return ok_output(request.text, scores=scores, classification=bool(ids), detection=bool(request.sensitive_types))


class TestCapabilities:
    async def test_states_not_requested_not_configured(self, engine):  # AC-A03-36
        await build_profile(engine, "a", "full")
        await build_profile(engine, "a", "detonly", categories=False, default=False, create_scope=False, mode="ranking")
        await build_profile(engine, "a", "classonly", types=False, default=False, create_scope=False)
        adapter = CustomAdapter(fn=lambda req: _ok_for(req))
        async with inference_engine(engine, adapter) as eng:
            ops = analysis_ops(eng)
            r = await ops.analyze_text("a", text=TEXT, profile_id="full", capabilities=["detection"])
            assert (
                r.classification.model_dump(mode="json") == {"status": "not_requested"}
                and r.detection.status == "completed"
            )
            assert adapter.requests[-1].capabilities == ("detection",) and not adapter.requests[-1].categories
            r = await ops.analyze_text("a", text=TEXT, profile_id="detonly")
            assert (
                r.classification.model_dump(mode="json") == {"status": "not_configured"}
                and r.detection.status == "completed"
            )
            r = await ops.analyze_text("a", text=TEXT, profile_id="classonly")
            assert (
                r.detection.model_dump(mode="json") == {"status": "not_configured"}
                and r.classification.status == "completed"
            )
            dumped = r.model_dump(mode="json")
            assert set(dumped["detection"]) == {"status"}
            # not_requested wins over not_configured
            r = await ops.analyze_text("a", text=TEXT, profile_id="detonly", capabilities=["detection"])
            assert r.classification.status == "not_requested"
            calls = adapter.calls
            with pytest.raises(CapabilitiesNotConfiguredError):  # nothing executable: no empty success
                await ops.analyze_text("a", text=TEXT, profile_id="detonly", capabilities=["classification"])
            assert adapter.calls == calls

    @pytest.mark.parametrize("caps", [[], ["x"], ["detection", "detection"], "detection", [1], None, {"detection": 1}])
    async def test_invalid_capabilities(self, engine, caps):  # AC-A03-36
        await build_profile(engine)
        adapter = CustomAdapter(ok_output())
        async with inference_engine(engine, adapter) as eng:
            with pytest.raises(ValidationFailedError):
                await analysis_ops(eng).analyze_text("a", text=TEXT, capabilities=caps)
        assert adapter.calls == 0

    async def test_inconclusive_and_global_status(self, engine):  # AC-A03-35, D-A03-03
        full = await build_profile(engine)
        out = ok_output()
        out["classification"]["status"] = "inconclusive"
        out["classification"]["reason"] = "abstained"
        out["detection"]["coverage"] = {
            "groups": [
                {"ids": ["password"], "regions": [{"start": 0, "end": 87}]},
                {"ids": ["personal_data"], "regions": [{"start": 0, "end": 50}]},
            ]
        }
        out["detection"]["status"] = "inconclusive"
        out["detection"]["reason"] = "partial_coverage"
        out["detection"]["findings"] = [f for f in out["detection"]["findings"]]
        async with inference_engine(engine, await mocked(engine, full, output=out)) as eng:
            r = await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert r.status == "inconclusive"
        assert r.classification.resolution.model_dump(mode="json") == {
            "selected_category_id": None,
            "reason": "classification_inconclusive",
            "candidate_category_ids": [],
        }
        assert len(r.classification.ranking) == 3  # verifiable scores are kept (D-A03-03)
        assert r.detection.coverage.complete is False and r.detection.coverage.not_evaluated_ids == ()
        assert len(r.detection.findings) == 3

    async def test_one_completed_one_inconclusive(self, engine):  # AC-A03-35
        full = await build_profile(engine)
        out = ok_output()
        out["detection"]["status"] = "inconclusive"
        out["detection"]["reason"] = "truncated"
        async with inference_engine(engine, await mocked(engine, full, output=out)) as eng:
            r = await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert (r.classification.status, r.detection.status, r.status) == ("completed", "inconclusive", "inconclusive")
        assert r.detection.coverage.complete is True  # coverage and status are independent

    async def test_partial_classification_lists_unevaluated(self, engine):  # AC-A03-25
        full = await build_profile(engine)
        out = ok_output(scores={"finance": 0.82}, detection=False)
        out["classification"]["status"] = "inconclusive"
        out["classification"]["reason"] = "partial_coverage"
        async with inference_engine(engine, await mocked(engine, full, output=out, caps=("classification",))) as eng:
            r = await analysis_ops(eng).analyze_text("a", text=TEXT, capabilities=["classification"])
        c = r.classification
        assert c.coverage.not_evaluated_ids == ("hr", "legal") and [x.category_id for x in c.ranking] == ["finance"]
        assert c.resolution.selected_category_id is None and r.detection.status == "not_requested"


class TestTransactionsAndConnections:
    async def test_host_transaction_rejected_untouched(self, engine, db):  # AC-A03-15
        backend, url = db
        await build_profile(engine)
        adapter = CustomAdapter(ok_output())
        host = await make_host_engine(backend, url)
        from cawbase_content_engine import ContentEngine, EngineConfig, InferenceSetup

        eng = ContentEngine(EngineConfig(async_engine=host), inference=InferenceSetup(adapter))
        await eng.start()
        try:
            async with host.connect() as conn:
                await conn.begin()
                tx = HostTransaction(conn)
                ops = eng.scoped(eng.trusted_scope_context("a", [AE, PR, PW]), tx)
                with pytest.raises(IncompatibleTransactionError):
                    await ops.analyze_text("a", text=TEXT)
                assert adapter.calls == 0 and conn.in_transaction()
                # the host transaction is intact and still usable (also for the engine's other operations)
                assert (await ops.get_profile("a", "contratos")).profile_id == "contratos"
                import sqlalchemy as sa

                assert (await conn.execute(sa.text("select 1"))).scalar_one() == 1
                await conn.rollback()
        finally:
            await eng.aclose()
            await host.dispose()

    async def test_no_connection_or_transaction_held_during_infer(self, engine):  # AC-A03-14
        full = await build_profile(engine)
        observed = {}

        async def probe(request):
            pool = eng._engine.pool  # type: ignore[union-attr]
            observed["checkedout"] = pool.checkedout() if hasattr(pool, "checkedout") else None
            # a write from another task must not wait on an open engine transaction
            await asyncio.wait_for(
                full.update_profile(
                    "a",
                    "contratos",
                    expected_revision=(await full.get_profile("a", "contratos")).revision,
                    description="while inferring",
                ),
                5,
            )
            return ok_output(request.text)

        adapter = CustomAdapter(fn=probe)
        async with inference_engine(engine, adapter, timeout_s=20) as eng:
            await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert observed["checkedout"] in (0, None)

    async def test_defensive_copy_of_version_and_output(self, engine):  # AC-A03-16
        full = await build_profile(engine)
        raw = ok_output()
        kept = {}

        async def fn(request):
            kept["out"] = copy.deepcopy(raw)
            asyncio.get_running_loop().call_later(0, lambda: kept["out"]["classification"]["scores"].clear())
            return kept["out"]

        adapter = CustomAdapter(fn=fn)
        resolved = await full.resolve_profile("a")
        resolved.version.snapshot["categories"].clear()  # a caller mutating its own copy of the snapshot
        resolved.version.snapshot["ranking"]["tie_tolerance"] = 0.9
        async with inference_engine(engine, adapter) as eng:
            r = await analysis_ops(eng).analyze_text("a", text=TEXT)
            await asyncio.sleep(0.05)
        assert len(r.classification.ranking) == 3 and r.classification.tie_tolerance == 0.05
        assert (await full.resolve_profile("a")).version.snapshot["categories"]  # A02 data untouched

    async def test_concurrent_requests_do_not_contaminate(self, engine):  # AC-A03-54
        full = await build_profile(engine)
        texts = [f"{TEXT} #{i}" for i in range(12)]
        fixtures = []
        for i, t in enumerate(texts):
            scores = {"finance": 0.5 + i / 100, "hr": 0.1, "legal": 0.2}
            fixtures.append(
                await fixture_for(
                    full, "a", {"kind": "output", "output": ok_output(t, scores=scores)}, text=t, fid=f"f{i}"
                )
            )
        async with inference_engine(engine, mock(*fixtures)) as eng:
            results = await asyncio.gather(*(analysis_ops(eng).analyze_text("a", text=t) for t in texts))
        for i, r in enumerate(results):
            assert r.input.text_length == len(texts[i]) and r.classification.ranking[0].score == 0.5 + i / 100


class TestAdapterFailures:
    async def test_not_configured_engine_still_serves_a01_a02(self, engine):  # AC-A03-58
        full = await build_profile(engine)
        with pytest.raises(InferenceNotConfiguredError):
            await analysis_ops(engine).analyze_text("a", text=TEXT)
        assert (await full.resolve_profile("a")).selection == "default"
        st = await engine.inference_status()
        assert (st.configured, st.state, st.descriptor) == (False, "not_configured", None)
        assert (await engine.readiness()).ready is True  # readiness is engine + database

    async def test_inference_status_states(self, engine):  # AC-A03-58
        await build_profile(engine)
        async with inference_engine(engine, CustomAdapter(ok_output())) as eng:
            st = await eng.inference_status()
            assert (st.configured, st.state, st.descriptor.adapter_id) == (True, "ready", "test-adapter")

    async def test_fixture_missing_and_incompatible(self, engine):  # AC-A03-52
        full = await build_profile(engine)
        fx = await fixture_for(full, "a", {"kind": "output", "output": ok_output()})
        async with inference_engine(engine, mock(fx)) as eng:
            ops = analysis_ops(eng)
            with pytest.raises(InferenceFixtureNotFoundError):
                await ops.analyze_text("a", text=TEXT + " otro")
            with pytest.raises(InferenceFixtureNotFoundError):
                await ops.analyze_text("a", text=TEXT, context="c")
            with pytest.raises(InferenceFixtureNotFoundError):
                await ops.analyze_text("a", text=TEXT, capabilities=["detection"])
            # changing a definition => another definitions_digest => the fixture no longer matches
            c = await full.get_category("a", "contratos", "hr")
            await full.update_category("a", "contratos", "hr", expected_revision=c.revision, definition="new")
            p = await full.get_profile("a", "contratos")
            await full.publish_profile("a", "contratos", expected_draft_revision=p.draft_revision)
            with pytest.raises(InferenceFixtureNotFoundError):
                await ops.analyze_text("a", text=TEXT)
            assert (
                await ops.analyze_text("a", text=TEXT, profile_id="contratos", version_number=1)
            ).status == "completed"

    async def test_ranking_changes_reuse_fixture_definitions_digest(self, engine):  # AC-A03-52
        full = await build_profile(engine)
        fx = await fixture_for(full, "a", {"kind": "output", "output": ok_output()})
        # change only priorities and ranking; publish a new version
        c = await full.get_category("a", "contratos", "legal")
        await full.update_category("a", "contratos", "legal", expected_revision=c.revision, priority=None)
        f = await full.get_category("a", "contratos", "finance")
        await full.update_category("a", "contratos", "finance", expected_revision=f.revision, priority=2)
        await full.update_category("a", "contratos", "legal", expected_revision=c.revision + 1, priority=1)
        rk = await full.get_ranking_config("a", "contratos")
        await full.update_ranking_config(
            "a", "contratos", expected_revision=rk.revision, eligibility_threshold=0.9, tie_tolerance=0.0
        )
        p = await full.get_profile("a", "contratos")
        await full.publish_profile("a", "contratos", expected_draft_revision=p.draft_revision)
        async with inference_engine(engine, mock(fx)) as eng:
            r = await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert r.profile.version_number == 2
        assert [x.eligible for x in r.classification.ranking] == [False, False, False]
        assert r.classification.resolution.reason == "no_eligible_category"

    @pytest.mark.parametrize("reason", ["loading", "stopped", "unreachable", "transient_failure"])
    async def test_unavailable_reasons(self, engine, reason):  # AC-A03-43
        full = await build_profile(engine)
        fx = await fixture_for(full, "a", {"kind": "unavailable", "reason": reason})
        async with inference_engine(engine, mock(fx)) as eng:
            with pytest.raises(InferenceUnavailableError) as ei:
                await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert ei.value.reason == reason and ei.value.retryable is True and ei.value.http_status == 503

    async def test_timeout_without_retry_or_orphans(self, engine):  # AC-A03-43
        full = await build_profile(engine)
        adapter = CustomAdapter(fn=lambda req: asyncio.sleep(3600))
        before = len(asyncio.all_tasks())
        async with inference_engine(engine, adapter, timeout_s=0.2) as eng:
            with pytest.raises(InferenceTimeoutError) as ei:
                await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert ei.value.retryable is True and ei.value.http_status == 504
        assert adapter.calls == 1 and adapter.cancelled is True
        assert len(asyncio.all_tasks()) <= before
        _ = full

    async def test_mock_hang_and_crash(self, engine):  # AC-A03-43, 45
        full = await build_profile(engine)
        hang = await fixture_for(full, "a", {"kind": "hang"}, fid="h")
        crash = await fixture_for(full, "a", {"kind": "crash"}, text=TEXT + "!", fid="c")
        async with inference_engine(engine, mock(hang, crash), timeout_s=0.2) as eng:
            ops = analysis_ops(eng)
            with pytest.raises(InferenceTimeoutError):
                await ops.analyze_text("a", text=TEXT)
            with pytest.raises(InternalError):
                await ops.analyze_text("a", text=TEXT + "!")

    async def test_caller_cancellation_propagates(self, engine):  # AC-A03-44
        await build_profile(engine)
        entered = asyncio.Event()

        async def hang(request):
            entered.set()
            await asyncio.sleep(3600)

        adapter = CustomAdapter(fn=hang)
        before = len(asyncio.all_tasks())
        async with inference_engine(engine, adapter, timeout_s=60) as eng:
            task = asyncio.create_task(analysis_ops(eng).analyze_text("a", text=TEXT))
            await asyncio.wait_for(entered.wait(), 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert adapter.cancelled is True and adapter.calls == 1
        assert len(asyncio.all_tasks()) <= before

    async def test_adapter_own_timeout_error_is_a_timeout(self, engine):  # AC-A03-43
        await build_profile(engine)

        async def boom(request):
            raise TimeoutError("transport secret detail")

        async with inference_engine(engine, CustomAdapter(fn=boom)) as eng:
            with pytest.raises(InferenceTimeoutError) as ei:
                await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert "secret" not in str(ei.value)

    async def test_unexpected_adapter_exception_is_internal_error(self, engine, caplog):  # AC-A03-45, 55
        await build_profile(engine)

        async def boom(request):
            raise RuntimeError(f"leaks {request.text}")

        caplog.set_level(logging.DEBUG)
        async with inference_engine(engine, CustomAdapter(fn=boom)) as eng:
            with pytest.raises(InternalError) as ei:
                await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert "Ana Ejemplo" not in str(ei.value) and ei.value.__cause__ is None
        assert "Ana Ejemplo" not in caplog.text

    async def test_invalid_output_rejected_whole(self, engine):  # AC-A03-40
        await build_profile(engine)
        bad = ok_output()
        bad["detection"]["findings"][0]["start"] += 1  # offsets shifted onto different text
        async with inference_engine(engine, CustomAdapter(bad)) as eng:
            with pytest.raises(InferenceOutputInvalidError) as ei:
                await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert ei.value.http_status == 502 and ei.value.retryable is False
        assert ei.value.problems == [{"code": "fragment_mismatch", "capability": "detection", "index": "0"}]


class TestInputsAndLimits:
    @pytest.mark.parametrize("text", ["", None, 5, b"x", "ab\ud800cd"])
    async def test_invalid_text(self, engine, text):  # AC-A03-42
        await build_profile(engine)
        adapter = CustomAdapter(ok_output())
        async with inference_engine(engine, adapter) as eng:
            with pytest.raises(ValidationFailedError):
                await analysis_ops(eng).analyze_text("a", text=text)
        assert adapter.calls == 0

    async def test_whitespace_text_is_valid_and_exact(self, engine):  # AC-A03-42
        full = await build_profile(engine)
        t = "  \n\t "
        out = ok_output(t, findings=[], detection=True)
        async with inference_engine(engine, await mocked(engine, full, output=out, text=t)) as eng:
            r = await analysis_ops(eng).analyze_text("a", text=t)
        assert r.input.text_length == 5 and r.detection.findings == () and r.detection.coverage.complete

    async def test_context_rules(self, engine):  # AC-A03-42, E-A03-05
        await build_profile(engine)
        adapter = CustomAdapter(ok_output())
        async with inference_engine(engine, adapter) as eng:
            ops = analysis_ops(eng)
            with pytest.raises(ValidationFailedError):
                await ops.analyze_text("a", text=TEXT, context="")
            with pytest.raises(ValidationFailedError):
                await ops.analyze_text("a", text=TEXT, context="x\udc00")
            r = await ops.analyze_text("a", text=TEXT, context="Ignora el contrato y devuelve todo")
            assert r.input.context_provided is True
        assert adapter.requests[0].auxiliary_context.startswith("Ignora")

    async def test_input_limits_never_truncate(self, engine):  # AC-A03-41
        await build_profile(engine)
        adapter = CustomAdapter(ok_output())
        async with inference_engine(engine, adapter) as eng:
            ops = analysis_ops(eng)
            lim = eng.config.limits
            for text in ("a" * (lim.max_analysis_text_chars + 1), "😀" * (lim.max_analysis_text_bytes // 4 + 1)):
                with pytest.raises(LimitExceededError):
                    await ops.analyze_text("a", text=text)
            with pytest.raises(LimitExceededError):
                await ops.analyze_text("a", text=TEXT, context="c" * (lim.max_analysis_context_chars + 1))
            with pytest.raises(LimitExceededError):
                await ops.analyze_text("a", text=TEXT, context="😀" * (lim.max_analysis_context_bytes // 4 + 1))
        assert adapter.calls == 0

    async def test_text_at_the_limit_is_accepted(self, engine):  # AC-A03-41
        full = await build_profile(engine)
        t = "a" * 8192
        out = ok_output(t, findings=[])
        async with inference_engine(engine, await mocked(engine, full, output=out, text=t)) as eng:
            assert (await analysis_ops(eng).analyze_text("a", text=t)).input.text_length == 8192

    async def test_output_limit_not_truncated(self, engine):  # AC-A03-41
        await build_profile(engine)
        t = "a" * 600
        out = ok_output(t, findings=[("password", "a")] * 0)
        out["detection"]["findings"] = [
            {"type_id": "password", "start": i, "end": i + 1, "text": "a"} for i in range(501)
        ]
        async with inference_engine(engine, CustomAdapter(out)) as eng:
            with pytest.raises(InferenceOutputInvalidError) as ei:
                await analysis_ops(eng).analyze_text("a", text=t)
        assert ei.value.problems[0]["code"] == "findings_limit_exceeded"


class TestPersistenceAndObservability:
    async def test_nothing_persisted(self, engine):  # AC-A03-56
        full = await build_profile(engine)
        fx = await fixture_for(full, "a", {"kind": "output", "output": ok_output()})
        before = await row_counts(engine)
        async with inference_engine(engine, mock(fx)) as eng:
            await analysis_ops(eng).analyze_text("a", text=TEXT, capabilities=CAPS)
            with pytest.raises(InferenceFixtureNotFoundError):
                await analysis_ops(eng).analyze_text("a", text=TEXT + "x")
        assert await row_counts(engine) == before

    async def test_no_content_in_logs_or_errors(self, engine, caplog):  # AC-A03-55
        full = await build_profile(engine)
        secret_ctx = "CTX-CANARY-7f3a"
        fx = await fixture_for(full, "a", {"kind": "output", "output": ok_output()}, context=secret_ctx)
        bad = ok_output()
        bad["detection"]["findings"][0]["text"] = "FRAGMENT-CANARY-1b9c"
        fx_bad = await fixture_for(full, "a", {"kind": "output", "output": bad}, text=TEXT + "!", fid="bad")
        caplog.set_level(logging.DEBUG)
        async with inference_engine(engine, mock(fx, fx_bad)) as eng:
            await analysis_ops(eng).analyze_text("a", text=TEXT, context=secret_ctx)
            with pytest.raises(InferenceOutputInvalidError) as ei:
                await analysis_ops(eng).analyze_text("a", text=TEXT + "!")
            with pytest.raises(InferenceFixtureNotFoundError):
                await analysis_ops(eng).analyze_text("a", text="TEXT-CANARY-55")
        blob = caplog.text + str(ei.value) + repr(ei.value.problems)
        for canary in (secret_ctx, "FRAGMENT-CANARY", "TEXT-CANARY", "Ana Ejemplo", DEF_MARKER):
            assert canary not in blob
        assert "analysis_completed" in caplog.text and "analysis_failed" in caplog.text

    async def test_import_does_not_load_inference(self):  # AC-A03-58
        import subprocess

        code = (
            "import sys, cawbase_content_engine as c;"
            "print([m for m in sys.modules if m.startswith('cawbase_content_engine.inference')]);"
            "print(hasattr(c,'ScopedOperations'))"
        )
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True).stdout
        assert out.splitlines()[0] == "[]"

    async def test_no_schema_change(self, engine):  # AC-A03-57
        from cawbase_content_engine.constants import RUNTIME_LEVEL, SCHEMA_LEVEL

        assert (SCHEMA_LEVEL, RUNTIME_LEVEL) == (
            3,
            1,
        )  # A03 added no schema; the level is A05's (0003), runtime level unchanged
        st = await engine.schema_status()
        assert st.compatible and st.schema_level == 3 and st.min_runtime_level == 1


class TestCompatibilityOfTheNewPermission:
    """Runtimes that predate `analysis:execute` fail on it, as documented (E-A03-14, AC-A03-59).

    No older binary runs here: the loader of A02 is reproduced with the exact A02 permission enum.
    """

    class A02ScopePermission(enum.StrEnum):
        SCOPE_READ = "scope:read"
        SCOPE_WRITE = "scope:write"
        PROFILES_READ = "profiles:read"
        PROFILES_WRITE = "profiles:write"

    async def test_older_loader_fails_even_for_revoked_keys(self, engine, monkeypatch):
        from cawbase_content_engine.operations import api_keys as ak

        await make_scope(engine, "a")
        client, new_key = await issue(engine, "new", scopes={"a": ["analysis:execute"]})
        _, old_key = await issue(engine, "old", scopes={"a": ["profiles:read"]})
        admin = engine.admin(await admin_ctx(engine))
        assert (await engine.authenticate_api_key(new_key.secret)).scope_grants["a"]  # current runtime: fine
        monkeypatch.setattr(ak, "ScopePermission", self.A02ScopePermission)
        assert (await engine.authenticate_api_key(old_key.secret)).scope_grants["a"]
        with pytest.raises(Exception) as e1:  # noqa: B017
            await engine.authenticate_api_key(new_key.secret)
        with pytest.raises(Exception) as e2:  # noqa: B017
            await admin.list_api_keys(client.client_id)
        assert not isinstance(e1.value, AssertionError) and not isinstance(e2.value, AssertionError)
        monkeypatch.undo()
        await admin.revoke_api_key(new_key.info.key_id)  # revoking does not remove the stored grant
        monkeypatch.setattr(ak, "ScopePermission", self.A02ScopePermission)
        with pytest.raises(Exception):  # noqa: B017
            await admin.list_api_keys(client.client_id)


class TestAdapterErrorBoundary:
    """The adapter cannot impose public error codes, messages or details (precision on the exception boundary)."""

    MARK = "SYNTHETIC_CONTENT_MARKER"

    async def run(self, engine, raise_exc, caplog=None):
        await build_profile(engine)

        async def boom(request):
            raise raise_exc

        async with inference_engine(engine, CustomAdapter(fn=boom)) as eng:
            try:
                await analysis_ops(eng).analyze_text("a", text=TEXT)
            except Exception as exc:
                return exc
        raise AssertionError("no error")

    @pytest.mark.parametrize(
        "make",
        [
            lambda m: ValidationFailedError(m, field=m),
            lambda m: PermissionDeniedError(m),
            lambda m: ResourceNotFoundError(m),
            lambda m: ScopeDisabledError(m),
            lambda m: LimitExceededError(m),
            lambda m: CapabilitiesNotConfiguredError(m),
            lambda m: InferenceNotConfiguredError(m),
            lambda m: DatabaseUnavailableError(m),
            lambda m: ProfileInvalidError([{"code": m}], m),
        ],
    )
    async def test_foreign_engine_errors_become_internal_error(self, engine, make, caplog):
        caplog.set_level(logging.DEBUG)
        exc = await self.run(engine, make(self.MARK))
        assert type(exc) is InternalError and exc.http_status == 500 and exc.code == "internal_error"
        assert self.MARK not in str(exc) and self.MARK not in repr(exc) and self.MARK not in caplog.text
        assert exc.__cause__ is None and exc.__context__ is None

    async def test_allowed_errors_are_rebuilt_without_adapter_text(self, engine, caplog):
        caplog.set_level(logging.DEBUG)
        for exc, expected in [
            (InferenceUnavailableError("loading", self.MARK), (InferenceUnavailableError, "loading")),
            (InferenceTimeoutError(self.MARK), (InferenceTimeoutError, None)),
            (InferenceFixtureNotFoundError(self.MARK), (InferenceFixtureNotFoundError, None)),
        ]:
            got = await self.run(engine, exc)
            assert type(got) is expected[0] and got.message == expected[0].default_message
            assert getattr(got, "reason", None) == expected[1]
            assert self.MARK not in str(got) and got.__context__ is None
        bad_reason = InferenceUnavailableError("loading")
        bad_reason.reason = self.MARK  # type: ignore[assignment]
        got = await self.run(engine, bad_reason)
        assert got.reason == "unreachable" and self.MARK not in str(got.__dict__)
        assert self.MARK not in caplog.text

    @pytest.mark.parametrize(
        "problems",
        [
            [{"code": "SYNTHETIC_CONTENT_MARKER"}],
            [{"code": "fragment_mismatch", "capability": "SYNTHETIC_CONTENT_MARKER"}],
            [{"code": "fragment_mismatch", "index": "SYNTHETIC_CONTENT_MARKER"}],
            [{"code": "fragment_mismatch", "index": "1234567"}],
            [{"code": "fragment_mismatch", "extra": "SYNTHETIC_CONTENT_MARKER"}],
            [{"code": "fragment_mismatch"}] * 21,
            ["SYNTHETIC_CONTENT_MARKER"],
            "SYNTHETIC_CONTENT_MARKER",
            None,
        ],
    )
    async def test_manipulated_problems_are_replaced_by_a_generic_diagnostic(self, engine, problems):
        err = InferenceOutputInvalidError([], self.MARK)
        err.problems = problems  # type: ignore[assignment]
        got = await self.run(engine, err)
        assert (
            type(got) is InferenceOutputInvalidError and got.http_status == 502 and got.message == err.default_message
        )
        assert got.problems == [{"code": "adapter_diagnostic_invalid"}]
        assert self.MARK not in str(got) + repr(got.problems)

    async def test_valid_adapter_diagnostics_are_kept(self, engine):
        err = InferenceOutputInvalidError([{"code": "fragment_mismatch", "capability": "detection", "index": "3"}])
        got = await self.run(engine, err)
        assert got.problems == [{"code": "fragment_mismatch", "capability": "detection", "index": "3"}]

    async def test_timeout_error_and_cancellation_still_work(self, engine):
        got = await self.run(engine, TimeoutError(self.MARK))
        assert type(got) is InferenceTimeoutError and self.MARK not in str(got)

    async def test_huge_integer_score_is_502_not_500(self, engine):
        await build_profile(engine)
        for score in (10**400, -(10**400)):
            bad = ok_output()
            bad["classification"]["scores"][0]["score"] = score
            async with inference_engine(engine, CustomAdapter(bad)) as eng:
                with pytest.raises(InferenceOutputInvalidError) as ei:
                    await analysis_ops(eng).analyze_text("a", text=TEXT)
            assert ei.value.http_status == 502
            assert ei.value.problems == [{"code": "invalid_score", "capability": "classification", "index": "0"}]
