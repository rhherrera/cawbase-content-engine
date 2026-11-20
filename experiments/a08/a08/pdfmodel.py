"""Candidate PDF extractor v2 (prototype): a content-stream interpreter over pikepdf with full text state, simple and
Type0/Identity-H fonts, Form XObjects, /Contents arrays and geometry; plus the document gate of the A08 matrix.

Identity of the prototype extractor: cce-pdf-extract / 1 / document-structures/2 (protocol §0.9). Rejections raise
Reject(reason) with the closed reasons of the contract (A05 §10.4 + uncovered_content).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import pikepdf
from fontTools import agl
from fontTools.encodings.StandardEncoding import StandardEncoding

EXTRACTOR = {"id": "cce-pdf-extract", "version": "1", "document_structures": "document-structures/2"}
CONFIG = {"max_pages": 100, "max_form_depth": 8, "max_ops_per_page": 100000, "line_tol": 0.5, "space_gap": 0.15,
          "column_gap": 1.5, "decorative_rules": "surviving set supplied by the caller"}


VARIANT: set[str] = set()  # informative variants, never counted ("AT": /ActualText equal to its glyphs' text)


class Reject(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------- matrices
def mmul(a, b):
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def minv(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise Reject("unsupported_structure", "singular matrix")
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return [ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_)]


def apply(m, x, y):
    return (x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5])


IDENT = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


# ---------------------------------------------------------------- fonts
_HEX = re.compile(r"<([0-9A-Fa-f]+)>")


def parse_cmap(data: bytes) -> tuple[dict[bytes, str], list[int]]:
    """ToUnicode CMap -> {code bytes: unicode}, code lengths from codespacerange."""
    text = data.decode("latin-1")
    lens = sorted({len(a) // 2 for a, _ in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", " ".join(re.findall(r"begincodespacerange(.*?)endcodespacerange", text, re.S)))}) or [1]
    out: dict[bytes, str] = {}

    def u(h: str) -> str:
        b = bytes.fromhex(h)
        if len(b) % 2:
            return b.decode("latin-1")
        return b.decode("utf-16-be", "surrogatepass").encode("utf-16", "surrogatepass").decode("utf-16")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for s, d in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            out[bytes.fromhex(s)] = u(d) if d else ""
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            a, b = int(lo, 16), int(hi, 16)
            n = len(lo) // 2
            if b - a > 65535:
                raise Reject("font_mapping_insufficient", "range")
            if rest.startswith("["):
                for i, d in enumerate(_HEX.findall(rest)):
                    out[(a + i).to_bytes(n, "big")] = u(d)
            else:
                base = bytes.fromhex(rest[1:-1])
                for i in range(b - a + 1):
                    bb = bytearray(base)
                    v = int.from_bytes(bb[-2:], "big") + i if len(bb) >= 2 else bb[-1] + i
                    if len(bb) >= 2:
                        bb[-2:] = v.to_bytes(2, "big")
                    else:
                        bb[-1] = v & 0xFF
                    out[(a + i).to_bytes(n, "big")] = u(bytes(bb).hex())
    return out, lens


def _win_ansi(code: int) -> str | None:
    if code in (0x81, 0x8D, 0x8F, 0x90, 0x9D):
        return None
    try:
        return bytes([code]).decode("cp1252")
    except UnicodeDecodeError:
        return None


def _base_enc(name: str, code: int) -> str | None:
    if name == "/WinAnsiEncoding":
        return _win_ansi(code)
    if name == "/MacRomanEncoding":
        return bytes([code]).decode("mac_roman") if code >= 32 else None
    if name == "/StandardEncoding":
        g = StandardEncoding[code] if code < len(StandardEncoding) else ".notdef"
        return agl.toUnicode(g) if g != ".notdef" else None
    return None


def _decompose(u: str) -> str:
    if u and 0xFB00 <= ord(u[0]) <= 0xFB06 and len(u) == 1:
        return unicodedata.normalize("NFKC", u)  # presentation-form ligatures -> their letters (relation n:1)
    return u


@dataclass
class Font:
    name: str
    objgen: tuple
    kind: str  # simple | type0
    code_len: int
    to_u: dict[bytes, str]
    widths: dict[int, float]
    default_width: float
    ascent: float
    descent: float
    subtype: str

    def split(self, data: bytes) -> list[bytes]:
        n = self.code_len
        if len(data) % n:
            raise Reject("font_mapping_insufficient", "odd code length")
        return [data[i:i + n] for i in range(0, len(data), n)]

    def uni(self, code: bytes) -> str:
        if code not in self.to_u:
            raise Reject("font_mapping_insufficient", f"code {code.hex()} unmapped")
        u = self.to_u[code]
        if u == "" or "�" in u:
            raise Reject("font_mapping_insufficient", "empty/replacement")
        return u

    def width(self, code: bytes) -> float:
        return self.widths.get(int.from_bytes(code, "big"), self.default_width)

    def reverse(self) -> dict[str, list[bytes]]:
        r: dict[str, list[bytes]] = {}
        for k, v in self.to_u.items():
            r.setdefault(v, []).append(k)
        return r


def load_font(name: str, fd: Any) -> Font:
    sub = str(fd.get("/Subtype", ""))
    tu = fd.get("/ToUnicode")
    to_u: dict[bytes, str] = {}
    if sub == "/Type0":
        enc = fd.get("/Encoding")
        if not isinstance(enc, pikepdf.Name) or str(enc) != "/Identity-H":
            raise Reject("font_mapping_insufficient", "Type0 CMap other than Identity-H")
        desc = fd.DescendantFonts[0]
        if str(desc.get("/Subtype")) not in ("/CIDFontType2", "/CIDFontType0"):
            raise Reject("font_mapping_insufficient", "descendant")
        if tu is None:
            raise Reject("font_mapping_insufficient", "Type0 without ToUnicode")
        to_u, lens = parse_cmap(tu.read_bytes())
        if lens != [2]:
            raise Reject("font_mapping_insufficient", "Identity-H with non 2-byte ToUnicode")
        widths: dict[int, float] = {}
        w = desc.get("/W")
        if w is not None:
            w = list(w)
            i = 0
            while i < len(w):
                first = int(w[i])
                if isinstance(w[i + 1], pikepdf.Array):
                    for k, v in enumerate(w[i + 1]):
                        widths[first + k] = float(v)
                    i += 2
                else:
                    last, v = int(w[i + 1]), float(w[i + 2])
                    for k in range(first, last + 1):
                        widths[k] = v
                    i += 3
        dw = float(desc.get("/DW", 1000))
        fdesc = desc.get("/FontDescriptor")
        code_len = 2
    elif sub in ("/Type1", "/TrueType", "/MMType1"):
        if tu is not None:
            to_u, lens = parse_cmap(tu.read_bytes())
            if lens != [1]:
                raise Reject("font_mapping_insufficient", "simple font with multi-byte ToUnicode")
        else:
            enc = fd.get("/Encoding")
            base, diffs = None, {}
            if isinstance(enc, pikepdf.Name):
                base = str(enc)
            elif isinstance(enc, pikepdf.Dictionary):
                base = str(enc.get("/BaseEncoding", "/StandardEncoding"))
                code = 0
                for item in enc.get("/Differences", []):
                    if isinstance(item, (int, pikepdf.Object)) and not isinstance(item, pikepdf.Name):
                        code = int(item)
                    else:
                        diffs[code] = str(item)[1:]
                        code += 1
            else:
                raise Reject("font_mapping_insufficient", "no ToUnicode and no encoding")
            for code in range(256):
                u = agl.toUnicode(diffs[code]) if code in diffs else _base_enc(base or "", code)
                if u:
                    to_u[bytes([code])] = u
        fc = int(fd.get("/FirstChar", 0))
        ws = fd.get("/Widths")
        if ws is None:
            raise Reject("font_mapping_insufficient", "simple font without /Widths")
        widths = {fc + i: float(v) for i, v in enumerate(ws)}
        fdesc = fd.get("/FontDescriptor")
        dw = float(fdesc.get("/MissingWidth", 0)) if fdesc is not None else 0.0
        code_len = 1
    else:
        raise Reject("font_mapping_insufficient", f"font subtype {sub}")
    to_u = {k: _decompose(v) for k, v in to_u.items()}
    asc = float(fdesc.get("/Ascent", 800)) if fdesc is not None else 800.0
    des = float(fdesc.get("/Descent", -200)) if fdesc is not None else -200.0
    if asc <= 0:
        asc = 800.0
    if des >= 0:
        des = -200.0
    return Font(name, fd.objgen, "type0" if sub == "/Type0" else "simple", code_len, to_u, widths, dw, asc, des, sub)


# ---------------------------------------------------------------- interpreter records
@dataclass
class Glyph:
    gid: int
    page: int
    skey: str  # stream key "page:<p>:<k>" (k = index in /Contents array) or "xobj:<objnum>"
    chain: tuple
    op: int
    elem: int
    cstart: int
    cend: int
    code: bytes
    uni: str
    font: str
    font_key: tuple
    size: float
    th: float
    tc: float
    tw: float
    ts: float
    tr: int
    w0: float
    adv: float  # text-space advance including Tc/Tw and Th
    tm: list
    tlm: list
    ctm: list
    origin: tuple
    dirv: tuple
    quad: list
    fill: tuple
    in_form: bool
    mcid: Any = None
    at_span: bool = False


@dataclass
class Painted:
    kind: str  # path | image | shading | inline_image
    bbox: list
    order: int
    page: int
    skey: str
    op: int
    detail: dict = field(default_factory=dict)


@dataclass
class PageModel:
    index: int
    glyphs: list[Glyph]
    painted: list[Painted]
    streams: dict[str, list]  # skey -> parsed instructions
    stream_objs: dict[str, Any]
    text: str = ""
    segments: list = field(default_factory=list)
    cropbox: list = field(default_factory=list)


def _num(x) -> float:
    return float(x)


def _is_rect(path, ctm) -> bool:
    """One axis-aligned rectangle in page space: a single `re`, or m + 3/4 l (+ h) with axis-aligned edges."""
    if len(path) == 1 and path[0][0] == "re":
        return abs(ctm[1]) < 1e-9 and abs(ctm[2]) < 1e-9 or abs(ctm[0]) < 1e-9 and abs(ctm[3]) < 1e-9
    ops = [p[0] for p in path]
    if not ops or ops[0] != "m" or any(o not in ("l", "h") for o in ops[1:]):
        return False
    pts = [apply(ctm, *p[1][:2]) for p in path if p[0] in ("m", "l")]
    if len(pts) == 5 and abs(pts[0][0] - pts[4][0]) < 0.01 and abs(pts[0][1] - pts[4][1]) < 0.01:
        pts = pts[:4]
    if len(pts) != 4:
        return False
    for k in range(4):
        (x0, y0), (x1, y1) = pts[k], pts[(k + 1) % 4]
        if abs(x0 - x1) > 0.01 and abs(y0 - y1) > 0.01:
            return False
    return True


WHITE = {("g", (1.0,)), ("rg", (1.0, 1.0, 1.0)), ("k", (0.0, 0.0, 0.0, 0.0))}


class Interp:
    def __init__(self, pdf: pikepdf.Pdf) -> None:
        self.pdf = pdf
        self.fonts: dict[tuple, Font] = {}
        self.gid = 0
        self.opstate: dict[tuple, dict] = {}

    def font(self, res, name: str) -> Font:
        fonts = res.get("/Font") if res is not None else None
        if fonts is None or name not in fonts:
            raise Reject("font_mapping_insufficient", "font resource missing")
        fd = fonts[name]
        key = fd.objgen if fd.objgen != (0, 0) else (name, id(fd))
        if key not in self.fonts:
            if str(fd.get("/Subtype", "")) == "/Type3":
                raise Reject("font_mapping_insufficient", "Type3")
            self.fonts[key] = load_font(name, fd)
        return self.fonts[key]

    def page(self, pno: int, page: pikepdf.Page) -> PageModel:
        pm = PageModel(pno, [], [], {}, {})
        obj = page.obj
        cb = [float(v) for v in (obj.get("/CropBox") or obj.get("/MediaBox") or page.mediabox)]
        pm.cropbox = cb
        res = page.resources if "/Resources" in obj or True else None
        contents = obj.get("/Contents")
        streams = []
        if contents is None:
            streams = []
        elif isinstance(contents, pikepdf.Array):
            streams = list(contents)
        else:
            streams = [contents]
        st = self._fresh_state()
        self.order = 0
        for k, s in enumerate(streams):
            skey = f"page:{pno}:{k}"
            try:
                ops = pikepdf.parse_content_stream(s)
            except pikepdf.PdfError as e:
                raise Reject("parser_repair", f"content parse {e}") from None
            if ops and str(ops[-1].operator) == "" and len(streams) > 1:
                raise Reject("unsupported_structure", "operator split across /Contents streams")
            pm.streams[skey] = ops
            pm.stream_objs[skey] = s
            self._run(pm, skey, ops, res, st, chain=(("page", pno),), depth=0)
        if st["in_bt"]:
            raise Reject("unsupported_structure", "unterminated BT")
        return pm

    def _fresh_state(self) -> dict:
        return {"ctm": IDENT[:], "stack": [], "tc": 0.0, "tw": 0.0, "th": 1.0, "tl": 0.0, "font": None, "fname": None,
                "size": 0.0, "tr": 0, "ts": 0.0, "tm": IDENT[:], "tlm": IDENT[:], "in_bt": False,
                "fill": ("g", (0.0,)), "fcs": "/DeviceGray", "fill_raw": [("g", [0])], "path": [], "clip": None, "pending_clip": False, "mc": []}

    def _save(self, st):
        st["stack"].append({k: (v[:] if isinstance(v, list) else v) for k, v in st.items() if k not in ("stack", "path", "mc")})

    def _restore(self, st):
        if not st["stack"]:
            raise Reject("unsupported_structure", "Q without q")
        st.update(st["stack"].pop())

    def _run(self, pm: PageModel, skey: str, ops, res, st, chain, depth: int) -> None:
        if depth > CONFIG["max_form_depth"]:
            raise Reject("structure_limit", "form depth")
        if len(ops) > CONFIG["max_ops_per_page"]:
            raise Reject("structure_limit", "ops")
        for i, ins in enumerate(ops):
            try:
                self._one(pm, skey, ops, i, ins, res, st, chain, depth)
            except (IndexError, ValueError, TypeError, AttributeError) as e:
                raise Reject("unsupported_structure", f"malformed operator ({type(e).__name__})") from None

    def _one(self, pm, skey, ops, i, ins, res, st, chain, depth) -> None:
        if True:
            op = str(ins.operator)
            a = ins.operands
            self.order += 1
            if op == "q":
                self._save(st)
            elif op == "Q":
                self._restore(st)
            elif op == "cm":
                st["ctm"] = mmul([_num(x) for x in a], st["ctm"])
            elif op in ("m", "l", "c", "v", "y", "re", "h"):
                st["path"].append((op, [_num(x) for x in a]))
            elif op in ("W", "W*"):
                st["pending_clip"] = True
            elif op in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"):
                bbox, segs, curves, subpaths = self._path_geom(st)
                if st["pending_clip"] and bbox:
                    st["clip"] = bbox if st["clip"] is None else [max(st["clip"][0], bbox[0]), max(st["clip"][1], bbox[1]), min(st["clip"][2], bbox[2]), min(st["clip"][3], bbox[3])]
                st["pending_clip"] = False
                if op != "n" and bbox:
                    pm.painted.append(Painted("path", bbox, self.order, pm.index, skey, i, {
                        "fill": op not in ("S", "s"), "stroke": op in ("S", "s", "B", "B*", "b", "b*"), "segments": segs,
                        "curves": curves, "subpaths": subpaths, "fill_color": st["fill"], "line_width": st.get("lw", 1.0),
                        "ops": [p[0] for p in st["path"]], "rects": [p[1] for p in st["path"] if p[0] == "re"], "ctm": st["ctm"][:],
                        "is_rect": _is_rect(st["path"], st["ctm"])}))
                st["path"] = []
            elif op == "w":
                st["lw"] = _num(a[0]) * math.hypot(st["ctm"][0], st["ctm"][1])
            elif op in ("g", "rg", "k"):
                st["fill"] = (op, tuple(round(_num(x), 6) for x in a))
                st["fill_raw"] = [(op, list(a))]
            elif op in ("cs",):
                st["fcs"] = self._cs_kind(res, str(a[0]))
                st["fill"] = ("cs", (str(a[0]),))
                st["fill_raw"] = [("cs", list(a))]
            elif op in ("sc", "scn"):
                if a and isinstance(a[-1], pikepdf.Name):
                    st["fill"] = ("pattern", (str(a[-1]),))
                else:
                    vals = tuple(round(_num(x), 6) for x in a)
                    cs = st["fcs"]
                    st["fill"] = ({"/DeviceGray": "g", "/DeviceRGB": "rg", "/DeviceCMYK": "k"}.get(cs, "sc:" + cs), vals)
                st["fill_raw"] = [x for x in st.get("fill_raw", []) if x[0] == "cs"][:1] + [(op, list(a))]
            elif op == "BT":
                st["in_bt"] = True
                st["tm"], st["tlm"] = IDENT[:], IDENT[:]
            elif op == "ET":
                st["in_bt"] = False
            elif op == "Tc":
                st["tc"] = _num(a[0])
            elif op == "Tw":
                st["tw"] = _num(a[0])
            elif op == "Tz":
                st["th"] = _num(a[0]) / 100.0
            elif op == "TL":
                st["tl"] = _num(a[0])
            elif op == "Ts":
                st["ts"] = _num(a[0])
            elif op == "Tr":
                st["tr"] = int(a[0])
            elif op == "Tf":
                st["fname"] = str(a[0])
                st["font"] = self.font(res, st["fname"])
                st["size"] = _num(a[1])
            elif op == "Td":
                st["tlm"] = mmul([1, 0, 0, 1, _num(a[0]), _num(a[1])], st["tlm"])
                st["tm"] = st["tlm"][:]
            elif op == "TD":
                st["tl"] = -_num(a[1])
                st["tlm"] = mmul([1, 0, 0, 1, _num(a[0]), _num(a[1])], st["tlm"])
                st["tm"] = st["tlm"][:]
            elif op == "Tm":
                st["tlm"] = [_num(x) for x in a]
                st["tm"] = st["tlm"][:]
            elif op == "T*":
                st["tlm"] = mmul([1, 0, 0, 1, 0, -st["tl"]], st["tlm"])
                st["tm"] = st["tlm"][:]
            elif op in ("Tj", "TJ", "'", '"'):
                if op in ("'", '"'):
                    if op == '"':
                        st["tw"], st["tc"] = _num(a[0]), _num(a[1])
                    st["tlm"] = mmul([1, 0, 0, 1, 0, -st["tl"]], st["tlm"])
                    st["tm"] = st["tlm"][:]
                self._show(pm, skey, i, op, a, st, chain)
            elif op == "Do":
                xo = (res.get("/XObject") or {}).get(str(a[0])) if res is not None else None
                if xo is None:
                    raise Reject("unsupported_structure", "XObject missing")
                sub = str(xo.get("/Subtype", ""))
                if sub == "/Image":
                    pm.painted.append(Painted("image", self._unit_bbox(st["ctm"]), self.order, pm.index, skey, i, {"obj": xo, "ctm": st["ctm"][:]}))
                elif sub == "/Form":
                    if "/OC" in xo:
                        raise Reject("forms_or_layers", "optional content")
                    self._save(st)
                    mx = [_num(v) for v in xo.get("/Matrix", IDENT)]
                    st["ctm"] = mmul(mx, st["ctm"])
                    xkey = f"xobj:{xo.objgen[0]}"
                    try:
                        xops = pikepdf.parse_content_stream(xo)
                    except pikepdf.PdfError:
                        raise Reject("parser_repair", "form parse") from None
                    pm.streams.setdefault(xkey, xops)
                    pm.stream_objs[xkey] = xo
                    self._run(pm, xkey, xops, xo.get("/Resources") or res, st, chain + ((skey, i),), depth + 1)
                    self._restore(st)
                else:
                    raise Reject("unsupported_structure", f"XObject {sub}")
            elif op == "INLINE IMAGE":
                pm.painted.append(Painted("inline_image", self._unit_bbox(st["ctm"]), self.order, pm.index, skey, i, {"img": a[0], "ctm": st["ctm"][:]}))
            elif op == "sh":
                pm.painted.append(Painted("shading", list(st["clip"] or pm.cropbox), self.order, pm.index, skey, i, {}))
            elif op in ("BDC", "BMC"):
                props = a[1] if op == "BDC" and len(a) > 1 else None
                if isinstance(props, pikepdf.Name) and res is not None:
                    props = (res.get("/Properties") or {}).get(str(props))
                at_span = None
                if isinstance(props, pikepdf.Dictionary):
                    for k in ("/ActualText", "/Alt", "/E"):
                        if k in props:
                            if k == "/ActualText" and "AT" in VARIANT:
                                at_span = (str(props.ActualText), len(pm.glyphs))
                                continue
                            raise Reject("alternate_text_duplication", k)
                    if str(a[0]) == "/OC":
                        raise Reject("forms_or_layers", "optional content")
                st["mc"].append(props.get("/MCID") if isinstance(props, pikepdf.Dictionary) else None)
                st.setdefault("at", []).append(at_span)
            elif op == "EMC":
                if st["mc"]:
                    st["mc"].pop()
                span = st.get("at", [None]).pop() if st.get("at") else None
                if span is not None:
                    text = "".join(g.uni for g in pm.glyphs[span[1]:])
                    if text != span[0]:
                        raise Reject("alternate_text_duplication", "ActualText differs from its glyphs")
                    for g in pm.glyphs[span[1]:]:
                        g.at_span = True
            elif op in ("d0", "d1", "BX", "EX", "MP", "DP", "ri", "i", "j", "J", "M", "d", "gs", "G", "RG", "K", "CS", "SC", "SCN"):
                if op == "gs" and res is not None:
                    gs = (res.get("/ExtGState") or {}).get(str(a[0]))
                    if gs is not None and "/Font" in gs:
                        raise Reject("unsupported_text_state", "font in ExtGState")
            elif op == "":
                raise Reject("unsupported_structure", "dangling operands")
            else:
                raise Reject("unsupported_structure", f"operator {op}")

    def _cs_kind(self, res, name: str) -> str:
        """Device colour space equivalent of a named colour space (ICCBased by its /N); other spaces keep their name."""
        if name in ("/DeviceGray", "/DeviceRGB", "/DeviceCMYK"):
            return name
        cs = (res.get("/ColorSpace") or {}).get(name) if res is not None else None
        if isinstance(cs, pikepdf.Array) and len(cs) == 2 and str(cs[0]) == "/ICCBased":
            return {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(int(cs[1].get("/N", 0)), name)
        if isinstance(cs, pikepdf.Name):
            return str(cs)
        return name

    def _unit_bbox(self, ctm):
        pts = [apply(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
        return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]

    def _path_geom(self, st):
        pts, segs, curves, subpaths = [], 0, 0, 0
        for op, a in st["path"]:
            if op == "re":
                x, y, w, h = a
                pts += [apply(st["ctm"], x, y), apply(st["ctm"], x + w, y + h), apply(st["ctm"], x + w, y), apply(st["ctm"], x, y + h)]
                segs += 4
                subpaths += 1
            elif op == "m":
                pts.append(apply(st["ctm"], a[0], a[1]))
                subpaths += 1
            elif op == "l":
                pts.append(apply(st["ctm"], a[0], a[1]))
                segs += 1
            elif op in ("c", "v", "y"):
                for k in range(0, len(a), 2):
                    pts.append(apply(st["ctm"], a[k], a[k + 1]))
                curves += 1
        if not pts:
            return None, 0, 0, 0
        return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)], segs, curves, subpaths

    def _show(self, pm, skey, i, op, a, st, chain):
        f: Font | None = st["font"]
        if f is None:
            raise Reject("font_mapping_insufficient", "no font")
        if st["tr"] == 3:
            raise Reject("hidden_text", "Tr 3")
        if st["tr"] not in (0, 1, 2):
            raise Reject("unsupported_text_state", f"Tr {st['tr']}")
        if not st["in_bt"]:
            raise Reject("unsupported_structure", "show outside BT")
        elems = list(a[0]) if op == "TJ" else [a[-1]]
        size, th = st["size"], st["th"]
        rec = {"fname": st["fname"], "size": size, "th": th, "tc": st["tc"], "tw": st["tw"], "ts": st["ts"], "tr": st["tr"],
               "fill": st["fill"], "fill_raw": list(st["fill_raw"]), "fcs": st["fcs"], "tlm": st["tlm"][:], "tm_start": st["tm"][:], "ctm": st["ctm"][:], "op": op}
        self.opstate[(pm.index, skey, i)] = rec
        for ei, el in enumerate(elems):
            if isinstance(el, pikepdf.String):
                data = bytes(el)
                pos = 0
                for code in f.split(data):
                    u = f.uni(code)
                    w0 = f.width(code)
                    tw = st["tw"] if (f.code_len == 1 and code == b" ") else 0.0
                    adv = (w0 / 1000.0 * size + st["tc"] + tw) * th
                    trm = mmul(mmul([size * th, 0, 0, size, 0, st["ts"]], st["tm"]), st["ctm"])
                    ox, oy = apply(trm, 0, 0)
                    ex, ey = apply(trm, 1, 0)
                    dv = (ex - ox, ey - oy)
                    q = [apply(trm, x, y) for x, y in ((0, f.descent / 1000), (w0 / 1000, f.descent / 1000), (w0 / 1000, f.ascent / 1000), (0, f.ascent / 1000))]
                    g = Glyph(self.gid, pm.index, skey, chain, i, ei if op == "TJ" else 0, pos, pos + len(code), code, u, st["fname"], f.objgen,
                              size, th, st["tc"], st["tw"], st["ts"], st["tr"], w0, adv, st["tm"][:], st["tlm"][:], st["ctm"][:],
                              (ox, oy), dv, [c for p in q for c in p], st["fill"], skey.startswith("xobj"), st["mc"][-1] if st["mc"] else None)
                    self.gid += 1
                    pm.glyphs.append(g)
                    st["tm"] = mmul([1, 0, 0, 1, adv, 0], st["tm"])
                    pos += len(code)
            else:
                st["tm"] = mmul([1, 0, 0, 1, -_num(el) / 1000.0 * size * th, 0], st["tm"])
        rec["tm_end"] = st["tm"][:]


# ---------------------------------------------------------------- units and source map
def qbox(g: Glyph) -> list[float]:
    xs, ys = g.quad[0::2], g.quad[1::2]
    return [min(xs), min(ys), max(xs), max(ys)]


def build_units(pm: PageModel) -> None:
    text: list[str] = []
    segs = []
    prev: Glyph | None = None
    for g in pm.glyphs:
        if prev is not None:
            kind = _sep(prev, g)
            if kind:
                ch = "\n" if kind == "synthesized_line" else " "
                segs.append({"unit_start": len("".join(text)), "unit_end": len("".join(text)) + 1, "kind": kind, "gid": None})
                text.append(ch)
        start = len("".join(text))
        text.append(g.uni)
        segs.append({"unit_start": start, "unit_end": start + len(g.uni), "kind": "original", "gid": g.gid,
                     "relation": "1:1" if len(g.uni) == 1 else "n:1"})
        prev = g
    pm.text = "".join(text)
    pm.segments = segs


def _sep(a: Glyph, b: Glyph) -> str | None:
    da = math.hypot(*a.dirv) or 1.0
    db = math.hypot(*b.dirv) or 1.0
    ua = (a.dirv[0] / da, a.dirv[1] / da)
    ub = (b.dirv[0] / db, b.dirv[1] / db)
    if abs(ua[0] - ub[0]) > 1e-3 or abs(ua[1] - ub[1]) > 1e-3:
        return "synthesized_line"
    adv = _adv_page(a)
    end_a = (a.origin[0] + adv[0], a.origin[1] + adv[1])
    dx, dy = b.origin[0] - end_a[0], b.origin[1] - end_a[1]
    along = dx * ua[0] + dy * ua[1]
    perp = -dx * ua[1] + dy * ua[0]
    h = max(_hpage(a), _hpage(b))
    if abs(perp) > CONFIG["line_tol"] * h:
        return "synthesized_line"
    if along < -CONFIG["line_tol"] * h or along > CONFIG["column_gap"] * h:
        return "synthesized_line"
    if along > CONFIG["space_gap"] * h and not a.uni.isspace() and not b.uni.isspace():
        return "synthesized_space"
    return None


def _adv_page(g: Glyph) -> tuple[float, float]:
    m = mmul([1, 0, 0, 1, 0, 0], g.tm)
    x0, y0 = apply(mmul(m, g.ctm), 0, 0)
    m2 = mmul([1, 0, 0, 1, g.adv, 0], g.tm)
    x1, y1 = apply(mmul(m2, g.ctm), 0, 0)
    return (x1 - x0, y1 - y0)


def _hpage(g: Glyph) -> float:
    trm = mmul(mmul([g.size, 0, 0, g.size, 0, 0], g.tm), g.ctm)
    return math.hypot(trm[2], trm[3])


def piece(g: Glyph) -> dict:
    return {"page_index": g.page, "use": {"chain": [list(x) for x in g.chain]}, "stream": g.skey, "operator_index": g.op,
            "operand_index": g.elem, "code_start": g.cstart, "code_end": g.cend,
            "font": {"resource_name": g.font, "object_ref": f"{g.font_key[0]} {g.font_key[1]}", "code_bytes": len(g.code)},
            "relation": "1:1" if len(g.uni) == 1 else "n:1", "quad": [round(v, 3) for v in g.quad]}


def source_map(file_sha: str, size: int, pages: list[PageModel]) -> dict:
    gl = {g.gid: g for p in pages for g in p.glyphs}
    units = []
    for p in pages:
        segs = [{"unit_start": s["unit_start"], "unit_end": s["unit_end"], "kind": s["kind"],
                 "pieces": [piece(gl[s["gid"]])] if s["gid"] is not None else []} for s in p.segments]
        units.append({"unit_id": f"u{p.index + 1:04d}", "kind": "pdf_page", "text_length": len(p.text), "segments": segs})
    cfg = hashlib.sha256(json.dumps(CONFIG, sort_keys=True).encode()).hexdigest()
    return {"schema": EXTRACTOR["document_structures"], "identity": {"file_sha256": file_sha, "size_bytes": size, "format": "pdf",
            "extractor": {"id": EXTRACTOR["id"], "version": EXTRACTOR["version"], "config_sha256": cfg}}, "units": units}


def canonical(o: Any) -> bytes:
    return json.dumps(o, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
