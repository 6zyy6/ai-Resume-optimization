from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import JsonValue

from app.core.ids import new_id
from app.db.models import AiRun, AiTraceEvent, Task
from app.integrations.ai_client import AiExecutionReceipt, TraceEvent, derive_ai_run_id


_SAFE_DETAIL_KEYS = frozenset(
    {
        "provider",
        "model",
        "response_model",
        "response_id",
        "stop_reason",
        "tool_name",
        "schema_valid",
        "status",
        "duration_ms",
        "latency_ms",
        "schema_path",
        "error_code",
        "risk_flags",
        "fallback_reason",
        "input_hash",
        "prompt_template_version",
        "input_length",
        "output_length",
        "source_event_type_hash",
        "usage",
    }
)
_NUMBER_DETAIL_KEYS = frozenset(
    {"duration_ms", "latency_ms", "input_length", "output_length"}
)
_USAGE_KEYS = frozenset(
    {
        "input",
        "output",
        "cache_read",
        "cache_write",
        "reasoning",
        "total_tokens",
        "cost_usd",
    }
)
_APPROVED_PROVIDERS = frozenset(
    {
        "anthropic",
        "deepseek",
        "faux",
        "google",
        "openai",
        "qwen-token-plan-cn",
        "test-faux",
    }
)
_APPROVED_MODEL_PREFIXES = (
    "claude-",
    "deepseek-",
    "faux-",
    "gemini-",
    "gpt-",
    "qwen-",
)
_APPROVED_STATUSES = frozenset(
    {"cancelled", "error", "failed", "ok", "queued", "running", "succeeded"}
)
_APPROVED_STOP_REASONS = frozenset(
    {"aborted", "error", "length", "stop", "tool_use"}
)
_APPROVED_TOOL_NAMES = frozenset(
    {
        "emit_fact_check_result",
        "emit_question",
        "emit_resume_suggestion",
        "get_confirmed_facts",
        "get_jd_requirements",
        "unknown",
    }
)
_APPROVED_CODES = frozenset(
    {
        "INVALID_JSON",
        "OUTPUT_REFERENCE_INVALID",
        "OUTPUT_SCHEMA_INVALID",
        "UNSUPPORTED_CLAIM",
        "absolute_claim",
        "already_terminal",
        "cost_limit_exceeded",
        "fact_validation_failed",
        "input_schema_invalid",
        "invalid_json",
        "model_route_unavailable",
        "owner_instance_lost",
        "output_reference_invalid",
        "output_schema_invalid",
        "prompt_version_unavailable",
        "provider_429",
        "provider_error",
        "provider_timeout",
        "provider_unavailable",
        "route_missing",
        "runtime_failed",
        "safe_flag",
        "schema_validation_failed",
        "timeout_exceeded",
        "token_limit_exceeded",
        "tool_limit_exceeded",
        "turn_limit_exceeded",
        "unknown_id",
        "unknown_tool",
        "unsupported_award",
        "unsupported_numeric",
        "unsupported_role",
        "unsupported_tool",
    }
)
_JSON_PATH = re.compile(
    r"\$(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[(?:0|[1-9]\d*)\]))*"
)
_CANONICAL_HASH = re.compile(r"[a-f0-9]{16,64}")
_PROTECTED_HASH = re.compile(r"sha256:[a-f0-9]{16}")
_PROMPT_TEMPLATE_VERSION = re.compile(
    r"[a-z][a-z0-9-]{0,63}@[0-9]+(?:\.[0-9]+)*"
)


