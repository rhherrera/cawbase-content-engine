"""A06 protection (``protect_text``) end to end on both backends: AC-A06-01..10, 14, 15, 59."""

from __future__ import annotations

import random

import pytest

from cawbase_content_engine.errors import (
    CapabilitiesNotConfiguredError,
    DetectionInconclusiveError,
    LimitExceededError,
    MarkerCollisionError,
    PermissionDeniedError,
    QuotaExceededError,
    ValidationFailedError,
)
from cawbase_content_engine.protection.markers import find_markers
from cawbase_content_engine.protection.types import InlineAnalysis, JobAnalysis, ProtectionPolicy

from .a03_helpers import build_profile
from .a06_helpers import (
    A06_ALL,
    INLINE,
    MAIL,
    NAME,
    PASSWORD,
    PSEUDO,
    REDACT,
    TEXT,
    TP,
    Harness,
    f,
    make_harness,
    span,
)


async def test_round_trip_is_exact(a06: Harness):  # AC-A06-01
    fs = [f(TEXT, NAME), f(TEXT, MAIL), f(TEXT, PASSWORD, "password")]
    cid, res = await a06.protect(TEXT, *fs)
    assert res.outcome == "protected" and len(res.markers) == 3 and res.counts["pseudonymized"] == 3
    assert NAME not in res.text and MAIL not in res.text and PASSWORD not in res.text
    back = await a06.restore(cid, res.text)
    assert back.text == TEXT and back.outcome == "restored" and back.counts["markers_restored"] == 3
    assert res.analysis.source == "inline" and res.analysis.profile.profile_id == "contratos"
    assert res.analysis.provenance.simulated is not None and res.analysis.detection_status == "completed"


async def test_redaction_creates_no_mapping_and_cannot_be_restored(a06: Harness):  # AC-A06-02
    cid, res = await a06.protect(TEXT, f(TEXT, PASSWORD, "password"), f(TEXT, NAME), policy=REDACT)
    assert res.text.count("[REDACTED]") == 2 and res.markers == () and res.counts["redacted"] == 2
    assert all(g.action == "redact" and g.marker is None for g in res.groups)
    assert await a06.rows("protection_mappings") == []
    back = await a06.restore(cid, res.text, delivered_markers=[])
    assert back.text == res.text and back.outcome == "no_markers"


async def test_per_type_actions_and_default(a06: Harness):
    pol = ProtectionPolicy("pseudonymize", {"password": "redact"})
    cid, res = await a06.protect(TEXT, f(TEXT, NAME), f(TEXT, PASSWORD, "password"), policy=pol)
    assert [g.action for g in res.groups] == ["pseudonymize", "redact"] and len(res.markers) == 1
    assert len(await a06.rows("protection_mappings")) == 1


UNICODE_CASES = [
    "Cliente 😀 Ana Ejemplo vive aquí",  # non-BMP before the finding (offsets are code points, not UTF-16 units)
    "Ané Ejemplo (NFD) y Ana Ejemplo",  # combining acute: the text is never normalized
    "x‍y Ana Ejemplo z‍",  # zero width joiner
    "L1\r\nAna Ejemplo\r\nL3 Ana Ejemplo",  # CRLF kept outside the findings
    "\u0000\u0007 Ana Ejemplo   fin",  # control characters and a line separator
]


@pytest.mark.parametrize("text", UNICODE_CASES)
async def test_unicode_offsets_and_crlf_are_preserved(a06: Harness, text):  # AC-A06-03
    s, e = span(text, "Ana Ejemplo")
    cid, res = await a06.protect(text, ("personal_data", s, e))
    g = res.groups[0]
    assert (g.input_start, g.input_end) == (s, e)
    assert res.text[: g.output_start] == text[:s] and res.text[g.output_end :] == text[e:]
    assert res.text[g.output_start : g.output_end] == g.marker
    assert (await a06.restore(cid, res.text)).text == text


async def test_same_value_same_marker_in_one_text_across_calls_and_different_per_context(a06: Harness):  # AC-A06-04
    text = f"{NAME}, otra vez {NAME} y finalmente {NAME}."
    fs = [f(text, NAME, occurrence=i) for i in range(3)]
    cid, res = await a06.protect(text, *fs)
    assert len(res.markers) == 1 and res.text.count(res.markers[0]) == 3
    assert res.counts["new_markers"] == 1 and len(await a06.rows("protection_mappings")) == 1
    text2 = f"Hola {NAME}"
    a06.adapter.on(text2, f(text2, NAME))
    again = await a06.ops().protect_text(
        "a", context_id=cid, kind="conversation", text=text2, policy=PSEUDO, analysis=INLINE
    )
    assert again.markers == res.markers and again.counts["reused_markers"] == 1 and again.counts["new_markers"] == 0
    assert len(await a06.rows("protection_mappings")) == 1
    _, other = await a06.protect(text2, f(text2, NAME))  # a new context: an independent marker
    assert other.markers != res.markers


