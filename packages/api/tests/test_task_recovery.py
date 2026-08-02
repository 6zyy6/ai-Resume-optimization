import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import Base, Fact, TaskEvent, UsageLedger, User
from app.main import create_app
from app.modules.auth.router import require_session
from app.modules.auth.service import AuthenticatedSession
from app.modules.facts.service import FactService
from app.modules.tasks.service import (
    TaskAdmission,
    TaskClaimError,
    TaskService,
)
from app.workers.dispatcher import OutboxDispatcher, TaskQueueBusy
from app.workers.execution import HttpServiceError, TaskExecutor, retry_delay, should_retry


class RedisDown:
    def publish(self, task_id: str, owner_user_id: str, queue: str) -> None:
        raise ConnectionError("redis unavailable")


def _run(awaitable):
    return asyncio.run(awaitable)


async def _seed_user(sessions) -> None:
    async with sessions.begin() as session:
        session.add(User(id="usr_recovery"))


class RecoveryClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


@pytest.mark.anyio
async def test_worker_crash_then_reclaim_completes_one_result(sql_session_factory):
    """A crash after claim must be reclaimable without two completion results."""
    await _seed_user(sql_session_factory)
    clock = RecoveryClock()
    first_process = TaskService(sql_session_factory, clock=clock)
    task = await first_process.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_crash",
        idempotency_key="recovery-1",
        admission=TaskAdmission.ai(),
    )
    abandoned = await first_process.claim_task("usr_recovery", task.id)
    assert abandoned is not None
    assert abandoned.attempts == 1

    clock.value += timedelta(seconds=301)
    restarted_process = TaskService(sql_session_factory, clock=clock)
    reclaimed = await restarted_process.claim_task("usr_recovery", task.id)
    assert reclaimed is not None
    completed = await restarted_process.complete_task(
        "usr_recovery",
        task.id,
        reclaimed.token,
        "resume_version:rv_recovered",
    )
    with pytest.raises(TaskClaimError):
        await first_process.complete_task(
            "usr_recovery",
            task.id,
            abandoned.token,
            "resume_version:rv_duplicate",
        )

    assert reclaimed is not None
    assert reclaimed.attempts == 2
    assert completed.result_ref == "resume_version:rv_recovered"
    async with sql_session_factory() as session:
        completion_count = await session.scalar(
            select(func.count())
            .select_from(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.stage == "succeeded")
        )
    assert completion_count == 1


@pytest.mark.anyio
async def test_terminal_failure_pending_write_rolls_back_with_its_transaction(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_terminal_pending_rollback",
        idempotency_key="terminal-pending-rollback",
        admission=TaskAdmission.ai(),
    )
    claim = await service.claim_task("usr_recovery", task.id)
    assert claim is not None

    with pytest.raises(RuntimeError, match="pending write fault"):
        async with sql_session_factory.begin() as session:
            await service.mark_terminal_failure_pending_in_session(
                session,
                "usr_recovery",
                task.id,
                claim.token,
                "ValueError",
                retryable=False,
            )
            raise RuntimeError("pending write fault")

    stored = await service.get_task("usr_recovery", task.id)
    assert stored is not None
    assert stored.status == "running"
    assert stored.stage == "running"
    assert stored.error_code is None


@pytest.mark.anyio
async def test_terminal_pending_reclaim_is_owner_scoped_and_rejects_stale_claims(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    clock = RecoveryClock()
    service = TaskService(sql_session_factory, clock=clock)
    task = await service.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_terminal_pending_claim",
        idempotency_key="terminal-pending-claim",
        admission=TaskAdmission.ai(),
    )
    claim = await service.claim_task("usr_recovery", task.id)
    assert claim is not None

    with pytest.raises(TaskClaimError):
        await service.mark_terminal_failure_pending(
            "usr_other",
            task.id,
            claim.token,
            "ValueError",
            retryable=False,
        )
    pending_claim = await service.mark_terminal_failure_pending(
        "usr_recovery",
        task.id,
        claim.token,
        "ValueError",
        retryable=False,
    )
    assert pending_claim.terminal_failure_pending is True
    assert pending_claim.terminal_failure_error_code == "ValueError"
    assert pending_claim.terminal_failure_retryable is False

    clock.value += timedelta(seconds=301)
    reclaimed = await service.claim_task("usr_recovery", task.id)
    assert reclaimed is not None
    assert reclaimed.token != claim.token
    assert reclaimed.attempts == claim.attempts
    assert reclaimed.terminal_failure_pending is True
    assert reclaimed.terminal_failure_error_code == "ValueError"
    assert reclaimed.terminal_failure_retryable is False

    with pytest.raises(TaskClaimError):
        await service.mark_terminal_failure_pending(
            "usr_recovery",
            task.id,
            claim.token,
            "RuntimeError",
            retryable=False,
        )
    stored = await service.get_task("usr_recovery", task.id)
    assert stored is not None
    assert stored.stage == "terminal_failure_pending_permanent"
    assert stored.error_code == "ValueError"


