"""A06 restoration (``restore_text``): per-response authorization, parser failures, new sensitive data in both modes,
single pass, scope-level access. AC-A06-26..38, 56..58, 63."""

from __future__ import annotations

import pytest

from cawbase_content_engine.errors import (
    ContextKindMismatchError,
    ContextUnavailableError,
    DetectionInconclusiveError,
    LimitExceededError,
    MarkerMalformedError,
    MarkerNotAuthorizedError,
    MarkerUnknownError,
    NewSensitiveDataError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ValidationFailedError,
)
from cawbase_content_engine.protection.markers import marker_of

from .a06_helpers import (
    INLINE,
    MAIL,
    NAME,
    PASSWORD,
    TEXT,
    Harness,
    capture_sql,
    expire_context,
    f,
    inject_mapping,
    make_harness,
    span,
    touches,
)

T16 = "7Q4K9M2XJ8WH3NV5"


async def three(h: Harness):
    fs = [f(TEXT, NAME), f(TEXT, MAIL), f(TEXT, PASSWORD, "password")]
    cid, res = await h.protect(TEXT, *fs)
    return cid, res, res.markers


async def test_restores_exactly_the_authorized_markers(a06: Harness):  # AC-A06-26
    cid, res, (m1, m2, m3) = await three(a06)
    response = f"Respuesta: {m1} / {m3}"
    a = await a06.restore(cid, response, delivered_markers=[m1, m3])
    assert a.text == f"Respuesta: {NAME} / {PASSWORD}" and a.outcome == "restored"
    assert (
        a.counts["markers_restored"] == 2 and a.counts["distinct_markers"] == 2 and a.counts["authorized_unused"] == 0
    )
    # via delivered_material (the texts handed to the model) the extraction is the same
    b = await a06.restore(cid, response, material=[res.text], delivered_markers=None)
    assert b.text == a.text and b.counts["authorized_unused"] == 1  # m2 was delivered but did not come back
    assert (await a06.restore(cid, f"{m1} {m1}", delivered_markers=[m1])).text == f"{NAME} {NAME}"


async def test_exactly_one_authorization_declaration_is_required(a06: Harness):
    cid, _res, (m1, *_r) = await three(a06)
    a06.adapter.on(m1)
    ops = a06.ops()
    for kw in ({}, {"delivered_markers": [m1], "delivered_material": [m1]}):
        with pytest.raises(ValidationFailedError):
            await ops.restore_text("a", context_id=cid, kind="conversation", text=m1, analysis=INLINE, **kw)
    for bad in ("string", [3], ["[[cce1:" + T16 + "]]"], {m1: 1}):
        with pytest.raises(ValidationFailedError):
            await ops.restore_text(
                "a", context_id=cid, kind="conversation", text=m1, analysis=INLINE, delivered_markers=bad
            )
    with pytest.raises(ValidationFailedError):
        await ops.restore_text(
            "a",
            context_id=cid,
            kind="conversation",
            text=m1,
            analysis=INLINE,
            on_new_findings="x",
            delivered_markers=[m1],
        )  # type: ignore[arg-type]


async def test_a_valid_marker_that_was_not_delivered_is_not_authorized_and_nothing_is_consulted(
    a06: Harness,
):  # AC-A06-27
    cid, _res, (m1, m2, _m3) = await three(a06)
    a06.adapter.on(f"{m1} {m2}")
    calls, keys = len(a06.adapter.calls), a06.keys.key_calls
    with capture_sql(a06.engine) as stmts:
        with pytest.raises(MarkerNotAuthorizedError):
            await a06.ops().restore_text(
                "a", context_id=cid, kind="conversation", text=f"{m1} {m2}", analysis=INLINE, delivered_markers=[m1]
            )
    assert stmts == [] and len(a06.adapter.calls) == calls and a06.keys.key_calls == keys  # no oracle, no work


