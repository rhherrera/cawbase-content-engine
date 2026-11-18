"""Independent verifier of A07 derivatives (docs/docx-protection.md, section 6 and 7.4).

Runs in its OWN isolated child and uses only the standard library (``zipfile``, ``xml.etree.ElementTree``,
``xml.parsers.expat``): a parser different from the transformer's (``lxml``). It never trusts what the transformer
says it did: it re-reads both packages, extracts the text on its own, compares it with the oracle by position,
compares the structure and scans the WHOLE package for residues, attributing every match (section 6.3.1).
Its report never contains values, positions or part names.
"""

from __future__ import annotations

import io
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET  # noqa: S405 - DOCTYPE/entities are refused before parsing
import zipfile
from typing import Any
from xml.parsers import expat  # noqa: S408 - same guard

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REMOVABLE = re.compile(
    r"(word/(comments|commentsExtended|commentsIds|commentsExtensible|people)\.xml"
    r"|word/_rels/(comments|commentsExtended|commentsIds|commentsExtensible|people)\.xml\.rels"
    r"|docProps/custom\.xml|docProps/thumbnail\.\w+)"
)
_EDITABLE = re.compile(
    r"(word/document\.xml|word/(header|footer)\d*\.xml|word/(footnotes|endnotes)\.xml"
    r"|docProps/(core|app)\.xml|\[Content_Types\]\.xml|.*\.rels)"
)
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
MODES = ("exact", "nfc", "nfd", "cf_exact", "cf_nfc", "cf_nfd", "ws")
RAW_MODES = ("exact", "nfc", "nfd")
ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")


class Fail(Exception):
    def __init__(self, code: str, check: str) -> None:
        super().__init__(code)
        self.code, self.check = code, check


def _local(tag: Any) -> str:
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def _nsof(tag: Any) -> str:
    return tag[1:].partition("}")[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _is_w(el: Any, name: str) -> bool:
    return _nsof(el.tag) == W and _local(el.tag) == name


# ---- package -------------------------------------------------------------------------------------
class Pkg:
    def __init__(self, data: bytes) -> None:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            infos = [i for i in zf.infolist() if not i.is_dir()]
            names = [i.filename for i in infos]
            if len(set(n.lower() for n in names)) != len(names):
                raise Fail("derivative_verification_failed", "package")
            for n in names:
                if n.startswith("/") or ".." in n.split("/") or "\\" in n:
                    raise Fail("derivative_verification_failed", "package")
            self.raw = {n: zf.read(n) for n in names}
        except (zipfile.BadZipFile, OSError, KeyError):
            raise Fail("derivative_verification_failed", "package") from None
        self._trees: dict[str, Any] = {}

    def xml_names(self) -> list[str]:
        return [n for n in self.raw if n.endswith((".xml", ".rels"))]

    def tree(self, name: str) -> Any:
        if name not in self._trees:
            raw = self.raw[name]
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise Fail("derivative_verification_failed", "package")
            try:
                parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))  # noqa: S314
                parser.feed(raw)
                self._trees[name] = parser.close()
            except ET.ParseError:
                raise Fail("derivative_verification_failed", "package") from None
        return self._trees[name]


def _rels_of(part: str) -> str:
    d, _, f = part.rpartition("/")
    return f"{d}/_rels/{f}.rels" if d else f"_rels/{f}.rels"


def _target(owner: str, target: str) -> str:
    import posixpath

    if target.startswith("/"):
        return target.lstrip("/")
    base = owner.rpartition("/")[0]
    return posixpath.normpath(posixpath.join(base, target)) if base else posixpath.normpath(target)


