"""Permission catalog (8 permissions) and the delegation rules (D-27)."""

from __future__ import annotations

from enum import StrEnum


class ScopePermission(StrEnum):
    SCOPE_READ = "scope:read"
    SCOPE_WRITE = "scope:write"


class AdminPermission(StrEnum):
    ADMIN_SCOPES_READ = "admin:scopes:read"
    ADMIN_SCOPES_WRITE = "admin:scopes:write"
    ADMIN_CLIENTS_READ = "admin:clients:read"
    ADMIN_CLIENTS_WRITE = "admin:clients:write"
    ADMIN_GRANTS_WRITE = "admin:grants:write"
    ADMIN_DIAGNOSTICS_READ = "admin:diagnostics:read"


ALL_ADMIN_PERMISSIONS = frozenset(AdminPermission)
ALL_SCOPE_PERMISSIONS = frozenset(ScopePermission)
