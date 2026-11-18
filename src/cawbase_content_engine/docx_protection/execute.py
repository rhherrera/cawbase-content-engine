"""Worker side of A07 (docs/docx-protection.md sections 3-7 and 10): one claimed job, run to its outcome.

Protection: bind the source analysis to the exact original (S1-S11) -> plan (R1-R5, fields, E preconditions) ->
markers in the context (A06 functions) -> transform -> verify (A05 extractor + independent verifier) -> publish
with the binding in the same transaction. Restoration: digest -> positions -> decrypt (A06) -> restore -> verify ->
publish. Every transformation and verification runs in the isolated child; the parent never parses the package.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import (
    AnalysisSourceInvalidError,
    ContextKindMismatchError,
    ContextUnavailableError,
    EngineError,
    IsolationUnavailableError,
    MappingIntegrityError,
    MarkerUnknownError,
    ProtectionKeyUnavailableError,
    QuotaExceededError,
    ResourceNotFoundError,
)
from ..jobs import db as jdb
from ..jobs.execute import _dump, _publish_outcome, _read_input, _verify_isolation, extraction_params, source_map_bytes
from ..jobs.ident import EXTRACTOR_ID, EXTRACTOR_VERSION, config_sha
from ..jobs.models import ClaimedJob
from ..jobs.ops import published_artifact
from ..jobs.worker import JobFailure, LeaseLost, PublishHook
from ..protection import ops as pops
from ..protection.grouping import plan_protection
from ..protection.markers import REDACTED, marker_of, scan, token_of
from . import binding as bd

if TYPE_CHECKING:
    from ..jobs.worker import JobWorker

log = logging.getLogger("cawbase_content_engine.docx")
MAX_SELECTIONS = 10_000

_A06_CODES: dict[type[EngineError], str] = {
    ContextUnavailableError: "context_unavailable",
    ContextKindMismatchError: "context_kind_mismatch",
    ProtectionKeyUnavailableError: "protection_key_unavailable",
    MappingIntegrityError: "mapping_integrity_failed",
    MarkerUnknownError: "marker_unknown",
    QuotaExceededError: "quota_exceeded",
    AnalysisSourceInvalidError: "analysis_source_invalid",
    ResourceNotFoundError: "context_unavailable",
}


def _a06(exc: EngineError) -> JobFailure:
    for cls, code in _A06_CODES.items():
        if isinstance(exc, cls):
            return JobFailure(code)
    raise exc


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _child(w: JobWorker, data: bytes, mode: str | None, a07: dict[str, Any] | None = None) -> dict[str, Any]:
    """One run of the isolated child (A05 runner, same limits). Child crash -> retryable; timeout -> limit."""
    docs = w.e.documents
    assert docs is not None
    lim = w.ctx.lim
    params = extraction_params(lim)
    if mode is not None:
        params["mode"] = mode
        params["a07"] = a07 or {}
    try:
        out = await docs.runner.run(data, params, timeout_s=lim.extraction_timeout_s, limits=lim)
    except TimeoutError:
        raise JobFailure("limit_exceeded") from None
    except (JobFailure, LeaseLost, IsolationUnavailableError):
        raise
    except Exception:
        raise JobFailure("extraction_process_failed") from None
    if not isinstance(out, dict):
        raise JobFailure("extraction_process_failed")
    return out


async def _artifact_bytes(w: JobWorker, art: Any) -> bytes:
    """Whole object, verified (SHA-256 and size) as every A05 read; a corrupt object fails closed."""
    ctx = w.ctx
    from ..jobs.setup import ObjectMissing, StorageError

    buf = bytearray()
    try:
        async for chunk in ctx.storage.open_read(art.storage_key):
            buf += chunk
            if len(buf) > int(art.size_bytes):
                raise JobFailure("stored_object_corrupt")
    except ObjectMissing:
        raise JobFailure("stored_object_corrupt") from None
    except StorageError:
        raise JobFailure("storage_unavailable") from None
    if len(buf) != int(art.size_bytes) or hashlib.sha256(buf).digest() != bytes(art.sha256):
        raise JobFailure("stored_object_corrupt")
    return bytes(buf)


class _TxFailure(EngineError):
    """Carries a job failure code out of a transaction unchanged (internal control flow, never to a caller)."""

    http_status = None

    def __init__(self, job_code: str) -> None:
        super().__init__()
        self.job_code = job_code


async def _owner_tx(w: JobWorker, c: ClaimedJob, body: Any) -> Any:
    """1 (scope, shared) -> 8 (own job, shared: possession) -> ``body`` (ranks 12+); decides after every lock."""
    ctx = w.ctx

    async def fn(conn: AsyncConnection) -> Any:
        t = ctx.t
        scope = await jdb.lock_scope(conn, t, c.scope_id, exclusive=False)
        row = (
            await conn.execute(
                sa.select(t.jobs)
                .where(t.jobs.c.scope_id == c.scope_id, t.jobs.c.job_id == c.job_id)
                .with_for_update(read=True)
            )
        ).first()
        try:
            return await body(conn, scope, row)
        except JobFailure as f:
            raise _TxFailure(f.code) from None

    try:
        return await ctx.run(fn)
    except _TxFailure as f:
        raise JobFailure(f.job_code) from None


def _owned(w: JobWorker, c: ClaimedJob, scope: Any, row: Any, now: datetime) -> None:
    if scope.status != "active" or not w._owner_ok(row, c, now):
        raise LeaseLost


# ---- protection ------------------------------------------------------------------------------------
async def run_protection(w: JobWorker, c: ClaimedJob, row: Any, snap: dict[str, Any]) -> str:
    ctx, e = w.ctx, w.e
    setup = e.protection
    if setup is None or e.documents is None:
        raise JobFailure("configuration_unavailable")
    lim = ctx.lim
    await w.set_phase(c, "validation")
    await _verify_isolation(w)
    analysis_id = snap["analysis_job_id"]
    ctx_id, kind = snap["protection_context"]["context_id"], snap["protection_context"]["kind"]

    async def read_source(conn: AsyncConnection) -> Any:
        t = ctx.t
        return (
            await conn.execute(sa.select(t.jobs).where(t.jobs.c.scope_id == c.scope_id, t.jobs.c.job_id == analysis_id))
        ).first()

    a_row = await ctx.run(read_source, write=False)
    if a_row is None:
        raise JobFailure("analysis_unavailable")
    try:
        r_art = await published_artifact(ctx, c.scope_id, analysis_id, "result", "document_analysis")
        m_art = await published_artifact(ctx, c.scope_id, analysis_id, "source_map", "document_analysis")
    except EngineError:
        raise JobFailure("analysis_unavailable") from None
    manifest = json.loads(await _artifact_bytes(w, r_art))
    smap_bytes = await _artifact_bytes(w, m_art)
    smap = json.loads(smap_bytes)
    original = await _read_input(w, c.scope_id, snap["input"]["file_id"], limit=lim.max_file_bytes)
    a_snap = (
        json.loads(a_row.execution_snapshot) if isinstance(a_row.execution_snapshot, str) else a_row.execution_snapshot
    )
    # S5-S7, S10, S10b: identity of the original, of the extractor and of the analysis
    if manifest.get("job_id") != analysis_id or manifest.get("operation") != "document_analysis":
        raise JobFailure("analysis_source_invalid")
    doc = manifest.get("document", {})
    ident = smap.get("identity", {})
    sha, size = _sha(original), len(original)
    if not (
        doc.get("sha256") == ident.get("file_sha256") == a_snap["input"]["sha256"] == sha
        and doc.get("size_bytes") == ident.get("size_bytes") == a_snap["input"]["size_bytes"] == size
        and doc.get("format") == ident.get("format") == "docx"
    ):
        raise JobFailure("analysis_source_invalid")
    current = {"id": EXTRACTOR_ID, "version": EXTRACTOR_VERSION, "config_sha256": config_sha(lim)}
    if ident.get("extractor") != current or manifest.get("provenance", {}).get("extractor") != current:
        raise JobFailure("analysis_source_stale")
    prof = manifest.get("profile", {})
    if (
        prof.get("scope_id") != c.scope_id
        or prof.get("profile_id") != a_row.profile_id
        or prof.get("version_id") != a_row.version_id
        or prof.get("version_number") != a_row.version_number
        or prof.get("content_hash") != a_row.profile_content_hash
        or prof.get("selection") != a_row.selection
    ):
        raise JobFailure("analysis_source_invalid")  # S10
    units_m = manifest.get("units", [])
    if not bd.provenance_ok(
        manifest.get("provenance", {}).get("inference"), a_snap.get("inference_provenance"), units_m
    ):
        raise JobFailure("analysis_provenance_unsupported")  # S10b
    det = manifest.get("coverage", {}).get("inference", {}).get("per_capability", {}).get("detection", {})
    if det.get("status") in ("not_requested", "not_configured"):
        raise JobFailure("capabilities_not_configured")
    if det.get("status") != "completed" or det.get("complete") is not True:
        raise JobFailure("detection_inconclusive")  # S9
    for u in units_m:
        if u.get("status") == "analyzed":
            dres = u["analysis"]["detection"]
            if dres.get("status") != "completed" or not dres.get("coverage", {}).get("complete"):
                raise JobFailure("detection_inconclusive")
    # S8: the exact map of the exact original (re-extraction in the isolated child)
    await w.set_phase(c, "extraction")
    out = await _child(w, original, None)
    if out.get("rejected"):
        raise JobFailure("analysis_source_stale")
    units = out["units"]
    if source_map_bytes(sha, size, out["format"], lim, units) != smap_bytes or len(units) != len(units_m):
        raise JobFailure("analysis_source_stale")
    for u, um in zip(units, units_m, strict=True):
        if u["unit_id"] != um.get("unit_id") or len(u["text"]) != um.get("text_length"):
            raise JobFailure("analysis_source_stale")
    # planning: A06 grouping per unit; collision (PC-04); R1-R5 and E preconditions in the child
    await w.set_phase(c, "planning")
    if any(scan(u["text"]).strict or scan(u["text"]).altered for u in units):
        raise JobFailure("marker_collision")
    policy = snap["policy"]
    plans: list[tuple[dict[str, Any], list[Any]]] = []
    for u, um in zip(units, units_m, strict=True):
        if um.get("status") != "analyzed":
            plans.append((u, []))
            continue
        findings = [(f["type_id"], int(f["start"]), int(f["end"])) for f in um["analysis"]["detection"]["findings"]]
        try:
            groups = plan_protection(findings, len(u["text"]), policy["default_action"], policy["actions"])
        except AnalysisSourceInvalidError:
            raise JobFailure("analysis_source_invalid") from None
        plans.append((u, groups))
    selections = [
        {"unit_id": u["unit_id"], "start": g.start, "end": g.end, "substitute": ""} for u, gs in plans for g in gs
    ]
    if len(selections) > MAX_SELECTIONS:
        raise JobFailure("limit_exceeded")
    checked = await _child(w, original, "docx_plan", {"selections": selections})
    if checked.get("failure") or checked.get("rejected"):
        raise JobFailure(str(checked.get("failure") or "analysis_source_stale"))
    # mapping: markers of the context (A06 obtain_tokens: same fragment -> same marker, verified reuse)
    await w.set_phase(c, "mapping")
    fragments = list(
        dict.fromkeys(u["text"][g.start : g.end] for u, gs in plans for g in gs if g.action == "pseudonymize")
    )

    async def mapping(conn: AsyncConnection, scope: Any, jrow: Any) -> Any:
        crow = await pops._context_row(conn, e, c.scope_id, ctx_id, "no_key_update")  # 12
        now = await jdb.db_now(conn, ctx.backend)  # after every lock wait
        _owned(w, c, scope, jrow, now)
        pops._check_usable(crow, kind, now)
        return await pops.obtain_tokens(conn, e, setup, c.scope_id, ctx_id, kind, crow, fragments, now)

    try:
        token_of_fragment, reused, created = await _owner_tx(w, c, mapping)
    except (LeaseLost, JobFailure):
        raise
    except EngineError as exc:
        raise _a06(exc) from None
    # transformation
    await w.set_phase(c, "transformation")
    sels, report_units, oracle, subs, markers_at, values = [], [], [], [], [], []
    pseudo = redacted = 0
    for u, gs in plans:
        triples = []
        for g in gs:
            frag = u["text"][g.start : g.end]
            sub = marker_of(token_of_fragment[frag]) if g.action == "pseudonymize" else REDACTED
            triples.append((g.start, g.end, sub))
            sels.append({"unit_id": u["unit_id"], "start": g.start, "end": g.end, "substitute": sub})
            values.append(frag)
            pseudo += g.action == "pseudonymize"
            redacted += g.action == "redact"
        text, spans = bd.oracle_text(u["text"], triples)
        oracle.append(text)
        subs.append([list(sp) for sp in spans])
        markers_at.append([list(sp) for sp, (_s, _e, sub) in zip(spans, triples, strict=True) if sub != REDACTED])
        report_units.append(
            {
                "unit_id": u["unit_id"],
                "groups": [
                    {
                        "action": g.action,
                        "input_start": g.start,
                        "input_end": g.end,
                        "output_start": sp[0],
                        "output_end": sp[1],
                        "marker": sub if sub != REDACTED else None,
                    }
                    for g, sp, (_s, _e, sub) in zip(gs, spans, triples, strict=True)
                ],
            }
        )
    done = await _child(w, original, "docx_protect", {"selections": sels})
    if done.get("failure") or done.get("rejected"):
        raise JobFailure(str(done.get("failure") or "derivative_verification_failed"))
    derivative = base64.b64decode(done["derivative"])
    if len(derivative) > lim.max_file_bytes:
        raise JobFailure("limit_exceeded")
    # verification: A05 extractor on the derivative (positions of the binding) + independent verifier
    await w.set_phase(c, "verification")
    dext = await _child(w, derivative, None)
    if dext.get("rejected") or len(dext["units"]) != len(units):
        raise JobFailure("derivative_verification_failed")
    occurrences: list[dict[str, Any]] = []
    for du, text, (_u, gs), osp in zip(dext["units"], oracle, plans, subs, strict=True):
        if du["text"] != text:
            raise JobFailure("derivative_verification_failed")
        for g, sp in zip(gs, osp, strict=True):
            if g.action != "pseudonymize":
                continue
            occ = bd.occurrence_at(du, sp[0], sp[1])
            if occ is None:
                raise JobFailure("derivative_verification_failed")
            occ["token"] = token_of(du["text"][sp[0] : sp[1]])
            occurrences.append(occ)
    packed = len(original).to_bytes(8, "big") + original + derivative
    ver = await _child(
        w,
        packed,
        "docx_verify",
        {"oracle": oracle, "values": values, "subs": subs, "markers": markers_at, "mode": "protect"},
    )
    if not ver.get("ok"):
        raise JobFailure(str(ver.get("failure") or "derivative_verification_failed"))
    # publication: artifacts, then T3 + binding in ONE transaction (hook)
    await w.set_phase(c, "publication")
    d_sha = _sha(derivative)
    did = snap["derivative_id"]
    removed = done.get("removed", [])
    declared = [
        x
        for x in manifest.get("coverage", {}).get("extraction", {}).get("declared_not_covered", [])
        if x.get("reason") != "not_analyzed"
    ]
    textual = bool(manifest.get("coverage", {}).get("extraction", {}).get("textual_subset_complete"))
    from ..documents.docx_transform import TRANSFORMER_ID, TRANSFORMER_VERSION

    report = {
        "contract_version": 1,
        "job_id": c.job_id,
        "derivative_id": did,
        "derivative": {"sha256": d_sha, "size_bytes": len(derivative), "format": "docx"},
        "source": {
            "analysis_job_id": analysis_id,
            "file_id": snap["input"]["file_id"],
            "file_sha256": sha,
            "extractor": current,
            "profile": prof,
            "provenance": bd.projection(a_snap["inference_provenance"]),
        },
        "context": {"context_id": ctx_id, "kind": kind},
        "policy": policy,
        "units": [u for u in report_units if u["groups"]],
        "counts": {
            "findings": sum(g.findings for _u, gs in plans for g in gs),
            "groups": len(sels),
            "pseudonymized": pseudo,
            "redacted": redacted,
            "new_markers": created,
            "reused_markers": reused,
            "occurrences": len(occurrences),
        },
        "authorized_removals_performed": removed,
        "coverage": {
            "textual_subset_complete": textual,
            "document_coverage_complete": textual and not declared,
            "declared_not_covered": declared,
        },
        "inspection_scope": ver.get("inspection_scope", {}),
        "verification": {"package": "passed", "oracle": "passed", "structure": "passed", "residues": "passed"},
        "transformer": {"id": TRANSFORMER_ID, "version": TRANSFORMER_VERSION},
    }
    rdata = _dump(report)
    if len(rdata) > lim.max_result_bytes:
        raise JobFailure("limit_exceeded")

    async def ctx_key(conn: AsyncConnection) -> Any:
        return await pops._context_row(conn, e, c.scope_id, ctx_id)

    crow = await ctx.run(ctx_key, write=False)
    if crow is None or crow.index_key_wrapped is None:
        raise JobFailure("context_unavailable")
    try:
        index_key = pops.context_index_key(setup, c.scope_id, ctx_id, crow)
    except EngineError as exc:
        raise _a06(exc) from None
    did_art = await w.write_artifact(c, "docx_protected", derivative, len(derivative))
    rep_art = await w.write_artifact(c, "docx_protection_report", rdata, lim.max_result_bytes)
    binding_row = {
        "scope_id": c.scope_id,
        "derivative_id": did,
        "protection_job_id": c.job_id,
        "attempt_no": c.attempt_no,
        "context_id": ctx_id,
        "context_kind": kind,
        "artifact_id": did_art,
        "sha256": d_sha,
        "size_bytes": len(derivative),
        "extractor_id": EXTRACTOR_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "transformer_id": TRANSFORMER_ID,
        "transformer_version": TRANSFORMER_VERSION,
        "profile_id": a_row.profile_id,
        "version_id": a_row.version_id,
        "version_number": a_row.version_number,
        "profile_content_hash": a_row.profile_content_hash,
        "selection": a_row.selection,
        "occurrences": occurrences,
        "occurrence_count": len(occurrences),
        "mac_key_version": crow.index_key_version,
        "state": "active",
    }
    binding_row["binding_mac"] = bd.binding_mac(index_key, binding_row)
    tokens = sorted({o["token"] for o in occurrences})
    hook = _ProtectionHook(w, c, analysis_id, ctx_id, kind, tokens, binding_row)
    return await _publish_outcome(w, c, {"docx_protected": did_art, "docx_protection_report": rep_art}, hook)


class _ProtectionHook(PublishHook):
    def __init__(
        self,
        w: JobWorker,
        c: ClaimedJob,
        analysis_id: str,
        ctx_id: str,
        kind: str,
        tokens: list[str],
        row: dict[str, Any],
    ) -> None:
        self.w, self.c, self.ctx_id, self.kind, self.tokens, self.row = w, c, ctx_id, kind, tokens, row
        self.share_jobs = (analysis_id,)
        self.analysis_id = analysis_id
        self.crow: Any = None

    async def lock(self, conn: AsyncConnection) -> None:
        self.crow = await pops._context_row(conn, self.w.e, self.c.scope_id, self.ctx_id, "share")  # 12

    async def decide(self, conn: AsyncConnection, now: datetime, jobs: dict[str, Any]) -> str | None:
        a = jobs.get(self.analysis_id)
        if a is None or a.state != "succeeded" or a.data_state != "available" or a.result_expires_at <= now:
            return "analysis_unavailable"
        if self.crow is None or self.crow.state != "active" or now >= self.crow.expires_at:
            return "context_unavailable"
        if self.crow.kind != self.kind:
            return "context_kind_mismatch"
        if self.tokens:
            m = self.w.ctx.t.protection_mappings
            n = (
                await conn.execute(
                    sa.select(sa.func.count()).where(
                        m.c.scope_id == self.c.scope_id, m.c.context_id == self.ctx_id, m.c.token.in_(self.tokens)
                    )
                )
            ).scalar_one()
            if int(n) != len(self.tokens):
                return "context_unavailable"
        return None

    async def after(self, conn: AsyncConnection, now: datetime) -> None:
        d = self.w.ctx.t.docx_derivatives
        await conn.execute(sa.insert(d).values(**self.row, created_at=now))  # 14: with T3, never without it


# ---- restoration -----------------------------------------------------------------------------------
async def _binding(w: JobWorker, scope_id: str, did: str) -> Any:
    async def fn(conn: AsyncConnection) -> Any:
        d = w.ctx.t.docx_derivatives
        return (await conn.execute(sa.select(d).where(d.c.scope_id == scope_id, d.c.derivative_id == did))).first()

    return await w.ctx.run(fn, write=False)


def _occ_key(o: Any) -> tuple[Any, ...]:
    return (o["unit_id"], o["start"], o["end"], o["part_name"], o["node_path"], o["char_start"], o["char_end"])


def check_positions(units: list[dict[str, Any]], occurrences: list[dict[str, Any]]) -> bool:
    """T5 (pure): strict markers exactly at the recorded occurrences, none elsewhere, no R1/R2 anywhere."""
    found: list[tuple[Any, ...]] = []
    for u in units:
        sc = scan(u["text"])
        if sc.altered:
            return False
        for s, e, marker in sc.strict:
            occ = bd.occurrence_at(u, s, e)
            if occ is None:
                return False
            found.append((*_occ_key(occ), token_of(marker)))
    expected = [(*_occ_key(o), o["token"]) for o in occurrences]
    return sorted(found) == sorted(expected)


async def run_restoration(w: JobWorker, c: ClaimedJob, row: Any, snap: dict[str, Any]) -> str:
    ctx, e = w.ctx, w.e
    setup = e.protection
    if setup is None or e.documents is None:
        raise JobFailure("configuration_unavailable")
    lim = ctx.lim
    await w.set_phase(c, "validation")
    await _verify_isolation(w)
    did = snap["derivative_id"]
    ctx_id, kind = snap["protection_context"]["context_id"], snap["protection_context"]["kind"]
    b = await _binding(w, c.scope_id, did)
    if b is None or b.state != "active" or b.context_id != ctx_id:  # T1
        raise JobFailure("derivative_binding_unavailable")
    if b.context_kind != kind:
        raise JobFailure("context_kind_mismatch")
    copy = await _read_input(w, c.scope_id, snap["input"]["file_id"], limit=lim.max_file_bytes)
    if _sha(copy) != b.sha256 or len(copy) != int(b.size_bytes):  # T2: before anything parses the file
        raise JobFailure("derivative_mismatch")
    await w.set_phase(c, "extraction")
    out = await _child(w, copy, None)
    occurrences = list(b.occurrences or [])
    if (
        out.get("rejected")
        or b.extractor_id != EXTRACTOR_ID
        or b.extractor_version != EXTRACTOR_VERSION
        or not check_positions(out["units"], occurrences)
    ):
        raise JobFailure("derivative_markers_invalid")  # T5
    await w.set_phase(c, "mapping")
    tokens = sorted({o["token"] for o in occurrences})

    async def decrypt(conn: AsyncConnection, scope: Any, jrow: Any) -> Any:
        crow = await pops._context_row(conn, e, c.scope_id, ctx_id, "share")  # 12
        d = ctx.t.docx_derivatives
        brow = (
            await conn.execute(
                sa.select(d).where(d.c.scope_id == c.scope_id, d.c.derivative_id == did).with_for_update(read=True)
            )
        ).first()  # 14
        now = await jdb.db_now(conn, ctx.backend)  # after every lock wait
        _owned(w, c, scope, jrow, now)
        pops._check_usable(crow, kind, now)  # T3
        if brow is None or brow.state != "active":
            raise JobFailure("derivative_binding_unavailable")
        index_key = pops.context_index_key(setup, c.scope_id, ctx_id, crow)
        if not bd.mac_ok(index_key, dict(brow._mapping), brow.binding_mac):  # T4
            raise JobFailure("binding_integrity_failed")
        if [dict(o) for o in brow.occurrences or []] != occurrences:
            raise JobFailure("binding_integrity_failed")
        return await pops.decrypt_tokens(conn, e, setup, c.scope_id, ctx_id, kind, tokens)  # 13 (T6)

    try:
        originals = await _owner_tx(w, c, decrypt)
    except (LeaseLost, JobFailure):
        raise
    except EngineError as exc:
        raise _a06(exc) from None
    await w.set_phase(c, "transformation")
    occ_in = [{**o, "marker": marker_of(o["token"]), "value": originals[o["token"]]} for o in occurrences]
    done = await _child(w, copy, "docx_restore", {"occurrences": occ_in})
    if done.get("failure") or done.get("rejected"):
        raise JobFailure(str(done.get("failure") or "derivative_verification_failed"))
    restored = base64.b64decode(done["restored"])
    await w.set_phase(c, "verification")
    oracle = []
    by_unit: dict[str, list[dict[str, Any]]] = {}
    for o in occ_in:
        by_unit.setdefault(o["unit_id"], []).append(o)
    for u in out["units"]:
        text = u["text"]
        for o in sorted(by_unit.get(u["unit_id"], []), key=lambda x: -int(x["start"])):
            text = text[: o["start"]] + o["value"] + text[o["end"] :]
        oracle.append(text)
    rext = await _child(w, restored, None)
    if rext.get("rejected") or [u["text"] for u in rext["units"]] != oracle:
        raise JobFailure("derivative_verification_failed")
    packed = len(copy).to_bytes(8, "big") + copy + restored
    ver = await _child(
        w, packed, "docx_verify", {"oracle": oracle, "values": [], "subs": [[] for _ in oracle], "mode": "restore"}
    )
    if not ver.get("ok"):
        raise JobFailure(str(ver.get("failure") or "derivative_verification_failed"))
    await w.set_phase(c, "publication")
    report = {
        "contract_version": 1,
        "job_id": c.job_id,
        "derivative_id": did,
        "restored": {"sha256": _sha(restored), "size_bytes": len(restored), "format": "docx"},
        "counts": {"occurrences_restored": len(occ_in), "distinct_markers": len(tokens)},
        "removed_classes_absent": [
            "comments",
            "comment_people",
            "core_descriptive_properties",
            "app_descriptive_properties",
            "custom_properties",
            "thumbnail",
        ],
        "verification": {"package": "passed", "oracle": "passed", "structure": "passed"},
    }
    rdata = _dump(report)
    r_art = await w.write_artifact(c, "docx_restored", restored, len(restored))
    rep_art = await w.write_artifact(c, "docx_restoration_report", rdata, lim.max_result_bytes)
    hook = _RestorationHook(w, c, ctx_id, kind, did)
    return await _publish_outcome(w, c, {"docx_restored": r_art, "docx_restoration_report": rep_art}, hook)


class _RestorationHook(PublishHook):
    def __init__(self, w: JobWorker, c: ClaimedJob, ctx_id: str, kind: str, did: str) -> None:
        self.w, self.c, self.ctx_id, self.kind, self.did = w, c, ctx_id, kind, did
        self.crow: Any = None
        self.brow: Any = None

    async def lock(self, conn: AsyncConnection) -> None:
        self.crow = await pops._context_row(conn, self.w.e, self.c.scope_id, self.ctx_id, "share")  # 12
        d = self.w.ctx.t.docx_derivatives
        self.brow = (
            await conn.execute(
                sa.select(d)
                .where(d.c.scope_id == self.c.scope_id, d.c.derivative_id == self.did)
                .with_for_update(read=True)
            )
        ).first()  # 14

    async def decide(self, conn: AsyncConnection, now: datetime, jobs: dict[str, Any]) -> str | None:
        if self.crow is None or self.crow.state != "active" or now >= self.crow.expires_at:
            return "context_unavailable"
        if self.crow.kind != self.kind:
            return "context_kind_mismatch"
        if self.brow is None or self.brow.state != "active":
            return "derivative_binding_unavailable"
        return None
