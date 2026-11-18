"""Candidate planner, operator-local transformer and mechanical restorer (protocol §0.2-§0.5, contract §7, §9).

The planner decides, BEFORE transforming, the zone kind (Z1/Z2), the font attempt and the factor of every line plan;
the plan (numbers only) is what the verifier uses together with the frozen oracle to compute expectations.
"""

from __future__ import annotations

import io
import math
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pikepdf
from cawbase_content_engine.protection import markers as A06
from cawbase_content_engine.protection.grouping import plan_protection
from fontTools import subset as ftsubset
from fontTools.ttLib import TTFont

from .gate import Extraction
from .pdfmodel import Glyph, apply, minv, mmul, qbox

ROOT = Path(__file__).resolve().parent.parent
CAND_FONT = ROOT / "fonts" / "NotoSans-Condensed.ttf"
SIGMA = "[]:" + A06.ALPHABET
MIN_SCALE = 0.60
VARIANT: set[str] = set()  # informative (never counted): "RZ" moves a Z2 redaction back with its line on restoration
CAND_RES = "/FA08"


class PlanFail(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------- candidate font
class Candidate:
    def __init__(self) -> None:
        f = TTFont(str(CAND_FONT))
        self.upm = f["head"].unitsPerEm
        cmap = f.getBestCmap()
        hm = f["hmtx"].metrics
        self.adv = {ch: hm[cmap[ord(ch)]][0] / self.upm * 1000 for ch in SIGMA + " " if ord(ch) in cmap}
        os2 = f["OS/2"]
        self.ascent = os2.sTypoAscender / self.upm * 1000
        self.descent = os2.sTypoDescender / self.upm * 1000
        self.capheight = getattr(os2, "sCapHeight", 714) / self.upm * 1000
        self.bbox = [f["head"].xMin, f["head"].yMin, f["head"].xMax, f["head"].yMax]

    def yext(self, text: str) -> tuple[float, float]:  # ink extent in 1/1000 text space units
        if not hasattr(self, "_yx"):
            f = TTFont(str(CAND_FONT))
            cmap, glyf = f.getBestCmap(), f["glyf"]
            self._yx = {}
            for ch in SIGMA:
                g = glyf[cmap[ord(ch)]]
                if g.numberOfContours:
                    self._yx[ch] = (g.yMin / self.upm * 1000, g.yMax / self.upm * 1000)
        ys = [self._yx[c] for c in text if c in self._yx]
        return min(y[0] for y in ys), max(y[1] for y in ys)

    def width(self, text: str) -> float:  # in 1/1000 text space units
        return sum(self.adv[c] for c in text)

    def embed(self, pdf: pikepdf.Pdf) -> pikepdf.Object:
        opts = ftsubset.Options()
        opts.name_IDs = ["*"]
        opts.notdef_outline = True
        opts.layout_features = []
        f = TTFont(str(CAND_FONT))
        sub = ftsubset.Subsetter(opts)
        sub.populate(text=SIGMA + " ")
        sub.subset(f)
        buf = io.BytesIO()
        f.save(buf)
        data = buf.getvalue()
        ff = pdf.make_stream(data)
        ff.Length1 = len(data)
        widths = [round(self.adv.get(chr(c), 0)) for c in range(32, 127)]
        fd = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name("/AAAAAA+NotoSans-Condensed"),
                               Flags=32, FontBBox=[int(v * 1000 / self.upm) for v in self.bbox], ItalicAngle=0,
                               Ascent=int(self.ascent), Descent=int(self.descent), CapHeight=int(self.capheight), StemV=80, FontFile2=ff))
        lines = "".join(f"<{ord(c):02X}> <{ord(c):04X}>\n" for c in sorted(SIGMA + " "))
        cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
                f"{len(SIGMA) + 1} beginbfchar\n{lines}endbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
        return pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType,
                                 BaseFont=pikepdf.Name("/AAAAAA+NotoSans-Condensed"), FirstChar=32, LastChar=126, Widths=widths,
                                 Encoding=pikepdf.Name.WinAnsiEncoding, FontDescriptor=fd, ToUnicode=pdf.make_stream(cmap.encode())))


CAND = None


def cand() -> Candidate:
    global CAND
    if CAND is None:
        CAND = Candidate()
    return CAND


# ---------------------------------------------------------------- planning
@dataclass
class Sel:
    sid: str
    page: int
    start: int
    end: int
    action: str
    value: str
    substitute: str
    glyphs: list[Glyph]
    zone: dict
    font: str = ""  # original | candidate
    attempt: int = 0
    scale: float = 1.0
    zone_kind: str = "Z1"
    x0: float = 0.0
    zx0: float = 0.0
    orig_adv_page: float = 0.0
    sub_nat_page: float = 0.0


@dataclass
class LinePlan:
    page: int
    sels: list[Sel]
    kind: str
    f: float
    x0: float
    r: float
    moved: list[Glyph] = field(default_factory=list)
    layout: dict = field(default_factory=dict)  # gid -> new origin x; sid -> new origin x


