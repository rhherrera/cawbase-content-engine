"""Synthetic PDFs for the A08 controls (pikepdf only; no font program, no experiments import).

Font ``/F1``: a simple TrueType font named ``Helvetica`` with explicit ``/Widths`` (deterministic metrics) and
``WinAnsiEncoding``; no embedded program, so ink falls back to the typographic box (the conservative path). Optional
extras: a ``ToUnicode`` font with a ligature code and a right-to-left code, Form XObjects, images, annotations,
marked content. Values are evidently false (``example.test``).
"""

from __future__ import annotations

import base64
import io
import zlib
from typing import Any

import pikepdf

from cawbase_content_engine.documents import child
from cawbase_content_engine.documents.common import Limits
from cawbase_content_engine.jobs.setup import JobsLimits
from cawbase_content_engine.protection import markers as A06

P = {k: getattr(JobsLimits(), k) for k in Limits.__dataclass_fields__}


def width(ch: str) -> int:
    if ch == " ":
        return 278
    if ch.isupper():
        return 667
    if ch.isdigit():
        return 556
    if ch in ".,:;!|'`":
        return 278
    return 500


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def line(x: float, y: float, text: str, *, size: float = 11, font: str = "/F1", pre: str = "") -> str:
    return f"BT {font} {size} Tf {pre} {x} {y} Td ({esc(text)}) Tj ET"


def hexline(x: float, y: float, codes: bytes, *, size: float = 11, font: str = "/F2") -> str:
    return f"BT {font} {size} Tf {x} {y} Td <{codes.hex()}> Tj ET"


def _f1(pdf: pikepdf.Pdf) -> Any:
    ws = [width(bytes([c]).decode("cp1252", "replace")) for c in range(32, 256)]
    fd = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.FontDescriptor,
            FontName=pikepdf.Name("/Helvetica"),
            Flags=32,
            FontBBox=[-166, -225, 1000, 931],
            ItalicAngle=0,
            Ascent=718,
            Descent=-207,
            CapHeight=718,
            StemV=88,
        )
    )
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.TrueType,
            BaseFont=pikepdf.Name("/Helvetica"),
            FirstChar=32,
            LastChar=255,
            Widths=ws,
            Encoding=pikepdf.Name.WinAnsiEncoding,
            FontDescriptor=fd,
        )
    )


#: /F2: codes 0x41.. map through ToUnicode; 0x01 = the "fi" ligature, 0x02 = Hebrew alef
F2_MAP = {0x01: "fi", 0x02: "א"}


def f2_codes(text: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(text):
        if text.startswith("fi", i):
            out.append(0x01)
            i += 2
        elif text[i] == "א":
            out.append(0x02)
            i += 1
        else:
            out.append(ord(text[i]))
            i += 1
    return bytes(out)


def _f2(pdf: pikepdf.Pdf) -> Any:
    lines = [f"<{c:02X}> <{ord(u) if len(u) == 1 else 0:04X}>" for c, u in F2_MAP.items() if len(u) == 1]
    lines += [f"<{c:02X}> <{u.encode('utf-16-be').hex().upper()}>" for c, u in F2_MAP.items() if len(u) > 1]
    lines += [f"<{c:02X}> <{c:04X}>" for c in range(32, 127)]
    cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CMapName /X def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        f"{len(lines)} beginbfchar\n" + "\n".join(lines) + "\nendbfchar\nendcmap\nend\nend\n"
    )
    ws = [500] * 127
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.TrueType,
            BaseFont=pikepdf.Name("/Helvetica"),
            FirstChar=0,
            LastChar=126,
            Widths=ws,
            FontDescriptor=pikepdf.Dictionary(
                Type=pikepdf.Name.FontDescriptor,
                FontName=pikepdf.Name("/Helvetica"),
                Flags=32,
                Ascent=718,
                Descent=-207,
                FontBBox=[0, -207, 1000, 718],
                ItalicAngle=0,
                CapHeight=718,
                StemV=88,
            ),
            ToUnicode=pdf.make_stream(cmap.encode("ascii")),
        )
    )


