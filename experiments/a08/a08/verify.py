"""Independent verifier (protocol §9 + §0.4-§0.6). Never uses the transformer's output to set expectations: those come
from the ORIGINAL (PDFium/Poppler/pypdf), the frozen oracle/zones and the plan numbers. Parsers: PDFium, Poppler and
pypdf (strict) — none of them is pikepdf/QPDF, which the transformer uses.
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
import tempfile
import unicodedata
import urllib.parse
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as c
from cawbase_content_engine.protection import markers as A06
from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    ContentStream,
    DictionaryObject,
    IndirectObject,
    NameObject,
    StreamObject,
    TextStringObject,
)

DPI = 150
DILATE = 3


class Fail(Exception):
    def __init__(self, check: str, detail: str = "", report: dict | None = None) -> None:
        super().__init__(f"{check}: {detail}")
        self.check = check
        self.detail = detail
        self.report = report or {}


# ---------------------------------------------------------------- readers
def pdfium_chars(data: bytes) -> list[list[dict]]:
    doc = pdfium.PdfDocument(data)
    out = []
    for i in range(len(doc)):
        tp = doc[i].get_textpage()
        page = []
        for k in range(tp.count_chars()):
            u = chr(c.FPDFText_GetUnicode(tp.raw, k))
            gen = bool(c.FPDFText_IsGenerated(tp.raw, k))
            if u in "\r\n" or (gen and not u.isspace()):
                continue
            b = tp.get_charbox(k)
            page.append({"u": " " if u.isspace() else u, "box": list(b), "gen": gen})
        out.append(page)
    return out


def render_pdfium(data: bytes, no_text: bool = False) -> list[np.ndarray]:
    doc = pdfium.PdfDocument(data)
    out = []
    for i in range(len(doc)):
        p = doc[i]
        if no_text:
            for o in list(p.get_objects(max_depth=1)):
                if o.type == c.FPDF_PAGEOBJ_TEXT:
                    p.remove_obj(o)
            p.gen_content()
        bm = p.render(scale=DPI / 72, grayscale=True)
        out.append(np.array(bm.to_pil().convert("L")))
    return out


def render_poppler(data: bytes) -> list[np.ndarray]:
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "in.pdf"
        f.write_bytes(data)
        r = subprocess.run(["pdftoppm", "-r", str(DPI), "-gray", "-png", str(f), str(Path(td) / "p")], capture_output=True)
        if r.returncode or r.stderr.strip():
            raise Fail("VA", "poppler render stderr")
        files = sorted(Path(td).glob("p-*.png"), key=lambda q: int(q.stem.split("-")[-1]))
        return [np.array(Image.open(x).convert("L")) for x in files]


def poppler_text(data: bytes) -> tuple[list[str], str]:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        r = subprocess.run(["pdftotext", "-raw", "-enc", "UTF-8", f.name, "-"], capture_output=True)
        info = subprocess.run(["pdfinfo", f.name], capture_output=True)
        err = (r.stderr + info.stderr).decode("utf-8", "replace").strip()
    return r.stdout.decode("utf-8").split("\f"), err


def poppler_words(data: bytes) -> list[list[dict]]:
    import xml.etree.ElementTree as ET
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        xml = subprocess.run(["pdftotext", "-bbox", f.name, "-"], capture_output=True).stdout.decode("utf-8")
    xml = re.sub(r"<!DOCTYPE[^>]*>", "", xml)
    root = ET.fromstring(xml)
    pages = []
    for pg in root.iter("{http://www.w3.org/1999/xhtml}page"):
        h = float(pg.get("height"))
        ws = []
        for w in pg.iter("{http://www.w3.org/1999/xhtml}word"):
            x0, y0, x1, y1 = (float(w.get(k)) for k in ("xMin", "yMin", "xMax", "yMax"))
            ws.append({"t": w.text or "", "box": [x0, h - y1, x1, h - y0]})
        pages.append(ws)
    return pages


def pypdf_reader(data: bytes) -> PdfReader:
    warnings: list[str] = []

    class H(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())

    lg = logging.getLogger("pypdf")
    h = H()
    lg.addHandler(h)
    try:
        r = PdfReader(io.BytesIO(data), strict=True)
        for p in r.pages:
            p.get_contents()
            _ = p.extract_text()
    finally:
        lg.removeHandler(h)
    if warnings:
        raise Fail("VA", "pypdf warnings: " + "; ".join(warnings)[:200])
    return r


# ---------------------------------------------------------------- helpers
def _center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _in(pt, z, tol=0.0):
    return z[0] - tol <= pt[0] <= z[2] + tol and z[1] - tol <= pt[1] <= z[3] + tol


def reps(v: str) -> set[str]:
    out = set()
    for n in (v, unicodedata.normalize("NFC", v), unicodedata.normalize("NFD", v)):
        for x in (n, n.casefold()):
            out.add(x)
            out.add(" ".join(x.split()))
    return {x for x in out if x.strip()}


def _decoded(o) -> list[str]:
    out = []
    if isinstance(o, TextStringObject):
        out.append(str(o))
    elif isinstance(o, ByteStringObject):
        b = bytes(o)
        for enc in ("utf-8", "utf-16-be", "utf-16-le", "latin-1"):
            try:
                out.append(b.decode(enc))
            except UnicodeDecodeError:
                pass
    elif isinstance(o, NameObject):
        out.append(str(o))
    s = list(out)
    for x in s:
        try:
            out.append(urllib.parse.unquote(x))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- structure (pypdf)
def walk(reader: PdfReader):
    """Reachable objects from the trailer (pypdf objects), plus the set of all object numbers in the xref."""
    seen: set[int] = set()
    out = []
    stack = [reader.trailer]
    while stack:
        o = stack.pop()
        if isinstance(o, IndirectObject):
            if o.idnum in seen:
                continue
            seen.add(o.idnum)
            o = o.get_object()
        out.append(o)
        if isinstance(o, (DictionaryObject, StreamObject)):
            for k, v in o.items():
                if k in ("/Parent", "/P", "/Pg"):
                    if isinstance(v, IndirectObject) and v.idnum not in seen:
                        stack.append(v)
                    continue
                stack.append(v)
        elif isinstance(o, ArrayObject):
            stack.extend(o)
    all_ids = set()
    for gen, ids in reader.xref.items():
        all_ids |= set(ids.keys())
    all_ids |= set(reader.xref_objStm.keys()) if hasattr(reader, "xref_objStm") else set()
    return out, seen, all_ids


def content_streams(reader: PdfReader):
    """(label, ContentStream, resources) for every page content and every Form XObject (used or not)."""
    out = []
    for pi, p in enumerate(reader.pages):
        cs = p.get_contents()
        if cs is not None:
            out.append((f"page{pi + 1}", ContentStream(cs, reader), p.get("/Resources"), pi + 1))
    objs, _, _ = walk(reader)
    for o in objs:
        if isinstance(o, StreamObject) and o.get("/Subtype") == "/Form":
            out.append(("form", ContentStream(o, reader), o.get("/Resources"), None))
    return out


def font_decoders(reader: PdfReader) -> dict:
    """pypdf's own encodings and ToUnicode maps for every font object in the document."""
    from pypdf._cmap import get_encoding
    decs = {}
    objs, _, _ = walk(reader)
    for o in objs:
        if isinstance(o, DictionaryObject) and o.get("/Type") == "/Font" and o.get("/Subtype") in ("/TrueType", "/Type1", "/Type0", "/MMType1"):
            try:
                enc, mp = get_encoding(o)
            except Exception:
                continue
            decs[id(o)] = (o, enc, mp, o.get("/Subtype") == "/Type0")
    return decs


