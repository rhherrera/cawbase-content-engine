"""Controls (not counted in the utility rate): CA (adversarial, decide which decorative/path rules survive), CR (safe
rejections), CS (structural variants absent from the native exports) and CV (must make the verifier FAIL).

    .venv/bin/python -m a08.controls <env> [--variant=TB]
"""

from __future__ import annotations

import io
import json
import sys
import traceback
from pathlib import Path

import pikepdf
from PIL import Image, ImageDraw

from . import gate
from . import oracle as O
from .manifest import ROOT, load
from .pdfmodel import Reject
from .run import run_case
from .synth import Builder
from .transform import PlanFail, restore
from .verify import Fail, verify

LINE = "Árbol: Ana Gil Soler, documento 00000000-X, pago aprobado."
VALS = [("S1", "Ana Gil Soler", "personal_data", "Árbol:", ", documento"), ("S2", "00000000-X", "government_id", "documento", ", pago")]
TEXT = LINE + "Página de control sintético 0123456789 [](){}:"


def sels(items=VALS, page=1):
    m = load()
    acts = m["policy"]["actions"]
    return [{"id": i, "value": v, "type_id": t, "action": acts.get(t, "pseudonymize"), "page": page, "left_context": l, "right_context": r,
             "ordinal_on_page": 1, "value_id": i} for i, v, t, l, r in items]


def base(b: Builder | None = None, font="simple", page_text=True) -> Builder:
    b = b or Builder()
    (b.font_simple if font == "simple" else b.font_type0)("/F1", TEXT)
    if page_text:
        b.text("/F1", 11, 72, 760, "Página de control sintético.")
        b.text("/F1", 11, 72, 700, LINE)
    return b


# ---------------------------------------------------------------- CA: adversarial objects that REPRESENT text
def ca_controls() -> dict:
    def img(b: Builder, pil: Image.Image, x, y, w, h, name="/Im1"):
        raw = pil.convert("L").tobytes()
        st = b.pdf.make_stream(raw, Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image, Width=pil.size[0], Height=pil.size[1],
                               ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8)
        b.pages[0].obj.Resources.XObject[name] = st
        b.raw(f"q {w} 0 0 {h} {x} {y} cm {name} Do Q")

    out = {}
    # CA01: 16x16 image with two legible digits, placed at 6x6 pt
    b = base()
    im = Image.new("L", (16, 16), 255)
    d = ImageDraw.Draw(im)
    for seg in [((1, 2), (6, 2)), ((6, 2), (6, 13)), ((9, 2), (9, 8)), ((9, 8), (14, 8)), ((14, 2), (14, 13))]:  # "7" "4"
        d.line(seg, fill=0, width=2)
    img(b, im, 300, 640, 6, 6)
    out["CA01"] = (b.save(), "imagen diminuta con «74»", [300, 640, 306, 646])
    # CA02: letters GIL with straight segments (<= 8 per subpath), stroked
    b = base()
    b.raw("q 1 w 0 G 300 620 m 280 620 l 280 600 l 300 600 l 300 610 l 292 610 l S 310 620 m 310 600 l S 320 620 m 320 600 l 334 600 l S Q")
    out["CA02"] = (b.save(), "letras «GIL» con segmentos rectos", [280, 600, 334, 620])
    # CA03: letters EF with long thin rules (not a grid)
    b = base()
    for x0, y0, w, h in [(100, 500, 1.5, 120), (100, 620, 250, 1.5), (100, 560, 250, 1.5), (100, 500, 250, 1.5),
                         (400, 500, 1.5, 120), (400, 620, 250, 1.5), (400, 560, 250, 1.5)]:
        b.raw(f"q 0 g {x0} {y0} {w} {h} re f Q")
    out["CA03"] = (b.save(), "letras «EF» con filetes largos", [100, 500, 650, 622])
    # CA04: pixel letters from many uniform 1x1 images (2x2 pt each)
    b = base()
    one = Image.new("L", (1, 1), 0)
    pix = ["X...X", "X...X", "XXXXX", "X...X", "X...X"]  # an "H"
    k = 0
    for r, row in enumerate(pix):
        for cidx, ch in enumerate(row):
            if ch == "X":
                k += 1
                img(b, one, 300 + cidx * 2, 600 - r * 2, 2, 2, name=f"/P{k}")
    out["CA04"] = (b.save(), "letra «H» de imágenes uniformes", [300, 590, 312, 602])
    # CA05: pixel letters from filled rectangles
    b = base()
    for r, row in enumerate(pix):
        for cidx, ch in enumerate(row):
            if ch == "X":
                b.raw(f"q 0 g {300 + cidx * 2} {600 - r * 2} 2 2 re f Q")
    out["CA05"] = (b.save(), "letra «H» de rectángulos rellenos", [300, 590, 312, 602])
    # CA06: a full table grid plus extra cell borders drawing the digit "1" inside a cell
    b = base()
    for y in (600, 580, 560):
        b.raw(f"q 0.5 w 0 G 72 {y} m 372 {y} l S Q")
    for x in (72, 222, 372):
        b.raw(f"q 0.5 w 0 G {x} 560 m {x} 600 l S Q")
    b.raw("q 0.5 w 0 G 150 562 m 150 578 l S 146 574 m 150 578 l S Q")
    out["CA06"] = (b.save(), "cifra «1» con bordes adicionales dentro de una tabla", [146, 562, 151, 579])
    # CA07: letters with small curves (<= 6 pt): an "S" and an "O"
    b = base()
    b.raw("q 0.4 w 0 G 304 598 m 300.5 599 300.5 596.5 302 596 c 303.5 595.5 304.5 594 301 593.5 c S Q")
    b.raw("q 0.4 w 0 G 308 596 m 308 598.5 312 598.5 312 596 c 312 593.5 308 593.5 308 596 c S Q")
    out["CA07"] = (b.save(), "letras «SO» con curvas pequeñas (cada una <= 6 pt)", [299, 592, 314, 601])
    return out


