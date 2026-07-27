def test_api_error_has_stable_envelope(client):
    response = client.get("/v1/testing/error")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "RESUME_VERSION_CONFLICT",
            "message": "简历已在其他设备更新",
            "request_id": response.headers["x-request-id"],
            "details": {},
        }
    }
