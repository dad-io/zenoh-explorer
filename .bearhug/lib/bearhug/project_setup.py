"""Install the portable Bear Hug components into one explicit Git checkout.

The component builder returns inventory schema version 1 with the fixed ``components`` keys
``runtime``, ``stop_coordinator``, ``boardrows``, ``memexlint``, ``memex_hook``, ``memq``, and
``graft`` and ``codebase_memory``. Each record names an absolute bundle path (and, for trees,
its file manifest); a non-empty ``missing`` list prevents installation. This module maps that
inventory to the target runtime, provider configuration, memory catalog and project MCP settings.
It owns the target transaction and receipt, while leaving provider sessions and global trust
outside setup.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from bearhug.campaign.grounding import (
    EXCLUDE_PATHS_ENV_KEY,
    GroundingError,
    normalize_exclude_paths,
)
from bearhug.lint.reachability import literal_tool_union
from bearhug.paths import REPO_ROOT


class ProjectSetupError(RuntimeError):
    """The target or portable setup plan cannot be safely applied."""


class EmbeddingServiceError(ProjectSetupError):
    """A service setting needs attention before project installation."""

    def __init__(self, message: str, setting: str = "embed_url") -> None:
        super().__init__(message)
        self.setting = setting


class ProjectSetupConflict(ProjectSetupError):
    """A target file differs from the bytes previously owned by setup."""


_PROVIDERS = frozenset({"claude", "codex"})
_SETUP_RECEIPT = ".bearhug/project-setup.json"
_ENV_FILE = ".bearhug/setup.env"
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_COMMAND_TIMEOUT = 300.0

# Measured on a native probe (2026-09-20, Claude Code 2.1.275): Claude Code reads a command
# hook's `timeout` field in seconds, not
# milliseconds -- a hook registered `"timeout": 30` ran a 4-second sleep to completion, while
# one registered `"timeout": 1` was killed mid-sleep. These three values, as the bundled Graft
# package emits them, are therefore not millisecond values misreading Claude's unit; they are
# real, second-valued ceilings (over two hours for the smallest) far larger than any hook body
# needs. Renamed from `_GRAFT_CLAUDE_TIMEOUT_MS`, which encoded the unit-confusion theory this
# measurement disproved.
_GRAFT_CLAUDE_TIMEOUT_CEILINGS = frozenset({8000, 10000, 15000})
# The same values after `_normalize_graft_claude_settings`'s division by 1000 --
# the timeout a setup-desired Graft handler carries once staged.  `_merge_hooks` uses this to
# recognize a stale on-disk, unshrunk ceiling on a handler this run itself desires and rewrite
# it to the value a fresh install would have written.
_GRAFT_CLAUDE_TIMEOUT_SECONDS = frozenset(value // 1000 for value in _GRAFT_CLAUDE_TIMEOUT_CEILINGS)

# Explicit timeouts (seconds) for Bear Hug's own memex, memq and Stop coordinator Claude
# hook registrations, which previously carried none and so ran under Claude Code's own
# default. Each value is at least three times a measured maximum and stays clear of
# `_GRAFT_CLAUDE_TIMEOUT_SECONDS`, so a value here is never shortened by the rewrite
# above even if some future Claude-settings identity happened to collide with it — see
# `docs/OPERATING-MODES.md`'s timeout paragraph for the corpus and transcript sizes,
# the measured medians and maxima, and the rounding.
#
# `memex-hook.sh`'s four `memexlint`-backed advisory subcommands (`session-start`,
# `pre-decide`, `post-edit`, `stop`) each answer from the same small, in-memory decision
# corpus (a few hundred records measured) and finished in tens of milliseconds at the
# measured corpus size; five seconds is a wide margin over that and still short enough
# that a genuine hang is caught quickly. None of the four can deny a tool call: each
# exits 0 and injects nothing on any internal failure, by the hook's own design, so a
# kill at this timeout only ever costs lost advisory context.
_MEMEX_HOOK_TIMEOUT_SECONDS = 5
# `pre-question` does not go through `memexlint`. It used to be the one subcommand in
# this set that could DENY a tool call (`samgate`, blocking a not-yet-acknowledged
# AskUserQuestion); the owner's decision on 2026-09-25 retired that, and a second
# decision the same day replaced its raw word-overlap search with
# `memex-pre-question-score.py` scoring the question through
# `bearhug.campaign.grounding.select_decisions` -- the grounding engine mode 3
# onboarding uses, reused wholesale rather than re-implemented, after owner-supplied
# evidence found the old search surfacing mostly false positives on common words. That
# script carries its own hard `SIGALRM` time bound (2 seconds by default) and fails open
# to no advice on any error, including that deadline. `pre-question` is kept on its own,
# wider Claude-registered timeout rather than being folded into
# `_MEMEX_HOOK_TIMEOUT_SECONDS` above: the value was originally sized for the risk of a
# killed blocking gate silently letting an unacknowledged question through, and while
# that risk is gone (the gate can no longer deny), the internal 2-second bound is what
# actually limits this subcommand's cost now, so the wider outer registration is a
# courtesy ceiling above it rather than a value worth re-deriving from scratch.
_MEMEX_PRE_QUESTION_TIMEOUT_SECONDS = 30
# The Stop coordinator reads the turn's transcript up to several times (once per
# evaluator that needs it) and a long session's transcript can run to many megabytes, so
# its cost scales with session length rather than staying flat like the corpus-bound
# subcommands above. Measured well under a second against a long synthetic session
# transcript; thirty seconds keeps a wide margin for a much longer real session while
# still catching a genuine hang inside one turn.
_STOP_COORDINATOR_TIMEOUT_SECONDS = 30
# MemQ is an external binary (see `docs/RUNBOOK.md`'s MemQ prerequisite) not available to
# measure directly here. Its `UserPromptSubmit` hook does the same kind of per-turn
# document search and injection as memex's own corpus hooks, optionally through a local
# embedding service, so it is given the same generous margin as the Stop coordinator
# rather than the corpus hooks' tighter one, until it can be measured directly.
_MEMQ_HOOK_TIMEOUT_SECONDS = 30

# Graft's Claude helpers and skill are project-facing extension files.  Established projects may
# have a richer or locally patched copy, so setup adopts that copy instead of treating it as a
# foreign-file conflict.  Fresh projects still receive the staged Graft files unchanged.
_GRAFT_PROJECT_FILES = frozenset(
    {
        ".claude/helpers/graft-hooks.cjs",
        ".claude/helpers/graft-statusline.cjs",
        ".claude/skills/graft/SKILL.md",
    }
)
# The single source of truth for the Codebase Memory launcher's repository-
# relative path -- used by `_component_specs` to build its `_FileSpec` (the one place a receipt
# row ever gets `component: "codebase_memory"`) and reused, unchanged, as the closed allowlist a
# disabled re-run's removal path checks a receipt row's `path` against. The two
# sides cannot drift apart because they are the same Python name, not two copies of the string.
_CODEBASE_MEMORY_LAUNCHER_PATH = "scripts/bin/codebase-memory-mcp"
# Closed allowlist. `_process_disabled_codebase_memory` never removes a
# `codebase_memory`-labelled receipt row, however its sha256 checks out, unless its `path` is a
# member of this exact set -- see that function's own docstring for the full reasoning.
_CODEBASE_MEMORY_KNOWN_PATHS = frozenset({_CODEBASE_MEMORY_LAUNCHER_PATH})
# The single source of truth for the "codebase-memory-mcp" MCP server entry, used
# both when `_provider_specs` adds it (`include_codebase_memory=True`) and when a disabled
# re-run decides whether an on-disk entry is provably Bear Hug's own to remove -- an entry is
# removed only if it is byte-for-byte one of these two literals (see
# `_process_disabled_codebase_memory`).
_CODEBASE_MEMORY_MCP_ENTRY = {"command": _CODEBASE_MEMORY_LAUNCHER_PATH, "args": []}
_CODEBASE_MEMORY_CODEX_ENTRY = {
    "command": _CODEBASE_MEMORY_LAUNCHER_PATH,
    "args": [],
    "cwd": ".",
}


def _canonical_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProjectSetupError(f"setup record is not canonical JSON: {exc}") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _physical_directory(value: Path | str, label: str) -> Path:
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        raise ProjectSetupError(f"{label} must be an explicit absolute path")
    if supplied.is_symlink():
        raise ProjectSetupError(f"{label} may not be a symlink")
    try:
        resolved = supplied.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise ProjectSetupError(f"cannot resolve {label}: {supplied}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProjectSetupError(f"{label} must be a physical directory")
    return resolved


def _git_root(root: Path) -> None:
    """Check Git identity without requiring a non-empty history.

    A newly created repository with no commit is a valid setup target.  We only need the exact
    worktree root and therefore do not ask Git for HEAD, branch, or a clean index.
    """

    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            ("git", "-C", str(root), "rev-parse", "--show-toplevel"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectSetupError(f"cannot inspect target Git checkout: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise ProjectSetupError(f"target is not a Git worktree: {detail}")
    try:
        observed = Path(result.stdout.decode().strip()).resolve(strict=True)
    except (OSError, UnicodeError) as exc:
        raise ProjectSetupError("Git returned an invalid worktree root") from exc
    if observed != root:
        raise ProjectSetupError(
            f"target must name the exact Git worktree root, observed {observed}"
        )


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ProjectSetupError(f"{label} is not a repository-relative path")
    path = PurePosixPath(value)
    if str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ProjectSetupError(f"{label} is not a canonical repository-relative path")
    return value


def _assert_safe_ancestors(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ProjectSetupError(f"setup path escapes target: {path}") from exc
    current = root
    for part in relative.parts[:-1]:
        current /= part
        try:
            if current.is_symlink():
                raise ProjectSetupError(
                    f"refusing to traverse symlinked project directory: {current}"
                )
        except OSError as exc:
            raise ProjectSetupError(f"cannot inspect project directory: {current}") from exc


def _assert_physical_ancestors(path: Path) -> None:
    """Refuse a cache path whose parent chain could redirect an assembly write."""

    for ancestor in (path.parent, *path.parent.parents):
        try:
            if ancestor.is_symlink():
                raise ProjectSetupError(f"path may not traverse a symlink: {ancestor}")
        except OSError as exc:
            raise ProjectSetupError(f"cannot inspect path ancestor: {ancestor}") from exc


def _target_file(root: Path, relative: str) -> Path:
    path = root / _relative_path(relative, "setup file path")
    _assert_safe_ancestors(root, path)
    if path.is_symlink():
        raise ProjectSetupConflict(f"refusing to replace symlink: {path}")
    return path


def _read_regular(path: Path, label: str) -> tuple[bytes, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProjectSetupError(f"cannot inspect {label}: {exc}") from exc
    if metadata.st_nlink != 1 or not stat.S_ISREG(metadata.st_mode):
        raise ProjectSetupConflict(f"{label} must be one regular non-hardlinked file")
    if metadata.st_size > _MAX_FILE_BYTES:
        raise ProjectSetupError(f"{label} exceeds the byte limit")
    try:
        return path.read_bytes(), stat.S_IMODE(metadata.st_mode)
    except OSError as exc:
        raise ProjectSetupError(f"cannot read {label}: {exc}") from exc


def _source_bytes(value: Any, *, label: str) -> tuple[bytes, int]:
    if "content" in value:
        content = value["content"]
        if isinstance(content, str):
            content = content.encode()
        if not isinstance(content, bytes):
            raise ProjectSetupError(f"{label}.content must be bytes or text")
        mode = value.get("mode", "0644")
    elif "source" in value:
        source = Path(value["source"]).expanduser()
        if not source.is_absolute() or source.is_symlink():
            raise ProjectSetupError(f"{label}.source must be an absolute non-symlink file")
        observed = _read_regular(source, f"source {source}")
        if observed is None:
            raise ProjectSetupError(f"source file does not exist: {source}")
        content, observed_mode = observed
        mode = value.get("mode", f"{observed_mode:04o}")
    else:
        raise ProjectSetupError(f"{label} needs source or content")
    if not isinstance(mode, (str, int)):
        raise ProjectSetupError(f"{label}.mode is invalid")
    try:
        mode_int = int(mode, 8) if isinstance(mode, str) else mode
    except ValueError as exc:
        raise ProjectSetupError(f"{label}.mode is invalid") from exc
    if mode_int not in {0o600, 0o644, 0o755}:
        raise ProjectSetupError(f"{label}.mode must be 0600, 0644, or 0755")
    if len(content) > _MAX_FILE_BYTES:
        raise ProjectSetupError(f"{label} exceeds the byte limit")
    return content, mode_int


def _normalize_graft_claude_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Shrink Graft's inherited Claude timeout ceilings to a sane hook-body bound.

    Measured on a native probe (2026-09-20, Claude Code 2.1.275): Claude Code reads a command
    hook's `timeout` field in seconds, not
    milliseconds. The bundled Graft package's own values (`_GRAFT_CLAUDE_TIMEOUT_CEILINGS`) are
    therefore not millisecond values that need converting to seconds; they are real,
    second-valued ceilings, each far larger than any hook body needs. This divides them by 1000
    to a sane bound instead -- a deliberate tightening, not a unit correction.

    The values are emitted by the bundled Graft package, not authored by the target project.
    Keep this projection scoped to that staged settings document so a user's existing hooks are
    merged byte-for-byte with their original timeout values.
    """
    normalized = copy.deepcopy(dict(settings))
    hooks = normalized.get("hooks")
    if not isinstance(hooks, dict):
        return normalized
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            registered = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(registered, list):
                continue
            for hook in registered:
                if not isinstance(hook, dict):
                    continue
                timeout = hook.get("timeout")
                if (
                    isinstance(timeout, int)
                    and not isinstance(timeout, bool)
                    and timeout in _GRAFT_CLAUDE_TIMEOUT_CEILINGS
                ):
                    hook["timeout"] = timeout // 1000
    return normalized


@dataclass(frozen=True, slots=True)
class _FileSpec:
    path: str
    content: bytes
    mode: int
    component: str
    merge: str | None = None
    providers: tuple[str, ...] = ()
    #: The `(type, command)` identities `_provider_specs` read from the
    #: staged Graft bundle's own settings, AFTER `_normalize_graft_claude_settings` ran, at the
    #: moment the `.claude/settings.json` spec is built. Carried on the spec (not recomputed
    #: later) because that is the one place the document still distinguishes a Graft-staged
    #: identity from a Bear Hug-owned one — by the time this spec's `content` is merged with the
    #: target's on-disk file, they are one document. Empty for every other spec.
    normalize_identities: frozenset[tuple[Any, Any]] = frozenset()


@dataclass(frozen=True, slots=True)
class _CommandSpec:
    component: str
    argv: tuple[str, ...]
    timeout: float = 120.0
    required: bool = True