def _decode_with(dec, data: bytes) -> str:
    o, enc, mp, two = dec
    out = []
    if two:
        for i in range(0, len(data) - 1, 2):
            k = data[i:i + 2].decode("latin-1")
            out.append(mp.get(k, mp.get(chr(int.from_bytes(data[i:i + 2], "big")), "")))
    else:
        for b in data:
            k = chr(b)
            if k in mp:
                out.append(mp[k])
            elif isinstance(enc, dict):
                out.append(enc.get(b, ""))
            else:
                try:
                    out.append(bytes([b]).decode(enc))
                except Exception:
                    out.append("")
    return "".join(x if isinstance(x, str) else "" for x in out)


def show_texts(reader: PdfReader) -> list[tuple[str, int | None, str, str]]:
    """Every show operator of every content stream decoded with its current font AND with every font."""
    decs = font_decoders(reader)
    out = []
    for label, cs, res, page in content_streams(reader):
        fonts = (res.get_object().get("/Font") if res is not None else None) or {}
        cur = None
        per_current, per_any = [], {k: [] for k in decs}
        for operands, op in cs.operations:
            if op == b"Tf":
                fo = fonts.get(operands[0]) if fonts else None
                fo = fo.get_object() if fo is not None else None
                cur = next((d for d in decs.values() if d[0] is fo), None)
            elif op in (b"Tj", b"'", b'"', b"TJ"):
                strs = [x for x in (operands[0] if op == b"TJ" else [operands[-1]]) if isinstance(x, (ByteStringObject, TextStringObject))]
                for s in strs:
                    raw = bytes(s) if isinstance(s, ByteStringObject) else s.get_original_bytes()
                    if cur is not None:
                        per_current.append(_decode_with(cur, raw))
                    for k, d in decs.items():
                        per_any[k].append(_decode_with(d, raw))
        out.append((label, page, "".join(per_current), "\n".join("".join(v) for v in per_any.values())))
    return out