def _kpage(g: Glyph) -> float:
    """Page units per text-space unit along the baseline at Th = 1 (font size included)."""
    trm = mmul(mmul([g.size, 0, 0, g.size, 0, 0], g.tm), g.ctm)
    return math.hypot(trm[0], trm[1])


def _adv_page(g: Glyph) -> float:
    return g.adv * _kpage(g) / g.size if g.size else 0.0


def localize(ex: Extraction, oracle: dict, sel: dict) -> tuple[int, int]:
    occ = next(o for o in oracle["occurrences"] if o["id"] == sel["id"])
    if occ["status"] != "ok":
        raise PlanFail("oracle_unavailable", sel["id"])
    if occ.get("split_across_lines"):
        raise PlanFail("selection_crosses_boundary", f"{sel['id']} split across lines")
    pm = ex.pages[sel["page"] - 1]
    boxes = [o["box"] for o in occ["pdfium_chars"]]
    gl = {g.gid: g for g in pm.glyphs}
    cands = []
    v = sel["value"]
    s = pm.text
    k = s.find(v)
    while k >= 0:
        gs = [gl[x["gid"]] for x in pm.segments if x["gid"] is not None and x["unit_start"] < k + len(v) and x["unit_end"] > k]
        ink = [g for g in gs if not g.uni.isspace()]
        ok = ink and all(any(b[0] - 1 <= (qbox(g)[0] + qbox(g)[2]) / 2 <= b[2] + 1 and qbox(g)[1] - 1 <= b[3] and qbox(g)[3] + 1 >= b[1] for b in boxes) for g in ink)
        if ok:
            cands.append((k, k + len(v)))
        k = s.find(v, k + 1)
    if len(cands) != 1:
        raise PlanFail("localization_ambiguous" if cands else "not_localizable", f"{sel['id']} candidates={len(cands)}")
    return cands[0]


def _font_codes(ex: Extraction, g: Glyph, text: str) -> list[bytes] | None:
    f = ex.interp.fonts[g.font_key]
    rev = f.reverse()
    out = []
    i = 0
    while i < len(text):
        for L in (3, 2, 1):  # longest first (ligatures n:1)
            piece = text[i:i + L]
            if len(piece) == L and piece in rev:
                codes = rev[piece]
                if len(codes) != 1:
                    return None
                out.append(codes[0])
                i += L
                break
        else:
            return None
    return out


def _program_glyphs(ex: Extraction, g: Glyph) -> dict[bytes, bool] | None:
    """code -> the embedded program has a non-empty glyph for it (TrueType/CIDFontType2 only; else None)."""
    fobj = next((o for o in ex.pdf.objects if isinstance(o, pikepdf.Dictionary) and o.objgen == g.font_key), None)
    if fobj is None:
        return None
    desc = fobj.DescendantFonts[0] if "/DescendantFonts" in fobj else fobj
    fd = desc.get("/FontDescriptor")
    ff = fd.get("/FontFile2") if fd is not None else None
    if ff is None:
        return None
    try:
        tt = TTFont(io.BytesIO(ff.read_bytes()))
    except Exception:
        return None
    glyf = tt["glyf"] if "glyf" in tt else None
    order = tt.getGlyphOrder()

    def nonempty(gid: int) -> bool:
        if glyf is None or gid <= 0 or gid >= len(order):
            return False
        gl = glyf[order[gid]]
        return gl.numberOfContours != 0

    f = ex.interp.fonts[g.font_key]
    out: dict[bytes, bool] = {}
    if f.kind == "type0":
        c2g = desc.get("/CIDToGIDMap")
        for code in f.to_u:
            cid = int.from_bytes(code, "big")
            gid = cid if (c2g is None or str(c2g) == "/Identity") else None
            out[code] = gid is not None and nonempty(gid)
        return out
    cm = None
    for tab in tt["cmap"].tables if "cmap" in tt else []:
        if (tab.platformID, tab.platEncID) in ((3, 0), (1, 0)):
            cm = tab.cmap
            break
    for code in f.to_u:
        c = code[0]
        gname = cm.get(c) or cm.get(0xF000 + c) if cm else None
        out[code] = gname is not None and nonempty(tt.getGlyphID(gname))
    return out


_INK: dict = {}


