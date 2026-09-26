"""Bounded local attestation for an installed native harness.

The provider APIs needed to prove effective configuration and hook trust are not part of the
installer.  This module therefore records the strongest facts available from the sealed manifest
and the target filesystem, while making the two non-observable claims explicit.  In particular,
matching installed bytes never becomes a claim that Claude Code or Codex loaded, enabled, or
trusted them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.harness_installer import (
    HarnessInstallError,
    TargetCheckoutIdentity,
    TransactionalHarnessInstaller,
    validate_installed_manifest,
)


class HarnessAttestationError(HarnessInstallError):
    """An attestation is malformed or cannot be safely bound to the target."""


_SHA256 = "^[0-9a-f]{64}$"
_PROVIDERS = frozenset({"claude", "codex"})
_STATUS = frozenset({"verified", "mismatch", "unavailable"})
_OID = re.compile(r"^[0-9a-f]{40,64}$")
_PATH = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "provider",
        "observed_at",
        "target",
        "installed_manifest_sha256",
        "native_materialization_sha256",
        "source_policy_sha256",
        "installed_bytes",
        "effective_sources",
        "trust",
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
_FILE_FIELDS = frozenset(
    {
        "path",
        "expected_sha256",
        "observed_sha256",
        "expected_bytes",
        "observed_bytes",
        "expected_mode",
        "observed_mode",
        "link_count",
        "status",
    }
)
_EFFECTIVE_FIELDS = frozenset(
    {"status", "provider_api_observable", "observed_files", "ambient_overlap", "errors", "warnings"}
)
_TRUST_FIELDS = frozenset(
    {"status", "provider_api_observable", "managed", "hook_config_sha256", "errors", "warnings"}
)
_CLAIM_FIELDS = frozenset(
    {
        "installed_bytes_verified",
        "effective_sources_verified",
        "provider_projection_verified",
        "hook_trust_verified",
        "runtime_observed",
    }
)


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _closed(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise HarnessAttestationError(f"{label} has missing or unknown fields")
    return value


def _sha(value: Any, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise HarnessAttestationError(f"{label} must be lowercase SHA-256")
    return value


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


def _read(path: Path) -> tuple[bytes, os.stat_result] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return None
        content = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > 64 * 1024 * 1024:
                return None
        after = os.fstat(descriptor)
        if (
            before.st_ino,
            before.st_dev,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return None
        return bytes(content), after
    finally:
        os.close(descriptor)


def _file_observations(
    target: TargetCheckoutIdentity, manifest: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    complete = True
    for declared in manifest["files"]:
        observed = _read(target.root / declared["path"])
        if observed is None:
            complete = False
            rows.append(
                {
                    "path": declared["path"],
                    "expected_sha256": declared["sha256"],
                    "observed_sha256": None,
                    "expected_bytes": declared["bytes"],
                    "observed_bytes": None,
                    "expected_mode": declared["mode"],
                    "observed_mode": None,
                    "link_count": None,
                    "status": "mismatch",
                }
            )
            continue
        content, metadata = observed
        observed_sha = _digest(content)
        observed_mode = format(stat.S_IMODE(metadata.st_mode), "04o")
        matches = (
            observed_sha == declared["sha256"]
            and len(content) == declared["bytes"]
            and observed_mode == declared["mode"]
        )
        complete &= matches
        rows.append(
            {
                "path": declared["path"],
                "expected_sha256": declared["sha256"],
                "observed_sha256": observed_sha,
                "expected_bytes": declared["bytes"],
                "observed_bytes": len(content),
                "expected_mode": declared["mode"],
                "observed_mode": observed_mode,
                "link_count": metadata.st_nlink,
                "status": "verified" if matches else "mismatch",
            }
        )
    return rows, complete


def _effective_sources(
    target: TargetCheckoutIdentity, provider: str, files: list[dict[str, Any]]
) -> dict[str, Any]:
    # Reading project files is useful diagnostic evidence, but neither provider documents this
    # filesystem view as the complete effective-source API. Keep the claim unavailable.
    paths = {row["path"] for row in files}
    candidates = [
        "CLAUDE.md",
        "AGENTS.md",
        ".claude/settings.json",
        ".codex/hooks.json",
        ".codex/config.toml",
    ]
    observed: list[dict[str, Any]] = []
    for relative in candidates:
        if relative not in paths:
            continue
        item = _read(target.root / relative)
        if item is None:
            continue
        content, metadata = item
        observed.append(
            {
                "path": relative,
                "sha256": _digest(content),
                "bytes": len(content),
                "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
            }
        )
    return {
        "status": "unavailable",
        "provider_api_observable": False,
        "observed_files": observed,
        "ambient_overlap": [],
        "errors": [],
        "warnings": [
            f"{provider} effective source API was not observed; local files are diagnostic only."
        ],
    }


def _trust(
    target: TargetCheckoutIdentity, provider: str, files: list[dict[str, Any]]
) -> dict[str, Any]:
    hook_names = {".claude/settings.json", ".codex/hooks.json"}
    digest = next(
        (
            row["observed_sha256"]
            for row in files
            if row["path"] in hook_names and row["status"] == "verified"
        ),
        None,
    )
    return {
        "status": "unavailable",
        "provider_api_observable": False,
        "managed": None,
        "hook_config_sha256": digest,
        "errors": [],
        "warnings": [
            f"{provider} hook trust is provider-managed and cannot be proven from local bytes."
        ],
    }


def _content_address(value: dict[str, Any]) -> str:
    copied = copy.deepcopy(value)
    copied.pop("content_sha256", None)
    return _digest(_canonical(copied))


def validate_harness_attestation(value: Any) -> dict[str, Any]:
    record = _closed(value, _ATTESTATION_FIELDS, "harness attestation")
    if (
        record["schema_version"] != "1"
        or record["record_kind"] != "provider_harness_attestation"
        or record["provider"] not in _PROVIDERS
    ):
        raise HarnessAttestationError("harness attestation identity is invalid")
    _sha(record["installed_manifest_sha256"], "installed_manifest_sha256", optional=True)
    _sha(record["native_materialization_sha256"], "native_materialization_sha256", optional=True)
    _sha(record["source_policy_sha256"], "source_policy_sha256", optional=True)
    if (record["installed_manifest_sha256"] is None) != (
        record["native_materialization_sha256"] is None or record["source_policy_sha256"] is None
    ):
        raise HarnessAttestationError("attestation manifest and source identities disagree")
    target = _closed(record["target"], _TARGET_FIELDS, "attestation target")
    _sha(target["repository_root_sha256"], "target.repository_root_sha256")
    _sha(target["repository_common_dir_sha256"], "target.repository_common_dir_sha256")
    for key in (
        "repository_root_device",
        "repository_root_inode",
        "repository_common_dir_device",
        "repository_common_dir_inode",
    ):
        if type(target[key]) is not int or target[key] < 0:
            raise HarnessAttestationError(f"target.{key} is invalid")
    if (
        not isinstance(target["head_oid"], str)
        or _OID.fullmatch(target["head_oid"]) is None
        or not isinstance(target["tree_oid"], str)
        or _OID.fullmatch(target["tree_oid"]) is None
        or not isinstance(target["branch"], str)
        or not target["branch"]
    ):
        raise HarnessAttestationError("attestation target Git identity is invalid")
    rows = record["installed_bytes"]
    if not isinstance(rows, list):
        raise HarnessAttestationError("installed_bytes must be an array")
    paths: list[str] = []
    for index, row in enumerate(rows):
        row = _closed(row, _FILE_FIELDS, f"installed_bytes[{index}]")
        _sha(row["expected_sha256"], f"installed_bytes[{index}].expected_sha256")
        _sha(row["observed_sha256"], f"installed_bytes[{index}].observed_sha256", optional=True)
        if (
            row["status"] not in _STATUS
            or not isinstance(row["path"], str)
            or _PATH.fullmatch(row["path"]) is None
        ):
            raise HarnessAttestationError(f"installed_bytes[{index}] is invalid")
        if (
            type(row["expected_bytes"]) is not int
            or row["expected_bytes"] < 0
            or (
                row["observed_bytes"] is not None
                and (type(row["observed_bytes"]) is not int or row["observed_bytes"] < 0)
            )
        ):
            raise HarnessAttestationError(f"installed_bytes[{index}] byte counts are invalid")
        if row["expected_mode"] not in {"0600", "0644", "0755"} or (
            row["observed_mode"] is not None
            and row["observed_mode"] not in {"0600", "0644", "0755"}
        ):
            raise HarnessAttestationError(f"installed_bytes[{index}] modes are invalid")
        if row["link_count"] is not None and (
            type(row["link_count"]) is not int or row["link_count"] < 0
        ):
            raise HarnessAttestationError(f"installed_bytes[{index}] link count is invalid")
        if row["status"] == "verified" and (
            row["observed_sha256"] != row["expected_sha256"]
            or row["observed_bytes"] != row["expected_bytes"]
            or row["observed_mode"] != row["expected_mode"]
            or row["link_count"] != 1
        ):
            raise HarnessAttestationError(f"installed_bytes[{index}] verified row is inconsistent")
        paths.append(row["path"])
    if paths != sorted(set(paths)):
        raise HarnessAttestationError("installed_bytes paths must be sorted and unique")
    for section, fields in (("effective_sources", _EFFECTIVE_FIELDS), ("trust", _TRUST_FIELDS)):
        block = _closed(record[section], fields, section)
        if block["status"] not in _STATUS or not isinstance(block["provider_api_observable"], bool):
            raise HarnessAttestationError(f"{section} status is invalid")
        if not block["provider_api_observable"] and block["status"] == "verified":
            raise HarnessAttestationError(
                f"{section} cannot be verified without provider API evidence"
            )
        if section == "effective_sources":
            if not isinstance(block["observed_files"], list):
                raise HarnessAttestationError("effective_sources.observed_files is invalid")
            for item in block["observed_files"]:
                if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes", "mode"}:
                    raise HarnessAttestationError("effective_sources.observed_files is not closed")
                if _PATH.fullmatch(item["path"]) is None:
                    raise HarnessAttestationError("effective source path is invalid")
                _sha(item["sha256"], "effective source sha256")
                if (
                    type(item["bytes"]) is not int
                    or item["bytes"] < 0
                    or item["mode"] not in {"0600", "0644", "0755"}
                ):
                    raise HarnessAttestationError("effective source file observation is invalid")
            if not isinstance(block["ambient_overlap"], list) or any(
                not isinstance(item, str) for item in block["ambient_overlap"]
            ):
                raise HarnessAttestationError("effective_sources.ambient_overlap is invalid")
        else:
            if block["managed"] is not None and not isinstance(block["managed"], bool):
                raise HarnessAttestationError("trust.managed is invalid")
            _sha(block["hook_config_sha256"], "trust.hook_config_sha256", optional=True)
        if (
            not isinstance(block["errors"], list)
            or not isinstance(block["warnings"], list)
            or any(not isinstance(item, str) for item in (*block["errors"], *block["warnings"]))
        ):
            raise HarnessAttestationError(f"{section} diagnostics are invalid")
    claims = _closed(record["claims"], _CLAIM_FIELDS, "attestation claims")
    if (
        claims["effective_sources_verified"]
        or claims["provider_projection_verified"]
        or claims["hook_trust_verified"]
        or claims["runtime_observed"]
    ):
        raise HarnessAttestationError("attestation overclaims provider effectiveness or runtime")
    expected_installed_claim = bool(rows) and all(row["status"] == "verified" for row in rows)
    if claims["installed_bytes_verified"] is not expected_installed_claim:
        raise HarnessAttestationError("installed byte claim disagrees with observations")
    if (
        not isinstance(record["limitations"], list)
        or not record["limitations"]
        or any(not isinstance(item, str) or not item for item in record["limitations"])
    ):
        raise HarnessAttestationError("limitations must be non-empty strings")
    _sha(record["content_sha256"], "content_sha256")
    if record["content_sha256"] != _content_address(record):
        raise HarnessAttestationError("attestation content digest is false")
    return copy.deepcopy(record)


def attest_installed_harness(
    *,
    installer: TransactionalHarnessInstaller,
    provider: str,
    target: TargetCheckoutIdentity,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Attest installed bytes and report effective/trust surfaces as unavailable."""
    if provider not in _PROVIDERS:
        raise HarnessAttestationError(f"unsupported provider: {provider!r}")
    installed = installer.read_installed(provider=provider, target=target)
    if installed is None:
        rows: list[dict[str, Any]] = []
        installed_sha = None
        native_sha = None
        policy_sha = None
    else:
        validate_installed_manifest(installed)
        rows, _ = _file_observations(target, installed)
        installed_sha = installed["content_sha256"]
        native_sha = installed["native_materialization_sha256"]
        policy_sha = installed["source_policy_sha256"]
    effective = _effective_sources(target, provider, rows)
    trust = _trust(target, provider, rows)
    record: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "provider_harness_attestation",
        "provider": provider,
        "observed_at": (observed_at or datetime.now(UTC))
        .astimezone(UTC)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": _target_record(target),
        "installed_manifest_sha256": installed_sha,
        "native_materialization_sha256": native_sha,
        "source_policy_sha256": policy_sha,
        "installed_bytes": sorted(rows, key=lambda row: row["path"]),
        "effective_sources": effective,
        "trust": trust,
        "claims": {
            "installed_bytes_verified": bool(rows)
            and all(row["status"] == "verified" for row in rows),
            "effective_sources_verified": False,
            "provider_projection_verified": False,
            "hook_trust_verified": False,
            "runtime_observed": False,
        },
        "limitations": [
            "Local manifest and target bytes are verified with no-follow regular-file reads.",
            "Provider effective-source APIs were not observed; local configuration files are "
            "diagnostic only.",
            "Provider hook trust and runtime behavior are not observable in this attestation.",
        ],
        "content_sha256": "",
    }
    record["content_sha256"] = _content_address(record)
    return validate_harness_attestation(record)


__all__ = ["HarnessAttestationError", "attest_installed_harness", "validate_harness_attestation"]
