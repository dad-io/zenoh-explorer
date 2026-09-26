"""4.1 — a streaming reader over Claude Code session transcripts.

The corpus is 313 MB across ~83 files, the largest 22.6 MB. Nothing here materialises a
transcript: every entry point is a generator over one line at a time.

The normalisation is deliberately thin. This reader says what the record *is* — a human turn, a
tool call, a gate that blocked — and nothing about what it means. Every judgement lives in a
check that can be tested against a fixture, because three ad-hoc scans this session each
returned a confident uniform zero by guessing at this structure instead of pinning it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Hook attachment types, kept distinct. Lumping them together hides the two that matter:
#: a hook that ERRORED without blocking is the silent-failure class (response-shape's stamp
#: raised NameError into a bare except and the gate became invisible), and a CANCELLED hook is
#: a timeout. Both read as "passed" if you only look for hook_blocking_error.
HOOK_KINDS = {
    "hook_blocking_error": "hook_block",
    "hook_non_blocking_error": "hook_error",
    "hook_cancelled": "hook_cancelled",
    "hook_additional_context": "hook_inject",
    "hook_success": "hook_pass",
}

#: Lines longer than this are truncated before parsing. A pasted binary blob is not evidence,
#: and one 4 MB line would defeat the point of streaming.
MAX_LINE = 400_000


@dataclass(frozen=True, slots=True)
class Event:
    """One normalised transcript record."""

    kind: str  # user | subagent_prompt | system_user | assistant | tool_use | hook_* | other
    timestamp: str
    session: str
    text: str = ""
    name: str = ""  # tool name, or hook event name
    payload: dict[str, Any] = field(default_factory=dict)
    #: The assistant message this event belongs to (`message.id`, else the record `uuid`).
    #: Claude Code writes one tool_use block per record and splits a parallel batch across
    #: records sharing one message id (2,155 such spans in the frozen corpus), so this — not
    #: the record and not the timestamp — is what "issued together" means.
    message_id: str = ""


#: Text a `type: user` record opens with when the HARNESS wrote it, not a person. Mirrors
#: `bearhug_runtime.turns._INJECTED_PREFIXES` (the captured gates' predicate); the parity test in
#: tests/test_replay_transcript.py pins the two together.
INJECTED_PREFIXES = (
    "<task-notification>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "[Request interrupted by user",
)


def _human_text(message: Any) -> str | None:
    """The text of a user-role message, or None if this is a tool_result.

    Claude Code delivers tool results as user-role messages. Treating one as a prompt is the
    mistake `response-shape.py` and `dlv-verify-gate.py` both had to correct for.
    """
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text":
                text = block.get("text", "")
                if not isinstance(text, str):
                    return None
                parts.append(text)
        return "\n".join(parts)
    return None


def is_genuine_user_record(record: dict[str, Any]) -> bool:
    """A prompt a person typed — the boundary every turn-scoped rule is measured against.

    Re-implements the captured gates' predicate (ported into the runtime as
    `turns.is_real_user_message`) rather than importing it, so the lab stays stdlib-only; the
    parity test keeps the two from drifting. Measured on the frozen corpus while building M08:
    of 2,027 user records the earlier reader called human, 196 were `isMeta` and 362 opened with
    a harness tag — a quarter of every ledger denominator was a turn nobody started.
    """
    if not isinstance(record, dict) or record.get("type") != "user":
        return False
    if record.get("isMeta") or record.get("isCompactSummary"):
        return False
    if record.get("isSidechain") is True:
        return False
    text = _human_text(record.get("message") or {})
    if text is None or not text.strip():
        return False
    return not text.strip().startswith(INJECTED_PREFIXES)


def is_subagent_prompt_record(record: dict[str, Any]) -> bool:
    """The parent's dispatch brief at the top of a subagent transcript, or a later prompt in one.

    Every record in a subagent transcript is `isSidechain: true` (209 of 209 frozen files; no
    main-session record is). The gates' predicate excludes sidechain records because a Stop hook
    never runs against one; the lab measures subagent work too (roadmap 4.12), so the brief opens
    a turn here — as `subagent_prompt`, never as a human.
    """
    if not isinstance(record, dict) or record.get("type") != "user":
        return False
    if record.get("isSidechain") is not True:
        return False
    if record.get("isMeta") or record.get("isCompactSummary"):
        return False
    text = _human_text(record.get("message") or {})
    if text is None or not text.strip():
        return False
    return not text.strip().startswith(INJECTED_PREFIXES)


#: Event kinds that open a turn. Both are prompts; only the first is a person.
TURN_OPENERS = ("user", "subagent_prompt")


def _system_user_label(record: dict[str, Any], text: str) -> str:
    """Why a non-genuine, non-tool_result user record is not a prompt — for the `name` field."""
    if record.get("isCompactSummary"):
        return "compact-summary"
    if record.get("isMeta"):
        return "meta"
    stripped = text.strip()
    for prefix in INJECTED_PREFIXES:
        if stripped.startswith(prefix):
            return prefix.strip("<>[").split(" ")[0].lower()
    return "unclassified"


def session_transcripts(project_dir: Path | str) -> list[Path]:
    """Every transcript for a project — top-level sessions AND per-session subagents.

    Subagent transcripts live at ``<project>/<session-id>/subagents/agent-*.jsonl``. A glob of
    ``*.jsonl`` finds none of them: for barracuda that is 109 files and 44 MB, carrying 3,024
    tool calls, silently absent from any count that only walks the top level.
    """
    project_dir = Path(project_dir)
    top = sorted(project_dir.glob("*.jsonl"))
    subagents = sorted(project_dir.glob("*/subagents/*.jsonl"))
    return top + subagents


@dataclass(slots=True)
class RecordStats:
    """What `iter_records` skipped, for readers that report it (the field census)."""

    overlong_lines: int = 0
    malformed_lines: int = 0
    non_object_lines: int = 0
    #: Records whose `uuid` already appeared in the same file. Claude Code re-appends a record
    #: under a new `parentUuid` when a session is rewound or resumed; the frozen corpus holds
    #: 1,180 such repeats in 6 files (1,140 hook attachments, 35 user, 5 assistant). The same
    #: event twice is not two events, so the second copy is dropped here, once, for every reader.
    duplicate_records: int = 0
    #: Valid JSON objects whose nested fields cannot be normalized into an Event.
    malformed_records: int = 0


def iter_records(
    path: Path | str, *, stats: RecordStats | None = None
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Stream one transcript's JSON object records as ``(line_number, record)``.

    The single record loop every reader shares: line truncation, JSON parsing, the object check,
    and uuid de-duplication happen here and nowhere else, so two readers cannot disagree about
    what a record is. A record without a `uuid` (24,864 in the frozen corpus — ai-title,
    last-prompt, mode ...) is never collapsed; there is nothing to say two of them are one.
    """
    path = Path(path)
    stats = stats if stats is not None else RecordStats()
    try:
        handle = path.open("r", errors="replace")
    except OSError:
        # A live session can delete a subagent transcript mid-scan. Missing is not malformed.
        return
    seen: set[str] = set()
    with handle:
        for line_number, line in enumerate(handle, 1):
            if len(line) > MAX_LINE:
                stats.overlong_lines += 1
                line = line[:MAX_LINE]
            try:
                record = json.loads(line)
            except ValueError:
                stats.malformed_lines += 1
                continue
            if not isinstance(record, dict):
                stats.non_object_lines += 1
                continue
            uuid = record.get("uuid")
            if isinstance(uuid, str) and uuid:
                if uuid in seen:
                    stats.duplicate_records += 1
                    continue
                seen.add(uuid)
            yield line_number, record


