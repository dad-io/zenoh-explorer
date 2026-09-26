"""Provider-neutral, privacy-bounded lifecycle event journal.

The journal is native evidence for shared evaluators.  Provider transcripts may corroborate an
event only when their existing normalized representation retains every required fact.  The
current Claude and Codex App Server normalizers intentionally discard per-event timestamps and
semantic payloads, so their adapters expose a typed gap instead of manufacturing journal rows.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.paths import assert_writable
from bearhug.providers.claude import ClaudeNormalization
from bearhug.providers.codex_app_server import CodexAppServerNormalization


class ProviderEventJournalError(ValueError):
    """A journal row or append-only store is malformed or ambiguous."""


@dataclass(frozen=True, slots=True)
class ProviderEventAdapterGap(RuntimeError):
    """A normalized provider surface lacks facts required by the canonical journal."""

    provider: str
    adapter: str
    adapter_version: str
    missing_facts: tuple[str, ...]

    def __str__(self) -> str:
        return (
            f"{self.provider}/{self.adapter}@{self.adapter_version} cannot be adapted "
            f"losslessly: {', '.join(self.missing_facts)}"
        )


_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "event_id",
        "sequence",
        "occurred_at",
        "event_type",
        "action",
        "source",
        "identity",
        "payload",
        "authority",
    }
)
_SOURCE_FIELDS = frozenset({"provider", "adapter", "adapter_version", "surface"})
_IDENTITY_FIELDS = frozenset({"session_id", "thread_id", "turn_id", "observation"})
_PAYLOAD_FIELDS = frozenset({"sha256", "byte_count", "media_type", "retention"})
_AUTHORITY_FIELDS = frozenset({"classification", "basis"})
_ACTIONS = {
    "prompt": frozenset({"submitted"}),
    "tool": frozenset({"requested", "completed"}),
    "permission": frozenset({"requested", "resolved"}),
    "stop": frozenset({"requested", "evaluated"}),
    "subagent": frozenset({"started", "completed"}),
    "compaction": frozenset({"started", "completed"}),
}
_SURFACE_BASIS = {
    "shared_evaluator": "shared_evaluator_verdict",
    "provider_runtime": "provider_runtime_observation",
    "provider_transcript": "provider_transcript_observation",
}
_CLASSIFICATIONS = frozenset({"authoritative", "corroborating", "diagnostic"})
_OBSERVATIONS = frozenset({"provider_observed", "runtime_observed"})
_TOKEN = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z")
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}\Z")
_EVENT_FILE = re.compile(r"(?P<sequence>[0-9]{20})-(?P<event>[0-9a-f]{64})\.json\Z")
_TEMP_FILE = re.compile(r"\..+\.tmp-[0-9]+-[0-9a-f]{16}\Z")
_HASH_DOMAIN = b"bear-hug/provider-event-journal/v1\0"
_MAX_RECORD_BYTES = 32 * 1024


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
        raise ProviderEventJournalError(f"value is not canonical JSON: {exc}") from exc


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderEventJournalError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProviderEventJournalError(f"{name} has missing or unknown fields")
    return value


def _sha256(value: Any, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ProviderEventJournalError(f"{name} must be lowercase SHA-256")


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderEventJournalError("occurred_at must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ProviderEventJournalError("occurred_at must be a canonical zoned timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ProviderEventJournalError("occurred_at must be a canonical zoned timestamp") from exc
    if _canonical_timestamp(parsed) != value:
        raise ProviderEventJournalError("occurred_at must be a canonical zoned timestamp")
    return parsed


def _identity_tuple(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (value["session_id"], value["thread_id"], value["turn_id"])


def event_sha256(value: Mapping[str, Any]) -> str:
    """Hash one event's closed identity, excluding only the self-referential event id."""

    material = dict(value)
    material.pop("event_id", None)
    return hashlib.sha256(_HASH_DOMAIN + _canonical_json(material)).hexdigest()