def structural(pkg: Pkg, base: Pkg | None = None) -> None:
    """V1-V4: every XML part parses; content types cover every part; internal relationships resolve; every part
    is reachable from the package relationships."""
    for n in pkg.xml_names():
        pkg.tree(n)
    if "[Content_Types].xml" not in pkg.raw or "_rels/.rels" not in pkg.raw:
        raise Fail("derivative_verification_failed", "package")
    ct = pkg.tree("[Content_Types].xml")
    defaults = {d.get("Extension", "").lower() for d in ct if _local(d.tag) == "Default"}
    overrides = {o.get("PartName", "").lstrip("/") for o in ct if _local(o.tag) == "Override"}
    inherited: set[str] = set()
    if base is not None and "[Content_Types].xml" in base.raw:  # a dangling override the original already had
        inherited = {
            o.get("PartName", "").lstrip("/")
            for o in base.tree("[Content_Types].xml")
            if _local(o.tag) == "Override" and o.get("PartName", "").lstrip("/") not in base.raw
        }
    for o in overrides:
        if o not in pkg.raw and o not in inherited:
            raise Fail("derivative_verification_failed", "content_types")
    for n in pkg.raw:
        if n == "[Content_Types].xml" or n.endswith(".rels"):
            continue
        if n not in overrides and n.rpartition(".")[2].lower() not in defaults:
            raise Fail("derivative_verification_failed", "content_types")
    reach: set[str] = set()
    todo = [("", "_rels/.rels")]
    while todo:
        owner, rels = todo.pop()
        if rels not in pkg.raw:
            continue
        for rel in pkg.tree(rels):
            if rel.get("TargetMode") == "External":
                continue
            tgt = _target(owner, rel.get("Target", ""))
            if tgt not in pkg.raw:
                raise Fail("derivative_verification_failed", "relationships")
            if tgt not in reach:
                reach.add(tgt)
                todo.append((tgt, _rels_of(tgt)))
    for n in pkg.raw:
        if n == "[Content_Types].xml" or n.endswith(".rels"):
            continue
        if n not in reach:
            raise Fail("derivative_verification_failed", "relationships")


def e_absent(pkg: Pkg) -> None:
    """V6: the authorized E classes are absent (parts, relationships, anchors, property elements)."""
    for n in pkg.raw:
        if _REMOVABLE.fullmatch(n):
            raise Fail("derivative_verification_failed", "e_classes")
    for n in pkg.xml_names():
        root = pkg.tree(n)
        for el in root.iter():
            if _local(el.tag) in ("commentRangeStart", "commentRangeEnd", "commentReference"):
                raise Fail("derivative_verification_failed", "e_classes")
        if n.endswith(".rels"):
            for rel in root:
                if rel.get("Type", "").endswith(
                    ("/comments", "/commentsExtended", "/commentsIds", "/commentsExtensible", "/people", "/thumbnail")
                ) or rel.get("Type", "").endswith("/custom-properties"):
                    raise Fail("derivative_verification_failed", "e_classes")
    for name, keep in (("docProps/core.xml", CORE_KEEP), ("docProps/app.xml", APP_KEEP)):
        if name in pkg.raw:
            for el in pkg.tree(name):
                if isinstance(el.tag, str) and _local(el.tag) not in keep:
                    raise Fail("derivative_verification_failed", "e_classes")


# ---- independent extraction ----------------------------------------------------------------------
class Unit:
    def __init__(self, part: str) -> None:
        self.part = part
        self.text: list[str] = []
        self.where: list[tuple[int, int] | None] = []  # per char: (element ordinal, index in that element) or None


def _ordinals(root: Any) -> dict[int, int]:
    """Document-order ordinal of every ELEMENT (comments/PIs excluded, as expat sees them)."""
    out: dict[int, int] = {}
    i = 0
    for el in root.iter():
        if isinstance(el.tag, str):
            out[id(el)] = i
            i += 1
    return out


def _paragraph(p: Any, part: str, ords: dict[int, int]) -> Unit:
    u = Unit(part)
    for el in p.iter():
        if _nsof(el.tag) != W:
            continue
        local = _local(el.tag)
        if local == "t":
            for k, ch in enumerate(el.text or ""):
                u.text.append(ch)
                u.where.append((ords[id(el)], k))
        elif local in ("tab", "br", "cr", "noBreakHyphen"):
            u.text.append({"tab": "\t", "br": "\n", "cr": "\n", "noBreakHyphen": "-"}[local])
            u.where.append(None)
    return u


def _walk(node: Any) -> list[Any]:
    out: list[Any] = []
    for child in node:
        if not isinstance(child.tag, str):
            continue
        local = _local(child.tag)
        if local == "p":
            out.append(child)
        elif local == "tbl":
            for tr in child.findall(f"{{{W}}}tr"):
                for tc in tr.findall(f"{{{W}}}tc"):
                    out.extend(_walk(tc))
        elif local in ("sdt", "sdtContent", "customXml"):
            out.extend(_walk(child))
    return out


