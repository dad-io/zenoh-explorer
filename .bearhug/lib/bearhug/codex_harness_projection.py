"""Deterministic, staging-only projection of harness policy into Codex-native files.

The projection is deliberately smaller than Codex's feature surface.  A provider feature is not
semantic equivalence: this compiler emits only ordered project instructions because
``harness-policy.v1`` contains their complete bytes.  It also emits custom-agent TOML because the
policy materializes every required native field.  Hooks, skills, MCP servers, safety predicates,
and experimental command rules stay explicit capability gaps until their provider-native inputs
can be derived without guessing.

This module never installs a projection, launches Codex, reads ambient personal configuration, or
claims that staged files were trusted or loaded by a runtime.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bearhug.harness_policy import (
    ALL_CAPABILITIES,
    HarnessPolicy,
    HarnessPolicyError,
    validate_harness_policy,
)
from bearhug.paths import assert_writable


class CodexHarnessProjectionError(HarnessPolicyError):
    """A Codex-native projection would be incomplete, unsafe, or ambiguous."""


_AGENTS_FILE = "AGENTS.md"
_MANIFEST_FILE = "codex-harness-projection-manifest.v1.json"

_OFFICIAL_DOCUMENTATION = (
    {
        "surface": "project-instructions",
        "url": "https://developers.openai.com/codex/guides/agents-md",
        "status": "lossless",
    },
    {
        "surface": "project-config",
        "url": "https://developers.openai.com/codex/config-reference",
        "status": "gap",
    },
    {
        "surface": "project-hooks",
        "url": "https://learn.chatgpt.com/docs/hooks",
        "status": "gap",
    },
    {
        "surface": "command-escalation-rules",
        "url": "https://learn.chatgpt.com/docs/agent-configuration/rules",
        "status": "gap",
    },
    {
        "surface": "project-agents",
        "url": "https://learn.chatgpt.com/docs/agent-configuration/subagents",
        "status": "lossless",
    },
    {
        "surface": "project-skills",
        "url": "https://developers.openai.com/codex/skills",
        "status": "gap",
    },
    {
        "surface": "mcp-config",
        "url": "https://developers.openai.com/codex/mcp",
        "status": "gap",
    },
)
_LIMITATIONS = [
    "staged bytes are not installed, trusted, loaded, or runtime-attested",
    "ambient user, admin, system, and plugin configuration is neither read nor trusted",
    "documentation URLs establish supported surfaces but do not version-lock a Codex runtime",
    "only complete ordered instructions and materialized custom-agent fields have lossless "
    "native mappings",
]
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_AGENT_PATH = re.compile(r"^\.codex/agents/[a-z][a-z0-9_-]{0,63}\.toml$")

_BASE_CAPABILITIES = frozenset({"semantic-instructions", "project-instructions"})
_AGENT_CAPABILITIES = frozenset({"semantic-agent-roles", "project-agents"})
_COLLECTION_GAPS = {
    "hooks": ("project-hooks", "unresolved-handler-artifact"),
    "skills": ("project-skills", "unresolved-content-artifact"),
    "mcp_servers": ("mcp-config", "unresolved-connection-reference"),
    "safety_predicates": (
        "semantic-safety-predicates",
        "safety-contract-not-lossless",
    ),
}
_REQUIREMENT_REASON = {
    "semantic-hooks": "unresolved-handler-artifact",
    "project-hooks": "unresolved-handler-artifact",
    "pre-tool-block": "hook-authority-not-attested",
    "post-tool-observation": "hook-authority-not-attested",
    "stop-continuation": "hook-authority-not-attested",
    "semantic-skills": "unresolved-content-artifact",
    "project-skills": "unresolved-content-artifact",
    "semantic-mcp": "unresolved-connection-reference",
    "mcp-config": "unresolved-connection-reference",
    "semantic-safety-predicates": "safety-contract-not-lossless",
    "command-escalation-rules": "experimental-surface",
    "provider-task-store": "no-project-local-mapping",
    "os-sandbox": "runtime-setting-not-projected",
    "hook-trust-attestation": "runtime-attestation-unavailable",
    "instruction-source-attestation": "runtime-attestation-unavailable",
}


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def codex_projection_manifest_sha256(value: Mapping[str, Any]) -> str:
    """Hash a manifest's canonical content, excluding its digest field."""

    manifest = copy.deepcopy(dict(value))
    manifest.pop("content_sha256", None)
    return _digest(_canonical_bytes(manifest))


