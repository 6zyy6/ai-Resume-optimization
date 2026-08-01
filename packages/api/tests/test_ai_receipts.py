from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import inspect as python_inspect
import json
from pathlib import Path
import subprocess

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.db.models import AiRun, AiTraceEvent, User
from app.integrations.ai_client import (
    AI_WORKFLOW_REQUEST_ADAPTER,
    AiExecutionReceipt,
    FixtureAiClient,
    InternalAiClient,
    ParseJdRequest,
    derive_ai_run_id,
    workflow_stage_for,
)
from app.modules.ai_runs.service import AiRunService
from app.modules.tasks.service import TaskAdmission, TaskService


pytestmark = pytest.mark.anyio


def _receipt(
    task_id: str,
    workflow_stage: str,
    *,
    status: str = "failed",
    error_code: str | None = "provider_unavailable",
) -> AiExecutionReceipt:
    ai_run_id = derive_ai_run_id(task_id, workflow_stage, "input_hash_1")
    finished_at = "2026-07-30T08:00:04Z"
    return AiExecutionReceipt.model_validate_json(
        json.dumps(
            {
            "run": {
                "ai_run_id": ai_run_id,
                "trace_id": "tr_receipt",
                "task_id": task_id,
                "workflow_type": "parse_jd",
                "workflow_version": "2",
                "prompt_template_version": "jd-parse@2",
                "status": status,
                "error_code": error_code,
                "provider": "deepseek",
                "requested_model": "deepseek-chat",
                "response_model": "deepseek-chat-202607",
                "started_at": "2026-07-30T08:00:00Z",
                "first_token_at": "2026-07-30T08:00:01Z",
                "finished_at": finished_at,
                "usage": {
                    "input": 19,
                    "output": 3,
                    "cache_read": 2,
                    "cache_write": 0,
                    "reasoning": 1,
                    "total_tokens": 25,
                    "cost_usd": 0.123456789012,
                },
                "events": [
                    {
                        "ai_run_id": ai_run_id,
                        "trace_id": "tr_receipt",
                        "task_id": task_id,
                        "event_seq": 1,
                        "event_type": "provider_started",
                        "occurred_at": "2026-07-30T08:00:01Z",
                        "details": {
                            "provider": "deepseek",
                            "duration_ms": 12,
                            "usage": {
                                "input": 19,
                                "output": 3,
                                "cache_read": 2,
                                "cache_write": 0,
                                "reasoning": 1,
                                "total_tokens": 25,
                                "cost_usd": 0.123456789012,
                                "content": "nested body must not persist",
                            },
                            "context": {"content": "nested user content"},
                            "prompt": "never persist this prompt",
                            "user_body": "never persist this JD",
                            "provider_response": "never persist provider output",
                        },
                    },
                    {
                        "ai_run_id": ai_run_id,
                        "trace_id": "tr_receipt",
                        "task_id": task_id,
                        "event_seq": 2,
                        "event_type": f"run_{status}",
                        "occurred_at": finished_at,
                        "details": {"error_code": error_code},
                    },
                ],
                "turn_count": 1,
                "tool_call_count": 0,
                "retry_count": 1,
                "fallback_count": 0,
                "schema_valid": False,
                "facts_valid": False,
                "input_hash": "input_hash_1",
                "exportable": False,
                "risk_flags": ["provider_failure"],
            }
            }
        )
    )


def test_stable_run_id_is_rederived_for_the_same_task_stage_and_hash():
    first = derive_ai_run_id("tsk_1", "match", "abc")
    second = derive_ai_run_id("tsk_1", "match", "abc")

    assert first == second == "run_3e24278c1fc67246ecc0288cd4eae94ff19794f4"
    assert first.startswith("run_")
    assert len(first) == 44
    assert derive_ai_run_id("tsk_1", "suggestions", "abc") == (
        "run_f1065a7eb658641b64889f8fa6f9a29233d0282b"
    )
    assert derive_ai_run_id("tsk_1", "match", "def") == (
        "run_376c6db670f40d18d7afac2eccf0af9f7f12168d"
    )


