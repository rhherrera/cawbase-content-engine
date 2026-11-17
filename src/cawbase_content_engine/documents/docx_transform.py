"""A07 transformation of DOCX packages, run ONLY inside the isolated extraction child (docs/docx-protection.md).

* ``plan`` / ``protect``: selection rules R1-R5 (section 4.3), field results (4.4), substitution in the first
  affected run (4.5) and mandatory removal of the authorized ``E`` classes (section 5).
* ``restore``: replaces each recorded marker occurrence by its original, in the same ``w:t`` (section 7.3).

Untrusted input: the package is read through the hardened readers of ``docx`` (bounded ZIP, no DTD, no entities,
no network). Nothing here talks to the database, the storage or the key provider.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from typing import Any

from . import docx
from .common import Limits, Reject

W = docx.W
REL_NS = docx.REL
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

TRANSFORMER_ID = "cce-docx-transform"
TRANSFORMER_VERSION = "1"

COMMENT_PARTS = (
    "word/comments.xml",
    "word/commentsExtended.xml",
    "word/commentsIds.xml",
    "word/commentsExtensible.xml",
)
PEOPLE_PART = "word/people.xml"
_COMMENT_REL_TYPES = ("/comments", "/commentsExtended", "/commentsIds", "/commentsExtensible", "/people")
_ANCHORS = ("commentRangeStart", "commentRangeEnd", "commentReference")
CORE_KEEP = {"created", "modified", "lastPrinted", "revision", "language"}
APP_KEEP = {
    "Application",
    "AppVersion",
    "DocSecurity",
    "ScaleCrop",
    "LinksUpToDate",
    "SharedDoc",
    "HyperlinksChanged",
    "TotalTime",
    "Pages",
    "Words",
    "Characters",
    "CharactersWithSpaces",
    "Lines",
    "Paragraphs",
}
_RUN_OK = {"rPr", "t", "lastRenderedPageBreak"}
_BETWEEN_OK = {"proofErr", "commentRangeStart", "commentRangeEnd"}
_BOUNDARY = {"bookmarkStart", "bookmarkEnd", "permStart", "permEnd"}


class Failure(Exception):
    """A closed job failure code (docs 8.5); never carries content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _local(el: Any) -> str:
    return el.tag.rpartition("}")[2] if isinstance(el.tag, str) else ""


def _ns(el: Any) -> str:
    return el.tag[1:].partition("}")[0] if isinstance(el.tag, str) and el.tag.startswith("{") else ""


def _is_w(el: Any, name: str) -> bool:
    return _ns(el) == W and _local(el) == name


# ---- package ------------------------------------------------------------------------------------
class Package:
    """Raw entries (to copy unchanged) + lazily parsed XML parts (hardened)."""

    def __init__(self, data: bytes, lim: Limits) -> None:
        self.lim = lim
        docx.read_parts(data, lim)  # limits, names, decompression (declared and produced)
        zf = zipfile.ZipFile(io.BytesIO(data))
        self.order = [i.filename for i in zf.infolist() if not i.is_dir()]
        self.raw: dict[str, bytes] = {n: zf.read(n) for n in self.order}
        self.trees: dict[str, Any] = {}
        self.removed: set[str] = set()
        self.dirty: set[str] = set()

    def root(self, name: str, *, write: bool = False) -> Any:
        if write:
            self.dirty.add(name)
        if name not in self.trees:
            from lxml import etree

            self.trees[name] = etree.ElementTree(docx.parse_xml(self.raw[name], self.lim))
        return self.trees[name].getroot()

    def tree(self, name: str) -> Any:
        self.root(name)
        return self.trees[name]

    def remove(self, name: str) -> None:
        self.removed.add(name)
        self.trees.pop(name, None)

    def names(self) -> list[str]:
        return [n for n in self.order if n not in self.removed]

    def build(self) -> bytes:
        from lxml import etree

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in self.names():
                if name in self.dirty and name in self.trees:  # untouched parts are copied byte for byte
                    tree = self.trees[name]
                    body = etree.tostring(
                        tree,
                        xml_declaration=True,
                        encoding="UTF-8",
                        standalone=tree.docinfo.standalone,
                    )
                else:
                    body = self.raw[name]
                zf.writestr(name, body)
        return out.getvalue()