@dataclass(frozen=True, slots=True)
class SetupResult:
    """Reviewable setup result returned by :func:`setup_project`."""

    target: Path
    providers: tuple[str, ...]
    dry_run: bool
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    unsupported: tuple[str, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    receipt_path: Path | None
    commands: tuple[dict[str, Any], ...]
    #: Every Claude hooks.json registration this run added — on a fresh
    #: install, everything; on a repeat run, only what was actually missing (including a
    #: registration an operator removed by hand, which setup still silently restores). Each
    #: entry is `{"event", "matcher", "command"}`. Defaulted so an older positional construction
    #: (a test's own stand-in SetupResult, for one) keeps working unchanged.
    added_registrations: tuple[dict[str, str], ...] = ()
    #: Every Claude hooks.json registration this run deleted from Bear Hug's
    #: own exact-matcher group because a different-spelling, equal-tool-set group already held a
    #: byte-identical copy of it — the duplicate an older, unrepaired install left behind. Each
    #: entry is `{"event", "matcher", "command", "kept_under"}`, where `kept_under` names the
    #: matcher of the group that still holds it. Defaulted for the same reason as
    #: `added_registrations`.
    removed_registrations: tuple[dict[str, str], ...] = ()
    #: Every identity setup found ALSO held by a differently
    #: spelled, equal-tool-set group (the same `literal_tool_union` equivalence the hook audit
    #: uses) whose handler for it is not byte-identical -- an edited handler never justifies
    #: anything. Either setup already held a copy of its own and declined to delete it, or its
    #: own exact-matcher group did not exist yet and this run ADDED its own copy rather than
    #: leave a weaker foreign copy standing alone. Either way both
    #: registrations end up present and the deletion rule itself (`removed_registrations`,
    #: above) is unchanged. Each entry is `{"event", "matcher",
    #: "kept_under", "type", "command", "differs"}`, where `matcher` is setup's own group,
    #: `kept_under` is the surviving foreign group, `type`/`command` name the identity (fix
    #: round 2, N4: both fields are new in this same change, so there is no established schema
    #: for `type` to break), and `differs` lists the handler keys whose values differ between
    #: them. Defaulted for the same reason as `added_registrations`.
    kept_duplicates: tuple[dict[str, Any], ...] = ()
    #: Every EXISTING Claude-settings registration whose identity
    #: this run itself desires, whose on-disk `timeout` was one of `_GRAFT_CLAUDE_TIMEOUT_CEILINGS`
    #: (a Graft millisecond value never converted because it predates
    #: `_normalize_graft_claude_settings`), and whose desired handler's timeout equals that
    #: value divided by 1000 -- rewritten in place to the desired (seconds) value. Every other
    #: key of the existing handler, and every foreign registration, is untouched. Each entry is
    #: `{"event", "matcher", "type", "command", "old", "new"}` (fix round 2, N4: `type` added
    #: since this field is new in this same change). Defaulted for the same reason as
    #: `added_registrations`.
    normalized_timeouts: tuple[dict[str, Any], ...] = ()
    #: One entry per optional external CLI setup detects
    #: but never installs or downloads (today, only `gopls`). Each entry is `{"name", "found",
    #: "path", "version"}`; `found` is False only when the tool is absent from PATH, and a probe
    #: failure once found (non-zero exit, timeout, or an OS error launching it) still reports
    #: `found: true` with `version: null` -- never an error. Defaulted for the same reason as
    #: `added_registrations`.
    external_tools: tuple[dict[str, Any], ...] = ()
    #: One entry per component this run removed
    #: the no-longer-wanted pieces of (today, only ever `codebase_memory`; item 4 forbids
    #: generalizing the rule that produces this to any other component). Each entry is
    #: `{"component", "files_removed", "entries_removed", "kept_modified", "kept_unknown_path",
    #: "kept_unsafe_path", "kept_foreign", "left_data"}`; `files_removed` and `entries_removed`
    #: list what was actually deleted or stripped (predicted, not yet done, on a dry run);
    #: `kept_modified` lists a receipt-owned file left in place because its bytes no longer match
    #: what Bear Hug wrote; `kept_unknown_path` lists a `codebase_memory`-labelled
    #: receipt row whose path is not one of the component's own known paths -- never even read
    #: from disk; `kept_unsafe_path` lists one whose path is a symlink, escapes the
    #: project, or is absolute -- decided lexically, never dereferenced; `kept_foreign` lists a
    #: provider config file whose `codebase-memory-mcp` entry was left because it is not
    #: byte-for-byte what setup would generate; `left_data` lists `.bearhug/codebase-memory/`
    #: when present -- always data, never removed. Empty when the component was never installed.
    #: Defaulted for the same reason as `added_registrations`.
    removed_components: tuple[dict[str, Any], ...] = ()
    #: Every identity setup declined to add for the same reason
    #: `removed_registrations` reports -- a different-spelling, equal-tool-set group already
    #: holds a byte-identical copy of it -- but where Bear Hug's own exact-matcher group did not
    #: exist in the file yet, so there was nothing to delete FROM: the identity was simply left
    #: out of the group setup would otherwise have created. Each entry is `{"event", "matcher",
    #: "command", "held_under"}`, where `matcher` is the group Bear Hug would have created and
    #: `held_under` names the surviving foreign group's matcher exactly as written in the file
    #: (padding included). Recording this changes nothing about which identities are added --
    #: the same ones were already left out before this field existed, unreported. Defaulted for
    #: the same reason as `added_registrations`.
    already_held_registrations: tuple[dict[str, str], ...] = ()
    #: Every EXISTING Claude-settings registration whose identity this run itself
    #: desires, found with no `timeout` key at all, that had the desired timeout added to it in
    #: place -- the memex, memq and Stop coordinator registrations shipped for a time with none.
    #: Every other key of the existing handler, and every foreign registration, is untouched.
    #: Each entry is `{"event", "matcher", "type", "command", "timeout"}`, where `timeout` is the
    #: value written (there is no "old" value to report: the key was absent, not different).
    #: Defaulted for the same reason as `added_registrations`.
    timeout_filled: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_mapping(self) -> dict[str, Any]:
        return {
            "record_kind": "project_setup_result",
            "target": self.target.as_posix(),
            "providers": list(self.providers),
            "dry_run": self.dry_run,
            "ok": self.ok,
            "changed": list(self.changed),
            "unchanged": list(self.unchanged),
            "unsupported": list(self.unsupported),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "receipt_path": self.receipt_path.as_posix() if self.receipt_path else None,
            "commands": list(self.commands),
            "added_registrations": [dict(entry) for entry in self.added_registrations],
            "removed_registrations": [dict(entry) for entry in self.removed_registrations],
            "kept_duplicates": [dict(entry) for entry in self.kept_duplicates],
            "normalized_timeouts": [dict(entry) for entry in self.normalized_timeouts],
            "external_tools": [dict(entry) for entry in self.external_tools],
            "removed_components": [dict(entry) for entry in self.removed_components],
            "already_held_registrations": [
                dict(entry) for entry in self.already_held_registrations
            ],
            "timeout_filled": [dict(entry) for entry in self.timeout_filled],
        }


def _component_path(components: Mapping[str, Any], name: str, *, directory: bool = False) -> Path:
    record = components.get(name)
    if not isinstance(record, Mapping) or not isinstance(record.get("path"), str):
        raise ProjectSetupError(f"portable component inventory is missing {name}.path")
    path = Path(record["path"]).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise ProjectSetupError(f"portable component {name} path is not a physical absolute path")
    if directory and not path.is_dir():
        raise ProjectSetupError(f"portable component {name} directory is unavailable: {path}")
    if not directory and not path.is_file():
        raise ProjectSetupError(f"portable component {name} file is unavailable: {path}")
    return path


def _component_specs(
    inventory: Mapping[str, Any], *, providers: Sequence[str], target: Path
) -> list[_FileSpec]:
    if inventory.get("schema_version") != 1:
        raise ProjectSetupError("unsupported portable component inventory schema")
    missing = inventory.get("missing", [])
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise ProjectSetupError("portable component inventory has an invalid missing list")
    if missing:
        raise ProjectSetupError("required portable components are missing: " + ", ".join(missing))
    components = inventory.get("components")
    if not isinstance(components, Mapping):
        raise ProjectSetupError("portable component inventory has no components map")
    specs: list[_FileSpec] = []

    runtime = _component_path(components, "runtime", directory=True)
    for source in sorted(runtime.rglob("*")):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(runtime).as_posix()
        content, mode = _source_bytes({"source": str(source)}, label=f"runtime/{relative}")
        specs.append(
            _FileSpec(
                f"scripts/hooks/_bearhug/{relative}",
                content,
                mode,
                "runtime",
            )
        )

    for name, destination in (
        ("stop_coordinator", "scripts/hooks/stop-coordinator.py"),
        ("boardrows", "scripts/hooks/boardrows.py"),
        ("memex_hook", "scripts/hooks/memex-hook.sh"),
    ):
        source = _component_path(components, name)
        # Memex is a project extension rather than a Bear Hug runtime primitive.  Barracuda and
        # other established projects may have a richer hook at this path; adopt it in place and
        # leave future setup runs observing the project's current bytes.  The coordinator and
        # board parser remain Bear Hug-owned and retain normal receipt/conflict handling.
        existing = (
            _read_regular(_target_file(target, destination), f"existing {destination}")
            if name == "memex_hook"
            else None
        )
        content, mode = existing or _source_bytes({"source": str(source)}, label=name)
        specs.append(_FileSpec(destination, content, mode, name))

    for name, destination in (
        ("memexlint", "scripts/bin/memexlint"),
        ("memq", "scripts/bin/memq-bin"),
    ):
        source = _component_path(components, name)
        content, mode = _source_bytes({"source": str(source), "mode": "0755"}, label=name)
        specs.append(_FileSpec(destination, content, mode, name))

    _check_duplicate_specs(specs)
    launcher = b"""#!/bin/sh
# Bind memory operations to this installation, including linked Git worktrees.
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd) || exit 2
if [ -f "$root/.bearhug/setup.env" ]; then
    while IFS='=' read -r key value; do
        case "$key" in
            MEMQ_EMBED_URL|MEMQ_EMBED_MODEL) export "$key=$value" ;;
        esac
    done < "$root/.bearhug/setup.env"
fi
export MEMQ_REPO="$root" MEMQ_CONFIG="$root/.memq.json" MEMQ_DB="$root/.memq/db"
exec "$root/scripts/bin/memq-bin" "$@"
"""
    specs.append(_FileSpec("scripts/bin/memq", launcher, 0o755, "memq"))
    # Codebase Memory is optional. `build_components` (setup_components.py)
    # omits the "codebase_memory" key from `components` entirely unless the caller named an
    # explicit binary; when it is absent, install nothing for it at all -- no launcher, and (by
    # extension, in `_provider_specs` and `setup_project` below) no MCP server entry in either
    # provider's config and no post-setup auto-index command.
    codebase_record = components.get("codebase_memory")
    if isinstance(codebase_record, Mapping):
        # The Codebase Memory server is a single ~256 MB static binary. It is not vendored into
        # the checkout: the launcher execs the content-addressed copy in Bear Hug's component
        # bundle, whose path and digest are recorded here and in the setup receipt. Copying it
        # would exceed the setup file limit and duplicate it in every worktree.
        codebase = _component_path(components, "codebase_memory")
        codebase_sha = ""
        if isinstance(codebase_record.get("sha256"), str):
            codebase_sha = codebase_record["sha256"]
        if not codebase_sha:
            digest = hashlib.sha256()
            try:
                with codebase.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise ProjectSetupError(f"cannot read codebase-memory-mcp bundle: {exc}") from exc
            codebase_sha = digest.hexdigest()
        if any(ch in str(codebase) for ch in "\"$`\\\n"):
            raise ProjectSetupError(
                "codebase-memory-mcp bundle path contains unsafe shell characters"
            )
        codebase_launcher = (
            b"#!/bin/sh\n"
            b"# Keep this MCP server and its cache bound to the selected checkout.\n"
            b"# The server binary is the pinned Bear Hug component bundle, not a vendored copy.\n"
            + f"# bundle sha256: {codebase_sha or 'unrecorded'}\n".encode()
            + b'root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd) || exit 2\n'
            b'cd -- "$root" || exit 2\n'
            b'export CBM_CACHE_DIR="$root/.bearhug/codebase-memory"\n'
            + f'bin="{codebase}"\n'.encode()
            + b'[ -x "$bin" ] || {\n'
            b'    echo "codebase-memory-mcp: bundle binary missing: $bin" >&2\n'
            b'    exit 2\n'
            b'}\n'
            b'exec "$bin" "$@"\n'
        )
        specs.append(
            _FileSpec(_CODEBASE_MEMORY_LAUNCHER_PATH, codebase_launcher, 0o755, "codebase_memory")
        )
    return specs


def _check_duplicate_specs(specs: Sequence[_FileSpec]) -> None:
    paths: set[str] = set()
    for spec in specs:
        if spec.path in paths:
            raise ProjectSetupConflict(f"component inventory declares {spec.path} more than once")
        paths.add(spec.path)


def _codex_toml(value: Mapping[str, Any]) -> bytes:
    """Serialize project MCP and native automation opt-ins to documented Codex TOML."""

    servers = value.get("mcp_servers", value)
    if not isinstance(servers, Mapping):
        raise ProjectSetupError("codex_mcp must map server names to settings")
    blocks: list[str] = ['sandbox_mode = "workspace-write"']
    for name, record in sorted(servers.items()):
        if (
            not isinstance(name, str)
            or not name
            or any(
                ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for ch in name
            )
        ):
            raise ProjectSetupError(f"invalid Codex MCP server name: {name!r}")
        if not isinstance(record, Mapping):
            raise ProjectSetupError(f"Codex MCP server {name!r} is not an object")
        lines = [f"[mcp_servers.{name}]"]
        for field in ("command", "cwd"):
            if field in record:
                if not isinstance(record[field], str) or not record[field]:
                    raise ProjectSetupError(f"Codex MCP {name}.{field} must be text")
                lines.append(f"{field} = {json.dumps(record[field])}")
        if "args" in record:
            args = record["args"]
            if (
                not isinstance(args, Sequence)
                or isinstance(args, (str, bytes))
                or any(not isinstance(item, str) for item in args)
            ):
                raise ProjectSetupError(f"Codex MCP {name}.args must be an array of text")
            lines.append("args = [" + ", ".join(json.dumps(item) for item in args) + "]")
        if "env" in record:
            env = record["env"]
            if not isinstance(env, Mapping) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
            ):
                raise ProjectSetupError(f"Codex MCP {name}.env must map text to text")
            pairs = ", ".join(f"{key} = {json.dumps(env[key])}" for key in sorted(env))
            lines.append("env = {" + pairs + "}")
        blocks.append("\n".join(lines))
    blocks.append("[features]\nhooks = true")
    blocks.append("[tools.update_plan]\nenabled = true")
    return ("\n\n".join(blocks) + "\n").encode()


def _load_previous_receipt(root: Path) -> dict[str, str]:
    path = _target_file(root, _SETUP_RECEIPT)
    current = _read_regular(path, "setup receipt")
    if current is None:
        return {}
    raw, _ = current
    try:
        value = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectSetupConflict(f"existing setup receipt is invalid: {path}") from exc
    if not isinstance(value, Mapping) or value.get("record_kind") != "project_setup_receipt":
        raise ProjectSetupConflict(f"existing setup receipt is not owned by Bear Hug: {path}")
    files = value.get("files")
    if not isinstance(files, list):
        raise ProjectSetupConflict("existing setup receipt has no file inventory")
    result: dict[str, str] = {}
    for row in files:
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("sha256"), str)
        ):
            raise ProjectSetupConflict("existing setup receipt has an invalid file inventory")
        result[row["path"]] = row["sha256"]
    return result


def _previous_receipt_file_rows(root: Path) -> list[dict[str, str]]:
    """Every ``{"path", "component", "mode", "sha256"}`` row from the existing setup receipt.

    Additive alongside `_load_previous_receipt`, which collapses the same
    file into a `path -> sha256` map for its own narrower conflict-detection use and stays
    byte-unchanged by this addition. Used only to find files a PAST run owned by component, so a
    component that becomes disabled can identify exactly what it previously installed without
    threading a new shape through `_load_previous_receipt`'s existing callers.

    `mode` lets a row that ends up preserved rather than removed --
    `kept_unknown_path`, `kept_unsafe_path`, or a failed removal -- be written back into the
    receipt with its own real mode, not a value guessed from the one component path this module
    happens to know the fixed mode of.
    """
    path = _target_file(root, _SETUP_RECEIPT)
    current = _read_regular(path, "setup receipt")
    if current is None:
        return []
    raw, _ = current
    try:
        value = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectSetupConflict(f"existing setup receipt is invalid: {path}") from exc
    if not isinstance(value, Mapping) or value.get("record_kind") != "project_setup_receipt":
        raise ProjectSetupConflict(f"existing setup receipt is not owned by Bear Hug: {path}")
    files = value.get("files")
    if not isinstance(files, list):
        raise ProjectSetupConflict("existing setup receipt has no file inventory")
    result: list[dict[str, str]] = []
    for row in files:
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("component"), str)
            or not isinstance(row.get("mode"), str)
            or not isinstance(row.get("sha256"), str)
        ):
            raise ProjectSetupConflict("existing setup receipt has an invalid file inventory")
        result.append(
            {
                "path": row["path"],
                "component": row["component"],
                "mode": row["mode"],
                "sha256": row["sha256"],
            }
        )
    return result


def _merge_json(
    existing: Any,
    desired: Any,
    *,
    path: str = "",
    claude_hooks_removed: list[dict[str, str]] | None = None,
    claude_hooks_normalized: list[dict[str, Any]] | None = None,
    claude_hooks_normalize_identities: frozenset[tuple[Any, Any]] | None = None,
) -> Any:
    """Merge additions while keeping existing values authoritative on conflicts.

    `claude_hooks_removed` is narrow and Claude-specific: only `_merge_claude` sets it, so only
    a top-level "hooks" key merged on ITS behalf can ever delete Bear Hug's own duplicate.
    Every other caller — `_merge_json_settings` (every `merge="json-settings"` spec, including
    `.codex/hooks.json`) included — leaves it `None`, and `_merge_hooks` never deletes anything
    when `removed` is `None`. It is intentionally not threaded into the recursive call below:
    "hooks" is only ever a top-level key in the documents this function actually merges.

    `claude_hooks_normalized` is threaded the same narrow way: only `_merge_claude`
    sets it, so only the Claude settings merge can ever rewrite a stale Graft millisecond
    timeout on a handler this run itself desires; every other caller leaves it `None`, and
    `_merge_hooks` never rewrites a timeout when `normalized` is `None` — in particular, this
    keeps `.codex/hooks.json` untouched by this rewrite. `claude_hooks_normalize_identities`
    travels with it: the set of identities that actually came from the
    staged Graft bundle, without which the rewrite is not scoped to Graft's own timeouts.
    """

    if isinstance(existing, Mapping) and isinstance(desired, Mapping):
        result = copy.deepcopy(dict(existing))
        for key, value in desired.items():
            child = f"{path}.{key}" if path else str(key)
            if key not in result:
                result[key] = copy.deepcopy(value)
            elif key == "hooks" and isinstance(result[key], Mapping) and isinstance(value, Mapping):
                result[key] = _merge_hooks(
                    result[key],
                    value,
                    removed=claude_hooks_removed,
                    normalized=claude_hooks_normalized,
                    normalize_identities=claude_hooks_normalize_identities,
                )
            else:
                result[key] = _merge_json(result[key], value, path=child)
        return result
    if existing == desired:
        return copy.deepcopy(existing)
    return copy.deepcopy(existing)


