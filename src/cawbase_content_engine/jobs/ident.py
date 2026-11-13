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
    return sha256_hex(file_sha256_hex, VALIDATOR_ID, VALIDATOR_VERSION, config_sha(lim))
