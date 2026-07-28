import asyncio
import hashlib
import importlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import Fact, Resume
from test_resume_versions import _headers, _resume, _snapshot, _version_request, resume_client


WRITE_CASES = [
    (
        "resume_create",
        "/v1/resumes",
        {"kind": "base", "title": "Cross-site protected"},
        Resume,
    ),
    (
        "fact_create",
        "/v1/facts",
        {"kind": "responsibility", "value": "Protected write"},
        Fact,
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
    ("name", "route", "payload", "model"),
    WRITE_CASES,
    ids=[case[0] for case in WRITE_CASES],
)
@pytest.mark.parametrize("browser_headers", MALICIOUS_BROWSER_HEADERS)
def test_cookie_session_rejects_cross_site_writes_for_public_non_auth_routes(
    resume_client, name, route, payload, model, browser_headers
):
    """Cookie-authenticated writes reject hostile Origin and Fetch Metadata before mutation."""
    client, sessions = resume_client
    response = client.post(
        route,
        json=payload,
        headers=_cookie_write_headers(f"round8-cross-site-{name}", browser_headers),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_ORIGIN_FORBIDDEN"

    async def count_rows() -> int:
        async with sessions() as session:
            return int(await session.scalar(select(func.count()).select_from(model)))

    assert asyncio.run(count_rows()) == 0


@pytest.mark.parametrize(
    ("name", "route", "payload", "_model"),
    WRITE_CASES,
    ids=[case[0] for case in WRITE_CASES],
)
@pytest.mark.parametrize("browser_headers", COMPATIBLE_CLIENT_HEADERS)
def test_cookie_session_keeps_same_origin_and_non_browser_write_compatibility(
    resume_client, name, route, payload, _model, browser_headers
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


_ACCEPTANCE_GROUP_COUNTS = {
    "ENG": 10,
    "FLOW": 14,
    "WEB": 10,
    "MP": 10,
    "AI": 14,
    "FILE": 12,
    "DATA": 10,
    "PERF": 12,
    "SEC": 14,
    "OBS": 12,
    "UX": 8,
    "USER": 10,
    "OPS": 10,
}
ACCEPTANCE_IDS = [
    f"{group}-{index:02d}"
    for group, count in _ACCEPTANCE_GROUP_COUNTS.items()
    for index in range(1, count + 1)
]
EXPECTED_COMMIT_SHA = "a" * 40


def _write_release_manifest(
    tmp_path: Path,
    *,
    acceptance_status: str = "PASS",
) -> tuple[Path, dict]:
    release_dir = tmp_path / "artifacts" / "acceptance" / "task4-local"
    logs_dir = release_dir / "logs"
    logs_dir.mkdir(parents=True)
    raw_log = logs_dir / "focused.log"
    raw_log.write_text("42 passed\n")
    raw_log_sha256 = hashlib.sha256(raw_log.read_bytes()).hexdigest()
    item_status = {
        "PASS": "PASS",
        "FAIL": "FAIL",
        "BLOCKED": "BLOCKED",
    }.get(acceptance_status, "PASS")
    manifest = {
        "release_id": "task4-local",
        "commit_sha": EXPECTED_COMMIT_SHA,
        "created_at": "2026-07-28T00:00:00+00:00",
        "scope": ["release"] if acceptance_status == "PASS" else ["task4"],
        "acceptance_status": acceptance_status,
        "web_image_digest": f"sha256:{'b' * 64}",
        "api_image_digest": f"sha256:{'c' * 64}",
        "pi_image_digest": f"sha256:{'d' * 64}",
        "worker_image_digest": f"sha256:{'e' * 64}",
        "miniprogram_build_version": "mp-20260728.1",
        "database_schema_version": "202607280001",
        "prompt_version": "prompt-v1",
        "workflow_version": "workflow-v1",
        "model_route_version": "route-v1",
        "template_version": "template-v1",
        "test_environment": "unit-test",
        "executor": "executor@example.com",
        "reviewer": "reviewer@example.com",
        "open_severity_counts": {
            "sev1": 0,
            "sev2": 0,
            "sev3": 0,
            "sev4": 0,
        },
        "commands": [
            {
                "command": command,
                "started_at": "2026-07-28T00:00:00+00:00",
                "ended_at": "2026-07-28T00:01:00+00:00",
                "exit_code": 0,
                "raw_log": "logs/focused.log",
                "raw_log_sha256": raw_log_sha256,
            }
            for command in ("pnpm generate", "pnpm lint", "pnpm test", "pnpm build")
        ],
        "acceptance_items": [
            {
                "id": acceptance_id,
                "status": item_status,
                "evidence": [
                    {
                        "path": "logs/focused.log",
                        "sha256": raw_log_sha256,
                    }
                ],
            }
            for acceptance_id in ACCEPTANCE_IDS
        ],
    }
    (release_dir / "manifest.json").write_text(json.dumps(manifest))
    return release_dir, manifest


def _verify_release_evidence(
    release_dir: Path,
    expected_commit_sha: str,
) -> None:
    module = importlib.import_module("scripts.verify_acceptance_evidence")
    module.verify_release_evidence(release_dir, expected_commit_sha)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("manifest", "release_id"),
        ("manifest", "commit_sha"),
        ("manifest", "created_at"),
        ("manifest", "scope"),
        ("manifest", "acceptance_status"),
        ("manifest", "web_image_digest"),
        ("manifest", "api_image_digest"),
        ("manifest", "pi_image_digest"),
        ("manifest", "worker_image_digest"),
        ("manifest", "miniprogram_build_version"),
        ("manifest", "database_schema_version"),
        ("manifest", "prompt_version"),
        ("manifest", "workflow_version"),
        ("manifest", "model_route_version"),
        ("manifest", "template_version"),
        ("manifest", "test_environment"),
        ("manifest", "executor"),
        ("manifest", "reviewer"),
        ("manifest", "open_severity_counts"),
        ("manifest", "commands"),
        ("manifest", "acceptance_items"),
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
        _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA)


@pytest.mark.parametrize("status", ["PASS", "FAIL", "BLOCKED"])
def test_release_evidence_verifier_accepts_complete_hashed_manifest(
    tmp_path, status
):
    release_dir, _ = _write_release_manifest(tmp_path, acceptance_status=status)

    assert _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA) is None


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
        _verify_release_evidence(release_dir, EXPECTED_COMMIT_SHA)
