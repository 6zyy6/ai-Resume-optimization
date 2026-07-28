from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import Outbox, Task, TaskEvent
from app.db.ownership import authorized_owner_ids
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
from app.modules.tasks.state import TERMINAL_STATUSES, TaskStateError, require_transition
from app.workers.execution import QUEUE_NAMES


class TaskServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class TaskService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.idempotency = IdempotencyService()

    async def create_task(
        self,
        owner_user_id: str,
        *,
        task_type: str,
        queue: str,
        trace_id: str,
        idempotency_key: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        priority: int = 0,
        payload: dict[str, Any] | None = None,
        usage_decision: Any | None = None,
    ) -> Task:
        if queue not in QUEUE_NAMES:
            raise TaskServiceError("TASK_QUEUE_INVALID", "Unsupported task queue", 422)
        body = {
            "type": task_type,
            "queue": queue,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "priority": priority,
            "payload": payload or {},
        }
        async with self.idempotency.transaction(self.sessions) as session:
            try:
                claim = await self.idempotency.claim(
                    session,
                    owner_user_id,
                    "/v1/tasks",
                    idempotency_key,
                    body,
                )
            except IdempotencyConflict as error:
                raise TaskServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                task_id = (claim.replay_response or {})["id"]
                task = await session.get(Task, task_id)
                if task is None:
                    raise RuntimeError("idempotent task response references a missing task")
                return task

            if usage_decision is not None and not usage_decision.allowed:
                raise TaskServiceError(
                    usage_decision.reason or "AI_LIMIT_REACHED",
                    "AI task admission was denied",
                    429,
                )
            now = datetime.now(timezone.utc)
            task = Task(
                id=usage_decision.task_id
                if usage_decision is not None and usage_decision.task_id
                else new_id("tsk"),
                owner_user_id=claim.row.owner_user_id,
                type=task_type,
                status="queued",
                priority=priority,
                resource_type=resource_type,
                resource_id=resource_id,
                trace_id=trace_id,
                attempts=0,
                max_attempts=3,
                queued_at=now,
                stage="queued",
                progress=0,
                cancellation_requested=False,
            )
            session.add(task)
            await session.flush()
            await self._append_event(session, task, "queued", 0, now)
            session.add(
                Outbox(
                    id=new_id("out"),
                    owner_user_id=task.owner_user_id,
                    task_id=task.id,
                    queue=queue,
                    payload={**(payload or {}), "task_id": task.id},
                    attempts=0,
                    available_at=now,
                    created_at=now,
                )
            )
            await session.flush()
            await self.idempotency.complete(
                session,
                claim,
                202,
                self._task_payload(task),
            )
            return task

    async def get_task(self, owner_user_id: str, task_id: str) -> Task | None:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_user_id)
            return await session.scalar(
                select(Task).where(
                    Task.id == task_id,
                    Task.owner_user_id.in_(owners),
                )
            )

    async def claim_task(self, task_id: str) -> Task | None:
        async with self.sessions.begin() as session:
            task = await self._locked_task(session, task_id)
            if task is None or task.status in TERMINAL_STATUSES:
                return None
            if task.cancellation_requested:
                await self._finish(session, task, "cancelled")
                return None
            if task.attempts >= task.max_attempts:
                await self._finish(session, task, "failed", error_code="TASK_RETRIES_EXHAUSTED")
                return None
            if task.status != "running":
                require_transition(task.status, "running")
            now = datetime.now(timezone.utc)
            task.status = "running"
            task.stage = "running"
            task.started_at = task.started_at or now
            task.attempts += 1
            await self._append_event(session, task, "running", task.progress, now)
            await session.flush()
            return task

    async def report_progress(self, task_id: str, stage: str, progress: int) -> Task:
        if progress < 0 or progress > 99:
            raise TaskStateError("TASK_PROGRESS_INVALID", "Progress must be between 0 and 99")
        async with self.sessions.begin() as session:
            task = await self._required_locked_task(session, task_id)
            if task.status in TERMINAL_STATUSES:
                raise TaskStateError("TASK_TERMINAL", "Terminal task state cannot change")
            if task.status not in {"running", "waiting_for_user"}:
                raise TaskStateError(
                    "TASK_STATE_INVALID",
                    "Progress can only be reported by an active task",
                )
            if progress < task.progress:
                raise TaskStateError(
                    "TASK_PROGRESS_INVALID",
                    "Task progress cannot decrease",
                )
            task.stage = stage
            task.progress = progress
            await self._append_event(session, task, stage, progress)
            await session.flush()
            return task

    async def request_cancel(self, owner_user_id: str, task_id: str) -> Task:
        async with self.sessions.begin() as session:
            owners = await authorized_owner_ids(session, owner_user_id)
            task = await session.scalar(
                select(Task)
                .where(Task.id == task_id, Task.owner_user_id.in_(owners))
                .with_for_update()
            )
            if task is None:
                raise TaskServiceError("RESOURCE_NOT_FOUND", "Task not found", 404)
            if task.status in TERMINAL_STATUSES:
                return task
            task.cancellation_requested = True
            await self._finish(session, task, "cancelled")
            return task

    async def complete_task(self, task_id: str, result_ref: str) -> Task:
        async with self.sessions.begin() as session:
            task = await self._required_locked_task(session, task_id)
            if task.status == "cancelled" or task.cancellation_requested:
                if task.status != "cancelled":
                    await self._finish(session, task, "cancelled")
                return task
            if task.status == "succeeded":
                return task
            if task.status in TERMINAL_STATUSES:
                raise TaskStateError("TASK_TERMINAL", "Terminal task state cannot change")
            require_transition(task.status, "succeeded")
            await self._finish(
                session,
                task,
                "succeeded",
                result_ref=result_ref,
                progress=100,
            )
            return task

    async def fail_task(self, task_id: str, error_code: str) -> Task:
        async with self.sessions.begin() as session:
            task = await self._required_locked_task(session, task_id)
            if task.status == "failed":
                return task
            if task.status in TERMINAL_STATUSES:
                raise TaskStateError("TASK_TERMINAL", "Terminal task state cannot change")
            require_transition(task.status, "failed")
            await self._finish(session, task, "failed", error_code=error_code)
            return task

    async def list_events(
        self,
        owner_user_id: str,
        task_id: str,
        after_seq: int = 0,
    ) -> tuple[TaskEvent, ...]:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_user_id)
            exists = await session.scalar(
                select(Task.id).where(
                    Task.id == task_id,
                    Task.owner_user_id.in_(owners),
                )
            )
            if exists is None:
                raise TaskServiceError("RESOURCE_NOT_FOUND", "Task not found", 404)
            rows = await session.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == task_id, TaskEvent.seq > after_seq)
                .order_by(TaskEvent.seq)
            )
            return tuple(rows.all())

    async def _finish(
        self,
        session: AsyncSession,
        task: Task,
        status: str,
        *,
        result_ref: str | None = None,
        error_code: str | None = None,
        progress: int | None = None,
    ) -> None:
        require_transition(task.status, status)
        now = datetime.now(timezone.utc)
        task.status = status
        task.stage = status
        task.finished_at = now
        task.result_ref = result_ref
        task.error_code = error_code
        if progress is not None:
            task.progress = progress
        await session.flush()
        await self._append_event(session, task, status, task.progress, now)
        await session.flush()

    async def _append_event(
        self,
        session: AsyncSession,
        task: Task,
        stage: str,
        progress: int,
        created_at: datetime | None = None,
    ) -> None:
        sequence = (
            await session.scalar(
                select(func.coalesce(func.max(TaskEvent.seq), 0)).where(
                    TaskEvent.task_id == task.id
                )
            )
            or 0
        ) + 1
        session.add(
            TaskEvent(
                id=new_id("tev"),
                owner_user_id=task.owner_user_id,
                task_id=task.id,
                seq=sequence,
                stage=stage,
                progress=progress,
                created_at=created_at or datetime.now(timezone.utc),
            )
        )

    @staticmethod
    async def _locked_task(session: AsyncSession, task_id: str) -> Task | None:
        return await session.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )

    async def _required_locked_task(
        self,
        session: AsyncSession,
        task_id: str,
    ) -> Task:
        task = await self._locked_task(session, task_id)
        if task is None:
            raise TaskServiceError("RESOURCE_NOT_FOUND", "Task not found", 404)
        return task

    @staticmethod
    def _task_payload(task: Task) -> dict[str, Any]:
        return {
            "id": task.id,
            "type": task.type,
            "status": task.status,
            "progress": task.progress,
            "stage": task.stage,
            "trace_id": task.trace_id,
            "result_ref": task.result_ref,
            "error_code": task.error_code,
        }