def _validated(value: HarnessPolicy | Mapping[str, Any]) -> HarnessPolicy:
    if isinstance(value, HarnessPolicy):
        policy = validate_harness_policy(value.document)
        if policy.sha256 != value.sha256:
            raise CodexHarnessProjectionError(
                "validated harness policy digest no longer matches its document"
            )
        return policy
    return validate_harness_policy(dict(value))


def _instruction_projection(
    policy: HarnessPolicy,
) -> tuple[bytes, list[dict[str, Any]]]:
    output = bytearray()
    mappings: list[dict[str, Any]] = []
    for index, record in enumerate(policy.document["instructions"]):
        if index:
            output.extend(b"\n\n")
        content = record["content"].encode()
        start = len(output)
        output.extend(content)
        mappings.append(
            {
                "source_collection": "instructions",
                "source_id": record["id"],
                "semantic_sha256": record["semantic_sha256"],
                "content_sha256": record["content_sha256"],
                "target_path": _AGENTS_FILE,
                "target_byte_start": start,
                "target_byte_length": len(content),
                "mapping": "exact-ordered-utf8-segment",
            }
        )
    return bytes(output), mappings


def _toml_string(value: str) -> str:
    """Render one policy string as a TOML basic string without normalizing its content."""

    return json.dumps(value, ensure_ascii=False)


def _agent_projection(record: Mapping[str, Any]) -> bytes:
    return (
        f"name = {_toml_string(record['id'])}\n"
        f"description = {_toml_string(record['purpose'])}\n"
        f"developer_instructions = {_toml_string(record['instructions'])}\n"
    ).encode()


def _required_capabilities(policy: HarnessPolicy) -> list[str]:
    required = {record["id"] for record in policy.document["required_capabilities"]}
    for role in policy.document["agent_roles"]:
        if role["required"]:
            required.update(role["required_capabilities"])
    return sorted(required)


def _gap(
    *,
    capability: str,
    source_collection: str,
    source_id: str,
    semantic_sha256: str,
    required: bool,
    reason_code: str,
    missing_capabilities: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "capability": capability,
        "source_collection": source_collection,
        "source_id": source_id,
        "semantic_sha256": semantic_sha256,
        "required": required,
        "reason_code": reason_code,
        "missing_capabilities": missing_capabilities or [],
    }


def _analyze(
    policy: HarnessPolicy,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
]:
    agents_blob, instruction_mappings = _instruction_projection(policy)
    files: list[dict[str, Any]] = [
        {
            "path": _AGENTS_FILE,
            "kind": "codex-project-instructions",
            "mode": "0644",
            "byte_length": len(agents_blob),
            "sha256": _digest(agents_blob),
            "source_semantic_sha256": [
                record["semantic_sha256"] for record in policy.document["instructions"]
            ],
            "_content": agents_blob,
        }
    ]
    gaps: list[dict[str, Any]] = []
    provided = set(_BASE_CAPABILITIES)

    every_role_projected = True
    for record in policy.document["agent_roles"]:
        missing = sorted(set(record["required_capabilities"]) - _BASE_CAPABILITIES)
        if missing:
            every_role_projected = False
            gaps.append(
                _gap(
                    capability="project-agents",
                    source_collection="agent_roles",
                    source_id=record["id"],
                    semantic_sha256=record["semantic_sha256"],
                    required=record["required"],
                    reason_code="agent-capability-unavailable",
                    missing_capabilities=missing,
                )
            )
            continue
        blob = _agent_projection(record)
        files.append(
            {
                "path": f".codex/agents/{record['id']}.toml",
                "kind": "codex-project-agent",
                "mode": "0644",
                "byte_length": len(blob),
                "sha256": _digest(blob),
                "source_semantic_sha256": [record["semantic_sha256"]],
                "_content": blob,
            }
        )
    if policy.document["agent_roles"] and every_role_projected:
        provided.update(_AGENT_CAPABILITIES)

    for collection, (capability, reason) in _COLLECTION_GAPS.items():
        for record in policy.document[collection]:
            gaps.append(
                _gap(
                    capability=capability,
                    source_collection=collection,
                    source_id=record["id"],
                    semantic_sha256=record["semantic_sha256"],
                    required=record["required"],
                    reason_code=reason,
                )
            )

    for record in policy.document["required_capabilities"]:
        capability = record["id"]
        if capability not in provided:
            gaps.append(
                _gap(
                    capability=capability,
                    source_collection="required_capabilities",
                    source_id=capability,
                    semantic_sha256=record["semantic_sha256"],
                    required=True,
                    reason_code=_REQUIREMENT_REASON.get(
                        capability, "unsupported-provider-capability"
                    ),
                    missing_capabilities=[capability],
                )
            )

    gaps.sort(
        key=lambda item: (
            item["capability"],
            item["source_collection"],
            item["source_id"],
            item["semantic_sha256"],
        )
    )
    files.sort(key=lambda item: item["path"])
    return files, instruction_mappings, gaps, sorted(provided)


