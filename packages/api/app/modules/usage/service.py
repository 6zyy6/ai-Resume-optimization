from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from app.core.ids import new_id


AI_TASKS_PER_DAY = 20
AI_TASKS_CONCURRENT = 2
GLOBAL_COST_ALERT_CNY = Decimal("70.00")
GLOBAL_COST_DEGRADED_CNY = Decimal("90.00")
GLOBAL_COST_LIMIT_CNY = Decimal("100.00")


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class UsageRecord:
    id: str
    owner_user_id: str
    usage_type: str
    quantity: int
    cost_cny: Decimal
    trace_id: str
    created_at: datetime


class UsageRepository(Protocol):
    async def append_ai_task(
        self,
        owner_user_id: str,
        trace_id: str,
        created_at: datetime,
        cost_cny: Decimal = Decimal("0"),
    ) -> UsageRecord: ...

    async def count_ai_tasks(self, owner_user_id: str, since: datetime) -> int: ...
    async def running_ai_tasks(self, owner_user_id: str) -> int: ...
    async def daily_cost(self, since: datetime) -> Decimal: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class InMemoryUsageRepository:
    def __init__(self) -> None:
        self.rows: list[UsageRecord] = []
        self._running_ai_tasks: dict[str, int] = {}
        self._daily_cost_override: Decimal | None = None

    async def append_ai_task(
        self,
        owner_user_id: str,
        trace_id: str,
        created_at: datetime,
        cost_cny: Decimal = Decimal("0"),
    ) -> UsageRecord:
        row = UsageRecord(
            id=new_id("usg"),
            owner_user_id=owner_user_id,
            usage_type="ai_task",
            quantity=1,
            cost_cny=cost_cny,
            trace_id=trace_id,
            created_at=created_at,
        )
        self.rows.append(row)
        return row

    async def count_ai_tasks(self, owner_user_id: str, since: datetime) -> int:
        return sum(
            row.quantity
            for row in self.rows
            if row.owner_user_id == owner_user_id
            and row.usage_type == "ai_task"
            and row.created_at >= since
        )

    async def running_ai_tasks(self, owner_user_id: str) -> int:
        return self._running_ai_tasks.get(owner_user_id, 0)

    async def daily_cost(self, since: datetime) -> Decimal:
        if self._daily_cost_override is not None:
            return self._daily_cost_override
        return sum(
            (row.cost_cny for row in self.rows if row.created_at >= since),
            start=Decimal("0"),
        )

    def set_running_ai_tasks(self, owner_user_id: str, count: int) -> None:
        self._running_ai_tasks[owner_user_id] = count

    def set_daily_cost(self, cost_cny: Decimal) -> None:
        self._daily_cost_override = cost_cny


@dataclass(frozen=True)
class UsageDecision:
    allowed: bool
    reason: str | None
    retry_after: int | None


@dataclass(frozen=True)
class UsageSummary:
    ai_tasks_used: int
    ai_tasks_limit: int
    ai_tasks_running: int
    ai_concurrent_limit: int
    global_cost_cny: Decimal
    global_cost_limit_cny: Decimal
    cost_state: str


class UsageService:
    def __init__(self, repository: UsageRepository, clock: Clock) -> None:
        self.repository = repository
        self.clock = clock

    def _day_start(self) -> datetime:
        now = self.clock.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _retry_after_day(self) -> int:
        return int((self._day_start() + timedelta(days=1) - self.clock.now()).total_seconds())

    async def decide_ai_task(
        self,
        owner_user_id: str,
        *,
        is_retry: bool = False,
    ) -> UsageDecision:
        day_start = self._day_start()
        cost = await self.repository.daily_cost(day_start)
        if cost >= GLOBAL_COST_LIMIT_CNY:
            return UsageDecision(False, "AI_LIMIT_REACHED", self._retry_after_day())
        if await self.repository.count_ai_tasks(owner_user_id, day_start) >= AI_TASKS_PER_DAY:
            return UsageDecision(False, "AI_LIMIT_REACHED", self._retry_after_day())
        if await self.repository.running_ai_tasks(owner_user_id) >= AI_TASKS_CONCURRENT:
            return UsageDecision(False, "AI_CONCURRENCY_LIMIT_REACHED", None)
        if cost >= GLOBAL_COST_DEGRADED_CNY:
            if is_retry:
                return UsageDecision(False, "AI_RETRY_DISABLED", None)
            return UsageDecision(True, "AI_COST_DEGRADED", None)
        if cost >= GLOBAL_COST_ALERT_CNY:
            return UsageDecision(True, "AI_COST_ALERT", None)
        return UsageDecision(True, None, None)

    async def record_ai_task(
        self,
        owner_user_id: str,
        trace_id: str,
        *,
        cost_cny: Decimal = Decimal("0"),
    ) -> UsageRecord:
        return await self.repository.append_ai_task(
            owner_user_id,
            trace_id,
            self.clock.now(),
            cost_cny,
        )

    async def summary(self, owner_user_id: str) -> UsageSummary:
        day_start = self._day_start()
        cost = await self.repository.daily_cost(day_start)
        state = "normal"
        if cost >= GLOBAL_COST_LIMIT_CNY:
            state = "stopped"
        elif cost >= GLOBAL_COST_DEGRADED_CNY:
            state = "degraded"
        elif cost >= GLOBAL_COST_ALERT_CNY:
            state = "alert"
        return UsageSummary(
            ai_tasks_used=await self.repository.count_ai_tasks(owner_user_id, day_start),
            ai_tasks_limit=AI_TASKS_PER_DAY,
            ai_tasks_running=await self.repository.running_ai_tasks(owner_user_id),
            ai_concurrent_limit=AI_TASKS_CONCURRENT,
            global_cost_cny=cost,
            global_cost_limit_cny=GLOBAL_COST_LIMIT_CNY,
            cost_state=state,
        )


def build_default_usage_service() -> UsageService:
    return UsageService(InMemoryUsageRepository(), SystemClock())
