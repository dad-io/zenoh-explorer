"""4.3 — per-session metrics over the transcript corpus.

Barracuda has no per-session metric tooling of its own; every count here is net-new. Two
distinctions this module must not blur, because they have both bitten before:

* A `tool_result` arrives as a user-role message and is NOT a human turn. `iter_events` already
  draws that line — a `tool_result` record yields no `Event` at all — so a human-turn count taken
  from `iter_events` is correct by construction. This module never re-parses message content to
  ask "is this a human turn"; it only counts what `iter_events` already decided.
* Subagent transcripts live one directory down (`<session>/subagents/agent-*.jsonl`) and carry
  their own turns. They are kept in a SEPARATE bucket end to end — never summed into a per-session
  number — because 109 of them (44 MB, 3,024 tool calls) were invisible to every scan this repo's
  history records before `session_transcripts` existed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.replay.hooks import attribute
from bearhug.replay.transcript import _events_of, iter_records, session_transcripts

#: Tool names that hand work to another agent. Counted separately from ordinary tool calls
#: because "how much of this corpus is orchestration rather than direct work" is its own number.
DISPATCH_TOOLS = ("Agent", "Task")

# M01 measured these four fields at ``$.message.usage`` on every assistant record in the frozen
# corpus.  ``usage.iterations`` repeats the same component totals and is deliberately not a second
# source: counting both would double the measured consumption.
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
TOKEN_ALIASES = {
    "input_tokens": ("input_tokens", "inputTokens"),
    "output_tokens": ("output_tokens", "outputTokens"),
    "cache_creation_input_tokens": (
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
    ),
    "cache_read_input_tokens": ("cache_read_input_tokens", "cacheReadInputTokens"),
}


def _timestamp(value: Any) -> datetime | None:
    """Return one comparable UTC timestamp, or ``None`` for an unmeasured value."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(slots=True)
class DurationMetrics:
    """Observed transcript timing, explicitly not model or API latency.

    The corpus contains several fields whose names include ``duration`` but M01 established only
    their shape, not their meaning. M04 therefore uses the one field with a stable mechanical
    interpretation: record timestamps. Span is ``max(timestamp) - min(timestamp)`` even when
    records arrive out of order. A one-timestamp transcript has a measured zero span; a transcript
    without a valid timestamp has no measured span.
    """

    records_seen: int = 0
    valid_timestamp_records: int = 0
    missing_timestamp_records: int = 0
    malformed_timestamp_records: int = 0
    out_of_order_records: int = 0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    previous_timestamp: datetime | None = None
    largest_forward_gap_ms: float | None = None

    def observe(self, value: Any) -> None:
        self.records_seen += 1
        if value is None or value == "":
            self.missing_timestamp_records += 1
            return
        parsed = _timestamp(value)
        if parsed is None:
            self.malformed_timestamp_records += 1
            return
        self.valid_timestamp_records += 1
        if self.first_timestamp is None or parsed < self.first_timestamp:
            self.first_timestamp = parsed
        if self.last_timestamp is None or parsed > self.last_timestamp:
            self.last_timestamp = parsed
        if self.previous_timestamp is not None:
            delta_ms = (parsed - self.previous_timestamp).total_seconds() * 1000.0
            if delta_ms < 0:
                self.out_of_order_records += 1
            elif self.largest_forward_gap_ms is None or delta_ms > self.largest_forward_gap_ms:
                self.largest_forward_gap_ms = delta_ms
        self.previous_timestamp = parsed

    @property
    def observed_transcript_span_ms(self) -> float | None:
        if self.first_timestamp is None or self.last_timestamp is None:
            return None
        return (self.last_timestamp - self.first_timestamp).total_seconds() * 1000.0

    @property
    def timestamp_coverage_percentage(self) -> float | None:
        if not self.records_seen:
            return None
        return self.valid_timestamp_records * 100.0 / self.records_seen

    def as_dict(self) -> dict[str, Any]:
        return {
            "measurement": "observed_transcript_span",
            "not_model_or_api_latency": True,
            "records_seen": self.records_seen,
            "valid_timestamp_records": self.valid_timestamp_records,
            "missing_timestamp_records": self.missing_timestamp_records,
            "malformed_timestamp_records": self.malformed_timestamp_records,
            "timestamp_coverage_percentage": self.timestamp_coverage_percentage,
            "out_of_order_records": self.out_of_order_records,
            "first_timestamp": (
                self.first_timestamp.isoformat() if self.first_timestamp is not None else None
            ),
            "last_timestamp": (
                self.last_timestamp.isoformat() if self.last_timestamp is not None else None
            ),
            "observed_transcript_span_ms": self.observed_transcript_span_ms,
            "largest_forward_gap_ms": self.largest_forward_gap_ms,
        }


