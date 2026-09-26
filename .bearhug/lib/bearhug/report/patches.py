"""6.2 — the patch emitter. A proposed fix, as a unified diff against the snapshotted file.

There is no apply step here, and there will not be one (docs/CHARTER.md): a patch is written to
`patches/<finding-id>.diff` for a human to read and apply, in Barracuda, by hand. This module's
whole job is producing that diff safely, which means two things: never guess a fix for a finding
this module cannot resolve mechanically, and never let a patch that renames a memex anchor go
out without saying so — hence the ANCHOR-SAFETY verdict in every header (Task 2.8, `lint.anchors`).

One generator exists so far. `stalecount-claude-md-66` — CLAUDE.md's own count of its §0
one-liners disagreeing with §0's actual bullet count — is the only finding in the current set
with an unambiguous mechanical fix: swap the stale number-word for the current one. Every other
check produces findings this module deliberately does not try to fix (see `SKIP_REASONS`): a
wrong patch against a golden master is worse than none.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.lint.anchors import broken_anchors
from bearhug.lint.stalecount import NUMBER_WORDS
from bearhug.model import Finding, Severity
from bearhug.paths import PATCHES_DIR, assert_writable

#: word -> digit, reversed for digit -> word. STALECOUNT only fires on referents this file
#: already writes as words ("Eleven one-liners"), so the fix must stay in word form too — a
#: patch that flips prose from "Eleven" to "16" changes the file's own register, which is a
#: stylistic call this module has no business making unattended.
_COUNT_WORD = {value: word for word, value in NUMBER_WORDS.items()}

#: Why each OTHER check's findings get no generator, kept next to the one that exists so the
#: absence reads as a decision rather than an oversight. Checked by test against every check
#: name `lint.runner.run_all_checks` and `hooks.audit.audit` can actually produce.
SKIP_REASONS: dict[str, str] = {
    "DEADREF": "the fix is picking the INTENDED section or decision id, which is a judgment "
    "call about what the author meant, not a mechanical correction.",
    "SUPERSEDED": "the fix is deciding whether the citing rule should now defer to the "
    "successor decision, which can change what the rule says — a content edit, not a "
    "mechanical one.",
    "GATE-COVERAGE-FORWARD": "the fix touches .claude/settings.json or adds prose naming a "
    "gate, either of which is a judgment call about which is stale, not a mechanical patch "
    "to CLAUDE.md.",
    "MATCHER-REACHABILITY": "the fix is a matcher edit in .claude/settings.json weighed "
    "against real traffic share — a design decision, not a mechanical one.",
    "CONTRACT": "the fix is inside a hook script's own logic, not CLAUDE.md; this module only "
    "emits diffs against the snapshotted CLAUDE.md.",
    "LATENCY": "the fix is a timeout value in .claude/settings.json chosen against what the "
    "hook actually needs — a design decision, not a mechanical one.",
    "INERTNESS": "silence is not a defect with a single correct fix (docs/METHOD.md) — there "
    "is nothing here to patch mechanically.",
    "INPUT-ABSENT": "the finding is that CLAUDE.md or .claude/settings.json does not exist yet "
    "(a project observed before setup, mode 1) — there is no snapshotted file here for this "
    "module to diff against, and whether to run setup at all is the project's own decision.",
    "WRITE-PATH": "the fix is inside a hook script's matcher or body, not CLAUDE.md; out of "
    "this module's scope.",
    "OVERLAP": "the fix is deciding which registration to remove, merge, or keep as an "
    "alternative provider — a judgment call about the project's own automation, not a "
    "mechanical CLAUDE.md edit; out of this module's scope.",
    "GATE-COVERAGE-REVERSE": "the fix is either a settings.json matcher or a new CLAUDE.md "
    "sentence naming a gate — which one is right depends on whether the gate SHOULD be "
    "user-visible, a judgment call.",
    "COVERAGE": "the fix is a new fixture in hooks/fixtures.py, not a CLAUDE.md edit; out of "
    "this module's scope.",
    "ORDERING": "the fix is inside a hook script's own Stop-ordering logic, not CLAUDE.md; "
    "out of this module's scope.",
    "ARBITRATION": "the fix is deciding which of two conflicting Stop gates should win, which "
    "changes what the harness DOES, not a mechanical text correction.",
    "BUDGET": "the fix is trimming or restructuring CLAUDE.md content by editorial judgment "
    "about what a section needs to say, not a mechanical correction a diff generator can make.",
    # G02. Every one of these is a `.idea/runConfigurations/*.xml` edit, and this module only
    # emits diffs against the snapshotted CLAUDE.md.
    "RUNCONFIG-PARSE": "the fix is an XML edit in .idea/runConfigurations/, not CLAUDE.md. The "
    "one real instance is hand-authored as patches/G02-runconfig-malformed-comment.diff, "
    "because rewording a comment to avoid `--` trades copy-pasteability of the documented "
    "command and the better fix (move the command into a real script) is Barracuda's call.",
    "RUNCONFIG-WORKDIR": "the fix is choosing WHICH working directory a configuration should "
    "resolve against, which is a Barracuda judgment about that target, not a mechanical edit.",
    "RUNCONFIG-TOOL": "the fix is either defining the missing external tool or dropping the "
    "before-run task — opposite conclusions from the same finding, so not mechanical.",
    "RUNCONFIG-PORT": "the fix is deciding which configuration keeps the port, which changes "
    "what a human attaches to; out of this module's scope.",
    "RUNCONFIG-PURPOSE": "the finding is that a name and its flags disagree, and it does not "
    "establish which is wrong — patching either way would guess at the author's intent.",
    "RUNCONFIG-WORKLOAD": "the fix is choosing the benchmark or test selector a profile should "
    "run under, which is a Barracuda judgment about what is worth profiling.",
    "RUNCONFIG-TARGET": "an INFO inventory of declared targets with existence `unverified` "
    "(A.5) — there is no defect asserted here to fix.",
}

LIMIT_MECHANICAL = (
    "Proves only that this text substitution resolves the cited finding's own claim; it does "
    "not prove the resulting sentence still reads well, or that no other place in the file "
    "makes the same stale claim. A human applies this by hand and rereads the surrounding "
    "prose before committing it (docs/CHARTER.md — bear-hug has no apply step)."
)


@dataclass(frozen=True, slots=True)
class Patch:
    """One emitted `patches/<finding-id>.diff`, and the verdict that gated it."""

    finding_id: str
    path: Path
    anchor_verdict: str  # "SAFE" or "UNSAFE"
    anchor_records: tuple[str, ...] = ()


def _fix_stale_count(finding: Finding, claude_md: str) -> str | None:
    """The one generator. Swaps a stale count-word for the current count, in place.

    Returns the whole new file text, or None if this finding is not one this generator
    handles, or if it cannot locate the exact token to replace with confidence — a guess here
    would be a wrong patch against a golden master, which docs/CHARTER.md rates worse than no
    patch at all.
    """
    if finding.check != "STALECOUNT" or finding.severity is not Severity.COSMETIC:
        return None
    if not finding.evidence or finding.evidence[0].line is None:
        return None

    match = re.search(
        r"holds (\d+) top-level bullets\. The prose says (\d+)\.", finding.detail or ""
    )
    if not match:
        return None
    actual, claimed = int(match.group(1)), int(match.group(2))
    new_word, old_word = _COUNT_WORD.get(actual), _COUNT_WORD.get(claimed)
    if new_word is None or old_word is None:
        return None  # no word form on record for one of the two counts — do not guess digits

    lines = claude_md.splitlines(keepends=True)
    line_index = finding.evidence[0].line - 1
    if not 0 <= line_index < len(lines):
        return None
    line = lines[line_index]

    token = re.search(rf"\b{re.escape(old_word)}\b", line, re.IGNORECASE)
    if not token:
        return None
    replacement = new_word.capitalize() if token.group(0)[0].isupper() else new_word
    new_line = line[: token.start()] + replacement + line[token.end() :]
    if new_line == line:
        return None

    lines[line_index] = new_line
    return "".join(lines)


#: Every generator this module ships. One entry, deliberately — see the module docstring.
_GENERATORS = {"STALECOUNT": _fix_stale_count}


def _anchor_verdict(
    old_text: str, new_text: str, *, index: dict[str, Any]
) -> tuple[str, tuple[str, ...]]:
    """SAFE/UNSAFE plus the records that would break, computed the one way (`lint.anchors`).

    Never re-derives which anchors break: `broken_anchors` is Task 2.8's own answer to that
    question, and a second computation here is exactly the duplicate-authority failure mode
    docs/METHOD.md calls out in `lint.refs`.
    """
    broken = broken_anchors(old_text, new_text, index=index)
    if not broken:
        return "SAFE", ()
    records = sorted({record_id for ids in broken.values() for record_id in ids})
    return "UNSAFE", tuple(records)


def _render_header(
    finding: Finding, snapshot_id: str, verdict: str, records: tuple[str, ...]
) -> str:
    lines = [
        f"# bear-hug patch — proposed fix for finding {finding.id}",
        f"# check:            {finding.check}",
        f"# snapshot:         {snapshot_id}",
        f"# ANCHOR-SAFETY:    {verdict}"
        + (f" — would break memex record(s) {', '.join(records)}" if records else ""),
        "# apply by hand in project-barracuda; bear-hug has no apply step (docs/CHARTER.md)",
        "#",
    ]
    return "\n".join(lines) + "\n"


def generate_patch(
    finding: Finding,
    *,
    claude_md: str,
    index: dict[str, Any],
    snapshot_id: str,
    out_dir: Path | str | None = None,
) -> Patch | None:
    """Emit `patches/<finding.id>.diff` for one finding, or None if it admits no mechanical fix.

    Every write is routed through `paths.assert_writable`, so a caller cannot point this at
    project-barracuda even by passing the wrong ``out_dir`` by mistake.
    """
    generator = _GENERATORS.get(finding.check)
    if generator is None:
        return None
    new_text = generator(finding, claude_md)
    if new_text is None or new_text == claude_md:
        return None

    verdict, records = _anchor_verdict(claude_md, new_text, index=index)

    diff_lines = difflib.unified_diff(
        claude_md.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/CLAUDE.md@{snapshot_id}",
        tofile=f"b/CLAUDE.md@{snapshot_id}",
    )
    content = _render_header(finding, snapshot_id, verdict, records) + "".join(diff_lines)

    directory = Path(out_dir) if out_dir is not None else PATCHES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = assert_writable(directory / f"{finding.id}.diff")
    path.write_text(content, encoding="utf-8")
    return Patch(finding_id=finding.id, path=path, anchor_verdict=verdict, anchor_records=records)


def emit_patches(
    findings: list[Finding],
    *,
    claude_md: str,
    index: dict[str, Any],
    snapshot_id: str,
    out_dir: Path | str | None = None,
) -> list[Patch]:
    """Emit every mechanically-fixable finding's patch. Silent skips are a feature, not a bug."""
    patches: list[Patch] = []
    for finding in findings:
        patch = generate_patch(
            finding, claude_md=claude_md, index=index, snapshot_id=snapshot_id, out_dir=out_dir
        )
        if patch is not None:
            patches.append(patch)
    return patches


