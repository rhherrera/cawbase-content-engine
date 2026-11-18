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
# Stage 2B (criterion 5): the verifier's own copies of the protocol constants; never read from the transformer or plan.
MIN_SCALE = 0.60
POS_TOL = 0.05  # pt
CAND_FONT = Path(__file__).resolve().parent.parent / "fonts" / "NotoSans-Condensed.ttf"
CAND_PS = "NotoSans-Condensed"
_CAND_ADV: dict | None = None


def _cand_adv() -> dict:
    global _CAND_ADV
    if _CAND_ADV is None:
        from fontTools.ttLib import TTFont
        tt = TTFont(str(CAND_FONT))
        cm, hm, upm = tt.getBestCmap(), tt["hmtx"], tt["head"].unitsPerEm
        _CAND_ADV = {chr(k): hm[g][0] / upm for k, g in cm.items()}
    return _CAND_ADV


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
            page.append({"u": " " if u.isspace() else u, "box": list(b), "gen": gen, "loose": list(tp.get_charbox(k, loose=True)),
                         "size": c.FPDFText_GetFontSize(tp.raw, k)})
        out.append(page)
    return out


def _font_name(tp, k) -> str:
    import ctypes
    n = c.FPDFText_GetFontInfo(tp.raw, k, None, 0, None)
    if n <= 0:
        return ""
    buf = ctypes.create_string_buffer(n)
    c.FPDFText_GetFontInfo(tp.raw, k, buf, n, None)
    return buf.value.decode("utf-8", "replace")


def pdfium_indexed(data: bytes) -> list[list[dict]]:
    """Every PDFium char (index-aligned with the oracle's `i`), with tight and loose boxes, size and font name."""
    doc = pdfium.PdfDocument(data)
    out = []
    for i in range(len(doc)):
        tp = doc[i].get_textpage()
        page = []
        for k in range(tp.count_chars()):
            m = c.FS_MATRIX()
            c.FPDFText_GetMatrix(tp.raw, k, m)
            page.append({"i": k, "u": chr(c.FPDFText_GetUnicode(tp.raw, k)), "gen": bool(c.FPDFText_IsGenerated(tp.raw, k)),
                         "box": list(tp.get_charbox(k)), "loose": list(tp.get_charbox(k, loose=True)),
                         # effective em in page units (Tf size x vertical scale of Tm x CTM)
                         "size": c.FPDFText_GetFontSize(tp.raw, k) * (m.c ** 2 + m.d ** 2) ** 0.5, "font": _font_name(tp, k)})
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
    """Authorized zones, from the ORIGINAL's frozen oracle only (stage 2B, criterion 5). The plan contributes which
    selections it groups in a line and whether it claims Z1 or Z2 (both checked by `check_plan`); it never sets a limit."""
    occs = {o["id"]: o for o in oracle["occurrences"]}
    zs = []
    for ln in plan_lines:
        sels = ln["sels"]
        zz = [occs[s["sid"]]["zone"] for s in sels]
        if ln["kind"] == "Z1":
            for s, z in zip(sels, zz):
                zs.append({"page": ln["page"], "rect": [z["x0"], z["band"][0], z["r_z1"], z["band"][1]], "kind": "Z1", "sids": [s["sid"]]})
        else:
            zs.append({"page": ln["page"], "rect": [min(z["x0"] for z in zz), min(z["band"][0] for z in zz), min(z["r_line"] for z in zz),
                                                     max(z["band"][1] for z in zz)], "kind": "Z2", "sids": [s["sid"] for s in sels]})
    return zs


