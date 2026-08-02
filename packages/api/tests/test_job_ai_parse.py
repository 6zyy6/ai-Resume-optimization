from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, inspect, select, text

from app.core.config import Settings
from app.db.models import (
    AiRun,
    JdRequirement,
    JobDescription,
    Outbox,
    Task,
    UsageLedger,
    User,
)
from app.integrations.ai_client import (
    AiExecutionReceipt,
    ParseJdRequest,
    derive_ai_run_id,
)
from app.modules.jobs.service import JobService
from app.modules.tasks.service import TaskClaimError, TaskService
from app.integrations.storage import MemoryStorage
from app.workers.execution import TaskExecutor, resolve_operation
from app.workers.dispatcher import OutboxDispatcher, TaskQueueBusy
from app.workers.pipeline import configure_pipeline_operations


pytestmark = pytest.mark.anyio


def _alembic_config(database_path: Path) -> Config:
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


def test_jd_requirement_model_declares_structured_provenance_and_owner_ai_run_fk():
    table = JdRequirement.__table__
    assert {
        "source_start",
        "source_end",
        "source_hash",
        "explicitness",
        "confidence_band",
        "generation_mode",
        "workflow_version",
        "ai_run_id",
        "input_hash",
    } <= set(table.c.keys())
    assert any(
        {element.parent.name for element in constraint.elements}
        == {"ai_run_id", "owner_user_id"}
        and {element.target_fullname for element in constraint.elements}
        == {"ai_runs.id", "ai_runs.owner_user_id"}
        for constraint in table.foreign_key_constraints
    )


def test_migration_0015_backfills_repeated_legacy_rows_by_occurrence_and_round_trips(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "jd-provenance.db"
    config = _alembic_config(database_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "0014")
    engine = create_engine(f"sqlite:///{database_path}")
    now = "2026-08-02 08:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, status, locale, created_at) "
                "VALUES ('usr_legacy_jd', 'active', 'zh-CN', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO job_descriptions "
                "(id, title, raw_encrypted, status, created_at, owner_user_id) "
                "VALUES ('job_legacy_jd', '工程师', '- Python\n- Python', "
                "'parsed', :now, 'usr_legacy_jd')"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO jd_requirements "
                "(id, job_id, type, priority, text_encrypted, confirmed, owner_user_id) "
                "VALUES ('req_legacy_1', 'job_legacy_jd', 'preferred', 1, 'Python', 0, "
                "'usr_legacy_jd'), "
                "('req_legacy_2', 'job_legacy_jd', 'preferred', 2, 'Python', 0, "
                "'usr_legacy_jd'), "
                "('req_legacy_3', 'job_legacy_jd', 'preferred', 3, 'Python', 0, "
                "'usr_legacy_jd')"
            )
        )

    command.upgrade(config, "0015")
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT source_start, source_end, source_hash, explicitness, "
                "confidence_band, generation_mode, workflow_version, ai_run_id, "
                "input_hash FROM jd_requirements ORDER BY priority"
            )
        ).all()
    assert [(row.source_start, row.source_end) for row in rows] == [
        (2, 8),
        (11, 17),
        (0, 17),
    ]
    assert [row.source_hash for row in rows] == [
        hashlib.sha256(b"Python").hexdigest(),
        hashlib.sha256(b"Python").hexdigest(),
        hashlib.sha256(b"- Python\n- Python").hexdigest(),
    ]
    assert all(
        (row.explicitness, row.confidence_band, row.generation_mode)
        == ("explicit", "high", "rule_fallback")
        for row in rows[:2]
    )
    assert (rows[2].explicitness, rows[2].confidence_band) == ("implicit", "low")
    assert all(row.workflow_version == "legacy-rule-fallback@1" for row in rows)
    assert all(row.ai_run_id is None and len(row.input_hash) == 64 for row in rows)

    command.downgrade(config, "0014")
    assert "source_start" not in {
        column["name"] for column in inspect(engine).get_columns("jd_requirements")
    }
    engine.dispose()


