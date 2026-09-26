"""Accepted-board intake and discovery for the existing capsule campaign worker.

The project binding is a locator, not another execution journal. Plan definitions stay in
WORK.json; once attached, execution status comes only from the campaign controller.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bearhug import host_git
from bearhug import project_work as work
from bearhug import terminal_driver as terminal
from bearhug.campaign import grounding, worktree_cleanup
from bearhug.campaign.capsule_campaign import CapsuleCampaign
from bearhug.campaign.prepared import load_prepared
from bearhug.campaign.proposal import board_capsule_id, prepare_terminal_request
from bearhug.campaign.worktrees import WorktreeInventoryError, inventory_worktrees
from bearhug.paths import RUNS_DIR

BINDING = ".bearhug/campaign.json"
PROFILE = ".bearhug/terminal-driver.json"
ONBOARDING = ".bearhug/onboarding.json"
RECOVERY_OUTCOMES = ("failed", "blocked", "hil_required")


def _blocking_lease(reason: str) -> str | None:
    match = re.search(r"is owned by (?:active|orphaned) lease ([0-9a-f]{64})\b", reason)
    return match[1] if match else None


def _recover_stopped_lease_owner(root: Path, config, lease_id: str) -> dict:
    """Find an exact lease owner after reenrollment, then reuse its ordinary stop worker.

    Caller holds the project intake lock. No newest-run selection or lease deletion occurs.
    """
    link = binding(root)
    attached = terminal._load_session(config, link["session_id"])
    if not attached or not attached.get("locator"):
        raise work.WorkError("The attached campaign has no recoverable locator.")
    if terminal._worker_active(attached):
        raise work.WorkError("The campaign worker is already running.")
    current = CapsuleCampaign(
        load_prepared(terminal._exact_locator(attached["locator"], config.private_root)),
        read_only=True,
    )
    if Path(current.record["subject"]["path"]) != root:
        raise work.WorkError("Campaign locator belongs to another worktree.")
    lease = current.leases().get(lease_id)
    if lease.state == "released":
        return {"status": "recovery_not_needed", "reason": "Lease is already released."}
    owners = []
    for path in sorted((config.private_root / "sessions").glob("*.json")):
        if path.is_symlink():
            raise work.WorkError("Campaign session custody must not be a symlink.")
        record = json.loads(path.read_text())
        session = terminal._load_session(config, record["session_id"])
        if not session or not session.get("locator"):
            continue
        locator = terminal._exact_locator(session["locator"], config.private_root)
        # First match the sealed content identity, then validate the complete owner record.
        sealed = json.loads(locator.read_text())
        if sealed.get("content_sha256") != lease.identity.controller_authority_sha256:
            continue
        owner = CapsuleCampaign(load_prepared(locator, recovery=True), read_only=True)
        if (
            owner.record["campaign_id"] != lease.identity.campaign_id
            or owner.record["run_id"] != lease.identity.run_id
            or owner.record["subject"]["repository_common_dir_sha256"]
            != lease.identity.repository_common_dir_sha256
            or Path(owner.record["subject"]["path"]) != root
            or not any(
                row.get("lease_id") == lease_id
                for row in (owner.state or {}).get("launches", {}).values()
            )
        ):
            raise work.WorkError("Lease owner does not match its sealed campaign.")
        owners.append(session)
    if len(owners) != 1:
        raise work.WorkError("Lease requires one exact preserved owner session; no owner guessed.")
    session = owners[0]
    if terminal._worker_active(session):
        raise work.WorkError("The lease owner's worker is still running.")
    recovery_status = terminal._status_for_locator(session["locator"], recovery=True)
    session.update(recovery_status)
    if not session.get("stopped") or session.get("unresolved_spend"):
        raise work.WorkError(
            "Lease owner must be explicitly stopped with its episode/review boundaries resolved: "
            + session["locator"]
        )
    worker = terminal.TerminalDriver(config, config_path=work._path(root, PROFILE))._launch_worker(
        session, operation="stop"
    )
    session.update(
        worker=dict(worker),
        status="stopping",
        reason="finishing the preserved owner's requested stop",
    )
    terminal._save_session(config, session)
    # `control continue` short-circuits to the same durably-failed refusal `recover` does
    # (`_handle_control`, gated on `failed_episode and not unresolved_spend`) and makes no
    # progress; naming it here would be a second dead end right after this one is cleared.
    next_action = "After lease release, run scripts/bin/bearhug-campaign control continue."
    if session.get("failed_episode") and not session.get("unresolved_spend"):
        next_action = "After lease release, run scripts/bin/bearhug-campaign retire."
    return {
        "status": "recovering",
        "reason": "Owner stop queued; no provider retry started.",
        "lease_id": lease_id,
        "locator": session["locator"],
        "worker": worker,
        "next_action": next_action,
    }


def binding(root: Path) -> dict:
    path = work._path(root, BINDING)
    value = json.loads(path.read_text()) if path.is_file() else {}
    if not isinstance(value, dict):
        raise work.WorkError("Campaign binding must be a JSON object.")
    return value


def profile(root: Path) -> terminal.TerminalDriverConfig:
    config = terminal.load_config(work._path(root, PROFILE))
    if config.subject_path != root:
        raise work.WorkError(
            "Campaign profile belongs to another worktree; configure this worktree."
        )
    return config


def _accepted_prompt(state: dict, text: str) -> str:
    return (
        f"Execute the accepted plan {state['plan']['path']} "
        f"(SHA-256 {state['plan']['sha256']}):\n\n{text}"
    )


def _board_request(root: Path, state: dict, text: str, session_id: str) -> bytes:
    return terminal._canonical({
        "hook_event_name": "UserPromptSubmit",
        "cwd": str(root),
        "session_id": session_id,
        "prompt": _accepted_prompt(state, text),
    })


def _campaign_lane_tasks(tasks: list[dict]) -> list[dict]:
    """Return only the campaign-lane rows the controller may ever select.

    An interactive-lane task is never handed to the campaign: it never becomes a capsule and
    the campaign never dispatches it. A campaign-lane task that depends on an interactive-lane
    task keeps that ordering -- the campaign waits until the interactive task is completed --
    by refusing to build the request until it is, and by dropping the satisfied edge before
    the capsule graph is built, since the dependency's own capsule never exists to point to.
    """
    by_id = {row["id"]: row for row in tasks}
    campaign_rows = []
    for row in tasks:
        if row.get("lane", work.DEFAULT_LANE) != work.DEFAULT_LANE:
            continue
        interactive_deps = [
            dep for dep in row["depends_on"]
            if by_id[dep].get("lane", work.DEFAULT_LANE) != work.DEFAULT_LANE
        ]
        unmet = [dep for dep in interactive_deps if by_id[dep]["status"] != "completed"]
        if unmet:
            raise work.WorkError(
                f"Task {row['id']} depends on interactive-lane task {unmet[0]}, which is not "
                "completed yet; complete it with bearhug-work before the campaign starts."
            )
        campaign_rows.append(
            {**row, "depends_on": [dep for dep in row["depends_on"] if dep not in interactive_deps]}
        )
    return campaign_rows


def _require_clean_subject_for_intake(root: Path) -> None:
    """Refuse campaign intake with a named commit step, rather than let a dirty tree from
    completed interactive work surface as a cryptic clean-base failure inside the first lease.

    Every capsule lease requires the subject clean at its own sealed HEAD
    (``campaign/launcher.py``'s ``_require_clean_base``); an interactive task completed through
    bearhug-work writes the tracked ``docs/superpowers/plans/{BOARD.md,LEDGER.md,WORK.json}``, so
    a campaign whose interactive dependencies were just satisfied would otherwise fail its very
    first lease with no obvious cause.
    """
    try:
        result = host_git.run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError) as exc:
        raise work.WorkError(f"cannot inspect Git status for {root}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise work.WorkError(f"git status failed: {detail}")
    if result.stdout:
        raise work.WorkError(
            "Commit the board changes from completed interactive tasks before the campaign can "
            "start (each capsule lease needs a clean tracked tree at its sealed HEAD): "
            + host_git.describe_dirty_status(root, result.stdout)
        )


_GROUNDING_TOOLS = ("memex", "architecture", "graft", "memq", "codebase_memory")

_LIMITS_NOT_RECORDED = ("not recorded",)


def _status_label(status: str) -> str:
    """The human phrase for a machine status; ``grounded`` must never read as a quality claim."""

    return "bindings compiled" if status == "grounded" else status.replace("_", " ")


def _more_suffix(listed_count: int, shown_count: int, *, eligible: object, returned: object) -> str:
    """"(+N more)" derived from the report's own exact counts, never from a capped listing.

    A producer's listed array (``memex.dropped``) may itself be capped well below the true
    dropped total; counting the array's own length once it runs out would silently understate
    the remainder and contradict the eligible/returned numbers already printed earlier on the
    same line. ``eligible``/``returned`` are the ``_selection_shape`` counts covering the same
    population, always exact for the callers of this helper -- when either is missing or not
    an integer, name the fact of "more" without inventing a count for it.
    """

    if listed_count <= shown_count:
        return ""
    if isinstance(eligible, int) and isinstance(returned, int):
        remainder = eligible - returned - shown_count
        return f" (+{remainder} more)"
    return " (and more)"


def _memex_limit_lines(memex: Mapping) -> list[str]:
    lines: list[str] = []
    selection = memex.get("selection")
    if isinstance(selection, Mapping) and selection.get("truncated"):
        eligible, returned = selection.get("eligible_count"), selection.get("returned_count")
        line = (
            f"memex: {returned} of {eligible} eligible decisions returned "
            f"(limit {selection.get('limit')})"
        )
        ids = [
            row.get("id") for row in (memex.get("dropped") or [])
            if isinstance(row, Mapping) and row.get("id")
        ]
        if ids:
            shown = ids[:5]
            more = _more_suffix(len(ids), len(shown), eligible=eligible, returned=returned)
            line += f"; dropped {', '.join(shown)}{more}"
        if memex.get("tie_at_cut") and memex.get("tie_break_rule"):
            line += f"; the cut fell inside a tied score group ({memex['tie_break_rule']})"
        lines.append(line)
    skipped = memex.get("skipped")
    if isinstance(skipped, Mapping) and skipped.get("count"):
        count = skipped["count"]
        listed = skipped.get("listed") or []
        reasons = sorted({
            row.get("reason") for row in listed
            if isinstance(row, Mapping) and row.get("reason")
        })
        if reasons:
            detail = f" ({', '.join(reasons)}"
            if isinstance(count, int) and count > len(listed):
                detail += f"; reasons from the first {len(listed)} listed"
            detail += ")"
        else:
            detail = ""
        noun = "file" if count == 1 else "files"
        lines.append(f"memex: {count} decision {noun} skipped{detail}")
    return lines


def _architecture_limit_lines(architecture: Mapping) -> list[str]:
    lines: list[str] = []
    gap_selection = architecture.get("gap_selection")
    if isinstance(gap_selection, Mapping):
        gap_count = gap_selection.get("eligible_count")
        if isinstance(gap_count, int) and gap_count > 0:
            if gap_selection.get("truncated"):
                # The gap list itself is capped (MAX_ARCHITECTURE_GAPS); the sealed row and
                # everything downstream of it only ever carries `returned_count` of these.
                lines.append(
                    f"architecture: {gap_selection.get('returned_count')} of {gap_count} "
                    f"gaps recorded (limit {gap_selection.get('limit')})"
                )
            else:
                noun = "gap" if gap_count == 1 else "gaps"
                lines.append(f"architecture: {gap_count} {noun} recorded")
    selection = architecture.get("selection")
    if isinstance(selection, Mapping):
        basis = selection.get("count_basis")
        if basis == "lower_bound":
            lines.append("architecture: records count is a lower bound")
        elif basis == "unknown" and architecture.get("status") in {"ok", "no_match"}:
            # Only worth a line when the tool actually ran (a refused term mixed with a
            # genuine match); a fully unavailable index already says so via `tools`.
            lines.append("architecture: records count is unknown")
        elif basis == "exact" and selection.get("truncated"):
            lines.append(
                f"architecture: {selection.get('returned_count')} of "
                f"{selection.get('eligible_count')} records returned "
                f"(limit {selection.get('limit')})"
            )
    return lines


def _tool_limit_lines(name: str, tool: Mapping, *, queried: int) -> list[str]:
    """Graft and MemQ share the same coverage fields; render them identically by name."""

    lines: list[str] = []
    selection = tool.get("selection")
    if isinstance(selection, Mapping) and selection.get("truncated") is True:
        lines.append(
            f"{name}: kept {selection.get('returned_count')} of at least "
            f"{tool.get('local_candidates')} candidate hits (limit {selection.get('limit')})"
        )
    excluded = tool.get("excluded_count")
    if isinstance(excluded, int) and excluded > 0:
        noun = "hit" if excluded == 1 else "hits"
        lines.append(f"{name}: {excluded} {noun} excluded (non-matching pointer or archived)")
    at_limit = tool.get("queries_at_limit") or []
    per_query_limit = tool.get("per_query_limit")
    if at_limit and per_query_limit is not None:
        lines.append(
            f"{name}: pre-cap total unknown; {len(at_limit)} of {queried} queries reached "
            f"the per-query limit {per_query_limit}"
        )
    hits_by_term = tool.get("hits_by_term") or []
    if hits_by_term:
        # Neither tool reports a total beyond what one query returned, so a query that reached
        # its own per-query cap is named "at least"; a query below the cap is exact.
        parts = [
            f"{row.get('term')}: at least {row.get('returned')}"
            if row.get("at_least") else f"{row.get('term')}: {row.get('returned')}"
            for row in hits_by_term if isinstance(row, Mapping)
        ]
        if parts:
            lines.append(f"{name}: hits per query — {'; '.join(parts)}")
    failed = tool.get("failed_queries") or []
    if failed:
        terms = sorted({
            row.get("term") for row in failed if isinstance(row, Mapping) and row.get("term")
        })
        noun = "query" if len(failed) == 1 else "queries"
        label = "term" if len(terms) == 1 else "terms"
        lines.append(f"{name}: {len(failed)} {noun} failed ({label}: {', '.join(terms)})")
    clipped = tool.get("clipped_queries") or []
    if clipped:
        terms = sorted(clipped)
        noun = "query" if len(clipped) == 1 else "queries"
        label = "term" if len(terms) == 1 else "terms"
        lines.append(
            f"{name}: {len(clipped)} {noun} produced clipped output ({label}: {', '.join(terms)})"
        )
    # `partial` is exactly "failed or clipped" today; a dedicated line only earns its place
    # when neither more specific line above already said so, and stays available as a
    # catch-all for a future reason this function does not yet name.
    if tool.get("partial") and not failed and not clipped:
        lines.append(f"{name}: partial result")
    return lines


_TUNABLE_LIMIT_DEFAULTS = {
    "max_tool_terms": grounding.MAX_TOOL_TERMS,
    "max_decisions": grounding.MAX_DECISIONS,
    "graft_per_query_hits": grounding.GRAFT_PER_QUERY_HITS,
    "max_tool_hits": grounding.MAX_TOOL_HITS,
    "task_terms_first": grounding.TASK_TERMS_FIRST_DEFAULT,
}

#: Keys of the settable grounding options that persist across a same-project re-onboard the same
#: way ``--model``/``--effort`` already do (``_onboard_current``'s options merge). Kept as its
#: own tuple, not derived from ``_TUNABLE_LIMIT_DEFAULTS``, since a future settable option might
#: not want the same persistence.
STICKY_GROUNDING_OPTION_KEYS = (
    "max_tool_terms", "max_decisions", "graft_per_query_hits", "max_tool_hits",
    "task_terms_first",
)


def _effective_limit_lines(
    limits_config: Mapping, *, carried_over: frozenset[str] = frozenset()
) -> list[str]:
    """Name every operator-settable grounding limit that ran at other than its default.

    ``limits_config`` always carries the effective value of all five settable options, defaults
    included; a line here means this onboarding proposal used a non-default value, so the
    dashboard/``status`` text never leaves a raised or lowered limit unstated. ``carried_over``
    names which of those non-default values were not given on the most recent onboard command
    line and instead persisted from an earlier one (the same way ``--model``/``--effort`` already
    persist) -- named explicitly rather than left to read as "you just set this".
    """

    lines: list[str] = []
    for name, default in _TUNABLE_LIMIT_DEFAULTS.items():
        value = limits_config.get(name)
        # bool is an int subclass in Python, so this one check also covers task_terms_first.
        if isinstance(value, int) and value != default:
            suffix = "; carried over from the previous onboarding" if name in carried_over else ""
            lines.append(f"{name}: {value} (default {default}){suffix}")
    return lines


def _report_limits(
    report: Mapping, *, carried_over: frozenset[str] = frozenset()
) -> tuple[list[str], bool]:
    """Short, truthful limit lines built only from fields an older report never recorded.

    ``source_selection`` is unconditionally present on every compile regardless of
    which tools were consulted or what they found, so its absence is the one reliable signal
    that this report predates the truthful-caps work entirely. ``carried_over`` is forwarded to
    ``_effective_limit_lines``; see its docstring.
    """

    if not isinstance(report.get("source_selection"), Mapping):
        return list(_LIMITS_NOT_RECORDED), False
    lines: list[str] = []
    memex = report.get("memex")
    if isinstance(memex, Mapping):
        lines += _memex_limit_lines(memex)
    architecture = report.get("architecture")
    if isinstance(architecture, Mapping):
        lines += _architecture_limit_lines(architecture)
    tool_terms = report.get("tool_terms")
    queried = len(tool_terms.get("queried") or []) if isinstance(tool_terms, Mapping) else 0
    for name in ("graft", "memq"):
        tool = report.get(name)
        if isinstance(tool, Mapping):
            lines += _tool_limit_lines(name, tool, queried=queried)
    term_selection = report.get("term_selection")
    if isinstance(term_selection, Mapping) and term_selection.get("truncated"):
        lines.append(
            f"terms: {term_selection.get('returned_count')} of "
            f"{term_selection.get('eligible_count')} candidate terms extracted "
            f"(limit {term_selection.get('limit')})"
        )
    if isinstance(tool_terms, Mapping) and tool_terms.get("truncated"):
        line = (
            f"tool terms: {tool_terms.get('returned_count')} of "
            f"{tool_terms.get('eligible_count')} candidate terms were queried"
        )
        not_queried = tool_terms.get("not_queried") or []
        if not_queried:
            shown = not_queried[:5]
            eligible = tool_terms.get("eligible_count")
            returned = tool_terms.get("returned_count")
            more = _more_suffix(len(not_queried), len(shown), eligible=eligible, returned=returned)
            line += f"; not queried: {', '.join(shown)}{more}"
        lines.append(line)
    limits_config = report.get("limits_config")
    if isinstance(limits_config, Mapping):
        lines += _effective_limit_lines(limits_config, carried_over=carried_over)
    source_selection = report.get("source_selection")
    if isinstance(source_selection, Mapping) and source_selection.get("truncated"):
        line = (
            f"packet sources: {source_selection.get('returned_count')} of "
            f"{source_selection.get('eligible_count')} included "
            f"(limit {source_selection.get('limit')})"
        )
        dropped_ids = source_selection.get("dropped_source_ids")
        if isinstance(dropped_ids, list) and dropped_ids:
            shown = dropped_ids[:5]
            # `dropped_source_ids` is never capped by the producer (unlike memex's `dropped`),
            # so its own length is already the true remainder; no eligible/returned math needed.
            more = f" (+{len(dropped_ids) - 5} more)" if len(dropped_ids) > 5 else ""
            line += f"; dropped {', '.join(shown)}{more}"
        lines.append(line)
    for row in report.get("inline") or []:
        if not isinstance(row, Mapping):
            continue
        if row.get("outcome") == "omitted":
            lines.append(
                f"inline omitted: {row.get('source_id')} ({row.get('bytes')} bytes > "
                f"{row.get('limit')})"
            )
        elif row.get("outcome") == "trimmed":
            lines.append(
                f"inline trimmed: {row.get('source_id')} ({row.get('rows_before')} rows cut "
                f"to {row.get('rows_after')})"
            )
    return lines, True


def _freshness_suffix(tool: object) -> str:
    """Render one index's sealed-at-onboard age, or its absence, for the tool summary."""
    from bearhug.project_knowledge import age_label, age_seconds

    if not isinstance(tool, dict):
        return ""
    if tool.get("status") in {"absent", "unreported"}:
        return f" ({tool['status']})"
    parts = []
    if tool.get("kind") == "generated" and tool.get("status") not in {"indexed", "built", "fresh"}:
        parts.append(str(tool["status"]))
    age = age_seconds(tool.get("updated_at"))
    if age is not None:
        verb = "indexed " if tool.get("kind") == "generated" else "updated "
        parts.append(verb + age_label(age))
    return f" ({', '.join(parts)})" if parts else ""


def grounding_summary(grounding: object, *, carried_over: frozenset[str] = frozenset()) -> dict:
    """Summarize the draft's recorded grounding for the dashboard; never re-run the compiler.

    States: ``grounded`` (decision bindings exist), ``no_match`` (tools answered, nothing bound),
    ``unavailable`` (every consulted tool failed or was absent), ``not_recorded`` (draft predates
    grounding). Tool statuses are copied verbatim from the compiler report. ``status_label`` is the
    human phrase for ``status``; ``grounded`` reads as "bindings compiled", never as a coverage or
    correctness claim.

    ``limits`` carries one line per recorded limit kind that actually fired, in this fixed order:
    memex decisions dropped by the cap (with ids, and the tie-break rule when the cut fell inside
    a tied score group) or skipped (with reasons); architecture gaps recorded and their own list
    truncation; the architecture record count's lower-bound or unknown basis, or its own
    truncation; per tool (Graft, then MemQ) the local hit cap, excluded hits, the per-query limit,
    per-query hit counts, failed queries, clipped queries and a partial-result fallback; the term
    and tool-term extraction caps (naming the terms that were not queried); any operator-settable
    limit that ran at other than its default value, named as "carried over from the previous
    onboarding" when ``carried_over`` (a key of ``project_campaign._onboard_current``'s persisted
    ``options_carried_over``) says this onboard command line did not set it itself; the packet
    source cap with any dropped source ids; and an inline observation trimmed or omitted from the
    packet. Every number is read from the report, never inferred. Anything else the report
    records (raw hits, term weights, freshness) is visible in the report itself, not in this
    summary. ``limits_recorded`` is ``False`` only for a report from before this work, where
    nothing can be truthfully inferred.
    """

    if not isinstance(grounding, dict):
        return {
            "status": "not_recorded",
            "status_label": _status_label("not_recorded"),
            "reason": "This draft predates grounding; refresh it with bearhug-campaign onboard.",
            "tools": {},
            "tool_summary": "not recorded",
            "decisions": [],
            "bindings": 0,
            "invariants": 0,
            "sources": 0,
            "limits": list(_LIMITS_NOT_RECORDED),
            "limits_recorded": False,
        }
    if grounding.get("status") == "unavailable" or "report" not in grounding:
        return {
            "status": "unavailable",
            "status_label": _status_label("unavailable"),
            "reason": str(grounding.get("reason") or "grounding compiler failed"),
            "tools": {},
            "tool_summary": "unavailable",
            "decisions": [],
            "bindings": 0,
            "invariants": 0,
            "sources": 0,
            "limits": list(_LIMITS_NOT_RECORDED),
            "limits_recorded": False,
        }
    report = grounding.get("report") or {}
    tools = {
        name: str((report.get(name) or {}).get("status") or "not_consulted")
        for name in _GROUNDING_TOOLS
    }
    freshness = (report.get("freshness") or {}).get("tools") or {}
    matched = (report.get("memex") or {}).get("matched") or []
    decisions = [
        f"{row.get('id')} ({row.get('status')})" for row in matched if isinstance(row, dict)
    ]
    bindings = len(grounding.get("bindings") or [])
    invariants = len(grounding.get("invariants") or [])
    sources = len(grounding.get("sources") or [])
    consulted = {name: state for name, state in tools.items() if state != "not_consulted"}
    if bindings:
        status = "grounded"
        # Bindings compiled and sealed is a fact about what ran, not a claim that discovery
        # was complete or correct; see `limits` for what grounding did not or could not check.
        reason = (
            f"{bindings} decision binding(s), {invariants} invariant(s) and {sources} packet "
            "source(s) were compiled and sealed with the draft."
        )
    elif consulted and all(state in {"unavailable", "failed"} for state in consulted.values()):
        status = "unavailable"
        reason = "Every consulted knowledge source was unavailable or failed; see the tool states."
    elif consulted:
        status = "no_match"
        reason = "Project knowledge was consulted but no accepted decision matched the plan."
    else:
        status = "not_consulted"
        reason = "No knowledge source was consulted for this draft."
    limits, limits_recorded = _report_limits(report, carried_over=carried_over)
    return {
        "status": status,
        "status_label": _status_label(status),
        "reason": reason,
        "tools": tools,
        "tool_summary": " · ".join(
            f"{name} {state}" + _freshness_suffix(freshness.get(name))
            for name, state in tools.items()
        ),
        "freshness": freshness,
        "decisions": decisions,
        "bindings": bindings,
        "invariants": invariants,
        "sources": sources,
        "limits": limits,
        "limits_recorded": limits_recorded,
    }


def onboarding_status(root: Path) -> dict:
    """Read the prepared choices; dashboard refreshes never run setup or providers."""
    from bearhug.project_knowledge import knowledge_freshness, with_ages

    path = work._path(root, ONBOARDING)
    # Live index state with ages, read now; the sealed grounding block carries no clock reading.
    knowledge = with_ages(knowledge_freshness(root))
    if not path.is_file():
        return {
            "status": "pending",
            "reason": "Project execution setup will be prepared by the next assistant hook.",
            "next_action": (
                "To prepare it now, run scripts/bin/bearhug-campaign onboard "
                "--provider <claude|codex>."
            ),
            "knowledge": knowledge,
        }
    saved = json.loads(path.read_text())
    draft = saved["draft"]
    state = work._load(root)
    if not state or draft.get("plan_sha256") != state["plan"]["sha256"]:
        return {
            "status": "stale",
            "reason": "The accepted plan changed; refresh its proposed execution settings.",
            "next_action": (
                f"Run scripts/bin/bearhug-campaign onboard --provider {saved['provider']}."
            ),
            "knowledge": knowledge,
        }
    blockers = draft.get("blockers", [])
    qualification = draft["qualification"]
    questions = [item for item in blockers if item != qualification.get("reason")]
    ready = not blockers
    carried_over = frozenset(saved.get("options_carried_over") or ())
    return {
        "status": "needs_input"
        if questions
        else "qualification_unavailable"
        if blockers
        else "awaiting_approval",
        "reason": (
            " ".join(blockers)
            if blockers
            else "Project settings are ready for review and approval."
        ),
        "provider": draft["provider"],
        "model": draft["model"],
        "effort": draft["effort"],
        "validation_commands": draft["validation_commands"],
        "spend_limit": "unrestricted",
        "questions": questions,
        "qualification": qualification,
        "grounding": grounding_summary(draft.get("grounding"), carried_over=carried_over),
        "knowledge": knowledge,
        "draft_path": ONBOARDING,
        "draft_sha256": draft["draft_sha256"],
        "next_action": (
            "After reviewing the proposed settings, run scripts/bin/bearhug-campaign onboard "
            f"--provider {saved['provider']} --approve-sha256 {draft['draft_sha256']}."
            if ready
            else "Supply only missing project choices with bearhug-campaign onboard --model MODEL "
            "--effort EFFORT --validate 'COMMAND' and the same --provider. "
            "Provider qualification is a Bear Hug product prerequisite; do not invent evidence."
            if questions
            else (
                "Qualify this provider: run .venv/bin/python tests/qualify_claude_provider.py "
                "--publish --retire-stale --install-into <project> from the Bear Hug checkout, "
                "then run onboard again. Installed hooks and a synchronized task list do not "
                "establish qualification."
                if draft["provider"] == "claude"
                else "Qualify this provider through the route docs/PROVIDERS.md documents for "
                "Codex, then run onboard again. Installed hooks and a synchronized task list do "
                "not establish qualification."
            )
        ),
    }


def _generated_profile(root: Path, saved: dict) -> bool:
    path = work._path(root, PROFILE)
    return (
        path.is_file()
        and saved.get("profile_sha256") == hashlib.sha256(path.read_bytes()).hexdigest()
    )


def _profile_change_requested(args, saved: dict) -> bool:
    """Recognize an explicit reviewed choice that should replace a settled profile."""
    return (
        saved.get("replacement_pending") is True
        or saved.get("provider") != args.provider
        or any(
            getattr(args, key, None) is not None
            for key in (
                "model",
                "effort",
                "validate",
                "qualification_index",
                "state_root",
                "lease_paths",
                "lease_probe",
                "allowed_programs",
            )
        )
    )


_SETTLE_SEQUENCE = (
    ("stop", "scripts/bin/bearhug-campaign control stop"),
    ("recover", "scripts/bin/bearhug-campaign recover --recovery-outcome failed"),
    ("retire", "scripts/bin/bearhug-campaign retire"),
)

# The only two `report()` statuses `_status_body` itself treats as needing extra recovery
# detail (interrupted-provider-process and failed-episode projection, ~line 1417) rather than
# an ordinary in-progress read. `failed` is a worker that ended badly; `blocked` also covers a
# phase review that finished without validating the combined system (`capsule_campaign.py`
# saves status="blocked" for exactly that outcome). Neither is `stopped` (handled separately,
# above) and neither is a live status (`running`, `preflighted`, `checkpoint`, `integrating`,
# `phase_review`, `prepared`) that a worker could still resume on its own.
_NON_LIVE_TERMINAL_STATUSES = frozenset({"failed", "blocked"})


def _remaining_settle_steps(session: Mapping[str, Any]) -> str:
    """Name every step still owed, not only the one that refused.

    Each condition below used to speak for itself alone, so a refusal said what was
    wrong and never where in the sequence the operator stood. Measured 2026-09-17: a
    campaign needed stop, then recover, then retire, and three separate refusals were
    read as three separate problems. One of them said "recovery is not needed" while
    custody was still held, and another said "wait for the worker" about a worker that
    had lived 0.27 seconds.

    A durably failed, already-stopped session whose lease is still held is a further,
    narrower case this must not fall into the ordinary "recover" branch for: plain
    `recover --recovery-outcome failed` only ever answers `recovery_not_needed` for it
    (`_recover_current`'s short-circuit launches no worker), so naming it here would send
    the operator straight back into that refusal. `control stop` is re-runnable and is the
    command that actually releases this custody, so it is named again even though `stopped`
    is already true, followed by the documented `recover --lease-id` route naming the exact
    lease.
    """

    if (
        session.get("stopped")
        and session.get("failed_episode")
        and not session.get("unresolved_spend")
        and (session.get("custody_active") or session.get("active_leases", 0))
    ):
        lease_ids = session.get("active_lease_ids") or []
        lease_id = lease_ids[0]["lease_id"] if lease_ids else None
        lines = ["scripts/bin/bearhug-campaign control stop"]
        if lease_id:
            lines.append(f"scripts/bin/bearhug-campaign recover --lease-id {lease_id}")
        lines.append("scripts/bin/bearhug-campaign retire")
        return " Still owed, in this order: " + "; then ".join(lines) + "."
    owed: list[str] = []
    if not session.get("stopped"):
        owed.append("stop")
    if (
        session.get("custody_active")
        or session.get("unresolved_spend")
        or session.get("active_leases", 0)
    ):
        owed.append("recover")
    owed.append("retire")
    lines = [command for name, command in _SETTLE_SEQUENCE if name in owed]
    if "recover" in owed:
        episode = session.get("active_episode")
        if isinstance(episode, str) and episode:
            lines = [
                f"{line} --episode-id {episode}" if "recover" in line else line
                for line in lines
            ]
    return " Still owed, in this order: " + "; then ".join(lines) + "."


def _worker_detail(session: Mapping[str, Any]) -> str:
    """Say which operation holds the campaign and for how long, not merely that one does."""

    worker = session.get("worker")
    if not isinstance(worker, Mapping):
        return ""
    operation = worker.get("operation")
    started = worker.get("started_at")
    parts = []
    if isinstance(operation, str) and operation:
        parts.append(f"operation {operation}")
    if isinstance(started, (int, float)):
        parts.append(f"running {max(0.0, time.time() - started):.0f}s")
    return f" ({', '.join(parts)})" if parts else ""


def _settled_attached_session(
    root: Path, *, intake_locked: bool = False, config=None
) -> dict | None:
    """Check that a previous campaign is stopped and all provider custody is released."""
    link = binding(root)
    session_id = link.get("session_id")
    if not session_id:
        return None
    config = config or profile(root)

    def check() -> dict:
        session = terminal._load_session(config, session_id)
        if session is None or not session.get("locator"):
            raise work.WorkError("The attached campaign session has no recoverable locator.")
        if terminal._worker_active(session):
            raise work.WorkError(
                "The attached campaign worker is still running"
                + _worker_detail(session)
                + "; wait for it, then re-run this."
                + _remaining_settle_steps(session)
            )
        terminal._refresh_session(config, session)
        if terminal._worker_active(session):
            raise work.WorkError(
                "The attached campaign worker is still running"
                + _worker_detail(session)
                + "; wait for it, then re-run this."
                + _remaining_settle_steps(session)
            )
        if not session.get("stopped"):
            raise work.WorkError(
                "The attached campaign is not stopped; evidence remains attached."
                + _remaining_settle_steps(session)
            )
        if (
            session.get("custody_active")
            or session.get("unresolved_spend")
            or session.get("active_leases", 0)
        ):
            raise work.WorkError(
                "The attached campaign still owns provider custody. A resolved episode "
                "boundary does not release it, and status may still read that recovery "
                "is not needed."
                + _remaining_settle_steps(session)
            )
        locator = terminal._exact_locator(session["locator"], config.private_root)
        prepared = load_prepared(locator, recovery=True)
        if Path(prepared.record["subject"]["path"]) != root:
            raise work.WorkError("Campaign locator belongs to another worktree.")
        campaign = CapsuleCampaign(prepared, read_only=True)
        if campaign.state and campaign.state.get("accepted"):
            raise work.WorkError(
                "The attached campaign has accepted capsule work; continue it under "
                "its existing profile."
            )
        terminal._save_session(config, session)
        return session

    if intake_locked:
        return check()
    with terminal._intake_lock(config):
        return check()


def _retire_attached_session(root: Path, *, intake_locked: bool = False, config=None) -> None:
    """Detach only a fully settled run while retaining its exact session and locator record."""
    link = binding(root)
    session_id = link.get("session_id")
    if not session_id:
        return
    config = config or profile(root)

    def retire() -> None:
        session = _settled_attached_session(root, intake_locked=True, config=config)
        if session is None:  # pragma: no cover - guarded by session_id above
            return
        # Mark this exact request as superseded by the retirement: a resubmission of the
        # identical bytes (same session id, same accepted plan text -- a provider re-onboard
        # changes neither) is a genuinely new request for a fresh dispatch, not a repeat of
        # the one this retirement just closed out. TerminalDriver's own duplicate-request
        # check (`_user_prompt`) reads this field. Purely additive: it does not change how
        # any request digest is computed, and an older session record without this field
        # (from before this change) is read back as `None`, which never equals a digest, so
        # its pre-existing duplicate-detection behaviour is unaffected across an upgrade.
        session["superseded_request_sha256"] = session.get("request_sha256")
        terminal._save_session(config, session)
        history = link.get("retired_campaigns", [])
        if not isinstance(history, list):
            raise work.WorkError("Campaign retired history is malformed.")
        history.append(
            {
                "session_id": session["session_id"],
                # Recorded so a later start from this exact session, for this exact task, its
                # exact then-current accepted plan and its exact then-installed profile, can be
                # told to onboard again instead of silently re-deriving the same request and
                # landing back on this retired campaign's own prepared state root. A changed
                # plan (a successor revision) is a different request and is not matched by this
                # history entry; neither is a genuine re-onboard and re-approval, since that
                # installs a different profile and changes profile_sha256 -- the whole point of
                # the recovery route this refusal itself names.
                "task_id": link.get("task_id"),
                "plan_sha256": link.get("plan_sha256"),
                "profile_sha256": link.get("profile_sha256"),
                "locator": session["locator"],
                "status": session.get("status"),
                "reason": session.get("reason"),
                "retired_at": work._now(),
            }
        )
        link["retired_campaigns"] = history
        link.pop("session_id", None)
        link.pop("task_id", None)
        work._write_json(work._path(root, BINDING), link)

    if intake_locked:
        retire()
    else:
        with terminal._intake_lock(config):
            retire()


def _git_common_dir(root: Path) -> Path:
    result = host_git.run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode != 0:
        raise work.WorkError("cannot read this worktree's Git common directory")
    return Path(result.stdout.decode("utf-8", errors="replace").strip()).resolve()


def _session_records(config) -> list[dict]:
    directory = config.private_root / "sessions"
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink():
            continue
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return records


_RECOVERABLE_TERMINAL_STATUSES = frozenset({"failed", "blocked"})


def _session_settled(
    config, session: Mapping[str, Any], *, has_accepted_work: bool
) -> tuple[bool, str | None]:
    """Whether this exact session no longer needs any of its worktrees.

    Takes the raw session record already read by `_session_records` (never re-reads it
    through `terminal._load_session`, which refuses a session recorded under a provider
    other than the profile's *current* one -- exactly what a project reconfigured from one
    provider to another leaves behind, and `_refresh_session` needs no provider match to
    answer this).

    Settled requires, first, what `terminal._session_active` already means by "not active":
    no worker running, no custody held (`custody_active`, `unresolved_spend`, an active
    lease). Past that, `has_accepted_work` -- whether ANY of this session's launches was
    ever accepted (`worktree_cleanup.launch_records`'s own `accepted` flag, computed by the
    caller across every one of this session's records) -- decides everything else, because
    Bear Hug records no field anywhere that says accepted work was later integrated: a
    phase-validated campaign's own combined-integration worktree is not itself listed
    `accepted` (only the individual capsules are), but its own replay still needs it exactly
    as long as any of those capsules' work is unintegrated. So accepted work, of any kind,
    makes the whole session unsettled, regardless of status or `stopped` -- there is no
    status this module can read that proves later integration happened. Absent accepted
    work, settled means: explicitly `control stop`-ed, or a durably recovered failure/block
    (`_RECOVERABLE_TERMINAL_STATUSES`; custody being released above already establishes
    "recovered"), or one of Git's own terminal statuses (`terminal.TERMINAL_STATUSES`) --
    completed, integrated by name, or superseded, with nothing left it could still need.
    Anything else -- an unclassified status, or a terminal status *with* accepted work -- is
    kept, never guessed into settled.
    """

    try:
        refreshed = terminal._refresh_session(config, dict(session))
    except Exception as exc:  # noqa: BLE001 - surfaced as a kept reason, not a crash
        return False, f"its settled status cannot be confirmed: {exc}"
    if terminal._worker_active(refreshed):
        return False, "its campaign worker is still running"
    if (
        refreshed.get("custody_active")
        or refreshed.get("unresolved_spend")
        or refreshed.get("active_leases", 0)
    ):
        return False, "its campaign still holds provider custody"
    if has_accepted_work:
        return False, (
            "its campaign has accepted capsule work with no record of being integrated"
        )
    if refreshed.get("stopped"):
        return True, None
    status = refreshed.get("status")
    if status in _RECOVERABLE_TERMINAL_STATUSES or status in terminal.TERMINAL_STATUSES:
        return True, None
    return False, "its campaign has not been stopped; run `bearhug-campaign control stop`"


def _discover_launch_records(root: Path, config) -> list[dict]:
    """Every capsule worktree launch from every session this project ever bound.

    Each record also carries the ``session_id`` it belongs to, the raw session record (for
    `_session_settled`, which must not re-read it through `terminal._load_session`), and
    that campaign's ``common_dir``, so a caller can decide, per session, whether the
    worktree is still part of a live campaign before treating it as a removal candidate.
    """

    records: list[dict] = []
    for session in _session_records(config):
        session_id = session.get("session_id")
        locator = session.get("locator")
        if not session_id or not isinstance(locator, str):
            continue
        try:
            exact = terminal._exact_locator(locator, config.private_root)
            prepared = load_prepared(exact, recovery=True)
        except Exception:  # noqa: BLE001 - an unreadable/foreign locator is not this root's
            continue
        if Path(prepared.record["subject"]["path"]) != root:
            continue
        try:
            loaded = CapsuleCampaign(prepared, read_only=True)
        except Exception:  # noqa: BLE001 - corrupt campaign state names nothing to remove
            continue
        for record in worktree_cleanup.launch_records(loaded):
            records.append(
                {
                    **record,
                    "session_id": session_id,
                    "session": session,
                    "common_dir": prepared.record["subject"]["common_dir"],
                }
            )
    return records


def cleanup_after_retire(root: Path, session_id: str, config, *, keep: bool) -> list:
    """Remove (or, with `keep`, name and leave) the campaign that just retired its worktrees.

    Every accepted-work refusal is already enforced by `_settled_attached_session` before a
    retire can happen at all, so every launch this exact session made is eligible.
    """

    records = [r for r in _discover_launch_records(root, config) if r["session_id"] == session_id]
    if not records:
        return []
    common_dir = Path(records[0]["common_dir"])
    return worktree_cleanup.plan_removal(
        subject=root, common_dir=common_dir, records=records, protect_all=keep,
    )


def plan_prune(root: Path, config, *, apply: bool) -> list:
    """Find (and, with `apply`, remove) leftover Bear Hug lease worktrees from older runs.

    A worktree is a candidate only once its own session agrees it is settled (see
    `_session_settled`) -- a live or still-resumable campaign's worktree is named and kept,
    never removed, regardless of whether it is currently attached to this project. A `bh-*`
    branch with no loadable custody registration is reported, never removed: name-matching
    alone never proves Bear Hug's custody (see `worktree_cleanup.plan_removal`). Finally, a
    Bear Hug-owned worktree whose own *directory* is already gone (a stale Git admin entry)
    is swept per entry, never through a repository-wide `git worktree prune` -- that command
    also expires a user's own worktree (losing its index) the moment its directory is
    briefly unavailable, with nothing printed; see `worktree_cleanup.
    remove_missing_registered_entries`.
    """

    all_records = _discover_launch_records(root, config)
    sessions_with_accepted_work = {
        record["session_id"] for record in all_records if record.get("accepted")
    }
    settled_cache: dict[str, tuple[bool, str | None]] = {}
    removable: list[dict] = []
    actions: list = []
    common_dir: Path | None = None
    for record in all_records:
        session_id = record["session_id"]
        if session_id not in settled_cache:
            settled_cache[session_id] = _session_settled(
                config, record["session"],
                has_accepted_work=session_id in sessions_with_accepted_work,
            )
        settled, reason = settled_cache[session_id]
        common_dir = common_dir or Path(record["common_dir"])
        if not settled:
            actions.append(
                worktree_cleanup.kept(record.get("worktree"), record.get("branch"), reason)
            )
            continue
        removable.append(record)

    if common_dir is None:
        try:
            common_dir = _git_common_dir(root)
        except work.WorkError:
            common_dir = None

    if common_dir is not None:
        actions.extend(
            worktree_cleanup.plan_removal(
                subject=root, common_dir=common_dir, records=removable, dry_run=not apply,
            )
        )
        actions.extend(
            worktree_cleanup.remove_missing_registered_entries(
                subject=root, common_dir=common_dir, records=removable, dry_run=not apply,
            )
        )
        try:
            registry = [
                r["registration"] for r in all_records if r.get("registration") is not None
            ]
            inventory = inventory_worktrees(subject=root, common_dir=common_dir, registry=registry)
        except WorktreeInventoryError:
            inventory = None
        if inventory is not None:
            known = {Path(r["worktree"]) for r in all_records if r.get("worktree")}
            for entry in inventory.entries:
                if entry.prunable:
                    continue  # already covered by remove_missing_registered_entries above
                if entry.custody == "unregistered" and entry.path not in known:
                    actions.append(
                        worktree_cleanup.kept(
                            entry.path, entry.branch,
                            "it has a bh-* branch but no Bear Hug custody registration was "
                            "found for it; a name match alone is not enough",
                        )
                    )
    return actions


def onboard(root: Path, args) -> dict:
    """Prepare or approve settings through the existing campaign contracts."""
    with work._campaign_authority_locked(root):
        return _onboard_current(root, args)


def _draft_under_approval(saved: dict, args, options: dict, *, redo: bool):
    """The saved draft ``--approve-sha256`` names, when nothing about it has changed.

    Deriving again to approve re-runs the lease evidence probe, and the draft digest
    covers what that probe reports -- the declared lease paths and the warnings a failed
    or timed-out probe appends. A second probe that observes anything differently
    therefore produces a digest the operator is not holding, and the approval they were
    told to run cannot land. Approving the reviewed draft removes both the second probe
    and that race.

    ``None`` means the proposal must be derived again: a different provider or options,
    an older derivation, or drifted qualification evidence. The rebuild itself is
    digest-verified, so a saved draft that does not hash to the digest it claims is
    refused rather than quietly re-derived.
    """

    from bearhug import project_onboarding

    if not args.approve_sha256 or redo:
        return None
    if saved.get("provider") != args.provider:
        return None
    if (saved.get("options") or {}) != options:
        return None
    draft = saved.get("draft")
    if not isinstance(draft, dict) or draft.get("draft_sha256") != args.approve_sha256:
        return None
    return project_onboarding.draft_from_mapping(draft)


def _onboard_current(root: Path, args) -> dict:
    from bearhug import project_onboarding

    saved_path = work._path(root, ONBOARDING)
    saved = json.loads(saved_path.read_text()) if saved_path.is_file() else {}
    state = work._load(root)
    generated = _generated_profile(root, saved)
    stale_derivation = saved.get("derivation_version") != project_onboarding.DERIVATION_VERSION
    # A saved proposal that the installed profile was never approved from is unfinished
    # work, not a settled configuration: returning status here swallowed the operator's
    # --approve-sha256 without a word and left the campaign permanently unapprovable.
    pending_draft = (saved.get("draft") or {}).get("draft_sha256")
    unapproved = bool(pending_draft) and pending_draft != _approved_draft_sha256(root)
    drifted = _qualification_drifted(root, saved)
    # An explicit option is a request, whether or not a campaign is attached. This was
    # only consulted through `replacing_settled`, which requires a session_id, so after a
    # retire the early return below swallowed every explicit choice and reported "ready".
    # Measured 2026-09-17: onboard was re-run with a corrected --validate, answered
    # "Profile configured", changed nothing, and left the sealed gate carrying the command
    # the operator had just removed.
    change_requested = bool(generated and _profile_change_requested(args, saved))
    replacing_settled = bool(
        work._path(root, PROFILE).is_file()
        and binding(root).get("session_id")
        and change_requested
    )
    if work._path(root, PROFILE).is_file() and (
        not change_requested
        and not replacing_settled
        and not stale_derivation
        and not unapproved
        and not drifted
        and (
            not generated
            or (state and saved.get("approved_plan_sha256") == state["plan"]["sha256"])
        )
    ):
        return status(root)
    if binding(root).get("session_id") and not replacing_settled:
        raise work.WorkError("Finish or restore the attached campaign before changing its profile.")
    if replacing_settled:
        _settled_attached_session(root)
    previous_options = (
        dict(saved.get("options", {})) if saved.get("provider") == args.provider else {}
    )
    options = dict(previous_options)
    # A settable grounding option not given on *this* command line but present from an earlier
    # one persists (the same sticky merge `--model`/`--effort` already use, just below) rather
    # than resetting to its default. That is easy to miss -- record which options this run's
    # options dict left untouched, so the dashboard can say so explicitly instead of reading as
    # "you just chose this".
    carried_over_keys: set[str] = set()
    for key in (
        "model",
        "effort",
        "qualification_index",
        "state_root",
        "lease_paths",
        "lease_probe",
        "allowed_programs",
        "max_tool_terms",
        "max_decisions",
        "graft_per_query_hits",
        "max_tool_hits",
        "task_terms_first",
    ):
        value = getattr(args, key, None)
        if value is not None:
            options[key] = value
        elif key in STICKY_GROUNDING_OPTION_KEYS and key in previous_options:
            carried_over_keys.add(key)
    if args.validate is not None:
        options["validate"] = [shlex.split(command) for command in args.validate]
    draft = _draft_under_approval(
        saved, args, options, redo=stale_derivation or drifted
    ) or project_onboarding.derive_draft(root, args.provider, **options)
    value = {
        "provider": args.provider,
        "options": options,
        "options_carried_over": sorted(carried_over_keys),
        "draft": draft.review(),
        "derivation_version": project_onboarding.DERIVATION_VERSION,
    }
    if replacing_settled:
        value["replacement_pending"] = True
    for key in ("profile_sha256", "approved_plan_sha256"):
        if key in saved:
            value[key] = saved[key]
    if saved != value:
        work._write_json(saved_path, value)
    retired_worktree_actions: list | None = None
    retired_worktree_error: str | None = None
    if args.approve_sha256:
        if replacing_settled:
            config = profile(root)
            with terminal._intake_lock(config):
                _settled_attached_session(root, intake_locked=True, config=config)
                retiring_session_id = binding(root).get("session_id")
                project_onboarding.approve_draft(
                    draft,
                    expected_sha256=args.approve_sha256,
                    replace_profile_sha256=saved.get("profile_sha256") if generated else None,
                )
                _retire_attached_session(root, intake_locked=True, config=config)
                # Board-driven approval retires a settled campaign the same way the CLI's
                # own `retire` does, including removing its own lease worktrees by default:
                # otherwise this path's leftovers would need `prune` to ever go away,
                # and the RUNBOOK's claim that approval "does this automatically" would be
                # false for the one thing that command actually removes. Every removal or
                # kept item is reported here exactly as the CLI's own `retire` reports it,
                # never silently discarded.
                if retiring_session_id:
                    try:
                        retired_worktree_actions = cleanup_after_retire(
                            root, retiring_session_id, config, keep=False
                        )
                    except (OSError, ValueError, RuntimeError, KeyError) as exc:
                        retired_worktree_error = str(exc)
        else:
            project_onboarding.approve_draft(
                draft,
                expected_sha256=args.approve_sha256,
                replace_profile_sha256=saved.get("profile_sha256") if generated else None,
            )
        value["profile_sha256"] = hashlib.sha256(work._path(root, PROFILE).read_bytes()).hexdigest()
        value["approved_plan_sha256"] = draft.plan_sha256
        value.pop("replacement_pending", None)
        work._write_json(saved_path, value)
        link = binding(root)
        link["enabled"] = True
        work._write_json(work._path(root, BINDING), link)
        result = status(root)
        if retired_worktree_actions is not None:
            for action in retired_worktree_actions:
                verb = "removed" if action.removed else "kept"
                branch = f" [{action.branch}]" if action.branch else ""
                print(f"{verb}: {action.path}{branch} -- {action.reason}")
            result["worktrees"] = [action.as_dict() for action in retired_worktree_actions]
        elif retired_worktree_error is not None:
            result["worktree_cleanup_error"] = retired_worktree_error
        return result
    return onboarding_status(root)


def recover(
    root: Path,
    *,
    outcome: str = "failed",
    episode_id: str | None = None,
    review_id: str | None = None,
    lease_id: str | None = None,
    resolve_orphan: bool = False,
    confirm: str | None = None,
) -> dict:
    """Queue explicit controller recovery without retrying provider execution."""
    with work._campaign_authority_locked(root):
        if resolve_orphan:
            return _resolve_orphaned_lease(root, lease_id=lease_id, confirm=confirm)
        return _recover_current(
            root, outcome=outcome, episode_id=episode_id, review_id=review_id, lease_id=lease_id,
        )


def _resolve_orphaned_lease(root: Path, *, lease_id: str | None, confirm: str | None) -> dict:
    """Release one orphaned lease, the only supported way out of an orphan.

    An orphaned lease is counted as custody, so it blocks `retire`, and every command that
    could release it heartbeats first and is refused. Measured 2026-09-17 on row 240 T2,
    where that left no supported route at all and the campaign was abandoned. The lease
    store's own controller HIL path existed and was tested with no caller; this is it.

    `--confirm` is required and recorded as the HIL answer. It is evidence of who released
    the lease, not a password, and it exists so that releasing another process's claim is a
    decision the operator typed rather than a flag they passed.
    """

    from bearhug.campaign.leases import release_orphaned_lease

    if not lease_id:
        raise work.WorkError("--resolve-orphan requires the exact --lease-id to release.")
    if not confirm or not confirm.strip():
        raise work.WorkError(
            "--resolve-orphan requires --confirm with the reason you are releasing this "
            "lease; it is recorded as the controller HIL answer."
        )
    link = binding(root)
    session_id = link.get("session_id")
    if not session_id:
        raise work.WorkError("No attached campaign owns a lease to resolve.")
    config = profile(root)
    session = terminal._load_session(config, session_id)
    if session is None or not session.get("locator"):
        raise work.WorkError("The attached campaign session has no recoverable locator.")
    if terminal._worker_active(session):
        raise work.WorkError(
            "The attached campaign worker is still running"
            + _worker_detail(session)
            + "; wait for it, then re-run this."
        )
    campaign = CapsuleCampaign(
        load_prepared(terminal._exact_locator(session["locator"], config.private_root),
                      recovery=True),
        read_only=True,
    )
    # The lease store is shared by every campaign of this repository (see
    # campaign/prepared.py's "shared campaign lease root"); a bare lease id says nothing
    # about which campaign it belongs to. Bind it to this attached campaign's own launches
    # before anything below can act on it, so this route can never touch another campaign's
    # lease.
    own_lease_ids = {
        launch.get("lease_id") for launch in (campaign.state or {}).get("launches", {}).values()
    }
    if lease_id not in own_lease_ids:
        raise work.WorkError(
            "That lease does not belong to the attached campaign; refusing to resolve "
            "another campaign's lease."
        )
    store = campaign.leases(writable=True)
    record = store.get(lease_id)
    # A targeted sweep of this one lease only, never the shared store: if it is still
    # active and its own expires_at has already passed, mark it orphaned so the documented
    # release route below can reach it. A no-op otherwise (not yet expired, or already
    # orphaned/released) -- release_orphaned_lease keeps its own state == "orphaned" refusal.
    store.orphan_expired_lease(lease_id)
    # This targeted sweep is the only one this route performs: `sweep_expired=False` turns
    # off the two store-wide sweeps `release_orphaned_lease` would otherwise still run
    # internally, which reach every lease of every campaign sharing this repository's store,
    # not only this one. The lease this call requires to be orphaned already is, from the
    # sweep just above; a store-wide sweep here would only ever touch other campaigns' leases.
    released = release_orphaned_lease(
        store,
        lease_id=lease_id,
        reason="operator resolved orphaned lease from the project terminal",
        answer=confirm.strip(),
        sweep_expired=False,
    )
    return {
        "status": "orphan_resolved",
        "reason": (
            "The orphaned lease is released. Custody no longer counts it, so "
            "scripts/bin/bearhug-campaign retire can now detach this campaign."
        ),
        "lease_id": lease_id,
        "previous_state": record.state,
        "state": released.state,
        "next_action": "scripts/bin/bearhug-campaign retire",
    }


def _recover_current(root, *, outcome, episode_id, review_id, lease_id):
    if outcome not in RECOVERY_OUTCOMES:
        raise work.WorkError(f"Unsupported recovery outcome: {outcome}")
    link = binding(root)
    session_id = link.get("session_id")
    if not session_id:
        raise work.WorkError("No attached campaign can be recovered.")
    config = profile(root)
    if outcome == "failed" and episode_id is None and review_id is None and lease_id is None:
        with _binding_lock(root, config):
            if _reclaim_provisional_intake(root, config, link):
                return {
                    "status": "intake_released",
                    "reason": "Interrupted intake had no accepted request or worker; retry start.",
                    "session_id": session_id,
                }
    with terminal._intake_lock(config):
        if lease_id is not None:
            if episode_id is not None or review_id is not None:
                raise work.WorkError("Lease recovery does not take episode/review ids.")
            return _recover_stopped_lease_owner(root, config, lease_id)
        session = terminal._load_session(config, session_id)
        if session is None or not session.get("locator"):
            raise work.WorkError("The attached campaign session has no recoverable locator.")
        terminal._refresh_session(config, session)
        if terminal._worker_active(session):
            raise work.WorkError("The campaign worker is already running.")
        if session.get("failed_episode") and not session.get("unresolved_spend"):
            return {
                "status": "recovery_not_needed",
                "reason": session["reason"],
                "locator": session["locator"],
            }
        # A courtesy, not the guard: `CapsuleRuntime._recover_episode` refuses this exact
        # combination on its own before touching any state, once the worker it would queue
        # below actually opens the runtime. Checking it here too, through the very same
        # function (`_status_for_locator` -> `_recovery_provider_liveness_refusal`), means the
        # operator sees "blocked" immediately instead of only later in `status.json` after a
        # worker was queued and started for nothing.
        #
        # `_status_for_locator` only ever inspects the first capsule in plan order with an open
        # episode fence, so with two capsules both interrupted this field can name a DIFFERENT
        # episode than the one `--episode-id` selects. Only act on it when the ids agree (or the
        # operator did not name one, matching the pre-existing best-effort behaviour): otherwise
        # this courtesy check would refuse a valid recovery of one capsule by naming another's
        # still-alive pid. The runtime's own chokepoint stays correct either way -- it is scoped
        # to the exact episode being recovered -- so this only fixes the courtesy layer's
        # accuracy, not its safety.
        if outcome == "hil_required":
            interrupted = session.get("interrupted_provider_processes")
            if (
                isinstance(interrupted, Mapping)
                and not interrupted.get("ready_for_hil_required")
                and (episode_id is None or interrupted.get("episode_id") == episode_id)
            ):
                return {
                    "status": "blocked",
                    "reason": interrupted["guidance"],
                    "locator": session["locator"],
                }
        worker = terminal.TerminalDriver(
            config, config_path=work._path(root, PROFILE)
        )._launch_worker(
            session,
            operation="recover",
            recovery_outcome=outcome,
            episode_id=episode_id,
            review_id=review_id,
        )
        session.update(
            worker=dict(worker),
            status="recovering",
            reason=f"campaign recovery ({outcome}) queued",
            last_event_action="recovery_queued",
        )
        terminal._save_session(config, session)
    return {
        "status": "recovering",
        "reason": f"campaign recovery ({outcome}) queued; no provider retry was started",
        "session_id": session_id,
        "locator": session["locator"],
        "worker": worker,
    }


def _append_custody_visibility_if_still_held(root: Path, result: dict) -> None:
    """Show held custody in every status bucket, not only the ones that already compute it.

    A bucket resolved before custody was ever inspected (`needs_requalification`,
    `needs_configuration`, `needs_approval`, `ready`, and the early no-session/no-locator
    branches) must not hide an active lease the attached campaign still holds: the operator
    needs `control stop` named regardless of which bucket won. Never overrides a bucket that
    already computed this itself (the `active_lease_ids` key already present), never changes
    `status` or any existing `reason` text (only appends), and never raises: any failure here
    -- no attached session, no profile yet, an unreadable locator -- leaves the result exactly
    as its own bucket already built it.

    The FIELD is unconditional -- it has no consumer and is pure information -- but the
    SENTENCE is gated on custody held *and* nothing executing: `control stop` ends an episode
    in flight, and the same `reason` text this function builds reaches the running provider's
    own session context (`project_work.context()` splices `campaign["reason"]` into it), so a
    healthy running campaign must never be told, in its own context, to stop itself. The
    discriminator is exactly the one the `running` bucket already computes for its own dead-pid
    check (`not terminal._pid_alive(pid)`, above): a bucket with no live worker -- either no
    `worker` was ever recorded, or its pid no longer answers -- is safe to tell regardless of
    which status bucket it is.
    """

    if "active_lease_ids" in result:
        return
    try:
        link = binding(root)
        session_id = link.get("session_id")
        if not session_id:
            return
        config = profile(root)
        session = terminal._load_session(config, session_id)
        if not session or not session.get("locator"):
            return
        locator = terminal._exact_locator(session["locator"], config.private_root)
        observed = terminal._status_for_locator(str(locator))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, work.WorkError):
        return
    active_lease_ids = observed.get("active_lease_ids") or []
    if not (observed.get("custody_active") or observed.get("active_leases") or active_lease_ids):
        return
    result["active_lease_ids"] = active_lease_ids
    worker = result.get("worker")
    pid = worker.get("pid") if isinstance(worker, Mapping) else None
    live_worker = (
        isinstance(worker, Mapping)
        and worker.get("status") in {"started", "running"}
        and isinstance(pid, int)
        and pid > 0
        and terminal._pid_alive(pid)
    )
    if live_worker:
        return
    result["reason"] = (
        f"{result.get('reason', '')} The attached campaign still holds provider custody. Run "
        "scripts/bin/bearhug-campaign control stop to release it."
    ).strip()


