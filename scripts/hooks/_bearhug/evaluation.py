"""R13 — run the whole evaluator set, adapt failures under D04, and decide nothing.

Four properties, each with the reason it exists rather than as a rule to follow:

**No short-circuit on a block.** D03's arbitration is a pure function of the FULL result set. If
the first block ended the loop, arbitration would be choosing among whatever happened to run first
and the combined remediation would silently lose the rest — which is the ARBITRATION defect
restated, not solved.

**An exception becomes `error`.** Never `pass`, which is crash-reads-as-consent written into the
schema, and never `block`, which would attribute a defect the gate never found. D04's ruled policy
then decides whether that error may stop the turn; the recorded verdict is `error` either way.

**Only this module may record `not_reached`.** An evaluator cannot truthfully emit it — it does not
know the evaluator set exists — and `EvaluatorResult` rejects it by name, so the monopoly is
structural rather than conventional.

**It selects no decision.** Arbitration is R14 and the Stop response is R15. A run that chose would
be a second authority beside them.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .registry import CoordinatorInputError, EvaluatorEntry, invoke
from .results import EvaluatorResult, Evidence

#: Sam's D04 ruling, per gate. Duplicated from failure-policy-table.v1.json because the runtime is
#: stdlib-only and cannot read the lab's JSON at evaluation time. A lab test compares the two, since
#: duplication is only acceptable while something proves the copies agree — otherwise the runtime
#: would apply a policy nobody ruled.
FAILURE_POLICY: dict[str, str] = {
    "response-shape": "fail_open",
    "dlv-verify-gate": "fail_closed",
    "task-durability": "fail_closed",
    "joinkey-lint": "fail_open",
    "review-gate": "fail_closed",
}

#: A gate the ruled table does not know. Fail-CLOSED deliberately: the permissive default is the
#: one that lets an unverified change through silently, and an unruled gate is precisely the case
#: where nobody has decided that is acceptable.
UNKNOWN_GATE_POLICY = "fail_closed"

#: D04 rules a coordinator input failure fail-open. It matches the pre-coordinator behaviour and a
#: block there gives the model nothing it can act on.
COORDINATOR_INPUT_POLICY = "fail_open"


class CoordinatorFatal(Exception):
    """A condition that makes evaluating the REST of the set impossible.

    Distinct from an evaluator raising: that is one gate's failure and the others still run. This
    is the only thing that may leave later slots `not_reached`.
    """


@dataclass(frozen=True, slots=True)
class SlotOutcome:
    """One planned slot and what became of it."""

    ordinal: int
    gate_id: str
    #: "evaluated" or "not_reached". The telemetry schema's vocabulary, not a verdict.
    execution_state: str
    result: EvaluatorResult | None = None
    not_reached_reason: str | None = None
    #: Set only when `result` is an error, recording WHICH policy was applied — the policy decision
    #: itself is evidence, per D04.
    failure_policy_applied: str | None = None
    #: True when this error must STOP the turn under D04's ruling. Recording the policy without a
    #: field that acts on it is what let a fail-closed gate crash and pass silently: the value was
    #: written here and read nowhere, so the ruling was a claim rather than a behaviour.
    blocks_the_turn: bool = False
    exception_type: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    """The complete ordered result set for one Stop event. It contains no decision."""

    event_id: str
    outcomes: tuple[SlotOutcome, ...]
    coordinator_error: str | None = None
    coordinator_failure_policy: str | None = None


def _policy_for(gate_id: str) -> str:
    return FAILURE_POLICY.get(gate_id, UNKNOWN_GATE_POLICY)


def _error_result(entry: EvaluatorEntry, event_id: str, exc: BaseException,
                  duration_ms: float) -> EvaluatorResult:
    """An evaluator that could not evaluate. The remediation names the gate, not the exception.

    The exception TYPE is evidence; its message is not — a message can carry a path, a command, or
    a fragment of source, none of which may reach a result and therefore telemetry.
    """
    return EvaluatorResult.errored(
        gate_id=entry.gate_id,
        gate_version=entry.gate_version,
        event_id=event_id,
        reason_code="evaluator_exception",
        remediation=(
            f"{entry.gate_id} could not evaluate this turn. Its check did not run, so this turn "
            "is unverified by it — re-run the turn, or record explicitly that the check was "
            "skipped."
        ),
        evidence=(
            Evidence("state", f"exception_type={type(exc).__name__}"),
            Evidence("state", f"failure_policy={_policy_for(entry.gate_id)}"),
        ),
        duration_ms=duration_ms,
    )


def _unreached(entry: EvaluatorEntry, reason: str, *,
               exception_type: str | None = None) -> SlotOutcome:
    """A slot that produced NO verdict, carrying the ruling that decides what that means.

    Round 4's secondary finding. Three branches filed a slot `not_reached` and left
    `blocks_the_turn` at its default False, so a fail-closed gate the coordinator could not
    dispatch — a missing declared dependency, a fatal condition, or a fatal in an EARLIER slot —
    let the turn end in silence. Round 2 fixed the fourth branch, the evaluator that raises, and
    the ruling was applied in exactly the one place it was tested.

    A `not_reached` slot has no result to convert into a block: the gate never looked, so there is
    nothing to attribute to it. `blocks_the_turn` therefore means something different here than on
    an errored slot, and the coordinator renders it as its OWN fault. That distinction is the point
    — blaming a gate for a dispatch failure it was never reached by would put a defect in a
    component that has none.
    """
    return SlotOutcome(
        ordinal=entry.ordinal,
        gate_id=entry.gate_id,
        execution_state="not_reached",
        not_reached_reason=reason,
        failure_policy_applied=_policy_for(entry.gate_id),
        blocks_the_turn=(_policy_for(entry.gate_id) == "fail_closed"),
        exception_type=exception_type,
    )


def evaluate_set(
    event: Mapping[str, Any],
    *,
    registry: Iterable[EvaluatorEntry],
    dependencies: Mapping[str, Any] | None = None,
    event_id: Any = None,
) -> EvaluationRun:
    """Evaluate every planned slot and return the complete ordered set.

    Chooses nothing. R14 arbitrates over what this returns.
    """
    entries = tuple(registry)

    if not entries:
        # coordinator.py's own comment: "a fail-open registry fault would make a promotion that
        # registered NO evaluators look exactly like a clean run — every turn passing because
        # nothing was evaluated." It described the defect precisely and did not implement the
        # ruling; `arbitrate([])` returned pass, which is honest about what it SAW. The layer that
        # has to notice it saw nothing is this one, because it is the only one that knows a slot
        # was PLANNED.
        return EvaluationRun(
            event_id=str(event_id if event_id is not None else uuid.uuid4().hex),
            outcomes=(),
            coordinator_error="empty_evaluator_set",
            coordinator_failure_policy="fail_closed",
        )

    # The coordinator's own inputs first. A fault here is the coordinator's, never a gate's — the
    # fail-closed gates would otherwise be blamed for it and would block the turn.
    if event_id is None:
        event_id = uuid.uuid4().hex
    try:
        from .registry import _validate_event_id

        _validate_event_id(event_id)
    except CoordinatorInputError:
        return EvaluationRun(
            event_id=str(event_id),
            outcomes=tuple(
                SlotOutcome(
                    ordinal=entry.ordinal,
                    gate_id=entry.gate_id,
                    execution_state="not_reached",
                    not_reached_reason="coordinator input failure; no evaluator was run",
                )
                for entry in entries
            ),
            coordinator_error="coordinator_input_parse_failure",
            coordinator_failure_policy=COORDINATOR_INPUT_POLICY,
        )

    outcomes: list[SlotOutcome] = []
    fatal: str | None = None

    for entry in entries:
        if fatal is not None:
            outcomes.append(_unreached(entry, fatal))
            continue

        started = time.perf_counter()
        try:
            result = invoke(entry, event, event_id=event_id, dependencies=dependencies)
        except CoordinatorFatal as exc:
            # The only condition that may leave later slots unreached.
            fatal = f"coordinator-fatal condition at {entry.gate_id}: {type(exc).__name__}"
            outcomes.append(_unreached(entry, fatal, exception_type=type(exc).__name__))
            continue
        except (CoordinatorInputError, ValueError) as exc:
            # Raised while ASSEMBLING the call — a missing declared dependency, say. The gate never
            # ran, so this is the coordinator's and must not be recorded against the gate.
            outcomes.append(_unreached(
                entry, f"coordinator could not dispatch {entry.gate_id}: {exc}",
                exception_type=type(exc).__name__,
            ))
            continue
        except BaseException as exc:  # noqa: BLE001 — one gate's failure, adapted under D04
            duration = max(0.0, round((time.perf_counter() - started) * 1000, 4))
            policy = _policy_for(entry.gate_id)
            outcomes.append(SlotOutcome(
                ordinal=entry.ordinal, gate_id=entry.gate_id, execution_state="evaluated",
                result=_error_result(entry, event_id, exc, duration),
                failure_policy_applied=policy,
                blocks_the_turn=(policy == "fail_closed"),
                exception_type=type(exc).__name__,
            ))
            continue

        outcomes.append(SlotOutcome(
            ordinal=entry.ordinal, gate_id=entry.gate_id,
            execution_state="evaluated", result=result,
        ))

    return EvaluationRun(event_id=event_id, outcomes=tuple(outcomes))
