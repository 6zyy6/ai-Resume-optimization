from dataclasses import dataclass
import os
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.errors import createApiError
from app.core.middleware import (
    CsrfProtectionMiddleware,
    RequestContextMiddleware,
    get_request_context,
)
from app.modules.auth.router import router as auth_router
from app.modules.auth.service import (
    AuthRepository,
    EmailSender,
    WechatExchange,
    build_default_auth_service,
)
from app.modules.auth.preflight import AuthPreflightStore
from app.integrations.ai_client import AiClient, InternalAiClient, LegacyAiClientAdapter
from app.integrations.storage import StoragePort, build_storage
from app.modules.exports.router import router as exports_router
from app.modules.exports.service import ExportService
from app.modules.imports.router import router as imports_router
from app.modules.imports.service import ImportService
from app.modules.intake.router import router as intake_router
from app.modules.intake.service import IntakeService
from app.modules.jobs.router import router as jobs_router
from app.modules.jobs.service import JobService
from app.modules.matching.router import router as matching_router
from app.modules.matching.service import MatchingService
from app.modules.privacy.router import router as privacy_router
from app.modules.privacy.service import PrivacyRepository, build_default_privacy_service
from app.modules.facts.router import router as facts_router
from app.modules.facts.service import FactService
from app.modules.resumes.router import router as resumes_router
from app.modules.resumes.service import ResumeService
from app.modules.suggestions.router import router as suggestions_router
from app.modules.suggestions.service import SuggestionService
from app.modules.tasks.router import router as tasks_router
from app.modules.tasks.service import TaskService
from app.modules.usage.router import router as usage_router
from app.modules.usage.service import UsageRepository, build_default_usage_service
from app.modules.users.router import router as users_router
from app.modules.users.service import EmailCrypto, KeyProvider, MeService
from app.workers.dispatcher import OutboxDispatcher, build_default_dispatcher


@dataclass(frozen=True)
class ApplicationDependencies:
    auth_repository: AuthRepository
    auth_preflight: AuthPreflightStore
    usage_repository: UsageRepository
    privacy_repository: PrivacyRepository
    email_sender: EmailSender
    wechat_exchange: WechatExchange
    email_crypto: EmailCrypto
    keys: KeyProvider
    task4_sessions: async_sessionmaker[AsyncSession] | None = None
    task_dispatcher: OutboxDispatcher | None = None
    storage: StoragePort | None = None
    ai_client: AiClient | None = None
    auth_code_factory: Callable[[], str] | None = None

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
    if resolved.cors_allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
            allow_headers=["Content-Type", "Idempotency-Key", "X-Trace-Id"],
        )
    application.add_middleware(
        CsrfProtectionMiddleware,
        trusted_proxy_ips=resolved.trusted_proxy_ips,
    )
    application.add_middleware(RequestContextMiddleware)
    application.state.auth_service = build_default_auth_service(
        resolved.app_env,
        repository=dependencies.auth_repository if dependencies else None,
        preflight_store=dependencies.auth_preflight if dependencies else None,
        email_sender=dependencies.email_sender if dependencies else None,
        wechat_exchange=dependencies.wechat_exchange if dependencies else None,
        email_crypto=dependencies.email_crypto if dependencies else None,
        keys=dependencies.keys if dependencies else None,
        code_factory=dependencies.auth_code_factory if dependencies else None,
    )
    application.state.usage_service = build_default_usage_service(
        dependencies.usage_repository if dependencies else None,
        application.state.auth_service.clock,
    )
    application.state.privacy_service = build_default_privacy_service(
        application.state.auth_service,
        dependencies.privacy_repository if dependencies else None,
    )
    task4_sessions = dependencies.task4_sessions if dependencies and dependencies.task4_sessions else async_sessionmaker(create_async_engine(resolved.database_url), expire_on_commit=False)
    application.state.fact_service = FactService(task4_sessions)
    application.state.resume_service = ResumeService(task4_sessions)
    application.state.task_service = TaskService(task4_sessions)
    application.state.me_service = MeService(
        task4_sessions,
        application.state.auth_service.users,
    )
    storage = (
        dependencies.storage
        if dependencies and dependencies.storage
        else build_storage(resolved)
    )
    ai_client = (
        dependencies.ai_client
        if dependencies and dependencies.ai_client
        else (
            InternalAiClient(resolved.ai_internal_url, resolved.ai_service_token)
            if resolved.app_env == "production" and resolved.ai_service_token
            else None
        )
    )
    application.state.storage = storage
    application.state.intake_service = IntakeService(task4_sessions, ai_client)
    application.state.import_service = ImportService(task4_sessions, storage)
    legacy_ai_client = (
        LegacyAiClientAdapter(ai_client) if ai_client is not None else None
    )
    application.state.job_service = JobService(task4_sessions, legacy_ai_client)
    application.state.matching_service = MatchingService(task4_sessions, ai_client)
    application.state.suggestion_service = SuggestionService(task4_sessions)
    application.state.export_service = ExportService(task4_sessions, storage)
    application.state.task_dispatcher = (
        dependencies.task_dispatcher
        if dependencies and dependencies.task_dispatcher
        else build_default_dispatcher(task4_sessions)
    )
    application.state.ready = (
        dependencies is not None or resolved.app_env != "production"
    )
    application.include_router(auth_router)
    application.include_router(users_router)
    application.include_router(usage_router)
    application.include_router(privacy_router)
    application.include_router(facts_router)
    application.include_router(resumes_router)
    application.include_router(intake_router)
    application.include_router(imports_router)
    application.include_router(jobs_router)
    application.include_router(matching_router)
    application.include_router(suggestions_router)
    application.include_router(exports_router)
    application.include_router(tasks_router)
    application.add_exception_handler(HTTPException, api_error_handler)
    application.add_exception_handler(StarletteHTTPException, framework_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.get("/v1/health/live")(live)

    @application.get("/v1/version")
    async def version() -> dict[str, str]:
        return {
            "commit_sha": os.getenv("APP_COMMIT_SHA", "development"),
            "service": "api",
        }

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
