import asyncio

import pytest
from sqlalchemy import select

from app.db.models import Outbox, TaskEvent, User
from app.modules.tasks.service import TaskAdmission, TaskService
from app.workers.celery_app import celery_app
from app.workers.dispatcher import OutboxDispatcher, TaskQueueBusy, drain_forever
from app.workers.execution import (
    HttpServiceError,
    configure_worker,
)


pytestmark = pytest.mark.anyio


class AlwaysDown:
    def publish(self, task_id: str, owner_user_id: str, queue: str) -> None:
        raise ConnectionError("redis unavailable")


class RecoveringPublisher:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.calls = 0

    def publish(self, task_id: str, owner_user_id: str, queue: str) -> None:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("redis unavailable")
        self.stop.set()


class CancelledPublisher:
    def publish(self, task_id: str, owner_user_id: str, queue: str) -> None:
        raise asyncio.CancelledError


async def _seed_user(sessions) -> None:
    async with sessions.begin() as session:
        session.add(User(id="usr_worker"))


async def _task(service: TaskService, key: str):
    return await service.create_task(
        "usr_worker",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id=f"tr_{key}",
        idempotency_key=key,
        admission=TaskAdmission.ai(),
    )


async def test_outbox_exhaustion_fails_task_with_observable_event(sql_session_factory):
    """Excluding an exhausted Outbox row must not leave its Task silently queued."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await _task(service, "dispatch-exhaust")
    dispatcher = OutboxDispatcher(
        sql_session_factory,
        AlwaysDown(),
        retry_base_seconds=0,
        jitter=lambda: 0,
    )

    for _ in range(3):
        with pytest.raises(TaskQueueBusy):
            await dispatcher.dispatch_task(task.id)

    stored = await service.get_task("usr_worker", task.id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error_code == "TASK_QUEUE_UNAVAILABLE"
    async with sql_session_factory() as session:
        outbox = await session.scalar(select(Outbox).where(Outbox.task_id == task.id))
        event = await session.scalar(
            select(TaskEvent).where(
                TaskEvent.task_id == task.id,
                TaskEvent.stage == "failed",
            )
        )
    assert outbox is not None
    assert outbox.exhausted_at is not None
    assert event is not None


async def test_continuous_drain_recovers_without_client_retry(sql_session_factory):
    """A committed Outbox row must be retried even when the client never returns."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await _task(service, "dispatch-drain")
    stop = asyncio.Event()
    publisher = RecoveringPublisher(stop)
    dispatcher = OutboxDispatcher(
        sql_session_factory,
        publisher,
        retry_base_seconds=0,
        jitter=lambda: 0,
    )

    await asyncio.wait_for(
        drain_forever(dispatcher, poll_interval=0, stop_event=stop),
        timeout=1,
    )

    async with sql_session_factory() as session:
        outbox = await session.scalar(select(Outbox).where(Outbox.task_id == task.id))
    assert publisher.calls == 2
    assert outbox is not None
    assert outbox.dispatched_at is not None


async def test_dispatcher_does_not_swallow_process_cancellation(sql_session_factory):
    """Worker shutdown must propagate instead of becoming TASK_QUEUE_BUSY."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await _task(service, "dispatch-cancel")
    dispatcher = OutboxDispatcher(
        sql_session_factory,
        CancelledPublisher(),
        retry_base_seconds=0,
        jitter=lambda: 0,
    )

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.dispatch_task(task.id)


async def test_registered_celery_entry_uses_task_executor_retries(sql_session_factory):
    """Calling the raw handler would skip persistent claim attempts and retry policy."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await _task(service, "celery-executor")
    calls = 0

    async def operation(_claim) -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise HttpServiceError(503)
        return "resume_version:rv_worker"

    configure_worker(service, lambda task_type: operation)
    registered = celery_app.tasks["app.workers.execution.execute_task"]
    result = await asyncio.to_thread(registered.run, task.id, "usr_worker")

    assert result["status"] == "succeeded"
    assert result["result_ref"] == "resume_version:rv_worker"
    assert calls == 3
    stored = await service.get_task("usr_worker", task.id)
    assert stored is not None
    assert stored.attempts == 3


async def test_unknown_worker_operation_fails_task_instead_of_leaking_live_claim(
    sql_session_factory,
):
    """Resolver failures must pass through TaskExecutor's persistent failure path."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await _task(service, "celery-unknown")

    def missing_operation(_task_type: str):
        raise KeyError("operation not registered")

    configure_worker(service, missing_operation)
    registered = celery_app.tasks["app.workers.execution.execute_task"]
    result = await asyncio.to_thread(registered.run, task.id, "usr_worker")

    assert result["status"] == "failed"
    assert result["error_code"] == "KeyError"
    stored = await service.get_task("usr_worker", task.id)
    assert stored is not None
    assert stored.claim_token is None


async def test_worker_reuses_one_event_loop_across_tasks(sql_session_factory):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    first = await _task(service, "worker-loop-first")
    second = await _task(service, "worker-loop-second")
    loops = []

    async def operation(_claim) -> str:
        loops.append(asyncio.get_running_loop())
        return "resume_version:rv_worker"

    configure_worker(service, lambda task_type: operation)
    registered = celery_app.tasks["app.workers.execution.execute_task"]
    first_result = await asyncio.to_thread(registered.run, first.id, "usr_worker")
    second_result = await asyncio.to_thread(registered.run, second.id, "usr_worker")

    assert first_result["status"] == "succeeded"
    assert second_result["status"] == "succeeded"
    assert loops[0] is loops[1]
