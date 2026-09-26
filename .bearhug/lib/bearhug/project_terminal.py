"""Native project-terminal enrollment and hook response bridge.

This module is deliberately a small provider edge.  It does not schedule capsules, choose a
provider, or infer project authority.  An explicit enrollment adds one command hook to the native
project configuration and records the exact dispatcher command in a private project profile.  The
hook process forwards the provider's raw JSON unchanged to that dispatcher and translates the
dispatcher's typed decision back to the provider's documented command-hook response shape.

The bridge is useful only when a caller supplies a dispatcher that is itself qualified to run the
execution engine.  Installing this bridge never constitutes provider qualification, hook trust,
effective-configuration proof, or runtime attestation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProjectTerminalError(RuntimeError):
    """The project-terminal profile, native configuration, or hook exchange is invalid."""


class ProjectTerminalConfigConflict(ProjectTerminalError):
    """An enrollment would overwrite unknown or concurrently changed project configuration."""


_SCHEMA_VERSION = "1"
_RECORD_KIND = "project_terminal_profile"
_MANAGED_ID = "bearhug.project-terminal.v1"
_PROVIDERS = frozenset({"claude", "codex"})
_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse")
_SOURCES_BY_PROVIDER = {
    "claude": frozenset({"startup", "resume", "clear", "compact", "fork"}),
    "codex": frozenset({"startup", "resume", "clear", "compact"}),
}
_MAX_PROFILE_BYTES = 64 * 1024
_MAX_CONFIG_BYTES = 8 * 1024 * 1024
_MAX_HOOK_INPUT_BYTES = 16 * 1024 * 1024
_MAX_HOOK_OUTPUT_BYTES = 256 * 1024
_MAX_REASON_BYTES = 4096
_MAX_CONTEXT_BYTES = 64 * 1024
_PROFILE_RELATIVE = ".bearhug/project-terminal.json"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectTerminalError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_load(raw: bytes, *, label: str, maximum: int) -> Any:
    if len(raw) > maximum:
        raise ProjectTerminalError(f"{label} exceeds the byte limit")
    def reject_constant(value: str) -> None:
        raise ProjectTerminalError(f"non-finite JSON number {value}")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ProjectTerminalError) as exc:
        raise ProjectTerminalError(f"{label} is invalid JSON: {exc}") from exc


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProjectTerminalError(f"value is not canonical JSON: {exc}") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_string(value: Any, *, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProjectTerminalError(f"{label} must be a non-empty string without NUL")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProjectTerminalError(f"{label} must be valid UTF-8") from exc
    if size > maximum:
        raise ProjectTerminalError(f"{label} exceeds the byte limit")
    return value


def _canonical_root(value: Path | str) -> Path:
    if not isinstance(value, (Path, str)):
        raise ProjectTerminalError("path must be an explicit absolute path")
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        raise ProjectTerminalError("project_root must be an explicit absolute path")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise ProjectTerminalError(f"project_root is unavailable: {supplied}") from exc
    if supplied != resolved or not resolved.is_dir():
        raise ProjectTerminalError("project_root must be an existing physical directory")
    return resolved


def _relative_profile(root: Path, profile_path: Path | str | None) -> Path:
    if profile_path is None:
        return root / _PROFILE_RELATIVE
    supplied = Path(profile_path).expanduser()
    if not supplied.is_absolute():
        supplied = root / supplied
    path = supplied
    if not path.is_relative_to(root):
        raise ProjectTerminalError("profile path must be inside project_root")
    relative = path.relative_to(root)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ProjectTerminalError("profile path must be canonical and stay inside project_root")
    for index in range(1, len(relative.parts) + 1):
        candidate = root.joinpath(*relative.parts[:index])
        if candidate.is_symlink():
            raise ProjectTerminalError("profile path may not traverse a symlink")
    if supplied.exists() and supplied.resolve() != supplied:
        raise ProjectTerminalError("profile path may not be a symlink")
    return path


def _argv(value: Sequence[str], *, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise ProjectTerminalError(f"{label} must be a non-empty argv array")
    checked: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ProjectTerminalError(f"{label}[{index}] must be a non-empty string without NUL")
        if len(item.encode("utf-8")) > 64 * 1024:
            raise ProjectTerminalError(f"{label}[{index}] exceeds the byte limit")
        checked.append(item)
    return tuple(checked)


def _assert_no_symlinked_ancestors(path: Path) -> None:
    """Reject a path whose parent chain would redirect a project-owned write/read."""

    for directory in (path.parent, *path.parent.parents):
        try:
            if directory.is_symlink():
                raise ProjectTerminalError(
                    f"refusing to traverse symlinked project directory: {directory}"
                )
        except OSError as exc:
            raise ProjectTerminalError(f"cannot inspect project directory: {directory}") from exc


def _atomic_write(
    path: Path,
    content: bytes,
    *,
    mode: int,
    expected_sha256: str | None = None,
) -> None:
    """Create or replace one regular file only after checking its previous digest."""

    if len(content) > _MAX_CONFIG_BYTES:
        raise ProjectTerminalError(f"{path} exceeds the byte limit")
    _assert_no_symlinked_ancestors(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_no_symlinked_ancestors(path)
    if path.is_symlink():
        raise ProjectTerminalError(f"refusing to replace symlink: {path}")
    observed: bytes | None = None
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise ProjectTerminalError(f"cannot inspect {path}") from exc
    if metadata is not None:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ProjectTerminalError(f"{path} must be one regular file")
        try:
            observed = path.read_bytes()
        except OSError as exc:
            raise ProjectTerminalError(f"cannot read {path}") from exc
        if expected_sha256 is not None and _sha256(observed) != expected_sha256:
            raise ProjectTerminalConfigConflict(f"{path} changed during enrollment")
    elif expected_sha256 is not None:
        raise ProjectTerminalConfigConflict(f"{path} disappeared during enrollment")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, mode)
        temporary.write_bytes(content)
        os.replace(temporary, path)
        os.chmod(path, mode)
    except OSError as exc:
        raise ProjectTerminalError(f"cannot publish {path}") from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _load_profile(path: Path) -> ProjectTerminalProfile:
    _assert_no_symlinked_ancestors(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProjectTerminalError(f"cannot read profile {path}") from exc
    value = _json_load(raw, label="project-terminal profile", maximum=_MAX_PROFILE_BYTES)
    return ProjectTerminalProfile.from_mapping(value, profile_path=path)


@dataclass(frozen=True, slots=True)
class ProjectTerminalProfile:
    """Content-addressed inputs needed by the native hook bridge."""

    project_root: str
    provider: str
    dispatcher_argv: tuple[str, ...]
    enabled: bool = True
    default_on: bool = True
    managed_id: str = _MANAGED_ID

    def __post_init__(self) -> None:
        root = _canonical_root(self.project_root)
        if self.project_root != root.as_posix():
            raise ProjectTerminalError("profile project_root must be canonical")
        if self.provider not in _PROVIDERS:
            raise ProjectTerminalError(f"unsupported provider: {self.provider!r}")
        _argv(self.dispatcher_argv, label="profile dispatcher_argv")
        if type(self.enabled) is not bool or type(self.default_on) is not bool:
            raise ProjectTerminalError("profile enabled/default_on must be boolean")
        if self.managed_id != _MANAGED_ID:
            raise ProjectTerminalError("unsupported profile managed_id")

    @property
    def profile_path(self) -> Path:
        return Path(self.project_root) / _PROFILE_RELATIVE

    @property
    def content_sha256(self) -> str:
        return _sha256(_canonical(self.to_mapping()))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "record_kind": _RECORD_KIND,
            "managed_id": self.managed_id,
            "project_root": self.project_root,
            "provider": self.provider,
            "dispatcher_argv": list(self.dispatcher_argv),
            "default_on": self.default_on,
            "enabled": self.enabled,
        }

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, profile_path: Path | None = None
    ) -> ProjectTerminalProfile:
        expected = {
            "schema_version",
            "record_kind",
            "managed_id",
            "project_root",
            "provider",
            "dispatcher_argv",
            "default_on",
            "enabled",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ProjectTerminalError("project-terminal profile has missing or unknown fields")
        root = _canonical_root(value["project_root"])
        if profile_path is not None:
            expected_path = _relative_profile(root, profile_path)
            if expected_path != profile_path:
                raise ProjectTerminalError("profile path is not canonical")
        if value["schema_version"] != _SCHEMA_VERSION or value["record_kind"] != _RECORD_KIND:
            raise ProjectTerminalError("unsupported project-terminal profile identity")
        profile = cls(
            project_root=root.as_posix(),
            provider=value["provider"],
            dispatcher_argv=_argv(value["dispatcher_argv"], label="profile dispatcher_argv"),
            default_on=value["default_on"],
            enabled=value["enabled"],
            managed_id=value["managed_id"],
        )
        if profile_path is not None:
            try:
                if profile_path.read_bytes() != _canonical(profile.to_mapping()):
                    raise ProjectTerminalError("profile is not canonical JSON")
            except OSError as exc:
                raise ProjectTerminalError(f"cannot verify profile {profile_path}") from exc
        return profile


@dataclass(frozen=True, slots=True)
class ProjectTerminalEnrollment:
    """Result of one explicit enrollment or idempotent re-enrollment."""

    profile: ProjectTerminalProfile
    profile_path: Path
    native_config_path: Path
    native_config_sha256: str
    changed: bool
    provider_events: tuple[str, ...] = _EVENTS


@dataclass(frozen=True, slots=True)
class ProjectTerminalRequest:
    """Exact provider input delivered to the root dispatcher."""

    profile: ProjectTerminalProfile
    event_name: str
    raw_input: bytes
    input_sha256: str
    session_id: str
    turn_id: str | None

    @property
    def parsed(self) -> dict[str, Any]:
        value = _json_load(
            self.raw_input,
            label="provider hook input",
            maximum=_MAX_HOOK_INPUT_BYTES,
        )
        if not isinstance(value, dict):
            raise ProjectTerminalError("provider hook input must be an object")
        return value


@dataclass(frozen=True, slots=True)
class ProjectTerminalDecision:
    """Provider-neutral decision returned by the root dispatcher."""

    decision: str
    reason: str | None = None
    additional_context: str | None = None

    def __post_init__(self) -> None:
        if self.decision not in {"allow", "deny", "block"}:
            raise ProjectTerminalError("dispatcher decision must be allow, deny, or block")
        if self.reason is not None:
            _bounded_string(self.reason, label="dispatcher reason", maximum=_MAX_REASON_BYTES)
        if self.additional_context is not None:
            _bounded_string(
                self.additional_context,
                label="dispatcher additional_context",
                maximum=_MAX_CONTEXT_BYTES,
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProjectTerminalDecision:
        if not isinstance(value, Mapping):
            raise ProjectTerminalError("dispatcher result must be an object")
        if set(value) - {"decision", "reason", "additional_context"}:
            raise ProjectTerminalError("dispatcher result has unknown fields")
        if "decision" not in value:
            raise ProjectTerminalError("dispatcher result has no decision")
        return cls(value["decision"], value.get("reason"), value.get("additional_context"))


def _native_config_path(root: Path, provider: str) -> Path:
    if provider == "claude":
        return root / ".claude" / "settings.json"
    if provider == "codex":
        return root / ".codex" / "hooks.json"
    raise ProjectTerminalError(f"unsupported provider: {provider!r}")


def _hook_command(*, bootstrap_argv: Sequence[str], profile_path: Path) -> str:
    command = (*_argv(bootstrap_argv, label="bootstrap_argv"), "--profile", profile_path.as_posix())
    return shlex.join(command)


def _managed_handler(command: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": command,
        "timeout": 30,
        "statusMessage": "bear-hug project terminal",
    }


def _managed_groups(provider: str, command: str) -> dict[str, dict[str, Any]]:
    # Claude and Codex both use the same event names and command-handler stdin contract.  The
    # PreToolUse matcher is deliberately all tools so the root dispatcher, rather than a guessed
    # matcher list, owns the engine's tool coverage.
    return {
        "SessionStart": {
            "matcher": (
                "startup|resume|clear|compact|fork"
                if provider == "claude"
                else "startup|resume|clear|compact"
            ),
            "hooks": [_managed_handler(command)],
        },
        "UserPromptSubmit": {"hooks": [_managed_handler(command)]},
        "PreToolUse": {"matcher": ".*", "hooks": [_managed_handler(command)]},
    }


def _load_native_config(path: Path) -> tuple[dict[str, Any], bytes | None, int]:
    _assert_no_symlinked_ancestors(path)
    if path.is_symlink():
        raise ProjectTerminalError(f"native config may not be a symlink: {path}")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {}, None, 0o644
    except OSError as exc:
        raise ProjectTerminalError(f"cannot inspect native config {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ProjectTerminalError(f"native config must be one regular file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProjectTerminalError(f"cannot read native config {path}") from exc
    value = _json_load(raw, label=f"native config {path}", maximum=_MAX_CONFIG_BYTES)
    if not isinstance(value, dict):
        raise ProjectTerminalError(f"native config must be a JSON object: {path}")
    return value, raw, stat.S_IMODE(metadata.st_mode) or 0o644


def _inline_codex_hooks_present(root: Path) -> bool:
    # We intentionally do not parse or rewrite TOML.  A project with inline hooks must be enrolled
    # through that representation by an operator who can preserve its unknown syntax.
    config = root / ".codex" / "config.toml"
    _assert_no_symlinked_ancestors(config)
    if not config.is_file() or config.is_symlink():
        return False
    try:
        raw = config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectTerminalError(f"cannot inspect Codex config {config}") from exc
    for line in raw.splitlines():
        if line.strip() in {
            "[hooks]",
            "[hooks.SessionStart]",
            "[hooks.UserPromptSubmit]",
            "[hooks.PreToolUse]",
        }:
            return True
        if line.lstrip().startswith("[[hooks."):
            return True
    return False


def _same_handler(value: Any, command: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "command"
        and value.get("command") == command
    )


def _merge_native_config(
    config: Mapping[str, Any], *, provider: str, command: str
) -> tuple[dict[str, Any], bool]:
    result = copy.deepcopy(dict(config))
    raw_hooks = result.get("hooks")
    if raw_hooks is None:
        raw_hooks = {}
        result["hooks"] = raw_hooks
    if not isinstance(raw_hooks, dict):
        raise ProjectTerminalConfigConflict("native config hooks must be an object")
    additions = _managed_groups(provider, command)
    changed = False
    for event in _EVENTS:
        groups = raw_hooks.get(event)
        if groups is None:
            groups = []
            raw_hooks[event] = groups
            changed = True
        if not isinstance(groups, list):
            raise ProjectTerminalConfigConflict(f"native config hooks.{event} must be an array")

        managed_positions: list[tuple[int, int]] = []
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                raise ProjectTerminalConfigConflict(
                    f"native config hooks.{event} has a non-object group"
                )
            handlers = group.get("hooks")
            if handlers is None:
                continue
            if not isinstance(handlers, list):
                raise ProjectTerminalConfigConflict(
                    f"native config hooks.{event}[{group_index}].hooks must be an array"
                )
            for handler_index, handler in enumerate(handlers):
                if _same_handler(handler, command):
                    managed_positions.append((group_index, handler_index))

        if len(managed_positions) > 1:
            # Remove only exact managed duplicates.  Unknown entries and their order remain intact.
            for group_index, handler_index in reversed(managed_positions[1:]):
                groups[group_index]["hooks"].pop(handler_index)
                changed = True
        if managed_positions:
            continue
        groups.append(copy.deepcopy(additions[event]))
        changed = True
    return result, changed


def enroll_project(
    project_root: Path | str,
    *,
    provider: str,
    dispatcher_argv: Sequence[str],
    bootstrap_argv: Sequence[str] | None = None,
    profile_path: Path | str | None = None,
) -> ProjectTerminalEnrollment:
    """Explicitly enroll one project with default-on native command hooks.

    ``dispatcher_argv`` is the root-owned executable that receives the provider's raw hook JSON
    on stdin and emits one decision JSON object.  ``bootstrap_argv`` is the executable prefix that
    runs this module's ``hook`` command; callers normally supply an absolute interpreter/module
    command so the enrollment is portable across provider shells.  Existing unknown config keys,
    groups, and handlers are copied byte-for-byte in semantic order; malformed or concurrent files
    fail closed instead of being guessed over.
    """

    root = _canonical_root(project_root)
    if provider not in _PROVIDERS:
        raise ProjectTerminalError(f"unsupported provider: {provider!r}")
    if provider == "codex":
        raise ProjectTerminalError(
            "Codex native project hooks are not available; use the explicit campaign CLI"
        )
    dispatcher = _argv(dispatcher_argv, label="dispatcher_argv")
    bootstrap = _argv(
        bootstrap_argv or (sys.executable, "-m", "bearhug.project_terminal", "hook"),
        label="bootstrap_argv",
    )
    profile = ProjectTerminalProfile(root.as_posix(), provider, dispatcher)
    profile_file = _relative_profile(root, profile_path)
    config_file = _native_config_path(root, provider)
    config, original, mode = _load_native_config(config_file)
    command = _hook_command(bootstrap_argv=bootstrap, profile_path=profile_file)
    merged, changed = _merge_native_config(config, provider=provider, command=command)
    profile_bytes = _canonical(profile.to_mapping())
    profile_changed = False
    if profile_file.exists():
        if profile_file.is_symlink():
            raise ProjectTerminalConfigConflict("existing profile is a symlink")
        try:
            existing = _load_profile(profile_file)
        except ProjectTerminalError as exc:
            raise ProjectTerminalConfigConflict(f"existing profile is not ours: {exc}") from exc
        if existing == profile:
            pass
        elif (
            not existing.enabled
            and existing.project_root == profile.project_root
            and existing.provider == profile.provider
            and existing.dispatcher_argv == profile.dispatcher_argv
            and existing.default_on == profile.default_on
            and existing.managed_id == profile.managed_id
        ):
            # Re-enrollment is the explicit opt-in after a prior opt-out.  The config merge below
            # restores this bridge's exact handlers while preserving every unknown entry.
            _atomic_write(
                profile_file,
                profile_bytes,
                mode=0o600,
                expected_sha256=_sha256(_canonical(existing.to_mapping())),
            )
            profile_changed = True
        else:
            raise ProjectTerminalConfigConflict("existing project-terminal profile differs")
    else:
        _atomic_write(profile_file, profile_bytes, mode=0o600)
        profile_changed = True
    changed = changed or profile_changed
    config_bytes = _canonical(merged)
    if not changed and original is not None:
        config_bytes = original
    if original != config_bytes:
        _atomic_write(
            config_file,
            config_bytes,
            mode=mode,
            expected_sha256=_sha256(original) if original is not None else None,
        )
        changed = True
    return ProjectTerminalEnrollment(
        profile=profile,
        profile_path=profile_file,
        native_config_path=config_file,
        native_config_sha256=_sha256(config_bytes),
        changed=changed,
    )


def opt_out_project(
    project_root: Path | str,
    *,
    provider: str,
    dispatcher_argv: Sequence[str],
    bootstrap_argv: Sequence[str] | None = None,
    profile_path: Path | str | None = None,
) -> ProjectTerminalEnrollment:
    """Remove only this bridge's exact handlers, leaving all unknown project config untouched."""

    root = _canonical_root(project_root)
    dispatcher = _argv(dispatcher_argv, label="dispatcher_argv")
    bootstrap = _argv(
        bootstrap_argv or (sys.executable, "-m", "bearhug.project_terminal", "hook"),
        label="bootstrap_argv",
    )
    profile_file = _relative_profile(root, profile_path)
    if not profile_file.exists():
        raise ProjectTerminalConfigConflict("cannot opt out an unenrolled project")
    if profile_file.is_symlink():
        raise ProjectTerminalConfigConflict("existing profile is a symlink")
    try:
        existing = _load_profile(profile_file)
    except ProjectTerminalError as exc:
        raise ProjectTerminalConfigConflict(f"existing profile is not ours: {exc}") from exc
    profile = ProjectTerminalProfile(root.as_posix(), provider, dispatcher, enabled=False)
    if existing.project_root != profile.project_root or existing.provider != profile.provider:
        raise ProjectTerminalConfigConflict(
            "existing profile belongs to another project/provider"
        )
    if existing.dispatcher_argv != profile.dispatcher_argv:
        raise ProjectTerminalConfigConflict("opt-out dispatcher does not match enrollment")
    config_file = _native_config_path(root, provider)
    config, original, mode = _load_native_config(config_file)
    command = _hook_command(bootstrap_argv=bootstrap, profile_path=profile_file)
    result = copy.deepcopy(config)
    raw_hooks = result.get("hooks")
    if raw_hooks is not None and not isinstance(raw_hooks, dict):
        raise ProjectTerminalConfigConflict("native config hooks must be an object")
    changed = False
    if isinstance(raw_hooks, dict):
        for event in _EVENTS:
            groups = raw_hooks.get(event)
            if groups is None:
                continue
            if not isinstance(groups, list):
                raise ProjectTerminalConfigConflict(f"native config hooks.{event} must be an array")
            for group in groups:
                if not isinstance(group, dict):
                    raise ProjectTerminalConfigConflict(
                        f"native config hooks.{event} has a non-object group"
                    )
                handlers = group.get("hooks")
                if handlers is None:
                    continue
                if not isinstance(handlers, list):
                    raise ProjectTerminalConfigConflict(
                        f"native config hooks.{event} handlers must be an array"
                    )
                kept = [handler for handler in handlers if not _same_handler(handler, command)]
                if len(kept) != len(handlers):
                    group["hooks"] = kept
                    changed = True
    if original is None:
        raise ProjectTerminalConfigConflict("cannot opt out an unenrolled project")
    config_bytes = _canonical(result)
    profile_bytes = _canonical(profile.to_mapping())
    profile_changed = existing.enabled
    if profile_changed:
        _atomic_write(
            profile_file,
            profile_bytes,
            mode=0o600,
            expected_sha256=_sha256(_canonical(existing.to_mapping())),
        )
    if changed:
        _atomic_write(config_file, config_bytes, mode=mode, expected_sha256=_sha256(original))
    else:
        config_bytes = original
    changed = changed or profile_changed
    return ProjectTerminalEnrollment(
        profile=profile,
        profile_path=profile_file,
        native_config_path=config_file,
        native_config_sha256=_sha256(config_bytes),
        changed=changed,
    )


