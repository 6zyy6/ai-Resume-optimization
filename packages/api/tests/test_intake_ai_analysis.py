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
    Outbox,
    SourceRecord,
    Task,
    TaskEvent,
    UsageLedger,
    User,
)
from app.integrations.ai_client import (
    AiExecutionReceipt,
    AnalyzeIntakeRequest,
    AnalyzeIntakeResult,
    FactCandidate as AiFactCandidate,
    derive_ai_run_id,
)
from app.main import create_app
from app.integrations.storage import MemoryStorage
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.intake.service import (
    IntakeError,
    IntakeService,
    _validated_candidates,
)
from app.modules.tasks.service import TaskAdmission
from app.workers.dispatcher import OutboxDispatcher, TaskQueueBusy
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


class FailingPublisher:
    def publish(self, task_id, owner_user_id, queue):
        raise ConnectionError("broker unavailable")


class MismatchedReceiptClient(IntakeReceiptClient):
    async def run(self, input, cancellation=None):
        receipt = await super().run(input, cancellation)
        return receipt.model_copy(
            update={
                "run": receipt.run.model_copy(update={"task_id": "tsk_wrong"}),
            }
        )


class CancellingIntakeReceiptClient:
    def __init__(self, task_service, *, stale_receipt=False):
        self.task_service = task_service
        self.stale_receipt = stale_receipt

    async def run(self, input, cancellation=None):
        ai_run_id = derive_ai_run_id(input.task_id, "analysis", input.input_hash)
        assert cancellation is not None
        assert await cancellation.register_run(ai_run_id) is True
        await self.task_service.request_cancel("usr_analysis", input.task_id)
        assert await cancellation.is_cancel_requested() is True
        await cancellation.acknowledge_cancel(ai_run_id)
        receipt = await IntakeReceiptClient(
            lambda _: [],
            status="cancelled",
            error_code=None,
        ).run(input)
        if self.stale_receipt:
            return receipt.model_copy(
                update={
                    "run": receipt.run.model_copy(
                        update={"ai_run_id": "airun_stale"}
                    )
                }
            )
        return receipt


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
            "value": valid_slice,
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
        ("result", valid_slice, 3, 14, "accept_or_edit"),
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
    assert valid_slice not in trace_payload


def test_mixed_negative_answer_keeps_only_the_positive_source_slice(
    intake_analysis_app,
):
    """A mixed answer is substantive, but its negative clause is not a positive fact."""
    client, sessions, application = intake_analysis_app
    answer_text = "没有实习，但完成了课程项目"
    positive = answer_text[6:13]
    negative = answer_text[0:4]

    def candidates(input):
        return [
            {
                "kind": "experience",
                "value": positive,
                "source_answer_id": input.payload.answer_id,
                "source_range": {"start": 6, "end": 13},
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
            {
                "kind": "experience",
                "value": answer_text,
                "source_answer_id": input.payload.answer_id,
                "source_range": {"start": 0, "end": len(answer_text)},
                "source_hash": hashlib.sha256(answer_text.encode()).hexdigest(),
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
        ("experience", positive, 6, 13, "accept_or_edit")
    ]


@pytest.mark.parametrize(
    "negative_slice",
    [
        "并没有负责项目",
        "从未负责项目",
        "未参与这个项目",
        "并未负责项目",
        "不曾参与项目",
        "完全没有参与项目",
        "没有参与过项目",
        "没负责项目",
        "未能完成项目",
    ],
)
def test_negative_candidate_slices_never_become_positive_candidates(
    intake_analysis_app,
    negative_slice,
):
    """An exact negative source span must never become a positive fact."""
    client, sessions, application = intake_analysis_app
    answer_text = f"补充说明：{negative_slice}，但完成了汇报"
    start = answer_text.index(negative_slice)
    end = start + len(negative_slice)

    def candidates(input):
        return [{
            "kind": "role",
            "value": negative_slice,
            "source_answer_id": input.payload.answer_id,
            "source_range": {"start": start, "end": end},
            "source_hash": hashlib.sha256(negative_slice.encode()).hexdigest(),
            "risk_flags": [],
        }]

    queued = _queue_answer(client, answer_text)
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

    assert _run(_model_count(sessions, FactCandidate)) == 0
    assert "negative_source" in _run(_trace_payload(sessions))


@pytest.mark.parametrize(
    ("negative_slice", "value"),
    [
        ("没有实际负责", "实际负责"),
        ("不负责", "负责"),
        ("没有做", "做"),
        ("并未实际参与", "实际参与"),
    ],
)
def test_any_explicit_negative_marker_blocks_automatic_candidates(
    intake_analysis_app,
    negative_slice,
    value,
):
    """Validation must not depend on an enumerated list of negated verbs."""
    client, sessions, application = intake_analysis_app
    answer_text = f"补充说明：{negative_slice}，但完成了课程项目"
    start = answer_text.index(negative_slice)
    candidate_start = answer_text.index(value, start)
    candidate_end = candidate_start + len(value)
    candidate_slice = answer_text[candidate_start:candidate_end]

    def candidates(input):
        return [{
            "kind": "role",
            "value": value,
            "source_answer_id": input.payload.answer_id,
            "source_range": {"start": candidate_start, "end": candidate_end},
            "source_hash": hashlib.sha256(candidate_slice.encode()).hexdigest(),
            "risk_flags": [],
        }]

    queued = _queue_answer(client, answer_text)
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

    assert _run(_model_count(sessions, FactCandidate)) == 0
    assert "negative_source" in _run(_trace_payload(sessions))


@pytest.mark.parametrize(
    "term",
    [
        "无人机研发",
        "无障碍设计",
        "未来规划",
        "未成年人服务",
        "不断优化流程",
        "不同方案比较",
        "沉没成本分析",
        "无损检测",
        "无监督学习",
        "无服务器架构",
        "未登录用户处理",
        "不稳定网络优化",
        "不锈钢检测",
        "无锡志愿活动",
        "未央区调研",
        "不间断服务",
    ],
)
def test_negative_marker_inside_candidate_lexeme_is_not_rejected(term):
    answer = f"我完成了{term}"
    start = answer.index(term)
    valid, invalid = _validate_candidate_slice(
        answer,
        term,
        start=start,
        end=start + len(term),
    )

    assert len(valid) == 1
    assert invalid == []


@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        ("没有负责项目", "负责项目"),
        ("并未实际参与项目", "参与项目"),
        ("我不负责项目", "负责项目"),
        ("我无实际项目经验", "项目经验"),
        ("我未获得奖项", "获得奖项"),
    ],
)
def test_source_range_cannot_crop_a_negative_prefix(answer, evidence):
    start = answer.index(evidence)

    valid, invalid = _validate_candidate_slice(
        answer,
        evidence,
        start=start,
        end=start + len(evidence),
    )

    assert valid == []
    assert invalid == [(0, "negative_source")]