async def test_unknown_and_foreign_markers_fail_closed_without_partial_text(a06: Harness):  # AC-A06-28
    cid, _res, (m1, *_r) = await three(a06)
    other, ores = await a06.protect("Hola Zoe", f("Hola Zoe", "Zoe"))
    foreign = ores.markers[0]
    unknown = marker_of(T16)
    for stray in (unknown, foreign):
        with pytest.raises(MarkerUnknownError):
            await a06.restore(cid, f"{m1} y {stray}", delivered_markers=[m1, stray])
    # the foreign marker still works in ITS context
    assert (await a06.restore(other, foreign, delivered_markers=[foreign])).text == "Zoe"


async def test_empty_authorized_set(a06: Harness):  # AC-A06-29
    cid, _res, (m1, *_r) = await three(a06)
    with pytest.raises(MarkerNotAuthorizedError):
        await a06.restore(cid, f"x {m1}", delivered_markers=[])
    calls = len(a06.adapter.calls)
    out = await a06.restore(cid, "sin marcadores", delivered_markers=[])
    assert out.outcome == "no_markers" and out.text == "sin marcadores"
    assert len(a06.adapter.calls) == calls + 1  # the response is still evaluated
    out2 = await a06.restore(cid, "sin marcadores", delivered_material=[])  # type: ignore[call-arg]
    assert out2.outcome == "no_markers"


ALTERED = [
    f"[[cce1:{T16.lower()}]]",
    f"[[CCE1: {T16}]]",
    f"[CCE1:{T16}]",
    f"CCE1:{T16}",
    "[[CCE1:7Q4K9M2XJ8WH3NVI]]",
    "[[CCE1:7Q4K9M2X]]",
]


@pytest.mark.parametrize("altered", ALTERED)
async def test_recognizable_alterations_fail_closed_before_any_work(a06: Harness, altered):  # AC-A06-30
    cid, _res, (m1, *_r) = await three(a06)
    keys = a06.keys.key_calls
    with capture_sql(a06.engine) as stmts:
        with pytest.raises(MarkerMalformedError):
            await a06.restore(cid, f"{m1} luego {altered}", delivered_markers=[m1])
    assert stmts == [] and a06.keys.key_calls == keys


UNRECOGNIZED = [
    f"[[CCE1‍:{T16}]]",
    f"［［CCE1：{T16}］］",
    "[CCE1:7Q4K9M2X]",
    "[[CCE 7Q4K9M2XJ8WH3NV5]]",
]


@pytest.mark.parametrize("weird", UNRECOGNIZED)
async def test_unrecognized_alterations_leave_text_unrestored_without_error(a06: Harness, weird):  # AC-A06-31
    cid, _res, (m1, *_r) = await three(a06)
    out = await a06.restore(cid, f"{weird}", delivered_markers=[m1])
    assert out.text == weird and out.outcome == "no_markers"
    assert out.counts["authorized_unused"] == 1 and out.counts["markers_restored"] == 0  # the host can see the absence


async def test_redacted_text_and_prose_are_returned_as_is(a06: Harness):  # AC-A06-32
    cid, _res, (m1, *_r) = await three(a06)
    text = f"[REDACTED] y [CCE report] y {m1}"
    out = await a06.restore(cid, text, delivered_markers=[m1])
    assert out.text == f"[REDACTED] y [CCE report] y {NAME}"


async def test_an_original_that_looks_like_markers_is_inserted_literally_once(a06: Harness):  # AC-A06-33
    cid = await a06.context()
    nasty = f"{marker_of(T16)} y [[CCE1:AAAAAAAAAAAAAAAA]]"
    marker = await inject_mapping(a06, cid, nasty)
    out = await a06.restore(cid, f"<{marker}>", delivered_markers=[marker])
    assert out.text == f"<{nasty}>" and out.counts["markers_restored"] == 1