def check_plan(plan_lines: list[dict], oracle: dict) -> None:
    """VZP: the plan stays inside the authorized zones (never wider), groups every selection of one oracle line in one
    plan line, and a Z2 plan only groups selections of the same line."""
    occs = {o["id"]: o for o in oracle["occurrences"]}
    where: dict[str, int] = {}
    for li, ln in enumerate(plan_lines):
        if ln["kind"] not in ("Z1", "Z2"):
            raise Fail("VZP", "unknown zone kind")
        for s in ln["sels"]:
            o = occs.get(s["sid"])
            if o is None or "zone" not in o or o["page"] != ln["page"] or s["sid"] in where:
                raise Fail("VZP", "selection without an authorized zone")
            where[s["sid"]] = li

    def same_line(a, b):
        za, zb = occs[a]["zone"], occs[b]["zone"]
        return occs[a]["page"] == occs[b]["page"] and abs(za["band"][0] - zb["band"][0]) < 0.5 and abs(za["band"][1] - zb["band"][1]) < 0.5

    ids = sorted(where)
    for x in ids:
        for y in ids:
            if x < y and same_line(x, y) and where[x] != where[y] and "Z2" in (plan_lines[where[x]]["kind"], plan_lines[where[y]]["kind"]):
                raise Fail("VZP", "selections of one line split across plans")
    zones = zones_of(plan_lines, oracle)
    for ln in plan_lines:
        zl = [z for z in zones if z["sids"][0] in {s["sid"] for s in ln["sels"]}]
        if ln["kind"] == "Z2":
            if any(not same_line(ln["sels"][0]["sid"], s["sid"]) for s in ln["sels"]):
                raise Fail("VZP", "Z2 plan over selections of different lines")
            R = zl[0]["rect"]
            if ln["r"] > R[2] + 0.01 or ln["x0"] < R[0] - 0.01:
                raise Fail("VZP", "plan exceeds the authorized zone")
            for s in ln["sels"]:
                if not (R[0] - 0.01 <= s["x_new"] <= R[2]):
                    raise Fail("VZP", "substitute placed outside the authorized zone")
        else:
            for s, z in zip(ln["sels"], zl):
                if not (z["rect"][0] - 0.01 <= s["x_new"] <= z["rect"][2]) or ln["r"] > max(q["rect"][2] for q in zl) + 0.01:
                    raise Fail("VZP", "plan exceeds the authorized zone")


def expected_zone_text(z: dict, ln: dict, orig_chars: list[dict], subs: dict[str, str], oracle: dict, values=None) -> tuple[str, list]:
    """Text expected inside a zone (substitutes, or restored values, in the original order of the line) and, for a
    restored Z2 zone, the ORIGINAL position of each non-selected char. Uses no plan number."""
    occs = {o["id"]: o for o in oracle["occurrences"]}
    if z["kind"] == "Z1":
        sid = z["sids"][0]
        return (values or subs)[sid], []
    sel_boxes = {sid: occs[sid]["pdfium_chars"] for sid in z["sids"]}
    sel_list = [c["box"] for bx in sel_boxes.values() for c in bx]
    items = [("s", min(c["box"][0] for c in bx), sid) for sid, bx in sel_boxes.items()]

    def is_sel(box):
        return any(max(abs(p - q) for p, q in zip(box, sb)) <= 0.01 for sb in sel_list)

    for ch in orig_chars:
        if _in(_center(ch["box"]), z["rect"]) and not is_sel(ch["box"]) and not ch["u"].isspace():
            items.append(("c", ch["box"][0], ch))
    items.sort(key=lambda t: t[1])
    text, exp = [], []
    for kind, x, obj in items:
        if kind == "s":
            text.append((values or subs)[obj])
        else:
            text.append(obj["u"])
            if values is not None:
                exp.append((obj["u"], obj["box"][0], obj["box"]))
    return "".join(text), exp


def _tc_gap(orig_idx: list[dict], idx: list[int]) -> float:
    """Character spacing (Tc) bound for a selection: the gap between its last two glyphs (origin of the last minus the
    advance end of the one before), measured inside the value. The restorer and transformer count Tc in the value's
    advance and PDFium's loose box does not; an inter-word gap is NOT tolerated (stage-2B measurement run 1, CV32)."""
    ink = [orig_idx[k] for k in idx if not orig_idx[k]["u"].isspace()]
    if len(ink) < 2 or abs(ink[-1]["loose"][1] - ink[-2]["loose"][1]) > 0.5:
        return 0.0
    return abs(ink[-1]["loose"][0] - ink[-2]["loose"][2])


