from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Literal, Protocol, TypeAlias

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.workers.execution import HttpServiceError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def get(self, key: str, default: object = None) -> object:
        return getattr(self, key, default)


WorkflowType: TypeAlias = Literal[
    "analyze_intake_answer",
    "compose_resume_draft",
    "parse_jd",
    "match_resume_to_jd",
    "generate_suggestions_batch",
]
IdString: TypeAlias = Annotated[str, Field(min_length=1, max_length=128)]
HashString: TypeAlias = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"),
]
MAX_AI_TEXT_LENGTH = 20_000
TextString: TypeAlias = Annotated[
    str, Field(min_length=1, max_length=MAX_AI_TEXT_LENGTH)
]
ShortString: TypeAlias = Annotated[str, Field(min_length=1, max_length=64)]
TitleString: TypeAlias = Annotated[str, Field(min_length=1, max_length=512)]
ReasonString: TypeAlias = Annotated[str, Field(min_length=1, max_length=4_000)]
IdList: TypeAlias = Annotated[tuple[IdString, ...], Field(max_length=1_000)]
RiskList: TypeAlias = Annotated[tuple[IdString, ...], Field(max_length=100)]
TraceEventType: TypeAlias = Literal[
    "run_queued",
    "agent_start",
    "turn_start",
    "message_start",
    "first_token",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_end",
    "turn_end",
    "auto_retry_start",
    "auto_retry_end",
    "model_fallback",
    "schema_validation_failed",
    "fact_validation_failed",
    "agent_end",
    "agent_settled",
    "run_succeeded",
    "run_failed",
    "run_cancelled",
    "user_accepted",
    "user_edited",
    "user_ignored",
    "unknown",
]
WORKFLOW_STAGES: Mapping[WorkflowType, str] = {
    "analyze_intake_answer": "analysis",
    "compose_resume_draft": "draft",
    "parse_jd": "parse",
    "match_resume_to_jd": "match",
    "generate_suggestions_batch": "suggestions",
}


def workflow_stage_for(workflow_type: WorkflowType) -> str:
    return WORKFLOW_STAGES[workflow_type]


