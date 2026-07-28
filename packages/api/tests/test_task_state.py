import pytest
from sqlalchemy import select

from app.db.models import TaskEvent, User
from app.modules.tasks.service import TaskService
from app.modules.tasks.state import TaskStateError


pytestmark = pytest.mark.anyio


async def _seed_user(sessions) -> None:
    async with sessions.begin() as session:
        session.add(User(id="usr_tasks"))


async def test_task_progress_events_start_at_one_and_follow_state(sql_session_factory):
    """Starting event sequences at zero or skipping persisted progress breaks SSE resume."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)

    created = await service.create_task(
        "usr_tasks",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_state",
        idempotency_key="state-1",
    )
    claimed = await service.claim_task(created.id)
    progressed = await service.report_progress(created.id, "drafting", 40)
    completed = await service.complete_task(created.id, "resume_version:rv_1")
    events = await service.list_events("usr_tasks", created.id)

    assert created.status == "queued"
    assert claimed is not None
    assert claimed.status == "running"
    assert progressed.stage == "drafting"
    assert progressed.progress == 40
    assert completed.status == "succeeded"
    assert completed.progress == 100
    assert completed.result_ref == "resume_version:rv_1"
    assert [(event.seq, event.stage, event.progress) for event in events] == [
        (1, "queued", 0),
        (2, "running", 0),
        (3, "drafting", 40),
        (4, "succeeded", 100),
    ]


async def test_terminal_task_cannot_regress(sql_session_factory):
    """Allowing a terminal task to be claimed or failed would regress its final result."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_tasks",
        task_type="file_export",
        queue="file.export",
        trace_id="tr_terminal",
        idempotency_key="terminal-1",
    )
    await service.claim_task(task.id)
    await service.complete_task(task.id, "file:file_1")

    assert await service.claim_task(task.id) is None
    try:
        await service.fail_task(task.id, "LATE_FAILURE")
    except TaskStateError as error:
        assert error.code == "TASK_TERMINAL"
    else:
        raise AssertionError("a succeeded task accepted a later failure")

    stored = await service.get_task("usr_tasks", task.id)
    assert stored is not None
    assert stored.status == "succeeded"
    assert stored.result_ref == "file:file_1"


async def test_cancelled_task_discards_late_business_result(sql_session_factory):
    """A worker finishing after cancellation must not publish its business result."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_tasks",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_cancel",
        idempotency_key="cancel-1",
    )
    await service.claim_task(task.id)

    cancelled = await service.request_cancel("usr_tasks", task.id)
    late_completion = await service.complete_task(task.id, "resume_version:late")

    assert cancelled.status == "cancelled"
    assert cancelled.cancellation_requested is True
    assert late_completion.status == "cancelled"
    assert late_completion.result_ref is None
    async with sql_session_factory() as session:
        terminal_events = list(
            (
                await session.scalars(
                    select(TaskEvent).where(
                        TaskEvent.task_id == task.id,
                        TaskEvent.stage.in_(("cancelled", "succeeded")),
                    )
                )
            ).all()
        )
    assert [(event.stage, event.progress) for event in terminal_events] == [
        ("cancelled", cancelled.progress)
    ]
