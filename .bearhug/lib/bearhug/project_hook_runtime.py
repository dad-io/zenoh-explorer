"""Installed provider-neutral enforcement for project command hooks.

The native wrapper retains provider parsing and response rendering.  This module converts the
reviewed Codex command-hook surface into the closed normalized schema, runs the existing ordered
dispatcher, journals both the event and aggregate result, and maintains only the small amount of
durable session evidence which completion evaluators require.

Formatting is deliberately outside evaluator threads: it is a mutating post-tool step.  Project
validation commands run in a detached helper after a successful write and publish a durable result;
the completion checkpoint blocks while the helper is pending/running or when it failed.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.harness_policy_v2 import compile_harness_policy_v2, semantic_record_sha256_v2
from bearhug.hook_adapters.tool_use import _parse_patch
from bearhug.hook_dispatcher import (
    EvaluatorContext,
    EvaluatorOutcome,
    EvaluatorRegistration,
    OrderedHookDispatcher,
)
from bearhug.normalized_hook_journal import append_normalized_hook_record
from bearhug.normalized_hooks import build_normalized_hook_event
from bearhug.project_hook_launcher import (
    HookProject,
    hook_authority_directory,
    resolve_hook_project,
)

try:
    from bearhug_runtime.writes import resolve_any_file_writes
except ImportError:  # Source checkouts do not ordinarily put runtime/ on sys.path.
    _development_runtime = Path(__file__).resolve().parents[2] / "runtime"
    if _development_runtime.is_dir():
        sys.path.insert(0, str(_development_runtime))
        from bearhug_runtime.writes import resolve_any_file_writes
    else:
        resolve_any_file_writes = None  # type: ignore[assignment]

try:
    from bearhug_runtime.evaluators.dlv import _real_dlv_subcommand
    from bearhug_runtime.evaluators.response_shape import LONG_CHARS, _count_asks, _prose_lines
    from bearhug_runtime.evaluators.review_gate import REVIEW_AGENTS
except ImportError:  # pragma: no cover - a malformed installation fails before dispatch.
    _real_dlv_subcommand = None  # type: ignore[assignment]
    _count_asks = None  # type: ignore[assignment]
    _prose_lines = None  # type: ignore[assignment]
    LONG_CHARS = 6000
    REVIEW_AGENTS = ()


class ProjectHookRuntimeError(RuntimeError):
    """The installed hook could not produce a trustworthy governed result."""


RUNTIME_ID = "bearhug-project-hooks"
RUNTIME_VERSION = "1.0.0"
ADAPTER_ID = "codex-command-hook"
ADAPTER_VERSION = "1.0.0"
_EVENTS = {
    "SessionStart": ("session-start", "session"),
    "SessionEnd": ("session-end", "session"),
    "UserPromptSubmit": ("user-prompt-submit", "turn"),
    "PreToolUse": ("pre-tool-use", "turn"),
    "PostToolUse": ("post-tool-use", "turn"),
    "PreCompact": ("pre-compact", "turn"),
    "PostCompact": ("post-compact", "turn"),
    "SubagentStart": ("subagent-start", "turn"),
    "SubagentStop": ("subagent-stop", "turn"),
    "Stop": ("completion-request", "turn"),
    "Interrupt": ("interrupt", "turn"),
}
_SOURCE_SUFFIXES = (
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".h",
    ".java",
    ".js",
    ".mjs",
    ".proto",
    ".py",
    ".rs",
    ".sh",
    ".ts",
    ".tsx",
)
_REVIEW_AGENTS = frozenset(REVIEW_AGENTS)
_STATE_LIMIT = 256 * 1024
_CONFIG_LIMIT = 256 * 1024
_CHECK_OUTPUT_LIMIT = 16 * 1024
_READ_ONLY_TOOLS = frozenset(
    {"read", "grep", "glob", "ls", "find", "web_search", "web_fetch", "taskoutput"}
)
_READ_ONLY_GIT = frozenset(
    {
        "blame",
        "cat-file",
        "diff",
        "diff-tree",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "show",
        "show-ref",
        "status",
    }
)


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 1024 or "\x00" in value:
        raise ProjectHookRuntimeError(f"{label} is missing or invalid")
    return value


def _relative_path(root: Path, value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(root)
        except ValueError as exc:
            raise ProjectHookRuntimeError("tool write is outside the installed project") from exc
    rendered = candidate.as_posix()
    pure = PurePosixPath(rendered)
    if (
        not rendered
        or rendered.startswith("/")
        or "\\" in rendered
        or pure.as_posix() != rendered
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ProjectHookRuntimeError("tool write has an unsafe or ambiguous path")
    protected = (
        pure.parts[0] in {".git", ".bearhug", ".codex"}
        or pure.parts[:2] == ("scripts", "hooks")
        or rendered in {"scripts/bearhug_work.py", "scripts/bearhug_native.py"}
        or rendered.startswith("scripts/bin/bearhug-")
    )
    if protected:
        raise ProjectHookRuntimeError("tool write targets protected Git or Bear Hug state")
    return rendered


def _git(root: Path, *arguments: str) -> str:
    try:
        value = _git_bytes(root, *arguments).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ProjectHookRuntimeError("cannot resolve repository identity") from exc
    if not value:
        raise ProjectHookRuntimeError("cannot resolve repository identity")
    return value


def _git_bytes(root: Path, *arguments: str) -> bytes:
    from bearhug.host_git import run_git

    try:
        result = run_git(root, *arguments, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectHookRuntimeError("cannot capture repository content identity") from exc
    if result.returncode != 0:
        raise ProjectHookRuntimeError("cannot capture repository content identity")
    return result.stdout


_IDENTITY_EXCLUDES = (
    ":(exclude).bearhug/hook-state/**",
    ":(exclude).bearhug/normalized-hooks/**",
    ":(exclude).automation-stamps/**",
)


def _checkout_identity(root: Path) -> dict[str, Any]:
    head = _git(root, "rev-parse", "HEAD")
    diff = _git_bytes(
        root,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "HEAD",
        "--",
        ".",
        *_IDENTITY_EXCLUDES,
    )
    raw_untracked = _git_bytes(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        ".",
        *_IDENTITY_EXCLUDES,
    )
    if raw_untracked and not raw_untracked.endswith(b"\0"):
        raise ProjectHookRuntimeError("Git returned ambiguous untracked paths")
    try:
        untracked = sorted(
            item.decode("utf-8") for item in raw_untracked[:-1].split(b"\0") if item
        )
    except UnicodeDecodeError as exc:
        raise ProjectHookRuntimeError("untracked path is not UTF-8") from exc
    digest = hashlib.sha256()
    digest.update(b"bearhug-checkout-v1\0")
    digest.update(head.encode())
    digest.update(b"\0")
    digest.update(diff)
    total = len(diff)
    for relative in untracked:
        path = root / relative
        digest.update(relative.encode())
        digest.update(b"\0")
        if path.is_symlink():
            content = os.fsencode(os.readlink(path))
        else:
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise ProjectHookRuntimeError(
                    "untracked file changed during identity capture"
                ) from exc
        total += len(content)
        if total > 128 * 1024 * 1024:
            raise ProjectHookRuntimeError("checkout identity input exceeds 128 MiB")
        digest.update(hashlib.sha256(content).digest())
    changed_raw = _git_bytes(
        root,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "HEAD",
        "--",
        ".",
        *_IDENTITY_EXCLUDES,
    )
    try:
        tracked = [item.decode("utf-8") for item in changed_raw.split(b"\0") if item]
    except UnicodeDecodeError as exc:
        raise ProjectHookRuntimeError("changed path is not UTF-8") from exc
    return {
        "sha256": digest.hexdigest(),
        "head_oid": head,
        "paths": sorted(set(tracked + untracked)),
    }


def _repository(root: Path) -> dict[str, Any]:
    common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return {
        "common_dir_sha256": _sha256(str(common).encode()),
        "worktree_sha256": _sha256(str(root).encode()),
        "head_oid": _git(root, "rev-parse", "HEAD"),
        "branch": None if branch == "HEAD" else branch,
    }


def _response_success(response: Any) -> bool | None:
    """Normalize explicit native completion evidence; absence is not proof of success."""
    if isinstance(response, str):
        if response.lstrip().startswith("{"):
            try:
                value = json.loads(response)
            except (ValueError, RecursionError):
                value = None
            if isinstance(value, Mapping):
                return _response_success(value)
        codes = re.findall(r"(?mi)^\s*Process exited with code\s+(-?\d+)\s*$", response)
        if codes:
            return all(int(code) == 0 for code in codes)
        if response.lstrip().lower().startswith(("error", "failed")):
            return False
        return None
    if isinstance(response, Mapping):
        if (
            response.get("success") is False or response.get("isError") is True
            or response.get("is_error") is True or bool(response.get("error"))
            or response.get("status") in {"failed", "error", "cancelled", "interrupted"}
        ):
            return False
        codes = [response[key] for key in ("exit_code", "returncode") if key in response]
        if codes:
            return all(type(code) is int and code == 0 for code in codes)
        if response.get("success") is True or response.get("status") in {"success", "succeeded"}:
            return True
        if isinstance(response.get("output"), str):
            return _response_success(response["output"])
    return None


def _tool_effects(
    root: Path, payload: Mapping[str, Any], event: str
) -> tuple[str, str, list[dict[str, Any]]]:
    name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(name, str) or not isinstance(tool_input, Mapping):
        raise ProjectHookRuntimeError("tool hook is missing tool_name or tool_input")
    post = event == "PostToolUse"
    status = "succeeded" if post else "intended"
    response = payload.get("tool_response")
    if post and _response_success(response) is False:
        status = "failed"
    effects: list[dict[str, Any]] = []
    operation = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-") or "tool"
    family = "shell" if name.lower() in {"bash", "shell", "exec_command"} else "filesystem-write"

    if name == "apply_patch":
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise ProjectHookRuntimeError("apply_patch command is missing")
        try:
            parsed = _parse_patch(command, cwd=root, worktree=root)
        except Exception as exc:
            raise ProjectHookRuntimeError(f"apply_patch effects are unsupported: {exc}") from exc
        for item in parsed:
            effects.append(
                {
                    "kind": item.kind,
                    "path": _relative_path(root, item.path),
                    "target_path": (
                        None if item.target_path is None else _relative_path(root, item.target_path)
                    ),
                    "status": status,
                }
            )
    elif name.lower() in {"bash", "shell", "exec_command"}:
        command = tool_input.get("command") or tool_input.get("cmd")
        if not isinstance(command, str):
            raise ProjectHookRuntimeError("shell command is missing")
        if resolve_any_file_writes is None:
            if any(token in command for token in (">", "tee ", "-w ", "-i ")):
                raise ProjectHookRuntimeError("shell-write resolver is unavailable")
        else:
            resolved = resolve_any_file_writes("Bash", {"command": command}, tool_response=response)
            if resolved.opaque:
                raise ProjectHookRuntimeError("opaque interpreter writes are unsupported")
            for value in resolved.paths:
                effects.append(
                    {
                        "kind": "modify",
                        "path": _relative_path(root, value),
                        "target_path": None,
                        "status": status,
                    }
                )
    else:
        candidate = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(candidate, str):
            effects.append(
                {
                    "kind": "modify",
                    "path": _relative_path(root, candidate),
                    "target_path": None,
                    "status": status,
                }
            )
        elif name.lower() in {"write", "edit", "multiedit", "notebookedit"}:
            raise ProjectHookRuntimeError(f"{name} write targets are unsupported")
        elif name.lower() in _READ_ONLY_TOOLS:
            family = "filesystem-read"
        else:
            family = "mcp"
    return family, operation, effects


def _git_commands(command: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Find Git invocations structurally, including wrappers, paths, and substitutions."""

    # Preserve shell concatenation/escaping (g''it and g\\it are both git). Expansions,
    # substitutions and indirect evaluation cannot be represented as a closed command here.
    # Refuse them rather than pretending a lexical approximation is authoritative.
    command = command.replace("\\\n", "")
    quote = None
    escaped = False
    for character in command:
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if character == "'":
                quote = None
            continue
        if character == "\\":
            escaped = True
            continue
        if character == quote:
            quote = None
            continue
        if character in {"'", '"'} and quote is None:
            quote = character
            continue
        if character in {"$", "`"} or (quote is None and character in {"{", "}", "*", "?", "["}):
            raise ProjectHookRuntimeError("dynamic shell syntax has no closed command model")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="();&|<>\n")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:
        raise ProjectHookRuntimeError("shell command cannot be parsed safely") from exc
    for index, token in enumerate(tokens):
        name = Path(token).name.casefold()
        tail = tokens[index + 1:]
        if name in {"eval", "source", "alias", "unalias", "function"}:
            raise ProjectHookRuntimeError("indirect shell evaluation has no closed command model")
        if name in {"sh", "bash", "zsh", "dash", "ksh", "fish"}:
            raise ProjectHookRuntimeError("nested shells have no closed command model")
        if name == "env" and any(
            item == "-S" or item.startswith("--split-string") for item in tail
        ):
            raise ProjectHookRuntimeError("indirect env commands have no closed command model")
        if token.startswith("GIT_") and "=" in token:
            raise ProjectHookRuntimeError("Git environment overrides are forbidden")
    found: list[tuple[str, tuple[str, ...]]] = []
    for index, token in enumerate(tokens):
        if Path(token).name.casefold() != "git":
            continue
        tail = tokens[index + 1 :]
        cursor = 0
        while cursor < len(tail):
            value = tail[cursor]
            if value == "-c" or value.startswith("--config-env") or value.startswith("-c"):
                raise ProjectHookRuntimeError("Git configuration overrides are forbidden")
            if value in {"-C", "--git-dir", "--work-tree", "--namespace"}:
                cursor += 2
                continue
            if value.startswith(("--git-dir=", "--work-tree=", "--namespace=", "--config-env=")):
                cursor += 1
                continue
            if value in {
                "--bare",
                "--no-pager",
                "--paginate",
                "--literal-pathspecs",
                "--glob-pathspecs",
                "--noglob-pathspecs",
                "--icase-pathspecs",
                "--no-optional-locks",
            }:
                cursor += 1
                continue
            break
        subcommand = tail[cursor].casefold() if cursor < len(tail) else ""
        if any(
            item.split("=", 1)[0] in {"--output", "--ext-diff", "--textconv", "--exec-path"}
            for item in tail
        ):
            raise ProjectHookRuntimeError("Git external execution or output paths are forbidden")
        found.append((subcommand, tuple(tail[cursor + 1 :])))
    return tuple(found)


