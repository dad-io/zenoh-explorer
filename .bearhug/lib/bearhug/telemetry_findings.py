"""Turn Stop coordinator telemetry into FINDINGS, without re-reading a whole day's file at once.

The coordinator writes one `coordinator_evaluation` record per evaluator per Stop to
`<subject>/.bearhug/telemetry/v1/<YYYY-MM-DD>/events.jsonl` (see
`runtime/bearhug_runtime/telemetry.py` and `telemetry_store.py`, which own the writer and the
schema). That file is the only record of a real block an operator would otherwise have to read by
hand. This module is the read side for three shapes worth surfacing during startup observation:

1. **Repeated block on one write** — the same `gate_id` blocked the same `session_id` two or more
   times at the same `write_position`. Costly: it is a wasted turn re-litigating one write.
2. **Block on a write outside the project** — a block whose evidence says `write_in_project=false`.
   Broken: the gate fired on something that was never this project's problem.
3. **Unknown provenance** — a block that carries no `write_in_project` entry at all. Reported once
   per run, as info, and NEVER folded into "false": `write_in_project` is a fact the runtime either
   recorded or did not, and a record from before the evidence contract existed simply does not know.

Every value pulled out of a record is DATA — a session id, a gate id, an evidence string are all
either operator-authored prose or another module's bug surface, never trusted beyond the narrow
`key=value` shape this module itself looks for. A line that does not parse, or a record that lacks
a field this module needs, is skipped rather than guessed at; nothing here repairs telemetry.

Bounded like `startup_readiness.py`'s own directory walks: at most the last `MAX_TELEMETRY_DAYS`
dated directories (newest first), at most `MAX_FILES` files, and a line longer than `MAX_JSON_BYTES`
is treated as unparseable rather than loaded. Each `events.jsonl` is read one line at a time — never
`read_text()`'d whole — because a real session's file only grows.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bearhug.model import Evidence, Finding, Severity
from bearhug.startup_readiness import MAX_FILES, MAX_JSON_BYTES

#: One check name for every finding this module emits, so the FINDINGS tile groups them together.
CHECK = "TELEMETRY-STOP"

#: "Scan only dated directories, newest first, at most the last 7 days."
MAX_TELEMETRY_DAYS = 7

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RECORD_KIND = "coordinator_evaluation"


def _session_prefix(session_id: str) -> str:
    """A short, citable handle for a message — never the full id, which a summary line would
    otherwise wrap awkwardly and which nothing here needs in full."""
    return f"{session_id[:8]}…" if len(session_id) > 8 else session_id


def _state_evidence(evidence: Any) -> dict[str, str]:
    """The evidence list's ``state``-kind entries as a ``key -> value`` map.

    Only entries shaped exactly like ``key=value`` are read; anything else (a different kind, a
    value with no ``=``, a non-string value) is left out rather than misparsed into a wrong key.
    """
    out: dict[str, str] = {}
    if not isinstance(evidence, list):
        return out
    for item in evidence:
        if not isinstance(item, dict) or item.get("kind") != "state":
            continue
        value = item.get("value")
        if not isinstance(value, str) or "=" not in value:
            continue
        key, _, val = value.partition("=")
        if key:
            out[key] = val
    return out


def _dated_dirs(root: Path) -> list[Path]:
    """Every ``YYYY-MM-DD`` subdirectory of ``root``, newest first, capped at the last 7 days."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    names = sorted(
        (entry.name for entry in entries if _DAY_RE.match(entry.name) and entry.is_dir()),
        reverse=True,
    )
    return [root / name for name in names[:MAX_TELEMETRY_DAYS]]


def _iter_coordinator_records(subject: Path):
    """Yield ``(events.jsonl path, record dict)`` for every parseable coordinator_evaluation line.

    Streams each day's file one line at a time. A line over ``MAX_JSON_BYTES``, a line that is not
    JSON, or a JSON value that is not an object is skipped — never buffered whole, never repaired.
    ``MAX_FILES`` bounds how many daily files this walk will open, mirroring the cap
    `startup_readiness._observed` already applies to the same telemetry directory.
    """
    root = Path(subject) / ".bearhug" / "telemetry" / "v1"
    opened = 0
    for day_dir in _dated_dirs(root):
        if opened >= MAX_FILES:
            break
        path = day_dir / "events.jsonl"
        if not path.is_file():
            continue
        opened += 1
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for raw_line in stream:
                    line = raw_line.strip()
                    if not line or len(line.encode("utf-8", "replace")) > MAX_JSON_BYTES:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and record.get("record_kind") == _RECORD_KIND:
                        yield path, record
        except OSError:
            continue


