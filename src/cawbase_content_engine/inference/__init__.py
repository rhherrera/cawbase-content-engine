"""Inference contract, strict output validation, result models and the deterministic mock (A03).

Imported lazily: ``import cawbase_content_engine`` does not load this package.
"""

from __future__ import annotations

from .adapter import (
    ANALYSIS_CONTRACT_VERSION,
    INFERENCE_CONTRACT_VERSION,
    AdapterDescriptor,
    AdapterState,
    ElementDefinition,
    InferenceAdapter,
    InferenceLimits,
    InferenceRequest,
    InferenceSetup,
    InferenceStatus,
    definitions_digest,
)
from .mock import MockFixture, MockInferenceAdapter, full_coverage, locate, request_key
from .prepared import build_request, prepare, request_for
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

__all__ = [
    "ANALYSIS_CONTRACT_VERSION",
    "AdapterDescriptor",
    "AdapterState",
    "AnalysisResult",
    "CapabilityNotRun",
    "ClassificationResult",
    "Coverage",
    "CoverageGroup",
    "DetectionResult",
    "ElementDefinition",
    "FindingLocation",
    "INFERENCE_CONTRACT_VERSION",
    "InferenceAdapter",
    "InferenceLimits",
    "InferenceRequest",
    "InferenceSetup",
    "InferenceStatus",
    "InputInfo",
    "MockFixture",
    "MockInferenceAdapter",
    "ModelRef",
    "ProfileRef",
    "Provenance",
    "RankedCategory",
    "Region",
    "Resolution",
    "Scale",
    "build_request",
    "definitions_digest",
    "full_coverage",
    "locate",
    "prepare",
    "request_for",
    "request_key",
]
