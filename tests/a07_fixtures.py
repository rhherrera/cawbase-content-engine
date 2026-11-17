"""Synthetic DOCX builder for the A07 tests: raw OOXML written by hand, so the expected text of every paragraph
(the oracle) is known BY CONSTRUCTION and never taken from the extractor under test. Only fictitious values."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
)
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
NAME = "Ana Ejemplo"
MAIL = "ana.ejemplo@example.test"
CODE = "EjemploNoReal-123"


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def t(text: str) -> str:
    sp = ' xml:space="preserve"' if text != text.strip() or not text else ""
    return f"<w:t{sp}>{esc(text)}</w:t>"


def r(text: str, rpr: str = "") -> str:
    return f"<w:r>{f'<w:rPr>{rpr}</w:rPr>' if rpr else ''}{t(text)}</w:r>"


def p(*runs: str, ppr: str = "") -> str:
    return f"<w:p>{f'<w:pPr>{ppr}</w:pPr>' if ppr else ''}{''.join(runs)}</w:p>"


def tbl(rows: list[list[str]], *, grid: int = 2) -> str:
    cols = "".join('<w:gridCol w:w="2000"/>' for _ in range(grid))
    body = "".join("<w:tr>" + "".join(rows_i) + "</w:tr>" for rows_i in rows)
    return f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid>{cols}</w:tblGrid>{body}</w:tbl>'


def tc(*content: str, tcpr: str = "") -> str:
    return f"<w:tc>{f'<w:tcPr>{tcpr}</w:tcPr>' if tcpr else ''}{''.join(content)}</w:tc>"


@dataclass
class Doc:
    body: list[str] = field(default_factory=list)
    headers: dict[str, list[str]] = field(default_factory=dict)  # type -> paragraphs
    footers: dict[str, list[str]] = field(default_factory=dict)
    footnotes: list[list[str]] = field(default_factory=list)  # each: paragraphs
    endnotes: list[list[str]] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)  # comment texts (anchors written by the test)
    people: bool = False
    comments_extended: bool = False
    core: dict[str, str] = field(default_factory=dict)
    app: dict[str, str] = field(default_factory=dict)
    custom: dict[str, str] = field(default_factory=dict)
    thumbnail: bool = False
    hyperlinks: dict[str, str] = field(default_factory=dict)  # rId -> external target
    image: bool = False
    title_pg: bool = False
    even_odd: bool = False
    extra_parts: dict[str, bytes] = field(default_factory=dict)

    def build(self) -> bytes:
        ct = [
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="xml" ContentType="application/xml"/>',
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/>',
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.styles+xml"/>',
            '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.settings+xml"/>',
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.'
            'core-properties+xml"/>',
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'extended-properties+xml"/>',
        ]
        pkg_rels = [
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/>',
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/'
            'core-properties" Target="docProps/core.xml"/>',
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'extended-properties" Target="docProps/app.xml"/>',
        ]
        rels = [
            '<Relationship Id="rIdS" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'styles" Target="styles.xml"/>',
            '<Relationship Id="rIdT" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'settings" Target="settings.xml"/>',
        ]
        parts: dict[str, bytes] = {}
        sect = []
        n = 0
        for kind, store in (("header", self.headers), ("footer", self.footers)):
            for typ, paras in store.items():
                n += 1
                name = f"{kind}{n}.xml"
                tag = "hdr" if kind == "header" else "ftr"
                parts[f"word/{name}"] = (DECL + f"<w:{tag} {NS}>{''.join(paras)}</w:{tag}>").encode()
                ct.append(
                    f'<Override PartName="/word/{name}" ContentType="application/vnd.openxmlformats-officedocument.'
                    f'wordprocessingml.{kind}+xml"/>'
                )
                rels.append(
                    f'<Relationship Id="rId{kind}{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    f'relationships/{kind}" Target="{name}"/>'
                )
                sect.append(f'<w:{kind}Reference w:type="{typ}" r:id="rId{kind}{n}"/>')
        for kind, notes in (("footnote", self.footnotes), ("endnote", self.endnotes)):
            if not notes:
                continue
            body = (
                f'<w:{kind} w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:{kind}>'
                f'<w:{kind} w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r>'
                f"</w:p></w:{kind}>"
            )
            for i, paras in enumerate(notes, start=1):
                body += f'<w:{kind} w:id="{i}">{"".join(paras)}</w:{kind}>'
            parts[f"word/{kind}s.xml"] = (DECL + f"<w:{kind}s {NS}>{body}</w:{kind}s>").encode()
            ct.append(
                f'<Override PartName="/word/{kind}s.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                f'wordprocessingml.{kind}s+xml"/>'
            )
            rels.append(
                f'<Relationship Id="rId{kind}s" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                f'relationships/{kind}s" Target="{kind}s.xml"/>'
            )
        if self.comments:
            body = "".join(
                f'<w:comment w:id="{i}" w:author="Autor Sintetico" w:date="2026-01-01T00:00:00Z" w:initials="AS">'
                f"{p(r(text))}</w:comment>"
                for i, text in enumerate(self.comments)
            )
            parts["word/comments.xml"] = (DECL + f"<w:comments {NS}>{body}</w:comments>").encode()
            ct.append(
                '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                'wordprocessingml.comments+xml"/>'
            )
            rels.append(
                '<Relationship Id="rIdC" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'comments" Target="comments.xml"/>'
            )
        if self.comments_extended:
            parts["word/commentsExtended.xml"] = (
                DECL + '<w15:commentsEx xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"/>'
            ).encode()
            ct.append(
                '<Override PartName="/word/commentsExtended.xml" ContentType="application/vnd.openxmlformats-'
                'officedocument.wordprocessingml.commentsExtended+xml"/>'
            )
            rels.append(
                '<Relationship Id="rIdCE" Type="http://schemas.microsoft.com/office/2011/relationships/'
                'commentsExtended" Target="commentsExtended.xml"/>'
            )
        if self.people:
            parts["word/people.xml"] = (
                DECL + '<w15:people xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">'
                '<w15:person w15:author="Autor Sintetico"/></w15:people>'
            ).encode()
            ct.append(
                '<Override PartName="/word/people.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                'wordprocessingml.people+xml"/>'
            )
            rels.append(
                '<Relationship Id="rIdP" Type="http://schemas.microsoft.com/office/2011/relationships/people" '
                'Target="people.xml"/>'
            )
        for rid, target in self.hyperlinks.items():
            rels.append(
                f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                f'hyperlink" Target="{esc(target)}" TargetMode="External"/>'
            )
        if self.image:
            parts["word/media/image1.png"] = PNG
            ct.insert(0, '<Default Extension="png" ContentType="image/png"/>')
            rels.append(
                '<Relationship Id="rIdImg" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'image" Target="media/image1.png"/>'
            )
        core = "".join(f"<{k}>{esc(v)}</{k}>" for k, v in self.core.items())
        parts["docProps/core.xml"] = (
            DECL + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-'
            'properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            + core
            + '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created>'
            "</cp:coreProperties>"
        ).encode()
        app = "".join(f"<{k}>{esc(v)}</{k}>" for k, v in {"Application": "SyntheticWriter", **self.app}.items())
        parts["docProps/app.xml"] = (
            DECL
            + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            + app
            + "</Properties>"
        ).encode()
        if self.custom:
            props = "".join(
                f'<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="{i + 2}" name="{esc(k)}">'
                f"<vt:lpwstr>{esc(v)}</vt:lpwstr></property>"
                for i, (k, v) in enumerate(self.custom.items())
            )
            parts["docProps/custom.xml"] = (
                DECL + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
                'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
                + props
                + "</Properties>"
            ).encode()
            ct.append(
                '<Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                'custom-properties+xml"/>'
            )
            pkg_rels.append(
                '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'custom-properties" Target="docProps/custom.xml"/>'
            )
        if self.thumbnail:
            parts["docProps/thumbnail.png"] = PNG
            if '<Default Extension="png"' not in "".join(ct):
                ct.insert(0, '<Default Extension="png" ContentType="image/png"/>')
            pkg_rels.append(
                '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/'
                'thumbnail" Target="docProps/thumbnail.png"/>'
            )
        title = "<w:titlePg/>" if self.title_pg else ""
        doc = (
            DECL + f"<w:document {NS}><w:body>{''.join(self.body)}"
            f'<w:sectPr>{"".join(sect)}<w:pgSz w:w="11906" w:h="16838"/>{title}</w:sectPr></w:body></w:document>'
        )
        settings = "<w:evenAndOddHeaders/>" if self.even_odd else ""
        files = {
            "[Content_Types].xml": DECL
            + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            + "".join(ct)
            + "</Types>",
            "_rels/.rels": DECL
            + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(pkg_rels)
            + "</Relationships>",
            "word/document.xml": doc,
            "word/_rels/document.xml.rels": DECL
            + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels)
            + "</Relationships>",
            "word/styles.xml": DECL + f'<w:styles {NS}><w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
            '<w:name w:val="Normal"/></w:style><w:style w:type="character" w:styleId="Hyperlink">'
            '<w:name w:val="Hyperlink"/></w:style></w:styles>',
            "word/settings.xml": DECL + f"<w:settings {NS}>{settings}</w:settings>",
        }
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:  # byte-deterministic (fixed dates)
            for name, body in files.items():
                zf.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), body.encode("utf-8"))
            for name, data in {**parts, **self.extra_parts}.items():
                zf.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
        return out.getvalue()


def image_run(descr: str = "", title: str = "") -> str:
    """An inline picture whose alternative text (``wp:docPr``) is N content."""
    return (
        '<w:r><w:drawing><wp:inline><wp:extent cx="9525" cy="9525"/>'
        f'<wp:docPr id="1" name="Imagen 1" descr="{esc(descr)}" title="{esc(title)}"/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
        '<pic:nvPicPr><pic:cNvPr id="0" name="image1.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:embed="rIdImg"/></pic:blipFill><pic:spPr/></pic:pic></a:graphicData></a:graphic>'
        "</wp:inline></w:drawing></w:r>"
    )


def patch(data: bytes, part: str, fn: object) -> bytes:
    """Returns a copy of the package with ``part`` rewritten by ``fn(bytes) -> bytes`` (controls)."""
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
        for info in zin.infolist():
            body = zin.read(info.filename)
            if info.filename == part:
                body = fn(body)  # type: ignore[operator]
            zo.writestr(info.filename, body)
    return out.getvalue()


def part(data: bytes, name: str) -> bytes:
    return zipfile.ZipFile(io.BytesIO(data)).read(name)


def names(data: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(data)).namelist()