@dataclass(frozen=True, slots=True)
class Skipped:
    """One finding the emitter did not patch, and the stated reason — never a silent skip."""

    finding_id: str
    check: str
    reason: str


@dataclass(frozen=True, slots=True)
class PatchOutcome:
    snapshot_id: str
    patches: tuple[Patch, ...]
    skipped: tuple[Skipped, ...]

    def render(self) -> str:
        lines = [
            f"# bear-hug report --emit-patches — {self.snapshot_id}",
            "",
            f"emitted {len(self.patches)}, skipped {len(self.skipped)}",
            "",
        ]
        if self.patches:
            lines += ["## emitted (proposed; never applied by bear-hug)", ""]
            lines += [
                f"- `{p.finding_id}` → `{p.path.name}` — ANCHOR-SAFETY {p.anchor_verdict}"
                for p in self.patches
            ]
            lines.append("")
        if self.skipped:
            lines += ["## skipped, with reason", ""]
            lines += [f"- `{s.finding_id}` ({s.check}): {s.reason}" for s in self.skipped]
            lines.append("")
        return "\n".join(lines)


_NO_GENERATOR = (
    "no generator exists for this check and none is proposed: a wrong patch against a golden "
    "master is worse than none."
)


def emit_report_patches(
    snapshot_dir: Path | str,
    findings: list[Finding],
    *,
    out_dir: Path | str | None = None,
) -> PatchOutcome:
    """S01 — the explicit CLI path. Every finding either produces a patch or a stated skip.

    Refuses findings from another snapshot: a diff is written against the snapshotted
    `CLAUDE.md`, so a finding measured elsewhere would produce a patch for a file it never saw.
    """
    import json

    directory = Path(out_dir) if out_dir is not None else PATCHES_DIR
    assert_writable(directory / "probe.diff")  # refuse the subject before reading anything
    snapshot = Path(snapshot_dir)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id", snapshot.name)
    foreign = sorted({f.snapshot for f in findings if f.snapshot != snapshot_id})
    if foreign:
        raise ValueError(
            f"findings were measured against snapshot(s) {', '.join(foreign)}, not "
            f"{snapshot_id}; a patch must be diffed against the file the finding saw"
        )
    claude_md = (snapshot / "project" / "CLAUDE.md").read_text(encoding="utf-8")
    index = json.loads((snapshot / "memex-index.json").read_text(encoding="utf-8"))

    emitted: list[Patch] = []
    skipped: list[Skipped] = []
    for finding in findings:
        if finding.check not in _GENERATORS:
            skipped.append(
                Skipped(finding.id, finding.check, SKIP_REASONS.get(finding.check, _NO_GENERATOR))
            )
            continue
        patch = generate_patch(
            finding, claude_md=claude_md, index=index, snapshot_id=snapshot_id, out_dir=directory
        )
        if patch is None:
            skipped.append(
                Skipped(
                    finding.id,
                    finding.check,
                    "the generator found no mechanical fix for this row (an INFO-severity or "
                    "unresolvable count has no single correct replacement).",
                )
            )
            continue
        emitted.append(patch)
    return PatchOutcome(snapshot_id, tuple(emitted), tuple(skipped))