@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        ("我从未在任何课程社团或实习中真正独立直接负责项目", "负责项目"),
        ("我未曾以任何形式真正负责项目", "负责项目"),
        ("我不再以任何身份继续负责项目", "负责项目"),
        ("I did not independently lead the project", "lead the project"),
        ("I never independently owned delivery", "owned delivery"),
        ("I wasn't directly responsible", "responsible"),
    ],
)
def test_same_clause_negation_has_no_distance_or_language_bypass(answer, evidence):
    start = answer.index(evidence)

    valid, invalid = _validate_candidate_slice(
        answer,
        evidence,
        start=start,
        end=start + len(evidence),
    )

    assert valid == []
    assert invalid == [(0, "negative_source")]


@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        ("无项目经验", "无项目经验"),
        ("不负责项目", "不负责项目"),
        ("不具备项目经验", "不具备项目经验"),
        ("不熟悉Python", "不熟悉Python"),
        ("项目不是我负责的", "负责"),
        ("并非我负责项目", "负责项目"),
        ("否认负责项目", "负责项目"),
        ("无相关项目经验", "项目经验"),
        ("没实际负责项目", "负责项目"),
        ("不直接负责项目", "负责项目"),
        ("未直接负责项目", "负责项目"),
        ("我没有相关经验：负责项目", "负责项目"),
        ("没\u200b有负责项目", "负责项目"),
        ("我不确定是否完成项目", "完成项目"),
        ("我不清楚是否完成项目", "完成项目"),
        ("我不知道是否完成项目", "完成项目"),
        ("I don't lead the project", "lead the project"),
        ("I don’t lead the project", "lead the project"),
        ("I dont lead the project", "lead the project"),
        ("I didn’t lead the project", "lead the project"),
    ],
)
def test_negative_or_uncertain_context_cannot_publish_exact_span(answer, evidence):
    start = answer.index(evidence)

    valid, invalid = _validate_candidate_slice(
        answer,
        evidence,
        start=start,
        end=start + len(evidence),
    )

    assert valid == []
    assert invalid == [(0, "negative_source")]


@pytest.mark.parametrize(
    "answer",
    [
        "负责项目，但这并不是我的职责",
        "负责项目，这不是我的职责",
        "负责项目，并非由我完成",
        "负责项目，后续工作与我无关",
        "负责项目，但不是我做的",
        "负责项目，并非本人完成",
        "负责项目。并不是我的职责",
    ],
)
def test_same_sentence_responsibility_disclaimer_rejects_candidate(answer):
    evidence = "负责项目"

    valid, invalid = _validate_candidate_slice(
        answer,
        evidence,
        start=0,
        end=len(evidence),
    )

    assert valid == []
    assert invalid == [(0, "negative_source")]


def test_positive_span_after_adversative_is_not_tainted_by_prior_negation():
    answer = "没有实习，但完成课程项目"
    evidence = "完成课程项目"
    start = answer.index(evidence)

    valid, invalid = _validate_candidate_slice(
        answer,
        evidence,
        start=start,
        end=start + len(evidence),
    )

    assert len(valid) == 1
    assert invalid == []


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("完成课程项目", "独立完成课程项目"),
        ("完成课程项目", "完成课程项目并负责汇报"),
        ("完成课程项目", "完成高质量课程项目"),
        ("完成课程项目", "完成课程项目获得好评"),
        ("熟悉Python", "熟悉Java"),
        ("掌握SQL", "掌握AWS"),
        ("下降-5%", "下降5%"),
        ("服务兩人", "服务二人"),
        ("增长双倍", "增长二倍"),
        ("排名Ⅱ", "排名二"),
    ],
)
def test_candidate_value_must_equal_its_evidence_slice(source, value):
    valid, invalid = _validate_candidate_slice(source, value)

    assert valid == []
    assert invalid == [(0, "source_value_mismatch")]


