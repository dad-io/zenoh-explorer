"""R14 — arbitration as a pure function over the full result set.

Input is every result for one event; output is one deterministic decision model. No stdout, no
filesystem, no telemetry, no hook-input parsing — R15 owns the Stop response, and a module that
rendered would be a second authority beside it.

The table is `arbitration-table/1`, approved by Sam on 2026-08-31 and duplicated here because a
stdlib-only runtime cannot read the lab's JSON at evaluation time. A lab test compares the copies;
an unpinned duplicate would arbitrate by a table nobody approved.

The resolution it encodes, which D01 measured and Sam confirmed: when `response_form` collides with
the work-adding categories, **the work is selected and the shape is deferred and reported**. After
the demanded work is done the reply is different text, so grading the shape of a reply that is
about to be replaced measures nothing. The rejected alternative — shape wins — would let a
too-long reply suppress a missing `dlv` session, a missing review, an empty task list and a
join-key defect at once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

TABLE_VERSION = "arbitration-table/1"

#: category -> (priority, compatible_with). Lower priority number wins the anchor.
CATEGORIES: dict[str, dict[str, Any]] = {
    "verification": {"priority": 1,
                     "compatible_with": ["review", "durability", "consistency", "unmapped"]},
    "review": {"priority": 2,
               "compatible_with": ["verification", "durability", "consistency", "unmapped"]},
    "durability": {"priority": 3,
                   "compatible_with": ["verification", "review", "consistency", "unmapped"]},
    "consistency": {"priority": 4,
                    "compatible_with": ["verification", "review", "durability", "unmapped"]},
    "unmapped": {"priority": 5,
                 "compatible_with": ["verification", "review", "durability", "consistency"]},
    # Compatible with NOTHING. This is the measured contradiction, not a policy preference.
    "response_form": {"priority": 6, "compatible_with": []},
}

REASON_CATEGORIES: dict[str, str] = {
    "multi_ask": "response_form",
    "no_engagement_point": "response_form",
    "dlv_session_missing": "verification",
    "task_tool_withdrawn": "durability",
    "task_list_empty": "durability",
    "board_row_missing": "durability",
    "phase_tag_missing": "durability",
    "task_authority_path_missing": "durability",
    "joinkey_defects": "consistency",
    "adversarial_review_missing": "review",
}

#: A total order over fields carried IN the results, so input order cannot change the output.
TIE_BREAK = ("evaluator_ordinal", "gate_id", "reason_code")

#: A reason this table version does not recognise. Carried through verbatim and never suppressed:
#: the alternative silently loses a gate's demand whenever a gate is updated ahead of the table,
#: which is the shape of 9632e77 — a shipped false negative from classification that did not cover
#: its cases.
UNMAPPED_CATEGORY = "unmapped"


@dataclass(frozen=True, slots=True)
class Decision:
    """One arbitrated decision. Carries no rendering and no side effect."""

    decision: str  # "pass" | "block" | "coordinator_error"
    table_version: str = TABLE_VERSION
    categories_selected: tuple[str, ...] = ()
    categories_deferred: tuple[str, ...] = ()
    remediation_order: tuple[str, ...] = ()
    remediation: str | None = None
    deferred_are_reported: bool = False
    errors_deferred_to_failure_policy: tuple[str, ...] = ()
    error_reason: str | None = None
    evidence: tuple[str, ...] = field(default=())


def _get(result: Any, name: str) -> Any:
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


def category_of(reason_code: Any) -> str:
    """The category for a reason code, or `unmapped` when the table does not know it."""
    return REASON_CATEGORIES.get(str(reason_code), UNMAPPED_CATEGORY)


def arbitrate(
    results: Sequence[Any],
    *,
    ordinals: Mapping[str, int] | None = None,
) -> Decision:
    """Choose one decision from the full result set. Pure."""
    ordinals = dict(ordinals or {})
    rows = list(results)

    gate_ids = [str(_get(r, "gate_id")) for r in rows]
    duplicates = sorted({gid for gid in gate_ids if gate_ids.count(gid) > 1})
    if duplicates:
        # R12's registry enforces uniqueness. If it does not hold, the tie-break is not total and
        # arbitration must fail loudly rather than pick one and continue.
        return Decision(
            decision="coordinator_error",
            error_reason=f"duplicate gate_id in the result set: {', '.join(duplicates)}",
        )

    errors = tuple(
        sorted(str(_get(r, "gate_id")) for r in rows if _get(r, "verdict") == "error")
    )
    blocks = [r for r in rows if _get(r, "verdict") == "block"]

    if not blocks:
        return Decision(
            decision="pass",
            errors_deferred_to_failure_policy=errors,
            evidence=(f"table_version={TABLE_VERSION}", "blocking_results=0"),
        )

    def sort_key(result: Any) -> tuple[Any, ...]:
        gate_id = str(_get(result, "gate_id"))
        return (
            ordinals.get(gate_id, len(ordinals)),
            gate_id,
            str(_get(result, "reason_code")),
        )

    blocks.sort(key=sort_key)
    present = {}
    for result in blocks:
        present.setdefault(category_of(_get(result, "reason_code")), []).append(result)

    anchor = min(present, key=lambda name: CATEGORIES[name]["priority"])
    compatible = set(CATEGORIES[anchor]["compatible_with"])
    selected = {anchor} | (set(present) & compatible)
    deferred = set(present) - selected

    chosen = [r for r in blocks if category_of(_get(r, "reason_code")) in selected]
    order = tuple(str(_get(r, "gate_id")) for r in chosen)

    paragraphs = [
        str(_get(r, "remediation")) for r in chosen if _get(r, "remediation")
    ]
    remediation = "\n\n".join(paragraphs)
    if deferred:
        # Named, not discarded: the model must not be surprised by a second block next turn.
        #
        # Each category is named WITH the gates that raised it. `response_form` is this table's
        # vocabulary — it is not a gate id, not a filename, and not a decision number, so the one
        # token the deferral handed the reader resolved to nothing in the repository they were
        # standing in. Round 5 raised it as the small half of the retyped-remediation finding.
        # The gate ids come from the results themselves rather than from a second table.
        gates_by_category: dict[str, list[str]] = {}
        for result in blocks:
            category = category_of(_get(result, "reason_code"))
            if category in deferred:
                gate_id = str(_get(result, "gate_id"))
                if gate_id not in gates_by_category.setdefault(category, []):
                    gates_by_category[category].append(gate_id)
        named = ", ".join(
            f"{category} ({', '.join(gates_by_category.get(category, ['gate unknown']))})"
            for category in sorted(deferred)
        )
        remediation += (
            "\n\nDeferred to the next turn: "
            + named
            + ". These are not waived — they are re-evaluated once the work above is done, "
            "because the reply that carries it will be different text."
        )

    return Decision(
        decision="block",
        categories_selected=tuple(sorted(selected)),
        categories_deferred=tuple(sorted(deferred)),
        remediation_order=order,
        remediation=remediation,
        deferred_are_reported=bool(deferred),
        errors_deferred_to_failure_policy=errors,
        evidence=(
            f"table_version={TABLE_VERSION}",
            f"anchor_category={anchor}",
            f"blocking_results={len(blocks)}",
        ),
    )
