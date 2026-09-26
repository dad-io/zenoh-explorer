"""M10 — one DLV classifier for the lab, and the three-way depth report (roadmap 4.9) on it.

Three paths used to classify DLV independently: the ledger's verb-position check (any `dlv` verb,
so `dlv --help` verified a Go write), the eval scorer's regex over every string in a stream (so a
prose mention did), and the promoted runtime evaluator's approved-subcommand match. The runtime
is what Barracuda actually runs, so the lab now measures the same three-way distinction it
records in evidence — `real-session`, `word-only`, `none` — from one versioned function, with a
parity test pinning it to the runtime.

The depth report classifies every Go-editing turn by what happened AT OR AFTER its last Go write,
the gate's own scope. Counts only: no percentage is reported until M14 ground truth covers this
classifier (Addendum C: the 10% figure is not a compliance rate and must not be quoted as one).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bearhug.model import Evidence, Finding, Severity
from bearhug.paths import REPORTS_DIR, assert_writable
from bearhug.replay.shell import bindings, resolve_verb, segments, strip_env
from bearhug.replay.transcript import Turn, iter_events, iter_turns

#: Bumped whenever the classification below changes. Reports carry it so two runs can be compared.
#: 1.1.0 (2026-09-01, from the M14 labels): a newline is a segment boundary, a heredoc no longer
#: swallows what follows it, and the verb is resolved to a program (path basename, or a variable
#: bound in the same command) instead of requiring the literal token `dlv`.
DLV_CLASSIFIER_VERSION = "1.1.0"

#: The subcommands that start a debugger session. Mirrors the runtime evaluator's set.
DLV_SUBCOMMANDS = frozenset(("test", "debug", "exec", "attach", "trace", "core", "connect", "dap"))

#: The captured gate's own test — the word `dlv` at a token boundary, anywhere. Mirrors the
#: runtime's `_DLV_WORD`; a path like `hooks/dlv-verify-gate.py` is not preceded by a boundary
#: character and correctly does not match.
_DLV_WORD = re.compile(r"(?:^|[\s;&|])dlv\b")

CLASSES = ("real-session", "word-only", "none")


def classify_dlv_command(command: object) -> str:
    """`real-session` if an approved `dlv <subcommand>` sits in shell verb position; `word-only`
    if the captured gate's word match would fire but no real session does; else `none`."""
    if not isinstance(command, str) or not command:
        return "none"
    bound = bindings(command)
    for tokens in segments(command):
        tokens = strip_env(tokens)
        if len(tokens) < 2 or tokens[1] not in DLV_SUBCOMMANDS:
            continue
        program = resolve_verb(tokens[0], bound)
        # 1.1.0: the program is matched by BASENAME — `/Users/x/go/bin/dlv attach`, and a verb
        # written `"$DLV"` resolved through an assignment in the same command, are the shell
        # running dlv. An unbound `$DLV` is not guessed. M14 found four such sessions classed
        # word-only in the frozen corpus.
        if program is not None and program.rsplit("/", 1)[-1] == "dlv":
            return "real-session"
    if _DLV_WORD.search(command):
        return "word-only"
    return "none"


@dataclass(slots=True)
class DepthCounts:
    go_turns: int = 0
    real_session: int = 0
    word_only: int = 0
    none: int = 0
    #: A real session ran in the turn but only BEFORE its last Go write — counted under `none`
    #: and here, so the report can say how often the ordering rule, not the tool, was the miss.
    dlv_only_before_write: int = 0
    #: Turns whose Go write travelled through Bash (redirect, sed -i, gofmt -w, inline Python).
    bash_mediated_go_writes: int = 0
    #: Gate complaints attributed to dlv-verify-gate in these turns — a lower bound on activity.
    reminded: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(slots=True)
class DlvDepth:
    split_date: str
    windows: dict[str, DepthCounts] = field(default_factory=dict)
    turns_measured: int = 0
    date_range: tuple[str, str] = ("", "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "classifier_version": DLV_CLASSIFIER_VERSION,
            "split_date": self.split_date,
            "turns_measured": self.turns_measured,
            "date_range": list(self.date_range),
            "windows": {name: counts.as_dict() for name, counts in self.windows.items()},
        }


def _classify_turn(turn: Turn) -> tuple[bool, str, bool, bool]:
    """(saw_go_write, class at-or-after the last write, real session only before it,
    bash-mediated write seen)."""
    from bearhug.replay.ledger import _go_path, bash_writes_go

    last_write_index = -1
    bash_write = False
    for index, event in enumerate(turn.events):
        if event.kind != "tool_use":
            continue
        if event.name in ("Edit", "Write", "MultiEdit") and _go_path(event.payload):
            last_write_index = index
        elif event.name == "Bash" and bash_writes_go(event.payload.get("command", "")):
            last_write_index = index
            bash_write = True
    if last_write_index < 0:
        return False, "none", False, False

    best = "none"
    real_before = False
    for index, event in enumerate(turn.events):
        if event.kind != "tool_use" or event.name != "Bash":
            continue
        verdict = classify_dlv_command(event.payload.get("command", ""))
        if verdict == "none":
            continue
        if index >= last_write_index:
            if verdict == "real-session":
                best = "real-session"
            elif best == "none":
                best = "word-only"
        elif verdict == "real-session":
            real_before = True
    return True, best, real_before and best != "real-session", bash_write


