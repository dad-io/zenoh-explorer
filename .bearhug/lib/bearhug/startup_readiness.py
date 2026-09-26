"""Bounded, read-only observations for the operational startup path.

This module describes what is present for one explicitly selected checkout.  It does not install
files, rebuild indexes, start providers, execute hooks, or choose a session.  Individual findings
use ``ready``, ``missing``, ``stale``, ``unverified`` or ``error`` and are plain JSON data.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import stat
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bearhug import paths
from bearhug.replay.cockpit_harness import runtime_block

SEALED_MANIFEST = paths.REPO_ROOT / "patches" / "promotion-package" / "manifest.json"
CLAUDE_HOME = paths.CLAUDE_HOME
EMBED_PROBE_TIMEOUT_SECONDS = 2.0
OBSERVATION_STALE_SECONDS = 24 * 60 * 60
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_FILES = 256
MAX_TASK_FILES = 1_000

_STATUSES = frozenset({"ready", "missing", "stale", "unverified", "error"})
_EXECUTABLES = frozenset(
    {
        "bash",
        "claude",
        "codebase-memory-mcp",
        "codex",
        "graft",
        "go",
        "memq",
        "node",
        "npx",
        "pi",
        "python",
        "python3",
        "sh",
        "uv",
    }
)
_SHELL_TOKENS = frozenset(
    {"[", "]", "&&", "||", ";", "!", "if", "then", "else", "fi", "exit", "command", "exec", "env"}
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def urlopen(request: Request, *, timeout: float):
    """Open one request without following redirects; retained as a test injection point."""
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _result(status: str, reason: str, **fields: object) -> dict[str, object]:
    if status not in _STATUSES:
        raise ValueError(f"unsupported readiness status: {status}")
    return {"status": status, "reason": reason, **fields}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number: {value}")


def _read_json(path: Path) -> tuple[str, dict[str, object] | None, str]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing", None, f"file is absent: {path}"
    except OSError:
        return "error", None, f"file could not be inspected: {path}"
    if not stat.S_ISREG(metadata.st_mode):
        return "error", None, f"expected a regular file: {path}"
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_JSON_BYTES + 1)
        if len(content) > MAX_JSON_BYTES:
            raise ValueError("file exceeds inspection bound")
        value = json.loads(content.decode("utf-8"), parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return "error", None, f"malformed JSON in {path}: {type(exc).__name__}"
    if not isinstance(value, dict):
        return "error", None, f"JSON root is not an object: {path}"
    return "ready", value, ""


def _read_toml(path: Path) -> tuple[str, dict[str, object] | None, str]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing", None, f"file is absent: {path}"
    except OSError:
        return "error", None, f"file could not be inspected: {path}"
    if not stat.S_ISREG(metadata.st_mode):
        return "error", None, f"expected a regular file: {path}"
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_JSON_BYTES + 1)
        if len(content) > MAX_JSON_BYTES:
            raise ValueError("file exceeds inspection bound")
        value = tomllib.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        return "error", None, f"malformed TOML in {path}: {type(exc).__name__}"
    if not isinstance(value, dict):
        return "error", None, f"TOML root is not a table: {path}"
    return "ready", value, ""


def _path_token(token: str, subject: Path) -> Path | None:
    value = (
        token.replace("${CLAUDE_PROJECT_DIR:-.}", str(subject))
        .replace("${CLAUDE_PROJECT_DIR}", str(subject))
        .replace("$CLAUDE_PROJECT_DIR", str(subject))
        .replace("${HOME}", str(Path.home()))
        .replace("$HOME", str(Path.home()))
    )
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    if "/" in token or candidate.suffix in {".py", ".sh", ".cjs"}:
        return subject / candidate
    return None


def _requirements(command: str, subject: Path) -> list[tuple[str, Path | None]]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    found: list[tuple[str, Path | None]] = []
    for token in tokens:
        if not token or token.startswith("-") or token in _SHELL_TOKENS or "=" in token:
            continue
        configured = _path_token(token, subject)
        if configured is not None and configured.suffix in {".py", ".sh", ".cjs"}:
            found.append((configured.name, configured))
        elif Path(token).name in _EXECUTABLES:
            found.append((Path(token).name, configured))
    return list(dict.fromkeys(found))


def _check_executables(requirements: list[tuple[str, Path | None]]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    missing: list[str] = []
    for name, configured in requirements:
        if configured is None:
            located = shutil.which(name)
            # ``uv`` commonly exposes Python through a symlink.  ``which`` checks the command's
            # mode, but test doubles and unusual PATH entries can still return a broken or
            # non-file path, so verify the resolved target is a usable executable while keeping
            # the symlink itself as the reported command path.
            present = (
                located is not None
                and Path(located).is_file()
                and os.access(located, os.X_OK)
            )
        else:
            # Hook source files remain physical files: copying or executing a linked script
            # would make the selected checkout's hook identity ambiguous.  Interpreter and
            # binary paths may be symlinks (including uv's Python shim), provided they resolve
            # to an executable regular file.
            is_script = configured.suffix in {".py", ".sh", ".cjs"}
            present = (
                configured.is_file()
                and (not is_script or not configured.is_symlink())
                and (is_script or os.access(configured, os.X_OK))
            )
            located = str(configured) if present else None
        rows.append({"name": name, "present": present, "path": located})
        if not present:
            missing.append(name)
    if not rows:
        return _result("unverified", "no recognised executable requirements", requirements=[])
    if missing:
        return _result(
            "stale",
            "configured executable(s) are unavailable",
            requirements=rows,
            missing=sorted(set(missing)),
        )
    return _result("ready", "recognised configured executables are present", requirements=rows)


def _hook_commands(document: dict[str, object]) -> tuple[list[tuple[str, str]], str | None]:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return [], "hook configuration has no hooks object"
    commands: list[tuple[str, str]] = []
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            return [], "hook configuration has malformed hook registrations"
        for group in groups:
            registered = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(registered, list):
                return [], "hook configuration has malformed hook registrations"
            for hook in registered:
                command = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(command, str) or not command.strip():
                    return [], "hook configuration has a hook without a command"
                commands.append((event, command))
    return commands, None


def _codex(subject: Path) -> tuple[dict[str, object], dict[str, object]]:
    path = subject / ".codex" / "hooks.json"
    state, document, reason = _read_json(path)
    if document is None:
        return (
            _result(state, reason, source=str(path)),
            _result("unverified", "no Codex hook configuration was available", requirements=[]),
        )
    commands, error = _hook_commands(document)
    if error:
        return (
            _result("error", error, source=str(path)),
            _result("error", "Codex hook requirements could not be inspected", requirements=[]),
        )
    executable = _check_executables(
        [requirement for _, command in commands for requirement in _requirements(command, subject)]
    )
    return (
        _result(
            str(executable["status"]),
            "Codex hook files inspected; use /hooks in Codex to review trust and activation",
            source=str(path),
            events=sorted({event for event, _ in commands}),
            registrations=len(commands),
            activation="unverified",
        ),
        executable,
    )


def _claude(subject: Path) -> tuple[dict[str, object], dict[str, object], dict[str, str]]:
    settings = subject / ".claude" / "settings.json"
    local = subject / ".claude" / "settings.local.json"
    paths_to_read = [settings] + ([local] if local.is_file() else [])
    documents: list[tuple[Path, dict[str, object]]] = []
    commands: list[tuple[str, str]] = []
    errors: list[str] = []
    for path in paths_to_read:
        state, document, reason = _read_json(path)
        if state == "missing":
            continue
        if state != "ready" or document is None:
            errors.append(reason)
            continue
        documents.append((path, document))
        # settings.local.json commonly contains only env/permissions.  It is an overlay, so its
        # lack of a hooks key does not erase project-level registrations.
        if path == local and "hooks" not in document:
            continue
        registered, error = _hook_commands(document)
        if error:
            errors.append(f"{path}: {error}")
        else:
            commands.extend(registered)
    if errors:
        return (
            _result("error", "; ".join(errors), sources=[str(path) for path, _ in documents]),
            _result("error", "hook requirements could not be inspected", requirements=[]),
            {},
        )
    if not settings.is_file():
        return (
            _result("missing", "Claude settings.json is absent", sources=[]),
            _result("unverified", "no Claude hook configuration was available", requirements=[]),
            {},
        )
    if not commands:
        return (
            _result("missing", "Claude settings declares no hook commands",
                    sources=[str(path) for path, _ in documents]),
            _result("unverified", "no Claude hook commands were available", requirements=[]),
            {},
        )
    requirements = [req for _, command in commands for req in _requirements(command, subject)]
    executable = _check_executables(requirements)
    state = str(executable["status"])
    env: dict[str, str] = {}
    for _, document in documents:
        values = document.get("env")
        if isinstance(values, dict):
            for key in ("MEMQ_EMBED_URL", "MEMQ_EMBED_MODEL"):
                if isinstance(values.get(key), str) and values[key]:
                    env[key] = values[key]
    return (
        _result(
            state,
            "hook registrations are present and requirements are available"
            if state == "ready" else "one or more configured hook requirements are unavailable",
            sources=[str(path) for path, _ in documents],
            events=sorted({event for event, _ in commands}), registrations=len(commands),
        ),
        executable,
        env,
    )


def _one_provider_suffices(
    subject: Path, claude: dict[str, object], codex: dict[str, object]
) -> tuple[dict[str, object], dict[str, object]]:
    """Bear Hug needs Claude Code or Codex, not both.

    A provider that setup did not select for this checkout, or that is absent while the other
    provider's hooks are configured, is reported as not required rather than as a missing gap.
    A provider that setup did select and that is absent stays ``missing``.
    """
    declared = _managed_mcp_providers(subject)
    rows = {"claude": dict(claude), "codex": dict(codex)}
    for name, other in (("claude", "codex"), ("codex", "claude")):
        row = rows[name]
        if row.get("status") != "missing":
            continue
        if declared:
            if name in declared:
                continue
            reason = (
                f"not selected at setup (providers: {', '.join(sorted(declared))}); "
                "Bear Hug needs one provider"
            )
        elif rows[other].get("status") == "ready":
            reason = f"not configured; {other} hooks are configured and Bear Hug needs one provider"
        else:
            continue
        rows[name] = {**row, "status": "unverified", "reason": reason, "required": False}
    return rows["claude"], rows["codex"]


def _read_setup_receipt(subject: Path) -> dict[str, object] | None:
    """This exact target's setup receipt, or None if absent, foreign, or malformed.

    The one place that decides a JSON document IS this checkout's setup receipt, so every
    reader below (which providers, which components) validates one document instead of each
    re-deriving the same test.
    """
    state, receipt, _ = _read_json(subject / ".bearhug" / "project-setup.json")
    if (
        state != "ready"
        or receipt is None
        or receipt.get("record_kind") != "project_setup_receipt"
        or receipt.get("target") != str(subject.resolve())
    ):
        return None
    return receipt


def _managed_mcp_providers(subject: Path) -> frozenset[str]:
    """Return setup-declared providers for this exact checkout, if any."""
    receipt = _read_setup_receipt(subject)
    if receipt is None:
        return frozenset()
    providers = receipt.get("providers")
    if not isinstance(providers, list):
        return frozenset()
    return frozenset(
        provider
        for provider in providers
        if isinstance(provider, str) and provider in {"claude", "codex"}
    )


def _receipt_components(subject: Path) -> tuple[frozenset[str], bool]:
    """Component labels this setup receipt's file list records, and whether it records any.

    Every file row a current `bearhug setup` writes carries a `component` label (`graft`,
    `codebase_memory`, ...); a disabled or never-requested component simply has no row, which
    is how a receipt can say a component is absent without a separate boolean. A receipt from
    before that label existed has no `files` list, or files with no `component` key at all, and
    cannot honestly say what was installed either way; callers get `components_known=False` and
    keep their own pre-fix assumption rather than reading that silence as "nothing installed".
    """
    receipt = _read_setup_receipt(subject)
    if receipt is None:
        return frozenset(), False
    files = receipt.get("files")
    if not isinstance(files, list) or not files:
        return frozenset(), False
    labelled = [
        row.get("component")
        for row in files
        if isinstance(row, dict) and isinstance(row.get("component"), str)
    ]
    if not labelled:
        return frozenset(), False
    return frozenset(labelled), True


#: The setup receipt's own component label for the one MCP server whose requirement is genuinely
#: conditional. `codebase-memory-mcp` is opt-in (`--codebase-memory`) and is required only when
#: its labelled file row exists. `graft` has no such label to look up: it is required
#: unconditionally (see `_required_mcp_servers`), because the label a receipt actually carries
#: for graft's own files differs BY PROVIDER and is never literally `"graft"` for Codex —
#: `project_setup.py`'s `_provider_specs` labels `.mcp.json` `"graft"` only inside
#: `if "claude" in providers:`, labels `.codex/config.toml` `"codex"`, and labels the shared
#: launcher plus vendored runtime `"graft-runtime"` for either provider. A component-label
#: lookup for `graft` the way `codebase-memory-mcp` uses one would silently never fire for Codex.
_CODEBASE_MEMORY_COMPONENT = "codebase_memory"


def _required_mcp_servers(subject: Path, is_managed: bool) -> tuple[tuple[str, ...], bool]:
    """Which MCP server names this provider's config must register, and whether that is certain.

    Returns `(required, components_known)`. With `components_known` True: `graft` has no
    opt-out, so it is required of every managed provider unconditionally, never through a
    per-label lookup (see `_CODEBASE_MEMORY_COMPONENT` above for why); `codebase-memory-mcp` is
    read from the receipt's own component label and is authoritative there. Do not additionally
    guess from `.mcp.json`/`config.toml`, which is what setup WROTE for another reason, not what
    it currently claims is required. With `components_known` False (an older receipt, or none),
    `required` falls back to today's pre-fix behaviour: assume the opt-in memory component alone,
    since that is the one this rule always guessed at before; `graft` is never newly required by
    guesswork, since today's behaviour never checked it by name either.
    """
    if not is_managed:
        return (), True
    components, components_known = _receipt_components(subject)
    if not components_known:
        return ("codebase-memory-mcp",), False
    required = ["graft"]
    if _CODEBASE_MEMORY_COMPONENT in components:
        required.append("codebase-memory-mcp")
    return tuple(required), True


def _mcp_requirements(command: str, subject: Path) -> list[tuple[str, Path | None]]:
    """Inspect MCP command fields, retaining absolute paths that contain spaces."""
    raw = command.strip()
    if raw.startswith("/"):
        # MCP's command field is the executable; arguments have their own field.  Treating an
        # absolute command as one explicit path also handles setup targets whose checkout path
        # contains spaces, without launching or resolving the configured process.
        return [(Path(raw).name, Path(raw))]
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    if not tokens:
        return []
    command_token = tokens[0]
    return [(Path(command_token).name, _path_token(command_token, subject))]


def _mcp_detail(
    provider: str,
    path: Path,
    status: str,
    reason: str,
    servers: list[str] | tuple[str, ...] = (),
    requirements: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return _result(
        status,
        reason,
        provider=provider,
        source=str(path),
        servers=sorted(servers),
        requirements=requirements or [],
    )


def _mcp_provider(
    subject: Path, provider: str, managed: frozenset[str]
) -> tuple[dict[str, object], dict[str, object]]:
    if provider == "claude":
        path = subject / ".mcp.json"
        state, document, reason = _read_json(path)
        server_key = "mcpServers"
    else:
        path = subject / ".codex" / "config.toml"
        state, document, reason = _read_toml(path)
        server_key = "mcp_servers"

    required, components_known = _required_mcp_servers(subject, provider in managed)
    # The rule (docs/OPERATING-MODES.md's readiness row): `graft` is always required for a
    # managed provider; `codebase-memory-mcp` only when the setup receipt's own component
    # labels record it as installed. An older receipt with no component labels at all cannot
    # confirm that either way, so this falls back to assuming the memory component alone
    # (today's pre-fix behaviour) and says so, rather than reporting a confirmed gap it cannot
    # actually see.
    fallback_note = (
        ""
        if components_known
        else (
            " (this setup receipt has no per-component record, so codebase-memory-mcp's "
            "requirement is assumed, not confirmed)"
        )
    )

    if state == "missing":
        missing_reason = (
            f"{provider} MCP configuration is absent; "
            f"does not register required {', '.join(required)}{fallback_note}"
            if required else reason
        )
        return _mcp_detail(provider, path, "missing", missing_reason), _result(
            "unverified", "no MCP server commands were available", requirements=[]
        )
    if state != "ready" or document is None:
        return _mcp_detail(provider, path, "error", reason), _result(
            "error", "MCP requirements could not be inspected", requirements=[]
        )

    servers = document.get(server_key, {})
    if not isinstance(servers, dict):
        return _mcp_detail(
            provider, path, "error",
            f"{provider} MCP configuration has malformed server declarations"
        ), _result("error", "MCP requirements could not be inspected", requirements=[])
    if not servers:
        missing_reason = (
            f"{provider} MCP configuration declares no servers; "
            f"does not register required {', '.join(required)}{fallback_note}"
            if required
            else f"{provider} MCP configuration declares no servers"
        )
        return _mcp_detail(provider, path, "missing", missing_reason), _result(
            "unverified", "no MCP server commands were available", requirements=[]
        )

    names: list[str] = []
    requirements: list[tuple[str, Path | None]] = []
    for name, server in servers.items():
        command = server.get("command") if isinstance(server, dict) else None
        if not isinstance(name, str) or not isinstance(command, str) or not command.strip():
            return _mcp_detail(
                provider,
                path,
                "error",
                f"{provider} MCP configuration has malformed server declarations",
                names,
            ), _result("error", "MCP requirements could not be inspected", requirements=[])
        names.append(name)
        requirements.extend(_mcp_requirements(command, subject))

    def _registered(name: str) -> bool:
        server = servers.get(name)
        if server is None:
            return False
        # codebase-memory-mcp is the one server bear-hug's own generated entry marks
        # `"enabled": false` rather than removing outright; an explicitly disabled entry is
        # not a registration. No other server name carries this convention.
        if name == "codebase-memory-mcp" and isinstance(server, dict):
            return server.get("enabled") is not False
        return True

    missing_required = [name for name in required if not _registered(name)]

    executable = _check_executables(requirements)
    if missing_required:
        detail = _mcp_detail(
            provider,
            path,
            "missing",
            f"{provider} MCP configuration does not register required "
            f"{', '.join(missing_required)}{fallback_note}",
            names,
            executable.get("requirements", []),
        )
    else:
        executable_state = str(executable["status"])
        detail = _mcp_detail(
            provider,
            path,
            executable_state,
            (
                f"{provider} MCP server registrations are present"
                if executable_state == "ready"
                else f"one or more {provider} MCP server commands are unavailable"
            ),
            names,
            executable.get("requirements", []),
        )
    return detail, executable


def _mcp(subject: Path) -> tuple[dict[str, object], dict[str, object]]:
    managed = _managed_mcp_providers(subject)
    provider_rows: dict[str, dict[str, object]] = {}
    executable_sources: list[dict[str, object]] = []
    for provider in ("claude", "codex"):
        detail, executable = _mcp_provider(subject, provider, managed)
        provider_rows[provider] = detail
        executable_sources.append(executable)

    requirements = [
        row
        for executable in executable_sources
        for row in executable.get("requirements", [])
        if isinstance(row, dict)
    ]
    servers = sorted(
        {
            server
            for detail in provider_rows.values()
            for server in detail.get("servers", [])
            if isinstance(server, str)
        }
    )
    active_statuses = [
        str(detail["status"])
        for provider, detail in provider_rows.items()
        if str(detail["status"]) != "missing" or provider in managed
    ]
    if any(status == "error" for status in active_statuses):
        state = "error"
    elif any(status == "missing" for status in active_statuses):
        state = "missing"
    elif any(status == "stale" for status in active_statuses):
        state = "stale"
    elif any(status == "ready" for status in active_statuses):
        state = "ready"
    else:
        state = "missing"

    source = str(subject / ".mcp.json")
    if (provider_rows["claude"]["status"] == "missing"
            and provider_rows["codex"]["status"] != "missing"):
        source = str(subject / ".codex" / "config.toml")
    if state == "ready":
        registration_summary = "; ".join(
            f"{provider}={','.join(detail['servers'])}"
            for provider, detail in provider_rows.items()
            if detail["status"] == "ready" and detail.get("servers")
        )
        reason = (
            f"MCP server registrations are present ({registration_summary}); "
            "live connection and graph freshness remain unverified"
        )
    elif state == "missing":
        reason = "one or more required MCP server registrations are missing"
    elif state == "stale":
        reason = "one or more MCP server commands are unavailable"
    else:
        reason = "one or more MCP configurations could not be inspected"
    connection = _result(
        "unverified", "MCP server processes were not started; live connection was not checked"
    )
    graph_freshness = _result(
        "unverified", "MCP graph was not queried; graph freshness was not checked"
    )
    return (
        _result(
            state,
            reason,
            source=source,
            servers=servers,
            requirements=requirements,
            providers=provider_rows,
            sources={provider: detail["source"] for provider, detail in provider_rows.items()},
            connection=connection,
            graph_freshness=graph_freshness,
        ),
        _result(
            "error" if any(item["status"] == "error" for item in executable_sources)
            else "stale" if any(item["status"] == "stale" for item in executable_sources)
            else "ready" if any(item["status"] == "ready" for item in executable_sources)
            else "unverified",
            "MCP executable requirements were inspected",
            requirements=requirements,
        ),
    )


def _runtime(subject: Path) -> dict[str, object]:
    manifest_state, _, manifest_reason = _read_json(SEALED_MANIFEST)
    if manifest_state != "ready":
        status = "missing" if manifest_state == "missing" else "error"
        return _result(status, manifest_reason, sealed_manifest=str(SEALED_MANIFEST))
    try:
        block = runtime_block(subject, SEALED_MANIFEST)
    except (OSError, TypeError, ValueError) as exc:
        return _result("error", f"runtime identity could not be read: {type(exc).__name__}")
    payload = dict(block)
    payload.pop("status", None)
    payload.pop("reason", None)
    if block.get("status") == "absent":
        return _result(
            "missing", str(block.get("reason") or "installed runtime is absent"), **payload
        )
    if block.get("drift") == "match":
        return _result("ready", "installed runtime matches the sealed manifest", **payload)
    if block.get("drift") == "drift":
        return _result("stale", "installed runtime differs from the sealed manifest", **payload)
    return _result(
        "unverified", str(block.get("reason") or "runtime identity is unavailable"), **payload
    )


def _git_anchor(subject: Path) -> dict[str, object]:
    """Read Git's main worktree identity with fixed, bounded commands."""
    try:
        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(subject), *args], check=True, capture_output=True,
                text=True, timeout=1,
            ).stdout.strip()

        root = git("rev-parse", "--show-toplevel")
        common = git("rev-parse", "--git-common-dir")
        worktrees = git("worktree", "list", "--porcelain").splitlines()
    except (OSError, subprocess.SubprocessError):
        return {"status": "unverified", "subject": str(subject), "main": None,
                "reason": "Git worktree identity could not be read"}
    main = next((line[9:] for line in worktrees if line.startswith("worktree ")), root)
    root_path, main_path = Path(root).resolve(), Path(main).resolve()
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (subject / common_path).resolve()
    else:
        common_path = common_path.resolve()
    return {
        "status": "ready", "subject": str(root_path), "main": str(main_path),
        "common_dir": str(common_path), "is_main": root_path == main_path,
        "reason": "main checkout identity observed",
    }


