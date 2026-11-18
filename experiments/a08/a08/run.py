"""Runs the D-A04-04 check: baseline (engine extractor v1) and candidate (extractor v2 -> plan -> protect -> verify ->
restore -> verify) on the frozen corpus and on the controls. Writes outputs/<env>/.

    .venv/bin/python -m a08.run <env> [case ...]
"""

from __future__ import annotations

import hashlib
import json
import platform
import resource
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from cawbase_content_engine.documents.common import Limits
from cawbase_content_engine.documents.common import Reject as V1Reject
from cawbase_content_engine.documents.pdf import extract as v1_extract
from cawbase_content_engine.jobs.setup import JobsLimits
from cawbase_content_engine.protection import markers as A06

from . import gate
from .manifest import ROOT, load, selections
from .pdfmodel import Reject, canonical
from .transform import PlanFail, plan, protect, restore
from .verify import Fail, verify

RULES_FILE = ROOT / "outputs" / "rules.json"


VARIANT: set[str] = set()


def case_paths(cid: str) -> tuple[Path, Path]:
    for base in ("corpus", "corpus-private"):
        p = ROOT / base / cid / f"{cid}.pdf"
        if p.exists():
            return p, ROOT / "corpus" / cid / ("oracle-TB.json" if "TB" in VARIANT else "oracle.json")
    raise FileNotFoundError(cid)


def baseline(data: bytes, oracle: dict) -> dict:
    lim = Limits.from_params(asdict(JobsLimits()))
    try:
        out = v1_extract(data, lim)
    except V1Reject as r:
        return {"open_gate": "rejected", "reason": r.reason}
    texts = [u["text"] for u in out["units"]]
    loc = {}
    for o in oracle["occurrences"]:
        if o["kind"] != "S":
            continue
        t = texts[o["page"] - 1] if o["page"] <= len(texts) else ""
        loc[o["id"]] = t.count(o["value"])
    return {"open_gate": "accepted", "units": len(texts), "value_occurrences_in_unit_text": loc,
            "declared_not_covered": out.get("declared_not_covered")}


def run_case(cid: str, template: str, data: bytes, oracle: dict, m: dict, outdir: Path, rules: tuple[str, ...], variant: str = "",
             sels_override: list | None = None, hooks: dict | None = None) -> dict:
    res: dict = {"case_id": cid + variant, "stages": {}}
    st = res["stages"]
    t0 = time.perf_counter()
    if oracle.get("status") != "ok":
        bad = [o for o in oracle["occurrences"] if o["status"] != "ok"]
        st["oracle"] = {"result": "failed", "reason": "oracle_unavailable", "detail": [(o["id"], o.get("reason")) for o in bad]}
        return res
    st["oracle"] = {"result": "passed"}
    try:
        ex = gate.extract(data, rules)
        st["open"] = st["gate"] = st["extract"] = {"result": "passed"}
        res["inventory"] = {k: v for k, v in ex.inventory.items() if k != "rules_used"} | {"rules_used": ex.inventory["rules_used"]}
        smap_a = canonical(ex.smap)
        smap_b = canonical(gate.extract(data, rules).smap)
        res["source_map_deterministic"] = smap_a == smap_b
        res["source_map_sha256"] = hashlib.sha256(smap_a).hexdigest()
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
    try:
        pr = protect(data, ex, plans)
        st["remove"] = st["substitute"] = st["write"] = {"result": "passed"}
    except (PlanFail, Reject) as e:
        st["substitute"] = {"result": "failed", "reason": getattr(e, "reason", "error"), "detail": getattr(e, "detail", "")}
        return res
    binding = pr.binding
    res["removed_classes"] = pr.removed_classes
    # occurrences of the emitted markers in the derivative (extractor v2): what the binding authorizes
    try:
        exd = gate.extract(pr.data, rules)
        st["reopen"] = {"result": "passed"}
    except Reject as r:
        st["reopen"] = {"result": "failed", "reason": r.reason, "detail": r.detail}
        return res
    binding["occurrences"] = [{"page": pm.index + 1, "start": a, "end": b, "token": A06.token_of(mk)}
                              for pm in exd.pages for a, b, mk in A06.scan(pm.text).strict]
    values = {A06.token_of(A06.marker_of(t)): frag for frag, t in tokens.items()}
    sel_values = sorted({s["value"] for s in sels} | {se.value for lp in plans for se in lp.sels})
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{cid}{variant}.protected.pdf").write_bytes(pr.data)
    (outdir / f"{cid}{variant}.binding.json").write_text(json.dumps(binding, indent=1, ensure_ascii=False) + "\n")
    try:
        rep = verify(data, pr.data, oracle, binding, m["policy"], mode="protected", selected_values=sel_values)
        st["verify"] = {"result": "passed", "checks": rep}
    except Fail as f:
        st["verify"] = {"result": "failed", "check": f.check, "detail": f.detail, "checks": f.report}
        if not (f.check == "VR" and f.report):
            return res
        res["rt_candidate"] = True  # only the PDFium raster check failed: continue to measure restoration (RT evaluation)
    res["_artifacts"] = {"protected": pr.data, "binding": binding, "values": values, "sel_values": sel_values}
    try:
        rest = restore(pr.data, binding, values)
        st["restore"] = {"result": "passed"}
    except (PlanFail, Reject) as e:
        st["restore"] = {"result": "failed", "reason": getattr(e, "reason", "error"), "detail": getattr(e, "detail", "")}
        return res
    (outdir / f"{cid}{variant}.restored.pdf").write_bytes(rest)
    res["_artifacts"]["restored"] = rest
    try:
        rep2 = verify(data, rest, oracle, binding, m["policy"], mode="restored", restored_from=pr.data, values=values)
        st["verify_restore"] = {"result": "passed", "checks": rep2}
    except Fail as f:
        st["verify_restore"] = {"result": "failed", "check": f.check, "detail": f.detail, "checks": f.report}
        if not (f.check == "VR" and f.report):
            return res
        res["rt_candidate"] = True
    res["seconds"] = round(time.perf_counter() - t0, 2)
    res["success"] = st["verify"]["result"] == "passed" and st["verify_restore"]["result"] == "passed"
    res["success_rt"] = res["success"] or (res.get("rt_candidate", False) and all(
        v.get("all_on_unchanged_glyph_ink") for k in ("verify", "verify_restore") if st[k]["result"] == "failed"
        for v in st[k]["checks"]["VR"]["pdfium_outside_mask"]))
    return res