def ink_box(ex: Extraction, g: Glyph) -> list[float]:
    """Glyph ink box in page space from the embedded TrueType program (as PDFium reports it); advance box otherwise."""
    key = g.font_key
    if key not in _INK:
        _INK[key] = None
        fobj = next((o for o in ex.pdf.objects if isinstance(o, pikepdf.Dictionary) and o.objgen == key), None)
        if fobj is not None:
            desc = fobj.DescendantFonts[0] if "/DescendantFonts" in fobj else fobj
            fd = desc.get("/FontDescriptor")
            ff = fd.get("/FontFile2") if fd is not None else None
            if ff is not None:
                try:
                    tt = TTFont(io.BytesIO(ff.read_bytes()))
                    cm = None
                    for tab in tt["cmap"].tables if "cmap" in tt else []:
                        if (tab.platformID, tab.platEncID) in ((3, 0), (1, 0)):
                            cm = tab.cmap
                            break
                    _INK[key] = (tt, cm, "/DescendantFonts" in fobj)
                except Exception:
                    _INK[key] = None
    info = _INK[key]
    if info is None:
        return qbox(g)
    tt, cm, cid = info
    order = tt.getGlyphOrder()
    if cid:
        gid = int.from_bytes(g.code, "big")
    else:
        name = (cm.get(g.code[0]) or cm.get(0xF000 + g.code[0])) if cm else None
        gid = tt.getGlyphID(name) if name else 0
    if gid <= 0 or gid >= len(order):
        return qbox(g)
    gl = tt["glyf"][order[gid]]
    if gl.numberOfContours == 0 or not hasattr(gl, "xMin"):
        return qbox(g)
    upm = tt["head"].unitsPerEm
    trm = mmul(mmul([g.size * g.th, 0, 0, g.size, 0, g.ts], g.tm), g.ctm)
    pts = [apply(trm, x / upm, y / upm) for x, y in ((gl.xMin, gl.yMin), (gl.xMax, gl.yMax), (gl.xMin, gl.yMax), (gl.xMax, gl.yMin))]
    return [min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts)]


def _orig_eligible(ex: Extraction, g: Glyph) -> bool:
    codes = _font_codes(ex, g, SIGMA)
    if codes is None:
        return False
    have = _program_glyphs(ex, g)
    return have is not None and all(have.get(c, False) for c in codes)


def _orig_width(ex: Extraction, g: Glyph, text: str) -> float:
    f = ex.interp.fonts[g.font_key]
    return sum(f.width(c) for c in _font_codes(ex, g, text))


def plan(ex: Extraction, oracle: dict, sels: list[dict], policy: dict, tokens: dict[str, str], zone_kinds=("Z1", "Z2")) -> list[LinePlan]:
    """Localize, group (A06), pick zone/font by the frozen attempt order (§0.5). Raises PlanFail."""
    by_page: dict[int, list] = {}
    for s in sels:
        st, en = localize(ex, oracle, s)
        by_page.setdefault(s["page"], []).append((s, st, en))
    plans: list[LinePlan] = []
    for page, items in sorted(by_page.items()):
        pm = ex.pages[page - 1]
        findings = [(s["type_id"], st, en) for s, st, en in items]
        groups = plan_protection(findings, len(pm.text), policy["default_action"], policy["actions"])
        gl = {g.gid: g for g in pm.glyphs}
        out_sels: list[Sel] = []
        for grp in groups:
            src = [s for s, st, en in items if st >= grp.start and en <= grp.end]
            seg = [x for x in pm.segments if x["unit_start"] < grp.end and x["unit_end"] > grp.start]
            if any(x["kind"] == "synthesized_line" for x in seg):
                raise PlanFail("selection_crosses_boundary", "line separator inside the selection")
            if any(x["unit_start"] < grp.start or x["unit_end"] > grp.end for x in seg if x["gid"] is not None):
                raise PlanFail("localization_ambiguous", "partial piece (ligature)")
            glyphs = [gl[x["gid"]] for x in seg if x["gid"] is not None]
            frag = pm.text[grp.start:grp.end]
            if any(unicodedata.bidirectional(ch) in ("R", "AL", "RLE", "RLO", "RLI") for ch in frag):
                raise PlanFail("selection_not_transformable", "right-to-left")
            if any(g.at_span for g in glyphs):
                raise PlanFail("selection_not_transformable", "selection inside an /ActualText span (variant AT)")
            if any(g.in_form for g in glyphs) or len({g.skey for g in glyphs}) != 1:
                raise PlanFail("selection_not_transformable", "selection across streams or inside a Form XObject (prototype)")
            g0 = glyphs[0]
            if g0.dirv[0] <= 0 or abs(g0.dirv[1]) > 1e-6 * abs(g0.dirv[0]):
                raise PlanFail("zone_plan_unjustified", "orientation other than horizontal left-to-right (prototype)")
            if grp.action == "redact":
                sub = A06.REDACTED
            else:
                if frag not in tokens:
                    tokens[frag] = A06.new_token()
                sub = A06.marker_of(tokens[frag])
            zsrc = src[0]
            occ = next(o for o in oracle["occurrences"] if o["id"] == zsrc["id"])
            se = Sel(zsrc["id"], page, grp.start, grp.end, grp.action, frag, sub, glyphs, occ["zone"])
            se.x0 = g0.origin[0]
            se.zx0 = max(occ["zone"]["x0"], g0.origin[0])
            last = glyphs[-1]
            se.orig_adv_page = last.origin[0] + _adv_page(last) - g0.origin[0]
            out_sels.append(se)
        # group selections by line (oracle band)
        lines: list[list[Sel]] = []
        for se in sorted(out_sels, key=lambda z: (-z.zone["band"][1], z.x0)):
            if lines and abs(lines[-1][0].zone["band"][0] - se.zone["band"][0]) < 0.5 and abs(lines[-1][0].zone["band"][1] - se.zone["band"][1]) < 0.5:
                lines[-1].append(se)
            else:
                lines.append([se])
        for line in lines:
            line.sort(key=lambda z: z.x0)
            plans.append(_line_plan(ex, pm, line, zone_kinds))
    return plans


