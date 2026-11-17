"""A06: protection contexts, encrypted mappings and per-scope usage.

Additive (expand-only): no existing object is altered or removed and no data is migrated or purged.
``schema_level`` becomes 4; ``min_runtime_level`` stays 1 (see docs/text-protection.md, section 10).
The table definitions are shared with the runtime (``persistence/a06_schema.py``) and frozen at this revision.

Revision ID: 0004
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from cawbase_content_engine._version import __version__
from cawbase_content_engine.migrations.ns import namespace
from cawbase_content_engine.persistence.a06_schema import build_a06

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_ORDER = ("protection_contexts", "protection_mappings", "protection_scope_usage")


def upgrade() -> None:
    bind = op.get_bind()
    ns = namespace(bind)
    md = sa.MetaData(schema=ns.schema)
    # stub of the existing parent table so the foreign keys resolve; it is not created
    sa.Table(ns.table("scopes"), md, sa.Column("scope_id", sa.Text, primary_key=True))
    tables = build_a06(md, ns)
    for name in _ORDER:
        tables[name].create(bind, checkfirst=False)
    now = datetime.now(UTC)
    if bind.dialect.name == "sqlite":
        now = now.replace(tzinfo=None)
    state = ns.qualified("engine_state")
    op.execute(
        sa.text(
            f"UPDATE {state} SET schema_level = 4, package_version_at_last_migration = :v, updated_at = :now"  # noqa: S608
        ).bindparams(sa.bindparam("now", now, type_=sa.DateTime(timezone=True)), v=__version__)
    )
