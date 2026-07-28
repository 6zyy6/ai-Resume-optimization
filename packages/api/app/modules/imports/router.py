from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.imports.service import ImportService, ImportServiceError
from app.modules.tasks.service import TaskAdmission, TaskService, TaskServiceError
from app.integrations.storage import LocalStorage, MemoryStorage


router = APIRouter(
    prefix="/v1",
    tags=["imports"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 403, 404, 409, 422)
    },
)


class UploadTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    display_name: str = Field(min_length=1, max_length=255)
    mime: str = Field(min_length=1, max_length=128)
    size: int = Field(gt=0, le=10 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: Literal["resume_import"] = "resume_import"


class UploadTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file_id: str
    upload_url: str
    expires_in: int
    status: str


class FileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    status: str
    expires_at: str


class ImportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file_id: str


class ImportedFactInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=10000)


class ImportConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    facts: list[ImportedFactInput] = Field(default_factory=list, max_length=500)


class ImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    file_id: str
    status: str
    draft_facts: list[dict]
    fallback_reason: str | None
    task_id: str | None
    fact_ids: list[str] = Field(default_factory=list)


def get_import_service(request: Request) -> ImportService:
    return request.app.state.import_service


def get_task_service(request: Request) -> TaskService:
    return request.app.state.task_service


def _key(value: str | None, request: Request) -> str:
    if value:
        return value
    raise createApiError(
        "IDEMPOTENCY_KEY_REQUIRED",
        "Idempotency-Key is required",
        get_request_context(request).request_id,
        422,
    )


def _raise(request: Request, error: ImportServiceError) -> None:
    raise createApiError(
        error.code,
        error.message,
        get_request_context(request).request_id,
        error.status_code,
    )


def _import(row, fact_ids: list[str] | None = None) -> ImportResponse:
    return ImportResponse(
        id=row.id,
        file_id=row.file_id,
        status=row.status,
        draft_facts=row.draft_facts,
        fallback_reason=row.fallback_reason,
        task_id=row.task_id,
        fact_ids=fact_ids or [],
    )


@router.post("/files/upload-tokens", status_code=201, response_model=UploadTokenResponse)
async def create_upload_token(
    payload: UploadTokenRequest,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: ImportService = Depends(get_import_service),
) -> UploadTokenResponse:
    try:
        row, url = await service.create_upload_token(
            authenticated.user_id, **payload.model_dump()
        )
    except ImportServiceError as error:
        _raise(request, error)
    return UploadTokenResponse(
        file_id=row.id,
        upload_url=url,
        expires_in=600,
        status=row.status,
    )


@router.post("/files/{file_id}/confirm-upload", response_model=FileResponse)
async def confirm_upload(
    file_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: ImportService = Depends(get_import_service),
) -> FileResponse:
    try:
        row = await service.confirm_upload(
            authenticated.user_id, file_id, _key(idempotency_key, request)
        )
    except ImportServiceError as error:
        _raise(request, error)
    return FileResponse(
        id=row.id,
        status=row.status,
        expires_at=row.expires_at.isoformat(),
    )


@router.delete("/files/{file_id}", status_code=204)
async def delete_file(
    file_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: ImportService = Depends(get_import_service),
) -> Response:
    try:
        await service.delete_file(
            authenticated.user_id, file_id, _key(idempotency_key, request)
        )
    except ImportServiceError as error:
        _raise(request, error)
    return Response(status_code=204)


@router.post("/imports", status_code=202, response_model=ImportResponse)
async def create_import(
    payload: ImportCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: ImportService = Depends(get_import_service),
    task_service: TaskService = Depends(get_task_service),
) -> ImportResponse:
    try:
        key = _key(idempotency_key, request)
        row = await service.create_import(
            authenticated.user_id,
            payload.file_id,
            key,
        )
        task = await task_service.create_task(
            authenticated.user_id,
            task_type="parse_resume_import",
            queue="file.parse",
            trace_id=get_request_context(request).trace_id,
            idempotency_key=f"import:{key}",
            admission=TaskAdmission.unmetered(),
            resource_type="resume_import",
            resource_id=row.id,
            payload={"import_id": row.id},
        )
        row = await service.attach_task(authenticated.user_id, row.id, task.id)
        return _import(row)
    except (ImportServiceError, TaskServiceError) as error:
        _raise(request, error)