def _identity(record: dict) -> tuple[str, str, str, str] | None:
    """The four identity fields every finding here names, or None if any is missing/malformed."""
    session_id = record.get("session_id")
    gate_id = record.get("gate_id")
    event_id = record.get("event_id")
    observed_at = record.get("observed_at")
    fields = (session_id, gate_id, event_id, observed_at)
    if not all(isinstance(value, str) and value for value in fields):
        return None
    return session_id, gate_id, event_id, observed_at  # type: ignore[return-value]


def telemetry_findings(subject: Path, *, snapshot_id: str) -> list[Finding]:
    """Scan one subject's telemetry store and return the findings it supports.

    Read-only: opens `events.jsonl` files for reading only, and writes nothing. Safe to call
    against a live, growing telemetry store — every file is read to EOF once and closed.
    """
    subject = Path(subject)
    repeats: dict[tuple[str, str, str], dict[str, Any]] = {}
    outside_rows: list[Finding] = []
    unknown_count = 0

    for path, record in _iter_coordinator_records(subject):
        result = record.get("result")
        if not isinstance(result, dict) or result.get("verdict") != "block":
            continue
        identity = _identity(record)
        if identity is None:
            continue
        session_id, gate_id, event_id, observed_at = identity
        state = _state_evidence(result.get("evidence"))
        write_position = state.get("write_position")
        write_in_project = state.get("write_in_project")
        write_path = state.get("write_path")

        if write_position is None:
            # Not a write-shaped block (e.g. task-durability's phase-tag check): the evidence
            # contract this module codes against only ever applies to writes, so a block with no
            # write_position says nothing about write provenance either way.
            continue

        key = (session_id, gate_id, write_position)
        bucket = repeats.setdefault(
            key, {"count": 0, "first": observed_at, "last": observed_at, "path": path}
        )
        bucket["count"] += 1
        bucket["first"] = min(bucket["first"], observed_at)
        bucket["last"] = max(bucket["last"], observed_at)

        if write_in_project is None:
            unknown_count += 1
        elif write_in_project == "false":
            outside_rows.append(
                Finding(
                    id=f"telemetry-outside-project-{event_id}",
                    check=CHECK,
                    severity=Severity.BROKEN,
                    summary=(
                        f"Gate {gate_id} blocked session {_session_prefix(session_id)} on a "
                        f"write outside the project (write_path={write_path or 'opaque'}, "
                        f"observed_at={observed_at})"
                    ),
                    snapshot=snapshot_id,
                    evidence=(Evidence(file=str(path), run_id=event_id),),
                    limit=(
                        "write_path is the evaluator's own resolved evidence string, redacted and "
                        "capped before it reached telemetry; this does not independently "
                        "re-resolve the path."
                    ),
                )
            )

    rows: list[Finding] = list(outside_rows)
    for (session_id, gate_id, write_position), bucket in repeats.items():
        if bucket["count"] < 2:
            continue
        rows.append(
            Finding(
                id=f"telemetry-repeat-block-{session_id[:8]}-{gate_id}-{write_position}",
                check=CHECK,
                severity=Severity.COSTLY,
                summary=(
                    f"Gate {gate_id} blocked session {_session_prefix(session_id)} "
                    f"{bucket['count']} times at write position {write_position} "
                    f"(first {bucket['first']}, last {bucket['last']})"
                ),
                snapshot=snapshot_id,
                evidence=(Evidence(file=str(bucket["path"])),),
                limit=(
                    "Counts Stop telemetry records only; does not establish whether the write was "
                    "ever corrected, or whether other sessions hit the same position."
                ),
            )
        )

    if unknown_count:
        rows.append(
            Finding(
                id="telemetry-unknown-provenance",
                check=CHECK,
                severity=Severity.INFO,
                summary=(
                    f"{unknown_count} block(s) carry no write provenance; runtime predates the "
                    "evidence contract"
                ),
                snapshot=snapshot_id,
                evidence=(Evidence(file=str(subject / ".bearhug" / "telemetry" / "v1")),),
                limit=(
                    "Unknown provenance is not the same as an in-project write: these records "
                    "were never told to say write_in_project=false, so this count is never added "
                    "to the outside-project finding above."
                ),
            )
        )
    return rows


def write_telemetry_findings(subject: Path, root: Path, *, snapshot_id: str) -> Path:
    """Scan `subject`'s telemetry and write `<root>/findings/telemetry-<snapshot_id>.json`.

    Fits the same pattern `bearhug lint`/`bearhug hooks audit` write to: a stage owns one prefix
    under `findings/`, and `bearhug cockpit` (src/bearhug/replay/cockpit.py `_findings_for`) already
    globs every file there, so nothing else needs to name this stage for it to reach the FINDINGS
    tile or the web page's "Reported exceptions" panel.
    """
    from bearhug.report import write_findings

    rows = telemetry_findings(subject, snapshot_id=snapshot_id)
    return write_findings(
        rows, out_dir=Path(root) / "findings", snapshot_id=snapshot_id, prefix="telemetry"
    )
