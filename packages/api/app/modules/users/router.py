from fastapi import APIRouter, Depends, Request

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.users.schemas import MeResponse
from app.modules.users.service import MeService


router = APIRouter(
    prefix="/v1/me",
    tags=["users"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 404)
    },
)


def get_me_service(request: Request) -> MeService:
    return request.app.state.me_service


@router.get("", response_model=MeResponse)
async def get_me(
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: MeService = Depends(get_me_service),
) -> MeResponse:
    profile = await service.get(authenticated.user_id)
    if profile is None:
        raise createApiError(
            "RESOURCE_NOT_FOUND",
            "User not found",
            get_request_context(request).request_id,
            404,
        )
    return MeResponse.model_validate(profile, strict=False)