def z2_geometry(z: dict, ln: dict, orig_idx: list[dict], der_idx: list[dict], oracle: dict, subs: dict[str, str]) -> dict:
    """Protected Z2 zone: expected geometry derived from the ORIGINAL (PDFium loose boxes = origin and advance), the frozen
    oracle and what the derivative actually draws; the plan's factor is only cross-checked (criterion 5).
    Model (origin coordinates): x' = a1 + f*(x - a1) + sum over substitutes k to the left of (w'_k - f*(e_k - a_k)),
    a single f for the whole line, every glyph keeps its order, Unicode, baseline and height, and its advance scales by f."""
    occs = {o["id"]: o for o in oracle["occurrences"]}
    sels = []
    for sid in z["sids"]:
        idx = sorted(x["i"] for x in occs[sid]["pdfium_chars"])
        first, last = orig_idx[idx[0]], orig_idx[idx[-1]]
        sels.append({"sid": sid, "idx": set(idx), "a": max(occs[sid]["zone"]["x0"], first["loose"][0]), "e": last["loose"][2], "gap": _tc_gap(orig_idx, idx)})
    sels.sort(key=lambda q: q["a"])
    selected = set().union(*(q["idx"] for q in sels))
    rect = z["rect"]
    orig = sorted((ch for ch in orig_idx if not ch["gen"] and not ch["u"].isspace() and ch["i"] not in selected
                   and _in(_center(ch["box"]), rect)), key=lambda q: q["loose"][0])
    items = sorted([("s", q["a"], q) for q in sels] + [("c", ch["loose"][0], ch) for ch in orig], key=lambda t: t[1])
    der = sorted((ch for ch in der_idx if not ch["gen"] and not ch["u"].isspace() and _in(_center(ch["box"]), rect)), key=lambda q: q["box"][0])
    # map the derivative's chars onto the items (VZ already proved the text is the same, in the same order)
    pos, mapped = 0, []
    for kind, _, obj in items:
        if kind == "s":
            sub = "".join(subs[obj["sid"]].split())
            seg = der[pos:pos + len(sub)]
            if "".join(ch["u"] for ch in seg) != sub:
                raise Fail("Z2", f"zone {z['sids']} substitute not where the line order puts it")
            mapped.append(("s", obj, seg))
            pos += len(sub)
        else:
            if pos >= len(der) or der[pos]["u"] != obj["u"]:
                raise Fail("Z2", f"zone {z['sids']} order of the non-selected text")
            mapped.append(("c", obj, der[pos]))
            pos += 1
    # substitute natural widths (frozen candidate font) and measured compression
    adv = _cand_adv()
    ratios = []
    for kind, obj, seg in mapped:
        if kind != "s":
            continue
        if any(CAND_PS not in ch["font"] for ch in seg):
            raise Fail("VL", "substitute font is not the frozen candidate (legibility not measurable by this verifier)")
        nat = sum(adv[ch["u"]] for ch in seg) * seg[0]["size"]
        obj["w"] = seg[-1]["loose"][2] - seg[0]["loose"][0]
        obj["a_der"] = seg[0]["loose"][0]
        ratios.append(obj["w"] / nat)
    # f from consecutive non-selected glyphs (within a segment), else from the substitutes
    num = den = 0.0
    for (k1, o1, d1), (k2, o2, d2) in zip(mapped, mapped[1:]):
        if k1 == "c" and k2 == "c":
            dx, dxp = o2["loose"][0] - o1["loose"][0], d2["loose"][0] - d1["loose"][0]
            num, den = num + dx * dxp, den + dx * dx
    f = num / den if den > 1e-9 else ratios[0]
    if not (MIN_SCALE - 0.002 <= f <= 1.002):
        raise Fail("VL", f"zone {z['sids']} compression {f:.3f} outside [{MIN_SCALE}, 1]")
    if any(abs(r - f) > 0.003 for r in ratios):
        raise Fail("Z2", f"zone {z['sids']} substitute compressed differently from its line")
    if abs(f - ln["f"]) > 0.002:
        raise Fail("Z2", f"zone {z['sids']} plan factor {ln['f']:.4f} differs from the measured {f:.4f}")
    a1 = sels[0]["a"]
    if abs(sels[0]["a_der"] - a1) > POS_TOL:
        raise Fail("Z2", f"zone {z['sids']} substitute does not start at the zone start")
    shift = tol = 0.0
    worst = 0.0
    for kind, obj, d in mapped:
        if kind == "s":
            pred = a1 + f * (obj["a"] - a1) + shift
            if abs(obj["a_der"] - pred) > POS_TOL + tol:
                raise Fail("Z2", f"zone {z['sids']} substitute not at its expected position")
            shift += obj["w"] - f * (obj["e"] - obj["a"])
            tol += f * obj["gap"]
        else:
            pred = a1 + f * (obj["loose"][0] - a1) + shift
            err = abs(d["loose"][0] - pred)
            worst = max(worst, err)
            if err > POS_TOL + tol:
                raise Fail("Z2", f"zone {z['sids']} char '{obj['u']}' not at its expected position")
            w0, w1 = obj["loose"][2] - obj["loose"][0], d["loose"][2] - d["loose"][0]
            if abs(w1 - f * w0) > POS_TOL:
                raise Fail("Z2", f"zone {z['sids']} char '{obj['u']}' advance not compressed by the line factor")
            if abs(d["box"][1] - obj["box"][1]) > POS_TOL or abs(d["box"][3] - obj["box"][3]) > POS_TOL:
                raise Fail("Z2", "height/baseline")
    return {"f_measured": round(f, 4), "substitute_ratios": [round(r, 4) for r in ratios], "max_residual_pt": round(worst, 4),
            "moved_chars": sum(1 for k, _, _ in mapped if k == "c")}


