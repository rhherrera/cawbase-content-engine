"""Synthetic PDF fixtures + independent manifests (oracle emitted by construction from the generator's own spec)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from a04.common import sha256_bytes
from a04.pdfgen import Pdf, strip_marks, text_obj

F2MAP = {"fi": b"\x01", "fl": b"\x02", "\U0001f600": b"\x03", "́": b"\x04"}
VALUES = {
    "v1": "Ana Ejemplo",
    "v2": "ana.ejemplo@example.test",
    "v3": "EjemploNoReal-123",
    "v4": "Zoé Café",
    "v5": "Oficina Ejemplo",
}


def enc_f2(s: str) -> bytes:
    out, i = b"", 0
    while i < len(s):
        for k, v in F2MAP.items():
            if s.startswith(k, i):
                out += v
                i += len(k)
                break
        else:
            out += s[i].encode("cp1252")
            i += 1
    return out


class Spec:
    def __init__(self, fid: str, note: str, *, compress=True):
        self.fid, self.note = fid, note
        self.pdf = Pdf(compress=compress)
        self.fonts = self.pdf.fonts()
        self.page_units: list[list[str]] = []  # per page: list of shown groups (for expected unit text)
        self.occs: list[dict] = []
        self.cases: list[dict] = []
        self.extra: dict = {}
        self.cur: list[tuple[str, list]] = []
        self.manual_occs: list[dict] = []

    def page(self, objs: list[dict], *, rotate=0, xobjects=None, extra_content=b"", mediabox=(0, 0, 400, 300)) -> int:
        """objs: dicts {font,size,x,y,items,rot} or {"do": name, "unit_groups": [...] } for Form XObject uses."""
        content, groups = b"", []
        for o in objs:
            if "do" in o:
                content += f"/{o['do']} Do\n".encode()
                groups += [("x", g) for g in o["groups"]]
                continue
            font = o.get("font", "F1")
            text, items, occs = strip_marks(o["items"])
            if font == "F2":
                items = [
                    enc_f2(it) if isinstance(it, str) else [enc_f2(e) if isinstance(e, str) else e for e in it]
                    for it in items
                ]
            content += text_obj(font, o.get("size", 12), o["x"], o["y"], items, o.get("rot", 0))
            groups.append(("t", (text, occs)))
        content += extra_content
        self.pdf.add_page(content, self.fonts, rotate=rotate, xobjects=xobjects, mediabox=mediabox)
        self.page_units.append(groups)
        return len(self.page_units) - 1

    def finish(self, selected_cases: list[dict]) -> tuple[bytes, dict]:
        data = self.pdf.build()
        units = []
        occs = []
        for pi, groups in enumerate(self.page_units):
            text, pos = "", 0
            for kind, g in groups:
                if kind == "t":
                    t, oc = g
                else:
                    t, oc = g["text"], g["occs"]
                if text:
                    text += "\n"
                    pos += 1
                for o in oc:
                    occs.append(
                        {
                            **o,
                            "page": pi,
                            "start": pos + o["start"],
                            "end": pos + o["end"],
                            "unit_index": pi,
                            **({"object": g["object"]} if kind == "x" else {}),
                        }
                    )
                text += t
                pos += len(t)
            units.append({"page": pi, "text": text})
        for mo in self.manual_occs:
            occs.append({**mo, "unit_index": mo["page"]})
        man = {
            "fixture_id": self.fid,
            "kind": "pdf",
            "file": f"{self.fid}.pdf",
            "sha256": sha256_bytes(data),
            "note": self.note,
            "values": {o["value_id"]: VALUES.get(o["value_id"], o["text"]) for o in occs},
            "expected_units": units,
            "occurrences": occs,
            "cases": selected_cases,
            "reference": {
                "pages": len(units),
                "mediaboxes": [
                    list(map(float, mb)) for mb in self.extra.get("mediaboxes", [[0, 0, 400, 300]] * len(units))
                ],
                "rotations": self.extra.get("rotations", [0] * len(units)),
            },
            **{k: v for k, v in self.extra.items() if k not in ("mediaboxes", "rotations")},
        }
        return data, man


def T(items, x=30, y=250, **k):
    return {"items": items, "x": x, "y": y, **k}


def p_basic() -> Spec:
    s = Spec(
        "P01_basic", "value split across Tj operators and a TJ array with kerning; identical unselected occurrence"
    )
    s.page(
        [
            T(["Contactar a <<v1.a|Ana ", "Ejem", "plo>> hoy."], y=250),
            T([["Correo: <<v2.a|ana.ej", -20, "emplo@example.test>>"]], y=220),
            T(["Repetida sin seleccionar: <<v1.b|Ana Ejemplo>>."], y=190),
        ]
    )
    s.page([T(["Clave <<v3.a|EjemploNoReal-123>> final."], y=250), T(["Otra <<v1.c|Ana Ejemplo>> mencion"], y=220)])
    s.cases = [
        {"name": "select_a", "selected": ["v1.a", "v2.a", "v3.a", "v1.c"], "expected": "ok", "replacement": "[X]"}
    ]
    return s


def p_columns() -> Spec:
    s = Spec(
        "P02_columns",
        "two columns at the same height; unit text concatenates in stream order (no semantic segmentation)",
    )
    s.page(
        [
            T(["Columna izq <<v1.a|Ana Ejemplo>> fin"], x=30, y=250),
            T(["Columna der <<v2.a|ana.ejemplo@example.test>> fin"], x=210, y=250),
        ],
        mediabox=(0, 0, 520, 300),
    )
    s.extra["mediaboxes"] = [[0, 0, 520, 300]]
    s.cases = [{"name": "select_both_columns", "selected": ["v1.a", "v2.a"], "expected": "ok", "replacement": "[X]"}]
    return s


def p_rotated() -> Spec:
    s = Spec("P03_rotated", "page /Rotate 90; page with 90-degree text matrix; page with 180-degree text matrix")
    s.page([T(["Pagina rotada <<v1.a|Ana Ejemplo>> fin"], y=250)], rotate=90)
    s.page([T(["Texto girado <<v1.b|Ana Ejemplo>> fin"], x=60, y=40, rot=90)])
    s.page([T(["Texto invertido <<v2.a|ana.ejemplo@example.test>> fin"], x=300, y=200, rot=180)])
    s.extra["rotations"] = [90, 0, 0]
    s.cases = [
        {"name": "select_all_rotated", "selected": ["v1.a", "v1.b", "v2.a"], "expected": "ok", "replacement": "[X]"}
    ]
    return s


def p_unicode() -> Spec:
    s = Spec(
        "P04_unicode_ligature",
        "custom encoding: code 1 = fi ligature (1 glyph -> 2 code points), code 3 -> U+1F600 (code point outside the BMP), code 4 -> U+0301 combining",
    )
    s.page(
        [
            T(["Emoji \U0001f600 antes de <<v1.a|Ana Ejemplo>> y <<v4.a|Zoé Café>> luego"], font="F2", y=250),
            T(["<<v5.a|Oficina Ejemplo>> y de nuevo <<v5.b|Oficina Ejemplo>>"], font="F2", y=220),
            T(["Parcial: of<<v6.a|fi>>cina"], font="F2", y=190),
        ]
    )
    s.cases = [
        {"name": "select_values", "selected": ["v1.a", "v4.a", "v5.a"], "expected": "ok", "replacement": "[X]"},
        {
            "name": "partial_ligature",
            "selected_ranges": [["v6.a", 0, 1]],
            "expected": "reject:localization_ambiguous",
            "replacement": "[X]",
            "note": "select only the 'f' of the 'fi' ligature (indivisible piece, n:1)",
        },
    ]
    return s


def p_shared() -> Spec:
    s = Spec("P05_shared_xobject", "Form XObject used on two pages; partial selection of a shared object must fail")
    xs = s.pdf.stream(
        text_obj("F1", 12, 30, 30, ["Pie compartido <<v2.a|ana.ejemplo@example.test>> fin"])
        .replace(b"<<v2.a|", b"")
        .replace(b">>", b"")
        if False
        else text_obj("F1", 12, 30, 30, ["Pie compartido ana.ejemplo@example.test fin"]),
        b"/Type /XObject /Subtype /Form /BBox [0 0 400 60] /Resources << /Font << /F1 %d 0 R >> >> " % s.fonts["F1"],
    )
    grp = {
        "text": "Pie compartido ana.ejemplo@example.test fin",
        "occs": [
            {
                "occ": "v2.a",
                "value_id": "v2",
                "start": 15,
                "end": 39,
                "text": "ana.ejemplo@example.test",
                "object": "xo1",
            }
        ],
        "object": "xo1",
    }
    s.page([T(["Pagina uno <<v1.a|Ana Ejemplo>>"], y=250), {"do": "X1", "groups": [grp]}], xobjects={"X1": xs})
    grp2 = {"text": grp["text"], "occs": [{**grp["occs"][0], "occ": "v2.b"}], "object": "xo1"}
    s.page([T(["Pagina dos"], y=250), {"do": "X1", "groups": [grp2]}], xobjects={"X1": xs})
    s.cases = [
        {"name": "both_uses", "selected": ["v1.a", "v2.a", "v2.b"], "expected": "ok", "replacement": "[X]"},
        {
            "name": "one_use_only",
            "selected": ["v2.a"],
            "expected": "reject:shared_object_partial",
            "replacement": "[X]",
        },
    ]
    return s


def p_ambiguous() -> Spec:
    s = Spec(
        "P06_artificial_concat",
        "value broken across two text objects (line wrap): the interval includes a synthesized separator",
    )
    s.page([T(["Linea que corta Ana"], y=250), T(["Ejemplo en la siguiente"], y=235)])
    s.manual_occs.append({"occ": "v1.a", "value_id": "v1", "page": 0, "start": 16, "end": 27, "text": "Ana\nEjemplo"})
    s.cases = [
        {
            "name": "wrapped_value",
            "selected": ["v1.a"],
            "expected": "reject:localization_ambiguous",
            "replacement": "[X]",
        }
    ]
    return s


def p_replacements() -> Spec:
    s = Spec(
        "P07_replacements",
        "same value, replacements shorter / equal / longer than the original and with a glyph not available in the font",
    )
    s.page([T(["Valor <<v1.a|Ana Ejemplo>> en linea y mas texto despues"], y=250)])
    s.cases = [
        {"name": "short", "selected": ["v1.a"], "expected": "ok", "replacement": "[X]"},
        {
            "name": "equal_width",
            "selected": ["v1.a"],
            "expected": "ok",
            "replacement": "Aoa Ejemplo",
            "note": "n and o have the same advance width in Helvetica (556): equal total width",
        },
        {
            "name": "wider_than_original",
            "selected": ["v1.a"],
            "expected": "reject:replacement_overflow",
            "replacement": "WWWWWWWWWWWWWW",
        },
        {
            "name": "glyph_unavailable",
            "selected": ["v1.a"],
            "expected": "reject:substitute_glyph_unavailable",
            "replacement": "Łukasz",
        },
    ]
    return s


def p_meta() -> Spec:
    s = Spec(
        "P08_meta_comments",
        "/Info and XMP descriptive metadata, Text+Popup comment annotation (authorized removal) and a Link annotation whose URI repeats a selected value",
    )
    pdf = s.pdf
    s.page([T(["Texto visible <<v1.a|Ana Ejemplo>> fin"], y=250), T(["Enlace aqui"], y=200)])
    pdf.info = b"<< /Title (Informe de Ana Ejemplo) /Author (Ana Ejemplo) /Subject (EjemploNoReal-123) /Keywords (ana.ejemplo@example.test) /Producer (Synthetic) >>"
    xmp = (
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator><rdf:Seq><rdf:li>Ana Ejemplo</rdf:li></rdf:Seq></dc:creator></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    )
    xm = pdf.stream(xmp, b"/Type /Metadata /Subtype /XML ", compress=False)
    pop = pdf.add(b"<< /Type /Annot /Subtype /Popup /Rect [200 200 300 250] /Open false >>")
    note = pdf.add(
        b"<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] /Contents (Revisar a Ana Ejemplo) /T (Ana Ejemplo) /Popup %d 0 R /F 4 >>"
        % pop
    )
    link = pdf.add(
        b"<< /Type /Annot /Subtype /Link /Rect [30 195 100 215] /Border [0 0 0] /A << /S /URI /URI (https://example.test/u?n=Ana%20Ejemplo) >> >>"
    )
    pdf.objs[pdf.pages[0]] = pdf.objs[pdf.pages[0]].replace(
        b" /Contents", b" /Annots [%d 0 R %d 0 R %d 0 R] /Contents" % (note, pop, link)
    )
    pdf.extra_catalog = b"/Metadata %d 0 R " % xm
    s.cases = [
        {
            "name": "remove_authorized",
            "selected": ["v1.a"],
            "expected": "ok",
            "replacement": "[X]",
            "post_verification": "residue_detected_in_link_uri",
            "allowed_residue_hows": ["pct-NFC", "pct-NFD"],
            "extra_mask_user_rects": [[10, 10, 30, 30]],
        }
    ]
    s.extra["removal_expected"] = {
        "classes": ["info_authorship", "info_descriptive", "xmp_descriptive", "comment_annotations"],
        "residue_values": ["Ana Ejemplo", "EjemploNoReal-123", "ana.ejemplo@example.test"],
        "kept_link_uri_contains": "Ana%20Ejemplo",
    }
    return s


def p_all_selected() -> Spec:
    s = Spec("P09_all_selected", "every occurrence of v1 selected (global absence); and a control without the values")
    s.page([T(["<<v1.a|Ana Ejemplo>> y <<v1.b|Ana Ejemplo>>"], y=250), T(["Final <<v1.c|Ana Ejemplo>>"], y=220)])
    s.cases = [
        {
            "name": "all",
            "selected": ["v1.a", "v1.b", "v1.c"],
            "expected": "ok",
            "replacement": "[X]",
            "global_absence": ["v1"],
        }
    ]
    return s


def p_no_values() -> Spec:
    s = Spec("P10_no_values", "document without the selected values")
    s.page([T(["Texto neutro sin valores."], y=250)])
    s.cases = [
        {
            "name": "none_present",
            "selected": [],
            "expected": "ok",
            "replacement": "[X]",
            "note": "selection refers to value absent from the document",
        }
    ]
    return s


OK = [
    p_basic,
    p_columns,
    p_rotated,
    p_unicode,
    p_shared,
    p_ambiguous,
    p_replacements,
    p_meta,
    p_all_selected,
    p_no_values,
]


def write_all(outdir: Path) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    mans = []
    for fn in OK:
        s = fn()
        data, man = s.finish(s.cases)
        (outdir / man["file"]).write_bytes(data)
        (outdir / f"{man['fixture_id']}.manifest.json").write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n")
        mans.append(man)
    return mans


if __name__ == "__main__":
    for m in write_all(Path(sys.argv[1])):
        print(m["fixture_id"], len(m["expected_units"]), "units", len(m["occurrences"]), "occs", m["sha256"][:12])
