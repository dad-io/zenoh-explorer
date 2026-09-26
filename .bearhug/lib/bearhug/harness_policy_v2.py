"""Closed provider-neutral harness policy v2 and explicit v1 migration.

V2 models semantic events and evaluator orchestration without embedding provider-native filenames,
configuration syntax, or lifecycle substitutions.  Compilation is pure: it validates, canonicalizes,
serializes, and hashes an in-memory document without reading ambient state or writing files.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.harness_policy import (
    ALL_CAPABILITIES,
    HarnessPolicy,
    validate_harness_policy,
)


class HarnessPolicyV2Error(ValueError):
    """A v2 semantic policy is malformed, ambiguous, or cannot be migrated losslessly."""


_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "policy_id",
        "policy_version",
        "provenance",
        "migration",
        "instructions",
        "event_policies",
        "skills",
        "agent_roles",
        "mcp_servers",
        "capabilities",
    }
)
_PROVENANCE_FIELDS = frozenset({"source_id", "source_uri", "source_sha256"})
_MIGRATION_FIELDS = frozenset({"source_schema_version", "source_policy_sha256"})
_INSTRUCTION_FIELDS = frozenset(
    {"id", "order", "content", "content_sha256", "semantic_sha256", "provenance"}
)
_EVENT_POLICY_FIELDS = frozenset(
    {
        "id",
        "event",
        "event_failure_policy",
        "execution",
        "evaluators",
        "semantic_sha256",
        "provenance",
    }
)
_EXECUTION_FIELDS = frozenset({"mode", "concurrency_approval"})
_APPROVAL_FIELDS = frozenset({"decision_id", "decision_sha256"})
_EVALUATOR_FIELDS = frozenset(
    {
        "id",
        "order",
        "matcher",
        "implementation",
        "timeout",
        "failure_policy",
        "authority",
        "requirement",
        "semantic_sha256",
        "provenance",
    }
)
_MATCHER_FIELDS = frozenset({"tool_families"})
_IMPLEMENTATION_FIELDS = frozenset({"id", "version", "content_sha256"})
_TIMEOUT_FIELDS = frozenset({"value", "unit"})
_SKILL_FIELDS = frozenset(
    {"id", "description", "content_sha256", "requirement", "semantic_sha256", "provenance"}
)
_AGENT_ROLE_FIELDS = frozenset(
    {
        "id",
        "purpose",
        "instructions",
        "required_capabilities",
        "requirement",
        "semantic_sha256",
        "provenance",
    }
)
_MCP_FIELDS = frozenset(
    {"id", "transport", "connection_ref", "requirement", "semantic_sha256", "provenance"}
)
_CAPABILITY_FIELDS = frozenset({"id", "requirement", "semantic_sha256", "provenance"})

SEMANTIC_EVENTS = frozenset(
    {
        "completion-request",
        "interrupt",
        "permission-request",
        "post-compact",
        "post-tool-use",
        "pre-compact",
        "pre-tool-use",
        "session-end",
        "session-start",
        "subagent-start",
        "subagent-stop",
        "user-prompt-submit",
    }
)
TOOL_SCOPED_EVENTS = frozenset(
    {"permission-request", "post-tool-use", "pre-tool-use"}
)
TOOL_FAMILIES = frozenset(
    {
        "any-tool",
        "filesystem-read",
        "filesystem-write",
        "mcp",
        "network",
        "search",
        "shell",
        "subagent",
        "user-interaction",
    }
)
_FAILURE_POLICIES = frozenset({"block", "continue", "observe-only"})
_AUTHORITIES = frozenset({"advisory", "evidence", "gate", "safety"})
_REQUIREMENTS = frozenset({"optional", "required"})
_EXECUTION_MODES = frozenset({"ordered", "approved-concurrent"})
_V1_EVENT_MAP = {
    "session-start": "session-start",
    "user-prompt-submit": "user-prompt-submit",
    "pre-tool-use": "pre-tool-use",
    "permission-request": "permission-request",
    "post-tool-use": "post-tool-use",
    "stop": "completion-request",
    "subagent-start": "subagent-start",
    "subagent-stop": "subagent-stop",
    "pre-compact": "pre-compact",
}
_V1_TOOL_MAP = {
    "file-read": "filesystem-read",
    "file-write": "filesystem-write",
    "mcp": "mcp",
    "network": "network",
    "search": "search",
    "shell": "shell",
    "subagent": "subagent",
    "user-interaction": "user-interaction",
}


@dataclass(frozen=True, slots=True)
class HarnessPolicyV2:
    """A canonical, content-addressed v2 semantic policy compiled in memory."""

    document: dict[str, Any]
    canonical_bytes: bytes
    sha256: str


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise HarnessPolicyV2Error(f"policy is not canonical JSON: {exc}") from exc
    return (rendered + "\n").encode()


def _digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def semantic_record_sha256_v2(record: Mapping[str, Any]) -> str:
    """Hash a v2 declaration after excluding its self-digest field."""

    value = copy.deepcopy(dict(record))
    value.pop("semantic_sha256", None)
    if set(value) == _EVENT_POLICY_FIELDS - {"semantic_sha256"}:
        evaluators = value.get("evaluators")
        execution = value.get("execution")
        if isinstance(evaluators, list) and isinstance(execution, dict):
            if execution.get("mode") == "ordered":
                value["evaluators"] = sorted(
                    evaluators,
                    key=lambda item: (
                        item.get("order") if isinstance(item, dict) else -1,
                        item.get("id", "") if isinstance(item, dict) else "",
                    ),
                )
            elif execution.get("mode") == "approved-concurrent":
                value["evaluators"] = sorted(
                    evaluators,
                    key=lambda item: item.get("id", "") if isinstance(item, dict) else "",
                )
    return _digest(_canonical_bytes(value))


def _closed(value: Any, fields: frozenset[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise HarnessPolicyV2Error(f"{context} has missing or unknown fields")
    return value


def _string(value: Any, context: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise HarnessPolicyV2Error(f"{context} must be a non-empty string")
    if maximum is not None and len(value) > maximum:
        raise HarnessPolicyV2Error(f"{context} exceeds {maximum} characters")
    for character in value:
        codepoint = ord(character)
        if (
            codepoint <= 0x08
            or codepoint in {0x0B, 0x0C}
            or 0x0E <= codepoint <= 0x1F
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
        ):
            raise HarnessPolicyV2Error(f"{context} contains a forbidden code point")
    return value


def _identifier(value: Any, context: str) -> str:
    value = _string(value, context)
    if _ID.fullmatch(value) is None:
        raise HarnessPolicyV2Error(f"{context} is not a semantic identifier: {value!r}")
    return value


def _version(value: Any, context: str) -> str:
    value = _string(value, context)
    if _VERSION.fullmatch(value) is None:
        raise HarnessPolicyV2Error(f"{context} is not a semantic version: {value!r}")
    return value


def _sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise HarnessPolicyV2Error(f"{context} must be lowercase SHA-256")
    return value


def _integer(
    value: Any,
    context: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if type(value) is not int or value < minimum or (
        maximum is not None and value > maximum
    ):
        raise HarnessPolicyV2Error(f"{context} is outside its integer bounds")
    return value


def _choice(value: Any, allowed: frozenset[str], context: str) -> str:
    value = _string(value, context)
    if value not in allowed:
        raise HarnessPolicyV2Error(f"{context} is unsupported: {value!r}")
    return value


def _array(value: Any, context: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        suffix = " and must not be empty" if nonempty else ""
        raise HarnessPolicyV2Error(f"{context} must be an array{suffix}")
    return value


def _provenance(value: Any, context: str) -> None:
    record = _closed(value, _PROVENANCE_FIELDS, context)
    _identifier(record["source_id"], f"{context}.source_id")
    _string(record["source_uri"], f"{context}.source_uri", maximum=2048)
    _sha256(record["source_sha256"], f"{context}.source_sha256")


def _semantic_digest(record: dict[str, Any], context: str) -> None:
    supplied = _sha256(record["semantic_sha256"], f"{context}.semantic_sha256")
    expected = semantic_record_sha256_v2(record)
    if supplied != expected:
        raise HarnessPolicyV2Error(
            f"{context}.semantic_sha256 does not match the declaration: expected {expected}"
        )


def _implementation(value: Any, context: str) -> None:
    record = _closed(value, _IMPLEMENTATION_FIELDS, context)
    _identifier(record["id"], f"{context}.id")
    _version(record["version"], f"{context}.version")
    _sha256(record["content_sha256"], f"{context}.content_sha256")


def _requirement(value: Any, context: str) -> str:
    return _choice(value, _REQUIREMENTS, context)


def _validate_instruction(record: Any, index: int) -> None:
    context = f"instructions[{index}]"
    record = _closed(record, _INSTRUCTION_FIELDS, context)
    _identifier(record["id"], f"{context}.id")
    _integer(record["order"], f"{context}.order", minimum=0)
    content = _string(record["content"], f"{context}.content", maximum=1048576)
    supplied = _sha256(record["content_sha256"], f"{context}.content_sha256")
    if supplied != _digest(content.encode()):
        raise HarnessPolicyV2Error(f"{context}.content_sha256 does not match content")
    _provenance(record["provenance"], f"{context}.provenance")
    _semantic_digest(record, context)


def _tool_families(value: Any, context: str, *, tool_scoped: bool) -> list[str]:
    values = _array(value, context, nonempty=tool_scoped)
    checked = [
        _choice(item, TOOL_FAMILIES, f"{context}[{index}]")
        for index, item in enumerate(values)
    ]
    if checked != sorted(set(checked)):
        raise HarnessPolicyV2Error(f"{context} must be sorted and contain no duplicates")
    if "any-tool" in checked and len(checked) != 1:
        raise HarnessPolicyV2Error(f"{context} may not combine any-tool with another family")
    if not tool_scoped and checked:
        raise HarnessPolicyV2Error(f"{context} must be empty for a non-tool event")
    return checked


def _validate_evaluator(record: Any, index: int, *, event: str, mode: str) -> None:
    context = f"event evaluator[{index}]"
    record = _closed(record, _EVALUATOR_FIELDS, context)
    _identifier(record["id"], f"{context}.id")
    order = record["order"]
    if mode == "ordered":
        _integer(order, f"{context}.order", minimum=0)
    elif order is not None:
        raise HarnessPolicyV2Error(
            "approved-concurrent execution must not assign evaluator order"
        )
    matcher = _closed(record["matcher"], _MATCHER_FIELDS, f"{context}.matcher")
    _tool_families(
        matcher["tool_families"],
        f"{context}.matcher.tool_families",
        tool_scoped=event in TOOL_SCOPED_EVENTS,
    )
    _implementation(record["implementation"], f"{context}.implementation")
    timeout = _closed(record["timeout"], _TIMEOUT_FIELDS, f"{context}.timeout")
    _integer(timeout["value"], f"{context}.timeout.value", minimum=1, maximum=3600000)
    if timeout["unit"] != "milliseconds":
        raise HarnessPolicyV2Error(f"{context}.timeout.unit must be 'milliseconds'")
    _choice(record["failure_policy"], _FAILURE_POLICIES, f"{context}.failure_policy")
    _choice(record["authority"], _AUTHORITIES, f"{context}.authority")
    _requirement(record["requirement"], f"{context}.requirement")
    _provenance(record["provenance"], f"{context}.provenance")
    _semantic_digest(record, context)


def _validate_event_policy(record: Any, index: int) -> None:
    context = f"event_policies[{index}]"
    record = _closed(record, _EVENT_POLICY_FIELDS, context)
    _identifier(record["id"], f"{context}.id")
    event = _choice(record["event"], SEMANTIC_EVENTS, f"{context}.event")
    _choice(
        record["event_failure_policy"],
        _FAILURE_POLICIES,
        f"{context}.event_failure_policy",
    )
    execution = _closed(record["execution"], _EXECUTION_FIELDS, f"{context}.execution")
    mode = _choice(execution["mode"], _EXECUTION_MODES, f"{context}.execution.mode")
    approval = execution["concurrency_approval"]
    if mode == "ordered":
        if approval is not None:
            raise HarnessPolicyV2Error("ordered execution must not carry concurrency approval")
    else:
        if approval is None:
            raise HarnessPolicyV2Error(
                "approved-concurrent execution requires a content-bound approval"
            )
        approval = _closed(
            approval,
            _APPROVAL_FIELDS,
            f"{context}.execution.concurrency_approval",
        )
        _identifier(
            approval["decision_id"],
            f"{context}.execution.concurrency_approval.decision_id",
        )
        _sha256(
            approval["decision_sha256"],
            f"{context}.execution.concurrency_approval.decision_sha256",
        )
    evaluators = _array(record["evaluators"], f"{context}.evaluators", nonempty=True)
    identifiers: set[str] = set()
    orders: set[int] = set()
    for evaluator_index, evaluator in enumerate(evaluators):
        _validate_evaluator(evaluator, evaluator_index, event=event, mode=mode)
        identifier = evaluator["id"]
        if identifier in identifiers:
            raise HarnessPolicyV2Error(f"{context} repeats evaluator {identifier!r}")
        identifiers.add(identifier)
        if mode == "ordered":
            order = evaluator["order"]
            if order in orders:
                raise HarnessPolicyV2Error(
                    f"{context} has ambiguous evaluator order {order}"
                )
            orders.add(order)
    _provenance(record["provenance"], f"{context}.provenance")
    _semantic_digest(record, context)


def _validate_skill(record: Any, index: int) -> None:
    context = f"skills[{index}]"
    record = _closed(record, _SKILL_FIELDS, context)
    _identifier(record["id"], f"{context}.id")
    _string(record["description"], f"{context}.description", maximum=4096)
    _sha256(record["content_sha256"], f"{context}.content_sha256")
    _requirement(record["requirement"], f"{context}.requirement")
    _provenance(record["provenance"], f"{context}.provenance")
    _semantic_digest(record, context)


def _sorted_capabilities(value: Any, context: str) -> list[str]:
    values = _array(value, context)
    checked = [
        _choice(item, ALL_CAPABILITIES, f"{context}[{index}]")
        for index, item in enumerate(values)
    ]
    if checked != sorted(set(checked)):
        raise HarnessPolicyV2Error(f"{context} must be sorted and contain no duplicates")
    return checked


def _validate_agent_role(record: Any, index: int) -> None:
    context = f"agent_roles[{index}]"
    record = _closed(record, _AGENT_ROLE_FIELDS, context)
    _identifier(record["id"], f"{context}.id")
    _string(record["purpose"], f"{context}.purpose", maximum=4096)
    _string(record["instructions"], f"{context}.instructions", maximum=1048576)
    _sorted_capabilities(
        record["required_capabilities"], f"{context}.required_capabilities"
    )
    _requirement(record["requirement"], f"{context}.requirement")
    _provenance(record["provenance"], f"{context}.provenance")
    _semantic_digest(record, context)


def _validate_mcp(record: Any, index: int) -> None:
    context = f"mcp_servers[{index}]"
    record = _closed(record, _MCP_FIELDS, context)
    _identifier(record["id"], f"{context}.id")
    _choice(record["transport"], frozenset({"stdio", "streamable-http"}), f"{context}.transport")
    _identifier(record["connection_ref"], f"{context}.connection_ref")
    _requirement(record["requirement"], f"{context}.requirement")
    _provenance(record["provenance"], f"{context}.provenance")
    _semantic_digest(record, context)


def _validate_capability(record: Any, index: int) -> None:
    context = f"capabilities[{index}]"
    record = _closed(record, _CAPABILITY_FIELDS, context)
    _choice(record["id"], ALL_CAPABILITIES, f"{context}.id")
    _requirement(record["requirement"], f"{context}.requirement")
    _provenance(record["provenance"], f"{context}.provenance")
    _semantic_digest(record, context)


def _unique_records(records: list[dict[str, Any]], collection: str) -> None:
    identifiers: set[str] = set()
    for record in records:
        identifier = record["id"]
        if identifier in identifiers:
            raise HarnessPolicyV2Error(f"{collection} repeats id {identifier!r}")
        identifiers.add(identifier)


def _canonical_document(raw: dict[str, Any]) -> dict[str, Any]:
    document = copy.deepcopy(raw)
    document["instructions"] = sorted(
        document["instructions"], key=lambda item: (item["order"], item["id"])
    )
    policies = []
    for policy in document["event_policies"]:
        if policy["execution"]["mode"] == "ordered":
            policy["evaluators"] = sorted(
                policy["evaluators"], key=lambda item: (item["order"], item["id"])
            )
        else:
            policy["evaluators"] = sorted(
                policy["evaluators"], key=lambda item: item["id"]
            )
        policies.append(policy)
    document["event_policies"] = sorted(
        policies, key=lambda item: (item["event"], item["id"])
    )
    for collection in ("skills", "agent_roles", "mcp_servers", "capabilities"):
        document[collection] = sorted(document[collection], key=lambda item: item["id"])
    return document


def _validate_required_semantics(document: dict[str, Any]) -> None:
    required = {
        record["id"]
        for record in document["capabilities"]
        if record["requirement"] == "required"
    }
    available = {"semantic-instructions"}
    if document["event_policies"]:
        available.add("semantic-hooks")
    if document["skills"]:
        available.add("semantic-skills")
    if document["agent_roles"]:
        available.add("semantic-agent-roles")
    if document["mcp_servers"]:
        available.add("semantic-mcp")
    if any(
        evaluator["authority"] == "safety"
        for policy in document["event_policies"]
        for evaluator in policy["evaluators"]
    ):
        available.add("semantic-safety-predicates")
    missing = sorted((required & {
        "semantic-instructions",
        "semantic-hooks",
        "semantic-skills",
        "semantic-agent-roles",
        "semantic-mcp",
        "semantic-safety-predicates",
    }) - available)
    if missing:
        raise HarnessPolicyV2Error(
            f"required semantic capabilities have no declarations: {missing}"
        )


def compile_harness_policy_v2(value: Mapping[str, Any] | HarnessPolicyV2) -> HarnessPolicyV2:
    """Validate and compile one v2 policy into deterministic in-memory JSON bytes."""

    if isinstance(value, HarnessPolicyV2):
        compiled = compile_harness_policy_v2(value.document)
        if compiled.sha256 != value.sha256 or compiled.canonical_bytes != value.canonical_bytes:
            raise HarnessPolicyV2Error("compiled policy no longer matches its document")
        return compiled
    if not isinstance(value, Mapping):
        raise HarnessPolicyV2Error("harness policy v2 must be an object")
    raw = _closed(copy.deepcopy(dict(value)), _TOP_FIELDS, "harness policy v2")
    if raw["schema_version"] != "2":
        raise HarnessPolicyV2Error("unsupported harness policy schema")
    _identifier(raw["policy_id"], "policy_id")
    _version(raw["policy_version"], "policy_version")
    _provenance(raw["provenance"], "provenance")
    migration = raw["migration"]
    if migration is not None:
        migration = _closed(migration, _MIGRATION_FIELDS, "migration")
        if migration["source_schema_version"] != "1":
            raise HarnessPolicyV2Error("migration source schema must be version 1")
        _sha256(migration["source_policy_sha256"], "migration.source_policy_sha256")

    collections = {
        "instructions": (_validate_instruction, True),
        "event_policies": (_validate_event_policy, False),
        "skills": (_validate_skill, False),
        "agent_roles": (_validate_agent_role, False),
        "mcp_servers": (_validate_mcp, False),
        "capabilities": (_validate_capability, False),
    }
    for collection, (validator, nonempty) in collections.items():
        records = _array(raw[collection], collection, nonempty=nonempty)
        for index, record in enumerate(records):
            validator(record, index)
        _unique_records(records, collection)
    instruction_orders = [record["order"] for record in raw["instructions"]]
    if len(instruction_orders) != len(set(instruction_orders)):
        raise HarnessPolicyV2Error("instructions have ambiguous order")
    events = [record["event"] for record in raw["event_policies"]]
    if len(events) != len(set(events)):
        raise HarnessPolicyV2Error("co-matching event policies are not allowed")
    document = _canonical_document(raw)
    _validate_required_semantics(document)
    blob = _canonical_bytes(document)
    return HarnessPolicyV2(document=document, canonical_bytes=blob, sha256=_digest(blob))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HarnessPolicyV2Error(f"harness policy JSON contains duplicate key {key!r}")
        value[key] = item
    return value


def load_harness_policy_v2(path: Path | str) -> HarnessPolicyV2:
    """Load v2 JSON with duplicate-key rejection, then compile it purely in memory."""

    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except HarnessPolicyV2Error:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessPolicyV2Error(f"cannot read harness policy v2 {path}: {exc}") from exc
    return compile_harness_policy_v2(value)


def _v2_record(
    record: Mapping[str, Any],
    *,
    drop: tuple[str, ...] = (),
    **changes: Any,
) -> dict[str, Any]:
    value = copy.deepcopy(dict(record))
    for key in drop:
        value.pop(key, None)
    value.update(changes)
    value.pop("semantic_sha256", None)
    value["semantic_sha256"] = semantic_record_sha256_v2(value)
    return value


def migrate_harness_policy_v1(
    value: Mapping[str, Any] | HarnessPolicy,
    *,
    target_policy_version: str,
) -> HarnessPolicyV2:
    """Losslessly migrate representable v1 semantics; reject anything needing a CP02 ruling."""

    _version(target_policy_version, "target_policy_version")
    if isinstance(value, HarnessPolicy):
        source = validate_harness_policy(value.document)
        if source.sha256 != value.sha256:
            raise HarnessPolicyV2Error("validated v1 policy no longer matches its document")
    else:
        source = validate_harness_policy(dict(value))
    document = source.document
    if document["safety_predicates"]:
        raise HarnessPolicyV2Error(
            "CP02 ruling required: v1 safety_predicates do not identify an event or "
            "their ordering relative to hooks"
        )

    instructions = [_v2_record(record) for record in document["instructions"]]
    skills = [
        _v2_record(
            record,
            requirement="required" if record["required"] else "optional",
            drop=("required",),
        )
        for record in document["skills"]
    ]
    roles = [
        _v2_record(
            record,
            requirement="required" if record["required"] else "optional",
            drop=("required",),
        )
        for record in document["agent_roles"]
    ]
    mcp_servers = [
        _v2_record(
            record,
            requirement="required" if record["required"] else "optional",
            drop=("required",),
        )
        for record in document["mcp_servers"]
    ]
    capabilities = [
        _v2_record(record, requirement="required")
        for record in document["required_capabilities"]
    ]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hook in document["hooks"]:
        event = _V1_EVENT_MAP[hook["event"]]
        tool_families = sorted(_V1_TOOL_MAP[item] for item in hook["tool_classes"])
        if event in TOOL_SCOPED_EVENTS and not tool_families:
            raise HarnessPolicyV2Error(
                f"v1 hook {hook['id']!r} has an empty tool matcher; CP02 must define "
                "whether that means no tools or any tool"
            )
        if event not in TOOL_SCOPED_EVENTS and tool_families:
            raise HarnessPolicyV2Error(
                f"v1 hook {hook['id']!r} attaches tool classes to a non-tool event"
            )
        evaluator = {
            "id": hook["id"],
            "order": hook["order"],
            "matcher": {"tool_families": tool_families},
            "implementation": hook["handler"],
            "timeout": {"value": hook["timeout_ms"], "unit": "milliseconds"},
            "failure_policy": hook["failure_policy"],
            "authority": hook["authority"],
            "requirement": "required" if hook["required"] else "optional",
            "provenance": hook["provenance"],
        }
        evaluator["semantic_sha256"] = semantic_record_sha256_v2(evaluator)
        grouped[event].append(evaluator)

    event_policies: list[dict[str, Any]] = []
    for event, evaluators in grouped.items():
        failure_policies = {item["failure_policy"] for item in evaluators}
        if len(failure_policies) != 1:
            raise HarnessPolicyV2Error(
                f"CP02 ruling required: v1 event {event!r} has mixed failure policies"
            )
        event_policy = {
            "id": f"{event}-policy",
            "event": event,
            "event_failure_policy": next(iter(failure_policies)),
            "execution": {"mode": "ordered", "concurrency_approval": None},
            "evaluators": sorted(evaluators, key=lambda item: (item["order"], item["id"])),
            "provenance": document["provenance"],
        }
        event_policy["semantic_sha256"] = semantic_record_sha256_v2(event_policy)
        event_policies.append(event_policy)

    migrated = {
        "schema_version": "2",
        "policy_id": document["policy_id"],
        "policy_version": target_policy_version,
        "provenance": document["provenance"],
        "migration": {
            "source_schema_version": "1",
            "source_policy_sha256": source.sha256,
        },
        "instructions": instructions,
        "event_policies": event_policies,
        "skills": skills,
        "agent_roles": roles,
        "mcp_servers": mcp_servers,
        "capabilities": capabilities,
    }
    return compile_harness_policy_v2(migrated)


__all__ = [
    "HarnessPolicyV2",
    "HarnessPolicyV2Error",
    "SEMANTIC_EVENTS",
    "TOOL_FAMILIES",
    "TOOL_SCOPED_EVENTS",
    "compile_harness_policy_v2",
    "load_harness_policy_v2",
    "migrate_harness_policy_v1",
    "semantic_record_sha256_v2",
]
