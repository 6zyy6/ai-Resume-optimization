import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.models import (
    IdempotencyRecord,
    Resume,
    ResumeSection,
    ResumeVersion,
    Session,
    Task,
    UsageLedger,
    User,
    UserAlias,
    UserIdentity,
)
from app.db.repositories import (
    SqlAlchemyResumeRepository,
    SqlAlchemyResumeVersionRepository,
)
from app.db.task3_repositories import (
    SqlAuthRepository,
    SqlPrivacyRepository,
    SqlUsageRepository,
)
from app.main import ApplicationDependencies, create_app
from app.modules.auth.preflight import InMemoryAuthPreflightStore
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
    UsageAdmissionError,
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
        auth_preflight=InMemoryAuthPreflightStore(),
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
        preflight_store=InMemoryAuthPreflightStore(),
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
        preflight_store=InMemoryAuthPreflightStore(),
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
async def test_sql_confirmed_merge_uses_alias_for_append_only_history_and_dependencies(
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
            Resume(
                id="resume_wechat",
                owner_user_id="usr_wechat",
                kind="base",
                title="Resume",
            )
        )
        await session.flush()
        session.add(
            ResumeVersion(
                id="version_wechat",
                owner_user_id="usr_wechat",
                resume_id="resume_wechat",
                snapshot_json={"title": "Resume"},
                snapshot_hash="snapshot-hash",
                created_by="usr_wechat",
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            ResumeSection(
                id="section_wechat",
                owner_user_id="usr_wechat",
                resume_version_id="version_wechat",
                section_key="experience",
                index_data={},
            )
        )
        session.add(
            UsageLedger(
                id="usage_wechat",
                owner_user_id="usr_wechat",
                usage_type="ai_task",
                quantity=1,
                cost_cny=Decimal("1.25"),
                trace_id="tr_usage",
                created_at=now,
            )
        )

    await repository.merge_users("usr_wechat", "usr_email")

    async with sql_session_factory() as session:
        source = await session.get(User, "usr_wechat")
        alias = await session.get(UserAlias, "usr_wechat")
        version_owner = await session.scalar(
            select(ResumeVersion.owner_user_id).where(
                ResumeVersion.id == "version_wechat"
            )
        )
        section_owner = await session.scalar(
            select(ResumeSection.owner_user_id).where(
                ResumeSection.id == "section_wechat"
            )
        )
        usage_owner = await session.scalar(
            select(UsageLedger.owner_user_id).where(
                UsageLedger.id == "usage_wechat"
            )
        )
    assert source is not None
    assert source.status == "merged"
    assert alias is not None
    assert alias.canonical_user_id == "usr_email"
    assert version_owner == section_owner == usage_owner == "usr_wechat"

    async with sql_session_factory() as session:
        resume = await SqlAlchemyResumeRepository(session).get(
            "resume_wechat",
            "usr_email",
        )
        version = await SqlAlchemyResumeVersionRepository(session).get(
            "version_wechat",
            "usr_email",
        )
    assert resume is not None
    assert version is not None
    assert (
        await SqlUsageRepository(sql_session_factory).count_ai_tasks(
            "usr_email",
            now,
        )
        == 1
    )


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
async def test_sql_atomic_admission_detects_idempotency_semantic_mismatch(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    await SqlAuthRepository(sql_session_factory).save_user(
        UserAccount(
            id="usr_1",
            status="active",
            email_encrypted=None,
            email_lookup_hash=None,
            created_at=now,
        )
    )
    repository = SqlUsageRepository(sql_session_factory)
    service = UsageService(repository, type("Clock", (), {"now": lambda _: now})())
    first = await service.admit_ai_task(
        "usr_1",
        "tr_first",
        "same-key",
        workflow_type="quality_check",
        cost_cny=Decimal("1.25"),
    )
    replay = await service.admit_ai_task(
        "usr_1",
        "tr_replay",
        "same-key",
        workflow_type="quality_check",
        cost_cny=Decimal("1.250"),
    )
    assert replay == first

    with pytest.raises(UsageAdmissionError) as caught:
        await service.admit_ai_task(
            "usr_1",
            "tr_changed",
            "same-key",
            workflow_type="quality_check",
            cost_cny=Decimal("2.00"),
        )

    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert caught.value.status_code == 409
    assert await repository.count_ai_tasks("usr_1", now) == 1


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


async def sql_privacy_harness(sql_session_factory):
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
        preflight_store=InMemoryAuthPreflightStore(),
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
    return now, auth_service, authenticated, service


@pytest.mark.anyio
async def test_sql_deletion_state_survives_restart_and_replays_after_revocation(
    sql_session_factory,
):
    now, auth_service, authenticated, service = await sql_privacy_harness(
        sql_session_factory
    )

    task = await service.request_deletion(
        authenticated,
        "delete-key",
        "tr_delete",
    )

    restarted_auth = AuthService(
        repository=SqlAuthRepository(sql_session_factory),
        preflight_store=InMemoryAuthPreflightStore(),
        email_sender=DiscardSender(),
        wechat_exchange=StaticWechat(),
        email_crypto=DeterministicEmailCrypto(),
        keys=StaticKeys(),
        hasher=HmacSecretHasher(),
        clock=auth_service.clock,
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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("table_name", "operation"),
    [
        ("tasks", "INSERT"),
        ("idempotency_records", "INSERT"),
        ("users", "UPDATE"),
        ("sessions", "UPDATE"),
    ],
)
async def test_sql_deletion_acceptance_rolls_back_every_step_before_retry(
    sql_session_factory,
    table_name,
    operation,
):
    _, _, authenticated, service = await sql_privacy_harness(
        sql_session_factory
    )
    trigger_name = f"fail_deletion_{table_name}"
    async with sql_session_factory.begin() as session:
        await session.execute(
            text(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE {operation} ON {table_name}
                BEGIN
                  SELECT RAISE(ABORT, 'injected deletion failure');
                END
                """
            )
        )

    with pytest.raises(IntegrityError):
        await service.request_deletion(
            authenticated,
            "delete-key",
            "tr_delete",
        )

    async with sql_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
        assert (
            await session.scalar(
                select(func.count()).select_from(IdempotencyRecord)
            )
            == 0
        )
        assert (await session.get(User, "usr_1")).status == "active"
        assert (await session.get(Session, "ses_1")).revoked_at is None

    async with sql_session_factory.begin() as session:
        await session.execute(text(f"DROP TRIGGER {trigger_name}"))

    task = await service.request_deletion(
        authenticated,
        "delete-key",
        "tr_delete_retry",
    )
    replay = await service.request_deletion(
        authenticated,
        "delete-key",
        "tr_delete_replay",
    )
    assert replay == task
    async with sql_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(IdempotencyRecord)
            )
            == 1
        )
        assert (await session.get(User, "usr_1")).status == "pending_deletion"
        assert (await session.get(Session, "ses_1")).revoked_at is not None
