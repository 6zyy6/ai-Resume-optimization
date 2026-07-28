import asyncio
import inspect
import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.ids import new_id
from app.db.models import Outbox, Task, TaskEvent
from app.modules.tasks.state import TERMINAL_STATUSES
from app.workers.celery_app import celery_app


MAX_DISPATCH_ATTEMPTS = 3


class Publisher(Protocol):
    def publish(
        self,
        task_id: str,
        owner_user_id: str,
        queue: str,
    ) -> object: ...


class TaskQueueBusy(Exception):
    code = "TASK_QUEUE_BUSY"
    message = "Task queue is temporarily unavailable"
    status_code = 503


class CeleryPublisher:
    def publish(self, task_id: str, owner_user_id: str, queue: str) -> None:
        celery_app.send_task(
            "app.workers.execution.execute_task",
            args=[task_id, owner_user_id],
            queue=queue,
            task_id=task_id,
            retry=False,
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
        error: Exception | None = None
        dispatched = False
        async with self.sessions.begin() as session:
            outbox = await session.scalar(
                select(Outbox)
                .where(Outbox.task_id == task_id)
                .with_for_update()
            )
            if outbox is None:
                raise RuntimeError("task has no Outbox row")
            dispatched, error = await self._dispatch_locked(session, outbox)
        if error is not None:
            raise TaskQueueBusy() from error
        return dispatched

    async def dispatch_pending(self, limit: int = 100) -> int:
        now = datetime.now(timezone.utc)
        dispatched = 0
        async with self.sessions.begin() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(Outbox)
                        .where(
                            Outbox.dispatched_at.is_(None),
                            Outbox.exhausted_at.is_(None),
                            Outbox.available_at <= now,
                        )
                        .order_by(Outbox.created_at, Outbox.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for outbox in rows:
                sent, _ = await self._dispatch_locked(session, outbox)
                dispatched += int(sent)
        return dispatched

    async def _dispatch_locked(
        self,
        session: AsyncSession,
        outbox: Outbox,
    ) -> tuple[bool, Exception | None]:
        if outbox.dispatched_at is not None or outbox.exhausted_at is not None:
            return False, None
        now = datetime.now(timezone.utc)
        if _as_utc(outbox.available_at) > now:
            return False, TaskQueueBusy()
        outbox.attempts += 1
        try:
            result = self.publisher.publish(
                outbox.task_id,
                outbox.owner_user_id,
                outbox.queue,
            )
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            outbox.last_error = type(error).__name__
            if outbox.attempts >= MAX_DISPATCH_ATTEMPTS:
                outbox.exhausted_at = now
                await self._fail_exhausted_task(session, outbox, now)
            else:
                delay = self.retry_base_seconds * (2 ** (outbox.attempts - 1))
                outbox.available_at = now + timedelta(
                    seconds=delay + max(0.0, self.jitter())
                )
            await session.flush()
            return False, error
        outbox.dispatched_at = now
        outbox.last_error = None
        await session.flush()
        return True, None

    @staticmethod
    async def _fail_exhausted_task(
        session: AsyncSession,
        outbox: Outbox,
        now: datetime,
    ) -> None:
        task = await session.scalar(
            select(Task)
            .where(
                Task.id == outbox.task_id,
                Task.owner_user_id == outbox.owner_user_id,
            )
            .with_for_update()
        )
        if task is None or task.status in TERMINAL_STATUSES:
            return
        task.status = "failed"
        task.stage = "failed"
        task.error_code = "TASK_QUEUE_UNAVAILABLE"
        task.finished_at = now
        task.claim_token = None
        task.claim_lease_expires_at = None
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
                stage="failed",
                progress=task.progress,
                created_at=now,
            )
        )
        await session.flush()


async def drain_forever(
    dispatcher: OutboxDispatcher,
    *,
    poll_interval: float = 1,
    stop_event: asyncio.Event | None = None,
) -> None:
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        await dispatcher.dispatch_pending()
        if stop.is_set():
            return
        if poll_interval <= 0:
            await asyncio.sleep(0)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except TimeoutError:
            continue


def build_default_dispatcher(
    sessions: async_sessionmaker[AsyncSession],
) -> OutboxDispatcher:
    return OutboxDispatcher(sessions, CeleryPublisher())


async def _main() -> None:
    engine = create_async_engine(get_settings().database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await drain_forever(build_default_dispatcher(sessions))
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_main())


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    main()
