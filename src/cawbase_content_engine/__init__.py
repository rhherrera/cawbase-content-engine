"""cawbase-content-engine: independent foundation (scopes, API keys, persistence).

Importing this package has no side effects: no connections, no environment reads, no migrations,
no routes, no logging configuration. See docs/public-api.md.
"""

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:  # pragma: no cover - names resolved lazily at runtime (see __getattr__)
    from .inference import (
        AdapterDescriptor,
        AdapterState,
        AnalysisResult,
        ElementDefinition,
        InferenceAdapter,
        InferenceLimits,
        InferenceRequest,
        InferenceSetup,
        InferenceStatus,
        MockFixture,
        MockInferenceAdapter,
    )
    from .inference import (
        request_for as build_inference_request,
    )
    from .protection.markers import find_markers
    from .protection.types import (
        AnalysisRef,
        ContextInfo,
        InlineAnalysis,
        JobAnalysis,
        KeyProvider,
        KeyRing,
        KeyVersionUsage,
        NewFindingRegion,
        ProtectedGroup,
        ProtectedText,
        ProtectionCleanupSummary,
        ProtectionLimits,
        ProtectionPolicy,
        ProtectionSetup,
        RestoredText,
    )

_LAZY_INFERENCE = {
    "AdapterDescriptor": "AdapterDescriptor",
    "AdapterState": "AdapterState",
    "AnalysisResult": "AnalysisResult",
    "ElementDefinition": "ElementDefinition",
    "InferenceAdapter": "InferenceAdapter",
    "InferenceLimits": "InferenceLimits",
    "InferenceRequest": "InferenceRequest",
    "InferenceSetup": "InferenceSetup",
    "InferenceStatus": "InferenceStatus",
    "MockFixture": "MockFixture",
    "MockInferenceAdapter": "MockInferenceAdapter",
    "build_inference_request": "request_for",
}


_LAZY_PROTECTION = (
    "AnalysisRef",
    "ContextInfo",
    "InlineAnalysis",
    "JobAnalysis",
    "KeyProvider",
    "KeyRing",
    "KeyVersionUsage",
    "NewFindingRegion",
    "ProtectedGroup",
    "ProtectedText",
    "ProtectionCleanupSummary",
    "ProtectionLimits",
    "ProtectionPolicy",
    "ProtectionSetup",
    "RestoredText",
)


def __getattr__(name: str) -> Any:
    """A03 inference and A06 protection types load on first use, keeping ``import cawbase_content_engine`` light
    (D-33): neither the inference package nor ``cryptography`` is imported with the base package."""
    if name in _LAZY_PROTECTION:
        from .protection import types as _ptypes

        return getattr(_ptypes, name)
    if name == "find_markers":
        from .protection.markers import find_markers

        return find_markers
    target = _LAZY_INFERENCE.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import inference

    return getattr(inference, target)


def starter_catalog() -> StarterCatalog:
    """The packaged starter catalog of sensitive types (pure data; loaded lazily)."""
    from .catalog import starter_catalog as _load

    return _load()


__all__ = [
    "AnalysisRef",
    "ContextInfo",
    "InlineAnalysis",
    "JobAnalysis",
    "KeyProvider",
    "KeyRing",
    "KeyVersionUsage",
    "NewFindingRegion",
    "ProtectedGroup",
    "ProtectedText",
    "ProtectionCleanupSummary",
    "ProtectionLimits",
    "ProtectionPolicy",
    "ProtectionSetup",
    "RestoredText",
    "find_markers",
    "AdapterDescriptor",
    "AdapterState",
    "AnalysisResult",
    "ElementDefinition",
    "InferenceAdapter",
    "InferenceLimits",
    "InferenceRequest",
    "InferenceSetup",
    "InferenceStatus",
    "MockFixture",
    "MockInferenceAdapter",
    "build_inference_request",
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
