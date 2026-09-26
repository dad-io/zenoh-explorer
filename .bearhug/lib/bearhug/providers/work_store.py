"""Explicit provider-work binding preparation and create-only artifact persistence.

Preparation is read-only and has no output path.  Persistence is a separate call requiring an
absolute, existing, non-subject root; artifacts are published under Bear Hug's fixed
``work-authority/v1`` namespace with content-addressed filenames and create-only link semantics.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.prime import PrimeError, inspect_git
from bearhug.project_board import parse_board_bytes
from bearhug.providers.work_authority import (
    WorkBindingError,
    build_work_binding,
    validate_provider_work_observation,
    validate_work_binding,
)

BOARD_PATH = "docs/superpowers/plans/BOARD.md"
_MAX_SUBJECT_FILE_BYTES = 32 * 1024 * 1024
_SUPPORTED_PROVIDERS = frozenset({"anthropic-claude", "openai-codex"})


class WorkArtifactStoreError(ValueError):
    """Work authority could not be bound or persisted without ambiguity."""


@dataclass(frozen=True, slots=True)
class WorkAuthorityArtifacts:
    subject_worktree: str
    observation: dict[str, Any]
    binding: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PersistedWorkAuthority:
    observation_path: Path
    binding_path: Path


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise WorkArtifactStoreError(f"artifact is not canonical JSON: {exc}") from exc
    return (text + "\n").encode()


def _repository_path(
    value: str,
    where: str,
    *,
    suffix: str | None = ".md",
) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise WorkArtifactStoreError(f"{where} must be a canonical repository-relative path")
    path = PurePosixPath(value)
    parts = tuple(value.split("/"))
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in parts)
        or (suffix is not None and path.suffix != suffix)
    ):
        raise WorkArtifactStoreError(f"{where} must be a canonical repository-relative path")
    return parts


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        stat.S_IFMT(after.st_mode),
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _open_subject_file(
    root: Path,
    relative: str,
    *,
    suffix: str | None = ".md",
) -> tuple[int, os.stat_result]:
    parts = _repository_path(relative, relative, suffix=suffix)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(root, directory_flags)
    except OSError as exc:
        raise WorkArtifactStoreError(f"subject root cannot be opened safely: {root}") from exc
    try:
        for component in parts[:-1]:
            try:
                child = os.open(component, directory_flags, dir_fd=descriptor)
            except OSError as exc:
                raise WorkArtifactStoreError(
                    f"{relative} traverses a missing, symlinked, or non-directory component"
                ) from exc
            os.close(descriptor)
            descriptor = child
        try:
            file_descriptor = os.open(parts[-1], flags, dir_fd=descriptor)
        except OSError as exc:
            raise WorkArtifactStoreError(
                f"{relative} is missing, symlinked, or not readable"
            ) from exc
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(file_descriptor)
            raise WorkArtifactStoreError(f"{relative} is not a regular file")
        return file_descriptor, metadata
    finally:
        os.close(descriptor)


def read_subject_file(root: Path, relative: str, *, suffix: str | None = ".md") -> bytes:
    descriptor, before = _open_subject_file(root, relative, suffix=suffix)
    try:
        if before.st_size > _MAX_SUBJECT_FILE_BYTES:
            raise WorkArtifactStoreError(
                f"{relative} exceeds {_MAX_SUBJECT_FILE_BYTES} bytes"
            )
        chunks: list[bytes] = []
        remaining = _MAX_SUBJECT_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > _MAX_SUBJECT_FILE_BYTES or len(raw) != after.st_size:
            raise WorkArtifactStoreError(f"{relative} changed while it was read")
        if not _same_file(before, after):
            raise WorkArtifactStoreError(f"{relative} changed while it was read")
        return raw
    except OSError as exc:
        raise WorkArtifactStoreError(f"{relative} could not be read safely") from exc
    finally:
        os.close(descriptor)


def _assert_subject_file(root: Path, relative: str) -> None:
    descriptor, _ = _open_subject_file(root, relative)
    os.close(descriptor)


def _parsed_board(root: Path) -> list[dict[str, Any]]:
    board_before = read_subject_file(root, BOARD_PATH)
    try:
        rows = parse_board_bytes(board_before, error=WorkArtifactStoreError)
    except WorkArtifactStoreError:
        raise
    if board_before != read_subject_file(root, BOARD_PATH):
        raise WorkArtifactStoreError("the subject BOARD changed during verification")
    return rows


def _verified_candidates(
    root: Path,
    candidates: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Iterable):
        raise WorkArtifactStoreError("candidates must be an array")
    explicit = [dict(candidate) for candidate in candidates]
    if not explicit:
        return []
    rows = _parsed_board(root)
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        identifier = row.get("row")
        if not isinstance(identifier, str) or not identifier:
            raise WorkArtifactStoreError("the subject board parser returned a row without identity")
        if identifier in by_id:
            raise WorkArtifactStoreError(
                f"the subject board parser returned duplicate row {identifier}"
            )
        by_id[identifier] = row

    verified: list[dict[str, str]] = []
    for index, candidate in enumerate(explicit):
        if set(candidate) != {"board_row", "authority_path"}:
            raise WorkArtifactStoreError(f"candidates[{index}] has missing or unknown fields")
        board_row = candidate["board_row"]
        authority = candidate["authority_path"]
        if not isinstance(board_row, str) or board_row not in by_id:
            raise WorkArtifactStoreError(f"candidate BOARD row {board_row!r} was not parsed")
        if not isinstance(authority, str):
            raise WorkArtifactStoreError(f"candidates[{index}].authority_path is malformed")
        parsed_authority = by_id[board_row].get("authority")
        if parsed_authority != authority:
            raise WorkArtifactStoreError(
                f"candidate row {board_row} authority does not match the subject board parser"
            )
        _assert_subject_file(root, authority)
        verified.append({"board_row": board_row, "authority_path": authority})
    return verified


def authority_candidate(root: Path | str, board_row: str) -> dict[str, str]:
    """Resolve one explicit BOARD row to its authority path with Bear Hug's trusted parser.

    Callers still pass the returned typed mapping through ``prepare_work_authority_artifacts``,
    which parses the board again and checks the authority file while binding. The second read is
    intentional: a BOARD edit between resolution and preparation must fail closed rather than
    silently binding the active plan to a different row.
    """
    subject = Path(root).expanduser()
    rows = _parsed_board(subject)
    matches = [row for row in rows if row.get("row") == board_row]
    if not matches:
        raise WorkArtifactStoreError(f"candidate BOARD row {board_row!r} was not parsed")
    authority = matches[0].get("authority")
    if not isinstance(authority, str):
        raise WorkArtifactStoreError(
            f"candidate BOARD row {board_row!r} has no valid authority path"
        )
    _assert_subject_file(subject, authority)
    return {"board_row": board_row, "authority_path": authority}


def repository_identities(root: Path) -> tuple[str, str, Any]:
    try:
        git = inspect_git(root)
    except PrimeError as exc:
        raise WorkArtifactStoreError(f"subject Git identity could not be read: {exc}") from exc
    common_digest = hashlib.sha256(os.fsencode(git.common_dir)).hexdigest()
    worktree_material = {
        "algorithm": "bearhug-content-sensitive-worktree-v1",
        "repository_common_dir_sha256": common_digest,
        "worktree": git.worktree,
        "head": git.head,
        "dirty_sha256": git.dirty_sha256,
    }
    worktree_digest = hashlib.sha256(_canonical_bytes(worktree_material)).hexdigest()
    return common_digest, worktree_digest, git


def prepare_work_authority_artifacts(
    *,
    observation: Mapping[str, Any],
    subject: Path | str,
    task_id: str | None,
    active_plan_path: str,
    candidates: Iterable[Mapping[str, Any]],
    created_at: datetime,
) -> WorkAuthorityArtifacts:
    """Build validated artifacts in memory from explicit identities and authoritative files."""

    observed = validate_provider_work_observation(observation)
    if observed["provider"] not in _SUPPORTED_PROVIDERS:
        raise WorkArtifactStoreError("this workflow supports Claude and Codex only")
    try:
        first_common, first_worktree, first_git = repository_identities(
            Path(subject).expanduser()
        )
    except (OSError, TypeError) as exc:
        raise WorkArtifactStoreError("subject path could not be inspected") from exc
    root = Path(first_git.worktree)
    plan_bytes = read_subject_file(root, active_plan_path)
    verified = _verified_candidates(root, candidates)
    second_common, second_worktree, second_git = repository_identities(root)
    if (
        first_common != second_common
        or first_worktree != second_worktree
        or first_git.head != second_git.head
    ):
        raise WorkArtifactStoreError("subject checkout changed during binding preparation")
    try:
        binding = build_work_binding(
            observation=observed,
            task_id=task_id,
            repository_common_dir_sha256=first_common,
            worktree_sha256=first_worktree,
            active_plan_path=active_plan_path,
            active_plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            bindings=verified,
            created_at=created_at,
        )
    except WorkBindingError as exc:
        raise WorkArtifactStoreError(f"explicit work binding is invalid: {exc}") from exc
    return WorkAuthorityArtifacts(root.as_posix(), observed, binding)


def _open_absolute_directory(path: Path) -> int:
    unsafe = any(part in {".", ".."} for part in path.parts)
    if not path.is_absolute() or path == Path("/") or unsafe:
        raise WorkArtifactStoreError("output root must be an explicit absolute directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise WorkArtifactStoreError(
            "output root is missing, symlinked, or not a directory"
        ) from exc


def _child_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise WorkArtifactStoreError(f"cannot create artifact namespace {name!r}") from exc
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise WorkArtifactStoreError(
            f"artifact namespace {name!r} is not a real directory"
        ) from exc


def _publish(directory_fd: int, name: str, payload: bytes) -> None:
    temporary = f".{name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise WorkArtifactStoreError(f"cannot stage artifact {name}") from exc
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise WorkArtifactStoreError(f"refusing to overwrite artifact {name}") from exc
        os.fsync(directory_fd)
    except OSError as exc:
        raise WorkArtifactStoreError(f"cannot publish artifact {name}") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)


def persist_work_authority_artifacts(
    artifacts: WorkAuthorityArtifacts,
    *,
    output_root: Path | str,
) -> PersistedWorkAuthority:
    """Create both validated artifacts below one explicit non-subject output root."""

    if not isinstance(artifacts, WorkAuthorityArtifacts):
        raise WorkArtifactStoreError("artifacts must be a prepared WorkAuthorityArtifacts value")
    observation = validate_provider_work_observation(artifacts.observation)
    binding = validate_work_binding(artifacts.binding, observation=observation)
    if observation["provider"] not in _SUPPORTED_PROVIDERS:
        raise WorkArtifactStoreError("this store accepts Claude and Codex work only")
    root = Path(output_root).expanduser()
    if not root.is_absolute():
        raise WorkArtifactStoreError("output root must be an explicit absolute directory")
    subject = Path(artifacts.subject_worktree)
    try:
        common = Path(os.path.commonpath((root, subject)))
    except ValueError as exc:
        raise WorkArtifactStoreError("output root and subject path cannot be compared") from exc
    if common == subject:
        raise WorkArtifactStoreError(
            "Bear Hug artifact output must be outside the subject checkout"
        )

    root_fd = _open_absolute_directory(root)
    descriptors = [root_fd]
    try:
        namespace_fd = _child_directory(root_fd, "work-authority")
        descriptors.append(namespace_fd)
        version_fd = _child_directory(namespace_fd, "v1")
        descriptors.append(version_fd)
        observations_fd = _child_directory(version_fd, "observations")
        descriptors.append(observations_fd)
        bindings_fd = _child_directory(version_fd, "bindings")
        descriptors.append(bindings_fd)
        observation_name = f"{observation['observation_id']}.json"
        binding_name = f"{binding['binding_id']}.json"
        _publish(observations_fd, observation_name, _canonical_bytes(observation))
        _publish(bindings_fd, binding_name, _canonical_bytes(binding))
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    base = root / "work-authority" / "v1"
    return PersistedWorkAuthority(
        observation_path=base / "observations" / observation_name,
        binding_path=base / "bindings" / binding_name,
    )


__all__ = [
    "PersistedWorkAuthority",
    "WorkArtifactStoreError",
    "WorkAuthorityArtifacts",
    "authority_candidate",
    "persist_work_authority_artifacts",
    "prepare_work_authority_artifacts",
    "read_subject_file",
    "repository_identities",
]