def test_edit_only_candidate_cannot_bypass_exact_evidence():
    valid, invalid = _validate_candidate_slice(
        "负责课程项目",
        "独立负责课程项目",
        risk_flags=("conflict",),
    )

    assert valid == []
    assert invalid == [(0, "source_value_mismatch")]


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("  完成   项目 ", "完成 项目"),
        ("熟悉Cafe\u0301", "熟悉Café"),
    ],
)
def test_evidence_comparison_allows_whitespace_and_canonical_unicode(source, value):
    valid, invalid = _validate_candidate_slice(source, value)

    assert len(valid) == 1
    assert invalid == []


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("结果10⁵", "结果105"),
        ("结果2³", "结果23"),
        ("结果10⁻⁵", "结果10−5"),
        ("完成项目？", "完成项目"),
        ("完成项目。", "完成项目"),
        ("熟悉Ｐｙｔｈｏｎ", "熟悉Python"),
    ],
)
def test_evidence_comparison_rejects_compatibility_or_punctuation_rewrites(
    source,
    value,
):
    valid, invalid = _validate_candidate_slice(source, value)

    assert valid == []
    assert invalid == [(0, "source_value_mismatch")]


def test_candidate_without_exact_source_value_is_rejected():
    valid, invalid = _validate_candidate_slice(
        "我完成了课程项目",
        "负责用户调研",
    )

    assert valid == []
    assert invalid == [(0, "source_value_mismatch")]


def test_negation_alignment_preserves_4900_direct_negative_combinations():
    markers = (
        "没有",
        "并没有",
        "完全没有",
        "从未",
        "不曾",
        "并未",
        "未能",
        "没",
        "不",
        "无",
    )
    modifiers = ("", "实际", "真正", "直接", "独立", "具体", "主动")
    actions = ("负责", "参与", "完成", "承担", "组织", "主导", "获得")
    objects = (
        "项目",
        "任务",
        "工作",
        "实习",
        "活动",
        "课程",
        "比赛",
        "调研",
        "设计",
        "汇报",
    )
    rejected = 0

    for marker in markers:
        for modifier in modifiers:
            for action in actions:
                for object_name in objects:
                    candidate = f"{modifier}{action}{object_name}"
                    valid, _ = _validate_candidate_slice(
                        f"{marker}{candidate}",
                        candidate,
                        start=len(marker),
                        end=len(marker) + len(candidate),
                    )
                    rejected += not valid

    assert rejected == 4_900


@pytest.mark.parametrize(
    ("source", "value", "expected_count"),
    [
        ("服务了1,000人", "服务了1000人", 0),
        ("提升.5%", "提升.5%", 1),
        ("下降-5%", "下降5%", 0),
        ("提升5%", "提升.5%", 0),
    ],
)
def test_numeric_candidate_value_requires_exact_source_evidence(
    intake_analysis_app,
    source,
    value,
    expected_count,
):
    """Loose digit matching must not merge signs, decimals, or thousands."""
    client, sessions, application = intake_analysis_app

    def candidates(input):
        return [{
            "kind": "result",
            "value": value,
            "source_answer_id": input.payload.answer_id,
            "source_range": {"start": 0, "end": len(source)},
            "source_hash": hashlib.sha256(source.encode()).hexdigest(),
            "risk_flags": [],
        }]

    queued = _queue_answer(client, source)
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

    assert _run(_model_count(sessions, FactCandidate)) == expected_count
    if expected_count == 0:
        assert "source_value_mismatch" in _run(_trace_payload(sessions))


@pytest.mark.parametrize(
    ("source", "value", "expected_count"),
    [
        ("完成十项任务", "完成十项任务", 1),
        ("完成十项任务", "完成二十项任务", 0),
        ("获得第二名", "获得第二名", 1),
        ("获得第二名", "获得第一名", 0),
        ("提升百分之五", "提升百分之五", 1),
        ("提升百分之五", "提升百分之十", 0),
        ("服务十人", "服务二十人", 0),
        ("连续三个月", "连续三周", 0),
        ("增长十倍", "增长十倍", 1),
        ("增长十倍", "增长二十倍", 0),
        ("进入前十", "进入前十", 1),
        ("进入前十", "进入前三", 0),
        ("月薪一万", "月薪一万", 1),
        ("月薪一万", "月薪两万", 0),
        ("服务十余人", "服务十余人", 1),
        ("服务十余人", "服务二十余人", 0),
        ("GPA四点五", "GPA四点五", 1),
        ("GPA四点五", "GPA三点五", 0),
        ("月薪两万", "月薪二万", 0),
        ("服务两人", "服务二人", 0),
        ("服务一〇人", "服务一零人", 0),
        ("服务十多人", "服务二十多人", 0),
        ("增长三成", "增长五成", 0),
        ("提升五％", "提升十％", 0),
        ("一直负责项目", "一直负责项目", 1),
        ("唯一负责项目", "唯一负责项目", 1),
        ("一直负责项目", "负责十人", 0),
        ("唯一负责项目", "负责二人", 0),
        ("服务俩人", "服务双人", 0),
        ("增长双倍", "增长俩倍", 0),
        ("完成过半", "完成过半且增长双倍", 0),
        ("投入半天", "投入半天且服务俩人", 0),
        ("完成廿项", "完成卅项", 0),
        ("月薪壹萬", "月薪贰萬", 0),
        ("服务俩人", "服务俩人", 1),
        ("增长双倍", "增长双倍", 1),
        ("完成廿项", "完成二十项", 0),
        ("完成卅项", "完成三十项", 0),
        ("月薪壹萬", "月薪一万", 0),
        ("负责团队协作", "服务几人", 0),
        ("负责团队协作", "提升百分之几", 0),
        ("负责团队协作", "获得第几名", 0),
        ("负责团队协作", "完成若干项", 0),
        ("负责团队协作", "服务数人", 0),
        ("完成数据分析", "完成数据分析", 1),
        ("推动数字化项目", "推动数字化项目", 1),
    ],
)
def test_chinese_numeric_value_requires_exact_source_evidence(
    intake_analysis_app,
    source,
    value,
    expected_count,
):
    client, sessions, application = intake_analysis_app

    def candidates(input):
        return [{
            "kind": "result",
            "value": value,
            "source_answer_id": input.payload.answer_id,
            "source_range": {"start": 0, "end": len(source)},
            "source_hash": hashlib.sha256(source.encode()).hexdigest(),
            "risk_flags": [],
        }]

    queued = _queue_answer(client, source)
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

    assert _run(_model_count(sessions, FactCandidate)) == expected_count
    if expected_count == 0:
        assert "source_value_mismatch" in _run(_trace_payload(sessions))


