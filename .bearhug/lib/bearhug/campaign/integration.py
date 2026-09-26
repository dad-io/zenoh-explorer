"""Explicit, provider-neutral integration custody for accepted campaign candidates.

The provider author and reviewer paths deliberately never merge.  This module is the separate
integration-owner boundary: it receives already-declared candidate identities, rechecks their Git
objects in one clean integration worktree, merges them in the caller's explicit order, reruns
shell-free acceptance commands, and publishes a bounded create-only receipt.  It never pushes or
changes a remote.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from bearhug.campaign.capsule_candidate import validate_dependency_base, verify_dependency_base
from bearhug.campaign.review import (
    CampaignReviewError,
    CampaignReviewStore,
    canonical_json_sha256,
    worktree_sha256,
)
from bearhug.paths import assert_writable
from bearhug.providers.receipt import (
    CandidateResult,
    ProviderReceiptError,
    _common_dir,
    capture_close_repository,
    capture_launch_repository,
)


class CampaignIntegrationError(RuntimeError):
    """An explicit integration operation could not preserve a closed result."""


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_INPUT_FIELDS = frozenset({"work_unit_id", "merge_order", "provider_receipt_sha256", "candidate"})
_INPUT_DEPENDENCY_FIELDS = frozenset({"dependency_base"})
_CANDIDATE_FIELDS = frozenset(
    {
        "repository_common_dir_sha256",
        "base_oid",
        "head_oid",
        "tree_oid",
        "patch_sha256",
        "clean",
    }
)
_MERGE_FIELDS = frozenset(
    {
        "work_unit_id",
        "merge_order",
        "head_oid",
        "before_head_oid",
        "after_head_oid",
        "status",
        "stdout_sha256",
        "stderr_sha256",
    }
)
_CHECK_FIELDS = frozenset(
    {"ordinal", "argv_sha256", "returncode", "status", "stdout_sha256", "stderr_sha256"}
)
_REVIEW_REF_FIELDS = frozenset({"work_unit_id", "review_id", "receipt_sha256"})
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "campaign_id",
        "run_id",
        "integration_id",
        "integrator_id",
        "target",
        "branch",
        "repository_common_dir_sha256",
        "base_oid",
        "initial_tree_oid",
        "inputs",
        "reviews",
        "merges",
        "checks",
        "final_head_oid",
        "final_tree_oid",
        "candidate",
        "status",
        "blockers",
        "content_sha256",
    }
)
_ATTEMPT_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "campaign_id",
        "run_id",
        "integration_id",
        "integrator_id",
        "target",
        "branch",
        "repository_common_dir_sha256",
        "base_oid",
        "initial_tree_oid",
        "inputs",
        "reviews",
        "checks",
        "claim",
        "content_sha256",
    }
)
_PROGRESS_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "integration_id",
        "sequence",
        "previous_sha256",
        "event",
        "content_sha256",
    }
)
_MAX_RECEIPT_BYTES = 64 * 1024 * 1024
_MAX_COMMAND_ARGUMENTS = 256
_MAX_ARGUMENT_BYTES = 64 * 1024
_MAX_CHECK_TIMEOUT_SECONDS = 8 * 60 * 60


def _canonical(value: Mapping[str, Any], *, omit_digest: bool = False) -> bytes:
    material = dict(value)
    if omit_digest:
        material.pop("content_sha256", None)
    try:
        return (
            json.dumps(
                material,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CampaignIntegrationError(f"integration receipt is not canonical JSON: {exc}") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CampaignIntegrationError(f"{label} must be lowercase SHA-256")


def _oid(value: Any, label: str) -> None:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise CampaignIntegrationError(f"{label} must be a full lowercase Git object id")


def _id(value: Any, label: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise CampaignIntegrationError(f"{label} is not a canonical identifier")


def _candidate(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CANDIDATE_FIELDS:
        raise CampaignIntegrationError(f"{label} is not a closed candidate")
    _digest(value["repository_common_dir_sha256"], f"{label}.repository_common_dir_sha256")
    _digest(value["patch_sha256"], f"{label}.patch_sha256")
    for field in ("base_oid", "head_oid", "tree_oid"):
        _oid(value[field], f"{label}.{field}")
    if value["clean"] is not True:
        raise CampaignIntegrationError(f"{label} must be clean")
    return value


def _input(value: Any, index: int) -> dict[str, Any]:
    label = f"inputs[{index}]"
    if not isinstance(value, dict) or set(value) not in {
        _INPUT_FIELDS,
        _INPUT_FIELDS | _INPUT_DEPENDENCY_FIELDS,
    }:
        raise CampaignIntegrationError(f"{label} is not closed")
    _id(value["work_unit_id"], f"{label}.work_unit_id")
    if type(value["merge_order"]) is not int or value["merge_order"] < 1:
        raise CampaignIntegrationError(f"{label}.merge_order must be positive")
    _digest(value["provider_receipt_sha256"], f"{label}.provider_receipt_sha256")
    _candidate(value["candidate"], f"{label}.candidate")
    if "dependency_base" in value:
        try:
            dependency = validate_dependency_base(
                value["dependency_base"],
                repository_common_dir_sha256=value["candidate"]["repository_common_dir_sha256"],
            )
        except Exception as exc:
            raise CampaignIntegrationError(f"{label}.dependency_base is invalid: {exc}") from exc
        if dependency["base_oid"] != value["candidate"]["base_oid"]:
            raise CampaignIntegrationError(f"{label}.dependency_base does not match candidate base")
    return value


def _absolute_directory(value: Path | str, label: str) -> Path:
    requested = Path(value).expanduser()
    if not requested.is_absolute() or "\x00" in str(requested):
        raise CampaignIntegrationError(f"{label} must be an explicit absolute path")
    try:
        if requested.is_symlink():
            raise CampaignIntegrationError(f"{label} may not be a symlink")
        resolved = requested.resolve(strict=True)
        observed = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise CampaignIntegrationError(f"{label} is unavailable: {requested}") from exc
    if (
        resolved != requested
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) & 0o077
    ):
        raise CampaignIntegrationError(f"{label} must be an owner-only physical directory")
    return resolved


def _private_directory(value: Path | str, *, create: bool) -> Path:
    requested = Path(value).expanduser()
    if not requested.is_absolute() or any(part in {".", ".."} for part in requested.parts):
        raise CampaignIntegrationError("integration state root must be an explicit absolute path")
    assert_writable(requested)
    if create:
        try:
            requested.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise CampaignIntegrationError(
                f"cannot create integration state root: {requested}"
            ) from exc
    try:
        if requested.is_symlink():
            raise CampaignIntegrationError("integration state root may not be a symlink")
        resolved = requested.resolve(strict=True)
        observed = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise CampaignIntegrationError(
            f"integration state root is unavailable: {requested}"
        ) from exc
    if (
        resolved != requested
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) & 0o077
    ):
        raise CampaignIntegrationError("integration state root must be owner-only")
    return resolved


def _controller_lock_root() -> Path:
    # Environment-selected roots can split a repository-wide lock across different inodes.
    # The OS account home is stable across callers and outside provider worktree write access.
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".local/state/bearhug/operation-locks"


@contextmanager
def _integration_operation_lock(
    state_root: Path | str, integration_id: str, target: Path | str
):
    """Exclude Git mutation and acceptance commands across all repository processes."""

    _id(integration_id, "integration_id")
    _private_directory(state_root, create=True)
    target_path = _absolute_directory(target, "integration target")
    try:
        common_dir, _ = _common_dir(target_path)
    except ProviderReceiptError as exc:
        raise CampaignIntegrationError("cannot resolve integration repository lock") from exc
    # The lock identity is derived from the common directory, but the lock inode itself belongs to
    # the controller.  A provider which can edit a checkout (or even a compromised Git helper)
    # must never be able to replace the process-wide serialization primitive.
    lock_root = _controller_lock_root()
    try:
        lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_root = lock_root.resolve(strict=True)
        lock_root_stat = lock_root.stat(follow_symlinks=False)
    except OSError as exc:
        raise CampaignIntegrationError(f"cannot create controller lock root: {exc}") from exc
    if (
        not stat.S_ISDIR(lock_root_stat.st_mode)
        or lock_root_stat.st_uid != os.geteuid()
        or stat.S_IMODE(lock_root_stat.st_mode) & 0o077
    ):
        raise CampaignIntegrationError("controller lock root must be an owner-only directory")
    repository_id = hashlib.sha256(os.fsencode(common_dir)).hexdigest()
    path = lock_root / f"integration-{repository_id}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CampaignIntegrationError(f"cannot open integration operation lock: {exc}") from exc
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) & 0o077
        ):
            raise CampaignIntegrationError("integration operation lock is not owner-only")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CampaignIntegrationError(
                "integration operation is already active in another process"
            ) from exc
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _closed_json(raw: bytes, path: Path) -> dict[str, Any]:
    if not raw.endswith(b"\n"):
        raise CampaignIntegrationError(f"integration receipt is not newline-terminated: {path}")

    def closed(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CampaignIntegrationError(f"integration receipt repeats key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=closed)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignIntegrationError(f"integration receipt is invalid JSON: {path}") from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise CampaignIntegrationError(f"integration receipt is not canonical JSON: {path}")
    return value


def _read_record(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CampaignIntegrationError(f"cannot open integration receipt {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_size > _MAX_RECEIPT_BYTES
        ):
            raise CampaignIntegrationError("integration receipt is not a bounded owner-only file")
        raw = bytearray()
        while len(raw) <= _MAX_RECEIPT_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_RECEIPT_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if len(raw) > _MAX_RECEIPT_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise CampaignIntegrationError("integration receipt changed while read")
        return _closed_json(bytes(raw), path)
    finally:
        os.close(descriptor)


def _validate_merge(value: Any, index: int) -> dict[str, Any]:
    label = f"merges[{index}]"
    if not isinstance(value, dict) or set(value) != _MERGE_FIELDS:
        raise CampaignIntegrationError(f"{label} is not closed")
    _id(value["work_unit_id"], f"{label}.work_unit_id")
    if type(value["merge_order"]) is not int or value["merge_order"] < 1:
        raise CampaignIntegrationError(f"{label}.merge_order must be positive")
    for field in ("head_oid", "before_head_oid"):
        _oid(value[field], f"{label}.{field}")
    if value["after_head_oid"] is not None:
        _oid(value["after_head_oid"], f"{label}.after_head_oid")
    if value["status"] not in {"merged", "conflict", "failed"}:
        raise CampaignIntegrationError(f"{label}.status is unsupported")
    if value["status"] == "merged" and value["after_head_oid"] is None:
        raise CampaignIntegrationError(f"{label}.merged step lacks after_head_oid")
    if value["status"] != "merged" and value["after_head_oid"] is not None:
        raise CampaignIntegrationError(f"{label}.failed step has after_head_oid")
    _digest(value["stdout_sha256"], f"{label}.stdout_sha256")
    _digest(value["stderr_sha256"], f"{label}.stderr_sha256")
    return value


def _validate_check(value: Any, index: int) -> dict[str, Any]:
    label = f"checks[{index}]"
    if not isinstance(value, dict) or set(value) != _CHECK_FIELDS:
        raise CampaignIntegrationError(f"{label} is not closed")
    if value["ordinal"] != index:
        raise CampaignIntegrationError(f"{label}.ordinal is not positional")
    _digest(value["argv_sha256"], f"{label}.argv_sha256")
    if value["returncode"] is not None and type(value["returncode"]) is not int:
        raise CampaignIntegrationError(f"{label}.returncode must be an integer or null")
    if value["status"] not in {"passed", "failed", "timeout", "launch_error", "not_run"}:
        raise CampaignIntegrationError(f"{label}.status is unsupported")
    _digest(value["stdout_sha256"], f"{label}.stdout_sha256")
    _digest(value["stderr_sha256"], f"{label}.stderr_sha256")
    return value


def _validate_review_ref(value: Any, index: int) -> dict[str, Any]:
    label = f"reviews[{index}]"
    if not isinstance(value, dict) or set(value) != _REVIEW_REF_FIELDS:
        raise CampaignIntegrationError(f"{label} is not closed")
    _id(value["work_unit_id"], f"{label}.work_unit_id")
    _id(value["review_id"], f"{label}.review_id")
    _digest(value["receipt_sha256"], f"{label}.receipt_sha256")
    return value


def _validate_attempt(value: Any) -> dict[str, Any]:
    """Validate the immutable input fence used by ordinary integration recovery."""

    if not isinstance(value, dict) or set(value) != _ATTEMPT_FIELDS:
        raise CampaignIntegrationError("integration attempt has missing or unknown fields")
    if value["schema_version"] != "1" or value["record_kind"] != "campaign_integration_attempt":
        raise CampaignIntegrationError("unsupported integration attempt schema or kind")
    for field in ("campaign_id", "run_id", "integration_id", "integrator_id"):
        _id(value[field], field)
    target = value["target"]
    if (
        not isinstance(target, str)
        or not target.startswith("/")
        or "\x00" in target
        or str(Path(target)) != target
    ):
        raise CampaignIntegrationError("integration attempt target is not canonical")
    if not isinstance(value["branch"], str) or not value["branch"]:
        raise CampaignIntegrationError("integration attempt branch is invalid")
    _digest(value["repository_common_dir_sha256"], "attempt.repository_common_dir_sha256")
    _oid(value["base_oid"], "attempt.base_oid")
    _oid(value["initial_tree_oid"], "attempt.initial_tree_oid")
    inputs = value["inputs"]
    if not isinstance(inputs, list) or not inputs:
        raise CampaignIntegrationError("integration attempt must name inputs")
    checked_inputs = [_input(item, index) for index, item in enumerate(inputs)]
    if [item["merge_order"] for item in checked_inputs] != list(range(1, len(inputs) + 1)):
        raise CampaignIntegrationError("integration attempt inputs are not ordered")
    reviews = value["reviews"]
    if not isinstance(reviews, list):
        raise CampaignIntegrationError("integration attempt reviews must be an array")
    [_validate_review_ref(item, index) for index, item in enumerate(reviews)]
    checks = value["checks"]
    if not isinstance(checks, list) or not checks:
        raise CampaignIntegrationError("integration attempt checks must be an array")
    for index, command in enumerate(checks):
        _command(command, label=f"attempt.checks[{index}]")
    claim = value["claim"]
    if claim is not None:
        if not isinstance(claim, dict):
            raise CampaignIntegrationError("integration attempt claim is not an object")
        from bearhug.campaign.claims import validate_claim_record

        try:
            validate_claim_record(claim)
        except ValueError as exc:
            raise CampaignIntegrationError("integration attempt claim is invalid") from exc
        expected_claim = {
            "campaign_id": value["campaign_id"],
            "claimant_id": value["integrator_id"],
            "role": "integrator",
            "branch": value["branch"],
            "base_oid": value["base_oid"],
            "repository_common_dir_sha256": value["repository_common_dir_sha256"],
            "worktree_sha256": worktree_sha256(value["target"]),
        }
        if any(claim[key] != expected for key, expected in expected_claim.items()):
            raise CampaignIntegrationError(
                "integration attempt claim differs from its target authority"
            )
    claimed = value["content_sha256"]
    _digest(claimed, "attempt.content_sha256")
    if claimed != _sha256(_canonical(value, omit_digest=True)):
        raise CampaignIntegrationError("integration attempt content digest changed")
    return value


def _progress_value(
    integration_id: str,
    sequence: int,
    previous_sha256: str,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(event, Mapping) or not event:
        raise CampaignIntegrationError("integration progress event is empty")
    value = {
        "schema_version": "1",
        "record_kind": "campaign_integration_progress",
        "integration_id": integration_id,
        "sequence": sequence,
        "previous_sha256": previous_sha256,
        "event": dict(event),
    }
    value["content_sha256"] = _sha256(_canonical(value, omit_digest=True))
    return value


def validate_integration_receipt(value: Any) -> dict[str, Any]:
    """Validate one closed integration receipt, including its final candidate claim."""

    if not isinstance(value, dict) or set(value) != _RECEIPT_FIELDS:
        raise CampaignIntegrationError("integration receipt has missing or unknown fields")
    if value["schema_version"] != "1" or value["record_kind"] != "campaign_integration_receipt":
        raise CampaignIntegrationError("unsupported integration receipt schema or kind")
    for field in ("campaign_id", "run_id", "integration_id", "integrator_id"):
        _id(value[field], field)
    target = value["target"]
    if (
        not isinstance(target, str)
        or not target.startswith("/")
        or "\x00" in target
        or str(Path(target)) != target
    ):
        raise CampaignIntegrationError("target must be a canonical absolute path")
    if not isinstance(value["branch"], str) or not value["branch"] or "\x00" in value["branch"]:
        raise CampaignIntegrationError("branch must be non-empty text")
    _digest(value["repository_common_dir_sha256"], "repository_common_dir_sha256")
    for field in ("base_oid", "initial_tree_oid"):
        _oid(value[field], field)
    inputs = value["inputs"]
    if not isinstance(inputs, list) or not inputs:
        raise CampaignIntegrationError("integration receipt must name inputs")
    checked_inputs = [_input(item, index) for index, item in enumerate(inputs)]
    if [item["merge_order"] for item in checked_inputs] != sorted(
        item["merge_order"] for item in checked_inputs
    ) or len({item["merge_order"] for item in checked_inputs}) != len(checked_inputs):
        raise CampaignIntegrationError("integration inputs must be in unique merge order")
    if len({item["work_unit_id"] for item in checked_inputs}) != len(checked_inputs):
        raise CampaignIntegrationError("integration inputs repeat a work unit")
    available_heads = {value["base_oid"]}
    for index, item in enumerate(checked_inputs):
        if item["merge_order"] != index + 1:
            raise CampaignIntegrationError("integration inputs must start at merge order one")
        candidate = item["candidate"]
        if candidate["repository_common_dir_sha256"] != value["repository_common_dir_sha256"]:
            raise CampaignIntegrationError("integration input candidate is bound to another base")
        dependency = item.get("dependency_base")
        if dependency is None:
            if candidate["base_oid"] != value["base_oid"]:
                raise CampaignIntegrationError(
                    "dependent integration input lacks its selected dependency base"
                )
        else:
            if candidate["base_oid"] == value["base_oid"]:
                raise CampaignIntegrationError(
                    "integration dependency base must describe an inherited base"
                )
            provenance_heads = {row["candidate"]["head_oid"] for row in dependency["provenance"]}
            if not provenance_heads <= available_heads:
                raise CampaignIntegrationError(
                    "integration dependency base names an input that is not already merged"
                )
        available_heads.add(candidate["head_oid"])
    if len({item["candidate"]["head_oid"] for item in checked_inputs}) != len(checked_inputs):
        raise CampaignIntegrationError("integration inputs repeat a candidate head")
    reviews = value["reviews"]
    if not isinstance(reviews, list):
        raise CampaignIntegrationError("integration reviews must be an array")
    checked_reviews = [_validate_review_ref(item, index) for index, item in enumerate(reviews)]
    if len({item["review_id"] for item in checked_reviews}) != len(checked_reviews):
        raise CampaignIntegrationError("integration reviews repeat a review id")
    input_units = {item["work_unit_id"] for item in checked_inputs}
    if any(item["work_unit_id"] not in input_units for item in checked_reviews):
        raise CampaignIntegrationError("integration review names an unknown work unit")
    merges = value["merges"]
    if not isinstance(merges, list) or len(merges) > len(checked_inputs):
        raise CampaignIntegrationError("integration merge steps are inconsistent")
    checked_merges = [_validate_merge(item, index) for index, item in enumerate(merges)]
    for index, merge in enumerate(checked_merges):
        expected = checked_inputs[index]
        if (
            merge["work_unit_id"] != expected["work_unit_id"]
            or merge["merge_order"] != expected["merge_order"]
            or merge["head_oid"] != expected["candidate"]["head_oid"]
        ):
            raise CampaignIntegrationError("integration merge step differs from declared input")
    first_failed = next(
        (index for index, merge in enumerate(checked_merges) if merge["status"] != "merged"),
        None,
    )
    if first_failed is not None and any(
        merge["status"] == "merged" for merge in checked_merges[first_failed + 1 :]
    ):
        raise CampaignIntegrationError("integration merge steps continue after a failed step")
    checks = value["checks"]
    if not isinstance(checks, list) or not checks:
        raise CampaignIntegrationError("integration checks must be an array")
    [_validate_check(item, index) for index, item in enumerate(checks)]
    if (value["final_head_oid"] is None) != (value["final_tree_oid"] is None):
        raise CampaignIntegrationError("final Git state must include both head and tree")
    if value["final_head_oid"] is not None:
        _oid(value["final_head_oid"], "final_head_oid")
    if value["final_tree_oid"] is not None:
        _oid(value["final_tree_oid"], "final_tree_oid")
    final_candidate = value["candidate"]
    if final_candidate is not None:
        final_candidate = _candidate(final_candidate, "candidate")
        if (
            final_candidate["repository_common_dir_sha256"] != value["repository_common_dir_sha256"]
            or final_candidate["base_oid"] != value["base_oid"]
            or final_candidate["head_oid"] != value["final_head_oid"]
            or final_candidate["tree_oid"] != value["final_tree_oid"]
        ):
            raise CampaignIntegrationError("final candidate is not bound to final Git state")
    if value["status"] not in {"passed", "blocked"}:
        raise CampaignIntegrationError("integration status is unsupported")
    blockers = value["blockers"]
    if (
        not isinstance(blockers, list)
        or not all(isinstance(item, str) and item for item in blockers)
        or len(blockers) != len(set(blockers))
    ):
        raise CampaignIntegrationError("integration blockers must be a unique string array")
    if value["status"] == "passed":
        if blockers or final_candidate is None or len(checked_merges) != len(checked_inputs):
            raise CampaignIntegrationError("passed integration lacks complete evidence")
        if any(item["status"] != "merged" for item in checked_merges):
            raise CampaignIntegrationError("passed integration contains a failed merge")
        if any(item["status"] != "passed" for item in checks):
            raise CampaignIntegrationError("passed integration contains a failed check")
    elif not blockers or final_candidate is not None:
        raise CampaignIntegrationError("blocked integration must name blockers and no candidate")
    claimed_digest = value["content_sha256"]
    _digest(claimed_digest, "content_sha256")
    if claimed_digest != _sha256(_canonical(value, omit_digest=True)):
        raise CampaignIntegrationError("integration receipt content digest changed")
    return value


class CampaignIntegrationStore:
    """Owner-only, create-only storage for integration receipts."""

    def __init__(self, root: Path | str) -> None:
        self.root = _private_directory(root, create=True)

    def _path(self, integration_id: str) -> Path:
        _id(integration_id, "integration_id")
        return self.root / f"{integration_id}.json"

    def attempt_path(self, integration_id: str) -> Path:
        _id(integration_id, "integration_id")
        return self.root / f"{integration_id}.attempt.json"

    def progress_root(self, integration_id: str) -> Path:
        _id(integration_id, "integration_id")
        target = self.root / f"{integration_id}.progress"
        if target.is_symlink():
            raise CampaignIntegrationError("integration progress root may not be a symlink")
        target.mkdir(mode=0o700, exist_ok=True)
        if not target.is_dir():
            raise CampaignIntegrationError("integration progress root is not a directory")
        return target

    def write_attempt(self, value: Mapping[str, Any]) -> Path:
        attempt = _validate_attempt(dict(value))
        target = self.attempt_path(attempt["integration_id"])
        raw = _canonical(attempt)
        if len(raw) > _MAX_RECEIPT_BYTES:
            raise CampaignIntegrationError("integration attempt exceeds its byte bound")
        if target.exists() or target.is_symlink():
            existing = self.read_attempt(attempt["integration_id"])
            if existing != attempt:
                raise CampaignIntegrationError("integration attempt is already bound differently")
            return target
        self._create(target, raw, "integration attempt")
        return target

    def read_attempt(self, integration_id: str) -> dict[str, Any]:
        target = self.attempt_path(integration_id)
        if target.is_symlink() or not target.is_file():
            raise CampaignIntegrationError(f"integration attempt is unavailable: {target}")
        value = _validate_attempt(_read_record(target))
        if value["integration_id"] != integration_id:
            raise CampaignIntegrationError("integration attempt filename does not bind its id")
        return value

    def append_progress(self, integration_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        root = self.progress_root(integration_id)
        paths = sorted(root.glob("*.json"))
        previous = "0" * 64
        if paths:
            for expected, path in enumerate(paths):
                if path.is_symlink() or not path.is_file():
                    raise CampaignIntegrationError("integration progress custody is not physical")
                value = self._read_progress(path)
                if value["sequence"] != expected or value["previous_sha256"] != previous:
                    raise CampaignIntegrationError("integration progress chain changed")
                previous = value["content_sha256"]
        value = _progress_value(integration_id, len(paths), previous, event)
        path = root / f"{value['sequence']:020d}-{value['content_sha256']}.json"
        self._create(path, _canonical(value), "integration progress")
        return value

    def read_progress(self, integration_id: str) -> list[dict[str, Any]]:
        root = self.progress_root(integration_id)
        paths = sorted(root.glob("*.json"))
        values = []
        previous = "0" * 64
        for sequence, path in enumerate(paths):
            value = self._read_progress(path)
            if (
                value["integration_id"] != integration_id
                or value["sequence"] != sequence
                or value["previous_sha256"] != previous
                or path.name != f"{sequence:020d}-{value['content_sha256']}.json"
            ):
                raise CampaignIntegrationError("integration progress chain changed")
            previous = value["content_sha256"]
            values.append(value)
        return values

    @staticmethod
    def _read_progress(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECEIPT_BYTES:
            raise CampaignIntegrationError("integration progress is not a bounded regular file")
        raw = path.read_bytes()
        parsed = _closed_json(raw, path)
        if (
            not isinstance(parsed, dict)
            or set(parsed) != _PROGRESS_FIELDS
            or _canonical(parsed) != raw
            or parsed["record_kind"] != "campaign_integration_progress"
            or not isinstance(parsed["sequence"], int)
            or parsed["sequence"] < 0
            or not isinstance(parsed["event"], dict)
            or not isinstance(parsed["previous_sha256"], str)
            or not isinstance(parsed["content_sha256"], str)
            or parsed["content_sha256"] != _sha256(_canonical(parsed, omit_digest=True))
        ):
            raise CampaignIntegrationError("integration progress record is malformed")
        return parsed

    def _create(self, target: Path, raw: bytes, label: str) -> None:
        temporary = self.root / f".{target.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
        if target.parent != self.root:
            temporary = target.parent / f".{target.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
        target.parent.mkdir(mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise CampaignIntegrationError(f"refusing to replace {label} {target}") from exc
            directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise CampaignIntegrationError(f"cannot publish {label} {target}: {exc}") from exc
        finally:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()

    def write(self, value: dict[str, Any]) -> Path:
        receipt = validate_integration_receipt(value)
        target = self._path(receipt["integration_id"])
        raw = _canonical(receipt)
        if len(raw) > _MAX_RECEIPT_BYTES:
            raise CampaignIntegrationError("integration receipt exceeds its byte bound")
        if target.exists() or target.is_symlink():
            observed = self.read(receipt["integration_id"])
            if observed != receipt:
                raise CampaignIntegrationError("integration receipt is already bound differently")
            return target
        temporary = self.root / f".{target.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise CampaignIntegrationError(
                    f"refusing to replace integration receipt {target}"
                ) from exc
            directory = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise CampaignIntegrationError(
                f"cannot publish integration receipt {target}: {exc}"
            ) from exc
        finally:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()
        return target

    def read(self, integration_id: str) -> dict[str, Any]:
        target = self._path(integration_id)
        if target.is_symlink() or not target.is_file():
            raise CampaignIntegrationError(f"integration receipt is unavailable: {target}")
        value = validate_integration_receipt(_read_record(target))
        if value["integration_id"] != integration_id:
            raise CampaignIntegrationError("integration receipt filename does not bind its id")
        return value


@dataclass(frozen=True, slots=True)
class CampaignIntegrationResult:
    receipt: dict[str, Any]
    receipt_path: Path


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    status: str


CommandExecutor = Callable[..., Any]


def _bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return str(value).encode("utf-8", errors="replace")


def _command(argv: Sequence[str], *, label: str) -> tuple[str, ...]:
    if not isinstance(argv, (tuple, list)) or not argv or len(argv) > _MAX_COMMAND_ARGUMENTS:
        raise CampaignIntegrationError(f"{label} must be a non-empty bounded argv")
    result: list[str] = []
    for index, argument in enumerate(argv):
        if (
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument.encode("utf-8")) > _MAX_ARGUMENT_BYTES
        ):
            raise CampaignIntegrationError(f"{label}[{index}] is not bounded text")
        result.append(argument)
    return tuple(result)


def _run_command(
    executor: CommandExecutor,
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout: float,
) -> _CommandResult:
    try:
        from bearhug.processes import run_owned

        run = run_owned if executor is subprocess.run else executor
        result = run(
            argv,
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _CommandResult(None, _bytes(exc.stdout), _bytes(exc.stderr), "timeout")
    except OSError as exc:
        return _CommandResult(None, b"", str(exc).encode(), "launch_error")
    returncode = getattr(result, "returncode", None)
    if type(returncode) is not int:
        return _CommandResult(
            None,
            _bytes(getattr(result, "stdout", b"")),
            _bytes(getattr(result, "stderr", b"")),
            "launch_error",
        )
    stdout = _bytes(getattr(result, "stdout", b""))
    stderr = _bytes(getattr(result, "stderr", b""))
    return _CommandResult(returncode, stdout, stderr, "passed" if returncode == 0 else "failed")


def _git(target: Path, *args: str, timeout: float = 60.0) -> _CommandResult:
    from bearhug.host_git import run_git

    try:
        result = run_git(
            target, "-c", "user.name=Bear Hug", "-c", "user.email=bearhug@localhost",
            *args, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _CommandResult(None, _bytes(exc.stdout), _bytes(exc.stderr), "timeout")
    except OSError as exc:
        return _CommandResult(None, b"", str(exc).encode(), "launch_error")
    return _CommandResult(
        result.returncode, result.stdout, result.stderr,
        "passed" if result.returncode == 0 else "failed",
    )


def _git_output(target: Path, *args: str) -> str:
    result = _git(target, *args)
    if result.status != "passed":
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CampaignIntegrationError(f"git {' '.join(args)} failed: {detail}")
    try:
        return result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise CampaignIntegrationError(f"git {' '.join(args)} returned non-UTF-8 output") from exc


def _record_command(result: _CommandResult, argv: tuple[str, ...], ordinal: int) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "argv_sha256": _sha256(
            json.dumps(list(argv), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ),
        "returncode": result.returncode,
        "status": result.status,
        "stdout_sha256": _sha256(result.stdout),
        "stderr_sha256": _sha256(result.stderr),
    }


def _record_merge(
    item: Mapping[str, Any],
    *,
    before: str,
    after: str | None,
    result: _CommandResult,
) -> dict[str, Any]:
    return {
        "work_unit_id": item["work_unit_id"],
        "merge_order": item["merge_order"],
        "head_oid": item["candidate"]["head_oid"],
        "before_head_oid": before,
        "after_head_oid": after,
        "status": (
            "merged"
            if result.status == "passed"
            else "conflict"
            if result.returncode is not None
            else "failed"
        ),
        "stdout_sha256": _sha256(result.stdout),
        "stderr_sha256": _sha256(result.stderr),
    }


def _content_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["content_sha256"] = _sha256(_canonical(result, omit_digest=True))
    return result


def _verify_candidate_git(target: Path, candidate: Mapping[str, Any]) -> None:
    head, base = candidate["head_oid"], candidate["base_oid"]
    if _git_output(target, "rev-parse", "--verify", f"{head}^{{commit}}") != head:
        raise CampaignIntegrationError("integration candidate head does not resolve exactly")
    if _git_output(target, "rev-parse", "--verify", f"{head}^{{tree}}") != candidate["tree_oid"]:
        raise CampaignIntegrationError("integration candidate tree differs from Git")
    if _git(target, "merge-base", "--is-ancestor", base, head).status != "passed":
        raise CampaignIntegrationError("integration candidate does not descend from its base")
    patch = _git(
        target,
        "diff-tree",
        "--no-commit-id",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-textconv",
        "-r",
        base,
        head,
        "--",
    )
    if patch.status != "passed" or _sha256(patch.stdout) != candidate["patch_sha256"]:
        raise CampaignIntegrationError("integration candidate patch differs from Git")


def _verify_merge_tree(
    target_path: Path, item: Mapping[str, Any], merge: Mapping[str, Any]
) -> None:
    if merge["status"] != "merged":
        return
    before, after = merge["before_head_oid"], merge["after_head_oid"]
    if after == before:
        ancestry = _git(
            target_path, "merge-base", "--is-ancestor", item["candidate"]["head_oid"], after
        )
        if ancestry.status != "passed":
            raise CampaignIntegrationError("integration no-op merge is not an ancestor")
        return
    parent = _git_output(target_path, "rev-parse", "--verify", f"{after}^1")
    second = _git_output(target_path, "rev-parse", "--verify", f"{after}^2")
    parents = _git_output(target_path, "rev-list", "--parents", "-n", "1", after).split()
    if len(parents) != 3 or parent != before or second != item["candidate"]["head_oid"]:
        raise CampaignIntegrationError("integration merge parent custody changed")
    expected_tree = _git_output(
        target_path,
        "merge-tree",
        "--write-tree",
        before,
        item["candidate"]["head_oid"],
    )
    actual_tree = _git_output(target_path, "rev-parse", "--verify", f"{after}^{{tree}}")
    if expected_tree != actual_tree:
        raise CampaignIntegrationError("integration merge tree custody changed")


def verify_integration_git(receipt: Mapping[str, Any]) -> None:
    """Reopen passed integration custody against its actual Git objects and worktree."""
    value = validate_integration_receipt(dict(receipt))
    if value["status"] != "passed":
        raise CampaignIntegrationError("integration Git verification requires a passed receipt")
    target = _absolute_directory(value["target"], "integration receipt target")
    actual = capture_launch_repository(target)
    if (
        actual.repository_common_dir_sha256 != value["repository_common_dir_sha256"]
        or actual.head_oid != value["final_head_oid"]
        or actual.tree_oid != value["final_tree_oid"]
        or _git_output(target, "symbolic-ref", "--quiet", "--short", "HEAD") != value["branch"]
        or _git_output(target, "rev-parse", "--verify", f"{value['base_oid']}^{{tree}}")
        != value["initial_tree_oid"]
    ):
        raise CampaignIntegrationError("integration receipt Git custody changed")
    cursor = value["base_oid"]
    for item, merge in zip(value["inputs"], value["merges"], strict=True):
        _verify_candidate_git(target, item["candidate"])
        if item.get("dependency_base") is not None:
            verify_dependency_base(
                target, item["dependency_base"], original_base_oid=value["base_oid"]
            )
        if merge["before_head_oid"] != cursor:
            raise CampaignIntegrationError(
                "integration merge chain differs from its declared order"
            )
        _verify_merge_tree(target, item, merge)
        cursor = merge["after_head_oid"]
    if cursor != value["final_head_oid"]:
        raise CampaignIntegrationError("integration candidate changed after its final merge")
    _verify_candidate_git(target, value["candidate"])


def integrate_campaign_candidates(
    *,
    state_root: Path | str,
    campaign_id: str,
    run_id: str,
    integration_id: str,
    integrator_id: str,
    target: Path | str,
    base_oid: str,
    candidates: Sequence[Mapping[str, Any]],
    checks: Sequence[Sequence[str]],
    check_timeout_s: float = 28800.0,
    _review_refs: Sequence[Mapping[str, Any]] = (),
    _claim: Mapping[str, Any] | None = None,
) -> CampaignIntegrationResult:
    """Merge candidates while holding the process-wide target/integration fence."""

    with _integration_operation_lock(state_root, integration_id, target):
        return _integrate_campaign_candidates_unlocked(
            state_root=state_root,
            campaign_id=campaign_id,
            run_id=run_id,
            integration_id=integration_id,
            integrator_id=integrator_id,
            target=target,
            base_oid=base_oid,
            candidates=candidates,
            checks=checks,
            check_timeout_s=check_timeout_s,
            _review_refs=_review_refs,
            _claim=_claim,
        )


def _integrate_campaign_candidates_unlocked(
    *,
    state_root: Path | str,
    campaign_id: str,
    run_id: str,
    integration_id: str,
    integrator_id: str,
    target: Path | str,
    base_oid: str,
    candidates: Sequence[Mapping[str, Any]],
    checks: Sequence[Sequence[str]],
    check_timeout_s: float = 28800.0,
    _review_refs: Sequence[Mapping[str, Any]] = (),
    _claim: Mapping[str, Any] | None = None,
) -> CampaignIntegrationResult:
    """Merge declared candidates in order and publish a closed integration result."""

    for field, value in (
        ("campaign_id", campaign_id),
        ("run_id", run_id),
        ("integration_id", integration_id),
        ("integrator_id", integrator_id),
    ):
        _id(value, field)
    _oid(base_oid, "base_oid")
    if (
        isinstance(check_timeout_s, bool)
        or not isinstance(check_timeout_s, (int, float))
        or not 0 < check_timeout_s <= _MAX_CHECK_TIMEOUT_SECONDS
    ):
        raise CampaignIntegrationError("check_timeout_s is outside its bounded range")
    target_path = _absolute_directory(target, "integration target")
    requested_state = Path(state_root).expanduser()
    if not requested_state.is_absolute() or any(
        part in {".", ".."} for part in requested_state.parts
    ):
        raise CampaignIntegrationError("integration state root must be an explicit absolute path")
    state = _private_directory(requested_state, create=True)
    inputs = [_input(item, index) for index, item in enumerate(candidates)]
    if not inputs:
        raise CampaignIntegrationError("at least one candidate is required")
    if [item["merge_order"] for item in inputs] != sorted(item["merge_order"] for item in inputs):
        raise CampaignIntegrationError("candidates must be supplied in merge order")
    command_checks = [
        _command(command, label=f"checks[{index}]") for index, command in enumerate(checks)
    ]
    if not command_checks:
        raise CampaignIntegrationError("at least one acceptance check is required")
    review_refs = [_validate_review_ref(item, index) for index, item in enumerate(_review_refs)]
    store = CampaignIntegrationStore(state / "integration-receipts")
    if store._path(integration_id).exists():
        existing = store.read(integration_id)
        if (
            existing["campaign_id"] != campaign_id
            or existing["run_id"] != run_id
            or existing["integrator_id"] != integrator_id
            or existing["target"] != str(target_path)
            or existing["base_oid"] != base_oid
            or existing["inputs"] != [dict(item) for item in inputs]
            or existing["reviews"] != [dict(item) for item in review_refs]
            or [item["argv_sha256"] for item in existing["checks"]]
            != [
                _sha256(
                    json.dumps(list(command), ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                )
                for command in command_checks
            ]
        ):
            raise CampaignIntegrationError("integration receipt is already bound differently")
        if existing["status"] == "passed":
            verify_integration_git(existing)
        return CampaignIntegrationResult(existing, store._path(integration_id))

    try:
        launch = capture_launch_repository(target_path)
    except ProviderReceiptError as exc:
        raise CampaignIntegrationError(
            f"integration target is not a clean Git worktree: {exc}"
        ) from exc
    if launch.head_oid != base_oid and not store.attempt_path(integration_id).exists():
        raise CampaignIntegrationError("integration target HEAD does not match the explicit base")
    branch = _git_output(target_path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        raise CampaignIntegrationError("integration target must be on a named branch")
    if store.attempt_path(integration_id).exists():
        recorded_attempt = store.read_attempt(integration_id)
        if recorded_attempt["branch"] != branch:
            raise CampaignIntegrationError("integration target branch differs from its attempt")
    available_heads = {base_oid}
    for index, item in enumerate(inputs):
        candidate = item["candidate"]
        if candidate["repository_common_dir_sha256"] != launch.repository_common_dir_sha256:
            raise CampaignIntegrationError(
                f"inputs[{index}] is bound to another repository or base"
            )
        dependency = item.get("dependency_base")
        if dependency is None:
            if candidate["base_oid"] != base_oid:
                raise CampaignIntegrationError(
                    f"inputs[{index}] lacks its selected dependency base"
                )
        else:
            if candidate["base_oid"] == base_oid:
                raise CampaignIntegrationError(
                    f"inputs[{index}] dependency base does not inherit from another input"
                )
            provenance_heads = {row["candidate"]["head_oid"] for row in dependency["provenance"]}
            if not provenance_heads <= available_heads:
                raise CampaignIntegrationError(
                    f"inputs[{index}] dependency base names an input not merged earlier"
                )
            try:
                verify_dependency_base(
                    target_path,
                    dependency,
                    original_base_oid=base_oid,
                )
            except Exception as exc:
                raise CampaignIntegrationError(
                    f"inputs[{index}] selected dependency base is not verified: {exc}"
                ) from exc
        resolved_head = _git_output(
            target_path,
            "rev-parse",
            "--verify",
            f"{candidate['head_oid']}^{{commit}}",
        )
        if resolved_head != candidate["head_oid"]:
            raise CampaignIntegrationError(f"inputs[{index}] head does not resolve exactly")
        resolved_tree = _git_output(
            target_path, "rev-parse", "--verify", f"{candidate['head_oid']}^{{tree}}"
        )
        if resolved_tree != candidate["tree_oid"]:
            raise CampaignIntegrationError(f"inputs[{index}] tree does not match its candidate")
        ancestry = _git(target_path, "merge-base", "--is-ancestor", base_oid, candidate["head_oid"])
        if ancestry.status != "passed":
            raise CampaignIntegrationError(f"inputs[{index}] is not a descendant of the base")
        _verify_candidate_git(target_path, candidate)
        available_heads.add(candidate["head_oid"])

    attempt = _content_receipt(
        {
            "schema_version": "1",
            "record_kind": "campaign_integration_attempt",
            "campaign_id": campaign_id,
            "run_id": run_id,
            "integration_id": integration_id,
            "integrator_id": integrator_id,
            "target": str(target_path),
            "branch": branch,
            "repository_common_dir_sha256": launch.repository_common_dir_sha256,
            "base_oid": base_oid,
            "initial_tree_oid": launch.tree_oid,
            "inputs": [dict(item) for item in inputs],
            "reviews": [dict(item) for item in review_refs],
            "checks": [list(command) for command in command_checks],
            "claim": None if _claim is None else dict(_claim),
        }
    )
    if store.attempt_path(integration_id).exists():
        existing_attempt = store.read_attempt(integration_id)
        if (
            existing_attempt["campaign_id"] != campaign_id
            or existing_attempt["run_id"] != run_id
            or existing_attempt["integrator_id"] != integrator_id
            or existing_attempt["target"] != str(target_path)
            or existing_attempt["branch"] != branch
            or existing_attempt["repository_common_dir_sha256"]
            != launch.repository_common_dir_sha256
            or existing_attempt["base_oid"] != base_oid
            or existing_attempt["inputs"] != [dict(item) for item in inputs]
            or existing_attempt["reviews"] != [dict(item) for item in review_refs]
            or existing_attempt["checks"] != [list(command) for command in command_checks]
            or existing_attempt["claim"] != (None if _claim is None else dict(_claim))
        ):
            raise CampaignIntegrationError("integration attempt is already bound differently")
        if existing_attempt["initial_tree_oid"] != _git_output(
            target_path, "rev-parse", "--verify", f"{base_oid}^{{tree}}"
        ):
            raise CampaignIntegrationError("integration attempt initial tree differs from its base")
        attempt = existing_attempt
    else:
        store.write_attempt(attempt)
    launch = replace(launch, head_oid=base_oid, tree_oid=attempt["initial_tree_oid"])
    progress = store.read_progress(integration_id)
    progress_merges = []
    progress_checks = []
    started_check: tuple[int, str] | None = None
    for row in progress:
        event = row["event"]
        if event.get("kind") == "merge":
            if progress_checks:
                raise CampaignIntegrationError("integration progress records a merge after checks")
            if set(event) != {"kind", "merge"}:
                raise CampaignIntegrationError("integration merge progress is malformed")
            progress_merges.append(_validate_merge(event["merge"], len(progress_merges)))
        elif event.get("kind") == "check":
            if len(progress_merges) != len(inputs) or any(
                merge["status"] != "merged" for merge in progress_merges
            ):
                raise CampaignIntegrationError(
                    "integration progress records a check before complete merges"
                )
            if set(event) != {"kind", "check"}:
                raise CampaignIntegrationError("integration check progress is malformed")
            checked = _validate_check(event["check"], len(progress_checks))
            if started_check is not None and started_check != (
                checked["ordinal"],
                checked["argv_sha256"],
            ):
                raise CampaignIntegrationError(
                    "integration check result differs from its start fence"
                )
            progress_checks.append(checked)
            started_check = None
        elif event.get("kind") == "check_started":
            if (
                set(event) != {"kind", "ordinal", "argv_sha256"}
                or type(event["ordinal"]) is not int
                or event["ordinal"] != len(progress_checks)
                or started_check is not None
            ):
                raise CampaignIntegrationError("integration check start progress is malformed")
            _digest(event["argv_sha256"], "integration check start argv_sha256")
            started_check = (event["ordinal"], event["argv_sha256"])
        else:
            raise CampaignIntegrationError("integration progress event is unsupported")
    if started_check is not None:
        raise CampaignIntegrationError(
            "integration acceptance command outcome is unknown; explicit failed recovery "
            "disposition is required"
        )

    def observed_prefix() -> int:
        """Return the number of declared inputs already applied to the target.

        ``git merge --no-ff`` still produces an already-up-to-date no-op when an input is
        already reachable from the current tree.  Recovery must account for those inputs without
        inventing a second parent that Git never created.
        """
        current = _git_output(target_path, "rev-parse", "--verify", "HEAD")
        if current == base_oid:
            return 0
        # Walk the first-parent chain for each possible prefix.  Prefer a real two-parent merge
        # boundary; otherwise accept the input only when Git proves it was already an ancestor at
        # that point.  Trying the longest prefix first preserves trailing no-op inputs.
        for count in range(len(inputs), 0, -1):
            cursor = current
            valid = True
            for index in reversed(range(count)):
                candidate_head = inputs[index]["candidate"]["head_oid"]
                parents = _git(target_path, "rev-parse", "--verify", f"{cursor}^1")
                second = _git(target_path, "rev-parse", "--verify", f"{cursor}^2")
                if parents.status == "passed" and second.status == "passed":
                    parent = parents.stdout.decode("ascii", errors="strict").strip()
                    second_head = second.stdout.decode("ascii", errors="strict").strip()
                    if second_head == candidate_head:
                        cursor = parent
                        continue
                ancestor = _git(
                    target_path,
                    "merge-base",
                    "--is-ancestor",
                    candidate_head,
                    cursor,
                )
                if ancestor.status != "passed":
                    valid = False
                    break
            if valid and cursor == base_oid:
                return count
        raise CampaignIntegrationError(
            "integration target is between declared Git merge boundaries; "
            "explicit recovery disposition is required"
        )

    merged_count = observed_prefix()
    failed_merge_index = next(
        (index for index, row in enumerate(progress_merges) if row["status"] != "merged"), None
    )
    if failed_merge_index is not None:
        if failed_merge_index != merged_count or len(progress_merges) != failed_merge_index + 1:
            raise CampaignIntegrationError("integration progress claims an impossible failed merge")
    elif len(progress_merges) > merged_count:
        raise CampaignIntegrationError("integration progress claims a merge absent from Git")
    merges: list[dict[str, Any]] = list(progress_merges)
    # A crash after Git committed but before its progress record leaves a conclusive merge
    # boundary.  Reconstruct that step with empty command streams rather than merging again.
    if len(merges) < merged_count:
        final_head = _git_output(target_path, "rev-parse", "--verify", "HEAD")
        boundaries: list[tuple[str, str, str]] = []
        cursor = final_head
        for index in reversed(range(merged_count)):
            candidate_head = inputs[index]["candidate"]["head_oid"]
            parent = _git(target_path, "rev-parse", "--verify", f"{cursor}^1")
            second = _git(target_path, "rev-parse", "--verify", f"{cursor}^2")
            if (
                parent.status == "passed"
                and second.status == "passed"
                and second.stdout.decode("ascii").strip() == candidate_head
            ):
                before = parent.stdout.decode("ascii").strip()
            elif (
                _git(target_path, "merge-base", "--is-ancestor", candidate_head, cursor).status
                == "passed"
            ):
                before = cursor
            else:
                raise CampaignIntegrationError("integration merge custody is ambiguous")
            boundaries.append((before, cursor, candidate_head))
            cursor = before
        if cursor != base_oid:
            raise CampaignIntegrationError("integration merge custody does not return to its base")
        boundaries.reverse()
        while len(merges) < merged_count:
            index = len(merges)
            before, after, _ = boundaries[index]
            merges.append(
                _record_merge(
                    inputs[index],
                    before=before,
                    after=after,
                    result=_CommandResult(0, b"", b"", "passed"),
                )
            )

    for index, merge in enumerate(merges):
        _verify_merge_tree(target_path, inputs[index], merge)
    for merge in merges[len(progress_merges) :]:
        store.append_progress(integration_id, {"kind": "merge", "merge": merge})
    blockers: list[str] = []
    if failed_merge_index is not None:
        blockers.append(f"merge_failed:{progress_merges[failed_merge_index]['work_unit_id']}")
    for item in inputs[merged_count:] if failed_merge_index is None else ():
        before = _git_output(target_path, "rev-parse", "--verify", "HEAD")
        result = _git(
            target_path,
            "merge",
            "--no-ff",
            "--no-edit",
            item["candidate"]["head_oid"],
            timeout=check_timeout_s,
        )
        after = None
        if result.status == "passed":
            try:
                current = capture_launch_repository(target_path)
                after = current.head_oid
            except ProviderReceiptError as exc:
                result = _CommandResult(
                    result.returncode,
                    result.stdout,
                    result.stderr + str(exc).encode("utf-8"),
                    "failed",
                )
        merges.append(_record_merge(item, before=before, after=after, result=result))
        store.append_progress(integration_id, {"kind": "merge", "merge": merges[-1]})
        if result.status != "passed":
            blockers.append(f"merge_failed:{item['work_unit_id']}")
            abort = _git(target_path, "merge", "--abort", timeout=check_timeout_s)
            if abort.status != "passed":
                blockers.append("merge_abort_failed")
            else:
                try:
                    restored = capture_launch_repository(target_path)
                    if restored.head_oid != before:
                        blockers.append("merge_abort_head_mismatch")
                except ProviderReceiptError:
                    blockers.append("merge_abort_left_dirty_worktree")
            break

    check_records: list[dict[str, Any]] = list(progress_checks)
    if len(check_records) > len(command_checks):
        raise CampaignIntegrationError("integration progress claims an unknown check")
    for ordinal, record in enumerate(check_records):
        expected_argv = command_checks[ordinal]
        if record["argv_sha256"] != _sha256(
            json.dumps(list(expected_argv), ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ):
            raise CampaignIntegrationError("integration check progress differs from its command")
    if any(record["status"] != "passed" for record in check_records):
        failed = next(record for record in check_records if record["status"] != "passed")
        blockers.append(f"integration_check_failed:{failed['ordinal']}")
    if not blockers:
        for ordinal, command in enumerate(command_checks[len(check_records) :], len(check_records)):
            argv_sha256 = _sha256(
                json.dumps(list(command), ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            store.append_progress(
                integration_id,
                {"kind": "check_started", "ordinal": ordinal, "argv_sha256": argv_sha256},
            )
            result = _run_command(
                subprocess.run,
                command,
                cwd=target_path,
                timeout=float(check_timeout_s),
            )
            check_records.append(_record_command(result, command, ordinal))
            store.append_progress(integration_id, {"kind": "check", "check": check_records[-1]})
            if result.status != "passed":
                blockers.append(f"integration_check_failed:{ordinal}")
                for skipped, later in enumerate(command_checks[ordinal + 1 :], ordinal + 1):
                    check_records.append(
                        _record_command(
                            _CommandResult(None, b"", b"", "not_run"),
                            later,
                            skipped,
                        )
                    )
                    store.append_progress(
                        integration_id, {"kind": "check", "check": check_records[-1]}
                    )
                break
    else:
        check_records.extend(
            _record_command(_CommandResult(None, b"", b"", "not_run"), command, ordinal)
            for ordinal, command in enumerate(
                command_checks[len(check_records) :], len(check_records)
            )
        )

    final_head: str | None = None
    final_tree: str | None = None
    final_candidate: CandidateResult | None = None
    try:
        final = capture_launch_repository(target_path)
        final_head = final.head_oid
        final_tree = final.tree_oid
        if _git_output(target_path, "symbolic-ref", "--quiet", "--short", "HEAD") != branch:
            blockers.append("integration_final_branch_changed")
        merged_head = merges[-1]["after_head_oid"] if merges else base_oid
        if not blockers and final_head != merged_head:
            blockers.append("integration_check_changed_candidate")
        if not blockers:
            _, final_candidate = capture_close_repository(
                target_path, launch, require_unchanged=False
            )
            if final_candidate is None:
                blockers.append("integration_produced_no_clean_candidate")
    except ProviderReceiptError as exc:
        blockers.append(f"integration_final_state_unavailable:{type(exc).__name__}")

    receipt = _content_receipt(
        {
            "schema_version": "1",
            "record_kind": "campaign_integration_receipt",
            "campaign_id": campaign_id,
            "run_id": run_id,
            "integration_id": integration_id,
            "integrator_id": integrator_id,
            "target": str(target_path),
            "branch": branch,
            "repository_common_dir_sha256": launch.repository_common_dir_sha256,
            "base_oid": base_oid,
            "initial_tree_oid": launch.tree_oid,
            "inputs": [dict(item) for item in inputs],
            "reviews": [dict(item) for item in review_refs],
            "merges": merges,
            "checks": check_records,
            "final_head_oid": final_head,
            "final_tree_oid": final_tree,
            "candidate": (
                final_candidate.to_dict() if final_candidate is not None and not blockers else None
            ),
            "status": "blocked" if blockers else "passed",
            "blockers": list(dict.fromkeys(blockers)),
        }
    )
    receipt = validate_integration_receipt(receipt)
    path = CampaignIntegrationStore(state / "integration-receipts").write(receipt)
    return CampaignIntegrationResult(receipt, path)


def _attempt_claim(
    attempt: Mapping[str, Any], campaign_root: Path | str, target: Path
) -> dict[str, Any] | None:
    """Reopen and compare the complete ordinary integration claim before recovery."""

    claim = attempt.get("claim")
    if claim is None:
        return None
    from bearhug.campaign.claims import active_claims, validate_claim_record

    try:
        validate_claim_record(dict(claim))
        live = {row["claim_id"]: row for row in active_claims(campaign_root)}
    except Exception as exc:
        raise CampaignIntegrationError(f"integration claim custody is unavailable: {exc}") from exc
    observed = live.get(claim["claim_id"])
    if observed is None:
        raise CampaignIntegrationError("integration claim is no longer active for recovery")
    if observed != claim:
        raise CampaignIntegrationError("integration claim identity differs from its attempt")
    _, common_digest = _common_dir(target)
    if (
        common_digest != claim["repository_common_dir_sha256"]
        or worktree_sha256(str(target)) != claim["worktree_sha256"]
    ):
        raise CampaignIntegrationError("integration target no longer matches its claim custody")
    branch = _git_output(target, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch != claim["branch"]:
        raise CampaignIntegrationError("integration target branch differs from its claim custody")
    return dict(claim)


def _release_attempt_claim(
    attempt: Mapping[str, Any], campaign_root: Path | str, *, reason: str
) -> None:
    claim = attempt.get("claim")
    if claim is None:
        return
    from bearhug.campaign.claims import active_claims, release_claim

    live = {row["claim_id"]: row for row in active_claims(campaign_root)}
    observed = live.get(claim["claim_id"])
    if observed is None:
        return
    if observed != claim:
        raise CampaignIntegrationError("integration claim identity differs before release")
    try:
        release_claim(
            campaign_root,
            claim["claim_id"],
            claimant_id=claim["claimant_id"],
            reason=reason,
        )
    except Exception as exc:
        raise CampaignIntegrationError(f"integration claim release failed: {exc}") from exc


def _blocked_attempt_receipt(
    attempt: Mapping[str, Any],
    store: CampaignIntegrationStore,
    *,
    disposition: str,
    reason: str,
) -> CampaignIntegrationResult:
    """Close an ambiguous local attempt with an explicit bounded disposition."""

    target = Path(attempt["target"])
    merges: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for row in store.read_progress(attempt["integration_id"]):
        event = row["event"]
        if event.get("kind") == "merge":
            merges.append(_validate_merge(event["merge"], len(merges)))
        elif event.get("kind") == "check":
            checks.append(_validate_check(event["check"], len(checks)))
        elif event.get("kind") == "check_started":
            # A start fence with no result is precisely why recovery is closing this
            # attempt with an explicit failed disposition; it is not a check verdict.
            continue
    declared_checks = [
        _command(command, label=f"attempt.checks[{index}]")
        for index, command in enumerate(attempt["checks"])
    ]
    while len(checks) < len(declared_checks):
        ordinal = len(checks)
        checks.append(
            _record_command(
                _CommandResult(None, b"", b"", "not_run"), declared_checks[ordinal], ordinal
            )
        )
    try:
        actual = capture_launch_repository(target)
        final_head, final_tree = actual.head_oid, actual.tree_oid
    except ProviderReceiptError:
        final_head = final_tree = None
    blockers = [f"integration_recovery_disposition:{disposition}", reason]
    receipt = _content_receipt(
        {
            "schema_version": "1",
            "record_kind": "campaign_integration_receipt",
            "campaign_id": attempt["campaign_id"],
            "run_id": attempt["run_id"],
            "integration_id": attempt["integration_id"],
            "integrator_id": attempt["integrator_id"],
            "target": attempt["target"],
            "branch": attempt["branch"],
            "repository_common_dir_sha256": attempt["repository_common_dir_sha256"],
            "base_oid": attempt["base_oid"],
            "initial_tree_oid": attempt["initial_tree_oid"],
            "inputs": [dict(item) for item in attempt["inputs"]],
            "reviews": [dict(item) for item in attempt["reviews"]],
            "merges": merges,
            "checks": checks,
            "final_head_oid": final_head,
            "final_tree_oid": final_tree,
            "candidate": None,
            "status": "blocked",
            "blockers": list(dict.fromkeys(blockers)),
        }
    )
    return CampaignIntegrationResult(receipt=receipt, receipt_path=store.write(receipt))


def recover_integration_attempt(
    *,
    state_root: Path | str,
    campaign_root: Path | str,
    integration_id: str,
    disposition: str = "blocked",
    check_timeout_s: float = 28800.0,
    resume: bool = True,
) -> CampaignIntegrationResult | None:
    """Recover an integration while holding the same process-wide operation fence."""

    state = _private_directory(Path(state_root).expanduser(), create=False)
    attempt = CampaignIntegrationStore(state / "integration-receipts").read_attempt(
        integration_id
    )
    with _integration_operation_lock(state, integration_id, attempt["target"]):
        return _recover_integration_attempt_unlocked(
            state_root=state,
            campaign_root=campaign_root,
            integration_id=integration_id,
            disposition=disposition,
            check_timeout_s=check_timeout_s,
            resume=resume,
        )


def _recover_integration_attempt_unlocked(
    *,
    state_root: Path | str,
    campaign_root: Path | str,
    integration_id: str,
    disposition: str = "blocked",
    check_timeout_s: float = 28800.0,
    resume: bool = True,
) -> CampaignIntegrationResult | None:
    """Recover one interrupted integration locator without repeating any provider operation.

    A complete durable receipt is adopted after exact claim validation.  A started attempt with a
    reproducible Git boundary resumes the existing local merge/check operation.  An ambiguous
    attempt is retained for the default blocked disposition; ``failed`` is the explicit bounded
    disposition that closes its claim and publishes a blocked receipt.
    """

    if disposition not in {"failed", "blocked", "hil_required"}:
        raise CampaignIntegrationError("integration recovery disposition is unsupported")
    requested_state = Path(state_root).expanduser()
    state = _private_directory(requested_state, create=False)
    store = CampaignIntegrationStore(state / "integration-receipts")
    attempt = store.read_attempt(integration_id)
    target = _absolute_directory(attempt["target"], "integration recovery target")
    receipt_path = store._path(integration_id)
    if receipt_path.exists():
        receipt = store.read(integration_id)
        if (
            receipt["campaign_id"] != attempt["campaign_id"]
            or receipt["run_id"] != attempt["run_id"]
            or receipt["integrator_id"] != attempt["integrator_id"]
            or receipt["target"] != attempt["target"]
            or receipt["base_oid"] != attempt["base_oid"]
            or receipt["inputs"] != attempt["inputs"]
            or receipt["reviews"] != attempt["reviews"]
            or receipt["branch"] != attempt["branch"]
            or receipt["repository_common_dir_sha256"] != attempt["repository_common_dir_sha256"]
            or receipt["initial_tree_oid"] != attempt["initial_tree_oid"]
            or [row["argv_sha256"] for row in receipt["checks"]]
            != [
                _sha256(json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode())
                for command in attempt["checks"]
            ]
        ):
            raise CampaignIntegrationError("integration receipt differs from its attempt")
        _, common_digest = _common_dir(target)
        if (
            common_digest != receipt["repository_common_dir_sha256"]
            or _git_output(target, "symbolic-ref", "--quiet", "--short", "HEAD")
            != receipt["branch"]
        ):
            raise CampaignIntegrationError("integration receipt target custody changed")
        if receipt["status"] == "passed":
            verify_integration_git(receipt)
            actual = capture_launch_repository(target)
            if (
                actual.head_oid != receipt["final_head_oid"]
                or actual.tree_oid != receipt["final_tree_oid"]
            ):
                raise CampaignIntegrationError(
                    "integration receipt candidate changed before adoption"
                )
        # A normal completion may already have published the release tombstone.  If an active
        # record remains, it must still match the complete old claim before we adopt the receipt.
        claim = attempt.get("claim")
        if claim is not None:
            from bearhug.campaign.claims import active_claims

            live = {row["claim_id"]: row for row in active_claims(campaign_root)}
            if claim["claim_id"] in live and live[claim["claim_id"]] != claim:
                raise CampaignIntegrationError("integration claim identity differs before adoption")
        _release_attempt_claim(
            attempt,
            campaign_root,
            reason="completed" if receipt["status"] == "passed" else "failed",
        )
        return CampaignIntegrationResult(receipt, receipt_path)
    claim = _attempt_claim(attempt, campaign_root, target)
    if not resume:
        if disposition != "failed":
            raise CampaignIntegrationError("non-executing recovery requires failed disposition")
        result = _blocked_attempt_receipt(
            attempt,
            store,
            disposition=disposition,
            reason="integration_execution_budget_unavailable",
        )
        _release_attempt_claim(attempt, campaign_root, reason="failed")
        return result
    try:
        result = _integrate_campaign_candidates_unlocked(
            state_root=state,
            campaign_id=attempt["campaign_id"],
            run_id=attempt["run_id"],
            integration_id=attempt["integration_id"],
            integrator_id=attempt["integrator_id"],
            target=target,
            base_oid=attempt["base_oid"],
            candidates=attempt["inputs"],
            checks=attempt["checks"],
            _review_refs=attempt["reviews"],
            _claim=claim,
            check_timeout_s=check_timeout_s,
        )
    except CampaignIntegrationError as exc:
        if disposition != "failed":
            raise
        result = _blocked_attempt_receipt(
            attempt,
            store,
            disposition=disposition,
            reason=type(exc).__name__,
        )
    if result.receipt["status"] == "passed" or disposition == "failed":
        _release_attempt_claim(
            attempt,
            campaign_root,
            reason="completed" if result.receipt["status"] == "passed" else "failed",
        )
    return result


def integrate_reviewed_campaign_candidates(
    *,
    review_store: CampaignReviewStore | Path | str,
    review_ids: Mapping[str, Sequence[str]],
    minimum_review_approvals: int,
    **integration_kwargs: Any,
) -> CampaignIntegrationResult:
    """Require durable eligible reviews before delegating to candidate integration.

    Review receipts are re-opened from the create-only store, matched to the exact candidate and
    author receipt digest supplied for each work unit, and checked for independent reviewer
    sessions/worktrees.  The resulting review references are carried into the integration receipt
    so a later reader can follow the evidence chain without trusting an operator-side annotation.
    """

    if isinstance(review_store, CampaignReviewStore):
        store = review_store
    else:
        try:
            store = CampaignReviewStore(review_store)
        except CampaignReviewError as exc:
            raise CampaignIntegrationError(f"review store is unavailable: {exc}") from exc
    if type(minimum_review_approvals) is not int or not 1 <= minimum_review_approvals <= 32:
        raise CampaignIntegrationError("minimum_review_approvals is outside its bounded range")
    candidates = integration_kwargs.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise CampaignIntegrationError("reviewed integration requires candidate inputs")
    campaign_id = integration_kwargs.get("campaign_id")
    if not isinstance(campaign_id, str):
        raise CampaignIntegrationError("reviewed integration requires campaign_id")
    if not isinstance(review_ids, Mapping):
        raise CampaignIntegrationError("review_ids must map work units to id arrays")
    by_unit: dict[str, Sequence[str]] = {}
    for unit_id, identifiers in review_ids.items():
        if (
            not isinstance(unit_id, str)
            or not isinstance(identifiers, Sequence)
            or isinstance(identifiers, (str, bytes))
        ):
            raise CampaignIntegrationError("review_ids must map work units to id arrays")
        by_unit[unit_id] = identifiers

    refs: list[dict[str, Any]] = []
    seen_reviews: set[str] = set()
    for index, item in enumerate(candidates):
        checked = _input(item, index)
        identifiers = by_unit.get(checked["work_unit_id"])
        if identifiers is None or len(identifiers) < minimum_review_approvals:
            raise CampaignIntegrationError(
                f"integration input {checked['work_unit_id']!r} lacks review quorum"
            )
        local_sessions: set[str] = set()
        local_worktrees: set[str] = set()
        for review_id in identifiers:
            if not isinstance(review_id, str) or review_id in seen_reviews:
                raise CampaignIntegrationError("review ids must be unique across the integration")
            seen_reviews.add(review_id)
            try:
                review = store.read(review_id)
            except CampaignReviewError as exc:
                raise CampaignIntegrationError(f"cannot read review {review_id!r}: {exc}") from exc
            if (
                review["campaign_id"] != campaign_id
                or review["eligible"] is not True
                or review["candidate"] != checked["candidate"]
                or review["author_receipt_sha256"] != checked["provider_receipt_sha256"]
            ):
                raise CampaignIntegrationError(
                    f"review {review_id!r} does not approve the exact integration candidate"
                )
            if (
                review["reviewer_session_id"] in local_sessions
                or review["reviewer_worktree_sha256"] in local_worktrees
            ):
                raise CampaignIntegrationError(
                    f"reviews for {checked['work_unit_id']!r} are not independent"
                )
            local_sessions.add(review["reviewer_session_id"])
            local_worktrees.add(review["reviewer_worktree_sha256"])
            refs.append(
                {
                    "work_unit_id": checked["work_unit_id"],
                    "review_id": review_id,
                    "receipt_sha256": canonical_json_sha256(review),
                }
            )
    expected_units = {item["work_unit_id"] for item in candidates}
    if set(by_unit) != expected_units:
        raise CampaignIntegrationError("review_ids must name exactly the integration work units")
    return integrate_campaign_candidates(
        **integration_kwargs,
        _review_refs=refs,
    )


__all__ = [
    "CampaignIntegrationError",
    "CampaignIntegrationResult",
    "CampaignIntegrationStore",
    "integrate_campaign_candidates",
    "integrate_reviewed_campaign_candidates",
    "recover_integration_attempt",
    "validate_integration_receipt",
]
