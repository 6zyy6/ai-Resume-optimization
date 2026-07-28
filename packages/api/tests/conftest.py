import os
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ["APP_ENV"] = "test"

from app.db.models import Base
from app.core.config import Settings
from app.db.models import User
from app.integrations.storage import MemoryStorage
from app.main import app, create_app
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.exports.service import ExportService
from app.modules.facts.service import FactService
from app.modules.imports.service import ImportService
from app.modules.jobs.service import JobService
from app.modules.matching.service import MatchingService
from app.modules.resumes.service import ResumeService
from app.modules.suggestions.service import SuggestionService
from app.modules.tasks.service import TaskService


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def sql_session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def pipeline_client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pipeline.db'}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enable_pipeline_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async def prepare():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as session:
            session.add_all([User(id="usr_a"), User(id="usr_b")])
        return sessions

    sessions = asyncio.run(prepare())
    storage = MemoryStorage()
    application = create_app(
        Settings(app_env="test", database_url="sqlite+aiosqlite://")
    )
    application.state.fact_service = FactService(sessions)
    application.state.resume_service = ResumeService(sessions)
    application.state.storage = storage
    application.state.import_service = ImportService(sessions, storage)
    application.state.job_service = JobService(sessions)
    application.state.matching_service = MatchingService(sessions)
    application.state.suggestion_service = SuggestionService(sessions)
    application.state.export_service = ExportService(sessions, storage)
    application.state.task_service = TaskService(sessions)

    def authenticated() -> AuthenticatedSession:
        now = datetime.now(timezone.utc)
        return AuthenticatedSession(
            "usr_a", "ses_a", now, now + timedelta(days=1)
        )

    application.dependency_overrides[require_session] = authenticated
    with TestClient(application) as test_client:
        yield test_client, sessions, storage
    asyncio.run(engine.dispose())
