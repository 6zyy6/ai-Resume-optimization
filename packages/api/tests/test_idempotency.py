from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from test_resume_versions import (
    _headers,
    _resume,
    _run,
    _snapshot,
    _version_count,
    resume_client,
)
from test_facts import _fact_count, fact_client
from app.db.models import IdempotencyRecord, UserAlias
from app.modules.facts.service import FactService
from app.modules.resumes.service import ResumeService


def test_ten_identical_writes_replay_one_resume_version(resume_client):
    """Dropping the idempotency lookup creates more than one version."""
    client, sessions = resume_client
    resume_id = _resume(client, "r-idem")
    responses = [
        client.post(
            f"/v1/resumes/{resume_id}/versions",
            json={"base_version": 0, "snapshot": _snapshot("one")},
            headers=_headers("same-key"),
        )
        for _ in range(10)
    ]

    assert {response.status_code for response in responses} == {201}
    assert len({response.json()["id"] for response in responses}) == 1
    assert _run(_version_count(sessions, resume_id)) == 1


def test_same_key_with_a_different_semantic_body_conflicts(resume_client):
    """Ignoring the body hash would incorrectly replay a different write."""
    client, _ = resume_client
    resume_id = _resume(client, "r-idem-conflict")
    first = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("one")},
        headers=_headers("reused"),
    )
    second = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("two")},
        headers=_headers("reused"),
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_fact_key_reuse_with_a_different_body_returns_api_conflict(fact_client):
    """Leaking the idempotency exception would turn a client conflict into 500."""
    client, _ = fact_client
    first = client.post(
        "/v1/facts",
        json={"kind": "metric", "value": "one"},
        headers=_headers("fact-reused"),
    )
    second = client.post(
        "/v1/facts",
        json={"kind": "metric", "value": "two"},
        headers=_headers("fact-reused"),
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_concurrent_fact_claims_replay_one_complete_response(fact_client):
    """An unlocked read/insert race leaks a unique violation or duplicate facts."""
    client, sessions = fact_client

    def create():
        return client.post(
            "/v1/facts",
            json={"kind": "metric", "value": "one"},
            headers=_headers("concurrent-fact"),
        )

    with ThreadPoolExecutor(max_workers=10) as executor:
        responses = list(executor.map(lambda _: create(), range(10)))

    assert {response.status_code for response in responses} == {201}
    assert len({response.json()["id"] for response in responses}) == 1
    assert len({str(response.json()) for response in responses}) == 1
    assert _run(_fact_count(sessions)) == 1


def test_concurrent_different_fact_payloads_have_one_winner_and_one_conflict(
    fact_client,
):
    """The loser must observe semantic reuse instead of a database exception."""
    client, sessions = fact_client

    def create(value):
        return client.post(
            "/v1/facts",
            json={"kind": "metric", "value": value},
            headers=_headers("concurrent-different-fact"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(create, ("one", "two")))

    assert sorted(response.status_code for response in responses) == [201, 409]
    assert _run(_fact_count(sessions)) == 1


def test_task4_write_replays_are_immutable_after_later_mutations(
    fact_client, resume_client
):
    """Reconstructing replays from current rows changes the original response body."""
    fact_http, _ = fact_client
    resume_http, _ = resume_client

    created_fact = fact_http.post(
        "/v1/facts",
        json={"kind": "metric", "value": "original"},
        headers=_headers("immutable-fact-create"),
    )
    fact_id = created_fact.json()["id"]
    updated_fact = fact_http.patch(
        f"/v1/facts/{fact_id}",
        json={"value": "intermediate"},
        headers=_headers("immutable-fact-update"),
    )
    fact_http.patch(
        f"/v1/facts/{fact_id}",
        json={"value": "final"},
        headers=_headers("immutable-fact-final"),
    )
    replayed_create = fact_http.post(
        "/v1/facts",
        json={"kind": "metric", "value": "original"},
        headers=_headers("immutable-fact-create"),
    )
    replayed_update = fact_http.patch(
        f"/v1/facts/{fact_id}",
        json={"value": "intermediate"},
        headers=_headers("immutable-fact-update"),
    )
    assert replayed_create.json() == created_fact.json()
    assert replayed_update.json() == updated_fact.json()

    sourced = fact_http.post(
        "/v1/facts",
        json={
            "kind": "metric",
            "value": "confirmed evidence",
            "sources": [{"source_type": "user_edit", "content": "evidence"}],
        },
        headers=_headers("immutable-status-create"),
    )
    sourced_id = sourced.json()["id"]
    confirmed = fact_http.post(
        f"/v1/facts/{sourced_id}/confirm",
        headers=_headers("immutable-status-confirm"),
    )
    fact_http.post(
        f"/v1/facts/{sourced_id}/reject",
        headers=_headers("immutable-status-reject"),
    )
    replayed_confirm = fact_http.post(
        f"/v1/facts/{sourced_id}/confirm",
        headers=_headers("immutable-status-confirm"),
    )
    assert replayed_confirm.json() == confirmed.json()

    created_resume = resume_http.post(
        "/v1/resumes",
        json={"kind": "base", "title": "Original"},
        headers=_headers("immutable-resume-create"),
    )
    resume_id = created_resume.json()["id"]
    updated_resume = resume_http.patch(
        f"/v1/resumes/{resume_id}",
        json={"title": "Intermediate"},
        headers=_headers("immutable-resume-update"),
    )
    resume_http.patch(
        f"/v1/resumes/{resume_id}",
        json={"title": "Final"},
        headers=_headers("immutable-resume-final"),
    )
    assert resume_http.post(
        "/v1/resumes",
        json={"kind": "base", "title": "Original"},
        headers=_headers("immutable-resume-create"),
    ).json() == created_resume.json()
    assert resume_http.patch(
        f"/v1/resumes/{resume_id}",
        json={"title": "Intermediate"},
        headers=_headers("immutable-resume-update"),
    ).json() == updated_resume.json()

    version_one = resume_http.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("one")},
        headers=_headers("immutable-version"),
    )
    resume_http.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 1, "snapshot": _snapshot("two")},
        headers=_headers("immutable-version-two"),
    )
    assert resume_http.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 0, "snapshot": _snapshot("one")},
        headers=_headers("immutable-version"),
    ).json() == version_one.json()

    restored = resume_http.post(
        f"/v1/resumes/{resume_id}/versions/{version_one.json()['id']}/restore",
        json={"base_version": 2},
        headers=_headers("immutable-restore"),
    )
    resume_http.post(
        f"/v1/resumes/{resume_id}/versions",
        json={"base_version": 3, "snapshot": _snapshot("four")},
        headers=_headers("immutable-version-four"),
    )
    replayed_restore = resume_http.post(
        f"/v1/resumes/{resume_id}/versions/{version_one.json()['id']}/restore",
        json={"base_version": 2},
        headers=_headers("immutable-restore"),
    )
    assert replayed_restore.json() == restored.json()


