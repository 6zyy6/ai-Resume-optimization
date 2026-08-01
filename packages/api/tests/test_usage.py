from dataclasses import dataclass
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.main import app
from app.db.models import Task, UsageLedger, User
from app.db.task3_repositories import SqlUsageRepository
from app.modules.auth.service import AuthenticatedSession
from app.modules.tasks.service import TaskAdmission, TaskService, TaskServiceError
from app.modules.usage.service import (
    InMemoryUsageRepository,
    UsageRecord,
    UsageAdmissionError,
    UsageService,
)


INVALID_COSTS = (
    pytest.param(Decimal("NaN"), id="nan"),
    pytest.param(Decimal("Infinity"), id="positive-infinity"),
    pytest.param(Decimal("-Infinity"), id="negative-infinity"),
    pytest.param(Decimal("-1.00"), id="negative"),
    pytest.param(0, id="non-decimal"),
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


class PostgresAdmissionSession:
    bind = type(
        "PostgresBind",
        (),
        {"dialect": type("PostgresDialect", (), {"name": "postgresql"})()},
    )()

    def __init__(self) -> None:
        self.executed_sql: list[str] = []
        self.scalar_values = iter((0, 0, Decimal("0")))

    async def execute(self, statement):
        self.executed_sql.append(str(statement))

    async def get(self, _model, _key):
        return None

    async def scalars(self, _statement):
        return ()

    async def scalar(self, _statement):
        assert self.executed_sql == ["SELECT pg_advisory_xact_lock(73467231)"]
        return next(self.scalar_values)


@dataclass
class AuthStub:
    cookie_secure: bool = False

    async def authenticate(self, raw_token: str | None) -> AuthenticatedSession | None:
        if raw_token != "valid-session":
            return None
        return AuthenticatedSession(
            user_id="usr_1",
            session_id="ses_1",
            authenticated_at=datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
        )


@pytest.fixture
def usage_harness():
    clock = FakeClock()
    repository = InMemoryUsageRepository()
    service = UsageService(repository, clock)
    previous_usage = getattr(app.state, "usage_service", None)
    previous_auth = app.state.auth_service
    app.state.usage_service = service
    app.state.auth_service = AuthStub()
    yield service, repository
    app.state.usage_service = previous_usage
    app.state.auth_service = previous_auth


@pytest.mark.anyio
async def test_daily_ai_task_limit_denies_the_twenty_first_task(usage_harness):
    service, repository = usage_harness
    for index in range(20):
        await repository.append_ai_task("usr_1", f"tr_{index}", service.clock.now())

    decision = await service.decide_ai_task("usr_1")

    assert decision.allowed is False
    assert decision.reason == "AI_LIMIT_REACHED"
    assert decision.retry_after == 16 * 60 * 60


@pytest.mark.anyio
async def test_reserved_usage_blocks_admission_but_is_hidden_from_display(
    client,
    usage_harness,
):
    service, repository = usage_harness
    now = service.clock.now()
    repository.rows.extend(
        UsageRecord(
            id=f"usg_reserved_{index}",
            owner_user_id="usr_1",
            usage_type="ai_task",
            quantity=1,
            cost_cny=Decimal("0"),
            trace_id=f"tr_reserved_{index}",
            state="reserved",
            task_id=f"tsk_reserved_{index}",
            ai_run_id=None,
            created_at=now,
            updated_at=now,
        )
        for index in range(20)
    )

    decision = await service.decide_ai_task("usr_1")
    summary = await service.summary("usr_1")
    client.cookies.set("session", "valid-session")
    response = client.get("/v1/me/usage")

    assert decision.allowed is False
    assert decision.reason == "AI_LIMIT_REACHED"
    assert summary.ai_tasks_used == 0
    assert summary.global_cost_cny == Decimal("0")
    assert response.status_code == 200
    assert response.json()["ai_tasks_used"] == 0
    assert Decimal(response.json()["global_cost_cny"]) == Decimal("0")


@pytest.mark.anyio
async def test_concurrent_ai_task_limit_denies_a_third_running_task(usage_harness):
    service, repository = usage_harness
    repository.set_running_ai_tasks("usr_1", 2)

    decision = await service.decide_ai_task("usr_1")

    assert decision.allowed is False
    assert decision.reason == "AI_CONCURRENCY_LIMIT_REACHED"
    assert decision.retry_after is None


@pytest.mark.anyio
async def test_ai_cost_thresholds_alert_degrade_retries_and_stop_new_tasks(
    usage_harness,
):
    service, repository = usage_harness

    repository.set_daily_cost(Decimal("70.00"))
    alert = await service.decide_ai_task("usr_1")
    assert alert.allowed is True
    assert alert.reason == "AI_COST_ALERT"

    repository.set_daily_cost(Decimal("90.00"))
    normal = await service.decide_ai_task("usr_1")
    retry = await service.decide_ai_task("usr_1", is_retry=True)
    assert normal.allowed is True
    assert normal.reason == "AI_COST_DEGRADED"
    assert retry.allowed is False
    assert retry.reason == "AI_RETRY_DISABLED"

    repository.set_daily_cost(Decimal("100.00"))
    stopped = await service.decide_ai_task("usr_1")
    assert stopped.allowed is False
    assert stopped.reason == "AI_LIMIT_REACHED"


@pytest.mark.anyio
async def test_recording_ai_usage_appends_an_auditable_row(usage_harness):
    service, repository = usage_harness

    recorded = await service.record_ai_task(
        "usr_1",
        "tr_1",
        cost_cny=Decimal("1.25"),
    )

    assert recorded.owner_user_id == "usr_1"
    assert recorded.usage_type == "ai_task"
    assert recorded.quantity == 1
    assert recorded.cost_cny == Decimal("1.25")
    assert repository.rows == [recorded]


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_cost", INVALID_COSTS)
@pytest.mark.parametrize("entrypoint", ("record", "append"))
async def test_in_memory_usage_writes_reject_invalid_cost_without_rows(
    usage_harness,
    invalid_cost,
    entrypoint,
):
    service, repository = usage_harness

    with pytest.raises(UsageAdmissionError) as caught:
        if entrypoint == "record":
            await service.record_ai_task(
                "usr_1",
                "tr_invalid_record",
                cost_cny=invalid_cost,
            )
        else:
            await repository.append_ai_task(
                "usr_1",
                "tr_invalid_append",
                service.clock.now(),
                invalid_cost,
            )

    assert caught.value.code == "USAGE_COST_INVALID"
    assert caught.value.status_code == 422
    assert repository.rows == []


@pytest.mark.anyio
async def test_atomic_admission_allows_only_one_request_at_nineteen_daily_tasks(
    usage_harness,
):
    service, repository = usage_harness
    for index in range(19):
        await repository.append_ai_task("usr_1", f"tr_seed_{index}", service.clock.now())

    first, second = await asyncio.gather(
        service.admit_ai_task("usr_1", "tr_20", "key-20"),
        service.admit_ai_task("usr_1", "tr_21", "key-21"),
    )

    assert sorted([first.allowed, second.allowed]) == [False, True]
    assert await repository.count_ai_tasks("usr_1", service._day_start()) == 20
    assert len(repository.tasks) == 1
    assert len(repository.idempotency) == 2


@pytest.mark.anyio
async def test_atomic_admission_allows_only_one_request_with_one_running_task(
    usage_harness,
):
    service, repository = usage_harness
    repository.set_running_ai_tasks("usr_1", 1)

    first, second = await asyncio.gather(
        service.admit_ai_task("usr_1", "tr_1", "key-1"),
        service.admit_ai_task("usr_1", "tr_2", "key-2"),
    )

    assert sorted([first.allowed, second.allowed]) == [False, True]
    denied = first if not first.allowed else second
    assert denied.reason == "AI_CONCURRENCY_LIMIT_REACHED"
    assert len(repository.tasks) == 1
    assert len(repository.rows) == 1


@pytest.mark.anyio
async def test_atomic_admission_at_global_stop_creates_no_task_or_ledger_row(
    usage_harness,
):
    service, repository = usage_harness
    repository.set_daily_cost(Decimal("100.00"))

    first, second = await asyncio.gather(
        service.admit_ai_task("usr_1", "tr_1", "key-1"),
        service.admit_ai_task("usr_2", "tr_2", "key-2"),
    )

    assert first.allowed is False
    assert second.allowed is False
    assert first.reason == second.reason == "AI_LIMIT_REACHED"
    assert repository.tasks == {}
    assert repository.rows == []


@pytest.mark.anyio
async def test_in_memory_admission_rejects_projected_cost_above_global_limit(
    usage_harness,
):
    service, repository = usage_harness
    repository.set_daily_cost(Decimal("99.50"))

    decision = await service.admit_ai_task(
        "usr_1",
        "tr_projected",
        "projected-key",
        cost_cny=Decimal("1.00"),
    )

    assert decision.allowed is False
    assert decision.reason == "AI_LIMIT_REACHED"
    assert repository.tasks == {}
    assert repository.rows == []


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_cost", INVALID_COSTS)
async def test_in_memory_admission_rejects_invalid_cost_without_writes(
    usage_harness,
    invalid_cost,
):
    service, repository = usage_harness

    with pytest.raises(UsageAdmissionError) as caught:
        await service.admit_ai_task(
            "usr_1",
            "tr_invalid",
            "invalid-key",
            cost_cny=invalid_cost,
        )

    assert caught.value.code == "USAGE_COST_INVALID"
    assert caught.value.status_code == 422
    assert repository.tasks == {}
    assert repository.rows == []
    assert repository.idempotency == {}


@pytest.mark.anyio
async def test_in_memory_admission_counts_reserved_cost_across_owners(
    usage_harness,
):
    service, repository = usage_harness

    first = await service.admit_ai_task(
        "usr_cost_a",
        "tr_cost_a",
        "cost-a",
        cost_cny=Decimal("60.00"),
    )
    second = await service.admit_ai_task(
        "usr_cost_b",
        "tr_cost_b",
        "cost-b",
        cost_cny=Decimal("60.00"),
    )

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "AI_LIMIT_REACHED"
    assert len(repository.tasks) == 1
    assert [row.cost_cny for row in repository.rows] == [Decimal("60.00")]


@pytest.mark.anyio
async def test_in_memory_admission_adds_reserved_cost_to_override_baseline(
    usage_harness,
):
    service, repository = usage_harness
    repository.set_daily_cost(Decimal("99.00"))

    first = await service.admit_ai_task(
        "usr_override_a",
        "tr_override_a",
        "override-a",
        cost_cny=Decimal("1.00"),
    )
    summary = await service.summary("usr_override_a")
    second = await service.admit_ai_task(
        "usr_override_b",
        "tr_override_b",
        "override-b",
        cost_cny=Decimal("1.00"),
    )

    assert first.allowed is True
    assert summary.global_cost_cny == Decimal("99.00")
    assert second.allowed is False
    assert second.reason == "AI_LIMIT_REACHED"
    assert len(repository.tasks) == 1
    assert [row.cost_cny for row in repository.rows] == [Decimal("1.00")]


@pytest.mark.anyio
async def test_in_memory_decision_counts_reserved_cost_but_summary_hides_it(
    usage_harness,
):
    service, _ = usage_harness
    reserved = await service.admit_ai_task(
        "usr_reserved_cost_a",
        "tr_reserved_cost_a",
        "reserved-cost-a",
        cost_cny=Decimal("100.00"),
    )

    decision = await service.decide_ai_task("usr_reserved_cost_b")
    summary = await service.summary("usr_reserved_cost_b")

    assert reserved.allowed is True
    assert decision.allowed is False
    assert decision.reason == "AI_LIMIT_REACHED"
    assert summary.global_cost_cny == Decimal("0")


@pytest.mark.anyio
async def test_atomic_admission_replays_same_key_with_same_semantic_input(
    usage_harness,
):
    service, repository = usage_harness

    first = await service.admit_ai_task(
        "usr_1",
        "tr_first",
        "same-key",
        workflow_type="quality_check",
        cost_cny=Decimal("1.25"),
    )
    replay = await service.admit_ai_task(
        "usr_1",
        "tr_retry",
        "same-key",
        workflow_type="quality_check",
        cost_cny=Decimal("1.250"),
    )

    assert replay == first
    assert len(repository.tasks) == 1
    assert len(repository.rows) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changed",
    [
        {"workflow_type": "rewrite", "is_retry": False, "cost_cny": Decimal("1.25")},
        {"workflow_type": "quality_check", "is_retry": True, "cost_cny": Decimal("1.25")},
        {"workflow_type": "quality_check", "is_retry": False, "cost_cny": Decimal("2.00")},
    ],
)
async def test_atomic_admission_rejects_same_key_with_different_semantic_input(
    usage_harness,
    changed,
):
    service, repository = usage_harness
    await service.admit_ai_task(
        "usr_1",
        "tr_first",
        "reused-key",
        workflow_type="quality_check",
        cost_cny=Decimal("1.25"),
    )

    with pytest.raises(UsageAdmissionError) as caught:
        await service.admit_ai_task(
            "usr_1",
            "tr_second",
            "reused-key",
            **changed,
        )

    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert caught.value.status_code == 409
    assert len(repository.tasks) == 1
    assert len(repository.rows) == 1


