"""Executable Claude/Codex user-question path over the shared hook dispatcher."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bearhug.evaluators.memex_graft import build_semantic_question_input
from bearhug.hook_adapters import (
    adapt_claude_ask_user_question,
    adapt_codex_user_input_request,
    render_claude_ask_user_question_result,
    render_codex_user_input_result,
    to_dispatch_custody,
)
from bearhug.hook_adapters._common import parse_native_json
from bearhug.hook_dispatcher import EvaluatorContext, OrderedHookDispatcher
from bearhug.normalized_hooks import validate_normalized_hook_result
from bearhug.provider_hook_runtime import ProviderHookRun, run_provider_hook
from bearhug.providers.codex_user_input import CodexUserInputRequestEvidence

_MAX_CONTEXT_BYTES = 512 * 1024


class UserQuestionRuntimeError(RuntimeError):
    """The governed user-question path cannot safely continue."""


class UserQuestionBlocked(UserQuestionRuntimeError):
    """Memex or another required evaluator blocked the provider question."""


@dataclass(frozen=True, slots=True)
class ClaudeQuestionRun:
    result: dict[str, Any]
    provider_response: bytes


CodexAnswerProvider = Callable[
    [bytes, CodexUserInputRequestEvidence, tuple[bytes, ...]],
    Mapping[str, Sequence[str]],
]


class EvidenceBoundContextStore:
    """Transient evaluator context accepted only when its evidence enters the result."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, str, str], bytes] = {}

    def sink(
        self,
        event_id: str,
        evaluator_id: str,
        evidence_sha256: str,
        raw_context: bytes,
    ) -> None:
        if not isinstance(raw_context, bytes) or not raw_context:
            raise UserQuestionRuntimeError("provider context must be non-empty exact bytes")
        if len(raw_context) > _MAX_CONTEXT_BYTES:
            raise UserQuestionRuntimeError("provider context exceeds its byte bound")
        key = (event_id, evaluator_id, evidence_sha256)
        with self._lock:
            if key in self._values:
                raise UserQuestionRuntimeError("provider context key was emitted more than once")
            self._values[key] = raw_context

    def consume(self, result: Mapping[str, Any]) -> tuple[bytes, ...]:
        checked = validate_normalized_hook_result(result)
        accepted: list[bytes] = []
        with self._lock:
            for evaluation in checked["evaluations"]:
                evidence = evaluation["evidence"]
                if evidence is None:
                    continue
                key = (
                    checked["event_id"],
                    evaluation["evaluator_id"],
                    evidence["sha256"],
                )
                context = self._values.get(key)
                if context is not None:
                    accepted.append(context)
            stale = [key for key in self._values if key[0] == checked["event_id"]]
            for key in stale:
                del self._values[key]
        if sum(len(value) for value in accepted) > _MAX_CONTEXT_BYTES:
            raise UserQuestionRuntimeError("accepted provider context exceeds its total byte bound")
        return tuple(accepted)


def provider_question_reader(event: Mapping[str, Any], context: EvaluatorContext) -> bytes:
    """Project Claude/Codex transient custody into one canonical semantic question document."""

    custody = context.custody
    if custody is None:
        raise UserQuestionRuntimeError("question reader received no transient custody")
    native = parse_native_json(custody.native_input, where="provider question custody")
    provider = event["source"]["provider"]
    if provider == "anthropic-claude":
        tool_input = native.get("tool_input")
        questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    elif provider == "openai-codex":
        params = native.get("params")
        questions = params.get("questions") if isinstance(params, dict) else None
    else:  # normalized_hooks already rejects this, but keep the custody boundary closed.
        raise UserQuestionRuntimeError("unsupported question provider")
    if not isinstance(questions, list):
        raise UserQuestionRuntimeError("provider question custody has no question array")
    projected: list[tuple[str, str]] = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise UserQuestionRuntimeError(f"provider question {index} is not an object")
        header, text = question.get("header"), question.get("question")
        if not isinstance(header, str) or not isinstance(text, str):
            raise UserQuestionRuntimeError(f"provider question {index} has no header/question")
        projected.append((header, text))
    return build_semantic_question_input(tuple(projected))


def _combined_context(contexts: tuple[bytes, ...]) -> bytes | None:
    if not contexts:
        return None
    return b"\n\n".join(contexts)


