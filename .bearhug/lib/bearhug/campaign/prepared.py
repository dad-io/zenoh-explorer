"""Prepare and reopen one native execution campaign.

Preparation is the small, durable boundary between project authored authority and the campaign
runtime.  It reads explicit records, checks their identities against one clean Git worktree, and
stores the records and their source bytes by digest.  It never creates a worktree, runs a command,
launches a provider, or changes the subject repository.

The record written here is intentionally boring JSON.  It is a custody receipt that tells the
runtime exactly which authority was selected and where its state will live; it is not a second
campaign schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.campaign.capsule_storage import CapsuleObjectStore
from bearhug.campaign.capsules import (
    CapsuleContractError,
    validate_capsule_plan,
    validate_intent_envelope,
)
from bearhug.campaign.importer import CampaignImportError, SubjectIdentity, inspect_subject
from bearhug.campaign.worktrees import WorktreeInventoryError, inventory_worktrees
from bearhug.paths import RUNS_DIR, WriteBoundaryError, assert_writable
from bearhug.providers.policy import (
    ProviderPolicy,
    ProviderPolicyError,
    validate_provider_policy,
)
from bearhug.providers.qualification_index import (
    ProviderQualificationIndex,
    ProviderQualificationIndexError,
    load_bound_provider_qualification_index,
    load_provider_qualification_index,
)


class PreparedCampaignError(ValueError):
    """An explicit native campaign cannot be prepared or reopened safely."""


_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_JSON_BYTES = 256 * 1024 * 1024
_MAX_SOURCE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PreparedSource:
    """One authority source and the exact bytes sealed for runtime grounding."""

    source_id: str
    path: str | None
    content_sha256: str
    content: bytes
    blob_path: Path


@dataclass(frozen=True, slots=True)
class PreparedCampaign:
    """The validated native authority and the exact local custody root."""

    record: dict[str, Any]
    intent: dict[str, Any]
    plan: dict[str, Any]
    provider_policy: ProviderPolicy
    qualification_index: ProviderQualificationIndex
    # A list keeps the projection directly inspectable by the runtime and cockpit. Each row has
    # source_id/path/content_sha256/blob_path and the exact bytes under ``content``.
    source_contents: list[dict[str, Any]]
    root: Path


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PreparedCampaignError(f"value is not canonical JSON: {exc}") from exc


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _writable(path: Path, *, label: str) -> Path:
    try:
        return assert_writable(path)
    except WriteBoundaryError as exc:
        raise PreparedCampaignError(f"{label} is outside the Bear Hug write boundary") from exc


def _json_digest(value: Any) -> str:
    """Digest one JSON value using the policy/runtime canonical form (without a newline)."""

    try:
        data = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PreparedCampaignError(f"value is not canonical JSON: {exc}") from exc
    return _digest(data)


def _closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PreparedCampaignError(f"JSON repeats key {key!r}")
        result[key] = value
    return result


def _read_regular(path: Path, *, maximum: int) -> bytes:
    """Read one owner-owned regular file without following the file or its aliases."""

    requested = Path(path)
    if not requested.is_absolute() or str(requested) != str(requested.resolve(strict=False)):
        raise PreparedCampaignError(f"explicit locator must be an exact absolute path: {path}")
    if requested.is_symlink():
        raise PreparedCampaignError(f"explicit locator may not be a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(requested, flags)
    except OSError as exc:
        raise PreparedCampaignError(f"cannot read explicit file {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_size > maximum
        ):
            raise PreparedCampaignError(f"explicit file is not a bounded user-owned file: {path}")
        chunks: list[bytes] = []
        size = 0
        while size <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > maximum or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PreparedCampaignError(f"explicit file changed while reading: {path}")
        return raw
    finally:
        os.close(descriptor)


def _read_json(
    path: Path, *, label: str, maximum: int = _MAX_JSON_BYTES
) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular(path, maximum=maximum)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_pairs)
    except PreparedCampaignError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedCampaignError(f"{label} is not one UTF-8 JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise PreparedCampaignError(f"{label} must contain one JSON object")
    return value, raw


def _exact_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    value, raw = _read_json(path, label=label)
    return value, raw


def _absolute_file(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise PreparedCampaignError(f"{label} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PreparedCampaignError(f"{label} is unavailable: {path}") from exc
    if resolved != path or path.is_symlink() or not path.is_file():
        raise PreparedCampaignError(f"{label} must be an exact physical file: {path}")
    return path


def _absolute_directory(
    value: str | Path, *, label: str, create: bool = False, private: bool = True
) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise PreparedCampaignError(f"{label} must be an exact absolute path")
    if path.is_symlink():
        raise PreparedCampaignError(f"{label} may not be a symlink: {path}")
    try:
        canonical = path.resolve(strict=False)
    except OSError as exc:
        raise PreparedCampaignError(f"{label} is unavailable: {path}") from exc
    if canonical != path:
        raise PreparedCampaignError(f"{label} must be a physical path: {path}")
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise PreparedCampaignError(f"cannot create {label}: {path}: {exc}") from exc
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PreparedCampaignError(f"{label} is unavailable: {path}") from exc
    if resolved != path or not path.is_dir():
        raise PreparedCampaignError(f"{label} must be a physical directory: {path}")
    info = path.stat(follow_symlinks=False)
    if info.st_uid != os.geteuid() or (private and stat.S_IMODE(info.st_mode) & 0o077):
        qualifier = "owner-only and user-owned" if private else "user-owned"
        raise PreparedCampaignError(f"{label} must be {qualifier}: {path}")
    return path


def _repository_relative(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise PreparedCampaignError(f"{label} must be a canonical repository-relative path")
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or value.startswith("~")
        or value.endswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or candidate.as_posix() != value
    ):
        raise PreparedCampaignError(f"{label} must be a canonical repository-relative path")
    return value


def _token(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise PreparedCampaignError(f"{label} must be a lowercase token")
    return value


def _sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PreparedCampaignError(f"{label} must be a lowercase SHA-256")
    return value


def _oid(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _GIT_OID.fullmatch(value) is None:
        raise PreparedCampaignError(f"{label} must be a Git object id")
    return value


def _git(root: Path, *arguments: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            (
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(root),
                *arguments,
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreparedCampaignError(f"cannot inspect subject Git identity: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise PreparedCampaignError(f"git {' '.join(arguments)} failed: {detail}")
    value = result.stdout.decode("utf-8", errors="strict").strip()
    if not value or "\n" in value or "\r" in value:
        raise PreparedCampaignError(f"Git returned invalid value for {' '.join(arguments)}")
    return value


def _verify_base(subject: SubjectIdentity, plan: Mapping[str, Any]) -> None:
    declared = plan["subject"]
    if declared["repository_id"] == "":
        raise PreparedCampaignError("plan subject repository_id must be non-empty")
    base_oid = _oid(declared["base_oid"], label="plan.subject.base_oid")
    base = _git(subject.root, "rev-parse", "--verify", f"{base_oid}^{{commit}}")
    base_tree = _git(subject.root, "rev-parse", "--verify", f"{base_oid}^{{tree}}")
    if base != base_oid:
        raise PreparedCampaignError("plan subject base_oid is not the exact resolved commit")
    if _digest(base_tree.encode("ascii")) != declared["base_tree_sha256"]:
        raise PreparedCampaignError("plan subject base_tree_sha256 does not match Git")
    ancestor = subprocess.run(
        (
            "git",
            "--no-optional-locks",
            "-C",
            str(subject.root),
            "merge-base",
            "--is-ancestor",
            base_oid,
            "HEAD",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": os.environ.get("HOME", "/nonexistent"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        },
    )
    if ancestor.returncode != 0:
        raise PreparedCampaignError("plan subject base_oid is not an ancestor of subject HEAD")


def _lease_subject_root(subject: SubjectIdentity) -> Path:
    """Pin the repository's one main worktree as the cross-campaign lease anchor."""

    try:
        inventory = inventory_worktrees(subject=subject.root, common_dir=subject.common_dir)
    except WorktreeInventoryError as exc:
        raise PreparedCampaignError(
            f"cannot establish the repository worktree anchor: {exc}"
        ) from exc
    mains = [entry.path for entry in inventory.entries if entry.kind == "main"]
    if len(mains) != 1:
        raise PreparedCampaignError("repository must expose exactly one physical main worktree")
    anchor = mains[0]
    if anchor.is_symlink() or anchor.resolve(strict=True) != anchor or not anchor.is_dir():
        raise PreparedCampaignError("repository main worktree anchor is not physical")
    return anchor


