from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, TypeVar


RecordT = TypeVar("RecordT")


class ImmutableUsageError(ValueError):
    pass


class OwnerScopedRepository(Protocol[RecordT]):
    async def record(self, values: dict[str, Any]) -> RecordT: ...
    async def get(self, identifier: str, owner_user_id: str) -> RecordT | None: ...
    async def update(
        self,
        identifier: str,
        owner_user_id: str,
        values: dict[str, Any],
    ) -> RecordT | None: ...
    async def delete(self, identifier: str, owner_user_id: str) -> bool: ...


class UserRepository(Protocol[RecordT]):
    async def record(self, values: dict[str, Any]) -> RecordT: ...
    async def get(self, identifier: str) -> RecordT | None: ...
    async def update(self, identifier: str, values: dict[str, Any]) -> RecordT | None: ...


class FactRepository(OwnerScopedRepository[RecordT], Protocol[RecordT]):
    pass


class ResumeRepository(OwnerScopedRepository[RecordT], Protocol[RecordT]):
    pass


class JobRepository(OwnerScopedRepository[RecordT], Protocol[RecordT]):
    pass


class SuggestionRepository(OwnerScopedRepository[RecordT], Protocol[RecordT]):
    pass


class TaskRepository(OwnerScopedRepository[RecordT], Protocol[RecordT]):
    pass


class IdempotencyRepository(OwnerScopedRepository[RecordT], Protocol[RecordT]):
    pass


@dataclass(frozen=True)
class UsageEntry:
    id: str
    owner_user_id: str
    usage_type: str
    quantity: int
    cost_cny: Decimal
    trace_id: str
    created_at: datetime


class UsageRepository(Protocol):
    async def append(self, values: dict[str, Any]) -> UsageEntry: ...
    async def list_for_owner(self, owner_user_id: str) -> tuple[UsageEntry, ...]: ...
