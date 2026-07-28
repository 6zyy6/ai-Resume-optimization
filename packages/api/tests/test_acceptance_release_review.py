import hashlib
import importlib
import json
import os
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.core.middleware import CsrfProtectionMiddleware, RequestContextMiddleware
from test_task4_round8_release_gates import (
    ACCEPTANCE_IDS,
    EXPECTED_COMMIT_SHA,
    _verify_release_evidence,
    _write_release_manifest,
)


def test_release_manifest_commit_must_match_candidate_commit(tmp_path):
    release_dir, _ = _write_release_manifest(tmp_path)
    verifier = importlib.import_module("scripts.verify_acceptance_evidence")

    try:
        verifier.verify_release_evidence(release_dir, "b" * 40)
    except TypeError:
        pytest.fail("verifier does not accept the candidate commit SHA")
    except ValueError as error:
        assert "candidate commit" in str(error)
    else:
        pytest.fail("format-valid evidence from a different commit was accepted")


def test_production_acceptance_ids_match_the_acceptance_spec():
    verifier = importlib.import_module("scripts.verify_acceptance_evidence")
    spec_path = (
        Path(__file__).resolve().parents[3]
        / "docs/superpowers/specs/ai-resume-assistant/11-acceptance-and-evidence.md"
    )
    spec_ids = re.findall(
        r"^\| ([A-Z]+-\d{2}) \|",
        spec_path.read_text(),
        flags=re.MULTILINE,
    )

    assert len(spec_ids) == 146
    assert len(set(spec_ids)) == 146
    assert getattr(verifier, "ACCEPTANCE_IDS", None) == frozenset(spec_ids)


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
        _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA)


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
        _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA)


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
        _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA)


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
        _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA)


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
        _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA)


def test_blocked_local_scope_cannot_claim_global_release_pass(tmp_path):
    release_dir, manifest = _write_release_manifest(tmp_path)
    manifest["scope"] = ["task4"]
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA)

    manifest["acceptance_status"] = "BLOCKED"
    for item in manifest["acceptance_items"]:
        item["status"] = "BLOCKED"
    manifest["commands"][0]["exit_code"] = 1
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    assert _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA) is None


def _swap_path_after_fd_open(
    monkeypatch,
    verifier,
    *,
    opened_name: str,
    original_path: Path,
    moved_path: Path,
    outside_path: Path,
) -> dict[str, object]:
    original_open = verifier.os.open
    original_read = verifier.os.read
    state: dict[str, object] = {"target_fd": None, "swapped": False}

    def open_with_target_tracking(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == opened_name and dir_fd is not None:
            state["target_fd"] = fd
        return fd

    def read_after_path_swap(fd, length):
        if fd == state["target_fd"] and not state["swapped"]:
            os.replace(original_path, moved_path)
            original_path.symlink_to(outside_path)
            state["swapped"] = True
        return original_read(fd, length)

    monkeypatch.setattr(verifier.os, "open", open_with_target_tracking)
    monkeypatch.setattr(verifier.os, "read", read_after_path_swap)
    monkeypatch.setattr(
        verifier.os,
        "supports_dir_fd",
        {*verifier.os.supports_dir_fd, open_with_target_tracking},
    )
    return state


@pytest.mark.parametrize("target_kind", ["raw_log", "item_evidence"])
def test_evidence_path_swap_after_open_reads_and_hashes_the_open_fd(
    tmp_path,
    monkeypatch,
    target_kind,
):
    release_dir, manifest = _write_release_manifest(
        tmp_path,
        acceptance_status="BLOCKED",
    )
    manifest["commands"] = manifest["commands"][:1]
    if target_kind == "raw_log":
        original_path = release_dir / manifest["commands"][0]["raw_log"]
        expected_hash = manifest["commands"][0]["raw_log_sha256"]
    else:
        original_path = release_dir / "evidence" / "item.log"
        original_path.parent.mkdir()
        original_path.write_text("acceptance evidence\n")
        expected_hash = hashlib.sha256(original_path.read_bytes()).hexdigest()
        manifest["acceptance_items"][0]["evidence"] = [
            {
                "path": "evidence/item.log",
                "sha256": expected_hash,
            }
        ]
    (release_dir / "manifest.json").write_text(json.dumps(manifest))
    outside_path = tmp_path / f"outside-{target_kind}.log"
    outside_path.write_text("outside replacement\n")
    moved_path = tmp_path / f"opened-{target_kind}.log"
    verifier = importlib.import_module("scripts.verify_acceptance_evidence")
    state = _swap_path_after_fd_open(
        monkeypatch,
        verifier,
        opened_name=original_path.name,
        original_path=original_path,
        moved_path=moved_path,
        outside_path=outside_path,
    )

    assert _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA) is None
    assert state["swapped"] is True
    assert original_path.is_symlink()
    assert hashlib.sha256(moved_path.read_bytes()).hexdigest() == expected_hash
    assert hashlib.sha256(outside_path.read_bytes()).hexdigest() != expected_hash


def test_manifest_path_swap_after_open_reads_the_open_fd(tmp_path, monkeypatch):
    release_dir, manifest = _write_release_manifest(
        tmp_path,
        acceptance_status="BLOCKED",
    )
    outside = tmp_path / "outside-manifest.json"
    outside_manifest = {**manifest, "commit_sha": "b" * 40}
    outside.write_text(json.dumps(outside_manifest))
    manifest_path = release_dir / "manifest.json"
    moved_manifest = tmp_path / "opened-manifest.json"
    verifier = importlib.import_module("scripts.verify_acceptance_evidence")
    state = _swap_path_after_fd_open(
        monkeypatch,
        verifier,
        opened_name="manifest.json",
        original_path=manifest_path,
        moved_path=moved_manifest,
        outside_path=outside,
    )

    assert _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA) is None
    assert state["swapped"] is True
    assert manifest_path.is_symlink()
    assert json.loads(moved_manifest.read_text())["commit_sha"] == EXPECTED_COMMIT_SHA
    assert json.loads(manifest_path.read_text())["commit_sha"] == "b" * 40


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