def test_simplified_chinese_numeric_fuzz_rejects_1920_value_mismatches():
    numbers = (
        "零",
        "一",
        "二",
        "三",
        "四",
        "五",
        "六",
        "七",
        "八",
        "九",
        "十",
        "二十",
        "三十",
        "百",
        "千",
        "万",
    )
    suffixes = (
        "人",
        "名",
        "次",
        "项",
        "个",
        "天",
        "周",
        "月",
        "年",
        "份",
        "家",
        "倍",
    )
    stems = (
        "服务",
        "完成",
        "组织",
        "支持",
        "覆盖",
        "持续",
        "增长",
        "减少",
        "交付",
        "获得",
    )
    mismatches = 0

    for index, number in enumerate(numbers):
        other = numbers[(index + 1) % len(numbers)]
        for suffix in suffixes:
            for stem in stems:
                valid, invalid = _validate_candidate_slice(
                    f"{stem}{number}{suffix}",
                    f"{stem}{other}{suffix}",
                )
                mismatches += not valid and invalid == [
                    (0, "source_value_mismatch")
                ]

    assert mismatches == 1_920


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


def test_real_cancel_after_claim_clear_persists_receipt_without_second_terminal_write(
    intake_analysis_app,
):
    """Claim acknowledgement must not make the final cancelled receipt unpersistable."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    configure_pipeline_operations(
        sessions,
        Settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:"),
        application.state.task_service,
        storage_override=MemoryStorage(),
        ai_client_override=CancellingIntakeReceiptClient(
            application.state.task_service
        ),
    )

    result = _run(
        TaskExecutor(application.state.task_service).execute(
            "usr_analysis",
            task_id,
            resolve_operation,
        )
    )

    answer_id = _run(_answer_id(sessions, task_id))
    assert result["status"] == "cancelled"
    assert _run(_analysis_state(sessions, answer_id))[0] == "failed"
    assert _run(_ai_run_state(sessions)) == ("cancelled", None)
    assert _run(_usage_state(sessions, task_id))[0] == "consumed"
    assert _run(_cancelled_task_state(sessions, task_id)) == (
        "cancelled",
        True,
        None,
        derive_ai_run_id(
            task_id,
            "analysis",
            _run(_outbox_payload(sessions, task_id))["analysis_input_hash"],
        ),
    )
    assert _run(_model_count(sessions, FactCandidate)) == 0


def test_cancelled_task_rejects_a_receipt_from_a_different_run(
    intake_analysis_app,
):
    """Cancelled-task recovery must remain bound to its registered active run."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    configure_pipeline_operations(
        sessions,
        Settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:"),
        application.state.task_service,
        storage_override=MemoryStorage(),
        ai_client_override=CancellingIntakeReceiptClient(
            application.state.task_service,
            stale_receipt=True,
        ),
    )

    result = _run(
        TaskExecutor(application.state.task_service).execute(
            "usr_analysis",
            task_id,
            resolve_operation,
        )
    )

    assert result["status"] == "cancelled"
    assert _run(_model_count(sessions, AiRun)) == 0
    assert _run(_model_count(sessions, FactCandidate)) == 0


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
    assert "analysis_snapshot" not in _run(_outbox_payload(sessions, task_id))


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


@pytest.mark.parametrize("recovery", ["retry", "continue", "restart"])
def test_outbox_exhaustion_atomically_unblocks_intake_recovery(
    intake_analysis_app,
    recovery,
):
    """A never-published task must not strand the answer or its reservation."""
    client, sessions, _ = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    session_id = _run(_session_id(sessions))
    dispatcher = OutboxDispatcher(
        sessions,
        FailingPublisher(),
        retry_base_seconds=0,
        jitter=lambda: 0,
    )

    for _ in range(3):
        with pytest.raises(TaskQueueBusy):
            _run(dispatcher.dispatch_task(task_id))

    assert _run(_task_error(sessions, task_id)) == (
        "failed",
        "TASK_QUEUE_UNAVAILABLE",
    )
    assert _run(_analysis_state(sessions, answer_id))[0] == "failed"
    assert _run(_usage_state(sessions, task_id)) == ("released", None)
    assert _run(_outbox_state(sessions, task_id)) == (3, True)
    assert "analysis_snapshot" in _run(_outbox_payload(sessions, task_id))
    assert _run(_task_event_stages(sessions, task_id))[-1] == "failed"
    assert client.get(
        f"/v1/intake-sessions/{session_id}"
    ).json()["analysis_status"] == "failed"

    if recovery == "retry":
        recovered = client.post(
            f"/v1/intake-sessions/{session_id}/analysis/retry",
            json={"base_version": 1},
            headers={"Idempotency-Key": "exhausted-analysis-retry"},
        )
        assert recovered.status_code == 202
    elif recovery == "continue":
        recovered = client.post(
            f"/v1/intake-sessions/{session_id}/analysis/continue",
            json={"base_version": 1},
            headers={"Idempotency-Key": "exhausted-analysis-continue"},
        )
        assert recovered.status_code == 200
        assert recovered.json()["analysis_status"] == "completed"
    else:
        recovered = client.post(
            "/v1/intake-sessions",
            json={"restart": True},
            headers={"Idempotency-Key": "exhausted-analysis-restart"},
        )
        assert recovered.status_code == 201
        assert recovered.json()["id"] != session_id
    assert "analysis_snapshot" not in _run(_outbox_payload(sessions, task_id))


