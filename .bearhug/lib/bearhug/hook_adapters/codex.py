"""Codex App Server ``item/tool/requestUserInput`` normalized boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bearhug.hook_adapters._common import (
    AdapterCustody,
    HookAdapterError,
    UnsupportedProviderDecision,
    canonical_json,
    identifier,
    verify_adapter_custody,
)
from bearhug.normalized_hooks import (
    build_normalized_hook_event,
    validate_normalized_hook_event,
    validate_normalized_hook_result,
)
from bearhug.providers.codex_user_input import (
    EXACT_PROTOCOL_IDENTITY,
    CodexUserInputProtocolIdentity,
    CodexUserInputRequestEvidence,
    build_raw_user_input_response,
    validate_raw_user_input_request,
)

CODEX_PROVIDER = "openai-codex"
CODEX_ADAPTER = "codex-app-server-stdio"


@dataclass(frozen=True, slots=True)
class AdaptedCodexUserInput:
    """Normalized event plus the prose-free request correlation needed for a response."""

    event: dict[str, Any]
    request: CodexUserInputRequestEvidence
    custody: AdapterCustody


def adapt_codex_user_input_request(
    raw: bytes,
    *,
    occurred_at: datetime,
    session_id: str,
    expected_request_id: str | int,
    expected_thread_id: str,
    expected_turn_id: str,
    expected_item_id: str,
    repository: Mapping[str, Any],
    protocol: CodexUserInputProtocolIdentity = EXACT_PROTOCOL_IDENTITY,
) -> AdaptedCodexUserInput:
    """Convert the sealed exact-version App Server request to one substitute event."""

    session_id = identifier(session_id, where="session_id")
    request = validate_raw_user_input_request(
        raw,
        expected_request_id=expected_request_id,
        expected_thread_id=expected_thread_id,
        expected_turn_id=expected_turn_id,
        expected_item_id=expected_item_id,
        protocol=protocol,
    )
    extension = canonical_json(
        {
            "auto_resolution_ms": request.auto_resolution_ms,
            "is_blocking": request.is_blocking,
            "option_counts": [question.option_count for question in request.questions],
            "question_count": len(request.questions),
            "secret_questions": sum(question.is_secret for question in request.questions),
        }
    )
    event = build_normalized_hook_event(
        occurred_at=occurred_at,
        semantic_event="pre-tool-use",
        scope="turn",
        provider=CODEX_PROVIDER,
        adapter=CODEX_ADAPTER,
        adapter_version=protocol.provider_version,
        native_event=protocol.request_method,
        evidence_kind="provider-substitute",
        native_input=raw,
        session_id=session_id,
        thread_id=request.thread_id,
        turn_id=request.turn_id,
        tool_call_id=request.item_id,
        repository=dict(repository),
        tool={
            "family": "user-interaction",
            "operation": "request-user-input",
            "status": "intended",
            "effects": [],
        },
        provider_extension=extension,
    )
    return AdaptedCodexUserInput(
        event=event,
        request=request,
        custody=AdapterCustody(
            provider=CODEX_PROVIDER,
            native_event=protocol.request_method,
            raw_native_input=raw,
            raw_provider_extension=extension,
        ),
    )


def render_codex_user_input_result(
    result: Mapping[str, Any],
    *,
    adapted: AdaptedCodexUserInput,
    answers: Mapping[str, Sequence[str]] | None = None,
    protocol: CodexUserInputProtocolIdentity = EXACT_PROTOCOL_IDENTITY,
) -> bytes:
    """Render an aggregate decision as the exact-version JSON-RPC response.

    App Server defines no deny/error result for ``item/tool/requestUserInput``. A block is
    therefore unrepresentable and fails closed here; the owning runner must terminate or cancel
    the turn through a separately sealed App Server contract instead of inventing answers.
    """

    if not isinstance(adapted, AdaptedCodexUserInput):
        raise HookAdapterError("adapted request has the wrong type")
    event = validate_normalized_hook_event(adapted.event)
    verify_adapter_custody(adapted.custody, event)
    result = validate_normalized_hook_result(result)
    if (
        event["source"]["provider"] != CODEX_PROVIDER
        or event["source"]["adapter"] != CODEX_ADAPTER
        or event["source"]["adapter_version"] != protocol.provider_version
        or event["source"]["native_event"] != protocol.request_method
        or event["source"]["evidence_kind"] != "provider-substitute"
        or event["tool"]["family"] != "user-interaction"
        or event["tool"]["operation"] != "request-user-input"
    ):
        raise HookAdapterError("event is not a Codex request_user_input event")
    request = adapted.request
    if request.protocol != protocol:
        raise HookAdapterError("request evidence does not match the selected Codex protocol")
    if (
        event["source"]["input_sha256"] != request.raw_sha256
        or event["source"]["input_byte_count"] != request.raw_byte_count
        or event["identity"]["thread_id"] != request.thread_id
        or event["identity"]["turn_id"] != request.turn_id
        or event["identity"]["tool_call_id"] != request.item_id
    ):
        raise HookAdapterError("Codex request evidence does not match normalized event custody")
    if result["event_id"] != event["event_id"]:
        raise HookAdapterError("result does not belong to the supplied event")
    if result["decision"] == "block":
        raise UnsupportedProviderDecision(
            "Codex request_user_input has no native block response; turn cancellation is required"
        )
    if answers is None:
        raise HookAdapterError("Codex request_user_input requires complete answers")
    return build_raw_user_input_response(request, answers, protocol=protocol)


__all__ = [
    "AdaptedCodexUserInput",
    "CODEX_ADAPTER",
    "CODEX_PROVIDER",
    "adapt_codex_user_input_request",
    "render_codex_user_input_result",
]
