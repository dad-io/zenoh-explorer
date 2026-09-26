"""T02/T04/T05 + K02 — the cockpit artifact: Python emits, the TUI reads (T01, Sam's ruling
2026-09-01).

One JSON file the dashboard consumes instead of parsing transcripts or findings itself. Everything
in it is carried from an artifact Bear Hug already wrote — findings files, the DLV depth report,
the eval matrix — or read from live transcripts through Phase 4's reader, which is the one turn
authority. Nothing is re-derived: the cockpit copies summaries and severities as written, and where
an input is absent it says `unavailable` or `unreported`, never zero.

**Schema 2 (K02)** added the read model Addendum B.2 asks for: `tasks` and `phase` (session tasks
joined to durable BOARD rows through the runtime's own readers — see `cockpit_tasks`), `runtime`,
`coordinator` and `hooks_health` (see `cockpit_harness`), an optional `toolchain` block (see
`cockpit_toolchain`), and two blocks that keep the numbers honest:

* **`basis`** — the snapshot, runtime hash and corpus digest every number in the file rests on.
* **`comparison`** — `compared` only when a baseline shares all three. A mismatch in any one is
  **`incomparable`, never an improvement and never a regression** (S04's rule).

**Schema 3** adds privacy-bounded Claude/Codex provider observations. Raw events, prompts and
canonical promotion receipts remain outside this read model; a missing or malformed observation is
labelled, never rendered as a healthy zero.

**Schema 4** adds one explicit provider-work observation and its optional explicit BOARD binding.
The selected subject's Git common-directory, worktree path and bound active-plan bytes must match;
the producer never scans a directory or chooses a newest record.

**The subject is read only when asked.** `subject_root` has no default: without it the four
subject-derived blocks report `unavailable` / `absent` / `unreported` with a reason. That keeps
`build_cockpit` deterministic — the committed contract fixture `tui/testdata/cockpit.json` must not
change because the machine it was built on had a different Barracuda checked out.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bearhug.model import SEVERITY_ORDER
from bearhug.paths import (
    CORPUS_DIR,
    FINDINGS_DIR,
    REPORTS_DIR,
    SNAPSHOTS_DIR,
    assert_writable,
)
from bearhug.providers.observation import provider_observations
from bearhug.replay.cockpit_harness import HEALTH_LIMITS, harness
from bearhug.replay.cockpit_tasks import MISMATCH_CLASSES, tasks_and_phase
from bearhug.replay.cockpit_toolchain import toolchain_from_subject
from bearhug.replay.cockpit_work import provider_work_for_cockpit
from bearhug.replay.metrics import DISPATCH_TOOLS
from bearhug.replay.transcript import iter_events, iter_records, session_transcripts

#: The Go reader accepts exactly this and rejects "1" and anything unknown rather than coercing it.
#: Both sides move together, with `tui/testdata/cockpit.json` regenerated — see `cockpit_fixture`.
COCKPIT_SCHEMA_VERSION = "4"
TOP_FINDINGS = 12
RECENT_SESSIONS = 8
RECENT_SUBAGENTS = 12

#: Async Agent completion arrives as a harness-written user record. Only the task id and terminal
#: state cross the privacy boundary; notification prose and dispatch prompts never enter the
#: tracked cockpit artifact.
_TASK_TERMINAL = re.compile(
    r"<task-id>([^<]+)</task-id>[\s\S]*?<status>(completed|failed|killed|stopped)</status>"
)

#: The three facts a number has to share with another number before the two can be compared.
BASIS_FIELDS = ("snapshot_id", "runtime_sha256", "corpus_digest")


def _safe(snapshot_id: str) -> str:
    return snapshot_id.replace("@", "-at-").replace("/", "-")


def latest_snapshot_id(snapshots_dir: Path | str | None = None) -> str | None:
    root = Path(snapshots_dir) if snapshots_dir is not None else SNAPSHOTS_DIR
    candidates = sorted(p for p in root.glob("*") if (p / "manifest.json").is_file())
    if not candidates:
        return None
    manifest = json.loads((candidates[-1] / "manifest.json").read_text(encoding="utf-8"))
    return manifest.get("snapshot_id", candidates[-1].name)


def _findings_for(snapshot_id: str, findings_dir: Path) -> tuple[list[dict], dict[str, str]]:
    """Every finding filed against ``snapshot_id`` across prefixes, plus prefix → snapshot map for
    every findings file present (so stale files are named, not silently skipped)."""
    rows: list[dict] = []
    by_prefix: dict[str, str] = {}
    for path in sorted(findings_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        filed = str(data.get("snapshot", ""))
        prefix = path.name.rsplit("-", 1)[0] if "-20" in path.name else path.stem
        prefix = path.name.split(f"-{_safe(filed)}")[0] if filed else path.stem
        by_prefix[prefix] = filed
        if filed != snapshot_id:
            continue
        for f in data.get("findings", []):
            if isinstance(f, dict) and f.get("id"):
                severity = f.get("severity", "info")
                rows.append(
                    {
                        "id": f["id"],
                        "check": f.get("check", ""),
                        "severity": severity,
                        # K06: the rank is EMITTED, so the Go side orders by what Python decided
                        # rather than by a mirrored severity table of its own. A second ordering is
                        # a second authority, and the two can drift silently.
                        "rank": SEVERITY_ORDER.get(severity, 9),
                        "summary": f.get("summary", ""),
                        "source": path.name,
                    }
                )
    rows.sort(key=lambda r: (r["rank"], r["check"], r["id"]))
    return rows, by_prefix


def _latest_report(reports_dir: Path, prefix: str, snapshot_id: str) -> dict | None:
    exact = reports_dir / f"{prefix}-{_safe(snapshot_id)}.json"
    # the unsuffixed artifact is the snapshot's own harness; a runtime-tagged variant
    # (eval-matrix-sealed-…) is carried only when no exact file exists
    matches = (
        [exact]
        if exact.is_file()
        else sorted(reports_dir.glob(f"{prefix}-*{_safe(snapshot_id)}.json"))
    )
    if not matches:
        matches = sorted(reports_dir.glob(f"{prefix}-*.json"))
    if not matches:
        return None
    try:
        data = json.loads(matches[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    data["_source"] = matches[-1].name
    return data


def _sessions(live_dir: Path | None, *, now: datetime) -> list[dict[str, Any]]:
    """Recent top-level sessions read through Phase 4's reader: the last tool call and the
    dispatches in the trailing 24 hours. Python parses; the TUI only renders."""
    if live_dir is None or not Path(live_dir).is_dir():
        return []
    cutoff = now - timedelta(hours=24)
    recent = []
    for path in session_transcripts(live_dir):
        if path.parent.name == "subagents":
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            continue
        if mtime >= cutoff:
            recent.append((mtime, path))
    recent.sort(reverse=True)
    rows = []
    for mtime, path in recent[:RECENT_SESSIONS]:
        last_tool = last_at = ""
        dispatches = human = 0
        for event in iter_events(path):
            if event.kind == "user":
                human += 1
            elif event.kind == "tool_use":
                last_tool, last_at = event.name, event.timestamp
                if event.name in DISPATCH_TOOLS and event.timestamp >= cutoff.isoformat():
                    dispatches += 1
        rows.append(
            {
                "transcript": path.name,
                "modified_at": mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "human_turns": human,
                "last_tool": last_tool or None,
                "last_tool_at": last_at or None,
                "dispatches_24h": dispatches,
            }
        )
    return rows


def _message_text(record: dict[str, Any]) -> str:
    """Text blocks used only to recognise a terminal task notification."""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


LIVE_WINDOW = timedelta(seconds=60)
_STATUS_RANK = {"running": 0, "unknown": 1}


def _last_record(path: Path) -> dict[str, Any] | None:
    """The last parseable JSON record of a transcript, read from the tail, never the whole file."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            buf = b""
            while True:
                read = min(65536, size)
                size -= read
                fh.seek(size)
                buf = fh.read(read) + buf
                if size == 0 or buf.count(b"\n") >= 2:
                    lines = [ln for ln in buf.split(b"\n") if ln.strip()]
                    if size > 0:
                        lines = lines[1:]  # the first line may be cut
                    for ln in reversed(lines):
                        try:
                            record = json.loads(ln)
                        except ValueError:
                            continue
                        if isinstance(record, dict):
                            return record
                    if size == 0:
                        return None
    except OSError:
        return None


