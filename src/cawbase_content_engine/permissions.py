"""Permission catalog (17: 8 from A01, 2 from A02, 1 from A03, 6 from A05) and the delegation rules (D-27)."""

from __future__ import annotations

from enum import StrEnum


class ScopePermission(StrEnum):
    SCOPE_READ = "scope:read"
    SCOPE_WRITE = "scope:write"
    PROFILES_READ = "profiles:read"
    PROFILES_WRITE = "profiles:write"
    ANALYSIS_EXECUTE = "analysis:execute"
    FILES_WRITE = "files:write"
    FILES_READ = "files:read"
    JOBS_READ = "jobs:read"
    JOBS_CANCEL = "jobs:cancel"
    RESULTS_READ = "results:read"
    DATA_DELETE = "data:delete"


class AdminPermission(StrEnum):
    ADMIN_SCOPES_READ = "admin:scopes:read"
    ADMIN_SCOPES_WRITE = "admin:scopes:write"
    ADMIN_CLIENTS_READ = "admin:clients:read"
    ADMIN_CLIENTS_WRITE = "admin:clients:write"
    ADMIN_GRANTS_WRITE = "admin:grants:write"
    ADMIN_DIAGNOSTICS_READ = "admin:diagnostics:read"


ALL_ADMIN_PERMISSIONS = frozenset(AdminPermission)
ALL_SCOPE_PERMISSIONS = frozenset(ScopePermission)