@dataclass(slots=True)
class DurationAggregate:
    """Separate session/subagent roll-up without pretending overlapping spans are wall time."""

    transcripts: int = 0
    transcripts_with_span: int = 0
    sum_observed_transcript_spans_ms: float = 0.0
    valid_timestamp_records: int = 0
    records_seen: int = 0
    malformed_timestamp_records: int = 0
    out_of_order_records: int = 0

    @classmethod
    def from_metrics(cls, metrics: list[SessionMetrics]) -> DurationAggregate:
        result = cls(transcripts=len(metrics))
        for item in metrics:
            duration = item.duration
            result.records_seen += duration.records_seen
            result.valid_timestamp_records += duration.valid_timestamp_records
            result.malformed_timestamp_records += duration.malformed_timestamp_records
            result.out_of_order_records += duration.out_of_order_records
            span = duration.observed_transcript_span_ms
            if span is not None:
                result.transcripts_with_span += 1
                result.sum_observed_transcript_spans_ms += span
        return result

    @property
    def transcript_coverage_percentage(self) -> float | None:
        if not self.transcripts:
            return None
        return self.transcripts_with_span * 100.0 / self.transcripts

    def as_dict(self) -> dict[str, Any]:
        return {
            "measurement": "observed_transcript_span",
            "not_model_or_api_latency": True,
            "transcripts": self.transcripts,
            "transcripts_with_span": self.transcripts_with_span,
            "transcript_coverage_percentage": self.transcript_coverage_percentage,
            "sum_observed_transcript_spans_ms": self.sum_observed_transcript_spans_ms,
            "records_seen": self.records_seen,
            "valid_timestamp_records": self.valid_timestamp_records,
            "malformed_timestamp_records": self.malformed_timestamp_records,
            "out_of_order_records": self.out_of_order_records,
            "aggregation_limit": (
                "The sum is a sum of per-transcript spans; overlapping transcripts mean it is "
                "not elapsed wall time."
            ),
        }


@dataclass(slots=True)
class CompactionMetrics:
    """M06's structural detector for the one compaction shape M01 observed.

    The frozen corpus established ``type == \"system\"`` plus an object-valued
    ``compactMetadata`` field. Text containing ``compact`` and summary/continuation record labels
    are lookalikes, not events. Unknown future shapes are therefore undercounted rather than
    promoted from prose by a substring match.
    """

    records_seen: int = 0
    explicit_compactions: int = 0
    malformed_compaction_records: int = 0
    ignored_structural_lookalikes: int = 0

    def observe(self, record: Mapping[str, Any]) -> None:
        self.records_seen += 1
        record_type = record.get("type")
        has_metadata = "compactMetadata" in record
        if record_type == "system" and has_metadata:
            if isinstance(record.get("compactMetadata"), Mapping):
                self.explicit_compactions += 1
            else:
                self.malformed_compaction_records += 1
            return
        if has_metadata or record_type in {"compact", "compaction", "summary", "continuation"}:
            self.ignored_structural_lookalikes += 1

    def merge(self, other: CompactionMetrics) -> None:
        self.records_seen += other.records_seen
        self.explicit_compactions += other.explicit_compactions
        self.malformed_compaction_records += other.malformed_compaction_records
        self.ignored_structural_lookalikes += other.ignored_structural_lookalikes

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": "system-object-compactMetadata/v1",
            "records_seen": self.records_seen,
            "explicit_compactions": self.explicit_compactions,
            "malformed_compaction_records": self.malformed_compaction_records,
            "ignored_structural_lookalikes": self.ignored_structural_lookalikes,
            "precision_limit": (
                "Counts only the system.compactMetadata object shape established by M01; a new "
                "record format will be missed until separately measured and versioned."
            ),
        }