def test_migration_0015_refuses_downgrade_after_new_provenance_is_written(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "jd-provenance-guard.db"
    config = _alembic_config(database_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "0015")
    engine = create_engine(f"sqlite:///{database_path}")
    now = "2026-08-02 08:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, status, locale, created_at) "
                "VALUES ('usr_new_jd', 'active', 'zh-CN', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO job_descriptions "
                "(id, title, raw_encrypted, status, created_at, owner_user_id) "
                "VALUES ('job_new_jd', '工程师', 'Python', 'parsed', :now, 'usr_new_jd')"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO jd_requirements "
                "(id, job_id, type, priority, text_encrypted, confirmed, source_start, "
                "source_end, source_hash, explicitness, confidence_band, generation_mode, "
                "workflow_version, ai_run_id, input_hash, owner_user_id) VALUES "
                "('req_new_jd', 'job_new_jd', 'preferred', 1, 'Python', 0, 0, 6, "
                ":source_hash, 'explicit', 'high', 'rule_fallback', '2', NULL, "
                ":input_hash, 'usr_new_jd')"
            ),
            {
                "source_hash": hashlib.sha256(b"Python").hexdigest(),
                "input_hash": "a" * 64,
            },
        )

    with pytest.raises(RuntimeError, match="cannot downgrade JD provenance"):
        command.downgrade(config, "0014")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0015"
        assert "source_start" in {
            column["name"] for column in inspect(connection).get_columns("jd_requirements")
        }
    engine.dispose()


class ReceiptClient:
    def __init__(self, requirements=(), *, status="succeeded", error_code=None):
        self.requirements = tuple(requirements)
        self.status = status
        self.error_code = error_code
        self.requests: list[ParseJdRequest] = []

    async def run(self, request, cancellation=None):
        del cancellation
        assert isinstance(request, ParseJdRequest)
        self.requests.append(request)
        return _receipt(
            request,
            requirements=self.requirements,
            status=self.status,
            error_code=self.error_code,
        )


class CancellingReceiptClient(ReceiptClient):
    owner_id: str
    tasks: TaskService

    async def run(self, request, cancellation=None):
        assert cancellation is not None
        ai_run_id = derive_ai_run_id(request.task_id, "parse", request.input_hash)
        assert await cancellation.register_run(ai_run_id) is True
        await self.tasks.request_cancel(self.owner_id, request.task_id)
        await cancellation.acknowledge_cancel(ai_run_id)
        return _receipt(request, status="cancelled", error_code="cancelled_by_user")


