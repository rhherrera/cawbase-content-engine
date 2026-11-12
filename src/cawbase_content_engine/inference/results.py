"""Public result models of ``analyze_text`` (docs/inference-and-analysis.md, section 6)."""

from __future__ import annotations

from typing import Literal

from ..models import _Frozen


class Region(_Frozen):
    start: int
    end: int


class CoverageGroup(_Frozen):
    ids: tuple[str, ...]
    regions: tuple[Region, ...]


class Coverage(_Frozen):
    complete: bool
    text_length: int
    groups: tuple[CoverageGroup, ...]
    not_evaluated_ids: tuple[str, ...]


class Scale(_Frozen):
    min: int = 0
    max: int = 1
    calibrated: bool = False


class RankedCategory(_Frozen):
    rank: int
    category_id: str
    score: float
    eligible: bool


class Resolution(_Frozen):
    selected_category_id: str | None
    reason: Literal["highest_score", "priority_tiebreak", "no_eligible_category", "classification_inconclusive"]
    candidate_category_ids: tuple[str, ...]


class CapabilityNotRun(_Frozen):
    """Only a status: the capability was not evaluated (never an empty list)."""

    status: Literal["not_requested", "not_configured"]


class ClassificationResult(_Frozen):
    status: Literal["completed", "inconclusive"]
    reason: str | None
    scale: Scale
    mode: Literal["ranking", "resolution"]
    eligibility_threshold: float
    tie_tolerance: float
    coverage: Coverage
    ranking: tuple[RankedCategory, ...]
    resolution: Resolution | None  # null in ranking mode


class FindingLocation(_Frozen):
    type_id: str
    start: int
    end: int


class DetectionResult(_Frozen):
    status: Literal["completed", "inconclusive"]
    reason: str | None
    coverage: Coverage
    findings: tuple[FindingLocation, ...]


class ProfileRef(_Frozen):
    scope_id: str
    profile_id: str
    version_id: str
    version_number: int
    content_hash: str
    selection: str


class InputInfo(_Frozen):
    text_length: int
    context_provided: bool


class ModelRef(_Frozen):
    id: str | None
    version: str | None


class Provenance(_Frozen):
    kind: Literal["mock", "model"]
    simulated: bool
    adapter_id: str
    adapter_version: str
    configuration_id: str | None
    model: ModelRef | None
    inference_contract_version: int


class AnalysisResult(_Frozen):
    analysis_contract_version: int
    request_id: str
    status: Literal["completed", "inconclusive"]
    profile: ProfileRef
    input: InputInfo
    provenance: Provenance
    classification: ClassificationResult | CapabilityNotRun
    detection: DetectionResult | CapabilityNotRun
