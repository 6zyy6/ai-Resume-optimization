import hashlib
import importlib
import json
from pathlib import Path

import pytest

from test_resume_versions import _headers, _resume, _snapshot, _version_request, resume_client


WRITE_CASES = [
    (
        "resume_create",
        "/v1/resumes",
        {"kind": "base", "title": "Cross-site protected"},
    ),
    (
        "fact_create",
        "/v1/facts",
        {"kind": "responsibility", "value": "Protected write"},
    ),
]
MALICIOUS_BROWSER_HEADERS = [
    pytest.param(
        {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        id="evil-origin-and-cross-site",
    ),
    pytest.param({"Sec-Fetch-Site": "cross-site"}, id="cross-site-without-origin"),
]
COMPATIBLE_CLIENT_HEADERS = [
    pytest.param(
        {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        id="same-origin-browser",
    ),
    pytest.param({}, id="non-browser-without-origin"),
]


def _cookie_write_headers(key: str, extra: dict[str, str]) -> dict[str, str]:
    return {
        **_headers(key),
        "Cookie": "session=authenticated-cookie-session",
        **extra,
    }


@pytest.mark.parametrize(
    ("name", "route", "payload"), WRITE_CASES, ids=[case[0] for case in WRITE_CASES]
)
@pytest.mark.parametrize("browser_headers", MALICIOUS_BROWSER_HEADERS)
def test_cookie_session_rejects_cross_site_writes_for_public_non_auth_routes(
    resume_client, name, route, payload, browser_headers
):
    """Cookie-authenticated writes reject hostile Origin and Fetch Metadata before mutation."""
    client, _ = resume_client
    response = client.post(
        route,
        json=payload,
        headers=_cookie_write_headers(f"round8-cross-site-{name}", browser_headers),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_ORIGIN_FORBIDDEN"


@pytest.mark.parametrize(
    ("name", "route", "payload"), WRITE_CASES, ids=[case[0] for case in WRITE_CASES]
)
@pytest.mark.parametrize("browser_headers", COMPATIBLE_CLIENT_HEADERS)
def test_cookie_session_keeps_same_origin_and_non_browser_write_compatibility(
    resume_client, name, route, payload, browser_headers
):
    """The explicit policy permits same-origin browsers and signed non-browser API clients."""
    client, _ = resume_client
    response = client.post(
        route,
        json=payload,
        headers=_cookie_write_headers(f"round8-compatible-{name}", browser_headers),
    )

    assert response.status_code == 201


def test_version_list_reports_persisted_restore_operation(resume_client):
    """History must expose the stored audit operation instead of synthesizing save."""
    client, _ = resume_client
    resume_id = _resume(client, "round8-restore-resume")
    saved = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=_version_request(0, _snapshot("source version")),
        headers=_headers("round8-save"),
    )
    assert saved.status_code == 201
    restored = client.post(
        f"/v1/resumes/{resume_id}/versions/{saved.json()['id']}/restore",
        json={"base_version": 1},
        headers=_headers("round8-restore"),
    )
    assert restored.status_code == 201

    listed = client.get(f"/v1/resumes/{resume_id}/versions")

    assert listed.status_code == 200
    operations = {item["id"]: item["operation"] for item in listed.json()["items"]}
    assert operations[saved.json()["id"]] == "save"
    assert operations[restored.json()["id"]] == "restore"


def _write_release_manifest(tmp_path: Path) -> tuple[Path, dict]:
    release_dir = tmp_path / "artifacts" / "acceptance" / "task4-local"
    logs_dir = release_dir / "logs"
    logs_dir.mkdir(parents=True)
    raw_log = logs_dir / "focused.log"
    raw_log.write_text("42 passed\n")
    manifest = {
        "release_id": "task4-local",
        "commit_sha": "a" * 40,
        "created_at": "2026-07-28T00:00:00+00:00",
        "scope": ["task4"],
        "acceptance_status": "PASS",
        "commands": [
            {
                "command": ".venv/bin/python -m pytest packages/api/tests -q",
                "started_at": "2026-07-28T00:00:00+00:00",
                "ended_at": "2026-07-28T00:01:00+00:00",
                "exit_code": 0,
                "raw_log": "logs/focused.log",
                "raw_log_sha256": hashlib.sha256(raw_log.read_bytes()).hexdigest(),
            }
        ],
    }
    (release_dir / "manifest.json").write_text(json.dumps(manifest))
    return release_dir, manifest


def _verify_release_evidence(release_dir: Path) -> None:
    module = importlib.import_module("scripts.verify_acceptance_evidence")
    module.verify_release_evidence(release_dir)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("manifest", "release_id"),
        ("manifest", "commit_sha"),
        ("manifest", "created_at"),
        ("manifest", "scope"),
        ("manifest", "acceptance_status"),
        ("manifest", "commands"),
        ("command", "command"),
        ("command", "started_at"),
        ("command", "ended_at"),
        ("command", "exit_code"),
        ("command", "raw_log"),
        ("command", "raw_log_sha256"),
    ],
)
def test_release_evidence_verifier_rejects_missing_required_fields(
    tmp_path, location, field
):
    release_dir, manifest = _write_release_manifest(tmp_path)
    target = manifest if location == "manifest" else manifest["commands"][0]
    del target[field]
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


@pytest.mark.parametrize("status", ["PASS", "FAIL", "BLOCKED"])
def test_release_evidence_verifier_accepts_complete_hashed_manifest(
    tmp_path, status
):
    release_dir, manifest = _write_release_manifest(tmp_path)
    manifest["acceptance_status"] = status
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    assert _verify_release_evidence(release_dir) is None


@pytest.mark.parametrize(
    "corruption",
    ["missing_log", "tampered_log", "wrong_hash", "invalid_acceptance_status"],
)
def test_release_evidence_verifier_rejects_missing_or_tampered_evidence(
    tmp_path, corruption
):
    release_dir, manifest = _write_release_manifest(tmp_path)
    raw_log = release_dir / manifest["commands"][0]["raw_log"]
    if corruption == "missing_log":
        raw_log.unlink()
    elif corruption == "tampered_log":
        raw_log.write_text("42 passed\nmutated\n")
    elif corruption == "wrong_hash":
        manifest["commands"][0]["raw_log_sha256"] = "0" * 64
        (release_dir / "manifest.json").write_text(json.dumps(manifest))
    else:
        manifest["acceptance_status"] = "PENDING"
        (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)