def test_authenticated_user_can_query_usage_without_consuming_ai_quota(
    client,
    usage_harness,
):
    _, repository = usage_harness
    repository.set_daily_cost(Decimal("100.00"))
    client.cookies.set("session", "valid-session")

    response = client.get("/v1/me/usage")

    assert response.status_code == 200
    assert response.json() == {
        "ai_tasks_used": 0,
        "ai_tasks_limit": 20,
        "ai_tasks_running": 0,
        "ai_concurrent_limit": 2,
        "global_cost_cny": "100.00",
        "global_cost_limit_cny": "100.00",
        "cost_state": "stopped",
    }


@pytest.mark.anyio
async def test_sql_usage_summary_hides_reserved_quantity_and_cost(
    sql_session_factory,
):
    clock = FakeClock()
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_sql_usage"))
    repository = SqlUsageRepository(sql_session_factory)
    service = UsageService(repository, clock)
    reserved = await TaskService(sql_session_factory, clock=clock).create_task(
        "usr_sql_usage",
        task_type="parse_jd",
        queue="ai.interactive",
        trace_id="tr_reserved",
        idempotency_key="reserved-key",
        admission=TaskAdmission.ai(Decimal("9.00")),
    )
    await repository.append_ai_task(
        "usr_sql_usage",
        "tr_consumed",
        clock.now(),
        Decimal("1.25"),
    )

    summary = await service.summary("usr_sql_usage")

    assert reserved.status == "queued"
    assert summary.ai_tasks_used == 1
    assert summary.global_cost_cny == Decimal("1.25")