async def test_new_sensitive_data_is_redacted_by_default_and_listed_without_values(a06: Harness):  # AC-A06-34
    cid, _res, (m1, m2, _m3) = await three(a06)
    response = f"Hola {m1}, mail {m2}. Nuevo contacto: Zoe Falsa en zoe.falsa@example.test."
    out = await a06.restore(
        cid,
        response,
        f(response, "Zoe Falsa"),
        f(response, "zoe.falsa@example.test"),
        delivered_markers=[m1, m2],
    )
    assert out.outcome == "restored_with_redactions"
    assert out.text == f"Hola {NAME}, mail {MAIL}. Nuevo contacto: [REDACTED] en [REDACTED]."
    assert out.counts["new_findings_redacted"] == 2 and out.counts["redacted_groups"] == 2
    assert [(r.type_id, r.start, r.end) for r in out.new_findings] == [
        ("personal_data", *span(response, "Zoe Falsa")),
        ("personal_data", *span(response, "zoe.falsa@example.test")),
    ]
    assert "Zoe" not in repr(out.new_findings) and len(await a06.rows("protection_mappings")) == 3  # nothing stored


async def test_new_data_is_always_redacted_even_when_the_protection_policy_would_pseudonymize(a06: Harness):
    cid, _res, (m1, *_r) = await three(a06)
    response = f"{m1} / Zoe Falsa"
    out = await a06.restore(cid, response, f(response, "Zoe Falsa"), delivered_markers=[m1])
    assert out.text.endswith("[REDACTED]") and len(await a06.rows("protection_mappings")) == 3


async def test_inconclusive_detection_on_the_response_decrypts_nothing(a06: Harness):  # AC-A06-35
    cid, _res, (m1, *_r) = await three(a06)
    response = f"x {m1}"
    a06.adapter.on(response)
    a06.adapter.mode[response] = ("inconclusive", "abstained")
    keys = a06.keys.key_calls
    with capture_sql(a06.engine) as stmts:
        with pytest.raises(DetectionInconclusiveError):
            await a06.ops().restore_text(
                "a", context_id=cid, kind="conversation", text=response, analysis=INLINE, delivered_markers=[m1]
            )
    assert touches(stmts, "protection_") == [] and a06.keys.key_calls == keys


async def test_findings_equal_to_or_inside_a_marker_are_ignored_in_both_modes(a06: Harness):  # AC-A06-36
    cid, _res, (m1, m2, _m3) = await three(a06)
    response = f"A {m1} B {m2} C"
    s1, e1 = span(response, m1)
    for mode in ("redact", "fail"):
        out = await a06.restore(
            cid,
            response,
            ("personal_data", s1, e1),  # exactly the marker
            ("password", s1 + 3, s1 + 9),  # inside the marker
            delivered_markers=[m1, m2],
            on_new_findings=mode,
        )
        assert out.text == f"A {NAME} B {MAIL} C" and out.outcome == "restored"
        assert out.counts["findings_ignored_inside_markers"] == 2 and out.new_findings == ()
        assert out.counts["new_findings_redacted"] == 0 and out.counts["markers_consumed_by_redaction"] == 0


async def test_context_deleted_or_expired_before_the_lock_is_unavailable(a06: Harness):  # AC-A06-37
    cid, _res, (m1, *_r) = await three(a06)
    await a06.ops().delete_context("a", cid)
    with pytest.raises(ContextUnavailableError):
        await a06.restore(cid, m1, delivered_markers=[m1])
    cid2, _r2, (n1, *_x) = await three(a06)
    await expire_context(a06, cid2)
    with pytest.raises(ContextUnavailableError):
        await a06.restore(cid2, n1, delivered_markers=[n1])


async def test_a_response_over_the_text_limit_is_rejected_never_truncated(a06: Harness):  # AC-A06-38
    cid, _res, (m1, *_r) = await three(a06)
    big = m1 + " " + "x" * 9000
    a06.adapter.on(big)
    with pytest.raises(LimitExceededError):
        await a06.ops().restore_text(
            "a", context_id=cid, kind="conversation", text=big, analysis=INLINE, delivered_markers=[m1]
        )


async def test_kind_must_match(a06: Harness):
    cid, _res, (m1, *_r) = await three(a06)
    with pytest.raises(ContextKindMismatchError):
        await a06.restore(cid, m1, delivered_markers=[m1], kind="job")


