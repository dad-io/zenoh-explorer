"""Normalize Pi 0.84.2's documented ``--mode json`` event stream.

This first adapter slice is transport evidence only.  It deliberately does not register Pi as a
qualified Bear Hug provider or claim effective resource, trust, sandbox, or runtime-hook parity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SUPPORTED_PI_VERSION = "0.84.2"
_THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
_TERMINAL_STOP_REASONS = frozenset({"stop", "length", "toolUse", "error", "aborted"})


class PiEventError(ValueError):
    """The Pi stream is malformed or has an ambiguous lifecycle."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PiEventError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _nonempty(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{context} must be a non-empty string without NUL bytes")
    return value


def _absolute_path(value: Any, context: str) -> str:
    value = _nonempty(value, context)
    if not Path(value).is_absolute():
        raise ValueError(f"{context} must be absolute")
    return value


def _resource_paths(values: tuple[str, ...], context: str) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{context} must be a tuple")
    if len(values) != len(set(values)):
        raise ValueError(f"{context} contains duplicate paths")
    for index, value in enumerate(values):
        _absolute_path(value, f"{context}[{index}]")


@dataclass(frozen=True, slots=True)
class PiLaunchContract:
    """Immutable requested facts for one isolated Pi JSON-mode invocation.

    Prompt bytes are supplied on stdin.  ``agent_dir`` is carried separately because Pi controls
    it through ``PI_CODING_AGENT_DIR`` rather than a command-line flag.
    """

    cwd: str
    agent_dir: str
    session_dir: str
    prompt_sha256: str
    adapter_version: str
    requested_provider: str
    requested_model: str
    requested_thinking_level: str
    pi_version: str = SUPPORTED_PI_VERSION
    executable: str = "pi"
    ephemeral: bool = True
    extension_paths: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    prompt_template_paths: tuple[str, ...] = ()
    theme_paths: tuple[str, ...] = ()
    load_context_files: bool = False

    def __post_init__(self) -> None:
        _absolute_path(self.cwd, "cwd")
        _absolute_path(self.agent_dir, "agent_dir")
        _absolute_path(self.session_dir, "session_dir")
        _nonempty(self.adapter_version, "adapter_version")
        _nonempty(self.requested_provider, "requested_provider")
        _nonempty(self.requested_model, "requested_model")
        _nonempty(self.executable, "executable")
        if self.pi_version != SUPPORTED_PI_VERSION:
            raise ValueError(f"Pi adapter supports exactly version {SUPPORTED_PI_VERSION}")
        if self.requested_thinking_level not in _THINKING_LEVELS:
            raise ValueError("requested_thinking_level is unsupported")
        if len(self.prompt_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.prompt_sha256
        ):
            raise ValueError("prompt_sha256 must be lowercase SHA-256")
        if type(self.ephemeral) is not bool or type(self.load_context_files) is not bool:
            raise ValueError("ephemeral and load_context_files must be booleans")
        _resource_paths(self.extension_paths, "extension_paths")
        _resource_paths(self.skill_paths, "skill_paths")
        _resource_paths(self.prompt_template_paths, "prompt_template_paths")
        _resource_paths(self.theme_paths, "theme_paths")


def build_pi_command(contract: PiLaunchContract) -> tuple[str, ...]:
    """Return shell-free argv for Pi JSON mode; prompt content stays on stdin."""

    command = [
        contract.executable,
        "--mode",
        "json",
        "-p",
        "--offline",
        "--provider",
        contract.requested_provider,
        "--model",
        contract.requested_model,
        "--thinking",
        contract.requested_thinking_level,
        "--session-dir",
        contract.session_dir,
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
    ]
    for path in contract.extension_paths:
        command.extend(("--extension", path))
    for path in contract.skill_paths:
        command.extend(("--skill", path))
    for path in contract.prompt_template_paths:
        command.extend(("--prompt-template", path))
    for path in contract.theme_paths:
        command.extend(("--theme", path))
    if not contract.load_context_files:
        command.append("--no-context-files")
    if contract.ephemeral:
        command.append("--no-session")
    return tuple(command)


