"""``analyze_text`` orchestration: prepare -> release connection -> infer -> validate -> build."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

from .. import validation as v
from ..errors import (
    CapabilitiesNotConfiguredError,
    EngineError,
    InferenceFixtureNotFoundError,
    InferenceOutputInvalidError,
    InferenceTimeoutError,
    InferenceUnavailableError,
    InternalError,
    LimitExceededError,
    ValidationFailedError,
)
from ..operations.selection import select_published_version
from .adapter import CAPABILITIES, InferenceSetup
from .build import build_result
from .output import PROBLEM_CODES, validate_output
from .prepared import build_request, prepare
from .results import AnalysisResult

if TYPE_CHECKING:
    from ..engine import ContentEngine

log = logging.getLogger("cawbase_content_engine.analysis")


def check_text(value: Any, field: str, max_chars: int, max_bytes: int) -> str:
    """Exact text: no normalization or trimming. Empty and lone surrogates are invalid."""
    if not isinstance(value, str) or isinstance(value, bool):
        raise ValidationFailedError(f"Invalid {field}.", field=field)
    if not value:
        raise ValidationFailedError(f"{field} must not be empty.", field=field)
    if len(value) > max_chars:
        raise LimitExceededError(f"{field} exceeds the maximum length.", field=field)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationFailedError(f"{field} is not valid Unicode.", field=field) from None
    if len(encoded) > max_bytes:
        raise LimitExceededError(f"{field} exceeds the maximum size.", field=field)
    return str(value)  # a plain str copy


def check_capabilities(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Collection) or not value:
        raise ValidationFailedError("Invalid capabilities.", field="capabilities")
    items = list(value)
    if any(not isinstance(c, str) or c not in CAPABILITIES for c in items) or len(set(items)) != len(items):
        raise ValidationFailedError("Invalid capabilities.", field="capabilities")
    return tuple(c for c in CAPABILITIES if c in items)


def _safe_problems(raw: Any) -> list[dict[str, str]]:
    """Adapter-supplied diagnostics are data: keep them only if they fit the closed catalog and limits."""
    generic = [{"code": "adapter_diagnostic_invalid"}]
    if not isinstance(raw, list) or len(raw) > 20:
        return generic
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or not set(item) <= {"code", "capability", "index"}:
            return generic
        code, cap, idx = item.get("code"), item.get("capability"), item.get("index")
        if not isinstance(code, str) or code not in PROBLEM_CODES:
            return generic
        entry = {"code": code}
        if cap is not None:
            if not isinstance(cap, str) or cap not in CAPABILITIES:
                return generic
            entry["capability"] = cap
        if idx is not None:
            if not isinstance(idx, str) or not idx.isascii() or not idx.isdigit() or len(idx) > 6:
                return generic
            entry["index"] = idx
        out.append(entry)
    return out


def _protocol_error(exc: Exception) -> EngineError:
    """The adapter's error protocol is closed: unavailable, timeout, invalid output, fixture-not-found.

    Each is rebuilt with the generic public message and only validated attributes; nothing the adapter
    supplied (message, field, problems, request id, ...) is propagated as is. Any other error, including
    other ``EngineError`` subclasses, is an unexpected adapter failure (``internal_error``).
    """
    if isinstance(exc, InferenceUnavailableError):
        reason = getattr(exc, "reason", None)
        return InferenceUnavailableError(reason if reason in InferenceUnavailableError.REASONS else "unreachable")
    if isinstance(exc, InferenceTimeoutError):
        return InferenceTimeoutError()
    if isinstance(exc, InferenceOutputInvalidError):
        return InferenceOutputInvalidError(_safe_problems(getattr(exc, "problems", None)))
    if isinstance(exc, InferenceFixtureNotFoundError):
        return InferenceFixtureNotFoundError()
    log.error("adapter_failed", extra={"error_class": type(exc).__name__})
    return InternalError()


async def _invoke(setup: InferenceSetup, request: Any) -> Any:
    """Call the adapter in the caller's own task under a timeout: cancellation reaches it, nothing is orphaned."""
    failure: EngineError
    try:
        async with asyncio.timeout(setup.timeout_s):
            return await setup.adapter.infer(request)
    except TimeoutError:
        # our own deadline, or the adapter's own timeout: either way no valid answer
        failure = InferenceTimeoutError()
    except Exception as exc:  # CancelledError is a BaseException and propagates untouched
        failure = _protocol_error(exc)
    raise failure  # raised outside the handler: no chained context carrying adapter-supplied text


async def run_analysis(
    engine: ContentEngine,
    *,
    scope_id: str,
    text: Any,
    profile_id: Any,
    version_number: Any,
    capabilities: Any,
    context: Any,
    request_id: str | None,
) -> AnalysisResult:
    lim = engine.config.limits
    # 3. input validation (the caller has been authorized and the host transaction rejected already)
    text = check_text(text, "text", lim.max_analysis_text_chars, lim.max_analysis_text_bytes)
    if context is not None:
        context = check_text(context, "context", lim.max_analysis_context_chars, lim.max_analysis_context_bytes)
    requested = check_capabilities(capabilities)
    pid = None if profile_id is None else v.slug(profile_id, "profile_id")
    num = None if version_number is None else v.int_in(version_number, "version_number", 1)
    if num is not None and pid is None:
        raise ValidationFailedError("version_number needs a profile_id.", field="version_number")
    # 4. inference configured
    setup = engine.inference
    if setup is None:
        from ..errors import InferenceNotConfiguredError

        raise InferenceNotConfiguredError()
    rid = request_id or uuid.uuid4().hex
    started = time.monotonic()
    t = engine.tables
    try:
        # 5. preparation: one short engine-owned read transaction, finished (connection returned to the
        #    pool) before the adapter is called. Successive reads, not an atomic snapshot.
        resolved = await engine._run(
            lambda c: select_published_version(c, t, scope_id=scope_id, profile_id=pid, version_number=num),
            write=False,
            tx=None,
        )
        prepared = prepare(resolved)
        executable = tuple(c for c in requested if prepared.configured(c))
        if not executable:
            raise CapabilitiesNotConfiguredError()
        request = build_request(
            prepared, request_id=rid, text=text, context=context, capabilities=executable, limits=lim
        )
        # 7. adapter (no connection, no transaction held), then strict validation and core-side results
        raw = await _invoke(setup, request)
        validated = validate_output(raw, prepared, request)
        result = build_result(
            request_id=rid,
            prepared=prepared,
            requested=requested,
            validated=validated,
            text_length=len(text),
            context_provided=context is not None,
            descriptor=setup.descriptor,
        )
    except asyncio.CancelledError:
        log.info("analysis_cancelled", extra={"request_id": rid})
        raise
    except EngineError as exc:
        log.info(
            "analysis_failed",
            extra={"request_id": rid, "error_code": exc.code, "duration_ms": int((time.monotonic() - started) * 1000)},
        )
        raise
    except Exception as exc:
        log.error("analysis_failed", extra={"request_id": rid, "error_class": type(exc).__name__})
        raise InternalError() from None
    log.info(
        "analysis_completed",
        extra={
            "request_id": rid,
            "status": result.status,
            "adapter_kind": setup.descriptor.kind,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return result
