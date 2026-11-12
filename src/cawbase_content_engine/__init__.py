"""cawbase-content-engine: independent foundation (scopes, API keys, persistence).

Importing this package has no side effects: no connections, no environment reads, no migrations,
no routes, no logging configuration. See docs/public-api.md.
"""

from ._version import __version__
from .bootstrap import SecretFileSink, create_initial_admin_key, recover_admin_access
from .config import EngineConfig, Limits
from .contexts import AdminContext, AuthenticatedPrincipal, ScopeContext
from .engine import AdminOperations, ContentEngine, ScopedOperations
from .errors import *  # noqa: F403
from .errors import EngineError
from .models import (
    ApiKeyGrants,
    ApiKeyInfo,
    CatalogProvenance,
    Category,
    Client,
    DefaultProfile,
    Diagnostics,
    Examples,
    IssuedApiKey,
    LivenessReport,
    Page,
    Profile,
    ProfileVersion,
    ProfileVersionSummary,
    RankingConfig,
    ReadinessReport,
    ResolvedProfile,
    SchemaStatus,
    Scope,
    SensitiveType,
    StarterCatalog,
    Translation,
    UpsertResult,
)
from .permissions import AdminPermission, ScopePermission
from .persistence.sqlite_support import install_transaction_control
from .schema import check_schema
from .transactions import HostTransaction


def starter_catalog() -> StarterCatalog:
    """The packaged starter catalog of sensitive types (pure data; loaded lazily)."""
    from .catalog import starter_catalog as _load

    return _load()


__all__ = [
    "AdminContext",
    "AdminOperations",
    "AdminPermission",
    "ApiKeyGrants",
    "ApiKeyInfo",
    "AuthenticatedPrincipal",
    "Client",
    "ContentEngine",
    "Diagnostics",
    "EngineConfig",
    "EngineError",
    "HostTransaction",
    "IssuedApiKey",
    "Limits",
    "LivenessReport",
    "Page",
    "ReadinessReport",
    "SchemaStatus",
    "Scope",
    "ScopeContext",
    "ScopedOperations",
    "ScopePermission",
    "SecretFileSink",
    "UpsertResult",
    "__version__",
    "Category",
    "CatalogProvenance",
    "DefaultProfile",
    "Examples",
    "Profile",
    "ProfileVersion",
    "ProfileVersionSummary",
    "RankingConfig",
    "ResolvedProfile",
    "SensitiveType",
    "StarterCatalog",
    "Translation",
    "check_schema",
    "create_initial_admin_key",
    "install_transaction_control",
    "recover_admin_access",
    "starter_catalog",
]
