import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.main import app
from app.modules.auth.service import (
    AuthError,
    AuthService,
    HmacSecretHasher,
    InMemoryAuthRepository,
    build_default_auth_service,
    cookie_secure_for_environment,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class StaticKeys:
    def get_key(self, purpose: str) -> bytes:
        return f"test-{purpose}-key".encode()


class DeterministicEmailCrypto:
    def encrypt(self, email: str, key: bytes) -> str:
        digest = hashlib.sha256(key + email.encode()).hexdigest()
        return f"encrypted:{digest}"

    def lookup_hash(self, email: str, key: bytes) -> str:
        return hashlib.sha256(b"lookup:" + key + email.encode()).hexdigest()


class RecordingEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_otp(self, email: str, code: str) -> None:
        self.sent.append((email, code))

    def latest_code(self, email: str) -> str:
        return next(code for address, code in reversed(self.sent) if address == email)


class StubWechatExchange:
    def __init__(self) -> None:
        self.subjects: dict[str, str] = {}

    async def exchange(self, code: str) -> str | None:
        return self.subjects.get(code)


@dataclass
class AuthHarness:
    service: AuthService
    repository: InMemoryAuthRepository
    clock: FakeClock
    sender: RecordingEmailSender
    wechat: StubWechatExchange
    hasher: HmacSecretHasher


@pytest.fixture
def auth_harness():
    clock = FakeClock()
    repository = InMemoryAuthRepository()
    sender = RecordingEmailSender()
    wechat = StubWechatExchange()
    hasher = HmacSecretHasher()
    service = AuthService(
        repository=repository,
        email_sender=sender,
        wechat_exchange=wechat,
        email_crypto=DeterministicEmailCrypto(),
        keys=StaticKeys(),
        hasher=hasher,
        clock=clock,
        code_factory=lambda: "123456",
        token_factory=lambda: f"session-token-{len(repository.sessions) + 1}",
        app_env="test",
    )
    previous = getattr(app.state, "auth_service", None)
    app.state.auth_service = service
    yield AuthHarness(service, repository, clock, sender, wechat, hasher)
    app.state.auth_service = previous


def start_email(client, email: str):
    return client.post("/v1/auth/email/start", json={"email": email})


def verify_email(client, harness: AuthHarness, email: str, *, consent: bool = True):
    payload: dict[str, object] = {
        "email": email,
        "code": harness.sender.latest_code(email.lower()),
    }
    if consent:
        payload["consents"] = [
            {
                "document_type": "user_agreement",
                "document_version": "2026-07-27",
                "decision": "accepted",
            },
            {
                "document_type": "privacy_policy",
                "document_version": "2026-07-27",
                "decision": "accepted",
            },
        ]
    return client.post("/v1/auth/email/verify", json=payload)


def test_email_otp_is_six_digits_and_expires_after_ten_minutes(
    client,
    auth_harness: AuthHarness,
):
    email = "person@example.com"
    started = start_email(client, email)

    assert started.status_code == 202
    code = auth_harness.sender.latest_code(email)
    assert len(code) == 6
    assert code.isdigit()
    assert started.json()["expires_in"] == 600

    auth_harness.clock.advance(minutes=10, seconds=1)
    expired = verify_email(client, auth_harness, email)
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "AUTH_CODE_INVALID"


def test_email_otp_requires_sixty_seconds_before_resend(
    client,
    auth_harness: AuthHarness,
):
    email = "person@example.com"
    assert start_email(client, email).status_code == 202

    blocked = start_email(client, email)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    assert blocked.json()["error"]["code"] == "AUTH_RATE_LIMITED"

    auth_harness.clock.advance(seconds=60)
    assert start_email(client, email).status_code == 202


def test_email_start_limits_each_ip_to_five_requests_per_minute(
    client,
    auth_harness: AuthHarness,
):
    for index in range(5):
        assert start_email(client, f"person-{index}@example.com").status_code == 202

    blocked = start_email(client, "person-5@example.com")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "AUTH_RATE_LIMITED"
    assert int(blocked.headers["retry-after"]) > 0


def test_email_start_limits_each_email_to_five_requests_per_hour(
    client,
    auth_harness: AuthHarness,
):
    email = "person@example.com"
    for _ in range(5):
        assert start_email(client, email).status_code == 202
        auth_harness.clock.advance(seconds=60)

    blocked = start_email(client, email)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "AUTH_RATE_LIMITED"
    assert int(blocked.headers["retry-after"]) > 0


def test_auth_requests_reject_unknown_fields(client, auth_harness: AuthHarness):
    response = client.post(
        "/v1/auth/email/start",
        json={"email": "person@example.com", "unexpected": True},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.parametrize("route", ["/v1/auth/refresh", "/v1/auth/logout"])
def test_bodyless_auth_writes_reject_unknown_fields(client, route):
    response = client.post(route, json={"unexpected": True})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_first_email_login_requires_consent_and_stores_protected_email(
    client,
    auth_harness: AuthHarness,
):
    email = "Person@Example.COM"
    assert start_email(client, email).status_code == 202

    rejected = verify_email(client, auth_harness, email, consent=False)
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "CONSENT_REQUIRED"
    assert auth_harness.repository.users == {}

    accepted = verify_email(client, auth_harness, email)
    assert accepted.status_code == 200
    user = auth_harness.repository.users[accepted.json()["user_id"]]
    assert user.email_encrypted != email.lower()
    assert email.lower() not in user.email_encrypted
    assert user.email_lookup_hash
    assert {
        (consent.document_type, consent.document_version)
        for consent in auth_harness.repository.consents
    } == {
        ("user_agreement", "2026-07-27"),
        ("privacy_policy", "2026-07-27"),
    }
    email_identity = next(iter(auth_harness.repository.identities.values()))
    assert email_identity.identity_type == "email_otp"

    cookie = accepted.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie


def test_explicit_wechat_login_onboards_a_new_user_only_with_current_consents(
    client,
    auth_harness: AuthHarness,
):
    auth_harness.wechat.subjects["unknown-code"] = "unknown-subject"
    rejected = client.post("/v1/auth/wechat/login", json={"code": "unknown-code"})
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "CONSENT_REQUIRED"
    assert auth_harness.repository.users == {}

    accepted = client.post(
        "/v1/auth/wechat/login",
        json={
            "code": "unknown-code",
            "consents": [
                {
                    "document_type": "user_agreement",
                    "document_version": "2026-07-27",
                    "decision": "accepted",
                },
                {
                    "document_type": "privacy_policy",
                    "document_version": "2026-07-27",
                    "decision": "accepted",
                },
            ],
        },
    )
    assert accepted.status_code == 200
    user_id = accepted.json()["user_id"]
    assert len(auth_harness.repository.users) == 1
    assert len(auth_harness.repository.consents) == 2

    auth_harness.wechat.subjects["known-code"] = "known-subject"
    existing_id = asyncio.run(
        auth_harness.service.register_wechat_identity(
            "known-subject",
            status="active",
        )
    )
    existing = client.post("/v1/auth/wechat/login", json={"code": "known-code"})
    assert existing.status_code == 200
    assert existing.json()["user_id"] == existing_id
    wechat_identity = next(
        identity
        for identity in auth_harness.repository.identities.values()
        if identity.owner_user_id == user_id
    )
    assert wechat_identity.identity_type == "wechat_miniprogram"

    inactive_id = asyncio.run(
        auth_harness.service.register_wechat_identity(
            "inactive-subject",
            status="pending_deletion",
        )
    )
    auth_harness.wechat.subjects["inactive-code"] = "inactive-subject"
    inactive = client.post("/v1/auth/wechat/login", json={"code": "inactive-code"})
    assert inactive.status_code == 403
    assert inactive.json()["error"]["code"] == "AUTH_ACCOUNT_INACTIVE"
    assert inactive_id != user_id


@pytest.mark.parametrize(
    "consents",
    [
        [
            {
                "document_type": "privacy_policy",
                "document_version": "2026-07-27",
                "decision": "accepted",
            }
        ],
        [
            {
                "document_type": "user_agreement",
                "document_version": "stale",
                "decision": "accepted",
            },
            {
                "document_type": "privacy_policy",
                "document_version": "2026-07-27",
                "decision": "accepted",
            },
        ],
    ],
)
def test_first_login_rejects_incomplete_or_stale_consent(
    client,
    auth_harness: AuthHarness,
    consents,
):
    email = "person@example.com"
    assert start_email(client, email).status_code == 202

    response = client.post(
        "/v1/auth/email/verify",
        json={
            "email": email,
            "code": auth_harness.sender.latest_code(email),
            "consents": consents,
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CONSENT_REQUIRED"
    assert auth_harness.repository.users == {}


def test_first_login_rejects_unknown_consent_document(
    client,
    auth_harness: AuthHarness,
):
    email = "person@example.com"
    assert start_email(client, email).status_code == 202

    response = client.post(
        "/v1/auth/email/verify",
        json={
            "email": email,
            "code": auth_harness.sender.latest_code(email),
            "consents": [
                {
                    "document_type": "x",
                    "document_version": "2026-07-27",
                    "decision": "accepted",
                }
            ],
        },
    )

    assert response.status_code == 422


def test_binding_existing_email_requires_confirmation_and_merges_accounts(
    client,
    auth_harness: AuthHarness,
):
    email = "person@example.com"
    assert start_email(client, email).status_code == 202
    email_login = verify_email(client, auth_harness, email)
    email_user_id = email_login.json()["user_id"]
    client.post("/v1/auth/logout")

    wechat_user_id = asyncio.run(
        auth_harness.service.register_wechat_identity("wechat-subject")
    )
    auth_harness.wechat.subjects["wechat-code"] = "wechat-subject"
    wechat_login = client.post("/v1/auth/wechat/login", json={"code": "wechat-code"})
    assert wechat_login.status_code == 200
    assert wechat_login.json()["user_id"] == wechat_user_id

    auth_harness.clock.advance(seconds=60)
    assert start_email(client, email).status_code == 202
    code = auth_harness.sender.latest_code(email)
    confirmation = client.post(
        "/v1/auth/identities/bind-email",
        json={"email": email, "code": code},
    )
    assert confirmation.status_code == 409
    assert confirmation.json()["error"]["code"] == "AUTH_MERGE_CONFIRMATION_REQUIRED"
    assert confirmation.json()["error"]["details"] == {
        "canonical_account": {
            "user_id": email_user_id,
            "has_email": True,
        },
        "current_account": {
            "user_id": wechat_user_id,
            "has_email": False,
        },
    }

    merged = client.post(
        "/v1/auth/identities/bind-email",
        json={"email": email, "code": code, "confirm_merge": True},
    )

    assert merged.status_code == 204
    assert auth_harness.repository.users[wechat_user_id].status == "merged"
    assert all(
        identity.owner_user_id != wechat_user_id
        for identity in auth_harness.repository.identities.values()
    )
    assert any(
        session.owner_user_id == email_user_id
        for session in auth_harness.repository.sessions.values()
        if session.revoked_at is None
    )


def test_wechat_login_rejects_client_supplied_external_identifier(
    client,
    auth_harness: AuthHarness,
):
    response = client.post(
        "/v1/auth/wechat/login",
        json={"code": "code", "openid": "must-not-be-trusted"},
    )
    assert response.status_code == 422


def test_logout_revokes_the_hashed_session(
    client,
    auth_harness: AuthHarness,
):
    email = "person@example.com"
    assert start_email(client, email).status_code == 202
    verified = verify_email(client, auth_harness, email)
    raw_token = verified.cookies["session"]
    token_hash = auth_harness.hasher.hash_secret(
        raw_token,
        StaticKeys().get_key("session"),
    )
    assert raw_token not in auth_harness.repository.sessions
    assert auth_harness.repository.sessions[token_hash].revoked_at is None

    logged_out = client.post("/v1/auth/logout")
    assert logged_out.status_code == 204
    assert auth_harness.repository.sessions[token_hash].revoked_at == auth_harness.clock.now()
    assert "session=" in logged_out.headers["set-cookie"]


def test_session_cookie_is_secure_outside_test_environments():
    assert cookie_secure_for_environment("production") is True
    assert cookie_secure_for_environment("development") is True
    assert cookie_secure_for_environment("test") is False


@pytest.mark.anyio
async def test_unconfigured_external_auth_providers_fail_instead_of_claiming_success():
    service = build_default_auth_service("development")

    with pytest.raises(AuthError) as email_error:
        await service.start_email("person@example.com", "127.0.0.1")
    assert email_error.value.code == "AUTH_PROVIDER_UNAVAILABLE"
    assert email_error.value.status_code == 503

    with pytest.raises(AuthError) as wechat_error:
        await service.login_wechat("code")
    assert wechat_error.value.code == "AUTH_PROVIDER_UNAVAILABLE"
    assert wechat_error.value.status_code == 503


def test_production_auth_without_injected_ports_is_unconfigured_not_an_import_crash():
    service = build_default_auth_service("production")

    assert service.cookie_secure is True
