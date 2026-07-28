from base64 import urlsafe_b64encode
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BulletFactLink,
    Fact,
    FactRevision,
    FactSource,
    IdempotencyRecord,
    Resume,
    ResumeVersion,
    SourceRecord,
    VersionOperation,
)
from app.modules.facts.service import FactService
from app.modules.resumes.service import ResumeService
from scripts.export_openapi import build_application
from test_facts import _headers as fact_headers, _run, fact_client
from test_resume_versions import (
    _headers,
    _resume,
    _snapshot,
    _version_request,
    resume_client,
)

SEMANTIC_CONFLICT_OPERATIONS = (
    "fact_create",
    "fact_update",
    "fact_status",
    "resume_create",
    "resume_update",
    "version_save",
    "version_restore",
)
TRANSACTION_OPERATIONS = (
    "fact_create",
    "fact_update",
    "fact_confirm",
    "fact_reject",
    "resume_create",
    "resume_update",
    "version_save",
    "version_restore",
)
FAILURE_POINTS = (
    "claim_before",
    "claim_after",
    "complete_before",
    "complete_after",
    "commit_before",
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
            json=_version_request(version, _snapshot(str(version))),
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


@pytest.mark.parametrize("operation", SEMANTIC_CONFLICT_OPERATIONS)
def test_twenty_semantic_conflicts_per_public_write_class(
    operation, fact_client, resume_client
):
    """Every Task 4 public write class binds one key to one semantic body."""
    fact_http, fact_sessions = fact_client
    resume_http, resume_sessions = resume_client
    key_prefix = f"semantic-{operation}-"

    if operation == "fact_create":
        for index in range(20):
            winner = fact_http.post(
                "/v1/facts",
                json={
                    "kind": "metric",
                    "value": f"Increased revenue {index}%",
                    "status": "confirmed",
                    "sources": [
                        {
                            "source_type": "user_confirmation",
                            "content": f"Increased revenue {index}%",
                        }
                    ],
                },
                headers=fact_headers(f"{key_prefix}{index}"),
            )
            loser = fact_http.post(
                "/v1/facts",
                json={"kind": "metric", "value": f"Resolved tickets {index}%"},
                headers=fact_headers(f"{key_prefix}{index}"),
            )
            _assert_semantic_conflict(winner, loser, 201)
        assert _run(_counts(fact_sessions, Fact, SourceRecord, FactSource)) == (
            20,
            20,
            20,
        )
        sessions = fact_sessions
    elif operation == "fact_update":
        fact_id = fact_http.post(
            "/v1/facts",
            json={"kind": "metric", "value": "initial"},
            headers=fact_headers("semantic-update-setup"),
        ).json()["id"]
        for index in range(20):
            winner = fact_http.patch(
                f"/v1/facts/{fact_id}",
                json={"value": f"winner-{index}"},
                headers=fact_headers(f"{key_prefix}{index}"),
            )
            loser = fact_http.patch(
                f"/v1/facts/{fact_id}",
                json={"value": f"loser-{index}"},
                headers=fact_headers(f"{key_prefix}{index}"),
            )
            _assert_semantic_conflict(winner, loser, 200)
        assert _run(_counts(fact_sessions, Fact, FactRevision)) == (1, 20)
        sessions = fact_sessions
    elif operation == "fact_status":
        fact_id = fact_http.post(
            "/v1/facts",
            json={
                "kind": "metric",
                "value": "sourced",
                "sources": [
                    {"source_type": "user_confirmation", "content": "sourced"}
                ],
            },
            headers=fact_headers("semantic-status-setup"),
        ).json()["id"]
        for index in range(20):
            winner = fact_http.post(
                f"/v1/facts/{fact_id}/confirm",
                headers=fact_headers(f"{key_prefix}{index}"),
            )
            loser = fact_http.post(
                f"/v1/facts/{fact_id}/reject",
                headers=fact_headers(f"{key_prefix}{index}"),
            )
            _assert_semantic_conflict(winner, loser, 200)
        assert _run(_counts(fact_sessions, Fact, SourceRecord, FactSource)) == (
            1,
            1,
            1,
        )
        assert _run(_fact_status(fact_sessions, fact_id)) == "confirmed"
        sessions = fact_sessions
    elif operation == "resume_create":
        for index in range(20):
            winner = resume_http.post(
                "/v1/resumes",
                json={"kind": "base", "title": f"Winner {index}"},
                headers=_headers(f"{key_prefix}{index}"),
            )
            loser = resume_http.post(
                "/v1/resumes",
                json={"kind": "base", "title": f"Loser {index}"},
                headers=_headers(f"{key_prefix}{index}"),
            )
            _assert_semantic_conflict(winner, loser, 201)
        assert _run(_counts(resume_sessions, Resume)) == (20,)
        sessions = resume_sessions
    elif operation == "resume_update":
        resume_id = _resume(resume_http, "semantic-resume-update-setup")
        for index in range(20):
            winner = resume_http.patch(
                f"/v1/resumes/{resume_id}",
                json={"title": f"Winner {index}"},
                headers=_headers(f"{key_prefix}{index}"),
            )
            loser = resume_http.patch(
                f"/v1/resumes/{resume_id}",
                json={"title": f"Loser {index}"},
                headers=_headers(f"{key_prefix}{index}"),
            )
            _assert_semantic_conflict(winner, loser, 200)
        assert _run(_counts(resume_sessions, Resume)) == (1,)
        sessions = resume_sessions
    elif operation == "version_save":
        resume_id = _resume(resume_http, "semantic-version-save-setup")
        for index in range(20):
            winner = resume_http.post(
                f"/v1/resumes/{resume_id}/versions",
                json=_version_request(index, _snapshot(f"Winner {index}")),
                headers=_headers(f"{key_prefix}{index}"),
            )
            loser = resume_http.post(
                f"/v1/resumes/{resume_id}/versions",
                json=_version_request(index, _snapshot(f"Loser {index}")),
                headers=_headers(f"{key_prefix}{index}"),
            )
            _assert_semantic_conflict(winner, loser, 201)
        assert _run(_counts(resume_sessions, ResumeVersion, VersionOperation)) == (
            20,
            20,
        )
        sessions = resume_sessions
    else:
        resume_id = _resume(resume_http, "semantic-version-restore-setup")
        source_id = resume_http.post(
            f"/v1/resumes/{resume_id}/versions",
            json=_version_request(0, _snapshot("Source")),
            headers=_headers("semantic-version-restore-source"),
        ).json()["id"]
        for index in range(20):
            winner = resume_http.post(
                f"/v1/resumes/{resume_id}/versions/{source_id}/restore",
                json={"base_version": index + 1},
                headers=_headers(f"{key_prefix}{index}"),
            )
            loser = resume_http.post(
                f"/v1/resumes/{resume_id}/versions/{source_id}/restore",
                json={"base_version": index + 2},
                headers=_headers(f"{key_prefix}{index}"),
            )
            _assert_semantic_conflict(winner, loser, 201)
        assert _run(_counts(resume_sessions, ResumeVersion, VersionOperation)) == (
            21,
            21,
        )
        assert _run(_operation_count(resume_sessions, "restore")) == 20
        sessions = resume_sessions

    claims = _run(_claims_for_prefix(sessions, key_prefix))
    assert len(claims) == 20
    assert {claim.response_status for claim in claims} <= {200, 201}
    if operation == "fact_status":
        assert {claim.route for claim in claims} == {
            f"/v1/facts/{fact_id}/status"
        }


@pytest.mark.parametrize("operation", TRANSACTION_OPERATIONS)
@pytest.mark.parametrize("failure_point", FAILURE_POINTS)
def test_task4_transaction_failure_matrix_has_no_partial_state(
    operation, failure_point, fact_client, resume_client, monkeypatch
):
    """Eight transaction classes stay atomic at five generic failure points."""
    service, sessions, invoke = _prepare_failure_operation(
        operation,
        fact_client,
        resume_client,
    )
    before = _run(_durable_task4_state(sessions))
    _inject_failure(monkeypatch, service, failure_point)

    with pytest.raises(RuntimeError, match=f"injected {failure_point}"):
        _run(invoke())

    assert _run(_durable_task4_state(sessions)) == before


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
        json=_version_request(0, _snapshot("known")),
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
                        **_version_request(1, _snapshot("x")),
                        extra: True,
                    },
                    headers=_headers(f"unknown-version-outer-{index}"),
                ),
                resume_http.post(
                    f"/v1/resumes/{resume_id}/versions",
                    json=_version_request(
                        1,
                        {**_snapshot("x"), extra: True},
                    ),
                    headers=_headers(f"unknown-snapshot-{index}"),
                ),
                resume_http.post(
                    f"/v1/resumes/{resume_id}/versions",
                    json=_version_request(
                        1,
                        {
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
                    ),
                    headers=_headers(f"unknown-section-{index}"),
                ),
                resume_http.post(
                    f"/v1/resumes/{resume_id}/versions",
                    json=_version_request(
                        1,
                        {
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
                    ),
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


def _assert_semantic_conflict(winner, loser, winner_status):
    assert winner.status_code == winner_status
    assert loser.status_code == 409
    assert loser.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def _prepare_failure_operation(operation, fact_client, resume_client):
    _, fact_sessions = fact_client
    _, resume_sessions = resume_client
    if operation.startswith("fact_"):
        service = FactService(fact_sessions)
        sessions = fact_sessions
        if operation == "fact_create":
            return service, sessions, lambda: service.create_fact(
                "usr_a",
                kind="metric",
                value="Increased conversion by 20%",
                status="confirmed",
                sources=[
                    {
                        "source_type": "user_confirmation",
                        "content": "Increased conversion by 20%",
                    }
                ],
                idempotency_key="failure-fact-create",
            )
        fact = _run(
            service.create_fact(
                "usr_a",
                kind="metric",
                value="Increased conversion by 20%",
                sources=(
                    [
                        {
                            "source_type": "user_confirmation",
                            "content": "Increased conversion by 20%",
                        }
                    ]
                    if operation == "fact_confirm"
                    else []
                ),
            )
        )
        if operation == "fact_update":
            return service, sessions, lambda: service.update_fact(
                "usr_a",
                fact.id,
                {"value": "Increased conversion by 25%"},
                "failure-fact-update",
            )
        status = "confirmed" if operation == "fact_confirm" else "rejected"
        return service, sessions, lambda: service.set_status(
            "usr_a",
            fact.id,
            status,
            f"failure-fact-{status}",
        )

    service = ResumeService(resume_sessions)
    sessions = resume_sessions
    if operation == "resume_create":
        return service, sessions, lambda: service.create_resume(
            "usr_a",
            {
                "kind": "base",
                "title": "Atomic create",
                "base_resume_id": None,
                "job_description_id": None,
            },
            "failure-resume-create",
        )
    resume = _run(
        service.create_resume(
            "usr_a",
            {
                "kind": "base",
                "title": "Atomic setup",
                "base_resume_id": None,
                "job_description_id": None,
            },
            f"failure-{operation}-setup",
        )
    )
    if operation == "resume_update":
        return service, sessions, lambda: service.update_resume(
            "usr_a",
            resume.id,
            "Atomic update",
            "failure-resume-update",
        )
    if operation == "version_save":
        fact = _run(
            FactService(resume_sessions).create_fact(
                "usr_a",
                kind="metric",
                value="Increased conversion by 20%",
                status="confirmed",
                sources=[
                    {
                        "source_type": "user_confirmation",
                        "content": "Increased conversion by 20%",
                    }
                ],
            )
        )
        snapshot = {
            **_snapshot("Atomic version"),
            "sections": [
                {
                    "id": "section_atomic",
                    "type": "experience",
                    "title": "Experience",
                    "items": [
                        {
                            "id": "bullet_atomic",
                            "text": "Increased conversion by 20%",
                            "fact_refs": [fact.id],
                        }
                    ],
                }
            ],
        }
        return service, sessions, lambda: service.save_resume_version(
            "usr_a",
            resume.id,
            0,
            snapshot,
            "failure-version-save",
            [
                {
                    "bullet_id": "bullet_atomic",
                    "start": 0,
                    "end": len("Increased conversion by 20%"),
                    "fact_refs": [fact.id],
                }
            ],
        )
    source = _run(
        service.save_resume_version(
            "usr_a",
            resume.id,
            0,
            _snapshot("Restore source"),
            "failure-restore-source",
        )
    )
    return service, sessions, lambda: service.restore(
        "usr_a",
        resume.id,
        source.row.id,
        1,
        "failure-version-restore",
    )


def _inject_failure(monkeypatch, service, failure_point):
    if failure_point.startswith("claim_"):
        original = service.idempotency.claim

        async def fail_claim(*args, **kwargs):
            if failure_point == "claim_before":
                raise RuntimeError(f"injected {failure_point}")
            claim = await original(*args, **kwargs)
            raise RuntimeError(f"injected {failure_point}")

        monkeypatch.setattr(service.idempotency, "claim", fail_claim)
        return
    if failure_point.startswith("complete_"):
        original = service.idempotency.complete

        async def fail_complete(*args, **kwargs):
            if failure_point == "complete_before":
                raise RuntimeError(f"injected {failure_point}")
            await original(*args, **kwargs)
            raise RuntimeError(f"injected {failure_point}")

        monkeypatch.setattr(service.idempotency, "complete", fail_complete)
        return

    async def fail_commit(_session):
        raise RuntimeError(f"injected {failure_point}")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)


async def _claims_for_prefix(sessions, prefix):
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(IdempotencyRecord).order_by(IdempotencyRecord.key)
            )
        ).all()
        return [row for row in rows if row.key.startswith(prefix)]