def _request_from_raw(raw: bytes, profile: ProjectTerminalProfile) -> ProjectTerminalRequest:
    if not raw or len(raw) > _MAX_HOOK_INPUT_BYTES:
        raise ProjectTerminalError("provider hook input is empty or oversized")
    value = _json_load(raw, label="provider hook input", maximum=_MAX_HOOK_INPUT_BYTES)
    if not isinstance(value, dict):
        raise ProjectTerminalError("provider hook input must be an object")
    event = value.get("hook_event_name")
    if event not in _EVENTS:
        raise ProjectTerminalError(f"unsupported native hook event: {event!r}")
    if value.get("provider") is not None and value.get("provider") != profile.provider:
        raise ProjectTerminalError("provider hook input names another provider")
    session_id = _bounded_string(value.get("session_id"), label="session_id", maximum=256)
    cwd = _canonical_root(value.get("cwd"))
    root = _canonical_root(profile.project_root)
    if cwd != root and root not in cwd.parents:
        raise ProjectTerminalError("hook cwd is outside enrolled project")
    turn_id = value.get("turn_id", value.get("prompt_id"))
    if turn_id is not None:
        turn_id = _bounded_string(turn_id, label="turn_id", maximum=256)
    sources = _SOURCES_BY_PROVIDER[profile.provider]
    if (
        event == "SessionStart"
        and value.get("source") is not None
        and value["source"] not in sources
    ):
        raise ProjectTerminalError("SessionStart source is unsupported")
    if event == "UserPromptSubmit" and not isinstance(value.get("prompt"), str):
        raise ProjectTerminalError("UserPromptSubmit prompt is missing")
    if event == "PreToolUse":
        _bounded_string(value.get("tool_name"), label="tool_name", maximum=256)
        if "tool_input" not in value or not isinstance(
            value["tool_input"],
            (dict, list, str, int, float, bool, type(None)),
        ):
            raise ProjectTerminalError("PreToolUse tool_input is malformed")
    return ProjectTerminalRequest(profile, event, raw, _sha256(raw), session_id, turn_id)


