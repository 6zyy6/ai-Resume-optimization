def test_trace_id_is_propagated(client):
    response = client.get("/v1/health/ready", headers={"X-Trace-Id": "tr_test"})

    assert response.status_code == 200
    assert response.headers["x-trace-id"] == "tr_test"
