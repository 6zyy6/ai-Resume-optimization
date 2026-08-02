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
    UsageLedger,
    User,
)
from app.integrations.ai_client import (
    AiProtocolError,
    InternalAiClient,
    ParseJdRequest,
    derive_ai_run_id,
)
from app.modules.matching.service import MatchingService
from app.modules.tasks.service import TaskAdmission, TaskClaimError, TaskService
from app.workers.pipeline import TaskAiCancellation
from app.workers.execution import TaskExecutor
from test_ai_receipts import CANONICAL_HASH, _receipt
from test_match_ai_orchestration import _receipt as _match_receipt


pytestmark = pytest.mark.anyio


@dataclass
class CancellationProbe:
    continue_after_registration: bool = True
    cancelled: bool = True
    registered_run_id: str | None = None
    acknowledged_run_id: str | None = None
    registration_count: int = 0
    acknowledgement_count: int = 0

    async def register_run(self, ai_run_id: str) -> bool:
        self.registration_count += 1
        self.registered_run_id = ai_run_id
        return self.continue_after_registration

    async def is_cancel_requested(self) -> bool:
        return self.cancelled

    async def acknowledge_cancel(self, ai_run_id: str) -> None:
        self.acknowledgement_count += 1
        self.acknowledged_run_id = ai_run_id


async def test_terminal_post_receipt_wins_over_pending_cancellation(monkeypatch):
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
                json={"receipt": _receipt("task_cancel", "parse").model_dump(mode="json")},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    probe = CancellationProbe()
    request = ParseJdRequest(
        workflow_type="parse_jd",
        prompt_template_version="jd-parse@2",
        trace_id="trace_cancel",
        task_id="task_cancel",
        owner_scope_hash="b" * 64,
        input_version=1,
        input_hash=CANONICAL_HASH,
        payload={
            "jd_text": "cancel me",
            "allowed_categories": ("must_have",),
        },
    )
    ai_run_id = derive_ai_run_id("task_cancel", "parse", CANONICAL_HASH)
    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        transport=httpx.MockTransport(handler),
    )

    receipt = await client.run(request, cancellation=probe)

    assert receipt.run.status == "failed"
    assert probe.registration_count == 1
    assert probe.registered_run_id == ai_run_id
    assert probe.acknowledged_run_id is None
    assert client_options[0]["trust_env"] is False
    assert requests == [("POST", "/internal/v1/runs")]


