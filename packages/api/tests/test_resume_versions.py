import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import (
    Base,
    IdempotencyRecord,
    JobDescription,
    Resume,
    ResumeVersion,
    User,
)
from app.main import create_app
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.resumes.service import ResumeError, ResumeService


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def resume_client(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'resumes.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    _run(_create_schema(engine))
    sql_session_factory = async_sessionmaker(engine, expire_on_commit=False)
    _run(_seed_users(sql_session_factory))
    application = create_app(Settings(app_env="test", database_url="sqlite+aiosqlite://"))
    application.state.resume_service = ResumeService(sql_session_factory)
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


def _snapshot(title: str) -> dict:
    return {"schema_version": "1", "title": title, "target": None, "sections": []}


def _resume(client, key="r-1") -> str:
    response = client.post(
        "/v1/resumes", json={"kind": "base", "title": "Base"}, headers=_headers(key)
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_stale_resume_write_returns_visible_conflict(resume_client):
    """Changing the base-version comparison to accept stale writes breaks this."""
    client, _ = resume_client
    resume_id = _resume(client)
    first = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("one")},
        headers=_headers("v-1"),
    )
    second = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("two")},
        headers=_headers("v-2"),
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "RESUME_VERSION_CONFLICT"


def test_normal_version_endpoint_rejects_forged_restore_operation(resume_client):
    """Letting clients send restore bypasses normal snapshot deduplication."""
    client, _ = resume_client
    resume_id = _resume(client, "forged-restore-resume")
    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("one"), "operation": "restore"},
        headers=_headers("forged-restore"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_nested_unknown_snapshot_field_is_rejected(resume_client):
    """A loose snapshot dictionary accepts unknown nested content."""
    client, _ = resume_client
    resume_id = _resume(client, "strict-snapshot-resume")
    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={
            "base_version": 0,
            "snapshot": {"schema_version": "1", "title": "one", "target": None, "sections": [], "unexpected": True},
        },
        headers=_headers("strict-snapshot"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_owner_cannot_read_or_write_another_users_resume(resume_client):
    """Removing resume owner predicates would expose usr_b's resume."""
    client, sessions = resume_client
    other = _run(
        ResumeService(sessions).create_resume(
            "usr_b", {"kind": "base", "title": "Private", "base_resume_id": None, "job_description_id": None}, "other-resume"
        )
    )

    assert client.get(f"/v1/resumes/{other.id}").status_code == 404
    assert client.patch(
        f"/v1/resumes/{other.id}", json={"title": "Stolen"}, headers=_headers("other-write")
    ).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "base",
            "title": "Arbitrary resume",
            "base_resume_id": "missing_resume",
        },
        {
            "kind": "base",
            "title": "Arbitrary job",
            "job_description_id": "missing_job",
        },
    ],
)
def test_base_resume_rejects_arbitrary_references(resume_client, payload):
    client, _ = resume_client

    response = client.post(
        "/v1/resumes",
        json=payload,
        headers=_headers(f"base-arbitrary-{payload['title']}"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_base_resume_rejects_cross_user_references(resume_client):
    client, sessions = resume_client
    _run(_seed_cross_user_references(sessions))

    response = client.post(
        "/v1/resumes",
        json={
            "kind": "base",
            "title": "Cross-user base",
            "base_resume_id": "resume_usr_b",
            "job_description_id": "job_usr_b",
        },
        headers=_headers("base-cross-user"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_resume_service_rejects_base_references_with_stable_error(resume_client):
    _, sessions = resume_client

    with pytest.raises(ResumeError) as caught:
        _run(
            ResumeService(sessions).create_resume(
                "usr_a",
                {
                    "kind": "base",
                    "title": "Service bypass",
                    "base_resume_id": "missing_resume",
                    "job_description_id": None,
                },
                "base-service-bypass",
            )
        )

    assert caught.value.code == "VALIDATION_FAILED"
    assert caught.value.status_code == 422


def test_identical_snapshot_does_not_create_a_second_version(resume_client):
    """Removing normal-save deduplication produces two version rows."""
    client, sessions = resume_client
    resume_id = _resume(client)
    first = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("one")},
        headers=_headers("v-3"),
    )
    duplicate = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 1, "snapshot": _snapshot("one")},
        headers=_headers("v-4"),
    )

    assert first.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == first.json()["id"]
    assert _run(_version_count(sessions, resume_id)) == 1


