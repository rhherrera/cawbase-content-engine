"""Strict validation of the raw adapter output (docs/inference-and-analysis.md, section 5).

Any violation rejects the *whole* output (``inference_output_invalid``); nothing is repaired, no
element is dropped to return the rest. ``problems`` carry only codes and numeric positions.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import InferenceOutputInvalidError
from .adapter import CAPABILITIES, INFERENCE_CONTRACT_VERSION, InferenceRequest
from .prepared import PreparedAnalysis

REASONS = ("partial_coverage", "abstained", "truncated")
STATUSES = ("completed", "inconclusive")
_MAX_PROBLEMS = 20


@dataclass(frozen=True)
class ValidatedCoverage:
    groups: tuple[tuple[tuple[str, ...], tuple[tuple[int, int], ...]], ...]  # (ids, regions)
    evaluated: frozenset[str]
    complete: bool


@dataclass(frozen=True)
class ValidatedCapability:
    status: str
    reason: str | None
    coverage: ValidatedCoverage
    scores: tuple[tuple[str, float], ...] = ()  # classification
    findings: tuple[tuple[str, int, int], ...] = ()  # detection (type_id, start, end)


class _Problems:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, code: str, capability: str | None = None, index: int | None = None) -> None:
        if len(self.items) >= _MAX_PROBLEMS:
            return
        item = {"code": code}
        if capability:
            item["capability"] = capability
        if index is not None:
            item["index"] = str(index)
        self.items.append(item)

    def __bool__(self) -> bool:
        return bool(self.items)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _keys(obj: Any, allowed: set[str], required: set[str], pr: _Problems, cap: str | None) -> bool:
    if not isinstance(obj, dict) or not all(isinstance(k, str) for k in obj):
        pr.add("invalid_type", cap)
        return False
    ok = True
    if set(obj) - allowed:
        pr.add("unknown_field", cap)
        ok = False
    if required - set(obj):
        pr.add("missing_field", cap)
        ok = False
    return ok


def validate_output(raw: Any, prepared: PreparedAnalysis, request: InferenceRequest) -> dict[str, ValidatedCapability]:
    """Return the validated capabilities (executed ones only) or raise ``InferenceOutputInvalidError``."""
    pr = _Problems()
    # canonical JSON round trip: proves serializability, enforces the size limit and yields a private copy
    if not isinstance(raw, Mapping):
        raise InferenceOutputInvalidError([{"code": "output_not_object"}])
    try:
        dumped = json.dumps(dict(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        raise InferenceOutputInvalidError([{"code": "output_not_serializable"}]) from None
    if len(dumped) > request.limits.max_output_bytes:
        raise InferenceOutputInvalidError([{"code": "output_too_large"}])
    out = json.loads(dumped)

    text = request.text
    n = len(text)
    executed = request.capabilities
    result: dict[str, ValidatedCapability] = {}
    if _keys(out, {"contract_version", *CAPABILITIES}, {"contract_version"}, pr, None):
        if not _is_int(out["contract_version"]) or out["contract_version"] != INFERENCE_CONTRACT_VERSION:
            pr.add("contract_version_mismatch")
        for cap in CAPABILITIES:
            if cap in out and cap not in executed:
                pr.add("capability_not_executed", cap)
            elif cap in executed and cap not in out:
                pr.add("capability_missing", cap)
            elif cap in executed:
                v = _capability(cap, out[cap], prepared, request, text, n, pr)
                if v is not None:
                    result[cap] = v
    if pr:
        raise InferenceOutputInvalidError(pr.items)
    return result


def _capability(
    cap: str, obj: Any, prepared: PreparedAnalysis, request: InferenceRequest, text: str, n: int, pr: _Problems
) -> ValidatedCapability | None:
    extra = "scores" if cap == "classification" else "findings"
    if not _keys(obj, {"status", "reason", "coverage", extra}, {"status", "reason", "coverage", extra}, pr, cap):
        return None
    before = len(pr.items)
    status, reason = obj["status"], obj["reason"]
    if not isinstance(status, str) or status not in STATUSES:
        pr.add("invalid_status", cap)
        status = None
    if status == "completed" and reason is not None:
        pr.add("reason_invalid", cap)
    elif status == "inconclusive" and (not isinstance(reason, str) or reason not in REASONS):
        pr.add("reason_invalid", cap)
    elif status is not None and reason is not None and not isinstance(reason, str):
        pr.add("reason_invalid", cap)
    ids = set(prepared.element_ids(cap))
    cov = _coverage(cap, obj["coverage"], ids, n, request.limits.max_regions, pr)
    if cov is None:
        return None
    if status == "completed" and not cov.complete:
        pr.add("status_coverage_mismatch", cap)
    if reason == "partial_coverage" and cov.complete:
        pr.add("reason_coverage_mismatch", cap)
    scores: tuple[tuple[str, float], ...] = ()
    findings: tuple[tuple[str, int, int], ...] = ()
    if cap == "classification":
        scores = _scores(obj["scores"], ids, cov, pr)
    else:
        findings = _findings(obj["findings"], ids, cov, text, n, request, pr)
    if len(pr.items) > before or status is None:
        return None
    return ValidatedCapability(status, reason, cov, scores, findings)


def _coverage(cap: str, obj: Any, ids: set[str], n: int, max_regions: int, pr: _Problems) -> ValidatedCoverage | None:
    if not _keys(obj, {"groups"}, {"groups"}, pr, cap):
        return None
    groups = obj["groups"]
    if not isinstance(groups, list):
        pr.add("invalid_type", cap)
        return None
    seen: set[str] = set()
    total_regions = 0
    out: list[tuple[tuple[str, ...], tuple[tuple[int, int], ...]]] = []
    ok = True
    for gi, g in enumerate(groups):
        if not _keys(g, {"ids", "regions"}, {"ids", "regions"}, pr, cap):
            ok = False
            continue
        gids, regions = g["ids"], g["regions"]
        if not isinstance(gids, list) or not isinstance(regions, list):
            pr.add("invalid_type", cap, gi)
            ok = False
            continue
        if not gids or not regions:
            pr.add("empty_group", cap, gi)
            ok = False
            continue
        gset: list[str] = []
        for x in gids:
            if not isinstance(x, str):
                pr.add("invalid_type", cap, gi)
                ok = False
            elif x not in ids:
                pr.add("unknown_id", cap, gi)  # never echo the id
                ok = False
            elif x in seen or x in gset:
                pr.add("duplicate_id", cap, gi)
                ok = False
            else:
                gset.append(x)
        seen.update(gset)
        regs: list[tuple[int, int]] = []
        for r in regions:
            if not _keys(r, {"start", "end"}, {"start", "end"}, pr, cap):
                ok = False
                continue
            s, e = r["start"], r["end"]
            if not _is_int(s) or not _is_int(e):
                pr.add("invalid_type", cap, gi)
                ok = False
            elif not (0 <= s < e <= n):
                pr.add("region_out_of_range", cap, gi)
                ok = False
            else:
                regs.append((s, e))
        total_regions += len(regions)
        if total_regions > max_regions:
            pr.add("regions_limit_exceeded", cap)
            return None
        if ok and any(regs[i + 1][0] <= regs[i][1] for i in range(len(regs) - 1)):
            pr.add("regions_not_canonical", cap, gi)
            ok = False
        out.append((tuple(sorted(gset)), tuple(regs)))
    if not ok:
        return None
    out.sort(key=lambda g: g[0][0])
    complete = seen == ids and all(regs == ((0, n),) for _, regs in out)
    return ValidatedCoverage(tuple(out), frozenset(seen), complete)


def _scores(raw: Any, ids: set[str], cov: ValidatedCoverage, pr: _Problems) -> tuple[tuple[str, float], ...]:
    cap = "classification"
    if not isinstance(raw, list):
        pr.add("invalid_type", cap)
        return ()
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    clean = True
    for i, item in enumerate(raw):
        if not _keys(item, {"category_id", "score"}, {"category_id", "score"}, pr, cap):
            clean = False
            continue
        cid, score = item["category_id"], item["score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not (0 <= score <= 1)
        ):
            pr.add("invalid_score", cap, i)
            clean = False
        elif not isinstance(cid, str) or cid not in ids:
            pr.add("unknown_id", cap, i)
            clean = False
        elif cid in seen:
            pr.add("duplicate_id", cap, i)
            clean = False
        else:
            seen.add(cid)
            out.append((cid, float(score)))
    if clean and seen != set(cov.evaluated):
        pr.add("scores_coverage_mismatch", cap)
    return tuple(out)


def _findings(
    raw: Any, ids: set[str], cov: ValidatedCoverage, text: str, n: int, request: InferenceRequest, pr: _Problems
) -> tuple[tuple[str, int, int], ...]:
    cap = "detection"
    if not isinstance(raw, list):
        pr.add("invalid_type", cap)
        return ()
    if len(raw) > request.limits.max_findings:
        pr.add("findings_limit_exceeded", cap)
        return ()
    regions_of: dict[str, tuple[tuple[int, int], ...]] = {}
    for gids, regs in cov.groups:
        for x in gids:
            regions_of[x] = regs
    seen: set[tuple[str, int, int]] = set()
    out: list[tuple[str, int, int]] = []
    for i, f in enumerate(raw):
        if not _keys(f, {"type_id", "start", "end", "text"}, {"type_id", "start", "end", "text"}, pr, cap):
            continue
        tid, s, e, frag = f["type_id"], f["start"], f["end"], f["text"]
        if not isinstance(tid, str) or tid not in ids:
            pr.add("unknown_id", cap, i)
            continue
        if not _is_int(s) or not _is_int(e) or not isinstance(frag, str):
            pr.add("invalid_offset", cap, i)
            continue
        if not (0 <= s < e <= n):
            pr.add("offset_out_of_range", cap, i)
            continue
        if e - s > request.limits.max_finding_chars:
            pr.add("finding_too_long", cap, i)
            continue
        if text[s:e] != frag:
            pr.add("fragment_mismatch", cap, i)
            continue
        if not any(rs <= s and e <= re_ for rs, re_ in regions_of.get(tid, ())):
            pr.add("finding_outside_coverage", cap, i)
            continue
        key = (tid, s, e)
        if key in seen:
            pr.add("duplicate_finding", cap, i)
            continue
        seen.add(key)
        out.append(key)
    return tuple(out)
