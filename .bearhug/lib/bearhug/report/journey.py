"""J01 — validate and evaluate a returned end-to-end Barracuda journey.

Bear Hug does not drive the product session. It accepts a closed evidence document, checks that
each ordered step resolves to one attribution bundle, and distinguishes incomplete evidence from
an accepted journey.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Final

from bearhug.replay.dlv_proof import RECOMMENDED_REQUIRED_LEVEL

REQUIRED_STEPS: Final = (
    "session_started",
    "work_restored",
    "cockpit_task_authority",
    "go_edit",
    "toolchain_pipeline",
    "dlv_proof",
    "review",
    "coordinator_arbitration",
    "task_updated",
    "durable_board_ledger",
    "cockpit_evidence_chain",
)
REQUIRED_TOOLCHAIN_STAGES: Final = (
    "gofmt",
    "vet_package",
    "build_module",
    "race_test_package",
)


@dataclass(frozen=True, slots=True)
class JourneyVerdict:
    status: str
    accepted: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
        }


def validate_journey(journey: object) -> list[str]:
    """Cross-field checks beyond ``barracuda-journey.schema.json``."""
    if not isinstance(journey, dict):
        return ["journey is not an object"]
    problems: list[str] = []
    attribution = journey.get("attribution")
    attribution_id = attribution.get("id") if isinstance(attribution, dict) else None
    steps = journey.get("steps")
    if not isinstance(steps, list):
        return ["steps is not a list"]
    ids = [step.get("id") for step in steps if isinstance(step, dict)]
    if ids != list(REQUIRED_STEPS):
        problems.append(
            "ordered step list must be exactly " + ", ".join(REQUIRED_STEPS)
        )
    duplicates = sorted(str(step_id) for step_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        problems.append(f"duplicate journey steps: {duplicates}")
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            problems.append(f"steps[{index}] is not an object")
            continue
        if step.get("attribution_ref") != attribution_id:
            problems.append(
                f"steps[{index}] {step.get('id')}: attribution_ref does not resolve to "
                "the journey attribution"
            )
    return problems


def _step_map(journey: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        step["id"]: step
        for step in journey["steps"]
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    }


def evaluate_journey(journey: object) -> JourneyVerdict:
    """Accept only a complete, attributable, depth-checked journey."""
    structural = validate_journey(journey)
    if structural:
        return JourneyVerdict("invalid", False, tuple(structural))
    assert isinstance(journey, dict)
    reasons: list[str] = []
    steps = _step_map(journey)
    for step_id in REQUIRED_STEPS:
        step = steps[step_id]
        state = step.get("state")
        if state != "observed":
            reasons.append(f"{step_id} is {state}")
            continue
        if not step.get("observed_at"):
            reasons.append(f"{step_id}: observed_at is missing")
        if not step.get("evidence_refs"):
            reasons.append(f"{step_id}: evidence_refs is empty")

    pipeline = steps["toolchain_pipeline"].get("result") or {}
    stages = pipeline.get("toolchain_stages") or []
    by_stage = {
        row.get("stage"): row.get("outcome")
        for row in stages
        if isinstance(row, dict)
    }
    if set(by_stage) != set(REQUIRED_TOOLCHAIN_STAGES):
        reasons.append("toolchain_pipeline does not name the exact required stage set")
    for stage in REQUIRED_TOOLCHAIN_STAGES:
        if by_stage.get(stage) != "pass":
            reasons.append(f"toolchain_pipeline: {stage} is {by_stage.get(stage, 'missing')}")

    dlv = steps["dlv_proof"].get("result") or {}
    proof_level = dlv.get("dlv_proof_level")
    if not isinstance(proof_level, int) or proof_level < RECOMMENDED_REQUIRED_LEVEL:
        reasons.append(
            f"DLV proof level {proof_level!r} is below G05 level {RECOMMENDED_REQUIRED_LEVEL}"
        )

    review = steps["review"].get("result") or {}
    if review.get("review_outcome") != "pass":
        reasons.append(f"review outcome is {review.get('review_outcome', 'missing')}")

    coordinator = steps["coordinator_arbitration"].get("result") or {}
    if coordinator.get("coordinator_decisions") != 1:
        reasons.append("coordinator must emit exactly one authority-bearing decision")

    durable = steps["durable_board_ledger"].get("result") or {}
    if durable.get("board_confirmed") is not True or durable.get("ledger_confirmed") is not True:
        reasons.append("durable BOARD and LEDGER state are not both confirmed")

    final = steps["cockpit_evidence_chain"].get("result") or {}
    if final.get("evidence_chain_visible") is not True:
        reasons.append("final cockpit evidence chain is not visible")
    if final.get("runtime_identity_visible") is not True:
        reasons.append("final cockpit runtime identity is not visible")

    if reasons:
        return JourneyVerdict("incomplete", False, tuple(reasons))
    return JourneyVerdict("accepted", True, ())


__all__ = [
    "REQUIRED_STEPS",
    "REQUIRED_TOOLCHAIN_STAGES",
    "JourneyVerdict",
    "evaluate_journey",
    "validate_journey",
]