def _memq(subject: Path) -> dict[str, object]:
    anchor = _git_anchor(subject)
    configured = os.environ.get("MEMQ_REPO")
    receipt_state, receipt, _ = _read_json(subject / ".bearhug/project-setup.json")
    project_local = (
        receipt_state == "ready" and isinstance(receipt, dict)
        and receipt.get("record_kind") == "project_setup_receipt"
        and receipt.get("target") == str(subject.resolve())
    )
    expected = Path(configured).expanduser().resolve() if configured else (
        subject.resolve() if project_local else Path(
            str(anchor.get("main")) if anchor.get("status") == "ready" else str(subject.resolve())
        )
    )
    if anchor.get("status") != "ready":
        anchor_state, anchor_reason = "unverified", str(anchor.get("reason"))
    elif project_local and expected == subject.resolve():
        anchor_state, anchor_reason = "ready", "MemQ index belongs to this installed worktree"
    elif configured and expected != Path(str(anchor["main"])).resolve():
        anchor_state, anchor_reason = "stale", "MEMQ_REPO is not this repository's main checkout"
    else:
        anchor_state, anchor_reason = "ready", "MemQ index anchor is the selected main checkout"

    config_path = expected / ".memq.json"
    state, config, reason = _read_json(config_path)
    anchor_report = {**anchor, "status": anchor_state, "reason": anchor_reason,
                     "configured_repo": bool(configured)}
    if state == "missing":
        return _result("missing", reason, config=str(config_path), index=None, anchor=anchor_report)
    if state != "ready" or config is None:
        return _result("error", reason, config=str(config_path), index=None, anchor=anchor_report)
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        return _result(
            "error", "MemQ config has no sources array", config=str(config_path), index=None,
            anchor=anchor_report,
        )
    index = expected / ".memq"
    manifest = index / "manifest.json"
    if not manifest.is_file() or not (index / "db").is_dir():
        state, reason = "missing", "MemQ index directory or manifest is absent"
    else:
        manifest_state, data, manifest_reason = _read_json(manifest)
        if manifest_state != "ready" or data is None:
            state, reason = manifest_state, manifest_reason
        elif not isinstance(data.get("hashes"), dict) or not data["hashes"]:
            state, reason = "unverified", "MemQ index manifest has no bounded source identity"
        elif anchor_state != "ready":
            state, reason = anchor_state, anchor_reason
        else:
            try:
                state = (
                    "stale"
                    if config_path.stat().st_mtime_ns > manifest.stat().st_mtime_ns
                    else "ready"
                )
            except OSError:
                state = "unverified"
            reason = (
                "MemQ config and index are present"
                if state == "ready"
                else "MemQ config is newer than the index manifest"
            )
    return _result(
        state,
        reason,
        config=str(config_path),
        index=str(index),
        index_manifest=str(manifest),
        source_count=len(sources),
        anchor=anchor_report,
    )


