"""Production readers for the injected dependencies R09 and R10 declare.

R09 and R10 take `tasks`, `repo` and `board` as arguments so the evaluators stay pure and
testable. Something has to supply them in production, and until this module existed nothing did —
the adapter referenced readers that were never written, and its outer `except` swallowed the
ModuleNotFoundError, so every Stop would have passed silently. That is crash-reads-as-consent at
the promotion layer.

**These readers do I/O; the evaluators do not.** That separation is the whole point: all the
filesystem access the Stop path needs lives here, in one place, called by the adapter and injected
downward.

**The board is read through the host's `boardrows`, never re-parsed.** R10's constraint, and the
reason for it: until 2026-08-25 done-ness was a regex over free prose that read a checkmark
anywhere in the cell, so rows 96 and 159 were live and invisible. A second lifecycle vocabulary
here would reintroduce exactly that.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

#: The board's path relative to the repository root, as the captured gates name it.
BOARD = "docs/superpowers/plans/BOARD.md"
PLAN = "docs/memex/syntheses/MASTER-PLAN.md"
LEDGER = "docs/superpowers/plans/LEDGER.md"

_DOC_PATH = re.compile(r"docs/[\w./-]+\.md")

#: C5's gate citation, ported verbatim from the captured gate.
#:
#: The negative lookbehind is load-bearing: a citation written "was GATED BY ROW N" records a
#: LIFTED gate, and firing on it would re-block that row forever. A row whose status says
#: UNBLOCKED is skipped for the same reason.
_GATE_CITATION = re.compile(r"(?<![Ww]as )GATED BY ROW ([0-9]+)")
_LEDGER_JOIN = re.compile(r"JOIN row ([0-9]+)")
_PLAN_TAG = re.compile(
    r"^\|\s*(\*\*)?(P[0-9][0-9]*(?:·hold)?|corpus|UNPLACED)(\*\*)?\s*\|"
)


class MissingBoardRows(Exception):
    """The host's `boardrows` module could not be imported.

    Raised rather than worked around. A reader that silently fell back to its own parser would
    become the second lifecycle authority R10 exists to prevent, and would do it invisibly.
    """


def _boardrows():
    try:
        import boardrows  # the host's shared parser, beside the adapter on sys.path
    except ImportError as exc:  # pragma: no cover - exercised by the constructed failure test
        raise MissingBoardRows(
            "scripts/hooks/boardrows.py is not importable. The coordinator reads the board "
            "through it so that board-restore and the join-key check cannot disagree about what "
            "a row is; falling back to a private parser here would recreate that disagreement "
            "silently."
        ) from exc
    return boardrows


def transcript_lines_for(payload: Any) -> list[str]:
    """The transcript's lines, or an empty list when there is none.

    Empty rather than raising: a Stop event with no transcript is a real, ordinary state, and the
    evaluators already treat it as not-applicable.
    """
    path = (payload or {}).get("transcript_path") or ""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def transcript_readable_for(payload: Any) -> bool:
    """Whether task-durability's named transcript exists, measured before evaluation.

    The legacy gate checks this before reading the live task store.  Keeping the filesystem read
    here preserves that branch without putting I/O back into the pure evaluator.
    """
    path = (payload or {}).get("transcript_path") or ""
    return bool(path and os.path.isfile(path))


def tasks_for(payload: Any, home: str | None = None) -> dict[str, dict[str, Any]]:
    """The LIVE task store for this session: `~/.claude/tasks/<session-id>/*.json`.

    Not the transcript. The captured gate's docstring records why: replaying TaskCreate/TaskUpdate
    out of the JSONL cannot observe a task leaving the list by any route that writes no
    `status: deleted` update, so it reports tasks that no longer exist — an unsatisfiable block.
    Measured 2026-08-14: three tasks replayed as open while the store held no directory at all.
    """
    transcript = (payload or {}).get("transcript_path") or ""
    session_id = os.path.basename(transcript)
    if session_id.endswith(".jsonl"):
        session_id = session_id[: -len(".jsonl")]
    if not session_id:
        return {}

    store = os.path.join(home or os.path.expanduser("~"), ".claude", "tasks", session_id)
    if not os.path.isdir(store):
        return {}

    tasks: dict[str, dict[str, Any]] = {}
    try:
        names = sorted(os.listdir(store))
    except OSError:
        return {}

    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(store, name), encoding="utf-8", errors="replace") as handle:
                record = json.load(handle)
        except (ValueError, OSError):
            continue
        if not isinstance(record, dict):
            continue
        identifier = str(record.get("id") or name[: -len(".json")])
        subject = (record.get("subject") or "").strip()
        metadata = record.get("metadata") or {}
        status = str(record.get("status") or "").strip()
        tasks[identifier] = {
            "subject": subject,
            "text": subject + "\n" + (record.get("description") or ""),
            # `completed` is the boolean the durability evaluator keys on; `status` carries the raw
            # lifecycle word so a reader can tell `in_progress` from `pending` — a distinction the
            # boolean collapses. Additive: every existing consumer reads `completed`.
            "completed": status == "completed",
            "status": status,
            "board_row": str(metadata.get("board_row") or "").strip(),
            "authority": str(metadata.get("authority") or "").strip(),
            "open_question": bool(metadata.get("open_question")),
        }
    return tasks


class RepoReader:
    """Answers the filesystem questions R09 asks, rooted at one repository."""

    def __init__(self, root: str) -> None:
        self._root = root

    def doc_exists(self, path: str) -> bool:
        if not path:
            return False
        return os.path.isfile(os.path.join(self._root, path))

    def board_exists(self) -> bool:
        return os.path.isfile(os.path.join(self._root, BOARD))

    def has_board_row(self, row: str) -> bool:
        """Delegated to the host's parser, never re-derived."""
        if not row:
            return False
        try:
            return bool(_boardrows().has_row(self._root, row))
        except MissingBoardRows:
            raise
        except Exception:  # noqa: BLE001 - a malformed board is not this reader's to interpret
            return False


