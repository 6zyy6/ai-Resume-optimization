from fastapi import APIRouter, Depends, Header, Query, Request, Response

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.resumes.schemas import QualityCheckResponse, QualityIssueResponse, RestoreRequest, ResumeCreate, ResumeListResponse, ResumeResponse, ResumeUpdate, ResumeVersionResponse, ResumeVersionsResponse, VersionCreate
from app.modules.resumes.service import ResumeError, ResumeService, SavedResume, SavedVersion


router = APIRouter(prefix="/v1/resumes", tags=["resumes"], responses={status: {"model": ApiErrorEnvelope} for status in (401, 404, 409, 422)})


def get_resume_service(request: Request) -> ResumeService:
    return request.app.state.resume_service


def _key(value: str | None, request: Request) -> str:
    if value:
        return value
    raise createApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required", get_request_context(request).request_id, 422)


def _raise(request: Request, error: ResumeError) -> None:
    raise createApiError(error.code, error.message, get_request_context(request).request_id, error.status_code)


def _resume(row) -> ResumeResponse:
    if isinstance(row, SavedResume):
        return ResumeResponse.model_validate(row.response, strict=False)
    return ResumeResponse(id=row.id, kind=row.kind, title=row.title, base_resume_id=row.base_resume_id, job_description_id=row.job_description_id, version=row.head_version)


def _version(saved: SavedVersion) -> ResumeVersionResponse:
    if saved.response:
        return ResumeVersionResponse.model_validate(saved.response, strict=False)
    row = saved.row
    return ResumeVersionResponse(id=row.id, resume_id=row.resume_id, parent_version_id=row.parent_version_id, snapshot=row.snapshot_json, snapshot_hash=row.snapshot_hash, operation=saved.operation, created_at=row.created_at)


@router.get("", response_model=ResumeListResponse)
async def list_resumes(request: Request, cursor: str | None = Query(default=None), limit: int = Query(default=20, ge=1, le=100), authenticated: AuthenticatedSession = Depends(require_session), service: ResumeService = Depends(get_resume_service)) -> ResumeListResponse:
    try:
        rows, next_cursor = await service.list_resumes(authenticated.user_id, cursor, limit)
    except ResumeError as error:
        _raise(request, error)
    return ResumeListResponse(items=[_resume(row) for row in rows], next_cursor=next_cursor)


@router.post("", status_code=201, response_model=ResumeResponse)
async def create_resume(payload: ResumeCreate, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), authenticated: AuthenticatedSession = Depends(require_session), service: ResumeService = Depends(get_resume_service)) -> ResumeResponse:
    try:
        return _resume(await service.create_resume(authenticated.user_id, payload.model_dump(), _key(idempotency_key, request)))
    except ResumeError as error:
        _raise(request, error)


@router.get("/{resume_id}", response_model=ResumeResponse)
async def get_resume(resume_id: str, request: Request, authenticated: AuthenticatedSession = Depends(require_session), service: ResumeService = Depends(get_resume_service)) -> ResumeResponse:
    resume = await service.get_resume(authenticated.user_id, resume_id)
    if resume is None:
        _raise(request, ResumeError("RESOURCE_NOT_FOUND", "Resume not found", 404))
    return _resume(resume)


@router.patch("/{resume_id}", response_model=ResumeResponse)
async def update_resume(resume_id: str, payload: ResumeUpdate, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), authenticated: AuthenticatedSession = Depends(require_session), service: ResumeService = Depends(get_resume_service)) -> ResumeResponse:
    try:
        return _resume(await service.update_resume(authenticated.user_id, resume_id, payload.title, _key(idempotency_key, request)))
    except ResumeError as error:
        _raise(request, error)


@router.get("/{resume_id}/versions", response_model=ResumeVersionsResponse)
async def list_versions(resume_id: str, request: Request, cursor: str | None = Query(default=None), limit: int = Query(default=20, ge=1, le=100), authenticated: AuthenticatedSession = Depends(require_session), service: ResumeService = Depends(get_resume_service)) -> ResumeVersionsResponse:
    try:
        page = await service.versions(authenticated.user_id, resume_id, cursor, limit)
    except ResumeError as error:
        _raise(request, error)
    if page is None:
        _raise(request, ResumeError("RESOURCE_NOT_FOUND", "Resume not found", 404))
    rows, next_cursor = page
    return ResumeVersionsResponse(items=[_version(SavedVersion(row, 200, "save")) for row in rows], next_cursor=next_cursor)


@router.post("/{resume_id}/versions", status_code=201, response_model=ResumeVersionResponse)
async def save_version(resume_id: str, payload: VersionCreate, request: Request, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), authenticated: AuthenticatedSession = Depends(require_session), service: ResumeService = Depends(get_resume_service)) -> ResumeVersionResponse:
    try:
        saved = await service.save_resume_version(authenticated.user_id, resume_id, payload.base_version, payload.snapshot.model_dump(mode="json"), _key(idempotency_key, request))
        response.status_code = saved.status_code
        return _version(saved)
    except ResumeError as error:
        _raise(request, error)


@router.post("/{resume_id}/versions/{version_id}/restore", status_code=201, response_model=ResumeVersionResponse)
async def restore_version(resume_id: str, version_id: str, payload: RestoreRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), authenticated: AuthenticatedSession = Depends(require_session), service: ResumeService = Depends(get_resume_service)) -> ResumeVersionResponse:
    try:
        return _version(await service.restore(authenticated.user_id, resume_id, version_id, payload.base_version, _key(idempotency_key, request)))
    except ResumeError as error:
        _raise(request, error)


@router.post("/{resume_id}/quality-checks", response_model=QualityCheckResponse)
async def quality_check(resume_id: str, request: Request, authenticated: AuthenticatedSession = Depends(require_session), service: ResumeService = Depends(get_resume_service)) -> QualityCheckResponse:
    issues = await service.quality(authenticated.user_id, resume_id)
    if issues is None:
        _raise(request, ResumeError("RESOURCE_NOT_FOUND", "Resume not found", 404))
    return QualityCheckResponse(issues=[QualityIssueResponse(code=item.code, path=item.path, message=item.message) for item in issues])