def non_text_ops(reader: PdfReader) -> list:
    out = []
    for label, cs, res, page in content_streams(reader):
        seq, inbt = [], False
        for operands, op in cs.operations:
            if op == b"BT":
                inbt = True
                seq.append(("BT",))
            elif op == b"ET":
                inbt = False
                seq.append(("ET",))
            elif op in (b"BDC", b"BMC", b"EMC"):
                seq.append((op.decode(),))
            elif not inbt:
                seq.append((op.decode("latin-1"), tuple(str(x) for x in operands) if op != b"INLINE IMAGE" else ("img",)))
        out.append((label, seq))
    return out


def annotations(reader: PdfReader) -> list:
    out = []
    for pi, p in enumerate(reader.pages):
        for a in p.get("/Annots", []) or []:
            a = a.get_object()
            if a.get("/Subtype") in ("/Text", "/Popup"):
                continue
            act = a.get("/A")
            act = act.get_object() if act is not None else None
            out.append((pi, a.get("/Subtype"), tuple(round(float(v), 3) for v in a.get("/Rect")),
                        str(act.get("/S")) if act else None, str(act.get("/URI")) if act and "/URI" in act else None))
    return out


# ---------------------------------------------------------------- the checks
def zones_of(plan_lines: list[dict], oracle: dict) -> list[dict]:
    zs = []
    for ln in plan_lines:
        occs = {o["id"]: o for o in oracle["occurrences"]}
        sels = ln["sels"]
        band = [min(occs[s["sid"]]["zone"]["band"][0] for s in sels), max(occs[s["sid"]]["zone"]["band"][1] for s in sels)]
        if ln["kind"] == "Z1":
            for s in sels:
                z = occs[s["sid"]]["zone"]
                zs.append({"page": ln["page"], "rect": [z["x0"], z["band"][0], z["r_z1"], z["band"][1]], "kind": "Z1", "sids": [s["sid"]]})
        else:
            x0 = min(occs[s["sid"]]["zone"]["x0"] for s in sels)
            zs.append({"page": ln["page"], "rect": [x0, band[0], ln["r"], band[1]], "kind": "Z2", "sids": [s["sid"] for s in sels]})
    return zs