def _token_value(value: Any) -> int | None:
    """Token counters are non-negative JSON integers; bool and float are not counters."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(slots=True)
class _MessageTokenState:
    """Temporary per-message state used to collapse cumulative transcript snapshots."""

    saw_usage: bool = False
    values: dict[str, int] = field(default_factory=dict)
    provenance: dict[str, set[str]] = field(default_factory=dict)
    malformed_values: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class TokenMetrics:
    """Reported token components, coverage, and structural provenance.

    A Claude transcript can repeat one assistant message several times while its cumulative usage
    grows.  Bear Hug identifies that message by ``message.id`` and keeps the maximum reported value
    for each component.  It never sums both the top-level usage and the repeated ``iterations``
    array.  Missing IDs fall back to distinct record identities, so they cannot be silently merged.
    """

    totals: Counter[str] = field(default_factory=Counter)
    assistant_messages: int = 0
    messages_with_usage: int = 0
    complete_messages: int = 0
    messages_with_field: Counter[str] = field(default_factory=Counter)
    malformed_values: Counter[str] = field(default_factory=Counter)
    provenance: dict[str, Counter[str]] = field(default_factory=dict)

    def coverage_percentage(self, component: str | None = None) -> float | None:
        """Percentage of distinct assistant messages with valid reported values."""
        if not self.assistant_messages:
            return None
        numerator = (
            self.complete_messages
            if component is None
            else self.messages_with_field[component]
        )
        return numerator * 100.0 / self.assistant_messages

    def merge(self, other: TokenMetrics) -> None:
        self.totals.update(other.totals)
        self.assistant_messages += other.assistant_messages
        self.messages_with_usage += other.messages_with_usage
        self.complete_messages += other.complete_messages
        self.messages_with_field.update(other.messages_with_field)
        self.malformed_values.update(other.malformed_values)
        for component, paths in other.provenance.items():
            self.provenance.setdefault(component, Counter()).update(paths)

    def as_dict(self) -> dict[str, Any]:
        return {
            "totals": {component: self.totals[component] for component in TOKEN_FIELDS},
            "assistant_messages": self.assistant_messages,
            "messages_with_usage": self.messages_with_usage,
            "complete_messages": self.complete_messages,
            "coverage_percentage": self.coverage_percentage(),
            "component_coverage_percentage": {
                component: self.coverage_percentage(component) for component in TOKEN_FIELDS
            },
            "malformed_values": {
                component: self.malformed_values[component] for component in TOKEN_FIELDS
            },
            "provenance": {
                component: dict(sorted(self.provenance.get(component, {}).items()))
                for component in TOKEN_FIELDS
            },
        }


def _finish_token_metrics(states: dict[str, _MessageTokenState]) -> TokenMetrics:
    result = TokenMetrics(assistant_messages=len(states))
    for state in states.values():
        if state.saw_usage:
            result.messages_with_usage += 1
        for component, value in state.values.items():
            result.totals[component] += value
            result.messages_with_field[component] += 1
        if all(component in state.values for component in TOKEN_FIELDS):
            result.complete_messages += 1
        result.malformed_values.update(state.malformed_values)
        for component, paths in state.provenance.items():
            counter = result.provenance.setdefault(component, Counter())
            counter.update(paths)
    return result


@dataclass(slots=True)
class SessionMetrics:
    """One transcript file's mechanical facts. One instance per `.jsonl`, subagent or not."""

    path: Path
    is_subagent: bool
    session_id: str = ""
    human_turns: int = 0
    tool_result_turns: int = 0
    #: `type: user` records the harness wrote (task notifications, slash-command echoes, meta
    #: caveats). Neither a prompt nor a tool result; see transcript.is_genuine_user_record.
    system_turns: int = 0
    #: Turns opened by a parent's dispatch brief inside a subagent transcript — a prompt, not a
    #: person; see transcript.is_subagent_prompt_record.
    prompt_turns: int = 0
    tool_calls: Counter = field(default_factory=Counter)
    dispatches: int = 0
    gate_blocks: Counter = field(default_factory=Counter)
    tokens: TokenMetrics = field(default_factory=TokenMetrics)
    duration: DurationMetrics = field(default_factory=DurationMetrics)
    compactions: CompactionMetrics = field(default_factory=CompactionMetrics)
    first_ts: str = ""
    last_ts: str = ""

    @property
    def total_tool_calls(self) -> int:
        return sum(self.tool_calls.values())

    @property
    def day(self) -> str:
        return self.first_ts[:10]


