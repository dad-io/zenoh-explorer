"""D05's projection, as the ruled table actually specifies it.

`stamp-compatibility/1`'s `projection_rule` is one sentence:

    The coordinator emits, per evaluator slot, the EXACT verdict string that gate writes today for
    the same outcome. It does not invent, lowercase, normalise, or tidy the vocabulary.

The coordinator's first projection did not do that. It collapsed every pass to `PASS`, every
not-applicable to `SKIP-<reason_code>`, and every crash to `CRASH-<Type>` — so of the three
anomalies the table names as MUST-PRESERVE, it preserved none:

  * `pass-no-tasks-no-edits` (task-durability, lowercase) became `PASS`
  * `PASS-board-only-tool-absent` and `PASS-tags-ok` became `PASS`
  * `CRASH <Type>` (joinkey, SPACE-separated) became `CRASH-<Type>`
  * `BLOCKED <n> join-key defect(s)` became `BLOCKED`

ADR section 8 asserted the opposite in prose: "The projection emits the exact strings the gates
already write, including two lowercase outliers and joinkey's space-separated `CRASH`." That is
the same defect class four Barracuda-owned reviews have now rejected — a responsibility named in
prose and implemented nowhere — found here by working through the round-4 return's column of ADR
claims its own falsifier does not measure.

**The map is derived, not remembered.** Every string below is the one the snapshotted gate leaves
on disk for that outcome, and `test_stamp_projection.py` extracts the gates' `_stamp(...)` call
sites by AST and holds this table to them. Where a gate stamps TWICE on one path, the second write
wins (`last_write_wins` in the table) and only the surviving string appears here — `pass-board-only`
is written and immediately overwritten by `PASS-board-only-tool-absent` on the captured gate's own
path, so the captured gate itself never observes it. The evaluator's `board_only` — decision 0137's
board route reaching the undetected-absence case, a branch the captured gate has no path to — is
now (ruled) projected onto that exact literal, since it is the snapshot's own string for a
board-only pass and no other outcome uses it.

**Two outcomes stamp NOTHING**, and that is deliberate rather than an omission. See
`SILENT` below.
"""

from __future__ import annotations

from typing import Any

#: (gate_id, reason_code) -> the exact string that gate writes. Verdict-independent: the reason
#: code already determines the branch, and keying on the verdict too would let a gate's `pass` and
#: `block` for one reason code disagree about which branch ran.
PROJECTION: dict[tuple[str, str], str] = {
    ("response-shape", "stop_hook_active"): "SKIP-stop-hook-active",
    ("response-shape", "no_final_text"): "PASS-no-final-text",
    ("response-shape", "stop_requested"): "PASS-stop-requested",
    ("response-shape", "shape_ok"): "PASS",
    ("response-shape", "multi_ask"): "BLOCKED",
    ("response-shape", "no_engagement_point"): "BLOCKED",

    ("dlv-verify-gate", "no_go_writes"): "PASS-no-go-edits",
    # Two reason codes, one legacy branch: the captured gate stamps `PASS` whenever dlv ran after
    # the write, and R08 split that into a real subcommand match and a bare word match so the
    # DISTINCTION lands in evidence. Whether the word-only case should stop passing is a behaviour
    # change, deferred and unstarted — docs/proposals/DEFERRED.md, R08B. Until it is ruled, both
    # project to the string the gate writes today, because this is a projection and not a policy.
    ("dlv-verify-gate", "dlv_session_after_write"): "PASS",
    ("dlv-verify-gate", "dlv_word_match_after_write"): "PASS",
    ("dlv-verify-gate", "dlv_session_missing"): "BLOCKED",

    ("task-durability", "stop_hook_active"): "SKIP-stop-hook-active",
    ("task-durability", "no_transcript"): "SKIP-no-transcript",
    # The two LOWERCASE outliers the table names for the CAPTURED gate's own paths.
    # `pass-no-tasks-no-edits` survives its branch there; `pass-board-only` does not (see the
    # module docstring) on the captured gate's own path to it.
    ("task-durability", "no_tasks_no_edits"): "pass-no-tasks-no-edits",
    ("task-durability", "board_only_tool_absent"): "PASS-board-only-tool-absent",
    # `board_only` is an evaluator-only outcome (decision 0137's board route reaching the
    # undetected-absence case): the captured gate has no branch that reaches it. `pass-board-only`
    # is the exact literal already in the captured gate's source for this meaning, dormant there
    # because its one path to it always continues into `PASS-board-only-tool-absent` first.
    ("task-durability", "board_only"): "pass-board-only",
    ("task-durability", "tags_ok"): "PASS-tags-ok",
    ("task-durability", "task_tool_withdrawn"): "BLOCKED",
    ("task-durability", "task_list_empty"): "BLOCKED",
    ("task-durability", "task_authority_path_missing"): "BLOCKED",
    ("task-durability", "board_row_missing"): "BLOCKED",
    ("task-durability", "phase_tag_missing"): "BLOCKED",

    ("joinkey-lint", "joinkey_clean"): "PASS",
    ("joinkey-lint", "joinkey_unplaced_only"): "PASS",

    ("review-gate", "stop_hook_active"): "SKIP-stop-hook-active",
    ("review-gate", "no_transcript"): "PASS",
    ("review-gate", "no_source_write"): "PASS",
    ("review-gate", "review_dispatched_after_write"): "PASS",
    ("review-gate", "adversarial_review_missing"): "BLOCKED",
}

