"""A05 tables (jobs, files, artifacts, idempotency, fences, usage, tombstones).

One definition shared by ``Tables`` (runtime) and by migration ``0003`` (frozen at this revision: any later
change to these definitions needs its own revision). ``scope_id`` is part of every primary and foreign key.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from .dialect import Namespace
from .types import UtcDateTime, json_type

BIG = sa.BigInteger


def _ts(name: str, *, nullable: bool = True) -> sa.Column[Any]:
    return sa.Column(name, UtcDateTime, nullable=nullable)


def build_a05(md: sa.MetaData, ns: Namespace) -> dict[str, sa.Table]:
    t, ix = ns.table, ns.index

    def scope_col() -> sa.Column[str]:
        return sa.Column("scope_id", sa.Text, sa.ForeignKey(f"{ns.qualified('scopes')}.scope_id"), primary_key=True)

    def purge_cols() -> list[sa.Column[object]]:
        return [
            sa.Column("delete_after", UtcDateTime),
            sa.Column("seal_confirmed_at", UtcDateTime),
            sa.Column("writers_released_at", UtcDateTime),
            sa.Column("purge_token", sa.Text),
            sa.Column("purge_claim_expires_at", UtcDateTime),
            sa.Column("physically_deleted_at", UtcDateTime),
            sa.Column("charged_bytes", BIG, nullable=False, server_default="0"),
        ]

    out: dict[str, sa.Table] = {}
    out["stored_files"] = sa.Table(
        t("stored_files"),
        md,
        scope_col(),
        sa.Column("file_id", sa.Text, primary_key=True),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("integrity_failure", sa.Text),
        sa.Column("storage_backend_id", sa.Text),
        sa.Column("storage_key", sa.Text),
        sa.Column("write_state", sa.Text),
        sa.Column("resolver_id", sa.Text),
        sa.Column("external_ref", sa.Text),
        sa.Column("external_version", sa.Text),
        sa.Column("size_bytes", BIG),
        sa.Column("sha256", sa.LargeBinary),
        sa.Column("detected_format", sa.Text),
        sa.Column("reserved_bytes", BIG, nullable=False, server_default="0"),
        sa.Column("upload_generation", sa.Integer, nullable=False, server_default="1"),
        _ts("upload_deadline_at"),
        sa.Column("link_job_id", sa.Text),
        _ts("link_deadline_at"),
        _ts("reserved_at", nullable=False),
        _ts("received_at"),
        sa.Column("retention_s", sa.Integer),
        _ts("expires_at"),
        _ts("abandoned_at"),
        _ts("deleted_at"),
        _ts("end_of_life_at"),
        _ts("file_metadata_expires_at"),
        *purge_cols(),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("file_id", name=t("stored_files_file_id_uq")),
        sa.CheckConstraint(
            "state IN ('reserved','stored','abandoned','deleted','expired','lost')", name=t("stored_files_state")
        ),
        sa.Index(ix("ix_stored_files_purge"), "state", "delete_after"),
        sa.Index(ix("ix_stored_files_expires"), "expires_at"),
        sa.Index(ix("ix_stored_files_meta"), "file_metadata_expires_at"),
        sa.Index(ix("ix_stored_files_scope"), "scope_id", "state", "created_at"),
        sa.Index(
            ix("ix_stored_files_key"),
            "storage_backend_id",
            "storage_key",
            unique=True,
            sqlite_where=sa.text("storage_key IS NOT NULL"),
            postgresql_where=sa.text("storage_key IS NOT NULL"),
        ),
    )
    out["file_validations"] = sa.Table(
        t("file_validations"),
        md,
        scope_col(),
        sa.Column("file_id", sa.Text, primary_key=True),
        sa.Column("validation_identity", sa.Text, primary_key=True),
        sa.Column("validator_id", sa.Text, nullable=False),
        sa.Column("validator_version", sa.Text, nullable=False),
        sa.Column("validator_config_sha256", sa.Text, nullable=False),
        sa.Column("result", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("job_id", sa.Text),
        sa.Column("attempt_no", sa.Integer),
        _ts("validated_at", nullable=False),
        sa.CheckConstraint("result IN ('passed','rejected')", name=t("file_validations_result")),
    )
    out["jobs"] = sa.Table(
        t("jobs"),
        md,
        scope_col(),
        sa.Column("job_id", sa.Text, primary_key=True),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("phase", sa.Text, nullable=False),
        sa.Column("input_file_id", sa.Text, nullable=False),
        sa.Column("context_file_id", sa.Text),
        sa.Column("validation_identity", sa.Text),
        sa.Column("profile_id", sa.Text, nullable=False),
        sa.Column("version_id", sa.Text, nullable=False),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("profile_content_hash", sa.Text, nullable=False),
        sa.Column("selection", sa.Text, nullable=False),
        sa.Column("capabilities", json_type(), nullable=False),
        sa.Column("execution_snapshot", json_type(), nullable=False),
        sa.Column("execution_snapshot_hash", sa.Text, nullable=False),
        sa.Column("requirements_key", sa.Text, nullable=False),
        sa.Column("request_fingerprint", sa.Text, nullable=False),
        sa.Column("fence_generation", sa.Integer, nullable=False),
        sa.Column("attempt_no", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("lease_token", sa.Text),
        sa.Column("lease_owner", sa.Text),
        _ts("attempt_started_at"),
        _ts("attempt_deadline_at"),
        _ts("lease_expires_at"),
        _ts("heartbeat_at"),
        _ts("available_at", nullable=False),
        _ts("accepted_at", nullable=False),
        _ts("started_at"),
        _ts("terminated_at"),
        _ts("deadline_at", nullable=False),
        _ts("cancel_requested_at"),
        sa.Column("cancel_reason", sa.Text),
        sa.Column("failure_code", sa.Text),
        sa.Column("last_error_code", sa.Text),
        sa.Column("published_attempt_no", sa.Integer),
        sa.Column("result_artifact_id", sa.Text),
        sa.Column("source_map_artifact_id", sa.Text),
        sa.Column("data_state", sa.Text, nullable=False, server_default="none"),
        sa.Column("result_retention_s", sa.Integer, nullable=False),
        _ts("result_expires_at"),
        sa.Column("metadata_retention_s", sa.Integer, nullable=False),
        _ts("metadata_expires_at"),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("job_id", name=t("jobs_job_id_uq")),
        sa.CheckConstraint(
            "state IN ('queued','running','retry_wait','succeeded','failed','cancelled')", name=t("jobs_state")
        ),
        sa.CheckConstraint("data_state IN ('none','available','expired','deleted','lost')", name=t("jobs_data_state")),
        sa.CheckConstraint(
            "(state IN ('succeeded','failed','cancelled')) = (terminated_at IS NOT NULL)", name=t("jobs_terminal")
        ),
        sa.Index(ix("ix_jobs_claim"), "state", "requirements_key", "available_at"),
        sa.Index(ix("ix_jobs_lease"), "state", "lease_expires_at"),
        sa.Index(ix("ix_jobs_scope"), "scope_id", "state", "accepted_at"),
        sa.Index(ix("ix_jobs_meta"), "metadata_expires_at"),
    )
    out["job_attempts"] = sa.Table(
        t("job_attempts"),
        md,
        scope_col(),
        sa.Column("job_id", sa.Text, primary_key=True),
        sa.Column("attempt_no", sa.Integer, primary_key=True),
        sa.Column("worker_id", sa.Text),
        sa.Column("lease_token_hash", sa.Text),
        _ts("attempt_started_at", nullable=False),
        _ts("attempt_deadline_at", nullable=False),
        _ts("ended_at"),
        sa.Column("outcome", sa.Text),
        sa.Column("error_code", sa.Text),
    )
    out["job_artifacts"] = sa.Table(
        t("job_artifacts"),
        md,
        scope_col(),
        sa.Column("artifact_id", sa.Text, primary_key=True),
        sa.Column("job_id", sa.Text, nullable=False),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("storage_backend_id", sa.Text, nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("write_state", sa.Text, nullable=False),
        sa.Column("reserved_bytes", BIG, nullable=False, server_default="0"),
        _ts("abort_after"),
        sa.Column("size_bytes", BIG),
        sa.Column("sha256", sa.LargeBinary),
        _ts("reserved_at", nullable=False),
        _ts("written_at"),
        _ts("abandoned_at"),
        _ts("expires_at"),
        *purge_cols(),
        sa.CheckConstraint(
            "state IN ('reserved','written','published','discarded','expired','deleted','lost')",
            name=t("job_artifacts_state"),
        ),
        sa.Index(ix("ix_job_artifacts_purge"), "state", "delete_after"),
        sa.Index(ix("ix_job_artifacts_job"), "scope_id", "job_id"),
        sa.Index(ix("ix_job_artifacts_key"), "storage_backend_id", "storage_key", unique=True),
    )
    out["storage_tombstones"] = sa.Table(
        t("storage_tombstones"),
        md,
        scope_col(),
        sa.Column("tombstone_id", sa.Text, primary_key=True),
        sa.Column("storage_backend_id", sa.Text, nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("write_state", sa.Text, nullable=False),
        _ts("seal_confirmed_at"),
        sa.Column("charged_bytes", BIG, nullable=False),
        _ts("delete_after", nullable=False),
        sa.Column("purge_token", sa.Text),
        _ts("purge_claim_expires_at"),
        _ts("created_at", nullable=False),
        sa.Index(ix("ix_tombstones_key"), "storage_backend_id", "storage_key", unique=True),
        sa.Index(ix("ix_tombstones_purge"), "delete_after"),
    )
    out["idempotency_records"] = sa.Table(
        t("idempotency_records"),
        md,
        scope_col(),
        sa.Column("operation", sa.Text, primary_key=True),
        sa.Column("key_hash", sa.Text, primary_key=True),
        sa.Column("request_fingerprint", sa.Text),
        sa.Column("job_id", sa.Text),
        sa.Column("file_id", sa.Text),
        sa.Column("upload_generation", sa.Integer, nullable=False, server_default="1"),
        _ts("created_at", nullable=False),
        _ts("expires_at"),
        sa.Index(ix("ix_idempotency_expires"), "expires_at"),
    )
    out["scope_job_fences"] = sa.Table(
        t("scope_job_fences"),
        md,
        scope_col(),
        sa.Column("observed_scope_revision", sa.Integer, nullable=False),
        sa.Column("disable_generation", sa.Integer, nullable=False),
        _ts("updated_at", nullable=False),
    )

    def usage_cols() -> list[sa.Column[Any]]:
        return [
            sa.Column(n, BIG, nullable=False, server_default="0")
            for n in (
                "reserved_bytes",
                "stored_bytes",
                "uncertain_bytes",
                "open_reservations",
                "pending_seals",
                "sealed_keys",
                "active_jobs",
                "running_jobs",
            )
        ]

    out["scope_usage"] = sa.Table(
        t("scope_usage"),
        md,
        scope_col(),
        *usage_cols(),
        sa.Column("live_files", BIG, nullable=False, server_default="0"),
        _ts("updated_at", nullable=False),
        sa.CheckConstraint(
            "reserved_bytes >= 0 AND stored_bytes >= 0 AND uncertain_bytes >= 0 AND open_reservations >= 0 "
            "AND pending_seals >= 0 AND sealed_keys >= 0 AND active_jobs >= 0 AND running_jobs >= 0 "
            "AND live_files >= 0",
            name=t("scope_usage_nonneg"),
        ),
    )
    out["engine_usage"] = sa.Table(
        t("engine_usage"),
        md,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        *usage_cols(),
        _ts("updated_at", nullable=False),
        sa.CheckConstraint("id = 1", name=t("engine_usage_single")),
        sa.CheckConstraint(
            "reserved_bytes >= 0 AND stored_bytes >= 0 AND uncertain_bytes >= 0 AND open_reservations >= 0 "
            "AND pending_seals >= 0 AND sealed_keys >= 0 AND active_jobs >= 0 AND running_jobs >= 0",
            name=t("engine_usage_nonneg"),
        ),
    )
    return out
