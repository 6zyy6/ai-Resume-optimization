import asyncio
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    create_engine,
    event,
    func,
    inspect,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.contracts import FactStatus
from app.db.models import (
    AiTraceEvent,
    Base,
    Fact,
    FactSource,
    JobDescription,
    Resume,
    SourceRecord,
    UsageLedger,
    User,
)
from app.db.memory import (
    InMemoryFactRepository,
    InMemoryIdempotencyRepository,
    InMemoryJobRepository,
    InMemoryResumeRepository,
    InMemorySuggestionRepository,
    InMemoryTaskRepository,
)
from app.db.ports import UsageEntry
from app.db.repositories import (
    SqlAlchemyFactRepository,
    SqlAlchemyResumeRepository,
    SqlAlchemyUsageRepository,
)


def test_metadata_contains_complete_owner_scoped_foundation():
    required_tables = {
        "users",
        "user_identities",
        "user_consents",
        "sessions",
        "source_records",
        "experiences",
        "facts",
        "fact_sources",
        "fact_revisions",
        "resumes",
        "resume_versions",
        "resume_sections",
        "bullet_fact_links",
        "version_operations",
        "job_descriptions",
        "jd_requirements",
        "match_analyses",
        "match_items",
        "suggestions",
        "suggestion_fact_links",
        "suggestion_decisions",
        "files",
        "tasks",
        "task_events",
        "ai_runs",
        "ai_trace_events",
        "exports",
        "idempotency_records",
        "usage_ledger",
        "audit_logs",
    }
    assert required_tables == set(Base.metadata.tables)

    owner_scoped_tables = required_tables - {"users"}
    for table_name in owner_scoped_tables:
        owner_column = Base.metadata.tables[table_name].c.owner_user_id
        assert owner_column.nullable is False, table_name


def test_memory_resource_repositories_are_owner_scoped():
    async def run():
        repository_types = (
            InMemoryFactRepository,
            InMemoryResumeRepository,
            InMemoryJobRepository,
            InMemorySuggestionRepository,
            InMemoryTaskRepository,
            InMemoryIdempotencyRepository,
        )
        for repository_type in repository_types:
            repository = repository_type()
            await repository.record({"id": "resource_1", "owner_user_id": "user_a", "value": "original"})
            assert await repository.get("resource_1", "user_b") is None
            assert await repository.update("resource_1", "user_b", {"value": "spoofed"}) is None
            assert await repository.delete("resource_1", "user_b") is False
            assert (await repository.get("resource_1", "user_a"))["value"] == "original"
            assert await repository.delete("resource_1", "user_a") is True

    asyncio.run(run())


def test_sql_resource_repository_is_owner_scoped():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add_all([User(id="user_a"), User(id="user_b")])
            repository = SqlAlchemyResumeRepository(session)
            await repository.record(
                {
                    "id": "resume_1",
                    "owner_user_id": "user_a",
                    "kind": "base",
                    "title": "Original",
                }
            )
            await session.commit()

            assert await repository.get("resume_1", "user_b") is None
            assert await repository.update("resume_1", "user_b", {"title": "Spoofed"}) is None
            assert await repository.delete("resume_1", "user_b") is False

            owned = await repository.get("resume_1", "user_a")
            assert isinstance(owned, Resume)
            assert owned.title == "Original"
        await engine.dispose()

    asyncio.run(run())


