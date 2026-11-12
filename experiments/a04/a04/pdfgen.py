"""Raw PDF writer used ONLY to generate synthetic fixtures + independent manifests (not used by the transformer)."""

from __future__ import annotations

import re
import zlib

HELV = {  # Helvetica advance widths (1/1000 em) for ASCII 32..126 (Adobe Core 14 AFM values)
    " ": 278,
    "!": 278,
    '"': 355,
    "#": 556,
    "$": 556,
    "%": 889,
    "&": 667,
    "'": 191,
    "(": 333,
    ")": 333,
    "*": 389,
    "+": 584,
    ",": 278,
    "-": 333,
    ".": 278,
    "/": 278,
    ":": 278,
    ";": 278,
    "<": 584,
    "=": 584,
    ">": 584,
    "?": 556,
    "@": 1015,
    "[": 278,
    "\\": 278,
    "]": 278,
    "^": 469,
    "_": 556,
    "`": 333,
    "{": 334,
    "|": 260,
    "}": 334,
    "~": 584,
}
for c, w in zip("0123456789", [556] * 10):
    HELV[c] = w
UP = dict(
    zip(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        [
            667,
            667,
            722,
            722,
            667,
            611,
            778,
            722,
            278,
            500,
            667,
            556,
            833,
            722,
            778,
            667,
            778,
            722,
            667,
            611,
            722,
            667,
            944,
            667,
            667,
            611,
        ],
    )
)
LO = dict(
    zip(
        "abcdefghijklmnopqrstuvwxyz",
        [
            556,
            556,
            500,
            556,
            556,
            278,
            556,
            556,
            222,
            222,
            500,
            222,
            833,
            556,
            556,
            556,
            556,
            333,
            500,
            278,
            556,
            500,
            722,
            500,
            500,
            500,
        ],
    )
)
HELV.update(UP)
HELV.update(LO)
CP1252_EXTRA = {"é": 556, "ë": 556, "ñ": 556, "á": 556, "ó": 556, "ú": 556, "í": 278, "Ł": None}


def width_table_winansi() -> list[int]:
    """Widths for codes 32..255 using cp1252 decoding; unknown accented lowercase = 556."""
    ws = []
    for code in range(32, 256):
        ch = bytes([code]).decode("cp1252", errors="replace")
        ws.append(HELV.get(ch, CP1252_EXTRA.get(ch) or 556))
    return ws


TOUNI = (
    "/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def "
    "/CMapName /Adobe-Identity-UCS def /CMapType 2 def 1 begincodespacerange <00> <FF> endcodespacerange "
    "4 beginbfchar <01> <00660069> <02> <0066006C> <03> <D83DDE00> <04> <0301> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end"
)


def esc(b: bytes) -> bytes:
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class Pdf:
    def __init__(self, compress: bool = True):
        self.objs: list[bytes | None] = [None]
        self.compress = compress
        self.pages: list[int] = []
        self.units: list[str] = []
        self.occs: list[dict] = []
        self.extra_catalog = b""
        self.info: bytes | None = None

    def add(self, body: bytes | None = None) -> int:
        self.objs.append(body)
        return len(self.objs) - 1

    def set(self, n: int, body: bytes) -> None:
        self.objs[n] = body

    def stream(self, data: bytes, extra: bytes = b"", *, compress: bool | None = None) -> int:
        c = self.compress if compress is None else compress
        if c:
            data, filt = zlib.compress(data), b"/Filter /FlateDecode "
        else:
            filt = b""
        return self.add(b"<< " + extra + filt + b"/Length %d >>\nstream\n" % len(data) + data + b"\nendstream")

    def fonts(self) -> dict[str, int]:
        ws = width_table_winansi()
        wa = b"[" + b" ".join(str(w).encode() for w in ws) + b"]"
        f1 = self.add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding /FirstChar 32 /LastChar 255 /Widths "
            + wa
            + b" >>"
        )
        tu = self.stream(TOUNI.encode(), compress=False)
        # F2: custom encoding (Differences) with ligature fi/fl, bullet (mapped to a non-BMP code point) and acute (mapped to U+0301)
        w2 = list(ws)
        w2 = [500, 500, 350, 333] + w2[4:]  # codes 1..4 have their own widths; we write FirstChar 1
        wa2 = b"[" + b" ".join(str(w).encode() for w in ([500, 500, 350, 333] + [250] * 27 + ws)) + b"]"
        f2 = self.add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding << /Type /Encoding /BaseEncoding /WinAnsiEncoding "
            b"/Differences [1 /fi /fl /bullet /acute] >> /FirstChar 1 /LastChar 255 /Widths "
            + wa2
            + b" /ToUnicode %d 0 R >>" % tu
        )
        return {"F1": f1, "F2": f2}

    def add_page(
        self,
        content: bytes,
        fonts: dict[str, int],
        *,
        rotate: int = 0,
        xobjects: dict[str, int] | None = None,
        mediabox=(0, 0, 400, 300),
    ) -> int:
        cs = self.stream(content)
        res = b"<< /Font << " + b" ".join(f"/{k} {v} 0 R".encode() for k, v in fonts.items()) + b" >>"
        if xobjects:
            res += b" /XObject << " + b" ".join(f"/{k} {v} 0 R".encode() for k, v in xobjects.items()) + b" >>"
        res += b" >>"
        page = self.add(
            b"<< /Type /Page /Parent PAGES 0 R /MediaBox [%d %d %d %d] /Rotate %d /Resources " % (*mediabox, rotate)
            + res
            + b" /Contents %d 0 R >>" % cs
        )
        self.pages.append(page)
        return page

    def build(self, *, header: bytes = b"%PDF-1.7\n") -> bytes:
        pages = self.add()
        cat = self.add(b"<< /Type /Catalog /Pages %d 0 R " % pages + self.extra_catalog + b">>")
        self.set(
            pages,
            b"<< /Type /Pages /Kids ["
            + b" ".join(b"%d 0 R" % p for p in self.pages)
            + b"] /Count %d >>" % len(self.pages),
        )
        for p in self.pages:
            self.objs[p] = self.objs[p].replace(b"PAGES 0 R", b"%d 0 R" % pages)
        info = b""
        if self.info is not None:
            n = self.add(self.info)
            info = b"/Info %d 0 R " % n
        out = bytearray(header + b"%\xe2\xe3\xcf\xd3\n")
        offs = [0]
        for i in range(1, len(self.objs)):
            offs.append(len(out))
            out += b"%d 0 obj\n" % i + self.objs[i] + b"\nendobj\n"
        x = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % len(self.objs)
        for o in offs[1:]:
            out += b"%010d 00000 n \n" % o
        out += b"trailer\n<< /Size %d /Root %d 0 R %s>>\nstartxref\n%d\n%%%%EOF\n" % (len(self.objs), cat, info, x)
        return bytes(out)


