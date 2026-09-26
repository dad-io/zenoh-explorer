"""R18 — append under concurrency, rotate daily, read back without reinterpreting.

The protocol's storage contract: acquire `events.jsonl.lock` with `flock(LOCK_EX)`, encode one
record plus a newline, and issue ONE `os.write` to a descriptor opened
`O_APPEND|O_CREAT|O_WRONLY`. On any lock/open/write failure, drop the observation and preserve the
decision.

The single write under a lock exists for one failure: two coordinators ending turns at the same
moment must not interleave halves of two records into one unparseable line. A reader that then
guessed at the halves would report a defect that never happened, which is worse than the lost
observation.

**Telemetry is an observer, not an authority.** Every failure path here returns False rather than
raising, because a telemetry failure may never change the selected verdict, the remediation, the
exit status, or the Claude Code response.

The reader is a separate entry point from the writer so lab ingestion is read-only by construction
rather than by care.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

#: Versions this reader understands. A reader may support several versions but must never
#: REINTERPRET an old one, so an unknown version is refused rather than coerced.
SUPPORTED_SCHEMA_VERSIONS = ("1",)

#: Raw records are kept for 30 days, then the whole daily file is deleted.
RETENTION_DAYS = 30

_FILENAME = "events.jsonl"


def project_id(project_root: Any) -> str:
    """A repository's identity inside the store path: its name, plus a digest of its location.

    Round 6's design finding, and it was the right shape of objection: moving the telemetry root
    out of the repository was correct, but the root it moved to is shared by every consumer of
    `~/.claude` on the machine, and a record's fifteen keys name no repository. After install,
    Barracuda's records, bear-hug's suite and any other project would interleave in one
    `events.jsonl`, and `audit.py` reading it could not say which project — or which test run — a
    record came from.

    **A digest, not the path.** The protocol's redaction rule strips home-directory prefixes and
    forbids retaining anything that is not a schema field; an absolute repository path can carry a
    username. The basename is the repository's own name, which `manifest.json` and the git origin
    already carry in the open, and the digest disambiguates two checkouts sharing a name without
    keeping where either one is.

    Deliberately in the PATH rather than in the record. Adding a sixteenth key would change a
    closed schema and force a version bump on every reader; a directory answers the same question,
    keeps `expire()`'s whole-file deletion intact, and lets a reader select one project by walking
    rather than by filtering.
    """
    import hashlib
    import re

    resolved = os.path.abspath(str(project_root))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    name = os.path.basename(resolved.rstrip(os.sep)) or "unknown"
    return f"{re.sub(r'[^A-Za-z0-9._-]', '-', name)[:40]}-{digest}"


def default_root(project_root: Any = None) -> Path:
    """The location `docs/RUNTIME-PROTOCOL.md` declares, resolved here and nowhere else.

    `<project_root>/.bearhug/telemetry/v1` — INSIDE the project, in a directory the project's
    `.gitignore` must list. Ruled by Sam on 2026-09-01 (D08): the system's state is project-based,
    not split between the repository and `~/.claude`.

    History, kept because it is the reason the two guards below exist. Until 2026-09-01 the root was
    `${CLAUDE_CONFIG_DIR:-~/.claude}/telemetry/bearhug/v1/<project-id>`, chosen after round 5 found
    the coordinator falling back to the event's `cwd` and writing `<repo>/telemetry/<date>/` into
    the working tree, untracked and un-ignored. Moving back inside the tree is deliberate and comes
    with two guards the round-5 arrangement lacked: the directory is `.bearhug/`, which the
    promotion package's `.gitignore` line covers and the verifier checks on the host, and a record
    outside `.bearhug/` anywhere in the tree fails the verifier. Project scoping is now by location,
    so the `<project-id>` digest segment is gone; `project_id()` remains for records that name a
    repository without keeping its path.
    """
    if project_root is None:
        project_root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(project_root) / ".bearhug" / "telemetry" / "v1"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def daily_path(root: Any, *, day: str | None = None) -> Path:
    """`<root>/<YYYY-MM-DD>/events.jsonl`.

    Dated directories make expiry a whole-file deletion rather than a rewrite: rewriting a file to
    drop old records could lose one written concurrently.
    """
    return Path(root) / (day or _today()) / _FILENAME


def append(record: Any, *, root: Any, day: str | None = None) -> bool:
    """Append one record. Returns True if it landed, False if the observation was dropped.

    Never raises. R17 returns None when redaction could not complete, and None must not be
    written — a store that wrote it would defeat the drop.
    """
    if record is None:
        return False

    try:
        # Encode BEFORE opening anything: an unencodable record should not create a file.
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        return False

    path = daily_path(root, day=day)
    lock_path = path.with_name(path.name + ".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    lock_fd = None
    data_fd = None
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data_fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        # ONE write. Two writes could interleave with another process between them even under the
        # lock's own release, and O_APPEND only makes a single write atomic.
        os.write(data_fd, line)
        return True
    except (OSError, ValueError):
        return False
    finally:
        for descriptor in (data_fd, lock_fd):
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)


def read_day(root: Any, *, day: str | None = None) -> tuple[list[dict], list[str]]:
    """Return (records, failures) for one day. Reads only; never repairs and never guesses.

    A partial final line — a process killed mid-write — is reported as a failure and ignored. It
    is never reconstructed: a guessed record is a fact nobody observed.
    """
    path = daily_path(root, day=day)
    records: list[dict] = []
    failures: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return records, failures

    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            failures.append(f"line {number}: unparseable ({exc.__class__.__name__})")
            continue
        if not isinstance(parsed, dict):
            failures.append(f"line {number}: not an object")
            continue
        version = str(parsed.get("schema_version"))
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            failures.append(
                f"line {number}: unsupported schema_version {version!r}; refused rather than "
                "reinterpreted"
            )
            continue
        records.append(parsed)
    return records, failures


def expire(root: Any, *, today: str | None = None, keep_days: int = RETENTION_DAYS) -> int:
    """Delete whole daily directories older than the window. Returns how many were removed.

    Whole files, never a rewrite: a rewrite could drop a record written concurrently, and the
    point of the retention rule is to lose OLD observations, not recent ones.
    """
    root_path = Path(root)
    reference = date.fromisoformat(today) if today else date.fromisoformat(_today())
    cutoff = reference - timedelta(days=keep_days)
    removed = 0
    try:
        entries = sorted(root_path.iterdir())
    except OSError:
        return 0

    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            when = date.fromisoformat(entry.name)
        except ValueError:
            # Not a dated directory. Left alone rather than guessed at.
            continue
        if when < cutoff:
            try:
                shutil.rmtree(entry)
                removed += 1
            except OSError:
                # Best-effort, like every other path here.
                continue
    return removed


def expire_store(
    project_root: Any, *, today: str | None = None, keep_days: int = RETENTION_DAYS
) -> int:
    """Expire the dated files under one project's own store.

    Since D08 (2026-09-01) the store is `<repo>/.bearhug/telemetry/v1`, one per repository. There
    are no sibling projects to sweep: another repository's `.bearhug/` is that repository's, and a
    writer here has no authority to delete inside it. The parent-of-`v1` traversal below therefore
    only fires for the pre-D08 home-directory layout (`.../bearhug/v1/<project>`), where it still
    protects abandoned worktrees' records from living forever; an explicit scratch root is never
    treated as authority over its siblings.
    """
    root_path = Path(project_root)
    store_root = root_path.parent
    if store_root.name != "v1":
        # Explicit roots are supported by tests and verification runs.  Only the declared
        # ``.../bearhug/v1/<project>`` shape grants authority to enumerate sibling projects;
        # otherwise ``/tmp/observability`` would make retention traverse unrelated `/tmp` trees.
        return expire(root_path, today=today, keep_days=keep_days)
    removed = expire(store_root, today=today, keep_days=keep_days)
    try:
        projects = sorted(store_root.iterdir())
    except OSError:
        return removed

    for project in projects:
        try:
            if not project.is_dir() or project.is_symlink():
                continue
        except OSError:
            continue
        removed += expire(project, today=today, keep_days=keep_days)
    return removed
