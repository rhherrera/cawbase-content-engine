"""Validator identity (docs 10.7): input hash + validator + version + configuration."""

from __future__ import annotations

from dataclasses import asdict

from .db import canonical, sha256_hex
from .setup import JobsLimits

VALIDATOR_ID = "cce-docs-validate"
VALIDATOR_VERSION = "1"
EXTRACTOR_ID = "cce-docs-extract"
EXTRACTOR_VERSION = "1"
_KEYS = (
    "max_file_bytes",
    "max_pdf_pages",
    "max_zip_entries",
    "max_docx_parts_processed",
    "max_decompressed_total_bytes",
    "max_decompressed_entry_bytes",
    "max_compression_ratio",
    "max_xml_depth",
    "max_xml_nodes_total",
    "max_table_nesting",
    "max_pdf_objects",
    "max_pdf_content_bytes_per_page",
    "max_form_xobject_depth",
    "max_text_operators_per_page",
)


def config_sha(lim: JobsLimits) -> str:
    d = asdict(lim)
    return sha256_hex(canonical({k: d[k] for k in _KEYS}))


def validation_identity(file_sha256_hex: str, lim: JobsLimits) -> str:
    """A05 identity (extractor v1 for every format): validations recorded before A08 and legacy jobs."""
    return sha256_hex(file_sha256_hex, VALIDATOR_ID, VALIDATOR_VERSION, config_sha(lim))


VALIDATOR_VERSION_2 = "2"


def validation_identity_v2(file_sha256_hex: str, lim: JobsLimits) -> str:
    """A08 identity (docs/pdf-protection.md 4.2): the validation of a new ``document_analysis`` also depends on the PDF
    extractor v2, so a file refused by the v1 extractor can be validated again (and a v1 result is never reused)."""
    from ..documents.pdf_extract import config_sha as pdf_config_sha

    return sha256_hex(file_sha256_hex, VALIDATOR_ID, VALIDATOR_VERSION_2, config_sha(lim), pdf_config_sha(lim))


def docx_identity(lim: JobsLimits) -> dict[str, str]:
    return {"id": EXTRACTOR_ID, "version": EXTRACTOR_VERSION, "config_sha256": config_sha(lim)}


def extractor_set(lim: JobsLimits) -> dict[str, dict[str, str]]:
    """Extractors pinned by a new ``document_analysis`` (P-A08-04): DOCX keeps its identity, PDF has its own."""
    from ..documents.pdf_extract import identity as pdf_identity

    return {"docx": docx_identity(lim), "pdf": pdf_identity(lim)}