@pytest.mark.anyio
async def test_sql_decision_counts_reserved_cost_but_summary_hides_it(
    sql_session_factory,
):
    clock = FakeClock()
    async with sql_session_factory.begin() as session:
        session.add_all(
            [User(id="usr_sql_reserved_a"), User(id="usr_sql_reserved_b")]
        )
    await TaskService(sql_session_factory, clock=clock).create_task(
        "usr_sql_reserved_a",
        task_type="parse_jd",
        queue="ai.interactive",
        trace_id="tr_sql_reserved_cost",
        idempotency_key="sql-reserved-cost",
        admission=TaskAdmission.ai(Decimal("100.00")),
    )
    service = UsageService(SqlUsageRepository(sql_session_factory), clock)

    decision = await service.decide_ai_task("usr_sql_reserved_b")
    summary = await service.summary("usr_sql_reserved_b")

    assert decision.allowed is False
    assert decision.reason == "AI_LIMIT_REACHED"
    assert summary.global_cost_cny == Decimal("0")


async def _seed_global_cost_boundary(
    sql_session_factory,
    owner_ids,
    *,
    consumed_cost=Decimal("99.00"),
):
    now = FakeClock().now()
    async with sql_session_factory.begin() as session:
        session.add_all(User(id=owner_id) for owner_id in owner_ids)
        await session.flush()
        session.add_all(
            [
                UsageLedger(
                    id="usg_global_consumed_99",
                    owner_user_id=owner_ids[0],
                    usage_type="ai_task",
                    quantity=1,
                    cost_cny=consumed_cost,
                    trace_id="tr_global_consumed_99",
                    state="consumed",
                    task_id=None,
                    created_at=now,
                    updated_at=now,
                ),
                UsageLedger(
                    id="usg_global_released_100",
                    owner_user_id=owner_ids[0],
                    usage_type="ai_task",
                    quantity=1,
                    cost_cny=Decimal("100.00"),
                    trace_id="tr_global_released_100",
                    state="released",
                    task_id=None,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
    return now


@pytest.mark.anyio
async def test_task_admission_rejects_projected_cost_above_global_limit(
    sql_session_factory,
):
    await _seed_global_cost_boundary(
        sql_session_factory,
        ("usr_projected_cost",),
        consumed_cost=Decimal("99.50"),
    )
    service = TaskService(sql_session_factory, clock=FakeClock())

    with pytest.raises(TaskServiceError) as caught:
        await service.create_task(
            "usr_projected_cost",
            task_type="parse_jd",
            queue="ai.interactive",
            trace_id="tr_projected_cost",
            idempotency_key="projected-cost",
            admission=TaskAdmission.ai(Decimal("1.00")),
        )

    async with sql_session_factory() as session:
        task_count = await session.scalar(select(func.count()).select_from(Task))
        ledger_count = await session.scalar(
            select(func.count()).select_from(UsageLedger)
        )

    assert caught.value.code == "AI_LIMIT_REACHED"
    assert task_count == 0
    assert ledger_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_cost", INVALID_COSTS)
async def test_task_admission_rejects_invalid_cost_without_writes(
    sql_session_factory,
    invalid_cost,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_negative_task_cost"))
    service = TaskService(sql_session_factory, clock=FakeClock())

    with pytest.raises(TaskServiceError) as caught:
        await service.create_task(
            "usr_negative_task_cost",
            task_type="parse_jd",
            queue="ai.interactive",
            trace_id="tr_negative_task_cost",
            idempotency_key="negative-task-cost",
            admission=TaskAdmission.ai(invalid_cost),
        )

    assert caught.value.code == "TASK_ADMISSION_INVALID"
    assert caught.value.status_code == 422
    async with sql_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(UsageLedger))
            == 0
        )


@pytest.mark.anyio
async def test_task_admission_counts_reserved_cost_at_global_stop_for_one_owner(
    sql_session_factory,
):
    now = await _seed_global_cost_boundary(sql_session_factory, ("usr_cost",))
    service = TaskService(sql_session_factory, clock=FakeClock())

    first = await service.create_task(
        "usr_cost",
        task_type="parse_jd",
        queue="ai.interactive",
        trace_id="tr_cost_first",
        idempotency_key="cost-first",
        admission=TaskAdmission.ai(Decimal("1.00")),
    )
    with pytest.raises(TaskServiceError) as caught:
        await service.create_task(
            "usr_cost",
            task_type="parse_jd",
            queue="ai.interactive",
            trace_id="tr_cost_second",
            idempotency_key="cost-second",
            admission=TaskAdmission.ai(Decimal("1.00")),
        )
    consumed = await service.consume_ai_reservation(
        "usr_cost",
        first.id,
        "run_cost_first",
    )
    with pytest.raises(TaskServiceError) as after_consumption:
        await service.create_task(
            "usr_cost",
            task_type="parse_jd",
            queue="ai.interactive",
            trace_id="tr_cost_after_consumption",
            idempotency_key="cost-after-consumption",
            admission=TaskAdmission.ai(Decimal("1.00")),
        )

    async with sql_session_factory() as session:
        admitted_cost = await session.scalar(
            select(func.sum(UsageLedger.cost_cny)).where(
                UsageLedger.task_id == first.id,
                UsageLedger.created_at >= now,
            )
        )

    assert first.status == "queued"
    assert caught.value.code == "AI_LIMIT_REACHED"
    assert consumed.state == "consumed"
    assert consumed.cost_cny == Decimal("1.00")
    assert after_consumption.value.code == "AI_LIMIT_REACHED"
    assert Decimal(admitted_cost or 0) == Decimal("1.00")


@pytest.mark.anyio
async def test_task_admission_counts_reserved_cost_across_owners(
    sql_session_factory,
):
    await _seed_global_cost_boundary(
        sql_session_factory,
        ("usr_cost_seed", "usr_cost_first", "usr_cost_second"),
    )
    service = TaskService(sql_session_factory, clock=FakeClock())

    first = await service.create_task(
        "usr_cost_first",
        task_type="parse_jd",
        queue="ai.interactive",
        trace_id="tr_cross_owner_first",
        idempotency_key="cross-owner-first",
        admission=TaskAdmission.ai(Decimal("1.00")),
    )
    with pytest.raises(TaskServiceError) as caught:
        await service.create_task(
            "usr_cost_second",
            task_type="parse_jd",
            queue="ai.interactive",
            trace_id="tr_cross_owner_second",
            idempotency_key="cross-owner-second",
            admission=TaskAdmission.ai(Decimal("1.00")),
        )

    assert first.owner_user_id == "usr_cost_first"
    assert caught.value.code == "AI_LIMIT_REACHED"


@pytest.mark.anyio
async def test_task_admission_serializes_global_cost_across_owners_on_sqlite(
    sql_session_factory,
):
    await _seed_global_cost_boundary(
        sql_session_factory,
        ("usr_concurrent_seed", "usr_concurrent_a", "usr_concurrent_b"),
    )
    service = TaskService(sql_session_factory, clock=FakeClock())

    async def admit(owner_id, key):
        try:
            return await service.create_task(
                owner_id,
                task_type="parse_jd",
                queue="ai.interactive",
                trace_id=f"tr_{key}",
                idempotency_key=key,
                admission=TaskAdmission.ai(Decimal("1.00")),
            )
        except TaskServiceError as error:
            return error

    results = await asyncio.gather(
        admit("usr_concurrent_a", "concurrent-a"),
        admit("usr_concurrent_b", "concurrent-b"),
    )
    async with sql_session_factory() as session:
        task_count = await session.scalar(select(func.count()).select_from(Task))

    assert sum(isinstance(result, Task) for result in results) == 1
    denied = next(result for result in results if isinstance(result, TaskServiceError))
    assert denied.code == "AI_LIMIT_REACHED"
    assert task_count == 1


@pytest.mark.anyio
async def test_task_admission_uses_the_shared_postgres_global_cost_lock(
    sql_session_factory,
):
    session = PostgresAdmissionSession()
    now = FakeClock().now()

    reservation = await TaskService(
        sql_session_factory,
        clock=FakeClock(),
    )._admit_usage(
        session,
        "usr_postgres_lock",
        TaskAdmission.ai(Decimal("1.00")),
        "tr_postgres_lock",
        now,
        "tsk_postgres_lock",
    )

    assert reservation is not None
    assert session.executed_sql == ["SELECT pg_advisory_xact_lock(73467231)"]