def _embedding_observation(url: str | None, model: str | None) -> dict[str, object]:
    if not url:
        return _result("unverified", "no embedding endpoint is configured", model=model)
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return _result("error", "embedding URL is malformed", model=model)
    if parsed.scheme not in {"http", "https"} or host not in {"localhost", "127.0.0.1", "::1"}:
        return _result(
            "unverified", "embedding probe is limited to localhost", host=host or None, model=model
        )
    if parsed.username or parsed.password:
        return _result("error", "embedding URL contains credentials", host=host, model=model)
    endpoint = url.rstrip("/")
    if not endpoint.endswith("/embeddings"):
        endpoint += "/embeddings"
    payload: dict[str, object] = {"input": "bearhug startup readiness"}
    if model:
        payload["model"] = model
    try:
        response = urlopen(
            Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            ),
            timeout=EMBED_PROBE_TIMEOUT_SECONDS,
        )
        try:
            value = getattr(response, "status", None)
            code = int(value if value is not None else response.getcode())
            raw = response.read(MAX_JSON_BYTES + 1)
        finally:
            response.close()
    except HTTPError as exc:
        code, raw = exc.code, b""
    except (OSError, URLError, TimeoutError):
        return _result(
            "stale", "local embedding service did not answer within the bound",
            host=host, model=model,
        )
    except Exception as exc:
        return _result(
            "error", f"embedding probe failed: {type(exc).__name__}", host=host, model=model
        )
    if not 200 <= code < 300:
        state = "unverified" if code in {400, 401, 403, 404, 405, 415} else "stale"
        return _result(
            state,
            f"local embedding service returned HTTP {code} without a usable vector",
            host=host, http_status=code, model=model,
        )
    if len(raw) > MAX_JSON_BYTES:
        return _result(
            "unverified", "embedding response exceeded the inspection bound",
            host=host, model=model,
        )
    try:
        document = json.loads(raw.decode("utf-8"))
        data = document.get("data") if isinstance(document, dict) else None
        vector = (
            data[0].get("embedding")
            if isinstance(data, list) and data and isinstance(data[0], dict)
            else None
        )
    except (UnicodeDecodeError, ValueError, IndexError, TypeError, AttributeError):
        vector = None
    valid = isinstance(vector, list) and bool(vector) and all(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        for value in vector
    )
    if not valid:
        return _result(
            "unverified", "local embedding response has no finite vector",
            host=host, http_status=code, model=model,
        )
    return _result(
        "ready", "local embedding service returned a finite vector", host=host,
        http_status=code, dimensions=len(vector), model=model,
    )


