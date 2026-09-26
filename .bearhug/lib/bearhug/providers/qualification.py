"""Exact-version, capture-backed fixture qualification for provider adapters.

Constructed fixtures remain useful parser tests, but never count as qualification evidence.
Captured fixtures are privacy-bounded projections of raw provider bytes held with their canonical
provider-session receipts in a separate evidence root. Qualification reopens and revalidates that
private evidence on every run. This module reports evidence; it never grants compatibility.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.providers.claude import (
    ClaudeEventError,
    ClaudeNormalization,
    normalize_claude_jsonl,
)
from bearhug.providers.codex_app_server import (
    AppServerEventError,
    CodexAppServerNormalization,
    normalize_app_server_jsonl,
)
from bearhug.providers.receipt import ProviderReceiptError, validate_provider_receipt

CANONICAL_ALGORITHM = "bearhug-provider-qualification-canonical-json-sha256/1"
SANITIZER_VERSION = "bearhug-provider-fixture-sanitizer/1"

_PAIRS = {
    "anthropic-claude": "claude-code-stream-json",
    "openai-codex": "codex-app-server-stdio",
}
_TOP = frozenset(
    {
        "schema_version",
        "canonical_algorithm",
        "record_kind",
        "bundle_id",
        "provider",
        "adapter",
        "adapter_version",
        "normalizer_arguments",
        "fixtures",
    }
)
_NORMALIZER_ARGUMENTS = frozenset({"requested_model", "requested_reasoning_effort"})
_FIXTURE = frozenset(
    {
        "fixture_id",
        "path",
        "scenario",
        "origin",
        "fixture_sha256",
        "source_raw_sha256",
        "source_raw_event_count",
        "raw_evidence_path",
        "receipt_evidence_path",
        "receipt_sha256",
        "sanitizer_version",
        "sanitized_fixture_sha256",
    }
)
_SCENARIOS = frozenset({"success", "provider_failure", "protocol_failure"})
_ORIGINS = frozenset({"sanitized_captured", "constructed"})
_TOKEN = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASCII_PATH = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*(?![\s\S])"
)
_PSEUDONYM = re.compile(
    r"^(?:item|model|parent|request|session|thread|tool|turn)-fixture-[1-9][0-9]*$"
)

_CLAUDE_TYPES = frozenset(
    {"assistant", "hook_event", "prompt_suggestion", "result", "system", "unknown"}
)
_CLAUDE_SYSTEM_SUBTYPES = frozenset(
    {"hook_response", "hook_started", "init", "task_summary", "unknown"}
)
_CLAUDE_RESULT_SUBTYPES = frozenset({"error", "success"})
_CLAUDE_HOOKS = frozenset(
    {
        "ConfigChange",
        "Elicitation",
        "Notification",
        "PermissionRequest",
        "PostToolUse",
        "PostToolUseFailure",
        "PreCompact",
        "PreToolUse",
        "SessionEnd",
        "SessionStart",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "TaskCompleted",
        "TeammateIdle",
        "UnknownHook",
        "UserPromptSubmit",
    }
)
_CODEX_METHODS = frozenset(
    {
        "item/completed",
        "item/requestApproval",
        "item/started",
        "model/rerouted",
        "notification/unknown",
        "serverRequest/resolved",
        "turn/completed",
        "turn/started",
    }
)
_CODEX_TURN_STATUSES = frozenset({"completed", "failed", "interrupted", "unknown"})
_CODEX_ITEM_STATUSES = frozenset({"completed", "declined", "failed", "unknown"})
_SANDBOX_TYPES = frozenset({"readOnly", "workspaceWrite", "unknown"})
_APPROVAL_POLICIES = frozenset({"never", "on-request", "untrusted", "unknown"})
_EFFORTS = frozenset(
    {"auto", "high", "low", "max", "medium", "minimal", "none", "ultra", "unknown", "xhigh"}
)
_SAFE_LITERAL_STRINGS = frozenset(
    {
        *_CLAUDE_TYPES,
        *_CLAUDE_SYSTEM_SUBTYPES,
        *_CLAUDE_RESULT_SUBTYPES,
        *_CLAUDE_HOOKS,
        *_CODEX_METHODS,
        *_CODEX_TURN_STATUSES,
        *_CODEX_ITEM_STATUSES,
        *_SANDBOX_TYPES,
        *_APPROVAL_POLICIES,
        *_EFFORTS,
        "fixture-error",
        "tool_use",
    }
)
_SAFE_KEYS = frozenset(
    {
        "approvalPolicy",
        "code",
        "content",
        "effort",
        "error",
        "fromModel",
        "hook_event",
        "hook_event_name",
        "id",
        "is_error",
        "item",
        "message",
        "method",
        "model",
        "modelUsage",
        "name",
        "params",
        "parent_tool_use_id",
        "permission_denials",
        "reasoningEffort",
        "requestId",
        "result",
        "sandbox",
        "sessionId",
        "session_id",
        "status",
        "subtype",
        "thread",
        "threadId",
        "toModel",
        "turn",
        "turnId",
        "type",
        "uuid",
    }
)


class ProviderQualificationError(ValueError):
    """A fixture capture, manifest, or normalization claim is invalid."""


@dataclass(frozen=True, slots=True)
class SanitizedCapture:
    """A distributable projection bound to separate, revalidated private evidence."""

    provider: str
    adapter: str
    adapter_version: str
    source_raw_sha256: str
    source_raw_event_count: int
    raw_evidence_path: str
    receipt_evidence_path: str
    receipt_sha256: str
    sanitizer_version: str
    sanitized_fixture_sha256: str
    sanitized: bytes


@dataclass(frozen=True, slots=True)
class QualifiedFixture:
    fixture_id: str
    path: str
    scenario: str
    origin: str
    fixture_sha256: str
    terminal_state: str | None


@dataclass(frozen=True, slots=True)
class CompatibilityFixtureEvidence:
    path: str
    sha256: str
    scenario: str
    origin: str = "captured"


@dataclass(frozen=True, slots=True)
class CompatibilityQualificationEvidence:
    """Closed authority that a compatibility-policy mutation must match exactly."""

    provider: str
    adapter: str
    adapter_version: str
    bundle_id: str
    manifest_sha256: str
    fixtures: tuple[CompatibilityFixtureEvidence, ...]

    def policy_fixtures(
        self,
        *,
        expected_provider: str,
        expected_adapter: str,
        expected_adapter_version: str,
        expected_bundle_id: str,
        expected_manifest_sha256: str,
    ) -> tuple[dict[str, str], ...]:
        """Return policy rows only after all qualification identity is matched."""

        if (
            self.provider != expected_provider
            or self.adapter != expected_adapter
            or self.adapter_version != expected_adapter_version
            or self.bundle_id != expected_bundle_id
            or self.manifest_sha256 != expected_manifest_sha256
        ):
            raise ProviderQualificationError(
                "qualification evidence identity, bundle, or exact version mismatch"
            )
        return tuple(
            {
                "path": fixture.path,
                "sha256": fixture.sha256,
                "scenario": fixture.scenario,
                "origin": fixture.origin,
            }
            for fixture in self.fixtures
        )


@dataclass(frozen=True, slots=True)
class QualificationBundle:
    provider: str
    adapter: str
    adapter_version: str
    bundle_id: str
    manifest_sha256: str
    fixtures: tuple[QualifiedFixture, ...]
    qualification_ready: bool
    blockers: tuple[str, ...]

    def compatibility_fixtures(self) -> CompatibilityQualificationEvidence:
        """Return closed exact-version evidence, not identity-free fixture rows."""

        if not self.qualification_ready:
            raise ProviderQualificationError("fixture bundle is not qualification-ready")
        return CompatibilityQualificationEvidence(
            provider=self.provider,
            adapter=self.adapter,
            adapter_version=self.adapter_version,
            bundle_id=self.bundle_id,
            manifest_sha256=self.manifest_sha256,
            fixtures=tuple(
                CompatibilityFixtureEvidence(
                    fixture.path,
                    fixture.fixture_sha256,
                    fixture.scenario,
                )
                for fixture in self.fixtures
                if fixture.origin == "sanitized_captured"
            ),
        )


@dataclass(frozen=True, slots=True)
class _CaptureEvidence:
    raw: bytes
    receipt: dict[str, Any]
    receipt_sha256: str


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _canonical(value: dict[str, Any], *, where: str = "manifest") -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProviderQualificationError(f"{where} is not canonical JSON data: {exc}") from exc


def _jsonl(raw: bytes, *, where: str) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n"):
        raise ProviderQualificationError(f"{where} must be non-empty newline-terminated JSONL")
    records: list[dict[str, Any]] = []
    for index, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise ProviderQualificationError(f"{where} has a blank record at line {index}")
        try:
            value = json.loads(line, object_pairs_hook=_closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProviderQualificationError(
                f"{where} has invalid JSON at line {index}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ProviderQualificationError(f"{where} line {index} is not an object")
        records.append(value)
    return records


def _pair(provider: Any, adapter: Any) -> tuple[str, str]:
    if not isinstance(provider, str) or not isinstance(adapter, str):
        raise ProviderQualificationError("provider and adapter must be strings")
    if provider not in _PAIRS:
        raise ProviderQualificationError(f"unknown provider {provider!r}")
    if _PAIRS[provider] != adapter:
        raise ProviderQualificationError(
            f"unknown or mismatched adapter {adapter!r} for provider {provider!r}"
        )
    return provider, adapter


def _string(value: Any, where: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProviderQualificationError(f"{where} must be a non-empty bounded string")
    return value


def _digest(value: Any, where: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProviderQualificationError(f"{where} must be lowercase SHA-256")
    return value


def _path(value: Any, where: str) -> str:
    value = _string(value, where, maximum=512)
    if _ASCII_PATH.fullmatch(value) is None:
        raise ProviderQualificationError(f"{where} must be a canonical ASCII relative path")
    return value


class _IdentifierMap:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get(self, kind: str, value: Any) -> Any:
        if not isinstance(value, str) or not value:
            return value
        key = (kind, value)
        if key not in self._values:
            count = 1 + sum(item_kind == kind for item_kind, _ in self._values)
            self._values[key] = f"{kind}-fixture-{count}"
        return self._values[key]


def _enum(value: Any, allowed: frozenset[str], fallback: str = "unknown") -> Any:
    if not isinstance(value, str):
        return value
    return value if value in allowed else fallback


def _claude_projection(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identities = _IdentifierMap()
    projected: list[dict[str, Any]] = []
    for record in records:
        item: dict[str, Any] = {}
        record_type = _enum(record.get("type"), _CLAUDE_TYPES)
        if "type" in record:
            item["type"] = record_type
        if "subtype" in record:
            if record_type == "system":
                item["subtype"] = _enum(record["subtype"], _CLAUDE_SYSTEM_SUBTYPES)
            elif record_type == "result":
                item["subtype"] = "success" if record["subtype"] == "success" else "error"
            else:
                item["subtype"] = "unknown"
        if "session_id" in record:
            item["session_id"] = identities.get("session", record["session_id"])
        if record_type == "system" and record.get("subtype") == "init":
            if "model" in record:
                item["model"] = identities.get("model", record["model"])
            if "effort" in record:
                item["effort"] = _enum(record["effort"], _EFFORTS)
        if record_type == "assistant":
            if "parent_tool_use_id" in record:
                parent = record["parent_tool_use_id"]
                item["parent_tool_use_id"] = (
                    None if parent is None else identities.get("parent", parent)
                )
            message = record.get("message")
            if isinstance(message, dict):
                safe_message: dict[str, Any] = {}
                if "model" in message:
                    safe_message["model"] = identities.get("model", message["model"])
                content = message.get("content")
                tools = []
                if isinstance(content, list):
                    tools = [
                        {
                            "type": "tool_use",
                            "name": identities.get("tool", block.get("name")),
                        }
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "tool_use"
                    ]
                if tools:
                    safe_message["content"] = tools
                item["message"] = safe_message
        if (
            record_type == "system"
            and record.get("subtype") in {"hook_started", "hook_response"}
            and "hook_event" in record
        ):
            item["hook_event"] = _enum(record["hook_event"], _CLAUDE_HOOKS, "UnknownHook")
        if record_type == "hook_event" and "hook_event_name" in record:
            item["hook_event_name"] = _enum(record["hook_event_name"], _CLAUDE_HOOKS, "UnknownHook")
        if record_type == "result":
            if "is_error" in record:
                item["is_error"] = record["is_error"]
            if "uuid" in record:
                item["uuid"] = identities.get("turn", record["uuid"])
            usage = record.get("modelUsage")
            if isinstance(usage, dict):
                item["modelUsage"] = {
                    str(identities.get("model", model)): {} for model in sorted(usage)
                }
            elif "modelUsage" in record:
                item["modelUsage"] = {}
            denials = record.get("permission_denials")
            if isinstance(denials, list):
                item["permission_denials"] = [{} for _ in denials]
            elif "permission_denials" in record:
                item["permission_denials"] = []
        projected.append(item)
    return projected


def _codex_projection(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identities = _IdentifierMap()
    projected: list[dict[str, Any]] = []

    def request_id(value: Any) -> Any:
        return value if type(value) is int else identities.get("request", value)

    for record in records:
        item: dict[str, Any] = {}
        if "id" in record:
            item["id"] = request_id(record["id"])
        method = record.get("method")
        if "method" in record:
            if isinstance(method, str) and method.endswith("/requestApproval"):
                item["method"] = "item/requestApproval"
            else:
                item["method"] = _enum(method, _CODEX_METHODS, "notification/unknown")
        if "error" in record:
            item["error"] = {"code": "fixture-error"}
        if "result" in record:
            result: dict[str, Any] = {}
            source_result = record["result"]
            if isinstance(source_result, dict) and record.get("id") == 1:
                if "model" in source_result:
                    result["model"] = identities.get("model", source_result["model"])
                if "reasoningEffort" in source_result:
                    result["reasoningEffort"] = _enum(source_result["reasoningEffort"], _EFFORTS)
                if "approvalPolicy" in source_result:
                    result["approvalPolicy"] = _enum(
                        source_result["approvalPolicy"], _APPROVAL_POLICIES
                    )
                sandbox = source_result.get("sandbox")
                if isinstance(sandbox, dict) and "type" in sandbox:
                    result["sandbox"] = {"type": _enum(sandbox["type"], _SANDBOX_TYPES)}
                thread = source_result.get("thread")
                if isinstance(thread, dict):
                    safe_thread: dict[str, Any] = {}
                    if "id" in thread:
                        safe_thread["id"] = identities.get("thread", thread["id"])
                    if "sessionId" in thread:
                        safe_thread["sessionId"] = identities.get("session", thread["sessionId"])
                    result["thread"] = safe_thread
            item["result"] = result

        params = record.get("params")
        if isinstance(params, dict):
            safe_params: dict[str, Any] = {}
            if "threadId" in params:
                safe_params["threadId"] = identities.get("thread", params["threadId"])
            if "turnId" in params:
                safe_params["turnId"] = identities.get("turn", params["turnId"])
            if "requestId" in params:
                safe_params["requestId"] = request_id(params["requestId"])
            turn = params.get("turn")
            if isinstance(turn, dict):
                safe_turn: dict[str, Any] = {}
                if "id" in turn:
                    safe_turn["id"] = identities.get("turn", turn["id"])
                if "status" in turn:
                    safe_turn["status"] = _enum(turn["status"], _CODEX_TURN_STATUSES)
                safe_params["turn"] = safe_turn
            item_record = params.get("item")
            if isinstance(item_record, dict):
                safe_item: dict[str, Any] = {}
                if "id" in item_record:
                    safe_item["id"] = identities.get("item", item_record["id"])
                if "type" in item_record:
                    safe_item["type"] = identities.get("tool", item_record["type"])
                if "status" in item_record:
                    safe_item["status"] = _enum(item_record["status"], _CODEX_ITEM_STATUSES)
                safe_params["item"] = safe_item
            for field in ("fromModel", "toModel"):
                if field in params:
                    safe_params[field] = identities.get("model", params[field])
            item["params"] = safe_params
        projected.append(item)
    return projected


def _encoded_jsonl(records: list[dict[str, Any]]) -> bytes:
    try:
        return b"".join(
            json.dumps(
                record,
                allow_nan=False,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
            for record in records
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProviderQualificationError(f"capture cannot be sanitized: {exc}") from exc


def _privacy_check(records: list[dict[str, Any]], *, where: str) -> None:
    """Accept only the closed structural vocabulary emitted by the sanitizer."""

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key not in _SAFE_KEYS and _PSEUDONYM.fullmatch(key) is None:
                    raise ProviderQualificationError(
                        f"distributable fixture {where} retains non-allowlisted field {path}/{key}"
                    )
                visit(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")
        elif isinstance(value, str):
            if value not in _SAFE_LITERAL_STRINGS and _PSEUDONYM.fullmatch(value) is None:
                raise ProviderQualificationError(
                    f"distributable fixture {where} retains non-allowlisted string at {path}"
                )
        elif value is not None and type(value) not in {bool, int}:
            raise ProviderQualificationError(
                f"distributable fixture {where} retains non-structural value at {path}"
            )

    for index, record in enumerate(records):
        visit(record, f"line-{index + 1}")


def _sanitize_bytes(raw: bytes, *, provider: str, adapter: str) -> bytes:
    provider, _ = _pair(provider, adapter)
    records = _jsonl(raw, where="provider capture")
    projected = (
        _claude_projection(records)
        if provider == "anthropic-claude"
        else _codex_projection(records)
    )
    sanitized = _encoded_jsonl(projected)
    _privacy_check(_jsonl(sanitized, where="sanitized capture"), where="sanitized capture")
    return sanitized


def sanitize_constructed_fixture(raw: bytes, *, provider: str, adapter: str) -> bytes:
    """Create safe parser-test bytes; the result carries no capture provenance."""

    return _sanitize_bytes(raw, provider=provider, adapter=adapter)


def _artifact_path(root: Path, relative: str, *, kind: str) -> Path:
    unresolved = root / relative
    resolved = unresolved.resolve()
    if resolved != root and root not in resolved.parents:
        raise ProviderQualificationError(f"{kind} escapes its root: {relative}")
    parts = PurePosixPath(relative).parts
    components = [root.joinpath(*parts[:index]) for index in range(1, len(parts) + 1)]
    if not resolved.is_file() or any(component.is_symlink() for component in components):
        raise ProviderQualificationError(f"{kind} is missing or symlinked: {relative}")
    return resolved


def _normalization_for_receipt(
    raw: bytes, receipt: dict[str, Any]
) -> ClaudeNormalization | CodexAppServerNormalization:
    identity = receipt["identity"]
    try:
        if receipt["provider"] == "anthropic-claude":
            requested_model = identity["requested_model"]
            requested_effort = identity["requested_reasoning_effort"]
            if not isinstance(requested_model, str) or not isinstance(requested_effort, str):
                raise ProviderQualificationError(
                    "Claude capture receipt lacks requested model or reasoning effort"
                )
            return normalize_claude_jsonl(
                raw,
                adapter_version=receipt["adapter_version"],
                requested_model=requested_model,
                requested_reasoning_effort=requested_effort,
            )
        return normalize_app_server_jsonl(
            raw,
            adapter_version=receipt["adapter_version"],
            requested_model=identity["requested_model"],
            requested_reasoning_effort=identity["requested_reasoning_effort"],
        )
    except (ClaudeEventError, AppServerEventError) as exc:
        raise ProviderQualificationError(
            f"raw capture cannot reproduce its provider session receipt: {exc}"
        ) from exc


def _correlate_receipt(
    receipt: dict[str, Any], normalization: ClaudeNormalization | CodexAppServerNormalization
) -> None:
    identity = receipt["identity"]
    common = {
        "provider": normalization.provider,
        "adapter": normalization.adapter,
        "adapter_version": normalization.adapter_version,
        "session_id": normalization.session_id,
        "thread_id": normalization.thread_id,
        "turn_id": normalization.turn_id,
        "raw_event_sha256": normalization.raw_event_sha256,
        "raw_event_count": normalization.raw_event_count,
        "terminal_state": normalization.terminal_state,
        "approval_requests": normalization.approval_requests,
        "approval_resolutions": normalization.approval_resolutions,
        "item_types": list(normalization.item_types),
    }
    if any(receipt[field] != expected for field, expected in common.items()):
        raise ProviderQualificationError(
            "provider session receipt does not correlate to the exact raw capture"
        )
    expected_identity = {
        "requested_model": normalization.requested_model,
        "configured_model": normalization.configured_model,
        "final_observed_model": normalization.final_observed_model,
        "requested_reasoning_effort": normalization.requested_reasoning_effort,
        "configured_reasoning_effort": normalization.configured_reasoning_effort,
    }
    if any(identity[field] != expected for field, expected in expected_identity.items()):
        raise ProviderQualificationError(
            "provider session receipt identity does not correlate to the exact raw capture"
        )
    if isinstance(normalization, ClaudeNormalization):
        expected_verification = (
            normalization.model_verification,
            normalization.reasoning_effort_verification,
            "denials_only",
        )
    else:
        expected_verification = (
            normalization.identity_verification,
            normalization.identity_verification,
            "full_lifecycle",
        )
    if (
        identity["model_verification"],
        identity["reasoning_effort_verification"],
        receipt["approval_observation"],
    ) != expected_verification:
        raise ProviderQualificationError(
            "provider session receipt verification claims do not match the raw capture"
        )
    if not set(normalization.limitations).issubset(receipt["limitations"]):
        raise ProviderQualificationError(
            "provider session receipt omits limitations observed in the raw capture"
        )


def _capture_evidence(
    root: Path,
    *,
    raw_path: str,
    receipt_path: str,
    provider: str,
    adapter: str,
    adapter_version: str,
) -> _CaptureEvidence:
    raw_file = _artifact_path(root, raw_path, kind="raw capture evidence")
    receipt_file = _artifact_path(root, receipt_path, kind="receipt evidence")
    raw = raw_file.read_bytes()
    receipt_bytes = receipt_file.read_bytes()
    try:
        receipt_value = json.loads(receipt_bytes, object_pairs_hook=_closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderQualificationError(
            f"provider session receipt is invalid JSON: {exc}"
        ) from exc
    if not isinstance(receipt_value, dict):
        raise ProviderQualificationError("provider session receipt is not a JSON object")
    # The production receipt writer emits sorted, indented JSON; historical qualification
    # fixtures use compact canonical JSON. Accept both exact encodings without rewriting either
    # artifact: provenance below continues to hash the original receipt bytes.
    if receipt_bytes not in (
        _canonical(receipt_value, where="provider session receipt"),
        json.dumps(receipt_value, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n",
    ):
        raise ProviderQualificationError("provider session receipt is not canonical JSON bytes")
    try:
        receipt = validate_provider_receipt(receipt_value)
    except ProviderReceiptError as exc:
        raise ProviderQualificationError(f"provider session receipt is invalid: {exc}") from exc
    if (
        receipt["provider"] != provider
        or receipt["adapter"] != adapter
        or receipt["adapter_version"] != adapter_version
    ):
        raise ProviderQualificationError(
            "provider session receipt identity or exact adapter version mismatch"
        )
    records = _jsonl(raw, where="raw capture evidence")
    if receipt["raw_event_sha256"] != _sha256(raw) or receipt["raw_event_count"] != len(records):
        raise ProviderQualificationError(
            "provider session receipt does not bind the exact raw event bytes and count"
        )
    normalization = _normalization_for_receipt(raw, receipt)
    _correlate_receipt(receipt, normalization)
    return _CaptureEvidence(raw, receipt, _sha256(receipt_bytes))


def sanitize_provider_capture(
    capture_evidence_root: Path | str,
    *,
    raw_path: str,
    receipt_path: str,
    provider: str,
    adapter: str,
    adapter_version: str,
) -> SanitizedCapture:
    """Derive a fixture only from a canonical receipt and its exact private raw bytes."""

    provider, adapter = _pair(provider, adapter)
    _string(adapter_version, "adapter_version", maximum=128)
    raw_path = _path(raw_path, "raw_path")
    receipt_path = _path(receipt_path, "receipt_path")
    if raw_path == receipt_path:
        raise ProviderQualificationError("raw and receipt evidence paths must differ")
    root_input = Path(capture_evidence_root)
    if root_input.is_symlink() or not root_input.is_dir():
        raise ProviderQualificationError("capture evidence root must be a physical directory")
    root = root_input.resolve()
    evidence = _capture_evidence(
        root,
        raw_path=raw_path,
        receipt_path=receipt_path,
        provider=provider,
        adapter=adapter,
        adapter_version=adapter_version,
    )
    records = _jsonl(evidence.raw, where="raw capture evidence")
    sanitized = _sanitize_bytes(evidence.raw, provider=provider, adapter=adapter)
    return SanitizedCapture(
        provider=provider,
        adapter=adapter,
        adapter_version=adapter_version,
        source_raw_sha256=_sha256(evidence.raw),
        source_raw_event_count=len(records),
        raw_evidence_path=raw_path,
        receipt_evidence_path=receipt_path,
        receipt_sha256=evidence.receipt_sha256,
        sanitizer_version=SANITIZER_VERSION,
        sanitized_fixture_sha256=_sha256(sanitized),
        sanitized=sanitized,
    )


def sanitized_capture_entry(
    capture: SanitizedCapture, *, fixture_id: str, path: str, scenario: str
) -> dict[str, Any]:
    return {
        "fixture_id": fixture_id,
        "path": path,
        "scenario": scenario,
        "origin": "sanitized_captured",
        "fixture_sha256": capture.sanitized_fixture_sha256,
        "source_raw_sha256": capture.source_raw_sha256,
        "source_raw_event_count": capture.source_raw_event_count,
        "raw_evidence_path": capture.raw_evidence_path,
        "receipt_evidence_path": capture.receipt_evidence_path,
        "receipt_sha256": capture.receipt_sha256,
        "sanitizer_version": capture.sanitizer_version,
        "sanitized_fixture_sha256": capture.sanitized_fixture_sha256,
    }


def constructed_fixture_entry(
    raw: bytes, *, fixture_id: str, path: str, scenario: str
) -> dict[str, Any]:
    return {
        "fixture_id": fixture_id,
        "path": path,
        "scenario": scenario,
        "origin": "constructed",
        "fixture_sha256": _sha256(raw),
        "source_raw_sha256": None,
        "source_raw_event_count": None,
        "raw_evidence_path": None,
        "receipt_evidence_path": None,
        "receipt_sha256": None,
        "sanitizer_version": None,
        "sanitized_fixture_sha256": None,
    }


def raw_capture_entry(raw: bytes, *, fixture_id: str, path: str, scenario: str) -> dict[str, Any]:
    """Reject raw bytes as distributable qualification evidence."""

    del raw, fixture_id, path, scenario
    raise ProviderQualificationError(
        "raw_captured is not a distributable origin; use receipt-backed deterministic sanitization"
    )


def validate_qualification_manifest(value: Any) -> dict[str, Any]:
    """Validate the closed manifest and cross-field provenance invariants."""

    if not isinstance(value, dict) or set(value) != _TOP:
        raise ProviderQualificationError("qualification manifest has missing or unknown fields")
    manifest = json.loads(_canonical(value))
    if (
        manifest["schema_version"] != "1"
        or manifest["canonical_algorithm"] != CANONICAL_ALGORITHM
        or manifest["record_kind"] != "provider_qualification_fixture_bundle"
    ):
        raise ProviderQualificationError("unsupported qualification manifest schema or kind")
    bundle_id = manifest["bundle_id"]
    if not isinstance(bundle_id, str) or _TOKEN.fullmatch(bundle_id) is None:
        raise ProviderQualificationError("bundle_id is not a canonical token")
    _pair(manifest["provider"], manifest["adapter"])
    _string(manifest["adapter_version"], "adapter_version", maximum=128)
    arguments = manifest["normalizer_arguments"]
    if not isinstance(arguments, dict) or set(arguments) != _NORMALIZER_ARGUMENTS:
        raise ProviderQualificationError("normalizer_arguments has missing or unknown fields")
    _string(arguments["requested_model"], "normalizer_arguments.requested_model", maximum=128)
    _string(
        arguments["requested_reasoning_effort"],
        "normalizer_arguments.requested_reasoning_effort",
        maximum=64,
    )
    fixtures = manifest["fixtures"]
    if not isinstance(fixtures, list) or not 1 <= len(fixtures) <= 3:
        raise ProviderQualificationError("fixtures must contain one to three entries")
    ids: set[str] = set()
    paths: set[str] = set()
    scenarios: set[str] = set()
    raw_paths: set[str] = set()
    receipt_paths: set[str] = set()
    receipt_digests: set[str] = set()
    for index, fixture in enumerate(fixtures):
        where = f"fixtures[{index}]"
        if not isinstance(fixture, dict) or set(fixture) != _FIXTURE:
            raise ProviderQualificationError(f"{where} has missing or unknown fields")
        fixture_id = fixture["fixture_id"]
        if not isinstance(fixture_id, str) or _TOKEN.fullmatch(fixture_id) is None:
            raise ProviderQualificationError(f"{where}.fixture_id is not a canonical token")
        fixture_path = _path(fixture["path"], f"{where}.path")
        scenario = fixture["scenario"]
        origin = fixture["origin"]
        if scenario not in _SCENARIOS:
            raise ProviderQualificationError(f"{where}.scenario is unsupported")
        if origin not in _ORIGINS:
            raise ProviderQualificationError(f"{where}.origin is unsupported")
        _digest(fixture["fixture_sha256"], f"{where}.fixture_sha256")
        if fixture_id in ids or fixture_path in paths or scenario in scenarios:
            raise ProviderQualificationError("fixture ids, paths, and scenarios must be unique")
        ids.add(fixture_id)
        paths.add(fixture_path)
        scenarios.add(scenario)

        provenance = (
            fixture["source_raw_sha256"],
            fixture["source_raw_event_count"],
            fixture["raw_evidence_path"],
            fixture["receipt_evidence_path"],
            fixture["receipt_sha256"],
            fixture["sanitizer_version"],
            fixture["sanitized_fixture_sha256"],
        )
        if origin == "sanitized_captured":
            _digest(fixture["source_raw_sha256"], f"{where}.source_raw_sha256")
            if (
                type(fixture["source_raw_event_count"]) is not int
                or fixture["source_raw_event_count"] < 1
            ):
                raise ProviderQualificationError(f"{where}.source_raw_event_count is invalid")
            raw_path = _path(fixture["raw_evidence_path"], f"{where}.raw_evidence_path")
            receipt_path = _path(fixture["receipt_evidence_path"], f"{where}.receipt_evidence_path")
            receipt_digest = _digest(fixture["receipt_sha256"], f"{where}.receipt_sha256")
            if raw_path == receipt_path:
                raise ProviderQualificationError(f"{where} reuses one path for raw and receipt")
            if (
                raw_path in raw_paths
                or receipt_path in receipt_paths
                or receipt_digest in receipt_digests
            ):
                raise ProviderQualificationError(
                    "captured fixtures must bind distinct raw sessions and receipts"
                )
            raw_paths.add(raw_path)
            receipt_paths.add(receipt_path)
            receipt_digests.add(receipt_digest)
            if fixture["sanitizer_version"] != SANITIZER_VERSION:
                raise ProviderQualificationError(f"{where}.sanitizer_version is unsupported")
            if (
                _digest(
                    fixture["sanitized_fixture_sha256"],
                    f"{where}.sanitized_fixture_sha256",
                )
                != fixture["fixture_sha256"]
            ):
                raise ProviderQualificationError(f"{where} sanitized fixture digests disagree")
        elif any(item is not None for item in provenance):
            raise ProviderQualificationError(f"{where} constructed provenance must be null")
    return manifest


def _replay(
    *,
    provider: str,
    raw: bytes,
    adapter_version: str,
    requested_model: str,
    requested_reasoning_effort: str,
    scenario: str,
) -> str | None:
    error: ClaudeEventError | AppServerEventError | None = None
    normalized = None
    try:
        if provider == "anthropic-claude":
            normalized = normalize_claude_jsonl(
                raw,
                adapter_version=adapter_version,
                requested_model=requested_model,
                requested_reasoning_effort=requested_reasoning_effort,
            )
        else:
            normalized = normalize_app_server_jsonl(
                raw,
                adapter_version=adapter_version,
                requested_model=requested_model,
                requested_reasoning_effort=requested_reasoning_effort,
            )
    except (ClaudeEventError, AppServerEventError) as exc:
        error = exc

    if scenario == "protocol_failure":
        if error is None:
            raise ProviderQualificationError(
                "scenario protocol_failure was accepted by the production normalizer"
            )
        return None
    if error is not None:
        raise ProviderQualificationError(
            f"scenario {scenario} was rejected by the production normalizer: {error}"
        ) from error
    assert normalized is not None
    expected = "completed" if scenario == "success" else "failed"
    if normalized.terminal_state != expected:
        raise ProviderQualificationError(
            f"scenario {scenario} normalized as {normalized.terminal_state!r}, "
            f"expected {expected!r}"
        )
    if normalized.raw_event_sha256 != _sha256(raw) or normalized.raw_event_count != len(
        raw.splitlines()
    ):
        raise ProviderQualificationError("normalizer did not bind the exact fixture bytes/count")
    return normalized.terminal_state


def _separate_roots(bundle_root: Path, evidence_root: Path) -> None:
    if (
        bundle_root == evidence_root
        or bundle_root in evidence_root.parents
        or evidence_root in bundle_root.parents
    ):
        raise ProviderQualificationError(
            "capture evidence root must be physically separate from the distributable bundle root"
        )


def validate_qualification_bundle(
    value: Any,
    bundle_root: Path | str,
    *,
    expected_provider: str,
    expected_adapter: str,
    expected_adapter_version: str,
    capture_evidence_root: Path | str | None = None,
) -> QualificationBundle:
    """Revalidate private capture evidence, sanitize it, and replay every fixture."""

    manifest = validate_qualification_manifest(value)
    expected_provider, expected_adapter = _pair(expected_provider, expected_adapter)
    if (
        manifest["provider"] != expected_provider
        or manifest["adapter"] != expected_adapter
        or manifest["adapter_version"] != expected_adapter_version
    ):
        raise ProviderQualificationError("qualification bundle identity or exact version mismatch")
    _string(expected_adapter_version, "expected_adapter_version", maximum=128)
    root_input = Path(bundle_root)
    if root_input.is_symlink() or not root_input.is_dir():
        raise ProviderQualificationError("bundle root must be a physical directory")
    root = root_input.resolve()
    captured_entries = [
        fixture for fixture in manifest["fixtures"] if fixture["origin"] == "sanitized_captured"
    ]
    evidence_root: Path | None = None
    if captured_entries:
        if capture_evidence_root is None:
            raise ProviderQualificationError(
                "captured qualification requires an explicit capture evidence root"
            )
        evidence_input = Path(capture_evidence_root)
        if evidence_input.is_symlink() or not evidence_input.is_dir():
            raise ProviderQualificationError("capture evidence root must be a physical directory")
        evidence_root = evidence_input.resolve()
        _separate_roots(root, evidence_root)

    arguments = manifest["normalizer_arguments"]
    qualified: list[QualifiedFixture] = []
    for fixture in manifest["fixtures"]:
        path = _artifact_path(root, fixture["path"], kind="fixture")
        raw = path.read_bytes()
        if _sha256(raw) != fixture["fixture_sha256"]:
            raise ProviderQualificationError(f"fixture digest changed: {fixture['path']}")
        records = _jsonl(raw, where=fixture["path"])
        _privacy_check(records, where=fixture["path"])
        sanitized = _sanitize_bytes(raw, provider=manifest["provider"], adapter=manifest["adapter"])
        if sanitized != raw:
            raise ProviderQualificationError(
                "distributable fixture is not deterministic sanitizer-normal form: "
                f"{fixture['path']}"
            )

        if fixture["origin"] == "sanitized_captured":
            assert evidence_root is not None
            evidence = _capture_evidence(
                evidence_root,
                raw_path=fixture["raw_evidence_path"],
                receipt_path=fixture["receipt_evidence_path"],
                provider=manifest["provider"],
                adapter=manifest["adapter"],
                adapter_version=manifest["adapter_version"],
            )
            identity = evidence.receipt["identity"]
            if (
                evidence.receipt_sha256 != fixture["receipt_sha256"]
                or _sha256(evidence.raw) != fixture["source_raw_sha256"]
                or len(evidence.raw.splitlines()) != fixture["source_raw_event_count"]
                or _sanitize_bytes(
                    evidence.raw,
                    provider=manifest["provider"],
                    adapter=manifest["adapter"],
                )
                != raw
            ):
                raise ProviderQualificationError(
                    f"captured fixture provenance changed: {fixture['path']}"
                )
            if (
                identity["requested_model"] != arguments["requested_model"]
                or identity["requested_reasoning_effort"] != arguments["requested_reasoning_effort"]
            ):
                raise ProviderQualificationError(
                    "normalizer arguments do not match the captured provider session receipt"
                )
            expected_terminal = "completed" if fixture["scenario"] == "success" else "failed"
            if (
                fixture["scenario"] == "protocol_failure"
                or evidence.receipt["terminal_state"] != expected_terminal
            ):
                raise ProviderQualificationError(
                    "captured scenario does not match its provider session receipt"
                )

        terminal = _replay(
            provider=manifest["provider"],
            raw=raw,
            adapter_version=manifest["adapter_version"],
            requested_model=arguments["requested_model"],
            requested_reasoning_effort=arguments["requested_reasoning_effort"],
            scenario=fixture["scenario"],
        )
        qualified.append(
            QualifiedFixture(
                fixture["fixture_id"],
                fixture["path"],
                fixture["scenario"],
                fixture["origin"],
                fixture["fixture_sha256"],
                terminal,
            )
        )

    captured_scenarios = {
        fixture.scenario for fixture in qualified if fixture.origin == "sanitized_captured"
    }
    blockers: list[str] = []
    if "success" not in captured_scenarios:
        blockers.append("captured_success_fixture_missing")
    if "provider_failure" not in captured_scenarios:
        blockers.append("captured_provider_failure_fixture_missing")
    manifest_sha256 = _sha256(_canonical(manifest))
    return QualificationBundle(
        provider=manifest["provider"],
        adapter=manifest["adapter"],
        adapter_version=manifest["adapter_version"],
        bundle_id=manifest["bundle_id"],
        manifest_sha256=manifest_sha256,
        fixtures=tuple(qualified),
        qualification_ready=not blockers,
        blockers=tuple(blockers),
    )


__all__ = [
    "CANONICAL_ALGORITHM",
    "SANITIZER_VERSION",
    "CompatibilityFixtureEvidence",
    "CompatibilityQualificationEvidence",
    "ProviderQualificationError",
    "QualificationBundle",
    "QualifiedFixture",
    "SanitizedCapture",
    "constructed_fixture_entry",
    "raw_capture_entry",
    "sanitize_constructed_fixture",
    "sanitize_provider_capture",
    "sanitized_capture_entry",
    "validate_qualification_bundle",
    "validate_qualification_manifest",
]
