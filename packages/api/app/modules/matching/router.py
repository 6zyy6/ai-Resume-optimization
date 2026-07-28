from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.matching.service import (
    MatchAnalysisResult,
    MatchingService,
    MatchServiceError,
)
from app.modules.tasks.service import TaskService, TaskServiceError


router = APIRouter(
    prefix="/v1/match-analyses",
    tags=["matching"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 404, 409, 422)
    },
)


class MatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    resume_version_id: str
    job_id: str


class MatchItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    requirement_id: str
    category: str
    evidence_refs: list[str]


class MatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    resume_version_id: str
    job_id: str
    status: str
    workflow_version: str
    task_id: str | None
    items: list[MatchItemResponse]


class SuggestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    status: str
    target_path: str
    requirement_id: str | None
    requirement_text: str | None
    original_text: str
    suggested_text: str
    reason: str
    fact_refs: list[str]
    risk_flags: list[str]


class SuggestionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[SuggestionResponse]


def get_matching_service(request: Request) -> MatchingService:
    return request.app.state.matching_service


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


def _raise(request: Request, error: MatchServiceError) -> None:
    raise createApiError(
        error.code,
        error.message,
        get_request_context(request).request_id,
        error.status_code,
    )


def _analysis(result: MatchAnalysisResult) -> MatchResponse:
    return MatchResponse(
        id=result.analysis.id,
        resume_version_id=result.analysis.resume_version_id,
        job_id=result.analysis.job_id,
        status=result.analysis.status,
        workflow_version=result.analysis.workflow_version,
        task_id=result.analysis.task_id,
        items=[
            MatchItemResponse(
                id=item.id,
                requirement_id=item.requirement_id,
                category=item.category,
                evidence_refs=list(item.evidence_refs),
            )
            for item in result.items
        ],
    )


def _suggestion(
    row,
    fact_refs: list[str],
    requirement_text: str | None,
) -> SuggestionResponse:
    return SuggestionResponse(
        id=row.id,
        status=row.status,
        target_path=row.target_path,
        requirement_id=row.requirement_id,
        requirement_text=requirement_text,
        original_text=row.original_text_encrypted,
        suggested_text=row.suggested_encrypted,
        reason=row.reason,
        fact_refs=fact_refs,
        risk_flags=list(row.risk_flags),
    )


@router.post("", status_code=202, response_model=MatchResponse)
async def create_match(
    payload: MatchCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: MatchingService = Depends(get_matching_service),
    task_service: TaskService = Depends(get_task_service),
) -> MatchResponse:
    try:
        key = _key(idempotency_key, request)
        result = await service.create(
            authenticated.user_id,
            resume_version_id=payload.resume_version_id,
            job_id=payload.job_id,
            idempotency_key=key,
            trace_id=get_request_context(request).trace_id,
            task_service=task_service,
        )
        return _analysis(result)
    except (MatchServiceError, TaskServiceError) as error:
        _raise(request, error)


@router.get("/{analysis_id}", response_model=MatchResponse)
async def get_match(
    analysis_id: str,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: MatchingService = Depends(get_matching_service),
) -> MatchResponse:
    result = await service.get(authenticated.user_id, analysis_id)
    if result is None:
        _raise(
            request,
            MatchServiceError("RESOURCE_NOT_FOUND", "Match analysis not found", 404),
        )
    return _analysis(result)


@router.get("/{analysis_id}/suggestions", response_model=SuggestionListResponse)
async def get_match_suggestions(
    analysis_id: str,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: MatchingService = Depends(get_matching_service),
) -> SuggestionListResponse:
    result = await service.get(authenticated.user_id, analysis_id)
    if result is None:
        _raise(
            request,
            MatchServiceError("RESOURCE_NOT_FOUND", "Match analysis not found", 404),
        )
    if result.analysis.status != "succeeded":
        _raise(
            request,
            MatchServiceError(
                "MATCH_ANALYSIS_NOT_READY",
                "Match analysis is not ready for suggestions",
                409,
            ),
        )
    return SuggestionListResponse(
        items=[
            _suggestion(
                row,
                result.suggestion_fact_refs.get(row.id, []),
                result.requirement_texts.get(row.requirement_id or ""),
            )
            for row in result.suggestions
        ]
    )
