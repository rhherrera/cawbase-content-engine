"""Independent verification helpers (stdlib only for DOCX: zipfile + xml.etree; no lxml, no python-docx, no transformer code)."""

from __future__ import annotations

import io
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from urllib.parse import quote

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def variants(value: str) -> dict[str, bytes]:
    """Byte encodings of a value used for the raw scan (literal search is NOT sufficient on its own)."""
    out = {}
    for form in ("NFC", "NFD"):
        v = unicodedata.normalize(form, value)
        out[f"utf8-{form}"] = v.encode("utf-8")
        out[f"utf16le-{form}"] = v.encode("utf-16-le")
        out[f"utf16be-{form}"] = v.encode("utf-16-be")
        out[f"pct-{form}"] = quote(v).encode("ascii")
    out["xmlcharref"] = "".join(f"&#{ord(c)};" for c in value).encode("ascii")
    out["xmlcharref-hex"] = "".join(f"&#x{ord(c):x};" for c in value).encode("ascii")
    return out


def _strings_of_xml(data: bytes) -> list[str]:
    """Every logical string of an XML part: text nodes, attribute values, comments/PIs, and paragraph-joined text."""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    root = ET.fromstring(data, parser=parser)
    out: list[str] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            out.append(el.text or "")
            continue
        if el.text and el.text.strip():
            out.append(el.text)
        if el.tail and el.tail.strip():
            out.append(el.tail)
        out += [v for v in el.attrib.values()]
    for p in root.iter(W + "p"):
        out.append("".join((t.text or "") for t in p.iter(W + "t")))
    return out


def scan_docx(parts: dict[str, bytes], values: Iterable[str]) -> list[dict]:
    """Return residue findings: {"value","part","how"} for each place a value (any encoding) remains."""
    finds = []
    for value in values:
        vs = variants(value)
        for name, data in parts.items():
            for how, needle in vs.items():
                if needle and needle in data:
                    finds.append({"value": value, "part": name, "how": f"raw:{how}"})
                    break
            if name.endswith((".xml", ".rels")):
                try:
                    strings = _strings_of_xml(data)
                except ET.ParseError:
                    finds.append({"value": value, "part": name, "how": "unparseable-xml"})
                    continue
                nv = {unicodedata.normalize(f, value) for f in ("NFC", "NFD")}
                for s in strings:
                    if any(v in unicodedata.normalize(f, s) for v in nv for f in ("NFC", "NFD")):
                        finds.append({"value": value, "part": name, "how": "xml-string"})
                        break
        for name in parts:
            if value in name:
                finds.append({"value": value, "part": name, "how": "part-name"})
    # de-duplicate
    seen, out = set(), []
    for f in finds:
        k = (f["value"], f["part"], f["how"].split(":")[0])
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def docx_paragraph_texts(parts: dict[str, bytes], order: list[str]) -> list[tuple[str, str]]:
    """Independent paragraph text extraction (w:t/tab/br only). Mirrors NO transformer logic."""
    res = []
    for name in order:
        if name not in parts:
            continue
        root = ET.fromstring(parts[name])
        for p in root.iter(W + "p"):
            s = ""
            for el in p.iter():
                if el.tag == W + "t":
                    s += el.text or ""
                elif el.tag == W + "tab":
                    s += "\t"
                elif el.tag in (W + "br", W + "cr"):
                    s += "\n"
                elif el.tag == W + "noBreakHyphen":
                    s += "‑"
            if s:
                res.append((name, s))
    return res


def docx_skeleton(data: bytes) -> list[tuple]:
    """Structure/style skeleton of an XML part: tags + attributes (xml:space excluded), text ignored."""
    root = ET.fromstring(data)
    sk = []
    for el in root.iter():
        if isinstance(el.tag, str):
            sk.append((el.tag, tuple(sorted((k, v) for k, v in el.attrib.items() if not k.endswith("}space")))))
    return sk


def open_check(data: bytes) -> dict:
    """Open without repair: zip integrity, every XML parses, relationships resolve, every part has a content type."""
    z = zipfile.ZipFile(io.BytesIO(data))
    res = {"zip_testzip": z.testzip(), "parts": len(z.namelist())}
    names = set(z.namelist())
    ct = ET.fromstring(z.read("[Content_Types].xml"))
    defaults = {e.get("Extension").lower() for e in ct if e.tag.endswith("Default")}
    overrides = {e.get("PartName").lstrip("/") for e in ct if e.tag.endswith("Override")}
    problems = []
    for n in names:
        if n.endswith((".xml", ".rels")):
            try:
                ET.fromstring(z.read(n))
            except ET.ParseError:
                problems.append(f"xml:{n}")
        if n != "[Content_Types].xml" and n not in overrides and n.rsplit(".", 1)[-1].lower() not in defaults:
            problems.append(f"no-content-type:{n}")
    for o in overrides:
        if o not in names:
            problems.append(f"override-without-part:{o}")
    import posixpath

    for n in [n for n in names if n.endswith(".rels")]:
        base = posixpath.dirname(posixpath.dirname(n))
        for r in ET.fromstring(z.read(n)):
            if r.get("TargetMode") == "External":
                continue
            t = r.get("Target")
            full = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join(base, t))
            if full not in names:
                problems.append(f"dangling-rel:{n}->{t}")
    res["problems"] = problems
    res["ok"] = not problems and res["zip_testzip"] is None
    return res
