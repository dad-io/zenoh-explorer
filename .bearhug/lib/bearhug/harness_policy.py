"""Closed provider-neutral harness policy and deterministic semantic staging.

This is the 11.9 boundary: it validates semantics and emits a neutral, content-addressed staging
artifact.  It deliberately does not generate or claim equivalence for CLAUDE.md, AGENTS.md, hook
configuration, rules, agents, or skills in either provider's native syntax.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.paths import assert_writable


class HarnessPolicyError(ValueError):
    """A semantic policy is malformed, ambiguous, or unavailable for the selected provider."""


_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

SEMANTIC_CAPABILITIES = frozenset(
    {
        "semantic-instructions",
        "semantic-hooks",
        "semantic-skills",
        "semantic-agent-roles",
        "semantic-mcp",
        "semantic-safety-predicates",
    }
)
NATIVE_CAPABILITIES = frozenset(
    {
        "project-instructions",
        "project-hooks",
        "pre-tool-block",
        "post-tool-observation",
        "stop-continuation",
        "project-skills",
        "project-agents",
        "mcp-config",
        "provider-task-store",
        "command-escalation-rules",
        "os-sandbox",
        "hook-trust-attestation",
        "instruction-source-attestation",
    }
)
_NATIVE_CAPABILITIES_BY_PROVIDER: dict[str, frozenset[str]] = {
    # The neutral compiler emits no provider-native surface. Native capabilities are negotiated
    # by the separate provider projection compilers, which must prove each mapping rather than
    # treating the existence of a provider feature as semantic equivalence.
    "claude": frozenset(),
    "codex": frozenset(),
}
CAPABILITIES_BY_PROVIDER = {
    provider: SEMANTIC_CAPABILITIES | native
    for provider, native in _NATIVE_CAPABILITIES_BY_PROVIDER.items()
}
ALL_CAPABILITIES = SEMANTIC_CAPABILITIES | NATIVE_CAPABILITIES

_TOOL_CLASSES = frozenset(
    {
        "file-read",
        "file-write",
        "mcp",
        "network",
        "search",
        "shell",
        "subagent",
        "user-interaction",
    }
)
_EVENTS = frozenset(
    {
        "session-start",
        "user-prompt-submit",
        "pre-tool-use",
        "permission-request",
        "post-tool-use",
        "stop",
        "subagent-start",
        "subagent-stop",
        "pre-compact",
    }
)
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "policy_id",
        "policy_version",
        "provenance",
        "instructions",
        "hooks",
        "skills",
        "agent_roles",
        "mcp_servers",
        "safety_predicates",
        "required_capabilities",
    }
)
_COLLECTIONS = (
    "instructions",
    "hooks",
    "skills",
    "agent_roles",
    "mcp_servers",
    "safety_predicates",
    "required_capabilities",
)
_FIELDS = {
    "instructions": frozenset(
        {"id", "order", "content", "content_sha256", "semantic_sha256", "provenance"}
    ),
    "hooks": frozenset(
        {
            "id",
            "order",
            "event",
            "tool_classes",
            "handler",
            "timeout_ms",
            "failure_policy",
            "authority",
            "required",
            "semantic_sha256",
            "provenance",
        }
    ),
    "skills": frozenset(
        {"id", "description", "content_sha256", "required", "semantic_sha256", "provenance"}
    ),
    "agent_roles": frozenset(
        {
            "id",
            "purpose",
            "instructions",
            "required_capabilities",
            "required",
            "semantic_sha256",
            "provenance",
        }
    ),
    "mcp_servers": frozenset(
        {"id", "transport", "connection_ref", "required", "semantic_sha256", "provenance"}
    ),
    "safety_predicates": frozenset(
        {
            "id",
            "order",
            "tool_classes",
            "effect",
            "evaluator",
            "authority",
            "required",
            "semantic_sha256",
            "provenance",
        }
    ),
    "required_capabilities": frozenset({"id", "semantic_sha256", "provenance"}),
}
_PROVENANCE_FIELDS = frozenset({"source_id", "source_uri", "source_sha256"})
_HANDLER_FIELDS = frozenset({"id", "version", "content_sha256"})
_SEMANTIC_FILE = "semantic/harness-policy.v1.json"
_MANIFEST_FILE = "harness-build-manifest.v1.json"


@dataclass(frozen=True, slots=True)
class HarnessPolicy:
    """A validated, canonical semantic policy document."""

    document: dict[str, Any]
    sha256: str


def _canonical_bytes(value: Any) -> bytes:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def semantic_record_sha256(record: Mapping[str, Any]) -> str:
    """Hash one declaration's complete semantics, excluding its digest field itself."""

    value = dict(record)
    value.pop("semantic_sha256", None)
    return _digest(_canonical_bytes(value))


