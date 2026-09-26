"""Copy declared, Git-ignored evidence from the subject into a leased worktree.

``git worktree add`` materializes tracked files only, so a validation command that reads a
Git-ignored artifact — a search index, a telemetry journal — fails inside a lease for a
reason the capsule cannot repair, however many repair episodes it is given. Measured on
barracuda row 240: the same 1128 memex evidence anchors passed in the subject and failed
five times in the lease, all of them citing `.memq.json` or `.bearhug/telemetry/...`.

What may cross is declared in the sealed execution config and approved by digest; this
module decides nothing. It enforces the boundaries that make the copy safe:

- Git-ignored in the subject only. A tracked or untracked-but-not-ignored file would make
  the lease dirty and break the clean-base invariant the launcher just proved.
- No symlink anywhere on either side, resolved against the real subject root, so a declared
  path cannot reach outside the checkout.
- Bounded in count and bytes, and never overwriting anything the lease already has.
- Read from the subject, written only into the lease. The subject is never modified.

Every copy is reported with its digest so the evidence says what was inherited rather than
leaving a lease that silently differs from its subject.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.host_git import describe_dirty_status, run_git

MAX_MATERIALIZED_BYTES = 4 * 1024 * 1024 * 1024
MAX_MATERIALIZED_FILES = 200_000


class LeaseMaterializationError(RuntimeError):
    """A declared path cannot be carried into the lease safely."""


@dataclass(frozen=True)
class MaterializedPath:
    """One declared path, and what actually crossed."""

    path: str
    kind: str
    files: int
    bytes: int
    content_sha256: str


def _git_ignored(subject: Path, relative: str) -> bool:
    # Routed through the shared, hardened `run_git` so this ignore verdict honours the same
    # user global-ignore authority as `capture_launch_repository`, instead of the fully
    # ambient environment (no hardening at all) this call used before.
    try:
        result = run_git(subject, "check-ignore", "-q", "--", relative)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LeaseMaterializationError(
            f"cannot determine Git ignore status for {relative}: {exc}"
        ) from exc
    if result.returncode not in (0, 1):
        raise LeaseMaterializationError(
            f"cannot determine Git ignore status for {relative}"
        )
    return result.returncode == 0


def _check_shape(relative: str) -> None:
    """Refuse an escaping declaration before it reaches the filesystem or Git.

    This runs first: passing an absolute or traversing path to ``git check-ignore`` would
    ask about a file outside the checkout, and the answer would be meaningless.
    """

    candidate = PurePosixPath(relative)
    if not relative or relative != relative.strip():
        raise LeaseMaterializationError("declared path must be exact text")
    if candidate.is_absolute() or ".." in candidate.parts or relative.startswith("/"):
        raise LeaseMaterializationError(f"declared path escapes the subject: {relative}")


def _safe_source(subject: Path, relative: str) -> Path:
    """Resolve a declared path inside the subject, refusing every escape."""

    _check_shape(relative)
    source = subject / relative
    if os.path.islink(source):
        raise LeaseMaterializationError(f"declared path is a symlink: {relative}")
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise LeaseMaterializationError(f"declared path is unavailable: {relative}") from exc
    # resolve() follows every component, so this also catches a symlinked parent.
    inside = resolved == subject or subject in resolved.parents
    if resolved != source.absolute() or not inside:
        raise LeaseMaterializationError(f"declared path resolves outside the subject: {relative}")
    return resolved


def _lease_dirty(lease_root: Path) -> bytes:
    """The loop below tests "is this Git-ignored?"
    against the *subject* at its own current HEAD (``_git_ignored``), but writes into the
    *lease*, which may sit at an explicit, older ``base_oid``. When ``.gitignore`` gained the
    declared entry between the two commits, the comment above this module's own launcher
    invariant ("every declared path is ignored in the subject, so the worktree stays clean")
    does not hold: the copied path is untracked-but-not-ignored at the lease's own commit, and
    the very next cleanliness check (``capture_launch_repository``) dies with "provider launch
    requires a clean Git worktree" -- an error naming neither the declared path nor
    materialization. Checked here, against the lease itself, right after each declared path is
    copied, so the failure is attributed to the path that caused it instead.

    Routed through the shared, hardened ``run_git`` (rather than this module's own fully
    ambient call) so this verdict agrees with ``capture_launch_repository`` about the same
    lease worktree. Returns the raw status bytes; empty means clean, so the caller decides
    dirtiness on exactly what Git printed, the same as every other verdict in this codebase,
    and parses only if it needs to say what it saw.
    """

    try:
        result = run_git(lease_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LeaseMaterializationError(
            f"cannot determine the lease's own Git status: {exc}"
        ) from exc
    if result.returncode != 0:
        raise LeaseMaterializationError("cannot determine the lease's own Git status")
    return result.stdout


def _digest_file(path: Path, digest: Any) -> int:
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    return total


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)
    destination.chmod(stat.S_IMODE(source.stat().st_mode))


def materialize_declared_paths(
    *,
    subject: Path | str,
    destination: Path | str,
    declared: tuple[str, ...] | list[str],
) -> tuple[MaterializedPath, ...]:
    """Copy each declared Git-ignored path from the subject into the leased worktree."""

    if not declared:
        return ()
    subject_root = Path(subject).resolve(strict=True)
    lease_root = Path(destination).resolve(strict=True)
    if subject_root == lease_root:
        raise LeaseMaterializationError("lease and subject must be different checkouts")

    carried: list[MaterializedPath] = []
    total_files = 0
    total_bytes = 0
    for relative in declared:
        _check_shape(relative)
        if not _git_ignored(subject_root, relative):
            # Anything Git tracks is already in the lease, and anything it neither tracks
            # nor ignores would show up as a dirty worktree.
            raise LeaseMaterializationError(
                f"declared path is not Git-ignored in the subject: {relative}"
            )
        source = _safe_source(subject_root, relative)
        target = lease_root / relative
        if os.path.lexists(target):
            raise LeaseMaterializationError(f"lease already holds the declared path: {relative}")

        digest = hashlib.sha256()
        files = 0
        size = 0
        if source.is_dir():
            kind = "directory"
            for current in sorted(source.rglob("*")):
                if current.is_dir():
                    if current.is_symlink():
                        raise LeaseMaterializationError(
                            f"declared tree contains a symlinked directory: {relative}"
                        )
                    continue
                if current.is_symlink() or not current.is_file():
                    raise LeaseMaterializationError(
                        f"declared tree contains a non-regular file: {relative}"
                    )
                inner = current.relative_to(source).as_posix()
                digest.update(inner.encode("utf-8") + b"\0")
                size += _digest_file(current, digest)
                files += 1
                _copy_file(current, target / inner)
                if total_files + files > MAX_MATERIALIZED_FILES:
                    raise LeaseMaterializationError(
                        "declared paths exceed the materialization file bound"
                    )
                if total_bytes + size > MAX_MATERIALIZED_BYTES:
                    raise LeaseMaterializationError(
                        "declared paths exceed the materialization byte bound"
                    )
        elif source.is_file():
            kind = "file"
            size = _digest_file(source, digest)
            files = 1
            if total_bytes + size > MAX_MATERIALIZED_BYTES:
                raise LeaseMaterializationError(
                    "declared paths exceed the materialization byte bound"
                )
            _copy_file(source, target)
        else:
            raise LeaseMaterializationError(
                f"declared path is not a regular file or tree: {relative}"
            )

        total_files += files
        total_bytes += size
        # Raise here, naming this declared path, rather
        # than let a lease sealed against an older base surface as a downstream, uncorrelated
        # "dirty worktree" once the launcher's own cleanliness check runs.
        dirty = _lease_dirty(lease_root)
        if dirty:
            raise LeaseMaterializationError(
                f"declared path leaves the lease dirty: {relative}: "
                + describe_dirty_status(lease_root, dirty)
            )
        carried.append(MaterializedPath(relative, kind, files, size, digest.hexdigest()))
    return tuple(carried)


__all__ = [
    "DerivedMaterialization",
    "LeaseMaterializationError",
    "MAX_MATERIALIZED_BYTES",
    "MAX_MATERIALIZED_FILES",
    "MaterializedPath",
    "derive_lease_materialized_paths",
    "materialize_declared_paths",
]


PROBE_TIMEOUT_SECONDS = 28800.0
_MAX_PROBE_OUTPUT = 4 * 1024 * 1024


@dataclass(frozen=True)
class DerivedMaterialization:
    """What a probe observed, and what it could not observe."""

    paths: tuple[str, ...]
    status: str
    reason: str | None
    commands: tuple[str, ...]
    red_at_base: tuple[str, ...] = ()
    """Sealed commands that still fail on an untouched base with the evidence carried.

    No capsule can repair these: the work has not started, so the failure is not the
    work's. Measured on row 240 T2, where the campaign gate required a green
    `scripts/memex-lint.sh` on a corpus the capsule was commissioned to turn red, and a
    `go test` package holding a test that failed in 0.06s before any change.
    """


def _absolute_candidates(text: str, probe_root: Path) -> list[str]:
    """Absolute paths the command named that live under the probe worktree.

    Both the literal and the fully resolved root are matched: a temporary directory is
    reached through a symlink on macOS (/var -> /private/var), so a command that resolves
    its own paths reports a prefix that never equals the one we handed to Git.
    """

    prefixes = {str(probe_root), str(probe_root.resolve())}
    found: list[str] = []
    for token in text.replace(",", " ").replace("'", " ").replace('"', " ").split():
        cleaned = token.strip().rstrip(":;)")
        for prefix in prefixes:
            if not cleaned.startswith(prefix + os.sep):
                continue
            relative = cleaned[len(prefix) + 1 :]
            if relative and relative not in found:
                found.append(relative)
            break
    return found


def _still_red(
    subject_root: Path,
    probe: Path,
    observed: Sequence[str],
    failed: Sequence[tuple[str, Sequence[str]]],
    *,
    timeout_seconds: float,
) -> list[str]:
    """Re-run the commands that failed, this time with the evidence the lease was missing.

    A command failing in a bare probe proves nothing on its own: that is the ordinary
    case this module exists to fix, the lease simply lacked a Git-ignored file. One that
    still fails once the observed paths are carried in is failing on an untouched base
    under the conditions every capsule will meet. No capsule can repair that, because no
    work has happened yet.

    Measured on row 240 T2: `scripts/memex-lint.sh` was sealed into a campaign whose own
    plan commissioned the capsule to make it red, and the `go test` package held
    `TestOnTopic_TitleMatchOutranksBodyMention`, red in 0.06s at the base commit. The
    campaign ran 24 minutes of provider work before either was noticed.
    """

    if not failed:
        return []
    try:
        materialize_declared_paths(
            subject=subject_root, destination=probe, declared=list(observed)
        )
    except (LeaseMaterializationError, OSError):
        # The retry is a second opinion, not a gate of its own. Without the evidence it
        # would accuse commands that the real lease will satisfy, so say nothing.
        return []
    red: list[str] = []
    for text, argv in failed:
        try:
            done = subprocess.run(
                " ".join(str(part) for part in argv), shell=True, cwd=str(probe),
                capture_output=True, text=True, timeout=timeout_seconds, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode != 0:
            red.append(text)
    return red


def derive_lease_materialized_paths(
    *,
    subject: Path | str,
    commands: Sequence[Sequence[str]],
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> DerivedMaterialization:
    """Run the validation commands in a throwaway lease and see what the lease lacks.

    This is a measurement, not a prediction. It proves that these commands named these
    paths and that the paths exist and are Git-ignored in the subject; it does not prove
    the list is complete, because a command that fails silently on a missing file names
    nothing. An empty result with status ``observed`` means nothing was observed missing,
    not that nothing is.
    """

    subject_root = Path(subject).resolve(strict=True)
    if not commands:
        return DerivedMaterialization((), "not_consulted", "no validation commands", ())
    if shutil.which("git") is None:
        return DerivedMaterialization((), "unavailable", "git is not on PATH", ())

    observed: list[str] = []
    ran: list[str] = []
    failed: list[tuple[str, Sequence[str]]] = []
    red: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bearhug-lease-probe-") as scratch:
        probe = Path(scratch) / "probe"
        add = subprocess.run(
            ("git", "-C", str(subject_root), "worktree", "add", "-q", "--detach",
             str(probe), "HEAD"),
            capture_output=True, text=True, check=False,
        )
        if add.returncode != 0:
            return DerivedMaterialization(
                (), "unavailable", f"probe worktree unavailable: {add.stderr.strip()[:200]}", ()
            )
        try:
            for command in commands:
                argv = [str(part) for part in command]
                if not argv:
                    continue
                ran.append(" ".join(argv))
                try:
                    done = subprocess.run(
                        " ".join(argv), shell=True, cwd=str(probe), capture_output=True,
                        text=True, timeout=timeout_seconds, check=False,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    return DerivedMaterialization(
                        tuple(observed), "failed",
                        f"probe command did not complete: {type(exc).__name__}", tuple(ran),
                    )
                if done.returncode == 0:
                    continue
                failed.append((" ".join(argv), argv))
                text = (done.stdout or "")[:_MAX_PROBE_OUTPUT]
                text += (done.stderr or "")[:_MAX_PROBE_OUTPUT]
                for relative in _absolute_candidates(text, probe):
                    if relative in observed:
                        continue
                    # Five filters, all of which must hold: the command named it, the probe
                    # lacks it, the subject has it, Git ignores it there, and it is a path
                    # this module would accept.
                    if os.path.lexists(probe / relative):
                        continue
                    if not os.path.lexists(subject_root / relative):
                        continue
                    if not _git_ignored(subject_root, relative):
                        continue
                    try:
                        _check_shape(relative)
                    except LeaseMaterializationError:
                        continue
                    observed.append(relative)
            red = _still_red(
                subject_root, probe, observed, failed, timeout_seconds=timeout_seconds
            )
        finally:
            subprocess.run(
                ("git", "-C", str(subject_root), "worktree", "remove", "--force", str(probe)),
                capture_output=True, check=False,
            )
    return DerivedMaterialization(
        tuple(sorted(observed)), "observed", None, tuple(ran), tuple(red)
    )
