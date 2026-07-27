import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import Base, Fact, User
from app.main import create_app
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.facts.service import FactService


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def fact_client(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'facts.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    _run(_create_schema(engine))
    sql_session_factory = async_sessionmaker(engine, expire_on_commit=False)
    _run(_seed_users(sql_session_factory))
    application = create_app(Settings(app_env="test", database_url="sqlite+aiosqlite://"))
    application.state.fact_service = FactService(sql_session_factory)
    application.dependency_overrides[require_session] = _authenticated
    with TestClient(application) as client:
        yield client, sql_session_factory
    _run(engine.dispose())


async def _create_schema(engine):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def _authenticated() -> AuthenticatedSession:
    now = datetime.now(timezone.utc)
    return AuthenticatedSession("usr_a", "ses_a", now, now + timedelta(days=1))


async def _seed_users(sessions):
    async with sessions.begin() as session:
        session.add_all([User(id="usr_a"), User(id="usr_b")])


def _headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


def test_owner_cannot_read_or_change_another_users_fact(fact_client):
    """Removing the owner predicate would expose or change usr_b's fact."""
    client, sessions = fact_client
    fact = _run(
        FactService(sessions).create_fact(
            "usr_b", kind="metric", value="B only", sources=[]
        )
    )

    assert client.get(f"/v1/facts/{fact.id}").status_code == 404
    assert client.patch(
        f"/v1/facts/{fact.id}", json={"value": "stolen"}, headers=_headers("f-1")
    ).status_code == 404


def test_confirming_a_fact_without_a_source_is_rejected(fact_client):
    """Removing the confirmation source gate would return 200 here."""
    client, _ = fact_client
    created = client.post(
        "/v1/facts", json={"kind": "metric", "value": "42"}, headers=_headers("f-2")
    )

    response = client.post(
        f"/v1/facts/{created.json()['id']}/confirm", headers=_headers("f-3")
    )

    assert created.status_code == 201
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FACT_SOURCE_REQUIRED"


def test_creating_a_confirmed_fact_with_a_source_keeps_its_source(fact_client):
    """Skipping source persistence would leave source_ids empty."""
    client, sessions = fact_client
    response = client.post(
        "/v1/facts",
        json={
            "kind": "metric",
            "value": "42%",
            "status": "confirmed",
            "sources": [{"source_type": "user_edit", "content": "dashboard"}],
        },
        headers=_headers("f-4"),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "confirmed"
    assert len(response.json()["source_ids"]) == 1
    assert _run(_fact_count(sessions)) == 1


async def _fact_count(sessions) -> int:
    async with sessions() as session:
        return len((await session.scalars(select(Fact))).all())