def _sub_width(ex, se: Sel, font: str) -> float:
    g0 = se.glyphs[0]
    k = _kpage(g0) / g0.size
    w = cand().width(se.substitute) if font == "candidate" else _orig_width(ex, g0, se.substitute)
    return w / 1000.0 * g0.size * k


def _fits_vertically(ex, se: Sel, font: str) -> bool:
    """The substitute's ink stays inside the zone band (+0.25 pt), at the value's size and baseline (§7.3, VO)."""
    g0 = se.glyphs[0]
    trm = mmul(mmul([g0.size, 0, 0, g0.size, 0, g0.ts], g0.tm), g0.ctm)
    yscale = math.hypot(trm[2], trm[3])
    if font == "candidate":
        lo, hi = cand().yext(se.substitute)
    else:
        info = _INK.get(g0.font_key)
        ink_box(ex, g0)
        info = _INK.get(g0.font_key)
        if not info:
            return False
        tt, cm, cid = info
        order, glyf, upm = tt.getGlyphOrder(), tt["glyf"], tt["head"].unitsPerEm
        ys = []
        for code in _font_codes(ex, g0, se.substitute):
            gid = int.from_bytes(code, "big") if cid else tt.getGlyphID((cm.get(code[0]) or cm.get(0xF000 + code[0])))
            gl = glyf[order[gid]]
            if gl.numberOfContours:
                ys.append((gl.yMin / upm * 1000, gl.yMax / upm * 1000))
        lo, hi = min(y[0] for y in ys), max(y[1] for y in ys)
    base = apply(trm, 0, 0)[1]
    band = se.zone["band"]
    return base + lo / 1000 * yscale >= band[0] - 0.25 and base + hi / 1000 * yscale <= band[1] + 0.25


def _line_plan(ex, pm, line: list[Sel], zone_kinds) -> LinePlan:
    elig = _orig_eligible(ex, line[0].glyphs[0])
    fonts = (["original"] if elig else []) + ["candidate"]
    if "Z1" in zone_kinds:
        ok = True
        for se in line:
            done = False
            for att, font in ((1, "original"), (2, "candidate")):
                if font not in fonts:
                    continue
                nat = _sub_width(ex, se, font)
                avail = se.zone["r_z1"] - se.zx0
                s = min(1.0, avail / nat) if nat > 0 else 1.0
                if s >= MIN_SCALE and _fits_vertically(ex, se, font):
                    se.font, se.attempt, se.scale, se.zone_kind, se.sub_nat_page = font, att, s, "Z1", nat
                    done = True
                    break
            ok = ok and done
        if ok:
            lp = LinePlan(pm.index + 1, line, "Z1", 1.0, line[0].zx0, max(se.zone["r_z1"] for se in line))
            for se in line:
                lp.layout[se.sid] = se.zx0
            return lp
    if "Z2" not in zone_kinds:
        raise PlanFail("replacement_overflow", "Z1 only")
    x0 = line[0].zx0
    r = line[-1].zone["r_line"]
    band = [min(se.zone["band"][0] for se in line), max(se.zone["band"][1] for se in line)]
    sel_ids = {g.gid for se in line for g in se.glyphs}
    def cx(g):
        b = ink_box(ex, g)
        return (b[0] + b[2]) / 2

    on_line = [g for g in pm.glyphs if g.gid not in sel_ids and not g.uni.isspace() and band[0] <= (ink_box(ex, g)[1] + ink_box(ex, g)[3]) / 2 <= band[1] and cx(g) >= x0]
    # moved = non-selected glyphs whose centre lies in the zone (as the verifier counts them); the layout must also end
    # before the first glyph of the line that stays outside the zone
    moved = [g for g in on_line if cx(g) <= r]
    stay = [ink_box(ex, g)[0] for g in on_line if cx(g) > r]
    moved += [g for g in pm.glyphs if g.uni.isspace() and g.gid not in sel_ids and band[0] <= (qbox(g)[1] + qbox(g)[3]) / 2 <= band[1]
              and x0 <= g.origin[0] and (not stay or g.origin[0] < min(stay))]
    if stay:
        r = min(r, min(stay))
    for g in moved:
        if g.in_form or g.skey != line[0].glyphs[0].skey:
            raise PlanFail("zone_plan_unjustified", "moved glyph in another stream or Form XObject")
        if abs(g.dirv[1]) > 1e-6 or g.dirv[0] <= 0:
            raise PlanFail("zone_plan_unjustified", "moved glyph orientation")
    for p in pm.painted:
        b = p.bbox
        if b[0] >= x0 - 0.01 and b[2] <= r + 0.01 and b[1] >= band[0] - 0.01 and b[3] <= band[1] + 0.01:
            raise PlanFail("zone_plan_unjustified", "painted object inside the moved region")
    for an in ex.pdf.pages[pm.index].obj.get("/Annots", []):
        if str(an.get("/Subtype", "")) == "/Link":
            rb = [float(v) for v in an.Rect]
            rb = [min(rb[0], rb[2]), min(rb[1], rb[3]), max(rb[0], rb[2]), max(rb[1], rb[3])]
            if rb[0] < r and rb[2] > x0 and rb[1] < band[1] and rb[3] > band[0]:
                raise PlanFail("zone_plan_unjustified", "link annotation over the moved region")
    vertical_fail = False
    for att, font in ((3, "original"), (4, "candidate")):
        if font not in fonts:
            continue
        if not all(_fits_vertically(ex, se, font) for se in line):
            vertical_fail = True
            continue
        items = []
        for se in line:
            items.append(("sel", se.zx0, se.orig_adv_page + (se.x0 - se.zx0), se, _sub_width(ex, se, font)))
        for g in moved:
            items.append(("g", g.origin[0], _adv_page(g), g, None))
        items.sort(key=lambda t: t[1])
        D = 0.0
        newx: dict[Any, float] = {}
        end = x0
        for kind, x, w, obj, wsub in items:
            nx = x + D
            if kind == "sel":
                newx[obj.sid] = nx
                end = max(end, nx + wsub)
                D += wsub - w
            else:
                newx[obj.gid] = nx
                end = max(end, nx + w)
        f = min(1.0, (r - x0) / (end - x0)) if end > x0 else 1.0
        if f >= MIN_SCALE:
            lp = LinePlan(pm.index + 1, line, "Z2", f, x0, r, moved)
            lp.layout = {k: x0 + f * (v - x0) for k, v in newx.items()}
            for se in line:
                se.font, se.attempt, se.scale, se.zone_kind = font, att, f, "Z2"
                se.sub_nat_page = _sub_width(ex, se, font)
            return lp
    raise PlanFail("replacement_overflow", "no attempt fits (Z1 and Z2)" + (": substitute taller than the zone band" if vertical_fail else ""))


