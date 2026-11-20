"""Stage 2B verifier controls: the stage-2 CV01-CV30 rebuilt on 2B bases, plus CV31-CV35 (criteria 3 and 5). Each is
an alteration of a VALID output that the verifier must reject.

Bases: `L1` of the 2B corpus (frozen oracle, typographic band; Z1 + Z2 + redaction + permitted occurrence) and `L2` of
stage 2 (table borders) with a control oracle built here with the typographic band (a control base, outside the
denominator). Both must complete the cycle strictly; otherwise the CV run is reported as not executable.

    .venv/bin/python -m a08.cv2b <env>
"""

from __future__ import annotations

import io
import json
import sys
from decimal import Decimal
from pathlib import Path

import pikepdf

from . import oracle as O
from .cv import _drop_redacted, _edit, _ops, controls
from .manifest import MANIFEST_2B, ROOT, load
from .run2b import CORPUS, run_case
from .verify import Fail, verify, zones_of


def _base(cid: str, pdf: Path, orc: dict, template: str, m: dict) -> dict:
    r = run_case(cid, template, pdf.read_bytes(), orc, m, ROOT / "outputs" / "cv2b-base")
    if not r.get("success"):
        raise SystemExit(f"CV base {cid} did not complete the cycle: {list(r['stages'].items())[-1]}")
    return {"cid": cid, "orig": pdf.read_bytes(), "oracle": orc, "policy": m["policy"], **r["_artifacts"]}


