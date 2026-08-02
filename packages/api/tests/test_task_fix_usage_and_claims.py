import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.models import Outbox, Task, UsageLedger, User
from app.modules.tasks.service import (
    TaskAdmission,
    TaskClaimError,
    TaskService,
    TaskServiceError,
)


pytestmark = pytest.mark.anyio


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


async def _seed_users(sessions) -> None:
    async with sessions.begin() as session:
        session.add_all([User(id="usr_claim_a"), User(id="usr_claim_b")])


async def test_task_creation_requires_an_explicit_admission(sql_session_factory):
    """Making admission optional would let internal callers bypass every usage gate."""
    await _seed_users(sql_session_factory)
    service = TaskService(sql_session_factory)

    with pytest.raises(TypeError):
        await service.create_task(
            "usr_claim_a",
            task_type="resume_optimize",
            queue="ai.interactive",
            trace_id="tr_no_admission",
            idempotency_key="no-admission",
        )

    async with sql_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0


@pytest.mark.parametrize(
    ("queue", "admission"),
    (
        ("ai.interactive", TaskAdmission.unmetered()),
        ("ai.batch", TaskAdmission.unmetered()),
        ("ai.interactive", TaskAdmission("custom")),
        ("ai.batch", TaskAdmission("custom")),
    ),
)
async def test_ai_queues_reject_non_ai_admission_without_writes(
    sql_session_factory,
    queue,
    admission,
):
    """Removing the queue-strategy binding would let AI work bypass metering."""
    await _seed_users(sql_session_factory)
    service = TaskService(sql_session_factory)

    with pytest.raises(TaskServiceError) as rejected:
        await service.create_task(
            "usr_claim_a",
            task_type="resume_optimize",
            queue=queue,
            trace_id=f"tr_{queue}_{admission.usage_type}",
            idempotency_key=f"{queue}-{admission.usage_type}",
            admission=admission,
        )

    assert rejected.value.code == "TASK_ADMISSION_INVALID"
    assert rejected.value.status_code == 422
    async with sql_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(UsageLedger))
            == 0
        )


@pytest.mark.parametrize(
    ("queue", "resource_type", "resource_id", "payload_session_id"),
    (
        ("ai.batch", "intake_session", "intake_1", "intake_1"),
        ("ai.interactive", "resume", "intake_1", "intake_1"),
        ("ai.interactive", "intake_session", "intake_1", "intake_other"),
    ),
)
async def test_unmetered_intake_fallback_cannot_forge_queue_or_resource(
    sql_session_factory,
    queue,
    resource_type,
    resource_id,
    payload_session_id,
):
    await _seed_users(sql_session_factory)
    service = TaskService(sql_session_factory)
    payload = {
        "intake_session_id": payload_session_id,
        "generation_mode": "rule_fallback",
        "draft_input_hash": "a" * 64,
        "draft_snapshot": {"workflow_type": "compose_resume_draft"},
    }

    with pytest.raises(TaskServiceError) as rejected:
        await service.create_task(
            "usr_claim_a",
            task_type="generate_intake_draft",
            queue=queue,
            trace_id=f"tr_{queue}_{resource_type}",
            idempotency_key=f"fallback-{queue}-{resource_type}-{payload_session_id}",
            admission=TaskAdmission.unmetered(),
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
        )

    assert rejected.value.code == "TASK_ADMISSION_INVALID"
    async with sql_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
        assert await session.scalar(select(func.count()).select_from(UsageLedger)) == 0


async def test_concurrent_admission_consumes_one_slot_and_replay_does_not_recount(
    sql_session_factory,
):
    """Two requests reading one remaining slot must not both create durable tasks."""
    await _seed_users(sql_session_factory)
    clock = MutableClock()
    service = TaskService(sql_session_factory, clock=clock)
    admission = TaskAdmission.ai(cost_cny=Decimal("1.25"))
    first = await service.create_task(
        "usr_claim_a",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_first",
        idempotency_key="usage-first",
        admission=admission,
    )
    first_claim = await service.claim_task("usr_claim_a", first.id)
    assert first_claim is not None

    results = await asyncio.gather(
        service.create_task(
            "usr_claim_a",
            task_type="resume_optimize",
            queue="ai.batch",
            trace_id="tr_second",
            idempotency_key="usage-second",
            admission=admission,
        ),
        service.create_task(
            "usr_claim_a",
            task_type="resume_optimize",
            queue="ai.batch",
            trace_id="tr_third",
            idempotency_key="usage-third",
            admission=admission,
        ),
        return_exceptions=True,
    )

    accepted = [result for result in results if isinstance(result, Task)]
    denied = [result for result in results if isinstance(result, TaskServiceError)]
    assert len(accepted) == 1
    assert len(denied) == 1
    assert denied[0].code == "AI_CONCURRENCY_LIMIT_REACHED"

    replay = await service.create_task(
        "usr_claim_a",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_replay",
        idempotency_key="usage-second"
        if accepted[0].trace_id == "tr_second"
        else "usage-third",
        admission=admission,
    )
    assert replay.id == accepted[0].id
    async with sql_session_factory() as session:
        assert (
            await session.scalar(select(func.sum(UsageLedger.quantity)))
            == 2
        )


async def test_live_lease_blocks_duplicate_claim_and_expired_lease_fences_old_worker(
    sql_session_factory,
):
    """A second worker must wait for lease expiry and an old token must never write."""
    await _seed_users(sql_session_factory)
    clock = MutableClock()
    service = TaskService(sql_session_factory, clock=clock)
    task = await service.create_task(
        "usr_claim_a",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_lease",
        idempotency_key="lease",
        admission=TaskAdmission.ai(),
    )

    first = await service.claim_task("usr_claim_a", task.id, lease_seconds=30)
    blocked = await service.claim_task("usr_claim_a", task.id, lease_seconds=30)
    assert first is not None
    assert blocked is None

    clock.value += timedelta(seconds=31)
    replacement = await service.claim_task(
        "usr_claim_a", task.id, lease_seconds=30
    )
    assert replacement is not None
    assert replacement.token != first.token
    with pytest.raises(TaskClaimError) as stale:
        await service.report_progress(
            "usr_claim_a",
            task.id,
            first.token,
            "stale",
            50,
        )
    assert stale.value.code == "TASK_CLAIM_STALE"

    completed = await service.complete_task(
        "usr_claim_a",
        task.id,
        replacement.token,
        "resume_version:rv_fenced",
    )
    assert completed.status == "succeeded"
    assert completed.result_ref == "resume_version:rv_fenced"


async def test_worker_writes_require_owner_and_current_claim_token(sql_session_factory):
    """Knowing a task id alone must not authorize claim or result writes."""
    await _seed_users(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_claim_a",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_owner",
        idempotency_key="owner",
        admission=TaskAdmission.ai(),
    )

    assert await service.claim_task("usr_claim_b", task.id) is None
    claim = await service.claim_task("usr_claim_a", task.id)
    assert claim is not None
    with pytest.raises(TaskClaimError):
        await service.complete_task(
            "usr_claim_b",
            task.id,
            claim.token,
            "resume_version:stolen",
        )
    with pytest.raises(TaskClaimError):
        await service.fail_task(
            "usr_claim_a",
            task.id,
            "clm_wrong",
            "FORGED_FAILURE",
        )

    stored = await service.get_task("usr_claim_a", task.id)
    assert stored is not None
    assert stored.status == "running"
    assert stored.result_ref is None
