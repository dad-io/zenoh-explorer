"""Observe pinned source freshness from immutable Git blobs, without granting authority."""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.providers.receipt import ProviderReceiptError, capture_launch_repository

_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MAX_SOURCE_BYTES = 4 * 1024 * 1024


class CapsuleGroundingError(ValueError):
    """A clean, exact source observation cannot be established."""


def _git(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", "--literal-pathspecs", "-C", str(root), *args),
            capture_output=True, check=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapsuleGroundingError("cannot inspect pinned Git source evidence") from exc
    return completed.stdout


def _snapshot(root: Path, revision: str, path: str) -> dict[str, Any]:
    missing = {"status": "missing", "git_blob_oid": None, "sha256": None, "bytes": None}
    entry = _git(root, "ls-tree", "-z", revision, "--", path)
    if not entry:
        return missing
    try:
        metadata, observed_path = entry[:-1].split(b"\t", 1)
        mode, object_kind, oid = metadata.decode("ascii").split(" ")
    except (ValueError, UnicodeError) as exc:
        raise CapsuleGroundingError("Git source entry is malformed") from exc
    if not entry.endswith(b"\0") or observed_path != path.encode("utf-8"):
        raise CapsuleGroundingError("Git source lookup was not exact")
    if mode not in {"100644", "100755"} or object_kind != "blob":
        return dict(missing, status="not_regular")
    size = int(_git(root, "cat-file", "-s", oid).strip())
    if size > _MAX_SOURCE_BYTES:
        raise CapsuleGroundingError("source evidence exceeds the 4 MiB bound")
    raw = _git(root, "cat-file", "blob", oid)
    if len(raw) != size:
        raise CapsuleGroundingError("Git source size changed during observation")
    return {
        "status": "present", "git_blob_oid": oid,
        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": size,
    }


def capture_capsule_grounding(
    worktree: Path | str,
    *,
    base_oid: str,
    sources: Sequence[Mapping[str, Any]],
    expected_head_oid: str | None = None,
) -> dict[str, Any]:
    """Return mechanical before/current hashes for explicitly selected repository sources.

    Each source contains exactly source_id, path and expected_sha256. The expected digest remains
    unchanged even when reality differs. No source is substituted, accepted, or edited. Sources
    outside this explicit inventory have not been observed; an empty inventory is unavailable.
    """

    root = Path(worktree).resolve()
    if not isinstance(base_oid, str) or _OID.fullmatch(base_oid) is None:
        raise CapsuleGroundingError("base_oid must be an exact Git object id")
    if expected_head_oid is not None and (
        not isinstance(expected_head_oid, str) or _OID.fullmatch(expected_head_oid) is None
    ):
        raise CapsuleGroundingError("expected_head_oid must be an exact Git object id")
    if not isinstance(sources, (list, tuple)) or len(sources) > 256:
        raise CapsuleGroundingError("sources must be an explicit bounded inventory")
    selected = []
    seen = set()
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != {
            "source_id", "path", "expected_sha256"
        }:
            raise CapsuleGroundingError("source requires source_id, path and expected_sha256")
        source_id, path, expected = (
            source[key] for key in ("source_id", "path", "expected_sha256")
        )
        if (
            not isinstance(source_id, str) or _TOKEN.fullmatch(source_id) is None
            or source_id in seen
        ):
            raise CapsuleGroundingError("source identity must be unique and canonical")
        if not isinstance(expected, str) or _SHA.fullmatch(expected) is None:
            raise CapsuleGroundingError("source expected_sha256 must be SHA-256")
        if (
            not isinstance(path, str) or not path or "\\" in path
            or any(
                ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                for char in path
            )
            or PurePosixPath(path).is_absolute() or PurePosixPath(path).as_posix() != path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise CapsuleGroundingError("source path must be a canonical repository-relative file")
        seen.add(source_id)
        selected.append(dict(source))
    try:
        launch = capture_launch_repository(root)
    except ProviderReceiptError as exc:
        raise CapsuleGroundingError(f"source observation needs a clean checkout: {exc}") from exc
    if expected_head_oid is not None and launch.head_oid != expected_head_oid:
        raise CapsuleGroundingError("source observation head differs from the expected head")
    _git(root, "merge-base", "--is-ancestor", base_oid, launch.head_oid)
    observed = []
    for source in sorted(selected, key=lambda item: item["source_id"]):
        before = _snapshot(root, base_oid, source["path"])
        current = _snapshot(root, launch.head_oid, source["path"])
        freshness = current["status"]
        if freshness == "present":
            freshness = "fresh" if current["sha256"] == source["expected_sha256"] else "changed"
        observed.append(dict(source, before=before, current=current, freshness=freshness))
    try:
        close = capture_launch_repository(root)
    except ProviderReceiptError as exc:
        raise CapsuleGroundingError(f"checkout changed during source observation: {exc}") from exc
    if close != launch:
        raise CapsuleGroundingError("checkout changed during source observation")
    return {
        "basis": "mechanical_git_blobs", "truth_effect": "observation_only",
        "repository_common_dir_sha256": launch.repository_common_dir_sha256,
        "base_oid": base_oid, "head_oid": launch.head_oid, "tree_oid": launch.tree_oid,
        "status": "observed" if observed else "unavailable", "sources": observed,
    }


__all__ = ["CapsuleGroundingError", "capture_capsule_grounding"]