def expected_zone_text(z: dict, ln: dict, orig_chars: list[dict], subs: dict[str, str], oracle: dict, values=None) -> tuple[str, list]:
    """Text expected inside a zone and, for Z2, the expected left edge of each non-selected char (from the ORIGINAL)."""
    occs = {o["id"]: o for o in oracle["occurrences"]}
    if z["kind"] == "Z1":
        sid = z["sids"][0]
        return (values or subs)[sid], []
    sel_boxes = {sid: occs[sid]["pdfium_chars"] for sid in z["sids"]}
    sel_ranges = sorted(((min(c["box"][0] for c in bx), max(c["box"][2] for c in bx), sid) for sid, bx in sel_boxes.items()))
    sl = {s["sid"]: s for s in ln["sels"]}
    items = []
    for a, b, sid in sel_ranges:
        items.append(("s", a, sid))
    x0 = ln["x0"]
    sel_list = [c["box"] for bx in sel_boxes.values() for c in bx]

    def is_sel(box):
        return any(max(abs(p - q) for p, q in zip(box, sb)) <= 0.01 for sb in sel_list)

    for ch in orig_chars:
        cx, cy = _center(ch["box"])
        if _in((cx, cy), z["rect"]) and not is_sel(ch["box"]) and not ch["u"].isspace():
            items.append(("c", ch["box"][0], ch))
    items.sort(key=lambda t: t[1])
    text, exp = [], []
    for kind, x, obj in items:
        if kind == "s":
            text.append((values or subs)[obj])
        else:
            D = sum(sl[sid]["sub_nat_page"] - sl[sid]["span_page"] for a, b, sid in sel_ranges if b <= obj["box"][0] + 0.01)
            if values is None:
                xl = x0 + ln["f"] * (obj["box"][0] + D - x0)
            else:
                xl = obj["box"][0]
            text.append(obj["u"])
            exp.append((obj["u"], xl, obj["box"]))
    return "".join(text), exp


def mask_for(zones, page: int, shape, height_pt: float) -> np.ndarray:
    m = np.zeros(shape, dtype=bool)
    s = DPI / 72.0
    for z in zones:
        if z["page"] != page:
            continue
        x0, y0, x1, y1 = z["rect"]
        c0, c1 = int(x0 * s) - DILATE, int(np.ceil(x1 * s)) + DILATE
        r0, r1 = int((height_pt - y1) * s) - DILATE, int(np.ceil((height_pt - y0) * s)) + DILATE
        m[max(r0, 0):min(r1, shape[0]), max(c0, 0):min(c1, shape[1])] = True
    return m