def test_all_task4_write_replays_survive_account_merge(
    fact_client, resume_client
):
    """Looking only under the canonical physical owner loses historical keys."""
    _, fact_sessions = fact_client
    fact_service = FactService(fact_sessions)
    original_fact = _run(
        fact_service.create_fact(
            "usr_b",
            kind="metric",
            value="historical",
            sources=[],
            idempotency_key="merged-fact",
        )
    )
    updated_fact = _run(
        fact_service.update_fact(
            "usr_b",
            original_fact.id,
            {"value": "updated"},
            "merged-fact-update",
        )
    )
    status_fact = _run(
        fact_service.create_fact(
            "usr_b",
            kind="metric",
            value="confirmed",
            sources=[{"source_type": "user_edit", "content": "confirmed"}],
            idempotency_key="merged-status-create",
        )
    )
    confirmed_fact = _run(
        fact_service.set_status(
            "usr_b",
            status_fact.id,
            "confirmed",
            "merged-status-confirm",
        )
    )
    _run(_merge_user(fact_sessions))
    assert _run(
        fact_service.create_fact(
            "usr_a",
            kind="metric",
            value="historical",
            sources=[],
            idempotency_key="merged-fact",
        )
    ).response == original_fact.response
    assert _run(
        fact_service.update_fact(
            "usr_a",
            original_fact.id,
            {"value": "updated"},
            "merged-fact-update",
        )
    ).response == updated_fact.response
    assert _run(
        fact_service.set_status(
            "usr_a",
            status_fact.id,
            "confirmed",
            "merged-status-confirm",
        )
    ).response == confirmed_fact.response

    _, resume_sessions = resume_client
    resume_service = ResumeService(resume_sessions)
    create_body = {
        "kind": "base",
        "title": "Historical",
        "base_resume_id": None,
        "job_description_id": None,
    }
    original_resume = _run(
        resume_service.create_resume("usr_b", create_body, "merged-resume")
    )
    updated_resume = _run(
        resume_service.update_resume(
            "usr_b",
            original_resume.id,
            "Updated",
            "merged-resume-update",
        )
    )
    version_one = _run(
        resume_service.save_resume_version(
            "usr_b",
            original_resume.id,
            0,
            _snapshot("one"),
            "merged-version",
        )
    )
    _run(
        resume_service.save_resume_version(
            "usr_b",
            original_resume.id,
            1,
            _snapshot("two"),
            "merged-version-two",
        )
    )
    restored = _run(
        resume_service.restore(
            "usr_b",
            original_resume.id,
            version_one.row.id,
            2,
            "merged-restore",
        )
    )
    _run(_merge_user(resume_sessions))
    assert _run(
        resume_service.create_resume("usr_a", create_body, "merged-resume")
    ).response == original_resume.response
    assert _run(
        resume_service.update_resume(
            "usr_a",
            original_resume.id,
            "Updated",
            "merged-resume-update",
        )
    ).response == updated_resume.response
    assert _run(
        resume_service.save_resume_version(
            "usr_a",
            original_resume.id,
            0,
            _snapshot("one"),
            "merged-version",
        )
    ).response == version_one.response
    assert _run(
        resume_service.restore(
            "usr_a",
            original_resume.id,
            version_one.row.id,
            2,
            "merged-restore",
        )
    ).response == restored.response


def test_twenty_semantic_key_conflicts_are_stable_and_leave_twenty_rows(
    fact_client,
):
    """Conflict admission must remain stable across repeated write classes."""
    client, sessions = fact_client
    for index in range(20):
        key = f"conflict-{index}"
        assert client.post(
            "/v1/facts",
            json={"kind": "metric", "value": f"winner-{index}"},
            headers=_headers(key),
        ).status_code == 201
        conflict = client.post(
            "/v1/facts",
            json={"kind": "metric", "value": f"loser-{index}"},
            headers=_headers(key),
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert _run(_fact_count(sessions)) == 20
    assert _run(_idempotency_count_for_route(sessions, "/v1/facts")) == 20


async def _merge_user(sessions):
    async with sessions.begin() as session:
        session.add(UserAlias(alias_user_id="usr_b", canonical_user_id="usr_a"))


async def _idempotency_count_for_route(sessions, route: str) -> int:
    async with sessions() as session:
        return len(
            (
                await session.scalars(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.route == route
                    )
                )
            ).all()
        )
