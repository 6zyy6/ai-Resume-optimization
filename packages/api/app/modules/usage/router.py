from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.contracts import ApiErrorEnvelope
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.usage.service import UsageService


router = APIRouter(
    prefix="/v1/me",
    tags=["usage"],
    responses={401: {"model": ApiErrorEnvelope}},
)


class UsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ai_tasks_used: int
    ai_tasks_limit: int
    ai_tasks_running: int
    ai_concurrent_limit: int
    global_cost_cny: Decimal
    global_cost_limit_cny: Decimal
    cost_state: str


def get_usage_service(request: Request) -> UsageService:
    return request.app.state.usage_service


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    authenticated: AuthenticatedSession = Depends(require_session),
    service: UsageService = Depends(get_usage_service),
) -> UsageResponse:
    summary = await service.summary(authenticated.user_id)
    return UsageResponse(**summary.__dict__)
