"""WORK.json v2 importer: BOARD/LEDGER/MASTER-PLAN -> a lossless, region-aware index.

Built to the frozen contract in the Phase 2 scratchpad (`workv2-contract.md`). Bear Hug owns
Barracuda's plan/board/ledger STATE without losing any content. Byte-identical regeneration is
not required; the bar is content-completeness: every source line becomes exactly one v2 "unit",
`src_line` verbatim, and no line is dropped. `cells`/`fields` on a board row are a derived index
that MAY be imperfect without losing content, because the raw line is always retained on the unit.

This module is read-only: it never opens its sources for writing. It is copied into managed
projects as part of `project_work.py`'s support library, so it uses only the standard library.

Item 1 (the Barracuda F1 lesson): a table row's cells are found by classifying each `|` by the
region its position lands in — inside a backtick code span, inside a markdown `[text](url)` link,
or escaped as `\\|` — rather than by blanking or masking characters first. A naive
``re.split(r"(?<!\\)\\|", line)`` over-splits a Notes cell containing an inline-code pipe (for
example `` `a|b` ``) or a link whose target embeds a pipe, and mis-assigns every field after it.

BMAD-derived additions (DERIVED, additive, by-reference only): each row's `fields` also carries
`context_pointers`, `readiness`/`readiness_reason`, and `acceptance`. These are index fields
computed from `cells`, never a copy of prose — they parse tokens/anchors out of `cells.authority`,
`cells.notes`, and `cells.blocked_by` (memex decision ids, `file:symbol`/`path.go#Symbol` code
anchors, PR/review refs) and cost nothing to keep fresh because they hold pointers, not text.
`src_line` remains the sole authority; these fields may be imperfect or empty without losing
content. See `_context_pointers`, `_readiness`, and `_acceptance` below for the exact, conservative
rules (each documents its own precedence/heuristics and reasons for a claimed state).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# A backtick run opens a code span; it closes at the next run of the same length. A markdown
# inline link is `[text](url)`, immediately adjacent, with no nested brackets/parens required for
# board Notes/Authority cells. A `|` inside either region is not a cell boundary.
_PROTECTED_REGION = re.compile(
    r"(?P<tick>`+)(?:(?!(?P=tick)).)*?(?P=tick)"
    r"|\[(?:[^\[\]\\]|\\.)*\]\((?:[^()\\]|\\.)*\)"
)

_ROW_START = re.compile(r"^\|\s*(\d+)\s*\|")
_PHASE = re.compile(r"^\s*\[(?P<phase>P\d+(?:·hold)?|corpus|UNPLACED)\]")
_RULING_VALUES = {"accepted", "proposed", "superseded"}
_EXECUTION_VALUES = {"executed", "partial", "unstarted"}
_BLOCKED_TOKEN_SPLIT = re.compile(r"[,;\s]+")

_BOARD_CELL_NAMES = ("entry", "ruling", "execution", "notes", "blocked_by", "authority")

# --- BMAD-derived context pointers: decisions / anchors / reviews -----------------------------
#
# These parse REFERENCES (ids, anchors, paths) out of raw cell prose. They never copy prose into
# the index, so they stay cheap to keep fresh. Each pattern is deliberately conservative: it is
# built to match the shapes actually observed in Barracuda's board (see module docstring), and
# errs toward under-matching (missing a pointer leaves `src_line` as the fallback authority)
# rather than over-matching (inventing a pointer that is not really there).

# A decision reference is either the word "decision(s)" immediately followed by one or more
# 3-4 digit numbers (comma/hyphen/en-dash separated, e.g. "decisions 0190, 0191" or
# "0167–0181"), or a BARE zero-padded number of the shape memex decision ids actually take in
# this corpus (0034, 0110, 0223, ...). The leading zero is the signal that makes a bare number
# "clearly decision-shaped" rather than a row count, a line number, or a year/date fragment (which
# never start with a zero in this corpus).
_DECISION_PHRASE = re.compile(
    r"\bdecisions?\b[\s:#→]{0,4}(\d{3,4}(?:\s*[-–,]\s*\d{3,4})*)",
    re.IGNORECASE,
)
_DECISION_NUM = re.compile(r"\d{3,4}")
_BARE_DECISION = re.compile(r"\b0\d{2,3}\b")

# A code anchor is a path-like token immediately followed by `#Symbol` (e.g.
# `pkg/codec#MemberLayout`) or `:line` (e.g. `supervisor_provision.go:117`), with no gap. `path`
# must contain at least one letter — this excludes IP:port and bare-numeric tokens (e.g.
# `10.0.0.45:4840`) that otherwise match the same shape.
_ANCHOR = re.compile(
    r"\b(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*)"
    r"(?:#(?P<hsym>[A-Za-z_][A-Za-z0-9_]*)|:(?P<line>\d{1,6}))\b"
)

# A review/PR ref is either a `docs/superpowers/reviews/*.md` path, or a `#<digits>` token with at
# least 2 digits (a bare `#1`/`#6` is far more often an enumerated obligation/footnote marker in
# this corpus than a PR number — see the board's "§5.7.3.1 obligations #1-#4" style text — so
# single-digit hash refs are deliberately excluded as not "clearly" PR-shaped).
_REVIEW_PATH = re.compile(r"docs/superpowers/reviews/[\w.-]+\.md")
_REVIEW_HASH = re.compile(r"(?<![\w#])#(\d{2,6})\b")

# --- BMAD-derived readiness: held/owed ruling language --------------------------------------
_HELD_MARK = re.compile(r"\bHELD\b")
_HELD_BY_DECISION = re.compile(r"held by decision", re.IGNORECASE)
_RULING_OWED = re.compile(r"ruling owed", re.IGNORECASE)
_NEEDS_RULING = re.compile(r"needs?\b[^.]{0,40}?\bruling\b", re.IGNORECASE)

# --- BMAD-derived acceptance: check/test/command/gate references -----------------------------
_ACCEPT_SCRIPT = re.compile(r"scripts/[\w./-]+")
_ACCEPT_TEST = re.compile(r"[\w./-]*_test\.(?:go|py)\b")
_ACCEPT_SECTION = re.compile(r"§[\w.]+")
_ACCEPT_GATE = re.compile(r"\b(?:PASS|green)\b")


def _ordered_dedup(hits: list[tuple[int, str]]) -> list[str]:
    """Sort (position, token) pairs by position and dedup, keeping first-seen order."""
    out: list[str] = []
    for _, token in sorted(hits, key=lambda pair: pair[0]):
        if token not in out:
            out.append(token)
    return out


def _decision_ids(text: str) -> list[str]:
    """Memex decision ids referenced in `text`, deduped, in order of first appearance."""
    hits: list[tuple[int, str]] = []
    for phrase in _DECISION_PHRASE.finditer(text):
        group_start = phrase.start(1)
        for num in _DECISION_NUM.finditer(phrase.group(1)):
            hits.append((group_start + num.start(), num.group(0)))
    for bare in _BARE_DECISION.finditer(text):
        hits.append((bare.start(), bare.group(0)))
    return _ordered_dedup(hits)


def _anchors(text: str) -> list[str]:
    """`file:symbol` / `path.go#Symbol` / `file.py:123` code anchors, deduped, in order."""
    hits: list[tuple[int, str]] = []
    for match in _ANCHOR.finditer(text):
        path = match["path"]
        if not any(char.isalpha() for char in path):
            continue  # excludes IP:port and other purely-numeric false positives
        token = f"{path}#{match['hsym']}" if match["hsym"] else f"{path}:{match['line']}"
        hits.append((match.start(), token))
    return _ordered_dedup(hits)


def _reviews(text: str) -> list[str]:
    """PR/review refs (`#486`, `docs/superpowers/reviews/*.md`), deduped, in order."""
    hits: list[tuple[int, str]] = []
    for match in _REVIEW_PATH.finditer(text):
        hits.append((match.start(), match.group(0)))
    for match in _REVIEW_HASH.finditer(text):
        hits.append((match.start(), match.group(0)))
    return _ordered_dedup(hits)


def _context_pointers(
    authority_raw: str, notes_raw: str, blocked_by_raw: str, depends_on: list[str]
) -> dict[str, Any]:
    """Pointers-only index parsed from a row's authority/notes/blocked-by cells (by reference).

    Never embeds surrounding prose: every list holds ids/anchors/paths, not sentences, so this
    stays cheap to keep fresh. `depends_on` is reused verbatim from the existing parsed value.
    """
    pointer_text = " ".join((authority_raw, notes_raw, blocked_by_raw))
    return {
        "decisions": _decision_ids(pointer_text),
        "anchors": _anchors(pointer_text),
        "reviews": _reviews(pointer_text),
        "depends_on": depends_on,
    }


def _ruling_held(notes_raw: str, ruling_raw: str, ruling_norm: str) -> str:
    """Which held/owed-ruling rule fired, or "" if none did.

    Checked in this order: an explicit `HELD` marker, the phrase "held by decision", the phrase
    "ruling owed", a "needs ... ruling" phrase, and finally `ruling_norm == "proposed"` (a ruling
    that has not yet been accepted). Only the raw `notes`/`ruling` cells are scanned — this is
    deliberately narrower than the `authority`/`blocked_by` cells `_context_pointers` scans,
    matching the spec's "cells.notes/cells.ruling" wording.
    """
    combined = f"{notes_raw} {ruling_raw}"
    if _HELD_MARK.search(combined):
        return "notes/ruling cell contains HELD"
    if _HELD_BY_DECISION.search(combined):
        return "notes/ruling cell contains 'held by decision'"
    if _RULING_OWED.search(combined):
        return "notes/ruling cell contains 'ruling owed'"
    if _NEEDS_RULING.search(combined):
        return "notes/ruling cell contains a 'needs ... ruling' phrase"
    if ruling_norm == "proposed":
        return "ruling_norm == proposed"
    return ""


def _readiness(
    *,
    execution_norm: str,
    ruling_norm: str,
    notes_raw: str,
    ruling_raw: str,
    depends_on: list[str],
    done_map: dict[str, bool],
    decisions: list[str],
) -> tuple[str, str]:
    """`(readiness, readiness_reason)`, derived conservatively and precedence-ordered.

    Precedence (first match wins) — this order is the contract, not an implementation detail:

    1. `done`               -- execution_norm == "executed".
    2. `blocked-by-dep`     -- some `depends_on` row is not itself done (including a dep id with
                               no row at all, per spec: still blocked).
    3. `blocked-by-ruling`  -- the row shows a held/owed ruling (see `_ruling_held`).
    4. `context-missing`    -- an OPEN row (unstarted/partial) with no linked decision pointer.
    5. `ready`              -- none of the above.
    6. `unknown`            -- execution_norm itself is not one of the recognized values, so
                               precedence steps 2-4 cannot be evaluated conservatively; never
                               guessed as `ready`/`done`.
    """
    if execution_norm == "executed":
        return "done", "execution_norm == executed"
    if execution_norm not in _EXECUTION_VALUES:
        return "unknown", f"execution_norm not recognized ({execution_norm!r})"

    unmet = [
        dep if done_map.get(dep) is not None else f"{dep} (row not found)"
        for dep in depends_on
        if not done_map.get(dep, False)
    ]
    if unmet:
        return "blocked-by-dep", f"depends_on unresolved: {', '.join(unmet)}"

    held_reason = _ruling_held(notes_raw, ruling_raw, ruling_norm)
    if held_reason:
        return "blocked-by-ruling", held_reason

    if execution_norm in {"unstarted", "partial"} and not decisions:
        return "context-missing", "open row (unstarted/partial) with no linked decision pointer"

    return "ready", "no blocking dependency, held ruling, or missing context found"


def _acceptance(notes_raw: str) -> str:
    """A pointer to what proves this row 'done', mined from `notes_raw`. "" when none parses.

    Checked in this order: a `scripts/...` command, a `*_test.go`/`*_test.py` reference, a
    `§`-section reference (combined with a nearby PASS/green gate mention when both are present),
    then a bare PASS/green gate mention. Never invents a pointer; empty when nothing matches.
    """
    script = _ACCEPT_SCRIPT.search(notes_raw)
    if script:
        return script.group(0)
    test_ref = _ACCEPT_TEST.search(notes_raw)
    if test_ref:
        return test_ref.group(0)
    section = _ACCEPT_SECTION.search(notes_raw)
    gate = _ACCEPT_GATE.search(notes_raw)
    if section and gate:
        return f"{section.group(0)} {gate.group(0)}"
    if section:
        return section.group(0)
    if gate:
        return gate.group(0)
    return ""


def _pipe_boundaries(line: str) -> list[int]:
    """Positions of the `|` characters in `line` that are real cell boundaries."""
    spans = sorted((m.start(), m.end()) for m in _PROTECTED_REGION.finditer(line))
    boundaries: list[int] = []
    i, n = 0, len(line)
    span_index = 0
    while i < n:
        if span_index < len(spans) and i == spans[span_index][0]:
            i = spans[span_index][1]
            span_index += 1
            continue
        char = line[i]
        if char == "\\" and i + 1 < n and line[i + 1] == "|":
            i += 2
            continue
        if char == "|":
            boundaries.append(i)
        i += 1
    return boundaries


def split_cells_region_aware(line: str) -> list[str]:
    """Split a markdown table row on unescaped, out-of-region `|` characters.

    A `|` inside an inline-code span, inside a markdown `[..](..)` link, or escaped as `\\|` is
    not a boundary. Returns the cell strings between the bounding pipes (mirroring the leading
    and trailing empty segments a well-formed row produces, both dropped), with `\\|` -> `|`
    unescaping applied to each returned cell.
    """
    boundaries = _pipe_boundaries(line)
    cuts = [-1, *boundaries, len(line)]
    parts = [line[cuts[k] + 1 : cuts[k + 1]] for k in range(len(cuts) - 1)]
    interior = parts[1:-1] if len(parts) >= 2 else []
    return [part.replace("\\|", "|") for part in interior]


def _ruling_norm(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in _RULING_VALUES else ""


def _execution_norm(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in _EXECUTION_VALUES else ""


def _phase(entry_raw: str) -> str:
    match = _PHASE.match(entry_raw)
    return match["phase"] if match else ""


def _depends_on(blocked_by_raw: str) -> list[str]:
    tokens = [t for t in _BLOCKED_TOKEN_SPLIT.split(blocked_by_raw.strip()) if t]
    if tokens and all(t.isdigit() for t in tokens):
        return tokens
    return []


def _board_row_unit(line: str, cells: list[str], done_map: dict[str, bool]) -> dict[str, Any]:
    board_row = _ROW_START.match(line).group(1)
    named = dict(zip(_BOARD_CELL_NAMES, cells[1:], strict=True))
    ruling_norm = _ruling_norm(named["ruling"])
    execution_norm = _execution_norm(named["execution"])
    depends_on = _depends_on(named["blocked_by"])
    context_pointers = _context_pointers(
        named["authority"], named["notes"], named["blocked_by"], depends_on
    )
    readiness, readiness_reason = _readiness(
        execution_norm=execution_norm,
        ruling_norm=ruling_norm,
        notes_raw=named["notes"],
        ruling_raw=named["ruling"],
        depends_on=depends_on,
        done_map=done_map,
        decisions=context_pointers["decisions"],
    )
    return {
        "kind": "row",
        "src_line": line,
        "board_row": board_row,
        "cells": named,
        "fields": {
            "phase": _phase(named["entry"]),
            "ruling_norm": ruling_norm,
            "execution_norm": execution_norm,
            "depends_on": depends_on,
            "done": execution_norm == "executed",
            "context_pointers": context_pointers,
            "readiness": readiness,
            "readiness_reason": readiness_reason,
            "acceptance": _acceptance(named["notes"]),
        },
    }


def _board_units(text: str) -> list[dict[str, Any]]:
    lines = text.split("\n")
    # First pass: classify every line and, for board rows, resolve `board_row -> done` so that
    # `blocked-by-dep` readiness (second pass) can look up ANY row's done-state regardless of
    # source order (board rows are not guaranteed to appear in board_row-numeric order).
    classified: list[tuple[str, list[str] | None]] = []
    done_map: dict[str, bool] = {}
    for line in lines:
        match = _ROW_START.match(line)
        cells = split_cells_region_aware(line) if match else []
        if match and len(cells) == 7:
            classified.append((line, cells))
            board_row = match.group(1)
            execution_raw = cells[1:][_BOARD_CELL_NAMES.index("execution")]
            done_map[board_row] = _execution_norm(execution_raw) == "executed"
        else:
            classified.append((line, None))

    units: list[dict[str, Any]] = []
    for line, cells in classified:
        if cells is not None:
            units.append(_board_row_unit(line, cells, done_map))
        else:
            units.append({"kind": "block", "src_line": line})
    return units


def _block_units(text: str) -> list[dict[str, Any]]:
    return [{"kind": "block", "src_line": line} for line in text.split("\n")]


def build_workv2(board_path: str | Path, ledger_path: str | Path, plan_path: str | Path) -> dict:
    """Read the three project-authored docs and return the candidate WORK.json v2 dict.

    Read-only: this never writes any of `board_path`/`ledger_path`/`plan_path`. Every source
    line becomes exactly one unit, in order; board rows additionally get region-aware `cells`
    and normalized `fields` (see module docstring). `src_line` is authority on every unit.
    """
    board_text = Path(board_path).read_text(encoding="utf-8")
    ledger_text = Path(ledger_path).read_text(encoding="utf-8")
    plan_text = Path(plan_path).read_text(encoding="utf-8")
    return {
        "schema": "bearhug-work/2",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_docs": {
            "board": str(board_path),
            "ledger": str(ledger_path),
            "master_plan": str(plan_path),
        },
        "board": {"units": _board_units(board_text)},
        "ledger": {"units": _block_units(ledger_text)},
        "master_plan": {"units": _block_units(plan_text)},
    }
