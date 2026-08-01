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
    delete,
    event,
    func,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.contracts import FactStatus
from app.db import repositories as repository_module
from app.db.models import (
    AiTraceEvent,
    Base,
    Fact,
    FactSource,
    JobDescription,
    Resume,
    ResumeVersion,
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


def migrated_sqlite_engine(database_path, monkeypatch):
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    command.upgrade(config, "head")
    return create_engine(f"sqlite:///{database_path}")


def seed_resume_version(session):
    session.add(User(id="user_a"))
    session.flush()
    session.add(Resume(id="resume_a", owner_user_id="user_a", kind="base", title="A"))
    session.flush()
    session.add(
        ResumeVersion(
            id="version_a",
            owner_user_id="user_a",
            resume_id="resume_a",
            snapshot_json={"title": "A"},
            snapshot_hash="hash_a",
            created_by="user_a",
        )
    )


def test_metadata_contains_complete_owner_scoped_foundation():
    required_tables = {
        "users",
        "user_aliases",
        "user_identities",
        "user_consents",
        "sessions",
        "source_records",
        "experiences",
        "facts",
        "fact_sources",
        "fact_revisions",
        "intake_sessions",
        "intake_answers",
            "resumes",
            "targeted_resume_keys",
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
            "resume_imports",
            "tasks",
        "task_events",
        "outbox",
        "ai_runs",
        "ai_trace_events",
        "exports",
        "idempotency_records",
        "usage_ledger",
        "audit_logs",
    }
    assert required_tables == set(Base.metadata.tables)

    owner_scoped_tables = required_tables - {"users", "user_aliases"}
    for table_name in owner_scoped_tables:
        owner_column = Base.metadata.tables[table_name].c.owner_user_id
        assert owner_column.nullable is False, table_name


def test_metadata_registers_migration_indexes():
    expected_indexes = {
        "tasks": {
            "ix_tasks_active_usage",
            "ix_tasks_active_ai_run_id",
        },
        "outbox": {"ix_outbox_dispatch_ready"},
        "usage_ledger": {"ix_usage_ledger_owner_state_created"},
    }

    for table_name, expected_names in expected_indexes.items():
        actual_names = {
            index.name for index in Base.metadata.tables[table_name].indexes
        }
        assert expected_names <= actual_names


def test_ai_receipt_and_usage_reservation_metadata_is_constrained():
    ai_runs = Base.metadata.tables["ai_runs"]
    usage = Base.metadata.tables["usage_ledger"]

    assert ai_runs.c.status.nullable is False
    assert ai_runs.c.error_code.nullable is True
    assert ai_runs.c.workflow_stage.nullable is False
    assert ai_runs.c.provider_cost.type.precision == 38
    assert ai_runs.c.provider_cost.type.scale == 18
    assert ai_runs.c.prompt_template_version.type.length == 128
    assert usage.c.state.nullable is False
    assert usage.c.task_id.nullable is True
    assert usage.c.ai_run_id.nullable is True
    assert usage.c.updated_at.nullable is False
    state_checks = {
        str(constraint.sqltext)
        for constraint in usage.constraints
        if getattr(constraint, "name", None) == "ck_usage_ledger_state"
    }
    assert state_checks == {"state IN ('reserved', 'consumed', 'released')"}


def test_postgresql_ai_run_ddl_preserves_cost_precision_and_template_width():
    ddl = str(
        CreateTable(Base.metadata.tables["ai_runs"]).compile(
            dialect=postgresql.dialect()
        )
    )

    assert "provider_cost NUMERIC(38, 18)" in ddl
    assert "prompt_template_version VARCHAR(128)" in ddl


def test_migration_0012_marks_legacy_usage_as_consumed(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy-usage.db"
    monkeypatch.setenv(
        "ALEMBIC_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path}",
    )
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    command.upgrade(config, "0011")
    engine = create_engine(f"sqlite:///{database_path}")
    created_at = "2026-07-30 08:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, status, locale, created_at) "
                "VALUES ('usr_legacy', 'active', 'zh-CN', :created_at)"
            ),
            {"created_at": created_at},
        )
        connection.execute(
            text(
                "INSERT INTO usage_ledger "
                "(id, owner_user_id, usage_type, quantity, cost_cny, trace_id, created_at) "
                "VALUES ('usg_legacy', 'usr_legacy', 'ai_task', 1, 0.5, "
                "'tr_legacy', :created_at)"
            ),
            {"created_at": created_at},
        )
    engine.dispose()

    command.upgrade(config, "0012")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, task_id, ai_run_id, updated_at "
                "FROM usage_ledger WHERE id = 'usg_legacy'"
            )
        ).one()
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text("DELETE FROM usage_ledger WHERE id = 'usg_legacy'")
            )
    engine.dispose()

    assert row.state == "consumed"
    assert row.task_id is None
    assert row.ai_run_id is None
    assert str(row.updated_at).startswith("2026-07-30 08:00:00")

    command.downgrade(config, "0011")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(usage_ledger)"))}
        preserved = connection.execute(
            text("SELECT quantity, cost_cny FROM usage_ledger WHERE id = 'usg_legacy'")
        ).one()
    engine.dispose()

    assert "state" not in columns
    assert preserved.quantity == 1
    assert Decimal(str(preserved.cost_cny)) == Decimal("0.5")


