from fastapi import APIRouter, Body, Depends, Request, Response, Security
from fastapi.security import APIKeyCookie

from app.contracts import ApiErrorEnvelope
from app.core.errors import createApiError
from app.core.middleware import get_request_context
from app.modules.auth.schemas import (
    AuthenticatedResponse,
    BindEmailRequest,
    EmptyRequest,
    EmailStartRequest,
    EmailVerifyRequest,
    OtpStartedResponse,
    WechatLoginRequest,
)
from app.modules.auth.service import (
    AuthError,
    AuthenticatedSession,
    AuthService,
    LoginResult,
)


router = APIRouter(
    prefix="/v1/auth",
    tags=["auth"],
    responses={
        status: {"model": ApiErrorEnvelope}
        for status in (401, 403, 404, 409, 422, 429, 503)
    },
)
SESSION_COOKIE = "session"
session_cookie = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def raise_auth_error(request: Request, error: AuthError) -> None:
    api_error = createApiError(
        error.code,
        error.message,
        get_request_context(request).request_id,
        error.status_code,
        error.details,
    )
    if error.retry_after is not None:
        api_error.headers = {"Retry-After": str(error.retry_after)}
    raise api_error


def set_session_cookie(response: Response, result: LoginResult, service: AuthService) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        result.raw_token,
        expires=result.expires_at,
        httponly=True,
        secure=service.cookie_secure,
        samesite="lax",
        path="/",
    )


async def require_session(
    request: Request,
    raw_session: str | None = Security(session_cookie),
    service: AuthService = Depends(get_auth_service),
) -> AuthenticatedSession:
    authenticated = await service.authenticate(raw_session)
    if authenticated is None:
        raise_auth_error(
            request,
            AuthError("AUTH_REQUIRED", "Authentication required", 401),
        )
    return authenticated


@router.post("/email/start", status_code=202, response_model=OtpStartedResponse)
async def start_email(
    payload: EmailStartRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> OtpStartedResponse:
    try:
        await service.start_email(
            payload.email,
            request.client.host if request.client else "unknown",
        )
    except AuthError as error:
        raise_auth_error(request, error)
    return OtpStartedResponse(status="sent", expires_in=600)


@router.post("/email/verify", response_model=AuthenticatedResponse)
async def verify_email(
    payload: EmailVerifyRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> AuthenticatedResponse:
    try:
        result = await service.verify_email(payload.email, payload.code, payload.consents)
    except AuthError as error:
        raise_auth_error(request, error)
    set_session_cookie(response, result, service)
    return AuthenticatedResponse(user_id=result.user_id, expires_at=result.expires_at)


@router.post("/wechat/login", response_model=AuthenticatedResponse)
async def login_wechat(
    payload: WechatLoginRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> AuthenticatedResponse:
    try:
        result = await service.login_wechat(payload.code, payload.consents)
    except AuthError as error:
        raise_auth_error(request, error)
    set_session_cookie(response, result, service)
    return AuthenticatedResponse(user_id=result.user_id, expires_at=result.expires_at)


@router.post("/identities/bind-email", status_code=204)
async def bind_email(
    payload: BindEmailRequest,
    request: Request,
    authenticated: AuthenticatedSession = Depends(require_session),
    service: AuthService = Depends(get_auth_service),
) -> Response:
    try:
        await service.bind_email(
            authenticated,
            payload.email,
            payload.code,
            payload.confirm_merge,
        )
    except AuthError as error:
        raise_auth_error(request, error)
    return Response(status_code=204)


@router.post("/refresh", response_model=AuthenticatedResponse)
async def refresh(
    request: Request,
    response: Response,
    _payload: EmptyRequest | None = Body(default=None),
    service: AuthService = Depends(get_auth_service),
) -> AuthenticatedResponse:
    try:
        result = await service.refresh(request.cookies.get(SESSION_COOKIE))
    except AuthError as error:
        raise_auth_error(request, error)
    set_session_cookie(response, result, service)
    return AuthenticatedResponse(user_id=result.user_id, expires_at=result.expires_at)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    _payload: EmptyRequest | None = Body(default=None),
    service: AuthService = Depends(get_auth_service),
) -> Response:
    await service.logout(request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=service.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response
