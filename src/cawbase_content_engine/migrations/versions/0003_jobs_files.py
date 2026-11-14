"""A05: files, jobs, attempts, artifacts, tombstones, idempotency, scope fences and usage counters.

Additive (expand-only): no existing object is altered or removed and no data is migrated or purged.
``schema_level`` becomes 3; ``min_runtime_level`` stays 1 (see docs/jobs-files-and-lifecycle.md, section 18).
The table definitions are shared with the runtime (``persistence/a05_schema.py``) and frozen at this revision.

Revision ID: 0003
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from cawbase_content_engine._version import __version__
from cawbase_content_engine.migrations.ns import namespace
from cawbase_content_engine.persistence.a05_schema import build_a05

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_ORDER = (
    "stored_files",
    "file_validations",
    "jobs",
    "job_attempts",
    "job_artifacts",
    "storage_tombstones",
    "idempotency_records",
    "scope_job_fences",
    "scope_usage",
    "engine_usage",
)


def upgrade() -> None:
    bind = op.get_bind()
    ns = namespace(bind)
    md = sa.MetaData(schema=ns.schema)
    # stub of the existing parent table so the foreign keys resolve; it is not created
    sa.Table(ns.table("scopes"), md, sa.Column("scope_id", sa.Text, primary_key=True))
    tables = build_a05(md, ns)
    for name in _ORDER:
        tables[name].create(bind, checkfirst=False)
    now = datetime.now(UTC)
    if bind.dialect.name == "sqlite":
        now = now.replace(tzinfo=None)
    state = ns.qualified("engine_state")
    op.execute(
        sa.text(
            f"UPDATE {state} SET schema_level = 3, package_version_at_last_migration = :v, updated_at = :now"  # noqa: S608
        ).bindparams(sa.bindparam("now", now, type_=sa.DateTime(timezone=True)), v=__version__)
    )
