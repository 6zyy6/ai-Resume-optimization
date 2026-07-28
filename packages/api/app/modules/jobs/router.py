from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.jobs.service import JobService, JobServiceError
from app.modules.tasks.service import TaskAdmission, TaskService, TaskServiceError


router = APIRouter(
    prefix="/v1/jobs",
    tags=["jobs"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 404, 409, 422, 502)
    },
)


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1, max_length=255)
    company: str | None = Field(default=None, max_length=255)
    raw: str = Field(min_length=2, max_length=100000)


class RequirementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str | None = Field(default=None, min_length=1, max_length=5000)
    type: Literal["must_have", "preferred", "responsibility", "other"] | None = None
    priority: int | None = Field(default=None, ge=1, le=1000)
    confirmed: bool | None = None


class RequirementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    type: str
    priority: int
    text: str
    confirmed: bool


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    title: str
    company: str | None
    status: str
    requirements: list[RequirementResponse] = Field(default_factory=list)
    task_id: str | None = None


def get_job_service(request: Request) -> JobService:
    return request.app.state.job_service


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


def _raise(request: Request, error: JobServiceError) -> None:
    raise createApiError(
        error.code,
        error.message,
        get_request_context(request).request_id,
        error.status_code,
    )


def _requirement(row) -> RequirementResponse:
    return RequirementResponse(
        id=row.id,
        type=row.type,
        priority=row.priority,
        text=row.text_encrypted,
        confirmed=row.confirmed,
    )


def _job(row, requirements=(), task_id: str | None = None) -> JobResponse:
    return JobResponse(
        id=row.id,
        title=row.title,
        company=row.company,
        status=row.status,
        requirements=[_requirement(item) for item in requirements],
        task_id=task_id,
    )


@router.post("", status_code=201, response_model=JobResponse)
async def create_job(
    payload: JobCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: JobService = Depends(get_job_service),
) -> JobResponse:
    try:
        return _job(
            await service.create(
                authenticated.user_id,
                payload.model_dump(),
                _key(idempotency_key, request),
            )
        )
    except JobServiceError as error:
        _raise(request, error)


@router.post("/{job_id}/parse", status_code=202, response_model=JobResponse)
async def parse_job(
    job_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: JobService = Depends(get_job_service),
    task_service: TaskService = Depends(get_task_service),
) -> JobResponse:
    try:
        key = _key(idempotency_key, request)
        row, requirements = await service.parse(
            authenticated.user_id,
            job_id,
            key,
            trace_id=get_request_context(request).trace_id,
        )
        task = await task_service.create_task(
            authenticated.user_id,
            task_type="parse_job",
            queue="ai.interactive",
            trace_id=get_request_context(request).trace_id,
            idempotency_key=f"job-parse:{key}",
            admission=TaskAdmission.ai(),
            resource_type="job_description",
            resource_id=row.id,
            payload={"job_id": row.id},
        )
    except (JobServiceError, TaskServiceError) as error:
        _raise(request, error)
    return _job(row, requirements, task.id)


@router.patch(
    "/{job_id}/requirements/{requirement_id}",
    response_model=RequirementResponse,
)
async def update_requirement(
    job_id: str,
    requirement_id: str,
    payload: RequirementUpdate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: JobService = Depends(get_job_service),
) -> RequirementResponse:
    try:
        return _requirement(
            await service.update_requirement(
                authenticated.user_id,
                job_id,
                requirement_id,
                payload.model_dump(exclude_none=True),
                _key(idempotency_key, request),
            )
        )
    except JobServiceError as error:
        _raise(request, error)
