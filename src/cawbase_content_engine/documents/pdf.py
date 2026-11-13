"""PDF validation + extraction of the conservative A05 subset (docs 11.2): simple fonts, one page = one unit."""

from __future__ import annotations

import io
import re
from typing import Any

from .common import Limits, Reject, Unit, inflate_limited

_POSITIONING = {"Td", "TD", "Tm", "T*", "BT"}
_UNSUPPORTED_STATE = {"Tc", "Tw", "Tz", "Ts", "TL"}
_STD_ENC = {"/WinAnsiEncoding": "cp1252", "/MacRomanEncoding": "mac_roman", "/StandardEncoding": "cp1252"}
_STD14 = re.compile(r"^/(?:[A-Z]{6}\+)?(Helvetica|Times|Courier|Symbol|ZapfDingbats)")


def _stream_bytes(obj: Any, limit: int) -> bytes:
    import pikepdf

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


def _cmap(data: bytes) -> dict[int, str]:
    out: dict[int, str] = {}
    text = data.decode("latin-1")

    def u(hexstr: str) -> str:
        b = bytes.fromhex(hexstr)
        return b.decode("utf-16-be", "replace") if len(b) >= 2 else b.decode("latin-1")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            out[int(src, 16)] = u(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            a, b = int(lo, 16), int(hi, 16)
            if b - a > 65535:
                raise Reject("font_mapping_insufficient")
            if rest.startswith("["):
                for i, d in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", rest)):
                    out[a + i] = u(d)
            else:
                base = u(rest[1:-1])
                for i in range(b - a + 1):
                    out[a + i] = base[:-1] + chr(ord(base[-1]) + i) if base else ""
    return out


class _Font:
    def __init__(self, font: Any, lim: Limits) -> None:
        sub = str(font.get("/Subtype", ""))
        if sub not in ("/Type1", "/TrueType", "/MMType1"):
            raise Reject("font_mapping_insufficient")  # Type0/CID, Type3 and the rest are outside the A05 subset
        self.map: dict[int, str] | None = None
        self.codec: str | None = None
        tu = font.get("/ToUnicode")
        if tu is not None:
            self.map = _cmap(_stream_bytes(tu, lim.max_pdf_content_bytes_per_page))
            return
        enc = font.get("/Encoding")
        if enc is None:
            if _STD14.match(str(font.get("/BaseFont", ""))):
                self.codec = "cp1252"
                return
            raise Reject("font_mapping_insufficient")
        import pikepdf

        is_dict = isinstance(enc, pikepdf.Dictionary)
        if is_dict and "/Differences" in enc:
            raise Reject("font_mapping_insufficient")
        name = str(enc.get("/BaseEncoding", "")) if is_dict else str(enc)
        if name not in _STD_ENC:
            raise Reject("font_mapping_insufficient")
        self.codec = _STD_ENC[name]

    def decode(self, raw: bytes) -> list[tuple[str, str]]:
        """-> [(text, relation)] per code."""
        out: list[tuple[str, str]] = []
        for code in raw:
            if self.map is not None:
                if code not in self.map:
                    raise Reject("font_mapping_insufficient")
                s = self.map[code]
            else:
                assert self.codec
                try:
                    s = bytes([code]).decode(self.codec)
                except UnicodeDecodeError:
                    raise Reject("font_mapping_insufficient") from None
            out.append((s, "1:1" if len(s) == 1 else "n:1"))
        return out


def _inherited(node: Any, key: str) -> Any:
    seen = 0
    while node is not None and seen < 64:
        if key in node:
            return node[key]
        node = node.get("/Parent")
        seen += 1
    return None


def _axis_ok(m: list[float]) -> None:
    a, b, c, d = m[0], m[1], m[2], m[3]
    eps = 1e-6
    scale = (abs(a) + abs(b) + abs(c) + abs(d)) or 1.0
    if abs(b) < eps * scale and abs(c) < eps * scale and (a * d) > 0:
        return  # 0 or 180 degrees (scale may be negative on both axes)
    if abs(a) < eps * scale and abs(d) < eps * scale and (b * c) < 0 and b > 0:
        return  # 90 degrees
    raise Reject("unsupported_structure")  # 270, oblique, mirrored


def extract(data: bytes, lim: Limits) -> dict[str, Any]:
    import pikepdf

    if data.count(b" obj") + data.count(b"\nobj") > lim.max_pdf_objects:
        raise Reject("structure_limit")
    if b"/ObjStm" in data:
        raise Reject("unsupported_structure")
    if data.count(b"%%EOF") > 1 or b"/Prev" in data:
        raise Reject("incremental_update")
    if b"/ByteRange" in data:
        raise Reject("signed")
    for token in (
        b"/JavaScript",
        b"/JS ",
        b"/JS(",
        b"/Launch",
        b"/OpenAction",
        b"/SubmitForm",
        b"/ImportData",
        b"/GoToR",
        b"/Rendition",
    ):
        if token in data:
            raise Reject("executable_action")
    if b"/EmbeddedFiles" in data or b"/FileAttachment" in data:
        raise Reject("embedded_content")
    try:
        pdf = pikepdf.open(io.BytesIO(data), attempt_recovery=False)
    except pikepdf.PasswordError:
        raise Reject("encrypted") from None
    except pikepdf.PdfError:
        raise Reject("parser_repair") from None
    with pdf:
        if pdf.is_encrypted:
            raise Reject("encrypted")
        if pdf.is_linearized:
            raise Reject("unsupported_structure")
        if pdf.get_warnings():
            raise Reject("parser_repair")
        root = pdf.Root
        for k in ("/AcroForm", "/OCProperties", "/XFA"):
            if k in root:
                raise Reject("forms_or_layers")
        if "/StructTreeRoot" in root:
            for obj in pdf.objects:
                if isinstance(obj, pikepdf.Dictionary) and ("/Alt" in obj or "/ActualText" in obj):
                    raise Reject("alternate_text_duplication")
        pages = pdf.pages
        if len(pages) > lim.max_pdf_pages:
            raise Reject("structure_limit")
        nc: dict[str, int] = {}
        reasons: dict[str, str] = {}

        def note(cls: str, n: int = 1, reason: str = "no_extraction") -> None:
            if n:
                nc[cls] = nc.get(cls, 0) + n
                reasons[cls] = reason

        info = pdf.trailer.get("/Info")
        if info is not None:
            note(
                "document_properties",
                sum(1 for k in info.keys() if str(info[k]).strip() not in ("", "[]")),
                "not_analyzed",
            )
        if "/Metadata" in root:
            note("document_properties", 1, "not_analyzed")
        units: list[Unit] = []
        any_image_only = False
        for pno, page in enumerate(pages):
            obj = page.obj
            rot = _inherited(obj, "/Rotate")
            if rot is not None and int(rot) not in (0, 90, 180):
                raise Reject("unsupported_structure")
            contents = obj.get("/Contents")
            if contents is not None and isinstance(contents, pikepdf.Array):
                raise Reject("unsupported_structure")
            for annot in obj.get("/Annots", []):
                sub = str(annot.get("/Subtype", ""))
                if sub == "/Link":
                    note("link_annotations")
                    act = annot.get("/A")
                    if act is not None and str(act.get("/S", "")) not in ("/URI", "/GoTo"):
                        raise Reject("executable_action")
                elif sub in ("/Text", "/Popup"):
                    note("comments", 1, "not_analyzed")
                else:
                    raise Reject("unsupported_structure")
            u = Unit(unit_id=f"u{pno + 1:04d}", kind="pdf_page", ordinal=pno + 1)
            state = {"ops": 0, "images": 0, "last_show": False, "sep": False}
            res = _inherited(obj, "/Resources")
            if contents is not None:
                _run(pikepdf, pdf, contents, res, u, pno, state, lim, depth=0, ref=obj.objgen)
            if state["images"]:
                note("images", state["images"])
            u_text_empty = not u.text.strip("\n")
            if u_text_empty and state["images"]:
                any_image_only = True
            units.append(u)
        if units and all(not u.text.strip("\n") for u in units) and any_image_only:
            raise Reject("scanned_without_text")
        declared = [{"element_class": k, "count": v, "reason": reasons[k]} for k, v in sorted(nc.items())]
        return {"format": "pdf", "units": [x.finish() for x in units], "declared_not_covered": declared}


def _run(
    pikepdf: Any,
    pdf: Any,
    stream: Any,
    res: Any,
    u: Unit,
    pno: int,
    state: dict[str, Any],
    lim: Limits,
    *,
    depth: int,
    ref: Any,
) -> None:
    if depth > lim.max_form_xobject_depth:
        raise Reject("structure_limit")
    raw = _stream_bytes(stream, lim.max_pdf_content_bytes_per_page)
    fonts = res.get("/Font") if res is not None else None
    cache: dict[str, _Font] = {}
    cur: _Font | None = None
    xobjs = res.get("/XObject") if res is not None else None
    for op_index, (operands, operator) in enumerate(pikepdf.parse_content_stream(pikepdf.Stream(pdf, raw))):
        op = str(operator)
        state["ops"] += 1
        if state["ops"] > lim.max_text_operators_per_page:
            raise Reject("structure_limit")
        if op in _UNSUPPORTED_STATE:
            raise Reject("unsupported_text_state")
        if op == "Tr":
            mode = int(operands[0])
            if mode == 3:
                raise Reject("hidden_text")
            if mode != 0:
                raise Reject("unsupported_text_state")
        elif op == "Tm":
            _axis_ok([float(x) for x in operands])
        elif op == "cm":
            m = [float(x) for x in operands]
            if abs(m[1]) > 1e-9 or abs(m[2]) > 1e-9 or m[0] <= 0 or m[3] <= 0:
                raise Reject("unsupported_structure")
        elif op in _POSITIONING:
            state["sep"] = True
        elif op == "Tf":
            name = str(operands[0])
            if fonts is None or name not in fonts:
                raise Reject("font_mapping_insufficient")
            if name not in cache:
                cache[name] = _Font(fonts[name], lim)
            cur = cache[name]
        elif op in ("Tj", "TJ", "'", '"'):
            if cur is None:
                raise Reject("font_mapping_insufficient")
            if op in ("'", '"'):
                state["sep"] = True
            strings: list[tuple[int, Any]]
            if op == "TJ":
                strings = [(i, s) for i, s in enumerate(operands[0]) if isinstance(s, pikepdf.String)]
            else:
                strings = [(0, operands[-1])]
            if state["last_show"] and state["sep"]:
                u.add("\n", kind="synthesized")
            state["sep"] = False
            state["last_show"] = True
            for idx, s in strings:
                for pos, (txt, rel) in enumerate(cur.decode(bytes(s))):
                    u.add(
                        txt,
                        pieces=[
                            {
                                "page_index": pno,
                                "object_ref": f"{ref[0]} {ref[1]}",
                                "operator_index": op_index,
                                "operand_index": idx,
                                "char_start": pos,
                                "char_end": pos + 1,
                                "relation": rel,
                            }
                        ],
                    )
        elif op == "Do":
            name = str(operands[0])
            xo = xobjs.get(name) if xobjs is not None else None
            if xo is None:
                raise Reject("unsupported_structure")
            sub = str(xo.get("/Subtype", ""))
            if sub == "/Image":
                state["images"] += 1
            elif sub == "/Form":
                mx = xo.get("/Matrix")
                if mx is not None and [float(v) for v in mx] not in ([1.0, 0.0, 0.0, 1.0, 0.0, 0.0],):
                    _axis_ok([float(v) for v in mx])
                state["sep"] = True
                _run(pikepdf, pdf, xo, xo.get("/Resources") or res, u, pno, state, lim, depth=depth + 1, ref=xo.objgen)
                state["sep"] = True
            else:
                raise Reject("unsupported_structure")
        elif op == "BI":
            state["images"] += 1
