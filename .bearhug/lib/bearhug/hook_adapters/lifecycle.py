"""Strict provider adapters for reviewed Claude/Codex non-tool lifecycle families."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bearhug.hook_adapters._common import (
    AdapterCustody,
    HookAdapterError,
    bounded_text,
    canonical_json,
    identifier,
    parse_native_json,
    require_fields,
    validate_repository,
)
from bearhug.normalized_hooks import build_normalized_hook_event

CLAUDE_PROVIDER = "anthropic-claude"
CODEX_PROVIDER = "openai-codex"
CLAUDE_LIFECYCLE_VERSION = "2.1.260"
CODEX_LIFECYCLE_VERSION = "0.153.0-alpha.5"

_PROVIDER_CONFIG = {
    CLAUDE_PROVIDER: ("claude-command-hook", CLAUDE_LIFECYCLE_VERSION),
    CODEX_PROVIDER: ("codex-command-hook", CODEX_LIFECYCLE_VERSION),
}
_EVENTS = {
    "SessionStart": ("session-start", "session"),
    "UserPromptSubmit": ("user-prompt-submit", "turn"),
    "Stop": ("completion-request", "turn"),
    "SessionEnd": ("session-end", "session"),
    "PreCompact": ("pre-compact", "turn"),
    "PostCompact": ("post-compact", "turn"),
    "SubagentStart": ("subagent-start", "turn"),
    "SubagentStop": ("subagent-stop", "turn"),
    "Interrupt": ("interrupt", "turn"),
}
_CODEX_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
)
_CLAUDE_PERMISSION_MODES = _CODEX_PERMISSION_MODES | {"auto"}
_CODEX_BASE = frozenset({"session_id", "transcript_path", "cwd", "hook_event_name"})
_CLAUDE_BASE = _CODEX_BASE
_CLAUDE_COMMON_OPTIONAL = frozenset(
    {"prompt_id", "permission_mode", "agent_id", "agent_type", "effort"}
)
_CLAUDE_RESUME_OPTIONAL = frozenset(
    {
        "seconds_since_last_response",
        "context_tokens",
        "prompt_cache_likely_expired",
        "estimated_cache_write_usd",
    }
)
_MAX_BACKGROUND_TASKS = 256
_MAX_SESSION_CRONS = 256


@dataclass(frozen=True, slots=True)
class AdaptedLifecycleHook:
    """One content-minimized lifecycle event plus exact native custody."""

    event: dict[str, Any]
    custody: AdapterCustody


def _shape(provider: str, native_event: str) -> tuple[frozenset[str], frozenset[str]]:
    if provider == CLAUDE_PROVIDER:
        if native_event == "SessionStart":
            return _CLAUDE_BASE | {"source"}, _CLAUDE_COMMON_OPTIONAL | {
                "model",
                "session_title",
            } | _CLAUDE_RESUME_OPTIONAL
        if native_event == "UserPromptSubmit":
            return _CLAUDE_BASE | {"prompt"}, _CLAUDE_COMMON_OPTIONAL
        if native_event == "Stop":
            return _CLAUDE_BASE | {"stop_hook_active"}, _CLAUDE_COMMON_OPTIONAL | {
                "last_assistant_message",
                "background_tasks",
                "session_crons",
            }
        if native_event == "SessionEnd":
            return _CLAUDE_BASE | {"reason"}, _CLAUDE_COMMON_OPTIONAL
        if native_event == "PreCompact":
            return _CLAUDE_BASE | {"trigger", "custom_instructions"}, _CLAUDE_COMMON_OPTIONAL
        if native_event == "PostCompact":
            return _CLAUDE_BASE | {"trigger", "compact_summary"}, _CLAUDE_COMMON_OPTIONAL
        if native_event == "SubagentStart":
            return (
                _CLAUDE_BASE | {"agent_id", "agent_type"},
                _CLAUDE_COMMON_OPTIONAL - {"agent_id", "agent_type"},
            )
        if native_event == "SubagentStop":
            return (
                _CLAUDE_BASE
                | {
                    "agent_id",
                    "agent_type",
                    "agent_transcript_path",
                    "stop_hook_active",
                },
                _CLAUDE_COMMON_OPTIONAL
                - {"agent_id", "agent_type"}
                | {"last_assistant_message", "background_tasks", "session_crons"},
            )
        if native_event == "Interrupt":
            raise HookAdapterError("Claude has no reviewed Interrupt command-hook contract")
        raise HookAdapterError("unsupported lifecycle event")

    base = _CODEX_BASE if provider == CODEX_PROVIDER else _CLAUDE_BASE
    if native_event == "SessionEnd":
        return base | {"reason"}, frozenset()
    common = {"model", "permission_mode"}
    optional: set[str] = set()
    if native_event == "SessionStart":
        return base | common | {"source"}, frozenset(optional)
    if native_event == "UserPromptSubmit":
        turn = {"turn_id"} if provider == CODEX_PROVIDER else set()
        event_optional = optional | {"agent_id", "agent_type"}
        return base | common | turn | {"prompt"}, frozenset(event_optional)
    if native_event == "Stop":
        turn = {"turn_id"} if provider == CODEX_PROVIDER else set()
        return (
            base | common | turn | {"stop_hook_active", "last_assistant_message"},
            frozenset(optional),
        )
    if native_event in {"PreCompact", "PostCompact"}:
        turn = {"turn_id", "model"}
        event_optional = optional | {"agent_id", "agent_type"}
        return base | turn | {"trigger"}, frozenset(event_optional)
    if native_event == "SubagentStart":
        turn = {"turn_id", "model", "permission_mode"}
        return base | turn | {"agent_id", "agent_type"}, frozenset({"prompt_id", "effort"})
    if native_event == "SubagentStop":
        turn = {"turn_id", "model", "permission_mode"}
        return (
            base
            | turn
            | {
                "agent_id",
                "agent_type",
                "agent_transcript_path",
                "stop_hook_active",
                "last_assistant_message",
            },
            frozenset(),
        )
    if native_event == "Interrupt":
        return base | {"turn_id", "model", "permission_mode"}, frozenset()
    raise HookAdapterError("unsupported lifecycle event")


def _nullable_text(value: Any, *, where: str, maximum: int) -> None:
    if value is not None:
        bounded_text(value, where=where, maximum=maximum, allow_empty=True)


def _bounded_prose(value: Any, *, where: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise HookAdapterError(f"{where} must be a string")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HookAdapterError(f"{where} is not valid UTF-8") from exc
    if len(raw) > maximum or "\x00" in value or "\r" in value:
        raise HookAdapterError(f"{where} exceeds its byte bound or contains forbidden controls")


def _nonnegative_number(value: Any, *, where: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HookAdapterError(f"{where} must be a non-negative finite number")
    if not math.isfinite(value) or value < 0:
        raise HookAdapterError(f"{where} must be a non-negative finite number")
    return value


def _bounded_runtime_text(value: Any, *, where: str, maximum: int = 4096) -> None:
    if not isinstance(value, str):
        raise HookAdapterError(f"{where} must be a string")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HookAdapterError(f"{where} is not valid UTF-8") from exc
    if len(raw) > maximum or "\x00" in value or "\r" in value:
        raise HookAdapterError(f"{where} exceeds its byte bound or contains forbidden controls")


def _validate_background_tasks(value: Any) -> int:
    if not isinstance(value, list) or len(value) > _MAX_BACKGROUND_TASKS:
        raise HookAdapterError("background_tasks must be a bounded array")
    required = frozenset({"id", "type", "status", "description"})
    optional = frozenset({"command", "agent_type", "server", "tool", "name"})
    for index, candidate in enumerate(value):
        item = require_fields(
            candidate,
            required=required,
            optional=optional,
            where=f"background_tasks[{index}]",
        )
        for name in ("id", "type", "status"):
            identifier(item[name], where=f"background_tasks[{index}].{name}", maximum=1024)
        _bounded_runtime_text(
            item["description"], where=f"background_tasks[{index}].description"
        )
        for name in optional:
            if name in item:
                _bounded_runtime_text(item[name], where=f"background_tasks[{index}].{name}")
    return len(value)


def _validate_session_crons(value: Any) -> int:
    if not isinstance(value, list) or len(value) > _MAX_SESSION_CRONS:
        raise HookAdapterError("session_crons must be a bounded array")
    required = frozenset({"id", "schedule", "recurring", "prompt"})
    for index, candidate in enumerate(value):
        item = require_fields(
            candidate, required=required, where=f"session_crons[{index}]"
        )
        identifier(item["id"], where=f"session_crons[{index}].id", maximum=1024)
        _bounded_runtime_text(item["schedule"], where=f"session_crons[{index}].schedule")
        if type(item["recurring"]) is not bool:
            raise HookAdapterError(f"session_crons[{index}].recurring must be boolean")
        _bounded_runtime_text(item["prompt"], where=f"session_crons[{index}].prompt")
    return len(value)


def _validate_common(
    native: dict[str, Any],
    *,
    provider: str,
    expected_session_id: str,
    expected_cwd: str,
) -> None:
    if identifier(native["session_id"], where="session_id") != identifier(
        expected_session_id, where="expected_session_id"
    ):
        raise HookAdapterError("session_id does not match invocation custody")
    cwd = bounded_text(native["cwd"], where="cwd", maximum=4096)
    if cwd != bounded_text(expected_cwd, where="expected_cwd", maximum=4096):
        raise HookAdapterError("cwd does not match launcher custody")
    if provider == CLAUDE_PROVIDER:
        bounded_text(native["transcript_path"], where="transcript_path", maximum=4096)
    else:
        _nullable_text(native["transcript_path"], where="transcript_path", maximum=4096)
    permission_modes = (
        _CLAUDE_PERMISSION_MODES if provider == CLAUDE_PROVIDER else _CODEX_PERMISSION_MODES
    )
    if "permission_mode" in native and native["permission_mode"] not in permission_modes:
        raise HookAdapterError("permission_mode is unsupported")
    if "model" in native:
        identifier(native["model"], where="model")
    for name in ("agent_id", "agent_type"):
        if name in native:
            identifier(native[name], where=name)
    if "effort" in native:
        effort = require_fields(native["effort"], required=frozenset({"level"}), where="effort")
        if effort["level"] not in {"low", "medium", "high", "xhigh", "max"}:
            raise HookAdapterError("effort.level is unsupported")
    if provider == CODEX_PROVIDER and any(name in native for name in ("prompt_id", "effort")):
        raise HookAdapterError("Claude-only lifecycle fields are forbidden for Codex")


def adapt_lifecycle_hook(
    raw: bytes,
    *,
    provider: str,
    provider_version: str,
    occurred_at: datetime,
    expected_session_id: str,
    expected_cwd: str,
    repository: Mapping[str, Any],
    expected_turn_id: str | None = None,
) -> AdaptedLifecycleHook:
    """Normalize one reviewed non-tool lifecycle event for Claude or Codex.

    Claude's command-hook payload does not expose a stable turn id on all turn events.  Its owning
    provider runner must therefore supply the already-correlated turn id; Codex supplies and binds
    ``turn_id`` directly in the native payload.
    """

    if provider not in _PROVIDER_CONFIG:
        raise HookAdapterError("provider is unsupported")
    adapter, reviewed_version = _PROVIDER_CONFIG[provider]
    provider_version = identifier(provider_version, where="provider_version", maximum=128)
    if provider_version != reviewed_version:
        raise HookAdapterError("lifecycle adapter requires the exact reviewed provider version")
    preliminary = parse_native_json(raw, where="lifecycle hook input")
    native_event = preliminary.get("hook_event_name")
    if native_event not in _EVENTS:
        raise HookAdapterError("lifecycle hook event is unsupported")
    required, optional = _shape(provider, native_event)
    native = require_fields(
        preliminary, required=required, optional=optional, where="lifecycle hook input"
    )
    _validate_common(
        native,
        provider=provider,
        expected_session_id=expected_session_id,
        expected_cwd=expected_cwd,
    )
    semantic_event, scope = _EVENTS[native_event]
    turn_id: str | None = None
    if scope == "turn":
        if provider == CODEX_PROVIDER:
            turn_id = identifier(native["turn_id"], where="turn_id")
            if expected_turn_id is not None and turn_id != identifier(
                expected_turn_id, where="expected_turn_id"
            ):
                raise HookAdapterError("turn_id does not match invocation custody")
        else:
            if expected_turn_id is None:
                raise HookAdapterError(
                    "Claude turn event requires runner-correlated expected_turn_id"
                )
            turn_id = identifier(expected_turn_id, where="expected_turn_id")
            if (
                "prompt_id" in native
                and identifier(native["prompt_id"], where="prompt_id") != turn_id
            ):
                raise HookAdapterError("prompt_id does not match invocation custody")
    extension: dict[str, Any] = {
        "agent_scoped": "agent_id" in native,
        "native_event": native_event,
    }
    if native_event == "SessionStart":
        allowed_sources = {"startup", "resume", "clear", "compact", "fork"}
        if native["source"] not in allowed_sources:
            raise HookAdapterError("SessionStart source is unsupported")
        extension["source"] = native["source"]
        if "session_title" in native:
            _bounded_runtime_text(native["session_title"], where="session_title")
        for name in ("seconds_since_last_response", "context_tokens", "estimated_cache_write_usd"):
            if name in native:
                _nonnegative_number(native[name], where=name)
        if "prompt_cache_likely_expired" in native and type(
            native["prompt_cache_likely_expired"]
        ) is not bool:
            raise HookAdapterError("prompt_cache_likely_expired must be boolean")
        extension["resume_cost_present"] = any(
            name in native for name in _CLAUDE_RESUME_OPTIONAL
        )
    elif native_event == "SessionEnd":
        if provider == CODEX_PROVIDER and native["reason"] != "other":
            raise HookAdapterError("Codex SessionEnd reason is unsupported")
        identifier(native["reason"], where="reason")
        extension["reason"] = native["reason"]
    elif native_event == "UserPromptSubmit":
        _bounded_prose(native["prompt"], where="prompt", maximum=32 * 1024)
        extension["prompt_present"] = True
    elif native_event in {"Stop", "SubagentStop"}:
        if type(native["stop_hook_active"]) is not bool:
            raise HookAdapterError("stop_hook_active must be boolean")
        if "last_assistant_message" in native and native["last_assistant_message"] is not None:
            _bounded_prose(
                native["last_assistant_message"],
                where="last_assistant_message",
                maximum=32 * 1024,
            )
        extension["stop_hook_active"] = native["stop_hook_active"]
        extension["last_assistant_message_present"] = (
            native.get("last_assistant_message") is not None
        )
        if "background_tasks" in native:
            extension["background_task_count"] = _validate_background_tasks(
                native["background_tasks"]
            )
        if "session_crons" in native:
            extension["session_cron_count"] = _validate_session_crons(native["session_crons"])
        if native_event == "SubagentStop":
            if provider == CLAUDE_PROVIDER:
                bounded_text(
                    native["agent_transcript_path"],
                    where="agent_transcript_path",
                    maximum=4096,
                )
            else:
                _nullable_text(
                    native["agent_transcript_path"],
                    where="agent_transcript_path",
                    maximum=4096,
                )
    elif native_event in {"PreCompact", "PostCompact"}:
        if native["trigger"] not in {"manual", "auto"}:
            raise HookAdapterError("compaction trigger is unsupported")
        extension["trigger"] = native["trigger"]
        if (
            provider == CLAUDE_PROVIDER
            and native_event == "PreCompact"
            and native["custom_instructions"] is not None
        ):
            _bounded_prose(
                native["custom_instructions"], where="custom_instructions", maximum=32 * 1024
            )
        if provider == CLAUDE_PROVIDER and native_event == "PreCompact":
            extension["custom_instructions_present"] = bool(native["custom_instructions"])
        elif provider == CLAUDE_PROVIDER:
            _bounded_prose(native["compact_summary"], where="compact_summary", maximum=48 * 1024)
            summary = native["compact_summary"].encode("utf-8")
            extension["compact_summary_byte_count"] = len(summary)
            extension["compact_summary_sha256"] = hashlib.sha256(summary).hexdigest()
    elif native_event == "SubagentStart":
        extension["subagent_started"] = True
    elif native_event == "Interrupt":
        extension["interrupted"] = True
    extension_bytes = canonical_json(extension)
    event = build_normalized_hook_event(
        occurred_at=occurred_at,
        semantic_event=semantic_event,
        scope=scope,
        provider=provider,
        adapter=adapter,
        adapter_version=provider_version,
        native_event=native_event,
        evidence_kind="native-hook",
        native_input=raw,
        session_id=native["session_id"],
        thread_id=native["session_id"] if provider == CODEX_PROVIDER else None,
        turn_id=turn_id,
        tool_call_id=None,
        repository=validate_repository(repository),
        tool=None,
        provider_extension=extension_bytes,
    )
    return AdaptedLifecycleHook(
        event=event,
        custody=AdapterCustody(
            provider=provider,
            native_event=native_event,
            raw_native_input=raw,
            raw_provider_extension=extension_bytes,
        ),
    )


__all__ = [
    "AdaptedLifecycleHook",
    "CLAUDE_LIFECYCLE_VERSION",
    "CODEX_LIFECYCLE_VERSION",
    "adapt_lifecycle_hook",
]
