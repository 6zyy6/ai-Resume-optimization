from datetime import timezone

import pytest
from sqlalchemy import select

from app.db.models import TaskEvent, UsageLedger, User
from app.modules.tasks.service import TaskAdmission, TaskClaimError, TaskService


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
        admission=TaskAdmission.ai(),
    )
    claimed = await service.claim_task("usr_tasks", created.id)
    progressed = await service.report_progress(
        "usr_tasks", created.id, claimed.token, "drafting", 40
    )
    completed = await service.complete_task(
        "usr_tasks", created.id, claimed.token, "resume_version:rv_1"
    )
    events = await service.list_events("usr_tasks", created.id)

    assert created.status == "queued"
    assert claimed is not None
    assert claimed.task_id == created.id
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
        admission=TaskAdmission.unmetered(),
    )
    claim = await service.claim_task("usr_tasks", task.id)
    assert claim is not None
    await service.complete_task("usr_tasks", task.id, claim.token, "file:file_1")

    assert await service.claim_task("usr_tasks", task.id) is None
    try:
        await service.fail_task(
            "usr_tasks", task.id, claim.token, "LATE_FAILURE"
        )
    except TaskClaimError as error:
        assert error.code == "TASK_CLAIM_STALE"
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
        admission=TaskAdmission.ai(),
    )
    claim = await service.claim_task("usr_tasks", task.id)
    assert claim is not None

    cancelled = await service.request_cancel("usr_tasks", task.id)
    with pytest.raises(TaskClaimError):
        await service.complete_task(
            "usr_tasks", task.id, claim.token, "resume_version:late"
        )
    late_completion = await service.get_task("usr_tasks", task.id)

    assert cancelled.status == "cancelled"
    assert cancelled.cancellation_requested is True
    assert late_completion is not None
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


async def test_ai_run_binding_survives_cancel_and_records_acknowledgement(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_tasks",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_ai_cancel",
        idempotency_key="ai-cancel-1",
        admission=TaskAdmission.ai(),
    )
    claim = await service.claim_task("usr_tasks", task.id)
    assert claim is not None

    registered = await service.register_ai_run(
        "usr_tasks",
        task.id,
        claim.token,
        "run_active",
    )
    cancelled = await service.request_cancel("usr_tasks", task.id)
    acknowledged = await service.acknowledge_ai_cancel(
        "usr_tasks",
        task.id,
        "run_active",
    )

    assert registered is True
    assert await service.is_cancel_requested("usr_tasks", task.id) is True
    assert cancelled.status == "cancelled"
    assert cancelled.active_ai_run_id == "run_active"
    assert cancelled.ai_cancel_requested_at is not None
    assert acknowledged.ai_cancel_acknowledged_at is not None
    assert acknowledged.claim_token is None
    assert (
        acknowledged.ai_cancel_acknowledged_at.replace(tzinfo=timezone.utc)
        >= acknowledged.ai_cancel_requested_at.replace(tzinfo=timezone.utc)
    )


async def test_ai_run_created_during_cancel_is_bound_but_cannot_continue(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_tasks",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_cancel_race",
        idempotency_key="cancel-race-1",
        admission=TaskAdmission.ai(),
    )
    claim = await service.claim_task("usr_tasks", task.id)
    assert claim is not None
    await service.request_cancel("usr_tasks", task.id)

    with pytest.raises(TaskClaimError):
        await service.register_ai_run(
            "usr_tasks",
            task.id,
            "clm_stale",
            "run_from_stale_worker",
        )
    should_continue = await service.register_ai_run(
        "usr_tasks",
        task.id,
        claim.token,
        "run_created_during_cancel",
    )
    stored = await service.get_task("usr_tasks", task.id)

    assert should_continue is False
    assert stored is not None
    assert stored.status == "cancelled"
    assert stored.active_ai_run_id == "run_created_during_cancel"


async def test_same_claim_cannot_replace_an_active_ai_run(sql_session_factory):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_tasks",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_run_conflict",
        idempotency_key="run-conflict-1",
        admission=TaskAdmission.ai(),
    )
    claim = await service.claim_task("usr_tasks", task.id)
    assert claim is not None

    first = await service.register_ai_run(
        "usr_tasks",
        task.id,
        claim.token,
        "run_first",
    )
    replay = await service.register_ai_run(
        "usr_tasks",
        task.id,
        claim.token,
        "run_first",
    )
    with pytest.raises(TaskClaimError) as conflict:
        await service.register_ai_run(
            "usr_tasks",
            task.id,
            claim.token,
            "run_second",
        )
    stored = await service.get_task("usr_tasks", task.id)

    assert first is True
    assert replay is True
    assert conflict.value.code == "TASK_AI_RUN_CONFLICT"
    assert stored is not None
    assert stored.active_ai_run_id == "run_first"


async def test_first_ai_run_consumes_one_reservation_and_later_run_reuses_it(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_tasks",
        task_type="match_resume",
        queue="ai.batch",
        trace_id="tr_two_runs",
        idempotency_key="two-runs-1",
        admission=TaskAdmission.ai(),
    )
    claim = await service.claim_task("usr_tasks", task.id)
    assert claim is not None

    await service.register_ai_run("usr_tasks", task.id, claim.token, "run_match")
    await service.settle_ai_run("usr_tasks", task.id, "run_match")
    await service.register_ai_run(
        "usr_tasks",
        task.id,
        claim.token,
        "run_suggestions",
    )

    async with sql_session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(UsageLedger).where(
                        UsageLedger.owner_user_id == "usr_tasks",
                        UsageLedger.task_id == task.id,
                    )
                )
            ).all()
        )

    assert len(rows) == 1
    assert rows[0].state == "consumed"
    assert rows[0].quantity == 1
    assert rows[0].ai_run_id == "run_match"


async def test_rule_only_ai_task_releases_unused_reservation(sql_session_factory):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_tasks",
        task_type="rule_only",
        queue="ai.interactive",
        trace_id="tr_rule_only",
        idempotency_key="rule-only-1",
        admission=TaskAdmission.ai(),
    )
    claim = await service.claim_task("usr_tasks", task.id)
    assert claim is not None

    await service.complete_task("usr_tasks", task.id, claim.token, "rule:done")

    async with sql_session_factory() as session:
        reservation = await session.scalar(
            select(UsageLedger).where(
                UsageLedger.owner_user_id == "usr_tasks",
                UsageLedger.task_id == task.id,
            )
        )

    assert reservation is not None
    assert reservation.state == "released"
    assert reservation.ai_run_id is None


async def test_settling_cancelled_ai_run_only_acknowledges_cancellation(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_tasks",
        task_type="cancel_settle",
        queue="ai.interactive",
        trace_id="tr_cancel_settle",
        idempotency_key="cancel-settle-1",
        admission=TaskAdmission.ai(),
    )
    claim = await service.claim_task("usr_tasks", task.id)
    assert claim is not None
    await service.register_ai_run(
        "usr_tasks",
        task.id,
        claim.token,
        "run_cancel_settle",
    )
    await service.request_cancel("usr_tasks", task.id)

    settled = await service.settle_ai_run(
        "usr_tasks",
        task.id,
        "run_cancel_settle",
    )

    assert settled.status == "cancelled"
    assert settled.active_ai_run_id == "run_cancel_settle"
    assert settled.ai_cancel_acknowledged_at is not None
    assert settled.claim_token is None