def status(root: Path, *, include_cockpit: bool = False) -> dict:
    """Discover the accepted plan's binding, then show held custody in every bucket."""

    result = _status_body(root, include_cockpit=include_cockpit)
    _append_custody_visibility_if_still_held(root, result)
    return result


def _status_body(root: Path, *, include_cockpit: bool = False) -> dict:
    """Discover only the exact accepted plan's binding; no scanning or latest-run guessing."""
    result = {"status": "unattached", "reason": "No accepted plan; no campaign attached."}
    session = config = None
    try:
        link = binding(root)
        if link.get("enabled") is False:
            return {"status": "opted_out", "reason": "Campaign execution was explicitly disabled."}
        result["retired_campaigns"] = link.get("retired_campaigns", [])
        result["attached"] = bool(link.get("session_id"))
        state = work._load(root)
        if not state:
            if result["attached"]:
                raise work.WorkError(
                    "Bound campaign has no accepted board; restore its board first."
                )
            return result
        result.update(plan_sha256=state["plan"]["sha256"], profile=PROFILE)
        saved_path = work._path(root, ONBOARDING)
        saved = json.loads(saved_path.read_text()) if saved_path.is_file() else {}
        # A proposal that has not been approved is only actionable if its digest is
        # visible: onboard returns status once the draft is current, so without this the
        # operator is told to approve a digest nothing will tell them.
        if _qualification_drifted(root, saved):
            # The installed provider settings or rules moved since this proposal was
            # derived. Dispatch would be accepted and then fail inside the worker with a
            # digest mismatch, so refuse it here where the operator can act.
            return {
                **result,
                "status": "needs_requalification",
                "reason": (
                    "The provider's installed settings or rules changed since this proposal "
                    "was derived. Re-run onboard to re-qualify, then approve the digest it "
                    "reports."
                ),
            }
        pending = (saved.get("draft") or {}).get("draft_sha256")
        if pending and pending != _approved_draft_sha256(root):
            result["pending_draft_sha256"] = pending
            result["pending_approval_command"] = (
                f"onboard --approve-sha256 {pending}"
            )
        needs_refresh = (
            _generated_profile(root, saved)
            and saved.get("approved_plan_sha256") != state["plan"]["sha256"]
        )
        if not work._path(root, PROFILE).is_file() or needs_refresh:
            onboarding = onboarding_status(root)
            return {
                **result,
                "status": "needs_configuration",
                "reason": onboarding["reason"],
                "onboarding": onboarding,
            }
        config = profile(root)
        if not link.get("session_id"):
            if result.get("pending_draft_sha256"):
                # Reporting "ready" while dispatch would refuse states a readiness this
                # campaign does not have.
                return {
                    **result,
                    "status": "needs_approval",
                    "reason": (
                        "The onboarding proposal changed after the installed profile was "
                        "approved. Approve it with "
                        f"{result['pending_approval_command']} before starting."
                    ),
                }
            return {
                **result,
                "status": "ready",
                "reason": (
                    "Profile configured. Starting a ready task prepares this plan through "
                    "the campaign controller; clean Git state and provider evidence "
                    "are checked before execution."
                ),
            }
        if link.get("plan_sha256") != state["plan"]["sha256"]:
            raise work.WorkError("Campaign is bound to a different accepted plan; finish it first.")
        result.update(link)
        session = terminal._load_session(config, link["session_id"])
        if not session or not session.get("locator"):
            unaccepted = not session or not (
                session.get("worker") is not None or session.get("request_sha256") is not None
                or session.get("history")
            )
            provisional = (
                unaccepted and link.get("intake_phase") == "preparing"
                and link.get("profile_sha256")
                == hashlib.sha256(work._path(root, PROFILE).read_bytes()).hexdigest()
            )
            return {
                **result, "attached": not provisional, "status": "blocked",
                "reason": (
                    "Campaign intake interrupted; retry start or recover failed intake."
                    if provisional else
                    "Campaign custody is unavailable; restore its exact profile and original "
                    "session evidence before retrying."
                ),
                "next_action": (
                    "Run scripts/bin/bearhug-campaign recover --recovery-outcome failed."
                    if provisional else
                    "Restore the original private session and profile; do not delete its binding."
                ),
            }
        result.update({key: session.get(key) for key in ("locator", "worker", "reason", "status")})
        plan_text, plan_digest = work._read_plan(root, state["plan"]["path"])
        if plan_digest != state["plan"]["sha256"]:
            raise work.WorkError(
                "Campaign authority differs from the accepted plan's exact contents."
            )
        request_sha = hashlib.sha256(
            _board_request(root, state, work.project_lane_text(plan_text), link["session_id"])
        ).hexdigest()
        if session.get("request_sha256") != request_sha:
            return {
                **result, "status": "intake_pending", "attached": False, "locator": None,
                "reason": (
                    "This accepted plan has no matching campaign request; "
                    "retry starting its ready task."
                ),
            }
        prepared = load_prepared(terminal._exact_locator(session["locator"], config.private_root))
        if Path(prepared.record["subject"]["path"]) != root:
            raise work.WorkError("Campaign locator belongs to another worktree.")
        projected_plan_text = work.project_lane_text(plan_text)
        if prepared.intent["goal"] != " ".join(
            _accepted_prompt(state, projected_plan_text).split()
        ):
            raise work.WorkError(
                "Campaign authority differs from the accepted plan's exact contents."
            )
        runtime_paths = [prepared.root / "capsules", prepared.root / "run-states"]
        if all(not path.exists() and not path.is_symlink() for path in runtime_paths):
            return {**result, "status": "prepared", "reason": "Waiting for campaign worker."}
        campaign = CapsuleCampaign(prepared, read_only=True)
        if campaign.state is None:
            return {**result, "status": "prepared", "reason": "Waiting for campaign worker."}
        report = campaign.report()
        if report["status"] in {"blocked", "failed", "stopped"}:
            recovery_state = terminal._status_for_locator(session["locator"])
            if recovery_state.get("interrupted_provider_processes") is not None:
                result["interrupted_provider_processes"] = recovery_state[
                    "interrupted_provider_processes"
                ]
            if recovery_state.get("failed_episode"):
                report["reason"] = recovery_state["reason"]
                result.update(
                    failed_episode=True,
                    unresolved_spend=recovery_state["unresolved_spend"],
                    active_leases=recovery_state["active_leases"],
                    active_lease_ids=recovery_state.get("active_lease_ids", []),
                    custody_active=recovery_state["custody_active"],
                )
        elif report["status"] == "running":
            # `report()` only replays the controller's own last-persisted state. A worker
            # that dies without a next turn (SIGKILL) never writes a terminal status of its
            # own, so a persisted "running"/"started" pair goes stale forever with nobody to
            # correct it. Measured in pilot G: `kill -9` of the worker pid left `status`
            # reporting `{"status": "running", "worker": {"pid": ..., "status": "started"}}`
            # although `kill -0` on that pid failed. Say so here; the FSM's own "running"
            # bucket and every state, lease, fence and custody record are unchanged -- only
            # this read-only projection gains the truth about the OS process.
            worker = result.get("worker")
            pid = worker.get("pid") if isinstance(worker, Mapping) else None
            if (
                isinstance(worker, Mapping)
                and worker.get("status") in {"started", "running"}
                and isinstance(pid, int)
                and pid > 0
                and not terminal._pid_alive(pid)
            ):
                result["worker"] = {**worker, "alive": False}
                report["reason"] = (
                    f"{report['reason']}; no live process answers for pid {pid}. Run "
                    "scripts/bin/bearhug-campaign control stop, then recover if it names an "
                    "unresolved attempt, then retire."
                )
                # The controller that would have captured this episode's provider call died
                # with the worker above; if it left an unresolved episode behind, this is the
                # exact interrupted attempt `recover --recovery-outcome hil_required` will
                # check before it may proceed. Say so now, from the same function that check
                # uses, rather than only after the operator queues that worker.
                recovery_state = terminal._status_for_locator(session["locator"])
                interrupted = recovery_state.get("interrupted_provider_processes")
                if interrupted is not None:
                    result["interrupted_provider_processes"] = interrupted
                    report["reason"] = f"{report['reason']} {interrupted['guidance']}"
        result.update(
            status=report["status"],
            reason=report["reason"],
            observed_at=work._now(),
            task_states={},
        )
        lease_id = _blocking_lease(report["reason"])
        if lease_id:
            lease_state = campaign.leases().get(lease_id).state
            result.update(
                blocking_lease_id=lease_id,
                blocking_lease_state=lease_state,
                next_action=(
                    "Run scripts/bin/bearhug-campaign control continue."
                    if lease_state == "released"
                    else f"Run scripts/bin/bearhug-campaign recover --lease-id {lease_id}."
                ),
            )
            if lease_state == "released":
                result["reason"] = (
                    "Previous lease conflict is resolved; continue the current campaign."
                )
        accepted = campaign.state.get("accepted", {})
        active = campaign.state.get("active_capsule_id")
        for task in state["tasks"]:
            if task.get("lane", work.DEFAULT_LANE) != work.DEFAULT_LANE:
                # An interactive-lane task is never a capsule; its status is whatever
                # bearhug-work start/complete/block last recorded on the board directly.
                result["task_states"][task["id"]] = {
                    "status": task["status"],
                    "session_id": task["session_id"],
                    "provider": task["provider"],
                    "evidence": task["evidence"],
                }
                continue
            capsule = board_capsule_id(task["id"])
            if capsule not in {item["capsule_id"] for item in campaign.plan["capsules"]}:
                raise work.WorkError("Campaign capsule mapping differs from the accepted board.")
            task_state = "pending"
            if capsule in accepted:
                task_state = "completed"
            elif capsule == active:
                task_state = (
                    "blocked"
                    if report["status"] in {"blocked", "failed", "stopped", "awaiting_hil"}
                    else "in_progress"
                )
            result["task_states"][task["id"]] = {
                "status": task_state,
                "session_id": link["session_id"],
                "provider": config.provider,
                "evidence": f"Campaign {session['locator']} · {capsule}",
            }
        if include_cockpit:
            result["cockpit"] = report["cockpit"]
        return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        # Changed live authority blocks execution, but must not hide a completed stop.
        # Reopen the exact sealed owner; never infer released custody from a cached status.
        if session and config and session.get("locator"):
            try:
                locator = terminal._exact_locator(session["locator"], config.private_root)
                sealed = load_prepared(locator, recovery=True)
                if Path(sealed.record["subject"]["path"]) == root:
                    observed = terminal._status_for_locator(str(locator))
                    if observed.get("stopped"):
                        return {**result, **observed, "authority_warning": str(exc)}
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                pass
        return {**result, "status": "blocked", "reason": str(exc)}


