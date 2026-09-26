"""Closed provider-neutral hook events and ordered aggregate results.

Provider adapters may use this module only after they have interpreted their documented native
surface.  Native payloads and provider extensions are retained by digest and byte count, never as
prompt, transcript, tool-input, or tool-result prose.  Evaluators therefore receive one semantic
contract independent of Claude Code or Codex transport syntax.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.harness_policy_v2 import SEMANTIC_EVENTS, TOOL_FAMILIES, TOOL_SCOPED_EVENTS


class NormalizedHookError(ValueError):
    """A normalized hook event or result is malformed, ambiguous, or not content-bound."""


_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "event_id",
        "occurred_at",
        "semantic_event",
        "scope",
        "source",
        "identity",
        "repository",
        "tool",
        "provider_extension",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "provider",
        "adapter",
        "adapter_version",
        "native_event",
        "evidence_kind",
        "input_sha256",
        "input_byte_count",
    }
)
_IDENTITY_FIELDS = frozenset({"session_id", "thread_id", "turn_id", "tool_call_id"})
_REPOSITORY_FIELDS = frozenset({"common_dir_sha256", "worktree_sha256", "head_oid", "branch"})
_TOOL_FIELDS = frozenset({"family", "operation", "status", "effects"})
_EFFECT_FIELDS = frozenset({"kind", "path", "target_path", "status"})
_DIGEST_FIELDS = frozenset({"sha256", "byte_count"})
_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "result_id",
        "event_id",
        "completed_at",
        "runtime",
        "decision",
        "evaluations",
        "remediation",
    }
)
_RUNTIME_FIELDS = frozenset({"id", "version", "content_sha256"})
_EVALUATION_FIELDS = frozenset({"evaluator_id", "order", "decision", "evidence", "remediation"})
_REMEDIATION_FIELDS = frozenset({"code", "message", "paths"})

PROVIDERS = frozenset({"anthropic-claude", "openai-codex"})
SESSION_SCOPED_EVENTS = frozenset({"session-start", "session-end"})
TURN_SCOPED_EVENTS = SEMANTIC_EVENTS - SESSION_SCOPED_EVENTS
TOOL_EFFECT_KINDS = frozenset({"read", "create", "modify", "delete", "rename"})
TOOL_EFFECT_STATUSES = frozenset({"intended", "succeeded", "failed", "partial"})
EFFECT_STATUSES = frozenset({"intended", "succeeded", "failed", "unknown"})
DECISIONS = frozenset({"allow", "block", "continue"})

_TOKEN = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_NATIVE_EVENT = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_EVENT_DOMAIN = b"bear-hug/normalized-hook-event/v1\0"
_RESULT_DOMAIN = b"bear-hug/normalized-hook-result/v1\0"
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_RECORD_BYTES = 64 * 1024


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NormalizedHookError(f"value is not canonical JSON: {exc}") from exc


def _exact(value: Any, fields: frozenset[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise NormalizedHookError(f"{where} has missing or unknown fields")
    return value


def _identifier(value: Any, where: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise NormalizedHookError(f"{where} must be a bounded non-empty identifier")
    return value


def _token(value: Any, where: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise NormalizedHookError(f"{where} must be a canonical token")
    return value


def _sha256(value: Any, where: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise NormalizedHookError(f"{where} must be lowercase SHA-256")
    return value


def _count(value: Any, where: str, *, maximum: int = _MAX_INPUT_BYTES) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise NormalizedHookError(f"{where} must be an in-bounds non-negative integer")
    return value


def _canonical_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise NormalizedHookError("timestamp must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _timestamp(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise NormalizedHookError(f"{where} must be an exact UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise NormalizedHookError(f"{where} must be an exact UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise NormalizedHookError(f"{where} must be an exact UTC timestamp")
    return value


def _digest_record(value: Any, where: str) -> dict[str, Any]:
    record = _exact(value, _DIGEST_FIELDS, where)
    _sha256(record["sha256"], f"{where}.sha256")
    _count(record["byte_count"], f"{where}.byte_count")
    return record


def _safe_relative_path(value: Any, where: str) -> str:
    path = _identifier(value, where, maximum=1024)
    if "\\" in path or path.startswith("/") or "//" in path:
        raise NormalizedHookError(f"{where} must be a canonical repository-relative POSIX path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise NormalizedHookError(f"{where} must not contain empty or traversal segments")
    if PurePosixPath(path).as_posix() != path:
        raise NormalizedHookError(f"{where} must be a canonical repository-relative POSIX path")
    return path


def _validate_source(value: Any) -> str:
    source = _exact(value, _SOURCE_FIELDS, "source")
    provider = source["provider"]
    if provider not in PROVIDERS:
        raise NormalizedHookError("source.provider is unsupported")
    _token(source["adapter"], "source.adapter")
    _identifier(source["adapter_version"], "source.adapter_version", maximum=128)
    if (
        not isinstance(source["native_event"], str)
        or _NATIVE_EVENT.fullmatch(source["native_event"]) is None
    ):
        raise NormalizedHookError("source.native_event is not canonical")
    if source["evidence_kind"] not in {"native-hook", "provider-substitute"}:
        raise NormalizedHookError("source.evidence_kind is unsupported")
    _sha256(source["input_sha256"], "source.input_sha256")
    _count(source["input_byte_count"], "source.input_byte_count")
    return provider


def _validate_identity(value: Any, *, provider: str, semantic_event: str, scope: str) -> None:
    identity = _exact(value, _IDENTITY_FIELDS, "identity")
    _identifier(identity["session_id"], "identity.session_id")
    thread_id = identity["thread_id"]
    if provider == "anthropic-claude":
        if thread_id is not None:
            raise NormalizedHookError("Claude identity.thread_id must be null")
    elif thread_id is None:
        raise NormalizedHookError("Codex identity.thread_id is required")
    else:
        _identifier(thread_id, "identity.thread_id")

    turn_id = identity["turn_id"]
    if scope == "session":
        if turn_id is not None:
            raise NormalizedHookError("session-scoped event identity.turn_id must be null")
    elif scope == "turn" and turn_id is None:
        raise NormalizedHookError("turn-scoped event identity.turn_id is required")
    elif scope == "turn":
        _identifier(turn_id, "identity.turn_id")
    else:
        raise NormalizedHookError("scope is unsupported")

    tool_call_id = identity["tool_call_id"]
    if semantic_event in TOOL_SCOPED_EVENTS:
        if tool_call_id is None:
            raise NormalizedHookError("tool-scoped event identity.tool_call_id is required")
        _identifier(tool_call_id, "identity.tool_call_id")
    elif tool_call_id is not None:
        raise NormalizedHookError("non-tool event identity.tool_call_id must be null")


def _validate_repository(value: Any) -> None:
    repository = _exact(value, _REPOSITORY_FIELDS, "repository")
    _sha256(repository["common_dir_sha256"], "repository.common_dir_sha256")
    _sha256(repository["worktree_sha256"], "repository.worktree_sha256")
    if (
        not isinstance(repository["head_oid"], str)
        or _GIT_OID.fullmatch(repository["head_oid"]) is None
    ):
        raise NormalizedHookError("repository.head_oid must be a lowercase Git object id")
    if repository["branch"] is not None:
        _identifier(repository["branch"], "repository.branch", maximum=1024)


def _validate_effect(value: Any, index: int) -> tuple[str, str, str | None, str]:
    where = f"tool.effects[{index}]"
    effect = _exact(value, _EFFECT_FIELDS, where)
    kind = effect["kind"]
    if kind not in TOOL_EFFECT_KINDS:
        raise NormalizedHookError(f"{where}.kind is unsupported")
    path = _safe_relative_path(effect["path"], f"{where}.path")
    target = effect["target_path"]
    if kind == "rename":
        target = _safe_relative_path(target, f"{where}.target_path")
        if target == path:
            raise NormalizedHookError(f"{where} rename source and target must differ")
    elif target is not None:
        raise NormalizedHookError(f"{where}.target_path is only valid for rename")
    status = effect["status"]
    if status not in EFFECT_STATUSES:
        raise NormalizedHookError(f"{where}.status is unsupported")
    return kind, path, target, status


def _validate_tool(value: Any, *, semantic_event: str) -> None:
    if semantic_event not in TOOL_SCOPED_EVENTS:
        if value is not None:
            raise NormalizedHookError("non-tool semantic event must have a null tool")
        return
    tool = _exact(value, _TOOL_FIELDS, "tool")
    family = tool["family"]
    if family not in TOOL_FAMILIES or family == "any-tool":
        raise NormalizedHookError("tool.family must name one concrete policy tool family")
    _token(tool["operation"], "tool.operation")
    status = tool["status"]
    if status not in TOOL_EFFECT_STATUSES:
        raise NormalizedHookError("tool.status is unsupported")
    if semantic_event in {"pre-tool-use", "permission-request"} and status != "intended":
        raise NormalizedHookError("pre-decision tool event status must be intended")
    if semantic_event == "post-tool-use" and status == "intended":
        raise NormalizedHookError("post-tool-use status must report an observed outcome")
    if not isinstance(tool["effects"], list) or len(tool["effects"]) > 256:
        raise NormalizedHookError("tool.effects must be a bounded array")
    seen: set[tuple[str, str, str | None, str]] = set()
    effect_statuses: set[str] = set()
    for index, value in enumerate(tool["effects"]):
        effect = _validate_effect(value, index)
        if effect in seen:
            raise NormalizedHookError("tool.effects contains a duplicate effect")
        seen.add(effect)
        effect_statuses.add(effect[3])
    if semantic_event in {"pre-tool-use", "permission-request"} and effect_statuses - {"intended"}:
        raise NormalizedHookError("pre-decision tool effects must be intended")
    if semantic_event == "post-tool-use":
        if "intended" in effect_statuses:
            raise NormalizedHookError("post-tool-use effects must report observed outcomes")
        if status == "succeeded" and effect_statuses - {"succeeded"}:
            raise NormalizedHookError("successful post-tool-use has a non-successful effect")
        if status == "failed" and "succeeded" in effect_statuses:
            raise NormalizedHookError("failed post-tool-use carries a successful effect")
        if status == "partial" and (
            "succeeded" not in effect_statuses or effect_statuses == {"succeeded"}
        ):
            raise NormalizedHookError(
                "partial post-tool-use must distinguish successful and unsuccessful effects"
            )


def normalized_hook_event_sha256(value: Mapping[str, Any]) -> str:
    """Hash one event excluding only its self-referential event id."""

    material = copy.deepcopy(dict(value))
    material.pop("event_id", None)
    return hashlib.sha256(_EVENT_DOMAIN + _canonical_json(material)).hexdigest()


def validate_normalized_hook_event(value: Any) -> dict[str, Any]:
    """Validate one closed normalized event and its deterministic content identity."""

    event = _exact(copy.deepcopy(value), _EVENT_FIELDS, "normalized hook event")
    if event["schema_version"] != "1" or event["record_kind"] != "normalized_hook_event":
        raise NormalizedHookError("unsupported normalized hook event schema or record kind")
    _sha256(event["event_id"], "event_id")
    _timestamp(event["occurred_at"], "occurred_at")
    semantic_event = event["semantic_event"]
    if semantic_event not in SEMANTIC_EVENTS:
        raise NormalizedHookError("semantic_event is unsupported by harness-policy.v2")
    scope = event["scope"]
    if scope not in {"session", "turn"}:
        raise NormalizedHookError("scope is unsupported")
    if semantic_event in SESSION_SCOPED_EVENTS and scope != "session":
        raise NormalizedHookError(f"{semantic_event} must be session-scoped")
    if semantic_event in TURN_SCOPED_EVENTS and scope != "turn":
        raise NormalizedHookError(f"{semantic_event} must be turn-scoped")
    provider = _validate_source(event["source"])
    _validate_identity(
        event["identity"], provider=provider, semantic_event=semantic_event, scope=scope
    )
    _validate_repository(event["repository"])
    _validate_tool(event["tool"], semantic_event=semantic_event)
    extension = event["provider_extension"]
    if extension is not None:
        _digest_record(extension, "provider_extension")
    if normalized_hook_event_sha256(event) != event["event_id"]:
        raise NormalizedHookError("event_id does not match the canonical event")
    if len(_canonical_json(event)) > _MAX_RECORD_BYTES:
        raise NormalizedHookError("normalized hook event exceeds its byte bound")
    return event


def build_normalized_hook_event(
    *,
    occurred_at: datetime,
    semantic_event: str,
    scope: str,
    provider: str,
    adapter: str,
    adapter_version: str,
    native_event: str,
    evidence_kind: str,
    native_input: bytes,
    session_id: str,
    thread_id: str | None,
    turn_id: str | None,
    tool_call_id: str | None,
    repository: Mapping[str, Any],
    tool: Mapping[str, Any] | None,
    provider_extension: bytes | None = None,
) -> dict[str, Any]:
    """Build an event while immediately discarding native and extension content."""

    if not isinstance(native_input, bytes) or len(native_input) > _MAX_INPUT_BYTES:
        raise NormalizedHookError("native_input must be bounded exact bytes")
    if provider_extension is not None and (
        not isinstance(provider_extension, bytes) or len(provider_extension) > _MAX_INPUT_BYTES
    ):
        raise NormalizedHookError("provider_extension must be bounded exact bytes or null")
    event: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "normalized_hook_event",
        "occurred_at": _canonical_timestamp(occurred_at),
        "semantic_event": semantic_event,
        "scope": scope,
        "source": {
            "provider": provider,
            "adapter": adapter,
            "adapter_version": adapter_version,
            "native_event": native_event,
            "evidence_kind": evidence_kind,
            "input_sha256": hashlib.sha256(native_input).hexdigest(),
            "input_byte_count": len(native_input),
        },
        "identity": {
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "tool_call_id": tool_call_id,
        },
        "repository": copy.deepcopy(dict(repository)),
        "tool": None if tool is None else copy.deepcopy(dict(tool)),
        "provider_extension": (
            None
            if provider_extension is None
            else {
                "sha256": hashlib.sha256(provider_extension).hexdigest(),
                "byte_count": len(provider_extension),
            }
        ),
    }
    event["event_id"] = normalized_hook_event_sha256(event)
    return validate_normalized_hook_event(event)


def _validate_remediation(value: Any, where: str) -> dict[str, Any]:
    item = _exact(value, _REMEDIATION_FIELDS, where)
    _token(item["code"], f"{where}.code")
    _identifier(item["message"], f"{where}.message", maximum=2048)
    if not isinstance(item["paths"], list) or len(item["paths"]) > 32:
        raise NormalizedHookError(f"{where}.paths must be a bounded array")
    paths = [
        _safe_relative_path(path, f"{where}.paths[{index}]")
        for index, path in enumerate(item["paths"])
    ]
    if paths != sorted(set(paths)):
        raise NormalizedHookError(f"{where}.paths must be sorted without duplicates")
    return item


def _validate_evaluation(value: Any, index: int) -> dict[str, Any]:
    where = f"evaluations[{index}]"
    evaluation = _exact(value, _EVALUATION_FIELDS, where)
    _token(evaluation["evaluator_id"], f"{where}.evaluator_id")
    if type(evaluation["order"]) is not int or not 0 <= evaluation["order"] <= 65535:
        raise NormalizedHookError(f"{where}.order must be an in-bounds non-negative integer")
    if evaluation["decision"] not in DECISIONS:
        raise NormalizedHookError(f"{where}.decision is unsupported")
    if evaluation["evidence"] is not None:
        _digest_record(evaluation["evidence"], f"{where}.evidence")
    remediation = evaluation["remediation"]
    if not isinstance(remediation, list) or len(remediation) > 8:
        raise NormalizedHookError(f"{where}.remediation must be a bounded array")
    for remediation_index, item in enumerate(remediation):
        _validate_remediation(item, f"{where}.remediation[{remediation_index}]")
    if evaluation["decision"] == "block" and not remediation:
        raise NormalizedHookError(f"{where} blocks without bounded remediation")
    if evaluation["decision"] != "block" and remediation:
        raise NormalizedHookError(f"{where} may remediate only a block decision")
    return evaluation


def _aggregate_decision(evaluations: Iterable[Mapping[str, Any]]) -> str:
    decisions = [item["decision"] for item in evaluations]
    if "block" in decisions:
        return "block"
    if "allow" in decisions:
        return "allow"
    return "continue"


def normalized_hook_result_sha256(value: Mapping[str, Any]) -> str:
    """Hash one result excluding only its self-referential result id."""

    material = copy.deepcopy(dict(value))
    material.pop("result_id", None)
    return hashlib.sha256(_RESULT_DOMAIN + _canonical_json(material)).hexdigest()


def validate_normalized_hook_result(value: Any) -> dict[str, Any]:
    """Validate one closed ordered result and recompute its aggregate decision."""

    result = _exact(copy.deepcopy(value), _RESULT_FIELDS, "normalized hook result")
    if result["schema_version"] != "1" or result["record_kind"] != "normalized_hook_result":
        raise NormalizedHookError("unsupported normalized hook result schema or record kind")
    _sha256(result["result_id"], "result_id")
    _sha256(result["event_id"], "event_id")
    _timestamp(result["completed_at"], "completed_at")
    runtime = _exact(result["runtime"], _RUNTIME_FIELDS, "runtime")
    _token(runtime["id"], "runtime.id")
    _identifier(runtime["version"], "runtime.version", maximum=128)
    _sha256(runtime["content_sha256"], "runtime.content_sha256")
    if result["decision"] not in DECISIONS:
        raise NormalizedHookError("decision is unsupported")
    evaluations = result["evaluations"]
    if not isinstance(evaluations, list) or not evaluations or len(evaluations) > 32:
        raise NormalizedHookError("evaluations must be a non-empty bounded array")
    previous_order = -1
    evaluator_ids: set[str] = set()
    expected_remediation: list[dict[str, Any]] = []
    for index, value in enumerate(evaluations):
        evaluation = _validate_evaluation(value, index)
        if evaluation["order"] <= previous_order:
            raise NormalizedHookError("evaluations must have a strictly increasing total order")
        previous_order = evaluation["order"]
        if evaluation["evaluator_id"] in evaluator_ids:
            raise NormalizedHookError("evaluations repeat an evaluator_id")
        evaluator_ids.add(evaluation["evaluator_id"])
        if evaluation["decision"] == "block":
            expected_remediation.extend(copy.deepcopy(evaluation["remediation"]))
    if len(expected_remediation) > 32:
        raise NormalizedHookError("aggregate remediation exceeds its item bound")
    if result["decision"] != _aggregate_decision(evaluations):
        raise NormalizedHookError("decision does not match ordered evaluator decisions")
    if result["remediation"] != expected_remediation:
        raise NormalizedHookError("remediation is not the ordered aggregate of blocking results")
    if normalized_hook_result_sha256(result) != result["result_id"]:
        raise NormalizedHookError("result_id does not match the canonical result")
    if len(_canonical_json(result)) > _MAX_RECORD_BYTES:
        raise NormalizedHookError("normalized hook result exceeds its byte bound")
    return result


def build_hook_evaluation(
    *,
    evaluator_id: str,
    order: int,
    decision: str,
    evidence: bytes | None = None,
    remediation: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build one privacy-bounded evaluator result for ordered aggregation."""

    if evidence is not None and (
        not isinstance(evidence, bytes) or len(evidence) > _MAX_INPUT_BYTES
    ):
        raise NormalizedHookError("evidence must be bounded exact bytes or null")
    value = {
        "evaluator_id": evaluator_id,
        "order": order,
        "decision": decision,
        "evidence": (
            None
            if evidence is None
            else {"sha256": hashlib.sha256(evidence).hexdigest(), "byte_count": len(evidence)}
        ),
        "remediation": [copy.deepcopy(dict(item)) for item in remediation],
    }
    return _validate_evaluation(value, 0)


