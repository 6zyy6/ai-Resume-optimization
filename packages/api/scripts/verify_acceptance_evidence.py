import argparse
import hashlib
import hmac
import json
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
    "commands",
}
_COMMAND_FIELDS = {
    "command",
    "started_at",
    "ended_at",
    "exit_code",
    "raw_log",
    "raw_log_sha256",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


def verify_release_evidence(release_dir: Path) -> None:
    try:
        root = Path(release_dir).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("release directory is missing") from error
    if not root.is_dir():
        raise ValueError("release directory is not a directory")
    manifest_path = root / "manifest.json"
    manifest = _load_manifest(manifest_path)
    _require_fields(manifest, _MANIFEST_FIELDS, "manifest")
    if _nonempty_string(manifest["release_id"], "release_id") != root.name:
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
    if (
        not isinstance(manifest["acceptance_status"], str)
        or manifest["acceptance_status"] not in {"PASS", "FAIL", "BLOCKED"}
    ):
        raise ValueError("acceptance_status is invalid")
    commands = manifest["commands"]
    if not isinstance(commands, list) or not commands:
        raise ValueError("commands must be a non-empty list")
    for index, command in enumerate(commands):
        _verify_command(root, command, index)


def _verify_command(root: Path, command: Any, index: int) -> None:
    if not isinstance(command, dict):
        raise ValueError(f"commands[{index}] must be an object")
    _require_fields(command, _COMMAND_FIELDS, f"commands[{index}]")
    _nonempty_string(command["command"], f"commands[{index}].command")
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
    if type(command["exit_code"]) is not int:
        raise ValueError(f"commands[{index}].exit_code must be an integer")
    raw_log = _nonempty_string(
        command["raw_log"],
        f"commands[{index}].raw_log",
    )
    expected_hash = command["raw_log_sha256"]
    if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
        raise ValueError(f"commands[{index}].raw_log_sha256 is invalid")
    log_path = _contained_regular_file(root, raw_log)
    actual_hash = hashlib.sha256(log_path.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise ValueError(f"commands[{index}] raw log hash does not match")


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("manifest must not be a symbolic link")
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise ValueError("manifest is missing") from error
    if not stat.S_ISREG(mode):
        raise ValueError("manifest must be a regular file")
    try:
        manifest = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("manifest is not valid JSON") from error
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    return manifest


def _contained_regular_file(root: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise ValueError("raw_log must be relative")
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("raw_log escapes the release directory or is missing") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("raw_log path must not contain symbolic links")
    try:
        mode = candidate.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise ValueError("raw_log is missing") from error
    if not stat.S_ISREG(mode):
        raise ValueError("raw_log must be a regular file")
    return resolved


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
