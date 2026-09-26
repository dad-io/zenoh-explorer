"""One truthful Bear Hug segment for a provider status line.

A status line is redrawn constantly, so this reads only small cached files and never
opens a prepared record, a campaign state machine or a Git repository. That cache is
refreshed when the driver runs, not when this renders, so every line carries the age of
what it is reporting: a stale reading is reported as stale rather than as fact, and a
source that is missing is named as missing rather than rendered as a healthy default.

Stdlib only, and no failure here may break the host's status line: `render` converts any
unexpected error into a segment that says so.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_MAX_READ_BYTES = 1 << 20
_LABEL = "bear hug"
# Beyond this the cached reading is old enough that presenting it without emphasis would
# imply a liveness the file does not have.
_STALE_SECONDS = 15 * 60


def _colour(code: str, text: str, *, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > _MAX_READ_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _age(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _subject_root(start: Path) -> Path | None:
    """The nearest enclosing checkout that Bear Hug has been installed into."""

    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".bearhug").is_dir():
            return candidate
    return None


def _custody(session: dict[str, Any]) -> str | None:
    leases = session.get("active_leases")
    if isinstance(leases, int) and leases > 0:
        return f"{leases} lease" + ("s" if leases != 1 else "")
    if session.get("unresolved_spend") is True:
        return "unresolved spend"
    if session.get("custody_active") is True:
        # Custody with no lease and no fence is the launch-marker case: name it exactly,
        # because "active" alone would suggest a worktree that does not exist.
        return "custody marker"
    return None


def _worker_phase(session: dict[str, Any]) -> str | None:
    worker = session.get("worker")
    if not isinstance(worker, dict):
        return None
    if worker.get("status") == "started" and worker.get("finished_at") is None:
        operation = worker.get("operation")
        return f"{operation} running" if isinstance(operation, str) else "worker running"
    return None


def segment(root: Path, *, now: float | None = None) -> str:
    """Render the Bear Hug segment for one installed checkout, without colour."""

    now = time.time() if now is None else now
    binding = _read_json(root / ".bearhug" / "campaign.json")
    if binding is None:
        return f"{_LABEL} · installed · no campaign"
    if binding.get("enabled") is False:
        return f"{_LABEL} · opted out"

    profile = _read_json(root / ".bearhug" / "terminal-driver.json")
    if profile is None:
        return f"{_LABEL} · campaign bound · profile unreadable"

    task = binding.get("task_id")
    provider = binding.get("provider") or profile.get("provider")
    session_id = binding.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        head = f"{_LABEL} · accepted · not dispatched"
        return f"{head} · {provider}" if isinstance(provider, str) else head

    state_root = profile.get("state_root")
    if not isinstance(state_root, str):
        return f"{_LABEL} · {task or 'campaign'} · state root unknown"
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    session_path = Path(state_root) / "sessions" / f"{digest}.json"
    session = _read_json(session_path)
    if session is None:
        return f"{_LABEL} · {task or 'campaign'} · no session record"

    try:
        observed = now - session_path.stat().st_mtime
    except OSError:
        observed = None

    parts = [_LABEL]
    status = _worker_phase(session) or session.get("status")
    parts.append(f"{task} {status}" if task and isinstance(status, str) else str(task or status))
    if isinstance(provider, str):
        parts.append(provider)
    custody = _custody(session)
    parts.append(custody if custody else "no custody")
    age = _age(observed)
    if age is not None:
        # The reading is a cache of the last driver turn, never a live probe.
        parts.append(f"{age} old" if observed and observed > _STALE_SECONDS else age)
    return " · ".join(parts)


def render(payload: dict[str, Any] | None = None, *, now: float | None = None) -> str:
    """Render the segment for a provider status-line payload, never raising."""

    try:
        payload = payload if isinstance(payload, dict) else {}
        workspace = payload.get("workspace")
        start = None
        if isinstance(workspace, dict):
            start = workspace.get("current_dir") or workspace.get("project_dir")
        start = start or payload.get("cwd") or os.getcwd()
        root = _subject_root(Path(str(start)))
        # Every outcome falls through to one place, so colour cannot depend on which
        # branch produced the text.
        body = f"{_LABEL} · not installed here" if root is None else segment(root, now=now)
    except Exception as exc:  # A status line must not take the host down with it.
        body = f"{_LABEL} · unreadable: {type(exc).__name__}"
    # A status line is always read through a pipe by the host, which renders the ANSI
    # itself, so isatty() would suppress colour exactly where it is wanted.
    if os.environ.get("NO_COLOR"):
        return body
    label, _, rest = body.partition(" · ")
    return _colour("38;5;208", label, enabled=True) + _colour("38;5;244", " · ", enabled=True) + (
        _colour("38;5;251", rest, enabled=True) if rest else ""
    )


def main(argv: list[str] | None = None) -> int:
    """Read one status-line payload on stdin and print one line."""

    del argv
    raw = ""
    if not sys.stdin.isatty():
        raw = sys.stdin.read(_MAX_READ_BYTES)
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    print(render(payload if isinstance(payload, dict) else {}))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
