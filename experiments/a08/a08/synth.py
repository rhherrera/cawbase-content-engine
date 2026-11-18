"""Synthetic PDF builder for the controls (protocol §8, §0.7, §0.8). Fonts: Noto Sans (OFL-1.1) subsets."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
from fontTools import subset as ftsubset
from fontTools.ttLib import TTFont

FONTS = Path(__file__).resolve().parent.parent / "fonts"
A4 = (595.28, 841.89)


def _subset(ttf: Path, text: str, retain_gids: bool) -> tuple[bytes, TTFont]:
    opts = ftsubset.Options()
    opts.retain_gids = retain_gids
    opts.notdef_outline = True
    opts.layout_features = []
    opts.name_IDs = ["*"]
    f = TTFont(str(ttf))
    sub = ftsubset.Subsetter(opts)
    sub.populate(text=text)
    sub.subset(f)
    b = io.BytesIO()
    f.save(b)
    return b.getvalue(), TTFont(io.BytesIO(b.getvalue()))


class Font:
    def __init__(self, kind: str, enc) -> None:
        self.kind, self.enc = kind, enc

    def codes(self, s: str) -> bytes:
        return self.enc(s)


class Builder:
    def __init__(self, pages: int = 1, rotate: int = 0) -> None:
        self.pdf = pikepdf.new()
        self.pages = []
        for _ in range(pages):
            p = self.pdf.add_blank_page(page_size=A4)
            if rotate:
                p.obj.Rotate = rotate
            p.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(), XObject=pikepdf.Dictionary())
            self.pages.append(p)
        self.content = [[] for _ in range(pages)]
        self.fonts: dict[str, Font] = {}

    def _res(self, k: int = 0):
        return self.pages[k].obj.Resources

    def font_simple(self, name: str, text: str, ttf: str = "NotoSans-Regular.ttf", tounicode: bool = True) -> Font:
        chars = "".join(sorted(set(text + " ")))
        data, tt = _subset(FONTS / ttf, chars, retain_gids=False)
        cmap, hm, upm = tt.getBestCmap(), tt["hmtx"].metrics, tt["head"].unitsPerEm
        widths = []
        for code in range(32, 256):
            try:
                ch = bytes([code]).decode("cp1252")
            except UnicodeDecodeError:
                widths.append(0)
                continue
            g = cmap.get(ord(ch))
            widths.append(round(hm[g][0] / upm * 1000) if g else 0)
        ff = self.pdf.make_stream(data)
        ff.Length1 = len(data)
        fd = self.pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name("/AAAAAA+" + ttf[:-4]),
                                    Flags=32, FontBBox=[-600, -300, 1500, 1100], ItalicAngle=0, Ascent=1069, Descent=-293, CapHeight=714, StemV=80, FontFile2=ff))
        d = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType, BaseFont=pikepdf.Name("/AAAAAA+" + ttf[:-4]),
                               FirstChar=32, LastChar=255, Widths=widths, Encoding=pikepdf.Name.WinAnsiEncoding, FontDescriptor=fd)
        if tounicode:
            lines = "".join(f"<{c.encode('cp1252')[0]:02X}> <{ord(c):04X}>\n" for c in chars)
            d.ToUnicode = self.pdf.make_stream(_cmap(1, len(chars), lines))
        ref = self.pdf.make_indirect(d)
        for p in self.pages:
            p.obj.Resources.Font[name] = ref
        f = Font("simple", lambda s: s.encode("cp1252"))
        self.fonts[name] = f
        return f

    def font_type0(self, name: str, text: str, ttf: str = "NotoSans-Regular.ttf", tounicode: bool = True, encoding: str = "/Identity-H") -> Font:
        chars = "".join(sorted(set(text + " ")))
        data, tt = _subset(FONTS / ttf, chars, retain_gids=True)
        full = TTFont(str(FONTS / ttf))
        cmap = full.getBestCmap()
        order = full.getGlyphOrder()
        hm, upm = full["hmtx"].metrics, full["head"].unitsPerEm
        gid = {c: order.index(cmap[ord(c)]) for c in chars}
        W = []
        for c in sorted(gid, key=lambda q: gid[q]):
            W += [gid[c], [round(hm[cmap[ord(c)]][0] / upm * 1000)]]
        ff = self.pdf.make_stream(data)
        fd = self.pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name("/BAAAAA+" + ttf[:-4]),
                                    Flags=32, FontBBox=[-600, -300, 1500, 1100], ItalicAngle=0, Ascent=1069, Descent=-293, CapHeight=714, StemV=80, FontFile2=ff))
        desc = self.pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.CIDFontType2, BaseFont=pikepdf.Name("/BAAAAA+" + ttf[:-4]),
                                      CIDSystemInfo=pikepdf.Dictionary(Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0),
                                      FontDescriptor=fd, DW=1000, W=W, CIDToGIDMap=pikepdf.Name.Identity))
        d = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type0, BaseFont=pikepdf.Name("/BAAAAA+" + ttf[:-4]),
                               Encoding=pikepdf.Name(encoding), DescendantFonts=[desc])
        if tounicode:
            lines = "".join(f"<{gid[c]:04X}> <{ord(c):04X}>\n" for c in chars)
            d.ToUnicode = self.pdf.make_stream(_cmap(2, len(chars), lines))
        ref = self.pdf.make_indirect(d)
        for p in self.pages:
            p.obj.Resources.Font[name] = ref
        f = Font("type0", lambda s: b"".join(gid[c].to_bytes(2, "big") for c in s))
        self.fonts[name] = f
        return f

    def text(self, font: str, size: float, x: float, y: float, s: str, page: int = 0, pre: str = "", tm: str | None = None) -> None:
        pos = f"{tm} Tm" if tm else f"{x} {y} Td"
        self.content[page].append(f"BT {pre} {font} {size} Tf {pos} <{self.fonts[font].codes(s).hex()}> Tj ET")

    def raw(self, s: str, page: int = 0) -> None:
        self.content[page].append(s)

    def save(self, contents_split: list[int] | None = None, **kw) -> bytes:
        for k, p in enumerate(self.pages):
            body = "\n".join(self.content[k]).encode()
            if contents_split and k == 0:
                parts, prev = [], 0
                for cut in contents_split:
                    parts.append(self.pdf.make_stream(body[prev:cut]))
                    prev = cut
                parts.append(self.pdf.make_stream(body[prev:]))
                p.obj.Contents = pikepdf.Array(parts)
            else:
                p.obj.Contents = self.pdf.make_stream(body)
        b = io.BytesIO()
        self.pdf.save(b, **kw)
        return b.getvalue()


def _cmap(nbytes: int, n: int, lines: str) -> bytes:
    lo, hi = ("00", "FF") if nbytes == 1 else ("0000", "FFFF")
    return ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            f"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n1 begincodespacerange\n<{lo}> <{hi}>\nendcodespacerange\n"
            f"{n} beginbfchar\n{lines}endbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n").encode()
