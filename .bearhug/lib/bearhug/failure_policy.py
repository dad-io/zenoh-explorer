"""D04 — load the evaluator failure-policy table and its constructed failure cases.

Specification only. R13 applies the approved table; nothing here decides anything.

The table deliberately carries `current_behaviour` and `proposed_policy` as separate fields. The
plan's instruction was to report current behaviour per gate BEFORE proposing future behaviour,
and a single `policy` field would have quietly turned a proposal into a description of reality.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bearhug.paths import REPO_ROOT

TABLE_PATH = REPO_ROOT / "docs" / "schemas" / "failure-policy-table.v1.json"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "failure_policy"


def load_failure_table(path: Path = TABLE_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_failure_fixtures(directory: Path = FIXTURES_DIR) -> dict[str, dict[str, Any]]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(Path(directory).glob("*.json"))
    }


def unruled_gates(table: dict[str, Any] | None = None) -> list[str]:
    """Which evaluators still need Sam's fail-open/fail-closed ruling.

    Exposed so a later task cannot start writing coordinator failure handling while a row is
    still unruled — the check is one call rather than a careful read of the JSON.
    """
    table = table or load_failure_table()
    unruled = [
        gate
        for gate, row in table["evaluators"].items()
        if row["proposed_policy"]["policy"] == "unruled"
    ]
    unruled += [
        f"coordinator:{name}"
        for name, row in table["coordinator"].items()
        if row["proposed_policy"]["policy"] == "unruled"
    ]
    return sorted(unruled)


def derive_fixture_expectation(
    case: dict[str, Any], table: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Derive a constructed failure case's policy-owned fields from the ruled table."""
    table = table or load_failure_table()
    domain = case["domain"]
    if domain == "evaluator":
        row = table["evaluators"][case["gate_id"]]
        evaluator_verdict = "error"
    elif domain == "coordinator":
        row = table["coordinator"][case["failure"]]
        evaluator_verdict = None
    else:
        raise ValueError(f"unknown failure fixture domain: {domain!r}")

    proposed = row["proposed_policy"]
    policy = proposed["policy"]
    if row.get("never_changes_decision"):
        if "decision_before_failure" not in case:
            raise ValueError("an observer failure fixture must state decision_before_failure")
        coordinator_decision = case["decision_before_failure"]
    else:
        coordinator_decision = "block" if policy == "fail_closed" else "pass"

    expected: dict[str, Any] = {
        "evaluator_verdict": evaluator_verdict,
        "coordinator_decision": coordinator_decision,
        "telemetry_reason": proposed["telemetry_reason"],
        "policy_applied": policy,
    }
    if row.get("never_changes_decision"):
        expected["decision_unchanged_by_telemetry_failure"] = True
    return expected