def _runs(se: Sel) -> list[dict]:
    """Font runs of the value (no text): resource, size, Th, number of code points and advance in page units."""
    out: list[dict] = []
    for g in se.glyphs:
        key = (g.font, round(g.size, 6), round(g.th, 6))
        if out and out[-1]["key"] == key:
            out[-1]["n"] += len(g.uni)
            out[-1]["adv"] += _adv_page(g)
        else:
            out.append({"key": key, "font": g.font, "size": g.size, "th": g.th, "n": len(g.uni), "adv": _adv_page(g)})
    for r in out:
        del r["key"]
    return out


# ---------------------------------------------------------------- content-stream editing
def _n(x: float) -> Decimal:
    return Decimal(f"{x:.6f}").normalize() if x == x else Decimal(0)


def _op(name: str, *operands) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction([o if isinstance(o, pikepdf.Object) else o for o in operands], pikepdf.Operator(name))


def _tm(m) -> list:
    return [_n(v) for v in m]


def _fill_ops(raw) -> list:
    return [_op(n, *ops) for n, ops in raw]


def _rebuild_show(ins, rec, removed: dict[tuple[int, int], float], split: tuple[int, int] | None = None, block: list | None = None) -> list:
    """Show operator with the removed codes replaced by TJ numbers equal to their advance. With `split`, the operator is
    cut right before that code and `block` is inserted there, so content order follows the visual order."""
    op = str(ins.operator)
    a = ins.operands
    pre = []
    if op == "'":
        pre = [_op("T*")]
    elif op == '"':
        pre = [_op("Tw", a[0]), _op("Tc", a[1]), _op("T*")]
    elems = list(a[0]) if op == "TJ" else [a[-1]]
    size, th = rec["size"], rec["th"]
    parts: list[list] = [[]]

    def add_num(num: float) -> None:
        cur = parts[-1]
        if cur and isinstance(cur[-1], Decimal):
            cur[-1] = _n(float(cur[-1]) + num)
        else:
            cur.append(_n(num))

    for ei, el in enumerate(elems):
        if isinstance(el, pikepdf.String):
            data = bytes(el)
            n = 2 if rec.get("code_len") == 2 else 1
            cur = b""
            pos = 0
            while pos < len(data):
                key = (ei, pos)
                if split is not None and key == split:
                    if cur:
                        parts[-1].append(pikepdf.String(cur))
                        cur = b""
                    parts.append([])
                if key in removed:
                    if cur:
                        parts[-1].append(pikepdf.String(cur))
                        cur = b""
                    add_num(-removed[key] / (size * th) * 1000.0)
                else:
                    cur += data[pos:pos + n]
                pos += n
            if cur:
                parts[-1].append(pikepdf.String(cur))
        else:
            parts[-1].append(el)
    out = list(pre)
    for k, part in enumerate(parts):
        if k:
            out += block or []
        if part:
            out.append(_op("TJ", pikepdf.Array(part)))
    return out