def test_workflow_types_map_to_the_only_allowed_business_stages():
    assert {
        workflow_type: workflow_stage_for(workflow_type)
        for workflow_type in (
            "analyze_intake_answer",
            "compose_resume_draft",
            "parse_jd",
            "match_resume_to_jd",
            "generate_suggestions_batch",
        )
    } == {
        "analyze_intake_answer": "analysis",
        "compose_resume_draft": "draft",
        "parse_jd": "parse",
        "match_resume_to_jd": "match",
        "generate_suggestions_batch": "suggestions",
    }


def test_workflow_request_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        AI_WORKFLOW_REQUEST_ADAPTER.validate_python(
            {
                "workflow_type": "parse_jd",
                "workflow_version": "2",
                "prompt_template_version": "jd-parse@2",
                "trace_id": "tr_strict",
                "task_id": "tsk_strict",
                "owner_scope_hash": "owner_hash",
                "locale": "zh-CN",
                "input_version": 1,
                "input_hash": "input_hash",
                "payload": {
                    "jd_text": "Python",
                    "allowed_categories": ["must_have"],
                    "unknown": "must fail",
                },
            }
        )


def test_public_clients_expose_only_the_typed_v2_run_signature():
    for client_type in (FixtureAiClient, InternalAiClient):
        parameters = python_inspect.signature(client_type.run).parameters
        assert tuple(parameters) == ("self", "input", "cancellation")
        assert parameters["input"].default is python_inspect.Parameter.empty
        assert all(
            parameter.kind is not python_inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        assert not hasattr(client_type, "_run_legacy")


def test_python_workflow_schema_matches_real_typebox_validation():
    valid = {
        "workflow_type": "parse_jd",
        "workflow_version": "2",
        "prompt_template_version": "jd-parse@2",
        "trace_id": "tr_parity",
        "task_id": "tsk_parity",
        "owner_scope_hash": "owner_hash",
        "locale": "zh-CN",
        "input_version": 1,
        "input_hash": "input_hash",
        "payload": {
            "jd_text": "Python 工程师",
            "allowed_categories": ("must_have",),
        },
    }
    samples = [
        valid,
        {**valid, "task_id": ""},
        {**valid, "input_hash": ""},
        {**valid, "input_version": "1"},
        {**valid, "payload": {**valid["payload"], "jd_text": ""}},
    ]
    python_results = []
    for sample in samples:
        try:
            AI_WORKFLOW_REQUEST_ADAPTER.validate_json(json.dumps(sample))
        except ValidationError:
            python_results.append(False)
        else:
            python_results.append(True)
    script = """
      import { Value } from 'typebox/value';
      import { WorkflowInputSchema } from './src/contracts.ts';
      const chunks = [];
      for await (const chunk of process.stdin) chunks.push(chunk);
      const samples = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      process.stdout.write(JSON.stringify(samples.map(
        sample => Value.Check(WorkflowInputSchema, sample)
      )));
    """
    completed = subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            "--input-type=module",
            "-e",
            script,
        ],
        input=json.dumps(samples),
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2] / "ai",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [True, False, False, False, False]
    assert python_results == [True, False, False, False, False]


@pytest.mark.parametrize(
    ("status", "error_code"),
    [("failed", "provider_unavailable"), ("cancelled", None)],
)
def test_terminal_non_success_receipt_preserves_run_metadata(status, error_code):
    receipt = _receipt(
        "tsk_receipt",
        "parse",
        status=status,
        error_code=error_code,
    )

    assert receipt.run.status == status
    assert receipt.run.error_code == error_code
    assert receipt.run.provider == "deepseek"
    assert receipt.run.response_model == "deepseek-chat-202607"
    assert receipt.run.usage.total_tokens == 25
    assert receipt.run.usage.cost_usd == Decimal("0.123456789012")
    assert [event.event_seq for event in receipt.run.events] == [1, 2]
    assert receipt.result is None


