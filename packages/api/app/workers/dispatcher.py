import inspect
import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Outbox
from app.workers.celery_app import celery_app


class Publisher(Protocol):
    def publish(self, task_id: str, queue: str) -> object: ...


class TaskQueueBusy(Exception):
    code = "TASK_QUEUE_BUSY"
    message = "Task queue is temporarily unavailable"
    status_code = 503


class CeleryPublisher:
    def publish(self, task_id: str, queue: str) -> None:
        celery_app.send_task(
            "app.workers.execution.execute_task",
            args=[task_id],
            queue=queue,
            task_id=task_id,
        )


class OutboxDispatcher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        publisher: Publisher,
        *,
        retry_base_seconds: float = 1,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.sessions = sessions
        self.publisher = publisher
        self.retry_base_seconds = retry_base_seconds
        self.jitter = jitter

    async def dispatch_task(self, task_id: str) -> bool:
        error: BaseException | None = None
        dispatched = False
        async with self.sessions.begin() as session:
            outbox = await session.scalar(
                select(Outbox)
                .where(Outbox.task_id == task_id)
                .with_for_update()
            )
            if outbox is None:
                raise RuntimeError("task has no Outbox row")
            if outbox.dispatched_at is not None:
                return False
            now = datetime.now(timezone.utc)
            available_at = _as_utc(outbox.available_at)
            if available_at > now:
                error = TaskQueueBusy()
            else:
                outbox.attempts += 1
                try:
                    result = self.publisher.publish(outbox.task_id, outbox.queue)
                    if inspect.isawaitable(result):
                        await result
                except BaseException as caught:
                    error = caught
                    delay = self.retry_base_seconds * (2 ** (outbox.attempts - 1))
                    outbox.available_at = now + timedelta(
                        seconds=delay + max(0.0, self.jitter())
                    )
                    outbox.last_error = type(caught).__name__
                else:
                    outbox.dispatched_at = now
                    outbox.last_error = None
                    dispatched = True
            await session.flush()
        if error is not None:
            raise TaskQueueBusy() from error
        return dispatched

    async def dispatch_pending(self, limit: int = 100) -> int:
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            task_ids = tuple(
                (
                    await session.scalars(
                        select(Outbox.task_id)
                        .where(
                            Outbox.dispatched_at.is_(None),
                            Outbox.available_at <= now,
                            Outbox.attempts < 3,
                        )
                        .order_by(Outbox.created_at, Outbox.id)
                        .limit(limit)
                    )
                ).all()
            )
        dispatched = 0
        for task_id in task_ids:
            try:
                dispatched += int(await self.dispatch_task(task_id))
            except TaskQueueBusy:
                continue
        return dispatched


def build_default_dispatcher(
    sessions: async_sessionmaker[AsyncSession],
) -> OutboxDispatcher:
    return OutboxDispatcher(sessions, CeleryPublisher())


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
