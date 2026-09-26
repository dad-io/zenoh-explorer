"""G05 — the DLV proof-level protocol and its observable mappings.

The level reached and the gate verdict are deliberately separate facts. Before R08C was ruled
(Sam, 2026-09-03), the runtime blocked a resolved debugger path the captured gate's word match
could not see, for parity, while this module still recorded that a process launched
(`real_subcommand_unmatched_by_word`, level 2). R08C is now ruled and the runtime PASSES that case
(`real_subcommand_resolved_program`); both evidence strings are kept in the mapping — the old one
for records written before the ruling, the new one for what the runtime emits today — and both are
level 2, because the level was never the thing the ruling changed.
"""

from __future__ import annotations

from typing import Final

TRANSCRIPT_OBSERVABLE: Final = "transcript_observable"
RECEIPT_REQUIRED: Final = "receipt_required"
NOT_MEASURABLE: Final = "not_measurable"

LEVELS: Final = {
    0: {
        "name": "no_dlv_evidence",
        "evidence": "No debugger mention or invocation is observed after the qualifying write.",
        "observability": TRANSCRIPT_OBSERVABLE,
    },
    1: {
        "name": "command_mentioned",
        "evidence": "The token dlv is present, but no approved debugger subcommand is resolved.",
        "observability": TRANSCRIPT_OBSERVABLE,
    },
    2: {
        "name": "process_launched",
        "evidence": "A resolved dlv program is invoked with an approved debugger subcommand.",
        "observability": TRANSCRIPT_OBSERVABLE,
    },
    3: {
        "name": "target_loaded_or_attached",
        "evidence": "A DLV-side receipt records that the target loaded or attached.",
        "observability": RECEIPT_REQUIRED,
    },
    4: {
        "name": "breakpoint_set",
        "evidence": "A DLV-side receipt records a successfully created breakpoint.",
        "observability": RECEIPT_REQUIRED,
    },
    5: {
        "name": "breakpoint_hit",
        "evidence": "A DLV-side receipt records that execution stopped at the breakpoint.",
        "observability": RECEIPT_REQUIRED,
    },
    6: {
        "name": "runtime_value_observed",
        "evidence": "A DLV-side receipt records a runtime value from the stopped target.",
        "observability": RECEIPT_REQUIRED,
    },
    7: {
        "name": "result_tied_to_changed_behaviour",
        "evidence": "A reviewer establishes that the observation explains the changed behaviour.",
        "observability": NOT_MEASURABLE,
    },
}

DLV_MATCH_TO_LEVEL: Final = {
    None: 0,
    "word_only": 1,
    "real_subcommand": 2,
    # Pre-R08C evidence string (runtime dlv-verify-gate 1.1.0 and earlier), kept so a record
    # written before the ruling still maps rather than raising KeyError.
    "real_subcommand_unmatched_by_word": 2,
    # R08C, ruled (Sam, 2026-09-03): the runtime's current evidence string for the same shape.
    "real_subcommand_resolved_program": 2,
}

LAB_CLASS_TO_LEVEL: Final = {
    "none": 0,
    "word-only": 1,
    "real-session": 2,
}

# Provisional G05 decision. This does not silently change the installed runtime's R08 parity rule.
RECOMMENDED_REQUIRED_LEVEL: Final = 2


def level_for_dlv_match(match: str | None) -> int:
    """Map runtime evidence to a proof level, refusing vocabulary drift."""
    return DLV_MATCH_TO_LEVEL[match]


def level_for_lab_class(class_name: str) -> int:
    """Map the replay classifier's public class to the same ladder."""
    return LAB_CLASS_TO_LEVEL[class_name]


def observability_of(level: int) -> str:
    """Return where evidence for ``level`` can truthfully come from."""
    return LEVELS[level]["observability"]


def satisfies_required_level(
    match: str | None, *, required_level: int = RECOMMENDED_REQUIRED_LEVEL
) -> bool:
    """Whether runtime evidence reaches the requested level; verdict policy lives elsewhere."""
    return level_for_dlv_match(match) >= required_level


__all__ = [
    "DLV_MATCH_TO_LEVEL",
    "LAB_CLASS_TO_LEVEL",
    "LEVELS",
    "NOT_MEASURABLE",
    "RECEIPT_REQUIRED",
    "RECOMMENDED_REQUIRED_LEVEL",
    "TRANSCRIPT_OBSERVABLE",
    "level_for_dlv_match",
    "level_for_lab_class",
    "observability_of",
    "satisfies_required_level",
]
