"""boardrows — the ONE parser for BOARD.md rows.

Decision 0137 requires the queue to survive a session boundary, and Part B of
`2026-08-13-PLAN-claude-md-context-engineering-and-board-automation.md` covers
BOTH directions of the mirror:

    tasks → board   PostToolUse on TaskCreate/TaskUpdate
    board → tasks   SessionStart, the direction that actually failed

Sam's ruling on that scope extension is explicit that both directions "read the
same rows through the reconstruction library ... so the two directions cannot
disagree about what a row is." This module is that library. `task-durability.py`
(the detector) and `board-restore.py` (the restorer) both import it; neither
parses the board itself.

The board stays the authority (decision 0182): this module READS rows and never
writes one, and never derives a grade, status or count of its own.
"""

import os
import re

BOARD = "docs/superpowers/plans/BOARD.md"

# A row: | <n> | <entry> | <ruling> | <execution> | <notes> | <blocked by> | <authority> |
#
# Ruling and execution are the two orthogonal lifecycle axes, ruled by decision
# 0274 and shared VERBATIM with the decision corpus — the same words mean the same
# thing in `docs/memex/decisions/*.md` frontmatter. They were split out of a single
# free-text status cell on 2026-08-25; before that one cell carried both axes plus
# the rationale, in 31 distinct spellings.
# Anchored on a leading integer cell so prose tables elsewhere in the file
# (the phase legend, the per-package sub-tables) cannot be mistaken for queue
# rows — those have non-numeric first cells.
_ROW = re.compile(r"^\|\s*(\d+)\s*\|(.*)$")

# A cell break is an UNESCAPED pipe. GFM tables define `\|` as one literal pipe inside a
# cell; a naive split cannot represent a pipe at all, so a row quoting a regex shifted every
# cell to its right. Measured 2026-09-01: eleven live rows, row 184 pushing ~2,632 characters
# of its Notes cell past the boundary and `blocked` reading a fragment of prose. The two
# workarounds each fix one axis — `&#124;` parses and does not render, a bare `|` renders and
# does not parse — so the spelling the format already defines is the one honoured here.
_CELL_BREAK = re.compile(r"(?<!\\)\|")


def _split_cells(body):
    """The row body split into cells, with `\|` kept as one literal pipe."""
    return [c.strip().replace("\\|", "|") for c in _CELL_BREAK.split(body)]

# Phase tag as the phase check requires it (decision 0152), read from the LEADING
# position of the entry cell.
#
# UNPLACED is included because it is a CLAIM, not an absence: under decision 0258
# placement is ruled and never inferred, so "deliberately unplaced" and "nobody
# tagged it" are different states and a caller must be able to tell them apart.
# They were both reported as "" until 2026-08-25.
#
# Anchored rather than searched: a `[P2]` written in a status cell's prose is a
# MENTION, not a placement. Measured before anchoring — zero rows disagreed between
# their leading tag and a whole-row search, so this moved no existing value while
# closing the same hazard that made the old free-text done-marker read a
# sub-part checkmark as done.
_PHASE = re.compile(r"^\s*\[(P\d+(?:·hold|·P\d+)?|corpus|UNPLACED)\]")

# An authority path must be a real docs/**.md file — the durability check.
_DOCPATH = re.compile(r"(docs/[A-Za-z0-9._/\-]+\.md)")

def board_path(repo_root):
    return os.path.join(repo_root, BOARD)


def parse(repo_root):
    """Return every queue row as a dict. Order is board order.

    Keys: row, entry, status, blocked, authority, phase, done.
    `authority` is the first docs/**.md path found anywhere in the row, because
    some rows carry it in the authority cell and older ones inline it in prose.

    Keys: row, entry, ruling, execution, status (the Notes cell), blocked,
    authority, phase, done. `status` keeps its name for the callers that read it as
    the row's prose; the two AXES are `ruling` and `execution` (decision 0274).
    """
    path = board_path(repo_root)
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _ROW.match(line.rstrip("\n"))
            if not m:
                continue
            cells = _split_cells(m.group(2))
            entry = cells[0] if cells else ""
            ruling = cells[1] if len(cells) > 1 else ""
            execution = cells[2] if len(cells) > 2 else ""
            status = cells[3] if len(cells) > 3 else ""
            blocked = cells[4] if len(cells) > 4 else ""
            authority = cells[5] if len(cells) > 5 else ""
            whole = m.group(2)
            ph = _PHASE.match(cells[0] if cells else "")
            dp = _DOCPATH.search(authority) or _DOCPATH.search(whole)
            out.append(
                {
                    "row": m.group(1),
                    "entry": entry,
                    "status": status,
                    "blocked": blocked,
                    "authority": dp.group(1) if dp else "",
                    "ruling": ruling,
                    "execution": execution,
                    "phase": ph.group(1) if ph else "",
                    # DONE is now READ, not inferred. It was a regex over free
                    # prose until 2026-08-25, and that regex matched a checkmark
                    # ANYWHERE in the cell: rows 96 and 159 were live and invisible,
                    # so joinkey-lint C2 passed against a wrong open set and two
                    # live rows were deleted from the master plan on the strength of
                    # it. A dedicated cell from a closed vocabulary cannot be
                    # misread that way — the ambiguity is gone, not guarded.
                    "done": execution == "executed",
                }
            )
    return out


def open_rows(repo_root):
    """Rows that still carry work. What a cold session must rebuild."""
    return [r for r in parse(repo_root) if not r["done"]]


def has_row(repo_root, row):
    """Whether `row` exists on the board. Used by the parity check, which keys
    on metadata.board_row and NOT on the task id — ids are session-scoped and
    restart at 1, which is the false pass board row 52 was rekeyed to fix."""
    if not str(row).strip():
        return False
    return any(r["row"] == str(row).strip() for r in parse(repo_root))


# The two lifecycle axes, ruled by decision 0274 and shared VERBATIM with the memex
# frontmatter (`memexlint`'s allowedStatus / allowedExecution). One vocabulary, two
# layers: a board row and a decision record mean the same thing by the same word.
#
# DO NOT ADD A VALUE TO EITHER SET TO ADMIT A ROW. Fix the row, or take the missing
# state to Sam as an amendment to 0274 — and if one is added it is added in BOTH
# layers, or the vocabulary has quietly forked.
#
# This supersedes the shape check that preceded it (a status cell must LEAD with a
# state, 2026-08-25). That check existed because the audit of the same property had
# been run with an allowlist grown to fit the board — `built,` and a bare backtick
# were in it BECAUSE rows 11 and 69 started that way, making "0 exceptions" true by
# construction. A closed vocabulary in a dedicated cell is strictly stronger: there is
# no prose for a marker to hide in.
_RULING = frozenset(("proposed", "accepted", "superseded", "rejected"))
_EXECUTION = frozenset(("unstarted", "partial", "executed"))


def vocabulary_violations(repo_root):
    """Rows whose ruling or execution cell is not in the ruled set (decision 0274)."""
    out = []
    for r in parse(repo_root):
        if r["ruling"] not in _RULING:
            out.append((r["row"], "ruling", r["ruling"]))
        if r["execution"] not in _EXECUTION:
            out.append((r["row"], "execution", r["execution"]))
        # Same coherence rule memexlint applies to a record: only an accepted
        # ruling has work in progress. A proposed row cannot be partly built,
        # because what it would be building is not yet decided.
        if r["execution"] in ("partial",) and r["ruling"] != "accepted":
            out.append((r["row"], "coherence",
                        "execution=%s with ruling=%s" % (r["execution"], r["ruling"])))
    return out
