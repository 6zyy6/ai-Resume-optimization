import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.main import app
from app.modules.auth.service import AuthService, HmacSecretHasher, InMemoryAuthRepository
from app.modules.privacy.service import InMemoryPrivacyRepository, PrivacyService


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
        return hashlib.sha256(key + email.encode()).hexdigest()

    def lookup_hash(self, email: str, key: bytes) -> str:
        return hashlib.sha256(b"lookup:" + key + email.encode()).hexdigest()


class DiscardSender:
    async def send_otp(self, email: str, code: str) -> None:
        return None


class StubWechat:
    async def exchange(self, code: str) -> str | None:
        return "known-subject" if code == "known-code" else None


@pytest.fixture
def privacy_harness():
    clock = FakeClock()
    auth_repository = InMemoryAuthRepository()
    auth_service = AuthService(
        repository=auth_repository,
        email_sender=DiscardSender(),
        wechat_exchange=StubWechat(),
        email_crypto=DeterministicEmailCrypto(),
        keys=StaticKeys(),
        hasher=HmacSecretHasher(),
        clock=clock,
        code_factory=lambda: "123456",
        token_factory=lambda: f"session-token-{len(auth_repository.sessions) + 1}",
        app_env="test",
    )
    user_id = asyncio.run(auth_service.register_wechat_identity("known-subject"))
    privacy_repository = InMemoryPrivacyRepository()
    privacy_service = PrivacyService(privacy_repository, auth_service, clock)
    previous_auth = app.state.auth_service
    previous_privacy = getattr(app.state, "privacy_service", None)
    app.state.auth_service = auth_service
    app.state.privacy_service = privacy_service
    yield auth_service, auth_repository, privacy_service, privacy_repository, clock, user_id
    app.state.auth_service = previous_auth
    app.state.privacy_service = previous_privacy


def login(client):
    response = client.post("/v1/auth/wechat/login", json={"code": "known-code"})
    assert response.status_code == 200
    return response.cookies["session"]


def test_data_export_reuses_the_task_for_the_same_idempotency_key(
    client,
    privacy_harness,
):
    login(client)

    first = client.post(
        "/v1/me/data-exports",
        headers={"Idempotency-Key": "export-key"},
    )
    second = client.post(
        "/v1/me/data-exports",
        headers={"Idempotency-Key": "export-key"},
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json() == first.json()
    assert first.json()["type"] == "data_export"
    assert first.json()["status"] == "queued"
    assert len(privacy_harness[3].tasks) == 1


def test_privacy_task_requires_an_idempotency_key(client, privacy_harness):
    login(client)

    response = client.post("/v1/me/data-exports")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.parametrize(
    "route",
    ["/v1/me/data-exports", "/v1/me/deletion-requests"],
)
def test_bodyless_privacy_writes_reject_unknown_fields(
    client,
    privacy_harness,
    route,
):
    login(client)

    response = client.post(
        route,
        headers={"Idempotency-Key": "privacy-key"},
        json={"unexpected": True},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_data_export_limits_each_user_to_ten_requests_per_hour(
    client,
    privacy_harness,
):
    login(client)
    for index in range(10):
        response = client.post(
            "/v1/me/data-exports",
            headers={"Idempotency-Key": f"export-{index}"},
        )
        assert response.status_code == 202

    blocked = client.post(
        "/v1/me/data-exports",
        headers={"Idempotency-Key": "export-10"},
    )

    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "3600"
    assert blocked.json()["error"]["code"] == "EXPORT_RATE_LIMITED"

    privacy_harness[4].advance(hours=1)
    allowed = client.post(
        "/v1/me/data-exports",
        headers={"Idempotency-Key": "export-after-window"},
    )
    assert allowed.status_code == 202


def test_deletion_requires_recent_authentication(client, privacy_harness):
    login(client)
    privacy_harness[4].advance(minutes=10, seconds=1)

    response = client.post(
        "/v1/me/deletion-requests",
        headers={"Idempotency-Key": "delete-key"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "RECENT_AUTH_REQUIRED"
    assert privacy_harness[3].tasks == {}


def test_refresh_does_not_count_as_recent_identity_verification(
    client,
    privacy_harness,
):
    login(client)
    privacy_harness[4].advance(minutes=10, seconds=1)
    refreshed = client.post("/v1/auth/refresh")
    assert refreshed.status_code == 200

    response = client.post(
        "/v1/me/deletion-requests",
        headers={"Idempotency-Key": "delete-key"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "RECENT_AUTH_REQUIRED"


def test_deletion_creates_one_task_and_immediately_revokes_every_session(
    client,
    privacy_harness,
):
    first_token = login(client)
    second_token = login(client)
    auth_service, auth_repository, privacy_service, privacy_repository, _, user_id = (
        privacy_harness
    )
    authenticated = asyncio.run(auth_service.authenticate(second_token))
    assert authenticated is not None

    response = client.post(
        "/v1/me/deletion-requests",
        headers={"Idempotency-Key": "delete-key"},
    )
    repeated = client.post(
        "/v1/me/deletion-requests",
        headers={"Idempotency-Key": "delete-key"},
    )

    assert response.status_code == 202
    assert repeated.status_code == 202
    assert repeated.json() == response.json()
    assert len(privacy_repository.tasks) == 1
    assert auth_repository.users[user_id].status == "pending_deletion"
    assert asyncio.run(auth_service.authenticate(first_token)) is None
    assert asyncio.run(auth_service.authenticate(second_token)) is None
    assert all(session.revoked_at is not None for session in auth_repository.sessions.values())

    unavailable = client.get("/v1/me/usage")
    assert unavailable.status_code == 401
    assert unavailable.json()["error"]["code"] == "AUTH_REQUIRED"
