"""G07 — measured coverage of qualified Go changes by the required toolchain stages.

This measures transcript-observable invocations, not outcomes. Claude transcripts do not name
which async PostToolUse hook produced a successful ``hook_success`` record, so treating a hook's
registration as proof that its commands ran would turn G04's blind spot into fabricated coverage.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from bearhug.paths import REPORTS_DIR, assert_writable
from bearhug.replay.dlv import DLV_CLASSIFIER_VERSION, classify_dlv_command
from bearhug.replay.dlv_proof import RECOMMENDED_REQUIRED_LEVEL
from bearhug.replay.shell import bindings, resolve_verb, segments, strip_env
from bearhug.replay.toolchain_reachability import is_go_write
from bearhug.replay.transcript import Turn, iter_events, iter_turns

MEASUREMENT_VERSION: Final = "1.0.0"
QUALIFIED_CHANGE_DEFINITION: Final = (
    "A transcript turn opened by a genuine human prompt or subagent dispatch that contains at "
    "least one normalized Go-writing tool call. The measurement unit is the turn, not a file or "
    "tool call; stage evidence counts only at or after that turn's final normalized Go-writing "
    "tool call and before the next turn opener. Direct Edit/Write/MultiEdit Go paths and "
    "classifier-recognized Bash writes qualify."
)
STAGES: Final = ("vet_package", "build_module", "race_test_package", "dlv_level_2")
LIMITS: Final = (
    "Every numerator is a floor: only explicit transcript-visible invocations are counted; a "
    "successful hook has no script identity in the transcript. Bash writes inherit the M14 "
    "go-write classifier's measured misses and false positives. Wrapper commands and headless or "
    "GoLand activity without a structured receipt are invisible. An invocation is not a pass: "
    "tool exit status and code correctness are not established by this report."
)


@dataclass(slots=True)
class StageCoverage:
    eligible: int = 0
    observed: int = 0

    @property
    def fraction(self) -> float | None:
        return self.observed / self.eligible if self.eligible else None

    def as_dict(self) -> dict[str, int | float | None]:
        return {**asdict(self), "fraction": self.fraction}


@dataclass(slots=True)
class GoCoverage:
    qualified_changes: int = 0
    bash_mediated_qualified_changes: int = 0
    turns_measured: int = 0
    transcripts_measured: int = 0
    date_range: tuple[str, str] = ("", "")
    stages: dict[str, StageCoverage] = field(
        default_factory=lambda: {name: StageCoverage() for name in STAGES}
    )
    unit: str = "qualified_go_change_turn"
    measurement_version: str = MEASUREMENT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "measurement_version": self.measurement_version,
            "unit": self.unit,
            "qualified_change_definition": QUALIFIED_CHANGE_DEFINITION,
            "qualified_changes": self.qualified_changes,
            "bash_mediated_qualified_changes": self.bash_mediated_qualified_changes,
            "turns_measured": self.turns_measured,
            "transcripts_measured": self.transcripts_measured,
            "date_range": list(self.date_range),
            "stages": {name: self.stages[name].as_dict() for name in STAGES},
            "limits": LIMITS,
        }


def _go_invocation(tokens: list[str], bound: dict[str, str]) -> tuple[str, list[str]] | None:
    tokens = strip_env(tokens)
    if not tokens:
        return None
    program = resolve_verb(tokens[0], bound)
    if program is None or program.rsplit("/", 1)[-1] != "go":
        return None
    args = tokens[1:]
    index = 0
    # Global flags accepted before the subcommand. The value-taking forms are kept bounded; an
    # unknown global flag means the following non-flag remains the best observable subcommand.
    while index < len(args) and args[index].startswith("-"):
        flag = args[index]
        index += 1
        if flag in {"-C"} and index < len(args):
            index += 1
    if index >= len(args):
        return None
    return args[index], args[index + 1 :]


def _command_stages(command: object) -> set[str]:
    if not isinstance(command, str) or not command:
        return set()
    found: set[str] = set()
    bound = bindings(command)
    for token_segment in segments(command):
        invocation = _go_invocation(token_segment, bound)
        if invocation is None:
            continue
        subcommand, args = invocation
        if subcommand == "vet" and not any("..." in arg for arg in args):
            found.add("vet_package")
        elif subcommand == "build" and any(arg == "./..." for arg in args):
            found.add("build_module")
        elif subcommand == "test" and any(
            arg == "-race" or arg.startswith("-race=") for arg in args
        ):
            found.add("race_test_package")
    if classify_dlv_command(command) == "real-session":
        found.add("dlv_level_2")
    return found


def _qualified_turn(turn: Turn) -> tuple[bool, bool, set[str]]:
    final_write = -1
    bash_write = False
    for index, event in enumerate(turn.events):
        if not is_go_write(event):
            continue
        final_write = index
        bash_write = bash_write or event.name == "Bash"
    if final_write < 0:
        return False, False, set()
    found: set[str] = set()
    for event in turn.events[final_write:]:
        if event.kind == "tool_use" and event.name == "Bash":
            found.update(_command_stages(event.payload.get("command")))
    return True, bash_write, found


def compute_go_coverage(paths: list[Path] | tuple[Path, ...]) -> GoCoverage:
    """Measure required stage invocations after each qualified turn's final Go write."""
    report = GoCoverage(transcripts_measured=len(paths))
    first_day = last_day = ""
    for path in paths:
        for turn in iter_turns(iter_events(path)):
            report.turns_measured += 1
            day = turn.timestamp[:10]
            if day and (not first_day or day < first_day):
                first_day = day
            if day > last_day:
                last_day = day
            qualified, bash_write, found = _qualified_turn(turn)
            if not qualified:
                continue
            report.qualified_changes += 1
            report.bash_mediated_qualified_changes += int(bash_write)
            for name, stage in report.stages.items():
                stage.eligible += 1
                stage.observed += int(name in found)
    report.date_range = (first_day, last_day)
    return report


