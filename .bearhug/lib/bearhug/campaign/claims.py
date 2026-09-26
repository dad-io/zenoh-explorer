"""Transactional, append-only claims over campaign worktrees and shared resources.

Claims live under a caller-supplied Bear Hug campaign directory.  An active claim and its release
are separate immutable records: losing a process never releases authority, and a second process
cannot overwrite either side.  ``flock`` serializes the read-conflict-publish transaction; the
records, rather than the lock file, remain the durable authority.

This module validates caller-supplied Git identities.  It deliberately does not run Git or infer a
base, branch, worktree, or repository identity.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.paths import assert_writable


class CampaignClaimError(RuntimeError):
    """A claim, release, store, or lock cannot be trusted."""


class CampaignClaimConflict(CampaignClaimError):
    """An active claim already owns the requested worktree, branch, path, or resource."""


_ACTIVE_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "claim_id",
        "campaign_id",
        "claimant_id",
        "role",
        "created_at",
        "repository_common_dir_sha256",
        "worktree_sha256",
        "branch",
        "base_oid",
        "path_prefixes",
        "semantic_resources",
    }
)
_RELEASE_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "claim_id",
        "campaign_id",
        "claimant_id",
        "released_at",
        "reason",
    }
)
_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TEMP = re.compile(r"^\.[0-9a-f]{64}\.json\.tmp-\d+-[0-9a-f]{16}$")
_RELEASE_REASONS = frozenset({"completed", "failed", "abandoned"})
_CLAIM_ID_DOMAIN = b"campaign-worktree-claim-id/1\0"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _timestamp(value: str, field: str) -> None:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise CampaignClaimError(f"{field} must be UTC at whole-second precision")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise CampaignClaimError(f"{field} is not a valid timestamp") from exc


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token(value: Any, field: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise CampaignClaimError(
            f"{field} must start lowercase and contain only lowercase letters, digits, ._:/-"
        )
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CampaignClaimError(f"{field} must be lowercase SHA-256")
    return value


def _branch(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value != value.strip()
        or value.startswith("refs/heads/")
        or value.casefold() == "head"
        or value.startswith("/")
        or value.endswith(("/", "."))
        or "//" in value
        or ".." in value
        or "@{" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char in " ~^:?*[\\" for char in value)
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    ):
        raise CampaignClaimError("branch must be one canonical short Git branch name")
    return value


def _path_prefix(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CampaignClaimError(f"invalid path prefix: {value!r}")
    if value == ".":
        return value
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/") or value.endswith("/"):
        raise CampaignClaimError(f"invalid path prefix: {value!r}")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise CampaignClaimError(f"invalid path prefix: {value!r}")
    if path.as_posix() != value:
        raise CampaignClaimError(f"noncanonical path prefix: {value!r}")
    return value


def _paths_overlap(left: str, right: str) -> bool:
    return (
        left == "."
        or right == "."
        or left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


def _canonical_paths(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise CampaignClaimError("path_prefixes must be an array")
    raw = list(values)
    paths = [_path_prefix(value) for value in raw]
    if len(paths) != len(set(paths)):
        raise CampaignClaimError("duplicate path prefixes are not allowed")
    paths.sort()
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if _paths_overlap(left, right):
                raise CampaignClaimError(
                    f"overlapping path prefixes in one claim: {left!r} and {right!r}"
                )
    return paths


def _canonical_resources(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise CampaignClaimError("semantic_resources must be an array")
    raw = list(values)
    resources = [_token(value, "semantic resource") for value in raw]
    if len(resources) != len(set(resources)):
        raise CampaignClaimError("duplicate semantic resources are not allowed")
    return sorted(resources)


def _identity_material(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in (
            "campaign_id",
            "claimant_id",
            "role",
            "created_at",
            "repository_common_dir_sha256",
            "worktree_sha256",
            "branch",
            "base_oid",
            "path_prefixes",
            "semantic_resources",
        )
    }


def _claim_id(value: dict[str, Any]) -> str:
    return hashlib.sha256(_CLAIM_ID_DOMAIN + _canonical_json(_identity_material(value))).hexdigest()


def build_claim(
    *,
    campaign_id: str,
    claimant_id: str,
    role: str,
    repository_common_dir_sha256: str,
    worktree_sha256: str,
    branch: str,
    base_oid: str,
    path_prefixes: Iterable[str],
    semantic_resources: Iterable[str],
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build one canonical active claim; no filesystem state is read or written."""

    value: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "campaign_worktree_claim",
        "campaign_id": _token(campaign_id, "campaign_id"),
        "claimant_id": _token(claimant_id, "claimant_id"),
        "role": _token(role, "role"),
        "created_at": created_at or _now(),
        "repository_common_dir_sha256": _digest(
            repository_common_dir_sha256, "repository_common_dir_sha256"
        ),
        "worktree_sha256": _digest(worktree_sha256, "worktree_sha256"),
        "branch": _branch(branch),
        "base_oid": base_oid,
        "path_prefixes": _canonical_paths(path_prefixes),
        "semantic_resources": _canonical_resources(semantic_resources),
    }
    _timestamp(value["created_at"], "created_at")
    if not isinstance(base_oid, str) or _GIT_OID.fullmatch(base_oid) is None:
        raise CampaignClaimError("base_oid must be a full lowercase Git object id")
    if not value["path_prefixes"] and not value["semantic_resources"]:
        raise CampaignClaimError("a claim must own at least one path prefix or semantic resource")
    value["claim_id"] = _claim_id(value)
    return value


