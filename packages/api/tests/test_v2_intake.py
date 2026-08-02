import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import (
    Base,
    AiRun,
    AiTraceEvent,
    BulletFactLink,
    Experience,
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
from app.integrations.ai_client import (
    AiExecutionReceipt,
    ComposeResumeDraftRequest,
    derive_ai_run_id,
)
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.intake.service import IntakeService
from app.integrations.storage import MemoryStorage
from app.workers.execution import TaskExecutor, resolve_operation
from app.workers.pipeline import configure_pipeline_operations


class DraftReceiptClient:
    def __init__(self, sections=(), *, status="succeeded", error_code=None):
        self.sections = sections
        self.status = status
        self.error_code = error_code
        self.requests = []

    async def run(self, input, cancellation=None):
        assert isinstance(input, ComposeResumeDraftRequest)
        self.requests.append(input)
        ai_run_id = derive_ai_run_id(input.task_id, "draft", input.input_hash)
        result = {"sections": self.sections} if self.status == "succeeded" else None
        terminal = {
            "succeeded": "run_succeeded",
            "failed": "run_failed",
            "cancelled": "run_cancelled",
        }[self.status]
        return AiExecutionReceipt.model_validate_json(
            json.dumps(
                {
                    "run": {
                        "ai_run_id": ai_run_id,
                        "trace_id": input.trace_id,
                        "task_id": input.task_id,
                        "workflow_type": "compose_resume_draft",
                        "workflow_version": "2",
                        "prompt_template_version": "resume-draft@2",
                        "status": self.status,
                        "error_code": self.error_code,
                        "provider": "test-faux",
                        "requested_model": "faux-1",
                        "response_model": "faux-1.1",
                        "started_at": "2026-08-02T08:00:00Z",
                        "first_token_at": "2026-08-02T08:00:01Z",
                        "finished_at": "2026-08-02T08:00:02Z",
                        "usage": {
                            "input": 10,
                            "output": 5,
                            "cache_read": 0,
                            "cache_write": 0,
                            "reasoning": 0,
                            "total_tokens": 15,
                            "cost_usd": "0.001",
                        },
                        "events": [
                            {
                                "ai_run_id": ai_run_id,
                                "trace_id": input.trace_id,
                                "task_id": input.task_id,
                                "event_seq": 1,
                                "event_type": "agent_start",
                                "occurred_at": "2026-08-02T08:00:00Z",
                                "details": {"provider": "test-faux"},
                            },
                            {
                                "ai_run_id": ai_run_id,
                                "trace_id": input.trace_id,
                                "task_id": input.task_id,
                                "event_seq": 2,
                                "event_type": terminal,
                                "occurred_at": "2026-08-02T08:00:02Z",
                                "details": {"error_code": self.error_code},
                            },
                        ],
                        "turn_count": 1,
                        "tool_call_count": 0,
                        "retry_count": 0,
                        "fallback_count": 0,
                        "schema_valid": True,
                        "facts_valid": self.status == "succeeded",
                        "input_hash": input.input_hash,
                        "exportable": self.status == "succeeded",
                        "risk_flags": [],
                    },
                    "result": result,
                },
                ensure_ascii=False,
            )
        )


class CancellingDraftReceiptClient(DraftReceiptClient):
    def __init__(self, task_service):
        super().__init__(status="cancelled")
        self.task_service = task_service

    async def run(self, input, cancellation=None):
        ai_run_id = derive_ai_run_id(input.task_id, "draft", input.input_hash)
        assert cancellation is not None
        assert await cancellation.register_run(ai_run_id) is True
        await self.task_service.request_cancel("usr_a", input.task_id)
        assert await cancellation.is_cancel_requested() is True
        await cancellation.acknowledge_cancel(ai_run_id)
        return await super().run(input)


class RaisingDraftClient:
    def __init__(self):
        self.calls = 0

    async def run(self, input, cancellation=None):
        self.calls += 1
        raise ValueError("invalid draft response")


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
        json={
            "base_version": 0,
            "title": "产品经理基础简历",
            "generation_mode": "rule_fallback",
        },
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
    assert _run(_model_count(sessions, UsageLedger)) == 0
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
        json={
            "base_version": 0,
            "title": "不应生成",
            "generation_mode": "model",
        },
        headers=_headers("draft-rejected"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INTAKE_FACTS_NOT_READY"
    assert _run(_model_count(sessions, Task)) == 0
    assert _run(_model_count(sessions, Resume)) == 0


def test_model_draft_queues_an_immutable_sourced_input_snapshot(intake_client):
    client, sessions, _, _ = intake_client
    started = _start(client, "draft-snapshot-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )

    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "模型草稿",
            "generation_mode": "model",
        },
        headers=_headers("draft-snapshot"),
    )

    assert queued.status_code == 202
    payload = _run(_outbox_payload(sessions, queued.json()["task_id"]))
    assert payload["generation_mode"] == "model"
    assert len(payload["draft_input_hash"]) == 64
    assert payload["draft_snapshot"]["workflow_type"] == "compose_resume_draft"
    assert [
        fact["value"]
        for fact in payload["draft_snapshot"]["payload"]["confirmed_facts"]
    ] == ["负责用户调研", "完成产品原型"]
    assert all(
        fact["source_hashes"]
        for fact in payload["draft_snapshot"]["payload"]["confirmed_facts"]
    )
    assert _run(_model_count(sessions, UsageLedger)) == 1


def test_public_cancel_restores_unclaimed_draft_and_allows_explicit_fallback(
    intake_client,
):
    client, sessions, _, _ = intake_client
    started = _start(client, "public-cancel-draft-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "待取消模型草稿",
            "generation_mode": "model",
        },
        headers=_headers("public-cancel-model-draft"),
    )
    task_id = queued.json()["task_id"]
    assert _run(_intake_draft_state(sessions, session_id)) == ("drafting", 1)
    assert _run(_usage_for_task(sessions, task_id)).state == "reserved"

    cancelled = client.post(
        f"/v1/tasks/{task_id}/cancel",
        headers=_headers("public-cancel-model-task"),
    )
    replay = client.post(
        f"/v1/tasks/{task_id}/cancel",
        headers=_headers("public-cancel-model-task"),
    )

    assert cancelled.status_code == 200
    assert replay.json() == cancelled.json()
    assert _run(_task_result(sessions, task_id)) == ("cancelled", None)
    assert _run(_intake_draft_state(sessions, session_id)) == ("active", 2)
    assert _run(_usage_for_task(sessions, task_id)).state == "released"
    assert _run(_model_count(sessions, Resume)) == 0
    assert _run(_model_count(sessions, ResumeVersion)) == 0
    assert "draft_snapshot" not in _run(_outbox_payload(sessions, task_id))

    fallback = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 2,
            "title": "事实原文草稿",
            "generation_mode": "rule_fallback",
        },
        headers=_headers("fallback-after-public-cancel"),
    )
    assert fallback.status_code == 202
    assert fallback.json()["task_id"] != task_id
    assert _run(_usage_for_task(sessions, fallback.json()["task_id"])) is None


