"""R12 — the ordered evaluator registry: data, plus the one way to call a gate.

**Data, not import order.** Implicit import order is not a decision anyone approved, and it changes
when a module is renamed or an import is reordered to satisfy a linter. The population, the order,
and the ordinals are declared here and cross-checked against `arbitration-table.v1.json`, whose
tie-break `(evaluator_ordinal, gate_id, reason_code)` depends on them.

**One invocation path.** The five evaluators have three dependency shapes — none, tasks+repo+
transcript, board — and R07/R08/R11 read `event_id` from the event while R09/R10 take it as a
keyword. That divergence is recorded in the failure-policy table's `coordinator_preconditions`.
The registry declares each shape as data and exposes a single `invoke`, so the coordinator cannot
call two ways and get two attributions for one mistake.

**It enumerates; it does not run the set, arbitrate, or render.** Only something that knows the set
exists may record a slot as `not_reached`, which is why an evaluator may never emit it — it does not
know the set exists. Executing the set is R13; arbitration is R14; the Stop response is R15.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .evaluators import (
    evaluate_dlv_verification,
    evaluate_joinkey_lint,
    evaluate_response_shape,
    evaluate_review_gate,
    evaluate_task_durability,
)

#: After promotion barracuda registers THREE Stop commands, not one. D02 struck the
#: "ONE registered Stop command" claim as false.
STOP_REGISTRATIONS_AFTER_PROMOTION = 3

#: The coordinator holds sole DECISION authority, which is a different claim from sole
#: registration and is the one D02 preserved.
SOLE_DECISION_AUTHORITY = True


class CoordinatorInputError(Exception):
    """The coordinator's own inputs are invalid, so no evaluator was run.

    Its own type, deliberately: R13 must file this as `coordinator_input_parse_failure` — which
    D04 rules FAIL-OPEN — and never as an evaluator `error`. dlv-verify-gate is ruled FAIL-CLOSED
    and raises inside itself on a bad `event_id`, so without this distinction a coordinator bug
    would block the turn and attribute the block to a gate that did nothing wrong.
    """


@dataclass(frozen=True, slots=True)
class EvaluatorEntry:
    """One evaluator slot, declared."""

    ordinal: int
    gate_id: str
    call: Callable[..., Any]
    #: Names of the injected dependencies this evaluator needs, and nothing more.
    requires: tuple[str, ...]
    #: How this evaluator receives the event id: inside the event mapping, or as a keyword.
    #: Recorded rather than normalised, because the two shapes exist for defensible reasons and
    #: `invoke` is what makes the caller's experience uniform.
    event_id_via: str
    gate_version: str
    notes: str = ""


def build_registry(entries: Sequence[EvaluatorEntry]) -> tuple[EvaluatorEntry, ...]:
    """Validate the invariants at construction rather than assuming them.

    A duplicate ordinal or a gap would make D03's tie-break non-total, and arbitration's
    determinism would be a claim rather than a fact.
    """
    ordered = tuple(sorted(entries, key=lambda entry: entry.ordinal))
    ordinals = [entry.ordinal for entry in ordered]
    if ordinals != list(range(len(ordered))):
        raise ValueError(
            f"evaluator ordinals must be dense and zero-based, got {ordinals}"
        )
    gate_ids = [entry.gate_id for entry in ordered]
    if len(set(gate_ids)) != len(gate_ids):
        raise ValueError(f"duplicate gate_id in the registry: {gate_ids}")
    for entry in ordered:
        if entry.event_id_via not in ("event", "keyword"):
            raise ValueError(f"{entry.gate_id}: unknown event_id_via {entry.event_id_via!r}")
    return ordered


REGISTRY: tuple[EvaluatorEntry, ...] = build_registry([
    EvaluatorEntry(
        ordinal=0, gate_id="response-shape", call=evaluate_response_shape,
        requires=(), event_id_via="event", gate_version="1.4.0",
        notes=(
            "1.1.0 (Round 13): an unreadable transcript is `no_transcript`, not_applicable — the "
            "guard the other three transcript-reading gates already carried. Verdict parity kept. "
            "1.2.0 (Round 14): 0135/R07 ruled — Sam, 2026-09-03. LONG_CHARS rises from 1,500 to "
            "6,000; a turn-ending message under 6,000 characters with no question now passes. "
            "The two-ask rule and the stop-request exemption are unchanged. This is a deliberate "
            "divergence from the captured legacy hook, which keeps its own 1,500-char threshold. "
            "1.3.0 (2026-09-15): `_prose_lines`'s fence reader now tracks the marker's character "
            "and length instead of toggling a boolean on any ``` line, fixing three measured "
            "defects — an invisible ~~~ fence, an unterminated fence swallowing trailing asks, "
            "and a ``` nested in a ```` block closing the outer fence early. Verdict-affecting "
            "only for messages containing those shapes; see response_shape.py's module docstring. "
            "1.4.0 (Round 17, 2026-09-15): three more CommonMark deviations closed in the same "
            "reader — a marker indented 4+ spaces no longer opens a fence (CommonMark reads that "
            "as an indented code block); a backtick fence's info string containing a backtick no "
            "longer toggles a fence (an inline code span or a stray ```json``` line is now "
            "ordinary text; tilde fences are unaffected); and a closer carrying an info string no "
            "longer closes the fence (only trailing whitespace after the marker is a valid "
            "closer). The unterminated-fence-ends-at-EOF behaviour from 1.3.0 is unchanged. "
            "Verdict-affecting only for messages containing those three shapes."
        ),
    ),
    EvaluatorEntry(
        ordinal=1, gate_id="dlv-verify-gate", call=evaluate_dlv_verification,
        requires=(), event_id_via="event", gate_version="1.2.0",
        notes=(
            "1.2.0 (Round 14): R08C is RULED, not deferred — Sam, 2026-09-03. A resolved dlv "
            "program (a path whose basename is `dlv`, or a `$NAME` bound to such a path in the "
            "same command) now PASSES on its own; before this the runtime BLOCKED these for "
            "parity with the captured gate's word-boundary regex. R08B — tightening the "
            "remaining word-only match to require a real subcommand — is still DEFERRED: it "
            "needs the measured false-positive rate and is tracked with G05's proof-level "
            "protocol. This evaluator therefore still PASSES on a word-only match, and says so "
            "in its evidence."
        ),
    ),
    EvaluatorEntry(
        ordinal=2, gate_id="task-durability", call=evaluate_task_durability,
        requires=("tasks", "repo", "transcript_lines", "transcript_readable"),
        event_id_via="keyword",
        gate_version="1.4.0",
        notes=(
            "Reads the LIVE task store through an injected reader; never the transcript. "
            "1.1.0 (Round 15): PHASE_TAG's `#<row> · <phase>` form now also accepts the phase "
            "token wrapped in its own brackets — `#236 · [corpus]`, `#236 · [P1]`, "
            "`#236 · [P5·hold]` — mirrored from the captured gate's own fix. An untagged subject "
            "still blocks. "
            "1.2.0: decision 0137's board route now reaches the undetected-absence case too — "
            "a touched board passes as `board_only` even when no ToolSearch miss or withdrawal "
            "notice ever fired, which is what a client offering no task tool at all leaves "
            "behind. `board_only_tool_absent` (the detected-absence pass) is unchanged; the "
            "`task_list_empty` remediation now names the board route as well. "
            "1.3.0: a review found `board_only` reachable through `_board_touched`'s raw-line "
            "scan with no board write at all (an edit to another file naming the path, a file "
            "named DASHBOARD.md, a failed or denied board edit, a Bash redirect into a "
            "same-named scratch copy elsewhere). `board_only` now requires `_board_written`'s "
            "structural check instead — an edit tool's own `file_path` resolving to the board, "
            "or a Bash write to the full board path, confirmed by a non-error correlated "
            "tool_result. `board_only_tool_absent` keeps `_board_touched` exactly as ported. "
            "1.4.0: a further review found the 1.3.0 regex still read command TEXT, not effect "
            "(a heredoc body, a quoted string, a `.bak`/`~`/`-old` variant, a redirect into a "
            "scratch copy elsewhere), so `_board_written` now asks the shared write resolver "
            "(`resolve_any_file_writes`, the one `review_gate.py` already uses) with `git "
            "checkout`/`git restore` targeting the board excluded (a revert records no new "
            "row). The remediation no longer scopes the board route to a client with no task "
            "tool: a real board write satisfies it in any client, task preferred where the "
            "tool exists. `board_only`'s evidence now reads `board_written`, the fact that "
            "actually decided it."
        ),
    ),
    EvaluatorEntry(
        ordinal=3, gate_id="joinkey-lint", call=evaluate_joinkey_lint,
        requires=("board",), event_id_via="keyword", gate_version="1.1.0",
        notes=(
            "Registered with --check, which is what makes it an authority rather than a report. "
            "1.1.0 ports C8 (Barracuda decision 0304): every board row splits into seven cells."
        ),
    ),
    EvaluatorEntry(
        ordinal=4, gate_id="review-gate", call=evaluate_review_gate,
        requires=(), event_id_via="event", gate_version="1.1.0",
        notes=(
            "1.1.0 (Round 15): mirrors the captured gate's post-2026-08-26 fix. A Bash or "
            "Edit/Write/MultiEdit write only counts INSIDE the project root (a scratchpad heredoc "
            "or a `/tmp` redirect is not a code write); a block a Stop already judged does not "
            "re-fire on the same write after its 'Stop hook feedback:' entry is re-injected and "
            "the turn continues past an injected `<task-notification>`. The remediation text no "
            "longer claims a reply can settle the gate. New evidence: `write_in_project` and "
            "`write_path`, recorded alongside every write-position result."
        ),
    ),
])

#: D02 Option D. These stay registered at Stop and are NOT evaluators, so their absence from
#: REGISTRY must never be read as absence from the Stop event.
RETAINED_EXTERNAL_REGISTRATIONS: tuple[dict[str, Any], ...] = (
    {
        "command": "graft-hooks.cjs stop",
        "order": 0,
        "is_evaluator": False,
        "runs_before_the_coordinator": True,
        "non_blocking_evidence": "PROVEN by reading, 2026-08-31",
        "why": (
            "Its wrapper defers to an external npm package behind a bare `.catch`, so no RUN can "
            "establish its Stop contract — but READING can, and a Barracuda-owned session did: "
            "hooks.js has zero occurrences of process.exit, block, decision, stopReason or "
            "exitCode, and its stop branch is `handleStop(dir); return;`. The 'accepted, not "
            "proven' wording this field carried was refuted in round 1 and still stood in round "
            "3, disagreeing with manifest.json and the ADR inside one package."
        ),
    },
    {
        "command": "memex-hook.sh stop",
        "order": 1,
        "is_evaluator": False,
        "runs_before_the_coordinator": True,
        "non_blocking_evidence": "verified from the snapshot",
        "why": (
            "It prints one plain-text line and exits 0, so it holds no Stop authority. It is also "
            "stateful across turns, which a pure evaluator could not reproduce without writing."
        ),
    },
)


def resolve(gate_id: str) -> EvaluatorEntry:
    """The entry for one gate id. Raises on an unknown id rather than returning None."""
    for entry in REGISTRY:
        if entry.gate_id == gate_id:
            return entry
    raise KeyError(f"unknown evaluator gate_id: {gate_id!r}")


def _validate_event_id(event_id: Any) -> str:
    if not isinstance(event_id, str) or not event_id:
        raise CoordinatorInputError(
            f"event_id must be a non-empty string, got {type(event_id).__name__}: {event_id!r}. "
            "The protocol requires the coordinator to create it once per event; this is a "
            "coordinator input failure, not a gate failure."
        )
    return event_id


def invoke(
    entry: EvaluatorEntry,
    event: Mapping[str, Any],
    *,
    event_id: Any,
    dependencies: Mapping[str, Any] | None = None,
) -> Any:
    """Call one evaluator through the single supported path.

    Validates the coordinator's own inputs FIRST, so a coordinator fault never reaches an
    evaluator and can never be recorded against a gate. Supplies exactly the dependencies the
    entry declares — a gate handed something it never declared would be silently coupled to it.
    """
    event_id = _validate_event_id(event_id)
    supplied = dict(dependencies or {})

    missing = [name for name in entry.requires if name not in supplied]
    if missing:
        raise ValueError(
            f"{entry.gate_id} requires {', '.join(missing)}, which the coordinator did not "
            "supply; evaluating against a silently absent reader would be worse than failing here"
        )

    kwargs = {name: supplied[name] for name in entry.requires}
    if entry.event_id_via == "keyword":
        kwargs["event_id"] = event_id
        return entry.call(event, **kwargs)

    # `event`-carried: the evaluator reads event["event_id"] itself. A copy, so the coordinator's
    # own event mapping is never mutated by dispatch.
    return entry.call({**event, "event_id": event_id}, **kwargs)


def planned_slots() -> tuple[dict[str, Any], ...]:
    """The complete planned set, enumerable BEFORE anything runs.

    R12's Done condition. R13 needs this to mark a slot `not_reached` truthfully, which only
    something that knows the whole set can do.
    """
    return tuple(
        {"ordinal": entry.ordinal, "gate_id": entry.gate_id,
         "gate_version": entry.gate_version, "requires": entry.requires}
        for entry in REGISTRY
    )
