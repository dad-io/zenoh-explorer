"""A05 — select cited architecture context without treating the index as authority.

Only a fresh, commit-matching artifact can yield records. Every other state returns an explicit
``index_gap`` with the freshness problems and no architectural claims. Selection is deliberately
bounded and query-driven: this is a narrow read surface, not a default full-index dump.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from bearhug.arch.freshness import FreshnessResult, verdict_for

MAX_RESULTS = 50
CONTEXT_SCHEMA_VERSION = "1"


def _values(value: Any) -> Iterable[str]:
    """Yield scalar values a query may match; field names themselves are not facts."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield str(value)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _values(child)


def _matches(record: dict[str, Any], query: str) -> bool:
    needle = query.casefold()
    return any(needle in value.casefold() for value in _values(record))


def _selected_record(record: dict[str, Any]) -> dict[str, Any]:
    """Keep the citation adjacent to a fact while avoiding a second provenance shape."""
    provenance = record["provenance"]
    return {
        "id": record["id"],
        "kind": record["kind"],
        "authority": record["authority"],
        "citation": {"path": provenance["path"], "line": provenance["line"]},
        "fact": {
            key: value
            for key, value in record.items()
            if key not in {"id", "kind", "authority", "provenance"}
        },
    }


def _gap(query: str, freshness: FreshnessResult, gaps: list[str]) -> dict[str, Any]:
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "query": query,
        "status": "index_gap",
        "freshness": freshness.as_dict(),
        "records": [],
        "gaps": gaps,
    }


def select(artifact: Any, query: str, *, head: str | None = None) -> dict[str, Any]:
    """Return bounded, cited records relevant to ``query`` when the index is fresh.

    Silence is never presented as absence: no match is an ``index_gap``. A stale, partial, or
    structurally unknown artifact returns no records even when a string match would be possible.
    """
    freshness = verdict_for(artifact, head=head)
    normalized = query.strip()
    if not normalized:
        return _gap(query, freshness, ["empty_query: refusing a full-index dump"])
    if not freshness.usable:
        return _gap(query, freshness, list(freshness.problems))

    matches = sorted(
        (record for record in artifact["records"] if _matches(record, normalized)),
        key=lambda record: record["id"],
    )
    if not matches:
        return _gap(
            query,
            freshness,
            ["no_query_match: the index cannot establish that the subject is absent"],
        )

    gaps: list[str] = []
    if len(matches) > MAX_RESULTS:
        gaps.append(
            f"results_truncated: {len(matches)} matched; returned the first {MAX_RESULTS} by id"
        )
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "query": query,
        "status": "ok",
        "freshness": freshness.as_dict(),
        "records": [_selected_record(record) for record in matches[:MAX_RESULTS]],
        "gaps": gaps,
    }


__all__ = ["CONTEXT_SCHEMA_VERSION", "MAX_RESULTS", "select"]