async def test_no_findings_returns_the_same_text_and_still_validates_the_context(a06: Harness):  # AC-A06-05
    text = "Nada sensible aquí."
    cid, res = await a06.protect(text)
    assert res.text == text and res.outcome == "no_findings" and res.groups == () and res.markers == ()
    assert await a06.rows("protection_mappings") == []
    from cawbase_content_engine.errors import ContextKindMismatchError, ResourceNotFoundError

    a06.adapter.on(text)
    with pytest.raises(ContextKindMismatchError):
        await a06.ops().protect_text("a", context_id=cid, kind="job", text=text, policy=PSEUDO, analysis=INLINE)
    with pytest.raises(ResourceNotFoundError):
        await a06.ops().protect_text(
            "a", context_id="ctx_" + "0" * 32, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
        )


async def test_regions_outside_groups_are_copied_exactly(a06: Harness):  # AC-A06-06
    rnd = random.Random(11)  # noqa: S311 - reproducible test data, not security
    alphabet = "abc de\r\n😀éX1;"
    for round_ in range(12):
        text = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(40, 120)))
        findings, used = [], []
        for _ in range(rnd.randint(1, 4)):
            s = rnd.randint(0, len(text) - 3)
            e = rnd.randint(s + 1, min(len(text), s + 12))
            if any(s < ue and us < e for us, ue in used):
                continue
            used.append((s, e))
            findings.append(("personal_data", s, e))
        if not findings:
            continue
        cid, res = await a06.protect(text, *findings)
        assert (await a06.restore(cid, res.text)).text == text, round_
        # the literal runs between groups are byte-identical in the output
        out_pos = 0
        pos = 0
        for g in res.groups:
            assert res.text[out_pos : g.output_start] == text[pos : g.input_start]
            out_pos, pos = g.output_end, g.input_end
        assert res.text[out_pos:] == text[pos:]


async def test_overlapping_findings_form_one_group_and_redaction_wins(a06: Harness):  # AC-A06-07, 08
    text = "0123456789abcdefghijklmnopqrstuvwxyz0123456789"
    fs = [("personal_data", 0, 10), ("password", 8, 20), ("personal_data", 19, 30), ("personal_data", 30, 35)]
    cid, res = await a06.protect(text, *fs)
    assert [(g.input_start, g.input_end) for g in res.groups] == [(0, 30), (30, 35)]  # adjacent: separate
    assert len(res.markers) == 2
    cid2, red = await a06.protect(text, *fs, policy=ProtectionPolicy("pseudonymize", {"password": "redact"}))
    assert [(g.action, g.input_start, g.input_end) for g in red.groups] == [("redact", 0, 30), ("pseudonymize", 30, 35)]
    assert len(await a06.rows("protection_mappings")) == 2 + 1  # 2 from the first context, 1 from the second
    assert (await a06.restore(cid, res.text)).text == text


@pytest.mark.parametrize(
    "status,reason",
    [("inconclusive", "partial_coverage"), ("inconclusive", "abstained"), ("inconclusive", "truncated")],
)
async def test_inconclusive_detection_yields_no_text_no_rows(a06: Harness, status, reason):  # AC-A06-09
    cid = await a06.context()
    a06.adapter.on(TEXT, f(TEXT, NAME))
    a06.adapter.mode[TEXT] = (status, reason)
    before = await a06.rows("protection_contexts")
    with pytest.raises(DetectionInconclusiveError):
        await a06.ops().protect_text(
            "a", context_id=cid, kind="conversation", text=TEXT, policy=PSEUDO, analysis=INLINE
        )
    assert await a06.rows("protection_mappings") == [] and await a06.rows("protection_contexts") == before


async def test_detection_not_configured_in_the_profile(a06: Harness):  # AC-A06-09
    await build_profile(a06.engine, "nodet", types=False, create_scope=True)
    ops = a06.ops("nodet")
    cid = (await ops.create_context("nodet", kind="conversation")).context_id
    with pytest.raises(CapabilitiesNotConfiguredError):
        await ops.protect_text("nodet", context_id=cid, kind="conversation", text=TEXT, policy=PSEUDO, analysis=INLINE)


