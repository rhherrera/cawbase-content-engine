"""Stage 2B intake: freezes one case BEFORE the candidate runs on it — PDF (copied, or the stage-2 frozen PDF when
the case reuses it), provenance, oracle and zones with the typographic band (criterion 1) — and records the hashes in
corpus2b/FREEZE.json.

    .venv/bin/python -m a08.intake2b <case_id> [<exported_pdf> <exporter_json>]

Reused T1 cases (L1, C1, M1, and W1/G1 if they were exported for stage 2) take no PDF argument: the stage-2 PDF is
used as is and its hash must match stage 2's FREEZE.json. W/G cases whose PDF the user delivers take the PDF and a
provenance-input JSON (corpus2b/EXPORT-W-G.md).
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import oracle
from .intake import NON_REDISTRIBUTABLE, _run, fonts_of
from .manifest import MANIFEST_2B, ROOT, load, sha256_file

CORPUS = ROOT / "corpus2b"
FREEZE = CORPUS / "FREEZE.json"


def _freeze(case_id: str, entry: dict) -> None:
    fz = json.loads(FREEZE.read_text()) if FREEZE.exists() else {"stage": "2B", "manifest_sha256": sha256_file(MANIFEST_2B), "cases": {}}
    if fz["manifest_sha256"] != sha256_file(MANIFEST_2B):
        raise SystemExit("manifest changed after freezing started")
    if case_id in fz["cases"]:
        raise SystemExit(f"{case_id} already frozen: a frozen input is never replaced")
    fz["cases"][case_id] = entry
    FREEZE.write_text(json.dumps(fz, indent=1, ensure_ascii=False) + "\n")


def main(case_id: str, exported: str | None = None, exporter_json: str | None = None) -> None:
    m = load(MANIFEST_2B)
    case = next(x for x in m["cases"] if x["case_id"] == case_id)
    meta = CORPUS / case_id
    meta.mkdir(parents=True, exist_ok=True)
    if case.get("stage2_pdf") and exported is None:
        s2 = json.loads((ROOT / "corpus" / "FREEZE.json").read_text())["cases"][case_id]
        src = next(p for p in (ROOT / case["stage2_pdf"], ROOT / case["stage2_pdf"].replace("corpus/", "corpus-private/", 1)) if p.exists())
        if sha256_file(src) != s2["pdf_sha256"]:
            raise SystemExit("stage-2 PDF differs from its frozen hash")
        dst = src
        prov2 = json.loads((ROOT / "corpus" / case_id / "provenance.json").read_text())
        prov = {**prov2, "reused_from_stage2": {"pdf": case["stage2_pdf"], "stage2_freeze_commit": "5f1af03"},
                "intake_2b_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "manifest_sha256": sha256_file(MANIFEST_2B)}
    else:
        src = Path(exported)
        fonts = fonts_of(src)
        private = any(any(f["name"].split("+")[-1].startswith(p) for p in NON_REDISTRIBUTABLE) for f in fonts)
        base = ROOT / ("corpus2b-private" if private else "corpus2b") / case_id
        base.mkdir(parents=True, exist_ok=True)
        dst = base / f"{case_id}.pdf"
        shutil.copyfile(src, dst)
        prov = {
            "case_id": case_id, "family": case["family"], "template": case["template"],
            "pdf_sha256": sha256_file(dst), "pdf_size": dst.stat().st_size,
            "stored_in": "corpus2b-private (fuentes incrustadas no redistribuibles; fuera de Git)" if private else "corpus2b",
            "exporter": json.loads(Path(exporter_json).read_text()),
            "pdfinfo": _run("pdfinfo", str(dst)).splitlines(), "pdffonts": fonts,
            "type0_cid_subset": any(f["type"].startswith("CID") and "+" in f["name"] for f in fonts),
            "intake_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "intake_host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
            "manifest_sha256": sha256_file(MANIFEST_2B),
        }
    (meta / "provenance.json").write_text(json.dumps(prov, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    o = oracle.build(case_id, dst, case["template"], band_mode="typographic", manifest=m)
    (meta / "oracle.json").write_text(json.dumps(o, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    _freeze(case_id, {"pdf": str(dst.relative_to(ROOT)), "pdf_sha256": sha256_file(dst), "oracle_sha256": sha256_file(meta / "oracle.json"),
                      "provenance_sha256": sha256_file(meta / "provenance.json"), "oracle_status": o["status"],
                      "frozen_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    print(case_id, prov["pdf_sha256"][:12], o["status"], [(x["id"], x["status"], x.get("split_across_lines")) for x in o["occurrences"]],
          "type0" if prov["type0_cid_subset"] else "")


if __name__ == "__main__":
    main(*sys.argv[1:4])
