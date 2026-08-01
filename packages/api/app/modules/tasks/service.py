from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import Outbox, Task, TaskEvent, UsageLedger
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.db.ports import is_valid_cost_cny
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
from app.modules.tasks.state import TERMINAL_STATUSES, TaskStateError, require_transition
from app.modules.usage.service import (
    GLOBAL_AI_COST_ADVISORY_LOCK_ID,
    evaluate_admission_usage,
)
from app.workers.execution import QUEUE_NAMES


DEFAULT_LEASE_SECONDS = 300
AI_QUEUE_NAMES = frozenset({"ai.interactive", "ai.batch"})
SUPPORTED_ADMISSION_USAGE_TYPES = frozenset({None, "ai_task"})


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TaskAdmission:
    usage_type: str | None
    cost_cny: Decimal = Decimal("0")

    @classmethod
    def ai(cls, cost_cny: Decimal = Decimal("0")) -> "TaskAdmission":
        return cls("ai_task", cost_cny)

    @classmethod
    def unmetered(cls) -> "TaskAdmission":
        return cls(None)


@dataclass(frozen=True)
class TaskClaim:
    task_id: str
    owner_user_id: str
    task_type: str
    token: str
    attempts: int
    max_attempts: int


@dataclass
class TaskClaimError(Exception):
    code: str
    message: str


class TaskServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class TaskService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
    ) -> None:
        self.sessions = sessions
        self.clock = clock or SystemClock()
        self.idempotency = IdempotencyService()

    async def create_task(
        self,
        owner_user_id: str,
        *,
        task_type: str,
        queue: str,
        trace_id: str,
        idempotency_key: str,
        admission: TaskAdmission,
        resource_type: str | None = None,
        resource_id: str | None = None,
        priority: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> Task:
        async with self.idempotency.transaction(self.sessions) as session:
            return await self.create_task_in_session(
                session,
                owner_user_id,
                task_type=task_type,
                queue=queue,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
                admission=admission,
                resource_type=resource_type,
                resource_id=resource_id,
                priority=priority,
                payload=payload,
            )

    async def create_task_in_session(
        self,
        session: AsyncSession,
        owner_user_id: str,
        *,
        task_type: str,
        queue: str,
        trace_id: str,
        idempotency_key: str,
        admission: TaskAdmission,
        resource_type: str | None = None,
        resource_id: str | None = None,
        priority: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> Task:
        if not isinstance(admission, TaskAdmission):
            raise TypeError("admission must be a TaskAdmission")
        if queue not in QUEUE_NAMES:
            raise TaskServiceError("TASK_QUEUE_INVALID", "Unsupported task queue", 422)
        if not is_valid_cost_cny(admission.cost_cny):
            raise TaskServiceError(
                "TASK_ADMISSION_INVALID",
                "AI task cost must be a finite non-negative Decimal",
                422,
            )
        if (
            admission.usage_type not in SUPPORTED_ADMISSION_USAGE_TYPES
            or (
                queue in AI_QUEUE_NAMES
                and admission.usage_type != "ai_task"
            )
        ):
            raise TaskServiceError(
                "TASK_ADMISSION_INVALID",
                "Unsupported admission strategy for task queue",
                422,
            )
        body = {
            "type": task_type,
            "queue": queue,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "priority": priority,
            "payload": payload or {},
            "usage_type": admission.usage_type,
            "cost_cny": format(admission.cost_cny.normalize(), "f"),
        }
        try:
            claim = await self.idempotency.claim(
                session,
                owner_user_id,
                "/internal/tasks",
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
            task = await session.scalar(
                select(Task).where(
                    Task.id == task_id,
                    Task.owner_user_id == claim.row.owner_user_id,
                )
            )
            if task is None:
                raise RuntimeError(
                    "idempotent task response references a missing task"
                )
            return task

        now = self.clock.now()
        task_id = new_id("tsk")
        reservation = await self._admit_usage(
            session,
            claim.row.owner_user_id,
            admission,
            trace_id,
            now,
            task_id,
        )
        task = Task(
            id=task_id,
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
            usage_type=admission.usage_type,
        )
        session.add(task)
        if reservation is not None:
            session.add(reservation)
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

    async def list_tasks(
        self,
        owner_user_id: str,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[Task], str | None]:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_user_id)
            query = (
                select(Task)
                .where(Task.owner_user_id.in_(owners))
                .order_by(Task.queued_at, Task.id)
            )
            if cursor:
                queued_at, identifier = _decode_cursor(cursor)
                query = query.where(
                    (Task.queued_at > queued_at)
                    | ((Task.queued_at == queued_at) & (Task.id > identifier))
                )
            rows = list((await session.scalars(query.limit(limit + 1))).all())
            page = rows[:limit]
            next_cursor = _cursor(page[-1]) if len(rows) > limit else None
            return page, next_cursor

    async def get_task(self, owner_user_id: str, task_id: str) -> Task | None:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_user_id)
            return await session.scalar(
                select(Task).where(
                    Task.id == task_id,
                    Task.owner_user_id.in_(owners),
                )
            )

    async def claim_task(
        self,
        owner_user_id: str,
        task_id: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> TaskClaim | None:
        now = self.clock.now()
        async with self.sessions.begin() as session:
            task = await session.scalar(
                select(Task)
                .where(
                    Task.id == task_id,
                    Task.owner_user_id == owner_user_id,
                )
                .with_for_update()
            )
            if task is None or task.status in TERMINAL_STATUSES:
                return None
            if task.cancellation_requested:
                await self._finish(session, task, "cancelled")
                return None
            live_lease = (
                task.status == "running"
                and task.claim_lease_expires_at is not None
                and _as_utc(task.claim_lease_expires_at) > now
            )
            if live_lease or task.status not in {"queued", "running"}:
                return None
            if task.attempts >= task.max_attempts:
                await self._finish(
                    session,
                    task,
                    "failed",
                    error_code="TASK_RETRIES_EXHAUSTED",
                )
                return None
            if task.status != "running":
                require_transition(task.status, "running")
            task.status = "running"
            task.stage = "running"
            task.started_at = task.started_at or now
            task.attempts += 1
            task.claim_token = new_id("clm")
            task.claim_lease_expires_at = now + timedelta(seconds=lease_seconds)
            await self._append_event(session, task, "running", task.progress, now)
            await session.flush()
            return TaskClaim(
                task.id,
                task.owner_user_id,
                task.type,
                task.claim_token,
                task.attempts,
                task.max_attempts,
            )

    async def report_progress(
        self,
        owner_user_id: str,
        task_id: str,
        claim_token: str,
        stage: str,
        progress: int,
    ) -> Task:
        if progress < 0 or progress > 99:
            raise TaskStateError("TASK_PROGRESS_INVALID", "Progress must be between 0 and 99")
        async with self.sessions.begin() as session:
            task = await self._claimed_task(
                session, owner_user_id, task_id, claim_token
            )
            if progress < task.progress:
                raise TaskStateError(
                    "TASK_PROGRESS_INVALID",
                    "Task progress cannot decrease",
                )
            task.stage = stage
            task.progress = progress
            task.claim_lease_expires_at = self.clock.now() + timedelta(
                seconds=DEFAULT_LEASE_SECONDS
            )
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
            task.ai_cancel_requested_at = (
                task.ai_cancel_requested_at or self.clock.now()
            )
            if task.status == "queued" and task.claim_token is None:
                await self._release_unused_ai_reservation_in_session(
                    session,
                    task,
                )
            await self._finish(session, task, "cancelled")
            return task

    async def request_cancel_idempotent(
        self,
        owner_user_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> Task:
        body = {"task_id": task_id}
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_user_id)
            try:
                claim = await self.idempotency.claim(
                    session,
                    owner,
                    "/v1/tasks/:task_id/cancel",
                    idempotency_key,
                    body,
                )
            except IdempotencyConflict as error:
                raise TaskServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            owners = await authorized_owner_ids(session, owner_user_id)
            if claim.is_replay:
                task = await session.scalar(
                    select(Task).where(
                        Task.id == (claim.replay_response or {})["id"],
                        Task.owner_user_id.in_(owners),
                    )
                )
                if task is None:
                    raise RuntimeError("Idempotent cancelled task is missing")
                return task
            task = await session.scalar(
                select(Task)
                .where(
                    Task.id == task_id,
                    Task.owner_user_id.in_(owners),
                )
                .with_for_update()
            )
            if task is None:
                raise TaskServiceError("RESOURCE_NOT_FOUND", "Task not found", 404)
            if task.status not in TERMINAL_STATUSES:
                task.cancellation_requested = True
                task.ai_cancel_requested_at = (
                    task.ai_cancel_requested_at or self.clock.now()
                )
                if task.status == "queued" and task.claim_token is None:
                    await self._release_unused_ai_reservation_in_session(
                        session,
                        task,
                    )
                await self._finish(session, task, "cancelled")
            response = self._task_payload(task)
            await self.idempotency.complete(session, claim, 200, response)
            return task

    async def register_ai_run(
        self,
        owner_user_id: str,
        task_id: str,
        claim_token: str,
        ai_run_id: str,
    ) -> bool:
        async with self.sessions.begin() as session:
            task = await session.scalar(
                select(Task)
                .where(
                    Task.id == task_id,
                    Task.owner_user_id == owner_user_id,
                )
                .with_for_update()
            )
            if task is None:
                raise TaskClaimError(
                    "TASK_CLAIM_STALE",
                    "Task claim is missing, expired or superseded",
                )
            if task.cancellation_requested:
                if task.claim_token != claim_token:
                    raise TaskClaimError(
                        "TASK_CLAIM_STALE",
                        "Task claim is missing, expired or superseded",
                    )
                if (
                    task.active_ai_run_id is not None
                    and task.active_ai_run_id != ai_run_id
                ):
                    raise TaskClaimError(
                        "TASK_AI_RUN_STALE",
                        "A cancelled task already references another AI run",
                    )
                if task.active_ai_run_id == ai_run_id:
                    return False
                task.active_ai_run_id = ai_run_id
                await self._bind_ai_reservation_in_session(
                    session,
                    owner_user_id,
                    task_id,
                    ai_run_id,
                )
                await session.flush()
                return False
            if (
                task.status != "running"
                or task.claim_token != claim_token
                or task.claim_lease_expires_at is None
                or _as_utc(task.claim_lease_expires_at) <= self.clock.now()
            ):
                raise TaskClaimError(
                    "TASK_CLAIM_STALE",
                    "Task claim is missing, expired or superseded",
                )
            if task.active_ai_run_id == ai_run_id:
                return True
            if task.active_ai_run_id is not None:
                raise TaskClaimError(
                    "TASK_AI_RUN_CONFLICT",
                    "Task claim already references another active AI run",
                )
            task.active_ai_run_id = ai_run_id
            task.ai_cancel_acknowledged_at = None
            await self._bind_ai_reservation_in_session(
                session,
                owner_user_id,
                task_id,
                ai_run_id,
            )
            await session.flush()
            return True

    async def consume_ai_reservation(
        self,
        owner_user_id: str,
        task_id: str,
        ai_run_id: str,
    ) -> UsageLedger:
        async with self.sessions.begin() as session:
            return await self.consume_ai_reservation_in_session(
                session,
                owner_user_id,
                task_id,
                ai_run_id,
            )

    async def consume_ai_reservation_in_session(
        self,
        session: AsyncSession,
        owner_user_id: str,
        task_id: str,
        ai_run_id: str,
    ) -> UsageLedger:
        return await self._consume_ai_reservation_in_session(
            session,
            owner_user_id,
            task_id,
            ai_run_id,
        )

    async def release_ai_reservation(
        self,
        owner_user_id: str,
        task_id: str,
    ) -> UsageLedger:
        async with self.sessions.begin() as session:
            return await self.release_ai_reservation_in_session(
                session,
                owner_user_id,
                task_id,
            )

    async def release_ai_reservation_in_session(
        self,
        session: AsyncSession,
        owner_user_id: str,
        task_id: str,
    ) -> UsageLedger:
        reservation = await self._usage_reservation(
            session,
            owner_user_id,
            task_id,
        )
        if reservation.state == "released":
            return reservation
        if reservation.state != "reserved":
            raise TaskServiceError(
                "AI_RESERVATION_STATE_INVALID",
                "Consumed AI reservation cannot be released",
                409,
            )
        reservation.state = "released"
        reservation.updated_at = self.clock.now()
        await session.flush()
        return reservation

    async def settle_ai_run(
        self,
        owner_user_id: str,
        task_id: str,
        ai_run_id: str,
    ) -> Task:
        async with self.sessions.begin() as session:
            return await self.settle_ai_run_in_session(
                session,
                owner_user_id,
                task_id,
                ai_run_id,
            )

    async def settle_ai_run_in_session(
        self,
        session: AsyncSession,
        owner_user_id: str,
        task_id: str,
        ai_run_id: str,
    ) -> Task:
        task = await session.scalar(
            select(Task)
            .where(
                Task.id == task_id,
                Task.owner_user_id == owner_user_id,
                Task.active_ai_run_id == ai_run_id,
            )
            .with_for_update()
        )
        if task is None:
            raise TaskClaimError(
                "TASK_AI_RUN_STALE",
                "AI run is missing or superseded",
            )
        if task.status == "running":
            task.active_ai_run_id = None
        elif task.status == "cancelled":
            task.ai_cancel_acknowledged_at = (
                task.ai_cancel_acknowledged_at or self.clock.now()
            )
            task.claim_token = None
            task.claim_lease_expires_at = None
        await session.flush()
        return task

    async def is_cancel_requested(
        self,
        owner_user_id: str,
        task_id: str,
    ) -> bool:
        async with self.sessions() as session:
            value = await session.scalar(
                select(Task.cancellation_requested).where(
                    Task.id == task_id,
                    Task.owner_user_id == owner_user_id,
                )
            )
            return value is True

    async def acknowledge_ai_cancel(
        self,
        owner_user_id: str,
        task_id: str,
        ai_run_id: str,
    ) -> Task:
        async with self.sessions.begin() as session:
            task = await session.scalar(
                select(Task)
                .where(
                    Task.id == task_id,
                    Task.owner_user_id == owner_user_id,
                    Task.active_ai_run_id == ai_run_id,
                    Task.cancellation_requested.is_(True),
                )
                .with_for_update()
            )
            if task is None:
                raise TaskClaimError(
                    "TASK_AI_RUN_STALE",
                    "AI run is missing, superseded or not cancelled",
                )
            task.ai_cancel_acknowledged_at = (
                task.ai_cancel_acknowledged_at or self.clock.now()
            )
            task.claim_token = None
            task.claim_lease_expires_at = None
            await session.flush()
            return task

    async def claimed_task_in_session(
        self,
        session: AsyncSession,
        owner_user_id: str,
        task_id: str,
        claim_token: str,
    ) -> Task:
        return await self._claimed_task(
            session,
            owner_user_id,
            task_id,
            claim_token,
        )

    async def complete_task_in_session(
        self,
        session: AsyncSession,
        task: Task,
        result_ref: str,
    ) -> Task:
        await self._release_unused_ai_reservation_in_session(session, task)
        await self._finish(
            session,
            task,
            "succeeded",
            result_ref=result_ref,
            progress=100,
        )
        return task

    async def release_claim_for_retry(
        self,
        owner_user_id: str,
        task_id: str,
        claim_token: str,
    ) -> None:
        async with self.sessions.begin() as session:
            task = await self._claimed_task(
                session, owner_user_id, task_id, claim_token
            )
            task.claim_lease_expires_at = self.clock.now()
            await session.flush()

    async def complete_task(
        self,
        owner_user_id: str,
        task_id: str,
        claim_token: str,
        result_ref: str,
    ) -> Task:
        async with self.sessions.begin() as session:
            task = await self._claimed_task(
                session, owner_user_id, task_id, claim_token
            )
            await self._release_unused_ai_reservation_in_session(session, task)
            await self._finish(
                session,
                task,
                "succeeded",
                result_ref=result_ref,
                progress=100,
            )
            return task

    async def fail_task(
        self,
        owner_user_id: str,
        task_id: str,
        claim_token: str,
        error_code: str,
        *,
        release_unused_ai_reservation: bool = False,
    ) -> Task:
        async with self.sessions.begin() as session:
            task = await self._claimed_task(
                session, owner_user_id, task_id, claim_token
            )
            if release_unused_ai_reservation:
                await self._release_unused_ai_reservation_in_session(session, task)
            await self._finish(session, task, "failed", error_code=error_code)
            return task

    async def finalize_unused_ai_reservation(
        self,
        owner_user_id: str,
        task_id: str,
        claim_token: str,
    ) -> Task | None:
        async with self.sessions.begin() as session:
            task = await session.scalar(
                select(Task)
                .where(
                    Task.id == task_id,
                    Task.owner_user_id == owner_user_id,
                    Task.claim_token == claim_token,
                    Task.status.in_(("cancelled", "failed")),
                    Task.active_ai_run_id.is_(None),
                )
                .with_for_update()
            )
            if task is None:
                return None
            released = await self._release_unused_ai_reservation_in_session(
                session,
                task,
            )
            if not released:
                return task
            task.claim_token = None
            task.claim_lease_expires_at = None
            await session.flush()
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
                .where(
                    TaskEvent.task_id == task_id,
                    TaskEvent.owner_user_id.in_(owners),
                    TaskEvent.seq > after_seq,
                )
                .order_by(TaskEvent.seq)
            )
            return tuple(rows.all())

    async def _admit_usage(
        self,
        session: AsyncSession,
        owner_user_id: str,
        admission: TaskAdmission,
        trace_id: str,
        now: datetime,
        task_id: str,
    ) -> UsageLedger | None:
        if admission.usage_type is None:
            return None
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    f"{GLOBAL_AI_COST_ADVISORY_LOCK_ID})"
                )
            )
        owners = await authorized_owner_ids(session, owner_user_id)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_tasks = int(
            await session.scalar(
                select(func.coalesce(func.sum(UsageLedger.quantity), 0)).where(
                    UsageLedger.owner_user_id.in_(owners),
                    UsageLedger.usage_type == admission.usage_type,
                    UsageLedger.state.in_(("reserved", "consumed")),
                    UsageLedger.created_at >= day_start,
                )
            )
            or 0
        )
        running_tasks = int(
            await session.scalar(
                select(func.count()).select_from(Task).where(
                    Task.owner_user_id.in_(owners),
                    Task.usage_type == admission.usage_type,
                    Task.status.in_(("queued", "running")),
                )
            )
            or 0
        )
        retry_after = int((day_start + timedelta(days=1) - now).total_seconds())
        cost = Decimal(
            await session.scalar(
                select(func.coalesce(func.sum(UsageLedger.cost_cny), 0)).where(
                    UsageLedger.state.in_(("reserved", "consumed")),
                    UsageLedger.created_at >= day_start,
                )
            )
            or 0
        )
        decision = evaluate_admission_usage(
            cost,
            admission.cost_cny,
            daily_tasks,
            running_tasks,
            retry_after,
            False,
        )
        if not decision.allowed:
            raise TaskServiceError(
                decision.reason or "AI_LIMIT_REACHED",
                "AI task admission was denied",
                429,
            )
        return UsageLedger(
            id=new_id("usg"),
            owner_user_id=owner_user_id,
            usage_type=admission.usage_type,
            quantity=1,
            cost_cny=admission.cost_cny,
            trace_id=trace_id,
            state="reserved",
            task_id=task_id,
            created_at=now,
            updated_at=now,
        )

    async def _usage_reservation(
        self,
        session: AsyncSession,
        owner_user_id: str,
        task_id: str,
    ) -> UsageLedger:
        reservation = await session.scalar(
            select(UsageLedger)
            .where(
                UsageLedger.owner_user_id == owner_user_id,
                UsageLedger.task_id == task_id,
                UsageLedger.usage_type == "ai_task",
            )
            .with_for_update()
        )
        if reservation is None:
            raise TaskServiceError(
                "AI_RESERVATION_NOT_FOUND",
                "AI reservation not found",
                404,
            )
        return reservation

    async def _consume_ai_reservation_in_session(
        self,
        session: AsyncSession,
        owner_user_id: str,
        task_id: str,
        ai_run_id: str,
    ) -> UsageLedger:
        reservation = await self._usage_reservation(
            session,
            owner_user_id,
            task_id,
        )
        if reservation.state == "released":
            raise TaskServiceError(
                "AI_RESERVATION_STATE_INVALID",
                "Released AI reservation cannot be consumed",
                409,
            )
        if reservation.state == "consumed" and reservation.ai_run_id != ai_run_id:
            raise TaskServiceError(
                "AI_RESERVATION_STATE_INVALID",
                "Consumed AI reservation belongs to another AI run",
                409,
            )
        if reservation.state == "reserved":
            reservation.state = "consumed"
            reservation.ai_run_id = ai_run_id
            reservation.updated_at = self.clock.now()
            await session.flush()
        return reservation

    async def _bind_ai_reservation_in_session(
        self,
        session: AsyncSession,
        owner_user_id: str,
        task_id: str,
        ai_run_id: str,
    ) -> UsageLedger:
        reservation = await self._usage_reservation(
            session,
            owner_user_id,
            task_id,
        )
        if reservation.state == "released":
            raise TaskServiceError(
                "AI_RESERVATION_STATE_INVALID",
                "Released AI reservation cannot be bound to an AI run",
                409,
            )
        if reservation.state == "reserved":
            reservation.state = "consumed"
            reservation.ai_run_id = ai_run_id
            reservation.updated_at = self.clock.now()
            await session.flush()
        return reservation

    async def _release_unused_ai_reservation_in_session(
        self,
        session: AsyncSession,
        task: Task,
    ) -> bool:
        if task.usage_type != "ai_task" or task.active_ai_run_id is not None:
            return False
        reservation = await session.scalar(
            select(UsageLedger)
            .where(
                UsageLedger.owner_user_id == task.owner_user_id,
                UsageLedger.task_id == task.id,
                UsageLedger.usage_type == "ai_task",
                UsageLedger.state == "reserved",
            )
            .with_for_update()
        )
        if reservation is not None:
            reservation.state = "released"
            reservation.updated_at = self.clock.now()
            await session.flush()
            return True
        return False

    async def _claimed_task(
        self,
        session: AsyncSession,
        owner_user_id: str,
        task_id: str,
        claim_token: str,
    ) -> Task:
        task = await session.scalar(
            select(Task)
            .where(
                Task.id == task_id,
                Task.owner_user_id == owner_user_id,
                Task.claim_token == claim_token,
                Task.status == "running",
            )
            .with_for_update()
        )
        if (
            task is None
            or task.claim_lease_expires_at is None
            or _as_utc(task.claim_lease_expires_at) <= self.clock.now()
        ):
            raise TaskClaimError(
                "TASK_CLAIM_STALE",
                "Task claim is missing, expired or superseded",
            )
        return task

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
        now = self.clock.now()
        task.status = status
        task.stage = status
        task.finished_at = now
        task.result_ref = result_ref
        task.error_code = error_code
        if status != "cancelled" or task.usage_type != "ai_task":
            task.claim_token = None
            task.claim_lease_expires_at = None
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
                    TaskEvent.task_id == task.id,
                    TaskEvent.owner_user_id == task.owner_user_id,
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
                created_at=created_at or self.clock.now(),
            )
        )

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


def _cursor(task: Task) -> str:
    value = f"{_as_utc(task.queued_at).isoformat()}|{task.id}"
    return urlsafe_b64encode(value.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        decoded = urlsafe_b64decode(cursor.encode()).decode()
        queued_at, identifier = decoded.split("|", 1)
        if not identifier:
            raise ValueError
        return datetime.fromisoformat(queued_at), identifier
    except (UnicodeError, ValueError) as error:
        raise TaskServiceError("VALIDATION_FAILED", "Invalid cursor", 422) from error


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
