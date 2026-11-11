"""Authorized contexts.

These are trusted value objects, *not* unforgeable credentials: any code in the same process can
construct one (see docs/security-and-isolation.md, "Límite del contexto Python"). They are bound to
the engine instance that created them so that they cannot be mixed up between engines.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import PermissionDeniedError, ResourceNotAccessibleError
from .permissions import AdminPermission, ScopePermission


@dataclass(frozen=True)
class ScopeContext:
    scope_id: str
    permissions: frozenset[ScopePermission]
    actor: str | None = None
    key_id: str | None = None
    _token: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class AdminContext:
    permissions: frozenset[AdminPermission]
    scope_grants: Mapping[str, frozenset[ScopePermission]] = field(default_factory=dict)
    actor: str | None = None
    key_id: str | None = None
    _token: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Result of authenticating an API key (used by the HTTP adapter)."""

    key_id: str
    client_id: str
    admin_permissions: frozenset[AdminPermission]
    scope_grants: Mapping[str, frozenset[ScopePermission]]
    _token: Any = field(default=None, repr=False, compare=False)

    def scope_context(self, scope_id: str) -> ScopeContext:
        """No grant of any kind on ``scope_id`` => not accessible (same as a missing scope)."""
        perms = self.scope_grants.get(scope_id)
        if not perms:
            raise ResourceNotAccessibleError()
        return ScopeContext(scope_id, perms, actor=f"key:{self.key_id}", key_id=self.key_id, _token=self._token)

    def admin_context(self) -> AdminContext:
        if not self.admin_permissions:
            raise PermissionDeniedError()
        return AdminContext(
            self.admin_permissions,
            dict(self.scope_grants),
            actor=f"key:{self.key_id}",
            key_id=self.key_id,
            _token=self._token,
        )
