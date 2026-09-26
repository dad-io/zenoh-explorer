"""R04 — inventory write-detection semantics before extracting a shared resolver.

R05 must not begin from an assumption. This module reports what the snapshot actually contains,
and it keeps two defects apart that are easy to conflate and have completely different fixes:

**Parser duplication** — two scripts each implement "which paths did this tool call write", and
they disagree. Fixed by extracting one module.

**Matcher blindness** — a script is registered on a matcher Claude Code will never fire for a Bash
tool call, so the script never even *runs* for a Bash-mediated write. Fixed only by editing
`settings.json`.

A shared parser cannot fix blindness. The snapshot proves it rather than merely illustrating it:
`written-path.py` exists to resolve Bash-written paths for three PostToolUse hooks, the resolver
landed, the registration did not, and its Bash branch is therefore unreachable in production.

Blindness is **computed** from the snapshot's `settings.json`, never transcribed, so it cannot go
stale the moment a matcher changes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bearhug.hooks.audit import load_settings
from bearhug.lint.gates import hook_arguments, parse_hooks
from bearhug.paths import REPORTS_DIR

#: Events whose payload can carry `tool_input.command`, i.e. where a Bash write is observable at
#: all. Stop is absent by design: a Stop gate does not receive a tool call, it reads the transcript
#: — which is how the two Stop gates escaped this blindness entirely.
BASH_WRITE_CAPABLE_EVENTS = ("PreToolUse", "PostToolUse")

#: A matcher is a regex over the tool name. `None` or empty means "every tool".
_BASH_TOOL = "Bash"


def matcher_sees_bash(matcher: str | None) -> bool:
    """Would Claude Code fire a hook with this matcher for a Bash tool call?

    This is the whole distinction. The matcher decides whether the hook PROCESS runs; a write
    resolver only runs after the process has started, so no amount of shared parsing can rescue a
    registration whose matcher excludes Bash.
    """
    if not matcher or matcher == "*":
        return True
    try:
        return re.search(matcher, _BASH_TOOL, re.IGNORECASE) is not None
    except re.error:
        # An unparseable matcher is reported as seeing nothing rather than guessed at.
        return False


#: Registrations that detect writes, keyed by (script, configured arguments).
#:
#: Keyed by the whole registration and not by the script, because `memex-hook.sh post-edit`
#: detects writes while `memex-hook.sh pre-question` and `pre-decide` do not. Keying by script
#: alone reported three memex registrations as blind write detectors when only one is a write
#: detector at all.
_WRITE_DETECTORS = frozenset({
    ("scripts/hooks/go-postedit.sh", ()),
    ("scripts/hooks/debug-first.sh", ()),
    ("scripts/hooks/memex-hook.sh", ("post-edit",)),
    ("scripts/hooks/deep-check.py", ()),
    ("scripts/hooks/hard-safety.py", ()),
    ("scripts/hooks/dlv-verify-gate.py", ()),
    ("scripts/hooks/review-gate.py", ()),
    ("scripts/hooks/joinkey-lint.py", ("--check",)),
    ("scripts/hooks/task-durability.py", ()),
    ("scripts/hooks/written-path.py", ()),
})


def _codewrites_consumers(project: Path) -> dict[str, list[str]]:
    """Who imports codewrites, and exactly what. Read from source, not assumed."""
    consumers: dict[str, list[str]] = {}
    hooks = project / "scripts" / "hooks"
    if not hooks.is_dir():
        return consumers
    for path in sorted(hooks.glob("*.py")):
        if path.name in ("codewrites.py",) or path.name.endswith("_test.py"):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        rel = f"scripts/hooks/{path.name}"
        names: list[str] = []
        for match in re.finditer(r"from codewrites import \(([^)]*)\)", source):
            names += [
                n.strip().split("#")[0].strip().rstrip(",")
                for n in match.group(1).split("\n")
                if n.strip() and not n.strip().startswith("#")
            ]
        for match in re.finditer(r"from codewrites import ([^\n(]+)", source):
            # Strip a trailing comment: `from codewrites import x  # noqa: E402` was reporting
            # the noqa pragma as an imported name.
            imported = match.group(1).split("#")[0]
            names += [n.strip() for n in imported.split(",") if n.strip()]
        if re.search(r"^\s*import codewrites\b", source, re.MULTILINE):
            used = sorted(set(re.findall(r"codewrites\.(\w+)", source)))
            names += used or ["<module import>"]
        names = [n for n in dict.fromkeys(names) if n]
        if names:
            consumers[rel] = names
    return consumers


def build_inventory(snapshot_dir: Path, *, snapshot_id: str) -> dict[str, Any]:
    """The R04 report: registrations, blindness, sharing, distinctions, defects, unknowns."""
    snapshot_dir = Path(snapshot_dir)
    project = snapshot_dir / "project"
    settings = load_settings(snapshot_dir)

    registrations: list[dict[str, Any]] = []
    for spec in parse_hooks(settings):
        script = spec.script
        args = tuple(hook_arguments(spec.command))
        registrations.append({
            "event": spec.event,
            "matcher": spec.matcher,
            "script": script,
            "arguments": list(args),
            "command": spec.command,
            "sees_bash": matcher_sees_bash(spec.matcher),
            "bash_observable_at_this_event": spec.event in BASH_WRITE_CAPABLE_EVENTS,
            "detects_writes": (script, args) in _WRITE_DETECTORS if script else False,
        })

    # A SCRIPT is blind only if none of its registrations at a Bash-observable event sees Bash.
    # hard-safety.py is registered twice on PreToolUse — once on Bash and once on
    # Edit|Write|MultiEdit — so reporting the second registration alone would say it cannot see a
    # Bash write when it plainly can. Per-registration blindness is still recorded; this is the
    # per-script roll-up, and they answer different questions.
    per_script: dict[str, dict[str, Any]] = {}
    for row in registrations:
        if not row["script"] or not row["detects_writes"]:
            continue
        entry = per_script.setdefault(
            row["script"],
            {
                "script": row["script"],
                "sees_bash_somewhere": False,
                "has_bash_observable_registration": False,
                "registrations": [],
            },
        )
        entry["registrations"].append(
            {"event": row["event"], "matcher": row["matcher"], "sees_bash": row["sees_bash"]}
        )
        if row["bash_observable_at_this_event"]:
            entry["has_bash_observable_registration"] = True
            if row["sees_bash"]:
                entry["sees_bash_somewhere"] = True

    # Three categories, not two. A Stop gate has NO Bash-observable registration at all — it never
    # receives a tool call and reads the transcript instead — so matcher blindness is not a
    # property it can have. Calling it blind would be the same mis-classification as the first
    # GATE-COVERAGE pass: a real distinction collapsed into a scary-sounding count.
    for entry in per_script.values():
        if not entry["has_bash_observable_registration"]:
            entry["category"] = "reads_transcript"
        elif entry["sees_bash_somewhere"]:
            entry["category"] = "sees_bash"
        else:
            entry["category"] = "matcher_blind"

    return {
        "schema_version": 1,
        "task": "R04",
        "snapshot_id": snapshot_id,
        "derived_from": f"{snapshot_dir}/project/.claude/settings.json",
        "registrations": registrations,
        "write_detectors_by_script": sorted(per_script.values(), key=lambda r: r["script"]),
        "blind_write_detectors": sorted(
            row["script"] for row in per_script.values()
            if row["category"] == "matcher_blind"
        ),
        "bash_visible_write_detectors": sorted(
            row["script"] for row in per_script.values() if row["category"] == "sees_bash"
        ),
        "transcript_reading_write_detectors": sorted(
            row["script"] for row in per_script.values()
            if row["category"] == "reads_transcript"
        ),
        "why_transcript_readers_are_not_blind": (
            "A Stop hook receives no tool call, so it has no matcher and cannot be matcher-blind. "
            "dlv-verify-gate.py and review-gate.py escaped the Bash blindness precisely by "
            "abandoning the matcher surface and reading the transcript — which is why sharing the "
            "parser DID help them and did not help the PostToolUse hooks. Same parser, opposite "
            "outcome, decided by the registration."
        ),

        "defect_classes": {
            "parser_duplication": {
                "definition": "Two or more scripts each implement 'which paths did this tool call "
                              "write', and their answers differ.",
                "fixed_by": "Extracting one resolver module and migrating the consumers.",
            },
            "matcher_blindness": {
                "definition": "A script is registered on a matcher Claude Code will never fire "
                              "for a Bash tool call, so the script never RUNS for a "
                              "Bash-mediated write.",
                "fixed_by": "Editing .claude/settings.json — adding Bash to the matcher, or a "
                            "separate Bash-matched entry. No Python change can do it.",
            },
        },
        "shared_parser_does_not_fix_blindness": (
            "A shared write resolver CANNOT cause a hook registered on "
            "\"matcher\": \"Edit|Write|MultiEdit\" to receive a Bash PostToolUse event. The "
            "matcher decides whether the hook process is executed at all, and the resolver only "
            "runs after execution. The four Edit|Write|MultiEdit PostToolUse hooks stay dead for "
            "a Bash-mediated write no matter how good the shared parser is."
        ),

        "shipped_fix_that_bought_nothing": {
            "module": "scripts/hooks/written-path.py",
            "purpose": "Give the PostToolUse hooks Bash-write awareness. Its own header records "
                       "the measurement: all three callers are matched on Edit|Write|MultiEdit "
                       "and were DEAD for a session where every change went through Bash, 42h "
                       "and 45h stale on the automation dashboard.",
            "invoked_by": [
                "scripts/hooks/debug-first.sh",
                "scripts/hooks/memex-hook.sh",
            ],
            "bash_branch_reachable_in_production": False,
            "evidence": (
                "written-path.py's Bash branch requires tool_input.command, which only a Bash "
                "tool call carries. Both callers are registered PostToolUse on "
                "Edit|Write|MultiEdit, so `cmd` is never a string and the branch — including its "
                "`from codewrites import written_source_paths` — never executes. The only "
                "Bash-matched PostToolUse entry in the snapshot is graft-hooks.cjs tool-savings, "
                "which is not a write detector. The shared resolver landed; the registration did "
                "not; the fix bought those hooks nothing."
            ),
            "why_the_stop_gates_escaped": (
                "dlv-verify-gate.py and review-gate.py abandoned the matcher surface entirely and "
                "read the transcript, so sharing the parser DID help them. That is the cleanest "
                "statement of the distinction: same parser, opposite outcome, decided by the "
                "registration."
            ),
        },

        "codewrites_consumers": _codewrites_consumers(project),

        "reimplementers": {
            "scripts/hooks/hardsafety.py": {
                "mechanism": "Its own shlex tokenizer with verb-position semantics, splitting on "
                             "shell operators. Uses codewrites only for strip_heredocs.",
                "differs_how": "Quote content is PRESERVED and unquoted so `git 'push'` fires, "
                               "where codewrites.shell_skeleton removes it and would leave "
                               "`git ''`. Adds dd/truncate/chmod/chown, env-var prefixes, and "
                               "operator segmentation; omits cp/mv destinations, git "
                               "checkout/restore, and the inline-interpreter branch. Filters by "
                               "LOCATION rather than extension, and fails CLOSED where "
                               "codewrites fails open.",
                "deliberate": True,
                "ruling": "codewrites.py documents the split: a safety gate needs the quoting, so "
                          "the divergence is justified rather than drift. A shared resolver must "
                          "expose both modes.",
            },
            "scripts/hooks/task-durability.py": {
                "mechanism": "Its own any-file detector over parsed assistant tool_use blocks, "
                             "plus a BOARD_WRITE regex for named-file detection.",
                "differs_how": "Asks a different question — 'did anything change' — with no "
                               "extension filter. Sees Bash writes only for `git commit` and for "
                               "BOARD.md; every other Bash write of any file is invisible. Its "
                               "BOARD_WRITE verb set is a strict subset of codewrites': "
                               "redirect, tee and `sed -i` only, with no `perl -i`, no cp/mv, no "
                               "git checkout/restore, no interpreter.",
                "deliberate": False,
            },
            "scripts/hooks/deepcheck.py": {
                "mechanism": "Path-only `.go` test plus Go module and package-segment resolution.",
                "differs_how": "Reads only tool_input.file_path / tool_response.filePath, with "
                               "ZERO Bash awareness and no MultiEdit walk. Adds something no "
                               "other detector has: segment-exact module identity, after an "
                               "ancestor directory named `barracuda` once won the scan.",
                "deliberate": False,
            },
            "scripts/hooks/go-postedit.sh": {
                "mechanism": "jq over tool_input.file_path / tool_response.filePath, then a `.go` "
                             "suffix case.",
                "differs_how": "Bash-written paths are DELIBERATELY not resolved. Added and then "
                               "reverted 2026-08-27 on Sam's ruling, one commit later: it is the "
                               "only fleet hook that both MUTATES a file (gofmt -w) and BLOCKS "
                               "the turn, so a wrong path is damage, not a missed opportunity.",
                "deliberate": True,
                "ruling": "Reverted 2026-08-27 on Sam's ruling. R05 must not re-enable this "
                          "without first reproducing the wrong-file mutation as a constructed "
                          "negative.",
            },
        },

        "distinctions": {
            "any_file": {
                "needed": True,
                "consumers": ["scripts/hooks/task-durability.py", "scripts/hooks/hardsafety.py"],
                "why": "Cannot be expressed as a filter over the source-file question: a BOARD.md "
                       "row and a .pdf under _resources/ are both outside SOURCE_EXTS, which "
                       "deliberately excludes markdown and JSON. hardsafety has no extension "
                       "concept at all — it filters by location.",
            },
            "source_file": {
                "needed": True,
                "consumers": ["scripts/hooks/review-gate.py"],
                "why": "Sole consumer of SOURCE_EXTS and bash_writes_source in the snapshot.",
            },
            "go_file": {
                "needed": True,
                "consumers": [
                    "scripts/hooks/dlv-verify-gate.py", "scripts/hooks/go-postedit.sh",
                    "scripts/hooks/debug-first.sh", "scripts/hooks/deepcheck.py",
                ],
                "is_a_filter_over_source": True,
                "why": "codewrites derives bash_writes_source from written_source_paths so the "
                       "two cannot disagree, and the Go regex is the same generator with a "
                       "one-extension tuple.",
                "exception": (
                    "The inline-interpreter branch (`python -c`, `python <<EOF`) yields a BOOLEAN "
                    "and no path, because the path sits inside quoted content the skeleton "
                    "removed. Two separately-compiled literal regexes exist only for that. So the "
                    "resolver owes both a path-list API and a boolean API, and the boolean cannot "
                    "be a pure derivation of the list."
                ),
                "extra_requirement": "deepcheck needs Go MODULE identity, not just the extension: "
                                     "a .go file outside a known module must be declined, or "
                                     "`cat > /tmp/scratch.go` wedges the turn on a go.mod error.",
            },
        },

        "resolver_requirements": {
            "apis": [
                "a path-list API returning WHICH paths were written, first-seen order, deduped",
                "a boolean API for the inline-interpreter branch, where no path is recoverable",
                "an any-file question with no extension filter",
                "a source-file question filtered by SOURCE_EXTS",
                "a go-file question, as a filter plus module identity",
            ],
            "quoting_modes": [
                "skeleton mode: quoted content stripped, for the transcript-reading gates",
                "shlex mode: quoted content preserved and unquoted, so a safety gate sees "
                "`git 'push'` — mutually exclusive with skeleton mode",
            ],
            "post_resolution_guards": [
                "resolve a relative path against the COMMAND's cwd, parsed from a leading "
                "`cd X &&` — the hook payload does not carry it",
                "the resolved path must be inside the repo",
                "…and inside a Go module, with _resources/ excluded explicitly",
                "refuse symlinks and any path outside this checkout, including sibling worktrees",
            ],
            "failure_direction": "Caller-selected, not a constant: advisory and read-only "
                                 "consumers fail OPEN, while the safety gate fails CLOSED on "
                                 "unparseable quoting.",
        },

        "defects": [
            {
                "id": "TD-BOARD-SUBSTRING",
                "severity": "HIGH",
                "file": "scripts/hooks/task-durability.py",
                "summary": "board_touched scans the raw serialized transcript line, the exact "
                           "practice the same file's docstring says was abandoned.",
                "detail": "The docstring states 'Detection follows the EVENT, never a substring "
                          "(decision 0204)' and records a measured 2026-08-20 false block, where "
                          "reading the gate's own source flipped a read-only session to changed. "
                          "session_changed_files was rewritten to walk parsed records; "
                          "board_touched, in the same file, still does `if \"BOARD.md\" not in "
                          "line`, `any(t in line for t in EDIT_TOOLS)`, and `'\"name\":\"Bash\"' "
                          "in line`. The fix landed on one function and not the other.",
                "failure_scenario": "A transcript line carrying a tool_result whose CONTENT quotes "
                                    "a BOARD.md row alongside the bytes `\"name\":\"Bash\"` makes "
                                    "board_touched return True for a session that never touched "
                                    "the board.",
            },
            {
                "id": "TD-GIT-COMMIT-SUBSTRING",
                "severity": "MEDIUM",
                "file": "scripts/hooks/task-durability.py",
                "summary": "`\"git commit\" in cmd` is a raw substring over an un-heredoc-stripped "
                           "command.",
                "detail": "codewrites.strip_heredocs exists precisely because this repo writes "
                          "long `git commit -F -` heredocs that cite paths and quote code, but "
                          "task-durability imports nothing from codewrites. Narrower than "
                          "TD-BOARD-SUBSTRING because it operates on the parsed command rather "
                          "than the raw line.",
                "failure_scenario": "A `git commit -F -` heredoc whose BODY quotes the phrase "
                                    "`git commit` sets session_changed_files True on a session "
                                    "that only wrote a commit message.",
            },
            {
                "id": "EDIT-HALF-NOT-SHARED",
                "severity": "MEDIUM",
                "file": "scripts/hooks/dlv-verify-gate.py",
                "summary": "Only the BASH half of write detection was extracted; both "
                           "codewrites consumers reimplement the edit-tool half, and disagree.",
                "detail": "review-gate.py includes NotebookEdit in its edit-tool tuple and "
                          "matches extensions by rsplit against SOURCE_EXTS; dlv-verify-gate.py "
                          "omits NotebookEdit and matches by endswith('.go'). Both are "
                          "case-sensitive, so a `.GO` path misses in either. Both also enumerate "
                          "`edits[*].file_path`, a key the real MultiEdit payload does not carry.",
                "failure_scenario": "A NotebookEdit writing a .go cell is seen by review-gate and "
                                    "not by dlv-verify-gate, so one gate blocks and the other "
                                    "passes on identical input.",
            },
        ],

        "unknowns": [
            "settings.local.json (3847 bytes in the snapshot) was NOT read. If it registers or "
            "overrides hooks, the matcher table above is incomplete.",
            "The ambient ~/.claude layer was not read; it may register further write detectors.",
            "The real MultiEdit payload shape was inferred from defensive code, not from a "
            "fixture. Whether `edits[*].file_path` ever exists is unconfirmed.",
            "Whether a PostToolUse Bash payload carries anything a resolver can use is not "
            "established from the snapshot; only that the matcher syntax accepts Bash.",
            "Whether codewrites' verb set was ever validated against `git apply`, `patch -p1`, or "
            "`awk -i inplace`. All three are absent from both parsers, but absence in the source "
            "is not evidence the forms are unused.",
        ],
    }


def write_inventory(inventory: dict[str, Any]) -> tuple[Path, Path]:
    """Emit the R04 report as JSON plus a Markdown rendering of that same JSON."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"write-detection-{inventory['snapshot_id']}"
    json_path = REPORTS_DIR / f"{stem}.json"
    md_path = REPORTS_DIR / f"{stem}.md"
    json_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(inventory), encoding="utf-8")
    return json_path, md_path


