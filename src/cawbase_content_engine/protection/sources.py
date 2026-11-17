"""Analysis sources bound to the EXACT text (docs/text-protection.md, section 4).

``InlineAnalysis``: bound by construction (the engine analyzes this very ``str`` now).
``JobAnalysis``: bound by identity of the text (SHA-256 and size of its UTF-8 bytes against the job's pinned input),
re-checked in the decision transaction with the job row locked. Findings supplied by the caller are never accepted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from .. import validation as v
from ..errors import (
    AnalysisSourceInvalidError,
    AnalysisTextMismatchError,
    CapabilitiesNotConfiguredError,
    DetectionInconclusiveError,
    JobDataExpiredError,
    JobNotSucceededError,
    ResourceNotFoundError,
    ValidationFailedError,
)
from .grouping import Finding
from .types import AnalysisRef, AnalysisSource, InlineAnalysis, JobAnalysis

if TYPE_CHECKING:
    from ..engine import ContentEngine
    from ..inference.results import AnalysisResult


@dataclass(frozen=True)
class JobBinding:
    job_id: str
    artifact_id: str
    artifact_sha256: str  # hex, as read (and verified by A05) before the decision transaction


@dataclass(frozen=True)
class Resolved:
    findings: tuple[Finding, ...]
    ref: AnalysisRef
    job: JobBinding | None


def check_source(value: Any) -> InlineAnalysis | JobAnalysis:
    """Only the two engine-controlled sources exist: a caller-supplied result or offsets are not accepted."""
    if isinstance(value, InlineAnalysis):
        if value.profile_id is not None:
            v.slug(value.profile_id, "profile_id")
        if value.version_number is not None:
            if value.profile_id is None:
                raise ValidationFailedError("version_number needs a profile_id.", field="version_number")
            v.int_in(value.version_number, "version_number", 1)
        return value
    if isinstance(value, JobAnalysis):
        from ..jobs.ops import check_job_id

        check_job_id(value.job_id)
        return value
    raise ValidationFailedError("Invalid analysis source.", field="analysis")


def _detection_findings(analysis: AnalysisResult) -> tuple[Finding, ...]:
    det: Any = analysis.detection
    status = getattr(det, "status", None)
    if status == "completed":
        return tuple((f.type_id, f.start, f.end) for f in det.findings)
    if status == "inconclusive":
        raise DetectionInconclusiveError()
    if status in ("not_requested", "not_configured"):
        raise CapabilitiesNotConfiguredError()
    raise AnalysisSourceInvalidError()


async def resolve(
    engine: ContentEngine, scope_id: str, source: InlineAnalysis | JobAnalysis, text: str, *, request_id: str | None
) -> Resolved:
    """Runs OUTSIDE any transaction (inference and storage reads never hold a connection)."""
    if isinstance(source, InlineAnalysis):
        from ..inference.service import run_analysis

        result = await run_analysis(
            engine,
            scope_id=scope_id,
            text=text,
            profile_id=source.profile_id,
            version_number=source.version_number,
            capabilities=("detection",),
            context=source.context,
            request_id=request_id,
        )
        findings = _detection_findings(result)
        return Resolved(findings, AnalysisRef("inline", None, result.profile, result.provenance), None)
    return await _resolve_job(engine, scope_id, source.job_id, text)


async def _resolve_job(engine: ContentEngine, scope_id: str, job_id: str, text: str) -> Resolved:
    from ..inference.results import AnalysisResult
    from ..jobs import db as jdb
    from ..jobs import ops as jops

    t = engine.tables

    async def pre(conn: AsyncConnection) -> dict[str, Any]:
        now = await jdb.db_now(conn, engine.backend)
        row = (
            await conn.execute(sa.select(t.jobs).where(t.jobs.c.scope_id == scope_id, t.jobs.c.job_id == job_id))
        ).first()
        if row is None:
            raise ResourceNotFoundError()
        if row.operation != "text_analysis":  # document jobs speak in document units (A07/A08), never here
            raise AnalysisSourceInvalidError()
        if row.state != "succeeded":
            raise JobNotSucceededError()
        if row.data_state != "available" or (row.result_expires_at is not None and row.result_expires_at <= now):
            raise JobDataExpiredError()
        art = None
        if row.result_artifact_id:
            ar = t.job_artifacts
            art = (
                await conn.execute(
                    sa.select(ar.c.artifact_id, ar.c.sha256).where(
                        ar.c.scope_id == scope_id, ar.c.artifact_id == row.result_artifact_id
                    )
                )
            ).first()
        if art is None:
            raise AnalysisSourceInvalidError()
        snap = row.execution_snapshot
        if isinstance(snap, str):
            snap = json.loads(snap)
        return {
            "snap_input": snap["input"],
            "profile": (row.profile_id, row.version_id, row.version_number, row.profile_content_hash, row.selection),
            "artifact_id": art.artifact_id,
            "sha": bytes(art.sha256).hex(),
        }

    info: dict[str, Any] = await engine._run(pre, write=False, tx=None)
    # condition 5 first: the identity of the text decides before any object is read
    encoded = text.encode("utf-8")
    pinned = info["snap_input"]
    if hashlib.sha256(encoded).hexdigest() != pinned.get("sha256") or len(encoded) != pinned.get("size_bytes"):
        raise AnalysisTextMismatchError()
    manifest = await jops.get_job_result(engine, scope_id, job_id)  # sha256 verified by A05 while reading
    try:
        if manifest.get("job_id") != job_id or manifest.get("operation") != "text_analysis":
            raise AnalysisSourceInvalidError()
        analysis = AnalysisResult.model_validate(manifest["analysis"])
    except AnalysisSourceInvalidError:
        raise
    except Exception:
        raise AnalysisSourceInvalidError() from None
    if analysis.input.text_length != len(text):
        raise AnalysisTextMismatchError()
    p = analysis.profile
    if (p.scope_id, p.profile_id, p.version_id, p.version_number, p.content_hash, p.selection) != (
        scope_id,
        *info["profile"],
    ):
        raise AnalysisSourceInvalidError()
    findings = _detection_findings(analysis)
    return Resolved(
        findings,
        AnalysisRef("job", job_id, analysis.profile, analysis.provenance),
        JobBinding(job_id, info["artifact_id"], info["sha"]),
    )


async def lock_job(conn: AsyncConnection, t: Any, scope_id: str, binding: JobBinding) -> Any:
    """Rank 8: lock the job row FOR SHARE and return it. The time is NOT read here: the caller reads it ONCE, after
    it holds every lock of the transaction (job and context), and decides with that single instant (docs 11.1)."""
    return (
        await conn.execute(
            sa.select(t.jobs)
            .where(t.jobs.c.scope_id == scope_id, t.jobs.c.job_id == binding.job_id)
            .with_for_update(read=True)
        )
    ).first()


async def recheck_job(
    conn: AsyncConnection, t: Any, scope_id: str, binding: JobBinding, row: Any, now: datetime
) -> None:
    """Re-verify, with the decision instant ``now``, the state, the expiry of the result and that the published
    artifact is the one that was read before the transaction (docs 4.2)."""
    if row is None:
        raise ResourceNotFoundError()
    if row.state != "succeeded":
        raise JobNotSucceededError()
    if row.data_state != "available" or (row.result_expires_at is not None and row.result_expires_at <= now):
        raise JobDataExpiredError()
    if row.result_artifact_id != binding.artifact_id:
        raise AnalysisSourceInvalidError()
    ar = t.job_artifacts
    art = (
        await conn.execute(
            sa.select(ar.c.sha256, ar.c.state).where(ar.c.scope_id == scope_id, ar.c.artifact_id == binding.artifact_id)
        )
    ).first()
    if art is None or art.state != "published" or bytes(art.sha256).hex() != binding.artifact_sha256:
        raise AnalysisSourceInvalidError()


_ = AnalysisSource
