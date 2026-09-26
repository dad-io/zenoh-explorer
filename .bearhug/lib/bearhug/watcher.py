"""Closed lease/heartbeat metadata for watcher-owned cockpit projections.

The heartbeat is a liveness observation, not campaign or provider authority.  A writer publishes
one stable identity for its lifetime and increments the sequence on every replacement of the
projection.  Readers can therefore distinguish an old heartbeat from a new watcher when they have
an expected identity, while still treating the process table and provider sessions as separate
observations.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class WatcherError(ValueError):
    """Watcher metadata is malformed or does not bind to the selected subject."""


_FIELDS = frozenset(
    {
        "status",
        "watcher_id",
        "scope",
        "subject_path_sha256",
        "heartbeat_sequence",
        "heartbeat_at",
        "stale_after_seconds",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCOPES = frozenset({"persistent", "private"})


def new_watcher_id() -> str:
    """Return an unpredictable, content-shaped identity for one watcher lifetime."""

    return hashlib.sha256(os.urandom(32)).hexdigest()


def subject_path_sha256(subject: Path | str) -> str:
    return hashlib.sha256(os.fsencode(Path(subject).expanduser().resolve(strict=True))).hexdigest()


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise WatcherError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise WatcherError(f"{label} must be a UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise WatcherError(f"{label} must be a canonical UTC timestamp")
    return value


def watcher_record(
    *,
    watcher_id: str,
    scope: str,
    subject: Path | str,
    heartbeat_sequence: int,
    heartbeat_at: datetime | None = None,
    stale_after_seconds: int,
) -> dict[str, Any]:
    """Build one canonical heartbeat object; validation is applied before returning it."""

    if not isinstance(watcher_id, str) or _SHA256.fullmatch(watcher_id) is None:
        raise WatcherError("watcher_id must be lowercase SHA-256")
    if scope not in _SCOPES:
        raise WatcherError("watcher scope is unsupported")
    if type(heartbeat_sequence) is not int or heartbeat_sequence < 1:
        raise WatcherError("heartbeat_sequence must be positive")
    if type(stale_after_seconds) is not int or stale_after_seconds < 1:
        raise WatcherError("stale_after_seconds must be positive")
    moment = heartbeat_at or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise WatcherError("heartbeat_at must be timezone-aware")
    record = {
        "status": "active",
        "watcher_id": watcher_id,
        "scope": scope,
        "subject_path_sha256": subject_path_sha256(subject),
        "heartbeat_sequence": heartbeat_sequence,
        "heartbeat_at": moment.astimezone(UTC).replace(microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "stale_after_seconds": stale_after_seconds,
    }
    return validate_watcher(record)


def validate_watcher(value: Any) -> dict[str, Any]:
    """Validate the closed writer-side heartbeat object without reading ambient state."""

    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise WatcherError("watcher record is not closed")
    if value["status"] != "active":
        raise WatcherError("watcher record must be published with active status")
    if not isinstance(value["watcher_id"], str) or _SHA256.fullmatch(value["watcher_id"]) is None:
        raise WatcherError("watcher_id must be lowercase SHA-256")
    if value["scope"] not in _SCOPES:
        raise WatcherError("watcher scope is unsupported")
    if type(value["heartbeat_sequence"]) is not int or value["heartbeat_sequence"] < 1:
        raise WatcherError("heartbeat_sequence must be positive")
    if (
        not isinstance(value["subject_path_sha256"], str)
        or _SHA256.fullmatch(value["subject_path_sha256"]) is None
    ):
        raise WatcherError("subject_path_sha256 must be lowercase SHA-256")
    _timestamp(value["heartbeat_at"], "heartbeat_at")
    if type(value["stale_after_seconds"]) is not int or value["stale_after_seconds"] < 1:
        raise WatcherError("stale_after_seconds must be positive")
    return dict(value)


def classify_watcher(
    value: Any,
    *,
    now: datetime,
    expected_subject: Path | str | None = None,
    expected_watcher_id: str | None = None,
) -> tuple[str, str | None, float | None, str | None]:
    """Return status, identity, age, and reason for a reader-side diagnostic."""

    try:
        record = validate_watcher(value)
    except WatcherError as exc:
        return "malformed", None, None, str(exc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise WatcherError("diagnostic time must be timezone-aware")
    heartbeat = datetime.strptime(record["heartbeat_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    age = (now.astimezone(UTC) - heartbeat).total_seconds()
    if expected_watcher_id is not None and record["watcher_id"] != expected_watcher_id:
        return (
            "replaced",
            record["watcher_id"],
            age,
            "watcher identity differs from the expected lease",
        )
    if expected_subject is not None:
        try:
            expected = subject_path_sha256(expected_subject)
        except (OSError, ValueError) as exc:
            return "wrong_subject", record["watcher_id"], age, f"cannot bind subject: {exc}"
        if record["subject_path_sha256"] != expected:
            return (
                "wrong_subject",
                record["watcher_id"],
                age,
                "watcher heartbeat belongs to another subject",
            )
    if age < 0:
        return "stale", record["watcher_id"], age, "watcher heartbeat is in the future"
    if age > record["stale_after_seconds"]:
        return "stale", record["watcher_id"], age, "watcher heartbeat exceeded its lease"
    return "active", record["watcher_id"], age, None


__all__ = [
    "WatcherError",
    "classify_watcher",
    "new_watcher_id",
    "subject_path_sha256",
    "validate_watcher",
    "watcher_record",
]
