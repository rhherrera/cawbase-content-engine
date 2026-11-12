# A04 — feasibility experiments (not part of the engine)

Experiment code for the A04 document-feasibility investigation (see `docs/features/A04-viabilidad-y-contrato-de-documentos.md`,
`docs/document-processing.md` and `docs/a04-feasibility-report.md`). **Nothing here is the engine, a library, or a product API**:
it is bounded code that contrasts hypotheses with synthetic documents. It lives outside `src/`, has its own
`pyproject.toml`/`uv.lock` (independent environment) and is excluded from the engine's wheel and sdist
(`[tool.hatch.build.targets.sdist] exclude = ["experiments"]`; the wheel only packages `src/cawbase_content_engine`).

## Layout

| Path | Purpose |
|---|---|
| `a04/gen_docx.py`, `a04/ooxml.py` | raw OOXML writer + DOCX fixtures; the manifest (values, occurrences, expected units, selections) is emitted **by construction**, never from the extractor under test |
| `a04/gen_pdf.py`, `a04/pdfgen.py`, `a04/gen_pdf_embedded.py` | raw PDF writer + PDF fixtures (`P11` needs the DejaVu font of the Linux image; its output is committed) |
| `a04/gen_reject.py` | rejection / partial-coverage fixtures (bombs, encryption, signatures, revisions, …) |
| `a04/docx_tool.py`, `a04/pdf_tool.py` | inventory, extraction, mapping, transformation, restoration (python-docx not used for edits; lxml/pikepdf) |
| `a04/verify.py`, `a04/verify_pdf.py` | independent verification (stdlib XML for DOCX; PDFium, pypdf, Poppler and pixel comparison for PDF) |
| `a04/a03_bridge.py` | EXP-04: A03 contract + deterministic mock (disposable SQLite file; no A03 changes) |
| `run_all.py` | runs EXP-01..10 and CN-01..07; writes `outputs/results-<env>.json` |
| `licenses.py` | records effective versions, licenses, bundled binaries |
| `fixtures/` | committed synthetic documents + independent manifests (hash-verified on load) |
| `outputs/` | results, environment records, renders and before/after samples (see below) |
| `docker/Dockerfile`, `run_linux.sh`, `run_macos.sh` | reproducible runs |

## Reproduce

```bash
bash experiments/a04/run_macos.sh      # macOS (needs Poppler: pdftotext/pdfinfo/pdftoppm)
bash experiments/a04/run_linux.sh      # Linux in Docker: network only to install pinned wheels/packages; processing runs with --network none
```

Regenerate fixtures (byte-deterministic, **except** `reject/Q01_encrypted.pdf` whose RC4/AES salt is random: the committed copy is authoritative; hashes are checked by `run_all.py`):

```bash
cd experiments/a04
uv run python -m a04.gen_docx fixtures/docx
uv run python -m a04.gen_pdf fixtures/pdf
uv run python -m a04.gen_reject fixtures/reject
# P11 (embedded DejaVu subset) only inside the Linux image:  python -m a04.gen_pdf_embedded fixtures/pdf
```

## Conventions

- All values are synthetic (`example.test`, fictitious names). No network during processing, no macros/scripts/external references.
- Limits are the experiment limits of the contract (§16), not product limits.
- `outputs/renders/<env>/*.png` are before | after montages (left original, right transformed). `outputs/samples/<env>` hold the transformed files whose hashes are in `results-<env>.json`.
- macOS, Linux (Docker) and GitHub Actions are separate environments: a result is attributed only to the environment that produced it (no workflow was run for A04).
