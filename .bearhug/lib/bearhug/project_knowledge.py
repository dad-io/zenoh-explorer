"""Freshness of the project knowledge indexes grounding reads: read-only, never a refresh.

Grounding consults four project-owned stores. Three of them are generated indexes that only an
interactive session's hooks (SessionStart/Stop, Graft's own hooks) or an explicit command
rebuild; a headless capsule campaign never does. This module reports how old each index is and
what it claims about itself, so a grounding report can say ``memq ok · indexed 3h ago`` instead of
a bare ``ok`` over a stale index. It reads file metadata and existing status records only.

- ``memex``: authored decision records under ``docs/memex/decisions``; count and newest change.
- ``architecture``: ``bearhug.project_architecture.status`` (fresh/partial/stale/error/unreported).
- ``memq``: newest file under ``.memq/db`` (the embedding store the launcher binds).
- ``graft``: ``graft/INDEX.md`` (the context graph ``graft build`` writes); drift is not checked
  here because ``graft check`` re-parses the tree and is not read-only cheap.

Every timestamp is ISO-8601 UTC; every absence is reported as ``absent``, never guessed. The
report carries no clock reading of its own: identical indexes give identical bytes, so a draft that
seals it keeps a stable digest. ``with_ages`` adds ages for live display only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOLS = ("memex", "architecture", "graft", "memq")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _newest(paths: list[Path]) -> tuple[float | None, int]:
    newest: float | None = None
    count = 0
    for path in paths:
        try:
            if not path.is_file():
                continue
            count += 1
            stamp = path.stat().st_mtime
        except OSError:
            continue
        newest = stamp if newest is None or stamp > newest else newest
    return newest, count


def _tree(root: Path, limit: int = 20000) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    found: list[Path] = []
    for path in root.rglob("*"):
        found.append(path)
        if len(found) >= limit:
            break
    return found


def knowledge_freshness(root: Path | str, *, now: datetime | None = None) -> dict[str, Any]:
    """Describe each knowledge index's age and self-reported state without touching it."""

    subject = Path(root).expanduser().resolve()
    del now  # ages are never sealed; callers derive them at read time via `with_ages`
    report: dict[str, Any] = {"tools": {}}

    decisions = subject / "docs/memex/decisions"
    newest, count = _newest(list(decisions.glob("*.md")) if decisions.is_dir() else [])
    report["tools"]["memex"] = (
        {"status": "present", "records": count, "updated_at": _iso(newest), "kind": "authored"}
        if newest is not None
        else {"status": "absent", "records": 0, "kind": "authored"}
    )

    try:
        from bearhug import project_architecture

        arch = project_architecture.status(subject)
        report["tools"]["architecture"] = {
            "status": str(arch.get("status") or "unreported"),
            "reason": str(arch.get("reason") or ""),
            "updated_at": arch.get("observed_at"),
            "head": arch.get("head"),
            "kind": "generated",
        }
    except Exception as exc:  # noqa: BLE001 - a freshness probe must never raise into grounding
        report["tools"]["architecture"] = {
            "status": "error",
            "reason": f"cannot read architecture status: {exc}",
            "kind": "generated",
        }

    newest, count = _newest(_tree(subject / ".memq/db"))
    report["tools"]["memq"] = (
        {"status": "indexed", "files": count, "updated_at": _iso(newest), "kind": "generated"}
        if newest is not None
        else {
            "status": "absent",
            "files": 0,
            "kind": "generated",
            "reason": "no .memq/db; run scripts/bin/memq index",
        }
    )

    index = subject / "graft/INDEX.md"
    try:
        stamp = index.stat().st_mtime if index.is_file() else None
    except OSError:
        stamp = None
    report["tools"]["graft"] = (
        {
            "status": "built",
            "updated_at": _iso(stamp),
            "kind": "generated",
            "drift_check": "not_run",
        }
        if stamp is not None
        else {
            "status": "absent",
            "kind": "generated",
            "reason": "no graft/INDEX.md; run scripts/bin/graft build",
        }
    )

    return report


def age_seconds(updated_at: object, *, now: datetime | None = None) -> int | None:
    """Seconds since an ISO timestamp, or None when it is absent or unparsable."""
    if not isinstance(updated_at, str):
        return None
    try:
        stamp = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0, int(((now or datetime.now(UTC)) - stamp).total_seconds()))


def with_ages(report: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Copy a freshness report adding `observed_at` and per-tool `age_seconds` for live display.

    Kept out of `knowledge_freshness` on purpose: that report is sealed into the onboarding draft,
    and a clock reading inside it would change the draft digest on every identical derivation.
    """
    moment = now or datetime.now(UTC)
    tools = {}
    for name, tool in report.get("tools", {}).items():
        copy = dict(tool)
        age = age_seconds(tool.get("updated_at"), now=moment)
        if age is not None:
            copy["age_seconds"] = age
        tools[name] = copy
    return {**report, "observed_at": moment.isoformat(timespec="seconds"), "tools": tools}


def age_label(seconds: int | None) -> str:
    if seconds is None:
        return "age unknown"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


__all__ = ["TOOLS", "age_label", "age_seconds", "knowledge_freshness", "with_ages"]
