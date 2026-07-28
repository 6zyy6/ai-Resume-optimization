import pytest
from sqlalchemy import func, select

from app.db.models import Outbox, Task, User
from app.modules.tasks.service import TaskAdmission, TaskService
from app.workers.dispatcher import OutboxDispatcher, TaskQueueBusy
from app.workers.celery_app import celery_app
from app.workers.execution import QUEUE_NAMES


pytestmark = pytest.mark.anyio


class FlakyPublisher:
    def __init__(self) -> None:
        self.available = False
        self.deliveries: list[tuple[str, str]] = []

    def publish(self, task_id: str, owner_user_id: str, queue: str) -> None:
        if not self.available:
            raise ConnectionError("redis unavailable")
        self.deliveries.append((task_id, queue))


async def _seed_user(sessions) -> None:
    async with sessions.begin() as session:
        session.add(User(id="usr_outbox"))


async def test_task_and_outbox_are_persisted_in_one_transaction(sql_session_factory):
    """Committing a task without its Outbox row would strand durable work."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)

    task = await service.create_task(
        "usr_outbox",
        task_type="resume_parse",
        queue="file.parse",
        trace_id="tr_outbox",
        idempotency_key="outbox-1",
        admission=TaskAdmission.unmetered(),
        payload={"task_id": "spoofed"},
    )

    async with sql_session_factory() as session:
        stored_task = await session.get(Task, task.id)
        outbox = await session.scalar(select(Outbox).where(Outbox.task_id == task.id))
    assert stored_task is not None
    assert outbox is not None
    assert outbox.queue == "file.parse"
    assert outbox.payload == {"task_id": task.id}


async def test_dispatch_retry_reuses_one_task_and_one_outbox_row(sql_session_factory):
    """Creating new durable rows on broker retry would duplicate business work."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_outbox",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_retry",
        idempotency_key="outbox-2",
        admission=TaskAdmission.ai(),
    )
    publisher = FlakyPublisher()
    dispatcher = OutboxDispatcher(
        sql_session_factory,
        publisher,
        retry_base_seconds=0,
        jitter=lambda: 0,
    )

    try:
        await dispatcher.dispatch_task(task.id)
    except TaskQueueBusy as error:
        assert error.code == "TASK_QUEUE_BUSY"
    else:
        raise AssertionError("Redis failure did not surface TASK_QUEUE_BUSY")
    publisher.available = True
    await dispatcher.dispatch_task(task.id)

    async with sql_session_factory() as session:
        task_count = await session.scalar(select(func.count()).select_from(Task))
        outbox_count = await session.scalar(select(func.count()).select_from(Outbox))
        outbox = await session.scalar(select(Outbox).where(Outbox.task_id == task.id))
    assert task_count == 1
    assert outbox_count == 1
    assert outbox is not None
    assert outbox.attempts == 2
    assert outbox.dispatched_at is not None
    assert publisher.deliveries == [(task.id, "ai.interactive")]


async def test_idempotent_replay_does_not_duplicate_task_or_usage(sql_session_factory):
    """Replaying an admitted task must not consume usage or durable rows twice."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    first = await service.create_task(
        "usr_outbox",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_first",
        idempotency_key="outbox-replay",
        admission=TaskAdmission.ai(),
    )

    replay = await service.create_task(
        "usr_outbox",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_retry",
        idempotency_key="outbox-replay",
        admission=TaskAdmission.ai(),
    )

    async with sql_session_factory() as session:
        task_count = await session.scalar(select(func.count()).select_from(Task))
        outbox_count = await session.scalar(select(func.count()).select_from(Outbox))
    assert replay.id == first.id
    assert task_count == 1
    assert outbox_count == 1


def test_only_documented_worker_queues_are_accepted():
    """Routing to a misspelled queue would leave a task without a worker."""
    assert QUEUE_NAMES == (
        "ai.interactive",
        "ai.batch",
        "file.parse",
        "file.export",
        "privacy",
    )


def test_celery_app_registers_the_dispatched_execution_entrypoint():
    """Publishing a task name absent from the worker registry would drop every job."""
    assert "app.workers.execution.execute_task" in celery_app.tasks