def build_provider_event(
    *,
    sequence: int,
    occurred_at: datetime,
    event_type: str,
    action: str,
    provider: str,
    adapter: str,
    adapter_version: str,
    surface: str,
    session_id: str,
    thread_id: str | None,
    turn_id: str,
    identity_observation: str,
    payload: bytes,
    payload_media_type: str,
    authority_classification: str,
) -> dict[str, Any]:
    """Build a closed event while retaining no prompt, tool input, or result content."""

    if not isinstance(payload, bytes):
        raise ProviderEventJournalError("payload must be exact bytes")
    value: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "provider_event",
        "sequence": sequence,
        "occurred_at": _canonical_timestamp(occurred_at),
        "event_type": event_type,
        "action": action,
        "source": {
            "provider": provider,
            "adapter": adapter,
            "adapter_version": adapter_version,
            "surface": surface,
        },
        "identity": {
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "observation": identity_observation,
        },
        "payload": {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_count": len(payload),
            "media_type": payload_media_type,
            "retention": "digest_only",
        },
        "authority": {
            "classification": authority_classification,
            "basis": _SURFACE_BASIS.get(surface, ""),
        },
    }
    value["event_id"] = event_sha256(value)
    return validate_provider_event(value)


def validate_provider_event(value: Any) -> dict[str, Any]:
    """Validate one closed v1 event and recompute its deterministic identity."""

    event = _exact(value, _EVENT_FIELDS, "provider event")
    if event["schema_version"] != "1" or event["record_kind"] != "provider_event":
        raise ProviderEventJournalError("unsupported provider event schema or record kind")
    _sha256(event["event_id"], "event_id")
    if type(event["sequence"]) is not int or event["sequence"] < 0:
        raise ProviderEventJournalError("sequence must be a non-negative integer")
    _parse_timestamp(event["occurred_at"])
    event_type = event["event_type"]
    actions = _ACTIONS.get(event_type) if isinstance(event_type, str) else None
    if actions is None or event["action"] not in actions:
        raise ProviderEventJournalError("unknown event type or action")

    source = _exact(event["source"], _SOURCE_FIELDS, "source")
    for field in ("provider", "adapter"):
        if not isinstance(source[field], str) or _TOKEN.fullmatch(source[field]) is None:
            raise ProviderEventJournalError(f"source.{field} is not a canonical token")
    if (
        not isinstance(source["adapter_version"], str)
        or not source["adapter_version"]
        or len(source["adapter_version"]) > 128
    ):
        raise ProviderEventJournalError("source.adapter_version must be a bounded string")
    surface = source["surface"]
    expected_basis = _SURFACE_BASIS.get(surface) if isinstance(surface, str) else None
    if expected_basis is None:
        raise ProviderEventJournalError("source.surface is unknown")

    identity = _exact(event["identity"], _IDENTITY_FIELDS, "identity")
    for field in ("session_id", "turn_id"):
        if (
            not isinstance(identity[field], str)
            or not identity[field]
            or len(identity[field]) > 256
        ):
            raise ProviderEventJournalError(f"identity.{field} must be a bounded string")
    if identity["thread_id"] is not None and (
        not isinstance(identity["thread_id"], str)
        or not identity["thread_id"]
        or len(identity["thread_id"]) > 256
    ):
        raise ProviderEventJournalError("identity.thread_id must be null or a bounded string")
    observation = identity["observation"]
    if not isinstance(observation, str) or observation not in _OBSERVATIONS:
        raise ProviderEventJournalError("identity.observation is unknown")
    if (surface == "shared_evaluator") != (observation == "runtime_observed"):
        raise ProviderEventJournalError("identity observation does not match the source surface")

    payload = _exact(event["payload"], _PAYLOAD_FIELDS, "payload")
    _sha256(payload["sha256"], "payload.sha256")
    if type(payload["byte_count"]) is not int or payload["byte_count"] < 0:
        raise ProviderEventJournalError("payload.byte_count must be a non-negative integer")
    if not isinstance(payload["media_type"], str) or _MEDIA_TYPE.fullmatch(
        payload["media_type"]
    ) is None:
        raise ProviderEventJournalError("payload.media_type is not canonical")
    if payload["retention"] != "digest_only":
        raise ProviderEventJournalError("journal payloads must be digest-only")

    authority = _exact(event["authority"], _AUTHORITY_FIELDS, "authority")
    classification = authority["classification"]
    if not isinstance(classification, str) or classification not in _CLASSIFICATIONS:
        raise ProviderEventJournalError("authority.classification is unknown")
    if authority["basis"] != expected_basis:
        raise ProviderEventJournalError("authority basis does not match the source surface")
    if (
        event_type == "stop"
        and surface == "provider_transcript"
        and classification == "authoritative"
    ):
        raise ProviderEventJournalError(
            "a provider transcript cannot be authoritative Stop evidence"
        )
    if event_sha256(event) != event["event_id"]:
        raise ProviderEventJournalError("event_id does not match the canonical event")
    return event


