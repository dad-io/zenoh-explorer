"""The review gate as a pure evaluator — did this turn write code and commission no review?

Three semantics come from Sam's rulings and a port must not drift from any of them.

**The reviewer TIER is required** (2026-08-26). The captured gate's `if sub and sub not in ...`
short-circuited on an absent `subagent_type`, so the whitelist was skipped entirely and any Agent
dispatch carrying an attack word satisfied the gate — a one-line bypass of the control that
decision 0264 exists to remove.

**The brief WORDING stays generous** (2026-08-26). A stricter matcher was measured failing 6 of 9
realistic briefs, and a gate that blocks a genuine review over grammar gets rationalised past
(CLAUDE.md §10).

**A write only counts INSIDE the project root**, and a block already SETTLED must not re-fire on
the same write — both ported from the captured gate's post-2026-08-26 fix, after a scratchpad
heredoc under `/private/tmp/.../scratchpad` blocked a turn three times although nothing entered
the repo. Root scoping is `resolve_source_file_writes`'s own contract (see `writes.py`); the
settled-block rule is `current_turn_tool_calls(..., respect_judged_boundary=True)` — the boundary
`dlv-verify-gate.py` deliberately does NOT adopt, so this evaluator is the one caller that opts in.

Ordering matters as much as presence: a review of code that did not exist yet is not a review of
this code, so the review must come after the LAST write, compared on (line, block index).

Consumes R05's shared write resolver, so a source file written through Bash counts. Reads the
transcript and nothing else; it never prints, exits, stamps, or catches its own exception — a
missing transcript must not become apparent consent, and R13 adapts that under D04.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping

from ..results import EvaluatorResult, Evidence
from ..turns import current_turn_tool_calls
from ..writes import WriteResolution, project_root, resolve_source_file_writes

GATE_ID = "review-gate"
GATE_VERSION = "1.1.0"

#: The reviewer tiers that satisfy the gate. Ported verbatim.
REVIEW_AGENTS = ("opus-reviewer", "code-reviewer", "general-purpose", "claude", "fork")

#: Deliberately generous. Ported verbatim from the captured gate.
_ATTACK = re.compile(
    r"\b(?:attack|adversarial|refut|defect|bug|flaw|false positive|false negative|"
    r"wrong|incorrect|break|fail|vulnerab|regress|review|critique|hole|weakness)",
    re.I,
)

_DISPATCH_TOOLS = ("Agent", "Task")

REMEDIATION = (
    "review gate — this turn wrote code and commissioned no adversarial review of it. Your own "
    "checking is not the control here: on 2026-08-26 two Opus reviews each caught a defect this "
    "loop's self-checking had missed, one of them a gate that would have blocked the very commit "
    "landing it.\n\n"
    "Dispatch a reviewer on THIS turn's diff before ending the turn:\n"
    '  Agent(subagent_type="opus-reviewer", prompt=<brief>)\n\n'
    "The brief must ATTACK, not summarise: give it the diff (`git diff`), name the claim being "
    "made, tell it to find defects, false assumptions and missing cases, require `file:line` on "
    "every finding, and end with an anchor command (CLAUDE.md §2). Then VERIFY its cited lines "
    "yourself before acting on or relaying anything — an unverified citation is not evidence.\n\n"
    "It runs in parallel: dispatch it and keep working.\n\n"
    "This gate reads tool calls, not your reply: a write outside the project root is ignored; a "
    "write inside it needs a reviewer dispatched after it."
)


def _duration_ms(started: float) -> float:
    return max(0.0, round((time.perf_counter() - started) * 1000, 4))


def _is_review_dispatch(name: object, tool_input: object) -> bool:
    """One tool_use commissioning an adversarial review.

    The tier check is unconditional: an ABSENT subagent_type does not satisfy the gate, which is
    the bypass the captured version had.
    """
    if name not in _DISPATCH_TOOLS or not isinstance(tool_input, Mapping):
        return False
    subagent = tool_input.get("subagent_type")
    if not isinstance(subagent, str) or subagent not in REVIEW_AGENTS:
        return False
    prompt = tool_input.get("prompt")
    description = tool_input.get("description")
    brief = "{} {}".format(
        prompt if isinstance(prompt, str) else "",
        description if isinstance(description, str) else "",
    )
    return bool(_ATTACK.search(brief))


def _readable(path: object) -> bool:
    """Is there a file here to read?

    Checked WITHOUT opening it, so the check cannot itself raise.
    """
    import os

    return isinstance(path, str) and bool(path) and os.path.isfile(path)


def evaluate_review_gate(event: Mapping[str, object]) -> EvaluatorResult:
    """Evaluate the current turn and return exactly one immutable result."""
    started = time.perf_counter()
    event_id = event.get("event_id")

    if event.get("stop_hook_active"):
        return EvaluatorResult.not_applicable(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="stop_hook_active",
            evidence=(Evidence("event_field", "stop_hook_active=true"),),
            duration_ms=_duration_ms(started),
        )

    # PARITY with review-gate.py:121 — `if not transcript_path or not
    # os.path.isfile(transcript_path): return False`, which its main() turns into a PASS.
    # Recorded as not_applicable rather than pass: the schema distinguishes "examined the turn
    # and approved it" from "had nothing to examine", and claiming the former would assert a
    # review verdict this gate never reached. The DECISION is identical either way — neither
    # blocks — so this is a recording difference, not a policy one.
    transcript_path = event.get("transcript_path")
    if not transcript_path or not _readable(transcript_path):
        return EvaluatorResult.not_applicable(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="no_transcript",
            evidence=(Evidence("event_field", "transcript_path=unreadable"),),
            duration_ms=_duration_ms(started),
        )

    root = project_root(event)
    last_write = None
    last_write_resolution: WriteResolution | None = None
    last_rejected_path: str | None = None
    last_review = None
    for call in current_turn_tool_calls(
        event.get("transcript_path"), respect_judged_boundary=True
    ):
        resolution = resolve_source_file_writes(call.name, call.tool_input, root=root)
        if resolution:
            last_write = call
            last_write_resolution = resolution
        elif resolution.rejected_paths:
            # A source-extension write the resolver saw and excluded ONLY because it resolved
            # outside the project root — a scratchpad heredoc, a `/tmp` redirect. Tracked so a
            # pass caused by "nothing was written INSIDE the project" can still say what was
            # written outside it, without re-parsing the command here.
            last_rejected_path = resolution.rejected_paths[-1]
        if _is_review_dispatch(call.name, call.tool_input):
            last_review = call

    if last_write is None:
        if last_rejected_path is not None:
            return EvaluatorResult.passed(
                gate_id=GATE_ID,
                gate_version=GATE_VERSION,
                event_id=event_id,
                reason_code="no_source_write",
                evidence=(
                    Evidence("state", "source_write=false"),
                    Evidence("state", "write_in_project=false"),
                    Evidence("state", f"write_path={last_rejected_path}"),
                ),
                duration_ms=_duration_ms(started),
            )
        return EvaluatorResult.passed(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="no_source_write",
            evidence=(Evidence("state", "source_write=false"),),
            duration_ms=_duration_ms(started),
        )

    assert last_write_resolution is not None  # last_write is only set alongside its resolution
    write_path = (
        last_write_resolution.paths[-1] if last_write_resolution.paths else "opaque"
    )
    reviewed_after = last_review is not None and last_review.position >= last_write.position
    evidence = [
        Evidence("state", "source_write=true"),
        Evidence("state", f"write_position={last_write.line_number}.{last_write.block_index}"),
        Evidence("state", "write_in_project=true"),
        Evidence("state", f"write_path={write_path}"),
        Evidence("state", f"review_dispatched={'true' if last_review else 'false'}"),
    ]
    if last_review is not None:
        # Position and tier only — never the brief, which is model-authored prose and would put
        # free text into a result and therefore into telemetry.
        evidence.append(
            Evidence("state", f"review_position={last_review.line_number}."
                              f"{last_review.block_index}")
        )
        subagent = (last_review.tool_input or {}).get("subagent_type")
        if isinstance(subagent, str):
            evidence.append(Evidence("state", f"reviewer_tier={subagent}"))
        evidence.append(
            Evidence("state", f"review_after_write={'true' if reviewed_after else 'false'}")
        )

    if reviewed_after:
        return EvaluatorResult.passed(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="review_dispatched_after_write",
            evidence=tuple(evidence),
            duration_ms=_duration_ms(started),
        )

    return EvaluatorResult.blocked(
        gate_id=GATE_ID,
        gate_version=GATE_VERSION,
        event_id=event_id,
        reason_code="adversarial_review_missing",
        remediation=REMEDIATION,
        evidence=tuple(evidence),
        duration_ms=_duration_ms(started),
    )
