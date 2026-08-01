from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import func, select

from app.db.models import (
    JdRequirement,
    MatchAnalysis,
    MatchItem,
    Suggestion,
    SuggestionFactLink,
)
from app.integrations.ai_client import (
    AiRunCancelled,
    InternalAiClient,
    ParseJdRequest,
    derive_ai_run_id,
)
from app.modules.matching.service import MatchingService
from app.modules.tasks.service import TaskClaimError


pytestmark = pytest.mark.anyio


@dataclass
class CancellationProbe:
    continue_after_registration: bool = True
    cancelled: bool = True
    registered_run_id: str | None = None
    acknowledged_run_id: str | None = None

    async def register_run(self, ai_run_id: str) -> bool:
        self.registered_run_id = ai_run_id
        return self.continue_after_registration

    async def is_cancel_requested(self) -> bool:
        return self.cancelled

    async def acknowledge_cancel(self, ai_run_id: str) -> None:
        self.acknowledged_run_id = ai_run_id


async def test_internal_ai_client_cancels_registered_run_before_polling(monkeypatch):
    requests: list[tuple[str, str]] = []
    client_options: list[dict] = []
    real_async_client = httpx.AsyncClient

    def recording_client(**kwargs):
        client_options.append(kwargs)
        return real_async_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", recording_client)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/internal/v1/runs":
            return httpx.Response(
                202,
                json={
                    "run": {"ai_run_id": ai_run_id, "status": "queued"},
                },
            )
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(202, json={"status": "cancelling"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    probe = CancellationProbe()
    request = ParseJdRequest(
        workflow_type="parse_jd",
        prompt_template_version="jd-parse@2",
        trace_id="trace_cancel",
        task_id="task_cancel",
        owner_scope_hash="owner_hash",
        input_version=1,
        input_hash="cancel_hash",
        payload={
            "jd_text": "cancel me",
            "allowed_categories": ["must_have"],
        },
    )
    ai_run_id = derive_ai_run_id("task_cancel", "parse", "cancel_hash")
    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AiRunCancelled):
        await client.run(request, cancellation=probe)

    assert probe.registered_run_id == ai_run_id
    assert probe.acknowledged_run_id == ai_run_id
    assert client_options[0]["trust_env"] is False
    assert requests == [
        ("POST", "/internal/v1/runs"),
        ("POST", f"/internal/v1/runs/{ai_run_id}/cancel"),
    ]


async def test_cancelled_match_cannot_publish_ai_business_rows(
    pipeline_client,
):
    client, sessions, _ = pipeline_client
    from test_matching import _seed_resume

    version_id = await _seed_resume(sessions)
    job = client.post(
        "/v1/jobs",
        json={"title": "后端实习生", "raw": "Python SQL"},
        headers={"Idempotency-Key": "cancel-job"},
    )
    parse = client.post(
        f"/v1/jobs/{job.json()['id']}/parse",
        headers={"Idempotency-Key": "cancel-job-parse"},
    )
    await client.app.state.job_service.process_parse(
        "usr_a",
        job.json()["id"],
        trace_id="trace_parse",
        task_id=parse.json()["task_id"],
    )
    parsed_job = client.get(f"/v1/jobs/{job.json()['id']}").json()
    for index, requirement in enumerate(parsed_job["requirements"]):
        confirmed = client.patch(
            f"/v1/jobs/{job.json()['id']}/requirements/{requirement['id']}",
            json={"confirmed": True},
            headers={"Idempotency-Key": f"cancel-confirm-{index}"},
        )
        assert confirmed.status_code == 200
    created = client.post(
        "/v1/match-analyses",
        json={
            "resume_version_id": version_id,
            "job_id": job.json()["id"],
        },
        headers={"Idempotency-Key": "cancel-match"},
    )
    task_service = client.app.state.task_service
    claim = await task_service.claim_task(
        "usr_a",
        created.json()["task_id"],
    )
    assert claim is not None

    class CancelThenReturnAi:
        async def run(self, **_):
            await task_service.request_cancel("usr_a", claim.task_id)
            return {
                "result": {
                    "matches": [
                        {
                            "category": "transferable",
                            "fact_refs": [],
                            "requirement_refs": [],
                        }
                    ]
                }
            }

    service = MatchingService(sessions, CancelThenReturnAi())
    with pytest.raises(TaskClaimError):
        await service.process_match(
            "usr_a",
            created.json()["id"],
            trace_id="trace_cancel_match",
            task_id=claim.task_id,
            claim_token=claim.token,
            task_service=task_service,
        )

    async with sessions() as session:
        analysis = await session.scalar(
            select(MatchAnalysis).where(
                MatchAnalysis.id == created.json()["id"]
            )
        )
        counts = [
            int(
                await session.scalar(
                    select(func.count()).select_from(model).where(
                        model.owner_user_id == "usr_a"
                    )
                )
                or 0
            )
            for model in (
                MatchItem,
                Suggestion,
                SuggestionFactLink,
            )
        ]
        requirements = int(
            await session.scalar(
                select(func.count()).select_from(JdRequirement).where(
                    JdRequirement.job_id == job.json()["id"]
                )
            )
            or 0
        )

    assert analysis is not None
    assert analysis.status == "queued"
    assert counts == [0, 0, 0]
    assert requirements > 0
