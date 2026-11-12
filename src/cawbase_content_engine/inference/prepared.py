"""Immutable data fixed at preparation time (docs/inference-and-analysis.md, 3.2 and 3.4).

``PreparedAnalysis`` is built from the published version row into new immutable structures and is
the *only* thing the output validator and the result builder read: mutating the ``dict`` snapshot
a caller holds, or anything the adapter receives or returns, cannot alter it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..config import Limits
from ..models import ResolvedProfile
from .adapter import (
    CAPABILITIES,
    INFERENCE_CONTRACT_VERSION,
    ElementDefinition,
    InferenceLimits,
    InferenceRequest,
    definitions_digest,
)


@dataclass(frozen=True)
class PreparedCategory:
    category_id: str
    priority: int | None


@dataclass(frozen=True)
class PreparedAnalysis:
    scope_id: str
    profile_id: str
    version_id: str
    version_number: int
    content_hash: str
    selection: str
    language: str
    mode: str
    eligibility_threshold: float
    tie_tolerance: float
    classification_configured: bool
    detection_configured: bool
    categories: tuple[ElementDefinition, ...]
    priorities: tuple[tuple[str, int | None], ...]
    sensitive_types: tuple[ElementDefinition, ...]

    def configured(self, capability: str) -> bool:
        return self.classification_configured if capability == "classification" else self.detection_configured

    def element_ids(self, capability: str) -> tuple[str, ...]:
        elements = self.categories if capability == "classification" else self.sensitive_types
        return tuple(e.element_id for e in elements)

    def priority(self, category_id: str) -> int | None:
        return dict(self.priorities).get(category_id)


def _element(raw: dict[str, Any], id_key: str) -> ElementDefinition:
    ex = raw.get("examples") or {}
    tr = raw.get("translations") or {}
    return ElementDefinition(
        element_id=str(raw[id_key]),
        name=str(raw["name"]),
        definition=str(raw["definition"]),
        examples_positive=tuple(str(x) for x in ex.get("positive", ())),
        examples_negative=tuple(str(x) for x in ex.get("negative", ())),
        translations=tuple(sorted((str(k), str(v["name"]), str(v["definition"])) for k, v in tr.items())),
    )


def prepare(resolved: ResolvedProfile) -> PreparedAnalysis:
    """Copy the published version into immutable structures (no reference to the original ``dict``)."""
    ver = resolved.version
    snap = ver.snapshot
    rk = snap["ranking"]
    cats = tuple(_element(c, "category_id") for c in snap["categories"])
    types = tuple(_element(t, "type_id") for t in snap["sensitive_types"])
    caps = snap.get("capabilities") or {}
    return PreparedAnalysis(
        scope_id=ver.scope_id,
        profile_id=ver.profile_id,
        version_id=ver.version_id,
        version_number=ver.version_number,
        content_hash=ver.content_hash,
        selection=resolved.selection,
        language=str(snap["profile"]["language"]),
        mode=str(rk["mode"]),
        eligibility_threshold=float(rk["eligibility_threshold"]),
        tie_tolerance=float(rk["tie_tolerance"]),
        classification_configured=bool(caps.get("classification")) and bool(cats),
        detection_configured=bool(caps.get("detection")) and bool(types),
        categories=cats,
        priorities=tuple((str(c["category_id"]), c.get("priority")) for c in snap["categories"]),
        sensitive_types=types,
    )


def build_request(
    prepared: PreparedAnalysis,
    *,
    request_id: str,
    text: str,
    context: str | None,
    capabilities: tuple[str, ...],
    limits: Limits,
) -> InferenceRequest:
    """The request the adapter sees: only executable capabilities and their definitions; no priorities."""
    caps = tuple(c for c in CAPABILITIES if c in capabilities)
    # new element objects (an independent copy of what validation reads from ``prepared``)
    cats = tuple(replace(e) for e in prepared.categories) if "classification" in caps else ()
    types = tuple(replace(e) for e in prepared.sensitive_types) if "detection" in caps else ()
    return InferenceRequest(
        contract_version=INFERENCE_CONTRACT_VERSION,
        request_id=request_id,
        text=text,
        auxiliary_context=context,
        capabilities=caps,
        language=prepared.language,
        categories=cats,
        sensitive_types=types,
        definitions_digest=definitions_digest(prepared.language, cats, types),
        limits=InferenceLimits(
            max_findings=limits.max_analysis_findings,
            max_finding_chars=limits.max_finding_chars,
            max_regions=limits.max_analysis_regions,
            max_output_bytes=limits.max_inference_output_bytes,
        ),
    )


def request_for(
    resolved: ResolvedProfile,
    *,
    text: str,
    context: str | None = None,
    capabilities: tuple[str, ...] = CAPABILITIES,
    limits: Limits | None = None,
    request_id: str = "fixture-authoring",
) -> InferenceRequest:
    """Authoring helper (pure): the exact ``InferenceRequest`` the engine would send for this input.

    Used to compute a mock fixture's key; it applies the same executable-capability filter as the engine.
    """
    prepared = prepare(resolved)
    executable = tuple(c for c in CAPABILITIES if c in capabilities and prepared.configured(c))
    return build_request(
        prepared,
        request_id=request_id,
        text=text,
        context=context,
        capabilities=executable,
        limits=limits or Limits(),
    )
