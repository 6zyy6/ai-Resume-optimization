from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Fact, IdempotencyRecord, Job, Resume, Suggestion, Task, UsageRow, User


class Repository(Protocol):
    async def record(self, values: dict[str, Any]) -> Any: ...
    async def get(self, identifier: str) -> Any: ...
    async def update(self, identifier: str, values: dict[str, Any]) -> Any: ...


UserRepository = Repository
FactRepository = Repository
ResumeRepository = Repository
JobRepository = Repository
SuggestionRepository = Repository
TaskRepository = Repository
IdempotencyRepository = Repository
UsageRepository = Repository


class ImmutableUsageError(ValueError):
    pass


class SqlAlchemyRepository:
    model: type

    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(self, values: dict[str, Any]) -> Any:
        row = self.model(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, identifier: str) -> Any:
        return await self.session.get(self.model, identifier)

    async def update(self, identifier: str, values: dict[str, Any]) -> Any:
        row = await self.get(identifier)
        if row is None:
            return None
        for name, value in values.items():
            setattr(row, name, value)
        await self.session.flush()
        return row


class SqlAlchemyUserRepository(SqlAlchemyRepository):
    model = User


class SqlAlchemyFactRepository(SqlAlchemyRepository):
    model = Fact

    async def record(self, values: dict[str, Any]) -> Any:
        values = values.copy()
        values["source_count"] = len(values.get("source_ids", []))
        return await super().record(values)


class SqlAlchemyResumeRepository(SqlAlchemyRepository):
    model = Resume


class SqlAlchemyJobRepository(SqlAlchemyRepository):
    model = Job


class SqlAlchemySuggestionRepository(SqlAlchemyRepository):
    model = Suggestion


class SqlAlchemyTaskRepository(SqlAlchemyRepository):
    model = Task


class SqlAlchemyIdempotencyRepository(SqlAlchemyRepository):
    model = IdempotencyRecord


class SqlAlchemyUsageRepository(SqlAlchemyRepository):
    model = UsageRow

    async def update(self, identifier: str, values: dict[str, Any]) -> Any:
        raise ImmutableUsageError("usage rows are immutable")


from app.db.memory import InMemoryUsageRepository
