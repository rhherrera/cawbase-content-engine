"""A08 planner, transformer and restorer of PDF documents, run ONLY inside the isolated extraction child
(docs/pdf-protection.md, sections 5-9).

* ``plan`` / ``protect``: coverage rules of the A08 matrix (D-A08-03), localization of every selection by unit
  offsets on the v2 model, safe zones computed from the ORIGINAL (typographic band ``TB``; ``Z1`` first, ``Z2`` as
  last resort), the substitute drawn with the frozen candidate font inside its zone, the selected codes REMOVED from
  their operators (never covered) and the mandatory removal of the authorized auxiliary classes.
* ``restore``: from the exact derivative and its authenticated binding only (never the original nor the analysis):
  each marker is replaced by the exact Unicode value encoded with the value's original font; on a ``Z2`` line the
  moved text returns to its original position and a redaction travels with its line (``RZ``).

Untrusted input; nothing here talks to the database, the storage or the key provider.
"""

from __future__ import annotations

import base64
import io
import json
import math
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from importlib import resources
from typing import Any

from . import pdf_extract as X
from .common import Limits, Reject

TRANSFORMER_ID = "cce-pdf-transform"
TRANSFORMER_VERSION = "1"
MIN_SCALE = 0.60
ZONE_MARGIN_EM = 0.3
V_TOL = 0.25  # pt: the substitute's ink may exceed the zone band by at most this
CAND_RES = "/FCCE8"
RULES = ("RP3", "RP5", "RP7")  # resource rules that survived the stage-2 adversarial controls (C-2B-06)
REDACTED = "[REDACTED]"


