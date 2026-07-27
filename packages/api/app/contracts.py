from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FactStatus(str, Enum):
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_FOR_USER = "waiting_for_user"


class MatchCategory(str, Enum):
    PROVED = "proved"
    UNDEREXPRESSED = "underexpressed"
    NEEDS_CONFIRMATION = "needs_confirmation"
    REAL_GAP = "real_gap"


class SuggestionStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EDITED = "edited"
    IGNORED = "ignored"
    REVERTED = "reverted"
    BLOCKED = "blocked"


class FactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    kind: str
    value: str
    status: FactStatus
    source_ids: list[str]
    confirmed_at: datetime | None


class ResumeBullet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    text: str
    fact_refs: list[str]


class ResumeSection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    type: str
    title: str
    items: list[ResumeBullet]


class ResumeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"]
    title: str
    target: str | None
    sections: list[ResumeSection]


class ApiErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str
    message: str
    request_id: str
    details: dict[str, Any]


class ApiErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    error: ApiErrorBody


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    type: str
    status: TaskStatus
    progress: int = Field(ge=0, le=100)
    stage: str
    trace_id: str
    result_ref: str | None
    error_code: str | None
