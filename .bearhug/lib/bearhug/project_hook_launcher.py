"""Secure project resolution shared by Claude and Codex hook launchers.

Provider hook payloads and ambient environment variables are not repository authority.  A native
compiler supplies exactly one installation root; this module derives its Git worktree and common
directory, derives the same identity from the process cwd, and requires both identities to agree.

The installed protected Codex wrapper uses this launcher in production: it reads the retained,
verified runtime descriptor, binds the physical repository identity to external owner-only host
custody, and checks the supported workspace/read-only sandbox declaration. Event adaptation and
native result rendering remain in the wrapper/runtime rather than in this shared identity layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import stat
import subprocess
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ProjectHookLauncherError(RuntimeError):
    """The hook process cannot be bound to one installed repository safely."""


@dataclass(frozen=True, slots=True)
class HookProject:
    """Physical repository identity selected for one provider hook invocation."""

    provider: str
    requested_cwd: str
    repository_root: str
    git_common_dir: str
    repository_device: int
    repository_inode: int
    common_dir_device: int
    common_dir_inode: int


@dataclass(frozen=True, slots=True)
class HookEntrypoint:
    """A regular, singly linked repository file observed through no-follow traversal."""

    relative_path: str
    absolute_path: str
    sha256: str
    byte_count: int
    device: int
    inode: int
    mode: int


@dataclass(frozen=True, slots=True)
class PreparedHookLaunch:
    """Provider-neutral inputs which CP07 may pass to the common dispatcher.

    ``entrypoint.absolute_path`` is diagnostic metadata, not an execution-safe pathname.  The
    execution boundary reopens the relative path with no-follow traversal, compares the resulting
    descriptor to every sealed entrypoint field, and yields that retained descriptor.  Callers must
    execute/read the descriptor itself; reopening the returned path would reintroduce a check/use
    race.
    """

    project: HookProject
    entrypoint: HookEntrypoint
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _DirectorySeal:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _GitLocation:
    worktree: _DirectorySeal
    common_dir: _DirectorySeal


_PROVIDERS = frozenset({"claude", "codex"})
_MAX_ENTRYPOINT_BYTES = 16 * 1024 * 1024
_MAX_FORWARDED_ENVIRONMENT_NAMES = 64
_MAX_ENVIRONMENT_VALUE_BYTES = 64 * 1024
_MAX_LAUNCH_ENVIRONMENT_BYTES = 256 * 1024
_GIT_TIMEOUT_SECONDS = 5
_ENVIRONMENT_NAME = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_EXACTLY_STRIPPED_ENVIRONMENT = frozenset(
    {
        "BASH_ENV",
        "CDPATH",
        "CLAUDE_PROJECT_DIR",
        "ENV",
        "PYTHONHOME",
        "PYTHONPATH",
    }
)
_INJECTION_ENVIRONMENT_PREFIXES = (
    "BEARHUG_",
    "DYLD_",
    "GIT_",
    "LD_",
)
_INJECTION_ENVIRONMENT_NAMES = frozenset(
    {
        "NODE_OPTIONS",
        "PERL5LIB",
        "PERL5OPT",
        "RUBYLIB",
        "RUBYOPT",
    }
)

# PATH is intentionally the platform's compiled default, not the caller's environment.  CP13 may
# replace this with an installer-sealed absolute executable, but ambient PATH never selects the Git
# binary whose answers become repository authority.
_DEFAULT_GIT_SEARCH_PATH = os.defpath


def _canonical_absolute(value: Path | str, where: str) -> Path:
    raw = os.fspath(Path(value).expanduser())
    if not raw or "\0" in raw or not os.path.isabs(raw):
        raise ProjectHookLauncherError(f"{where} must be an explicit absolute path")
    # Do not call resolve(): that would conceal a symlink in a path supplied as authority.
    components = raw.split(os.sep)
    if any(component in {"", ".", ".."} for component in components[1:]):
        raise ProjectHookLauncherError(f"{where} must be a canonical absolute path")
    return Path(raw)


def _open_directory_no_follow(path: Path, where: str) -> _DirectorySeal:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.sep, flags)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise ProjectHookLauncherError(
                    f"{where} contains a missing, symlinked, or non-directory component"
                ) from exc
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ProjectHookLauncherError(f"{where} is not a directory")
        return _DirectorySeal(path, metadata.st_dev, metadata.st_ino)
    finally:
        os.close(descriptor)


def _safe_directory(value: Path | str, where: str) -> _DirectorySeal:
    return _open_directory_no_follow(_canonical_absolute(value, where), where)


def _same_directory(left: _DirectorySeal, right: _DirectorySeal) -> bool:
    return (left.device, left.inode) == (right.device, right.inode)


def _git_environment() -> dict[str, str]:
    # Git identity must come from -C and the repository itself.  Do not inherit dynamic-loader,
    # runtime, config, locale, or executable-selection state from the provider process.
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": _DEFAULT_GIT_SEARCH_PATH,
    }


def _git_executable() -> str:
    executable = next(
        (
            Path(directory) / "git"
            for directory in _DEFAULT_GIT_SEARCH_PATH.split(os.pathsep)
            if directory and (Path(directory) / "git").exists()
        ),
        None,
    )
    if executable is None or not executable.is_absolute():
        raise ProjectHookLauncherError("git is required to resolve the installed repository")
    _safe_directory(executable.parent, "fixed Git executable directory")
    try:
        metadata = executable.lstat()
    except OSError as exc:
        raise ProjectHookLauncherError("cannot inspect the fixed Git executable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not metadata.st_mode & 0o111
    ):
        raise ProjectHookLauncherError("the fixed Git executable is not a regular executable")
    return executable.as_posix()


def _git_text(directory: Path, *arguments: str) -> str:
    environment = _git_environment()
    try:
        completed = subprocess.run(
            [_git_executable(), "-C", os.fspath(directory), *arguments],
            cwd=os.sep,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectHookLauncherError(f"cannot inspect Git identity for {directory}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        raise ProjectHookLauncherError(f"cannot inspect Git identity for {directory}{suffix}")
    value = completed.stdout.strip()
    if not value or "\n" in value or "\r" in value:
        raise ProjectHookLauncherError(f"Git returned an ambiguous identity for {directory}")
    return value


def _git_location(directory: _DirectorySeal, where: str) -> _GitLocation:
    inside = _git_text(directory.path, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        raise ProjectHookLauncherError(f"{where} is not inside a Git worktree")
    worktree_raw = _git_text(
        directory.path, "rev-parse", "--path-format=absolute", "--show-toplevel"
    )
    common_raw = _git_text(
        directory.path, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    worktree = _safe_directory(worktree_raw, f"{where} Git worktree")
    common_dir = _safe_directory(common_raw, f"{where} Git common directory")
    _assert_git_marker(worktree, where)
    return _GitLocation(worktree, common_dir)


def _assert_git_marker(worktree: _DirectorySeal, where: str) -> None:
    """Require the worktree's .git marker itself to be a no-follow directory or single file."""

    root_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    root_flags |= getattr(os, "O_NOFOLLOW", 0)
    marker_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = os.open(worktree.path, root_flags)
    try:
        try:
            marker_descriptor = os.open(".git", marker_flags, dir_fd=root_descriptor)
        except OSError as exc:
            raise ProjectHookLauncherError(
                f"{where} Git marker is missing, symlinked, or unreadable"
            ) from exc
        try:
            metadata = os.fstat(marker_descriptor)
            valid_directory = stat.S_ISDIR(metadata.st_mode)
            valid_file = stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
            if not valid_directory and not valid_file:
                raise ProjectHookLauncherError(
                    f"{where} Git marker is not a regular single-link file or directory"
                )
        finally:
            os.close(marker_descriptor)
    finally:
        os.close(root_descriptor)