def run_ca() -> dict:
    res = {}
    refuted: dict[str, list] = {}
    for cid, (data, desc, box) in ca_controls().items():
        try:
            ex = gate.extract(data, gate.ALL_RULES)
            rules = sorted({p.detail.get("rule") for pm in ex.pages for p in pm.painted
                            if p.bbox[0] >= box[0] - 2 and p.bbox[2] <= box[2] + 2 and p.bbox[1] >= box[1] - 2 and p.bbox[3] <= box[3] + 2})
            res[cid] = {"description": desc, "result": "admitted (rule failure)", "admitted_by": rules}
            for r in rules:
                refuted.setdefault(r, []).append(cid)
        except Reject as r:
            res[cid] = {"description": desc, "result": "rejected", "reason": r.reason}
    surviving = [r for r in gate.ALL_RULES if r not in refuted]
    return {"controls": res, "refuted": refuted, "surviving": surviving}


# ---------------------------------------------------------------- CR: safe rejections (expected outcome fixed)
def cr_controls() -> dict:
    out = {}

    def simple():
        return base()

    b = Builder()
    b.font_type0("/F1", TEXT, encoding="/Identity-V")
    b.text("/F1", 11, 72, 700, LINE)
    out["CR01"] = (b.save(), "font_mapping_insufficient|unsupported_structure")
    b = Builder()
    b.font_simple("/F1", TEXT)
    t3 = b.pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type3, FontBBox=[0, 0, 1000, 1000],
                             FontMatrix=[0.001, 0, 0, 0.001, 0, 0], CharProcs=pikepdf.Dictionary(), Encoding=pikepdf.Dictionary(Differences=[65, pikepdf.Name.A]),
                             FirstChar=65, LastChar=65, Widths=[500]))
    b.pages[0].obj.Resources.Font["/T3"] = t3
    b.raw("BT /T3 11 Tf 72 700 Td (A) Tj ET")
    out["CR02"] = (b.save(), "font_mapping_insufficient")
    b = Builder()
    b.font_type0("/F1", TEXT, tounicode=False)
    b.text("/F1", 11, 72, 700, LINE)
    out["CR03"] = (b.save(), "font_mapping_insufficient")
    b = Builder()
    b.font_type0("/F1", TEXT, encoding="/UniGB-UCS2-H")
    b.text("/F1", 11, 72, 700, LINE)
    out["CR04"] = (b.save(), "font_mapping_insufficient")
    b = Builder()
    scan = Image.new("L", (600, 800), 255)
    ImageDraw.Draw(scan).text((50, 100), LINE, fill=0)
    st = b.pdf.make_stream(scan.tobytes(), Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image, Width=600, Height=800,
                           ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8)
    b.pages[0].obj.Resources.XObject["/Im1"] = st
    b.raw("q 595 0 0 842 0 0 cm /Im1 Do Q")
    out["CR05"] = (b.save(), "scanned_without_text")
    b.font_simple("/F1", TEXT)
    b.text("/F1", 11, 72, 700, LINE, pre="3 Tr")
    out["CR06"] = (b.save(), "hidden_text|uncovered_content")
    b = base()
    im = Image.new("L", (200, 40), 255)
    ImageDraw.Draw(im).text((5, 10), "Ana Gil Soler", fill=0)
    st = b.pdf.make_stream(im.tobytes(), Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image, Width=200, Height=40,
                           ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8)
    b.pages[0].obj.Resources.XObject["/Im1"] = st
    b.raw("q 150 0 0 30 72 600 cm /Im1 Do Q")
    out["CR07"] = (b.save(), "uncovered_content")
    b = base()
    b.raw("q 0 g 100 500 m 100 560 160 560 160 500 c 160 440 100 440 100 500 c f 200 440 m 200 560 l 230 560 l 260 470 260 440 230 440 c f Q")
    out["CR08"] = (b.save(), "uncovered_content")
    b = base()
    out["CR09"] = (b.save(encryption=pikepdf.Encryption(user="", owner="o", R=4)), "encrypted")
    b = base()
    sig = b.pdf.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Sig, T=pikepdf.String("Sig1"), V=pikepdf.Dictionary(Type=pikepdf.Name.Sig, ByteRange=[0, 0, 0, 0])))
    b.pdf.Root.AcroForm = pikepdf.Dictionary(Fields=[sig], SigFlags=3)
    out["CR10"] = (b.save(), "signed")
    data = base().save()
    xref_pos = data.rfind(b"startxref")
    prev = int(data[xref_pos + 9:].split()[0])
    tail = b"\n99 0 obj\n(incremental)\nendobj\n"
    off = len(data) + 1
    upd = data + tail + f"xref\n99 1\n{off:010d} 00000 n \ntrailer\n<< /Size 100 /Root 1 0 R /Prev {prev} >>\nstartxref\n{len(data) + len(tail)}\n%%EOF\n".encode()
    out["CR11"] = (upd, "incremental_update|parser_repair")
    d = bytearray(base().save())
    k = d.rfind(b"startxref")
    d[k + 10:k + 14] = b"1234"
    out["CR12"] = (bytes(d), "parser_repair")
    b = base()
    fld = b.pdf.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Tx, T=pikepdf.String("f1"), V=pikepdf.String("Ana Gil Soler"), Type=pikepdf.Name.Annot,
                              Subtype=pikepdf.Name.Widget, Rect=[72, 600, 200, 620]))
    b.pages[0].obj.Annots = pikepdf.Array([fld])
    b.pdf.Root.AcroForm = pikepdf.Dictionary(Fields=[fld])
    out["CR13"] = (b.save(), "forms_or_layers")
    b = base()
    b.pdf.Root.OCProperties = pikepdf.Dictionary(OCGs=[], D=pikepdf.Dictionary())
    out["CR14"] = (b.save(), "forms_or_layers")
    b = base()
    ef = b.pdf.make_stream(b"Ana Gil Soler", Type=pikepdf.Name.EmbeddedFile)
    b.pdf.Root.Names = pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(Names=[pikepdf.String("a.txt"), pikepdf.Dictionary(Type=pikepdf.Name.Filespec, F=pikepdf.String("a.txt"), EF=pikepdf.Dictionary(F=ef))]))
    out["CR15"] = (b.save(), "embedded_content")
    b = base()
    b.pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)"))
    out["CR16"] = (b.save(), "executable_action")
    b = base()
    b.pages[0].obj.Annots = pikepdf.Array([b.pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.FreeText, Rect=[72, 600, 200, 620], Contents=pikepdf.String("nota")))])
    out["CR17"] = (b.save(), "unsupported_structure")
    b = Builder()
    b.font_simple("/F1", TEXT)
    b.raw(f"/Span <</ActualText (Ana Gil Soler)>> BDC BT /F1 11 Tf 72 700 Td <{b.fonts['/F1'].codes(LINE).hex()}> Tj ET EMC")
    out["CR18"] = (b.save(), "alternate_text_duplication")
    b = Builder()
    b.font_simple("/F1", TEXT)
    body = f"BT /F1 11 Tf 72 700 Td <{b.fonts['/F1'].codes(LINE).hex()}>"
    b.raw(body + "\nTj ET")
    out["CR19"] = (b.save(contents_split=[len(body)]), "unsupported_structure|parser_repair")
    b = base(page_text=False)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    form = b.pdf.make_stream(f"BT /F1 11 Tf 0 0 Td <{b.fonts['/F1'].codes(LINE).hex()}> Tj ET".encode(), Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form,
                             BBox=[0, -5, 500, 15], Resources=pikepdf.Dictionary(Font=b.pages[0].obj.Resources.Font))
    b.pages[0].obj.Resources.XObject["/Fm1"] = form
    b.raw("q 1 0 0 1 72 700 cm /Fm1 Do Q")
    b.pages[0].obj.Annots = pikepdf.Array([b.pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=[72, 690, 300, 715],
                                          AP=pikepdf.Dictionary(N=form), A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String("https://example.test/"))))])
    out["CR20"] = (b.save(), "shared_object_partial|selection_not_transformable")
    b = base(page_text=False)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.text("/F1", 11, 700, 700, LINE)
    out["CR21"] = (b.save(), "hidden_text")
    b = base()
    b.raw("q 0 g 105 696 70 12 re f Q")
    out["CR22"] = (b.save(), "hidden_text")
    b = Builder()
    b.font_simple("/F1", TEXT + "-")
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.text("/F1", 11, 72, 700, "Árbol: Ana Gil So-")
    b.text("/F1", 11, 72, 686, "ler, documento 00000000-X, pago aprobado.")
    out["CR23"] = (b.save(), "selection_crosses_boundary|oracle_unavailable")
    return out


