import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from app.modules.tasks.state import TaskStateError


QUEUE_NAMES = (
    "ai.interactive",
    "ai.batch",
    "file.parse",
    "file.export",
    "privacy",
)
_task_handler: Callable[[str], Any] | None = None


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


def configure_task_handler(handler: Callable[[str], Any]) -> None:
    global _task_handler
    _task_handler = handler


def execute_task(task_id: str) -> Any:
    if _task_handler is None:
        raise RuntimeError("task execution handler is not configured")
    result = _task_handler(task_id)
    return asyncio.run(result) if inspect.isawaitable(result) else result


class TaskExecutor:
    def __init__(
        self,
        service: Any,
        *,
        sleep: Callable[[float], Awaitable[None] | None],
        jitter: Callable[[], float],
    ) -> None:
        self.service = service
        self.sleep = sleep
        self.jitter = jitter

    async def execute(
        self,
        task_id: str,
        operation: Callable[[], Awaitable[str] | str],
    ) -> Any:
        while True:
            task = await self.service.claim_task(task_id)
            if task is None:
                raise TaskStateError(
                    "TASK_NOT_CLAIMABLE",
                    "Task is terminal or has exhausted its attempts",
                )
            try:
                result = operation()
                if inspect.isawaitable(result):
                    result = await result
                return await self.service.complete_task(task_id, result)
            except Exception as error:
                if should_retry(error) and task.attempts < task.max_attempts:
                    delay = self.sleep(retry_delay(task.attempts, self.jitter))
                    if inspect.isawaitable(delay):
                        await delay
                    continue
                return await self.service.fail_task(
                    task_id,
                    type(error).__name__,
                )
