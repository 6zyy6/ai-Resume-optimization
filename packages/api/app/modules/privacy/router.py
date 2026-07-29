from datetime import datetime

from fastapi import APIRouter, Body, Depends, Header, Request
from pydantic import BaseModel, ConfigDict

from app.contracts import ApiErrorEnvelope
from app.core.constants import SESSION_COOKIE_NAME
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import raise_auth_error, require_session
from app.modules.auth.schemas import EmptyRequest
from app.modules.auth.service import AuthError, AuthenticatedSession
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


class PrivacyExportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file_id: str
    download_url: str
    download_expires_in: int


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
        task = await service.request_data_export(
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
    service: PrivacyService = Depends(get_privacy_service),
) -> PrivacyTaskResponse:
    auth_service = request.app.state.auth_service
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    authenticated = await auth_service.authenticate(raw_token)
    if authenticated is None:
        replay_identity = await auth_service.identify_deletion_replay(raw_token)
        if replay_identity is not None:
            replay = await service.replay_deletion(
                replay_identity.user_id,
                idempotency_key,
            )
            if replay is not None:
                return task_response(replay)
        raise_auth_error(
            request,
            AuthError("AUTH_REQUIRED", "Authentication required", 401),
        )
    try:
        task = await service.request_deletion(
            authenticated,
            idempotency_key,
            get_request_context(request).trace_id,
        )
    except PrivacyError as error:
        raise_privacy_error(request, error)
    return task_response(task)


@router.get(
    "/data-exports/{task_id}",
    response_model=PrivacyExportResponse,
)
async def get_data_export(
    task_id: str,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: PrivacyService = Depends(get_privacy_service),
) -> PrivacyExportResponse:
    try:
        artifact = await service.get_data_export(
            authenticated.user_id,
            task_id,
        )
    except PrivacyError as error:
        raise_privacy_error(request, error)
    expires_in = 900
    return PrivacyExportResponse(
        file_id=artifact.file_id,
        download_url=request.app.state.storage.download_url(
            artifact.object_key,
            artifact.display_name,
            expires_in,
        ),
        download_expires_in=expires_in,
    )
