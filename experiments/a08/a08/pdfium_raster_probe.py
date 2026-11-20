"""Evidence for N-2B-2 (stage 2B report §4): PDFium rasterizes the SAME glyphs of a text-showing operator 1 px apart
depending only on how much text the operator keeps after them, with identical bytes for those glyphs and identical
extracted positions. Synthetic page (controls' base line); compares the render of the prefix "Árbol:" region.

    .venv/bin/python -m a08.pdfium_raster_probe
"""

from __future__ import annotations

import io
import json

import numpy as np
import pikepdf

from .controls import LINE, TEXT
from .synth import Builder
from .verify import pdfium_chars, render_pdfium, render_poppler


def page(shown: bytes) -> bytes:
    b = Builder()
    b.font_simple("/F1", TEXT)
    b.text("/F1", 11, 72, 760, "Página de control sintético.")
    b.raw(f"BT /F1 11 Tf 72 700 Td <{shown.hex()}> Tj ET")
    return b.save()


def main() -> None:
    b = Builder()
    f = b.font_simple("/F1", TEXT)
    full = f.codes(LINE)
    ref = page(full)
    A, P = render_pdfium(ref)[0], render_poppler(ref)[0]
    rc = [c for c in pdfium_chars(ref)[0] if c["box"][1] < 712 and c["box"][3] > 698][:6]  # "Árbol:"
    out = []
    for keep in ("Árbol:", "Árbol: ", "Árbol: A", "Árbol: An", "Árbol: Ana", "Árbol: Ana Gil", LINE):
        d = page(f.codes(keep))
        B, Q = render_pdfium(d)[0], render_poppler(d)[0]
        rows, cols = slice(270, 300), slice(140, 215)  # 150 dpi region of "Árbol:" (72-103 pt)
        dc = [c for c in pdfium_chars(d)[0] if c["box"][1] < 712 and c["box"][3] > 698][:6]
        moved = max(max(abs(p - q) for p, q in zip(x["box"], y["box"])) for x, y in zip(rc, dc))
        out.append({"operator_text": keep, "pdfium_px_differing_in_prefix": int((A[rows, cols] != B[rows, cols]).sum()),
                    "poppler_px_differing_in_prefix": int((P[rows, cols] != Q[rows, cols]).sum()),
                    "max_extracted_box_change_pt": round(moved, 4)})
        print(out[-1])
    _ = (io, json, np, pikepdf)


if __name__ == "__main__":
    main()
