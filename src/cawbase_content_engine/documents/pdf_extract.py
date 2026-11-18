"""PDF extractor v2 of A08 (``cce-pdf-extract`` 1, ``document-structures/2``; docs/pdf-protection.md, section 4).

Runs ONLY inside the isolated extraction child. A content-stream interpreter over ``pikepdf`` with the full text
state (``Tc``, ``Tw``, ``Tz``, ``TL``, ``Ts``, ``Tr`` 0-2), simple fonts and Type0 ``Identity-H`` fonts with
``ToUnicode``, Form XObjects, ``/Contents`` arrays, object streams and the geometry of every glyph (origin, advance
and the typographic box built from the font's ascent and descent). One page is one unit; separators are synthesized
by geometry: ``synthesized_line`` when the line changes and ``synthesized_space`` for a wide gap inside a line.

The same model feeds the A08 planner and transformer (``pdf_transform``). The independent verifier
(``pdf_verify``) re-implements the interpretation over ``pypdf`` and never imports this module.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from . import ttf_ink
from .common import Limits, Reject, inflate_limited

EXTRACTOR_ID = "cce-pdf-extract"
EXTRACTOR_VERSION = "1"
DOCUMENT_STRUCTURES = "document-structures/2"
#: geometric constants of the separators (part of the identity: they change the unit text)
GEOMETRY = {"line_tol": 0.5, "space_gap": 0.15, "column_gap": 1.5}
#: limits of A05 section 12.1 that bound this extractor (part of the identity)
LIMIT_KEYS = (
    "max_file_bytes",
    "max_pdf_pages",
    "max_pdf_objects",
    "max_pdf_content_bytes_per_page",
    "max_form_xobject_depth",
    "max_text_operators_per_page",
)
APPROX_WIDTH = 500.0  # base-14 fonts without /Widths: analysis only, never transformable
_RISKY_ACTIONS = {
    "/JavaScript",
    "/Launch",
    "/SubmitForm",
    "/ImportData",
    "/GoToR",
    "/GoToE",
    "/Rendition",
    "/ResetForm",
    "/Hide",
    "/Sound",
    "/Movie",
    "/RichMediaExecute",
    "/SetOCGState",
    "/Trans",
    "/GoTo3DView",
}


def config_payload(lim: Any) -> dict[str, Any]:
    return {**{k: getattr(lim, k) for k in LIMIT_KEYS}, **GEOMETRY}


def config_sha(lim: Any) -> str:
    raw = json.dumps(config_payload(lim), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def identity(lim: Any) -> dict[str, str]:
    return {"id": EXTRACTOR_ID, "version": EXTRACTOR_VERSION, "config_sha256": config_sha(lim)}


# ---------------------------------------------------------------- matrices
Mat = list[float]
IDENT: Mat = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


def mmul(a: Mat, b: Mat) -> Mat:
    return [
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    ]


def minv(m: Mat) -> Mat:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise Reject("unsupported_structure")
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return [ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_)]


def apply(m: Mat, x: float, y: float) -> tuple[float, float]:
    return (x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5])


# ---------------------------------------------------------------- fonts
_PAIR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>")
_RANGE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])")
_HEXS = re.compile(rb"<([0-9A-Fa-f]*)>")


def _utf16(h: bytes) -> str:
    b = bytes.fromhex(h.decode("ascii"))
    if len(b) % 2:
        return b.decode("latin-1")
    return b.decode("utf-16-be", "surrogatepass").encode("utf-16", "surrogatepass").decode("utf-16")


def parse_cmap(data: bytes) -> tuple[dict[bytes, str], list[int]]:
    """``ToUnicode`` CMap -> ({code bytes: text}, sorted code lengths of the codespace ranges)."""
    spaces = b" ".join(re.findall(rb"begincodespacerange(.*?)endcodespacerange", data, re.S))
    lens = sorted({len(a) // 2 for a, _ in _PAIR.findall(spaces)}) or [1]
    out: dict[bytes, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for s, d in _PAIR.findall(block):
            out[bytes.fromhex(s.decode("ascii"))] = _utf16(d) if d else ""
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, rest in _RANGE.findall(block):
            a, b = int(lo, 16), int(hi, 16)
            n = len(lo) // 2
            if b < a or b - a > 65535:
                raise Reject("font_mapping_insufficient")
            if rest.startswith(b"["):
                for i, d in enumerate(_HEXS.findall(rest)):
                    out[(a + i).to_bytes(n, "big")] = _utf16(d)
            else:
                base = bytearray(bytes.fromhex(rest[1:-1].decode("ascii")))
                for i in range(b - a + 1):
                    bb = bytearray(base)
                    if len(bb) >= 2:
                        v = int.from_bytes(bb[-2:], "big") + i
                        bb[-2:] = (v & 0xFFFF).to_bytes(2, "big")
                    elif bb:
                        bb[-1] = (bb[-1] + i) & 0xFF
                    out[(a + i).to_bytes(n, "big")] = _utf16(bytes(bb).hex().encode())
    return out, lens


# StandardEncoding differs from WinAnsi only on these printable ASCII codes
_STD_OVERRIDE = {0x27: "’", 0x60: "‘"}


def _base_text(name: str, code: int) -> str | None:
    if name == "/WinAnsiEncoding":
        if code in (0x81, 0x8D, 0x8F, 0x90, 0x9D):
            return None
        try:
            return bytes([code]).decode("cp1252")
        except UnicodeDecodeError:
            return None
    if name == "/MacRomanEncoding":
        return bytes([code]).decode("mac_roman") if code >= 32 else None
    if name == "/StandardEncoding":
        if 32 <= code <= 126:
            return _STD_OVERRIDE.get(code, chr(code))
        return None
    return None


def _decompose(u: str) -> str:
    if len(u) == 1 and 0xFB00 <= ord(u) <= 0xFB06:
        import unicodedata

        return unicodedata.normalize("NFKC", u)  # presentation-form ligature -> its letters (relation n:1)
    return u


_STD14 = re.compile(r"^/(?:[A-Z]{6}\+)?(Helvetica|Times|Courier|Symbol|ZapfDingbats|Arial)")


def stream_bytes(obj: Any, lim: Limits) -> bytes:
    """Decoded bytes of a content / CMap stream: no filter or one ``FlateDecode`` without parameters, bounded."""
    import pikepdf

    limit = lim.max_pdf_content_bytes_per_page
    filt = obj.get("/Filter")
    raw = obj.read_raw_bytes()
    if filt is None:
        if len(raw) > limit:
            raise Reject("decompression_limit")
        return bytes(raw)
    if isinstance(filt, pikepdf.Array):
        filt = filt[0] if len(filt) == 1 else None
    if filt is None or str(filt) != "/FlateDecode" or obj.get("/DecodeParms") is not None:
        raise Reject("unsupported_structure")
    return inflate_limited(bytes(raw), limit)


@dataclass
class Font:
    resource: str
    objgen: tuple[int, int]
    kind: str  # simple | type0
    subtype: str
    code_len: int
    to_u: dict[bytes, str]
    widths: dict[int, float]
    default_width: float
    ascent: float
    descent: float
    approximate: bool  # base-14 without /Widths: analysis only
    subset: bool
    program: ttf_ink.TTInfo | None = None  # embedded TrueType program (ink boxes only)
    c2g: bytes | None = None  # CIDToGIDMap stream of a Type0 font (None = identity)

    def ink(self, code: bytes, text: str) -> tuple[float, float, float, float] | None | bool:
        """Ink box (1/1000 em) of a code; ``False`` = empty glyph; ``None`` = unknown."""
        if self.program is None:
            return None
        if self.kind == "type0":
            gid: int | None = ttf_ink.gid_cid(int.from_bytes(code, "big"), self.c2g)
        else:
            gid = ttf_ink.gid_simple(self.program, code[0], text)
        if gid is None or not (0 < gid < len(self.program.boxes)):
            return None
        y = ttf_ink.ink_box(self.program, gid)
        return False if y is None else y

    def split(self, data: bytes) -> list[bytes]:
        n = self.code_len
        if len(data) % n:
            raise Reject("font_mapping_insufficient")
        return [data[i : i + n] for i in range(0, len(data), n)]

    def text(self, code: bytes) -> str:
        u = self.to_u.get(code)
        if u is None or u == "" or "�" in u:
            raise Reject("font_mapping_insufficient")
        return u

    def width(self, code: bytes) -> float:
        return self.widths.get(int.from_bytes(code, "big"), self.default_width)

    def reverse(self) -> dict[str, list[bytes]]:
        r: dict[str, list[bytes]] = {}
        for k, v in self.to_u.items():
            r.setdefault(v, []).append(k)
        return r


def load_font(resource: str, fd: Any, lim: Limits) -> Font:
    import pikepdf

    sub = str(fd.get("/Subtype", ""))
    tu = fd.get("/ToUnicode")
    base = str(fd.get("/BaseFont", ""))
    to_u: dict[bytes, str] = {}
    approximate = False
    if sub == "/Type0":
        enc = fd.get("/Encoding")
        if not isinstance(enc, pikepdf.Name) or str(enc) != "/Identity-H":
            raise Reject("font_mapping_insufficient")
        descs = fd.get("/DescendantFonts")
        if not isinstance(descs, pikepdf.Array) or len(descs) != 1:
            raise Reject("font_mapping_insufficient")
        desc = descs[0]
        if str(desc.get("/Subtype", "")) not in ("/CIDFontType2", "/CIDFontType0"):
            raise Reject("font_mapping_insufficient")
        if tu is None or not isinstance(tu, pikepdf.Stream):
            raise Reject("font_mapping_insufficient")
        to_u, lens = parse_cmap(stream_bytes(tu, lim))
        if lens != [2]:
            raise Reject("font_mapping_insufficient")
        widths: dict[int, float] = {}
        w = desc.get("/W")
        if w is not None:
            items = list(w)
            i = 0
            while i < len(items):
                first = int(items[i])
                if i + 1 < len(items) and isinstance(items[i + 1], pikepdf.Array):
                    for k, v in enumerate(items[i + 1]):
                        widths[first + k] = float(v)
                    i += 2
                elif i + 2 < len(items):
                    last, wv = int(items[i + 1]), float(items[i + 2])
                    if last < first or last - first > 65535:
                        raise Reject("font_mapping_insufficient")
                    for k in range(first, last + 1):
                        widths[k] = wv
                    i += 3
                else:
                    raise Reject("font_mapping_insufficient")
        dw = float(desc.get("/DW", 1000))
        fdesc = desc.get("/FontDescriptor")
        code_len = 2
    elif sub in ("/Type1", "/TrueType", "/MMType1"):
        if tu is not None:
            if not isinstance(tu, pikepdf.Stream):
                raise Reject("font_mapping_insufficient")
            to_u, lens = parse_cmap(stream_bytes(tu, lim))
            if lens != [1]:
                raise Reject("font_mapping_insufficient")
        else:
            enc = fd.get("/Encoding")
            if enc is None:
                if not _STD14.match(base):
                    raise Reject("font_mapping_insufficient")
                name = "/WinAnsiEncoding" if not base.endswith(("Symbol", "ZapfDingbats")) else ""
            elif isinstance(enc, pikepdf.Name):
                name = str(enc)
            elif isinstance(enc, pikepdf.Dictionary):
                if "/Differences" in enc:
                    raise Reject("font_mapping_insufficient")  # glyph names without ToUnicode: not reliable
                name = str(enc.get("/BaseEncoding", "/StandardEncoding"))
            else:
                raise Reject("font_mapping_insufficient")
            if name not in ("/WinAnsiEncoding", "/MacRomanEncoding", "/StandardEncoding"):
                raise Reject("font_mapping_insufficient")
            for code in range(256):
                t = _base_text(name, code)
                if t:
                    to_u[bytes([code])] = t
        ws = fd.get("/Widths")
        fdesc = fd.get("/FontDescriptor")
        if ws is None:
            if not _STD14.match(base):
                raise Reject("font_mapping_insufficient")
            approximate = True
            widths = {}
            dw = APPROX_WIDTH
        else:
            fc = int(fd.get("/FirstChar", 0))
            widths = {fc + i: float(v) for i, v in enumerate(ws)}
            dw = float(fdesc.get("/MissingWidth", 0)) if fdesc is not None else 0.0
        code_len = 1
    else:
        raise Reject("font_mapping_insufficient")  # Type3 and anything else
    to_u = {k: _decompose(v) for k, v in to_u.items()}
    asc = float(fdesc.get("/Ascent", 800)) if fdesc is not None else 800.0
    des = float(fdesc.get("/Descent", -200)) if fdesc is not None else -200.0
    if asc <= 0:
        asc = 800.0
    if des >= 0:
        des = -200.0
    program, c2g = _program(fd, sub, fdesc, lim)
    return Font(
        resource,
        tuple(fd.objgen),
        "type0" if sub == "/Type0" else "simple",
        sub,
        code_len,
        to_u,
        widths,
        dw,
        asc,
        des,
        approximate,
        "+" in base[:8],
        program,
        c2g,
    )


def _program(fd: Any, sub: str, fdesc: Any, lim: Limits) -> tuple[ttf_ink.TTInfo | None, bytes | None]:
    """The embedded TrueType program (``FontFile2``), read only for ink boxes; anything else -> (None, None)."""
    import pikepdf

    try:
        ff = fdesc.get("/FontFile2") if fdesc is not None else None
        if not isinstance(ff, pikepdf.Stream):
            return None, None
        info = ttf_ink.read(stream_bytes(ff, lim))
        c2g = None
        if sub == "/Type0":
            m = fd.DescendantFonts[0].get("/CIDToGIDMap")
            if isinstance(m, pikepdf.Stream):
                c2g = stream_bytes(m, lim)
            elif m is not None and str(m) != "/Identity":
                return None, None
        return info, c2g
    except (Reject, ValueError, TypeError, AttributeError, KeyError, IndexError):
        return None, None


# ---------------------------------------------------------------- interpreter records
@dataclass
class Glyph:
    gid: int
    page: int
    skey: str  # "page:<p>:<k>" (k = index in /Contents) or "xobj:<num>:<gen>"
    bt: int  # index of the text object (BT ... ET) in that stream
    op: int  # operator index in the stream
    elem: int  # operand index (TJ array element; 0 otherwise)
    cstart: int  # code byte range inside the string operand
    cend: int
    code: bytes
    text: str
    font: str  # font resource name
    font_key: tuple[int, int]
    size: float  # Tf size
    th: float
    tc: float
    tw: float
    ts: float
    tr: int
    w0: float  # glyph width (1/1000 text space)
    adv: float  # text-space advance including Tc, Tw and Th
    tm: Mat
    ctm: Mat
    origin: tuple[float, float]
    dirv: tuple[float, float]  # page-space vector of one text-space unit along the baseline (font size included)
    box: tuple[float, float, float, float]  # typographic box (origin..origin+w0, descent..ascent), page space
    in_form: bool
    mc: tuple[Any, ...]  # marked-content stack (MCIDs / tags) at the glyph
    at_span: bool = False
    approximate: bool = False
    inkv: tuple[float, float] | None = None  # vertical ink extent in page space (None: unknown -> use the box)
    inkh: tuple[float, float] | None = None  # horizontal ink extent in page space
    ink_empty: bool = False


@dataclass
class Painted:
    kind: str  # path | image | inline_image | shading
    bbox: tuple[float, float, float, float]
    page: int
    skey: str
    op: int
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpState:
    fname: str
    size: float
    th: float
    tc: float
    tw: float
    ts: float
    tr: int
    fill_raw: list[tuple[str, list[Any]]]
    tlm: Mat
    tm_start: Mat
    ctm: Mat
    op: str
    code_len: int
    tm_end: Mat = field(default_factory=lambda: IDENT[:])


@dataclass
class PageModel:
    index: int
    glyphs: list[Glyph]
    painted: list[Painted]
    streams: dict[str, list[Any]]
    stream_objs: dict[str, Any]
    cropbox: tuple[float, float, float, float]
    text: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)


def _num(x: Any) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise Reject("unsupported_structure")
    return v


def _is_rect(path: list[tuple[str, list[float]]], ctm: Mat) -> bool:
    """One axis-aligned rectangle in page space: a single ``re``, or m + 3/4 l (+ h) with axis-aligned edges."""
    if len(path) == 1 and path[0][0] == "re":
        return (abs(ctm[1]) < 1e-9 and abs(ctm[2]) < 1e-9) or (abs(ctm[0]) < 1e-9 and abs(ctm[3]) < 1e-9)
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


_TEXT_ONLY = {"Tc", "Tw", "Tz", "TL", "Ts", "Tr", "Tf", "Td", "TD", "Tm", "T*", "Tj", "TJ", "'", '"'}
_IGNORED = {
    "d0",
    "d1",
    "BX",
    "EX",
    "MP",
    "DP",
    "ri",
    "i",
    "j",
    "J",
    "M",
    "d",
    "G",
    "RG",
    "K",
    "CS",
    "SC",
    "SCN",
}


class Interp:
    """Interprets the page content (and the Form XObjects it invokes) of an open ``pikepdf.Pdf``."""

    def __init__(self, pdf: Any, lim: Limits) -> None:
        self.pdf = pdf
        self.lim = lim
        self.fonts: dict[tuple[Any, ...], Font] = {}
        self.gid = 0
        self.opstate: dict[tuple[int, str, int], OpState] = {}
        self.ops_seen = 0

    def font(self, res: Any, name: str) -> Font:
        fonts = res.get("/Font") if res is not None else None
        if fonts is None or name not in fonts:
            raise Reject("font_mapping_insufficient")
        fd = fonts[name]
        key: tuple[Any, ...] = tuple(fd.objgen) if tuple(fd.objgen) != (0, 0) else (name, id(fd))
        if key not in self.fonts:
            self.fonts[key] = load_font(name, fd, self.lim)
        return self.fonts[key]

    def content_ops(self, obj: Any) -> list[Any]:
        import pikepdf

        raw = stream_bytes(obj, self.lim)
        try:
            return list(pikepdf.parse_content_stream(pikepdf.Stream(self.pdf, raw)))
        except pikepdf.PdfError:
            raise Reject("parser_repair") from None

    def page(self, pno: int, page: Any) -> PageModel:
        import pikepdf

        obj = page.obj
        box = obj.get("/CropBox") or obj.get("/MediaBox") or page.mediabox
        cb = tuple(_num(v) for v in box)
        if len(cb) != 4:
            raise Reject("unsupported_structure")
        pm = PageModel(
            pno, [], [], {}, {}, (min(cb[0], cb[2]), min(cb[1], cb[3]), max(cb[0], cb[2]), max(cb[1], cb[3]))
        )
        res = page.resources
        contents = obj.get("/Contents")
        streams: list[Any] = []
        if contents is None:
            streams = []
        elif isinstance(contents, pikepdf.Array):
            streams = list(contents)
        else:
            streams = [contents]
        st = self._fresh_state()
        self.ops_seen = 0
        for k, s in enumerate(streams):
            if not isinstance(s, pikepdf.Stream):
                raise Reject("unsupported_structure")
            skey = f"page:{pno}:{k}"
            ops = self.content_ops(s)
            if ops and str(ops[-1].operator) == "" and len(streams) > 1:
                raise Reject("unsupported_structure")  # an operator split across /Contents streams
            pm.streams[skey] = ops
            pm.stream_objs[skey] = s
            st["bt"] = -1
            self._run(pm, skey, ops, res, st, chain=(("page", pno),), depth=0)
        if st["in_bt"] or st["stack"] or st["mc"]:
            raise Reject("unsupported_structure")
        return pm

    def _fresh_state(self) -> dict[str, Any]:
        return {
            "ctm": IDENT[:],
            "stack": [],
            "tc": 0.0,
            "tw": 0.0,
            "th": 1.0,
            "tl": 0.0,
            "font": None,
            "fname": None,
            "size": 0.0,
            "tr": 0,
            "ts": 0.0,
            "tm": IDENT[:],
            "tlm": IDENT[:],
            "in_bt": False,
            "fill": ("g", (0.0,)),
            "fcs": "/DeviceGray",
            "fill_raw": [("g", [0])],
            "path": [],
            "pending_clip": False,
            "mc": [],
            "at": [],
            "lw": 1.0,
            "bt": -1,
        }

    def _save(self, st: dict[str, Any]) -> None:
        if len(st["stack"]) > 256:
            raise Reject("structure_limit")
        st["stack"].append(
            {
                k: (v[:] if isinstance(v, list) else v)
                for k, v in st.items()
                if k not in ("stack", "path", "mc", "at", "bt", "in_bt")
            }
        )

    def _restore(self, st: dict[str, Any]) -> None:
        if not st["stack"]:
            raise Reject("unsupported_structure")
        st.update(st["stack"].pop())

    def _run(
        self, pm: PageModel, skey: str, ops: list[Any], res: Any, st: dict[str, Any], chain: Any, depth: int
    ) -> None:
        if depth > self.lim.max_form_xobject_depth:
            raise Reject("structure_limit")
        for i, ins in enumerate(ops):
            self.ops_seen += 1
            if self.ops_seen > self.lim.max_text_operators_per_page:
                raise Reject("structure_limit")
            try:
                self._one(pm, skey, i, ins, res, st, chain, depth)
            except (IndexError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
                raise Reject("unsupported_structure") from None

    def _one(
        self, pm: PageModel, skey: str, i: int, ins: Any, res: Any, st: dict[str, Any], chain: Any, depth: int
    ) -> None:
        import pikepdf

        op = str(ins.operator)
        a = list(ins.operands)
        if op in _TEXT_ONLY and not st["in_bt"]:
            if op not in ("Tc", "Tw", "Tz", "TL", "Ts", "Tr", "Tf"):  # state operators are legal outside BT
                raise Reject("unsupported_structure")
        if op == "q":
            self._save(st)
        elif op == "Q":
            self._restore(st)
        elif op == "cm":
            if st["in_bt"]:
                raise Reject("unsupported_structure")
            st["ctm"] = mmul([_num(x) for x in a[:6]], st["ctm"])
        elif op in ("m", "l", "c", "v", "y", "re", "h"):
            st["path"].append((op, [_num(x) for x in a]))
        elif op in ("W", "W*"):
            st["pending_clip"] = True
        elif op in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"):
            bbox, segs, curves, subpaths = self._path_geom(st)
            st["pending_clip"] = False
            if op != "n" and bbox is not None:
                pm.painted.append(
                    Painted(
                        "path",
                        bbox,
                        pm.index,
                        skey,
                        i,
                        {
                            "fill": op not in ("S", "s"),
                            "stroke": op in ("S", "s", "B", "B*", "b", "b*"),
                            "segments": segs,
                            "curves": curves,
                            "subpaths": subpaths,
                            "fill_color": st["fill"],
                            "is_rect": _is_rect(st["path"], st["ctm"]),
                        },
                    )
                )
            st["path"] = []
        elif op == "w":
            st["lw"] = _num(a[0]) * math.hypot(st["ctm"][0], st["ctm"][1])
        elif op in ("g", "rg", "k"):
            st["fill"] = (op, tuple(round(_num(x), 6) for x in a))
            st["fill_raw"] = [(op, list(a))]
        elif op == "cs":
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
            st["fill_raw"] = [x for x in st["fill_raw"] if x[0] == "cs"][:1] + [(op, list(a))]
        elif op == "BT":
            if st["in_bt"]:
                raise Reject("unsupported_structure")
            st["in_bt"] = True
            st["bt"] += 1
            st["tm"], st["tlm"] = IDENT[:], IDENT[:]
        elif op == "ET":
            if not st["in_bt"]:
                raise Reject("unsupported_structure")
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
            st["tlm"] = [_num(x) for x in a[:6]]
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
            if xo is None or not isinstance(xo, pikepdf.Stream):
                raise Reject("unsupported_structure")
            if st["in_bt"]:
                raise Reject("unsupported_structure")
            sub = str(xo.get("/Subtype", ""))
            if sub == "/Image":
                pm.painted.append(
                    Painted("image", self._unit_bbox(st["ctm"]), pm.index, skey, i, {"obj": xo, "ctm": st["ctm"][:]})
                )
            elif sub == "/Form":
                if "/OC" in xo:
                    raise Reject("forms_or_layers")
                self._save(st)
                mx = [_num(v) for v in xo.get("/Matrix", IDENT)]
                st["ctm"] = mmul(mx, st["ctm"])
                og = tuple(xo.objgen)
                xkey = f"xobj:{og[0]}:{og[1]}"
                xops = self.content_ops(xo)
                pm.streams.setdefault(xkey, xops)
                pm.stream_objs[xkey] = xo
                saved_bt = st["bt"]
                st["bt"] = -1
                self._run(pm, xkey, xops, xo.get("/Resources") or res, st, chain + ((skey, i),), depth + 1)
                st["bt"] = saved_bt
                self._restore(st)
            else:
                raise Reject("unsupported_structure")
        elif op == "INLINE IMAGE":
            pm.painted.append(Painted("inline_image", self._unit_bbox(st["ctm"]), pm.index, skey, i, {}))
        elif op == "sh":
            pm.painted.append(Painted("shading", pm.cropbox, pm.index, skey, i, {}))
        elif op in ("BDC", "BMC"):
            props = a[1] if op == "BDC" and len(a) > 1 else None
            if isinstance(props, pikepdf.Name) and res is not None:
                props = (res.get("/Properties") or {}).get(str(props))
            at_span = None
            tag = str(a[0]) if a else ""
            if tag == "/OC":
                raise Reject("forms_or_layers")
            mcid = None
            if isinstance(props, pikepdf.Dictionary):
                if "/Alt" in props or "/E" in props:
                    raise Reject("alternate_text_duplication")
                if "/ActualText" in props:
                    at_span = (str(props.ActualText), len(pm.glyphs))
                mcid = int(props["/MCID"]) if "/MCID" in props else None
            st["mc"].append((tag, mcid))
            st["at"].append(at_span)
        elif op == "EMC":
            if not st["mc"]:
                raise Reject("unsupported_structure")
            st["mc"].pop()
            span = st["at"].pop()
            if span is not None:
                glyphs = pm.glyphs[span[1] :]
                if "".join(g.text for g in glyphs) != span[0]:
                    raise Reject("alternate_text_duplication")  # /ActualText that differs from its glyphs
                for g in glyphs:
                    g.at_span = True
        elif op == "gs":
            gs = (res.get("/ExtGState") or {}).get(str(a[0])) if res is not None else None
            if gs is not None and "/Font" in gs:
                raise Reject("unsupported_text_state")
        elif op in _IGNORED:
            pass
        elif op == "":
            raise Reject("unsupported_structure")  # dangling operands
        else:
            raise Reject("unsupported_structure")

    def _cs_kind(self, res: Any, name: str) -> str:
        import pikepdf

        if name in ("/DeviceGray", "/DeviceRGB", "/DeviceCMYK"):
            return name
        cs = (res.get("/ColorSpace") or {}).get(name) if res is not None else None
        if isinstance(cs, pikepdf.Array) and len(cs) == 2 and str(cs[0]) == "/ICCBased":
            return {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(int(cs[1].get("/N", 0)), name)
        if isinstance(cs, pikepdf.Name):
            return str(cs)
        return name

    def _unit_bbox(self, ctm: Mat) -> tuple[float, float, float, float]:
        pts = [apply(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def _path_geom(self, st: dict[str, Any]) -> tuple[tuple[float, float, float, float] | None, int, int, int]:
        pts: list[tuple[float, float]] = []
        segs = curves = subpaths = 0
        for op, a in st["path"]:
            if op == "re":
                x, y, w, h = a[:4]
                pts += [
                    apply(st["ctm"], x, y),
                    apply(st["ctm"], x + w, y + h),
                    apply(st["ctm"], x + w, y),
                    apply(st["ctm"], x, y + h),
                ]
                segs += 4
                subpaths += 1
            elif op == "m":
                pts.append(apply(st["ctm"], a[0], a[1]))
                subpaths += 1
            elif op == "l":
                pts.append(apply(st["ctm"], a[0], a[1]))
                segs += 1
            elif op in ("c", "v", "y"):
                for k in range(0, len(a) - 1, 2):
                    pts.append(apply(st["ctm"], a[k], a[k + 1]))
                curves += 1
        if not pts:
            return None, 0, 0, 0
        return (
            (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)),
            segs,
            curves,
            subpaths,
        )

    def _show(self, pm: PageModel, skey: str, i: int, op: str, a: list[Any], st: dict[str, Any], chain: Any) -> None:
        import pikepdf

        f: Font | None = st["font"]
        if f is None:
            raise Reject("font_mapping_insufficient")
        if st["tr"] == 3:
            raise Reject("hidden_text")
        if st["tr"] not in (0, 1, 2):
            raise Reject("unsupported_text_state")
        if op == "TJ":
            if not isinstance(a[0], pikepdf.Array):
                raise Reject("unsupported_structure")
            elems = list(a[0])
        else:
            elems = [a[-1]]
        size, th = st["size"], st["th"]
        self.opstate[(pm.index, skey, i)] = OpState(
            st["fname"],
            size,
            th,
            st["tc"],
            st["tw"],
            st["ts"],
            st["tr"],
            list(st["fill_raw"]),
            st["tlm"][:],
            st["tm"][:],
            st["ctm"][:],
            op,
            f.code_len,
        )
        mc = tuple(st["mc"])
        for ei, el in enumerate(elems):
            if isinstance(el, pikepdf.String):
                data = bytes(el)
                pos = 0
                for code in f.split(data):
                    u = f.text(code)
                    w0 = f.width(code)
                    tw = st["tw"] if (f.code_len == 1 and code == b" ") else 0.0
                    adv = (w0 / 1000.0 * size + st["tc"] + tw) * th
                    trm = mmul(mmul([size * th, 0, 0, size, 0, st["ts"]], st["tm"]), st["ctm"])
                    ox, oy = apply(trm, 0, 0)
                    ex, ey = apply(trm, 1, 0)
                    q = [
                        apply(trm, x, y)
                        for x, y in (
                            (0, f.descent / 1000),
                            (w0 / 1000, f.descent / 1000),
                            (w0 / 1000, f.ascent / 1000),
                            (0, f.ascent / 1000),
                        )
                    ]
                    box = (min(p[0] for p in q), min(p[1] for p in q), max(p[0] for p in q), max(p[1] for p in q))
                    iy = f.ink(code, u)
                    inkv = inkh = None
                    if isinstance(iy, tuple):
                        a0, a1 = apply(trm, iy[0] / 1000, iy[1] / 1000), apply(trm, iy[2] / 1000, iy[3] / 1000)
                        inkv = (min(a0[1], a1[1]), max(a0[1], a1[1]))
                        inkh = (min(a0[0], a1[0]), max(a0[0], a1[0]))
                    pm.glyphs.append(
                        Glyph(
                            self.gid,
                            pm.index,
                            skey,
                            st["bt"],
                            i,
                            ei if op == "TJ" else 0,
                            pos,
                            pos + len(code),
                            code,
                            u,
                            st["fname"],
                            f.objgen,
                            size,
                            th,
                            st["tc"],
                            st["tw"],
                            st["ts"],
                            st["tr"],
                            w0,
                            adv,
                            st["tm"][:],
                            st["ctm"][:],
                            (ox, oy),
                            (ex - ox, ey - oy),
                            box,
                            skey.startswith("xobj"),
                            mc,
                            approximate=f.approximate,
                            inkv=inkv,
                            inkh=inkh,
                            ink_empty=iy is False,
                        )
                    )
                    self.gid += 1
                    st["tm"] = mmul([1, 0, 0, 1, adv, 0], st["tm"])
                    pos += len(code)
            else:  # a number of the TJ array (int or Decimal)
                st["tm"] = mmul([1, 0, 0, 1, -_num(el) / 1000.0 * size * th, 0], st["tm"])
        self.opstate[(pm.index, skey, i)].tm_end = st["tm"][:]


# ---------------------------------------------------------------- geometry helpers shared by the planner
def kpage(g: Glyph) -> float:
    """Page units per text-space unit along the baseline (font size included, Th excluded)."""
    trm = mmul(mmul([g.size, 0, 0, g.size, 0, 0], g.tm), g.ctm)
    return math.hypot(trm[0], trm[1])


def em(g: Glyph) -> float:
    """Effective em in page units: Tf size x vertical scale of Tm x CTM."""
    trm = mmul(mmul([g.size, 0, 0, g.size, 0, 0], g.tm), g.ctm)
    return math.hypot(trm[2], trm[3])


def adv_page(g: Glyph) -> float:
    """Advance of the glyph in page units (with Tc, Tw and Th)."""
    return g.adv * kpage(g) / g.size if g.size else 0.0


def horizontal(g: Glyph) -> bool:
    """Combined matrix axis-aligned, left to right, upright (no rotation, no mirror, no skew)."""
    trm = mmul(mmul([g.size * g.th, 0, 0, g.size, 0, g.ts], g.tm), g.ctm)
    return trm[0] > 0 and trm[3] > 0 and abs(trm[1]) <= 1e-6 * trm[0] and abs(trm[2]) <= 1e-6 * trm[3]


# ---------------------------------------------------------------- units and source map
def _sep(a: Glyph, b: Glyph) -> str | None:
    da = math.hypot(*a.dirv) or 1.0
    db = math.hypot(*b.dirv) or 1.0
    ua = (a.dirv[0] / da, a.dirv[1] / da)
    ub = (b.dirv[0] / db, b.dirv[1] / db)
    if abs(ua[0] - ub[0]) > 1e-3 or abs(ua[1] - ub[1]) > 1e-3:
        return "synthesized_line"
    ax0, ay0 = apply(mmul(a.tm, a.ctm), 0, 0)
    ax1, ay1 = apply(mmul(mmul([1, 0, 0, 1, a.adv, 0], a.tm), a.ctm), 0, 0)
    end_a = (a.origin[0] + (ax1 - ax0), a.origin[1] + (ay1 - ay0))
    dx, dy = b.origin[0] - end_a[0], b.origin[1] - end_a[1]
    along = dx * ua[0] + dy * ua[1]
    perp = -dx * ua[1] + dy * ua[0]
    h = max(em(a), em(b))
    if abs(perp) > GEOMETRY["line_tol"] * h:
        return "synthesized_line"
    if along < -GEOMETRY["line_tol"] * h or along > GEOMETRY["column_gap"] * h:
        return "synthesized_line"
    if along > GEOMETRY["space_gap"] * h and not a.text.isspace() and not b.text.isspace():
        return "synthesized_space"
    return None


def build_units(pm: PageModel) -> None:
    parts: list[str] = []
    n = 0
    segs: list[dict[str, Any]] = []
    prev: Glyph | None = None
    for g in pm.glyphs:
        if prev is not None:
            kind = _sep(prev, g)
            if kind:
                segs.append({"unit_start": n, "unit_end": n + 1, "kind": kind, "gid": None})
                parts.append("\n" if kind == "synthesized_line" else " ")
                n += 1
        segs.append({"unit_start": n, "unit_end": n + len(g.text), "kind": "original", "gid": g.gid})
        parts.append(g.text)
        n += len(g.text)
        prev = g
    pm.text = "".join(parts)
    pm.segments = segs


def piece(g: Glyph) -> dict[str, Any]:
    return {
        "page_index": g.page,
        "stream": g.skey,
        "text_object": g.bt,
        "operator_index": g.op,
        "operand_index": g.elem,
        "code_start": g.cstart,
        "code_end": g.cend,
        "font": {"resource_name": g.font, "object_ref": f"{g.font_key[0]} {g.font_key[1]}", "code_bytes": len(g.code)},
        "relation": "1:1" if len(g.text) == 1 else "n:1",
        "box": [round(v, 3) for v in g.box],
    }


def unit_dicts(pages: list[PageModel]) -> list[dict[str, Any]]:
    gl = {g.gid: g for p in pages for g in p.glyphs}
    out = []
    for p in pages:
        segs = [
            {
                "unit_start": s["unit_start"],
                "unit_end": s["unit_end"],
                "kind": s["kind"],
                "pieces": [piece(gl[s["gid"]])] if s["gid"] is not None else [],
            }
            for s in p.segments
        ]
        out.append(
            {
                "unit_id": f"u{p.index + 1:04d}",
                "kind": "pdf_page",
                "ordinal": p.index + 1,
                "text": p.text,
                "segments": segs,
                "empty": not any(s["kind"] == "original" for s in p.segments),
            }
        )
    return out


# ---------------------------------------------------------------- document gate (structural, never by byte search)
def _walk_struct(node: Any, depth: int = 0, seen: set[tuple[int, int]] | None = None) -> Any:
    import pikepdf

    seen = seen if seen is not None else set()
    if depth > 200:
        raise Reject("structure_limit")
    if isinstance(node, pikepdf.Array):
        for k in node:
            yield from _walk_struct(k, depth + 1, seen)
    elif isinstance(node, pikepdf.Dictionary):
        key = tuple(node.objgen)
        if key != (0, 0):
            if key in seen:
                return
            seen.add((key[0], key[1]))
        yield node
        if "/K" in node:
            yield from _walk_struct(node.K, depth + 1, seen)


def gate(pdf: Any, data: bytes, lim: Limits) -> dict[str, Any]:
    """Document-level rejections of the v2 matrix (A04/A05 reasons kept) and the inventory of auxiliary classes."""
    import pikepdf

    inv: dict[str, Any] = {"e_classes": {}, "links": 0}
    if pdf.get_warnings():
        raise Reject("parser_repair")
    allowed = 2 if pdf.is_linearized else 1
    if data.count(b"startxref") > allowed or data.count(b"%%EOF") > allowed:
        raise Reject("incremental_update")
    root = pdf.Root
    acro = root.get("/AcroForm")
    if acro is not None:
        fields = acro.get("/Fields") or []
        if int(acro.get("/SigFlags", 0) or 0) or any(str(f.get("/FT", "")) == "/Sig" for f in fields):
            raise Reject("signed")
        raise Reject("forms_or_layers")
    if "/Perms" in root:
        raise Reject("signed")
    for k in ("/OCProperties", "/XFA"):
        if k in root:
            raise Reject("forms_or_layers")
    if "/PieceInfo" in root:
        raise Reject("unsupported_structure")
    names = root.get("/Names")
    if names is not None:
        if "/EmbeddedFiles" in names:
            raise Reject("embedded_content")
        if "/JavaScript" in names:
            raise Reject("executable_action")
    oa = root.get("/OpenAction")
    if isinstance(oa, pikepdf.Dictionary) and str(oa.get("/S", "")) != "/GoTo":
        raise Reject("executable_action")
    if "/AA" in root:
        raise Reject("executable_action")
    n_objects = 0
    for o in pdf.objects:
        n_objects += 1
        if n_objects > lim.max_pdf_objects:
            raise Reject("structure_limit")
        if isinstance(o, pikepdf.Dictionary) or isinstance(o, pikepdf.Stream):
            d = o.stream_dict if isinstance(o, pikepdf.Stream) else o
            if "/JS" in d or str(d.get("/S", "")) in _RISKY_ACTIONS:
                raise Reject("executable_action")
            if "/EF" in d or str(d.get("/Type", "")) in ("/EmbeddedFile", "/Filespec"):
                raise Reject("embedded_content")
            if "/ByteRange" in d and "/Contents" in d:
                raise Reject("signed")
    if "/StructTreeRoot" in root:
        for el in _walk_struct(root.StructTreeRoot.get("/K")):
            if "/Alt" in el or "/ActualText" in el or "/E" in el:
                raise Reject("alternate_text_duplication")
    info = pdf.trailer.get("/Info")
    if info is not None:
        n = sum(1 for k in info.keys() if k not in ("/CreationDate", "/ModDate"))
        if n:
            inv["e_classes"]["document_info_descriptive"] = n
    if "/Metadata" in root:
        inv["e_classes"]["xmp_metadata"] = 1
    om = 0
    for o in pdf.objects:
        if isinstance(o, (pikepdf.Dictionary, pikepdf.Stream)) and tuple(o.objgen) != tuple(root.objgen):
            d = o.stream_dict if isinstance(o, pikepdf.Stream) else o
            if "/Metadata" in d:
                om += 1
    if om:
        inv["e_classes"]["object_metadata"] = om
    for p in pdf.pages:
        if "/PieceInfo" in p.obj:
            raise Reject("unsupported_structure")
        if "/AA" in p.obj:
            raise Reject("executable_action")
        rot = p.obj.get("/Rotate")
        if rot is not None and int(rot) % 90 != 0:
            raise Reject("unsupported_structure")
        for an in p.obj.get("/Annots", []) or []:
            sub = str(an.get("/Subtype", ""))
            if sub == "/Link":
                inv["links"] += 1
                acts = []
                if an.get("/A") is not None:
                    acts.append(an.get("/A"))
                aa = an.get("/AA")
                if aa is not None:
                    acts += [aa[k] for k in aa.keys()]
                for act in acts:
                    if str(act.get("/S", "")) not in ("/URI", "/GoTo"):
                        raise Reject("executable_action")
            elif sub in ("/Text", "/Popup"):
                inv["e_classes"]["comments"] = inv["e_classes"].get("comments", 0) + 1
            elif sub == "/FileAttachment":
                raise Reject("embedded_content")
            elif sub == "/Widget":
                raise Reject("forms_or_layers")
            else:
                raise Reject("unsupported_structure")
    return inv


def visibility(pages: list[PageModel]) -> None:
    """Hidden text (PX-07): a glyph outside the crop box or covered by an opaque fill painted after it."""
    for pm in pages:
        cb = pm.cropbox
        for g in pm.glyphs:
            b = g.box
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            if not (cb[0] <= cx <= cb[2] and cb[1] <= cy <= cb[3]):
                raise Reject("hidden_text")
        for p in pm.painted:
            if p.kind != "path" or not p.detail.get("fill"):
                continue
            pb = p.bbox
            for g in pm.glyphs:
                if g.skey == p.skey and g.op < p.op and (g.box[2] - g.box[0]) > 0:
                    cx, cy = (g.box[0] + g.box[2]) / 2, (g.box[1] + g.box[3]) / 2
                    if pb[0] <= cx <= pb[2] and pb[1] <= cy <= pb[3]:
                        raise Reject("hidden_text")


@dataclass
class Model:
    pdf: Any
    pages: list[PageModel]
    interp: Interp
    inventory: dict[str, Any]


def open_pdf(data: bytes) -> Any:
    import pikepdf

    try:
        pdf = pikepdf.open(io.BytesIO(data), attempt_recovery=False)
    except pikepdf.PasswordError:
        raise Reject("encrypted") from None
    except pikepdf.PdfError:
        raise Reject("parser_repair") from None
    if pdf.is_encrypted:
        raise Reject("encrypted")
    return pdf


def model(data: bytes, lim: Limits) -> Model:
    """Opens, gates and interprets the whole document (raises ``Reject`` with a closed reason)."""
    if len(data) > lim.max_file_bytes:
        raise Reject("structure_limit")
    pdf = open_pdf(data)
    inv = gate(pdf, data, lim)
    if len(pdf.pages) > lim.max_pdf_pages:
        raise Reject("structure_limit")
    it = Interp(pdf, lim)
    pages = [it.page(i, p) for i, p in enumerate(pdf.pages)]
    if pdf.get_warnings():
        raise Reject("parser_repair")
    visibility(pages)
    for pm in pages:
        build_units(pm)
    if all(not pm.glyphs for pm in pages) and any(
        p.kind in ("image", "inline_image") for pm in pages for p in pm.painted
    ):
        raise Reject("scanned_without_text")
    return Model(pdf, pages, it, inv)


def extract(data: bytes, lim: Limits) -> dict[str, Any]:
    """A05 child output for an analysis with the v2 extractor (one unit per page)."""
    m = model(data, lim)
    nc: dict[str, int] = {}
    reasons: dict[str, str] = {}

    def note(cls: str, n: int, reason: str) -> None:
        if n:
            nc[cls] = nc.get(cls, 0) + n
            reasons[cls] = reason

    e = m.inventory["e_classes"]
    note("document_properties", e.get("document_info_descriptive", 0) + e.get("xmp_metadata", 0), "not_analyzed")
    note("comments", e.get("comments", 0), "not_analyzed")
    note("link_annotations", m.inventory["links"], "no_extraction")
    note("images", sum(1 for pm in m.pages for p in pm.painted if p.kind in ("image", "inline_image")), "no_extraction")
    note(
        "vector_graphics",
        sum(1 for pm in m.pages for p in pm.painted if p.kind in ("path", "shading")),
        "no_extraction",
    )
    declared = [{"element_class": k, "count": v, "reason": reasons[k]} for k, v in sorted(nc.items())]
    return {"format": "pdf", "units": unit_dicts(m.pages), "declared_not_covered": declared, "pdf_extractor": "v2"}
