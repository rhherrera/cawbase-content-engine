"""Operator-supplied setup objects and the A05 limits/retentions (docs/jobs-files-and-lifecycle.md, 12-13)."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..errors import InvalidConfigurationError

MiB = 1024 * 1024
DAY = 86400


@dataclass(frozen=True)
class JobsLimits:
    """Initial technical choices (E-A05-17), not measurements. Every value must be a positive number."""

    max_file_bytes: int = 25 * MiB
    max_pdf_pages: int = 100
    max_zip_entries: int = 1000
    max_docx_parts_processed: int = 300
    max_decompressed_total_bytes: int = 200 * MiB
    max_decompressed_entry_bytes: int = 50 * MiB
    max_compression_ratio: int = 100
    max_xml_depth: int = 64
    max_xml_nodes_total: int = 2_000_000
    max_table_nesting: int = 2
    max_pdf_objects: int = 200_000
    max_pdf_content_bytes_per_page: int = 8 * MiB
    max_form_xobject_depth: int = 8
    max_text_operators_per_page: int = 100_000
    max_units_per_document: int = 5000
    max_document_text_chars: int = 2_000_000
    max_document_findings: int = 50_000
    max_source_map_bytes: int = 32 * MiB
    max_result_bytes: int = 32 * MiB
    max_extraction_output_bytes: int = 64 * MiB
    extraction_timeout_s: float = 120.0
    extraction_cpu_s: int = 120
    extraction_memory_bytes: int = 1024 * MiB
    extraction_max_pids: int = 32
    unit_inference_concurrency: int = 4
    max_attempt_duration_s: float = 3600.0
    max_job_duration_s: float = 86400.0
    max_attempts: int = 5
    backoff_base_s: float = 30.0
    backoff_max_s: float = 900.0
    lease_ttl_s: float = 60.0
    heartbeat_interval_s: float = 20.0
    upload_timeout_s: float = 600.0
    upload_idle_timeout_s: float = 60.0
    submit_link_timeout_s: float = 600.0
    download_timeout_s: float = 600.0
    max_spool_bytes: int = 256 * MiB
    purge_claim_ttl_s: float = 300.0
    max_active_jobs_per_scope: int = 100
    max_active_jobs_total: int = 10_000
    max_running_jobs_per_scope: int = 4
    max_live_files_per_scope: int = 1000
    max_storage_bytes_per_scope: int = 2048 * MiB
    max_storage_bytes_total: int = 102_400 * MiB
    max_seals_per_scope: int = 10_000
    max_seals_total: int = 1_000_000
    cleanup_batch_size: int = 500
    admin_events_purge_batch_size: int = 1000

    @property
    def purge_grace_s(self) -> float:
        return self.download_timeout_s + 300.0

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise InvalidConfigurationError(f"Jobs limit '{name}' must be positive.")
        if self.heartbeat_interval_s > self.lease_ttl_s / 3:
            raise InvalidConfigurationError("heartbeat_interval_s must be at most lease_ttl_s / 3.")
        if self.admin_events_purge_batch_size > 10_000:
            raise InvalidConfigurationError("admin_events_purge_batch_size is out of range.")


@dataclass(frozen=True)
class Retention:
    """D-A05-01 defaults; configurable, never indefinite (maximum 3650 days)."""

    originals_s: int = 7 * DAY
    results_s: int = 7 * DAY
    temporary_s: int = 24 * 3600
    metadata_s: int = 30 * DAY
    admin_events_s: int = 90 * DAY

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or not (0 < value <= 3650 * DAY):
                raise InvalidConfigurationError(f"Retention '{name}' is out of range.")


@dataclass(frozen=True)
class JobsSetup:
    limits: JobsLimits = field(default_factory=JobsLimits)
    retention: Retention = field(default_factory=Retention)


# ---- storage protocol ---------------------------------------------------------------------


class StorageError(Exception):
    """Transient backend failure (never carries paths or content)."""


class StorageKeyExists(StorageError):
    """create-only violated, or the key is sealed."""


class StorageLimitExceeded(StorageError):
    """More bytes than ``max_bytes`` (or the cooperative ``abort_after`` passed)."""


class ObjectMissing(StorageError):
    """The object does not exist (or is sealed)."""


@dataclass(frozen=True)
class StorageDescriptor:
    backend_id: str
    durability: str = "fsync"
    transient_overhead_factor: int = 1
    create_only: bool = True
    seal: bool = True
    writer_release_evidence: bool = True
    inventory: bool = False


@dataclass(frozen=True)
class PutReceipt:
    size: int
    sha256: bytes


@dataclass(frozen=True)
class ObjectInfo:
    size: int


class ManagedStorage(Protocol):
    @property
    def descriptor(self) -> StorageDescriptor: ...
    async def put_new(
        self, key: str, chunks: AsyncIterable[bytes], *, max_bytes: int, abort_after: datetime
    ) -> PutReceipt: ...
    def open_read(self, key: str) -> AsyncIterator[bytes]: ...
    async def stat(self, key: str) -> ObjectInfo | None: ...
    async def delete(self, key: str) -> None: ...
    async def delete_and_seal(self, key: str) -> None: ...
    async def writers_released(self, key: str) -> bool: ...
    async def list_keys(self, prefix: str, *, after: str | None, limit: int) -> Sequence[str]: ...


@dataclass(frozen=True)
class ExternalObjectInfo:
    version_id: str
    size: int


@dataclass(frozen=True)
class ResolverDescriptor:
    resolver_id: str
    stable_versions: bool


class ExternalSourceResolver(Protocol):
    @property
    def descriptor(self) -> ResolverDescriptor: ...
    async def describe(self, ref: str) -> ExternalObjectInfo: ...
    def open_read(self, ref: str, version_id: str) -> AsyncIterator[bytes]: ...


@dataclass(frozen=True)
class StorageSetup:
    managed: ManagedStorage
    external_resolvers: Mapping[str, ExternalSourceResolver] = field(default_factory=dict)
    spool_dir: str | None = None

    def __post_init__(self) -> None:
        d = self.managed.descriptor
        if not (d.create_only and d.seal and d.writer_release_evidence):
            raise InvalidConfigurationError(
                "The managed storage lacks required capabilities (create-only, seal, evidence)."
            )
        for rid, r in self.external_resolvers.items():
            if r.descriptor.resolver_id != rid:
                raise InvalidConfigurationError("Resolver id mismatch.")


@dataclass(frozen=True)
class DocumentSetup:
    """``runner`` executes the extraction child. ``untrusted_input`` (default) forbids unconfined runners."""

    runner: Any
    untrusted_input: bool = True

    def __post_init__(self) -> None:
        profile = getattr(self.runner, "profile", None)
        if profile not in ("linux_container_reference", "host_provided", "unconfined_development"):
            raise InvalidConfigurationError("Unknown isolation profile.")
        if self.untrusted_input and profile == "unconfined_development":
            raise InvalidConfigurationError(
                "untrusted_input requires a verified isolation profile; 'unconfined_development' is development only."
            )
