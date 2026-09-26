"""K02 — the cockpit's `runtime`, `coordinator` and `hooks_health` blocks.

Three questions about the harness itself, answered from artifacts that already exist:

* **`runtime`** — what version is installed in the subject, what its tree hashes to, and whether
  that differs from the sealed manifest's digest. A missing runtime is `absent` with a reason, never
  version 0 and never an empty hash: a snapshot that predates promotion had no runtime, and zero
  would read as "clean".
* **`coordinator`** — the latest Stop decision and every evaluator's result, grouped by `event_id`
  from the subject's own telemetry. Absent telemetry is `unreported`.
* **`hooks_health`** — crashed, skipped, unreachable and silent, from telemetry and the persisted
  hook-audit findings, with **every source named**. D05's rule: a crash is recorded in
  `.automation-stamps/` *and* telemetry, so the pane must say which it read, because telemetry is
  droppable by design.

**A crash is not a pass and `not_reached` is not a crash.** The runtime records an evaluator that
raised as `execution_state: evaluated` with `result.verdict: "error"`, and a slot the coordinator
never dispatched as `execution_state: not_reached` with no result at all. Those are different
defects with different owners — blaming a gate for a dispatch failure it was never reached by would
put a defect in a component that has none — so this module keeps them in separate lists.

**Nothing here is recomputed.** Verdicts, execution states and finding severities are read as
written. This module derives exactly one thing, `drift`, and it derives it by comparing two hashes
neither of which it invents.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bearhug.paths import BARRACUDA_ROOT, FINDINGS_DIR, PATCHES_DIR
from bearhug.report.regression import runtime_tree_sha256

#: Where the runtime is installed in the subject, and where its telemetry lands (D08: the store is
#: project-based, so it is under the subject's own root rather than under `~/.claude`).
INSTALLED_RUNTIME = Path("scripts") / "hooks" / "_bearhug"
TELEMETRY_RELATIVE = Path(".bearhug") / "telemetry" / "v1"
STAMPS_RELATIVE = Path(".automation-stamps")

#: The sealed package's manifest — the digest an installed tree is compared against.
SEALED_MANIFEST = PATCHES_DIR / "promotion-package" / "manifest.json"

#: The reason code the runtime writes when an evaluator raised. Read, never re-derived.
CRASH_REASON = "evaluator_exception"

#: What the hook audit calls an unreachable registration versus a silent one. Both are read from
#: the persisted findings; this module never re-runs the battery.
COVERAGE_CHECK = "COVERAGE"
SILENT_CHECK = "INERTNESS"

HEALTH_LIMITS = (
    "Zero crashed evaluators is a WINDOW, not a proof of absence: it means no crash appears in the "
    "telemetry actually on disk. A detector that returns nothing has not shown the behaviour is "
    "absent, and telemetry is droppable by design (D05).",
    "`unreported` is not `healthy`. An absent source is reported as absent, never as zero.",
    "Crashed/skipped are read from the runtime's own `execution_state` and `result.verdict`; "
    "Coverage gaps and silence are synthetic hook-audit findings, not live reachability failures. "
    "A deployed repository need not contain the bundled components' tests.",
)


def _safe(snapshot_id: str) -> str:
    return snapshot_id.replace("@", "-at-").replace("/", "-")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- runtime -----------------------------------------------------------------------------------


def runtime_block(subject_root: Path, sealed_manifest: Path | None = None) -> dict[str, Any]:
    """Installed version, installed tree hash, and drift against the sealed digest."""
    manifest_path = sealed_manifest if sealed_manifest is not None else SEALED_MANIFEST
    sealed = _read_json(manifest_path) or {}
    sealed_sha = sealed.get("runtime_sha256")
    sealed_version = sealed.get("runtime_version")

    installed_dir = subject_root / INSTALLED_RUNTIME
    version_file = installed_dir / "VERSION"
    installed_version = None
    if version_file.is_file():
        try:
            installed_version = version_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            installed_version = None
    # The runtime's own algorithm (`bearhug-runtime-sha256/1`), not a second hasher: the manifest
    # and this reading must not be able to describe the same tree differently.
    installed_sha = runtime_tree_sha256(installed_dir)

    sources = {
        "version": str(version_file),
        "tree": str(installed_dir),
        "sealed_manifest": str(manifest_path),
        "hash_algorithm": "bearhug-runtime-sha256/1",
    }

    if installed_sha is None:
        return {
            "status": "absent",
            "reason": (
                f"no installed runtime tree at {installed_dir}. A snapshot that predates promotion "
                "had none, and `absent` is the honest value — a zero hash would read as clean."
            ),
            "installed_version": installed_version,
            "installed_sha256": None,
            "sealed_version": sealed_version,
            "sealed_sha256": sealed_sha,
            "drift": "unknown",
            "sources": sources,
        }

    if sealed_sha is None:
        drift = "unknown"
    elif sealed_sha == installed_sha:
        drift = "match"
    else:
        drift = "drift"

    return {
        "status": "carried",
        "reason": "" if drift != "unknown" else f"{manifest_path} names no runtime_sha256",
        "installed_version": installed_version,
        "installed_sha256": installed_sha,
        "sealed_version": sealed_version,
        "sealed_sha256": sealed_sha,
        "drift": drift,
        "sources": sources,
    }


# --- coordinator -------------------------------------------------------------------------------


def _telemetry_records(telemetry_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Every `coordinator_evaluation` record on disk, and the files they came from.

    Streamed line by line: a telemetry directory grows without bound and is never loaded whole
    (CLAUDE.md).
    """
    records: list[dict[str, Any]] = []
    read: list[str] = []
    if not telemetry_dir.is_dir():
        return records, read
    for events in sorted(telemetry_dir.glob("*/events.jsonl")):
        read.append(str(events))
        try:
            with events.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and (
                        record.get("record_kind") == "coordinator_evaluation"
                    ):
                        records.append(record)
        except OSError:
            continue
    return records, read