def test_direct_cancel_restores_only_unclaimed_intake_draft(intake_client):
    client, sessions, application, _ = intake_client
    started = _start(client, "direct-cancel-draft-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "直接取消草稿",
            "generation_mode": "model",
        },
        headers=_headers("direct-cancel-model-draft"),
    )
    task_id = queued.json()["task_id"]

    _run(application.state.task_service.request_cancel("usr_a", task_id))

    assert _run(_intake_draft_state(sessions, session_id)) == ("active", 2)
    assert _run(_usage_for_task(sessions, task_id)).state == "released"
    assert _run(_model_count(sessions, Resume)) == 0
    assert _run(_model_count(sessions, ResumeVersion)) == 0


def test_running_unstarted_draft_cancel_restores_intake_and_releases_reservation(
    intake_client,
):
    client, sessions, application, _ = intake_client
    started = _start(client, "running-cancel-draft-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "运行中取消草稿",
            "generation_mode": "model",
        },
        headers=_headers("running-cancel-model-draft"),
    )
    task_id = queued.json()["task_id"]
    claim = _run(application.state.task_service.claim_task("usr_a", task_id))
    assert claim is not None

    _run(application.state.task_service.request_cancel("usr_a", task_id))

    assert _run(_intake_draft_state(sessions, session_id)) == ("active", 2)
    assert _run(_usage_for_task(sessions, task_id)).state == "released"


