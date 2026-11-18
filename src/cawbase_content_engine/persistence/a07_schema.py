"""A07 table: the restoration binding of every published protected DOCX (docs/docx-protection.md, section 12).

One definition shared by ``Tables`` (runtime) and by migration ``0005`` (frozen at this revision: any later change
needs its own revision). ``scope_id`` is part of the primary key and of the foreign key to ``scopes``. There is no
foreign key to ``protection_contexts`` on purpose: A06 purges its tombstones on its own schedule and the context is
re-checked on every use. No column holds an original value, a fragment, a type or a finding offset of the original.
"""

from __future__ import annotations

import sqlalchemy as sa

from .dialect import Namespace
from .types import UtcDateTime, json_type

BIG = sa.BigInteger


def build_a07(md: sa.MetaData, ns: Namespace) -> dict[str, sa.Table]:
    t, ix = ns.table, ns.index
    scopes = ns.qualified("scopes")
    out: dict[str, sa.Table] = {}
    out["docx_derivatives"] = sa.Table(
        t("docx_derivatives"),
        md,
        sa.Column("scope_id", sa.Text, sa.ForeignKey(f"{scopes}.scope_id"), primary_key=True),
        sa.Column("derivative_id", sa.Text, primary_key=True),
        sa.Column("protection_job_id", sa.Text, nullable=False),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("context_id", sa.Text, nullable=False),
        sa.Column("context_kind", sa.Text, nullable=False),
        sa.Column("artifact_id", sa.Text, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("size_bytes", BIG, nullable=False),
        sa.Column("extractor_id", sa.Text, nullable=False),
        sa.Column("extractor_version", sa.Text, nullable=False),
        sa.Column("transformer_id", sa.Text, nullable=False),
        sa.Column("transformer_version", sa.Text, nullable=False),
        sa.Column("profile_id", sa.Text, nullable=False),
        sa.Column("version_id", sa.Text, nullable=False),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("profile_content_hash", sa.Text, nullable=False),
        sa.Column("selection", sa.Text, nullable=False),
        sa.Column("occurrences", json_type()),
        sa.Column("occurrence_count", sa.Integer, nullable=False),
        sa.Column("binding_mac", sa.LargeBinary),
        sa.Column("mac_key_version", sa.Text),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.Column("ended_at", UtcDateTime),
        sa.Column("metadata_expires_at", UtcDateTime),
        sa.UniqueConstraint("derivative_id", name=t("docx_derivatives_id_uq")),
        sa.UniqueConstraint("scope_id", "protection_job_id", name=t("docx_derivatives_job_uq")),
        sa.CheckConstraint("state IN ('active','ended')", name=t("docx_derivatives_state")),
        sa.CheckConstraint("occurrence_count >= 0 AND size_bytes >= 0", name=t("docx_derivatives_counts")),
        sa.Index(ix("ix_docx_derivatives_state"), "state"),
        sa.Index(ix("ix_docx_derivatives_context"), "scope_id", "context_id"),
        sa.Index(ix("ix_docx_derivatives_meta"), "metadata_expires_at"),
    )
    return out