def _graft(subject: Path) -> dict[str, object]:
    index = subject / "graft" / "INDEX.md"
    stats_path = subject / "graft" / ".cache" / "stats.json"
    if not index.is_file():
        return _result("missing", "Graft INDEX.md is absent", index=str(index))
    state, stats, reason = _read_json(stats_path)
    if state == "missing":
        return _result(
            "unverified", "Graft cache freshness metadata is absent",
            index=str(index), stats=str(stats_path),
        )
    if state != "ready" or stats is None:
        return _result(state, reason, index=str(index), stats=str(stats_path))
    if stats.get("dirty") is True or stats.get("syncing") is True:
        return _result(
            "stale", "Graft cache reports dirty or syncing",
            index=str(index), stats=str(stats_path),
        )
    if not isinstance(stats.get("syncedAt"), str) or not stats["syncedAt"]:
        return _result(
            "unverified", "Graft cache has no sync timestamp",
            index=str(index), stats=str(stats_path),
        )
    return _result(
        "ready", "Graft index and clean cache metadata are present",
        index=str(index), stats=str(stats_path),
    )


def _observed(directory: Path, pattern: str, label: str) -> dict[str, object]:
    if not directory.is_dir():
        return _result("missing", f"no {label} records were observed", path=str(directory), files=0)
    try:
        files = []
        for path in directory.glob(pattern):
            if not path.is_file() or path.is_symlink():
                continue
            if len(files) >= MAX_FILES:
                return _result(
                    "unverified", f"{label} count exceeded the inspection bound",
                    path=str(directory), files=MAX_FILES,
                )
            files.append(path)
    except OSError:
        return _result(
            "error", f"{label} directory could not be inspected", path=str(directory), files=0
        )
    if len(files) > MAX_FILES:
        return _result(
            "unverified", f"{label} count exceeded the inspection bound",
            path=str(directory), files=MAX_FILES,
        )
    if not files:
        return _result("missing", f"no {label} records were observed", path=str(directory), files=0)
    try:
        newest = max(path.stat().st_mtime for path in files)
    except OSError:
        return _result(
            "error", f"{label} timestamps could not be read",
            path=str(directory), files=len(files),
        )
    age = max(0.0, datetime.now(UTC).timestamp() - newest)
    state = "ready" if age <= OBSERVATION_STALE_SECONDS else "stale"
    return _result(
        state,
        f"{label} records observed" if state == "ready" else f"{label} records are old",
        path=str(directory), files=len(files), age_seconds=round(age, 3),
        stale_after_seconds=OBSERVATION_STALE_SECONDS,
    )


