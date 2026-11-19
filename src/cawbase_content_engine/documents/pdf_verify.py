"""Independent verifier of A08 derivatives (docs/pdf-protection.md, sections 9.3 and 10).

Runs in its OWN isolated child and reads every PDF with ``pypdf`` (strict) - a parser different from the
transformer's (``pikepdf``/QPDF) - through its own content-stream interpreter, its own ``ToUnicode`` reader and its
own zone and coverage rules. It never trusts what the transformer says it did: the expected text, the zones, the
geometry and the residue scope come from the ORIGINAL and the selections; every number the transformer recorded in
the binding layout (the restorer's only geometric input) is checked against the verifier's own computation.
Its report never contains values, positions of values, font names or tokens.

* ``protect``: original + derivative + selections (with their values) + layout.
* ``restore``: derivative + restored + authenticated layout + values (never the original nor the analysis).
"""

from __future__ import annotations

import io
import json
import logging
import math
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

from . import ttf_ink

# the verifier's own copies of the approved constants (never read from the transformer)
MIN_SCALE = 0.60
ZONE_MARGIN_EM = 0.3
V_TOL = 0.25
POS_TOL = 0.01
LINE_TOL, SPACE_GAP, COLUMN_GAP = 0.5, 0.15, 1.5
_STRICT = re.compile(r"\[\[CCE1:[0-9A-HJKMNP-TV-Z]{16}\]\]", re.ASCII)
_ALTERED = (
    re.compile(r"(?<![0-9a-z])cce[0-9]{1,3}\s*:\s*[0-9a-z]{16}(?![0-9a-z])", re.ASCII | re.IGNORECASE),
    re.compile(r"\[\s*\[\s*cce[0-9]{1,3}\s*:", re.ASCII | re.IGNORECASE),
)
REDACTED = "[REDACTED]"
_WHITE = (("g", (1.0,)), ("rg", (1.0, 1.0, 1.0)), ("k", (0.0, 0.0, 0.0, 0.0)))


class Fail(Exception):
    def __init__(self, code: str, check: str) -> None:
        super().__init__(code)
        self.code, self.check = code, check


def _fail(check: str, code: str = "derivative_verification_failed") -> Fail:
    return Fail(code, check)


@lru_cache(maxsize=1)
def _candidate() -> dict[str, Any]:
    import hashlib

    base = resources.files("cawbase_content_engine.documents") / "data"
    meta = json.loads((base / "cce-candidate.json").read_text(encoding="utf-8"))
    if hashlib.sha256((base / "cce-candidate.ttf").read_bytes()).hexdigest() != meta["subset_sha256"]:
        raise _fail("VL")
    return meta  # type: ignore[no-any-return]


# ---------------------------------------------------------------- reading (pypdf, strict, no warnings)
def open_strict(data: bytes) -> Any:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    warnings: list[str] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            warnings.append(record.levelname)

    lg = logging.getLogger("pypdf")
    h = _H(level=logging.WARNING)
    lg.addHandler(h)
    try:
        r = PdfReader(io.BytesIO(data), strict=True)
        if r.is_encrypted:
            raise _fail("VA")
        for p in r.pages:
            p.get_contents()
    except Fail:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError):
        raise _fail("VA") from None
    finally:
        lg.removeHandler(h)
    if warnings:
        raise _fail("VA")
    return r


def _raw(s: Any) -> bytes:
    from pypdf.generic import TextStringObject

    if isinstance(s, TextStringObject):
        return bytes(s.get_original_bytes())
    return bytes(s)


def _num(x: Any) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise _fail("VA")
    return v


def _mul(a: list[float], b: list[float]) -> list[float]:
    return [
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    ]


def _pt(m: list[float], x: float, y: float) -> tuple[float, float]:
    return (x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5])


# ---------------------------------------------------------------- content-stream lexer (own; never pypdf's nor QPDF's)
class Name(str):
    """A PDF name, with its leading ``/`` and ``#xx`` escapes decoded."""


_WSP = b" \t\r\n\f\x00"
_DEL = b"()<>[]{}/%"
_ESC = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f"}
_NUM = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")


def lex(data: bytes, sink: list[tuple[str, bytes]] | None = None) -> list[tuple[list[Any], str]]:
    """Tokenizes a content stream into (operands, operator); strings stay raw ``bytes``. Malformed -> ``VA``."""
    ops: list[tuple[list[Any], str]] = []
    stack: list[list[Any]] = [[]]
    kinds: list[str] = ["top"]
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c in _WSP:
            i += 1
        elif c == 0x25:  # % comment (kept for the residue scan: nothing in a stream escapes it)
            j = i
            while i < n and data[i] not in b"\r\n":
                i += 1
            if sink is not None:
                sink.append(("comment", data[j:i]))
        elif c == 0x2F:  # /name
            j = i + 1
            while j < n and data[j] not in _WSP and data[j] not in _DEL:
                j += 1
            raw = data[i + 1 : j]
            name = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
            stack[-1].append(Name("/" + name.decode("latin-1")))
            i = j
        elif c == 0x28:  # (literal string)
            out = bytearray()
            depth, j = 1, i + 1
            while j < n:
                ch = data[j]
                if ch == 0x5C:  # backslash
                    j += 1
                    if j >= n:
                        break
                    e = data[j]
                    if e in _ESC:
                        out += _ESC[e]
                    elif 0x30 <= e <= 0x37:
                        k = j
                        while k < n and k < j + 3 and 0x30 <= data[k] <= 0x37:
                            k += 1
                        out.append(int(data[j:k], 8) & 0xFF)
                        j = k - 1
                    elif e in b"\r\n":
                        if e == 0x0D and j + 1 < n and data[j + 1] == 0x0A:
                            j += 1
                    else:
                        out.append(e)
                elif ch == 0x28:
                    depth += 1
                    out.append(ch)
                elif ch == 0x29:
                    depth -= 1
                    if depth == 0:
                        break
                    out.append(ch)
                else:
                    out.append(ch)
                j += 1
            if depth != 0:
                raise _fail("VA")
            stack[-1].append(bytes(out))
            i = j + 1
        elif c == 0x3C and data[i + 1 : i + 2] == b"<":
            stack.append([])
            kinds.append("dict")
            i += 2
        elif c == 0x3E and data[i + 1 : i + 2] == b">":
            if kinds[-1] != "dict":
                raise _fail("VA")
            items = stack.pop()
            kinds.pop()
            if len(items) % 2:
                raise _fail("VA")
            stack[-1].append({str(items[k]): items[k + 1] for k in range(0, len(items), 2)})
            i += 2
        elif c == 0x3C:  # <hex string>
            j = data.find(b">", i)
            if j < 0:
                raise _fail("VA")
            hx = re.sub(rb"\s", b"", data[i + 1 : j])
            if len(hx) % 2:
                hx += b"0"
            try:
                stack[-1].append(bytes.fromhex(hx.decode("ascii")))
            except ValueError:
                raise _fail("VA") from None
            i = j + 1
        elif c == 0x5B:
            stack.append([])
            kinds.append("array")
            i += 1
        elif c == 0x5D:
            if kinds[-1] != "array":
                raise _fail("VA")
            items = stack.pop()
            kinds.pop()
            stack[-1].append(items)
            i += 1
        else:
            m = _NUM.match(data, i)
            j = i
            while j < n and data[j] not in _WSP and data[j] not in _DEL:
                j += 1
            tok = data[i:j]
            if not tok:
                raise _fail("VA")
            if m is not None and m.end() == j:
                stack[-1].append(float(tok))
            elif tok in (b"true", b"false", b"null"):
                stack[-1].append(tok == b"true" if tok != b"null" else None)
            elif tok == b"BI":
                k = data.find(b"ID", j)
                if k < 0 or len(stack) != 1:
                    raise _fail("VA")
                start = k + 3
                end = start
                while True:
                    end = data.find(b"EI", end)
                    if end < 0:
                        raise _fail("VA")
                    if data[end - 1 : end] in (b" ", b"\n", b"\r", b"\t") and (end + 2 >= n or data[end + 2] in _WSP):
                        break
                    end += 2
                ops.append(([{"data": data[start:end]}], "INLINE IMAGE"))
                if sink is not None:
                    sink.append(("image", data[j:end]))
                stack = [[]]
                kinds = ["top"]
                i = end + 2
                continue
            else:
                if len(stack) != 1:
                    raise _fail("VA")
                op = tok.decode("latin-1")
                if sink is not None and op not in ("Tj", "TJ", "'", '"'):
                    sink.extend(_strings(stack[0]))
                ops.append((stack[0], op))
                stack = [[]]
            i = j
    if len(stack) != 1 or stack[0]:
        raise _fail("VA")
    return ops


def _strings(operands: Any) -> list[tuple[str, bytes]]:
    """Every string (and name) inside the operands of a NON-show operator (never text a reader extracts)."""
    out: list[tuple[str, bytes]] = []
    stack = [operands]
    while stack:
        x = stack.pop()
        if isinstance(x, bytes):
            out.append(("string", x))
        elif isinstance(x, Name):
            out.append(("name", str(x).encode("utf-8", "surrogateescape")))
        elif isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            stack.extend(x.values())
            stack.extend(Name(k) for k in x)
    return out


def _stream_of(o: Any) -> bytes:
    from pypdf.generic import ArrayObject

    o = o.get_object()
    if isinstance(o, ArrayObject):
        return b"\n".join(_sdata(x) for x in o)
    return _sdata(o)


# ---------------------------------------------------------------- fonts (own reader)
_TOK = re.compile(rb"<([0-9A-Fa-f]*)>|(\[)|(\])|([A-Za-z]+)")


