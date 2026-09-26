"""Evidence adapter for Codex App Server's stable stdio JSONL protocol.

The App Server is the rich-client surface: unlike ``codex exec --json`` it reports the configured
model, reasoning effort, sandbox and approval policy and streams item/approval/turn lifecycle.
Those are provider observations, but the configured model is explicitly not per-turn execution
telemetry.  The adapter therefore records it and any observed reroute without making the stronger
promotion claim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bearhug.providers.codex import _closed_object


class AppServerEventError(ValueError):
    """The App Server stream is malformed or cannot support an unambiguous receipt."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class CodexAppServerContract:
    """Immutable requested facts for one local App Server thread and turn."""

    cwd: str
    prompt_sha256: str
    adapter_version: str
    requested_model: str | None = None
    requested_reasoning_effort: str | None = None
    sandbox: str = "read-only"
    approval_policy: str = "never"
    service_name: str = "bear_hug"
    writable_roots: tuple[str, ...] = ()
    codex_home: str | None = None

    def __post_init__(self) -> None:
        if not Path(self.cwd).is_absolute():
            raise ValueError("cwd must be absolute")
        _require_sha256(self.prompt_sha256, "prompt_sha256")
        if self.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("App Server adapter permits only read-only or workspace-write")
        if self.writable_roots and self.sandbox != "workspace-write":
            raise ValueError("additional write roots require workspace-write")
        if any(not Path(root).is_absolute() for root in self.writable_roots):
            raise ValueError("additional write roots must be absolute")
        if self.codex_home is not None and not Path(self.codex_home).is_absolute():
            raise ValueError("codex_home must be absolute when supplied")
        if self.approval_policy not in {"never", "on-request", "untrusted"}:
            raise ValueError("unsupported approval policy")
        if not self.adapter_version:
            raise ValueError("adapter_version must not be empty")
        if not self.service_name:
            raise ValueError("service_name must not be empty")


def build_app_server_command(contract: CodexAppServerContract) -> tuple[str, ...]:
    """Return the stable local transport; WebSocket remains experimental and is not used."""

    del contract  # the command is intentionally independent of turn content and policy
    # Disable hooks, project instructions, plugins, skills, apps, and MCP before App Server reads
    # the target. The capsule's protocol is the sole automation authority for this child process.
    return (
        "codex",
        "-c",
        "features.hooks=false",
        "-c",
        "features.plugins=false",
        "-c",
        "features.remote_plugin=false",
        "-c",
        "features.apps=false",
        "-c",
        "features.skill_search=false",
        "-c",
        "features.skip_host_skill_discovery=true",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "mcp_servers={}",
        "app-server",
        "--listen",
        "stdio://",
    )


def build_initialize_messages(
    contract: CodexAppServerContract,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build initialization and thread creation messages with fixed correlation ids."""

    initialize = {
        "method": "initialize",
        "id": 0,
        "params": {
            "clientInfo": {
                "name": "bear_hug",
                "title": "Bear Hug",
                "version": contract.adapter_version,
            }
        },
    }
    initialized = {"method": "initialized", "params": {}}
    thread_params: dict[str, Any] = {
        "approvalPolicy": contract.approval_policy,
        "cwd": contract.cwd,
        # ThreadStartParams uses SandboxMode strings; the returned SandboxPolicy object uses
        # camel-case type names. Keep those two wire representations distinct.
        "sandbox": contract.sandbox,
        "serviceName": contract.service_name,
    }
    if contract.requested_model is not None:
        thread_params["model"] = contract.requested_model
    thread_start = {"method": "thread/start", "id": 1, "params": thread_params}
    return initialize, initialized, thread_start


def build_turn_start_message(
    thread_id: str,
    prompt: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    output_schema: dict[str, Any] | None = None,
    writable_roots: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build one turn request after ``thread/start`` returns the provider thread id."""

    if not thread_id:
        raise ValueError("thread_id must not be empty")
    if not prompt:
        raise ValueError("prompt must not be empty")
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
    }
    if model is not None:
        params["model"] = model
    if reasoning_effort is not None:
        params["effort"] = reasoning_effort
    if writable_roots:
        params["sandboxPolicy"] = {
            "type": "workspaceWrite", "writableRoots": list(writable_roots),
            "networkAccess": False,
        }
    if output_schema is not None:
        params["outputSchema"] = output_schema
    return {"method": "turn/start", "id": 2, "params": params}