# ---------------------------------------------------------------- CS: structural positives
def cs_controls() -> dict:
    out = {}
    b = Builder()
    b.font_type0("/F1", TEXT)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.text("/F1", 11, 72, 700, LINE)
    out["CS01"] = (b.save(), VALS)
    b = base()
    out["CS03"] = (b.save(object_stream_mode=pikepdf.ObjectStreamMode.generate), VALS)
    b = Builder()
    b.font_simple("/F1", TEXT)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    c = b.fonts["/F1"].codes
    s1 = "BT /F1 11 Tf 72 700 Td <" + c("Árbol: Ana Gil").hex() + "> Tj"
    s2 = " <" + c(" Soler, documento 00000000-X, pago aprobado.").hex() + "> Tj ET"
    b.raw(s1 + s2)
    head = len("\n".join(b.content[0][:-1])) + 1 + len(s1)
    out["CS04"] = (b.save(contents_split=[head]), VALS)
    b = Builder()
    b.font_simple("/F1", TEXT)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.raw(f"BT /F1 11 Tf 14 TL 0.3 Tc 1.2 Tw 95 Tz 0.5 Ts 72 714 Td T* <{b.fonts['/F1'].codes(LINE).hex()}> Tj ET")
    out["CS05"] = (b.save(), VALS)
    b = Builder()
    b.font_type0("/F1", TEXT)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.raw(f"BT /F1 11 Tf 3 Tw -0.2 Tc 72 700 Td <{b.fonts['/F1'].codes(LINE).hex()}> Tj ET")
    out["CS06"] = (b.save(), VALS)
    b = Builder(pages=2)
    b.font_simple("/F1", TEXT)
    form = b.pdf.make_indirect(b.pdf.make_stream(f"BT /F1 11 Tf 0 0 Td <{b.fonts['/F1'].codes(LINE).hex()}> Tj ET".encode(), Type=pikepdf.Name.XObject,
                                                 Subtype=pikepdf.Name.Form, BBox=[0, -5, 500, 15], Resources=pikepdf.Dictionary(Font=b.pages[0].obj.Resources.Font)))
    for k in (0, 1):
        b.pages[k].obj.Resources.XObject["/Fm1"] = form
        b.text("/F1", 11, 72, 760, "Página de control sintético.", page=k)
        b.raw("q 1 0 0 1 72 700 cm /Fm1 Do Q", page=k)
    out["CS07a"] = (b.save(), VALS + [("S3", "Ana Gil Soler", "personal_data", "Árbol:", ", documento", 2), ("S4", "00000000-X", "government_id", "documento", ", pago", 2)])
    out["CS07b"] = (b.save(), VALS)
    b = Builder(rotate=270)
    b.font_simple("/F1", TEXT)
    b.text("/F1", 11, 0, 0, "Página de control sintético.", tm="0 -1 1 0 100 760")
    b.text("/F1", 11, 0, 0, LINE, tm="0 -1 1 0 140 760")
    out["CS08"] = (b.save(), VALS)
    b = Builder()
    b.font_type0("/F1", TEXT)
    b.raw("1 0 0 -1 0 841.89 cm")
    b.text("/F1", 11, 0, 0, "Página de control sintético.", tm="1 0 0 -1 72 81.89")
    b.text("/F1", 11, 0, 0, LINE, tm="1 0 0 -1 72 141.89")
    out["CS09"] = (b.save(), VALS)
    b = base()
    out["CS10"] = (b.save(linearize=True), VALS)
    b = Builder()
    b.font_simple("/F1", TEXT)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.raw(f"BT 0.3 w 0 0 1 RG 2 Tr /F1 11 Tf 72 700 Td <{b.fonts['/F1'].codes(LINE).hex()}> Tj ET")
    out["CS11"] = (b.save(), VALS)
    b = Builder()
    line2 = "Árbol: Ana Gil Soler y Pepa Ruiz Lago, aprobado."
    b.font_simple("/F1", TEXT + line2)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.text("/F1", 11, 72, 700, line2)
    out["CS12"] = (b.save(), [("S1", "Ana Gil Soler", "personal_data", "Árbol:", "y"), ("S2", "Pepa Ruiz Lago", "personal_data", "y", ", aprobado")])
    b = Builder()
    line3 = "Árbol: Ana Gil Soler 00000000-X pago aprobado."
    b.font_simple("/F1", TEXT + line3)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.text("/F1", 11, 72, 700, line3)
    out["CS13"] = (b.save(), [("S1", "Ana Gil Soler", "personal_data", "Árbol:", ""), ("S2", "00000000-X", "government_id", "", "pago")])
    return out


