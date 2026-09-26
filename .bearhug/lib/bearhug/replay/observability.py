"""M07 — inventory stated rules and mechanisms before adding more classifiers.

This module does not decide whether a directive is good, complied with, or enforceable. It tiles
the captured CLAUDE.md through the existing section parser, records every parsed lead-clause
directive, separately records every hook registration, and appends the detectors the effectiveness
ledger already implements. Joins are explicit textual references to a REGISTERED hook script: a
hook named in a section body is context for that section, never assigned to each directive in it,
and a file that is not a registered hook (a deploy script, a test) is recorded as a file reference,
not a mechanism.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from bearhug.lint.gates import classify_hook, parse_hooks, resolve_script
from bearhug.lint.parse import parse_sections
from bearhug.paths import REPORTS_DIR, assert_writable
from bearhug.read_optional import read_json_or_default, read_text_or_default
from bearhug.replay.ledger import RULE_GATES, RULES, UNMEASURABLE_GATES

_AUTHORITY = re.compile(
    r"\b(NEVER|MUST|ALWAYS|REQUIRED|FORBIDDEN|CRITICAL|MANDATORY|ONLY|overrides?)\b",
    re.IGNORECASE,
)
_FILE_REF = re.compile(r"[A-Za-z0-9_./-]*[A-Za-z0-9_-]\.(?:py|sh|cjs|js)\b")

#: What each existing ledger detector counts, stated once so the inventory reports the detector's
#: own contract rather than re-deriving it from the ledger's source.
_MEASURED_RULES: dict[str, dict[str, str]] = {
    "git-push": {
        "positive": "prohibition: no positive event; denominator is parsed git push attempts",
        "violation": "shlex-parsed non-dry-run git push tool call",
        "attribution": "git_hard_safety_hits + attributed hard-safety complaint",
    },
    "git-stash": {
        "positive": "prohibition: no positive event; denominator is parsed git stash attempts",
        "violation": "shlex-parsed non-readonly git stash tool call",
        "attribution": "git_hard_safety_hits + attributed hard-safety complaint",
    },
    "dlv-before-claiming": {
        "positive": "real dlv invocation after a detected Go write in the same replay turn",
        "violation": "detected Go write without a later real dlv invocation in that turn",
        "attribution": "bash_writes_go/_go_path + has_dlv_invocation + dlv complaint signature",
    },
    "review-after-write": {
        "positive": "opus-reviewer dispatch after a detected Go write in the same replay turn",
        "violation": "detected Go write without a later matching reviewer dispatch",
        "attribution": "bash_writes_go/_go_path + Agent payload + review complaint signature",
    },
    "one-ask-per-turn": {
        "positive": "response-shape classifier returns ok or ok-stop-requested",
        "violation": "response-shape classifier returns bundled or no-engagement",
        "attribution": "ask_shape + response-shape complaint signature",
    },
    "max-two-concurrent-subagents": {
        "positive": "a turn's largest single assistant message dispatched at most two subagents",
        "violation": "an assistant message (by message.id) with more than two Agent/Task calls",
        "attribution": "structural: tool_use blocks grouped by message.id; no gate can remind",
        # The CLAUDE.md lead clause this detector measures, matched by its opening words.
        "statement_prefix": "MAX 2 CONCURRENT SUBAGENTS",
    },
}

LIMITS = (
    "A parsed lead clause is a candidate rule, not a judgment that the prose is correct.",
    "A mechanism join is an explicit reference to a REGISTERED hook script; section-level hook "
    "references are context only and are not joined to each directive.",
    "Static block/injection classification is read from source and does not establish "
    "reachability or effect.",
    "Unmeasurable means no established detector here, never compliant or zero.",
    "Detector rows restate what the ledger already counts; no detector was added here.",
)


@dataclass(frozen=True, slots=True)
class ObservabilityRow:
    id: str
    source_type: str
    source: str
    line: int | None
    section: str | None
    statement: str
    stated_authority: tuple[str, ...] = ()
    mechanism_refs: tuple[str, ...] = ()
    file_refs: tuple[str, ...] = ()
    section_mechanism_refs: tuple[str, ...] = ()
    observable_positive: str | None = None
    observable_violation: str | None = None
    attribution_source: str | None = None
    unmeasurable_reason: str | None = None
    detector_task_id: str | None = None
    event: str | None = None
    matcher: str | None = None
    registration_index: str | None = None


@dataclass(slots=True)
class ObservabilityInventory:
    snapshot_id: str
    rows: list[ObservabilityRow] = field(default_factory=list)
    sections: int = 0
    directives: int = 0
    hook_registrations: int = 0
    existing_detectors: int = 0

    def as_dict(self) -> dict[str, Any]:
        directive_rows = [row for row in self.rows if row.source_type == "claude_directive"]
        return {
            "schema_version": "1",
            "snapshot_id": self.snapshot_id,
            "summary": {
                "sections": self.sections,
                "directives": self.directives,
                "hook_registrations": self.hook_registrations,
                "existing_detectors": self.existing_detectors,
                "rows": len(self.rows),
                "directives_naming_a_registered_hook": sum(
                    1 for row in directive_rows if row.mechanism_refs
                ),
                "directive_rows_without_explicit_mechanism": sum(
                    1 for row in directive_rows if not row.mechanism_refs
                ),
                # Document order: directive rows are already sorted by line.
                "sections_naming_a_registered_hook": list(
                    dict.fromkeys(
                        row.section for row in directive_rows if row.section_mechanism_refs
                    )
                ),
            },
            "rows": [asdict(row) for row in self.rows],
            "limits": list(LIMITS),
        }


def _authority(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(1).lower() for match in _AUTHORITY.finditer(text)))


def _file_refs(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_FILE_REF.findall(text)))


def _registered_refs(text: str, registered: dict[str, str]) -> tuple[str, ...]:
    """Registered hook scripts the text names, by basename, resolved to the registered path."""
    found: list[str] = []
    for ref in _file_refs(text):
        script = registered.get(ref.rsplit("/", 1)[-1])
        if script and script not in found:
            found.append(script)
    return tuple(found)


def build_observability_inventory(snapshot_dir: Path | str) -> ObservabilityInventory:
    snapshot = Path(snapshot_dir)
    project = snapshot / "project"
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id", snapshot.name)
    claude_text, _claude_text_absent = read_text_or_default(project / "CLAUDE.md")
    settings, _settings_absent = read_json_or_default(project / ".claude" / "settings.json")

    specs = parse_hooks(settings)
    registered: dict[str, str] = {}
    for spec in specs:
        script = resolve_script(spec.command)
        if script:
            registered.setdefault(script.rsplit("/", 1)[-1], script)

    sections = parse_sections(claude_text)
    inventory = ObservabilityInventory(snapshot_id=snapshot_id, sections=len(sections))
    for section in sections:
        section_refs = _registered_refs(section.body, registered)
        for directive in section.directives:
            direct_refs = _registered_refs(directive.text, registered)
            inventory.rows.append(
                ObservabilityRow(
                    id=f"claude-{section.id}-L{directive.line}",
                    source_type="claude_directive",
                    source="CLAUDE.md",
                    line=directive.line,
                    section=section.id,
                    statement=directive.text,
                    stated_authority=_authority(f"{section.title} {directive.text}"),
                    mechanism_refs=direct_refs,
                    file_refs=tuple(
                        ref for ref in _file_refs(directive.text)
                        if ref.rsplit("/", 1)[-1] not in registered
                    ),
                    section_mechanism_refs=section_refs,
                    unmeasurable_reason=(
                        None
                        if direct_refs
                        else "No registered hook is named by this parsed lead clause."
                    ),
                    detector_task_id=f"M08-L{directive.line}",
                )
            )
            inventory.directives += 1

    for spec in specs:
        script = resolve_script(spec.command)
        verdict = (
            classify_hook(project / script)
            if script
            else {"blocks": False, "injects": False, "readable": False}
        )
        authority = []
        if not verdict["readable"]:
            authority.append("source-not-in-snapshot")
        if verdict["blocks"]:
            authority.append("blocking-by-static-source")
        if verdict["injects"]:
            authority.append("context-injection-by-static-source")
        if not authority:
            authority.append("registration-only")
        registration = f"{spec.event}:{spec.group_index}:{spec.position_in_group}"
        inventory.rows.append(
            ObservabilityRow(
                id=f"hook-{spec.event}-{spec.group_index}-{spec.position_in_group}",
                source_type="hook_registration",
                source=".claude/settings.json",
                line=None,
                section=None,
                statement=spec.command,
                stated_authority=tuple(authority),
                mechanism_refs=(script,) if script else (),
                observable_positive=f"registration selected on {spec.event}/{spec.matcher or '*'}",
                observable_violation=None,
                attribution_source=(
                    f"settings registration {registration}; script static classification"
                ),
                unmeasurable_reason=(
                    "Registration and source shape do not establish a rule-level compliance "
                    "detector."
                    if verdict["readable"]
                    else "The script this registration runs was not captured in the snapshot, so "
                    "its source could not be classified."
                ),
                event=spec.event,
                matcher=spec.matcher or "*",
                registration_index=registration,
            )
        )
        inventory.hook_registrations += 1

    for rule in RULES:
        measured = _MEASURED_RULES[rule]
        gate = RULE_GATES[rule]
        if gate is None:
            mechanism: tuple[str, ...] = ("prose-only",)
        else:
            mechanism = tuple(
                script for base, script in registered.items()
                if base.rsplit(".", 1)[0] == gate
            ) or (gate,)
        prefix = measured.get("statement_prefix")
        if prefix:
            inventory.rows = [
                replace(row, detector_task_id=f"detector-{rule}")
                if row.source_type == "claude_directive" and row.statement.startswith(prefix)
                else row
                for row in inventory.rows
            ]
        inventory.rows.append(
            ObservabilityRow(
                id=f"detector-{rule}",
                source_type="ledger_detector",
                source="src/bearhug/replay/ledger.py",
                line=None,
                section=None,
                statement=rule,
                stated_authority=("derived-detector",),
                mechanism_refs=mechanism,
                observable_positive=measured["positive"],
                observable_violation=measured["violation"],
                attribution_source=measured["attribution"],
            )
        )
        inventory.existing_detectors += 1

    for rule, reason in UNMEASURABLE_GATES.items():
        inventory.rows.append(
            ObservabilityRow(
                id=f"detector-gap-{rule}",
                source_type="ledger_detector_gap",
                source="src/bearhug/replay/ledger.py",
                line=None,
                section=None,
                statement=rule,
                stated_authority=("unmeasurable",),
                mechanism_refs=tuple(
                    script for base, script in registered.items()
                    if base.rsplit(".", 1)[0] == rule
                ),
                observable_positive="attributed complaint count where the transcript exposes it",
                observable_violation=None,
                attribution_source="hook signature attribution",
                unmeasurable_reason=reason,
                detector_task_id=f"M08-{rule}",
            )
        )

    inventory.rows.sort(key=lambda row: (row.source_type, row.source, row.line or 0, row.id))
    return inventory


def render_observability_inventory(inventory: ObservabilityInventory) -> str:
    data = inventory.as_dict()
    summary = data["summary"]
    lines = [
        f"# Rule observability inventory — {inventory.snapshot_id}",
        "",
        f"- {summary['sections']} CLAUDE sections; {summary['directives']} parsed directives, of "
        f"which {summary['directives_naming_a_registered_hook']} name a registered hook",
        f"- sections whose body names a registered hook: "
        f"{', '.join('§' + s for s in summary['sections_naming_a_registered_hook']) or 'none'}",
        f"- {summary['hook_registrations']} hook registrations",
        f"- {summary['existing_detectors']} existing rate-producing detectors",
        f"- {summary['directive_rows_without_explicit_mechanism']} directives without an explicit "
        "mechanism",
        "",
        "| id | source | statement | authority | mechanism | observable violation | next |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in inventory.rows:
        if not row.statement:
            raise AssertionError(f"empty inventory statement: {row.id}")
        statement = row.statement.replace("|", "\\|").replace("\n", " ")
        source = row.source + (f":{row.line}" if row.line else "")
        authority = ", ".join(row.stated_authority) or "not explicit"
        mechanism = ", ".join(row.mechanism_refs) or "—"
        violation = (row.observable_violation or "unmeasurable").replace("|", "\\|")
        lines.append(
            f"| `{row.id}` | {source} | {statement} | {authority} | {mechanism} | {violation} | "
            f"{row.detector_task_id or '—'} |"
        )
    lines += ["", "## Limits", ""] + [f"- {limit}" for limit in data["limits"]]
    return "\n".join(lines) + "\n"


def write_observability_inventory(
    inventory: ObservabilityInventory, *, reports_dir: Path | str | None = None
) -> tuple[Path, Path]:
    root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    safe = inventory.snapshot_id.replace("@", "-at-").replace("/", "-")
    json_path = assert_writable(root / f"rule-observability-{safe}.json")
    markdown_path = assert_writable(root / f"rule-observability-{safe}.md")
    json_path.write_text(
        json.dumps(inventory.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_observability_inventory(inventory), encoding="utf-8")
    return json_path, markdown_path


__all__ = [
    "LIMITS",
    "ObservabilityInventory",
    "ObservabilityRow",
    "build_observability_inventory",
    "render_observability_inventory",
    "write_observability_inventory",
]