def _merge_hooks(
    existing: Mapping[str, Any],
    desired: Mapping[str, Any],
    *,
    added: list[dict[str, str]] | None = None,
    removed: list[dict[str, str]] | None = None,
    kept: list[dict[str, Any]] | None = None,
    already_held: list[dict[str, str]] | None = None,
    normalized: list[dict[str, Any]] | None = None,
    timeout_filled: list[dict[str, Any]] | None = None,
    normalize_identities: frozenset[tuple[Any, Any]] | None = None,
) -> dict[str, Any]:
    """Merge Bear Hug's desired hooks into a project's existing ``hooks`` mapping.

    When `added` is given, every registration this call newly adds — a
    whole new event, a whole new group, or one handler folded into an existing exact-matcher
    group — is appended to it as ``{"event", "matcher", "command"}``. Optional and side-effect
    only, so every existing caller that never asks for this is unaffected.

    `removed` both permits and records deletion. Only when it is given does this call delete
    Bear Hug's own byte-identical copy of a registration from an exact-matcher group, and only
    when a different-spelling, equal-tool-set group (`literal_tool_union`, the same equivalence
    the hook audit itself uses) ALSO holds a handler for that identity that is byte-identical
    (every key and value) to the one being deleted. Falling short of either keeps both
    registrations exactly as they were before this deletion existed, and the hook audit keeps
    reporting the overlap. Each deletion is appended as
    ``{"event", "matcher", "command", "kept_under"}``, where
    `kept_under` names the matcher of the group that still holds the identity. With
    `removed=None` nothing is deleted.

    When `kept` is given, every identity that qualified for the above
    duplicate check (same event, same `literal_tool_union` equivalence) but was NOT deletable —
    the surviving different-spelling group's handler for it is not byte-identical to what this
    run would itself write — is appended as
    ``{"event", "matcher", "kept_under", "type", "command", "differs"}``, in either of two cases:

    - Setup's own exact-matcher group already exists and actually holds a copy of the identity
      (found among `matching`'s own handlers below). Without this half an operator who deletes
      Bear Hug's own copy by hand (acting on a PREVIOUS `kept_duplicates` row) would still be
      told one is being kept, although only the foreign registration exists and the hook audit
      reports no overlap for it.
    - Setup's own exact-matcher group does not exist in `existing` yet. A non-byte-identical
      survivor is not provably Bear Hug's own bytes and must not stand in for it unannounced, so
      this call ADDS Bear Hug's own handler to the fresh group being created — an edited foreign
      copy never justifies leaving Bear Hug's own safety behavior out — and reports the pair the
      same way, so the file ends up in exactly the state the sibling case above already produces:
      both registrations present, the audit still reporting the overlap. `own_handler` for the
      `differs` comparison below is the handler this run is adding (there is no pre-existing
      on-disk copy of Bear Hug's own to compare against yet).

    `matcher` is setup's own group, `kept_under` the surviving foreign group, and `differs` is
    the sorted list of handler keys whose values disagree between the identity's
    OWN on-disk (or, for a freshly added copy, about-to-be-written) handler and the survivor —
    not the handler this run would itself write in the abstract, which can differ from what is
    actually on disk in either direction. This changes nothing about the *delete* decision:
    `kept=None` records nothing, exactly as before this item existed, and every deletion already
    performed is unaffected by whether `kept` is supplied.

    When `already_held` is given, every identity declined from a FRESH
    group -- Bear Hug's own exact-matcher group does not exist in `existing` yet, so there is
    nothing for `removed` to act on -- because a different-spelling, equal-tool-set group
    already holds a byte-identical copy of it (the same test `deletable_identities` uses above)
    is appended as ``{"event", "matcher", "command", "held_under"}``, where `matcher` is the
    group this call would otherwise have created and `held_under` names the surviving foreign
    group's matcher exactly as it appears in `existing`. This is purely a report: the identity
    was already left out of `fresh` before this parameter existed, and still is regardless of
    whether `already_held` is supplied. A foreign handler that shares the identity but is not
    byte-identical is never excluded from `fresh` (see the `kept` case above) and is never named
    here either -- only a provably byte-identical survivor, whose copy really is left out, earns
    the claim "already held".

    `normalized` both permits and records rewriting a stale Graft millisecond
    timeout. Only when it is given, and only for an identity ALREADY present in the existing
    group (never a freshly added one), does this call rewrite an existing handler's `timeout` in
    place: when that existing handler's `timeout` is one of `_GRAFT_CLAUDE_TIMEOUT_CEILINGS`, the
    desired handler's `timeout` equals that value divided by 1000 (the value
    `_normalize_graft_claude_settings` would have produced), AND the identity is one of
    `normalize_identities` (see `_graft_hook_identities`; without this last
    condition every desired Claude-settings identity whose OWN timeout happens to be 8, 10 or 15
    is eligible, which today reaches Bear Hug's own `forbidden-command-gate.py` and
    `bearhug_work.py native-hook`, neither of which is Graft's), the existing `timeout` is
    rewritten to the desired value. Every other key of the existing handler, and every foreign
    registration, is left untouched. Each rewrite is appended as ``{"event", "matcher", "type",
    "command", "old", "new"}``. With `normalized=None` or `normalize_identities=None` nothing is
    rewritten. This is disjoint from the `Interrupt`/`SessionEnd`/other-event 15→3/15→45 upgrade
    below: those key off the DESIRED handler's timeout being exactly 3 or 45 (Codex's own
    former-15-second identity, never a Claude-side or Graft-side one), never on a value in
    `_GRAFT_CLAUDE_TIMEOUT_CEILINGS` or its post-division form, so the same handler can never match
    both branches.

    Separately, and unconditionally (no parameter gates the MUTATION itself): an existing handler
    for an identity this run desires, found with no `timeout` key at all, has the desired timeout
    added in place -- never a Graft-bundle identity, which `normalize_identities` excludes here
    the same way it scopes the rewrite above. When `timeout_filled` is given, every such fill is
    appended to it as ``{"event", "matcher", "type", "command", "timeout"}`` -- the same shape
    `normalized` above uses, minus `old`, since there is no prior value to name.
    """
    result = copy.deepcopy(dict(existing))
    for event, groups in desired.items():
        if not isinstance(groups, list) or not isinstance(result.get(event, []), list):
            raise ProjectSetupConflict(f"existing provider hooks.{event} has an incompatible shape")
        host_command = "python3 -I .codex/bearhug-host/codex-hook.py"
        if any(
            handler.get("command") == host_command
            for group in groups
            if isinstance(group, Mapping) and isinstance(group.get("hooks"), list)
            for handler in group["hooks"] if isinstance(handler, Mapping)
        ):
            for group in result.get(event, []):
                if not isinstance(group, Mapping) or not isinstance(group.get("hooks"), list):
                    continue
                for handler in group["hooks"]:
                    if isinstance(handler, dict) and handler.get("type") == "command" and (
                        handler.get("command") == "python3 scripts/hooks/codex-hook.py"
                    ):
                        handler["command"] = host_command
        if event not in result:
            result[event] = copy.deepcopy(groups)
            if added is not None:
                for new_group in groups:
                    if not isinstance(new_group, Mapping):
                        continue
                    for handler in new_group.get("hooks", []):
                        if isinstance(handler, Mapping):
                            added.append({
                                "event": event,
                                "matcher": new_group.get("matcher"),
                                "command": handler.get("command"),
                            })
            continue
        if not isinstance(result[event], list) or not isinstance(groups, list):
            raise ProjectSetupConflict(f"existing provider hooks.{event} has an incompatible shape")
        for group in groups:
            if not isinstance(group, Mapping):
                raise ProjectSetupConflict(f"setup hooks.{event} contains a malformed group")
            matcher = group.get("matcher")
            matching = [
                item
                for item in result[event]
                if isinstance(item, Mapping) and item.get("matcher") == matcher
            ]
            # An owned identity already present under a *different* matcher spelling
            # that denotes the identical literal tool set is a duplicate invocation, not a new
            # registration — Claude Code fires every matching group once per tool call. Only a
            # literal `|`-joined tool-name matcher is ever compared this way; a regex, an empty
            # matcher, or `*` stays "not provably equal" and is added as today. Every group read
            # here is read-only: a foreign group is never edited, reordered, or removed.
            own_tools = literal_tool_union(matcher)
            duplicate_identities: set[tuple[Any, Any]] = set()
            # A survivor is credited toward
            # *deletion* only when its handler for the identity is byte-identical -- every key
            # and value, not only type and command -- to the handler this run would itself
            # write. `duplicate_identities` above is the loose test: it only ever declines
            # to add a second copy, which is safe for any group `literal_tool_union` resolves to
            # the same tool set. Both sets use exactly that one equivalence -- the same the hook
            # audit uses -- with no separate whitespace-sensitive test: measured on a native probe
            # (2026-09-20, Claude Code 2.1.275), a matcher padded with spaces
            # (` Write | Edit | MultiEdit `, `Write |Edit`) firing exactly like its unpadded form
            # on a real Edit call, alongside a plain matcher, a bare `Edit`, and no matcher at
            # all -- all five groups fired. `literal_tool_union` trims any whitespace around a
            # `|`-separated name, not only spaces; only the space-padded spelling above was
            # measured, and this function treats the wider case the same way without a further
            # measurement. An earlier round's narrower, whitespace-sensitive check
            # (`_unpadded_literal_tool_union`, since removed) was never measured and was wrong;
            # setup and the audit share this one function.
            #
            # Sharing `literal_tool_union` is not the same
            # as always agreeing. For a matcher that function cannot resolve to a literal set --
            # blank, whitespace-only, or `"*"` -- `own_tools` above is `None`, and this block
            # simply skips the cross-spelling dedup for it (added as today, the same as a regex
            # this function cannot solve). The hook audit's own `_overlap_scope` computes a
            # SEPARATE `unmatched` verdict alongside the same `None`, and folds a blank or
            # whitespace-only matcher into "fires on every tool" for its OVERLAP check -- a
            # claim this function makes no equivalent of. Whether a blank or whitespace-only
            # matcher actually fires on every tool natively has not been measured (unlike the
            # padded case above); until it is, this divergence is disclosed, not resolved by
            # treating a blank matcher as `*` here, which would license a deletion this module
            # has not earned.
            deletable_identities: set[tuple[Any, Any]] = set()
            # Which different-spelling group to credit as `kept_under`.
            # Recorded only alongside `deletable_identities`, below, so it always names a group
            # that actually qualified this exact identity's deletion — never merely the first
            # same-tool-set group found, which can carry an edited handler for the same identity
            # and never justified anything.
            duplicate_sources: dict[tuple[Any, Any], Any] = {}
            # Every (matcher, handler) pair, foreign or not-yet-known-deletable,
            # recorded for EVERY identity added to `duplicate_identities` above -- not only the
            # deletable ones -- so a declined dedup can still name which surviving group and
            # handler it was declined against. Read-only, like everything else gathered here.
            kept_sources: dict[tuple[Any, Any], list[tuple[Any, Any]]] = {}
            desired_hooks_here = group.get("hooks")
            if not isinstance(desired_hooks_here, list):
                desired_hooks_here = []
            if own_tools is not None:
                for item in result[event]:
                    if not isinstance(item, Mapping) or item.get("matcher") == matcher:
                        continue
                    other_matcher = item.get("matcher")
                    if literal_tool_union(other_matcher) != own_tools:
                        continue
                    other_handlers = item.get("hooks", [])
                    if not isinstance(other_handlers, list):
                        continue
                    for other_handler in other_handlers:
                        if not isinstance(other_handler, Mapping):
                            continue
                        identity = (other_handler.get("type"), other_handler.get("command"))
                        duplicate_identities.add(identity)
                        kept_sources.setdefault(identity, []).append((other_matcher, other_handler))
                        if any(
                            isinstance(desired_handler, Mapping)
                            and desired_handler == other_handler
                            for desired_handler in desired_hooks_here
                        ):
                            # `kept_under` must name the group that actually
                            # qualified the deletion, not merely the first same-tool-set group
                            # found. Only recorded here, on the same branch that makes the
                            # identity deletable: a group with a byte-identical handler for this
                            # identity. An edited handler never justified anything and must not
                            # be credited as the survivor.
                            deletable_identities.add(identity)
                            duplicate_sources.setdefault(identity, other_matcher)
            if not matching:
                fresh = []
                for handler in group.get("hooks", []):
                    if isinstance(handler, Mapping):
                        identity = (handler.get("type"), handler.get("command"))
                        if identity in duplicate_identities and identity in deletable_identities:
                            # Bear Hug's own exact-matcher group does not exist yet in this
                            # file, so there is nothing to delete this copy FROM -- it is
                            # simply left out of the group this call would otherwise create.
                            # Only excluded when a survivor is provably the identical bytes
                            # (`deletable_identities`): the same standard `removed` already
                            # requires. Name it in `already_held`.
                            if already_held is not None:
                                already_held.append({
                                    "event": event,
                                    "matcher": matcher,
                                    "command": handler.get("command"),
                                    "held_under": duplicate_sources.get(identity),
                                })
                            continue
                        # A survivor that only shares the identity -- an edited handler, a
                        # different timeout, an extra key -- never justified anything ("An
                        # edited handler never justified anything and must not be credited
                        # as the survivor", above) and must not silently stand in for Bear
                        # Hug's own safety behavior. Add Bear Hug's own handler to the fresh
                        # group as usual (fall through to `fresh.append` below, whether or
                        # not `kept` is supplied) and report the pair the same way the
                        # sibling branch reports a kept, differing duplicate -- so the file
                        # ends up in the sibling case's own end state: both registrations
                        # present, the audit still reporting the overlap.
                        if identity in duplicate_identities and kept is not None:
                            for other_matcher, other_handler in kept_sources.get(identity, ()):
                                if not isinstance(other_handler, Mapping):
                                    continue
                                differs = sorted(
                                    key
                                    for key in set(handler) | set(other_handler)
                                    if handler.get(key) != other_handler.get(key)
                                )
                                kept.append({
                                    "event": event,
                                    "matcher": matcher,
                                    "kept_under": other_matcher,
                                    "type": identity[0],
                                    "command": identity[1],
                                    "differs": differs,
                                })
                    fresh.append(handler)
                if fresh:
                    result[event].append({**copy.deepcopy(group), "hooks": copy.deepcopy(fresh)})
                    if added is not None:
                        for handler in fresh:
                            if isinstance(handler, Mapping):
                                added.append({
                                    "event": event,
                                    "matcher": matcher,
                                    "command": handler.get("command"),
                                })
                continue
            destination = matching[0]
            desired_handlers = group.get("hooks")
            existing_handlers = destination.get("hooks")
            if not isinstance(desired_handlers, list) or not isinstance(existing_handlers, list):
                raise ProjectSetupConflict(
                    f"existing provider hooks.{event} has malformed handlers"
                )
            identities = {
                (item.get("type"), item.get("command"))
                for matched in matching
                for item in matched.get("hooks", [])
                if isinstance(item, Mapping)
            }
            groups_that_lost_a_handler: set[int] = set()
            for handler in desired_handlers:
                identity = (
                    (handler.get("type"), handler.get("command"))
                    if isinstance(handler, Mapping)
                    else (None, None)
                )
                if identity in duplicate_identities:
                    # An older, unrepaired install may already have written Bear Hug's own
                    # copy of this identity into the exact-matcher group before this loose test
                    # existed to stop it being added a second time. Remove that copy only when
                    # the surviving,
                    # differently-spelled group's own handler for it is byte-identical (every key
                    # and value, not only type and command) to what this run would itself write
                    # (`deletable_identities`, above) — an edited variant is not provably a live
                    # holder of Bear Hug's own bytes, so it is left in place and the audit keeps
                    # reporting it. The different-spelling group credited in `duplicate_sources`
                    # is only ever read, here and above: never edited, reordered, or removed.
                    #
                    # A deletion nobody asked to observe must not happen at all —
                    # `_merge_json`'s generic "hooks" branch reaches this same code for every
                    # merge="json-settings" spec (e.g. `.codex/hooks.json`), not only Claude's own
                    # settings. Attempt removal only when `removed` is supplied; with
                    # `removed=None` this behaves exactly as it did at 548dd24 for an
                    # already-present identity (leave it, add nothing).
                    if (
                        removed is not None
                        and isinstance(handler, Mapping)
                        and identity in deletable_identities
                    ):
                        # A malformed shape can hold more than one group under the exact same
                        # matcher spelling, or one group can hold the byte-identical copy more
                        # than once; remove every copy, from every such group, in this one pass,
                        # and report each, rather than only the first group's first copy.
                        for matched in matching:
                            matched_handlers = matched.get("hooks")
                            if not isinstance(matched_handlers, list):
                                continue
                            duplicate_copies = [
                                item
                                for item in matched_handlers
                                if isinstance(item, Mapping)
                                and (item.get("type"), item.get("command")) == identity
                                and item == handler
                            ]
                            for existing_copy in duplicate_copies:
                                matched_handlers.remove(existing_copy)
                                groups_that_lost_a_handler.add(id(matched))
                                removed.append({
                                    "event": event,
                                    "matcher": matcher,
                                    "command": existing_copy.get("command"),
                                    "kept_under": duplicate_sources.get(identity),
                                })
                    elif (
                        kept is not None
                        and isinstance(handler, Mapping)
                        and identity not in deletable_identities
                    ):
                        # The identity is a genuine duplicate (the loose,
                        # tool-set-equivalence test), not deletable (the byte-identical test
                        # failed against every survivor recorded for it) -- report each
                        # surviving group it was declined against, rather than silently doing
                        # nothing as before this item existed.
                        #
                        # Only when setup's own exact-matcher group
                        # actually holds a copy of the identity (`own_handler is not None`,
                        # equivalent to `identity in identities` -- both look up the same
                        # `matching` groups this exact way, so testing one tests the other; the
                        # separate `identity in identities` clause first tried was
                        # dropped as dead weight once this lookup existed anyway).
                        # Without this, an operator who deletes Bear Hug's own copy from a
                        # group it shares with a foreign registration (acting on a PREVIOUS
                        # `kept_duplicates` row) is told on the next run that one is still
                        # being kept, although only the foreign registration exists and the
                        # hook audit reports no overlap for it.
                        #
                        # `differs` compares this same OWN on-disk handler
                        # against the survivor, not the handler this run would itself write --
                        # the row describes the two registrations actually in the file, and an
                        # operator's own copy can have drifted from what setup would generate
                        # in either direction.
                        own_handler = next(
                            (
                                item
                                for matched in matching
                                for item in matched.get("hooks", [])
                                if isinstance(item, Mapping)
                                and (item.get("type"), item.get("command")) == identity
                            ),
                            None,
                        )
                        if own_handler is not None:
                            for other_matcher, other_handler in kept_sources.get(identity, ()):
                                if not isinstance(other_handler, Mapping):
                                    continue
                                differs = sorted(
                                    key
                                    for key in set(own_handler) | set(other_handler)
                                    if own_handler.get(key) != other_handler.get(key)
                                )
                                kept.append({
                                    "event": event,
                                    "matcher": matcher,
                                    "kept_under": other_matcher,
                                    "type": identity[0],
                                    "command": identity[1],
                                    "differs": differs,
                                })
                    continue
                if identity not in identities:
                    existing_handlers.append(copy.deepcopy(handler))
                    identities.add(identity)
                    if added is not None and isinstance(handler, Mapping):
                        added.append({
                            "event": event,
                            "matcher": matcher,
                            "command": handler.get("command"),
                        })
                elif event in {"Interrupt", "SessionEnd"} and handler.get("timeout") == 3:
                    # Upgrade Bear Hug's former 15-second default for this exact command.
                    # Codex clamps these events to 3 seconds; retain foreign handlers/settings.
                    for matched in matching:
                        for item in matched.get("hooks", []):
                            if (
                                isinstance(item, dict)
                                and (item.get("type"), item.get("command")) == identity
                                and item.get("timeout") == 15
                            ):
                                item["timeout"] = 3
                elif event not in {"Interrupt", "SessionEnd"} and handler.get("timeout") == 45:
                    # Formatting is a bounded 30-second sandboxed step before validation is
                    # detached. Upgrade only Bear Hug's exact former 15-second handler.
                    for matched in matching:
                        for item in matched.get("hooks", []):
                            if (
                                isinstance(item, dict)
                                and (item.get("type"), item.get("command")) == identity
                                and item.get("timeout") == 15
                            ):
                                item["timeout"] = 45
                elif (
                    normalized is not None
                    and normalize_identities is not None
                    # The ruling scopes this to an identity that "comes
                    # from the Graft bundle setup stages". Without this clause, any desired
                    # Claude-settings identity whose OWN timeout happens to be 8, 10 or 15 is
                    # eligible -- today that reaches Bear Hug's own
                    # `forbidden-command-gate.py` (desired 10, on both PreToolUse
                    # registrations) and `bearhug_work.py native-hook` (desired 10, on five
                    # events), neither of which ever passed through
                    # `_normalize_graft_claude_settings`. A hand-set `10000` on the gate --
                    # exactly the value a "never let this safeguard be killed" choice produces
                    # now that measurement has shown the timeout unit is seconds -- would
                    # otherwise be shortened 1000-fold to `10`, and a hook exceeding its timeout
                    # would be cancelled (`exit_code 1`/"cancelled", measured for PostToolUse on
                    # Claude Code 2.1.275); what a cancelled PreToolUse gate does was not
                    # measured.
                    and identity in normalize_identities
                    and isinstance(handler, Mapping)
                    and isinstance(handler.get("timeout"), int)
                    and not isinstance(handler.get("timeout"), bool)
                    and handler.get("timeout") in _GRAFT_CLAUDE_TIMEOUT_SECONDS
                ):
                    # An existing registration for an identity this run itself
                    # desires, AND that identity came from the staged Graft bundle, may still
                    # carry a stale Graft millisecond timeout, left behind by an install that
                    # predates `_normalize_graft_claude_settings`. Rewrite it to the seconds
                    # value a fresh install would have written, and report it.
                    for matched in matching:
                        for item in matched.get("hooks", []):
                            if (
                                isinstance(item, dict)
                                and (item.get("type"), item.get("command")) == identity
                                and isinstance(item.get("timeout"), int)
                                and not isinstance(item.get("timeout"), bool)
                                and item.get("timeout") in _GRAFT_CLAUDE_TIMEOUT_CEILINGS
                                and item.get("timeout") // 1000 == handler.get("timeout")
                            ):
                                old_timeout = item["timeout"]
                                item["timeout"] = handler.get("timeout")
                                normalized.append({
                                    "event": event,
                                    "matcher": matcher,
                                    "type": identity[0],
                                    "command": identity[1],
                                    "old": old_timeout,
                                    "new": item["timeout"],
                                })
                elif (
                    isinstance(handler, Mapping)
                    and handler.get("timeout") is not None
                    and (normalize_identities is None or identity not in normalize_identities)
                ):
                    # An existing registration for an identity this run itself desires, carrying
                    # no `timeout` key at all -- the shape the memex, memq and Stop coordinator
                    # registrations shipped in for a time, before each carried the explicit value
                    # measured for it (docs/OPERATING-MODES.md's timeout paragraph has the
                    # arithmetic). Add the desired timeout to the existing handler in place, so an
                    # already-installed project is upgraded the same way a fresh one is written,
                    # and report the file as changed the way any other rewritten byte does.
                    #
                    # Excluded by the same `normalize_identities` scoping the ms-to-s rewrite
                    # above already uses: an identity that comes from the staged Graft bundle is
                    # never touched here, whatever its on-disk shape -- that rewrite (or leaving a
                    # non-millisecond value alone) is entirely the Graft rule's to make. A handler
                    # whose desired timeout is exactly 3 or 45 already took one of the two Codex
                    # branches above and never reaches this one for the same identity.
                    for matched in matching:
                        for item in matched.get("hooks", []):
                            if (
                                isinstance(item, dict)
                                and (item.get("type"), item.get("command")) == identity
                                and item.get("timeout") is None
                            ):
                                item["timeout"] = handler["timeout"]
                                if timeout_filled is not None:
                                    timeout_filled.append({
                                        "event": event,
                                        "matcher": matcher,
                                        "type": identity[0],
                                        "command": identity[1],
                                        "timeout": item["timeout"],
                                    })
            if groups_that_lost_a_handler:
                # Never leave an empty group behind, whichever of the (possibly
                # several) same-spelling groups it was. Remove by identity, not by value, in case
                # another group happens to share the same (now-empty) content.
                result[event] = [
                    g
                    for g in result[event]
                    if not (id(g) in groups_that_lost_a_handler and not g.get("hooks"))
                ]
    return result


