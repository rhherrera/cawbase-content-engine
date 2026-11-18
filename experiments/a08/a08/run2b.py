"""Stage 2B measurement (docs/a08-stage2b-protocol.md): 10 native PDFs (W/G/L/C/M x T1/T5), approved criteria
TB (typographic band in the frozen oracle), AT (/ActualText equal to its glyphs, outside every selection), RZ (a Z2
redaction returns with its line), RT as a diagnostic only, zones and expected geometry from the original only.

    .venv/bin/python -m a08.run2b <env> [case ...]

Inputs that each component receives are recorded separately per case (`inputs`): the verifier reads the original and
the frozen oracle; the restorer receives ONLY the derivative bytes, the authenticated binding and the token -> value
table (simulated custody), never the original nor the analysis.
"""

from __future__ import annotations

import hashlib
import json
import platform
import resource
import secrets
import sys
import time
import traceback
from pathlib import Path

from cawbase_content_engine.protection import markers as A06

from . import gate
from . import transform as T
from .manifest import MANIFEST_2B, ROOT, load, selections
from .pdfmodel import Reject, canonical
from .run import baseline
from .transform import PlanFail, plan, protect, restore, seal
from .verify import Fail, verify

CORPUS = ROOT / "corpus2b"
FREEZE = CORPUS / "FREEZE.json"
# Frozen with the protocol (criterion 6): only the rules that survived the stage-2 adversarial controls.
RULES = ("RP3", "RP5", "RP7")
OFFICIAL = {"gate": {"AT"}, "transform": {"RZ"}}
RESIDUE = {"VK", "VX", "VP", "VE", "VS"}
COLLATERAL = {"VT", "VB", "VN", "VG", "VA"}


def official() -> None:
    gate.VARIANT.clear()
    gate.VARIANT.update(OFFICIAL["gate"])
    T.VARIANT.clear()
    T.VARIANT.update(OFFICIAL["transform"])


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def case_pdf(case: dict) -> Path | None:
    cid = case["case_id"]
    if case.get("stage2_pdf"):
        for base in ("", "-private"):
            p = ROOT / case["stage2_pdf"].replace("corpus/", f"corpus{base}/", 1)
            if p.exists():
                return p
        return None
    for base in ("corpus2b", "corpus2b-private"):
        p = ROOT / base / cid / f"{cid}.pdf"
        if p.exists():
            return p
    return None


def _blocker(check: str, detail: str, report: dict) -> str | None:
    if check in RESIDUE:
        return "residue"
    if check in COLLATERAL:
        return "collateral_loss"
    if check == "VR":
        if "poppler" in detail:
            return "collateral_loss"
        vr = report.get("VR") or {}
        if isinstance(vr, dict) and not all(v.get("rt_all_explained_by_glyph_shift_le_1px") for v in vr.get("pdfium_outside_mask", [])):
            return "collateral_loss"
    return None


def selected_font_kinds(ex, plans) -> list[dict]:
    out = []
    for lp in plans:
        for se in lp.sels:
            kinds = set()
            for g in se.glyphs:
                f = ex.interp.fonts[g.font_key]
                base = str(ex.pdf.get_object(f.objgen).get("/BaseFont", "")) if f.objgen else ""
                kinds.add((f.kind, f.subtype, "+" in base[:8]))
            out.append({"sid": se.sid, "fonts": sorted([{"kind": k, "subtype": s, "subset": sub} for k, s, sub in kinds], key=str)})
    return out


