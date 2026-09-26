"""Deterministic provider-neutral hook evaluation and aggregation.

The dispatcher is deliberately below the Claude and Codex adapters.  It consumes only a
validated normalized event and a compiled ``harness-policy.v2`` declaration, runs the declared
evaluator set, and produces the sole normalized aggregate result.  Provider-native rendering is
therefore outside this module.

Evaluators run in daemon threads so an uncooperative evaluator cannot consume the hook's entire
wall-clock budget.  Evaluators are required to be pure: a timed-out Python thread cannot be
forcibly terminated and its eventual return is ignored.  Native materialization and installation
must not register evaluators which mutate state.
"""

from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import math
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bearhug.harness_policy_v2 import HarnessPolicyV2, compile_harness_policy_v2
from bearhug.normalized_hooks import (
    DECISIONS,
    NormalizedHookError,
    build_hook_evaluation,
    build_normalized_hook_result,
    validate_normalized_hook_event,
)


class HookDispatchError(RuntimeError):
    """The dispatcher cannot safely identify or execute an event policy."""


@dataclass(frozen=True, slots=True)
class EvaluatorContext:
    """Bounded execution context supplied to one provider-neutral evaluator."""

    event_id: str
    evaluator_id: str
    timeout_ms: int
    deadline_monotonic: float
    custody: TransientHookCustody | None


@dataclass(frozen=True, slots=True)
class TransientHookCustody:
    """Raw provider bytes available only for the duration of one dispatch.

    These bytes may contain prompts, questions, answers, or tool results.  They are validated
    against the digest-only event before any evaluator receives them and are never copied into a
    normalized result or journal record.
    """

    native_input: bytes
    provider_extension: bytes | None = None


@dataclass(frozen=True, slots=True)
class EvaluatorOutcome:
    """An evaluator's semantic output before dispatcher-owned identity and ordering."""

    decision: str
    evidence: bytes | None = None
    remediation: tuple[Mapping[str, Any], ...] = ()


Evaluator = Callable[[Mapping[str, Any], EvaluatorContext], EvaluatorOutcome]
JournalCallback = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class EvaluatorRegistration:
    """One content-bound evaluator implementation available to the registry."""

    evaluator_id: str
    implementation_id: str
    version: str
    content_sha256: str
    evaluate: Evaluator
    requires_custody: bool = False


@dataclass(frozen=True, slots=True)
class _Invocation:
    declaration: dict[str, Any]
    order: int
    deadline_monotonic: float
    output: queue.Queue[object]


@dataclass(frozen=True, slots=True)
class _Raised:
    category: str


