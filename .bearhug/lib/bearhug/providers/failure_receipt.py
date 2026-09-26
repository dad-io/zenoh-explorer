"""Closed receipts for provider launches that cannot produce a success receipt.

Failure receipts preserve only facts available at the failure boundary.  In particular, they
never synthesize a session, thread, event count, close repository, or promotion candidate.
"""

from __future__ import annotations

import json
import math
import os
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.paths import assert_writable
from bearhug.providers.receipt import LaunchRepository, sha256_bytes


class ProviderFailureReceiptError(ValueError):
    """A failed-run receipt or custody reference is malformed or ambiguous."""


_TOP = frozenset(
    {
        "schema_version",
        "record_kind",
        "provider",
        "adapter",
        "adapter_version",
        "observed_at",
        "cwd",
        "role",
        "required_capabilities",
        "stage",
        "reason_code",
        "detail",
        "terminal_state",
        "exit_code",
        "timeout_seconds",
        "session_id",
        "thread_id",
        "raw_event_count",
        "custody",
        "launch_repository",
        "candidate",
        "promotion_eligible",
        "launch",
        "limitations",
    }
)
_CUSTODY = frozenset({"request", "raw_events", "stderr"})
_ARTIFACT = frozenset({"path", "sha256", "bytes"})
_LAUNCH_REPOSITORY = frozenset({"repository_common_dir_sha256", "head_oid", "tree_oid", "clean"})
_LAUNCH = frozenset(
    {
        "argv_sha256",
        "prompt_sha256",
        "settings_sha256",
        "rules_sha256",
        "sandbox",
        "approval_policy",
    }
)
_REASON_SEMANTICS = {
    "launch_os_error": ("launch", "failed"),
    "timeout": ("transport", "incomplete"),
    "nonzero_exit": ("provider", "failed"),
    "protocol_error": ("protocol", "incomplete"),
    "normalization_error": ("normalization", "incomplete"),
}


