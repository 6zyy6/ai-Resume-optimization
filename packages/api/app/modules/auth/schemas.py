from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyRequest(StrictModel):
    pass


class ConsentInput(StrictModel):
    document_type: Literal["user_agreement", "privacy_policy"]
    document_version: str = Field(min_length=1, max_length=32)
    decision: Literal["accepted"]


class EmailStartRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)


class EmailVerifyRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")
    consents: list[ConsentInput] | None = Field(default=None, max_length=2)


class PasswordRegisterRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")
    password: str = Field(min_length=8, max_length=128)
    consents: list[ConsentInput] | None = Field(default=None, max_length=2)


class PasswordLoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    consents: list[ConsentInput] | None = Field(default=None, max_length=2)


class WechatLoginRequest(StrictModel):
    code: str = Field(min_length=1, max_length=512)
    consents: list[ConsentInput] | None = Field(default=None, max_length=2)


class BindEmailRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")
    confirm_merge: bool = False


class OtpStartedResponse(StrictModel):
    status: Literal["sent"]
    expires_in: int


class AuthenticatedResponse(StrictModel):
    user_id: str
    expires_at: datetime
