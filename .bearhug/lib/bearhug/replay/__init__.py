"""Phase 4 — mine real session transcripts."""

from bearhug.replay.corpus import CorpusError, CorpusMember, CorpusSelection, select_corpus
from bearhug.replay.field_census import FieldCensus, FieldStat, census_fields, render_coverage_table
from bearhug.replay.hooks import (
    Firing,
    attribute,
    blocking_firings,
    stacked_blocks,
    tool_traffic,
)
from bearhug.replay.injection import (
    InjectorStats,
    attribute_injector,
    compute_injection_stats,
    extract_rare_tokens,
)
from bearhug.replay.injection import build_findings as build_injection_findings
from bearhug.replay.injection import render_table as render_injection_table
from bearhug.replay.ledger import (
    LedgerData,
    build_findings,
    compute_ledger,
    render_table,
)
from bearhug.replay.metrics import (
    CompactionMetrics,
    CorpusMetrics,
    DurationAggregate,
    DurationMetrics,
    SessionMetrics,
    TokenMetrics,
    corpus_metrics,
    session_metrics_for,
)
from bearhug.replay.transcript import (
    Event,
    Turn,
    iter_events,
    iter_turns,
    session_transcripts,
)

__all__ = [
    "CompactionMetrics",
    "CorpusMetrics",
    "CorpusError",
    "CorpusMember",
    "CorpusSelection",
    "Event",
    "DurationAggregate",
    "DurationMetrics",
    "Firing",
    "FieldCensus",
    "FieldStat",
    "InjectorStats",
    "LedgerData",
    "SessionMetrics",
    "TokenMetrics",
    "Turn",
    "attribute",
    "attribute_injector",
    "blocking_firings",
    "build_findings",
    "build_injection_findings",
    "compute_injection_stats",
    "compute_ledger",
    "corpus_metrics",
    "census_fields",
    "extract_rare_tokens",
    "iter_events",
    "iter_turns",
    "render_injection_table",
    "render_coverage_table",
    "render_table",
    "session_metrics_for",
    "session_transcripts",
    "select_corpus",
    "stacked_blocks",
    "tool_traffic",
]
