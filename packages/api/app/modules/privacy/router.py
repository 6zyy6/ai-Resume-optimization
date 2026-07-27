from datetime import datetime

from fastapi import APIRouter, Body, Depends, Header, Request
from pydantic import BaseModel, ConfigDict

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import require_session
from app.modules.auth.schemas import EmptyRequest
from app.modules.auth.service import AuthenticatedSession
from app.modules.privacy.service import PrivacyError, PrivacyService, PrivacyTask


router = APIRouter(
    prefix="/v1/me",
    tags=["privacy"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 403, 422, 429)
    },
)


class PrivacyTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    type: str
    status: str
    stage: str
    progress: int
    trace_id: str
    queued_at: datetime


def get_privacy_service(request: Request) -> PrivacyService:
    return request.app.state.privacy_service


def task_response(task: PrivacyTask) -> PrivacyTaskResponse:
    return PrivacyTaskResponse(
        id=task.id,
        type=task.type,
        status=task.status,
        stage=task.stage,
        progress=task.progress,
        trace_id=task.trace_id,
        queued_at=task.queued_at,
    )


def raise_privacy_error(request: Request, error: PrivacyError) -> None:
    api_error = createApiError(
        error.code,
        error.message,
        get_request_context(request).request_id,
        error.status_code,
    )
    if error.retry_after is not None:
        api_error.headers = {"Retry-After": str(error.retry_after)}
    raise api_error


@router.post("/data-exports", status_code=202, response_model=PrivacyTaskResponse)
async def request_data_export(
    request: Request,
    _payload: EmptyRequest | None = Body(default=None),
    idempotency_key: str = Header(
        min_length=1,
        max_length=255,
        alias="Idempotency-Key",
    ),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: PrivacyService = Depends(get_privacy_service),
) -> PrivacyTaskResponse:
    try:
        task = service.request_data_export(
            authenticated,
            idempotency_key,
            get_request_context(request).trace_id,
        )
    except PrivacyError as error:
        raise_privacy_error(request, error)
    return task_response(task)


@router.post("/deletion-requests", status_code=202, response_model=PrivacyTaskResponse)
async def request_deletion(
    request: Request,
    _payload: EmptyRequest | None = Body(default=None),
    idempotency_key: str = Header(
        min_length=1,
        max_length=255,
        alias="Idempotency-Key",
    ),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: PrivacyService = Depends(get_privacy_service),
) -> PrivacyTaskResponse:
    try:
        task = service.request_deletion(
            authenticated,
            idempotency_key,
            get_request_context(request).trace_id,
        )
    except PrivacyError as error:
        raise_privacy_error(request, error)
    return task_response(task)