def _seed_legacy_ai_run(connection, *, suffix: str, workflow_type: str, input_hash: str):
    created_at = "2026-07-30 08:00:00"
    task_id = f"tsk_{suffix}"
    connection.execute(
        text(
            "INSERT INTO tasks "
            "(id, owner_user_id, type, status, priority, trace_id, attempts, "
            "max_attempts, queued_at, stage, progress, cancellation_requested) "
            "VALUES (:task_id, 'usr_migration', 'ai', 'queued', 0, :trace_id, "
            "0, 3, :created_at, 'queued', 0, 0)"
        ),
        {"task_id": task_id, "trace_id": f"tr_{suffix}", "created_at": created_at},
    )
    connection.execute(
        text(
            "INSERT INTO ai_runs "
            "(id, owner_user_id, trace_id, task_id, workflow_type, workflow_version, "
            "provider, requested_model, input_tokens, output_tokens, cache_tokens, "
            "reasoning_tokens, provider_cost, cost_cny, turn_count, tool_count, "
            "retry_count, fallback_count, prompt_template_version, input_hash) "
            "VALUES (:run_id, 'usr_migration', :trace_id, :task_id, :workflow_type, "
            "'2', 'deepseek', 'deepseek-chat', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "
            "'template@2', :input_hash)"
        ),
        {
            "run_id": f"run_{suffix}",
            "trace_id": f"tr_{suffix}",
            "task_id": task_id,
            "workflow_type": workflow_type,
            "input_hash": input_hash,
        },
    )


def test_migration_0012_maps_all_workflows_to_fixed_business_stages(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "workflow-stage.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    command.upgrade(config, "0011")
    engine = create_engine(f"sqlite:///{database_path}")
    workflows = {
        "analyze_intake_answer": "analysis",
        "compose_resume_draft": "draft",
        "parse_jd": "parse",
        "match_resume_to_jd": "match",
        "generate_suggestions_batch": "suggestions",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, status, locale, created_at) "
                "VALUES ('usr_migration', 'active', 'zh-CN', '2026-07-30 08:00:00')"
            )
        )
        for index, workflow_type in enumerate(workflows):
            _seed_legacy_ai_run(
                connection,
                suffix=str(index),
                workflow_type=workflow_type,
                input_hash=f"hash_{index}",
            )
    engine.dispose()

    command.upgrade(config, "0012")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                text("SELECT workflow_type, workflow_stage FROM ai_runs")
            ).all()
        )
    engine.dispose()

    assert rows == workflows


def test_migration_0012_rejects_duplicate_mapped_stable_keys(tmp_path, monkeypatch):
    database_path = tmp_path / "duplicate-stage.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    command.upgrade(config, "0011")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, status, locale, created_at) "
                "VALUES ('usr_migration', 'active', 'zh-CN', '2026-07-30 08:00:00')"
            )
        )
        _seed_legacy_ai_run(
            connection,
            suffix="duplicate",
            workflow_type="parse_jd",
            input_hash="same_hash",
        )
        connection.execute(
            text(
                "INSERT INTO ai_runs "
                "(id, owner_user_id, trace_id, task_id, workflow_type, "
                "workflow_version, provider, requested_model, response_model, "
                "started_at, first_token_at, finished_at, stop_reason, input_tokens, "
                "output_tokens, cache_tokens, reasoning_tokens, provider_cost, "
                "cost_cny, turn_count, tool_count, schema_valid, facts_valid, "
                "retry_count, fallback_count, result_ref, prompt_template_version, "
                "input_hash) SELECT 'run_duplicate_2', owner_user_id, "
                "trace_id, task_id, workflow_type, workflow_version, provider, "
                "requested_model, response_model, started_at, first_token_at, "
                "finished_at, stop_reason, input_tokens, output_tokens, cache_tokens, "
                "reasoning_tokens, provider_cost, cost_cny, turn_count, tool_count, "
                "schema_valid, facts_valid, retry_count, fallback_count, result_ref, "
                "prompt_template_version, input_hash FROM ai_runs "
                "WHERE id = 'run_duplicate'"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="duplicate"):
        command.upgrade(config, "0012")


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("provider_cost", "0.1234567", "provider cost"),
        ("prompt_template_version", "x" * 65, "prompt template"),
    ],
)
def test_migration_0012_rejects_lossy_ai_run_downgrade(
    tmp_path,
    monkeypatch,
    column,
    value,
    message,
):
    database_path = tmp_path / f"lossy-{column}.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    command.upgrade(config, "0011")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, status, locale, created_at) "
                "VALUES ('usr_migration', 'active', 'zh-CN', "
                "'2026-07-30 08:00:00')"
            )
        )
        _seed_legacy_ai_run(
            connection,
            suffix="lossy",
            workflow_type="parse_jd",
            input_hash="lossy_hash",
        )
    engine.dispose()
    command.upgrade(config, "0012")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE ai_runs SET {column} = :value WHERE id = 'run_lossy'"),
            {"value": value},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match=message):
        command.downgrade(config, "0011")