class AiRunService:
    async def persist_in_session(
        self,
        session: AsyncSession,
        owner_id: str,
        receipt: AiExecutionReceipt,
        *,
        workflow_stage: str,
        result_ref: str | None = None,
    ) -> AiRun:
        run = receipt.run
        if workflow_stage != receipt.workflow_stage:
            raise ValueError("workflow stage does not match receipt workflow type")
        expected_id = derive_ai_run_id(run.task_id, workflow_stage, run.input_hash)
        if run.ai_run_id != expected_id:
            raise ValueError("AI receipt id does not match task, stage and input hash")
        _validate_events(receipt)
        task = await session.scalar(
            select(Task)
            .where(Task.id == run.task_id, Task.owner_user_id == owner_id)
            .with_for_update()
        )
        if task is None:
            raise ValueError("AI receipt task is not owned by the requested owner")
        if task.trace_id != run.trace_id:
            raise ValueError("AI receipt trace does not match its task trace")
        existing = await session.scalar(
            select(AiRun).where(
                AiRun.task_id == run.task_id,
                AiRun.owner_user_id == owner_id,
                AiRun.workflow_stage == workflow_stage,
                AiRun.input_hash == run.input_hash,
            )
        )
        if existing is not None:
            if existing.id != run.ai_run_id:
                raise ValueError("AI run stable key conflicts with another receipt")
            await _assert_replay_matches(
                session,
                existing,
                receipt,
                workflow_stage=workflow_stage,
                result_ref=result_ref,
            )
            return existing
        stored = AiRun(
            owner_user_id=owner_id,
            **_stored_run_values(
                receipt,
                workflow_stage=workflow_stage,
                result_ref=result_ref,
            ),
        )
        try:
            async with session.begin_nested():
                session.add(stored)
                await session.flush()
        except IntegrityError:
            replay = await session.scalar(
                select(AiRun).where(
                    AiRun.task_id == run.task_id,
                    AiRun.owner_user_id == owner_id,
                    AiRun.workflow_stage == workflow_stage,
                    AiRun.input_hash == run.input_hash,
                )
            )
            if replay is None or replay.id != run.ai_run_id:
                raise
            await _assert_replay_matches(
                session,
                replay,
                receipt,
                workflow_stage=workflow_stage,
                result_ref=result_ref,
            )
            return replay
        session.add_all(
            [
                AiTraceEvent(
                    id=new_id("aie"),
                    owner_user_id=owner_id,
                    ai_run_id=run.ai_run_id,
                    event_seq=event.event_seq,
                    event_type=event.event_type,
                    payload=_safe_details(event),
                    created_at=_datetime(event.occurred_at),
                )
                for event in run.events
            ]
        )
        await session.flush()
        return stored


def _validate_events(receipt: AiExecutionReceipt) -> None:
    run = receipt.run
    if not run.events:
        raise ValueError("AI receipt requires at least one trace event")
    for expected, event in enumerate(run.events, start=1):
        if event.event_seq != expected:
            raise ValueError("AI trace event sequence must start at 1 and be continuous")
        if (
            event.ai_run_id != run.ai_run_id
            or event.trace_id != run.trace_id
            or event.task_id != run.task_id
        ):
            raise ValueError("AI trace event references do not match its run")


def _safe_details(event: TraceEvent) -> dict[str, JsonValue] | None:
    if not event.details:
        return None
    safe: dict[str, JsonValue] = {}
    for key, value in event.details.items():
        if key not in _SAFE_DETAIL_KEYS:
            continue
        if key == "risk_flags" and isinstance(value, list):
            safe[key] = [
                normalized
                for item in value
                if (normalized := _safe_string_field(key, item))
            ]
        elif key in _NUMBER_DETAIL_KEYS and _is_finite_number(value):
            safe[key] = value
        elif key == "schema_valid" and isinstance(value, bool):
            safe[key] = value
        elif key == "usage" and isinstance(value, dict):
            safe[key] = {
                usage_key: usage_value
                for usage_key, usage_value in value.items()
                if usage_key in _USAGE_KEYS and _is_finite_number(usage_value)
            }
        elif (normalized := _safe_string_field(key, value)) is not None:
            safe[key] = normalized
    return safe or None


