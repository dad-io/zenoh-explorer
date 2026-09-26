"""Read-only Git worktree inventory with explicit Bear Hug custody.

The inventory is intentionally narrower than a launcher.  It reads the exact subject and Git
common directory supplied by its caller, parses Git's NUL-delimited porcelain without involving a
shell, and classifies existing worktrees against an explicit registry.  It never creates, adopts,
repairs, prunes, unlocks, or removes a worktree.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_MAX_PORCELAIN_BYTES = 16 * 1024 * 1024
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BRANCH_PREFIX = "refs/heads/"

WorktreeKind = Literal["main", "linked", "bare"]
WorktreeCustody = Literal["foreign", "unregistered", "bearhug-owned"]


class WorktreeInventoryError(ValueError):
    """The requested inventory cannot be trusted without changing repository state."""


@dataclass(frozen=True, slots=True)
class WorktreeRegistration:
    """Caller-owned evidence that Bear Hug registered one exact ``bh-*`` worktree."""

    repository_common_dir_sha256: str
    worktree_sha256: str
    branch: str


@dataclass(frozen=True, slots=True)
class WorktreeEntry:
    """One canonical record from ``git worktree list --porcelain -z``."""

    path: Path
    worktree_sha256: str
    kind: WorktreeKind
    head_oid: str | None
    branch: str | None
    detached: bool
    locked: bool
    locked_reason: str | None
    prunable: bool
    prunable_reason: str | None
    custody: WorktreeCustody


@dataclass(frozen=True, slots=True)
class WorktreeInventory:
    """An observation tied to one exact repository and its canonical common directory."""

    subject: Path
    subject_worktree_sha256: str
    common_dir: Path
    repository_common_dir_sha256: str
    entries: tuple[WorktreeEntry, ...]


@dataclass(frozen=True, slots=True)
class _ParsedWorktree:
    path: Path
    kind: WorktreeKind
    head_oid: str | None
    branch: str | None
    detached: bool
    locked: bool
    locked_reason: str | None
    prunable: bool
    prunable_reason: str | None


def _path_sha256(path: Path) -> str:
    return hashlib.sha256(os.fsencode(path)).hexdigest()


def _valid_branch(value: str) -> bool:
    return not (
        not value
        or len(value) > 255
        or value != value.strip()
        or value.casefold() == "head"
        or value.startswith("/")
        or value.endswith(("/", "."))
        or "//" in value
        or ".." in value
        or "@{" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in " ~^:?*[\\" for character in value)
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    )


def _canonical_existing_directory(value: Path | str, label: str) -> Path:
    requested = Path(value).expanduser()
    try:
        if requested.is_symlink():
            raise WorktreeInventoryError(f"{label} may not be a symlink: {requested}")
        canonical = requested.resolve(strict=True)
    except OSError as exc:
        raise WorktreeInventoryError(f"cannot resolve {label}: {requested}") from exc
    if not canonical.is_dir():
        raise WorktreeInventoryError(f"{label} is not a directory: {canonical}")
    return canonical


def _canonical_reported_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise WorktreeInventoryError("Git returned a non-absolute worktree path")
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise WorktreeInventoryError(f"cannot canonicalize Git worktree path: {value!r}") from exc


def _canonical_git_directory(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise WorktreeInventoryError(f"Git returned a non-absolute {label}")
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise WorktreeInventoryError(f"Git returned an unavailable {label}: {value!r}") from exc
    if not canonical.is_dir():
        raise WorktreeInventoryError(f"Git returned a non-directory {label}: {canonical}")
    return canonical


def _git(subject: Path, *arguments: str) -> bytes:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            (
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(subject),
                *arguments,
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeInventoryError(f"cannot inspect Git worktrees: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise WorktreeInventoryError(f"git {' '.join(arguments)} failed: {detail}")
    if len(result.stdout) > _MAX_PORCELAIN_BYTES:
        raise WorktreeInventoryError("Git worktree inventory exceeds the bounded input limit")
    return result.stdout


def _git_line(subject: Path, label: str, *arguments: str) -> str:
    raw = _git(subject, *arguments)
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise WorktreeInventoryError(f"Git returned non-UTF-8 {label}") from exc
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise WorktreeInventoryError(f"Git returned malformed {label}")
    return value


def _field(record_index: int, raw: bytes) -> tuple[str, str | None]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorktreeInventoryError(
            f"worktree record {record_index} contains non-UTF-8 data"
        ) from exc
    key, separator, value = text.partition(" ")
    if not key:
        raise WorktreeInventoryError(f"worktree record {record_index} contains an empty field")
    return key, value if separator else None


def _reason(value: str | None, field: str, record_index: int) -> str | None:
    if value is None:
        return None
    if not value:
        raise WorktreeInventoryError(f"worktree record {record_index} has an empty {field} reason")
    return value


def parse_worktree_porcelain_z(raw: bytes) -> tuple[_ParsedWorktree, ...]:
    """Strictly parse Git's NUL-delimited worktree porcelain.

    Paths and optional reasons may contain whitespace and newlines.  Unknown fields, ambiguous
    record boundaries, duplicate identities, and semantically impossible record shapes fail
    closed instead of being guessed.
    """

    if not isinstance(raw, bytes) or not raw:
        raise WorktreeInventoryError("Git returned an empty worktree inventory")
    if len(raw) > _MAX_PORCELAIN_BYTES:
        raise WorktreeInventoryError("Git worktree inventory exceeds the bounded input limit")
    if not raw.endswith(b"\0\0"):
        raise WorktreeInventoryError("Git worktree inventory has no complete final record")
    body = raw[:-2]
    if not body or b"\0\0\0" in raw:
        raise WorktreeInventoryError("Git worktree inventory contains an empty record")

    encoded_records = body.split(b"\0\0")
    parsed: list[_ParsedWorktree] = []
    paths: set[Path] = set()
    branches: set[str] = set()
    for record_index, encoded in enumerate(encoded_records):
        fields = encoded.split(b"\0")
        if not fields or any(not item for item in fields):
            raise WorktreeInventoryError(f"worktree record {record_index} contains an empty field")
        first_key, first_value = _field(record_index, fields[0])
        if first_key != "worktree" or first_value is None or not first_value:
            raise WorktreeInventoryError(f"worktree record {record_index} must begin with a path")
        path = _canonical_reported_path(first_value)
        if path in paths:
            raise WorktreeInventoryError(f"duplicate canonical worktree path: {path}")
        paths.add(path)

        values: dict[str, str | None] = {}
        for encoded_field in fields[1:]:
            key, value = _field(record_index, encoded_field)
            if key not in {"HEAD", "branch", "bare", "detached", "locked", "prunable"}:
                raise WorktreeInventoryError(
                    f"worktree record {record_index} contains unknown field {key!r}"
                )
            if key in values:
                raise WorktreeInventoryError(
                    f"worktree record {record_index} repeats field {key!r}"
                )
            values[key] = value

        for flag in ("bare", "detached"):
            if flag in values and values[flag] is not None:
                raise WorktreeInventoryError(
                    f"worktree record {record_index} gives flag {flag!r} a value"
                )

        bare = "bare" in values
        if bare:
            if set(values) != {"bare"}:
                raise WorktreeInventoryError(
                    f"bare worktree record {record_index} contains incompatible fields"
                )
            kind: WorktreeKind = "bare"
            head = None
            branch = None
            detached = False
            locked = False
            locked_reason = None
            prunable = False
            prunable_reason = None
        else:
            head = values.get("HEAD")
            if head is None or _OID.fullmatch(head) is None:
                raise WorktreeInventoryError(
                    f"worktree record {record_index} has no valid full HEAD object id"
                )
            branch_ref = values.get("branch")
            detached = "detached" in values
            if (branch_ref is None) == (not detached):
                raise WorktreeInventoryError(
                    f"worktree record {record_index} must be either branched or detached"
                )
            if branch_ref is not None:
                if not branch_ref.startswith(_BRANCH_PREFIX) or len(branch_ref) == len(
                    _BRANCH_PREFIX
                ):
                    raise WorktreeInventoryError(
                        f"worktree record {record_index} has malformed branch ref"
                    )
                branch = branch_ref.removeprefix(_BRANCH_PREFIX)
                if not _valid_branch(branch):
                    raise WorktreeInventoryError(
                        f"worktree record {record_index} has malformed branch ref"
                    )
                if branch in branches:
                    raise WorktreeInventoryError(f"duplicate worktree branch: {branch}")
                branches.add(branch)
            else:
                branch = None
            kind = "main" if record_index == 0 else "linked"
            locked = "locked" in values
            locked_reason = _reason(values.get("locked"), "locked", record_index)
            prunable = "prunable" in values
            prunable_reason = _reason(values.get("prunable"), "prunable", record_index)

        parsed.append(
            _ParsedWorktree(
                path=path,
                kind=kind,
                head_oid=head,
                branch=branch,
                detached=detached,
                locked=locked,
                locked_reason=locked_reason,
                prunable=prunable,
                prunable_reason=prunable_reason,
            )
        )

    if any(item.kind == "bare" for item in parsed) and (
        len(parsed) != 1 or parsed[0].kind != "bare"
    ):
        raise WorktreeInventoryError("a bare repository cannot contain linked worktree records")
    return tuple(parsed)


def _checked_registry(
    registry: Iterable[WorktreeRegistration], common_digest: str
) -> dict[str, WorktreeRegistration]:
    if isinstance(registry, (str, bytes)) or not isinstance(registry, Iterable):
        raise WorktreeInventoryError("worktree registry must be an iterable of registrations")
    checked: dict[str, WorktreeRegistration] = {}
    branches: set[str] = set()
    for index, registration in enumerate(registry):
        if not isinstance(registration, WorktreeRegistration):
            raise WorktreeInventoryError(f"worktree registry entry {index} is malformed")
        if _SHA256.fullmatch(registration.repository_common_dir_sha256) is None:
            raise WorktreeInventoryError(
                f"worktree registry entry {index} has malformed common-dir identity"
            )
        if registration.repository_common_dir_sha256 != common_digest:
            raise WorktreeInventoryError(
                f"worktree registry entry {index} belongs to a foreign common directory"
            )
        if _SHA256.fullmatch(registration.worktree_sha256) is None:
            raise WorktreeInventoryError(
                f"worktree registry entry {index} has malformed worktree identity"
            )
        if not registration.branch.startswith("bh-") or not _valid_branch(registration.branch):
            raise WorktreeInventoryError(
                f"worktree registry entry {index} does not name a bh-* branch"
            )
        if registration.worktree_sha256 in checked:
            raise WorktreeInventoryError(
                f"duplicate worktree registry identity: {registration.worktree_sha256}"
            )
        if registration.branch in branches:
            raise WorktreeInventoryError(
                f"duplicate worktree registry branch: {registration.branch}"
            )
        checked[registration.worktree_sha256] = registration
        branches.add(registration.branch)
    return checked


def _custody(
    record: _ParsedWorktree,
    worktree_digest: str,
    registry: dict[str, WorktreeRegistration],
) -> WorktreeCustody:
    branch = record.branch
    registration = registry.get(worktree_digest)
    if registration is not None and registration.branch != branch:
        raise WorktreeInventoryError(
            f"registered worktree branch mismatch for {record.path}: "
            f"{registration.branch!r} != {branch!r}"
        )
    if branch is None or not branch.startswith("bh-"):
        return "foreign"
    if registration is None:
        return "unregistered"
    return "bearhug-owned"


def inventory_worktrees(
    *,
    subject: Path | str,
    common_dir: Path | str,
    registry: Iterable[WorktreeRegistration] = (),
) -> WorktreeInventory:
    """Read one exact repository's worktree inventory without changing any worktree."""

    subject_path = _canonical_existing_directory(subject, "subject worktree")
    common_path = _canonical_existing_directory(common_dir, "Git common directory")

    observed_common = _canonical_git_directory(
        _git_line(
            subject_path,
            "Git common directory",
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ),
        "Git common directory",
    )
    if observed_common != common_path:
        raise WorktreeInventoryError(
            f"subject belongs to foreign common directory {observed_common}, not {common_path}"
        )

    is_bare_text = _git_line(
        subject_path, "bare-repository state", "rev-parse", "--is-bare-repository"
    )
    if is_bare_text not in {"true", "false"}:
        raise WorktreeInventoryError("Git returned malformed bare-repository state")
    is_bare = is_bare_text == "true"
    if is_bare:
        if subject_path != common_path:
            raise WorktreeInventoryError("bare subject must be its exact Git common directory")
    else:
        top = _canonical_git_directory(
            _git_line(subject_path, "worktree root", "rev-parse", "--show-toplevel"),
            "worktree root",
        )
        if top != subject_path:
            raise WorktreeInventoryError(
                f"subject must name the exact worktree root, observed {top}"
            )

    common_digest = _path_sha256(common_path)
    checked_registry = _checked_registry(registry, common_digest)
    raw = _git(
        subject_path,
        f"--git-dir={common_path}",
        "worktree",
        "list",
        "--porcelain",
        "-z",
    )
    parsed = parse_worktree_porcelain_z(raw)

    # Close the inspection window: an administrative relink during the read invalidates the result.
    closing_common = _canonical_git_directory(
        _git_line(
            subject_path,
            "closing Git common directory",
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ),
        "closing Git common directory",
    )
    if closing_common != common_path:
        raise WorktreeInventoryError("subject common directory changed during inventory")

    by_path = {record.path: record for record in parsed}
    subject_record = by_path.get(subject_path)
    if subject_record is None:
        raise WorktreeInventoryError("exact subject is absent from its Git worktree inventory")
    if is_bare != (subject_record.kind == "bare"):
        raise WorktreeInventoryError("subject kind disagrees with Git worktree inventory")

    entries: list[WorktreeEntry] = []
    for record in parsed:
        digest = _path_sha256(record.path)
        entries.append(
            WorktreeEntry(
                path=record.path,
                worktree_sha256=digest,
                kind=record.kind,
                head_oid=record.head_oid,
                branch=record.branch,
                detached=record.detached,
                locked=record.locked,
                locked_reason=record.locked_reason,
                prunable=record.prunable,
                prunable_reason=record.prunable_reason,
                custody=_custody(record, digest, checked_registry),
            )
        )

    return WorktreeInventory(
        subject=subject_path,
        subject_worktree_sha256=_path_sha256(subject_path),
        common_dir=common_path,
        repository_common_dir_sha256=common_digest,
        entries=tuple(entries),
    )


__all__ = [
    "WorktreeEntry",
    "WorktreeInventory",
    "WorktreeInventoryError",
    "WorktreeRegistration",
    "inventory_worktrees",
    "parse_worktree_porcelain_z",
]
