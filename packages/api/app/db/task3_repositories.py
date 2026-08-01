from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer

from app.core.ids import new_id
from app.db.models import (
    IdempotencyRecord,
    File,
    Outbox,
    Session,
    Task,
    TaskEvent,
    UsageLedger,
    User,
    UserAlias,
    UserConsent,
    UserIdentity,
)
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.db.ports import is_valid_cost_cny
from app.modules.auth.service import SessionRecord
from app.modules.privacy.service import PrivacyExportArtifact, PrivacyTask
from app.modules.usage.service import (
    GLOBAL_AI_COST_ADVISORY_LOCK_ID,
    UsageAdmissionError,
    UsageDecision,
    UsageRecord,
    evaluate_admission_usage,
)
from app.modules.users.service import (
    ConsentRecord,
    IdentityRecord,
    UserAccount,
)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class SqlAuthRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @staticmethod
    def _user(row: User | None) -> UserAccount | None:
        if row is None:
            return None
        return UserAccount(
            id=row.id,
            status=row.status,
            email_encrypted=row.email_encrypted,
            email_lookup_hash=row.email_lookup_hash,
            created_at=_as_utc(row.created_at),
            password_hash=row.password_hash,
        )

    async def find_user(self, user_id: str) -> UserAccount | None:
        async with self.sessions() as session:
            canonical = await canonical_user_id(session, user_id)
            return self._user(
                await session.get(
                    User,
                    canonical,
                    options=(undefer(User.password_hash),),
                )
            )

    async def find_user_by_email_hash(self, email_hash: str) -> UserAccount | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(User)
                .options(undefer(User.password_hash))
                .where(User.email_lookup_hash == email_hash)
            )
            return self._user(row)

    async def find_user_by_identity(
        self,
        identity_type: str,
        subject_hash: str,
    ) -> UserAccount | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(User)
                .options(undefer(User.password_hash))
                .join(UserIdentity, UserIdentity.owner_user_id == User.id)
                .where(
                    UserIdentity.type == identity_type,
                    UserIdentity.external_subject_hash == subject_hash,
                )
            )
            return self._user(row)

    async def save_user(self, user: UserAccount) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(User, user.id)
            if row is None:
                session.add(
                    User(
                        id=user.id,
                        status=user.status,
                        email_encrypted=user.email_encrypted,
                        email_lookup_hash=user.email_lookup_hash,
                        password_hash=user.password_hash,
                        created_at=user.created_at,
                    )
                )
            else:
                row.status = user.status
                row.email_encrypted = user.email_encrypted
                row.email_lookup_hash = user.email_lookup_hash
                row.password_hash = user.password_hash

    async def save_identity(self, identity: IdentityRecord) -> None:
        async with self.sessions.begin() as session:
            session.add(
                UserIdentity(
                    id=identity.id,
                    owner_user_id=identity.owner_user_id,
                    type=identity.identity_type,
                    external_subject_hash=identity.external_subject_hash,
                    verified_at=identity.verified_at,
                )
            )

    async def save_consent(self, consent: ConsentRecord) -> None:
        async with self.sessions.begin() as session:
            session.add(
                UserConsent(
                    id=consent.id,
                    owner_user_id=consent.owner_user_id,
                    document_type=consent.document_type,
                    document_version=consent.document_version,
                    decision=consent.decision,
                    decided_at=consent.decided_at,
                )
            )

    async def create_user_with_identity_and_consents(
        self,
        user: UserAccount,
        identity: IdentityRecord,
        consents: tuple[ConsentRecord, ...],
    ) -> None:
        async with self.sessions.begin() as session:
            session.add(
                User(
                    id=user.id,
                    status=user.status,
                    email_encrypted=user.email_encrypted,
                    email_lookup_hash=user.email_lookup_hash,
                    password_hash=user.password_hash,
                    created_at=user.created_at,
                )
            )
            session.add(
                UserIdentity(
                    id=identity.id,
                    owner_user_id=identity.owner_user_id,
                    type=identity.identity_type,
                    external_subject_hash=identity.external_subject_hash,
                    verified_at=identity.verified_at,
                )
            )
            session.add_all(
                [
                    UserConsent(
                        id=consent.id,
                        owner_user_id=consent.owner_user_id,
                        document_type=consent.document_type,
                        document_version=consent.document_version,
                        decision=consent.decision,
                        decided_at=consent.decided_at,
                    )
                    for consent in consents
                ]
            )

    async def save_session(self, auth_session: SessionRecord) -> None:
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(Session).where(Session.token_hash == auth_session.token_hash)
            )
            if row is None:
                session.add(
                    Session(
                        id=auth_session.id,
                        owner_user_id=auth_session.owner_user_id,
                        token_hash=auth_session.token_hash,
                        expires_at=auth_session.expires_at,
                        revoked_at=auth_session.revoked_at,
                        device_type=f"auth:{int(auth_session.authenticated_at.timestamp())}",
                    )
                )
            else:
                row.owner_user_id = auth_session.owner_user_id
                row.expires_at = auth_session.expires_at
                row.revoked_at = auth_session.revoked_at
                row.device_type = (
                    f"auth:{int(auth_session.authenticated_at.timestamp())}"
                )

    async def find_session(self, token_hash: str) -> SessionRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(Session).where(Session.token_hash == token_hash)
            )
            if row is None:
                return None
            prefix, _, epoch = row.device_type.partition(":")
            authenticated_at = (
                datetime.fromtimestamp(int(epoch), timezone.utc)
                if prefix == "auth" and epoch.isdigit()
                else _as_utc(row.expires_at) - timedelta(days=30)
            )
            return SessionRecord(
                id=row.id,
                owner_user_id=row.owner_user_id,
                token_hash=row.token_hash,
                authenticated_at=authenticated_at,
                expires_at=_as_utc(row.expires_at),
                revoked_at=_as_utc(row.revoked_at) if row.revoked_at else None,
            )


    async def revoke_all_sessions(self, user_id: str, now: datetime) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(Session)
                .where(
                    Session.owner_user_id == user_id,
                    Session.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )

    async def consents_for_user(
        self,
        user_id: str,
    ) -> tuple[ConsentRecord, ...]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(UserConsent).where(
                        UserConsent.owner_user_id == user_id
                    )
                )
            ).all()
            return tuple(
                ConsentRecord(
                    id=row.id,
                    owner_user_id=row.owner_user_id,
                    document_type=row.document_type,
                    document_version=row.document_version,
                    decision=row.decision,
                    decided_at=_as_utc(row.decided_at),
                )
                for row in rows
            )

    async def set_password_if_missing(
        self,
        user_id: str,
        password_hash: str,
    ) -> bool:
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(User)
                .where(
                    User.id == user_id,
                    User.password_hash.is_(None),
                )
                .values(password_hash=password_hash)
            )
            return result.rowcount == 1

    async def merge_users(
        self,
        source_user_id: str,
        target_user_id: str,
    ) -> None:
        async with self.sessions() as session:
            await session.begin()
            try:
                target_user_id = await canonical_user_id(session, target_user_id)
                session.add(
                    UserAlias(
                        alias_user_id=source_user_id,
                        canonical_user_id=target_user_id,
                        created_at=datetime.now(timezone.utc),
                    )
                )
                for model in (UserIdentity, UserConsent, Session):
                    await session.execute(
                        update(model)
                        .where(model.owner_user_id == source_user_id)
                        .values(owner_user_id=target_user_id)
                    )
                await session.execute(
                    update(User)
                    .where(User.id == source_user_id)
                    .values(status="merged")
                )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise


class SqlUsageRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def append_ai_task(
        self,
        owner_user_id: str,
        trace_id: str,
        created_at: datetime,
        cost_cny: Decimal = Decimal("0"),
    ) -> UsageRecord:
        if not is_valid_cost_cny(cost_cny):
            raise UsageAdmissionError(
                "USAGE_COST_INVALID",
                "AI task cost must be a finite non-negative Decimal",
                422,
            )
        async with self.sessions.begin() as session:
            owner_user_id = await canonical_user_id(session, owner_user_id)
            row = UsageLedger(
                id=new_id("usg"),
                owner_user_id=owner_user_id,
                usage_type="ai_task",
                quantity=1,
                cost_cny=cost_cny,
                trace_id=trace_id,
                state="consumed",
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(row)
        return UsageRecord(
            id=row.id,
            owner_user_id=row.owner_user_id,
            usage_type=row.usage_type,
            quantity=row.quantity,
            cost_cny=row.cost_cny,
            trace_id=row.trace_id,
            created_at=_as_utc(row.created_at),
            state=row.state,
            task_id=row.task_id,
            ai_run_id=row.ai_run_id,
            updated_at=_as_utc(row.updated_at),
        )

    async def count_ai_tasks(self, owner_user_id: str, since: datetime) -> int:
        async with self.sessions() as session:
            owner_ids = await authorized_owner_ids(session, owner_user_id)
            return int(
                await session.scalar(
                    select(func.coalesce(func.sum(UsageLedger.quantity), 0)).where(
                        UsageLedger.owner_user_id.in_(owner_ids),
                        UsageLedger.usage_type == "ai_task",
                        UsageLedger.state.in_(("reserved", "consumed")),
                        UsageLedger.created_at >= since,
                    )
                )
                or 0
            )

    async def count_consumed_ai_tasks(
        self,
        owner_user_id: str,
        since: datetime,
    ) -> int:
        async with self.sessions() as session:
            owner_ids = await authorized_owner_ids(session, owner_user_id)
            return int(
                await session.scalar(
                    select(func.coalesce(func.sum(UsageLedger.quantity), 0)).where(
                        UsageLedger.owner_user_id.in_(owner_ids),
                        UsageLedger.usage_type == "ai_task",
                        UsageLedger.state == "consumed",
                        UsageLedger.created_at >= since,
                    )
                )
                or 0
            )

    async def running_ai_tasks(self, owner_user_id: str) -> int:
        async with self.sessions() as session:
            owner_ids = await authorized_owner_ids(session, owner_user_id)
            return int(
                await session.scalar(
                    select(func.count()).select_from(Task).where(
                        Task.owner_user_id.in_(owner_ids),
                        Task.usage_type == "ai_task",
                        Task.status.in_(("queued", "running")),
                    )
                )
                or 0
            )

    async def daily_cost(self, since: datetime) -> Decimal:
        async with self.sessions() as session:
            value = await session.scalar(
                select(func.coalesce(func.sum(UsageLedger.cost_cny), 0)).where(
                    UsageLedger.usage_type == "ai_task",
                    UsageLedger.state == "consumed",
                    UsageLedger.created_at >= since
                )
            )
            return Decimal(value or 0)

    async def admission_cost(self, since: datetime) -> Decimal:
        async with self.sessions() as session:
            value = await session.scalar(
                select(func.coalesce(func.sum(UsageLedger.cost_cny), 0)).where(
                    UsageLedger.usage_type == "ai_task",
                    UsageLedger.state.in_(("reserved", "consumed")),
                    UsageLedger.created_at >= since,
                )
            )
            return Decimal(value or 0)

    async def admit_ai_task(
        self,
        owner_user_id: str,
        trace_id: str,
        idempotency_key: str,
        created_at: datetime,
        day_start: datetime,
        retry_after: int,
        workflow_type: str,
        is_retry: bool,
        cost_cny: Decimal,
        body_hash: str,
    ) -> UsageDecision:
        if not is_valid_cost_cny(cost_cny):
            raise UsageAdmissionError(
                "USAGE_COST_INVALID",
                "AI task cost must be a finite non-negative Decimal",
                422,
            )
        route = "/internal/ai-task-admissions"
        async with self.sessions() as session:
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            else:
                await session.begin()
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            f"{GLOBAL_AI_COST_ADVISORY_LOCK_ID})"
                        )
                    )
            try:
                owner_user_id = await canonical_user_id(session, owner_user_id)
                owner_ids = await authorized_owner_ids(session, owner_user_id)
                existing = await session.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.owner_user_id.in_(owner_ids),
                        IdempotencyRecord.route == route,
                        IdempotencyRecord.key == idempotency_key,
                    )
                )
                if existing is not None and existing.response_json is not None:
                    if existing.body_hash != body_hash:
                        raise UsageAdmissionError(
                            "IDEMPOTENCY_KEY_REUSED",
                            "Idempotency key was reused with different input",
                            409,
                        )
                    payload = existing.response_json
                    await session.commit()
                    return UsageDecision(
                        payload["allowed"],
                        payload["reason"],
                        payload["retry_after"],
                        payload["task_id"],
                    )

                cost = Decimal(
                    await session.scalar(
                        select(func.coalesce(func.sum(UsageLedger.cost_cny), 0)).where(
                            UsageLedger.usage_type == "ai_task",
                            UsageLedger.state.in_(("reserved", "consumed")),
                            UsageLedger.created_at >= day_start,
                        )
                    )
                    or 0
                )
                daily_tasks = int(
                    await session.scalar(
                        select(func.coalesce(func.sum(UsageLedger.quantity), 0)).where(
                            UsageLedger.owner_user_id.in_(owner_ids),
                            UsageLedger.usage_type == "ai_task",
                            UsageLedger.state.in_(("reserved", "consumed")),
                            UsageLedger.created_at >= day_start,
                        )
                    )
                    or 0
                )
                running_tasks = int(
                    await session.scalar(
                        select(func.count()).select_from(Task).where(
                            Task.owner_user_id.in_(owner_ids),
                            Task.usage_type == "ai_task",
                            Task.status.in_(("queued", "running")),
                        )
                    )
                    or 0
                )
                decision = evaluate_admission_usage(
                    cost,
                    cost_cny,
                    daily_tasks,
                    running_tasks,
                    retry_after,
                    is_retry,
                )
                if decision.allowed:
                    task_id = new_id("tsk")
                    session.add(
                        Task(
                            id=task_id,
                            owner_user_id=owner_user_id,
                            type="ai_task",
                            status="queued",
                            priority=0,
                            resource_type=workflow_type,
                            trace_id=trace_id,
                            attempts=0,
                            max_attempts=3,
                            queued_at=created_at,
                            stage="queued",
                            progress=0,
                            usage_type="ai_task",
                        )
                    )
                    session.add(
                        UsageLedger(
                            id=new_id("usg"),
                            owner_user_id=owner_user_id,
                            usage_type="ai_task",
                            quantity=1,
                            cost_cny=cost_cny,
                            trace_id=trace_id,
                            state="reserved",
                            task_id=task_id,
                            created_at=created_at,
                            updated_at=created_at,
                        )
                    )
                    decision = UsageDecision(
                        True,
                        decision.reason,
                        decision.retry_after,
                        task_id,
                    )
                session.add(
                    IdempotencyRecord(
                        id=new_id("idem"),
                        owner_user_id=owner_user_id,
                        route=route,
                        key=idempotency_key,
                        body_hash=body_hash,
                        response_status=202 if decision.allowed else 429,
                        response_json={
                            "allowed": decision.allowed,
                            "reason": decision.reason,
                            "retry_after": decision.retry_after,
                            "task_id": decision.task_id,
                        },
                        expires_at=day_start + timedelta(days=1),
                        created_at=created_at,
                    )
                )
                await session.commit()
                return decision
            except BaseException:
                await session.rollback()
                raise


class SqlPrivacyRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @staticmethod
    def _task(row: Task | None) -> PrivacyTask | None:
        if row is None:
            return None
        return PrivacyTask(
            id=row.id,
            owner_user_id=row.owner_user_id,
            type=row.type,
            status=row.status,
            stage=row.stage,
            progress=row.progress,
            trace_id=row.trace_id,
            queued_at=_as_utc(row.queued_at),
        )

    async def find_idempotent(
        self,
        owner_user_id: str,
        route: str,
        key: str,
    ) -> PrivacyTask | None:
        async with self.sessions() as session:
            owner_ids = await authorized_owner_ids(session, owner_user_id)
            record = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.owner_user_id.in_(owner_ids),
                    IdempotencyRecord.route == route,
                    IdempotencyRecord.key == key,
                )
            )
            if record is None or record.response_json is None:
                return None
            return self._task(
                await session.scalar(
                    select(Task).where(
                        Task.id == record.response_json["task_id"],
                        Task.owner_user_id.in_(owner_ids),
                    )
                )
            )

    async def save_task(self, task: PrivacyTask, route: str, key: str) -> None:
        async with self.sessions.begin() as session:
            owner_user_id = await canonical_user_id(
                session,
                task.owner_user_id,
            )
            row = Task(
                id=task.id,
                owner_user_id=owner_user_id,
                type=task.type,
                status=task.status,
                priority=0,
                trace_id=task.trace_id,
                attempts=0,
                max_attempts=3,
                queued_at=task.queued_at,
                stage=task.stage,
                progress=task.progress,
            )
            session.add(row)
            await session.flush()
            self._add_delivery_records(session, row, task.queued_at)
            session.add(
                IdempotencyRecord(
                    id=new_id("idem"),
                    owner_user_id=owner_user_id,
                    route=route,
                    key=key,
                    body_hash="empty",
                    response_status=202,
                    response_json={"task_id": task.id},
                    expires_at=task.queued_at + timedelta(days=1),
                    created_at=task.queued_at,
                )
            )

    @staticmethod
    def _add_delivery_records(
        session: AsyncSession,
        task: Task,
        now: datetime,
    ) -> None:
        session.add(
            TaskEvent(
                id=new_id("tev"),
                owner_user_id=task.owner_user_id,
                task_id=task.id,
                seq=1,
                stage="queued",
                progress=0,
                created_at=now,
            )
        )
        session.add(
            Outbox(
                id=new_id("out"),
                owner_user_id=task.owner_user_id,
                task_id=task.id,
                queue="privacy",
                payload={"task_id": task.id},
                attempts=0,
                available_at=now,
                created_at=now,
            )
        )

    async def find_active_deletion(
        self,
        owner_user_id: str,
    ) -> PrivacyTask | None:
        async with self.sessions() as session:
            owner_ids = await authorized_owner_ids(session, owner_user_id)
            row = await session.scalar(
                select(Task).where(
                    Task.owner_user_id.in_(owner_ids),
                    Task.type == "account_deletion",
                    Task.status.in_(("queued", "running")),
                )
            )
            return self._task(row)

    async def find_export_artifact(
        self,
        owner_user_id: str,
        task_id: str,
    ) -> PrivacyExportArtifact | None:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_user_id)
            task = await session.scalar(
                select(Task).where(
                    Task.id == task_id,
                    Task.owner_user_id.in_(owners),
                    Task.type == "data_export",
                    Task.status == "succeeded",
                )
            )
            if task is None or not task.result_ref:
                return None
            file_row = await session.scalar(
                select(File).where(
                    File.id == task.result_ref,
                    File.owner_user_id == task.owner_user_id,
                    File.status == "confirmed",
                    File.deleted_at.is_(None),
                )
            )
            if file_row is None:
                return None
            return PrivacyExportArtifact(
                file_id=file_row.id,
                object_key=file_row.object_key,
                display_name=file_row.display_name,
            )

    async def data_exports_since(
        self,
        owner_user_id: str,
        since: datetime,
    ) -> tuple[PrivacyTask, ...]:
        async with self.sessions() as session:
            owner_ids = await authorized_owner_ids(session, owner_user_id)
            rows = (
                await session.scalars(
                    select(Task)
                    .where(
                        Task.owner_user_id.in_(owner_ids),
                        Task.type == "data_export",
                        Task.queued_at > since,
                    )
                    .order_by(Task.queued_at, Task.id)
                )
            ).all()
            return tuple(self._task(row) for row in rows)

    async def bind_idempotency(
        self,
        task: PrivacyTask,
        route: str,
        key: str,
    ) -> None:
        async with self.sessions.begin() as session:
            owner_user_id = await canonical_user_id(
                session,
                task.owner_user_id,
            )
            session.add(
                IdempotencyRecord(
                    id=new_id("idem"),
                    owner_user_id=owner_user_id,
                    route=route,
                    key=key,
                    body_hash="empty",
                    response_status=202,
                    response_json={"task_id": task.id},
                    expires_at=task.queued_at + timedelta(days=1),
                    created_at=task.queued_at,
                )
            )

    async def accept_deletion(
        self,
        owner_user_id: str,
        route: str,
        key: str,
        trace_id: str,
        now: datetime,
    ) -> PrivacyTask:
        async with self.sessions() as session:
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            else:
                await session.begin()
            try:
                owner_user_id = await canonical_user_id(
                    session,
                    owner_user_id,
                )
                owner_ids = await authorized_owner_ids(session, owner_user_id)
                await session.scalar(
                    select(User)
                    .where(User.id == owner_user_id)
                    .with_for_update()
                )
                existing = await session.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.owner_user_id.in_(owner_ids),
                        IdempotencyRecord.route == route,
                        IdempotencyRecord.key == key,
                    )
                )
                if existing is not None and existing.response_json is not None:
                    task = await session.get(
                        Task,
                        existing.response_json["task_id"],
                    )
                    await session.commit()
                    return self._task(task)

                task = await session.scalar(
                    select(Task).where(
                        Task.owner_user_id.in_(owner_ids),
                        Task.type == "account_deletion",
                        Task.status.in_(("queued", "running")),
                    )
                )
                if task is None:
                    task = Task(
                        id=new_id("tsk"),
                        owner_user_id=owner_user_id,
                        type="account_deletion",
                        status="queued",
                        priority=0,
                        trace_id=trace_id,
                        attempts=0,
                        max_attempts=3,
                        queued_at=now,
                        stage="queued",
                        progress=0,
                    )
                    session.add(task)
                    await session.flush()
                    self._add_delivery_records(session, task, now)
                session.add(
                    IdempotencyRecord(
                        id=new_id("idem"),
                        owner_user_id=owner_user_id,
                        route=route,
                        key=key,
                        body_hash="empty",
                        response_status=202,
                        response_json={"task_id": task.id},
                        expires_at=now + timedelta(days=1),
                        created_at=now,
                    )
                )
                await session.execute(
                    update(User)
                    .where(User.id == owner_user_id)
                    .values(status="pending_deletion")
                )
                await session.execute(
                    update(Session)
                    .where(
                        Session.owner_user_id.in_(owner_ids),
                        Session.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
                await session.commit()
                return self._task(task)
            except BaseException:
                await session.rollback()
                raise