def _block(ex: Extraction, lp: LinePlan, rec_after: dict, codes_fn) -> list:
    """Drawing block inserted after the operator holding the first selected glyph (same BT, same CTM)."""
    g0 = lp.sels[0].glyphs[0]
    ins: list = []
    ctm = g0.ctm
    G0 = mmul(g0.tm, ctm)

    def tm_at(g: Glyph, x_new: float):
        G = mmul(g.tm, g.ctm)
        dx = x_new - g.origin[0]
        G2 = mmul(G, [1, 0, 0, 1, dx, 0])
        return mmul(G2, minv(ctm))

    _ = G0
    draws = []
    for se in lp.sels:
        draws.append(("sel", lp.layout[se.sid], se))
    for g in lp.moved:
        draws.append(("g", lp.layout[g.gid], g))
    draws.sort(key=lambda t: t[1])
    for kind, x, obj in draws:
        if kind == "sel":
            se = obj
            g = se.glyphs[0]
            fname = CAND_RES if se.font == "candidate" else g.font
            ins += _fill_ops(ex.interp.opstate[(g.page, g.skey, g.op)]["fill_raw"])
            ins += [_op("Tf", pikepdf.Name(fname), _n(g.size)), _op("Tz", _n(se.scale * 100.0)), _op("Tc", 0), _op("Tw", 0),
                    _op("Ts", _n(g.ts)), _op("Tr", g.tr), _op("Tm", *_tm(tm_at(g, x))), _op("Tj", pikepdf.String(codes_fn(se)))]
        else:
            g = obj
            ins += _fill_ops(ex.interp.opstate[(g.page, g.skey, g.op)]["fill_raw"])
            ins += [_op("Tf", pikepdf.Name(g.font), _n(g.size)), _op("Tz", _n(g.th * lp.f * 100.0)), _op("Tc", 0), _op("Tw", 0),
                    _op("Ts", _n(g.ts)), _op("Tr", g.tr), _op("Tm", *_tm(tm_at(g, x))), _op("Tj", pikepdf.String(g.code))]
    r = rec_after
    ins += _fill_ops(r["fill_raw"])
    ins += [_op("Tf", pikepdf.Name(r["fname"]), _n(r["size"])), _op("Tz", _n(r["th"] * 100.0)), _op("Tc", _n(r["tc"])),
            _op("Tw", _n(r["tw"])), _op("Ts", _n(r["ts"])), _op("Tr", r["tr"]), _op("Tm", *_tm(r["tlm"]))]
    rel = mmul(g0.tm, minv(r["tlm"]))
    dx = rel[4]
    if abs(rel[5]) > 1e-6 or abs(rel[0] - 1) > 1e-6:
        raise PlanFail("zone_plan_unjustified", "text matrix not recoverable")
    if abs(dx) > 1e-9:
        ins.append(_op("TJ", pikepdf.Array([_n(-dx / (r["size"] * r["th"]) * 1000.0)])))
    return ins


@dataclass
class Result:
    data: bytes
    binding: dict
    removed_classes: dict


def remove_e(pdf: pikepdf.Pdf) -> dict:
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
        if isinstance(o, (pikepdf.Dictionary, pikepdf.Stream)) and "/Metadata" in o:
            del o["/Metadata"]
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
            p.obj.Annots = pdf.make_indirect(pikepdf.Array(keep)) if keep else pikepdf.Array()
            if not keep:
                del p.obj["/Annots"]
    if c:
        out["comments"] = c
    return out


