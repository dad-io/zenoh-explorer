"""G08 — one retirement decision over CLI and GoLand promotion proof.

The two workflows use the same check names, G05 proof level, and G01 toolchain identity. A missing
observation is not a pass, and one new BROKEN finding stops retirement regardless of which
workflow produced it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Final

from bearhug.replay.dlv_proof import RECOMMENDED_REQUIRED_LEVEL

REQUIRED_WORKFLOWS: Final = ("cli", "goland")
REQUIRED_CHECKS: Final = (
    "toolchain_identity",
    "vet_package",
    "build_module",
    "race_test_package",
    "dlv_level_2",
)
CHECK_STATUSES: Final = frozenset({"pass", "broken", "unobserved", "stale"})


@dataclass(frozen=True, slots=True)
class PromotionVerdict:
    status: str
    retirement_allowed: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "retirement_allowed": self.retirement_allowed,
            "reasons": list(self.reasons),
        }


def validate_promotion_workflows(workflows: object) -> list[str]:
    """Cross-field structural checks for the regression manifest's workflow block."""
    if not isinstance(workflows, list):
        return ["promotion_workflows is missing or is not a list"]
    problems: list[str] = []
    names = [row.get("workflow") for row in workflows if isinstance(row, dict)]
    counts = Counter(names)
    for name in REQUIRED_WORKFLOWS:
        if counts[name] == 0:
            problems.append(f"promotion workflow {name} is missing")
        elif counts[name] > 1:
            problems.append(f"promotion workflow {name} is duplicated")
    unexpected = sorted(str(name) for name in counts if name not in REQUIRED_WORKFLOWS)
    if unexpected:
        problems.append(f"unexpected promotion workflows: {unexpected}")

    contracts: set[str] = set()
    identities: set[str] = set()
    for index, row in enumerate(workflows):
        if not isinstance(row, dict):
            problems.append(f"promotion_workflows[{index}] is not an object")
            continue
        workflow = row.get("workflow", f"index {index}")
        contract = row.get("contract_version")
        if isinstance(contract, str) and contract:
            contracts.add(contract)
        identity = row.get("toolchain_identity")
        if isinstance(identity, str) and identity:
            identities.add(identity)
        checks = row.get("checks")
        if not isinstance(checks, list):
            problems.append(f"{workflow} checks are missing or not a list")
            continue
        ids = [check.get("id") for check in checks if isinstance(check, dict)]
        check_counts = Counter(ids)
        for check_id in REQUIRED_CHECKS:
            if check_counts[check_id] == 0:
                problems.append(f"{workflow} required check {check_id} is missing")
            elif check_counts[check_id] > 1:
                problems.append(f"{workflow} required check {check_id} is duplicated")
        extra = sorted(
            str(check_id) for check_id in check_counts if check_id not in REQUIRED_CHECKS
        )
        if extra:
            problems.append(f"{workflow} has unexpected checks: {extra}")
    if len(contracts) > 1:
        problems.append(f"workflows use different proof contract versions: {sorted(contracts)}")
    if len(identities) > 1:
        problems.append("workflows name different toolchain identities")
    return problems


def evaluate_promotion_workflows(workflows: object) -> PromotionVerdict:
    """Allow retirement only when both complete workflow records are clean and observed."""
    structural = validate_promotion_workflows(workflows)
    if structural:
        return PromotionVerdict("halted", False, tuple(structural))
    assert isinstance(workflows, list)  # established above
    reasons: list[str] = []
    for row in workflows:
        workflow = str(row["workflow"])
        if not row.get("toolchain_identity"):
            reasons.append(f"{workflow}: toolchain identity is unobserved")
        for check in row["checks"]:
            check_id = str(check["id"])
            status = str(check.get("status"))
            if status != "pass":
                reasons.append(f"{workflow}: {check_id} is {status}")
            if check_id == "dlv_level_2":
                proof_level = check.get("proof_level")
                if not isinstance(proof_level, int) or proof_level < RECOMMENDED_REQUIRED_LEVEL:
                    reasons.append(
                        f"{workflow}: DLV proof level {proof_level!r} is below "
                        f"G05 level {RECOMMENDED_REQUIRED_LEVEL}"
                    )
        for finding in row.get("new_findings", []):
            if finding.get("severity") == "broken":
                reasons.append(
                    f"{workflow}: new BROKEN finding {finding.get('id', '<unnamed>')}"
                )
    if reasons:
        return PromotionVerdict("halted", False, tuple(reasons))
    return PromotionVerdict("ready", True, ())


__all__ = [
    "CHECK_STATUSES",
    "REQUIRED_CHECKS",
    "REQUIRED_WORKFLOWS",
    "PromotionVerdict",
    "evaluate_promotion_workflows",
    "validate_promotion_workflows",
]