def build_normalized_hook_result(
    *,
    event_id: str,
    completed_at: datetime,
    runtime_id: str,
    runtime_version: str,
    runtime_sha256: str,
    evaluations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the sole aggregate decision from already ordered evaluator results."""

    ordered = [copy.deepcopy(dict(item)) for item in evaluations]
    for index, evaluation in enumerate(ordered):
        _validate_evaluation(evaluation, index)
    remediation = [
        copy.deepcopy(item)
        for evaluation in ordered
        if evaluation.get("decision") == "block"
        for item in evaluation.get("remediation", [])
    ]
    result: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "normalized_hook_result",
        "event_id": event_id,
        "completed_at": _canonical_timestamp(completed_at),
        "runtime": {
            "id": runtime_id,
            "version": runtime_version,
            "content_sha256": runtime_sha256,
        },
        "decision": _aggregate_decision(ordered),
        "evaluations": ordered,
        "remediation": remediation,
    }
    result["result_id"] = normalized_hook_result_sha256(result)
    return validate_normalized_hook_result(result)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NormalizedHookError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(path: Path | str, validator: Any, kind: str) -> dict[str, Any]:
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except NormalizedHookError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NormalizedHookError(f"cannot load normalized hook {kind}: {exc}") from exc
    return validator(value)


def load_normalized_hook_event(path: Path | str) -> dict[str, Any]:
    """Load a normalized event with duplicate-key rejection."""

    return _load(path, validate_normalized_hook_event, "event")


def load_normalized_hook_result(path: Path | str) -> dict[str, Any]:
    """Load a normalized result with duplicate-key rejection."""

    return _load(path, validate_normalized_hook_result, "result")


__all__ = [
    "DECISIONS",
    "EFFECT_STATUSES",
    "NormalizedHookError",
    "PROVIDERS",
    "SESSION_SCOPED_EVENTS",
    "TOOL_EFFECT_KINDS",
    "TOOL_EFFECT_STATUSES",
    "TURN_SCOPED_EVENTS",
    "build_hook_evaluation",
    "build_normalized_hook_event",
    "build_normalized_hook_result",
    "load_normalized_hook_event",
    "load_normalized_hook_result",
    "normalized_hook_event_sha256",
    "normalized_hook_result_sha256",
    "validate_normalized_hook_event",
    "validate_normalized_hook_result",
]
