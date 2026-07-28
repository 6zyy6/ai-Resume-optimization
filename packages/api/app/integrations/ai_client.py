from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


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
        self.poll_interval_seconds = poll_interval_seconds
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
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = await client.post(
                "/internal/v1/runs", json=payload, headers=headers
            )
            response.raise_for_status()
            ai_run_id = response.json()["ai_run_id"]
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout_seconds
            while loop.time() < deadline:
                status_response = await client.get(
                    f"/internal/v1/runs/{ai_run_id}",
                    headers=headers,
                )
                status_response.raise_for_status()
                run = status_response.json()["run"]
                if run["status"] == "succeeded":
                    return {"result": run.get("output"), "run": run}
                if run["status"] in {"failed", "cancelled"}:
                    raise RuntimeError(
                        f"AI_RUN_{run['status'].upper()}: "
                        f"{run.get('error_code', 'unknown')}"
                    )
                await asyncio.sleep(self.poll_interval_seconds)
            try:
                await client.post(
                    f"/internal/v1/runs/{ai_run_id}/cancel",
                    headers=headers,
                )
            finally:
                raise TimeoutError("AI internal run timed out")