class BoardDriver(terminal.TerminalDriver):
    def __init__(self, config, tasks, **kwargs):
        super().__init__(config, **kwargs)
        self.tasks = tasks

    def _prepare(self, raw):
        prepared = prepare_terminal_request(
            self.config.subject_path,
            self.config.template_path,
            self.config.policy_path,
            self.config.execution_path,
            raw,
            state_root=self.config.private_root,
            task_rows=self.tasks,
        )
        if (
            prepared.record["provider_policy"]["roles"]["author"]["provider"]
            != self.config.provider
        ):
            raise work.WorkError(
                "Execution profile provider differs from the approved author policy."
            )
        return prepared


def _release_failed_intake(root: Path, *, session_id: str, plan_sha256: str) -> None:
    """Remove only the exact provisional binding whose intake produced no locator."""

    current = binding(root)
    if (
        current.get("session_id") != session_id
        or current.get("plan_sha256") != plan_sha256
    ):
        return
    replacement = {"enabled": current.get("enabled", True)}
    if "retired_campaigns" in current:
        replacement["retired_campaigns"] = current["retired_campaigns"]
    work._write_json(work._path(root, BINDING), replacement)


def _reclaim_provisional_intake(root: Path, config, link: dict) -> bool:
    """Reclaim only a durably marked pre-locator intake, under authority/binding fences.

    The terminal intake fence excludes concurrent preparation. TerminalDriver publishes a
    locator before invoking any worker, so an unaccepted session with no locator or worker is
    a pre-allocation state. Never infer this for older unmarked bindings or missing acquired
    custody. A crash after locator publication retains the original session and its controls.
    """
    if link.get("intake_phase") != "preparing":
        return False
    if (
        not isinstance(link.get("session_id"), str) or not link["session_id"]
        or any(not isinstance(link.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", link[key])
               for key in ("plan_sha256", "request_sha256", "profile_sha256"))
        or link.get("provider") != config.provider
        or hashlib.sha256(work._path(root, PROFILE).read_bytes()).hexdigest()
        != link["profile_sha256"]
    ):
        raise work.WorkError("Interrupted intake authority changed; restore its exact profile.")
    with terminal._intake_lock(config):
        session = terminal._load_session(config, link["session_id"])
        if session and (
            session.get("locator") or session.get("worker") is not None
            or session.get("request_sha256") is not None or session.get("history")
        ):
            return False
        if binding(root) != link:
            raise work.WorkError("Interrupted intake binding changed during recovery.")
        _release_failed_intake(
            root, session_id=link["session_id"], plan_sha256=link["plan_sha256"],
        )
    return True


@contextlib.contextmanager
def _binding_lock(root: Path, config):
    """Serialize binding plus dispatch, independently of the caller's BOARD lock."""

    private = config.private_root
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()
    descriptor = os.open(
        private / f".project-binding-{key}.lock",
        os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
            or observed.st_mode & 0o077
        ):
            raise work.WorkError("Campaign binding lock must be a private regular file.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def start(root: Path, *, task_id: str, session_id: str, provider: str) -> dict:
    """Dispatch once, including direct callers without the BOARD transition lock."""
    with work._campaign_authority_locked(root):
        return _start_current(root, task_id=task_id, session_id=session_id, provider=provider)


def _approved_draft_sha256(root: Path) -> str | None:
    """The draft digest the installed profile was approved from.

    `approve_draft` seals its inputs under ``onboarding-inputs/<draft digest>/``, so the
    profile itself records which proposal it came from; no new field is needed, and a
    profile approved before this check was written is still comparable.
    """

    try:
        execution_path = profile(root).execution_path
    except Exception:
        return None
    parent = Path(execution_path).parent.name
    return parent if len(parent) == 64 else None


def _qualification_drifted(root: Path, saved: dict) -> bool:
    """Has the provider's qualified runtime moved since the saved proposal was derived?

    Qualification binds the exact installed settings and rules files by digest. Those live
    outside the project and change without it: an edit to ~/.claude/settings.json invalidates
    a proposal nothing in the project touched. `approve_draft` already refuses a drifted
    proposal, but onboard returns early before reaching it, so the drift only surfaced deep
    in the worker as "provider settings digest mismatch" after a dispatch was accepted.
    """

    from bearhug import project_onboarding

    provider = saved.get("provider")
    if not provider:
        return False
    current = (saved.get("draft") or {}).get("qualification") or {}
    try:
        observed = project_onboarding._qualification(
            root, provider, (saved.get("options") or {}).get("qualification_index")
        ).to_mapping()
    except Exception:
        # An unreadable qualification is its own blocker elsewhere; do not invent drift.
        return False
    return bool(current) and observed != current


def _require_approved_draft(root: Path) -> None:
    """Refuse to start work from a proposal that was never approved.

    Onboarding rewrites its draft whenever the derivation changes, but the installed
    profile only changes on approval. Without this, a re-derived proposal is written,
    never approved, and dispatch silently seals the previously approved config instead --
    measured on row 240, where freshly derived lease evidence was dropped in exactly that
    gap and the capsule failed on the validation the derivation existed to prevent.
    """

    saved_path = work._path(root, ONBOARDING)
    if not saved_path.is_file():
        return
    try:
        saved = json.loads(saved_path.read_text())
    except ValueError:
        return
    current = (saved.get("draft") or {}).get("draft_sha256")
    approved = _approved_draft_sha256(root)
    if not current or not approved or current == approved:
        return
    raise work.WorkError(
        "The onboarding proposal changed after the installed profile was approved; "
        f"approve it with onboard --approve-sha256 {current} before starting."
    )


def _start_current(root: Path, *, task_id: str, session_id: str, provider: str) -> dict:
    state = work._load(root)
    if not state:
        raise work.WorkError("Accept a plan before starting a campaign.")
    text, digest = work._read_plan(root, state["plan"]["path"])
    if digest != state["plan"]["sha256"]:
        raise work.WorkError("Accepted plan changed; review and accept its exact bytes again.")
    task = next((row for row in state["tasks"] if row["id"] == task_id), None)
    if not task:
        raise work.WorkError("Unknown task.")
    if task.get("lane", work.DEFAULT_LANE) != work.DEFAULT_LANE:
        raise work.WorkError(
            "This task is on the interactive lane; use bearhug-work start/complete/block."
        )
    observed = status(root)
    if observed.get("status") == "needs_configuration" or not work._path(root, PROFILE).is_file():
        raise work.WorkError(observed["reason"])
    _require_approved_draft(root)
    if [
        {key: row[key] for key in ("id", "title", "depends_on", "done_when", "lane")}
        for row in work.parse_tasks(text)
    ] != [
        {key: row[key] for key in ("id", "title", "depends_on", "done_when", "lane")}
        for row in state["tasks"]
    ]:
        raise work.WorkError("Board definitions differ from the exact accepted plan.")
    config = profile(root)
    if provider != config.provider:
        raise work.WorkError(f"This execution profile uses {config.provider}, not {provider}.")
    with _binding_lock(root, config):
        return _start_bound(
            root, config=config, state=state, text=text, digest=digest,
            task_id=task_id, session_id=session_id, provider=provider,
        )


def _start_bound(root: Path, *, config, state, text, digest, task_id, session_id, provider) -> dict:
    """The binding fence remains held until locator publication or provisional cleanup."""

    link = binding(root)
    if link.get("enabled") is False:
        raise work.WorkError("Campaign execution is opted out.")
    if _reclaim_provisional_intake(root, config, link):
        link = binding(root)
    if link.get("session_id"):
        if link["session_id"] != session_id:
            raise work.WorkError(
                "Campaign already belongs to another session; use campaign controls."
            )
        if link.get("task_id") != task_id:
            raise work.WorkError(
                "The whole accepted plan is already dispatched; read campaign status."
            )
        observed = status(root)
        if link.get("plan_sha256") != digest:
            raise work.WorkError("Existing campaign belongs to another accepted plan.")
        if observed.get("status") == "stopped":
            # Without this, a second start over a stopped campaign fell through to the
            # locator check below, returned the stopped status as if re-delivery were a
            # legitimate no-op, and left the campaign stopped with nothing telling the
            # operator so -- "half-registering" a start that never resumed anything.
            raise work.WorkError(
                "This campaign is stopped; run scripts/bin/bearhug-campaign control "
                "continue to resume it, or scripts/bin/bearhug-campaign retire to detach "
                "it and start fresh."
            )
        if observed.get("status") in _NON_LIVE_TERMINAL_STATUSES:
            # A worker that failed, or a phase review that finished without validating the
            # combined system, keeps its locator -- so without this, the code below returned
            # the old failure as though a second `start` were a legitimate idempotent
            # re-delivery. `project_work.py` then re-raises that stored reason as if it were
            # new, and nothing tells the operator the campaign never started a second worker
            # or names the actual route forward.
            session_record = terminal._load_session(config, session_id)
            durably_failed = False
            session_active = False
            if session_record is not None and session_record.get("locator"):
                terminal._refresh_session(config, session_record)
                durably_failed = bool(
                    session_record.get("failed_episode")
                    and not session_record.get("unresolved_spend")
                )
                session_active = terminal._session_active(session_record)
            if not durably_failed and session_active:
                # `control continue` is the real re-attempt for this case (`_handle_control`
                # only refuses it when durably failed or the session is no longer active).
                route = " Run scripts/bin/bearhug-campaign control continue to resume it."
            else:
                route = _remaining_settle_steps(session_record or {})
            raise work.WorkError(
                "The previous attempt ended: "
                f"{observed.get('reason', 'no reason recorded')}." + route
            )
        if observed.get("locator"):
            return observed  # Re-delivery must never start a second worker or spend again.
        raise work.WorkError(
            "Existing campaign binding has no recoverable locator; restore its original "
            "session custody before retrying."
        )
    # The profile file's own bytes are read fresh here (not the caller's stale digest, if
    # any): _start_current already required this file to exist before reaching _start_bound.
    current_profile_sha256 = hashlib.sha256(work._path(root, PROFILE).read_bytes()).hexdigest()
    stale_retirement = next(
        (
            entry
            for entry in link.get("retired_campaigns", [])
            if entry.get("session_id") == session_id
            and entry.get("task_id") == task_id
            and entry.get("plan_sha256") == digest
            and entry.get("profile_sha256") == current_profile_sha256
        ),
        None,
    )
    if stale_retirement is not None:
        # This exact session already retired a campaign for this exact task, under this
        # exact accepted plan and this exact installed profile, and never ran onboard again
        # since -- the installed profile is still byte-identical to the one that was retired.
        # A fresh dispatch here would re-derive the identical request and land back on the
        # retired campaign's own prepared state root, so refuse instead of silently starting
        # over under it. A changed (successor) plan is a different request and is not caught
        # by this (test_failed_successor_intake_does_not_bind_project_to_abandoned_session),
        # and neither is a genuine re-onboard and re-approval, since installing a different
        # profile changes profile_sha256 and this match no longer applies
        # (test_a_real_reonboard_and_reapproval_after_retire_starts_fresh).
        raise work.WorkError(
            "This task's campaign was retired; run onboard again and approve a fresh "
            "draft before starting it."
        )
    campaign_tasks = _campaign_lane_tasks(state["tasks"])
    if any(row["status"] != "pending" for row in campaign_tasks):
        raise work.WorkError("Campaign intake requires an unstarted accepted plan.")
    # Every interactive-lane task the campaign depends on is completed by this point
    # (`_campaign_lane_tasks` already refused otherwise); completing one through bearhug-work
    # wrote the board (BOARD.md/LEDGER.md/WORK.json) into the tracked tree. The campaign leases
    # the subject at its own sealed HEAD, which must be clean, so refuse intake itself here and
    # name the commit step, rather than let that failure surface deep inside the first lease.
    _require_clean_subject_for_intake(root)
    raw = _board_request(root, state, work.project_lane_text(text), session_id)
    request_sha256 = hashlib.sha256(raw).hexdigest()
    link = {
        "enabled": True,
        **({"retired_campaigns": link["retired_campaigns"]} if "retired_campaigns" in link else {}),
        "plan_sha256": digest,
        "plan_path": state["plan"]["path"],
        "session_id": session_id,
        "provider": provider,
        "task_id": task_id,
        "intake_phase": "preparing",
        "request_sha256": request_sha256,
        "profile_sha256": hashlib.sha256(work._path(root, PROFILE).read_bytes()).hexdigest(),
    }
    work._write_json(work._path(root, BINDING), link)
    driver = BoardDriver(config, campaign_tasks, config_path=work._path(root, PROFILE))
    try:
        decision = driver.dispatch(raw)
    except Exception:
        # The binding above is provisional until TerminalDriver publishes a recoverable locator.
        # A failed intake must not reserve the whole project for an abandoned session.
        session = terminal._load_session(config, session_id)
        if (
            session is None
            or not session.get("locator")
            or session.get("request_sha256") != request_sha256
        ):
            _release_failed_intake(root, session_id=session_id, plan_sha256=digest)
        raise
    observed = status(root)
    if not observed.get("locator"):
        session = terminal._load_session(config, session_id)
        if (
            session is None
            or not session.get("locator")
            or session.get("request_sha256") != request_sha256
        ):
            _release_failed_intake(root, session_id=session_id, plan_sha256=digest)
        return {**observed, "status": "blocked", "reason": decision.reason}
    if binding(root) != link:
        raise work.WorkError("Campaign binding changed before locator confirmation.")
    work._write_json(work._path(root, BINDING), {**link, "intake_phase": "attached"})
    return {**observed, "intake_phase": "attached"}


def configure(root: Path, args) -> dict:
    with work._campaign_authority_locked(root):
        return _configure_current(root, args)


def _configure_current(root: Path, args) -> dict:
    if work._path(root, PROFILE).is_file():
        current_config = profile(root)
        with _binding_lock(root, current_config):
            _reclaim_provisional_intake(root, current_config, binding(root))
    current = status(root)
    if current.get("attached") and current["status"] != "phase_validated":
        raise work.WorkError("Finish or recover the attached campaign before changing its profile.")
    state_root = (
        Path(args.state_root)
        if args.state_root
        else (RUNS_DIR / "project-campaigns" / hashlib.sha256(str(root).encode()).hexdigest()[:24])
    )
    state_root = terminal._absolute_dir(state_root, label="state root", create=True, private=True)
    config = terminal.TerminalDriverConfig(
        subject=root,
        provider=args.provider,
        template_path=Path(args.template).resolve(strict=True),
        policy_path=Path(args.policy).resolve(strict=True),
        execution_path=Path(args.execution).resolve(strict=True),
        state_root=state_root,
    )
    terminal._write_atomic(
        work._path(root, PROFILE),
        terminal._canonical(config.to_mapping()),
        mode=0o600,
        private_parent=False,
    )
    link = binding(root)
    link["enabled"] = True
    work._write_json(work._path(root, BINDING), link)
    return status(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("status")
    setup = sub.add_parser(
        "onboard", help="prepare project execution settings from the accepted plan"
    )
    setup.add_argument("--provider", choices=("claude", "codex"), required=True)
    setup.add_argument("--model")
    setup.add_argument("--effort")
    setup.add_argument(
        "--validate", action="append", help="validation command; repeat for multiple checks"
    )
    setup.add_argument("--approve-sha256", help="approve the exact reviewed draft")
    setup.add_argument(
        "--lease-path",
        dest="lease_paths",
        action="append",
        help=(
            "Git-ignored path a validation command needs inside the lease; repeat for "
            "several. A lease carries tracked files only, so evidence a gate reads must be "
            "named here or the gate fails there for a reason no capsule can repair"
        ),
    )
    parser.add_argument(
        "--allow-program",
        dest="allowed_programs",
        action="append",
        help=(
            "program an episode may run that is not a validation gate; repeat for several. "
            "A gate must pass, so a check a task exists to run and report red cannot be one. "
            "Without this the two lists were one list"
        ),
    )
    setup.add_argument(
        "--lease-probe",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "measure the lease paths instead of declaring them, by running the whole "
            "validation suite in a throwaway worktree. Slow, and incomplete by construction; "
            "use it once to discover the list, then declare it with --lease-path"
        ),
    )
    setup.add_argument("--qualification-index", help=argparse.SUPPRESS)
    setup.add_argument("--state-root", help=argparse.SUPPRESS)
    setup.add_argument(
        "--max-tool-terms", type=int,
        help=(
            "how many of the plan's candidate terms query Graft/MemQ/architecture per "
            f"onboarding (default {grounding.MAX_TOOL_TERMS}; allowed "
            f"{grounding.TUNABLE_LIMIT_BOUNDS['max_tool_terms'][0]}-"
            f"{grounding.TUNABLE_LIMIT_BOUNDS['max_tool_terms'][2]}). Task-table terms (ID, "
            "Task, Depends on, Done when) always fill these slots before other plan prose"
        ),
    )
    setup.add_argument(
        "--max-decisions", type=int,
        help=(
            "how many matched decisions can bind as packet sources per onboarding (default "
            f"{grounding.MAX_DECISIONS}, previously 8; allowed "
            f"{grounding.TUNABLE_LIMIT_BOUNDS['max_decisions'][0]}-"
            f"{grounding.TUNABLE_LIMIT_BOUNDS['max_decisions'][2]}; pass 8 for the old default)"
        ),
    )
    setup.add_argument(
        "--graft-per-query-hits", type=int,
        help=(
            "how many hits one Graft query may return (default "
            f"{grounding.GRAFT_PER_QUERY_HITS}; allowed "
            f"{grounding.TUNABLE_LIMIT_BOUNDS['graft_per_query_hits'][0]}-"
            f"{grounding.TUNABLE_LIMIT_BOUNDS['graft_per_query_hits'][2]})"
        ),
    )
    setup.add_argument(
        "--max-tool-hits", type=int,
        help=(
            "how many distinct hits one tool (Graft or MemQ) keeps across all its queries per "
            f"onboarding (default {grounding.MAX_TOOL_HITS}; allowed "
            f"{grounding.TUNABLE_LIMIT_BOUNDS['max_tool_hits'][0]}-"
            f"{grounding.TUNABLE_LIMIT_BOUNDS['max_tool_hits'][2]})"
        ),
    )
    setup.add_argument(
        "--task-terms-first",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "rank the plan's task-table terms (ID, Task, Depends on, Done when) ahead of its "
            "other prose when filling the --max-tool-terms query slots (default: on, previously "
            "off). Measured to change which decisions bind, not just which "
            "terms are queried: raising Graft's or MemQ's queried-term set can change what MemQ "
            "discovers, which feeds the decision-matching score. Use --no-task-terms-first to "
            "restore the previous ranking when an operator's own explanatory prose in the "
            "plan file should not outrank the task table for a query slot, and re-review "
            "bindings afterward either way, not just the digest"
        ),
    )
    configure_parser = sub.add_parser("configure")
    configure_parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    configure_parser.add_argument("--template", required=True)
    configure_parser.add_argument("--policy", required=True)
    configure_parser.add_argument("--execution", required=True)
    configure_parser.add_argument("--state-root")
    launch = sub.add_parser("dispatch", help=argparse.SUPPRESS)
    launch.add_argument("--task", required=True)
    launch.add_argument("--session", required=True)
    launch.add_argument("--provider", required=True, choices=("claude", "codex"))
    control = sub.add_parser("control")
    control.add_argument("command", choices=sorted(terminal.CONTROL_COMMANDS))
    retire_parser = sub.add_parser(
        "retire", help="Detach a stopped campaign with no accepted work; retain evidence"
    )
    retire_parser.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="do not remove this campaign's own lease worktrees (removed by default)",
    )
    retire_parser.add_argument(
        "--json",
        action="store_true",
        help="print only the final machine-readable result, not one line per worktree",
    )
    prune_parser = sub.add_parser(
        "prune",
        help="List leftover Bear Hug lease worktrees from older runs (dry run by default)",
    )
    prune_parser.add_argument(
        "--apply", action="store_true", help="remove them instead of only listing (default: list)"
    )
    prune_parser.add_argument(
        "--json",
        action="store_true",
        help="print only the final machine-readable result, not one line per worktree",
    )
    recovery = sub.add_parser(
        "recover", help="recover one interrupted campaign spend boundary without retrying it"
    )
    recovery.add_argument("--recovery-outcome", choices=RECOVERY_OUTCOMES, default="failed")
    recovery.add_argument("--episode-id")
    recovery.add_argument("--review-id")
    recovery.add_argument("--lease-id", help="finish a stopped earlier campaign's lease release")
    recovery.add_argument(
        "--resolve-orphan",
        action="store_true",
        help="release the --lease-id orphan through controller HIL; requires --confirm",
    )
    recovery.add_argument(
        "--confirm",
        help="the operator decision recorded as the HIL answer for --resolve-orphan",
    )
    sub.add_parser("opt-out")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    try:
        if args.operation == "dispatch":
            result = start(root, task_id=args.task, session_id=args.session, provider=args.provider)
        elif args.operation == "onboard":
            with work._locked(root):
                result = onboard(root, args)
        elif args.operation == "configure":
            with work._locked(root):
                result = configure(root, args)
        elif args.operation == "control":
            link = binding(root)
            config = profile(root)
            driver = terminal.TerminalDriver(config, config_path=work._path(root, PROFILE))
            decision = driver.dispatch(
                terminal._canonical(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "cwd": str(root),
                        "session_id": link["session_id"],
                        "prompt": f"bearhug {args.command}",
                    }
                )
            )
            result = status(root)
            if args.command == "repair":
                result["control_message"] = decision.reason
            if args.command == "stop" and decision.reason == (
                "stop requested for the active Bear Hug campaign"
            ) and not result.get("stopped"):
                result.update(status="stopping", reason=decision.reason)
        elif args.operation == "retire":
            # The helper holds the intake lock. Acceptance may already hold the board lock
            # in the calling process, so do not try to acquire that lock again here.
            link_before = binding(root)
            session_id = link_before.get("session_id")
            _retire_attached_session(root)
            result = {
                "status": "retired", "reason": "Settled campaign detached; evidence retained."
            }
            if session_id:
                # The detach above already succeeded and is not undone by anything below.
                # A worktree cleanup failure (a balky Git process, a permission error) must
                # report the retire it actually did, with the cleanup failure named
                # alongside it, never mask a successful retire as a whole-command "blocked".
                try:
                    config = profile(root)
                    actions = cleanup_after_retire(
                        root, session_id, config, keep=args.keep_worktrees
                    )
                except (OSError, ValueError, RuntimeError, KeyError) as exc:
                    result["worktree_cleanup_error"] = str(exc)
                else:
                    result["worktrees"] = [action.as_dict() for action in actions]
                    if not args.json:
                        for action in actions:
                            verb = "removed" if action.removed else "kept"
                            branch = f" [{action.branch}]" if action.branch else ""
                            print(f"{verb}: {action.path}{branch} -- {action.reason}")
        elif args.operation == "prune":
            config = profile(root)
            actions = plan_prune(root, config, apply=args.apply)
            result = {
                "status": "pruned" if args.apply else "dry_run",
                "reason": (
                    "Removed leftover Bear Hug lease worktrees."
                    if args.apply
                    else "Dry run; nothing removed. Re-run with --apply to remove them."
                ),
                "worktrees": [action.as_dict() for action in actions],
            }
            if not args.json:
                for action in actions:
                    if args.apply:
                        verb = "removed" if action.removed else "kept"
                    else:
                        verb = "would remove" if "would remove" in action.reason else "kept"
                    branch = f" [{action.branch}]" if action.branch else ""
                    print(f"{verb}: {action.path}{branch} -- {action.reason}")
        elif args.operation == "recover":
            with work._locked(root):
                result = recover(
                    root,
                    outcome=args.recovery_outcome,
                    episode_id=args.episode_id,
                    review_id=args.review_id,
                    lease_id=args.lease_id,
                    resolve_orphan=args.resolve_orphan,
                    confirm=args.confirm,
                )
        elif args.operation == "opt-out":
            with work._locked(root), work._campaign_authority_locked(root):
                current = status(root)
                if current.get("attached") and current["status"] != "phase_validated":
                    raise work.WorkError(
                        "Finish or recover the attached campaign before opting out."
                    )
                link = binding(root)
                link["enabled"] = False
                work._write_json(work._path(root, BINDING), link)
                result = status(root)
        else:
            result = status(root)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        result = {"status": "blocked", "reason": str(exc)}
    print(json.dumps(result))
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
