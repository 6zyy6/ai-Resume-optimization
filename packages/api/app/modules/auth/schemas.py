from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyRequest(StrictModel):
    pass


class ConsentInput(StrictModel):
    document_type: str = Field(min_length=1, max_length=64)
    document_version: str = Field(min_length=1, max_length=32)
    decision: Literal["accepted"]


class EmailStartRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)


class EmailVerifyRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")
    consent: ConsentInput | None = None


class WechatLoginRequest(StrictModel):
    code: str = Field(min_length=1, max_length=512)


class BindEmailRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")


class OtpStartedResponse(StrictModel):
    status: Literal["sent"]
    expires_in: int


class AuthenticatedResponse(StrictModel):
    user_id: str
    expires_at: datetime
