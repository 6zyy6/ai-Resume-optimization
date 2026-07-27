from test_resume_versions import (
    _headers,
    _resume,
    _run,
    _snapshot,
    _version_count,
    resume_client,
)
from test_facts import fact_client


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
