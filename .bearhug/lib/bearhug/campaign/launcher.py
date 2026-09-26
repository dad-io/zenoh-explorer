"""Provision one leased, clean-base campaign worktree without launching a provider.

The launcher reserves the complete worktree and claim identity before its first Git mutation.  It
then creates one derived ``bh-*`` branch and linked worktree, verifies their exact identity, and
publishes create-only custody evidence.  Cleanup never removes an attempted linked worktree; it
can remove only its still-empty pre-worktree directory and a synchronously confirmed branch.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.campaign.capsule_candidate import validate_dependency_base, verify_dependency_base
from bearhug.campaign.leases import CampaignLeaseStore, LeaseIdentity
from bearhug.campaign.worktrees import (
    WorktreeEntry,
    WorktreeInventoryError,
    WorktreeRegistration,
    inventory_worktrees,
)
from bearhug.host_git import describe_dirty_status, run_git
from bearhug.paths import ARTIFACT_ROOT, REPO_ROOT, WriteBoundaryError, assert_writable

_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_GIT_OUTPUT = 16 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 60
_MIN_LAUNCH_TTL_SECONDS = 2 * _GIT_TIMEOUT_SECONDS + 5
_HEARTBEAT_INTERVAL_SECONDS = 5.0
_TRANSIENT_GIT_SUFFIX = ".lock"
# Inside a per-worktree admin directory only these entries carry Git authority: they are what
# can repoint a checkout at other objects, refs or configuration. Everything else there is
# runtime state that ordinary Git work rewrites in place -- index, ORIG_HEAD, COMMIT_EDITMSG,
# FETCH_HEAD, reflogs under logs/ -- and sealing it reports a neighbouring `git status` as
# tampering.
_WORKTREE_AUTHORITY_ENTRIES = ("HEAD", "commondir", "config.worktree", "gitdir", "refs")


class CampaignLauncherError(RuntimeError):
    """A worktree could not be provisioned without guessing about custody."""


class CampaignLauncherOrphanedError(CampaignLauncherError):
    """Provisioning failed after cleanup could no longer be proved safe."""

    def __init__(self, message: str, *, lease_identity: LeaseIdentity) -> None:
        super().__init__(message)
        self.lease_identity = lease_identity


@dataclass(frozen=True, slots=True)
class CampaignWorktreeTarget:
    """The deterministic branch and canonical destination for one launch identity."""

    branch: str
    path: Path
    worktree_sha256: str


@dataclass(frozen=True, slots=True)
class CampaignWorktreeLaunch:
    """Closed launch authority returned after custody evidence reaches stable storage."""

    lease_identity: LeaseIdentity
    registration: WorktreeRegistration
    registration_content_sha256: str
    registration_path: Path
    worktree: Path
    branch: str
    base_oid: str


@dataclass(frozen=True, slots=True)
class _DirectorySeal:
    path: Path
    device: int
    inode: int
    uid: int
    mode: int


@dataclass(frozen=True, slots=True)
class _SecurityPathSeal:
    path: Path
    kind: str
    device: int | None
    inode: int | None
    uid: int | None
    mode: int | None
    content_sha256: str | None


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise CampaignLauncherError(f"{label} must be a canonical lowercase token")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CampaignLauncherError(f"{label} must be lowercase SHA-256")
    return value


def _oid(value: Any, label: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise CampaignLauncherError(f"{label} must be a full lowercase Git object id")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CampaignLauncherError(f"custody evidence is not canonical JSON: {exc}") from exc


def _path_sha256(path: Path) -> str:
    return hashlib.sha256(os.fsencode(path)).hexdigest()


def _seal_owned_directory(value: Path | str, label: str) -> _DirectorySeal:
    requested = Path(value).expanduser()
    try:
        if requested.is_symlink():
            raise CampaignLauncherError(f"{label} may not be a symlink")
        path = requested.resolve(strict=True)
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CampaignLauncherError(f"cannot resolve {label}: {requested}") from exc
    mode = stat.S_IMODE(observed.st_mode)
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.geteuid() or mode & 0o077:
        raise CampaignLauncherError(f"{label} must be an owner-only physical directory")
    return _DirectorySeal(path, observed.st_dev, observed.st_ino, observed.st_uid, mode)


def _check_seal(seal: _DirectorySeal, label: str) -> None:
    if _seal_owned_directory(seal.path, label) != seal:
        raise CampaignLauncherError(f"{label} identity changed during provisioning")


def _seal_git_directory(path: Path, label: str) -> _DirectorySeal:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CampaignLauncherError(f"{label} is unavailable") from exc
    mode = stat.S_IMODE(observed.st_mode)
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.geteuid() or mode & 0o022:
        raise CampaignLauncherError(
            f"{label} must be physical, user-owned, and not group/other writable"
        )
    return _DirectorySeal(
        path,
        observed.st_dev,
        observed.st_ino,
        observed.st_uid,
        mode,
    )


def _check_git_seal(seal: _DirectorySeal, label: str) -> None:
    if _seal_git_directory(seal.path, label) != seal:
        raise CampaignLauncherError(f"{label} identity changed during provisioning")


def _seal_security_path(path: Path) -> _SecurityPathSeal:
    if not os.path.lexists(path):
        return _SecurityPathSeal(path, "missing", None, None, None, None, None)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CampaignLauncherError(f"cannot seal Git authority path: {path}") from exc
    try:
        observed = os.fstat(descriptor)
        mode = stat.S_IMODE(observed.st_mode)
        if observed.st_uid != os.geteuid() or mode & 0o022:
            raise CampaignLauncherError(
                f"Git authority path must be user-owned and not group/other writable: {path}"
            )
        if stat.S_ISDIR(observed.st_mode):
            kind = "directory"
            content_sha256 = None
        elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
            kind = "file"
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_GIT_OUTPUT:
                    raise CampaignLauncherError(f"Git authority file exceeds limit: {path}")
                digest.update(chunk)
            content_sha256 = digest.hexdigest()
        else:
            raise CampaignLauncherError(
                f"Git authority path is not a physical file/directory: {path}"
            )
        closing = os.fstat(descriptor)
        if (closing.st_dev, closing.st_ino, closing.st_mode) != (
            observed.st_dev,
            observed.st_ino,
            observed.st_mode,
        ):
            raise CampaignLauncherError(f"Git authority path changed while sealing: {path}")
        return _SecurityPathSeal(
            path,
            kind,
            observed.st_dev,
            observed.st_ino,
            observed.st_uid,
            mode,
            content_sha256,
        )
    finally:
        os.close(descriptor)


def _walk_authority_tree(root: Path, budget: int) -> tuple[set[Path], int]:
    """Enumerate one authority tree, skipping Git's transient `*.lock` files.

    A lock file exists only for the span of the Git operation that holds it, so its
    presence or absence is never evidence of tampering. Sealing one turns any concurrent
    Git command -- another worktree's `git status`, a dashboard poll, a fetch -- into a
    spurious "Git authority path changed" abort.
    """

    found: set[Path] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        if current.name.endswith(_TRANSIENT_GIT_SUFFIX):
            continue
        found.add(current)
        budget -= 1
        if budget < 0:
            raise CampaignLauncherError("Git authority tree exceeds bounded path inventory")
        try:
            if current.is_dir() and not current.is_symlink():
                pending.extend(current.iterdir())
        except OSError as exc:
            raise CampaignLauncherError(f"cannot enumerate Git authority tree: {current}") from exc
    return found, budget


def _repository_security_seals(
    subject_git_dir: Path, common_dir: Path
) -> tuple[_SecurityPathSeal, ...]:
    candidates = {
        common_dir / "HEAD",
        common_dir / "config",
        common_dir / "config.worktree",
        common_dir / "info",
        common_dir / "info" / "attributes",
        common_dir / "info" / "grafts",
        common_dir / "objects",
        common_dir / "objects" / "info",
        common_dir / "objects" / "info" / "alternates",
        common_dir / "objects" / "pack",
        common_dir / "packed-refs",
        common_dir / "refs",
        common_dir / "shallow",
        subject_git_dir / "HEAD",
        subject_git_dir / "commondir",
        subject_git_dir / "config.worktree",
    }
    budget = 10_000
    refs_root = common_dir / "refs"
    if os.path.lexists(refs_root):
        found, budget = _walk_authority_tree(refs_root, budget)
        candidates |= found
    worktrees_root = common_dir / "worktrees"
    if os.path.lexists(worktrees_root):
        candidates.add(worktrees_root)
        try:
            admins = sorted(worktrees_root.iterdir())
        except OSError as exc:
            raise CampaignLauncherError(
                f"cannot enumerate Git authority tree: {worktrees_root}"
            ) from exc
        for admin in admins:
            candidates.add(admin)
            budget -= 1
            if budget < 0:
                raise CampaignLauncherError("Git authority tree exceeds bounded path inventory")
            if admin.is_symlink() or not admin.is_dir():
                continue
            for name in _WORKTREE_AUTHORITY_ENTRIES:
                entry = admin / name
                # Seal the name whether or not it exists today: a "missing" seal is what makes
                # a config.worktree or a per-worktree ref *appearing* mid-provisioning drift.
                candidates.add(entry)
                budget -= 1
                if budget < 0:
                    raise CampaignLauncherError(
                        "Git authority tree exceeds bounded path inventory"
                    )
                if name == "refs" and os.path.lexists(entry):
                    found, budget = _walk_authority_tree(entry, budget)
                    candidates |= found
    return tuple(_seal_security_path(path) for path in sorted(candidates))


def _check_security_seals(seals: tuple[_SecurityPathSeal, ...]) -> None:
    for seal in seals:
        if _seal_security_path(seal.path) != seal:
            raise CampaignLauncherError(f"Git authority path changed: {seal.path}")


def _worktree_admin_seals(destination: Path, common_dir: Path) -> tuple[_SecurityPathSeal, ...]:
    raw = Path(_git_text(destination, "rev-parse", "--path-format=absolute", "--git-dir"))
    try:
        admin = raw.resolve(strict=True)
    except OSError as exc:
        raise CampaignLauncherError("created worktree admin directory is unavailable") from exc
    admin_parent = common_dir / "worktrees"
    if admin.parent != admin_parent or not admin.is_dir():
        raise CampaignLauncherError("created worktree admin directory escaped Git authority")
    paths = {
        admin_parent,
        admin,
        admin / "HEAD",
        admin / "commondir",
        admin / "gitdir",
        # Sealed whether or not it exists, like the equivalent entry in
        # `_repository_security_seals`: a `config.worktree` appearing here after this
        # point (from `worktree add`'s copy or anything else) is drift, not authority.
        admin / "config.worktree",
    }
    return tuple(_seal_security_path(path) for path in sorted(paths))


def _git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def _git(
    cwd: Path,
    *arguments: str,
    ok: tuple[int, ...] = (0,),
    input_bytes: bytes | None = None,
) -> tuple[int, bytes]:
    argv = (
        "git",
        "--no-optional-locks",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-C",
        str(cwd),
        *arguments,
    )
    try:
        result = subprocess.run(
            argv,
            input=input_bytes,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CampaignLauncherError(f"cannot run Git command {arguments[0]!r}: {exc}") from exc
    if len(result.stdout) > _MAX_GIT_OUTPUT or len(result.stderr) > _MAX_GIT_OUTPUT:
        raise CampaignLauncherError("Git command output exceeded the bounded limit")
    if result.returncode not in ok:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CampaignLauncherError(f"git {' '.join(arguments)} failed: {detail}")
    return result.returncode, result.stdout


def _git_text(cwd: Path, *arguments: str) -> str:
    _, raw = _git(cwd, *arguments)
    try:
        value = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise CampaignLauncherError("Git returned non-UTF-8 identity output") from exc
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise CampaignLauncherError("Git returned malformed identity output")
    return value


def _branch_oid(subject: Path, common_dir: Path, branch: str) -> str | None:
    returncode, raw = _git(
        subject,
        f"--git-dir={common_dir}",
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        ok=(0, 1),
    )
    if returncode == 1:
        return None
    try:
        value = raw.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise CampaignLauncherError("Git returned a non-ASCII branch object id") from exc
    return _oid(value, "observed branch object id")


def _nul_fields(raw: bytes, label: str) -> tuple[str, ...]:
    if not raw:
        return ()
    if not raw.endswith(b"\x00"):
        raise CampaignLauncherError(f"Git returned malformed {label}")
    try:
        fields = tuple(field.decode("utf-8", errors="strict") for field in raw[:-1].split(b"\x00"))
    except UnicodeDecodeError as exc:
        raise CampaignLauncherError(f"Git returned non-UTF-8 {label}") from exc
    if any(not field or "\n" in field or "\r" in field for field in fields):
        raise CampaignLauncherError(f"Git returned malformed {label}")
    return fields


def _config_names(subject: Path, scope: str) -> tuple[str, ...]:
    _, raw = _git(
        subject,
        "config",
        scope,
        "--no-includes",
        "--null",
        "--name-only",
        "--list",
    )
    return _nul_fields(raw, f"{scope.removeprefix('--')} configuration")


def _worktree_config_path(subject: Path, *git_dir_args: str) -> Path:
    """Resolve a per-worktree config file, absolute, via Git itself.

    With no extra arguments this is the file Git reads for ``subject``'s own worktree
    scope. With ``f"--git-dir={common_dir}"`` it is the file the same-shaped
    ``git --git-dir=<common> worktree add`` mutation this module runs (the
    ``_git_mutation_with_lease`` worktree-add call) copies into a newly created linked
    worktree: Git resolves ``--git-path`` relative to the given ``--git-dir``, which for
    ``--git-dir=<common>`` is always ``<common>/config.worktree`` -- the *main* worktree's
    own per-worktree file -- regardless of which worktree ``subject`` actually is. Git
    2.50.1 (Apple Git-155) confirms the copy: ``git worktree add`` run this way copies
    ``<common>/config.worktree`` verbatim (filtering only ``core.bare``/``core.worktree``)
    into the new worktree's own ``config.worktree``, even when invoked with ``-C`` on an
    unrelated linked worktree.
    """

    text = _git_text(
        subject,
        *git_dir_args,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "config.worktree",
    )
    path = Path(text)
    if not path.is_absolute():
        raise CampaignLauncherError("Git returned a non-absolute per-worktree config path")
    return path


def _config_names_at_path(cwd: Path, path: Path) -> tuple[str, ...]:
    _, raw = _git(
        cwd,
        "config",
        "--file",
        str(path),
        "--no-includes",
        "--null",
        "--name-only",
        "--list",
    )
    return _nul_fields(raw, "worktree configuration")


def _reject_checkout_commands(subject: Path, common_dir: Path, base_oid: str) -> None:
    """Fail closed on repository-controlled commands used by worktree checkout."""

    local_names = _config_names(subject, "--local")
    names = list(local_names)
    lowered = {name.lower() for name in local_names}
    if any(name == "include.path" or name.startswith("includeif.") for name in lowered):
        raise CampaignLauncherError("repository configuration includes external configuration")
    if "extensions.worktreeconfig" in lowered:
        returncode, raw = _git(
            subject,
            "config",
            "--local",
            "--no-includes",
            "--type=bool",
            "--get",
            "extensions.worktreeConfig",
            ok=(0, 1),
        )
        if returncode == 0:
            try:
                enabled = raw.decode("ascii", errors="strict").strip() == "true"
            except UnicodeDecodeError as exc:
                raise CampaignLauncherError(
                    "Git returned malformed worktreeConfig configuration"
                ) from exc
            if enabled:
                own_path = _worktree_config_path(subject)
                if os.path.lexists(own_path):
                    names.extend(_config_names_at_path(subject, own_path))
                # `git worktree add --git-dir=<common> ...` always copies
                # `<common>/config.worktree` into a newly created worktree, whichever
                # worktree the command was issued from (see `_worktree_config_path`).
                # Inspect that copy source too, so a subject that is itself a linked
                # worktree cannot inherit a forbidden key it never had in its own file.
                copy_source_path = _worktree_config_path(subject, f"--git-dir={common_dir}")
                if copy_source_path != own_path and os.path.lexists(copy_source_path):
                    names.extend(_config_names_at_path(subject, copy_source_path))

    command_keys = {
        "core.attributesfile",
        "core.fsmonitor",
        "core.fsmonitorhookversion",
        "extensions.partialclone",
    }
    for name in names:
        normalized = name.lower()
        if normalized == "include.path" or normalized.startswith("includeif."):
            raise CampaignLauncherError("repository configuration includes external configuration")
        network_capable = normalized.startswith("remote.") and normalized.endswith(
            (".promisor", ".partialclonefilter", ".uploadpack")
        )
        if normalized.startswith("filter.") or normalized in command_keys or network_capable:
            raise CampaignLauncherError(
                f"repository checkout command configuration is not allowed: {name}"
            )

    alternates = common_dir / "objects" / "info" / "alternates"
    if os.path.lexists(alternates):
        raise CampaignLauncherError("alternate object stores are not allowed for checkout")
    pack_directory = common_dir / "objects" / "pack"
    try:
        if pack_directory.is_dir() and any(
            entry.name.endswith(".promisor") for entry in pack_directory.iterdir()
        ):
            raise CampaignLauncherError("promisor object packs are not allowed for checkout")
    except OSError as exc:
        raise CampaignLauncherError("cannot inspect local object-pack authority") from exc

    _, objects = _git(
        subject,
        f"--git-dir={common_dir}",
        "rev-list",
        "--objects",
        "--missing=print",
        "--no-object-names",
        base_oid,
    )
    try:
        object_lines = objects.decode("ascii", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise CampaignLauncherError("Git returned non-ASCII local object inventory") from exc
    if not object_lines or any(line.startswith("?") for line in object_lines):
        raise CampaignLauncherError(
            "base checkout is not fully available in the local object store"
        )
    if any(_OID.fullmatch(line) is None for line in object_lines):
        raise CampaignLauncherError("Git returned malformed local object inventory")

    _, paths = _git(
        subject,
        f"--git-dir={common_dir}",
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        base_oid,
    )
    if paths and not paths.endswith(b"\x00"):
        raise CampaignLauncherError("Git returned malformed base path inventory")
    if not paths:
        return
    _, attributes = _git(
        subject,
        "check-attr",
        "--cached",
        "-z",
        "--stdin",
        "filter",
        input_bytes=paths,
    )
    fields = _nul_fields(attributes, "filter attribute inventory")
    if len(fields) % 3 != 0:
        raise CampaignLauncherError("Git returned malformed filter attribute inventory")
    for index in range(0, len(fields), 3):
        path, attribute, value = fields[index : index + 3]
        if attribute != "filter":
            raise CampaignLauncherError("Git returned an unexpected checkout attribute")
        if value not in {"unspecified", "unset"}:
            raise CampaignLauncherError(
                f"repository checkout filter attribute is not allowed: {path}"
            )


def _refresh_lease(
    lease_store: CampaignLeaseStore,
    identity: LeaseIdentity,
    *,
    ttl_seconds: float,
    now: float | None,
) -> None:
    try:
        record = lease_store.heartbeat(identity, ttl_seconds=ttl_seconds, now=now)
    except BaseException as exc:
        raise CampaignLauncherOrphanedError(
            "campaign worktree lease is no longer active; artifacts remain fenced",
            lease_identity=identity,
        ) from exc
    if record.state != "active" or record.identity != identity:
        raise CampaignLauncherOrphanedError(
            "campaign worktree lease identity changed; artifacts remain fenced",
            lease_identity=identity,
        )


def _launch_ttl(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= _MIN_LAUNCH_TTL_SECONDS
    ):
        raise CampaignLauncherError(f"launcher TTL must exceed {_MIN_LAUNCH_TTL_SECONDS} seconds")
    return float(value)


def _lease_clock(*, now: float | None) -> Callable[[], float]:
    started_monotonic = time.monotonic()
    if now is None:
        started_lease_time = time.time()
    elif isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise CampaignLauncherError("now must be a finite timestamp")
    else:
        started_lease_time = float(now)

    def advancing() -> float:
        return started_lease_time + max(0.0, time.monotonic() - started_monotonic)

    return advancing


def _git_mutation_with_lease(
    cwd: Path,
    *arguments: str,
    lease_store: CampaignLeaseStore,
    lease_identity: LeaseIdentity,
    ttl_seconds: float,
    clock: Callable[[], float],
) -> tuple[int, bytes]:
    _refresh_lease(
        lease_store,
        lease_identity,
        ttl_seconds=ttl_seconds,
        now=clock(),
    )
    stop = threading.Event()
    heartbeat_errors: list[BaseException] = []
    interval = min(_HEARTBEAT_INTERVAL_SECONDS, ttl_seconds / 4)

    def keep_alive() -> None:
        while not stop.wait(interval):
            try:
                _refresh_lease(
                    lease_store,
                    lease_identity,
                    ttl_seconds=ttl_seconds,
                    now=clock(),
                )
            except BaseException as exc:
                heartbeat_errors.append(exc)
                stop.set()

    heartbeat = threading.Thread(target=keep_alive, name="bearhug-lease-heartbeat", daemon=True)
    heartbeat.start()
    result: tuple[int, bytes] | None = None
    command_error: BaseException | None = None
    try:
        result = _git(cwd, *arguments)
    except BaseException as exc:
        command_error = exc
    finally:
        stop.set()
        heartbeat.join()
    if heartbeat_errors:
        raise CampaignLauncherOrphanedError(
            "lease heartbeat failed during Git mutation; artifacts remain fenced",
            lease_identity=lease_identity,
        ) from heartbeat_errors[0]
    _refresh_lease(
        lease_store,
        lease_identity,
        ttl_seconds=ttl_seconds,
        now=clock(),
    )
    if command_error is not None:
        raise command_error
    if result is None:
        raise CampaignLauncherOrphanedError(
            "Git mutation returned no result; artifacts remain fenced",
            lease_identity=lease_identity,
        )
    return result


def derive_campaign_worktree_target(
    *,
    worktree_parent: Path | str,
    campaign_id: str,
    run_id: str,
    controller_authority_sha256: str,
    work_unit_id: str,
    session_id: str,
    claimant_id: str,
    repository_common_dir_sha256: str,
    base_oid: str,
) -> CampaignWorktreeTarget:
    """Derive one stable branch and destination from the complete launch identity."""

    campaign = _token(campaign_id, "campaign_id")
    run = _token(run_id, "run_id")
    authority = _sha256(controller_authority_sha256, "controller_authority_sha256")
    work_unit = _token(work_unit_id, "work_unit_id")
    session = _token(session_id, "session_id")
    claimant = _token(claimant_id, "claimant_id")
    common = _sha256(repository_common_dir_sha256, "repository_common_dir_sha256")
    base = _oid(base_oid, "base_oid")
    parent = _seal_owned_directory(worktree_parent, "worktree parent").path
    material = {
        "base_oid": base,
        "campaign_id": campaign,
        "claimant_id": claimant,
        "controller_authority_sha256": authority,
        "repository_common_dir_sha256": common,
        "run_id": run,
        "session_id": session,
        "work_unit_id": work_unit,
    }
    suffix = hashlib.sha256(_canonical(material)).hexdigest()[:24]
    label = re.sub(r"[^a-z0-9]+", "-", work_unit).strip("-")[:40] or "work"
    branch = f"bh-{label}-{suffix}"
    destination = parent / branch
    if destination.parent != parent or not branch.startswith("bh-"):
        raise CampaignLauncherError("derived worktree target escaped its exact parent")
    return CampaignWorktreeTarget(branch, destination, _path_sha256(destination))


def _require_base_identity(
    subject: Path, subject_head_oid: str, base_oid: str, inventory: Any
) -> None:
    entry = next((item for item in inventory.entries if item.path == subject), None)
    if entry is None or entry.kind == "bare" or entry.head_oid != subject_head_oid:
        raise CampaignLauncherError("subject HEAD moved away from the sealed typeset HEAD")
    if _git_text(subject, "rev-parse", "--verify", "HEAD") != subject_head_oid:
        raise CampaignLauncherError("subject HEAD does not match the sealed typeset HEAD")
    try:
        resolved = _git_text(subject, "rev-parse", "--verify", f"{base_oid}^{{commit}}")
    except CampaignLauncherError as exc:
        raise CampaignLauncherError("explicit base does not resolve to itself as a commit") from exc
    if resolved != base_oid:
        raise CampaignLauncherError("explicit base does not resolve to itself as a commit")
    ancestor, _ = _git(
        subject,
        "merge-base",
        "--is-ancestor",
        base_oid,
        subject_head_oid,
        ok=(0, 1),
    )
    if ancestor != 0:
        raise CampaignLauncherError("explicit base is not an ancestor of the sealed typeset HEAD")


def _clean_verdict(cwd: Path, *, label: str) -> None:
    """The one cleanliness verdict this module makes, routed through the shared, hardened
    ``run_git`` (unlike this module's own ``_git``, used everywhere else here for identity
    reads and the mutating worktree/branch creation ``run_git``'s stricter repository-filter
    refusal and lack of stdin support must not reach) so it agrees with
    ``capture_launch_repository`` about the same repository, and names what it saw the same way.
    """

    try:
        result = run_git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CampaignLauncherError(f"cannot inspect Git status for {cwd}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CampaignLauncherError(f"git status failed: {detail}")
    if result.stdout:
        raise CampaignLauncherError(f"{label}: " + describe_dirty_status(cwd, result.stdout))


def _require_clean_base(
    subject: Path, subject_head_oid: str, base_oid: str, inventory: Any
) -> None:
    _require_base_identity(subject, subject_head_oid, base_oid, inventory)
    _clean_verdict(subject, label="subject worktree is not clean at the explicit base")


def _verify_created_worktree(
    *,
    subject: Path,
    common_dir: Path,
    destination: Path,
    destination_identity: tuple[int, int],
    branch: str,
    base_oid: str,
) -> WorktreeEntry:
    observed = destination.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) & 0o077
        or (observed.st_dev, observed.st_ino) != destination_identity
    ):
        raise CampaignLauncherError("created worktree directory custody changed")
    _reject_checkout_commands(subject, common_dir, base_oid)
    # The `worktree add` mutation just ran may have copied a per-worktree config file
    # (see `_worktree_config_path`) into the destination's own admin directory. Re-run
    # the same refusal set against the destination itself so a forbidden key that landed
    # there -- whether copied at creation or written by a concurrent command -- is caught
    # before this worktree is trusted with custody.
    _reject_checkout_commands(destination, common_dir, base_oid)
    inventory = inventory_worktrees(subject=subject, common_dir=common_dir)
    entry = next((item for item in inventory.entries if item.path == destination), None)
    if (
        entry is None
        or entry.kind != "linked"
        or entry.branch != branch
        or entry.head_oid != base_oid
        or entry.detached
        or entry.locked
        or entry.prunable
    ):
        raise CampaignLauncherError("created worktree does not have the exact leased Git identity")
    if _git_text(destination, "rev-parse", "--verify", "HEAD") != base_oid:
        raise CampaignLauncherError("created worktree HEAD does not match the leased base")
    if _git_text(destination, "symbolic-ref", "--quiet", "--short", "HEAD") != branch:
        raise CampaignLauncherError("created worktree branch does not match the lease")
    _clean_verdict(destination, label="created worktree is not clean")
    if _branch_oid(subject, common_dir, branch) != base_oid:
        raise CampaignLauncherError("created branch moved away from the leased base")
    return entry


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_create_only(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(8).hex()}")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise CampaignLauncherError("custody registration already exists") from exc
        _fsync_directory(path.parent)
    finally:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()
        _fsync_directory(path.parent)


def _registration_bytes(
    *,
    lease: LeaseIdentity,
    registration: WorktreeRegistration,
    worktree: Path,
    work_unit_id: str,
) -> tuple[str, bytes]:
    material: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "campaign_worktree_registration",
        "campaign_id": lease.campaign_id,
        "run_id": lease.run_id,
        "controller_authority_sha256": lease.controller_authority_sha256,
        "work_unit_id": work_unit_id,
        "session_id": lease.session_id,
        "claimant_id": lease.claimant_id,
        "lease_id": lease.lease_id,
        "lease_epoch": lease.epoch,
        "repository_common_dir_sha256": registration.repository_common_dir_sha256,
        "worktree_sha256": registration.worktree_sha256,
        "worktree": os.fsdecode(os.fsencode(worktree)),
        "branch": registration.branch,
        "base_oid": lease.base_oid,
    }
    content_sha256 = hashlib.sha256(_canonical(material)).hexdigest()
    material["content_sha256"] = content_sha256
    return content_sha256, _canonical(material)


def _remove_proven_artifacts(
    *,
    subject: Path,
    common_dir: Path,
    subject_seal: _DirectorySeal,
    common_seal: _DirectorySeal,
    security_seals: tuple[_SecurityPathSeal, ...],
    parent_seal: _DirectorySeal,
    lease_store: CampaignLeaseStore,
    lease_identity: LeaseIdentity,
    ttl_seconds: float,
    clock: Callable[[], float],
    destination: Path,
    destination_identity: tuple[int, int] | None,
    destination_mutation_attempted: bool,
    branch: str,
    base_oid: str,
    branch_mutation_attempted: bool,
    branch_creation_confirmed: bool,
    worktree_attempted: bool,
) -> bool:
    """Best-effort rollback; false means the lease must remain as a fence."""

    try:
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_seal(parent_seal, "worktree parent")
        # A linked-worktree removal recursively deletes its checkout.  Once Git may have touched
        # the directory, no in-process observation can exclude a writer arriving during deletion.
        if worktree_attempted:
            return False
        destination_removed = not os.path.lexists(destination)
        if not destination_removed:
            if destination_identity is None or not destination_mutation_attempted:
                return False
            observed = destination.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(observed.st_mode)
                or (
                    observed.st_dev,
                    observed.st_ino,
                )
                != destination_identity
            ):
                return False
            _refresh_lease(
                lease_store,
                lease_identity,
                ttl_seconds=ttl_seconds,
                now=clock(),
            )
            try:
                destination.rmdir()
            except OSError:
                destination_removed = False
            else:
                destination_removed = True

        observed_oid = _branch_oid(subject, common_dir, branch)
        branch_removed = observed_oid is None
        if observed_oid is not None:
            if (
                not branch_mutation_attempted
                or not branch_creation_confirmed
                or observed_oid != base_oid
                or not destination_removed
            ):
                return False
            _check_git_seal(subject_seal, "subject worktree")
            _check_git_seal(common_seal, "Git common directory")
            _check_security_seals(security_seals)
            closing_before_ref = inventory_worktrees(subject=subject, common_dir=common_dir)
            if any(
                item.path == destination or item.branch == branch
                for item in closing_before_ref.entries
            ):
                return False
            _git_mutation_with_lease(
                subject,
                f"--git-dir={common_dir}",
                "update-ref",
                "-d",
                f"refs/heads/{branch}",
                base_oid,
                lease_store=lease_store,
                lease_identity=lease_identity,
                ttl_seconds=ttl_seconds,
                clock=clock,
            )
            branch_removed = _branch_oid(subject, common_dir, branch) is None
        if destination_removed and branch_removed:
            closing = inventory_worktrees(subject=subject, common_dir=common_dir)
            return (
                not os.path.lexists(destination)
                and _branch_oid(subject, common_dir, branch) is None
                and all(
                    item.path != destination and item.branch != branch for item in closing.entries
                )
            )
    except (CampaignLauncherError, OSError, WorktreeInventoryError):
        return False
    return False


def _artifacts_proved_absent(
    *,
    subject: Path,
    common_dir: Path,
    subject_seal: _DirectorySeal,
    common_seal: _DirectorySeal,
    security_seals: tuple[_SecurityPathSeal, ...],
    parent_seal: _DirectorySeal,
    destination: Path,
    branch: str,
) -> bool:
    try:
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_seal(parent_seal, "worktree parent")
        if os.path.lexists(destination) or _branch_oid(subject, common_dir, branch) is not None:
            return False
        inventory = inventory_worktrees(subject=subject, common_dir=common_dir)
        if any(item.path == destination or item.branch == branch for item in inventory.entries):
            return False
        return not os.path.lexists(destination) and _branch_oid(subject, common_dir, branch) is None
    except (CampaignLauncherError, OSError, WorktreeInventoryError):
        return False


def provision_campaign_worktree(
    *,
    subject: Path | str,
    common_dir: Path | str,
    state_root: Path | str,
    worktree_parent: Path | str,
    lease_store: CampaignLeaseStore,
    lease_subject_root: Path | str | None = None,
    campaign_id: str,
    run_id: str,
    controller_authority_sha256: str,
    work_unit_id: str,
    session_id: str,
    claimant_id: str,
    subject_head_oid: str,
    base_oid: str,
    dependency_base: Mapping[str, Any] | None = None,
    subject_base_oid: str | None = None,
    claim_set: Mapping[str, Any],
    ttl_seconds: float,
    pid: int | None = None,
    host: str | None = None,
    now: float | None = None,
) -> CampaignWorktreeLaunch:
    """Lease and provision one exact clean worktree; do not launch a provider process."""

    campaign = _token(campaign_id, "campaign_id")
    run = _token(run_id, "run_id")
    authority = _sha256(controller_authority_sha256, "controller_authority_sha256")
    work_unit = _token(work_unit_id, "work_unit_id")
    session = _token(session_id, "session_id")
    claimant = _token(claimant_id, "claimant_id")
    subject_head = _oid(subject_head_oid, "subject_head_oid")
    base = _oid(base_oid, "base_oid")
    sealed_base = _oid(subject_base_oid or base_oid, "subject_base_oid")
    if dependency_base is None and sealed_base != base:
        raise CampaignLauncherError(
            "subject_base_oid differs from base_oid without a selected dependency base"
        )
    ttl_seconds = _launch_ttl(ttl_seconds)
    lease_clock = _lease_clock(now=now)
    try:
        inventory = inventory_worktrees(subject=subject, common_dir=common_dir)
    except WorktreeInventoryError as exc:
        raise CampaignLauncherError(f"cannot establish repository inventory: {exc}") from exc
    subject_path = inventory.subject
    common_path = inventory.common_dir
    common_sha256 = inventory.repository_common_dir_sha256
    subject_seal = _seal_git_directory(subject_path, "subject worktree")
    common_seal = _seal_git_directory(common_path, "Git common directory")
    try:
        subject_git_dir = Path(
            _git_text(subject_path, "rev-parse", "--path-format=absolute", "--git-dir")
        ).resolve(strict=True)
    except OSError as exc:
        raise CampaignLauncherError("subject Git admin directory is unavailable") from exc
    security_seals = _repository_security_seals(subject_git_dir, common_path)
    if dependency_base is not None:
        if subject_base_oid is None:
            raise CampaignLauncherError(
                "selected dependency base requires the sealed subject base identity"
            )
        try:
            selected_dependency = validate_dependency_base(
                dependency_base,
                repository_common_dir_sha256=common_sha256,
            )
            if selected_dependency["base_oid"] != base:
                raise CampaignLauncherError(
                    "selected dependency base does not match the explicit launch base"
                )
            verify_dependency_base(
                subject_path,
                selected_dependency,
                original_base_oid=sealed_base,
            )
        except CampaignLauncherError:
            raise
        except Exception as exc:
            raise CampaignLauncherError(
                f"selected dependency base cannot be verified: {exc}"
            ) from exc
    _require_base_identity(subject_path, subject_head, sealed_base, inventory)
    _check_git_seal(subject_seal, "subject worktree")
    _check_git_seal(common_seal, "Git common directory")
    _check_security_seals(security_seals)
    _reject_checkout_commands(subject_path, common_path, base)
    _require_clean_base(subject_path, subject_head, sealed_base, inventory)
    _check_git_seal(subject_seal, "subject worktree")
    _check_git_seal(common_seal, "Git common directory")
    _check_security_seals(security_seals)

    state_seal = _seal_owned_directory(state_root, "launcher state root")
    try:
        writable_state = assert_writable(state_seal.path)
    except WriteBoundaryError as exc:
        raise CampaignLauncherError(str(exc)) from exc
    allowed_state_roots = (
        REPO_ROOT.resolve(), ARTIFACT_ROOT.resolve(), Path(tempfile.gettempdir()).resolve()
    )
    if not any(
        writable_state == allowed or allowed in writable_state.parents
        for allowed in allowed_state_roots
    ):
        raise CampaignLauncherError(
            "launcher state root must be inside Bear Hug, its artifact root or system temp"
        )
    if (
        writable_state == subject_path
        or subject_path in writable_state.parents
        or writable_state in subject_path.parents
    ):
        raise CampaignLauncherError("launcher state root and subject must be disjoint")

    parent_seal = _seal_owned_directory(worktree_parent, "worktree parent")
    try:
        assert_writable(parent_seal.path)
    except WriteBoundaryError as exc:
        raise CampaignLauncherError(str(exc)) from exc
    if parent_seal.path == subject_path or subject_path in parent_seal.path.parents:
        raise CampaignLauncherError("worktree parent must be outside the subject")
    if parent_seal.path == common_path or common_path in parent_seal.path.parents:
        raise CampaignLauncherError("worktree parent must be outside the Git common directory")
    lease_subject = subject_path
    if lease_subject_root is not None:
        lease_subject = Path(lease_subject_root).resolve(strict=True)
        main_roots = [entry.path for entry in inventory.entries if entry.kind == "main"]
        if main_roots != [lease_subject]:
            raise CampaignLauncherError(
                "shared lease anchor is not this repository's main worktree"
            )
    if lease_store.subject_root != lease_subject:
        raise CampaignLauncherError("lease store is bound to a different subject")
    if lease_store.repository_common_dir_sha256 != common_sha256:
        raise CampaignLauncherError("lease store is bound to a different Git common directory")

    target = derive_campaign_worktree_target(
        worktree_parent=parent_seal.path,
        campaign_id=campaign,
        run_id=run,
        controller_authority_sha256=authority,
        work_unit_id=work_unit,
        session_id=session,
        claimant_id=claimant,
        repository_common_dir_sha256=common_sha256,
        base_oid=base,
    )
    if os.path.lexists(target.path):
        raise CampaignLauncherError("derived worktree destination already exists")
    if any(item.path == target.path or item.branch == target.branch for item in inventory.entries):
        raise CampaignLauncherError("derived worktree identity already exists in Git inventory")
    if _branch_oid(subject_path, common_path, target.branch) is not None:
        raise CampaignLauncherError("derived worktree branch already exists")

    lease = lease_store.acquire(
        campaign_id=campaign,
        run_id=run,
        session_id=session,
        claimant_id=claimant,
        controller_authority_sha256=authority,
        repository_common_dir_sha256=common_sha256,
        worktree_sha256=target.worktree_sha256,
        branch=target.branch,
        base_oid=base,
        claim_set=claim_set,
        ttl_seconds=ttl_seconds,
        pid=pid,
        host=host,
        now=lease_clock(),
    )
    destination_identity: tuple[int, int] | None = None
    destination_mutation_attempted = False
    branch_mutation_attempted = False
    branch_creation_confirmed = False
    worktree_attempted = False
    worktree_creation_confirmed = False
    branch_ref_seals: tuple[_SecurityPathSeal, ...] = ()
    admin_seals: tuple[_SecurityPathSeal, ...] = ()
    registration_published = False
    try:
        _refresh_lease(
            lease_store,
            lease.identity,
            ttl_seconds=ttl_seconds,
            now=lease_clock(),
        )
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_seal(parent_seal, "worktree parent")
        destination_mutation_attempted = True
        target.path.mkdir(mode=0o700)
        created = target.path.stat(follow_symlinks=False)
        destination_identity = created.st_dev, created.st_ino
        _refresh_lease(
            lease_store,
            lease.identity,
            ttl_seconds=ttl_seconds,
            now=lease_clock(),
        )
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_seal(parent_seal, "worktree parent")
        _reject_checkout_commands(subject_path, common_path, base)
        current_inventory = inventory_worktrees(subject=subject_path, common_dir=common_path)
        _require_clean_base(subject_path, subject_head, sealed_base, current_inventory)
        if _branch_oid(subject_path, common_path, target.branch) is not None:
            raise CampaignLauncherError("derived worktree branch appeared before creation")
        branch_mutation_attempted = True
        _git_mutation_with_lease(
            subject_path,
            f"--git-dir={common_path}",
            "update-ref",
            f"refs/heads/{target.branch}",
            base,
            "0" * len(base),
            lease_store=lease_store,
            lease_identity=lease.identity,
            ttl_seconds=ttl_seconds,
            clock=lease_clock,
        )
        branch_creation_confirmed = True
        branch_ref_seals = (_seal_security_path(common_path / "refs" / "heads" / target.branch),)
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_security_seals(branch_ref_seals)
        _refresh_lease(
            lease_store,
            lease.identity,
            ttl_seconds=ttl_seconds,
            now=lease_clock(),
        )
        _reject_checkout_commands(subject_path, common_path, base)
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_security_seals(branch_ref_seals)
        _check_seal(parent_seal, "worktree parent")
        worktree_attempted = True
        _git_mutation_with_lease(
            subject_path,
            f"--git-dir={common_path}",
            "worktree",
            "add",
            str(target.path),
            target.branch,
            lease_store=lease_store,
            lease_identity=lease.identity,
            ttl_seconds=ttl_seconds,
            clock=lease_clock,
        )
        worktree_creation_confirmed = True
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_security_seals(branch_ref_seals)
        _verify_created_worktree(
            subject=subject_path,
            common_dir=common_path,
            destination=target.path,
            destination_identity=destination_identity,
            branch=target.branch,
            base_oid=base,
        )
        admin_seals = _worktree_admin_seals(target.path, common_path)
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_security_seals(branch_ref_seals)
        _check_security_seals(admin_seals)
        registration = WorktreeRegistration(common_sha256, target.worktree_sha256, target.branch)
        registered_inventory = inventory_worktrees(
            subject=subject_path,
            common_dir=common_path,
            registry=(registration,),
        )
        registered = next(
            (item for item in registered_inventory.entries if item.path == target.path), None
        )
        if registered is None or registered.custody != "bearhug-owned":
            raise CampaignLauncherError("created worktree did not enter registered custody")

        content_sha256, evidence_bytes = _registration_bytes(
            lease=lease.identity,
            registration=registration,
            worktree=target.path,
            work_unit_id=work_unit,
        )
        _refresh_lease(
            lease_store,
            lease.identity,
            ttl_seconds=ttl_seconds,
            now=lease_clock(),
        )
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_security_seals(branch_ref_seals)
        _check_security_seals(admin_seals)
        _verify_created_worktree(
            subject=subject_path,
            common_dir=common_path,
            destination=target.path,
            destination_identity=destination_identity,
            branch=target.branch,
            base_oid=base,
        )
        _check_seal(state_seal, "launcher state root")
        evidence_directory = state_seal.path / "worktree-registrations"
        evidence_directory.mkdir(mode=0o700, exist_ok=True)
        evidence_seal = _seal_owned_directory(evidence_directory, "registration directory")
        registration_path = evidence_directory / f"{content_sha256}.json"
        _refresh_lease(
            lease_store,
            lease.identity,
            ttl_seconds=ttl_seconds,
            now=lease_clock(),
        )
        _publish_create_only(registration_path, evidence_bytes)
        registration_published = True
        _check_seal(evidence_seal, "registration directory")
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_security_seals(branch_ref_seals)
        _check_security_seals(admin_seals)
        if not worktree_creation_confirmed:
            raise CampaignLauncherError("worktree creation was not synchronously confirmed")
        _verify_created_worktree(
            subject=subject_path,
            common_dir=common_path,
            destination=target.path,
            destination_identity=destination_identity,
            branch=target.branch,
            base_oid=base,
        )
        final_inventory = inventory_worktrees(
            subject=subject_path,
            common_dir=common_path,
            registry=(registration,),
        )
        final_entry = next(
            (item for item in final_inventory.entries if item.path == target.path), None
        )
        if final_entry is None or final_entry.custody != "bearhug-owned":
            raise CampaignLauncherError("published worktree custody drifted before return")
        try:
            if registration_path.read_bytes() != evidence_bytes:
                raise CampaignLauncherError("published custody evidence changed before return")
        except OSError as evidence_exc:
            raise CampaignLauncherError(
                "published custody evidence is unavailable"
            ) from evidence_exc
        _check_git_seal(subject_seal, "subject worktree")
        _check_git_seal(common_seal, "Git common directory")
        _check_security_seals(security_seals)
        _check_security_seals(branch_ref_seals)
        _check_security_seals(admin_seals)
        _refresh_lease(
            lease_store,
            lease.identity,
            ttl_seconds=ttl_seconds,
            now=lease_clock(),
        )
        return CampaignWorktreeLaunch(
            lease.identity,
            registration,
            content_sha256,
            registration_path,
            target.path,
            target.branch,
            base,
        )
    except BaseException as exc:
        # A directory-fsync or temp-cleanup error can occur after the create-only link is visible.
        # Once any registration target exists, preserve the worktree and lease rather than risk
        # leaving durable custody evidence for an object we rolled back.
        if isinstance(exc, CampaignLauncherOrphanedError):
            raise
        if "registration_path" in locals() and os.path.lexists(registration_path):
            registration_published = True
        if registration_published:
            raise CampaignLauncherOrphanedError(
                "provisioning failed after custody publication; lease and evidence remain",
                lease_identity=lease.identity,
            ) from exc
        cleaned = _remove_proven_artifacts(
            subject=subject_path,
            common_dir=common_path,
            subject_seal=subject_seal,
            common_seal=common_seal,
            security_seals=security_seals,
            parent_seal=parent_seal,
            lease_store=lease_store,
            lease_identity=lease.identity,
            ttl_seconds=ttl_seconds,
            clock=lease_clock,
            destination=target.path,
            destination_identity=destination_identity,
            destination_mutation_attempted=destination_mutation_attempted,
            branch=target.branch,
            base_oid=base,
            branch_mutation_attempted=branch_mutation_attempted,
            branch_creation_confirmed=branch_creation_confirmed,
            worktree_attempted=worktree_attempted,
        )
        if cleaned:
            if not _artifacts_proved_absent(
                subject=subject_path,
                common_dir=common_path,
                subject_seal=subject_seal,
                common_seal=common_seal,
                security_seals=security_seals,
                parent_seal=parent_seal,
                destination=target.path,
                branch=target.branch,
            ):
                raise CampaignLauncherOrphanedError(
                    "Git rollback lost its final custody proof; lease remains",
                    lease_identity=lease.identity,
                ) from exc
            try:
                lease_store.release(
                    lease.identity,
                    reason="provisioning-failed",
                    now=lease_clock(),
                )
            except BaseException as release_exc:
                raise CampaignLauncherOrphanedError(
                    "Git rollback succeeded but lease release failed; lease remains",
                    lease_identity=lease.identity,
                ) from release_exc
            if isinstance(exc, CampaignLauncherError):
                raise
            if isinstance(exc, Exception):
                raise CampaignLauncherError(
                    f"worktree provisioning failed: {type(exc).__name__}"
                ) from exc
            raise
        raise CampaignLauncherOrphanedError(
            "provisioning failed and safe rollback could not be proved; lease remains",
            lease_identity=lease.identity,
        ) from exc


__all__ = [
    "CampaignLauncherError",
    "CampaignLauncherOrphanedError",
    "CampaignWorktreeLaunch",
    "CampaignWorktreeTarget",
    "derive_campaign_worktree_target",
    "provision_campaign_worktree",
]