def _single_installation_root(
    installation_roots: Iterable[Path | str],
) -> _DirectorySeal:
    if isinstance(installation_roots, (str, bytes, Path)):
        raise ProjectHookLauncherError(
            "installation identity must be an array containing exactly one repository root"
        )
    declared = list(installation_roots)
    if len(declared) == 0:
        raise ProjectHookLauncherError("installed repository identity is missing")
    if len(declared) != 1:
        raise ProjectHookLauncherError("installed repository identity is ambiguous")
    return _safe_directory(declared[0], "installed repository root")


def resolve_hook_project(
    *,
    provider: str,
    installation_roots: Iterable[Path | str],
    cwd: Path | str | None = None,
) -> HookProject:
    """Bind a hook invocation to exactly one installed Claude or Codex worktree.

    ``installation_roots`` is plural only so missing and ambiguous installer output can be rejected
    explicitly.  A conforming native wrapper always embeds a one-element declaration.  Neither
    provider payload fields nor ``CLAUDE_PROJECT_DIR`` participate in this decision.
    """

    if provider not in _PROVIDERS:
        raise ProjectHookLauncherError(f"unsupported hook provider: {provider!r}")
    installed = _single_installation_root(installation_roots)
    requested = _safe_directory(Path.cwd() if cwd is None else cwd, "hook cwd")

    installed_git = _git_location(installed, "installed repository root")
    if not _same_directory(installed, installed_git.worktree):
        raise ProjectHookLauncherError(
            "installed repository root is not the root of its Git worktree"
        )
    requested_git = _git_location(requested, "hook cwd")
    if not _same_directory(installed_git.worktree, requested_git.worktree):
        raise ProjectHookLauncherError("hook cwd is outside the installed repository worktree")
    if not _same_directory(installed_git.common_dir, requested_git.common_dir):
        raise ProjectHookLauncherError("hook cwd has a different Git common directory")

    # Re-open the caller-controlled directories after Git inspection to detect replacement during
    # resolution.  This cannot make arbitrary path-based execution race-free; CP13 must install
    # sealed bytes transactionally.  It does keep a changed cwd/root from becoming authority here.
    if not _same_directory(installed, _safe_directory(installed.path, "installed repository root")):
        raise ProjectHookLauncherError("installed repository root changed during resolution")
    if not _same_directory(requested, _safe_directory(requested.path, "hook cwd")):
        raise ProjectHookLauncherError("hook cwd changed during resolution")
    if not _same_directory(
        installed_git.common_dir,
        _safe_directory(installed_git.common_dir.path, "installed Git common directory"),
    ):
        raise ProjectHookLauncherError("installed Git common directory changed during resolution")

    return HookProject(
        provider=provider,
        requested_cwd=requested.path.as_posix(),
        repository_root=installed_git.worktree.path.as_posix(),
        git_common_dir=installed_git.common_dir.path.as_posix(),
        repository_device=installed_git.worktree.device,
        repository_inode=installed_git.worktree.inode,
        common_dir_device=installed_git.common_dir.device,
        common_dir_inode=installed_git.common_dir.inode,
    )


