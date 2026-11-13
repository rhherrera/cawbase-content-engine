"""A05 result types (immutable). No storage keys, external references, resolver ids or lease tokens."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class StoredFile(_Frozen):
    scope_id: str
    file_id: str
    origin: str
    state: str
    integrity_failure: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    detected_format: str | None = None
    received_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    download_validation: str = "pending"  # passed | rejected | pending, for the current validator identity
    validations: tuple[dict[str, Any], ...] = ()


class FileRef(_Frozen):
    file_id: str
    sha256: str | None
    size_bytes: int | None


class JobInfo(_Frozen):
    scope_id: str
    job_id: str
    operation: str
    state: str
    phase: str
    attempt_count: int
    max_attempts: int
    cancel_requested_at: datetime | None
    cancel_reason: str | None
    failure_code: str | None
    last_error_code: str | None
    accepted_at: datetime
    started_at: datetime | None
    terminated_at: datetime | None
    deadline_at: datetime
    input: FileRef
    context: FileRef | None
    profile: dict[str, Any]
    data_state: str
    result_expires_at: datetime | None
    metadata_expires_at: datetime | None
    cancel_outcome: str | None = None


class JobAccepted(_Frozen):
    job: JobInfo
    replayed: bool


class ClaimedJob(_Frozen):
    """Trusted local surface only (never exposed over HTTP)."""

    job_id: str
    scope_id: str
    attempt_no: int
    lease_token: str
    lease_expires_at: datetime
    attempt_deadline_at: datetime
    phase: str


class JobRunOutcome(_Frozen):
    job_id: str
    state: str
    attempt_no: int
    outcome_code: str


class CleanupSummary(_Frozen):
    """No content: counts by class and diagnostics only."""

    counts: dict[str, int]
    diagnostics: dict[str, int]


class PurgeSummary(_Frozen):
    dry_run: bool
    matched: int
    deleted: int
    oldest_occurred_at: datetime | None
    newest_occurred_at: datetime | None
    min_event_id: int | None
    max_event_id: int | None
