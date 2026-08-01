import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from pydantic import ValidationError
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy import event as sqlalchemy_event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.models import (
    AiRun,
    AiTraceEvent,
    Base,
    FactCandidate,
    Fact,
    FactSource,
    IntakeAnswer,
    IntakeSession,
    SourceRecord,
    Task,
    UsageLedger,
    User,
)
from app.integrations.ai_client import (
    AiExecutionReceipt,
    AnalyzeIntakeRequest,
    FactCandidate as AiFactCandidate,
    derive_ai_run_id,
)
from app.main import create_app
from app.integrations.storage import MemoryStorage
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.intake.service import IntakeService
from app.workers.execution import TaskExecutor, resolve_operation
from app.workers.pipeline import configure_pipeline_operations


def test_fact_candidate_metadata_has_decision_and_owner_boundaries():
    """Weak candidate constraints could cross owners or create two decision facts."""
    table = Base.metadata.tables.get("fact_candidates")

    assert table is not None
    assert {
        "id",
        "owner_user_id",
        "intake_answer_id",
        "kind",
        "value_encrypted",
        "source_start",
        "source_end",
        "source_hash",
        "status",
        "decision_mode",
        "ai_run_id",
        "decision_source_id",
        "fact_id",
        "decided_at",
        "decided_by",
    } <= set(table.c.keys())
    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "pending" in check_sql and "accepted" in check_sql
    assert "accept_or_edit" in check_sql and "edit_only" in check_sql
    assert "source_start" in check_sql and "source_end" in check_sql
    assert "decided_at IS NULL" in check_sql
    assert "decided_at IS NOT NULL" in check_sql
    assert "decision_source_id IS NOT NULL" in check_sql
    assert "fact_id IS NOT NULL" in check_sql
    foreign_key_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert {
        "fk_fact_candidate_answer_owner",
        "fk_fact_candidate_ai_run_owner",
        "fk_fact_candidate_decision_source_owner",
        "fk_fact_candidate_fact_owner",
    } <= foreign_key_names
    unique_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_fact_candidate_owner" in unique_names


def test_intake_answer_metadata_tracks_one_owner_scoped_analysis_task():
    """An unscoped analysis task could attach another user's run to an answer."""
    table = Base.metadata.tables["intake_answers"]

    assert {
        "analysis_status",
        "analysis_task_id",
        "analysis_input_version",
        "analysis_input_hash",
        "next_question_source",
    } <= set(table.c.keys())
    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "waiting_for_confirmation" in check_sql
    assert "fallback" in check_sql
    foreign_key_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert "fk_intake_answer_analysis_task_owner" in foreign_key_names


def test_source_record_metadata_allows_candidate_edit_provenance():
    """Rejecting fact_candidate_edit would force edited text onto the answer source."""
    table = Base.metadata.tables["source_records"]
    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )

    assert "fact_candidate_edit" in check_sql


def test_typed_ai_candidate_requires_the_exact_source_hash():
    """Without a typed source hash FastAPI cannot reject a fabricated source slice."""
    values = {
        "kind": "experience",
        "value": "完成课程项目",
        "source_answer_id": "ians_1",
        "source_range": {"start": 2, "end": 8},
        "risk_flags": (),
    }

    with pytest.raises(ValidationError):
        AiFactCandidate.model_validate(values)
    candidate = AiFactCandidate.model_validate(
        {**values, "source_hash": "a" * 64}
    )
    assert candidate.source_hash == "a" * 64


