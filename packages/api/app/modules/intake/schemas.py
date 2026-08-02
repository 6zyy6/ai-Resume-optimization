from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntakeStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    restart: bool = False


class IntakeQuestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    type: Literal["short_answer", "deep_answer"]
    prompt: str
    reason: Literal["conflict", "missing_unit", "ambiguous_role"] | None = None


class IntakeFactSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    kind: str
    value: str
    status: Literal["unconfirmed", "confirmed", "rejected"]


class IntakeFactCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    intake_answer_id: str
    kind: str
    value: str
    source_excerpt: str
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["pending", "accepted", "edited", "rejected"]
    decision_mode: Literal["accept_or_edit", "edit_only"]
    ai_run_id: str

    @model_validator(mode="after")
    def source_range_is_ordered(self) -> "IntakeFactCandidateResponse":
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        return self


class IntakeSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    status: Literal["active", "drafting", "completed", "abandoned"]
    version: int
    current_question: IntakeQuestionResponse | None
    completed_count: int
    remaining_estimate: int
    answered_question_ids: list[str]
    skipped_question_ids: list[str]
    fact_summaries: list[IntakeFactSummary]
    fact_candidates: list[IntakeFactCandidateResponse]
    analysis_task_id: str | None
    analysis_status: Literal[
        "idle",
        "queued",
        "running",
        "waiting_for_confirmation",
        "failed",
        "completed",
    ]
    task_id: str | None
    resume_id: str | None


class IntakeAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question_id: str = Field(min_length=1, max_length=64)
    answer: str | None = Field(default=None, min_length=1, max_length=1000)
    skipped: bool = False
    base_version: int = Field(ge=0)

    @model_validator(mode="after")
    def answer_or_skip(self) -> "IntakeAnswerRequest":
        if self.skipped == (self.answer is not None):
            raise ValueError("Provide an answer or set skipped=true")
        return self


class IntakeDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_version: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=255)
    generation_mode: Literal["model", "rule_fallback"]


class IntakeDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str
    task_id: str
    status: Literal["queued"]
    version: int


class FactCandidateDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["accept", "edit", "reject"]
    value: str | None = Field(default=None, min_length=1, max_length=1000)
    base_version: int = Field(ge=0)

    @model_validator(mode="after")
    def value_matches_decision(self) -> "FactCandidateDecisionRequest":
        if (self.decision == "edit") != (self.value is not None):
            raise ValueError("Only edit requires a value")
        return self


class FactCandidateDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_id: str
    status: Literal["accepted", "edited", "rejected"]
    fact_summary: IntakeFactSummary | None
    session_version: int
    current_question: IntakeQuestionResponse | None


class IntakeAnalysisActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_version: int = Field(ge=0)
