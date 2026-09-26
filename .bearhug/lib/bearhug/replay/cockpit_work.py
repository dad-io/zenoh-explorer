"""Project one explicit provider-work record and binding into cockpit v4.

There is deliberately no directory scan here.  The caller names at most one observation and one
binding file; their content identities must agree with each other and with the selected checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from bearhug.providers.work_authority import (
    validate_provider_work_observation,
    validate_work_binding,
)
from bearhug.providers.work_store import read_subject_file, repository_identities

_MAX_RECORD_BYTES = 2 * 1024 * 1024
_SUPPORTED_PROVIDERS = frozenset({"anthropic-claude", "openai-codex"})


class CockpitWorkError(ValueError):
    """An explicitly supplied work-authority record cannot be trusted for this subject."""


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CockpitWorkError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_record(path: Path | str, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    source = Path(os.path.abspath(Path(path).expanduser()))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise CockpitWorkError(f"cannot read explicit {label} file: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CockpitWorkError(f"explicit {label} source is not a regular file")
        chunks: list[bytes] = []
        remaining = _MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise CockpitWorkError(f"explicit {label} source changed while it was read")
    if len(raw) > _MAX_RECORD_BYTES:
        raise CockpitWorkError(f"explicit {label} file exceeds {_MAX_RECORD_BYTES} bytes")
    try:
        value = json.loads(raw, object_pairs_hook=_closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CockpitWorkError(f"explicit {label} file is not closed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CockpitWorkError(f"explicit {label} file is not a JSON object")
    return value, {
        "path": str(source),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_count": len(raw),
    }


def _subject_identity(subject_root: Path | str | None) -> dict[str, Any]:
    if subject_root is None:
        return {
            "status": "unavailable",
            "reason": "no subject root supplied; work binding identity cannot be checked",
            "repository_common_dir_sha256": None,
            "worktree_sha256": None,
        }
    root = Path(subject_root).expanduser()
    if not root.is_dir():
        return {
            "status": "unavailable",
            "reason": "selected subject root is not a directory",
            "repository_common_dir_sha256": None,
            "worktree_sha256": None,
        }
    try:
        common_digest, worktree_digest, _ = repository_identities(root)
    except (OSError, ValueError) as exc:
        return {
            "status": "unavailable",
            "reason": f"cannot inspect selected subject Git identity: {exc}",
            "repository_common_dir_sha256": None,
            "worktree_sha256": None,
        }
    return {
        "status": "carried",
        "reason": "",
        "repository_common_dir_sha256": common_digest,
        "worktree_sha256": worktree_digest,
    }


def _empty(status: str, reason: str, subject: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "observation_source": None,
        "binding_source": None,
        "subject": subject,
        "observation": None,
        "binding": None,
    }


def _observation_projection(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": observation["observation_id"],
        "observed_at": observation["observed_at"],
        "provider": observation["provider"],
        "session_id": observation["session_id"],
        "thread_id": observation["thread_id"],
        "work": observation["work"],
        "limitations": observation["limitations"],
    }


def _binding_projection(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "binding_id": binding["binding_id"],
        "provider_work_observation_id": binding["provider_work_observation_id"],
        "provider": binding["provider"],
        "session_id": binding["session_id"],
        "thread_id": binding["thread_id"],
        "task_id": binding["task_id"],
        "repository_common_dir_sha256": binding["repository_common_dir_sha256"],
        "worktree_sha256": binding["worktree_sha256"],
        "active_plan_path": binding["active_plan_path"],
        "active_plan_sha256": binding["active_plan_sha256"],
        "resolution": binding["resolution"],
        "bindings": binding["bindings"],
    }


def provider_work_for_cockpit(
    *,
    subject_root: Path | str | None,
    provider_work_observation: Path | str | None,
    work_binding: Path | str | None,
) -> dict[str, Any]:
    """Return the closed v4 projection for exactly the files the caller supplied."""

    subject = _subject_identity(subject_root)
    if provider_work_observation is None:
        if work_binding is not None:
            return _empty(
                "unavailable",
                "a work binding was supplied without its provider work observation",
                subject,
            )
        return _empty("unreported", "no provider work observation supplied", subject)

    try:
        raw_observation, observation_source = _read_record(
            provider_work_observation, "provider work observation"
        )
        observation = validate_provider_work_observation(raw_observation)
    except ValueError as exc:
        return _empty("unavailable", f"provider work observation rejected: {exc}", subject)
    if observation["provider"] not in _SUPPORTED_PROVIDERS:
        block = _empty(
            "unavailable",
            f"provider {observation['provider']} is not supported by cockpit v4",
            subject,
        )
        block["observation_source"] = observation_source
        return block

    block = {
        "status": "observed",
        "reason": "no explicit work binding supplied; provider work is not joined to BOARD",
        "observation_source": observation_source,
        "binding_source": None,
        "subject": subject,
        "observation": _observation_projection(observation),
        "binding": None,
    }
    if work_binding is None:
        return block

    try:
        raw_binding, binding_source = _read_record(work_binding, "work binding")
        binding = validate_work_binding(raw_binding, observation=observation)
    except ValueError as exc:
        block.update(
            {
                "status": "unavailable",
                "reason": f"work binding rejected: {exc}",
                "binding_source": None,
                "binding": None,
            }
        )
        return block
    if subject["status"] != "carried":
        block.update(
            {
                "status": "unavailable",
                "reason": (
                    "work binding cannot be checked because selected subject identity is "
                    "unavailable"
                ),
                "binding_source": binding_source,
            }
        )
        return block
    for field in ("repository_common_dir_sha256", "worktree_sha256"):
        if binding[field] != subject[field]:
            block.update(
                {
                    "status": "unavailable",
                    "reason": f"work binding {field} does not match the selected subject",
                    "binding_source": binding_source,
                }
            )
            return block

    try:
        second_common, second_worktree, git = repository_identities(
            Path(subject_root).expanduser()  # type: ignore[arg-type]
        )
        if (
            second_common != subject["repository_common_dir_sha256"]
            or second_worktree != subject["worktree_sha256"]
        ):
            raise CockpitWorkError("selected subject identity changed before active-plan read")
        root = Path(git.worktree)
        plan_bytes = read_subject_file(root, binding["active_plan_path"])
        third_common, third_worktree, _ = repository_identities(root)
        if third_common != second_common or third_worktree != second_worktree:
            raise CockpitWorkError("selected subject identity changed during active-plan read")
    except (OSError, ValueError) as exc:
        block.update(
            {
                "status": "unavailable",
                "reason": (
                    "work binding active plan is not safely readable in the selected subject: "
                    f"{exc}"
                ),
                "binding_source": binding_source,
            }
        )
        return block
    plan_digest = hashlib.sha256(plan_bytes).hexdigest()
    if plan_digest != binding["active_plan_sha256"]:
        block.update(
            {
                "status": "unavailable",
                "reason": (
                    "work binding active_plan_sha256 does not match the selected subject"
                ),
                "binding_source": binding_source,
            }
        )
        return block

    block.update(
        {
            "status": binding["resolution"],
            "reason": (
                ""
                if binding["resolution"] == "bound"
                else f"explicit BOARD binding is {binding['resolution']}"
            ),
            "binding_source": binding_source,
            "binding": _binding_projection(binding),
        }
    )
    return block


__all__ = ["CockpitWorkError", "provider_work_for_cockpit"]