def build_pi_environment(contract: PiLaunchContract) -> dict[str, str]:
    """Return the required environment overrides for a controlled, offline Pi launch."""

    return {
        "PI_CODING_AGENT_DIR": contract.agent_dir,
        "PI_CODING_AGENT_SESSION_DIR": contract.session_dir,
        "PI_OFFLINE": "1",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
    }


@dataclass(frozen=True, slots=True)
class NormalizedPiEvent:
    sequence: int
    kind: str
    raw_line_sha256: str
    message_role: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class PiNormalization:
    """Facts derivable from a single complete Pi JSON event stream."""

    provider: str
    adapter: str
    adapter_version: str
    pi_version: str
    session_id: str
    session_version: int
    session_cwd: str
    raw_event_sha256: str
    raw_event_count: int
    terminal_state: str
    requested_provider: str
    requested_model: str
    requested_thinking_level: str
    final_observed_provider: str
    final_observed_model: str
    observed_thinking_level: str | None
    model_verification: str
    thinking_level_verification: str
    agent_run_count: int
    turn_count: int
    tool_execution_count: int
    extension_error_count: int
    promotion_identity_eligible: bool
    events: tuple[NormalizedPiEvent, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _record_string(record: dict[str, Any], field: str, context: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise PiEventError(f"{context} has no non-empty {field}")
    return value


def _message(record: dict[str, Any], context: str) -> dict[str, Any]:
    value = record.get("message")
    if not isinstance(value, dict):
        raise PiEventError(f"{context} has no message object")
    return value


def _message_role(message: dict[str, Any], context: str) -> str:
    role = message.get("role")
    if not isinstance(role, str) or not role:
        raise PiEventError(f"{context} message has no role")
    return role


def _assistant_identity(message: dict[str, Any], context: str) -> tuple[str, str, str]:
    if _message_role(message, context) != "assistant":
        raise PiEventError(f"{context} message must be assistant")
    provider = _record_string(message, "provider", f"{context} assistant message")
    model = _record_string(message, "model", f"{context} assistant message")
    stop_reason = _record_string(message, "stopReason", f"{context} assistant message")
    if stop_reason not in _TERMINAL_STOP_REASONS:
        raise PiEventError(f"{context} assistant message has non-terminal stopReason")
    return provider, model, stop_reason


def normalize_pi_jsonl(
    raw: bytes,
    *,
    adapter_version: str,
    requested_provider: str,
    requested_model: str,
    requested_thinking_level: str,
    expected_cwd: str | None = None,
    pi_version: str = SUPPORTED_PI_VERSION,
) -> PiNormalization:
    """Normalize complete LF-framed Pi JSON while retaining unknown kinds by digest."""

    if pi_version != SUPPORTED_PI_VERSION:
        raise PiEventError(f"Pi adapter supports exactly version {SUPPORTED_PI_VERSION}")
    for value, context in (
        (adapter_version, "adapter_version"),
        (requested_provider, "requested_provider"),
        (requested_model, "requested_model"),
    ):
        if not isinstance(value, str) or not value:
            raise PiEventError(f"{context} must be a non-empty string")
    if requested_thinking_level not in _THINKING_LEVELS:
        raise PiEventError("requested_thinking_level is unsupported")
    if expected_cwd is not None and not Path(expected_cwd).is_absolute():
        raise PiEventError("expected_cwd must be absolute")
    if not raw or not raw.endswith(b"\n"):
        raise PiEventError("event stream must be non-empty LF-terminated JSONL")

    parsed: list[tuple[dict[str, Any], bytes]] = []
    for ordinal, line in enumerate(raw.split(b"\n")[:-1], start=1):
        if not line:
            raise PiEventError(f"blank JSONL record at line {ordinal}")
        if line.endswith(b"\r"):
            raise PiEventError(f"CRLF framing is not canonical at line {ordinal}")
        try:
            event = json.loads(line, object_pairs_hook=_closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError, PiEventError) as exc:
            raise PiEventError(f"invalid JSONL record at line {ordinal}: {exc}") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise PiEventError(f"line {ordinal} has no string event type")
        parsed.append((event, line))

    header = parsed[0][0]
    if header.get("type") != "session":
        raise PiEventError("first event must be a session header")
    session_id = _record_string(header, "id", "session header")
    session_cwd = _record_string(header, "cwd", "session header")
    if not Path(session_cwd).is_absolute():
        raise PiEventError("session header cwd must be absolute")
    if expected_cwd is not None and Path(session_cwd) != Path(expected_cwd):
        raise PiEventError("session header cwd does not match the launch contract")
    session_version = header.get("version")
    if type(session_version) is not int or session_version != 3:
        raise PiEventError("Pi 0.84.2 session header must have version 3")
    _record_string(header, "timestamp", "session header")

    agent_active = False
    pending_retry = False
    final_agent_end = False
    terminal_seen = False
    turn_active = False
    message_role: str | None = None
    active_tools: dict[str, str] = {}
    last_assistant: dict[str, Any] | None = None
    agent_runs = 0
    turns = 0
    tool_executions = 0
    extension_errors = 0
    normalized: list[NormalizedPiEvent] = []

    for sequence, (event, line) in enumerate(parsed):
        kind = event["type"]
        if terminal_seen:
            raise PiEventError("event appears after terminal agent_settled")
        if kind == "session" and sequence != 0:
            raise PiEventError("stream contains multiple session headers")

        event_role: str | None = None
        tool_call_id: str | None = None
        if kind == "agent_start":
            if agent_active or turn_active or message_role is not None or active_tools:
                raise PiEventError("agent_start overlaps active lifecycle state")
            if final_agent_end:
                raise PiEventError("agent_start follows a non-retrying agent_end")
            if agent_runs > 0 and not pending_retry:
                raise PiEventError("additional agent_start has no retry authority")
            agent_active = True
            pending_retry = False
            agent_runs += 1
        elif kind == "turn_start":
            if not agent_active or turn_active or message_role is not None or active_tools:
                raise PiEventError("turn_start is outside an idle active agent")
            turn_active = True
            turns += 1
        elif kind == "message_start":
            if not agent_active or message_role is not None:
                raise PiEventError("message_start is outside an idle active agent")
            message = _message(event, "message_start")
            event_role = _message_role(message, "message_start")
            message_role = event_role
        elif kind == "message_update":
            if message_role != "assistant":
                raise PiEventError("message_update has no active assistant message")
            update = event.get("assistantMessageEvent")
            if not isinstance(update, dict) or not isinstance(update.get("type"), str):
                raise PiEventError("message_update has no assistant delta type")
            event_role = "assistant"
        elif kind == "message_end":
            if message_role is None:
                raise PiEventError("message_end has no active message")
            message = _message(event, "message_end")
            event_role = _message_role(message, "message_end")
            if event_role != message_role:
                raise PiEventError("message_end role does not match message_start")
            if event_role == "assistant":
                _assistant_identity(message, "message_end")
                last_assistant = message
            message_role = None
        elif kind == "tool_execution_start":
            if not agent_active or not turn_active or message_role is not None:
                raise PiEventError("tool_execution_start is outside an active turn")
            tool_call_id = _record_string(event, "toolCallId", "tool_execution_start")
            tool_name = _record_string(event, "toolName", "tool_execution_start")
            if tool_call_id in active_tools:
                raise PiEventError("duplicate active tool call id")
            active_tools[tool_call_id] = tool_name
            tool_executions += 1
        elif kind == "tool_execution_update":
            tool_call_id = _record_string(event, "toolCallId", "tool_execution_update")
            tool_name = _record_string(event, "toolName", "tool_execution_update")
            if active_tools.get(tool_call_id) != tool_name:
                raise PiEventError("tool_execution_update has no matching active tool")
        elif kind == "tool_execution_end":
            tool_call_id = _record_string(event, "toolCallId", "tool_execution_end")
            tool_name = _record_string(event, "toolName", "tool_execution_end")
            if active_tools.get(tool_call_id) != tool_name:
                raise PiEventError("tool_execution_end has no matching active tool")
            is_error = event.get("isError")
            if type(is_error) is not bool:
                raise PiEventError("tool_execution_end.isError must be boolean")
            del active_tools[tool_call_id]
        elif kind == "turn_end":
            if not agent_active or not turn_active or message_role is not None or active_tools:
                raise PiEventError("turn_end does not close an idle active turn")
            message = _message(event, "turn_end")
            _assistant_identity(message, "turn_end")
            if last_assistant is None or message != last_assistant:
                raise PiEventError("turn_end message does not match finalized assistant message")
            if not isinstance(event.get("toolResults"), list):
                raise PiEventError("turn_end.toolResults must be an array")
            turn_active = False
        elif kind == "agent_end":
            if not agent_active or turn_active or message_role is not None or active_tools:
                raise PiEventError("agent_end does not close an idle active agent")
            if not isinstance(event.get("messages"), list):
                raise PiEventError("agent_end.messages must be an array")
            will_retry = event.get("willRetry")
            if type(will_retry) is not bool:
                raise PiEventError("agent_end.willRetry must be boolean")
            agent_active = False
            pending_retry = will_retry
            final_agent_end = not will_retry
        elif kind == "agent_settled":
            if (
                agent_active
                or turn_active
                or message_role is not None
                or active_tools
                or pending_retry
                or not final_agent_end
            ):
                raise PiEventError("agent_settled has incomplete lifecycle state")
            terminal_seen = True
        elif kind == "extension_error":
            extension_errors += 1

        normalized.append(
            NormalizedPiEvent(
                sequence=sequence,
                kind=kind,
                raw_line_sha256=_sha256(line),
                message_role=event_role,
                tool_call_id=tool_call_id,
            )
        )

    if not terminal_seen:
        raise PiEventError("stream has no terminal agent_settled event")
    if last_assistant is None:
        raise PiEventError("settled stream has no finalized assistant message")
    observed_provider, observed_model, stop_reason = _assistant_identity(last_assistant, "final")
    terminal_state = {
        "error": "failed",
        "aborted": "aborted",
    }.get(stop_reason, "completed")
    identity_matches = observed_provider == requested_provider and observed_model == requested_model
    limitations = (
        "configured_thinking_level_not_present_in_pi_json_stream",
        "effective_resources_and_project_trust_require_sdk_attestation",
        "final_provider_payload_requires_sdk_attestation",
        "pi_has_no_builtin_os_sandbox",
    )
    return PiNormalization(
        provider="pi",
        adapter="pi-cli-json",
        adapter_version=adapter_version,
        pi_version=pi_version,
        session_id=session_id,
        session_version=session_version,
        session_cwd=session_cwd,
        raw_event_sha256=_sha256(raw),
        raw_event_count=len(parsed),
        terminal_state=terminal_state,
        requested_provider=requested_provider,
        requested_model=requested_model,
        requested_thinking_level=requested_thinking_level,
        final_observed_provider=observed_provider,
        final_observed_model=observed_model,
        observed_thinking_level=None,
        model_verification=(
            "provider_observed_match" if identity_matches else "provider_observed_mismatch"
        ),
        thinking_level_verification="launch_requested_only",
        agent_run_count=agent_runs,
        turn_count=turns,
        tool_execution_count=tool_executions,
        extension_error_count=extension_errors,
        promotion_identity_eligible=False,
        events=tuple(normalized),
        limitations=limitations,
    )


__all__ = [
    "NormalizedPiEvent",
    "PiEventError",
    "PiLaunchContract",
    "PiNormalization",
    "SUPPORTED_PI_VERSION",
    "build_pi_command",
    "build_pi_environment",
    "normalize_pi_jsonl",
]
