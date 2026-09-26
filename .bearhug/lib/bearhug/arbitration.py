"""D03 — load the proposed arbitration table and its constructed result sets.

Specification only. This module reads data; it does not arbitrate. R14 implements
`arbitrate()` as a pure function and must reproduce every fixture's declared expectation.

Keeping the table as JSON rather than as Python constants is deliberate: the plan requires the
tests to be "expressed as data", and a table that lives in code drifts from the document that
justifies it. The one Python-side derivation here is `CATEGORY_OF_REASON`, which is read from
the table rather than restated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bearhug.paths import REPO_ROOT

TABLE_PATH = REPO_ROOT / "docs" / "schemas" / "arbitration-table.v1.json"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "arbitration"


def load_table(path: Path = TABLE_PATH) -> dict[str, Any]:
    """The proposed table, as data. Raises rather than returning a default."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_fixtures(directory: Path = FIXTURES_DIR) -> dict[str, dict[str, Any]]:
    """Every constructed result set, keyed by case name."""
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(Path(directory).glob("*.json"))
    }


#: reason_code -> category, read from the table so there is one authority for the mapping.
CATEGORY_OF_REASON: dict[str, str] = load_table()["reason_categories"]


def category_of(reason_code: str, table: dict[str, Any] | None = None) -> str:
    """The category for a reason code, or `unmapped` when the table does not know it.

    Never raises on an unknown code: the table's own policy is that an unrecognised block is
    carried through, and a loader that raised would turn a gate update into a crash.
    """
    table = table or load_table()
    return table["reason_categories"].get(
        reason_code, table["unmapped_reason_policy"]["category"]
    )


def derive_fixture_expectation(
    case: dict[str, Any], table: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Interpret the approved table into one fixture's expected arbitration outcome.

    Runtime arbitration is tested against the fixtures; this function separately proves that the
    fixture answers follow the table instead of being a second hand-maintained policy.
    """
    table = table or load_table()
    results = list(case["results"])
    gate_ids = [result["gate_id"] for result in results]
    base: dict[str, Any] = {
        "decision": "pass",
        "categories_selected": [],
        "categories_deferred": [],
        "remediation_order": [],
        "deferred_are_reported": False,
    }
    if len(gate_ids) != len(set(gate_ids)):
        return {
            **base,
            "decision": "coordinator_error",
            "error_reason": "duplicate gate_id in the result set",
        }

    def tie_key(result: dict[str, Any]) -> tuple[int, str, str]:
        gate = table["gates"].get(result["gate_id"], {})
        return (
            int(gate.get("evaluator_ordinal", 2**31 - 1)),
            result["gate_id"],
            result.get("reason_code") or "",
        )

    errors = sorted(
        (result for result in results if result.get("verdict") == "error"), key=tie_key
    )
    blocks = [result for result in results if result.get("verdict") == "block"]
    if not blocks:
        if errors:
            base["errors_deferred_to_failure_policy"] = [result["gate_id"] for result in errors]
        return base

    category_for = {
        id(result): category_of(result.get("reason_code") or "", table) for result in blocks
    }
    present = set(category_for.values())

    def category_key(name: str) -> tuple[int, str]:
        return table["categories"][name]["priority"], name

    anchor = min(present, key=category_key)
    selected = present & {anchor, *table["categories"][anchor]["compatible_with"]}
    deferred = present - selected
    selected_blocks = sorted(
        (result for result in blocks if category_for[id(result)] in selected), key=tie_key
    )
    expected = {
        **base,
        "decision": "block",
        "categories_selected": sorted(selected, key=category_key),
        "categories_deferred": sorted(deferred, key=category_key),
        "remediation_order": [result["gate_id"] for result in selected_blocks],
        "deferred_are_reported": bool(deferred),
    }
    if errors:
        expected["errors_deferred_to_failure_policy"] = [result["gate_id"] for result in errors]
    return expected
