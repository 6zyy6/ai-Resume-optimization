import pytest

from app.core.config import Settings
from app.core.middleware import CsrfProtectionMiddleware
from app.local import DerivedKeyProvider, build_local_app


def local_settings(**overrides):
    values = {
        "app_env": "development",
        "database_url": "sqlite+aiosqlite://",
        "storage_backend": "memory",
        "auth_redis_url": "redis://127.0.0.1:6379/2",
        "local_auth_secret": "a" * 32,
        "local_email_otp": "123456",
        "ai_service_token": "service-token",
    }
    values.update(overrides)
    return Settings(**values)


def test_local_app_wires_development_auth_and_ai_client():
    application = build_local_app(local_settings())

    assert application.state.ready is True
    assert application.state.auth_service.cookie_secure is False
    assert application.state.auth_service.code_factory() == "123456"
    assert application.state.job_service.ai_client is not None
    csrf = next(
        middleware
        for middleware in application.user_middleware
        if middleware.cls is CsrfProtectionMiddleware
    )
    assert csrf.kwargs["trusted_proxy_ips"] == ("127.0.0.1", "::1")


def test_local_app_rejects_unsafe_auth_configuration():
    with pytest.raises(ValueError, match="LOCAL_AUTH_SECRET"):
        build_local_app(local_settings(local_auth_secret="short"))

    with pytest.raises(ValueError, match="LOCAL_EMAIL_OTP"):
        build_local_app(local_settings(local_email_otp="12345"))


def test_derived_keys_are_stable_and_purpose_scoped():
    first = DerivedKeyProvider("a" * 32)
    second = DerivedKeyProvider("a" * 32)

    assert first.get_key("session") == second.get_key("session")
    assert first.get_key("session") != first.get_key("email")