def _response_for(
    request: ProjectTerminalRequest, decision: ProjectTerminalDecision
) -> bytes:
    event = request.event_name
    if event == "SessionStart":
        if decision.decision != "allow":
            raise ProjectTerminalError("SessionStart has context-only native control")
        output: dict[str, Any] = {}
        if decision.additional_context:
            output["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": decision.additional_context,
            }
        return _canonical(output) if output else b""
    if event == "UserPromptSubmit":
        if decision.decision in {"block", "deny"}:
            reason = decision.reason or "project terminal dispatcher blocked this prompt"
            output = {"decision": "block", "reason": reason}
            if decision.additional_context:
                output["hookSpecificOutput"] = {
                    "hookEventName": event,
                    "additionalContext": decision.additional_context,
                }
            return _canonical(output)
        output = {}
        if decision.additional_context:
            output["hookSpecificOutput"] = {
                "hookEventName": event,
                "additionalContext": decision.additional_context,
            }
        return _canonical(output) if output else b""
    # PreToolUse uses provider-native permissionDecision.  A neutral "block" maps to deny.
    output = {"hookSpecificOutput": {"hookEventName": event}}
    if decision.decision in {"deny", "block"}:
        output["hookSpecificOutput"].update(
            {
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason
                or "project terminal dispatcher denied this tool call",
            }
        )
    else:
        # Passing this coordinator does not grant native tool permission or override another
        # installed policy. Omit permissionDecision so the provider's usual checks still apply.
        if decision.additional_context:
            output["hookSpecificOutput"]["additionalContext"] = decision.additional_context
    return _canonical(output)


