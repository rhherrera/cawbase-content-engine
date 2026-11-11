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
    Client,
    Diagnostics,
    IssuedApiKey,
    LivenessReport,
    Page,
    ReadinessReport,
    SchemaStatus,
    Scope,
    UpsertResult,
)
from .permissions import AdminPermission, ScopePermission
from .persistence.sqlite_support import install_transaction_control
from .schema import check_schema
from .transactions import HostTransaction

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
    "check_schema",
    "create_initial_admin_key",
    "install_transaction_control",
    "recover_admin_access",
]