def iter_events(path: Path | str, *, stats: RecordStats | None = None) -> Iterator[Event]:
    """Stream one transcript as normalised events, skipping and counting malformed records."""
    for _, record in iter_records(path, stats=stats):
        yield from _events_of(record, stats=stats)


def _blocking_message(attachment: dict[str, Any]) -> str:
    """The gate's own words.

    `blockingError` is nested inside itself — the outer value is a mapping whose own
    `blockingError` key holds the text. Stringifying the outer value yields a Python repr that
    every signature match then has to see through, so unwrap it once, here.
    """
    value = attachment.get("blockingError", "")
    if isinstance(value, dict):
        value = value.get("blockingError", value)
    return value if isinstance(value, str) else str(value)


def _injected_text(attachment: dict[str, Any]) -> str:
    """The text a `hook_additional_context` record actually put in front of the model.

    One `SessionStart` (or other) moment can run several hooks, and Claude Code batches their
    output into a single record: `content` is a LIST, one string per hook that had something to
    say. Joined here with a blank line between them so a byte count of `Event.text` is the true
    size of what that one moment injected, not of one hook's slice of it.
    """
    content = attachment.get("content", "")
    if isinstance(content, list):
        return "\n\n".join(part for part in content if isinstance(part, str))
    return content if isinstance(content, str) else str(content)