@pytest.mark.parametrize("state", ["reserved", "released"])
def test_migration_0012_downgrade_rejects_non_consumed_usage(
    tmp_path, monkeypatch, state
):
    database_path = tmp_path / f"downgrade-{state}.db"
    engine = migrated_sqlite_engine(database_path, monkeypatch)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, status, locale, created_at) "
                "VALUES ('usr_downgrade', 'active', 'zh-CN', '2026-07-30 08:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO usage_ledger "
                "(id, owner_user_id, usage_type, quantity, cost_cny, trace_id, "
                "state, updated_at, created_at) VALUES "
                "('usg_downgrade', 'usr_downgrade', 'ai_task', 1, 0, 'tr', "
                ":state, '2026-07-30 08:00:00', '2026-07-30 08:00:00')"
            ),
            {"state": state},
        )
    engine.dispose()
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))

    with pytest.raises(RuntimeError, match="non-consumed"):
        command.downgrade(config, "0011")


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


def test_sql_resume_version_repository_is_owner_scoped_create_read_only():
    async def run():
        repository_type = getattr(
            repository_module,
            "SqlAlchemyResumeVersionRepository",
            None,
        )
        assert repository_type is not None

        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(User(id="user_a"))
            await session.flush()
            session.add(Resume(id="resume_a", owner_user_id="user_a", kind="base", title="A"))
            await session.flush()
            repository = repository_type(session)

            assert not hasattr(repository, "update")
            assert not hasattr(repository, "delete")
            created = await repository.create(
                {
                    "id": "version_a",
                    "owner_user_id": "user_a",
                    "resume_id": "resume_a",
                    "snapshot_json": {"title": "A"},
                    "snapshot_hash": "hash_a",
                    "created_by": "user_a",
                }
            )
            await session.commit()
            assert created.id == "version_a"
            assert await repository.get("version_a", "user_b") is None
            assert (await repository.get("version_a", "user_a")).snapshot_hash == "hash_a"
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


