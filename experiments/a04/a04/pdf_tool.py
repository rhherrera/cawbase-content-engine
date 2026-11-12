"""Bounded PDF tooling for the A04 experiments (pikepdf/QPDF): inventory, extraction, mapping, content-stream rewrite.

Experiment code, not a PDF engine: it supports a deliberately narrow subset (simple fonts with a reliable code->Unicode map,
operators Tj/TJ/'/", no horizontal-scaling/spacing state) and REJECTS everything else.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Any

import pikepdf
from fontTools.agl import toUnicode as agl_to_unicode

from a04.common import LIMITS, Reject, bounded_inflate, check_unit_limits, sha256_bytes

EXTRACTOR = {
    "id": "a04-pdf-experiment",
    "version": "1",
    "config": {"unit": "page", "order": "content-stream order, Form XObjects expanded at Do"},
}
SHOW = {"Tj", "TJ", "'", '"'}
POSITION = {"Td", "TD", "Tm", "T*", "BT", "'", '"'}
PAINT = {"f", "F", "f*", "S", "s", "B", "B*", "b", "b*", "sh"}
BAD_TEXT_STATE = {"Tc", "Tw", "Tz", "Ts", "TL"}
CATALOG_OK = {"Type", "Pages", "Metadata", "Version", "Lang", "PageLayout", "PageMode", "ViewerPreferences"}


class _Warn(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


@dataclass
class FontInfo:
    key: str
    first: int
    widths: list[float]
    code2uni: dict[int, str]
    uni2code: dict[str, int]
    two_byte: bool = False
    available: set[str] | None = (
        None  # characters with a glyph in the embedded font program (None = not embedded: unknown)
    )

    def width(self, code: int) -> float:
        i = code - self.first
        if 0 <= i < len(self.widths):
            return self.widths[i]
        raise Reject("font_mapping_insufficient", "no width")


@dataclass
class Seg:
    u_start: int
    u_end: int
    kind: str  # original | synthesized
    piece: tuple | None = None  # (container, instr_idx, elem_idx, byte_off, byte_len)
    font: FontInfo | None = None
    relation: str = "1:1"
    width: float = 0.0
    code: int = 0


@dataclass
class PUnit:
    page: int
    text: str
    segs: list[Seg]
    scope_note: str = "page unit: local scope, not a document-level classification"


def parse_tounicode(data: bytes) -> dict[int, str]:
    out: dict[int, str] = {}
    txt = data.decode("latin-1")
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
        for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            out[int(a, 16)] = bytes.fromhex(b).decode("utf-16-be")
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
        for a, b, c in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            base = bytes.fromhex(c).decode("utf-16-be")
            for k in range(int(a, 16), int(b, 16) + 1):
                out[k] = base[:-1] + chr(ord(base[-1]) + k - int(a, 16))
    return out


def load_font(fobj: pikepdf.Object, key: str) -> FontInfo:
    st = str(fobj.get("/Subtype"))
    if st not in ("/Type1", "/TrueType"):
        raise Reject("font_unsupported", st)  # Type0/Type3/CID are outside the experiment subset
    enc = fobj.get("/Encoding")
    code2uni: dict[int, str] = {}
    if "/ToUnicode" in fobj:
        code2uni.update(parse_tounicode(fobj.ToUnicode.read_bytes()))
    base = None
    if enc is not None:
        if isinstance(enc, pikepdf.Name):
            base = str(enc)
        else:
            base = str(enc.get("/BaseEncoding", "")) or None
    if base == "/WinAnsiEncoding":
        for c in range(32, 256):
            ch = bytes([c]).decode("cp1252", errors="replace")
            if ch != "�":
                code2uni.setdefault(c, ch)
    elif "/ToUnicode" not in fobj:
        raise Reject("font_mapping_insufficient", f"{key}: no ToUnicode and no supported base encoding")
    if enc is not None and not isinstance(enc, pikepdf.Name) and "/Differences" in enc:
        code = 0
        for it in enc.Differences:
            if (
                isinstance(it, int)
                or str(type(it).__name__) == "Object"
                and it._type_code == pikepdf.ObjectType.integer
            ):
                code = int(it)
            else:
                if "/ToUnicode" not in fobj:
                    u = agl_to_unicode(str(it)[1:])
                    if not u:
                        raise Reject("font_mapping_insufficient", "unknown glyph name")
                    code2uni[code] = u
                code += 1
    first = int(fobj.get("/FirstChar", 0))
    widths = [float(w) for w in fobj.get("/Widths", [])]
    if not widths:
        raise Reject("font_mapping_insufficient", f"{key}: no /Widths (no width model)")
    uni2code = {}
    for c, u in sorted(code2uni.items()):
        uni2code.setdefault(u, c)
    available = None
    desc = fobj.get("/FontDescriptor")
    if desc is not None and "/FontFile2" in desc:
        from fontTools.ttLib import TTFont

        tt = TTFont(io.BytesIO(desc.FontFile2.read_bytes()), lazy=True)
        cm = tt.getBestCmap() or {}
        available = {chr(c) for c in cm}
    return FontInfo(key, first, widths, code2uni, uni2code, False, available)


class PdfDoc:
    def __init__(self, data: bytes):
        self.data = data
        self.sha256 = sha256_bytes(data)
        if len(data) > LIMITS["max_file_bytes"]:
            raise Reject("limit_exceeded", "file size")
        if data.count(b"%%EOF") > 1:
            raise Reject("incremental_update", "multiple %%EOF")
        try:
            self.pdf = pikepdf.open(io.BytesIO(data))
        except pikepdf.PasswordError as exc:
            raise Reject("encrypted") from exc
        except pikepdf.PdfError as exc:
            raise Reject("corrupt_or_unreadable", "open") from exc
        self.open_warnings = list(self.pdf.get_warnings())
        if self.pdf.is_encrypted:
            raise Reject("encrypted")
        if self.open_warnings:
            raise Reject("repaired_by_parser", self.open_warnings[0][:80])
        problems = self.pdf.check_pdf_syntax()
        if problems:
            raise Reject("repaired_by_parser", problems[0][:80])
        self.inv: list[dict] = []
        self.fonts: dict[str, FontInfo] = {}
        self.uses: dict[Any, int] = {}
        self.units: list[PUnit] = []

    def add(self, element: str, kind: str, disp: str, reason: str = "") -> None:
        self.inv.append({"element": element, "kind": kind, "disposition": disp, "reason": reason})

    # ---- inventory
    def prescan_streams(self) -> None:
        """Bounded inflate of every Flate stream BEFORE any library decompresses it (counts bytes actually produced)."""
        total = 0
        for obj in self.pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                filt = obj.get("/Filter")
                names = (
                    [str(filt)]
                    if isinstance(filt, pikepdf.Name)
                    else [str(f) for f in filt]
                    if filt is not None
                    else []
                )
                if names == ["/FlateDecode"]:
                    total += len(bounded_inflate(obj.read_raw_bytes()))
                elif any(n not in ("/DCTDecode",) for n in names):
                    raise Reject("unsupported_filter", ",".join(names))
                if total > LIMITS["max_total_uncompressed"]:
                    raise Reject("limit_exceeded", "total produced bytes")

    def inventory(self) -> list[dict]:
        self.prescan_streams()
        root = self.pdf.Root
        trailer = self.pdf.trailer
        if "/Prev" in trailer:
            raise Reject("incremental_update", "/Prev")
        for k in root.keys():
            kk = k[1:]
            if kk in CATALOG_OK:
                continue
            if kk == "AcroForm":
                af = root.AcroForm
                sig = int(af.get("/SigFlags", 0)) != 0 or any(
                    str(f.get("/FT", "")) == "/Sig" for f in af.get("/Fields", [])
                )
                raise Reject("signed" if sig else "forms", "AcroForm")
            code = {
                "AcroForm": "forms",
                "OCProperties": "optional_content",
                "Names": "names_tree",
                "OpenAction": "actions",
                "AA": "actions",
                "StructTreeRoot": "tagged_structure",
                "Perms": "signed",
                "Collection": "attachments",
            }.get(kk, "unknown_catalog_entry")
            if kk == "Names":
                nm = root.Names
                for sub in nm.keys():
                    if sub in ("/EmbeddedFiles", "/JavaScript"):
                        raise Reject("attachments" if sub == "/EmbeddedFiles" else "actions", sub)
                continue
            if kk == "StructTreeRoot":
                self._check_struct(root.StructTreeRoot)
                self.add("StructTreeRoot", "tagged_structure", "P", "no /Alt or /ActualText")
                continue
            if kk == "OpenAction":
                a = root.OpenAction
                if isinstance(a, pikepdf.Dictionary) and str(a.get("/S", "")) in (
                    "/JavaScript",
                    "/Launch",
                    "/SubmitForm",
                    "/ImportData",
                    "/GoToR",
                    "/Rendition",
                ):
                    raise Reject("actions", str(a.get("/S")))
                continue
            raise Reject(code, kk)
        if "/Metadata" in root:
            self.add("Catalog/Metadata (XMP)", "metadata", "E", "XMP removed (descriptive)")
        if self.pdf.docinfo is not None and len(self.pdf.docinfo.keys()):
            self.add("Info", "metadata", "E", "descriptive/personal /Info fields removed")
        self.container_of_page: list[Any] = []
        for pi, page in enumerate(self.pdf.pages):
            if pi >= LIMITS["max_pdf_pages"]:
                raise Reject("limit_exceeded", "pages")
            self._inventory_page(pi, page)
        self.add(
            "file structure (page tree, xref, resources)",
            "structure",
            "P",
            "structural dictionaries inspected; linearization/PDF-A not preserved",
        )
        return self.inv

    def _check_struct(self, st: Any) -> None:
        seen = set()

        def walk(o: Any, d: int = 0) -> None:
            if d > 12:
                raise Reject("limit_exceeded", "struct depth")
            if isinstance(o, pikepdf.Dictionary):
                for k in ("/Alt", "/ActualText"):
                    if k in o:
                        raise Reject("tagged_structure_text", k)
                for v in o.values():
                    if isinstance(v, (pikepdf.Dictionary, pikepdf.Array)):
                        if v.is_indirect:
                            if v.objgen in seen:
                                continue
                            seen.add(v.objgen)
                        walk(v, d + 1)
            elif isinstance(o, pikepdf.Array):
                for v in o:
                    if isinstance(v, (pikepdf.Dictionary, pikepdf.Array)):
                        walk(v, d + 1)

        walk(st)

    def _inventory_page(self, pi: int, page: Any) -> None:
        for k in page.keys():
            if k in ("/AA",):
                raise Reject("actions", k)
        annots = page.get("/Annots")
        if annots is not None:
            for a in annots:
                st = str(a.get("/Subtype"))
                if st in ("/Text", "/Popup"):
                    self.add(f"page{pi}:annot{st}", "annotation", "E", "comment annotation removed")
                elif st == "/Link":
                    act = a.get("/A")
                    if act is not None and str(act.get("/S")) not in ("/URI", "/GoTo"):
                        raise Reject("actions", str(act.get("/S")))
                    self.add(f"page{pi}:annot/Link", "annotation", "N", "link target/appearance not covered")
                elif st == "/Widget":
                    raise Reject("forms")
                else:
                    raise Reject("annotation_unsupported", st)
        res = page.get("/Resources")
        if res is None:
            raise Reject("unknown_structure", "page without resources")
        if "/Properties" in res:
            raise Reject("optional_content")
        for xk, xo in (res.get("/XObject") or {}).items():
            st = str(xo.get("/Subtype"))
            if st == "/Image":
                self.add(f"page{pi}:{xk}", "image", "N", "image not covered (no OCR)")
            elif st != "/Form":
                raise Reject("unknown_structure", f"XObject {st}")
            else:
                if "/OC" in xo or "/Resources" not in xo and False:
                    raise Reject("optional_content")
        for fk, fo in (res.get("/Font") or {}).items():
            self.fonts[f"{pi}:{fk}"] = load_font(fo, f"{pi}:{fk}")
            self.add(
                f"page{pi}:font{fk}",
                "font",
                "P",
                f"mapping from {'ToUnicode' if '/ToUnicode' in fo else 'standard encoding'}; widths present",
            )

    # ---- extraction
    def extract(self) -> list[PUnit]:
        if not self.inv:
            self.inventory()
        users: dict[Any, int] = {}
        self.units = []
        for pi, page in enumerate(self.pdf.pages):
            text, segs = "", []
            state = {"pending_sep": False, "font": None, "paths": 0, "in_form": 0}
            self._walk(page, ("page", pi), page.Resources, pi, state, segs, users)
            segs, text = self._finalize(segs)
            check_unit_limits(text)
            if state["paths"]:
                self.add(f"page{pi}", "vector_graphics", "N", "painted paths present: text-as-paths cannot be verified")
            if not text and any(e["kind"] == "image" and e["element"].startswith(f"page{pi}:") for e in self.inv):
                raise Reject("scanned_document", f"page {pi} has images and no text")
            self.units.append(PUnit(pi, text, segs))
        self.uses = users
        return self.units

    @staticmethod
    def _finalize(segs: list[Seg]) -> tuple[list[Seg], str]:
        out, pos, text = [], 0, ""
        for s in segs:
            n = s.u_end - s.u_start
            s.u_start, s.u_end = pos, pos + n
            pos += n
            text += s.text if hasattr(s, "text") else ""
            out.append(s)
        return out, "".join(getattr(s, "text", "") for s in out)

    def _walk(
        self, owner: Any, container: tuple, resources: Any, pi: int, state: dict, segs: list[Seg], users: dict
    ) -> None:
        instrs = pikepdf.parse_content_stream(owner)
        font: FontInfo | None = state.get("font")
        for ii, ins in enumerate(instrs):
            op = str(ins.operator)
            if op in BAD_TEXT_STATE:
                raise Reject("text_state_unsupported", op)
            if op == "Tr" and int(ins.operands[0]) != 0:
                raise Reject("invisible_or_clipped_text", f"Tr {int(ins.operands[0])}")
            if op in PAINT:
                state["paths"] += 1
            if op in ("BI", "ID", "EI"):
                self.add(f"page{pi}", "inline_image", "N", "inline image not covered")
            if op == "Tf":
                font = (
                    self.fonts[f"{pi}:{str(ins.operands[0])}"]
                    if f"{pi}:{str(ins.operands[0])}" in self.fonts
                    else self._font_from(resources, str(ins.operands[0]), pi)
                )
                continue
            if op == "Do":
                xo = resources["/XObject"][ins.operands[0]]
                if str(xo.get("/Subtype")) == "/Form":
                    key = ("xobj", xo.objgen)
                    users[key] = users.get(key, 0) + 1
                    state["pending_sep"] = True
                    sub_res = xo.get("/Resources", resources)
                    saved = state.get("font")
                    state["font"] = font
                    self._walk(xo, key, sub_res, pi, state, segs, users)
                    state["font"] = saved
                    state["pending_sep"] = True
                continue
            if op in POSITION and op not in SHOW:
                state["pending_sep"] = True
                continue
            if op in SHOW:
                if font is None:
                    raise Reject("font_mapping_insufficient", "show without font")
                if op in POSITION:
                    state["pending_sep"] = True
                operands = ins.operands
                elems = list(operands[0]) if op == "TJ" else [operands[-1]]
                shown_any = False
                for ei, el in enumerate(elems):
                    if isinstance(el, pikepdf.String):
                        raw = bytes(el)
                        if (
                            raw
                            and state["pending_sep"]
                            and not shown_any
                            and (segs and any(s.kind == "original" for s in segs))
                        ):
                            sep = Seg(0, 1, "synthesized")
                            sep.text = "\n"  # type: ignore[attr-defined]
                            segs.append(sep)
                        if raw:
                            state["pending_sep"] = False
                            shown_any = True
                        for bo, code in enumerate(raw):
                            if code not in font.code2uni:
                                raise Reject("font_mapping_insufficient", f"code {code}")
                            u = font.code2uni[code]
                            sg = Seg(
                                0,
                                len(u),
                                "original",
                                (container, ii, ei, bo, 1),
                                font,
                                "1:1" if len(u) == 1 else "n:1",
                                font.width(code),
                                code,
                            )
                            sg.text = u  # type: ignore[attr-defined]
                            segs.append(sg)
        state["font"] = font

    def _font_from(self, resources: Any, name: str, pi: int) -> FontInfo:
        key = f"{pi}:xobj:{name}"
        if key not in self.fonts:
            self.fonts[key] = load_font(resources["/Font"][name], key)
        return self.fonts[key]


# ---------------------------------------------------------------- transformation
def _pieces(unit: PUnit, start: int, end: int) -> list[Seg]:
    if not (0 <= start < end <= len(unit.text)):
        raise Reject("localization_ambiguous", "interval outside the unit text")
    hit = [s for s in unit.segs if s.u_start < end and s.u_end > start]
    if any(s.kind != "original" for s in hit):
        raise Reject("localization_ambiguous", "interval includes a synthesized separator (artificial concatenation)")
    if hit[0].u_start != start or hit[-1].u_end != end:
        raise Reject("localization_ambiguous", "boundary falls inside an indivisible piece (n:1)")
    if len({s.font.key.split(":", 1)[1] if s.font else None for s in hit}) != 1:
        raise Reject("localization_ambiguous", "font change inside the value")
    return hit


def transform(doc: PdfDoc, selections: list[dict], *, remove_authorized: bool = True) -> tuple[bytes, dict]:
    if sha256_bytes(doc.data) != doc.sha256:
        raise Reject("identity_mismatch")
    if len(selections) > LIMITS["max_selected"]:
        raise Reject("limit_exceeded", "selected")
    plans = []
    for sel in selections:
        unit = doc.units[sel["unit"]]
        segs = _pieces(unit, sel["start"], sel["end"])
        font = segs[0].font
        rep = sel["replacement"]
        codes = []
        for ch in rep:
            if ch not in font.uni2code:
                raise Reject("substitute_glyph_unavailable", "character not encodable in the font")
            if font.available is not None and ch not in font.available:
                raise Reject("substitute_glyph_unavailable", "no glyph for the character in the embedded font program")
            codes.append(font.uni2code[ch])
        w_new = sum(font.width(c) for c in codes)
        w_old = sum(s.width for s in segs)
        # kerning numbers inside the selected span also advance the pen
        w_old += sum(-n for n in _numbers_between(doc, segs))
        if w_new > w_old + 1e-6:
            raise Reject("replacement_overflow", f"{w_new:.0f} > {w_old:.0f}")
        plans.append({"segs": segs, "codes": bytes(codes), "comp": w_old - w_new, "font": font})
    # shared objects: every use must be selected
    hits: dict[tuple, int] = {}
    for p in plans:
        key = p["segs"][0].piece[:4]
        hits[key] = hits.get(key, 0) + 1
    for key, n in hits.items():
        cont = key[0]
        if cont[0] == "xobj" and doc.uses.get(cont, 1) > n:
            raise Reject("shared_object_partial", f"{n} of {doc.uses[cont]} uses selected")
    # apply edits grouped by container+instruction (deduplicate shared pieces)
    edits: dict[tuple, dict] = {}
    drops: dict[tuple, list] = {}
    done: set = set()
    for p in plans:
        pk = tuple(s.piece[:4] for s in p["segs"])
        if pk in done:
            continue
        done.add(pk)
        a, b = p["segs"][0].piece, p["segs"][-1].piece
        if a[:2] == b[:2] and a[2] != b[2]:
            drops.setdefault((a[0], a[1]), []).append((a[2], b[2]))
        for k, s in enumerate(p["segs"]):
            cont, ii, ei, bo, _ = s.piece
            e = edits.setdefault((cont, ii), {}).setdefault(ei, [])
            e.append((bo, 0 if k else 1, p["codes"] if k == 0 else b"", p["comp"] if k == 0 else 0.0, s))
    changed = _apply(doc, edits, drops)
    report: dict = {
        "removed_classes": [],
        "edited_streams": sorted(str(c) for c in changed),
        "compensations": [round(p["comp"], 6) for p in plans],
    }
    if remove_authorized:
        _remove_authorized(doc, report)
    out = io.BytesIO()
    doc.pdf.save(out, deterministic_id=True)
    return out.getvalue(), report


def _numbers_between(doc: PdfDoc, segs: list[Seg]) -> list[float]:
    """Kerning numbers of a TJ array lying between the first and last selected string pieces (same instruction)."""
    nums: list[float] = []
    ins_cache = {}
    first, last = segs[0].piece, segs[-1].piece
    if first[:2] != last[:2] or first[2] == last[2]:
        return nums
    cont, ii = first[0], first[1]
    owner = doc.pdf.pages[cont[1]] if cont[0] == "page" else doc.pdf.get_object(cont[1])
    instrs = ins_cache.setdefault(cont, pikepdf.parse_content_stream(owner))
    el = list(instrs[ii].operands[0])
    for k in range(first[2] + 1, last[2]):
        if not isinstance(el[k], pikepdf.String):
            nums.append(float(el[k]))
    return nums


def _apply(doc: PdfDoc, edits: dict, drops: dict) -> set:
    by_cont: dict[tuple, dict[int, dict]] = {}
    for (cont, ii), v in edits.items():
        by_cont.setdefault(cont, {})[ii] = v
    changed = set()
    for cont, per in by_cont.items():
        owner = doc.pdf.pages[cont[1]] if cont[0] == "page" else doc.pdf.get_object(cont[1])
        instrs = list(pikepdf.parse_content_stream(owner))
        for ii, per_elem in per.items():
            ins = instrs[ii]
            op = str(ins.operator)
            elems = list(ins.operands[0]) if op == "TJ" else [ins.operands[-1]]
            new: list = []
            need_tj = False
            for ei, el in enumerate(elems):
                if ei in per_elem and isinstance(el, pikepdf.String):
                    raw = bytes(el)
                    cur = bytearray()
                    cursor = 0
                    for bo, is_first, codes, comp, _s in sorted(per_elem[ei], key=lambda t: t[0]):
                        cur += raw[cursor:bo]
                        cursor = bo + 1  # each edit consumes exactly one original code (simple fonts)
                        if is_first:
                            cur += codes
                            if comp > 1e-9:
                                new.append(pikepdf.String(bytes(cur)))
                                new.append(-comp)
                                cur = bytearray()
                                need_tj = True
                    cur += raw[cursor:]
                    if cur or not any(isinstance(x, pikepdf.String) for x in new[-1:]):
                        new.append(pikepdf.String(bytes(cur)))
                else:
                    if not isinstance(el, pikepdf.String) and any(lo < ei < hi for lo, hi in drops.get((cont, ii), [])):
                        continue  # kerning inside ONE selected span is accounted for by that selection's compensation
                    new.append(el)
            if op == "TJ" or need_tj:
                instrs[ii] = pikepdf.ContentStreamInstruction([pikepdf.Array(new)], pikepdf.Operator("TJ"))
            else:
                instrs[ii] = pikepdf.ContentStreamInstruction(
                    ins.operands[:-1] + [new[0]] if len(ins.operands) > 1 else [new[0]], ins.operator
                )
        data = pikepdf.unparse_content_stream(instrs)
        if cont[0] == "page":
            owner.Contents = doc.pdf.make_stream(data)
        else:
            owner.write(data)
        changed.add(cont)
    return changed


def _remove_authorized(doc: PdfDoc, report: dict) -> None:
    pdf = doc.pdf
    di = pdf.docinfo
    removed = [
        k for k in list(di.keys()) if k in ("/Title", "/Author", "/Subject", "/Keywords", "/Creator", "/Producer")
    ]
    for k in removed:
        del di[k]
    if removed:
        report["removed_classes"] += ["info_authorship", "info_descriptive"]
    if "/Metadata" in pdf.Root:
        del pdf.Root["/Metadata"]
        report["removed_classes"].append("xmp_descriptive")
    for page in pdf.pages:
        an = page.get("/Annots")
        if an is None:
            continue
        keep = [a for a in an if str(a.get("/Subtype")) not in ("/Text", "/Popup")]
        if len(keep) != len(an):
            report["removed_classes"].append("comment_annotations")
            if keep:
                page.Annots = pikepdf.Array(keep)
            else:
                del page["/Annots"]
    report["removed_classes"] = sorted(set(report["removed_classes"]))


# ---------------------------------------------------------------- mechanical restoration (EXP-09 feasibility only)
def restore(doc: PdfDoc, items: list[dict]) -> bytes:
    """Replace each substitute (located BY POSITION, validated against the extracted text) with the original value and drop
    the layout compensation number. Local, synthetic correspondences only: this is not the A06 restoration contract."""
    by_cont: dict[tuple, list] = {}
    for it in items:
        unit = doc.units[it["unit"]]
        segs = _pieces(unit, it["start"], it["start"] + len(it["sub"]))
        if "".join(unit.text[s.u_start : s.u_end] for s in segs) != it["sub"]:
            raise Reject("restore_mismatch", "text at the recorded position is not the recorded substitute")
        font = segs[0].font
        if any(s.piece[:3] != segs[0].piece[:3] for s in segs):
            raise Reject("restore_mismatch", "substitute spread over several operators")
        codes = bytes(font.uni2code[ch] for ch in it["orig"])
        by_cont.setdefault(segs[0].piece[0], []).append(
            (segs[0].piece[1], segs[0].piece[2], segs[0].piece[3], len(it["sub"]), codes, it["comp"])
        )
    for cont, lst in by_cont.items():
        owner = doc.pdf.pages[cont[1]] if cont[0] == "page" else doc.pdf.get_object(cont[1])
        instrs = list(pikepdf.parse_content_stream(owner))
        for ii in sorted({x[0] for x in lst}):
            ins = instrs[ii]
            elems = list(ins.operands[0]) if str(ins.operator) == "TJ" else [ins.operands[-1]]
            for _i, ei, bo, n, codes, comp in sorted((x for x in lst if x[0] == ii), key=lambda x: (-x[1], -x[2])):
                raw = bytes(elems[ei])
                elems[ei] = pikepdf.String(raw[:bo] + codes + raw[bo + n :])
                if comp > 1e-9:
                    nxt = elems[ei + 1] if ei + 1 < len(elems) else None
                    if nxt is None or isinstance(nxt, pikepdf.String) or abs(float(nxt) + comp) > 1e-6:
                        raise Reject("restore_mismatch", "compensation number not found where recorded")
                    del elems[ei + 1]
            instrs[ii] = (
                pikepdf.ContentStreamInstruction([pikepdf.Array(elems)], pikepdf.Operator("TJ"))
                if str(ins.operator) == "TJ" or len(elems) != 1
                else pikepdf.ContentStreamInstruction(ins.operands[:-1] + [elems[0]], ins.operator)
            )
        data = pikepdf.unparse_content_stream(instrs)
        if cont[0] == "page":
            owner.Contents = doc.pdf.make_stream(data)
        else:
            owner.write(data)
    out = io.BytesIO()
    doc.pdf.save(out, deterministic_id=True)
    return out.getvalue()