async def test_internal_client_posts_strict_envelope_and_returns_failed_receipt():
    request = ParseJdRequest(
        workflow_type="parse_jd",
        prompt_template_version="jd-parse@2",
        trace_id="tr_client",
        task_id="tsk_client",
        owner_scope_hash="owner_hash",
        input_version=1,
        input_hash="input_hash_1",
        payload={
            "jd_text": "Python 工程师",
            "allowed_categories": ("must_have",),
        },
    )
    expected_id = derive_ai_run_id("tsk_client", "parse", "input_hash_1")
    terminal = _receipt("tsk_client", "parse")

    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.method == "POST"
        assert http_request.url.path == "/internal/v1/runs"
        body = json.loads(http_request.content)
        assert body == {
            "ai_run_id": expected_id,
            "input": request.model_dump(mode="json"),
        }
        return httpx.Response(
            202,
            json={"request_id": "req_1", "receipt": terminal.model_dump(mode="json")},
        )

    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        transport=httpx.MockTransport(handler),
    )
    receipt = await client.run(request)

    assert receipt.run.status == "failed"
    assert receipt.run.error_code == "provider_unavailable"
    assert receipt.run.provider == "deepseek"
    assert receipt.workflow_stage == "parse"


async def test_failed_receipt_is_persisted_once_with_safe_sequential_trace(
    sql_session_factory,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_receipt"))
    tasks = TaskService(sql_session_factory)
    task = await tasks.create_task(
        "usr_receipt",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_receipt",
        idempotency_key="receipt-task",
        admission=TaskAdmission.ai(cost_cny=Decimal("0.25")),
    )
    receipt = _receipt(task.id, "parse")
    service = AiRunService()

    async with sql_session_factory.begin() as session:
        first = await service.persist_in_session(
            session,
            "usr_receipt",
            receipt,
            workflow_stage="parse",
        )
        replay = await service.persist_in_session(
            session,
            "usr_receipt",
            receipt,
            workflow_stage="parse",
        )

    async with sql_session_factory() as session:
        stored = await session.scalar(
            select(AiRun).where(
                AiRun.id == receipt.run.ai_run_id,
                AiRun.owner_user_id == "usr_receipt",
            )
        )
        events = list(
            (
                await session.scalars(
                    select(AiTraceEvent)
                    .where(AiTraceEvent.ai_run_id == receipt.run.ai_run_id)
                    .order_by(AiTraceEvent.event_seq)
                )
            ).all()
        )
        run_count = int(
            await session.scalar(select(func.count()).select_from(AiRun)) or 0
        )

    assert first.id == replay.id == receipt.run.ai_run_id
    assert run_count == 1
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error_code == "provider_unavailable"
    assert stored.workflow_stage == "parse"
    assert stored.provider_cost == Decimal("0.123456789012")
    assert stored.cost_cny == Decimal("0")
    assert [event.event_seq for event in events] == [1, 2]
    assert events[0].payload == {
        "provider": "deepseek",
        "duration_ms": 12,
        "usage": {
            "input": 19,
            "output": 3,
            "cache_read": 2,
            "cache_write": 0,
            "reasoning": 1,
            "total_tokens": 25,
            "cost_usd": 0.123456789012,
        },
    }
    assert events[1].payload == {"error_code": "provider_unavailable"}
    persisted_text = repr(stored.__dict__) + repr([event.payload for event in events])
    assert "never persist this prompt" not in persisted_text
    assert "never persist this JD" not in persisted_text
    assert "never persist provider output" not in persisted_text
    assert "nested user content" not in persisted_text
    assert "nested body must not persist" not in persisted_text


async def test_receipt_requires_at_least_one_terminal_trace_event(
    sql_session_factory,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_empty_events"))
    tasks = TaskService(sql_session_factory)
    task = await tasks.create_task(
        "usr_empty_events",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_receipt",
        idempotency_key="empty-events",
        admission=TaskAdmission.ai(),
    )
    receipt = _receipt(task.id, "parse")
    invalid = receipt.model_copy(
        update={"run": receipt.run.model_copy(update={"events": ()})}
    )

    async with sql_session_factory.begin() as session:
        with pytest.raises(ValueError, match="at least one"):
            await AiRunService().persist_in_session(
                session,
                "usr_empty_events",
                invalid,
                workflow_stage="parse",
            )


async def test_receipt_trace_must_match_the_owned_task(sql_session_factory):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_trace_mismatch"))
    tasks = TaskService(sql_session_factory)
    task = await tasks.create_task(
        "usr_trace_mismatch",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_task",
        idempotency_key="trace-mismatch",
        admission=TaskAdmission.ai(),
    )
    receipt = _receipt(task.id, "parse")

    async with sql_session_factory.begin() as session:
        with pytest.raises(ValueError, match="trace"):
            await AiRunService().persist_in_session(
                session,
                "usr_trace_mismatch",
                receipt,
                workflow_stage="parse",
            )


async def test_stable_replay_rejects_changed_receipt_metadata_or_events(
    sql_session_factory,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_replay_conflict"))
    tasks = TaskService(sql_session_factory)
    task = await tasks.create_task(
        "usr_replay_conflict",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_receipt",
        idempotency_key="replay-conflict",
        admission=TaskAdmission.ai(),
    )
    receipt = _receipt(task.id, "parse")
    service = AiRunService()
    async with sql_session_factory.begin() as session:
        await service.persist_in_session(
            session,
            "usr_replay_conflict",
            receipt,
            workflow_stage="parse",
        )

    changed_metadata = receipt.model_copy(
        update={
            "run": receipt.run.model_copy(update={"response_model": "changed"})
        }
    )
    changed_events = receipt.model_copy(
        update={
            "run": receipt.run.model_copy(
                update={
                    "events": (
                        receipt.run.events[0].model_copy(
                            update={"event_type": "model_fallback"}
                        ),
                        receipt.run.events[1],
                    )
                }
            )
        }
    )
    changed_unmapped_metadata = receipt.model_copy(
        update={
            "run": receipt.run.model_copy(update={"exportable": True})
        }
    )
    for changed in (changed_metadata, changed_events, changed_unmapped_metadata):
        async with sql_session_factory.begin() as session:
            with pytest.raises(ValueError, match="conflict"):
                await service.persist_in_session(
                    session,
                    "usr_replay_conflict",
                    changed,
                    workflow_stage="parse",
                )


async def test_integrity_error_replay_path_also_rejects_receipt_drift(
    sql_session_factory,
    monkeypatch,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_integrity_replay"))
    task = await TaskService(sql_session_factory).create_task(
        "usr_integrity_replay",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_receipt",
        idempotency_key="integrity-replay",
        admission=TaskAdmission.ai(),
    )
    receipt = _receipt(task.id, "parse")
    service = AiRunService()
    async with sql_session_factory.begin() as session:
        await service.persist_in_session(
            session,
            "usr_integrity_replay",
            receipt,
            workflow_stage="parse",
        )

    changed = receipt.model_copy(
        update={"run": receipt.run.model_copy(update={"risk_flags": ("changed",)})}
    )
    async with sql_session_factory.begin() as session:
        real_scalar = session.scalar
        scalar_calls = 0

        async def hide_initial_replay(*args, **kwargs):
            nonlocal scalar_calls
            scalar_calls += 1
            if scalar_calls == 2:
                return None
            return await real_scalar(*args, **kwargs)

        monkeypatch.setattr(session, "scalar", hide_initial_replay)
        with pytest.raises(ValueError, match="conflict"):
            await service.persist_in_session(
                session,
                "usr_integrity_replay",
                changed,
                workflow_stage="parse",
            )