# ---- fail mode (AC-A06-56) -------------------------------------------------------------------------------
async def marker_response(h: Harness):
    cid, _res, (m1, m2, _m3) = await three(h)
    response = f"0123456789 {m1} {m2} tail text"
    return cid, response, m1, m2


@pytest.mark.parametrize("shape", ["outside", "crosses_left", "crosses_right", "covers", "spans_two", "adjacent"])
async def test_fail_mode_rejects_before_decrypting(a06: Harness, shape):  # AC-A06-56
    cid, response, m1, m2 = await marker_response(a06)
    s1, e1 = span(response, m1)
    s2, e2 = span(response, m2)
    finding = {
        "outside": (0, 8),
        "crosses_left": (s1 - 3, s1 + 4),
        "crosses_right": (e1 - 4, e1 + 1),
        "covers": (s1 - 2, e1 + 1),
        "spans_two": (s1 + 5, e2 - 5),
        "adjacent": (e2, e2 + 5),
    }[shape]
    a06.adapter.on(response, ("personal_data", *finding))
    keys = a06.keys.key_calls
    with capture_sql(a06.engine) as stmts:
        with pytest.raises(NewSensitiveDataError):
            await a06.ops().restore_text(
                "a",
                context_id=cid,
                kind="conversation",
                text=response,
                analysis=INLINE,
                delivered_markers=[m1, m2],
                on_new_findings="fail",
            )
    assert touches(stmts, "protection_") == [] and a06.keys.key_calls == keys  # no transaction, no mapping, no key


async def test_fail_mode_only_rejects_new_findings(a06: Harness):  # AC-A06-56
    cid, response, m1, m2 = await marker_response(a06)
    s1, e1 = span(response, m1)
    out = await a06.restore(
        cid, response, ("personal_data", s1, e1), delivered_markers=[m1, m2], on_new_findings="fail"
    )
    assert out.outcome == "restored" and NAME in out.text and MAIL in out.text


# ---- redact mode with crossings (AC-A06-57) --------------------------------------------------------------
@pytest.mark.parametrize("shape", ["left", "right", "covers", "two", "adjacent"])
async def test_redact_mode_crossings(a06: Harness, shape):  # AC-A06-57
    cid, response, m1, m2 = await marker_response(a06)
    s1, e1 = span(response, m1)
    s2, e2 = span(response, m2)
    assert e1 + 1 == s2
    finding, expected, consumed = {
        "left": ((s1 - 3, s1 + 4), response[: s1 - 3] + f"[REDACTED] {MAIL} tail text", 1),
        "right": ((e1 - 4, e1 + 1), response[:s1] + f"[REDACTED]{MAIL} tail text", 1),
        "covers": ((s1 - 2, e1 + 1), response[: s1 - 2] + f"[REDACTED]{MAIL} tail text", 1),
        "two": ((s1 + 5, e2 - 5), response[:s1] + "[REDACTED] tail text", 2),
        "adjacent": ((e2, e2 + 5), f"0123456789 {NAME} {MAIL}[REDACTED] text", 0),
    }[shape]
    out = await a06.restore(cid, response, ("personal_data", *finding), delivered_markers=[m1, m2])
    assert out.text == expected and out.outcome == "restored_with_redactions"
    assert out.counts["markers_consumed_by_redaction"] == consumed and out.counts["markers_restored"] == 2 - consumed
    assert out.counts["redacted_groups"] == 1 and out.counts["new_findings_redacted"] == 1
    if consumed:
        assert NAME not in out.text and m1 not in out.text


async def test_consumed_markers_are_never_looked_up_or_decrypted(a06: Harness):  # AC-A06-57
    cid, response, m1, m2 = await marker_response(a06)
    s1, e1 = span(response, m1)
    s2, e2 = span(response, m2)
    keys = a06.keys.key_calls
    out = await a06.restore(cid, response, ("personal_data", s1 + 2, e2 - 2), delivered_markers=[m1, m2])
    assert out.text == "0123456789 [REDACTED] tail text" and out.counts["markers_restored"] == 0
    assert a06.keys.key_calls == keys  # nothing was decrypted: no key was even requested