def main(env: str, only: list[str]) -> None:
    m = load()
    out = ROOT / "outputs" / env
    out.mkdir(parents=True, exist_ok=True)
    rules = tuple(json.loads(RULES_FILE.read_text())["surviving"]) if RULES_FILE.exists() else gate.ALL_RULES
    results = []
    for case in m["cases"]:
        cid = case["case_id"]
        if only and cid not in only:
            continue
        try:
            pdfp, orp = case_paths(cid)
        except FileNotFoundError:
            results.append({"case_id": cid, "family": case["family"], "stages": {"export": {"result": "missing", "reason": "export_unavailable"}}})
            print(cid, "missing")
            continue
        data = pdfp.read_bytes()
        oracle = json.loads(orp.read_text())
        frozen = json.loads((ROOT / "corpus" / "FREEZE.json").read_text())["cases"][cid]
        assert hashlib.sha256(data).hexdigest() == frozen["pdf_sha256"], cid
        fz = json.loads((ROOT / "corpus" / "FREEZE.json").read_text())
        want = fz["informative"][cid]["oracle_TB_sha256"] if "TB" in VARIANT else frozen["oracle_sha256"]
        assert hashlib.sha256(orp.read_bytes()).hexdigest() == want, cid
        prov = json.loads((ROOT / "corpus" / cid / "provenance.json").read_text())
        r = {"family": case["family"], "type0_cid_subset": prov["type0_cid_subset"], "baseline": baseline(data, oracle)}
        try:
            r |= run_case(cid, case["template"], data, oracle, m, out / "cases", rules)
        except Exception as e:  # an unexpected error is a failure of the candidate, recorded as such
            r |= {"case_id": cid, "stages": {"error": {"result": "failed", "reason": type(e).__name__, "detail": traceback.format_exc()[-600:]}}}
        r.pop("_artifacts", None)
        results.append(r)
        last = [k for k, v in r["stages"].items()][-1]
        print(cid, "SUCCESS" if r.get("success") else f"fail@{last}", r["stages"][last].get("reason") or r["stages"][last].get("check") or "", r["stages"][last].get("detail", "")[:140] if isinstance(r["stages"][last].get("detail"), str) else "")
    env_info = {"python": platform.python_version(), "platform": platform.platform(), "rules": list(rules),
                "maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)}
    (out / ("results.json" if not only else "results-partial.json")).write_text(json.dumps({"env": env_info, "results": results}, indent=1, ensure_ascii=False, default=str) + "\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0].startswith("--variant="):
        VARIANT.update(args[0].split("=", 1)[1].split("+"))
        gate.VARIANT.update(VARIANT)
        from . import transform as _tr
        _tr.VARIANT.update(VARIANT)
        args = args[1:]
    main(args[0], args[1:])
