"""Strict tool-use adapters, including bounded Codex ``apply_patch`` fan-out.

Only provider-observed effects enter normalized events.  Patch bodies and tool responses remain in
transient custody; normalized records retain repository-relative effect paths and content digests.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
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
    verify_adapter_custody,
)
from bearhug.normalized_hooks import (
    build_normalized_hook_event,
    validate_normalized_hook_event,
    validate_normalized_hook_result,
)

CODEX_PROVIDER = "openai-codex"
CODEX_TOOL_ADAPTER = "codex-command-hook"
CODEX_TOOL_PROVIDER_VERSION = "0.153.0-alpha.5"

_COMMON_REQUIRED = frozenset(
    {
        "session_id",
        "transcript_path",
        "cwd",
        "hook_event_name",
        "model",
        "permission_mode",
        "turn_id",
        "tool_name",
        "tool_use_id",
        "tool_input",
    }
)
_COMMON_OPTIONAL = frozenset({"agent_id", "agent_type"})
_PERMISSION_MODES = frozenset({"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"})
_HEADER = "Success. Updated the following files:"
_FAILURE_PREFIXES = (
    "Error: ",
    "Failed to apply patch: ",
    "Failed to find expected lines in ",
    "Failed to read file to update ",
)
_MAX_PATCH_EFFECTS = 256
_MAX_PATCH_PATH_BYTES = 1024


@dataclass(frozen=True, slots=True)
class PatchEffect:
    kind: str
    path: str
    target_path: str | None
    summary_kind: str
    summary_path: str


@dataclass(frozen=True, slots=True)
class AdaptedCodexApplyPatch:
    """One normalized patch event plus exact transient provider bytes."""

    event: dict[str, Any]
    effects: tuple[PatchEffect, ...]
    custody: AdapterCustody


def _multiline(value: Any, *, where: str, maximum: int = 60 * 1024) -> str:
    if not isinstance(value, str) or not value:
        raise HookAdapterError(f"{where} must be a non-empty string")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HookAdapterError(f"{where} is not valid UTF-8") from exc
    if len(raw) > maximum or "\x00" in value or "\r" in value:
        raise HookAdapterError(f"{where} exceeds its byte bound or contains forbidden controls")
    return value


def _absolute_directory(value: str, *, where: str) -> Path:
    text = bounded_text(value, where=where, maximum=4096)
    path = Path(text)
    if not path.is_absolute() or path != Path(os.path.normpath(text)):
        raise HookAdapterError(f"{where} must be a canonical absolute path")
    try:
        mode = path.lstat().st_mode
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise HookAdapterError(f"{where} is not an accessible directory") from exc
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode) or resolved != path:
        raise HookAdapterError(f"{where} must be a physical directory, not a symlink")
    return path


def _safe_effect_path(
    raw_path: str,
    *,
    cwd: Path,
    worktree: Path,
    where: str,
) -> str:
    value = bounded_text(raw_path, where=where, maximum=_MAX_PATCH_PATH_BYTES)
    if value != value.strip() or "\\" in value or Path(value).is_absolute():
        raise HookAdapterError(f"{where} must be a canonical relative POSIX path")
    pure = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in pure.parts) or pure.as_posix() != value:
        raise HookAdapterError(f"{where} contains traversal or a non-canonical segment")
    candidate = cwd.joinpath(*pure.parts)
    try:
        relative = candidate.relative_to(worktree)
    except ValueError as exc:
        raise HookAdapterError(f"{where} escapes the installed worktree") from exc
    current = worktree
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HookAdapterError(f"{where} cannot be checked without following links") from exc
        if stat.S_ISLNK(mode):
            raise HookAdapterError(f"{where} traverses a symlink")
    normalized = relative.as_posix()
    if not normalized or normalized == ".":
        raise HookAdapterError(f"{where} must name a file below the worktree")
    return normalized


def _operation_header(line: str) -> tuple[str, str] | None:
    for marker, kind in (
        ("*** Add File: ", "create"),
        ("*** Delete File: ", "delete"),
        ("*** Update File: ", "modify"),
    ):
        if line.startswith(marker):
            return kind, line[len(marker) :]
    return None


def _parse_patch(command: str, *, cwd: Path, worktree: Path) -> tuple[PatchEffect, ...]:
    command = _multiline(command, where="tool_input.command")
    lines = command.split("\n")
    if lines[-1] == "":
        lines.pop()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise HookAdapterError("apply_patch command must have exact begin and end markers")
    effects: list[PatchEffect] = []
    seen_sources: set[str] = set()
    seen_destinations: set[str] = set()
    index = 1
    while index < len(lines) - 1:
        parsed = _operation_header(lines[index])
        if parsed is None:
            raise HookAdapterError(f"apply_patch has an invalid operation at line {index + 1}")
        kind, raw_path = parsed
        path = _safe_effect_path(
            raw_path, cwd=cwd, worktree=worktree, where=f"patch line {index + 1} path"
        )
        if path in seen_sources:
            raise HookAdapterError("apply_patch repeats a source path")
        seen_sources.add(path)
        index += 1
        target: str | None = None
        body_lines = 0
        change_lines = 0
        if kind == "modify" and index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
            target = _safe_effect_path(
                lines[index][len("*** Move to: ") :],
                cwd=cwd,
                worktree=worktree,
                where=f"patch line {index + 1} move path",
            )
            if target == path or target in seen_destinations or target in seen_sources:
                raise HookAdapterError("apply_patch move target is duplicate or equals its source")
            seen_destinations.add(target)
            index += 1
        while index < len(lines) - 1 and _operation_header(lines[index]) is None:
            line = lines[index]
            if line.startswith("*** ") and line != "*** End of File":
                raise HookAdapterError(f"apply_patch has an unknown marker at line {index + 1}")
            if kind == "add":
                if not line.startswith("+"):
                    raise HookAdapterError("apply_patch add body must contain only '+' lines")
            elif kind == "delete":
                raise HookAdapterError("apply_patch delete operation cannot have a body")
            elif not (line in {"@@", "*** End of File"} or line.startswith(("@@ ", "+", "-", " "))):
                raise HookAdapterError("apply_patch update body has an invalid hunk line")
            if kind == "modify" and line.startswith(("+", "-", " ")):
                change_lines += 1
            body_lines += 1
            index += 1
        if kind == "add" and body_lines == 0:
            raise HookAdapterError("apply_patch add operation requires content lines")
        if kind == "modify" and body_lines == 0 and target is None:
            raise HookAdapterError("apply_patch update operation is empty")
        if kind == "modify" and change_lines == 0 and target is None:
            raise HookAdapterError("apply_patch update operation has no change lines")
        if target is None:
            summary_kind = {"create": "A", "modify": "M", "delete": "D"}[kind]
            effects.append(PatchEffect(kind, path, None, summary_kind, path))
        else:
            # Codex 0.153.0-alpha.5 reports the destination, not the source, for a move.
            effects.append(PatchEffect("rename", path, target, "M", target))
        if len(effects) > _MAX_PATCH_EFFECTS:
            raise HookAdapterError("apply_patch exceeds the effect count bound")
    if not effects:
        raise HookAdapterError("apply_patch must contain at least one file operation")
    occupied = seen_sources | seen_destinations
    if len(occupied) != len(seen_sources) + len(seen_destinations):
        raise HookAdapterError("apply_patch paths overlap ambiguously")
    return tuple(effects)


def _parse_observed_summary(
    value: Any,
    *,
    effects: tuple[PatchEffect, ...],
    cwd: Path,
    worktree: Path,
) -> tuple[set[tuple[str, str]], bool]:
    response = _multiline(value, where="tool_response")
    lines = response.splitlines()
    if lines and lines[0].startswith(_FAILURE_PREFIXES):
        return set(), True
    if not lines or lines[0] != _HEADER or len(lines) == 1:
        raise HookAdapterError("tool_response is not a recognized apply_patch outcome")
    observed: set[tuple[str, str]] = set()
    expected = {(effect.summary_kind, effect.summary_path) for effect in effects}
    for index, line in enumerate(lines[1:], start=2):
        if line.startswith(_FAILURE_PREFIXES):
            return observed, True
        if len(line) < 3 or line[0] not in "AMD" or line[1] != " ":
            raise HookAdapterError(f"tool_response summary line {index} is malformed")
        path = _safe_effect_path(
            line[2:], cwd=cwd, worktree=worktree, where=f"tool_response line {index} path"
        )
        item = (line[0], path)
        if item not in expected or item in observed:
            raise HookAdapterError("tool_response reports an unexpected or duplicate effect")
        observed.add(item)
    return observed, False


def _parse_native(
    raw: bytes,
    *,
    expected_session_id: str,
    expected_cwd: str,
    expected_worktree: str,
    expected_turn_id: str,
    expected_tool_use_id: str,
) -> tuple[dict[str, Any], Path, Path, tuple[PatchEffect, ...]]:
    preliminary = parse_native_json(raw, where="Codex tool hook input")
    hook_event = preliminary.get("hook_event_name")
    if hook_event not in {"PreToolUse", "PostToolUse"}:
        raise HookAdapterError("hook_event_name must be PreToolUse or PostToolUse")
    required = _COMMON_REQUIRED | ({"tool_response"} if hook_event == "PostToolUse" else set())
    native = require_fields(
        preliminary,
        required=frozenset(required),
        optional=_COMMON_OPTIONAL,
        where="Codex tool hook input",
    )
    if identifier(native["session_id"], where="session_id") != identifier(
        expected_session_id, where="expected_session_id"
    ):
        raise HookAdapterError("session_id does not match invocation custody")
    cwd = _absolute_directory(native["cwd"], where="cwd")
    expected = _absolute_directory(expected_cwd, where="expected_cwd")
    worktree = _absolute_directory(expected_worktree, where="expected_worktree")
    if cwd != expected:
        raise HookAdapterError("cwd does not match launcher custody")
    try:
        cwd.relative_to(worktree)
    except ValueError as exc:
        raise HookAdapterError("cwd is outside the installed worktree") from exc
    if native["transcript_path"] is not None:
        bounded_text(native["transcript_path"], where="transcript_path", maximum=4096)
    identifier(native["model"], where="model")
    if native["permission_mode"] not in _PERMISSION_MODES:
        raise HookAdapterError("permission_mode is unsupported")
    turn_id = identifier(native["turn_id"], where="turn_id")
    if turn_id != identifier(expected_turn_id, where="expected_turn_id"):
        raise HookAdapterError("turn_id does not match invocation custody")
    tool_use_id = identifier(native["tool_use_id"], where="tool_use_id")
    if tool_use_id != identifier(expected_tool_use_id, where="expected_tool_use_id"):
        raise HookAdapterError("tool_use_id does not match invocation custody")
    if native["tool_name"] != "apply_patch":
        raise HookAdapterError("tool_name must be apply_patch")
    for name in ("agent_id", "agent_type"):
        if name in native:
            identifier(native[name], where=name)
    tool_input = require_fields(
        native["tool_input"], required=frozenset({"command"}), where="tool_input"
    )
    effects = _parse_patch(tool_input["command"], cwd=cwd, worktree=worktree)
    return native, cwd, worktree, effects


def adapt_codex_apply_patch(
    raw: bytes,
    *,
    occurred_at: datetime,
    provider_version: str,
    expected_session_id: str,
    expected_cwd: str,
    expected_worktree: str,
    expected_turn_id: str,
    expected_tool_use_id: str,
    repository: Mapping[str, Any],
) -> AdaptedCodexApplyPatch:
    """Normalize one exact-version Codex apply_patch PreToolUse or PostToolUse event."""

    provider_version = identifier(provider_version, where="provider_version", maximum=128)
    if provider_version != CODEX_TOOL_PROVIDER_VERSION:
        raise HookAdapterError("Codex adapter requires the exact reviewed provider version")
    native, cwd, worktree, effects = _parse_native(
        raw,
        expected_session_id=expected_session_id,
        expected_cwd=expected_cwd,
        expected_worktree=expected_worktree,
        expected_turn_id=expected_turn_id,
        expected_tool_use_id=expected_tool_use_id,
    )
    is_post = native["hook_event_name"] == "PostToolUse"
    observed: set[tuple[str, str]] = set()
    response_failed = False
    if is_post:
        observed, response_failed = _parse_observed_summary(
            native["tool_response"], effects=effects, cwd=cwd, worktree=worktree
        )
    effect_records: list[dict[str, Any]] = []
    statuses: list[str] = []
    for effect in effects:
        status = (
            "succeeded" if (effect.summary_kind, effect.summary_path) in observed else "unknown"
        )
        if not is_post:
            status = "intended"
        statuses.append(status)
        effect_records.append(
            {
                "kind": effect.kind,
                "path": effect.path,
                "target_path": effect.target_path,
                "status": status,
            }
        )
    if not is_post:
        aggregate_status = "intended"
    elif response_failed and "succeeded" in statuses:
        aggregate_status = "partial"
    elif response_failed:
        aggregate_status = "failed"
    elif statuses and all(status == "succeeded" for status in statuses):
        aggregate_status = "succeeded"
    elif "succeeded" in statuses:
        aggregate_status = "partial"
    else:
        aggregate_status = "failed"
    extension = canonical_json(
        {
            "agent_scoped": "agent_id" in native,
            "effect_count": len(effects),
            "observed_effect_count": len(observed),
            "response_failed": response_failed,
            "response_present": is_post,
        }
    )
    event = build_normalized_hook_event(
        occurred_at=occurred_at,
        semantic_event="post-tool-use" if is_post else "pre-tool-use",
        scope="turn",
        provider=CODEX_PROVIDER,
        adapter=CODEX_TOOL_ADAPTER,
        adapter_version=provider_version,
        native_event=native["hook_event_name"],
        evidence_kind="native-hook",
        native_input=raw,
        session_id=native["session_id"],
        thread_id=native["session_id"],
        turn_id=native["turn_id"],
        tool_call_id=native["tool_use_id"],
        repository=validate_repository(repository),
        tool={
            "family": "filesystem-write",
            "operation": "apply-patch",
            "status": aggregate_status,
            "effects": effect_records,
        },
        provider_extension=extension,
    )
    return AdaptedCodexApplyPatch(
        event=event,
        effects=effects,
        custody=AdapterCustody(
            provider=CODEX_PROVIDER,
            native_event=native["hook_event_name"],
            raw_native_input=raw,
            raw_provider_extension=extension,
        ),
    )


def _blocking_reason(result: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for item in result["remediation"]:
        text = f"{item['code']}: {item['message']}"
        if item["paths"]:
            text += f" ({', '.join(item['paths'])})"
        parts.append(text)
    reason = "; ".join(parts)
    if not reason or len(reason.encode("utf-8")) > 8192:
        raise HookAdapterError("aggregate Codex hook reason is empty or exceeds 8192 bytes")
    return reason


def render_codex_apply_patch_result(
    result: Mapping[str, Any], *, adapted: AdaptedCodexApplyPatch
) -> bytes:
    """Render a normalized patch decision using the current Codex command-hook contract."""

    if not isinstance(adapted, AdaptedCodexApplyPatch):
        raise HookAdapterError("adapted Codex patch has the wrong type")
    event = validate_normalized_hook_event(adapted.event)
    verify_adapter_custody(adapted.custody, event)
    result = validate_normalized_hook_result(result)
    if result["event_id"] != event["event_id"]:
        raise HookAdapterError("result does not belong to the supplied patch event")
    if (
        event["source"]["provider"] != CODEX_PROVIDER
        or event["source"]["adapter"] != CODEX_TOOL_ADAPTER
        or event["source"]["adapter_version"] != CODEX_TOOL_PROVIDER_VERSION
        or event["tool"]["family"] != "filesystem-write"
        or event["tool"]["operation"] != "apply-patch"
    ):
        raise HookAdapterError("event is not a reviewed Codex apply_patch event")
    if result["decision"] != "block":
        return b"{}"
    reason = _blocking_reason(result)
    if event["semantic_event"] == "pre-tool-use":
        return canonical_json(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    if event["semantic_event"] == "post-tool-use":
        return canonical_json({"decision": "block", "reason": reason})
    raise HookAdapterError("Codex patch event has an unsupported semantic event")


__all__ = [
    "AdaptedCodexApplyPatch",
    "CODEX_TOOL_ADAPTER",
    "CODEX_TOOL_PROVIDER_VERSION",
    "PatchEffect",
    "adapt_codex_apply_patch",
    "render_codex_apply_patch_result",
]
