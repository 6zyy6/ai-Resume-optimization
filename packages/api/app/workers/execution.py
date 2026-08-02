import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.modules.tasks.state import TaskStateError


QUEUE_NAMES = (
    "ai.interactive",
    "ai.batch",
    "file.parse",
    "file.export",
    "privacy",
)


class HttpServiceError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP service returned {status_code}")
        self.status_code = status_code


class TransientStorageError(Exception):
    pass


def should_retry(error: BaseException) -> bool:
    return (
        isinstance(error, TimeoutError)
        or isinstance(error, TransientStorageError)
        or (
            isinstance(error, HttpServiceError)
            and (error.status_code == 429 or error.status_code >= 500)
        )
    )


def retry_delay(attempt: int, jitter: Callable[[], float]) -> float:
    return float(2 ** (attempt - 1)) + max(0.0, jitter())


Operation = Callable[[Any], Awaitable[str] | str]
OperationResolver = Callable[[str], Operation]


@dataclass(frozen=True)
class TerminalFailure:
    error_code: str
    retryable: bool


TerminalFailureHandler = Callable[[Any, TerminalFailure], Awaitable[None] | None]


@dataclass(frozen=True)
class RegisteredOperation:
    operation: Operation
    terminal_failure_handler: TerminalFailureHandler | None = None

    def __call__(self, claim: Any) -> Awaitable[str] | str:
        return self.operation(claim)


@dataclass(frozen=True)
class WorkerRuntime:
    service: Any
    resolver: OperationResolver


_runtime: WorkerRuntime | None = None
_operations: dict[str, RegisteredOperation] = {}
_worker_loop: asyncio.AbstractEventLoop | None = None


def configure_worker(service: Any, resolver: OperationResolver) -> None:
    global _runtime
    _runtime = WorkerRuntime(service, resolver)


def register_operation(
    task_type: str,
    operation: Operation,
    *,
    terminal_failure_handler: TerminalFailureHandler | None = None,
) -> None:
    _operations[task_type] = RegisteredOperation(
        operation,
        terminal_failure_handler,
    )


def resolve_operation(task_type: str) -> RegisteredOperation:
    try:
        return _operations[task_type]
    except KeyError as error:
        raise RuntimeError(f"no operation registered for task type {task_type}") from error


def execute_task(task_id: str, owner_user_id: str) -> dict[str, Any]:
    global _worker_loop
    if _runtime is None:
        raise RuntimeError("task worker runtime is not configured")
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
    return _worker_loop.run_until_complete(
        TaskExecutor(_runtime.service).execute(
            owner_user_id,
            task_id,
            _runtime.resolver,
        )
    )


class TaskExecutor:
    def __init__(
        self,
        service: Any,
        *,
        sleep: Callable[[float], Awaitable[None] | None] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.service = service
        self.sleep = sleep
        self.jitter = jitter

    async def execute(
        self,
        owner_user_id: str,
        task_id: str,
        resolver: OperationResolver,
    ) -> dict[str, Any]:
        while True:
            claim = await self.service.claim_task(owner_user_id, task_id)
            if claim is None:
                task = await self.service.get_task(owner_user_id, task_id)
                if task is not None and task.status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    return _task_result(task)
                raise TaskStateError(
                    "TASK_NOT_CLAIMABLE",
                    "Task has a live claim or exhausted its attempts",
                )
            if claim.terminal_failure_pending:
                operation = resolver(claim.task_type)
                terminal_failure_handler = getattr(
                    operation,
                    "terminal_failure_handler",
                    None,
                )
                if (
                    terminal_failure_handler is None
                    or claim.terminal_failure_error_code is None
                    or claim.terminal_failure_retryable is None
                ):
                    raise RuntimeError(
                        f"terminal failure handler is unavailable for {claim.task_type}"
                    )
                return await self._handle_operation_terminal_failure(
                    owner_user_id,
                    task_id,
                    claim,
                    TerminalFailure(
                        claim.terminal_failure_error_code,
                        claim.terminal_failure_retryable,
                    ),
                    terminal_failure_handler,
                )
            operation: Operation | None = None
            terminal_failure_handler: TerminalFailureHandler | None = None
            try:
                operation = resolver(claim.task_type)
                terminal_failure_handler = getattr(
                    operation,
                    "terminal_failure_handler",
                    None,
                )
                result = operation(claim)
                if inspect.isawaitable(result):
                    result = await result
                current = await self.service.get_task(owner_user_id, task_id)
                if current is not None and current.status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    return await self._terminal_result(
                        owner_user_id,
                        task_id,
                        claim.token,
                        current,
                    )
                task = await self.service.complete_task(
                    owner_user_id,
                    task_id,
                    claim.token,
                    result,
                )
                return _task_result(task)
            except Exception as error:
                retryable = should_retry(error)
                current = await self.service.get_task(owner_user_id, task_id)
                if current is not None and current.status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    if retryable:
                        return _task_result(current)
                    return await self._terminal_result(
                        owner_user_id,
                        task_id,
                        claim.token,
                        current,
                    )
                if retryable and claim.attempts < claim.max_attempts:
                    await self.service.release_claim_for_retry(
                        owner_user_id,
                        task_id,
                        claim.token,
                    )
                    delay = self.sleep(retry_delay(claim.attempts, self.jitter))
                    if inspect.isawaitable(delay):
                        await delay
                    continue
                if terminal_failure_handler is not None:
                    terminal_failure = TerminalFailure(
                        type(error).__name__,
                        retryable,
                    )
                    claim = await self.service.mark_terminal_failure_pending(
                        owner_user_id,
                        task_id,
                        claim.token,
                        terminal_failure.error_code,
                        retryable=terminal_failure.retryable,
                    )
                    return await self._handle_operation_terminal_failure(
                        owner_user_id,
                        task_id,
                        claim,
                        terminal_failure,
                        terminal_failure_handler,
                    )
                task = await self.service.fail_task(
                    owner_user_id,
                    task_id,
                    claim.token,
                    type(error).__name__,
                    release_unused_ai_reservation=not retryable,
                )
                return _task_result(task)

    async def _handle_operation_terminal_failure(
        self,
        owner_user_id: str,
        task_id: str,
        claim: Any,
        terminal_failure: TerminalFailure,
        handler: TerminalFailureHandler,
    ) -> dict[str, Any]:
        handler_error: BaseException | None = None
        for _ in range(2):
            try:
                result = handler(claim, terminal_failure)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                handler_error = error
                continue
            task = await self.service.get_task(owner_user_id, task_id)
            if task is not None and task.status in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                return _task_result(task)
            handler_error = RuntimeError(
                "operation terminal failure handler did not reach a terminal state"
            )
        raise RuntimeError(
            f"terminal failure handler failed for {claim.task_type}"
        ) from handler_error

    async def _terminal_result(
        self,
        owner_user_id: str,
        task_id: str,
        claim_token: str,
        task: Any,
    ) -> dict[str, Any]:
        await self.service.finalize_unused_ai_reservation(
            owner_user_id,
            task_id,
            claim_token,
        )
        current = await self.service.get_task(owner_user_id, task_id)
        return _task_result(current or task)


def _task_result(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "status": task.status,
        "result_ref": task.result_ref,
        "error_code": task.error_code,
    }
