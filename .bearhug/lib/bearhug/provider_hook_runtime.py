"""Provider-neutral execution boundary for adapted hooks.

Adapters own provider parsing and native custody.  This module owns the common runtime sequence:
validate custody, durably append the normalized event, run the ordered dispatcher, durably append
the aggregate result, and only then render a provider response.  A response is never returned for
an event whose runtime result was not journaled.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bearhug.hook_adapters import (
    AdaptedCodexApplyPatch,
    AdaptedLifecycleHook,
    adapt_codex_apply_patch,
    adapt_lifecycle_hook,
    render_codex_apply_patch_result,
    to_dispatch_custody,
)
from bearhug.hook_adapters._common import HookAdapterError
from bearhug.hook_dispatcher import HookDispatchError, OrderedHookDispatcher
from bearhug.normalized_hook_journal import (
    NormalizedHookJournalError,
    append_normalized_hook_record,
)
from bearhug.normalized_hooks import (
    NormalizedHookError,
    validate_normalized_hook_event,
    validate_normalized_hook_result,
)


class ProviderHookRuntimeError(RuntimeError):
    """An adapted provider hook could not complete the governed runtime sequence."""


@dataclass(frozen=True, slots=True)
class ProviderHookRun:
    """The normalized result and native response produced by one governed hook invocation."""

    event: dict[str, Any]
    result: dict[str, Any]
    provider_response: bytes
    event_journal_path: Path
    result_journal_path: Path


ProviderResponseRenderer = Callable[..., bytes]
_MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024


def _raise(stage: str, exc: Exception) -> ProviderHookRuntimeError:
    return ProviderHookRuntimeError(f"{stage} failed: {exc}")


def run_provider_hook(
    adapted: Any,
    *,
    dispatcher: OrderedHookDispatcher,
    renderer: ProviderResponseRenderer,
    journal_directory: Path | str,
    render_kwargs: Mapping[str, Any] | None = None,
) -> ProviderHookRun:
    """Run one adapted event through shared custody, evaluation, journal, and rendering.

    The dispatcher must not have its observer-style ``journal_callback`` configured.  The runtime
    boundary writes the event and result itself so journal failures remain visible and fail closed;
    combining both mechanisms would duplicate entries in the append-only journal.
    """

    if not isinstance(dispatcher, OrderedHookDispatcher):
        raise ProviderHookRuntimeError("dispatcher has the wrong type")
    if not callable(renderer):
        raise ProviderHookRuntimeError("renderer must be callable")
    if dispatcher.journal_callback_configured:
        raise ProviderHookRuntimeError(
            "dispatcher journal callback cannot be combined with the runtime journal"
        )
    if render_kwargs is not None and not isinstance(render_kwargs, Mapping):
        raise ProviderHookRuntimeError("render_kwargs must be a mapping")

    event = getattr(adapted, "event", None)
    custody = getattr(adapted, "custody", None)
    if not isinstance(event, Mapping) or custody is None:
        raise ProviderHookRuntimeError("adapted hook must contain an event and custody")

    try:
        checked_event = validate_normalized_hook_event(event)
        dispatch_custody = to_dispatch_custody(custody, checked_event)
    except (HookAdapterError, NormalizedHookError, TypeError, ValueError) as exc:
        raise _raise("adapter custody validation", exc) from exc

    try:
        event_path = append_normalized_hook_record(checked_event, journal_directory)
    except (NormalizedHookJournalError, OSError, TypeError, ValueError) as exc:
        raise _raise("normalized event journaling", exc) from exc

    try:
        result = dispatcher.dispatch(checked_event, custody=dispatch_custody)
        checked_result = validate_normalized_hook_result(result)
        if checked_result["event_id"] != checked_event["event_id"]:
            raise ProviderHookRuntimeError("dispatcher returned a result for another event")
    except ProviderHookRuntimeError:
        raise
    except (HookDispatchError, NormalizedHookError, TypeError, ValueError) as exc:
        raise _raise("hook dispatch", exc) from exc

    try:
        result_path = append_normalized_hook_record(checked_result, journal_directory)
    except (NormalizedHookJournalError, OSError, TypeError, ValueError) as exc:
        raise _raise("normalized result journaling", exc) from exc

    try:
        response = renderer(
            checked_result,
            adapted=adapted,
            **(dict(render_kwargs) if render_kwargs is not None else {}),
        )
        if (
            not isinstance(response, bytes)
            or not 1 <= len(response) <= _MAX_PROVIDER_RESPONSE_BYTES
        ):
            raise HookAdapterError(
                f"provider response must contain 1..{_MAX_PROVIDER_RESPONSE_BYTES} exact bytes"
            )
    except (HookAdapterError, TypeError, ValueError) as exc:
        raise _raise("provider response rendering", exc) from exc

    return ProviderHookRun(
        event=checked_event,
        result=checked_result,
        provider_response=response,
        event_journal_path=event_path,
        result_journal_path=result_path,
    )


def run_codex_apply_patch_hook(
    raw: bytes,
    *,
    occurred_at: datetime,
    provider_version: str,
    expected_session_id: str,
    expected_cwd: str,
    expected_worktree: str,
    expected_turn_id: str,
    expected_tool_use_id: str,
    repository: Mapping[str, Any],
    dispatcher: OrderedHookDispatcher,
    journal_directory: Path | str,
) -> ProviderHookRun:
    """Adapt, govern, journal, and render one Codex ``apply_patch`` hook event."""

    adapted: AdaptedCodexApplyPatch = adapt_codex_apply_patch(
        raw,
        occurred_at=occurred_at,
        provider_version=provider_version,
        expected_session_id=expected_session_id,
        expected_cwd=expected_cwd,
        expected_worktree=expected_worktree,
        expected_turn_id=expected_turn_id,
        expected_tool_use_id=expected_tool_use_id,
        repository=repository,
    )
    return run_provider_hook(
        adapted,
        dispatcher=dispatcher,
        renderer=render_codex_apply_patch_result,
        journal_directory=journal_directory,
    )


def run_lifecycle_hook(
    raw: bytes,
    *,
    provider: str,
    provider_version: str,
    occurred_at: datetime,
    expected_session_id: str,
    expected_cwd: str,
    repository: Mapping[str, Any],
    dispatcher: OrderedHookDispatcher,
    journal_directory: Path | str,
    renderer: ProviderResponseRenderer,
    expected_turn_id: str | None = None,
    render_kwargs: Mapping[str, Any] | None = None,
) -> ProviderHookRun:
    """Run a lifecycle event through the bridge with an explicit provider renderer.

    Lifecycle response syntax is provider- and event-specific.  The caller must supply the
    reviewed renderer instead of this boundary inventing a response for an event whose native
    blocking semantics have not been established.
    """

    adapted: AdaptedLifecycleHook = adapt_lifecycle_hook(
        raw,
        provider=provider,
        provider_version=provider_version,
        occurred_at=occurred_at,
        expected_session_id=expected_session_id,
        expected_cwd=expected_cwd,
        repository=repository,
        expected_turn_id=expected_turn_id,
    )
    return run_provider_hook(
        adapted,
        dispatcher=dispatcher,
        renderer=renderer,
        journal_directory=journal_directory,
        render_kwargs=render_kwargs,
    )


__all__ = [
    "ProviderHookRun",
    "ProviderHookRuntimeError",
    "ProviderResponseRenderer",
    "run_codex_apply_patch_hook",
    "run_lifecycle_hook",
    "run_provider_hook",
]