def render_markdown(inventory: dict[str, Any]) -> str:
    """A rendering of the JSON, never a second computation over the sources."""
    lines = [f"# Write-detection inventory — snapshot {inventory['snapshot_id']}", ""]
    lines.append("## The two defects are not the same defect")
    lines.append("")
    for name, row in inventory["defect_classes"].items():
        lines.append(f"**{name}** — {row['definition']}")
        lines.append("")
        lines.append(f"- Fixed by: {row['fixed_by']}")
        lines.append("")
    lines.append(f"> {inventory['shared_parser_does_not_fix_blindness']}")
    lines.append("")

    fix = inventory["shipped_fix_that_bought_nothing"]
    lines.append("## The shipped fix that bought nothing")
    lines.append("")
    lines.append(f"`{fix['module']}` — Bash branch reachable in production: "
                 f"**{fix['bash_branch_reachable_in_production']}**")
    lines.append("")
    lines.append(fix["evidence"])
    lines.append("")
    lines.append(fix["why_the_stop_gates_escaped"])
    lines.append("")

    lines.append("## Registrations and Bash visibility")
    lines.append("")
    lines.append("| event | matcher | script | sees Bash | detects writes |")
    lines.append("|---|---|---|---|---|")
    for row in inventory["registrations"]:
        lines.append(
            f"| {row['event']} | `{row['matcher'] or '(none)'}` | "
            f"`{row['script'] or row['command'][:40]}` | "
            f"{'yes' if row['sees_bash'] else '**NO**'} | "
            f"{'yes' if row['detects_writes'] else '—'} |"
        )
    lines.append("")

    lines.append("## Defects")
    lines.append("")
    for defect in inventory["defects"]:
        lines.append(f"### {defect['id']} — {defect['severity']}")
        lines.append("")
        lines.append(f"`{defect['file']}` — {defect['summary']}")
        lines.append("")
        lines.append(defect["detail"])
        lines.append("")
        lines.append(f"**Failure scenario:** {defect['failure_scenario']}")
        lines.append("")

    lines.append("## Explicit unknowns")
    lines.append("")
    for unknown in inventory["unknowns"]:
        lines.append(f"- {unknown}")
    lines.append("")
    return "\n".join(lines)