def _u16(h: bytes) -> str:
    b = bytes.fromhex(h.decode("ascii"))
    if len(b) % 2:
        return b.decode("latin-1")
    return b.decode("utf-16-be", "surrogatepass").encode("utf-16", "surrogatepass").decode("utf-16")


def cmap_of(data: bytes) -> tuple[dict[bytes, str], set[int]]:
    """Tokenizer-based ``ToUnicode`` reader (independent of the extractor's)."""
    out: dict[bytes, str] = {}
    lens: set[int] = set()
    mode = ""
    buf: list[Any] = []
    arr: list[bytes] | None = None
    for m in _TOK.finditer(data):
        hx, lb, rb, kw = m.groups()
        if kw is not None:
            k = kw.decode()
            if k in ("begincodespacerange", "beginbfchar", "beginbfrange"):
                mode, buf = k, []
            elif k.startswith("end"):
                mode = ""
            continue
        if not mode:
            continue
        if lb:
            arr = []
            continue
        if rb:
            buf.append(arr or [])
            arr = None
        elif hx is not None:
            if arr is not None:
                arr.append(hx)
                continue
            buf.append(hx)
        else:
            continue
        if mode == "begincodespacerange" and len(buf) == 2:
            lens.add(len(buf[0]) // 2)
            buf = []
        elif mode == "beginbfchar" and len(buf) == 2:
            out[bytes.fromhex(buf[0].decode())] = _u16(buf[1]) if buf[1] else ""
            buf = []
        elif mode == "beginbfrange" and len(buf) == 3:
            lo, hi, dst = buf
            a, b, n = int(lo, 16), int(hi, 16), len(lo) // 2
            if b < a or b - a > 65535:
                raise _fail("VA")
            for i in range(b - a + 1):
                if isinstance(dst, list):
                    if i < len(dst):
                        out[(a + i).to_bytes(n, "big")] = _u16(dst[i])
                else:
                    raw = bytearray(bytes.fromhex(dst.decode()))
                    if len(raw) >= 2:
                        raw[-2:] = ((int.from_bytes(raw[-2:], "big") + i) & 0xFFFF).to_bytes(2, "big")
                    elif raw:
                        raw[-1] = (raw[-1] + i) & 0xFF
                    out[(a + i).to_bytes(n, "big")] = _u16(bytes(raw).hex().encode())
            buf = []
    return out, lens


def _enc_text(name: str, code: int) -> str | None:
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
        return {0x27: "’", 0x60: "‘"}.get(code, chr(code)) if 32 <= code <= 126 else None
    return None


@dataclass
class VFont:
    name: str
    ident: Any
    two: bool
    to_u: dict[bytes, str]
    widths: dict[int, float]
    dw: float
    asc: float
    des: float
    approx: bool
    program: ttf_ink.TTInfo | None
    c2g: bytes | None
    obj: Any

    def width(self, code: bytes) -> float:
        return self.widths.get(int.from_bytes(code, "big"), self.dw)

    def text(self, code: bytes) -> str:
        u = self.to_u.get(code)
        if not u or "�" in u:
            raise _fail("VU")
        if len(u) == 1 and 0xFB00 <= ord(u) <= 0xFB06:
            u = unicodedata.normalize("NFKC", u)
        return u

    def ink(self, code: bytes, text: str) -> tuple[float, float, float, float] | None | bool:
        if self.program is None:
            return None
        gid = (
            ttf_ink.gid_cid(int.from_bytes(code, "big"), self.c2g)
            if self.two
            else ttf_ink.gid_simple(self.program, code[0], text)
        )
        if gid is None or not (0 < gid < len(self.program.boxes)):
            return None
        y = ttf_ink.ink_box(self.program, gid)
        return False if y is None else y


_STD14 = re.compile(r"^/(?:[A-Z]{6}\+)?(Helvetica|Times|Courier|Symbol|ZapfDingbats|Arial)")


def _sdata(o: Any) -> bytes:
    try:
        return bytes(o.get_object().get_data())
    except Exception:
        raise _fail("VA") from None


def font_of(name: str, fd: Any) -> VFont:
    from pypdf.generic import ArrayObject, NameObject, StreamObject

    fd = fd.get_object()
    sub = str(fd.get("/Subtype", ""))
    base = str(fd.get("/BaseFont", ""))
    tu = fd.get("/ToUnicode")
    to_u: dict[bytes, str] = {}
    approx = False
    c2g = None
    if sub == "/Type0":
        if str(fd.get("/Encoding", "")) != "/Identity-H":
            raise _fail("VU")
        desc = fd["/DescendantFonts"][0].get_object()
        if tu is None:
            raise _fail("VU")
        to_u, lens = cmap_of(_sdata(tu))
        if lens != {2}:
            raise _fail("VU")
        widths: dict[int, float] = {}
        w = list(desc.get("/W", []) or [])
        i = 0
        while i < len(w):
            first = int(w[i])
            nxt = w[i + 1].get_object() if i + 1 < len(w) else None
            if isinstance(nxt, ArrayObject):
                for k, v in enumerate(nxt):
                    widths[first + k] = float(v)
                i += 2
            else:
                last, v = int(w[i + 1]), float(w[i + 2])
                for k in range(first, last + 1):
                    widths[k] = v
                i += 3
        dw = float(desc.get("/DW", 1000))
        fdesc = desc.get("/FontDescriptor")
        two = True
        m = desc.get("/CIDToGIDMap")
        if m is not None and isinstance(m.get_object(), StreamObject):
            c2g = _sdata(m)
    else:
        if tu is not None:
            to_u, lens = cmap_of(_sdata(tu))
            if lens != {1}:
                raise _fail("VU")
        else:
            enc = fd.get("/Encoding")
            enc = enc.get_object() if enc is not None else None
            if enc is None:
                ename = "/WinAnsiEncoding" if _STD14.match(base) else ""
            elif isinstance(enc, NameObject):
                ename = str(enc)
            else:
                if "/Differences" in enc:
                    raise _fail("VU")
                ename = str(enc.get("/BaseEncoding", "/StandardEncoding"))
            for c in range(256):
                t = _enc_text(ename, c)
                if t:
                    to_u[bytes([c])] = t
        ws = fd.get("/Widths")
        fdesc = fd.get("/FontDescriptor")
        if ws is None:
            approx = True
            widths, dw = {}, 500.0
        else:
            fc = int(fd.get("/FirstChar", 0))
            widths = {fc + i: float(v) for i, v in enumerate(ws.get_object())}
            dw = float(fdesc.get_object().get("/MissingWidth", 0)) if fdesc is not None else 0.0
        two = False
    fdo = fdesc.get_object() if fdesc is not None else None
    asc = float(fdo.get("/Ascent", 800)) if fdo is not None else 800.0
    des = float(fdo.get("/Descent", -200)) if fdo is not None else -200.0
    asc = asc if asc > 0 else 800.0
    des = des if des < 0 else -200.0
    program = None
    if fdo is not None and fdo.get("/FontFile2") is not None:
        try:
            program = ttf_ink.read(_sdata(fdo["/FontFile2"]))
        except Fail:
            program = None
    return VFont(name, fd.indirect_reference or id(fd), two, to_u, widths, dw, asc, des, approx, program, c2g, fd)


# ---------------------------------------------------------------- interpreter
@dataclass
class VGlyph:
    text: str
    code: bytes
    font: VFont
    size: float
    th: float
    tr: int
    ox: float
    oy: float
    ux: float  # page vector of one text unit along the baseline (size included)
    uy: float
    adv: float  # advance in page units (Tc, Tw, Th included)
    em: float
    box: tuple[float, float, float, float]
    inkv: tuple[float, float] | None
    ink_empty: bool
    fill: tuple[Any, ...]
    mc: tuple[Any, ...]
    in_form: bool
    horizontal: bool
    at: bool = False
    wadv: float = 0.0  # advance of the glyph width alone (no Tc, no Tw), page units
    inkh: tuple[float, float] | None = None  # horizontal ink extent, page units


@dataclass
class VPage:
    crop: tuple[float, float, float, float]
    glyphs: list[VGlyph] = field(default_factory=list)
    painted: list[tuple[str, tuple[float, float, float, float], dict[str, Any]]] = field(default_factory=list)
    nontext: list[Any] = field(default_factory=list)
    props: list[Any] = field(default_factory=list)
    hidden: list[tuple[str, bytes]] = field(default_factory=list)  # comments, non-show strings/names, inline images
    prop_texts: list[str] = field(default_factory=list)  # decoded TEXT strings of marked-content property lists
    text: str = ""
    segs: list[tuple[int, int, int | None]] = field(default_factory=list)  # (start, end, glyph index | None)


def _text_of(v: Any) -> str:
    """A PDF text string (PDFDocEncoding or UTF-16 with BOM) from raw bytes or a pypdf object."""
    if isinstance(v, bytes):
        if v[:2] in (b"\xfe\xff", b"\xff\xfe"):
            return v.decode("utf-16")
        if v[:3] == b"\xef\xbb\xbf":
            return v[3:].decode("utf-8", "replace")
        return v.decode("latin-1")
    return str(v.get_object() if hasattr(v, "get_object") else v)


def _props(p: Any) -> tuple[Any, ...]:
    return tuple(sorted((str(k), "|".join(_decoded(v))) for k, v in p.items()))


class Interp:
    def __init__(self, reader: Any) -> None:
        self.r = reader
        self.fonts: dict[Any, VFont] = {}

    def font(self, res: Any, name: str) -> VFont:
        fonts = res.get_object().get("/Font") if res is not None else None
        if fonts is None or name not in fonts.get_object():
            raise _fail("VU")
        ref = fonts.get_object().raw_get(name)
        key = getattr(ref, "idnum", None) or (name, id(fonts))
        if key not in self.fonts:
            self.fonts[key] = font_of(name, fonts.get_object()[name])
        return self.fonts[key]

    def page(self, pg: Any) -> VPage:
        box = pg.get("/CropBox") or pg.mediabox
        b = [float(v) for v in box]
        vp = VPage((min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3])))
        cont = pg.get("/Contents")
        if cont is not None:
            st = self._state()
            self._run(vp, lex(_stream_of(cont), vp.hidden), pg.get("/Resources"), st, False, 0)
            if st["inbt"] or st["mc"] or st["stack"]:
                raise _fail("VA")
        return vp

    def _state(self) -> dict[str, Any]:
        return {
            "ctm": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            "stack": [],
            "tc": 0.0,
            "tw": 0.0,
            "th": 1.0,
            "tl": 0.0,
            "ts": 0.0,
            "tr": 0,
            "font": None,
            "size": 0.0,
            "tm": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            "tlm": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            "inbt": False,
            "fill": ("g", (0.0,)),
            "fcs": "/DeviceGray",
            "path": [],
            "mc": [],
            "at": [],
        }

    def _run(self, vp: VPage, ops: list[Any], res: Any, st: dict[str, Any], in_form: bool, depth: int) -> None:
        from pypdf.generic import ArrayObject, DictionaryObject, StreamObject

        if depth > 32:
            raise _fail("VA")
        for operands, op in ops:
            a = list(operands)
            if not st["inbt"] and op not in ("BT", "BDC", "BMC", "EMC", "Tc", "Tw", "Tz", "TL", "Ts", "Tr", "Tf"):
                if op == "INLINE IMAGE":
                    vp.nontext.append(("img", a[0]["data"]))
                else:
                    vp.nontext.append((op, tuple(str(x) for x in a)))
            elif op in ("BT", "ET", "BDC", "BMC", "EMC"):
                vp.nontext.append((op,))
            if op == "q":
                st["stack"].append(
                    {
                        k: (v[:] if isinstance(v, list) else v)
                        for k, v in st.items()
                        if k not in ("stack", "path", "mc", "at", "inbt")
                    }
                )
            elif op == "Q":
                if not st["stack"]:
                    raise _fail("VA")
                st.update(st["stack"].pop())
            elif op == "cm":
                st["ctm"] = _mul([_num(x) for x in a[:6]], st["ctm"])
            elif op in ("m", "l", "c", "v", "y", "re", "h"):
                st["path"].append((op, [_num(x) for x in a]))
            elif op in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"):
                if op != "n":
                    geo = self._geom(st)
                    if geo is not None:
                        bbox, segs, curves, rect = geo
                        vp.painted.append(
                            (
                                "path",
                                bbox,
                                {
                                    "fill": op not in ("S", "s"),
                                    "stroke": op in ("S", "s", "B", "B*", "b", "b*"),
                                    "fill_color": st["fill"],
                                    "curves": curves,
                                    "segments": segs,
                                    "is_rect": rect,
                                },
                            )
                        )
                st["path"] = []
            elif op in ("g", "rg", "k"):
                st["fill"] = (op, tuple(round(_num(x), 6) for x in a))
            elif op == "cs":
                nm = str(a[0])
                st["fcs"] = nm
                cs = (res.get_object().get("/ColorSpace") or {}).get(nm) if res is not None else None
                cs = cs.get_object() if cs is not None else None
                if isinstance(cs, ArrayObject) and len(cs) == 2 and str(cs[0]) == "/ICCBased":
                    st["fcs"] = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(
                        int(cs[1].get_object().get("/N", 0)), nm
                    )
                st["fill"] = ("cs", (nm,))
            elif op in ("sc", "scn"):
                if a and isinstance(a[-1], Name):
                    st["fill"] = ("pattern", (str(a[-1]),))
                else:
                    kind = {"/DeviceGray": "g", "/DeviceRGB": "rg", "/DeviceCMYK": "k"}.get(
                        st["fcs"], "sc:" + st["fcs"]
                    )
                    st["fill"] = (kind, tuple(round(_num(x), 6) for x in a))
            elif op == "BT":
                st["inbt"] = True
                st["tm"], st["tlm"] = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            elif op == "ET":
                st["inbt"] = False
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
                st["font"] = self.font(res, str(a[0]))
                st["size"] = _num(a[1])
            elif op in ("Td", "TD"):
                if op == "TD":
                    st["tl"] = -_num(a[1])
                st["tlm"] = _mul([1.0, 0.0, 0.0, 1.0, _num(a[0]), _num(a[1])], st["tlm"])
                st["tm"] = st["tlm"][:]
            elif op == "Tm":
                st["tlm"] = [_num(x) for x in a[:6]]
                st["tm"] = st["tlm"][:]
            elif op in ("T*", "'", '"'):
                if op == '"':
                    st["tw"], st["tc"] = _num(a[0]), _num(a[1])
                st["tlm"] = _mul([1.0, 0.0, 0.0, 1.0, 0.0, -st["tl"]], st["tlm"])
                st["tm"] = st["tlm"][:]
                if op != "T*":
                    self._show(vp, [a[-1]], st, in_form)
            elif op == "Tj":
                self._show(vp, [a[0]], st, in_form)
            elif op == "TJ":
                self._show(vp, list(a[0]), st, in_form)
            elif op == "Do":
                xo = (res.get_object().get("/XObject") or {}).get(str(a[0])) if res is not None else None
                xo = xo.get_object() if xo is not None else None
                if not isinstance(xo, StreamObject):
                    raise _fail("VA")
                if str(xo.get("/Subtype")) == "/Form":
                    saved = {
                        k: (v[:] if isinstance(v, list) else v)
                        for k, v in st.items()
                        if k not in ("stack", "path", "mc", "at")
                    }
                    st["ctm"] = _mul([_num(v) for v in xo.get("/Matrix", [1, 0, 0, 1, 0, 0])], st["ctm"])
                    self._run(vp, lex(_sdata(xo), vp.hidden), xo.get("/Resources") or res, st, True, depth + 1)
                    st.update(saved)
                else:
                    vp.painted.append(("image", self._unit(st["ctm"]), {}))
            elif op == "INLINE IMAGE":
                vp.painted.append(("inline_image", self._unit(st["ctm"]), {}))
            elif op == "sh":
                vp.painted.append(("shading", vp.crop, {}))
            elif op in ("BDC", "BMC"):
                props = a[1] if op == "BDC" and len(a) > 1 else None
                if isinstance(props, Name) and res is not None:
                    props = (res.get_object().get("/Properties") or {}).get(str(props))
                    props = props.get_object() if props is not None else None
                mcid = None
                at = None
                if isinstance(props, (dict, DictionaryObject)):
                    vp.props.append((str(a[0]), _props(props)))
                    vp.prop_texts.extend(t for v in props.values() for t in _text_strings(v))
                    if "/MCID" in props:
                        mcid = int(props["/MCID"])
                    if "/ActualText" in props:
                        at = (_text_of(props["/ActualText"]), len(vp.glyphs))
                st["mc"].append((str(a[0]) if a else "", mcid))
                st["at"].append(at)
            elif op == "EMC":
                if not st["mc"]:
                    raise _fail("VA")
                st["mc"].pop()
                span = st["at"].pop()
                if span is not None:
                    gs = vp.glyphs[span[1] :]
                    if "".join(g.text for g in gs) != span[0]:
                        raise _fail("VB")
                    for g in gs:
                        g.at = True

    def _unit(self, ctm: list[float]) -> tuple[float, float, float, float]:
        pts = [_pt(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def _geom(self, st: dict[str, Any]) -> tuple[tuple[float, float, float, float], int, int, bool] | None:
        pts: list[tuple[float, float]] = []
        segs = curves = 0
        path = st["path"]
        for op, a in path:
            if op == "re":
                x, y, w, h = a[:4]
                pts += [
                    _pt(st["ctm"], x, y),
                    _pt(st["ctm"], x + w, y + h),
                    _pt(st["ctm"], x + w, y),
                    _pt(st["ctm"], x, y + h),
                ]
                segs += 4
            elif op in ("m", "l"):
                pts.append(_pt(st["ctm"], a[0], a[1]))
                segs += op == "l"
            elif op in ("c", "v", "y"):
                for k in range(0, len(a) - 1, 2):
                    pts.append(_pt(st["ctm"], a[k], a[k + 1]))
                curves += 1
        if not pts:
            return None
        bbox = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
        ctm = st["ctm"]
        axis = (abs(ctm[1]) < 1e-9 and abs(ctm[2]) < 1e-9) or (abs(ctm[0]) < 1e-9 and abs(ctm[3]) < 1e-9)
        rect = len(path) == 1 and path[0][0] == "re" and axis
        if not rect:
            ops = [p[0] for p in path]
            if ops and ops[0] == "m" and all(o in ("l", "h") for o in ops[1:]):
                q = [_pt(ctm, *p[1][:2]) for p in path if p[0] in ("m", "l")]
                if len(q) == 5 and abs(q[0][0] - q[4][0]) < 0.01 and abs(q[0][1] - q[4][1]) < 0.01:
                    q = q[:4]
                rect = len(q) == 4 and all(
                    abs(q[k][0] - q[(k + 1) % 4][0]) <= 0.01 or abs(q[k][1] - q[(k + 1) % 4][1]) <= 0.01
                    for k in range(4)
                )
        return bbox, segs, curves, rect

    def _show(self, vp: VPage, elems: list[Any], st: dict[str, Any], in_form: bool) -> None:
        f: VFont | None = st["font"]
        if f is None or not st["inbt"]:
            raise _fail("VA")
        size, th = st["size"], st["th"]
        mc = tuple(st["mc"])
        for el in elems:
            if isinstance(el, bytes):
                data = el
                n = 2 if f.two else 1
                if len(data) % n:
                    raise _fail("VU")
                for i in range(0, len(data), n):
                    code = data[i : i + n]
                    u = f.text(code)
                    w0 = f.width(code)
                    tw = st["tw"] if (n == 1 and code == b" ") else 0.0
                    adv_t = (w0 / 1000.0 * size + st["tc"] + tw) * th
                    trm = _mul(_mul([size * th, 0.0, 0.0, size, 0.0, st["ts"]], st["tm"]), st["ctm"])
                    ox, oy = _pt(trm, 0, 0)
                    ex, ey = _pt(trm, 1, 0)
                    base = _mul(st["tm"], st["ctm"])
                    a0 = _pt(base, 0, 0)
                    a1 = _pt(_mul([1.0, 0.0, 0.0, 1.0, adv_t, 0.0], base), 0, 0)
                    q = [
                        _pt(trm, x, y)
                        for x, y in (
                            (0, f.des / 1000),
                            (w0 / 1000, f.des / 1000),
                            (w0 / 1000, f.asc / 1000),
                            (0, f.asc / 1000),
                        )
                    ]
                    box = (min(p[0] for p in q), min(p[1] for p in q), max(p[0] for p in q), max(p[1] for p in q))
                    sv = _mul(_mul([size, 0.0, 0.0, size, 0.0, 0.0], st["tm"]), st["ctm"])
                    em = math.hypot(sv[2], sv[3])
                    iy = f.ink(code, u)
                    inkv = inkh = None
                    if isinstance(iy, tuple):
                        p0, p1 = _pt(trm, iy[0] / 1000, iy[1] / 1000), _pt(trm, iy[2] / 1000, iy[3] / 1000)
                        inkv = (min(p0[1], p1[1]), max(p0[1], p1[1]))
                        inkh = (min(p0[0], p1[0]), max(p0[0], p1[0]))
                    hz = trm[0] > 0 and trm[3] > 0 and abs(trm[1]) <= 1e-6 * trm[0] and abs(trm[2]) <= 1e-6 * trm[3]
                    vp.glyphs.append(
                        VGlyph(
                            u,
                            code,
                            f,
                            size,
                            th,
                            st["tr"],
                            ox,
                            oy,
                            ex - ox,
                            ey - oy,
                            math.hypot(a1[0] - a0[0], a1[1] - a0[1]) * (1 if adv_t >= 0 else -1),
                            em,
                            box,
                            inkv,
                            iy is False,
                            st["fill"],
                            mc,
                            in_form,
                            hz,
                            wadv=w0 / 1000.0 * size * th * math.hypot(base[0], base[1]),
                            inkh=inkh,
                        )
                    )
                    st["tm"] = _mul([1.0, 0.0, 0.0, 1.0, adv_t, 0.0], st["tm"])
            else:
                st["tm"] = _mul([1.0, 0.0, 0.0, 1.0, -_num(el) / 1000.0 * size * th, 0.0], st["tm"])


def _sep(a: VGlyph, b: VGlyph) -> str | None:
    la, lb = math.hypot(a.ux, a.uy) or 1.0, math.hypot(b.ux, b.uy) or 1.0
    ua, ub = (a.ux / la, a.uy / la), (b.ux / lb, b.uy / lb)
    if abs(ua[0] - ub[0]) > 1e-3 or abs(ua[1] - ub[1]) > 1e-3:
        return "\n"
    end = (a.ox + a.adv * ua[0], a.oy + a.adv * ua[1])
    dx, dy = b.ox - end[0], b.oy - end[1]
    along, perp = dx * ua[0] + dy * ua[1], -dx * ua[1] + dy * ua[0]
    h = max(a.em, b.em)
    if abs(perp) > LINE_TOL * h or along < -LINE_TOL * h or along > COLUMN_GAP * h:
        return "\n"
    if along > SPACE_GAP * h and not a.text.isspace() and not b.text.isspace():
        return " "
    return None


def units_of(vp: VPage) -> None:
    parts: list[str] = []
    n = 0
    prev: VGlyph | None = None
    for i, g in enumerate(vp.glyphs):
        if prev is not None:
            s = _sep(prev, g)
            if s:
                vp.segs.append((n, n + 1, None))
                parts.append(s)
                n += 1
        vp.segs.append((n, n + len(g.text), i))
        parts.append(g.text)
        n += len(g.text)
        prev = g
    vp.text = "".join(parts)


def read_model(data: bytes) -> tuple[Any, list[VPage]]:
    r = open_strict(data)
    it = Interp(r)
    pages = []
    try:
        for pg in r.pages:
            vp = it.page(pg)
            units_of(vp)
            pages.append(vp)
    except Fail:
        raise
    except Exception:
        raise _fail("VA") from None
    return r, pages


# ---------------------------------------------------------------- coverage, zones (own rules)
def _white(p: tuple[str, Any, dict[str, Any]]) -> bool:
    d = p[2]
    return bool(p[0] == "path" and d.get("fill") and not d.get("stroke") and d.get("fill_color") in _WHITE)


def coverage(vp: VPage) -> None:
    """D-A08-03 re-checked on the original: RP3 grids, RP5 a filled rectangle behind text, RP7 a white backdrop."""
    boxes = [g.box for g in vp.glyphs if not g.text.isspace()]
    ta = (min(b[0] for b in boxes), max(b[2] for b in boxes)) if boxes else (vp.crop[0], vp.crop[2])
    area = (vp.crop[2] - vp.crop[0]) * (vp.crop[3] - vp.crop[1])
    thin = [
        p
        for p in vp.painted
        if p[0] == "path" and min(p[1][2] - p[1][0], p[1][3] - p[1][1]) <= 2.0 and not p[2]["curves"]
    ]
    hs = sorted(
        [
            [(p[1][1] + p[1][3]) / 2, p[1][0], p[1][2]]
            for p in thin
            if (p[1][3] - p[1][1]) <= 2.0 and (p[1][2] - p[1][0]) > (p[1][3] - p[1][1])
        ]
    )
    vs = sorted(
        [
            [(p[1][0] + p[1][2]) / 2, p[1][1], p[1][3]]
            for p in thin
            if not ((p[1][3] - p[1][1]) <= 2.0 and (p[1][2] - p[1][0]) > (p[1][3] - p[1][1]))
            and (p[1][2] - p[1][0]) <= 2.0
        ]
    )

    def merge(items: list[list[float]]) -> list[list[float]]:
        out: list[list[float]] = []
        for c, a, z in items:
            if out and abs(out[-1][0] - c) <= 0.75 and a <= out[-1][2] + 1.0:
                out[-1][1], out[-1][2] = min(out[-1][1], a), max(out[-1][2], z)
            else:
                out.append([c, a, z])
        return out

    hm, vm = merge(hs), merge(vs)
    grid: set[int] = set()
    if len(hm) >= 2 and len(vm) >= 2:
        x0, x1 = min(v[0] for v in vm), max(v[0] for v in vm)
        y0, y1 = min(h[0] for h in hm), max(h[0] for h in hm)
        if all(h[1] <= x0 + 1.0 and h[2] >= x1 - 1.0 for h in hm) and all(
            v[1] <= y0 + 1.0 and v[2] >= y1 - 1.0 for v in vm
        ):
            grid = {
                id(p)
                for p in thin
                if p[1][0] >= x0 - 1.5 and p[1][2] <= x1 + 1.5 and p[1][1] >= y0 - 1.5 and p[1][3] <= y1 + 1.5
            }
    for p in vp.painted:
        kind, b, d = p
        w, h = b[2] - b[0], b[3] - b[1]
        if kind == "path":
            if _white(p) and (w * h >= 0.5 * area or (b[0] <= ta[0] + 0.5 and b[2] >= ta[1] - 0.5)):
                continue
            if id(p) in grid:
                continue
            if (
                d["fill"]
                and not d["stroke"]
                and d["is_rect"]
                and any(gb[0] >= b[0] and gb[2] <= b[2] and gb[1] >= b[1] and gb[3] <= b[3] for gb in boxes)
                and not any(
                    o is not p
                    and o[1][0] > b[0] + 1
                    and o[1][2] < b[2] - 1
                    and o[1][1] > b[1] + 1
                    and o[1][3] < b[3] - 1
                    for o in vp.painted
                )
            ):
                continue
        raise Fail("uncovered_content", "VC")


def _cy(b: tuple[float, ...]) -> float:
    return (b[1] + b[3]) / 2


def zone(vp: VPage, idx: list[int]) -> dict[str, Any]:
    ink = [vp.glyphs[i] for i in idx if not vp.glyphs[i].text.isspace()]
    vb = (min(g.box[1] for g in ink), max(g.box[3] for g in ink))
    cy, h = (vb[0] + vb[1]) / 2, vb[1] - vb[0]
    sel = set(idx)
    line = [
        i
        for i, g in enumerate(vp.glyphs)
        if not g.text.isspace() and g.box[2] > g.box[0] and abs(_cy(g.box) - cy) < h / 2
    ]
    lid = set(line)
    x0 = ink[0].box[0]
    left = min([x0] + [g.inkh[0] for g in ink if g.inkh is not None])  # ink left of the origin (italics)
    x_end = max([g.box[2] for g in ink] + [g.inkh[1] for g in ink if g.inkh is not None])
    band = [min(vp.glyphs[i].box[1] for i in line), max(vp.glyphs[i].box[3] for i in line)]
    for i, g in enumerate(vp.glyphs):
        if i in lid or g.text.isspace() or g.box[2] <= g.box[0] or g.ink_empty:
            continue
        y0, y1 = g.inkv if g.inkv is not None else (g.box[1], g.box[3])
        if g.box[0] < vp.crop[2] and g.box[2] > x0 and y0 < band[1] and y1 > band[0]:
            if (y0 + y1) / 2 > (band[0] + band[1]) / 2:
                band[1] = min(band[1], y0)
            else:
                band[0] = max(band[0], y1)
    size = max(g.em for g in ink)
    margin = ZONE_MARGIN_EM * size
    last_cx = (ink[-1].box[0] + ink[-1].box[2]) / 2
    nxt = [
        vp.glyphs[i].box[0]
        for i in line
        if i not in sel and (vp.glyphs[i].box[0] + vp.glyphs[i].box[2]) / 2 > last_cx + 0.01
    ]
    ra = min(nxt) if nxt else math.inf
    rb = math.inf
    for p in vp.painted:
        b = p[1]
        if b[1] >= band[1] or b[3] <= band[0] or _white(p):
            continue
        if b[0] > x0 + 0.01:
            rb = min(rb, b[0])
        elif b[2] > x0 + 0.01:
            rb = min(rb, b[2])
    rc = max(g.box[2] for g in vp.glyphs if not g.text.isspace() and g.box[2] > g.box[0])
    rd = vp.crop[2]
    return {
        "x0": x0,
        "left": left,
        "band": band,
        "r_z1": max(min(ra, rb, rc, rd) - margin, x_end),
        "r_line": max(min(rb, rc, rd) - margin, x_end),
    }


def _in(g: VGlyph, rect: list[float], tol: float = 0.01) -> bool:
    """A glyph belongs to a zone when its centre is inside AND it ends inside (a straddling glyph stays put; the
    planner applies the same rule with the same 0.01 pt; the restoration is checked with the same rule)."""
    cx, cy = (g.box[0] + g.box[2]) / 2, _cy(g.box)
    right = max(g.box[2], g.inkh[1]) if g.inkh is not None else g.box[2]
    return rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3] and right <= rect[2] + tol


def _decoded(o: Any) -> list[str]:
    from pypdf.generic import ByteStringObject, NameObject, TextStringObject

    o = o.get_object() if hasattr(o, "get_object") else o
    out: list[str] = []
    if isinstance(o, bytes):
        for enc in ("utf-8", "utf-16-be", "utf-16-le", "latin-1"):
            try:
                out.append(o.decode(enc))
            except UnicodeDecodeError:
                pass
    elif isinstance(o, Name):
        out.append(str(o))
    elif isinstance(o, TextStringObject):
        out.append(str(o))
        raw = bytes(o.get_original_bytes())
        for enc in ("utf-8", "utf-16-be", "utf-16-le", "latin-1"):
            try:
                out.append(raw.decode(enc))
            except UnicodeDecodeError:
                pass
    elif isinstance(o, ByteStringObject):
        for enc in ("utf-8", "utf-16-be", "utf-16-le", "latin-1"):
            try:
                out.append(bytes(o).decode(enc))
            except UnicodeDecodeError:
                pass
    elif isinstance(o, NameObject):
        out.append(str(o))
    out += [urllib.parse.unquote(x) for x in list(out)]
    return out


def _text_strings(o: Any) -> list[str]:
    """A PDF STRING decoded as text (UTF-16 with BOM, UTF-8 with BOM, PDFDocEncoding/Latin-1), plus its
    URL-decoded form; names, numbers and other objects give nothing. Used for values of any length."""
    from pypdf.generic import ByteStringObject, TextStringObject

    o = o.get_object() if hasattr(o, "get_object") else o
    out: list[str] = []
    if isinstance(o, TextStringObject):
        out.append(str(o))
        out.append(_text_of(bytes(o.get_original_bytes())))
    elif isinstance(o, (bytes, ByteStringObject)):
        out.append(_text_of(bytes(o)))
    out += [urllib.parse.unquote(x) for x in list(out)]
    return out


def _hit_any_length(texts: list[str], values: list[str]) -> bool:
    """Substring of any representation of any value, WITHOUT a minimum length, in decoded text."""
    folded = [t.casefold() for t in texts]
    return any(r.casefold() in t for t in folded for v in values for r in reps(v))


def reps(v: str) -> set[str]:
    out = set()
    for n in (v, unicodedata.normalize("NFC", v), unicodedata.normalize("NFD", v)):
        for x in (n, n.casefold()):
            out.add(x)
            out.add(" ".join(x.split()))
    return {x for x in out if x.strip()}


def _walk(r: Any) -> tuple[list[Any], set[int], set[int]]:
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject

    seen: set[int] = set()
    out: list[Any] = []
    stack: list[Any] = [r.trailer]
    while stack:
        o = stack.pop()
        if isinstance(o, IndirectObject):
            if o.idnum in seen:
                continue
            seen.add(o.idnum)
            o = o.get_object()
        out.append(o)
        if isinstance(o, (DictionaryObject, StreamObject)):
            for k in o:
                v = o.raw_get(k)
                if k in ("/Parent", "/P", "/Pg") and not isinstance(v, IndirectObject):
                    continue
                stack.append(v)
        elif isinstance(o, ArrayObject):
            stack.extend(o)
    ids: set[int] = set()
    for _gen, xs in r.xref.items():
        ids |= set(xs.keys())
    ids |= set(getattr(r, "xref_objStm", {}).keys())
    return out, seen, ids


def _structure(ref: Any, out: Any) -> None:
    """VG, VN, VE, VS: page geometry, annotations (minus the removed comments), removed classes absent, no orphan."""
    if len(out.pages) != len(ref.pages):
        raise _fail("VG")
    for a, b in zip(out.pages, ref.pages, strict=True):
        for k in ("/MediaBox", "/CropBox", "/Rotate", "/UserUnit", "/BleedBox", "/TrimBox", "/ArtBox"):
            va, vb = a.get(k), b.get(k)
            if (va is None) != (vb is None):
                raise _fail("VG")
            if va is not None:
                la = [float(x) for x in (va if isinstance(va, list) else [va])]
                lb = [float(x) for x in (vb if isinstance(vb, list) else [vb])]
                if la != lb:
                    raise _fail("VG")

    def annots(rd: Any) -> list[Any]:
        res = []
        for pi, p in enumerate(rd.pages):
            for an in p.get("/Annots", []) or []:
                an = an.get_object()
                if an.get("/Subtype") in ("/Text", "/Popup"):
                    continue
                act = an.get("/A")
                act = act.get_object() if act is not None else None
                res.append(
                    (
                        pi,
                        str(an.get("/Subtype")),
                        tuple(round(float(v), 3) for v in an.get("/Rect", [])),
                        str(act.get("/S")) if act else None,
                        "|".join(_decoded(act.get("/URI"))) if act and "/URI" in act else None,
                    )
                )
        return res

    if annots(out) != annots(ref):
        raise _fail("VN")
    info = out.trailer.get("/Info")
    if info is not None and any(k not in ("/CreationDate", "/ModDate") for k in info.get_object()):
        raise _fail("VE")
    objs, reach, ids = _walk(out)
    from pypdf.generic import DictionaryObject, StreamObject

    for o in objs:
        if isinstance(o, (DictionaryObject, StreamObject)):
            if "/Metadata" in o or o.get("/Type") == "/Metadata" or o.get("/Subtype") in ("/Text", "/Popup"):
                raise _fail("VE")
    if ids - reach:
        raise _fail("VS")


def _blanks(a: VPage, b: VPage, rects: list[list[float]], tol: float = 0.01) -> None:
    """Whitespace (E-A08-27): every whitespace glyph of ``a`` outside the zones is still in ``b`` at its position,
    and every whitespace glyph that ``b`` adds outside the zones is KNOWN to have no ink (embedded program, empty
    glyph); inside a zone, whitespace is the moved text's own spacing."""
    have: dict[tuple[str, float, float], int] = {}
    for g in a.glyphs:
        if g.text.isspace():
            k = (g.text, round(g.ox, 2), round(g.oy, 2))
            have[k] = have.get(k, 0) + 1
    seen: dict[tuple[str, float, float], int] = {}
    for g in b.glyphs:
        if not g.text.isspace():
            continue
        k = (g.text, round(g.ox, 2), round(g.oy, 2))
        if seen.get(k, 0) < have.get(k, 0):
            seen[k] = seen.get(k, 0) + 1
        elif not g.ink_empty and not any(_in(g, r, tol) for r in rects):
            raise _fail("VT")  # outside the zones, an added blank must be provably invisible
    for g in a.glyphs:
        if g.text.isspace() and not any(_in(g, r, tol) for r in rects):
            k = (g.text, round(g.ox, 2), round(g.oy, 2))
            if seen.get(k, 0) < 1:
                raise _fail("VT")


def _candidate_font_ok(f: VFont) -> bool:
    from pypdf.generic import StreamObject

    m = _candidate()
    import hashlib

    o = f.obj
    try:
        fd = o["/FontDescriptor"].get_object()
        ff = fd["/FontFile2"].get_object()
        if not isinstance(ff, StreamObject) or hashlib.sha256(ff.get_data()).hexdigest() != m["subset_sha256"]:
            return False
        if str(o.get("/Subtype")) != "/TrueType" or str(o.get("/Encoding")) != "/WinAnsiEncoding":
            return False
        if int(o.get("/FirstChar")) != 32:
            return False
        ws = [int(v) for v in o["/Widths"]]
        if ws != [int(m["widths"].get(chr(c), 0)) for c in range(32, 32 + len(ws))]:
            return False
        tu, _ = cmap_of(_sdata(o["/ToUnicode"]))
        return all(len(k) == 1 and v == chr(k[0]) for k, v in tu.items())
    except Exception:
        return False


# ---------------------------------------------------------------- protect
def _sel_idx(vp: VPage, start: int, end: int) -> list[int]:
    idx = [i for s, e, i in vp.segs if i is not None and s < end and e > start]
    if not idx or any(s < start or e > end for s, e, i in vp.segs if i is not None and s < end and e > start):
        raise _fail("VU")
    return idx


def _runs(vp: VPage, idx: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    key = None
    for i in idx:
        g = vp.glyphs[i]
        k = (g.font.name, round(g.size, 6), round(g.th, 6), round(g.em, 4))
        if out and k == key:
            out[-1]["n"] += len(g.text)
            out[-1]["adv"] += g.adv
        else:
            out.append({"font": g.font.name, "size": g.size, "em": g.em, "th": g.th, "n": len(g.text), "adv": g.adv})
            key = k
    return out


def _close(a: float, b: float, tol: float = POS_TOL) -> bool:
    return abs(float(a) - float(b)) <= tol


def protect(original: bytes, derivative: bytes, p: dict[str, Any]) -> dict[str, Any]:
    ro, op = read_model(original)
    rd, dp = read_model(derivative)
    _structure(ro, rd)
    units = p["units"]
    if [vp.text for vp in op] != units:
        raise _fail("VU")  # the two independent interpretations of the original disagree
    for vp in op:
        coverage(vp)
    sels = p["selections"]
    cand_w = _candidate()["widths"]
    # zones, from the ORIGINAL only
    info: dict[int, dict[str, Any]] = {}
    for k, s in enumerate(sels):
        pno = int(s["unit_id"][1:]) - 1
        vp = op[pno]
        idx = _sel_idx(vp, int(s["start"]), int(s["end"]))
        if vp.text[int(s["start"]) : int(s["end"])] != s["value"]:
            raise _fail("VU")
        g0, last = vp.glyphs[idx[0]], vp.glyphs[idx[-1]]
        nat = sum(cand_w[c] for c in s["substitute"]) / 1000.0 * math.hypot(g0.ux, g0.uy) / g0.th
        info[k] = {
            "page": pno,
            "idx": idx,
            "zone": zone(vp, idx),
            "x_orig": g0.ox,
            "span": last.ox + last.adv - g0.ox,
            "nat": nat,
            "runs": _runs(vp, idx),
            "size": g0.size,
        }
    layout = p["layout"]
    by_key = {(s["unit_id"], int(s["start"]), int(s["end"])): k for k, s in enumerate(sels)}
    zones: list[dict[str, Any]] = []
    placed: set[int] = set()
    for ln in layout:
        ks: list[int] = []
        for ls in ln["sels"]:
            hit = by_key.get((f"u{int(ln['page']) + 1:04d}", int(ls["unit_start"]), int(ls["unit_end"])))
            if hit is None:
                raise _fail("VZP")
            k = hit
            if k in placed or info[k]["page"] != int(ln["page"]):
                raise _fail("VZP")
            s = sels[k]
            if ls["action"] != s["action"] or ls["substitute"] != s["substitute"]:
                raise _fail("VZP")
            placed.add(k)
            ks.append(k)
        if not ks:
            raise _fail("VZP")
        bands = [info[k]["zone"]["band"] for k in ks]
        union = [min(b[0] for b in bands), max(b[1] for b in bands)]
        if ln["kind"] == "Z1":
            for k, ls in zip(ks, ln["sels"], strict=True):
                z = info[k]["zone"]
                zones.append(
                    {
                        "page": int(ln["page"]),
                        "kind": "Z1",
                        "ks": [k],
                        "rect": [z["left"], z["band"][0], z["r_z1"], z["band"][1]],
                        "line": ln,
                    }
                )
                if not _close(ls["x_new"], z["x0"]) or not _close(ls["zone_r"], z["r_z1"]):
                    raise _fail("VZP")
            if float(ln["r"]) > max(info[k]["zone"]["r_z1"] for k in ks) + POS_TOL:
                raise _fail("VZP")
        elif ln["kind"] == "Z2":
            same = [
                q
                for q in info
                if info[q]["page"] == info[ks[0]]["page"]
                and abs(info[q]["zone"]["band"][0] - bands[0][0]) < 0.5
                and abs(info[q]["zone"]["band"][1] - bands[0][1]) < 0.5
            ]
            if sorted(same) != sorted(ks):
                raise _fail("VZP")  # a Z2 line groups exactly the selections of one line
            r_line = min(info[k]["zone"]["r_line"] for k in ks)
            rect = [min(info[k]["zone"]["left"] for k in ks), union[0], r_line, union[1]]
            if float(ln["r"]) > r_line + POS_TOL or not _close(ln["x0"], min(info[k]["zone"]["x0"] for k in ks)):
                raise _fail("VZP")  # the plan never widens the authorized zone (C-2B-05)
            if any(not _close(ls["zone_r"], r_line) for ls in ln["sels"]):
                raise _fail("VZP")
            zones.append({"page": int(ln["page"]), "kind": "Z2", "ks": ks, "rect": rect, "line": ln})
        else:
            raise _fail("VZP")
        # VBND: every number the restorer will use, against the verifier's own computation from the original
        if not all(_close(a, b) for a, b in zip(ln["band"], union, strict=True)):
            raise _fail("VBND")
        if not _close(ln["left"], min(info[k]["zone"]["left"] for k in ks)):
            raise _fail("VBND")
        for k, ls in zip(ks, ln["sels"], strict=True):
            it = info[k]
            if not all(_close(a, b) for a, b in zip(ls["band"], it["zone"]["band"], strict=True)):
                raise _fail("VBND")
            if not _close(ls["left"], it["zone"]["left"]):
                raise _fail("VBND")
            if not (
                _close(ls["x_orig"], it["x_orig"])
                and _close(ls["span"], it["span"])
                and _close(ls["nat"], it["nat"])
                and _close(ls["size"], it["size"], 1e-6)
            ):
                raise _fail("VBND")
            if len(ls["runs"]) != len(it["runs"]):
                raise _fail("VBND")
            for a, b in zip(ls["runs"], it["runs"], strict=True):
                if a["font"] != b["font"] or int(a["n"]) != b["n"]:
                    raise _fail("VBND")
                if not (
                    _close(a["size"], b["size"], 1e-6)
                    and _close(a["em"], b["em"])
                    and _close(a["th"], b["th"], 1e-6)
                    and _close(a["adv"], b["adv"])
                ):
                    raise _fail("VBND")
    if len(placed) != len(sels):
        raise _fail("VZP")
    # VT: every glyph outside the zones is identical (text, position, advance, size, font, colour, MCID, AT)
    for pno, (a, b) in enumerate(zip(op, dp, strict=True)):
        rects = [z["rect"] for z in zones if int(z["page"]) == pno]
        oa = [g for g in a.glyphs if not g.text.isspace() and not any(_in(g, r) for r in rects)]
        ob = [g for g in b.glyphs if not g.text.isspace() and not any(_in(g, r) for r in rects)]
        _blanks(a, b, rects)
        if len(oa) != len(ob):
            raise _fail("VT")
        for x, y in zip(oa, ob, strict=True):
            if (
                x.text != y.text
                or not (_close(x.ox, y.ox) and _close(x.oy, y.oy) and _close(x.adv, y.adv) and _close(x.em, y.em))
                or x.font.name != y.font.name
                or x.fill != y.fill
                or x.tr != y.tr
                or x.mc != y.mc
                or x.at != y.at
            ):
                raise _fail("VT")
    # VB: non-text painting and marked-content property lists identical
    for a, b in zip(op, dp, strict=True):
        if a.nontext != b.nontext or a.props != b.props or a.painted != b.painted:
            raise _fail("VB")
    geometry = []
    for z in zones:
        geometry.append(_zone_protect(z, op[int(z["page"])], dp[int(z["page"])], info, sels))
    # O1 (whitespace-insensitive) and markers: the derivative text is the oracle; substitutes whole, no alteration
    for pno, (a, b) in enumerate(zip(op, dp, strict=True)):
        text = a.text
        for k in sorted([k for k in info if info[k]["page"] == pno], key=lambda q: -int(sels[q]["start"])):
            text = text[: int(sels[k]["start"])] + sels[k]["substitute"] + text[int(sels[k]["end"]) :]
        if "".join(text.split()) != "".join(b.text.split()):
            raise _fail("VO1")
        found = [m.group(0) for m in _STRICT.finditer(b.text)]
        exp = [
            sels[k]["substitute"]
            for k in sorted(info, key=lambda q: int(sels[q]["start"]))
            if info[k]["page"] == pno and sels[k]["action"] == "pseudonymize"
        ]
        orig_markers = [m.group(0) for m in _STRICT.finditer(a.text)]
        if (
            orig_markers
            or found != exp
            or b.text.count(REDACTED) - a.text.count(REDACTED)
            != sum(1 for k in info if info[k]["page"] == pno and sels[k]["action"] == "redact")
        ):
            raise _fail("VO1")
        stripped = _STRICT.sub("", b.text)
        if any(rx.search(stripped) for rx in _ALTERED):
            raise _fail("VO1")
    scope = _residues(rd, op, dp, sels, info, derivative)
    return {"ok": True, "geometry": geometry, "inspection_scope": scope}


def _zone_protect(
    z: dict[str, Any], a: VPage, b: VPage, info: dict[int, Any], sels: list[dict[str, Any]]
) -> dict[str, Any]:
    rect = z["rect"]
    ln = z["line"]
    m = _candidate()
    inside = sorted([g for g in b.glyphs if not g.text.isspace() and _in(g, rect)], key=lambda g: g.ox)
    selected = {i for k in z["ks"] for i in info[k]["idx"]}
    items: list[tuple[str, float, Any]] = [("s", info[k]["x_orig"], k) for k in z["ks"]]
    if z["kind"] == "Z2":
        items += [
            ("c", g.ox, g)
            for i, g in enumerate(a.glyphs)
            if i not in selected and not g.text.isspace() and _in(g, rect)
        ]
    items.sort(key=lambda t: t[1])
    exp = "".join(sels[o]["substitute"] if kind == "s" else o.text for kind, _x, o in items)
    if "".join(g.text for g in inside) != "".join(exp.split()):
        raise _fail("VZ")
    # map the derivative's glyphs onto the items; substitutes must be the frozen candidate font
    pos = 0
    mapped: list[tuple[str, Any, Any]] = []
    for kind, _x, o in items:
        if kind == "s":
            sub = "".join(sels[o]["substitute"].split())
            seg = inside[pos : pos + len(sub)]
            if "".join(g.text for g in seg) != sub or any(not _candidate_font_ok(g.font) for g in seg):
                raise _fail("VL")
            mapped.append(("s", o, seg))
            pos += len(sub)
        else:
            mapped.append(("c", o, inside[pos]))
            pos += 1
    band = rect[1], rect[3]
    ratios = []
    for kind, o, seg in mapped:
        if kind != "s":
            continue
        g0 = seg[0]
        lo, hi = (
            min(m["y_extent"][g.text][0] for g in seg if g.text in m["y_extent"]),
            max(m["y_extent"][g.text][1] for g in seg if g.text in m["y_extent"]),
        )
        if g0.oy + lo / 1000 * g0.em < band[0] - V_TOL or g0.oy + hi / 1000 * g0.em > band[1] + V_TOL:
            raise _fail("VO")
        nat = sum(m["widths"][g.text] for g in seg) / 1000.0 * math.hypot(g0.ux, g0.uy) / g0.th
        w = seg[-1].ox + seg[-1].adv - g0.ox
        if seg[-1].ox + seg[-1].adv > rect[2] + V_TOL or g0.ox < rect[0] - V_TOL:
            raise _fail("VO")
        ratios.append(w / nat)
        if not (MIN_SCALE - 0.002 <= w / nat <= 1.002):
            raise _fail("VL")
        if not _close(g0.oy, a.glyphs[info[o]["idx"][0]].oy) or not _close(g0.em, a.glyphs[info[o]["idx"][0]].em):
            raise _fail("VO")
        if g0.mc != a.glyphs[info[o]["idx"][0]].mc:
            raise _fail("VB")
    if z["kind"] == "Z1":
        k = z["ks"][0]
        seg = mapped[0][2]
        if not _close(seg[0].ox, info[k]["x_orig"]):
            raise _fail("VZ")
        return {"kind": "Z1", "ratio": round(ratios[0], 4)}
    # Z2: x' = a1 + f (x - a1) + sum of (w'_k - f span_k) of the substitutes to the left; one f for the line
    num = den = 0.0
    for (k1, o1, d1), (k2, o2, d2) in zip(mapped, mapped[1:], strict=False):
        if k1 == "c" and k2 == "c":
            dx, dxp = o2.ox - o1.ox, d2.ox - d1.ox
            num, den = num + dx * dxp, den + dx * dx
    f = num / den if den > 1e-9 else ratios[0]
    if not (MIN_SCALE - 0.002 <= f <= 1.002):
        raise _fail("VL")
    if any(abs(r - f) > 0.003 for r in ratios):
        raise _fail("Z2")
    if abs(f - float(ln["f"])) > 0.002:
        raise _fail("Z2")
    a1 = info[z["ks"][0]]["x_orig"]
    shift = 0.0
    moved = []
    for kind, o, d in mapped:
        if kind == "s":
            pred = a1 + f * (info[o]["x_orig"] - a1) + shift
            if abs(d[0].ox - pred) > 0.05:
                raise _fail("Z2")
            shift += (d[-1].ox + d[-1].adv - d[0].ox) - f * info[o]["span"]
        else:
            pred = a1 + f * (o.ox - a1) + shift
            if abs(d.ox - pred) > 0.05 or abs(d.adv - f * o.wadv) > 0.05:
                raise _fail("Z2")
            if (
                not (_close(d.oy, o.oy) and _close(d.em, o.em))
                or d.font.name != o.font.name
                or d.fill != o.fill
                or d.mc != o.mc
                or d.at
                or o.at
            ):
                raise _fail("Z2")
            moved.append((o, d))
    # VBND for the moved glyphs (non-space ones; spaces are moved too and are recorded with the rest)
    lm = sorted(ln["moved"], key=lambda q: float(q["x_new"]))
    ink_moved = [mv for mv in lm if any(_close(mv["x_orig"], o.ox) and _close(mv["x_new"], d.ox) for o, d in moved)]
    if len(ink_moved) != len(moved):
        raise _fail("VBND")
    for mv in lm:
        hit = [g for g in a.glyphs if _close(g.ox, mv["x_orig"]) and _in(g, rect) and _close(g.th, mv["th"], 1e-6)]
        if not hit:
            raise _fail("VBND")
    return {"kind": "Z2", "f": round(f, 4), "moved": len(moved)}


_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)


_HEXSTR = re.compile(rb"(?<!<)<([0-9A-Fa-f\s]+)>(?!>)")


def _syntax_only(raw: bytes) -> bytes:
    """The file with every stream body and every hexadecimal string blanked: what remains is the literal syntax
    (names, literal strings, comments). Stream contents and hex strings are scanned DECODED elsewhere; their raw
    digits (an /ID, compressed data) cannot carry readable text and would only produce chance matches."""
    out = _STREAM.sub(lambda m: b"stream\n" + b"\x00" * len(m.group(1)) + b"endstream", raw)
    return _HEXSTR.sub(lambda m: b"<" + b"\x00" * len(m.group(1)) + b">", out)


def _residues(
    rd: Any, op: list[VPage], dp: list[VPage], sels: list[dict[str, Any]], info: dict[int, Any], raw_file: bytes
) -> dict[str, Any]:
    """VK, VX, VP: no selected value anywhere but its permitted occurrences (identical, not selected, in place)."""
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, StreamObject

    values = sorted({s["value"] for s in sels})
    perm: dict[tuple[int, str], int] = {}
    for pno, vp in enumerate(op):
        taken = [(int(sels[k]["start"]), int(sels[k]["end"])) for k in info if info[k]["page"] == pno]
        for v in values:
            n = 0
            for m in re.finditer(re.escape(v), vp.text):
                if not any(m.start() < e and m.end() > s for s, e in taken):
                    n += 1
            perm[(pno, v)] = n
    for pno, vp in enumerate(dp):
        # a match ENTIRELY inside an emitted substitute is the substitute, not a copy (A07 6.3.1): masked out
        text = _STRICT.sub(lambda m: "\x00" * len(m.group(0)), vp.text).replace(REDACTED, "\x00" * len(REDACTED))
        for v in values:
            if text.count(v) != perm[(pno, v)]:
                raise Fail("residue_detected", "VP")
            for r in reps(v):
                hay = text.casefold() if r == r.casefold() else text
                if " ".join(hay.split()).count(" ".join(r.split())) > perm[(pno, v)]:
                    raise Fail("residue_detected", "VX")
    # the codes of every font run decoded with EVERY font of the document (a value re-encoded with another font)
    uniq = {id(g.font): g.font for vp in dp for g in vp.glyphs}
    cand_ok: dict[int, bool] = {}
    for pno, vp in enumerate(dp):
        runs: list[tuple[VFont, bytes]] = []
        for g in vp.glyphs:
            if runs and runs[-1][0] is g.font:
                runs[-1] = (g.font, runs[-1][1] + g.code)
            else:
                runs.append((g.font, g.code))
        for src, raw in runs:
            if id(src) not in cand_ok:
                cand_ok[id(src)] = _candidate_font_ok(src)
            if cand_ok[id(src)]:
                continue  # the emitted substitutes themselves (verified to be exactly them in their zones)
            for f in uniq.values():
                if f is src or f.two != src.two:
                    continue
                n = 2 if f.two else 1
                dec = "".join(f.to_u.get(raw[i : i + n], "\x00") for i in range(0, len(raw) - n + 1, n))
                for v in values:
                    for r in reps(v):
                        hay = dec.casefold() if r == r.casefold() else dec
                        if len(r) >= 3 and hay.count(r) > perm[(pno, v)]:
                            raise Fail("residue_detected", "VX")
    objs, _reach, _ids = _walk(rd)
    content_ids = set()
    for pg in rd.pages:
        c = pg.get("/Contents")
        if c is None:
            continue
        c = c.get_object()
        for x in c if isinstance(c, ArrayObject) else [c]:
            content_ids.add(id(x.get_object()))
    n_strings = n_streams = 0
    for o in objs:
        items = (
            list(o.values())
            if isinstance(o, (DictionaryObject, StreamObject))
            else (list(o) if isinstance(o, ArrayObject) else [o])
        )
        # text surfaces (strings, link URIs, destination names): every value, whatever its length (contract 10.4)
        texts: list[str] = []
        if isinstance(o, (DictionaryObject, StreamObject)):
            for k in o:
                v = o.raw_get(k)
                texts += _text_strings(v)
                if str(k) in ("/Dest", "/D") and isinstance(v, NameObject):
                    texts.append(str(v)[1:])
        elif isinstance(o, ArrayObject):
            for v in o:
                texts += _text_strings(v)
        else:
            texts += _text_strings(o)
        if _hit_any_length(texts, values):
            raise Fail("residue_detected", "VK")
        keys = [str(k) for k in o] if isinstance(o, (DictionaryObject, StreamObject)) else []
        for it in items + keys:
            for t in _decoded(it) if not isinstance(it, str) else [it, urllib.parse.unquote(it)]:
                n_strings += 1
                tf = t.casefold()
                if any(r.casefold() in tf for v in values for r in reps(v) if len(r) >= 3):
                    raise Fail("residue_detected", "VK")
        if isinstance(o, StreamObject) and id(o) not in content_ids and o.get("/Subtype") != "/Form":
            try:
                d = o.get_data()
            except Exception:
                raise Fail("residue_detected", "VK") from None
            n_streams += 1
            for v in values:
                for r in reps(v):
                    for enc in ("utf-8", "utf-16-be", "utf-16-le"):
                        bb = r.encode(enc, "ignore")
                        if len(bb) >= 3 and bb in d:
                            raise Fail("residue_detected", "VK")
    for vp in dp:
        # comments and strings of the content streams are text: any length; names and image data are binary-like
        texts = [_text_of(b) for kind, b in vp.hidden if kind in ("comment", "string")]
        texts += [b.decode("utf-8", "replace") for kind, b in vp.hidden if kind == "comment"]
        if _hit_any_length(texts + vp.prop_texts, values):
            raise Fail("residue_detected", "VK")
        for _kind, blob in vp.hidden:
            for v in values:
                for r in reps(v):
                    for enc in ("utf-8", "utf-16-be", "utf-16-le", "latin-1"):
                        try:
                            bb = r.encode(enc)
                        except UnicodeEncodeError:
                            continue
                        if len(bb) >= 3 and (bb in blob or bb.lower() in blob.lower()):
                            raise Fail("residue_detected", "VK")
        for _tag, props in vp.props:
            for _k, val in props:
                if any(r.casefold() in val.casefold() for v in values for r in reps(v) if len(r) >= 3):
                    raise Fail("residue_detected", "VK")
    syntax = _syntax_only(raw_file)  # stream bodies are scanned DECODED above; compressed bytes cannot carry text
    for v in values:
        for r in reps(v):
            for enc in ("utf-8", "utf-16-be", "utf-16-le"):
                bb = r.encode(enc, "ignore")
                if len(bb) >= 3 and bb in syntax:
                    raise Fail("residue_detected", "VK")
    return {
        "objects": len(objs),
        "strings_and_names": n_strings,
        "non_content_streams": n_streams,
        "pages": len(dp),
        "representations": ["exact", "nfc", "nfd", "casefold", "whitespace", "url", "utf-16", "any_font"],
        "not_covered": ["glyph_shapes_of_embedded_fonts", "pixels_of_images"],
    }


# ---------------------------------------------------------------- restore
def restore(derivative: bytes, restored: bytes, p: dict[str, Any]) -> dict[str, Any]:
    rd, dp = read_model(derivative)
    rr, rp = read_model(restored)
    _structure(rd, rr)
    layout = p["layout"]
    values: dict[str, str] = p["values"]
    zones = []
    for ln in layout:
        band = [float(v) for v in ln["band"]]
        if ln["kind"] == "Z1":
            for s in ln["sels"]:
                sb = [float(v) for v in s["band"]]
                zones.append(
                    {
                        "page": int(ln["page"]),
                        "rect": [float(s["left"]), sb[0], float(s["zone_r"]), sb[1]],
                        "ln": ln,
                        "sels": [s],
                    }
                )
        else:
            zones.append(
                {
                    "page": int(ln["page"]),
                    "rect": [float(ln["left"]), band[0], float(ln["sels"][0]["zone_r"]), band[1]],
                    "ln": ln,
                    "sels": ln["sels"],
                }
            )
    for pno, (a, b) in enumerate(zip(dp, rp, strict=True)):
        rects = [z["rect"] for z in zones if z["page"] == pno]
        oa = [g for g in a.glyphs if not g.text.isspace() and not any(_in(g, r) for r in rects)]
        ob = [g for g in b.glyphs if not g.text.isspace() and not any(_in(g, r) for r in rects)]
        _blanks(a, b, rects)
        if len(oa) != len(ob) or any(
            x.text != y.text
            or not (_close(x.ox, y.ox) and _close(x.oy, y.oy) and _close(x.adv, y.adv))
            or x.font.name != y.font.name
            or x.mc != y.mc
            for x, y in zip(oa, ob, strict=True)
        ):
            raise _fail("VT")
        if a.nontext != b.nontext or a.props != b.props or a.painted != b.painted:
            raise _fail("VB")
    for z in zones:
        _zone_restore(z, dp[z["page"]], rp[z["page"]], values)
    for pno, (a, b) in enumerate(zip(dp, rp, strict=True)):
        text = a.text
        for ln in layout:
            if int(ln["page"]) != pno:
                continue
            for s in ln["sels"]:
                if s["action"] == "pseudonymize":
                    text = text.replace(s["substitute"], values[s["sid"]], 1)
        if "".join(text.split()) != "".join(b.text.split()):
            raise _fail("VO1")
        if _STRICT.search(b.text) or b.text.count(REDACTED) != a.text.count(REDACTED):
            raise _fail("VM")
    return {"ok": True}


def _zone_restore(z: dict[str, Any], a: VPage, b: VPage, values: dict[str, str]) -> None:
    rect, ln = z["rect"], z["ln"]
    sels = sorted(z["sels"], key=lambda s: float(s["x_orig"]))
    excess: dict[str, float] = {}
    scale: dict[str, float] = {}
    m = _candidate()
    if ln["kind"] == "Z2":
        for s in sels:
            if s["action"] == "redact":
                sc = min(1.0, float(s["span"]) / float(s["nat"])) if float(s["nat"]) > 0 else 1.0
                scale[s["sid"]] = max(sc, MIN_SCALE)
                excess[s["sid"]] = MIN_SCALE * float(s["nat"]) - float(s["span"]) if sc < MIN_SCALE else 0.0

    def e_left(x: float) -> float:
        return sum(excess.get(s["sid"], 0.0) for s in sels if float(s["x_orig"]) < x - 1e-6)

    ins = sorted([g for g in b.glyphs if not g.text.isspace() and _in(g, rect)], key=lambda g: g.ox)
    der = sorted([g for g in a.glyphs if not g.text.isspace() and _in(g, rect)], key=lambda g: g.ox)
    # the derivative's sequence: each substitute starts at its recorded x_new; the rest are moved glyphs
    by_new = sorted(sels, key=lambda s: float(s["x_new"]))
    items: list[tuple[str, Any]] = []
    pos = j = 0
    while pos < len(der):
        s = by_new[j] if j < len(by_new) else None
        sub = "".join(s["substitute"].split()) if s is not None else ""
        if s is not None and _close(der[pos].ox, float(s["x_new"]), 0.05):
            if "".join(g.text for g in der[pos : pos + len(sub)]) != sub:
                raise _fail("VZ")
            items.append(("s", s))
            pos += len(sub)
            j += 1
        else:
            items.append(("c", der[pos]))
            pos += 1
    if j != len(by_new):
        raise _fail("VZ")
    exp = "".join(
        (values[o["sid"]] if o["action"] == "pseudonymize" else REDACTED) if k == "s" else o.text for k, o in items
    )
    if "".join(g.text for g in ins) != "".join(exp.split()):
        raise _fail("VZ")
    moved = sorted(ln["moved"], key=lambda q: float(q["x_new"]))
    q = 0
    for k, o in items:
        if k == "s":
            txt = values[o["sid"]] if o["action"] == "pseudonymize" else REDACTED
            want = "".join(txt.split())
            seg, got = [], ""
            while q < len(ins) and len(got) < len(want):  # glyphs, not characters (a ligature is one glyph)
                seg.append(ins[q])
                got += ins[q].text
                q += 1
            if got != want:
                raise _fail("VZ")
        else:
            seg = ins[q : q + 1]
            q += 1
        if k == "s":
            x_to = float(o["x_orig"]) + e_left(float(o["x_orig"]))
            if o["action"] == "pseudonymize":
                if (
                    not _close(seg[0].ox, x_to, 0.02)
                    or abs(seg[-1].ox + seg[-1].adv - (x_to + float(o["span"]))) > 0.05
                ):
                    raise _fail("VZ")
                if any(not any(_close(g.em, float(r["em"])) for r in o["runs"]) for g in seg):
                    raise _fail("VZ")
            elif ln["kind"] == "Z2":
                if not _close(seg[0].ox, x_to, 0.02):
                    raise _fail("Z2")
                w = seg[-1].ox + seg[-1].adv - seg[0].ox
                nat = sum(m["widths"][g.text] for g in seg) / 1000.0 * math.hypot(seg[0].ux, seg[0].uy) / seg[0].th
                if abs(w / nat - scale[o["sid"]]) > 0.003:
                    raise _fail("Z2")
        elif ln["kind"] == "Z2":
            g = seg[0]
            mv = next((x for x in moved if _close(float(x["x_new"]), o.ox, 0.02)), None)
            if mv is None:
                raise _fail("Z2")
            x_to = float(mv["x_orig"]) + e_left(float(mv["x_orig"]))
            if not _close(g.ox, x_to, 0.02) or not _close(g.th, float(mv["th"]), 1e-6):
                raise _fail("Z2")
            if not (_close(g.oy, o.oy) and _close(g.em, o.em)):
                raise _fail("Z2")
            if e_left(float(mv["x_orig"])) > 0 and g.ox + g.adv > float(ln["r"]) + 0.05:
                raise _fail("Z2")  # the RZ excess never pushes the line beyond its authorized limit


def verify(mode: str, first: bytes, second: bytes, params: dict[str, Any]) -> dict[str, Any]:
    """Child entry point: never raises; returns ``{"ok": True, ...}`` or ``{"ok": False, "failure": code}``."""
    try:
        if mode == "protect":
            return protect(first, second, params)
        if mode == "restore":
            return restore(first, second, params)
        return {"ok": False, "failure": "derivative_verification_failed"}
    except Fail as f:
        return {"ok": False, "failure": f.code, "check": f.check}
    except RecursionError:
        return {"ok": False, "failure": "derivative_verification_failed", "check": "VA"}