def _hooks(subject: Path, claude: dict[str, object], codex: dict[str, object]) -> dict[str, object]:
    stamps = _observed(subject / ".automation-stamps", "*", "stamp")
    telemetry = _observed(subject / ".bearhug" / "telemetry" / "v1", "*/events.jsonl", "telemetry")
    codex_events = _observed(
        subject / ".bearhug" / "codex-hooks" / "v1", "*/events.jsonl", "Codex lifecycle"
    )
    states = {str(record["status"]) for record in (stamps, telemetry, codex_events)}
    if "ready" in states:
        observed_state = "ready"
    elif "stale" in states:
        observed_state = "stale"
    elif states == {"missing"}:
        observed_state = "missing"
    else:
        observed_state = "unverified"
    return {
        "configured": _result(
            "ready" if "ready" in {claude["status"], codex["status"]} else "unverified",
            "provider hook configuration inspected; registration does not prove activation",
            providers={"claude": claude, "codex": codex},
        ),
        "observed": _result(
            observed_state,
            "recent stamp or telemetry activity observed"
            if observed_state == "ready"
            else "recent hook activity was not observed",
            stamps=stamps,
            telemetry=telemetry,
            codex_lifecycle=codex_events,
        ),
    }


def _session(session_id: str | None) -> dict[str, object]:
    if session_id is None:
        return _result("unverified", "no session id supplied; exact task presence was not checked",
                       session_id=None, task_store=None)
    if (
        not session_id
        or session_id in {".", ".."}
        or "/" in session_id
        or "\\" in session_id
        or any(ord(c) < 32 for c in session_id)
    ):
        return _result(
            "error", "session id is not a single safe directory name",
            session_id=session_id, task_store=None,
        )
    task_store = CLAUDE_HOME / "tasks" / session_id
    try:
        metadata = task_store.lstat()
    except FileNotFoundError:
        return _result(
            "missing", "exact Claude task store is absent", session_id=session_id,
            task_store=str(task_store), task_files=0,
        )
    except OSError:
        return _result(
            "error", "exact Claude task store could not be inspected", session_id=session_id,
            task_store=str(task_store), task_files=0,
        )
    if not stat.S_ISDIR(metadata.st_mode) or task_store.is_symlink():
        return _result(
            "error", "exact Claude task store is not a real directory", session_id=session_id,
            task_store=str(task_store), task_files=0,
        )
    try:
        files = []
        for path in task_store.glob("*.json"):
            if not path.is_file() or path.is_symlink():
                continue
            if len(files) >= MAX_TASK_FILES:
                return _result(
                    "unverified", "exact task store exceeded the inspection bound",
                    session_id=session_id, task_store=str(task_store), task_files=MAX_TASK_FILES,
                )
            files.append(path)
    except OSError:
        return _result(
            "error", "exact Claude task store could not be listed", session_id=session_id,
            task_store=str(task_store), task_files=0,
        )
    if len(files) > MAX_TASK_FILES:
        return _result(
            "unverified", "exact task store exceeded the inspection bound", session_id=session_id,
            task_store=str(task_store), task_files=MAX_TASK_FILES,
        )
    return _result(
        "ready", "exact Claude task store is present", session_id=session_id,
        task_store=str(task_store), task_files=len(files),
    )


