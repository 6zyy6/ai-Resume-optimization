import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.core.middleware import CsrfProtectionMiddleware, RequestContextMiddleware
from test_task4_round8_release_gates import (
    ACCEPTANCE_IDS,
    _verify_release_evidence,
    _write_release_manifest,
)


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "unknown"])
def test_release_manifest_requires_each_known_acceptance_id_exactly_once(
    tmp_path, corruption
):
    release_dir, manifest = _write_release_manifest(tmp_path)
    if corruption == "missing":
        manifest["acceptance_items"].pop()
    elif corruption == "duplicate":
        manifest["acceptance_items"][-1]["id"] = ACCEPTANCE_IDS[0]
    else:
        manifest["acceptance_items"][-1]["id"] = "UNKNOWN-01"
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("web_image_digest", ""),
        ("api_image_digest", ""),
        ("pi_image_digest", ""),
        ("worker_image_digest", ""),
        ("miniprogram_build_version", ""),
        ("database_schema_version", ""),
        ("prompt_version", ""),
        ("workflow_version", ""),
        ("model_route_version", ""),
        ("template_version", ""),
        ("test_environment", ""),
        ("executor", ""),
        ("reviewer", ""),
    ],
)
def test_release_manifest_rejects_empty_artifact_and_audit_fields(
    tmp_path, field, value
):
    release_dir, manifest = _write_release_manifest(tmp_path)
    manifest[field] = value
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


@pytest.mark.parametrize(
    "corruption",
    ["invalid_status", "missing_evidence", "unsafe_evidence", "wrong_evidence_hash"],
)
def test_release_manifest_validates_each_acceptance_item_evidence(
    tmp_path, corruption
):
    release_dir, manifest = _write_release_manifest(tmp_path)
    item = manifest["acceptance_items"][0]
    if corruption == "invalid_status":
        item["status"] = "PENDING"
    elif corruption == "missing_evidence":
        item["evidence"] = []
    elif corruption == "unsafe_evidence":
        item["evidence"][0]["path"] = "../../../outside.log"
    else:
        item["evidence"][0]["sha256"] = "0" * 64
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


@pytest.mark.parametrize(
    "corruption",
    ["nonzero_command", "missing_generate", "missing_lint", "missing_test", "missing_build"],
)
def test_release_pass_requires_successful_root_gate_commands(tmp_path, corruption):
    release_dir, manifest = _write_release_manifest(tmp_path)
    if corruption == "nonzero_command":
        manifest["commands"][0]["exit_code"] = 1
    else:
        missing = {
            "missing_generate": "pnpm generate",
            "missing_lint": "pnpm lint",
            "missing_test": "pnpm test",
            "missing_build": "pnpm build",
        }[corruption]
        manifest["commands"] = [
            command for command in manifest["commands"] if command["command"] != missing
        ]
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


@pytest.mark.parametrize("corruption", ["blocked_item", "open_sev1", "open_sev2"])
def test_release_pass_requires_every_item_pass_and_no_open_high_severity(
    tmp_path, corruption
):
    release_dir, manifest = _write_release_manifest(tmp_path)
    if corruption == "blocked_item":
        manifest["acceptance_items"][0]["status"] = "BLOCKED"
    else:
        manifest["open_severity_counts"][corruption.removeprefix("open_")] = 1
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


def test_blocked_local_scope_cannot_claim_global_release_pass(tmp_path):
    release_dir, manifest = _write_release_manifest(tmp_path)
    manifest["scope"] = ["task4"]
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)

    manifest["acceptance_status"] = "BLOCKED"
    for item in manifest["acceptance_items"]:
        item["status"] = "BLOCKED"
    manifest["commands"][0]["exit_code"] = 1
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    assert _verify_release_evidence(release_dir) is None


def test_raw_evidence_swap_between_check_and_read_is_rejected(tmp_path, monkeypatch):
    release_dir, manifest = _write_release_manifest(
        tmp_path,
        acceptance_status="BLOCKED",
    )
    manifest["commands"] = manifest["commands"][:1]
    raw_log = release_dir / manifest["commands"][0]["raw_log"]
    outside = tmp_path / "outside.log"
    outside.write_text("outside evidence\n")
    manifest["commands"][0]["raw_log_sha256"] = hashlib.sha256(
        outside.read_bytes()
    ).hexdigest()
    (release_dir / "manifest.json").write_text(json.dumps(manifest))
    original_read_bytes = Path.read_bytes

    def swap_then_read(path):
        if path == raw_log:
            path.unlink()
            path.symlink_to(outside)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", swap_then_read)

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


def test_manifest_swap_between_check_and_read_is_rejected(tmp_path, monkeypatch):
    release_dir, manifest = _write_release_manifest(
        tmp_path,
        acceptance_status="BLOCKED",
    )
    outside = tmp_path / "outside-manifest.json"
    outside.write_text(json.dumps(manifest))
    manifest_path = release_dir / "manifest.json"
    manifest_path.write_text("{invalid")
    original_read_text = Path.read_text

    def swap_then_read(path, *args, **kwargs):
        if path == manifest_path:
            path.unlink()
            path.symlink_to(outside)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", swap_then_read)

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


HOSTILE_HEADERS = [
    pytest.param(
        {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        id="evil-origin",
    ),
    pytest.param({"Sec-Fetch-Site": "cross-site"}, id="fetch-metadata"),
    pytest.param({"Origin": "null"}, id="null-origin"),
    pytest.param({"Origin": "https://testserver:not-a-port"}, id="invalid-port"),
    pytest.param(
        {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
        id="conflicting-fetch-metadata",
    ),
]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("hostile_headers", HOSTILE_HEADERS)
def test_cookie_authenticated_mutation_matrix_is_blocked_without_write(
    method, hostile_headers
):
    mutations = {"count": 0}
    application = FastAPI()
    application.add_middleware(CsrfProtectionMiddleware)
    application.add_middleware(RequestContextMiddleware)

    @application.api_route("/mutation", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def mutate():
        mutations["count"] += 1
        return JSONResponse({"mutated": True})

    with TestClient(application) as client:
        client.cookies.set("session", "authenticated-cookie-session")
        response = client.request(method, "/mutation", headers=hostile_headers)

    assert response.status_code == 403
    assert response.json()["error"] == {
        "code": "CSRF_ORIGIN_FORBIDDEN",
        "message": "Cross-site cookie-authenticated write is forbidden",
        "request_id": response.headers["X-Request-Id"],
        "details": {},
    }
    assert response.headers["X-Trace-Id"]
    assert mutations["count"] == 0


def test_trusted_proxy_can_supply_original_request_protocol():
    application = FastAPI()
    application.add_middleware(
        CsrfProtectionMiddleware,
        trusted_proxy_ips=("testclient",),
    )
    application.add_middleware(RequestContextMiddleware)

    @application.post("/mutation")
    async def mutate():
        return JSONResponse({"mutated": True})

    with TestClient(application) as client:
        client.cookies.set("session", "authenticated-cookie-session")
        response = client.post(
            "/mutation",
            headers={
                "Origin": "https://testserver",
                "Sec-Fetch-Site": "same-origin",
                "X-Forwarded-Proto": "https",
            },
        )

    assert response.status_code == 200
