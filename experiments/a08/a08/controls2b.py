"""Stage 2B controls (outside the denominator; docs/a08-stage2b-protocol.md §6). Negative controls must be REJECTED
for the expected reason; an accepted negative is a blocker. Positive structural controls (CS) are reported.

    .venv/bin/python -m a08.controls2b <env>

CA01-07  adversarial objects that represent text: rejected by the frozen rule set (criterion 6)
CR01-23  stage-2 safe rejections, re-run under the 2B criteria
CR27-29  /ActualText (criterion 3): differs from its glyphs; contains the selection; partially overlaps it
CR30-31  restoration: not the exact derivative; binding altered (authenticated binding, D-A08-05)
CS01-13  stage-2 structural positives (CS12: two pseudonymizations on one line; CS13: pseudonymization then redaction)
CS14     redaction then pseudonymization on one line (criterion 2)
CS15     /ActualText equal to its glyphs, outside the selection and inside the moved Z2 region (criterion 3)
CS16     pseudonymization then a SHORT redaction on one Z2 line without room: the RZ excess does not fit, restoration
         must fail closed (restoration_layout_unavailable) (criterion 2)
CS17     the same line with room to its right (a longer line on the page): the RZ excess fits (criterion 2)
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from . import gate
from . import oracle as O
from .controls import LINE, TEXT, VALS, _items, base, ca_controls, cr_controls, cs_controls
from .manifest import ROOT, load
from .pdfmodel import Reject
from .run2b import RULES, official, run_case
from .synth import Builder
from .transform import PlanFail, restore


def cr2b() -> dict:
    out = {}
    # CR27: /ActualText that differs from its glyphs (outside the selection)
    b = Builder()
    b.font_simple("/F1", TEXT)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    c = b.fonts["/F1"].codes
    head, tail = "Árbol: Ana Gil Soler, documento 00000000-X, ", "pago aprobado."
    b.raw(f"BT /F1 11 Tf 72 700 Td <{c(head).hex()}> Tj /Span <</ActualText (pago aprobada)>> BDC <{c(tail).hex()}> Tj EMC ET")
    out["CR27"] = (b.save(), "alternate_text_duplication", VALS)
    # CR28: /ActualText equal to its glyphs but containing the whole selection
    b = Builder()
    b.font_simple("/F1", TEXT)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    c = b.fonts["/F1"].codes
    b.raw(f"BT /F1 11 Tf 72 700 Td <{c('Árbol: ').hex()}> Tj /Span <</ActualText (Ana Gil Soler)>> BDC <{c('Ana Gil Soler').hex()}> Tj EMC "
          f"<{c(', documento 00000000-X, pago aprobado.').hex()}> Tj ET")
    out["CR28"] = (b.save(), "selection_not_transformable", VALS)
    # CR29: /ActualText equal to its glyphs, partially overlapping the selection
    b = Builder()
    b.font_simple("/F1", TEXT)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    c = b.fonts["/F1"].codes
    b.raw(f"BT /F1 11 Tf 72 700 Td <{c('Árbol: Ana ').hex()}> Tj /Span <</ActualText (Gil Soler, documento)>> BDC "
          f"<{c('Gil Soler, documento').hex()}> Tj EMC <{c(' 00000000-X, pago aprobado.').hex()}> Tj ET")
    out["CR29"] = (b.save(), "selection_not_transformable", VALS)
    return out


def cs2b() -> dict:
    out = {}
    b = Builder()
    line = "Árbol: 00000000-X Ana Gil Soler pago aprobado."
    b.font_simple("/F1", TEXT + line)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.text("/F1", 11, 72, 700, line)
    out["CS14"] = (b.save(), [("S1", "00000000-X", "government_id", "Árbol:", "Ana"), ("S2", "Ana Gil Soler", "personal_data", "", "pago")])
    b = Builder()
    b.font_simple("/F1", TEXT + "fi")
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    c = b.fonts["/F1"].codes
    b.raw(f"BT /F1 11 Tf 72 700 Td <{c('Árbol: Ana Gil Soler, documento 00000000-X, pago ').hex()}> Tj "
          f"/Span <</ActualText (fi)>> BDC <{c('fi').hex()}> Tj EMC <{c('nal aprobado.').hex()}> Tj ET")
    out["CS15"] = (b.save(), [("S1", "Ana Gil Soler", "personal_data", "Árbol:", ", documento"), ("S2", "00000000-X", "government_id", "documento", ", pago")])
    b = Builder()
    line = "Árbol: Ana Gil Soler cobró 350 € al contado."
    b.font_simple("/F1", TEXT + line + "€")
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.text("/F1", 11, 72, 700, line)
    out["CS16"] = (b.save(), [("S1", "Ana Gil Soler", "personal_data", "Árbol:", "cobró"), ("S2", "350 €", "salary_compensation", "cobró", "al contado")])
    b = Builder()
    longer = "Página de control sintético, con una línea más larga que la del valor."
    b.font_simple("/F1", TEXT + line + longer + "€")
    b.text("/F1", 11, 72, 760, longer)
    b.text("/F1", 11, 72, 700, line)
    out["CS17"] = (b.save(), [("S1", "Ana Gil Soler", "personal_data", "Árbol:", "cobró"), ("S2", "350 €", "salary_compensation", "cobró", "al contado")])
    return out


def run_one(cid: str, data: bytes, vals, env_dir: Path) -> dict:
    official()
    m = load()
    tmp = env_dir / "controls" / f"{cid}.pdf"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    items = _items(vals)
    try:
        orc = O.build(cid, tmp, "", custom=(items, []), band_mode="typographic")
    except Exception as e:
        orc = {"status": "oracle_unavailable", "occurrences": [], "error": type(e).__name__}
    acts = m["policy"]["actions"]
    sl = [{**it, "action": acts.get(it["type_id"], "pseudonymize"), "ordinal_on_page": 1} for it in items]
    if orc.get("status") != "ok":
        try:
            gate.extract(data, RULES)
            return {"stages": {"oracle": {"result": "failed", "reason": "oracle_unavailable"}}, "gate_on_oracle_failure": "accepted"}
        except Reject as r:
            return {"stages": {"gate": {"result": "failed", "reason": r.reason, "detail": r.detail}}}
        except Exception as e:
            return {"stages": {"gate": {"result": "failed", "reason": type(e).__name__}}}
    return run_case(cid, "", data, orc, m, env_dir / "controls", sels_override=sl)


def outcome(r: dict) -> str:
    if r.get("success"):
        return "cycle"
    failed = [kv for kv in r["stages"].items() if kv[1].get("result") in ("failed", "missing")]
    last = failed[0] if failed else list(r["stages"].items())[-1]
    return f"{last[0]}:{last[1].get('reason') or last[1].get('check')}"


def restoration_controls(env_dir: Path) -> dict:
    """CR30/CR31 on a valid cycle (base control line): the restorer must refuse before reading anything else."""
    r = run_one("CR30base", base().save(), VALS, env_dir)
    if "restored" not in r.get("_artifacts", {}):  # a protected + restored pair is enough (RT diagnostic allowed here)
        return {"CR30": {"expected": "derivative_mismatch", "outcome": "base_failed:" + outcome(r), "as_expected": False, "accepted_indebidamente": False},
                "CR31": {"expected": "binding_invalid", "outcome": "base_failed:" + outcome(r), "as_expected": False, "accepted_indebidamente": False}}
    a = r["_artifacts"]
    res = {}
    tampered = bytearray(a["protected"])
    tampered[-2] ^= 0x20  # one byte of the trailer whitespace region: a different file, same content
    bnd = json.loads(json.dumps(a["binding"]))
    bnd["lines"][0]["sels"][0]["x_orig"] += 3.0
    for cid, (deriv, binding, exp) in {"CR30": (bytes(tampered), a["binding"], "derivative_mismatch"),
                                       "CR31": (a["protected"], bnd, "binding_invalid")}.items():
        try:
            restore(deriv, binding, a["values"], key=a["key"])
            oc = "cycle"
        except (PlanFail, Reject) as e:
            oc = f"restore:{e.reason}"
        res[cid] = {"expected": exp, "outcome": oc, "as_expected": exp in oc, "accepted_indebidamente": oc == "cycle"}
    return res


def main(env: str) -> None:
    out = ROOT / "outputs" / env
    out.mkdir(parents=True, exist_ok=True)
    res: dict = {"rules": list(RULES), "CA": {}, "CR": {}, "CS": {}}
    for cid, (data, desc, _box) in ca_controls().items():
        try:
            gate.extract(data, RULES)
            oc = "accepted"
        except Reject as r:
            oc = f"gate:{r.reason}"
        res["CA"][cid] = {"description": desc, "expected": "uncovered_content", "outcome": oc, "as_expected": "uncovered_content" in oc,
                          "accepted_indebidamente": oc == "accepted"}
        print(cid, oc)
    cr = {k: (d, e, VALS) for k, (d, e) in cr_controls().items()} | cr2b()
    for cid, (data, expected, vals) in sorted(cr.items()):
        try:
            r = run_one(cid, data, vals, out)
        except Exception:
            r = {"stages": {"error": {"result": "failed", "reason": traceback.format_exc()[-300:]}}}
        oc = outcome(r)
        ok = any(e in oc for e in expected.split("|"))
        res["CR"][cid] = {"expected": expected, "outcome": oc, "as_expected": ok, "accepted_indebidamente": oc == "cycle"}
        print(cid, oc, "OK" if ok else "UNEXPECTED")
    for cid, v in restoration_controls(out).items():
        res["CR"][cid] = v
        print(cid, v["outcome"], "OK" if v["as_expected"] else "UNEXPECTED")
    for cid, (data, vals) in sorted((cs_controls() | cs2b()).items()):
        try:
            r = run_one(cid, data, vals, out)
        except Exception:
            r = {"stages": {"error": {"result": "failed", "reason": traceback.format_exc()[-300:]}}}
        res["CS"][cid] = {"outcome": outcome(r), "rt_diagnostic_only": r.get("rt_diagnostic_only", False), "plan": r.get("plan"),
                          "blocker": r.get("blocker"),
                          "stages": {k: {kk: vv for kk, vv in v.items() if kk != "checks"} for k, v in r["stages"].items()},
                          "geometry": ((r["stages"].get("verify") or {}).get("checks") or {}).get("geometry")}
        print(cid, outcome(r), "(RT diagnostic)" if r.get("rt_diagnostic_only") else "", "BLOCKER" if r.get("blocker") else "")
    res["summary"] = {"negatives": sum(len(res[k]) for k in ("CA", "CR")),
                      "as_expected": sum(v["as_expected"] for k in ("CA", "CR") for v in res[k].values()),
                      "accepted_indebidamente": sorted(c for k in ("CA", "CR") for c, v in res[k].items() if v["accepted_indebidamente"]),
                      "cs_blockers": sorted(c for c, v in res["CS"].items() if v["blocker"])}
    (out / "controls.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str) + "\n")
    print("summary", res["summary"])


if __name__ == "__main__":
    main(sys.argv[1])
    _ = LINE
