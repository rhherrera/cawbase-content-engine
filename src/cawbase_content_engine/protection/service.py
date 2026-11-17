"""Orchestration of ``protect_text`` and ``restore_text``: validate, parse (pure), obtain bound findings, decide.

The order is the contract's (docs/text-protection.md, 5.7 and 7.1): nothing touches the database or the key
provider before the pure checks that can reject the call have run.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .. import validation as v
from ..errors import (
    EngineError,
    InternalError,
    LimitExceededError,
    MarkerCollisionError,
    MarkerMalformedError,
    MarkerNotAuthorizedError,
    NewSensitiveDataError,
    ValidationFailedError,
)
from ..inference.service import check_text
from . import ops, sources
from .grouping import classify_restore, plan_protection
from .markers import is_strict, markers_of_all, scan, token_of
from .types import (
    Kind,
    NewFindingRegion,
    ProtectedGroup,
    ProtectedText,
    ProtectionPolicy,
    ProtectionSetup,
    RestoredText,
)

if TYPE_CHECKING:
    from ..engine import ContentEngine

log = logging.getLogger("cawbase_content_engine.protection")
_CONTEXT_ID = re.compile(r"^ctx_[0-9a-f]{32}$")
_ACTIONS = ("pseudonymize", "redact")


def check_context_id(value: Any) -> str:
    if not isinstance(value, str) or not _CONTEXT_ID.fullmatch(value):
        raise ValidationFailedError("Invalid context_id.", field="context_id")
    return value


def check_kind(value: Any) -> Kind:
    return v.choice(value, "kind", ("conversation", "job"))  # type: ignore[return-value]


def check_policy(value: Any) -> ProtectionPolicy:
    if not isinstance(value, ProtectionPolicy):
        raise ValidationFailedError("Invalid policy.", field="policy")
    v.choice(value.default_action, "default_action", _ACTIONS)
    if not isinstance(value.actions, Mapping):
        raise ValidationFailedError("Invalid policy actions.", field="policy")
    for type_id, action in value.actions.items():
        v.slug(type_id, "policy")
        v.choice(action, "policy", _ACTIONS)
    return value


def _timed(op: str, scope_id: str, context_id: str, kind: str, rid: str | None) -> Any:
    class _T:
        def __init__(self) -> None:
            self.t0 = time.monotonic()

        def ok(self, **counts: int) -> None:
            log.info(
                f"{op}_completed",
                extra={
                    "scope_id": scope_id,
                    "context_id": context_id,
                    "kind": kind,
                    "request_id": rid,
                    "duration_ms": int((time.monotonic() - self.t0) * 1000),
                    **counts,
                },
            )

        def fail(self, exc: BaseException) -> None:
            log.info(
                f"{op}_failed",
                extra={
                    "scope_id": scope_id,
                    "context_id": context_id,
                    "kind": kind,
                    "request_id": rid,
                    "error_code": exc.code if isinstance(exc, EngineError) else "internal_error",
                    "duration_ms": int((time.monotonic() - self.t0) * 1000),
                },
            )

    return _T()


async def protect_text(
    engine: ContentEngine,
    setup: ProtectionSetup,
    scope_id: str,
    *,
    context_id: Any,
    kind: Any,
    text: Any,
    policy: Any,
    analysis: Any,
    request_id: Any,
) -> ProtectedText:
    lim = engine.config.limits
    body = check_text(text, "text", lim.max_analysis_text_chars, lim.max_analysis_text_bytes)
    cid, knd, pol = check_context_id(context_id), check_kind(kind), check_policy(policy)
    source = sources.check_source(analysis)
    rid = None if request_id is None else v.request_id(request_id)
    timer = _timed("protect", scope_id, cid, knd, rid)
    try:
        # 5.7: a strict literal or a recognizable alteration in the INPUT is a collision, before any effect
        parsed = scan(body)
        if parsed.strict or parsed.altered:
            raise MarkerCollisionError()
        resolved = await sources.resolve(engine, scope_id, source, body, request_id=rid)
        groups = plan_protection(resolved.findings, len(body), pol.default_action, pol.actions)
        for g in groups:
            if g.action == "pseudonymize" and g.end - g.start > setup.limits.max_fragment_chars:
                raise LimitExceededError("A fragment exceeds the maximum length.", field="text")
        out, details, markers, counts, expires_at = await ops.protect_decide(
            engine, setup, scope_id, cid, knd, body, groups, resolved.job
        )
    except EngineError as exc:
        timer.fail(exc)
        raise
    except Exception as exc:
        timer.fail(exc)
        raise InternalError() from None
    timer.ok(**counts)
    return ProtectedText(
        contract_version=1,
        scope_id=scope_id,
        context_id=cid,
        kind=knd,
        text=out,
        outcome="protected" if groups else "no_findings",
        groups=tuple(ProtectedGroup(**d) for d in details),
        markers=markers,
        counts=counts,
        analysis=resolved.ref,
        context_expires_at=expires_at,
    )


def _authorized(engine: ContentEngine, setup: ProtectionSetup, material: Any, delivered: Any) -> set[str]:
    """Exactly one of the two host declarations; the content of the model never builds this set (docs 7.2)."""
    if (material is None) == (delivered is None):
        raise ValidationFailedError("Give exactly one of delivered_material and delivered_markers.", field="delivered")
    lim = setup.limits
    if material is not None:
        if isinstance(material, (str, bytes, Mapping)) or not isinstance(material, Collection):
            raise ValidationFailedError("Invalid delivered_material.", field="delivered_material")
        items = list(material)
        if any(not isinstance(x, str) for x in items):
            raise ValidationFailedError("Invalid delivered_material.", field="delivered_material")
        if sum(len(x) for x in items) > lim.max_delivered_material_chars:
            raise LimitExceededError("delivered_material exceeds the maximum size.", field="delivered_material")
        return markers_of_all(items)
    if isinstance(delivered, (str, bytes, Mapping)) or not isinstance(delivered, Collection):
        raise ValidationFailedError("Invalid delivered_markers.", field="delivered_markers")
    items = list(delivered)
    if len(items) > lim.max_delivered_markers:
        raise LimitExceededError("Too many delivered markers.", field="delivered_markers")
    if any(not is_strict(x) for x in items):
        raise ValidationFailedError("Invalid delivered_markers.", field="delivered_markers")
    return set(items)


async def restore_text(
    engine: ContentEngine,
    setup: ProtectionSetup,
    scope_id: str,
    *,
    context_id: Any,
    kind: Any,
    text: Any,
    analysis: Any,
    delivered_material: Any,
    delivered_markers: Any,
    on_new_findings: Any,
    request_id: Any,
) -> RestoredText:
    lim = engine.config.limits
    body = check_text(text, "text", lim.max_analysis_text_chars, lim.max_analysis_text_bytes)
    cid, knd = check_context_id(context_id), check_kind(kind)
    mode = v.choice(on_new_findings, "on_new_findings", ("redact", "fail"))
    source = sources.check_source(analysis)
    authorized = _authorized(engine, setup, delivered_material, delivered_markers)
    rid = None if request_id is None else v.request_id(request_id)
    timer = _timed("restore", scope_id, cid, knd, rid)
    try:
        # 7.1 steps 2-3 (pure; no database, no keys): syntax, then the authorization of THIS response
        parsed = scan(body)
        if parsed.altered:
            raise MarkerMalformedError()
        in_response = {m for _, _, m in parsed.strict}
        if any(m not in authorized for m in in_response):
            raise MarkerNotAuthorizedError()
        # step 4: sensitive data in the response AS THE MODEL WROTE IT (markers still in place)
        resolved = await sources.resolve(engine, scope_id, source, body, request_id=rid)
        # step 5 (pure): classify; `fail` rejects BEFORE any decryption, key use or mapping read
        plan = classify_restore([(s, e) for s, e, _ in parsed.strict], resolved.findings, len(body))
        if plan.new_findings and mode == "fail":
            raise NewSensitiveDataError()
        tokens = sorted({token_of(m) for i, (_, _, m) in enumerate(parsed.strict) if i not in plan.consumed})
        # step 6: one decision transaction decrypts exactly the markers that will be restored
        originals = await ops.restore_decide(engine, setup, scope_id, cid, knd, tokens, resolved.job)
        # step 7 (pure): a single pass; inserted originals are never examined again
        out, restored = ops.assemble_restored(body, parsed.strict, plan.consumed, plan.groups, originals)
    except EngineError as exc:
        timer.fail(exc)
        raise
    except Exception as exc:
        timer.fail(exc)
        raise InternalError() from None
    counts = {
        "markers_restored": restored,
        "distinct_markers": len(in_response),
        "authorized_unused": len(authorized - in_response),
        "markers_consumed_by_redaction": len(plan.consumed),
        "new_findings_redacted": len(plan.new_findings),
        "findings_ignored_inside_markers": plan.ignored,
        "redacted_groups": len(plan.groups),
    }
    timer.ok(**counts)
    outcome = "restored_with_redactions" if plan.groups else ("restored" if restored else "no_markers")
    return RestoredText(
        contract_version=1,
        scope_id=scope_id,
        context_id=cid,
        kind=knd,
        text=out,
        outcome=outcome,  # type: ignore[arg-type]
        counts=counts,
        new_findings=tuple(NewFindingRegion(t, s, e) for t, s, e in plan.new_findings),
        analysis=resolved.ref,
    )


_ = Sequence
