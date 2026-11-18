"""Evidence generator (AC-A07-70, 71, 72): writes original / protected / restored DOCX of representative fixtures
through the REAL pipeline into ``CCE_A07_EVIDENCE_DIR``; ``scripts/a07_render_evidence.sh`` then opens and renders
them with LibreOffice on Linux. Skipped unless the variable is set (it is not a unit test)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cawbase_content_engine.docx_protection.types import NewContext

from .a07_fixtures import MAIL, NAME, Doc, p, r, tbl, tc
from .a07_helpers import PSEUDO, World, analyse, mock_for, ops, read_all, run, setup_scope, unit_texts, upload

OUT = os.environ.get("CCE_A07_EVIDENCE_DIR")
pytestmark = pytest.mark.skipif(not OUT, reason="evidence generation only (CCE_A07_EVIDENCE_DIR not set)")


def fixtures() -> dict[str, bytes]:
    f01 = Doc(
        body=[
            p(
                r("F01: contactar a An", "<w:i/>"),
                r("a Ej", "<w:b/>"),
                r(f"emplo ({MAIL}) hoy.", '<w:color w:val="C00000"/>'),
            ),
            p(r(f"Mención libre de {NAME}, no seleccionada, con 😀 y é."), ppr='<w:jc w:val="center"/>'),
        ]
    )
    f02 = Doc(
        body=[
            p(r("F02: tablas")),
            tbl(
                [
                    [tc(p(r(f"Celda combinada {NAME}")), tcpr='<w:gridSpan w:val="2"/>')],
                    [
                        tc(
                            p(r("izq")),
                            tbl([[tc(p(r(f"Anidada {MAIL}"))), tc(p(r("x")))]]),
                            tcpr='<w:vMerge w:val="restart"/>',
                        ),
                        tc(p(r("der"))),
                    ],
                    [tc(p(), tcpr="<w:vMerge/>"), tc(p(r(f"Final {NAME}")))],
                ]
            ),
        ]
    )
    f03 = Doc(
        body=[p(r("F03: cuerpo"), '<w:r><w:footnoteReference w:id="1"/></w:r>'), p(r(f"Texto con {NAME}."))],
        headers={"default": [p(r(f"Encabezado {NAME}"))], "first": [p(r("Primera página"))], "even": [p(r("Par"))]},
        footers={"default": [p(r(f"Pie {MAIL}"))]},
        footnotes=[[p(r(f"Nota con {NAME}"))]],
        title_pg=True,
        even_odd=True,
    )
    f05 = Doc(
        body=[
            p(
                r("F05: enlace "),
                f'<w:hyperlink r:id="rIdH">{r(f"de {NAME}", '<w:rStyle w:val="Hyperlink"/>')}</w:hyperlink>',
            )
        ],
        hyperlinks={"rIdH": "https://example.test/doc"},
    )
    f06 = Doc(
        body=[
            p(
                f'<w:sdt><w:sdtPr><w:alias w:val="Campo"/><w:text/></w:sdtPr><w:sdtContent>{r(f"F06: control {NAME}")}</w:sdtContent></w:sdt>'
            )
        ]
    )
    f07 = Doc(
        body=[
            p(
                '<w:commentRangeStart w:id="0"/>',
                r(f"F07: texto anotado de {NAME}"),
                '<w:commentRangeEnd w:id="0"/>',
                '<w:r><w:commentReference w:id="0"/></w:r>',
            )
        ],
        comments=["Comentario sintético"],
        people=True,
        core={"dc:title": "Informe sintético", "dc:creator": "Autor Sintetico"},
        custom={"Cliente": "Sintético"},
        thumbnail=True,
    )
    out = {
        "F01": f01.build(),
        "F02": f02.build(),
        "F03": f03.build(),
        "F05": f05.build(),
        "F06": f06.build(),
        "F07": f07.build(),
    }
    e01 = Path(OUT or ".") / "E01_libreoffice.docx"
    if e01.exists():
        out["E01"] = e01.read_bytes()
    return out


def picks(text: str) -> list[tuple[str, str, int]]:
    found = []
    if "no seleccionada" in text:
        return []
    for frag in (NAME, MAIL):
        if frag in text:
            found.append(("personal_data", frag, 0))
    return found


async def test_write_evidence(migrated, tmp_path):
    _, url = migrated
    out_dir = Path(OUT or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {}
    for name, data in fixtures().items():
        w = World(url=url, tmp=tmp_path / name)
        try:
            base = await setup_scope(w)
            try:
                texts = unit_texts(data)
            except Exception as exc:  # the A05 matrix refuses the file
                summary[name] = {"result": "refused_by_a05", "reason": getattr(exc, "reason", type(exc).__name__)}
                continue
            eng = await w.engine(await mock_for(base, {t: picks(t) for t in texts}))
            o = ops(eng)
            try:
                _fid, aid = await analyse(eng, data, key=name)
            except AssertionError:
                info = (await o.list_jobs("a")).items[-1]
                summary[name] = {
                    "result": "analysis_failed",
                    "failure_code": info.failure_code,
                    "last_error_code": info.last_error_code,
                }
                continue
            acc = await o.submit_docx_protection(
                "a", analysis_job_id=aid, policy=PSEUDO, context=NewContext(), idempotency_key="p-" + name
            )
            await run(eng, acc.job.job_id)
            info = await o.get_job("a", acc.job.job_id)
            if info.state != "succeeded":
                summary[name] = {"result": "protection_failed", "failure_code": info.failure_code}
                continue
            prot = await read_all(await o.open_docx_protected("a", acc.job.job_id))
            res = await o.get_docx_protection_result("a", acc.job.job_id)
            copy = await upload(o, prot, "c-" + name)
            racc = await o.submit_docx_restoration(
                "a",
                derivative_id=res.derivative_id,
                file_id=copy,
                context_id=acc.context_id,
                kind="job",
                idempotency_key="r-" + name,
            )
            await run(eng, racc.job.job_id)
            rinfo = await o.get_job("a", racc.job.job_id)
            rest = (
                await read_all(await o.open_docx_restored("a", racc.job.job_id)) if rinfo.state == "succeeded" else b""
            )
            (out_dir / f"{name}_original.docx").write_bytes(data)
            (out_dir / f"{name}_protected.docx").write_bytes(prot)
            if rest:
                (out_dir / f"{name}_restored.docx").write_bytes(rest)
            summary[name] = {
                "result": "ok" if rest else "restoration_failed",
                "restored_equals_original_text": unit_texts(rest) == unit_texts(data) if rest else False,
                "counts": dict(res.counts),
                "removed": list(res.authorized_removals_performed),
            }
        finally:
            await w.close()
    (out_dir / "pipeline-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
