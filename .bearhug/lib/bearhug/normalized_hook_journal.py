"""Append-only storage for provider-neutral hook events and aggregate results.

The journal stores only the already privacy-bounded records from :mod:`bearhug.normalized_hooks`.
Its envelope supplies a per-session stream identity, contiguous sequence, and hash chain without
changing the stable event or result identities.  It is storage infrastructure only: it does not
parse provider transcripts, install hooks, or claim that a provider runtime emitted anything.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from bearhug.normalized_hooks import (
    NormalizedHookError,
    validate_normalized_hook_event,
    validate_normalized_hook_result,
)
from bearhug.paths import assert_writable


class NormalizedHookJournalError(ValueError):
    """A normalized hook journal or append request is malformed or unsafe."""


_ENTRY_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "entry_id",
        "stream_id",
        "sequence",
        "previous_entry_id",
        "payload_kind",
        "event",
        "result",
    }
)
_ENTRY_DOMAIN = b"bear-hug/normalized-hook-journal-entry/v1\0"
_STREAM_DOMAIN = b"bear-hug/normalized-hook-journal-stream/v1\0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENTRY_FILE = re.compile(r"(?P<sequence>[0-9]{20})-(?P<entry>[0-9a-f]{64})\.json\Z")
_TEMP_FILE = re.compile(
    r"\.(?P<sequence>[0-9]{20})-(?P<entry>[0-9a-f]{64})\.json"
    r"\.tmp-(?P<pid>[0-9]+)-(?P<nonce>[0-9a-f]{16})\Z"
)
_MAX_SEQUENCE = 2**63 - 1
_MAX_ENTRY_BYTES = 160 * 1024


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NormalizedHookJournalError(f"value is not canonical JSON: {exc}") from exc


def _exact(value: Any, fields: frozenset[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise NormalizedHookJournalError(f"{where} has missing or unknown fields")
    return value


def _sha256(value: Any, where: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise NormalizedHookJournalError(f"{where} must be lowercase SHA-256")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NormalizedHookJournalError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def normalized_hook_stream_id(event: Mapping[str, Any]) -> str:
    """Return the stable session/worktree stream identity for one normalized event."""

    try:
        checked = validate_normalized_hook_event(event)
    except NormalizedHookError as exc:
        raise NormalizedHookJournalError(f"invalid normalized hook event: {exc}") from exc
    material = {
        "provider": checked["source"]["provider"],
        "session_id": checked["identity"]["session_id"],
        "thread_id": checked["identity"]["thread_id"],
        "common_dir_sha256": checked["repository"]["common_dir_sha256"],
        "worktree_sha256": checked["repository"]["worktree_sha256"],
    }
    return hashlib.sha256(_STREAM_DOMAIN + _canonical_json(material)).hexdigest()


def normalized_hook_journal_entry_sha256(value: Mapping[str, Any]) -> str:
    """Hash one journal envelope, excluding only its self-referential entry id."""

    material = copy.deepcopy(dict(value))
    material.pop("entry_id", None)
    return hashlib.sha256(_ENTRY_DOMAIN + _canonical_json(material)).hexdigest()


def validate_normalized_hook_journal_entry(value: Any) -> dict[str, Any]:
    """Validate one closed journal envelope and every nested content identity."""

    entry = _exact(copy.deepcopy(value), _ENTRY_FIELDS, "normalized hook journal entry")
    if entry["schema_version"] != "1" or entry["record_kind"] != "normalized_hook_journal_entry":
        raise NormalizedHookJournalError("unsupported journal entry schema or record kind")
    _sha256(entry["entry_id"], "entry_id")
    _sha256(entry["stream_id"], "stream_id")
    sequence = entry["sequence"]
    if type(sequence) is not int or not 0 <= sequence <= _MAX_SEQUENCE:
        raise NormalizedHookJournalError("sequence must be an in-bounds non-negative integer")
    previous = entry["previous_entry_id"]
    if sequence == 0:
        if previous is not None:
            raise NormalizedHookJournalError("the first journal entry cannot name a predecessor")
    elif previous is None:
        raise NormalizedHookJournalError("a non-first journal entry must name its predecessor")
    else:
        _sha256(previous, "previous_entry_id")

    kind = entry["payload_kind"]
    if kind == "event":
        if entry["result"] is not None:
            raise NormalizedHookJournalError("an event entry must have a null result")
        try:
            entry["event"] = validate_normalized_hook_event(entry["event"])
        except NormalizedHookError as exc:
            raise NormalizedHookJournalError(f"invalid nested event: {exc}") from exc
        if normalized_hook_stream_id(entry["event"]) != entry["stream_id"]:
            raise NormalizedHookJournalError("event identity does not match journal stream_id")
    elif kind == "result":
        if entry["event"] is not None:
            raise NormalizedHookJournalError("a result entry must have a null event")
        try:
            entry["result"] = validate_normalized_hook_result(entry["result"])
        except NormalizedHookError as exc:
            raise NormalizedHookJournalError(f"invalid nested result: {exc}") from exc
    else:
        raise NormalizedHookJournalError("payload_kind must be event or result")

    if normalized_hook_journal_entry_sha256(entry) != entry["entry_id"]:
        raise NormalizedHookJournalError("entry_id does not match the canonical journal entry")
    if len(_canonical_json(entry)) > _MAX_ENTRY_BYTES:
        raise NormalizedHookJournalError("normalized hook journal entry exceeds its byte bound")
    return entry


def _build_entry(
    record: Mapping[str, Any],
    *,
    stream_id: str,
    sequence: int,
    previous_entry_id: str | None,
) -> dict[str, Any]:
    kind = record.get("record_kind")
    if kind == "normalized_hook_event":
        try:
            payload = validate_normalized_hook_event(record)
        except NormalizedHookError as exc:
            raise NormalizedHookJournalError(f"invalid normalized hook event: {exc}") from exc
        payload_kind = "event"
        event, result = payload, None
    elif kind == "normalized_hook_result":
        try:
            payload = validate_normalized_hook_result(record)
        except NormalizedHookError as exc:
            raise NormalizedHookJournalError(f"invalid normalized hook result: {exc}") from exc
        payload_kind = "result"
        event, result = None, payload
    else:
        raise NormalizedHookJournalError("record must be a normalized hook event or result")
    entry: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "normalized_hook_journal_entry",
        "stream_id": stream_id,
        "sequence": sequence,
        "previous_entry_id": previous_entry_id,
        "payload_kind": payload_kind,
        "event": event,
        "result": result,
    }
    entry["entry_id"] = normalized_hook_journal_entry_sha256(entry)
    return validate_normalized_hook_journal_entry(entry)


def validate_normalized_hook_journal(values: Iterable[Any]) -> tuple[dict[str, Any], ...]:
    """Validate sequence, hash chain, stream identity, and event/result relationships."""

    entries = tuple(validate_normalized_hook_journal_entry(value) for value in values)
    stream_id: str | None = None
    previous: str | None = None
    events: dict[str, dict[str, Any]] = {}
    results: set[str] = set()
    completed_events: set[str] = set()
    for expected_sequence, entry in enumerate(entries):
        if entry["sequence"] != expected_sequence:
            raise NormalizedHookJournalError(
                f"non-monotonic journal sequence: expected {expected_sequence}, "
                f"got {entry['sequence']}"
            )
        if entry["previous_entry_id"] != previous:
            raise NormalizedHookJournalError("journal predecessor hash chain is broken")
        if stream_id is None:
            stream_id = entry["stream_id"]
        elif entry["stream_id"] != stream_id:
            raise NormalizedHookJournalError("journal contains multiple stream identities")
        if entry["payload_kind"] == "event":
            event = entry["event"]
            event_id = event["event_id"]
            if event_id in events:
                raise NormalizedHookJournalError(f"duplicate event_id: {event_id}")
            events[event_id] = event
        else:
            result = entry["result"]
            result_id = result["result_id"]
            event_id = result["event_id"]
            if event_id not in events:
                raise NormalizedHookJournalError("result precedes or references an absent event")
            if event_id in completed_events:
                raise NormalizedHookJournalError("an event has more than one aggregate result")
            if result_id in results:
                raise NormalizedHookJournalError(f"duplicate result_id: {result_id}")
            if result["completed_at"] < events[event_id]["occurred_at"]:
                raise NormalizedHookJournalError("result completed_at precedes its event")
            completed_events.add(event_id)
            results.add(result_id)
        previous = entry["entry_id"]
    return entries


def _assert_no_symlink_components(path: Path) -> None:
    current = path
    while True:
        if (current.exists() or current.is_symlink()) and current.is_symlink():
            raise NormalizedHookJournalError(f"refusing symlink path component: {current}")
        if current == current.parent:
            return
        current = current.parent


def _prepare_directory(
    directory: Path | str, *, project_state_root: Path | str | None = None
) -> Path:
    requested = Path(directory).expanduser()
    if not requested.is_absolute() or any(part in {".", ".."} for part in requested.parts):
        raise NormalizedHookJournalError(
            "journal destination must be an explicit absolute directory"
        )
    _assert_no_symlink_components(requested)
    if project_state_root is None:
        root = assert_writable(requested)
    else:
        project = Path(project_state_root).expanduser().resolve(strict=True)
        expected_parent = project / ".bearhug" / "normalized-hooks" / "v1"
        if requested.parent != expected_parent or _SHA256.fullmatch(requested.name) is None:
            raise NormalizedHookJournalError(
                "project journal must be one session digest below .bearhug/normalized-hooks/v1"
            )
        root = requested
    with suppress(FileExistsError):
        requested.mkdir(mode=0o700, parents=True)
    if requested.is_symlink() or not requested.is_dir():
        raise NormalizedHookJournalError("journal destination is not a regular directory")
    _assert_no_symlink_components(requested)
    return root


@contextmanager
def _journal_lock(directory: Path) -> Iterator[None]:
    lock_path = directory / ".journal.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise NormalizedHookJournalError(f"cannot open journal lock: {exc}") from exc
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise NormalizedHookJournalError("journal lock must be a singly linked regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _inventory(directory: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    entries: list[Path] = []
    temporaries: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name == ".journal.lock":
            if path.is_symlink() or not path.is_file():
                raise NormalizedHookJournalError("journal lock is not a regular file")
            continue
        if _TEMP_FILE.fullmatch(path.name):
            if path.is_symlink() or not path.is_file():
                raise NormalizedHookJournalError(
                    f"journal temporary is not a regular file: {path.name}"
                )
            temporaries.append(path)
            continue
        if _ENTRY_FILE.fullmatch(path.name) is None:
            raise NormalizedHookJournalError(f"unexpected journal artifact: {path.name}")
        if path.is_symlink() or not path.is_file():
            raise NormalizedHookJournalError(f"journal entry is not a regular file: {path.name}")
        entries.append(path)
    return tuple(entries), tuple(temporaries)


def _read_regular_file(path: Path, *, allow_publish_link: bool = False) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NormalizedHookJournalError(f"cannot open journal entry {path.name}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        expected_links = {1, 2} if allow_publish_link else {1}
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink not in expected_links
            or before.st_size > _MAX_ENTRY_BYTES + 1
        ):
            raise NormalizedHookJournalError("journal entry is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = _MAX_ENTRY_BYTES + 2
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_nlink, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise NormalizedHookJournalError("journal entry changed during stable read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_entry(path: Path, *, allow_publish_link: bool = False) -> dict[str, Any]:
    raw = _read_regular_file(path, allow_publish_link=allow_publish_link)
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise NormalizedHookJournalError("journal entry is not canonical one-line JSON")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except NormalizedHookJournalError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NormalizedHookJournalError(f"invalid journal entry {path.name}: {exc}") from exc
    entry = validate_normalized_hook_journal_entry(value)
    expected_name = f"{entry['sequence']:020d}-{entry['entry_id']}.json"
    if path.name != expected_name:
        raise NormalizedHookJournalError("journal entry filename does not match its content")
    if raw != _canonical_json(entry) + b"\n":
        raise NormalizedHookJournalError("journal entry bytes are not canonical JSON")
    return entry


def _publish_pair_inodes(
    entries: tuple[Path, ...], temporaries: tuple[Path, ...]
) -> set[tuple[int, int]]:
    entry_inodes = {(item.stat().st_dev, item.stat().st_ino) for item in entries}
    allowed: set[tuple[int, int]] = set()
    for temporary in temporaries:
        metadata = temporary.stat()
        inode = (metadata.st_dev, metadata.st_ino)
        if metadata.st_nlink == 1:
            continue
        if metadata.st_nlink == 2 and inode in entry_inodes:
            allowed.add(inode)
            continue
        raise NormalizedHookJournalError("journal temporary has an unsafe hard-link count")
    return allowed


def _read_committed(
    directory: Path, *, allow_publish_pairs: bool = False
) -> tuple[dict[str, Any], ...]:
    paths, temporaries = _inventory(directory)
    allowed = _publish_pair_inodes(paths, temporaries) if allow_publish_pairs else set()
    entries = []
    for path in paths:
        metadata = path.stat()
        inode = (metadata.st_dev, metadata.st_ino)
        entries.append(_read_entry(path, allow_publish_link=inode in allowed))
    return validate_normalized_hook_journal(entries)


@contextmanager
def _journal_read_lock(directory: Path) -> Iterator[None]:
    lock_path = directory / ".journal.lock"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags)
    except FileNotFoundError:
        entries, temporaries = _inventory(directory)
        if entries or temporaries:
            raise NormalizedHookJournalError("non-empty journal is missing its lock") from None
        yield
        return
    except OSError as exc:
        raise NormalizedHookJournalError(f"cannot open journal lock: {exc}") from exc
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise NormalizedHookJournalError("journal lock must be a singly linked regular file")
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def read_normalized_hook_journal(directory: Path | str) -> tuple[dict[str, Any], ...]:
    """Read committed entries; an abandoned regular temporary is non-authoritative."""

    root = Path(directory).expanduser()
    _assert_no_symlink_components(root)
    if not root.is_dir():
        raise NormalizedHookJournalError(f"journal directory does not exist: {root}")
    with _journal_read_lock(root):
        return _read_committed(root)


def _publish(path: Path, entry: Mapping[str, Any]) -> None:
    payload = _canonical_json(entry) + b"\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise NormalizedHookJournalError(
                f"refusing to overwrite journal entry: {path.name}"
            ) from exc
        temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
        temporary.unlink(missing_ok=True)


def _remove_temporaries(directory: Path) -> int:
    _, temporaries = _inventory(directory)
    removed = 0
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for temporary in temporaries:
            os.unlink(temporary.name, dir_fd=directory_fd)
            removed += 1
        if removed:
            os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return removed


def _recover_locked(directory: Path) -> tuple[tuple[dict[str, Any], ...], int]:
    # A process can die after linking the durable final name but before unlinking its temporary.
    # That exact two-link pair is accepted only long enough to validate the committed authority,
    # remove the non-authoritative name, and revalidate the singly linked final file.
    before = _read_committed(directory, allow_publish_pairs=True)
    removed = _remove_temporaries(directory)
    after = _read_committed(directory)
    if before != after:
        raise NormalizedHookJournalError("journal authority changed during crash recovery")
    return after, removed


def recover_normalized_hook_journal(directory: Path | str) -> int:
    """Validate committed authority then remove abandoned, non-authoritative temp files."""

    root = _prepare_directory(directory)
    with _journal_lock(root):
        _, removed = _recover_locked(root)
        return removed


def append_normalized_hook_record(
    record: Mapping[str, Any],
    directory: Path | str,
    *,
    project_state_root: Path | str | None = None,
) -> Path:
    """Atomically append one normalized event or result under the stream lock."""

    if not isinstance(record, Mapping):
        raise NormalizedHookJournalError("record must be a normalized hook event or result")
    kind = record.get("record_kind")
    if kind == "normalized_hook_event":
        try:
            checked = validate_normalized_hook_event(record)
        except NormalizedHookError as exc:
            raise NormalizedHookJournalError(f"invalid normalized hook event: {exc}") from exc
        requested_stream = normalized_hook_stream_id(checked)
    elif kind == "normalized_hook_result":
        try:
            checked = validate_normalized_hook_result(record)
        except NormalizedHookError as exc:
            raise NormalizedHookJournalError(f"invalid normalized hook result: {exc}") from exc
        requested_stream = None
    else:
        raise NormalizedHookJournalError("record must be a normalized hook event or result")

    root = _prepare_directory(directory, project_state_root=project_state_root)
    with _journal_lock(root):
        existing, _ = _recover_locked(root)
        if existing:
            stream_id = existing[0]["stream_id"]
            if requested_stream is not None and requested_stream != stream_id:
                raise NormalizedHookJournalError("event belongs to a different journal stream")
        else:
            if requested_stream is None:
                raise NormalizedHookJournalError("the first journal record must be an event")
            stream_id = requested_stream
        entry = _build_entry(
            checked,
            stream_id=stream_id,
            sequence=len(existing),
            previous_entry_id=existing[-1]["entry_id"] if existing else None,
        )
        validate_normalized_hook_journal((*existing, entry))
        target = root / f"{entry['sequence']:020d}-{entry['entry_id']}.json"
        _publish(target, entry)
    return target


__all__ = [
    "NormalizedHookJournalError",
    "append_normalized_hook_record",
    "normalized_hook_journal_entry_sha256",
    "normalized_hook_stream_id",
    "read_normalized_hook_journal",
    "recover_normalized_hook_journal",
    "validate_normalized_hook_journal",
    "validate_normalized_hook_journal_entry",
]
