"""Bounded advisory context for installed Codex hooks, after gate evaluation.

Project tools execute only in the OS sandbox. Their output is retrieval material,
never a gate verdict. Local Markdown recall is a read-only fallback when MemQ
needs a service unavailable to the network-isolated hook.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

MAX_CONTEXT_BYTES = 8 * 1024
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SCAN_ENTRIES = 4096
_EXCLUDED = {".git", ".bearhug", ".codex", ".claude", ".venv", "node_modules", "vendor"}


def _bounded(text: str, maximum: int = MAX_CONTEXT_BYTES) -> str:
    clean = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    raw = clean.encode("utf-8")
    if len(raw) > maximum:
        marker = b"\n[Bear Hug context truncated]"
        raw = raw[: maximum - len(marker)] + marker
    return raw.decode("utf-8", "ignore").strip()


def _safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("context source must stay inside the project")
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("context source is symlinked")
    return current


def _read(root: Path, relative: str) -> str:
    _safe_path(root, relative)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("context source is not a bounded regular file")
        data = source.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError("context source grew past the limit")
    return data.decode("utf-8")


def _matches(relative: str, pattern: str) -> bool:
    """Match project-relative source globs without allowing '*' across directories."""
    parts, wanted = relative.split("/"), pattern.split("/")

    @cache
    def match(index: int, rule: int) -> bool:
        if rule == len(wanted):
            return index == len(parts)
        if wanted[rule] == "**":
            return any(match(next_index, rule + 1) for next_index in range(index, len(parts) + 1))
        return (
            index < len(parts)
            and fnmatch.fnmatchcase(parts[index], wanted[rule])
            and match(index + 1, rule + 1)
        )

    return match(0, 0)


def _local_recall(root: Path, query: str) -> str:
    """Search configured local Markdown sources without importing project code."""
    # Same package, same stdlib-only constraint (see grounding.py's own docstring); this is the
    # one exclusion check this recall path has of its own, independent of whatever setup did or
    # did not scope out of `.memq.json` -- see "Project-declared exclusions" in grounding.py.
    from bearhug.campaign.grounding import path_excluded, read_project_excludes

    exclude_paths = read_project_excludes(root)
    try:
        config = json.loads(_read(root, ".memq.json"))
        sources = config.get("sources", [])
    except (OSError, ValueError, AttributeError):
        sources = [{"type": "markdown", "glob": "docs/memex/**/*.md", "label": "memex"}]
    if not isinstance(sources, list):
        return "Memory recall unavailable: source configuration is malformed."
    patterns = []
    for item in sources[:32]:
        if not isinstance(item, dict) or item.get("type") != "markdown":
            continue
        pattern = item.get("glob")
        if (
            isinstance(pattern, str)
            and pattern
            and len(pattern) <= 512
            and len(Path(pattern).parts) <= 32
            and not Path(pattern).is_absolute()
            and ".." not in Path(pattern).parts
        ):
            patterns.append(pattern)
    terms = set(re.findall(r"[a-z][a-z0-9_-]{3,}", query.lower()))
    terms -= {"this", "that", "with", "from", "have", "what", "would", "should", "please"}
    matches: list[tuple[int, str, str]] = []
    seen = total = visited = 0
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if name not in _EXCLUDED
            and not (Path(directory) / name).is_symlink()
            and not path_excluded(
                (Path(directory) / name).relative_to(root).as_posix(), exclude_paths
            )
        )
        visited += len(names) + len(files)
        if visited > MAX_SCAN_ENTRIES:
            break
        for name in sorted(files):
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if not any(_matches(relative, pattern) for pattern in patterns):
                continue
            if path_excluded(relative, exclude_paths):
                continue
            try:
                text = _read(root, relative)
            except (OSError, ValueError, UnicodeError):
                continue
            total += len(text.encode())
            if total > MAX_SOURCE_BYTES:
                break
            seen += 1
            lines = text.splitlines()
            scored = [
                (len(terms & set(re.findall(r"[a-z][a-z0-9_-]{3,}", line.lower()))), i)
                for i, line in enumerate(lines)
            ]
            score, index = max(scored, default=(0, 0))
            if score:
                snippet = "\n".join(lines[max(0, index - 1) : index + 3])
                matches.append((score, f"{relative}:{index + 1}", _bounded(snippet, 900)))
        if total > MAX_SOURCE_BYTES:
            break
    scope = f"Local Markdown recall: {seen} configured documents read"
    if visited > MAX_SCAN_ENTRIES or total > MAX_SOURCE_BYTES:
        scope += "; scan limit reached, coverage is partial"
    scope += ". External/PDF sources and semantic index freshness are unverified."
    if not terms:
        return scope
    selected = sorted(matches, key=lambda item: (-item[0], item[1]))[:4]
    if not selected:
        return scope + "\nNo local lexical match; this does not prove no governing decision exists."
    return (
        scope
        + "\nRetrieved project text (advisory; verify the cited source):\n"
        + "\n\n".join(f"{path}\n{snippet}" for _score, path, snippet in selected)
    )


def _tool(
    root: Path, name: str, arguments: list[str], *, raw: bytes | None = None
) -> tuple[int | None, str]:
    from bearhug.project_hook_runtime import ProjectHookRuntimeError, _sandboxed_run

    try:
        executable = _safe_path(root, f"scripts/bin/{name}")
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return None, ""
        result = _sandboxed_run(
            root,
            [str(executable), *arguments],
            timeout=3.0,
            input_data=raw,
            read_only=True,
        )
        return result.returncode, _bounded(result.stdout.decode("utf-8", "replace"), 4096)
    except (OSError, ValueError, ProjectHookRuntimeError):
        return None, ""


def _memory(root: Path, event: str, payload: Mapping[str, Any]) -> str:
    if event == "SessionStart":
        code, output = _tool(root, "memexlint", ["-root", str(root), "-catalog"])
        return output if code == 0 and output else _local_recall(root, "")
    if event != "UserPromptSubmit":
        return ""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return ""
    # MemQ receives only its query, never the provider transcript or credentials.
    raw = json.dumps({"prompt": prompt[:16384], "cwd": str(root)}).encode()
    code, output = _tool(root, "memq", ["hook"], raw=raw)
    if code == 0:
        try:
            value = json.loads(output)["hookSpecificOutput"]["additionalContext"]
            if isinstance(value, str) and value.strip():
                return "MemQ retrieved project text (advisory):\n" + value
        except (KeyError, TypeError, ValueError):
            pass
    code, output = _tool(root, "memexlint", ["-root", str(root), "-topic", prompt[:4096]])
    recall = _local_recall(root, prompt[:16384])
    return "\n".join(filter(None, (output if code == 0 else "", recall)))


def _citations(root: Path, payload: Mapping[str, Any]) -> str:
    from bearhug.project_hook_runtime import _tool_effects

    if payload.get("hook_event_name") != "PostToolUse":
        return ""
    _family, _operation, effects = _tool_effects(root, payload, "PostToolUse")
    paths = sorted({item["path"] for item in effects if item.get("status") == "succeeded"})
    if not paths:
        return ""
    governed = []
    for path in paths[:4]:
        code, output = _tool(root, "memexlint", ["-root", str(root), "-reverse-index", path])
        if code == 0 and output:
            governed.append(output)
    if not governed and not any(path.startswith("docs/memex/") for path in paths):
        return ""
    code, output = _tool(root, "memexlint", ["-root", str(root)])
    state = (
        "unavailable" if code is None else ("reported success" if code == 0 else "needs attention")
    )
    return _bounded(
        "\n".join([*governed, f"Changed decision citation check: {state} (advisory).", output]),
        4096,
    )


def _architecture(root: Path) -> str:
    from bearhug.project_hook_runtime import ProjectHookRuntimeError, _sandboxed_run

    try:
        directory = _safe_path(root, ".bearhug/architecture")
        directory.mkdir(parents=True, exist_ok=True)
        result = _sandboxed_run(
            root,
            [
                str(Path(sys.executable).resolve()),
                "-I",
                str(Path(__file__).resolve()),
                "--architecture-refresh",
                str(root),
            ],
            timeout=8.0,
            read_only=True,
            writable_paths=(directory,),
        )
        if result.returncode:
            return "Architecture refresh unavailable; source and rule freshness are unverified."
        report = json.loads(result.stdout)
        rules = report.get("rules", {})
        canonical = report.get("canonical", {})
        return _bounded(
            f"Architecture observation: {report.get('status', 'unknown')}; "
            f"{report.get('reason', '')}\n"
            f"Approved project rules: {json.dumps(rules, ensure_ascii=False)}\n"
            f"Canonical project index: {json.dumps(canonical, ensure_ascii=False)}\n"
            f"Coverage limits: {report.get('limits', '')}",
            1800,
        )
    except (OSError, ValueError, ProjectHookRuntimeError):
        return "Architecture refresh unavailable; source and rule freshness are unverified."


def additional_context(root: Path, payload: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """Never perform advisory work before or instead of an enforcement decision."""
    if result.get("decision") not in {"allow", "continue"}:
        return ""
    event = payload.get("hook_event_name")
    if event not in {
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
        "Interrupt",
        "SessionEnd",
    }:
        return ""
    parts = []
    from bearhug import project_native, project_work

    try:
        project_native.event(root, dict(payload), "codex", include_campaign=False)
        if event in {"SessionStart", "UserPromptSubmit"} or (
            event == "PostToolUse"
            and payload.get("tool_name", "").split(".")[-1]
            in {"update_plan", "TaskCreate", "TaskUpdate"}
        ):
            state = project_work._execution_state(root, campaign={})
            if state:
                parts.append(_bounded(project_work.task_summary(state), 1000))
                parts.append(
                    _bounded(
                        project_native.context(
                            root, "codex", payload["session_id"], include_campaign=False
                        ),
                        1800,
                    )
                )
    except (OSError, ValueError, KeyError, TypeError):
        parts.append(
            "Native task reconciliation unavailable; do not claim synchronized completion."
        )
    if event in {"SessionStart", "Stop"}:
        parts.append(_architecture(root))
    parts.extend((_bounded(_memory(root, event, payload), 3600), _citations(root, payload)))
    return _bounded("\n\n".join(filter(None, parts)))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--architecture-refresh":
        raise SystemExit(2)
    # -I ignores ambient Python config; this file belongs to the installed bundle.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bearhug.project_architecture import refresh

    print(json.dumps(refresh(Path(sys.argv[2]).resolve(strict=True))))