def verify(original: bytes, out: bytes, oracle: dict, binding: dict, policy_values: dict, *, mode: str,
           restored_from: bytes | None = None, values: dict | None = None, selected_values: list[str] | None = None) -> dict:
    """mode 'protected': `out` vs original. mode 'restored': `out` vs derivative (restored_from) and original.
    Returns {check: 'passed' | detail}; raises Fail on the first failure."""
    report: dict = {}
    ref = original if mode == "protected" else restored_from
    # VA: opening without repair (PDFium, Poppler, pypdf strict)
    try:
        pdfium.PdfDocument(out)
    except Exception as e:
        raise Fail("VA", f"pdfium {e}") from None
    ptext, perr = poppler_text(out)
    if perr:
        raise Fail("VA", "poppler stderr: " + perr[:120])
    rd = pypdf_reader(out)
    rr = PdfReader(io.BytesIO(ref))
    import shutil
    if shutil.which("qpdf"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(out)
            f.flush()
            q = subprocess.run(["qpdf", "--check", f.name], capture_output=True, text=True)
        if q.returncode != 0 or "WARNING" in (q.stdout + q.stderr):
            raise Fail("VA", "qpdf --check")
        report["VA_qpdf"] = "passed"
    else:
        report["VA_qpdf"] = "not run (qpdf CLI not installed)"
    report["VA"] = "passed"
    # VG: geometry
    if len(rd.pages) != len(rr.pages):
        raise Fail("VG", "page count")
    for a, b in zip(rd.pages, rr.pages):
        for k in ("/MediaBox", "/CropBox", "/Rotate", "/UserUnit"):
            va, vb = a.get(k), b.get(k)
            if (va is None) != (vb is None) or (va is not None and [float(x) for x in (va if isinstance(va, list) else [va])] != [float(x) for x in (vb if isinstance(vb, list) else [vb])]):
                raise Fail("VG", k)
    report["VG"] = "passed"
    zones = zones_of(binding["lines"], oracle)
    lines_by_sid = {s["sid"]: ln for ln in binding["lines"] for s in ln["sels"]}
    subs = {s["sid"]: s["substitute"] for ln in binding["lines"] for s in ln["sels"]}
    oc = pdfium_chars(original)
    rc = pdfium_chars(ref)
    dc = pdfium_chars(out)
    # VT: text outside zones identical. Ink characters: same Unicode, order and box (+-0.01 pt). Whitespace: compared as
    # word separation (runs of real or PDFium-generated spaces collapse to one), because PDFium decides heuristically
    # whether a space is emitted as a real or a generated character (verifier note N-1 of the report).
    for pi, (ra, da) in enumerate(zip(rc, dc)):
        zp = [z["rect"] for z in zones if z["page"] == pi + 1]

        def outside(chars):
            seq = []
            for ch in chars:
                if ch["u"].isspace():
                    if seq and seq[-1]["u"] != " ":
                        seq.append({"u": " ", "box": None})
                    continue
                if not any(_in(_center(ch["box"]), z) for z in zp):
                    seq.append(ch)
                elif seq and seq[-1]["u"] != " ":
                    seq.append({"u": " ", "box": None})
            while seq and seq[-1]["u"] == " ":
                seq.pop()
            return seq
        oa, ob = outside(ra), outside(da)
        if [x["u"] for x in oa] != [x["u"] for x in ob]:
            raise Fail("VT", f"page {pi + 1} text outside zones differs")
        for x, y in zip(oa, ob):
            if x["box"] is not None and max(abs(p - q) for p, q in zip(x["box"], y["box"])) > 0.01:
                raise Fail("VT", f"page {pi + 1} char '{x['u']}' moved outside zones")
    report["VT"] = "passed"
    # VZ / VO / Z2 oracle
    zt = []
    for z in zones:
        ln = lines_by_sid[z["sids"][0]]
        vals = None
        if mode == "restored":
            vals = {s["sid"]: (values[s["token"]] if s["token"] else s["substitute"]) for s in ln["sels"]}
        exp_text, exp_pos = expected_zone_text(z, ln, oc[z["page"] - 1], subs, oracle, vals)
        inside = sorted([ch for ch in dc[z["page"] - 1] if not ch["u"].isspace() and _in(_center(ch["box"]), z["rect"])], key=lambda q: q["box"][0])
        got = "".join(ch["u"] for ch in inside if not ch["u"].isspace())
        if got != "".join(exp_text.split()):
            raise Fail("VZ", f"zone {z['sids']} text")
        for ch in inside:
            if ch["u"].isspace():
                continue
            b, zr = ch["box"], z["rect"]
            if mode == "protected" and b[2] > zr[2] + 0.25 or b[0] < zr[0] - 0.25 or b[1] < zr[1] - 0.25 or b[3] > zr[3] + 0.25:
                raise Fail("VO", f"zone {z['sids']} overflow")
            if ch["u"] == "�" or ord(ch["u"]) == 0:
                raise Fail("VZ", "notdef")
        if exp_pos:
            nonsel = [ch for ch in inside]
            for u, xl, obox in exp_pos:
                cand = [ch for ch in nonsel if ch["u"] == u and abs(ch["box"][0] - xl) <= 0.05]
                if not cand:
                    raise Fail("Z2", f"zone {z['sids']} char '{u}' not at its expected position")
                if abs(cand[0]["box"][1] - obox[1]) > 0.05 or abs(cand[0]["box"][3] - obox[3]) > 0.05:
                    raise Fail("Z2", "height/baseline")
        if mode == "protected":
            # marker grammar with whitespace kept (real or PDFium-generated): a gap inside a marker breaks it
            spaced = "".join(ch["u"] for ch in dc[z["page"] - 1] if _in(_center(ch["box"]), z["rect"]))  # PDFium reading order
            for s in ln["sels"]:
                if s["sid"] in z["sids"] and s["substitute"] not in spaced:
                    raise Fail("VZ", "substitute grammar (exact, whitespace included)")
            if A06.scan(got).altered:
                raise Fail("VZ", "altered marker")
        zt.append({"sids": z["sids"], "kind": z["kind"], "text_ok": True})
        # Poppler: words of the zone
    # Poppler groups by words, not characters: the zone's expected text must appear in Poppler's text of the page
    # (whitespace removed) — verifier note N-2 of the report. Positions are checked at character level with PDFium.
    for z in zones:
        ln = lines_by_sid[z["sids"][0]]
        vals = None
        if mode == "restored":
            vals = {s["sid"]: (values[s["token"]] if s["token"] else s["substitute"]) for s in ln["sels"]}
        exp_text, _ = expected_zone_text(z, ln, oc[z["page"] - 1], subs, oracle, vals)
        page_txt = "".join(ptext[z["page"] - 1].split())
        if "".join(exp_text.split()) not in page_txt:
            raise Fail("VZ", f"poppler zone {z['sids']}")
    report["VZ"] = "passed"
    report["VO"] = "passed"
    report["Z2"] = "passed" if any(z["kind"] == "Z2" for z in zones) else "n/a"
    # VR: renders outside the mask. Poppler: 0 px. PDFium: 0 px (frozen rule); the count and whether every differing
    # pixel lies on the ink of an unchanged text character is recorded for the informative RT evaluation (report N-3).
    vr_pdfium = []
    for name, fn in (("pdfium", render_pdfium), ("poppler", render_poppler)):
        ra_, da_ = fn(ref), fn(out)
        for pi, (a, b) in enumerate(zip(ra_, da_)):
            if a.shape != b.shape:
                raise Fail("VR", f"{name} page {pi + 1} size")
            h = float(rr.pages[pi].mediabox.height)
            m = mask_for(zones, pi + 1, a.shape, h)
            diff = (a != b) & ~m
            if diff.any():
                if name == "poppler":
                    raise Fail("VR", f"poppler page {pi + 1}: {int(diff.sum())} px outside the mask")
                ink = np.zeros(a.shape, dtype=bool)
                sc = DPI / 72.0
                for ch in rc[pi]:
                    if ch["u"].isspace():
                        continue
                    x0, y0, x1, y1 = ch["box"]
                    ink[max(int((h - y1) * sc) - 2, 0):int(np.ceil((h - y0) * sc)) + 2, max(int(x0 * sc) - 2, 0):int(np.ceil(x1 * sc)) + 2] = True
                vr_pdfium.append({"page": pi + 1, "px": int(diff.sum()), "max_delta": int(np.abs(a.astype(int) - b.astype(int))[diff].max()),
                                  "all_on_unchanged_glyph_ink": bool(not (diff & ~ink).any())})
    if vr_pdfium:
        report["VR"] = {"pdfium_outside_mask": vr_pdfium, "poppler": "0 px"}
    else:
        report["VR"] = "passed"
    _vr_failed = bool(vr_pdfium)

    # VB: non-text painting identical; full-page render without text objects identical
    if non_text_ops(rd) != non_text_ops(rr):
        raise Fail("VB", "non-text operators differ")
    for pi, (a, b) in enumerate(zip(render_pdfium(ref, no_text=True), render_pdfium(out, no_text=True))):
        if a.shape != b.shape or (a != b).any():
            raise Fail("VB", f"page {pi + 1} non-text render differs")
    report["VB"] = "passed"
    # VN: annotations (except E classes)
    if annotations(rd) != annotations(rr):
        raise Fail("VN", "annotations differ")
    report["VN"] = "passed"
    # VE: E classes absent
    info = rd.trailer.get("/Info")
    if info is not None and any(k not in ("/CreationDate", "/ModDate") for k in info.get_object().keys()):
        raise Fail("VE", "descriptive /Info")
    fresh = PdfReader(io.BytesIO(out))  # fresh reader: text extraction mutates cached objects
    objs, reach, all_ids = walk(fresh)
    for o in objs:
        if isinstance(o, (DictionaryObject, StreamObject)) and ("/Metadata" in o or o.get("/Type") == "/Metadata"):
            raise Fail("VE", "metadata present")
        if isinstance(o, DictionaryObject) and o.get("/Subtype") in ("/Text", "/Popup"):
            raise Fail("VE", "comment annotation")
    report["VE"] = "passed"
    # VS: unreachable objects
    unreachable = all_ids - reach
    if unreachable:
        raise Fail("VS", f"{len(unreachable)} unreachable objects")
    report["VS"] = "passed"
    # VK / VX / VP: residues of the selected values everywhere, with the permitted occurrences by position
    sel_values = selected_values or []
    if mode == "protected":
        perm = [o for o in oracle["occurrences"] if o["kind"] == "O"]
        texts_any = show_texts(rd)
        for v in sel_values:
            R = reps(v)
            perm_v = [o for o in perm if o["value"] == v]
            # (1) decoded show operands per page with the current font: only the permitted count
            for label, page, cur, anyf in texts_any:
                allowed = len([o for o in perm_v if o["page"] == page]) if page else 0
                for r in R:
                    n = cur.casefold().count(r.casefold()) if r == r.casefold() else cur.count(r)
                    if n > allowed:
                        raise Fail("VX", f"value in show operands of {label}")
                    if anyf.count(r) > allowed * max(1, len(font_decoders(rd))):
                        raise Fail("VX", f"value decodable with another font in {label}")
            # (2) permitted occurrences exactly at their oracle position (PDFium), and nowhere else
            for pi, chars in enumerate(dc):
                s = "".join(ch["u"] for ch in chars)
                hits = [m.start() for m in re.finditer(re.escape(v), s)]
                pv = [o for o in perm_v if o["page"] == pi + 1]
                if len(hits) != len(pv):
                    raise Fail("VP", f"page {pi + 1} occurrences of a selected value: {len(hits)} vs permitted {len(pv)}")
                for h, o in zip(hits, pv):
                    got = [ch["box"] for ch in chars[h:h + len(v)] if not ch["u"].isspace()]
                    expb = [x["box"] for x in o["pdfium_chars"]]
                    if len(got) != len(expb) or any(max(abs(p - q) for p, q in zip(a, b)) > 0.01 for a, b in zip(got, expb)):
                        raise Fail("VP", "permitted occurrence moved")
            # (3) every string / name in the object graph, decoded (also URL-decoded)
            for o in objs:
                items = o.values() if isinstance(o, (DictionaryObject, StreamObject)) else (o if isinstance(o, ArrayObject) else [o])
                for it in items:
                    for t in _decoded(it):
                        tf = t.casefold()
                        if any(r.casefold() in tf for r in R):
                            raise Fail("VK", "value in an object string")
            # (4) decoded data of every non-content stream and raw bytes of the whole file
            for o in objs:
                if isinstance(o, StreamObject) and o.get("/Subtype") != "/Form" and o.get("/Type") != "/XObject":
                    try:
                        d = o.get_data()
                    except Exception:
                        continue
                    if any(any(r.encode(e, "ignore") in d for e in ("utf-8", "utf-16-be", "utf-16-le", "latin-1") if r.encode(e, "ignore")) for r in R):
                        if o not in [cs for cs in []]:
                            # content streams are reached as page /Contents; they are covered by (1)
                            if not _is_content(fresh, o):
                                raise Fail("VK", "value in a stream")
            for r in R:
                for e in ("utf-8", "utf-16-be", "utf-16-le", "latin-1"):
                    b = r.encode(e, "ignore")
                    if b and len(b) >= 3 and b in out:
                        raise Fail("VK", "value in the raw file bytes")
        report["VK"] = report["VX"] = report["VP"] = "passed"
    else:
        allp = "".join("".join(ch["u"] for ch in p) for p in dc)
        if A06.find_markers(allp):
            raise Fail("VM", "marker left after restoration")
        exp_red = sum(1 for ln in binding["lines"] for s in ln["sels"] if not s["token"])
        if allp.count(A06.REDACTED) != exp_red + "".join("".join(ch["u"] for ch in p) for p in oc).count(A06.REDACTED):
            raise Fail("VM", "redactions not intact")
        report["VM"] = "passed"
    _ = policy_values
    if _vr_failed:
        raise Fail("VR", "pdfium: " + "; ".join(f"page {v['page']} {v['px']} px" for v in report["VR"]["pdfium_outside_mask"]), report)
    return report


def _is_content(reader: PdfReader, stream) -> bool:
    for p in reader.pages:
        cont = p.get("/Contents")
        if cont is None:
            continue
        cont = cont.get_object()
        lst = cont if isinstance(cont, ArrayObject) else [cont]
        for x in lst:
            if x.get_object() is stream:
                return True
    return False