def session_metrics_for(path: Path, *, since: str | None = None) -> SessionMetrics:
    """One transcript's metrics, streamed in a single pass over its lines.

    `_events_of` (transcript.py's own record-to-Event judgement) decides what is a genuine human
    turn; this function never re-implements that judgement, only tallies its output against the
    raw `type == "user"` count to get `tool_result_turns` without a second file read.
    """
    metrics = SessionMetrics(path=path, is_subagent=path.parent.name == "subagents")
    token_states: dict[str, _MessageTokenState] = {}
    for line_number, record in iter_records(path):
        record_ts = record.get("timestamp") or ""
        if since and record_ts and record_ts[:10] < since:
            continue
        metrics.duration.observe(record.get("timestamp"))
        metrics.compactions.observe(record)
        if record.get("type") == "assistant":
            message = record.get("message")
            message_id = message.get("id") if isinstance(message, dict) else None
            identity = (
                message_id
                if isinstance(message_id, str) and message_id
                else f"<record:{line_number}>"
            )
            state = token_states.setdefault(identity, _MessageTokenState())
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                state.saw_usage = True
                for component, aliases in TOKEN_ALIASES.items():
                    valid_values: list[int] = []
                    for alias in aliases:
                        if alias not in usage:
                            continue
                        state.provenance.setdefault(component, set()).add(
                            f"$.message.usage.{alias}"
                        )
                        value = _token_value(usage[alias])
                        if value is None:
                            state.malformed_values[component] += 1
                            continue
                        valid_values.append(value)
                    if len(set(valid_values)) > 1:
                        # Two aliases making incompatible claims are not evidence for either
                        # value. Keep the provenance, expose the conflict, and wait for another
                        # well-formed snapshot of this message.
                        state.malformed_values[component] += 1
                    elif valid_values:
                        value = valid_values[0]
                        state.values[component] = max(state.values.get(component, 0), value)
        events = list(_events_of(record))
        is_raw_user = record.get("type") == "user"
        has_prompt_event = any(e.kind in ("user", "subagent_prompt") for e in events)
        has_system_event = any(e.kind == "system_user" for e in events)
        if is_raw_user and not has_prompt_event and not has_system_event:
            metrics.tool_result_turns += 1
        for event in events:
            if not metrics.session_id and event.session:
                metrics.session_id = event.session
            if event.timestamp:
                if not metrics.first_ts or event.timestamp < metrics.first_ts:
                    metrics.first_ts = event.timestamp
                if event.timestamp > metrics.last_ts:
                    metrics.last_ts = event.timestamp
            if event.kind == "user":
                metrics.human_turns += 1
            elif event.kind == "subagent_prompt":
                metrics.prompt_turns += 1
            elif event.kind == "system_user":
                metrics.system_turns += 1
            elif event.kind == "tool_use":
                metrics.tool_calls[event.name] += 1
                if event.name in DISPATCH_TOOLS:
                    metrics.dispatches += 1
            elif event.kind == "hook_block":
                metrics.gate_blocks[attribute(event.text)] += 1
    metrics.tokens = _finish_token_metrics(token_states)
    return metrics