def run_case(cid: str, template: str, data: bytes, oracle: dict, m: dict, outdir: Path, sels_override: list | None = None,
             key: bytes | None = None) -> dict:
    official()
    key = key or secrets.token_bytes(32)
    res: dict = {"case_id": cid, "stages": {}}
    st = res["stages"]
    t0 = time.perf_counter()
    if oracle.get("status") != "ok":
        bad = [o for o in oracle["occurrences"] if o["status"] != "ok"]
        st["oracle"] = {"result": "failed", "reason": "oracle_unavailable", "detail": [(o["id"], o.get("reason")) for o in bad]}
        return res
    st["oracle"] = {"result": "passed"}
    try:
        ex = gate.extract(data, RULES)
        st["open"] = st["gate"] = st["extract"] = {"result": "passed"}
        res["inventory"] = {k: v for k, v in ex.inventory.items() if k != "rules_used"} | {"rules_used": ex.inventory["rules_used"]}
        smap_a = canonical(ex.smap)
        res["source_map_deterministic"] = smap_a == canonical(gate.extract(data, RULES).smap)
        res["source_map_sha256"] = sha(smap_a)
    except Reject as r:
        st["gate"] = {"result": "failed", "reason": r.reason, "detail": r.detail}
        return res
    sels = sels_override if sels_override is not None else selections(m, template)
    tokens: dict[str, str] = {}
    try:
        plans = plan(ex, oracle, sels, m["policy"], tokens)
        st["localize"] = st["plan"] = {"result": "passed"}
    except PlanFail as e:
        st["plan"] = {"result": "failed", "reason": e.reason, "detail": e.detail}
        return res
    res["plan"] = [{"page": lp.page, "kind": lp.kind, "f": round(lp.f, 4),
                    "sels": [{"sid": s.sid, "action": s.action, "font": s.font, "attempt": s.attempt, "scale": round(s.scale, 4)} for s in lp.sels],
                    "moved_glyphs": len(lp.moved)} for lp in plans]
    res["selected_fonts"] = selected_font_kinds(ex, plans)
    try:
        pr = protect(data, ex, plans)
        st["remove"] = st["substitute"] = st["write"] = {"result": "passed"}
    except (PlanFail, Reject) as e:
        st["substitute"] = {"result": "failed", "reason": getattr(e, "reason", "error"), "detail": getattr(e, "detail", "")}
        return res
    binding = pr.binding
    res["removed_classes"] = pr.removed_classes
    try:
        exd = gate.extract(pr.data, RULES)
        st["reopen"] = {"result": "passed"}
    except Reject as r:
        st["reopen"] = {"result": "failed", "reason": r.reason, "detail": r.detail}  # nothing emitted: safe failure
        return res
    binding["occurrences"] = [{"page": pm.index + 1, "start": a, "end": b, "token": A06.token_of(mk)}
                              for pm in exd.pages for a, b, mk in A06.scan(pm.text).strict]
    binding = seal(binding, pr.data, key)
    values = {A06.token_of(A06.marker_of(t)): frag for frag, t in tokens.items()}
    sel_values = sorted({s["value"] for s in sels} | {se.value for lp in plans for se in lp.sels})
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{cid}.protected.pdf").write_bytes(pr.data)
    (outdir / f"{cid}.binding.json").write_text(json.dumps(binding, indent=1, ensure_ascii=False) + "\n")
    res["inputs"] = {"verifier_protected": {"original_sha256": sha(data), "oracle_sha256": sha(json.dumps(oracle, sort_keys=True).encode()),
                                            "derivative_sha256": sha(pr.data)}}
    try:
        rep = verify(data, pr.data, oracle, binding, m["policy"], mode="protected", selected_values=sel_values)
        st["verify"] = {"result": "passed", "checks": rep}
    except Fail as f:
        st["verify"] = {"result": "failed", "check": f.check, "detail": f.detail, "checks": f.report}
        b = _blocker(f.check, f.detail, f.report)
        if b:
            res["blocker"] = b
        if not (f.check == "VR" and f.report and not b):
            return res
        res["rt_diagnostic_candidate"] = True  # only the strict PDFium raster failed, inside the 1-px fringe: diagnostic
    res["_artifacts"] = {"protected": pr.data, "binding": binding, "values": values, "sel_values": sel_values, "key": key}
    try:
        # the restorer's ONLY inputs: derivative bytes, authenticated binding, token -> value table (and the custody key)
        rest = restore(pr.data, binding, values, key=key)
        st["restore"] = {"result": "passed"}
    except (PlanFail, Reject) as e:
        st["restore"] = {"result": "failed", "reason": getattr(e, "reason", "error"), "detail": getattr(e, "detail", "")}
        return res
    res["inputs"]["restorer"] = {"derivative_sha256": sha(pr.data), "binding_sha256": sha(json.dumps(binding, sort_keys=True).encode()),
                                 "values": len(values), "original": "not provided", "analysis": "not provided"}
    (outdir / f"{cid}.restored.pdf").write_bytes(rest)
    res["_artifacts"]["restored"] = rest
    res["inputs"]["verifier_restored"] = {"original_sha256": sha(data), "derivative_sha256": sha(pr.data), "restored_sha256": sha(rest)}
    try:
        rep2 = verify(data, rest, oracle, binding, m["policy"], mode="restored", restored_from=pr.data, values=values)
        st["verify_restore"] = {"result": "passed", "checks": rep2}
    except Fail as f:
        st["verify_restore"] = {"result": "failed", "check": f.check, "detail": f.detail, "checks": f.report}
        b = _blocker(f.check, f.detail, f.report)
        if b:
            res["blocker"] = b
        if not (f.check == "VR" and f.report and not b):
            return res
        res["rt_diagnostic_candidate"] = True
    res["seconds"] = round(time.perf_counter() - t0, 2)
    res["success"] = st["verify"]["result"] == "passed" and st["verify_restore"]["result"] == "passed"
    # RT is a diagnostic (criterion 4): recorded, never a success
    res["rt_diagnostic_only"] = (not res["success"]) and res.get("rt_diagnostic_candidate", False)
    res["type0_selected_cycle"] = res["success"] and any(
        f["kind"] == "type0" and f["subset"] for s in res["selected_fonts"] for f in s["fonts"])
    return res


