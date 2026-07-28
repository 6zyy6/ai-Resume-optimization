import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.db.models import User
from app.main import create_app
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.tasks.router import get_task_service, task_event_stream
from app.modules.tasks.service import TaskAdmission, TaskService


async def _seed_users(sessions) -> None:
    async with sessions.begin() as session:
        session.add_all([User(id="usr_api_a"), User(id="usr_api_b")])


def _authenticated() -> AuthenticatedSession:
    now = datetime.now(timezone.utc)
    return AuthenticatedSession("usr_api_a", "ses_api", now, now + timedelta(days=1))


class NeverDisconnected:
    async def is_disconnected(self) -> bool:
        return False


class BrokenTaskService:
    async def list_tasks(self, *_args, **_kwargs):
        raise SQLAlchemyError("database unavailable")


@pytest.mark.anyio
async def test_task_api_is_read_only_owner_scoped_and_cursor_paginated(
    sql_session_factory,
):
    """A public create route or missing owner filter would expose an unsafe Task API."""
    await _seed_users(sql_session_factory)
    service = TaskService(sql_session_factory)
    first = await service.create_task(
        "usr_api_a",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_list_1",
        idempotency_key="list-1",
        admission=TaskAdmission.unmetered(),
    )
    second = await service.create_task(
        "usr_api_a",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_list_2",
        idempotency_key="list-2",
        admission=TaskAdmission.unmetered(),
    )
    await service.create_task(
        "usr_api_b",
        task_type="private_task",
        queue="privacy",
        trace_id="tr_private",
        idempotency_key="list-private",
        admission=TaskAdmission.unmetered(),
    )
    application = create_app(
        Settings(app_env="test", database_url="sqlite+aiosqlite://")
    )
    application.state.task_service = service
    application.dependency_overrides[require_session] = _authenticated

    with TestClient(application) as client:
        forbidden_create = client.post(
            "/v1/tasks",
            headers={"Idempotency-Key": "public-create"},
            json={"type": "arbitrary", "queue": "privacy"},
        )
        page_one = client.get("/v1/tasks?limit=1")
        page_two = client.get(
            "/v1/tasks",
            params={"limit": 1, "cursor": page_one.json()["next_cursor"]},
        )

    assert forbidden_create.status_code == 405
    assert forbidden_create.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert [item["id"] for item in page_one.json()["items"]] == [first.id]
    assert [item["id"] for item in page_two.json()["items"]] == [second.id]
    assert page_two.json()["next_cursor"] is None
    assert "claim_token" not in page_one.json()["items"][0]


def test_task_list_infrastructure_failure_uses_stable_error_envelope():
    """Database failures at the public boundary must not escape as raw 500 errors."""
    application = create_app(
        Settings(app_env="test", database_url="sqlite+aiosqlite://")
    )
    application.dependency_overrides[require_session] = _authenticated
    application.dependency_overrides[get_task_service] = lambda: BrokenTaskService()
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/v1/tasks")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "TASK_SERVICE_UNAVAILABLE"
    assert response.json()["error"]["request_id"]


@pytest.mark.anyio
async def test_sse_resumes_from_last_event_id_and_declares_event_stream(
    sql_session_factory,
):
    """Ignoring Last-Event-ID would replay old progress after browser reconnect."""
    await _seed_users(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_api_a",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_sse_resume",
        idempotency_key="sse-resume",
        admission=TaskAdmission.unmetered(),
    )
    claim = await service.claim_task("usr_api_a", task.id)
    assert claim is not None
    await service.report_progress(
        "usr_api_a", task.id, claim.token, "drafting", 50
    )
    await service.complete_task(
        "usr_api_a", task.id, claim.token, "resume_version:rv_sse"
    )
    application = create_app(
        Settings(app_env="test", database_url="sqlite+aiosqlite://")
    )
    application.state.task_service = service
    application.dependency_overrides[require_session] = _authenticated

    with TestClient(application) as client:
        response = client.get(
            f"/v1/tasks/{task.id}/events",
            headers={"Last-Event-ID": "2"},
        )
        schema = client.get("/openapi.json").json()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 1\n" not in response.text
    assert "id: 2\n" not in response.text
    assert "id: 3\n" in response.text
    assert "id: 4\n" in response.text
    assert (
        "text/event-stream"
        in schema["paths"]["/v1/tasks/{task_id}/events"]["get"]["responses"]["200"][
            "content"
        ]
    )


@pytest.mark.anyio
async def test_sse_tails_new_events_emits_heartbeat_and_closes_at_terminal(
    sql_session_factory,
):
    """Snapshotting events once would miss progress committed after connection."""
    await _seed_users(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_api_a",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_sse_tail",
        idempotency_key="sse-tail",
        admission=TaskAdmission.unmetered(),
    )
    claim = await service.claim_task("usr_api_a", task.id)
    assert claim is not None
    stream = task_event_stream(
        NeverDisconnected(),
        service,
        "usr_api_a",
        task.id,
        after_seq=2,
        poll_interval=0,
        heartbeat_interval=0,
    )

    first_chunk = await anext(stream)
    assert first_chunk.startswith(": heartbeat ")
    await service.report_progress(
        "usr_api_a", task.id, claim.token, "drafting", 60
    )
    assert "id: 3\n" in await anext(stream)
    await service.complete_task(
        "usr_api_a", task.id, claim.token, "resume_version:rv_tail"
    )
    assert "id: 4\n" in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
