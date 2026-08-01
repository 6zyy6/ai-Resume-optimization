from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import JsonValue

from app.core.ids import new_id
from app.db.models import AiRun, AiTraceEvent, Task, UsageLedger
from app.integrations.ai_client import AiExecutionReceipt, TraceEvent, derive_ai_run_id


_SENSITIVE_DETAIL_KEYS = frozenset(
    {
        "answer_text",
        "authorization",
        "confirmed_facts",
        "current_object",
        "job_description",
        "jd_requirements",
        "jd_text",
        "payload",
        "prompt",
        "provider_raw_response",
        "provider_response",
        "raw_response",
        "request",
        "reasoning",
        "resume_snapshot",
        "secret",
        "thinking",
        "token",
        "user_body",
        "user_input",
    }
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
            return existing
        reservation_cost = await session.scalar(
            select(UsageLedger.cost_cny).where(
                UsageLedger.task_id == run.task_id,
                UsageLedger.owner_user_id == owner_id,
                UsageLedger.usage_type == "ai_task",
            )
        )
        stored = AiRun(
            id=run.ai_run_id,
            owner_user_id=owner_id,
            trace_id=run.trace_id,
            task_id=run.task_id,
            workflow_type=run.workflow_type,
            workflow_version=run.workflow_version,
            workflow_stage=workflow_stage,
            status=run.status,
            error_code=run.error_code,
            provider=run.provider,
            requested_model=run.requested_model,
            response_model=run.response_model,
            started_at=_datetime(run.started_at),
            first_token_at=_datetime(run.first_token_at),
            finished_at=_datetime(run.finished_at),
            stop_reason=run.error_code or run.status,
            input_tokens=run.usage.input,
            output_tokens=run.usage.output,
            cache_tokens=run.usage.cache_read + run.usage.cache_write,
            reasoning_tokens=run.usage.reasoning,
            provider_cost=run.usage.cost_usd,
            cost_cny=Decimal(reservation_cost or 0),
            turn_count=run.turn_count,
            tool_count=run.tool_call_count,
            schema_valid=run.schema_valid,
            facts_valid=run.facts_valid,
            retry_count=run.retry_count,
            fallback_count=run.fallback_count,
            result_ref=result_ref,
            prompt_template_version=run.prompt_template_version,
            input_hash=run.input_hash,
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
    safe = _sanitize_mapping(event.details)
    return safe or None


def _sanitize_mapping(values: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        key: _sanitize_value(value)
        for key, value in values.items()
        if key.lower() not in _SENSITIVE_DETAIL_KEYS
        and not key.lower().endswith(
            ("_body", "_prompt", "_response", "_secret", "_key", "_token")
        )
    }


def _sanitize_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
