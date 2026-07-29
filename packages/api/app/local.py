import hashlib
import hmac
from dataclasses import replace

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.db.task3_repositories import (
    SqlAuthRepository,
    SqlPrivacyRepository,
    SqlUsageRepository,
)
from app.integrations.ai_client import InternalAiClient
from app.main import ApplicationDependencies, create_app
from app.modules.auth.preflight import RedisAuthPreflightStore
from app.modules.auth.service import (
    EnvelopeEmailCrypto,
    UnavailableWechatExchange,
)

class LocalEmailSender:
    async def send_otp(self, _email: str, _code: str) -> None:
        return None


class DerivedKeyProvider:
    def __init__(self, secret: str) -> None:
        self.secret = secret.encode()

    def get_key(self, purpose: str) -> bytes:
        return hmac.new(self.secret, purpose.encode(), hashlib.sha256).digest()


def build_local_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    if resolved.app_env != "development":
        raise ValueError("app.local can only run with APP_ENV=development")
    if len(resolved.local_auth_secret.encode()) < 32:
        raise ValueError("LOCAL_AUTH_SECRET must be at least 32 bytes")
    if len(resolved.local_email_otp) != 6 or not resolved.local_email_otp.isdigit():
        raise ValueError("LOCAL_EMAIL_OTP must contain exactly six digits")
    resolved = replace(
        resolved,
        trusted_proxy_ips=tuple(
            dict.fromkeys((*resolved.trusted_proxy_ips, "127.0.0.1", "::1"))
        ),
    )

    engine = create_async_engine(resolved.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(resolved.auth_redis_url)
    dependencies = ApplicationDependencies(
        auth_repository=SqlAuthRepository(sessions),
        auth_preflight=RedisAuthPreflightStore(redis),
        usage_repository=SqlUsageRepository(sessions),
        privacy_repository=SqlPrivacyRepository(sessions),
        email_sender=LocalEmailSender(),
        wechat_exchange=UnavailableWechatExchange(),
        email_crypto=EnvelopeEmailCrypto(),
        keys=DerivedKeyProvider(resolved.local_auth_secret),
        task4_sessions=sessions,
        ai_client=InternalAiClient(
            resolved.ai_internal_url,
            resolved.ai_service_token,
        ),
        auth_code_factory=lambda: resolved.local_email_otp,
    )
    application = create_app(resolved, dependencies)
    application.state.local_engine = engine
    application.state.local_redis = redis
    return application
