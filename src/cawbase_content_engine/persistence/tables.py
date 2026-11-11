"""SQLAlchemy Core table definitions for both backends (fixed namespace)."""

from __future__ import annotations

from functools import lru_cache

import sqlalchemy as sa

from .dialect import Backend, Namespace
from .types import UtcDateTime, big_pk, json_type


class Tables:
    def __init__(self, backend: Backend) -> None:
        ns = Namespace(backend)
        self.backend = backend
        self.ns = ns
        md = self.metadata = sa.MetaData(schema=ns.schema)
        t, ix = ns.table, ns.index

        def fk(col: str) -> sa.ForeignKey:
            return sa.ForeignKey(f"{ns.qualified(col.split('.')[0])}.{col.split('.')[1]}")

        self.engine_state = sa.Table(
            t("engine_state"),
            md,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
            sa.Column("schema_level", sa.Integer, nullable=False),
            sa.Column("min_runtime_level", sa.Integer, nullable=False),
            sa.Column("package_version_at_last_migration", sa.Text, nullable=False),
            sa.Column("admin_initialized_at", UtcDateTime),
            sa.Column("admin_bootstrap_generation", sa.Integer, nullable=False, server_default="0"),
            sa.Column("updated_at", UtcDateTime, nullable=False),
            sa.CheckConstraint("id = 1", name=t("engine_state_single_row")),
            sa.CheckConstraint("schema_level >= 1", name=t("engine_state_level_pos")),
            sa.CheckConstraint("admin_bootstrap_generation >= 0", name=t("engine_state_gen_nonneg")),
        )
        self.scopes = sa.Table(
            t("scopes"),
            md,
            sa.Column("scope_id", sa.Text, primary_key=True),
            sa.Column("display_name", sa.Text, nullable=False),
            sa.Column("description", sa.Text),
            sa.Column("metadata", json_type(), nullable=False),
            sa.Column("status", sa.Text, nullable=False),
            sa.Column("revision", sa.Integer, nullable=False),
            sa.Column("created_at", UtcDateTime, nullable=False),
            sa.Column("updated_at", UtcDateTime, nullable=False),
            sa.CheckConstraint("status IN ('active','disabled')", name=t("scopes_status")),
            sa.CheckConstraint("revision >= 1", name=t("scopes_revision")),
            sa.CheckConstraint("length(scope_id) BETWEEN 1 AND 128", name=t("scopes_id_len")),
            sa.Index(ix("ix_scopes_status_id"), "status", "scope_id"),
        )
        self.clients = sa.Table(
            t("clients"),
            md,
            sa.Column("client_id", sa.Text, primary_key=True),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("display_name", sa.Text, nullable=False),
            sa.Column("status", sa.Text, nullable=False),
            sa.Column("created_at", UtcDateTime, nullable=False),
            sa.Column("updated_at", UtcDateTime, nullable=False),
            sa.UniqueConstraint("name", name=t("clients_name_key")),
            sa.CheckConstraint("status IN ('active','disabled')", name=t("clients_status")),
        )
        self.api_keys = sa.Table(
            t("api_keys"),
            md,
            sa.Column("key_id", sa.Text, primary_key=True),
            sa.Column("client_id", sa.Text, fk("clients.client_id"), nullable=False),
            sa.Column("secret_hash", sa.LargeBinary, nullable=False),
            sa.Column("hash_algorithm", sa.Text, nullable=False),
            sa.Column("label", sa.Text, nullable=False),
            sa.Column("issuance_request_id", sa.Text, nullable=False),
            sa.Column("issuance_fingerprint", sa.LargeBinary, nullable=False),
            sa.Column("expires_at", UtcDateTime),
            sa.Column("revoked_at", UtcDateTime),
            sa.Column("created_at", UtcDateTime, nullable=False),
            sa.Column("created_by_key_id", sa.Text),
            sa.UniqueConstraint("client_id", "issuance_request_id", name=t("api_keys_request_key")),
            sa.Index(ix("ix_api_keys_client"), "client_id"),
        )
        self.api_key_admin_permissions = sa.Table(
            t("api_key_admin_permissions"),
            md,
            sa.Column("key_id", sa.Text, fk("api_keys.key_id"), primary_key=True),
            sa.Column("permission", sa.Text, primary_key=True),
        )
        self.api_key_scope_grants = sa.Table(
            t("api_key_scope_grants"),
            md,
            sa.Column("key_id", sa.Text, fk("api_keys.key_id"), primary_key=True),
            sa.Column("scope_id", sa.Text, fk("scopes.scope_id"), primary_key=True),
            sa.Column("permission", sa.Text, primary_key=True),
            sa.Index(ix("ix_scope_grants_scope"), "scope_id"),
        )
        self.admin_events = sa.Table(
            t("admin_events"),
            md,
            sa.Column("event_id", big_pk(), primary_key=True, autoincrement=True),
            sa.Column("occurred_at", UtcDateTime, nullable=False),
            sa.Column("action", sa.Text, nullable=False),
            sa.Column("actor_key_id", sa.Text),
            sa.Column("actor_label", sa.Text),
            sa.Column("target_type", sa.Text, nullable=False),
            sa.Column("target_id", sa.Text, nullable=False),
            sa.Column("scope_id", sa.Text),
            sa.Column("detail", json_type(), nullable=False),
            sa.Index(ix("ix_admin_events_time"), "occurred_at"),
            sa.Index(ix("ix_admin_events_target"), "target_type", "target_id"),
        )


@lru_cache(maxsize=2)
def tables_for(backend: Backend) -> Tables:
    return Tables(backend)