def extract(pkg: Pkg) -> tuple[list[Unit], dict[str, dict[int, int]]]:
    ords: dict[str, dict[int, int]] = {}
    units: list[Unit] = []
    doc = pkg.tree("word/document.xml")
    ords["word/document.xml"] = _ordinals(doc)
    body = doc.find(f"{{{W}}}body")
    if body is None:
        raise Fail("derivative_verification_failed", "oracle")
    units.extend(_paragraph(p, "word/document.xml", ords["word/document.xml"]) for p in _walk(body))
    rid: dict[str, str] = {}
    if "word/_rels/document.xml.rels" in pkg.raw:
        for rel in pkg.tree("word/_rels/document.xml.rels"):
            t = rel.get("Target", "")
            rid[rel.get("Id", "")] = "word/" + t.lstrip("/") if not t.startswith("word/") else t
    order = {"default": 0, "first": 1, "even": 2}
    for kind in ("header", "footer"):
        refs = []
        for s in body.findall(f"{{{W}}}sectPr"):
            for ref in s.findall(f"{{{W}}}{kind}Reference"):
                refs.append((ref.get(f"{{{W}}}type", "default"), rid.get(ref.get(f"{{{R}}}id", ""), "")))
        for _typ, part in sorted(refs, key=lambda x: order.get(x[0], 9)):
            root = pkg.tree(part)
            ords[part] = _ordinals(root)
            units.extend(_paragraph(p, part, ords[part]) for p in _walk(root))
    for part, tag in (("word/footnotes.xml", "footnote"), ("word/endnotes.xml", "endnote")):
        if part not in pkg.raw:
            continue
        root = pkg.tree(part)
        ords[part] = _ordinals(root)
        notes = sorted(root.findall(f"{{{W}}}{tag}"), key=lambda n: int(n.get(f"{{{W}}}id", "0")))
        for n in notes:
            if n.get(f"{{{W}}}type") in ("separator", "continuationSeparator", "continuationNotice"):
                continue
            units.extend(_paragraph(p, part, ords[part]) for p in _walk(n))
    return units, ords


# ---- structure ----------------------------------------------------------------------------------
def _signature(root: Any) -> tuple[list[Any], list[list[Any]]]:
    """(non-run skeleton, per-paragraph run signatures). Text, xml:space, comment anchors and the inside of runs
    are left out of the skeleton; each run is summarised by its properties and the tags of its non-text children."""
    skel: list[Any] = []
    runs: list[list[Any]] = []

    def rpr(run: Any) -> Any:
        pr = run.find(f"{{{W}}}rPr")
        return ET.tostring(pr) if pr is not None else b""

    def visit(el: Any) -> None:
        if not isinstance(el.tag, str):
            return
        local = _local(el.tag)
        if _nsof(el.tag) == W and local in ("commentRangeStart", "commentRangeEnd", "commentReference"):
            return
        if _nsof(el.tag) == W and local == "r":
            if runs:
                runs[-1].append(
                    (
                        rpr(el),
                        tuple(
                            _local(c.tag)
                            for c in el
                            if isinstance(c.tag, str) and _local(c.tag) not in ("t", "rPr", "commentReference")
                        ),
                    )
                )
            return
        attrs = tuple(sorted((k, v) for k, v in el.attrib.items() if not k.endswith("}space")))
        skel.append((el.tag, attrs))
        if _nsof(el.tag) == W and local == "p":
            runs.append([])
        for c in el:
            visit(c)
        if _nsof(el.tag) == W and local == "p":
            skel.append(("/p",))

    visit(root)
    return skel, runs


def _subsequence(small: list[Any], big: list[Any]) -> bool:
    it = iter(big)
    return all(any(x == y for y in it) for x in small)