def protect(data: bytes, ex: Extraction, plans: list[LinePlan]) -> Result:
    pdf = pikepdf.open(io.BytesIO(data))
    need_cand = any(se.font == "candidate" for lp in plans for se in lp.sels)
    cfont = cand().embed(pdf) if need_cand else None
    edits: dict[tuple, dict] = {}
    binding = {"occurrences": [], "redactions": [], "lines": []}
    for lp in plans:
        g0 = lp.sels[0].glyphs[0]
        pg = ex.pages[lp.page - 1]
        e = edits.setdefault((lp.page, g0.skey), {"removed": {}, "blocks": {}})
        for g in [g for se in lp.sels for g in se.glyphs] + lp.moved:
            e["removed"].setdefault(g.op, {})[(g.elem, g.cstart)] = g.adv
        rec_after = dict(ex.interp.opstate[(g0.page, g0.skey, g0.op)])
        rec_after["code_len"] = 2 if ex.interp.fonts[g0.font_key].code_len == 2 else 1

        def codes(se: Sel) -> bytes:
            if se.font == "candidate":
                return se.substitute.encode("ascii")
            return b"".join(_font_codes(ex, se.glyphs[0], se.substitute))

        e["blocks"].setdefault(g0.op, []).append((len(binding["lines"]), _block(ex, lp, rec_after, codes), (g0.elem, g0.cstart)))
        if cfont is not None:
            res = pdf.pages[lp.page - 1].Resources
            if "/Font" not in res:
                res.Font = pikepdf.Dictionary()
            res.Font[CAND_RES] = cfont
        line = {"page": lp.page, "kind": lp.kind, "f": lp.f, "x0": lp.x0, "r": lp.r, "stream": g0.skey,
                "sels": [{"sid": se.sid, "action": se.action, "font": se.font, "attempt": se.attempt, "scale": se.scale,
                          "x_new": lp.layout[se.sid], "orig_adv_page": se.orig_adv_page, "sub_nat_page": se.sub_nat_page,
                          "value_font": se.glyphs[0].font, "size": se.glyphs[0].size, "th": se.glyphs[0].th, "unit_start": se.start,
                          "x_orig": se.x0, "span_page": se.orig_adv_page + (se.x0 - se.zx0), "substitute": se.substitute,
                          "runs": _runs(se),
                          "token": A06.token_of(se.substitute) if se.action == "pseudonymize" else None} for se in lp.sels],
                "moved": [{"x_new": lp.layout[g.gid], "th": g.th} for g in sorted(lp.moved, key=lambda q: lp.layout[q.gid])]}
        binding["lines"].append(line)
        _ = pg
    for (page, skey), e in edits.items():
        pm = ex.pages[page - 1]
        sobj = pdf.pages[page - 1].obj.Contents
        if skey.startswith("page:"):
            k = int(skey.split(":")[2])
            target = sobj[k] if isinstance(sobj, pikepdf.Array) else sobj
        else:
            raise PlanFail("selection_not_transformable", "Form XObject edit (prototype)")
        ops = pikepdf.parse_content_stream(target)
        new = []
        for i, ins in enumerate(ops):
            blocks = e["blocks"].get(i, [])
            if len(blocks) > 1:
                raise PlanFail("zone_plan_unjustified", "two line plans start in the same operator (prototype)")
            if i in e["removed"]:
                rec = dict(ex.interp.opstate[(page - 1, skey, i)])
                rec["code_len"] = 2 if any(g.op == i and g.skey == skey and len(g.code) == 2 for g in pm.glyphs) else 1
                if blocks:
                    li, blk, split = blocks[0]
                    marker = [_op("BX"), _op("EX")]
                    tmp = _rebuild_show(ins, rec, e["removed"][i], split, marker)
                    k = next(j for j, x in enumerate(tmp) if str(x.operator) == "BX")
                    tmp = tmp[:k] + blk + tmp[k + 2:]
                    binding["lines"][li]["block"] = [len(new) + k, len(new) + k + len(blk)]
                    new += tmp
                else:
                    new += _rebuild_show(ins, rec, e["removed"][i])
            else:
                new.append(ins)
        target.write(pikepdf.unparse_content_stream(new))
    removed = remove_e(pdf)
    out = io.BytesIO()
    pdf.save(out, object_stream_mode=pikepdf.ObjectStreamMode.disable, compress_streams=True,
             stream_decode_level=pikepdf.StreamDecodeLevel.generalized, deterministic_id=True)
    return Result(out.getvalue(), binding, removed)


# ---------------------------------------------------------------- mechanical restoration (derivative + binding + values)
def _page_point(ctm, tm) -> float:
    return apply(mmul(tm, ctm), 0, 0)[0]


