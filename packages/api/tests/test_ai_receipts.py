from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import inspect as python_inspect
import json
from pathlib import Path
import subprocess
from typing import get_args

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import event as sqlalchemy_event, func, select

from app.db.models import AiRun, AiTraceEvent, User
from app.integrations.ai_client import (
    AI_WORKFLOW_REQUEST_ADAPTER,
    AiExecutionReceipt,
    AiProtocolError,
    FixtureAiClient,
    InternalAiClient,
    ParseJdRequest,
    TraceEventType,
    TraceUsage,
    derive_ai_run_id,
    workflow_stage_for,
)
from app.modules.ai_runs.service import AiRunService
from app.modules.tasks.service import TaskAdmission, TaskService


pytestmark = pytest.mark.anyio

CANONICAL_HASH = "a" * 64


def _receipt(
    task_id: str,
    workflow_stage: str,
    *,
    status: str = "failed",
    error_code: str | None = "provider_unavailable",
) -> AiExecutionReceipt:
    ai_run_id = derive_ai_run_id(task_id, workflow_stage, CANONICAL_HASH)
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
                    "cost_usd": 0.1234567890123,
                },
                "events": [
                    {
                        "ai_run_id": ai_run_id,
                        "trace_id": "tr_receipt",
                        "task_id": task_id,
                        "event_seq": 1,
                        "event_type": "agent_start",
                        "occurred_at": "2026-07-30T08:00:01Z",
                        "details": {
                            "provider": "deepseek",
                            "model": "FULL JD / Resume John john@example.com",
                            "fallback_reason": "Resume John john@example.com",
                            "risk_flags": [
                                "safe_flag",
                                "FULL JD / Resume John john@example.com",
                            ],
                            "duration_ms": 12,
                            "usage": {
                                "input": 19,
                                "output": 3,
                                "cache_read": 2,
                                "cache_write": 0,
                                "reasoning": 1,
                                "total_tokens": 25,
                                "cost_usd": 0.1234567890123,
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
                "input_hash": CANONICAL_HASH,
                "exportable": False,
                "risk_flags": ["provider_failure"],
            }
            }
        )
    )


def test_stable_run_id_is_rederived_for_the_same_task_stage_and_hash():
    first = derive_ai_run_id("tsk_1", "match", CANONICAL_HASH)
    second = derive_ai_run_id("tsk_1", "match", CANONICAL_HASH)

    assert first == second == "run_2505943c5ae4ea2e6b1ee5f17bbc4156cf7c7782"
    assert first.startswith("run_")
    assert len(first) == 44
    assert derive_ai_run_id("tsk_1", "suggestions", CANONICAL_HASH) == (
        "run_6cc41282b5ade5d5b03d51554943e08cdef21f0f"
    )
    assert derive_ai_run_id("tsk_1", "match", "b" * 64) == (
        "run_1c789a1f35877e6c7684aee084837e5ee1bf5acb"
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


def test_python_trace_event_types_match_the_pi_contract():
    script = """
      import { TRACE_EVENT_TYPES } from './src/contracts.ts';
      process.stdout.write(JSON.stringify(TRACE_EVENT_TYPES));
    """
    completed = subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            "--input-type=module",
            "-e",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2] / "ai",
    )

    assert completed.returncode == 0, completed.stderr
    assert list(get_args(TraceEventType)) == json.loads(completed.stdout)


