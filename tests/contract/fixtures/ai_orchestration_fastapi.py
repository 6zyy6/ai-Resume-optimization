import os
from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.task3_repositories import (
    SqlAuthRepository,
    SqlPrivacyRepository,
    SqlUsageRepository,
)
from app.integrations.ai_client import InternalAiClient
from app.integrations.storage import MemoryStorage
from app.local import DerivedKeyProvider, LocalEmailSender
from app.main import ApplicationDependencies, create_app
from app.modules.auth.preflight import InMemoryAuthPreflightStore
from app.modules.auth.router import require_session
from app.modules.auth.service import (
    AuthenticatedSession,
    EnvelopeEmailCrypto,
    UnavailableWechatExchange,
)
from app.modules.tasks.service import TaskService
from app.workers.execution import TaskExecutor, resolve_operation
from app.workers.pipeline import configure_pipeline_operations
from assert_state import inspect


database_url = os.environ["CONTRACT_DATABASE_URL"]
settings = Settings(
    app_env="development",
    database_url=database_url,
    trusted_proxy_ips=("127.0.0.1", "::1"),
    cors_allowed_origins=tuple(
        origin
        for origin in (os.getenv("CONTRACT_WEB_ORIGIN"),)
        if origin
    ),
    storage_backend="memory",
    ai_internal_url=os.environ["AI_INTERNAL_URL"],
    ai_service_token=os.environ["AI_SERVICE_TOKEN"],
)
engine = create_async_engine(database_url)
sessions = async_sessionmaker(engine, expire_on_commit=False)
app = create_app(
    settings,
    ApplicationDependencies(
        auth_repository=SqlAuthRepository(sessions),
        auth_preflight=InMemoryAuthPreflightStore(),
        usage_repository=SqlUsageRepository(sessions),
        privacy_repository=SqlPrivacyRepository(sessions),
        email_sender=LocalEmailSender(),
        wechat_exchange=UnavailableWechatExchange(),
        email_crypto=EnvelopeEmailCrypto(),
        keys=DerivedKeyProvider("contract-auth-secret-at-least-32-bytes"),
        task4_sessions=sessions,
        storage=MemoryStorage(),
        ai_client=InternalAiClient(
            settings.ai_internal_url,
            settings.ai_service_token,
            poll_interval_seconds=0.01,
        ),
        auth_code_factory=lambda: "123456",
    ),
)


class RunTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ai_mode: Literal["fixture", "unavailable"] = "fixture"


@app.post("/v1/testing/tasks/{task_id}/run")
async def run_contract_task(
    task_id: str,
    payload: RunTaskRequest,
    authenticated: AuthenticatedSession = Depends(require_session),
) -> dict[str, object]:
    task_service = TaskService(sessions)
    ai_client = InternalAiClient(
        settings.ai_internal_url
        if payload.ai_mode == "fixture"
        else "http://127.0.0.1:9",
        settings.ai_service_token,
        poll_interval_seconds=0.01,
    )
    configure_pipeline_operations(
        sessions,
        settings,
        task_service,
        ai_client_override=ai_client,
    )
    return await TaskExecutor(
        task_service,
        sleep=lambda _delay: None,
        jitter=lambda: 0,
    ).execute(authenticated.user_id, task_id, resolve_operation)


@app.get("/v1/testing/tasks/{task_id}/inspection")
async def inspect_contract_task(
    task_id: str,
    authenticated: AuthenticatedSession = Depends(require_session),
) -> dict[str, object]:
    return await inspect(database_url, authenticated.user_id, task_id)


@app.on_event("shutdown")
async def close_contract_database() -> None:
    await engine.dispose()
