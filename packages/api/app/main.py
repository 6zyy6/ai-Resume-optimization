from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import Settings, get_settings
from app.core.errors import createApiError
from app.core.middleware import RequestContextMiddleware, get_request_context
from app.modules.auth.router import router as auth_router
from app.modules.auth.service import (
    AuthRepository,
    EmailSender,
    WechatExchange,
    build_default_auth_service,
)
from app.modules.privacy.router import router as privacy_router
from app.modules.privacy.service import PrivacyRepository, build_default_privacy_service
from app.modules.usage.router import router as usage_router
from app.modules.usage.service import UsageRepository, build_default_usage_service
from app.modules.users.service import EmailCrypto, KeyProvider


@dataclass(frozen=True)
class ApplicationDependencies:
    auth_repository: AuthRepository
    usage_repository: UsageRepository
    privacy_repository: PrivacyRepository
    email_sender: EmailSender
    wechat_exchange: WechatExchange
    email_crypto: EmailCrypto
    keys: KeyProvider

def api_error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    context = get_request_context(request)
    error = createApiError(code, message, context.request_id, status_code)
    return JSONResponse(status_code=status_code, content=error.detail)


async def api_error_handler(request: Request, error: HTTPException) -> JSONResponse:
    if isinstance(error.detail, dict) and "error" in error.detail:
        return JSONResponse(
            status_code=error.status_code,
            content=error.detail,
            headers=error.headers,
        )
    return api_error_response(request, error.status_code, "HTTP_ERROR", "Request failed")


async def framework_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
    code, message = {
        403: ("RESOURCE_FORBIDDEN", "Resource forbidden"),
        404: ("RESOURCE_NOT_FOUND", "Resource not found"),
        405: ("METHOD_NOT_ALLOWED", "Method not allowed"),
    }.get(error.status_code, ("HTTP_ERROR", "Request failed"))
    return api_error_response(request, error.status_code, code, message)


async def validation_error_handler(request: Request, _: RequestValidationError) -> JSONResponse:
    return api_error_response(request, 422, "VALIDATION_FAILED", "Request validation failed")


async def live() -> dict[str, str]:
    return {"status": "ok"}


def create_app(
    settings: Settings | None = None,
    dependencies: ApplicationDependencies | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    application = FastAPI(title="AI Resume API", version="1")
    application.add_middleware(RequestContextMiddleware)
    application.state.auth_service = build_default_auth_service(
        resolved.app_env,
        repository=dependencies.auth_repository if dependencies else None,
        email_sender=dependencies.email_sender if dependencies else None,
        wechat_exchange=dependencies.wechat_exchange if dependencies else None,
        email_crypto=dependencies.email_crypto if dependencies else None,
        keys=dependencies.keys if dependencies else None,
    )
    application.state.usage_service = build_default_usage_service(
        dependencies.usage_repository if dependencies else None,
        application.state.auth_service.clock,
    )
    application.state.privacy_service = build_default_privacy_service(
        application.state.auth_service,
        dependencies.privacy_repository if dependencies else None,
    )
    application.state.ready = (
        dependencies is not None or resolved.app_env != "production"
    )
    application.include_router(auth_router)
    application.include_router(usage_router)
    application.include_router(privacy_router)
    application.add_exception_handler(HTTPException, api_error_handler)
    application.add_exception_handler(StarletteHTTPException, framework_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.get("/v1/health/live")(live)

    @application.get("/v1/health/ready")
    async def ready(request: Request) -> dict[str, str]:
        if not request.app.state.ready:
            raise createApiError(
                "APP_NOT_READY",
                "Application dependencies are not configured",
                get_request_context(request).request_id,
                503,
            )
        return {"status": "ready"}

    if resolved.app_env == "test":
        @application.get("/v1/testing/error")
        async def testing_error(request: Request) -> None:
            context = get_request_context(request)
            raise createApiError(
                "RESUME_VERSION_CONFLICT",
                "简历已在其他设备更新",
                context.request_id,
                409,
            )

        @application.get("/v1/testing/validate")
        async def testing_validate(value: int) -> dict[str, int]:
            return {"value": value}

        @application.get("/v1/testing/context")
        async def testing_context(request: Request) -> dict[str, str | None]:
            return {"actor_id": get_request_context(request).actor_id}

    return application


app = create_app()