def inspect_readiness(subject: Path, *, session_id: str | None = None) -> dict[str, object]:
    """Inspect one selected checkout and, when supplied, exactly one selected session."""
    selected = Path(subject).expanduser().resolve()
    observed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not selected.is_dir():
        error = _result("error", "selected subject is not a directory")
        return {
            "observed_at": observed_at,
            "subject": str(selected),
            "session_id": session_id,
            "runtime": error,
            "claude": error,
            "codex": error,
            "mcp": error,
            "executables": _result("unverified", error["reason"], requirements=[]),
            "memq": error,
            "embedding": _result("unverified", error["reason"]),
            "graft": error,
            "hooks": {"configured": error, "observed": error},
            "session": _session(session_id),
        }
    claude, hook_exec, env = _claude(selected)
    codex, codex_exec = _codex(selected)
    claude, codex = _one_provider_suffices(selected, claude, codex)
    mcp, mcp_exec = _mcp(selected)
    executable_sources = [hook_exec, codex_exec, mcp_exec]
    requirements = [
        row
        for source in executable_sources
        for row in source.get("requirements", [])
        if isinstance(row, dict)
    ]
    if any(source["status"] == "error" for source in executable_sources):
        executable = _result(
            "error", "one executable source could not be inspected", requirements=requirements
        )
    elif any(source["status"] == "stale" for source in executable_sources):
        executable = _result(
            "stale", "one or more configured executables are unavailable", requirements=requirements
        )
    elif any(source["status"] == "ready" for source in executable_sources):
        executable = _result(
            "ready", "configured executable requirements were observed", requirements=requirements
        )
    else:
        executable = _result(
            "unverified", "no executable requirements were observed", requirements=requirements
        )
    embed_url = env.get("MEMQ_EMBED_URL") or os.environ.get("MEMQ_EMBED_URL")
    embed_model = env.get("MEMQ_EMBED_MODEL") or os.environ.get("MEMQ_EMBED_MODEL")
    return {
        "observed_at": observed_at,
        "subject": str(selected),
        "session_id": session_id,
        "runtime": _runtime(selected),
        "claude": claude,
        "codex": codex,
        "mcp": mcp,
        "executables": executable,
        "memq": _memq(selected),
        "embedding": _embedding_observation(embed_url, embed_model),
        "graft": _graft(selected),
        "hooks": _hooks(selected, claude, codex),
        "session": _session(session_id),
    }


__all__ = ["inspect_readiness"]
