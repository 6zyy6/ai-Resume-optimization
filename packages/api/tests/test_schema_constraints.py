import asyncio

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.contracts import FactStatus
from app.db.models import AiTraceEvent, Base, Fact
from app.db.repositories import ImmutableUsageError, InMemoryUsageRepository, SqlAlchemyFactRepository


def test_confirmed_fact_without_a_source_is_rejected():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(Fact(id="fact_1", status=FactStatus.CONFIRMED, source_count=0))
            with pytest.raises(IntegrityError):
                await session.commit()
        await engine.dispose()

    asyncio.run(run())


def test_trace_event_sequence_is_unique():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add_all(
                [
                    AiTraceEvent(id="event_1", ai_run_id="run_1", event_seq=1),
                    AiTraceEvent(id="event_2", ai_run_id="run_1", event_seq=1),
                ]
            )
            with pytest.raises(IntegrityError):
                await session.commit()
        await engine.dispose()

    asyncio.run(run())


def test_confirmed_fact_with_a_source_is_persisted():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            repository = SqlAlchemyFactRepository(session)
            await repository.record(
                {"id": "fact_2", "status": FactStatus.CONFIRMED, "source_ids": ["source_1"]}
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def test_historical_usage_rows_cannot_be_updated_through_repository():
    async def run():
        repository = InMemoryUsageRepository()
        await repository.record({"id": "usage_1", "units": 1})
        with pytest.raises(ImmutableUsageError):
            await repository.update("usage_1", {"units": 2})

    asyncio.run(run())