@pytest.mark.parametrize("new_semantics", ["candidate", "edit_source", "analysis"])
def test_migration_0013_refuses_to_drop_new_intake_semantics(
    tmp_path,
    monkeypatch,
    new_semantics,
):
    """Downgrade must not silently erase candidates, edits, or analysis state."""
    database_path = tmp_path / f"intake-{new_semantics}.db"
    config = _alembic_config(database_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "0013")
    engine = create_engine(f"sqlite:///{database_path}")
    now = "2026-08-02 08:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, status, locale, created_at) "
                "VALUES ('usr_intake', 'active', 'zh-CN', :now)"
            ),
            {"now": now},
        )
        if new_semantics == "candidate":
            connection.execute(
                text(
                    "INSERT INTO fact_candidates "
                    "(id, intake_answer_id, kind, value_encrypted, source_start, "
                    "source_end, source_hash, status, decision_mode, ai_run_id, "
                    "created_at, owner_user_id) VALUES "
                    "('fc_1', 'ians_1', 'skill', 'Python', 0, 6, :hash, "
                    "'pending', 'accept_or_edit', 'run_1', :now, 'usr_intake')"
                ),
                {"hash": "a" * 64, "now": now},
            )
        elif new_semantics == "edit_source":
            connection.execute(
                text(
                    "INSERT INTO source_records "
                    "(id, source_type, source_ref, content_encrypted, created_at, "
                    "owner_user_id) VALUES "
                    "('src_1', 'fact_candidate_edit', 'fc_1', 'edited', :now, "
                    "'usr_intake')"
                ),
                {"now": now},
            )
        else:
            connection.execute(
                text(
                    "INSERT INTO intake_sessions "
                    "(id, status, version, answered_question_ids, skipped_question_ids, "
                    "fact_ids, created_at, updated_at, owner_user_id) VALUES "
                    "('intake_1', 'active', 1, '[]', '[]', '[]', :now, :now, "
                    "'usr_intake')"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO intake_answers "
                    "(id, session_id, question_id, answer_encrypted, state, created_at, "
                    "owner_user_id, analysis_status, analysis_input_version, "
                    "analysis_input_hash) VALUES "
                    "('ians_1', 'intake_1', 'question_1', 'Python', 'answered', :now, "
                    "'usr_intake', 'failed', 1, :hash)"
                ),
                {"hash": "a" * 64, "now": now},
            )

    with pytest.raises(RuntimeError, match="cannot downgrade intake analysis"):
        command.downgrade(config, "0012")

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0013"
        assert "fact_candidates" in inspect(connection).get_table_names()
        assert "analysis_status" in {
            column["name"]
            for column in inspect(connection).get_columns("intake_answers")
        }
    engine.dispose()


def test_migration_0013_round_trips_empty_schema(tmp_path, monkeypatch):
    """An empty deployment must support the documented 0012↔0013 rollback path."""
    database_path = tmp_path / "intake-roundtrip.db"
    config = _alembic_config(database_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")

    command.upgrade(config, "0012")
    command.upgrade(config, "0013")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        assert "fact_candidates" in inspect(connection).get_table_names()
        assert "analysis_status" in {
            column["name"]
            for column in inspect(connection).get_columns("intake_answers")
        }
    command.downgrade(config, "0012")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0012"
        assert "fact_candidates" not in inspect(connection).get_table_names()
        assert "analysis_status" not in {
            column["name"]
            for column in inspect(connection).get_columns("intake_answers")
        }
    engine.dispose()


def _alembic_config(database_path: Path) -> Config:
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


class IntakeReceiptClient:
    def __init__(self, candidates, *, status="succeeded", error_code=None):
        self.candidates = candidates
        self.status = status
        self.error_code = error_code

    async def run(self, input, cancellation=None):
        assert isinstance(input, AnalyzeIntakeRequest)
        ai_run_id = derive_ai_run_id(input.task_id, "analysis", input.input_hash)
        if cancellation is not None:
            await cancellation.register_run(ai_run_id)
        result = None
        if self.status == "succeeded":
            result = {
                "fact_candidates": self.candidates(input),
                "missing_slots": [],
                "question_candidate": {
                    "reason": "澄清本人职责",
                    "slot": "course_role",
                    "text": "你在课程项目中具体负责什么？",
                    "related_fact_refs": [],
                },
            }
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
                        "workflow_type": "analyze_intake_answer",
                        "workflow_version": "2",
                        "prompt_template_version": "intake-answer@2",
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
                                "details": {
                                    "prompt": input.payload.answer_text,
                                    "provider": "test-faux",
                                },
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
                        "exportable": False,
                        "risk_flags": [],
                    },
                    "result": result,
                },
                ensure_ascii=False,
            )
        )