def test_non_intake_outbox_exhaustion_only_releases_its_own_reservation(
    intake_analysis_app,
):
    """Generic exhaustion must not update an unrelated IntakeAnswer graph."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    intake_task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, intake_task_id))
    generic = _run(
        application.state.task_service.create_task(
            "usr_analysis",
            task_type="resume_optimize",
            queue="ai.batch",
            trace_id="tr_generic_exhaustion",
            idempotency_key="generic-exhaustion",
            admission=TaskAdmission.ai(),
            resource_type="resume",
            resource_id=answer_id,
        )
    )
    dispatcher = OutboxDispatcher(
        sessions,
        FailingPublisher(),
        retry_base_seconds=0,
        jitter=lambda: 0,
    )

    for _ in range(3):
        with pytest.raises(TaskQueueBusy):
            _run(dispatcher.dispatch_task(generic.id))

    assert _run(_task_error(sessions, generic.id)) == (
        "failed",
        "TASK_QUEUE_UNAVAILABLE",
    )
    assert _run(_usage_state(sessions, generic.id)) == ("released", None)
    assert _run(_analysis_state(sessions, answer_id))[0] == "queued"
    assert _run(_usage_state(sessions, intake_task_id)) == ("reserved", None)


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


def test_session_projects_candidates_and_blocks_answers_until_all_are_decided(
    intake_analysis_app,
):
    """Removing candidate projection or the waiting gate would strand or bypass review."""
    client, sessions, application = intake_analysis_app
    answer_text = "我完成课程项目并负责展示"
    queued = _queue_answer(client, answer_text)
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))

    def candidates(input):
        return [
            {
                "kind": "experience",
                "value": "完成课程项目",
                "source_answer_id": input.payload.answer_id,
                "source_range": {"start": 1, "end": 7},
                "source_hash": "6abe8b47ad04032eae5f3a495688705b6bf33ef4a55ec11c8a3f769dd74d0b83",
                "risk_flags": [],
            },
            {
                "kind": "role",
                "value": "负责展示",
                "source_answer_id": input.payload.answer_id,
                "source_range": {"start": 8, "end": 12},
                "source_hash": "a4f47f8f25ea715c522ab0c3f0643047edbfd74ed39649b76b44d6018d10aa19",
                "risk_flags": ["conflict"],
            },
        ]

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
    session_id = _run(_session_id(sessions))

    fetched = client.get(f"/v1/intake-sessions/{session_id}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["analysis_status"] == "waiting_for_confirmation"
    assert body["current_question"] is None
    assert body["fact_candidates"] == [
        {
            "id": body["fact_candidates"][0]["id"],
            "intake_answer_id": answer_id,
            "kind": "experience",
            "value": "完成课程项目",
            "source_excerpt": "完成课程项目",
            "source_start": 1,
            "source_end": 7,
            "source_hash": "6abe8b47ad04032eae5f3a495688705b6bf33ef4a55ec11c8a3f769dd74d0b83",
            "status": "pending",
            "decision_mode": "accept_or_edit",
            "ai_run_id": body["fact_candidates"][0]["ai_run_id"],
        },
        {
            "id": body["fact_candidates"][1]["id"],
            "intake_answer_id": answer_id,
            "kind": "role",
            "value": "负责展示",
            "source_excerpt": "负责展示",
            "source_start": 8,
            "source_end": 12,
            "source_hash": "a4f47f8f25ea715c522ab0c3f0643047edbfd74ed39649b76b44d6018d10aa19",
            "status": "pending",
            "decision_mode": "edit_only",
            "ai_run_id": body["fact_candidates"][1]["ai_run_id"],
        },
    ]

    blocked = client.post(
        f"/v1/intake-sessions/{session_id}/answers",
        json={
            "question_id": "course_role",
            "answer": "我负责展示",
            "skipped": False,
            "base_version": 1,
        },
        headers={"Idempotency-Key": "answer-before-review"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "INTAKE_FACT_REVIEW_REQUIRED"

    first = client.post(
        f"/v1/intake-sessions/{session_id}/fact-candidates/"
        f"{body['fact_candidates'][0]['id']}/decision",
        json={"decision": "reject", "base_version": 1},
        headers={"Idempotency-Key": "reject-first-candidate"},
    )
    assert first.status_code == 200
    assert first.json()["current_question"] is None

    second = client.post(
        f"/v1/intake-sessions/{session_id}/fact-candidates/"
        f"{body['fact_candidates'][1]['id']}/decision",
        json={"decision": "reject", "base_version": 2},
        headers={"Idempotency-Key": "reject-second-candidate"},
    )
    assert second.status_code == 200
    assert second.json()["current_question"]["id"] == "course_role"
    terminal = client.get(f"/v1/intake-sessions/{session_id}").json()
    assert [candidate["status"] for candidate in terminal["fact_candidates"]] == [
        "rejected",
        "rejected",
    ]


@pytest.mark.parametrize("analysis_state", ["queued", "waiting_for_confirmation"])
def test_restart_rejects_sessions_with_live_answer_analysis(
    intake_analysis_app,
    analysis_state,
):
    """Restart must not abandon a session while its analysis can still publish results."""
    client, sessions, application = intake_analysis_app
    if analysis_state == "queued":
        _queue_answer(client, "我完成了课程项目")
        session_id = _run(_session_id(sessions))
    else:
        _, session_id = _analyze_one_candidate(
            client,
            sessions,
            application,
            "我完成了课程项目",
        )

    response = client.post(
        "/v1/intake-sessions",
        json={"restart": True},
        headers={"Idempotency-Key": f"restart-{analysis_state}"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INTAKE_ANALYSIS_IN_PROGRESS"
    assert client.get(f"/v1/intake-sessions/{session_id}").json()["status"] == "active"


def test_abandoned_or_nonwaiting_analysis_cannot_publish_candidates_or_facts(
    intake_analysis_app,
):
    """Missing lifecycle gates would let an old worker or stale review mutate history."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    session_id = _run(_session_id(sessions))
    claim = _run(application.state.task_service.claim_task("usr_analysis", task_id))
    _run(_set_session_status(sessions, session_id, "abandoned"))
    service = IntakeService(
        sessions,
        IntakeReceiptClient(lambda input: [{
            "kind": "experience",
            "value": "我完成了课程项目",
            "source_answer_id": input.payload.answer_id,
            "source_range": {"start": 0, "end": 8},
            "source_hash": hashlib.sha256("我完成了课程项目".encode()).hexdigest(),
            "risk_flags": [],
        }]),
    )

    with pytest.raises(IntakeError, match="resources do not match"):
        _run(
            service.process_answer_analysis(
                "usr_analysis",
                answer_id,
                task_id=task_id,
                claim_token=claim.token,
                task_service=application.state.task_service,
            )
        )
    assert _run(_model_count(sessions, FactCandidate)) == 0

    _run(_set_session_status(sessions, session_id, "active"))
    _run(
        service.process_answer_analysis(
            "usr_analysis",
            answer_id,
            task_id=task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )
    candidate_id = _run(_candidate_id(sessions, answer_id))
    _run(_set_session_status(sessions, session_id, "abandoned"))
    abandoned = client.post(
        f"/v1/intake-sessions/{session_id}/fact-candidates/{candidate_id}/decision",
        json={"decision": "accept", "base_version": 1},
        headers={"Idempotency-Key": "abandoned-candidate"},
    )
    assert abandoned.status_code == 409
    assert abandoned.json()["error"]["code"] == "INTAKE_NOT_ACTIVE"
    assert _run(_model_count(sessions, Fact)) == 0

    _run(_set_session_status(sessions, session_id, "active"))
    _run(_set_answer_analysis_status(sessions, answer_id, "completed"))
    nonwaiting = client.post(
        f"/v1/intake-sessions/{session_id}/fact-candidates/{candidate_id}/decision",
        json={"decision": "accept", "base_version": 1},
        headers={"Idempotency-Key": "nonwaiting-candidate"},
    )
    assert nonwaiting.status_code == 409
    assert nonwaiting.json()["error"]["code"] == "INTAKE_FACT_REVIEW_NOT_ACTIVE"
    assert _run(_model_count(sessions, Fact)) == 0


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


def test_draft_rejects_pending_fact_review_without_mutating_intake(
    intake_analysis_app,
):
    """Confirmed facts must not let draft generation bypass a pending review."""
    client, sessions, application = intake_analysis_app
    candidate_id, session_id = _analyze_one_candidate(
        client,
        sessions,
        application,
        "我完成了课程项目",
    )
    _run(
        _seed_confirmed_intake_fact(
            sessions,
            fact_id="fact_ready_one",
            value="我持续完成课程项目",
        )
    )
    _run(
        _seed_confirmed_intake_fact(
            sessions,
            fact_id="fact_ready_two",
            value="我负责用户调研",
        )
    )
    task_count = _run(_model_count(sessions, Task))
    usage_count = _run(_model_count(sessions, UsageLedger))
    session_state = _run(_intake_state(sessions, session_id))

    blocked = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={"base_version": 1, "title": "不应生成"},
        headers={"Idempotency-Key": "draft-before-candidate-review"},
    )

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "INTAKE_FACT_REVIEW_REQUIRED"
    assert _run(_model_count(sessions, Task)) == task_count
    assert _run(_model_count(sessions, UsageLedger)) == usage_count
    assert _run(_intake_state(sessions, session_id)) == session_state
    assert _run(_candidate_status(sessions, candidate_id)) == "pending"

    decided = client.post(
        f"/v1/intake-sessions/{session_id}/fact-candidates/"
        f"{candidate_id}/decision",
        json={"decision": "reject", "base_version": 1},
        headers={"Idempotency-Key": "review-after-draft-block"},
    )
    assert decided.status_code == 200
    assert _run(_candidate_status(sessions, candidate_id)) == "rejected"


