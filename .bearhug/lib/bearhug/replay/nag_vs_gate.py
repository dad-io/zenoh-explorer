"""M09 — nag vs gate: prose-only rules, advisory hooks, blocking gates, and how far each can see.

Roadmap 4.7 asks which prose to delete and which to gate. Before any rate can answer that, two
joins have to be honest. A RULE is joined to a MECHANISM only by an explicit reference: a lead
clause that names a registered hook, or a ledger detector whose author recorded which gate it is
attributed to (`RULE_GATES`). A section body that names a hook is reported as an ambiguity for the
directives in it, never resolved for them. A MECHANISM's class is read from its source (block /
inject), and from a stop-census run when one exists for this snapshot; a hook that decides outside
the snapshot (a delegate, a binary) is `unavailable-external`, not "does nothing".

Violation rates appear for a rule only when its detector is in `validated_detectors` — the set
M14's ground truth produces. Until then the counts are carried and the rate is withheld, with the
reason on the row.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bearhug.lint.gates import BLOCKS, INJECTS, classify_hook, parse_hooks, resolve_script
from bearhug.lint.reachability import EDIT_TOOLS, TURN_SCOPED, WRITE_CONCERNED, matcher_tools
from bearhug.paths import REPORTS_DIR, assert_writable
from bearhug.replay.ledger import RULE_GATES, LedgerData
from bearhug.replay.observability import ObservabilityInventory, build_observability_inventory

MECHANISM_CLASSES = (
    "blocking",
    "advisory-injection",
    "advisory-plain-text",
    "registration-only",
    "unavailable-external",
)
RULE_CLASSES = (
    "explicitly-gated",
    "detector-attributed",
    "ambiguous-section-mechanism",
    "prose-only",
)

LIMITS = (
    "A rule is joined to a mechanism only by an explicit reference (a lead clause naming a "
    "registered hook, or a ledger detector's recorded gate). A section naming a hook is an "
    "ambiguity for its directives, not a join.",
    "Mechanism class is read from source and, where a stop census exists for this snapshot, from "
    "observed runs. Neither establishes that the mechanism fired on any real turn.",
    "Reachability is structural (matcher versus the tools a write travels through); it says "
    "where a gate cannot look, not what it missed.",
    "A violation rate is reported only for a detector M14 has validated. Every other rate is "
    "withheld and its counts carried, so a number nobody has checked cannot become a headline.",
    "Silence is not absence: `registration-only` and `unavailable-external` mean this snapshot "
    "cannot say what the mechanism does, never that it does nothing.",
)


@dataclass(frozen=True, slots=True)
class MechanismRow:
    script: str
    mechanism_class: str
    registrations: tuple[str, ...]
    events: tuple[str, ...]
    multiply_registered: bool
    reachability: str
    reachability_by_event: dict[str, str]
    static: dict[str, bool]
    observed_behaviours: tuple[str, ...] = ()
    contract_captured: bool | None = None
    ambiguity: str | None = None


@dataclass(frozen=True, slots=True)
class RuleRow:
    id: str
    statement: str
    section: str | None
    line: int | None
    rule_class: str
    mechanism_refs: tuple[str, ...] = ()
    candidate_mechanisms: tuple[str, ...] = ()
    mechanism_class: str | None = None
    detector: str | None = None
    counts: dict[str, Any] | None = None
    violation_rate: float | None = None
    rate_withheld_reason: str | None = None


@dataclass(slots=True)
class NagVsGateTable:
    snapshot_id: str
    mechanisms: list[MechanismRow] = field(default_factory=list)
    rules: list[RuleRow] = field(default_factory=list)
    stop_census_used: bool = False
    ledger_used: bool = False
    validated_detectors: tuple[str, ...] = ()

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "rules": len(self.rules),
            "mechanisms": len(self.mechanisms),
            "rules_by_class": dict(Counter(row.rule_class for row in self.rules)),
            "mechanisms_by_class": dict(Counter(row.mechanism_class for row in self.mechanisms)),
            "mechanisms_by_reachability": dict(
                Counter(row.reachability for row in self.mechanisms)
            ),
            "rules_with_a_detector": sum(1 for row in self.rules if row.detector),
            "rules_with_a_reported_rate": sum(
                1 for row in self.rules if row.violation_rate is not None
            ),
            "stop_census_used": self.stop_census_used,
            "ledger_used": self.ledger_used,
            "validated_detectors": list(self.validated_detectors),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "snapshot_id": self.snapshot_id,
            "summary": self.summary,
            "mechanisms": [asdict(row) for row in self.mechanisms],
            "rules": [asdict(row) for row in self.rules],
            "limits": list(LIMITS),
        }


def _safe(snapshot_id: str) -> str:
    return snapshot_id.replace("@", "-at-").replace("/", "-")


def load_stop_census(
    snapshot_dir: Path | str, *, reports_dir: Path | str | None = None
) -> dict | None:
    """The stop census written for this snapshot, if one exists. None is 'no census', never a
    census for some other snapshot."""
    snapshot = Path(snapshot_dir)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id", snapshot.name)
    root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    candidates = (
        root / f"stop-contract-{snapshot_id}.json",
        root / f"stop-contract-{_safe(snapshot_id)}.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def _reachability(event: str, tools: set[str], source: str) -> str:
    if event in TURN_SCOPED:
        return "turn-scoped"
    if not tools:
        return "all-tools"
    if "Bash" in tools:
        return "sees-bash"
    if tools & EDIT_TOOLS and WRITE_CONCERNED.search(source):
        return "blind-to-bash-writes"
    return "matcher-limited"


_REACH_ORDER = ("blind-to-bash-writes", "matcher-limited", "sees-bash", "all-tools", "turn-scoped")


def _worst(reaches: dict[str, str]) -> str:
    for label in _REACH_ORDER:
        if label in reaches.values():
            return label
    return "not-applicable"


def _census_by_script(census: dict | None) -> tuple[dict[str, set[str]], dict[str, bool | None]]:
    behaviours: dict[str, set[str]] = {}
    captured: dict[str, bool | None] = {}
    if not census:
        return behaviours, captured
    for observation in census.get("observations", []):
        script = observation.get("script")
        if not script:
            continue
        if observation.get("availability") == "observed":
            behaviours.setdefault(script, set()).update(observation.get("behaviours", []))
        else:
            behaviours.setdefault(script, set())
    for registration in census.get("registrations", []):
        script = registration.get("script")
        if script:
            captured[script] = registration.get("contract_captured")
    return behaviours, captured


def _mechanisms(project: Path, settings: dict, census: dict | None) -> list[MechanismRow]:
    observed, captured = _census_by_script(census)
    grouped: dict[str, list] = {}
    for spec in parse_hooks(settings):
        key = resolve_script(spec.command) or spec.command
        grouped.setdefault(key, []).append(spec)

    rows: list[MechanismRow] = []
    for key, specs in grouped.items():
        script = resolve_script(specs[0].command)
        if script:
            verdict = classify_hook(project / script)
            source = ""
            if verdict["readable"]:
                source = (project / script).read_text(encoding="utf-8", errors="replace")
        else:
            # An inline command with no script in the tree: classify the command text itself.
            verdict = {
                "blocks": bool(BLOCKS.search(key)),
                "injects": bool(INJECTS.search(key)),
                "readable": False,
            }
            source = key
        reaches: dict[str, str] = {}
        tools_by_event: dict[str, set[str]] = {}
        for spec in specs:
            tools_by_event.setdefault(spec.event, set()).update(matcher_tools(spec.matcher))
        for event, tools in tools_by_event.items():
            reaches[event] = _reachability(event, tools, source)

        contract = captured.get(key)
        ambiguity = None
        if contract is False or not verdict["readable"]:
            mechanism_class = "unavailable-external"
            ambiguity = (
                "the code that decides is outside the snapshot"
                if contract is False
                else "no script in the snapshot resolves from this command"
            )
        elif verdict["blocks"]:
            mechanism_class = "blocking"
        elif verdict["injects"]:
            mechanism_class = "advisory-injection"
        elif "plain_text" in observed.get(key, set()):
            mechanism_class = "advisory-plain-text"
        else:
            mechanism_class = "registration-only"
            ambiguity = (
                "source neither blocks nor injects by static shape and no stop census exists for "
                "this snapshot to observe a run"
                if census is None
                else "source neither blocks nor injects by static shape, and no observed run "
                "produced text or a decision"
            )
        rows.append(
            MechanismRow(
                script=key,
                mechanism_class=mechanism_class,
                registrations=tuple(f"{spec.event}/{spec.matcher or '*'}" for spec in specs),
                events=tuple(dict.fromkeys(spec.event for spec in specs)),
                multiply_registered=len(specs) > 1,
                reachability=_worst(reaches),
                reachability_by_event=reaches,
                static=dict(verdict),
                observed_behaviours=tuple(sorted(observed.get(key, set()))),
                contract_captured=contract,
                ambiguity=ambiguity,
            )
        )
    rows.sort(key=lambda row: row.script)
    return rows


def _rate(
    rule: str, ledger: LedgerData | None, validated: frozenset[str]
) -> tuple[dict[str, Any] | None, float | None, str | None]:
    if ledger is None:
        return None, None, "no ledger supplied; counts unavailable"
    buckets = ledger.counts.get(rule)
    if not buckets:
        return None, None, f"the ledger carries no row for {rule}"
    window = "post" if "post" in buckets else "all"
    counts = buckets[window]
    summary = {"window": window, "denominator": counts.denominator, "violated": counts.violated}
    if rule not in validated:
        return summary, None, (
            f"classifier `{rule}` has no M14 ground truth; the rate is withheld until it does"
        )
    if counts.denominator == 0:
        return summary, None, "denominator is zero in the measured window"
    return summary, counts.violated / counts.denominator, None


def build_nag_vs_gate(
    snapshot_dir: Path | str,
    *,
    inventory: ObservabilityInventory | None = None,
    stop_census: dict | None = None,
    ledger: LedgerData | None = None,
    validated_detectors: frozenset[str] = frozenset(),
) -> NagVsGateTable:
    snapshot = Path(snapshot_dir)
    project = snapshot / "project"
    inventory = inventory or build_observability_inventory(snapshot)
    census_snapshot = stop_census.get("snapshot_id") if stop_census is not None else None
    if census_snapshot not in (None, inventory.snapshot_id):
        raise ValueError(
            f"stop census is for snapshot {stop_census.get('snapshot_id')!r}, "
            f"not {inventory.snapshot_id!r}"
        )
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    table = NagVsGateTable(
        snapshot_id=inventory.snapshot_id,
        stop_census_used=stop_census is not None,
        ledger_used=ledger is not None,
        validated_detectors=tuple(sorted(validated_detectors)),
    )
    table.mechanisms = _mechanisms(project, settings, stop_census)
    class_of = {row.script: row.mechanism_class for row in table.mechanisms}

    for row in inventory.rows:
        if row.source_type != "claude_directive":
            continue
        detector = None
        if row.detector_task_id and row.detector_task_id.startswith("detector-"):
            detector = row.detector_task_id.removeprefix("detector-")
        mechanism_class = None
        candidates: tuple[str, ...] = ()
        if row.mechanism_refs:
            rule_class = "explicitly-gated"
            mechanism_class = class_of.get(row.mechanism_refs[0])
        elif detector and RULE_GATES.get(detector) is not None:
            rule_class = "detector-attributed"
            gate = RULE_GATES[detector]
            mechanism_class = next(
                (cls for script, cls in class_of.items() if Path(script).stem == gate), None
            )
        elif row.section_mechanism_refs:
            rule_class = "ambiguous-section-mechanism"
            candidates = row.section_mechanism_refs
        else:
            rule_class = "prose-only"
        counts, rate, reason = (None, None, None)
        if detector:
            counts, rate, reason = _rate(detector, ledger, validated_detectors)
        table.rules.append(
            RuleRow(
                id=row.id,
                statement=row.statement,
                section=row.section,
                line=row.line,
                rule_class=rule_class,
                mechanism_refs=row.mechanism_refs,
                candidate_mechanisms=candidates,
                mechanism_class=mechanism_class,
                detector=detector,
                counts=counts,
                violation_rate=rate,
                rate_withheld_reason=reason,
            )
        )
    return table


def render_nag_vs_gate(table: NagVsGateTable) -> str:
    data = table.as_dict()
    summary = data["summary"]
    lines = [
        f"# Nag vs gate — {table.snapshot_id}",
        "",
        f"- rules: {summary['rules']} — "
        + ", ".join(f"{k} {v}" for k, v in sorted(summary["rules_by_class"].items())),
        f"- mechanisms: {summary['mechanisms']} — "
        + ", ".join(f"{k} {v}" for k, v in sorted(summary["mechanisms_by_class"].items())),
        "- reachability: "
        + ", ".join(f"{k} {v}" for k, v in sorted(summary["mechanisms_by_reachability"].items())),
        f"- rules with a detector: {summary['rules_with_a_detector']}; with a reported rate: "
        f"{summary['rules_with_a_reported_rate']} (validated: "
        f"{', '.join(summary['validated_detectors']) or 'none'})",
        f"- stop census used: {summary['stop_census_used']}; ledger used: {summary['ledger_used']}",
        "",
        "## Mechanisms",
        "",
        "| script | class | reachability | registrations | observed | note |",
        "|---|---|---|---|---|---|",
    ]
    for row in table.mechanisms:
        lines.append(
            f"| `{row.script.replace('|', '\\|')}` | {row.mechanism_class} | {row.reachability} | "
            f"{', '.join(r.replace('|', '\\|') for r in row.registrations)} | "
            f"{', '.join(row.observed_behaviours) or '—'} | {row.ambiguity or '—'} |"
        )
    lines += [
        "",
        "## Rules",
        "",
        "| id | class | mechanism | detector | rate | note |",
        "|---|---|---|---|---|---|",
    ]
    for row in table.rules:
        mechanism = ", ".join(row.mechanism_refs or row.candidate_mechanisms) or "—"
        if row.candidate_mechanisms:
            mechanism += " (section-level, not joined)"
        rate = f"{row.violation_rate:.0%}" if row.violation_rate is not None else "withheld"
        lines.append(
            f"| `{row.id}` | {row.rule_class} | {mechanism} | {row.detector or '—'} | {rate} | "
            f"{(row.rate_withheld_reason or '—').replace('|', '\\|')} |"
        )
    lines += ["", "## Limits", ""] + [f"- {limit}" for limit in data["limits"]]
    return "\n".join(lines) + "\n"


def write_nag_vs_gate(
    table: NagVsGateTable, *, reports_dir: Path | str | None = None
) -> tuple[Path, Path]:
    root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    safe = _safe(table.snapshot_id)
    json_path = assert_writable(root / f"nag-vs-gate-{safe}.json")
    md_path = assert_writable(root / f"nag-vs-gate-{safe}.md")
    json_path.write_text(
        json.dumps(table.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_nag_vs_gate(table), encoding="utf-8")
    return json_path, md_path


__all__ = [
    "LIMITS",
    "MECHANISM_CLASSES",
    "RULE_CLASSES",
    "MechanismRow",
    "NagVsGateTable",
    "RuleRow",
    "build_nag_vs_gate",
    "load_stop_census",
    "render_nag_vs_gate",
    "write_nag_vs_gate",
]