def _items(vals):
    out = []
    for v in vals:
        i, val, t, l, r = v[:5]
        out.append({"id": i, "value": val, "type_id": t, "page": v[5] if len(v) > 5 else 1, "left_context": l, "right_context": r, "value_id": i})
    return out


def run_one(cid: str, data: bytes, vals, env_dir: Path, rules, band: str) -> dict:
    m = load()
    tmp = env_dir / "controls" / f"{cid}.pdf"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    items = _items(vals)
    try:
        orc = O.build(cid, tmp, "", custom=(items, []))
    except Exception as e:  # the oracle cannot even read it (e.g. encrypted, broken): no oracle; go straight to the gate
        orc = {"status": "oracle_unavailable", "occurrences": [], "error": type(e).__name__}
    if band == "TB" and orc.get("status") == "ok":
        orc = O.informative_tb(cid, tmp, orc)
    acts = m["policy"]["actions"]
    sl = [{**it, "action": acts.get(it["type_id"], "pseudonymize"), "ordinal_on_page": 1} for it in items]
    if orc.get("status") != "ok":
        try:
            gate.extract(data, rules)
            return {"stages": {"oracle": {"result": "failed", "reason": "oracle_unavailable"}}, "gate_on_oracle_failure": "accepted"}
        except Reject as r:
            return {"stages": {"gate": {"result": "failed", "reason": r.reason, "detail": r.detail}}}
        except Exception as e:
            return {"stages": {"gate": {"result": "failed", "reason": type(e).__name__}}}
    return run_case(cid, "", data, orc, m, env_dir / "controls", rules, sels_override=sl)