def _exact(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProviderFailureReceiptError(f"{name} has missing or unknown fields")
    return value


def _sha(value: Any, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ProviderFailureReceiptError(f"{name} must be lowercase SHA-256")


def _git_oid(value: Any, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) not in {40, 64}
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ProviderFailureReceiptError(f"{name} must be a full Git object id")


def _optional_string(value: Any, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ProviderFailureReceiptError(f"{name} must be a non-empty string or null")


def _provider_token(value: Any, name: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9-]{1,63}", value) is None:
        raise ProviderFailureReceiptError(f"{name} must be a lowercase provider token")


def _unique_strings(value: Any, name: str, *, nonempty: bool = False) -> None:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise ProviderFailureReceiptError(f"{name} must be a unique string array")


def _artifact(path: Path | None, run_root: Path) -> dict[str, Any] | None:
    if path is None:
        return None
    root = run_root.resolve()
    target = path.resolve(strict=True)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ProviderFailureReceiptError("custody artifact is outside its provider run") from exc
    if not target.is_file() or relative == Path("."):
        raise ProviderFailureReceiptError("custody artifact must be a regular file")
    encoded_path = relative.as_posix()
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ProviderFailureReceiptError("custody artifact path is not safe and relative")
    raw = target.read_bytes()
    return {"path": encoded_path, "sha256": sha256_bytes(raw), "bytes": len(raw)}


def _parse_complete_json_objects(raw: bytes) -> tuple[dict[str, Any], ...]:
    """Return only independently valid, complete JSONL objects.

    A corrupt line cannot make a neighbouring provider identity authoritative.  Duplicate keys
    are rejected so the failure path never chooses between ambiguous values.
    """

    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key {key!r}")
            value[key] = item
        return value

    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line:
            continue
        try:
            value = json.loads(line, object_pairs_hook=closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return tuple(records)


def observed_app_server_identity(raw: bytes) -> tuple[str | None, str | None]:
    """Conservatively recover explicit session/thread values from partial App Server JSONL."""

    sessions: set[str] = set()
    threads: set[str] = set()
    for message in _parse_complete_json_objects(raw):
        result = message.get("result")
        result_thread = result.get("thread") if isinstance(result, dict) else None
        if isinstance(result_thread, dict):
            thread_id = result_thread.get("id")
            session_id = result_thread.get("sessionId")
            if isinstance(thread_id, str) and thread_id:
                threads.add(thread_id)
            if isinstance(session_id, str) and session_id:
                sessions.add(session_id)
        params = message.get("params")
        if isinstance(params, dict):
            thread_id = params.get("threadId")
            if isinstance(thread_id, str) and thread_id:
                threads.add(thread_id)
    return (
        next(iter(sessions)) if len(sessions) == 1 else None,
        next(iter(threads)) if len(threads) == 1 else None,
    )


def observed_claude_identity(raw: bytes) -> tuple[str | None, None]:
    """Conservatively recover an explicit Claude session id from partial JSONL."""

    sessions = {
        session_id
        for record in _parse_complete_json_objects(raw)
        if isinstance((session_id := record.get("session_id")), str) and session_id
    }
    return (next(iter(sessions)) if len(sessions) == 1 else None, None)


def validate_provider_failure_receipt(value: Any) -> dict[str, Any]:
    """Validate one closed failed-run receipt without coercion."""

    receipt = _exact(value, _TOP, "provider failure receipt")
    if (
        receipt["schema_version"] != "1"
        or receipt["record_kind"] != "provider_session_failure_receipt"
    ):
        raise ProviderFailureReceiptError("unsupported failure receipt schema or record kind")
    for field in ("provider", "adapter", "adapter_version", "observed_at", "cwd", "detail"):
        if not isinstance(receipt[field], str) or not receipt[field]:
            raise ProviderFailureReceiptError(f"{field} must be a non-empty string")
    _provider_token(receipt["provider"], "provider")
    _provider_token(receipt["adapter"], "adapter")
    if len(receipt["adapter_version"]) > 128:
        raise ProviderFailureReceiptError("adapter_version exceeds 128 characters")
    if len(receipt["detail"]) > 1024:
        raise ProviderFailureReceiptError("detail exceeds the privacy-bounded maximum")
    try:
        observed = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderFailureReceiptError("observed_at must be ISO-8601") from exc
    if observed.tzinfo is None or not Path(receipt["cwd"]).is_absolute():
        raise ProviderFailureReceiptError("timestamp must be zoned and cwd must be absolute")
    _optional_string(receipt["role"], "role")
    _optional_string(receipt["session_id"], "session_id")
    _optional_string(receipt["thread_id"], "thread_id")
    _unique_strings(receipt["required_capabilities"], "required_capabilities")
    _unique_strings(receipt["limitations"], "limitations", nonempty=True)
    if any(len(item) > 256 for item in receipt["limitations"]):
        raise ProviderFailureReceiptError("limitations entries must not exceed 256 characters")

    semantics = _REASON_SEMANTICS.get(receipt["reason_code"])
    if semantics is None or (receipt["stage"], receipt["terminal_state"]) != semantics:
        raise ProviderFailureReceiptError("failure stage, reason, and terminal state disagree")
    exit_code = receipt["exit_code"]
    if exit_code is not None and type(exit_code) is not int:
        raise ProviderFailureReceiptError("exit_code must be an integer or null")
    timeout_seconds = receipt["timeout_seconds"]
    if receipt["reason_code"] == "timeout":
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or (timeout_seconds <= 0)
        ):
            raise ProviderFailureReceiptError("timeout_seconds must be positive for a timeout")
    elif timeout_seconds is not None:
        raise ProviderFailureReceiptError("timeout_seconds is allowed only for a timeout")
    if receipt["reason_code"] == "launch_os_error" and exit_code is not None:
        raise ProviderFailureReceiptError("an operating-system launch error has no exit code")
    if receipt["reason_code"] == "nonzero_exit" and (type(exit_code) is not int or exit_code == 0):
        raise ProviderFailureReceiptError("a nonzero exit requires its actual exit code")
    raw_event_count = receipt["raw_event_count"]
    if raw_event_count is not None and (type(raw_event_count) is not int or raw_event_count < 1):
        raise ProviderFailureReceiptError("raw_event_count must be positive or null")

    custody = _exact(receipt["custody"], _CUSTODY, "custody")
    for name, artifact_value in custody.items():
        if artifact_value is None:
            continue
        artifact = _exact(artifact_value, _ARTIFACT, f"custody.{name}")
        path = artifact["path"]
        if (
            not isinstance(path, str)
            or not path
            or re.fullmatch(r"[A-Za-z0-9._/-]+", path) is None
            or Path(path).is_absolute()
            or any(part in {"", ".", ".."} for part in Path(path).parts)
        ):
            raise ProviderFailureReceiptError(f"custody.{name}.path must be safe and relative")
        _sha(artifact["sha256"], f"custody.{name}.sha256")
        if type(artifact["bytes"]) is not int or artifact["bytes"] < 0:
            raise ProviderFailureReceiptError(f"custody.{name}.bytes must be non-negative")

    launch_repository = _exact(
        receipt["launch_repository"], _LAUNCH_REPOSITORY, "launch_repository"
    )
    _sha(
        launch_repository["repository_common_dir_sha256"],
        "launch_repository.repository_common_dir_sha256",
    )
    _git_oid(launch_repository["head_oid"], "launch_repository.head_oid")
    _git_oid(launch_repository["tree_oid"], "launch_repository.tree_oid")
    if launch_repository["clean"] is not True:
        raise ProviderFailureReceiptError("launch repository must be clean")
    if receipt["candidate"] is not None or receipt["promotion_eligible"] is not False:
        raise ProviderFailureReceiptError("failed runs cannot carry a candidate or promotion claim")

    launch = _exact(receipt["launch"], _LAUNCH, "launch")
    _sha(launch["argv_sha256"], "launch.argv_sha256")
    _sha(launch["prompt_sha256"], "launch.prompt_sha256")
    for field in ("settings_sha256", "rules_sha256"):
        if launch[field] is not None:
            _sha(launch[field], f"launch.{field}")
    if launch["sandbox"] not in {"read-only", "workspace-write"} or launch[
        "approval_policy"
    ] not in {"never", "on-request", "untrusted"}:
        raise ProviderFailureReceiptError("launch sandbox or approval policy is unsupported")
    return receipt


def build_provider_failure_receipt(
    *,
    provider: str,
    adapter: str,
    adapter_version: str,
    observed_at: datetime,
    cwd: str,
    role: str | None,
    required_capabilities: tuple[str, ...],
    stage: str,
    reason_code: str,
    detail: str,
    terminal_state: str,
    exit_code: int | None,
    timeout_seconds: float | None,
    session_id: str | None,
    thread_id: str | None,
    raw_event_count: int | None,
    run_root: Path,
    request_path: Path | None,
    raw_events_path: Path | None,
    stderr_path: Path | None,
    launch_repository: LaunchRepository,
    command_sha256: str,
    prompt_sha256: str,
    settings_sha256: str | None,
    rules_sha256: str | None,
    sandbox: str,
    approval_policy: str,
) -> dict[str, Any]:
    """Build a failed-run receipt from explicit launch facts and already-durable custody."""

    bounded_detail = " ".join(detail.split())[:1024] or "provider run failed"
    limitations = ["provider_run_did_not_produce_success_receipt"]
    if session_id is None:
        limitations.append("provider_session_identity_unavailable")
    if thread_id is None:
        limitations.append("provider_thread_identity_unavailable")
    if raw_event_count is None:
        limitations.append("raw_event_count_unestablished")
    value = {
        "schema_version": "1",
        "record_kind": "provider_session_failure_receipt",
        "provider": provider,
        "adapter": adapter,
        "adapter_version": adapter_version,
        "observed_at": observed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cwd": str(Path(cwd).expanduser().resolve()),
        "role": role,
        "required_capabilities": list(required_capabilities),
        "stage": stage,
        "reason_code": reason_code,
        "detail": bounded_detail,
        "terminal_state": terminal_state,
        "exit_code": exit_code,
        "timeout_seconds": timeout_seconds,
        "session_id": session_id,
        "thread_id": thread_id,
        "raw_event_count": raw_event_count,
        "custody": {
            "request": _artifact(request_path, run_root),
            "raw_events": _artifact(raw_events_path, run_root),
            "stderr": _artifact(stderr_path, run_root),
        },
        "launch_repository": launch_repository.to_dict(),
        "candidate": None,
        "promotion_eligible": False,
        "launch": {
            "argv_sha256": command_sha256,
            "prompt_sha256": prompt_sha256,
            "settings_sha256": settings_sha256,
            "rules_sha256": rules_sha256,
            "sandbox": sandbox,
            "approval_policy": approval_policy,
        },
        "limitations": limitations,
    }
    return validate_provider_failure_receipt(value)


def write_provider_failure_receipt(value: dict[str, Any], path: Path | str) -> Path:
    """Create one immutable failure receipt; an existing target is never replaced."""

    validated = validate_provider_failure_receipt(value)
    target = assert_writable(Path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(validated, indent=2, sort_keys=True).encode() + b"\n"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except FileExistsError as exc:
        raise ProviderFailureReceiptError(
            f"refusing to overwrite provider failure receipt: {target}"
        ) from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
    return target


__all__ = [
    "ProviderFailureReceiptError",
    "build_provider_failure_receipt",
    "observed_app_server_identity",
    "observed_claude_identity",
    "validate_provider_failure_receipt",
    "write_provider_failure_receipt",
]