def _agent_outcome(agent_file: Path, *, now: datetime) -> str | None:
    """What a dispatch's OWN transcript says (K07a): completed, running, unknown; None when absent.

    A parent transcript that never carried a completion notification is not evidence the agent is
    running — measured 2026-09-03, three finished opus-reviewer dispatches read `running` for
    hours. The agent file ending on an assistant message with no tool call is completion; a write
    inside LIVE_WINDOW is life; anything else is unknown and is never drawn as running.
    """
    try:
        mtime = datetime.fromtimestamp(agent_file.stat().st_mtime, UTC)
    except OSError:
        return None
    last = _last_record(agent_file)
    if last is not None and last.get("type") == "assistant":
        content = (last.get("message") or {}).get("content")
        if isinstance(content, list) and not any(
            isinstance(block, dict) and block.get("type") == "tool_use" for block in content
        ):
            return "completed"
    if now - mtime <= LIVE_WINDOW:
        return "running"
    return "unknown"


def _parse_ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _recent_subagents(live_dir: Path | None, *, now: datetime) -> list[dict[str, Any]]:
    """Recent Agent dispatch ownership and status, parsed once on the Python side.

    T01 makes Python the transcript authority; this reader emits only metadata: no prompt,
    description, result, or notification prose is retained. Status is evidence or `unknown`:
    a parent-stream terminal notification or a direct tool_result is terminal; otherwise the
    agent's own transcript decides (`_agent_outcome`); a dispatch with neither is `running` only
    inside LIVE_WINDOW of its dispatch and `unknown` after.
    """
    if live_dir is None or not Path(live_dir).is_dir():
        return []
    cutoff = now - timedelta(hours=24)
    cutoff_text = cutoff.isoformat()
    calls: dict[str, dict[str, Any]] = {}
    where: dict[str, tuple[Path, datetime | None]] = {}
    direct_terminal: dict[str, str] = {}
    tool_to_agent: dict[str, str] = {}
    agent_terminal: dict[str, str] = {}

    for path in session_transcripts(live_dir):
        if path.parent.name == "subagents":
            continue
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
                continue
        except OSError:
            continue
        # Session ids are join keys. Abbreviate only in the TUI display; an eight-character
        # prefix collision can otherwise merge unrelated coordinator, chain, and child evidence.
        owner = path.stem
        for _, record in iter_records(path):
            for task_id, status in _TASK_TERMINAL.findall(_message_text(record)):
                agent_terminal[task_id] = status

            message = record.get("message") or {}
            content = message.get("content")
            if not isinstance(content, list):
                continue
            result = record.get("toolUseResult") or {}
            if not isinstance(result, dict):
                result = {}  # most tools write a plain string here; only a dispatch is a dict
            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    tool_id = str(block.get("tool_use_id") or "")
                    if not tool_id:
                        continue
                    if result.get("isAsync") and result.get("status") == "async_launched":
                        agent_id = str(result.get("agentId") or "")
                        if agent_id:
                            tool_to_agent[tool_id] = agent_id
                    else:
                        direct_terminal[tool_id] = (
                            "failed" if block.get("is_error") else "completed"
                        )
                    continue
                if block.get("type") != "tool_use" or block.get("name") not in DISPATCH_TOOLS:
                    continue
                timestamp = str(record.get("timestamp") or "")
                if timestamp and timestamp < cutoff_text:
                    continue
                tool_id = str(block.get("id") or f"{message.get('id', '')}:{index}")
                if not tool_id:
                    continue
                payload = block.get("input") or {}
                subagent_type = (
                    str(payload.get("subagent_type") or "general-purpose")
                    if isinstance(payload, dict)
                    else "general-purpose"
                )
                calls[tool_id] = {
                    "dispatch_id": hashlib.sha256(f"{owner}:{tool_id}".encode()).hexdigest()[:12],
                    "owner_session": owner,
                    "subagent_type": subagent_type,
                    "dispatched_at": timestamp or None,
                    "status": "unknown",
                }
                where[tool_id] = (path, _parse_ts(timestamp) if timestamp else None)

    for tool_id, row in calls.items():
        if tool_id in direct_terminal:
            row["status"] = direct_terminal[tool_id]
            continue
        agent_id = tool_to_agent.get(tool_id)
        if agent_id and agent_id in agent_terminal:
            row["status"] = agent_terminal[agent_id]
            continue
        path, dispatched = where[tool_id]
        outcome = None
        if agent_id:
            agent_file = path.parent / path.stem / "subagents" / f"agent-{agent_id}.jsonl"
            outcome = _agent_outcome(agent_file, now=now)
        if outcome is None:
            outcome = (
                "running"
                if dispatched is not None and now - dispatched <= LIVE_WINDOW
                else "unknown"
            )
        row["status"] = outcome

    rows = list(calls.values())
    # Running first, then unknown, then terminal; newest first within each; the id breaks ties.
    rows.sort(
        key=lambda row: (str(row.get("dispatched_at") or ""), row["dispatch_id"]), reverse=True
    )
    rows.sort(key=lambda row: _STATUS_RANK.get(row["status"], 2))
    return rows[:RECENT_SUBAGENTS]