class SourceRange(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class FactProjection(StrictModel):
    id: IdString
    kind: ShortString
    value: TextString


class ComposeFact(FactProjection):
    source_hashes: tuple[HashString, ...] = Field(min_length=1, max_length=1_000)


class RequirementProjection(StrictModel):
    id: IdString
    category: Literal[
        "responsibility",
        "must_have",
        "nice_to_have",
        "implicit_capability",
    ]
    value: TextString


class AnalyzeIntakePayload(StrictModel):
    session_id_hash: HashString
    answer_id: IdString
    question_id: IdString
    question_reason: ReasonString
    answer_text: TextString
    answer_state: ShortString
    confirmed_facts: tuple[FactProjection, ...] = Field(max_length=1_000)
    covered_slots: IdList
    missing_slots: IdList
    asked_question_ids: IdList


class ExperienceGroup(StrictModel):
    title: TitleString
    fact_refs: IdList


class ComposeResumeDraftPayload(StrictModel):
    resume_title: TitleString
    experience_groups: tuple[ExperienceGroup, ...] = Field(max_length=1_000)
    confirmed_facts: tuple[ComposeFact, ...] = Field(max_length=1_000)
    allowed_section_types: tuple[ShortString, ...] = Field(
        min_length=1, max_length=64
    )


class ParseJdPayload(StrictModel):
    jd_text: TextString
    job_title: TitleString | None = None
    allowed_categories: tuple[
        Literal[
            "responsibility",
            "must_have",
            "nice_to_have",
            "implicit_capability",
        ],
        ...,
    ] = Field(min_length=1, max_length=4)

    @field_validator("job_title", mode="before")
    @classmethod
    def reject_explicit_null_job_title(cls, value: object) -> object:
        if value is None:
            raise ValueError("job_title must be omitted instead of null")
        return value


class MatchResumeToJdPayload(StrictModel):
    resume_version_id: IdString
    resume_snapshot_hash: HashString
    confirmed_facts: tuple[FactProjection, ...] = Field(max_length=1_000)
    confirmed_requirements: tuple[RequirementProjection, ...] = Field(max_length=1_000)


class SuggestionSource(StrictModel):
    requirement_ref: IdString
    category: Literal["transferable", "needs_evidence"]
    fact_refs: IdList
    target_path: TitleString
    original_hash: HashString
    original_text: TextString


class GenerateSuggestionsBatchPayload(StrictModel):
    matches: tuple[SuggestionSource, ...] = Field(max_length=1_000)
    confirmed_facts: tuple[FactProjection, ...] = Field(max_length=1_000)
    confirmed_requirements: tuple[RequirementProjection, ...] = Field(max_length=1_000)


class WorkflowRequestBase(StrictModel):
    workflow_version: Literal["2"] = "2"
    prompt_template_version: IdString
    trace_id: IdString
    task_id: IdString
    owner_scope_hash: HashString
    locale: Literal["zh-CN"] = "zh-CN"
    input_version: int = Field(ge=1)
    input_hash: HashString


class AnalyzeIntakeRequest(WorkflowRequestBase):
    workflow_type: Literal["analyze_intake_answer"]
    payload: AnalyzeIntakePayload


class ComposeResumeDraftRequest(WorkflowRequestBase):
    workflow_type: Literal["compose_resume_draft"]
    payload: ComposeResumeDraftPayload


class ParseJdRequest(WorkflowRequestBase):
    workflow_type: Literal["parse_jd"]
    payload: ParseJdPayload


class MatchResumeToJdRequest(WorkflowRequestBase):
    workflow_type: Literal["match_resume_to_jd"]
    payload: MatchResumeToJdPayload


class GenerateSuggestionsBatchRequest(WorkflowRequestBase):
    workflow_type: Literal["generate_suggestions_batch"]
    payload: GenerateSuggestionsBatchPayload


AiWorkflowRequest: TypeAlias = Annotated[
    AnalyzeIntakeRequest
    | ComposeResumeDraftRequest
    | ParseJdRequest
    | MatchResumeToJdRequest
    | GenerateSuggestionsBatchRequest,
    Field(discriminator="workflow_type"),
]
AI_WORKFLOW_REQUEST_ADAPTER = TypeAdapter(AiWorkflowRequest)


class AtomicClaim(StrictModel):
    text: TextString
    fact_refs: IdList
    claim_order: int = Field(ge=0)


class FactCandidate(StrictModel):
    kind: ShortString
    value: TextString
    source_answer_id: IdString
    source_range: SourceRange
    risk_flags: RiskList


class QuestionCandidate(StrictModel):
    reason: ReasonString
    slot: IdString
    text: ReasonString
    related_fact_refs: IdList


class AnalyzeIntakeResult(StrictModel):
    fact_candidates: tuple[FactCandidate, ...] = Field(max_length=1_000)
    missing_slots: IdList
    question_candidate: QuestionCandidate | None


class ResumeBullet(StrictModel):
    text: TextString
    atomic_claims: tuple[AtomicClaim, ...] = Field(max_length=1_000)
    risk_flags: RiskList


class ResumeSectionResult(StrictModel):
    type: ShortString
    title: TitleString
    bullets: tuple[ResumeBullet, ...] = Field(max_length=1_000)


class ComposeResumeDraftResult(StrictModel):
    sections: tuple[ResumeSectionResult, ...] = Field(max_length=1_000)


class ParsedRequirement(StrictModel):
    category: Literal[
        "responsibility",
        "must_have",
        "nice_to_have",
        "implicit_capability",
    ]
    priority: Literal[1, 2, 3]
    value: TextString
    source_range: SourceRange
    explicitness: Literal["explicit", "implicit"]
    confidence_band: Literal["high", "medium", "low"]


class ParseJdResult(StrictModel):
    requirements: tuple[ParsedRequirement, ...] = Field(max_length=1_000)


class MatchResultItem(StrictModel):
    requirement_ref: IdString
    category: Literal["direct", "transferable", "needs_evidence", "gap"]
    fact_refs: IdList
    resume_target_paths: tuple[TitleString, ...] = Field(max_length=1_000)
    reason_code: IdString


class MatchResumeToJdResult(StrictModel):
    matches: tuple[MatchResultItem, ...] = Field(max_length=1_000)


class SuggestionResultItem(StrictModel):
    target_path: TitleString
    original_hash: HashString
    suggested_text: TextString
    atomic_claims: tuple[AtomicClaim, ...] = Field(max_length=1_000)
    requirement_ref: IdString
    reason: ReasonString
    risk_flags: RiskList
    proposed_status: Literal["pending", "blocked"]


class GenerateSuggestionsBatchResult(StrictModel):
    suggestions: tuple[SuggestionResultItem, ...] = Field(max_length=1_000)


WorkflowResult: TypeAlias = (
    AnalyzeIntakeResult
    | ComposeResumeDraftResult
    | ParseJdResult
    | MatchResumeToJdResult
    | GenerateSuggestionsBatchResult
)


class TraceUsage(StrictModel):
    input: int = Field(ge=0)
    output: int = Field(ge=0)
    cache_read: int = Field(ge=0)
    cache_write: int = Field(ge=0)
    reasoning: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(
        ge=0,
        le=Decimal("999999"),
        max_digits=24,
        decimal_places=18,
    )


class TraceEvent(StrictModel):
    ai_run_id: IdString
    trace_id: IdString
    task_id: IdString
    event_seq: int = Field(ge=1)
    event_type: TraceEventType
    occurred_at: IdString
    details: dict[str, JsonValue] | None = None


class WorkflowRun(StrictModel):
    ai_run_id: IdString
    trace_id: IdString
    task_id: IdString
    workflow_type: WorkflowType
    workflow_version: IdString
    prompt_template_version: IdString
    status: Literal["succeeded", "failed", "cancelled"]
    error_code: str | None
    provider: str | None
    requested_model: str | None
    response_model: str | None
    started_at: str
    first_token_at: str | None
    finished_at: str
    usage: TraceUsage
    events: tuple[TraceEvent, ...]
    turn_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    fallback_count: int = Field(ge=0)
    schema_valid: bool
    facts_valid: bool
    input_hash: HashString
    exportable: bool
    risk_flags: RiskList


_RESULT_TYPES: dict[str, type[StrictModel]] = {
    "analyze_intake_answer": AnalyzeIntakeResult,
    "compose_resume_draft": ComposeResumeDraftResult,
    "parse_jd": ParseJdResult,
    "match_resume_to_jd": MatchResumeToJdResult,
    "generate_suggestions_batch": GenerateSuggestionsBatchResult,
}


class AiExecutionReceipt(StrictModel):
    run: WorkflowRun
    result: WorkflowResult | None = None

    @property
    def workflow_stage(self) -> str:
        return workflow_stage_for(self.run.workflow_type)

    @model_validator(mode="after")
    def validate_terminal_result(self) -> AiExecutionReceipt:
        if self.result is not None:
            expected = _RESULT_TYPES[self.run.workflow_type]
            if not isinstance(self.result, expected):
                raise ValueError("receipt result does not match workflow_type")
        if self.run.status != "succeeded" and self.result is not None:
            raise ValueError("failed or cancelled receipt cannot contain a result")
        return self

    def __getitem__(self, key: str) -> object:
        if key not in {"run", "result"}:
            raise KeyError(key)
        return getattr(self, key)


def derive_ai_run_id(task_id: str, workflow_stage: str, input_hash: str) -> str:
    value = f"{task_id}:{workflow_stage}:{input_hash}".encode()
    return f"run_{hashlib.sha256(value).hexdigest()[:40]}"


class AiCancellation(Protocol):
    async def register_run(self, ai_run_id: str) -> bool: ...

    async def is_cancel_requested(self) -> bool: ...

    async def acknowledge_cancel(self, ai_run_id: str) -> None: ...


class AiRunCancelled(Exception):
    pass


class AiProtocolError(RuntimeError):
    pass


class AiClient(Protocol):
    async def run(
        self,
        input: AiWorkflowRequest,
        cancellation: AiCancellation | None = None,
    ) -> AiExecutionReceipt: ...


class FixtureAiClient:
    def __init__(self, fixtures: Mapping[str, AiExecutionReceipt]) -> None:
        self.fixtures = fixtures

    async def run(
        self,
        input: AiWorkflowRequest,
        cancellation: AiCancellation | None = None,
    ) -> AiExecutionReceipt:
        del cancellation
        fixture = self.fixtures[input.workflow_type]
        if not isinstance(fixture, AiExecutionReceipt):
            raise TypeError("AI fixture must be an AiExecutionReceipt")
        return fixture


class LegacyAiClientAdapter:
    """Isolates the V1 keyword contract until the old business services migrate."""

    def __init__(self, delegate: object) -> None:
        self.delegate = delegate

    async def run(self, **legacy: JsonValue) -> object:
        if isinstance(self.delegate, InternalAiClient):
            return await self._run_internal(self.delegate, **legacy)
        if isinstance(self.delegate, FixtureAiClient):
            fixture = self.delegate.fixtures[str(legacy["workflow_type"])]
            if isinstance(fixture, AiExecutionReceipt):
                return {
                    "result": (
                        fixture.result.model_dump(mode="json")
                        if fixture.result is not None
                        else None
                    ),
                    "run": fixture.run.model_dump(mode="json"),
                }
            if isinstance(fixture, Mapping):
                return fixture
            raise TypeError("Legacy AI fixture must be a mapping")
        run = getattr(self.delegate, "run")
        return await run(**legacy)

    async def _run_internal(
        self,
        delegate: InternalAiClient,
        *,
        cancellation: AiCancellation | None = None,
        **legacy: JsonValue,
    ) -> object:
        trace_id = str(legacy["trace_id"])
        task_id = str(legacy["task_id"])
        facts = legacy.get("facts", [])
        input_data = legacy.get("input_data", {})
        assert isinstance(input_data, dict)
        jd_requirements = input_data.get("jd_requirements", [])
        current_object = {
            key: value
            for key, value in input_data.items()
            if key not in {"jd_requirements", "locale", "target"}
        }
        payload: dict[str, JsonValue] = {
            "workflow_type": str(legacy["workflow_type"]),
            "workflow_version": str(legacy["workflow_version"]),
            "trace_id": trace_id,
            "task_id": task_id,
            "locale": input_data.get("locale", "zh-CN"),
            "target": input_data.get("target", "resume"),
            "confirmed_facts": facts,
            "jd_requirements": [
                {
                    "id": item["id"],
                    "category": item.get("category", item.get("type", "other")),
                    "value": item.get("value", item.get("text", "")),
                }
                for item in jd_requirements
                if isinstance(item, dict)
            ],
            "current_object": current_object,
        }
        headers = {
            "Authorization": f"Bearer {delegate.service_token}",
            "X-Trace-Id": trace_id,
        }
        async with httpx.AsyncClient(
            base_url=delegate.base_url,
            timeout=min(2.0, delegate.timeout_seconds),
            transport=delegate.transport,
            trust_env=False,
        ) as client:
            try:
                response = await client.post(
                    "/internal/v1/runs", json=payload, headers=headers
                )
                response.raise_for_status()
                ai_run_id = str(response.json()["ai_run_id"])
                if cancellation is not None and not await cancellation.register_run(
                    ai_run_id
                ):
                    await _cancel_run(client, ai_run_id, headers)
                    await cancellation.acknowledge_cancel(ai_run_id)
                    raise AiRunCancelled("AI run cancelled before registration")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + delegate.timeout_seconds
                while loop.time() < deadline:
                    if (
                        cancellation is not None
                        and await cancellation.is_cancel_requested()
                    ):
                        await _cancel_run(client, ai_run_id, headers)
                        await cancellation.acknowledge_cancel(ai_run_id)
                        raise AiRunCancelled("AI run cancelled by task owner")
                    status_response = await client.get(
                        f"/internal/v1/runs/{ai_run_id}", headers=headers
                    )
                    status_response.raise_for_status()
                    run = status_response.json()["run"]
                    if run["status"] == "succeeded":
                        return {"result": run.get("output"), "run": run}
                    if run["status"] == "failed":
                        _raise_terminal_failure(
                            str(run.get("error_code", "unknown"))
                        )
                    if run["status"] == "cancelled":
                        if cancellation is not None:
                            await cancellation.acknowledge_cancel(ai_run_id)
                        raise AiRunCancelled(
                            "AI_RUN_CANCELLED: "
                            f"{run.get('error_code', 'unknown')}"
                        )
                    await asyncio.sleep(delegate.poll_interval_seconds)
                raise TimeoutError("AI internal run timed out")
            except httpx.HTTPStatusError as error:
                raise HttpServiceError(error.response.status_code) from error
            except (httpx.TimeoutException, httpx.TransportError) as error:
                raise TimeoutError("AI internal transport failed") from error


class InternalAiClient:
    def __init__(
        self,
        base_url: str,
        service_token: str,
        *,
        timeout_seconds: float = 35,
        poll_interval_seconds: float = 0.1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = min(0.5, max(0.01, poll_interval_seconds))
        self.transport = transport

    async def run(
        self,
        input: AiWorkflowRequest,
        cancellation: AiCancellation | None = None,
    ) -> AiExecutionReceipt:
        ai_run_id = derive_ai_run_id(
            input.task_id,
            workflow_stage_for(input.workflow_type),
            input.input_hash,
        )
        headers = {
            "Authorization": f"Bearer {self.service_token}",
            "X-Trace-Id": input.trace_id,
        }
        payload = {
            "ai_run_id": ai_run_id,
            "input": input.model_dump(mode="json", exclude_none=True),
        }
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=min(2.0, self.timeout_seconds),
            transport=self.transport,
            trust_env=False,
        ) as client:
            return await self._execute(client, payload, headers, ai_run_id, cancellation)

    async def _execute(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, JsonValue],
        headers: dict[str, str],
        ai_run_id: str,
        cancellation: AiCancellation | None,
    ) -> AiExecutionReceipt:
        run_settled = False
        cancel_sent = False
        try:
            try:
                response = await client.post("/internal/v1/runs", json=payload, headers=headers)
                response.raise_for_status()
                cancel_requested = False
                if cancellation is not None:
                    should_continue = await cancellation.register_run(ai_run_id)
                    if not should_continue:
                        cancel_requested = True
                body = _response_json(response)
                receipt = _receipt_from_body(body)
                if receipt is not None:
                    _validate_receipt_id(receipt, ai_run_id)
                    run_settled = True
                    if receipt.run.status == "cancelled" and cancellation is not None:
                        await _acknowledge_terminal_cancel(
                            cancellation,
                            ai_run_id,
                        )
                    return receipt
                _validate_summary(body, ai_run_id)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.timeout_seconds
                while loop.time() < deadline:
                    if (
                        cancellation is not None
                        and not cancel_requested
                        and await cancellation.is_cancel_requested()
                    ):
                        cancel_requested = True
                    if cancel_requested and not cancel_sent:
                        await _cancel_run(client, ai_run_id, headers)
                        cancel_sent = True
                    status_response = await client.get(
                        f"/internal/v1/runs/{ai_run_id}", headers=headers
                    )
                    status_response.raise_for_status()
                    status_body = _response_json(status_response)
                    receipt = _receipt_from_body(status_body)
                    if receipt is not None:
                        _validate_receipt_id(receipt, ai_run_id)
                        run_settled = True
                        if receipt.run.status == "cancelled" and cancellation is not None:
                            await _acknowledge_terminal_cancel(
                                cancellation,
                                ai_run_id,
                            )
                        return receipt
                    _validate_summary(status_body, ai_run_id)
                    await asyncio.sleep(self.poll_interval_seconds)
                raise TimeoutError("AI internal run timed out")
            finally:
                if not run_settled and not cancel_sent:
                    try:
                        await _cancel_run(client, ai_run_id, headers)
                    except (httpx.HTTPError, TimeoutError):
                        pass
        except httpx.HTTPStatusError as error:
            raise HttpServiceError(error.response.status_code) from error
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise TimeoutError("AI internal transport failed") from error