def fmt_num(v: float) -> str:
    return ("%.3f" % v).rstrip("0").rstrip(".")


def text_obj(font: str, size: float, x: float, y: float, items: list, rot: int = 0) -> bytes:
    """One BT..ET text object. items: list of str (-> Tj) or list (-> TJ with str/number). Rotation by 0/90/180/270."""
    a, b, c, d = {0: (1, 0, 0, 1), 90: (0, 1, -1, 0), 180: (-1, 0, 0, -1), 270: (0, -1, 1, 0)}[rot]
    ops = [f"BT /{font} {fmt_num(size)} Tf {a} {b} {c} {d} {fmt_num(x)} {fmt_num(y)} Tm"]
    out = ops[0].encode() + b" "
    for it in items:
        if isinstance(it, str):
            out += b"(" + esc(it.encode("cp1252", errors="strict")) + b") Tj "
        elif isinstance(it, bytes):
            out += b"(" + esc(it) + b") Tj "
        else:
            out += (
                b"["
                + b" ".join(
                    b"(" + esc(e.encode("cp1252") if isinstance(e, str) else e) + b")"
                    if isinstance(e, (str, bytes))
                    else fmt_num(e).encode()
                    for e in it
                )
                + b"] TJ "
            )
    return out + b"ET\n"


MARK = re.compile(r"<<([A-Za-z0-9_.]+)\|(.*?)>>", re.S)


def strip_marks(items: list) -> tuple[str, list, list[dict]]:
    """Return clean shown text, clean items, and occurrences [start,end) relative to the shown text (markers may span items)."""
    flat = []
    for it in items:
        if isinstance(it, str):
            flat.append(it)
        elif isinstance(it, bytes):
            flat.append(it.decode("cp1252"))
        else:
            flat += [e for e in it if isinstance(e, str)]
    joined = "".join(flat)
    occs, clean, idx = [], [], 0
    for m in MARK.finditer(joined):
        clean.append(joined[idx : m.start()])
        start = sum(len(c) for c in clean)
        clean.append(m.group(2))
        occs.append(
            {
                "occ": m.group(1),
                "value_id": m.group(1).split(".")[0],
                "start": start,
                "end": start + len(m.group(2)),
                "text": m.group(2),
            }
        )
        idx = m.end()
    clean.append(joined[idx:])
    # strip markers per item preserving item boundaries
    open_ = False

    def clean_str(s: str) -> str:
        nonlocal open_
        res, i = "", 0
        while i < len(s):
            m = re.match(r"<<[A-Za-z0-9_.]+\|", s[i:])
            if m:
                open_ = True
                i += m.end()
            elif s.startswith(">>", i) and open_:
                open_ = False
                i += 2
            else:
                res += s[i]
                i += 1
        return res

    new_items = []
    for it in items:
        if isinstance(it, str):
            new_items.append(clean_str(it))
        elif isinstance(it, bytes):
            new_items.append(clean_str(it.decode("cp1252")))
        else:
            new_items.append([clean_str(e) if isinstance(e, str) else e for e in it])
    return "".join(clean), new_items, occs