def test_saving_an_old_snapshot_creates_a_new_current_head(resume_client):
    """Deduplicating against history leaves the authoritative head on unrelated content."""
    client, sessions = resume_client
    resume_id = _resume(client, "old-head-resume")
    one = client.post(f"/v1/resumes/{resume_id}/versions", json={"base_version": 0, "snapshot": _snapshot("one")}, headers=_headers("old-head-1"))
    two = client.post(f"/v1/resumes/{resume_id}/versions", json={"base_version": 1, "snapshot": _snapshot("two")}, headers=_headers("old-head-2"))
    replayed = client.post(f"/v1/resumes/{resume_id}/versions", json={"base_version": 2, "snapshot": _snapshot("one")}, headers=_headers("old-head-3"))

    assert one.status_code == 201
    assert two.status_code == 201
    assert replayed.status_code == 201
    assert replayed.json()["id"] != one.json()["id"]
    assert _run(_version_count(sessions, resume_id)) == 3


def test_resume_list_uses_an_opaque_cursor(resume_client):
    """Ignoring cursor returns the first page again instead of the remaining resume."""
    client, _ = resume_client
    _resume(client, "cursor-r1")
    _resume(client, "cursor-r2")
    first = client.get("/v1/resumes?limit=1")
    second = client.get(f"/v1/resumes?limit=1&cursor={first.json()['next_cursor']}")

    assert first.status_code == 200
    assert first.json()["next_cursor"]
    assert second.status_code == 200
    assert second.json()["items"][0]["id"] != first.json()["items"][0]["id"]


def test_restore_copies_history_into_a_new_immutable_row(resume_client):
    """Returning the historical row instead of appending makes this fail."""
    client, sessions = resume_client
    resume_id = _resume(client)
    one = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("one")},
        headers=_headers("v-5"),
    ).json()
    client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 1, "snapshot": _snapshot("two")},
        headers=_headers("v-6"),
    )
    restored = client.post(
        f"/v1/resumes/{resume_id}/versions/{one['id']}/restore",
        json={"base_version": 2},
        headers=_headers("v-7"),
    )

    assert restored.status_code == 201
    assert restored.json()["id"] != one["id"]
    assert restored.json()["snapshot_hash"] == one["snapshot_hash"]
    assert _run(_version_count(sessions, resume_id)) == 3


def test_targeted_resume_does_not_change_the_base_snapshot_hash(resume_client):
    """Writing targeted content into the base resume would change base_hash."""
    client, sessions = resume_client
    base_id = _resume(client)
    base = client.post(
        f"/v1/resumes/{base_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("base")},
        headers=_headers("v-8"),
    ).json()
    _run(_job(sessions))
    targeted = client.post(
        "/v1/resumes",
        json={
            "kind": "job_targeted",
            "title": "Targeted",
            "base_resume_id": base_id,
            "job_description_id": "job_1",
        },
        headers=_headers("r-2"),
    )
    targeted_write = client.post(
        f"/v1/resumes/{targeted.json()['id']}/versions",
        json={"base_version": 0, "snapshot": _snapshot("tailored")},
        headers=_headers("v-9"),
    )

    assert targeted_write.status_code == 201
    assert _run(_version_hash(sessions, base["id"])) == base["snapshot_hash"]


def test_rejected_version_write_rolls_back_its_idempotency_record(resume_client):
    """Committing idempotency before bullet validation would leave a partial write."""
    client, sessions = resume_client
    resume_id = _resume(client, "rollback-resume")
    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={
            "base_version": 0,
            "snapshot": {
                "schema_version": "1",
                "title": "invalid",
                "target": None,
                "sections": [{"items": [{"text": "Unsupported claim", "fact_refs": []}]}],
            },
        },
        headers=_headers("rollback-version"),
    )

    assert response.status_code == 422
    assert _run(_version_count(sessions, resume_id)) == 0
    assert _run(_idempotency_count(sessions)) == 1  # the resume creation only


async def _job(sessions):
    async with sessions.begin() as session:
        session.add(JobDescription(id="job_1", owner_user_id="usr_a", title="Role", raw_encrypted="jd", status="ready"))


async def _seed_cross_user_references(sessions):
    async with sessions.begin() as session:
        session.add_all(
            [
                Resume(
                    id="resume_usr_b",
                    owner_user_id="usr_b",
                    kind="base",
                    title="B",
                ),
                JobDescription(
                    id="job_usr_b",
                    owner_user_id="usr_b",
                    title="B",
                    raw_encrypted="jd",
                    status="ready",
                ),
            ]
        )


async def _version_count(sessions, resume_id: str) -> int:
    async with sessions() as session:
        return len((await session.scalars(select(ResumeVersion).where(ResumeVersion.resume_id == resume_id))).all())


async def _version_hash(sessions, version_id: str) -> str:
    async with sessions() as session:
        return (await session.scalar(select(ResumeVersion.snapshot_hash).where(ResumeVersion.id == version_id)))


async def _idempotency_count(sessions) -> int:
    async with sessions() as session:
        return len((await session.scalars(select(IdempotencyRecord))).all())
