from fastapi import APIRouter, Depends, Header, Request, Response

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.intake.schemas import (
    FactCandidateDecisionRequest,
    FactCandidateDecisionResponse,
    IntakeAnalysisActionRequest,
    IntakeAnswerRequest,
    IntakeDraftRequest,
    IntakeDraftResponse,
    IntakeSessionResponse,
    IntakeStartRequest,
)
from app.modules.intake.service import IntakeError, IntakeService
from app.modules.tasks.service import TaskService


router = APIRouter(
    prefix="/v1/intake-sessions",
    tags=["intake"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 404, 409, 422, 429, 503)
    },
)


def get_intake_service(request: Request) -> IntakeService:
    return request.app.state.intake_service


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


def _raise(request: Request, error: IntakeError) -> None:
    raise createApiError(
        error.code,
        error.message,
        get_request_context(request).request_id,
        error.status_code,
    )


@router.post(
    "",
    status_code=201,
    response_model=IntakeSessionResponse,
    responses={200: {"model": IntakeSessionResponse}},
)
async def start_intake(
    payload: IntakeStartRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: IntakeService = Depends(get_intake_service),
) -> IntakeSessionResponse:
    try:
        saved = await service.start(
            authenticated.user_id,
            restart=payload.restart,
            idempotency_key=_key(idempotency_key, request),
        )
    except IntakeError as error:
        _raise(request, error)
    response.status_code = saved.status_code
    return IntakeSessionResponse.model_validate(saved.response, strict=False)


@router.get("/{session_id}", response_model=IntakeSessionResponse)
async def get_intake(
    session_id: str,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: IntakeService = Depends(get_intake_service),
) -> IntakeSessionResponse:
    result = await service.get(authenticated.user_id, session_id)
    if result is None:
        _raise(request, IntakeError("RESOURCE_NOT_FOUND", "Intake session not found", 404))
    return IntakeSessionResponse.model_validate(result, strict=False)


@router.post(
    "/{session_id}/answers",
    response_model=IntakeSessionResponse,
    status_code=202,
    responses={200: {"model": IntakeSessionResponse}},
)
async def answer_intake(
    session_id: str,
    payload: IntakeAnswerRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: IntakeService = Depends(get_intake_service),
    task_service: TaskService = Depends(get_task_service),
) -> IntakeSessionResponse:
    try:
        saved = await service.answer(
            authenticated.user_id,
            session_id,
            payload.model_dump(),
            _key(idempotency_key, request),
            trace_id=get_request_context(request).trace_id,
            task_service=task_service,
        )
    except IntakeError as error:
        _raise(request, error)
    response.status_code = saved.status_code
    return IntakeSessionResponse.model_validate(saved.response, strict=False)


@router.post(
    "/{session_id}/analysis/retry",
    response_model=IntakeSessionResponse,
    status_code=202,
)
async def retry_answer_analysis(
    session_id: str,
    payload: IntakeAnalysisActionRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: IntakeService = Depends(get_intake_service),
    task_service: TaskService = Depends(get_task_service),
) -> IntakeSessionResponse:
    try:
        saved = await service.retry_analysis(
            authenticated.user_id,
            session_id,
            payload.model_dump(),
            _key(idempotency_key, request),
            trace_id=get_request_context(request).trace_id,
            task_service=task_service,
        )
    except IntakeError as error:
        _raise(request, error)
    response.status_code = saved.status_code
    return IntakeSessionResponse.model_validate(saved.response, strict=False)


@router.post(
    "/{session_id}/analysis/continue",
    response_model=IntakeSessionResponse,
)
async def continue_answer_analysis(
    session_id: str,
    payload: IntakeAnalysisActionRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: IntakeService = Depends(get_intake_service),
    task_service: TaskService = Depends(get_task_service),
) -> IntakeSessionResponse:
    try:
        saved = await service.continue_analysis(
            authenticated.user_id,
            session_id,
            payload.model_dump(),
            _key(idempotency_key, request),
            task_service=task_service,
        )
    except IntakeError as error:
        _raise(request, error)
    return IntakeSessionResponse.model_validate(saved.response, strict=False)


@router.post(
    "/{session_id}/fact-candidates/{candidate_id}/decision",
    response_model=FactCandidateDecisionResponse,
)
async def decide_fact_candidate(
    session_id: str,
    candidate_id: str,
    payload: FactCandidateDecisionRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: IntakeService = Depends(get_intake_service),
) -> FactCandidateDecisionResponse:
    try:
        result = await service.decide_candidate(
            authenticated.user_id,
            session_id,
            candidate_id,
            payload.model_dump(),
            _key(idempotency_key, request),
        )
    except IntakeError as error:
        _raise(request, error)
    return FactCandidateDecisionResponse.model_validate(result, strict=False)


@router.post(
    "/{session_id}/drafts",
    status_code=202,
    response_model=IntakeDraftResponse,
)
async def create_draft(
    session_id: str,
    payload: IntakeDraftRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: IntakeService = Depends(get_intake_service),
    task_service: TaskService = Depends(get_task_service),
) -> IntakeDraftResponse:
    try:
        result = await service.queue_draft(
            authenticated.user_id,
            session_id,
            payload.model_dump(),
            _key(idempotency_key, request),
            trace_id=get_request_context(request).trace_id,
            task_service=task_service,
        )
    except IntakeError as error:
        _raise(request, error)
    return IntakeDraftResponse.model_validate(result, strict=False)