class BoardReader:
    """Answers the board questions R10 asks, entirely through the host's `boardrows`."""

    def __init__(self, root: str) -> None:
        self._root = root

    def rows(self) -> list[dict[str, Any]]:
        """The host's parsed rows, plus the one field the evaluator needs that they do not carry.

        `boardrows.parse()` emits row/entry/status/blocked/authority/ruling/execution/phase/done —
        there is no `gated_by`. C5 asks whether an OPEN row cites a DONE row as a LIVE gate, and
        the citation lives in the `status` text. Extracting it is the reader's job; deciding what
        it means is the evaluator's.

        Both historical exclusions are preserved: a status containing UNBLOCKED is skipped, and
        "was GATED BY ROW N" does not fire. Without them every lifted gate would re-block forever.
        """
        parsed = []
        for row in _boardrows().parse(self._root):
            enriched = dict(row)
            status = str(row.get("status") or "")
            citations: list[str] = []
            if "UNBLOCKED" not in status:
                citations = [match.group(1) for match in _GATE_CITATION.finditer(status)]
            enriched["gated_by"] = citations
            parsed.append(enriched)
        return parsed

    def vocabulary_violations(self) -> list[tuple[str, str, str]]:
        return list(_boardrows().vocabulary_violations(self._root))

    def plan_phases(self) -> dict[str, list[str]]:
        """Which plan phase(s) each board row is placed under.

        A row listed under two phases is a defect C2 must catch, so every occurrence is kept
        rather than the last one winning.
        """
        placements: dict[str, list[str]] = {}
        path = os.path.join(self._root, PLAN)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return placements

        in_table = False
        for line in text.splitlines():
            if "Open rows by phase" in line:
                in_table = True
                continue
            if not in_table:
                continue
            match = _PLAN_TAG.match(line)
            if not match:
                if line.startswith("|") and "total" in line.lower():
                    break
                continue
            phase = match.group(2).strip().strip("*").split("·")[0]
            cells = line.split("|")
            rowlist = cells[3] if len(cells) > 3 else ""
            for token in rowlist.replace("–", "-").split(","):
                token = re.sub(r"[^0-9-]", "", token)
                if not token:
                    continue
                if re.match(r"^[0-9]+-[0-9]+$", token):
                    first, last = token.split("-")
                    for row in range(int(first), int(last) + 1):
                        placements.setdefault(str(row), []).append(phase)
                elif token.isdigit():
                    placements.setdefault(token, []).append(phase)
        return placements

    def ledger_joins(self) -> list[str]:
        path = os.path.join(self._root, LEDGER)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return _LEDGER_JOIN.findall(handle.read())
        except OSError:
            return []

    def cell_counts(self) -> list[tuple[str, int]] | None:
        """(row id, cell count) for every data row, counted with the HOST's splitter — C8.

        Barracuda decision 0304 (2026-09-01): a bare `|` inside a cell is a column break, so a
        row quoting a regex shifts every cell to its right while each later value stays a
        plausible string; only the cell COUNT sees it. `boardrows._split_cells` honours the GFM
        escape `\\|`, and this reader uses it and `boardrows._ROW` rather than a private split,
        so the runtime cannot count differently from the captured gate.

        None — not seven, not an empty list — when the host's boardrows predates C8 and has no
        `_split_cells`: the constraint is then UNMEASURED and the evaluator says so in evidence.
        """
        boardrows = _boardrows()
        row_pattern = getattr(boardrows, "_ROW", None)
        split = getattr(boardrows, "_split_cells", None)
        if row_pattern is None or split is None:
            return None
        path = os.path.join(self._root, BOARD)
        counts: list[tuple[str, int]] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    match = row_pattern.match(line.rstrip("\n"))
                    if match:
                        counts.append((match.group(1), len(split(match.group(2)))))
        except OSError:
            return []
        return counts

    def active_marker_rows(self) -> list[str]:
        """Rows carrying the ACTIVE pointer. C7 allows one, and zero when nothing is in flight."""
        path = os.path.join(self._root, BOARD)
        marker = "\U0001f535"
        found: list[str] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if marker not in line:
                        continue
                    stripped = line.lstrip()
                    if not stripped.startswith("|"):
                        continue
                    cells = stripped.split("|")
                    if len(cells) < 2:
                        continue
                    row = cells[1].strip()
                    if row.isdigit():
                        found.append(row)
        except OSError:
            return []
        return found


def repo_for(root: str) -> RepoReader:
    return RepoReader(root)


def board_for(root: str) -> BoardReader:
    return BoardReader(root)
