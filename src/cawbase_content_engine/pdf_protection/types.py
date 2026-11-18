"""Public types of A08 (docs/pdf-protection.md, section 11). Immutable; none carries an original value."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..docx_protection.types import ExistingContext, NewContext
from ..jobs.models import JobInfo

__all__ = [
    "ExistingContext",
    "NewContext",
    "PdfCleanupSummary",
    "PdfDerivativeInfo",
    "PdfProtectionAccepted",
    "PdfProtectionResult",
    "PdfRestorationResult",
]


@dataclass(frozen=True)
class PdfProtectionAccepted:
    job: JobInfo
    replayed: bool
    context_id: str
    context_kind: str
    context_expires_at: datetime


@dataclass(frozen=True)
class PdfProtectionResult:
    """The ``pdf_protection_report`` artifact: groups and markers by unit offsets, zone kinds and counts, removed
    classes, coverage and the inspection scope; never a value, a position of a value on the page or a font name."""

    contract_version: int
    job_id: str
    derivative_id: str
    derivative: Mapping[str, object]
    source: Mapping[str, object]
    context: Mapping[str, object]
    policy: Mapping[str, object]
    units: Sequence[Mapping[str, object]]
    counts: Mapping[str, int]
    zones: Mapping[str, int]
    authorized_removals_performed: Sequence[Mapping[str, object]]
    coverage: Mapping[str, object]
    inspection_scope: Mapping[str, object]
    verification: Mapping[str, object]
    transformer: Mapping[str, object]


@dataclass(frozen=True)
class PdfDerivativeInfo:
    scope_id: str
    derivative_id: str
    protection_job_id: str
    context_id: str
    context_kind: str
    sha256: str
    size_bytes: int
    occurrence_count: int
    state: Literal["active", "ended"]  # no per-derivative revocation (D-A08-06)
    created_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class PdfRestorationResult:
    contract_version: int
    job_id: str
    derivative_id: str
    restored: Mapping[str, object]
    counts: Mapping[str, int]
    removed_classes_absent: Sequence[str]
    verification: Mapping[str, object]


@dataclass(frozen=True)
class PdfCleanupSummary:
    bindings_ended: int
    tombstones_purged: int
