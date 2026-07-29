from typing import Literal

from pydantic import BaseModel, ConfigDict


class MeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str
    masked_email: str | None
    identity_type: Literal["email", "wechat", "hybrid", "unknown"]
    consent_versions: dict[str, str]
