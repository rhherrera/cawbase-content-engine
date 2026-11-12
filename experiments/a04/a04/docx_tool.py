"""Bounded DOCX tooling for the A04 experiments: inventory, extraction, mapping, transformation (lxml).

This is experiment code, not a framework: it implements only what is needed to test the A04 hypotheses.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from a04.common import LIMITS, Reject, check_unit_limits, read_zip_bounded, sha256_bytes

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
RN = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
CTN = "http://schemas.openxmlformats.org/package/2006/content-types"
q = lambda t: f"{{{W}}}{t}"  # noqa: E731
EXTRACTOR = {
    "id": "a04-docx-experiment",
    "version": "1",
    "config": {"unit": "paragraph", "order": "body,hdr/ftr(section,type),footnotes,endnotes"},
}
EXTRACTOR_CONFIG_SHA = sha256_bytes(repr(sorted(EXTRACTOR["config"].items())).encode())

REVISION = {
    "ins",
    "del",
    "moveFrom",
    "moveTo",
    "moveFromRangeStart",
    "moveFromRangeEnd",
    "moveToRangeStart",
    "moveToRangeEnd",
    "rPrChange",
    "pPrChange",
    "sectPrChange",
    "tblPrChange",
    "tblGridChange",
    "trPrChange",
    "tcPrChange",
    "numberingChange",
    "cellIns",
    "cellDel",
    "cellMerge",
}
REJECT_EL = {
    **{k: "revisions" for k in REVISION},
    "vanish": "hidden_text",
    "specVanish": "hidden_text",
    "txbxContent": "text_box",
    "AlternateContent": "text_box",
    "pict": "text_box",
    "textbox": "text_box",
    "object": "embedded_object",
    "control": "embedded_object",
    "altChunk": "external_content",
    "OLEObject": "embedded_object",
}
EXTERNAL_FIELDS = re.compile(
    r"^\s*(INCLUDETEXT|INCLUDEPICTURE|LINK|DDE|DDEAUTO|AUTOTEXT|IMPORT|DATABASE|MACROBUTTON|HYPERLINK\s+\\l)\b", re.I
)
REL_OK = {
    "officeDocument",
    "styles",
    "settings",
    "fontTable",
    "theme",
    "numbering",
    "webSettings",
    "header",
    "footer",
    "footnotes",
    "endnotes",
    "comments",
    "commentsExtended",
    "commentsIds",
    "people",
    "core-properties",
    "extended-properties",
    "custom-properties",
    "thumbnail",
    "image",
    "hyperlink",
}
E_PARTS = {
    "word/comments.xml",
    "word/commentsExtended.xml",
    "word/commentsIds.xml",
    "word/people.xml",
    "docProps/custom.xml",
}
P_PARTS = {
    "word/styles.xml",
    "word/settings.xml",
    "word/fontTable.xml",
    "word/numbering.xml",
    "word/theme/theme1.xml",
    "word/webSettings.xml",
}
MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpeg", b"GIF8": "gif"}


def parse_xml(data: bytes) -> etree._Element:
    if re.search(rb"<!(DOCTYPE|ENTITY)", data, re.I):
        raise Reject("xml_doctype")
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, dtd_validation=False
    )
    try:
        return etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise Reject("corrupt_or_unreadable", "xml") from exc


@dataclass
class Segment:
    u_start: int
    u_end: int
    kind: str  # "original" | "special" (tab/br: real char, no w:t) | "field_result" (original but not transformable)
    part: str
    node: Any = None  # lxml element (w:t) - not serialized
    node_path: str = ""
    t_start: int = 0
    t_end: int = 0
    relation: str = "1:1"


@dataclass
class Unit:
    unit_id: str
    part: str
    ordinal: int
    text: str
    segments: list[Segment] = field(default_factory=list)
    scope_note: str = ""


@dataclass
class Inventory:
    entries: list[dict] = field(default_factory=list)  # {"element","kind","disposition","reason"}

    def add(self, element: str, kind: str, disp: str, reason: str = "") -> None:
        self.entries.append({"element": element, "kind": kind, "disposition": disp, "reason": reason})

    def has(self, disp: str) -> bool:
        return any(e["disposition"] == disp for e in self.entries)


class Docx:
    def __init__(self, data: bytes):
        self.data = data
        self.sha256 = sha256_bytes(data)
        if data[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
            raise Reject("encrypted_or_legacy", "CFB container")
        self.raw = read_zip_bounded(data)
        self.order = list(zipfile.ZipFile(io.BytesIO(data)).namelist())
        self.trees: dict[str, etree._Element] = {}
        self.inv = Inventory()
        self.units: list[Unit] = []
        self.rels: dict[str, list[dict]] = {}
        self.ctypes: dict[str, str] = {}

    # -- parsing helpers
    def tree(self, name: str) -> etree._Element:
        if name not in self.trees:
            self.trees[name] = parse_xml(self.raw[name])
        return self.trees[name]

    def load_package(self) -> None:
        if "[Content_Types].xml" not in self.raw or "_rels/.rels" not in self.raw:
            raise Reject("corrupt_or_unreadable", "not an OPC package")
        ct = self.tree("[Content_Types].xml")
        for e in ct:
            t = etree.QName(e).localname
            if t == "Override":
                self.ctypes[e.get("PartName").lstrip("/")] = e.get("ContentType")
        for name in self.raw:
            if name.endswith(".rels"):
                for r in self.tree(name):
                    base = posixpath.dirname(posixpath.dirname(name))
                    tgt = r.get("Target")
                    ext = r.get("TargetMode") == "External"
                    full = (
                        tgt
                        if ext
                        else posixpath.normpath(posixpath.join(base, tgt))
                        if not tgt.startswith("/")
                        else tgt.lstrip("/")
                    )
                    self.rels.setdefault(name, []).append(
                        {
                            "id": r.get("Id"),
                            "type": r.get("Type").rsplit("/", 1)[-1],
                            "target": full,
                            "raw_target": tgt,
                            "external": ext,
                        }
                    )

    # -- inventory (all parts, relations, elements)
    def inventory(self) -> Inventory:
        self.load_package()
        referenced_hf: dict[str, str] = {}
        sig = [n for n in self.raw if n.startswith(("_xmlsignatures/", "_xmlsignatures"))]
        if sig or any(
            "digital-signature" in r["type"] or "signature" in r["type"].lower()
            for rs in self.rels.values()
            for r in rs
        ):
            raise Reject("signed")
        # relationships
        for rname, rs in self.rels.items():
            for r in rs:
                if r["type"] not in REL_OK:
                    raise Reject("unknown_relationship", r["type"])
                if r["external"]:
                    if r["type"] == "hyperlink":
                        self.inv.add(f"{rname}#{r['id']}", "hyperlink_target", "N", "link target not covered")
                    else:
                        raise Reject("external_reference", r["type"])
                elif r["target"] not in self.raw:
                    raise Reject("corrupt_or_unreadable", "dangling relationship")
        # parts
        for name in self.order:
            if name.endswith("/") or name in ("[Content_Types].xml",) or name.endswith(".rels"):
                continue
            if (
                name == "word/document.xml"
                or re.fullmatch(r"word/(header|footer)\d*\.xml", name)
                or name in ("word/footnotes.xml", "word/endnotes.xml")
            ):
                continue
            if name in E_PARTS:
                self.inv.add(name, "part", "E", "authorized comment/metadata part")
            elif name in P_PARTS:
                self.inv.add(name, "part", "P", "structural; inspected")
            elif name in ("docProps/core.xml", "docProps/app.xml"):
                self.inv.add(name, "part", "E", "descriptive/personal properties blanked")
            elif name.startswith("docProps/thumbnail"):
                self.inv.add(name, "part", "E", "thumbnail removed")
            elif name.startswith("word/media/"):
                if not any(self.raw[name].startswith(m) for m in MAGIC):
                    raise Reject("media_type_mismatch")
                self.inv.add(name, "part", "N", "image not covered (no OCR)")
            elif re.search(r"vbaProject|embeddings/|customXml/|glossary/|charts/|diagrams/|activeX", name):
                raise Reject("embedded_or_unknown_part", name.split("/")[1] if "/" in name else name)
            else:
                raise Reject("unknown_part", name)
        # header/footer references and orphans
        doc = self.tree("word/document.xml")
        docrels = {r["id"]: r for r in self.rels.get("word/_rels/document.xml.rels", [])}
        for sp in doc.iter(q("sectPr")):
            for ref in list(sp.findall(q("headerReference"))) + list(sp.findall(q("footerReference"))):
                rid = ref.get(f"{{{RN}}}id")
                referenced_hf[docrels[rid]["target"]] = f"{etree.QName(ref).localname}:{ref.get(q('type'))}"
        for name in self.order:
            if re.fullmatch(r"word/(header|footer)\d*\.xml", name) and name not in referenced_hf:
                raise Reject("orphan_header_footer", name)
        self.hf = referenced_hf
        self.content_parts = (
            ["word/document.xml"]
            + self._hf_order()
            + [p for p in ("word/footnotes.xml", "word/endnotes.xml") if p in self.raw]
        )
        for p in self.content_parts:
            self._scan_elements(p)
        self._inspect_structural()
        return self.inv

    def _hf_order(self) -> list[str]:
        rank = {
            "headerReference:default": 0,
            "headerReference:first": 1,
            "headerReference:even": 2,
            "footerReference:default": 3,
            "footerReference:first": 4,
            "footerReference:even": 5,
        }
        return [n for n, _ in sorted(self.hf.items(), key=lambda kv: (rank.get(kv[1], 9), kv[0]))]

    def _scan_elements(self, part: str) -> None:
        root = self.tree(part)
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            ln = etree.QName(el).localname
            ns = etree.QName(el).namespace
            if ns == W and ln in REJECT_EL:
                raise Reject(REJECT_EL[ln], f"{part}:{ln}")
            if ns and ns.endswith("/math") and ln in ("oMath", "oMathPara"):
                self.inv.add(part, "equation", "N", "equation text not extracted")
            if ns == W and ln == "sdtPr" and el.find(q("dataBinding")) is not None:
                raise Reject("data_bound_control", part)
            if ns == W and ln == "instrText" or (ns == W and ln == "fldSimple"):
                instr = el.text if ln == "instrText" else el.get(q("instr"))
                if EXTERNAL_FIELDS.match(instr or ""):
                    raise Reject("external_field", part)
                self.inv.add(part, "field_instruction", "N", "instruction not extracted/evaluated")
            if ns == W and ln == "drawing":
                uris = [
                    g.get("uri", "")
                    for g in el.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}graphicData")
                ]
                if any(u.endswith(("/chart", "/diagram")) for u in uris):
                    raise Reject("chart_or_diagram", part)
                self.inv.add(part, "drawing", "N", "image/alt text not covered")
            if ln == "docPr" and (el.get("descr") or el.get("title")):
                self.inv.add(part, "alt_text", "N", "alternative text not covered")
            if ns == W and ln == "sym":
                self.inv.add(part, "symbol", "N", "symbol character not extracted")
        self.inv.add(part, "content_part", "S", "text extracted and mapped")

    # -- structural inspection (contract 2.2): allow-list of strings; free strings reclassify to N
    _ID = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")
    _SAFE_ATTR_NAMES = {
        "val",
        "ascii",
        "hAnsi",
        "cs",
        "eastAsia",
        "styleId",
        "type",
        "default",
        "name",
        "w",
        "sz",
        "percent",
        "charset",
        "color",
        "fill",
        "space",
    }

    def _inspect_structural(self) -> None:
        for name in sorted(P_PARTS & set(self.raw)):
            root = self.tree(name)
            free = []
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                ln = etree.QName(el).localname
                if ln in ("docVars", "docVar", "mailMerge", "attachedTemplate"):
                    raise Reject("settings_payload_or_external", ln)
                if el.text and el.text.strip():
                    free.append(f"{ln}:text")
                for k, v in el.attrib.items():
                    kn = etree.QName(k).localname
                    if ln == "lvlText" and kn == "val" and not re.fullmatch(r"[%0-9.()\s\-•·o§]*", v):
                        free.append("lvlText:free text")
                    elif kn not in self._SAFE_ATTR_NAMES and not self._ID.match(v):
                        free.append(f"{ln}@{kn}")
                    elif len(v) > 64:
                        free.append(f"{ln}@{kn}:long")
            if free:
                self.inv.add(
                    name, "structural_free_strings", "N", f"{len(free)} free string(s): {sorted(set(free))[:5]}"
                )
            else:
                self.inv.add(name, "structural_inspection", "P", "all strings in allow-list")

    # -- extraction + mapping
    def extract(self) -> list[Unit]:
        if not self.inv.entries:
            self.inventory()
        self.units = []
        for part in self.content_parts:
            root = self.tree(part)
            for p in root.iter(q("p")):
                text, segs = self._paragraph(p, part)
                if not text:
                    continue
                check_unit_limits(text)
                u = Unit(
                    f"u{len(self.units)}",
                    part,
                    len(self.units),
                    text,
                    segs,
                    scope_note="paragraph unit: local scope, not a document-level classification",
                )
                self.units.append(u)
        if len(self.units) > LIMITS["max_units"]:
            raise Reject("limit_exceeded", "units")
        return self.units

    def _paragraph(self, p: etree._Element, part: str) -> tuple[str, list[Segment]]:
        out, segs, pos = [], [], 0
        depth_stack: list[str] = []  # field phases
        in_simple = lambda el: any(a.tag == q("fldSimple") for a in el.iterancestors())  # noqa: E731
        for r in p.iter(q("r")):
            for ch in r:
                ln = etree.QName(ch).localname if isinstance(ch.tag, str) else ""
                if ln == "fldChar":
                    ft = ch.get(q("fldCharType"))
                    if ft == "begin":
                        depth_stack.append("instr")
                    elif ft == "separate" and depth_stack:
                        depth_stack[-1] = "result"
                    elif ft == "end" and depth_stack:
                        depth_stack.pop()
                    continue
                in_result = bool(depth_stack and depth_stack[-1] == "result") or in_simple(r)
                if depth_stack and depth_stack[-1] == "instr":
                    continue
                if ln == "t":
                    t = ch.text or ""
                    if not t:
                        continue
                    segs.append(
                        Segment(
                            pos,
                            pos + len(t),
                            "field_result" if in_result else "original",
                            part,
                            ch,
                            p.getroottree().getpath(ch),
                            0,
                            len(t),
                        )
                    )
                    out.append(t)
                    pos += len(t)
                elif ln in ("tab", "br", "cr", "noBreakHyphen"):
                    c = {"tab": "\t", "br": "\n", "cr": "\n", "noBreakHyphen": "‑"}[ln]
                    segs.append(Segment(pos, pos + 1, "special", part, ch, p.getroottree().getpath(ch)))
                    out.append(c)
                    pos += 1
        return "".join(out), segs


def resolve(unit: Unit, start: int, end: int) -> list[Segment]:
    """Segments overlapped by [start,end). Raises Reject when the interval cannot be localised unambiguously."""
    if not (0 <= start < end <= len(unit.text)):
        raise Reject("localization_ambiguous", "interval outside the unit text")
    hit = [s for s in unit.segments if s.u_start < end and s.u_end > start]
    covered = sum(min(s.u_end, end) - max(s.u_start, start) for s in hit)
    if covered != end - start:
        raise Reject("localization_ambiguous", "interval not fully covered by original pieces")
    return hit


def transform(dx: Docx, selections: list[dict], *, remove_authorized: bool = True) -> tuple[bytes, dict]:
    """selections: {"unit": index, "start", "end", "replacement"}. Returns (new bytes, report)."""
    if sha256_bytes(dx.data) != dx.sha256:
        raise Reject("identity_mismatch")
    if len(selections) > LIMITS["max_selected"]:
        raise Reject("limit_exceeded", "selected")
    edits: dict[int, list[tuple[int, int, str, Segment]]] = {}
    seen: list[tuple[int, int, int]] = []
    for sel in selections:
        u = dx.units[sel["unit"]]
        hit = resolve(u, sel["start"], sel["end"])
        for s in hit:
            if s.kind == "field_result":
                raise Reject("field_result_not_transformable", "field result selected")
            if s.kind == "special":
                raise Reject("localization_ambiguous", "interval includes tab/break node")
        for a, b, c in seen:
            if c == sel["unit"] and a < sel["end"] and b > sel["start"]:
                raise Reject("overlapping_selection")
        seen.append((sel["start"], sel["end"], sel["unit"]))
        for k, s in enumerate(hit):
            lo = max(s.u_start, sel["start"]) - s.u_start
            hi = min(s.u_end, sel["end"]) - s.u_start
            edits.setdefault(id(s.node), []).append((lo, hi, sel["replacement"] if k == 0 else "", s))
    for _key, lst in edits.items():
        node = lst[0][3].node
        t = node.text or ""
        for lo, hi, rep, _s in sorted(lst, key=lambda x: -x[0]):
            t = t[:lo] + rep + t[hi:]
        node.text = t
        if t != t.strip() or "  " in t:
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    report: dict = {"removed_classes": [], "removed_parts": []}
    changed: dict[str, bytes] = {}
    for part in dx.content_parts:
        if part in dx.trees:
            changed[part] = etree.tostring(dx.trees[part], xml_declaration=True, encoding="UTF-8", standalone=True)
    removed: set[str] = set()
    if remove_authorized:
        _remove_authorized(dx, changed, removed, report)
    return _repack(dx, changed, removed), report


def _blank(root: etree._Element, names: set[str]) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and etree.QName(el).localname in names:
            el.text = ""


def _remove_authorized(dx: Docx, changed: dict[str, bytes], removed: set[str], report: dict) -> None:
    present = lambda n: n in dx.raw  # noqa: E731
    # comments: anchors + parts
    if present("word/comments.xml"):
        for part in dx.content_parts:
            root = dx.tree(part)
            for el in list(root.iter(q("commentRangeStart"), q("commentRangeEnd"))):
                el.getparent().remove(el)
            for el in list(root.iter(q("commentReference"))):
                run = el.getparent()
                if all(c.tag in (q("rPr"), q("commentReference")) for c in run):
                    run.getparent().remove(run)
                else:
                    run.remove(el)
            changed[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        report["removed_classes"].append("comments")
    for n in sorted(E_PARTS & set(dx.raw)):
        removed.add(n)
        report["removed_parts"].append(n)
    if "word/comments.xml" in removed:
        pass
    if present("word/people.xml"):
        report["removed_classes"].append("comment_authors")
    if present("docProps/custom.xml"):
        report["removed_classes"].append("custom_properties")
    for n in [n for n in dx.raw if n.startswith("docProps/thumbnail")]:
        removed.add(n)
        report["removed_parts"].append(n)
        report["removed_classes"].append("thumbnail")
    if present("docProps/core.xml"):
        root = dx.tree("docProps/core.xml")
        _blank(root, {"creator", "lastModifiedBy", "title", "subject", "keywords", "description", "category"})
        changed["docProps/core.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        report["removed_classes"] += ["authorship", "descriptive_properties"]
    if present("docProps/app.xml"):
        root = dx.tree("docProps/app.xml")
        _blank(root, {"Company", "Manager"})
        changed["docProps/app.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        report["removed_classes"].append("company")
    # relationships + content types
    for rname in [n for n in dx.raw if n.endswith(".rels")]:
        root = dx.tree(rname)
        base = posixpath.dirname(posixpath.dirname(rname))
        mod = False
        for r in list(root):
            if r.get("TargetMode") == "External":
                continue
            tgt = posixpath.normpath(posixpath.join(base, r.get("Target")))
            if tgt in removed:
                root.remove(r)
                mod = True
        if mod:
            changed[rname] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    ct = dx.tree("[Content_Types].xml")
    for e in list(ct):
        if e.get("PartName") and e.get("PartName").lstrip("/") in removed:
            ct.remove(e)
    changed["[Content_Types].xml"] = etree.tostring(ct, xml_declaration=True, encoding="UTF-8", standalone=True)
    report["removed_classes"] = sorted(set(report["removed_classes"]))


def _repack(dx: Docx, changed: dict[str, bytes], removed: set[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in dx.order:
            if name in removed or name.endswith("/"):
                continue
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, changed.get(name, dx.raw[name]))
    return buf.getvalue()