async def test_transitive_chains_in_redact_mode(a06: Harness):  # AC-A06-58
    cid, response, m1, m2 = await marker_response(a06)
    s1, e1 = span(response, m1)
    s2, e2 = span(response, m2)
    # f1 crosses m1 from the left, f2 bridges m1 and the text between, f3 reaches into m2: one group
    findings = [
        ("personal_data", s1 - 3, s1 + 2),
        ("password", e1 - 2, e1 + 1),
        ("personal_data", e1, s2 + 3),
        ("password", s1 + 4, s1 + 9),  # inside m1: ignored, takes no part
    ]
    out = await a06.restore(cid, response, *findings, delivered_markers=[m1, m2])
    assert out.text == response[: s1 - 3] + "[REDACTED] tail text"
    assert out.counts["redacted_groups"] == 1 and out.counts["markers_consumed_by_redaction"] == 2
    assert out.counts["findings_ignored_inside_markers"] == 1 and out.counts["new_findings_redacted"] == 3
    # an ignored finding inside an untouched marker does not drag it into the group
    only_inner = await a06.restore(cid, response, ("password", s2 + 2, s2 + 8), delivered_markers=[m1, m2])
    assert only_inner.outcome == "restored" and only_inner.counts["markers_consumed_by_redaction"] == 0
    # fail mode rejects the chain
    with pytest.raises(NewSensitiveDataError):
        await a06.restore(cid, response, *findings, delivered_markers=[m1, m2], on_new_findings="fail")


async def test_all_markers_consumed_still_reports_redactions(a06: Harness):
    cid, response, m1, m2 = await marker_response(a06)
    s1, _ = span(response, m1)
    _, e2 = span(response, m2)
    out = await a06.restore(cid, response, ("personal_data", s1 - 1, e2 + 1), delivered_markers=[m1, m2])
    assert out.outcome == "restored_with_redactions" and out.counts["markers_restored"] == 0


# ---- scope-level access (AC-A06-63) ------------------------------------------------------------------------
async def test_text_restore_is_scope_level_with_no_internal_acl(a06: Harness):  # AC-A06-63
    from cawbase_content_engine.errors import ResourceNotAccessibleError

    cid, _res, (m1, *_r) = await three(a06)
    a06.adapter.on(m1)
    # a second holder of text:restore in the SAME scope can use the context id: access control per
    # conversation is the host's job before it delegates
    from cawbase_content_engine import ScopePermission as SP

    second = a06.engine.scoped(
        a06.engine.trusted_scope_context("a", [SP.TEXT_RESTORE, SP.ANALYSIS_EXECUTE], actor="another-user")
    )
    out = await second.restore_text(
        "a", context_id=cid, kind="conversation", text=m1, analysis=INLINE, delivered_markers=[m1]
    )
    assert out.text == NAME
    with pytest.raises(PermissionDeniedError):  # without text:restore nothing is allowed
        await a06.ops(perms=[SP.ANALYSIS_EXECUTE]).restore_text(
            "a", context_id=cid, kind="conversation", text=m1, analysis=INLINE, delivered_markers=[m1]
        )
    other = await make_harness(a06.backend, a06.url, scopes=("b",), keys=a06.keys)
    try:
        with pytest.raises(ResourceNotAccessibleError):  # asking for scope "a" with a context of scope "b"
            await other.ops("b").restore_text(
                "a", context_id=cid, kind="conversation", text=m1, analysis=INLINE, delivered_markers=[m1]
            )
        other.adapter.on(m1)
        with pytest.raises(ResourceNotFoundError):  # same id under the caller's own scope: indistinguishable
            await other.ops("b").restore_text(
                "b", context_id=cid, kind="conversation", text=m1, analysis=INLINE, delivered_markers=[m1]
            )
    finally:
        await other.engine.aclose()
