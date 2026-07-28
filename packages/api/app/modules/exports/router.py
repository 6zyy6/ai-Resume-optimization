from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.exports.service import ExportResult, ExportService, ExportServiceError
from app.modules.tasks.service import TaskAdmission, TaskService, TaskServiceError


router = APIRouter(
    prefix="/v1/exports",
    tags=["exports"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 404, 409, 422)
    },
)


class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    resume_version_id: str
    template_version: Literal["clear-standard", "modern-whitespace"]
    download_name: str | None = Field(default=None, max_length=255)


class ExportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    resume_version_id: str
    template_version: str
    content_hash: str
    status: str
    task_id: str | None
    download_name: str
    download_url: str | None
    download_expires_in: int | None


def get_export_service(request: Request) -> ExportService:
    return request.app.state.export_service


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


def _raise(request: Request, error: ExportServiceError) -> None:
    raise createApiError(
        error.code,
        error.message,
        get_request_context(request).request_id,
        error.status_code,
    )


def _response(result: ExportResult) -> ExportResponse:
    row = result.export
    return ExportResponse(
        id=row.id,
        resume_version_id=row.resume_version_id,
        template_version=row.template_version,
        content_hash=row.content_hash,
        status=row.status,
        task_id=row.task_id,
        download_name=row.download_name,
        download_url=result.download_url,
        download_expires_in=result.download_expires_in,
    )


@router.post("", status_code=202, response_model=ExportResponse)
async def create_export(
    payload: ExportCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: ExportService = Depends(get_export_service),
    task_service: TaskService = Depends(get_task_service),
) -> ExportResponse:
    try:
        key = _key(idempotency_key, request)
        result = await service.create(
            authenticated.user_id,
            resume_version_id=payload.resume_version_id,
            template_version=payload.template_version,
            download_name=payload.download_name,
            idempotency_key=key,
        )
        task = await task_service.create_task(
            authenticated.user_id,
            task_type="render_resume_export",
            queue="file.export",
            trace_id=get_request_context(request).trace_id,
            idempotency_key=f"export:{key}",
            admission=TaskAdmission.unmetered(),
            resource_type="export",
            resource_id=result.export.id,
            payload={"export_id": result.export.id},
        )
        await service.attach_task(
            authenticated.user_id, result.export.id, task.id
        )
        result.export.task_id = task.id
        return _response(result)
    except (ExportServiceError, TaskServiceError) as error:
        _raise(request, error)


@router.get("/{export_id}", response_model=ExportResponse)
async def get_export(
    export_id: str,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: ExportService = Depends(get_export_service),
) -> ExportResponse:
    try:
        result = await service.get(authenticated.user_id, export_id)
    except ExportServiceError as error:
        _raise(request, error)
    if result is None:
        _raise(request, ExportServiceError("RESOURCE_NOT_FOUND", "Export not found", 404))
    return _response(result)