@dataclass(frozen=True, slots=True)
class NormalizedAppServerEvent:
    sequence: int
    kind: str
    raw_line_sha256: str
    item_type: str | None = None
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class CodexAppServerNormalization:
    provider: str
    adapter: str
    adapter_version: str
    session_id: str
    thread_id: str
    turn_id: str
    raw_event_sha256: str
    raw_event_count: int
    terminal_state: str
    requested_model: str | None
    configured_model: str
    final_observed_model: str
    requested_reasoning_effort: str | None
    configured_reasoning_effort: str | None
    identity_verification: str
    promotion_identity_eligible: bool
    approval_requests: int
    approval_resolutions: int
    item_types: tuple[str, ...]
    events: tuple[NormalizedAppServerEvent, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_lines(raw: bytes) -> list[tuple[dict[str, Any], bytes]]:
    if not raw or not raw.endswith(b"\n"):
        raise AppServerEventError("event stream must be non-empty newline-terminated JSONL")
    parsed: list[tuple[dict[str, Any], bytes]] = []
    for ordinal, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise AppServerEventError(f"blank JSONL record at line {ordinal}")
        try:
            message = json.loads(line, object_pairs_hook=_closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise AppServerEventError(f"invalid JSONL record at line {ordinal}: {exc}") from exc
        if not isinstance(message, dict):
            raise AppServerEventError(f"line {ordinal} is not a JSON object")
        parsed.append((message, line))
    return parsed


def _response(messages: list[tuple[dict[str, Any], bytes]], request_id: int) -> dict[str, Any]:
    matches = [message for message, _ in messages if message.get("id") == request_id]
    if len(matches) != 1:
        raise AppServerEventError(
            f"expected one response for request id {request_id}, observed {len(matches)}"
        )
    message = matches[0]
    if "error" in message:
        raise AppServerEventError(f"request id {request_id} failed: {message['error']!r}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise AppServerEventError(f"request id {request_id} has no result object")
    return result


def _thread_id_of(params: Any) -> str | None:
    if not isinstance(params, dict):
        return None
    value = params.get("threadId")
    return value if isinstance(value, str) and value else None


def normalize_app_server_jsonl(
    raw: bytes,
    *,
    adapter_version: str,
    requested_model: str | None = None,
    requested_reasoning_effort: str | None = None,
    expected_cwd: str | None = None,
    expected_sandbox: str | None = None,
    expected_approval_policy: str | None = None,
) -> CodexAppServerNormalization:
    """Normalize one App Server connection containing exactly one thread and one turn.

    Unknown notification methods are retained by kind and line digest.  Known lifecycle is strict:
    correlation ids must agree, items must finish in a terminal state, approval resolutions must
    name a request observed on this connection, and no turn event may follow completion.
    """

    messages = _parse_lines(raw)
    _response(messages, 0)
    thread_result = _response(messages, 1)
    _response(messages, 2)

    thread = thread_result.get("thread")
    if not isinstance(thread, dict):
        raise AppServerEventError("thread/start response has no thread object")
    thread_id = thread.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        raise AppServerEventError("thread/start response has no thread id")
    session_id = thread.get("sessionId", thread_id)
    if not isinstance(session_id, str) or not session_id:
        raise AppServerEventError("thread/start response has no session id")
    configured_model = thread_result.get("model")
    if not isinstance(configured_model, str) or not configured_model:
        raise AppServerEventError("thread/start response has no configured model")
    configured_effort = thread_result.get("reasoningEffort")
    if configured_effort is not None and not isinstance(configured_effort, str):
        raise AppServerEventError("thread/start reasoning effort is not a string or null")
    if expected_cwd is not None and thread_result.get("cwd") != expected_cwd:
        raise AppServerEventError("thread/start response cwd does not match the launch contract")
    if expected_approval_policy is not None and thread_result.get(
        "approvalPolicy"
    ) != expected_approval_policy:
        raise AppServerEventError(
            "thread/start response approval policy does not match the launch contract"
        )
    if expected_sandbox is not None:
        expected_type = "readOnly" if expected_sandbox == "read-only" else "workspaceWrite"
        sandbox = thread_result.get("sandbox")
        if not isinstance(sandbox, dict) or sandbox.get("type") != expected_type:
            raise AppServerEventError(
                "thread/start response sandbox does not match the launch contract"
            )

    turn_id = ""
    terminal_state = "incomplete"
    terminal_seen = False
    final_model = configured_model
    approval_ids: set[str] = set()
    resolved_ids: set[str] = set()
    started_items: dict[str, str] = {}
    completed_items: set[str] = set()
    item_types: list[str] = []
    events: list[NormalizedAppServerEvent] = []

    for sequence, (message, line) in enumerate(messages):
        method = message.get("method")
        kind = method if isinstance(method, str) else f"response/{message.get('id', 'unknown')}"
        params = message.get("params")
        correlated = _thread_id_of(params)
        if correlated is not None and correlated != thread_id:
            raise AppServerEventError(
                f"event {sequence} thread id {correlated!r} does not match {thread_id!r}"
            )
        if terminal_seen and isinstance(method, str) and method.startswith(("turn/", "item/")):
            raise AppServerEventError("turn or item event appears after terminal turn event")

        item_type = item_id = None
        if isinstance(params, dict):
            item = params.get("item")
            if isinstance(item, dict):
                raw_type, raw_id = item.get("type"), item.get("id")
                item_type = raw_type if isinstance(raw_type, str) and raw_type else None
                item_id = raw_id if isinstance(raw_id, str) and raw_id else None
                if item_type is None or item_id is None:
                    raise AppServerEventError(f"event {sequence} item has no string type/id")

        if method == "turn/started":
            turn = params.get("turn") if isinstance(params, dict) else None
            observed_turn = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(observed_turn, str) or not observed_turn:
                raise AppServerEventError("turn/started has no turn id")
            if turn_id and turn_id != observed_turn:
                raise AppServerEventError("stream contains multiple turn ids")
            turn_id = observed_turn
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params, dict) else None
            observed_turn = turn.get("id") if isinstance(turn, dict) else None
            status = turn.get("status") if isinstance(turn, dict) else None
            if not turn_id or observed_turn != turn_id:
                raise AppServerEventError("terminal turn id does not match turn/started")
            if status not in {"completed", "failed", "interrupted"}:
                raise AppServerEventError("terminal turn/completed has a non-terminal status")
            terminal_state = "completed" if status == "completed" else "failed"
            terminal_seen = True
        elif method == "item/started":
            assert item_id is not None and item_type is not None
            if item_id in started_items:
                raise AppServerEventError(f"item {item_id!r} started more than once")
            started_items[item_id] = item_type
            if item_type not in item_types:
                item_types.append(item_type)
        elif method == "item/completed":
            assert item_id is not None and item_type is not None
            if started_items.get(item_id) != item_type:
                raise AppServerEventError(f"item {item_id!r} completed without matching start")
            status = params["item"].get("status")
            if status is not None and status not in {"completed", "failed", "declined"}:
                raise AppServerEventError(f"terminal item {item_id!r} has status {status!r}")
            completed_items.add(item_id)
        elif isinstance(method, str) and method.endswith("/requestApproval"):
            request_id = message.get("id")
            if not isinstance(request_id, (str, int)):
                raise AppServerEventError("approval request has no correlation id")
            approval_ids.add(str(request_id))
        elif method == "serverRequest/resolved":
            request_id = params.get("requestId") if isinstance(params, dict) else None
            if not isinstance(request_id, (str, int)) or str(request_id) not in approval_ids:
                raise AppServerEventError("approval resolution names no observed request")
            resolved_ids.add(str(request_id))
        elif method == "model/rerouted":
            if not isinstance(params, dict):
                raise AppServerEventError("model/rerouted has no params")
            from_model, to_model = params.get("fromModel"), params.get("toModel")
            if from_model != final_model or not isinstance(to_model, str) or not to_model:
                raise AppServerEventError("model reroute does not form a continuous identity chain")
            reroute_turn = params.get("turnId")
            if turn_id and reroute_turn != turn_id:
                raise AppServerEventError("model reroute turn id does not match active turn")
            final_model = to_model

        events.append(
            NormalizedAppServerEvent(sequence, kind, _sha256(line), item_type, item_id)
        )

    if not terminal_seen:
        raise AppServerEventError("stream has no terminal turn/completed event")
    unfinished = set(started_items) - completed_items
    if unfinished:
        raise AppServerEventError(f"stream has unfinished item(s): {', '.join(sorted(unfinished))}")

    limitations = (
        "configured_identity_is_not_per_turn_execution_attestation",
        "model_reroute_notifications_are_observed_but_silent_provider_routing_is_not_excluded",
        "candidate_git_identity_requires_external_verifier",
        "raw_stream_contains_server_messages_only_client_request_digest_required_separately",
    )
    return CodexAppServerNormalization(
        provider="openai-codex",
        adapter="codex-app-server-stdio",
        adapter_version=adapter_version,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=turn_id,
        raw_event_sha256=_sha256(raw),
        raw_event_count=len(messages),
        terminal_state=terminal_state,
        requested_model=requested_model,
        configured_model=configured_model,
        final_observed_model=final_model,
        requested_reasoning_effort=requested_reasoning_effort,
        configured_reasoning_effort=configured_effort,
        identity_verification="provider_observed",
        promotion_identity_eligible=False,
        approval_requests=len(approval_ids),
        approval_resolutions=len(resolved_ids),
        item_types=tuple(item_types),
        events=tuple(events),
        limitations=limitations,
    )


__all__ = [
    "AppServerEventError",
    "CodexAppServerContract",
    "CodexAppServerNormalization",
    "NormalizedAppServerEvent",
    "build_app_server_command",
    "build_initialize_messages",
    "build_turn_start_message",
    "normalize_app_server_jsonl",
]