def _relative_parts(value: str) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
    ):
        raise ProjectHookLauncherError(
            "hook entrypoint must be a canonical repository-relative path"
        )
    path = PurePosixPath(value)
    if str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ProjectHookLauncherError(
            "hook entrypoint must be a canonical repository-relative path"
        )
    return path.parts


def inspect_hook_entrypoint(project: HookProject, relative_path: str) -> HookEntrypoint:
    """Read and identify one handler without following repository symlinks or hard links."""

    parts = _relative_parts(relative_path)
    root = _safe_directory(project.repository_root, "resolved repository root")
    if (root.device, root.inode) != (project.repository_device, project.repository_inode):
        raise ProjectHookLauncherError("resolved repository root changed before handler inspection")
    common_dir = _safe_directory(project.git_common_dir, "resolved Git common directory")
    if (common_dir.device, common_dir.inode) != (
        project.common_dir_device,
        project.common_dir_inode,
    ):
        raise ProjectHookLauncherError(
            "resolved Git common directory changed before handler inspection"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(root.path, directory_flags)
    try:
        for component in parts[:-1]:
            try:
                child = os.open(component, directory_flags, dir_fd=descriptor)
            except OSError as exc:
                raise ProjectHookLauncherError(
                    "hook entrypoint traverses a missing, symlinked, or non-directory component"
                ) from exc
            os.close(descriptor)
            descriptor = child
        try:
            file_descriptor = os.open(parts[-1], flags, dir_fd=descriptor)
        except OSError as exc:
            raise ProjectHookLauncherError(
                "hook entrypoint is missing, symlinked, or unreadable"
            ) from exc
        try:
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ProjectHookLauncherError(
                    "hook entrypoint must be a regular file with exactly one link"
                )
            if before.st_size > _MAX_ENTRYPOINT_BYTES:
                raise ProjectHookLauncherError("hook entrypoint exceeds the byte limit")
            digest = hashlib.sha256()
            byte_count = 0
            while True:
                block = os.read(file_descriptor, 64 * 1024)
                if not block:
                    break
                byte_count += len(block)
                if byte_count > _MAX_ENTRYPOINT_BYTES:
                    raise ProjectHookLauncherError("hook entrypoint exceeds the byte limit")
                digest.update(block)
            after = os.fstat(file_descriptor)
            stable = (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) == (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if not stable or byte_count != after.st_size:
                raise ProjectHookLauncherError("hook entrypoint changed while it was inspected")
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)

    return HookEntrypoint(
        relative_path=relative_path,
        absolute_path=(root.path / PurePosixPath(relative_path)).as_posix(),
        sha256=digest.hexdigest(),
        byte_count=byte_count,
        device=before.st_dev,
        inode=before.st_ino,
        mode=before.st_mode,
    )


def _open_entrypoint_descriptor(project: HookProject, relative_path: str) -> int:
    parts = _relative_parts(relative_path)
    root = _safe_directory(project.repository_root, "resolved repository root")
    if (root.device, root.inode) != (project.repository_device, project.repository_inode):
        raise ProjectHookLauncherError("resolved repository root changed before handler opening")
    common_dir = _safe_directory(project.git_common_dir, "resolved Git common directory")
    if (common_dir.device, common_dir.inode) != (
        project.common_dir_device,
        project.common_dir_inode,
    ):
        raise ProjectHookLauncherError(
            "resolved Git common directory changed before handler opening"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(root.path, directory_flags)
    try:
        for component in parts[:-1]:
            try:
                child = os.open(component, directory_flags, dir_fd=descriptor)
            except OSError as exc:
                raise ProjectHookLauncherError(
                    "hook entrypoint traverses a missing, symlinked, or non-directory component"
                ) from exc
            os.close(descriptor)
            descriptor = child
        try:
            return os.open(parts[-1], flags, dir_fd=descriptor)
        except OSError as exc:
            raise ProjectHookLauncherError(
                "hook entrypoint is missing, symlinked, or unreadable"
            ) from exc
    finally:
        os.close(descriptor)


@contextmanager
def open_hook_entrypoint(project: HookProject, entrypoint: HookEntrypoint) -> Iterator[int]:
    """Reopen and retain a previously inspected hook entrypoint without following its path."""

    if not isinstance(project, HookProject) or not isinstance(entrypoint, HookEntrypoint):
        raise ProjectHookLauncherError("hook project or entrypoint has the wrong type")
    expected_absolute = (
        Path(project.repository_root) / PurePosixPath(entrypoint.relative_path)
    ).as_posix()
    if entrypoint.absolute_path != expected_absolute:
        raise ProjectHookLauncherError("hook entrypoint absolute path is inconsistent")
    if (
        not isinstance(entrypoint.sha256, str)
        or len(entrypoint.sha256) != 64
        or any(character not in "0123456789abcdef" for character in entrypoint.sha256)
        or type(entrypoint.byte_count) is not int
        or entrypoint.byte_count < 0
    ):
        raise ProjectHookLauncherError("hook entrypoint identity is malformed")

    descriptor = _open_entrypoint_descriptor(project, entrypoint.relative_path)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_dev != entrypoint.device
            or before.st_ino != entrypoint.inode
            or before.st_mode != entrypoint.mode
            or before.st_size != entrypoint.byte_count
        ):
            raise ProjectHookLauncherError("hook entrypoint changed before execution")
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            block = os.read(descriptor, 64 * 1024)
            if not block:
                break
            byte_count += len(block)
            if byte_count > _MAX_ENTRYPOINT_BYTES:
                raise ProjectHookLauncherError("hook entrypoint exceeds the byte limit")
            digest.update(block)
        after_read = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after_read.st_dev,
            after_read.st_ino,
            after_read.st_mode,
            after_read.st_nlink,
            after_read.st_size,
            after_read.st_mtime_ns,
            after_read.st_ctime_ns,
        ) or byte_count != entrypoint.byte_count or digest.hexdigest() != entrypoint.sha256:
            raise ProjectHookLauncherError("hook entrypoint changed or digest mismatched")
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield descriptor
    finally:
        os.close(descriptor)