def _safe_string_field(key: str, value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if key == "provider":
        return value if value in _APPROVED_PROVIDERS else None
    if key in {"model", "response_model"}:
        if value.startswith(_APPROVED_MODEL_PREFIXES):
            return value[:256]
        return _short_hash(value)
    if key == "response_id":
        return _short_hash(value)
    if key == "status":
        return value if value in _APPROVED_STATUSES else None
    if key == "stop_reason":
        return value if value in _APPROVED_STOP_REASONS else None
    if key == "tool_name":
        return value if value in _APPROVED_TOOL_NAMES else None
    if key == "schema_path":
        return value if _JSON_PATH.fullmatch(value) is not None else None
    if key in {"error_code", "fallback_reason", "risk_flags"}:
        return value if value in _APPROVED_CODES else None
    if key in {"input_hash", "source_event_type_hash"}:
        if _CANONICAL_HASH.fullmatch(value) is not None:
            return value
        return _short_hash(value)
    if key == "prompt_template_version":
        if _PROMPT_TEMPLATE_VERSION.fullmatch(value) is not None:
            return value
        return _short_hash(value)
    return None


def _short_hash(value: str) -> str:
    if _PROTECTED_HASH.fullmatch(value) is not None:
        return value
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()[:16]}"


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _stored_run_values(
    receipt: AiExecutionReceipt,
    *,
    workflow_stage: str,
    result_ref: str | None,
) -> dict[str, object]:
    run = receipt.run
    return {
        "id": run.ai_run_id,
        "trace_id": run.trace_id,
        "task_id": run.task_id,
        "workflow_type": run.workflow_type,
        "workflow_version": run.workflow_version,
        "workflow_stage": workflow_stage,
        "status": run.status,
        "error_code": run.error_code,
        "provider": run.provider,
        "requested_model": run.requested_model,
        "response_model": run.response_model,
        "started_at": _datetime(run.started_at),
        "first_token_at": _datetime(run.first_token_at),
        "finished_at": _datetime(run.finished_at),
        "stop_reason": run.error_code or run.status,
        "input_tokens": run.usage.input,
        "output_tokens": run.usage.output,
        "cache_tokens": run.usage.cache_read + run.usage.cache_write,
        "reasoning_tokens": run.usage.reasoning,
        "provider_cost": run.usage.cost_usd,
        "cost_cny": Decimal(0),
        "turn_count": run.turn_count,
        "tool_count": run.tool_call_count,
        "schema_valid": run.schema_valid,
        "facts_valid": run.facts_valid,
        "retry_count": run.retry_count,
        "fallback_count": run.fallback_count,
        "result_ref": result_ref,
        "prompt_template_version": run.prompt_template_version,
        "input_hash": run.input_hash,
        "receipt_hash": _receipt_hash(receipt),
    }


async def _assert_replay_matches(
    session: AsyncSession,
    existing: AiRun,
    receipt: AiExecutionReceipt,
    *,
    workflow_stage: str,
    result_ref: str | None,
) -> None:
    expected = _stored_run_values(
        receipt,
        workflow_stage=workflow_stage,
        result_ref=result_ref,
    )
    for field, value in expected.items():
        actual = getattr(existing, field)
        if isinstance(value, datetime):
            actual = _as_utc(actual)
            value = _as_utc(value)
        if isinstance(value, Decimal):
            actual = Decimal(actual)
        if actual != value:
            raise ValueError(f"AI receipt replay conflict for {field}")
    events = list(
        (
            await session.scalars(
                select(AiTraceEvent)
                .where(
                    AiTraceEvent.ai_run_id == existing.id,
                    AiTraceEvent.owner_user_id == existing.owner_user_id,
                )
                .order_by(AiTraceEvent.event_seq)
            )
        ).all()
    )
    expected_events = [
        (
            event.event_seq,
            event.event_type,
            _safe_details(event),
            _as_utc(_datetime(event.occurred_at)),
        )
        for event in receipt.run.events
    ]
    actual_events = [
        (
            event.event_seq,
            event.event_type,
            event.payload,
            _as_utc(event.created_at),
        )
        for event in events
    ]
    if actual_events != expected_events:
        raise ValueError("AI receipt replay conflict for trace events")


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _receipt_hash(receipt: AiExecutionReceipt) -> str:
    canonical = json.dumps(
        receipt.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)
