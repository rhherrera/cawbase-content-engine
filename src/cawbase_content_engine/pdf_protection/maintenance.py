"""A08 binding cleanup (docs/pdf-protection.md, section 11): the A07 rules over ``pdf_derivatives``.

A binding whose context is no longer alive is marked ``ended`` (occurrences, layout and MAC dropped; metadata
tombstone for ``metadata_retention_s``); due tombstones are purged. Explicit, bounded, repeatable; needs no keys.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from ..docx_protection.maintenance import run_binding_cleanup
from .types import PdfCleanupSummary

if TYPE_CHECKING:
    from ..engine import ContentEngine


async def run_pdf_cleanup(engine: ContentEngine, *, batch_size: int | None = None) -> PdfCleanupSummary:
    ended, purged = await run_binding_cleanup(engine, "pdf_derivatives", batch_size=batch_size)
    if ended:
        await _drop_layouts(engine)
    return PdfCleanupSummary(bindings_ended=ended, tombstones_purged=purged)


async def _drop_layouts(engine: ContentEngine) -> None:
    """An ended binding keeps no geometry either (the shared cleanup only knows the A07 columns)."""
    d = engine.tables.pdf_derivatives

    async def fn(conn: object) -> None:
        await conn.execute(  # type: ignore[attr-defined]
            sa.update(d).where(d.c.state == "ended", d.c.layout.is_not(None)).values(layout=None)
        )

    await engine._run(fn, write=True, tx=None)
