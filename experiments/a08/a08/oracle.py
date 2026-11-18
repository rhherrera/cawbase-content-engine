"""Geometric oracle and safe zones (protocol §6, §7, §0.2-§0.4), built ONLY from the original PDF with PDFium and
Poppler, never with the engine's extractor or the candidate pipeline. Output is frozen before the candidate runs.

    .venv/bin/python -m a08.oracle <case_id> <pdf> <template> <out.json>
"""

from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as c

from .manifest import load, permitted, selections, sha256_file

WS = re.compile(r"\s+")
ZONE_MARGIN_EM = 0.3


def page_chars(page) -> list[dict]:
    tp = page.get_textpage()
    out = []
    for i in range(tp.count_chars()):
        u = chr(c.FPDFText_GetUnicode(tp.raw, i))
        l, b, r, t = tp.get_charbox(i)
        out.append({"i": i, "u": u, "box": [l, b, r, t], "gen": bool(c.FPDFText_IsGenerated(tp.raw, i)),
                    "size": c.FPDFText_GetFontSize(tp.raw, i)})
    tp.close()
    return out


def painted_objects(page) -> list[dict]:
    """Paths and images that paint something (PDFium page objects, recursing into Form XObjects)."""
    out = []
    for o in page.get_objects(max_depth=32):
        if o.type == c.FPDF_PAGEOBJ_PATH:
            fill, stroke = ctypes.c_int(0), ctypes.c_int(0)
            c.FPDFPath_GetDrawMode(o.raw, ctypes.byref(fill), ctypes.byref(stroke))
            if not fill.value and not stroke.value:
                continue
            kind = "path"
        elif o.type == c.FPDF_PAGEOBJ_IMAGE:
            kind = "image"
        elif o.type == c.FPDF_PAGEOBJ_SHADING:
            kind = "shading"
        else:
            continue
        l, b, r, t = o.get_bounds()
        out.append({"kind": kind, "box": [l, b, r, t]})
    return out


def _norm(chars: list[dict]) -> tuple[str, list[int]]:
    """Page text with whitespace runs collapsed to one space, plus index map to char positions."""
    s, idx = [], []
    prev_ws = False
    for k, ch in enumerate(chars):
        ws = ch["u"].isspace() or ch["u"] in "\r\n\x00"
        if ws:
            if not prev_ws:
                s.append(" ")
                idx.append(k)
            prev_ws = True
        else:
            s.append(ch["u"])
            idx.append(k)
            prev_ws = False
    return "".join(s), idx


def _ctx(x: str) -> str:
    return WS.sub(" ", x).strip()


def find_triple(chars: list[dict], left: str, value: str, right: str) -> list[list[int]]:
    s, idx = _norm(chars)
    v = WS.sub(" ", value)
    pat = (re.escape(_ctx(left)) + r" ?" if left else "") + "(" + re.escape(v) + ")" + (r" ?" + re.escape(_ctx(right)) if right else "")
    out = []
    for m in re.finditer(pat, s):
        a, b = m.start(1), m.end(1)
        ks = [idx[j] for j in range(a, b)]
        out.append([k for k in ks if not (chars[k]["u"].isspace() or chars[k]["u"] in "\r\n")] if True else ks)
    return out


def poppler_words(pdf: Path, pno: int, height: float) -> list[dict]:
    xml = subprocess.run(["pdftotext", "-bbox-layout", "-f", str(pno), "-l", str(pno), str(pdf), "-"],
                         capture_output=True, check=True).stdout.decode("utf-8")
    xml = re.sub(r"<!DOCTYPE[^>]*>", "", xml)
    root = ET.fromstring(xml)
    ns = {"h": "http://www.w3.org/1999/xhtml"}
    words, blocks = [], []
    for bi, blk in enumerate(root.iter("{http://www.w3.org/1999/xhtml}block")):
        bb = [float(blk.get(k)) for k in ("xMin", "yMin", "xMax", "yMax")]
        blocks.append([bb[0], height - bb[3], bb[2], height - bb[1]])
        for w in blk.iter("{http://www.w3.org/1999/xhtml}word"):
            x0, y0, x1, y1 = (float(w.get(k)) for k in ("xMin", "yMin", "xMax", "yMax"))
            words.append({"t": w.text or "", "box": [x0, height - y1, x1, height - y0], "block": bi})
    _ = ns
    return words, blocks


def _center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _inside(pt, box, tol):
    return box[0] - tol <= pt[0] <= box[2] + tol and box[1] - tol <= pt[1] <= box[3] + tol


