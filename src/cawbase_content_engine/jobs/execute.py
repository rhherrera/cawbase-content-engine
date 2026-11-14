"""Execution of one claimed job: pinned version -> inputs -> (extraction) -> A03 per unit -> artifacts -> publish."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import DocumentsNotConfiguredError, IsolationUnavailableError
from ..inference.prepared import prepare
from ..inference.service import execute_prepared
from ..models import ResolvedProfile
from ..operations.versions import get_version_row, version_from_row
from . import db as jdb
from . import files as fl
from .ident import EXTRACTOR_ID, EXTRACTOR_VERSION, config_sha
from .models import ClaimedJob
from .worker import JobFailure, LeaseLost

if TYPE_CHECKING:
    from .worker import JobWorker


def _dump(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


async def _load(w: JobWorker, c: ClaimedJob) -> tuple[Any, dict[str, Any]]:
    t = w.ctx.t

    async def fn(conn: AsyncConnection) -> Any:
        return (
            await conn.execute(sa.select(t.jobs).where(t.jobs.c.scope_id == c.scope_id, t.jobs.c.job_id == c.job_id))
        ).first()

    row = await w.ctx.run(fn, write=False)
    if row is None:
        raise LeaseLost
    snap = row.execution_snapshot
    return row, json.loads(snap) if isinstance(snap, str) else snap


async def _pinned(w: JobWorker, row: Any) -> Any:
    """The version fixed at acceptance, read by its immutable row: never re-selected (disabled profiles still run)."""
    t = w.ctx.t

    async def fn(conn: AsyncConnection) -> Any:
        v = await get_version_row(conn, t, row.scope_id, row.profile_id, row.version_number)
        return None if v is None else version_from_row(v)

    version = await w.ctx.run(fn, write=False)
    if version is None or version.version_id != row.version_id or version.content_hash != row.profile_content_hash:
        raise JobFailure("configuration_unavailable")
    return prepare(ResolvedProfile(selection=row.selection, version=version))


async def _read_input(w: JobWorker, scope_id: str, file_id: str, *, limit: int) -> bytes:
    ctx = w.ctx

    async def fn(conn: AsyncConnection) -> Any:
        return await fl.get_file_row(conn, ctx.t, scope_id, file_id)

    frow = await ctx.run(fn, write=False)
    if frow is None or frow.state != "stored":
        raise JobFailure("input_unavailable")
    try:
        return await fl.read_verified(ctx, frow, limit=limit)
    except fl._Lost as lost:
        await fl.mark_lost(ctx, scope_id, file_id, lost.why)
        raise JobFailure("stored_object_corrupt") from None


async def execute_job(w: JobWorker, c: ClaimedJob) -> str:
    row, snap = await _load(w, c)
    ctx = w.ctx
    e = w.e
    if e.inference is None:
        raise JobFailure("configuration_unavailable")
    if row.requirements_key not in w.supported_keys():
        raise JobFailure("configuration_unavailable")
    prepared = await _pinned(w, row)
    requested = tuple(snap["capabilities_requested"])
    context_text: str | None = None
    if snap.get("context"):
        context_text = (
            await _read_input(
                w, c.scope_id, snap["context"]["file_id"], limit=e.config.limits.max_analysis_context_bytes * 2
            )
        ).decode("utf-8")
    if row.operation == "text_analysis":
        await w.set_phase(c, "analysis")
        text = (
            await _read_input(
                w, c.scope_id, snap["input"]["file_id"], limit=e.config.limits.max_analysis_text_bytes * 2
            )
        ).decode("utf-8")
        result = await execute_prepared(
            e, prepared, requested=requested, text=text, context=context_text, request_id=c.job_id
        )
        manifest = {
            "result_contract_version": 1,
            "job_id": c.job_id,
            "operation": "text_analysis",
            "analysis": result.model_dump(mode="json"),
        }
        data = _dump(manifest)
        if len(data) > ctx.lim.max_result_bytes:
            raise JobFailure("limit_exceeded")
        await w.set_phase(c, "publication")
        rid = await w.write_artifact(c, "result", data, ctx.lim.max_result_bytes)
        return await _publish_outcome(w, c, {"result": rid})
    if e.documents is None:
        raise DocumentsNotConfiguredError()
    return await _document(w, c, row, snap, prepared, requested, context_text)


async def _publish_outcome(w: JobWorker, c: ClaimedJob, ids: dict[str, str]) -> str:
    """Publication with reconciliation BEFORE any repetition (docs 7.3): an unknown commit is re-read, never re-run."""
    from ..errors import DatabaseUnavailableError

    try:
        outcome = await w.publish(c, ids)
    except DatabaseUnavailableError:  # the answer was lost: did our transaction commit?
        outcome = "unknown"
    if outcome in ("lease_lost", "unknown"):
        state = await _reconcile(w, c, ids)
        if state == "succeeded":
            return "succeeded"
        if (
            state == "mine" and outcome == "unknown"
        ):  # still running with our possession: the same conditional publish is safe
            outcome = await w.publish(c, ids)
        if outcome in ("lease_lost", "unknown"):
            raise LeaseLost
    return outcome


async def _reconcile(w: JobWorker, c: ClaimedJob, ids: dict[str, str]) -> str:
    t = w.ctx.t

    async def fn(conn: AsyncConnection) -> Any:
        return (
            await conn.execute(sa.select(t.jobs).where(t.jobs.c.scope_id == c.scope_id, t.jobs.c.job_id == c.job_id))
        ).first()

    row: Any = await w.ctx.run(fn, write=False)
    if row is None:
        return "gone"
    if (
        row.state == "succeeded"
        and row.published_attempt_no == c.attempt_no
        and row.result_artifact_id == ids.get("result")
    ):
        return "succeeded"
    if row.state == "running" and row.attempt_no == c.attempt_no and row.lease_token == c.lease_token:
        return "mine"
    return "other"


def _isolation_key(docs: Any, lim: Any) -> tuple[Any, ...]:
    """What an approval is bound to: the runner instance, its profile and EVERY limit the job will use."""
    return (
        id(docs.runner),
        docs.runner.profile,
        bool(docs.untrusted_input),
        int(lim.extraction_memory_bytes),
        int(lim.extraction_max_pids),
        int(lim.extraction_cpu_s),
        float(lim.extraction_timeout_s),
    )


async def _verify_isolation(w: JobWorker) -> None:
    """Approval lives on the engine instance (never in a module-level cache) and only for the configuration it was
    obtained with; any failure is `configuration_unavailable` and nothing is extracted (no fallback)."""
    docs = w.e.documents
    assert docs is not None
    lim = w.ctx.lim
    key = _isolation_key(docs, lim)
    if w.e._isolation_approved == key:
        return
    ok = False
    try:
        if docs.runner.profile == "unconfined_development" and not docs.untrusted_input:
            ok = True  # explicit development opt-in: there are no controls to verify
        else:
            verify = getattr(docs.runner, "verify", None)
            if verify is not None:
                try:
                    takes_limits = len(inspect.signature(verify).parameters) >= 1
                except (TypeError, ValueError):
                    takes_limits = False
                ok = bool(await (verify(lim) if takes_limits else verify()))
    except Exception:
        ok = False
    if not ok:
        w.e._isolation_approved = None
        raise IsolationUnavailableError()
    w.e._isolation_approved = key


async def _document(
    w: JobWorker,
    c: ClaimedJob,
    row: Any,
    snap: dict[str, Any],
    prepared: Any,
    requested: tuple[str, ...],
    context_text: str | None,
) -> str:
    ctx, e = w.ctx, w.e
    lim = ctx.lim
    docs = e.documents
    assert docs is not None
    await w.set_phase(c, "validation")
    await _verify_isolation(w)
    data = await _read_input(w, c.scope_id, snap["input"]["file_id"], limit=lim.max_file_bytes)
    params = {
        k: getattr(lim, k)
        for k in (
            "max_file_bytes",
            "max_pdf_pages",
            "max_zip_entries",
            "max_docx_parts_processed",
            "max_decompressed_total_bytes",
            "max_decompressed_entry_bytes",
            "max_compression_ratio",
            "max_xml_depth",
            "max_xml_nodes_total",
            "max_table_nesting",
            "max_pdf_objects",
            "max_pdf_content_bytes_per_page",
            "max_form_xobject_depth",
            "max_text_operators_per_page",
            "max_units_per_document",
            "max_document_text_chars",
            "max_extraction_output_bytes",
        )
    }
    try:
        out = await docs.runner.run(data, params, timeout_s=lim.extraction_timeout_s, limits=lim)
    except TimeoutError:
        raise JobFailure("limit_exceeded") from None
    except (JobFailure, LeaseLost, IsolationUnavailableError):
        raise
    except Exception:
        raise JobFailure("extraction_process_failed") from None
    identity = row.validation_identity
    if out.get("rejected"):
        reason = str(out["rejected"])
        await w.record_validation(c, snap["input"]["file_id"], identity, "rejected", reason)
        raise JobFailure("file_rejected", reason)
    await w.record_validation(c, snap["input"]["file_id"], identity, "passed", None)
    await w.set_phase(c, "extraction")
    units = out["units"]
    if len(units) > lim.max_units_per_document or sum(len(u["text"]) for u in units) > lim.max_document_text_chars:
        raise JobFailure("limit_exceeded")
    alim = e.config.limits
    for u in units:
        if (
            len(u["text"]) > alim.max_analysis_text_chars
            or len(u["text"].encode("utf-8")) > alim.max_analysis_text_bytes
        ):
            raise JobFailure("unit_too_large")
    analyzable = [u for u in units if not u["empty"]]
    if not analyzable:
        raise JobFailure("no_extractable_text")
    await w.set_phase(c, "analysis")
    sem = asyncio.Semaphore(lim.unit_inference_concurrency)
    results: dict[str, Any] = {}

    async def one(u: dict[str, Any]) -> None:
        async with sem:
            res = await execute_prepared(
                e,
                prepared,
                requested=requested,
                text=u["text"],
                context=context_text,
                request_id=f"{c.job_id}-{u['unit_id']}",
            )
            results[u["unit_id"]] = res.model_dump(mode="json")

    async with asyncio.TaskGroup() as tg:
        for u in analyzable:
            tg.create_task(one(u))
    manifest = _manifest(c, row, snap, units, results, out, requested)
    mdata = _dump(manifest)
    smap = _dump(
        {
            "schema": "document-structures/1",
            "identity": {
                "file_sha256": snap["input"]["sha256"],
                "size_bytes": snap["input"]["size_bytes"],
                "format": out["format"],
                "extractor": {"id": EXTRACTOR_ID, "version": EXTRACTOR_VERSION, "config_sha256": config_sha(lim)},
            },
            "units": [{"unit_id": u["unit_id"], "segments": u["segments"]} for u in units],
        }
    )
    if len(mdata) > lim.max_result_bytes or len(smap) > lim.max_source_map_bytes:
        raise JobFailure("limit_exceeded")
    await w.set_phase(c, "publication")
    rid = await w.write_artifact(c, "result", mdata, lim.max_result_bytes)
    sid = await w.write_artifact(c, "source_map", smap, lim.max_source_map_bytes)
    return await _publish_outcome(w, c, {"result": rid, "source_map": sid})


def _manifest(
    c: ClaimedJob,
    row: Any,
    snap: dict[str, Any],
    units: list[dict[str, Any]],
    results: dict[str, Any],
    out: dict[str, Any],
    requested: tuple[str, ...],
) -> dict[str, Any]:
    executable = set(snap["capabilities_executable"])
    per_cap: dict[str, Any] = {}
    for cap in ("classification", "detection"):
        if cap not in requested:
            per_cap[cap] = {"status": "not_requested"}
        elif cap not in executable:
            per_cap[cap] = {"status": "not_configured"}
        else:
            caps = [results[uid][cap] for uid in results]
            inconclusive = sum(1 for x in caps if x["status"] == "inconclusive")
            per_cap[cap] = {
                "status": "inconclusive" if inconclusive else "completed",
                "complete": all(x["coverage"]["complete"] for x in caps),
                "units_completed": len(caps) - inconclusive,
                "units_inconclusive": inconclusive,
            }
    executed = [v for v in per_cap.values() if "complete" in v]
    textual = bool(executed) and all(v["complete"] for v in executed)
    declared = out.get("declared_not_covered", [])
    status = "inconclusive" if any(v["status"] == "inconclusive" for v in executed) else "completed"
    return {
        "result_contract_version": 1,
        "job_id": c.job_id,
        "operation": "document_analysis",
        "document": {
            "file_id": snap["input"]["file_id"],
            "sha256": snap["input"]["sha256"],
            "size_bytes": snap["input"]["size_bytes"],
            "format": out["format"],
        },
        "profile": {
            "scope_id": row.scope_id,
            "profile_id": row.profile_id,
            "version_id": row.version_id,
            "version_number": row.version_number,
            "content_hash": row.profile_content_hash,
            "selection": row.selection,
        },
        "provenance": {
            "inference": snap["inference_provenance"],
            "extractor": snap["extractor"],
            "isolation_profile": snap["isolation_profile"],
        },
        "units": [
            {
                "unit_id": u["unit_id"],
                "kind": u["kind"],
                "ordinal": u["ordinal"],
                "text_length": len(u["text"]),
                "status": "empty" if u["empty"] else "analyzed",
                **({} if u["empty"] else {"analysis": results[u["unit_id"]]}),
            }
            for u in units
        ],
        "coverage": {
            "extraction": {
                "textual_subset_complete": textual,
                "document_coverage_complete": textual and not declared,
                "declared_not_covered": declared,
                "authorized_removals_performed": [],
            },
            "inference": {
                "units_total": len(units),
                "units_analyzed": len(results),
                "units_empty": len(units) - len(results),
                "per_capability": per_cap,
            },
        },
        "status": status,
    }


_ = jdb