async def test_inline_analysis_uses_the_same_text_and_the_api_accepts_no_caller_findings(a06: Harness):  # AC-A06-10
    import inspect

    from cawbase_content_engine import ScopedOperations

    cid, res = await a06.protect(TEXT, f(TEXT, NAME))
    assert a06.adapter.calls[-1] == TEXT  # the engine analyzed the very text that was transformed
    assert res.analysis.profile.version_number == 1 and res.analysis.profile.content_hash
    params = set(inspect.signature(ScopedOperations.protect_text).parameters)
    assert not {"findings", "result", "analysis_result", "offsets"} & params
    ops = a06.ops()
    for bad in (object(), {"findings": [("personal_data", 0, 3)]}, ("personal_data", 0, 3), None):
        with pytest.raises(ValidationFailedError):
            await ops.protect_text("a", context_id=cid, kind="conversation", text=TEXT, policy=PSEUDO, analysis=bad)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailedError):  # a version needs a profile
        await ops.protect_text(
            "a",
            context_id=cid,
            kind="conversation",
            text=TEXT,
            policy=PSEUDO,
            analysis=InlineAnalysis(version_number=1),
        )
    calls = len(a06.adapter.calls)
    with pytest.raises(ValidationFailedError):
        await ops.protect_text(
            "a", context_id=cid, kind="conversation", text=TEXT, policy=PSEUDO, analysis=JobAnalysis("nope")
        )
    assert len(a06.adapter.calls) == calls


COLLIDING = [
    "[[CCE1:7Q4K9M2XJ8WH3NV5]]",  # strict literal
    "x [[cce1:7q4k9m2xj8wh3nv5]] y",  # recognizable alterations
    "CCE1:7Q4K9M2XJ8WH3NV5",
    "[[CCE1:7Q4K9M2X]]",
    "[ [ cce1 : suelto",
]


@pytest.mark.parametrize("text", COLLIDING)
async def test_marker_collision_happens_before_inference_and_any_effect(a06: Harness, text):  # AC-A06-14
    cid = await a06.context()
    a06.adapter.on(text)
    with pytest.raises(MarkerCollisionError):
        await a06.ops().protect_text(
            "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
        )
    assert a06.adapter.calls == [] and await a06.rows("protection_mappings") == []


ORDINARY_PROSE = [
    "El [CCE report] está listo",
    "CCE1 es un código interno",
    "usar [[CCE1]] como alias",
    "cce: nota",
    "CCE1: nota breve",
    "[ CCE ] y Informe CCE",
]


@pytest.mark.parametrize("prose", ORDINARY_PROSE)
async def test_ordinary_prose_is_accepted_but_not_next_to_a_strict_marker(a06: Harness, prose):  # AC-A06-59
    text = f"{prose}. Contactar a {NAME}."
    cid, res = await a06.protect(text, f(text, NAME))
    assert res.text.startswith(prose) and len(res.markers) == 1
    back = await a06.restore(cid, res.text)  # prose coexists with a valid, authorized marker
    assert back.text == text and back.counts["markers_restored"] == 1
    mixed = f"{prose} [[CCE1:7Q4K9M2XJ8WH3NV5]] {NAME}"
    a06.adapter.on(mixed, f(mixed, NAME))
    with pytest.raises(MarkerCollisionError):
        await a06.ops().protect_text(
            "a", context_id=cid, kind="conversation", text=mixed, policy=PSEUDO, analysis=INLINE
        )


async def test_limits_text_fragment_mappings_bytes_contexts(migrated):  # AC-A06-15
    backend, url = migrated
    h = await make_harness(
        backend,
        url,
        setup=dict(
            limits=__import__("cawbase_content_engine.protection.types", fromlist=["x"]).ProtectionLimits(
                max_mappings_per_context=2, max_context_bytes=40, max_fragment_chars=15, max_active_contexts_per_scope=2
            )
        ),
    )
    try:
        long = "x" * 9000
        h.adapter.on(long)
        cid = await h.context()
        with pytest.raises(LimitExceededError):  # text beyond the A03 text limit: never truncated
            await h.ops().protect_text(
                "a", context_id=cid, kind="conversation", text=long, policy=PSEUDO, analysis=INLINE
            )
        text = "aaaaaaaaaaaaaaaaaaaaaaaaa y bbbbbbbbb y ccccc y ddddddddddd"
        too_long = ("personal_data", 0, 25)
        h.adapter.on(text, too_long)
        with pytest.raises(LimitExceededError):  # fragment over max_fragment_chars
            await h.ops().protect_text(
                "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
            )
        t2 = "bbbbbbbbb y ccccc y ddddddddddd"
        for n, frag in enumerate(("bbbbbbbbb", "ccccc")):
            h.adapter.on(t2, f(t2, frag))
            await h.ops().protect_text(
                "a", context_id=cid, kind="conversation", text=t2, policy=PSEUDO, analysis=INLINE
            )
            assert len(await h.rows("protection_mappings")) == n + 1
        h.adapter.on(t2, f(t2, "ddddddddddd"))
        with pytest.raises(QuotaExceededError):  # third mapping: over max_mappings_per_context
            await h.ops().protect_text(
                "a", context_id=cid, kind="conversation", text=t2, policy=PSEUDO, analysis=INLINE
            )
        assert len(await h.rows("protection_mappings")) == 2
        cid2 = await h.context()
        t3 = "zzzzzzzzzzzzzzz y wwwwwwwwwwwwwww y vvvvvvvvvvvvvvv"
        h.adapter.on(t3, f(t3, "zzzzzzzzzzzzzzz"), f(t3, "wwwwwwwwwwwwwww"), f(t3, "vvvvvvvvvvvvvvv"))
        with pytest.raises(QuotaExceededError):  # 45 bytes > max_context_bytes (40), and 3 mappings > 2
            await h.ops().protect_text(
                "a", context_id=cid2, kind="conversation", text=t3, policy=PSEUDO, analysis=INLINE
            )
        with pytest.raises(QuotaExceededError):  # third active context in the scope
            await h.context()
    finally:
        await h.engine.aclose()