class Failure(Exception):
    """A closed job failure code (docs 5.5, 8.5); never carries content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- candidate font (package data, frozen)
@dataclass(frozen=True)
class Candidate:
    data: bytes
    meta: dict[str, Any]

    def width(self, text: str) -> float:  # 1/1000 text-space units (the integer /Widths written in the PDF)
        w = self.meta["widths"]
        try:
            return float(sum(w[c] for c in text))
        except KeyError:
            raise Failure("substitute_glyph_unavailable") from None

    def yext(self, text: str) -> tuple[float, float]:
        ys = [self.meta["y_extent"][c] for c in text if c in self.meta["y_extent"]]
        return min(y[0] for y in ys), max(y[1] for y in ys)


@lru_cache(maxsize=1)
def candidate() -> Candidate:
    import hashlib

    base = resources.files("cawbase_content_engine.documents") / "data"
    data = (base / "cce-candidate.ttf").read_bytes()
    meta = json.loads((base / "cce-candidate.json").read_text(encoding="utf-8"))
    if hashlib.sha256(data).hexdigest() != meta["subset_sha256"]:
        raise Failure("substitute_glyph_unavailable")
    return Candidate(data, meta)


def embed_candidate(pdf: Any) -> Any:
    import pikepdf

    c = candidate()
    m = c.meta
    ff = pdf.make_stream(c.data)
    ff.Length1 = len(c.data)
    first, last = 32, 126
    widths = [int(m["widths"].get(chr(code), 0)) for code in range(first, last + 1)]
    fd = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.FontDescriptor,
            FontName=pikepdf.Name("/" + m["base_font"]),
            Flags=32,
            FontBBox=list(m["bbox"]),
            ItalicAngle=0,
            Ascent=int(m["ascent"]),
            Descent=int(m["descent"]),
            CapHeight=int(m["cap_height"]),
            StemV=80,
            FontFile2=ff,
        )
    )
    chars = sorted(set(m["alphabet"] + " "))
    lines = "".join(f"<{ord(ch):02X}> <{ord(ch):04X}>\n" for ch in chars)
    cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        f"{len(chars)} beginbfchar\n{lines}endbfchar\nendcmap\n"
        "CMapName currentdict /CMap defineresource pop\nend\nend\n"
    )
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.TrueType,
            BaseFont=pikepdf.Name("/" + m["base_font"]),
            FirstChar=first,
            LastChar=last,
            Widths=widths,
            Encoding=pikepdf.Name.WinAnsiEncoding,
            FontDescriptor=fd,
            ToUnicode=pdf.make_stream(cmap.encode("ascii")),
        )
    )


# ---------------------------------------------------------------- coverage of painted resources (D-A08-03)
def _thin(b: tuple[float, ...], t: float = 2.0) -> bool:
    return (b[2] - b[0]) <= t or (b[3] - b[1]) <= t


def _grids(painted: list[X.Painted]) -> set[int]:
    """RP3: thin axis-aligned elements forming complete rectangular grids (table rules)."""
    thin = [p for p in painted if p.kind == "path" and _thin(p.bbox) and p.detail.get("curves", 0) == 0]
    hs: list[list[float]] = []
    vs: list[list[float]] = []
    for p in thin:
        b = p.bbox
        if (b[3] - b[1]) <= 2.0 and (b[2] - b[0]) > (b[3] - b[1]):
            hs.append([(b[1] + b[3]) / 2, b[0], b[2]])
        elif (b[2] - b[0]) <= 2.0:
            vs.append([(b[0] + b[2]) / 2, b[1], b[3]])

    def merge(items: list[list[float]]) -> list[list[float]]:
        items.sort()
        out: list[list[float]] = []
        for c, a, z in items:
            if out and abs(out[-1][0] - c) <= 0.75 and a <= out[-1][2] + 1.0:
                out[-1][2] = max(out[-1][2], z)
                out[-1][1] = min(out[-1][1], a)
            else:
                out.append([c, a, z])
        return out

    hm, vm = merge(hs), merge(vs)
    ok: set[int] = set()
    if len(hm) < 2 or len(vm) < 2:
        return ok
    xs0, xs1 = min(v[0] for v in vm), max(v[0] for v in vm)
    ys0, ys1 = min(h[0] for h in hm), max(h[0] for h in hm)
    full = all(h[1] <= xs0 + 1.0 and h[2] >= xs1 - 1.0 for h in hm) and all(
        v[1] <= ys0 + 1.0 and v[2] >= ys1 - 1.0 for v in vm
    )
    if not full:
        return ok
    for p in thin:
        b = p.bbox
        if b[0] >= xs0 - 1.5 and b[2] <= xs1 + 1.5 and b[1] >= ys0 - 1.5 and b[3] <= ys1 + 1.5:
            ok.add(id(p))
    return ok


_WHITE = (("g", (1.0,)), ("rg", (1.0, 1.0, 1.0)), ("k", (0.0, 0.0, 0.0, 0.0)))


def classify(pages: list[X.PageModel]) -> dict[str, int]:
    """Every painted object must be admitted by a frozen rule (RP3 table grids, RP5 a filled rectangle behind text,
    RP7 a white background); otherwise the document is refused: ``uncovered_content`` (no OCR, no flattening)."""
    counts: dict[str, int] = {}
    for pm in pages:
        boxes = [g.box for g in pm.glyphs if not g.text.isspace()]
        ta = [min(b[0] for b in boxes), max(b[2] for b in boxes)] if boxes else [pm.cropbox[0], pm.cropbox[2]]
        page_area = (pm.cropbox[2] - pm.cropbox[0]) * (pm.cropbox[3] - pm.cropbox[1])
        grid = _grids(pm.painted)
        for p in pm.painted:
            b = p.bbox
            w, h = b[2] - b[0], b[3] - b[1]
            rule = None
            if p.kind == "path":
                d = p.detail
                white = d.get("fill") and not d.get("stroke") and d.get("fill_color") in _WHITE
                if white and (w * h >= 0.5 * page_area or (b[0] <= ta[0] + 0.5 and b[2] >= ta[1] - 0.5)):
                    rule = "RP7"
                elif id(p) in grid:
                    rule = "RP3"
                elif (
                    d.get("fill")
                    and not d.get("stroke")
                    and d.get("is_rect")
                    and any(gb[0] >= b[0] and gb[2] <= b[2] and gb[1] >= b[1] and gb[3] <= b[3] for gb in boxes)
                    and not any(
                        o is not p
                        and o.bbox[0] > b[0] + 1
                        and o.bbox[2] < b[2] - 1
                        and o.bbox[1] > b[1] + 1
                        and o.bbox[3] < b[3] - 1
                        for o in pm.painted
                    )
                ):
                    rule = "RP5"
            if rule is None:
                raise Failure("uncovered_content")
            counts[rule] = counts.get(rule, 0) + 1
    return counts


# ---------------------------------------------------------------- zones (from the ORIGINAL only)
def right_of(g: X.Glyph) -> float:
    """Right end of a glyph: its typographic box or its ink, whichever reaches further."""
    return max(g.box[2], g.inkh[1]) if g.inkh is not None else g.box[2]


def white_backdrop(p: X.Painted) -> bool:
    """A white fill without stroke is a backdrop, never a barrier of a zone (hidden text is refused apart)."""
    d = p.detail
    return bool(p.kind == "path" and d.get("fill") and not d.get("stroke") and d.get("fill_color") in _WHITE)


def _cy(b: tuple[float, ...]) -> float:
    return (b[1] + b[3]) / 2


def zone_for(pm: X.PageModel, value: list[X.Glyph]) -> dict[str, Any]:
    """Z1 and the Z2 line limit of one selection (protocol 2B, C-2B-01 and C-2B-05), on typographic boxes:
    band = union of the line's boxes clipped against every other line; Z1 ends 0.3 em before the next glyph,
    a painted object crossing the band, the text area or the crop box; Z2 ignores the next glyph."""
    ink = [g for g in value if not g.text.isspace()]
    vb = (min(g.box[1] for g in ink), max(g.box[3] for g in ink))
    cy, h = (vb[0] + vb[1]) / 2, vb[1] - vb[0]
    sel = {g.gid for g in value}
    line = [g for g in pm.glyphs if not g.text.isspace() and g.box[2] > g.box[0] and abs(_cy(g.box) - cy) < h / 2]
    lid = {g.gid for g in line}
    x0 = ink[0].box[0]
    left = min([x0] + [g.inkh[0] for g in ink if g.inkh is not None])  # ink left of the origin (italics)
    x_end = max([g.box[2] for g in ink] + [g.inkh[1] for g in ink if g.inkh is not None])
    band = [min(g.box[1] for g in line), max(g.box[3] for g in line)]
    for g in pm.glyphs:  # clip against the INK of every other line (typographic box when the ink is unknown)
        if g.gid in lid or g.text.isspace() or g.box[2] <= g.box[0] or g.ink_empty:
            continue
        b = g.box
        y0, y1 = g.inkv if g.inkv is not None else (b[1], b[3])
        if b[0] < pm.cropbox[2] and b[2] > x0 and y0 < band[1] and y1 > band[0]:
            if (y0 + y1) / 2 > (band[0] + band[1]) / 2:
                band[1] = min(band[1], y0)
            else:
                band[0] = max(band[0], y1)
    size = max(X.em(g) for g in ink)
    margin = ZONE_MARGIN_EM * size
    last_cx = (ink[-1].box[0] + ink[-1].box[2]) / 2  # following = centre right of the value's last glyph (kerning)
    nxt = [g.box[0] for g in line if g.gid not in sel and (g.box[0] + g.box[2]) / 2 > last_cx + 0.01]
    ra = min(nxt) if nxt else math.inf
    rb = math.inf
    for p in pm.painted:
        b = p.bbox
        if b[1] >= band[1] or b[3] <= band[0] or white_backdrop(p):
            continue
        if b[0] > x0 + 0.01:
            rb = min(rb, b[0])
        elif b[2] > x0 + 0.01:
            rb = min(rb, b[2])
    rc = max(g.box[2] for g in pm.glyphs if not g.text.isspace() and g.box[2] > g.box[0])
    rd = pm.cropbox[2]
    return {
        "x0": x0,
        "left": left,
        "band": band,
        "r_z1": max(min(ra, rb, rc, rd) - margin, x_end),
        "r_line": max(min(rb, rc, rd) - margin, x_end),
        "size": size,
    }


# ---------------------------------------------------------------- planning
@dataclass
class Sel:
    sid: str
    page: int  # 0-based
    start: int
    end: int
    action: str
    substitute: str
    glyphs: list[X.Glyph]
    zone: dict[str, Any]
    scale: float = 1.0
    x0: float = 0.0
    span: float = 0.0  # original advance of the value in page units (origin of the first to the end of the last)
    nat: float = 0.0  # natural width of the substitute in the candidate font, page units


@dataclass
class LinePlan:
    page: int
    sels: list[Sel]
    kind: str
    f: float
    x0: float
    r: float
    moved: list[X.Glyph] = field(default_factory=list)
    layout: dict[Any, float] = field(default_factory=dict)  # sid / gid -> new origin x


def _glyphs_of(pm: X.PageModel, start: int, end: int) -> list[X.Glyph]:
    gl = {g.gid: g for g in pm.glyphs}
    seg = [s for s in pm.segments if s["unit_start"] < end and s["unit_end"] > start]
    if not seg:
        raise Failure("analysis_source_invalid")
    if any(s["kind"] == "synthesized_line" for s in seg):
        raise Failure("selection_crosses_boundary")
    if any(s["unit_start"] < start or s["unit_end"] > end for s in seg if s["gid"] is not None):
        raise Failure("localization_ambiguous")  # part of a ligature (n:1)
    out = [gl[s["gid"]] for s in seg if s["gid"] is not None]
    if not out or out[0].text.isspace() or out[-1].text.isspace():
        raise Failure("selection_not_transformable")
    return out


def _sub_width(g0: X.Glyph, text: str) -> float:
    return candidate().width(text) / 1000.0 * X.kpage(g0)


def _fits_vertically(se: Sel) -> bool:
    g0 = se.glyphs[0]
    lo, hi = candidate().yext(se.substitute)
    base = g0.origin[1]
    vs = X.em(g0)
    band = se.zone["band"]
    return bool(base + lo / 1000 * vs >= band[0] - V_TOL and base + hi / 1000 * vs <= band[1] + V_TOL)


def plan(m: X.Model, selections: list[dict[str, Any]]) -> list[LinePlan]:
    """Selections: groups of A06 (``unit_id``, ``start``, ``end``, ``action``, ``substitute``) in unit offsets."""
    m.inventory["rules"] = classify(m.pages)
    by_page: dict[int, list[Sel]] = {}
    for k, s in enumerate(selections):
        try:
            pno = int(str(s["unit_id"])[1:]) - 1
        except ValueError:
            raise Failure("analysis_source_invalid") from None
        if not (0 <= pno < len(m.pages)):
            raise Failure("analysis_source_invalid")
        pm = m.pages[pno]
        start, end = int(s["start"]), int(s["end"])
        if not (0 <= start < end <= len(pm.text)):
            raise Failure("analysis_source_invalid")
        glyphs = _glyphs_of(pm, start, end)
        frag = pm.text[start:end]
        if any(unicodedata.bidirectional(ch) in ("R", "AL", "RLE", "RLO", "RLI") for ch in frag):
            raise Failure("selection_not_transformable")
        if any(g.at_span for g in glyphs):
            raise Failure("selection_not_transformable")  # /ActualText intersecting the selection (C-2B-03)
        if any(g.approximate for g in glyphs):
            raise Failure("font_mapping_insufficient")  # base-14 font without /Widths: no reliable geometry
        if any(g.in_form for g in glyphs) or len({g.skey for g in glyphs}) != 1:
            raise Failure("selection_not_transformable")  # Form XObject or several content streams
        if not all(X.horizontal(g) for g in glyphs):
            raise Failure("selection_not_transformable")
        se = Sel(f"s{k}", pno, start, end, str(s["action"]), str(s["substitute"]), glyphs, zone_for(pm, glyphs))
        g0, last = glyphs[0], glyphs[-1]
        se.x0 = g0.origin[0]
        se.span = last.origin[0] + X.adv_page(last) - g0.origin[0]
        se.nat = _sub_width(g0, se.substitute)
        by_page.setdefault(pno, []).append(se)
    plans: list[LinePlan] = []
    for pno, sels in sorted(by_page.items()):
        lines: list[list[Sel]] = []
        for se in sorted(sels, key=lambda z: (-z.zone["band"][1], z.x0)):
            lz = lines[-1][0].zone["band"] if lines else None
            if lz is not None and abs(lz[0] - se.zone["band"][0]) < 0.5 and abs(lz[1] - se.zone["band"][1]) < 0.5:
                lines[-1].append(se)
            else:
                lines.append([se])
        for line in lines:
            line.sort(key=lambda z: z.x0)
            plans.extend(_line_plan(m, m.pages[pno], line))
    _ = [_ for _ in plans]
    return plans


def _line_plan(m: X.Model, pm: X.PageModel, line: list[Sel]) -> list[LinePlan]:
    ok = True
    for se in line:
        avail = se.zone["r_z1"] - se.x0
        s = min(1.0, avail / se.nat) if se.nat > 0 else 1.0
        if s >= MIN_SCALE and _fits_vertically(se):
            se.scale = s
        else:
            ok = False
    if ok:
        out = []  # Z1: one block per selection, drawn where it was (reading order kept)
        for se in line:
            lp = LinePlan(pm.index, [se], "Z1", 1.0, se.x0, se.zone["r_z1"])
            lp.layout[se.sid] = se.x0
            out.append(lp)
        return out
    if not all(_fits_vertically(se) for se in line):
        raise Failure("replacement_overflow")
    x0 = line[0].x0
    r = min(se.zone["r_line"] for se in line)
    band = [min(se.zone["band"][0] for se in line), max(se.zone["band"][1] for se in line)]
    sel_ids = {g.gid for se in line for g in se.glyphs}
    skey = line[0].glyphs[0].skey
    if len({se.glyphs[0].skey for se in line}) != 1:
        raise Failure("zone_plan_unjustified")

    def cx(g: X.Glyph) -> float:
        return (g.box[0] + g.box[2]) / 2

    on_line = [
        g
        for g in pm.glyphs
        if g.gid not in sel_ids and not g.text.isspace() and band[0] <= _cy(g.box) <= band[1] and cx(g) >= x0
    ]
    # moved = the glyphs WHOLLY inside the zone (a glyph straddling its right limit stays where it is)
    moved = [g for g in on_line if cx(g) <= r and right_of(g) <= r + 0.01]
    stay = [g.box[0] for g in on_line if not (cx(g) <= r and right_of(g) <= r + 0.01)]
    moved += [
        g
        for g in pm.glyphs
        if g.text.isspace()
        and g.gid not in sel_ids
        and band[0] <= _cy(g.box) <= band[1]
        and x0 <= g.origin[0]
        and (not stay or g.origin[0] < min(stay))
    ]
    if stay:
        r = min(r, min(stay))
    mc0 = line[0].glyphs[0].mc
    for g in moved:
        if g.in_form or g.skey != skey:
            raise Failure("zone_plan_unjustified")
        if not X.horizontal(g) or g.approximate:
            raise Failure("zone_plan_unjustified")
        if g.at_span:
            raise Failure("zone_plan_unjustified")  # moving it would break its /ActualText (C-2B-03, C5)
        if g.mc != mc0:
            raise Failure("zone_plan_unjustified")  # it would change its marked-content sequence (MCID)
    for p in pm.painted:
        b = p.bbox
        if b[0] >= x0 - 0.01 and b[2] <= r + 0.01 and b[1] >= band[0] - 0.01 and b[3] <= band[1] + 0.01:
            raise Failure("zone_plan_unjustified")
    for an in m.pdf.pages[pm.index].obj.get("/Annots", []) or []:
        if str(an.get("/Subtype", "")) == "/Link":
            rb = [float(v) for v in an.Rect]
            rb = [min(rb[0], rb[2]), min(rb[1], rb[3]), max(rb[0], rb[2]), max(rb[1], rb[3])]
            if rb[0] < r and rb[2] > x0 and rb[1] < band[1] and rb[3] > band[0]:
                raise Failure("zone_plan_unjustified")
    items: list[tuple[str, float, float, Any]] = []
    for se in line:
        items.append(("sel", se.x0, se.span, se))
    for g in moved:
        items.append(("g", g.origin[0], X.adv_page(g), g))
    items.sort(key=lambda t: t[1])
    d = 0.0
    newx: dict[Any, float] = {}
    end = x0
    for kind, x, w, obj in items:
        nx = x + d
        if kind == "sel":
            newx[obj.sid] = nx
            end = max(end, nx + obj.nat)
            d += obj.nat - w
        else:
            newx[obj.gid] = nx
            end = max(end, nx + w)
    f = min(1.0, (r - x0) / (end - x0)) if end > x0 else 1.0
    if f < MIN_SCALE:
        raise Failure("replacement_overflow")
    lp = LinePlan(pm.index, line, "Z2", f, x0, r, moved)
    lp.layout = {k: x0 + f * (v - x0) for k, v in newx.items()}
    for se in line:
        se.scale = f
    return [lp]


def _runs(se: Sel) -> list[dict[str, Any]]:
    """Font runs of the value (never its text): resource, size, Th, number of code points, advance (page units)."""
    out: list[dict[str, Any]] = []
    key = None
    for g in se.glyphs:
        k = (g.font, round(g.size, 6), round(g.th, 6), round(X.em(g), 4))
        if out and k == key:
            out[-1]["n"] += len(g.text)
            out[-1]["adv"] += X.adv_page(g)
        else:
            out.append(
                {"font": g.font, "size": g.size, "em": X.em(g), "th": g.th, "n": len(g.text), "adv": X.adv_page(g)}
            )
            key = k
    return out


# ---------------------------------------------------------------- content-stream editing
def _n(x: float) -> Decimal:
    return Decimal(f"{x:.6f}").normalize() if x == x else Decimal(0)


def _op(name: str, *operands: Any) -> Any:
    import pikepdf

    return pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(name))


def _tm(mat: X.Mat) -> list[Decimal]:
    return [_n(v) for v in mat]


def _fill_ops(raw: list[tuple[str, list[Any]]]) -> list[Any]:
    return [_op(n, *ops) for n, ops in raw]


def _full_extent(
    ins: Any,
    rec: X.OpState,
    removed: dict[tuple[int, int], float],
    advances: dict[tuple[int, int], float],
    splits: dict[tuple[int, int], list[Any]],
    blank: tuple[bytes, float],
) -> tuple[list[Any], list[tuple[int, int]]]:
    """E-A08-27: the operator is cut at every block, and EVERY piece keeps the whole extent of the original operator:
    the codes that are not its own (or that were removed) are drawn as the font's ink-less space code with a ``TJ``
    correction that keeps the exact advance. The pieces start at the operator's own origin (the blocks end with the
    text matrix back there), so reading order is prefix -> block -> suffix and every text object has the original
    extent (PDFium positions the glyphs of a text object according to that extent: 2B section 3.1)."""
    import pikepdf

    op = str(ins.operator)
    a = list(ins.operands)
    pre: list[Any] = []
    if op == "'":
        pre = [_op("T*")]
    elif op == '"':
        pre = [_op("Tw", a[0]), _op("Tc", a[1]), _op("T*")]
    elems = list(a[0]) if op == "TJ" else [a[-1]]
    size, th = rec.size, rec.th
    n = rec.code_len
    order = sorted(splits)
    keys = [
        (ei, pos)
        for ei, el in enumerate(elems)
        if isinstance(el, pikepdf.String)
        for pos in range(0, len(bytes(el)), n)
    ]
    if any(k not in set(keys) for k in order):
        raise Failure("zone_plan_unjustified")  # a block whose first code is not in this operator

    def seg_of(key: tuple[int, int]) -> int:
        return sum(1 for s in order if s <= key)

    def piece(k: int) -> list[Any]:
        part: list[Any] = []
        cur = b""

        def flush() -> None:
            nonlocal cur
            if cur:
                part.append(pikepdf.String(cur))
                cur = b""

        def num(v: float) -> None:
            flush()
            if part and isinstance(part[-1], Decimal):
                part[-1] = _n(float(part[-1]) + v)
            else:
                part.append(_n(v))

        for ei, el in enumerate(elems):
            if isinstance(el, pikepdf.String):
                data = bytes(el)
                for pos in range(0, len(data), n):
                    key = (ei, pos)
                    if seg_of(key) == k and key not in removed:
                        cur += data[pos : pos + n]
                    else:
                        cur += blank[0]
                        num(-(advances[key] - blank[1]) / (size * th) * 1000.0)
            else:
                num(float(el))
        flush()
        return part

    out = list(pre)
    spans: list[tuple[int, int]] = []
    for k in range(len(order) + 1):
        if k:
            blk = splits[order[k - 1]]
            spans.append((len(out), len(out) + len(blk)))
            out += blk
        out.append(_op("TJ", pikepdf.Array(piece(k))))
    return out, spans


def _rebuild_show(
    ins: Any,
    rec: X.OpState,
    removed: dict[tuple[int, int], float],
    splits: dict[tuple[int, int], list[Any]] | None = None,
) -> tuple[list[Any], list[tuple[int, int]]]:
    """The show operator with the removed codes replaced by ``TJ`` numbers equal to their advance (the codes are
    GONE from the operand). Each ``splits`` key cuts the operator right before that code and its block goes there.
    Used when the font has no ink-less space code. Returns the operators and the ``[start, end)`` of each block."""
    import pikepdf

    op = str(ins.operator)
    a = list(ins.operands)
    pre: list[Any] = []
    if op == "'":
        pre = [_op("T*")]
    elif op == '"':
        pre = [_op("Tw", a[0]), _op("Tc", a[1]), _op("T*")]
    elems = list(a[0]) if op == "TJ" else [a[-1]]
    size, th = rec.size, rec.th
    splits = splits or {}
    parts: list[list[Any]] = [[]]
    cuts: list[tuple[int, int]] = []

    def add_num(num: float) -> None:
        cur = parts[-1]
        if cur and isinstance(cur[-1], Decimal):
            cur[-1] = _n(float(cur[-1]) + num)
        else:
            cur.append(_n(num))

    for ei, el in enumerate(elems):
        if isinstance(el, pikepdf.String):
            data = bytes(el)
            n = rec.code_len
            cur = b""
            pos = 0
            while pos < len(data):
                key = (ei, pos)
                if key in splits:
                    if cur:
                        parts[-1].append(pikepdf.String(cur))
                        cur = b""
                    parts.append([])
                    cuts.append(key)
                if key in removed:
                    if cur:
                        parts[-1].append(pikepdf.String(cur))
                        cur = b""
                    add_num(-removed[key] / (size * th) * 1000.0)
                else:
                    cur += data[pos : pos + n]
                pos += n
            if cur:
                parts[-1].append(pikepdf.String(cur))
        else:
            parts[-1].append(el)
    if len(cuts) != len(splits):
        raise Failure("zone_plan_unjustified")  # a block whose first code is not in this operator
    out = list(pre)
    spans: list[tuple[int, int]] = []
    for k, part in enumerate(parts):
        if k:
            blk = splits[cuts[k - 1]]
            spans.append((len(out), len(out) + len(blk)))
            out += blk
        if part:
            out.append(_op("TJ", pikepdf.Array(part)))
    return out, spans


def _block(m: X.Model, lp: LinePlan, rec_after: X.OpState, codes_of: Any, back_to: X.Mat | None = None) -> list[Any]:
    """Drawing block inserted where the first selected glyph was (same BT, same CTM)."""
    import pikepdf

    g0 = lp.sels[0].glyphs[0]
    ctm = g0.ctm
    ins: list[Any] = []

    def tm_at(g: X.Glyph, x_new: float) -> X.Mat:
        gm = X.mmul(g.tm, g.ctm)
        return X.mmul(X.mmul(gm, [1, 0, 0, 1, x_new - g.origin[0], 0]), X.minv(ctm))

    draws: list[tuple[str, float, Any]] = [("sel", lp.layout[se.sid], se) for se in lp.sels]
    draws += [("g", lp.layout[g.gid], g) for g in lp.moved]
    draws.sort(key=lambda t: t[1])
    for kind, x, obj in draws:
        if kind == "sel":
            g = obj.glyphs[0]
            ins += _fill_ops(m.interp.opstate[(g.page, g.skey, g.op)].fill_raw)
            ins += [
                _op("Tf", pikepdf.Name(CAND_RES), _n(g.size)),
                _op("Tz", _n(obj.scale * 100.0)),
                _op("Tc", 0),
                _op("Tw", 0),
                _op("Ts", _n(g.ts)),
                _op("Tr", g.tr),
                _op("Tm", *_tm(tm_at(g, x))),
                _op("Tj", pikepdf.String(codes_of(obj))),
            ]
        else:
            g = obj
            ins += _fill_ops(m.interp.opstate[(g.page, g.skey, g.op)].fill_raw)
            ins += [
                _op("Tf", pikepdf.Name(g.font), _n(g.size)),
                _op("Tz", _n(g.th * lp.f * 100.0)),
                _op("Tc", 0),
                _op("Tw", 0),
                _op("Ts", _n(g.ts)),
                _op("Tr", g.tr),
                _op("Tm", *_tm(tm_at(g, x))),
                _op("Tj", pikepdf.String(g.code)),
            ]
    r = rec_after
    ins += _fill_ops(r.fill_raw)
    ins += [
        _op("Tf", pikepdf.Name(r.fname), _n(r.size)),
        _op("Tz", _n(r.th * 100.0)),
        _op("Tc", _n(r.tc)),
        _op("Tw", _n(r.tw)),
        _op("Ts", _n(r.ts)),
        _op("Tr", r.tr),
        _op("Tm", *_tm(r.tlm)),
    ]
    rel = X.mmul(back_to if back_to is not None else g0.tm, X.minv(r.tlm))
    if abs(rel[5]) > 1e-6 or abs(rel[0] - 1) > 1e-6 or abs(rel[1]) > 1e-6 or abs(rel[2]) > 1e-6:
        raise Failure("zone_plan_unjustified")
    dx = rel[4]
    if abs(dx) > 1e-9:
        ins.append(_op("TJ", pikepdf.Array([_n(-dx / (r.size * r.th) * 1000.0)])))
    return ins


def remove_e(pdf: Any) -> list[dict[str, Any]]:
    """Authorized auxiliary classes (PT-25 to PT-27): descriptive ``/Info``, XMP, object metadata, comments."""
    import pikepdf

    out: dict[str, int] = {}
    info = pdf.trailer.get("/Info")
    if info is not None:
        keys = [k for k in info.keys() if k not in ("/CreationDate", "/ModDate")]
        for k in keys:
            del info[k]
        if keys:
            out["document_info_descriptive"] = len(keys)
    if "/Metadata" in pdf.Root:
        del pdf.Root["/Metadata"]
        out["xmp_metadata"] = 1
    n = 0
    for o in pdf.objects:
        if isinstance(o, (pikepdf.Dictionary, pikepdf.Stream)):
            d = o.stream_dict if isinstance(o, pikepdf.Stream) else o
            if "/Metadata" in d:
                del d["/Metadata"]
                n += 1
    if n:
        out["object_metadata"] = n
    c = 0
    for p in pdf.pages:
        annots = p.obj.get("/Annots")
        if annots is None:
            continue
        keep = [a for a in annots if str(a.get("/Subtype", "")) not in ("/Text", "/Popup")]
        c += len(annots) - len(keep)
        for a in keep:
            if "/Popup" in a:
                del a["/Popup"]
        if len(keep) != len(annots):
            if keep:
                p.obj.Annots = pdf.make_indirect(pikepdf.Array(keep))
            else:
                del p.obj["/Annots"]
    if c:
        out["comments"] = c
    return [{"element_class": k, "count": v} for k, v in sorted(out.items())]


def _blank(m: X.Model, page: int, skey: str, i: int, rec: X.OpState) -> tuple[bytes, float] | None:
    """The space code of the operator's font and its advance (text space) when it is unique and its glyph is KNOWN
    to have no ink (embedded TrueType program); otherwise None (the operator is then cut with numbers only)."""
    g = next((g for g in m.pages[page].glyphs if g.skey == skey and g.op == i), None)
    if g is None:
        return None
    f = m.interp.fonts[g.font_key]
    codes = f.reverse().get(" ", [])
    if len(codes) != 1 or f.ink(codes[0], " ") is not False:
        return None
    w = f.width(codes[0])
    tw = rec.tw if (f.code_len == 1 and codes[0] == b" ") else 0.0
    return codes[0], (w / 1000.0 * rec.size + rec.tc + tw) * rec.th


def _save(pdf: Any) -> bytes:
    import pikepdf

    buf = io.BytesIO()
    pdf.save(
        buf,
        object_stream_mode=pikepdf.ObjectStreamMode.disable,
        compress_streams=True,
        stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
        deterministic_id=True,
        linearize=False,
    )
    return buf.getvalue()


def _target(pdf: Any, page: int, skey: str) -> Any:
    import pikepdf

    k = int(skey.split(":")[2])
    cont = pdf.pages[page].obj.Contents
    return cont[k] if isinstance(cont, pikepdf.Array) else cont


def protect(data: bytes, lim: Limits, selections: list[dict[str, Any]], *, check_only: bool) -> dict[str, Any]:
    try:
        m = X.model(data, lim)
    except Reject as r:
        return {"rejected": r.reason}
    try:
        plans = plan(m, selections)
    except Failure as f:
        return {"failure": f.code}
    if check_only:
        return {
            "ok": True,
            "lines": len(plans),
            "z2": sum(1 for lp in plans if lp.kind == "Z2"),
            "rules": dict(m.inventory.get("rules", {})),
        }
    import pikepdf

    pdf = m.pdf
    cfont = embed_candidate(pdf) if plans else None
    edits: dict[tuple[int, str], dict[str, Any]] = {}
    layout: list[dict[str, Any]] = []
    for lp in plans:
        g0 = lp.sels[0].glyphs[0]
        if not g0.skey.startswith("page:"):
            return {"failure": "selection_not_transformable"}
        e = edits.setdefault((lp.page, g0.skey), {"removed": {}, "blocks": {}})
        for g in [g for se in lp.sels for g in se.glyphs] + lp.moved:
            e["removed"].setdefault(g.op, {})[(g.elem, g.cstart)] = g.adv
        rec_after = m.interp.opstate[(g0.page, g0.skey, g0.op)]

        def codes(se: Sel) -> bytes:
            return se.substitute.encode("ascii")

        try:
            blank = _blank(m, g0.page, g0.skey, g0.op, rec_after)
            blk = _block(m, lp, rec_after, codes, rec_after.tm_start if blank is not None else None)
        except Failure as f:
            return {"failure": f.code}
        e["blocks"].setdefault(g0.op, []).append((len(layout), blk, (g0.elem, g0.cstart)))
        res = pdf.pages[lp.page].Resources
        if "/Font" not in res:
            res.Font = pikepdf.Dictionary()
        assert cfont is not None
        if CAND_RES in res.Font and res.Font[CAND_RES].objgen != cfont.objgen:
            return {"failure": "unsupported_structure"}
        res.Font[CAND_RES] = cfont
        layout.append(
            {
                "page": lp.page,
                "stream": g0.skey,
                "kind": lp.kind,
                "f": lp.f,
                "x0": lp.x0,
                "r": lp.r,
                "left": min(se.zone["left"] for se in lp.sels),
                "band": [
                    min(se.zone["band"][0] for se in lp.sels),
                    max(se.zone["band"][1] for se in lp.sels),
                ],
                "sels": [
                    {
                        "sid": se.sid,
                        "action": se.action,
                        "unit_start": se.start,
                        "unit_end": se.end,
                        "scale": se.scale,
                        "x_new": lp.layout[se.sid],
                        "x_orig": se.x0,
                        "span": se.span,
                        "nat": se.nat,
                        "size": se.glyphs[0].size,
                        "runs": _runs(se),
                        "substitute": se.substitute,
                        "band": list(se.zone["band"]),
                        "left": se.zone["left"],
                        "zone_r": se.zone["r_z1"] if lp.kind == "Z1" else min(q.zone["r_line"] for q in lp.sels),
                    }
                    for se in lp.sels
                ],
                "moved": [
                    {"x_orig": g.origin[0], "x_new": lp.layout[g.gid], "th": g.th}
                    for g in sorted(lp.moved, key=lambda q: lp.layout[q.gid])
                ],
            }
        )
    for (page, skey), e in edits.items():
        pm = m.pages[page]
        target = _target(pdf, page, skey)
        ops = pm.streams[skey]
        new: list[Any] = []
        for i, ins in enumerate(ops):
            blocks = e["blocks"].get(i, [])
            if len({split for _li, _blk, split in blocks}) != len(blocks):
                return {"failure": "zone_plan_unjustified"}  # two blocks would start at the same code
            if i in e["removed"]:
                rec = m.interp.opstate[(page, skey, i)]
                ordered = sorted(blocks, key=lambda b: b[2])
                try:
                    blank = _blank(m, page, skey, i, rec)
                    if blank is not None:
                        adv = {(g.elem, g.cstart): g.adv for g in pm.glyphs if g.skey == skey and g.op == i}
                        tmp, spans = _full_extent(ins, rec, e["removed"][i], adv, {b[2]: b[1] for b in ordered}, blank)
                    else:
                        tmp, spans = _rebuild_show(ins, rec, e["removed"][i], {b[2]: b[1] for b in ordered})
                except Failure as f:
                    return {"failure": f.code}
                for (li, _blk, _split), (a, b) in zip(ordered, spans, strict=True):
                    layout[li]["block"] = [len(new) + a, len(new) + b]
                new += tmp
            else:
                new.append(ins)
        target.write(pikepdf.unparse_content_stream(new))
        if "/DecodeParms" in target:
            del target["/DecodeParms"]
    removed = remove_e(pdf)
    out = _save(pdf)
    return {
        "derivative": base64.b64encode(out).decode("ascii"),
        "removed": removed,
        "layout": layout,
        "lines": len(plans),
        "z2": sum(1 for lp in plans if lp.kind == "Z2"),
    }


# ---------------------------------------------------------------- mechanical restoration (derivative, binding, values)
def _shift(tm: X.Mat, ctm: X.Mat, dx_page: float) -> X.Mat:
    return X.mmul(X.mmul(X.mmul(tm, ctm), [1, 0, 0, 1, dx_page, 0]), X.minv(ctm))


def redaction_scale(span: float, nat: float) -> tuple[float, float]:
    """RZ (C-2B-02): a ``Z2`` redaction returns to the redacted value's start compressed to its original advance,
    never below ``MIN_SCALE``; returns (scale, excess in page units that shifts what follows)."""
    sc = min(1.0, span / nat) if nat > 0 else 1.0
    if sc < MIN_SCALE:
        return MIN_SCALE, MIN_SCALE * nat - span
    return sc, 0.0


def restore(data: bytes, lim: Limits, layout: list[dict[str, Any]], values: dict[str, str]) -> dict[str, Any]:
    """``values``: sid -> exact original of every PSEUDONYMIZED selection (redactions are never restored)."""
    try:
        m = X.model(data, lim)
    except Reject:
        return {"failure": "derivative_markers_invalid"}
    import pikepdf

    pdf = m.pdf
    by_stream: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for line in layout:
        by_stream.setdefault((int(line["page"]), str(line["stream"])), []).append(line)
    try:
        for (page, skey), lines in sorted(by_stream.items()):
            pm = m.pages[page]
            if skey not in pm.streams:
                return {"failure": "derivative_markers_invalid"}
            target = _target(pdf, page, skey)
            ops = list(pm.streams[skey])
            res = pdf.pages[page].Resources
            for line in sorted(lines, key=lambda q: -int(q["block"][0])):
                ops = _restore_line(m, pm, ops, res, skey, line, values)
            target.write(pikepdf.unparse_content_stream(ops))
            if "/DecodeParms" in target:
                del target["/DecodeParms"]
    except Failure as f:
        return {"failure": f.code}
    return {"restored": base64.b64encode(_save(pdf)).decode("ascii")}


def _ctm_at(m: X.Model, page: int, skey: str, b0: int, b1: int) -> X.Mat:
    """CTM of the block: the one its own show operators were drawn with (one BT, so one CTM for all of them)."""
    ctms = [rec.ctm for (pi, sk, i), rec in m.interp.opstate.items() if pi == page and sk == skey and b0 <= i < b1]
    if not ctms or any(c != ctms[0] for c in ctms):
        raise Failure("derivative_markers_invalid")
    return ctms[0]


def _restore_line(
    m: X.Model,
    pm: X.PageModel,
    ops: list[Any],
    res: Any,
    skey: str,
    line: dict[str, Any],
    values: dict[str, str],
) -> list[Any]:
    import pikepdf

    b0, b1 = int(line["block"][0]), int(line["block"][1])
    if not (0 <= b0 < b1 <= len(ops)):
        raise Failure("derivative_markers_invalid")
    g_ctm = _ctm_at(m, pm.index, skey, b0, b1)
    blk = ops[b0:b1]
    draws: list[list[Any]] = []
    cur: list[Any] = []
    for ins in blk:
        cur.append(ins)
        if str(ins.operator) == "Tj":
            draws.append(cur)
            cur = []
    tail = cur
    items = sorted(
        [("sel", float(s["x_new"]), s) for s in line["sels"]] + [("g", float(v["x_new"]), v) for v in line["moved"]],
        key=lambda t: t[1],
    )
    if len(items) != len(draws):
        raise Failure("derivative_markers_invalid")
    sels = sorted(line["sels"], key=lambda q: float(q["x_orig"]))
    excess: dict[str, tuple[float, float]] = {}
    if line["kind"] == "Z2":
        for s in sels:
            if s["action"] == "redact":
                excess[s["sid"]] = redaction_scale(float(s["span"]), float(s["nat"]))

    def e_left(x: float) -> float:
        return sum(excess[s["sid"]][1] for s in sels if s["sid"] in excess and float(s["x_orig"]) < x - 1e-6)

    out: list[Any] = []
    fonts: dict[str, X.Font] = {}

    def font(name: str) -> X.Font:
        if name not in fonts:
            if "/Font" not in res or name not in res.Font:
                raise Failure("restoration_encoding_unavailable")
            fonts[name] = X.load_font(name, res.Font[name], m.interp.lim)
        return fonts[name]

    for (kind, _xn, it), d in zip(items, draws, strict=True):
        tm_ins = next((x for x in d if str(x.operator) == "Tm"), None)
        if tm_ins is None:
            raise Failure("derivative_markers_invalid")
        tm = [float(v) for v in tm_ins.operands]
        tmc = X.mmul(tm, g_ctm)
        x_now = X.apply(tmc, 0, 0)[0]
        k_page = math.hypot(tmc[0], tmc[1])  # page units per text-space unit along the baseline (size excluded)
        v_page = math.hypot(tmc[2], tmc[3])
        if kind == "sel":
            x_to = float(it["x_orig"]) + e_left(float(it["x_orig"]))
            if it["action"] == "redact":
                if line["kind"] == "Z2":
                    sc = excess[it["sid"]][0]
                    keep = [x for x in d if str(x.operator) not in ("Tz", "Tm")]
                    tj = keep.pop()
                    out += keep + [_op("Tz", _n(sc * 100.0)), _op("Tm", *_tm(_shift(tm, g_ctm, x_to - x_now))), tj]
                else:
                    out += d  # Z1: the redaction stays where the derivative drew it (definitive)
                continue
            val = values.get(it["sid"])
            if val is None or sum(int(r["n"]) for r in it["runs"]) != len(val):
                raise Failure("derivative_markers_invalid")
            out += [x for x in d if str(x.operator) not in ("Tf", "Tz", "Tm", "Tj")]
            pos, x = 0, x_to
            total = sum(float(r["adv"]) for r in it["runs"])
            fit = float(it["span"]) / total if total > 0 else 1.0  # the internal kerning is spread over the value
            for run in it["runs"]:
                piece = val[pos : pos + int(run["n"])]
                pos += int(run["n"])
                vf = font(str(run["font"]))
                rev = vf.reverse()
                codes: list[bytes] = []
                i = 0
                while i < len(piece):
                    for ln in (3, 2, 1):  # longest first (ligatures n:1)
                        pc = piece[i : i + ln]
                        if len(pc) == ln and pc in rev and len(rev[pc]) == 1:
                            codes.append(rev[pc][0])
                            i += ln
                            break
                    else:
                        raise Failure("restoration_encoding_unavailable")
                size = float(run["em"]) / v_page if v_page else float(run["size"])  # the run's own em
                w_nat = sum(vf.width(cd) for cd in codes) / 1000.0 * size * k_page
                th = float(run["adv"]) * fit / w_nat if w_nat else 1.0
                out += [
                    _op("Tf", pikepdf.Name(str(run["font"])), _n(size)),
                    _op("Tz", _n(th * 100.0)),
                    _op("Tm", *_tm(_shift(tm, g_ctm, x - x_now))),
                    _op("Tj", pikepdf.String(b"".join(codes))),
                ]
                x += float(run["adv"]) * fit
        else:
            x_to = float(it["x_orig"]) + e_left(float(it["x_orig"]))
            keep = [x for x in d if str(x.operator) not in ("Tz", "Tm")]
            tj = keep.pop()
            out += keep + [_op("Tz", _n(float(it["th"]) * 100.0)), _op("Tm", *_tm(_shift(tm, g_ctm, x_to - x_now))), tj]
            if e_left(float(it["x_orig"])) > 0:
                tf = next(x for x in d if str(x.operator) == "Tf")
                gf = font(str(tf.operands[0]))
                w = sum(gf.width(cd) for cd in gf.split(bytes(tj.operands[0])))
                end = x_to + w / 1000.0 * float(tf.operands[1]) * k_page * float(it["th"])
                if end > float(line["r"]) + 0.01:
                    raise Failure("restoration_layout_unavailable")
    return ops[:b0] + out + tail + ops[b1:]