def run_project_hook(
    raw: bytes,
    *,
    profile: ProjectTerminalProfile,
    dispatcher: Callable[[ProjectTerminalRequest], ProjectTerminalDecision | Mapping[str, Any]],
) -> bytes:
    """Validate one provider event, route it, and render the exact native response bytes."""

    if not profile.enabled:
        return b""
    request = _request_from_raw(raw, profile)
    try:
        result = dispatcher(request)
        decision = (
            result
            if isinstance(result, ProjectTerminalDecision)
            else ProjectTerminalDecision.from_mapping(result)
        )
    except ProjectTerminalError:
        raise
    except Exception as exc:
        raise ProjectTerminalError(f"project terminal dispatcher failed: {exc}") from exc
    response = _response_for(request, decision)
    if len(response) > _MAX_HOOK_OUTPUT_BYTES:
        raise ProjectTerminalError("project terminal response exceeds the byte limit")
    return response


def _run_dispatcher_process(command, *, input, timeout, check, **options):
    from bearhug.hooks.runner import _terminate_process_group

    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, start_new_session=(os.name == "posix"), **options
    )
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except BaseException:
        _terminate_process_group(process)
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _invoke_dispatcher(
    request: ProjectTerminalRequest,
    *,
    executor: Callable[..., Any] = _run_dispatcher_process,
    timeout_s: float = 30.0,
) -> ProjectTerminalDecision:
    try:
        completed = executor(
            list(request.profile.dispatcher_argv),
            cwd=request.profile.project_root,
            input=request.raw_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "BEARHUG_PROJECT_ROOT": request.profile.project_root,
                "BEARHUG_PROVIDER": request.profile.provider,
                "BEARHUG_TERMINAL_EVENT": request.event_name,
                "BEARHUG_TERMINAL_INPUT_SHA256": request.input_sha256,
            },
            check=False,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectTerminalError(f"project terminal dispatcher unavailable: {exc}") from exc
    stdout = completed.stdout or b""
    stderr = completed.stderr or b""
    if isinstance(stdout, str):
        stdout = stdout.encode()
    if isinstance(stderr, str):
        stderr = stderr.encode()
    if completed.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").splitlines()
        raise ProjectTerminalError(
            "project terminal dispatcher failed"
            + (f": {detail[0][:256]}" if detail else f" with exit {completed.returncode}")
        )
    value = _json_load(
        stdout,
        label="project terminal dispatcher result",
        maximum=_MAX_HOOK_OUTPUT_BYTES,
    )
    return ProjectTerminalDecision.from_mapping(value)