def _validate_active(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _ACTIVE_FIELDS:
        raise CampaignClaimError("active claim has missing or unknown fields")
    if value["schema_version"] != "1" or value["record_kind"] != "campaign_worktree_claim":
        raise CampaignClaimError("unsupported active claim schema or record kind")
    if not isinstance(value["path_prefixes"], list):
        raise CampaignClaimError("path_prefixes must be an array")
    if not isinstance(value["semantic_resources"], list):
        raise CampaignClaimError("semantic_resources must be an array")
    expected = build_claim(
        campaign_id=value["campaign_id"],
        claimant_id=value["claimant_id"],
        role=value["role"],
        repository_common_dir_sha256=value["repository_common_dir_sha256"],
        worktree_sha256=value["worktree_sha256"],
        branch=value["branch"],
        base_oid=value["base_oid"],
        path_prefixes=value["path_prefixes"],
        semantic_resources=value["semantic_resources"],
        created_at=value["created_at"],
    )
    if value["path_prefixes"] != expected["path_prefixes"]:
        raise CampaignClaimError("path_prefixes must be sorted in canonical order")
    if value["semantic_resources"] != expected["semantic_resources"]:
        raise CampaignClaimError("semantic_resources must be sorted in canonical order")
    if value["claim_id"] != expected["claim_id"]:
        raise CampaignClaimError("claim_id does not match the deterministic claim identity")
    return value


def _validate_release(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _RELEASE_FIELDS:
        raise CampaignClaimError("claim release has missing or unknown fields")
    if (
        value["schema_version"] != "1"
        or value["record_kind"] != "campaign_worktree_claim_release"
    ):
        raise CampaignClaimError("unsupported claim release schema or record kind")
    _digest(value["claim_id"], "claim_id")
    _token(value["campaign_id"], "campaign_id")
    _token(value["claimant_id"], "claimant_id")
    _timestamp(value["released_at"], "released_at")
    if not isinstance(value["reason"], str) or value["reason"] not in _RELEASE_REASONS:
        raise CampaignClaimError(f"unsupported claim release reason: {value['reason']!r}")
    return value


def validate_claim_record(value: Any) -> dict[str, Any]:
    """Validate one closed active-claim or release-tombstone record without coercion."""

    if not isinstance(value, dict):
        raise CampaignClaimError("claim record must be a JSON object")
    if value.get("record_kind") == "campaign_worktree_claim":
        return _validate_active(value)
    if value.get("record_kind") == "campaign_worktree_claim_release":
        return _validate_release(value)
    raise CampaignClaimError("unsupported claim record kind")


def _store(campaign_root: Path | str) -> tuple[Path, Path, Path]:
    root = assert_writable(Path(campaign_root))
    root.mkdir(parents=True, exist_ok=True)
    claims = root / "claims"
    active = claims / "active"
    releases = claims / "releases"
    for directory in (claims, active, releases):
        if directory.is_symlink():
            raise CampaignClaimError(f"claim store directory must not be a symlink: {directory}")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.is_dir():
            raise CampaignClaimError(f"claim store path is not a directory: {directory}")
    return root, active, releases


@contextmanager
def _store_lock(root: Path, timeout_s: float) -> Iterator[None]:
    if timeout_s <= 0:
        raise CampaignClaimError("lock timeout must be greater than zero")
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root / ".claims.lock", flags, 0o600)
    except OSError as exc:
        raise CampaignClaimError(f"cannot open campaign claim lock: {exc}") from exc
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise CampaignClaimError("timed out waiting for campaign claim lock") from exc
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _publish(path: Path, value: dict[str, Any]) -> None:
    """Publish complete bytes with create-only link semantics; never replace an authority."""

    payload = _canonical_json(value) + b"\n"
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise CampaignClaimError(f"claim authority already exists: {path.name}") from exc
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


def _read_record(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CampaignClaimError(f"claim artifact is not a regular file: {path}")
    try:
        if path.stat().st_size > 65536:
            raise CampaignClaimError(f"claim artifact exceeds 64 KiB: {path.name}")
        value = json.loads(path.read_bytes(), object_pairs_hook=_closed_object)
        record = validate_claim_record(value)
    except (OSError, UnicodeDecodeError, ValueError, CampaignClaimError) as exc:
        raise CampaignClaimError(f"invalid claim artifact {path.name}: {exc}") from exc
    if path.name != f"{record['claim_id']}.json":
        raise CampaignClaimError(f"claim artifact filename does not match its id: {path.name}")
    return record


def _record_paths(directory: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if _TEMP.fullmatch(path.name):
            continue
        if not path.name.endswith(".json"):
            raise CampaignClaimError(f"unexpected file in claim store: {path.name}")
        paths.append(path)
    return paths


def _load_locked(active_dir: Path, release_dir: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    active = {record["claim_id"]: record for path in _record_paths(active_dir)
              for record in (_read_record(path),)}
    releases = {record["claim_id"]: record for path in _record_paths(release_dir)
                for record in (_read_record(path),)}
    for claim_id, release in releases.items():
        claim = active.get(claim_id)
        if claim is None:
            raise CampaignClaimError(f"release tombstone has no active claim: {claim_id}")
        if (
            release["campaign_id"] != claim["campaign_id"]
            or release["claimant_id"] != claim["claimant_id"]
        ):
            raise CampaignClaimError(f"release tombstone identity disagrees with claim: {claim_id}")
        if release["released_at"] < claim["created_at"]:
            raise CampaignClaimError(f"release tombstone predates its claim: {claim_id}")
    return active, releases


def _collision(candidate: dict[str, Any], existing: dict[str, Any]) -> str | None:
    if candidate["repository_common_dir_sha256"] != existing["repository_common_dir_sha256"]:
        return None
    if candidate["worktree_sha256"] == existing["worktree_sha256"]:
        return "worktree"
    if candidate["branch"] == existing["branch"]:
        return "branch"
    if candidate["base_oid"] != existing["base_oid"]:
        return "base"
    for left in candidate["path_prefixes"]:
        for right in existing["path_prefixes"]:
            if _paths_overlap(left, right):
                return f"path prefix {left!r} overlaps {right!r}"
    shared = sorted(set(candidate["semantic_resources"]) & set(existing["semantic_resources"]))
    if shared:
        return f"resource {shared[0]!r}"
    return None


def acquire_claim(
    campaign_root: Path | str,
    *,
    lock_timeout_s: float = 5.0,
    **claim_fields: Any,
) -> dict[str, Any]:
    """Atomically refuse conflicts or publish one complete active claim."""

    candidate = build_claim(**claim_fields)
    root, active_dir, release_dir = _store(campaign_root)
    with _store_lock(root, lock_timeout_s):
        active, releases = _load_locked(active_dir, release_dir)
        campaigns = {record["campaign_id"] for record in active.values()}
        if campaigns and campaigns != {candidate["campaign_id"]}:
            raise CampaignClaimError(
                f"campaign root already belongs to {', '.join(sorted(campaigns))}"
            )
        for claim_id, existing in sorted(active.items()):
            if claim_id in releases:
                continue
            axis = _collision(candidate, existing)
            if axis is not None:
                raise CampaignClaimConflict(
                    f"{axis} is already owned by active claim {claim_id}"
                )
        path = active_dir / f"{candidate['claim_id']}.json"
        if path.exists():
            raise CampaignClaimConflict(
                f"deterministic claim record {candidate['claim_id']} already exists"
            )
        _publish(path, candidate)
    return candidate


def release_claim(
    campaign_root: Path | str,
    claim_id: str,
    *,
    claimant_id: str,
    reason: str,
    released_at: str | None = None,
    lock_timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Publish an explicit release tombstone without changing or deleting the active record."""

    _digest(claim_id, "claim_id")
    _token(claimant_id, "claimant_id")
    root, active_dir, release_dir = _store(campaign_root)
    with _store_lock(root, lock_timeout_s):
        active, releases = _load_locked(active_dir, release_dir)
        claim = active.get(claim_id)
        if claim is None:
            raise CampaignClaimError(f"no active claim exists for {claim_id}")
        if claim_id in releases:
            raise CampaignClaimError(f"claim is already released: {claim_id}")
        if claim["claimant_id"] != claimant_id:
            raise CampaignClaimError(
                f"claimant does not own claim {claim_id}: expected {claim['claimant_id']!r}"
            )
        release = {
            "schema_version": "1",
            "record_kind": "campaign_worktree_claim_release",
            "claim_id": claim_id,
            "campaign_id": claim["campaign_id"],
            "claimant_id": claimant_id,
            "released_at": released_at or _now(),
            "reason": reason,
        }
        _validate_release(release)
        if release["released_at"] < claim["created_at"]:
            raise CampaignClaimError(f"release tombstone predates its claim: {claim_id}")
        _publish(release_dir / f"{claim_id}.json", release)
    return release


def active_claims(
    campaign_root: Path | str, *, lock_timeout_s: float = 5.0
) -> tuple[dict[str, Any], ...]:
    """Return the validated active set; malformed or orphaned records fail the whole read."""

    root, active_dir, release_dir = _store(campaign_root)
    with _store_lock(root, lock_timeout_s):
        active, releases = _load_locked(active_dir, release_dir)
        return tuple(active[claim_id] for claim_id in sorted(set(active) - set(releases)))


__all__ = [
    "CampaignClaimConflict",
    "CampaignClaimError",
    "acquire_claim",
    "active_claims",
    "build_claim",
    "release_claim",
    "validate_claim_record",
]
