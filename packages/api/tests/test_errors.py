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


def test_framework_not_found_uses_stable_envelope(client):
    response = client.get("/v1/missing")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "Resource not found",
            "request_id": response.headers["x-request-id"],
            "details": {},
        }
    }


def test_framework_method_not_allowed_uses_stable_envelope(client):
    response = client.post("/v1/health/ready")
    assert response.status_code == 405
    assert response.json() == {
        "error": {
            "code": "METHOD_NOT_ALLOWED",
            "message": "Method not allowed",
            "request_id": response.headers["x-request-id"],
            "details": {},
        }
    }


def test_request_validation_uses_stable_envelope(client):
    response = client.get("/v1/testing/validate", params={"value": "not-an-integer"})
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_FAILED",
            "message": "Request validation failed",
            "request_id": response.headers["x-request-id"],
            "details": {},
        }
    }
