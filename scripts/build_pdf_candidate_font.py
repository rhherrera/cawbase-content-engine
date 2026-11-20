"""Builds the A08 candidate font data shipped with the package (docs/pdf-protection.md, section 7.3).

Development tool, NOT part of the runtime: it needs ``fontTools`` (run it with the A08 experiments environment,
``experiments/a08/.venv/bin/python scripts/build_pdf_candidate_font.py``). From the OFL-1.1 font
``experiments/a08/fonts/NotoSans-Condensed.ttf`` (SHA-256 checked) it writes, under
``src/cawbase_content_engine/documents/data/``:

* ``cce-candidate.ttf``: a TrueType subset with ONLY the substitute alphabet (``[]:`` + the A06 marker alphabet)
  and the space, without layout features;
* ``cce-candidate.json``: the frozen metrics the transformer and the verifier use (integer ``/Widths`` exactly as
  written in the PDF, glyph vertical extents, ascent/descent) and the SHA-256 of the subset;
* ``OFL.txt``: the font licence.

The runtime never parses the font program: it embeds these exact bytes and both sides use these exact numbers.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "experiments" / "a08" / "fonts" / "NotoSans-Condensed.ttf"
SRC_SHA256 = None  # recorded in the output; the experiments' fetch script verifies the download
OUT = ROOT / "src" / "cawbase_content_engine" / "documents" / "data"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SIGMA = "[]:" + ALPHABET


def main() -> None:
    from fontTools import subset
    from fontTools.ttLib import TTFont

    raw = SRC.read_bytes()
    src_sha = hashlib.sha256(raw).hexdigest()
    opts = subset.Options()
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    opts.layout_features = []
    opts.hinting = False
    font = TTFont(io.BytesIO(raw))
    sub = subset.Subsetter(opts)
    sub.populate(text=SIGMA + " ")
    sub.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    data = buf.getvalue()
    font = TTFont(io.BytesIO(data))
    upm = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    hmtx = font["hmtx"].metrics
    glyf = font["glyf"]
    widths: dict[str, int] = {}
    ext: dict[str, list[float]] = {}
    for ch in SIGMA + " ":
        gname = cmap[ord(ch)]
        widths[ch] = round(hmtx[gname][0] / upm * 1000)
        g = glyf[gname]
        if g.numberOfContours:
            ext[ch] = [round(g.yMin / upm * 1000, 3), round(g.yMax / upm * 1000, 3)]
    os2, head = font["OS/2"], font["head"]
    meta = {
        "source": {"file": "NotoSans-Condensed.ttf", "sha256": src_sha, "license": "OFL-1.1"},
        "subset_sha256": hashlib.sha256(data).hexdigest(),
        "base_font": "CCECAN+NotoSans-Condensed",
        "postscript_name": "NotoSans-Condensed",
        "alphabet": SIGMA,
        "widths": widths,
        "y_extent": ext,
        "ascent": round(os2.sTypoAscender / upm * 1000),
        "descent": round(os2.sTypoDescender / upm * 1000),
        "cap_height": round(getattr(os2, "sCapHeight", 714) / upm * 1000),
        "bbox": [round(v * 1000 / upm) for v in (head.xMin, head.yMin, head.xMax, head.yMax)],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cce-candidate.ttf").write_bytes(data)
    (OUT / "cce-candidate.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    shutil.copyfile(SRC.parent / "OFL.txt", OUT / "OFL.txt")
    print("subset", len(data), "bytes", meta["subset_sha256"], file=sys.stderr)


if __name__ == "__main__":
    main()