__all__ = [
    "Patch",
    "PatchOutcome",
    "SKIP_REASONS",
    "Skipped",
    "emit_patches",
    "emit_report_patches",
    "generate_patch",
]


# --- H04: one timeout, one entry, the host's own formatting ------------------------------------


def render_timeout_patch(
    settings_path: Path | str, command_substring: str, timeout: int, *,
    relpath: str = ".claude/settings.json",
) -> tuple[str, str]:
    """A unified diff that adds `"timeout": <n>` to the ONE hook entry whose command contains
    `command_substring`, and the settings text after it. Textual, not re-serialised: the host's
    indentation and key order survive, so the hunk is one added line. Refuses an entry that
    already carries a timeout (a ruling replaces nothing silently) and a command that is not
    registered (H04, ruled 2026-09-01: 30 s on the Stop coordinator; graft keeps its own)."""
    import difflib
    import json

    path = Path(settings_path)
    text = path.read_text(encoding="utf-8")
    settings = json.loads(text)
    entries = [
        hook
        for groups in (settings.get("hooks") or {}).values()
        for group in groups
        for hook in group.get("hooks", [])
        if command_substring in str(hook.get("command", ""))
    ]
    if not entries:
        raise ValueError(f"no hook whose command contains {command_substring!r} in {path}")
    if len(entries) > 1:
        raise ValueError(f"{len(entries)} hooks contain {command_substring!r}; name one")
    if "timeout" in entries[0]:
        raise ValueError(
            f"the {command_substring!r} entry already carries timeout={entries[0]['timeout']}; "
            "H04 adds a bound where none exists, it does not overrule one"
        )
    lines = text.split("\n")
    index = next(i for i, line in enumerate(lines) if command_substring in line)
    indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    # insert BEFORE the command line so the entry's last key keeps its trailing-comma shape
    lines.insert(index, f'{indent}"timeout": {int(timeout)},')
    after = "\n".join(lines)
    json.loads(after)  # the edited text must still parse; a textual edit that breaks it is refused
    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{relpath}", tofile=f"b/{relpath}",
        )
    )
    return diff, after
