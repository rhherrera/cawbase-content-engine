"""Deterministic mock adapter over synthetic fixtures (docs/inference-and-analysis.md, section 7).

It is *not* a classifier or detector: it returns exactly what a fixture says for an input it was
told about, or fails with a typed error. No heuristics, no keyword matching, no fallback. Enabled only
by the host/operator composing it explicitly; a request can never choose it.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import (
    InferenceFixtureNotFoundError,
    InferenceUnavailableError,
    InvalidConfigurationError,
)
from .adapter import AdapterDescriptor, AdapterState, InferenceRequest, canonical_json

_KINDS = ("output", "unavailable", "hang", "crash")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_key(request: InferenceRequest) -> tuple[str, str | None, tuple[str, ...], str]:
    return (
        _sha(request.text),
        None if request.auxiliary_context is None else _sha(request.auxiliary_context),
        tuple(request.capabilities),
        request.definitions_digest,
    )


@dataclass(frozen=True)
class MockFixture:
    """Binds an input (text, context, capabilities, definitions) to an outcome.

    ``outcome``: ``{"kind": "output", "output": <raw output; may be invalid on purpose>}``,
    ``{"kind": "unavailable", "reason": loading|stopped|unreachable|transient_failure}``,
    ``{"kind": "hang"}`` (never answers: the engine times out) or ``{"kind": "crash"}``.
    """

    fixture_id: str
    text_sha256: str
    context_sha256: str | None
    capabilities: tuple[str, ...]
    definitions_digest: str
    outcome: Mapping[str, Any]

    @property
    def key(self) -> tuple[str, str | None, tuple[str, ...], str]:
        return (self.text_sha256, self.context_sha256, self.capabilities, self.definitions_digest)

    @classmethod
    def for_request(cls, fixture_id: str, request: InferenceRequest, outcome: Mapping[str, Any]) -> MockFixture:
        text, ctx, caps, digest = request_key(request)
        return cls(fixture_id, text, ctx, caps, digest, copy.deepcopy(dict(outcome)))

    @classmethod
    def from_dict(cls, raw: Any) -> MockFixture:
        try:
            m = raw["match"]
            fx = cls(
                fixture_id=raw["fixture_id"],
                text_sha256=m["text_sha256"],
                context_sha256=m.get("context_sha256"),
                capabilities=tuple(m["capabilities"]),
                definitions_digest=m["definitions_digest"],
                outcome=copy.deepcopy(raw["outcome"]),
            )
        except (KeyError, TypeError):
            raise InvalidConfigurationError("Invalid mock fixture structure.") from None
        fx._check()
        return fx

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "match": {
                "text_sha256": self.text_sha256,
                "context_sha256": self.context_sha256,
                "capabilities": list(self.capabilities),
                "definitions_digest": self.definitions_digest,
            },
            "outcome": copy.deepcopy(dict(self.outcome)),
        }

    def _check(self) -> None:
        def bad() -> InvalidConfigurationError:
            return InvalidConfigurationError("Invalid mock fixture structure.")

        if not isinstance(self.fixture_id, str) or not self.fixture_id or len(self.fixture_id) > 100:
            raise bad()
        for digest in (self.text_sha256, self.definitions_digest):
            if not isinstance(digest, str) or len(digest) != 64:
                raise bad()
        if self.context_sha256 is not None and (
            not isinstance(self.context_sha256, str) or len(self.context_sha256) != 64
        ):
            raise bad()
        if not self.capabilities or any(c not in ("classification", "detection") for c in self.capabilities):
            raise bad()
        if not isinstance(self.outcome, Mapping) or self.outcome.get("kind") not in _KINDS:
            raise bad()
        kind = self.outcome["kind"]
        if kind == "output" and not isinstance(self.outcome.get("output"), Mapping):
            # a non-mapping output is useful to exercise the validator: allow it only as an explicit raw value
            if "output" not in self.outcome:
                raise bad()
        if kind == "unavailable" and self.outcome.get("reason") not in InferenceUnavailableError.REASONS:
            raise bad()


class MockInferenceAdapter:
    """Replays fixtures. Fixtures are copied on load and the output is copied on every call."""

    def __init__(self, fixtures: Sequence[MockFixture], *, acknowledge_simulated: bool = False) -> None:
        if acknowledge_simulated is not True:
            raise InvalidConfigurationError("The mock adapter must be enabled explicitly (acknowledge_simulated=True).")
        table: dict[tuple[str, str | None, tuple[str, ...], str], MockFixture] = {}
        ids: set[str] = set()
        for fx in fixtures:
            if not isinstance(fx, MockFixture):
                raise InvalidConfigurationError("Mock fixtures must be MockFixture instances.")
            fx._check()
            if fx.key in table or fx.fixture_id in ids:
                raise InvalidConfigurationError("Duplicate mock fixture.")
            ids.add(fx.fixture_id)
            table[fx.key] = MockFixture(
                fx.fixture_id,
                fx.text_sha256,
                fx.context_sha256,
                tuple(fx.capabilities),
                fx.definitions_digest,
                copy.deepcopy(dict(fx.outcome)),
            )
        self._table = table
        digest = hashlib.sha256(
            canonical_json(sorted((f.to_dict() for f in table.values()), key=lambda d: d["fixture_id"]))
        )
        self._descriptor = AdapterDescriptor(
            kind="mock",
            adapter_id="cce-mock-fixtures",
            adapter_version="1",
            configuration_id="fxs-" + digest.hexdigest()[:16],
        )

    @classmethod
    def from_file(cls, path: str | Path, *, acknowledge_simulated: bool = False) -> MockInferenceAdapter:
        """Fixtures file: ``{"fixtures": [ {fixture_id, match, outcome}, ... ]}`` (synthetic data only)."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            items = data["fixtures"]
            if not isinstance(items, list):
                raise TypeError
        except (OSError, ValueError, KeyError, TypeError):
            raise InvalidConfigurationError("The mock fixtures file could not be read or is not valid.") from None
        return cls([MockFixture.from_dict(i) for i in items], acknowledge_simulated=acknowledge_simulated)

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    async def probe(self) -> AdapterState:
        return AdapterState.READY

    async def infer(self, request: InferenceRequest) -> Mapping[str, Any]:
        fx = self._table.get(request_key(request))
        if fx is None:
            raise InferenceFixtureNotFoundError()
        kind = fx.outcome["kind"]
        if kind == "output":
            return copy.deepcopy(fx.outcome["output"])
        if kind == "unavailable":
            raise InferenceUnavailableError(fx.outcome["reason"])
        if kind == "hang":
            await asyncio.Event().wait()  # until cancelled by the engine's timeout or the caller
        raise RuntimeError("simulated adapter crash")  # crash


def locate(text: str, fragment: str, *, occurrence: int = 0) -> dict[str, Any]:
    """Authoring helper: a raw finding location computed from the text (offsets verified by construction)."""
    pos = -1
    for _ in range(occurrence + 1):
        pos = text.index(fragment, pos + 1)
    return {"start": pos, "end": pos + len(fragment), "text": fragment}


def full_coverage(ids: Sequence[str], text: str) -> dict[str, Any]:
    """Authoring helper: one group covering the whole text."""
    return {"groups": [{"ids": sorted(ids), "regions": [{"start": 0, "end": len(text)}]}]}
