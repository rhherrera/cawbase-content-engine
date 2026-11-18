"""A08: restoration bindings of protected PDF derivatives.

Additive (expand-only): no existing object is altered or removed and no data is migrated or purged.
``schema_level`` becomes 6; ``min_runtime_level`` stays 1 (see docs/pdf-protection.md, section 13).
The table definition is shared with the runtime (``persistence/a08_schema.py``) and frozen at this revision.

Revision ID: 0006
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from cawbase_content_engine._version import __version__
from cawbase_content_engine.migrations.ns import namespace
from cawbase_content_engine.persistence.a08_schema import build_a08

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ns = namespace(bind)
    md = sa.MetaData(schema=ns.schema)
    # stub of the existing parent table so the foreign key resolves; it is not created
    sa.Table(ns.table("scopes"), md, sa.Column("scope_id", sa.Text, primary_key=True))
    build_a08(md, ns)["pdf_derivatives"].create(bind, checkfirst=False)
    now = datetime.now(UTC)
    if bind.dialect.name == "sqlite":
        now = now.replace(tzinfo=None)
    state = ns.qualified("engine_state")
    op.execute(
        sa.text(
            f"UPDATE {state} SET schema_level = 6, package_version_at_last_migration = :v, updated_at = :now"  # noqa: S608
        ).bindparams(sa.bindparam("now", now, type_=sa.DateTime(timezone=True)), v=__version__)
    )
