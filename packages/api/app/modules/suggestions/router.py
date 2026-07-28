from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.suggestions.service import (
    SavedSuggestionDecision,
    SuggestionService,
    SuggestionServiceError,
)


router = APIRouter(
    prefix="/v1/suggestions",
    tags=["suggestions"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 404, 409, 422)
    },
)


class EmptyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EditDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=10000)


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    suggestion_id: str
    status: str
    version_id: str
    decision_id: str


def get_suggestion_service(request: Request) -> SuggestionService:
    return request.app.state.suggestion_service


def _key(value: str | None, request: Request) -> str:
    if value:
        return value
    raise createApiError(
        "IDEMPOTENCY_KEY_REQUIRED",
        "Idempotency-Key is required",
        get_request_context(request).request_id,
        422,
    )


def _raise(request: Request, error: SuggestionServiceError) -> None:
    raise createApiError(
        error.code,
        error.message,
        get_request_context(request).request_id,
        error.status_code,
    )


def _response(saved: SavedSuggestionDecision) -> DecisionResponse:
    return DecisionResponse(
        suggestion_id=saved.suggestion.id,
        status=saved.suggestion.status,
        version_id=saved.version.id,
        decision_id=saved.decision.id,
    )


async def _decide(
    suggestion_id: str,
    decision: str,
    edited_text: str | None,
    request: Request,
    idempotency_key: str | None,
    authenticated: AuthenticatedSession,
    service: SuggestionService,
) -> DecisionResponse:
    try:
        return _response(
            await service.decide(
                authenticated.user_id,
                suggestion_id,
                decision,
                edited_text=edited_text,
                idempotency_key=_key(idempotency_key, request),
            )
        )
    except SuggestionServiceError as error:
        _raise(request, error)


@router.post("/{suggestion_id}/accept", status_code=201, response_model=DecisionResponse)
async def accept(
    suggestion_id: str,
    request: Request,
    _payload: EmptyDecision | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: SuggestionService = Depends(get_suggestion_service),
) -> DecisionResponse:
    return await _decide(
        suggestion_id, "accept", None, request, idempotency_key, authenticated, service
    )


@router.post("/{suggestion_id}/edit", status_code=201, response_model=DecisionResponse)
async def edit(
    suggestion_id: str,
    payload: EditDecision,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: SuggestionService = Depends(get_suggestion_service),
) -> DecisionResponse:
    return await _decide(
        suggestion_id,
        "edit",
        payload.text,
        request,
        idempotency_key,
        authenticated,
        service,
    )


@router.post("/{suggestion_id}/ignore", status_code=201, response_model=DecisionResponse)
async def ignore(
    suggestion_id: str,
    request: Request,
    _payload: EmptyDecision | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: SuggestionService = Depends(get_suggestion_service),
) -> DecisionResponse:
    return await _decide(
        suggestion_id, "ignore", None, request, idempotency_key, authenticated, service
    )


@router.post("/{suggestion_id}/revert", status_code=201, response_model=DecisionResponse)
async def revert(
    suggestion_id: str,
    request: Request,
    _payload: EmptyDecision | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: SuggestionService = Depends(get_suggestion_service),
) -> DecisionResponse:
    return await _decide(
        suggestion_id, "revert", None, request, idempotency_key, authenticated, service
    )
