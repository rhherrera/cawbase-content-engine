"""Initial schema: engine_state, scopes, clients, API keys, grants and admin events.

Revision ID: 0001
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from cawbase_content_engine._version import __version__
from cawbase_content_engine.migrations.ns import namespace

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ns = namespace(bind)
    t, ix, schema = ns.table, ns.index, ns.schema
    json_t = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    ts = sa.DateTime(timezone=True)
    big = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    def fk(target: str) -> sa.ForeignKey:
        table, col = target.split(".")
        return sa.ForeignKey(f"{ns.qualified(table)}.{col}")

    op.create_table(
        t("engine_state"),
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("schema_level", sa.Integer, nullable=False),
        sa.Column("min_runtime_level", sa.Integer, nullable=False),
        sa.Column("package_version_at_last_migration", sa.Text, nullable=False),
        sa.Column("admin_initialized_at", ts),
        sa.Column("admin_bootstrap_generation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", ts, nullable=False),
        sa.CheckConstraint("id = 1", name=t("engine_state_single_row")),
        sa.CheckConstraint("schema_level >= 1", name=t("engine_state_level_pos")),
        sa.CheckConstraint("admin_bootstrap_generation >= 0", name=t("engine_state_gen_nonneg")),
        schema=schema,
    )
    op.create_table(
        t("scopes"),
        sa.Column("scope_id", sa.Text, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("metadata", json_t, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("created_at", ts, nullable=False),
        sa.Column("updated_at", ts, nullable=False),
        sa.CheckConstraint("status IN ('active','disabled')", name=t("scopes_status")),
        sa.CheckConstraint("revision >= 1", name=t("scopes_revision")),
        sa.CheckConstraint("length(scope_id) BETWEEN 1 AND 128", name=t("scopes_id_len")),
        schema=schema,
    )
    op.create_index(ix("ix_scopes_status_id"), t("scopes"), ["status", "scope_id"], schema=schema)
    op.create_table(
        t("clients"),
        sa.Column("client_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", ts, nullable=False),
        sa.Column("updated_at", ts, nullable=False),
        sa.UniqueConstraint("name", name=t("clients_name_key")),
        sa.CheckConstraint("status IN ('active','disabled')", name=t("clients_status")),
        schema=schema,
    )
    op.create_table(
        t("api_keys"),
        sa.Column("key_id", sa.Text, primary_key=True),
        sa.Column("client_id", sa.Text, fk("clients.client_id"), nullable=False),
        sa.Column("secret_hash", sa.LargeBinary, nullable=False),
        sa.Column("hash_algorithm", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("issuance_request_id", sa.Text, nullable=False),
        sa.Column("issuance_fingerprint", sa.LargeBinary, nullable=False),
        sa.Column("expires_at", ts),
        sa.Column("revoked_at", ts),
        sa.Column("created_at", ts, nullable=False),
        sa.Column("created_by_key_id", sa.Text),
        sa.UniqueConstraint("client_id", "issuance_request_id", name=t("api_keys_request_key")),
        schema=schema,
    )
    op.create_index(ix("ix_api_keys_client"), t("api_keys"), ["client_id"], schema=schema)
    op.create_table(
        t("api_key_admin_permissions"),
        sa.Column("key_id", sa.Text, fk("api_keys.key_id"), primary_key=True),
        sa.Column("permission", sa.Text, primary_key=True),
        schema=schema,
    )
    op.create_table(
        t("api_key_scope_grants"),
        sa.Column("key_id", sa.Text, fk("api_keys.key_id"), primary_key=True),
        sa.Column("scope_id", sa.Text, fk("scopes.scope_id"), primary_key=True),
        sa.Column("permission", sa.Text, primary_key=True),
        schema=schema,
    )
    op.create_index(ix("ix_scope_grants_scope"), t("api_key_scope_grants"), ["scope_id"], schema=schema)
    op.create_table(
        t("admin_events"),
        sa.Column("event_id", big, primary_key=True, autoincrement=True),
        sa.Column("occurred_at", ts, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("actor_key_id", sa.Text),
        sa.Column("actor_label", sa.Text),
        sa.Column("target_type", sa.Text, nullable=False),
        sa.Column("target_id", sa.Text, nullable=False),
        sa.Column("scope_id", sa.Text),
        sa.Column("detail", json_t, nullable=False),
        schema=schema,
    )
    op.create_index(ix("ix_admin_events_time"), t("admin_events"), ["occurred_at"], schema=schema)
    op.create_index(ix("ix_admin_events_target"), t("admin_events"), ["target_type", "target_id"], schema=schema)

    now = datetime.now(UTC)
    if bind.dialect.name == "sqlite":
        now = now.replace(tzinfo=None)
    state = sa.table(
        t("engine_state"),
        sa.column("id"),
        sa.column("schema_level"),
        sa.column("min_runtime_level"),
        sa.column("package_version_at_last_migration"),
        sa.column("updated_at", ts),
        schema=schema,
    )
    op.bulk_insert(
        state,
        [
            {
                "id": 1,
                "schema_level": 1,
                "min_runtime_level": 1,
                "package_version_at_last_migration": __version__,
                "updated_at": now,
            }
        ],
    )