def _normalized_event(
    root: Path, raw: bytes, payload: Mapping[str, Any], occurred_at: datetime
) -> dict[str, Any]:
    native = _safe_token(payload.get("hook_event_name"), "hook_event_name")
    try:
        semantic, scope = _EVENTS[native]
    except KeyError as exc:
        raise ProjectHookRuntimeError(f"unsupported hook event: {native}") from exc
    session = _safe_token(payload.get("session_id"), "session_id")
    turn: str | None = None
    if scope == "turn":
        turn = _safe_token(payload.get("turn_id"), "turn_id")
    tool = None
    tool_call_id = None
    if native in {"PreToolUse", "PostToolUse"}:
        family, operation, effects = _tool_effects(root, payload, native)
        tool_call_id = _safe_token(payload.get("tool_use_id"), "tool_use_id")
        post = native == "PostToolUse"
        response = payload.get("tool_response")
        statuses = {item["status"] for item in effects}
        if not post:
            status = "intended"
        elif _response_success(response) is False or statuses == {"failed"}:
            status = "failed"
        elif "failed" in statuses:
            status = "partial"
        else:
            status = "succeeded"
        tool = {"family": family, "operation": operation, "status": status, "effects": effects}
    extension = _canonical(
        {
            "agent_type": payload.get("agent_type")
            if isinstance(payload.get("agent_type"), str)
            else None,
            "model": payload.get("model") if isinstance(payload.get("model"), str) else None,
        }
    )
    return build_normalized_hook_event(
        occurred_at=occurred_at,
        semantic_event=semantic,
        scope=scope,
        provider="openai-codex",
        adapter=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_event=native,
        evidence_kind="native-hook",
        native_input=raw,
        session_id=session,
        thread_id=session,
        turn_id=turn,
        tool_call_id=tool_call_id,
        repository=_repository(root),
        tool=tool,
        provider_extension=extension,
    )


