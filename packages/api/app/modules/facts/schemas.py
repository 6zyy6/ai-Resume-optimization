from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts import FactStatus


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_type: Literal["question_answer", "imported_resume", "user_edit", "user_confirmation"]
    content: str
    source_ref: str | None = None
    source_range: dict | None = None


class SourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_type: Literal["question_answer", "imported_resume", "user_edit", "user_confirmation", "fact_candidate_edit"]
    content: str
    source_ref: str | None = None
    source_range: dict | None = None


class FactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: str
    value: str
    status: FactStatus = Field(default=FactStatus.UNCONFIRMED, strict=False)
    sources: list[SourceInput] = Field(default_factory=list)


class FactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: str | None = None
    value: str | None = None


class FactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    kind: str
    value: str
    status: FactStatus
    source_ids: list[str]
    confirmed_at: datetime | None


class FactListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[FactResponse]
    next_cursor: str | None = None


class FactSourcesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[SourceResponse]
