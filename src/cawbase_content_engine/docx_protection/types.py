"""Public types of A07 (docs/docx-protection.md, section 8). Immutable; none carries an original value."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ..jobs.models import JobInfo


@dataclass(frozen=True)
class ExistingContext:
    """An A06 context of the same scope, alive and of this kind (created with ``create_context``)."""

    context_id: str
    kind: Literal["conversation", "job"]


@dataclass(frozen=True)
class NewContext:
    """The submission creates a ``job`` context of A06 in the SAME transaction that accepts the job (P-A07-07)."""

    ttl_s: int | None = None


@dataclass(frozen=True)
class DocxProtectionAccepted:
    job: JobInfo
    replayed: bool
    context_id: str
    context_kind: str
    context_expires_at: datetime


@dataclass(frozen=True)
class DocxProtectionResult:
    """The ``docx_protection_report`` artifact: positions of groups and markers, counts, removed classes, coverage
    and the inspection scope; never a value."""

    contract_version: int
    job_id: str
    derivative_id: str
    derivative: Mapping[str, Any]
    source: Mapping[str, Any]
    context: Mapping[str, Any]
    policy: Mapping[str, Any]
    units: Sequence[Mapping[str, Any]]
    counts: Mapping[str, int]
    authorized_removals_performed: Sequence[Mapping[str, Any]]
    coverage: Mapping[str, Any]
    inspection_scope: Mapping[str, Any]
    verification: Mapping[str, Any]
    transformer: Mapping[str, Any]


@dataclass(frozen=True)
class DocxDerivativeInfo:
    scope_id: str
    derivative_id: str
    protection_job_id: str
    context_id: str
    context_kind: str
    sha256: str
    size_bytes: int
    occurrence_count: int
    state: Literal["active", "ended"]  # no per-derivative revocation (P-A07-06)
    created_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class DocxRestorationResult:
    contract_version: int
    job_id: str
    derivative_id: str
    restored: Mapping[str, Any]
    counts: Mapping[str, int]
    removed_classes_absent: Sequence[str]
    verification: Mapping[str, Any]


@dataclass(frozen=True)
class DocxCleanupSummary:
    bindings_ended: int
    tombstones_purged: int
