"""P11: embedded SUBSET TrueType font (DejaVu Sans, Bitstream Vera license) - generated once in the Linux image where the font is installed.

Usage (Linux image): python -m a04.gen_pdf_embedded fixtures/pdf    The produced PDF + manifest are committed; macOS runs only read them.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

from a04.common import sha256_bytes
from a04.pdfgen import Pdf, strip_marks, text_obj

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
LINES = [
    "Cliente: <<v1.a|Ana Ejemplo>> y de nuevo <<v1.b|Ana Ejemplo>>",
    "Correo: <<v2.a|ana.ejemplo@example.test>> fin",
]


def main(outdir: Path) -> None:
    shown = [strip_marks([ln])[0] for ln in LINES]
    chars = sorted(set("".join(shown)) | set("[X]"))
    opts = subset.Options()
    opts.layout_features = []
    opts.notdef_outline = True
    opts.name_IDs = []
    opts.hinting = False
    ft = TTFont(FONT)
    sub = subset.Subsetter(opts)
    sub.populate(text="".join(chars))
    sub.subset(ft)
    buf = io.BytesIO()
    ft.save(buf)
    prog = buf.getvalue()
    tt = TTFont(io.BytesIO(prog))
    upm = tt["head"].unitsPerEm
    cmap = tt.getBestCmap()
    hmtx = tt["hmtx"]
    widths = []
    for code in range(32, 256):
        ch = bytes([code]).decode("cp1252", errors="replace")
        g = cmap.get(ord(ch)) if ch != "�" else None
        widths.append(round(hmtx[g][0] * 1000 / upm) if g else 0)
    p = Pdf()
    ff = p.stream(prog, b"/Length1 %d " % len(prog))
    desc = p.add(
        b"<< /Type /FontDescriptor /FontName /AAAAAA+DejaVuSans /Flags 32 /FontBBox [-1021 -463 1793 1232] /ItalicAngle 0 /Ascent 928 /Descent -236 /CapHeight 729 /StemV 80 /FontFile2 %d 0 R >>"
        % ff
    )
    font = p.add(
        b"<< /Type /Font /Subtype /TrueType /BaseFont /AAAAAA+DejaVuSans /FirstChar 32 /LastChar 255 /Widths ["
        + b" ".join(str(w).encode() for w in widths)
        + b"] /Encoding /WinAnsiEncoding /FontDescriptor %d 0 R >>" % desc
    )
    content, groups, occs = b"", [], []
    y = 250
    for ln in LINES:
        text, items, oc = strip_marks([ln])
        content += text_obj("F1", 14, 30, y, items)
        groups.append((text, oc))
        y -= 30
    p.add_page(content, {"F1": font})
    data = p.build()
    unit, pos = "", 0
    for text, oc in groups:
        if unit:
            unit += "\n"
            pos += 1
        for o in oc:
            occs.append({**o, "page": 0, "start": pos + o["start"], "end": pos + o["end"], "unit_index": 0})
        unit += text
        pos += len(text)
    man = {
        "fixture_id": "P11_embedded_subset",
        "kind": "pdf",
        "file": "P11_embedded_subset.pdf",
        "sha256": sha256_bytes(data),
        "note": "embedded subset TrueType (DejaVu Sans, Bitstream Vera license; subset keeps only the document characters plus [X]); WinAnsi encoding",
        "values": {"v1": "Ana Ejemplo", "v2": "ana.ejemplo@example.test"},
        "expected_units": [{"page": 0, "text": unit}],
        "occurrences": occs,
        "reference": {"pages": 1, "mediaboxes": [[0.0, 0.0, 400.0, 300.0]], "rotations": [0]},
        "font_source": {"path": FONT, "program_chars": "".join(chars), "program_sha256": sha256_bytes(prog)},
        "cases": [
            {"name": "subset_glyphs", "selected": ["v1.a", "v2.a"], "expected": "ok", "replacement": "[X]"},
            {
                "name": "glyph_not_in_subset",
                "selected": ["v1.a"],
                "expected": "reject:substitute_glyph_unavailable",
                "replacement": "Q",
            },
        ],
    }
    (outdir / man["file"]).write_bytes(data)
    (outdir / "P11_embedded_subset.manifest.json").write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n")
    print(man["fixture_id"], len(data), "bytes", sha256_bytes(data)[:12])


if __name__ == "__main__":
    main(Path(sys.argv[1]))
