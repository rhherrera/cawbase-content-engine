"""Worker side of A08 (docs/pdf-protection.md sections 3-10): one claimed job, run to its outcome.

Protection: bind the source analysis to the exact original (S1-S11, extractor v2, map reproduced byte for byte)
-> plan in the child (coverage D-A08-03, localization, zones Z1/Z2) -> markers in the context (A06 functions) ->
transform -> verify (extractor v2 on the derivative for the binding + the independent pypdf verifier) -> publish
the exact artifacts with the authenticated binding in the same transaction. Restoration: digest -> positions ->
decrypt (A06) -> restore from the derivative and the binding only -> verify -> publish. Every PDF is parsed only in
the isolated child; the parent never opens one. A07's possession, publication and A06 pieces are reused.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..docx_protection import binding as dbd
from ..docx_protection.execute import _a06, _artifact_bytes, _owned, _owner_tx, _ProtectionHook, _RestorationHook
from ..errors import AnalysisSourceInvalidError, EngineError, IsolationUnavailableError
from ..jobs import db as jdb
from ..jobs.execute import (
    _dump,
    _publish_outcome,
    _read_input,
    _verify_isolation,
    extraction_params,
    pdf_source_map_bytes,
)
from ..jobs.models import ClaimedJob
from ..jobs.ops import published_artifact
from ..jobs.worker import JobFailure, LeaseLost
from ..protection import ops as pops
from ..protection.grouping import plan_protection
from ..protection.markers import REDACTED, marker_of, scan, token_of
from . import binding as bd

if TYPE_CHECKING:
    from ..jobs.worker import JobWorker

log = logging.getLogger("cawbase_content_engine.pdf")
MAX_SELECTIONS = 10_000
MAX_OCCURRENCES = 10_000
REMOVED_CLASSES = ("document_info_descriptive", "xmp_metadata", "object_metadata", "comments")


class _PdfProtectionHook(_ProtectionHook):
    TABLE = "pdf_derivatives"


class _PdfRestorationHook(_RestorationHook):
    TABLE = "pdf_derivatives"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _child(w: JobWorker, data: bytes, mode: str | None, a08: dict[str, Any] | None = None) -> dict[str, Any]:
    """One run of the isolated child (A05 runner, same limits), always with the PDF extractor v2."""
    docs = w.e.documents
    assert docs is not None
    lim = w.ctx.lim
    params = extraction_params(lim)
    params["pdf_extractor"] = "v2"
    if mode is not None:
        params["mode"] = mode
        params["a08"] = a08 or {}
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


def _current(lim: Any) -> dict[str, str]:
    from ..documents.pdf_extract import identity

    return identity(lim)


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
    # S5-S7, S10, S10b: identity of the original, of the extractor (v2) and of the analysis
    if manifest.get("job_id") != analysis_id or manifest.get("operation") != "document_analysis":
        raise JobFailure("analysis_source_invalid")
    doc = manifest.get("document", {})
    ident = smap.get("identity", {})
    sha, size = _sha(original), len(original)
    if not (
        doc.get("sha256") == ident.get("file_sha256") == a_snap["input"]["sha256"] == sha
        and doc.get("size_bytes") == ident.get("size_bytes") == a_snap["input"]["size_bytes"] == size
        and doc.get("format") == ident.get("format") == "pdf"
    ):
        raise JobFailure("analysis_source_invalid")
    current = _current(lim)
    if ident.get("extractor") != current or manifest.get("provenance", {}).get("extractor") != current:
        raise JobFailure("analysis_source_stale")  # e.g. a PDF analysis of 0.4.0-0.6.0 (extractor v1): re-analyse
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
    if not dbd.provenance_ok(
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
    # S8: the exact map of the exact original (re-extraction with the v2 extractor in the isolated child)
    await w.set_phase(c, "extraction")
    out = await _child(w, original, None)
    if out.get("rejected") or out.get("format") != "pdf":
        raise JobFailure("analysis_source_stale")
    units = out["units"]
    if pdf_source_map_bytes(sha, size, lim, units) != smap_bytes or len(units) != len(units_m):
        raise JobFailure("analysis_source_stale")
    for u, um in zip(units, units_m, strict=True):
        if u["unit_id"] != um.get("unit_id") or len(u["text"]) != um.get("text_length"):
            raise JobFailure("analysis_source_stale")
    # planning: A06 grouping per unit; collision; coverage, localization and zones in the child (before markers)
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
    n_sel = sum(len(gs) for _u, gs in plans)
    if n_sel > MAX_SELECTIONS:
        raise JobFailure("limit_exceeded")
    # pre-check before any marker exists: a placeholder token of the NARROWEST alphabet glyph is a necessary
    # condition (a wider real token can only need more room), so a refusal here is certain and creates nothing
    from ..documents.pdf_transform import candidate
    from ..protection.markers import ALPHABET

    widths = candidate().meta["widths"]
    narrow = min(ALPHABET, key=lambda ch: (widths[ch], ch))
    probe = [
        {
            "unit_id": u["unit_id"],
            "start": g.start,
            "end": g.end,
            "action": g.action,
            "substitute": f"[[CCE1:{narrow * 16}]]" if g.action == "pseudonymize" else REDACTED,
        }
        for u, gs in plans
        for g in gs
    ]
    checked = await _child(w, original, "pdf_plan", {"selections": probe})
    if checked.get("failure") or checked.get("rejected") or not checked.get("ok"):
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
    sels: list[dict[str, Any]] = []
    vsels: list[dict[str, Any]] = []
    report_units = []
    pseudo = redacted = 0
    for u, gs in plans:
        groups_r = []
        for g in gs:
            frag = u["text"][g.start : g.end]
            sub = marker_of(token_of_fragment[frag]) if g.action == "pseudonymize" else REDACTED
            sel = {"unit_id": u["unit_id"], "start": g.start, "end": g.end, "action": g.action, "substitute": sub}
            sels.append(sel)
            vsels.append({**sel, "value": frag})
            pseudo += g.action == "pseudonymize"
            redacted += g.action == "redact"
            groups_r.append(
                {
                    "action": g.action,
                    "input_start": g.start,
                    "input_end": g.end,
                    "marker": sub if sub != REDACTED else None,
                }
            )
        if groups_r:
            report_units.append({"unit_id": u["unit_id"], "groups": groups_r})
    done = await _child(w, original, "pdf_protect", {"selections": sels})
    if done.get("failure") or done.get("rejected") or "derivative" not in done:
        raise JobFailure(str(done.get("failure") or "derivative_verification_failed"))
    derivative = base64.b64decode(done["derivative"])
    layout = done["layout"]
    if len(derivative) > lim.max_file_bytes:
        raise JobFailure("limit_exceeded")
    # verification: v2 extractor on the derivative (occurrences of the binding) + the independent verifier
    await w.set_phase(c, "verification")
    dext = await _child(w, derivative, None)
    if dext.get("rejected") or dext.get("format") != "pdf" or len(dext["units"]) != len(units):
        raise JobFailure("derivative_verification_failed")
    occurrences = bd.occurrences(dext["units"])
    expected = [s["substitute"] for s in sels if s["action"] == "pseudonymize"]
    if occurrences is None or [marker_of(o["token"]) for o in occurrences] != expected:
        raise JobFailure("derivative_verification_failed")
    if len(occurrences) > MAX_OCCURRENCES:
        raise JobFailure("limit_exceeded")
    packed = len(original).to_bytes(8, "big") + original + derivative
    ver = await _child(
        w,
        packed,
        "pdf_verify",
        {"mode": "protect", "units": [u["text"] for u in units], "selections": vsels, "layout": layout},
    )
    if not ver.get("ok"):
        raise JobFailure(str(ver.get("failure") or "derivative_verification_failed"))
    # publication: artifacts, then T3 + binding in ONE transaction (hook)
    await w.set_phase(c, "publication")
    d_sha = _sha(derivative)
    did = snap["derivative_id"]
    removed = done.get("removed", [])
    ext_cov = manifest.get("coverage", {}).get("extraction", {})
    declared = [x for x in ext_cov.get("declared_not_covered", []) if x.get("reason") != "not_analyzed"]
    textual = bool(ext_cov.get("textual_subset_complete"))
    from ..documents.pdf_transform import TRANSFORMER_ID, TRANSFORMER_VERSION

    report = {
        "contract_version": 1,
        "job_id": c.job_id,
        "derivative_id": did,
        "derivative": {"sha256": d_sha, "size_bytes": len(derivative), "format": "pdf"},
        "source": {
            "analysis_job_id": analysis_id,
            "file_id": snap["input"]["file_id"],
            "file_sha256": sha,
            "extractor": current,
            "profile": prof,
            "provenance": dbd.projection(a_snap["inference_provenance"]),
        },
        "context": {"context_id": ctx_id, "kind": kind},
        "policy": policy,
        "units": report_units,
        "counts": {
            "findings": sum(g.findings for _u, gs in plans for g in gs),
            "groups": len(sels),
            "pseudonymized": pseudo,
            "redacted": redacted,
            "new_markers": created,
            "reused_markers": reused,
            "occurrences": len(occurrences),
        },
        "zones": {
            "lines": len(layout),
            "z1": sum(1 for ln in layout if ln["kind"] == "Z1"),
            "z2": sum(1 for ln in layout if ln["kind"] == "Z2"),
        },
        "authorized_removals_performed": removed,
        "coverage": {
            "textual_subset_complete": textual,
            "document_coverage_complete": textual and not declared,
            "declared_not_covered": declared,
            "resources_admitted_by_rule": checked.get("rules", {}),
        },
        "inspection_scope": ver.get("inspection_scope", {}),
        "verification": {
            "structure": "passed",
            "oracle": "passed",
            "geometry": "passed",
            "residues": "passed",
            "binding": "passed",
            "render": "not_run_in_runtime",
        },
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
    did_art = await w.write_artifact(c, "pdf_protected", derivative, len(derivative))
    rep_art = await w.write_artifact(c, "pdf_protection_report", rdata, lim.max_result_bytes)
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
        "extractor_id": current["id"],
        "extractor_version": current["version"],
        "extractor_config_sha256": current["config_sha256"],
        "transformer_id": TRANSFORMER_ID,
        "transformer_version": TRANSFORMER_VERSION,
        "profile_id": a_row.profile_id,
        "version_id": a_row.version_id,
        "version_number": a_row.version_number,
        "profile_content_hash": a_row.profile_content_hash,
        "selection": a_row.selection,
        "occurrences": occurrences,
        "layout": layout,
        "occurrence_count": len(occurrences),
        "mac_key_version": crow.index_key_version,
        "state": "active",
    }
    binding_row["binding_mac"] = bd.binding_mac(index_key, binding_row)
    tokens = sorted({o["token"] for o in occurrences})
    hook = _PdfProtectionHook(w, c, analysis_id, ctx_id, kind, tokens, binding_row)
    log.info("pdf protection published job=%s lines=%d occurrences=%d", c.job_id, len(layout), len(occurrences))
    return await _publish_outcome(w, c, {"pdf_protected": did_art, "pdf_protection_report": rep_art}, hook)


# ---- restoration -----------------------------------------------------------------------------------
async def _binding(w: JobWorker, scope_id: str, did: str) -> Any:
    async def fn(conn: AsyncConnection) -> Any:
        d = w.ctx.t.pdf_derivatives
        return (await conn.execute(sa.select(d).where(d.c.scope_id == scope_id, d.c.derivative_id == did))).first()

    return await w.ctx.run(fn, write=False)


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


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
    current = _current(lim)
    occurrences = [dict(o) for o in (_json(b.occurrences) or [])]
    layout = [dict(x) for x in (_json(b.layout) or [])]
    if (b.extractor_id, b.extractor_version, b.extractor_config_sha256) != (
        current["id"],
        current["version"],
        current["config_sha256"],
    ):
        raise JobFailure("derivative_markers_invalid")
    out = await _child(w, copy, None)
    if out.get("rejected") or out.get("format") != "pdf" or not bd.check_positions(out["units"], occurrences):
        raise JobFailure("derivative_markers_invalid")  # T5, before anything is decrypted
    await w.set_phase(c, "mapping")
    tokens = sorted({o["token"] for o in occurrences})

    async def decrypt(conn: AsyncConnection, scope: Any, jrow: Any) -> Any:
        crow = await pops._context_row(conn, e, c.scope_id, ctx_id, "share")  # 12
        d = ctx.t.pdf_derivatives
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
        m = dict(brow._mapping)
        m["occurrences"], m["layout"] = _json(m["occurrences"]), _json(m["layout"])
        if not bd.mac_ok(index_key, m, brow.binding_mac):  # T4: scope, context, identity, positions, geometry
            raise JobFailure("binding_integrity_failed")
        if [dict(o) for o in m["occurrences"] or []] != occurrences or [dict(x) for x in m["layout"] or []] != layout:
            raise JobFailure("binding_integrity_failed")
        return await pops.decrypt_tokens(conn, e, setup, c.scope_id, ctx_id, kind, tokens)  # 13 (T6)

    try:
        originals = await _owner_tx(w, c, decrypt)
    except (LeaseLost, JobFailure):
        raise
    except EngineError as exc:
        raise _a06(exc) from None
    await w.set_phase(c, "transformation")
    values: dict[str, str] = {}
    for ln in layout:
        for s in ln["sels"]:
            if s["action"] == "pseudonymize":
                tok = token_of(s["substitute"])
                if tok not in originals:
                    raise JobFailure("marker_unknown")
                values[s["sid"]] = originals[tok]
    done = await _child(w, copy, "pdf_restore", {"layout": layout, "values": values})
    if done.get("failure") or done.get("rejected") or "restored" not in done:
        raise JobFailure(str(done.get("failure") or "derivative_verification_failed"))
    restored = base64.b64decode(done["restored"])
    if len(restored) > lim.max_file_bytes:
        raise JobFailure("limit_exceeded")
    await w.set_phase(c, "verification")
    rext = await _child(w, restored, None)
    if rext.get("rejected") or len(rext.get("units", [])) != len(out["units"]):
        raise JobFailure("derivative_verification_failed")
    for u, ru in zip(out["units"], rext["units"], strict=True):
        text = u["text"]
        for o in sorted([o for o in occurrences if o["unit_id"] == u["unit_id"]], key=lambda q: -int(q["start"])):
            text = text[: int(o["start"])] + originals[o["token"]] + text[int(o["end"]) :]
        if "".join(text.split()) != "".join(ru["text"].split()) or scan(ru["text"]).strict:
            raise JobFailure("derivative_verification_failed")
    packed = len(copy).to_bytes(8, "big") + copy + restored
    ver = await _child(w, packed, "pdf_verify", {"mode": "restore", "layout": layout, "values": values})
    if not ver.get("ok"):
        raise JobFailure(str(ver.get("failure") or "derivative_verification_failed"))
    await w.set_phase(c, "publication")
    report = {
        "contract_version": 1,
        "job_id": c.job_id,
        "derivative_id": did,
        "restored": {"sha256": _sha(restored), "size_bytes": len(restored), "format": "pdf"},
        "counts": {"occurrences_restored": len(occurrences), "distinct_markers": len(tokens)},
        "removed_classes_absent": list(REMOVED_CLASSES),
        "verification": {"structure": "passed", "oracle": "passed", "geometry": "passed", "markers": "passed"},
    }
    rdata = _dump(report)
    r_art = await w.write_artifact(c, "pdf_restored", restored, len(restored))
    rep_art = await w.write_artifact(c, "pdf_restoration_report", rdata, lim.max_result_bytes)
    hook = _PdfRestorationHook(w, c, ctx_id, kind, did)
    return await _publish_outcome(w, c, {"pdf_restored": r_art, "pdf_restoration_report": rep_art}, hook)
