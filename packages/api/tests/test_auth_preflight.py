import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.task3_repositories import SqlAuthRepository
from app.modules.auth.preflight import (
    InMemoryAuthPreflightBackend,
    InMemoryAuthPreflightStore,
)
from app.modules.auth.schemas import ConsentInput
from app.modules.auth.service import AuthError, AuthService, HmacSecretHasher


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class StaticKeys:
    def get_key(self, purpose: str) -> bytes:
        return f"test-{purpose}".encode().ljust(32, b"x")


class DeterministicEmailCrypto:
    def encrypt(self, email: str, key: bytes) -> str:
        return f"encrypted:{email}"

    def lookup_hash(self, email: str, key: bytes) -> str:
        return hashlib.sha256(b"lookup:" + key + email.encode()).hexdigest()


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_otp(self, email: str, code: str) -> None:
        self.sent.append((email, code))


class NoWechat:
    async def exchange(self, code: str) -> str | None:
        return None


def current_consents() -> list[ConsentInput]:
    return [
        ConsentInput(
            document_type="user_agreement",
            document_version="2026-07-27",
            decision="accepted",
        ),
        ConsentInput(
            document_type="privacy_policy",
            document_version="2026-07-27",
            decision="accepted",
        ),
    ]


def auth_worker(
    sessions: async_sessionmaker,
    preflight: InMemoryAuthPreflightStore,
    sender: RecordingSender,
    clock: FakeClock,
) -> AuthService:
    return AuthService(
        repository=SqlAuthRepository(sessions),
        preflight_store=preflight,
        email_sender=sender,
        wechat_exchange=NoWechat(),
        email_crypto=DeterministicEmailCrypto(),
        keys=StaticKeys(),
        hasher=HmacSecretHasher(),
        clock=clock,
        code_factory=lambda: "123456",
        token_factory=lambda: "session-token",
        app_env="test",
    )


@pytest.mark.anyio
async def test_otp_issued_by_one_worker_is_verified_after_repository_restart(
    sql_session_factory,
):
    backend = InMemoryAuthPreflightBackend()
    sender = RecordingSender()
    clock = FakeClock()
    worker_a = auth_worker(
        sql_session_factory,
        InMemoryAuthPreflightStore(backend),
        sender,
        clock,
    )
    await worker_a.start_email("person@example.com", "10.0.0.1")

    worker_b = auth_worker(
        sql_session_factory,
        InMemoryAuthPreflightStore(backend),
        sender,
        clock,
    )
    result = await worker_b.verify_email(
        "person@example.com",
        "123456",
        current_consents(),
    )

    assert result.user_id.startswith("usr_")


@pytest.mark.anyio
async def test_ip_rate_limit_is_shared_across_workers(sql_session_factory):
    backend = InMemoryAuthPreflightBackend()
    sender = RecordingSender()
    clock = FakeClock()
    workers = [
        auth_worker(
            sql_session_factory,
            InMemoryAuthPreflightStore(backend),
            sender,
            clock,
        )
        for _ in range(2)
    ]
    for index in range(5):
        await workers[index % 2].start_email(
            f"person-{index}@example.com",
            "10.0.0.1",
        )

    with pytest.raises(AuthError) as caught:
        await workers[1].start_email("person-5@example.com", "10.0.0.1")

    assert caught.value.code == "AUTH_RATE_LIMITED"
    assert caught.value.retry_after == 60


@pytest.mark.anyio
async def test_email_rate_limit_is_shared_across_store_instances(sql_session_factory):
    backend = InMemoryAuthPreflightBackend()
    sender = RecordingSender()
    clock = FakeClock()
    workers = [
        auth_worker(
            sql_session_factory,
            InMemoryAuthPreflightStore(backend),
            sender,
            clock,
        )
        for _ in range(2)
    ]
    for index in range(5):
        await workers[index % 2].start_email(
            "person@example.com",
            f"10.0.0.{index + 1}",
        )
        clock.advance(seconds=60)

    with pytest.raises(AuthError) as caught:
        await workers[1].start_email("person@example.com", "10.0.0.9")

    assert caught.value.code == "AUTH_RATE_LIMITED"
    assert caught.value.retry_after == 55 * 60