class FlakyReceiptClient(ReceiptClient):
    def __init__(self, requirements, failures: int):
        super().__init__(requirements)
        self.failures = failures
        self.calls = 0

    async def run(self, request, cancellation=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("provider timed out")
        return await super().run(request, cancellation)


class CancelThenErrorClient(ReceiptClient):
    owner_id: str
    tasks: TaskService

    async def run(self, request, cancellation=None):
        assert cancellation is not None
        ai_run_id = derive_ai_run_id(request.task_id, "parse", request.input_hash)
        assert await cancellation.register_run(ai_run_id) is True
        await self.tasks.request_cancel(self.owner_id, request.task_id)
        raise TimeoutError("transport failed after cancellation")


class FailingPublisher:
    def publish(self, task_id: str, owner_user_id: str, queue: str) -> None:
        del task_id, owner_user_id, queue
        raise RuntimeError("broker unavailable")


def _receipt(
    request: ParseJdRequest,
    *,
    requirements=(),
    status: str = "succeeded",
    error_code: str | None = None,
) -> AiExecutionReceipt:
    ai_run_id = derive_ai_run_id(request.task_id, "parse", request.input_hash)
    result = {"requirements": tuple(requirements)} if status == "succeeded" else None
    return AiExecutionReceipt.model_validate(
        {
            "run": {
                "ai_run_id": ai_run_id,
                "trace_id": request.trace_id,
                "task_id": request.task_id,
                "workflow_type": "parse_jd",
                "workflow_version": "2",
                "prompt_template_version": "jd-parse@2",
                "status": status,
                "error_code": error_code,
                "provider": "deepseek",
                "requested_model": "deepseek-chat",
                "response_model": "deepseek-chat",
                "started_at": "2026-08-02T08:00:00Z",
                "first_token_at": "2026-08-02T08:00:01Z",
                "finished_at": "2026-08-02T08:00:02Z",
                "usage": {
                    "input": 12,
                    "output": 8,
                    "cache_read": 0,
                    "cache_write": 0,
                    "reasoning": 0,
                    "total_tokens": 20,
                    "cost_usd": Decimal("0.01"),
                },
                "events": (
                    {
                        "ai_run_id": ai_run_id,
                        "trace_id": request.trace_id,
                        "task_id": request.task_id,
                        "event_seq": 1,
                        "event_type": f"run_{status}",
                        "occurred_at": "2026-08-02T08:00:02Z",
                        "details": {"error_code": error_code} if error_code else None,
                    },
                ),
                "turn_count": 1,
                "tool_call_count": 0,
                "retry_count": 0,
                "fallback_count": 0,
                "schema_valid": status == "succeeded",
                "facts_valid": status == "succeeded",
                "input_hash": request.input_hash,
                "exportable": False,
                "risk_flags": (),
            },
            "result": result,
        }
    )


async def _queued_parse(sessions, ai_client, *, raw: str, suffix: str):
    owner_id = f"usr_{suffix}"
    async with sessions.begin() as session:
        session.add(User(id=owner_id))
    tasks = TaskService(sessions)
    jobs = JobService(sessions, ai_client)
    job = await jobs.create(
        owner_id,
        {"title": "后端工程师", "company": None, "raw": raw},
        f"create-{suffix}",
    )
    queued, _ = await jobs.parse(
        owner_id,
        job.id,
        f"parse-{suffix}",
        trace_id=f"trace_{suffix}",
        task_service=tasks,
    )
    claim = await tasks.claim_task(owner_id, queued.task_id)
    assert claim is not None
    return owner_id, jobs, tasks, queued, claim


async def _queued_unclaimed_parse(sessions, ai_client, *, raw: str, suffix: str):
    owner_id = f"usr_{suffix}"
    async with sessions.begin() as session:
        session.add(User(id=owner_id))
    tasks = TaskService(sessions)
    jobs = JobService(sessions, ai_client)
    job = await jobs.create(
        owner_id,
        {"title": "后端工程师", "company": None, "raw": raw},
        f"create-{suffix}",
    )
    queued, _ = await jobs.parse(
        owner_id,
        job.id,
        f"parse-{suffix}",
        trace_id=f"trace_{suffix}",
        task_service=tasks,
    )
    return owner_id, jobs, tasks, queued


async def _rows(sessions, owner_id: str, job_id: str):
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(JdRequirement)
                    .where(
                        JdRequirement.owner_user_id == owner_id,
                        JdRequirement.job_id == job_id,
                    )
                    .order_by(JdRequirement.priority, JdRequirement.id)
                )
            ).all()
        )


async def _outbox_payload(sessions, owner_id: str, task_id: str):
    async with sessions() as session:
        outbox = await session.scalar(
            select(Outbox).where(
                Outbox.owner_user_id == owner_id,
                Outbox.task_id == task_id,
            )
        )
        assert outbox is not None
        return outbox.payload