def _closed_object(value: Any, fields: frozenset[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise HarnessPolicyError(f"{context} has missing or unknown fields")
    return value


def _string(value: Any, context: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise HarnessPolicyError(f"{context} must be a non-empty string")
    if maximum is not None and len(value) > maximum:
        raise HarnessPolicyError(f"{context} exceeds {maximum} characters")
    for character in value:
        codepoint = ord(character)
        forbidden_control = (
            codepoint <= 0x08
            or codepoint in {0x0B, 0x0C}
            or 0x0E <= codepoint <= 0x1F
            or 0x7F <= codepoint <= 0x9F
        )
        if forbidden_control or 0xD800 <= codepoint <= 0xDFFF:
            raise HarnessPolicyError(f"{context} contains a forbidden code point")
    return value


def _identifier(value: Any, context: str) -> str:
    value = _string(value, context)
    if _ID.fullmatch(value) is None:
        raise HarnessPolicyError(f"{context} is not a semantic identifier: {value!r}")
    return value


def _version(value: Any, context: str) -> str:
    value = _string(value, context)
    if _VERSION.fullmatch(value) is None:
        raise HarnessPolicyError(f"{context} is not a semantic version: {value!r}")
    return value


def _sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise HarnessPolicyError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _integer(value: Any, context: str, *, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bound = f" through {maximum}" if maximum is not None else " or greater"
        raise HarnessPolicyError(f"{context} must be an integer {minimum}{bound}")
    return value


def _boolean(value: Any, context: str) -> bool:
    if type(value) is not bool:
        raise HarnessPolicyError(f"{context} must be a boolean")
    return value


def _choice(value: Any, allowed: frozenset[str], context: str) -> str:
    value = _string(value, context)
    if value not in allowed:
        raise HarnessPolicyError(f"{context} is unsupported: {value!r}")
    return value


def _sorted_unique_strings(value: Any, allowed: frozenset[str], context: str) -> list[str]:
    if not isinstance(value, list):
        raise HarnessPolicyError(f"{context} must be an array")
    checked = [_choice(item, allowed, f"{context}[{index}]") for index, item in enumerate(value)]
    if checked != sorted(set(checked)):
        raise HarnessPolicyError(f"{context} must be sorted and contain no duplicates")
    return checked


def _provenance(value: Any, context: str) -> None:
    raw = _closed_object(value, _PROVENANCE_FIELDS, context)
    _identifier(raw["source_id"], f"{context}.source_id")
    _string(raw["source_uri"], f"{context}.source_uri", maximum=2048)
    _sha256(raw["source_sha256"], f"{context}.source_sha256")


def _handler(value: Any, context: str) -> None:
    raw = _closed_object(value, _HANDLER_FIELDS, context)
    _identifier(raw["id"], f"{context}.id")
    _version(raw["version"], f"{context}.version")
    _sha256(raw["content_sha256"], f"{context}.content_sha256")


def _record_digest(raw: dict[str, Any], context: str) -> None:
    supplied = _sha256(raw["semantic_sha256"], f"{context}.semantic_sha256")
    expected = semantic_record_sha256(raw)
    if supplied != expected:
        raise HarnessPolicyError(
            f"{context}.semantic_sha256 does not match the declaration: expected {expected}"
        )


def _validate_record(collection: str, raw: dict[str, Any], index: int) -> None:
    context = f"{collection}[{index}]"
    _closed_object(raw, _FIELDS[collection], context)
    _identifier(raw["id"], f"{context}.id")
    _provenance(raw["provenance"], f"{context}.provenance")

    if collection == "instructions":
        _integer(raw["order"], f"{context}.order", minimum=0)
        content = _string(raw["content"], f"{context}.content", maximum=1048576)
        content_hash = _sha256(raw["content_sha256"], f"{context}.content_sha256")
        expected = _digest(content.encode())
        if content_hash != expected:
            raise HarnessPolicyError(
                f"{context}.content_sha256 does not match UTF-8 instruction content"
            )
    elif collection == "hooks":
        _integer(raw["order"], f"{context}.order", minimum=0)
        _choice(raw["event"], _EVENTS, f"{context}.event")
        _sorted_unique_strings(raw["tool_classes"], _TOOL_CLASSES, f"{context}.tool_classes")
        _handler(raw["handler"], f"{context}.handler")
        _integer(raw["timeout_ms"], f"{context}.timeout_ms", minimum=1, maximum=3600000)
        _choice(
            raw["failure_policy"],
            frozenset({"block", "continue", "observe-only"}),
            f"{context}.failure_policy",
        )
        _choice(
            raw["authority"],
            frozenset({"advisory", "evidence", "gate", "safety"}),
            f"{context}.authority",
        )
        _boolean(raw["required"], f"{context}.required")
    elif collection == "skills":
        _string(raw["description"], f"{context}.description", maximum=4096)
        _sha256(raw["content_sha256"], f"{context}.content_sha256")
        _boolean(raw["required"], f"{context}.required")
    elif collection == "agent_roles":
        _string(raw["purpose"], f"{context}.purpose", maximum=4096)
        _string(raw["instructions"], f"{context}.instructions", maximum=1048576)
        _sorted_unique_strings(
            raw["required_capabilities"], ALL_CAPABILITIES, f"{context}.required_capabilities"
        )
        _boolean(raw["required"], f"{context}.required")
    elif collection == "mcp_servers":
        _choice(
            raw["transport"], frozenset({"stdio", "streamable-http"}), f"{context}.transport"
        )
        _identifier(raw["connection_ref"], f"{context}.connection_ref")
        _boolean(raw["required"], f"{context}.required")
    elif collection == "safety_predicates":
        _integer(raw["order"], f"{context}.order", minimum=0)
        tool_classes = _sorted_unique_strings(
            raw["tool_classes"], _TOOL_CLASSES, f"{context}.tool_classes"
        )
        if not tool_classes:
            raise HarnessPolicyError(f"{context}.tool_classes must not be empty")
        _choice(raw["effect"], frozenset({"allow", "deny", "prompt"}), f"{context}.effect")
        _handler(raw["evaluator"], f"{context}.evaluator")
        _choice(
            raw["authority"],
            frozenset({"advisory", "gate", "safety"}),
            f"{context}.authority",
        )
        _boolean(raw["required"], f"{context}.required")
    elif collection == "required_capabilities":
        _choice(raw["id"], ALL_CAPABILITIES, f"{context}.id")
    else:  # pragma: no cover - collection names are closed above
        raise AssertionError(collection)
    _record_digest(raw, context)


def _canonical_document(value: dict[str, Any]) -> dict[str, Any]:
    document = copy.deepcopy(value)
    for collection in _COLLECTIONS:
        key = (lambda item: (item["order"], item["id"])) if collection in {
            "instructions",
            "hooks",
            "safety_predicates",
        } else (lambda item: item["id"])
        document[collection] = sorted(document[collection], key=key)
    return document


def validate_harness_policy(value: Any) -> HarnessPolicy:
    """Validate and canonicalize a closed semantic harness policy."""

    raw = _closed_object(value, _TOP_FIELDS, "harness policy")
    if raw["schema_version"] != "1":
        raise HarnessPolicyError("unsupported harness policy schema")
    _identifier(raw["policy_id"], "policy_id")
    _version(raw["policy_version"], "policy_version")
    _provenance(raw["provenance"], "provenance")

    for collection in _COLLECTIONS:
        records = raw[collection]
        if not isinstance(records, list):
            raise HarnessPolicyError(f"{collection} must be an array")
        if collection == "instructions" and not records:
            raise HarnessPolicyError("instructions must not be empty")
        identifiers: set[str] = set()
        orders: set[int | tuple[str, int]] = set()
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise HarnessPolicyError(f"{collection}[{index}] must be an object")
            _validate_record(collection, record, index)
            identifier = record["id"]
            if identifier in identifiers:
                raise HarnessPolicyError(f"{collection} contains duplicate id {identifier!r}")
            identifiers.add(identifier)
            if collection in {"instructions", "hooks", "safety_predicates"}:
                order = record["order"]
                precedence_key = (record["event"], order) if collection == "hooks" else order
                if precedence_key in orders:
                    raise HarnessPolicyError(
                        f"{collection} has ambiguous precedence at order {order}"
                    )
                orders.add(precedence_key)

    document = _canonical_document(raw)
    blob = _canonical_bytes(document)
    return HarnessPolicy(document=document, sha256=_digest(blob))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HarnessPolicyError(f"harness policy JSON contains duplicate key {key!r}")
        value[key] = item
    return value


def load_harness_policy(path: Path | str) -> HarnessPolicy:
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, ValueError) as exc:
        raise HarnessPolicyError(f"cannot read harness policy {path}: {exc}") from exc
    return validate_harness_policy(value)


def _effective_requirements(policy: HarnessPolicy) -> list[str]:
    required = {record["id"] for record in policy.document["required_capabilities"]}
    for role in policy.document["agent_roles"]:
        if role["required"]:
            required.update(role["required_capabilities"])
    return sorted(required)


def _compiled_semantics(policy: HarnessPolicy) -> list[str]:
    mapping = {
        "instructions": "semantic-instructions",
        "hooks": "semantic-hooks",
        "skills": "semantic-skills",
        "agent_roles": "semantic-agent-roles",
        "mcp_servers": "semantic-mcp",
        "safety_predicates": "semantic-safety-predicates",
    }
    return sorted(capability for field, capability in mapping.items() if policy.document[field])


def _manifest(policy: HarnessPolicy, provider: str, semantic_blob: bytes) -> dict[str, Any]:
    required = _effective_requirements(policy)
    return {
        "schema_version": "1",
        "record_kind": "harness_build_manifest",
        "provider": provider,
        "compiler": {"name": "bearhug-semantic-harness", "version": "1"},
        "projection_kind": "neutral_semantic",
        "native_surface_emitted": False,
        "source_policy": {
            "id": policy.document["policy_id"],
            "version": policy.document["policy_version"],
            "sha256": policy.sha256,
            "provenance": policy.document["provenance"],
        },
        "files": [
            {
                "path": _SEMANTIC_FILE,
                "kind": "neutral-semantic-policy",
                "mode": "0644",
                "sha256": _digest(semantic_blob),
            }
        ],
        "declared_required_capabilities": required,
        "compiled_semantic_capabilities": _compiled_semantics(policy),
        "unavailable_required_capabilities": [],
        "limitations": [
            "declaration-time provider compatibility is not provider-runtime attestation",
            "neutral semantic projection only; no provider-native surface was emitted",
            "no installation, effective-configuration, hook-trust, or hook-execution claim",
        ],
    }


def compile_harness_policy(
    value: HarnessPolicy | Mapping[str, Any],
    *,
    provider: str,
    staging_dir: Path | str,
) -> dict[str, Any]:
    """Compile into a new staging directory and return the deterministic manifest.

    The destination must not already exist.  Validation and capability negotiation happen before
    any filesystem write.  This function has no target-repository or install operation.
    """

    if provider not in CAPABILITIES_BY_PROVIDER:
        raise HarnessPolicyError(f"unsupported harness provider: {provider!r}")
    if isinstance(value, HarnessPolicy):
        policy = validate_harness_policy(value.document)
        if policy.sha256 != value.sha256:
            raise HarnessPolicyError(
                "validated harness policy digest no longer matches its document"
            )
    else:
        policy = validate_harness_policy(dict(value))
    required = _effective_requirements(policy)
    unavailable = sorted(set(required) - CAPABILITIES_BY_PROVIDER[provider])
    if unavailable:
        raise HarnessPolicyError(
            f"provider {provider!r} cannot satisfy required harness capabilities: {unavailable}"
        )
    compiled = set(_compiled_semantics(policy))
    missing_semantics = sorted((set(required) & SEMANTIC_CAPABILITIES) - compiled)
    if missing_semantics:
        raise HarnessPolicyError(
            f"policy requires semantic capabilities with no declarations: {missing_semantics}"
        )

    destination = assert_writable(Path(staging_dir))
    if destination.exists() or destination.is_symlink():
        raise HarnessPolicyError(f"staging destination already exists: {destination}")
    parent = destination.parent
    if not parent.is_dir() or parent.is_symlink():
        raise HarnessPolicyError(f"staging parent must be an existing real directory: {parent}")

    semantic_blob = _canonical_bytes(policy.document)
    manifest = _manifest(policy, provider, semantic_blob)
    manifest_blob = json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True
    ).encode() + b"\n"

    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=parent))
    try:
        semantic_path = temporary / _SEMANTIC_FILE
        semantic_path.parent.mkdir(mode=0o755)
        semantic_path.write_bytes(semantic_blob)
        os.chmod(semantic_path, 0o644)
        manifest_path = temporary / _MANIFEST_FILE
        manifest_path.write_bytes(manifest_blob)
        os.chmod(manifest_path, 0o644)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return copy.deepcopy(manifest)


__all__ = [
    "ALL_CAPABILITIES",
    "CAPABILITIES_BY_PROVIDER",
    "HarnessPolicy",
    "HarnessPolicyError",
    "NATIVE_CAPABILITIES",
    "SEMANTIC_CAPABILITIES",
    "compile_harness_policy",
    "load_harness_policy",
    "semantic_record_sha256",
    "validate_harness_policy",
]
