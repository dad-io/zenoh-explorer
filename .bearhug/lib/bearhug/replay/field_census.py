"""M01 — stream transcript metric-field shapes without retaining transcript content.

This module discovers what the selected immutable corpus can actually measure before a metric is
implemented.  It records only scope, structural record type, field path, Python value type, and
counts.  Values and message content never enter the result object.

The census is intentionally not a metric calculator.  A numeric ``input_tokens`` field establishes
availability; it does not establish whether the field is incremental or cumulative.  M02-M06 own
those semantics and must leave a family unavailable when this census cannot support them.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bearhug.model import Evidence, Finding, Severity
from bearhug.replay.transcript import RecordStats, iter_records

FAMILIES = ("tokens", "cost", "duration", "context", "compaction", "timestamp")
NUMERIC_FAMILIES = frozenset({"tokens", "cost", "duration", "context"})

_KEYS = {
    "tokens": frozenset({
        "input_tokens", "output_tokens", "total_tokens", "cache_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    }),
    "cost": frozenset({
        "cost", "cost_usd", "total_cost", "total_cost_usd", "api_cost_usd",
    }),
    "duration": frozenset({
        "duration", "duration_ms", "latency", "latency_ms", "elapsed", "elapsed_ms",
    }),
    "context": frozenset({
        "context_tokens", "context_used", "context_usage", "context_window",
        "context_window_size",
    }),
    "compaction": frozenset({
        "compact_count", "compact_metadata", "compacted", "compaction", "compaction_count",
        "is_compact",
    }),
    "timestamp": frozenset({
        "timestamp", "created_at", "updated_at", "started_at", "ended_at",
        "start_time", "end_time",
    }),
}
_COMPACTION_RECORD_TYPES = frozenset({"compact", "compaction", "summary"})
_STRUCTURAL = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_CAMEL_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")


def _snake(name: str) -> str:
    first = _CAMEL_1.sub(r"\1_\2", name.replace("-", "_"))
    return _CAMEL_2.sub(r"\1_\2", first).lower()


def _family(key: str) -> str | None:
    normal = _snake(key)
    for family, names in _KEYS.items():
        if normal in names:
            return family
    return None


def _record_type(record: dict[str, Any]) -> str:
    value = record.get("type")
    if isinstance(value, str) and _STRUCTURAL.fullmatch(value):
        return value
    if value is None:
        return "<missing>"
    return f"<{type(value).__name__}>"


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _valid(family: str, value: Any) -> bool:
    if family in NUMERIC_FAMILIES:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and value >= 0
        )
    if family == "timestamp":
        if not isinstance(value, str) or not value:
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
    # M06 decides compaction semantics. Here a structured candidate merely has to be non-null.
    return value is not None


def _candidate_values(value: Any, path: str = "$"):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            child_path = f"{path}.{key}"
            family = _family(key)
            if family:
                yield family, child_path, child
            yield from _candidate_values(child, child_path)
    elif isinstance(value, list):
        for child in value:
            yield from _candidate_values(child, f"{path}[]")


@dataclass(slots=True)
class FieldStat:
    """One candidate field at one structural location; no observed value is retained."""

    scope: str
    record_type: str
    family: str
    path: str
    records_with_field: int = 0
    occurrences: int = 0
    valid_values: int = 0
    malformed_values: int = 0
    value_types: Counter[str] = field(default_factory=Counter)

    def as_dict(self, *, records_of_type: int) -> dict[str, Any]:
        coverage = self.records_with_field / records_of_type if records_of_type else None
        return {
            "scope": self.scope,
            "record_type": self.record_type,
            "family": self.family,
            "path": self.path,
            "records_with_field": self.records_with_field,
            "records_of_type": records_of_type,
            "coverage": coverage,
            "occurrences": self.occurrences,
            "valid_values": self.valid_values,
            "malformed_values": self.malformed_values,
            "value_types": dict(sorted(self.value_types.items())),
        }


@dataclass(slots=True)
class FieldCensus:
    """The content-free schema coverage of a selected transcript set."""

    records: Counter[tuple[str, str]] = field(default_factory=Counter)
    fields: dict[tuple[str, str, str, str], FieldStat] = field(default_factory=dict)
    malformed_lines: int = 0
    non_object_lines: int = 0
    duplicate_records: int = 0
    overlong_lines: int = 0

    @property
    def records_seen(self) -> int:
        return sum(self.records.values())

    def rows(self, family: str | None = None) -> list[dict[str, Any]]:
        selected = (
            stat for stat in self.fields.values() if family is None or stat.family == family
        )
        return [
            stat.as_dict(records_of_type=self.records[(stat.scope, stat.record_type)])
            for stat in sorted(
                selected,
                key=lambda item: (item.family, item.scope, item.record_type, item.path),
            )
        ]

    def availability(self, family: str) -> str:
        rows = [stat for stat in self.fields.values() if stat.family == family]
        if not rows:
            return "unavailable"
        if any(stat.malformed_values for stat in rows):
            return "version-dependent"
        return "directly-measurable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "records_seen": self.records_seen,
            "record_types": [
                {"scope": scope, "record_type": kind, "records": count}
                for (scope, kind), count in sorted(self.records.items())
            ],
            "families": {family: self.availability(family) for family in FAMILIES},
            "fields": self.rows(),
            "malformed_lines": self.malformed_lines,
            "non_object_lines": self.non_object_lines,
            "duplicate_records": self.duplicate_records,
            "overlong_lines": self.overlong_lines,
        }


def census_fields(paths: list[Path], *, since: str | None = None) -> FieldCensus:
    """Stream candidate metric field shapes from every selected transcript."""
    census = FieldCensus()
    for path in paths:
        scope = "subagent" if path.parent.name == "subagents" else "session"
        stats = RecordStats()
        for _, record in iter_records(path, stats=stats):
            timestamp = record.get("timestamp")
            if since and isinstance(timestamp, str) and timestamp[:10] < since:
                continue
            record_type = _record_type(record)
            census.records[(scope, record_type)] += 1
            seen: set[tuple[str, str]] = set()
            candidates = list(_candidate_values(record))
            if record_type in _COMPACTION_RECORD_TYPES:
                candidates.append(("compaction", "$.type", record_type))
            for family, field_path, value in candidates:
                key = (scope, record_type, family, field_path)
                stat = census.fields.setdefault(
                    key,
                    FieldStat(
                        scope=scope,
                        record_type=record_type,
                        family=family,
                        path=field_path,
                    ),
                )
                stat.occurrences += 1
                stat.value_types[_value_type(value)] += 1
                if _valid(family, value):
                    stat.valid_values += 1
                else:
                    stat.malformed_values += 1
                presence = (family, field_path)
                if presence not in seen:
                    stat.records_with_field += 1
                    seen.add(presence)
        census.overlong_lines += stats.overlong_lines
        census.malformed_lines += stats.malformed_lines
        census.non_object_lines += stats.non_object_lines
        census.duplicate_records += stats.duplicate_records
    return census


def render_coverage_table(census: FieldCensus) -> str:
    """A compact coverage table for ``bearhug replay metrics``."""
    lines = [
        f"{'family':12s} {'availability':22s} {'paths':>5s} {'valid':>7s} {'bad':>7s}",
        "-" * 57,
    ]
    for family in FAMILIES:
        stats = [stat for stat in census.fields.values() if stat.family == family]
        lines.append(
            f"{family:12s} {census.availability(family):22s} {len(stats):>5d} "
            f"{sum(stat.valid_values for stat in stats):>7d} "
            f"{sum(stat.malformed_values for stat in stats):>7d}"
        )
    return "\n".join(lines)


def build_findings(census: FieldCensus, *, snapshot_id: str, corpus_label: str) -> list[Finding]:
    """Persist M01 coverage in the ordinary replay finding/report path."""
    findings = []
    for family in FAMILIES:
        state = census.availability(family)
        rows = census.rows(family)
        descriptions = [
            (
                f"{row['scope']}/{row['record_type']} {row['path']}: "
                f"{row['records_with_field']}/{row['records_of_type']} records, "
                f"types={row['value_types']}, malformed={row['malformed_values']}"
            )
            for row in rows
        ]
        findings.append(
            Finding(
                id=f"replay-field-coverage-{family}",
                check="METRIC-COVERAGE",
                severity=Severity.INFO,
                summary=f"{family} is {state} in the selected transcript corpus.",
                snapshot=snapshot_id,
                evidence=(Evidence(run_id=f"{corpus_label} records={census.records_seen}"),),
                detail="; ".join(descriptions) if descriptions else "No candidate field found.",
                limit=(
                    "Schema availability is not metric semantics. Values are never retained; "
                    "incremental-vs-cumulative meaning and classifier validity remain M02-M06 work."
                ),
            )
        )
    return findings


__all__ = [
    "FAMILIES",
    "FieldCensus",
    "FieldStat",
    "build_findings",
    "census_fields",
    "render_coverage_table",
]