def _corpus_digest(snapshot_id: str, corpus_dir: Path | None = None) -> str | None:
    """The pinned corpus's sha256 for this snapshot, or None.

    Read from the committed manifest rather than recomputed: the corpus is pinned by hash and the
    transcripts stay on disk where Claude Code put them (CLAUDE.md).
    """
    root = corpus_dir if corpus_dir is not None else CORPUS_DIR
    if not root.is_dir():
        return None
    matches = sorted(root.glob(f"manifest-*{_safe(snapshot_id)}.json"))
    if not matches:
        return None
    try:
        data = json.loads(matches[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    digest = data.get("sha256")
    return str(digest) if digest else None


def _subject_blocks(
    *,
    subject_root: Path | None,
    home: Path | str | None,
    session_id: str | None,
    sealed_manifest: Path | str | None,
    findings_dir: Path,
    snapshot_id: str,
    recent_session_ids: list[str] | None = None,
) -> dict[str, Any]:
    """The four subject-derived blocks, or an explicit not-inspected reading of each.

    No default subject: a cockpit built without one has NOT observed the harness, and saying so is
    the difference between `unreported` and `healthy`.
    """
    if subject_root is None:
        reason = (
            "no subject root supplied; this cockpit did not inspect the harness. That is "
            "unobserved, not healthy."
        )
        return {
            "tasks": {
                "status": "unavailable",
                "reason": reason,
                "sources": {},
                "session_id": None,
                "active": None,
                "rows": [],
                "unmatched_board_rows": [],
                "classes": dict.fromkeys(MISMATCH_CLASSES, 0),
            },
            "phase": {
                "status": "unknown",
                "current": None,
                "governing_plan": None,
                "reason": reason,
            },
            "runtime": {
                "status": "absent",
                "reason": reason,
                "installed_version": None,
                "installed_sha256": None,
                "sealed_version": None,
                "sealed_sha256": None,
                "drift": "unknown",
                "sources": {},
            },
            "coordinator": {
                "status": "unreported",
                "reason": reason,
                "source": "",
                "events_observed": 0,
                "latest": None,
                "sessions": [],
            },
            "hooks_health": {
                "status": "unreported",
                "reason": reason,
                "sources_read": [],
                "telemetry_events": 0,
                "crashed": [],
                "skipped": [],
                "unreachable": [],
                "coverage_gaps": [],
                "silent": [],
                "limits": list(HEALTH_LIMITS),
            },
            "review_obligations": {"status": "unreported", "reason": reason, "rows": []},
        }

    blocks = tasks_and_phase(subject_root=subject_root, home=home, session_id=session_id)
    observed = harness(
        subject_root=subject_root,
        sealed_manifest=sealed_manifest,
        findings_dir=findings_dir,
        snapshot_id=snapshot_id,
        recent_session_ids=recent_session_ids,
    )
    blocks.update(observed)
    latest = observed["coordinator"].get("latest")
    if latest is None:
        blocks["review_obligations"] = {
            "status": "unreported",
            "reason": "no latest coordinator event; review obligation was not observed",
            "rows": [],
        }
    else:
        review_rows = [
            {
                "event_id": latest.get("event_id"),
                "observed_at": latest.get("observed_at"),
                "reason_code": row.get("reason_code"),
            }
            for row in latest.get("evaluators", [])
            if row.get("gate_id") == "review-gate" and row.get("verdict") == "block"
        ]
        blocks["review_obligations"] = {
            "status": "carried",
            "reason": "",
            "rows": review_rows,
        }
    # K05: read once here so a subject_root always yields an explicit toolchain reading (`carried`
    # or `unobserved`) rather than the block staying silently absent. build_cockpit's own
    # `toolchain=` parameter, applied after this returns, still overrides it when a caller passes
    # one directly.
    blocks["toolchain"] = toolchain_from_subject(subject_root)
    return blocks


def _comparison(
    basis: dict[str, Any], baseline: dict[str, Any] | None, carried_ids: list[str]
) -> dict[str, Any]:
    """S04's rule, applied to the cockpit.

    A baseline that does not share the snapshot, the runtime hash AND the corpus digest is
    `incomparable` — never improvement, never regression. The three are checked together because
    any one of them moving is enough to make two finding sets describe different worlds: a
    different snapshot is a different harness, a different runtime hash is a different evaluator
    set, and a different corpus digest is a different denominator.
    """
    empty = {"resolved": [], "new": [], "unchanged": []}
    if baseline is None:
        return {
            "status": "not-requested",
            "reason": "no baseline supplied; the cockpit reports state, not a delta",
            "baseline": None,
            **empty,
        }

    differing = [field for field in BASIS_FIELDS if baseline.get(field) != basis.get(field)]
    baseline_basis = {field: baseline.get(field) for field in BASIS_FIELDS}
    if differing:
        return {
            "status": "incomparable",
            "reason": (
                "the baseline does not share this cockpit's "
                + ", ".join(differing)
                + "; two finding sets measured on a different snapshot, runtime or corpus "
                "describe different worlds and their difference is neither improvement nor "
                "regression"
            ),
            "baseline": baseline_basis,
            **empty,
        }

    old = set(baseline.get("finding_ids") or [])
    new = set(carried_ids)
    return {
        "status": "compared",
        "reason": "",
        "baseline": baseline_basis,
        "resolved": sorted(old - new),
        "new": sorted(new - old),
        "unchanged": sorted(old & new),
    }


def _evidence_chain(
    coordinator_sessions: list[dict[str, Any]], basis: dict[str, Any]
) -> list[dict[str, Any]]:
    """K02/J01: one row per recent session, read straight from `coordinator.sessions` (K04) and
    `basis` (S04) rather than re-derived.

    J01's real run recorded `cockpit_evidence_chain`: evidence-chain visibility inferred from other
    panes rather than read from the artifact. This gives the journey's step 11 the exact fields it
    needs: whether a Stop event exists for the session, whether the runtime that produced it is the
    one this cockpit's numbers rest on, and the coordinator's decision for it.
    """
    basis_sha = basis.get("runtime_sha256")
    rows: list[dict[str, Any]] = []
    for session in coordinator_sessions:
        session_sha = session.get("runtime_sha256")
        matches = None if session_sha is None or basis_sha is None else session_sha == basis_sha
        rows.append(
            {
                "session_id": session["session_id"],
                "latest_stop_event_id": session.get("latest_event_id"),
                "runtime_version": session.get("runtime_version"),
                "runtime_matches_basis": matches,
                "decision": session.get("decision", "none"),
            }
        )
    return rows


def build_cockpit(
    *,
    snapshot_id: str | None = None,
    findings_dir: Path | str | None = None,
    reports_dir: Path | str | None = None,
    live_dir: Path | str | None = None,
    snapshots_dir: Path | str | None = None,
    now: datetime | None = None,
    drift: Any | None = None,
    subject_root: Path | str | None = None,
    home: Path | str | None = None,
    session_id: str | None = None,
    sealed_manifest: Path | str | None = None,
    corpus_dir: Path | str | None = None,
    basis: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
    toolchain: dict[str, Any] | None = None,
    provider_observations_dir: Path | str | None = None,
    provider_work_observation: Path | str | None = None,
    work_binding: Path | str | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    findings_root = Path(findings_dir) if findings_dir is not None else FINDINGS_DIR
    reports_root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    latest = latest_snapshot_id(snapshots_dir)
    target = snapshot_id or latest or "unknown"
    rows, by_prefix = _findings_for(target, findings_root)
    stale = sorted(prefix for prefix, filed in by_prefix.items() if filed != target)

    depth = _latest_report(reports_root, "dlv-depth", target)
    matrix = _latest_report(reports_root, "eval-matrix", target)

    live_root = Path(live_dir) if live_dir is not None else None
    session_rows = _sessions(live_root, now=now)
    # K04: every session with transcript activity in the trailing 24h, so the coordinator block can
    # say `recorded` / `no_stop_reached` / `unreported` for each rather than only the busiest one.
    recent_session_ids = [Path(row["transcript"]).stem for row in session_rows]
    # J01 / finding 2026-09-03-002: the trailing-24h scan names *other* recent sessions; it is not
    # the source of truth for the session under test. A cockpit built for one session (session_id
    # set, live_dir absent — J01's own invocation shape) must still read that session's coordinator
    # telemetry, or its evidence chain filters to [] even when a Stop fired. Key on session_id too.
    if session_id and session_id not in recent_session_ids:
        recent_session_ids.append(session_id)

    subject = _subject_blocks(
        subject_root=Path(subject_root) if subject_root is not None else None,
        home=home,
        session_id=session_id,
        sealed_manifest=sealed_manifest,
        findings_dir=findings_root,
        snapshot_id=target,
        recent_session_ids=recent_session_ids,
    )
    resolved_basis = basis or {
        "snapshot_id": target,
        "runtime_sha256": subject["runtime"].get("installed_sha256"),
        "corpus_digest": _corpus_digest(
            target, Path(corpus_dir) if corpus_dir is not None else None
        ),
    }
    cockpit: dict[str, Any] = {
        "schema_version": COCKPIT_SCHEMA_VERSION,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot_id": target,
        "freshness": {
            "latest_snapshot": latest,
            "current": latest is None or latest == target,
            "findings_files": by_prefix,
            "stale_findings": stale,
        },
        "findings": {
            "count": len(rows),
            "by_severity": dict(Counter(r["severity"] for r in rows)),
            "top": rows[:TOP_FINDINGS],
            "status": "carried" if rows else "unavailable",
        },
        "ledger": [r for r in rows if r["check"] == "effectiveness-ledger"],
        "dlv_depth": (
            {
                "status": "carried",
                "source": depth["_source"],
                "classifier_version": depth.get("classifier_version"),
                "split_date": depth.get("split_date"),
                "windows": depth.get("windows", {}),
            }
            if depth
            else {"status": "unavailable", "reason": "no dlv-depth report for this snapshot"}
        ),
        "eval_matrix": (
            {
                "status": "carried",
                "source": matrix["_source"],
                "effect_rule": matrix.get("effect_rule"),
                "cells": matrix.get("cells", []),
            }
            if matrix
            else {
                "status": "unreported",
                "reason": "no persisted eval results; nothing paid has run",
            }
        ),
        # S05 (Sam, 2026-09-02): the cockpit carries the same drift flag the report marks STALE on,
        # so the TUI shows it on every refresh. Carried from a DriftReport; never computed here.
        "drift": (
            {
                "status": "carried",
                "snapshot": drift.snapshot,
                "moved": drift.moved,
                "head_moved": drift.head_moved,
                "changed": list(drift.changed),
                "added": list(drift.added),
                "removed": list(drift.removed),
            }
            if drift is not None
            else {"status": "not-checked", "reason": "no drift report supplied"}
        ),
        "sessions": session_rows,
        "provider_sessions": provider_observations(provider_observations_dir, now=now),
        "provider_work": provider_work_for_cockpit(
            subject_root=subject_root,
            provider_work_observation=provider_work_observation,
            work_binding=work_binding,
        ),
        "subagents": _recent_subagents(Path(live_dir) if live_dir is not None else None, now=now),
        "limits": [
            "Every number is carried from an artifact Bear Hug wrote or read through its one "
            "transcript reader; the cockpit derives no severity, rate or status of its own.",
            "A stale findings file is named, not merged; `unreported` cost is not zero cost.",
            "Drift is carried from the snapshot's drift report: `moved` means a captured harness "
            "file changed since the snapshot and every finding against it is STALE; HEAD movement "
            "alone is informational.",
            "`basis` names the snapshot, runtime hash and corpus digest every number rests on. A "
            "comparison across a different one of the three is `incomparable`, never an "
            "improvement.",
            "Provider work and BOARD authority are carried only from the exact observation and "
            "binding files supplied by the caller and must match this selected checkout.",
        ],
    }
    cockpit.update(subject)
    cockpit["basis"] = {field: resolved_basis.get(field) for field in BASIS_FIELDS}
    cockpit["comparison"] = _comparison(cockpit["basis"], baseline, [row["id"] for row in rows])
    cockpit["evidence_chain"] = _evidence_chain(
        cockpit["coordinator"].get("sessions", []), cockpit["basis"]
    )
    # OPTIONAL by design: an absent `toolchain` block means every stage is unobserved, and the Go
    # reader synthesises that rather than reading absence as clean.
    if toolchain is not None:
        cockpit["toolchain"] = toolchain
    return cockpit


def write_cockpit(cockpit: dict[str, Any], *, reports_dir: Path | str | None = None) -> Path:
    root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    path = assert_writable(root / "cockpit.json")
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cockpit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = [
    "BASIS_FIELDS",
    "COCKPIT_SCHEMA_VERSION",
    "build_cockpit",
    "latest_snapshot_id",
    "write_cockpit",
]
