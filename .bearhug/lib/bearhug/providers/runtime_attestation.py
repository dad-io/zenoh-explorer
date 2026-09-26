"""Fail-closed per-session runtime attestation for provider lifecycle evidence.

The installer proves which bytes are present. This record proves a separate, bounded fact:
which installed materialization was associated with a provider session and which lifecycle event
digests were actually observed on that session's owned transport. It never asserts that an
unobserved hook ran, or that a configured model is an execution-model attestation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.paths import assert_writable
from bearhug.providers.claude import ClaudeNormalization
from bearhug.providers.codex_app_server import CodexAppServerNormalization


class RuntimeAttestationError(ValueError):
    """Runtime evidence is malformed, incomplete, or cannot be bound safely."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{32,128}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9./:_-]{0,127}$")
_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "provider",
        "adapter",
        "adapter_version",
        "observed_at",
        "native_materialization_sha256",
        "source_policy_sha256",
        "installed_manifest_sha256",
        "install_nonce",
        "session_id",
        "thread_id",
        "turn_id",
        "raw_event_sha256",
        "input_sha256",
        "expected_events",
        "observed_events",
        "missing_events",
        "mismatched_events",
        "promotion_eligible",
        "promotion_blockers",
        "limitations",
        "content_sha256",
    }
)
_EVENT_FIELDS = frozenset(
    {"kind", "sequence", "raw_line_sha256", "session_id", "thread_id", "turn_id"}
)
_PROVIDER_ADAPTERS = {
    "anthropic-claude": "claude-code-stream-json",
    "openai-codex": "codex-app-server-stdio",
}


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeAttestationError(
            f"runtime attestation is not canonical JSON: {exc}"
        ) from exc