def test_draft_snapshot_keeps_same_title_experiences_in_distinct_groups(
    intake_client,
):
    client, sessions, _, _ = intake_client
    started = _start(client, "same-title-groups-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责第一项工作", "负责第二项工作"],
        )
    )
    _run(_assign_same_title_experiences(sessions))

    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "同名经历草稿",
            "generation_mode": "model",
        },
        headers=_headers("same-title-groups"),
    )

    groups = _run(_outbox_payload(sessions, queued.json()["task_id"]))[
        "draft_snapshot"
    ]["payload"]["experience_groups"]
    assert groups == [
        {"title": "课程项目", "fact_refs": ["fact_draft_0"]},
        {"title": "课程项目", "fact_refs": ["fact_draft_1"]},
    ]


def test_draft_snapshot_keeps_same_kind_distinct_sources_in_distinct_groups(
    intake_client,
):
    client, sessions, _, _ = intake_client
    started = _start(client, "same-kind-groups-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责第一项工作", "负责第二项工作"],
        )
    )

    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "同类来源草稿",
            "generation_mode": "model",
        },
        headers=_headers("same-kind-groups"),
    )

    groups = _run(_outbox_payload(sessions, queued.json()["task_id"]))[
        "draft_snapshot"
    ]["payload"]["experience_groups"]
    assert groups == [
        {"title": "experience", "fact_refs": ["fact_draft_0"]},
        {"title": "experience", "fact_refs": ["fact_draft_1"]},
    ]


def test_draft_generation_mode_is_required(intake_client):
    client, sessions, _, _ = intake_client
    started = _start(client, "draft-mode-start")

    response = client.post(
        f"/v1/intake-sessions/{started.json()['id']}/drafts",
        json={"base_version": 0, "title": "缺少模式"},
        headers=_headers("draft-mode-required"),
    )

    assert response.status_code == 422
    assert _run(_model_count(sessions, Task)) == 0