def build(
    pages: list[str],
    *,
    info: dict[str, str] | None = None,
    xmp: bool = False,
    annots: list[list[Any]] | None = None,
    xobjects: dict[str, str] | None = None,
    image: bool = False,
    rotate: int = 0,
    contents_array: bool = False,
    objstm: bool = False,
    no_widths: bool = False,
    hook: Any = None,
) -> bytes:
    pdf = pikepdf.new()
    f1 = _f1(pdf)
    if no_widths:
        f1 = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type1,
                BaseFont=pikepdf.Name("/Helvetica"),
                Encoding=pikepdf.Name.WinAnsiEncoding,
            )
        )
    f2 = _f2(pdf)
    for k, content in enumerate(pages):
        res = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=f1, F2=f2))
        if xobjects:
            xd = pikepdf.Dictionary()
            for name, body in xobjects.items():
                xo = pdf.make_stream(body.encode("latin-1"))
                xo.Type, xo.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
                xo.BBox = [0, 0, 595, 842]
                xo.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=f1))
                xd[name] = xo
            res.XObject = xd
        if image:
            img = pdf.make_stream(zlib.compress(bytes(range(256)) * 3), Filter=pikepdf.Name.FlateDecode)
            img.Type, img.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
            img.Width, img.Height, img.ColorSpace, img.BitsPerComponent = 16, 16, pikepdf.Name.DeviceRGB, 8
            res.XObject = pikepdf.Dictionary(Im1=img)
        data = content.encode("latin-1")
        if contents_array:
            mid = data.find(b"ET") + 2
            cont: Any = pikepdf.Array([pdf.make_stream(data[:mid]), pdf.make_stream(data[mid:])])
        else:
            cont = pdf.make_stream(data)
        page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 595, 842], Contents=cont, Resources=res)
        if rotate:
            page.Rotate = rotate
        if annots and k < len(annots) and annots[k]:
            page.Annots = pikepdf.Array([pdf.make_indirect(a) for a in annots[k]])
        pdf.pages.append(pikepdf.Page(page))
    if info:
        pdf.trailer.Info = pdf.make_indirect(pikepdf.Dictionary({"/" + k: pikepdf.String(v) for k, v in info.items()}))
    if xmp:
        pdf.Root.Metadata = pdf.make_stream(
            b"<x:xmpmeta xmlns:x='adobe:ns:meta/'><dc>Titulo sintetico</dc></x:xmpmeta>"
        )
    if hook is not None:
        hook(pdf)
    buf = io.BytesIO()
    mode = pikepdf.ObjectStreamMode.generate if objstm else pikepdf.ObjectStreamMode.disable
    pdf.save(buf, object_stream_mode=mode, compress_streams=True)
    return buf.getvalue()


def link(rect: list[float], uri: str = "https://example.test/doc") -> pikepdf.Dictionary:
    return pikepdf.Dictionary(
        Type=pikepdf.Name.Annot,
        Subtype=pikepdf.Name.Link,
        Rect=rect,
        Border=[0, 0, 0],
        A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String(uri)),
    )


def note(rect: list[float], text: str = "Comentario sintetico") -> pikepdf.Dictionary:
    return pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Text, Rect=rect, Contents=pikepdf.String(text)
    )


# ---------------------------------------------------------------- a pure cycle in the child (no database)
def extract(data: bytes) -> dict[str, Any]:
    return child.process(data, {**P, "pdf_extractor": "v2"})