#: Outcomes the captured gate reached only by CRASHING: its outer handler stamped `CRASH-<type>`,
#: so that is the exact string it writes for the outcome. Round 13's response-shape 1.1.0 no longer
#: raises on a missing transcript (`no_transcript`, not_applicable), and fidelity still projects
#: what the gate wrote — the consumer sees the same token it saw before the round.
LEGACY_CRASH: dict[tuple[str, str], str] = {
    ("response-shape", "no_transcript"): "FileNotFoundError",
}

#: Outcomes on which the captured gate writes NO stamp at all, leaving whatever was there.
#:
#: `dlv-verify-gate` returns before any `_stamp` on both guards.  Join-key has no
#: stop-hook-active guard in either the captured gate or the evaluator, so it has no silent guard
#: pairing to project.
#:
#: **RULED 2026-09-02 (D05-silent-stamps, provisional under Sam's standing authorization): fidelity
#: wins; the table's `adapter.spec` clause now carries the exception and `silent_pairings` lists
#: these two.** The original contradiction, kept for the record: `projection_rule`
#: says emit the exact string the gate writes today — which, here, is nothing. The `adapter.spec`
#: clause says the coordinator "writes one stamp per evaluator slot". Both cannot hold. Fidelity
#: is implemented, because the disposition's whole rationale is compatibility with
#: `scripts/automation-status.sh`, a consumer bear-hug cannot inspect: writing `SKIP` where the
#: gate writes nothing CLEARS a stale `BLOCKED`, and whether the dashboard should forget a block
#: because the next turn was stop-hook-active is a decision, not a detail.
SILENT: frozenset[tuple[str, str]] = frozenset({
    ("dlv-verify-gate", "stop_hook_active"),
    ("dlv-verify-gate", "no_transcript"),
})

#: joinkey-lint separates its crash token with a SPACE where the other four use a hyphen. Under
#: the table's `verdict_grammar` the first token is the state, so joinkey's crash state is `CRASH`
#: and the others' is `CRASH-KeyError` — a different token per gate, which the table forbids
#: normalising because the consumer's matching cannot be captured.
CRASH_SEPARATOR: dict[str, str] = {"joinkey-lint": " "}

#: Counted forms. The count comes from the result's own evidence, so the projection reads what the
#: evaluator measured rather than recomputing it — a second count here would be a second authority.
_COUNTED = {
    ("joinkey-lint", "joinkey_defects"): "BLOCKED {n} join-key defect(s)",
    ("joinkey-lint", "joinkey_findings_report_mode"): "FINDINGS {n} (report mode)",
}

#: The evidence key carrying that count.
_COUNT_EVIDENCE = "blocking_defects"


def _count_from(result: Any) -> str | None:
    for item in getattr(result, "evidence", ()) or ():
        value = getattr(item, "value", None)
        if isinstance(value, str) and value.startswith(f"{_COUNT_EVIDENCE}="):
            return value.split("=", 1)[1]
    return None


def project(gate_id: str, result: Any, exception_type: str | None) -> str | None:
    """The exact string this gate writes for this outcome, or None to write nothing.

    Returns None rather than raising on an unknown pairing: a projection that crashed would take
    the Stop path down over observability, which `stamp.sh`'s contract — "NEVER fails the caller" —
    forbids. An unknown pairing writing nothing is the same shape as `SILENT`, and it is visible as
    an absent stamp rather than as a wrong one.
    """
    if exception_type:
        return f"CRASH{CRASH_SEPARATOR.get(gate_id, '-')}{exception_type}"
    if result is None:
        return None
    reason_code = getattr(result, "reason_code", None)
    key = (gate_id, str(reason_code))
    if key in LEGACY_CRASH:
        return f"CRASH{CRASH_SEPARATOR.get(gate_id, '-')}{LEGACY_CRASH[key]}"
    if key in SILENT:
        return None
    template = _COUNTED.get(key)
    if template is not None:
        count = _count_from(result)
        return None if count is None else template.format(n=count)
    return PROJECTION.get(key)
