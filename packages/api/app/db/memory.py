from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.db.ports import UsageEntry


class InMemoryRepository:
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}

    async def record(self, values: dict[str, Any]) -> dict[str, Any]:
        row = values.copy()
        self.rows[row["id"]] = row
        return row.copy()

    async def get(self, identifier: str, owner_user_id: str) -> dict[str, Any] | None:
        row = self.rows.get(identifier)
        if row is None or row["owner_user_id"] != owner_user_id:
            return None
        return row.copy()

    async def update(
        self,
        identifier: str,
        owner_user_id: str,
        values: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = self.rows.get(identifier)
        if row is None or row["owner_user_id"] != owner_user_id:
            return None
        row.update(values)
        row["owner_user_id"] = owner_user_id
        return row.copy()

    async def delete(self, identifier: str, owner_user_id: str) -> bool:
        row = self.rows.get(identifier)
        if row is None or row["owner_user_id"] != owner_user_id:
            return False
        del self.rows[identifier]
        return True


class InMemoryUserRepository:
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}

    async def record(self, values: dict[str, Any]) -> dict[str, Any]:
        row = values.copy()
        self.rows[row["id"]] = row
        return row.copy()

    async def get(self, identifier: str) -> dict[str, Any] | None:
        row = self.rows.get(identifier)
        return row.copy() if row else None

    async def update(
        self,
        identifier: str,
        values: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = self.rows.get(identifier)
        if row is None:
            return None
        row.update(values)
        return row.copy()


class InMemoryFactRepository(InMemoryRepository):
    pass


class InMemoryResumeRepository(InMemoryRepository):
    pass


class InMemoryJobRepository(InMemoryRepository):
    pass


class InMemorySuggestionRepository(InMemoryRepository):
    pass


class InMemoryTaskRepository(InMemoryRepository):
    pass


class InMemoryIdempotencyRepository(InMemoryRepository):
    pass


class InMemoryUsageRepository:
    def __init__(self):
        self.rows: list[UsageEntry] = []

    async def append(self, values: dict[str, Any]) -> UsageEntry:
        entry = UsageEntry(
            id=values["id"],
            owner_user_id=values["owner_user_id"],
            usage_type=values["usage_type"],
            quantity=values["quantity"],
            cost_cny=Decimal(values.get("cost_cny", 0)),
            trace_id=values["trace_id"],
            created_at=values.get("created_at", datetime.now(timezone.utc)),
        )
        self.rows.append(entry)
        return entry

    async def list_for_owner(self, owner_user_id: str) -> tuple[UsageEntry, ...]:
        return tuple(row for row in self.rows if row.owner_user_id == owner_user_id)
