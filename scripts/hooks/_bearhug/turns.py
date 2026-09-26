"""Current-turn tool calls from a Claude Code JSONL transcript.

Several Stop evaluators need the same boundary: everything after the last genuine user prompt.
Claude Code also writes tool results, hook feedback, compact summaries, and slash-command
expansions as ``type: "user"`` records. Treating one of those as a prompt moves the boundary past
the work an evaluator exists to inspect.

This is a streaming reader. It retains only tool calls in the current turn, performs no work at
import time, and deliberately does not decide what any tool call means. The write resolver and
individual evaluators own that interpretation.

JUDGED BOUNDARY (ported from the captured `codewrites.py`'s `judged_boundary`): a block already
SETTLED must not re-fire on the same write. After a Stop blocks, Claude Code re-injects the block's
own reason as a `type: "user"` entry that starts with "Stop hook feedback:" (isMeta, so
`is_real_user_message` already rejects it as a prompt). Everything AT OR BEFORE that entry was
already judged by a Stop and answered; only a write AFTER it is new. Without this, a turn that
blocked once, was allowed to continue past the re-injected feedback, and then received only an
injected `<task-notification>` would have the NEXT Stop re-scan the same turn-start window and
re-block on the identical write — a Stop already scored it.

This is an OPT-IN boundary, not a replacement for the turn-start rule: `dlv-verify-gate.py`'s
runtime port was left unchanged when the captured gate grew this boundary (parity means the two
must keep disagreeing on nothing else), so `current_turn_tool_calls` defaults to the OLD
turn-start-only window and only `review-gate` asks for the judged one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

_INJECTED_PREFIXES = (
    "<task-notification>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "[Request interrupted by user",
)

#: Not a guess — it is literally how Claude Code re-injects a Stop gate's block reason into the
#: transcript. Its presence proves a Stop already ran and the turn was allowed past it, so
#: everything at or before it was already judged and answered.
_STOP_FEEDBACK_PREFIX = "Stop hook feedback:"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool-use block and its stable transcript position."""

    line_number: int
    block_index: int
    name: object
    tool_input: object

    @property
    def position(self) -> tuple[int, int]:
        return (self.line_number, self.block_index)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def is_real_user_message(entry: object) -> bool:
    """Return true only for a genuine user prompt.

    This ports the captured ``codewrites.py`` predicate into the runtime so R08 and later
    transcript evaluators cannot drift on where a turn starts. ``isSidechain`` remains a defensive
    shape: Bear Hug recorded it in the predicate but did not observe it truthy.
    """
    if not isinstance(entry, Mapping) or entry.get("type") != "user":
        return False
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    if entry.get("isSidechain") is True:
        return False

    message = entry.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, list) and any(
        isinstance(block, Mapping) and block.get("type") == "tool_result"
        for block in content
    ):
        return False

    text = _content_text(content).strip()
    if not text:
        return False
    return not text.startswith(_INJECTED_PREFIXES)


def _is_settled_feedback(entry: object) -> bool:
    """True for the ``type: "user"`` entry Claude Code re-injects after a Stop block.

    Unlike ``is_real_user_message`` this does NOT exclude ``isMeta`` — the feedback entry IS meta,
    that is exactly why it is not a prompt, but it still marks where a Stop already judged the
    turn. A malformed or non-user entry is not feedback.
    """
    if not isinstance(entry, Mapping) or entry.get("type") != "user":
        return False
    message = entry.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    return _content_text(content).strip().startswith(_STOP_FEEDBACK_PREFIX)


def current_turn_tool_calls(
    transcript_path: object, *, respect_judged_boundary: bool = False
) -> tuple[ToolCall, ...]:
    """Stream ``transcript_path`` and return tool calls after its last real prompt.

    Malformed JSON lines are ignored, matching the captured gate. A missing or unreadable
    transcript raises: R13, not a pure evaluator or this reader, owns adaptation to D04's failure
    policy.

    ``respect_judged_boundary`` defaults to False, preserving the turn-start-only window
    `dlv-verify-gate.py` still uses (see the module docstring's JUDGED BOUNDARY section). Passing
    True additionally resets the window at the last settled "Stop hook feedback:" entry — used by
    `review-gate`, whose captured counterpart grew this rule.
    """
    calls: list[ToolCall] = []
    with open(transcript_path or "", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except ValueError:
                continue

            if is_real_user_message(entry):
                calls.clear()
                continue
            if respect_judged_boundary and _is_settled_feedback(entry):
                # In file order, this can only be reached at or after the last turn-start reset
                # above, so it is always a later (or equal) boundary — the same "idx >= start"
                # guarantee the captured `judged_boundary` checks explicitly.
                calls.clear()
                continue
            if not isinstance(entry, Mapping) or entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            if not isinstance(content, list):
                continue
            for block_index, block in enumerate(content):
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                calls.append(
                    ToolCall(
                        line_number=line_number,
                        block_index=block_index,
                        name=block.get("name", ""),
                        tool_input=block.get("input", {}) or {},
                    )
                )
    return tuple(calls)


__all__ = ["ToolCall", "current_turn_tool_calls", "is_real_user_message"]
