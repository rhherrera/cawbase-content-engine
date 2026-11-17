"""Public types of A06 (docs/text-protection.md, sections 3 and 9.1). Standard library only (plus A03 result types)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from ..errors import InvalidConfigurationError

if TYPE_CHECKING:
    from ..inference.results import ProfileRef, Provenance

Action = Literal["pseudonymize", "redact"]
Kind = Literal["conversation", "job"]
KEY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
KEY_BYTES = 32
DAY = 86400


# ---- inputs ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ProtectionPolicy:
    """Strict policy: every finding is protected. There is no «let it through» action [E-A06-03]."""

    default_action: Action
    actions: Mapping[str, Action] = field(default_factory=dict)


@dataclass(frozen=True)
class InlineAnalysis:
    """The engine analyzes the exact text in this call (docs 4.1)."""

    profile_id: str | None = None
    version_number: int | None = None
    context: str | None = None


@dataclass(frozen=True)
class JobAnalysis:
    """The result of an A05 ``text_analysis`` job, bound to the exact text by identity (docs 4.2)."""

    job_id: str


AnalysisSource = InlineAnalysis | JobAnalysis


# ---- keys and setup ----------------------------------------------------------------------------------
@runtime_checkable
class KeyProvider(Protocol):
    """Synchronous, in-memory, no I/O [E-A06-18]: it is called inside the decision transaction."""

    def active_version(self) -> str: ...

    def key(self, version: str) -> bytes | None: ...


@dataclass(frozen=True)
class KeyRing:
    """Static in-memory key provider. ``repr`` never shows key material."""

    active: str
    keys: Mapping[str, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.active, str) or not KEY_VERSION_RE.fullmatch(self.active):
            raise InvalidConfigurationError("Invalid active key version.")
        for version, key in dict(self.keys).items():
            if not isinstance(version, str) or not KEY_VERSION_RE.fullmatch(version):
                raise InvalidConfigurationError("Invalid key version label.")
            if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
                raise InvalidConfigurationError("Every key must be exactly 32 bytes.")
        if self.active not in self.keys:
            raise InvalidConfigurationError("The active key version is not in the key ring.")
        object.__setattr__(self, "keys", {k: bytes(v) for k, v in dict(self.keys).items()})

    def active_version(self) -> str:
        return self.active

    def key(self, version: str) -> bytes | None:
        return self.keys.get(version)

    @classmethod
    def from_directory(cls, path: Path | str, *, active: str) -> KeyRing:
        """Reads ``<version>.key`` files (32 raw bytes each)."""
        keys: dict[str, bytes] = {}
        for entry in sorted(Path(path).glob("*.key")):
            keys[entry.stem] = entry.read_bytes()
        return cls(active=active, keys=keys)


@dataclass(frozen=True)
class ProtectionLimits:
    """Initial technical choices (E-A06-27), not measurements. Every value must be a positive integer."""

    max_active_contexts_per_scope: int = 1000
    max_mappings_per_context: int = 10_000
    max_context_bytes: int = 4 * 1024 * 1024
    max_fragment_chars: int = 8192
    max_delivered_material_chars: int = 262_144
    max_delivered_markers: int = 10_000
    cleanup_batch_size: int = 200

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise InvalidConfigurationError(f"Protection limit '{name}' must be a positive integer.")


@dataclass(frozen=True)
class ProtectionSetup:
    """Operator-supplied configuration of A06 (docs 9.1). The host owns the key provider; the engine never closes it."""

    keys: KeyProvider
    context_ttl_s: int = 7 * DAY
    max_context_ttl_s: int = 30 * DAY
    metadata_retention_s: int = 30 * DAY
    limits: ProtectionLimits = field(default_factory=ProtectionLimits)

    def __post_init__(self) -> None:
        if not (callable(getattr(self.keys, "active_version", None)) and callable(getattr(self.keys, "key", None))):
            raise InvalidConfigurationError("'keys' must provide active_version() and key(version).")
        hard_max = 3650 * DAY
        for name in ("context_ttl_s", "max_context_ttl_s", "metadata_retention_s"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise InvalidConfigurationError(f"'{name}' must be a positive integer.")
        if not (60 <= self.context_ttl_s <= self.max_context_ttl_s <= hard_max):
            raise InvalidConfigurationError("context_ttl_s must lie within [60, max_context_ttl_s <= 3650 days].")
        if not isinstance(self.limits, ProtectionLimits):
            raise InvalidConfigurationError("'limits' must be a ProtectionLimits.")


# ---- outputs (immutable; never an original fragment) -------------------------------------------------
@dataclass(frozen=True)
class ContextInfo:
    scope_id: str
    context_id: str
    kind: str
    state: Literal["active", "expired", "deleted"]  # EFFECTIVE: compared with the database clock at the read
    created_at: datetime
    expires_at: datetime
    deleted_at: datetime | None
    mapping_count: int


@dataclass(frozen=True)
class AnalysisRef:
    source: Literal["inline", "job"]
    job_id: str | None
    profile: ProfileRef
    provenance: Provenance
    detection_status: Literal["completed"] = "completed"


@dataclass(frozen=True)
class ProtectedGroup:
    action: Action
    input_start: int
    input_end: int
    output_start: int
    output_end: int
    marker: str | None  # a reference, never the value; None when redacted


@dataclass(frozen=True)
class ProtectedText:
    contract_version: Literal[1]
    scope_id: str
    context_id: str
    kind: str
    text: str
    outcome: Literal["protected", "no_findings"]
    groups: tuple[ProtectedGroup, ...]
    markers: tuple[str, ...]
    counts: Mapping[str, int]
    analysis: AnalysisRef
    context_expires_at: datetime


@dataclass(frozen=True)
class NewFindingRegion:
    type_id: str
    start: int
    end: int


@dataclass(frozen=True)
class RestoredText:
    contract_version: Literal[1]
    scope_id: str
    context_id: str
    kind: str
    text: str
    outcome: Literal["restored", "restored_with_redactions", "no_markers"]
    counts: Mapping[str, int]
    new_findings: tuple[NewFindingRegion, ...]
    analysis: AnalysisRef


@dataclass(frozen=True)
class ProtectionCleanupSummary:
    """Counts only: never content."""

    contexts_expired: int = 0
    mappings_deleted: int = 0
    contexts_purged: int = 0


@dataclass(frozen=True)
class KeyVersionUsage:
    """For deciding when a key version can be retired: counts and the latest expiry only (no content)."""

    version: str
    live_contexts: int
    latest_expires_at: datetime | None