async def test_terminal_post_receipt_binds_and_consumes_before_return(
    sql_session_factory,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_terminal_post"))
    task_service = TaskService(sql_session_factory)
    task = await task_service.create_task(
        "usr_terminal_post",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_receipt",
        idempotency_key="terminal-post-accounting",
        admission=TaskAdmission.ai(),
    )
    claim = await task_service.claim_task("usr_terminal_post", task.id)
    assert claim is not None
    terminal = _receipt(task.id, "parse")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/internal/v1/runs"
        return httpx.Response(
            202,
            json={"receipt": terminal.model_dump(mode="json")},
        )

    cancellation = TaskAiCancellation(task_service, claim)
    request = ParseJdRequest(
        workflow_type="parse_jd",
        prompt_template_version="jd-parse@2",
        trace_id="tr_receipt",
        task_id=task.id,
        owner_scope_hash="b" * 64,
        input_version=1,
        input_hash=CANONICAL_HASH,
        payload={"jd_text": "Python", "allowed_categories": ("must_have",)},
    )
    receipt = await InternalAiClient(
        "http://pi.internal",
        "service-token",
        transport=httpx.MockTransport(handler),
    ).run(request, cancellation=cancellation)

    stored = await task_service.get_task("usr_terminal_post", task.id)
    async with sql_session_factory() as session:
        reservation = await session.scalar(
            select(UsageLedger).where(UsageLedger.task_id == task.id)
        )

    assert receipt == terminal
    assert stored is not None
    assert stored.active_ai_run_id == terminal.run.ai_run_id
    assert reservation is not None
    assert reservation.state == "consumed"
    assert reservation.ai_run_id == terminal.run.ai_run_id

    settled = await task_service.settle_ai_run(
        "usr_terminal_post",
        task.id,
        terminal.run.ai_run_id,
    )
    assert settled.active_ai_run_id is None


async def test_successful_malformed_post_binds_before_parsing_and_is_not_released(
    sql_session_factory,
):
    async with sql_session_factory.begin() as session:
        session.add(User(id="usr_malformed_post"))
    task_service = TaskService(sql_session_factory)
    task = await task_service.create_task(
        "usr_malformed_post",
        task_type="parse_job",
        queue="ai.interactive",
        trace_id="tr_malformed_post",
        idempotency_key="malformed-post-accounting",
        admission=TaskAdmission.ai(),
    )
    expected_run_id = derive_ai_run_id(task.id, "parse", CANONICAL_HASH)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                202,
                content=b'{"receipt":',
                headers={"content-type": "application/json"},
            )
        if request.method == "DELETE":
            return httpx.Response(202, json={"accepted": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        transport=httpx.MockTransport(handler),
    )

    async def malformed_after_acceptance(claim) -> str:
        request = ParseJdRequest(
            workflow_type="parse_jd",
            prompt_template_version="jd-parse@2",
            trace_id="tr_malformed_post",
            task_id=task.id,
            owner_scope_hash="b" * 64,
            input_version=1,
            input_hash=CANONICAL_HASH,
            payload={"jd_text": "Python", "allowed_categories": ("must_have",)},
        )
        await client.run(
            request,
            cancellation=TaskAiCancellation(task_service, claim),
        )
        return "unreachable"

    result = await TaskExecutor(task_service).execute(
        "usr_malformed_post",
        task.id,
        lambda _: malformed_after_acceptance,
    )
    stored = await task_service.get_task("usr_malformed_post", task.id)
    async with sql_session_factory() as session:
        reservation = await session.scalar(
            select(UsageLedger).where(UsageLedger.task_id == task.id)
        )

    assert result["status"] == "failed"
    assert result["error_code"] == AiProtocolError.__name__
    assert stored is not None
    assert stored.active_ai_run_id == expected_run_id
    assert reservation is not None
    assert reservation.state == "consumed"
    assert reservation.ai_run_id == expected_run_id


async def test_internal_ai_client_cancels_once_then_polls_terminal_receipt():
    requests: list[tuple[str, str]] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/internal/v1/runs":
            return httpx.Response(
                202,
                json={"run": {"ai_run_id": ai_run_id, "status": "queued"}},
            )
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(202, json={"status": "cancelling"})
        if request.method == "GET":
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(
                    200,
                    json={"run": {"ai_run_id": ai_run_id, "status": "running"}},
                )
            return httpx.Response(
                200,
                json={
                    "receipt": _receipt(
                        "task_cancel_poll",
                        "parse",
                        status="cancelled",
                        error_code=None,
                    ).model_dump(mode="json")
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    probe = CancellationProbe(continue_after_registration=False)
    request = ParseJdRequest(
        workflow_type="parse_jd",
        prompt_template_version="jd-parse@2",
        trace_id="trace_cancel",
        task_id="task_cancel_poll",
        owner_scope_hash="b" * 64,
        input_version=1,
        input_hash=CANONICAL_HASH,
        payload={"jd_text": "cancel me", "allowed_categories": ("must_have",)},
    )
    ai_run_id = derive_ai_run_id("task_cancel_poll", "parse", CANONICAL_HASH)
    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        transport=httpx.MockTransport(handler),
    )

    receipt = await client.run(request, cancellation=probe)

    assert receipt.run.status == "cancelled"
    assert probe.registration_count == 1
    assert probe.acknowledgement_count == 1
    assert requests.count(("POST", f"/internal/v1/runs/{ai_run_id}/cancel")) == 1
    assert requests.count(("GET", f"/internal/v1/runs/{ai_run_id}")) == 2


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
    claim = await client.app.state.task_service.claim_task(
        "usr_a", parse.json()["task_id"]
    )
    assert claim is not None
    await client.app.state.job_service.process_parse(
        "usr_a",
        job.json()["id"],
        trace_id="trace_parse",
        task_id=parse.json()["task_id"],
        claim_token=claim.token,
        task_service=client.app.state.task_service,
    )
    parsed_job = client.get(f"/v1/jobs/{job.json()['id']}").json()
    for index, requirement in enumerate(parsed_job["requirements"]):
        confirmed = client.patch(
            f"/v1/jobs/{job.json()['id']}/requirements/{requirement['id']}",
            json={"confirmed": True},
            headers={"Idempotency-Key": f"cancel-confirm-{index}"},
        )
        assert confirmed.status_code == 200
    task_service = client.app.state.task_service

    class CancelThenReturnAi:
        async def run(self, request, cancellation=None):
            assert cancellation is not None
            ai_run_id = derive_ai_run_id(
                request.task_id, "match", request.input_hash
            )
            assert await cancellation.register_run(ai_run_id) is True
            await task_service.request_cancel("usr_a", request.task_id)
            await cancellation.acknowledge_cancel(ai_run_id)
            return _match_receipt(request, status="cancelled")

    service = MatchingService(sessions, CancelThenReturnAi())
    client.app.state.matching_service = service
    created = client.post(
        "/v1/match-analyses",
        json={
            "resume_version_id": version_id,
            "job_id": job.json()["id"],
        },
        headers={"Idempotency-Key": "cancel-match"},
    )
    claim = await task_service.claim_task(
        "usr_a",
        created.json()["task_id"],
    )
    assert claim is not None

    await service.process_match(
        "usr_a",
        created.json()["id"],
        trace_id="trace_cancel_match",
        task_id=claim.task_id,
        claim_token=claim.token,
        task_service=task_service,
        cancellation=TaskAiCancellation(task_service, claim),
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
    assert analysis.status == "failed"
    assert counts == [0, 0, 0]
    assert requirements > 0
