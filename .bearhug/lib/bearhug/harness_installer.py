"""Transactional installation of sealed provider-native harness bytes.

This module proves only that exact manifest-declared bytes were installed in one explicitly bound
Git checkout.  It does not claim that Claude Code or Codex loaded those bytes, trusted their hooks,
projected the source policy faithfully at runtime, or exhibited any effective behavior.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from bearhug.harness_policy_v2 import HarnessPolicyV2
from bearhug.host_git import resolve_excludes_file_override
from bearhug.native_materialization import (
    NativeMaterializationError,
    NativeMaterializationManifest,
    compile_native_materialization_manifest,
)
from bearhug.paths import WriteBoundaryError, assert_writable


class HarnessInstallError(RuntimeError):
    """The bundle, checkout, state, receipt, or transaction cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        rollback_receipt: dict[str, Any] | None = None,
        rollback_receipt_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.rollback_receipt = rollback_receipt
        self.rollback_receipt_path = rollback_receipt_path


class HarnessInstallConflict(HarnessInstallError):
    """The explicit operation does not match the current installed or checkout state."""


@dataclass(frozen=True, slots=True)
class TargetCheckoutIdentity:
    """Exact physical Git checkout identity supplied to an installer operation."""

    root: Path
    common_dir: Path
    repository_root_device: int
    repository_root_inode: int
    repository_common_dir_device: int
    repository_common_dir_inode: int
    repository_root_sha256: str
    repository_common_dir_sha256: str
    head_oid: str
    tree_oid: str
    branch: str


@dataclass(frozen=True, slots=True)
class _BundleFile:
    path: str
    content: bytes
    mode: int
    fingerprint: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SealedNativeMaterializationBundle:
    """Validated manifest plus one complete, immutable-at-use staging tree snapshot."""

    manifest: NativeMaterializationManifest
    source_root: Path
    files: tuple[_BundleFile, ...]

    @classmethod
    def from_directory(
        cls,
        *,
        manifest: NativeMaterializationManifest | Mapping[str, Any],
        policy: HarnessPolicyV2 | Mapping[str, Any],
        source_root: Path | str,
    ) -> SealedNativeMaterializationBundle:
        try:
            compiled = compile_native_materialization_manifest(manifest, policy=policy)
        except NativeMaterializationError as exc:
            raise HarnessInstallError(f"native materialization is invalid: {exc}") from exc
        _validate_provider_paths(compiled.document)
        root = _physical_directory(source_root, "source bundle")
        files = _read_complete_source(root, compiled.document["files"])
        return cls(manifest=compiled, source_root=root, files=files)

    def verify_unchanged(self) -> None:
        """Reject replacement, relinking, mode drift, or an unknown source-tree entry."""

        observed = _read_complete_source(self.source_root, self.manifest.document["files"])
        if observed != self.files:
            raise HarnessInstallError("source bundle changed after it was sealed")


@dataclass(frozen=True, slots=True)
class HarnessInstallResult:
    operation: str
    outcome: str
    receipt: dict[str, Any]
    receipt_path: Path
    installed_manifest: dict[str, Any] | None
    installed_manifest_sha256: str | None


InstallerCheckpoint = Callable[[str, Path | None], None]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_INSTALL_NONCE = re.compile(r"^[0-9a-f]{32,128}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_MODES = {"0600": 0o600, "0644": 0o644, "0755": 0o755}
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_BUNDLE_BYTES = 256 * 1024 * 1024
_PROVIDERS = frozenset({"claude", "codex"})
_ACTIONS = frozenset({"create", "replace", "remove", "unchanged"})
_OUTCOMES = frozenset(
    {
        "planned-install",
        "planned-upgrade",
        "planned-uninstall",
        "planned-unchanged",
        "installed",
        "unchanged",
        "upgraded",
        "uninstalled",
    }
)
_ROLLBACK_OUTCOMES = frozenset({"rolled-back", "rollback-incomplete"})
_STATE_MANIFEST_PATH = "@installer-state/current-manifest"
_INSTALLED_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "provider",
        "native_materialization_sha256",
        "source_policy_sha256",
        "install_nonce",
        "target",
        "files",
        "created_directories",
        "claims",
        "limitations",
        "content_sha256",
    }
)
_TARGET_FIELDS = frozenset(
    {
        "repository_root_sha256",
        "repository_common_dir_sha256",
        "repository_root_device",
        "repository_root_inode",
        "repository_common_dir_device",
        "repository_common_dir_inode",
        "head_oid",
        "tree_oid",
        "branch",
    }
)
_INSTALLED_FILE_FIELDS = frozenset({"path", "mode", "sha256", "bytes"})
_CLAIM_FIELDS = frozenset(
    {
        "installed_bytes_verified",
        "effective_sources_verified",
        "provider_projection_verified",
        "hook_trust_verified",
        "runtime_observed",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "operation",
        "outcome",
        "provider",
        "observed_at",
        "native_materialization_sha256",
        "previous_installed_manifest_sha256",
        "installed_manifest_sha256",
        "target",
        "changes",
        "claims",
        "limitations",
        "content_sha256",
    }
)
_CHANGE_FIELDS = frozenset({"path", "action", "before_sha256", "after_sha256"})
_ROLLBACK_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "operation",
        "outcome",
        "provider",
        "observed_at",
        "error_type",
        "target",
        "rolled_back_paths",
        "unresolved_paths",
        "claims",
        "limitations",
        "content_sha256",
    }
)
_FALSE_RUNTIME_CLAIMS = {
    "effective_sources_verified": False,
    "provider_projection_verified": False,
    "hook_trust_verified": False,
    "runtime_observed": False,
}
_INSTALLED_LIMIT = (
    "Installed bytes only; effective provider sources, hook trust, projection, and runtime "
    "behavior are unverified."
)
_RECEIPT_LIMIT = (
    "This receipt proves a local byte transaction only; provider trust and effective runtime "
    "behavior are unverified."
)
_ROLLBACK_LIMIT = (
    "Rollback covers only installer-proven writes; no provider trust or runtime behavior is "
    "attested."
)


