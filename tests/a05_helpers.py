"""Helpers for the A05 tests: real engine, real filesystem storage, real extraction child; only inference is simulated."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from cawbase_content_engine import ContentEngine, EngineConfig, ScopePermission
from cawbase_content_engine.documents.runner import UnconfinedDevelopmentRunner
from cawbase_content_engine.inference import AdapterDescriptor, AdapterState, InferenceSetup, full_coverage, locate
from cawbase_content_engine.jobs.setup import DocumentSetup, JobsLimits, JobsSetup, Retention, StorageSetup
from cawbase_content_engine.jobs.storage import FilesystemStorage

from .a03_helpers import AE, PR, PW, build_profile

SP = ScopePermission
FULL = [PR, PW, AE, SP.FILES_WRITE, SP.FILES_READ, SP.JOBS_READ, SP.JOBS_CANCEL, SP.RESULTS_READ, SP.DATA_DELETE]
TEXT = "Contactar a Ana Ejemplo (ana.ejemplo@example.test). Mi contraseña es EjemploNoReal-123."
FRAGMENTS = ("Ana Ejemplo", "ana.ejemplo@example.test", "EjemploNoReal-123")


class StubAdapter:
    """Deterministic inference stand-in. ``on_infer`` lets a test pause, fail or observe a call."""

    def __init__(self, *, on_infer: Any = None, config_id: str = "stub-1") -> None:
        self.calls: list[str] = []
        self.on_infer = on_infer
        self._d = AdapterDescriptor(
            kind="mock", adapter_id="cce-a05-stub", adapter_version="1", configuration_id=config_id
        )

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._d

    async def probe(self) -> AdapterState:
        return AdapterState.READY

    async def infer(self, request: Any) -> dict[str, Any]:
        self.calls.append(request.text)
        if self.on_infer is not None:
            await self.on_infer(request)
        text = request.text
        out: dict[str, Any] = {"contract_version": 1}
        if "classification" in request.capabilities:
            ids = [c.element_id for c in request.categories]
            sc = {"finance": 0.82, "hr": 0.31, "legal": 0.80}
            out["classification"] = {
                "status": "completed",
                "reason": None,
                "coverage": full_coverage(ids, text),
                "scores": [{"category_id": i, "score": sc.get(i, 0.1)} for i in ids],
            }
        if "detection" in request.capabilities:
            ids = [t.element_id for t in request.sensitive_types]
            found = []
            for frag in FRAGMENTS:
                if frag in text and ids:
                    tid = "password" if "NoReal" in frag else "personal_data"
                    if tid in ids:
                        found.append({"type_id": tid, **locate(text, frag)})
            out["detection"] = {
                "status": "completed",
                "reason": None,
                "coverage": full_coverage(ids, text),
                "findings": found,
            }
        return out


def fast_limits(**kw: Any) -> JobsLimits:
    base = dict(
        lease_ttl_s=3.0,
        heartbeat_interval_s=1.0,
        backoff_base_s=0.05,
        backoff_max_s=0.2,
        upload_timeout_s=30.0,
        upload_idle_timeout_s=10.0,
        extraction_timeout_s=60.0,
        purge_claim_ttl_s=30.0,
    )
    base.update(kw)
    return replace(JobsLimits(), **base)


async def make_engine(
    url: str,
    tmp: Path,
    *,
    adapter: Any = None,
    limits: JobsLimits | None = None,
    retention: Retention | None = None,
    docs: bool = True,
    name: str = "objects",
) -> ContentEngine:
    storage = StorageSetup(managed=FilesystemStorage(tmp / name))
    kwargs: dict[str, Any] = dict(
        inference=InferenceSetup(adapter or StubAdapter(), timeout_s=10.0),
        storage=storage,
        jobs=JobsSetup(limits=limits or fast_limits(), retention=retention or Retention()),
    )
    if docs:
        kwargs["documents"] = DocumentSetup(runner=UnconfinedDevelopmentRunner(), untrusted_input=False)
    eng = ContentEngine(EngineConfig(database_url=url), **kwargs)
    await eng.start()
    return eng


async def ready_scope(engine: ContentEngine, scope_id: str = "a", **kw: Any) -> Any:
    """Scope + published default profile; returns full-permission scoped operations."""
    await build_profile(engine, scope_id, **kw)
    return engine.scoped(engine.trusted_scope_context(scope_id, FULL, actor="test-host"))


async def chunked(data: bytes, size: int = 7000) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]
        await asyncio.sleep(0)


def make_docx(
    paragraphs: list[str],
    *,
    comments: list[str] | None = None,
    hyperlink: str | None = None,
    table: list[list[str]] | None = None,
) -> bytes:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for i, row in enumerate(table):
            for j, cell in enumerate(row):
                t.cell(i, j).text = cell
    if comments:
        for c in comments:
            d.add_comment(d.paragraphs[0].runs, text=c, author="Synthetic Author", initials="SA")
    d.core_properties.author = "Synthetic Author"
    buf = io.BytesIO()
    d.save(buf)
    return _strip_custom_xml(buf.getvalue())


def _strip_custom_xml(data: bytes) -> bytes:
    """The python-docx template ships ``customXml/`` parts (refused by the A05 matrix, DX-17): fixtures drop them."""
    import re
    import zipfile

    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for info in src.infolist():
            if info.filename.startswith("customXml/"):
                continue
            body = src.read(info)
            if info.filename == "word/_rels/document.xml.rels":
                body = re.sub(rb"<Relationship [^>]*customXml[^>]*/>", b"", body)
            zf.writestr(info.filename, body)
    return out.getvalue()


def make_pdf(
    pages: list[list[str]], *, font: str = "Helvetica", extra_page_entries: dict[str, Any] | None = None
) -> bytes:
    """Simple Type1 (WinAnsi) PDF: one ``Tj`` per line, Td between lines."""
    import pikepdf

    pdf = pikepdf.new()
    fontobj = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name("/" + font),
            Encoding=pikepdf.Name.WinAnsiEncoding,
        )
    )
    for lines in pages:
        ops = ["BT", "/F1 12 Tf", "72 720 Td"]
        for i, line in enumerate(lines):
            esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({esc}) Tj")
            if i != len(lines) - 1:
                ops.append("0 -14 Td")
        ops.append("ET")
        content = pikepdf.Stream(pdf, "\n".join(ops).encode("cp1252"))
        page = pikepdf.Dictionary(
            Type=pikepdf.Name.Page,
            MediaBox=[0, 0, 612, 792],
            Contents=content,
            Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=fontobj)),
        )
        for k, v in (extra_page_entries or {}).items():
            page[k] = v
        pdf.pages.append(pikepdf.Page(page))
    buf = io.BytesIO()
    pdf.save(buf, object_stream_mode=pikepdf.ObjectStreamMode.disable, compress_streams=False)
    return buf.getvalue()


async def raw(engine: ContentEngine, sql: str, **params: Any) -> list[Any]:
    """Direct read of an engine table (tests only)."""
    t = engine.tables
    full = sql.format(
        **{
            n: (f'"{t.ns.schema}".' if t.ns.schema else "") + t.ns.table(n)
            for n in (
                "jobs",
                "stored_files",
                "job_artifacts",
                "job_attempts",
                "scope_usage",
                "engine_usage",
                "scope_job_fences",
                "storage_tombstones",
                "idempotency_records",
                "file_validations",
                "admin_events",
                "scopes",
            )
        }
    )
    async with engine._engine.begin() as c:  # type: ignore[union-attr]
        res = await c.execute(sa.text(full), params)
        return list(res.all()) if res.returns_rows else []


async def run_all(engine: ContentEngine, worker_id: str = "w1", rounds: int = 20) -> int:
    """Runs every eligible job to completion with a worker (no executor, no pauses)."""
    w = engine.job_worker(worker_id)
    n = 0
    for _ in range(rounds):
        claimed = await w.claim(limit=5)
        if not claimed:
            break
        for c in claimed:
            await w.run_claimed(c)
            n += 1
    return n


def at(engine: ContentEngine, dt: datetime | None = None, *, delta: timedelta | None = None) -> datetime:
    """A datetime parameter for raw SQL in the form the backend stores (naive UTC text on SQLite)."""
    base = dt or datetime.now(UTC)
    if delta is not None:
        base += delta
    return base.replace(tzinfo=None) if engine.backend.value == "sqlite" else base


async def make_host_engine_a05(backend: str, url: str, tmp: Path, *, adapter: Any = None) -> tuple[ContentEngine, Any]:
    """Engine in injected mode (host owns the AsyncEngine) with A05 configured; returns (engine, host_engine)."""
    from .helpers import make_host_engine

    host = await make_host_engine(backend, url)
    eng = ContentEngine(
        EngineConfig(async_engine=host),
        inference=InferenceSetup(adapter or StubAdapter(), timeout_s=10.0),
        storage=StorageSetup(managed=FilesystemStorage(tmp / "host-objects")),
        documents=DocumentSetup(runner=UnconfinedDevelopmentRunner(), untrusted_input=False),
        jobs=JobsSetup(limits=fast_limits()),
    )
    await eng.start()
    return eng, host