def test_model_draft_persists_only_supported_claims_with_ai_provenance(
    intake_client,
):
    client, sessions, application, _ = intake_client
    started = _start(client, "model-draft-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    ai_client = DraftReceiptClient(
        sections=[
            {
                "type": "experience",
                "title": "课程项目",
                "bullets": [
                    {
                        "text": "负责用户调研；获得国家级一等奖",
                        "atomic_claims": [
                            {
                                "text": "负责用户调研",
                                "fact_refs": ["fact_draft_0"],
                                "claim_order": 0,
                            },
                            {
                                "text": "获得国家级一等奖",
                                "fact_refs": ["fact_draft_0"],
                                "claim_order": 1,
                            },
                        ],
                        "risk_flags": [],
                    }
                ],
            }
        ]
    )
    application.state.intake_service = IntakeService(sessions, ai_client)
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "模型草稿",
            "generation_mode": "model",
        },
        headers=_headers("model-draft"),
    )
    task_id = queued.json()["task_id"]
    claim = _run(application.state.task_service.claim_task("usr_a", task_id))

    resume_id = _run(
        application.state.intake_service.process_draft(
            "usr_a",
            session_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    version = _run(_resume_version(sessions, resume_id))
    assert ai_client.requests[0].payload.confirmed_facts[0].source_hashes
    assert version.snapshot_json["sections"][0]["items"] == [
        {
            "id": version.snapshot_json["sections"][0]["items"][0]["id"],
            "text": "负责用户调研",
            "fact_refs": ["fact_draft_0"],
        }
    ]
    assert version.generation_mode == "model"
    assert version.workflow_version == "2"
    assert version.ai_run_id == derive_ai_run_id(
        task_id,
        "draft",
        version.input_hash,
    )
    assert _run(_model_count(sessions, AiRun)) == 1
    assert _run(_only_ai_run(sessions)).result_ref == resume_id
    assert _run(_model_count(sessions, BulletFactLink)) == 1
    terminal_payload = _run(_outbox_payload(sessions, task_id))
    assert "draft_snapshot" not in terminal_payload
    assert terminal_payload["generation_mode"] == "model"
    assert terminal_payload["draft_input_hash"] == version.input_hash


def test_model_draft_omits_responsibility_inflation_and_records_reason(
    intake_client,
):
    client, sessions, application, _ = intake_client
    started = _start(client, "responsibility-draft-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["参与用户调研", "完成产品原型"],
        )
    )
    ai_client = DraftReceiptClient(
        sections=[
            {
                "type": "experience",
                "title": "课程项目",
                "bullets": [
                    {
                        "text": "负责用户调研；完成产品原型",
                        "atomic_claims": [
                            {
                                "text": "负责用户调研",
                                "fact_refs": ["fact_draft_0"],
                                "claim_order": 0,
                            },
                            {
                                "text": "完成产品原型",
                                "fact_refs": ["fact_draft_1"],
                                "claim_order": 1,
                            },
                        ],
                        "risk_flags": [],
                    }
                ],
            }
        ]
    )
    application.state.intake_service = IntakeService(sessions, ai_client)
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "职责强度草稿",
            "generation_mode": "model",
        },
        headers=_headers("responsibility-model-draft"),
    )
    task_id = queued.json()["task_id"]
    claim = _run(application.state.task_service.claim_task("usr_a", task_id))

    resume_id = _run(
        application.state.intake_service.process_draft(
            "usr_a",
            session_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    version = _run(_resume_version(sessions, resume_id))
    assert [
        item["text"]
        for section in version.snapshot_json["sections"]
        for item in section["items"]
    ] == ["完成产品原型"]
    assert any(
        "CLAIM_RESPONSIBILITY_STRENGTH_UNSUPPORTED"
        in (payload or {}).get("risk_flags", [])
        for payload in _run(_trace_payloads(sessions, version.ai_run_id))
    )


def test_failed_model_creates_no_resume_then_allows_explicit_literal_fallback(
    intake_client,
):
    client, sessions, application, _ = intake_client
    started = _start(client, "failed-draft-start")
    session_id = started.json()["id"]
    values = ["负责用户调研", "完成产品原型"]
    _run(_seed_confirmed_intake_facts(sessions, session_id, values))
    application.state.intake_service = IntakeService(
        sessions,
        DraftReceiptClient(status="failed", error_code="provider_unavailable"),
    )
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "模型草稿",
            "generation_mode": "model",
        },
        headers=_headers("failed-model-draft"),
    )
    task_id = queued.json()["task_id"]
    claim = _run(application.state.task_service.claim_task("usr_a", task_id))

    _run(
        application.state.intake_service.process_draft(
            "usr_a",
            session_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    assert _run(_model_count(sessions, Resume)) == 0
    assert _run(_model_count(sessions, ResumeVersion)) == 0
    failed_payload = _run(_outbox_payload(sessions, task_id))
    assert "draft_snapshot" not in failed_payload
    assert failed_payload["generation_mode"] == "model"
    assert len(failed_payload["draft_input_hash"]) == 64
    restored = client.get(f"/v1/intake-sessions/{session_id}").json()
    assert restored["status"] == "active"
    fallback = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": restored["version"],
            "title": "事实原文草稿",
            "generation_mode": "rule_fallback",
        },
        headers=_headers("explicit-fallback-draft"),
    )
    assert fallback.status_code == 202
    fallback_task_id = fallback.json()["task_id"]
    assert _run(_usage_for_task(sessions, fallback_task_id)) is None
    claim = _run(
        application.state.task_service.claim_task("usr_a", fallback_task_id)
    )
    resume_id = _run(
        application.state.intake_service.process_draft(
            "usr_a",
            session_id,
            task_id=fallback_task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    version = _run(_resume_version(sessions, resume_id))
    assert [
        item["text"]
        for section in version.snapshot_json["sections"]
        for item in section["items"]
    ] == values
    assert version.generation_mode == "rule_fallback"
    assert version.workflow_version == "2"
    assert version.ai_run_id is None
    assert version.input_hash is not None


def test_cancelled_draft_receipt_persists_audit_and_recovers_intake(
    intake_client,
):
    client, sessions, application, _ = intake_client
    started = _start(client, "cancelled-draft-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "取消的模型草稿",
            "generation_mode": "model",
        },
        headers=_headers("cancelled-model-draft"),
    )
    task_id = queued.json()["task_id"]
    configure_pipeline_operations(
        sessions,
        Settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:"),
        application.state.task_service,
        storage_override=MemoryStorage(),
        ai_client_override=CancellingDraftReceiptClient(
            application.state.task_service
        ),
    )
    claim = _run(application.state.task_service.claim_task("usr_a", task_id))
    assert claim is not None

    result_ref = _run(resolve_operation("generate_intake_draft")(claim))

    assert result_ref == session_id
    assert _run(_task_result(sessions, task_id)) == ("cancelled", None)
    assert _run(_intake_draft_state(sessions, session_id)) == ("active", 2)
    assert _run(_usage_for_task(sessions, task_id)).state == "consumed"
    ai_run = _run(_only_ai_run(sessions))
    assert ai_run.status == "cancelled"
    assert _run(_trace_sequences(sessions, ai_run.id)) == [1, 2]
    assert _run(_model_count(sessions, Resume)) == 0
    assert _run(_model_count(sessions, ResumeVersion)) == 0
    assert "draft_snapshot" not in _run(_outbox_payload(sessions, task_id))


def test_cancel_before_draft_operation_prevents_ai_and_allows_fallback(
    intake_client,
):
    client, sessions, application, _ = intake_client
    started = _start(client, "executor-cancel-race-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "竞态取消草稿",
            "generation_mode": "model",
        },
        headers=_headers("executor-cancel-race-draft"),
    )
    task_id = queued.json()["task_id"]
    ai_client = DraftReceiptClient()
    configure_pipeline_operations(
        sessions,
        Settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:"),
        application.state.task_service,
        storage_override=MemoryStorage(),
        ai_client_override=ai_client,
    )

    async def race():
        service = application.state.task_service
        original_claim = service.claim_task
        claimed = asyncio.Event()
        continue_to_operation = asyncio.Event()

        async def claim_then_pause(*args, **kwargs):
            claim = await original_claim(*args, **kwargs)
            claimed.set()
            await continue_to_operation.wait()
            return claim

        service.claim_task = claim_then_pause
        try:
            execution = asyncio.create_task(
                TaskExecutor(service).execute("usr_a", task_id, resolve_operation)
            )
            await claimed.wait()
            await service.request_cancel("usr_a", task_id)
            continue_to_operation.set()
            return await execution
        finally:
            service.claim_task = original_claim

    result = _run(race())

    assert result["status"] == "cancelled"
    assert ai_client.requests == []
    assert _run(_intake_draft_state(sessions, session_id)) == ("active", 2)
    assert _run(_usage_for_task(sessions, task_id)).state == "released"
    assert "draft_snapshot" not in _run(_outbox_payload(sessions, task_id))
    fallback = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 2,
            "title": "竞态后的事实草稿",
            "generation_mode": "rule_fallback",
        },
        headers=_headers("fallback-after-executor-cancel"),
    )
    assert fallback.status_code == 202


