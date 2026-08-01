from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, TypeVar


RecordT = TypeVar("RecordT")


class ImmutableUsageError(ValueError):
    pass


def is_valid_cost_cny(cost_cny: object) -> bool:
    return (
        isinstance(cost_cny, Decimal)
        and cost_cny.is_finite()
        and cost_cny >= 0
    )


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


@dataclass(frozen=True)
class ResumeVersionEntry:
    id: str
    owner_user_id: str
    resume_id: str
    parent_version_id: str | None
    snapshot_json: dict[str, Any]
    snapshot_hash: str
    created_by: str
    created_at: datetime


class ResumeVersionRepository(Protocol):
    async def create(self, values: dict[str, Any]) -> ResumeVersionEntry: ...
    async def get(
        self,
        identifier: str,
        owner_user_id: str,
    ) -> ResumeVersionEntry | None: ...


class JobRepository(OwnerScopedRepository[RecordT], Protocol[RecordT]):
    pass


class SuggestionRepository(OwnerScopedRepository[RecordT], Protocol[RecordT]):
    pass


class TaskRepository(OwnerScopedRepository[RecordT], Protocol[RecordT]):
    pass


class OutboxRepository(OwnerScopedRepository[RecordT], Protocol[RecordT]):
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