def _assert_safe_destination(staging_dir: Path | str) -> Path:
    untrusted = Path(staging_dir).expanduser()
    absolute = Path(os.path.abspath(untrusted))
    if absolute.exists() or absolute.is_symlink():
        raise CodexHarnessProjectionError(f"staging destination already exists: {absolute}")
    parent = absolute.parent
    if not parent.is_dir():
        raise CodexHarnessProjectionError(f"staging parent must be an existing directory: {parent}")
    for component in (parent, *parent.parents):
        if component.is_symlink():
            raise CodexHarnessProjectionError(
                f"staging path must not traverse a symlink: {component}"
            )
    return assert_writable(absolute)


def _manifest(
    policy: HarnessPolicy,
    *,
    files: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    provided: list[str],
) -> dict[str, Any]:
    required = _required_capabilities(policy)
    unavailable_required = sorted(
        {gap["capability"] for gap in gaps if gap["required"]} | (set(required) - set(provided))
    )
    public_files = [
        {key: value for key, value in file.items() if key != "_content"} for file in files
    ]
    manifest: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "codex_harness_projection_manifest",
        "provider": "openai-codex",
        "compiler": {"name": "bearhug-codex-harness", "version": "1"},
        "projection_kind": "codex-native-staging",
        "staging_only": True,
        "source_policy": {
            "id": policy.document["policy_id"],
            "version": policy.document["policy_version"],
            "sha256": policy.sha256,
        },
        "files": public_files,
        "instruction_mappings": mappings,
        "declared_required_capabilities": required,
        "staged_capabilities": provided,
        "capability_gaps": gaps,
        "unavailable_required_capabilities": unavailable_required,
        "documentation_evidence": [dict(item) for item in _OFFICIAL_DOCUMENTATION],
        "runtime_claims": {
            "installed": False,
            "effective_sources_verified": False,
            "hook_trust_verified": False,
            "runtime_behavior_observed": False,
        },
        "limitations": list(_LIMITATIONS),
    }
    manifest["content_sha256"] = codex_projection_manifest_sha256(manifest)
    return manifest


