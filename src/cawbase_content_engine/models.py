"""Result types (immutable)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, SecretStr

from .permissions import AdminPermission, ScopePermission

T = TypeVar("T")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class Scope(_Frozen):
    scope_id: str
    display_name: str
    description: str | None
    metadata: dict[str, Any]
    status: str
    revision: int
    created_at: datetime
    updated_at: datetime


class Client(_Frozen):
    client_id: str
    name: str
    display_name: str
    status: str
    created_at: datetime
    updated_at: datetime


class ApiKeyGrants(_Frozen):
    admin: frozenset[AdminPermission] = frozenset()
    scopes: Mapping[str, frozenset[ScopePermission]] = {}


class ApiKeyInfo(_Frozen):
    """Never contains the secret or its hash."""

    key_id: str
    client_id: str
    label: str
    grants: ApiKeyGrants
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


class IssuedApiKey(_Frozen):
    """``secret`` is the full ``cce1.<key_id>.<secret>`` string, present only on first issuance."""

    info: ApiKeyInfo
    secret: SecretStr | None
    replayed: bool = False


class UpsertResult(_Frozen, Generic[T]):
    value: T
    created: bool


class Page(_Frozen, Generic[T]):
    items: list[T]
    next_cursor: str | None


class SchemaStatus(_Frozen):
    present: bool
    schema_level: int | None = None
    min_runtime_level: int | None = None
    compatible: bool = False
    reason: str | None = None


class Diagnostics(_Frozen):
    package_version: str
    backend: str
    schema_level: int | None
    min_runtime_level: int | None
    required_schema_level: int
    runtime_level: int
    migration_mode: str
    state: str


class LivenessReport(_Frozen):
    status: str = "alive"


class ReadinessReport(_Frozen):
    ready: bool
    state: str


# ---- A02: profiles, categories and sensitive types -------------------------------------------------
class RankingConfig(_Frozen):
    mode: str
    eligibility_threshold: float
    tie_tolerance: float
    revision: int


class CatalogProvenance(_Frozen):
    catalog_id: str
    catalog_version: int
    copied_at: datetime


class Profile(_Frozen):
    scope_id: str
    profile_id: str
    display_name: str
    description: str | None
    language: str
    status: str
    revision: int
    draft_revision: int
    current_version_number: int | None
    is_default: bool
    ranking: RankingConfig
    catalog_provenance: CatalogProvenance | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class Examples(_Frozen):
    positive: tuple[str, ...] = ()
    negative: tuple[str, ...] = ()


class Translation(_Frozen):
    name: str
    definition: str


class Category(_Frozen):
    scope_id: str
    profile_id: str
    category_id: str
    name: str
    definition: str
    examples: Examples
    translations: dict[str, Translation] | None
    priority: int | None
    state: str
    revision: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class SensitiveType(_Frozen):
    scope_id: str
    profile_id: str
    type_id: str
    name: str
    definition: str
    examples: Examples
    translations: dict[str, Translation] | None
    origin_entry: str | None
    state: str
    revision: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class ProfileVersionSummary(_Frozen):
    scope_id: str
    profile_id: str
    version_id: str
    version_number: int
    source_draft_revision: int
    content_hash: str
    capabilities: dict[str, bool]
    snapshot_bytes: int
    published_at: datetime
    published_by: str | None


class ProfileVersion(ProfileVersionSummary):
    snapshot: dict[str, Any]


class DefaultProfile(_Frozen):
    scope_id: str
    profile_id: str | None
    revision: int
    updated_at: datetime | None


class ResolvedProfile(_Frozen):
    selection: str
    version: ProfileVersion


class StarterCatalogEntry(_Frozen):
    entry: str
    names: dict[str, str]
    definitions: dict[str, str]
    examples: dict[str, Examples]


class StarterCatalog(_Frozen):
    catalog_id: str
    catalog_version: int
    entries: tuple[StarterCatalogEntry, ...]
