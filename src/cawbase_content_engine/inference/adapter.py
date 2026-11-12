"""Adapter-facing types (docs/inference-and-analysis.md, section 4).

Everything the adapter receives is immutable (frozen dataclasses, tuples, ``str``/``float``/``int``).
Nothing here performs I/O.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from ..errors import InvalidConfigurationError

INFERENCE_CONTRACT_VERSION = 1
ANALYSIS_CONTRACT_VERSION = 1
CAPABILITIES = ("classification", "detection")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_TIMEOUT_S = 600.0


class AdapterState(enum.StrEnum):
    READY = "ready"
    LOADING = "loading"
    STOPPED = "stopped"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AdapterDescriptor:
    """Trusted description of the adapter, given by the host when composing the engine."""

    kind: Literal["mock", "model"]
    adapter_id: str
    adapter_version: str
    configuration_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    inference_contract_version: int = INFERENCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.kind not in ("mock", "model"):
            raise InvalidConfigurationError("Adapter kind must be 'mock' or 'model'.")
        for name in ("adapter_id", "adapter_version"):
            if not isinstance(getattr(self, name), str) or not _SLUG.fullmatch(getattr(self, name)):
                raise InvalidConfigurationError(f"Invalid adapter descriptor field '{name}'.")
        for name in ("configuration_id", "model_id", "model_version"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not _SLUG.fullmatch(value)):
                raise InvalidConfigurationError(f"Invalid adapter descriptor field '{name}'.")
        if self.kind == "mock" and (self.model_id is not None or self.model_version is not None):
            raise InvalidConfigurationError("A mock adapter declares no model.")
        if self.inference_contract_version != INFERENCE_CONTRACT_VERSION:
            raise InvalidConfigurationError("Unsupported inference contract version.")


@dataclass(frozen=True)
class InferenceLimits:
    max_findings: int
    max_finding_chars: int
    max_regions: int
    max_output_bytes: int


@dataclass(frozen=True)
class ElementDefinition:
    """A category or a sensitive type as the adapter sees it (never with priority)."""

    element_id: str
    name: str
    definition: str
    examples_positive: tuple[str, ...]
    examples_negative: tuple[str, ...]
    translations: tuple[tuple[str, str, str], ...]  # (language, name, definition)


@dataclass(frozen=True)
class InferenceRequest:
    contract_version: int
    request_id: str
    text: str
    auxiliary_context: str | None
    capabilities: tuple[str, ...]
    language: str
    categories: tuple[ElementDefinition, ...]
    sensitive_types: tuple[ElementDefinition, ...]
    definitions_digest: str
    limits: InferenceLimits


@runtime_checkable
class InferenceAdapter(Protocol):
    """What the engine needs from an inference backend. The *host* owns it (the engine never closes it)."""

    @property
    def descriptor(self) -> AdapterDescriptor: ...

    async def probe(self) -> AdapterState: ...

    async def infer(self, request: InferenceRequest) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class InferenceSetup:
    """Explicit composition of an adapter into the engine (``ContentEngine(config, inference=...)``)."""

    adapter: InferenceAdapter
    timeout_s: float = 30.0
    descriptor: AdapterDescriptor = field(init=False)

    def __post_init__(self) -> None:
        t = self.timeout_s
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not (0 < t <= MAX_TIMEOUT_S):
            raise InvalidConfigurationError("'timeout_s' must be in (0, 600].")
        adapter = self.adapter
        if not (callable(getattr(adapter, "infer", None)) and callable(getattr(adapter, "probe", None))):
            raise InvalidConfigurationError("The adapter must implement 'probe' and 'infer'.")
        descriptor = getattr(adapter, "descriptor", None)
        if not isinstance(descriptor, AdapterDescriptor):
            raise InvalidConfigurationError("The adapter must expose an AdapterDescriptor.")
        object.__setattr__(self, "descriptor", descriptor)  # a trusted snapshot taken once


@dataclass(frozen=True)
class InferenceStatus:
    configured: bool
    state: str  # not_configured | ready | loading | stopped | unavailable | unknown
    descriptor: AdapterDescriptor | None = None


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _element_payload(e: ElementDefinition, id_key: str) -> dict[str, Any]:
    return {
        id_key: e.element_id,
        "name": e.name,
        "definition": e.definition,
        "examples": {"positive": list(e.examples_positive), "negative": list(e.examples_negative)},
        "translations": {lang: {"name": n, "definition": d} for lang, n, d in e.translations} or None,
    }


def definitions_digest(
    language: str, categories: tuple[ElementDefinition, ...], sensitive_types: tuple[ElementDefinition, ...]
) -> str:
    """SHA-256 (hex) of the canonical JSON of what affects inference: language and the definitions sent.

    Only the capabilities actually sent contribute (an omitted capability is an empty list). Priorities,
    ranking mode, threshold and tolerance are never part of it, so changing only those does not
    invalidate a mock fixture.
    """
    payload = {
        "language": language,
        "categories": [_element_payload(e, "category_id") for e in categories],
        "sensitive_types": [_element_payload(e, "type_id") for e in sensitive_types],
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()