@pytest.mark.parametrize(
    "analysis_status",
    ["queued", "running", "waiting_for_confirmation", "failed"],
)
def test_draft_rejects_unresolved_latest_answer_analysis(
    intake_analysis_app,
    analysis_status,
):
    client, sessions, _ = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    answer_id = _run(_answer_id(sessions, queued["analysis_task_id"]))
    session_id = _run(_session_id(sessions))
    _run(_set_answer_analysis_status(sessions, answer_id, analysis_status))
    _run(
        _seed_confirmed_intake_fact(
            sessions,
            fact_id="fact_ready_one",
            value="我持续完成课程项目",
        )
    )
    _run(
        _seed_confirmed_intake_fact(
            sessions,
            fact_id="fact_ready_two",
            value="我负责用户调研",
        )
    )
    before = (
        _run(_intake_state(sessions, session_id)),
        _run(_model_count(sessions, Task)),
        _run(_model_count(sessions, UsageLedger)),
    )

    blocked = client.post(
        f"/v1/intake-sessions/{session_id}/drafts",
        json={"base_version": 1, "title": "不应生成"},
        headers={"Idempotency-Key": f"draft-{analysis_status}"},
    )

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "INTAKE_ANALYSIS_NOT_READY"
    assert (
        _run(_intake_state(sessions, session_id)),
        _run(_model_count(sessions, Task)),
        _run(_model_count(sessions, UsageLedger)),
    ) == before