def validate_event_journal(values: Iterable[Any]) -> tuple[dict[str, Any], ...]:
    """Validate one turn journal, including order, time and duplicate invariants."""

    events = tuple(validate_provider_event(value) for value in values)
    seen: set[str] = set()
    identity: tuple[Any, ...] | None = None
    previous_time: datetime | None = None
    for expected_sequence, event in enumerate(events):
        if event["sequence"] != expected_sequence:
            raise ProviderEventJournalError(
                f"out-of-order sequence: expected {expected_sequence}, got {event['sequence']}"
            )
        if event["event_id"] in seen:
            raise ProviderEventJournalError(f"duplicate event id: {event['event_id']}")
        seen.add(event["event_id"])
        current_identity = _identity_tuple(event["identity"])
        if identity is None:
            identity = current_identity
        elif current_identity != identity:
            raise ProviderEventJournalError("journal contains multiple session/turn identities")
        current_time = _parse_timestamp(event["occurred_at"])
        if previous_time is not None and current_time < previous_time:
            raise ProviderEventJournalError("journal timestamps move backwards")
        previous_time = current_time
    return events


def events_from_claude_normalization(
    normalization: ClaudeNormalization,
) -> tuple[dict[str, Any], ...]:
    """Refuse the current lossy Claude compatibility surface explicitly."""

    if not isinstance(normalization, ClaudeNormalization):
        raise TypeError("normalization must be ClaudeNormalization")
    raise ProviderEventAdapterGap(
        normalization.provider,
        normalization.adapter,
        normalization.adapter_version,
        (
            "per_event_zoned_timestamp",
            "lossless_semantic_event_payload",
            "authoritative_shared_stop_verdict",
        ),
    )


def events_from_app_server_normalization(
    normalization: CodexAppServerNormalization,
) -> tuple[dict[str, Any], ...]:
    """Refuse the current lossy App Server compatibility surface explicitly."""

    if not isinstance(normalization, CodexAppServerNormalization):
        raise TypeError("normalization must be CodexAppServerNormalization")
    raise ProviderEventAdapterGap(
        normalization.provider,
        normalization.adapter,
        normalization.adapter_version,
        (
            "per_event_zoned_timestamp",
            "lossless_semantic_event_payload",
            "client_prompt_event",
            "authoritative_shared_stop_verdict",
        ),
    )


def _assert_no_symlink_components(path: Path) -> None:
    current = path
    while True:
        if (current.exists() or current.is_symlink()) and current.is_symlink():
            raise ProviderEventJournalError(f"refusing symlink path component: {current}")
        if current == current.parent:
            return
        current = current.parent