def test_resume_version_cannot_reference_another_owners_parent():
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
            session.add_all(
                [
                    Resume(id="resume_a", owner_user_id="user_a", kind="base", title="A"),
                    Resume(id="resume_b", owner_user_id="user_b", kind="base", title="B"),
                ]
            )
            await session.flush()
            session.add(
                ResumeVersion(
                    id="version_b",
                    owner_user_id="user_b",
                    resume_id="resume_b",
                    snapshot_json={"title": "B"},
                    snapshot_hash="hash_b",
                    created_by="user_b",
                )
            )
            await session.flush()
            session.add(
                ResumeVersion(
                    id="version_a",
                    owner_user_id="user_a",
                    resume_id="resume_a",
                    parent_version_id="version_b",
                    snapshot_json={"title": "A"},
                    snapshot_hash="hash_a",
                    created_by="user_a",
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
            cwd=Path(__file__).resolve().parents[1],
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


def test_confirmed_fact_last_source_link_cannot_be_moved_by_sql_update():
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
            session.add(
                Fact(
                    id="fact_target",
                    owner_user_id="user_a",
                    kind="achievement",
                    value_encrypted="encrypted target",
                    status=FactStatus.UNCONFIRMED,
                )
            )
            await session.flush()
            repository = SqlAlchemyFactRepository(session)
            await repository.record(
                {
                    "id": "fact_confirmed",
                    "owner_user_id": "user_a",
                    "kind": "achievement",
                    "value_encrypted": "encrypted confirmed",
                    "status": FactStatus.CONFIRMED,
                    "confirmed_at": datetime.now(timezone.utc),
                    "source_ids": ["source_1"],
                }
            )
            await session.commit()

            with pytest.raises(IntegrityError):
                await session.execute(
                    update(FactSource)
                    .where(FactSource.fact_id == "fact_confirmed")
                    .values(fact_id="fact_target")
                )
        await engine.dispose()

    asyncio.run(run())


def test_resume_version_rejects_direct_orm_update():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(User(id="user_a"))
            await session.flush()
            session.add(Resume(id="resume_a", owner_user_id="user_a", kind="base", title="A"))
            await session.flush()
            version = ResumeVersion(
                id="version_a",
                owner_user_id="user_a",
                resume_id="resume_a",
                snapshot_json={"title": "A"},
                snapshot_hash="hash_a",
                created_by="user_a",
            )
            session.add(version)
            await session.commit()

            version.snapshot_json = {"title": "mutated"}
            with pytest.raises(IntegrityError):
                await session.commit()
        await engine.dispose()

    asyncio.run(run())


def test_resume_version_rejects_direct_sql_delete():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(User(id="user_a"))
            await session.flush()
            session.add(Resume(id="resume_a", owner_user_id="user_a", kind="base", title="A"))
            await session.flush()
            session.add(
                ResumeVersion(
                    id="version_a",
                    owner_user_id="user_a",
                    resume_id="resume_a",
                    snapshot_json={"title": "A"},
                    snapshot_hash="hash_a",
                    created_by="user_a",
                )
            )
            await session.commit()

            with pytest.raises(IntegrityError):
                await session.execute(
                    delete(ResumeVersion).where(ResumeVersion.id == "version_a")
                )
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


def test_migration_0001_rejects_resume_version_update(tmp_path, monkeypatch):
    engine = migrated_sqlite_engine(tmp_path / "version-update.db", monkeypatch)
    try:
        with SyncSession(engine) as session:
            seed_resume_version(session)
            session.commit()
            with pytest.raises(IntegrityError):
                session.execute(
                    update(ResumeVersion)
                    .where(ResumeVersion.id == "version_a")
                    .values(snapshot_hash="mutated")
                )
    finally:
        engine.dispose()


def test_migration_0001_rejects_resume_version_delete(tmp_path, monkeypatch):
    engine = migrated_sqlite_engine(tmp_path / "version-delete.db", monkeypatch)
    try:
        with SyncSession(engine) as session:
            seed_resume_version(session)
            session.commit()
            with pytest.raises(IntegrityError):
                session.execute(
                    delete(ResumeVersion).where(ResumeVersion.id == "version_a")
                )
    finally:
        engine.dispose()


def test_migration_0001_rejects_cross_owner_parent_version(tmp_path, monkeypatch):
    engine = migrated_sqlite_engine(tmp_path / "version-parent.db", monkeypatch)

    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    event.listen(engine, "connect", enable_foreign_keys)
    try:
        with SyncSession(engine) as session:
            session.add_all([User(id="user_a"), User(id="user_b")])
            session.flush()
            session.add_all(
                [
                    Resume(id="resume_a", owner_user_id="user_a", kind="base", title="A"),
                    Resume(id="resume_b", owner_user_id="user_b", kind="base", title="B"),
                ]
            )
            session.flush()
            session.add(
                ResumeVersion(
                    id="version_b",
                    owner_user_id="user_b",
                    resume_id="resume_b",
                    snapshot_json={"title": "B"},
                    snapshot_hash="hash_b",
                    created_by="user_b",
                )
            )
            session.flush()
            session.add(
                ResumeVersion(
                    id="version_a",
                    owner_user_id="user_a",
                    resume_id="resume_a",
                    parent_version_id="version_b",
                    snapshot_json={"title": "A"},
                    snapshot_hash="hash_a",
                    created_by="user_a",
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()


def test_migration_0001_rejects_moving_confirmed_facts_last_source(tmp_path, monkeypatch):
    engine = migrated_sqlite_engine(tmp_path / "fact-source-update.db", monkeypatch)
    try:
        with SyncSession(engine) as session:
            session.add(User(id="user_a"))
            session.flush()
            session.add(
                SourceRecord(
                    id="source_1",
                    owner_user_id="user_a",
                    source_type="user_confirmation",
                    content_encrypted="encrypted",
                )
            )
            facts = [
                Fact(
                    id=identifier,
                    owner_user_id="user_a",
                    kind="achievement",
                    value_encrypted="encrypted",
                    status=FactStatus.UNCONFIRMED,
                )
                for identifier in ("fact_confirmed", "fact_target")
            ]
            session.add_all(facts)
            session.flush()
            session.add(
                FactSource(
                    fact_id="fact_confirmed",
                    source_record_id="source_1",
                    source_hash="source_hash",
                    owner_user_id="user_a",
                )
            )
            session.flush()
            facts[0].status = FactStatus.CONFIRMED
            facts[0].confirmed_at = datetime.now(timezone.utc)
            session.commit()
            with pytest.raises(IntegrityError):
                session.execute(
                    update(FactSource)
                    .where(FactSource.fact_id == "fact_confirmed")
                    .values(fact_id="fact_target")
                )
    finally:
        engine.dispose()