def test_workflow_request_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        AI_WORKFLOW_REQUEST_ADAPTER.validate_python(
            {
                "workflow_type": "parse_jd",
                "workflow_version": "2",
                "prompt_template_version": "jd-parse@2",
                "trace_id": "tr_strict",
                "task_id": "tsk_strict",
                "owner_scope_hash": "b" * 64,
                "locale": "zh-CN",
                "input_version": 1,
                "input_hash": CANONICAL_HASH,
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
        "owner_scope_hash": CANONICAL_HASH,
        "locale": "zh-CN",
        "input_version": 1,
        "input_hash": CANONICAL_HASH,
        "payload": {
            "jd_text": "Python 工程师",
            "allowed_categories": ("must_have",),
        },
    }
    samples = [
        valid,
        {**valid, "task_id": ""},
        {**valid, "input_hash": ""},
        {**valid, "owner_scope_hash": "john@example.com"},
        {**valid, "input_hash": "arbitrary-string"},
        {**valid, "input_hash": "A" * 64},
        {**valid, "input_version": "1"},
        {**valid, "payload": {**valid["payload"], "jd_text": ""}},
        {**valid, "payload": {**valid["payload"], "job_title": None}},
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
    assert json.loads(completed.stdout) == [
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    ]
    assert python_results == [
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    ]


async def test_every_posted_workflow_input_passes_the_real_typebox_schema():
    common = {
        "workflow_version": "2",
        "owner_scope_hash": "b" * 64,
        "locale": "zh-CN",
        "input_version": 1,
        "input_hash": CANONICAL_HASH,
    }
    samples = [
        {
            **common,
            "workflow_type": "analyze_intake_answer",
            "prompt_template_version": "intake-answer@2",
            "payload": {
                "session_id_hash": "c" * 64,
                "answer_id": "answer_1",
                "question_id": "question_1",
                "question_reason": "项目经历",
                "answer_text": "我使用 Python。",
                "answer_state": "answered",
                "confirmed_facts": [],
                "covered_slots": [],
                "missing_slots": ["impact"],
                "asked_question_ids": [],
            },
        },
        {
            **common,
            "workflow_type": "compose_resume_draft",
            "prompt_template_version": "resume-draft@2",
            "payload": {
                "resume_title": "简历",
                "experience_groups": [],
                "confirmed_facts": [],
                "allowed_section_types": ["experience"],
            },
        },
        {
            **common,
            "workflow_type": "parse_jd",
            "prompt_template_version": "jd-parse@2",
            "payload": {
                "jd_text": "Python 工程师",
                "allowed_categories": ["must_have"],
            },
        },
        {
            **common,
            "workflow_type": "match_resume_to_jd",
            "prompt_template_version": "resume-match@2",
            "payload": {
                "resume_version_id": "resume_1",
                "resume_snapshot_hash": "d" * 64,
                "confirmed_facts": [],
                "confirmed_requirements": [],
            },
        },
        {
            **common,
            "workflow_type": "generate_suggestions_batch",
            "prompt_template_version": "suggestions-batch@2",
            "payload": {
                "matches": [
                    {
                        "requirement_ref": "requirement_1",
                        "category": "transferable",
                        "fact_refs": [],
                        "target_path": "sections[0].bullets[0]",
                        "original_hash": "e" * 64,
                        "original_text": "开发服务",
                    }
                ],
                "confirmed_facts": [],
                "confirmed_requirements": [],
            },
        },
    ]
    posted_inputs = []
    for index, sample in enumerate(samples):
        sample = {
            **sample,
            "trace_id": f"tr_wire_{index}",
            "task_id": f"tsk_wire_{index}",
        }
        request = AI_WORKFLOW_REQUEST_ADAPTER.validate_json(json.dumps(sample))
        ai_run_id = derive_ai_run_id(
            request.task_id,
            workflow_stage_for(request.workflow_type),
            request.input_hash,
        )
        terminal = AiExecutionReceipt.model_validate_json(
            json.dumps(
                {
                    "run": {
                        "ai_run_id": ai_run_id,
                        "trace_id": request.trace_id,
                        "task_id": request.task_id,
                        "workflow_type": request.workflow_type,
                        "workflow_version": "2",
                        "prompt_template_version": request.prompt_template_version,
                        "status": "failed",
                        "error_code": "provider_unavailable",
                        "provider": None,
                        "requested_model": None,
                        "response_model": None,
                        "started_at": "2026-07-30T08:00:00Z",
                        "first_token_at": None,
                        "finished_at": "2026-07-30T08:00:01Z",
                        "usage": {
                            "input": 0,
                            "output": 0,
                            "cache_read": 0,
                            "cache_write": 0,
                            "reasoning": 0,
                            "total_tokens": 0,
                            "cost_usd": 0,
                        },
                        "events": [
                            {
                                "ai_run_id": ai_run_id,
                                "trace_id": request.trace_id,
                                "task_id": request.task_id,
                                "event_seq": 1,
                                "event_type": "run_failed",
                                "occurred_at": "2026-07-30T08:00:01Z",
                                "details": {"error_code": "provider_unavailable"},
                            }
                        ],
                        "turn_count": 0,
                        "tool_call_count": 0,
                        "retry_count": 0,
                        "fallback_count": 0,
                        "schema_valid": False,
                        "facts_valid": False,
                        "input_hash": request.input_hash,
                        "exportable": False,
                        "risk_flags": [],
                    }
                }
            )
        )

        def handler(http_request: httpx.Request) -> httpx.Response:
            body = json.loads(http_request.content)
            posted_inputs.append(body["input"])
            return httpx.Response(
                202,
                json={"receipt": terminal.model_dump(mode="json")},
            )

        await InternalAiClient(
            "http://pi.internal",
            "service-token",
            transport=httpx.MockTransport(handler),
        ).run(request)

    script = """
      import { Value } from 'typebox/value';
      import { WorkflowInputSchema } from './src/contracts.ts';
      const chunks = [];
      for await (const chunk of process.stdin) chunks.push(chunk);
      const inputs = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      process.stdout.write(JSON.stringify(inputs.map(
        input => Value.Check(WorkflowInputSchema, input)
      )));
    """
    completed = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        input=json.dumps(posted_inputs),
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2] / "ai",
    )

    assert completed.returncode == 0, completed.stderr
    assert len(posted_inputs) == 5
    assert "job_title" not in posted_inputs[2]["payload"]
    assert json.loads(completed.stdout) == [True, True, True, True, True]


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
    assert receipt.run.usage.cost_usd == Decimal("0.1234567890123")
    assert [event.event_seq for event in receipt.run.events] == [1, 2]
    assert receipt.result is None