def structure(orig: Pkg, deriv: Pkg, *, allow_removed: bool) -> None:
    """E1: untouched parts byte for byte; E2: edited parts keep their skeleton (only runs may disappear, in order,
    and only texts / xml:space / comment anchors may change)."""
    for n in deriv.raw:
        if n not in orig.raw:
            raise Fail("derivative_verification_failed", "structure")
    referenced = set()
    for n in orig.xml_names():
        if n.endswith(".rels") and not _REMOVABLE.fullmatch(n):
            owner = n.replace("_rels/", "")[: -len(".rels")]
            for rel in orig.tree(n):
                if rel.get("TargetMode") != "External" and not rel.get("Type", "").endswith(
                    ("/comments", "/commentsExtended", "/commentsIds", "/commentsExtensible", "/people")
                ):
                    referenced.add(_target(owner, rel.get("Target", "")))
    for n, body in orig.raw.items():
        if n not in deriv.raw:
            if not allow_removed or not (_REMOVABLE.fullmatch(n) or n not in referenced):
                raise Fail("derivative_verification_failed", "structure")
            continue
        if _EDITABLE.fullmatch(n):
            if n.endswith(".rels") or n in ("[Content_Types].xml", "docProps/core.xml", "docProps/app.xml"):
                continue  # their allowed changes are checked by V3, V4 and V6
            s1, r1 = _signature(orig.tree(n))
            s2, r2 = _signature(deriv.tree(n))
            if s1 != s2 or len(r1) != len(r2) or not all(_subsequence(b, a) for a, b in zip(r1, r2, strict=True)):
                raise Fail("derivative_verification_failed", "structure")
        elif deriv.raw[n] != body:
            raise Fail("derivative_verification_failed", "structure")