@contextmanager
def _append_lock(directory: Path) -> Iterator[None]:
    lock_path = directory / ".journal.lock"
    if lock_path.is_symlink():
        raise ProviderEventJournalError("journal lock must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ProviderEventJournalError(f"cannot open journal lock: {exc}") from exc
    try:
        if not Path(lock_path).is_file() or Path(lock_path).is_symlink():
            raise ProviderEventJournalError("journal lock is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _event_paths(directory: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda candidate: candidate.name):
        if path.name == ".journal.lock" or _TEMP_FILE.fullmatch(path.name):
            continue
        if _EVENT_FILE.fullmatch(path.name) is None:
            raise ProviderEventJournalError(f"unexpected journal artifact: {path.name}")
        if path.is_symlink() or not path.is_file():
            raise ProviderEventJournalError(f"journal artifact is not a regular file: {path.name}")
        paths.append(path)
    return tuple(paths)


def _read_event(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_RECORD_BYTES:
            raise ProviderEventJournalError(f"journal artifact exceeds {_MAX_RECORD_BYTES} bytes")
        raw = path.read_bytes()
        if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise ProviderEventJournalError("journal artifact is not canonical one-line JSON")
        value = json.loads(raw, object_pairs_hook=_closed_object)
        event = validate_provider_event(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderEventJournalError(f"invalid journal artifact {path.name}: {exc}") from exc
    expected_name = f"{event['sequence']:020d}-{event['event_id']}.json"
    if path.name != expected_name:
        raise ProviderEventJournalError("journal artifact filename does not match its event")
    if raw != _canonical_json(event) + b"\n":
        raise ProviderEventJournalError("journal artifact bytes are not canonical JSON")
    return event


def read_event_journal(directory: Path | str) -> tuple[dict[str, Any], ...]:
    """Read and revalidate one append-only journal snapshot."""

    root = Path(directory).expanduser()
    _assert_no_symlink_components(root)
    if not root.is_dir():
        raise ProviderEventJournalError(f"journal directory does not exist: {root}")
    return validate_event_journal(_read_event(path) for path in _event_paths(root))


def _publish_event(path: Path, event: dict[str, Any]) -> None:
    payload = _canonical_json(event) + b"\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ProviderEventJournalError(
                f"refusing to overwrite journal event: {path.name}"
            ) from exc
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


def append_provider_event(value: dict[str, Any], directory: Path | str) -> Path:
    """Atomically append one event; existing authority bytes are never replaced."""

    event = validate_provider_event(value)
    requested = Path(directory).expanduser()
    _assert_no_symlink_components(requested)
    root = assert_writable(requested)
    if requested.exists():
        if requested.is_symlink() or not requested.is_dir():
            raise ProviderEventJournalError("journal destination is not a regular directory")
    else:
        if not requested.parent.is_dir():
            raise ProviderEventJournalError("journal parent directory does not exist")
        requested.mkdir(mode=0o700)
    _assert_no_symlink_components(requested)
    with _append_lock(root):
        existing = validate_event_journal(_read_event(path) for path in _event_paths(root))
        if event["sequence"] != len(existing):
            raise ProviderEventJournalError(
                f"out-of-order append: expected sequence {len(existing)}, got {event['sequence']}"
            )
        if existing and _identity_tuple(event["identity"]) != _identity_tuple(
            existing[0]["identity"]
        ):
            raise ProviderEventJournalError("append changes the journal session/turn identity")
        if existing and _parse_timestamp(event["occurred_at"]) < _parse_timestamp(
            existing[-1]["occurred_at"]
        ):
            raise ProviderEventJournalError("append timestamp moves backwards")
        name = f"{event['sequence']:020d}-{event['event_id']}.json"
        target = root / name
        _publish_event(target, event)
    return target


__all__ = [
    "ProviderEventAdapterGap",
    "ProviderEventJournalError",
    "append_provider_event",
    "build_provider_event",
    "event_sha256",
    "events_from_app_server_normalization",
    "events_from_claude_normalization",
    "read_event_journal",
    "validate_event_journal",
    "validate_provider_event",
]