def _evaluator_row(record: dict[str, Any]) -> dict[str, Any]:
    """One evaluator slot, as the runtime wrote it. No verdict is invented here."""
    result = record.get("result") or {}
    return {
        "gate_id": record.get("gate_id"),
        "ordinal": record.get("evaluator_ordinal"),
        "execution_state": record.get("execution_state"),
        "applicability": result.get("applicability"),
        "verdict": result.get("verdict"),
        "reason_code": result.get("reason_code"),
        "exception_type": record.get("exception_type"),
        "not_reached_reason": record.get("not_reached_reason"),
    }


def _grouped(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(str(record.get("event_id")), []).append(record)
    return groups


def _observed_at(event: list[dict[str, Any]]) -> str:
    return max(str(r.get("observed_at") or "") for r in event)


#: K04 (Sam's ruling, 2026-09-03), measured against session `ddd93405`: 366 turns, 0 telemetry
#: records. Claude Code fires no Stop hook when a turn ends on an AskUserQuestion card, so a
#: session can run for hours with zero coordinator telemetry. The HARNESS pane must say so in
#: words rather than leave the pane blank or imply a pass.
NO_STOP_REASON = (
    "this session has transcript activity in the recent window but no Stop event was recorded — "
    "Claude Code fires no Stop hook when a turn ends on an AskUserQuestion card, so a session can "
    "run for hours with zero coordinator telemetry"
)


def _session_decision(rows: list[dict[str, Any]]) -> str:
    """`block` iff any evaluator in the event verdicted `block` — the one fact `arbitrate()`'s
    table always agrees on regardless of category/priority: `Decision(decision="pass", ...)` fires
    exactly when `blocks` is empty (runtime/bearhug_runtime/arbitrate.py). This mirrors that one
    invariant; it does not reconstruct the categorisation, priority or remediation the real table
    computes, and must not be read as doing so."""
    for row in rows:
        result = row.get("result") or {}
        if result.get("verdict") == "block":
            return "block"
    return "pass"


def _session_states(
    telemetry_dir: Path, session_ids: list[str], groups: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """K04: `recorded` / `no_stop_reached` / `unreported`, per recent session.

    `unreported` means the telemetry STORE itself is absent — this cockpit never looked.
    `no_stop_reached` means the store exists but carries no event for this session — it looked and
    found nothing, which is a different, more informative fact (and the one K04 exists to name).
    """
    # De-duplicated, order preserved: a caller may pass the same session id twice.
    ordered_ids = list(dict.fromkeys(session_ids))

    if not telemetry_dir.is_dir():
        return [
            {
                "session_id": session_id,
                "status": "unreported",
                "events_observed": 0,
                "latest_event_id": None,
                "runtime_version": None,
                "runtime_sha256": None,
                "decision": "none",
                "reason": (
                    f"no telemetry store under {telemetry_dir}: unreported means unobserved, "
                    "not healthy"
                ),
            }
            for session_id in ordered_ids
        ]

    by_session: dict[str, list[dict[str, Any]]] = {}
    for rows in groups.values():
        session_id = str(rows[0].get("session_id") or "")
        if session_id:
            by_session.setdefault(session_id, []).extend(rows)

    sessions: list[dict[str, Any]] = []
    for session_id in ordered_ids:
        session_records = by_session.get(session_id)
        if not session_records:
            sessions.append(
                {
                    "session_id": session_id,
                    "status": "no_stop_reached",
                    "events_observed": 0,
                    "latest_event_id": None,
                    "runtime_version": None,
                    "runtime_sha256": None,
                    "decision": "none",
                    "reason": NO_STOP_REASON,
                }
            )
            continue
        session_groups = _grouped(session_records)
        event_id, rows = max(
            session_groups.items(), key=lambda item: (_observed_at(item[1]), item[0])
        )
        first = rows[0]
        sessions.append(
            {
                "session_id": session_id,
                "status": "recorded",
                "events_observed": len(session_groups),
                "latest_event_id": event_id,
                "runtime_version": first.get("runtime_version"),
                "runtime_sha256": first.get("runtime_sha256"),
                "decision": _session_decision(rows),
                "reason": "",
            }
        )
    return sessions


def coordinator_block(
    subject_root: Path, recent_session_ids: list[str] | None = None
) -> dict[str, Any]:
    """The latest Stop event and every evaluator result in it, grouped by `event_id`, plus K04's
    per-session state for every session the caller names as recently active."""
    telemetry_dir = subject_root / TELEMETRY_RELATIVE
    records, read = _telemetry_records(telemetry_dir)
    groups = _grouped(records)
    sessions = _session_states(telemetry_dir, list(recent_session_ids or []), groups)

    if not groups:
        return {
            "status": "unreported",
            "reason": (
                f"no coordinator_evaluation records under {telemetry_dir}. Telemetry is droppable "
                "by design (D05), so `unreported` means unobserved — not healthy."
                " The installed Codex lifecycle adapter does not run coordinator policy gates."
            ),
            "source": ", ".join(read) or str(telemetry_dir),
            "events_observed": 0,
            "latest": None,
            "sessions": sessions,
        }

    # Latest by observed_at, with the event id breaking a tie so the ordering is stable rather
    # than dependent on the order the files happened to be read in.
    event_id, rows = max(groups.items(), key=lambda item: (_observed_at(item[1]), item[0]))
    first = rows[0]
    evaluators = sorted(
        (_evaluator_row(r) for r in rows),
        key=lambda row: (row["ordinal"] if row["ordinal"] is not None else 99, str(row["gate_id"])),
    )
    return {
        "status": "carried",
        "reason": "",
        "source": ", ".join(read),
        "events_observed": len(groups),
        "latest": {
            "event_id": event_id,
            "event_name": first.get("event_name"),
            "observed_at": _observed_at(rows),
            "session_id": first.get("session_id"),
            "coordinator_version": first.get("coordinator_version"),
            "coordinator_reason": first.get("coordinator_reason"),
            "runtime_version": first.get("runtime_version"),
            "runtime_sha256": first.get("runtime_sha256"),
            "evaluators": evaluators,
        },
        "sessions": sessions,
    }


# --- hooks_health ------------------------------------------------------------------------------


def hooks_health_block(
    subject_root: Path,
    findings_dir: Path | None = None,
    *,
    snapshot_id: str,
) -> dict[str, Any]:
    """Crashed, skipped, unreachable and silent — with every source named."""
    telemetry_dir = subject_root / TELEMETRY_RELATIVE
    records, read = _telemetry_records(telemetry_dir)
    sources = [f"telemetry: {path}" for path in read] or [f"telemetry: {telemetry_dir} (absent)"]

    stamps_dir = subject_root / STAMPS_RELATIVE
    if stamps_dir.is_dir():
        sources.append(f"stamps present but not read for health: {stamps_dir}")

    crashed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for record in records:
        row = _evaluator_row(record)
        row["event_id"] = record.get("event_id")
        row["observed_at"] = record.get("observed_at")
        if row["execution_state"] == "not_reached":
            skipped.append(row)
        elif row["verdict"] == "error" or row["reason_code"] == CRASH_REASON:
            crashed.append(row)

    def _key(row: dict[str, Any]) -> tuple[str, str]:
        return (str(row.get("observed_at") or ""), str(row.get("gate_id") or ""))

    crashed.sort(key=_key)
    skipped.sort(key=_key)

    coverage, silent = _audit_rows(findings_dir, snapshot_id=snapshot_id, sources=sources)

    status = "carried" if (records or coverage or silent) else "unreported"
    return {
        "status": status,
        "reason": (
            "" if records else "No coordinator evaluations observed; "
            "audit findings do not prove live hook health."
        ),
        "sources_read": sources,
        "telemetry_events": len(_grouped(records)),
        "crashed": crashed,
        "skipped": skipped,
        "unreachable": [row for row in coverage if not row["id"].startswith("hook-untested-")],
        "coverage_gaps": [row for row in coverage if row["id"].startswith("hook-untested-")],
        "silent": silent,
        "limits": list(HEALTH_LIMITS),
    }


def _audit_rows(
    findings_dir: Path | None, *, snapshot_id: str, sources: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """COVERAGE and INERTNESS findings for this snapshot, carried as filed.

    Severity and check are read from the file. Nothing is re-ranked here — the Go side reads the
    rank and status the Python side wrote, and so does this.
    """
    root = findings_dir if findings_dir is not None else FINDINGS_DIR
    unreachable: list[dict[str, Any]] = []
    silent: list[dict[str, Any]] = []
    if root is None or not Path(root).is_dir():
        return unreachable, silent

    for path in sorted(Path(root).glob(f"hooks-*{_safe(snapshot_id)}.json")):
        data = _read_json(path)
        if data is None or str(data.get("snapshot", "")) != snapshot_id:
            continue
        sources.append(f"hook-audit: {path.name}")
        for finding in data.get("findings", []):
            if not isinstance(finding, dict) or not finding.get("id"):
                continue
            row = {
                "id": finding["id"],
                "check": finding.get("check", ""),
                "severity": finding.get("severity", "info"),
                "summary": finding.get("summary", ""),
                "source": path.name,
            }
            if row["check"] == COVERAGE_CHECK:
                unreachable.append(row)
            elif row["check"] == SILENT_CHECK:
                silent.append(row)

    unreachable.sort(key=lambda row: row["id"])
    silent.sort(key=lambda row: row["id"])
    return unreachable, silent


def harness(
    *,
    subject_root: Path | str | None = None,
    sealed_manifest: Path | str | None = None,
    findings_dir: Path | str | None = None,
    snapshot_id: str,
    recent_session_ids: list[str] | None = None,
) -> dict[str, Any]:
    """The three harness blocks together."""
    root = Path(subject_root) if subject_root is not None else BARRACUDA_ROOT
    manifest = Path(sealed_manifest) if sealed_manifest is not None else None
    findings = Path(findings_dir) if findings_dir is not None else None
    return {
        "runtime": runtime_block(root, manifest),
        "coordinator": coordinator_block(root, recent_session_ids),
        "hooks_health": hooks_health_block(root, findings, snapshot_id=snapshot_id),
    }


__all__ = [
    "HEALTH_LIMITS",
    "coordinator_block",
    "harness",
    "hooks_health_block",
    "runtime_block",
]
