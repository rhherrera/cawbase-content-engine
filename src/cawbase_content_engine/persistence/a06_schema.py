"""A06 tables (protection contexts, encrypted mappings, per-scope usage).

One definition shared by ``Tables`` (runtime) and by migration ``0004`` (frozen at this revision: any later
change to these definitions needs its own revision). ``scope_id`` is part of every primary and foreign key.
No table ever holds an original value, a type, an offset or an unkeyed hash of a value
(docs/text-protection.md, section 10).
"""

from __future__ import annotations

import sqlalchemy as sa

from .dialect import Namespace
from .types import UtcDateTime

BIG = sa.BigInteger


def build_a06(md: sa.MetaData, ns: Namespace) -> dict[str, sa.Table]:
    t, ix = ns.table, ns.index
    scopes = ns.qualified("scopes")
    contexts = ns.qualified("protection_contexts")
    out: dict[str, sa.Table] = {}
    out["protection_contexts"] = sa.Table(
        t("protection_contexts"),
        md,
        sa.Column("scope_id", sa.Text, sa.ForeignKey(f"{scopes}.scope_id"), primary_key=True),
        sa.Column("context_id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("ttl_s", sa.Integer, nullable=False),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.Column("expires_at", UtcDateTime, nullable=False),
        sa.Column("deleted_at", UtcDateTime),
        sa.Column("index_key_version", sa.Text),
        sa.Column("index_key_wrapped", sa.LargeBinary),
        sa.Column("mapping_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("mapping_bytes", BIG, nullable=False, server_default="0"),
        sa.Column("metadata_expires_at", UtcDateTime),
        sa.Column("updated_at", UtcDateTime, nullable=False),
        sa.UniqueConstraint("context_id", name=t("protection_contexts_context_id_uq")),
        sa.CheckConstraint("state IN ('active','expired','deleted')", name=t("protection_contexts_state")),
        sa.CheckConstraint("kind IN ('conversation','job')", name=t("protection_contexts_kind")),
        sa.CheckConstraint("mapping_count >= 0 AND mapping_bytes >= 0", name=t("protection_contexts_counts")),
        sa.Index(ix("ix_protection_contexts_state"), "state", "expires_at"),
        sa.Index(ix("ix_protection_contexts_meta"), "metadata_expires_at"),
        sa.Index(ix("ix_protection_contexts_scope"), "scope_id", "state", "expires_at"),
    )
    out["protection_mappings"] = sa.Table(
        t("protection_mappings"),
        md,
        sa.Column("scope_id", sa.Text, primary_key=True),
        sa.Column("context_id", sa.Text, primary_key=True),
        sa.Column("token", sa.Text, primary_key=True),
        sa.Column("key_version", sa.Text, nullable=False),
        sa.Column("salt", sa.LargeBinary, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("equality_index", sa.LargeBinary, nullable=False),
        sa.Column("created_at", UtcDateTime, nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_id", "context_id"],
            [f"{contexts}.scope_id", f"{contexts}.context_id"],
            name=t("protection_mappings_context_fk"),
        ),
        sa.UniqueConstraint("scope_id", "context_id", "equality_index", name=t("protection_mappings_eq_uq")),
        sa.Index(ix("ix_protection_mappings_version"), "key_version"),
    )
    out["protection_scope_usage"] = sa.Table(
        t("protection_scope_usage"),
        md,
        sa.Column("scope_id", sa.Text, sa.ForeignKey(f"{scopes}.scope_id"), primary_key=True),
        sa.Column("active_contexts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", UtcDateTime, nullable=False),
        sa.CheckConstraint("active_contexts >= 0", name=t("protection_scope_usage_nonneg")),
    )
    return out