def _canonical(value: Any) -> bytes:
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
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HarnessInstallError(f"installer record is not canonical JSON: {exc}") from exc


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _closed(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise HarnessInstallError(f"{label} has missing or unknown fields")
    return value


def _sha(value: Any, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise HarnessInstallError(f"{label} must be lowercase SHA-256")
    return value


def _path(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or "//" in value
    ):
        raise HarnessInstallError(f"{label} is not a canonical repository-relative path")
    parsed = PurePosixPath(value)
    if str(parsed) != value or any(part in {"", ".", ".."} for part in parsed.parts):
        raise HarnessInstallError(f"{label} is not a canonical repository-relative path")
    return value


def _physical_directory(value: Path | str, label: str) -> Path:
    requested = Path(value).expanduser()
    try:
        if requested.is_symlink():
            raise HarnessInstallError(f"{label} may not be a symlink")
        resolved = requested.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise HarnessInstallError(f"cannot resolve {label}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise HarnessInstallError(f"{label} must be a physical directory")
    return resolved


def _validate_provider_path(provider: str, path: str, *, directory: bool = False) -> None:
    if provider == "claude":
        permitted = path == ".claude" if directory else path == "CLAUDE.md"
        permitted = permitted or path.startswith(".claude/")
    else:
        permitted = path == ".codex" if directory else path == "AGENTS.md"
        permitted = permitted or path.startswith(".codex/")
        if directory:
            permitted = permitted or path == ".agents/skills" or path.startswith(".agents/skills/")
        else:
            permitted = permitted or path.startswith(".agents/skills/")
    if not permitted:
        raise HarnessInstallError(
            f"manifest path {path!r} is not a declared {provider} provider harness path"
        )


def _validate_provider_paths(document: Mapping[str, Any]) -> None:
    provider = document["provider"]
    for row in document["destination_allowlist"]:
        _validate_provider_path(provider, row["path"], directory=row["kind"] == "directory")
    for row in document["files"]:
        _validate_provider_path(provider, row["path"])


def _validate_created_directory(provider: str, path: str) -> None:
    if provider == "codex" and path == ".agents":
        return
    _validate_provider_path(provider, path, directory=True)


def _read_regular(path: Path, expected_bytes: int, label: str) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HarnessInstallError(f"cannot open {label}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != expected_bytes
            or before.st_size > _MAX_FILE_BYTES
        ):
            raise HarnessInstallError(f"{label} is not one bounded regular non-hardlinked file")
        remaining = expected_bytes + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)

        if _stat_fingerprint(before) != _stat_fingerprint(after):
            raise HarnessInstallError(f"{label} changed while it was read")
        content = b"".join(chunks)
        if len(content) != expected_bytes:
            raise HarnessInstallError(f"{label} byte length changed while it was read")
        return content, after
    finally:
        os.close(descriptor)


def _read_complete_source(root: Path, declared: list[dict[str, Any]]) -> tuple[_BundleFile, ...]:
    expected = {row["path"]: row for row in declared}
    expected_directories = {
        str(parent)
        for path in expected
        for parent in PurePosixPath(path).parents
        if str(parent) != "."
    }
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            metadata = child.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise HarnessInstallError(
                    f"source bundle contains a symlink or special entry: {relative}"
                )
            observed_directories.add(relative)
        for name in files:
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            metadata = child.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise HarnessInstallError(
                    f"source bundle contains a symlink or hardlink: {relative}"
                )
            observed_files.add(relative)
    if observed_files != set(expected) or not observed_directories <= expected_directories:
        unknown = sorted(
            (observed_files - set(expected)) | (observed_directories - expected_directories)
        )
        missing = sorted(set(expected) - observed_files)
        raise HarnessInstallError(
            "source bundle complete file set disagrees with manifest; "
            f"unknown={unknown}, missing={missing}"
        )
    result: list[_BundleFile] = []
    total = 0
    for relative, row in sorted(expected.items()):
        content, metadata = _read_regular(root / relative, row["bytes"], f"source {relative}")
        total += len(content)
        if total > _MAX_BUNDLE_BYTES:
            raise HarnessInstallError("source bundle exceeds the installer byte limit")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != _MODES[row["mode"]] or _digest(content) != row["sha256"]:
            raise HarnessInstallError(f"source bundle changed or contradicts manifest: {relative}")
        result.append(
            _BundleFile(
                path=relative,
                content=content,
                mode=mode,
                fingerprint=_stat_fingerprint(metadata),
            )
        )
    return tuple(result)


def _git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }


def _git(root: Path, *arguments: str) -> bytes:
    environment = _git_environment()
    try:
        completed = subprocess.run(
            (
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                *arguments,
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessInstallError(f"cannot inspect target Git checkout: {exc}") from exc
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace").strip()
        raise HarnessInstallError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _git_line(root: Path, label: str, *arguments: str) -> str:
    try:
        value = _git(root, *arguments).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise HarnessInstallError(f"Git returned non-UTF-8 {label}") from exc
    if not value or "\n" in value or "\r" in value:
        raise HarnessInstallError(f"Git returned invalid {label}")
    return value


def inspect_target_checkout(value: Path | str) -> TargetCheckoutIdentity:
    """Inspect one exact physical, named-branch Git worktree without changing it."""

    root = _physical_directory(value, "target checkout")
    top = Path(_git_line(root, "worktree root", "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise HarnessInstallError(f"target must name the exact Git worktree root, observed {top}")
    common_raw = _git_line(
        root,
        "common directory",
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    common_requested = Path(common_raw)
    if common_requested.is_symlink():
        raise HarnessInstallError("target Git common directory may not be a symlink")
    common = common_requested.resolve(strict=True)
    if not common.is_dir():
        raise HarnessInstallError("target Git common directory is not physical")
    head = _git_line(root, "HEAD", "rev-parse", "--verify", "HEAD")
    tree = _git_line(root, "HEAD tree", "rev-parse", "--verify", "HEAD^{tree}")
    branch = _git_line(root, "branch", "symbolic-ref", "--quiet", "--short", "HEAD")
    if _OID.fullmatch(head) is None or _OID.fullmatch(tree) is None:
        raise HarnessInstallError("target Git checkout returned an invalid object identity")
    root_metadata = root.stat()
    common_metadata = common.stat()
    return TargetCheckoutIdentity(
        root=root,
        common_dir=common,
        repository_root_device=root_metadata.st_dev,
        repository_root_inode=root_metadata.st_ino,
        repository_common_dir_device=common_metadata.st_dev,
        repository_common_dir_inode=common_metadata.st_ino,
        repository_root_sha256=_digest(os.fsencode(root)),
        repository_common_dir_sha256=_digest(os.fsencode(common)),
        head_oid=head,
        tree_oid=tree,
        branch=branch,
    )


def _target_record(target: TargetCheckoutIdentity) -> dict[str, Any]:
    return {
        "repository_root_sha256": target.repository_root_sha256,
        "repository_common_dir_sha256": target.repository_common_dir_sha256,
        "repository_root_device": target.repository_root_device,
        "repository_root_inode": target.repository_root_inode,
        "repository_common_dir_device": target.repository_common_dir_device,
        "repository_common_dir_inode": target.repository_common_dir_inode,
        "head_oid": target.head_oid,
        "tree_oid": target.tree_oid,
        "branch": target.branch,
    }


def _validate_target_record(value: Any) -> dict[str, Any]:
    target = _closed(value, _TARGET_FIELDS, "installer target identity")
    _sha(target["repository_root_sha256"], "target.repository_root_sha256")
    _sha(target["repository_common_dir_sha256"], "target.repository_common_dir_sha256")
    for field in (
        "repository_root_device",
        "repository_root_inode",
        "repository_common_dir_device",
        "repository_common_dir_inode",
    ):
        if type(target[field]) is not int or target[field] < 0:
            raise HarnessInstallError(f"target.{field} is invalid")
    if not isinstance(target["head_oid"], str) or _OID.fullmatch(target["head_oid"]) is None:
        raise HarnessInstallError("target.head_oid is invalid")
    if not isinstance(target["tree_oid"], str) or _OID.fullmatch(target["tree_oid"]) is None:
        raise HarnessInstallError("target.tree_oid is invalid")
    if not isinstance(target["branch"], str) or not target["branch"]:
        raise HarnessInstallError("target.branch is invalid")
    return target


def _content_address(value: Mapping[str, Any]) -> str:
    copied = copy.deepcopy(dict(value))
    copied.pop("content_sha256", None)
    return _digest(_canonical(copied))


def _validate_claims(value: Any, *, installed: bool) -> dict[str, Any]:
    claims = _closed(value, _CLAIM_FIELDS, "installer claims")
    expected = {"installed_bytes_verified": installed, **_FALSE_RUNTIME_CLAIMS}
    if claims != expected:
        raise HarnessInstallError("installer record overclaims effective, trust, or runtime proof")
    return claims


def validate_installed_manifest(value: Any) -> dict[str, Any]:
    """Validate the closed current-state manifest that defines installer ownership."""

    manifest = _closed(value, _INSTALLED_FIELDS, "installed manifest")
    if (
        manifest["schema_version"] != "1"
        or manifest["record_kind"] != "provider_harness_installed_manifest"
        or manifest["provider"] not in _PROVIDERS
    ):
        raise HarnessInstallError("installed manifest identity is invalid")
    _sha(manifest["native_materialization_sha256"], "native_materialization_sha256")
    _sha(manifest["source_policy_sha256"], "source_policy_sha256")
    if (
        not isinstance(manifest["install_nonce"], str)
        or _INSTALL_NONCE.fullmatch(manifest["install_nonce"]) is None
    ):
        raise HarnessInstallError("installed manifest install_nonce is invalid")
    _validate_target_record(manifest["target"])
    rows = manifest["files"]
    if not isinstance(rows, list) or not rows:
        raise HarnessInstallError("installed manifest files must be non-empty")
    paths: list[str] = []
    for index, row in enumerate(rows):
        row = _closed(row, _INSTALLED_FILE_FIELDS, f"installed files[{index}]")
        path = _path(row["path"], f"installed files[{index}].path")
        _validate_provider_path(manifest["provider"], path)
        if row["mode"] not in _MODES:
            raise HarnessInstallError(f"installed files[{index}].mode is invalid")
        _sha(row["sha256"], f"installed files[{index}].sha256")
        if type(row["bytes"]) is not int or not 0 <= row["bytes"] <= _MAX_FILE_BYTES:
            raise HarnessInstallError(f"installed files[{index}].bytes is invalid")
        paths.append(path)
    if paths != sorted(set(paths)):
        raise HarnessInstallError("installed manifest paths must be sorted and unique")
    directories = manifest["created_directories"]
    if (
        not isinstance(directories, list)
        or directories != sorted(set(directories))
        or any(not isinstance(item, str) for item in directories)
    ):
        raise HarnessInstallError("installed manifest created_directories is invalid")
    for directory in directories:
        _path(directory, "created directory")
        _validate_created_directory(manifest["provider"], directory)
    _validate_claims(manifest["claims"], installed=True)
    expected_limit = [_INSTALLED_LIMIT]
    if manifest["limitations"] != expected_limit:
        raise HarnessInstallError("installed manifest limitations are not closed")
    if manifest["content_sha256"] != _content_address(manifest):
        raise HarnessInstallError("installed manifest content digest is false")
    return manifest


def validate_install_receipt(value: Any) -> dict[str, Any]:
    """Validate a closed content-addressed dry-run/install/upgrade/uninstall receipt."""

    receipt = _closed(value, _RECEIPT_FIELDS, "install receipt")
    if (
        receipt["schema_version"] != "1"
        or receipt["record_kind"] != "provider_harness_install_receipt"
        or receipt["operation"] not in {"dry-run", "install", "upgrade", "uninstall"}
        or receipt["outcome"] not in _OUTCOMES
        or receipt["provider"] not in _PROVIDERS
    ):
        raise HarnessInstallError("install receipt identity is invalid")
    permitted_outcomes = {
        "dry-run": {
            "planned-install",
            "planned-upgrade",
            "planned-uninstall",
            "planned-unchanged",
        },
        "install": {"installed", "unchanged"},
        "upgrade": {"upgraded"},
        "uninstall": {"uninstalled"},
    }
    if receipt["outcome"] not in permitted_outcomes[receipt["operation"]]:
        raise HarnessInstallError("install receipt operation and outcome disagree")
    _timestamp(receipt["observed_at"], "observed_at")
    _sha(receipt["native_materialization_sha256"], "native_materialization_sha256", optional=True)
    _sha(
        receipt["previous_installed_manifest_sha256"],
        "previous_installed_manifest_sha256",
        optional=True,
    )
    _sha(receipt["installed_manifest_sha256"], "installed_manifest_sha256", optional=True)
    _validate_target_record(receipt["target"])
    changes = receipt["changes"]
    if not isinstance(changes, list):
        raise HarnessInstallError("install receipt changes must be an array")
    paths: list[str] = []
    for index, change in enumerate(changes):
        change = _closed(change, _CHANGE_FIELDS, f"changes[{index}]")
        path = _path(change["path"], f"changes[{index}].path")
        _validate_provider_path(receipt["provider"], path)
        if change["action"] not in _ACTIONS:
            raise HarnessInstallError(f"changes[{index}].action is invalid")
        _sha(change["before_sha256"], f"changes[{index}].before_sha256", optional=True)
        _sha(change["after_sha256"], f"changes[{index}].after_sha256", optional=True)
        before = change["before_sha256"]
        after = change["after_sha256"]
        consistent = {
            "create": before is None and after is not None,
            "replace": before is not None and after is not None and before != after,
            "remove": before is not None and after is None,
            "unchanged": before is not None and before == after,
        }[change["action"]]
        if not consistent:
            raise HarnessInstallError(f"changes[{index}] contradicts its action")
        paths.append(path)
    if paths != sorted(set(paths)):
        raise HarnessInstallError("install receipt change paths must be sorted and unique")
    native = receipt["native_materialization_sha256"]
    previous = receipt["previous_installed_manifest_sha256"]
    installed_sha = receipt["installed_manifest_sha256"]
    actions = {change["action"] for change in changes}
    outcome_invariants = {
        "planned-install": native is not None
        and previous is None
        and installed_sha is None
        and actions == {"create"},
        "planned-upgrade": native is not None
        and previous is not None
        and installed_sha is None
        and bool(actions - {"unchanged"}),
        "planned-uninstall": native is not None
        and previous is not None
        and installed_sha is None
        and actions == {"remove"},
        "planned-unchanged": native is not None
        and previous is not None
        and installed_sha == previous
        and actions == {"unchanged"},
        "installed": native is not None
        and previous is None
        and installed_sha is not None
        and actions == {"create"},
        "unchanged": native is not None
        and previous is not None
        and installed_sha == previous
        and actions == {"unchanged"},
        "upgraded": native is not None
        and previous is not None
        and installed_sha is not None
        and installed_sha != previous
        and bool(actions - {"unchanged"}),
        "uninstalled": native is not None
        and previous is not None
        and installed_sha is None
        and actions == {"remove"},
    }
    if not changes or not outcome_invariants[receipt["outcome"]]:
        raise HarnessInstallError(
            "install receipt outcome requires consistent authority and changes"
        )
    installed = receipt["outcome"] in {"installed", "unchanged", "upgraded"}
    _validate_claims(receipt["claims"], installed=installed)
    if receipt["limitations"] != [_RECEIPT_LIMIT]:
        raise HarnessInstallError("install receipt limitations are not closed")
    if receipt["content_sha256"] != _content_address(receipt):
        raise HarnessInstallError("install receipt content digest is false")
    return receipt


def validate_rollback_receipt(value: Any) -> dict[str, Any]:
    """Validate a closed receipt that truthfully distinguishes complete from partial rollback."""

    receipt = _closed(value, _ROLLBACK_FIELDS, "rollback receipt")
    if (
        receipt["schema_version"] != "1"
        or receipt["record_kind"] != "provider_harness_rollback_receipt"
        or receipt["operation"] not in {"install", "upgrade", "uninstall"}
        or receipt["outcome"] not in _ROLLBACK_OUTCOMES
        or receipt["provider"] not in _PROVIDERS
    ):
        raise HarnessInstallError("rollback receipt identity is invalid")
    _timestamp(receipt["observed_at"], "observed_at")
    _validate_target_record(receipt["target"])
    if (
        not isinstance(receipt["error_type"], str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", receipt["error_type"]) is None
    ):
        raise HarnessInstallError("rollback receipt error_type is invalid")
    path_sets: list[list[str]] = []
    for field in ("rolled_back_paths", "unresolved_paths"):
        paths = receipt[field]
        if (
            not isinstance(paths, list)
            or paths != sorted(set(paths))
            or any(not isinstance(item, str) for item in paths)
        ):
            raise HarnessInstallError(f"rollback receipt {field} is invalid")
        for path in paths:
            if path != _STATE_MANIFEST_PATH:
                _path(path, f"rollback receipt {field}")
                _validate_provider_path(receipt["provider"], path)
        path_sets.append(paths)
    if set(path_sets[0]) & set(path_sets[1]):
        raise HarnessInstallError("rollback receipt paths overlap")
    incomplete = bool(receipt["unresolved_paths"])
    if incomplete != (receipt["outcome"] == "rollback-incomplete"):
        raise HarnessInstallError("rollback receipt outcome contradicts unresolved paths")
    _validate_claims(receipt["claims"], installed=False)
    if receipt["limitations"] != [_ROLLBACK_LIMIT]:
        raise HarnessInstallError("rollback receipt limitations are not closed")
    if receipt["content_sha256"] != _content_address(receipt):
        raise HarnessInstallError("rollback receipt content digest is false")
    return receipt


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise HarnessInstallError(f"{label} must be UTC at whole-second precision")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HarnessInstallError(f"{label} is invalid") from exc
    return value


def _installed_manifest(
    bundle: SealedNativeMaterializationBundle,
    target: TargetCheckoutIdentity,
    created_directories: set[str],
) -> dict[str, Any]:
    value = {
        "schema_version": "1",
        "record_kind": "provider_harness_installed_manifest",
        "provider": bundle.manifest.document["provider"],
        "native_materialization_sha256": bundle.manifest.sha256,
        "source_policy_sha256": bundle.manifest.document["source_policy"]["sha256"],
        "install_nonce": secrets.token_hex(16),
        "target": _target_record(target),
        "files": [
            {
                "path": row["path"],
                "mode": row["mode"],
                "sha256": row["sha256"],
                "bytes": row["bytes"],
            }
            for row in bundle.manifest.document["files"]
        ],
        "created_directories": sorted(created_directories),
        "claims": {"installed_bytes_verified": True, **_FALSE_RUNTIME_CLAIMS},
        "limitations": [_INSTALLED_LIMIT],
    }
    value["content_sha256"] = _content_address(value)
    return validate_installed_manifest(value)


def _stat_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
    )


def _read_owned(
    target: TargetCheckoutIdentity, row: Mapping[str, Any]
) -> tuple[bytes, tuple[int, ...]]:
    path = target.root / row["path"]
    try:
        content, metadata = _read_regular(path, row["bytes"], f"owned target {row['path']}")
    except HarnessInstallError as exc:
        raise HarnessInstallConflict(f"owned target changed: {row['path']}") from exc
    if stat.S_IMODE(metadata.st_mode) != _MODES[row["mode"]] or _digest(content) != row["sha256"]:
        raise HarnessInstallConflict(f"owned target changed: {row['path']}")
    return content, _stat_fingerprint(metadata)


def _ensure_physical_parents(root: Path, relative: str, *, create: bool) -> set[str]:
    created: set[str] = set()
    current = root
    parts = PurePosixPath(relative).parts[:-1]
    for index, part in enumerate(parts):
        current = current / part
        rel = PurePosixPath(*parts[: index + 1]).as_posix()
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                continue
            try:
                current.mkdir(mode=0o700)
            except FileExistsError as exc:
                metadata = current.lstat()
                if not stat.S_ISDIR(metadata.st_mode):
                    raise HarnessInstallConflict(
                        f"target parent concurrently changed: {rel}"
                    ) from exc
            else:
                created.add(rel)
                metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise HarnessInstallConflict(f"target parent is a symlink or non-directory: {rel}")
    return created


def _target_absent(target: TargetCheckoutIdentity, relative: str) -> None:
    _ensure_physical_parents(target.root, relative, create=False)
    try:
        (target.root / relative).lstat()
    except FileNotFoundError:
        return
    raise HarnessInstallConflict(f"foreign target already exists: {relative}")


def _write_temp(path: Path, content: bytes, mode: int) -> Path:
    temporary = path.with_name(f".{path.name}.installer-{os.getpid()}-{time.monotonic_ns()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise HarnessInstallError(f"short write for installer temporary {path.name}")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except BaseException:
        with suppress(OSError):
            temporary.unlink()
        raise
    finally:
        os.close(descriptor)
    return temporary


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_file(
    target: TargetCheckoutIdentity, file: _BundleFile
) -> tuple[set[str], tuple[int, ...]]:
    created = _ensure_physical_parents(target.root, file.path, create=True)
    path = target.root / file.path
    temporary = _write_temp(path, file.content, file.mode)
    try:
        os.link(temporary, path, follow_symlinks=False)
    except FileExistsError as exc:
        raise HarnessInstallConflict(f"target concurrently changed: {file.path}") from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
    _fsync_directory(path.parent)
    observed, metadata = _read_regular(path, len(file.content), f"target {file.path}")
    if observed != file.content or stat.S_IMODE(metadata.st_mode) != file.mode:
        raise HarnessInstallConflict(f"target changed after creation: {file.path}")
    return created, _stat_fingerprint(metadata)


def _replace_file(
    target: TargetCheckoutIdentity, file: _BundleFile, expected: Mapping[str, Any]
) -> tuple[bytes, tuple[int, ...]]:
    before, fingerprint = _read_owned(target, expected)
    path = target.root / file.path
    temporary = _write_temp(path, file.content, file.mode)
    try:
        if _stat_fingerprint(path.lstat()) != fingerprint:
            raise HarnessInstallConflict(f"target concurrently changed: {file.path}")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
    observed, metadata = _read_regular(path, len(file.content), f"target {file.path}")
    if observed != file.content or stat.S_IMODE(metadata.st_mode) != file.mode:
        raise HarnessInstallConflict(f"target changed after replacement: {file.path}")
    return before, _stat_fingerprint(metadata)


def _restore_file(root: Path, relative: str, content: bytes, mode: int) -> None:
    target = root / relative
    temporary = _write_temp(target, content, mode)
    try:
        os.replace(temporary, target)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _remove_empty_directories(root: Path, directories: set[str] | list[str]) -> None:
    for relative in sorted(directories, key=lambda item: (-len(PurePosixPath(item).parts), item)):
        path = root / relative
        try:
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                path.rmdir()
                _fsync_directory(path.parent)
        except (FileNotFoundError, OSError):
            continue


def _status_paths(root: Path) -> set[str]:
    # This module keeps its own environment rather than the shared, hardened `run_git`: a
    # repository defining its own Git content filters (git-crypt, for one) must be inspected
    # exactly as before, not newly refused by `run_git`'s stricter repository-filter check.
    # The resolved global ignore file is still passed through, under the same rule as every
    # other cleanliness verdict: only when this repository does not already set its own
    # `core.excludesFile`.
    excludes_file = resolve_excludes_file_override(root, environment=_git_environment())
    excludes_args = (
        ("-c", f"core.excludesFile={excludes_file}") if excludes_file is not None else ()
    )
    raw = _git(root, *excludes_args, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if not raw:
        return set()
    records = raw.split(b"\0")
    if records[-1] != b"":
        raise HarnessInstallError("Git status was not NUL-terminated")
    paths: set[str] = set()
    index = 0
    while index < len(records) - 1:
        record = records[index]
        if len(record) < 4 or record[2:3] != b" ":
            raise HarnessInstallError("Git returned malformed porcelain status")
        status_code = record[:2].decode("ascii", errors="strict")
        if "R" in status_code or "C" in status_code:
            raise HarnessInstallConflict("dirty checkout contains a rename or copy")
        try:
            path = record[3:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessInstallConflict("dirty checkout contains a non-UTF-8 path") from exc
        paths.add(path)
        index += 1
    return paths


def _assert_checkout_state(target: TargetCheckoutIdentity, owned_paths: set[str]) -> None:
    observed = inspect_target_checkout(target.root)
    if observed != target:
        raise HarnessInstallConflict("target checkout identity changed")
    if _git_line(target.root, "core.fileMode", "config", "--bool", "core.fileMode") != "true":
        raise HarnessInstallConflict("target checkout has core.fileMode disabled")
    index = _git(target.root, "ls-files", "-v", "-z")
    for record in index.split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise HarnessInstallError("Git returned malformed index flags")
        tag = chr(record[0])
        if tag == "S" or tag.islower():
            raise HarnessInstallConflict(
                "target index contains skip-worktree or assume-unchanged entries"
            )
    dirty = _status_paths(target.root)
    if not dirty <= owned_paths:
        raise HarnessInstallConflict(
            f"dirty checkout contains foreign paths: {sorted(dirty - owned_paths)}"
        )
    for path in owned_paths:
        tracked = _git(target.root, "ls-files", "-z", "--", path)
        if tracked:
            raise HarnessInstallConflict(f"owned target became a tracked product path: {path}")


@dataclass(slots=True)
class _Applied:
    action: Literal["create", "replace", "remove"]
    path: str
    before: bytes | None
    before_mode: int | None
    after: bytes | None
    after_mode: int | None
    after_fingerprint: tuple[int, ...] | None


class TransactionalHarnessInstaller:
    """Single-host, lock-serialized installer with explicit operation preconditions."""

    def __init__(
        self,
        *,
        state_root: Path | str,
        clock: Callable[[], datetime] | None = None,
        lock_timeout_s: float = 5.0,
        _checkpoint: InstallerCheckpoint | None = None,
    ) -> None:
        requested = Path(state_root).expanduser()
        if requested.is_symlink():
            raise HarnessInstallError("installer state root may not be a symlink")
        try:
            self.root = assert_writable(requested)
        except (OSError, WriteBoundaryError) as exc:
            raise HarnessInstallError(f"invalid installer state root: {exc}") from exc
        if lock_timeout_s <= 0:
            raise HarnessInstallError("installer lock timeout must be positive")
        # Directory creation is deliberately lazy.  An operation must prove that this root is
        # outside both the subject checkout and its Git common directory before writing here.
        self.receipts = self.root / "receipts"
        self.rollbacks = self.root / "rollbacks"
        self.installs = self.root / "installed"
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock_timeout_s = lock_timeout_s
        self._checkpoint = _checkpoint or (lambda _phase, _path: None)

    def _directory(self, name: str) -> Path:
        path = self.root / name
        if path.is_symlink():
            raise HarnessInstallError(f"installer state directory may not be a symlink: {name}")
        path.mkdir(mode=0o700, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise HarnessInstallError(f"installer state path is not a private directory: {name}")
        return path

    def _prepare_state(self) -> None:
        if self.root.is_symlink():
            raise HarnessInstallError("installer state root may not be a symlink")
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.resolve(strict=True) != self.root:
                raise HarnessInstallError("installer state root changed while it was prepared")
            metadata = self.root.lstat()
        except OSError as exc:
            raise HarnessInstallError(f"cannot prepare installer state root: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise HarnessInstallError("installer state root must be a private physical directory")
        self.receipts = self._directory("receipts")
        self.rollbacks = self._directory("rollbacks")
        self.installs = self._directory("installed")

    @contextmanager
    def _lock(self):
        self._prepare_state()
        path = self.root / ".installer.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise HarnessInstallError(f"cannot open installer lock: {exc}") from exc
        deadline = time.monotonic() + self.lock_timeout_s
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise HarnessInstallError("installer lock is not one regular file")
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise HarnessInstallError("timed out waiting for installer lock") from exc
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _now(self) -> str:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise HarnessInstallError("installer clock must be timezone-aware")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _assert_disjoint(
        self, target: TargetCheckoutIdentity, bundle: SealedNativeMaterializationBundle | None
    ) -> None:
        roots = [self.root]
        if bundle is not None:
            roots.append(bundle.source_root)
        authorities = (target.root, target.common_dir)
        for other in roots:
            if any(
                other == authority or other in authority.parents or authority in other.parents
                for authority in authorities
            ):
                raise HarnessInstallError(
                    "target Git authority, source bundle, and installer state must be disjoint"
                )
        if bundle is not None and (
            self.root == bundle.source_root
            or self.root in bundle.source_root.parents
            or bundle.source_root in self.root.parents
        ):
            raise HarnessInstallError("source bundle and installer state must be disjoint")

    def _current_path(self, provider: str, target: TargetCheckoutIdentity) -> Path:
        if provider not in _PROVIDERS:
            raise HarnessInstallError(f"unsupported provider: {provider!r}")
        directory = self.installs / target.repository_root_sha256
        if directory.is_symlink():
            raise HarnessInstallError("installed-manifest directory may not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise HarnessInstallError("installed-manifest path is not a private directory")
        return directory / f"{provider}.json"

    def _read_current(
        self, provider: str, target: TargetCheckoutIdentity
    ) -> tuple[dict[str, Any] | None, bytes | None, tuple[int, ...] | None]:
        path = self._current_path(provider, target)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None, None, None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _MAX_FILE_BYTES
        ):
            raise HarnessInstallError("current installed manifest is not one bounded regular file")
        content, observed = _read_regular(path, metadata.st_size, "current installed manifest")
        try:
            value = json.loads(content.decode("utf-8"), object_pairs_hook=_reject_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HarnessInstallError(f"current installed manifest is invalid JSON: {exc}") from exc
        validated = validate_installed_manifest(value)
        if content != _canonical(validated):
            raise HarnessInstallError("current installed manifest is not canonical")
        if (
            validated["provider"] != provider
            or validated["target"]["repository_root_sha256"] != target.repository_root_sha256
            or validated["target"]["repository_common_dir_sha256"]
            != target.repository_common_dir_sha256
            or validated["target"]["repository_root_device"] != target.repository_root_device
            or validated["target"]["repository_root_inode"] != target.repository_root_inode
            or validated["target"]["repository_common_dir_device"]
            != target.repository_common_dir_device
            or validated["target"]["repository_common_dir_inode"]
            != target.repository_common_dir_inode
        ):
            raise HarnessInstallError("current installed manifest has foreign target authority")
        return validated, content, _stat_fingerprint(observed)

    def read_installed(
        self, *, provider: str, target: TargetCheckoutIdentity
    ) -> dict[str, Any] | None:
        self._assert_disjoint(target, None)
        with self._lock():
            value, _, _ = self._read_current(provider, target)
            return copy.deepcopy(value)

    def dry_run_install(
        self,
        *,
        bundle: SealedNativeMaterializationBundle,
        target: TargetCheckoutIdentity,
    ) -> HarnessInstallResult:
        provider = bundle.manifest.document["provider"]
        self._preflight_bundle(bundle, target)
        with self._lock():
            self._preflight_bundle(bundle, target)
            current, _, _ = self._read_current(provider, target)
            if current is None:
                _assert_checkout_state(target, set())
                for file in bundle.files:
                    _target_absent(target, file.path)
                outcome = "planned-install"
                changes = self._changes(None, bundle)
                installed_sha = None
            else:
                self._validate_owned(current, target)
                if current["native_materialization_sha256"] != bundle.manifest.sha256:
                    raise HarnessInstallConflict(
                        "a different harness is already installed; use upgrade"
                    )
                outcome = "planned-unchanged"
                changes = self._changes(current, bundle)
                installed_sha = current["content_sha256"]
            return self._result(
                operation="dry-run",
                outcome=outcome,
                provider=provider,
                target=target,
                native_sha=bundle.manifest.sha256,
                previous_sha=current["content_sha256"] if current else None,
                installed_sha=installed_sha,
                changes=changes,
                installed=current,
            )

    def dry_run_upgrade(
        self,
        *,
        bundle: SealedNativeMaterializationBundle,
        target: TargetCheckoutIdentity,
    ) -> HarnessInstallResult:
        provider = bundle.manifest.document["provider"]
        self._preflight_bundle(bundle, target)
        with self._lock():
            self._preflight_bundle(bundle, target)
            current, _, _ = self._read_current(provider, target)
            if current is None:
                raise HarnessInstallConflict("provider harness is not installed; use install")
            self._validate_owned(current, target)
            if current["native_materialization_sha256"] == bundle.manifest.sha256:
                raise HarnessInstallConflict(
                    "requested upgrade is already installed; use dry_run_install"
                )
            old = {row["path"] for row in current["files"]}
            for path in sorted({file.path for file in bundle.files} - old):
                _target_absent(target, path)
            return self._result(
                operation="dry-run",
                outcome="planned-upgrade",
                provider=provider,
                target=target,
                native_sha=bundle.manifest.sha256,
                previous_sha=current["content_sha256"],
                installed_sha=None,
                changes=self._changes(current, bundle),
                installed=current,
            )

    def dry_run_uninstall(
        self,
        *,
        provider: str,
        target: TargetCheckoutIdentity,
    ) -> HarnessInstallResult:
        self._assert_disjoint(target, None)
        with self._lock():
            self._assert_disjoint(target, None)
            current, _, _ = self._read_current(provider, target)
            if current is None:
                raise HarnessInstallConflict("provider harness is not installed")
            self._validate_owned(current, target)
            return self._result(
                operation="dry-run",
                outcome="planned-uninstall",
                provider=provider,
                target=target,
                native_sha=current["native_materialization_sha256"],
                previous_sha=current["content_sha256"],
                installed_sha=None,
                changes=self._uninstall_changes(current),
                installed=current,
            )

    def install(
        self,
        *,
        bundle: SealedNativeMaterializationBundle,
        target: TargetCheckoutIdentity,
    ) -> HarnessInstallResult:
        provider = bundle.manifest.document["provider"]
        self._preflight_bundle(bundle, target)
        with self._lock():
            self._preflight_bundle(bundle, target)
            current, _, _ = self._read_current(provider, target)
            if current is not None:
                self._validate_owned(current, target)
                if current["native_materialization_sha256"] != bundle.manifest.sha256:
                    raise HarnessInstallConflict(
                        "a different harness is already installed; use upgrade"
                    )
                return self._result(
                    operation="install",
                    outcome="unchanged",
                    provider=provider,
                    target=target,
                    native_sha=bundle.manifest.sha256,
                    previous_sha=current["content_sha256"],
                    installed_sha=current["content_sha256"],
                    changes=self._changes(current, bundle),
                    installed=current,
                )
            _assert_checkout_state(target, set())
            for file in bundle.files:
                _target_absent(target, file.path)
            self._checkpoint("before-apply", None)
            try:
                self._preflight_bundle(bundle, target)
                _assert_checkout_state(target, set())
                for file in bundle.files:
                    _target_absent(target, file.path)
            except HarnessInstallError as exc:
                raise HarnessInstallConflict(
                    f"target concurrently changed before install: {exc}"
                ) from exc
            applied: list[_Applied] = []
            created_directories: set[str] = set()
            current_path = self._current_path(provider, target)
            current_written = False
            try:
                for index, file in enumerate(bundle.files):
                    item = _Applied("create", file.path, None, None, file.content, file.mode, None)
                    applied.append(item)
                    created_directories.update(
                        _ensure_physical_parents(target.root, file.path, create=True)
                    )
                    created, fingerprint = _create_file(target, file)
                    created_directories.update(created)
                    item.after_fingerprint = fingerprint
                    self._checkpoint(f"after-apply:{index}", target.root / file.path)
                installed = _installed_manifest(bundle, target, created_directories)
                current_written = True
                self._write_current_create(current_path, installed)
                self._validate_owned(installed, target)
                result = self._result(
                    operation="install",
                    outcome="installed",
                    provider=provider,
                    target=target,
                    native_sha=bundle.manifest.sha256,
                    previous_sha=None,
                    installed_sha=installed["content_sha256"],
                    changes=self._changes(None, bundle),
                    installed=installed,
                )
                return result
            except BaseException as exc:
                rolled_back: list[str] = []
                unresolved: list[str] = []
                if current_written:
                    if self._reconcile_current_absent(current_path, installed):
                        rolled_back.append(_STATE_MANIFEST_PATH)
                    else:
                        unresolved.append(_STATE_MANIFEST_PATH)
                target_rolled_back, target_unresolved = self._rollback_target(
                    target, applied, created_directories
                )
                self._raise_rolled_back(
                    "install",
                    provider,
                    target,
                    exc,
                    rolled_back + target_rolled_back,
                    unresolved + target_unresolved,
                )

    def upgrade(
        self,
        *,
        bundle: SealedNativeMaterializationBundle,
        target: TargetCheckoutIdentity,
    ) -> HarnessInstallResult:
        provider = bundle.manifest.document["provider"]
        self._preflight_bundle(bundle, target)
        with self._lock():
            self._preflight_bundle(bundle, target)
            current, current_bytes, current_fingerprint = self._read_current(provider, target)
            if current is None or current_bytes is None or current_fingerprint is None:
                raise HarnessInstallConflict("provider harness is not installed; use install")
            self._validate_owned(current, target)
            if current["native_materialization_sha256"] == bundle.manifest.sha256:
                raise HarnessInstallConflict("requested upgrade is already installed; use install")
            old = {row["path"]: row for row in current["files"]}
            new = {file.path: file for file in bundle.files}
            for path in sorted(set(new) - set(old)):
                _target_absent(target, path)
            self._checkpoint("before-apply", None)
            try:
                self._preflight_bundle(bundle, target)
                current_again, bytes_again, fingerprint_again = self._read_current(provider, target)
                if (
                    current_again != current
                    or bytes_again != current_bytes
                    or fingerprint_again != current_fingerprint
                ):
                    raise HarnessInstallConflict("installed manifest concurrently changed")
                self._validate_owned(current, target)
                for path in sorted(set(new) - set(old)):
                    _target_absent(target, path)
            except HarnessInstallError as exc:
                raise HarnessInstallConflict(
                    f"target concurrently changed before upgrade: {exc}"
                ) from exc
            applied: list[_Applied] = []
            created_directories = set(current["created_directories"])
            current_path = self._current_path(provider, target)
            state_replaced = False
            try:
                actions = [
                    path
                    for path in sorted(new)
                    if path not in old
                    or (
                        old[path]["sha256"],
                        old[path]["mode"],
                        old[path]["bytes"],
                    )
                    != (
                        _digest(new[path].content),
                        format(new[path].mode, "04o"),
                        len(new[path].content),
                    )
                ]
                actions.extend(path for path in sorted(set(old) - set(new)))
                for index, path in enumerate(actions):
                    if path not in old:
                        item = _Applied(
                            "create",
                            path,
                            None,
                            None,
                            new[path].content,
                            new[path].mode,
                            None,
                        )
                        applied.append(item)
                        created_directories.update(
                            _ensure_physical_parents(target.root, path, create=True)
                        )
                        created, fingerprint = _create_file(target, new[path])
                        created_directories.update(created)
                        item.after_fingerprint = fingerprint
                    elif path not in new:
                        before, _ = _read_owned(target, old[path])
                        applied.append(
                            _Applied(
                                "remove",
                                path,
                                before,
                                _MODES[old[path]["mode"]],
                                None,
                                None,
                                None,
                            )
                        )
                        _, fingerprint = _read_owned(target, old[path])
                        target_path = target.root / path
                        if _stat_fingerprint(target_path.lstat()) != fingerprint:
                            raise HarnessInstallConflict(f"target concurrently changed: {path}")
                        target_path.unlink()
                        _fsync_directory(target_path.parent)
                    else:
                        before, _ = _read_owned(target, old[path])
                        item = _Applied(
                            "replace",
                            path,
                            before,
                            _MODES[old[path]["mode"]],
                            new[path].content,
                            new[path].mode,
                            None,
                        )
                        applied.append(item)
                        before, fingerprint = _replace_file(target, new[path], old[path])
                        item.after_fingerprint = fingerprint
                    self._checkpoint(f"after-apply:{index}", target.root / path)
                installed = _installed_manifest(bundle, target, created_directories)
                state_replaced = True
                self._replace_current(
                    current_path,
                    current_fingerprint,
                    installed,
                )
                self._validate_owned(installed, target)
                return self._result(
                    operation="upgrade",
                    outcome="upgraded",
                    provider=provider,
                    target=target,
                    native_sha=bundle.manifest.sha256,
                    previous_sha=current["content_sha256"],
                    installed_sha=installed["content_sha256"],
                    changes=self._changes(current, bundle),
                    installed=installed,
                )
            except BaseException as exc:
                rolled_back: list[str] = []
                unresolved: list[str] = []
                if state_replaced:
                    try:
                        self._reconcile_current_old(current_path, installed, current_bytes)
                    except (OSError, HarnessInstallError):
                        unresolved.append(_STATE_MANIFEST_PATH)
                    else:
                        rolled_back.append(_STATE_MANIFEST_PATH)
                target_rolled_back, target_unresolved = self._rollback_target(
                    target, applied, created_directories - set(current["created_directories"])
                )
                self._raise_rolled_back(
                    "upgrade",
                    provider,
                    target,
                    exc,
                    rolled_back + target_rolled_back,
                    unresolved + target_unresolved,
                )

    def uninstall(
        self,
        *,
        provider: str,
        target: TargetCheckoutIdentity,
    ) -> HarnessInstallResult:
        self._assert_disjoint(target, None)
        with self._lock():
            self._assert_disjoint(target, None)
            current, current_bytes, current_fingerprint = self._read_current(provider, target)
            if current is None or current_bytes is None or current_fingerprint is None:
                raise HarnessInstallConflict("provider harness is not installed")
            self._validate_owned(current, target)
            self._checkpoint("before-apply", None)
            try:
                current_again, bytes_again, fingerprint_again = self._read_current(provider, target)
                if (
                    current_again != current
                    or bytes_again != current_bytes
                    or fingerprint_again != current_fingerprint
                ):
                    raise HarnessInstallConflict("installed manifest concurrently changed")
                self._validate_owned(current, target)
            except HarnessInstallError as exc:
                raise HarnessInstallConflict(
                    f"target concurrently changed before uninstall: {exc}"
                ) from exc
            applied: list[_Applied] = []
            current_path = self._current_path(provider, target)
            state_removed = False
            try:
                for index, row in enumerate(current["files"]):
                    before, _ = _read_owned(target, row)
                    applied.append(
                        _Applied(
                            "remove",
                            row["path"],
                            before,
                            _MODES[row["mode"]],
                            None,
                            None,
                            None,
                        )
                    )
                    _, fingerprint = _read_owned(target, row)
                    target_path = target.root / row["path"]
                    if _stat_fingerprint(target_path.lstat()) != fingerprint:
                        raise HarnessInstallConflict(f"target concurrently changed: {row['path']}")
                    target_path.unlink()
                    _fsync_directory(target_path.parent)
                    self._checkpoint(f"after-apply:{index}", target.root / row["path"])
                _assert_checkout_state(target, set())
                metadata = current_path.lstat()
                if _stat_fingerprint(metadata) != current_fingerprint:
                    raise HarnessInstallConflict("installed manifest concurrently changed")
                state_removed = True
                current_path.unlink()
                _fsync_directory(current_path.parent)
                _remove_empty_directories(target.root, current["created_directories"])
                _assert_checkout_state(target, set())
                return self._result(
                    operation="uninstall",
                    outcome="uninstalled",
                    provider=provider,
                    target=target,
                    native_sha=current["native_materialization_sha256"],
                    previous_sha=current["content_sha256"],
                    installed_sha=None,
                    changes=self._uninstall_changes(current),
                    installed=None,
                )
            except BaseException as exc:
                rolled_back: list[str] = []
                unresolved: list[str] = []
                if state_removed:
                    try:
                        self._reconcile_current_present(current_path, current_bytes)
                    except (OSError, HarnessInstallError):
                        unresolved.append(_STATE_MANIFEST_PATH)
                    else:
                        rolled_back.append(_STATE_MANIFEST_PATH)
                target_rolled_back, target_unresolved = self._rollback_target(
                    target, applied, set()
                )
                self._raise_rolled_back(
                    "uninstall",
                    provider,
                    target,
                    exc,
                    rolled_back + target_rolled_back,
                    unresolved + target_unresolved,
                )

    def _preflight_bundle(
        self, bundle: SealedNativeMaterializationBundle, target: TargetCheckoutIdentity
    ) -> None:
        self._assert_disjoint(target, bundle)
        bundle.verify_unchanged()
        if (
            _canonical(bundle.manifest.document) != bundle.manifest.canonical_bytes
            or _digest(bundle.manifest.canonical_bytes) != bundle.manifest.sha256
        ):
            raise HarnessInstallError("sealed native materialization manifest changed")

    def _validate_owned(self, manifest: Mapping[str, Any], target: TargetCheckoutIdentity) -> None:
        owned = {row["path"] for row in manifest["files"]}
        _assert_checkout_state(target, owned)
        for row in manifest["files"]:
            _read_owned(target, row)

    def _changes(
        self,
        current: Mapping[str, Any] | None,
        bundle: SealedNativeMaterializationBundle,
    ) -> list[dict[str, Any]]:
        old = {row["path"]: row for row in current["files"]} if current else {}
        new = {row.path: row for row in bundle.files}
        result: list[dict[str, Any]] = []
        for path in sorted(set(old) | set(new)):
            before = old.get(path)
            after = new.get(path)
            if before is None:
                action = "create"
            elif after is None:
                action = "remove"
            elif (
                before["sha256"],
                before["mode"],
                before["bytes"],
            ) == (_digest(after.content), format(after.mode, "04o"), len(after.content)):
                action = "unchanged"
            else:
                action = "replace"
            result.append(
                {
                    "path": path,
                    "action": action,
                    "before_sha256": before["sha256"] if before else None,
                    "after_sha256": _digest(after.content) if after else None,
                }
            )
        return result

    def _uninstall_changes(self, current: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "path": row["path"],
                "action": "remove",
                "before_sha256": row["sha256"],
                "after_sha256": None,
            }
            for row in current["files"]
        ]

    def _result(
        self,
        *,
        operation: str,
        outcome: str,
        provider: str,
        target: TargetCheckoutIdentity,
        native_sha: str | None,
        previous_sha: str | None,
        installed_sha: str | None,
        changes: list[dict[str, Any]],
        installed: Mapping[str, Any] | None,
    ) -> HarnessInstallResult:
        receipt = {
            "schema_version": "1",
            "record_kind": "provider_harness_install_receipt",
            "operation": operation,
            "outcome": outcome,
            "provider": provider,
            "observed_at": self._now(),
            "native_materialization_sha256": native_sha,
            "previous_installed_manifest_sha256": previous_sha,
            "installed_manifest_sha256": installed_sha,
            "target": _target_record(target),
            "changes": changes,
            "claims": {
                "installed_bytes_verified": outcome in {"installed", "unchanged", "upgraded"},
                **_FALSE_RUNTIME_CLAIMS,
            },
            "limitations": [_RECEIPT_LIMIT],
        }
        receipt["content_sha256"] = _content_address(receipt)
        receipt = validate_install_receipt(receipt)
        receipt_path = self._publish_record(self.receipts, receipt)
        return HarnessInstallResult(
            operation=operation,
            outcome=outcome,
            receipt=copy.deepcopy(receipt),
            receipt_path=receipt_path,
            installed_manifest=copy.deepcopy(dict(installed)) if installed is not None else None,
            installed_manifest_sha256=installed_sha,
        )

    def _publish_record(self, directory: Path, value: Mapping[str, Any]) -> Path:
        content = _canonical(value)
        digest = value["content_sha256"]
        path = directory / f"{digest}.json"
        temporary = _write_temp(path, content, 0o600)
        created = False
        try:
            os.link(temporary, path, follow_symlinks=False)
            created = True
        except FileExistsError:
            existing, metadata = _read_regular(
                path, len(content), "existing content-addressed receipt"
            )
            if existing != content or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise HarnessInstallError(
                    "content-addressed receipt path contains foreign bytes"
                ) from None
            return path
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
        try:
            _fsync_directory(directory)
        except BaseException:
            if created:
                try:
                    observed, metadata = _read_regular(
                        path, len(content), "failed content-addressed receipt"
                    )
                    if observed == content and stat.S_IMODE(metadata.st_mode) == 0o600:
                        path.unlink()
                        _fsync_directory(directory)
                except (OSError, HarnessInstallError):
                    pass
            raise
        return path

    def _write_current_create(self, path: Path, value: Mapping[str, Any]) -> None:
        self._write_current_bytes_create(path, _canonical(value))

    def _write_current_bytes_create(self, path: Path, content: bytes) -> None:
        temporary = _write_temp(path, content, 0o600)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise HarnessInstallConflict(
                "current installed manifest concurrently appeared"
            ) from exc
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
        _fsync_directory(path.parent)

    def _replace_current(
        self,
        path: Path,
        expected_fingerprint: tuple[int, ...],
        value: Mapping[str, Any],
    ) -> None:
        temporary = _write_temp(path, _canonical(value), 0o600)
        try:
            if _stat_fingerprint(path.lstat()) != expected_fingerprint:
                raise HarnessInstallConflict("current installed manifest concurrently changed")
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def _reconcile_current_absent(self, path: Path, value: Mapping[str, Any]) -> bool:
        content = _canonical(value)
        try:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                return True
            observed, metadata = _read_regular(path, len(content), "current installed manifest")
            if (
                observed != content
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or _stat_fingerprint(path.lstat()) != _stat_fingerprint(metadata)
            ):
                return False
            path.unlink()
            _fsync_directory(path.parent)
            return True
        except (OSError, HarnessInstallError):
            return False

    def _reconcile_current_old(self, path: Path, new: Mapping[str, Any], old_bytes: bytes) -> None:
        content = _canonical(new)
        try:
            metadata = path.lstat()
            observed, stable = _read_regular(path, metadata.st_size, "current installed manifest")
            if _stat_fingerprint(path.lstat()) != _stat_fingerprint(stable):
                raise HarnessInstallError("installed manifest changed during reconciliation")
            if observed == old_bytes and stat.S_IMODE(stable.st_mode) == 0o600:
                return
            if observed != content:
                raise HarnessInstallError("cannot reconcile foreign installed manifest")
            _restore_file(path.parent, path.name, old_bytes, 0o600)
            _fsync_directory(path.parent)
        except FileNotFoundError as exc:
            raise HarnessInstallError(
                "installed manifest disappeared during reconciliation"
            ) from exc

    def _reconcile_current_present(self, path: Path, old_bytes: bytes) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            self._write_current_bytes_create(path, old_bytes)
            return
        observed, _ = _read_regular(path, metadata.st_size, "current installed manifest")
        if observed != old_bytes or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise HarnessInstallError("cannot reconcile foreign installed manifest")

    def _rollback_target(
        self,
        target: TargetCheckoutIdentity,
        applied: list[_Applied],
        created_directories: set[str],
    ) -> tuple[list[str], list[str]]:
        rolled_back: list[str] = []
        unresolved: list[str] = []
        root_metadata = target.root.stat()
        if (
            root_metadata.st_dev != target.repository_root_device
            or root_metadata.st_ino != target.repository_root_inode
        ):
            return [], sorted(item.path for item in applied)
        for item in reversed(applied):
            path = target.root / item.path
            try:
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    metadata = None
                if item.before is None:
                    if metadata is None:
                        rolled_back.append(item.path)
                        continue
                    if item.after is None:
                        raise HarnessInstallConflict("rollback has no intended created bytes")
                    observed, stable = _read_regular(
                        path, len(item.after), f"rollback target {item.path}"
                    )
                    if (
                        observed != item.after
                        or stat.S_IMODE(stable.st_mode) != item.after_mode
                        or (
                            item.after_fingerprint is not None
                            and _stat_fingerprint(stable) != item.after_fingerprint
                        )
                    ):
                        raise HarnessInstallConflict("rollback target changed")
                    path.unlink()
                    _fsync_directory(path.parent)
                    rolled_back.append(item.path)
                    continue
                if metadata is None:
                    _ensure_physical_parents(target.root, item.path, create=True)
                    temporary = _write_temp(path, item.before, item.before_mode or 0o600)
                    try:
                        os.link(temporary, path, follow_symlinks=False)
                    finally:
                        temporary.unlink(missing_ok=True)
                    _fsync_directory(path.parent)
                    rolled_back.append(item.path)
                    continue
                observed, stable = _read_regular(
                    path, metadata.st_size, f"rollback target {item.path}"
                )
                if observed == item.before and stat.S_IMODE(stable.st_mode) == item.before_mode:
                    rolled_back.append(item.path)
                    continue
                if (
                    item.after is None
                    or observed != item.after
                    or stat.S_IMODE(stable.st_mode) != item.after_mode
                    or (
                        item.after_fingerprint is not None
                        and _stat_fingerprint(stable) != item.after_fingerprint
                    )
                ):
                    raise HarnessInstallConflict("rollback target changed")
                _restore_file(target.root, item.path, item.before, item.before_mode or 0o600)
                _fsync_directory(path.parent)
                rolled_back.append(item.path)
            except (OSError, HarnessInstallError):
                unresolved.append(item.path)
        _remove_empty_directories(target.root, created_directories)
        return sorted(rolled_back), sorted(unresolved)

    def _raise_rolled_back(
        self,
        operation: str,
        provider: str,
        target: TargetCheckoutIdentity,
        exc: BaseException,
        rolled_back: list[str],
        unresolved: list[str],
    ) -> None:
        rolled_back = sorted(set(rolled_back))
        unresolved = sorted(set(unresolved))
        outcome = "rollback-incomplete" if unresolved else "rolled-back"
        receipt = {
            "schema_version": "1",
            "record_kind": "provider_harness_rollback_receipt",
            "operation": operation,
            "outcome": outcome,
            "provider": provider,
            "observed_at": self._now(),
            "error_type": type(exc).__name__,
            "target": _target_record(target),
            "rolled_back_paths": rolled_back,
            "unresolved_paths": unresolved,
            "claims": {"installed_bytes_verified": False, **_FALSE_RUNTIME_CLAIMS},
            "limitations": [_ROLLBACK_LIMIT],
        }
        receipt["content_sha256"] = _content_address(receipt)
        receipt = validate_rollback_receipt(receipt)
        path = self._publish_record(self.rollbacks, receipt)
        detail = "rollback incomplete" if unresolved else "was rolled back"
        raise HarnessInstallError(
            f"{operation} failed and {detail}: {type(exc).__name__}",
            rollback_receipt=receipt,
            rollback_receipt_path=path,
        ) from exc


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HarnessInstallError(f"installer JSON repeats key {key!r}")
        value[key] = item
    return value


__all__ = [
    "HarnessInstallConflict",
    "HarnessInstallError",
    "HarnessInstallResult",
    "SealedNativeMaterializationBundle",
    "TargetCheckoutIdentity",
    "TransactionalHarnessInstaller",
    "inspect_target_checkout",
    "validate_install_receipt",
    "validate_installed_manifest",
    "validate_rollback_receipt",
]
