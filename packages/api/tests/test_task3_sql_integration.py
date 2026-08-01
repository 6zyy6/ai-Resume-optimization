import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.models import (
    File,
    IdempotencyRecord,
    JobDescription,
    Outbox,
    Resume,
    ResumeSection,
    ResumeVersion,
    Session,
    Task,
    TaskEvent,
    UsageLedger,
    User,
    UserIdentity,
)
from app.db.ownership import authorized_owner_ids
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
from app.integrations.storage import MemoryStorage
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
from app.modules.tasks.service import TaskAdmission, TaskService
from app.modules.usage.service import (
    InMemoryUsageRepository,
    UsageAdmissionError,
    UsageService,
    admission_body_hash,
)
from app.modules.users.service import IdentityRecord, UserAccount
from app.workers.execution import TaskExecutor, resolve_operation
from app.workers.pipeline import configure_pipeline_operations


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
            password_hash="scrypt$v1$stored",
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
    assert user.password_hash == "scrypt$v1$stored"
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
async def test_sql_chained_merge_authorizes_all_historical_resources_and_dependencies(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    repository = SqlAuthRepository(sql_session_factory)
    for user_id in ("usr_final", "usr_email", "usr_wechat"):
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
    await repository.merge_users("usr_email", "usr_final")

    async with sql_session_factory() as session:
        source = await session.get(User, "usr_wechat")
        intermediate = await session.get(User, "usr_email")
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
        owner_ids = await authorized_owner_ids(session, "usr_final")
        section = await session.scalar(
            select(ResumeSection).where(
                ResumeSection.id == "section_wechat",
                ResumeSection.owner_user_id.in_(owner_ids),
            )
        )
    assert source is not None
    assert source.status == "merged"
    assert intermediate is not None
    assert intermediate.status == "merged"
    assert version_owner == section_owner == usage_owner == "usr_wechat"
    assert section is not None

    async with sql_session_factory() as session:
        resume = await SqlAlchemyResumeRepository(session).get(
            "resume_wechat",
            "usr_final",
        )
        version = await SqlAlchemyResumeVersionRepository(session).get(
            "version_wechat",
            "usr_final",
        )
    assert resume is not None
    assert version is not None
    assert (
        await SqlUsageRepository(sql_session_factory).count_ai_tasks(
            "usr_final",
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
async def test_sql_decision_excludes_released_usage_from_daily_limit(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_released_daily"))
        await session.flush()
        session.add(
            UsageLedger(
                id="usg_released_daily",
                owner_user_id="usr_released_daily",
                usage_type="ai_task",
                quantity=20,
                cost_cny=Decimal("0"),
                trace_id="tr_released_daily",
                state="released",
                created_at=now,
                updated_at=now,
            )
        )
    repository = SqlUsageRepository(sql_session_factory)
    service = UsageService(
        repository,
        type("Clock", (), {"now": lambda _: now})(),
    )

    decision = await service.decide_ai_task("usr_released_daily")

    assert decision.allowed is True
    assert await repository.count_ai_tasks("usr_released_daily", now) == 0


@pytest.mark.anyio
async def test_sql_usage_reports_task_service_running_task_and_cancel_release(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_running_summary"))
    clock = type("Clock", (), {"now": lambda _: now})()
    task_service = TaskService(sql_session_factory, clock=clock)
    usage_service = UsageService(SqlUsageRepository(sql_session_factory), clock)
    task = await task_service.create_task(
        "usr_running_summary",
        task_type="parse_jd",
        queue="ai.interactive",
        trace_id="tr_running_summary",
        idempotency_key="running-summary",
        admission=TaskAdmission.ai(),
    )

    running = await usage_service.summary("usr_running_summary")
    cancelled = await task_service.request_cancel("usr_running_summary", task.id)
    after_cancel = await usage_service.summary("usr_running_summary")
    async with sql_session_factory() as session:
        reservation_state = await session.scalar(
            select(UsageLedger.state).where(UsageLedger.task_id == task.id)
        )

    assert running.ai_tasks_running == 1
    assert cancelled.status == "cancelled"
    assert reservation_state == "released"
    assert after_cancel.ai_tasks_running == 0


@pytest.mark.anyio
async def test_sql_legacy_admission_counts_task_service_concurrency(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_mixed_concurrency"))
    clock = type("Clock", (), {"now": lambda _: now})()
    task_service = TaskService(sql_session_factory, clock=clock)
    usage_service = UsageService(SqlUsageRepository(sql_session_factory), clock)
    for index in range(2):
        await task_service.create_task(
            "usr_mixed_concurrency",
            task_type="parse_jd",
            queue="ai.interactive",
            trace_id=f"tr_mixed_{index}",
            idempotency_key=f"mixed-{index}",
            admission=TaskAdmission.ai(),
        )

    decision = await usage_service.decide_ai_task("usr_mixed_concurrency")
    admission = await usage_service.admit_ai_task(
        "usr_mixed_concurrency",
        "tr_mixed_third",
        "mixed-third",
    )
    summary = await usage_service.summary("usr_mixed_concurrency")
    async with sql_session_factory() as session:
        task_count = await session.scalar(select(func.count()).select_from(Task))

    assert decision.allowed is False
    assert decision.reason == "AI_CONCURRENCY_LIMIT_REACHED"
    assert admission.allowed is False
    assert admission.reason == "AI_CONCURRENCY_LIMIT_REACHED"
    assert summary.ai_tasks_running == 2
    assert task_count == 2


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
async def test_sql_admission_excludes_released_and_creates_bound_reservations(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    owner_ids = ("usr_released_seed", "usr_reserve", "usr_consume")
    async with sql_session_factory.begin() as session:
        session.add_all(User(id=owner_id) for owner_id in owner_ids)
        await session.flush()
        session.add(
            UsageLedger(
                id="usg_released_global_limit",
                owner_user_id="usr_released_seed",
                usage_type="ai_task",
                quantity=20,
                cost_cny=Decimal("100.00"),
                trace_id="tr_released_global_limit",
                state="released",
                task_id=None,
                created_at=now,
                updated_at=now,
            )
        )
    repository = SqlUsageRepository(sql_session_factory)
    service = UsageService(
        repository,
        type("Clock", (), {"now": lambda _: now})(),
    )

    released_decision = await service.admit_ai_task(
        "usr_reserve",
        "tr_reserve",
        "reserve-key",
        cost_cny=Decimal("1.00"),
    )

    assert released_decision.allowed is True
    assert released_decision.task_id is not None
    async with sql_session_factory() as session:
        task = await session.get(Task, released_decision.task_id)
        reservation = await session.scalar(
            select(UsageLedger).where(
                UsageLedger.owner_user_id == "usr_reserve",
                UsageLedger.task_id == released_decision.task_id,
            )
        )
    assert reservation is not None
    assert task is not None
    assert task.usage_type == "ai_task"
    assert reservation.state == "reserved"
    assert reservation.cost_cny == Decimal("1.00")

    released = await TaskService(sql_session_factory).release_ai_reservation(
        "usr_reserve",
        released_decision.task_id,
    )
    consumed_decision = await service.admit_ai_task(
        "usr_consume",
        "tr_consume",
        "consume-key",
        cost_cny=Decimal("1.00"),
    )
    assert consumed_decision.allowed is True
    assert consumed_decision.task_id is not None
    consumed = await TaskService(sql_session_factory).consume_ai_reservation(
        "usr_consume",
        consumed_decision.task_id,
        "run_consume",
    )

    assert released.state == "released"
    assert released.task_id == released_decision.task_id
    assert consumed.state == "consumed"
    assert consumed.task_id == consumed_decision.task_id
    assert consumed.ai_run_id == "run_consume"


@pytest.mark.anyio
async def test_sql_admission_rejects_projected_cost_above_global_limit(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_sql_projected"))
        await session.flush()
        session.add(
            UsageLedger(
                id="usg_sql_projected_995",
                owner_user_id="usr_sql_projected",
                usage_type="ai_task",
                quantity=1,
                cost_cny=Decimal("99.50"),
                trace_id="tr_sql_projected_995",
                state="consumed",
                task_id=None,
                created_at=now,
                updated_at=now,
            )
        )
    service = UsageService(
        SqlUsageRepository(sql_session_factory),
        type("Clock", (), {"now": lambda _: now})(),
    )

    decision = await service.admit_ai_task(
        "usr_sql_projected",
        "tr_sql_projected",
        "sql-projected-key",
        cost_cny=Decimal("1.00"),
    )

    async with sql_session_factory() as session:
        task_count = await session.scalar(select(func.count()).select_from(Task))
        ledger_count = await session.scalar(
            select(func.count()).select_from(UsageLedger)
        )

    assert decision.allowed is False
    assert decision.reason == "AI_LIMIT_REACHED"
    assert decision.task_id is None
    assert task_count == 0
    assert ledger_count == 1


@pytest.mark.anyio
async def test_sql_admission_rejects_negative_cost_without_writes(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_sql_negative"))
    repository = SqlUsageRepository(sql_session_factory)

    with pytest.raises(UsageAdmissionError) as caught:
        await repository.admit_ai_task(
            "usr_sql_negative",
            "tr_sql_negative",
            "sql-negative-key",
            now,
            now.replace(hour=0, minute=0, second=0, microsecond=0),
            16 * 60 * 60,
            "generic",
            False,
            Decimal("-1.00"),
            admission_body_hash("generic", False, Decimal("-1.00")),
        )

    assert caught.value.code == "USAGE_COST_INVALID"
    assert caught.value.status_code == 422
    async with sql_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(UsageLedger))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(IdempotencyRecord))
            == 0
        )


async def merged_admission_harness(
    sql_session_factory,
):
    now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    auth_repository = SqlAuthRepository(sql_session_factory)
    for user_id in ("usr_source", "usr_target"):
        await auth_repository.save_user(
            UserAccount(
                id=user_id,
                status="active",
                email_encrypted=None,
                email_lookup_hash=None,
                created_at=now,
            )
        )
    service = UsageService(
        SqlUsageRepository(sql_session_factory),
        type("Clock", (), {"now": lambda _: now})(),
    )
    first = await service.admit_ai_task(
        "usr_source",
        "tr_first",
        "merge-key",
        workflow_type="quality_check",
        cost_cny=Decimal("1.25"),
    )
    await auth_repository.merge_users("usr_source", "usr_target")
    return service, first


async def admission_row_counts(
    sql_session_factory,
    idempotency_key: str,
):
    async with sql_session_factory() as session:
        return (
            await session.scalar(
                select(func.count()).select_from(Task).where(Task.type == "ai_task")
            ),
            await session.scalar(
                select(func.count())
                .select_from(UsageLedger)
                .where(UsageLedger.usage_type == "ai_task")
            ),
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(
                    IdempotencyRecord.route == "/internal/ai-task-admissions",
                    IdempotencyRecord.key == idempotency_key,
                )
            ),
        )


@pytest.mark.anyio
async def test_sql_admission_same_input_replays_across_account_merge(
    sql_session_factory,
):
    service, first = await merged_admission_harness(sql_session_factory)
    replay = await service.admit_ai_task(
        "usr_target",
        "tr_replay",
        "merge-key",
        workflow_type="quality_check",
        cost_cny=Decimal("1.250"),
    )

    assert replay == first
    assert await admission_row_counts(sql_session_factory, "merge-key") == (1, 1, 1)


@pytest.mark.anyio
async def test_sql_admission_changed_input_is_rejected_across_account_merge(
    sql_session_factory,
):
    service, _ = await merged_admission_harness(sql_session_factory)
    with pytest.raises(UsageAdmissionError) as caught:
        await service.admit_ai_task(
            "usr_target",
            "tr_changed",
            "merge-key",
            workflow_type="quality_check",
            cost_cny=Decimal("2.00"),
        )

    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert caught.value.status_code == 409
    assert await admission_row_counts(sql_session_factory, "merge-key") == (1, 1, 1)


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
    async with sql_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TaskEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 1
        outbox = await session.scalar(select(Outbox))
        assert outbox.queue == "privacy"
        assert outbox.payload == {"task_id": task.id}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("request_type", "expected_user_status"),
    [
        ("data_export", "active"),
        ("account_deletion", "deleted"),
    ],
)
async def test_sql_privacy_tasks_reach_a_worker_terminal_state(
    sql_session_factory,
    request_type,
    expected_user_status,
):
    now, _, authenticated, service = await sql_privacy_harness(
        sql_session_factory
    )
    task = (
        await service.request_data_export(authenticated, "worker-key", "tr_worker")
        if request_type == "data_export"
        else await service.request_deletion(authenticated, "worker-key", "tr_worker")
    )
    storage = MemoryStorage()
    async with sql_session_factory.begin() as session:
        session.add(
            JobDescription(
                id=f"job_{request_type}",
                owner_user_id="usr_1",
                title="隐私副本岗位",
                raw_encrypted="仅属于当前用户的岗位正文",
                status="draft",
            )
        )
        if request_type == "account_deletion":
            stored = storage.put(
                "uploads/private.txt",
                b"private source",
                "text/plain",
            )
            session.add(
                File(
                    id="file_private",
                    owner_user_id="usr_1",
                    purpose="resume_import",
                    display_name="private.txt",
                    object_key="uploads/private.txt",
                    sha256=stored.sha256,
                    size=len(stored.content),
                    mime=stored.mime,
                    status="confirmed",
                    expires_at=now + timedelta(days=1),
                )
            )
    task_service = TaskService(sql_session_factory)
    configure_pipeline_operations(
        sql_session_factory,
        Settings(app_env="test", database_url="sqlite+aiosqlite://"),
        task_service,
        storage_override=storage,
    )

    result = await TaskExecutor(
        task_service,
        sleep=lambda _: None,
        jitter=lambda: 0,
    ).execute("usr_1", task.id, resolve_operation)

    assert result["status"] == "succeeded"
    async with sql_session_factory() as session:
        assert (await session.get(User, "usr_1")).status == expected_user_status
        if request_type == "data_export":
            file_row = await session.get(File, result["result_ref"])
            assert file_row is not None
            exported = storage.get(file_row.object_key)
            assert exported is not None
            payload = json.loads(exported.content)
            assert payload["jobs"][0]["raw"] == "仅属于当前用户的岗位正文"
            assert payload["account"]["user_id"] == "usr_1"
        else:
            assert await session.get(Session, "ses_1") is None
            assert await session.get(JobDescription, "job_account_deletion") is None
            assert await session.get(File, "file_private") is None
            assert storage.get("uploads/private.txt") is None
            user = await session.get(User, "usr_1")
            assert user.email_encrypted is None
            assert user.email_lookup_hash is None


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
        ("task_events", "INSERT"),
        ("outbox", "INSERT"),
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
        assert await session.scalar(select(func.count()).select_from(TaskEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 0
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
        assert await session.scalar(select(func.count()).select_from(TaskEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 1
        assert (await session.get(User, "usr_1")).status == "pending_deletion"
        assert (await session.get(Session, "ses_1")).revoked_at is not None
