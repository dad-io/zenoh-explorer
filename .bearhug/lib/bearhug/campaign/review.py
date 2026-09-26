"""Independent, provider-neutral campaign review receipts.

This module does not launch a reviewer.  It binds a structured verdict to an author candidate and
to the canonical reviewer provider-session receipt.  A completed provider turn is not approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.paths import assert_writable
from bearhug.providers.receipt import validate_provider_receipt


class CampaignReviewError(ValueError):
    """Review evidence is malformed, non-independent, or names a different candidate."""


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOP = frozenset(
    {
        "schema_version", "record_kind", "campaign_id", "review_id", "completed_at",
        "author_receipt_sha256", "reviewer_receipt_sha256", "author_session_id",
        "reviewer_session_id", "author_worktree_sha256", "reviewer_worktree_sha256",
        "candidate", "verdict", "findings", "eligible", "blockers",
    }
)
_FINDING = frozenset({"finding_id", "severity", "summary"})
_CANDIDATE = frozenset(
    {
        "repository_common_dir_sha256",
        "base_oid",
        "head_oid",
        "tree_oid",
        "patch_sha256",
        "clean",
    }
)
_MAX_REVIEW_RECEIPT_BYTES = 64 * 1024 * 1024


def canonical_json_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def worktree_sha256(path: Path | str) -> str:
    return hashlib.sha256(os.fsencode(Path(path).expanduser().resolve())).hexdigest()


def _sha(value: Any, where: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise CampaignReviewError(f"{where} must be lowercase SHA-256")


def _id(value: Any, where: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise CampaignReviewError(f"{where} is not a valid identifier")


def validate_review_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _TOP:
        raise CampaignReviewError("review receipt has missing or unknown fields")
    if value["schema_version"] != "1" or value["record_kind"] != "campaign_review_receipt":
        raise CampaignReviewError("unsupported review receipt schema or kind")
    _id(value["campaign_id"], "campaign_id")
    _id(value["review_id"], "review_id")
    try:
        completed = datetime.fromisoformat(value["completed_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CampaignReviewError("completed_at must be ISO-8601") from exc
    if completed.tzinfo is None:
        raise CampaignReviewError("completed_at must include a timezone")
    for field in (
        "author_receipt_sha256", "reviewer_receipt_sha256", "author_worktree_sha256",
        "reviewer_worktree_sha256",
    ):
        _sha(value[field], field)
    for field in ("author_session_id", "reviewer_session_id"):
        if not isinstance(value[field], str) or not value[field]:
            raise CampaignReviewError(f"{field} must be a non-empty string")
    if value["author_session_id"] == value["reviewer_session_id"]:
        raise CampaignReviewError("author and reviewer session identities must differ")
    if value["author_worktree_sha256"] == value["reviewer_worktree_sha256"]:
        raise CampaignReviewError("author and reviewer worktrees must differ")
    candidate = value["candidate"]
    if (
        not isinstance(candidate, dict)
        or set(candidate) != _CANDIDATE
        or candidate.get("clean") is not True
    ):
        raise CampaignReviewError("review must bind a clean candidate")
    for field in ("repository_common_dir_sha256", "patch_sha256"):
        _sha(candidate.get(field), f"candidate.{field}")
    for field in ("base_oid", "head_oid", "tree_oid"):
        oid = candidate.get(field)
        if (
            not isinstance(oid, str)
            or len(oid) not in {40, 64}
            or any(char not in "0123456789abcdef" for char in oid)
        ):
            raise CampaignReviewError(f"candidate.{field} must be a full Git object id")
    if value["verdict"] not in {"approve", "reject", "incomplete"}:
        raise CampaignReviewError("review verdict is unsupported")
    findings = value["findings"]
    if not isinstance(findings, list):
        raise CampaignReviewError("findings must be an array")
    finding_ids: set[str] = set()
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict) or set(finding) != _FINDING:
            raise CampaignReviewError(f"findings[{index}] has missing or unknown fields")
        _id(finding["finding_id"], f"findings[{index}].finding_id")
        if finding["finding_id"] in finding_ids:
            raise CampaignReviewError(f"duplicate review finding {finding['finding_id']!r}")
        finding_ids.add(finding["finding_id"])
        if finding["severity"] not in {"blocker", "major", "minor", "info"}:
            raise CampaignReviewError(f"findings[{index}].severity is unsupported")
        if (
            not isinstance(finding["summary"], str)
            or not finding["summary"]
            or len(finding["summary"]) > 1000
        ):
            raise CampaignReviewError(f"findings[{index}].summary must not be empty")
    if type(value["eligible"]) is not bool:
        raise CampaignReviewError("eligible must be boolean")
    blockers = value["blockers"]
    if not isinstance(blockers, list) or not all(
        isinstance(item, str) and item for item in blockers
    ) or len(blockers) != len(set(blockers)):
        raise CampaignReviewError("blockers must be a unique string array")
    has_blocking_finding = any(finding["severity"] == "blocker" for finding in findings)
    if value["eligible"] and (
        value["verdict"] != "approve" or blockers or has_blocking_finding
    ):
        raise CampaignReviewError("eligible review must approve and have no blockers")
    if not value["eligible"] and not blockers:
        raise CampaignReviewError("ineligible review must name at least one blocker")
    return value


def _review_store_root(path: Path | str) -> Path:
    requested = Path(path).expanduser()
    if not requested.is_absolute() or any(part in {".", ".."} for part in requested.parts):
        raise CampaignReviewError("review store must be an explicit absolute directory")
    current = requested
    ancestors: list[Path] = []
    while current != current.parent:
        ancestors.append(current)
        current = current.parent
    for component in reversed(ancestors):
        if component.is_symlink():
            raise CampaignReviewError(f"review store path may not traverse a symlink: {component}")
    root = assert_writable(requested)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise CampaignReviewError("review store may not be a symlink")
    observed = root.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) & 0o077
    ):
        raise CampaignReviewError("review store must be an owner-only user directory")
    return root


def _read_review_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CampaignReviewError(f"cannot open review receipt {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_size > _MAX_REVIEW_RECEIPT_BYTES
        ):
            raise CampaignReviewError("review receipt is not a bounded owner-only regular file")
        raw = bytearray()
        while len(raw) <= _MAX_REVIEW_RECEIPT_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_REVIEW_RECEIPT_BYTES + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if len(raw) > _MAX_REVIEW_RECEIPT_BYTES or (
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
            raise CampaignReviewError("review receipt changed while it was read")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _closed_review_json(raw: bytes, path: Path) -> dict[str, Any]:
    if not raw.endswith(b"\n"):
        raise CampaignReviewError(f"review receipt is not newline-terminated: {path}")

    def closed(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CampaignReviewError(f"review receipt repeats key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=closed)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignReviewError(f"review receipt is not one UTF-8 JSON object: {path}") from exc
    if not isinstance(value, dict) or json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n" != raw:
        raise CampaignReviewError(f"review receipt is not canonical JSON: {path}")
    try:
        return validate_review_receipt(value)
    except CampaignReviewError:
        raise


class CampaignReviewStore:
    """Create-only, restart-safe storage for exact campaign review receipts."""

    def __init__(self, root: Path | str) -> None:
        self.root = _review_store_root(root)

    def _path(self, review_id: str) -> Path:
        _id(review_id, "review_id")
        return self.root / f"{review_id}.json"

    def write(self, value: dict[str, Any]) -> Path:
        receipt = validate_review_receipt(value)
        target = self._path(receipt["review_id"])
        raw = (
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(raw) > _MAX_REVIEW_RECEIPT_BYTES:
            raise CampaignReviewError("review receipt exceeds its byte bound")
        temporary = self.root / f".{target.name}.{os.getpid()}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except OSError as exc:
            raise CampaignReviewError(f"cannot create review receipt temporary: {exc}") from exc
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise CampaignReviewError(
                    f"refusing to replace review receipt {target}"
                ) from exc
            directory = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise CampaignReviewError(f"cannot publish review receipt {target}: {exc}") from exc
        finally:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()
        return target

    def read(self, review_id: str) -> dict[str, Any]:
        target = self._path(review_id)
        if target.is_symlink() or not target.is_file():
            raise CampaignReviewError(f"review receipt is unavailable: {target}")
        value = _closed_review_json(_read_review_file(target), target)
        if value["review_id"] != review_id:
            raise CampaignReviewError("review receipt filename does not bind its review_id")
        return value

    def read_all(self) -> tuple[dict[str, Any], ...]:
        receipts: list[dict[str, Any]] = []
        for path in sorted(self.root.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                raise CampaignReviewError(
                    f"review store contains an unexpected hidden file: {path.name}"
                )
            if path.suffix != ".json" or _ID.fullmatch(path.stem) is None:
                raise CampaignReviewError(f"review store contains an unknown file: {path.name}")
            receipts.append(self.read(path.stem))
        return tuple(receipts)


def build_review_receipt(
    *,
    campaign_id: str,
    review_id: str,
    author_receipt: dict[str, Any],
    reviewer_receipt: dict[str, Any],
    verdict: str,
    findings: list[dict[str, str]],
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    author = validate_provider_receipt(author_receipt)
    reviewer = validate_provider_receipt(reviewer_receipt)
    candidate = author["candidate"]
    if candidate is None:
        raise CampaignReviewError("author receipt has no clean candidate")
    if reviewer["session_id"] == author["session_id"]:
        raise CampaignReviewError("author and reviewer session identities must differ")
    author_worktree = worktree_sha256(author["cwd"])
    reviewer_worktree = worktree_sha256(reviewer["cwd"])
    if reviewer_worktree == author_worktree:
        raise CampaignReviewError("author and reviewer worktrees must differ")
    if reviewer["launch"]["sandbox"] != "read-only":
        raise CampaignReviewError("campaign reviewer must run read-only")
    if reviewer["launch_repository"]["repository_common_dir_sha256"] != candidate[
        "repository_common_dir_sha256"
    ] or reviewer["launch_repository"]["head_oid"] != candidate["head_oid"] or reviewer[
        "launch_repository"
    ]["tree_oid"] != candidate["tree_oid"]:
        raise CampaignReviewError("reviewer did not launch on the author's exact candidate")
    reviewer_candidate = reviewer["candidate"]
    if reviewer_candidate is None or (
        reviewer_candidate["head_oid"] != candidate["head_oid"]
        or reviewer_candidate["tree_oid"] != candidate["tree_oid"]
    ):
        raise CampaignReviewError("reviewer did not close on the author's exact candidate")

    blockers: list[str] = []
    if verdict != "approve":
        blockers.append(f"review_verdict_{verdict}")
    blockers.extend(
        f"blocking_finding:{finding['finding_id']}"
        for finding in findings
        if finding.get("severity") == "blocker"
    )
    if not author["promotion_eligible"]:
        blockers.append("author_session_not_promotion_eligible")
    if not reviewer["promotion_eligible"]:
        blockers.append("reviewer_session_not_promotion_eligible")
    if reviewer["terminal_state"] != "completed":
        blockers.append("reviewer_session_not_completed")
    value = {
        "schema_version": "1",
        "record_kind": "campaign_review_receipt",
        "campaign_id": campaign_id,
        "review_id": review_id,
        "completed_at": (completed_at or datetime.now(UTC)).astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "author_receipt_sha256": canonical_json_sha256(author),
        "reviewer_receipt_sha256": canonical_json_sha256(reviewer),
        "author_session_id": author["session_id"],
        "reviewer_session_id": reviewer["session_id"],
        "author_worktree_sha256": author_worktree,
        "reviewer_worktree_sha256": reviewer_worktree,
        "candidate": dict(candidate),
        "verdict": verdict,
        "findings": findings,
        "eligible": not blockers,
        "blockers": blockers,
    }
    return validate_review_receipt(value)


__all__ = [
    "CampaignReviewStore",
    "CampaignReviewError",
    "build_review_receipt",
    "canonical_json_sha256",
    "validate_review_receipt",
    "worktree_sha256",
]