async def test_model_receipt_persists_exact_sourced_candidates_and_provenance(
    sql_session_factory,
):
    raw = "负责 API 设计\n熟练 Python"
    python_start = raw.index("熟练 Python")
    client = ReceiptClient(
        (
            {
                "category": "responsibility",
                "priority": 1,
                "value": "负责 API 设计",
                "source_range": {"start": 0, "end": len("负责 API 设计")},
                "explicitness": "explicit",
                "confidence_band": "high",
            },
            {
                "category": "must_have",
                "priority": 2,
                "value": "熟练 Python",
                "source_range": {
                    "start": python_start,
                    "end": python_start + len("熟练 Python"),
                },
                "explicitness": "explicit",
                "confidence_band": "high",
            },
        )
    )
    owner, jobs, tasks, job, claim = await _queued_parse(
        sql_session_factory, client, raw=raw, suffix="model_jd"
    )

    await jobs.process_parse(
        owner,
        job.id,
        trace_id="trace_model_jd",
        task_id=job.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    rows = await _rows(sql_session_factory, owner, job.id)
    assert [raw[row.source_start : row.source_end] for row in rows] == [
        "负责 API 设计",
        "熟练 Python",
    ]
    assert [row.source_hash for row in rows] == [
        hashlib.sha256(value.encode()).hexdigest()
        for value in ("负责 API 设计", "熟练 Python")
    ]
    assert {(row.generation_mode, row.workflow_version) for row in rows} == {
        ("model", "2")
    }
    assert all(row.ai_run_id and row.input_hash for row in rows)
    assert all(row.confirmed is False for row in rows)
    assert len(client.requests) == 1
    assert client.requests[0].payload.jd_text == raw
    assert client.requests[0].payload.job_title == "后端工程师"


async def test_rule_fallback_maps_repeated_lines_to_distinct_occurrences_and_releases_usage(
    sql_session_factory,
):
    raw = "- Python\n- Python"
    owner, jobs, tasks, job, claim = await _queued_parse(
        sql_session_factory, None, raw=raw, suffix="fallback_jd"
    )

    await jobs.process_parse(
        owner,
        job.id,
        trace_id="trace_fallback_jd",
        task_id=job.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    rows = await _rows(sql_session_factory, owner, job.id)
    assert [(row.source_start, row.source_end) for row in rows] == [(2, 8), (11, 17)]
    assert [raw[row.source_start : row.source_end] for row in rows] == [
        "Python",
        "Python",
    ]
    assert all(row.source_hash == hashlib.sha256(b"Python").hexdigest() for row in rows)
    assert all(row.generation_mode == "rule_fallback" for row in rows)
    assert all(row.workflow_version == "2" for row in rows)
    assert all(row.ai_run_id is None for row in rows)
    assert all(row.input_hash for row in rows)
    async with sql_session_factory() as session:
        usage = await session.scalar(
            select(UsageLedger).where(
                UsageLedger.owner_user_id == owner,
                UsageLedger.task_id == job.task_id,
            )
        )
    assert usage is None
    payload = await _outbox_payload(sql_session_factory, owner, job.task_id)
    assert "parse_snapshot" not in payload
    assert payload["generation_mode"] == "rule_fallback"
    assert payload["parse_input_hash"]


@pytest.mark.parametrize(
    "requirement",
    [
        {
            "category": "must_have",
            "priority": 1,
            "value": "SQL",
            "source_range": {"start": 0, "end": 6},
            "explicitness": "explicit",
            "confidence_band": "high",
        },
        {
            "category": "must_have",
            "priority": 1,
            "value": "Python",
            "source_range": {"start": 0, "end": 99},
            "explicitness": "explicit",
            "confidence_band": "high",
        },
    ],
)
async def test_invalid_model_source_fails_task_and_publishes_zero_requirements(
    sql_session_factory,
    requirement,
):
    client = ReceiptClient((requirement,))
    owner, jobs, tasks, job, claim = await _queued_parse(
        sql_session_factory, client, raw="Python", suffix=f"bad_{requirement['source_range']['end']}"
    )

    await jobs.process_parse(
        owner,
        job.id,
        trace_id=f"trace_bad_{requirement['source_range']['end']}",
        task_id=job.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    assert await _rows(sql_session_factory, owner, job.id) == []
    task = await tasks.get_task(owner, job.task_id)
    assert task is not None
    assert task.status == "failed"
    assert task.error_code == "JD_REQUIREMENT_SOURCE_INVALID"
    async with sql_session_factory() as session:
        current = await session.scalar(
            select(JobDescription).where(
                JobDescription.id == job.id,
                JobDescription.owner_user_id == owner,
            )
        )
        run_count = await session.scalar(
            select(func.count()).select_from(AiRun).where(AiRun.owner_user_id == owner)
        )
    assert current is not None and current.status == "failed"
    assert run_count == 1


async def test_failed_model_receipt_is_audited_without_rule_fallback(
    sql_session_factory,
):
    client = ReceiptClient(status="failed", error_code="provider_unavailable")
    owner, jobs, tasks, job, claim = await _queued_parse(
        sql_session_factory, client, raw="Python", suffix="provider_failure"
    )

    await jobs.process_parse(
        owner,
        job.id,
        trace_id="trace_provider_failure",
        task_id=job.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    assert await _rows(sql_session_factory, owner, job.id) == []
    task = await tasks.get_task(owner, job.task_id)
    assert task is not None
    assert task.status == "failed"
    assert task.error_code == "provider_unavailable"
    async with sql_session_factory() as session:
        run = await session.scalar(
            select(AiRun).where(
                AiRun.owner_user_id == owner,
                AiRun.task_id == job.task_id,
            )
        )
        usage = await session.scalar(
            select(UsageLedger).where(
                UsageLedger.owner_user_id == owner,
                UsageLedger.task_id == job.task_id,
            )
        )
    assert run is not None and run.status == "failed"
    assert usage is not None and usage.state == "consumed"
    assert usage.ai_run_id == run.id
    assert "parse_snapshot" not in await _outbox_payload(
        sql_session_factory, owner, job.task_id
    )


async def test_final_transaction_rolls_back_receipt_candidates_and_task_then_retries_once(
    sql_session_factory,
):
    requirement = {
        "category": "must_have",
        "priority": 1,
        "value": "Python",
        "source_range": {"start": 0, "end": 6},
        "explicitness": "explicit",
        "confidence_band": "high",
    }
    client = ReceiptClient((requirement,))
    owner, jobs, tasks, job, claim = await _queued_parse(
        sql_session_factory, client, raw="Python", suffix="atomic_retry"
    )
    complete = tasks.complete_task_in_session

    async def fail_completion(session, task, result_ref):
        del session, task, result_ref
        raise RuntimeError("commit boundary failed")

    tasks.complete_task_in_session = fail_completion
    with pytest.raises(RuntimeError, match="commit boundary failed"):
        await jobs.process_parse(
            owner,
            job.id,
            trace_id="trace_atomic_retry",
            task_id=job.task_id,
            claim_token=claim.token,
            task_service=tasks,
        )
    tasks.complete_task_in_session = complete

    async with sql_session_factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(JdRequirement).where(
                JdRequirement.owner_user_id == owner,
                JdRequirement.job_id == job.id,
            )
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(AiRun).where(AiRun.owner_user_id == owner)
        ) == 0
        usage = await session.scalar(
            select(UsageLedger).where(
                UsageLedger.owner_user_id == owner,
                UsageLedger.task_id == job.task_id,
            )
        )
    task = await tasks.get_task(owner, job.task_id)
    assert task is not None and task.status == "running"
    assert usage is not None and usage.state == "reserved"

    await jobs.process_parse(
        owner,
        job.id,
        trace_id="trace_atomic_retry",
        task_id=job.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    assert len(await _rows(sql_session_factory, owner, job.id)) == 1
    async with sql_session_factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(AiRun).where(AiRun.owner_user_id == owner)
        ) == 1
    assert len(client.requests) == 2


async def test_cancelled_model_receipt_cannot_publish_requirements(
    sql_session_factory,
):
    client = CancellingReceiptClient()
    owner, jobs, tasks, job, claim = await _queued_parse(
        sql_session_factory, client, raw="Python", suffix="cancel_parse"
    )
    client.owner_id = owner
    client.tasks = tasks

    from app.workers.pipeline import TaskAiCancellation

    await jobs.process_parse(
        owner,
        job.id,
        trace_id="trace_cancel_parse",
        task_id=job.task_id,
        claim_token=claim.token,
        task_service=tasks,
        cancellation=TaskAiCancellation(tasks, claim),
    )

    assert await _rows(sql_session_factory, owner, job.id) == []
    task = await tasks.get_task(owner, job.task_id)
    assert task is not None and task.status == "cancelled"
    async with sql_session_factory() as session:
        current = await session.scalar(
            select(JobDescription).where(
                JobDescription.id == job.id,
                JobDescription.owner_user_id == owner,
            )
        )
        run = await session.scalar(
            select(AiRun).where(
                AiRun.owner_user_id == owner,
                AiRun.task_id == job.task_id,
            )
        )
    assert current is not None and current.status == "draft"
    assert run is not None and run.status == "cancelled"
    assert "parse_snapshot" not in await _outbox_payload(
        sql_session_factory, owner, job.task_id
    )


async def test_parse_worker_owner_mismatch_cannot_read_or_publish_job(
    sql_session_factory,
):
    owner, jobs, tasks, job, claim = await _queued_parse(
        sql_session_factory, None, raw="Python", suffix="owned_parse"
    )
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_parse_intruder"))

    with pytest.raises(TaskClaimError):
        await jobs.process_parse(
            "usr_parse_intruder",
            job.id,
            trace_id="trace_owned_parse",
            task_id=job.task_id,
            claim_token=claim.token,
            task_service=tasks,
        )

    assert await _rows(sql_session_factory, owner, job.id) == []


@pytest.mark.parametrize(
    ("failures", "expected_status", "expected_rows"),
    [(2, "succeeded", 1), (3, "failed", 0)],
)
async def test_provider_retry_recovery_never_falls_back_to_rule_rows(
    sql_session_factory,
    failures,
    expected_status,
    expected_rows,
):
    requirement = {
        "category": "must_have",
        "priority": 1,
        "value": "Python",
        "source_range": {"start": 0, "end": 6},
        "explicitness": "explicit",
        "confidence_band": "high",
    }
    client = FlakyReceiptClient((requirement,), failures)
    owner, _, tasks, job, _ = await _queued_parse(
        sql_session_factory, client, raw="Python", suffix=f"provider_retry_{failures}"
    )
    # The helper claims once to exercise service paths elsewhere; expire that lease so
    # the real executor can own all retry claims in this test.
    async with sql_session_factory.begin() as session:
        task = await session.scalar(
            select(Task).where(
                Task.id == job.task_id,
                Task.owner_user_id == owner,
            )
        )
        assert task is not None
        task.claim_lease_expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        task.attempts = 0
        task.status = "queued"
    configure_pipeline_operations(
        sql_session_factory,
        Settings(app_env="test", database_url="sqlite+aiosqlite://"),
        tasks,
        storage_override=MemoryStorage(),
        ai_client_override=client,
    )
    executor = TaskExecutor(tasks, sleep=lambda _: None, jitter=lambda: 0)

    result = await executor.execute(owner, job.task_id, resolve_operation)

    assert result["status"] == expected_status
    assert len(await _rows(sql_session_factory, owner, job.id)) == expected_rows
    assert client.calls == 3
    if expected_rows:
        rows = await _rows(sql_session_factory, owner, job.id)
        assert rows[0].generation_mode == "model"
    else:
        async with sql_session_factory() as session:
            current = await session.scalar(
                select(JobDescription).where(
                    JobDescription.id == job.id,
                    JobDescription.owner_user_id == owner,
                )
            )
            usage = await session.scalar(
                select(UsageLedger).where(
                    UsageLedger.task_id == job.task_id,
                    UsageLedger.owner_user_id == owner,
                )
            )
        assert current is not None and current.status == "failed"
        assert usage is not None and usage.state == "released"


async def test_parse_processing_rejects_claimless_execution(sql_session_factory):
    owner, jobs, _, job = await _queued_unclaimed_parse(
        sql_session_factory, None, raw="Python", suffix="claim_required"
    )

    with pytest.raises(TypeError, match="claim"):
        await jobs.process_parse(
            owner,
            job.id,
            trace_id="trace_claim_required",
            task_id=job.task_id,
        )

    assert await _rows(sql_session_factory, owner, job.id) == []


async def test_claimed_parse_uses_immutable_snapshot_after_job_text_changes(
    sql_session_factory,
):
    requirement = {
        "category": "must_have",
        "priority": 1,
        "value": "Python",
        "source_range": {"start": 0, "end": 6},
        "explicitness": "explicit",
        "confidence_band": "high",
    }
    client = ReceiptClient((requirement,))
    owner, jobs, tasks, job, claim = await _queued_parse(
        sql_session_factory, client, raw="Python", suffix="snapshot_authority"
    )
    async with sql_session_factory.begin() as session:
        current = await session.scalar(
            select(JobDescription).where(
                JobDescription.id == job.id,
                JobDescription.owner_user_id == owner,
            )
        )
        assert current is not None
        current.title = "已变更标题"
        current.raw_encrypted = "SQL"

    await jobs.process_parse(
        owner,
        job.id,
        trace_id="trace_snapshot_authority",
        task_id=job.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    assert client.requests[0].payload.job_title == "后端工程师"
    assert client.requests[0].payload.jd_text == "Python"
    assert [row.text_encrypted for row in await _rows(sql_session_factory, owner, job.id)] == [
        "Python"
    ]


async def test_preclaim_parse_cancellation_restores_job_and_scrubs_snapshot(
    sql_session_factory,
):
    owner, _, tasks, job = await _queued_unclaimed_parse(
        sql_session_factory, None, raw="Python", suffix="preclaim_cancel"
    )

    task = await tasks.request_cancel(owner, job.task_id)

    assert task.status == "cancelled"
    async with sql_session_factory() as session:
        current = await session.scalar(
            select(JobDescription).where(
                JobDescription.id == job.id,
                JobDescription.owner_user_id == owner,
            )
        )
    assert current is not None and current.status == "draft"
    assert "parse_snapshot" not in await _outbox_payload(
        sql_session_factory, owner, job.task_id
    )


async def test_cancel_then_transport_error_recovers_parse_via_real_executor(
    sql_session_factory,
):
    client = CancelThenErrorClient()
    owner, _, tasks, job = await _queued_unclaimed_parse(
        sql_session_factory, client, raw="Python", suffix="cancel_transport"
    )
    client.owner_id = owner
    client.tasks = tasks
    configure_pipeline_operations(
        sql_session_factory,
        Settings(app_env="test", database_url="sqlite+aiosqlite://"),
        tasks,
        storage_override=MemoryStorage(),
        ai_client_override=client,
    )

    result = await TaskExecutor(
        tasks, sleep=lambda _: None, jitter=lambda: 0
    ).execute(owner, job.task_id, resolve_operation)

    assert result["status"] == "cancelled"
    task = await tasks.get_task(owner, job.task_id)
    assert task is not None
    assert task.active_ai_run_id is None
    assert task.claim_token is None
    async with sql_session_factory() as session:
        current = await session.scalar(
            select(JobDescription).where(
                JobDescription.id == job.id,
                JobDescription.owner_user_id == owner,
            )
        )
        usage = await session.scalar(
            select(UsageLedger).where(
                UsageLedger.task_id == job.task_id,
                UsageLedger.owner_user_id == owner,
            )
        )
    assert current is not None and current.status == "draft"
    assert usage is not None and usage.state == "consumed"
    assert usage.ai_run_id is not None
    assert "parse_snapshot" not in await _outbox_payload(
        sql_session_factory, owner, job.task_id
    )


async def test_rule_fallback_queues_unmetered_when_ai_limits_are_exhausted(
    sql_session_factory,
):
    owner_id = "usr_fallback_limits"
    now = datetime.now(timezone.utc)
    async with sql_session_factory.begin() as session:
        session.add(User(id=owner_id))
        await session.flush()
        for index in range(20):
            task_id = f"tsk_limit_{index}"
            active = index < 2
            session.add(
                Task(
                    id=task_id,
                    owner_user_id=owner_id,
                    type="match_resume",
                    status="queued" if active else "succeeded",
                    trace_id=f"trace_limit_{index}",
                    queued_at=now,
                    finished_at=None if active else now,
                    stage="queued" if active else "succeeded",
                    usage_type="ai_task",
                )
            )
            session.add(
                UsageLedger(
                    id=f"usg_limit_{index}",
                    owner_user_id=owner_id,
                    usage_type="ai_task",
                    quantity=1,
                    cost_cny=Decimal("0"),
                    trace_id=f"trace_limit_{index}",
                    state="reserved" if active else "consumed",
                    task_id=task_id,
                    created_at=now,
                    updated_at=now,
                )
            )
    tasks = TaskService(sql_session_factory)
    jobs = JobService(sql_session_factory, None)
    job = await jobs.create(
        owner_id,
        {"title": "后端工程师", "company": None, "raw": "Python"},
        "create-fallback-limits",
    )

    queued, _ = await jobs.parse(
        owner_id,
        job.id,
        "parse-fallback-limits",
        trace_id="trace_fallback_limits",
        task_service=tasks,
    )

    task = await tasks.get_task(owner_id, queued.task_id)
    assert task is not None and task.status == "queued"
    assert task.usage_type is None
    async with sql_session_factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(UsageLedger).where(
                UsageLedger.task_id == queued.task_id,
                UsageLedger.owner_user_id == owner_id,
            )
        ) == 0


async def test_empty_rule_fallback_fails_with_stable_error(sql_session_factory):
    owner, jobs, tasks, job, claim = await _queued_parse(
        sql_session_factory, None, raw="  ", suffix="empty_fallback"
    )

    await jobs.process_parse(
        owner,
        job.id,
        trace_id="trace_empty_fallback",
        task_id=job.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    assert await _rows(sql_session_factory, owner, job.id) == []
    task = await tasks.get_task(owner, job.task_id)
    assert task is not None and task.status == "failed"
    assert task.error_code == "JD_REQUIREMENTS_EMPTY"
    assert "parse_snapshot" not in await _outbox_payload(
        sql_session_factory, owner, job.task_id
    )


async def test_parse_dispatch_exhaustion_restores_job_and_scrubs_snapshot(
    sql_session_factory,
):
    owner, _, tasks, job = await _queued_unclaimed_parse(
        sql_session_factory, None, raw="Python", suffix="dispatch_exhausted"
    )
    dispatcher = OutboxDispatcher(
        sql_session_factory,
        FailingPublisher(),
        retry_base_seconds=0,
        jitter=lambda: 0,
    )

    for _ in range(3):
        with pytest.raises(TaskQueueBusy):
            await dispatcher.dispatch_task(job.task_id)

    task = await tasks.get_task(owner, job.task_id)
    assert task is not None and task.status == "failed"
    assert task.error_code == "TASK_QUEUE_UNAVAILABLE"
    async with sql_session_factory() as session:
        current = await session.scalar(
            select(JobDescription).where(
                JobDescription.id == job.id,
                JobDescription.owner_user_id == owner,
            )
        )
    assert current is not None and current.status == "draft"
    assert "parse_snapshot" not in await _outbox_payload(
        sql_session_factory, owner, job.task_id
    )


async def test_job_create_enforces_typed_parse_input_limit_without_task_side_effects(
    pipeline_client,
):
    client, sessions, _ = pipeline_client

    accepted = client.post(
        "/v1/jobs",
        json={"title": "后端工程师", "raw": "x" * 20_000},
        headers={"Idempotency-Key": "job-20000"},
    )
    rejected = client.post(
        "/v1/jobs",
        json={"title": "后端工程师", "raw": "x" * 20_001},
        headers={"Idempotency-Key": "job-20001"},
    )

    assert accepted.status_code == 201
    assert rejected.status_code == 422
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
        assert await session.scalar(select(func.count()).select_from(UsageLedger)) == 0