class UserQuestionRuntime:
    """Join native adapters, ordered evaluators, transient context, and native rendering."""

    def __init__(
        self,
        dispatcher: OrderedHookDispatcher,
        context_store: EvidenceBoundContextStore,
        *,
        journal_directory: Path | str | None = None,
    ) -> None:
        if not isinstance(dispatcher, OrderedHookDispatcher):
            raise TypeError("dispatcher must be OrderedHookDispatcher")
        if not isinstance(context_store, EvidenceBoundContextStore):
            raise TypeError("context_store must be EvidenceBoundContextStore")
        self._dispatcher = dispatcher
        self._contexts = context_store
        self._journal_directory = journal_directory

    def handle_claude(
        self,
        raw: bytes,
        *,
        occurred_at: datetime,
        provider_version: str,
        session_id: str,
        cwd: str,
        repository: Mapping[str, Any],
    ) -> ClaudeQuestionRun:
        adapted = adapt_claude_ask_user_question(
            raw,
            occurred_at=occurred_at,
            provider_version=provider_version,
            expected_session_id=session_id,
            expected_cwd=cwd,
            repository=repository,
        )
        if self._journal_directory is None:
            result = self._dispatcher.dispatch(
                adapted.event,
                custody=to_dispatch_custody(adapted.custody, adapted.event),
            )
            contexts = self._contexts.consume(result)
            response = render_claude_ask_user_question_result(
                result,
                adapted=adapted,
                expected_session_id=session_id,
                expected_cwd=cwd,
                additional_context=_combined_context(contexts),
            )
        else:
            runtime: ProviderHookRun = run_provider_hook(
                adapted,
                dispatcher=self._dispatcher,
                journal_directory=self._journal_directory,
                renderer=lambda result, *, adapted: render_claude_ask_user_question_result(
                    result,
                    adapted=adapted,
                    expected_session_id=session_id,
                    expected_cwd=cwd,
                    additional_context=_combined_context(self._contexts.consume(result)),
                ),
            )
            result, response = runtime.result, runtime.provider_response
        return ClaudeQuestionRun(result=result, provider_response=response)

    def codex_answer_handler(
        self,
        *,
        occurred_at: Callable[[], datetime],
        session_id: str,
        repository: Mapping[str, Any],
        answer_provider: CodexAnswerProvider,
    ) -> Callable[[bytes, CodexUserInputRequestEvidence], Mapping[str, Sequence[str]]]:
        if not callable(occurred_at) or not callable(answer_provider):
            raise TypeError("occurred_at and answer_provider must be callable")

        def handle(
            raw: bytes,
            request: CodexUserInputRequestEvidence,
        ) -> Mapping[str, Sequence[str]]:
            adapted = adapt_codex_user_input_request(
                raw,
                occurred_at=occurred_at(),
                session_id=session_id,
                expected_request_id=request.request_id,
                expected_thread_id=request.thread_id,
                expected_turn_id=request.turn_id,
                expected_item_id=request.item_id,
                repository=repository,
                protocol=request.protocol,
            )
            if self._journal_directory is None:
                result = self._dispatcher.dispatch(
                    adapted.event,
                    custody=to_dispatch_custody(adapted.custody, adapted.event),
                )
                contexts = self._contexts.consume(result)
                if result["decision"] == "block":
                    codes = ",".join(item["code"] for item in result["remediation"])
                    raise UserQuestionBlocked(f"governed user question blocked: {codes}")
                answers = answer_provider(raw, request, contexts)
                # Render once so malformed or partial answers fail before transport.
                render_codex_user_input_result(result, adapted=adapted, answers=answers)
            else:
                answers_holder: dict[str, Mapping[str, Sequence[str]]] = {}

                def render_answer(result, *, adapted):
                    if result["decision"] == "block":
                        codes = ",".join(item["code"] for item in result["remediation"])
                        raise UserQuestionBlocked(f"governed user question blocked: {codes}")
                    answers_holder["answers"] = answer_provider(
                        raw,
                        request,
                        self._contexts.consume(result),
                    )
                    return render_codex_user_input_result(
                        result,
                        adapted=adapted,
                        answers=answers_holder["answers"],
                    )

                run_provider_hook(
                    adapted,
                    dispatcher=self._dispatcher,
                    journal_directory=self._journal_directory,
                    renderer=render_answer,
                )
                answers = answers_holder["answers"]
            return answers

        return handle



__all__ = [
    "ClaudeQuestionRun",
    "CodexAnswerProvider",
    "EvidenceBoundContextStore",
    "UserQuestionBlocked",
    "UserQuestionRuntime",
    "UserQuestionRuntimeError",
    "provider_question_reader",
]