def test_failed_analysis_retry_reuses_answer_and_replaces_unused_reservation(
    intake_analysis_app,
):
    """Retrying must not create a second answer or leave two active reservations."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    old_task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, old_task_id))
    analysis_input_hash = _run(_outbox_payload(sessions, old_task_id))[
        "analysis_input_hash"
    ]
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
        analysis_input_hash,
        new_task_id,
        "queued",
    )
    assert "analysis_snapshot" not in _run(_outbox_payload(sessions, old_task_id))


def test_retry_reuses_enqueue_snapshot_and_worker_ignores_mutated_live_context(
    intake_analysis_app,
):
    """Rebuilding retry input from live rows would silently change the model request."""
    client, sessions, application = intake_analysis_app
    started = client.post(
        "/v1/intake-sessions",
        json={"restart": False},
        headers={"Idempotency-Key": "snapshot-start"},
    )
    assert started.status_code == 201
    _run(
        _seed_confirmed_intake_fact(
            sessions,
            fact_id="fact_snapshot",
            value="原始已确认事实",
        )
    )
    queued = _queue_answer(client, "我完成了课程项目")
    old_task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, old_task_id))
    old_payload = _run(_outbox_payload(sessions, old_task_id))
    snapshot = old_payload["analysis_snapshot"]
    expected_hash = hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert old_payload["analysis_input_hash"] == expected_hash

    _run(_mutate_live_intake_context(sessions, "fact_snapshot"))
    _run(_mark_failed_unused(sessions, application, answer_id, old_task_id))
    retried = client.post(
        f"/v1/intake-sessions/{_run(_session_id(sessions))}/analysis/retry",
        json={"base_version": 1},
        headers={"Idempotency-Key": "snapshot-retry"},
    )
    assert retried.status_code == 202
    new_task_id = retried.json()["analysis_task_id"]
    new_payload = _run(_outbox_payload(sessions, new_task_id))
    assert new_payload["analysis_snapshot"] == snapshot
    assert new_payload["analysis_input_hash"] == expected_hash

    captured = []

    def candidates(input):
        captured.append(input)
        return []

    service = IntakeService(sessions, IntakeReceiptClient(candidates))
    claim = _run(application.state.task_service.claim_task("usr_analysis", new_task_id))
    _run(
        service.process_answer_analysis(
            "usr_analysis",
            answer_id,
            task_id=new_task_id,
            claim_token=claim.token,
            task_service=application.state.task_service,
        )
    )

    request = captured[0]
    assert request.input_hash == expected_hash
    assert request.payload.answer_text == "我完成了课程项目"
    assert request.payload.asked_question_ids == ("experience_radar",)
    assert [fact.value for fact in request.payload.confirmed_facts] == [
        "原始已确认事实"
    ]
    assert "analysis_snapshot" not in _run(_outbox_payload(sessions, new_task_id))


def test_worker_rejects_tampered_analysis_snapshot_before_calling_ai(
    intake_analysis_app,
):
    """Trusting a payload whose semantic hash no longer matches would break receipts."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    _run(_tamper_outbox_snapshot(sessions, task_id))
    service = IntakeService(sessions, FailingIntakeClient())
    claim = _run(application.state.task_service.claim_task("usr_analysis", task_id))

    with pytest.raises(IntakeError, match="snapshot"):
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
    assert "analysis_snapshot" not in _run(_outbox_payload(sessions, task_id))