@pytest.mark.parametrize(
    "cost_usd",
    ["0.1234567890123456789", "1000000"],
)
def test_trace_usage_rejects_cost_outside_the_shared_usd_contract(cost_usd):
    with pytest.raises(ValidationError):
        TraceUsage.model_validate(
            {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
                "total_tokens": 0,
                "cost_usd": Decimal(cost_usd),
            }
        )


async def test_internal_client_posts_strict_envelope_and_returns_failed_receipt():
    request = ParseJdRequest(
        workflow_type="parse_jd",
        prompt_template_version="jd-parse@2",
        trace_id="tr_client",
        task_id="tsk_client",
        owner_scope_hash="b" * 64,
        input_version=1,
        input_hash=CANONICAL_HASH,
        payload={
            "jd_text": "Python 工程师",
            "allowed_categories": ("must_have",),
        },
    )
    expected_id = derive_ai_run_id("tsk_client", "parse", CANONICAL_HASH)
    terminal = _receipt("tsk_client", "parse")

    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.method == "POST"
        assert http_request.url.path == "/internal/v1/runs"
        body = json.loads(http_request.content)
        assert body == {
            "ai_run_id": expected_id,
            "input": request.model_dump(mode="json", exclude_none=True),
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


async def test_internal_client_rejects_wire_cost_with_more_than_18_decimals():
    request = ParseJdRequest(
        workflow_type="parse_jd",
        prompt_template_version="jd-parse@2",
        trace_id="tr_wire_cost",
        task_id="tsk_wire_cost",
        owner_scope_hash="b" * 64,
        input_version=1,
        input_hash=CANONICAL_HASH,
        payload={
            "jd_text": "Python 工程师",
            "allowed_categories": ("must_have",),
        },
    )
    terminal = _receipt("tsk_wire_cost", "parse")
    body = {"receipt": terminal.model_dump(mode="json")}
    body["receipt"]["run"]["usage"]["cost_usd"] = "COST_MARKER"
    raw_receipt = json.dumps(body).replace(
        '"COST_MARKER"',
        "0.1234567890123456789",
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.method == "DELETE":
            return httpx.Response(202, json={"accepted": True})
        return httpx.Response(
            202,
            content=raw_receipt,
            headers={"content-type": "application/json"},
        )

    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AiProtocolError, match="terminal receipt is invalid"):
        await client.run(request)


@pytest.mark.parametrize("malicious_event_type", ["john@example.com", "13800138000"])
async def test_malicious_receipt_event_type_is_rejected_before_persistence(
    sql_session_factory,
    malicious_event_type,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_event_type_privacy"))
    task = await TaskService(sql_session_factory).create_task(
        "usr_event_type_privacy",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_receipt",
        idempotency_key="event-type-privacy",
        admission=TaskAdmission.ai(),
    )
    request = ParseJdRequest(
        workflow_type="parse_jd",
        prompt_template_version="jd-parse@2",
        trace_id="tr_receipt",
        task_id=task.id,
        owner_scope_hash="b" * 64,
        input_version=1,
        input_hash=CANONICAL_HASH,
        payload={
            "jd_text": "Python 工程师",
            "allowed_categories": ("must_have",),
        },
    )
    raw_receipt = _receipt(task.id, "parse").model_dump(mode="json")
    expected_id = derive_ai_run_id(task.id, "parse", CANONICAL_HASH)
    raw_receipt["run"]["ai_run_id"] = expected_id
    raw_receipt["run"]["input_hash"] = CANONICAL_HASH
    for event in raw_receipt["run"]["events"]:
        event["ai_run_id"] = expected_id
    raw_receipt["run"]["events"][0]["event_type"] = malicious_event_type

    def handler(_http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"receipt": raw_receipt})

    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AiProtocolError, match="terminal receipt is invalid"):
        receipt = await client.run(request)
        async with sql_session_factory.begin() as session:
            await AiRunService().persist_in_session(
                session,
                "usr_event_type_privacy",
                receipt,
                workflow_stage="parse",
            )

    async with sql_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AiRun)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(AiTraceEvent))
            == 0
        )


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
    assert stored.provider == "deepseek"
    assert stored.requested_model == "deepseek-chat"
    assert stored.response_model == "deepseek-chat-202607"
    assert stored.prompt_template_version == "jd-parse@2"
    assert stored.provider_cost == Decimal("0.1234567890123")
    assert stored.cost_cny == Decimal("0")
    assert [event.event_seq for event in events] == [1, 2]
    assert events[0].payload == {
        "provider": "deepseek",
        "model": (
            "sha256:"
            + hashlib.sha256(
                b"FULL JD / Resume John john@example.com"
            ).hexdigest()[:16]
        ),
        "risk_flags": ["safe_flag"],
        "duration_ms": 12,
        "usage": {
            "input": 19,
            "output": 3,
            "cache_read": 2,
            "cache_write": 0,
            "reasoning": 1,
            "total_tokens": 25,
            "cost_usd": 0.1234567890123,
        },
    }
    assert events[1].payload == {"error_code": "provider_unavailable"}
    persisted_text = repr(stored.__dict__) + repr([event.payload for event in events])
    assert "never persist this prompt" not in persisted_text
    assert "never persist this JD" not in persisted_text
    assert "never persist provider output" not in persisted_text
    assert "nested user content" not in persisted_text
    assert "nested body must not persist" not in persisted_text
    assert "FULL JD" not in persisted_text
    assert "Resume John" not in persisted_text
    assert "john@example.com" not in persisted_text
    assert "FULL_JD" not in persisted_text
    assert "Resume_John" not in persisted_text
    assert "john_example.com" not in persisted_text