def restored_excess(z: dict, ln: dict, orig_idx: list[dict], oracle: dict) -> tuple[list[tuple[float, float]], float]:
    """Restored Z2 line (criterion 2, RZ): each redaction returns to the redacted value's original start, compressed to
    the value's original span when wider, never below MIN_SCALE; the excess at MIN_SCALE shifts what follows. Derived
    from the original (loose boxes), the oracle and the frozen candidate font, never from the plan."""
    occs = {o["id"]: o for o in oracle["occurrences"]}
    adv = _cand_adv()
    w1 = sum(adv[ch] for ch in A06.REDACTED)
    out, tol = [], 0.0
    for s in ln["sels"]:
        if s["sid"] not in z["sids"] or s["token"]:
            continue
        pcs = occs[s["sid"]]["pdfium_chars"]
        idx = sorted(x["i"] for x in pcs)
        first, last = orig_idx[idx[0]], orig_idx[idx[-1]]
        a = max(occs[s["sid"]]["zone"]["x0"], first["loose"][0])
        span = last["loose"][2] - a
        w = w1 * first["size"]
        ex = MIN_SCALE * w - span if span / w < MIN_SCALE else 0.0
        out.append((a, ex))
        if ex:
            tol += _tc_gap(orig_idx, idx)
    return out, tol


def z1_legibility(z: dict, der_idx: list[dict], subs: dict[str, str]) -> float:
    sub = "".join(subs[z["sids"][0]].split())
    inside = sorted((ch for ch in der_idx if not ch["gen"] and not ch["u"].isspace() and _in(_center(ch["box"]), z["rect"])), key=lambda q: q["box"][0])
    if "".join(ch["u"] for ch in inside) != sub:
        raise Fail("VZ", f"zone {z['sids']} text")
    if any(CAND_PS not in ch["font"] for ch in inside):
        raise Fail("VL", "substitute font is not the frozen candidate (legibility not measurable by this verifier)")
    adv = _cand_adv()
    r = (inside[-1]["loose"][2] - inside[0]["loose"][0]) / (sum(adv[ch["u"]] for ch in inside) * inside[0]["size"])
    if not (MIN_SCALE - 0.002 <= r <= 1.002):
        raise Fail("VL", f"zone {z['sids']} compression {r:.3f} outside [{MIN_SCALE}, 1]")
    if inside[0]["loose"][0] < z["rect"][0] - POS_TOL:
        raise Fail("VO", f"zone {z['sids']} substitute starts before the zone")
    return r


