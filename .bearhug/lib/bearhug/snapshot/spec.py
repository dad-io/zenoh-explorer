"""What a snapshot captures, enumerated in one place.

The harness under study is two layers: the project layer that lives in barracuda's repo, and the
ambient layer that Claude Code assembles from ``~/.claude`` and its plugins. A snapshot that took
only the first would describe an injection surface smaller than the one the model actually sees.

Every rule is declared, never discovered, so the blast radius of a snapshot is readable here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

PROJECT = "project"
AMBIENT = "ambient"

#: Never copied: build artefacts and VCS internals are not part of the harness.
EXCLUDE_DIRS = frozenset({"__pycache__", ".git", "node_modules", ".pytest_cache"})
EXCLUDE_NAMES = frozenset({".DS_Store"})
EXCLUDE_SUFFIXES = (".pyc", ".pyo")


@dataclass(frozen=True, slots=True)
class Rule:
    """One capture instruction.

    ``kind`` is ``file`` (one path), ``glob`` (a pattern, possibly matching many), ``tree``
    (a directory, recursively), or ``census`` (count the files, copy none — the honest way to
    snapshot a directory whose *emptiness* is the observation).
    """

    layer: str
    kind: str
    pattern: str
    why: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class Target:
    """A concrete file resolved from a rule."""

    layer: str
    source: Path
    relpath: PurePosixPath


# --- 1.1 the project layer -----------------------------------------------------------------

PROJECT_RULES: tuple[Rule, ...] = (
    Rule(PROJECT, "file", "CLAUDE.md", "the golden master under study"),
    Rule(PROJECT, "file", ".claude/settings.json", "the hook wiring — every gate is declared here"),
    Rule(PROJECT, "file", ".claude/settings.local.json", "per-machine overrides of the above"),
    Rule(PROJECT, "glob", ".claude/agents/*.md", "dispatchable subagents"),
    Rule(PROJECT, "glob", ".claude/skills/*/SKILL.md", "project skills, injected on invocation"),
    Rule(PROJECT, "glob", ".claude/helpers/*.cjs", "helper commands the hooks shell out to"),
    Rule(PROJECT, "tree", "scripts/hooks", "the hook scripts themselves — Phase 3's subject"),
    Rule(
        PROJECT, "file", "scripts/bearhug_work.py",
        "the installed project-work adapter invoked by native lifecycle hooks", required=False,
    ),
    Rule(
        PROJECT, "file", "scripts/bearhug_native.py",
        "the native task reconciliation dependency imported by bearhug_work.py", required=False,
    ),
    Rule(
        PROJECT, "tree", ".bearhug/lib",
        "bundled project-local Bear Hug modules used by native lifecycle hooks", required=False,
    ),
    Rule(
        PROJECT, "file", ".bearhug/project-setup.json",
        "the setup receipt — which hook files and components Bear Hug itself installed here; "
        "the hooks audit's own-hook recognition reads it to tell Bear Hug's hooks apart from "
        "the project's", required=False,
    ),
    Rule(
        PROJECT, "file", "scripts/memex-lint.sh",
        "cited by CLAUDE.md:1072 and run by the pre-commit hook; memex is 5 of the 24 hooks",
        required=False,
    ),
    Rule(
        PROJECT, "file", "scripts/plan-board.sh",
        "the ONE extractor for board state — the vendored TUI and the joinkey gate both "
        "depend on it, and 0182 permits rendering it precisely because it is canonical",
        required=False,
    ),
    Rule(PROJECT, "tree", ".githooks", "the git-side gates, which CLAUDE.md also relies on"),
    Rule(PROJECT, "file", "docs/session-automation.md", "the harness's own documentation"),
    Rule(
        PROJECT, "file", ".mcp.json",
        "MCP servers are part of the harness: their tool schemas are injected into every "
        "session's context before the first user word (codebase-memory-mcp, graft)",
    ),
    Rule(
        PROJECT, "file", ".memq.json",
        "what memq indexes and recalls — memq is 3 of the 24 hook commands",
    ),
    Rule(
        PROJECT, "file", ".memq.example.json",
        "the checked-in template the live config drifts from", required=False,
    ),
    Rule(
        PROJECT, "glob", ".idea/runConfigurations/*.xml",
        "the declared live-observation surface: a remote dlv attach on :2345, two race "
        "configs, CPU/mem and mutex/block profiles, a NATS wiretap, and the plan board. "
        "This is the human half of the same observe-before-claiming rule dlv-verify-gate "
        "enforces on the model, and no phase has ever looked at it",
        required=False,
    ),
    Rule(
        PROJECT, "census", ".automation-stamps",
        "hook liveness stamps. Phase 3.6 cross-checks INERTNESS against their freshness, "
        "so their state at capture time is part of the evidence",
    ),
    Rule(
        PROJECT, "glob", "docs/superpowers/plans/2026-08-12-AUTOMATION-INVENTORY.md",
        "the prior hand inventory, to check bear-hug against",
    ),
    Rule(
        PROJECT, "glob", "docs/superpowers/plans/2026-08-13-PLAN-claude-md-*.md",
        "Sam's own context-engineering plan for CLAUDE.md, grounded in Anthropic's guidance "
        "and a Fable consult. Prior art for Phase 7.1 and the standing rule that CLAUDE.md is "
        "never edited without a measured prime-eval before/after",
        required=False,
    ),
    Rule(
        PROJECT, "glob", "docs/superpowers/plans/2026-08-27-SURVEY-*.md",
        "the prior hand survey of the automation surface", required=False,
    ),
)

# --- 1.2 the ambient layer -----------------------------------------------------------------

AMBIENT_RULES: tuple[Rule, ...] = (
    Rule(AMBIENT, "file", "settings.json", "user-level settings, incl. which plugins are enabled"),
    Rule(AMBIENT, "glob", "commands/*.md", "user slash commands"),
    Rule(AMBIENT, "glob", "skills/*/SKILL.md", "user skills available in every session"),
    Rule(
        AMBIENT, "glob", "plugins/cache/*/superpowers/*/hooks/hooks.json",
        "superpowers' own hook wiring, which stacks on barracuda's",
    ),
    Rule(
        AMBIENT, "glob", "plugins/cache/*/superpowers/*/skills/using-superpowers/SKILL.md",
        "the largest single ambient injection",
    ),
    Rule(
        AMBIENT, "glob", "plugins/cache/*/hookify/*/hooks/hooks.json",
        "hookify's wiring — enabled, and its rule dirs are empty (see the census below)",
        required=False,
    ),
    Rule(
        AMBIENT, "glob", "statusline-*.sh",
        "the status line runs on every turn and is part of the ambient surface",
        required=False,
    ),
    Rule(
        AMBIENT, "census", "plugins/data/hookify-*",
        "hookify is enabled with zero rules; the emptiness is the measurement",
        required=False,
    ),
)

ALL_RULES: tuple[Rule, ...] = PROJECT_RULES + AMBIENT_RULES


def in_capture_scope(relpath: str, layer: str = PROJECT) -> bool:
    """Could any rule in this spec have captured ``relpath``?

    A check that asks "is this file in the snapshot" cannot tell a genuinely missing file from
    one the spec never collects. GATE-COVERAGE reported `scripts/memex-lint.sh` as a dead
    reference for exactly that reason: the file exists, but only `scripts/hooks/` was captured.
    A detector whose negative is an artefact of its own scope is not a detector.
    """
    for rule in ALL_RULES:
        if rule.layer != layer:
            continue
        if rule.kind == "file" and rule.pattern == relpath:
            return True
        if rule.kind == "glob" and fnmatch(relpath, rule.pattern):
            return True
        if rule.kind == "tree" and relpath.startswith(rule.pattern.rstrip("/") + "/"):
            return True
    return False


def fingerprint() -> str:
    """A hash of what this spec captures, not of the file that declares it.

    Prose (`why`) is excluded deliberately: rewording a rationale must not invalidate a
    snapshot, but adding, removing or retargeting a rule must. A finding cites a snapshot;
    this is how a reader learns which spec produced it.
    """
    payload = json.dumps(
        [[r.layer, r.kind, r.pattern, r.required] for r in ALL_RULES], sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _excluded(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES or path.name.endswith(EXCLUDE_SUFFIXES):
        return True
    return any(part in EXCLUDE_DIRS for part in path.parts)


def expand(rule: Rule, root: Path) -> Iterator[Target]:
    """Resolve one rule against a live root, newest-first order removed for determinism."""
    if rule.kind == "file":
        candidate = root / rule.pattern
        if candidate.is_file() and not _excluded(candidate):
            yield Target(rule.layer, candidate, PurePosixPath(rule.pattern))
        return

    if rule.kind == "glob":
        for match in sorted(root.glob(rule.pattern)):
            if match.is_file() and not _excluded(match):
                yield Target(rule.layer, match, PurePosixPath(match.relative_to(root).as_posix()))
        return

    if rule.kind == "tree":
        base = root / rule.pattern
        if not base.is_dir():
            return
        for match in sorted(base.rglob("*")):
            if match.is_file() and not _excluded(match):
                yield Target(rule.layer, match, PurePosixPath(match.relative_to(root).as_posix()))
        return

    if rule.kind == "census":
        return  # censuses are counted, never copied — see capture.census()

    raise ValueError(f"unknown rule kind: {rule.kind!r}")


def census(rule: Rule, root: Path) -> list[dict[str, object]]:
    """Count what a census rule points at without copying any of it."""
    rows: list[dict[str, object]] = []
    for directory in sorted(root.glob(rule.pattern)):
        if not directory.is_dir():
            continue
        files = [p for p in directory.rglob("*") if p.is_file() and not _excluded(p)]
        rows.append(
            {
                "path": directory.relative_to(root).as_posix(),
                "files": len(files),
                "names": sorted(p.relative_to(directory).as_posix() for p in files)[:50],
            }
        )
    return rows