def _rels_name(part: str) -> str:
    d, _, f = part.rpartition("/")
    return f"{d}/_rels/{f}.rels" if d else f"_rels/{f}.rels"


def _target(part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    base = part.rpartition("/")[0]
    return posixpath.normpath(posixpath.join(base, target)) if base else posixpath.normpath(target)


def _owner_of_rels(rels: str) -> str:
    d, _, f = rels.rpartition("/")
    owner_dir = d.rpartition("/")[0] if d.endswith("_rels") else d
    name = f[: -len(".rels")]
    return f"{owner_dir}/{name}" if owner_dir else name


# ---- fields and paths ----------------------------------------------------------------------------
def field_content(root: Any) -> set[int]:
    """ids of every ``w:t`` inside a field (simple field descendants, or any ``w:t`` met while the stack of
    complex fields is not empty), in document order of the WHOLE part: a field may cross paragraphs."""
    out: set[int] = set()
    stack: list[bool] = []
    for el in root.iter():
        if _is_w(el, "fldChar"):
            kind = el.get(f"{{{W}}}fldCharType")
            if kind == "begin":
                stack.append(False)
            elif kind == "separate" and stack:
                stack[-1] = True
            elif kind == "end":
                if not stack:
                    raise Failure("unsupported_structure")
                stack.pop()
        elif _is_w(el, "t") and stack:
            out.add(id(el))
    if stack:
        raise Failure("unsupported_structure")
    for simple in root.iter(f"{{{W}}}fldSimple"):
        for t in simple.iter(f"{{{W}}}t"):
            out.add(id(t))
    return out


def _sdt_ok(sdt: Any) -> bool:
    """A plain-text content control (no type, or ``w:text``), not showing its placeholder, content not locked."""
    pr = sdt.find(f"{{{W}}}sdtPr")
    if pr is None:
        return True
    for child in pr:
        name = _local(child)
        if _ns(child) != W:
            return False  # w14 checkbox and other extensions
        if name in (
            "richText",
            "comboBox",
            "dropDownList",
            "date",
            "picture",
            "docPartObj",
            "docPartList",
            "citation",
            "bibliography",
            "group",
            "equation",
            "showingPlcHdr",
            "dataBinding",
        ):
            return False
        if name == "lock" and child.get(f"{{{W}}}val") in ("sdtContentLocked", "contentLocked"):
            return False
    return True


class Doc:
    """The original (or derivative) with path -> node maps of the parts that hold units."""

    def __init__(self, pkg: Package) -> None:
        self.pkg = pkg
        self.by_path: dict[str, dict[str, Any]] = {}
        self.fields: dict[str, set[int]] = {}

    def node(self, part: str, path: str) -> Any:
        if part not in self.by_path:
            tree = self.pkg.tree(part)
            self.by_path[part] = {tree.getpath(el): el for el in tree.getroot().iter()}
            self.fields[part] = field_content(tree.getroot())
        el = self.by_path[part].get(path)
        if el is None:
            raise Failure("analysis_source_stale")
        return el


# ---- selections ----------------------------------------------------------------------------------
def _pieces(unit: dict[str, Any], start: int, end: int) -> list[tuple[dict[str, Any], int, int]]:
    """(piece, c0, c1) of every segment touched by [start, end), in order; offsets relative to the node."""
    out = []
    for seg in unit["segments"]:
        us, ue = seg["unit_start"], seg["unit_end"]
        if ue <= start or us >= end:
            continue
        if seg["kind"] != "original" or len(seg["pieces"]) != 1:
            raise Failure("selection_not_transformable")
        piece = seg["pieces"][0]
        c0 = piece["char_start"] + max(start, us) - us
        c1 = piece["char_start"] + min(end, ue) - us
        out.append((piece, c0, c1))
    return out


def _parts_of(unit: dict[str, Any], start: int, end: int) -> set[str]:
    return {piece["part_name"] for piece, _a, _b in _pieces(unit, start, end)}


def check_selection(doc: Doc, unit: dict[str, Any], start: int, end: int) -> list[tuple[Any, int, int]]:
    """R1-R5 of docs 4.3; returns [(w:t element, c0, c1)]. Fields first: a field result wins over other codes."""
    pieces = _pieces(unit, start, end)
    if not pieces:
        raise Failure("analysis_source_invalid")
    nodes = []
    for piece, c0, c1 in pieces:
        el = doc.node(piece["part_name"], piece["node_path"])
        nodes.append((piece["part_name"], el, c0, c1))
    for part, el, _c0, _c1 in nodes:  # R5
        if id(el) in doc.fields[part]:
            raise Failure("field_result_not_transformable")
    for _part, el, _c0, _c1 in nodes:  # R1: only w:t pieces
        if not _is_w(el, "t"):
            raise Failure("selection_not_transformable")
    runs: list[Any] = []
    for _part, el, _c0, _c1 in nodes:
        run = el.getparent()
        if run is None or not _is_w(run, "r"):
            raise Failure("selection_not_transformable")
        if not runs or runs[-1] is not run:
            runs.append(run)
    parent = runs[0].getparent()
    for run in runs:  # R4: one container
        if run.getparent() is not parent:
            raise Failure("selection_crosses_boundary")
    _check_container(parent)
    siblings = list(parent)
    first, last = siblings.index(runs[0]), siblings.index(runs[-1])
    for sib in siblings[first : last + 1]:  # R2 + R3
        name = _local(sib)
        if _ns(sib) == W and name == "r":
            for child in sib:
                if _ns(child) != W or _local(child) not in _RUN_OK:
                    raise Failure("selection_not_transformable")
        elif _ns(sib) == W and name in _BETWEEN_OK:
            continue
        elif _ns(sib) == W and name in _BOUNDARY:
            raise Failure("selection_crosses_boundary")
        elif not isinstance(sib.tag, str):
            continue  # XML comment / processing instruction between runs: no content of the selection
        else:
            raise Failure("selection_not_transformable")
    return [(el, c0, c1) for _part, el, c0, c1 in nodes]


def _check_container(parent: Any) -> None:
    """The run's parent is a paragraph, a hyperlink in a paragraph, or the content of a plain-text control; every
    enclosing content control must be plain text. Any other inline wrapper is not transformable."""
    if _is_w(parent, "fldSimple"):
        raise Failure("field_result_not_transformable")
    if not (_is_w(parent, "p") or _is_w(parent, "hyperlink") or _is_w(parent, "sdtContent")):
        raise Failure("selection_not_transformable")
    if _is_w(parent, "hyperlink") and not _is_w(parent.getparent(), "p"):
        raise Failure("selection_not_transformable")
    if _is_w(parent, "sdtContent") and not _is_w(parent.getparent().getparent(), "p"):
        raise Failure("selection_not_transformable")
    anc = parent
    while anc is not None:
        if _is_w(anc, "sdt") and not _sdt_ok(anc):
            raise Failure("selection_not_transformable")
        anc = anc.getparent()


def _space(el: Any) -> None:
    text = el.text or ""
    if text and (text[0].isspace() or text[-1].isspace()):
        el.set(XML_SPACE, "preserve")


def _drop_text(el: Any) -> None:
    run = el.getparent()
    run.remove(el)
    if all(_local(c) == "rPr" for c in run):
        run.getparent().remove(run)


def apply_selection(nodes: list[tuple[Any, int, int]], substitute: str) -> None:
    """Docs 4.5: the substitute goes whole into the first affected ``w:t`` (first run's format)."""
    (first, a0, a1) = nodes[0]
    text = first.text or ""
    if len(nodes) == 1:
        first.text = text[:a0] + substitute + text[a1:]
    else:
        first.text = text[:a0] + substitute
    _space(first)
    for el, _c0, _c1 in nodes[1:-1]:
        _drop_text(el)
    if len(nodes) > 1:
        last, _l0, l1 = nodes[-1]
        rest = (last.text or "")[l1:]
        if rest:
            last.text = rest
            _space(last)
        else:
            _drop_text(last)


# ---- E classes -----------------------------------------------------------------------------------
def _rels_root(pkg: Package, rels: str) -> Any:
    return pkg.root(rels, write=True) if rels in pkg.raw and rels not in pkg.removed else None


def _drop_override(pkg: Package, part: str) -> None:
    ct = pkg.root("[Content_Types].xml", write=True)
    for ov in list(ct):
        if _local(ov) == "Override" and ov.get("PartName", "").lstrip("/") == part:
            ct.remove(ov)


def _drop_default_if_unused(pkg: Package, ext: str) -> None:
    if any(n.lower().endswith("." + ext.lower()) for n in pkg.names()):
        return
    ct = pkg.root("[Content_Types].xml", write=True)
    for d in list(ct):
        if _local(d) == "Default" and d.get("Extension", "").lower() == ext.lower():
            ct.remove(d)


def _remove_part(pkg: Package, part: str) -> None:
    pkg.remove(part)
    _drop_override(pkg, part)
    rels = _rels_name(part)
    if rels in pkg.raw:
        pkg.remove(rels)


def _referenced(pkg: Package) -> set[str]:
    out: set[str] = set()
    for name in pkg.names():
        if not name.endswith(".rels"):
            continue
        owner = _owner_of_rels(name)
        for rel in pkg.root(name):
            if rel.get("TargetMode") == "External":
                continue
            out.add(_target(owner, rel.get("Target", "")))
    return out


def remove_e_classes(pkg: Package) -> list[dict[str, Any]]:
    """Docs 5: mandatory removal; returns the classes effectively removed with counts (never content)."""
    removed: list[dict[str, Any]] = []
    names = set(pkg.names())
    unknown = [
        n
        for n in names
        if re.fullmatch(r"word/(comments\w*|people)\.xml", n) and n not in (*COMMENT_PARTS, PEOPLE_PART)
    ]
    if unknown:
        raise Failure("unsupported_structure")
    text_parts = [
        n
        for n in names
        if n == "word/document.xml"
        or re.fullmatch(r"word/(header|footer)\d*\.xml", n)
        or n in ("word/footnotes.xml", "word/endnotes.xml")
    ]
    comment_ids: set[str] = set()
    n_comments = 0
    if "word/comments.xml" in names:
        for c in pkg.root("word/comments.xml").iter(f"{{{W}}}comment"):
            n_comments += 1
            comment_ids.add(c.get(f"{{{W}}}id", ""))
    anchors = 0
    for part in text_parts:
        root = pkg.root(part)
        found = [el for el in root.iter() if _ns(el) == W and _local(el) in _ANCHORS]
        for el in found:
            if el.get(f"{{{W}}}id", "") not in comment_ids:
                raise Failure("unsupported_structure")  # PC-01: an anchor without its comment
            parent = el.getparent()
            parent.remove(el)
            pkg.dirty.add(part)
            anchors += 1
            if _is_w(parent, "r") and all(_local(c) == "rPr" for c in parent):
                parent.getparent().remove(parent)
    present = [p for p in COMMENT_PARTS if p in names]
    if present or anchors:
        doc_rels = _rels_root(pkg, "word/_rels/document.xml.rels")
        candidates: set[str] = set()
        for p in present:
            r = _rels_name(p)
            if r in names:
                for rel in pkg.root(r):
                    if rel.get("TargetMode") != "External":
                        candidates.add(_target(p, rel.get("Target", "")))
        if doc_rels is not None:
            for rel in list(doc_rels):
                if rel.get("Type", "").endswith(_COMMENT_REL_TYPES[:4]):
                    doc_rels.remove(rel)
        for p in present:
            _remove_part(pkg, p)
        still = _referenced(pkg)
        for cand in sorted(candidates):  # parts reachable ONLY from comments go with them
            if cand in pkg.raw and cand not in still and cand not in pkg.removed:
                _remove_part(pkg, cand)
                _drop_default_if_unused(pkg, cand.rpartition(".")[2])
        removed.append({"element_class": "comments", "count": n_comments})
    if PEOPLE_PART in names:
        doc_rels = _rels_root(pkg, "word/_rels/document.xml.rels")
        if doc_rels is not None:
            for rel in list(doc_rels):
                if rel.get("Type", "").endswith("/people"):
                    doc_rels.remove(rel)
        people = sum(1 for _ in pkg.root(PEOPLE_PART))
        _remove_part(pkg, PEOPLE_PART)
        removed.append({"element_class": "comment_people", "count": people})
    pkg_rels = _rels_root(pkg, "_rels/.rels")
    if "docProps/core.xml" in names:
        root = pkg.root("docProps/core.xml")
        gone = [el for el in list(root) if isinstance(el.tag, str) and _local(el) not in CORE_KEEP]
        for el in gone:
            root.remove(el)
        if gone:
            pkg.dirty.add("docProps/core.xml")
            removed.append({"element_class": "core_descriptive_properties", "count": len(gone)})
    if "docProps/app.xml" in names:
        root = pkg.root("docProps/app.xml")
        gone = [el for el in list(root) if isinstance(el.tag, str) and _local(el) not in APP_KEEP]
        for el in gone:
            root.remove(el)
        if gone:
            pkg.dirty.add("docProps/app.xml")
            removed.append({"element_class": "app_descriptive_properties", "count": len(gone)})
    if "docProps/custom.xml" in names:
        count = sum(1 for el in pkg.root("docProps/custom.xml") if isinstance(el.tag, str))
        if pkg_rels is not None:
            for rel in list(pkg_rels):
                if rel.get("Type", "").endswith("/custom-properties"):
                    pkg_rels.remove(rel)
        _remove_part(pkg, "docProps/custom.xml")
        removed.append({"element_class": "custom_properties", "count": count})
    thumbs = [n for n in names if n.startswith("docProps/thumbnail")]
    if thumbs:
        if pkg_rels is not None:
            for rel in list(pkg_rels):
                if rel.get("Type", "").endswith("/thumbnail"):
                    pkg_rels.remove(rel)
        for n in thumbs:
            _remove_part(pkg, n)
            _drop_default_if_unused(pkg, n.rpartition(".")[2])
        removed.append({"element_class": "thumbnail", "count": len(thumbs)})
    return removed


# ---- entry points --------------------------------------------------------------------------------
def protect(data: bytes, lim: Limits, selections: list[dict[str, Any]], *, check_only: bool) -> dict[str, Any]:
    try:
        extracted = docx.extract(data, lim)
        units = {u["unit_id"]: u for u in extracted["units"]}
        pkg = Package(data, lim)
        doc = Doc(pkg)
        planned = []
        for sel in selections:
            unit = units.get(sel["unit_id"])
            if unit is None:
                raise Failure("analysis_source_invalid")
            planned.append((sel, check_selection(doc, unit, int(sel["start"]), int(sel["end"]))))
        if check_only:
            remove_e_classes(pkg)  # PC-01..03 are checked before any mapping is created
            return {"ok": True}
        for sel, nodes in reversed(planned):
            apply_selection(nodes, sel["substitute"])
            pkg.dirty.update(_parts_of(units[sel["unit_id"]], int(sel["start"]), int(sel["end"])))
        removed = remove_e_classes(pkg)
        out = pkg.build()
    except Reject as r:
        return {"failure": "file_rejected", "reason": r.reason}
    except Failure as f:
        return {"failure": f.code}
    if len(out) > lim.max_file_bytes:
        return {"failure": "limit_exceeded"}
    import base64

    return {"derivative": base64.b64encode(out).decode("ascii"), "removed": removed}


def restore(data: bytes, lim: Limits, occurrences: list[dict[str, Any]]) -> dict[str, Any]:
    """Each occurrence: part_name, node_path, char_start, char_end, marker, value. One pass per node, right to
    left; the inserted value is never re-examined (docs 7.3)."""
    try:
        docx.extract(data, lim)  # the same validation the protection passed
        pkg = Package(data, lim)
        doc = Doc(pkg)
        by_node: dict[int, tuple[Any, list[dict[str, Any]]]] = {}
        for occ in occurrences:
            el = doc.node(occ["part_name"], occ["node_path"])
            if not _is_w(el, "t"):
                raise Failure("derivative_markers_invalid")
            by_node.setdefault(id(el), (el, []))[1].append(occ)
            pkg.dirty.add(occ["part_name"])
        for el, occs in by_node.values():
            text = el.text or ""
            for occ in sorted(occs, key=lambda o: -int(o["char_start"])):
                a, b = int(occ["char_start"]), int(occ["char_end"])
                if text[a:b] != occ["marker"]:
                    raise Failure("derivative_markers_invalid")
                text = text[:a] + occ["value"] + text[b:]
            el.text = text
            _space(el)
        out = pkg.build()
    except Reject as r:
        return {"failure": "file_rejected", "reason": r.reason}
    except Failure as f:
        return {"failure": f.code}
    if len(out) > lim.max_file_bytes:
        return {"failure": "limit_exceeded"}
    import base64

    return {"restored": base64.b64encode(out).decode("ascii")}