class FailingIntakeClient:
    async def run(self, input, cancellation=None):
        raise RuntimeError("malformed provider response")


class MismatchedReceiptClient(IntakeReceiptClient):
    async def run(self, input, cancellation=None):
        receipt = await super().run(input, cancellation)
        return receipt.model_copy(
            update={
                "run": receipt.run.model_copy(update={"task_id": "tsk_wrong"}),
            }
        )


@pytest.fixture
def intake_analysis_app(tmp_path):
    database_path = tmp_path / "intake-analysis.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    engine = create_async_engine(database_url)

    @sqlalchemy_event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    _run(_create_schema(engine))
    _run(_seed_analysis_user(sessions))
    application = create_app(Settings(app_env="test", database_url=database_url))

    def authenticated():
        now = datetime.now(timezone.utc)
        return AuthenticatedSession(
            "usr_analysis",
            "ses_analysis",
            now,
            now + timedelta(days=1),
        )

    application.dependency_overrides[require_session] = authenticated
    with TestClient(application) as client:
        yield client, sessions, application
    _run(engine.dispose())


def test_process_answer_analysis_atomically_persists_only_valid_candidates(
    intake_analysis_app,
):
    """Malformed or duplicate model claims must not leak into candidate state."""
    client, sessions, application = intake_analysis_app
    answer_text = "我在😀课程项目中提升了50%的完成率"
    valid_slice = answer_text[3:14]
    emoji_slice = answer_text[2:7]

    def candidates(input):
        valid = {
            "kind": "result",
            "value": "课程项目提升50%",
            "source_answer_id": input.payload.answer_id,
            "source_range": {"start": 3, "end": 14},
            "source_hash": hashlib.sha256(valid_slice.encode()).hexdigest(),
            "risk_flags": [],
        }
        return [
            valid,
            {**valid},
            {
                **valid,
                "value": "😀课程项目",
                "source_range": {"start": 2, "end": 7},
                "source_hash": hashlib.sha256(emoji_slice.encode()).hexdigest(),
            },
            {**valid, "source_answer_id": "ians_wrong"},
            {**valid, "source_range": {"start": 3, "end": 99}},
            {**valid, "source_hash": "f" * 64},
            {**valid, "value": "提升5%"},
        ]

    queued = _queue_answer(client, answer_text)
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    application.state.intake_service = IntakeService(
        sessions,
        IntakeReceiptClient(candidates),
    )
    claim = _run(application.state.task_service.claim_task("usr_analysis", task_id))
    assert claim is not None

    result = _run(
        application.state.intake_service.process_answer_analysis(
            "usr_analysis",
            answer_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    assert result == answer_id
    assert _run(_candidate_values(sessions, answer_id)) == [
        ("result", "课程项目提升50%", 3, 14, "accept_or_edit"),
        ("result", "😀课程项目", 2, 7, "accept_or_edit"),
    ]
    assert _run(_analysis_state(sessions, answer_id)) == (
        "waiting_for_confirmation",
        "model",
    )
    assert _run(_session_question(sessions)) == (
        "course_role",
        "你在课程项目中具体负责什么？",
    )
    assert _run(_task_state(sessions, task_id)) == ("succeeded", answer_id)
    assert _run(_usage_state(sessions, task_id))[0] == "consumed"
    assert _run(_model_count(sessions, AiRun)) == 1
    assert _run(_model_count(sessions, AiTraceEvent)) == 7
    trace_payload = _run(_trace_payload(sessions))
    assert answer_text not in trace_payload
    assert "课程项目提升50%" not in trace_payload


def test_mixed_negative_answer_keeps_only_the_positive_source_slice(
    intake_analysis_app,
):
    """A mixed answer is substantive, but its negative clause is not a positive fact."""
    client, sessions, application = intake_analysis_app
    answer_text = "没有实习，但完成了课程项目"
    positive = answer_text[5:13]
    negative = answer_text[0:4]

    def candidates(input):
        return [
            {
                "kind": "experience",
                "value": "完成课程项目",
                "source_answer_id": input.payload.answer_id,
                "source_range": {"start": 5, "end": 13},
                "source_hash": hashlib.sha256(positive.encode()).hexdigest(),
                "risk_flags": [],
            },
            {
                "kind": "experience",
                "value": "没有实习",
                "source_answer_id": input.payload.answer_id,
                "source_range": {"start": 0, "end": 4},
                "source_hash": hashlib.sha256(negative.encode()).hexdigest(),
                "risk_flags": [],
            },
        ]

    queued = _queue_answer(client, answer_text)
    assert queued["analysis_status"] == "queued"
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    service = IntakeService(sessions, IntakeReceiptClient(candidates))
    claim = _run(application.state.task_service.claim_task("usr_analysis", task_id))
    _run(
        service.process_answer_analysis(
            "usr_analysis",
            answer_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    assert _run(_candidate_values(sessions, answer_id)) == [
        ("experience", "完成课程项目", 5, 13, "accept_or_edit")
    ]


def test_cancelled_analysis_receipt_atomically_marks_answer_and_task_failed(
    intake_analysis_app,
):
    """A cancelled receipt must remain retryable instead of stranding queued state."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    application.state.intake_service = IntakeService(
        sessions,
        IntakeReceiptClient(
            lambda _: [],
            status="cancelled",
            error_code="already_terminal",
        ),
    )
    claim = _run(application.state.task_service.claim_task("usr_analysis", task_id))
    assert claim is not None

    _run(
        application.state.intake_service.process_answer_analysis(
            "usr_analysis",
            answer_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    assert _run(_analysis_state(sessions, answer_id)) == ("failed", None)
    assert _run(_task_error(sessions, task_id)) == ("failed", "already_terminal")
    assert _run(_usage_state(sessions, task_id))[0] == "consumed"
    assert _run(_ai_run_state(sessions)) == ("cancelled", "already_terminal")


def test_pipeline_executes_typed_answer_analysis_without_a_second_completion(
    intake_analysis_app,
):
    """Missing pipeline registration would leave a correctly queued answer forever."""
    client, sessions, application = intake_analysis_app
    answer_text = "我完成了课程项目"

    def candidates(input):
        return [{
            "kind": "experience",
            "value": answer_text,
            "source_answer_id": input.payload.answer_id,
            "source_range": {"start": 0, "end": len(answer_text)},
            "source_hash": hashlib.sha256(answer_text.encode()).hexdigest(),
            "risk_flags": [],
        }]

    queued = _queue_answer(client, answer_text)
    task_id = queued["analysis_task_id"]
    configure_pipeline_operations(
        sessions,
        Settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:"),
        application.state.task_service,
        storage_override=MemoryStorage(),
        ai_client_override=IntakeReceiptClient(candidates),
    )

    result = _run(
        TaskExecutor(application.state.task_service).execute(
            "usr_analysis",
            task_id,
            resolve_operation,
        )
    )

    assert result["status"] == "succeeded"
    answer_id = _run(_answer_id(sessions, task_id))
    assert _run(_task_state(sessions, task_id)) == ("succeeded", answer_id)
    assert len(_run(_candidate_values(sessions, answer_id))) == 1


def test_pipeline_failure_marks_answer_failed_and_releases_unused_usage(
    intake_analysis_app,
):
    """An exception before a receipt must not strand the answer in running state."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    configure_pipeline_operations(
        sessions,
        Settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:"),
        application.state.task_service,
        storage_override=MemoryStorage(),
        ai_client_override=FailingIntakeClient(),
    )

    result = _run(
        TaskExecutor(application.state.task_service).execute(
            "usr_analysis",
            task_id,
            resolve_operation,
        )
    )

    answer_id = _run(_answer_id(sessions, task_id))
    assert result["status"] == "failed"
    assert _run(_analysis_state(sessions, answer_id))[0] == "failed"
    assert _run(_model_count(sessions, AiRun)) == 0
    assert _run(_usage_state(sessions, task_id))[0] == "released"


def test_pipeline_receipt_mismatch_does_not_leave_answer_running(
    intake_analysis_app,
):
    """A rejected terminal receipt must leave both Task and Answer retryable."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    configure_pipeline_operations(
        sessions,
        Settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:"),
        application.state.task_service,
        storage_override=MemoryStorage(),
        ai_client_override=MismatchedReceiptClient(lambda _: []),
    )

    result = _run(
        TaskExecutor(application.state.task_service).execute(
            "usr_analysis",
            task_id,
            resolve_operation,
        )
    )

    answer_id = _run(_answer_id(sessions, task_id))
    assert result["status"] == "failed"
    assert _run(_analysis_state(sessions, answer_id))[0] == "failed"
    assert _run(_model_count(sessions, AiRun)) == 0
    assert _run(_usage_state(sessions, task_id))[0] == "consumed"


def test_analysis_result_transaction_rolls_back_every_result_side_effect(
    intake_analysis_app,
    monkeypatch,
):
    """A terminal-write failure must roll back receipt, trace, candidates, and usage."""
    client, sessions, application = intake_analysis_app
    answer_text = "我完成了课程项目"

    def candidates(input):
        return [{
            "kind": "experience",
            "value": answer_text,
            "source_answer_id": input.payload.answer_id,
            "source_range": {"start": 0, "end": len(answer_text)},
            "source_hash": hashlib.sha256(answer_text.encode()).hexdigest(),
            "risk_flags": [],
        }]

    queued = _queue_answer(client, answer_text)
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    service = IntakeService(sessions, IntakeReceiptClient(candidates))
    claim = _run(application.state.task_service.claim_task("usr_analysis", task_id))

    async def fail_terminal_write(*_args, **_kwargs):
        raise RuntimeError("terminal write failed")

    monkeypatch.setattr(
        application.state.task_service,
        "complete_task_in_session",
        fail_terminal_write,
    )
    with pytest.raises(RuntimeError, match="terminal write failed"):
        _run(
            service.process_answer_analysis(
                "usr_analysis",
                answer_id,
                task_id=task_id,
                claim_token=claim.token,
                task_service=application.state.task_service,
            )
        )

    assert _run(_model_count(sessions, AiRun)) == 0
    assert _run(_model_count(sessions, AiTraceEvent)) == 0
    assert _run(_model_count(sessions, FactCandidate)) == 0
    assert _run(_usage_state(sessions, task_id))[0] == "reserved"
    assert _run(_task_error(sessions, task_id))[0] == "running"
    assert _run(_analysis_state(sessions, answer_id))[0] == "running"


@pytest.mark.parametrize("decision", ["accept", "edit", "reject"])
def test_candidate_decisions_create_only_the_allowed_provenance(
    intake_analysis_app,
    decision,
):
    """Accept, edit, and reject must not share evidence semantics."""
    client, sessions, application = intake_analysis_app
    answer_text = "我完成了课程项目"
    candidate_id, session_id = _analyze_one_candidate(
        client,
        sessions,
        application,
        answer_text,
    )
    payload = {"decision": decision, "base_version": 1}
    if decision == "edit":
        payload["value"] = "我独立完成课程项目"

    first = client.post(
        f"/v1/intake-sessions/{session_id}/fact-candidates/{candidate_id}/decision",
        json=payload,
        headers={"Idempotency-Key": f"decision-{decision}"},
    )
    replay = client.post(
        f"/v1/intake-sessions/{session_id}/fact-candidates/{candidate_id}/decision",
        json=payload,
        headers={"Idempotency-Key": f"decision-{decision}"},
    )

    assert first.status_code == 200
    assert replay.json() == first.json()
    body = first.json()
    assert body["candidate_id"] == candidate_id
    assert body["status"] == {
        "accept": "accepted",
        "edit": "edited",
        "reject": "rejected",
    }[decision]
    assert body["session_version"] == 2
    assert body["current_question"]["id"] == "course_role"
    expected_facts = 0 if decision == "reject" else 1
    assert _run(_model_count(sessions, Fact)) == expected_facts
    assert _run(_model_count(sessions, FactSource)) == expected_facts
    assert _run(_model_count(sessions, SourceRecord)) == expected_facts
    if decision == "accept":
        assert body["fact_summary"] == {
            "id": body["fact_summary"]["id"],
            "kind": "experience",
            "value": answer_text,
            "status": "confirmed",
        }
        assert _run(_source_details(sessions)) == (
            "question_answer",
            answer_text,
            {"start": 0, "end": len(answer_text)},
        )
    elif decision == "edit":
        assert body["fact_summary"]["value"] == "我独立完成课程项目"
        assert _run(_source_details(sessions)) == (
            "fact_candidate_edit",
            "我独立完成课程项目",
            {"start": 0, "end": len("我独立完成课程项目")},
        )
    else:
        assert body["fact_summary"] is None


def test_candidate_decision_is_owner_filtered_and_rejects_invalid_modes(
    intake_analysis_app,
):
    """A candidate cannot be decided by another owner or accepted when edit-only."""
    client, sessions, application = intake_analysis_app
    candidate_id, session_id = _analyze_one_candidate(
        client,
        sessions,
        application,
        "我完成了课程项目",
        risk_flags=["conflict"],
    )
    forbidden_mode = client.post(
        f"/v1/intake-sessions/{session_id}/fact-candidates/{candidate_id}/decision",
        json={"decision": "accept", "base_version": 1},
        headers={"Idempotency-Key": "accept-edit-only"},
    )
    assert forbidden_mode.status_code == 422
    assert _run(_model_count(sessions, Fact)) == 0

    now = datetime.now(timezone.utc)
    application.dependency_overrides[require_session] = lambda: AuthenticatedSession(
        "usr_other",
        "ses_other",
        now,
        now + timedelta(days=1),
    )
    other_owner = client.post(
        f"/v1/intake-sessions/{session_id}/fact-candidates/{candidate_id}/decision",
        json={"decision": "reject", "base_version": 1},
        headers={"Idempotency-Key": "other-owner-decision"},
    )
    assert other_owner.status_code == 404


def test_failed_analysis_retry_reuses_answer_and_replaces_unused_reservation(
    intake_analysis_app,
):
    """Retrying must not create a second answer or leave two active reservations."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    old_task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, old_task_id))
    _run(_mark_failed_unused(sessions, application, answer_id, old_task_id))

    retried = client.post(
        f"/v1/intake-sessions/{_run(_session_id(sessions))}/analysis/retry",
        json={"base_version": 1},
        headers={"Idempotency-Key": "analysis-retry"},
    )
    replay = client.post(
        f"/v1/intake-sessions/{_run(_session_id(sessions))}/analysis/retry",
        json={"base_version": 1},
        headers={"Idempotency-Key": "analysis-retry"},
    )

    assert retried.status_code == 202
    assert replay.json() == retried.json()
    new_task_id = retried.json()["analysis_task_id"]
    assert new_task_id != old_task_id
    assert _run(_model_count(sessions, IntakeAnswer)) == 1
    assert _run(_model_count(sessions, Task)) == 2
    assert _run(_usage_states(sessions)) == [
        (old_task_id, "released"),
        (new_task_id, "reserved"),
    ]
    assert _run(_answer_input(sessions, answer_id)) == (
        answer_id,
        1,
        hashlib.sha256("我完成了课程项目".encode()).hexdigest(),
        new_task_id,
        "queued",
    )


def test_analysis_continue_releases_reservation_and_advances_by_rule(
    intake_analysis_app,
):
    """Rule continuation must preserve the answer without charging unused AI work."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    _run(_mark_failed_unused(sessions, application, answer_id, task_id))
    session_id = _run(_session_id(sessions))

    continued = client.post(
        f"/v1/intake-sessions/{session_id}/analysis/continue",
        json={"base_version": 1},
        headers={"Idempotency-Key": "analysis-continue"},
    )

    assert continued.status_code == 200
    assert continued.json()["analysis_status"] == "completed"
    assert continued.json()["current_question"]["id"] == "course_role"
    assert _run(_model_count(sessions, IntakeAnswer)) == 1
    assert _run(_analysis_state(sessions, _run(_answer_id(sessions, task_id)))) == (
        "completed",
        "fallback",
    )
    assert _run(_task_error(sessions, task_id))[0] == "failed"
    assert _run(_usage_state(sessions, task_id))[0] == "released"


def test_user_retry_is_disabled_at_the_global_degraded_cost_gate(
    intake_analysis_app,
):
    """Mislabeling a user retry as a normal task would bypass the 90-yuan gate."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    _run(_mark_failed_unused(sessions, application, answer_id, task_id))
    _run(_add_global_ai_cost(sessions))

    response = client.post(
        f"/v1/intake-sessions/{_run(_session_id(sessions))}/analysis/retry",
        json={"base_version": 1},
        headers={"Idempotency-Key": "degraded-retry"},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "AI_RETRY_DISABLED"
    assert _run(_model_count(sessions, Task)) == 1
    assert _run(_usage_state(sessions, task_id))[0] == "reserved"
    assert _run(_analysis_state(sessions, answer_id))[0] == "failed"


def _run(awaitable):
    return asyncio.run(awaitable)


async def _create_schema(engine):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def _seed_analysis_user(sessions):
    async with sessions.begin() as session:
        session.add_all([User(id="usr_analysis"), User(id="usr_other")])


def _queue_answer(client, answer):
    started = client.post(
        "/v1/intake-sessions",
        json={"restart": False},
        headers={"Idempotency-Key": "analysis-start"},
    ).json()
    response = client.post(
        f"/v1/intake-sessions/{started['id']}/answers",
        json={
            "question_id": "experience_radar",
            "answer": answer,
            "skipped": False,
            "base_version": 0,
        },
        headers={"Idempotency-Key": "analysis-answer"},
    )
    assert response.status_code == 202
    return response.json()


def _analyze_one_candidate(
    client,
    sessions,
    application,
    answer_text,
    *,
    risk_flags=None,
):
    flags = risk_flags or []

    def candidates(input):
        return [{
            "kind": "experience",
            "value": answer_text,
            "source_answer_id": input.payload.answer_id,
            "source_range": {"start": 0, "end": len(answer_text)},
            "source_hash": hashlib.sha256(answer_text.encode()).hexdigest(),
            "risk_flags": flags,
        }]

    queued = _queue_answer(client, answer_text)
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    application.state.intake_service = IntakeService(
        sessions,
        IntakeReceiptClient(candidates),
    )
    claim = _run(application.state.task_service.claim_task("usr_analysis", task_id))
    _run(
        application.state.intake_service.process_answer_analysis(
            "usr_analysis",
            answer_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )
    return _run(_candidate_id(sessions, answer_id)), _run(_session_id(sessions))


async def _answer_id(sessions, task_id):
    async with sessions() as session:
        return await session.scalar(
            select(IntakeAnswer.id).where(IntakeAnswer.analysis_task_id == task_id)
        )


async def _candidate_values(sessions, answer_id):
    async with sessions() as session:
        rows = await session.execute(
            select(
                FactCandidate.kind,
                FactCandidate.value_encrypted,
                FactCandidate.source_start,
                FactCandidate.source_end,
                FactCandidate.decision_mode,
            )
            .where(FactCandidate.intake_answer_id == answer_id)
            .order_by(FactCandidate.source_start.desc())
        )
        return list(rows.tuples())


async def _candidate_id(sessions, answer_id):
    async with sessions() as session:
        return await session.scalar(
            select(FactCandidate.id).where(
                FactCandidate.intake_answer_id == answer_id
            )
        )


async def _session_id(sessions):
    async with sessions() as session:
        return await session.scalar(select(IntakeSession.id))


async def _analysis_state(sessions, answer_id):
    async with sessions() as session:
        row = await session.scalar(select(IntakeAnswer).where(IntakeAnswer.id == answer_id))
        assert row is not None
        return row.analysis_status, row.next_question_source


async def _session_question(sessions):
    async with sessions() as session:
        question = await session.scalar(select(IntakeSession.current_question))
        return question["id"], question["prompt"]


async def _task_state(sessions, task_id):
    async with sessions() as session:
        task = await session.scalar(select(Task).where(Task.id == task_id))
        return task.status, task.result_ref


async def _task_error(sessions, task_id):
    async with sessions() as session:
        task = await session.scalar(select(Task).where(Task.id == task_id))
        return task.status, task.error_code


async def _usage_state(sessions, task_id):
    async with sessions() as session:
        row = await session.scalar(
            select(UsageLedger).where(UsageLedger.task_id == task_id)
        )
        return row.state, row.ai_run_id


async def _model_count(sessions, model):
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _ai_run_state(sessions):
    async with sessions() as session:
        row = await session.scalar(select(AiRun))
        return row.status, row.error_code


async def _trace_payload(sessions):
    async with sessions() as session:
        values = (
            await session.scalars(
                select(AiTraceEvent.payload).order_by(AiTraceEvent.event_seq)
            )
        ).all()
        return json.dumps(values, ensure_ascii=False)


async def _source_details(sessions):
    async with sessions() as session:
        row = await session.execute(
            select(
                SourceRecord.source_type,
                SourceRecord.content_encrypted,
                FactSource.source_range,
            ).join(
                FactSource,
                FactSource.source_record_id == SourceRecord.id,
            )
        )
        return row.one()


async def _mark_failed_unused(sessions, application, answer_id, task_id):
    claim = await application.state.task_service.claim_task("usr_analysis", task_id)
    await application.state.task_service.fail_task(
        "usr_analysis",
        task_id,
        claim.token,
        "provider_unavailable",
    )
    async with sessions.begin() as session:
        answer = await session.scalar(select(IntakeAnswer).where(IntakeAnswer.id == answer_id))
        answer.analysis_status = "failed"


async def _usage_states(sessions):
    async with sessions() as session:
        rows = await session.execute(
            select(UsageLedger.task_id, UsageLedger.state).order_by(
                UsageLedger.created_at,
                UsageLedger.id,
            )
        )
        return list(rows.tuples())


async def _answer_input(sessions, answer_id):
    async with sessions() as session:
        row = await session.scalar(select(IntakeAnswer).where(IntakeAnswer.id == answer_id))
        return (
            row.id,
            row.analysis_input_version,
            row.analysis_input_hash,
            row.analysis_task_id,
            row.analysis_status,
        )


async def _add_global_ai_cost(sessions):
    now = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        session.add(
            UsageLedger(
                id="usg_global_cost",
                owner_user_id="usr_other",
                usage_type="ai_task",
                quantity=0,
                cost_cny=Decimal("90"),
                trace_id="tr_global_cost",
                state="consumed",
                ai_run_id="run_global_cost",
                created_at=now,
                updated_at=now,
            )
        )