def poppler_agree(words, value_chars) -> tuple[bool, str, list]:
    """Exactly one consecutive Poppler word sequence whose union box contains every PDFium value-char center."""
    centers = [_center(ch["box"]) for ch in value_chars]
    n = len(words)
    hits = []
    for a in range(n):
        for b in range(a + 1, min(n, a + 8) + 1):
            seq = words[a:b]
            ub = [min(w["box"][0] for w in seq), min(w["box"][1] for w in seq), max(w["box"][2] for w in seq), max(w["box"][3] for w in seq)]
            if all(_inside(p, ub, 0.5) for p in centers) and all(any(_inside(p, w["box"], 0.5) for p in centers) for w in seq):
                hits.append((a, b, " ".join(w["t"] for w in seq)))
    minimal = [h for h in hits if not any(o != h and o[0] >= h[0] and o[1] <= h[1] for o in hits)]
    if len(minimal) != 1:
        return False, "", minimal
    return True, minimal[0][2], minimal


def line_of(chars, value_chars):
    """Chars on the same line band as the value (vertical center within half the value height)."""
    vb = [min(ch["box"][1] for ch in value_chars), max(ch["box"][3] for ch in value_chars)]
    cy, h = (vb[0] + vb[1]) / 2, (vb[1] - vb[0])
    return [ch for ch in chars if not ch["gen"] and ch["box"][2] > ch["box"][0] and abs(_center(ch["box"])[1] - cy) < h / 2]


def zone_for(chars, objs, page_box, value_chars, exclude: set[int]) -> dict:
    """Z1 and line limit for Z2 (protocol §7.1 + §0.3/§0.4), from the original only."""
    line = line_of(chars, value_chars)
    x0 = min(ch["box"][0] for ch in value_chars)
    x_end = max(ch["box"][2] for ch in value_chars)
    band = [min(ch["box"][1] for ch in line), max(ch["box"][3] for ch in line)]
    # clip the band so it does not overlap boxes of other lines
    line_ids = {ch["i"] for ch in line}
    for ch in chars:
        if ch["i"] in line_ids or ch["gen"] or ch["box"][2] <= ch["box"][0]:
            continue
        b = ch["box"]
        if b[0] < page_box[2] and b[2] > x0 and b[1] < band[1] and b[3] > band[0]:
            cyo = _center(b)[1]
            if cyo > (band[0] + band[1]) / 2:
                band[1] = min(band[1], b[1])
            else:
                band[0] = max(band[0], b[3])
    size = max(ch["size"] for ch in value_chars)
    margin = ZONE_MARGIN_EM * size
    # (a) next glyph not in this selection
    nxt = [ch["box"][0] for ch in line if ch["i"] not in exclude and ch["box"][0] >= x_end - 0.01 and not ch["u"].isspace()]
    ra = min(nxt) if nxt else float("inf")
    # (b) painted paths/images crossing the band
    rb = float("inf")
    for o in objs:
        b = o["box"]
        if b[1] >= band[1] or b[3] <= band[0]:
            continue
        if b[0] > x0 + 0.01:
            rb = min(rb, b[0])
        elif b[2] > x0 + 0.01:
            rb = min(rb, b[2])
    # (c) text area right edge, (d) crop box
    rc = max(ch["box"][2] for ch in chars if not ch["gen"] and not ch["u"].isspace() and ch["box"][2] > ch["box"][0])
    rd = page_box[2]
    r1 = min(ra, rb, rc, rd) - margin
    rline = min(rb, rc, rd) - margin
    return {"x0": x0, "band": band, "r_z1": max(r1, x_end), "r_line": max(rline, x_end), "font_size": size,
            "value_right": x_end, "limits": {"next_glyph": ra, "painted": rb, "text_area": rc, "crop": rd}}


