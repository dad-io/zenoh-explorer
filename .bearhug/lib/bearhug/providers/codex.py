"""Normalize the documented ``codex exec --json`` JSONL surface.

This is deliberately an evidence adapter, not an approval shortcut.  Codex CLI 0.153.0-alpha.5
does not put the resolved model, reasoning effort, settings digest, or candidate Git identity in
its JSONL events.  Those facts therefore remain unobserved until a stronger provider surface or a
separate authenticated attestation supplies them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class CodexEventError(ValueError):
    """The raw stream cannot support even a transport-level session receipt."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodexEventError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CodexLaunchContract:
    """Immutable requested launch facts; none are confused with provider observations."""

    cwd: str
    prompt_sha256: str
    adapter_version: str
    requested_model: str | None = None
    requested_reasoning_effort: str | None = None
    sandbox: str = "read-only"
    profile: str | None = None
    ignore_user_config: bool = True
    ephemeral: bool = True

    def __post_init__(self) -> None:
        if self.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("adapter launch permits only read-only or workspace-write sandbox")
        if len(self.prompt_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.prompt_sha256
        ):
            raise ValueError("prompt_sha256 must be lowercase SHA-256")
        if not Path(self.cwd).is_absolute():
            raise ValueError("cwd must be absolute")


def build_codex_command(contract: CodexLaunchContract) -> tuple[str, ...]:
    """Return a shell-free command; the prompt must be supplied on stdin.

    The command never enables the dangerous sandbox/hook bypass switches.  ``--strict-config``
    makes a misspelled governed setting fail rather than silently disappear.
    """

    command = [
        "codex",
        "exec",
        "--json",
        "--strict-config",
        "--sandbox",
        contract.sandbox,
        "--cd",
        contract.cwd,
    ]
    if contract.ephemeral:
        command.append("--ephemeral")
    if contract.ignore_user_config:
        command.append("--ignore-user-config")
    if contract.profile is not None:
        command.extend(("--profile", contract.profile))
    if contract.requested_model is not None:
        command.extend(("--model", contract.requested_model))
    if contract.requested_reasoning_effort is not None:
        command.extend(
            ("--config", f'model_reasoning_effort="{contract.requested_reasoning_effort}"')
        )
    command.append("-")
    return tuple(command)


@dataclass(frozen=True, slots=True)
class NormalizedCodexEvent:
    sequence: int
    kind: str
    raw_line_sha256: str
    item_type: str | None = None


@dataclass(frozen=True, slots=True)
class CodexUsage:
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int


@dataclass(frozen=True, slots=True)
class CodexNormalization:
    """Facts derivable from one exact raw stream, with absence represented explicitly."""

    provider: str
    adapter: str
    adapter_version: str
    session_id: str
    raw_event_sha256: str
    raw_event_count: int
    terminal_state: str
    requested_model: str | None
    resolved_model: str | None
    requested_reasoning_effort: str | None
    resolved_reasoning_effort: str | None
    identity_verification: str
    promotion_identity_eligible: bool
    usage: CodexUsage | None
    events: tuple[NormalizedCodexEvent, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _usage(payload: Any) -> CodexUsage:
    if not isinstance(payload, dict):
        raise CodexEventError("turn.completed usage must be an object")
    if set(payload) != set(_USAGE_FIELDS):
        raise CodexEventError("turn.completed usage has missing or unknown fields")
    values: list[int] = []
    for field in _USAGE_FIELDS:
        value = payload[field]
        if type(value) is not int or value < 0:  # bool is not a token count
            raise CodexEventError(f"usage.{field} must be a non-negative integer")
        values.append(value)
    return CodexUsage(*values)


def normalize_codex_jsonl(
    raw: bytes,
    *,
    adapter_version: str,
    requested_model: str | None = None,
    requested_reasoning_effort: str | None = None,
) -> CodexNormalization:
    """Normalize exact JSONL bytes while preserving unknown event kinds by digest.

    Append-only provider evolution is allowed: an unknown event remains a normalized event and
    its raw line remains hash-bound.  Lifecycle ambiguity, duplicate keys, malformed usage, or
    events after the terminal event fail closed.
    """

    if not raw or not raw.endswith(b"\n"):
        raise CodexEventError("event stream must be non-empty newline-terminated JSONL")

    parsed: list[tuple[dict[str, Any], bytes]] = []
    for ordinal, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise CodexEventError(f"blank JSONL record at line {ordinal}")
        try:
            event = json.loads(line, object_pairs_hook=_closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexEventError(f"invalid JSONL record at line {ordinal}: {exc}") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise CodexEventError(f"line {ordinal} has no string event type")
        parsed.append((event, line))

    first = parsed[0][0]
    if first.get("type") != "thread.started" or not isinstance(first.get("thread_id"), str):
        raise CodexEventError("first event must be thread.started with a session id")
    session_id = first["thread_id"]
    if not session_id:
        raise CodexEventError("thread id must not be empty")

    turn_started = 0
    terminal_state = "incomplete"
    terminal_seen = False
    usage: CodexUsage | None = None
    normalized: list[NormalizedCodexEvent] = []
    for sequence, (event, line) in enumerate(parsed):
        kind = event["type"]
        if terminal_seen:
            raise CodexEventError("event appears after terminal turn event")
        if kind == "thread.started" and sequence != 0:
            raise CodexEventError("stream contains multiple thread.started events")
        if kind == "turn.started":
            turn_started += 1
        elif kind == "turn.completed":
            terminal_seen = True
            terminal_state = "completed"
            usage = _usage(event.get("usage"))
        elif kind in {"turn.failed", "error"}:
            terminal_seen = True
            terminal_state = "failed"
        item = event.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        if item_type is not None and not isinstance(item_type, str):
            raise CodexEventError(f"event {sequence} has a non-string item type")
        normalized.append(
            NormalizedCodexEvent(sequence, kind, _sha256(line), item_type)
        )

    if turn_started != 1:
        raise CodexEventError(f"expected exactly one turn.started, observed {turn_started}")
    if terminal_state == "completed" and usage is None:
        raise CodexEventError("completed stream has no usage")

    limitations = (
        "resolved_model_not_present_in_codex_jsonl",
        "resolved_reasoning_effort_not_present_in_codex_jsonl",
        "settings_and_policy_digests_require_external_launch_receipt",
        "candidate_git_identity_requires_external_verifier",
    )
    return CodexNormalization(
        provider="openai-codex",
        adapter="codex-cli-jsonl",
        adapter_version=adapter_version,
        session_id=session_id,
        raw_event_sha256=_sha256(raw),
        raw_event_count=len(parsed),
        terminal_state=terminal_state,
        requested_model=requested_model,
        resolved_model=None,
        requested_reasoning_effort=requested_reasoning_effort,
        resolved_reasoning_effort=None,
        identity_verification="launch_requested_only" if requested_model else "unobserved",
        promotion_identity_eligible=False,
        usage=usage,
        events=tuple(normalized),
        limitations=limitations,
    )
