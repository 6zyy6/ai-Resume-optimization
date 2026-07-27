from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.core.ids import new_id
from app.modules.auth.service import AuthenticatedSession, AuthService, Clock


RECENT_AUTH_WINDOW = timedelta(minutes=10)
EXPORT_RATE_WINDOW = timedelta(hours=1)
EXPORT_RATE_LIMIT = 10


@dataclass(frozen=True)
class PrivacyTask:
    id: str
    owner_user_id: str
    type: str
    status: str
    stage: str
    progress: int
    trace_id: str
    queued_at: datetime


class PrivacyRepository(Protocol):
    async def find_idempotent(
        self,
        owner_user_id: str,
        route: str,
        key: str,
    ) -> PrivacyTask | None: ...

    async def find_active_deletion(self, owner_user_id: str) -> PrivacyTask | None: ...
    async def data_exports_since(
        self,
        owner_user_id: str,
        since: datetime,
    ) -> tuple[PrivacyTask, ...]: ...
    async def save_task(self, task: PrivacyTask, route: str, key: str) -> None: ...
    async def bind_idempotency(self, task: PrivacyTask, route: str, key: str) -> None: ...


class InMemoryPrivacyRepository:
    def __init__(self) -> None:
        self.tasks: dict[str, PrivacyTask] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}

    async def find_idempotent(
        self,
        owner_user_id: str,
        route: str,
        key: str,
    ) -> PrivacyTask | None:
        task_id = self._idempotency.get((owner_user_id, route, key))
        return self.tasks.get(task_id) if task_id else None

    async def find_active_deletion(self, owner_user_id: str) -> PrivacyTask | None:
        return next(
            (
                task
                for task in self.tasks.values()
                if task.owner_user_id == owner_user_id
                and task.type == "account_deletion"
                and task.status in {"queued", "running"}
            ),
            None,
        )

    async def data_exports_since(
        self,
        owner_user_id: str,
        since: datetime,
    ) -> tuple[PrivacyTask, ...]:
        return tuple(
            task
            for task in self.tasks.values()
            if task.owner_user_id == owner_user_id
            and task.type == "data_export"
            and task.queued_at > since
        )

    async def save_task(self, task: PrivacyTask, route: str, key: str) -> None:
        self.tasks[task.id] = task
        await self.bind_idempotency(task, route, key)

    async def bind_idempotency(self, task: PrivacyTask, route: str, key: str) -> None:
        self._idempotency[(task.owner_user_id, route, key)] = task.id


class PrivacyError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after


class PrivacyService:
    def __init__(
        self,
        repository: PrivacyRepository,
        auth_service: AuthService,
        clock: Clock,
    ) -> None:
        self.repository = repository
        self.auth_service = auth_service
        self.clock = clock

    async def _create_task(
        self,
        authenticated: AuthenticatedSession,
        task_type: str,
        route: str,
        idempotency_key: str,
        trace_id: str,
    ) -> PrivacyTask:
        existing = await self.repository.find_idempotent(
            authenticated.user_id,
            route,
            idempotency_key,
        )
        if existing is not None:
            return existing
        task = PrivacyTask(
            id=new_id("tsk"),
            owner_user_id=authenticated.user_id,
            type=task_type,
            status="queued",
            stage="queued",
            progress=0,
            trace_id=trace_id,
            queued_at=self.clock.now(),
        )
        await self.repository.save_task(task, route, idempotency_key)
        return task

    async def request_data_export(
        self,
        authenticated: AuthenticatedSession,
        idempotency_key: str,
        trace_id: str,
    ) -> PrivacyTask:
        route = "/v1/me/data-exports"
        existing = await self.repository.find_idempotent(
            authenticated.user_id,
            route,
            idempotency_key,
        )
        if existing is not None:
            return existing
        now = self.clock.now()
        recent = await self.repository.data_exports_since(
            authenticated.user_id,
            now - EXPORT_RATE_WINDOW,
        )
        if len(recent) >= EXPORT_RATE_LIMIT:
            retry_after = int(
                (recent[0].queued_at + EXPORT_RATE_WINDOW - now).total_seconds()
            )
            raise PrivacyError(
                "EXPORT_RATE_LIMITED",
                "Too many data export requests",
                429,
                max(1, retry_after),
            )
        return await self._create_task(
            authenticated,
            "data_export",
            route,
            idempotency_key,
            trace_id,
        )

    async def request_deletion(
        self,
        authenticated: AuthenticatedSession,
        idempotency_key: str,
        trace_id: str,
    ) -> PrivacyTask:
        route = "/v1/me/deletion-requests"
        existing = await self.repository.find_idempotent(
            authenticated.user_id,
            route,
            idempotency_key,
        )
        if existing is not None:
            return existing
        if self.clock.now() - authenticated.authenticated_at > RECENT_AUTH_WINDOW:
            raise PrivacyError(
                "RECENT_AUTH_REQUIRED",
                "Recent authentication is required",
                403,
            )
        accept_deletion = getattr(self.repository, "accept_deletion", None)
        if accept_deletion is not None:
            return await accept_deletion(
                authenticated.user_id,
                route,
                idempotency_key,
                trace_id,
                self.clock.now(),
            )
        active = await self.repository.find_active_deletion(authenticated.user_id)
        if active is not None:
            await self.repository.bind_idempotency(active, route, idempotency_key)
            return active
        task = await self._create_task(
            authenticated,
            "account_deletion",
            route,
            idempotency_key,
            trace_id,
        )
        await self.auth_service.deactivate_user(authenticated.user_id)
        await self.auth_service.revoke_all_sessions(authenticated.user_id)
        return task

    async def replay_deletion(
        self,
        owner_user_id: str,
        idempotency_key: str,
    ) -> PrivacyTask | None:
        return await self.repository.find_idempotent(
            owner_user_id,
            "/v1/me/deletion-requests",
            idempotency_key,
        )


def build_default_privacy_service(
    auth_service: AuthService,
    repository: PrivacyRepository | None = None,
) -> PrivacyService:
    return PrivacyService(
        repository or InMemoryPrivacyRepository(),
        auth_service,
        auth_service.clock,
    )
