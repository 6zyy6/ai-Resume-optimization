from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.contracts import ResumeSnapshot


class ResumeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["base", "job_targeted"]
    title: str
    base_resume_id: str | None = None
    job_description_id: str | None = None

    @model_validator(mode="after")
    def base_resume_has_no_references(self) -> "ResumeCreate":
        if self.kind == "base" and (
            self.base_resume_id is not None or self.job_description_id is not None
        ):
            raise ValueError("Base resumes cannot reference a base resume or job description")
        return self


class ResumeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str


class ResumeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    kind: str
    title: str
    base_resume_id: str | None
    job_description_id: str | None
    version: int


class ResumeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[ResumeResponse]
    next_cursor: str | None = None


class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_version: int
    snapshot: ResumeSnapshot


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_version: int


class ResumeVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    resume_id: str
    parent_version_id: str | None
    snapshot: dict[str, Any]
    snapshot_hash: str
    operation: str
    created_at: datetime


class ResumeVersionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[ResumeVersionResponse]
    next_cursor: str | None = None


class QualityIssueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str
    path: str
    message: str


class QualityCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    issues: list[QualityIssueResponse]