def _response_json(response: httpx.Response) -> object:
    try:
        return response.json(parse_float=Decimal)
    except ValueError as error:
        raise AiProtocolError("AI response body is invalid JSON") from error


def _receipt_from_body(body: object) -> AiExecutionReceipt | None:
    if not isinstance(body, dict):
        raise AiProtocolError("AI response body must be an object")
    value = body.get("receipt")
    if value is None:
        return None
    try:
        return AiExecutionReceipt.model_validate_json(
            json.dumps(
                _decimal_strings(value),
                separators=(",", ":"),
            )
        )
    except ValueError as error:
        raise AiProtocolError("AI terminal receipt is invalid") from error


def _decimal_strings(value: object, path: tuple[str, ...] = ()) -> object:
    if isinstance(value, Decimal):
        if path == ("run", "usage", "cost_usd"):
            return format(value, "f")
        return float(value)
    if isinstance(value, dict):
        return {
            key: _decimal_strings(item, (*path, key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_decimal_strings(item, path) for item in value]
    return value


def _validate_summary(body: object, expected_id: str) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("run"), dict):
        raise AiProtocolError("AI response has neither receipt nor run summary")
    run = body["run"]
    if run.get("ai_run_id") != expected_id or run.get("status") not in {"queued", "running"}:
        raise AiProtocolError("AI run summary is invalid")


def _validate_receipt_id(receipt: AiExecutionReceipt, expected_id: str) -> None:
    if receipt.run.ai_run_id != expected_id:
        raise AiProtocolError("AI receipt run id does not match request")


def _raise_terminal_failure(error_code: str) -> None:
    normalized = error_code.lower()
    if normalized == "provider_429":
        raise HttpServiceError(429)
    if normalized in {"provider_unavailable", "provider_error"}:
        raise HttpServiceError(503)
    if normalized == "provider_timeout":
        raise TimeoutError("AI provider timed out")
    raise RuntimeError(f"AI_RUN_FAILED: {error_code}")


async def _cancel_run(
    client: httpx.AsyncClient,
    ai_run_id: str,
    headers: dict[str, str],
) -> None:
    response = await client.post(f"/internal/v1/runs/{ai_run_id}/cancel", headers=headers)
    if response.status_code not in {202, 409}:
        response.raise_for_status()


async def _acknowledge_terminal_cancel(
    cancellation: AiCancellation,
    ai_run_id: str,
) -> None:
    try:
        await cancellation.acknowledge_cancel(ai_run_id)
    except Exception:
        # The terminal Pi receipt remains the authoritative result even if the
        # task acknowledgement races with lease or owner-state changes.
        return