async def _fact_status(sessions, fact_id):
    async with sessions() as session:
        return await session.scalar(select(Fact.status).where(Fact.id == fact_id))


async def _operation_count(sessions, operation_type):
    async with sessions() as session:
        return await session.scalar(
            select(func.count())
            .select_from(VersionOperation)
            .where(VersionOperation.operation_type == operation_type)
        )


async def _durable_task4_state(sessions):
    async with sessions() as session:
        facts = list(
            (
                await session.execute(
                    select(
                        Fact.id,
                        Fact.owner_user_id,
                        Fact.kind,
                        Fact.value_encrypted,
                        Fact.status,
                        Fact.confirmed_at,
                    ).order_by(Fact.id)
                )
            ).all()
        )
        sources = list(
            (
                await session.execute(
                    select(
                        SourceRecord.id,
                        SourceRecord.owner_user_id,
                        SourceRecord.source_type,
                        SourceRecord.source_ref,
                        SourceRecord.content_encrypted,
                    ).order_by(SourceRecord.id)
                )
            ).all()
        )
        fact_sources = list(
            (
                await session.execute(
                    select(
                        FactSource.fact_id,
                        FactSource.source_record_id,
                        FactSource.owner_user_id,
                        FactSource.source_hash,
                    ).order_by(
                        FactSource.fact_id,
                        FactSource.source_record_id,
                        FactSource.source_hash,
                    )
                )
            ).all()
        )
        fact_revisions = list(
            (
                await session.execute(
                    select(
                        FactRevision.id,
                        FactRevision.fact_id,
                        FactRevision.owner_user_id,
                        FactRevision.previous_value_hash,
                        FactRevision.new_value_encrypted,
                        FactRevision.actor,
                    ).order_by(FactRevision.id)
                )
            ).all()
        )
        resumes = list(
            (
                await session.execute(
                    select(
                        Resume.id,
                        Resume.owner_user_id,
                        Resume.kind,
                        Resume.title,
                        Resume.base_resume_id,
                        Resume.base_resume_owner_user_id,
                        Resume.job_description_id,
                        Resume.job_description_owner_user_id,
                        Resume.head_version,
                        Resume.head_version_id,
                    ).order_by(Resume.id)
                )
            ).all()
        )
        versions = [
            (
                row.id,
                row.owner_user_id,
                row.resume_id,
                row.parent_version_id,
                json.dumps(row.snapshot_json, sort_keys=True),
                row.snapshot_hash,
                row.created_by,
            )
            for row in (
                await session.scalars(
                    select(ResumeVersion).order_by(ResumeVersion.id)
                )
            ).all()
        ]
        operations = [
            (
                row.id,
                row.owner_user_id,
                row.version_id,
                row.operation_type,
                row.actor,
                json.dumps(row.metadata_json, sort_keys=True),
            )
            for row in (
                await session.scalars(
                    select(VersionOperation).order_by(VersionOperation.id)
                )
            ).all()
        ]
        links = [
            (
                row.resume_version_id,
                row.bullet_id,
                row.fact_id,
                row.fact_owner_user_id,
                row.owner_user_id,
                json.dumps(row.claim_range, sort_keys=True),
            )
            for row in (
                await session.scalars(
                    select(BulletFactLink).order_by(
                        BulletFactLink.resume_version_id,
                        BulletFactLink.bullet_id,
                        BulletFactLink.fact_id,
                    )
                )
            ).all()
        ]
        claims = [
            (
                row.id,
                row.owner_user_id,
                row.route,
                row.key,
                row.body_hash,
                row.response_status,
                json.dumps(row.response_json, sort_keys=True)
                if row.response_json is not None
                else None,
            )
            for row in (
                await session.scalars(
                    select(IdempotencyRecord).order_by(IdempotencyRecord.id)
                )
            ).all()
        ]
        return (
            facts,
            sources,
            fact_sources,
            fact_revisions,
            resumes,
            versions,
            operations,
            links,
            claims,
        )


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
