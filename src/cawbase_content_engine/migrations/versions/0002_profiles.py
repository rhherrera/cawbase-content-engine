"""A02: profiles, categories, sensitive types, published versions and the per-scope default.

Additive (expand-only): no existing object is altered or removed, no data is migrated and no profile is
created for existing scopes. ``schema_level`` becomes 2; ``min_runtime_level`` stays 1.

Revision ID: 0002
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from cawbase_content_engine._version import __version__
from cawbase_content_engine.migrations.ns import namespace

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ns = namespace(bind)
    t, ix, schema = ns.table, ns.index, ns.schema
    json_t = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    ts = sa.DateTime(timezone=True)

    def fk(target: str) -> sa.ForeignKey:
        table, col = target.split(".")
        return sa.ForeignKey(f"{ns.qualified(table)}.{col}")

    profile_fk = [ns.qualified("profiles") + ".scope_id", ns.qualified("profiles") + ".profile_id"]

    op.create_table(
        t("profiles"),
        sa.Column("scope_id", sa.Text, fk("scopes.scope_id"), primary_key=True),
        sa.Column("profile_id", sa.Text, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("deleted_at", ts),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("draft_revision", sa.Integer, nullable=False),
        sa.Column("ranking_mode", sa.Text, nullable=False),
        sa.Column("eligibility_threshold", sa.Double, nullable=False),
        sa.Column("tie_tolerance", sa.Double, nullable=False),
        sa.Column("ranking_revision", sa.Integer, nullable=False),
        sa.Column("current_version_number", sa.Integer),
        sa.Column("catalog_id", sa.Text),
        sa.Column("catalog_version", sa.Integer),
        sa.Column("catalog_copied_at", ts),
        sa.Column("created_at", ts, nullable=False),
        sa.Column("updated_at", ts, nullable=False),
        sa.CheckConstraint("length(profile_id) BETWEEN 1 AND 64", name=t("profiles_id_len")),
        sa.CheckConstraint("language IN ('es','en')", name=t("profiles_language")),
        sa.CheckConstraint("status IN ('active','disabled','deleted')", name=t("profiles_status")),
        sa.CheckConstraint("(status = 'deleted') = (deleted_at IS NOT NULL)", name=t("profiles_deleted_at")),
        sa.CheckConstraint(
            "revision >= 1 AND draft_revision >= 1 AND ranking_revision >= 1", name=t("profiles_revisions")
        ),
        sa.CheckConstraint("ranking_mode IN ('ranking','resolution')", name=t("profiles_ranking_mode")),
        sa.CheckConstraint(
            "eligibility_threshold BETWEEN 0 AND 1 AND tie_tolerance BETWEEN 0 AND 1", name=t("profiles_ranking_range")
        ),
        sa.CheckConstraint("current_version_number IS NULL OR current_version_number >= 1", name=t("profiles_version")),
        sa.CheckConstraint(
            "(catalog_id IS NULL) = (catalog_version IS NULL) AND (catalog_id IS NULL) = (catalog_copied_at IS NULL)",
            name=t("profiles_catalog"),
        ),
        schema=schema,
    )
    op.create_index(ix("ix_profiles_status"), t("profiles"), ["scope_id", "status", "profile_id"], schema=schema)

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
        ]
        if is_cat:
            constraints.append(
                sa.CheckConstraint("priority IS NULL OR priority BETWEEN 1 AND 10000", name=t(f"{kind}_priority"))
            )
        op.create_table(
            t(kind),
            sa.Column("scope_id", sa.Text, primary_key=True),
            sa.Column("profile_id", sa.Text, primary_key=True),
            sa.Column(id_col, sa.Text, primary_key=True),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("name_key", sa.Text, nullable=False),
            sa.Column("definition", sa.Text, nullable=False),
            sa.Column("examples", json_t, nullable=False),
            sa.Column("translations", json_t),
            extra,
            sa.Column("state", sa.Text, nullable=False),
            sa.Column("deleted_at", ts),
            sa.Column("revision", sa.Integer, nullable=False),
            sa.Column("created_at", ts, nullable=False),
            sa.Column("updated_at", ts, nullable=False),
            *constraints,
            schema=schema,
        )
        op.create_index(
            ix(f"ux_{kind}_name"),
            t(kind),
            ["scope_id", "profile_id", "name_key"],
            unique=True,
            schema=schema,
            postgresql_where=sa.text("state <> 'deleted'"),
            sqlite_where=sa.text("state <> 'deleted'"),
        )
        op.create_index(ix(f"ix_{kind}_state"), t(kind), ["scope_id", "profile_id", "state", id_col], schema=schema)
        if is_cat:
            op.create_index(
                ix(f"ux_{kind}_priority"),
                t(kind),
                ["scope_id", "profile_id", "priority"],
                unique=True,
                schema=schema,
                postgresql_where=sa.text("state = 'enabled' AND priority IS NOT NULL"),
                sqlite_where=sa.text("state = 'enabled' AND priority IS NOT NULL"),
            )

    op.create_table(
        t("profile_versions"),
        sa.Column("scope_id", sa.Text, primary_key=True),
        sa.Column("profile_id", sa.Text, primary_key=True),
        sa.Column("version_number", sa.Integer, primary_key=True),
        sa.Column("version_id", sa.Text, nullable=False),
        sa.Column("source_draft_revision", sa.Integer, nullable=False),
        sa.Column("snapshot_schema_version", sa.Integer, nullable=False),
        sa.Column("snapshot", json_t, nullable=False),
        sa.Column("content_hash", sa.LargeBinary, nullable=False),
        sa.Column("snapshot_bytes", sa.Integer, nullable=False),
        sa.Column("capabilities", json_t, nullable=False),
        sa.Column("published_at", ts, nullable=False),
        sa.Column("published_by", sa.Text),
        sa.ForeignKeyConstraint(["scope_id", "profile_id"], profile_fk),
        sa.CheckConstraint("version_number >= 1", name=t("profile_versions_number")),
        sa.UniqueConstraint("version_id", name=t("profile_versions_version_id_key")),
        sa.UniqueConstraint("scope_id", "profile_id", "source_draft_revision", name=t("profile_versions_source_key")),
        schema=schema,
    )
    op.create_table(
        t("scope_profile_defaults"),
        sa.Column("scope_id", sa.Text, fk("scopes.scope_id"), primary_key=True),
        sa.Column("profile_id", sa.Text),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("updated_at", ts, nullable=False),
        sa.ForeignKeyConstraint(["scope_id", "profile_id"], profile_fk),
        sa.CheckConstraint("revision >= 1", name=t("scope_profile_defaults_revision")),
        schema=schema,
    )

    now = datetime.now(UTC)
    if bind.dialect.name == "sqlite":
        now = now.replace(tzinfo=None)
    state = ns.qualified("engine_state")
    op.execute(
        sa.text(
            f"UPDATE {state} SET schema_level = 2, package_version_at_last_migration = :v, updated_at = :now"  # noqa: S608
        ).bindparams(sa.bindparam("now", now, type_=ts), v=__version__)
    )
