"""Verifier controls CV01-CV30 (protocol §8.3, §0.8): alterations of a VALID protected/restored output that the
verifier must reject. Base: L1 (Z1 + Z2 + redaction + permitted occurrence) and L2 (table borders), variant TB (the
only one in which they pass every check strictly, so a rejection can only come from the alteration).

    .venv/bin/python -m a08.cv <env>
"""

from __future__ import annotations

import io
import json
import sys
from decimal import Decimal

import pikepdf
from pypdf import PdfWriter
from pypdf.generic import NameObject, TextStringObject

from . import gate
from . import transform as T
from .manifest import ROOT, load
from .run import run_case
from .verify import Fail, verify


def _base(cid: str) -> dict:
    m = load()
    data = (ROOT / "corpus" / cid / f"{cid}.pdf").read_bytes()
    orc = json.loads((ROOT / "corpus" / cid / "oracle-TB.json").read_text())
    rules = tuple(json.loads((ROOT / "outputs" / "rules.json").read_text())["surviving"])
    r = run_case(cid, next(c["template"] for c in m["cases"] if c["case_id"] == cid), data, orc, m, ROOT / "outputs" / "cv-base", rules)
    assert r.get("success"), (cid, r["stages"])
    a = r["_artifacts"]
    return {"cid": cid, "orig": data, "oracle": orc, "policy": m["policy"], **a}


def _edit(pdf_bytes: bytes, fn) -> bytes:
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    fn(pdf)
    o = io.BytesIO()
    pdf.save(o, object_stream_mode=pikepdf.ObjectStreamMode.disable)
    return o.getvalue()


def _ops(pdf, page=0):
    c = pdf.pages[page].obj.Contents
    st = c[0] if isinstance(c, pikepdf.Array) else c
    return st, list(pikepdf.parse_content_stream(st))


def _value_codes(b, sid: str) -> tuple[str, bytes]:
    """Font resource and codes of a selected value, encoded with the value's original font (for injection)."""
    ln = next(ln for ln in b["binding"]["lines"] for s in ln["sels"] if s["sid"] == sid)
    sel = next(s for s in ln["sels"] if s["sid"] == sid)
    val = b["values"][sel["token"]] if sel["token"] else None
    run = sel["runs"][-1]
    pdf = pikepdf.open(io.BytesIO(b["protected"]))
    from .pdfmodel import load_font
    f = load_font(run["font"], pdf.pages[ln["page"] - 1].Resources.Font[run["font"]])
    rev = f.reverse()
    codes = b"".join(rev[ch][0] for ch in val if ch in rev)
    return run["font"], codes