async def test_trace_string_fields_apply_irreversible_field_level_privacy_policy(
    sql_session_factory,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_trace_privacy"))
    tasks = TaskService(sql_session_factory)
    task = await tasks.create_task(
        "usr_trace_privacy",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_receipt",
        idempotency_key="trace-field-privacy",
        admission=TaskAdmission.ai(),
    )
    receipt = _receipt(task.id, "parse")
    sensitive_values = (
        "13800138000",
        "11010519491231002X",
        "john.doe",
        "john-doe",
        "john_doe",
        "john@example.com",
        "gpt-13800138000",
        "deepseek-11010519491231002X-john@example.com",
        "faux-john_doe",
        "john-doe@2",
    )
    string_fields = (
        "provider",
        "model",
        "response_model",
        "response_id",
        "stop_reason",
        "tool_name",
        "status",
        "schema_path",
        "error_code",
        "fallback_reason",
        "input_hash",
        "prompt_template_version",
        "source_event_type_hash",
    )
    template = receipt.run.events[0]
    privacy_events = [
        template.model_copy(
            update={
                "event_seq": index,
                "details": {
                    **{field: value for field in string_fields},
                    "risk_flags": [value],
                },
            }
        )
        for index, value in enumerate(sensitive_values, start=1)
    ]
    response_id_hash = (
        "sha256:"
        + hashlib.sha256(b"response_123").hexdigest()[:16]
    )
    safe_details = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "response_model": "deepseek-chat-202607",
        "response_id": response_id_hash,
        "stop_reason": "stop",
        "tool_name": "emit_question",
        "schema_valid": True,
        "status": "ok",
        "schema_path": "$.atomic_claims[0].fact_refs",
        "error_code": "UNSUPPORTED_CLAIM",
        "fallback_reason": "provider_unavailable",
        "risk_flags": ["unsupported_numeric", "safe_flag"],
        "input_hash": "a" * 64,
        "prompt_template_version": "jd-parse@2",
        "source_event_type_hash": "b" * 16,
    }
    privacy_events.append(
        template.model_copy(
            update={
                "event_seq": len(privacy_events) + 1,
                "details": safe_details,
            }
        )
    )
    protected = receipt.model_copy(
        update={
            "run": receipt.run.model_copy(update={"events": tuple(privacy_events)})
        }
    )

    async with sql_session_factory.begin() as session:
        await AiRunService().persist_in_session(
            session,
            "usr_trace_privacy",
            protected,
            workflow_stage="parse",
        )
    async with sql_session_factory() as session:
        stored_events = list(
            (
                await session.scalars(
                    select(AiTraceEvent)
                    .where(AiTraceEvent.ai_run_id == protected.run.ai_run_id)
                    .order_by(AiTraceEvent.event_seq)
                )
            ).all()
        )

    serialized = json.dumps(
        [event.payload for event in stored_events],
        ensure_ascii=False,
    )
    for value in sensitive_values:
        assert value not in serialized
    for event in stored_events[:-1]:
        payload = event.payload or {}
        for field in (
            "model",
            "response_model",
            "response_id",
            "input_hash",
            "source_event_type_hash",
        ):
            protected_value = payload[field]
            assert isinstance(protected_value, str)
            assert protected_value.startswith("sha256:")
            assert len(protected_value) == 23
        assert payload.get("risk_flags") == []

    assert stored_events[-1].payload == safe_details


