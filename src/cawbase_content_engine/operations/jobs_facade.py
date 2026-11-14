"""A05 methods of ``ScopedOperations`` (authorization, host-transaction rules, dispatch). Lazy imports only."""

from __future__ import annotations

from collections.abc import AsyncIterable, Collection
from typing import TYPE_CHECKING, Any

from .. import validation as v
from ..contexts import ScopeContext
from ..errors import IncompatibleTransactionError, PermissionDeniedError, ResourceNotAccessibleError
from ..models import Page, UpsertResult
from ..permissions import ScopePermission
from ..transactions import HostTransaction
from .common import page_size

if TYPE_CHECKING:
    from ..engine import ContentEngine
    from ..jobs.models import JobAccepted, JobInfo, StoredFile
    from ..jobs.spool import SpooledStream


class JobOperations:
    _e: ContentEngine
    _ctx: ScopeContext
    _tx: HostTransaction | None

    def _gate(self, scope_id: str, perm: ScopePermission, *, host_ok: bool) -> None:
        v.scope_id(scope_id)
        if scope_id != self._ctx.scope_id:
            raise ResourceNotAccessibleError()
        if perm not in self._ctx.permissions:
            raise PermissionDeniedError()
        if self._tx is not None and not host_ok:  # storage I/O and transfers never run inside the host's transaction
            raise IncompatibleTransactionError("This operation cannot run inside a host transaction.")

    # ---- files -----------------------------------------------------------------------------------
    async def upload_file(
        self,
        scope_id: str,
        chunks: AsyncIterable[bytes],
        *,
        idempotency_key: str,
        expected_sha256: str | None = None,
        declared_size: int | None = None,
    ) -> UpsertResult[StoredFile]:
        self._gate(scope_id, ScopePermission.FILES_WRITE, host_ok=False)
        from ..jobs import files

        return await files.upload_file(
            self._e,
            scope_id,
            chunks,
            idempotency_key=idempotency_key,
            expected_sha256=expected_sha256,
            declared_size=declared_size,
        )

    async def register_external_file(
        self, scope_id: str, *, resolver_id: str, ref: str, idempotency_key: str
    ) -> UpsertResult[StoredFile]:
        self._gate(scope_id, ScopePermission.FILES_WRITE, host_ok=False)
        from ..jobs import files

        return await files.register_external_file(
            self._e, scope_id, resolver_id=resolver_id, ref=ref, idempotency_key=idempotency_key
        )

    async def get_file(self, scope_id: str, file_id: str) -> StoredFile:
        self._gate(scope_id, ScopePermission.FILES_READ, host_ok=True)
        from ..jobs import files

        return await files.get_file(self._e, scope_id, file_id, tx=self._tx)

    async def list_files(
        self, scope_id: str, *, state: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> Page[StoredFile]:
        self._gate(scope_id, ScopePermission.FILES_READ, host_ok=True)
        from ..jobs import files

        lim = self._e.config.limits
        items, nxt = await files.list_files(
            self._e,
            scope_id,
            state=state,
            limit=page_size(limit, lim.default_page_size, lim.max_page_size),
            cursor=cursor,
            tx=self._tx,
        )
        return Page(items=items, next_cursor=nxt)

    async def open_file_content(self, scope_id: str, file_id: str) -> SpooledStream:
        self._gate(scope_id, ScopePermission.FILES_READ, host_ok=False)
        from ..jobs import files

        return await files.open_file_content(self._e, scope_id, file_id)

    async def delete_file(self, scope_id: str, file_id: str) -> StoredFile:
        self._gate(scope_id, ScopePermission.DATA_DELETE, host_ok=True)
        from ..jobs import files

        return await files.delete_file(self._e, scope_id, file_id, tx=self._tx)

    # ---- jobs ------------------------------------------------------------------------------------
    async def submit_document_analysis(
        self,
        scope_id: str,
        *,
        file_id: str,
        idempotency_key: str,
        profile_id: str | None = None,
        version_number: int | None = None,
        capabilities: Collection[str] = ("classification", "detection"),
        context: str | None = None,
    ) -> JobAccepted:
        # with a context object the call writes to storage: not allowed inside a host transaction
        self._gate(scope_id, ScopePermission.ANALYSIS_EXECUTE, host_ok=context is None)
        from ..jobs import ops

        return await ops.submit(
            self._e,
            scope_id,
            operation="document_analysis",
            idempotency_key=idempotency_key,
            file_id=file_id,
            profile_id=profile_id,
            version_number=version_number,
            capabilities=capabilities,
            context=context,
            tx=self._tx,
        )

    async def submit_text_analysis(
        self,
        scope_id: str,
        *,
        text: str,
        idempotency_key: str,
        profile_id: str | None = None,
        version_number: int | None = None,
        capabilities: Collection[str] = ("classification", "detection"),
        context: str | None = None,
    ) -> JobAccepted:
        self._gate(scope_id, ScopePermission.ANALYSIS_EXECUTE, host_ok=False)
        from ..jobs import ops

        return await ops.submit(
            self._e,
            scope_id,
            operation="text_analysis",
            idempotency_key=idempotency_key,
            text=text,
            profile_id=profile_id,
            version_number=version_number,
            capabilities=capabilities,
            context=context,
            tx=None,
        )

    async def get_job(self, scope_id: str, job_id: str) -> JobInfo:
        self._gate(scope_id, ScopePermission.JOBS_READ, host_ok=True)
        from ..jobs import ops

        return await ops.get_job(self._e, scope_id, job_id, tx=self._tx)

    async def list_jobs(
        self,
        scope_id: str,
        *,
        state: str | None = None,
        operation: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[JobInfo]:
        self._gate(scope_id, ScopePermission.JOBS_READ, host_ok=True)
        from ..jobs import ops

        lim = self._e.config.limits
        items, nxt = await ops.list_jobs(
            self._e,
            scope_id,
            state=state,
            operation=operation,
            limit=page_size(limit, lim.default_page_size, lim.max_page_size),
            cursor=cursor,
            tx=self._tx,
        )
        return Page(items=items, next_cursor=nxt)

    async def cancel_job(self, scope_id: str, job_id: str) -> JobInfo:
        self._gate(scope_id, ScopePermission.JOBS_CANCEL, host_ok=True)
        from ..jobs import ops

        return await ops.cancel_job(self._e, scope_id, job_id, tx=self._tx)

    async def get_job_result(self, scope_id: str, job_id: str) -> dict[str, Any]:
        self._gate(scope_id, ScopePermission.RESULTS_READ, host_ok=False)
        from ..jobs import ops

        return await ops.get_job_result(self._e, scope_id, job_id)

    async def open_job_artifact(self, scope_id: str, job_id: str, kind: str) -> SpooledStream:
        self._gate(scope_id, ScopePermission.RESULTS_READ, host_ok=False)
        from ..jobs import ops

        return await ops.open_job_artifact(self._e, scope_id, job_id, kind)

    async def delete_job_data(self, scope_id: str, job_id: str) -> JobInfo:
        self._gate(scope_id, ScopePermission.DATA_DELETE, host_ok=True)
        from ..jobs import ops

        return await ops.delete_job_data(self._e, scope_id, job_id, tx=self._tx)