def validate_codex_projection_manifest(value: Any) -> dict[str, Any]:
    """Validate the closed invariants that JSON Schema cannot recompute."""

    expected_fields = {
        "schema_version",
        "record_kind",
        "provider",
        "compiler",
        "projection_kind",
        "staging_only",
        "source_policy",
        "files",
        "instruction_mappings",
        "declared_required_capabilities",
        "staged_capabilities",
        "capability_gaps",
        "unavailable_required_capabilities",
        "documentation_evidence",
        "runtime_claims",
        "limitations",
        "content_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise CodexHarnessProjectionError("Codex projection manifest is not a closed object")
    if value.get("schema_version") != "1" or value.get("provider") != "openai-codex":
        raise CodexHarnessProjectionError("unsupported Codex projection manifest identity")
    if value.get("record_kind") != "codex_harness_projection_manifest":
        raise CodexHarnessProjectionError("unsupported Codex projection manifest kind")
    if value.get("compiler") != {"name": "bearhug-codex-harness", "version": "1"}:
        raise CodexHarnessProjectionError("unsupported Codex projection compiler identity")
    if (
        value.get("projection_kind") != "codex-native-staging"
        or value.get("staging_only") is not True
    ):
        raise CodexHarnessProjectionError("Codex projection manifest is not staging-only")
    if value.get("unavailable_required_capabilities"):
        raise CodexHarnessProjectionError("manifest contains unavailable required capabilities")
    source = value.get("source_policy")
    if (
        not isinstance(source, dict)
        or set(source) != {"id", "version", "sha256"}
        or not isinstance(source["id"], str)
        or _ID.fullmatch(source["id"]) is None
        or not isinstance(source["version"], str)
        or _VERSION.fullmatch(source["version"]) is None
        or not isinstance(source["sha256"], str)
        or _SHA256.fullmatch(source["sha256"]) is None
    ):
        raise CodexHarnessProjectionError("source_policy is invalid")
    if value.get("documentation_evidence") != [dict(item) for item in _OFFICIAL_DOCUMENTATION]:
        raise CodexHarnessProjectionError("documentation_evidence is unsupported")
    if value.get("limitations") != _LIMITATIONS:
        raise CodexHarnessProjectionError("Codex projection limitations are unsupported")
    claims = value.get("runtime_claims")
    if (
        not isinstance(claims, dict)
        or set(claims)
        != {
            "installed",
            "effective_sources_verified",
            "hook_trust_verified",
            "runtime_behavior_observed",
        }
        or any(claims.values())
    ):
        raise CodexHarnessProjectionError("Codex projection manifest contains a runtime claim")
    expected = codex_projection_manifest_sha256(value)
    if value.get("content_sha256") != expected:
        raise CodexHarnessProjectionError(
            f"Codex projection manifest content digest mismatch: expected {expected}"
        )
    files = value.get("files")
    file_fields = {"path", "kind", "mode", "byte_length", "sha256", "source_semantic_sha256"}
    if not isinstance(files, list) or not files:
        raise CodexHarnessProjectionError("Codex projection files are invalid")
    paths: list[str] = []
    agents_source_digests: list[str] | None = None
    agents_byte_length: int | None = None
    for index, file in enumerate(files):
        if not isinstance(file, dict) or set(file) != file_fields:
            raise CodexHarnessProjectionError(f"files[{index}] is not closed")
        path = file["path"]
        expected_kind = (
            "codex-project-instructions"
            if path == _AGENTS_FILE
            else "codex-project-agent"
            if isinstance(path, str) and _AGENT_PATH.fullmatch(path) is not None
            else None
        )
        digests = file["source_semantic_sha256"]
        if (
            expected_kind is None
            or file["kind"] != expected_kind
            or file["mode"] != "0644"
            or type(file["byte_length"]) is not int
            or file["byte_length"] < 1
            or not isinstance(file["sha256"], str)
            or _SHA256.fullmatch(file["sha256"]) is None
            or not isinstance(digests, list)
            or not digests
            or len(digests) != len(set(digests))
            or any(not isinstance(item, str) or _SHA256.fullmatch(item) is None for item in digests)
        ):
            raise CodexHarnessProjectionError(f"files[{index}] has an invalid file contract")
        if path == _AGENTS_FILE:
            agents_source_digests = digests
            agents_byte_length = file["byte_length"]
        paths.append(path)
    if paths != sorted(set(paths)) or paths.count(_AGENTS_FILE) != 1:
        raise CodexHarnessProjectionError("Codex projection must contain exactly one AGENTS.md")
    capabilities = set(ALL_CAPABILITIES)
    declared = value.get("declared_required_capabilities")
    staged = value.get("staged_capabilities")
    if (
        not isinstance(declared, list)
        or declared != sorted(set(declared))
        or set(declared) - capabilities
        or not isinstance(staged, list)
        or staged != sorted(set(staged))
        or set(staged) - (_BASE_CAPABILITIES | _AGENT_CAPABILITIES)
        or not _BASE_CAPABILITIES.issubset(staged)
    ):
        raise CodexHarnessProjectionError("manifest capabilities are invalid")
    agent_files = [path for path in paths if path != _AGENTS_FILE]
    if bool(agent_files) != _AGENT_CAPABILITIES.issubset(staged):
        raise CodexHarnessProjectionError("agent files and staged capabilities disagree")
    mappings = value.get("instruction_mappings")
    mapping_fields = {
        "source_collection",
        "source_id",
        "semantic_sha256",
        "content_sha256",
        "target_path",
        "target_byte_start",
        "target_byte_length",
        "mapping",
    }
    if not isinstance(mappings, list) or not mappings:
        raise CodexHarnessProjectionError("instruction_mappings must not be empty")
    expected_start = 0
    mapping_semantics: list[str] = []
    for index, mapping in enumerate(mappings):
        if (
            not isinstance(mapping, dict)
            or set(mapping) != mapping_fields
            or mapping["source_collection"] != "instructions"
            or not isinstance(mapping["source_id"], str)
            or _ID.fullmatch(mapping["source_id"]) is None
            or not isinstance(mapping["semantic_sha256"], str)
            or _SHA256.fullmatch(mapping["semantic_sha256"]) is None
            or not isinstance(mapping["content_sha256"], str)
            or _SHA256.fullmatch(mapping["content_sha256"]) is None
            or mapping["target_path"] != _AGENTS_FILE
            or mapping["target_byte_start"] != expected_start
            or type(mapping["target_byte_length"]) is not int
            or mapping["target_byte_length"] < 1
            or mapping["mapping"] != "exact-ordered-utf8-segment"
        ):
            raise CodexHarnessProjectionError(f"instruction_mappings[{index}] is invalid")
        expected_start += mapping["target_byte_length"] + 2
        mapping_semantics.append(mapping["semantic_sha256"])
    if (
        agents_source_digests != mapping_semantics
        or agents_byte_length is None
        or expected_start - 2 != agents_byte_length
    ):
        raise CodexHarnessProjectionError("instruction mappings do not cover AGENTS.md")
    gap_fields = {
        "capability",
        "source_collection",
        "source_id",
        "semantic_sha256",
        "required",
        "reason_code",
        "missing_capabilities",
    }
    gaps = value.get("capability_gaps")
    if not isinstance(gaps, list):
        raise CodexHarnessProjectionError("capability_gaps must be an array")
    gap_keys: list[tuple[str, str, str, str]] = []
    for index, gap in enumerate(gaps):
        if not isinstance(gap, dict) or set(gap) != gap_fields:
            raise CodexHarnessProjectionError(f"capability_gaps[{index}] is not closed")
        missing = gap["missing_capabilities"]
        if (
            gap["capability"] not in capabilities
            or gap["source_collection"]
            not in {
                "hooks",
                "skills",
                "agent_roles",
                "mcp_servers",
                "safety_predicates",
                "required_capabilities",
            }
            or not isinstance(gap["source_id"], str)
            or _ID.fullmatch(gap["source_id"]) is None
            or not isinstance(gap["semantic_sha256"], str)
            or _SHA256.fullmatch(gap["semantic_sha256"]) is None
            or type(gap["required"]) is not bool
            or gap["reason_code"]
            not in set(_REQUIREMENT_REASON.values())
            | {
                "unresolved-handler-artifact",
                "unresolved-content-artifact",
                "agent-capability-unavailable",
                "unresolved-connection-reference",
                "safety-contract-not-lossless",
                "unsupported-provider-capability",
            }
            or not isinstance(missing, list)
            or missing != sorted(set(missing))
            or set(missing) - capabilities
            or gap["required"]
        ):
            raise CodexHarnessProjectionError(f"capability_gaps[{index}] is invalid")
        gap_keys.append(
            (gap["capability"], gap["source_collection"], gap["source_id"], gap["semantic_sha256"])
        )
    if gap_keys != sorted(set(gap_keys)):
        raise CodexHarnessProjectionError("capability_gaps must be sorted and unique")
    return copy.deepcopy(value)


def compile_codex_harness_projection(
    value: HarnessPolicy | Mapping[str, Any],
    *,
    staging_dir: Path | str,
) -> dict[str, Any]:
    """Stage a deterministic Codex-native projection without installing or launching it."""

    policy = _validated(value)
    files, mappings, gaps, provided = _analyze(policy)
    manifest = _manifest(
        policy,
        files=files,
        mappings=mappings,
        gaps=gaps,
        provided=provided,
    )
    unavailable = manifest["unavailable_required_capabilities"]
    if unavailable:
        raise CodexHarnessProjectionError(
            "Codex projection cannot satisfy required capabilities: " + ", ".join(unavailable)
        )

    destination = _assert_safe_destination(staging_dir)
    manifest_blob = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    )
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        for file in files:
            output = temporary / file["path"]
            output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            output.write_bytes(file["_content"])
            os.chmod(output, 0o644)
        manifest_path = temporary / _MANIFEST_FILE
        manifest_path.write_bytes(manifest_blob)
        os.chmod(manifest_path, 0o644)
        if destination.exists() or destination.is_symlink():
            raise CodexHarnessProjectionError(
                f"staging destination appeared during compilation: {destination}"
            )
        os.rename(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return copy.deepcopy(manifest)


__all__ = [
    "CodexHarnessProjectionError",
    "codex_projection_manifest_sha256",
    "compile_codex_harness_projection",
    "validate_codex_projection_manifest",
]