async def test_ai_run_top_level_audit_strings_are_sanitized_on_persist_and_replay(
    sql_session_factory,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_run_privacy"))
    tasks = TaskService(sql_session_factory)
    task = await tasks.create_task(
        "usr_run_privacy",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_receipt",
        idempotency_key="run-field-privacy",
        admission=TaskAdmission.ai(),
    )
    base = _receipt(task.id, "parse")
    malicious_values = (
        "deepseek-11010519491231002X-john@example.com",
        "gpt-13800138000",
        "faux-john_doe",
        "john.doe",
        "john-doe",
        "john@example.com",
    )
    malicious = base.model_copy(
        update={
            "run": base.run.model_copy(
                update={
                    "provider": malicious_values[0],
                    "requested_model": malicious_values[1],
                    "response_model": malicious_values[2],
                    "error_code": malicious_values[3],
                    "prompt_template_version": malicious_values[4],
                    "workflow_version": malicious_values[5],
                }
            )
        }
    )
    service = AiRunService()

    async with sql_session_factory.begin() as session:
        first = await service.persist_in_session(
            session,
            "usr_run_privacy",
            malicious,
            workflow_stage="parse",
        )
        replay = await service.persist_in_session(
            session,
            "usr_run_privacy",
            malicious,
            workflow_stage="parse",
        )
    async with sql_session_factory() as session:
        stored = await session.scalar(
            select(AiRun).where(AiRun.id == malicious.run.ai_run_id)
        )

    assert first.id == replay.id == malicious.run.ai_run_id
    assert stored is not None
    stored_text = "|".join(
        value or ""
        for value in (
            stored.provider,
            stored.workflow_version,
            stored.requested_model,
            stored.response_model,
            stored.error_code,
            stored.stop_reason,
            stored.prompt_template_version,
        )
    )
    for value in malicious_values:
        assert value not in stored_text
    assert stored.provider is None
    assert stored.requested_model == (
        "sha256:" + hashlib.sha256(malicious_values[1].encode()).hexdigest()[:16]
    )
    assert stored.response_model == (
        "sha256:" + hashlib.sha256(malicious_values[2].encode()).hexdigest()[:16]
    )
    assert stored.error_code is None
    assert stored.stop_reason is None
    assert stored.prompt_template_version == (
        "sha256:" + hashlib.sha256(malicious_values[4].encode()).hexdigest()[:16]
    )
    assert stored.workflow_version == (
        "sha256:" + hashlib.sha256(malicious_values[5].encode()).hexdigest()[:16]
    )


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


async def test_receipt_replay_trace_query_keeps_the_owner_predicate(
    sql_session_factory,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_trace_owner"))
    task = await TaskService(sql_session_factory).create_task(
        "usr_trace_owner",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_receipt",
        idempotency_key="trace-owner-query",
        admission=TaskAdmission.ai(),
    )
    receipt = _receipt(task.id, "parse")
    service = AiRunService()
    async with sql_session_factory.begin() as session:
        await service.persist_in_session(
            session,
            "usr_trace_owner",
            receipt,
            workflow_stage="parse",
        )

    statements: list[str] = []
    engine = sql_session_factory.kw["bind"].sync_engine

    def capture_statement(_connection, _cursor, statement, *_args):
        statements.append(statement)

    sqlalchemy_event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        async with sql_session_factory.begin() as session:
            await service.persist_in_session(
                session,
                "usr_trace_owner",
                receipt,
                workflow_stage="parse",
            )
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", capture_statement)

    trace_queries = [
        statement
        for statement in statements
        if "FROM ai_trace_events" in statement
    ]
    assert len(trace_queries) == 1
    assert "ai_trace_events.owner_user_id" in trace_queries[0]
