import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import VersionOperation
from test_resume_versions import (
    _headers,
    _resume,
    _snapshot,
    _version_request,
    resume_client,
)
from test_task4_round8_release_gates import (
    _cookie_write_headers,
    _verify_release_evidence,
    _write_release_manifest,
)


@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("manifest", "release_id", "other-release"),
        ("manifest", "commit_sha", "a" * 39),
        ("manifest", "created_at", "2026-07-28T00:00:00"),
        ("manifest", "scope", []),
        ("manifest", "acceptance_status", []),
        ("manifest", "commands", []),
        ("command", "command", ""),
        ("command", "exit_code", True),
        ("command", "raw_log_sha256", "A" * 64),
    ],
)
def test_release_evidence_rejects_malformed_field_values(
    tmp_path, location, field, value
):
    """Malformed values must not satisfy a manifest merely because fields exist."""
    release_dir, manifest = _write_release_manifest(tmp_path)
    target = manifest if location == "manifest" else manifest["commands"][0]
    target[field] = value
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


def test_release_evidence_rejects_reversed_command_times(tmp_path):
    """An end time before its start cannot describe an executed command."""
    release_dir, manifest = _write_release_manifest(tmp_path)
    command = manifest["commands"][0]
    command["started_at"] = "2026-07-28T00:02:00+00:00"
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


def test_release_evidence_rejects_missing_release_directory(tmp_path):
    """Every invalid verifier input uses the same public ValueError contract."""
    with pytest.raises(ValueError):
        _verify_release_evidence(tmp_path / "missing-release")


@pytest.mark.parametrize("kind", ["parent_escape", "absolute", "directory", "symlink"])
def test_release_evidence_rejects_unsafe_raw_log_paths(tmp_path, kind):
    """Raw evidence must be a real regular file contained by its release directory."""
    release_dir, manifest = _write_release_manifest(tmp_path)
    command = manifest["commands"][0]
    outside = tmp_path / "outside.log"
    outside.write_text("outside\n")
    if kind == "parent_escape":
        command["raw_log"] = "../../../outside.log"
    elif kind == "absolute":
        command["raw_log"] = str(outside)
    elif kind == "directory":
        command["raw_log"] = "logs"
    else:
        link = release_dir / "logs" / "linked.log"
        link.symlink_to(outside)
        command["raw_log"] = "logs/linked.log"
        command["raw_log_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    (release_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        _verify_release_evidence(release_dir)


@pytest.mark.parametrize(
    ("browser_headers", "expected_status"),
    [
        ({"Origin": "https://evil.example"}, 403),
        ({"Origin": "https://testserver:not-a-port"}, 403),
        (
            {
                "Origin": "https://testserver",
                "Sec-Fetch-Site": "same-origin",
                "X-Forwarded-Proto": "https",
            },
            201,
        ),
        (
            {
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "same-origin",
                "X-Forwarded-Host": "evil.example",
                "X-Forwarded-Proto": "https",
            },
            403,
        ),
    ],
)
def test_csrf_origin_policy_handles_origin_only_malformed_and_proxy_headers(
    resume_client, browser_headers, expected_status
):
    """Origin validation uses the request Host, allowing only proxy protocol adaptation."""
    client, _ = resume_client
    response = client.post(
        "/v1/facts",
        json={"kind": "responsibility", "value": "Protected write"},
        headers=_cookie_write_headers("csrf-security", browser_headers),
    )

    assert response.status_code == expected_status
    if expected_status == 403:
        assert response.json()["error"]["code"] == "CSRF_ORIGIN_FORBIDDEN"


@pytest.mark.parametrize("corruption", ["missing", "duplicate"])
def test_version_list_rejects_invalid_persisted_operation_history(
    resume_client, corruption
):
    """Missing or ambiguous audit rows must produce one stable domain error."""
    client, sessions = resume_client
    resume_id = _resume(client, f"operation-corruption-{corruption}")
    saved = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=_version_request(0, _snapshot("operation history")),
        headers=_headers(f"operation-save-{corruption}"),
    )
    assert saved.status_code == 201

    async def corrupt_operation() -> None:
        async with sessions.begin() as session:
            operation = await session.scalar(
                select(VersionOperation).where(
                    VersionOperation.version_id == saved.json()["id"]
                )
            )
            assert operation is not None
            if corruption == "missing":
                await session.delete(operation)
            else:
                session.add(
                    VersionOperation(
                        id="vop_duplicate",
                        owner_user_id=operation.owner_user_id,
                        version_id=operation.version_id,
                        operation_type="restore",
                        actor=operation.actor,
                        metadata_json={},
                    )
                )

    asyncio.run(corrupt_operation())
    response = client.get(f"/v1/resumes/{resume_id}/versions")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "RESUME_VERSION_OPERATION_INVALID"
