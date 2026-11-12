"""Core-side construction of the public result: eligibility, ranking, resolution, coverage, states.

The adapter's output is only an input here: the winner, the global coverage and the global status
are always computed by the core. Comparisons are exact (see ``exact``).
"""

from __future__ import annotations

from fractions import Fraction

from ..errors import InternalError
from .adapter import ANALYSIS_CONTRACT_VERSION, CAPABILITIES, AdapterDescriptor
from .output import ValidatedCapability, ValidatedCoverage
from .prepared import PreparedAnalysis
from .results import (
    AnalysisResult,
    CapabilityNotRun,
    ClassificationResult,
    Coverage,
    CoverageGroup,
    DetectionResult,
    FindingLocation,
    InputInfo,
    ModelRef,
    ProfileRef,
    Provenance,
    RankedCategory,
    Region,
    Resolution,
    Scale,
)


def exact(x: float | int) -> Fraction:
    """Exact rational of the shortest decimal representation of ``x`` (``repr``).

    Independent of the host's ``decimal`` context (no rounding) and of binary floating point:
    ``exact(0.9) - exact(0.7) == exact(0.2)``.
    """
    return Fraction(repr(float(x)))


def _coverage(cov: ValidatedCoverage, all_ids: tuple[str, ...], n: int) -> Coverage:
    return Coverage(
        complete=cov.complete,
        text_length=n,
        groups=tuple(
            CoverageGroup(ids=ids, regions=tuple(Region(start=s, end=e) for s, e in regs)) for ids, regs in cov.groups
        ),
        not_evaluated_ids=tuple(sorted(set(all_ids) - set(cov.evaluated))),
    )


def rank_scores(scores: tuple[tuple[str, float], ...], threshold: float) -> list[RankedCategory]:
    exact_scores = {cid: exact(s) for cid, s in scores}
    thr = exact(threshold)
    ordered = sorted(scores, key=lambda cs: (-exact_scores[cs[0]], cs[0]))
    ranked: list[RankedCategory] = []
    for cid, score in ordered:
        higher = sum(1 for v in exact_scores.values() if v > exact_scores[cid])
        ranked.append(RankedCategory(rank=1 + higher, category_id=cid, score=score, eligible=exact_scores[cid] >= thr))
    return ranked


def resolve(ranked: list[RankedCategory], tolerance: float, prepared: PreparedAnalysis) -> Resolution:
    eligible = [r for r in ranked if r.eligible]
    if not eligible:
        return Resolution(selected_category_id=None, reason="no_eligible_category", candidate_category_ids=())
    top = max(exact(r.score) for r in eligible)
    tol = exact(tolerance)
    candidates = [r for r in eligible if top - exact(r.score) <= tol]  # distance to the maximum, not chained
    if len(candidates) == 1:
        return Resolution(
            selected_category_id=candidates[0].category_id,
            reason="highest_score",
            candidate_category_ids=(candidates[0].category_id,),
        )
    prios = []
    for r in candidates:
        p = prepared.priority(r.category_id)
        if p is None:  # A02 guarantees priorities in resolution mode; never invent one
            raise InternalError()
        prios.append((p, r.category_id))
    selected = min(prios)[1]
    return Resolution(
        selected_category_id=selected,
        reason="priority_tiebreak",
        candidate_category_ids=tuple(r.category_id for r in candidates),
    )


def build_result(
    *,
    request_id: str,
    prepared: PreparedAnalysis,
    requested: tuple[str, ...],
    validated: dict[str, ValidatedCapability],
    text_length: int,
    context_provided: bool,
    descriptor: AdapterDescriptor,
) -> AnalysisResult:
    parts: dict[str, ClassificationResult | DetectionResult | CapabilityNotRun] = {}
    for cap in CAPABILITIES:
        if cap not in requested:
            parts[cap] = CapabilityNotRun(status="not_requested")
        elif cap not in validated:
            parts[cap] = CapabilityNotRun(status="not_configured")
    v = validated.get("classification")
    if v is not None:
        cov = _coverage(v.coverage, prepared.element_ids("classification"), text_length)
        ranked = rank_scores(v.scores, prepared.eligibility_threshold)
        resolution: Resolution | None = None
        if prepared.mode == "resolution":
            if v.status == "inconclusive":
                resolution = Resolution(
                    selected_category_id=None, reason="classification_inconclusive", candidate_category_ids=()
                )
            else:
                resolution = resolve(ranked, prepared.tie_tolerance, prepared)
        parts["classification"] = ClassificationResult(
            status=v.status,
            reason=v.reason,
            scale=Scale(),
            mode=prepared.mode,
            eligibility_threshold=prepared.eligibility_threshold,
            tie_tolerance=prepared.tie_tolerance,
            coverage=cov,
            ranking=tuple(ranked),
            resolution=resolution,
        )
    d = validated.get("detection")
    if d is not None:
        findings = sorted(d.findings, key=lambda f: (f[1], f[2], f[0]))
        parts["detection"] = DetectionResult(
            status=d.status,
            reason=d.reason,
            coverage=_coverage(d.coverage, prepared.element_ids("detection"), text_length),
            findings=tuple(FindingLocation(type_id=t, start=s, end=e) for t, s, e in findings),
        )
    overall = "completed" if all(x.status == "completed" for x in validated.values()) else "inconclusive"
    model = None
    if descriptor.model_id is not None or descriptor.model_version is not None:
        model = ModelRef(id=descriptor.model_id, version=descriptor.model_version)
    return AnalysisResult(
        analysis_contract_version=ANALYSIS_CONTRACT_VERSION,
        request_id=request_id,
        status=overall,
        profile=ProfileRef(
            scope_id=prepared.scope_id,
            profile_id=prepared.profile_id,
            version_id=prepared.version_id,
            version_number=prepared.version_number,
            content_hash=prepared.content_hash,
            selection=prepared.selection,
        ),
        input=InputInfo(text_length=text_length, context_provided=context_provided),
        provenance=Provenance(
            kind=descriptor.kind,
            simulated=descriptor.kind == "mock",
            adapter_id=descriptor.adapter_id,
            adapter_version=descriptor.adapter_version,
            configuration_id=descriptor.configuration_id,
            model=model,
            inference_contract_version=descriptor.inference_contract_version,
        ),
        classification=parts["classification"],
        detection=parts["detection"],
    )
