"""Join-key lint as a pure evaluator — constraint logic only, over an injected board parser.

**Nothing here parses the board.** Every parsed fact — rows, done-ness, phase tags, vocabulary
violations, plan placement, ledger joins, active markers — arrives through an injected reader that
is the existing `boardrows` authority in production.

That is not tidiness. C1-C5 all depend on knowing whether a row is DONE, and until 2026-08-25 that
was a regex over free prose which read a checkmark anywhere in the cell: rows 96 and 159 were live
and invisible, and C2 passed against a wrong open set. A second lifecycle vocabulary in the runtime
would reintroduce precisely that, which is why `boardrows` is shared with `board-restore.py` under
Sam's ruling that both directions "read the same rows through the reconstruction library ... so the
two directions cannot disagree about what a row is."

**Mechanical port.** The blocking set is the captured gate's, including the one asymmetry a port is
most likely to lose: C2-UNPLACED is reported and does NOT block.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from ..results import EvaluatorResult, Evidence

GATE_ID = "joinkey-lint"
#: 1.1.0 — C8 added (Barracuda decision 0304, 2026-09-01). A ninth blocking entry is a change to
#: the acceptance set, so the version moves with it.
GATE_VERSION = "1.1.0"

#: (label, blocks). Order and blocking-ness are the captured gate's `report(...)` calls.
#:
#: NINE entries for EIGHT constraints: C2 is reported twice, once as UNPLACED and once as MULTI,
#: and only MULTI blocks. C7 exists — the plan text and the D03 arbitration table both describe
#: this gate as aggregating "C1-C6", which understates it. C8 arrived in the captured gate on
#: 2026-09-01 (Barracuda decision 0304) and is ported here from the injected reader.
CONSTRAINTS: tuple[tuple[str, bool], ...] = (
    ("C1", True),            # board ids are unique
    ("C2-UNPLACED", False),  # an open row placed in NO plan phase — reported, never blocking
    ("C2-MULTI", True),      # an open row placed in more than one plan phase
    ("C3", True),            # a board tag disagreeing with the plan; the plan wins
    ("C4", True),            # a ledger JOIN cite naming a row that does not exist
    ("C5", True),            # an open row citing a DONE row as a live gate
    ("C6", True),            # ruling + execution outside decision 0274's ruled vocabulary
    ("C7", True),            # more than one ACTIVE pointer on the board
    ("C8", True),            # a board row that does not split into exactly seven cells
)

_DESCRIPTIONS = {
    "C1": "board ids are unique",
    "C2-UNPLACED": "open row placed in a plan phase",
    "C2-MULTI": "open row placed in EXACTLY one plan phase",
    "C3": "board tag agrees with the plan phase",
    "C4": "ledger JOIN cite names a real board row",
    "C5": "no open row cites a DONE row as a live gate",
    "C6": "ruling + execution are the ruled vocabulary (0274)",
    "C7": "at most one ACTIVE pointer",
    "C8": "every board row splits into exactly 7 cells",
}

#: Evidence is bounded by the schema at 32 items; leave room for the summary counts.
_MAX_DETAIL_EVIDENCE = 20


def _normalise_phase(tag: str) -> str:
    """Reduce a board phase tag exactly as the captured join-key gate does."""
    tag = (tag or "").strip().strip("*")
    tag = tag.split("·")[0]
    match = re.match(r"P[0-9]+", tag)
    if match:
        return match.group(0)
    if tag.startswith("corpus"):
        return "corpus"
    if tag.startswith("UNPLACED"):
        return "UNPLACED"
    return tag


def evaluate_joinkey_lint(
    event: Mapping[str, Any],
    *,
    event_id: str,
    board: Any,
    blocking: bool = True,
    gate_version: str = GATE_VERSION,
    now: Callable[[], float] | None = None,
) -> EvaluatorResult:
    """One verdict about one Stop event. Parses nothing; reads nothing; writes nothing."""
    import time

    clock = now or time.perf_counter
    started = clock()

    def finish(build, **kw):
        return build(
            gate_id=GATE_ID, gate_version=gate_version, event_id=event_id,
            duration_ms=max(0.0, round((clock() - started) * 1000, 4)), **kw
        )

    rows = list(board.rows())
    plan = dict(board.plan_phases())
    open_rows = [r for r in rows if not r.get("done")]
    board_ids = {str(r.get("row")) for r in rows}
    done_ids = {str(r.get("row")) for r in rows if r.get("done")}

    findings: dict[str, list[str]] = {label: [] for label, _ in CONSTRAINTS}

    # C1 — duplicate board ids.
    counts: dict[str, int] = {}
    for r in rows:
        rid = str(r.get("row"))
        counts[rid] = counts.get(rid, 0) + 1
    findings["C1"] = sorted((rid for rid, n in counts.items() if n > 1), key=str)

    # C4 — every ledger JOIN cite names a real board row.
    findings["C4"] = [
        f"ledger JOIN row {jr}: no such board row"
        for jr in board.ledger_joins() if str(jr) not in board_ids
    ]

    # C5 — an open row citing a DONE row as a live gate.
    findings["C5"] = []
    for row in open_rows:
        citations = row.get("gated_by") or []
        if isinstance(citations, (str, int)):
            citations = [citations]
        for citation in citations:
            if str(citation) in done_ids:
                findings["C5"].append(
                    f"row {row.get('row')} gated by row {citation} which is DONE"
                )

    # C6 — the ruled lifecycle vocabulary, read from the injected parser rather than re-derived.
    findings["C6"] = [
        f"row {row}: {field} outside the ruled vocabulary"
        for row, field, _value in board.vocabulary_violations()
    ]

    # C7 — at most one ACTIVE pointer.
    active = [str(r) for r in board.active_marker_rows()]
    if len(active) > 1:
        findings["C7"] = [
            f"{len(active)} rows carry the ACTIVE pointer ({', '.join(active[:8])}) — "
            "exactly one at a time, and zero when no row is being worked"
        ]

    # C8 — every board row splits into exactly SEVEN cells, counted by the host's own splitter.
    # None from the reader means the host's boardrows predates C8: unmeasured, recorded, not passed.
    cell_counts = board.cell_counts() if hasattr(board, "cell_counts") else None
    c8_unmeasured = cell_counts is None
    if not c8_unmeasured:
        findings["C8"] = [
            f"row {rid}: {count} cells, expected 7 — an unescaped `|` in a cell; write it `\\|`"
            for rid, count in cell_counts
            if count != 7
        ]

    # C2 and C3, per open row.
    for r in open_rows:
        rid = str(r.get("row"))
        phases = plan.get(rid, [])
        distinct = sorted(set(phases))
        if not phases:
            findings["C2-UNPLACED"].append(rid)
        elif len(distinct) > 1:
            findings["C2-MULTI"].append(f"{rid} -> {distinct}")
        board_tag = _normalise_phase(r.get("phase") or "")
        if board_tag and phases and len(distinct) == 1 and board_tag != distinct[0]:
            findings["C3"].append(f"row {rid}: board tag [{board_tag}] != plan {distinct[0]}")

    blocking_labels = [label for label, blocks in CONSTRAINTS if blocks and findings[label]]
    defect_count = sum(len(findings[label]) for label in blocking_labels)
    unplaced = len(findings["C2-UNPLACED"])

    evidence = [
        Evidence("state", f"open_rows={len(open_rows)}"),
        Evidence("state", f"unplaced_open_rows={unplaced}"),
        Evidence("state", f"blocking_defects={defect_count}"),
        Evidence("state", f"blocking_mode={'check' if blocking else 'report'}"),
    ]
    for label, _blocks in CONSTRAINTS:
        if findings[label] and len(evidence) < _MAX_DETAIL_EVIDENCE:
            evidence.append(Evidence("state", f"{label}_count={len(findings[label])}"))
    if c8_unmeasured:
        evidence.append(Evidence("state", "C8=unmeasured_host_boardrows_predates_c8"))

    if not defect_count:
        return finish(
            EvaluatorResult.passed,
            reason_code="joinkey_clean" if not unplaced else "joinkey_unplaced_only",
            evidence=tuple(evidence),
        )

    if not blocking:
        # Report mode finds the same defects and does not stop the turn. Recorded as a pass with
        # its count, never as a block: `--check` is what makes this gate an authority.
        return finish(
            EvaluatorResult.passed,
            reason_code="joinkey_findings_report_mode",
            evidence=tuple(evidence),
        )

    lines = [
        f"  {label} {_DESCRIPTIONS[label]} ({len(findings[label])}): "
        + "; ".join(str(item) for item in findings[label][:8])
        for label in blocking_labels
    ]
    return finish(
        EvaluatorResult.blocked,
        reason_code="joinkey_defects",
        remediation=(
            f"joinkey-lint: {defect_count} join-key defect(s) — fix before the turn ends.\n"
            + "\n".join(lines)
        ),
        evidence=tuple(evidence),
    )
