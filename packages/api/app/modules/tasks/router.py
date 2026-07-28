import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.db.models import Task, TaskEvent
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.tasks.service import TaskService, TaskServiceError
from app.modules.tasks.state import TERMINAL_STATUSES


router = APIRouter(
    prefix="/v1/tasks",
    tags=["tasks"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 404, 409, 422, 503)
    },
)


class DisconnectProbe(Protocol):
    async def is_disconnected(self) -> bool: ...


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


class TaskListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[TaskResponse]
    next_cursor: str | None


def get_task_service(request: Request) -> TaskService:
    return request.app.state.task_service


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


def _raise(request: Request, error: TaskServiceError) -> None:
    raise createApiError(
        error.code,
        error.message,
        get_request_context(request).request_id,
        error.status_code,
    )


def _raise_unavailable(request: Request) -> None:
    raise createApiError(
        "TASK_SERVICE_UNAVAILABLE",
        "Task service is temporarily unavailable",
        get_request_context(request).request_id,
        503,
    )


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    authenticated: AuthenticatedSession = Depends(require_session),
    service: TaskService = Depends(get_task_service),
) -> TaskListResponse:
    try:
        tasks, next_cursor = await service.list_tasks(
            authenticated.user_id,
            cursor,
            limit,
        )
    except TaskServiceError as error:
        _raise(request, error)
    except SQLAlchemyError:
        _raise_unavailable(request)
    return TaskListResponse(
        items=[_response(task) for task in tasks],
        next_cursor=next_cursor,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    try:
        task = await service.get_task(authenticated.user_id, task_id)
    except SQLAlchemyError:
        _raise_unavailable(request)
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
    except SQLAlchemyError:
        _raise_unavailable(request)


@router.get(
    "/{task_id}/events",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "SSE task progress stream",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def task_events(
    task_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: Annotated[
        int | None,
        Header(alias="Last-Event-ID"),
    ] = None,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: TaskService = Depends(get_task_service),
) -> StreamingResponse:
    try:
        task = await service.get_task(authenticated.user_id, task_id)
    except SQLAlchemyError:
        _raise_unavailable(request)
    if task is None:
        _raise(request, TaskServiceError("RESOURCE_NOT_FOUND", "Task not found", 404))
    cursor = max(after, last_event_id or 0)
    return StreamingResponse(
        task_event_stream(
            request,
            service,
            authenticated.user_id,
            task_id,
            after_seq=cursor,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def task_event_stream(
    request: DisconnectProbe,
    service: TaskService,
    owner_user_id: str,
    task_id: str,
    *,
    after_seq: int,
    poll_interval: float = 0.25,
    heartbeat_interval: float = 15,
) -> AsyncIterator[str]:
    cursor = after_seq
    last_heartbeat = time.monotonic()
    while not await request.is_disconnected():
        events = await service.list_events(owner_user_id, task_id, cursor)
        for event in events:
            cursor = event.seq
            yield _sse(event)
        task = await service.get_task(owner_user_id, task_id)
        if task is None:
            return
        if task.status in TERMINAL_STATUSES:
            terminal_events = await service.list_events(
                owner_user_id,
                task_id,
                cursor,
            )
            for event in terminal_events:
                cursor = event.seq
                yield _sse(event)
            return
        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval:
            yield f": heartbeat {int(now)}\n\n"
            last_heartbeat = now
        await asyncio.sleep(poll_interval)


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