def test_draft_terminal_failure_handler_clears_private_snapshot(intake_client):
    client, sessions, application, _ = intake_client
    started = _start(client, "terminal-draft-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "终态失败草稿",
            "generation_mode": "model",
        },
        headers=_headers("terminal-failure-draft"),
    )
    task_id = queued.json()["task_id"]
    ai_client = RaisingDraftClient()
    configure_pipeline_operations(
        sessions,
        Settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:"),
        application.state.task_service,
        storage_override=MemoryStorage(),
        ai_client_override=ai_client,
    )

    result = _run(
        TaskExecutor(application.state.task_service).execute(
            "usr_a",
            task_id,
            resolve_operation,
        )
    )

    assert result["status"] == "failed"
    assert ai_client.calls == 1
    assert _run(_intake_draft_state(sessions, session_id)) == ("active", 2)
    payload = _run(_outbox_payload(sessions, task_id))
    assert "draft_snapshot" not in payload
    assert payload["generation_mode"] == "model"
    assert len(payload["draft_input_hash"]) == 64


def test_model_draft_persists_supported_non_literal_rewrite_with_exact_link(
    intake_client,
):
    client, sessions, application, _ = intake_client
    started = _start(client, "rewrite-draft-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    rewrite = "用户调研"
    ai_client = DraftReceiptClient(
        sections=[
            {
                "type": "experience",
                "title": "课程项目",
                "bullets": [
                    {
                        "text": rewrite,
                        "atomic_claims": [
                            {
                                "text": rewrite,
                                "fact_refs": ["fact_draft_0"],
                                "claim_order": 0,
                            }
                        ],
                        "risk_flags": [],
                    }
                ],
            }
        ]
    )
    application.state.intake_service = IntakeService(sessions, ai_client)
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "安全改写草稿",
            "generation_mode": "model",
        },
        headers=_headers("rewrite-model-draft"),
    )
    task_id = queued.json()["task_id"]
    claim = _run(application.state.task_service.claim_task("usr_a", task_id))

    resume_id = _run(
        application.state.intake_service.process_draft(
            "usr_a",
            session_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    version = _run(_resume_version(sessions, resume_id))
    assert version.snapshot_json["sections"][0]["items"][0]["text"] == rewrite
    link = _run(_only_bullet_link(sessions))
    assert link.fact_id == "fact_draft_0"
    assert link.claim_range == {"start": 0, "end": len(rewrite)}
    assert link.fact_value_encrypted_at_link == "负责用户调研"


def test_model_draft_with_no_supported_claims_has_no_dangling_result_ref(
    intake_client,
):
    client, sessions, application, _ = intake_client
    started = _start(client, "unsupported-draft-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    unsupported = "获得国家级一等奖"
    application.state.intake_service = IntakeService(
        sessions,
        DraftReceiptClient(
            sections=[
                {
                    "type": "experience",
                    "title": "课程项目",
                    "bullets": [
                        {
                            "text": unsupported,
                            "atomic_claims": [
                                {
                                    "text": unsupported,
                                    "fact_refs": ["fact_draft_0"],
                                    "claim_order": 0,
                                }
                            ],
                            "risk_flags": [],
                        }
                    ],
                }
            ]
        ),
    )
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "不支持草稿",
            "generation_mode": "model",
        },
        headers=_headers("unsupported-model-draft"),
    )
    task_id = queued.json()["task_id"]
    claim = _run(application.state.task_service.claim_task("usr_a", task_id))

    result_ref = _run(
        application.state.intake_service.process_draft(
            "usr_a",
            session_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    assert result_ref == session_id
    assert _run(_model_count(sessions, Resume)) == 0
    ai_run = _run(_only_ai_run(sessions))
    assert ai_run.result_ref == session_id
    assert _run(_task_result(sessions, task_id))[0] == "failed"
    trace_payloads = _run(_trace_payloads(sessions, ai_run.id))
    assert any(
        payload and payload.get("risk_flags") == ["CLAIM_FACT_MISMATCH"]
        for payload in trace_payloads
    )
    assert unsupported not in json.dumps(trace_payloads, ensure_ascii=False)


def test_draft_worker_rejects_fact_state_changed_after_queue(intake_client):
    client, sessions, application, _ = intake_client
    started = _start(client, "changed-fact-draft-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    ai_client = DraftReceiptClient(
        sections=[
            {
                "type": "experience",
                "title": "课程项目",
                "bullets": [
                    {
                        "text": "负责用户调研",
                        "atomic_claims": [
                            {
                                "text": "负责用户调研",
                                "fact_refs": ["fact_draft_0"],
                                "claim_order": 0,
                            }
                        ],
                        "risk_flags": [],
                    }
                ],
            }
        ]
    )
    application.state.intake_service = IntakeService(
        sessions,
        ai_client,
    )
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "事实变化草稿",
            "generation_mode": "model",
        },
        headers=_headers("changed-fact-model-draft"),
    )
    task_id = queued.json()["task_id"]
    _run(_reject_fact(sessions, "fact_draft_1"))
    claim = _run(application.state.task_service.claim_task("usr_a", task_id))

    result_ref = _run(
        application.state.intake_service.process_draft(
            "usr_a",
            session_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )
    assert result_ref == session_id
    assert _run(_model_count(sessions, Resume)) == 0
    assert _run(_model_count(sessions, ResumeVersion)) == 0
    assert _run(_model_count(sessions, AiRun)) == 1
    assert _run(_only_ai_run(sessions)).result_ref == session_id
    assert _run(_usage_for_task(sessions, task_id)).state == "consumed"
    assert _run(_task_result(sessions, task_id))[0] == "failed"
    assert len(ai_client.requests) == 1
    assert client.get(f"/v1/intake-sessions/{session_id}").json()["status"] == "active"


def test_rule_fallback_rejects_changed_queued_fact_without_ai_ledger(intake_client):
    client, sessions, application, _ = intake_client
    started = _start(client, "changed-fallback-start")
    session_id = started.json()["id"]
    _run(
        _seed_confirmed_intake_facts(
            sessions,
            session_id,
            ["负责用户调研", "完成产品原型"],
        )
    )
    queued = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={
            "base_version": 0,
            "title": "事实原文草稿",
            "generation_mode": "rule_fallback",
        },
        headers=_headers("changed-fallback"),
    )
    task_id = queued.json()["task_id"]
    _run(_reject_fact(sessions, "fact_draft_1"))
    claim = _run(application.state.task_service.claim_task("usr_a", task_id))

    result_ref = _run(
        application.state.intake_service.process_draft(
            "usr_a",
            session_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    assert result_ref == session_id
    assert _run(_model_count(sessions, Resume)) == 0
    assert _run(_model_count(sessions, ResumeVersion)) == 0
    assert _run(_model_count(sessions, AiRun)) == 0
    assert _run(_usage_for_task(sessions, task_id)) is None
    assert _run(_task_result(sessions, task_id))[0] == "failed"
    assert client.get(f"/v1/intake-sessions/{session_id}").json()["status"] == "active"


async def _table_count(sessions, table_name: str) -> int:
    async with sessions() as session:
        return int(
            await session.scalar(text(f"SELECT COUNT(*) FROM {table_name}")) or 0
        )


async def _model_count(sessions, model) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _outbox_payload(sessions, task_id):
    async with sessions() as session:
        outbox = await session.scalar(select(Outbox).where(Outbox.task_id == task_id))
        return dict(outbox.payload)


async def _resume_version(sessions, resume_id):
    async with sessions() as session:
        return await session.scalar(
            select(ResumeVersion).where(ResumeVersion.resume_id == resume_id)
        )


async def _usage_for_task(sessions, task_id):
    async with sessions() as session:
        return await session.scalar(
            select(UsageLedger).where(UsageLedger.task_id == task_id)
        )


async def _only_ai_run(sessions):
    async with sessions() as session:
        return await session.scalar(select(AiRun))


async def _trace_payloads(sessions, ai_run_id):
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(AiTraceEvent.payload)
                    .where(AiTraceEvent.ai_run_id == ai_run_id)
                    .order_by(AiTraceEvent.event_seq)
                )
            ).all()
        )


