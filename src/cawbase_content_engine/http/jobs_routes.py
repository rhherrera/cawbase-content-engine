"""A05 HTTP routes: the same operations as the Python API (D-07). No reclaim/heartbeat/publish/cleanup/purge here."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from ..errors import UploadAbortedError, ValidationFailedError

_NOSTORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_MIME = {
    b"%PDF-": ("pdf", "application/pdf"),
    b"PK\x03\x04": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
}


def install(
    router: APIRouter, engine: Any, ops: Callable[[Request, str], Coroutine[Any, Any, Any]], max_body: int
) -> None:
    from .router import _json, _parse_object, _read_body

    def idem(request: Request) -> str:
        key = request.headers.get("idempotency-key")
        if not key:
            raise ValidationFailedError("The Idempotency-Key header is required.", field="Idempotency-Key")
        return key

    def only(d: dict[str, Any], allowed: set[str]) -> None:
        if set(d) - allowed:
            raise ValidationFailedError("Unknown fields.")

    async def body_chunks(request: Request) -> AsyncIterator[bytes]:
        try:
            async for chunk in request.stream():
                yield chunk
        except ClientDisconnect:
            raise UploadAbortedError() from None

    def page_json(page: Any) -> dict[str, Any]:
        return {"items": [i.model_dump(mode="json") for i in page.items], "next_cursor": page.next_cursor}

    @router.post("/v1/scopes/{scope_id}/files")
    async def upload(scope_id: str, request: Request) -> Response:
        o = await ops(request, scope_id)
        declared = request.headers.get("content-length")
        declared_size = int(declared) if declared and declared.isdigit() else None
        res = await o.upload_file(
            scope_id,
            body_chunks(request),
            idempotency_key=idem(request),
            expected_sha256=request.headers.get("x-content-sha256"),
            declared_size=declared_size,
        )
        headers = dict(_NOSTORE)
        if not res.created:
            headers["Idempotent-Replay"] = "true"
        return _json(res.value.model_dump(mode="json"), 201 if res.created else 200, headers)

    @router.get("/v1/scopes/{scope_id}/files")
    async def list_files(
        scope_id: str, request: Request, state: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> Response:
        o = await ops(request, scope_id)
        return _json(page_json(await o.list_files(scope_id, state=state, limit=limit, cursor=cursor)), headers=_NOSTORE)

    @router.get("/v1/scopes/{scope_id}/files/{file_id}")
    async def get_file(scope_id: str, file_id: str, request: Request) -> Response:
        o = await ops(request, scope_id)
        return _json((await o.get_file(scope_id, file_id)).model_dump(mode="json"), headers=_NOSTORE)

    @router.get("/v1/scopes/{scope_id}/files/{file_id}/content")
    async def file_content(scope_id: str, file_id: str, request: Request) -> Response:
        o = await ops(request, scope_id)
        stream = await o.open_file_content(scope_id, file_id)  # verified in the spool BEFORE any header is sent
        with open(stream.path, "rb") as fh:  # noqa: ASYNC230
            head = fh.read(8)
        ext, mime = next((v for k, v in _MIME.items() if head.startswith(k)), ("bin", "application/octet-stream"))
        headers = {
            **_NOSTORE,
            "Content-Length": str(stream.size),
            "Content-Disposition": f'attachment; filename="{file_id}.{ext}"',
        }
        return StreamingResponse(stream, media_type=mime, headers=headers)

    @router.delete("/v1/scopes/{scope_id}/files/{file_id}")
    async def delete_file(scope_id: str, file_id: str, request: Request) -> Response:
        o = await ops(request, scope_id)
        return _json((await o.delete_file(scope_id, file_id)).model_dump(mode="json"), headers=_NOSTORE)

    async def submit(scope_id: str, request: Request, kind: str) -> Response:
        raw = await _read_body(request, max_body)
        o = await ops(request, scope_id)
        d = _parse_object(raw)
        key = idem(request)
        common = {"profile_id", "version_number", "capabilities", "context"}
        kwargs: dict[str, Any] = {
            "profile_id": d.get("profile_id"),
            "version_number": d.get("version_number"),
            "context": d.get("context"),
        }
        if "capabilities" in d:
            kwargs["capabilities"] = d["capabilities"]
        if kind == "document":
            only(d, common | {"file_id"})
            acc = await o.submit_document_analysis(scope_id, file_id=d.get("file_id"), idempotency_key=key, **kwargs)
        else:
            only(d, common | {"text"})
            acc = await o.submit_text_analysis(scope_id, text=d.get("text"), idempotency_key=key, **kwargs)
        headers = {**_NOSTORE, "Location": f"/v1/scopes/{scope_id}/jobs/{acc.job.job_id}"}
        if acc.replayed:
            headers["Idempotent-Replay"] = "true"
        return _json(acc.model_dump(mode="json"), 200 if acc.replayed else 202, headers)

    @router.post("/v1/scopes/{scope_id}/document-analysis-jobs")
    async def submit_document(scope_id: str, request: Request) -> Response:
        return await submit(scope_id, request, "document")

    @router.post("/v1/scopes/{scope_id}/text-analysis-jobs")
    async def submit_text(scope_id: str, request: Request) -> Response:
        return await submit(scope_id, request, "text")

    @router.get("/v1/scopes/{scope_id}/jobs")
    async def list_jobs(
        scope_id: str,
        request: Request,
        state: str | None = None,
        operation: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Response:
        o = await ops(request, scope_id)
        return _json(
            page_json(await o.list_jobs(scope_id, state=state, operation=operation, limit=limit, cursor=cursor)),
            headers=_NOSTORE,
        )

    @router.get("/v1/scopes/{scope_id}/jobs/{job_id}")
    async def get_job(scope_id: str, job_id: str, request: Request) -> Response:
        o = await ops(request, scope_id)
        return _json((await o.get_job(scope_id, job_id)).model_dump(mode="json"), headers=_NOSTORE)

    @router.post("/v1/scopes/{scope_id}/jobs/{job_id}/cancel")
    async def cancel(scope_id: str, job_id: str, request: Request) -> Response:
        o = await ops(request, scope_id)
        return _json((await o.cancel_job(scope_id, job_id)).model_dump(mode="json"), headers=_NOSTORE)

    async def artifact(scope_id: str, job_id: str, request: Request, kind: str, name: str) -> Response:
        o = await ops(request, scope_id)
        stream = await o.open_job_artifact(scope_id, job_id, kind)
        headers = {
            **_NOSTORE,
            "Content-Length": str(stream.size),
            "Content-Disposition": f'attachment; filename="{job_id}-{name}.json"',
        }
        return StreamingResponse(stream, media_type="application/json", headers=headers)

    @router.get("/v1/scopes/{scope_id}/jobs/{job_id}/result")
    async def result(scope_id: str, job_id: str, request: Request) -> Response:
        return await artifact(scope_id, job_id, request, "result", "result")

    @router.get("/v1/scopes/{scope_id}/jobs/{job_id}/source-map")
    async def source_map(scope_id: str, job_id: str, request: Request) -> Response:
        return await artifact(scope_id, job_id, request, "source_map", "source-map")

    @router.delete("/v1/scopes/{scope_id}/jobs/{job_id}/data")
    async def delete_data(scope_id: str, job_id: str, request: Request) -> Response:
        o = await ops(request, scope_id)
        return _json((await o.delete_job_data(scope_id, job_id)).model_dump(mode="json"), headers=_NOSTORE)