@router.get("/imports/{import_id}", response_model=ImportResponse)
async def get_import(
    import_id: str,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: ImportService = Depends(get_import_service),
) -> ImportResponse:
    row = await service.get_import(authenticated.user_id, import_id)
    if row is None:
        _raise(request, ImportServiceError("RESOURCE_NOT_FOUND", "Import not found", 404))
    return _import(row)


@router.post("/imports/{import_id}/confirm", response_model=ImportResponse)
async def confirm_import(
    import_id: str,
    payload: ImportConfirm,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: ImportService = Depends(get_import_service),
) -> ImportResponse:
    try:
        row, fact_ids = await service.confirm_import(
            authenticated.user_id,
            import_id,
            [item.model_dump() for item in payload.facts],
            _key(idempotency_key, request),
        )
    except ImportServiceError as error:
        _raise(request, error)
    return _import(row, fact_ids)


@router.put(
    "/storage/upload/{object_key:path}",
    status_code=204,
    include_in_schema=True,
)
async def signed_local_upload(
    object_key: str,
    request: Request,
    expires: int,
    signature: str,
    scope: str,
) -> Response:
    storage = request.app.state.storage
    if not isinstance(storage, (MemoryStorage, LocalStorage)) or not storage.verify(
        "upload", object_key, expires, scope, signature
    ):
        raise createApiError(
            "SIGNED_URL_INVALID",
            "Upload URL is invalid or expired",
            get_request_context(request).request_id,
            403,
        )
    try:
        mime, expected_size_text = scope.rsplit(":", 1)
        expected_size = int(expected_size_text)
    except (ValueError, TypeError) as error:
        raise createApiError(
            "SIGNED_URL_INVALID",
            "Upload URL constraints are invalid",
            get_request_context(request).request_id,
            403,
        ) from error
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) != expected_size:
                raise ValueError
        except ValueError as error:
            raise createApiError(
                "FILE_UPLOAD_MISMATCH",
                "Upload does not match its signed size",
                get_request_context(request).request_id,
                422,
            ) from error
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > expected_size:
            raise createApiError(
                "FILE_UPLOAD_MISMATCH",
                "Upload exceeds its signed size",
                get_request_context(request).request_id,
                422,
            )
    content = bytes(buffer)
    if (
        len(content) != expected_size
        or expected_size > 10 * 1024 * 1024
        or request.headers.get("content-type", "").lower() != mime.lower()
    ):
        raise createApiError(
            "FILE_UPLOAD_MISMATCH",
            "Upload does not match signed size and MIME constraints",
            get_request_context(request).request_id,
            422,
        )
    storage.put(object_key, content, mime)
    return Response(status_code=204)


@router.get(
    "/storage/download/{object_key:path}",
    responses={200: {"content": {"application/pdf": {}}}},
)
async def signed_local_download(
    object_key: str,
    request: Request,
    expires: int,
    signature: str,
    scope: str,
) -> Response:
    storage = request.app.state.storage
    if not isinstance(storage, (MemoryStorage, LocalStorage)) or not storage.verify(
        "download", object_key, expires, scope, signature
    ):
        raise createApiError(
            "SIGNED_URL_INVALID",
            "Download URL is invalid or expired",
            get_request_context(request).request_id,
            403,
        )
    stored = storage.get(object_key)
    if stored is None:
        raise createApiError(
            "RESOURCE_NOT_FOUND",
            "Stored object not found",
            get_request_context(request).request_id,
            404,
        )
    safe_name = scope.replace('"', "").replace("\r", "").replace("\n", "")
    return Response(
        content=stored.content,
        media_type=stored.mime,
        headers={
            "Content-Disposition": (
                "attachment; filename=\"resume.pdf\"; "
                f"filename*=UTF-8''{quote(safe_name)}"
            )
        },
    )