_AUTHORITY_PROJECTS: dict[Path, HookProject] = {}


def bind_hook_authority(project: HookProject) -> None:
    """Bind the physical identity sealed by the protected production launcher."""
    hook_authority_directory(project)
    _AUTHORITY_PROJECTS[Path(project.repository_root)] = project


def _authority_root(root: Path) -> Path:
    selected = _AUTHORITY_PROJECTS.get(root)
    if selected is None:
        selected = resolve_hook_project(
            provider="codex", installation_roots=[root], cwd=root,
        )
        _AUTHORITY_PROJECTS[root] = selected
    return hook_authority_directory(selected)


def _state_path(root: Path, session_id: str) -> Path:
    name = f"{_sha256(session_id.encode())}.json"
    return _authority_root(root) / ".bearhug" / "hook-state" / name


def _assert_project_path(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ProjectHookRuntimeError("hook state escaped the project root") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.exists() or current.is_symlink():
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise ProjectHookRuntimeError("cannot inspect hook state path") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise ProjectHookRuntimeError("hook state path may not contain symlinks")


@contextlib.contextmanager
def _state_lock(root: Path, session_id: str):
    authority = _authority_root(root)
    directory = authority / ".bearhug" / "hook-state"
    _assert_project_path(authority, directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_project_path(authority, directory)
    path = directory / f"{_sha256(session_id.encode())}.lock"
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ProjectHookRuntimeError("hook state lock must be a singly linked regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        # This copy is display-only. A poisoned/missing project projection never participates in
        # gate evaluation, and cannot stop authoritative progress or roll back its write fence.
        with contextlib.suppress(OSError, ValueError, ProjectHookRuntimeError):
            authoritative = directory / f"{_sha256(session_id.encode())}.json"
            if authoritative.is_file():
                projection = root / ".bearhug" / "hook-state" / authoritative.name
                _assert_project_path(root, projection)
                _atomic_json(
                    projection,
                    _load_json(authoritative, maximum=_STATE_LIMIT, label="host hook state"),
                )
        os.close(descriptor)


def _load_json(path: Path, *, maximum: int, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ProjectHookRuntimeError(f"{label} may not be a symlink")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ProjectHookRuntimeError(f"cannot read {label}") from exc
    if len(raw) > maximum:
        raise ProjectHookRuntimeError(f"{label} exceeds its byte limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectHookRuntimeError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProjectHookRuntimeError(f"{label} must be a JSON object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    root = path.parents[2]
    _assert_project_path(root, path.parent)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_project_path(root, path)
    raw = _canonical(value)
    if len(raw) > _STATE_LIMIT:
        raise ProjectHookRuntimeError("hook state exceeds its byte limit")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _config(root: Path) -> dict[str, Any]:
    path = root / ".codex" / "bearhug-host" / "automation.json"
    _assert_project_path(root, path)
    value = _load_json(
        path, maximum=_CONFIG_LIMIT, label="protected automation config"
    )
    if not value:
        raise ProjectHookRuntimeError("automation config is missing; rerun Bear Hug setup")
    if value.get("schema_version") != "1" or value.get("record_kind") != "project_automation":
        raise ProjectHookRuntimeError("automation config has an unsupported schema")
    commands = value.get("validation_commands")
    if not isinstance(commands, list):
        raise ProjectHookRuntimeError("automation validation_commands must be an array")
    for command in commands:
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item and "\x00" not in item for item in command)
        ):
            raise ProjectHookRuntimeError("automation validation command is not a closed argv")
    return value


def _validator_candidate(root: Path, path: Path) -> dict[str, Any]:
    _assert_project_path(root, path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ProjectHookRuntimeError("validator must be a singly linked regular file")
            raw = stream.read(2 * 1024 * 1024 + 1)
    except OSError as exc:
        raise ProjectHookRuntimeError("automation candidate changed while read") from exc
    if len(raw) > 2 * 1024 * 1024:
        raise ProjectHookRuntimeError("automation candidate exceeds 2 MiB")
    interpreter = "python3" if path.suffix == ".py" else "sh"
    if raw.startswith(b"#!"):
        try:
            shebang = shlex.split(raw.split(b"\n", 1)[0][2:].decode())
        except (UnicodeDecodeError, ValueError):
            shebang = []
        if shebang and shebang[0] == "/usr/bin/env":
            shebang = shebang[1:]
        allowed = {"python3", "/usr/bin/python3"} if path.suffix == ".py" else {
            "sh", "/bin/sh", "bash", "/bin/bash"
        }
        interpreter = Path(shebang[0]).name if len(shebang) == 1 and shebang[0] in allowed else ""
    relative = path.relative_to(root).as_posix()
    digest = _sha256(raw)
    candidate: dict[str, Any] = {
        "path": relative,
        "sha256": digest,
        "proposed_argv": [interpreter, relative] if interpreter else [],
        "status": "approval_required" if interpreter else "manual_configuration_required",
    }
    if interpreter:
        candidate["approval_argv"] = [
            "bearhug", "automation", "admit-validator", "--root", str(root),
            "--path", relative, "--sha256", digest,
        ]
    return candidate


def _registered_validator_paths(config: Mapping[str, Any]) -> set[str]:
    return {
        PurePosixPath(item).as_posix()
        for argv in config.get("validation_commands", [])
        if isinstance(argv, list)
        for item in argv
        if isinstance(item, str)
    }


def _automation_candidates(root: Path, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    registered = _registered_validator_paths(config)
    scripts = root / "scripts"
    if not scripts.is_dir() or scripts.is_symlink():
        return []
    pattern = re.compile(r"(?:validate[-_].+|test_.+|check[-_].+|.*lint.*)\.(?:py|sh)\Z")
    candidates: list[dict[str, Any]] = []
    for scanned, path in enumerate(scripts.rglob("*")):
        if scanned >= 4096:
            raise ProjectHookRuntimeError("automation inventory exceeds 4096 paths")
        if len(candidates) >= 256:
            raise ProjectHookRuntimeError("automation candidate inventory exceeds 256 files")
        if not pattern.fullmatch(path.name) or path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in registered:
            continue
        candidates.append(_validator_candidate(root, path))
    return sorted(candidates, key=lambda candidate: candidate["path"])


def admit_validator(root: Path, path: str, sha256: str) -> dict[str, Any]:
    """Explicitly append one inspected validator without replacing existing gates or running it."""
    root = root.resolve(strict=True)
    relative = PurePosixPath(path)
    if (
        relative.is_absolute() or ".." in relative.parts or relative.as_posix() != path
        or not relative.parts or relative.parts[0] != "scripts"
        or not re.fullmatch(r"[0-9a-f]{64}", sha256)
    ):
        raise ProjectHookRuntimeError("validator path or sha256 is invalid")
    with _state_lock(root, "automation-config"):
        config_path = root / ".codex" / "bearhug-host" / "automation.json"
        config = _config(root)
        original = config_path.read_bytes()
        candidate = _validator_candidate(root, root / path)
        if candidate["sha256"] != sha256:
            raise ProjectHookRuntimeError("validator changed since inspection; inspect it again")
        if path in _registered_validator_paths(config):
            return {**candidate, "status": "already_admitted"}
        if candidate not in _automation_candidates(root, config) or not candidate["proposed_argv"]:
            raise ProjectHookRuntimeError("validator is not an admissible discovered command")
        config["validation_commands"].append(candidate["proposed_argv"])
        _assert_project_path(root, config_path)
        if config_path.read_bytes() != original:
            raise ProjectHookRuntimeError("automation config changed during admission")
        _atomic_json(config_path, config)
        # Display-only compatibility copy. No gate or future admission reads it back.
        with contextlib.suppress(OSError, ValueError, ProjectHookRuntimeError):
            projection = root / ".bearhug" / "automation.json"
            _assert_project_path(root, projection)
            _atomic_json(projection, config)
        return {**candidate, "status": "admitted"}


def _session_state(root: Path, session: str) -> dict[str, Any]:
    value = _load_json(_state_path(root, session), maximum=_STATE_LIMIT, label="hook session state")
    if not value:
        return {
            "schema_version": "1",
            "record_kind": "project_hook_session",
            "session_id_sha256": _sha256(session.encode()),
            "source_write_event": None,
            "source_checkout_sha256": None,
            "last_write_event": None,
            "current_checkout_sha256": None,
            "review_event": None,
            "review_checkout_sha256": None,
            "review_receipt": None,
            "active_review_agents": {},
            "dlv_event": None,
            "dlv_checkout_sha256": None,
            "checks": [],
            "interrupted": False,
        }
    if value.get("session_id_sha256") != _sha256(session.encode()):
        raise ProjectHookRuntimeError("hook session state belongs to another session")
    return value


def _reconcile_check_workers(state: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    for check in state.get("checks", []):
        if not isinstance(check, dict) or check.get("state") not in {"pending", "running"}:
            continue
        stamp = check.get("heartbeat_at") or check.get("started_at")
        try:
            observed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            observed = datetime.min.replace(tzinfo=UTC)
        maximum_age = 60 if check.get("state") == "pending" else 1_200
        if (now - observed).total_seconds() <= maximum_age:
            continue
        pid = check.get("worker_pid")
        alive = False
        if type(pid) is int and pid > 1:
            try:
                os.kill(pid, 0)
                alive = True
            except (OSError, ProcessLookupError):
                alive = False
        if alive:
            continue
        check["state"] = "failed"
        check["completed_at"] = now.isoformat()
        check["heartbeat_at"] = check["completed_at"]
        check["worker_pid"] = None
        check["commands"] = [
            {"argv_sha256": None, "returncode": None, "error": "stale-worker-reclaimed"}
        ]


def _command_environment(root: Path) -> dict[str, str]:
    search: list[str] = []
    for item in os.environ.get("PATH", os.defpath).split(os.pathsep):
        candidate = Path(item)
        if not candidate.is_absolute():
            continue
        resolved = candidate.resolve()
        if resolved == root or root in resolved.parents:
            continue
        rendered = str(resolved)
        if rendered not in search:
            search.append(rendered)
    for item in os.defpath.split(os.pathsep):
        if item not in search:
            search.append(item)
    return {
        "HOME": os.environ.get("HOME", os.devnull),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join(search),
        "PYTHONDONTWRITEBYTECODE": "1",
        "BEARHUG_PROJECT_ROOT": str(root),
        "BEARHUG_RUNTIME_ROOT": str(root / ".bearhug"),
        "BEARHUG_ARTIFACT_ROOT": str(root / ".bearhug"),
        "PYTHONPATH": str(root / ".bearhug" / "lib"),
    }


def _sandboxed_run(
    root: Path, argv: Sequence[str], *, timeout: float, input_data: bytes | None = None,
    read_only: bool = False, writable_paths: Sequence[Path] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Run project-defined automation without host credentials, network, or Git-state writes."""

    if not argv or timeout <= 0:
        raise ProjectHookRuntimeError("sandboxed command has no executable")
    if input_data is not None and len(input_data) > 2 * 1024 * 1024:
        raise ProjectHookRuntimeError("sandbox input exceeds 2 MiB")
    root = root.resolve(strict=True)
    for path in writable_paths:
        _assert_project_path(root, path)
        if path == root or not path.is_dir():
            raise ProjectHookRuntimeError("sandbox write exception must name a project directory")
    environment = _command_environment(root)
    with tempfile.TemporaryDirectory(prefix="bearhug-check-") as temporary:
        scratch = Path(temporary).resolve()
        environment["HOME"] = str(scratch)
        environment["TMPDIR"] = str(scratch)
        readable = {
            "/usr/bin", "/usr/sbin", "/usr/lib", "/bin", "/sbin", "/lib", "/lib64",
            str(root), str(scratch), str(Path(sys.base_prefix).resolve()),
        }
        # Interpreter/tool installations can live outside system directories. Only their bin
        # directories are exposed, never the user's home or the whole host filesystem.
        readable.update(
            item for item in environment["PATH"].split(os.pathsep)
            if Path(item).name in {"bin", "sbin"}
        )
        executable_name = shutil.which(argv[0], path=environment["PATH"])
        if executable_name:
            executable = Path(executable_name).resolve(strict=True)
            readable.add(str(executable))
            parts = executable.parts
            if "Cellar" in parts:
                cellar = parts.index("Cellar")
                if len(parts) > cellar + 2:
                    readable.add(str(Path(*parts[:cellar + 3])))
            if executable.name in {"go", "gofmt"} and executable.parent.name == "bin":
                readable.add(str(executable.parent.parent))
        writable = [str(scratch), *(str(path) for path in writable_paths)]
        if not read_only:
            writable.append(str(root))
        if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
            readable.update({
                "/System", "/Library/Developer", "/private/var/db/dyld",
                "/private/etc/localtime", "/private/etc/zoneinfo",
                "/dev/null", "/dev/urandom",
            })

            def quoted(value: str) -> str:
                return json.dumps(value)

            profile = "\n".join(
                [
                    "(version 1)",
                    "(deny default)",
                    "(allow process*)",
                    "(allow sysctl-read)",
                    "(allow file-read-metadata)",
                    # dyld ignition opens the root directory before locating its cache.
                    # A literal directory rule does not expose descendant file contents.
                    '(allow file-read-data (literal "/"))',
                    "(allow file-read* file-map-executable "
                    + " ".join(
                        f"(subpath {quoted(path)})" for path in sorted(readable) if path
                    )
                    + ")",
                    "(allow file-write* "
                    + " ".join(f"(subpath {quoted(path)})" for path in writable) + ")",
                    "(allow file-write* (literal \"/dev/null\"))",
                    *([] if read_only else [
                        f"(deny file-write* (subpath {quoted(str(root / '.git'))}) "
                        f"(subpath {quoted(str(root / '.bearhug'))}) "
                        f"(subpath {quoted(str(root / '.codex'))}))"
                    ]),
                    "(deny network*)",
                ]
            )
            command = ["/usr/bin/sandbox-exec", "-p", profile, *argv]
        else:
            bubblewrap = shutil.which("bwrap", path=environment["PATH"])
            if bubblewrap is None:
                raise ProjectHookRuntimeError(
                    "no supported OS sandbox is available for project automation"
                )
            command = [
                str(Path(bubblewrap).resolve(strict=True)),
                "--die-with-parent",
                "--unshare-all",
                "--new-session",
            ]
            # A fresh mount namespace has no implicit host home, credentials, sockets, or /tmp.
            for path in sorted(readable | {"/etc/ld.so.cache", "/etc/localtime"}):
                if Path(path).exists():
                    command.extend(("--ro-bind", path, path))
            command.extend(("--proc", "/proc", "--dev", "/dev"))
            for path in writable:
                command.extend(("--bind", path, path))
            for protected in (root / ".git", root / ".bearhug", root / ".codex"):
                if not read_only and protected.exists():
                    command.extend(("--ro-bind", str(protected), str(protected)))
            command.extend(("--chdir", str(root), "--", *argv))
        try:
            with tempfile.TemporaryFile() as incoming:
                incoming.write(input_data or b"")
                incoming.seek(0)
                process = subprocess.Popen(
                    command, cwd=root, env=environment, stdin=incoming,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                )
                output = bytearray()
                deadline = time.monotonic() + timeout
                try:
                    assert process.stdout is not None
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while selector.get_map():
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise ProjectHookRuntimeError("sandboxed command timed out")
                            for key, _ in selector.select(min(remaining, 1)):
                                chunk = os.read(key.fileobj.fileno(), 65536)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    continue
                                output.extend(chunk)
                                if len(output) > 2 * 1024 * 1024:
                                    raise ProjectHookRuntimeError("sandbox output exceeds 2 MiB")
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    return subprocess.CompletedProcess(command, process.returncode, bytes(output))
                finally:
                    # Reap the whole process group even if the leader closed stdout or exited.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                    if process.stdout is not None:
                        process.stdout.close()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProjectHookRuntimeError("sandboxed project automation could not run") from exc


def _format(root: Path, config: Mapping[str, Any], paths: Sequence[str]) -> None:
    if not paths:
        return
    commands = config.get("format_commands", [])
    if not isinstance(commands, list):
        raise ProjectHookRuntimeError("automation format_commands must be an array")
    for spec in commands:
        if not isinstance(spec, Mapping):
            raise ProjectHookRuntimeError("automation format command must be an object")
        raw = spec.get("argv")
        suffixes = spec.get("suffixes", [])
        if (
            not isinstance(raw, list)
            or not raw
            or not all(isinstance(item, str) and item for item in raw)
            or not isinstance(suffixes, list)
            or not all(isinstance(item, str) and item.startswith(".") for item in suffixes)
        ):
            raise ProjectHookRuntimeError("automation format command is not a closed argv")
        selected = [path for path in paths if not suffixes or path.endswith(tuple(suffixes))]
        if not selected:
            continue
        command: list[str] = []
        expanded = False
        for item in raw:
            if item == "{paths}":
                command.extend(selected)
                expanded = True
            else:
                command.append(item)
        if not expanded:
            command.extend(selected)
        try:
            result = _sandboxed_run(root, command, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProjectHookRuntimeError(f"formatter {raw[0]!r} could not run") from exc
        if result.returncode != 0:
            raise ProjectHookRuntimeError(f"formatter {raw[0]!r} failed")


def _start_checks(root: Path, session: str, event_id: str, state: dict[str, Any]) -> None:
    config_sha256 = _sha256(_canonical(_config(root)))
    check = {
        "event_id": event_id,
        "checkout_sha256": _checkout_identity(root)["sha256"],
        "config_sha256": config_sha256,
        "state": "pending",
        "started_at": datetime.now(UTC).isoformat(),
        "completed_at": None,
        "worker_pid": None,
        "heartbeat_at": None,
        "commands": [],
    }
    state.setdefault("checks", []).append(check)
    state["checks"] = state["checks"][-32:]
    _atomic_json(_state_path(root, session), state)
    try:
        protected_library = root / ".codex" / "bearhug-host" / "lib"
        _assert_project_path(root, protected_library)
        environment = _command_environment(root)
        environment.pop("PYTHONPATH", None)
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from bearhug.project_hook_runtime import main; raise SystemExit(main())",
                str(protected_library),
                "run-checks",
                "--root",
                str(root),
                "--session",
                session,
                "--event-id",
                event_id,
            ],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        check["worker_pid"] = process.pid
        check["heartbeat_at"] = datetime.now(UTC).isoformat()
        _atomic_json(_state_path(root, session), state)
    except OSError as exc:
        check["state"] = "failed"
        check["completed_at"] = datetime.now(UTC).isoformat()
        check["commands"] = [{"argv_sha256": None, "returncode": None, "error": "launch"}]
        _atomic_json(_state_path(root, session), state)
        raise ProjectHookRuntimeError("background validation helper could not start") from exc


def _run_checks(root: Path, session: str, event_id: str) -> int:
    config = _config(root)
    config_sha256 = _sha256(_canonical(config))
    with _state_lock(root, session):
        state = _session_state(root, session)
        matches = [item for item in state.get("checks", []) if item.get("event_id") == event_id]
        if len(matches) != 1 or matches[0].get("state") != "pending":
            return 2
        check = matches[0]
        if check.get("config_sha256") != config_sha256:
            check["state"] = "failed"
            check["completed_at"] = datetime.now(UTC).isoformat()
            check["commands"] = [
                {"argv_sha256": None, "returncode": None, "error": "config-changed"}
            ]
            _atomic_json(_state_path(root, session), state)
            return 1
        expected_checkout = check.get("checkout_sha256")
        if expected_checkout != _checkout_identity(root)["sha256"]:
            check["state"] = "failed"
            check["completed_at"] = datetime.now(UTC).isoformat()
            check["commands"] = [
                {"argv_sha256": None, "returncode": None, "error": "checkout-changed"}
            ]
            _atomic_json(_state_path(root, session), state)
            return 1
        check["state"] = "running"
        check["worker_pid"] = os.getpid()
        check["heartbeat_at"] = datetime.now(UTC).isoformat()
        _atomic_json(_state_path(root, session), state)
    results: list[dict[str, Any]] = []
    failed = False
    for argv in config["validation_commands"]:
        with _state_lock(root, session):
            heartbeat_state = _session_state(root, session)
            heartbeat_matches = [
                item
                for item in heartbeat_state.get("checks", [])
                if item.get("event_id") == event_id
            ]
            if len(heartbeat_matches) != 1 or heartbeat_matches[0].get("state") != "running":
                return 2
            heartbeat_matches[0]["heartbeat_at"] = datetime.now(UTC).isoformat()
            _atomic_json(_state_path(root, session), heartbeat_state)
        digest = _sha256(_canonical(argv))
        try:
            completed = _sandboxed_run(root, argv, timeout=900)
            output = completed.stdout[-_CHECK_OUTPUT_LIMIT:]
            results.append(
                {
                    "argv_sha256": digest,
                    "returncode": completed.returncode,
                    "output_sha256": _sha256(output),
                    "output_byte_count": len(output),
                }
            )
            failed = failed or completed.returncode != 0
        except (OSError, subprocess.SubprocessError, ProjectHookRuntimeError):
            results.append({"argv_sha256": digest, "returncode": None, "error": "execution"})
            failed = True
    with _state_lock(root, session):
        state = _session_state(root, session)
        matches = [item for item in state.get("checks", []) if item.get("event_id") == event_id]
        if len(matches) != 1 or matches[0].get("state") != "running":
            return 2
        check = matches[0]
        if check.get("checkout_sha256") != _checkout_identity(root)["sha256"]:
            results.append(
                {"argv_sha256": None, "returncode": None, "error": "checkout-changed"}
            )
            failed = True
        check["commands"] = results
        check["state"] = "failed" if failed else "passed"
        check["completed_at"] = datetime.now(UTC).isoformat()
        check["worker_pid"] = None
        check["heartbeat_at"] = check["completed_at"]
        _atomic_json(_state_path(root, session), state)
    return 1 if failed else 0


def _record_formatter_failure(
    root: Path, session: str, event_id: str, state: dict[str, Any]
) -> None:
    now = datetime.now(UTC).isoformat()
    state.setdefault("checks", []).append(
        {
            "event_id": event_id,
            "checkout_sha256": _checkout_identity(root)["sha256"],
            "config_sha256": _sha256(_canonical(_config(root))),
            "state": "failed",
            "started_at": now,
            "completed_at": now,
            "worker_pid": None,
            "heartbeat_at": now,
            "commands": [
                {"argv_sha256": None, "returncode": None, "error": "formatter"}
            ],
        }
    )
    state["checks"] = state["checks"][-32:]
    _atomic_json(_state_path(root, session), state)


def _evidence(value: Mapping[str, Any]) -> bytes:
    return _canonical(value)


def _block(identifier: str, message: str, paths: Sequence[str] = ()) -> EvaluatorOutcome:
    return EvaluatorOutcome(
        "block",
        _evidence({"evaluator": identifier, "status": "blocked"}),
        ({"code": identifier, "message": message, "paths": sorted(set(paths))},),
    )


def _policy(event: str, evaluator_ids: Sequence[str], runtime_sha: str) -> dict[str, Any]:
    def provenance(identifier: str) -> dict[str, Any]:
        return {
            "source_id": identifier,
            "source_uri": "repository:.bearhug/lib/bearhug/project_hook_runtime.py",
            "source_sha256": runtime_sha,
        }

    evaluators = []
    for index, identifier in enumerate(evaluator_ids):
        record: dict[str, Any] = {
            "id": identifier,
            "order": (index + 1) * 10,
            "matcher": {
                "tool_families": (
                    ["any-tool"]
                    if event in {"pre-tool-use", "post-tool-use", "permission-request"}
                    else []
                )
            },
            "implementation": {
                "id": identifier,
                "version": RUNTIME_VERSION,
                "content_sha256": runtime_sha,
            },
            "timeout": {"value": 5000, "unit": "milliseconds"},
            "failure_policy": "block",
            "authority": "safety" if identifier == "hard-safety" else "gate",
            "requirement": "required",
            "provenance": provenance(identifier),
        }
        record["semantic_sha256"] = semantic_record_sha256_v2(record)
        evaluators.append(record)
    event_policy: dict[str, Any] = {
        "id": f"{event}-policy",
        "event": event,
        "event_failure_policy": "block",
        "execution": {"mode": "ordered", "concurrency_approval": None},
        "evaluators": evaluators,
        "provenance": provenance(f"{event}-policy"),
    }
    event_policy["semantic_sha256"] = semantic_record_sha256_v2(event_policy)
    instruction = "Run project automation against one normalized provider event."
    instruction_record: dict[str, Any] = {
        "id": "project-automation",
        "order": 10,
        "content": instruction,
        "content_sha256": _sha256(instruction.encode()),
        "provenance": provenance("project-automation"),
    }
    instruction_record["semantic_sha256"] = semantic_record_sha256_v2(instruction_record)
    capabilities = []
    for identifier in ("semantic-hooks", "semantic-instructions"):
        record = {
            "id": identifier,
            "requirement": "required",
            "provenance": provenance(identifier),
        }
        record["semantic_sha256"] = semantic_record_sha256_v2(record)
        capabilities.append(record)
    return compile_harness_policy_v2(
        {
            "schema_version": "2",
            "policy_id": "bearhug-project",
            "policy_version": "2.0.0",
            "provenance": provenance("bearhug-project"),
            "migration": None,
            "instructions": [instruction_record],
            "event_policies": [event_policy],
            "skills": [],
            "agent_roles": [],
            "mcp_servers": [],
            "capabilities": capabilities,
        }
    ).document


def _evaluators(
    root: Path,
    payload: Mapping[str, Any],
    event: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[list[str], list[EvaluatorRegistration]]:
    semantic = event["semantic_event"]

    def registration(identifier: str, call) -> EvaluatorRegistration:
        runtime_sha = _sha256(Path(__file__).read_bytes())
        return EvaluatorRegistration(
            identifier,
            identifier,
            RUNTIME_VERSION,
            runtime_sha,
            call,
        )

    def observe(_event: Mapping[str, Any], _context: EvaluatorContext) -> EvaluatorOutcome:
        return EvaluatorOutcome("continue", _evidence({"status": "observed"}))

    def hard_safety(_event: Mapping[str, Any], _context: EvaluatorContext) -> EvaluatorOutcome:
        tool = event.get("tool") or {}
        if tool.get("family") == "mcp":
            return _block(
                "hard-safety",
                "This tool has no closed write/effect model; use a supported read, patch, or "
                "shell tool.",
            )
        shell = payload.get("tool_input") if isinstance(payload.get("tool_input"), Mapping) else {}
        command = shell.get("command") or shell.get("cmd")
        if tool.get("family") == "shell" and isinstance(command, str):
            try:
                git_commands = _git_commands(command)
            except ProjectHookRuntimeError:
                return _block("hard-safety", "The shell command cannot be parsed safely.")
            if any(subcommand not in _READ_ONLY_GIT for subcommand, _ in git_commands):
                return _block(
                    "hard-safety", "Git mutation is forbidden in a governed provider turn."
                )
        paths = [item["path"] for item in tool.get("effects", [])]
        return EvaluatorOutcome("allow", _evidence({"checked_paths": paths}))

    def task_durability(_event: Mapping[str, Any], _context: EvaluatorContext) -> EvaluatorOutcome:
        effects = (event.get("tool") or {}).get("effects")
        if semantic != "completion-request" and not effects:
            return EvaluatorOutcome("continue", _evidence({"status": "not-applicable"}))
        try:
            from bearhug.project_work import status as work_status

            work = work_status(root, include_campaign=False)
        except Exception:
            return _block(
                "task-durability",
                "Project work authority is unavailable; restore it before writing.",
            )
        permitted = {"in_progress", "completed"} if semantic == "completion-request" else {
            "in_progress"
        }
        session_id = event["identity"]["session_id"]
        owned = [
            row
            for row in work.get("tasks", [])
            if isinstance(row, Mapping)
            and row.get("session_id") == session_id
            and row.get("provider") == "codex"
            and row.get("status") in permitted
        ]
        if work.get("status") != "active" or len(owned) != 1:
            return _block(
                "task-durability",
                "Start or restore exactly one selected project work item for this Codex session "
                "before writing or completing.",
            )
        return EvaluatorOutcome(
            "allow", _evidence({"status": owned[0].get("status"), "task": owned[0].get("id")})
        )

    def checks(_event: Mapping[str, Any], _context: EvaluatorContext) -> EvaluatorOutcome:
        rows = state.get("checks", [])
        if not state.get("last_write_event"):
            return EvaluatorOutcome("continue", _evidence({"status": "not-applicable"}))
        if not rows:
            return _block(
                "project-checks",
                "No validation result exists for this session; make the intended write or run "
                "the configured checks.",
            )
        latest = rows[-1]
        status = latest.get("state")
        if (
            status != "passed"
            or latest.get("event_id") != state.get("last_write_event")
            or latest.get("checkout_sha256") != state.get("current_checkout_sha256")
        ):
            return _block(
                "project-checks",
                f"Project validation is {status or 'unknown'} or stale for the current checkout; "
                "wait for it or rerun the configured commands.",
            )
        return EvaluatorOutcome(
            "allow", _evidence({"status": status, "event_id": latest.get("event_id")})
        )

    def review(_event: Mapping[str, Any], _context: EvaluatorContext) -> EvaluatorOutcome:
        write_event = state.get("source_write_event")
        if write_event and (
            state.get("review_event") != write_event
            or state.get("review_checkout_sha256") != state.get("source_checkout_sha256")
            or state.get("review_checkout_sha256") != state.get("current_checkout_sha256")
        ):
            paths = [item["path"] for item in (event.get("tool") or {}).get("effects", [])]
            return _block(
                "review-gate",
                "Source changed after the last completed adversarial review; dispatch and finish "
                "an independent reviewer. Require its final response to include exactly "
                f"CANDIDATE_SHA256: {state.get('source_checkout_sha256')} and VERDICT: APPROVE.",
                paths,
            )
        return EvaluatorOutcome("allow", _evidence({"reviewed_event": state.get("review_event")}))

    def dlv(_event: Mapping[str, Any], _context: EvaluatorContext) -> EvaluatorOutcome:
        write_event = state.get("go_write_event")
        if write_event and (
            state.get("dlv_event") != write_event
            or state.get("dlv_checkout_sha256") != state.get("current_checkout_sha256")
        ):
            return _block(
                "dlv-verify-gate",
                "Go source changed after the last Delve session; run dlv test/debug/exec and "
                "retry Stop.",
            )
        return EvaluatorOutcome("allow", _evidence({"verified_event": state.get("dlv_event")}))

    def join_key(_event: Mapping[str, Any], _context: EvaluatorContext) -> EvaluatorOutcome:
        try:
            from bearhug.project_work import status as work_status

            work = work_status(root, include_campaign=False)
        except Exception:
            return _block("joinkey-lint", "Project work authority cannot be parsed unambiguously.")
        if work.get("status") in {"conflict", "unavailable"}:
            return _block("joinkey-lint", "Resolve the project work/BOARD authority conflict.")
        return EvaluatorOutcome("allow", _evidence({"authority_status": work.get("status")}))

    def response_shape(_event: Mapping[str, Any], _context: EvaluatorContext) -> EvaluatorOutcome:
        message = payload.get("last_assistant_message")
        if not isinstance(message, str) or not message.strip():
            return _block(
                "response-shape", "Provide a concrete final response before ending the turn."
            )
        if _count_asks is None or _prose_lines is None:
            return _block(
                "response-shape", "The promoted response-shape evaluator is unavailable."
            )
        asks = _count_asks(_prose_lines(message))
        if asks >= 2:
            return _block("response-shape", "Send one answerable question in this turn.")
        if len(message) >= LONG_CHARS and asks == 0:
            return _block(
                "response-shape",
                "Condense the final response or make the required question explicit.",
            )
        return EvaluatorOutcome("allow", _evidence({"characters": len(message)}))

    if semantic == "pre-tool-use":
        pairs = [("hard-safety", hard_safety), ("task-durability", task_durability)]
    elif semantic == "completion-request" and payload.get("stop_hook_active") is True:
        pairs = [("lifecycle-observer", observe)]
    elif semantic == "completion-request":
        pairs = [
            ("project-checks", checks),
            ("dlv-verify-gate", dlv),
            ("task-durability", task_durability),
            ("joinkey-lint", join_key),
            ("review-gate", review),
            ("response-shape", response_shape),
        ]
    else:
        pairs = [("lifecycle-observer", observe)]
    return [identifier for identifier, _ in pairs], [registration(*pair) for pair in pairs]


def evaluate_codex_hook(
    root: Path,
    raw: bytes,
    payload: Mapping[str, Any],
    *,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Normalize, enforce and journal one installed Codex hook invocation."""

    root = root.resolve(strict=True)
    config = _config(root)
    event = _normalized_event(root, raw, payload, occurred_at or datetime.now(UTC))
    session = event["identity"]["session_id"]
    native = payload["hook_event_name"]
    tool = event.get("tool") or {}
    paths = [item["path"] for item in tool.get("effects", [])]
    successful_write = native == "PostToolUse" and tool.get("status") == "succeeded" and bool(paths)
    checkout = _checkout_identity(root)

    with _state_lock(root, session):
        state = _session_state(root, session)
        _reconcile_check_workers(state)
        previous_checkout = state.get("current_checkout_sha256")
        first_seen_dirty = previous_checkout is None and bool(checkout["paths"])
        checkout_changed = (
            isinstance(previous_checkout, str) and previous_checkout != checkout["sha256"]
        )
        if native == "SessionStart":
            state["interrupted"] = False
        elif native == "Interrupt":
            state["interrupted"] = True
        elif successful_write:
            state["last_write_event"] = event["event_id"]
            source_paths = [path for path in paths if path.endswith(_SOURCE_SUFFIXES)]
            if source_paths:
                state["source_write_event"] = event["event_id"]
                state["source_checkout_sha256"] = checkout["sha256"]
                state["review_event"] = None
                state["review_checkout_sha256"] = None
                state["review_receipt"] = None
                state["active_review_agents"] = {}
            if any(path.endswith(".go") for path in paths):
                state["go_write_event"] = event["event_id"]
                state["dlv_event"] = None
                state["dlv_checkout_sha256"] = None
        elif native == "PostToolUse" and event["tool"]["family"] == "shell":
            tool_input = payload.get("tool_input") or {}
            command = tool_input.get("command") or tool_input.get("cmd")
            if (
                isinstance(command, str)
                and event["tool"].get("status") == "succeeded"
                and _response_success(payload.get("tool_response")) is True
                and _real_dlv_subcommand is not None
                and _real_dlv_subcommand(command) is not None
            ):
                state["dlv_event"] = state.get("go_write_event")
                state["dlv_checkout_sha256"] = checkout["sha256"]
        elif native == "SubagentStart" and payload.get("agent_type") in _REVIEW_AGENTS:
            agent_id = payload.get("agent_id")
            if isinstance(agent_id, str) and agent_id:
                state.setdefault("active_review_agents", {})[agent_id] = {
                    "source_write_event": state.get("source_write_event"),
                    "checkout_sha256": checkout["sha256"],
                }
        elif native == "SubagentStop" and payload.get("agent_type") in _REVIEW_AGENTS:
            agent_id = payload.get("agent_id")
            message = payload.get("last_assistant_message")
            active = state.setdefault("active_review_agents", {})
            started_for = active.pop(agent_id, None) if isinstance(agent_id, str) else None
            verdicts = (
                re.findall(
                    r"(?im)^\s*(?:verdict\s*:\s*)?(APPROVE|REQUEST-CHANGES)\s*$",
                    message,
                )
                if isinstance(message, str)
                else []
            )
            candidate_lines = (
                re.findall(r"(?im)^\s*CANDIDATE_SHA256:\s*([0-9a-f]{64})\s*$", message)
                if isinstance(message, str)
                else []
            )
            if (
                isinstance(started_for, Mapping)
                and started_for.get("source_write_event") == state.get("source_write_event")
                and started_for.get("checkout_sha256") == checkout["sha256"]
                and checkout["sha256"] == state.get("source_checkout_sha256")
                and candidate_lines == [checkout["sha256"]]
                and verdicts
                and verdicts[-1].upper() == "APPROVE"
                and "REQUEST-CHANGES" not in {item.upper() for item in verdicts}
            ):
                state["review_event"] = state.get("source_write_event")
                state["review_checkout_sha256"] = checkout["sha256"]
                state["review_receipt"] = {
                    "agent_id_sha256": _sha256(agent_id.encode()),
                    "agent_type": payload.get("agent_type"),
                    "candidate_sha256": checkout["sha256"],
                    "terminal_state": "completed",
                    "verdict": "APPROVE",
                    "message_sha256": _sha256(message.encode()),
                }
        if checkout_changed and not successful_write:
            # A mutation that was not attributed to the just-finished tool is still a mutation.
            # Invalidate all content-bound gates rather than trusting an incomplete tool model.
            state["last_write_event"] = event["event_id"]
            state["source_write_event"] = event["event_id"]
            state["source_checkout_sha256"] = checkout["sha256"]
            state["review_event"] = None
            state["review_checkout_sha256"] = None
            state["review_receipt"] = None
            state["active_review_agents"] = {}
            state["dlv_event"] = None
            state["dlv_checkout_sha256"] = None
        elif first_seen_dirty and not successful_write:
            # An upgrade, move or newly observed session cannot import old project-local passes.
            # Conservatively acquire obligations for the already-dirty candidate instead.
            state["last_write_event"] = event["event_id"]
            if any(path.endswith(_SOURCE_SUFFIXES) for path in checkout["paths"]):
                state["source_write_event"] = event["event_id"]
                state["source_checkout_sha256"] = checkout["sha256"]
            if any(path.endswith(".go") for path in checkout["paths"]):
                state["go_write_event"] = event["event_id"]
        state["current_checkout_sha256"] = checkout["sha256"]
        state["automation_candidates"] = _automation_candidates(root, config)
        _atomic_json(_state_path(root, session), state)

    if successful_write:
        try:
            _format(root, config, paths)
        except ProjectHookRuntimeError:
            with _state_lock(root, session):
                state = _session_state(root, session)
                _record_formatter_failure(root, session, event["event_id"], state)
            raise
        with _state_lock(root, session):
            state = _session_state(root, session)
            formatted_checkout = _checkout_identity(root)
            state["current_checkout_sha256"] = formatted_checkout["sha256"]
            if state.get("source_write_event") == event["event_id"]:
                state["source_checkout_sha256"] = formatted_checkout["sha256"]
            _start_checks(root, session, event["event_id"], state)
    elif first_seen_dirty:
        # Do not format pre-existing user edits. Run only the protected admitted validation
        # policy and require fresh review/Delve evidence where the dirty source demands it.
        with _state_lock(root, session):
            state = _session_state(root, session)
            _start_checks(root, session, event["event_id"], state)

    runtime_sha = _sha256(Path(__file__).read_bytes())
    identifiers, registrations = _evaluators(root, payload, event, state)
    dispatcher = OrderedHookDispatcher(
        policy=_policy(event["semantic_event"], identifiers, runtime_sha),
        registrations=registrations,
        runtime_id=RUNTIME_ID,
        runtime_version=RUNTIME_VERSION,
        runtime_sha256=runtime_sha,
        total_timeout_ms=12_000,
    )
    authority = _authority_root(root)
    journal = authority / ".bearhug" / "normalized-hooks" / "v1" / _sha256(session.encode())
    event_path = append_normalized_hook_record(event, journal, project_state_root=authority)
    result = dispatcher.dispatch(event)
    result_path = append_normalized_hook_record(result, journal, project_state_root=authority)
    for source in (event_path, result_path):
        with contextlib.suppress(OSError, ValueError, ProjectHookRuntimeError):
            target = root / source.relative_to(authority)
            _assert_project_path(root, target)
            _atomic_json(target, _load_json(source, maximum=_STATE_LIMIT, label="host journal"))
    return result


def configure_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="automation_command", required=True)
    checks = sub.add_parser("run-checks")
    checks.add_argument("--root", type=Path, required=True)
    checks.add_argument("--session", required=True)
    checks.add_argument("--event-id", required=True)
    admission = sub.add_parser(
        "admit-validator", help="approve one hash-checked discovered validator"
    )
    admission.add_argument("--root", type=Path, required=True)
    admission.add_argument("--path", required=True)
    admission.add_argument("--sha256", required=True)


def run(args: argparse.Namespace) -> int:
    try:
        if args.automation_command == "run-checks":
            return _run_checks(args.root.resolve(strict=True), args.session, args.event_id)
        if args.automation_command == "admit-validator":
            print(json.dumps(admit_validator(args.root, args.path, args.sha256), sort_keys=True))
            return 0
        return 2
    except (OSError, ProjectHookRuntimeError) as exc:
        print(f"bearhug automation: {exc}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bearhug.project_hook_runtime")
    configure_parser(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