def selections(data: bytes, picks: list[tuple[int, str, int, str]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """picks: (page index, fragment, occurrence, action) -> A06-like groups with fresh markers; and token -> value."""
    units = extract(data)["units"]
    sels, values, tokens = [], {}, {}
    for page, frag, occ, action in picks:
        text = units[page]["text"]
        pos = -1
        for _ in range(occ + 1):
            pos = text.index(frag, pos + 1)
        if action == "pseudonymize":
            tok = tokens.setdefault(frag, A06.new_token())
            sub = A06.marker_of(tok)
            values[tok] = frag
        else:
            sub = A06.REDACTED
        sels.append(
            {
                "unit_id": units[page]["unit_id"],
                "start": pos,
                "end": pos + len(frag),
                "action": action,
                "substitute": sub,
                "value": frag,
            }
        )
    sels.sort(key=lambda s: (s["unit_id"], s["start"]))
    return sels, values


def protect(data: bytes, sels: list[dict[str, Any]]) -> dict[str, Any]:
    plain = [{k: s[k] for k in ("unit_id", "start", "end", "action", "substitute")} for s in sels]
    return child.process(data, {**P, "pdf_extractor": "v2", "mode": "pdf_protect", "a08": {"selections": plain}})


def verify_protect(original: bytes, derivative: bytes, sels: list[dict[str, Any]], layout: list[Any]) -> dict[str, Any]:
    units = [u["text"] for u in extract(original)["units"]]
    packed = len(original).to_bytes(8, "big") + original + derivative
    return child.process(
        packed,
        {**P, "mode": "pdf_verify", "a08": {"mode": "protect", "units": units, "selections": sels, "layout": layout}},
    )


def values_by_sid(layout: list[Any], values: dict[str, str]) -> dict[str, str]:
    return {
        s["sid"]: values[A06.token_of(s["substitute"])]
        for ln in layout
        for s in ln["sels"]
        if s["action"] == "pseudonymize"
    }


def restore(derivative: bytes, layout: list[Any], vals: dict[str, str]) -> dict[str, Any]:
    return child.process(
        derivative, {**P, "pdf_extractor": "v2", "mode": "pdf_restore", "a08": {"layout": layout, "values": vals}}
    )


def verify_restore(derivative: bytes, restored: bytes, layout: list[Any], vals: dict[str, str]) -> dict[str, Any]:
    packed = len(derivative).to_bytes(8, "big") + derivative + restored
    return child.process(
        packed, {**P, "mode": "pdf_verify", "a08": {"mode": "restore", "layout": layout, "values": vals}}
    )


def cycle(data: bytes, picks: list[tuple[int, str, int, str]]) -> dict[str, Any]:
    """protect -> verify -> restore -> verify; returns every intermediate (the first failure stops it)."""
    sels, values = selections(data, picks)
    out: dict[str, Any] = {"sels": sels, "values": values}
    pr = protect(data, sels)
    out["protect"] = pr
    if "derivative" not in pr:
        out["stage"], out["reason"] = "protect", pr.get("failure") or pr.get("rejected")
        return out
    der = base64.b64decode(pr["derivative"])
    out["derivative"] = der
    v = verify_protect(data, der, sels, pr["layout"])
    out["verify"] = v
    if not v.get("ok"):
        out["stage"], out["reason"] = "verify", v.get("failure")
        return out
    vals = values_by_sid(pr["layout"], values)
    out["vals"] = vals
    rr = restore(der, pr["layout"], vals)
    if "restored" not in rr:
        out["stage"], out["reason"] = "restore", rr.get("failure") or rr.get("rejected")
        return out
    rest = base64.b64decode(rr["restored"])
    out["restored"] = rest
    v2 = verify_restore(der, rest, pr["layout"], vals)
    out["verify_restore"] = v2
    if not v2.get("ok"):
        out["stage"], out["reason"] = "verify_restore", v2.get("failure")
        return out
    out["stage"], out["reason"] = "cycle", None
    return out


def edit_page(data: bytes, page: int, fn: Any) -> bytes:
    """Rewrites the operator list of one page's (single) content stream with ``fn(ops) -> ops``."""
    pdf = pikepdf.open(io.BytesIO(data))
    st = pdf.pages[page].obj.Contents
    ops = list(pikepdf.parse_content_stream(st))
    st.write(pikepdf.unparse_content_stream(fn(ops)))
    buf = io.BytesIO()
    pdf.save(buf, object_stream_mode=pikepdf.ObjectStreamMode.disable, compress_streams=True)
    return buf.getvalue()


def edit_pdf(data: bytes, fn: Any) -> bytes:
    pdf = pikepdf.open(io.BytesIO(data))
    fn(pdf)
    buf = io.BytesIO()
    pdf.save(buf, object_stream_mode=pikepdf.ObjectStreamMode.disable, compress_streams=True)
    return buf.getvalue()