def run_project_hook_process(
    raw: bytes,
    *,
    profile: ProjectTerminalProfile,
    executor: Callable[..., Any] = _run_dispatcher_process,
    timeout_s: float = 30.0,
) -> bytes:
    """Run one native hook exchange through the profile's external root dispatcher.

    Prompt/tool failures become explicit provider-native blocks.  SessionStart cannot block in
    Claude's command-hook API, so its failure is raised to the caller and remains a visible hook
    error rather than being mislabeled as an enforced startup stop.
    """

    if not profile.enabled:
        return b""
    request = _request_from_raw(raw, profile)
    try:
        decision = _invoke_dispatcher(request, executor=executor, timeout_s=timeout_s)
    except ProjectTerminalError as exc:
        if request.event_name == "SessionStart":
            raise
        decision = ProjectTerminalDecision(
            "block" if request.event_name == "UserPromptSubmit" else "deny",
            reason=str(exc),
        )
    return _response_for(request, decision)


def _main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="bearhug.project_terminal")
    subparsers = parser.add_subparsers(dest="command", required=True)
    hook = subparsers.add_parser("hook")
    hook.add_argument("--profile", required=True)
    args = parser.parse_args(list(argv))
    if args.command != "hook":
        return 2
    try:
        profile_path = Path(args.profile).expanduser()
        profile = _load_profile(profile_path)
        response = run_project_hook_process(
            sys.stdin.buffer.read(_MAX_HOOK_INPUT_BYTES + 1), profile=profile
        )
        if response:
            sys.stdout.buffer.write(response)
            sys.stdout.buffer.flush()
        return 0
    except ProjectTerminalError as exc:
        # Exit 2 is the native synchronous block mechanism for UserPromptSubmit and PreToolUse.
        # SessionStart has no block response; its stderr remains diagnostic and the provider may
        # continue, which is recorded by the caller as an unavailable startup control.
        print(str(exc), file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    return _main(sys.argv[1:] if argv is None else argv)


__all__ = [
    "ProjectTerminalConfigConflict",
    "ProjectTerminalDecision",
    "ProjectTerminalEnrollment",
    "ProjectTerminalError",
    "ProjectTerminalProfile",
    "ProjectTerminalRequest",
    "enroll_project",
    "main",
    "opt_out_project",
    "run_project_hook",
    "run_project_hook_process",
]


if __name__ == "__main__":
    raise SystemExit(main())