async def _trace_sequences(sessions, ai_run_id):
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(AiTraceEvent.event_seq)
                    .where(AiTraceEvent.ai_run_id == ai_run_id)
                    .order_by(AiTraceEvent.event_seq)
                )
            ).all()
        )


async def _intake_draft_state(sessions, session_id):
    async with sessions() as session:
        row = await session.scalar(
            select(IntakeSession).where(IntakeSession.id == session_id)
        )
        return row.status, row.version


async def _only_bullet_link(sessions):
    async with sessions() as session:
        return await session.scalar(select(BulletFactLink))


async def _reject_fact(sessions, fact_id):
    async with sessions.begin() as session:
        fact = await session.scalar(select(Fact).where(Fact.id == fact_id))
        fact.status = "rejected"


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


async def _assign_same_title_experiences(sessions):
    async with sessions.begin() as session:
        session.add_all(
            [
                Experience(
                    id="exp_draft_0",
                    owner_user_id="usr_a",
                    type="project",
                    title="课程项目",
                ),
                Experience(
                    id="exp_draft_1",
                    owner_user_id="usr_a",
                    type="project",
                    title="课程项目",
                ),
            ]
        )
        await session.flush()
        for index in range(2):
            fact = await session.scalar(
                select(Fact).where(Fact.id == f"fact_draft_{index}")
            )
            fact.experience_id = f"exp_draft_{index}"
