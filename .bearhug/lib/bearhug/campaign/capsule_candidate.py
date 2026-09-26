"""Recompute one cumulative capsule candidate without rewriting provider receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from bearhug.campaign.author import _owns
from bearhug.campaign.capsules import CapsuleContractError
from bearhug.providers.receipt import (
    LaunchRepository,
    ProviderReceiptError,
    capture_close_repository,
    capture_launch_repository,
    validate_provider_receipt,
)

_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_FIELDS = frozenset(
    {"repository_common_dir_sha256", "base_oid", "head_oid", "tree_oid", "patch_sha256", "clean"}
)
_DEPENDENCY_FIELDS = frozenset(
    {"base_oid", "tree_oid", "repository_common_dir_sha256", "provenance"}
)
_PROVENANCE_FIELDS = frozenset({"capsule_id", "candidate", "candidate_sha256"})


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CapsuleContractError(f"dependency candidate is not canonical JSON: {exc}") from exc


def candidate_sha256(candidate: Mapping[str, Any]) -> str:
    """Return the content identity used for dependency candidate provenance."""

    return hashlib.sha256(_canonical_json(dict(candidate))).hexdigest()


def _check_oid(value: Any, label: str) -> None:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise CapsuleContractError(f"{label} must be a full Git object id")


def _check_sha(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CapsuleContractError(f"{label} must be lowercase SHA-256")


def _validate_dependency_candidate(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CANDIDATE_FIELDS:
        raise CapsuleContractError(f"{label} is not a closed candidate")
    candidate = dict(value)
    _check_sha(candidate["repository_common_dir_sha256"], f"{label}.repository_common_dir_sha256")
    for field in ("base_oid", "head_oid", "tree_oid"):
        _check_oid(candidate[field], f"{label}.{field}")
    _check_sha(candidate["patch_sha256"], f"{label}.patch_sha256")
    if candidate["clean"] is not True:
        raise CapsuleContractError(f"{label}.clean must be true")
    return candidate


def validate_dependency_base(
    value: Mapping[str, Any],
    *,
    repository_common_dir_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate a selected accepted dependency tree and its exact candidate provenance.

    Git ancestry and tree existence are checked by :func:`verify_dependency_base`.  A selected
    base may be one accepted head or a deterministic merge commit composed from several accepted
    heads; the latter is intentionally represented by all of its accepted inputs in provenance.
    """

    if not isinstance(value, Mapping) or set(value) != _DEPENDENCY_FIELDS:
        raise CapsuleContractError("dependency base has missing or unknown fields")
    result = dict(value)
    _check_oid(result["base_oid"], "dependency base/base_oid")
    _check_oid(result["tree_oid"], "dependency base/tree_oid")
    _check_sha(
        result["repository_common_dir_sha256"], "dependency base/repository_common_dir_sha256"
    )
    if (
        repository_common_dir_sha256 is not None
        and result["repository_common_dir_sha256"] != repository_common_dir_sha256
    ):
        raise CapsuleContractError("dependency base repository differs from the launch checkout")
    provenance = result["provenance"]
    if not isinstance(provenance, list) or not provenance:
        raise CapsuleContractError("dependency base provenance must be a non-empty array")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(provenance):
        label = f"dependency base/provenance[{index}]"
        if not isinstance(item, Mapping) or set(item) != _PROVENANCE_FIELDS:
            raise CapsuleContractError(f"{label} is not closed")
        capsule_id = item["capsule_id"]
        if not isinstance(capsule_id, str) or _TOKEN.fullmatch(capsule_id) is None:
            raise CapsuleContractError(f"{label}.capsule_id is invalid")
        if capsule_id in seen:
            raise CapsuleContractError("dependency base provenance repeats a capsule")
        seen.add(capsule_id)
        candidate = _validate_dependency_candidate(item["candidate"], f"{label}.candidate")
        _check_sha(item["candidate_sha256"], f"{label}.candidate_sha256")
        if item["candidate_sha256"] != candidate_sha256(candidate):
            raise CapsuleContractError(f"{label}.candidate_sha256 does not match candidate bytes")
        if candidate["repository_common_dir_sha256"] != result["repository_common_dir_sha256"]:
            raise CapsuleContractError(f"{label}.candidate is from another repository")
        if (
            candidate["head_oid"] == result["base_oid"]
            and candidate["tree_oid"] != result["tree_oid"]
        ):
            raise CapsuleContractError("dependency base tree differs from selected candidate")
        rows.append(
            {
                "capsule_id": capsule_id,
                "candidate": candidate,
                "candidate_sha256": item["candidate_sha256"],
            }
        )
    # Canonical ordering makes the provenance digest stable regardless of scheduler iteration.
    result["provenance"] = sorted(rows, key=lambda row: row["capsule_id"])
    return result