def _argument(value: object, where: str) -> str:
    if not isinstance(value, str) or "\0" in value or len(value.encode("utf-8")) > 64 * 1024:
        raise ProjectHookLauncherError(f"{where} must be a bounded string without NUL")
    return value


def _launch_environment(
    project: HookProject,
    base_environment: Mapping[str, str] | None,
    forward_environment_names: Iterable[str],
) -> tuple[tuple[str, str], ...]:
    source = os.environ if base_environment is None else base_environment
    if isinstance(forward_environment_names, (str, bytes)):
        raise ProjectHookLauncherError("forwarded environment names must be an array")
    forwarded = list(forward_environment_names)
    if len(forwarded) > _MAX_FORWARDED_ENVIRONMENT_NAMES:
        raise ProjectHookLauncherError("too many forwarded environment names")
    if forwarded != sorted(set(forwarded)):
        raise ProjectHookLauncherError(
            "forwarded environment names must be sorted and contain no duplicates"
        )
    for name in forwarded:
        if (
            not isinstance(name, str)
            or not name
            or name[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
            or len(name) > 128
            or any(character not in _ENVIRONMENT_NAME for character in name)
        ):
            raise ProjectHookLauncherError("forwarded environment name is invalid")
        if (
            name in _EXACTLY_STRIPPED_ENVIRONMENT
            or name in _INJECTION_ENVIRONMENT_NAMES
            or name == "PATH"
            or name.startswith(_INJECTION_ENVIRONMENT_PREFIXES)
        ):
            raise ProjectHookLauncherError(
                f"refusing to forward security-sensitive environment name {name}"
            )

    environment: dict[str, str] = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": _DEFAULT_GIT_SEARCH_PATH,
    }
    for key, value in source.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or "\0" in key
            or "=" in key
            or "\0" in value
        ):
            raise ProjectHookLauncherError("launch environment contains an invalid entry")
        if key not in forwarded:
            continue
        environment[key] = value
    environment.update(
        {
            "BEARHUG_GIT_COMMON_DIR": project.git_common_dir,
            "BEARHUG_PROJECT_ROOT": project.repository_root,
            "BEARHUG_PROVIDER": project.provider,
        }
    )
    total_bytes = 0
    for key, value in environment.items():
        try:
            key_bytes = key.encode("utf-8")
            value_bytes = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProjectHookLauncherError("launch environment contains non-UTF-8 text") from exc
        if len(value_bytes) > _MAX_ENVIRONMENT_VALUE_BYTES:
            raise ProjectHookLauncherError(f"launch environment value {key} exceeds the byte limit")
        total_bytes += len(key_bytes) + len(value_bytes) + 2
    if total_bytes > _MAX_LAUNCH_ENVIRONMENT_BYTES:
        raise ProjectHookLauncherError("launch environment exceeds the total byte limit")
    return tuple(sorted(environment.items()))