@pytest.mark.anyio
async def test_executor_retries_only_transient_failures(sql_session_factory):
    """Retrying validation failures, or not retrying 5xx, violates the worker policy."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    transient = await service.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_transient",
        idempotency_key="recovery-2",
        admission=TaskAdmission.ai(),
    )
    permanent = await service.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_permanent",
        idempotency_key="recovery-3",
        admission=TaskAdmission.ai(),
    )
    executor = TaskExecutor(service, sleep=lambda _: None, jitter=lambda: 0)
    transient_attempts = 0
    permanent_attempts = 0

    async def transient_operation(_claim) -> str:
        nonlocal transient_attempts
        transient_attempts += 1
        if transient_attempts < 3:
            raise HttpServiceError(503)
        return "resume_version:rv_retry"

    async def permanent_operation(_claim) -> str:
        nonlocal permanent_attempts
        permanent_attempts += 1
        raise ValueError("invalid workflow input")

    transient_result = await executor.execute(
        "usr_recovery", transient.id, lambda _: transient_operation
    )
    permanent_result = await executor.execute(
        "usr_recovery", permanent.id, lambda _: permanent_operation
    )

    assert transient_attempts == 3
    assert transient_result["status"] == "succeeded"
    assert transient_result["result_ref"] == "resume_version:rv_retry"
    assert permanent_attempts == 1
    assert permanent_result["status"] == "failed"
    assert permanent_result["error_code"] == "ValueError"
    assert should_retry(TimeoutError()) is True
    assert should_retry(HttpServiceError(429)) is True
    assert should_retry(HttpServiceError(400)) is False
    assert retry_delay(1, jitter=lambda: 0) == 1
    assert retry_delay(2, jitter=lambda: 0) == 2


@pytest.mark.anyio
async def test_executor_finalizes_cancelled_running_task_that_never_started_pi(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_cancelled_without_run",
        idempotency_key="cancelled-without-run",
        admission=TaskAdmission.ai(),
    )

    async def cancel_before_pi(_claim) -> str:
        await service.request_cancel("usr_recovery", task.id)
        return "unused"

    result = await TaskExecutor(service).execute(
        "usr_recovery",
        task.id,
        lambda _: cancel_before_pi,
    )
    stored = await service.get_task("usr_recovery", task.id)
    async with sql_session_factory() as session:
        reservation = await session.scalar(
            select(UsageLedger).where(UsageLedger.task_id == task.id)
        )

    assert result["status"] == "cancelled"
    assert stored is not None
    assert stored.claim_token is None
    assert stored.active_ai_run_id is None
    assert reservation is not None
    assert reservation.state == "released"
    assert reservation.ai_run_id is None


@pytest.mark.anyio
async def test_cancelled_timeout_keeps_ambiguous_reservation_and_claim(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_cancelled_timeout",
        idempotency_key="cancelled-timeout",
        admission=TaskAdmission.ai(),
    )
    claim_token = None

    async def cancel_then_timeout(claim) -> str:
        nonlocal claim_token
        claim_token = claim.token
        await service.request_cancel("usr_recovery", task.id)
        raise TimeoutError("Pi acceptance is ambiguous")

    result = await TaskExecutor(service).execute(
        "usr_recovery",
        task.id,
        lambda _: cancel_then_timeout,
    )
    stored = await service.get_task("usr_recovery", task.id)
    async with sql_session_factory() as session:
        reservation = await session.scalar(
            select(UsageLedger).where(UsageLedger.task_id == task.id)
        )

    assert result["status"] == "cancelled"
    assert stored is not None
    assert stored.claim_token == claim_token
    assert stored.active_ai_run_id is None
    assert reservation is not None
    assert reservation.state == "reserved"


@pytest.mark.anyio
async def test_executor_never_releases_cancelled_task_registered_during_post_race(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_cancelled_with_run",
        idempotency_key="cancelled-with-run",
        admission=TaskAdmission.ai(),
    )

    async def cancel_then_register(claim) -> str:
        await service.request_cancel("usr_recovery", task.id)
        should_continue = await service.register_ai_run(
            "usr_recovery",
            task.id,
            claim.token,
            "run_post_race",
        )
        assert should_continue is False
        return "unused"

    result = await TaskExecutor(service).execute(
        "usr_recovery",
        task.id,
        lambda _: cancel_then_register,
    )
    stored = await service.get_task("usr_recovery", task.id)
    async with sql_session_factory() as session:
        reservation = await session.scalar(
            select(UsageLedger).where(UsageLedger.task_id == task.id)
        )

    assert result["status"] == "cancelled"
    assert stored is not None
    assert stored.active_ai_run_id == "run_post_race"
    assert reservation is not None
    assert reservation.state == "consumed"
    assert reservation.ai_run_id == "run_post_race"


@pytest.mark.anyio
async def test_non_retryable_pre_pi_failure_releases_unused_reservation(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_pre_pi_validation",
        idempotency_key="pre-pi-validation",
        admission=TaskAdmission.ai(),
    )

    async def invalid_before_pi(_claim) -> str:
        raise ValueError("PRE_PI_VALIDATION")

    result = await TaskExecutor(service).execute(
        "usr_recovery",
        task.id,
        lambda _: invalid_before_pi,
    )
    async with sql_session_factory() as session:
        reservation = await session.scalar(
            select(UsageLedger).where(UsageLedger.task_id == task.id)
        )

    assert result == {
        "id": task.id,
        "status": "failed",
        "result_ref": None,
        "error_code": "ValueError",
    }
    assert reservation is not None
    assert reservation.state == "released"


@pytest.mark.anyio
async def test_transport_ambiguity_does_not_release_unused_reservation(
    sql_session_factory,
):
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.interactive",
        trace_id="tr_ambiguous_timeout",
        idempotency_key="ambiguous-timeout",
        admission=TaskAdmission.ai(),
    )

    async def ambiguous_transport(_claim) -> str:
        raise TimeoutError("Pi may have accepted the run")

    result = await TaskExecutor(
        service,
        sleep=lambda _: None,
        jitter=lambda: 0,
    ).execute(
        "usr_recovery",
        task.id,
        lambda _: ambiguous_transport,
    )
    async with sql_session_factory() as session:
        reservation = await session.scalar(
            select(UsageLedger).where(UsageLedger.task_id == task.id)
        )

    assert result["status"] == "failed"
    assert result["error_code"] == "TimeoutError"
    assert reservation is not None
    assert reservation.state == "reserved"


@pytest.mark.anyio
async def test_worker_shutdown_leaves_claimed_task_recoverable(sql_session_factory):
    """Turning worker cancellation into business failure would prevent crash recovery."""
    await _seed_user(sql_session_factory)
    service = TaskService(sql_session_factory)
    task = await service.create_task(
        "usr_recovery",
        task_type="resume_optimize",
        queue="ai.batch",
        trace_id="tr_shutdown",
        idempotency_key="recovery-shutdown",
        admission=TaskAdmission.ai(),
    )
    executor = TaskExecutor(service, sleep=lambda _: None, jitter=lambda: 0)

    async def interrupted_operation(_claim) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(
            "usr_recovery", task.id, lambda _: interrupted_operation
        )

    stored = await service.get_task("usr_recovery", task.id)
    assert stored is not None
    assert stored.status == "running"
    assert stored.attempts == 1


def test_redis_failure_returns_queue_busy_without_breaking_fact_reads(tmp_path):
    """A broker outage must not make already-persisted synchronous facts unreadable."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as session:
            session.add(User(id="usr_recovery"))
            await session.flush()
            session.add(
                Fact(
                    id="fact_existing",
                    owner_user_id="usr_recovery",
                    kind="achievement",
                    value_encrypted="existing synchronous fact",
                    status="unconfirmed",
                )
            )
        return sessions

    sessions = _run(setup())
    application = create_app(
        Settings(app_env="test", database_url="sqlite+aiosqlite://")
    )
    application.state.fact_service = FactService(sessions)
    task_service = TaskService(sessions)
    application.state.task_service = task_service
    dispatcher = OutboxDispatcher(
        sessions,
        RedisDown(),
        retry_base_seconds=0,
        jitter=lambda: 0,
    )

    def authenticated() -> AuthenticatedSession:
        now = datetime.now(timezone.utc)
        return AuthenticatedSession(
            "usr_recovery", "ses_recovery", now, now + timedelta(days=1)
        )

    application.dependency_overrides[require_session] = authenticated
    task = _run(
        task_service.create_task(
            "usr_recovery",
            task_type="resume_optimize",
            queue="ai.interactive",
            trace_id="tr_queue_down",
            idempotency_key="queue-down-1",
            admission=TaskAdmission.ai(),
        )
    )
    with pytest.raises(TaskQueueBusy):
        _run(dispatcher.dispatch_task(task.id))
    with TestClient(application) as client:
        fact = client.get("/v1/facts/fact_existing")

    assert fact.status_code == 200
    assert fact.json()["value"] == "existing synchronous fact"
    _run(engine.dispose())