def controls(b1: dict, b2: dict) -> dict:
    out = {}
    P, R = b1["protected"], b1.get("restored")
    zone_s3 = next(o for o in b1["oracle"]["occurrences"] if o["id"] == "S3")["zone"]

    def overlay(pdf):
        st, ops = _ops(pdf)
        for o in b1["oracle"]["occurrences"]:
            if o["kind"] == "S" and o["page"] == 1 and "zone" in o:
                z = o["zone"]
                ops.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")))
                ops += pikepdf.parse_content_stream(pikepdf.Stream(pdf, f"0 g {z['x0']} {z['band'][0]} {z['value_right'] - z['x0']} {z['band'][1] - z['band'][0]} re f Q".encode()))
        st.write(pikepdf.unparse_content_stream(ops))
    out["CV01"] = ("protected", _edit(b1["orig"], overlay), "superposición con el texto original debajo")
    font, codes = _value_codes(b1, "S3")

    def outside(pdf):
        st, ops = _ops(pdf)
        st.write(pikepdf.unparse_content_stream(ops) + f"\nBT {font} 11 Tf 2000 100 Td <{codes.hex()}> Tj ET".encode())
    out["CV02"] = ("protected", _edit(P, outside), "valor fuera de la CropBox")
    val3 = b1["values"][next(s for ln in b1["binding"]["lines"] for s in ln["sels"] if s["sid"] == "S3")["token"]]
    out["CV03"] = ("protected", _edit(P, lambda pdf: setattr(pdf.docinfo, "Title", val3) if False else pdf.docinfo.__setitem__("/Title", pikepdf.String(val3))), "copia en /Info /Title")

    def xmp(pdf):
        pdf.Root.Metadata = pdf.make_stream(f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:dc="http://purl.org/dc/elements/1.1/"><rdf:Description><dc:title>{val3}</dc:title></rdf:Description></rdf:RDF></x:xmpmeta>'.encode(), Type=pikepdf.Name.Metadata, Subtype=pikepdf.Name.XML)
    out["CV04"] = ("protected", _edit(P, xmp), "copia en XMP dc:title")

    def outline(pdf):
        with pdf.open_outline() as ol:
            ol.root.append(pikepdf.OutlineItem(val3, 0))
    out["CV05"] = ("protected", _edit(P, outline), "copia en un título del esquema")

    def link(pdf, contents=None, uri=None, rect=(72, 72, 120, 90)):
        d = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=list(rect),
                               A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String(uri or "https://example.test/")))
        if contents:
            d.Contents = pikepdf.String(contents)
        pdf.pages[0].obj.Annots = pikepdf.Array(list(pdf.pages[0].obj.get("/Annots", [])) + [pdf.make_indirect(d)])
    enc = "".join(f"%{b:02X}" for b in val3.encode())
    out["CV06"] = ("protected", _edit(P, lambda pdf: link(pdf, uri="mailto:" + enc)), "copia en /URI codificada %XX")
    out["CV07"] = ("protected", _edit(P, lambda pdf: link(pdf, contents=val3)), "copia en /Contents de una anotación")
    w = PdfWriter(clone_from=io.BytesIO(P))
    w._add_object(TextStringObject(val3))
    o = io.BytesIO()
    w.write(o)
    out["CV08"] = ("protected", o.getvalue(), "copia en un objeto huérfano")

    def objstm(pdf):
        pdf.Root["/A08Note"] = pdf.make_indirect(pikepdf.Dictionary(Note=pikepdf.String(val3)))
    pdf = pikepdf.open(io.BytesIO(P))
    objstm(pdf)
    o = io.BytesIO()
    pdf.save(o, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    out["CV09"] = ("protected", o.getvalue(), "copia dentro de un object stream")

    def tr3(pdf):
        st, ops = _ops(pdf)
        st.write(pikepdf.unparse_content_stream(ops) + f"\nBT 3 Tr {font} 11 Tf 72 72 Td <{codes.hex()}> Tj ET".encode())
    out["CV10"] = ("protected", _edit(P, tr3), "copia como texto Tr 3 en otra posición")

    def unused_form(pdf):
        fonts = pdf.pages[0].Resources.Font
        other = next(k for k in fonts.keys() if k != font)
        f2 = gate.extract(P).interp  # noqa: F841 (keeps the derivative parsable)
        from .pdfmodel import load_font
        fo = load_font(other, fonts[other])
        rev = fo.reverse()
        cds = b"".join(rev[ch][0] for ch in val3 if ch in rev and len(rev[ch]) == 1)
        xo = pdf.make_stream(f"BT {other} 11 Tf 0 0 Td <{cds.hex()}> Tj ET".encode(), Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form, BBox=[0, 0, 10, 10],
                             Resources=pikepdf.Dictionary(Font=fonts))
        if "/XObject" not in pdf.pages[0].Resources:
            pdf.pages[0].Resources.XObject = pikepdf.Dictionary()
        pdf.pages[0].Resources.XObject["/FmA08"] = xo
    out["CV11"] = ("protected", _edit(P, unused_form), "copia con otra fuente en un Form XObject no usado")

    def first_show(ops):
        return next(i for i, x in enumerate(ops) if str(x.operator) in ("Tj", "TJ"))

    def alter_glyph(pdf):
        st, ops = _ops(pdf)
        i = first_show(ops)
        a = ops[i].operands
        arr = list(a[0]) if str(ops[i].operator) == "TJ" else [a[0]]
        s0 = next(k for k, x in enumerate(arr) if isinstance(x, pikepdf.String))
        bb = bytearray(bytes(arr[s0]))
        bb[0] = bb[1] if len(bb) > 1 else (bb[0] ^ 1)
        arr[s0] = pikepdf.String(bytes(bb))
        ops[i] = pikepdf.ContentStreamInstruction([pikepdf.Array(arr)], pikepdf.Operator("TJ"))
        st.write(pikepdf.unparse_content_stream(ops))
    out["CV12"] = ("protected", _edit(P, alter_glyph), "carácter no seleccionado cambiado")

    def shift_line(pdf):
        st, ops = _ops(pdf)
        i = next(k for k, x in enumerate(ops) if str(x.operator) in ("Td", "Tm"))
        vals = [float(v) for v in ops[i].operands]
        vals[-1] += 0.5
        ops[i] = pikepdf.ContentStreamInstruction(vals, ops[i].operator)
        st.write(pikepdf.unparse_content_stream(ops))
    out["CV13"] = ("protected", _edit(P, shift_line), "línea no seleccionada desplazada 0,5 pt")

    def recolor(pdf):
        st, ops = _ops(pdf)
        i = first_show(ops)
        ops.insert(i, pikepdf.ContentStreamInstruction([1, 0, 0], pikepdf.Operator("rg")))
        st.write(pikepdf.unparse_content_stream(ops))
    out["CV14"] = ("protected", _edit(P, recolor), "color de una palabra no seleccionada")
    P2 = b2["protected"]
    occ2 = [o["zone"] for o in b2["oracle"]["occurrences"] if o["kind"] == "S" and o["page"] == 1 and "zone" in o]

    def drop_path(pdf, near=False):
        st, ops = _ops(pdf)
        idx = None
        for k, x in enumerate(ops):
            if str(x.operator) in ("f", "S", "f*", "B") and k >= 2:
                if not near:
                    idx = k
                    break
                # a border inside the dilated mask of a zone: path built right before, near a zone's right edge
                seg = [y for y in ops[max(0, k - 6):k] if str(y.operator) in ("re", "m", "l")]
                pts = [float(v) for y in seg for v in y.operands]
                if pts and any(abs(pts[0] - z["r_z1"]) < 40 and z["band"][0] - 20 < pts[1] < z["band"][1] + 20 for z in occ2):
                    idx = k
                    break
        if idx is None:
            raise RuntimeError("no path")
        ops[idx] = pikepdf.ContentStreamInstruction([], pikepdf.Operator("n"))
        st.write(pikepdf.unparse_content_stream(ops))
    out["CV15"] = ("protected2", _edit(P2, drop_path), "trazado (borde) eliminado")
    out["CV27"] = ("protected2", _edit(P2, lambda pdf: drop_path(pdf, near=True)), "borde de celda junto a una zona (dentro de la máscara) eliminado")
    ln_s3 = next(ln for ln in b1["binding"]["lines"] for s in ln["sels"] if s["sid"] == "S3")

    def block_edit(pdf, fn, line=ln_s3):
        st, ops = _ops(pdf, line["page"] - 1)
        b0, b1_ = line["block"]
        blk = ops[b0:b1_]
        blk = fn(pdf, blk)
        ops[b0:b1_] = blk
        st.write(pikepdf.unparse_content_stream(ops))

    def as_paths(pdf, blk):
        k = next(i for i, x in enumerate(blk) if str(x.operator) == "Tj")
        return blk[:k] + [pikepdf.ContentStreamInstruction([pikepdf.Array([Decimal(-12000)])], pikepdf.Operator("TJ"))] + blk[k + 1:]
    pdfp = pikepdf.open(io.BytesIO(P))
    block_edit(pdfp, as_paths)
    st, ops = _ops(pdfp)
    st.write(pikepdf.unparse_content_stream(ops) + f"\nq 0 g {zone_s3['x0']} {zone_s3['band'][0]} 100 4 re f Q".encode())
    o = io.BytesIO()
    pdfp.save(o)
    out["CV16"] = ("protected", o.getvalue(), "sustituto dibujado como trazados (no extraíble)")

    def no_tounicode(pdf):
        for k, f in pdf.pages[0].Resources.Font.items():
            if k == T.CAND_RES:
                cm = f.ToUnicode.read_bytes().decode()
                import re as _re
                f.ToUnicode = pdf.make_stream(_re.sub(r"<([0-9A-F]{2})> <00([0-9A-F]{2})>", lambda m: f"<{m.group(1)}> <00{(int(m.group(2), 16) % 26 + 0x61):02X}>", cm).encode())
    out["CV17"] = ("protected", _edit(P, no_tounicode), "fuente del sustituto con ToUnicode erróneo (texto no seleccionable como sustituto)")
    first_code = codes[:1] if len(codes) else b""

    def keep_first(pdf, blk):
        k = next(i for i, x in enumerate(blk) if str(x.operator) == "Tj")
        return blk[:k + 1] + [pikepdf.ContentStreamInstruction([pikepdf.Name(font), 11], pikepdf.Operator("Tf")),
                              pikepdf.ContentStreamInstruction([pikepdf.String(first_code)], pikepdf.Operator("Tj"))] + blk[k + 1:]
    out["CV18"] = ("protected", _edit(P, lambda pdf: block_edit(pdf, keep_first)), "primer carácter del valor conservado")

    def spaced(pdf, blk):
        k = next(i for i, x in enumerate(blk) if str(x.operator) == "Tj")
        s = bytes(blk[k].operands[0])
        return blk[:k] + [pikepdf.ContentStreamInstruction([pikepdf.Array([pikepdf.String(s[:7]), Decimal(-600), pikepdf.String(s[7:])])], pikepdf.Operator("TJ"))] + blk[k + 1:]
    out["CV19"] = ("protected", _edit(P, lambda pdf: block_edit(pdf, spaced)), "marcador con un espacio insertado por TJ")
    toks = [s["token"] for ln in b1["binding"]["lines"] for s in ln["sels"] if s["token"]]
    sw = dict(b1["values"])
    sw[toks[0]], sw[toks[1]] = b1["values"][toks[1]], b1["values"][toks[0]]
    try:
        out["CV20"] = ("restored", T.restore(P, b1["binding"], sw), "restauración con dos valores intercambiados")
    except T.PlanFail as e:
        out["CV20"] = ("restorer", e.reason, "restauración con dos valores intercambiados")
    if R:
        def misplace(pdf, blk):
            k = next(i for i, x in enumerate(blk) if str(x.operator) == "Tm")
            v = [float(x) for x in blk[k].operands]
            v[5] -= 30
            return blk[:k] + [pikepdf.ContentStreamInstruction(v, pikepdf.Operator("Tm"))] + blk[k + 1:]
        out["CV21"] = ("restored", _edit(R, lambda pdf: block_edit(pdf, misplace)), "valor restaurado en otra posición")
        out["CV30"] = ("restored", _edit(R, lambda pdf: _drop_redacted(pdf)), "restauración que elimina un [REDACTED]")
    out["CV22"] = ("protected", b1["orig"], "original sin modificar presentado como protegido")
    if b2.get("restored"):
        out["CV23"] = ("restored", b2["restored"], "restaurado de otro derivado")
    ln_z2 = next((ln for ln in b1["binding"]["lines"] if ln["kind"] == "Z2"), None)
    if ln_z2:
        def ink_tjs(blk):
            return [i for i, x in enumerate(blk) if str(x.operator) == "Tj" and bytes(x.operands[0]).strip(b" \x00\x03")]

        def mid(blk):
            t = ink_tjs(blk)
            return t[len(t) // 2]  # a moved glyph well inside the zone (the last one sits on the zone border)

        def drop_moved(pdf, blk):
            k = mid(blk)
            return blk[:k] + blk[k + 1:]

        def change_moved(pdf, blk):
            k = mid(blk)
            s = bytearray(bytes(blk[k].operands[0]))
            s[-1] = s[-1] ^ 1
            return blk[:k] + [pikepdf.ContentStreamInstruction([pikepdf.String(bytes(s))], pikepdf.Operator("Tj"))] + blk[k + 1:]

        def shift_moved(pdf, blk):
            last = mid(blk)
            k = max(i for i, x in enumerate(blk[:last]) if str(x.operator) == "Tm")
            v = [float(x) for x in blk[k].operands]
            v[4] += 1.0
            return blk[:k] + [pikepdf.ContentStreamInstruction(v, pikepdf.Operator("Tm"))] + blk[k + 1:]
        out["CV24"] = ("protected", _edit(P, lambda pdf: block_edit(pdf, drop_moved, ln_z2)), "Z2: carácter del sufijo perdido")
        out["CV25"] = ("protected", _edit(P, lambda pdf: block_edit(pdf, change_moved, ln_z2)), "Z2: carácter del sufijo cambiado")
        out["CV26"] = ("protected", _edit(P, lambda pdf: block_edit(pdf, shift_moved, ln_z2)), "Z2: sufijo desplazado 1 pt")
        bnd = json.loads(json.dumps(b1["binding"]))
        for ln in bnd["lines"]:
            if ln["kind"] == "Z2":
                ln["f"] = 1.0  # a restorer that does not undo the compression leaves the suffix where the derivative put it
                for s in ln["sels"]:
                    s["sub_nat_page"] = s["span_page"]
        out["CV29"] = ("restored", T.restore(P, bnd, b1["values"]), "restauración Z2 con el sufijo en posiciones comprimidas")
    out["CV28"] = ("protected", _edit(P, lambda pdf: link(pdf, rect=(300, 300, 350, 320))), "anotación de enlace añadida/desplazada")
    return out


def _drop_redacted(pdf):
    st, ops = _ops(pdf)
    for k, x in enumerate(ops):
        if str(x.operator) == "Tj" and bytes(x.operands[0]) == b"[REDACTED]":
            ops[k] = pikepdf.ContentStreamInstruction([pikepdf.Array([Decimal(-1000)])], pikepdf.Operator("TJ"))
            break
    st.write(pikepdf.unparse_content_stream(ops))


def main(env: str) -> None:
    b1, b2 = _base("L1"), _base("L2")
    res = {}
    for cid, (mode, data, desc) in sorted(controls(b1, b2).items()):
        if mode == "restorer":
            res[cid] = {"description": desc, "detected": True, "by": f"restorer:{data}"}
            print(cid, "detected by restorer", data)
            continue
        base = b2 if mode == "protected2" else (b2 if cid == "CV23" else b1)
        base = b1 if cid == "CV23" else base
        try:
            if mode.startswith("protected"):
                verify(base["orig"], data, base["oracle"], base["binding"], base["policy"], mode="protected", selected_values=base["sel_values"])
            else:
                verify(base["orig"], data, base["oracle"], base["binding"], base["policy"], mode="restored", restored_from=base["protected"], values=base["values"])
            res[cid] = {"description": desc, "detected": False}
            print(cid, "NOT DETECTED", desc)
        except Fail as f:
            res[cid] = {"description": desc, "detected": True, "by": f.check, "detail": f.detail[:120]}
            print(cid, "detected by", f.check, "-", desc)
    out = ROOT / "outputs" / env
    out.mkdir(parents=True, exist_ok=True)
    (out / "cv.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n")
    _ = NameObject


if __name__ == "__main__":
    main(sys.argv[1])
