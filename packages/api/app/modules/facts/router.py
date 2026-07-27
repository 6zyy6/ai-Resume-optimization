from fastapi import APIRouter, Depends, Header, Request

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.facts.schemas import FactCreate, FactListResponse, FactResponse, FactSourcesResponse, FactUpdate, SourceInput
from app.contracts import FactStatus
from app.modules.facts.service import FactError, FactService


router = APIRouter(prefix="/v1/facts", tags=["facts"], responses={status: {"model": ApiErrorEnvelope} for status in (401, 404, 409, 422)})


def get_fact_service(request: Request) -> FactService:
    return request.app.state.fact_service


def _key(value: str | None, request: Request) -> str:
    if value:
        return value
    raise createApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required", get_request_context(request).request_id, 422)


def _raise(request: Request, error: FactError) -> None:
    raise createApiError(error.code, error.message, get_request_context(request).request_id, error.status_code)


async def _response(service: FactService, fact) -> FactResponse:
    async with service.sessions() as session:
        return FactResponse(id=fact.id, kind=fact.kind, value=fact.value_encrypted, status=FactStatus(fact.status), source_ids=await service.source_ids(session, fact), confirmed_at=fact.confirmed_at)


@router.get("", response_model=FactListResponse)
async def list_facts(authenticated: AuthenticatedSession = Depends(require_session), service: FactService = Depends(get_fact_service)) -> FactListResponse:
    facts = await service.list_facts(authenticated.user_id)
    return FactListResponse(items=[await _response(service, fact) for fact in facts])


@router.post("", status_code=201, response_model=FactResponse)
async def create_fact(payload: FactCreate, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), authenticated: AuthenticatedSession = Depends(require_session), service: FactService = Depends(get_fact_service)) -> FactResponse:
    try:
        fact = await service.create_fact(authenticated.user_id, kind=payload.kind, value=payload.value, status=payload.status.value, sources=[item.model_dump() for item in payload.sources], idempotency_key=_key(idempotency_key, request))
        return await _response(service, fact)
    except FactError as error:
        _raise(request, error)


@router.get("/{fact_id}", response_model=FactResponse)
async def get_fact(fact_id: str, request: Request, authenticated: AuthenticatedSession = Depends(require_session), service: FactService = Depends(get_fact_service)) -> FactResponse:
    fact = await service.get_fact(authenticated.user_id, fact_id)
    if fact is None:
        _raise(request, FactError("RESOURCE_NOT_FOUND", "Fact not found", 404))
    return await _response(service, fact)


@router.patch("/{fact_id}", response_model=FactResponse)
async def update_fact(fact_id: str, payload: FactUpdate, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), authenticated: AuthenticatedSession = Depends(require_session), service: FactService = Depends(get_fact_service)) -> FactResponse:
    try:
        return await _response(service, await service.update_fact(authenticated.user_id, fact_id, payload.model_dump(exclude_none=True), _key(idempotency_key, request)))
    except FactError as error:
        _raise(request, error)


@router.post("/{fact_id}/confirm", response_model=FactResponse)
@router.post("/{fact_id}/reject", response_model=FactResponse)
async def set_fact_status(fact_id: str, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), authenticated: AuthenticatedSession = Depends(require_session), service: FactService = Depends(get_fact_service)) -> FactResponse:
    status = "confirmed" if request.url.path.endswith("/confirm") else "rejected"
    try:
        return await _response(service, await service.set_status(authenticated.user_id, fact_id, status, _key(idempotency_key, request)))
    except FactError as error:
        _raise(request, error)


@router.get("/{fact_id}/sources", response_model=FactSourcesResponse)
async def get_sources(fact_id: str, request: Request, authenticated: AuthenticatedSession = Depends(require_session), service: FactService = Depends(get_fact_service)) -> FactSourcesResponse:
    sources = await service.sources(authenticated.user_id, fact_id)
    if sources is None:
        _raise(request, FactError("RESOURCE_NOT_FOUND", "Fact not found", 404))
    return FactSourcesResponse(items=[SourceInput(source_type=item.source_type, source_ref=item.source_ref, content=item.content_encrypted) for item in sources])
