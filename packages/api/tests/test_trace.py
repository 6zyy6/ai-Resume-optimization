def test_trace_id_is_propagated(client):
    response = client.get("/v1/health/ready", headers={"X-Trace-Id": "tr_test"})

    assert response.status_code == 200
    assert response.headers["x-trace-id"] == "tr_test"


def test_version_endpoint_reports_service_and_commit(client, monkeypatch):
    monkeypatch.setenv("APP_COMMIT_SHA", "commit-test")
    response = client.get("/v1/version")

    assert response.status_code == 200
    assert response.json() == {"commit_sha": "commit-test", "service": "api"}


def test_public_actor_header_is_not_trusted(client):
    response = client.get("/v1/testing/context", headers={"X-Actor-Id": "user_spoof"})
    assert response.status_code == 200
    assert response.json()["actor_id"] is None