def compute_dlv_depth(paths: list[Path], *, since: str | None = None) -> DlvDepth:
    from bearhug.replay.ledger import GATE_LANDED

    split = GATE_LANDED["dlv-verify-gate"]
    depth = DlvDepth(split_date=split, windows={"pre": DepthCounts(), "post": DepthCounts()})
    first_day = last_day = ""
    for path in paths:
        for turn in iter_turns(iter_events(path)):
            day = turn.timestamp[:10]
            if since and day < since:
                continue
            depth.turns_measured += 1
            if day and (not first_day or day < first_day):
                first_day = day
            if day > last_day:
                last_day = day
            saw_go, verdict, real_before, bash_write = _classify_turn(turn)
            if not saw_go:
                continue
            counts = depth.windows["post" if day >= split else "pre"]
            counts.go_turns += 1
            attribute = verdict.replace("-", "_")
            setattr(counts, attribute, getattr(counts, attribute) + 1)
            counts.dlv_only_before_write += int(real_before)
            counts.bash_mediated_go_writes += int(bash_write)
            counts.reminded += sum(1 for gate in turn.blocked_by if gate == "dlv-verify-gate")
    depth.date_range = (first_day, last_day)
    return depth


LIMIT = (
    "Counts, not rates: the classifier has no M14 ground truth yet, so no percentage is reported "
    "and none should be inferred (Addendum C: the earlier 10% figure is not a compliance rate). "
    "A turn is everything after a genuine human prompt or a subagent dispatch brief; the gate's "
    "own scope is the same boundary, but a `none` turn may be one the gate blocked and that was "
    "then corrected in a later turn. `word-only` is what the captured gate accepts today "
    "(R08B tightens it only after a ruling). The frozen corpus ends 2026-08-14, before the gate "
    "landed, so its post window is empty by construction; the live corpus is pruned (roadmap "
    "4.14), so its counts are a floor."
)


def build_dlv_depth_findings(
    depth: DlvDepth, *, snapshot_id: str, corpus_label: str
) -> list[Finding]:
    findings: list[Finding] = []
    for window, counts in depth.windows.items():
        bounds = (
            f"{depth.date_range[0]}..{depth.split_date}"
            if window == "pre"
            else f"{depth.split_date}..{depth.date_range[1]}"
        )
        findings.append(
            Finding(
                id=f"dlv-depth-{window}",
                check="DLV-DEPTH",
                severity=Severity.INFO,
                summary=(
                    f"{window.upper()}-gate ({bounds}): {counts.go_turns} Go-editing turns — "
                    f"{counts.real_session} real dlv session at or after the write, "
                    f"{counts.word_only} word-only, {counts.none} no dlv."
                ),
                snapshot=snapshot_id,
                evidence=(Evidence(run_id=f"{corpus_label} {bounds}"),),
                detail=(
                    f"classifier {DLV_CLASSIFIER_VERSION}; go_turns={counts.go_turns} "
                    f"real_session={counts.real_session} word_only={counts.word_only} "
                    f"none={counts.none} dlv_only_before_write={counts.dlv_only_before_write} "
                    f"bash_mediated_go_writes={counts.bash_mediated_go_writes} "
                    f"reminded={counts.reminded}; turns_measured={depth.turns_measured}"
                ),
                limit=LIMIT,
            )
        )
    return findings


def render_dlv_depth(depth: DlvDepth, *, corpus_label: str) -> str:
    lines = [
        f"# DLV depth — {corpus_label}",
        "",
        f"- classifier {DLV_CLASSIFIER_VERSION}; split at {depth.split_date} "
        f"(dlv-verify-gate landed); {depth.turns_measured} turns, "
        f"{depth.date_range[0]}..{depth.date_range[1]}",
        "",
        "| window | go turns | real-session | word-only | none | dlv only before write | "
        "bash-mediated writes | reminded |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for window, c in depth.windows.items():
        lines.append(
            f"| {window} | {c.go_turns} | {c.real_session} | {c.word_only} | {c.none} | "
            f"{c.dlv_only_before_write} | {c.bash_mediated_go_writes} | {c.reminded} |"
        )
    lines += ["", "## Limit", "", LIMIT]
    return "\n".join(lines) + "\n"


def write_dlv_depth(
    depth: DlvDepth,
    *,
    corpus_label: str,
    corpus_digest: str,
    snapshot_id: str,
    reports_dir: Path | str | None = None,
) -> tuple[Path, Path]:
    root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    kind = corpus_label.split(":")[0]  # CorpusSelection.label is "<kind>:<digest12>"
    safe_snapshot = snapshot_id.replace("@", "-at-").replace("/", "-")
    stem = f"dlv-depth-{kind}-{corpus_digest[:12]}-{safe_snapshot}"
    json_path = assert_writable(root / f"{stem}.json")
    md_path = assert_writable(root / f"{stem}.md")
    payload = depth.as_dict()
    payload.update(
        {"corpus_label": corpus_label, "corpus_digest": corpus_digest, "snapshot_id": snapshot_id,
         "limit": LIMIT}
    )
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_dlv_depth(depth, corpus_label=corpus_label), encoding="utf-8")
    return json_path, md_path


__all__ = [
    "CLASSES",
    "DLV_CLASSIFIER_VERSION",
    "DLV_SUBCOMMANDS",
    "DepthCounts",
    "DlvDepth",
    "build_dlv_depth_findings",
    "classify_dlv_command",
    "compute_dlv_depth",
    "render_dlv_depth",
    "write_dlv_depth",
]
