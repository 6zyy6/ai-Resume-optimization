import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import Base, Experience, Session, User, UserIdentity
from app.db.task3_repositories import (
    SqlAuthRepository,
    SqlPrivacyRepository,
    SqlUsageRepository,
)
from app.main import ApplicationDependencies, create_app
from app.modules.auth.schemas import ConsentInput
from app.modules.auth.service import (
    AuthService,
    HmacSecretHasher,
    InMemoryAuthRepository,
    SessionRecord,
)
from app.modules.privacy.service import (
    InMemoryPrivacyRepository,
    PrivacyService,
    PrivacyTask,
)
from app.modules.usage.service import (
    InMemoryUsageRepository,
    UsageService,
)
from app.modules.users.service import IdentityRecord, UserAccount


class StaticKeys:
    def get_key(self, purpose: str) -> bytes:
        return f"test-{purpose}".encode().ljust(32, b"x")


class DeterministicEmailCrypto:
    def encrypt(self, email: str, key: bytes) -> str:
        return f"encrypted:{email}"

    def lookup_hash(self, email: str, key: bytes) -> str:
        return f"hash:{email}"


class DiscardSender:
    async def send_otp(self, email: str, code: str) -> None:
        return None


class StaticWechat:
    async def exchange(self, code: str) -> str | None:
        return "subject" if code == "valid-code" else None


