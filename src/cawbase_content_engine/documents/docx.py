"""DOCX validation + extraction (matrix of docs 11.2). Hardened: bounded ZIP reads, no DTD/entities, no network."""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from .common import Limits, Reject, Unit

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

_TRACKED = {
    "ins",
    "del",
    "moveFrom",
    "moveTo",
    "rPrChange",
    "pPrChange",
    "sectPrChange",
    "tblPrChange",
    "tcPrChange",
    "trPrChange",
    "numberingChange",
    "tblGridChange",
    "cellIns",
    "cellDel",
    "cellMerge",
    "moveFromRangeStart",
    "moveToRangeStart",
}
_FORBIDDEN_PARTS = (
    "customXml/",
    "word/glossary/",
    "word/charts/",
    "word/diagrams/",
    "word/embeddings/",
    "word/activeX/",
    "_xmlsignatures/",
    "word/vbaProject",
    "word/vbaData",
)
_EXTERNAL_FIELDS = re.compile(r"\b(INCLUDETEXT|INCLUDEPICTURE|LINK|DDE|DDEAUTO|AUTOTEXT|IMPORT)\b", re.I)


def _q(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


def read_parts(data: bytes, lim: Limits) -> dict[str, bytes]:
    if data[:4] == b"\xd0\xcf\x11\xe0":
        raise Reject("encrypted")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Reject("type_mismatch") from None
    infos = zf.infolist()
    if len(infos) > lim.max_zip_entries:
        raise Reject("structure_limit")
    seen: set[str] = set()
    total = 0
    parts: dict[str, bytes] = {}
    for info in infos:
        name = info.filename
        norm = name.lower()
        if (
            name.startswith("/")
            or ".." in name.split("/")
            or "\\" in name
            or any(ord(ch) < 32 for ch in name)
            or norm in seen
        ):
            raise Reject("unsupported_structure")
        seen.add(norm)
        if info.is_dir():
            continue
        if info.file_size > lim.max_decompressed_entry_bytes:
            raise Reject("decompression_limit")
        if (
            info.compress_size
            and info.file_size > 4096
            and info.file_size / info.compress_size > lim.max_compression_ratio
        ):
            raise Reject("decompression_limit")
        produced = 0
        chunks: list[bytes] = []
        keep = name.endswith((".xml", ".rels")) or name == "[Content_Types].xml"
        with zf.open(info) as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                produced += len(chunk)
                total += len(chunk)
                if produced > lim.max_decompressed_entry_bytes or total > lim.max_decompressed_total_bytes:
                    raise Reject("decompression_limit")
                if produced > info.file_size + 1024 * 1024 and info.file_size < 1024:
                    raise Reject("decompression_limit")  # headers claim far less than what is produced
                if keep:
                    chunks.append(chunk)
        parts[name] = b"".join(chunks) if keep else b""
        if keep and len([p for p in parts if p.endswith(".xml")]) > lim.max_docx_parts_processed:
            raise Reject("structure_limit")
    return parts


def parse_xml(raw: bytes, lim: Limits) -> Any:
    from lxml import etree

    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
        raise Reject("unsupported_structure")
    parser_kwargs = {"resolve_entities": False, "no_network": True, "load_dtd": False, "huge_tree": False}
    depth = nodes = 0
    try:
        it = etree.iterparse(io.BytesIO(raw), events=("start", "end"), **parser_kwargs)
        for event, _el in it:
            if event == "start":
                depth += 1
                nodes += 1
                if depth > lim.max_xml_depth or nodes > lim.max_xml_nodes_total:
                    raise Reject("structure_limit")
            else:
                depth -= 1
        return it.root
    except etree.XMLSyntaxError:
        raise Reject("parser_repair") from None


def extract(data: bytes, lim: Limits) -> dict[str, Any]:
    from lxml import etree

    parts = read_parts(data, lim)
    names = set(parts)
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise Reject("unsupported_structure")
    ct = parts["[Content_Types].xml"].decode("utf-8", "replace")
    if "macroEnabled" in ct or "vbaProject" in ct:
        raise Reject("macros")
    if "wordprocessingml.document.main+xml" not in ct:
        raise Reject("unsupported_format")
    for n in names:
        if n.startswith(_FORBIDDEN_PARTS):
            raise Reject(
                "macros"
                if "vba" in n
                else "embedded_content"
                if "embeddings" in n or "activeX" in n
                else "signed"
                if "xmlsignatures" in n
                else "unsupported_structure"
            )
    nc: dict[str, int] = {}

    def note(cls: str, n: int = 1, reason: str = "no_extraction") -> None:
        if n:
            nc[cls] = nc.get(cls, 0) + n
            reasons[cls] = reason

    reasons: dict[str, str] = {}
    note("images", sum(1 for n in names if n.startswith("word/media/")))
    if any(n.startswith("docProps/thumbnail") for n in names):
        note("thumbnail", 1, "not_analyzed")
    # relationships: external hyperlinks are inventoried (never resolved); any other external target is refused
    rid_target: dict[str, str] = {}
    for n in sorted(names):
        if not n.endswith(".rels"):
            continue
        root = parse_xml(parts[n], lim)
        for rel in root.iter(_q(REL, "Relationship")):
            mode, typ, target = rel.get("TargetMode"), rel.get("Type", ""), rel.get("Target", "")
            if mode == "External":
                if typ.endswith("/hyperlink"):
                    note("hyperlink_targets")
                else:
                    raise Reject("external_reference")
            elif n == "word/_rels/document.xml.rels":
                rid_target[rel.get("Id", "")] = (
                    "word/" + target.lstrip("/") if not target.startswith("word/") else target
                )
    # document properties (descriptive metadata): inventoried and NOT analysed
    for pn in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"):
        if pn in names:
            root = parse_xml(parts[pn], lim)
            cnt = sum(
                1 for el in root.iter() if isinstance(el.tag, str) and el.text and el.text.strip() and len(el) == 0
            )
            note("document_properties", cnt, "not_analyzed")
    if "word/comments.xml" in names:
        root = parse_xml(parts["word/comments.xml"], lim)
        note("comments", sum(1 for _ in root.iter(_q(W, "comment"))), "not_analyzed")
    fields = {"instr": 0}
    state: dict[str, Any] = {"sections": 0, "alt": 0, "math": 0}

    def scan_forbidden(root: Any) -> None:
        for el in root.iter():
            tag = el.tag
            if not isinstance(tag, str):
                continue
            ns, _, local = tag[1:].partition("}")
            if ns == W:
                if local in _TRACKED:
                    raise Reject("tracked_changes")
                if local in ("vanish", "specVanish"):
                    raise Reject("hidden_text")
                if local in ("txbxContent", "pict"):
                    raise Reject("unsupported_structure")
                if local in ("object", "control", "OLEObject"):
                    raise Reject("embedded_content")
                if local == "altChunk":
                    raise Reject("external_reference")
                if local in ("docVars", "docVar", "dataBinding"):
                    raise Reject("unsupported_structure")
                if local == "instrText":
                    fields["instr"] += 1
                    if _EXTERNAL_FIELDS.search(el.text or ""):
                        raise Reject("external_reference")
                if local == "fldSimple":
                    fields["instr"] += 1
                    if _EXTERNAL_FIELDS.search(el.get(_q(W, "instr"), "")):
                        raise Reject("external_reference")
                if local == "sectPr" and el.getparent() is not None and el.getparent().tag == _q(W, "pPr"):
                    state["sections"] += 1
            elif ns == MC and local == "AlternateContent":
                raise Reject("unsupported_structure")
            elif ns == M and local == "oMath":
                state["math"] += 1
            elif local == "docPr" and (el.get("descr") or el.get("title")):
                state["alt"] += 1
            elif local == "drawing":
                pass

    doc = parse_xml(parts["word/document.xml"], lim)
    scan_forbidden(doc)
    if state["sections"] > 0:
        raise Reject("unsupported_structure")  # more than one section
    body = doc.find(_q(W, "body"))
    if body is None:
        raise Reject("unsupported_structure")
    sect = body.findall(_q(W, "sectPr"))
    refs: dict[str, list[tuple[str, str]]] = {"header": [], "footer": []}
    for s in sect:
        for kind in ("header", "footer"):
            for ref in s.findall(_q(W, f"{kind}Reference")):
                refs[kind].append((ref.get(_q(W, "type"), "default"), rid_target.get(ref.get(_q(R, "id"), ""), "")))
    order = {"default": 0, "first": 1, "even": 2}
    ordered_parts: list[str] = []
    referenced = set()
    for kind in ("header", "footer"):
        for _typ, target in sorted(refs[kind], key=lambda x: order.get(x[0], 9)):
            if not target or target not in names:
                raise Reject("unsupported_structure")
            ordered_parts.append(target)
            referenced.add(target)
    for n in names:
        if re.fullmatch(r"word/(header|footer)\d*\.xml", n) and n not in referenced:
            raise Reject("unsupported_structure")  # orphan header/footer
    units: list[Unit] = []
    tree_cache: dict[str, Any] = {}

    def paragraphs(root: Any, part: str, nest: int = 0) -> list[Any]:
        found: list[Any] = []

        def walk(node: Any, depth: int) -> None:
            for child in node:
                if not isinstance(child.tag, str):
                    continue
                local = child.tag.rpartition("}")[2]
                if local == "p":
                    found.append(child)
                elif local == "tbl":
                    if depth + 1 > lim.max_table_nesting:
                        raise Reject("unsupported_structure")
                    for row in child.findall(_q(W, "tr")):
                        for cell in row.findall(_q(W, "tc")):
                            walk(cell, depth + 1)
                elif local in ("sdt", "sdtContent", "customXml"):
                    sdt_pr = child.find(_q(W, "sdtPr")) if local == "sdt" else None
                    if sdt_pr is not None and any(
                        isinstance(c.tag, str)
                        and c.tag.rpartition("}")[2]
                        in ("dataBinding", "comboBox", "dropDownList", "date", "picture", "docPartObj")
                        for c in sdt_pr
                    ):
                        raise Reject("unsupported_structure")
                    walk(child, depth)

        walk(root, nest)
        return found

    def add_unit(p: Any, part: str, tree: Any) -> None:
        u = Unit(unit_id=f"u{len(units) + 1:04d}", kind="docx_paragraph", ordinal=len(units) + 1)
        for el in p.iter():
            tag = el.tag
            if not isinstance(tag, str):
                continue
            local = tag.rpartition("}")[2]
            ns = tag[1:].partition("}")[0]
            if ns != W:
                continue
            if local == "t":
                text = el.text or ""
                if text:
                    u.add(
                        text,
                        pieces=[
                            {
                                "part_name": part,
                                "node_path": tree.getpath(el),
                                "char_start": 0,
                                "char_end": len(text),
                                "relation": "1:1",
                            }
                        ],
                    )
            elif local in ("tab", "br", "cr", "noBreakHyphen"):
                ch = {"tab": "\t", "br": "\n", "cr": "\n", "noBreakHyphen": "-"}[local]
                u.add(
                    ch,
                    pieces=[
                        {
                            "part_name": part,
                            "node_path": tree.getpath(el),
                            "char_start": 0,
                            "char_end": 1,
                            "relation": "1:1",
                        }
                    ],
                )
        units.append(u)

    def do_part(part: str, root: Any) -> None:
        tree = etree.ElementTree(root)
        tree_cache[part] = tree
        for p in paragraphs(root if part != "word/document.xml" else body, part):
            add_unit(p, part, tree)

    do_part("word/document.xml", doc)
    for part in ordered_parts:
        root = parse_xml(parts[part], lim)
        scan_forbidden(root)
        do_part(part, root)
    for note_part, tag in (("word/footnotes.xml", "footnote"), ("word/endnotes.xml", "endnote")):
        if note_part not in names:
            continue
        root = parse_xml(parts[note_part], lim)
        scan_forbidden(root)
        tree = etree.ElementTree(root)
        notes = sorted(root.findall(_q(W, tag)), key=lambda n: int(n.get(_q(W, "id"), "0")))
        for note_el in notes:
            if note_el.get(_q(W, "type")) in ("separator", "continuationSeparator", "continuationNotice"):
                if any((t.text or "").strip() for t in note_el.iter(_q(W, "t"))):
                    raise Reject("unsupported_structure")
                continue
            for p in paragraphs(note_el, note_part):
                add_unit(p, note_part, tree)
    note("field_instructions", fields["instr"])
    note("alt_text", state["alt"])
    note("equations", state["math"])
    declared = [{"element_class": k, "count": v, "reason": reasons[k]} for k, v in sorted(nc.items())]
    return {"format": "docx", "units": [u.finish() for u in units], "declared_not_covered": declared}