async def test_permissions_are_separate(a06: Harness):  # AC-A06-24 (protection side)
    cid = await a06.context()
    a06.adapter.on(TEXT, f(TEXT, NAME))
    for perms in ([], [TP], [A06_ALL[4]], [A06_ALL[1], A06_ALL[4]]):
        with pytest.raises(PermissionDeniedError):
            await a06.ops(perms=perms).protect_text(
                "a", context_id=cid, kind="conversation", text=TEXT, policy=PSEUDO, analysis=INLINE
            )
    assert a06.adapter.calls == []
    ok = await a06.ops(perms=[TP, A06_ALL[4]]).protect_text(
        "a", context_id=cid, kind="conversation", text=TEXT, policy=PSEUDO, analysis=INLINE
    )
    assert ok.markers and find_markers(ok.text) == ok.markers


async def test_the_a03_mock_adapter_drives_protection_and_restoration(migrated):  # AC-A06-01, 10 (A03 mock)
    """The deterministic MOCK of A03 (fixtures bound to the exact text) supplies the detection; everything A06 does
    around it (binding, grouping, encryption, persistence, restoration) is real."""
    from cawbase_content_engine import ContentEngine, EngineConfig, ScopePermission
    from cawbase_content_engine.inference import InferenceSetup
    from cawbase_content_engine.protection.types import ProtectionSetup

    from .a03_helpers import AE, PR, fixture_for, mock, ok_output
    from .a06_helpers import Keys

    _, url = migrated
    keys = Keys()

    async def compose(adapter):
        eng = ContentEngine(
            EngineConfig(database_url=url),
            inference=InferenceSetup(adapter, timeout_s=10.0),
            protection=ProtectionSetup(keys=keys),
        )
        await eng.start()
        return eng

    plain = ContentEngine(EngineConfig(database_url=url))
    await plain.start()
    try:
        setup_ops = await build_profile(plain, "a")
        found = [("personal_data", NAME), ("password", PASSWORD)]
        outcome = {"kind": "output", "output": ok_output(TEXT, findings=found, classification=False)}
        first = await fixture_for(setup_ops, "a", outcome, text=TEXT, caps=("detection",), fid="original")
    finally:
        await plain.aclose()
    eng = await compose(mock(first))
    try:
        ops = eng.scoped(eng.trusted_scope_context("a", A06_ALL, actor="t"))
        cid = (await ops.create_context("a", kind="conversation")).context_id
        res = await ops.protect_text(
            "a", context_id=cid, kind="conversation", text=TEXT, policy=PSEUDO, analysis=INLINE
        )
        assert res.analysis.provenance.kind == "mock" and res.analysis.provenance.simulated is True
        assert NAME not in res.text and PASSWORD not in res.text and len(res.markers) == 2
        reply = f"Gracias {res.markers[0]}."  # the markers are random: the reply fixture is built now
    finally:
        await eng.aclose()
    plain = ContentEngine(EngineConfig(database_url=url))
    await plain.start()
    try:
        reader = plain.scoped(plain.trusted_scope_context("a", [PR, AE], actor="t"))
        out = {"kind": "output", "output": ok_output(reply, findings=[], classification=False)}
        second = await fixture_for(reader, "a", out, text=reply, caps=("detection",), fid="reply")
    finally:
        await plain.aclose()
    eng = await compose(mock(second))
    try:
        ops = eng.scoped(eng.trusted_scope_context("a", A06_ALL, actor="t"))
        back = await ops.restore_text(
            "a", context_id=cid, kind="conversation", text=reply, analysis=INLINE, delivered_markers=res.markers
        )
        assert back.text == f"Gracias {NAME}." and back.outcome == "restored"
        assert back.analysis.provenance.kind == "mock"
    finally:
        await eng.aclose()
    _ = ScopePermission