def build(case_id: str, pdf_path: Path, template: str, custom: tuple[list, list] | None = None) -> dict:
    """custom = (selected items, not-selected items) for controls; items carry id, value, page, contexts."""
    m = load()
    doc = pdfium.PdfDocument(str(pdf_path))
    out = {"case_id": case_id, "pdf_sha256": sha256_file(pdf_path), "template": template, "pages": len(doc),
           "tools": {"pypdfium2": pdfium.version.PYPDFIUM_INFO.version if hasattr(pdfium.version.PYPDFIUM_INFO, "version") else str(pdfium.version.PYPDFIUM_INFO),
                     "pdfium": str(pdfium.version.PDFIUM_INFO),
                     "poppler": subprocess.run(["pdftotext", "-v"], capture_output=True, text=True).stderr.splitlines()[0]},
           "occurrences": [], "status": "ok"}
    cache = {}
    groups = (("S", custom[0]), ("O", custom[1])) if custom else (("S", selections(m, template)), ("O", permitted(m, template)))
    for kind, items in groups:
        for it in items:
            pno = it["page"]
            rec = {"id": it["id"], "kind": kind, "value_id": it.get("value_id", ""), "value": it["value"], "page": pno}
            if pno > len(doc):
                rec.update(status="oracle_unavailable", reason="page_missing")
                out["occurrences"].append(rec)
                continue
            if pno not in cache:
                page = doc[pno - 1]
                ch = page_chars(page)
                cb = page.get_cropbox()
                words, blocks = poppler_words(pdf_path, pno, page.get_size()[1])
                cache[pno] = (ch, painted_objects(page), cb, words, blocks)
            ch, objs, cb, words, blocks = cache[pno]
            found = find_triple(ch, it["left_context"], it["value"], it["right_context"])
            if len(found) != 1:
                rec.update(status="oracle_unavailable", reason=f"pdfium_matches={len(found)}")
                out["occurrences"].append(rec)
                continue
            vchars = [ch[k] for k in found[0]]
            ok, ptext, hits = poppler_agree(words, vchars)
            rec["pdfium_chars"] = [{"i": x["i"], "u": x["u"], "box": [round(v, 3) for v in x["box"]], "size": round(x["size"], 3)} for x in vchars]
            rec["poppler_text"] = ptext
            lines = {round(_center(x["box"])[1] / max(x["box"][3] - x["box"][1], 1.0)) for x in vchars}
            ys = [_center(x["box"])[1] for x in vchars]
            hmax = max(x["box"][3] - x["box"][1] for x in vchars)
            split = (max(ys) - min(ys)) > hmax / 2
            rec["split_across_lines"] = split
            _ = lines
            if not ok:
                rec.update(status="oracle_unavailable", reason=f"poppler_matches={len(hits)}")
                out["occurrences"].append(rec)
                continue
            rec["status"] = "ok"
            if kind == "S" and not split:
                rec["zone"] = zone_for(ch, objs, cb, vchars, {x["i"] for x in vchars})
            out["occurrences"].append(rec)
    if any(o["status"] != "ok" for o in out["occurrences"]):
        out["status"] = "oracle_unavailable"
    return out


def main(case_id: str, pdf: str, template: str, out: str) -> None:
    o = build(case_id, Path(pdf), template)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(o, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(case_id, o["status"], [(x["id"], x["status"], x.get("split_across_lines")) for x in o["occurrences"]])


if __name__ == "__main__":
    main(*sys.argv[1:5])


# ---------------------------------------------------------------- informative variant TB (NOT counted; report option)
def informative_tb(case_id: str, pdf_path: Path, frozen: dict) -> dict:
    """Same oracle and zones, but each zone band is the TYPOGRAPHIC line box: union of PDFium loose char boxes of the
    line (font ascent/descent), clipped so it never covers ink of another line. Option for the user, not the protocol."""
    import copy
    doc = pdfium.PdfDocument(str(pdf_path))
    out = copy.deepcopy(frozen)
    out["variant"] = "TB (banda tipográfica; informativa, fuera del cómputo)"
    for o in out["occurrences"]:
        if o.get("kind") != "S" or "zone" not in o:
            continue
        page = doc[o["page"] - 1]
        tp = page.get_textpage()
        chars = page_chars(page)
        loose = {i: tp.get_charbox(i, loose=True) for i in range(tp.count_chars())}
        vchars = [ch for ch in chars if ch["i"] in {x["i"] for x in o["pdfium_chars"]}]
        line = line_of(chars, vchars)
        ids = {ch["i"] for ch in line}
        band = [min(loose[ch["i"]][1] for ch in line), max(loose[ch["i"]][3] for ch in line)]
        x0, r = o["zone"]["x0"], o["zone"]["r_line"]
        for ch in chars:
            if ch["i"] in ids or ch["gen"] or ch["box"][2] <= ch["box"][0]:
                continue
            b = ch["box"]
            if b[0] < r and b[2] > x0 and b[1] < band[1] and b[3] > band[0]:
                if (b[1] + b[3]) / 2 > (band[0] + band[1]) / 2:
                    band[1] = min(band[1], b[1])
                else:
                    band[0] = max(band[0], b[3])
        o["zone"]["band"] = band
        tp.close()
    return out


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--tb":
    pass
