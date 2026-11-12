"""SQLAlchemy Core table definitions for both backends (fixed namespace)."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

import sqlalchemy as sa

from .dialect import Backend, Namespace
from .types import UtcDateTime, big_pk, json_type


class Tables:
    profiles: sa.Table
    categories: sa.Table
    sensitive_types: sa.Table
    profile_versions: sa.Table
    scope_profile_defaults: sa.Table

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

        self._a02_tables(md, ns, fk)

    def _a02_tables(self, md: sa.MetaData, ns: Namespace, fk: Callable[[str], sa.ForeignKey]) -> None:
        t, ix = ns.table, ns.index
        profile_fk = [ns.qualified("profiles") + ".scope_id", ns.qualified("profiles") + ".profile_id"]
        self.profiles = sa.Table(
            t("profiles"),
            md,
            sa.Column("scope_id", sa.Text, fk("scopes.scope_id"), primary_key=True),
            sa.Column("profile_id", sa.Text, primary_key=True),
            sa.Column("display_name", sa.Text, nullable=False),
            sa.Column("description", sa.Text),
            sa.Column("language", sa.Text, nullable=False),
            sa.Column("status", sa.Text, nullable=False),
            sa.Column("deleted_at", UtcDateTime),
            sa.Column("revision", sa.Integer, nullable=False),
            sa.Column("draft_revision", sa.Integer, nullable=False),
            sa.Column("ranking_mode", sa.Text, nullable=False),
            sa.Column("eligibility_threshold", sa.Double, nullable=False),
            sa.Column("tie_tolerance", sa.Double, nullable=False),
            sa.Column("ranking_revision", sa.Integer, nullable=False),
            sa.Column("current_version_number", sa.Integer),
            sa.Column("catalog_id", sa.Text),
            sa.Column("catalog_version", sa.Integer),
            sa.Column("catalog_copied_at", UtcDateTime),
            sa.Column("created_at", UtcDateTime, nullable=False),
            sa.Column("updated_at", UtcDateTime, nullable=False),
            sa.CheckConstraint("length(profile_id) BETWEEN 1 AND 64", name=t("profiles_id_len")),
            sa.CheckConstraint("language IN ('es','en')", name=t("profiles_language")),
            sa.CheckConstraint("status IN ('active','disabled','deleted')", name=t("profiles_status")),
            sa.CheckConstraint("(status = 'deleted') = (deleted_at IS NOT NULL)", name=t("profiles_deleted_at")),
            sa.CheckConstraint(
                "revision >= 1 AND draft_revision >= 1 AND ranking_revision >= 1", name=t("profiles_revisions")
            ),
            sa.CheckConstraint("ranking_mode IN ('ranking','resolution')", name=t("profiles_ranking_mode")),
            sa.CheckConstraint(
                "eligibility_threshold BETWEEN 0 AND 1 AND tie_tolerance BETWEEN 0 AND 1",
                name=t("profiles_ranking_range"),
            ),
            sa.CheckConstraint(
                "current_version_number IS NULL OR current_version_number >= 1", name=t("profiles_version")
            ),
            sa.CheckConstraint(
                "(catalog_id IS NULL) = (catalog_version IS NULL) "
                "AND (catalog_id IS NULL) = (catalog_copied_at IS NULL)",
                name=t("profiles_catalog"),
            ),
            sa.Index(ix("ix_profiles_status"), "scope_id", "status", "profile_id"),
        )
        for kind, id_col, extra in (
            ("profile_categories", "category_id", sa.Column("priority", sa.Integer)),
            ("profile_sensitive_types", "type_id", sa.Column("origin_entry", sa.Text)),
        ):
            is_cat = kind == "profile_categories"
            constraints: list[sa.schema.SchemaItem] = [
                sa.ForeignKeyConstraint(["scope_id", "profile_id"], profile_fk),
                sa.CheckConstraint(f"length({id_col}) BETWEEN 1 AND 64", name=t(f"{kind}_id_len")),
                sa.CheckConstraint("state IN ('enabled','disabled','deleted')", name=t(f"{kind}_state")),
                sa.CheckConstraint("(state = 'deleted') = (deleted_at IS NOT NULL)", name=t(f"{kind}_deleted_at")),
                sa.CheckConstraint("revision >= 1", name=t(f"{kind}_revision")),
                sa.Index(
                    ix(f"ux_{kind}_name"),
                    "scope_id",
                    "profile_id",
                    "name_key",
                    unique=True,
                    postgresql_where=sa.text("state <> 'deleted'"),
                    sqlite_where=sa.text("state <> 'deleted'"),
                ),
                sa.Index(ix(f"ix_{kind}_state"), "scope_id", "profile_id", "state", id_col),
            ]
            if is_cat:
                constraints += [
                    sa.CheckConstraint("priority IS NULL OR priority BETWEEN 1 AND 10000", name=t(f"{kind}_priority")),
                    sa.Index(
                        ix(f"ux_{kind}_priority"),
                        "scope_id",
                        "profile_id",
                        "priority",
                        unique=True,
                        postgresql_where=sa.text("state = 'enabled' AND priority IS NOT NULL"),
                        sqlite_where=sa.text("state = 'enabled' AND priority IS NOT NULL"),
                    ),
                ]
            table = sa.Table(
                t(kind),
                md,
                sa.Column("scope_id", sa.Text, primary_key=True),
                sa.Column("profile_id", sa.Text, primary_key=True),
                sa.Column(id_col, sa.Text, primary_key=True),
                sa.Column("name", sa.Text, nullable=False),
                sa.Column("name_key", sa.Text, nullable=False),
                sa.Column("definition", sa.Text, nullable=False),
                sa.Column("examples", json_type(), nullable=False),
                sa.Column("translations", json_type()),
                extra,
                sa.Column("state", sa.Text, nullable=False),
                sa.Column("deleted_at", UtcDateTime),
                sa.Column("revision", sa.Integer, nullable=False),
                sa.Column("created_at", UtcDateTime, nullable=False),
                sa.Column("updated_at", UtcDateTime, nullable=False),
                *constraints,
            )
            setattr(self, "categories" if is_cat else "sensitive_types", table)
        self.profile_versions = sa.Table(
            t("profile_versions"),
            md,
            sa.Column("scope_id", sa.Text, primary_key=True),
            sa.Column("profile_id", sa.Text, primary_key=True),
            sa.Column("version_number", sa.Integer, primary_key=True),
            sa.Column("version_id", sa.Text, nullable=False),
            sa.Column("source_draft_revision", sa.Integer, nullable=False),
            sa.Column("snapshot_schema_version", sa.Integer, nullable=False),
            sa.Column("snapshot", json_type(), nullable=False),
            sa.Column("content_hash", sa.LargeBinary, nullable=False),
            sa.Column("snapshot_bytes", sa.Integer, nullable=False),
            sa.Column("capabilities", json_type(), nullable=False),
            sa.Column("published_at", UtcDateTime, nullable=False),
            sa.Column("published_by", sa.Text),
            sa.ForeignKeyConstraint(["scope_id", "profile_id"], profile_fk),
            sa.CheckConstraint("version_number >= 1", name=t("profile_versions_number")),
            sa.UniqueConstraint("version_id", name=t("profile_versions_version_id_key")),
            sa.UniqueConstraint(
                "scope_id", "profile_id", "source_draft_revision", name=t("profile_versions_source_key")
            ),
        )
        self.scope_profile_defaults = sa.Table(
            t("scope_profile_defaults"),
            md,
            sa.Column("scope_id", sa.Text, fk("scopes.scope_id"), primary_key=True),
            sa.Column("profile_id", sa.Text),
            sa.Column("revision", sa.Integer, nullable=False),
            sa.Column("updated_at", UtcDateTime, nullable=False),
            sa.ForeignKeyConstraint(["scope_id", "profile_id"], profile_fk),
            sa.CheckConstraint("revision >= 1", name=t("scope_profile_defaults_revision")),
        )


@lru_cache(maxsize=2)
def tables_for(backend: Backend) -> Tables:
    return Tables(backend)