@dataclass(slots=True)
class CorpusMetrics:
    """The aggregate the CLI prints: sessions and subagents, kept in separate buckets."""

    sessions: list[SessionMetrics] = field(default_factory=list)
    subagents: list[SessionMetrics] = field(default_factory=list)

    @property
    def all_transcripts(self) -> list[SessionMetrics]:
        return self.sessions + self.subagents

    def tool_histogram(self) -> Counter:
        total: Counter = Counter()
        for m in self.all_transcripts:
            total.update(m.tool_calls)
        return total

    def gate_histogram(self) -> Counter:
        total: Counter = Counter()
        for m in self.all_transcripts:
            total.update(m.gate_blocks)
        return total

    def token_metrics(self, *, subagents: bool) -> TokenMetrics:
        total = TokenMetrics()
        selected = self.subagents if subagents else self.sessions
        for metrics in selected:
            total.merge(metrics.tokens)
        return total

    def duration_metrics(self, *, subagents: bool) -> DurationAggregate:
        selected = self.subagents if subagents else self.sessions
        return DurationAggregate.from_metrics(selected)

    def compaction_metrics(self, *, subagents: bool) -> CompactionMetrics:
        total = CompactionMetrics()
        selected = self.subagents if subagents else self.sessions
        for metrics in selected:
            total.merge(metrics.compactions)
        return total

    @property
    def session_tokens(self) -> TokenMetrics:
        return self.token_metrics(subagents=False)

    @property
    def subagent_tokens(self) -> TokenMetrics:
        return self.token_metrics(subagents=True)

    @property
    def session_durations(self) -> DurationAggregate:
        return self.duration_metrics(subagents=False)

    @property
    def subagent_durations(self) -> DurationAggregate:
        return self.duration_metrics(subagents=True)

    @property
    def session_compactions(self) -> CompactionMetrics:
        return self.compaction_metrics(subagents=False)

    @property
    def subagent_compactions(self) -> CompactionMetrics:
        return self.compaction_metrics(subagents=True)

    @property
    def total_human_turns(self) -> int:
        return sum(m.human_turns for m in self.sessions)

    @property
    def total_subagent_turns(self) -> int:
        """Turn openers inside subagent transcripts: dispatch briefs, plus any genuine human
        record a subagent file might carry (none observed in the frozen corpus)."""
        return sum(m.human_turns + m.prompt_turns for m in self.subagents)

    @property
    def total_system_turns(self) -> int:
        return sum(m.system_turns for m in self.all_transcripts)

    @property
    def total_tool_result_turns(self) -> int:
        return sum(m.tool_result_turns for m in self.all_transcripts)

    @property
    def total_dispatches(self) -> int:
        return sum(m.dispatches for m in self.sessions)

    @property
    def date_range(self) -> tuple[str, str]:
        starts = [m.first_ts for m in self.all_transcripts if m.first_ts]
        ends = [m.last_ts for m in self.all_transcripts if m.last_ts]
        if not starts:
            return ("", "")
        return (min(starts), max(ends))


def corpus_metrics(paths: list[Path], *, since: str | None = None) -> CorpusMetrics:
    """4.3 — every transcript, metered. `paths` is expected to be `session_transcripts(dir)`'s
    output so top-level sessions and subagents both get counted, in their own buckets."""
    report = CorpusMetrics()
    for path in paths:
        metrics = session_metrics_for(path, since=since)
        if metrics.is_subagent:
            report.subagents.append(metrics)
        else:
            report.sessions.append(metrics)
    return report


__all__ = [
    "CompactionMetrics",
    "CorpusMetrics",
    "DurationAggregate",
    "DurationMetrics",
    "SessionMetrics",
    "TOKEN_FIELDS",
    "TokenMetrics",
    "corpus_metrics",
    "session_metrics_for",
    "session_transcripts",
]
