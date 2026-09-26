"""Deterministic grounding: bind an accepted plan's terms to project knowledge, by reference.

The Sep 5 operating algorithm grounds every important term against the project's own knowledge
before a capsule is planned: decision records (Memex), the architecture index, the Graft code map
and MemQ recall.  This module is that step as deterministic code.  It reads those sources, never
writes them, and returns three things the intent envelope and execution packet can carry:

- ``bindings``: one accepted (or explicitly proposed) binding per matched decision record, with
  the record's exact byte digest as evidence;
- ``invariants``: one ``project_sealed`` invariant per matched *accepted* decision;
- ``sources``: bounded packet sources for tiers P1-P3.  Decision records travel as repository
  paths plus their sealed digest and are projected at packet time; architecture, Graft and MemQ
  results travel as small inline observations that name pointers, never copied prose.

Nothing here becomes authority.  A binding's ``state`` follows the record's own ``status``; an
observation is labelled ``observed``; an unavailable tool is reported as unavailable, not as
"nothing relevant".  Matching is lexical and conservative so the same inputs always yield the
same digest; the operator reviews the result in the onboarding draft before it is sealed.

Stdlib only: this module is copied into managed projects with the onboarding bridge.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "1"
RECORD_KIND = "capsule_grounding"
DECISION_DIR = "docs/memex/decisions"
ARCHITECTURE_INDEX = ".bearhug/architecture/index.json"
CANONICAL_ARCHITECTURE_INDEX = "docs/architecture/index.json"

MAX_TERMS = 40
MAX_TOOL_TERMS = 4
MAX_DECISIONS = 16
MAX_SOURCES = 32

# GS-02's recommended default (its decision record, section 2.1): the strict
# reading of "strong" (one rare title/tag word, or one distinctive body span) rejected a decision
# that accumulated a score of 7 from seven ordinary words with no single distinctive one -- 73% of
# the family's measured misses. ``strong_lexical_score`` grants ``strong`` a second way in: enough
# accumulated lexical evidence, title and body combined, on its own. The margin (not the absolute
# number) is what a caller who raises ``minimum_score`` inherits, so raising the floor for a strict
# mode does not silently reopen this gate at the old floor's width.
MINIMUM_SCORE_DEFAULT = 4
STRONG_LEXICAL_SCORE_MARGIN = 1
MAX_INLINE_BYTES = 16 * 1024
MAX_DECISION_FILE_BYTES = 256 * 1024
MAX_PROJECTION_BYTES = 6 * 1024
MAX_TOOL_OUTPUT_BYTES = 256 * 1024
DEFAULT_TOOL_TIMEOUT_S = 8.0

# Caps the report now names instead of applying silently. See "Truthful caps" in
# src/bearhug/campaign/README.md: every cap below is reported through `_selection_shape`.
MAX_ARCHITECTURE_RECORDS = 50
MAX_ARCHITECTURE_GAPS = 20
MAX_TOOL_HITS = 40
GRAFT_PER_QUERY_HITS = 8
MAX_DROPPED_DECISIONS_LISTED = 32
MAX_SKIPPED_DECISIONS_LISTED = 20
MAX_REASON_CHARS = 500
MAX_GAP_CHARS = 300
LIMITS_SOURCE_ID = "grounding.limits"

# Four of the caps above are operator-settable, one onboarding proposal at a time, through
# ``bearhug-campaign onboard``.  Each entry is ``(lower, default, upper)``.  The lower bound is
# the current default's smaller half.  The upper bound is never open-ended and is justified from
# an existing byte or time ceiling this module already enforces, never from another tunable knob:
#
# - ``max_tool_terms`` bounds how many external processes one compile launches (Graft, then MemQ,
#   sequentially, per queried term).  Each launch is bounded by ``DEFAULT_TOOL_TIMEOUT_S``; the
#   upper bound keeps the worst case (both tools hang on every query) under roughly 4 minutes:
#   16 terms x 2 tools x 8.0s = 256s.
# - ``max_decisions`` bounds how many matched decisions are offered as packet sources, but a
#   decision offered is not always a decision sent: every packet source, decisions included,
#   still competes for the same ``MAX_SOURCES`` (32) cap, of which one slot is permanently
#   reserved for the ``grounding.limits`` row and the rest is shared with architecture, Graft and
#   MemQ's own observation rows.  Measured on a 102-decision corpus: with 38 decisions matched,
#   only 31 actually reached the packet -- the other 7 were cut by ``MAX_SOURCES``, not by this
#   cap.  A bound of 42 would promise a number most of which can never reach the packet at all;
#   24 stays well under the ``MAX_SOURCES - 1 = 31`` ceiling this task does not touch, leaving
#   room for architecture/Graft/MemQ's own rows in the same shared cap, while still meaningfully
#   larger than the default.  (This module still reports the true count truthfully either
#   way: ``report.memex.selection`` is exact, and any further cut is named in
#   ``report.source_selection``, unrelated to and unaffected by this bound.)  The lower bound (4)
#   and upper bound (24) are pinned by the owner's decision, not recomputed from "the current
#   default's smaller half" now that the default itself moved from 8 to 16:
#   4 is still comfortably below any real corpus's minimum useful count, and 24 is still the same
#   ``MAX_SOURCES``-anchored ceiling above.
# - ``graft_per_query_hits`` is passed straight through as Graft's own ``-n`` argument, so it
#   directly bounds how many hit objects one Graft process can emit before
#   ``default_tool_runner`` clips its captured stdout at ``MAX_TOOL_OUTPUT_BYTES``.  Assuming a
#   conservative 2048-byte JSON footprint per raw hit entry (well over the 200/40-character
#   title/kind fields this module truncates to, plus pointer and JSON structural overhead),
#   floor(262144 / 2048) = 128 keeps one query's requested hit count from being able to overflow
#   that capture on its own.
# - ``max_tool_hits`` bounds the distinct hit rows kept in one tool's report, aggregated across
#   every queried term.  The same conservative per-row footprint anchors it to the same ceiling:
#   floor(262144 / 2048) = 128.
TUNABLE_LIMIT_BOUNDS: dict[str, tuple[int, int, int]] = {
    "max_tool_terms": (2, MAX_TOOL_TERMS, 16),
    "max_decisions": (4, MAX_DECISIONS, 24),
    "graft_per_query_hits": (4, GRAFT_PER_QUERY_HITS, 128),
    "max_tool_hits": (20, MAX_TOOL_HITS, 128),
}

# A refusal names *why* an upper bound sits where it does when the reason is not obvious from the
# number alone. Only ``max_decisions`` needs one today: its bound is capped well below the byte
# arithmetic that would otherwise allow 42, by a cap this task does not touch (``MAX_SOURCES``).
TUNABLE_LIMIT_BOUND_REASONS: dict[str, str] = {
    "max_decisions": (
        "packet sources -- decisions included -- are capped at MAX_SOURCES=32 (one slot "
        "reserved for the limits row) and architecture, Graft and MemQ observations share that "
        "same cap; raising this past what can actually reach the packet cannot bind more "
        "decisions, only more may compete for the remaining source slots"
    ),
}

_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DECISION_FILE = re.compile(r"^(\d{4})-[a-z0-9][a-z0-9-]*\.md$")
_DECISION_MENTION = re.compile(r"(?<![0-9])(?:decision\s+)?(0\d{3})(?![0-9])")
_BACKTICK = re.compile(r"`([^`\n]{2,120})`")
_IDENTIFIER = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:[a-z][A-Z]|_)[A-Za-z0-9_]*)\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'-]{3,}")
_MEMQ_HIT = re.compile(r"^\[raw ([0-9.]+) → eff ([0-9.]+)\] (\S+) \| (.+?)\s*$")
_GRAFT_POINTER = re.compile(r"^[A-Za-z0-9_./-]+(?::L\d+(?:-L\d+)?)?$")

# Kind -> (default tier, required truth state).
KINDS: dict[str, tuple[str, str]] = {
    "accepted_binding": ("p1", "accepted"),
    "observation": ("p2", "observed"),
    "proposal": ("p3", "proposed"),
}
_PATH_ROW_FIELDS = frozenset(
    {"source_id", "path", "content_sha256", "kind", "tier", "truth_state", "reason", "projection"}
)
_INLINE_ROW_FIELDS = frozenset(
    {"source_id", "content", "content_sha256", "kind", "tier", "truth_state", "reason"}
)
_PROJECTIONS = frozenset({"decision", "full"})

_STOPWORDS = frozenset(
    {
    "about", "above", "after", "again", "against", "also", "although", "always", "among",
    "another", "around", "authority", "because", "been", "before", "being", "below", "between",
    "both", "build", "change", "changes", "check", "close", "complete", "create", "current",
    "decision", "decisions", "depends", "design", "done", "during", "each", "either", "every",
    "exact", "execute", "existing", "first", "following", "from", "further", "given", "have",
    "having", "here", "implement", "inside", "instead", "into", "itself", "keep", "known",
    "later", "least", "less", "like", "made", "make", "many", "means", "might", "more", "most",
    "must", "never", "next", "none", "obligation", "obligations", "only", "other", "over",
    "pass", "passes", "plan", "plans", "project", "ready", "record", "records", "report",
    "result", "results", "return", "same", "several", "should", "since", "some", "still",
    "such", "table", "task", "tasks", "test", "tests", "than", "that", "their", "them", "then",
    "there", "these", "they", "this", "those", "through", "true", "under", "until", "update",
    "upon", "used", "using", "value", "values", "verify", "when", "where", "whether", "which",
    "while", "with", "within", "without", "work", "would", "write", "written", "yield",
    }
)

ToolRunner = Callable[[Sequence[str], Path, float], "ToolResult"]


class GroundingError(ValueError):
    """A grounding input or sealed grounding row is invalid."""


# ---------------------------------------------------------------------------------------------
# Project-declared exclusions
#
# A project may declare paths (for example a private archive its own CLAUDE.md says must never
# be cited) that Bear Hug should never bind or surface, however they were discovered. Neither
# Graft nor MemQ is vendored here and neither is known to accept its own exclude/ignore option
# (see docs/RUNBOOK.md's setup section), so this module enforces the list itself, once, at the
# one place every bound source passes through: immediately before a row is appended to
# ``sources`` in ``compile_grounding``, and against every Graft pointer in ``run_graft``. MemQ's
# own recall hits carry only a label and basename (see ``_MEMQ_HIT``), never a full repository
# path, so a free-text MemQ hit cannot be matched against a path glob; only MemQ's
# *decision-linked* discoveries are covered, through the same decision-path filter every other
# consumer of ``read_decisions``/``select_decisions`` goes through. This gap is named in
# ``compile_grounding``'s report under ``excluded_by_project`` rather than silently accepted.

EXCLUDE_PATHS_ENV_KEY = "BEARHUG_EXCLUDE_PATHS"
SETUP_ENV_FILE = ".bearhug/setup.env"


def normalize_exclude_paths(values: Sequence[str] | None) -> tuple[str, ...]:
    """Validate project-declared exclusion globs: repository-relative, forward slashes only."""

    if not values:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise GroundingError("an exclude path must be a non-empty string")
        pure = PurePosixPath(value.rstrip("/"))
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise GroundingError(f"exclude path is unsafe or ambiguous: {value!r}")
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(sorted(result))


def _exclude_prefix(pattern: str) -> str:
    """The directory-style prefix a wildcard-free (or trailing-``*``) exclude pattern names."""

    return pattern.rstrip("*").rstrip("/")


def path_excluded(path: str, exclude_paths: Sequence[str]) -> bool:
    """True when ``path`` (repository-relative, forward slashes) matches a declared exclusion.

    A pattern with no glob metacharacters, or one that is only a trailing ``*``/``**``, is also
    read as a directory prefix: excluding ``_archive/private`` covers every path under it, not
    only a path spelled exactly that way.
    """

    for pattern in exclude_paths:
        if fnmatch.fnmatch(path, pattern):
            return True
        prefix = _exclude_prefix(pattern)
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            return True
    return False


def read_project_excludes(root: Path) -> tuple[str, ...]:
    """Read the project-declared exclusion globs setup persisted, or ``()`` if none/unreadable.

    Read-only and best-effort, matching this module's read-only contract: a missing or
    unreadable ``.bearhug/setup.env`` (or a missing key in it) means no exclusions, exactly the
    behaviour of a project that never declared any.
    """

    env_path = root / SETUP_ENV_FILE
    try:
        text = env_path.read_text()
    except OSError:
        return ()
    raw = None
    for line in text.splitlines():
        if line.startswith(f"{EXCLUDE_PATHS_ENV_KEY}="):
            raw = line.split("=", 1)[1]
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        return ()
    try:
        return normalize_exclude_paths(values)
    except GroundingError:
        return ()


def resolve_tunable_limit(name: str, value: int | None) -> int:
    """Validate one operator-settable grounding limit; ``None`` keeps today's default.

    An explicit value outside its bound (``TUNABLE_LIMIT_BOUNDS``) is refused, never clamped:
    the operator asked for a specific number, and silently substituting a different one would
    hide the refusal instead of reporting it. When the bound itself is capped by something other
    than its own byte/time arithmetic (``TUNABLE_LIMIT_BOUND_REASONS``), the refusal says so.
    """

    lower, default, upper = TUNABLE_LIMIT_BOUNDS[name]
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise GroundingError(f"{name} must be a whole number, not {value!r}")
    if not lower <= value <= upper:
        reason = TUNABLE_LIMIT_BOUND_REASONS.get(name, "")
        suffix = f"; {reason}" if reason else ""
        raise GroundingError(
            f"{name}={value} is outside the allowed range {lower}-{upper} "
            f"(default {default}){suffix}"
        )
    return value


#: ``task_terms_first`` is a settable behaviour flag, not a bounded number. Default ON: two
#: independent real-task measurements (mode tests on a real Go project) showed the plan's
#: task-table terms losing query slots to the plan's own explanatory prose, which then changed
#: which decisions bound. ``--no-task-terms-first`` (CLI) or ``task_terms_first=False`` (this
#: function) restores the previous ranking: term order, queried terms, MemQ's own discoveries
#: and the bound decision set identical to what this module produced before this option existed.
TASK_TERMS_FIRST_DEFAULT = True


def resolve_tunable_flag(name: str, value: bool | None, *, default: bool = False) -> bool:
    """Validate one operator-settable grounding flag; ``None`` keeps today's default."""

    if value is None:
        return default
    if not isinstance(value, bool):
        raise GroundingError(f"{name} must be true or false, not {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class ToolResult:
    """One external tool invocation, or the reason it did not run."""

    status: str  # ok | unavailable | failed | timeout
    stdout: str = ""
    reason: str = ""
    stdout_truncated: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    """One Memex decision record, read as bytes plus a shallow frontmatter projection."""

    decision_id: str
    path: str
    sha256: str
    raw: bytes
    title: str
    status: str
    tags: tuple[str, ...]
    execution: str | None
    ruling: str
    sections: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Grounding:
    """The compiled result: intent additions, packet sources and a closed report."""

    terms: tuple[str, ...]
    bindings: tuple[dict[str, Any], ...]
    invariants: tuple[dict[str, Any], ...]
    sources: tuple[dict[str, Any], ...]
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return _sha256(_canonical(self.to_mapping()))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "record_kind": RECORD_KIND,
            "terms": list(self.terms),
            "bindings": [dict(row) for row in self.bindings],
            "invariants": [dict(row) for row in self.invariants],
            "sources": [dict(row) for row in self.sources],
            "report": json.loads(_canonical(self.report)),
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _selection_shape(
    *,
    eligible_count: int | None,
    returned_count: int,
    limit: int | None,
    truncated: bool | None,
    count_basis: str,
) -> dict[str, Any]:
    """The one shape every cap in this module reports itself through.

    ``exact`` means everything eligible was counted locally; ``lower_bound`` means a local cap
    already cut the input the count was taken over, so the true count may be higher and
    ``truncated`` is always ``True``; ``unknown`` means an external tool applied its own cut, so
    ``eligible_count`` is always ``None`` and ``truncated`` is ``True`` only when a local cap is
    known to have dropped rows too. Callers compute ``truncated`` themselves: the rule differs
    by basis, and this helper only shapes the dict.
    """

    return {
        "eligible_count": eligible_count,
        "returned_count": returned_count,
        "limit": limit,
        "truncated": truncated,
        "count_basis": count_basis,
    }


# ---------------------------------------------------------------------------------------------
# Terms


def _distinct_ordered(items: Sequence[str]) -> list[str]:
    """Case-insensitive de-duplication that keeps the first spelling seen, in order."""

    result: list[str] = []
    seen: set[str] = set()
    for term in items:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(term)
    return result


def _candidate_terms(texts: Sequence[str]) -> list[str]:
    """Every distinct candidate term, ranked and ordered, before the ``MAX_TERMS`` cap."""

    spans: set[str] = set()
    identifiers: set[str] = set()
    decision_ids: set[str] = set()
    words: dict[str, int] = {}
    for text in texts:
        if not isinstance(text, str):
            continue
        for match in _BACKTICK.finditer(text):
            spans.add(match.group(1).strip())
        for match in _DECISION_MENTION.finditer(text):
            decision_ids.add(match.group(1))
        stripped = _BACKTICK.sub(" ", text)
        for match in _IDENTIFIER.finditer(stripped):
            token = match.group(1)
            if len(token) >= 4:
                identifiers.add(token)
        for match in _WORD.finditer(stripped):
            word = match.group(0).lower().strip("'-")
            if len(word) >= 5 and word not in _STOPWORDS and not word.isdigit():
                words[word] = words.get(word, 0) + 1
    ranked_words = sorted(words, key=lambda item: (-words[item], item))
    ordered = (
        sorted(decision_ids)
        + sorted(spans)
        + sorted(identifiers)
        + ranked_words
    )
    return _distinct_ordered(ordered)


def extract_terms(texts: Sequence[str]) -> list[str]:
    """Return bounded, sorted candidate terms: code spans, identifiers, decision ids, words."""

    return _candidate_terms(texts)[:MAX_TERMS]


def _task_table_terms(tasks: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Case-folded candidate terms drawn only from the plan's task table.

    ``tasks`` is exactly what :func:`compile_grounding` receives: rows the plan authoring
    contract requires to come from the ID/Task/Depends on/Done when table, never from other
    prose in the plan file. Used to rank task-table terms ahead of prose-only ones when filling
    the ``MAX_TOOL_TERMS`` slots; extraction itself (:func:`_candidate_terms`) is unchanged.
    """

    texts: list[str] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        for key in ("title", "done_when"):
            value = task.get(key)
            if isinstance(value, str):
                texts.append(value)
    return frozenset(term.lower() for term in _candidate_terms(texts))


def _ranked_tool_terms(
    terms: Sequence[str],
    *,
    task_terms: frozenset[str] = frozenset(),
    task_terms_first: bool = False,
) -> list[str]:
    """Every term worth a process launch, ranked, before the ``MAX_TOOL_TERMS`` cap.

    ``task_terms_first`` defaults to ``False``: today's ranking (code spans, identifiers and
    multi-word phrases first, then the longest ordinary words -- origin never considered) is
    unchanged unless an operator explicitly opts in. When ``True``, terms drawn from the plan's
    task table (``task_terms``) rank before terms found only in the plan's other prose; within
    each of those two groups the ranking is the same as always. A term found in both the task
    table and elsewhere in the plan still counts as task-table-origin, since it was in fact
    extracted from the task table.

    This is not purely cosmetic: which terms are queried can change which decisions MemQ
    discovers (``run_memq``'s ``discovered_decisions``), which changes which decisions
    ``select_decisions`` scores highly enough to bind -- measured directly, not assumed. That is
    why this ranking stays off by default rather than being today's only behaviour.
    """

    def sort_key(term: str) -> tuple:
        base = (
            -(" " in term or "_" in term or any(c.isupper() for c in term)),
            -len(term), term,
        )
        if task_terms_first:
            return (term.lower() not in task_terms, *base)
        return base

    return sorted(
        (term for term in terms if not re.fullmatch(r"0\d{3}", term)), key=sort_key,
    )


def _tool_terms(
    terms: Sequence[str],
    *,
    task_terms: frozenset[str] = frozenset(),
    task_terms_first: bool = False,
    max_tool_terms: int = MAX_TOOL_TERMS,
) -> list[str]:
    """Terms worth a process launch: code spans/identifiers/words, task-table terms first only
    when ``task_terms_first`` is explicitly enabled."""

    return _ranked_tool_terms(
        terms, task_terms=task_terms, task_terms_first=task_terms_first
    )[:max_tool_terms]


# ---------------------------------------------------------------------------------------------
# Memex decisions


def _frontmatter(raw: bytes) -> tuple[dict[str, Any], str]:
    """Parse the YAML subset the Memex schema uses; return (fields, body)."""

    text = raw.decode("utf-8", errors="replace")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    block = text[4:end]
    body = text[end + 4 :]
    fields: dict[str, Any] = {}
    key: str | None = None
    block_lines: list[str] | None = None
    for line in block.split("\n"):
        if block_lines is not None:
            if line.startswith("  ") or line.strip() == "":
                block_lines.append(line[2:] if line.startswith("  ") else "")
                continue
            fields[key] = "\n".join(block_lines).strip()
            block_lines = None
            key = None
        if line.startswith("  - ") and key is not None and isinstance(fields.get(key), list):
            fields[key].append(line[4:].strip())
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value == "|":
            block_lines = []
        elif value == "":
            fields[key] = []
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fields[key] = [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
        else:
            fields[key] = value.strip().strip('"')
    if block_lines is not None and key is not None:
        fields[key] = "\n".join(block_lines).strip()
    return fields, body


def _sections(body: str) -> dict[str, str]:
    result: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in body.split("\n"):
        if line.startswith("## "):
            if current is not None:
                result[current] = "\n".join(lines).strip()
            current = line[3:].strip().lower()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        result[current] = "\n".join(lines).strip()
    return result


def read_decisions(root: Path) -> list[Decision]:
    """Read every decision record under ``docs/memex/decisions`` as exact bytes."""

    directory = root / DECISION_DIR
    if directory.is_symlink() or not directory.is_dir():
        return []
    result: list[Decision] = []
    for path in sorted(directory.iterdir()):
        match = _DECISION_FILE.match(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size > MAX_DECISION_FILE_BYTES:
            continue
        raw = path.read_bytes()
        fields, body = _frontmatter(raw)
        title = fields.get("title") if isinstance(fields.get("title"), str) else ""
        status = fields.get("status") if isinstance(fields.get("status"), str) else "unknown"
        tags = fields.get("tags") if isinstance(fields.get("tags"), list) else []
        execution = fields.get("execution") if isinstance(fields.get("execution"), str) else None
        ruling = (
            fields.get("ruling_verbatim") if isinstance(fields.get("ruling_verbatim"), str) else ""
        )
        result.append(
            Decision(
                decision_id=match.group(1),
                path=f"{DECISION_DIR}/{path.name}",
                sha256=_sha256(raw),
                raw=raw,
                title=title or path.stem,
                status=status,
                tags=tuple(str(tag) for tag in tags),
                execution=execution,
                ruling=ruling,
                sections=_sections(body),
            )
        )
    return result


def _skipped_decision_files(root: Path) -> list[dict[str, str]]:
    """Decision-named files ``read_decisions`` silently skips, with the exact reason.

    Mirrors that function's own skip order: a symlink is reported as ``symlink`` even when it
    would also fail the regular-file check, exactly as that check short-circuits there.
    """

    directory = root / DECISION_DIR
    if directory.is_symlink() or not directory.is_dir():
        return []
    skipped: list[dict[str, str]] = []
    for path in sorted(directory.iterdir()):
        if not _DECISION_FILE.match(path.name):
            continue
        relative = f"{DECISION_DIR}/{path.name}"
        if path.is_symlink():
            skipped.append({"path": relative, "reason": "symlink"})
        elif not path.is_file():
            skipped.append({"path": relative, "reason": "not_regular"})
        elif path.stat().st_size > MAX_DECISION_FILE_BYTES:
            skipped.append({"path": relative, "reason": "oversize"})
    return skipped


def _word_in(text: str, term: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])", text,
                     re.IGNORECASE) is not None


def _distinctive(term: str) -> bool:
    """A code span, identifier or path is worth more than an ordinary word."""

    return (" " in term or "_" in term or "-" in term or "/" in term or "." in term
            or any(c.isupper() for c in term[1:]))


def _term_weights(decisions: Sequence[Decision], terms: Sequence[str]) -> dict[str, int]:
    """Weight each term by distinctiveness and by rarity across decision titles and tags.

    A word that names a tenth of a large corpus ("authority", "codec") cannot single out a
    record, so it scores nothing on its own; a rare word scores 2 and a code span or identifier
    3.  Thresholds carry absolute floors so a three-record corpus is not read as "everything is
    common".
    """

    corpus = [d.title + " " + " ".join(d.tags) for d in decisions]
    total = len(corpus)
    common_at = max(4, -(-total * 15 // 100))
    mid_at = max(2, -(-total * 5 // 100))
    weights: dict[str, int] = {}
    for term in terms:
        if re.fullmatch(r"0\d{3}", term):
            continue
        frequency = sum(1 for text in corpus if _word_in(text, term))
        base = 3 if _distinctive(term) else 2
        if frequency >= common_at:
            weights[term] = 0
        elif frequency >= mid_at:
            weights[term] = 1
        else:
            weights[term] = base
    return weights


#: The exact rule ``select_decisions`` breaks a tied score group with, in words. Reused
#: verbatim by the docs and by ``compile_grounding``'s report so the report and the
#: documentation can never say the rule differently.
TIE_BREAK_RULE = "equal scores are ordered by decision id, lower first"


@dataclass(frozen=True, slots=True)
class DecisionSelection:
    """Every decision that scored, and how many the ``MAX_DECISIONS`` cap left out.

    ``matches`` is exactly what ``match_decisions`` has always returned: bounded, ordered by
    score then id. ``dropped`` is every eligible match the cap cut, in the same order, so a
    reader can tell "9 eligible, 1 dropped" from "8 eligible". ``tie_at_cut`` is true exactly
    when the last kept match and the first dropped match share the same score: the cap did not
    just drop the weakest matches, it also chose among decisions that scored identically, by
    ``TIE_BREAK_RULE``.

    ``details`` carries, per matched-or-dropped decision id, why it bound: ``path``/``paths``
    (which signal(s) produced the score), ``score_breakdown`` (the four addends, summing to the
    match's score), ``lexical_score`` (the title+body subtotal the accumulated-lexical ``strong``
    clause tests) and ``strong_reason`` (which clause satisfied ``strong``). See GS-02's
    recommendation, section 2.2 of its decision record. Kept separate from the
    ``(Decision, score, hits)`` tuples above rather than widening them, so every existing caller
    that only wants the tuple shape keeps working unchanged.
    """

    matches: tuple[tuple[Decision, int, tuple[str, ...]], ...]
    eligible_count: int
    returned_count: int
    limit: int
    truncated: bool
    count_basis: str
    dropped: tuple[tuple[Decision, int, tuple[str, ...]], ...]
    tie_at_cut: bool = False
    details: Mapping[str, dict[str, Any]] = field(default_factory=dict)


def select_decisions(
    decisions: Sequence[Decision],
    terms: Sequence[str],
    *,
    discovered_ids: Sequence[str] = (),
    minimum_score: int = MINIMUM_SCORE_DEFAULT,
    strong_lexical_score: int | None = None,
    max_decisions: int = MAX_DECISIONS,
) -> DecisionSelection:
    """Score every decision lexically against the terms; keep today's ordering and cap.

    An explicit decision id in the plan or a MemQ discovery scores 3 on its own.  Otherwise a
    record needs one rare word in its title or tags plus one more hit, or a distinctive code
    span or identifier anywhere in its ruling or Decision section -- or (GS-02's recommended
    default) enough *accumulated* lexical evidence: ``lexical_score`` (title/tag
    hits at full weight, plus body hits at half weight) reaching ``strong_lexical_score``
    (default ``minimum_score + STRONG_LEXICAL_SCORE_MARGIN``) also satisfies ``strong``, even when
    no single word was individually rare or distinctive enough. This deliberately reverses the
    older reading ("common words alone never select a record"): the empirical justification is
    that the strict reading caused 73% of measured misses on GS-02's evaluation, including the one
    real miss GS-01 named.
    """

    explicit = {term for term in terms if re.fullmatch(r"0\d{3}", term)}
    discovered = set(discovered_ids)
    weights = _term_weights(decisions, terms)
    effective_strong_lexical_score = (
        strong_lexical_score
        if strong_lexical_score is not None
        else minimum_score + STRONG_LEXICAL_SCORE_MARGIN
    )
    matched: list[tuple[Decision, int, tuple[str, ...]]] = []
    details: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        if decision.status not in {"accepted", "proposed"}:
            continue
        is_explicit = decision.decision_id in explicit
        is_discovered = decision.decision_id in discovered
        score = 0
        hits: list[str] = []
        explicit_score = 0
        discovered_score = 0
        title_score = 0
        body_score = 0
        hit_terms_title: list[str] = []
        hit_terms_body: list[str] = []
        if is_explicit:
            score += 3
            explicit_score = 3
            hits.append(decision.decision_id)
        if is_discovered:
            score += 3
            discovered_score = 3
        haystack_title = decision.title + " " + " ".join(decision.tags)
        body = decision.ruling + "\n" + decision.sections.get("decision", "")
        strong_rare_title = False
        strong_distinctive_body = False
        for term, weight in weights.items():
            if weight == 0:
                continue
            if _word_in(haystack_title, term):
                score += weight
                title_score += weight
                hits.append(term)
                hit_terms_title.append(term)
                strong_rare_title = strong_rare_title or weight >= 2
            elif weight >= 2 and body.strip() and _word_in(body, term):
                # The verbatim ruling and the Decision section count half; only a code span or
                # identifier found there can select a record on its own -- or contribute to the
                # accumulated-lexical clause below.
                score += weight // 2
                body_score += weight // 2
                hits.append(term)
                hit_terms_body.append(term)
                strong_distinctive_body = strong_distinctive_body or weight >= 3
        lexical_score = title_score + body_score
        # Enough accumulated lexical evidence, title and body combined, is its own kind of
        # distinctiveness -- GS-02's recommended default. See the module-level constants.
        strong_accumulated = lexical_score >= effective_strong_lexical_score
        strong = (
            is_explicit
            or is_discovered
            or strong_rare_title
            or strong_distinctive_body
            or strong_accumulated
        )
        if strong and score >= minimum_score and (hits or is_discovered):
            matched.append((decision, score, tuple(sorted(set(hits)))))
            paths: list[str] = []
            if is_explicit:
                paths.append("explicit_id")
            if is_discovered:
                paths.append("memq_discovered")
            if hit_terms_title:
                paths.append("lexical_title_tag")
            if hit_terms_body:
                paths.append("lexical_body")
            path = "none" if not paths else ("combination" if len(paths) > 1 else paths[0])
            if is_explicit:
                strong_reason = "explicit"
            elif is_discovered:
                strong_reason = "discovered"
            elif strong_rare_title:
                strong_reason = "rare_title_term"
            elif strong_distinctive_body:
                strong_reason = "distinctive_body_term"
            else:
                strong_reason = "accumulated_lexical"
            details[decision.decision_id] = {
                "path": path,
                "paths": paths,
                "score_breakdown": {
                    "explicit": explicit_score,
                    "discovered": discovered_score,
                    "lexical_title_tag": title_score,
                    "lexical_body": body_score,
                },
                "lexical_score": lexical_score,
                "strong_reason": strong_reason,
                "hit_terms_title": sorted(set(hit_terms_title)),
                "hit_terms_body": sorted(set(hit_terms_body)),
            }
    matched.sort(key=lambda item: (-item[1], item[0].decision_id))
    eligible = len(matched)
    kept = matched[:max_decisions]
    dropped = matched[max_decisions:]
    # The cut falls inside a tied score group exactly when the weakest kept match and the
    # strongest dropped match scored the same: the cap then chose between them by decision id,
    # not by any difference in how well they matched.
    tie_at_cut = bool(kept and dropped and kept[-1][1] == dropped[0][1])
    return DecisionSelection(
        matches=tuple(kept),
        eligible_count=eligible,
        returned_count=len(kept),
        limit=max_decisions,
        truncated=eligible > max_decisions,
        count_basis="exact",
        dropped=tuple(dropped),
        tie_at_cut=tie_at_cut,
        details=details,
    )


def match_decisions(
    decisions: Sequence[Decision],
    terms: Sequence[str],
    *,
    discovered_ids: Sequence[str] = (),
    minimum_score: int = MINIMUM_SCORE_DEFAULT,
) -> list[tuple[Decision, int, tuple[str, ...]]]:
    """Score decisions lexically against the terms; return the bounded, ordered matches.

    A thin wrapper over :func:`select_decisions` kept for existing callers; identical output.
    """

    return list(
        select_decisions(
            decisions, terms, discovered_ids=discovered_ids, minimum_score=minimum_score
        ).matches
    )


def project_decision(raw: bytes, *, max_bytes: int = MAX_PROJECTION_BYTES) -> bytes:
    """Bounded projection of one record: identity frontmatter, ruling, decision and prohibitions."""

    fields, body = _frontmatter(raw)
    sections = _sections(body)
    lines = ["---"]
    for key in ("title", "id", "status", "date", "execution", "supersedes", "superseded_by"):
        if key in fields:
            value = fields[key]
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    if isinstance(fields.get("ruling_verbatim"), str) and fields["ruling_verbatim"]:
        lines += ["", "## Ruling (verbatim)", fields["ruling_verbatim"]]
    for heading in ("decision", "what this forbids", "consequences"):
        if sections.get(heading):
            lines += ["", f"## {heading.title()}", sections[heading]]
    text = "\n".join(lines) + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        marker = b"\n[projection truncated; the full record is at the sealed path]\n"
        head = encoded[: max_bytes - len(marker)].decode("utf-8", errors="ignore")
        encoded = head.encode("utf-8") + marker
    return encoded


# ---------------------------------------------------------------------------------------------
# Architecture index, Graft, MemQ


def _git_head(root: Path) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        completed = subprocess.run(
            [git, "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", value) else None


def _refused_term(selected: Mapping[str, Any]) -> bool:
    """True when a per-term ``select()`` call refused to look, not just found no match.

    ``select()`` (``bearhug/arch/context.py``) returns ``status: "index_gap"`` for three
    reasons: an empty query, an unusable index (stale, partial, structurally unknown, or a
    commit mismatch -- the freshness problems, as gaps), or a genuine no-match search of a
    fresh, usable index. Only the first two mean this term's records were never counted; the
    third is a real, exact zero inside the index's own coverage. All three carry their reason
    as plain gap strings, and only the genuine-no-match one is prefixed ``no_query_match``.
    """

    if selected.get("status") != "index_gap":
        return False
    term_gaps = selected.get("gaps", [])
    return not (
        term_gaps and all(isinstance(g, str) and g.startswith("no_query_match") for g in term_gaps)
    )


def architecture_context(
    root: Path,
    terms: Sequence[str],
    *,
    head: str | None,
    task_terms: frozenset[str] = frozenset(),
    task_terms_first: bool = False,
    max_tool_terms: int = MAX_TOOL_TERMS,
) -> dict[str, Any]:
    """Select cited architecture records for the terms through the existing A05 selector."""

    # Nothing was counted in any of these states, so the count is unknown, not an exact zero.
    unknown_selection = _selection_shape(
        eligible_count=None, returned_count=0, limit=MAX_ARCHITECTURE_RECORDS,
        truncated=None, count_basis="unknown",
    )
    unknown_gap_selection = _selection_shape(
        eligible_count=None, returned_count=0, limit=MAX_ARCHITECTURE_GAPS,
        truncated=None, count_basis="unknown",
    )
    for relative in (ARCHITECTURE_INDEX, CANONICAL_ARCHITECTURE_INDEX):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue
        try:
            artifact = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"status": "unavailable", "reason": f"{relative}: unreadable ({exc})",
                    "index": relative, "records": [], "gaps": [],
                    "selection": unknown_selection, "gap_selection": unknown_gap_selection}
        try:
            from bearhug.arch.context import select
        except ImportError:
            return {"status": "unavailable", "reason": "architecture selector is not importable",
                    "index": relative, "records": [], "gaps": [],
                    "selection": unknown_selection, "gap_selection": unknown_gap_selection}
        records: dict[str, dict[str, Any]] = {}
        gaps: list[str] = []
        freshness: dict[str, Any] | None = None
        any_results_truncated = False
        any_term_refused = False
        for term in _tool_terms(
            terms, task_terms=task_terms, task_terms_first=task_terms_first,
            max_tool_terms=max_tool_terms,
        ):
            selected = select(artifact, term, head=head)
            freshness = selected.get("freshness", freshness)
            if _refused_term(selected):
                any_term_refused = True
            for gap in selected.get("gaps", []):
                if isinstance(gap, str) and gap.startswith("results_truncated"):
                    any_results_truncated = True
                gaps.append(f"{term}: {gap}")
            for record in selected.get("records", []):
                identity = str(record.get("id"))
                records.setdefault(identity, {
                    "id": identity,
                    "kind": record.get("kind"),
                    "provenance": record.get("provenance"),
                    "matched_terms": [],
                })
                records[identity]["matched_terms"].append(term)
        rows = [records[key] for key in sorted(records)]
        for row in rows:
            row["matched_terms"] = sorted(set(row["matched_terms"]))
        distinct_gaps = sorted(set(gaps))
        if any_term_refused:
            # At least one queried term's records were never counted at all, so the total is
            # not observable -- even though other terms may genuinely have matched. Only a
            # local, provable fact (the aggregated rows alone already over the cap) can still
            # prove truncation; it is never inferred from the refusal itself.
            record_selection = _selection_shape(
                eligible_count=None,
                returned_count=len(rows[:MAX_ARCHITECTURE_RECORDS]),
                limit=MAX_ARCHITECTURE_RECORDS,
                truncated=True if len(rows) > MAX_ARCHITECTURE_RECORDS else None,
                count_basis="unknown",
            )
        else:
            # A per-term selector call that hit its own cap already cut what we aggregate
            # here, so our own distinct-record count can only be a lower bound of the true
            # eligible set.
            record_basis = "lower_bound" if any_results_truncated else "exact"
            record_selection = _selection_shape(
                eligible_count=len(rows),
                returned_count=len(rows[:MAX_ARCHITECTURE_RECORDS]),
                limit=MAX_ARCHITECTURE_RECORDS,
                truncated=(
                    True if record_basis == "lower_bound"
                    else len(rows) > MAX_ARCHITECTURE_RECORDS
                ),
                count_basis=record_basis,
            )
        # The gaps themselves are always fully and exactly observed: refusal produces gap
        # strings rather than hiding them, so this count never becomes "unknown".
        gap_selection = _selection_shape(
            eligible_count=len(distinct_gaps),
            returned_count=len(distinct_gaps[:MAX_ARCHITECTURE_GAPS]),
            limit=MAX_ARCHITECTURE_GAPS,
            truncated=len(distinct_gaps) > MAX_ARCHITECTURE_GAPS,
            count_basis="exact",
        )
        return {
            "status": "ok" if rows else "no_match",
            "reason": "" if rows else "no fresh record matched any term",
            "index": relative,
            "freshness": freshness,
            "records": rows[:MAX_ARCHITECTURE_RECORDS],
            "gaps": distinct_gaps[:MAX_ARCHITECTURE_GAPS],
            "selection": record_selection,
            "gap_selection": gap_selection,
        }
    return {"status": "unavailable", "reason": "no architecture index is present",
            "index": None, "records": [], "gaps": [],
            "selection": unknown_selection, "gap_selection": unknown_gap_selection}


def default_tool_runner(argv: Sequence[str], cwd: Path, timeout_s: float) -> ToolResult:
    """Run one launcher without a shell; absence and failure are reported, never raised."""

    executable = Path(argv[0])
    if not executable.is_file():
        return ToolResult("unavailable", reason=f"launcher missing: {argv[0]}")
    try:
        completed = subprocess.run(
            list(argv), cwd=str(cwd), capture_output=True,
            timeout=timeout_s, check=False, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return ToolResult("timeout", reason=f"exceeded {timeout_s:g}s")
    except OSError as exc:
        return ToolResult("failed", reason=f"launch failed: {exc}")
    # Tools truncate snippets mid-character; decode leniently, the bytes are never authority.
    clipped = len(completed.stdout) > MAX_TOOL_OUTPUT_BYTES
    stdout = completed.stdout[:MAX_TOOL_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr = completed.stderr[:4096].decode("utf-8", errors="replace")
    if completed.returncode != 0:
        return ToolResult("failed", reason=stderr.strip()[:500] or f"exit {completed.returncode}",
                           stdout_truncated=clipped)
    return ToolResult("ok", stdout=stdout, stdout_truncated=clipped)


def _hit_selection(
    local_candidates: int, returned_count: int, *, max_tool_hits: int = MAX_TOOL_HITS
) -> dict[str, Any]:
    """The shared shape for a tool's hit list: an external cut, so the total is never known."""

    return _selection_shape(
        eligible_count=None,
        returned_count=returned_count,
        limit=max_tool_hits,
        truncated=True if local_candidates > max_tool_hits else None,
        count_basis="unknown",
    )


def _aborted_hit_selection(*, max_tool_hits: int = MAX_TOOL_HITS) -> dict[str, Any]:
    """The hit-list shape when a tool going unavailable discarded everything found so far.

    The item cap never ran on this path: nothing was kept because the tool became unavailable,
    not because the cap cut it, so ``truncated`` cannot be derived from a local candidate count
    the way :func:`_hit_selection` does for a completed loop.
    """

    return _selection_shape(
        eligible_count=None, returned_count=0, limit=max_tool_hits,
        truncated=None, count_basis="unknown",
    )


def _hits_by_term(counts: Mapping[str, int], *, at_limit: Sequence[str]) -> list[dict[str, Any]]:
    """Per-query hit counts, bounded the same way the dropped-decisions list is.

    Neither Graft nor MemQ reports a total match count beyond what one query returned, so
    ``at_least`` is the only truthful qualifier available: it is set exactly for a query whose
    raw response reached a known per-query cap, meaning more may exist beyond what was returned.
    A query below that cap (or a tool with no per-query cap at all) reports its exact count.
    """

    at_limit_set = set(at_limit)
    rows = [
        {"term": term, "returned": count, "at_least": term in at_limit_set}
        for term, count in sorted(counts.items())
    ]
    return rows[:MAX_DROPPED_DECISIONS_LISTED]


def _tool_coverage_fields(
    *,
    per_query_limit: int | None,
    queries_at_limit: Sequence[str],
    excluded_count: int,
    clipped_queries: Sequence[str],
    failed_queries: Sequence[Mapping[str, str]],
    excluded_by_project: int = 0,
) -> dict[str, Any]:
    """The parts of a Graft/MemQ report assembled identically by the early-abort and final
    return: only ``hits``/``terms``/``selection``/``local_candidates``/``status``/``reason``/
    ``partial`` differ between those two paths.

    ``excluded_count`` is every locally-dropped hit (a malformed pointer, the hardcoded
    ``_archive/`` convention, or a project-declared exclusion); ``excluded_by_project`` is the
    subset of that count attributable to the project's own declared list, reported separately so
    a project that declares no exclusions sees the same numbers it always has.
    """

    return {
        "per_query_limit": per_query_limit,
        "queries_at_limit": sorted(queries_at_limit),
        "excluded_count": excluded_count,
        "excluded_by_project": excluded_by_project,
        "clipped_queries": sorted(clipped_queries),
        "failed_queries": sorted(failed_queries, key=lambda row: row["term"]),
    }


def run_graft(
    root: Path,
    terms: Sequence[str],
    *,
    runner: ToolRunner,
    timeout_s: float,
    task_terms: frozenset[str] = frozenset(),
    task_terms_first: bool = False,
    max_tool_terms: int = MAX_TOOL_TERMS,
    graft_per_query_hits: int = GRAFT_PER_QUERY_HITS,
    max_tool_hits: int = MAX_TOOL_HITS,
    exclude_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Ask the Graft code map for each tool term; keep pointers and titles, never bodies."""

    launcher = root / "scripts/bin/graft"
    hits: dict[str, dict[str, Any]] = {}
    statuses: list[str] = []
    failed_queries: list[dict[str, str]] = []
    queries_at_limit: list[str] = []
    clipped_queries: list[str] = []
    excluded_count = 0
    excluded_by_project = 0
    raw_counts: dict[str, int] = {}
    queried_terms = _tool_terms(
        terms, task_terms=task_terms, task_terms_first=task_terms_first,
        max_tool_terms=max_tool_terms,
    )
    for term in queried_terms:
        result = runner(
            [str(launcher), "ask", term, "--json", "--no-refresh", "-n",
             str(graft_per_query_hits), str(root)],
            root, timeout_s,
        )
        if result.stdout_truncated:
            clipped_queries.append(term)
        if result.status != "ok":
            statuses.append(f"{term}: {result.status} {result.reason}".strip())
            failed_queries.append(
                {"term": term, "status": result.status, "reason": result.reason}
            )
            if result.status == "unavailable":
                return {
                    "status": "unavailable", "reason": result.reason, "hits": [], "terms": [],
                    "selection": _aborted_hit_selection(max_tool_hits=max_tool_hits),
                    "local_candidates": len(hits),
                    "hits_by_term": _hits_by_term(raw_counts, at_limit=queries_at_limit),
                    **_tool_coverage_fields(
                        per_query_limit=graft_per_query_hits, queries_at_limit=queries_at_limit,
                        excluded_count=excluded_count, clipped_queries=clipped_queries,
                        failed_queries=failed_queries, excluded_by_project=excluded_by_project,
                    ),
                    "partial": False,
                }
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            statuses.append(f"{term}: non-JSON output")
            failed_queries.append({"term": term, "status": "failed", "reason": "non-JSON output"})
            continue
        # The external per-query cap binds on what the tool returned, not on what survives
        # the local pointer/`_archive/`/project-exclude filter below: a query that answers 8
        # raw hits is at the limit even when some of them are excluded afterwards.
        raw_hits = payload.get("hits", []) if isinstance(payload, Mapping) else []
        raw_counts[term] = len(raw_hits)
        if len(raw_hits) >= graft_per_query_hits:
            queries_at_limit.append(term)
        for hit in raw_hits:
            pointer = str(hit.get("pointer", ""))
            if not _GRAFT_POINTER.match(pointer) or pointer.startswith("_archive/"):
                excluded_count += 1
                continue
            pointer_path = pointer.split(":L", 1)[0]
            if exclude_paths and path_excluded(pointer_path, exclude_paths):
                excluded_count += 1
                excluded_by_project += 1
                continue
            row = hits.setdefault(pointer, {
                "pointer": pointer,
                "title": str(hit.get("title", ""))[:200],
                "kind": str(hit.get("kind", ""))[:40],
                "matched_terms": [],
            })
            row["matched_terms"].append(term)
    rows = [hits[key] for key in sorted(hits)]
    for row in rows:
        row["matched_terms"] = sorted(set(row["matched_terms"]))
    local_candidates = len(rows)
    kept = rows[:max_tool_hits]
    status = "ok" if rows else ("failed" if statuses else "no_match")
    return {
        "status": status,
        "reason": "; ".join(statuses)[:1000],
        "hits": kept,
        "terms": queried_terms,
        "selection": _hit_selection(local_candidates, len(kept), max_tool_hits=max_tool_hits),
        "local_candidates": local_candidates,
        "hits_by_term": _hits_by_term(raw_counts, at_limit=queries_at_limit),
        **_tool_coverage_fields(
            per_query_limit=graft_per_query_hits, queries_at_limit=queries_at_limit,
            excluded_count=excluded_count, clipped_queries=clipped_queries,
            failed_queries=failed_queries, excluded_by_project=excluded_by_project,
        ),
        "partial": status == "ok" and bool(failed_queries or clipped_queries),
    }


def run_memq(
    root: Path,
    terms: Sequence[str],
    *,
    runner: ToolRunner,
    timeout_s: float,
    task_terms: frozenset[str] = frozenset(),
    task_terms_first: bool = False,
    max_tool_terms: int = MAX_TOOL_TERMS,
    max_tool_hits: int = MAX_TOOL_HITS,
) -> dict[str, Any]:
    """Query MemQ recall per tool term; keep labels, basenames and scores, resolve decisions."""

    launcher = root / "scripts/bin/memq"
    hits: dict[str, dict[str, Any]] = {}
    discovered: set[str] = set()
    statuses: list[str] = []
    failed_queries: list[dict[str, str]] = []
    clipped_queries: list[str] = []
    raw_counts: dict[str, int] = {}
    queried_terms = _tool_terms(
        terms, task_terms=task_terms, task_terms_first=task_terms_first,
        max_tool_terms=max_tool_terms,
    )
    for term in queried_terms:
        result = runner([str(launcher), "query", term], root, timeout_s)
        if result.stdout_truncated:
            clipped_queries.append(term)
        if result.status != "ok":
            statuses.append(f"{term}: {result.status} {result.reason}".strip())
            failed_queries.append(
                {"term": term, "status": result.status, "reason": result.reason}
            )
            if result.status == "unavailable":
                return {
                    "status": "unavailable", "reason": result.reason, "hits": [],
                    "discovered_decisions": [], "terms": [],
                    "selection": _aborted_hit_selection(max_tool_hits=max_tool_hits),
                    "local_candidates": len(hits),
                    "hits_by_term": _hits_by_term(raw_counts, at_limit=()),
                    **_tool_coverage_fields(
                        per_query_limit=None, queries_at_limit=(), excluded_count=0,
                        clipped_queries=clipped_queries, failed_queries=failed_queries,
                    ),
                    "partial": False,
                }
            continue
        term_count = 0
        for line in result.stdout.split("\n"):
            match = _MEMQ_HIT.match(line)
            if not match:
                continue
            term_count += 1
            label, basename = match.group(3), match.group(4)
            key = f"{label}|{basename}"
            row = hits.setdefault(key, {
                "label": label[:40], "basename": basename[:200],
                "score": float(match.group(2)), "matched_terms": [],
            })
            row["score"] = max(row["score"], float(match.group(2)))
            row["matched_terms"].append(term)
            decision = _DECISION_FILE.match(basename)
            if decision and (root / DECISION_DIR / basename).is_file():
                discovered.add(decision.group(1))
        raw_counts[term] = term_count
    rows = [hits[key] for key in sorted(hits)]
    for row in rows:
        row["matched_terms"] = sorted(set(row["matched_terms"]))
    local_candidates = len(rows)
    kept = rows[:max_tool_hits]
    status = "ok" if rows else ("failed" if statuses else "no_match")
    return {
        "status": status,
        "reason": "; ".join(statuses)[:1000],
        "hits": kept,
        "discovered_decisions": sorted(discovered),
        "terms": queried_terms,
        "selection": _hit_selection(local_candidates, len(kept), max_tool_hits=max_tool_hits),
        "local_candidates": local_candidates,
        # MemQ has no per-query cap of its own (unlike Graft's ``-n``), so no query here can be
        # truthfully called "at least": every count below is exactly what that one query matched.
        "hits_by_term": _hits_by_term(raw_counts, at_limit=()),
        **_tool_coverage_fields(
            per_query_limit=None, queries_at_limit=(), excluded_count=0,
            clipped_queries=clipped_queries, failed_queries=failed_queries,
        ),
        "partial": status == "ok" and bool(failed_queries or clipped_queries),
    }


# ---------------------------------------------------------------------------------------------
# Compile


def _row_count(payload: Mapping[str, Any]) -> int:
    """How many selected rows a payload carries, under whichever key holds them."""

    for key in ("records", "hits"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _inline_attempt(
    source_id: str, payload: Mapping[str, Any], reason: str
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build one inline packet row, and truthfully record whether it fit.

    Every attempt is reported, even the ones that fail: ``embedded`` fits whole, ``trimmed``
    fits once its row list is cut to 10, ``omitted`` means neither fit and the row is dropped.
    """

    rows_before = _row_count(payload)
    content = _canonical(payload).decode("utf-8")
    raw_bytes = len(content.encode("utf-8"))
    if raw_bytes <= MAX_INLINE_BYTES:
        row = {
            "source_id": source_id,
            "content": content,
            "content_sha256": _sha256(content.encode("utf-8")),
            "kind": "observation",
            "tier": "p2",
            "truth_state": "observed",
            "reason": reason,
        }
        outcome = {
            "source_id": source_id, "outcome": "embedded", "bytes": raw_bytes,
            "limit": MAX_INLINE_BYTES, "rows_before": rows_before, "rows_after": rows_before,
        }
        return row, outcome
    trimmed = dict(payload)
    for key in ("records", "hits"):
        if isinstance(trimmed.get(key), list):
            trimmed[key] = trimmed[key][:10]
    trimmed["truncated"] = True
    trimmed_content = _canonical(trimmed).decode("utf-8")
    trimmed_bytes = len(trimmed_content.encode("utf-8"))
    rows_after = _row_count(trimmed)
    if trimmed_bytes <= MAX_INLINE_BYTES:
        row = {
            "source_id": source_id,
            "content": trimmed_content,
            "content_sha256": _sha256(trimmed_content.encode("utf-8")),
            "kind": "observation",
            "tier": "p2",
            "truth_state": "observed",
            "reason": reason,
        }
        outcome = {
            "source_id": source_id, "outcome": "trimmed", "bytes": trimmed_bytes,
            "limit": MAX_INLINE_BYTES, "rows_before": rows_before, "rows_after": rows_after,
        }
        return row, outcome
    outcome = {
        "source_id": source_id, "outcome": "omitted", "bytes": trimmed_bytes,
        "limit": MAX_INLINE_BYTES, "rows_before": rows_before, "rows_after": rows_after,
    }
    return None, outcome


def _inline_source(
    source_id: str, payload: Mapping[str, Any], reason: str
) -> dict[str, Any] | None:
    """Thin wrapper over :func:`_inline_attempt` for callers that only need the row."""

    row, _outcome = _inline_attempt(source_id, payload, reason)
    return row


_LIMITS_TOOL_FIELDS = (
    "status", "reason", "selection", "gaps", "gap_selection",
    "dropped", "skipped", "failed_queries", "partial",
    "local_candidates", "per_query_limit", "queries_at_limit", "excluded_count",
    "clipped_queries", "hits_by_term", "tie_at_cut", "tie_break_rule",
)

_LIMITS_REASON = (
    "This row lists what grounding did not or could not look at. Source counts exclude this "
    "row itself. An empty result proves absence only inside that coverage."
)

#: The wall-clock field names no sealed grounding digest may depend on. This is the one
#: authority for that set: ``project_onboarding._stable_grounding`` imports and uses it rather
#: than keeping its own copy, since the two were previously two names for the same four fields.
VOLATILE_GROUNDING_FIELDS = frozenset({"updated_at", "observed_at", "compiled_at", "age_seconds"})


def _stable_freshness(value: Any) -> Any:
    """Copy dropping wall-clock fields, mirroring ``project_onboarding._stable_grounding``.

    This row embeds freshness as an opaque JSON string. The outer pass that strips wall-clock
    fields when sealing grounding into a draft cannot see through a string once it is
    serialized, so a clock field left inside it would still shift the draft digest on every
    identical derivation. Strip it here, before serialization.
    """

    if isinstance(value, Mapping):
        return {
            key: _stable_freshness(inner)
            for key, inner in value.items()
            if key not in VOLATILE_GROUNDING_FIELDS
        }
    if isinstance(value, list):
        return [_stable_freshness(item) for item in value]
    return value


def _bounded_text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _limits_tool_view(tool_report: Mapping[str, Any]) -> dict[str, Any]:
    """A bounded copy of one tool's report, holding only the fields ``grounding.limits`` names."""

    view: dict[str, Any] = {}
    for key in _LIMITS_TOOL_FIELDS:
        if key not in tool_report:
            continue
        value = tool_report[key]
        if key == "reason":
            value = _bounded_text(value, MAX_REASON_CHARS)
        elif key == "gaps":
            value = [_bounded_text(gap, MAX_GAP_CHARS) for gap in value]
        elif key == "failed_queries":
            value = [
                {**row, "reason": _bounded_text(row.get("reason", ""), MAX_REASON_CHARS)}
                for row in value
            ]
        view[key] = value
    return view


def _limits_content(report: Mapping[str, Any]) -> dict[str, Any]:
    """The full ``grounding.limits`` payload: bounded field-by-field, but not yet size-checked."""

    return {
        "memex": _limits_tool_view(report.get("memex") or {}),
        "architecture": _limits_tool_view(report.get("architecture") or {}),
        "graft": _limits_tool_view(report.get("graft") or {}),
        "memq": _limits_tool_view(report.get("memq") or {}),
        "term_selection": report.get("term_selection"),
        "tool_terms": report.get("tool_terms"),
        "inline": report.get("inline", []),
        "source_selection": report.get("source_selection"),
        "freshness": _stable_freshness(report.get("freshness")),
        "limits": report.get("limits", []),
    }


MAX_REDUCED_TERM_CHARS = 128
MAX_REDUCED_DROPPED_SOURCE_IDS = 32


def _bounded_terms(terms: Any, *, limit: int = MAX_REDUCED_TERM_CHARS) -> list[str]:
    """Bound each term's length.

    ``_IDENTIFIER``/``_WORD`` extraction has no upper length bound, so a single term can be
    pathologically long even though a term *list* is always short (``MAX_TOOL_TERMS``). Used
    only where the reduced payload must be self-contained: bounded by count is not enough.
    """

    if not isinstance(terms, list):
        return []
    return [_bounded_text(term, limit) for term in terms]


def _reduced_tool_coverage(tool_report: Mapping[str, Any]) -> dict[str, Any]:
    """The small, count-bounded coverage facts a reduced payload can still afford to carry."""

    view: dict[str, Any] = {}
    for key in ("local_candidates", "per_query_limit", "excluded_count"):
        if key in tool_report:
            view[key] = tool_report[key]
    for key in ("queries_at_limit", "clipped_queries"):
        if key in tool_report:
            view[key] = _bounded_terms(tool_report[key])
    hits_by_term = tool_report.get("hits_by_term")
    if isinstance(hits_by_term, list):
        # Bounded the same way the rest of this function is: each entry is already small (a
        # term, a count, a flag), but the term itself carries no upper length bound upstream.
        view["hits_by_term"] = [
            {
                "term": _bounded_text(row.get("term"), MAX_REDUCED_TERM_CHARS),
                "returned": row.get("returned"),
                "at_least": bool(row.get("at_least")),
            }
            for row in hits_by_term[:MAX_DROPPED_DECISIONS_LISTED]
            if isinstance(row, Mapping)
        ]
    return view


def _reduced_limits_content(report: Mapping[str, Any]) -> dict[str, Any]:
    """A minimal payload that always fits: no unbounded free text, only small, bounded facts.

    Every field here must be self-contained: bounded by this function itself, not merely by
    the caps other parts of the module happen to apply today.
    """

    tool_terms = report.get("tool_terms") or {}
    source_selection = dict(report.get("source_selection") or {})
    if "dropped_source_ids" in source_selection:
        source_selection["dropped_source_ids"] = source_selection["dropped_source_ids"][
            :MAX_REDUCED_DROPPED_SOURCE_IDS
        ]
    memex = report.get("memex") or {}
    return {
        "reduced": True,
        "statuses": {
            name: (report.get(name) or {}).get("status")
            for name in ("memex", "architecture", "graft", "memq")
        },
        "selections": {
            "memex": memex.get("selection"),
            "architecture": (report.get("architecture") or {}).get("selection"),
            "graft": (report.get("graft") or {}).get("selection"),
            "memq": (report.get("memq") or {}).get("selection"),
            "term_selection": report.get("term_selection"),
            "tool_terms": {
                **tool_terms,
                "queried": _bounded_terms(tool_terms.get("queried")),
                "not_queried": _bounded_terms(tool_terms.get("not_queried")),
            },
            "source_selection": source_selection,
        },
        "coverage": {
            "graft": _reduced_tool_coverage(report.get("graft") or {}),
            "memq": _reduced_tool_coverage(report.get("memq") or {}),
        },
        "memex_tie": {
            "tie_at_cut": memex.get("tie_at_cut", False),
            "tie_break_rule": memex.get("tie_break_rule"),
        },
        "limits_config": report.get("limits_config"),
        "inline": report.get("inline", []),
    }


def _grounding_limits_row(report: Mapping[str, Any]) -> dict[str, Any]:
    """Build the one inline row that always names every cap, omission and tool failure.

    Bounding the free text first still leaves pathological cases -- many gaps, many failed
    queries -- that exceed ``MAX_INLINE_BYTES``; the reduced payload has no free text and by
    construction always fits, so this row is never silently absent.
    """

    content = _canonical(_limits_content(report)).decode("utf-8")
    if len(content.encode("utf-8")) > MAX_INLINE_BYTES:
        content = _canonical(_reduced_limits_content(report)).decode("utf-8")
    return {
        "source_id": LIMITS_SOURCE_ID,
        "content": content,
        "content_sha256": _sha256(content.encode("utf-8")),
        "kind": "observation",
        "tier": "p2",
        "truth_state": "observed",
        "reason": _LIMITS_REASON,
    }


def _memq_scores_by_decision(memq_report: Mapping[str, Any]) -> dict[str, float]:
    """Best MemQ hit score, per resolved decision id, from one compile's MemQ report.

    MemQ hits resolve to a decision id by basename (see ``run_memq``); a hit's own score is not
    otherwise carried onto ``discovered_decisions``. Read-only, no relaunch: reused here purely to
    surface, per matched decision, the MemQ evidence that (may have) contributed its ``+3``. Two
    or more hits can resolve to the same id; the higher score is kept, matching ``run_memq``'s own
    ``max(row["score"], ...)`` merge.
    """

    scores: dict[str, float] = {}
    hits = memq_report.get("hits") if isinstance(memq_report, Mapping) else None
    if not isinstance(hits, list):
        return scores
    for hit in hits:
        if not isinstance(hit, Mapping):
            continue
        basename = hit.get("basename")
        if not isinstance(basename, str):
            continue
        match = _DECISION_FILE.match(basename)
        if not match:
            continue
        score = hit.get("score")
        if not isinstance(score, (int, float)):
            continue
        decision_id = match.group(1)
        scores[decision_id] = max(scores.get(decision_id, score), score)
    return scores


def compile_grounding(
    root: Path | str,
    *,
    plan_text: str,
    tasks: Sequence[Mapping[str, Any]] = (),
    goal: str | None = None,
    consult: Sequence[str] = ("memex", "architecture", "graft", "memq"),
    tool_runner: ToolRunner | None = None,
    timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    head: str | None = None,
    max_tool_terms: int | None = None,
    max_decisions: int | None = None,
    graft_per_query_hits: int | None = None,
    max_tool_hits: int | None = None,
    task_terms_first: bool | None = None,
    exclude_paths: Sequence[str] = (),
) -> Grounding:
    """Compile bindings, invariants and packet sources for one accepted plan, read-only.

    ``exclude_paths`` is the project's own declared exclusion list (``read_project_excludes``
    reads it back from what setup persisted); a bound source whose path matches one of these
    globs is dropped before it ever reaches ``sources``, and the drop is counted in
    ``report["excluded_by_project"]``. See "Project-declared exclusions" above this function's
    module for what each consulted tool can and cannot honour.

    ``max_tool_terms``, ``max_decisions``, ``graft_per_query_hits`` and ``max_tool_hits`` are the
    operator-settable limits (``None`` keeps each one's default); see ``TUNABLE_LIMIT_BOUNDS``
    for their bounds. An out-of-bounds value is refused here with :class:`GroundingError` before
    anything is read or any tool is launched. Every caller that wants "out of bounds" to refuse
    the whole operation, rather than degrade to unavailable grounding, must validate with
    :func:`resolve_tunable_limit` itself before calling this function; see
    ``project_onboarding.derive_draft``.

    ``task_terms_first`` (``None``/``False`` keeps today's ranking) opts into ranking the plan's
    task-table terms ahead of its other prose when filling the ``max_tool_terms`` query slots.
    Off by default: measured directly against a real decision corpus, this reordering can change
    which terms MemQ queries, which changes MemQ's own ``discovered_decisions``, which changes
    which decisions ``select_decisions`` scores highly enough to bind -- not just which terms are
    reported as queried. With this flag left unset, term order, queried terms, MemQ's discovered
    ids and the bound decision set are all identical to what this module produced before this
    option existed, on every plan, prose or not.
    """

    subject = Path(root).expanduser().resolve()
    if not subject.is_dir():
        raise GroundingError(f"grounding subject is not a directory: {subject}")
    effective_exclude_paths = normalize_exclude_paths(exclude_paths)
    effective_max_tool_terms = resolve_tunable_limit("max_tool_terms", max_tool_terms)
    effective_max_decisions = resolve_tunable_limit("max_decisions", max_decisions)
    effective_graft_per_query_hits = resolve_tunable_limit(
        "graft_per_query_hits", graft_per_query_hits
    )
    effective_max_tool_hits = resolve_tunable_limit("max_tool_hits", max_tool_hits)
    effective_task_terms_first = resolve_tunable_flag(
        "task_terms_first", task_terms_first, default=TASK_TERMS_FIRST_DEFAULT
    )
    runner = tool_runner or default_tool_runner
    texts = [plan_text]
    if goal:
        texts.append(goal)
    for task in tasks:
        for key in ("title", "done_when"):
            value = task.get(key) if isinstance(task, Mapping) else None
            if isinstance(value, str):
                texts.append(value)
    task_terms = _task_table_terms(tasks) if effective_task_terms_first else frozenset()
    all_terms = _candidate_terms(texts)
    terms = all_terms[:MAX_TERMS]
    ranked_tool_terms = _ranked_tool_terms(
        terms, task_terms=task_terms, task_terms_first=effective_task_terms_first
    )
    queried_tool_terms = ranked_tool_terms[:effective_max_tool_terms]
    not_queried_tool_terms = ranked_tool_terms[effective_max_tool_terms:]
    listed_not_queried = not_queried_tool_terms[:MAX_DROPPED_DECISIONS_LISTED]
    tool_eligible_count = len(ranked_tool_terms)
    report: dict[str, Any] = {
        "terms": terms,
        "consulted": sorted(set(consult)),
        "memex": {"status": "not_consulted"},
        "architecture": {"status": "not_consulted"},
        "graft": {"status": "not_consulted"},
        "memq": {"status": "not_consulted"},
        "codebase_memory": {"status": "not_consulted",
                            "reason": "MCP-only surface; no launcher contract yet"},
        "limits": [
            "lexical matching over titles, tags and verbatim rulings; not semantic understanding",
            "sources are by reference and digest; a changed record is reported at packet time",
        ],
        # The effective value of every operator-settable limit, defaults included, so a reader
        # of the sealed draft never has to guess what ran; see docs/RUNBOOK.md's "Tuning
        # grounding on a large decision corpus".
        "limits_config": {
            "max_tool_terms": effective_max_tool_terms,
            "max_decisions": effective_max_decisions,
            "graft_per_query_hits": effective_graft_per_query_hits,
            "max_tool_hits": effective_max_tool_hits,
            "task_terms_first": effective_task_terms_first,
        },
        "term_selection": _selection_shape(
            eligible_count=len(all_terms), returned_count=len(terms), limit=MAX_TERMS,
            truncated=len(all_terms) > MAX_TERMS, count_basis="exact",
        ),
        "tool_terms": {
            **_selection_shape(
                eligible_count=tool_eligible_count, returned_count=len(queried_tool_terms),
                limit=effective_max_tool_terms,
                truncated=tool_eligible_count > effective_max_tool_terms,
                count_basis="exact",
            ),
            "queried": list(queried_tool_terms),
            # Bounded the same way memex's own dropped-decisions list is: every term ranked
            # below the cap, so a reader can see which terms lost a query slot, not just how
            # many. Task-table terms rank ahead of prose-only terms; see ``_ranked_tool_terms``.
            "not_queried": list(listed_not_queried),
        },
    }
    if len(not_queried_tool_terms) > MAX_DROPPED_DECISIONS_LISTED:
        report["tool_terms"]["not_queried_listed"] = len(listed_not_queried)

    memq: dict[str, Any] = {"status": "not_consulted"}
    if "memq" in consult:
        memq = run_memq(
            subject, terms, runner=runner, timeout_s=timeout_s, task_terms=task_terms,
            task_terms_first=effective_task_terms_first,
            max_tool_terms=effective_max_tool_terms, max_tool_hits=effective_max_tool_hits,
        )
        report["memq"] = memq
    graft: dict[str, Any] = {"status": "not_consulted"}
    if "graft" in consult:
        graft = run_graft(
            subject, terms, runner=runner, timeout_s=timeout_s, task_terms=task_terms,
            task_terms_first=effective_task_terms_first,
            max_tool_terms=effective_max_tool_terms,
            graft_per_query_hits=effective_graft_per_query_hits,
            max_tool_hits=effective_max_tool_hits,
            exclude_paths=effective_exclude_paths,
        )
        report["graft"] = graft
    architecture: dict[str, Any] = {"status": "not_consulted"}
    if "architecture" in consult:
        architecture = architecture_context(
            subject, terms, head=head or _git_head(subject), task_terms=task_terms,
            task_terms_first=effective_task_terms_first,
            max_tool_terms=effective_max_tool_terms,
        )
        report["architecture"] = architecture
    # How old each consulted index is, read from metadata only; an `ok` over a stale index is
    # reported as such rather than hidden. Never a refresh: grounding stays read-only.
    from bearhug.project_knowledge import knowledge_freshness

    report["freshness"] = knowledge_freshness(subject)

    bindings: list[dict[str, Any]] = []
    invariants: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    excluded_decision_ids: list[str] = []
    if "memex" in consult:
        decisions = read_decisions(subject)
        selection = select_decisions(
            decisions, terms, discovered_ids=memq.get("discovered_decisions", []),
            max_decisions=effective_max_decisions,
        )
        matched = list(selection.matches)
        excluded_decision_ids = sorted(
            d.decision_id for d, _score, _hits in matched
            if effective_exclude_paths and path_excluded(d.path, effective_exclude_paths)
        )
        skipped = _skipped_decision_files(subject)
        memq_scores = _memq_scores_by_decision(memq)
        dropped_rows = [
            {
                "id": d.decision_id,
                "status": d.status,
                "score": score,
                "lexical_score": selection.details.get(d.decision_id, {}).get("lexical_score"),
                "strong_reason": selection.details.get(d.decision_id, {}).get("strong_reason"),
            }
            for d, score, _hits in selection.dropped
        ]
        listed_dropped = dropped_rows[:MAX_DROPPED_DECISIONS_LISTED]
        memex_report: dict[str, Any] = {
            "status": "ok" if decisions else "unavailable",
            "reason": "" if decisions else f"no records under {DECISION_DIR}",
            "records_read": len(decisions),
            "matched": [
                {
                    "id": d.decision_id, "status": d.status, "score": score, "terms": list(hits),
                    **selection.details.get(d.decision_id, {}),
                    "memq_score": memq_scores.get(d.decision_id),
                }
                for d, score, hits in matched
            ],
            "selection": _selection_shape(
                eligible_count=selection.eligible_count,
                returned_count=selection.returned_count,
                limit=selection.limit, truncated=selection.truncated,
                count_basis=selection.count_basis,
            ),
            "dropped": listed_dropped,
            "skipped": {
                "count": len(skipped), "listed": skipped[:MAX_SKIPPED_DECISIONS_LISTED],
            },
            # True exactly when the cap cut inside a group of equal scores; the rule itself is
            # only named when it actually applied, matching how every other limit line here
            # says nothing when the cap it describes never fired.
            "tie_at_cut": selection.tie_at_cut,
            # Decisions that matched by score but whose own path is a project-declared
            # exclusion (see "Project-declared exclusions" near the top of this module): they
            # are named here, matched, but produce no binding, invariant or source below.
            "excluded_by_project": excluded_decision_ids,
        }
        if selection.tie_at_cut:
            memex_report["tie_break_rule"] = TIE_BREAK_RULE
        if len(dropped_rows) > MAX_DROPPED_DECISIONS_LISTED:
            memex_report["dropped_listed"] = len(listed_dropped)
        report["memex"] = memex_report
        for decision, _score, hits in matched:
            if effective_exclude_paths and path_excluded(decision.path, effective_exclude_paths):
                continue
            accepted = decision.status == "accepted"
            term = ", ".join(hits) if hits else decision.title
            bindings.append({
                "binding_id": f"binding.decision.{decision.decision_id}",
                "term": term[:4096],
                "meaning": (decision.title if accepted
                            else f"{decision.title} (proposed; not authority)")[:16384],
                "state": "accepted" if accepted else "proposed",
                "evidence_refs": [decision.sha256],
            })
            if accepted:
                invariants.append({
                    "invariant_id": f"invariant.decision.{decision.decision_id}",
                    "statement": (
                        f"Accepted decision {decision.decision_id} governs this work: "
                        f"{decision.title}"
                    )[:16384],
                    "origin": "project_sealed",
                    "evidence_refs": [decision.sha256],
                })
            sources.append({
                "source_id": f"grounding.decision.{decision.decision_id}",
                "path": decision.path,
                "content_sha256": decision.sha256,
                "kind": "accepted_binding" if accepted else "proposal",
                "tier": "p1" if accepted else "p3",
                "truth_state": "accepted" if accepted else "proposed",
                "reason": f"decision record matched terms: {term[:200]}",
                "projection": "decision",
            })

    # A single, always-present summary of what the project's own declared exclusions removed,
    # across every consulted tool -- named even when the project declares none, matching this
    # module's "a limits row that always reaches the packet" convention for other caps.
    report["excluded_by_project"] = {
        "paths": list(effective_exclude_paths),
        "decisions": excluded_decision_ids,
        "graft_pointers": graft.get("excluded_by_project", 0) if graft else 0,
        "memq_note": (
            "MemQ recall hits carry only a label and basename, never a full repository path; "
            "project exclusions are enforced for decision-linked MemQ discovery only, not for "
            "free-text MemQ hits"
        ),
    }

    inline_outcomes: list[dict[str, Any]] = []
    if architecture.get("status") == "ok":
        row, outcome = _inline_attempt(
            "grounding.architecture",
            {"index": architecture["index"], "freshness": architecture.get("freshness"),
             "records": architecture["records"], "status": architecture["status"],
             "reason": architecture["reason"], "gaps": architecture["gaps"],
             "selection": architecture["selection"],
             "gap_selection": architecture["gap_selection"]},
            "architecture records matching the plan's terms, by id and provenance",
        )
        inline_outcomes.append(outcome)
        if row is not None:
            sources.append(row)
    if graft.get("status") == "ok":
        row, outcome = _inline_attempt(
            "grounding.graft",
            {"terms": graft["terms"], "hits": graft["hits"], "status": graft["status"],
             "reason": graft["reason"], "selection": graft["selection"],
             "failed_queries": graft["failed_queries"], "partial": graft["partial"]},
            "Graft code-map pointers for the plan's terms; open the pointer, not this note",
        )
        inline_outcomes.append(outcome)
        if row is not None:
            sources.append(row)
    if memq.get("status") == "ok":
        row, outcome = _inline_attempt(
            "grounding.memq",
            {"terms": memq["terms"], "hits": memq["hits"], "status": memq["status"],
             "reason": memq["reason"], "selection": memq["selection"],
             "failed_queries": memq["failed_queries"], "partial": memq["partial"]},
            "MemQ recall labels and files for the plan's terms; the files remain authority",
        )
        inline_outcomes.append(outcome)
        if row is not None:
            sources.append(row)

    report["inline"] = sorted(inline_outcomes, key=lambda row: row["source_id"])

    # One slot is reserved for `grounding.limits` below, so the sealed total never exceeds
    # `MAX_SOURCES` even though this cap is computed before that row exists.
    reserved_slots = 1
    regular_cap = MAX_SOURCES - reserved_slots
    eligible_sources = len(sources)
    regular_sources = sources[:regular_cap]
    source_selection: dict[str, Any] = _selection_shape(
        eligible_count=eligible_sources, returned_count=len(regular_sources), limit=regular_cap,
        truncated=eligible_sources > regular_cap, count_basis="exact",
    )
    source_selection["reserved_slots"] = reserved_slots
    if eligible_sources > regular_cap:
        source_selection["dropped_source_ids"] = sorted(
            row["source_id"] for row in sources[regular_cap:]
        )
    report["source_selection"] = source_selection

    limits_row = _grounding_limits_row(report)
    sealed = validate_grounding_sources(regular_sources + [limits_row], subject_root=None)
    return Grounding(
        terms=tuple(terms),
        bindings=tuple(sorted(bindings, key=lambda row: row["binding_id"])),
        invariants=tuple(sorted(invariants, key=lambda row: row["invariant_id"])),
        sources=tuple(sealed),
        report=report,
    )


# ---------------------------------------------------------------------------------------------
# Sealed rows: validation at prepare time, materialization at packet time


def validate_grounding_sources(
    rows: Any, *, subject_root: Path | None
) -> list[dict[str, Any]]:
    """Validate sealed grounding rows and return them normalized and sorted by source_id."""

    if rows is None:
        return []
    if not isinstance(rows, list):
        raise GroundingError("grounding_sources must be an array")
    if len(rows) > MAX_SOURCES:
        raise GroundingError(f"grounding_sources exceeds {MAX_SOURCES} rows")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        label = f"grounding_sources[{index}]"
        if not isinstance(row, Mapping):
            raise GroundingError(f"{label} is not an object")
        keys = set(row)
        if keys == _PATH_ROW_FIELDS:
            form = "path"
        elif keys == _INLINE_ROW_FIELDS:
            form = "inline"
        else:
            raise GroundingError(f"{label} is not a closed grounding row")
        source_id = row["source_id"]
        if not isinstance(source_id, str) or _TOKEN.fullmatch(source_id) is None:
            raise GroundingError(f"{label}.source_id is invalid")
        if not source_id.startswith("grounding."):
            raise GroundingError(f"{label}.source_id must start with 'grounding.'")
        if source_id in seen:
            raise GroundingError(f"{label} repeats source_id {source_id!r}")
        seen.add(source_id)
        kind = row["kind"]
        if kind not in KINDS:
            raise GroundingError(f"{label}.kind is unsupported")
        tier, truth = KINDS[kind]
        if row["tier"] not in {"p1", "p2", "p3"} or (kind != "observation" and row["tier"] != tier):
            raise GroundingError(f"{label}.tier is invalid for kind {kind!r}")
        if row["truth_state"] != truth:
            raise GroundingError(f"{label}.truth_state must be {truth!r} for kind {kind!r}")
        digest = row["content_sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise GroundingError(f"{label}.content_sha256 is invalid")
        reason = row["reason"]
        if not isinstance(reason, str) or not reason or len(reason) > 4096:
            raise GroundingError(f"{label}.reason must be bounded text")
        normalized: dict[str, Any] = {
            "source_id": source_id, "kind": kind, "tier": row["tier"],
            "truth_state": truth, "reason": reason, "content_sha256": digest,
        }
        if form == "path":
            path = row["path"]
            if (not isinstance(path, str) or not path or path.startswith("/")
                    or ".." in path.split("/") or "\\" in path):
                raise GroundingError(f"{label}.path must be repository-relative")
            if row["projection"] not in _PROJECTIONS:
                raise GroundingError(f"{label}.projection is unsupported")
            normalized["path"] = path
            normalized["projection"] = row["projection"]
            if subject_root is not None:
                target = subject_root / path
                if target.is_symlink() or not target.is_file():
                    raise GroundingError(f"{label}.path is not a regular file in the subject")
        else:
            content = row["content"]
            if not isinstance(content, str) or not content:
                raise GroundingError(f"{label}.content must be non-empty text")
            encoded = content.encode("utf-8")
            if len(encoded) > MAX_INLINE_BYTES:
                raise GroundingError(f"{label}.content exceeds {MAX_INLINE_BYTES} bytes")
            if _sha256(encoded) != digest:
                raise GroundingError(f"{label}.content does not match content_sha256")
            normalized["content"] = content
        result.append(normalized)
    result.sort(key=lambda item: item["source_id"])
    return result


def runtime_sources(rows: Sequence[Mapping[str, Any]], *, worktree: Path) -> list[dict[str, Any]]:
    """Materialize sealed grounding rows as packet sources from the leased worktree.

    A path row whose bytes no longer match the sealed digest is not sent stale: it becomes an
    observation naming the change, so the episode learns the record moved instead of reading a
    projection Bear Hug can no longer vouch for.
    """

    normalized = validate_grounding_sources(list(rows), subject_root=None)
    result: list[dict[str, Any]] = []
    for row in normalized:
        base = {
            "source_id": row["source_id"], "kind": row["kind"], "tier": row["tier"],
            "truth_state": row["truth_state"], "reason": row["reason"],
        }
        if "content" in row:
            result.append({**base, "content": row["content"].encode("utf-8"),
                           "source_sha256": row["content_sha256"], "source_form": "full"})
            continue
        target = worktree / row["path"]
        observed: bytes | None = None
        if (
            not target.is_symlink()
            and target.is_file()
            and target.stat().st_size <= MAX_DECISION_FILE_BYTES
        ):
            observed = target.read_bytes()
        if observed is None or _sha256(observed) != row["content_sha256"]:
            notice = _canonical({
                "source_id": row["source_id"], "path": row["path"],
                "sealed_sha256": row["content_sha256"],
                "observed_sha256": None if observed is None else _sha256(observed),
                "status": "changed_since_sealing" if observed is not None else "missing",
                "instruction": "read the current record from the path; the sealed binding "
                               "may be stale and reconciliation should be reported",
            })
            result.append({
                "source_id": row["source_id"], "kind": "observation", "tier": "p2",
                "truth_state": "observed", "content": notice,
                "reason": "grounding source changed or vanished since it was sealed",
            })
            continue
        projection = (project_decision(observed) if row["projection"] == "decision"
                      else observed)
        result.append({
            **base, "content": observed, "source_sha256": row["content_sha256"],
            "source_form": "projection" if projection != observed else "full",
            **({"projection": projection} if projection != observed else {}),
        })
    return result


__all__ = [
    "Decision",
    "EXCLUDE_PATHS_ENV_KEY",
    "Grounding",
    "GroundingError",
    "SETUP_ENV_FILE",
    "TIE_BREAK_RULE",
    "ToolResult",
    "TUNABLE_LIMIT_BOUNDS",
    "VOLATILE_GROUNDING_FIELDS",
    "architecture_context",
    "compile_grounding",
    "default_tool_runner",
    "extract_terms",
    "match_decisions",
    "normalize_exclude_paths",
    "path_excluded",
    "project_decision",
    "read_decisions",
    "read_project_excludes",
    "resolve_tunable_limit",
    "run_graft",
    "run_memq",
    "runtime_sources",
    "select_decisions",
    "validate_grounding_sources",
]