@pytest.fixture
async def sql_session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'task3.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def test_production_application_constructs_without_runtime_secrets_and_reports_not_ready():
    application = create_app(
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://localhost/ai_resume",
        )
    )

    with TestClient(application) as client:
        response = client.get("/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "APP_NOT_READY"


def test_production_application_is_ready_when_all_ports_are_injected():
    dependencies = ApplicationDependencies(
        auth_repository=InMemoryAuthRepository(),
        usage_repository=InMemoryUsageRepository(),
        privacy_repository=InMemoryPrivacyRepository(),
        email_sender=DiscardSender(),
        wechat_exchange=StaticWechat(),
        email_crypto=DeterministicEmailCrypto(),
        keys=StaticKeys(),
    )
    application = create_app(
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://localhost/ai_resume",
        ),
        dependencies,
    )

    with TestClient(application) as client:
        response = client.get("/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.anyio
async def test_sql_auth_adapter_persists_identity_and_recent_auth_session(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    repository = SqlAuthRepository(sql_session_factory)
    await repository.save_user(
        UserAccount(
            id="usr_1",
            status="active",
            email_encrypted=None,
            email_lookup_hash=None,
            created_at=now,
        )
    )
    await repository.save_identity(
        IdentityRecord(
            id="idn_1",
            owner_user_id="usr_1",
            identity_type="wechat_miniprogram",
            external_subject_hash="subject-hash",
            verified_at=now,
        )
    )
    await repository.save_session(
        SessionRecord(
            id="ses_1",
            owner_user_id="usr_1",
            token_hash="token-hash",
            authenticated_at=now,
            expires_at=now + timedelta(days=30),
        )
    )

    restarted = SqlAuthRepository(sql_session_factory)
    user = await restarted.find_user_by_identity(
        "wechat_miniprogram",
        "subject-hash",
    )
    session = await restarted.find_session("token-hash")

    assert user is not None
    assert user.id == "usr_1"
    assert session is not None
    assert session.authenticated_at == now


@pytest.mark.anyio
async def test_auth_service_uses_sql_adapter_for_wechat_onboarding_and_restart(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    clock = type("Clock", (), {"now": lambda _: now})()
    repository = SqlAuthRepository(sql_session_factory)
    service = AuthService(
        repository=repository,
        email_sender=DiscardSender(),
        wechat_exchange=StaticWechat(),
        email_crypto=DeterministicEmailCrypto(),
        keys=StaticKeys(),
        hasher=HmacSecretHasher(),
        clock=clock,
        code_factory=lambda: "123456",
        token_factory=lambda: "raw-session-token",
        app_env="test",
    )
    consents = [
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

    result = await service.login_wechat("valid-code", consents)
    restarted = AuthService(
        repository=SqlAuthRepository(sql_session_factory),
        email_sender=DiscardSender(),
        wechat_exchange=StaticWechat(),
        email_crypto=DeterministicEmailCrypto(),
        keys=StaticKeys(),
        hasher=HmacSecretHasher(),
        clock=clock,
        code_factory=lambda: "123456",
        token_factory=lambda: "unused",
        app_env="test",
    )

    authenticated = await restarted.authenticate(result.raw_token)
    assert authenticated is not None
    assert authenticated.user_id == result.user_id


@pytest.mark.anyio
async def test_sql_confirmed_merge_migrates_resources_identities_and_sessions(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    repository = SqlAuthRepository(sql_session_factory)
    for user_id in ("usr_email", "usr_wechat"):
        await repository.save_user(
            UserAccount(
                id=user_id,
                status="active",
                email_encrypted="encrypted" if user_id == "usr_email" else None,
                email_lookup_hash="email-hash" if user_id == "usr_email" else None,
                created_at=now,
            )
        )
    await repository.save_identity(
        IdentityRecord(
            id="idn_wechat",
            owner_user_id="usr_wechat",
            identity_type="wechat_miniprogram",
            external_subject_hash="wechat-hash",
            verified_at=now,
        )
    )
    await repository.save_session(
        SessionRecord(
            id="ses_wechat",
            owner_user_id="usr_wechat",
            token_hash="session-hash",
            authenticated_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    async with sql_session_factory.begin() as session:
        session.add(
            Experience(
                id="exp_wechat",
                owner_user_id="usr_wechat",
                type="work",
                title="Engineer",
            )
        )

    await repository.merge_users("usr_wechat", "usr_email")

    async with sql_session_factory() as session:
        source = await session.get(User, "usr_wechat")
        experience_owner = await session.scalar(
            select(Experience.owner_user_id).where(Experience.id == "exp_wechat")
        )
        identity_owner = await session.scalar(
            select(UserIdentity.owner_user_id).where(UserIdentity.id == "idn_wechat")
        )
        session_owner = await session.scalar(
            select(Session.owner_user_id).where(Session.id == "ses_wechat")
        )
    assert source is not None
    assert source.status == "merged"
    assert experience_owner == identity_owner == session_owner == "usr_email"


@pytest.mark.anyio
async def test_sql_usage_adapter_persists_append_only_ledger(sql_session_factory):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    auth_repository = SqlAuthRepository(sql_session_factory)
    await auth_repository.save_user(
        UserAccount(
            id="usr_1",
            status="active",
            email_encrypted=None,
            email_lookup_hash=None,
            created_at=now,
        )
    )
    repository = SqlUsageRepository(sql_session_factory)
    await repository.append_ai_task(
        "usr_1",
        "tr_1",
        now,
        Decimal("1.25"),
    )

    restarted = SqlUsageRepository(sql_session_factory)

    assert await restarted.count_ai_tasks("usr_1", now - timedelta(seconds=1)) == 1
    assert await restarted.daily_cost(now - timedelta(seconds=1)) == Decimal("1.250000")


@pytest.mark.anyio
async def test_sql_atomic_admission_serializes_requests_at_daily_boundary(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    auth_repository = SqlAuthRepository(sql_session_factory)
    await auth_repository.save_user(
        UserAccount(
            id="usr_1",
            status="active",
            email_encrypted=None,
            email_lookup_hash=None,
            created_at=now,
        )
    )
    repository = SqlUsageRepository(sql_session_factory)
    for index in range(19):
        await repository.append_ai_task("usr_1", f"tr_seed_{index}", now)
    service = UsageService(repository, type("Clock", (), {"now": lambda _: now})())

    first, second = await asyncio.gather(
        service.admit_ai_task("usr_1", "tr_20", "key-20"),
        service.admit_ai_task("usr_1", "tr_21", "key-21"),
    )

    assert sorted([first.allowed, second.allowed]) == [False, True]
    assert await repository.count_ai_tasks("usr_1", now) == 20


@pytest.mark.anyio
async def test_sql_privacy_adapter_persists_task_and_idempotency_mapping(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    auth_repository = SqlAuthRepository(sql_session_factory)
    await auth_repository.save_user(
        UserAccount(
            id="usr_1",
            status="active",
            email_encrypted=None,
            email_lookup_hash=None,
            created_at=now,
        )
    )
    task = PrivacyTask(
        id="tsk_1",
        owner_user_id="usr_1",
        type="data_export",
        status="queued",
        stage="queued",
        progress=0,
        trace_id="tr_1",
        queued_at=now,
    )
    repository = SqlPrivacyRepository(sql_session_factory)
    await repository.save_task(
        task,
        "/v1/me/data-exports",
        "export-key",
    )

    restarted = SqlPrivacyRepository(sql_session_factory)
    replay = await restarted.find_idempotent(
        "usr_1",
        "/v1/me/data-exports",
        "export-key",
    )

    assert replay == task


@pytest.mark.anyio
async def test_sql_deletion_state_survives_restart_and_replays_after_revocation(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    clock = type("Clock", (), {"now": lambda _: now})()
    repository = SqlAuthRepository(sql_session_factory)
    await repository.save_user(
        UserAccount(
            id="usr_1",
            status="active",
            email_encrypted=None,
            email_lookup_hash=None,
            created_at=now,
        )
    )
    await repository.save_session(
        SessionRecord(
            id="ses_1",
            owner_user_id="usr_1",
            token_hash=HmacSecretHasher().hash_secret(
                "raw-session",
                StaticKeys().get_key("session"),
            ),
            authenticated_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    auth_service = AuthService(
        repository=repository,
        email_sender=DiscardSender(),
        wechat_exchange=StaticWechat(),
        email_crypto=DeterministicEmailCrypto(),
        keys=StaticKeys(),
        hasher=HmacSecretHasher(),
        clock=clock,
        code_factory=lambda: "123456",
        token_factory=lambda: "unused",
        app_env="test",
    )
    authenticated = await auth_service.authenticate("raw-session")
    assert authenticated is not None
    service = PrivacyService(
        SqlPrivacyRepository(sql_session_factory),
        auth_service,
        clock,
    )

    task = await service.request_deletion(
        authenticated,
        "delete-key",
        "tr_delete",
    )

    restarted_auth = AuthService(
        repository=SqlAuthRepository(sql_session_factory),
        email_sender=DiscardSender(),
        wechat_exchange=StaticWechat(),
        email_crypto=DeterministicEmailCrypto(),
        keys=StaticKeys(),
        hasher=HmacSecretHasher(),
        clock=clock,
        code_factory=lambda: "123456",
        token_factory=lambda: "unused",
        app_env="test",
    )
    assert await restarted_auth.authenticate("raw-session") is None
    replay_identity = await restarted_auth.identify_deletion_replay("raw-session")
    assert replay_identity is not None
    replay = await SqlPrivacyRepository(sql_session_factory).find_idempotent(
        replay_identity.user_id,
        "/v1/me/deletion-requests",
        "delete-key",
    )
    assert replay == task
