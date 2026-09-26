"""K02 — the cockpit's `tasks` and `phase` blocks: session tasks joined to durable BOARD rows.

**The join runs through the runtime's own readers, never a private parser.** `bearhug_runtime`
is imported from `runtime/` and its `BoardReader`/`RepoReader` answer every board question through
the host's `boardrows`. R10's constraint and the reason for it: until 2026-08-25 done-ness was a
regex over free prose that read a checkmark anywhere in a cell, so rows 96 and 159 were live and
invisible. A second lifecycle vocabulary — here, or in Go — reintroduces exactly that. Decision
0274's two axes (`ruling`, `execution`) are read, never re-derived.

**The task store is read for ids, statuses and `metadata` only.** No subject task text — no
subject, description or prompt — reaches this block or any artifact built from it. The runtime's
`tasks_for` returns those fields; this module drops them, and `tests/test_cockpit.py` asserts the
serialised block carries none of them.

The six mismatch classes, which are the whole point of the block:

| class | means |
|---|---|
| `no_board_row` | a session task carries no `metadata.board_row` — the work is not durable |
| `no_task` | an OPEN board row no session task points at — queued work nobody is holding |
| `moved_row` | `metadata.board_row` names a row the board no longer has (a renumbering) |
| `no_phase_tag` | the joined row carries no `[Pn]` placement tag |
| `unresolved_authority` | the authority path does not resolve to a file in the subject |
| `stale_board_vs_plan` | the row's own phase tag and the plan's placement disagree |

`moved_row` and `no_board_row` are deliberately distinct: conflating them would hide a board
renumbering behind "this task was never wired up", which is a different defect with a different fix.
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from bearhug.paths import BARRACUDA_ROOT, CLAUDE_HOME, REPO_ROOT

#: The runtime's source tree. Imported, never reimplemented (CLAUDE.md: stdlib only, one authority).
RUNTIME_DIR = REPO_ROOT / "runtime"

#: Where the host keeps the shared board parser the coordinator reads through.
HOST_PARSER = Path("scripts") / "hooks" / "boardrows.py"

#: Every mismatch class, always present in the counts so a missing key cannot read as a zero.
MISMATCH_CLASSES = (
    "no_board_row",
    "no_task",
    "moved_row",
    "no_phase_tag",
    "unresolved_authority",
    "stale_board_vs_plan",
)

PLAN_ARTIFACT = "docs/memex/syntheses/MASTER-PLAN.md"

#: G03/K02's authority-path grammar, mirroring the runtime's own `docs/[\w./-]+\.md` shape check
#: (readers.py's private `_DOC_PATH`, task_durability.py's public `DOC_PATH`) rather than a fourth
#: independently-invented regex with different edges. This module already imports the runtime's
#: readers dynamically for board vocabulary (R10); it does not import the evaluator modules, so the
#: shape is duplicated here as a comment-pinned literal instead of a fourth copy with new edges.
_AUTHORITY_PATH = re.compile(r"^docs/[\w./-]+\.md$")


def _unavailable(reason: str) -> dict[str, Any]:
    """No board authority: rows are empty AND the status says why.

    An empty list on its own would read as "the board has no rows", which is a claim this module
    cannot make when it could not read the board at all.
    """
    return {
        "tasks": {
            "status": "unavailable",
            "reason": reason,
            "sources": {},
            "session_id": None,
            "active": None,
            "rows": [],
            "unmatched_board_rows": [],
            "classes": dict.fromkeys(MISMATCH_CLASSES, 0),
        },
        "phase": {
            "status": "unknown",
            "current": None,
            "governing_plan": None,
            "reason": reason,
        },
    }


@contextmanager
def _runtime_readers(subject_root: Path):
    """`bearhug_runtime.readers` with THIS subject's host parser importable beside it.

    Both paths go on `sys.path` and the cached modules are dropped first, so a previous call
    against a different subject cannot leave its `boardrows` behind — which would silently make
    one subject's board answer another's questions.
    """
    hooks = subject_root / "scripts" / "hooks"
    added = [str(RUNTIME_DIR), str(hooks)]
    for entry in added:
        sys.path.insert(0, entry)
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "boardrows" or name == "bearhug_runtime" or name.startswith("bearhug_runtime.")
    }
    for name in saved:
        del sys.modules[name]
    previous_bytecode_setting = sys.dont_write_bytecode
    # Importing the subject's authoritative board parser must not create
    # scripts/hooks/__pycache__ in Barracuda. K06 treats interpreter side effects as writes too.
    sys.dont_write_bytecode = True
    try:
        from bearhug_runtime import readers

        yield readers
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting
        for entry in added:
            with suppress(ValueError):  # another caller already removed it
                sys.path.remove(entry)
        for name in list(sys.modules):
            if name == "boardrows" or name.startswith("bearhug_runtime"):
                del sys.modules[name]
        sys.modules.update(saved)


def tasks_and_phase(
    *,
    subject_root: Path | str | None = None,
    home: Path | str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """The `tasks` and `phase` blocks, joined through the runtime's readers.

    Both are built together because the phase is derived from the active task's board row: they
    would otherwise read two different joins and could disagree.
    """
    root = Path(subject_root) if subject_root is not None else BARRACUDA_ROOT
    home_root = Path(home) if home is not None else CLAUDE_HOME.parent

    if not (root / HOST_PARSER).is_file():
        # Checked before importing rather than after: a `boardrows` left on sys.path by another
        # caller would otherwise satisfy the import and answer with the WRONG subject's parser.
        return _unavailable(
            f"{HOST_PARSER.as_posix()} is not present under {root}. The cockpit reads the board "
            "through the host's own parser so that board-restore, the join-key check and this "
            "pane cannot disagree about what a row is; a private parser here would recreate that "
            "disagreement silently (R10)."
        )
    if not session_id:
        return _unavailable("no session id: the live task store is keyed by it")

    with _runtime_readers(root) as readers:
        try:
            board = readers.board_for(str(root))
            rows = board.rows()
            placements = board.plan_phases()
        except readers.MissingBoardRows as exc:
            return _unavailable(str(exc))
        repo = readers.repo_for(str(root))
        store = readers.tasks_for({"transcript_path": f"{session_id}.jsonl"}, str(home_root))

        return _join(
            rows=rows,
            placements=placements,
            store=store,
            repo=repo,
            root=root,
            home_root=home_root,
            session_id=session_id,
        )


def _authority_status(repo: Any, authority: str) -> str:
    """K02/K03/J01: `exists` / `missing` / `not_a_docs_path`.

    J01's real run found `cockpit_task_authority` unobserved: a boolean "resolves or not" cannot
    distinguish a real gap (nobody wrote the doc yet) from a malformed pointer (the field names
    something never shaped like an authority document). `missing` covers both "no authority was
    ever named" and "a validly-shaped path names a file that is not there"; `not_a_docs_path` is an
    authority string that is not even shaped like `docs/**.md`, whether or not something happens to
    exist at that literal location (e.g. a task pointing at `README.md`).
    """
    if not authority:
        return "missing"
    if not _AUTHORITY_PATH.match(authority):
        return "not_a_docs_path"
    return "exists" if repo.doc_exists(authority) else "missing"


def _plan_phase_for(placements: dict[str, list[str]], row: str | None) -> str | None:
    """The plan's placement for a row, or None. A row placed under two phases is itself a defect
    C2 catches; here the disagreement is reported by joining on the FIRST placement and letting
    `stale_board_vs_plan` fire, rather than picking a winner."""
    if row is None:
        return None
    found = placements.get(row) or []
    return found[0] if found else None


def _join(
    *,
    rows: list[dict[str, Any]],
    placements: dict[str, list[str]],
    store: dict[str, dict[str, Any]],
    repo: Any,
    root: Path,
    home_root: Path,
    session_id: str,
) -> dict[str, Any]:
    by_row = {str(row.get("row")): row for row in rows}
    counts = dict.fromkeys(MISMATCH_CLASSES, 0)

    joined: list[dict[str, Any]] = []
    claimed: set[str] = set()

    # Sorted by task id: the store's directory order is filesystem order, which is not stable
    # across machines, and the TUI must not reshuffle between refreshes.
    for task_id in sorted(store):
        record = store[task_id]
        classes: list[str] = []

        board_row = (record.get("board_row") or "").strip() or None
        row = by_row.get(board_row) if board_row else None
        if board_row is None:
            classes.append("no_board_row")
        elif row is None:
            classes.append("moved_row")
        else:
            claimed.add(board_row)

        phase = (row.get("phase") or "").strip() or None if row else None
        if row is not None and phase is None:
            classes.append("no_phase_tag")

        plan_phase = _plan_phase_for(placements, board_row if row is not None else None)
        if phase is not None and plan_phase is not None and phase != plan_phase:
            classes.append("stale_board_vs_plan")

        # The task's own authority first, the row's as the fallback: older rows carry it inline in
        # prose and the runtime's parser already extracts it.
        authority = (record.get("authority") or "").strip()
        if not authority and row is not None:
            authority = (row.get("authority") or "").strip()
        authority_status = _authority_status(repo, authority)
        if authority and authority_status != "exists":
            classes.append("unresolved_authority")

        for name in classes:
            counts[name] += 1

        joined.append(
            {
                "task_id": task_id,
                "completed": bool(record.get("completed")),
                "status": (record.get("status") or "").strip() or None,
                "board_row": board_row,
                "authority": authority or None,
                "authority_status": authority_status,
                "phase": phase,
                "plan_phase": plan_phase,
                "ruling": (row.get("ruling") or "").strip() or None if row else None,
                "execution": (row.get("execution") or "").strip() or None if row else None,
                "classes": classes,
            }
        )

    # `no_task` is a BOARD-side class: an open row nobody is holding. Board order is kept — it is
    # the queue's own order and the one a reader expects.
    unmatched: list[dict[str, Any]] = []
    for row in rows:
        identifier = str(row.get("row"))
        if row.get("done") or identifier in claimed:
            continue
        counts["no_task"] += 1
        unmatched.append(
            {
                "board_row": identifier,
                "phase": (row.get("phase") or "").strip() or None,
                "plan_phase": _plan_phase_for(placements, identifier),
                "ruling": (row.get("ruling") or "").strip() or None,
                "execution": (row.get("execution") or "").strip() or None,
                "classes": ["no_task"],
            }
        )

    active, phase_block = _active_and_phase(joined)
    return {
        "tasks": {
            "status": "carried",
            "reason": "",
            "sources": {
                "task_store": str(home_root / ".claude" / "tasks" / session_id),
                "board": str(root / "docs" / "superpowers" / "plans" / "BOARD.md"),
                "board_parser": f"{HOST_PARSER.as_posix()} (the host's own, via bearhug_runtime)",
                "plan": str(root / PLAN_ARTIFACT),
            },
            "session_id": session_id,
            "active": active,
            "rows": joined,
            "unmatched_board_rows": unmatched,
            "classes": counts,
        },
        "phase": phase_block,
    }


def _active_and_phase(joined: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The active task and the phase derived from its row.

    "Active" is the one task whose lifecycle status is `in_progress`, NOT "the one open task":
    decisions 0260/0137 keep more than one session task open at once, so keying on open-ness
    resolved to nothing and left the phase perpetually `unknown` (J01). With no in-progress task
    there is nothing in flight; with several the active one is not derivable from the store alone,
    and the block says so rather than picking one and presenting a guess as the answer.
    """
    open_tasks = [row for row in joined if not row["completed"]]
    in_progress = [row for row in open_tasks if row.get("status") == "in_progress"]
    unknown = {"status": "unknown", "current": None, "governing_plan": None, "reason": ""}

    if not open_tasks:
        unknown["reason"] = "no open session task; nothing is in flight"
        return None, unknown
    if not in_progress:
        unknown["reason"] = (
            f"{len(open_tasks)} open session task(s), none in progress; nothing is in flight"
        )
        return None, unknown
    if len(in_progress) > 1:
        unknown["reason"] = (
            f"{len(in_progress)} session tasks in progress; the active one is not derivable from "
            "the store alone"
        )
        return None, unknown

    active = in_progress[0]
    if active["phase"] is None:
        unknown["reason"] = (
            "the active task's board row carries no phase tag"
            if active["board_row"]
            else "the active task has no board row to derive a phase from"
        )
        return active, unknown

    return active, {
        "status": "derived",
        "current": active["phase"],
        "governing_plan": PLAN_ARTIFACT,
        "reason": f"the active task's board row carries phase {active['phase']}",
    }


__all__ = ["MISMATCH_CLASSES", "PLAN_ARTIFACT", "tasks_and_phase"]