def test_analysis_continue_rejects_a_task_with_the_wrong_resource_type(
    intake_analysis_app,
):
    """Resource-id equality alone must not authorize a different task graph."""
    client, sessions, application = intake_analysis_app
    queued = _queue_answer(client, "我完成了课程项目")
    task_id = queued["analysis_task_id"]
    answer_id = _run(_answer_id(sessions, task_id))
    _run(_mark_failed_unused(sessions, application, answer_id, task_id))
    _run(_set_task_resource_type(sessions, task_id, "resume"))

    response = client.post(
        f"/v1/intake-sessions/{_run(_session_id(sessions))}/analysis/continue",
        json={"base_version": 1},
        headers={"Idempotency-Key": "continue-wrong-resource"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INTAKE_ANALYSIS_GRAPH_INVALID"
    assert _run(_analysis_state(sessions, answer_id))[0] == "failed"
    assert _run(_usage_state(sessions, task_id))[0] == "reserved"


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


def _validate_candidate_slice(
    source,
    value,
    *,
    start=0,
    end=None,
    risk_flags=(),
):
    end = len(source) if end is None else end
    source_slice = source[start:end]
    answer_id = "ians_guard_unit"
    answer = IntakeAnswer(
        id=answer_id,
        owner_user_id="usr_analysis",
        session_id="intake_guard_unit",
        question_id="experience_radar",
        answer_encrypted=source,
        state="answered",
        analysis_status="running",
    )
    candidate = AiFactCandidate.model_validate(
        {
            "kind": "experience",
            "value": value,
            "source_answer_id": answer_id,
            "source_range": {"start": start, "end": end},
            "source_hash": hashlib.sha256(source_slice.encode()).hexdigest(),
            "risk_flags": risk_flags,
        },
        strict=False,
    )
    result = AnalyzeIntakeResult(
        fact_candidates=(candidate,),
        missing_slots=(),
        question_candidate=None,
    )
    return _validated_candidates(answer, result)


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


async def _candidate_status(sessions, candidate_id):
    async with sessions() as session:
        return await session.scalar(
            select(FactCandidate.status).where(FactCandidate.id == candidate_id)
        )


async def _session_id(sessions):
    async with sessions() as session:
        return await session.scalar(select(IntakeSession.id))


async def _intake_state(sessions, session_id):
    async with sessions() as session:
        row = await session.scalar(
            select(IntakeSession).where(IntakeSession.id == session_id)
        )
        return row.status, row.version, row.task_id, row.draft_title


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


async def _cancelled_task_state(sessions, task_id):
    async with sessions() as session:
        task = await session.scalar(select(Task).where(Task.id == task_id))
        return (
            task.status,
            task.cancellation_requested,
            task.claim_token,
            task.active_ai_run_id,
        )


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
        return (row.status, row.error_code) if row is not None else None


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


async def _seed_confirmed_intake_fact(sessions, *, fact_id, value):
    async with sessions.begin() as session:
        intake = await session.scalar(select(IntakeSession))
        source = SourceRecord(
            id=f"src_{fact_id}",
            owner_user_id="usr_analysis",
            source_type="user_confirmation",
            source_ref=f"seed:{fact_id}",
            content_encrypted=value,
        )
        fact = Fact(
            id=fact_id,
            owner_user_id="usr_analysis",
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
                owner_user_id="usr_analysis",
                source_range={"start": 0, "end": len(value)},
                source_hash=hashlib.sha256(value.encode()).hexdigest(),
            )
        )
        await session.flush()
        fact.status = "confirmed"
        fact.confirmed_at = datetime.now(timezone.utc)
        intake.fact_ids = [*intake.fact_ids, fact.id]


async def _outbox_payload(sessions, task_id):
    async with sessions() as session:
        return await session.scalar(
            select(Outbox.payload).where(
                Outbox.task_id == task_id,
                Outbox.owner_user_id == "usr_analysis",
            )
        )


async def _outbox_state(sessions, task_id):
    async with sessions() as session:
        row = await session.scalar(
            select(Outbox).where(
                Outbox.task_id == task_id,
                Outbox.owner_user_id == "usr_analysis",
            )
        )
        return row.attempts, row.exhausted_at is not None


async def _task_event_stages(sessions, task_id):
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(TaskEvent.stage)
                    .where(
                        TaskEvent.task_id == task_id,
                        TaskEvent.owner_user_id == "usr_analysis",
                    )
                    .order_by(TaskEvent.seq)
                )
            ).all()
        )


async def _mutate_live_intake_context(sessions, fact_id):
    async with sessions.begin() as session:
        fact = await session.get(Fact, fact_id)
        fact.value_encrypted = "篡改后的事实"
        intake = await session.scalar(select(IntakeSession))
        intake.fact_ids = []
        intake.answered_question_ids = ["tampered_question"]


async def _tamper_outbox_snapshot(sessions, task_id):
    async with sessions.begin() as session:
        outbox = await session.scalar(
            select(Outbox).where(
                Outbox.task_id == task_id,
                Outbox.owner_user_id == "usr_analysis",
            )
        )
        payload = dict(outbox.payload)
        assert "analysis_snapshot" in payload
        snapshot = dict(payload["analysis_snapshot"])
        semantic_payload = dict(snapshot["payload"])
        semantic_payload["answer_text"] = "篡改后的回答"
        snapshot["payload"] = semantic_payload
        payload["analysis_snapshot"] = snapshot
        outbox.payload = payload


async def _set_session_status(sessions, session_id, status):
    async with sessions.begin() as session:
        intake = await session.get(IntakeSession, session_id)
        intake.status = status
        intake.active_owner_key = intake.owner_user_id if status == "active" else None


async def _set_answer_analysis_status(sessions, answer_id, status):
    async with sessions.begin() as session:
        answer = await session.get(IntakeAnswer, answer_id)
        answer.analysis_status = status


async def _set_task_resource_type(sessions, task_id, resource_type):
    async with sessions.begin() as session:
        task = await session.get(Task, task_id)
        task.resource_type = resource_type


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