def extra(b1: dict) -> dict:
    out = {}
    P = b1["protected"]
    ln = next(ln for ln in b1["binding"]["lines"] if ln["kind"] == "Z2")
    z = next(z for z in zones_of(b1["binding"]["lines"], b1["oracle"]) if z["kind"] == "Z2")
    R = z["rect"][2]
    # CV31: the plan widens its Z2 zone beyond the authorized limit to hide an alteration placed there
    bnd = json.loads(json.dumps(b1["binding"]))
    for x in bnd["lines"]:
        if x["block"] == ln["block"]:
            x["r"] = R + 40.0

    def inject(pdf):
        st, ops = _ops(pdf, ln["page"] - 1)
        cfont = next(k for k in pdf.pages[ln["page"] - 1].Resources.Font.keys() if k == "/FA08")
        st.write(pikepdf.unparse_content_stream(ops) + f"\nBT {cfont} 9 Tf {R + 4:.2f} {z['rect'][1] + 1:.2f} Td (Gil) Tj ET".encode())
    out["CV31"] = ("protected", _edit(P, inject), bnd, "el plan amplía su zona Z2 más allá del límite autorizado para ocultar una alteración")
    # CV32: suffix displaced 1 pt AND the plan's advance falsified so that a plan-driven expectation would match
    bnd2 = json.loads(json.dumps(b1["binding"]))
    for x in bnd2["lines"]:
        if x["block"] == ln["block"]:
            for s in x["sels"]:
                s["sub_nat_page"] += 1.0 / x["f"]

    def shift_suffix(pdf):
        st, ops = _ops(pdf, ln["page"] - 1)
        b0, b1_ = ln["block"]

        def draws_moved(i):  # a Tm whose next show op is a moved glyph (not a substitute)
            j = next((k for k in range(i + 1, b1_) if str(ops[k].operator) in ("Tj", "TJ")), None)
            return j is not None and str(ops[j].operator) == "Tj" and b"[" not in bytes(ops[j].operands[0])
        tms = [i for i in range(b0, b1_) if str(ops[i].operator) == "Tm" and draws_moved(i)]
        for i in tms:  # every moved glyph of the zone
            v = [float(q) for q in ops[i].operands]
            v[4] += 1.0
            ops[i] = pikepdf.ContentStreamInstruction(v, pikepdf.Operator("Tm"))
        st.write(pikepdf.unparse_content_stream(ops))
    out["CV32"] = ("protected", _edit(P, shift_suffix), bnd2, "Z2: sufijo desplazado 1 pt con el avance del plan falseado para ocultarlo")
    # CV33: a selected value copied into an /ActualText of the derivative
    val = b1["values"][next(s for x in b1["binding"]["lines"] for s in x["sels"] if s["token"])["token"]]

    def at_copy(pdf):  # an empty marked-content span (no glyphs): readers' text is unchanged, the value sits in the property list
        st, ops = _ops(pdf)
        i = next(k for k, x in enumerate(ops) if str(x.operator) in ("Tj", "TJ"))
        ops.insert(i, pikepdf.ContentStreamInstruction([pikepdf.Name("/Span"), pikepdf.Dictionary(ActualText=pikepdf.String(val))], pikepdf.Operator("BDC")))
        ops.insert(i + 1, pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")))
        st.write(pikepdf.unparse_content_stream(ops))
    out["CV33"] = ("protected", _edit(P, at_copy), None, "valor seleccionado copiado en un /ActualText (tramo vacío) del derivado")
    # CV34: an /ActualText that differs from its glyphs, added to non-selected text (no value in it)

    def at_wrong(pdf):
        st, ops = _ops(pdf)
        i = next(k for k, x in enumerate(ops) if str(x.operator) in ("Tj", "TJ"))
        ops.insert(i, pikepdf.ContentStreamInstruction([pikepdf.Name("/Span"), pikepdf.Dictionary(ActualText=pikepdf.String("Texto ajeno"))], pikepdf.Operator("BDC")))
        ops.insert(i + 2, pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")))
        st.write(pikepdf.unparse_content_stream(ops))
    out["CV34"] = ("protected", _edit(P, at_wrong), None, "/ActualText distinto de sus glifos añadido a texto no seleccionado")
    # CV35: the plan's factor falsified (derivative untouched)
    bnd3 = json.loads(json.dumps(b1["binding"]))
    for x in bnd3["lines"]:
        if x["block"] == ln["block"]:
            x["f"] = round(x["f"] + 0.05, 4)
    out["CV35"] = ("protected", P, bnd3, "Z2: factor del plan falseado (derivado sin cambios)")
    _ = (Decimal, _drop_redacted)
    return out


def main(env: str) -> None:
    m2b = load(MANIFEST_2B)
    m2 = load()
    l1 = next(c for c in m2b["cases"] if c["case_id"] == "L1")
    b1 = _base("L1", ROOT / l1["stage2_pdf"], json.loads((CORPUS / "L1" / "oracle.json").read_text()), "T1", m2b)
    l2pdf = ROOT / "corpus" / "L2" / "L2.pdf"
    orc2 = O.build("L2", l2pdf, "T2", band_mode="typographic", manifest=m2)
    b2 = _base("L2", l2pdf, orc2, "T2", m2)
    res = {}
    cases = {k: (mode, data, None, desc) for k, (mode, data, desc) in controls(b1, b2).items()} | extra(b1)
    for cid, (mode, data, bnd, desc) in sorted(cases.items()):
        if mode == "restorer":
            res[cid] = {"description": desc, "detected": True, "by": f"restorer:{data}"}
            print(cid, "detected by restorer", data)
            continue
        base = b2 if mode == "protected2" else b1
        binding = bnd or base["binding"]
        try:
            if mode.startswith("protected"):
                verify(base["orig"], data, base["oracle"], binding, base["policy"], mode="protected", selected_values=base["sel_values"])
            else:
                verify(base["orig"], data, base["oracle"], binding, base["policy"], mode="restored", restored_from=base["protected"], values=base["values"])
            res[cid] = {"description": desc, "detected": False}
            print(cid, "NOT DETECTED", desc)
        except Fail as f:
            res[cid] = {"description": desc, "detected": True, "by": f.check, "detail": f.detail[:120]}
            print(cid, "detected by", f.check, "-", desc)
        if cid == "CV33":  # the residue scan of marked-content properties on its own (criterion 3)
            from pypdf import PdfReader

            from .verify import marked_content, reps
            hit = any(any(r.casefold() in v.casefold() for r in reps(x)) for x in base["sel_values"]
                      for _, seq in marked_content(PdfReader(io.BytesIO(data))) for _, props in seq for _, v in props)
            res[cid]["residue_scan_of_marked_content_alone"] = "detects" if hit else "misses"
            print("   VK marked-content scan alone:", res[cid]["residue_scan_of_marked_content_alone"])
    out = ROOT / "outputs" / env
    out.mkdir(parents=True, exist_ok=True)
    (out / "cv.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n")
    print("detected", sum(v["detected"] for v in res.values()), "/", len(res))
    _ = io


if __name__ == "__main__":
    main(sys.argv[1])
