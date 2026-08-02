import asyncio

import pytest
from sqlalchemy import func, select

from app.db.models import Fact, ResumeVersion, Suggestion, SuggestionFactLink

from app.modules.suggestions.service import (
    SuggestionConflict,
    apply_suggestion_decision,
)


def _suggestion() -> dict:
    return {
        "status": "pending",
        "target_path": "/sections/0/items/0/text",
        "original_text": "负责数据分析",
        "original_hash": "4a671f36c855ec1b8b895d8fa22633abd00e9f9f6fc8de8de1a67f0a3456543e",
        "suggested_text": "使用 Python 完成数据分析",
        "requirement_id": "req_python",
        "reason": "突出与岗位要求相关的工具",
        "fact_refs": ["fact_python"],
        "risk_flags": [],
    }


def test_suggestion_explanation_contains_every_required_field():
    suggestion = _suggestion()
    assert {
        "requirement_id",
        "original_text",
        "suggested_text",
        "reason",
        "fact_refs",
        "risk_flags",
    } <= suggestion.keys()


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ("accept", "accepted"),
        ("edit", "edited"),
        ("ignore", "ignored"),
    ],
)
def test_suggestion_decisions_create_a_new_version(decision, expected):
    result = apply_suggestion_decision(
        suggestion=_suggestion(),
        decision=decision,
        current_text="负责数据分析",
        current_version_id="ver_1",
        edited_text="使用 Python 分析数据" if decision == "edit" else None,
    )
    assert result.status == expected
    assert result.parent_version_id == "ver_1"
    assert result.version_id != "ver_1"


def test_suggestion_base_hash_isolation_and_revert():
    with pytest.raises(SuggestionConflict, match="SUGGESTION_BASE_CONFLICT"):
        apply_suggestion_decision(
            suggestion=_suggestion(),
            decision="accept",
            current_text="已被其他操作改写",
            current_version_id="ver_2",
        )

    accepted = apply_suggestion_decision(
        suggestion=_suggestion(),
        decision="accept",
        current_text="负责数据分析",
        current_version_id="ver_1",
    )
    reverted = apply_suggestion_decision(
        suggestion={**_suggestion(), "status": accepted.status},
        decision="revert",
        current_text=accepted.text,
        current_version_id=accepted.version_id,
    )
    assert reverted.status == "reverted"
    assert reverted.text == "负责数据分析"


def test_blocked_suggestion_rejects_accept_as_unconfirmed_evidence():
    with pytest.raises(SuggestionConflict, match="FACT_NOT_CONFIRMED"):
        apply_suggestion_decision(
            suggestion={**_suggestion(), "status": "blocked"},
            decision="accept",
            current_text="负责数据分析",
            current_version_id="ver_1",
        )


def test_blocked_suggestion_can_be_ignored():
    result = apply_suggestion_decision(
        suggestion={**_suggestion(), "status": "blocked"},
        decision="ignore",
        current_text="负责数据分析",
        current_version_id="ver_1",
    )

    assert result.status == "ignored"


def test_accept_and_revert_endpoints_write_auditable_versions(pipeline_client):
    client, _, _ = pipeline_client
    suggestion_id, base_version_id = _setup_suggestion(client)

    accepted = client.post(
        f"/v1/suggestions/{suggestion_id}/accept",
        headers={"Idempotency-Key": "accept-suggestion"},
    )
    replay = client.post(
        f"/v1/suggestions/{suggestion_id}/accept",
        headers={"Idempotency-Key": "accept-suggestion"},
    )
    reverted = client.post(
        f"/v1/suggestions/{suggestion_id}/revert",
        headers={"Idempotency-Key": "revert-suggestion"},
    )

    assert accepted.status_code == 201
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["version_id"] != base_version_id
    assert replay.status_code == 201
    assert replay.json() == accepted.json()
    assert reverted.status_code == 201
    assert reverted.json()["status"] == "reverted"
    assert reverted.json()["version_id"] != accepted.json()["version_id"]