def main(env: str, only: list[str]) -> None:
    m = load(MANIFEST_2B)
    out = ROOT / "outputs" / env
    out.mkdir(parents=True, exist_ok=True)
    frozen = json.loads(FREEZE.read_text())
    results = []
    for case in m["cases"]:
        cid = case["case_id"]
        if only and cid not in only:
            continue
        pdfp = case_pdf(case)
        if pdfp is None or cid not in frozen["cases"]:
            results.append({"case_id": cid, "family": case["family"], "template": case["template"],
                            "stages": {"export": {"result": "missing", "reason": "export_unavailable"}}})
            print(cid, "missing")
            continue
        data = pdfp.read_bytes()
        fz = frozen["cases"][cid]
        assert sha(data) == fz["pdf_sha256"], cid
        orp = CORPUS / cid / "oracle.json"
        assert sha(orp.read_bytes()) == fz["oracle_sha256"], cid
        oracle = json.loads(orp.read_text())
        prov = json.loads((CORPUS / cid / "provenance.json").read_text())
        r = {"family": case["family"], "template": case["template"], "type0_cid_subset_in_pdf": prov["type0_cid_subset"],
             "baseline": baseline(data, oracle)}
        try:
            r |= run_case(cid, case["template"], data, oracle, m, out / "cases")
        except Exception as e:  # an unexpected error is a failure of the candidate, recorded as such
            r |= {"case_id": cid, "stages": {"error": {"result": "failed", "reason": type(e).__name__, "detail": traceback.format_exc()[-600:]}}}
        r.pop("_artifacts", None)
        results.append(r)
        failed = [kv for kv in r["stages"].items() if kv[1].get("result") in ("failed", "missing")]
        last = failed[0] if failed else list(r["stages"].items())[-1]
        print(cid, "SUCCESS" if r.get("success") else f"fail@{last[0]}", last[1].get("reason") or last[1].get("check") or "",
              str(last[1].get("detail", ""))[:140], "BLOCKER:" + r["blocker"] if r.get("blocker") else "")
    env_info = {"python": platform.python_version(), "platform": platform.platform(), "rules": list(RULES), "official": {k: sorted(v) for k, v in OFFICIAL.items()},
                "maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)}
    (out / ("results.json" if not only else "results-partial.json")).write_text(
        json.dumps({"env": env_info, "results": results}, indent=1, ensure_ascii=False, default=str) + "\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