def prepare_common_hook_launch(
    *,
    provider: str,
    installation_roots: Iterable[Path | str],
    entrypoint: str,
    arguments: Sequence[str] = (),
    cwd: Path | str | None = None,
    base_environment: Mapping[str, str] | None = None,
    forward_environment_names: Iterable[str] = (),
) -> PreparedHookLaunch:
    """Prepare provider-neutral launch inputs after binding cwd to the installed worktree."""

    if isinstance(arguments, (str, bytes)) or not isinstance(arguments, Sequence):
        raise ProjectHookLauncherError("hook arguments must be an array")
    project = resolve_hook_project(
        provider=provider,
        installation_roots=installation_roots,
        cwd=cwd,
    )
    inspected_entrypoint = inspect_hook_entrypoint(project, entrypoint)
    checked_arguments = tuple(
        _argument(value, f"hook arguments[{index}]") for index, value in enumerate(arguments)
    )
    return PreparedHookLaunch(
        project=project,
        entrypoint=inspected_entrypoint,
        arguments=checked_arguments,
        environment=_launch_environment(
            project,
            base_environment,
            forward_environment_names,
        ),
    )


def _hook_authority_base() -> Path:
    """Use the OS account, never provider-controlled HOME/XDG/BEARHUG environment values."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".local" / "state" / "bearhug" / "direct-hooks"


def hook_authority_directory(project: HookProject, *, create: bool = True) -> Path:
    """Derive one private host-side authority from the sealed physical installation identity.

    This directory is deliberately outside the project and ordinary workspace/tmp write roots.
    A provider can read the hook program but cannot acquire its durable write custody from an
    ordinary workspace-write/read-only tool. No payload field or environment override selects it.
    """
    root = _safe_directory(project.repository_root, "authority repository root")
    common = _safe_directory(project.git_common_dir, "authority Git common directory")
    if (root.device, root.inode, common.device, common.inode) != (
        project.repository_device, project.repository_inode,
        project.common_dir_device, project.common_dir_inode,
    ):
        raise ProjectHookLauncherError("hook repository identity changed before state custody")
    identity = {
        "provider": project.provider,
        "root": project.repository_root,
        "root_device": root.device,
        "root_inode": root.inode,
        "common": project.git_common_dir,
        "common_device": common.device,
        "common_inode": common.inode,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    key = hashlib.sha256(canonical).hexdigest()
    base = _hook_authority_base()
    if base == root.path or root.path in base.parents:
        raise ProjectHookLauncherError("hook authority must remain outside the project")
    directory = base / key
    if create:
        # Check every existing ancestor before mkdir, then retain no-follow physical identities.
        current = Path(os.sep)
        for part in directory.parts[1:]:
            current /= part
            if not current.exists() and not current.is_symlink():
                # A concurrent host callback may have created the same private path.
                with suppress(FileExistsError):
                    current.mkdir(mode=0o700)
            seal = _safe_directory(current, "host hook authority")
            if current == base or base in current.parents:
                metadata = current.stat(follow_symlinks=False)
                if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise ProjectHookLauncherError("host hook authority must be owner-only")
                if (metadata.st_dev, metadata.st_ino) != (seal.device, seal.inode):
                    raise ProjectHookLauncherError("host hook authority changed while opened")
    return directory


def require_direct_hook_sandbox(root: Path, payload: Mapping[str, object]) -> None:
    """Refuse direct profiles outside the qualified native protected-directory contract.

    Project configuration is protected by Codex workspace-write, as are the hook runner and its
    library beneath .codex. Explicit native bypass indicators override the installed declaration.
    Custom permission profiles and full-access modes need separate qualification.
    """
    config = root / ".codex" / "config.toml"
    _safe_directory(config.parent, "protected Codex configuration")
    if config.is_symlink():
        raise ProjectHookLauncherError("Codex configuration may not be a symlink")
    try:
        raw = config.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ProjectHookLauncherError("Codex configuration exceeds its limit")
        value = tomllib.loads(raw.decode())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ProjectHookLauncherError("direct hook sandbox configuration is unavailable") from exc
    if value.get("sandbox_mode") not in {"workspace-write", "read-only"}:
        raise ProjectHookLauncherError(
            "direct hooks require explicit workspace-write or read-only sandbox"
        )
    if value.get("default_permissions") or value.get("permissions"):
        raise ProjectHookLauncherError("custom direct-hook permission profiles are not qualified")
    if payload.get("permission_mode") not in {"default", "acceptEdits", "plan", "dontAsk"}:
        raise ProjectHookLauncherError(
            "native hook permission mode is absent or outside qualification"
        )
    explicit = payload.get("sandbox_mode") or payload.get("sandbox_policy")
    if explicit is not None:
        if isinstance(explicit, Mapping):
            explicit = explicit.get("type")
        if explicit not in {"workspace-write", "read-only", "workspaceWrite", "readOnly"}:
            raise ProjectHookLauncherError("native hook reports an unqualified sandbox")
    workspace = value.get("sandbox_workspace_write", {})
    if not isinstance(workspace, Mapping):
        raise ProjectHookLauncherError("workspace sandbox declaration is malformed")
    writable = workspace.get("writable_roots", [])
    if not isinstance(writable, list):
        raise ProjectHookLauncherError("workspace writable roots must be a list")
    for item in writable:
        if not isinstance(item, str):
            raise ProjectHookLauncherError("workspace writable root is malformed")
        candidate = Path(item).expanduser().resolve()
        authority = _hook_authority_base()
        protected = root / ".codex"
        if (
            candidate in (authority, protected)
            or candidate in authority.parents
            or authority in candidate.parents
            or protected in candidate.parents
        ):
            raise ProjectHookLauncherError("workspace writable roots expose protected hook custody")