def _git_result(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ("git", "-C", str(root), *args), capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapsuleContractError(f"cannot compose dependency base: {exc}") from exc


def _commit_tree(root: Path, tree_oid: str, parents: Sequence[str], message: str) -> str:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_AUTHOR_NAME": "Bear Hug dependency composer",
        "GIT_AUTHOR_EMAIL": "bearhug-dependency@localhost",
        "GIT_COMMITTER_NAME": "Bear Hug dependency composer",
        "GIT_COMMITTER_EMAIL": "bearhug-dependency@localhost",
        "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
    }
    parent_args = tuple(argument for parent in parents for argument in ("-p", parent))
    result = subprocess.run(
        ("git", "-C", str(root), "commit-tree", tree_oid, *parent_args),
        input=(message + "\n").encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=30,
        env=environment,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CapsuleContractError(f"cannot publish composed dependency base: {detail}")
    value = result.stdout.decode("ascii", errors="strict").strip()
    _check_oid(value, "composed dependency base/base_oid")
    return value


def compose_dependency_base(
    worktree: Path | str,
    *,
    original_base_oid: str,
    repository_common_dir_sha256: str,
    predecessors: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Select or deterministically compose accepted predecessor candidate heads.

    A single descendant candidate is selected directly.  Independent candidates are merged in
    capsule-id order with Git's three-way merge machinery and a content-addressed synthetic merge
    commit.  Conflicts fail closed before a dependent worktree can be launched.
    """

    root = Path(worktree).resolve()
    _check_oid(original_base_oid, "original base_oid")
    _check_sha(repository_common_dir_sha256, "repository_common_dir_sha256")
    rows: list[dict[str, Any]] = []
    for item in predecessors:
        if not isinstance(item, Mapping) or set(item) != _PROVENANCE_FIELDS:
            raise CapsuleContractError("predecessor provenance row is not closed")
        candidate = _validate_dependency_candidate(item["candidate"], "predecessor candidate")
        _check_sha(item["candidate_sha256"], "predecessor candidate_sha256")
        if item["candidate_sha256"] != candidate_sha256(candidate):
            raise CapsuleContractError("predecessor candidate provenance digest differs")
        if candidate["repository_common_dir_sha256"] != repository_common_dir_sha256:
            raise CapsuleContractError("predecessor candidate belongs to another repository")
        rows.append(
            {
                "capsule_id": item["capsule_id"],
                "candidate": candidate,
                "candidate_sha256": item["candidate_sha256"],
            }
        )
    if not rows:
        raise CapsuleContractError("a dependent capsule requires an accepted predecessor")
    rows.sort(key=lambda row: row["capsule_id"])
    try:
        _git(
            root, "merge-base", "--is-ancestor", original_base_oid, rows[0]["candidate"]["head_oid"]
        )
    except CapsuleContractError as exc:
        raise CapsuleContractError(
            "predecessor candidate is not descended from the sealed subject"
        ) from exc
    for row in rows[1:]:
        try:
            _git(
                root, "merge-base", "--is-ancestor", original_base_oid, row["candidate"]["head_oid"]
            )
        except CapsuleContractError as exc:
            raise CapsuleContractError(
                f"predecessor candidate {row['capsule_id']!r} "
                "is not descended from the sealed subject"
            ) from exc

    # Prefer an accepted head that already contains all other accepted heads.  This is the normal
    # transitive-DAG case and does not mint a new Git object.
    for selected in reversed(rows):
        if all(
            _git_result(
                root,
                "merge-base",
                "--is-ancestor",
                row["candidate"]["head_oid"],
                selected["candidate"]["head_oid"],
            ).returncode
            == 0
            for row in rows
        ):
            return {
                "base_oid": selected["candidate"]["head_oid"],
                "tree_oid": selected["candidate"]["tree_oid"],
                "repository_common_dir_sha256": repository_common_dir_sha256,
                "provenance": rows,
            }

    current = original_base_oid
    current_tree = _git(root, "rev-parse", "--verify", f"{current}^{{tree}}").decode().strip()
    for row in rows:
        candidate_head = row["candidate"]["head_oid"]
        if (
            _git_result(root, "merge-base", "--is-ancestor", candidate_head, current).returncode
            == 0
        ):
            continue
        merged = _git_result(root, "merge-tree", "--write-tree", current, candidate_head)
        if merged.returncode != 0:
            detail = merged.stdout.decode("utf-8", errors="replace").strip()
            raise CapsuleContractError(
                "accepted predecessor candidates conflict while composing dependency base: "
                f"{detail}"
            )
        merged_tree = merged.stdout.decode("ascii", errors="strict").splitlines()[0].strip()
        _check_oid(merged_tree, "composed dependency base/tree_oid")
        current = _commit_tree(
            root,
            merged_tree,
            (current, candidate_head),
            "bearhug dependency composition " + ",".join(row["capsule_id"] for row in rows),
        )
        current_tree = merged_tree
    return {
        "base_oid": current,
        "tree_oid": current_tree,
        "repository_common_dir_sha256": repository_common_dir_sha256,
        "provenance": rows,
    }


def verify_dependency_base(
    worktree: Path | str,
    dependency_base: Mapping[str, Any],
    *,
    original_base_oid: str | None = None,
) -> dict[str, Any]:
    """Reopen Git objects named by a dependency base and verify their ancestry and tree IDs."""

    root = Path(worktree).resolve()
    try:
        launch = capture_launch_repository(root)
    except ProviderReceiptError as exc:
        raise CapsuleContractError(f"dependency base checkout is unavailable: {exc}") from exc
    value = validate_dependency_base(
        dependency_base,
        repository_common_dir_sha256=launch.repository_common_dir_sha256,
    )
    resolved_tree = (
        _git(root, "rev-parse", "--verify", f"{value['base_oid']}^{{tree}}").decode().strip()
    )
    if resolved_tree != value["tree_oid"]:
        raise CapsuleContractError("dependency base tree does not resolve to the selected head")
    if original_base_oid is not None:
        _check_oid(original_base_oid, "original base_oid")
        try:
            _git(root, "merge-base", "--is-ancestor", original_base_oid, value["base_oid"])
        except CapsuleContractError as exc:
            raise CapsuleContractError(
                "dependency base is not descended from the sealed subject"
            ) from exc
    for row in value["provenance"]:
        candidate = row["candidate"]
        resolved = (
            _git(root, "rev-parse", "--verify", f"{candidate['head_oid']}^{{tree}}")
            .decode()
            .strip()
        )
        if resolved != candidate["tree_oid"]:
            raise CapsuleContractError(
                f"dependency candidate {row['capsule_id']!r} tree differs from Git"
            )
        try:
            _git(root, "merge-base", "--is-ancestor", candidate["head_oid"], value["base_oid"])
        except CapsuleContractError as exc:
            raise CapsuleContractError(
                f"dependency candidate {row['capsule_id']!r} is not in the selected base"
            ) from exc
    if original_base_oid is not None:
        expected = compose_dependency_base(
            root,
            original_base_oid=original_base_oid,
            repository_common_dir_sha256=value["repository_common_dir_sha256"],
            predecessors=value["provenance"],
        )
        if (
            expected["base_oid"] != value["base_oid"]
            or expected["tree_oid"] != value["tree_oid"]
            or expected["provenance"] != value["provenance"]
        ):
            raise CapsuleContractError(
                "dependency base does not match deterministic composition of accepted predecessors"
            )
    return value


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ("git", "-C", str(root), *args),
            input=input_bytes,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapsuleContractError(f"cannot inspect capsule candidate history: {exc}") from exc
    return result.stdout


def _history_paths(root: Path, base: str, head: str) -> tuple[str, ...]:
    commits = _git(root, "rev-list", "--reverse", f"{base}..{head}")
    if not commits:
        return ()
    raw = _git(
        root,
        "diff-tree",
        "--stdin",
        "--no-commit-id",
        "--name-only",
        "--no-renames",
        "-r",
        "-m",
        "-z",
        input_bytes=commits,
    )
    if not raw:
        return ()
    if not raw.endswith(b"\0"):
        raise CapsuleContractError("capsule changed paths are not NUL terminated")
    try:
        paths = set(item.decode("utf-8") for item in raw[:-1].split(b"\0"))
    except UnicodeError as exc:
        raise CapsuleContractError("capsule changed paths must be UTF-8") from exc
    if any(
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        for path in paths
    ):
        raise CapsuleContractError("capsule history contains a noncanonical changed path")
    return tuple(sorted(paths))


def capture_capsule_candidate(
    worktree: Path | str,
    launch: LaunchRepository,
    episode_provider_receipts: Sequence[Mapping[str, Any]],
    path_prefixes: Iterable[str],
    *,
    dependency_base: Mapping[str, Any] | None = None,
    original_base_oid: str | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Check the portable fresh-session chain and recompute original-base through final-head.

    The controller must separately validate each receipt's raw provider custody, selected role,
    policy, and active lease. This function proves Git continuity and committed mutation scope;
    it does not claim to observe uncommitted edits that were reverted within a provider turn.
    """
    root = Path(worktree).resolve()
    if not episode_provider_receipts:
        raise CapsuleContractError("capsule candidate requires episode provider evidence")
    if dependency_base is not None:
        checked_dependency = validate_dependency_base(
            dependency_base,
            repository_common_dir_sha256=launch.repository_common_dir_sha256,
        )
        if (
            launch.head_oid != checked_dependency["base_oid"]
            or launch.tree_oid != checked_dependency["tree_oid"]
        ):
            raise CapsuleContractError("capsule launch does not use the selected dependency base")
        if original_base_oid is not None:
            verify_dependency_base(root, checked_dependency, original_base_oid=original_base_oid)
    expected_head = launch.head_oid
    sessions: set[tuple[str, str]] = set()
    try:
        for value in episode_provider_receipts:
            receipt = validate_provider_receipt(value)
            candidate = receipt["candidate"]
            if (
                candidate is None
                or candidate["base_oid"] != expected_head
                or candidate["repository_common_dir_sha256"] != launch.repository_common_dir_sha256
                or receipt["cwd"] != str(root)
            ):
                raise CapsuleContractError("episode candidate breaks the capsule Git chain")
            session = (receipt["provider"], receipt["session_id"])
            if session in sessions:
                raise CapsuleContractError("fresh episodes must have distinct provider sessions")
            sessions.add(session)
            expected_head = candidate["head_oid"]
        close, candidate = capture_close_repository(root, launch, require_unchanged=False)
        if candidate is None or not close.clean or candidate.head_oid != expected_head:
            raise CapsuleContractError("capsule close is dirty or differs from episode evidence")
        paths = _history_paths(root, launch.head_oid, candidate.head_oid)
        prefixes = tuple(path_prefixes)
        outside = [path for path in paths if not _owns(path, prefixes)]
        if outside:
            raise CapsuleContractError(f"capsule committed paths outside its envelope: {outside}")
        current = capture_launch_repository(root)
        if current.head_oid != candidate.head_oid or current.tree_oid != candidate.tree_oid:
            raise CapsuleContractError("capsule checkout changed during candidate verification")
        return candidate.to_dict(), paths
    except ProviderReceiptError as exc:
        raise CapsuleContractError(f"invalid capsule provider/Git candidate: {exc}") from exc
