from typing import Any

from app.db.repositories import ImmutableUsageError


class InMemoryRepository:
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}

    async def record(self, values: dict[str, Any]) -> dict[str, Any]:
        row = values.copy()
        self.rows[row["id"]] = row
        return row

    async def get(self, identifier: str) -> dict[str, Any] | None:
        row = self.rows.get(identifier)
        return row.copy() if row else None

    async def update(self, identifier: str, values: dict[str, Any]) -> dict[str, Any] | None:
        row = self.rows.get(identifier)
        if row is None:
            return None
        row.update(values)
        return row.copy()


class InMemoryUserRepository(InMemoryRepository):
    pass


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


class InMemoryUsageRepository(InMemoryRepository):
    async def update(self, identifier: str, values: dict[str, Any]) -> None:
        raise ImmutableUsageError("usage rows are immutable")
