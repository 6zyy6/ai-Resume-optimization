import hashlib
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Fact,
    FactSource,
    IdempotencyRecord,
    Job,
    Resume,
    ResumeVersion,
    SourceRecord,
    Suggestion,
    Task,
    UsageRow,
    User,
)
from app.db.ownership import authorized_owner_ids
from app.db.ports import (
    FactRepository,
    IdempotencyRepository,
    JobRepository,
    ResumeRepository,
    ResumeVersionEntry,
    ResumeVersionRepository,
    SuggestionRepository,
    TaskRepository,
    UsageEntry,
    UsageRepository,
    UserRepository,
)

__all__ = [
    "FactRepository",
    "IdempotencyRepository",
    "JobRepository",
    "ResumeRepository",
    "ResumeVersionRepository",
    "SqlAlchemyFactRepository",
    "SqlAlchemyIdempotencyRepository",
    "SqlAlchemyJobRepository",
    "SqlAlchemyResumeRepository",
    "SqlAlchemyResumeVersionRepository",
    "SqlAlchemySuggestionRepository",
    "SqlAlchemyTaskRepository",
    "SqlAlchemyUsageRepository",
    "SqlAlchemyUserRepository",
    "SuggestionRepository",
    "TaskRepository",
    "UsageRepository",
    "UserRepository",
]


class SqlAlchemyRepository:
    model: type

    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(self, values: dict[str, Any]) -> Any:
        row = self.model(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, identifier: str, owner_user_id: str) -> Any:
        owner_ids = await authorized_owner_ids(self.session, owner_user_id)
        return await self.session.scalar(
            select(self.model).where(
                self.model.id == identifier,
                self.model.owner_user_id.in_(owner_ids),
            )
        )

    async def update(
        self,
        identifier: str,
        owner_user_id: str,
        values: dict[str, Any],
    ) -> Any:
        row = await self.get(identifier, owner_user_id)
        if row is None:
            return None
        for name, value in values.items():
            if name != "owner_user_id":
                setattr(row, name, value)
        await self.session.flush()
        return row

    async def delete(self, identifier: str, owner_user_id: str) -> bool:
        row = await self.get(identifier, owner_user_id)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True


class SqlAlchemyUserRepository:
    model = User

    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(self, values: dict[str, Any]) -> User:
        row = User(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, identifier: str) -> User | None:
        return await self.session.get(User, identifier)

    async def update(self, identifier: str, values: dict[str, Any]) -> User | None:
        row = await self.get(identifier)
        if row is None:
            return None
        for name, value in values.items():
            setattr(row, name, value)
        await self.session.flush()
        return row


class SqlAlchemyFactRepository(SqlAlchemyRepository):
    model = Fact

    async def record(self, values: dict[str, Any]) -> Fact:
        fact_values = values.copy()
        source_ids = tuple(fact_values.pop("source_ids", ()))
        desired_status = fact_values["status"]
        confirmed_at = fact_values.get("confirmed_at")
        if desired_status == "confirmed":
            fact_values["status"] = "unconfirmed"
            fact_values["confirmed_at"] = None

        fact = await super().record(fact_values)
        if source_ids:
            sources = (
                await self.session.scalars(
                    select(SourceRecord).where(
                        SourceRecord.id.in_(source_ids),
                        SourceRecord.owner_user_id == fact.owner_user_id,
                    )
                )
            ).all()
            if {source.id for source in sources} != set(source_ids):
                raise ValueError("all fact sources must exist and belong to the fact owner")
            self.session.add_all(
                [
                    FactSource(
                        fact_id=fact.id,
                        source_record_id=source.id,
                        owner_user_id=fact.owner_user_id,
                        source_hash=hashlib.sha256(source.content_encrypted.encode()).hexdigest(),
                    )
                    for source in sources
                ]
            )
            await self.session.flush()

        if desired_status == "confirmed":
            fact.status = desired_status
            fact.confirmed_at = confirmed_at
            await self.session.flush()
        return fact


class SqlAlchemyResumeRepository(SqlAlchemyRepository):
    model = Resume


class SqlAlchemyResumeVersionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, values: dict[str, Any]) -> ResumeVersionEntry:
        row = ResumeVersion(**values)
        self.session.add(row)
        await self.session.flush()
        return self._to_entry(row)

    async def get(
        self,
        identifier: str,
        owner_user_id: str,
    ) -> ResumeVersionEntry | None:
        owner_ids = await authorized_owner_ids(self.session, owner_user_id)
        row = await self.session.scalar(
            select(ResumeVersion).where(
                ResumeVersion.id == identifier,
                ResumeVersion.owner_user_id.in_(owner_ids),
            )
        )
        return self._to_entry(row) if row else None

    @staticmethod
    def _to_entry(row: ResumeVersion) -> ResumeVersionEntry:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return ResumeVersionEntry(
            id=row.id,
            owner_user_id=row.owner_user_id,
            resume_id=row.resume_id,
            parent_version_id=row.parent_version_id,
            snapshot_json=row.snapshot_json.copy(),
            snapshot_hash=row.snapshot_hash,
            created_by=row.created_by,
            created_at=created_at,
        )


class SqlAlchemyJobRepository(SqlAlchemyRepository):
    model = Job


class SqlAlchemySuggestionRepository(SqlAlchemyRepository):
    model = Suggestion


class SqlAlchemyTaskRepository(SqlAlchemyRepository):
    model = Task


class SqlAlchemyIdempotencyRepository(SqlAlchemyRepository):
    model = IdempotencyRecord


class SqlAlchemyUsageRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def append(self, values: dict[str, Any]) -> UsageEntry:
        row = UsageRow(**values)
        self.session.add(row)
        await self.session.flush()
        return self._to_entry(row)

    async def list_for_owner(self, owner_user_id: str) -> tuple[UsageEntry, ...]:
        owner_ids = await authorized_owner_ids(self.session, owner_user_id)
        rows = (
            await self.session.scalars(
                select(UsageRow)
                .where(UsageRow.owner_user_id.in_(owner_ids))
                .order_by(UsageRow.created_at, UsageRow.id)
            )
        ).all()
        return tuple(self._to_entry(row) for row in rows)

    @staticmethod
    def _to_entry(row: UsageRow) -> UsageEntry:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return UsageEntry(
            id=row.id,
            owner_user_id=row.owner_user_id,
            usage_type=row.usage_type,
            quantity=row.quantity,
            cost_cny=row.cost_cny,
            trace_id=row.trace_id,
            created_at=created_at,
        )
