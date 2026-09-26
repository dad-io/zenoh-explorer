"""R15 — the sole Claude Code Stop renderer, and the one invocation that ties the runtime together.

The Stop contract is a BYTE contract: a hook blocks by writing one JSON object to stdout and
exiting 0. A plain `sys.exit(1)` is a NON-blocking error the turn ignores — measured on
2026-08-21, when joinkey-lint ran at every Stop with `--check` and never blocked, so a duplicate
board id sat C1-red for a whole session. This module therefore returns the exact bytes and the exit
status, and its tests assert both.

Serialization is isolated from arbitration. R14 composed the remediation; a renderer that rewrote
it would be a second authority beside the table that produced it.

D02 ruled Option D, so there is no advisory channel to render. graft and memex remain separately
registered and speak for themselves; this module speaks only for the coordinator's five.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .arbitrate import Decision, arbitrate
from .evaluation import evaluate_set
from .registry import REGISTRY
from .results import EvaluatorResult
from .stamp_projection import project
from .telemetry import build_record
from .telemetry_store import append as append_telemetry

#: D04 ruled the coordinator's registry fault FAIL-CLOSED, with a remediation that names rollback.
#: A coordinator that does not know its own gate set cannot honestly claim any gate passed, and a
#: fail-open registry fault would make a promotion that registered NO evaluators look exactly like
#: a clean run — every turn passing because nothing was evaluated.
#: Quoted VERBATIM from the ruled table's `user_facing_remediation`, with one sentence added to
#: name the coordinator as the faulting component — 9767caf's concern at the rendering layer. A
#: lab test pins the quoted half against the table so a paraphrase cannot drift in.
COORDINATOR_ERROR_REMEDIATION = (
    "The Stop coordinator is misconfigured and cannot evaluate this turn. Roll back the "
    "coordinator registration; do not rely on Stop gating until it is restored."
    "\n\nThis is a coordinator fault, not a gate's: no gate result for this turn is trustworthy."
)


@dataclass(frozen=True, slots=True)
class Rendered:
    """Exactly what the hook writes and exits with."""

    stdout: bytes
    exit_code: int


#: The stamp verdict for each outcome, in the vocabulary the captured gates actually write.
#:
#: Uppercase first token, per D05. Writing `block` instead of `BLOCKED` would fall through
#: `automation-status.sh`'s `BLOCKED)` arm to `*) UP` and show the gate HEALTHY at the moment it
#: blocked the turn — the 2026-08-13 defect stamping was added to end.
#:
#: The per-gate strings live in `stamp_projection`, derived from the captured gates and pinned to
#: them by a lab test. They were computed HERE until 2026-08-31, by collapsing every pass to
#: `PASS`, every not-applicable to `SKIP-<reason_code>` and every crash to `CRASH-<Type>` — which
#: preserved none of the three anomalies the ruled table names as must-preserve, while ADR section
#: 8 asserted in prose that it preserved all of them.
def _stamp_verdict(outcome: Any) -> str | None:
    """The string to stamp, or None to write nothing.

    None is a real outcome, not a failure: three (gate, reason_code) pairs stamp nothing today,
    and an unknown pairing is projected as nothing rather than as a guess.
    """
    return project(outcome.gate_id, outcome.result, outcome.exception_type)


def _write_stamps(outcomes: Any, root: Any) -> None:
    """The projection D05 ruled: coordinator-owned, best-effort, AFTER the decision.

    Every failure path is swallowed, matching `stamp.sh`'s own contract that it "NEVER fails the
    caller". A blocking gate's exit code must not depend on observability.
    """
    try:
        import os
        import time
        from pathlib import Path

        directory = Path(root) / ".automation-stamps"
        directory.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        for outcome in outcomes:
            verdict = _stamp_verdict(outcome)
            if verdict is None:
                # The captured gate writes nothing on this outcome, so neither does the
                # projection: writing SKIP here would CLEAR a stale BLOCKED, and whether the
                # dashboard should forget a block is a decision rather than a detail. See
                # stamp_projection.SILENT — ruled 2026-09-02: silence never clears a stale stamp.
                continue
            target = directory / outcome.gate_id
            temporary = directory / f".{outcome.gate_id}.tmp"
            temporary.write_text(f"{now} {verdict}\n", encoding="utf-8")
            # Atomic, so a reader never sees a half-written verdict.
            os.replace(temporary, target)
    except BaseException:  # noqa: BLE001 - observability may never change a verdict
        # BaseException, not Exception. Barracuda decision 0277, accepted and executed: "A stamp
        # can never change its caller's exit code." Once the adapter began rendering a block on
        # its own BaseException, a MemoryError or an interrupted Stop hook raised HERE passed
        # through an `except Exception` and became a coordinator-fault block — a turn every gate
        # passed, stopped by a failed write. The adapter's own comment names the width:
        # "`except Exception` is exactly the width that let round 4's defect through one layer
        # down." Same width, one layer further down, in the two functions whose entire contract is
        # that they cannot affect the outcome.
        return


def _write_telemetry(run_result: Any, event: Any, root: Any) -> None:
    """One record per evaluator slot, under `root`. Best-effort; a failure is not an authority.

    `root` is the telemetry root ITSELF, not a directory to hang `telemetry/` off. It used to be
    the latter, which is how `<repo>/telemetry/` came to exist in the target's working tree.
    """
    try:
        from pathlib import Path

        _expire_old_records(root)
    except BaseException:  # noqa: BLE001 - observability may never change a verdict
        return
    try:
        raw = json.dumps(dict(event), sort_keys=True).encode("utf-8")
    except BaseException:  # noqa: BLE001 - see _write_stamps on the width
        raw = b""
    for outcome in run_result.outcomes:
        try:
            record = build_record(
                outcome,
                event_id=run_result.event_id,
                session_id=str(event.get("session_id") or "unknown"),
                event_name=str(event.get("hook_event_name") or "Stop"),
                raw_input=raw,
            )
            append_telemetry(record, root=Path(root))
        except BaseException:  # noqa: BLE001 - never an authority; see _write_stamps on the width
            continue


def _expire_old_records(root: Any) -> None:
    """Apply the protocol's 30-day retention, at most once per day.

    `telemetry_store.expire()` had ZERO callers, so retention was declared and never ran — a
    Barracuda-owned session found it as the second limb of the telemetry finding. Called from the
    write path because that is the only code that runs in production; a scan on every Stop would
    be waste, so a stamp file records the last sweep and the whole thing is skipped for the rest
    of the day.

    Every failure is swallowed: retention is observability about observability, and it may not
    change a verdict any more than the write it precedes.
    """
    try:
        from datetime import UTC, datetime
        from pathlib import Path

        from .telemetry_store import expire_store

        marker = Path(root) / ".last-expiry"
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == today:
            return
        expire_store(root)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{today}\n", encoding="utf-8")
    except BaseException:  # noqa: BLE001 - never an authority
        return


def _as_blocking(result: Any) -> Any:
    """The same result, presented to arbitration as a block.

    Only for an `error` from a gate D04 ruled fail-closed. The evaluator's own recorded verdict is
    untouched — telemetry still says `error`, because the gate did not find a defect, it failed to
    look. What changes is whether the coordinator lets the turn end.
    """
    return EvaluatorResult(
        gate_id=result.gate_id,
        gate_version=result.gate_version,
        event_id=result.event_id,
        applicability="applicable",
        verdict="block",
        reason_code=result.reason_code,
        remediation=result.remediation,
        evidence=result.evidence,
        duration_ms=result.duration_ms,
    )


def _stamp_root(event: Mapping[str, Any]) -> Any:
    """Where `.automation-stamps/` goes: beside the repository the captured gates stamp into.

    `CLAUDE_PROJECT_DIR` FIRST, matching `stamp.sh:26` — `_root=${CLAUDE_PROJECT_DIR:-$(git
    rev-parse --show-toplevel)}`. This read `event["cwd"]` first until 2026-09-01, so the two
    copies of "where does observability go" disagreed about precedence, which a Barracuda-owned
    session noticed while reading the fallback.
    """
    import os
    from pathlib import Path

    explicit = os.environ.get("CLAUDE_PROJECT_DIR")
    if explicit:
        return Path(explicit)

    start = Path(event.get("cwd") or os.getcwd()).resolve()
    if start.is_file():
        start = start.parent
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


def _telemetry_root(event: Mapping[str, Any]) -> Any:
    """Where telemetry goes: `<repo>/.bearhug/telemetry/v1`, project-based by location (D08).

    Resolved by `telemetry_store.default_root()`, the one authority for the protocol's declared
    path. Still a separate root from the stamps: stamps are the captured gates' legacy projection
    beside the repository, telemetry is the coordinator's own record under an ignored directory.

    Handed the same repository `_stamp_root` resolves, so the records land in the tree the gates
    actually ran against rather than whatever directory the process happened to start in.
    """
    from .telemetry_store import default_root

    return default_root(_stamp_root(event))


def render(decision: Decision) -> Rendered:
    """Turn one arbitrated decision into the Stop hook's bytes.

    A pass writes NOTHING. An empty object would not be silence — a reader could take `{}` for a
    malformed decision, and any stdout byte on a pass is a decision nobody made.
    """
    if decision.decision == "pass":
        return Rendered(stdout=b"", exit_code=0)

    if decision.decision == "coordinator_error":
        reason = COORDINATOR_ERROR_REMEDIATION
        if decision.error_reason:
            reason = f"{reason}\n\nDetail: {decision.error_reason}"
    else:
        if not decision.remediation:
            raise ValueError(
                "a block must carry a remediation; blocking a turn without telling the model what "
                "to do is an unsatisfiable stop"
            )
        reason = decision.remediation

    # `ensure_ascii=False` so the captured gates' em dashes and · separators survive; the stream is
    # UTF-8. Exactly two keys, because the schema of a Stop decision is closed by the harness.
    payload = json.dumps(
        {"decision": "block", "reason": reason}, ensure_ascii=False, separators=(",", ":")
    )
    return Rendered(stdout=payload.encode("utf-8"), exit_code=0)


def _decide(run_result: Any, entries: Any) -> Decision:
    """The whole decision, as a pure function of the evaluation run. No I/O, no writes.

    Split out of `run` on 2026-09-01 so the observability writes can happen AFTER the decision is
    selected, which is what the ruled stamp table says — "the coordinator writes one stamp per
    evaluator slot AFTER arbitration has chosen the decision, so a stamp write cannot influence
    what was decided." They ran before it, and the ordering is what makes that sentence true rather
    than merely intended.
    """
    if run_result.coordinator_error and run_result.coordinator_failure_policy == "fail_open":
        # D04 rules a coordinator INPUT failure fail-open: it matches the pre-coordinator
        # behaviour, and a block there gives the model nothing it can act on.
        return Decision(decision="pass", evidence=("coordinator_input_failure_fail_open",))

    if run_result.coordinator_error:
        # Everything else the run reports about ITSELF is fail-closed under D04 — today that is
        # the empty evaluator set, the case COORDINATOR_ERROR_REMEDIATION describes in prose.
        return Decision(
            decision="coordinator_error", error_reason=run_result.coordinator_error,
        )

    unevaluated = tuple(
        outcome.gate_id
        for outcome in run_result.outcomes
        if outcome.result is None and outcome.blocks_the_turn
    )
    if unevaluated:
        # A fail-closed gate the coordinator could not DISPATCH. Round 4's secondary finding:
        # `run` skipped every `result is None` outcome, so a missing declared dependency or a
        # fatal condition let the turn end in silence while the ruling said fail-closed.
        #
        # Rendered as the coordinator's fault, not the gate's, because the gate never looked.
        # This preempts any real block from a gate that DID run, and that is deliberate: the ruled
        # remediation says "no gate result for this turn is trustworthy", which is exactly true of
        # a coordinator that cannot dispatch its own declared set. Losing an actionable gate
        # remediation is the cost; letting an unverified turn end is not on the table.
        return Decision(
            decision="coordinator_error",
            error_reason=(
                "these fail-closed gates produced no verdict because the coordinator could not "
                f"dispatch them: {', '.join(unevaluated)}"
            ),
        )

    results = []
    for outcome in run_result.outcomes:
        if outcome.result is None:
            continue
        if outcome.blocks_the_turn:
            # D04's ruling, APPLIED. A fail-closed gate that could not evaluate must stop the
            # turn. Projecting only `outcome.result` dropped `blocks_the_turn`, arbitration saw
            # an `error` rather than a block, found no blocks, and returned pass — so the turn
            # was allowed while telemetry recorded `failure_policy=fail_closed`. A policy
            # asserted but not run is worse than the accidental fail-open it replaced.
            #
            # The recorded VERDICT stays `error` in telemetry; what changes is the coordinator's
            # decision. `evaluator_exception` is not in the arbitration table, so it lands in the
            # `unmapped` category — carried through verbatim and never suppressed, which is
            # exactly the treatment an unrecognised demand should get.
            results.append(_as_blocking(outcome.result))
            continue
        results.append(outcome.result)
    ordinals = {entry.gate_id: entry.ordinal for entry in entries}
    return arbitrate(results, ordinals=ordinals)


def run(
    event: Mapping[str, Any],
    *,
    dependencies: Mapping[str, Any] | None = None,
    registry: Any = None,
    event_id: Any = None,
    stamp_root: Any = None,
    telemetry_root: Any = None,
) -> Rendered:
    """One coordinator invocation: evaluate the full set, arbitrate, write, render once.

    R15's Done condition. Everything upstream is pure; this is the only place that produces the
    bytes, so exactly one thing in the process can speak with authority.

    `stamp_root` and `telemetry_root` are SEPARATE, and both default to the resolvers above rather
    than to one root under the repository. They were a single `observability_root` whose only
    caller in the whole package was the falsifier — which redirected it to scratch and then
    measured the redirect, so nothing measured where a real install writes.
    """
    entries = tuple(registry if registry is not None else REGISTRY)
    run_result = evaluate_set(
        event, registry=entries, dependencies=dependencies, event_id=event_id
    )

    decision = _decide(run_result, entries)

    # AFTER the decision, per the ruled table, and best-effort: every failure path in both writers
    # swallows BaseException, because decision 0277 rules that a stamp can never change its
    # caller's exit code. Round 3 rejected this promotion because both writes were named in the
    # ADR, the rollback runbook and the roadmap's D05 ruling, and implemented nowhere.
    try:
        resolved_stamps = stamp_root if stamp_root is not None else _stamp_root(event)
        _write_stamps(run_result.outcomes, resolved_stamps)
    except BaseException:  # noqa: BLE001 - root lookup is observability, not authority
        pass
    try:
        resolved_telemetry = (
            telemetry_root if telemetry_root is not None else _telemetry_root(event)
        )
        _write_telemetry(run_result, event, resolved_telemetry)
    except BaseException:  # noqa: BLE001 - root lookup is observability, not authority
        pass

    return render(decision)
