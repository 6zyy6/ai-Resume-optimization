from dataclasses import dataclass
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.main import app
from app.modules.auth.service import AuthenticatedSession
from app.modules.usage.service import InMemoryUsageRepository, UsageService


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
