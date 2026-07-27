from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.errors import createApiError
from app.core.middleware import RequestContextMiddleware, get_request_context
from app.modules.auth.router import router as auth_router
from app.modules.auth.service import build_default_auth_service
from app.modules.privacy.router import router as privacy_router
from app.modules.privacy.service import build_default_privacy_service
from app.modules.usage.router import router as usage_router
from app.modules.usage.service import build_default_usage_service

app = FastAPI(title="AI Resume API", version="1")
app.add_middleware(RequestContextMiddleware)
app.state.auth_service = build_default_auth_service(get_settings().app_env)
app.state.usage_service = build_default_usage_service()
app.state.privacy_service = build_default_privacy_service(app.state.auth_service)
app.include_router(auth_router)
app.include_router(usage_router)
app.include_router(privacy_router)


def api_error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    context = get_request_context(request)
    error = createApiError(code, message, context.request_id, status_code)
    return JSONResponse(status_code=status_code, content=error.detail)


@app.exception_handler(HTTPException)
async def api_error_handler(request: Request, error: HTTPException) -> JSONResponse:
    if isinstance(error.detail, dict) and "error" in error.detail:
        return JSONResponse(
            status_code=error.status_code,
            content=error.detail,
            headers=error.headers,
        )
    return api_error_response(request, error.status_code, "HTTP_ERROR", "Request failed")


@app.exception_handler(StarletteHTTPException)
async def framework_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
    code, message = {
        403: ("RESOURCE_FORBIDDEN", "Resource forbidden"),
        404: ("RESOURCE_NOT_FOUND", "Resource not found"),
        405: ("METHOD_NOT_ALLOWED", "Method not allowed"),
    }.get(error.status_code, ("HTTP_ERROR", "Request failed"))
    return api_error_response(request, error.status_code, code, message)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, _: RequestValidationError) -> JSONResponse:
    return api_error_response(request, 422, "VALIDATION_FAILED", "Request validation failed")


@app.get("/v1/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/health/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


if get_settings().app_env == "test":
    @app.get("/v1/testing/error")
    async def testing_error(request: Request) -> None:
        context = get_request_context(request)
        raise createApiError(
            "RESUME_VERSION_CONFLICT",
            "简历已在其他设备更新",
            context.request_id,
            409,
        )

    @app.get("/v1/testing/validate")
    async def testing_validate(value: int) -> dict[str, int]:
        return {"value": value}

    @app.get("/v1/testing/context")
    async def testing_context(request: Request) -> dict[str, str | None]:
        return {"actor_id": get_request_context(request).actor_id}