def _reject_malformed(stats: RecordStats | None) -> None:
    if stats is not None:
        stats.malformed_records += 1


def _valid_content_blocks(content: Any) -> bool:
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            return False
        if block.get("type") == "text" and not isinstance(block.get("text", ""), str):
            return False
    return True


def _events_of(record: dict[str, Any], *, stats: RecordStats | None = None) -> Iterator[Event]:
    kind = record.get("type")
    ts = record.get("timestamp", "")
    session = record.get("sessionId", "")

    if kind == "user":
        message = record.get("message")
        if message is None:
            message = {}
        if not isinstance(message, dict) or not _valid_content_blocks(message.get("content", "")):
            _reject_malformed(stats)
            return
        text = _human_text(message)
        if text is None or not text.strip():
            return  # a tool_result, or nothing at all
        if is_genuine_user_record(record):
            yield Event("user", ts, session, text=text.strip())
        elif is_subagent_prompt_record(record):
            yield Event("subagent_prompt", ts, session, text=text.strip())
        else:
            # Evidence that something happened (a subagent finished, a slash command ran), but
            # not a prompt: it must not open a turn.
            yield Event(
                "system_user", ts, session, text=text.strip(),
                name=_system_user_label(record, text),
            )
        return

    if kind == "assistant":
        message = record.get("message")
        if message is None:
            message = {}
        if not isinstance(message, dict):
            _reject_malformed(stats)
            return
        content = message.get("content")
        if content is None:
            content = []
        if not isinstance(content, list):
            _reject_malformed(stats)
            return
        message_id = message.get("id") or record.get("uuid") or ""
        if not isinstance(message_id, str):
            message_id = str(message_id)
        texts = []
        for block in content:
            if not isinstance(block, dict):
                _reject_malformed(stats)
                return
            if block.get("type") == "text":
                text = block.get("text", "")
                if not isinstance(text, str):
                    _reject_malformed(stats)
                    return
                texts.append(text)
            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                payload = block.get("input", {})
                if payload is None:
                    payload = {}
                if not isinstance(name, str) or not isinstance(payload, dict):
                    _reject_malformed(stats)
                    return
                yield Event(
                    "tool_use", ts, session,
                    name=name, payload=payload,
                    message_id=message_id,
                )
        if texts:
            yield Event("assistant", ts, session, text="\n".join(texts), message_id=message_id)
        return

    if kind == "attachment":
        attachment = record.get("attachment")
        if attachment is None:
            attachment = {}
        if not isinstance(attachment, dict):
            _reject_malformed(stats)
            return
        atype = attachment.get("type", "")
        if not isinstance(atype, str):
            _reject_malformed(stats)
            return
        if "hook" not in atype:
            return
        event_name = attachment.get("hookEvent") or attachment.get("hookName") or ""
        if not isinstance(event_name, str):
            _reject_malformed(stats)
            return
        kind_name = HOOK_KINDS.get(atype, "hook_other")
        if atype == "hook_blocking_error":
            text = _blocking_message(attachment)
        elif atype == "hook_additional_context":
            content = attachment.get("content", "")
            if not isinstance(content, str) and not (
                isinstance(content, list) and all(isinstance(part, str) for part in content)
            ):
                _reject_malformed(stats)
                return
            text = _injected_text(attachment)
        else:
            text = ""
        yield Event(kind_name, ts, session, name=event_name, text=text)


@dataclass(slots=True)
class Turn:
    """Everything between one genuine human message and the next."""

    prompt: str
    timestamp: str
    session: str
    events: list[Event] = field(default_factory=list)
    #: "human" for a genuine user prompt; "subagent_prompt" for a dispatch brief.
    opened_by: str = "human"

    @property
    def tool_calls(self) -> list[str]:
        return [e.name for e in self.events if e.kind == "tool_use"]

    @property
    def blocked_by(self) -> list[str]:
        from bearhug.replay.hooks import attribute

        return [attribute(e.text) for e in self.events if e.kind == "hook_block"]


def iter_turns(events: Iterable[Event]) -> Iterator[Turn]:
    """Group events into turns, split on genuine human messages."""
    current: Turn | None = None
    for event in events:
        if event.kind in TURN_OPENERS:
            if current is not None:
                yield current
            current = Turn(
                prompt=event.text,
                timestamp=event.timestamp,
                session=event.session,
                opened_by="human" if event.kind == "user" else event.kind,
            )
            continue
        if current is not None:
            current.events.append(event)
    if current is not None:
        yield current