def load_m14_validation(
    reports_dir: Path | str, *, corpus_kind: str, corpus_digest: str
) -> dict[str, Any]:
    """Load the two M14 reports that authorize the denominator and DLV rate."""
    root = Path(reports_dir)
    validation: dict[str, Any] = {}
    for classifier in ("go-write", "dlv"):
        path = root / f"ground-truth-{classifier}-{corpus_kind}-{corpus_digest[:12]}.json"
        if not path.is_file():
            raise ValueError(
                f"no M14 {classifier} report for corpus digest {corpus_digest[:12]} at {path}"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("corpus_digest") != corpus_digest:
            raise ValueError(f"M14 {classifier} report has a different corpus digest")
        if payload.get("validated") is not True:
            raise ValueError(f"M14 {classifier} report is not validated")
        validation[classifier] = {
            "classifier_version": payload.get("classifier_version"),
            "sample_size": payload.get("sample_size"),
            "validated": True,
            "validation_reason": payload.get("validation_reason"),
            "per_class": payload.get("per_class"),
        }
    return validation


def render_go_coverage(
    report: GoCoverage,
    *,
    corpus_kind: str,
    corpus_digest: str,
    classifier_validation: dict[str, Any],
) -> str:
    lines = [
        f"# Qualified Go change coverage — {corpus_kind}:{corpus_digest[:12]}",
        "",
        f"- measurement classifier: {MEASUREMENT_VERSION}",
        f"- unit: `{report.unit}`",
        f"- {report.qualified_changes} qualified changes across "
        f"{report.transcripts_measured} transcripts and {report.turns_measured} turns",
        f"- {report.bash_mediated_qualified_changes} qualified turns include Bash writes",
        "",
        "## Definition fixed before the rate",
        "",
        QUALIFIED_CHANGE_DEFINITION,
        "",
        "## Coverage",
        "",
        "| stage | observed | eligible | fraction |",
        "|---|---:|---:|---:|",
    ]
    for name in STAGES:
        row = report.stages[name]
        fraction = "unavailable" if row.fraction is None else f"{row.fraction:.1%}"
        lines.append(f"| {name} | {row.observed} | {row.eligible} | {fraction} |")
    lines += [
        "",
        f"The DLV row requires G05 level {RECOMMENDED_REQUIRED_LEVEL}: a real DLV process launch, "
        "not the word `dlv`.",
        "",
        "## M14 classifier validation",
        "",
    ]
    for classifier in ("go-write", "dlv"):
        item = classifier_validation[classifier]
        lines.append(
            f"- `{classifier}` {item.get('classifier_version')}, sample "
            f"n={item.get('sample_size')}, validated={item.get('validated')}"
        )
        for class_name, score in (item.get("per_class") or {}).items():
            lines.append(
                f"  - `{class_name}`: precision={score.get('precision')}, "
                f"recall={score.get('recall')}"
            )
    lines += ["", "## Limits", "", LIMITS]
    return "\n".join(lines) + "\n"


def write_go_coverage(
    report: GoCoverage,
    *,
    corpus_kind: str,
    corpus_digest: str,
    classifier_validation: dict[str, Any],
    reports_dir: Path | str | None = None,
) -> tuple[Path, Path]:
    root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    stem = f"go-coverage-{corpus_kind}-{corpus_digest[:12]}"
    json_path = assert_writable(root / f"{stem}.json")
    md_path = assert_writable(root / f"{stem}.md")
    payload = report.as_dict()
    payload.update(
        {
            "schema_version": 1,
            "corpus_kind": corpus_kind,
            "corpus_digest": corpus_digest,
            "classifier_validation": classifier_validation,
            "dlv_required_level": RECOMMENDED_REQUIRED_LEVEL,
            "dlv_classifier_version": DLV_CLASSIFIER_VERSION,
        }
    )
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(
        render_go_coverage(
            report,
            corpus_kind=corpus_kind,
            corpus_digest=corpus_digest,
            classifier_validation=classifier_validation,
        ),
        encoding="utf-8",
    )
    return json_path, md_path


__all__ = [
    "LIMITS",
    "MEASUREMENT_VERSION",
    "QUALIFIED_CHANGE_DEFINITION",
    "STAGES",
    "GoCoverage",
    "StageCoverage",
    "compute_go_coverage",
    "load_m14_validation",
    "render_go_coverage",
    "write_go_coverage",
]
