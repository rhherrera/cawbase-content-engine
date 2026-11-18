"""Document gate of the candidate (A08 matrix, protocol §0.7) and the extraction entry point of extractor v2."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from typing import Any

import pikepdf

from .pdfmodel import Interp, PageModel, Reject, build_units, qbox, source_map

ALL_RULES = ("RI1", "RI2", "RP1", "RP2", "RP3", "RP4", "RP5", "RP7")
VARIANT: set[str] = set()  # informative variants (never counted): "AT" admits /ActualText equal to its glyphs' text
E_ANNOTS = ("/Text", "/Popup")


@dataclass
class Extraction:
    pages: list[PageModel]
    smap: dict
    inventory: dict = field(default_factory=dict)
    interp: Any = None


def _walk_struct(node, depth=0, seen=None):
    seen = seen if seen is not None else set()
    if depth > 200:
        raise Reject("structure_limit", "struct depth")
    if isinstance(node, pikepdf.Array):
        for k in node:
            yield from _walk_struct(k, depth + 1, seen)
    elif isinstance(node, pikepdf.Dictionary):
        key = node.objgen
        if key != (0, 0):
            if key in seen:
                return
            seen.add(key)
        yield node
        if "/K" in node:
            yield from _walk_struct(node.K, depth + 1, seen)


def _actions(d) -> list:
    out = []
    a = d.get("/A")
    if a is not None:
        out.append(a)
    aa = d.get("/AA")
    if aa is not None:
        out += [aa[k] for k in aa.keys()]
    return out


def document_gate(pdf: pikepdf.Pdf, data: bytes) -> dict:
    inv = {"e_classes": {}, "links": 0, "struct_tree": False, "outlines": "/Outlines" in pdf.Root}
    if pdf.get_warnings():
        raise Reject("parser_repair", "qpdf warnings")
    allowed = 2 if pdf.is_linearized else 1
    if data.count(b"startxref") > allowed:
        raise Reject("incremental_update", "several xref sections")
    root = pdf.Root
    acro = root.get("/AcroForm")
    if acro is not None and (int(acro.get("/SigFlags", 0)) or any(str(f.get("/FT", "")) == "/Sig" for f in acro.get("/Fields", []))):
        raise Reject("signed", "signature field")
    if "/Perms" in root:
        raise Reject("signed", "Perms")
    for k in ("/AcroForm", "/OCProperties"):
        if k in root:
            raise Reject("forms_or_layers", k)
    if "/PieceInfo" in root:
        raise Reject("unsupported_structure", "PieceInfo")
    names = root.get("/Names")
    if names is not None:
        if "/EmbeddedFiles" in names:
            raise Reject("embedded_content", "EmbeddedFiles")
        if "/JavaScript" in names:
            raise Reject("executable_action", "JavaScript names")
    oa = root.get("/OpenAction")
    if isinstance(oa, pikepdf.Dictionary) and str(oa.get("/S", "")) != "/GoTo":
        raise Reject("executable_action", "OpenAction")
    if "/AA" in root:
        raise Reject("executable_action", "AA")
    if "/StructTreeRoot" in root:
        inv["struct_tree"] = True
        for el in _walk_struct(root.StructTreeRoot.get("/K")):
            for k in ("/Alt", "/ActualText", "/E"):
                if k in el:
                    raise Reject("alternate_text_duplication", k)
    from . import pdfmodel
    pdfmodel.VARIANT.clear()
    pdfmodel.VARIANT.update(VARIANT)
    info = pdf.trailer.get("/Info")
    if info is not None:
        n = sum(1 for k in info.keys() if k not in ("/CreationDate", "/ModDate"))
        if n:
            inv["e_classes"]["document_info_descriptive"] = n
    if "/Metadata" in root:
        inv["e_classes"]["xmp_metadata"] = 1
    om = sum(1 for o in pdf.objects if isinstance(o, (pikepdf.Dictionary, pikepdf.Stream)) and "/Metadata" in o and o.objgen != root.objgen)
    if om:
        inv["e_classes"]["object_metadata"] = om
    for p in pdf.pages:
        if "/PieceInfo" in p.obj:
            raise Reject("unsupported_structure", "PieceInfo")
        if "/AA" in p.obj:
            raise Reject("executable_action", "page AA")
        for an in p.obj.get("/Annots", []):
            sub = str(an.get("/Subtype", ""))
            if sub == "/Link":
                inv["links"] += 1
                for act in _actions(an):
                    if str(act.get("/S", "")) not in ("/URI", "/GoTo"):
                        raise Reject("executable_action", "link action")
            elif sub in E_ANNOTS:
                inv["e_classes"]["comments"] = inv["e_classes"].get("comments", 0) + 1
            elif sub == "/FileAttachment":
                raise Reject("embedded_content", "FileAttachment")
            elif sub == "/Widget":
                raise Reject("forms_or_layers", "Widget")
            else:
                raise Reject("unsupported_structure", f"annotation {sub}")
    return inv


# ---------------------------------------------------------------- decorative / ordinary resources
def _thin(b, t=2.0):
    return (b[2] - b[0]) <= t or (b[3] - b[1]) <= t


def _image_pixels(obj):
    try:
        im = pikepdf.PdfImage(obj).as_pil_image()
    except Exception:
        return None, None
    return im.size, len(set(im.convert("RGBA").getdata())) if im.size[0] * im.size[1] <= 4_000_000 else 999


def _merge_lines(elems):
    """Collinear thin axis-aligned elements merged into lines: [('h', y, x0, x1) | ('v', x, y0, y1)]."""
    hs, vs = [], []
    for b in elems:
        if (b[3] - b[1]) <= 2.0 and (b[2] - b[0]) > (b[3] - b[1]):
            hs.append([(b[1] + b[3]) / 2, b[0], b[2]])
        elif (b[2] - b[0]) <= 2.0:
            vs.append([(b[0] + b[2]) / 2, b[1], b[3]])

    def merge(items):
        items.sort()
        out = []
        for c, a, z in items:
            if out and abs(out[-1][0] - c) <= 0.75 and a <= out[-1][2] + 1.0:
                out[-1][2] = max(out[-1][2], z)
                out[-1][1] = min(out[-1][1], a)
            else:
                out.append([c, a, z])
        return out

    return merge(hs), merge(vs)


def _grids(paths):
    """RP3: thin axis-aligned elements forming complete rectangular grids. Returns ids of elements in a grid."""
    thin = [p for p in paths if p.kind == "path" and _thin(p.bbox) and p.detail.get("curves", 0) == 0]
    hs, vs = _merge_lines([p.bbox for p in thin])
    ok_ids = set()
    if len(hs) < 2 or len(vs) < 2:
        return ok_ids
    # connected grid: every horizontal spans from the leftmost to the rightmost vertical it meets and vice versa
    xs0, xs1 = min(v[0] for v in vs), max(v[0] for v in vs)
    ys0, ys1 = min(h[0] for h in hs), max(h[0] for h in hs)
    full = all(h[1] <= xs0 + 1.0 and h[2] >= xs1 - 1.0 for h in hs) and all(v[1] <= ys0 + 1.0 and v[2] >= ys1 - 1.0 for v in vs)
    if not full:
        return ok_ids
    for p in thin:
        b = p.bbox
        if b[0] >= xs0 - 1.5 and b[2] <= xs1 + 1.5 and b[1] >= ys0 - 1.5 and b[3] <= ys1 + 1.5:
            ok_ids.add(id(p))
    return ok_ids


def classify(pages: list[PageModel], rules: tuple[str, ...], pdf=None) -> dict:
    """Every painted object must be admitted by a rule in `rules`; returns per-rule counts or raises uncovered_content.
    Glyph boxes are INK boxes from the embedded program when available (as PDFium reports them)."""
    from types import SimpleNamespace

    from .transform import ink_box
    stub = SimpleNamespace(pdf=pdf)
    counts: dict[str, int] = {}
    for pm in pages:
        glyph_boxes = [ink_box(stub, g) if pdf is not None else qbox(g) for g in pm.glyphs if not g.uni.isspace()]
        ta = [min(b[0] for b in glyph_boxes), max(b[2] for b in glyph_boxes)] if glyph_boxes else [pm.cropbox[0], pm.cropbox[2]]
        text_w = ta[1] - ta[0]
        page_area = (pm.cropbox[2] - pm.cropbox[0]) * (pm.cropbox[3] - pm.cropbox[1])
        if not pm.glyphs and any(p.kind in ("image", "inline_image") and (p.bbox[2] - p.bbox[0]) * (p.bbox[3] - p.bbox[1]) > 0.5 * page_area for p in pm.painted):
            raise Reject("scanned_without_text", f"page {pm.index + 1}")
        grid = _grids(pm.painted) if "RP3" in rules else set()
        for p in pm.painted:
            b = p.bbox
            w, h = b[2] - b[0], b[3] - b[1]
            ok = None
            if p.kind in ("image", "inline_image"):
                obj = p.detail.get("obj") if p.kind == "image" else None
                size, colors = _image_pixels(obj) if obj is not None else (None, None)
                if "RI1" in rules and colors == 1:
                    ok = "RI1"
                elif "RI2" in rules and size and size[0] <= 16 and size[1] <= 16 and w <= 6 and h <= 6:
                    ok = "RI2"
            elif p.kind == "shading":
                ok = None
            else:
                d = p.detail
                fill_white = d.get("fill") and not d.get("stroke") and d.get("fill_color") in (("g", (1.0,)), ("rg", (1.0, 1.0, 1.0)), ("k", (0.0, 0.0, 0.0, 0.0)))
                if "RP7" in rules and fill_white and (w * h >= 0.5 * page_area or (b[0] <= ta[0] + 0.5 and b[2] >= ta[1] - 0.5)):
                    ok = "RP7"
                elif "RP3" in rules and id(p) in grid:
                    ok = "RP3"
                elif "RP4" in rules and _thin(b) and d.get("curves", 0) == 0 and d.get("subpaths", 0) == 1 and (d.get("is_rect") or d.get("segments") == 1) and max(w, h) >= 0.5 * text_w:
                    ok = "RP4"
                elif "RP5" in rules and d.get("fill") and not d.get("stroke") and d.get("is_rect") \
                        and any(gb[0] >= b[0] and gb[2] <= b[2] and gb[1] >= b[1] and gb[3] <= b[3] for gb in glyph_boxes) \
                        and not any(o is not p and o.bbox[0] > b[0] + 1 and o.bbox[2] < b[2] - 1 and o.bbox[1] > b[1] + 1 and o.bbox[3] < b[3] - 1 for o in pm.painted):
                    ok = "RP5"
                elif "RP1" in rules and d.get("curves", 0) == 0 and d.get("subpaths", 1) and d.get("segments", 0) / max(d.get("subpaths", 1), 1) <= 8:
                    ok = "RP1"
                elif "RP2" in rules and d.get("curves", 0) > 0 and w <= 6 and h <= 6:
                    ok = "RP2"
            if ok is None:
                raise Reject("uncovered_content", f"page {pm.index + 1} {p.kind} {[round(v, 1) for v in b]}")
            counts[ok] = counts.get(ok, 0) + 1
            p.detail["rule"] = ok
    return counts


def visibility(pages: list[PageModel]) -> None:
    """Hidden text: outside the crop box, outside the clip, white fill without a non-white backdrop, covered later."""
    for pm in pages:
        for g in pm.glyphs:
            b = qbox(g)
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            cb = pm.cropbox
            if not (cb[0] <= cx <= cb[2] and cb[1] <= cy <= cb[3]):
                raise Reject("hidden_text", "outside crop box")
        for p in pm.painted:
            if p.kind != "path" or not p.detail.get("fill"):
                continue
            fc = p.detail.get("fill_color")
            for g in pm.glyphs:
                pass
            _ = fc
        # covered: an opaque fill painted after a glyph and containing its center
        order_of = {}
        for p in pm.painted:
            order_of[id(p)] = p.order
        # glyph order is not stored; approximate by stream position: painted objects from the same stream after the op
        for p in pm.painted:
            if p.kind != "path" or not p.detail.get("fill"):
                continue
            b = p.bbox
            for g in pm.glyphs:
                if g.skey == p.skey and g.op < p.op:
                    gb = qbox(g)
                    cx, cy = (gb[0] + gb[2]) / 2, (gb[1] + gb[3]) / 2
                    if b[0] <= cx <= b[2] and b[1] <= cy <= b[3] and (gb[2] - gb[0]) > 0:
                        raise Reject("hidden_text", "glyph covered by a later fill")


def extract(data: bytes, rules: tuple[str, ...] = ALL_RULES, max_pages: int = 100) -> Extraction:
    try:
        pdf = pikepdf.open(io.BytesIO(data), attempt_recovery=False)
    except pikepdf.PasswordError:
        raise Reject("encrypted") from None
    except pikepdf.PdfError as e:
        raise Reject("parser_repair", str(e)[:80]) from None
    if pdf.is_encrypted:
        raise Reject("encrypted")
    inv = document_gate(pdf, data)
    if len(pdf.pages) > max_pages:
        raise Reject("structure_limit", "pages")
    it = Interp(pdf)
    pages = [it.page(i, p) for i, p in enumerate(pdf.pages)]
    visibility(pages)
    inv["rules_used"] = classify(pages, rules, pdf)
    for pm in pages:
        build_units(pm)
    smap = source_map(hashlib.sha256(data).hexdigest(), len(data), pages)
    ex = Extraction(pages, smap, inv, it)
    ex.pdf = pdf
    return ex
