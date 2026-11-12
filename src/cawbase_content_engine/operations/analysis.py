"""Public A03 method of ``ScopedOperations`` (authorization, host-transaction rejection, dispatch)."""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from .. import validation as v
from ..contexts import ScopeContext
from ..errors import IncompatibleTransactionError, PermissionDeniedError, ResourceNotAccessibleError
from ..permissions import ScopePermission
from ..transactions import HostTransaction

if TYPE_CHECKING:
    from ..engine import ContentEngine
    from ..inference.results import AnalysisResult


class AnalysisOperations:
    _e: ContentEngine
    _ctx: ScopeContext
    _tx: HostTransaction | None

    async def analyze_text(
        self,
        scope_id: str,
        *,
        text: str,
        profile_id: str | None = None,
        version_number: int | None = None,
        capabilities: Collection[str] = ("classification", "detection"),
        context: str | None = None,
        request_id: str | None = None,
    ) -> AnalysisResult:
        """Analyze ``text`` with a pinned published profile version. Nothing is persisted."""
        v.scope_id(scope_id)
        if scope_id != self._ctx.scope_id:  # membership is validated; the id grants nothing
            raise ResourceNotAccessibleError()
        if ScopePermission.ANALYSIS_EXECUTE not in self._ctx.permissions:
            raise PermissionDeniedError()
        if self._tx is not None:  # rejected before anything else: a long call must not hold the host's transaction
            raise IncompatibleTransactionError("Text analysis cannot run inside a host transaction.")
        from ..inference.service import run_analysis

        extra: Any = request_id
        return await run_analysis(
            self._e,
            scope_id=scope_id,
            text=text,
            profile_id=profile_id,
            version_number=version_number,
            capabilities=capabilities,
            context=context,
            request_id=extra,
        )