def marked_content(reader: PdfReader) -> list:
    """Marked-content property lists (inline or named) of every content stream, as strings, in order."""
    out = []
    for label, cs, res, page in content_streams(reader):
        props = (res.get_object().get("/Properties") if res is not None else None) or {}
        seq = []
        for operands, op in cs.operations:
            if op == b"BDC" and len(operands) > 1:
                p = operands[1]
                if isinstance(p, NameObject):
                    p = props.get(p)
                    p = p.get_object() if p is not None else None
                if isinstance(p, DictionaryObject):
                    seq.append((str(operands[0]), tuple(sorted((str(k), "|".join(_decoded(v))) for k, v in p.items()))))
        out.append((label, seq))
    return out


def rt_diagnostic(a: np.ndarray, b: np.ndarray, diff: np.ndarray, ref_chars: list[dict], out_chars: list[dict], h: float) -> dict:
    """Diagnostic only (criterion 4; never a success). Is every differing pixel outside the mask explained by a whole
    glyph raster displaced by at most 1 px? For each non-space char whose extracted box did not change (+-0.01 pt), the
    shift s in {-1,0,1}^2 that best reproduces its region (tight box + 2 px) is chosen; a differing pixel is explained
    when it lies in such a region and |out - ref shifted by s| <= 32 there. Any unexplained pixel is reported."""
    sc = DPI / 72.0
    A, B = a.astype(int), b.astype(int)
    explained = np.zeros(a.shape, dtype=bool)
    region = np.zeros(a.shape, dtype=bool)
    shifts: dict[str, int] = {}
    H, W = a.shape
    for ch in ref_chars:
        if ch["u"].isspace() or not any(o["u"] == ch["u"] and max(abs(p - q) for p, q in zip(o["box"], ch["box"])) <= 0.01 for o in out_chars):
            continue
        x0, y0, x1, y1 = ch["box"]
        # tight box + 2 px: a raster displaced by <= 1 px plus its <= 1 px anti-aliasing fringe
        r0, r1 = max(int((h - y1) * sc) - 2, 1), min(int(np.ceil((h - y0) * sc)) + 2, H - 1)
        c0, c1 = max(int(x0 * sc) - 2, 1), min(int(np.ceil(x1 * sc)) + 2, W - 1)
        if r1 <= r0 or c1 <= c0 or not diff[r0:r1, c0:c1].any():
            continue
        best = min(((int(np.abs(B[r0:r1, c0:c1] - A[r0 - dy:r1 - dy, c0 - dx:c1 - dx]).max()), dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)))
        _, dx, dy = best
        explained[r0:r1, c0:c1] |= np.abs(B[r0:r1, c0:c1] - A[r0 - dy:r1 - dy, c0 - dx:c1 - dx]) <= 32
        region[r0:r1, c0:c1] = True
        key = f"{dx},{dy}"
        shifts[key] = shifts.get(key, 0) + 1
    # where glyph regions touch, a pixel may belong to two glyphs shifted differently: it is also explained when it
    # equals (<= 32) the reference at some position <= 1 px away, inside an unchanged glyph's region
    pix = np.zeros(a.shape, dtype=bool)
    P = np.pad(A, 1, mode="edge")
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            pix |= np.abs(B - P[1 - dy:1 - dy + H, 1 - dx:1 - dx + W]) <= 32
    unexplained_glyph = diff & ~explained
    unexplained = diff & ~(explained | (pix & region))
    return {"rt_all_explained_by_glyph_shift_le_1px": bool(not unexplained.any()), "rt_px_unexplained": int(unexplained.sum()),
            "rt_px_unexplained_by_whole_glyph_shift": int(unexplained_glyph.sum()), "rt_glyph_shifts": shifts}


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
    # VZP: the plan never widens an authorized zone (zones come from the original's frozen oracle; criterion 5)
    check_plan(binding["lines"], oracle)
    report["VZP"] = "passed"
    zones = zones_of(binding["lines"], oracle)
    lines_by_sid = {s["sid"]: ln for ln in binding["lines"] for s in ln["sels"]}
    subs = {s["sid"]: s["substitute"] for ln in binding["lines"] for s in ln["sels"]}
    oc = pdfium_chars(original)
    rc = pdfium_chars(ref)
    dc = pdfium_chars(out)
    oc_idx = pdfium_indexed(original)
    dc_idx = pdfium_indexed(out)
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
    # VZ / VO / VL / Z2 geometry
    geometry = []
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
        # right limit: the zone's; in a restored output, also wherever the original's own ink already reached
        r_lim = z["rect"][2]
        if mode == "restored":
            r_lim = max([r_lim] + [ch["box"][2] for ch in oc[z["page"] - 1] if not ch["u"].isspace() and _in(_center(ch["box"]), z["rect"])])
        for ch in inside:
            if ch["u"].isspace():
                continue
            b, zr = ch["box"], z["rect"]
            if b[2] > r_lim + 0.25 or b[0] < zr[0] - 0.25 or b[1] < zr[1] - 0.25 or b[3] > zr[3] + 0.25:
                raise Fail("VO", f"zone {z['sids']} overflow")
            if ch["u"] == "\ufffd" or ord(ch["u"]) == 0:
                raise Fail("VZ", "notdef")
        if mode == "protected":
            if z["kind"] == "Z2":
                geometry.append({"sids": z["sids"], "kind": "Z2", **z2_geometry(z, ln, oc_idx[z["page"] - 1], dc_idx[z["page"] - 1], oracle, subs)})
            else:
                geometry.append({"sids": z["sids"], "kind": "Z1", "substitute_ratio": round(z1_legibility(z, dc_idx[z["page"] - 1], subs), 4)})
        shifts, stol = (restored_excess(z, ln, oc_idx[z["page"] - 1], oracle) if exp_pos else ([], 0.0))
        if shifts:
            geometry.append({"sids": z["sids"], "kind": "Z2-restored", "redaction_excess_pt": [round(e, 3) for _, e in shifts]})
        for u, xl, obox in exp_pos:  # restored Z2: every non-selected char back at its ORIGINAL position (+ RZ excess)
            xl = xl + sum(e for a, e in shifts if a < xl)
            cand = [ch for ch in inside if ch["u"] == u and abs(ch["box"][0] - xl) <= POS_TOL + (stol if any(e for _, e in shifts) else 0.0)]
            if not cand:
                raise Fail("Z2", f"zone {z['sids']} char '{u}' not at its original position")
            if abs(cand[0]["box"][1] - obox[1]) > POS_TOL or abs(cand[0]["box"][3] - obox[3]) > POS_TOL:
                raise Fail("Z2", "height/baseline")
        if mode == "protected":
            # marker grammar with whitespace kept (real or PDFium-generated): a gap inside a marker breaks it
            spaced = "".join(ch["u"] for ch in dc[z["page"] - 1] if _in(_center(ch["box"]), z["rect"]))  # PDFium reading order
            for s in ln["sels"]:
                if s["sid"] in z["sids"] and s["substitute"] not in spaced:
                    raise Fail("VZ", "substitute grammar (exact, whitespace included)")
            if A06.scan(got).altered:
                raise Fail("VZ", "altered marker")
    report["geometry"] = geometry
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
                vr_pdfium.append({"page": pi + 1, "px": int(diff.sum()), "max_delta": int(np.abs(a.astype(int) - b.astype(int))[diff].max()),
                                  **rt_diagnostic(a, b, diff, rc[pi], dc[pi], h)})
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
    # marked-content properties (/ActualText, /Alt, /E, MCIDs) unchanged: an /ActualText span is never edited (criterion 3)
    if marked_content(rd) != marked_content(rr):
        raise Fail("VB", "marked-content properties differ (/ActualText, /Alt, /E or MCID)")
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
            # (3b) marked-content properties in content streams (inline /ActualText, /Alt, /E): criterion 3
            for label, seq in marked_content(rd):
                for tag, props in seq:
                    if any(any(r.casefold() in v.casefold() for r in R) for _, v in props):
                        raise Fail("VK", f"value in marked-content properties of {label}")
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