def _merge_claude(
    existing: bytes,
    desired: bytes,
    *,
    normalize_identities: frozenset[tuple[Any, Any]] | None = None,
) -> bytes:
    try:
        old = json.loads(existing.decode())
        new = json.loads(desired.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectSetupConflict("Claude settings must be valid JSON before setup") from exc
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        raise ProjectSetupConflict("Claude settings must be JSON objects")
    # Claude's own settings merge is the one caller that always wants its own
    # inherited duplicate removed, so it always supplies a `removed` sink to enable that — a
    # throwaway one here, since a caller that wants to observe what was removed uses the separate
    # `_added_claude_hook_registrations` reporting pass over the same two byte strings.
    # Likewise the one caller that always wants a stale Graft millisecond timeout on
    # an identity it manages rewritten, so it always supplies a `normalized` sink too.
    # `normalize_identities` is the caller's own responsibility here --
    # `_merge_claude` has no bundle to derive it from, so every caller (all three: the plan, the
    # reporting observer, and the write loop) must pass the same set for a run to report exactly
    # what it wrote.
    result = _merge_json(
        old,
        new,
        claude_hooks_removed=[],
        claude_hooks_normalized=[],
        claude_hooks_normalize_identities=normalize_identities,
    )
    if "env" in new:
        if not isinstance(result.get("env"), Mapping):
            raise ProjectSetupConflict("Claude settings env must be an object")
        for key in ("MEMQ_EMBED_URL", "MEMQ_EMBED_MODEL"):
            if key in new["env"]:
                result["env"][key] = new["env"][key]
    return _canonical_json(result)


def _hook_registrations(hooks: Any) -> list[dict[str, str]]:
    """Every ``{"event", "matcher", "command"}`` triple one Claude ``hooks`` mapping declares.

    Used for reporting on a fresh install, where there is nothing to merge against and so
    nothing for `_merge_hooks` to observe adding: every registration Bear Hug generates counts
    as added.
    """
    registrations: list[dict[str, str]] = []
    if not isinstance(hooks, Mapping):
        return registrations
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            matcher = group.get("matcher")
            for handler in group.get("hooks", []):
                if isinstance(handler, Mapping):
                    registrations.append(
                        {"event": event, "matcher": matcher, "command": handler.get("command")}
                    )
    return registrations


def _graft_hook_identities(hooks: Any) -> frozenset[tuple[Any, Any]]:
    """Every ``(type, command)`` identity a normalized Graft hooks mapping declares.

    The millisecond-timeout rewrite must be scoped to an identity
    that actually came from the Graft bundle setup stage -- the ruling's own parenthetical ("it
    comes from the Graft bundle setup stages"). Called on `graft_settings` right after
    `_normalize_graft_claude_settings` has run and before it is combined with Bear Hug's own
    desired hooks, this is the one point a Graft-staged identity is still distinguishable from a
    Bear Hug-owned one that happens to share a post-division timeout value (`{8, 10, 15}`) --
    for example `forbidden-command-gate.py` (desired `10`) or `bearhug_work.py native-hook`
    (desired `10`), neither of which is Graft's.
    """
    identities: set[tuple[Any, Any]] = set()
    if not isinstance(hooks, Mapping):
        return frozenset(identities)
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            for handler in group.get("hooks", []):
                if isinstance(handler, Mapping):
                    identities.add((handler.get("type"), handler.get("command")))
    return frozenset(identities)


def _added_claude_hook_registrations(
    existing_raw: bytes,
    candidate_raw: bytes,
    *,
    removed: list[dict[str, str]] | None = None,
    kept: list[dict[str, Any]] | None = None,
    already_held: list[dict[str, str]] | None = None,
    normalized: list[dict[str, Any]] | None = None,
    timeout_filled: list[dict[str, Any]] | None = None,
    normalize_identities: frozenset[tuple[Any, Any]] | None = None,
) -> list[dict[str, str]]:
    """Which registrations a Claude settings merge would newly add.

    Mirrors the merge `_merge_claude` independently performs on the same bytes moments before or
    after this is called, purely to observe what changes — computed here rather than threaded
    back through `_merge_json`'s generic recursion, which every other merged key also shares.

    When `removed` is given, it is extended with every registration the same
    merge would delete (see `_merge_hooks`'s own `removed` parameter) — one observation pass
    covers both, since both come from the identical `_merge_hooks` call over the identical bytes.

    When `kept` is given, it is extended with every declined
    duplicate the same merge would find (see `_merge_hooks`'s own `kept` parameter).

    When `already_held` is given, it is extended with every identity the
    same merge declined to add beside a byte-identical foreign survivor because Bear Hug's own
    exact-matcher group did not exist yet (see `_merge_hooks`'s own `already_held` parameter).

    When `normalized` is given, it is extended with every stale
    Graft millisecond timeout the same merge would rewrite (see `_merge_hooks`'s own
    `normalized` parameter). `normalize_identities` must be the same set the
    real merge used, or this observer would report a rewrite the write path never made (or the
    reverse).

    When `timeout_filled` is given, it is extended with every absent-timeout fill the same merge
    would make (see `_merge_hooks`'s own `timeout_filled` parameter). All six observations come
    from this one `_merge_hooks` call over the identical bytes, so dry run and the write path can
    never disagree about what changed.
    """
    try:
        existing = json.loads(existing_raw.decode())
        candidate = json.loads(candidate_raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(existing, Mapping) or not isinstance(candidate, Mapping):
        return []
    existing_hooks = existing.get("hooks", {})
    candidate_hooks = candidate.get("hooks", {})
    if not isinstance(existing_hooks, Mapping) or not isinstance(candidate_hooks, Mapping):
        return []
    added: list[dict[str, str]] = []
    removed_here: list[dict[str, str]] = []
    kept_here: list[dict[str, Any]] = []
    already_held_here: list[dict[str, str]] = []
    normalized_here: list[dict[str, Any]] = []
    timeout_filled_here: list[dict[str, Any]] = []
    try:
        _merge_hooks(
            existing_hooks,
            candidate_hooks,
            added=added,
            removed=removed_here,
            kept=kept_here,
            already_held=already_held_here,
            normalized=normalized_here,
            timeout_filled=timeout_filled_here,
            normalize_identities=normalize_identities,
        )
    except ProjectSetupConflict:
        return []
    if removed is not None:
        removed.extend(removed_here)
    if kept is not None:
        kept.extend(kept_here)
    if already_held is not None:
        already_held.extend(already_held_here)
    if normalized is not None:
        normalized.extend(normalized_here)
    if timeout_filled is not None:
        timeout_filled.extend(timeout_filled_here)
    return added


def _merge_mcp(existing: bytes, desired: bytes) -> bytes:
    try:
        old = json.loads(existing.decode())
        new = json.loads(desired.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectSetupConflict("MCP configuration must be valid JSON before setup") from exc
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        raise ProjectSetupConflict("MCP configuration must be JSON objects")
    old_servers = old.get("mcpServers", {})
    new_servers = new.get("mcpServers", {})
    if not isinstance(old_servers, Mapping) or not isinstance(new_servers, Mapping):
        raise ProjectSetupConflict("MCP configuration mcpServers must be objects")
    for name, desired_server in new_servers.items():
        if name in old_servers and old_servers[name] != desired_server:
            existing_server = old_servers[name]
            if not _compatible_existing_mcp(name, existing_server, desired_server):
                raise ProjectSetupConflict(f"existing MCP server {name!r} conflicts with setup")
            # The project already owns this working registration.  _merge_json keeps its
            # command, environment and arguments intact; setup still installs its own bundled
            # launcher for fresh projects and reports the preserved registration in the receipt.
    return _canonical_json(_merge_json(old, new))


def _compatible_existing_mcp(name: Any, existing: Any, desired: Any) -> bool:
    """Recognize a known equivalent MCP registration without weakening conflict detection.

    Existing projects commonly register the same tools by PATH name (``graft`` and
    ``codebase-memory-mcp``), while a fresh setup uses absolute target launchers.  Replacing a
    working project command would be a surprising adoption side effect, so only these two
    service identities are eligible and their operation shape must still match.
    """

    if name not in {"graft", "codebase-memory-mcp"}:
        return False
    if not isinstance(existing, Mapping) or not isinstance(desired, Mapping):
        return False
    old_command = existing.get("command")
    new_command = desired.get("command")
    if not isinstance(old_command, str) or not isinstance(new_command, str):
        return False
    old_name = Path(old_command).name
    new_name = Path(new_command).name
    if name == "codebase-memory-mcp":
        return old_name == new_name == name and existing.get("args", []) in ([], None)
    old_args = existing.get("args", [])
    new_args = desired.get("args", [])
    return (
        old_name in {"graft", "graft-cli"}
        and new_name in {"node", "graft", "graft-cli"}
        and isinstance(old_args, list)
        and isinstance(new_args, list)
        and old_args[-1:] == ["mcp"]
        and new_args[-1:] == ["mcp"]
    )


def _merge_json_settings(existing: bytes, desired: bytes) -> bytes:
    try:
        old = json.loads(existing.decode())
        new = json.loads(desired.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectSetupConflict("JSON configuration must be valid before setup") from exc
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        raise ProjectSetupConflict("JSON configuration must be objects")
    result = _merge_json(old, new)
    if isinstance(old.get("sources"), list) and isinstance(new.get("sources"), list):
        existing_sources = result["sources"]
        identities = {
            (item.get("type"), item.get("glob"), item.get("label"))
            for item in existing_sources
            if isinstance(item, Mapping)
        }
        for item in new["sources"]:
            identity = (
                (
                    item.get("type"),
                    item.get("glob"),
                    item.get("label"),
                )
                if isinstance(item, Mapping)
                else None
            )
            if identity not in identities:
                existing_sources.append(copy.deepcopy(item))
                identities.add(identity)
    return _canonical_json(result)


def _merge_instructions(existing: bytes, desired: bytes) -> bytes:
    """Replace only generated tool blocks; retain project-authored instructions."""
    try:
        text, template = existing.decode(), desired.decode()
    except UnicodeDecodeError as exc:
        raise ProjectSetupConflict("project instructions must be UTF-8") from exc
    for name in ("graft", "bearhug"):
        start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
        if start not in template:
            continue
        if template.count(start) != 1 or template.count(end) != 1:
            raise ProjectSetupError(f"invalid generated {name} instruction block")
        block = template[template.index(start) : template.index(end) + len(end)]
        if start in text or end in text:
            if (
                text.count(start) != 1
                or text.count(end) != 1
                or text.index(start) > text.index(end)
            ):
                raise ProjectSetupConflict(f"ambiguous existing {name} instruction block")
            text = text[: text.index(start)] + block + text[text.index(end) + len(end) :]
        else:
            text = block + "\n\n" + text
    return (text.rstrip() + "\n").encode()


def _merge_gitignore(existing: bytes, desired: bytes) -> bytes:
    try:
        text = existing.decode()
        additions = desired.decode().splitlines()
    except UnicodeDecodeError as exc:
        raise ProjectSetupConflict(".gitignore must be UTF-8 text") from exc
    lines = text.splitlines()
    present = set(lines)
    for item in additions:
        if item and item not in present:
            lines.append(item)
            present.add(item)
    return ("\n".join(lines) + ("\n" if lines else "")).encode()


def _toml_sections(value: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped
            sections.setdefault(current, []).append(line)
        elif current is not None:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _merge_codex(existing: bytes, desired: bytes) -> bytes:
    try:
        old_text = existing.decode()
        new_text = desired.decode()
    except UnicodeDecodeError as exc:
        raise ProjectSetupConflict("Codex config must be UTF-8 TOML") from exc
    try:
        old_document = tomllib.loads(old_text)
        new_document = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ProjectSetupConflict("Codex config is invalid TOML") from exc
    if old_document.get("sandbox_mode", "workspace-write") not in {"workspace-write", "read-only"}:
        raise ProjectSetupConflict(
            "direct Codex hooks require workspace-write or read-only sandbox"
        )
    if old_document.get("default_permissions") or old_document.get("permissions"):
        raise ProjectSetupConflict(
            "custom Codex permission profiles need direct-hook qualification"
        )
    if "sandbox_mode" not in old_document:
        old_text = 'sandbox_mode = "workspace-write"\n' + old_text
    old_servers = old_document.get("mcp_servers", {})
    new_servers = new_document.get("mcp_servers", {})
    if not isinstance(old_servers, Mapping) or not isinstance(new_servers, Mapping):
        raise ProjectSetupConflict("Codex config mcp_servers must be a table")
    for name, desired_server in new_servers.items():
        if name in old_servers and old_servers[name] != desired_server:
            raise ProjectSetupConflict(f"Codex MCP server {name!r} conflicts with setup")
    new_sections = _toml_sections(new_text)
    additions: list[str] = []
    for section, block in new_sections.items():
        if not section.startswith("[mcp_servers."):
            continue
        name = section[len("[mcp_servers.") : -1]
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        if name not in old_servers:
            additions.append(block)
    desired_features = new_document.get("features", {})
    old_features = old_document.get("features", {})
    if not isinstance(desired_features, Mapping) or not isinstance(old_features, Mapping):
        raise ProjectSetupConflict("Codex features must be a table")
    features_header = r"(?m)^([ \t]*\[features\][ \t]*(?:#[^\n]*)?)(?:\n|$)"
    if "hooks" in desired_features and "hooks" not in old_features:
        if "features" not in old_document:
            additions.append("[features]\nhooks = true")
        elif re.search(features_header, old_text):
            old_text = re.sub(
                features_header,
                r"\1\nhooks = true\n",
                old_text,
                count=1,
            )
        else:
            raise ProjectSetupConflict(
                "Set features.hooks explicitly in the existing Codex features table"
            )
    # Codex >=0.152 makes the native checklist opt-in. Preserve a deliberate project choice.
    desired_plan = new_document.get("tools", {}).get("update_plan", {})
    old_tools = old_document.get("tools", {})
    if not isinstance(old_tools, Mapping):
        raise ProjectSetupConflict("Codex tools must be a table")
    old_plan = old_tools.get("update_plan", {})
    if not isinstance(old_plan, Mapping):
        raise ProjectSetupConflict("Codex tools.update_plan must be a table")
    plan_header = r"(?m)^([ \t]*\[tools\.update_plan\][ \t]*(?:#[^\n]*)?)(?:\n|$)"
    if "enabled" in desired_plan and "enabled" not in old_plan:
        if "update_plan" not in old_tools:
            additions.append("[tools.update_plan]\nenabled = true")
        elif re.search(plan_header, old_text):
            old_text = re.sub(
                plan_header,
                r"\1\nenabled = true\n",
                old_text,
                count=1,
            )
        else:
            raise ProjectSetupConflict(
                "Set tools.update_plan.enabled explicitly in the existing Codex tools table"
            )
    if not additions:
        return old_text.encode()
    separator = "\n" if old_text.endswith("\n") or not old_text else "\n\n"
    merged = old_text + separator + "\n\n".join(additions) + "\n"
    try:
        tomllib.loads(merged)
    except tomllib.TOMLDecodeError as exc:
        raise ProjectSetupConflict(
            "Codex inline tools table needs an explicit update_plan setting"
        ) from exc
    return merged.encode()


def _prepare_bundle_root(args: argparse.Namespace, target: Path) -> Path:
    supplied = getattr(args, "bundle_root", None)
    base = Path(supplied).expanduser() if supplied else REPO_ROOT / "runs" / "setup" / "components"
    if not base.is_absolute():
        raise ProjectSetupError("bundle_root must be an explicit absolute path")
    _assert_physical_ancestors(base)
    if base.is_symlink():
        raise ProjectSetupError("bundle_root may not be a symlink")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = base.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ProjectSetupError("bundle_root must be a private directory")
    fingerprint = _sha256(
        (target.as_posix() + "\0" + str(getattr(args, "provider", "both"))).encode()
    )[:24]
    result = base / fingerprint
    _assert_physical_ancestors(result)
    if result.is_symlink():
        raise ProjectSetupError("component bundle directory may not be a symlink")
    result.mkdir(mode=0o700, exist_ok=True)
    metadata = result.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ProjectSetupError("component bundle directory must be private")
    return result


@contextmanager
def _bundle_lock(root: Path):
    """Serialize setup calls sharing one content-addressed component bundle."""

    path = root / ".setup.lock"
    if path.is_symlink():
        raise ProjectSetupError("component bundle lock may not be a symlink")
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise ProjectSetupError(f"cannot open component bundle lock: {exc}") from exc
    try:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ProjectSetupError("timed out waiting for component setup lock") from exc
                time.sleep(0.01)
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _artifact_preflight(root: Path) -> dict[Path, bool]:
    """Record generated index roots before external commands are allowed to run."""

    result: dict[Path, bool] = {}
    for path in (
        root / "graft",
        root / ".memq",
        root / ".ignore",
        root / ".bearhug/codebase-memory",
    ):
        if path.is_symlink():
            raise ProjectSetupConflict(f"generated artifact path may not be a symlink: {path}")
        if path.exists():
            expected_kind = "file" if path.name == ".ignore" else "directory"
            if (expected_kind == "file" and not path.is_file()) or (
                expected_kind == "directory" and not path.is_dir()
            ):
                raise ProjectSetupConflict(
                    f"generated artifact path is not a {expected_kind}: {path}"
                )
        result[path] = path.exists()
    return result


def _rollback_artifacts(before: Mapping[Path, bool]) -> list[str]:
    """Remove only index roots absent before setup; preserve pre-existing roots."""

    retained: list[str] = []
    for path, existed in before.items():
        if existed:
            if path.exists():
                retained.append(f"retained pre-existing generated artifact: {path}")
            continue
        if not path.exists():
            continue
        if path.is_symlink():
            retained.append(f"retained changed generated artifact: {path}")
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            retained.append(f"could not remove generated artifact {path}: {exc}")
    return retained


def _component_inventory(destination: Path, args: argparse.Namespace) -> Mapping[str, Any]:
    try:
        from bearhug.setup_components import ComponentBundleError, build_components
    except ImportError as exc:
        raise ProjectSetupError(
            "portable component assets are unavailable; install Bear Hug assets"
        ) from exc
    kwargs: dict[str, Any] = {}
    for option, key in (
        ("memq", "memq"),
        ("graft", "graft"),
        ("codebase_memory", "codebase_memory"),
        ("memex_source", "memex_source"),
    ):
        value = getattr(args, option, None)
        if value:
            supplied = Path(value).expanduser()
            if not supplied.is_absolute():
                raise ProjectSetupError(f"--{option.replace('_', '-')} must be an absolute path")
            kwargs[key] = supplied
    try:
        inventory = build_components(destination, **kwargs)
    except ProjectSetupError:
        raise
    except ComponentBundleError as exc:
        raise ProjectSetupError(f"portable component assembly failed: {exc}") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise ProjectSetupError(f"portable component assembly failed: {exc}") from exc
    if not isinstance(inventory, Mapping):
        raise ProjectSetupError("portable component assembly returned an invalid inventory")
    return inventory


def _node() -> str:
    node = shutil.which("node")
    if not node:
        raise ProjectSetupError("required component 'node' is missing; install Node.js")
    return node


_EXTERNAL_TOOL_PROBE_TIMEOUT_SECONDS = 10.0


def _first_output_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _probe_external_tool(
    name: str, version_argv: Sequence[str], *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Detect one optional external CLI on PATH; never installs, downloads, or requires it.

    Used for `gopls`. `found` reflects PATH presence only. Once found, any
    probe failure -- a non-zero exit, a timeout, non-UTF-8 output, or an OS error launching it --
    still reports `found: true` with `version: None` ("found, version unknown"), never an error
    -- this function must never raise. `env`, when given, both narrows the PATH
    search and is the exact environment the probe command runs under; passed by a test as a
    scratch PATH pointing only at a fake binary, and left `None` in real use so the probe
    consults the process's actual environment.
    """
    search_path = env.get("PATH") if env is not None else None
    try:
        resolved = shutil.which(name, path=search_path)
    except OSError:
        # A directory or otherwise inspectable-but-unusable PATH entry; `shutil.which` already
        # skips these in the normal case, this is only the defensive fallback.
        resolved = None
    if resolved is None:
        return {"name": name, "found": False, "path": None, "version": None}
    version: str | None = None
    try:
        completed = subprocess.run(
            [resolved, *version_argv],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            # Never let a probed binary's own non-UTF-8 output raise; substitute the
            # standard replacement character for whatever cannot be decoded instead.
            errors="replace",
            check=False,
            timeout=_EXTERNAL_TOOL_PROBE_TIMEOUT_SECONDS,
            env=dict(env) if env is not None else None,
        )
        if completed.returncode == 0:
            version = _first_output_line(completed.stdout) or _first_output_line(completed.stderr)
    except (OSError, ValueError, subprocess.TimeoutExpired, UnicodeError):
        # Any other failure launching or reading the probe -- including a decode error
        # `errors="replace"` should already prevent, kept here as defense in depth -- is still
        # "found, version unknown", never an exception that reaches the caller.
        version = None
    return {"name": name, "found": True, "path": resolved, "version": version}


def _graft_cli(components: Mapping[str, Any]) -> Path:
    root = _component_path(components, "graft", directory=True)
    record = components["graft"]
    executable = record.get("executable") if isinstance(record, Mapping) else None
    path = (
        Path(executable).expanduser() if isinstance(executable, str) else root / "dist" / "cli.js"
    )
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ProjectSetupError(f"portable Graft CLI is unavailable: {path}")
    return path


def _run_checked(
    argv: Sequence[str], *, cwd: Path, timeout: float, label: str
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectSetupError(f"{label} unavailable or timed out: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "no command output").strip()[-2000:]
        raise ProjectSetupError(f"{label} failed ({result.returncode}): {detail}")
    return result


def _graft_stage(
    components: Mapping[str, Any], *, providers: Sequence[str], bundle_root: Path
) -> list[_FileSpec]:
    """Ask the pinned Graft package for provider files in a disposable Git worktree."""

    node = _node()
    cli = _graft_cli(components)
    agent_ids = ["claude"] if "claude" in providers else []
    if "codex" in providers:
        agent_ids.append("agents")
    stage = Path(tempfile.mkdtemp(prefix=".bearhug-graft-stage-", dir=bundle_root))
    try:
        _run_checked(
            ("git", "init", "-q", str(stage)), cwd=bundle_root, timeout=30, label="Git stage init"
        )
        command = [
            node,
            str(cli),
            "init",
            str(stage),
            "--no-build",
            "--no-global",
            "--agents",
            *agent_ids,
        ]
        _run_checked(command, cwd=stage, timeout=120, label="Graft init")
        result: list[_FileSpec] = []
        for source in sorted(stage.rglob("*")):
            if not source.is_file() or source.is_symlink() or ".git" in source.parts:
                continue
            relative = source.relative_to(stage).as_posix()
            if relative == "opencode.json":
                continue
            content, mode = _source_bytes({"source": str(source)}, label=f"Graft {relative}")
            if relative == ".mcp.json":
                try:
                    document = json.loads(content.decode())
                    servers = document.get("mcpServers", {})
                    if isinstance(servers, dict) and "graft" in servers:
                        servers["graft"] = {
                            "command": node,
                            "args": [str(cli), "mcp"],
                        }
                        content = _canonical_json(document)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProjectSetupError("Graft stage emitted invalid .mcp.json") from exc
            if relative.startswith(".claude/helpers/") and relative.endswith(".cjs"):
                # Graft's generated helper carries a build-time fallback path.  Point that
                # fallback at the sealed local package while retaining its normal resolver.
                text = content.decode(errors="strict")
                marker = "const BAKED = "
                if marker in text:
                    prefix, suffix = text.split(marker, 1)
                    end = suffix.find(";")
                    if end >= 0:
                        text = (
                            prefix
                            + marker
                            + 'path.join(dir, ".bearhug", "vendor", "graft", "dist", "claude")'
                            + suffix[end:]
                        )
                        content = text.encode()
            result.append(_FileSpec(relative, content, mode, "graft"))
        return result
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _adopt_existing_graft_files(specs: Sequence[_FileSpec], *, target: Path) -> list[_FileSpec]:
    """Keep established Graft-facing helpers and skills byte-for-byte during setup.

    Graft owns the fresh-project defaults, while the target project owns any copy that was already
    present.  This keeps setup additive when an in-flight repository has customized its helper or
    skill and still lets a clean repository receive the current staged files.
    """

    result: list[_FileSpec] = []
    for spec in specs:
        if spec.path in _GRAFT_PROJECT_FILES:
            existing = _read_regular(_target_file(target, spec.path), f"existing {spec.path}")
            if existing is not None:
                content, mode = existing
                # Fix round 2 (N5): `dataclasses.replace` carries every field this rebuild does
                # not name forward unchanged, including `normalize_identities`, so adding a
                # future `_FileSpec` field never becomes a silent drop here the way a
                # positional rebuild would. `_GRAFT_PROJECT_FILES` never names
                # `.claude/settings.json` (the one spec that carries a non-empty
                # `normalize_identities` today), so this is defensive, not a fix to an observed
                # loss -- see `test_adopt_existing_graft_files_preserves_every_file_spec_field`.
                result.append(replace(spec, content=content, mode=mode))
                continue
        result.append(spec)
    return result


def _service_env(args: argparse.Namespace) -> bytes | None:
    url = getattr(args, "embed_url", None)
    model = getattr(args, "embed_model", None)
    exclude_paths = getattr(args, "exclude_paths", None)
    if url is None and model is None and exclude_paths is None:
        return None
    url = url or "http://localhost:1239/v1"
    model = model or "nomic-embed-text-v1.5"
    if any(
        not isinstance(value, str) or not value or "\n" in value or "\r" in value or "\x00" in value
        for value in (url, model)
    ):
        raise ProjectSetupError("embedding service settings must be single-line text")
    values = {"MEMQ_EMBED_MODEL": model, "MEMQ_EMBED_URL": url}
    if exclude_paths is not None:
        # Always a single line: `_exclude_path_inputs`/`normalize_exclude_paths` already refuse
        # any glob containing a newline or NUL, and `json.dumps` never introduces one on a list
        # of plain strings.
        values[EXCLUDE_PATHS_ENV_KEY] = json.dumps(list(exclude_paths))
    # Sorted, exactly like `_merge_service_env` re-serializes on every later run: a value that
    # never changes must never register as a byte-level rewrite just because it crossed from
    # "written fresh" to "merged with what was already there".
    return "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode()


def _existing_service_settings(root: Path) -> dict[str, str]:
    settings: dict[str, str] = {}
    env_path = root / _ENV_FILE
    current = _read_regular(env_path, "setup service environment")
    if current is not None:
        try:
            text = current[0].decode()
        except UnicodeDecodeError as exc:
            raise ProjectSetupConflict("existing setup.env is not UTF-8") from exc
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ProjectSetupConflict("existing setup.env contains a malformed line")
            key, value = line.split("=", 1)
            settings[key] = value
    config = root / ".claude/settings.json"
    existing = _read_regular(config, "Claude settings")
    if existing is not None:
        try:
            document = json.loads(existing[0].decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectSetupConflict("existing Claude settings are not valid JSON") from exc
        env = document.get("env", {}) if isinstance(document, Mapping) else {}
        if isinstance(env, Mapping):
            for key in ("MEMQ_EMBED_URL", "MEMQ_EMBED_MODEL"):
                if isinstance(env.get(key), str):
                    settings.setdefault(key, env[key])
    return settings


def _service_settings(
    root: Path, *, embed_url: str | None, embed_model: str | None
) -> tuple[str, str]:
    existing = _existing_service_settings(root)
    url = (
        embed_url
        or existing.get("MEMQ_EMBED_URL")
        or os.environ.get("MEMQ_EMBED_URL")
        or "http://localhost:1239/v1"
    )
    model = (
        embed_model
        or existing.get("MEMQ_EMBED_MODEL")
        or os.environ.get("MEMQ_EMBED_MODEL")
        or "nomic-embed-text-v1.5"
    )
    if any(
        not isinstance(value, str) or not value or "\n" in value or "\r" in value or "\x00" in value
        for value in (url, model)
    ):
        raise ProjectSetupError("embedding service settings must be single-line text")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProjectSetupError("embedding service URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProjectSetupError("embedding service URL may not carry credentials or query secrets")
    return url, model


def _closed_commands(
    values: Sequence[Sequence[str] | str] | None,
    *,
    label: str,
) -> tuple[tuple[str, ...], ...]:
    """Normalize explicit CLI/programmatic commands to shell-free bounded argv arrays."""

    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        values = [values]  # type: ignore[list-item]
    result: list[tuple[str, ...]] = []
    for index, value in enumerate(values):
        if isinstance(value, str):
            try:
                items = shlex.split(value)
            except ValueError as exc:
                raise ProjectSetupError(f"{label} {index + 1} has invalid quoting") from exc
        elif isinstance(value, Sequence) and not isinstance(value, bytes):
            items = list(value)
        else:
            raise ProjectSetupError(f"{label} {index + 1} must be a command string or argv")
        if (
            not items
            or len(items) > 128
            or not all(
                isinstance(item, str)
                and item
                and "\x00" not in item
                and len(item.encode()) <= 64 * 1024
                for item in items
            )
        ):
            raise ProjectSetupError(f"{label} {index + 1} is not a bounded closed argv")
        result.append(tuple(items))
    return tuple(result)


def _automation_commands(
    root: Path,
    validation_commands: Sequence[Sequence[str] | str] | None,
    format_commands: Sequence[Sequence[str] | str] | None,
) -> tuple[tuple[tuple[str, ...], ...], tuple[dict[str, Any], ...]]:
    """Resolve project inputs, using conservative repository-native defaults when omitted."""

    validation = list(_closed_commands(validation_commands, label="validation command"))
    if validation_commands is None:
        if (root / "pyproject.toml").is_file():
            if (root / "uv.lock").is_file():
                validation.extend(
                    [
                        ("uv", "run", "ruff", "check", "."),
                        ("uv", "run", "pytest", "-q"),
                    ]
                )
            else:
                validation.extend(
                    [
                        ("python3", "-m", "ruff", "check", "."),
                        ("python3", "-m", "pytest", "-q"),
                    ]
                )
        if (root / "go.mod").is_file():
            validation.append(("go", "test", "./..."))
    explicit_format = _closed_commands(format_commands, label="format command")
    formatting: list[dict[str, Any]] = [
        {"argv": list(command), "suffixes": []} for command in explicit_format
    ]
    if format_commands is None and (root / "go.mod").is_file():
        formatting.append({"argv": ["gofmt", "-w", "{paths}"], "suffixes": [".go"]})
    return tuple(validation), tuple(formatting)


def _memory_source_inputs(values: Sequence[str] | None) -> tuple[dict[str, str], ...]:
    """Validate explicit project-owned MemQ markdown scopes (``label=glob``)."""

    if values is None:
        return ()
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, value in enumerate(values):
        if not isinstance(value, str) or "=" not in value:
            raise ProjectSetupError(
                f"memory source {index + 1} must use label=repository-relative-glob"
            )
        label, glob = value.split("=", 1)
        pure = PurePosixPath(glob)
        if (
            re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", label) is None
            or not glob
            or glob.startswith("/")
            or "\\" in glob
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ProjectSetupError(f"memory source {index + 1} is unsafe or ambiguous")
        identity = (label, glob)
        if identity not in seen:
            seen.add(identity)
            result.append({"type": "markdown", "glob": glob, "label": label})
    return tuple(result)


def _exclude_path_inputs(values: Sequence[str] | None) -> tuple[str, ...]:
    """Validate project-declared exclusion globs; delegates to the grounding engine's own
    validator so setup, the persisted receipt and grounding all agree on one safe syntax."""

    try:
        return normalize_exclude_paths(values)
    except GroundingError as exc:
        raise ProjectSetupError(str(exc)) from exc


def _existing_exclude_paths(root: Path) -> tuple[str, ...]:
    """The project-declared exclusion globs a previous setup run persisted, or ``()``."""

    raw = _existing_service_settings(root).get(EXCLUDE_PATHS_ENV_KEY)
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectSetupConflict("existing setup.env has an invalid exclude-path list") from exc
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ProjectSetupConflict("existing setup.env has an invalid exclude-path list")
    return _exclude_path_inputs(values)


def _resolved_exclude_paths(
    root: Path, exclude_paths: Sequence[str] | None, *, clear_exclude_paths: bool
) -> tuple[str, ...]:
    """Explicit ``--exclude-path`` values replace what was persisted; omitting the option
    keeps it; ``--clear-exclude-paths`` empties it. The two are mutually exclusive so a rerun
    can never silently discard one operator's exclusions with another's unrelated flag."""

    if clear_exclude_paths:
        if exclude_paths:
            raise ProjectSetupError(
                "--exclude-path and --clear-exclude-paths cannot both be given"
            )
        return ()
    if exclude_paths is not None:
        return _exclude_path_inputs(exclude_paths)
    return _existing_exclude_paths(root)


def _exclude_prefix(pattern: str) -> str:
    """The directory-style prefix a wildcard-free (or trailing-``*``) exclude pattern names.

    Mirrors ``bearhug.campaign.grounding``'s private helper of the same name; kept local rather
    than imported so this module never reaches into that one's private surface.
    """

    return pattern.rstrip("*").rstrip("/")


def _glob_overlaps_exclusion(glob_value: str, exclude_pattern: str) -> str:
    """How a MemQ source glob relates to one declared exclusion: ``"none"``, ``"full"`` or
    ``"partial"``.

    ``"full"``: every path the glob could ever match lies inside the exclusion -- safe to drop
    the whole source silently, since nothing legitimate is lost. ``"partial"``: the glob can
    match both inside and outside the exclusion (for example a source glob of
    ``_archive/*/*.md`` against an exclusion of one specific ``_archive/<name>`` subdirectory) --
    never safe to keep as-is or silently narrow, because MemQ's own recall hits carry no path
    (see ``bearhug.campaign.grounding``'s documented gap), so nothing downstream can filter an
    excluded hit back out once MemQ has indexed it. ``"none"``: the glob cannot match anything
    under the exclusion at all.

    Compared component by component against the exclusion's own directory-prefix reading
    (``_exclude_prefix``): a wildcard segment shared with the prefix keeps the comparison alive
    (it might match the prefix's literal segment) but also means the source is not exclusively
    inside the exclusion, which is exactly what makes the overlap partial rather than full.
    """

    prefix = _exclude_prefix(exclude_pattern)
    if not prefix:
        return "none"
    if glob_value == prefix or glob_value.startswith(prefix + "/"):
        return "full"
    glob_parts = glob_value.split("/")
    prefix_parts = prefix.split("/")
    any_wildcard_shared = False
    for glob_part, prefix_part in zip(glob_parts, prefix_parts, strict=False):
        if glob_part == "**":
            return "partial"
        if any(ch in glob_part for ch in "*?["):
            any_wildcard_shared = True
            if not fnmatch.fnmatchcase(prefix_part, glob_part):
                return "none"
        elif glob_part != prefix_part:
            return "none"
    if not any_wildcard_shared:
        # Every shared segment matched literally: the glob is either fully inside the exclusion
        # (already handled above) or fully outside it (a mismatch would have returned "none").
        # A pure-literal glob shorter than the exclusion's own depth names something at or above
        # the exclusion, never inside it.
        return "none"
    return "partial"


def _exclude_glob_covered(glob_value: str, exclude_paths: Sequence[str]) -> bool:
    """True when a MemQ source row's own glob falls entirely under a declared exclusion.

    MemQ has no exclude/ignore option of its own to write this into (see
    ``bearhug.campaign.grounding``'s "Project-declared exclusions"), so setup keeps an excluded
    scope out of ``.memq.json`` by never emitting its source row in the first place.
    """

    return any(_glob_overlaps_exclusion(glob_value, pattern) == "full" for pattern in exclude_paths)


def _exclude_glob_partial_conflict(glob_value: str, exclude_paths: Sequence[str]) -> str | None:
    """The first declared exclusion this glob partially overlaps, or ``None``.

    Partial overlap is refused outright rather than trimmed or kept: MemQ's own recall hits
    carry no path (see ``_glob_overlaps_exclusion``), so once an overlapping source is indexed,
    no later consumer can tell an excluded hit apart from a kept one. The operator must narrow
    the source glob (or the exclusion) so the two never overlap.
    """

    for pattern in exclude_paths:
        if _glob_overlaps_exclusion(glob_value, pattern) == "partial":
            return pattern
    return None


def _check_embedding_service(
    root: Path, url: str, *, dry_run: bool, model: str | None = None
) -> None:
    if dry_run:
        return
    try:
        request = Request(
            url.rstrip("/") + "/embeddings",
            data=json.dumps({"model": model, "input": "Bear Hug setup check"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10.0) as response:
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("embedding response is too large")
        document = json.loads(payload)
        rows = document.get("data") if isinstance(document, dict) else None
        vector = rows[0].get("embedding") if isinstance(rows, list) and rows else None
        if (
            not isinstance(vector, list)
            or not vector
            or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool) for value in vector
            )
        ):
            raise ValueError("service returned no embedding")
    except HTTPError as exc:
        detail = exc.read(8192).decode(errors="replace").lower()
        setting = "embed_model" if exc.code in {400, 422} or "model" in detail else "embed_url"
        raise EmbeddingServiceError(
            f"embedding service rejected the setup check (HTTP {exc.code}); "
            "check its URL and loaded embedding model",
            setting,
        ) from exc
    except (OSError, ValueError, AttributeError) as exc:
        raise EmbeddingServiceError(
            f"embedding service is unavailable or incompatible at {url}; "
            "start it or supply --embed-url"
        ) from exc


def _merge_service_env(existing: bytes, desired: bytes) -> bytes:
    """Add missing non-secret service variables while retaining project choices."""

    def parse(raw: bytes) -> dict[str, str]:
        try:
            text = raw.decode()
        except UnicodeDecodeError as exc:
            raise ProjectSetupConflict("setup.env must be UTF-8 text") from exc
        values: dict[str, str] = {}
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ProjectSetupConflict("setup.env contains a malformed line")
            key, value = line.split("=", 1)
            if not key or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in key):
                raise ProjectSetupConflict("setup.env contains an invalid variable name")
            values[key] = value
        return values

    old = parse(existing)
    for key, value in parse(desired).items():
        old[key] = value
    return ("".join(f"{key}={old[key]}\n" for key in sorted(old))).encode()


def _starter_specs(target: Path, providers: Sequence[str]) -> list[_FileSpec]:
    """Seed usable memory controls without replacing a project's authored conventions."""
    paths = [
        (f"memex/{name}", f"docs/memex/{name}") for name in ("SCHEMA.md", "index.md", "log.md")
    ]
    paths.append(("hooks/stamp.sh", "scripts/hooks/stamp.sh"))
    if "claude" in providers:
        paths.append(("skills/decide/SKILL.md", ".claude/skills/decide/SKILL.md"))
    if "codex" in providers:
        paths.append(("skills/decide/SKILL.md", ".agents/skills/decide/SKILL.md"))
    result = []
    for source, relative in paths:
        existing = _read_regular(_target_file(target, relative), f"starter {relative}")
        content, mode = (
            existing
            if existing is not None
            else (
                (REPO_ROOT / "setup" / source).read_bytes(),
                0o755 if source.endswith(".sh") else 0o644,
            )
        )
        result.append(_FileSpec(relative, content, mode, "memex", "starter"))
    return result


def _provider_specs(
    *,
    providers: Sequence[str],
    graft_specs: Sequence[_FileSpec],
    node: str,
    graft_cli: Path,
    memq_path: str,
    memex_hook_path: str,
    stop_path: str,
    bundle_root: Path,
    target: Path,
    embed_url: str | None,
    embed_model: str | None,
    validation_commands: Sequence[Sequence[str]],
    format_commands: Sequence[Mapping[str, Any]],
    memory_sources: Sequence[Mapping[str, str]],
    include_codebase_memory: bool,
) -> list[_FileSpec]:
    graft_settings: dict[str, Any] = {}
    graft_instructions = b""
    specs: list[_FileSpec] = []
    for spec in graft_specs:
        if spec.path == ".claude/settings.json":
            try:
                value = json.loads(spec.content.decode())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProjectSetupError("Graft stage emitted invalid Claude settings") from exc
            if not isinstance(value, Mapping):
                raise ProjectSetupError("Graft stage emitted non-object Claude settings")
            graft_settings = _normalize_graft_claude_settings(value)
            continue
        if spec.path in {".mcp.json", "AGENTS.md"}:
            # .mcp.json is generated below with the packaged command.  Graft's AGENTS.md is only
            # useful for Codex and is included when that provider is selected.
            if spec.path == "AGENTS.md" and "codex" in providers:
                graft_instructions = spec.content
            continue
        specs.append(spec)
    instructions = (REPO_ROOT / "setup/project-instructions.md").read_bytes()
    # The managed block stays short (Claude Code warns above 40 K characters of CLAUDE.md); the
    # full workflow is an installed doc the block points at.
    specs.append(
        _FileSpec(
            "docs/bearhug/WORKFLOW.md",
            (REPO_ROOT / "setup/project-workflow.md").read_bytes(), 0o644, "bearhug",
        )
    )
    for provider, filename in (("claude", "CLAUDE.md"), ("codex", "AGENTS.md")):
        if provider in providers:
            content = instructions + (b"\n" + graft_instructions if provider == "codex" else b"")
            specs.append(
                _FileSpec(
                    filename, _merge_instructions(b"", content), 0o644, "bearhug", "instructions"
                )
            )
    specs.append(_FileSpec("docs/superpowers/plans/.gitkeep", b"", 0o644, "bearhug"))
    graft_root = graft_cli.parents[1]
    for source in sorted(graft_root.rglob("*")):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(graft_root).as_posix()
        content, mode = _source_bytes({"source": str(source)}, label=f"Graft runtime {relative}")
        specs.append(_FileSpec(f".bearhug/vendor/graft/{relative}", content, mode, "graft-runtime"))
    specs.append(
        _FileSpec(
            "scripts/bin/graft",
            b"""#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
exec node "$root/.bearhug/vendor/graft/dist/cli.js" "$@"
""",
            0o755,
            "graft-runtime",
        )
    )
    # Bundle the exact source tree used by every installed command.  The former architecture-only
    # subset left the campaign launcher importing mutable modules from the Bear Hug checkout,
    # which made a target's behavior change when that checkout changed or disappeared.  The
    # source tree is stdlib-only and remains small enough for a per-project immutable copy.
    library_root = REPO_ROOT / "src/bearhug"
    library_sources = [
        path.relative_to(library_root).as_posix()
        for path in sorted(library_root.rglob("*.py"))
        if path.is_file() and not path.is_symlink()
    ]
    for source in library_sources:
        specs.append(
            _FileSpec(
                f".bearhug/lib/bearhug/{source}",
                (REPO_ROOT / "src/bearhug" / source).read_bytes(),
                0o644,
                "bearhug",
            )
        )
        if "codex" in providers:
            specs.append(
                _FileSpec(
                    f".codex/bearhug-host/lib/bearhug/{source}",
                    (library_root / source).read_bytes(), 0o644, "codex-host-runtime",
                )
            )
    # The promoted Stop evaluator write resolver remains the captured authority for shell and
    # edit-tool effects.  Install that immutable runtime beside the Bear Hug package so the
    # normalized Codex adapter does not copy or weaken its semantics.
    legacy_runtime_root = REPO_ROOT / "runtime" / "bearhug_runtime"
    # `VERSION` travels with the Python files: `runtime_version()` reads it for every telemetry
    # record, and a runtime vendored without it makes each hook's telemetry write fail open
    # silently, so nothing is ever recorded.
    runtime_files = [*legacy_runtime_root.rglob("*.py"), legacy_runtime_root / "VERSION"]
    for source in sorted(runtime_files):
        if source.is_file() and not source.is_symlink():
            relative = source.relative_to(legacy_runtime_root).as_posix()
            specs.append(
                _FileSpec(
                    f".bearhug/lib/bearhug_runtime/{relative}",
                    source.read_bytes(),
                    0o644,
                    "bearhug-runtime",
                )
            )
            if "codex" in providers:
                specs.append(
                    _FileSpec(
                        f".codex/bearhug-host/lib/bearhug_runtime/{relative}",
                        source.read_bytes(), 0o644, "codex-host-runtime",
                    )
                )
    arch_launcher = (
        b"#!/bin/sh\nset -eu\n"
        b"export PYTHONDONTWRITEBYTECODE=1\n"
        b'root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)\n'
        b'export PYTHONPATH="$root/.bearhug/lib"\n'
        b"exec python3 -m bearhug.project_architecture "
        b'--root "$root" "$@"\n'
    )
    specs.append(_FileSpec("scripts/bin/bearhug-arch", arch_launcher, 0o755, "bearhug"))
    for source, destination in (
        ("project_work.py", "scripts/bearhug_work.py"),
        ("project_native.py", "scripts/bearhug_native.py"),
    ):
        specs.append(
            _FileSpec(
                destination, (REPO_ROOT / "src/bearhug" / source).read_bytes(), 0o644, "bearhug"
            )
        )
    work_launcher = (
        b"#!/bin/sh\nset -eu\n"
        b"export PYTHONDONTWRITEBYTECODE=1\n"
        b'root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)\n'
        b'exec python3 "$root/scripts/bearhug_work.py" '
        b'--root "$root" "$@"\n'
    )
    specs.append(_FileSpec("scripts/bin/bearhug-work", work_launcher, 0o755, "bearhug"))
    campaign_launcher = (
        b"#!/bin/sh\nset -eu\n"
        b"export PYTHONDONTWRITEBYTECODE=1\n"
        b'root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)\n'
        # The launcher is intentionally independent of the development checkout.  Runtime
        # paths are explicit so packaged ``paths.py`` cannot mistake ``.bearhug/lib`` for the
        # project.  The packaged path resolver derives a machine-local, checkout-specific
        # artifact root outside the subject; do not persist a machine path in the project.
        b'export BEARHUG_RUNTIME_ROOT="$root/.bearhug"\n'
        b'export BEARHUG_PROJECT_ROOT="$root"\n'
        b'export PYTHONPATH="$root/.bearhug/lib"\n'
        b"exec python3 -m bearhug.project_campaign "
        b'--root "$root" "$@"\n'
    )
    specs.append(_FileSpec("scripts/bin/bearhug-campaign", campaign_launcher, 0o755, "bearhug"))
    # Existing project readers remain intact; the managed dashboard calls bearhug-work directly.
    existing_reader = _read_regular(_target_file(target, "scripts/plan-board.sh"), "board reader")
    reader = (
        existing_reader[0]
        if existing_reader
        else (
            b"#!/bin/sh\nset -eu\n"
            b'root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
            b'exec "$root/scripts/bin/bearhug-work" board\n'
        )
    )
    specs.append(
        _FileSpec(
            "scripts/plan-board.sh",
            reader,
            existing_reader[1] if existing_reader else 0o755,
            "bearhug",
            "starter",
        )
    )
    if "codex" in providers:
        hook_path = "scripts/hooks/codex-hook.py"
        specs.append(
            _FileSpec(
                hook_path, (REPO_ROOT / "setup/hooks/codex-hook.py").read_bytes(), 0o644, "codex"
            )
        )
        specs.append(
            _FileSpec(
                ".codex/bearhug-host/codex-hook.py",
                (REPO_ROOT / "setup/hooks/codex-hook.py").read_bytes(),
                0o644, "codex-host-runtime",
            )
        )
        command = "python3 -I .codex/bearhug-host/codex-hook.py"
        events = (
            "SessionStart",
            "SessionEnd",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PreCompact",
            "PostCompact",
            "SubagentStart",
            "SubagentStop",
            "Stop",
            "Interrupt",
        )
        hooks = {
            event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 3 if event in {"Interrupt", "SessionEnd"} else 45,
                        }
                    ]
                }
            ]
            for event in events
        }
        specs.append(
            _FileSpec(
                ".codex/hooks.json",
                _canonical_json({"hooks": hooks}),
                0o644,
                "codex",
                "json-settings",
            )
        )
    if "claude" in providers:
        forbidden_command_gate_path = "scripts/hooks/forbidden-command-gate.py"
        specs.append(
            _FileSpec(
                forbidden_command_gate_path,
                (REPO_ROOT / "setup/hooks/forbidden-command-gate.py").read_bytes(),
                0o644,
                "forbidden_command_gate",
            )
        )
        specs.append(
            _FileSpec(
                "scripts/hooks/forbidden_command_rules.py",
                (REPO_ROOT / "setup/hooks/forbidden_command_rules.py").read_bytes(),
                0o644,
                "forbidden_command_gate",
            )
        )
        # `memex-hook.sh pre-question` shells out to this small entry point (same split as
        # forbidden-command-gate.py/forbidden_command_rules.py above and
        # stop-coordinator.py/_bearhug) to score a question against accepted decisions with the
        # grounding engine's own selection rather than raw word overlap. Read directly from this
        # repository rather than through the portable component bundle, matching the two specs
        # above -- it is a project extension of memex-hook.sh, not a Bear Hug runtime primitive.
        specs.append(
            _FileSpec(
                "scripts/hooks/memex-pre-question-score.py",
                (REPO_ROOT / "setup/hooks/memex-pre-question-score.py").read_bytes(),
                0o644,
                "memex_question_scorer",
            )
        )
        forbidden_command_gate_command = (
            f'python3 "$CLAUDE_PROJECT_DIR"/{forbidden_command_gate_path}'
        )
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f'"$CLAUDE_PROJECT_DIR"/{memex_hook_path} session-start',
                                "timeout": _MEMEX_HOOK_TIMEOUT_SECONDS,
                            }
                        ]
                    }
                ],
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f'"$CLAUDE_PROJECT_DIR"/{memq_path} hook',
                                "timeout": _MEMQ_HOOK_TIMEOUT_SECONDS,
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "AskUserQuestion",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f'"$CLAUDE_PROJECT_DIR"/{memex_hook_path} pre-question',
                                "timeout": _MEMEX_PRE_QUESTION_TIMEOUT_SECONDS,
                            }
                        ],
                    },
                    {
                        "matcher": "Skill",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f'"$CLAUDE_PROJECT_DIR"/{memex_hook_path} pre-decide',
                                "timeout": _MEMEX_HOOK_TIMEOUT_SECONDS,
                            }
                        ],
                    },
                    # A project-supplied forbidden-command gate (Bear Hug's generalization of
                    # Barracuda's hard-safety.py; see setup/hooks/forbidden_command_rules.py).
                    # Registered on both Bash and the edit tools, same shape hard-safety.py used,
                    # so a rule scoped to file paths can see Edit/Write/MultiEdit too. Inert when
                    # the project has not written .bearhug/forbidden-commands.json.
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": forbidden_command_gate_command,
                                "timeout": 10,
                            }
                        ],
                    },
                    {
                        "matcher": "Edit|Write|MultiEdit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": forbidden_command_gate_command,
                                "timeout": 10,
                            }
                        ],
                    },
                ],
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|MultiEdit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f'"$CLAUDE_PROJECT_DIR"/{memex_hook_path} post-edit',
                                "timeout": _MEMEX_HOOK_TIMEOUT_SECONDS,
                            }
                        ],
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f'python3 "$CLAUDE_PROJECT_DIR"/{stop_path}',
                                "timeout": _STOP_COORDINATOR_TIMEOUT_SECONDS,
                            },
                            {
                                "type": "command",
                                "command": f'"$CLAUDE_PROJECT_DIR"/{memex_hook_path} stop',
                                "timeout": _MEMEX_HOOK_TIMEOUT_SECONDS,
                            },
                        ]
                    }
                ],
            }
        }
        # Keep the installed command relocatable.  An absolute target path would be frozen into
        # settings.json and make snapshot replay point back at the live checkout; Claude already
        # supplies this project root to every command through CLAUDE_PROJECT_DIR.
        work_hook = (
            'python3 "$CLAUDE_PROJECT_DIR"/scripts/bearhug_work.py '
            '--root "$CLAUDE_PROJECT_DIR" native-hook --provider claude'
        )
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"):
            settings["hooks"].setdefault(event, []).append(
                {
                    "hooks": [{"type": "command", "command": work_hook, "timeout": 10}],
                }
            )
        if embed_url is not None or embed_model is not None:
            settings["env"] = {
                "MEMQ_EMBED_URL": embed_url or "http://localhost:1239/v1",
                "MEMQ_EMBED_MODEL": embed_model or "nomic-embed-text-v1.5",
            }
        specs.append(
            _FileSpec(
                ".claude/settings.json",
                _canonical_json(_merge_json(graft_settings, settings)),
                0o644,
                "claude",
                "claude-settings",
                normalize_identities=_graft_hook_identities(graft_settings.get("hooks")),
            )
        )
        mcp_servers: dict[str, Any] = {"graft": {"command": "scripts/bin/graft", "args": ["mcp"]}}
        if include_codebase_memory:
            mcp_servers["codebase-memory-mcp"] = dict(_CODEBASE_MEMORY_MCP_ENTRY)
        specs.append(
            _FileSpec(
                ".mcp.json",
                _canonical_json({"mcpServers": mcp_servers}),
                0o644,
                "graft",
                "mcp-config",
            )
        )
    if "codex" in providers:
        codex_servers: dict[str, Any] = {
            "graft": {
                "command": "scripts/bin/graft",
                "args": ["mcp"],
                "cwd": ".",
            },
        }
        if include_codebase_memory:
            codex_servers["codebase-memory-mcp"] = dict(_CODEBASE_MEMORY_CODEX_ENTRY)
        specs.append(
            _FileSpec(
                ".codex/config.toml",
                _codex_toml(codex_servers),
                0o644,
                "codex",
                "codex-config",
            )
        )
    specs.append(
        _FileSpec(
            ".bearhug/automation.json",
            _canonical_json(
                {
                    "schema_version": "1",
                    "record_kind": "project_automation",
                    "validation_commands": [list(command) for command in validation_commands],
                    "format_commands": [dict(command) for command in format_commands],
                    "source_scopes": ["project-owned-memory", "project-owned-architecture"],
                    "capabilities": {
                        "background_checks": "required",
                        "dlv": "required-after-go-write",
                        "join_key": "required-at-completion",
                        "review": "required-after-source-write",
                        "review_brief_attestation": "unavailable-in-native-hook-payload",
                        "task_durability": "required-on-write",
                    },
                }
            ),
            0o644,
            "bearhug",
        )
    )
    if "codex" in providers:
        # Native callbacks read only protected policy. The .bearhug file is an observation for
        # existing UI and non-Codex integrations, never input to the direct Codex gate.
        automation = next(spec for spec in specs if spec.path == ".bearhug/automation.json")
        specs.append(
            _FileSpec(
                ".codex/bearhug-host/automation.json", automation.content,
                0o644, "codex-host-runtime",
            )
        )
    specs.append(
        _FileSpec(
            ".memq.json",
            _canonical_json(
                {
                    "sources": [
                        {"type": "markdown", "glob": "docs/*.md", "label": "doc"},
                        {
                            "type": "markdown",
                            "glob": "docs/superpowers/plans/*.md",
                            "label": "plan",
                        },
                        {"type": "markdown", "glob": "docs/plans/*.md", "label": "existing-plan"},
                        {
                            "type": "markdown",
                            "glob": "docs/memex/decisions/*.md",
                            "label": "memex-decision",
                        },
                        {
                            "type": "markdown",
                            "glob": "docs/memex/schemas/*.md",
                            "label": "memex-schema",
                        },
                        {
                            "type": "markdown",
                            "glob": "docs/memex/syntheses/*.md",
                            "label": "memex-synthesis",
                        },
                        {
                            "type": "markdown",
                            "glob": "docs/memex/concepts/*.md",
                            "label": "memex-concept",
                        },
                        {
                            "type": "markdown",
                            "glob": "docs/memex/overviews/*.md",
                            "label": "memex-overview",
                        },
                        {"type": "markdown", "glob": "README*.md", "label": "readme"},
                        *[dict(source) for source in memory_sources],
                    ]
                }
            ),
            0o644,
            "memq",
            "json-settings",
        )
    )
    specs.append(_FileSpec("docs/memex/decisions/.gitkeep", b"", 0o644, "memex"))
    specs.append(
        _FileSpec(
            ".gitignore",
            (
                b".memq/\ngraft/\n.bearhug/setup.env\n"
                b".bearhug/codebase-memory/\n.codebase-memory/\n"
                b".bearhug/telemetry/\n.bearhug/codex-hooks/\n.automation-stamps/\n"
                b".bearhug/normalized-hooks/\n.bearhug/hook-state/\n"
                b".bearhug/native-work/\n.bearhug/project-work.lock\nscripts/__pycache__/\n"
                b".bearhug/project-board-owner.json\n"
                b".bearhug/project-board-transition.json\n"
                b".bearhug/architecture/\n.bearhug/lib/**/__pycache__/\n.bearhug/campaign.json\n"
                b".codex/bearhug-host/**/__pycache__/\n"
                b".bearhug/onboarding.json\n.bearhug/terminal-driver.json\n"
                b".bearhug/project-terminal.json\n"
                b".bearhug/project-setup.json\n.bearhug/vendor/\n"
                b".bearhug/packets/\n.bearhug/packet-injections/\n"
                b".bearhug/packet-unavailable/\n"
                b".claude/settings.local.json\n.bearhug/qualification-index.json\n"
                b"scripts/bin/memq\nscripts/bin/memq-bin\nscripts/bin/memexlint\n"
                b"scripts/bin/codebase-memory-mcp\n"
                b"scripts/hooks/**/__pycache__/\n"
            ),
            0o644,
            "setup",
            "gitignore",
        )
    )
    _check_duplicate_specs_allow_configs(specs)
    return specs


def _check_duplicate_specs_allow_configs(specs: Sequence[_FileSpec]) -> None:
    seen: dict[str, _FileSpec] = {}
    for spec in specs:
        prior = seen.get(spec.path)
        if prior is None:
            seen[spec.path] = spec
            continue
        if spec.path in {".claude/settings.json", ".mcp.json", ".codex/config.toml"}:
            if prior.content != spec.content:
                raise ProjectSetupConflict(f"setup generated conflicting entries for {spec.path}")
            continue
        raise ProjectSetupConflict(f"setup generated duplicate target path: {spec.path}")


def _write_atomic(path: Path, content: bytes, mode: int) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except OSError as exc:
        with suppress(OSError):
            os.close(descriptor)
        raise ProjectSetupError(f"cannot publish {path}: {exc}") from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _ensure_parents(root: Path, path: Path, created: list[Path]) -> None:
    relative = path.parent.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise ProjectSetupConflict(f"setup parent is not a physical directory: {current}")
            continue
        current.mkdir(mode=0o700 if current.name == ".bearhug" else 0o755)
        created.append(current)


def _plan_files(
    root: Path, specs: Sequence[_FileSpec], previous: Mapping[str, str]
) -> tuple[
    list[tuple[_FileSpec, bytes | None, int | None, str]], list[str], list[str],
    list[dict[str, str]], list[dict[str, str]],
    list[dict[str, Any]], list[dict[str, Any]],
    list[dict[str, str]], list[dict[str, Any]],
]:
    planned: list[tuple[_FileSpec, bytes | None, int | None, str]] = []
    changed: list[str] = []
    unchanged: list[str] = []
    added_registrations: list[dict[str, str]] = []
    removed_registrations: list[dict[str, str]] = []
    kept_duplicates: list[dict[str, Any]] = []
    normalized_timeouts: list[dict[str, Any]] = []
    already_held_registrations: list[dict[str, str]] = []
    timeout_filled: list[dict[str, Any]] = []
    for spec in specs:
        path = _target_file(root, spec.path)
        current = _read_regular(path, f"target {spec.path}")
        desired = spec.content
        if current is not None and spec.merge == "claude-settings":
            desired = _merge_claude(
                current[0], desired, normalize_identities=spec.normalize_identities
            )
            # Mirror the merge above purely to observe what it newly
            # adds, removes, declines to dedup, or normalizes. `normalize_identities` must be
            # the same set the merge above used, or this plan would report
            # a rewrite the merge did not make.
            added_registrations.extend(
                _added_claude_hook_registrations(
                    current[0],
                    spec.content,
                    removed=removed_registrations,
                    kept=kept_duplicates,
                    already_held=already_held_registrations,
                    normalized=normalized_timeouts,
                    timeout_filled=timeout_filled,
                    normalize_identities=spec.normalize_identities,
                )
            )
        elif current is not None and spec.merge == "mcp-config":
            desired = _merge_mcp(current[0], desired)
        elif current is not None and spec.merge == "codex-config":
            desired = _merge_codex(current[0], desired)
        elif current is not None and spec.merge == "json-settings":
            desired = _merge_json_settings(current[0], desired)
        elif current is not None and spec.merge == "instructions":
            desired = _merge_instructions(current[0], desired)
        elif current is not None and spec.merge == "gitignore":
            desired = _merge_gitignore(current[0], desired)
        elif current is not None and spec.merge == "service-env":
            desired = _merge_service_env(current[0], desired)
        if current is None:
            if spec.merge == "claude-settings":
                # Nothing to merge against, so nothing to observe adding —
                # on a first install, every registration Bear Hug generates counts as added.
                try:
                    fresh_document = json.loads(desired.decode())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    fresh_document = {}
                added_registrations.extend(
                    _hook_registrations(
                        fresh_document.get("hooks")
                        if isinstance(fresh_document, Mapping)
                        else None
                    )
                )
            planned.append((spec, None, None, _sha256(desired)))
            changed.append(spec.path)
            continue
        old_content, old_mode = current
        old_hash = _sha256(old_content)
        if old_hash == _sha256(desired) and old_mode == spec.mode:
            planned.append((spec, old_content, old_mode, old_hash))
            unchanged.append(spec.path)
            continue
        owned_hash = previous.get(spec.path)
        if spec.merge is not None:
            # Merge formats retain foreign keys/comments and therefore legitimately differ from
            # the generated candidate even on first setup.  Their own merge function has already
            # rejected malformed input or a conflicting required server.
            planned.append((spec, old_content, old_mode, _sha256(desired)))
            changed.append(spec.path)
            continue
        if old_hash == _sha256(desired):
            # The bytes already are the desired bytes; only the mode differs. A fresh Git clone
            # or worktree checks tracked files out at 0644/0755 regardless of a spec's own mode
            # (0600 for setup's own generated records), with or without a receipt. That is a
            # mode repair to report as changed, never a conflict — a real byte difference still
            # falls through to the receipt check below exactly as before.
            planned.append((spec, old_content, old_mode, old_hash))
            changed.append(spec.path)
            continue
        if owned_hash != old_hash:
            raise ProjectSetupConflict(f"target file differs from setup-owned bytes: {spec.path}")
        planned.append((spec, old_content, old_mode, _sha256(desired)))
        changed.append(spec.path)
    return (
        planned,
        changed,
        unchanged,
        added_registrations,
        removed_registrations,
        kept_duplicates,
        normalized_timeouts,
        already_held_registrations,
        timeout_filled,
    )


def _receipt(
    target: Path,
    providers: Sequence[str],
    planned: Sequence[tuple[_FileSpec, bytes | None, int | None, str]],
    commands: Sequence[dict[str, Any]],
    env_path: str | None,
) -> bytes:
    files = [
        {
            "path": spec.path,
            "component": spec.component,
            "mode": f"{spec.mode:04o}",
            "sha256": digest,
        }
        for spec, _old, _old_mode, digest in sorted(planned, key=lambda row: row[0].path)
    ]
    return _canonical_json(
        {
            "record_kind": "project_setup_receipt",
            "schema_version": "1",
            "target": target.as_posix(),
            "providers": sorted(providers),
            "files": files,
            "service_env": env_path,
            "commands": list(commands),
            "limitations": [
                "Setup proves local file ownership and command results only.",
                (
                    "Provider qualification, effective configuration, and runtime behavior "
                    "remain unverified."
                ),
            ],
        }
    )


_CODEX_MCP_SERVER_BLOCK_RE = re.compile(
    r"(?m)^\[mcp_servers\.codebase-memory-mcp\]\n(?:(?!\[)[^\n]*\n?)*"
)


def _strip_codebase_memory_toml_entry(text: str) -> str | None:
    """Remove exactly the ``[mcp_servers.codebase-memory-mcp]`` TOML section, text-preserving.

    Mirrors this module's existing TOML-editing philosophy (`_merge_codex`'s
    own line-level text surgery, never a parse-then-reserialize round trip that would silently
    drop an operator's own formatting, comments, or hand-added sections elsewhere in the file).
    Matches from the section's own header line up to (not including) the next ``[...]`` header
    or the end of the file, so it works regardless of which server sorts next to it. Returns
    `None` if the section cannot be found as the expected single match -- signals the caller to
    leave the file untouched and report a failure rather than risk writing back something
    unintended.
    """
    new_text, count = _CODEX_MCP_SERVER_BLOCK_RE.subn("", text, count=1)
    if count != 1:
        return None
    return new_text


def _process_disabled_codebase_memory(
    root: Path,
    providers: Sequence[str],
    previous_rows: Sequence[Mapping[str, str]],
    *,
    perform: bool,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Classify, and when `perform` is True actually carry out, cleanup of a Codebase Memory
    install a past run owned that this run no longer wants.

    Scoped to `codebase_memory` only (item 4); no other component uses this function or its
    rule. Never raises: every filesystem or parsing failure this function can anticipate is
    caught and turned into a report entry, never an exception -- the caller
    additionally wraps the whole call as a last-resort safety net for anything this function
    itself does not anticipate.

    A file named by a previous receipt row is removed only if ALL of: (a) its
    `path` is a member of `_CODEBASE_MEMORY_KNOWN_PATHS` -- the closed allowlist derived from the
    exact same literal `_component_specs` uses to build the component's own file spec, so this
    check cannot silently drift out of sync with what setup actually installs; (b) the row's
    `component` is `"codebase_memory"`; (c) its current bytes still hash to the row's recorded
    sha256 (unmodified since Bear Hug wrote it). Checked in this order, not (a)-then-(b)-then-(c)
    literally: path SAFETY is decided before path RECOGNITION -- a path that is a
    symlink, escapes the project, or is absolute is never even compared against the allowlist by
    string equality, since it was never safely resolved to compare in the first place, and is
    reported `kept_unsafe_path` instead; deciding this never dereferences the path (the existing
    `_target_file`/`_relative_path` guards this reuses unchanged are already lstat-based and
    lexical; this function only additionally catches what they raise instead of letting it
    propagate). Only once a path is confirmed safe is it compared against the allowlist; a safe
    path that is not a member is reported `kept_unknown_path` and never even read from disk. A
    row that fails the sha comparison is reported `kept_modified`. `kept_unknown_path` and
    `kept_unsafe_path` rows are also restored into the receipt by the caller (see
    `_restore_unremoved_receipt_rows`) so a later run keeps seeing them rather than silently
    losing track; `kept_modified` is not, matching the original removal design (an operator's own
    edit is not something this run continues to claim as owned). A receipt that lists the same
    path in more than one row is processed once.

    A provider's `codebase-memory-mcp` MCP entry is removed only if its current parsed value is
    byte-for-byte one of `_CODEBASE_MEMORY_MCP_ENTRY` / `_CODEBASE_MEMORY_CODEX_ENTRY` -- the same
    literals `_provider_specs` writes when the component is enabled; otherwise it is left and
    reported as `kept_foreign`. This never inspects any OTHER server key, so an operator's own
    differently-named entry is untouched without needing special-case code.
    `.bearhug/codebase-memory/` is the project's index database: always data, never removed,
    reported as `left_data` when present.

    With `perform=False` (a dry run, or read-only classification), nothing is written; the
    returned report describes what a real run would do, using the same field names a real run
    reports. With `perform=True`, each removal is attempted and, if it raises `OSError`,
    downgraded out of the report's removed lists into the returned `preserve` tuple instead --
    the caller uses that, alongside `kept_unknown_path` and `kept_unsafe_path`, to keep the
    corresponding receipt rows and report a warning.

    Returns `(report, preserve)`. `report` is `None` when there is nothing to say at all: no
    previous receipt row for this component, no `codebase-memory-mcp` entry in any selected
    provider's config, and no `.bearhug/codebase-memory/` directory -- a project that never had
    the component (item 5's "nothing reported" case). `preserve` is every path (file removal
    failures, unknown-path rows, unsafe-path rows) whose previous receipt row must still be
    written back into the new receipt.
    """
    files_removed: list[str] = []
    kept_modified: list[str] = []
    kept_unknown_path: list[str] = []
    kept_unsafe_path: list[str] = []
    entries_removed: list[str] = []
    kept_foreign: list[str] = []
    left_data: list[str] = []
    failed: list[str] = []
    seen_paths: set[str] = set()

    for row in previous_rows:
        if row.get("component") != "codebase_memory":
            continue
        relative = row.get("path")
        if not isinstance(relative, str) or relative in seen_paths:
            # A receipt that lists the same path twice is processed once. A malformed
            # (non-string) path can never appear from `_previous_receipt_file_rows`, which
            # already validates this; skipped defensively rather than trusted regardless.
            continue
        seen_paths.add(relative)
        # Path safety is decided FIRST, before this row's path is even compared against
        # the allowlist below -- a symlinked, `../`-escaping, or absolute path must never raise
        # out of this function, and must never be treated as "known" or "unknown" by string
        # comparison alone, since a malformed path was never safely resolved to compare in the
        # first place. `_target_file`/`_relative_path` already do exactly the lexical,
        # lstat-based (never dereferencing) checks this needs; reused unchanged (never edited by
        # this fix), just no longer allowed to propagate past this one row.
        try:
            target_path = _target_file(root, relative)
        except ProjectSetupError:
            kept_unsafe_path.append(relative)
            continue
        # Only once a path is confirmed safe to resolve does it get compared against the
        # component's own closed allowlist. Never act on a row whose path is not a member, no
        # matter what its component label or sha256 say -- and never even read its content.
        if relative not in _CODEBASE_MEMORY_KNOWN_PATHS:
            kept_unknown_path.append(relative)
            continue
        try:
            current = _read_regular(target_path, f"existing {relative}")
        except ProjectSetupError:
            kept_unsafe_path.append(relative)
            continue
        if current is None:
            continue
        content, _mode = current
        if _sha256(content) != row.get("sha256"):
            kept_modified.append(relative)
            continue
        if not perform:
            files_removed.append(relative)
            continue
        try:
            target_path.unlink()
            files_removed.append(relative)
        except OSError:
            failed.append(relative)

    provider_configs: list[tuple[str, str]] = []
    if "claude" in providers:
        provider_configs.append((".mcp.json", "mcp"))
    if "codex" in providers:
        provider_configs.append((".codex/config.toml", "codex"))

    for relative, kind in provider_configs:
        path = _target_file(root, relative)
        current = _read_regular(path, f"existing {relative}")
        if current is None:
            continue
        content, mode = current
        try:
            text = content.decode()
        except UnicodeDecodeError:
            continue
        if kind == "mcp":
            try:
                document: Any = json.loads(text)
            except json.JSONDecodeError:
                continue
            servers = document.get("mcpServers") if isinstance(document, Mapping) else None
            desired_entry = _CODEBASE_MEMORY_MCP_ENTRY
        else:
            try:
                document = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            servers = document.get("mcp_servers") if isinstance(document, Mapping) else None
            desired_entry = _CODEBASE_MEMORY_CODEX_ENTRY
        if not isinstance(servers, Mapping) or "codebase-memory-mcp" not in servers:
            continue
        if servers["codebase-memory-mcp"] != desired_entry:
            kept_foreign.append(relative)
            continue
        if not perform:
            entries_removed.append(relative)
            continue
        try:
            if kind == "mcp":
                new_document = copy.deepcopy(document)
                del new_document["mcpServers"]["codebase-memory-mcp"]
                new_content = _canonical_json(new_document)
            else:
                new_text = _strip_codebase_memory_toml_entry(text)
                if new_text is None:
                    raise ProjectSetupError(
                        f"could not locate the codebase-memory-mcp section to remove: {relative}"
                    )
                new_content = new_text.encode()
            _write_atomic(path, new_content, mode)
            entries_removed.append(relative)
        except (OSError, ProjectSetupError):
            failed.append(relative)

    if (root / ".bearhug/codebase-memory").is_dir():
        left_data.append(".bearhug/codebase-memory")

    if not (
        files_removed
        or kept_modified
        or kept_unknown_path
        or kept_unsafe_path
        or entries_removed
        or kept_foreign
        or left_data
        or failed
    ):
        return None, ()
    report = {
        "component": "codebase_memory",
        "files_removed": tuple(files_removed),
        "entries_removed": tuple(entries_removed),
        "kept_modified": tuple(kept_modified),
        "kept_unknown_path": tuple(kept_unknown_path),
        "kept_unsafe_path": tuple(kept_unsafe_path),
        "kept_foreign": tuple(kept_foreign),
        "left_data": tuple(left_data),
    }
    preserve = tuple(failed) + tuple(kept_unknown_path) + tuple(kept_unsafe_path)
    return report, preserve


def _restore_unremoved_receipt_rows(
    receipt_path: Path,
    previous_rows: Sequence[Mapping[str, str]],
    preserve_paths: Sequence[str],
) -> None:
    """The receipt this run just wrote must still list
    a `codebase_memory` row this run did not actually remove -- an `OSError` mid-removal, an
    unknown path (`kept_unknown_path`), or an unsafe path (`kept_unsafe_path`) -- otherwise a
    later run could never rediscover it to retry or keep flagging it.

    Reads back the receipt this same run just wrote, adds the previous row for each path in
    `preserve_paths` (skipping one already present), and rewrites the receipt file. Each
    restored row uses its OWN previous `mode`, not a guess -- correct for any path, not only the
    one component path this module knows the fixed mode of. Left unguarded on purpose: if the
    receipt itself cannot be patched, the run should fail loudly rather than silently lose track
    of a row it did not resolve.
    """
    if not preserve_paths:
        return
    current = _read_regular(receipt_path, "setup receipt")
    if current is None:
        return
    raw, mode = current
    document = json.loads(raw.decode())
    files = document.get("files", [])
    existing_paths = {row.get("path") for row in files if isinstance(row, Mapping)}
    for row in previous_rows:
        if (
            row.get("component") == "codebase_memory"
            and row["path"] in preserve_paths
            and row["path"] not in existing_paths
        ):
            files.append(
                {
                    "path": row["path"],
                    "component": "codebase_memory",
                    "mode": row["mode"],
                    "sha256": row["sha256"],
                }
            )
            existing_paths.add(row["path"])
    document["files"] = files
    _write_atomic(receipt_path, _canonical_json(document), mode)


def setup_project(
    project_root: Path | str,
    *,
    provider: str = "both",
    dry_run: bool = False,
    embed_url: str | None = None,
    embed_model: str | None = None,
    memq: Path | str | None = None,
    graft: Path | str | None = None,
    codebase_memory: Path | str | None = None,
    memex_source: Path | str | None = None,
    architecture_rules: Path | str | None = None,
    bundle_root: Path | str | None = None,
    campaigns: str = "on",
    work_authority: str = "managed",
    board_row: str | None = None,
    validation_commands: Sequence[Sequence[str] | str] | None = None,
    format_commands: Sequence[Sequence[str] | str] | None = None,
    memory_sources: Sequence[str] | None = None,
    exclude_paths: Sequence[str] | None = None,
    clear_exclude_paths: bool = False,
    _lock_held: bool = False,
) -> SetupResult:
    """Assemble and install all requested local components in one explicit target.

    ``exclude_paths`` (repeatable, ``--exclude-path`` on the CLI) is the project's own declared
    exclusion list: repository-relative globs Bear Hug never indexes or grounds against,
    wherever it can enforce that itself (see ``bearhug.campaign.grounding``'s "Project-declared
    exclusions"). ``None`` (the default) keeps whatever a previous run persisted; an explicit
    value replaces it; ``clear_exclude_paths=True`` empties it. Persisted in ``.bearhug/setup.env``
    alongside the embedding service settings, the same non-secret, carried-over pattern.
    """

    root = _physical_directory(project_root, "project_root")
    _git_root(root)
    if provider == "both":
        providers = ("claude", "codex")
    elif provider in _PROVIDERS:
        providers = (provider,)
    else:
        raise ProjectSetupError(f"unsupported provider selection: {provider!r}")
    if campaigns not in {"on", "off"}:
        raise ProjectSetupError("campaign adoption must be explicitly 'on' or 'off'")
    if work_authority not in {"managed", "project-board"}:
        raise ProjectSetupError("work authority must be 'managed' or 'project-board'")
    if work_authority == "managed":
        if board_row is not None:
            raise ProjectSetupError("--board-row requires --work-authority project-board")
        authority_record: dict[str, Any] = {"mode": "managed"}
    else:
        if campaigns != "off":
            raise ProjectSetupError("project BOARD authority requires --campaigns off")
        if board_row is None or not re.fullmatch(r"[1-9][0-9]*", board_row):
            raise ProjectSetupError("project BOARD authority requires one positive --board-row")
        from bearhug.providers.work_store import (
            WorkArtifactStoreError,
            authority_candidate,
            read_subject_file,
        )

        try:
            candidate = authority_candidate(root, board_row)
            authority_bytes = read_subject_file(root, candidate["authority_path"])
        except WorkArtifactStoreError as exc:
            raise ProjectSetupError(f"project BOARD authority is unavailable: {exc}") from exc
        authority_record = {
            "mode": "project_board",
            "board_path": "docs/superpowers/plans/BOARD.md",
            "parser_path": "scripts/hooks/boardrows.py",
            "board_row": board_row,
            "authority_path": candidate["authority_path"],
            "authority_sha256": _sha256(authority_bytes),
        }
    # Setup may replace the pinned controller used by subsequent campaign workers.  Never do so
    # while an attached session can still own a process, lease, or unresolved spend fence.  A
    # preview remains available so the operator can inspect the eventual upgrade safely.
    campaign_binding = root / ".bearhug" / "campaign.json"
    if not dry_run and campaign_binding.is_file():
        _require_settled_campaign_custody(root)
    args = argparse.Namespace(
        provider=provider,
        embed_url=embed_url,
        embed_model=embed_model,
        memq=memq,
        graft=graft,
        codebase_memory=codebase_memory,
        memex_source=memex_source,
        architecture_rules=architecture_rules,
        bundle_root=bundle_root,
    )
    destination = _prepare_bundle_root(args, root)
    if not _lock_held:
        from bearhug.project_hook_runtime import _state_lock

        with _bundle_lock(destination), (
            nullcontext() if dry_run else _state_lock(root, "automation-config")
        ):
            return setup_project(
                root,
                provider=provider,
                dry_run=dry_run,
                embed_url=embed_url,
                embed_model=embed_model,
                memq=memq,
                graft=graft,
                codebase_memory=codebase_memory,
                memex_source=memex_source,
                architecture_rules=architecture_rules,
                bundle_root=bundle_root,
                campaigns=campaigns,
                work_authority=work_authority,
                board_row=board_row,
                validation_commands=validation_commands,
                format_commands=format_commands,
                memory_sources=memory_sources,
                exclude_paths=exclude_paths,
                clear_exclude_paths=clear_exclude_paths,
                _lock_held=True,
            )
    selected_exclude_paths = _resolved_exclude_paths(
        root, exclude_paths, clear_exclude_paths=clear_exclude_paths
    )
    service_url, service_model = _service_settings(
        root, embed_url=embed_url, embed_model=embed_model
    )
    _check_embedding_service(root, service_url, dry_run=dry_run, model=service_model)
    # Gopls is detected, never installed; nothing is written into the
    # project for it. Safe to probe unconditionally, in dry runs too -- it never touches `root`.
    external_tools = (_probe_external_tool("gopls", ("version",)),)
    inventory = _component_inventory(destination, args)
    components = inventory.get("components")
    if not isinstance(components, Mapping):
        raise ProjectSetupError("portable component inventory has no components map")
    graft_cli = _graft_cli(components)
    node = _node()
    validations, formatters = _automation_commands(root, validation_commands, format_commands)
    selected_memory_sources = []
    for source in _memory_source_inputs(memory_sources):
        conflict = _exclude_glob_partial_conflict(source["glob"], selected_exclude_paths)
        if conflict is not None:
            raise ProjectSetupError(
                f"memory source {source['label']!r} ({source['glob']!r}) partially overlaps "
                f"declared exclusion {conflict!r}: it would match paths both inside and outside "
                "the exclusion, and MemQ's own recall carries no path to filter one back out "
                "later; narrow the memory source glob or the exclusion so they never overlap"
            )
        if _exclude_glob_covered(source["glob"], selected_exclude_paths):
            continue
        selected_memory_sources.append(source)
    selected_memory_sources = tuple(selected_memory_sources)
    # Whether Codebase Memory is installed at all follows the inventory,
    # not the raw `codebase_memory` argument -- `build_components` (setup_components.py) is the
    # single place that decides whether the component was actually requested and built.
    codebase_memory_enabled = isinstance(components.get("codebase_memory"), Mapping)
    # Read before any of this run's own writes, so it always reflects the PRIOR
    # run's receipt regardless of when in this run's body it is consulted.
    previous_cbm_rows = [] if codebase_memory_enabled else _previous_receipt_file_rows(root)
    component_specs = _component_specs(inventory, providers=providers, target=root)
    graft_specs = _adopt_existing_graft_files(
        _graft_stage(components, providers=providers, bundle_root=destination), target=root
    )
    specs = _provider_specs(
        providers=providers,
        graft_specs=graft_specs,
        node=node,
        graft_cli=graft_cli,
        memq_path="scripts/bin/memq",
        memex_hook_path="scripts/hooks/memex-hook.sh",
        stop_path="scripts/hooks/stop-coordinator.py",
        bundle_root=destination,
        target=root,
        # Use the resolved settings so a rerun can project an existing Codex/setup.env choice
        # into newly installed Claude hooks.  The values are non-secret URL/model settings.
        embed_url=service_url,
        embed_model=service_model,
        validation_commands=validations,
        format_commands=formatters,
        memory_sources=selected_memory_sources,
        include_codebase_memory=codebase_memory_enabled,
    )
    specs = component_specs + specs + _starter_specs(root, providers)
    if architecture_rules is not None:
        rules_content, _ = _source_bytes(
            {"source": str(Path(architecture_rules).expanduser()), "mode": "0644"},
            label="architecture rules",
        )
        specs.append(_FileSpec("docs/arch-rules.json", rules_content, 0o644, "architecture-rules"))
    specs.append(
        _FileSpec(
            ".bearhug/adoption.json",
            _canonical_json(
                {
                    "schema_version": "1",
                    "record_kind": "bearhug_project_adoption",
                    "campaigns": campaigns,
                    "work_authority": authority_record,
                    # The authoritative copy lives in the gitignored `.bearhug/setup.env`
                    # (`_existing_exclude_paths` reads it back on the next run); this is a
                    # git-tracked, human-visible mirror of the same resolved list.
                    "exclude_paths": list(selected_exclude_paths),
                }
            ),
            0o600,
            "setup",
        )
    )
    _check_duplicate_specs_allow_configs(specs)
    env_content = _service_env(
        argparse.Namespace(
            embed_url=service_url, embed_model=service_model,
            exclude_paths=selected_exclude_paths,
        )
    )
    if env_content is not None:
        specs.append(_FileSpec(_ENV_FILE, env_content, 0o600, "service", "service-env"))
    if not specs:
        raise ProjectSetupError("portable component inventory is empty")
    previous = _load_previous_receipt(root)
    (
        planned,
        changed,
        unchanged,
        added_registrations,
        removed_registrations,
        kept_duplicates,
        normalized_timeouts,
        already_held_registrations,
        timeout_filled,
    ) = _plan_files(root, specs, previous)
    command_specs = [
        _CommandSpec(
            "graft-build",
            (node, str(graft_cli), "build", root.as_posix()),
            timeout=180.0,
        ),
        _CommandSpec("memq-index", (str(root / "scripts/bin/memq"), "index"), timeout=180.0),
    ]
    # Configure only the new project cache; preserve any deliberate existing setting.
    # Never run this, and so never create the cache directory, when
    # Codebase Memory was not requested.
    if (
        codebase_memory_enabled
        and not _target_file(root, ".bearhug/codebase-memory/_config.db").exists()
    ):
        command_specs.append(
            _CommandSpec(
                "codebase-memory-auto-index",
                (
                    str(root / _CODEBASE_MEMORY_LAUNCHER_PATH),
                    "config",
                    "set",
                    "auto_index",
                    "true",
                ),
                timeout=15.0,
            )
        )
    unsupported: list[str] = []
    if "codex" in providers:
        codex_spec = next(row[0] for row in planned if row[0].path == ".codex/config.toml")
        existing_codex = _read_regular(root / ".codex/config.toml", "Codex config")
        codex_content = (
            codex_spec.content
            if existing_codex is None
            else _merge_codex(existing_codex[0], codex_spec.content)
        )
        codex_document = tomllib.loads(codex_content.decode())
        if codex_document.get("features", {}).get("hooks") is not True:
            unsupported.append(
                "Codex hooks remain disabled by the project's explicit features.hooks setting; "
                "direct project automation will not execute until the project enables them"
            )
        unsupported.append(
            "Codex campaign execution requires a current local qualification index and "
            "operational evidence from each run; direct project hooks require Codex hook trust, "
            "and the native reviewer callback cannot attest to the dispatched review brief"
        )
    warnings: list[str] = []
    command_results: list[dict[str, Any]] = []
    receipt_path = root / _SETUP_RECEIPT
    receipt_spec = _FileSpec(_SETUP_RECEIPT, b"", 0o600, "setup", None)
    command_metadata = [
        {"component": command.component, "argv": list(command.argv), "required": command.required}
        for command in command_specs
    ]
    command_metadata.insert(
        0,
        {
            "component": "graft-init",
            "argv": [
                node,
                str(graft_cli),
                "init",
                "<disposable-stage>",
                "--no-build",
                "--no-global",
                "--agents",
                *(["claude"] if "claude" in providers else []),
                *(["agents"] if "codex" in providers else []),
            ],
            "required": True,
        },
    )
    receipt_content = _receipt(
        root, providers, planned, command_metadata, _ENV_FILE if env_content is not None else None
    )
    receipt_current = _read_regular(receipt_path, "setup receipt")
    if receipt_current is not None and _sha256(receipt_current[0]) == _sha256(receipt_content):
        unchanged.append(_SETUP_RECEIPT)
    else:
        if (
            receipt_current is not None
            and previous
            and previous.get(_SETUP_RECEIPT) not in {None, _sha256(receipt_current[0])}
        ):
            raise ProjectSetupConflict("setup receipt changed outside setup")
        changed.append(_SETUP_RECEIPT)
    if dry_run:
        # "a dry run reports the same without writing" -- classify only.
        # Classification must never raise; a dry run writes nothing regardless,
        # but stays consistent with the real run's "never crash" guarantee rather than relying
        # solely on this one call never having a bug.
        removed_components: tuple[dict[str, Any], ...] = ()
        if not codebase_memory_enabled:
            try:
                preview, _preserve = _process_disabled_codebase_memory(
                    root, providers, previous_cbm_rows, perform=False
                )
            except Exception as exc:
                warnings = [
                    *warnings,
                    f"could not preview Codebase Memory cleanup: {exc}",
                ]
            else:
                if preview is not None:
                    removed_components = (preview,)
        return SetupResult(
            root,
            providers,
            True,
            tuple(sorted(set(changed))),
            tuple(sorted(set(unchanged))),
            tuple(unsupported),
            tuple(warnings),
            (),
            None,
            tuple(command_metadata),
            tuple(added_registrations),
            tuple(removed_registrations),
            tuple(kept_duplicates),
            tuple(normalized_timeouts),
            external_tools,
            removed_components,
            tuple(already_held_registrations),
            tuple(timeout_filled),
        )

    originals: dict[Path, tuple[bytes, int] | None] = {}
    created_dirs: list[Path] = []
    all_specs = list(planned)
    all_specs.append(
        (
            receipt_spec,
            receipt_current[0] if receipt_current else None,
            receipt_current[1] if receipt_current else None,
            _sha256(receipt_content),
        )
    )
    # Tool setup commands may create these declared generated paths.
    # Record their pre-setup state so a failed command can remove only artifacts created here.
    artifact_before = _artifact_preflight(root)
    # The component build and merge happen before this point.  Re-read every destination once
    # immediately before the first write so an IDE edit during assembly cannot be silently lost.
    (
        rechecked,
        _recheck_changed,
        _recheck_unchanged,
        _recheck_added,
        _recheck_removed,
        _recheck_kept,
        _recheck_normalized,
        _recheck_already_held,
        _recheck_timeout_filled,
    ) = _plan_files(root, specs, previous)
    expected_plan = [(spec.path, digest) for spec, _old, _mode, digest in planned]
    observed_plan = [(spec.path, digest) for spec, _old, _mode, digest in rechecked]
    if expected_plan != observed_plan:
        raise ProjectSetupConflict("target changed while the setup bundle was assembled")
    written: dict[Path, bytes] = {}
    try:
        for spec, old_content, old_mode, _digest in all_specs:
            path = _target_file(root, spec.path)
            # Re-read immediately before each write.  The bundle and merge work can take long
            # enough for an editor to change a target file after the initial plan; never replace
            # that edit silently.
            observed = _read_regular(path, f"target {spec.path}")
            if old_content is None:
                changed_since_plan = observed is not None
            else:
                changed_since_plan = (
                    observed is None or observed[0] != old_content or observed[1] != old_mode
                )
            if changed_since_plan:
                raise ProjectSetupConflict(f"target changed while setup was writing: {spec.path}")
            originals[path] = (
                (old_content, old_mode)
                if old_content is not None and old_mode is not None
                else None
            )
            _ensure_parents(root, path, created_dirs)
            content = receipt_content if spec.path == _SETUP_RECEIPT else spec.content
            if (
                spec.path != _SETUP_RECEIPT
                and spec.merge == "claude-settings"
                and old_content is not None
            ):
                # This write loop is the THIRD `_merge_claude` call site
                # (after the plan in `_plan_files` and its reporting observer,
                # `_added_claude_hook_registrations`) and the one whose output actually reaches
                # disk. Passing `spec.normalize_identities` here too is required, not optional
                # -- omitting it here alone would silently produce a run that REPORTS
                # `8000 -> 8` while the file it writes keeps `8000`.
                content = _merge_claude(
                    old_content, spec.content, normalize_identities=spec.normalize_identities
                )
            elif (
                spec.path != _SETUP_RECEIPT
                and spec.merge == "mcp-config"
                and old_content is not None
            ):
                content = _merge_mcp(old_content, spec.content)
            elif (
                spec.path != _SETUP_RECEIPT
                and spec.merge == "codex-config"
                and old_content is not None
            ):
                content = _merge_codex(old_content, spec.content)
            elif (
                spec.path != _SETUP_RECEIPT
                and spec.merge == "json-settings"
                and old_content is not None
            ):
                content = _merge_json_settings(old_content, spec.content)
            elif spec.merge == "instructions" and old_content is not None:
                content = _merge_instructions(old_content, spec.content)
            elif (
                spec.path != _SETUP_RECEIPT
                and spec.merge == "gitignore"
                and old_content is not None
            ):
                content = _merge_gitignore(old_content, spec.content)
            elif (
                spec.path != _SETUP_RECEIPT
                and spec.merge == "service-env"
                and old_content is not None
            ):
                content = _merge_service_env(old_content, spec.content)
            if old_content is not None and old_mode == spec.mode and old_content == content:
                continue
            _write_atomic(path, content, spec.mode)
            written[path] = content
        try:
            from bearhug.setup_checks import verify_runtime
        except ImportError as exc:
            raise ProjectSetupError("runtime verification is unavailable") from exc
        runtime_check = verify_runtime(root)
        if runtime_check.get("status") != "ready":
            raise ProjectSetupError(
                "installed runtime verification failed: "
                + str(runtime_check.get("reason", "unknown"))
            )
        for command in command_specs:
            env = {
                **os.environ,
                "MEMQ_REPO": root.as_posix(),
                "MEMQ_CONFIG": (root / ".memq.json").as_posix(),
                "MEMQ_DB": (root / ".memq/db").as_posix(),
                "MEMQ_EMBED_URL": embed_url or service_url,
                "MEMQ_EMBED_MODEL": embed_model or service_model,
            }
            try:
                completed = subprocess.run(
                    command.argv,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=command.timeout,
                    env=env,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ProjectSetupError(
                    f"{command.component} setup command unavailable or timed out: {exc}"
                ) from exc
            result = {
                "component": command.component,
                "argv": list(command.argv),
                "returncode": completed.returncode,
            }
            if completed.returncode:
                detail = (completed.stderr or "").strip()[-2048:]
                result["error"] = detail
                raise ProjectSetupError(
                    f"{command.component} setup command failed ({completed.returncode}): {detail}"
                )
            command_results.append(result)
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, original in reversed(list(originals.items())):
            expected = written.get(path)
            if expected is not None:
                try:
                    current = _read_regular(path, f"rollback target {path}")
                except ProjectSetupError as rollback_exc:
                    rollback_errors.append(f"{path}: {rollback_exc}")
                    continue
                if current is None or _sha256(current[0]) != _sha256(expected):
                    rollback_errors.append(f"{path}: changed after setup write; preserved")
                    continue
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _write_atomic(path, original[0], original[1])
            except (OSError, ProjectSetupError) as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        for directory in reversed(created_dirs):
            with suppress(OSError):
                directory.rmdir()
        rollback_errors.extend(_rollback_artifacts(artifact_before))
        if rollback_errors:
            raise ProjectSetupError(
                f"{exc}; rollback incomplete: {'; '.join(rollback_errors)}"
            ) from exc
        raise
    # Only after every other write above has already succeeded.
    # This whole phase is wrapped as a last-resort safety net. Every failure mode
    # `_process_disabled_codebase_memory` can anticipate (an unsafe path, an unknown path, a
    # failed unlink or rewrite) is already classified without raising, inside that function; this
    # `try` exists for anything it does not anticipate. Either way, the run's own success status
    # is never affected by this cleanup step, and no receipt row this run did not prove it
    # removed is ever silently dropped.
    removed_components: tuple[dict[str, Any], ...] = ()
    if not codebase_memory_enabled:
        try:
            cbm_report, cbm_preserve = _process_disabled_codebase_memory(
                root, providers, previous_cbm_rows, perform=True
            )
        except Exception as exc:
            warnings.append(
                "Codebase Memory cleanup hit an unexpected error and removed nothing further "
                f"this run: {exc}; the setup receipt still lists what it owned before this run"
            )
            unresolved = [
                row["path"]
                for row in previous_cbm_rows
                if row.get("component") == "codebase_memory"
            ]
            _restore_unremoved_receipt_rows(receipt_path, previous_cbm_rows, unresolved)
        else:
            if cbm_report is not None:
                removed_components = (cbm_report,)
            if cbm_preserve:
                warnings.append(
                    "could not remove or verify some Bear Hug-owned Codebase Memory item(s): "
                    + ", ".join(sorted(cbm_preserve))
                    + "; left in place and the setup receipt still lists them"
                )
                _restore_unremoved_receipt_rows(receipt_path, previous_cbm_rows, cbm_preserve)
    return SetupResult(
        root,
        providers,
        False,
        tuple(sorted(set(changed))),
        tuple(sorted(set(unchanged))),
        tuple(unsupported),
        tuple(warnings),
        (),
        receipt_path,
        tuple(command_results or command_metadata),
        tuple(added_registrations),
        tuple(removed_registrations),
        tuple(kept_duplicates),
        tuple(normalized_timeouts),
        external_tools,
        removed_components,
        tuple(already_held_registrations),
        tuple(timeout_filled),
    )



_CUSTODY_PROBE = """
import json, sys
from pathlib import Path

from bearhug.project_campaign import _settled_attached_session
from bearhug.project_campaign import binding as campaign_status

root = Path(sys.argv[1])
try:
    attached = campaign_status(root)
    settled = _settled_attached_session(root) if attached.get("session_id") is not None else {}
except Exception as exc:
    print(json.dumps({"ok": False, "reason": str(exc)}))
else:
    print(json.dumps({"ok": True, "settled": settled is not None}))
"""


def _require_settled_campaign_custody(root: Path) -> None:
    """Refuse setup while an attached session can still own a process, lease or spend fence.

    The check runs in a subprocess that shares the campaign worker's runtime root
    (``BEARHUG_RUNTIME_ROOT=<target>/.bearhug``) rather than Bear Hug's own.  A prepared
    record seals absolute paths derived from the root of the process that wrote it, and
    re-deriving them here, under Bear Hug's ambient root, rejects a valid record and reports
    it as unsettled custody -- which made setup structurally unrunnable against any project
    with an attached campaign.
    """

    environment = dict(os.environ)
    environment["BEARHUG_RUNTIME_ROOT"] = str(root / ".bearhug")
    probe = subprocess.run(
        [sys.executable, "-c", _CUSTODY_PROBE, str(root)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(REPO_ROOT),
    )
    if probe.returncode != 0:
        raise ProjectSetupError(
            "attached campaign custody could not be read: "
            f"{(probe.stderr or probe.stdout).strip()[-400:]}"
        )
    try:
        verdict = json.loads(probe.stdout)
    except ValueError as exc:
        raise ProjectSetupError("attached campaign custody probe returned no verdict") from exc
    if not verdict.get("ok"):
        # Surface the controller's own reason.  Collapsing every cause into one sentence sent
        # the operator down a stop/recovery path that could not apply.
        raise ProjectSetupError(
            "attached campaign custody must finish its ordinary stop/recovery path before "
            f"setup: {verdict.get('reason')}"
        )
    if not verdict.get("settled"):
        raise ProjectSetupError(
            "campaign binding is incomplete; recover or remove it through campaign control "
            "before setup"
        )

def add_setup_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "setup", help="install portable Bear Hug components in a Git project"
    )
    parser.add_argument("target", help="explicit absolute target Git worktree")
    parser.add_argument("--provider", choices=("claude", "codex", "both"), default="both")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "show target changes; private bundle/stage builds may run, but target files and "
            "tool settings and indexes are not changed"
        ),
    )
    parser.add_argument(
        "--embed-url", help="local embedding service URL (non-secret; saved for subsequent runs)"
    )
    parser.add_argument(
        "--embed-model", help="local embedding model id (non-secret; saved for subsequent runs)"
    )
    parser.add_argument("--memq", help="explicit local MemQ source or binary")
    parser.add_argument("--graft", help="explicit local pinned Graft source or binary")
    parser.add_argument(
        "--codebase-memory", help="absolute path to standalone Codebase Memory MCP binary"
    )
    parser.add_argument("--memex-source", help="explicit Memex helper source tree")
    parser.add_argument(
        "--architecture-rules",
        help="explicit project-approved architecture rules to install as docs/arch-rules.json",
    )
    parser.add_argument("--bundle-root", help="private component bundle cache directory")
    parser.add_argument(
        "--campaigns",
        choices=("on", "off"),
        default="on",
        help="campaign adoption policy; use off for direct automation-only adoption",
    )
    parser.add_argument(
        "--work-authority",
        choices=("managed", "project-board"),
        default="managed",
        help="select Bear Hug WORK.json or one existing project-authored BOARD row",
    )
    parser.add_argument(
        "--board-row",
        help="positive numeric BOARD row selected with --work-authority project-board",
    )
    parser.add_argument(
        "--validate",
        action="append",
        help="project validation command (repeatable; parsed once to a shell-free argv)",
    )
    parser.add_argument(
        "--format-command",
        action="append",
        help=(
            "project formatting command (repeatable; receives changed paths, or use a literal "
            "{paths} argv item)"
        ),
    )
    parser.add_argument(
        "--memory-source",
        action="append",
        help="project-owned MemQ source as label=repository-relative-glob (repeatable)",
    )
    parser.add_argument(
        "--exclude-path",
        action="append",
        help=(
            "project-declared exclusion, a repository-relative glob (repeatable; saved for "
            "subsequent runs); never indexed or bound wherever Bear Hug can enforce it itself"
        ),
    )
    parser.add_argument(
        "--clear-exclude-paths",
        action="store_true",
        help="remove every previously saved --exclude-path instead of adding to them",
    )
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.set_defaults(func=run_setup)


def run_setup(args: argparse.Namespace) -> int:
    try:
        for attempt in range(3):
            try:
                result = setup_project(
                    args.target,
                    provider=getattr(args, "provider", "both"),
                    dry_run=getattr(args, "dry_run", False),
                    embed_url=getattr(args, "embed_url", None),
                    embed_model=getattr(args, "embed_model", None),
                    memq=getattr(args, "memq", None),
                    graft=getattr(args, "graft", None),
                    codebase_memory=getattr(args, "codebase_memory", None),
                    memex_source=getattr(args, "memex_source", None),
                    architecture_rules=getattr(args, "architecture_rules", None),
                    bundle_root=getattr(args, "bundle_root", None),
                    campaigns=getattr(args, "campaigns", "on"),
                    work_authority=getattr(args, "work_authority", "managed"),
                    board_row=getattr(args, "board_row", None),
                    validation_commands=getattr(args, "validate", None),
                    format_commands=getattr(args, "format_command", None),
                    memory_sources=getattr(args, "memory_source", None),
                    exclude_paths=getattr(args, "exclude_path", None),
                    clear_exclude_paths=getattr(args, "clear_exclude_paths", False),
                )
                break
            except EmbeddingServiceError as exc:
                if not sys.stdin.isatty() or getattr(args, "dry_run", False) or attempt == 2:
                    raise
                print(str(exc), file=sys.stderr)
                label = (
                    "Embedding service URL" if exc.setting == "embed_url" else "Embedding model id"
                )
                print(f"{label}: ", end="", file=sys.stderr, flush=True)
                try:
                    value = input().strip()
                except EOFError as ended:
                    raise ProjectSetupError("input ended; setup cancelled") from ended
                if not value:
                    raise ProjectSetupError("no service setting supplied; setup cancelled") from exc
                setattr(args, exc.setting, value)

    except ProjectSetupError as exc:
        print(f"setup error: {exc}", file=sys.stderr)
        if getattr(args, "format", "human") == "json":
            # Every exit path honors --format json, not only success, so a calling script
            # can parse the result uniformly. The stderr line and exit code are unchanged; this
            # only adds the same top-level keys SetupResult.to_mapping() uses, filled with what
            # a failed run actually knows.
            print(json.dumps(
                {
                    "record_kind": "project_setup_result",
                    "target": str(getattr(args, "target", "")),
                    "providers": [],
                    "dry_run": bool(getattr(args, "dry_run", False)),
                    "ok": False,
                    "changed": [],
                    "unchanged": [],
                    "unsupported": [],
                    "warnings": [],
                    "errors": [str(exc)],
                    "receipt_path": None,
                    "commands": [],
                    "added_registrations": [],
                    "removed_registrations": [],
                    "kept_duplicates": [],
                    "normalized_timeouts": [],
                    "external_tools": [],
                    "removed_components": [],
                    "already_held_registrations": [],
                    "timeout_filled": [],
                },
                ensure_ascii=False, sort_keys=True, indent=2,
            ))
        return 2
    if getattr(args, "format", "human") == "json":
        print(json.dumps(result.to_mapping(), ensure_ascii=False, sort_keys=True, indent=2))
    else:
        mode = "preview" if result.dry_run else "installed"
        print(f"setup {mode}: {result.target}")
        print(
            f"providers={','.join(result.providers)} "
            f"changed={len(result.changed)} unchanged={len(result.unchanged)}"
        )
        if result.receipt_path:
            print(f"receipt: {result.receipt_path}")
        if result.unsupported:
            for item in result.unsupported:
                print(f"limitation: {item}")
        if not result.dry_run and "codex" in result.providers:
            print(
                "next: start a new Codex session in the target and use /hooks to review "
                "and trust the installed hooks; setup does not grant trust"
            )
        if result.warnings:
            for item in result.warnings:
                print(f"warning: {item}")
        if result.added_registrations:
            # This run put an owned Claude hook registration back —
            # everything, on a fresh install; only what an operator had removed, on a repeat.
            for entry in result.added_registrations:
                print(
                    f"added: {entry.get('event')} {entry.get('matcher') or '*'} "
                    f"{entry.get('command')}"
                )
        if result.removed_registrations:
            # This run deleted its own copy of a hook identity that a
            # different-spelling, equal-tool-set group already provided.
            for entry in result.removed_registrations:
                print(
                    f"removed: {entry.get('event')} {entry.get('matcher') or '*'} "
                    f"{entry.get('command')} kept_under={entry.get('kept_under') or '*'}"
                )
        if result.kept_duplicates:
            # Setup found its own hook identity also held by a differently spelled,
            # equal-tool-set group whose handler for it is not byte-identical, so an edited
            # survivor never stands in for Bear Hug's own copy: either setup already held a
            # copy and declined to delete it, or it installed its own handler fresh beside
            # the non-identical foreign one. Either way both registrations remain, and this
            # reports the pair.
            for entry in result.kept_duplicates:
                differs = ",".join(entry.get("differs") or [])
                print(
                    f"kept_duplicate: {entry.get('event')} {entry.get('matcher') or '*'} "
                    f"{entry.get('command')} kept_under={entry.get('kept_under') or '*'} "
                    f"differs={differs or '(none)'}"
                )
        if result.already_held_registrations:
            # Setup found a hook identity it would otherwise have created a
            # fresh group for already held, byte-identically, by a differently spelled,
            # equal-tool-set group -- so it added nothing (adding would double-fire).
            for entry in result.already_held_registrations:
                print(
                    f"already held: {entry.get('event')} {entry.get('command')} "
                    f"under \"{entry.get('held_under') or '*'}\" (nothing added)"
                )
        if result.normalized_timeouts:
            # This block only prints; the rewrite itself already happened in `_merge_hooks`,
            # which both permits and records it: an
            # existing millisecond timeout on a handler this run itself manages was rewritten
            # to Claude Code's native seconds unit.
            for entry in result.normalized_timeouts:
                print(
                    f"normalized_timeout: {entry.get('event')} {entry.get('matcher') or '*'} "
                    f"{entry.get('command')} {entry.get('old')} -> {entry.get('new')}"
                )
        if result.timeout_filled:
            # This block only prints; the fill itself already happened in `_merge_hooks`, which
            # both permits and records it: an existing registration for an identity this run
            # itself manages, found with no `timeout` key at all, had one added in place.
            for entry in result.timeout_filled:
                print(
                    f"timeout_filled: {entry.get('event')} {entry.get('matcher') or '*'} "
                    f"{entry.get('command')} -> {entry.get('timeout')}"
                )
        if result.external_tools:
            # Detected, never installed.
            for entry in result.external_tools:
                if not entry.get("found"):
                    status = "not found"
                else:
                    status = entry.get("version") or "found, version unknown"
                location = f" · {entry.get('path')}" if entry.get("path") else ""
                print(f"external_tool: {entry.get('name')} {status}{location}")
        if result.removed_components:
            # What a disabled component's re-run
            # cleaned up (or left, and why) from a previous install this run no longer wants.
            for entry in result.removed_components:
                print(
                    f"removed_component: {entry.get('component')} "
                    f"files_removed={list(entry.get('files_removed') or [])} "
                    f"entries_removed={list(entry.get('entries_removed') or [])} "
                    f"kept_modified={list(entry.get('kept_modified') or [])} "
                    f"kept_unknown_path={list(entry.get('kept_unknown_path') or [])} "
                    f"kept_unsafe_path={list(entry.get('kept_unsafe_path') or [])} "
                    f"kept_foreign={list(entry.get('kept_foreign') or [])} "
                    f"left_data={list(entry.get('left_data') or [])}"
                )
    return 0 if result.ok else 2