_ACTIVE_DISPATCH = contextvars.ContextVar("bearhug_active_hook_dispatch", default=False)
_MAX_TOTAL_TIMEOUT_MS = 3_600_000
_MAX_EVALUATIONS = 32
_MAX_REMEDIATIONS = 32


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _failure_evidence(*, evaluator_id: str, category: str) -> bytes:
    return json.dumps(
        {"category": category, "evaluator_id": evaluator_id},
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _failure_outcome(evaluator_id: str, category: str, failure_policy: str) -> EvaluatorOutcome:
    if failure_policy == "block":
        noun = "timed out" if category == "timeout" else "failed"
        return EvaluatorOutcome(
            decision="block",
            evidence=_failure_evidence(evaluator_id=evaluator_id, category=category),
            remediation=(
                {
                    "code": f"evaluator-{category}",
                    "message": f"Evaluator {evaluator_id} {noun}; restore it and retry.",
                    "paths": [],
                },
            ),
        )
    return EvaluatorOutcome(
        decision="continue",
        evidence=_failure_evidence(evaluator_id=evaluator_id, category=category),
    )


class OrderedHookDispatcher:
    """Execute one compiled semantic event policy and emit one aggregate decision."""

    def __init__(
        self,
        *,
        policy: Mapping[str, Any] | HarnessPolicyV2,
        registrations: Iterable[EvaluatorRegistration],
        runtime_id: str,
        runtime_version: str,
        runtime_sha256: str,
        total_timeout_ms: int,
        journal_callback: JournalCallback | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = _utcnow,
    ) -> None:
        if type(total_timeout_ms) is not int or not 1 <= total_timeout_ms <= _MAX_TOTAL_TIMEOUT_MS:
            raise HookDispatchError("total_timeout_ms is outside its millisecond bounds")
        compiled = compile_harness_policy_v2(policy)
        registry: dict[str, EvaluatorRegistration] = {}
        for registration in registrations:
            if not isinstance(registration, EvaluatorRegistration):
                raise HookDispatchError("registry contains a malformed evaluator registration")
            if registration.evaluator_id in registry:
                raise HookDispatchError(f"registry repeats evaluator {registration.evaluator_id!r}")
            if not callable(registration.evaluate):
                raise HookDispatchError(f"evaluator {registration.evaluator_id!r} is not callable")
            registry[registration.evaluator_id] = registration

        self._policy = compiled
        self._registry = registry
        self._runtime_id = runtime_id
        self._runtime_version = runtime_version
        self._runtime_sha256 = runtime_sha256
        self._total_timeout_ms = total_timeout_ms
        self._journal_callback = journal_callback
        self._monotonic = monotonic
        self._utcnow = utcnow

    @property
    def journal_callback_configured(self) -> bool:
        """Whether this dispatcher has an observer callback emitting its own journal records."""

        return self._journal_callback is not None

    def _policy_for(self, semantic_event: str) -> dict[str, Any]:
        matches = [
            item
            for item in self._policy.document["event_policies"]
            if item["event"] == semantic_event
        ]
        if len(matches) != 1:
            raise HookDispatchError(f"no unique event policy is declared for {semantic_event!r}")
        return matches[0]

    @staticmethod
    def _matches(declaration: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
        families = declaration["matcher"]["tool_families"]
        if not families:
            return True
        family = event["tool"]["family"]
        return families == ["any-tool"] or family in families

    def _journal(self, record: Mapping[str, Any]) -> None:
        if self._journal_callback is None:
            return
        try:
            self._journal_callback(copy.deepcopy(record))
        except Exception:
            # Journaling is an observer.  It cannot rewrite or suppress the safety decision.
            return

    def _registration_for(self, declaration: Mapping[str, Any]) -> EvaluatorRegistration:
        evaluator_id = declaration["id"]
        registration = self._registry.get(evaluator_id)
        if registration is None:
            raise HookDispatchError(f"required evaluator {evaluator_id!r} is not registered")
        implementation = declaration["implementation"]
        actual = (
            registration.implementation_id,
            registration.version,
            registration.content_sha256,
        )
        expected = (
            implementation["id"],
            implementation["version"],
            implementation["content_sha256"],
        )
        if actual != expected:
            raise HookDispatchError(
                f"registered evaluator {evaluator_id!r} does not match policy identity"
            )
        return registration

    def _start(
        self,
        declaration: dict[str, Any],
        *,
        event: Mapping[str, Any],
        order: int,
        total_deadline: float,
        custody: TransientHookCustody | None,
    ) -> _Invocation | None:
        now = self._monotonic()
        remaining = total_deadline - now
        if remaining <= 0:
            return None
        budget_seconds = min(declaration["timeout"]["value"] / 1000, remaining)
        if budget_seconds <= 0:
            return None
        registration = self._registration_for(declaration)
        if registration.requires_custody and custody is None:
            raise HookDispatchError(
                f"required evaluator {registration.evaluator_id!r} has no transient custody"
            )
        output: queue.Queue[object] = queue.Queue(maxsize=1)
        context = EvaluatorContext(
            event_id=event["event_id"],
            evaluator_id=declaration["id"],
            timeout_ms=max(1, math.ceil(budget_seconds * 1000)),
            deadline_monotonic=now + budget_seconds,
            custody=custody if registration.requires_custody else None,
        )
        copied_event = copy.deepcopy(event)
        caller_context = contextvars.copy_context()

        def run() -> None:
            try:
                outcome = caller_context.run(registration.evaluate, copied_event, context)
            except BaseException:
                output.put(_Raised("error"))
                return
            output.put(outcome)

        thread = threading.Thread(
            target=run,
            name=f"bearhug-evaluator-{declaration['id']}",
            daemon=True,
        )
        thread.start()
        return _Invocation(
            declaration,
            order,
            context.deadline_monotonic,
            output,
        )

    def _finish(self, invocation: _Invocation, *, total_deadline: float) -> EvaluatorOutcome:
        now = self._monotonic()
        timeout = max(
            0.0,
            min(invocation.deadline_monotonic - now, total_deadline - now),
        )
        try:
            raw = invocation.output.get(timeout=timeout)
        except queue.Empty:
            try:
                raw = invocation.output.get_nowait()
            except queue.Empty:
                return _failure_outcome(
                    invocation.declaration["id"],
                    "timeout",
                    invocation.declaration["failure_policy"],
                )
        if isinstance(raw, _Raised) or not isinstance(raw, EvaluatorOutcome):
            return _failure_outcome(
                invocation.declaration["id"],
                "error",
                invocation.declaration["failure_policy"],
            )
        try:
            if raw.decision not in DECISIONS:
                raise NormalizedHookError("unsupported evaluator decision")
            # Build once with the final order to validate evidence/remediation bounds and shape.
            build_hook_evaluation(
                evaluator_id=invocation.declaration["id"],
                order=invocation.order,
                decision=raw.decision,
                evidence=raw.evidence,
                remediation=raw.remediation,
            )
        except (NormalizedHookError, TypeError, ValueError):
            return _failure_outcome(
                invocation.declaration["id"],
                "error",
                invocation.declaration["failure_policy"],
            )
        return raw

    def _event_failure_result(
        self,
        *,
        event: Mapping[str, Any],
        event_policy: Mapping[str, Any],
        category: str,
    ) -> dict[str, Any]:
        outcome = _failure_outcome("dispatcher", category, event_policy["event_failure_policy"])
        evaluation = build_hook_evaluation(
            evaluator_id="dispatcher",
            order=0,
            decision=outcome.decision,
            evidence=outcome.evidence,
            remediation=outcome.remediation,
        )
        return build_normalized_hook_result(
            event_id=event["event_id"],
            completed_at=self._utcnow(),
            runtime_id=self._runtime_id,
            runtime_version=self._runtime_version,
            runtime_sha256=self._runtime_sha256,
            evaluations=[evaluation],
        )

    @staticmethod
    def _validated_custody(
        event: Mapping[str, Any], custody: TransientHookCustody | None
    ) -> TransientHookCustody | None:
        if custody is None:
            return None
        if not isinstance(custody, TransientHookCustody):
            raise HookDispatchError("transient custody has the wrong type")
        native = custody.native_input
        if not isinstance(native, bytes):
            raise HookDispatchError("transient native input must be exact bytes")
        source = event["source"]
        if (
            len(native) != source["input_byte_count"]
            or hashlib.sha256(native).hexdigest() != source["input_sha256"]
        ):
            raise HookDispatchError("transient native input does not match normalized event")
        extension = event["provider_extension"]
        raw_extension = custody.provider_extension
        if extension is None:
            if raw_extension is not None:
                raise HookDispatchError("transient provider extension is not declared by the event")
        elif not isinstance(raw_extension, bytes) or (
            len(raw_extension) != extension["byte_count"]
            or hashlib.sha256(raw_extension).hexdigest() != extension["sha256"]
        ):
            raise HookDispatchError("transient provider extension does not match normalized event")
        return custody

    def dispatch(
        self,
        event: Mapping[str, Any],
        *,
        custody: TransientHookCustody | None = None,
    ) -> dict[str, Any]:
        """Run every matching evaluator and return the sole provider-neutral result."""

        if _ACTIVE_DISPATCH.get():
            raise HookDispatchError("recursive hook dispatch is forbidden")
        try:
            checked_event = validate_normalized_hook_event(event)
        except NormalizedHookError as exc:
            raise HookDispatchError(f"invalid normalized hook event: {exc}") from exc
        checked_custody = self._validated_custody(checked_event, custody)
        event_policy = self._policy_for(checked_event["semantic_event"])
        token = _ACTIVE_DISPATCH.set(True)
        try:
            self._journal(checked_event)
            declarations = [
                item for item in event_policy["evaluators"] if self._matches(item, checked_event)
            ]
            mode = event_policy["execution"]["mode"]
            if checked_event["semantic_event"] == "post-tool-use" and mode != "ordered":
                result = self._event_failure_result(
                    event=checked_event,
                    event_policy=event_policy,
                    category="unsafe-concurrency",
                )
                self._journal(result)
                return result
            if len(declarations) > _MAX_EVALUATIONS:
                result = self._event_failure_result(
                    event=checked_event,
                    event_policy=event_policy,
                    category="evaluator-overflow",
                )
                self._journal(result)
                return result
            if not declarations:
                evaluation = build_hook_evaluation(
                    evaluator_id="dispatcher",
                    order=0,
                    decision="continue",
                    evidence=_failure_evidence(evaluator_id="dispatcher", category="no-match"),
                )
                result = build_normalized_hook_result(
                    event_id=checked_event["event_id"],
                    completed_at=self._utcnow(),
                    runtime_id=self._runtime_id,
                    runtime_version=self._runtime_version,
                    runtime_sha256=self._runtime_sha256,
                    evaluations=[evaluation],
                )
                self._journal(result)
                return result

            if mode == "ordered":
                ordered = [(item, item["order"]) for item in declarations]
            else:
                # Approved concurrency has no policy order.  Evaluator id provides only a stable
                # aggregate serialization order; it does not claim a runtime execution order.
                ordered = [(item, index) for index, item in enumerate(declarations)]

            total_deadline = self._monotonic() + self._total_timeout_ms / 1000
            outcomes: list[tuple[dict[str, Any], int, EvaluatorOutcome]] = []
            try:
                if mode == "ordered":
                    for declaration, order in ordered:
                        invocation = self._start(
                            declaration,
                            event=checked_event,
                            order=order,
                            total_deadline=total_deadline,
                            custody=checked_custody,
                        )
                        outcome = (
                            _failure_outcome(
                                declaration["id"],
                                "timeout",
                                declaration["failure_policy"],
                            )
                            if invocation is None
                            else self._finish(invocation, total_deadline=total_deadline)
                        )
                        outcomes.append((declaration, order, outcome))
                else:
                    invocations = [
                        (
                            declaration,
                            order,
                            self._start(
                                declaration,
                                event=checked_event,
                                order=order,
                                total_deadline=total_deadline,
                                custody=checked_custody,
                            ),
                        )
                        for declaration, order in ordered
                    ]
                    for declaration, order, invocation in invocations:
                        outcome = (
                            _failure_outcome(
                                declaration["id"],
                                "timeout",
                                declaration["failure_policy"],
                            )
                            if invocation is None
                            else self._finish(invocation, total_deadline=total_deadline)
                        )
                        outcomes.append((declaration, order, outcome))
            except HookDispatchError:
                result = self._event_failure_result(
                    event=checked_event,
                    event_policy=event_policy,
                    category="registry-error",
                )
                self._journal(result)
                return result

            remediation_count = sum(
                len(outcome.remediation)
                for _, _, outcome in outcomes
                if outcome.decision == "block"
            )
            if remediation_count > _MAX_REMEDIATIONS:
                result = self._event_failure_result(
                    event=checked_event,
                    event_policy=event_policy,
                    category="remediation-overflow",
                )
                self._journal(result)
                return result

            evaluations = [
                build_hook_evaluation(
                    evaluator_id=declaration["id"],
                    order=order,
                    decision=outcome.decision,
                    evidence=outcome.evidence,
                    remediation=outcome.remediation,
                )
                for declaration, order, outcome in outcomes
            ]
            result = build_normalized_hook_result(
                event_id=checked_event["event_id"],
                completed_at=self._utcnow(),
                runtime_id=self._runtime_id,
                runtime_version=self._runtime_version,
                runtime_sha256=self._runtime_sha256,
                evaluations=evaluations,
            )
            self._journal(result)
            return result
        finally:
            _ACTIVE_DISPATCH.reset(token)


__all__ = [
    "EvaluatorContext",
    "EvaluatorOutcome",
    "EvaluatorRegistration",
    "HookDispatchError",
    "OrderedHookDispatcher",
    "TransientHookCustody",
]
