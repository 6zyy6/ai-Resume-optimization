from base64 import urlsafe_b64encode
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import (
    Fact,
    FactSource,
    IdempotencyRecord,
    ResumeVersion,
)
from app.modules.facts.service import FactService
from app.modules.resumes.service import ResumeService
from scripts.export_openapi import build_application
from test_facts import _headers as fact_headers, _run, fact_client
from test_resume_versions import (
    _headers,
    _resume,
    _snapshot,
    resume_client,
)


@pytest.mark.parametrize(
    "cursor",
    [
        "***",
        urlsafe_b64encode(b"not-a-timestamp|resource_1").decode(),
        urlsafe_b64encode(
            b"2026-07-28T00:00:00+00:00|resource_1|unexpected"
        ).decode(),
    ],
)
def test_malformed_fact_cursor_shapes_have_the_stable_validation_envelope(
    cursor, fact_client
):
    """Permissive base64/tuple decoding can turn malformed cursors into valid pages."""
    client, _ = fact_client
    response = client.get("/v1/facts", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["details"] == {}


@pytest.mark.parametrize(
    "cursor",
    [
        "***",
        urlsafe_b64encode(b"not-a-timestamp|resource_1").decode(),
        urlsafe_b64encode(
            b"2026-07-28T00:00:00+00:00|resource_1|unexpected"
        ).decode(),
    ],
)
@pytest.mark.parametrize("resource", ["resumes", "versions"])
def test_malformed_resume_cursor_shapes_have_the_stable_validation_envelope(
    resource, cursor, resume_client
):
    client, _ = resume_client
    resume_id = _resume(client, f"cursor-{resource}-{cursor}")
    route = (
        "/v1/resumes"
        if resource == "resumes"
        else f"/v1/resumes/{resume_id}/versions"
    )
    response = client.get(route, params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["details"] == {}


def test_facts_have_multi_page_and_terminal_page_contract(fact_client):
    client, _ = fact_client
    for index in range(3):
        response = client.post(
            "/v1/facts",
            json={"kind": "metric", "value": str(index)},
            headers=fact_headers(f"fact-page-{index}"),
        )
        assert response.status_code == 201

    first = client.get("/v1/facts", params={"limit": 2})
    terminal = client.get(
        "/v1/facts",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )

    assert len(first.json()["items"]) == 2
    assert first.json()["next_cursor"]
    assert len(terminal.json()["items"]) == 1
    assert terminal.json()["next_cursor"] is None


def test_resumes_and_versions_have_terminal_page_contract(resume_client):
    client, _ = resume_client
    resume_ids = [_resume(client, f"resume-page-{index}") for index in range(3)]
    first_resumes = client.get("/v1/resumes", params={"limit": 2})
    terminal_resumes = client.get(
        "/v1/resumes",
        params={"limit": 2, "cursor": first_resumes.json()["next_cursor"]},
    )
    assert len(first_resumes.json()["items"]) == 2
    assert len(terminal_resumes.json()["items"]) == 1
    assert terminal_resumes.json()["next_cursor"] is None

    resume_id = resume_ids[0]
    for version in range(3):
        response = client.post(
            f"/v1/resumes/{resume_id}/versions",
            json={"base_version": version, "snapshot": _snapshot(str(version))},
            headers=_headers(f"version-page-{version}"),
        )
        assert response.status_code == 201
    first_versions = client.get(
        f"/v1/resumes/{resume_id}/versions", params={"limit": 2}
    )
    terminal_versions = client.get(
        f"/v1/resumes/{resume_id}/versions",
        params={"limit": 2, "cursor": first_versions.json()["next_cursor"]},
    )
    assert len(first_versions.json()["items"]) == 2
    assert len(terminal_versions.json()["items"]) == 1
    assert terminal_versions.json()["next_cursor"] is None


def test_generated_openapi_keeps_all_cursor_contracts():
    generated_path = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "shared"
        / "generated"
        / "openapi.json"
    )
    generated = json.loads(generated_path.read_text())
    runtime = build_application().openapi()
    routes = (
        "/v1/facts",
        "/v1/resumes",
        "/v1/resumes/{resume_id}/versions",
    )
    for route in routes:
        generated_parameters = generated["paths"][route]["get"]["parameters"]
        runtime_parameters = runtime["paths"][route]["get"]["parameters"]
        assert generated_parameters == runtime_parameters
        assert [parameter["name"] for parameter in generated_parameters][-2:] == [
            "cursor",
            "limit",
        ]


def test_five_resource_owner_matrix_stays_hidden(fact_client, resume_client):
    fact_http, fact_sessions = fact_client
    resume_http, resume_sessions = resume_client
    other_fact_ids = [
        _run(
            FactService(fact_sessions).create_fact(
                "usr_b",
                kind="metric",
                value=f"private-{index}",
                sources=[],
            )
        ).id
        for index in range(5)
    ]
    other_resume_ids = []
    other_version_ids = []
    service = ResumeService(resume_sessions)
    for index in range(5):
        resume = _run(
            service.create_resume(
                "usr_b",
                {
                    "kind": "base",
                    "title": f"Private {index}",
                    "base_resume_id": None,
                    "job_description_id": None,
                },
                f"private-resume-{index}",
            )
        )
        version = _run(
            service.save_resume_version(
                "usr_b",
                resume.id,
                0,
                _snapshot(f"private-{index}"),
                f"private-version-{index}",
            )
        )
        other_resume_ids.append(resume.id)
        other_version_ids.append(version.row.id)

    for index in range(5):
        fact_id = other_fact_ids[index]
        resume_id = other_resume_ids[index]
        version_id = other_version_ids[index]
        assert fact_http.get(f"/v1/facts/{fact_id}").status_code == 404
        assert fact_http.patch(
            f"/v1/facts/{fact_id}",
            json={"value": "spoofed"},
            headers=fact_headers(f"owner-fact-{index}"),
        ).status_code == 404
        assert resume_http.get(f"/v1/resumes/{resume_id}").status_code == 404
        assert resume_http.post(
            f"/v1/resumes/{resume_id}/versions/{version_id}/restore",
            json={"base_version": 1},
            headers=_headers(f"owner-version-{index}"),
        ).status_code == 404


def test_failure_injection_rolls_back_resource_and_claim(
    fact_client, resume_client, monkeypatch
):
    fact_http, fact_sessions = fact_client
    fact_service = fact_http.app.state.fact_service

    async def fail_complete(*_):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(fact_service.idempotency, "complete", fail_complete)
    with pytest.raises(RuntimeError, match="injected failure"):
        fact_http.post(
            "/v1/facts",
            json={"kind": "metric", "value": "rolled back"},
            headers=fact_headers("failure-fact"),
        )
    assert _run(_counts(fact_sessions, Fact, IdempotencyRecord)) == (0, 0)

    resume_http, resume_sessions = resume_client
    resume_id = _resume(resume_http, "failure-resume-create")
    resume_service = resume_http.app.state.resume_service
    monkeypatch.setattr(resume_service.idempotency, "complete", fail_complete)
    with pytest.raises(RuntimeError, match="injected failure"):
        resume_http.post(
            f"/v1/resumes/{resume_id}/versions",
            json={"base_version": 0, "snapshot": _snapshot("rolled back")},
            headers=_headers("failure-version"),
        )
    assert _run(_counts(resume_sessions, ResumeVersion)) == (0,)
    assert _run(_route_claim_count(resume_sessions, "failure-version")) == 0


def test_confirmed_source_database_audit_has_no_orphans(fact_client):
    _, sessions = fact_client
    service = FactService(sessions)
    for index in range(50):
        _run(
            service.create_fact(
                "usr_a",
                kind="metric",
                value=f"confirmed-{index}",
                sources=[
                    {
                        "source_type": "user_confirmation",
                        "content": f"confirmed-{index}",
                    }
                ],
                status="confirmed",
                idempotency_key=f"confirmed-audit-{index}",
            )
        )

    assert _run(_confirmed_source_audit(sessions)) == (50, 50, 0)


def test_fifty_unknown_field_shapes_use_validation_envelope(
    fact_client, resume_client
):
    fact_http, _ = fact_client
    resume_http, _ = resume_client
    fact_id = fact_http.post(
        "/v1/facts",
        json={"kind": "metric", "value": "known"},
        headers=fact_headers("unknown-fact"),
    ).json()["id"]
    resume_id = _resume(resume_http, "unknown-resume")
    version_id = resume_http.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("known")},
        headers=_headers("unknown-version"),
    ).json()["id"]

    responses = []
    for index in range(5):
        extra = f"unknown_{index}"
        responses.extend(
            [
                fact_http.post(
                    "/v1/facts",
                    json={"kind": "metric", "value": "x", extra: True},
                    headers=fact_headers(f"unknown-create-{index}"),
                ),
                fact_http.patch(
                    f"/v1/facts/{fact_id}",
                    json={"value": "x", extra: True},
                    headers=fact_headers(f"unknown-patch-{index}"),
                ),
                fact_http.post(
                    "/v1/facts",
                    json={
                        "kind": "metric",
                        "value": "x",
                        "sources": [
                            {
                                "source_type": "user_edit",
                                "content": "x",
                                extra: True,
                            }
                        ],
                    },
                    headers=fact_headers(f"unknown-source-{index}"),
                ),
                resume_http.post(
                    "/v1/resumes",
                    json={"kind": "base", "title": "x", extra: True},
                    headers=_headers(f"unknown-resume-create-{index}"),
                ),
                resume_http.patch(
                    f"/v1/resumes/{resume_id}",
                    json={"title": "x", extra: True},
                    headers=_headers(f"unknown-resume-patch-{index}"),
                ),
                resume_http.post(
                    f"/v1/resumes/{resume_id}/versions",
                    json={
                        "base_version": 1,
                        "snapshot": _snapshot("x"),
                        extra: True,
                    },
                    headers=_headers(f"unknown-version-outer-{index}"),
                ),
                resume_http.post(
                    f"/v1/resumes/{resume_id}/versions",
                    json={
                        "base_version": 1,
                        "snapshot": {**_snapshot("x"), extra: True},
                    },
                    headers=_headers(f"unknown-snapshot-{index}"),
                ),
                resume_http.post(
                    f"/v1/resumes/{resume_id}/versions",
                    json={
                        "base_version": 1,
                        "snapshot": {
                            **_snapshot("x"),
                            "sections": [
                                {
                                    "id": "section",
                                    "type": "experience",
                                    "title": "Experience",
                                    "items": [],
                                    extra: True,
                                }
                            ],
                        },
                    },
                    headers=_headers(f"unknown-section-{index}"),
                ),
                resume_http.post(
                    f"/v1/resumes/{resume_id}/versions",
                    json={
                        "base_version": 1,
                        "snapshot": {
                            **_snapshot("x"),
                            "sections": [
                                {
                                    "id": "section",
                                    "type": "experience",
                                    "title": "Experience",
                                    "items": [
                                        {
                                            "id": "bullet",
                                            "text": "x",
                                            "fact_refs": [],
                                            extra: True,
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                    headers=_headers(f"unknown-bullet-{index}"),
                ),
                resume_http.post(
                    f"/v1/resumes/{resume_id}/versions/{version_id}/restore",
                    json={"base_version": 1, extra: True},
                    headers=_headers(f"unknown-restore-{index}"),
                ),
            ]
        )

    assert len(responses) == 50
    assert {response.status_code for response in responses} == {422}
    assert {
        response.json()["error"]["code"] for response in responses
    } == {"VALIDATION_FAILED"}


def test_one_thousand_edit_history_preserves_parent_chain_and_hashes(
    resume_client,
):
    _, sessions = resume_client
    service = ResumeService(sessions)
    resume = _run(
        service.create_resume(
            "usr_a",
            {
                "kind": "base",
                "title": "Long history",
                "base_resume_id": None,
                "job_description_id": None,
            },
            "history-resume",
        )
    )
    _run(_create_history(service, resume.id))

    assert _run(_history_audit(sessions, resume.id)) == (1000, 1000, True)


async def _counts(sessions, *models):
    async with sessions() as session:
        counts = []
        for model in models:
            counts.append(
                await session.scalar(select(func.count()).select_from(model))
            )
        return tuple(counts)


async def _route_claim_count(sessions, key: str):
    async with sessions() as session:
        return await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(IdempotencyRecord.key == key)
        )


async def _confirmed_source_audit(sessions):
    async with sessions() as session:
        confirmed = await session.scalar(
            select(func.count())
            .select_from(Fact)
            .where(Fact.status == "confirmed")
        )
        links = await session.scalar(select(func.count()).select_from(FactSource))
        orphans = await session.scalar(
            select(func.count())
            .select_from(Fact)
            .outerjoin(
                FactSource,
                (FactSource.fact_id == Fact.id)
                & (FactSource.owner_user_id == Fact.owner_user_id),
            )
            .where(Fact.status == "confirmed", FactSource.fact_id.is_(None))
        )
        return confirmed, links, orphans


async def _history_audit(sessions, resume_id: str):
    async with sessions() as session:
        rows = list(
            (
                await session.scalars(
                    select(ResumeVersion)
                    .where(ResumeVersion.resume_id == resume_id)
                    .order_by(ResumeVersion.created_at, ResumeVersion.id)
                )
            ).all()
        )
        parent_chain = all(
            row.parent_version_id == (rows[index - 1].id if index else None)
            for index, row in enumerate(rows)
        )
        expected_hashes = [
            hashlib.sha256(
                json.dumps(
                    row.snapshot_json,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            for row in rows
        ]
        return len(rows), len(set(expected_hashes)), parent_chain and all(
            row.snapshot_hash == expected_hashes[index]
            for index, row in enumerate(rows)
        )


async def _create_history(service: ResumeService, resume_id: str):
    for index in range(1000):
        await service.save_resume_version(
            "usr_a",
            resume_id,
            index,
            _snapshot(f"edit-{index}"),
            f"history-{index}",
        )