def _sha(value: Any, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RuntimeAttestationError(f"{name} must be lowercase SHA-256")


def _nonempty(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise RuntimeAttestationError(f"{name} must be a bounded non-empty string")


def _identifier(value: Any, name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RuntimeAttestationError(f"{name} must be a lowercase identifier")


def _strings(value: Any, name: str, *, nonempty: bool = False) -> None:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise RuntimeAttestationError(f"{name} must be a unique array of non-empty strings")


def _timestamp(value: Any) -> None:
    if not isinstance(value, str):
        raise RuntimeAttestationError("observed_at must be a UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise RuntimeAttestationError(
            "observed_at must be a UTC whole-second timestamp"
        ) from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise RuntimeAttestationError("observed_at must be a UTC whole-second timestamp")


def _content_address(value: Mapping[str, Any]) -> str:
    material = dict(value)
    material.pop("content_sha256", None)
    return hashlib.sha256(_canonical(material)).hexdigest()


def _claude_hook_label(value: str) -> str:
    """Render a Claude CamelCase hook name as the normalized lowercase event label."""

    return re.sub(r"(?<!^)(?=[A-Z])", "-", value).lower()


def _event(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _EVENT_FIELDS:
        raise RuntimeAttestationError(f"observed_events[{index}] has missing or unknown fields")
    if not isinstance(value["kind"], str) or _EVENT_NAME.fullmatch(value["kind"]) is None:
        raise RuntimeAttestationError(f"observed_events[{index}].kind is invalid")
    if type(value["sequence"]) is not int or value["sequence"] < 0:
        raise RuntimeAttestationError(f"observed_events[{index}].sequence is invalid")
    _sha(value["raw_line_sha256"], f"observed_events[{index}].raw_line_sha256")
    _nonempty(value["session_id"], f"observed_events[{index}].session_id")
    for field in ("thread_id", "turn_id"):
        if value[field] is not None:
            _nonempty(value[field], f"observed_events[{index}].{field}")
    return value


def validate_runtime_attestation(value: Any) -> dict[str, Any]:
    """Validate one closed runtime-attestation record without coercion."""

    if not isinstance(value, dict) or set(value) != _ATTESTATION_FIELDS:
        raise RuntimeAttestationError("runtime attestation has missing or unknown fields")
    if value["schema_version"] != "1" or value["record_kind"] != "provider_runtime_attestation":
        raise RuntimeAttestationError("unsupported runtime attestation schema or record kind")
    for field in ("provider", "adapter", "adapter_version", "session_id"):
        _nonempty(value[field], field)
    for field in ("thread_id", "turn_id"):
        if value[field] is not None:
            _nonempty(value[field], field)
    _timestamp(value["observed_at"])
    for field in (
        "native_materialization_sha256",
        "source_policy_sha256",
        "installed_manifest_sha256",
        "raw_event_sha256",
        "input_sha256",
    ):
        _sha(value[field], field)
    if (
        not isinstance(value["install_nonce"], str)
        or _NONCE.fullmatch(value["install_nonce"]) is None
    ):
        raise RuntimeAttestationError("install_nonce must be lowercase hex nonce")
    _identifier(value["provider"], "provider")
    _identifier(value["adapter"], "adapter")
    if value["provider"] not in _PROVIDER_ADAPTERS:
        raise RuntimeAttestationError("provider is unsupported")
    if _PROVIDER_ADAPTERS[value["provider"]] != value["adapter"]:
        raise RuntimeAttestationError("provider and adapter do not name one reviewed pair")
    if value["provider"] == "anthropic-claude" and value["thread_id"] is not None:
        raise RuntimeAttestationError("Claude runtime attestation thread_id must be null")
    if value["provider"] == "openai-codex" and value["thread_id"] is None:
        raise RuntimeAttestationError("Codex runtime attestation requires a thread_id")
    if (
        not isinstance(value["adapter_version"], str)
        or not 1 <= len(value["adapter_version"]) <= 128
    ):
        raise RuntimeAttestationError("adapter_version must be a bounded non-empty string")
    _strings(value["expected_events"], "expected_events", nonempty=True)
    if any(_EVENT_NAME.fullmatch(item) is None for item in value["expected_events"]):
        raise RuntimeAttestationError("expected_events contains an invalid event name")
    observed = [_event(item, index) for index, item in enumerate(value["observed_events"])]
    sequences = [item["sequence"] for item in observed]
    if sequences != sorted(sequences):
        raise RuntimeAttestationError("observed_events must be ordered by sequence")
    if len(set(sequences)) != len(sequences):
        raise RuntimeAttestationError("observed_events contains duplicate sequences")
    mismatches = set(value["mismatched_events"])
    for item in observed:
        if item["session_id"] != value["session_id"] or item["thread_id"] != value["thread_id"]:
            expected = f"{item['kind']}@{item['sequence']}:session_or_thread"
            if expected not in mismatches:
                raise RuntimeAttestationError(
                    "observed event identity mismatch is missing from mismatched_events"
                )
        if item["turn_id"] is not None and item["turn_id"] != value["turn_id"]:
            expected = f"{item['kind']}@{item['sequence']}:turn"
            if expected not in mismatches:
                raise RuntimeAttestationError(
                    "observed event turn mismatch is missing from mismatched_events"
                )
    _strings(value["missing_events"], "missing_events")
    _strings(value["mismatched_events"], "mismatched_events")
    _strings(value["promotion_blockers"], "promotion_blockers")
    _strings(value["limitations"], "limitations", nonempty=True)
    if type(value["promotion_eligible"]) is not bool:
        raise RuntimeAttestationError("promotion_eligible must be boolean")
    observed_kinds = {item["kind"] for item in observed}
    missing = [item for item in value["expected_events"] if item not in observed_kinds]
    if missing != value["missing_events"]:
        raise RuntimeAttestationError("missing_events does not match expected and observed events")
    expected_blockers = [f"required_runtime_event_missing:{item}" for item in missing]
    expected_blockers.extend(
        f"required_runtime_event_mismatch:{item}" for item in value["mismatched_events"]
    )
    if expected_blockers != value["promotion_blockers"]:
        raise RuntimeAttestationError("promotion_blockers does not match runtime evidence")
    eligible = not value["missing_events"] and not value["mismatched_events"]
    if value["promotion_eligible"] != eligible:
        raise RuntimeAttestationError("promotion_eligible does not match runtime evidence")
    if value["content_sha256"] != _content_address(value):
        raise RuntimeAttestationError("runtime attestation content digest is false")
    return value


def build_runtime_attestation(
    *,
    provider: str,
    adapter: str,
    adapter_version: str,
    observed_at: datetime,
    native_materialization_sha256: str,
    source_policy_sha256: str,
    installed_manifest_sha256: str,
    install_nonce: str,
    session_id: str,
    thread_id: str | None,
    turn_id: str | None,
    raw_event_sha256: str,
    input_sha256: str,
    expected_events: Iterable[str],
    observed_events: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a digest-only attestation and derive fail-closed eligibility."""

    expected = list(expected_events)
    observations = [dict(item) for item in observed_events]
    observed_kinds = {item.get("kind") for item in observations}
    missing = [item for item in expected if item not in observed_kinds]
    mismatched: list[str] = []
    for item in observations:
        label = f"{item.get('kind')}@{item.get('sequence')}"
        if item.get("session_id") != session_id or item.get("thread_id") != thread_id:
            mismatched.append(f"{label}:session_or_thread")
        if item.get("turn_id") is not None and item.get("turn_id") != turn_id:
            mismatched.append(f"{label}:turn")
    mismatched = list(dict.fromkeys(mismatched))
    blockers = [f"required_runtime_event_missing:{item}" for item in missing]
    blockers.extend(f"required_runtime_event_mismatch:{item}" for item in mismatched)
    value: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "provider_runtime_attestation",
        "provider": provider,
        "adapter": adapter,
        "adapter_version": adapter_version,
        "observed_at": observed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "native_materialization_sha256": native_materialization_sha256,
        "source_policy_sha256": source_policy_sha256,
        "installed_manifest_sha256": installed_manifest_sha256,
        "install_nonce": install_nonce,
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "raw_event_sha256": raw_event_sha256,
        "input_sha256": input_sha256,
        "expected_events": expected,
        "observed_events": observations,
        "missing_events": missing,
        "mismatched_events": mismatched,
        "promotion_eligible": not missing and not mismatched,
        "promotion_blockers": blockers,
        "limitations": [
            "observed lifecycle evidence only; provider trust and effective source "
            "remain separate claims"
        ],
    }
    value["content_sha256"] = _content_address(value)
    return validate_runtime_attestation(value)


def write_runtime_attestation(value: Mapping[str, Any], path: Path | str) -> Path:
    """Write one validated attestation create-only, without following a symlink."""

    record = validate_runtime_attestation(dict(value))
    target = assert_writable(Path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    raw = _canonical(record)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise RuntimeAttestationError("runtime attestation destination already exists") from exc
    except OSError as exc:
        raise RuntimeAttestationError(f"cannot create runtime attestation: {exc}") from exc
    try:
        written = os.write(descriptor, raw)
        if written != len(raw):
            raise RuntimeAttestationError("runtime attestation write was incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return target


def codex_runtime_event_observations(
    normalization: CodexAppServerNormalization,
    raw_server_events: bytes,
) -> tuple[dict[str, Any], ...]:
    """Extract only Codex hook lifecycle notifications from normalized custody."""

    if hashlib.sha256(raw_server_events).hexdigest() != normalization.raw_event_sha256:
        raise RuntimeAttestationError("server event custody digest does not match normalization")
    lines = raw_server_events.splitlines()
    if len(lines) != normalization.raw_event_count:
        raise RuntimeAttestationError("server event custody count does not match normalization")
    result: list[dict[str, Any]] = []
    for event in normalization.events:
        if (
            event.sequence >= len(lines)
            or hashlib.sha256(lines[event.sequence]).hexdigest() != event.raw_line_sha256
        ):
            raise RuntimeAttestationError("normalized event digest does not match server custody")
        if event.kind not in {"hook/started", "hook/completed"}:
            continue
        try:
            message = json.loads(lines[event.sequence])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeAttestationError("hook event custody is not valid JSON") from exc
        params = message.get("params") if isinstance(message, dict) else None
        if not isinstance(params, dict):
            raise RuntimeAttestationError(f"{event.kind} has no params object")
        native_thread = params.get("threadId")
        native_turn = params.get("turnId")
        if not isinstance(native_thread, str) or not native_thread:
            raise RuntimeAttestationError(f"{event.kind} has no thread id")
        if native_turn is not None and (not isinstance(native_turn, str) or not native_turn):
            raise RuntimeAttestationError(f"{event.kind} has an invalid turn id")
        result.append(
            {
                "kind": event.kind,
                "sequence": event.sequence,
                "raw_line_sha256": event.raw_line_sha256,
                "session_id": normalization.session_id,
                "thread_id": native_thread,
                "turn_id": native_turn,
            }
        )
    return tuple(result)


def claude_runtime_event_observations(
    normalization: ClaudeNormalization,
    raw_events: bytes,
) -> tuple[dict[str, Any], ...]:
    """Extract only Claude hook lifecycle lines from normalized stream custody.

    Claude's stream exposes hook start/response records but no separate thread identifier.  The
    absence is preserved as ``null``; a session id is not silently promoted to a thread id.
    Legacy ``hook_event`` records are retained as observations with a distinct kind so they cannot
    be mistaken for the richer start/response lifecycle.
    """

    if hashlib.sha256(raw_events).hexdigest() != normalization.raw_event_sha256:
        raise RuntimeAttestationError("Claude event custody digest does not match normalization")
    lines = raw_events.splitlines()
    if len(lines) != normalization.raw_event_count:
        raise RuntimeAttestationError("Claude event custody count does not match normalization")
    result: list[dict[str, Any]] = []
    for event in normalization.events:
        if (
            event.sequence >= len(lines)
            or hashlib.sha256(lines[event.sequence]).hexdigest() != event.raw_line_sha256
        ):
            raise RuntimeAttestationError("normalized Claude event digest does not match custody")
        try:
            record = json.loads(lines[event.sequence])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeAttestationError("Claude hook event custody is not valid JSON") from exc
        if not isinstance(record, dict):
            raise RuntimeAttestationError("Claude hook event custody is not an object")
        if record.get("session_id") != normalization.session_id:
            raise RuntimeAttestationError("Claude hook event session id does not match custody")
        record_type = record.get("type")
        subtype = record.get("subtype")
        if record_type == "system" and subtype in {"hook_started", "hook_response"}:
            hook_event = record.get("hook_event")
            if not isinstance(hook_event, str) or not hook_event:
                raise RuntimeAttestationError("Claude hook lifecycle record has no hook event")
            kind = (
                "hook/started:" if subtype == "hook_started" else "hook/response:"
            ) + _claude_hook_label(hook_event)
        elif record_type == "hook_event":
            hook_event = record.get("hook_event_name")
            if not isinstance(hook_event, str) or not hook_event:
                raise RuntimeAttestationError("Claude legacy hook record has no hook event")
            kind = "hook/observed:" + _claude_hook_label(hook_event)
        else:
            continue
        result.append(
            {
                "kind": kind,
                "sequence": event.sequence,
                "raw_line_sha256": event.raw_line_sha256,
                "session_id": normalization.session_id,
                "thread_id": None,
                "turn_id": None,
            }
        )
    return tuple(result)


def build_codex_runtime_attestation(
    normalization: CodexAppServerNormalization,
    *,
    raw_server_events: bytes,
    input_sha256: str,
    native_materialization_sha256: str,
    source_policy_sha256: str,
    installed_manifest_sha256: str,
    install_nonce: str,
    expected_events: Iterable[str],
    observed_at: datetime,
) -> dict[str, Any]:
    """Bind App Server server/client custody to installed harness identity."""

    observations = codex_runtime_event_observations(normalization, raw_server_events)
    return build_runtime_attestation(
        provider=normalization.provider,
        adapter=normalization.adapter,
        adapter_version=normalization.adapter_version,
        observed_at=observed_at,
        native_materialization_sha256=native_materialization_sha256,
        source_policy_sha256=source_policy_sha256,
        installed_manifest_sha256=installed_manifest_sha256,
        install_nonce=install_nonce,
        session_id=normalization.session_id,
        thread_id=normalization.thread_id,
        turn_id=normalization.turn_id,
        raw_event_sha256=normalization.raw_event_sha256,
        input_sha256=input_sha256,
        expected_events=expected_events,
        observed_events=observations,
    )


def build_claude_runtime_attestation(
    normalization: ClaudeNormalization,
    *,
    raw_events: bytes,
    input_sha256: str,
    native_materialization_sha256: str,
    source_policy_sha256: str,
    installed_manifest_sha256: str,
    install_nonce: str,
    expected_events: Iterable[str],
    observed_at: datetime,
) -> dict[str, Any]:
    """Bind Claude stream custody to an installed harness identity."""

    observations = claude_runtime_event_observations(normalization, raw_events)
    return build_runtime_attestation(
        provider=normalization.provider,
        adapter=normalization.adapter,
        adapter_version=normalization.adapter_version,
        observed_at=observed_at,
        native_materialization_sha256=native_materialization_sha256,
        source_policy_sha256=source_policy_sha256,
        installed_manifest_sha256=installed_manifest_sha256,
        install_nonce=install_nonce,
        session_id=normalization.session_id,
        thread_id=normalization.thread_id,
        turn_id=normalization.turn_id,
        raw_event_sha256=normalization.raw_event_sha256,
        input_sha256=input_sha256,
        expected_events=expected_events,
        observed_events=observations,
    )


__all__ = [
    "RuntimeAttestationError",
    "build_claude_runtime_attestation",
    "build_codex_runtime_attestation",
    "build_runtime_attestation",
    "claude_runtime_event_observations",
    "codex_runtime_event_observations",
    "validate_runtime_attestation",
    "write_runtime_attestation",
]