def test_edit_cannot_introduce_an_unconfirmed_tool(pipeline_client):
    client, _, _ = pipeline_client
    suggestion_id, _ = _setup_suggestion(client)

    response = client.post(
        f"/v1/suggestions/{suggestion_id}/edit",
        json={"text": "使用 Python 和 Kubernetes 开发服务"},
        headers={"Idempotency-Key": "unsupported-edit"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FACT_NOT_CONFIRMED"


def test_blocked_suggestion_endpoints_cannot_accept_or_edit(pipeline_client):
    client, sessions, _ = pipeline_client
    suggestion_id, _ = _setup_suggestion(client)
    asyncio.run(_set_suggestion_status(sessions, suggestion_id, "blocked"))

    accepted = client.post(
        f"/v1/suggestions/{suggestion_id}/accept",
        headers={"Idempotency-Key": "blocked-accept"},
    )
    edited = client.post(
        f"/v1/suggestions/{suggestion_id}/edit",
        json={"text": "Python"},
        headers={"Idempotency-Key": "blocked-edit"},
    )

    assert accepted.status_code == 422
    assert accepted.json()["error"]["code"] == "FACT_NOT_CONFIRMED"
    assert edited.status_code == 422
    assert edited.json()["error"]["code"] == "FACT_NOT_CONFIRMED"


def test_pending_accept_rejects_fact_drift_without_creating_version(
    pipeline_client,
):
    client, sessions, _ = pipeline_client
    suggestion_id, _ = _setup_suggestion(client)
    before = asyncio.run(_version_count(sessions))
    asyncio.run(_reject_linked_facts(sessions, suggestion_id))

    response = client.post(
        f"/v1/suggestions/{suggestion_id}/accept",
        headers={"Idempotency-Key": "fact-drift-accept"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FACT_NOT_CONFIRMED"
    assert asyncio.run(_version_count(sessions)) == before


def test_pending_accept_revalidates_original_hash_before_creating_version(
    pipeline_client,
):
    client, sessions, _ = pipeline_client
    suggestion_id, _ = _setup_suggestion(client)
    before = asyncio.run(_version_count(sessions))
    asyncio.run(_set_suggestion_hash(sessions, suggestion_id, "0" * 64))

    response = client.post(
        f"/v1/suggestions/{suggestion_id}/accept",
        headers={"Idempotency-Key": "hash-drift-accept"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SUGGESTION_BASE_CONFLICT"
    assert asyncio.run(_version_count(sessions)) == before


async def _set_suggestion_status(sessions, suggestion_id: str, status: str) -> None:
    async with sessions.begin() as session:
        suggestion = await session.scalar(
            select(Suggestion).where(
                Suggestion.id == suggestion_id,
                Suggestion.owner_user_id == "usr_a",
            )
        )
        assert suggestion is not None
        suggestion.status = status


async def _set_suggestion_hash(sessions, suggestion_id: str, value: str) -> None:
    async with sessions.begin() as session:
        suggestion = await session.scalar(
            select(Suggestion).where(
                Suggestion.id == suggestion_id,
                Suggestion.owner_user_id == "usr_a",
            )
        )
        assert suggestion is not None
        suggestion.original_hash = value


async def _reject_linked_facts(sessions, suggestion_id: str) -> None:
    async with sessions.begin() as session:
        fact_ids = list(
            (
                await session.scalars(
                    select(SuggestionFactLink.fact_id).where(
                        SuggestionFactLink.suggestion_id == suggestion_id,
                        SuggestionFactLink.owner_user_id == "usr_a",
                    )
                )
            ).all()
        )
        facts = list(
            (
                await session.scalars(
                    select(Fact).where(
                        Fact.id.in_(fact_ids),
                        Fact.owner_user_id == "usr_a",
                    )
                )
            ).all()
        )
        assert facts
        for fact in facts:
            fact.status = "rejected"


async def _version_count(sessions) -> int:
    async with sessions() as session:
        return int(
            await session.scalar(select(func.count()).select_from(ResumeVersion)) or 0
        )


def _setup_suggestion(client):
    fact = client.post(
        "/v1/facts",
        json={
            "kind": "skill",
            "value": "Python",
            "status": "confirmed",
            "sources": [{"source_type": "user_confirmation", "content": "Python"}],
        },
        headers={"Idempotency-Key": "pipeline-fact"},
    )
    resume = client.post(
        "/v1/resumes",
        json={"kind": "base", "title": "张三"},
        headers={"Idempotency-Key": "pipeline-resume"},
    )
    text = "使用 Python 开发服务"
    version = client.post(
        f"/v1/resumes/{resume.json()['id']}/versions",
        json={
            "base_version": 0,
            "snapshot": {
                "schema_version": "1",
                "title": "张三",
                "target": "后端工程师",
                "sections": [
                    {
                        "id": "experience",
                        "type": "experience",
                        "title": "项目经历",
                        "items": [
                            {
                                "id": "bullet_1",
                                "text": text,
                                "fact_refs": [fact.json()["id"]],
                            }
                        ],
                    }
                ],
            },
            "claim_evidence": [
                {
                    "bullet_id": "bullet_1",
                    "start": 0,
                    "end": len(text),
                    "fact_refs": [fact.json()["id"]],
                }
            ],
        },
        headers={"Idempotency-Key": "pipeline-version"},
    )
    job = client.post(
        "/v1/jobs",
        json={"title": "后端实习生", "company": None, "raw": "Python SQL"},
        headers={"Idempotency-Key": "pipeline-job"},
    )
    parsed = client.post(
        f"/v1/jobs/{job.json()['id']}/parse",
        headers={"Idempotency-Key": "pipeline-parse"},
    )
    claim = asyncio.run(
        client.app.state.task_service.claim_task("usr_a", parsed.json()["task_id"])
    )
    assert claim is not None
    asyncio.run(
        client.app.state.job_service.process_parse(
            "usr_a",
            job.json()["id"],
            trace_id="trace_pipeline_parse",
            task_id=parsed.json()["task_id"],
            claim_token=claim.token,
            task_service=client.app.state.task_service,
        )
    )
    parsed_job = client.get(f"/v1/jobs/{job.json()['id']}").json()
    for index, requirement in enumerate(parsed_job["requirements"]):
        confirmed = client.patch(
            f"/v1/jobs/{job.json()['id']}/requirements/{requirement['id']}",
            json={"confirmed": True},
            headers={"Idempotency-Key": f"pipeline-confirm-{index}"},
        )
        assert confirmed.status_code == 200
    match = client.post(
        "/v1/match-analyses",
        json={
            "resume_version_id": version.json()["id"],
            "job_id": job.json()["id"],
        },
        headers={"Idempotency-Key": "pipeline-match"},
    )
    asyncio.run(
        client.app.state.matching_service.process_match(
            "usr_a",
            match.json()["id"],
            trace_id="trace_pipeline_match",
            task_id=match.json()["task_id"],
        )
    )
    suggestions = client.get(
        f"/v1/match-analyses/{match.json()['id']}/suggestions"
    )
    assert fact.status_code == 201
    assert resume.status_code == 201
    assert version.status_code == 201
    assert parsed.status_code == 202
    assert match.status_code == 202
    assert suggestions.status_code == 200
    return suggestions.json()["items"][0]["id"], version.json()["id"]