def outcome(r: dict) -> str:
    if r.get("success"):
        return "cycle"
    last = list(r["stages"].items())[-1]
    return f"{last[0]}:{last[1].get('reason') or last[1].get('check')}"


def main(env: str, band: str) -> None:
    out = ROOT / "outputs" / env
    out.mkdir(parents=True, exist_ok=True)
    ca = run_ca()
    rules_file = ROOT / "outputs" / "rules.json"
    rules_file.write_text(json.dumps(ca, indent=1, ensure_ascii=False) + "\n")
    rules = tuple(ca["surviving"])
    print("CA refuted:", ca["refuted"], "surviving:", rules)
    res: dict = {"band": band, "rules": list(rules), "CA": ca, "CR": {}, "CS": {}}
    for cid, (data, expected) in cr_controls().items():
        try:
            r = run_one(cid, data, VALS if cid != "CR23" else VALS, out, rules, band)
        except Exception:
            r = {"stages": {"error": {"result": "failed", "reason": traceback.format_exc()[-300:]}}}
        oc = outcome(r)
        ok = any(e in oc for e in expected.split("|"))
        res["CR"][cid] = {"expected": expected, "outcome": oc, "as_expected": ok, "accepted_indebidamente": oc == "cycle"}
        print(cid, oc, "OK" if ok else "UNEXPECTED")
    for cid, (data, vals) in cs_controls().items():
        try:
            r = run_one(cid, data, vals, out, rules, band)
        except Exception:
            r = {"stages": {"error": {"result": "failed", "reason": traceback.format_exc()[-300:]}}}
        res["CS"][cid] = {"outcome": outcome(r), "success_rt": r.get("success_rt", False), "plan": r.get("plan"),
                          "stages": {k: {kk: vv for kk, vv in v.items() if kk != "checks"} for k, v in r["stages"].items()}}
        print(cid, outcome(r), "(RT ok)" if r.get("success_rt") and not r.get("success") else "")
    (out / "controls.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str) + "\n")


if __name__ == "__main__":
    from . import transform as _tr
    var = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--variant=")), "")
    band = "TB" if "TB" in var.split("+") else "ink"
    if "RZ" in var.split("+"):
        _tr.VARIANT.add("RZ")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0], band)
    _ = (io, Fail, verify, restore, PlanFail)
