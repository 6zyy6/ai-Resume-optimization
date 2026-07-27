from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.errors import createApiError
from app.core.middleware import RequestContextMiddleware, get_request_context

app = FastAPI(title="AI Resume API", version="1")
app.add_middleware(RequestContextMiddleware)


@app.exception_handler(HTTPException)
async def api_error_handler(_: Request, error: HTTPException) -> JSONResponse:
    if isinstance(error.detail, dict) and "error" in error.detail:
        return JSONResponse(status_code=error.status_code, content=error.detail)
    return JSONResponse(status_code=error.status_code, content={"detail": error.detail})


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
