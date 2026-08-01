from dataclasses import dataclass
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.main import app
from app.db.models import User
from app.db.task3_repositories import SqlUsageRepository
from app.modules.auth.service import AuthenticatedSession
from app.modules.tasks.service import TaskAdmission, TaskService
from app.modules.usage.service import (
    InMemoryUsageRepository,
    UsageRecord,
    UsageAdmissionError,
    UsageService,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


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