# ---- residues ------------------------------------------------------------------------------------
def normalize(text: str, mode: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalized text + for each normalized char the original span [a, b) of its cluster (base + combining)."""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    if mode == "exact":
        return text, [(i, i + 1) for i in range(len(text))]
    if mode == "ws":
        i = 0
        while i < len(text):
            if text[i].isspace():
                j = i
                while j < len(text) and text[j].isspace():
                    j += 1
                out.append(" ")
                spans.append((i, j))
                i = j
            else:
                out.append(text[i])
                spans.append((i, i + 1))
                i += 1
        return "".join(out), spans
    form = {"nfc": "NFC", "nfd": "NFD", "cf_exact": None, "cf_nfc": "NFC", "cf_nfd": "NFD"}[mode]
    i = 0
    while i < len(text):
        j = i + 1
        while j < len(text) and unicodedata.combining(text[j]):
            j += 1
        cluster = text[i:j]
        if form:
            cluster = unicodedata.normalize(form, cluster)  # type: ignore[arg-type]
        if mode.startswith("cf_"):
            cluster = cluster.casefold()
        for ch in cluster:
            out.append(ch)
            spans.append((i, j))
        i = j
    return "".join(out), spans


def _needles(values: list[str], modes: tuple[str, ...]) -> list[tuple[str, str]]:
    out = []
    for v in values:
        for m in modes:
            n, _ = normalize(v, m)
            if n.strip():
                out.append((m, n))
    return list(dict.fromkeys(out))


def find_spans(text: str, needles: list[tuple[str, str]]) -> set[tuple[int, int]]:
    """Original-coordinate spans of every (overlapping) match of every needle in its own normalization."""
    found: set[tuple[int, int]] = set()
    cache: dict[str, tuple[str, list[tuple[int, int]]]] = {}
    for mode, needle in needles:
        if mode not in cache:
            cache[mode] = normalize(text, mode)
        norm, spans = cache[mode]
        pos = norm.find(needle)
        while pos != -1:
            found.add((spans[pos][0], spans[pos + len(needle) - 1][1]))
            pos = norm.find(needle, pos + 1)
    return found


def _inside(span: tuple[int, int], subs: list[tuple[int, int]]) -> bool:
    return any(a <= span[0] and span[1] <= b for a, b in subs)


def _overlaps(span: tuple[int, int], subs: list[tuple[int, int]]) -> bool:
    return any(span[0] < b and a < span[1] for a, b in subs)


class Residues:
    def __init__(
        self,
        pkg: Pkg,
        units: list[Unit],
        ords: dict[str, dict[int, int]],
        values: list[str],
        subs: list[list[tuple[int, int]]],
    ) -> None:
        self.pkg, self.units, self.ords = pkg, units, ords
        self.needles = _needles(values, MODES)
        self.raw_needles = [(m, n) for m, n in _needles(values, RAW_MODES)]
        self.subs = subs
        # (part, ordinal, k) -> (unit index, offset) for every w:t char of every unit
        self.char_unit: dict[tuple[str, int, int], tuple[int, int]] = {}
        for ui, u in enumerate(units):
            for off, w in enumerate(u.where):
                if w is not None:
                    self.char_unit[(u.part, w[0], w[1])] = (ui, off)
        # exempt spans per unit: allowed occurrences (oracle matches not overlapping a substitute) + substitutes
        self.allowed: list[set[tuple[int, int]]] = []
        for ui, u in enumerate(units):
            text = "".join(u.text)
            spans = find_spans(text, self.needles)
            self.allowed.append({s for s in spans if not _overlaps(s, subs[ui])})
        self.counts = {f"K{i}": 0 for i in range(1, 9)}

    def _unit_span(self, part: str, chars: list[tuple[int, int]]) -> tuple[int, tuple[int, int]] | None:
        mapped = [self.char_unit.get((part, o, k)) for o, k in chars]
        if not mapped or any(m is None for m in mapped):
            return None
        ui = mapped[0][0]  # type: ignore[index]
        offs = [m[1] for m in mapped]  # type: ignore[index]
        if any(m[0] != ui for m in mapped) or offs != list(range(offs[0], offs[0] + len(offs))):  # type: ignore[index]
            return None
        return ui, (offs[0], offs[-1] + 1)

    def _exempt(self, part: str, chars: list[tuple[int, int]]) -> bool:
        got = self._unit_span(part, chars)
        if got is None:
            return False
        ui, span = got
        return span in self.allowed[ui] or _inside(span, self.subs[ui])

    def _fail(self, k: str) -> None:
        raise Fail("residue_detected", k)

    def run(self) -> dict[str, Any]:
        if not self.needles:
            return {"targets": self.counts, "representations": list(MODES)}
        for ui, u in enumerate(self.units):  # K1
            self.counts["K1"] += 1
            for span in find_spans("".join(u.text), self.needles):
                if span not in self.allowed[ui] and not _inside(span, self.subs[ui]):
                    self._fail("K1")
        for part in self.pkg.xml_names():
            self._xml_part(part)
        for part, raw in self.pkg.raw.items():
            if part.endswith((".xml", ".rels")):
                continue
            self.counts["K8"] += 1
            for _m, n in self.raw_needles:
                for enc in ENCODINGS:
                    if n.encode(enc) in raw:
                        self._fail("K8")
        return {"targets": self.counts, "representations": list(MODES)}

    def _xml_part(self, part: str) -> None:
        root = self.pkg.tree(part)
        ords = self.ords.get(part) or _ordinals(root)
        is_rels = part.endswith(".rels")
        for el in root.iter():
            if not isinstance(el.tag, str):  # K6: comments and processing instructions
                self.counts["K6"] += 1
                if find_spans(el.text or "", self.needles):
                    self._fail("K6")
                continue
            o = ords[id(el)]
            for _k, val in el.attrib.items():  # K4 / K5
                key = "K5" if is_rels else "K4"
                self.counts[key] += 1
                for cand in (val, urllib.parse.unquote(val)):
                    if find_spans(cand, self.needles):
                        self._fail(key)
            text = el.text or ""
            if text:  # K2
                self.counts["K2"] += 1
                for a, b in find_spans(text, self.needles):
                    if not self._exempt(part, [(o, k) for k in range(a, b)]):
                        self._fail("K2")
            if el.tail and find_spans(el.tail, self.needles):
                self._fail("K2")
        for p in root.iter(f"{{{W}}}p"):  # K3: every text node of the paragraph, in order
            self.counts["K3"] += 1
            chars: list[tuple[int, int]] = []
            buf: list[str] = []
            for el in p.iter():
                if isinstance(el.tag, str) and el.text:
                    o = ords[id(el)]
                    for k, ch in enumerate(el.text):
                        buf.append(ch)
                        chars.append((o, k))
            for a, b in find_spans("".join(buf), self.needles):
                if not self._exempt(part, chars[a:b]):
                    self._fail("K3")
        self._raw(part)

    def _raw(self, part: str) -> None:
        """K7: raw bytes of the part; a match is exempt only if it is exactly the literal bytes of an allowed
        occurrence (or of a substitute) held in ONE element's character data (byte offsets from expat)."""
        raw = self.pkg.raw[part]
        self.counts["K7"] += 1
        byte_of: dict[tuple[int, int], tuple[int, int]] = {}  # (ordinal, char index) -> (byte start, byte end)
        stack: list[int] = []
        counter = [0]
        text_len: dict[int, int] = {}
        parser = expat.ParserCreate()
        parser.buffer_text = False

        def start(_name: str, _attrs: Any) -> None:
            stack.append(counter[0])
            text_len[counter[0]] = 0
            counter[0] += 1

        def end(_name: str) -> None:
            stack.pop()

        def chars(data: str) -> None:
            if not stack:
                return
            o = stack[-1]
            off = parser.CurrentByteIndex
            enc = data.encode("utf-8")
            literal = raw[off : off + len(enc)] == enc
            pos = off
            for ch in data:
                b = len(ch.encode("utf-8"))
                if literal:
                    byte_of[(o, text_len[o])] = (pos, pos + b)
                pos += b
                text_len[o] += 1

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = chars
        try:
            parser.Parse(raw, True)
        except expat.ExpatError:
            raise Fail("derivative_verification_failed", "package") from None
        allowed_bytes: set[tuple[int, int]] = set()
        sub_bytes: list[tuple[int, int]] = []

        def to_bytes(u: Unit, a: int, b: int) -> tuple[int, int] | None:
            where = u.where[a:b]
            if not where or any(w is None for w in where):
                return None
            spots = [w for w in where if w is not None]
            if len({w[0] for w in spots}) != 1 or any(w not in byte_of for w in spots):
                return None
            return byte_of[spots[0]][0], byte_of[spots[-1]][1]

        for ui, u in enumerate(self.units):
            if u.part != part:
                continue
            for a, b in self.allowed[ui]:
                got = to_bytes(u, a, b)
                if got is not None:
                    allowed_bytes.add(got)
            for a, b in self.subs[ui]:
                got = to_bytes(u, a, b)
                if got is not None:
                    sub_bytes.append(got)
        for _m, n in self.raw_needles:
            for enc in ENCODINGS:
                needle = n.encode(enc)
                pos = raw.find(needle)
                while pos != -1:
                    span = (pos, pos + len(needle))
                    if not (
                        enc == "utf-8"
                        and (span in allowed_bytes or any(a <= span[0] and span[1] <= b for a, b in sub_bytes))
                    ):
                        self._fail("K7")
                    pos = raw.find(needle, pos + 1)


# ---- entry point ---------------------------------------------------------------------------------
def verify(original: bytes, derivative: bytes, params: dict[str, Any]) -> dict[str, Any]:
    """params: oracle [str] per unit in order; values [str]; subs [[start, end]] per unit; markers [[start, end]]
    per unit (strict markers expected, protect mode); mode "protect" | "restore"."""
    try:
        orig, deriv = Pkg(original), Pkg(derivative)
        structural(deriv, orig)
        e_absent(deriv)
        structure(orig, deriv, allow_removed=params.get("mode") == "protect")
        units, ords = extract(deriv)
        oracle = params["oracle"]
        if len(units) != len(oracle) or any("".join(u.text) != t for u, t in zip(units, oracle, strict=True)):
            raise Fail("derivative_verification_failed", "oracle")
        if params.get("mode") == "protect":
            from ..protection.markers import scan

            expected = params["markers"]
            for ui, u in enumerate(units):
                sc = scan("".join(u.text))
                if sc.altered or [[s, e] for s, e, _m in sc.strict] != [list(x) for x in expected[ui]]:
                    raise Fail("derivative_verification_failed", "markers")
        subs = [[(int(a), int(b)) for a, b in per] for per in params.get("subs", [[] for _ in units])]
        scope = Residues(deriv, units, ords, list(params.get("values", [])), subs).run()
    except Fail as f:
        return {"failure": f.code, "check": f.check}
    return {
        "ok": True,
        "checks": ["package", "content_types", "relationships", "e_classes", "structure", "oracle", "residues"],
        "inspection_scope": {
            **scope,
            "parts_xml": len(deriv.xml_names()),
            "parts_binary": len(deriv.raw) - len(deriv.xml_names()),
            "not_covered": ["image_pixels", "font_glyphs", "content_not_extracted_by_a05"],
        },
    }
