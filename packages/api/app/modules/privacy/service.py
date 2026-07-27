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
    def find_idempotent(
        self,
        owner_user_id: str,
        route: str,
        key: str,
    ) -> PrivacyTask | None: ...

    def find_active_deletion(self, owner_user_id: str) -> PrivacyTask | None: ...
    def data_exports_since(
        self,
        owner_user_id: str,
        since: datetime,
    ) -> tuple[PrivacyTask, ...]: ...
    def save_task(self, task: PrivacyTask, route: str, key: str) -> None: ...
    def bind_idempotency(self, task: PrivacyTask, route: str, key: str) -> None: ...


class InMemoryPrivacyRepository:
    def __init__(self) -> None:
        self.tasks: dict[str, PrivacyTask] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}

    def find_idempotent(
        self,
        owner_user_id: str,
        route: str,
        key: str,
    ) -> PrivacyTask | None:
        task_id = self._idempotency.get((owner_user_id, route, key))
        return self.tasks.get(task_id) if task_id else None

    def find_active_deletion(self, owner_user_id: str) -> PrivacyTask | None:
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

    def data_exports_since(
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

    def save_task(self, task: PrivacyTask, route: str, key: str) -> None:
        self.tasks[task.id] = task
        self.bind_idempotency(task, route, key)

    def bind_idempotency(self, task: PrivacyTask, route: str, key: str) -> None:
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

    def _create_task(
        self,
        authenticated: AuthenticatedSession,
        task_type: str,
        route: str,
        idempotency_key: str,
        trace_id: str,
    ) -> PrivacyTask:
        existing = self.repository.find_idempotent(
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
        self.repository.save_task(task, route, idempotency_key)
        return task

    def request_data_export(
        self,
        authenticated: AuthenticatedSession,
        idempotency_key: str,
        trace_id: str,
    ) -> PrivacyTask:
        route = "/v1/me/data-exports"
        existing = self.repository.find_idempotent(
            authenticated.user_id,
            route,
            idempotency_key,
        )
        if existing is not None:
            return existing
        now = self.clock.now()
        recent = self.repository.data_exports_since(
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
        return self._create_task(
            authenticated,
            "data_export",
            route,
            idempotency_key,
            trace_id,
        )

    def request_deletion(
        self,
        authenticated: AuthenticatedSession,
        idempotency_key: str,
        trace_id: str,
    ) -> PrivacyTask:
        route = "/v1/me/deletion-requests"
        existing = self.repository.find_idempotent(
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
        active = self.repository.find_active_deletion(authenticated.user_id)
        if active is not None:
            self.repository.bind_idempotency(active, route, idempotency_key)
            return active
        task = self._create_task(
            authenticated,
            "account_deletion",
            route,
            idempotency_key,
            trace_id,
        )
        self.auth_service.deactivate_user(authenticated.user_id)
        self.auth_service.revoke_all_sessions(authenticated.user_id)
        return task


def build_default_privacy_service(auth_service: AuthService) -> PrivacyService:
    return PrivacyService(
        InMemoryPrivacyRepository(),
        auth_service,
        auth_service.clock,
    )
