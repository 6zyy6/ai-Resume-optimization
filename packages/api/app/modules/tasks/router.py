import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.db.models import Task, TaskEvent
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.tasks.service import TaskService, TaskServiceError
from app.workers.dispatcher import OutboxDispatcher, TaskQueueBusy
from app.workers.execution import QUEUE_NAMES


router = APIRouter(
    prefix="/v1/tasks",
    tags=["tasks"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 404, 409, 422, 429, 503)
    },
)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: str = Field(min_length=1, max_length=64)
    queue: str
    resource_type: str | None = Field(default=None, max_length=64)
    resource_id: str | None = Field(default=None, max_length=64)
    priority: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    type: str
    status: str
    progress: int = Field(ge=0, le=100)
    stage: str
    trace_id: str
    result_ref: str | None
    error_code: str | None
    cancellation_requested: bool


def get_task_service(request: Request) -> TaskService:
    return request.app.state.task_service


def get_task_dispatcher(request: Request) -> OutboxDispatcher:
    return request.app.state.task_dispatcher


def _response(task: Task) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        type=task.type,
        status=task.status,
        progress=task.progress,
        stage=task.stage,
        trace_id=task.trace_id,
        result_ref=task.result_ref,
        error_code=task.error_code,
        cancellation_requested=task.cancellation_requested,
    )


def _raise(request: Request, error: TaskServiceError | TaskQueueBusy) -> None:
    raise createApiError(
        error.code,
        error.message,
        get_request_context(request).request_id,
        error.status_code,
    )


@router.post("", status_code=202, response_model=TaskResponse)
async def create_task(
    payload: TaskCreate,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(min_length=1, max_length=255, alias="Idempotency-Key"),
    ],
    authenticated: AuthenticatedSession = Depends(require_session),
    service: TaskService = Depends(get_task_service),
    dispatcher: OutboxDispatcher = Depends(get_task_dispatcher),
) -> TaskResponse:
    if payload.queue not in QUEUE_NAMES:
        _raise(
            request,
            TaskServiceError("TASK_QUEUE_INVALID", "Unsupported task queue", 422),
        )
    usage_decision = await request.app.state.usage_service.decide_ai_task(
        authenticated.user_id
    )
    try:
        task = await service.create_task(
            authenticated.user_id,
            task_type=payload.type,
            queue=payload.queue,
            trace_id=get_request_context(request).trace_id,
            idempotency_key=idempotency_key,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            priority=payload.priority,
            payload=payload.payload,
            usage_decision=usage_decision,
        )
        await dispatcher.dispatch_task(task.id)
    except (TaskServiceError, TaskQueueBusy) as error:
        _raise(request, error)
    return _response(task)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    task = await service.get_task(authenticated.user_id, task_id)
    if task is None:
        _raise(request, TaskServiceError("RESOURCE_NOT_FOUND", "Task not found", 404))
    return _response(task)


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    try:
        return _response(await service.request_cancel(authenticated.user_id, task_id))
    except TaskServiceError as error:
        _raise(request, error)


@router.get("/{task_id}/events")
async def task_events(
    task_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: TaskService = Depends(get_task_service),
) -> StreamingResponse:
    try:
        events = await service.list_events(authenticated.user_id, task_id, after)
    except TaskServiceError as error:
        _raise(request, error)
    return StreamingResponse(
        (_sse(event) for event in events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _sse(event: TaskEvent) -> str:
    data = json.dumps(
        {
            "task_id": event.task_id,
            "seq": event.seq,
            "stage": event.stage,
            "progress": event.progress,
            "created_at": event.created_at.isoformat(),
        },
        separators=(",", ":"),
    )
    return f"id: {event.seq}\nevent: progress\ndata: {data}\n\n"