def restore(derivative: bytes, binding: dict, values: dict[str, str]) -> bytes:
    """values: token -> original value (the context, simulated). Needs neither the original nor the analysis."""
    from .gate import extract as _extract
    from .pdfmodel import load_font

    ex = _extract(derivative)
    # T5: strict markers exactly at the bound occurrences; no recognizable alteration anywhere
    found = []
    for pm in ex.pages:
        sc = A06.scan(pm.text)
        if sc.altered:
            raise PlanFail("derivative_markers_invalid", "altered marker")
        found += [(pm.index + 1, a, b, A06.token_of(mk)) for a, b, mk in sc.strict]
    bound = sorted((o["page"], o["start"], o["end"], o["token"]) for o in binding["occurrences"])
    if sorted(found) != bound:
        raise PlanFail("derivative_markers_invalid", "markers differ from the binding")
    pdf = pikepdf.open(io.BytesIO(derivative))
    by_stream: dict[tuple, list] = {}
    for line in binding["lines"]:
        by_stream.setdefault((line["page"], line["stream"]), []).append(line)
    for (page, skey), lines in by_stream.items():
        k = int(skey.split(":")[2])
        cont = pdf.pages[page - 1].obj.Contents
        target = cont[k] if isinstance(cont, pikepdf.Array) else cont
        ops = list(pikepdf.parse_content_stream(target))
        res = pdf.pages[page - 1].Resources
        # CTM inside the block = CTM at the block start, from the derivative model
        for line in sorted(lines, key=lambda q: -q["block"][0]):
            b0, b1 = line["block"]
            g_ctm = _ctm_at(ex, page - 1, skey, b0)
            blk = ops[b0:b1]
            draws, cur = [], []
            for ins in blk:
                cur.append(ins)
                if str(ins.operator) == "Tj":
                    draws.append(cur)
                    cur = []
            tail = cur
            items = sorted([("sel", sl["x_new"], sl) for sl in line["sels"]] + [("g", mv["x_new"], mv) for mv in line["moved"]], key=lambda t: t[1])
            if len(items) != len(draws):
                raise PlanFail("derivative_markers_invalid", "block shape")
            sels_sorted = sorted(line["sels"], key=lambda q: q["x_orig"])
            out = []
            for (kind, xn, it), d in zip(items, draws):
                tm_ins = next(x for x in d if str(x.operator) == "Tm")
                tm = [float(v) for v in tm_ins.operands]
                x_now = _page_point(g_ctm, tm)
                if kind == "sel":
                    x_to = it["x_orig"]
                    val = values[it["token"]] if it["token"] else None
                    if val is None:
                        if line["kind"] == "Z2" and "RZ" in VARIANT:
                            # informative RZ: the redaction keeps its text but returns, with its line, to the value's
                            # original start, compressed to the original span if needed (>= MIN_SCALE)
                            sc = min(1.0, it["span_page"] / it["sub_nat_page"]) if it["sub_nat_page"] else 1.0
                            if sc < MIN_SCALE:
                                raise PlanFail("restoration_layout_unavailable", "redaction does not fit its original span")
                            keep = [x for x in d if str(x.operator) not in ("Tz", "Tm")]
                            tj = keep.pop()
                            out += keep + [_op("Tz", _n(sc * 100.0)), _op("Tm", *_tm(_shift(tm, g_ctm, it["x_orig"] - x_now))), tj]
                        else:
                            out += d  # redaction: definitive, kept where the derivative drew it (frozen rule §0.4)
                        continue
                    if sum(r["n"] for r in it["runs"]) != len(val):
                        raise PlanFail("derivative_markers_invalid", "runs do not match the value length")
                    keep = [x for x in d if str(x.operator) not in ("Tf", "Tz", "Tm", "Tj")]
                    out += keep
                    pos, x = 0, x_to
                    for run in it["runs"]:
                        piece = val[pos:pos + run["n"]]
                        pos += run["n"]
                        vf = load_font(run["font"], res.Font[run["font"]])
                        rev = vf.reverse()
                        codes, i = [], 0
                        while i < len(piece):
                            for L in (3, 2, 1):
                                pc = piece[i:i + L]
                                if len(pc) == L and pc in rev and len(rev[pc]) == 1:
                                    codes.append(rev[pc][0])
                                    i += L
                                    break
                            else:
                                raise PlanFail("restoration_encoding_unavailable", "value not encodable in its original font")
                        k_page = math.hypot(*mmul(tm, g_ctm)[0:2])
                        w_nat = sum(vf.width(cd) for cd in codes) / 1000.0 * run["size"] * k_page
                        th = run["adv"] / w_nat if w_nat else 1.0
                        out += [_op("Tf", pikepdf.Name(run["font"]), _n(run["size"])), _op("Tz", _n(th * 100.0)),
                                _op("Tm", *_tm(_shift(tm, g_ctm, x - x_now))), _op("Tj", pikepdf.String(b"".join(codes)))]
                        x += run["adv"]
                else:
                    f, x0 = line["f"], line["x0"]
                    xnat = x0 + (x_now - x0) / f
                    D = sum(sl["sub_nat_page"] - sl["orig_adv_page"] for sl in sels_sorted if sl["x_orig"] < xnat - 1e-6 and (sl["x_orig"] + sl["sub_nat_page"]) <= xnat + 1e-3 + 1e9)
                    D = 0.0
                    for sl in sels_sorted:
                        if sl["x_new"] < x_now:
                            D += sl["sub_nat_page"] - sl["span_page"]
                    x_to = xnat - D
                    new_tm = _shift(tm, g_ctm, x_to - x_now)
                    keep = [x for x in d if str(x.operator) not in ("Tz", "Tm")]
                    tj = keep.pop()
                    out += keep + [_op("Tz", _n(it["th"] * 100.0)), _op("Tm", *_tm(new_tm)), tj]
            ops[b0:b1] = out + tail
        target.write(pikepdf.unparse_content_stream(ops))
    o = io.BytesIO()
    pdf.save(o, object_stream_mode=pikepdf.ObjectStreamMode.disable, compress_streams=True,
             stream_decode_level=pikepdf.StreamDecodeLevel.generalized, deterministic_id=True)
    return o.getvalue()


def _shift(tm, ctm, dx_page: float):
    G = mmul(tm, ctm)
    return mmul(mmul(G, [1, 0, 0, 1, dx_page, 0]), minv(ctm))


def _ctm_at(ex, pidx: int, skey: str, op_index: int):
    best = None
    for (pi, sk, i), rec in ex.interp.opstate.items():
        if pi == pidx and sk == skey and i <= op_index:
            if best is None or i > best[0]:
                best = (i, rec["ctm"])
    if best is None:
        raise PlanFail("derivative_markers_invalid", "no text state before the block")
    return best[1]
