from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Literal, Protocol, TypeAlias

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from app.workers.execution import HttpServiceError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def get(self, key: str, default: object = None) -> object:
        return getattr(self, key, default)


WorkflowType: TypeAlias = Literal[
    "analyze_intake_answer",
    "compose_resume_draft",
    "parse_jd",
    "match_resume_to_jd",
    "generate_suggestions_batch",
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
    id: str
    kind: str
    value: str


class ComposeFact(FactProjection):
    source_hashes: tuple[str, ...] = Field(min_length=1)


class RequirementProjection(StrictModel):
    id: str
    category: Literal[
        "responsibility",
        "must_have",
        "nice_to_have",
        "implicit_capability",
    ]
    value: str


class AnalyzeIntakePayload(StrictModel):
    session_id_hash: str
    answer_id: str
    question_id: str
    question_reason: str
    answer_text: str
    answer_state: str
    confirmed_facts: tuple[FactProjection, ...]
    covered_slots: tuple[str, ...]
    missing_slots: tuple[str, ...]
    asked_question_ids: tuple[str, ...]


class ExperienceGroup(StrictModel):
    title: str
    fact_refs: tuple[str, ...]


class ComposeResumeDraftPayload(StrictModel):
    resume_title: str
    experience_groups: tuple[ExperienceGroup, ...]
    confirmed_facts: tuple[ComposeFact, ...]
    allowed_section_types: tuple[str, ...] = Field(min_length=1)


class ParseJdPayload(StrictModel):
    jd_text: str
    job_title: str | None = None
    allowed_categories: tuple[
        Literal[
            "responsibility",
            "must_have",
            "nice_to_have",
            "implicit_capability",
        ],
        ...,
    ] = Field(min_length=1, max_length=4)


class MatchResumeToJdPayload(StrictModel):
    resume_version_id: str
    resume_snapshot_hash: str
    confirmed_facts: tuple[FactProjection, ...]
    confirmed_requirements: tuple[RequirementProjection, ...]


class SuggestionSource(StrictModel):
    requirement_ref: str
    category: Literal["transferable", "needs_evidence"]
    fact_refs: tuple[str, ...]
    target_path: str
    original_hash: str
    original_text: str


class GenerateSuggestionsBatchPayload(StrictModel):
    matches: tuple[SuggestionSource, ...] = Field(min_length=1)
    confirmed_facts: tuple[FactProjection, ...]
    confirmed_requirements: tuple[RequirementProjection, ...]


class WorkflowRequestBase(StrictModel):
    workflow_version: Literal["2"] = "2"
    prompt_template_version: str
    trace_id: str
    task_id: str
    owner_scope_hash: str
    locale: Literal["zh-CN"] = "zh-CN"
    input_version: int = Field(ge=1)
    input_hash: str


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
    text: str
    fact_refs: tuple[str, ...]
    claim_order: int = Field(ge=0)


class FactCandidate(StrictModel):
    kind: str
    value: str
    source_answer_id: str
    source_range: SourceRange
    risk_flags: tuple[str, ...]


class QuestionCandidate(StrictModel):
    reason: str
    slot: str
    text: str
    related_fact_refs: tuple[str, ...]


class AnalyzeIntakeResult(StrictModel):
    fact_candidates: tuple[FactCandidate, ...]
    missing_slots: tuple[str, ...]
    question_candidate: QuestionCandidate | None


class ResumeBullet(StrictModel):
    text: str
    atomic_claims: tuple[AtomicClaim, ...]
    risk_flags: tuple[str, ...]


class ResumeSectionResult(StrictModel):
    type: str
    title: str
    bullets: tuple[ResumeBullet, ...]


class ComposeResumeDraftResult(StrictModel):
    sections: tuple[ResumeSectionResult, ...]


class ParsedRequirement(StrictModel):
    category: Literal[
        "responsibility",
        "must_have",
        "nice_to_have",
        "implicit_capability",
    ]
    priority: Literal[1, 2, 3]
    value: str
    source_range: SourceRange
    explicitness: Literal["explicit", "implicit"]
    confidence_band: Literal["high", "medium", "low"]


class ParseJdResult(StrictModel):
    requirements: tuple[ParsedRequirement, ...]


class MatchResultItem(StrictModel):
    requirement_ref: str
    category: Literal["direct", "transferable", "needs_evidence", "gap"]
    fact_refs: tuple[str, ...]
    resume_target_paths: tuple[str, ...]
    reason_code: str


class MatchResumeToJdResult(StrictModel):
    matches: tuple[MatchResultItem, ...]


class SuggestionResultItem(StrictModel):
    target_path: str
    original_hash: str
    suggested_text: str
    atomic_claims: tuple[AtomicClaim, ...]
    requirement_ref: str
    reason: str
    risk_flags: tuple[str, ...]
    proposed_status: Literal["pending", "blocked"]


class GenerateSuggestionsBatchResult(StrictModel):
    suggestions: tuple[SuggestionResultItem, ...]


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
    cost_usd: Decimal = Field(ge=0)


class TraceEvent(StrictModel):
    ai_run_id: str
    trace_id: str
    task_id: str
    event_seq: int = Field(ge=1)
    event_type: str
    occurred_at: str
    details: dict[str, JsonValue] | None = None


class WorkflowRun(StrictModel):
    ai_run_id: str
    trace_id: str
    task_id: str
    workflow_type: WorkflowType
    workflow_version: str
    prompt_template_version: str
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
    input_hash: str
    exportable: bool
    risk_flags: tuple[str, ...]


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
    def __init__(self, fixtures: Mapping[str, object]) -> None:
        self.fixtures = fixtures

    async def run(
        self,
        input: AiWorkflowRequest | None = None,
        cancellation: AiCancellation | None = None,
        **legacy: JsonValue,
    ) -> AiExecutionReceipt:
        del cancellation
        workflow_type = input.workflow_type if input is not None else str(legacy["workflow_type"])
        fixture = self.fixtures[workflow_type]
        if isinstance(fixture, AiExecutionReceipt):
            return fixture
        if not isinstance(fixture, Mapping):
            raise TypeError("AI fixture must be an AiExecutionReceipt")
        try:
            return AiExecutionReceipt.model_validate(fixture)
        except ValueError:
            # Temporary compatibility for pre-V2 callers removed by Tasks 4-7.
            return fixture  # type: ignore[return-value]


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
        input: AiWorkflowRequest | None = None,
        cancellation: AiCancellation | None = None,
        **legacy: JsonValue,
    ) -> AiExecutionReceipt:
        if input is None:
            return await self._run_legacy(  # type: ignore[return-value]
                cancellation=cancellation,
                **legacy,
            )
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
            "input": input.model_dump(mode="json"),
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
        try:
            try:
                response = await client.post("/internal/v1/runs", json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
                if cancellation is not None:
                    should_continue = await cancellation.register_run(ai_run_id)
                    if not should_continue:
                        await _cancel_run(client, ai_run_id, headers)
                        await cancellation.acknowledge_cancel(ai_run_id)
                        run_settled = True
                        raise AiRunCancelled("AI run cancelled before registration")
                receipt = _receipt_from_body(body)
                if receipt is not None:
                    _validate_receipt_id(receipt, ai_run_id)
                    run_settled = True
                    if receipt.run.status == "cancelled" and cancellation is not None:
                        await cancellation.acknowledge_cancel(ai_run_id)
                    return receipt
                _validate_summary(body, ai_run_id)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.timeout_seconds
                while loop.time() < deadline:
                    if cancellation is not None and await cancellation.is_cancel_requested():
                        await _cancel_run(client, ai_run_id, headers)
                        await cancellation.acknowledge_cancel(ai_run_id)
                        run_settled = True
                        raise AiRunCancelled("AI run cancelled by task owner")
                    status_response = await client.get(
                        f"/internal/v1/runs/{ai_run_id}", headers=headers
                    )
                    status_response.raise_for_status()
                    status_body = status_response.json()
                    receipt = _receipt_from_body(status_body)
                    if receipt is not None:
                        _validate_receipt_id(receipt, ai_run_id)
                        run_settled = True
                        if receipt.run.status == "cancelled" and cancellation is not None:
                            await cancellation.acknowledge_cancel(ai_run_id)
                        return receipt
                    _validate_summary(status_body, ai_run_id)
                    await asyncio.sleep(self.poll_interval_seconds)
                raise TimeoutError("AI internal run timed out")
            finally:
                if not run_settled:
                    try:
                        await _cancel_run(client, ai_run_id, headers)
                    except (httpx.HTTPError, TimeoutError):
                        pass
        except httpx.HTTPStatusError as error:
            raise HttpServiceError(error.response.status_code) from error
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise TimeoutError("AI internal transport failed") from error

    async def _run_legacy(
        self,
        *,
        cancellation: AiCancellation | None = None,
        **legacy: JsonValue,
    ) -> object:
        workflow_type = str(legacy["workflow_type"])
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
            "workflow_type": workflow_type,
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
            "Authorization": f"Bearer {self.service_token}",
            "X-Trace-Id": trace_id,
        }
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=min(2.0, self.timeout_seconds),
            transport=self.transport,
            trust_env=False,
        ) as client:
            try:
                response = await client.post("/internal/v1/runs", json=payload, headers=headers)
                response.raise_for_status()
                ai_run_id = str(response.json()["ai_run_id"])
                if cancellation is not None and not await cancellation.register_run(ai_run_id):
                    await _cancel_run(client, ai_run_id, headers)
                    await cancellation.acknowledge_cancel(ai_run_id)
                    raise AiRunCancelled("AI run cancelled before registration")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.timeout_seconds
                while loop.time() < deadline:
                    if cancellation is not None and await cancellation.is_cancel_requested():
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
                        _raise_terminal_failure(str(run.get("error_code", "unknown")))
                    if run["status"] == "cancelled":
                        if cancellation is not None:
                            await cancellation.acknowledge_cancel(ai_run_id)
                        raise AiRunCancelled(
                            "AI_RUN_CANCELLED: "
                            f"{run.get('error_code', 'unknown')}"
                        )
                    await asyncio.sleep(self.poll_interval_seconds)
                raise TimeoutError("AI internal run timed out")
            except httpx.HTTPStatusError as error:
                raise HttpServiceError(error.response.status_code) from error
            except (httpx.TimeoutException, httpx.TransportError) as error:
                raise TimeoutError("AI internal transport failed") from error


def _receipt_from_body(body: object) -> AiExecutionReceipt | None:
    if not isinstance(body, dict):
        raise AiProtocolError("AI response body must be an object")
    value = body.get("receipt")
    if value is None:
        return None
    try:
        return AiExecutionReceipt.model_validate(value)
    except ValueError as error:
        raise AiProtocolError("AI terminal receipt is invalid") from error


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
