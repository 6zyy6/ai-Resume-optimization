import asyncio
from datetime import datetime, timedelta, timezone
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import (
    Base,
    BulletFactLink,
    Fact,
    FactCandidate,
    FactSource,
    IntakeAnswer,
    IntakeSession,
    Outbox,
    Resume,
    ResumeVersion,
    SourceRecord,
    Task,
    TaskEvent,
    UsageLedger,
    User,
    UserConsent,
    UserIdentity,
)
from app.main import create_app
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def intake_client(tmp_path):
    database_path = tmp_path / "intake.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    engine = create_async_engine(database_url)

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    application = create_app(Settings(app_env="test", database_url=database_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    _run(_create_schema(engine))
    encrypted_email = application.state.auth_service.users.email_crypto.encrypt(
        "student@example.com",
        application.state.auth_service.users.keys.get_key("email-encryption"),
    )
    _run(_seed_users(sessions, encrypted_email))
    active_owner = {"id": "usr_a"}

    def authenticated() -> AuthenticatedSession:
        now = datetime.now(timezone.utc)
        return AuthenticatedSession(
            active_owner["id"],
            f"ses_{active_owner['id']}",
            now,
            now + timedelta(days=1),
        )

    application.dependency_overrides[require_session] = authenticated
    with TestClient(application) as client:
        yield client, sessions, application, active_owner
    _run(engine.dispose())


async def _create_schema(engine):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def _seed_users(sessions, encrypted_email: str):
    now = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        session.add_all(
            [
                User(
                    id="usr_a",
                    email_encrypted=encrypted_email,
                    email_lookup_hash="student-email-hash",
                    password_hash="password-hash",
                ),
                User(id="usr_b"),
                UserIdentity(
                    id="idn_email",
                    owner_user_id="usr_a",
                    type="email_otp",
                    external_subject_hash="student-email-hash",
                    verified_at=now,
                ),
                UserConsent(
                    id="cns_agreement",
                    owner_user_id="usr_a",
                    document_type="user_agreement",
                    document_version="2026-07-27",
                    decision="accepted",
                    decided_at=now,
                ),
                UserConsent(
                    id="cns_privacy",
                    owner_user_id="usr_a",
                    document_type="privacy_policy",
                    document_version="2026-07-27",
                    decision="accepted",
                    decided_at=now,
                ),
            ]
        )


def _headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


def _start(client, key: str = "intake-start"):
    return client.post(
        "/v1/intake-sessions",
        json={"restart": False},
        headers=_headers(key),
    )


def _answer(
    client,
    session_id: str,
    question_id: str,
    *,
    base_version: int,
    key: str,
    answer: str | None = None,
    skipped: bool = False,
):
    payload = {
        "question_id": question_id,
        "base_version": base_version,
        "skipped": skipped,
    }
    if answer is not None:
        payload["answer"] = answer
    return client.post(
        f"/v1/intake-sessions/{session_id}/answers",
        json=payload,
        headers=_headers(key),
    )


def test_me_returns_only_masked_account_and_current_consents(intake_client):
    """Returning raw encrypted or plaintext email would expose account data."""
    client, _, _, _ = intake_client

    response = client.get("/v1/me")

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "usr_a",
        "masked_email": "st***@example.com",
        "identity_type": "email",
        "consent_versions": {
            "privacy_policy": "2026-07-27",
            "user_agreement": "2026-07-27",
        },
    }
    assert "student@example.com" not in response.text
    assert "password" not in response.text


def test_intake_create_reuses_active_session_and_idempotent_replay(intake_client):
    """Dropping active-session reuse or idempotency would create duplicate journeys."""
    client, sessions, _, _ = intake_client

    first = _start(client)
    replay = _start(client)
    resumed = _start(client, "intake-resume")

    assert first.status_code == 201
    assert replay.status_code == 201
    assert resumed.status_code == 200
    assert first.json()["id"] == replay.json()["id"] == resumed.json()["id"]
    assert first.json()["version"] == 0
    assert first.json()["current_question"]["id"] == "experience_radar"
    assert _run(_table_count(sessions, "intake_sessions")) == 1


def test_intake_idempotency_key_cannot_be_reused_for_restart(intake_client):
    """Accepting a changed body under one key makes retries semantically unsafe."""
    client, _, _, _ = intake_client
    assert _start(client, "same-key").status_code == 201

    conflict = client.post(
        "/v1/intake-sessions",
        json={"restart": True},
        headers=_headers("same-key"),
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_restart_does_not_abandon_a_draft_task_in_progress(intake_client):
    """Abandoning a drafting session would let its worker create an orphan resume."""
    client, sessions, _, _ = intake_client
    started = _start(client)
    _run(_set_intake_status(sessions, started.json()["id"], "drafting"))

    response = client.post(
        "/v1/intake-sessions",
        json={"restart": True},
        headers=_headers("restart-drafting"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INTAKE_DRAFT_IN_PROGRESS"
    assert _run(_table_count(sessions, "intake_sessions")) == 1


def test_positive_answer_queues_analysis_without_creating_facts(intake_client):
    """Creating a Fact before candidate confirmation bypasses the review boundary."""
    client, sessions, _, _ = intake_client
    started = _start(client)
    session_id = started.json()["id"]

    saved = _answer(
        client,
        session_id,
        "experience_radar",
        answer="我持续完成了一个课程项目",
        base_version=0,
        key="answer-project",
    )

    assert saved.status_code == 202
    body = saved.json()
    assert body["version"] == 1
    assert body["current_question"] is None
    assert body["analysis_status"] == "queued"
    assert body["analysis_task_id"]
    assert body["fact_summaries"] == []
    assert _run(_model_count(sessions, IntakeAnswer)) == 1
    assert _run(_model_count(sessions, Task)) == 1
    assert _run(_model_count(sessions, TaskEvent)) == 1
    assert _run(_model_count(sessions, Outbox)) == 1
    assert _run(_model_count(sessions, UsageLedger)) == 1
    assert _run(_model_count(sessions, SourceRecord)) == 0
    assert _run(_model_count(sessions, FactCandidate)) == 0
    assert _run(_model_count(sessions, Fact)) == 0
    assert _run(_model_count(sessions, FactSource)) == 0


@pytest.mark.parametrize(
    ("answer", "skipped", "expected_state"),
    [
        ("没有", False, "negative"),
        ("没有相关经历。", False, "negative"),
        ("暂时没有类似经验", False, "negative"),
        ("不知道", False, "negative"),
        ("不清楚", False, "negative"),
        (None, True, "skipped"),
    ],
)
def test_negative_and_skipped_answers_never_create_positive_facts(
    intake_client,
    answer,
    skipped,
    expected_state,
):
    """Treating a negative or skipped answer as a fact would fabricate experience."""
    client, sessions, _, _ = intake_client
    started = _start(client)

    saved = _answer(
        client,
        started.json()["id"],
        "experience_radar",
        answer=answer,
        skipped=skipped,
        base_version=0,
        key=f"negative-{expected_state}",
    )

    assert saved.status_code == 200
    assert saved.json()["analysis_status"] == "idle"
    assert saved.json()["analysis_task_id"] is None
    assert saved.json()["fact_summaries"] == []
    assert _run(_model_count(sessions, Task)) == 0
    assert _run(_model_count(sessions, TaskEvent)) == 0
    assert _run(_model_count(sessions, Outbox)) == 0
    assert _run(_model_count(sessions, UsageLedger)) == 0
    assert _run(_model_count(sessions, SourceRecord)) == 0
    assert _run(_model_count(sessions, FactCandidate)) == 0
    assert _run(_model_count(sessions, Fact)) == 0
    assert _run(_model_count(sessions, FactSource)) == 0
    assert _run(_answer_state(sessions)) == expected_state


def test_answer_rejects_stale_version_without_mutating_session(intake_client):
    """Ignoring base_version would silently overwrite answers from another device."""
    client, sessions, _, _ = intake_client
    started = _start(client)
    session_id = started.json()["id"]
    first = _answer(
        client,
        session_id,
        "experience_radar",
        answer="我做了一个项目",
        base_version=0,
        key="version-first",
    )

    stale = _answer(
        client,
        session_id,
        "course_role",
        answer="我负责调研",
        base_version=0,
        key="version-stale",
    )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "INTAKE_VERSION_CONFLICT"
    assert _run(_table_count(sessions, "intake_answers")) == 1


def test_intake_fallback_finishes_after_eight_distinct_questions(intake_client):
    """The fallback must not trap a user by returning the same terminal prompt forever."""
    client, sessions, application, _ = intake_client
    current = _start(client).json()
    question_ids: list[str] = []

    while current["current_question"] is not None:
        question_id = current["current_question"]["id"]
        question_ids.append(question_id)
        assert len(question_ids) <= 8, "fallback question flow did not terminate"
        current = _answer(
            client,
            current["id"],
            question_id,
            skipped=True,
            base_version=current["version"],
            key=f"skip-{len(question_ids)}",
        ).json()

    assert len(question_ids) == 8
    assert len(question_ids) == len(set(question_ids))
    assert current["remaining_estimate"] == 0


@pytest.mark.parametrize(
    ("answer", "expected_question"),
    [
        ("我完成了课程项目", "course_role"),
        ("我参加过暑期实习", "work_role"),
        ("我负责社团志愿活动", "community_role"),
        ("我独立做了个人项目", "project_role"),
    ],
)
def test_intake_first_follow_up_branches_by_experience(
    intake_client,
    answer,
    expected_question,
):
    """Different experience profiles must not all receive one fixed question sequence."""
    client, sessions, application, _ = intake_client
    started = _start(client)

    saved = _answer(
        client,
        started.json()["id"],
        "experience_radar",
        answer=answer,
        base_version=0,
        key=f"branch-{expected_question}",
    )

    assert saved.status_code == 202
    continued = _run(
        _fail_analysis_and_continue(
            client,
            sessions,
            application,
            started.json()["id"],
            saved.json()["analysis_task_id"],
        )
    )
    assert continued["current_question"]["id"] == expected_question


def test_other_owner_cannot_read_or_answer_intake_session(intake_client):
    """Removing the owner predicate from either query would expose private answers."""
    client, _, _, active_owner = intake_client
    started = _start(client)
    session_id = started.json()["id"]
    active_owner["id"] = "usr_b"

    assert client.get(f"/v1/intake-sessions/{session_id}").status_code == 404
    response = _answer(
        client,
        session_id,
        "experience_radar",
        answer="窃取",
        base_version=0,
        key="other-owner",
    )
    assert response.status_code == 404


def test_draft_worker_atomically_creates_resume_version_evidence_and_task_result(
    intake_client,
):
    """A partial draft must never leave an empty Resume or an evidence-free version."""
    client, sessions, application, _ = intake_client
    started = _start(client)
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            [
                "我持续完成了一个课程项目",
                "我负责用户调研和方案设计",
            ],
        )
    )

    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={"base_version": 0, "title": "产品经理基础简历"},
        headers=_headers("draft-create"),
    )

    assert queued.status_code == 202
    task_id = queued.json()["task_id"]
    assert _run(_model_count(sessions, Task)) == 1
    assert _run(_model_count(sessions, Outbox)) == 1
    assert _run(_model_count(sessions, Resume)) == 0
    claim = _run(application.state.task_service.claim_task("usr_a", task_id))
    assert claim is not None
    resume_id = _run(
        application.state.intake_service.process_draft(
            "usr_a",
            session_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    assert _run(_model_count(sessions, Resume)) == 1
    assert _run(_model_count(sessions, ResumeVersion)) == 1
    assert _run(_model_count(sessions, BulletFactLink)) == 2
    assert _run(_resume_head(sessions, resume_id)) == (1, 1)
    assert _run(_task_result(sessions, task_id)) == ("succeeded", resume_id)
    restored = client.get(f"/v1/intake-sessions/{session_id}")
    assert restored.json()["status"] == "completed"
    assert restored.json()["resume_id"] == resume_id


def test_draft_requires_two_confirmed_sourced_facts(intake_client):
    """Generating from unconfirmed input would make evidence-free claims exportable."""
    client, sessions, _, _ = intake_client
    started = _start(client)
    session_id = started.json()["id"]
    response = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={"base_version": 0, "title": "不应生成"},
        headers=_headers("draft-rejected"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INTAKE_FACTS_NOT_READY"
    assert _run(_model_count(sessions, Task)) == 0
    assert _run(_model_count(sessions, Resume)) == 0


async def _table_count(sessions, table_name: str) -> int:
    async with sessions() as session:
        return int(
            await session.scalar(text(f"SELECT COUNT(*) FROM {table_name}")) or 0
        )


async def _model_count(sessions, model) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _answer_state(sessions) -> str:
    async with sessions() as session:
        return str(
            await session.scalar(text("SELECT state FROM intake_answers LIMIT 1"))
        )


async def _resume_head(sessions, resume_id: str) -> tuple[int, int]:
    async with sessions() as session:
        resume = await session.scalar(select(Resume).where(Resume.id == resume_id))
        version_count = await session.scalar(
            select(func.count())
            .select_from(ResumeVersion)
            .where(ResumeVersion.resume_id == resume_id)
        )
        assert resume is not None
        return resume.head_version, int(version_count or 0)


async def _task_result(sessions, task_id: str) -> tuple[str, str | None]:
    async with sessions() as session:
        task = await session.scalar(select(Task).where(Task.id == task_id))
        assert task is not None
        return task.status, task.result_ref


async def _set_intake_status(sessions, session_id: str, status: str) -> None:
    from app.db.models import IntakeSession

    async with sessions.begin() as session:
        await session.execute(
            update(IntakeSession)
            .where(IntakeSession.id == session_id)
            .values(status=status)
        )


async def _fail_analysis_and_continue(
    client,
    sessions,
    application,
    session_id,
    task_id,
):
    claim = await application.state.task_service.claim_task("usr_a", task_id)
    await application.state.task_service.fail_task(
        "usr_a",
        task_id,
        claim.token,
        "provider_unavailable",
    )
    async with sessions.begin() as session:
        answer = await session.scalar(
            select(IntakeAnswer).where(IntakeAnswer.analysis_task_id == task_id)
        )
        answer.analysis_status = "failed"
    response = client.post(
        f"/v1/intake-sessions/{session_id}/analysis/continue",
        json={"base_version": 1},
        headers=_headers(f"continue-{task_id}"),
    )
    assert response.status_code == 200
    return response.json()


async def _seed_confirmed_intake_facts(sessions, session_id, values):
    async with sessions.begin() as session:
        intake = await session.scalar(
            select(IntakeSession).where(IntakeSession.id == session_id)
        )
        fact_ids = []
        for index, value in enumerate(values):
            source = SourceRecord(
                id=f"src_draft_{index}",
                owner_user_id=intake.owner_user_id,
                source_type="question_answer",
                source_ref=f"draft-test:{index}",
                content_encrypted=value,
            )
            fact = Fact(
                id=f"fact_draft_{index}",
                owner_user_id=intake.owner_user_id,
                kind="experience",
                value_encrypted=value,
                status="unconfirmed",
            )
            session.add_all((source, fact))
            await session.flush()
            session.add(
                FactSource(
                    fact_id=fact.id,
                    source_record_id=source.id,
                    owner_user_id=intake.owner_user_id,
                    source_range={"start": 0, "end": len(value)},
                    source_hash=hashlib.sha256(value.encode()).hexdigest(),
                )
            )
            await session.flush()
            fact.status = "confirmed"
            fact.confirmed_at = datetime.now(timezone.utc)
            fact_ids.append(fact.id)
        intake.fact_ids = fact_ids
