"""Freezes one exported case: copies the PDF into corpus/<case>/ (or corpus-private/ when its embedded fonts are not
redistributable), writes provenance.json (hash, Producer/Creator, pdffonts, pdfinfo, exporter) and the oracle+zones.

    .venv/bin/python -m a08.intake <case_id> <exported_pdf> <exporter_json>
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import oracle
from .manifest import ROOT, load, sha256_file

NON_REDISTRIBUTABLE = ("Times-", "Helvetica", "Arial", "Calibri", "Cambria", "Aptos", "TimesNewRoman", "Georgia")


def _run(*a: str) -> str:
    r = subprocess.run(a, capture_output=True, text=True)
    return r.stdout + r.stderr


def fonts_of(pdf: Path) -> list[dict]:
    out = []
    lines = _run("pdffonts", str(pdf)).splitlines()[2:]
    for ln in lines:
        parts = ln.split()
        if not parts:
            continue
        name = parts[0]
        typ = " ".join(parts[1:-6]) if len(parts) > 7 else parts[1]
        out.append({"name": name, "type": typ, "encoding": parts[-6], "emb": parts[-5], "sub": parts[-4], "uni": parts[-3]})
    return out


def main(case_id: str, exported: str, exporter_json: str) -> None:
    m = load()
    case = next(x for x in m["cases"] if x["case_id"] == case_id)
    src = Path(exported)
    fonts = fonts_of(src)
    private = any(any(f["name"].split("+")[-1].startswith(p) for p in NON_REDISTRIBUTABLE) for f in fonts)
    base = ROOT / ("corpus-private" if private else "corpus") / case_id
    base.mkdir(parents=True, exist_ok=True)
    dst = base / f"{case_id}.pdf"
    shutil.copyfile(src, dst)
    meta_dir = ROOT / "corpus" / case_id
    meta_dir.mkdir(parents=True, exist_ok=True)
    info = _run("pdfinfo", str(dst))
    prov = {
        "case_id": case_id, "family": case["family"], "template": case["template"],
        "pdf_sha256": sha256_file(dst), "pdf_size": dst.stat().st_size,
        "stored_in": "corpus-private (fuentes incrustadas no redistribuibles; fuera de Git)" if private else "corpus",
        "exporter": json.loads(Path(exporter_json).read_text()),
        "pdfinfo": info.splitlines(), "pdffonts": fonts,
        "type0_cid_subset": any(f["type"].startswith("CID") and "+" in f["name"] for f in fonts),
        "intake_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "intake_host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "manifest_sha256": sha256_file(ROOT / "corpus" / "manifest.json"),
    }
    (meta_dir / "provenance.json").write_text(json.dumps(prov, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    o = oracle.build(case_id, dst, case["template"])
    (meta_dir / "oracle.json").write_text(json.dumps(o, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(case_id, "private" if private else "public", prov["pdf_sha256"][:12], o["status"], "type0" if prov["type0_cid_subset"] else "")


if __name__ == "__main__":
    main(*sys.argv[1:4])
