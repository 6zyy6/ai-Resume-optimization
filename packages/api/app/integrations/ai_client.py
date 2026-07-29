from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.workers.execution import HttpServiceError


class AiCancellation(Protocol):
    async def register_run(self, ai_run_id: str) -> bool: ...

    async def is_cancel_requested(self) -> bool: ...

    async def acknowledge_cancel(self, ai_run_id: str) -> None: ...


class AiRunCancelled(Exception):
    pass


class AiClient(Protocol):
    async def run(
        self,
        *,
        workflow_type: str,
        workflow_version: str,
        trace_id: str,
        task_id: str,
        facts: list[dict[str, Any]],
        input_data: dict[str, Any],
        cancellation: AiCancellation | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class FixtureAiClient:
    fixtures: dict[str, dict[str, Any]]

    async def run(self, *, workflow_type: str, **_: Any) -> dict[str, Any]:
        if workflow_type not in self.fixtures:
            raise KeyError(f"No fixture for workflow {workflow_type}")
        return self.fixtures[workflow_type]


class InternalAiClient:
    def __init__(
        self,
        base_url: str,
        service_token: str,
        *,
        timeout_seconds: float = 35,
        poll_interval_seconds: float = 0.1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = min(
            0.5,
            max(0.01, poll_interval_seconds),
        )
        self.transport = transport

    async def run(
        self,
        *,
        workflow_type: str,
        workflow_version: str,
        trace_id: str,
        task_id: str,
        facts: list[dict[str, Any]],
        input_data: dict[str, Any],
        cancellation: AiCancellation | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.service_token}",
            "X-Trace-Id": trace_id,
        }
        jd_requirements = input_data.get("jd_requirements", [])
        current_object = {
            key: value
            for key, value in input_data.items()
            if key not in {"jd_requirements", "locale", "target"}
        }
        payload = {
            "trace_id": trace_id,
            "task_id": task_id,
            "workflow_type": workflow_type,
            "workflow_version": workflow_version,
            "locale": input_data.get("locale", "zh-CN"),
            "target": input_data.get("target", "resume"),
            "confirmed_facts": facts,
            "jd_requirements": [
                {
                    "id": item["id"],
                    "category": item.get("category", item.get("type", "other")),
                    "value": item.get("value", item.get("text", "")),
                }
                for item in jd_requirements
            ],
            "current_object": current_object,
        }
        ai_run_id: str | None = None
        run_settled = False
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=min(2.0, self.timeout_seconds),
                transport=self.transport,
                trust_env=False,
            ) as client:
                try:
                    response = await client.post(
                        "/internal/v1/runs", json=payload, headers=headers
                    )
                    response.raise_for_status()
                    ai_run_id = response.json()["ai_run_id"]
                    if cancellation is not None:
                        should_continue = await cancellation.register_run(
                            ai_run_id
                        )
                        if not should_continue:
                            await _cancel_run(client, ai_run_id, headers)
                            await cancellation.acknowledge_cancel(ai_run_id)
                            run_settled = True
                            raise AiRunCancelled(
                                "AI run cancelled before registration"
                            )
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + self.timeout_seconds
                    while loop.time() < deadline:
                        if (
                            cancellation is not None
                            and await cancellation.is_cancel_requested()
                        ):
                            await _cancel_run(client, ai_run_id, headers)
                            await cancellation.acknowledge_cancel(ai_run_id)
                            run_settled = True
                            raise AiRunCancelled(
                                "AI run cancelled by task owner"
                            )
                        status_response = await client.get(
                            f"/internal/v1/runs/{ai_run_id}",
                            headers=headers,
                        )
                        status_response.raise_for_status()
                        run = status_response.json()["run"]
                        if run["status"] == "succeeded":
                            run_settled = True
                            return {"result": run.get("output"), "run": run}
                        if run["status"] == "failed":
                            run_settled = True
                            _raise_terminal_failure(
                                str(run.get("error_code", "unknown"))
                            )
                        if run["status"] == "cancelled":
                            if cancellation is not None:
                                await cancellation.acknowledge_cancel(ai_run_id)
                            run_settled = True
                            raise AiRunCancelled(
                                "AI_RUN_CANCELLED: "
                                f"{run.get('error_code', 'unknown')}"
                            )
                        await asyncio.sleep(self.poll_interval_seconds)
                    raise TimeoutError("AI internal run timed out")
                finally:
                    if ai_run_id is not None and not run_settled:
                        try:
                            await _cancel_run(client, ai_run_id, headers)
                        except (httpx.HTTPError, TimeoutError):
                            pass
        except httpx.HTTPStatusError as error:
            raise HttpServiceError(error.response.status_code) from error
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise TimeoutError("AI internal transport failed") from error


def _raise_terminal_failure(error_code: str) -> None:
    normalized = error_code.lower()
    if normalized == "provider_429":
        raise HttpServiceError(429)
    if normalized in {"provider_unavailable", "provider_error"}:
        raise HttpServiceError(503)
    if normalized == "provider_timeout":
        raise TimeoutError("AI provider timed out")
    raise RuntimeError(f"AI_RUN_FAILED: {error_code}")


async def _cancel_run(
    client: httpx.AsyncClient,
    ai_run_id: str,
    headers: dict[str, str],
) -> None:
    response = await client.post(
        f"/internal/v1/runs/{ai_run_id}/cancel",
        headers=headers,
    )
    if response.status_code not in {202, 409}:
        response.raise_for_status()