def test_targeted_resume_cannot_reference_another_owners_base_or_job():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")

        def enable_foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        event.listen(engine.sync_engine, "connect", enable_foreign_keys)
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add_all([User(id="user_a"), User(id="user_b")])
            await session.flush()
            session.add(
                Resume(
                    id="resume_base",
                    owner_user_id="user_b",
                    kind="base",
                    title="Other owner base",
                )
            )
            session.add(
                JobDescription(
                    id="job_1",
                    owner_user_id="user_b",
                    title="Other owner job",
                    raw_encrypted="encrypted",
                    status="confirmed",
                )
            )
            await session.flush()
            session.add(
                Resume(
                    id="resume_targeted",
                    owner_user_id="user_a",
                    kind="job_targeted",
                    title="Cross-owner targeted resume",
                    base_resume_id="resume_base",
                    job_description_id="job_1",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
        await engine.dispose()

    asyncio.run(run())


def test_sql_and_memory_adapters_import_independently():
    for module_name in ("app.db.memory", "app.db.repositories"):
        completed = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_confirmed_fact_without_a_source_is_rejected():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(User(id="user_a"))
            session.add(
                Fact(
                    id="fact_1",
                    owner_user_id="user_a",
                    kind="achievement",
                    value_encrypted="encrypted",
                    status=FactStatus.CONFIRMED,
                    confirmed_at=datetime.now(timezone.utc),
                )
            )
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
                    AiTraceEvent(
                        id="event_1",
                        owner_user_id="user_a",
                        ai_run_id="run_1",
                        event_seq=1,
                        event_type="turn_start",
                    ),
                    AiTraceEvent(
                        id="event_2",
                        owner_user_id="user_a",
                        ai_run_id="run_1",
                        event_seq=1,
                        event_type="turn_end",
                    ),
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
            session.add(User(id="user_a"))
            session.add(
                SourceRecord(
                    id="source_1",
                    owner_user_id="user_a",
                    source_type="user_confirmation",
                    content_encrypted="encrypted",
                )
            )
            await session.flush()
            repository = SqlAlchemyFactRepository(session)
            await repository.record(
                {
                    "id": "fact_2",
                    "owner_user_id": "user_a",
                    "kind": "achievement",
                    "value_encrypted": "encrypted",
                    "status": FactStatus.CONFIRMED,
                    "confirmed_at": datetime.now(timezone.utc),
                    "source_ids": ["source_1"],
                }
            )
            await session.commit()
            link_count = await session.scalar(
                select(func.count()).select_from(FactSource).where(FactSource.fact_id == "fact_2")
            )
            assert link_count == 1
        await engine.dispose()

    asyncio.run(run())


def test_historical_usage_rows_cannot_be_updated_through_sql():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(User(id="user_a"))
            session.add(
                UsageLedger(
                    id="usage_1",
                    owner_user_id="user_a",
                    usage_type="ai_task",
                    quantity=1,
                    trace_id="tr_1",
                )
            )
            await session.commit()
            with pytest.raises(IntegrityError):
                await session.execute(
                    update(UsageLedger)
                    .where(UsageLedger.id == "usage_1")
                    .values(quantity=2)
                )
        await engine.dispose()

    asyncio.run(run())


def test_sql_usage_repository_is_append_read_only_and_returns_frozen_values():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(User(id="user_a"))
            await session.flush()
            repository = SqlAlchemyUsageRepository(session)

            assert not hasattr(repository, "update")
            assert not hasattr(repository, "delete")
            entry = await repository.append(
                {
                    "id": "usage_1",
                    "owner_user_id": "user_a",
                    "usage_type": "ai_task",
                    "quantity": 1,
                    "cost_cny": Decimal("0.25"),
                    "trace_id": "tr_1",
                }
            )
            assert isinstance(entry, UsageEntry)
            with pytest.raises(FrozenInstanceError):
                entry.quantity = 2
            assert await repository.list_for_owner("user_b") == ()
            assert await repository.list_for_owner("user_a") == (entry,)
        await engine.dispose()

    asyncio.run(run())


def test_migration_0001_is_complete_and_downgrade_is_revision_scoped(tmp_path, monkeypatch):
    database_path = tmp_path / "migration.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    required_tables = set(Base.metadata.tables)
    assert required_tables <= set(inspect(engine).get_table_names())

    future_metadata = MetaData()
    future_table = Table(
        "future_after_0001",
        future_metadata,
        Column("id", Integer, primary_key=True),
    )
    future_table.create(engine)
    try:
        command.downgrade(config, "base")
        remaining_tables = set(inspect(engine).get_table_names())
        assert "future_after_0001" in remaining_tables
        assert required_tables.isdisjoint(remaining_tables)
    finally:
        future_table.drop(engine, checkfirst=True)
        engine.dispose()