def _ensure_native_approved(intent: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    if intent.get("mode") != "native_v2":
        raise PreparedCampaignError("native prepare requires an explicit native_v2 intent")
    approval = intent.get("approval")
    envelope_approval = intent.get("campaign_envelope", {}).get("approval")
    for label, value in (("intent", approval), ("campaign envelope", envelope_approval)):
        if not isinstance(value, Mapping) or value.get("mode") not in {
            "project_sealed",
            "human_approved",
        }:
            raise PreparedCampaignError(f"{label} must carry explicit project or human approval")
        if value.get("approved_by") == "adapter":
            raise PreparedCampaignError(
                f"{label} adapter approval cannot authorize native execution"
            )
    revision = plan.get("revision")
    if not isinstance(revision, Mapping) or revision.get("approval_mode") != "initial":
        raise PreparedCampaignError("native prepare requires an explicitly approved initial plan")
    if revision.get("predecessor_sha256") is not None:
        raise PreparedCampaignError("native prepare cannot start from a successor plan")


def _argv(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PreparedCampaignError(f"{label} must be a non-empty argv array")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or "\x00" in item:
            raise PreparedCampaignError(f"{label}[{index}] must be a non-empty argv string")
        result.append(item)
    return result


def validate_preflight_findings(
    value: Any, *, intent: Mapping[str, Any], plan: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Normalize explicitly supplied observations; never infer meaning from plan prose.

    Referencing a sealed digest admits the finding for reconciliation, not its interpretation as
    accepted truth. Only authority sources have byte custody in the current prepared contract;
    binding-only digests must be rejected before preparation publishes a locator. Source bytes
    are verified while preparing and again at runtime. Existing reconciliation/HIL/revision rules
    decide whether any provider spend may follow.
    """
    kinds = {
        "invalidated_assumption", "meaning_conflict", "concept_drift", "invariant_conflict",
        "scope_change", "authority_change", "mutation_envelope_change",
    }
    if not isinstance(value, list) or len(value) > 64:
        raise PreparedCampaignError("preflight_findings must contain at most 64 capsule rows")
    if len(_canonical(value)) > 256 * 1024:
        raise PreparedCampaignError("preflight_findings exceeds 256 KiB")
    capsules = {row["capsule_id"]: row for row in plan["capsules"]}
    authority = {row["content_sha256"] for row in intent["authority_refs"]}
    bindings = {row["binding_id"]: row for row in intent["bindings"]}
    normalized = []
    seen = set()
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {"capsule_id", "discoveries"}:
            raise PreparedCampaignError("preflight finding capsule row is not closed")
        cid = _token(row["capsule_id"], label="preflight capsule_id")
        if cid not in capsules or cid in seen:
            raise PreparedCampaignError("preflight capsule_id must name a unique sealed capsule")
        seen.add(cid)
        admitted = authority | {
            ref
            for binding_id in capsules[cid]["binding_refs"]
            for ref in bindings[binding_id]["evidence_refs"]
        }
        discoveries = row["discoveries"]
        if not isinstance(discoveries, list) or not 1 <= len(discoveries) <= 16:
            raise PreparedCampaignError("preflight discoveries must contain 1..16 findings")
        result = []
        duplicates = set()
        for discovery in discoveries:
            if not isinstance(discovery, Mapping) or set(discovery) != {
                "kind", "summary", "actor", "evidence_refs",
            }:
                raise PreparedCampaignError("preflight discovery is not closed")
            if not isinstance(discovery["kind"], str) or discovery["kind"] not in kinds:
                raise PreparedCampaignError("preflight discovery kind is unsupported")
            for field, maximum in (("summary", 3500), ("actor", 256)):
                text = discovery[field]
                if (
                    not isinstance(text, str) or not text.strip()
                    or len(text.encode("utf-8")) > maximum
                ):
                    raise PreparedCampaignError(f"preflight {field} must be bounded nonempty text")
                if any(ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F for char in text):
                    raise PreparedCampaignError(f"preflight {field} contains a control character")
            refs = discovery["evidence_refs"]
            if not isinstance(refs, list) or not 1 <= len(refs) <= 16:
                raise PreparedCampaignError("preflight evidence_refs must contain 1..16 digests")
            refs = [_sha(ref, label="preflight evidence reference") for ref in refs]
            if len(set(refs)) != len(refs) or not set(refs) <= admitted:
                raise PreparedCampaignError(
                    "preflight evidence must be unique sealed authority or capsule binding digests"
                )
            # `_validate_execution_config` requires authority_sources to exactly match the
            # sealed authority IDs; `_source_contents` verifies and materializes those bytes.
            # A binding evidence reference alone has no source-content field in this contract.
            if not set(refs) <= authority:
                raise PreparedCampaignError(
                    "preflight evidence must name an authority source with prepared byte custody"
                )
            finding = {**discovery, "evidence_refs": sorted(refs)}
            digest = _digest(_canonical(finding))
            if digest in duplicates:
                raise PreparedCampaignError("preflight discovery is duplicated")
            duplicates.add(digest)
            result.append(finding)
        normalized.append({"capsule_id": cid, "discoveries": result})
    return sorted(normalized, key=lambda row: row["capsule_id"])


def _validate_execution_config(
    value: Mapping[str, Any],
    *,
    config_path: Path,
    subject: SubjectIdentity,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], Path]:
    allowed = {
        "authority_sources",
        "validation_commands",
        "artifact_bindings",
        "system_checks",
        "qualification_index",
        "review_role",
        "checkpoint",
        "preflight_findings",
        "grounding_sources",
        "lease_materialized_paths",
    }
    required = {
        "authority_sources",
        "validation_commands",
        "artifact_bindings",
        "system_checks",
        "qualification_index",
    }
    if set(value) - allowed or not required <= set(value):
        raise PreparedCampaignError("execution config has missing or unknown fields")

    # Evidence a validation command needs but Git does not carry: a leased worktree
    # materializes only tracked files, so a gate that reads an ignored artifact fails there
    # for a reason the capsule cannot repair. The list is sealed here and approved by digest;
    # provisioning enforces what may actually be copied.
    raw_materialized = value.get("lease_materialized_paths", [])
    if not isinstance(raw_materialized, list):
        raise PreparedCampaignError("execution config lease_materialized_paths must be a list")
    materialized: list[str] = []
    for entry in raw_materialized:
        if not isinstance(entry, str) or not entry or entry != entry.strip():
            raise PreparedCampaignError("lease_materialized_paths entries must be exact text")
        candidate = PurePosixPath(entry)
        if candidate.is_absolute() or ".." in candidate.parts or entry.startswith("/"):
            raise PreparedCampaignError(
                f"lease_materialized_paths entry escapes the subject: {entry}"
            )
        if entry in materialized:
            raise PreparedCampaignError(f"lease_materialized_paths repeats {entry}")
        materialized.append(entry)
    if len(materialized) > 32:
        raise PreparedCampaignError("lease_materialized_paths exceeds 32 declared entries")

    raw_sources = value["authority_sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise PreparedCampaignError("execution config authority_sources must be non-empty")
    sources: list[dict[str, str]] = []
    source_ids: set[str] = set()
    for index, row in enumerate(raw_sources):
        if not isinstance(row, Mapping) or set(row) not in (
            {"source_id", "path"}, {"source_id", "request"}
        ):
            raise PreparedCampaignError(f"authority_sources[{index}] is not closed")
        source_id = _token(row["source_id"], label=f"authority_sources[{index}].source_id")
        if "request" in row:
            if not source_id.startswith("request.") or not isinstance(row["request"], str):
                raise PreparedCampaignError("inline authority must be a named user request")
            if not row["request"] or len(row["request"].encode("utf-8")) > _MAX_SOURCE_BYTES:
                raise PreparedCampaignError("inline user request is empty or too large")
            source = {"source_id": source_id, "request": row["request"]}
        else:
            path = _repository_relative(row["path"], label=f"authority_sources[{index}].path")
            source = {"source_id": source_id, "path": path}
        if source_id in source_ids:
            raise PreparedCampaignError(f"authority_sources repeats source_id {source_id!r}")
        source_ids.add(source_id)
        sources.append(source)
    sources.sort(key=lambda item: item["source_id"])

    refs = intent.get("authority_refs")
    if not isinstance(refs, list):
        raise PreparedCampaignError("intent authority_refs must be an array")
    ref_map = {row.get("source_id"): row for row in refs if isinstance(row, Mapping)}
    if set(ref_map) != source_ids:
        raise PreparedCampaignError(
            "execution authority_sources must exactly match intent authority_refs"
        )

    raw_commands = value["validation_commands"]
    if not isinstance(raw_commands, Mapping) or not raw_commands:
        raise PreparedCampaignError("validation_commands must be a non-empty object")
    commands: dict[str, list[str]] = {}
    for ref, command in raw_commands.items():
        _token(ref, label="validation command reference")
        if ref in commands:
            raise PreparedCampaignError(f"validation_commands repeats reference {ref!r}")
        commands[ref] = _argv(command, label=f"validation_commands.{ref}")
    sealed_commands = {
        ref
        for capsule in plan["capsules"]
        for profile in capsule["validation_profiles"]
        for ref in profile["command_refs"]
    }
    if set(commands) != sealed_commands:
        raise PreparedCampaignError(
            "validation_commands must exactly equal the command references sealed by plan profiles"
        )

    raw_artifacts = value["artifact_bindings"]
    if not isinstance(raw_artifacts, Mapping):
        raise PreparedCampaignError("artifact_bindings must be an object")
    capsule_ids = {capsule["capsule_id"] for capsule in plan["capsules"]}

    def binding_row(artifact_ref: str, binding: Any, label: str) -> dict[str, str]:
        _token(artifact_ref, label="artifact binding reference")
        if not isinstance(binding, Mapping) or set(binding) != {"command_ref", "stream"}:
            raise PreparedCampaignError(f"{label} is not closed")
        command_ref = _token(binding["command_ref"], label=f"{label}.command_ref")
        if command_ref not in commands:
            raise PreparedCampaignError(f"{label} names unknown command")
        stream = binding["stream"]
        if stream not in {"stdout", "stderr"}:
            raise PreparedCampaignError(f"{label}.stream is unsupported")
        return {"command_ref": command_ref, "stream": stream}

    # Scope bindings by capsule so the same artifact alias can resolve to different commands in
    # different capsules without an implicit or ambiguous global mapping.
    artifacts: dict[str, dict[str, dict[str, str]]] = {}
    if set(raw_artifacts) != capsule_ids:
        raise PreparedCampaignError("artifact_bindings must name every sealed capsule exactly")
    for capsule in plan["capsules"]:
        capsule_id = capsule["capsule_id"]
        rows = raw_artifacts[capsule_id]
        if not isinstance(rows, Mapping):
            raise PreparedCampaignError(f"artifact_bindings.{capsule_id} must be an object")
        required = set(capsule["completion_boundary"]["required_artifact_refs"])
        if set(rows) != required:
            raise PreparedCampaignError(
                f"artifact_bindings.{capsule_id} must exactly equal that capsule's "
                "required artifacts"
            )
        artifacts[capsule_id] = {
            ref: binding_row(ref, rows[ref], f"artifact_bindings.{capsule_id}.{ref}")
            for ref in sorted(rows)
        }
        capsule_commands = {
            ref for profile in capsule["validation_profiles"] for ref in profile["command_refs"]
        }
        if any(
            row["command_ref"] not in capsule_commands
            for row in artifacts[capsule_id].values()
        ):
            raise PreparedCampaignError(
                f"artifact_bindings.{capsule_id} names a command outside that capsule's profiles"
            )

    raw_checks = value["system_checks"]
    if not isinstance(raw_checks, list) or not raw_checks:
        raise PreparedCampaignError(
            "system_checks must contain at least one explicit argv command"
        )
    checks = [
        _argv(command, label=f"system_checks[{index}]") for index, command in enumerate(raw_checks)
    ]

    role = value.get("review_role", "reviewer")
    role = _token(role, label="review_role")
    checkpoint = value.get("checkpoint")
    if checkpoint not in {None, "capsule", "phase"}:
        raise PreparedCampaignError("checkpoint must be capsule, phase, or null")
    qualification_value = value["qualification_index"]
    if not isinstance(qualification_value, str) or not qualification_value:
        raise PreparedCampaignError("qualification_index must be a path")
    qualification_path = Path(qualification_value).expanduser()
    if not qualification_path.is_absolute():
        qualification_path = config_path.parent / qualification_path
    qualification_path = _absolute_file(qualification_path, label="qualification index")

    normalized = {
        "authority_sources": sources,
        "validation_commands": {key: commands[key] for key in sorted(commands)},
        "artifact_bindings": {key: artifacts[key] for key in sorted(artifacts)},
        "system_checks": checks,
        "qualification_index": str(qualification_path),
        "review_role": role,
        "checkpoint": checkpoint,
    }
    if "grounding_sources" in value:
        from bearhug.campaign.grounding import GroundingError, validate_grounding_sources

        try:
            normalized["grounding_sources"] = validate_grounding_sources(
                value["grounding_sources"], subject_root=subject.root
            )
        except GroundingError as exc:
            raise PreparedCampaignError(f"grounding_sources: {exc}") from exc
    if "preflight_findings" in value:
        normalized["preflight_findings"] = validate_preflight_findings(
            value["preflight_findings"], intent=intent, plan=plan,
        )
    if materialized:
        # Absent means none. Emitting an empty list would rewrite the normalized config of
        # every record sealed before this key existed, and a sealed record that its own
        # controller can no longer read cannot even be stopped.
        normalized["lease_materialized_paths"] = materialized
    return normalized, qualification_path


def _prepare_paths(
    *,
    root: Path,
    campaign_id: str,
    run_id: str,
    worktree_parent: Path | None,
    repository_common_dir_sha256: str,
    lease_root: Path | None,
) -> dict[str, str]:
    if worktree_parent is None:
        worktree = root / "worktrees"
        _absolute_directory(worktree, label="worktree parent", create=True)
    else:
        _writable(worktree_parent, label="worktree parent")
        worktree = _absolute_directory(worktree_parent, label="worktree parent", create=True)
    derived_lease_root = _writable(
        RUNS_DIR / "capsule-leases" / repository_common_dir_sha256,
        label="shared campaign lease root",
    )
    if lease_root is not None and lease_root.resolve(strict=False) != derived_lease_root:
        raise PreparedCampaignError(
            "lease root must be the repository shared coordination root derived "
            "from Git common identity"
        )
    lease = _absolute_directory(
        lease_root or derived_lease_root,
        label="shared campaign lease root",
        create=True,
    )
    campaign_root = root / "campaigns" / campaign_id
    run_root = campaign_root / "runs" / run_id
    # These are derived custody locations.  Their parents are created so a runtime can write into
    # them without another path-discovery step; no provider, Git, or product file is touched.
    for path in (
        campaign_root,
        run_root,
        run_root / "provider-output",
        run_root / "integration",
    ):
        _absolute_directory(path, label="derived campaign directory", create=True)
    return {
        "state_root": str(root),
        "campaign_root": str(campaign_root),
        "run_root": str(run_root),
        "worktree_parent": str(worktree),
        "lease_root": str(lease),
        "provider_output_root": str(run_root / "provider-output"),
        "integration_root": str(run_root / "integration"),
    }


def _write_create_only(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.stat(follow_symlinks=False)
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise PreparedCampaignError(
            f"prepared artifact directory must be owner-only: {path.parent}"
        )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise PreparedCampaignError(f"prepared artifact already exists: {path}") from exc
    except OSError as exc:
        raise PreparedCampaignError(f"cannot create prepared artifact {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        # fdopen closes the descriptor on normal and exceptional exits.
        pass
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_blob(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        if (
            path.is_symlink()
            or not path.is_file()
            or _digest(_read_regular(path, maximum=_MAX_SOURCE_BYTES)) != path.stem
        ):
            raise PreparedCampaignError(f"content-addressed source blob is mismatched: {path}")
        return
    _write_create_only(path, data)


def _source_contents(
    *, root: Path, subject: SubjectIdentity, intent: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, PreparedSource]:
    source_rows = config["authority_sources"]
    result: dict[str, PreparedSource] = {}
    blob_root = root / "source-blobs"
    _absolute_directory(blob_root, label="source blob root", create=True)
    refs = {row["source_id"]: row for row in intent["authority_refs"]}
    for row in source_rows:
        source_id = row["source_id"]
        expected = _sha(
            refs[source_id]["content_sha256"], label=f"intent authority {source_id} digest"
        )
        if "request" in row:
            raw = row["request"].encode("utf-8")
        else:
            source_path = _absolute_file(
                subject.root / row["path"], label=f"authority source {source_id}"
            )
            raw = _read_regular(source_path, maximum=_MAX_SOURCE_BYTES)
        actual = _digest(raw)
        if actual != expected:
            raise PreparedCampaignError(
                f"authority source {source_id!r} digest mismatch: expected {expected}, "
                f"observed {actual}"
            )
        blob_path = blob_root / f"{actual}.blob"
        _write_blob(blob_path, raw)
        result[source_id] = PreparedSource(source_id, row.get("path"), actual, raw, blob_path)
    return result


def _record_digest(record: Mapping[str, Any]) -> str:
    body = dict(record)
    body.pop("content_sha256", None)
    return _digest(_canonical(body))


def _build_prepared(
    *,
    locator: Path,
    subject: SubjectIdentity,
    lease_subject_root: Path,
    intent: dict[str, Any],
    plan: dict[str, Any],
    intent_raw: bytes,
    plan_raw: bytes,
    policy: ProviderPolicy,
    policy_value: dict[str, Any],
    policy_raw: bytes,
    policy_path: Path,
    qualification: ProviderQualificationIndex,
    qualification_path: Path,
    config: dict[str, Any],
    config_raw: bytes,
    config_path: Path,
    intent_path: Path,
    plan_path: Path,
    state_root: Path | None,
    worktree_parent: Path | None,
    campaign_id: str | None,
    run_id: str | None,
    lease_root: Path | None,
) -> PreparedCampaign:
    intent_result = validate_intent_envelope(intent)
    plan_result = validate_capsule_plan(plan, intent_envelope=intent)
    _ensure_native_approved(intent, plan)
    policy_sha = _json_digest(policy_value)
    if policy_sha not in intent["campaign_envelope"]["policy_refs"]:
        raise PreparedCampaignError(
            "provider policy digest is not authorized by the intent envelope"
        )

    seed = {
        "subject": {
            "path": str(subject.root),
            "common_dir": str(subject.common_dir),
            "repository_common_dir_sha256": subject.repository_common_dir_sha256,
            "head_oid": subject.head_oid,
            "tree_oid": subject.tree_oid,
            "lease_subject_root": str(lease_subject_root),
        },
        "intent_sha256": intent_result.digest,
        "plan_sha256": plan_result.digest,
        "intent_source_sha256": _digest(intent_raw),
        "plan_source_sha256": _digest(plan_raw),
        "policy_sha256": policy_sha,
        "policy_source_sha256": _digest(policy_raw),
        "qualification_index_sha256": qualification.sha256,
        "execution_config_sha256": _digest(config_raw),
        "campaign_id": campaign_id,
        "run_id": run_id,
        "lease_subject_root": str(lease_subject_root),
    }
    derived_id = f"{_digest(_canonical(seed))[:24]}"
    root = (
        _absolute_directory(state_root, label="campaign state root", create=True)
        if state_root is not None
        else _absolute_directory(
            _writable(RUNS_DIR / "capsules" / derived_id, label="campaign state root"),
            label="campaign state root",
            create=True,
        )
    )
    if subject.root == root or subject.root in root.parents:
        if state_root is not None:
            raise PreparedCampaignError(
                "campaign state root may not be inside the subject worktree: "
                f"{root} is inside {subject.root}. Pass --state-root outside the project, "
                "a sibling directory works."
            )
        raise PreparedCampaignError(
            "campaign state root may not be inside the subject worktree: "
            f"{root} is inside {subject.root}. No --state-root was given, so Bear Hug's "
            f"runs directory ({RUNS_DIR}) set the default and it is inside the project. "
            "Clone Bear Hug as a sibling of the project, or set BEARHUG_ARTIFACT_ROOT to "
            "a directory outside it."
        )
    selected_campaign = _token(campaign_id or f"campaign.{derived_id}", label="campaign_id")
    selected_run = _token(run_id or f"run.{derived_id}", label="run_id")
    paths = _prepare_paths(
        root=root,
        campaign_id=selected_campaign,
        run_id=selected_run,
        worktree_parent=worktree_parent,
        repository_common_dir_sha256=subject.repository_common_dir_sha256,
        lease_root=lease_root,
    )
    source_contents_by_id = _source_contents(
        root=root, subject=subject, intent=intent, config=config
    )
    source_records = [
        {
            "source_id": item.source_id,
            "path": item.path,
            "content_sha256": item.content_sha256,
            "blob_path": str(item.blob_path),
        }
        for item in source_contents_by_id.values()
    ]
    source_records.sort(key=lambda item: item["source_id"])
    record: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "prepared_campaign",
        "canonical_algorithm": "bearhug-prepared-campaign-canonical-json-sha256/1",
        "created_at": datetime.now(tz=UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "locator": str(locator),
        "campaign_id": selected_campaign,
        "run_id": selected_run,
        "subject": {
            "path": str(subject.root),
            "common_dir": str(subject.common_dir),
            "repository_common_dir_sha256": subject.repository_common_dir_sha256,
            "head_oid": subject.head_oid,
            "tree_oid": subject.tree_oid,
            "lease_subject_root": str(lease_subject_root),
            "base_oid": plan["subject"]["base_oid"],
            "base_tree_sha256": plan["subject"]["base_tree_sha256"],
        },
        "paths": paths,
        "inputs": {
            "intent_path": str(intent_path),
            "plan_path": str(plan_path),
            "policy_path": str(policy_path),
            "execution_config_path": str(config_path),
            "qualification_index_path": str(qualification_path),
        },
        "digests": {
            "intent_sha256": intent_result.digest,
            "plan_sha256": plan_result.digest,
            "intent_source_sha256": _digest(intent_raw),
            "plan_source_sha256": _digest(plan_raw),
            "policy_sha256": policy_sha,
            "policy_source_sha256": _digest(policy_raw),
            "qualification_index_sha256": qualification.sha256,
            "execution_config_sha256": _digest(config_raw),
        },
        "objects": {
            "capsule_store": str(root / "capsule-objects"),
            "intent": intent_result.digest,
            "plan": plan_result.digest,
        },
        "provider_policy": policy_value,
        "config": config,
        "sources": source_records,
    }
    # Input paths are supplied by the caller, and are patched by prepare_campaign after this
    # helper has assembled the common identity.  Keeping the field in one record makes load-time
    # digest checks explicit and inspectable.
    record["content_sha256"] = _record_digest(record)
    source_contents = [
        {
            "source_id": item.source_id,
            "path": item.path,
            "content_sha256": item.content_sha256,
            "source_sha256": item.content_sha256,
            "blob_path": str(item.blob_path),
            "content": item.content,
            "kind": "project_authority",
            "tier": "p0",
            "reason": "sealed project authority required by the native intent envelope",
        }
        for item in source_contents_by_id.values()
    ]
    source_contents.sort(key=lambda item: item["source_id"])
    return PreparedCampaign(record, intent, plan, policy, qualification, source_contents, root)


def prepare_campaign(
    subject: Path | str,
    intent_path: Path | str,
    plan_path: Path | str,
    policy_path: Path | str,
    execution_path: Path | str,
    state_root: Path | str | None = None,
    worktree_parent: Path | str | None = None,
    campaign_id: str | None = None,
    run_id: str | None = None,
    lease_root: Path | str | None = None,
) -> PreparedCampaign:
    """Validate and durably seal one explicit native campaign without spending."""

    try:
        subject_identity = inspect_subject(subject)
    except CampaignImportError as exc:
        raise PreparedCampaignError(str(exc)) from exc
    lease_subject_root = _lease_subject_root(subject_identity)
    intent_file = _absolute_file(intent_path, label="intent")
    plan_file = _absolute_file(plan_path, label="capsule plan")
    policy_file = _absolute_file(policy_path, label="provider policy")
    config_file = _absolute_file(execution_path, label="execution config")
    intent, intent_raw = _exact_json(intent_file, label="intent")
    plan, plan_raw = _exact_json(plan_file, label="capsule plan")
    policy_value, policy_raw = _exact_json(policy_file, label="provider policy")
    config_value, config_raw = _exact_json(config_file, label="execution config")
    try:
        intent_result = validate_intent_envelope(intent)
        plan_result = validate_capsule_plan(plan, intent_envelope=intent)
    except CapsuleContractError as exc:
        raise PreparedCampaignError(str(exc)) from exc
    _ensure_native_approved(intent, plan)
    _verify_base(subject_identity, plan)
    try:
        policy = validate_provider_policy(policy_value)
    except ProviderPolicyError as exc:
        raise PreparedCampaignError(str(exc)) from exc
    normalized_config, qualification_path = _validate_execution_config(
        config_value,
        config_path=config_file,
        subject=subject_identity,
        intent=intent,
        plan=plan,
    )
    try:
        qualification = load_provider_qualification_index(qualification_path)
    except ProviderQualificationIndexError as exc:
        raise PreparedCampaignError(str(exc)) from exc
    if _json_digest(policy_value) not in intent["campaign_envelope"]["policy_refs"]:
        raise PreparedCampaignError(
            "provider policy digest is not authorized by the intent envelope"
        )
    if normalized_config["review_role"] not in policy.roles:
        raise PreparedCampaignError(
            "execution config review_role "
            f"{normalized_config['review_role']!r} is absent from provider policy"
        )
    # The selected author role(s) are still determined by the sealed plan; require that every
    # role named by a capsule has a policy entry before making the seal.
    for capsule in plan["capsules"]:
        needs = capsule["provider_capability_needs"]
        if needs and "author" not in policy.roles:
            raise PreparedCampaignError(
                "plan requires provider capabilities but policy has no author role"
            )
    root_hint = Path(state_root).expanduser().absolute() if state_root is not None else None
    if root_hint is not None and ".." in root_hint.parts:
        raise PreparedCampaignError("campaign state root may not contain parent traversal")
    if root_hint is not None:
        root_hint = _writable(root_hint, label="campaign state root")
        candidate = root_hint.resolve(strict=False)
        if subject_identity.root == candidate or subject_identity.root in candidate.parents:
            raise PreparedCampaignError(
                "campaign state root may not be inside the subject worktree: "
                f"{candidate} is inside {subject_identity.root}. Pass --state-root outside "
                "the project, a sibling directory works."
            )
    worktree_hint = (
        Path(worktree_parent).expanduser().absolute() if worktree_parent is not None else None
    )
    if worktree_hint is not None:
        _writable(worktree_hint, label="worktree parent")
        candidate = worktree_hint.resolve(strict=False)
        if subject_identity.root == candidate or subject_identity.root in candidate.parents:
            raise PreparedCampaignError(
                "worktree parent may not be inside the subject worktree: "
                f"{candidate} is inside {subject_identity.root}. Pass --worktree-parent "
                "outside the project, a sibling directory works."
            )
    locator_root = root_hint
    if locator_root is None:
        # Build the same deterministic id used by _build_prepared so the prepared locator can be
        # included in the immutable record before it is written.
        seed = {
            "subject": {
                "path": str(subject_identity.root),
                "common_dir": str(subject_identity.common_dir),
                "repository_common_dir_sha256": subject_identity.repository_common_dir_sha256,
                "head_oid": subject_identity.head_oid,
                "tree_oid": subject_identity.tree_oid,
                "lease_subject_root": str(lease_subject_root),
            },
            "intent_sha256": intent_result.digest,
            "plan_sha256": plan_result.digest,
            "intent_source_sha256": _digest(intent_raw),
            "plan_source_sha256": _digest(plan_raw),
            "policy_sha256": _json_digest(policy_value),
            "policy_source_sha256": _digest(policy_raw),
            "qualification_index_sha256": qualification.sha256,
            "execution_config_sha256": _digest(config_raw),
            "campaign_id": campaign_id,
            "run_id": run_id,
            "lease_subject_root": str(lease_subject_root),
        }
        locator_root = _writable(
            RUNS_DIR / "capsules" / _digest(_canonical(seed))[:24],
            label="campaign state root",
        )
        candidate = Path(locator_root).resolve(strict=False)
        if subject_identity.root == candidate or subject_identity.root in candidate.parents:
            raise PreparedCampaignError(
                "campaign state root may not be inside the subject worktree: "
                f"{candidate} is inside {subject_identity.root}. No --state-root was "
                f"given, so Bear Hug's runs directory ({RUNS_DIR}) set the default and it "
                "is inside the project. Pass --state-root outside the project, clone Bear "
                "Hug as a sibling of it, or set BEARHUG_ARTIFACT_ROOT to a directory "
                "outside it."
            )
    root = _absolute_directory(locator_root, label="campaign state root", create=True)
    locator = root / "prepared.json"
    lease_hint = Path(lease_root).expanduser().absolute() if lease_root is not None else None
    if lease_hint is not None and ".." in lease_hint.parts:
        raise PreparedCampaignError("lease root may not contain parent traversal")
    prepared = _build_prepared(
        locator=locator,
        subject=subject_identity,
        lease_subject_root=lease_subject_root,
        intent=intent,
        plan=plan,
        intent_raw=intent_raw,
        plan_raw=plan_raw,
        policy=policy,
        policy_value=policy_value,
        policy_raw=policy_raw,
        policy_path=policy_file,
        qualification=qualification,
        qualification_path=qualification_path,
        config=normalized_config,
        config_raw=config_raw,
        config_path=config_file,
        intent_path=intent_file,
        plan_path=plan_file,
        state_root=root,
        worktree_parent=worktree_hint,
        campaign_id=campaign_id,
        run_id=run_id,
        lease_root=lease_hint,
    )
    # Attach the actual authority locators before computing the immutable record digest.
    prepared.record["inputs"] = {
        "intent_path": str(intent_file),
        "plan_path": str(plan_file),
        "policy_path": str(policy_file),
        "execution_config_path": str(config_file),
        "qualification_index_path": str(qualification_path),
    }
    prepared.record["content_sha256"] = _record_digest(prepared.record)

    store = CapsuleObjectStore(root / "capsule-objects", create=True)
    try:
        stored_intent = store.put(intent)
        stored_plan = store.put(plan)
    except CapsuleContractError as exc:
        raise PreparedCampaignError(str(exc)) from exc
    if (
        stored_intent != prepared.record["digests"]["intent_sha256"]
        or stored_plan != prepared.record["digests"]["plan_sha256"]
    ):
        raise PreparedCampaignError("stored capsule object digest changed during preparation")
    raw = _canonical(prepared.record)
    _write_create_only(locator, raw)
    return prepared


def _validate_record(value: Mapping[str, Any], locator: Path) -> None:
    required = {
        "schema_version",
        "record_kind",
        "canonical_algorithm",
        "created_at",
        "locator",
        "campaign_id",
        "run_id",
        "subject",
        "paths",
        "inputs",
        "digests",
        "objects",
        "provider_policy",
        "config",
        "sources",
        "content_sha256",
    }
    if set(value) != required:
        raise PreparedCampaignError("prepared record is not closed")
    if value["schema_version"] != "1" or value["record_kind"] != "prepared_campaign":
        raise PreparedCampaignError("unsupported prepared campaign record")
    if value["canonical_algorithm"] != "bearhug-prepared-campaign-canonical-json-sha256/1":
        raise PreparedCampaignError("unsupported prepared campaign canonical algorithm")
    if value["locator"] != str(locator):
        raise PreparedCampaignError("prepared locator does not match the requested physical file")
    _token(value["campaign_id"], label="campaign_id")
    _token(value["run_id"], label="run_id")
    _sha(value["content_sha256"], label="prepared content_sha256")
    if value["content_sha256"] != _record_digest(value):
        raise PreparedCampaignError("prepared record content digest is invalid")
    subject = value["subject"]
    if not isinstance(subject, Mapping) or set(subject) != {
        "path",
        "common_dir",
        "repository_common_dir_sha256",
        "head_oid",
        "tree_oid",
        "lease_subject_root",
        "base_oid",
        "base_tree_sha256",
    }:
        raise PreparedCampaignError("prepared subject identity is not closed")
    for label in ("path", "common_dir", "lease_subject_root"):
        path = _absolute_directory(subject[label], label=f"subject {label}", private=False)
        if str(path) != subject[label]:
            raise PreparedCampaignError(f"subject {label} is not an exact path")
    _sha(subject["repository_common_dir_sha256"], label="subject common directory digest")
    _oid(subject["head_oid"], label="subject head_oid")
    _oid(subject["tree_oid"], label="subject tree_oid")
    _oid(subject["base_oid"], label="subject base_oid")
    _sha(subject["base_tree_sha256"], label="subject base tree digest")
    paths = value["paths"]
    if not isinstance(paths, Mapping) or set(paths) != {
        "state_root",
        "campaign_root",
        "run_root",
        "worktree_parent",
        "lease_root",
        "provider_output_root",
        "integration_root",
    }:
        raise PreparedCampaignError("prepared derived paths are not closed")
    for label, path_value in paths.items():
        path = _absolute_directory(path_value, label=f"derived {label}", create=False)
        if str(path) != path_value:
            raise PreparedCampaignError(f"derived {label} is not an exact path")
    if paths["state_root"] != str(locator.parent):
        raise PreparedCampaignError("prepared state_root does not contain prepared.json")
    expected_campaign_root = locator.parent / "campaigns" / value["campaign_id"]
    expected_run_root = expected_campaign_root / "runs" / value["run_id"]
    expected_paths = {
        "campaign_root": expected_campaign_root,
        "run_root": expected_run_root,
        "provider_output_root": expected_run_root / "provider-output",
        "integration_root": expected_run_root / "integration",
    }
    for name, expected in expected_paths.items():
        if paths[name] != str(expected):
            raise PreparedCampaignError(f"prepared derived {name} does not match its identity")
    # lease_root is checked against the repository it is sealed for, not against the reading
    # process's RUNS_DIR.  Re-deriving it from ambient state asserted that the reader shares
    # the writer's runtime root, which is not an integrity property -- the record's own digest
    # is -- and it made a valid record unreadable to every process rooted elsewhere: setup, the
    # dashboard and the status probe each hit it in turn.
    lease_root = Path(paths["lease_root"])
    if lease_root.name != subject["repository_common_dir_sha256"] or (
        lease_root.parent.name != "capsule-leases"
    ):
        raise PreparedCampaignError("prepared derived lease_root does not match its identity")
    subject_root = Path(subject["path"])
    state_root = locator.parent
    if subject_root == state_root or subject_root in state_root.parents:
        raise PreparedCampaignError(
            "prepared state root may not be inside the subject worktree: "
            f"{state_root} is inside {subject_root}. Re-prepare the campaign with "
            "--state-root outside the project, a sibling directory works."
        )
    worktree_root = Path(paths["worktree_parent"])
    if subject_root == worktree_root or subject_root in worktree_root.parents:
        raise PreparedCampaignError(
            "prepared worktree parent may not be inside the subject: "
            f"{worktree_root} is inside {subject_root}. Re-prepare the campaign with "
            "--worktree-parent outside the project, a sibling directory works."
        )
    digests = value["digests"]
    if not isinstance(digests, Mapping) or set(digests) != {
        "intent_sha256",
        "plan_sha256",
        "intent_source_sha256",
        "plan_source_sha256",
        "policy_sha256",
        "policy_source_sha256",
        "qualification_index_sha256",
        "execution_config_sha256",
    }:
        raise PreparedCampaignError("prepared input digests are not closed")
    for label, digest in digests.items():
        _sha(digest, label=f"digest {label}")
    objects = value["objects"]
    if not isinstance(objects, Mapping) or set(objects) != {"capsule_store", "intent", "plan"}:
        raise PreparedCampaignError("prepared capsule objects are not closed")
    capsule_store = _absolute_directory(objects["capsule_store"], label="capsule object store")
    if str(capsule_store) != str(locator.parent / "capsule-objects"):
        raise PreparedCampaignError("prepared capsule object store is outside the state root")
    _sha(objects["intent"], label="stored intent digest")
    _sha(objects["plan"], label="stored plan digest")
    if objects["intent"] != value["digests"]["intent_sha256"] or objects["plan"] != value[
        "digests"
    ]["plan_sha256"]:
        raise PreparedCampaignError("prepared capsule object references do not match input digests")
    inputs = value["inputs"]
    if not isinstance(inputs, Mapping) or set(inputs) != {
        "intent_path",
        "plan_path",
        "policy_path",
        "execution_config_path",
        "qualification_index_path",
    }:
        raise PreparedCampaignError("prepared input locators are not closed")
    for label, input_path in inputs.items():
        exact = _absolute_file(input_path, label=f"input {label}")
        if str(exact) != input_path:
            raise PreparedCampaignError(f"input {label} is not an exact path")
    sources = value["sources"]
    if not isinstance(sources, list) or not sources:
        raise PreparedCampaignError("prepared authority sources must be a non-empty array")
    seen_sources: set[str] = set()
    for index, row in enumerate(sources):
        if not isinstance(row, Mapping) or set(row) != {
            "source_id",
            "path",
            "content_sha256",
            "blob_path",
        }:
            raise PreparedCampaignError(f"prepared source binding {index} is not closed")
        source_id = _token(row["source_id"], label=f"prepared source {index} id")
        if source_id in seen_sources:
            raise PreparedCampaignError(f"prepared sources repeat source_id {source_id!r}")
        seen_sources.add(source_id)
        if row["path"] is not None:
            _repository_relative(row["path"], label=f"prepared source {source_id} path")
        elif not source_id.startswith("request."):
            raise PreparedCampaignError("only sealed user requests may omit a project path")
        source_digest = _sha(
            row["content_sha256"], label=f"prepared source {source_id} digest"
        )
        blob = _absolute_file(row["blob_path"], label=f"prepared source {source_id} blob")
        expected_blob = locator.parent / "source-blobs" / f"{source_digest}.blob"
        if blob != expected_blob:
            raise PreparedCampaignError(
                f"prepared source {source_id!r} blob is outside content-addressed custody"
            )


def load_prepared(locator: Path | str, *, recovery: bool = False) -> PreparedCampaign:
    """Revalidate exact custody; recovery uses sealed source bytes after a live plan changes."""

    path = _absolute_file(locator, label="prepared campaign locator")
    raw = _read_regular(path, maximum=_MAX_JSON_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedCampaignError(
            f"prepared locator is not one UTF-8 JSON object: {path}"
        ) from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise PreparedCampaignError("prepared locator is not canonical JSON")
    _validate_record(value, path)
    root = path.parent
    inputs = value["inputs"]
    intent, intent_raw = _exact_json(Path(inputs["intent_path"]), label="sealed intent")
    plan, plan_raw = _exact_json(Path(inputs["plan_path"]), label="sealed capsule plan")
    policy_value, policy_raw = _exact_json(
        Path(inputs["policy_path"]), label="sealed provider policy"
    )
    config_value, config_raw = _exact_json(
        Path(inputs["execution_config_path"]), label="sealed execution config"
    )
    digests = value["digests"]
    if (
        _digest(intent_raw) != digests["intent_source_sha256"]
        or _digest(plan_raw) != digests["plan_source_sha256"]
    ):
        raise PreparedCampaignError("sealed intent or plan authority input digest changed")
    if _json_digest(policy_value) != digests["policy_sha256"]:
        raise PreparedCampaignError("sealed provider policy digest changed")
    if _digest(policy_raw) != digests["policy_source_sha256"]:
        raise PreparedCampaignError("sealed provider policy source bytes changed")
    if _digest(config_raw) != digests["execution_config_sha256"]:
        raise PreparedCampaignError("sealed execution config digest changed")
    try:
        intent_result = validate_intent_envelope(intent)
        plan_result = validate_capsule_plan(plan, intent_envelope=intent)
        policy = validate_provider_policy(policy_value)
    except (CapsuleContractError, ProviderPolicyError) as exc:
        raise PreparedCampaignError(str(exc)) from exc
    if (
        intent_result.digest != digests["intent_sha256"]
        or plan_result.digest != digests["plan_sha256"]
    ):
        raise PreparedCampaignError("sealed intent or plan object digest changed")
    _ensure_native_approved(intent, plan)
    subject_path = Path(value["subject"]["path"])
    if (
        subject_path.is_symlink()
        or not subject_path.is_dir()
        or str(subject_path.resolve()) != str(subject_path)
    ):
        raise PreparedCampaignError("sealed subject locator is no longer physical")
    common = _git(subject_path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common != value["subject"]["common_dir"]:
        raise PreparedCampaignError("sealed subject common directory changed")
    common_path = _absolute_directory(common, label="sealed Git common directory", private=False)
    if _digest(os.fsencode(common_path)) != value["subject"]["repository_common_dir_sha256"]:
        raise PreparedCampaignError("sealed subject common directory identity changed")
    if _lease_subject_root(
        SubjectIdentity(
            subject_path,
            _digest(os.fsencode(subject_path)),
            common_path,
            value["subject"]["repository_common_dir_sha256"],
            value["subject"]["head_oid"],
            value["subject"]["tree_oid"],
            value["subject"]["base_tree_sha256"],
            "sealed",
        )
    ) != Path(value["subject"]["lease_subject_root"]):
        raise PreparedCampaignError("sealed main worktree lease anchor changed")
    normalized_config, qualification_path = _validate_execution_config(
        config_value,
        config_path=Path(inputs["execution_config_path"]),
        subject=SubjectIdentity(
            subject_path,
            _digest(os.fsencode(subject_path)),
            Path(value["subject"]["common_dir"]),
            value["subject"]["repository_common_dir_sha256"],
            value["subject"]["head_oid"],
            value["subject"]["tree_oid"],
            value["subject"]["base_tree_sha256"],
            "sealed",
        ),
        intent=intent,
        plan=plan,
    )
    if normalized_config != value["config"]:
        raise PreparedCampaignError("sealed execution config changed")
    if str(qualification_path) != inputs["qualification_index_path"]:
        raise PreparedCampaignError("sealed qualification index locator changed")
    configured_sources = {
        row["source_id"]: row for row in normalized_config["authority_sources"]
    }
    authority_refs = {
        row["source_id"]: row for row in intent["authority_refs"]
    }
    record_sources = {row["source_id"]: row for row in value["sources"]}
    if set(record_sources) != set(configured_sources):
        raise PreparedCampaignError("sealed authority source bindings changed")
    for source_id, config_source in configured_sources.items():
        row = record_sources[source_id]
        if row["path"] != config_source.get("path"):
            raise PreparedCampaignError(f"sealed authority source {source_id!r} locator changed")
        if row["content_sha256"] != authority_refs[source_id]["content_sha256"]:
            raise PreparedCampaignError(f"sealed authority source {source_id!r} digest changed")
    qualification_raw = _read_regular(qualification_path, maximum=_MAX_JSON_BYTES)
    if _digest(qualification_raw) != digests["qualification_index_sha256"]:
        raise PreparedCampaignError("sealed qualification index digest changed")
    try:
        qualification = (
            load_bound_provider_qualification_index(qualification_path)
            if recovery
            else load_provider_qualification_index(qualification_path)
        )
    except ProviderQualificationIndexError as exc:
        raise PreparedCampaignError(str(exc)) from exc
    if qualification.sha256 != digests["qualification_index_sha256"]:
        raise PreparedCampaignError("sealed qualification index digest changed while loading")
    sources: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    for row in value["sources"]:
        if not isinstance(row, Mapping) or set(row) != {
            "source_id",
            "path",
            "content_sha256",
            "blob_path",
        }:
            raise PreparedCampaignError("prepared source binding is not closed")
        source_id = _token(row["source_id"], label="source_id")
        if source_id in source_ids:
            raise PreparedCampaignError(f"prepared sources repeat source_id {source_id!r}")
        source_ids.add(source_id)
        source_path = row["path"]
        expected = _sha(row["content_sha256"], label=f"source {source_id} digest")
        blob = _absolute_file(row["blob_path"], label=f"source {source_id} blob")
        sealed_content = _read_regular(blob, maximum=_MAX_SOURCE_BYTES)
        if _digest(sealed_content) != expected:
            raise PreparedCampaignError(f"authority source {source_id!r} blob changed")
        if recovery:
            # Stopping/recovering the old owner must work after the project plan changes.
            # Use its existing sealed bytes; ordinary execution still checks the live source.
            content = sealed_content
        elif source_path is None:
            content = configured_sources[source_id]["request"].encode("utf-8")
        else:
            source_path = _repository_relative(source_path, label=f"source {source_id} path")
            actual_path = _absolute_file(
                subject_path / source_path, label=f"authority source {source_id}"
            )
            content = _read_regular(actual_path, maximum=_MAX_SOURCE_BYTES)
        if _digest(content) != expected:
            raise PreparedCampaignError(f"authority source {source_id!r} digest changed")
        sources.append(
            {
                "source_id": source_id,
                "path": source_path,
                "content_sha256": expected,
                "source_sha256": expected,
                "blob_path": str(blob),
                "content": content,
                "kind": "project_authority",
                "tier": "p0",
                "reason": "sealed project authority required by the native intent envelope",
            }
        )
    sources.sort(key=lambda item: item["source_id"])
    store = CapsuleObjectStore(root / "capsule-objects", create=False)
    try:
        stored_intent = store.get(digests["intent_sha256"], record_kind="intent_envelope")
        stored_plan = store.get(digests["plan_sha256"], record_kind="capsule_plan")
    except CapsuleContractError as exc:
        raise PreparedCampaignError(str(exc)) from exc
    if (stored_intent != intent or stored_plan != plan) and (
        validate_intent_envelope(stored_intent).digest != intent_result.digest
        or validate_capsule_plan(stored_plan).digest != plan_result.digest
    ):
        # The source JSON may use an equivalent ordering, while object storage is canonical.  The
        # records must still identify the same canonical objects.
        raise PreparedCampaignError("stored capsule authority object changed")
    if value["provider_policy"] != policy_value:
        raise PreparedCampaignError("sealed provider policy projection changed")
    return PreparedCampaign(value, intent, plan, policy, qualification, sources, root)


__all__ = [
    "PreparedCampaign",
    "PreparedCampaignError",
    "load_prepared",
    "prepare_campaign",
]
