import argparse
import hashlib
import hmac
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any


_MANIFEST_FIELDS = {
    "release_id",
    "commit_sha",
    "created_at",
    "scope",
    "acceptance_status",
    "web_image_digest",
    "api_image_digest",
    "pi_image_digest",
    "worker_image_digest",
    "miniprogram_build_version",
    "database_schema_version",
    "prompt_version",
    "workflow_version",
    "model_route_version",
    "template_version",
    "test_environment",
    "executor",
    "reviewer",
    "open_severity_counts",
    "commands",
    "acceptance_items",
}
_COMMAND_FIELDS = {
    "command",
    "started_at",
    "ended_at",
    "exit_code",
    "raw_log",
    "raw_log_sha256",
}
_ACCEPTANCE_ITEM_FIELDS = {"id", "status", "evidence"}
_EVIDENCE_FIELDS = {"path", "sha256"}
_REQUIRED_GATE_COMMANDS = {
    "pnpm generate",
    "pnpm lint",
    "pnpm test",
    "pnpm build",
}
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
_ACCEPTANCE_IDS = {
    f"{group}-{index:02d}"
    for group, count in _ACCEPTANCE_GROUP_COUNTS.items()
    for index in range(1, count + 1)
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_STAT_IDENTITY_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")


def verify_release_evidence(release_dir: Path) -> None:
    _require_secure_open_support()
    release_path = Path(release_dir)
    try:
        root_fd = os.open(
            release_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise ValueError("release directory is missing, unsafe, or not a directory") from error
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise ValueError("release directory is not a directory")
        manifest = _load_manifest(root_fd)
        _verify_manifest(root_fd, release_path.name, manifest)
    finally:
        os.close(root_fd)


def _verify_manifest(root_fd: int, release_name: str, manifest: dict[str, Any]) -> None:
    _require_fields(manifest, _MANIFEST_FIELDS, "manifest")
    if _nonempty_string(manifest["release_id"], "release_id") != release_name:
        raise ValueError("release_id does not match release directory")
    if not (
        isinstance(manifest["commit_sha"], str)
        and _COMMIT_SHA.fullmatch(manifest["commit_sha"])
    ):
        raise ValueError("commit_sha must be a 40-character lowercase hex SHA")
    _timestamp(manifest["created_at"], "created_at")
    scope = manifest["scope"]
    if (
        not isinstance(scope, list)
        or not scope
        or any(not isinstance(item, str) or not item.strip() for item in scope)
    ):
        raise ValueError("scope must be a non-empty list of strings")
    acceptance_status = manifest["acceptance_status"]
    if (
        not isinstance(acceptance_status, str)
        or acceptance_status not in {"PASS", "FAIL", "BLOCKED"}
    ):
        raise ValueError("acceptance_status is invalid")

    for field in (
        "miniprogram_build_version",
        "database_schema_version",
        "prompt_version",
        "workflow_version",
        "model_route_version",
        "template_version",
        "test_environment",
        "executor",
        "reviewer",
    ):
        _nonempty_string(manifest[field], field)
    for field in (
        "web_image_digest",
        "api_image_digest",
        "pi_image_digest",
        "worker_image_digest",
    ):
        value = manifest[field]
        if not isinstance(value, str) or not _IMAGE_DIGEST.fullmatch(value):
            raise ValueError(f"{field} must be a sha256 image digest")

    severity_counts = _verify_severity_counts(manifest["open_severity_counts"])
    file_hashes: dict[str, str] = {}
    commands = _verify_commands(root_fd, manifest["commands"], file_hashes)
    item_statuses = _verify_acceptance_items(
        root_fd,
        manifest["acceptance_items"],
        file_hashes,
    )
    if acceptance_status == "PASS":
        if "release" not in scope:
            raise ValueError("PASS requires release scope")
        if any(exit_code != 0 for _, exit_code in commands):
            raise ValueError("PASS requires every command to succeed")
        command_names = {command for command, _ in commands}
        if not _REQUIRED_GATE_COMMANDS.issubset(command_names):
            raise ValueError("PASS is missing a required root gate command")
        if any(status != "PASS" for status in item_statuses):
            raise ValueError("PASS requires every acceptance item to pass")
        if severity_counts["sev1"] or severity_counts["sev2"]:
            raise ValueError("PASS requires zero open Sev1 and Sev2 issues")


def _verify_severity_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("open_severity_counts must be an object")
    required = {"sev1", "sev2", "sev3", "sev4"}
    _require_fields(value, required, "open_severity_counts")
    for field in required:
        if type(value[field]) is not int or value[field] < 0:
            raise ValueError(f"open_severity_counts.{field} must be a non-negative integer")
    return value


def _verify_commands(
    root_fd: int,
    commands: Any,
    file_hashes: dict[str, str],
) -> list[tuple[str, int]]:
    if not isinstance(commands, list) or not commands:
        raise ValueError("commands must be a non-empty list")
    verified: list[tuple[str, int]] = []
    for index, command in enumerate(commands):
        if not isinstance(command, dict):
            raise ValueError(f"commands[{index}] must be an object")
        _require_fields(command, _COMMAND_FIELDS, f"commands[{index}]")
        command_name = _nonempty_string(
            command["command"],
            f"commands[{index}].command",
        ).strip()
        started_at = _timestamp(
            command["started_at"],
            f"commands[{index}].started_at",
        )
        ended_at = _timestamp(
            command["ended_at"],
            f"commands[{index}].ended_at",
        )
        if ended_at < started_at:
            raise ValueError(f"commands[{index}] ends before it starts")
        exit_code = command["exit_code"]
        if type(exit_code) is not int:
            raise ValueError(f"commands[{index}].exit_code must be an integer")
        _verify_file_hash(
            root_fd,
            command["raw_log"],
            command["raw_log_sha256"],
            f"commands[{index}].raw_log",
            file_hashes,
        )
        verified.append((command_name, exit_code))
    return verified


def _verify_acceptance_items(
    root_fd: int,
    items: Any,
    file_hashes: dict[str, str],
) -> list[str]:
    if not isinstance(items, list) or len(items) != len(_ACCEPTANCE_IDS):
        raise ValueError("acceptance_items must contain all 146 known IDs exactly once")
    ids: list[str] = []
    statuses: list[str] = []
    for index, item in enumerate(items):
        path = f"acceptance_items[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{path} must be an object")
        _require_fields(item, _ACCEPTANCE_ITEM_FIELDS, path)
        item_id = _nonempty_string(item["id"], f"{path}.id")
        status_value = item["status"]
        if (
            not isinstance(status_value, str)
            or status_value not in {"PASS", "FAIL", "BLOCKED"}
        ):
            raise ValueError(f"{path}.status is invalid")
        evidence = item["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"{path}.evidence must be a non-empty list")
        for evidence_index, evidence_item in enumerate(evidence):
            evidence_path = f"{path}.evidence[{evidence_index}]"
            if not isinstance(evidence_item, dict):
                raise ValueError(f"{evidence_path} must be an object")
            _require_fields(evidence_item, _EVIDENCE_FIELDS, evidence_path)
            _verify_file_hash(
                root_fd,
                evidence_item["path"],
                evidence_item["sha256"],
                f"{evidence_path}.path",
                file_hashes,
            )
        ids.append(item_id)
        statuses.append(status_value)
    if len(set(ids)) != len(ids) or set(ids) != _ACCEPTANCE_IDS:
        raise ValueError("acceptance_items must contain all 146 known IDs exactly once")
    return statuses


def _verify_file_hash(
    root_fd: int,
    raw_path: Any,
    expected_hash: Any,
    label: str,
    file_hashes: dict[str, str],
) -> None:
    path = _nonempty_string(raw_path, label)
    if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
        raise ValueError(f"{label} sha256 is invalid")
    actual_hash = file_hashes.get(path)
    if actual_hash is None:
        actual_hash = hashlib.sha256(_read_regular_file(root_fd, path, label)).hexdigest()
        file_hashes[path] = actual_hash
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise ValueError(f"{label} hash does not match")


def _load_manifest(root_fd: int) -> dict[str, Any]:
    try:
        manifest = json.loads(
            _read_regular_file(root_fd, "manifest.json", "manifest").decode("utf-8")
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("manifest is not valid JSON") from error
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    return manifest


def _read_regular_file(root_fd: int, raw_path: str, label: str) -> bytes:
    parts = raw_path.split("/")
    if (
        raw_path.startswith("/")
        or "\\" in raw_path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"{label} must be a contained relative path")
    current_fd = os.dup(root_fd)
    file_fd: int | None = None
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=current_fd,
        )
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} must be a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(file_fd, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(file_fd)
        if any(
            getattr(before, field) != getattr(after, field)
            for field in _STAT_IDENTITY_FIELDS
        ):
            raise ValueError(f"{label} changed while it was read")
        return b"".join(chunks)
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"{label} is missing or unsafe") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(current_fd)


def _require_secure_open_support() -> None:
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
    ):
        raise ValueError("secure evidence verification is unsupported on this platform")


def _require_fields(values: dict[str, Any], fields: set[str], path: str) -> None:
    missing = fields - values.keys()
    if missing:
        raise ValueError(f"{path} is missing required fields")


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _timestamp(value: Any, path: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{path} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{path} must include a UTC offset")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    verify_release_evidence(parser.parse_args().release_dir)


if __name__ == "__main__":
    main()
