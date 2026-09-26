"""Portable managed-work and project-authored BOARD integration.

Fresh managed projects use WORK.json as their sole task-state authority. Established projects may
instead select one numeric row from their own BOARD explicitly; in that mode Bear Hug's trusted
seven-cell parser and checked, journaled BOARD transitions remain authoritative and WORK.json is
never created.
This file is copied intact into projects and therefore uses only the standard library.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from bearhug.project_board import parse_board_bytes
except ModuleNotFoundError:
    # The installed copy runs as ``<root>/scripts/bearhug_work.py`` from shims and provider
    # hooks that set no library path; its shared parser lives in ``<root>/.bearhug/lib``.
    _INSTALLED_LIBRARY = str(Path(__file__).resolve().parent.parent / ".bearhug" / "lib")
    if _INSTALLED_LIBRARY not in sys.path:
        sys.path.insert(0, _INSTALLED_LIBRARY)
    from bearhug.project_board import parse_board_bytes

STATE_PATH = "docs/superpowers/plans/WORK.json"
MANAGED_STATE_FALLBACK_PATH = "docs/superpowers/plans/WORK.managed.json"
PLAN_DIR = "docs/superpowers/plans"
ADOPTION_PATH = ".bearhug/adoption.json"
PROJECT_BOARD_OWNER_PATH = ".bearhug/project-board-owner.json"
PROJECT_BOARD_TRANSITION_PATH = ".bearhug/project-board-transition.json"
PROJECT_BOARD_RECEIPTS_PATH = ".bearhug/project-board-transition-receipts"
STATES = {"pending", "in_progress", "completed", "blocked"}
ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


class WorkError(ValueError):
    """Invalid input or a task transition that requires operator attention."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise WorkError("Use a repository-relative path inside the project")
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise WorkError(f"Refusing symlink: {relative}")
    return current


def _read_plan(root: Path, relative: str) -> tuple[str, str]:
    path = _path(root, relative)
    if path.suffix.lower() != ".md":
        raise WorkError("The accepted plan must be a Markdown file")
    content = path.read_bytes()
    if len(content) > 2 * 1024 * 1024:
        raise WorkError("Plan exceeds 2 MiB")
    return content.decode("utf-8"), hashlib.sha256(content).hexdigest()


def _title(text: str, fallback: str) -> str:
    return next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), fallback)


def _validate_tasks(tasks: list[dict[str, Any]]) -> None:
    by_id = {}
    for task in tasks:
        ident = task.get("id", "")
        if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in by_id:
            raise WorkError(f"Task IDs must be unique letters/digits/hyphens: {ident}")
        if not all(
            isinstance(task.get(k), str) and task[k].strip() for k in ("title", "done_when")
        ):
            raise WorkError(f"Task {ident} needs a title and completion criteria")
        if task.get("status") not in STATES:
            raise WorkError(f"Invalid state for {ident}")
        if task.get("lane", DEFAULT_LANE) not in LANES:
            raise WorkError(f"Task {ident} has an unknown lane: {task.get('lane')}")
        deps = task.get("depends_on")
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
            raise WorkError(f"Invalid dependencies for {ident}")
        by_id[ident] = task
    visited, visiting = set(), set()

    def visit(ident: str) -> None:
        if ident in visiting:
            raise WorkError(f"Dependency cycle at {ident}")
        if ident in visited:
            return
        if ident not in by_id:
            raise WorkError(f"Unknown dependency: {ident}")
        visiting.add(ident)
        for dep in by_id[ident]["depends_on"]:
            visit(dep)
        visiting.remove(ident)
        visited.add(ident)

    for ident in by_id:
        visit(ident)

    # Interactive-lane work runs before the campaign, never after or alongside it (the lead's
    # ruling on B2/the deadlock and sandwich a reviewer found): an interactive task may depend
    # only on other interactive tasks, or nothing. A campaign-lane task may depend on an
    # interactive-lane task; the reverse, at any distance, is refused here rather than accepted
    # into a plan the campaign and the board can never both run to completion.
    transitive: dict[str, frozenset[str]] = {}

    def ancestors(ident: str) -> frozenset[str]:
        if ident in transitive:
            return transitive[ident]
        result: set[str] = set()
        for dep in by_id[ident]["depends_on"]:
            result.add(dep)
            result |= ancestors(dep)
        transitive[ident] = frozenset(result)
        return transitive[ident]

    for ident, task in by_id.items():
        if task.get("lane", DEFAULT_LANE) != "interactive":
            continue
        campaign_ancestor = next(
            (
                dep
                for dep in ancestors(ident)
                if by_id[dep].get("lane", DEFAULT_LANE) != "interactive"
            ),
            None,
        )
        if campaign_ancestor is not None:
            raise WorkError(
                f"Interactive-lane task {ident} depends (directly or transitively) on "
                f"campaign-lane task {campaign_ancestor}. Interactive work runs before the "
                "campaign, never after it; put that follow-up work in a plan written after "
                "this one integrates."
            )


BOARD_ROW = re.compile(r"^\s*board row:\s*([1-9][0-9]*)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_board_row(text: str) -> int | None:
    """Return the project board row a plan binds itself to (`Board row: 240`), or None.

    The row is a declaration, never inferred: it makes every task's `board_row` and the native
    task metadata name the project's real row, so a project join key such as Barracuda's
    `metadata.board_row` stays truthful instead of carrying Bear Hug's private counter. Rows are
    integers, the same type as Bear Hug's own counter, so every reader keeps one schema.
    """
    rows = {int(match.group(1)) for match in BOARD_ROW.finditer(text)}
    if len(rows) > 1:
        raise WorkError("Declare at most one `Board row: N` line in the accepted plan")
    return rows.pop() if rows else None


LANES = {"campaign", "interactive"}
DEFAULT_LANE = "campaign"
_LANE_NAME = r"[A-Za-z][A-Za-z0-9_-]*"
# A marker must sit in a backtick pair or a parenthesis pair -- never bare text -- and the
# delimiter must be preceded by whitespace or the start of the cell, so ordinary prose ending in
# a phrase such as "the swim lane: interactive" (no delimiter, lower-case `lane`) never matches.
_LANE_MARKER = re.compile(
    rf"(?:^|(?<=\s))(?:`Lane:\s*({_LANE_NAME})`|\(Lane:\s*({_LANE_NAME})\))"
)


def _split_lane(title: str) -> tuple[str, str]:
    """Read an optional `` `Lane: <name>` `` or ``(Lane: <name>)`` marker out of a task's Task
    cell; it must be the last thing in the cell, and there must be at most one.

    A lane is declared in the task's own row, not a fifth table column: the parser's task
    table keeps its four-column contract (`ID | Task | Depends on | Done when`), so every
    existing plan and board reader keeps working unchanged. Absent the marker a task is on
    the `campaign` lane, exactly today's behaviour.
    """
    matches = list(_LANE_MARKER.finditer(title))
    if not matches:
        return title, DEFAULT_LANE
    if len(matches) > 1:
        raise WorkError("A task's Task cell may declare at most one lane marker")
    match = matches[0]
    if title[match.end() :].strip():
        raise WorkError(
            "A task's lane marker must be the last thing in its Task cell"
        )
    lane = (match.group(1) or match.group(2)).strip().lower()
    if lane not in LANES:
        raise WorkError(f"Unknown lane: {lane}")
    return title[: match.start()].rstrip(), lane


def parse_tasks(text: str) -> list[dict[str, Any]]:
    """Read one explicit task table; never guess tasks from prose or headings."""
    tasks: list[dict[str, Any]] = []
    in_table = False
    fenced = False
    found = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        cells = [
            cell.strip().replace(r"\|", "|")
            for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
        ]
        if [cell.casefold() for cell in cells] == ["id", "task", "depends on", "done when"]:
            if found:
                raise WorkError("Use one task table in the accepted plan")
            in_table = found = True
            continue
        if not in_table:
            continue
        if not line.strip().startswith("|"):
            in_table = False
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if len(cells) != 4:
            raise WorkError("Each task row needs ID, Task, Depends on and Done when")
        ident, title, dependencies, done = cells
        title, lane = _split_lane(title)
        deps = (
            []
            if dependencies in {"", "-", "—", "none", "None"}
            else [value.strip() for value in dependencies.split(",")]
        )
        if deps and not all(ID.fullmatch(dep) for dep in deps):
            raise WorkError(
                "The Depends on column takes task ids or — only; "
                f"got {dependencies!r}"
            )
        tasks.append(
            {
                "id": ident,
                "title": title,
                "depends_on": deps,
                "done_when": done,
                "lane": lane,
                "status": "pending",
                "session_id": "",
                "provider": "",
                "evidence": "",
                "updated_at": "",
            }
        )
    if not tasks:
        raise WorkError("Add a task table with columns: ID | Task | Depends on | Done when")
    if len(tasks) > 500:
        raise WorkError("A project plan may contain at most 500 tasks")
    _validate_tasks(tasks)
    return tasks


_LANE_PROJECTED_TASK = (
    "Completed outside this campaign; do not perform; not part of this candidate."
)


def project_lane_text(text: str) -> str:
    """Rewrite every interactive-lane task's row so its real content never reaches a capsule
    worker, a reviewer, or grounding.

    The campaign controller's own request embeds the accepted plan's exact prose (``_board_
    request``/``_accepted_prompt``), and grounding extracts terms from that same prose
    (``compile_grounding``'s ``plan_text``). Neither one may see a browser or capture task's real
    Task/Done-when text -- a worker told to "execute the accepted plan" would attempt it, and a
    reviewer checking obligation coverage has nothing in the candidate diff to check it against.
    An interactive-lane row's Task and Done-when cells are both replaced here with the same fixed
    neutral statement; its ID, dependencies and table position are left alone, so the table a
    reader (or a dependency reference) sees still lines up. This also removes the row's
    ```Lane: ...``` marker from what grounding ever reads, since the whole cell it lived in is
    replaced.

    A pure function of ``text`` alone -- lanes are read straight back out of the same bytes
    with :func:`parse_tasks`, never from mutable board state (a task's current ``status``,
    ``evidence`` or ``session_id``). The sealed request a campaign builds at intake, and the one
    ``status()`` recomputes from the accepted plan on every later call, must be byte-identical as
    long as the accepted plan itself has not changed; a projection that read anything else would
    change out from under a live campaign the moment `bearhug-work complete`/`block`/an
    interruption touched an interactive task's board row, detaching the campaign's own custody
    view from a locator that is still running (an unescaped evidence string could go further and
    forge extra table rows in the sealed text). Neither risk exists when the only inputs are the
    plan's own accepted bytes.

    Walks the same table the parser recognises (fenced-block and header detection identical to
    :func:`parse_tasks`), so it only ever touches a genuine task row.
    """
    lanes = {task["id"]: task["lane"] for task in parse_tasks(text)}
    in_table = False
    fenced = False
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        ending = line[len(line.rstrip("\r\n")) :]
        body = line[: len(line) - len(ending)]
        if body.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        cells = [
            cell.strip().replace(r"\|", "|")
            for cell in re.split(r"(?<!\\)\|", body.strip().strip("|"))
        ]
        if [cell.casefold() for cell in cells] == ["id", "task", "depends on", "done when"]:
            in_table = True
            out.append(line)
            continue
        if not in_table:
            out.append(line)
            continue
        if not body.strip().startswith("|"):
            in_table = False
            out.append(line)
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            out.append(line)
            continue
        if len(cells) != 4:
            out.append(line)
            continue
        ident = cells[0]
        if lanes.get(ident, DEFAULT_LANE) == DEFAULT_LANE:
            out.append(line)
            continue
        out.append(
            f"| {ident} | {_LANE_PROJECTED_TASK} | {cells[2]} | {_LANE_PROJECTED_TASK} |{ending}"
        )
    return "".join(out)


def _load(root: Path) -> dict[str, Any] | None:
    path = _managed_state_path(root)
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise WorkError("Unsupported project-work state")
    if not isinstance(state.get("plan"), dict) or not isinstance(state.get("tasks"), list):
        raise WorkError("Invalid project-work plan or tasks")
    if not isinstance(state.get("history"), list):
        raise WorkError("Invalid project-work history")
    _path(root, state["plan"]["path"])
    _validate_tasks(state["tasks"])
    return state


_WORKV2_SCHEMA = "bearhug-work/2"


def _load_workv2(root: Path) -> dict[str, Any] | None:
    """Return the imported v2 board document, or None if absent or not v2.

    The v2 board (``bearhug-work import``) is a different document than v1 managed
    work: it is read here for status and observation, never through ``_load``,
    which validates the v1 accepted-plan shape and rejects anything else. This is
    the CLI/startup counterpart of the cockpit's own v2 reader — a v2 file must
    read as a managed board, never as an "unsupported project-work state" error.
    """
    path = _path(root, STATE_PATH)
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if isinstance(document, dict) and document.get("schema") == _WORKV2_SCHEMA:
        return document
    return None


def _managed_state_path(root: Path) -> Path:
    """Return where v1 managed state (an accepted plan) lives.

    Ordinarily this is the primary path. But an established project may already carry an
    imported v2 board (``bearhug-work import``) at the primary path; that document is read-only
    and stays exactly where it is. A plan accepted afterward is a second, independent thing —
    tracked at a fixed fallback path instead, so ``accept`` never has to choose between two
    documents at one location. Reuses ``_load_workv2``'s own read and validation — one
    definition, one guard — rather than re-parsing the primary file and checking its ``schema``
    key a second time; ``_load_workv2`` also rejects non-object JSON (``[]``, ``null``, a bare
    number), which a bare ``.get("schema")`` here would not. When no v2 document is at the
    primary path (every project today, including fresh ones), this returns the primary path
    unchanged and every existing code path behaves exactly as it does now.
    """
    if _load_workv2(root) is not None:
        return _path(root, MANAGED_STATE_FALLBACK_PATH)
    return _path(root, STATE_PATH)


def _workv2_status(document: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Report an imported v2 board as an active managed state, never an error."""
    board = document.get("board", {})
    units = board.get("units", []) if isinstance(board, dict) else []
    rows = [u for u in units if isinstance(u, dict) and u.get("kind") == "row"]
    counts: dict[str, int] = {}
    for row in rows:
        fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        state = str(fields.get("readiness", "unknown"))
        counts[state] = counts.get(state, 0) + 1
    order = ["ready", "blocked-by-dep", "blocked-by-ruling", "context-missing", "done", "unknown"]
    parts = [f"{counts[s]} {s}" for s in order if counts.get(s)]
    parts += [f"{counts[s]} {s}" for s in sorted(counts) if s not in order]
    summary = " · ".join(parts) if parts else "no rows"
    result.update(
        status="active",
        reason=f"Imported board (WORK.json v2): {len(rows)} rows — {summary}",
        schema=_WORKV2_SCHEMA,
    )
    return result


def _adoption(root: Path) -> dict[str, Any] | None:
    path = _path(root, ADOPTION_PATH)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkError(f"Project adoption record is unreadable: {exc}") from exc
    required = {"schema_version", "record_kind", "campaigns"}
    optional = {"work_authority", "exclude_paths"}
    if (
        not isinstance(value, dict)
        or not required <= set(value) <= required | optional
        or value.get("schema_version") != "1"
        or value.get("record_kind") != "bearhug_project_adoption"
        or value.get("campaigns") not in {"on", "off"}
    ):
        raise WorkError("Project adoption record is malformed")
    exclude_paths = value.get("exclude_paths", [])
    if not isinstance(exclude_paths, list) or not all(
        isinstance(item, str) for item in exclude_paths
    ):
        raise WorkError("Project adoption record's exclude_paths is malformed")
    authority = value.get("work_authority", {"mode": "managed"})
    if not isinstance(authority, dict) or authority.get("mode") not in {
        "managed",
        "project_board",
    }:
        raise WorkError("Project work authority selection is malformed")
    if authority["mode"] == "managed":
        if set(authority) != {"mode"}:
            raise WorkError("Managed work authority has unknown fields")
    else:
        fields = {
            "mode",
            "board_path",
            "parser_path",
            "board_row",
            "authority_path",
            "authority_sha256",
        }
        if set(authority) != fields:
            raise WorkError("Project BOARD authority has missing or unknown fields")
        if authority["board_path"] != f"{PLAN_DIR}/BOARD.md":
            raise WorkError("Project BOARD authority names an unsupported board path")
        if authority["parser_path"] != "scripts/hooks/boardrows.py":
            raise WorkError("Project BOARD authority names an unsupported parser path")
        if not isinstance(authority["board_row"], str) or not re.fullmatch(
            r"[1-9][0-9]*", authority["board_row"]
        ):
            raise WorkError("Project BOARD authority row is invalid")
        _path(root, authority["authority_path"])
        if not re.fullmatch(r"[0-9a-f]{64}", str(authority["authority_sha256"])):
            raise WorkError("Project BOARD authority digest is invalid")
    value["work_authority"] = authority
    return value


def _project_board_authority(root: Path) -> dict[str, Any] | None:
    adoption = _adoption(root)
    if adoption is None or adoption["work_authority"]["mode"] != "project_board":
        return None
    return adoption["work_authority"]


def _project_board_rows(root: Path, authority: dict[str, Any]) -> list[dict[str, Any]]:
    board = _path(root, authority["board_path"])
    if board.is_symlink() or not board.is_file() or board.stat().st_size > 32 * 1024 * 1024:
        raise WorkError("project BOARD is missing, symlinked, or unbounded")
    board_before = board.read_bytes()
    rows = parse_board_bytes(board_before, error=WorkError)
    if board_before != board.read_bytes():
        raise WorkError("Project BOARD changed while it was read")
    return rows


def _project_board_owner(root: Path, board_row: str) -> dict[str, str]:
    path = _path(root, PROJECT_BOARD_OWNER_PATH)
    if not path.is_file():
        return {"board_row": board_row, "session_id": "", "provider": "", "state": "idle"}
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkError(f"Project BOARD session binding is unreadable: {exc}") from exc
    fields = {"board_row", "session_id", "provider", "state", "updated_at"}
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("board_row") != board_row
        or value.get("provider") not in {"", "codex", "claude"}
        or value.get("state") not in {"active", "interrupted", "completed"}
        or not isinstance(value.get("session_id"), str)
    ):
        raise WorkError("Project BOARD session binding is malformed or belongs to another row")
    return value


def _project_board_state(root: Path, authority: dict[str, Any]) -> dict[str, Any]:
    rows = _project_board_rows(root, authority)
    matches = [row for row in rows if str(row.get("row")) == authority["board_row"]]
    if len(matches) != 1:
        raise WorkError("Selected project BOARD row is missing or ambiguous")
    row = matches[0]
    if row.get("authority") != authority["authority_path"]:
        raise WorkError("Selected project BOARD row changed its authority path")
    ruling = row.get("ruling")
    execution = row.get("execution")
    if ruling not in {"proposed", "accepted", "superseded", "rejected"}:
        raise WorkError("Selected project BOARD row has an unsupported ruling")
    if execution not in {"unstarted", "partial", "executed"}:
        raise WorkError("Selected project BOARD row has an unsupported execution state")
    plan_text, current_sha256 = _read_plan(root, authority["authority_path"])
    owner = _project_board_owner(root, authority["board_row"])
    blocked = str(row.get("blocked") or "").strip()
    status = {"unstarted": "pending", "partial": "in_progress", "executed": "completed"}[execution]
    if status != "completed" and blocked not in {"", "—", "-", "none", "None"}:
        status = "blocked"
    if execution != "partial" or owner["state"] not in {"active", "interrupted"}:
        session_id = provider = ""
    else:
        session_id, provider = owner["session_id"], owner["provider"]
    title = str(row.get("entry") or f"BOARD row {authority['board_row']}").strip()
    notes = str(row.get("status") or "").strip()
    return {
        "schema_version": 1,
        "authority_mode": "project_board",
        "plan": {
            "path": authority["authority_path"],
            "sha256": authority["authority_sha256"],
            "title": _title(plan_text, authority["authority_path"]),
        },
        "tasks": [
            {
                "id": f"board-{authority['board_row']}",
                "board_row": authority["board_row"],
                "title": title,
                "done_when": notes or f"Project BOARD row {authority['board_row']} is executed",
                "depends_on": [],
                "status": status,
                "session_id": session_id,
                "provider": provider,
                "evidence": notes,
                "updated_at": owner.get("updated_at", ""),
                "owner_state": owner["state"],
            }
        ],
        "history": [],
        "updated_at": owner.get("updated_at", ""),
        "current_authority_sha256": current_sha256,
    }


@contextlib.contextmanager
def _locked(root: Path):
    lock = _path(root, ".bearhug/project-work.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def _campaign_authority_locked(root: Path):
    """Serialize accepted-plan/profile authority through dispatch, independently of BOARD locks.

    BOARD transitions may invoke dispatch in a child while holding `_locked`. Every authority
    writer takes this separate fence after the BOARD lock (if any); dispatch never takes the
    BOARD lock. Its location cannot move when a campaign profile/private root changes.
    """
    directory = _campaign_authority_root()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise WorkError("Project authority lock directory is not privately owned")
    if metadata.st_mode & 0o077:
        raise WorkError("Project authority lock directory must be owner-only")
    key = hashlib.sha256(os.fsencode(root.resolve(strict=True))).hexdigest()
    descriptor = os.open(
        directory / f"project-authority-{key}.lock",
        os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1 or metadata.st_mode & 0o077
        ):
            raise WorkError("Project authority lock must be an owner-only regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _campaign_authority_root() -> Path:
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".local/state/bearhug/operation-locks"


def _write_json(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".WORK-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


GENERATED = "<!-- Generated by Bear Hug from WORK.json; do not edit task state here. -->"


def _projections(root: Path, state: dict[str, Any]) -> dict[Path, str]:
    def cell(value: Any) -> str:
        return str(value).replace("\n", " ").replace("|", r"\|")

    board_lines = [
        GENERATED,
        "# Project task board",
        "",
        "| # | Entry | Ruling | Execution | Notes | Blocked by | Authority |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task in state["tasks"]:
        execution = {
            "pending": "unstarted",
            "in_progress": "partial",
            "blocked": "partial",
            "completed": "executed",
        }[task["status"]]
        values = [
            task["board_row"],
            f"[UNPLACED] {task['id']} — {task['title']}",
            "accepted",
            execution,
            f"{task['status']}; {task['evidence'] or task['done_when']}",
            ", ".join(task["depends_on"]),
            state["plan"]["path"],
        ]
        board_lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    ledger = [
        GENERATED,
        "# Project work history",
        "",
        "| Time | Task | Action | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for event in state["history"]:
        values = [
            event["at"],
            event.get("task_id", "—"),
            event["action"],
            event.get("evidence", event["plan_path"]),
        ]
        ledger.append("| " + " | ".join(cell(value) for value in values) + " |")
    return {
        _path(root, f"{PLAN_DIR}/BOARD.md"): "\n".join(board_lines) + "\n",
        _path(root, f"{PLAN_DIR}/LEDGER.md"): "\n".join(ledger) + "\n",
    }


def _save(root: Path, state: dict[str, Any]) -> None:
    state.pop("execution_authority", None)
    target = _managed_state_path(root)
    imported = target != _path(root, STATE_PATH)  # a v2 import owns BOARD.md/LEDGER.md instead
    views = _projections(root, state)
    # A project that authors its own BOARD.md or LEDGER.md keeps them: those files are cited as
    # historical evidence by the project's own records, and Bear Hug's views are only a
    # convenience for readers. WORK.json remains the authority either way; the state records
    # which views were left alone so the dashboard can say so instead of implying they are Bear
    # Hug's. When a v2 import owns the primary path, BOARD.md/LEDGER.md are the IMPORT's own
    # views (repair-views-v2's job, never this function's), whatever their first line says: the
    # GENERATED marker alone cannot tell an import's generated-looking board from Bear Hug's own,
    # so this shape refuses the write unconditionally instead of trusting the marker.
    preserved = sorted(
        path.name
        for path in views
        if path.exists() and (imported or not path.read_text().startswith(GENERATED))
    )
    if preserved:
        state["project_authored_views"] = preserved
    else:
        state.pop("project_authored_views", None)
    _write_json(target, state)
    # These are compatibility views for existing board readers and tools.
    # A later operation can regenerate them after interruption; WORK.json remains authority.
    for path, text in views.items():
        if imported or path.name in preserved:
            continue
        fd, temporary = tempfile.mkstemp(prefix=".board-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def _record(state: dict[str, Any], action: str, **fields: Any) -> None:
    when = _now()
    state["updated_at"] = when
    state["history"].append(
        {"action": action, "at": when, "plan_path": state["plan"]["path"], **fields}
    )


_LIVE_BOARD_ROW_LINE = re.compile(r"^\|\s*(\d+)\s*\|(.*)$")


def _v2_import_row_exists(root: Path, row: int) -> bool:
    """Whether `row` names a row of the project's own live BOARD.md.

    Checked against the live file (not the v2 import's stored snapshot, which only reflects the
    board as of the last `bearhug-work import`/`--force` and can lag behind edits made since), by
    the same row rule the Stop gate applies: a line matching `^\\|<digits>\\|...`, on universal
    newlines (``\\r\\n``/``\\r``/``\\n``), the way the gate's own board reader splits a file —
    never Bear Hug's own board parser, which also splits on other Unicode line separators (so a
    row can sit on a "line" for one reader and not the other) and raises on any malformed row
    anywhere in the file (so one unrelated bad row would refuse every declaration). This does
    not read the project's own installed board-row script, which would run project code; it
    reads only the row shape both readers already agree on. One undecodable byte does not stop
    the read; a missing BOARD.md is a named refusal, not an uncaught exception.
    """
    board = _path(root, f"{PLAN_DIR}/BOARD.md")
    try:
        raw = board.read_bytes()
    except OSError as exc:
        raise WorkError(f"docs/superpowers/plans/BOARD.md could not be read: {exc}") from exc
    text = raw.decode("utf-8", errors="replace")
    target = str(row)
    for line in re.split(r"\r\n|\r|\n", text):
        match = _LIVE_BOARD_ROW_LINE.match(line)
        if match is not None and match.group(1) == target:
            return True
    return False


def _definition(task: dict[str, Any], key: str) -> Any:
    """One definition field of a task. A task saved before lanes existed has no `lane` key; it
    was accepted on the default lane."""
    if key == "lane":
        return task.get("lane", DEFAULT_LANE)
    return task[key]


def accept(root: Path, plan_path: str, *, expected_sha256: str) -> dict[str, Any]:
    """Register exactly the plan bytes the user approved; preserve unchanged task progress."""
    if _project_board_authority(root) is not None:
        raise WorkError(
            "Project-authored BOARD authority is selected; do not create or replace WORK.json"
        )
    with _locked(root), _campaign_authority_locked(root):
        text, digest = _read_plan(root, plan_path)
        if digest != expected_sha256:
            raise WorkError("Plan changed since review; review it and accept its current SHA-256")
        tasks = parse_tasks(text)
        state = _execution_state(root)
        campaign = _campaign_view(root)
        retire_bound = (
            state
            and state["plan"]["sha256"] != digest
            and campaign.get("session_id")
            and campaign.get("status") != "phase_validated"
        )
        if retire_bound and not campaign.get("stopped"):
            raise WorkError("Finish the bound campaign before replacing its accepted plan")
        if state and state["plan"]["path"] != plan_path:
            if any(task["status"] != "completed" for task in state["tasks"]):
                raise WorkError("Finish the active plan before accepting a different plan")
            _record(state, "plan_finished", tasks=state["tasks"])
            old = {}
        else:
            old = {task["id"]: task for task in state["tasks"]} if state else {}
        # `lane` is part of a task's definition, not bookkeeping: re-accepting a plan that
        # changed a still-pending task's lane applies the change; changing it on a task that is
        # no longer pending is refused exactly like any other definition change below ("Do not
        # change started task ...; add a new task ID"), since the campaign may already be bound
        # to that task's old lane.
        definitions = ("title", "depends_on", "done_when", "lane")
        next_row = state.get("next_board_row", 1) if state else 1
        declared_row = parse_board_row(text)
        if _load_workv2(root) is not None:
            # This project has its own board (an imported v2 document already owns the primary
            # path); the Stop gate's board-parity rule can only pass work placed on it, keyed on
            # a real row number, never on Bear Hug's own counter.
            if declared_row is None:
                raise WorkError(
                    "This project has its own board; declare `Board row: N` naming an existing "
                    "row of docs/superpowers/plans/BOARD.md so the Stop gate's board-parity rule "
                    "can pass work placed on it"
                )
            if not _v2_import_row_exists(root, declared_row):
                raise WorkError(
                    f"Board row: {declared_row} names no row of "
                    "docs/superpowers/plans/BOARD.md; declare an existing row"
                )
        for task in tasks:
            previous = old.get(task["id"])
            if previous and all(
                _definition(task, key) == _definition(previous, key) for key in definitions
            ):
                task.update(previous)
            elif previous and previous["status"] != "pending":
                raise WorkError(f"Do not change started task {task['id']}; add a new task ID")
            if declared_row is not None:
                task["board_row"] = declared_row
            elif previous:
                task["board_row"] = previous["board_row"]
            else:
                task["board_row"] = next_row
                next_row += 1
        new_ids = {task["id"] for task in tasks}
        if any(task["status"] != "pending" and ident not in new_ids for ident, task in old.items()):
            raise WorkError("Do not remove started tasks from the accepted plan")
        if state and state["plan"]["path"] == plan_path and state["plan"]["sha256"] == digest:
            return state
        if retire_bound:
            # The controller verifies stopped custody and absence of accepted capsule work.
            # Keep this after task checks so an invalid replacement cannot detach the old run.
            retired = _campaign_call(root, "retire")
            if retired.get("status") != "retired":
                raise WorkError(retired.get("reason", "Bound campaign could not be retired"))
        state = state or {"schema_version": 1, "history": []}
        state.update(
            plan={
                "path": plan_path,
                "sha256": digest,
                "title": _title(text, plan_path),
                **({"board_row": declared_row} if declared_row is not None else {}),
            },
            tasks=tasks,
            next_board_row=next_row,
        )
        _record(state, "accepted", sha256=digest)
        _save(root, state)
        if (
            not retire_bound
            and campaign.get("session_id")
            and campaign.get("plan_sha256") != digest
        ):
            _write_json(_path(root, ".bearhug/campaign.json"), {"enabled": True})
    # Packets are compiled after the state lock is released, never inside it, and with the
    # Memex-only budget, never the default that also launches Graft and MemQ: the full compile
    # can run for seconds per task (minutes across a large plan), and the operator's own
    # `accept` call would otherwise wait on it. This Memex-only pass is fast (~1.5s for 20 tasks
    # against 400 records, measured) and leaves every packet current against the corpus and
    # marked partial; a caller that wants the full packet -- an explicit `bearhug-work start` or
    # `grounding` -- upgrades it on demand (`get_or_refresh` never treats a stored partial packet
    # as satisfying a full request). `compile_all` is best-effort and catches broadly: a
    # compilation fault (an unavailable tool, a malformed decision record the fuzz corpus didn't
    # anticipate, anything) is reported and never turns a successful `accept` into a failure --
    # the writes above have already landed by the time this runs, so nothing here can skip them.
    try:
        packets = _packets(root)
        packets.compile_all(
            root, plan_digest=digest, plan_text=text, tasks=tasks,
            consult=packets.HOOK_CONSULT, timeout_s=packets.HOOK_TIMEOUT_S, partial=True,
        )
    except Exception as exc:  # noqa: BLE001 - see the comment above
        print(f"Grounding packets were not compiled: {exc}", file=sys.stderr)
    return state


def _replace_board_cell(cell: str, value: str) -> str:
    leading = cell[: len(cell) - len(cell.lstrip())]
    trailing = cell[len(cell.rstrip()) :]
    escaped = re.sub(r"[\r\n\t]+", " ", value).replace("\\", "\\\\").replace("|", r"\|")
    return leading + escaped + trailing


def _write_board_cas(path: Path, before: bytes, after: bytes) -> None:
    """Update the opened BOARD inode only if its locked snapshot is still the path target."""

    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise WorkError("Project BOARD must be a singly linked regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        observed = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            observed.extend(chunk)
            if len(observed) > 32 * 1024 * 1024:
                raise WorkError("Project BOARD exceeds 32 MiB")
        current_path = path.stat(follow_symlinks=False)
        if bytes(observed) != before or (metadata.st_dev, metadata.st_ino) != (
            current_path.st_dev,
            current_path.st_ino,
        ):
            raise WorkError("Project BOARD changed before its locked transition")
        os.lseek(descriptor, 0, os.SEEK_SET)
        view = memoryview(after)
        written = 0
        while written < len(view):
            written += os.write(descriptor, view[written:])
        os.ftruncate(descriptor, len(after))
        os.fsync(descriptor)
        final_path = path.stat(follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != (final_path.st_dev, final_path.st_ino):
            # An external atomic editor won the path while we held the old inode. Its bytes remain
            # at the path and the durable transition journal forces explicit recovery.
            raise WorkError("Project BOARD path changed during its locked transition")
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _owner_matches(path: Path, expected: dict[str, Any] | None) -> bool:
    if expected is None:
        return not path.exists()
    try:
        return json.loads(path.read_text()) == expected
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _verify_transition_board(root: Path, authority: dict, journal: dict) -> None:
    """Keep the pending intent when an external writer replaces or changes its BOARD."""

    board = _path(root, authority["board_path"])
    before = board.stat(follow_symlinks=False)
    current = board.read_bytes()
    after = board.stat(follow_symlinks=False)
    expected = (journal["board_device"], journal["board_inode"])
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (before.st_dev, before.st_ino) != expected
        or (after.st_dev, after.st_ino) != expected
        or before.st_mtime_ns != after.st_mtime_ns
        or current != journal["after"].encode("utf-8")
    ):
        raise WorkError("Project BOARD diverged from its pending transition; intent preserved")


def _partial_board_write(current: bytes, before: bytes, after: bytes) -> bool:
    """Recognize only a possible prefix of this exact in-place write, not arbitrary edits."""

    if len(current) > len(before):
        return len(current) <= len(after) and current == after[:len(current)]
    if len(current) != len(before):
        return False
    prefix = 0
    while prefix < min(len(current), len(after)) and current[prefix] == after[prefix]:
        prefix += 1
    return current[prefix:] == before[prefix:]


def _retain_board_transition(root: Path, journal: dict) -> Path:
    """Retain create-only recovery intent across the unavoidable last-check/unlink interval."""
    raw = (json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    directory = _path(root, PROJECT_BOARD_RECEIPTS_PATH)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = _path(root, f"{PROJECT_BOARD_RECEIPTS_PATH}/{digest}.json")
    fd, temporary = tempfile.mkstemp(prefix=".transition-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            if target.is_symlink() or target.read_bytes() != raw:
                raise WorkError(
                    "Completed BOARD transition receipt diverged; intent retained"
                ) from exc
        Path(temporary).unlink()
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def _finish_board_transition(root: Path, authority: dict[str, Any]) -> dict[str, Any] | None:
    """Roll forward one durable BOARD/owner intent or fail on external divergence."""

    journal_path = _path(root, PROJECT_BOARD_TRANSITION_PATH)
    if not journal_path.exists():
        return
    try:
        journal = json.loads(journal_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkError("Project BOARD transition journal is unreadable") from exc
    required = {
        "schema_version",
        "record_kind",
        "board_path",
        "board_row",
        "before",
        "after",
        "before_sha256",
        "after_sha256",
        "board_device",
        "board_inode",
        "owner_before",
        "owner_after",
    }
    if (
        not isinstance(journal, dict)
        or set(journal) != required
        or journal.get("schema_version") != "1"
        or journal.get("record_kind") != "project_board_transition"
        or journal.get("board_path") != authority["board_path"]
        or journal.get("board_row") != authority["board_row"]
        or not isinstance(journal.get("before"), str)
        or not isinstance(journal.get("after"), str)
        or type(journal.get("board_device")) is not int
        or type(journal.get("board_inode")) is not int
        or not isinstance(journal.get("owner_after"), dict)
        or journal.get("owner_before") is not None
        and not isinstance(journal.get("owner_before"), dict)
    ):
        raise WorkError("Project BOARD transition journal is malformed")
    before = journal["before"].encode("utf-8")
    after = journal["after"].encode("utf-8")
    if (
        hashlib.sha256(before).hexdigest() != journal["before_sha256"]
        or hashlib.sha256(after).hexdigest() != journal["after_sha256"]
    ):
        raise WorkError("Project BOARD transition journal content is corrupt")
    board = _path(root, authority["board_path"])
    owner_path = _path(root, PROJECT_BOARD_OWNER_PATH)
    current = board.read_bytes()
    owner_before = journal["owner_before"]
    owner_after = journal["owner_after"]
    board_stat = board.stat(follow_symlinks=False)
    same_inode = (board_stat.st_dev, board_stat.st_ino) == (
        journal["board_device"],
        journal["board_inode"],
    )
    if not same_inode:
        raise WorkError("Project BOARD path diverged from its pending transition; intent preserved")
    if current == before and _owner_matches(owner_path, owner_before):
        _write_board_cas(board, before, after)
    elif (
        current != after and same_inode and _owner_matches(owner_path, owner_before)
        and _partial_board_write(current, before, after)
    ):
        # A crash may leave a prefix of our in-place write. The journal is durable first and the
        # inode is still the one it bound, so finishing that exact intent is deterministic.
        _write_board_cas(board, current, after)
    elif current != after:
        raise WorkError(
            "Project BOARD diverged from its pending transition; operator review needed"
        )
    _verify_transition_board(root, authority, journal)
    # Recheck both governed paths after CAS: an editor need not obey our inode lock.
    owner_path = _path(root, PROJECT_BOARD_OWNER_PATH)
    if _owner_matches(owner_path, owner_before):
        _write_json(owner_path, owner_after)
    elif not _owner_matches(owner_path, owner_after):
        raise WorkError("Project BOARD owner diverged from its pending transition")
    verified = _project_board_state(root, authority)
    expected_status = "completed" if owner_after.get("state") == "completed" else "in_progress"
    if verified["tasks"][0]["status"] != expected_status:
        raise WorkError("Project BOARD transition could not be verified through its own parser")
    _verify_transition_board(root, authority, journal)
    if not _owner_matches(_path(root, PROJECT_BOARD_OWNER_PATH), owner_after):
        raise WorkError("Project BOARD owner diverged from its pending transition")
    if _path(root, PROJECT_BOARD_TRANSITION_PATH) != journal_path:
        raise WorkError("Project BOARD transition journal path changed")
    if json.loads(journal_path.read_text()) != journal:
        raise WorkError("Project BOARD transition journal changed; intent preserved")
    receipt = _retain_board_transition(root, journal)
    _verify_transition_board(root, authority, journal)
    journal_path.unlink()
    directory = os.open(journal_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    try:
        _verify_transition_board(root, authority, journal)
        if not _owner_matches(_path(root, PROJECT_BOARD_OWNER_PATH), owner_after):
            raise WorkError("Project BOARD owner diverged after publication")
    except WorkError as exc:
        raise WorkError(f"{exc}; recovery intent retained in {receipt.relative_to(root)}") from exc
    return verified


def _project_board_transition(
    root: Path,
    authority: dict[str, Any],
    task_id: str,
    action: str,
    *,
    session_id: str,
    provider: str,
    evidence: str,
) -> dict[str, Any]:
    _finish_board_transition(root, authority)
    expected_id = f"board-{authority['board_row']}"
    if task_id != expected_id:
        raise WorkError(f"Unknown project BOARD task: {task_id}")
    state = _project_board_state(root, authority)
    task = state["tasks"][0]
    if state["current_authority_sha256"] != authority["authority_sha256"] and action != "block":
        raise WorkError("Project authority plan changed; review and rerun setup before continuing")
    owner = _project_board_owner(root, authority["board_row"])
    owns = owner["session_id"] == session_id and owner["provider"] == provider
    if action == "start":
        if task["status"] == "completed":
            raise WorkError("Executed project BOARD rows cannot be restarted")
        if task["status"] == "blocked" and not owns:
            raise WorkError(
                "Project BOARD row is blocked; resolve its dependency or ambiguity first"
            )
        if task["status"] == "in_progress" and owner["state"] == "active":
            if owns:
                return state
            raise WorkError("Project BOARD row is already owned by another session")
        if task["status"] == "in_progress" and owner["state"] != "interrupted":
            raise WorkError("Partial project BOARD row has no recoverable session binding")
        execution = "partial"
        notes = task["evidence"]
        next_owner_state = "active"
    else:
        if task["status"] not in {"in_progress", "blocked"} or not owns:
            raise WorkError("Only the session owning the selected BOARD row may update it")
        execution = "executed" if action == "complete" else "partial"
        label = "completed" if action == "complete" else "blocked"
        notes = f"{task['evidence']}; {label}: {evidence}".strip("; ")
        next_owner_state = "completed" if action == "complete" else "interrupted"

    board = _path(root, authority["board_path"])
    before = board.read_bytes()
    try:
        text = before.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkError("Project BOARD must be UTF-8 for a checked transition") from exc
    row_pattern = re.compile(rf"^\|\s*{re.escape(authority['board_row'])}\s*\|(.*)$", re.M)
    matches = list(row_pattern.finditer(text))
    if len(matches) != 1:
        raise WorkError("Selected project BOARD row is missing or ambiguous during transition")
    body = matches[0].group(1)
    boundaries = [
        index
        for index, char in enumerate(body)
        if char == "|" and (index == 0 or body[index - 1] != "\\")
    ]
    if len(boundaries) != 6:
        raise WorkError("Selected project BOARD row no longer has the governed seven-cell shape")
    starts = [0] + [position + 1 for position in boundaries[:-1]]
    ends = boundaries
    cells = [body[start:end] for start, end in zip(starts, ends, strict=True)]
    if cells[1].strip() != "accepted":
        raise WorkError("Only an accepted project BOARD row may transition")
    cells[2] = _replace_board_cell(cells[2], execution)
    cells[3] = _replace_board_cell(cells[3], notes)
    pieces: list[str] = []
    for cell, boundary in zip(cells, boundaries, strict=True):
        pieces.append(cell)
        pieces.append(body[boundary])
    replacement = "".join(pieces)
    after_text = text[: matches[0].start(1)] + replacement + text[matches[0].end(1) :]
    after = after_text.encode("utf-8")
    if board.read_bytes() != before:
        raise WorkError("Project BOARD changed before the checked transition could commit")
    owner_path = _path(root, PROJECT_BOARD_OWNER_PATH)
    owner_before = None if not owner_path.exists() else json.loads(owner_path.read_text())
    owner_after = {
        "board_row": authority["board_row"],
        "session_id": session_id,
        "provider": provider,
        "state": next_owner_state,
        "updated_at": _now(),
    }
    journal_path = _path(root, PROJECT_BOARD_TRANSITION_PATH)
    board_stat = board.stat(follow_symlinks=False)
    _write_json(
        journal_path,
        {
            "schema_version": "1",
            "record_kind": "project_board_transition",
            "board_path": authority["board_path"],
            "board_row": authority["board_row"],
            "before": text,
            "after": after_text,
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "board_device": board_stat.st_dev,
            "board_inode": board_stat.st_ino,
            "owner_before": owner_before,
            "owner_after": owner_after,
        },
    )
    if board.read_bytes() != before:
        raise WorkError("Project BOARD changed during the checked transition")
    _write_board_cas(board, before, after)
    # Return the snapshot verified while the intent still existed. A later independent editor
    # must not turn an already committed transition into an unjournaled validation failure.
    verified = _finish_board_transition(root, authority)
    assert verified is not None
    return verified


def transition(
    root: Path,
    task_id: str,
    action: str,
    *,
    session_id: str,
    provider: str,
    evidence: str = "",
    include_campaign: bool = True,
    packet_consult: Any = None,
    packet_timeout_s: float | None = None,
    packet_partial: bool = False,
) -> dict[str, Any]:
    if not session_id.strip() or provider not in {"codex", "claude"}:
        raise WorkError("Supply the exact session ID and provider")
    if action not in {"start", "complete", "block"}:
        raise WorkError("Unknown task operation")
    if action in {"complete", "block"} and not evidence.strip():
        raise WorkError("Supply --evidence with the check results or blocking reason")
    refresh_after: str | None = None
    campaign_dispatched = False
    state: dict[str, Any] | None = None
    with _locked(root):
        authority = _project_board_authority(root)
        if authority is not None:
            return _project_board_transition(
                root,
                authority,
                task_id,
                action,
                session_id=session_id,
                provider=provider,
                evidence=evidence.strip(),
            )
        # An interactive-lane task is never campaign-owned: bearhug-work start/complete/block
        # work for it exactly as if campaigns were off, whatever the project's adoption record
        # says, so a project can keep the campaign lane for the rest of the plan. The lane is
        # read from the raw board, not `_execution_state`, so the campaign bridge is never
        # queried at all for an interactive-lane transition -- not even for status.
        raw_state = _load(root)
        interactive = (
            raw_state is not None
            and task_id in {row["id"] for row in raw_state["tasks"]}
            and next(row for row in raw_state["tasks"] if row["id"] == task_id).get(
                "lane", DEFAULT_LANE
            )
            == "interactive"
        )
        if not interactive and not include_campaign and _campaign_enabled(root):
            raise WorkError("Campaign task changes require the explicit campaign controller")
        state = (
            _execution_state(root, campaign={})
            if (interactive or not include_campaign)
            else _execution_state(root)
        )
        if state is None:
            raise WorkError("Accept a plan before starting tasks")
        if (
            action != "block"
            and _read_plan(root, state["plan"]["path"])[1] != state["plan"]["sha256"]
        ):
            raise WorkError("Accepted plan changed; review and accept it again before continuing")
        by_id = {task["id"]: task for task in state["tasks"]}
        if task_id not in by_id:
            raise WorkError(f"Unknown task: {task_id}")
        task = by_id[task_id]
        campaign = _campaign_view(root) if (include_campaign and not interactive) else {}
        if not interactive and campaign.get("session_id") and action != "start":
            raise WorkError("Campaign controller owns task results; use campaign controls.")
        # Interactive work happens before the campaign, never alongside it: while a campaign is
        # bound and live, its leases need a clean tracked tree, and interactive work (screenshots,
        # a running app, board edits) dirties exactly the files bearhug-work writes. Only queried
        # for an interactive task, so a campaign-lane transition never pays for this call.
        campaign_live = _campaign_view(root) if interactive else {}
        owner = task["session_id"] == session_id and task["provider"] == provider
        if action == "start":
            if task["status"] == "completed":
                raise WorkError("Completed tasks cannot be restarted; add a follow-up task")
            if task["status"] == "in_progress":
                if owner:
                    return state
                raise WorkError("Task is already owned by another session")
            if interactive and campaign_live.get("session_id"):
                raise WorkError(
                    "A campaign is bound and live; interactive-lane work runs before the "
                    "campaign starts. Finish or stop the campaign before starting this task."
                )
            if any(by_id[dep]["status"] != "completed" for dep in task["depends_on"]):
                raise WorkError("Complete this task's dependencies first")
            if any(
                t["status"] == "in_progress"
                and t["session_id"] == session_id
                and t["provider"] == provider
                for t in state["tasks"]
            ):
                raise WorkError("This session already owns an active task")
            if include_campaign and not interactive and _campaign_enabled(root):
                reply = _campaign_call(
                    root,
                    "dispatch",
                    "--task",
                    task_id,
                    "--session",
                    session_id,
                    "--provider",
                    provider,
                )
                if not reply.get("locator") or reply.get("status") in {"blocked", "failed"}:
                    raise WorkError(reply.get("reason", "Campaign execution is unavailable"))
                refresh_after = task_id
                campaign_dispatched = True
            else:
                task.update(
                    status="in_progress", session_id=session_id, provider=provider, evidence=""
                )
                refresh_after = task_id
        else:
            if not owner or task["status"] != "in_progress":
                raise WorkError("Only the session owning an active task can complete or block it")
            task.update(
                status="completed" if action == "complete" else "blocked", evidence=evidence.strip()
            )
            # `complete`/`block` on an interactive task already in progress stay allowed even
            # while a campaign is bound and live, so an interrupted or long-running interactive
            # task is never stranded. The safer choice over silently refusing outright: warn
            # instead, since the alternative -- a cryptic clean-base failure deep inside the
            # campaign's next lease -- is exactly what this names up front.
            if interactive and campaign_live.get("session_id"):
                print(
                    "Note: a campaign is bound and live; commit the board changes from this "
                    "interactive task before the campaign's next capsule lease runs, or that "
                    "lease will fail its clean-base check.",
                    file=sys.stderr,
                )
        if not campaign_dispatched:
            task["updated_at"] = _now()
            _record(
                state,
                action,
                task_id=task_id,
                session_id=session_id,
                provider=provider,
                evidence=evidence.strip(),
                evidence_kind="reported",
            )
            _save(root, state)
    # The packet refresh (a full compile: Memex, architecture, Graft and MemQ, unless a caller
    # asked for the hook's reduced budget) runs after the lock above is released, never inside
    # it: it can take real time, and holding the project lock that long would block every other
    # bearhug-work call, including a concurrent session's hook, behind this one's own compile.
    if refresh_after is not None:
        _refresh_task_packet(
            root, state, refresh_after,
            consult=packet_consult, timeout_s=packet_timeout_s, partial=packet_partial,
        )
    return _execution_state(root) if campaign_dispatched else state


def observe(
    root: Path,
    event: str,
    session_id: str,
    *,
    provider: str = "codex",
    include_campaign: bool = True,
) -> None:
    """A native interruption pauses owned work; ordinary Stop never completes a task."""
    if event not in {"Interrupt", "SessionEnd"}:
        return
    with _locked(root):
        authority = _project_board_authority(root)
        if authority is not None:
            _finish_board_transition(root, authority)
            owner = _project_board_owner(root, authority["board_row"])
            if (
                owner["session_id"] == session_id
                and owner["provider"] == provider
                and owner["state"] == "active"
            ):
                _write_json(
                    _path(root, PROJECT_BOARD_OWNER_PATH),
                    {**owner, "state": "interrupted", "updated_at": _now()},
                )
            return
        if not _managed_state_path(root).exists():
            return
        state = _load(root)
        if state is None:
            return
        campaign_bound = (
            _campaign_view(root).get("session_id") if include_campaign else _campaign_enabled(root)
        )
        changed = False
        for task in state["tasks"]:
            # An interactive-lane task is never campaign-owned, so its own interruption must
            # still be handled while a campaign is bound -- otherwise its session ending leaves
            # it `in_progress` forever, owned by a session that no longer exists (nothing else
            # can ever mark it interrupted for it).
            if campaign_bound and task.get("lane", DEFAULT_LANE) != "interactive":
                continue
            if (
                task["session_id"] == session_id
                and task["provider"] == provider
                and task["status"] == "in_progress"
            ):
                task.update(
                    status="blocked",
                    evidence=f"{provider} {event}; explicit restart required",
                    updated_at=_now(),
                )
                _record(
                    state,
                    "interrupted",
                    task_id=task["id"],
                    session_id=session_id,
                    provider=provider,
                    evidence=task["evidence"],
                )
                changed = True
        if changed:
            _save(root, state)


def _sessions(root: Path) -> list[dict[str, str]]:
    """Project-local native observations; bounded tails, never provider transcript inference."""
    directory = _path(root, ".bearhug/codex-hooks/v1")
    sessions: dict[str, dict[str, str]] = {}
    for path in sorted(directory.glob("*/events.jsonl"))[-7:]:
        path = _path(root, path.relative_to(root).as_posix())
        with path.open("rb") as stream:
            size = path.stat().st_size
            stream.seek(max(0, size - 256 * 1024))
            if stream.tell():
                stream.readline()
            for line in stream:
                try:
                    record = json.loads(line)
                    if record.get("record_kind") != "codex_hook_observation":
                        continue
                    ident = record["session_id"]
                    entry = {
                        "session_id": ident,
                        "provider": "codex",
                        "last_event": record["hook_event_name"],
                        "observed_at": record["observed_at"],
                    }
                    if (
                        ident not in sessions
                        or entry["observed_at"] > sessions[ident]["observed_at"]
                    ):
                        sessions[ident] = entry
                except (ValueError, KeyError, TypeError):
                    continue
    return sorted(sessions.values(), key=lambda row: row["observed_at"], reverse=True)[:20]


def status(root: Path, *, include_campaign: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "unavailable",
        "reason": "",
        "plan": None,
        "tasks": [],
        "history": [],
        "sessions": [],
        "discovered_plans": [],
        "native_sync": [],
        "updated_at": "",
        "architecture": _architecture_status(root),
        "campaign": (
            _campaign_view(root)
            if include_campaign
            else {"status": "unavailable", "reason": "campaign execution not requested"}
        ),
    }
    v2 = _load_workv2(root)
    if v2 is not None:
        _workv2_status(v2, result)
        if not _managed_state_path(root).exists():
            # No plan accepted on top of the imported board: report the v2 board alone, exactly
            # as before.
            return result
        # A plan was accepted on top of the imported board (the fallback path exists); fall
        # through so the ordinary managed logic below runs against that fallback file — already
        # true of _execution_state/_load because both now resolve through _managed_state_path —
        # and merges plan/tasks/history/native_sync/sessions into this same result, which still
        # carries the v2 schema/reason set just above.
    try:
        authority = _project_board_authority(root)
        if authority is not None:
            with _locked(root):
                _finish_board_transition(root, authority)
                state = _project_board_state(root, authority)
        else:
            state = _execution_state(root, campaign=result["campaign"])
        if state:
            result.update(state)
            result["status"] = "active"
            plan = result["plan"]
            try:
                _text, current = _read_plan(root, plan["path"])
            except OSError:
                current = ""
            plan.update(current_sha256=current, changed=current != plan["sha256"])
            if plan["changed"]:
                result["reason"] = (
                    "Accepted plan changed or disappeared; review and accept before continuing"
                )
        if v2 is None:
            # A v2 import already accounts for the project's historical plans. Scanning the
            # plans directory again here would read every top-level *.md file on every hook and
            # every dashboard refresh (status() runs on every context()/native_hook call), and
            # one oversized, symlinked or non-UTF-8 file among them would turn the whole merged
            # status into "error" with no active front. The design's merge list names plan/
            # tasks/history/native_sync/sessions, never discovered_plans.
            directory = _path(root, PLAN_DIR)
            for path in sorted(directory.glob("*.md")):
                if path.name in {"BOARD.md", "LEDGER.md"}:
                    continue
                relative = path.relative_to(root).as_posix()
                if state and relative == state["plan"]["path"]:
                    continue
                text, digest = _read_plan(root, relative)
                result["discovered_plans"].append(
                    {"path": relative, "title": _title(text, path.stem), "sha256": digest}
                )
        result["sessions"] = _sessions(root)
        if state:
            result["native_sync"] = _native().views(root, state)
            for sync in result["native_sync"]:
                if not any(
                    row["session_id"] == sync["session_id"] and row["provider"] == sync["provider"]
                    for row in result["sessions"]
                ):
                    result["sessions"].append(
                        {
                            key: sync[key]
                            for key in ("session_id", "provider", "last_event", "observed_at")
                        }
                    )
        if not state:
            result["status"] = "draft" if result["discovered_plans"] else "unavailable"
            result["reason"] = (
                "No accepted plan; draft a task table, review it, then accept the exact plan"
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result.update(status="error", reason=str(exc), tasks=[])
    return result


def task_summary(view: dict[str, Any]) -> str:
    """Bounded, readable board projection that does not depend on a provider's widget."""
    tasks = view.get("tasks", [])
    if not tasks:
        return ""
    done = {task["id"] for task in tasks if task["status"] == "completed"}
    lines = [f"Project tasks ({len(done)}/{len(tasks)} completed):"]
    for task in tasks[:10]:
        waiting = [dep for dep in task["depends_on"] if dep not in done]
        label = task["status"]
        if waiting and label == "pending":
            label = "waiting for " + ", ".join(waiting)
        title = " ".join(task["title"].split())
        if len(title) > 160:
            title = title[:157] + "..."
        lines.append(f"- {task['id']} [{label}] {title}")
    if len(tasks) > 10:
        lines.append(f"{len(tasks) - 10} more tasks are on the HTML board.")
    return "\n".join(lines)


def context(root: Path, session_id: str, provider: str = "codex") -> str:
    view = status(root)
    prefix = f"Bear Hug session: {session_id}. Project work: {view['status']}."
    if view["status"] == "error":
        return prefix + " " + view["reason"]
    imported_v2 = view.get("schema") == _WORKV2_SCHEMA
    if imported_v2 and not _managed_state_path(root).exists():
        # No plan accepted on top of the imported board (no fallback file): report it alone,
        # exactly as before.
        return (
            prefix
            + " "
            + view["reason"]
            + " This is an imported managed board (docs/superpowers/plans/WORK.json). "
            "Read readiness and context pointers from it; the board's own docs stay authoritative."
        )
    # A plan was accepted on top of the imported board (the fallback path exists): produce
    # exactly the managed context an ordinary v1 project gets for that plan below — status()
    # already merged its plan/tasks into `view` — then name the read-only v2 board once, at the
    # end of whichever message below applies, instead of replacing it with the short v2-only
    # message. For an ordinary v1 project `imported_v2` is False and `note` is empty, so every
    # return below is unchanged.
    note = (
        " An imported board is also present, read-only, at docs/superpowers/plans/WORK.json."
        if imported_v2
        else ""
    )
    if not view["plan"]:
        drafts = ", ".join(row["path"] for row in view["discovered_plans"][:3]) or "none yet"
        return (
            prefix
            + f" Draft plans: {drafts}. "
            + (
                "When asked to plan, use docs/superpowers/plans/ and one table: "
                "ID | Task | Depends on | Done when. Only after the user accepts the plan, run "
                "scripts/bin/bearhug-work status --json, then use "
                "scripts/bin/bearhug-work accept PATH --sha256 SHA from that output. "
                "Do not accept a plan merely because you wrote it."
            )
            + note
        )
    plan = view["plan"]
    if plan["changed"]:
        return prefix + f" {plan['path']}: {view['reason']}." + note
    prefix += (
        "\n"
        + task_summary(view)
        + "\nInclude this task summary in your progress reply even when execution is blocked. "
    )
    campaign = view["campaign"]
    if campaign.get("status") not in {"unavailable", "opted_out"}:
        if campaign.get("status") == "needs_configuration":
            onboarding = campaign.get("onboarding", {})
            return (
                prefix
                + f" Plan: {plan['path']}. "
                + str(campaign.get("reason") or "")
                + " "
                + str(onboarding.get("next_action") or "")
                + " Keep native task data synchronized while execution is unavailable."
                + note
            )
        return (
            prefix
            + f" Plan: {plan['path']}. Campaign: {campaign['status']}. "
            + str(campaign.get("reason") or "")
            + (
                " The controller owns execution and completion; mirror its board states. "
                "Use scripts/bin/bearhug-campaign status or control continue/stop."
                if campaign.get("session_id")
                else f" To execute the accepted plan, use scripts/bin/bearhug-work start ID "
                f"--session {session_id} --provider {provider} for a dependency-ready task. "
                "This starts the existing campaign worker; "
                "do not implement the same tasks separately."
            )
            + note
        )
    mine = [
        task["id"]
        for task in view["tasks"]
        if task["session_id"] == session_id
        and task["provider"] == provider
        and task["status"] == "in_progress"
    ]
    completed = {task["id"] for task in view["tasks"] if task["status"] == "completed"}
    ready = [
        task["id"]
        for task in view["tasks"]
        if task["status"] == "pending" and all(dep in completed for dep in task["depends_on"])
    ]
    authority_instruction = (
        "The selected project-authored BOARD row is authoritative; do not create WORK.json "
        "or edit another row. "
        if view.get("authority_mode") == "project_board"
        else (
            # The accepted plan's own authority file is the fallback, not the v2 import it sits
            # alongside; name it so a session does not look for its task states in WORK.json.
            "docs/superpowers/plans/WORK.managed.json is authoritative; do not edit its task "
            "states by hand. "
            if imported_v2
            else "WORK.json is authoritative; do not edit its task states by hand. "
        )
    )
    return (
        prefix
        + f" Plan: {plan['path']}. Your active tasks: {mine}. Ready: {ready[:10]}. "
        + (
            f"Use scripts/bin/bearhug-work start ID --session {session_id} "
            f"--provider {provider} before work; use the same helper's complete command "
            f"with --session {session_id} --provider {provider} --evidence 'check results' "
            "after checks, or block with a reason. "
            + authority_instruction
            + "A stopped turn is not completed work."
        )
        + note
    )


def board(view: dict[str, Any]) -> str:
    """Legacy TSV projection, including truthful draft/error rows."""
    lines = ["== PLAN =="]

    def row(kind: str, ident: str, state: str, title: str, extra: str = "") -> str:
        return "\t".join(
            re.sub(r"[\t\r\n]+", " ", str(value)) for value in (kind, ident, state, title, extra)
        )

    plan = view.get("plan")
    if plan:
        lines.append(
            row(
                "plan",
                plan["path"],
                "changed" if plan.get("changed") else "accepted",
                plan["title"],
                view["reason"],
            )
        )
    for draft in view["discovered_plans"]:
        lines.append(row("plan", draft["path"], "draft", draft["title"], "Awaiting acceptance"))
    if view["reason"] and not plan:
        lines.append(row("status", "—", view["status"], view["reason"]))
    lines.append("== TASKS ==")
    for task in view["tasks"]:
        lane = task.get("lane", DEFAULT_LANE)
        lines.append(
            row(
                "task",
                task["id"],
                task["status"],
                task["title"],
                f"lane: {lane}; depends: {','.join(task['depends_on']) or 'none'}; "
                f"session: {task['session_id'] or 'unassigned'}",
            )
        )
    lines.append("== STATE ==")
    for event in view["history"][-30:]:
        lines.append(
            row(
                "event",
                event.get("task_id", "—"),
                event["action"],
                event["at"],
                event.get("evidence", event["plan_path"]),
            )
        )
    return "\n".join(lines)


def _native():
    if __package__:
        from bearhug import project_native
    else:
        import bearhug_native as project_native
    return project_native


def _campaign_enabled(root: Path) -> bool:
    helper = _path(root, "scripts/bin/bearhug-campaign")
    if not helper.is_file():
        return False
    try:
        adoption = _adoption(root)
    except WorkError:
        return False
    if adoption is not None and adoption["campaigns"] == "off":
        return False
    path = _path(root, ".bearhug/campaign.json")
    return not path.is_file() or json.loads(path.read_text()).get("enabled") is not False


def _campaign_call(root: Path, *arguments: str) -> dict[str, Any]:
    helper = _path(root, "scripts/bin/bearhug-campaign")
    try:
        result = subprocess.run(
            [str(helper), *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20 if arguments[0] in {"dispatch", "onboard"} else 5,
        )
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict):
            raise ValueError("campaign response is not an object")
        return reply
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"status": "blocked", "reason": f"Campaign bridge unavailable: {exc}"}


def _campaign_view(root: Path) -> dict[str, Any]:
    if not _path(root, "scripts/bin/bearhug-campaign").is_file():
        return {"status": "unavailable", "reason": "Campaign bridge is not installed."}
    return _campaign_call(root, "status")


def _execution_state(root: Path, *, campaign: dict | None = None) -> dict | None:
    authority = _project_board_authority(root)
    if authority is not None:
        return _project_board_state(root, authority)
    state = _load(root)
    if state is None:
        return None
    observed = campaign if campaign is not None else _campaign_view(root)
    if observed.get("plan_sha256") == state["plan"]["sha256"]:
        if observed.get("locator"):
            state["execution_authority"] = "campaign"
        for task in state["tasks"]:
            task.update(observed.get("task_states", {}).get(task["id"], {}))
    return state


def _architecture(root: Path):
    if not __package__:
        library = str(_path(root, ".bearhug/lib"))
        if library not in sys.path:
            sys.path.insert(0, library)
    from bearhug import project_architecture

    return project_architecture


def _architecture_status(root: Path) -> dict[str, Any]:
    try:
        return _architecture(root).status(root)
    except (ImportError, OSError, ValueError) as exc:
        return {"status": "unavailable", "reason": f"Architecture integration unavailable: {exc}"}


def _packets(root: Path):
    if not __package__:
        library = str(_path(root, ".bearhug/lib"))
        if library not in sys.path:
            sys.path.insert(0, library)
    from bearhug.campaign import packets

    return packets


def _discard_superseded_packet(root: Path, plan_digest: str) -> None:
    # A hook can read state, then lose a race with a concurrent `accept` that replaces the plan
    # before the hook's own packet write lands: the write would otherwise recreate a stale
    # plan-digest directory just after `compile_all`'s `rmtree` removed it, and it would then
    # linger forever. Cheap post-write check, not a lock (a hook must never block on one held by
    # `accept`'s own compile): if the live accepted plan has since moved on, discard what was
    # just written under the old digest.
    try:
        live = _load(root)
    except WorkError:
        return
    if live is not None and live["plan"]["sha256"] != plan_digest:
        with contextlib.suppress(OSError):
            _packets(root).discard(root, plan_digest)


def _task_packet(
    root: Path,
    state: dict[str, Any],
    task_id: str,
    *,
    consult: Any = None,
    timeout_s: float | None = None,
    partial: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Return task_id's current grounding packet, compiling it if missing or stale.

    ``consult``/``timeout_s``/``partial`` only take effect when a compile actually happens (a
    cached, still-current packet is returned as is): pass ``consult=packets.HOOK_CONSULT`` and
    ``partial=True`` for a hook-time recompile, which must never launch Graft or MemQ against the
    hook timeout; leave them unset for `bearhug-work start` and the `grounding` CLI operation,
    which do the full compile. ``force=True`` recompiles unconditionally (`--refresh`).
    """

    by_id = {task["id"]: task for task in state["tasks"]}
    if task_id not in by_id:
        raise WorkError(f"Unknown task: {task_id}")
    plan_text, _digest = _read_plan(root, state["plan"]["path"])
    plan_digest = state["plan"]["sha256"]
    kwargs: dict[str, Any] = {"partial": partial}
    if consult is not None:
        kwargs["consult"] = consult
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s
    packets = _packets(root)
    if force:
        packet = packets.write_packet(
            root, plan_digest=plan_digest, plan_text=plan_text, task=by_id[task_id], **kwargs
        )
    else:
        packet = packets.get_or_refresh(
            root, plan_digest=plan_digest, plan_text=plan_text, task=by_id[task_id], **kwargs
        )
    _discard_superseded_packet(root, plan_digest)
    return packet


def _refresh_task_packet(
    root: Path,
    state: dict[str, Any],
    task_id: str,
    *,
    consult: Any = None,
    timeout_s: float | None = None,
    partial: bool = False,
) -> None:
    # Best-effort: a grounding packet is guidance, never a gate. Any failure -- an unreadable
    # plan, an unavailable tool, a corrupt packet file -- is reported to stderr and never blocks
    # the task transition it rides along with.
    try:
        _task_packet(root, state, task_id, consult=consult, timeout_s=timeout_s, partial=partial)
    except Exception as exc:  # noqa: BLE001 - see the guard's own docstring above
        print(f"Grounding packet refresh skipped for {task_id}: {exc}", file=sys.stderr)


def _packet_injection_path(root: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:128] or "session"
    return _path(root, f".bearhug/packet-injections/{safe}.json")


def _packet_unavailable_note_path(root: Path, session_id: str) -> Path:
    # Deliberately a different directory than `_packet_injection_path`: whatever made the
    # injection path unusable (a blocked directory, a permissions problem) should not also
    # prevent recording that the "unavailable" note was already shown once this session.
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:128] or "session"
    return _path(root, f".bearhug/packet-unavailable/{safe}.flag")


def _packet_fingerprint(task_id: str, text: str) -> str:
    # A pure function of the task id and the packet's own rendered text, kept separate from its
    # one caller so the "the active task changed" half of it is directly testable: two tasks with
    # (hypothetically) identical packet text must still fingerprint differently.
    return f"{task_id}:{hashlib.sha256(text.encode()).hexdigest()}"


_PACKET_INJECTION_MAX_AGE_S = 7 * 24 * 3600


def _prune_stale_injections(root: Path) -> None:
    directory = _path(root, ".bearhug/packet-injections")
    if not directory.is_dir():
        return
    threshold = time.time() - _PACKET_INJECTION_MAX_AGE_S
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < threshold:
                entry.unlink()
        except OSError:
            continue


def _packet_injection_text(
    root: Path, session_id: str, provider: str, *, event: str, source: str = ""
) -> str:
    """The ACTIVE task's packet text, but only once per session and again only when it changes.

    "Active" means a task this exact session/provider owns as ``in_progress``. Nothing is
    returned (and no per-session state is touched) when there is no accepted plan or no active
    task. The whole read/compile/write path is wrapped: any failure degrades to a one-line
    "Grounding packet unavailable" note, emitted once per session rather than repeated (or
    silently dropped) on every later prompt.

    A `SessionStart` whose `source` is `compact`, `resume` or `clear` clears this session's
    stored injection fingerprint first: after any of those the model's context no longer holds
    whatever was injected earlier in the session (compaction summarizes it away; resume and clear
    start a fresh context), so the active task's packet, if any, is re-injected.
    """

    injection_path = _packet_injection_path(root, session_id)
    unavailable_path = _packet_unavailable_note_path(root, session_id)
    try:
        if event == "SessionStart" and source in {"compact", "resume", "clear"}:
            injection_path.unlink(missing_ok=True)
        state = _execution_state(root)
        if not state:
            return ""
        active = next(
            (
                task
                for task in state["tasks"]
                if task["status"] == "in_progress"
                and task["session_id"] == session_id
                and task["provider"] == provider
            ),
            None,
        )
        if active is None:
            return ""
        packets = _packets(root)
        with _locked(root):
            packet = _task_packet(
                root,
                state,
                active["id"],
                consult=packets.HOOK_CONSULT,
                timeout_s=packets.HOOK_TIMEOUT_S,
                partial=True,
            )
        text = packet.get("text", "")
        fingerprint = _packet_fingerprint(active["id"], text)
        previous = None
        if injection_path.is_file():
            previous = json.loads(injection_path.read_text(encoding="utf-8")).get("fingerprint")
        if previous == fingerprint:
            return ""
        injection_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(injection_path, {"fingerprint": fingerprint, "task_id": active["id"]})
        unavailable_path.unlink(missing_ok=True)
        _prune_stale_injections(root)
        return "\n" + text
    except Exception as exc:  # noqa: BLE001 - a packet is guidance; it must never fail the hook
        if unavailable_path.is_file():
            return ""
        try:
            unavailable_path.parent.mkdir(parents=True, exist_ok=True)
            unavailable_path.write_text(str(exc), encoding="utf-8")
        except OSError:
            pass
        return f"\nGrounding packet unavailable: {exc}"


def _refresh_architecture(root: Path) -> None:
    # Invoked by the installed lifecycle, never by the dashboard's status reader.
    try:
        _architecture(root).refresh(root)
    except (ImportError, OSError, ValueError) as exc:
        print(f"Bear Hug architecture refresh unavailable: {exc}", file=sys.stderr)


def _telemetry_runtime():
    """The runtime's telemetry construction and store modules, or (None, None) if unavailable.

    Mirrors the lazy, dev-checkout-tolerant import `project_hook_runtime.py` already uses for
    `bearhug_runtime`: an installed project vendors the runtime on `sys.path` already, while a
    source checkout of this repository needs `runtime/` added by hand. Never raises — a missing
    runtime here means telemetry is skipped, not that the hook fails.
    """
    try:
        from bearhug_runtime import telemetry, telemetry_store

        return telemetry, telemetry_store
    except ImportError:
        pass
    development_runtime = Path(__file__).resolve().parents[2] / "runtime"
    if not development_runtime.is_dir():
        return None, None
    if str(development_runtime) not in sys.path:
        sys.path.insert(0, str(development_runtime))
    try:
        from bearhug_runtime import telemetry, telemetry_store

        return telemetry, telemetry_store
    except ImportError:
        return None, None


def _record_emitter_observation(
    root: Path,
    *,
    session_id: str,
    event_name: str,
    emitter_id: str,
    observation: dict[str, Any],
) -> None:
    """Append one best-effort `emitter_observation` telemetry record.

    Telemetry is an observer here exactly as it is for the Stop coordinator (see
    `runtime/bearhug_runtime/telemetry.py`): it must never affect what the hook returns, never
    slow it down noticeably, and never carry the injected text itself — only shape (which sources
    contributed, how many bytes, whether anything was injected at all). Any failure — a missing
    runtime, a redaction failure, a store append failure — is swallowed rather than surfaced.
    """
    try:
        telemetry, telemetry_store = _telemetry_runtime()
        if telemetry is None or telemetry_store is None:
            return
        raw_input = json.dumps(
            {"session_id": session_id, "event_name": event_name, "emitter_id": emitter_id},
            sort_keys=True,
        ).encode("utf-8")
        record = telemetry.build_emitter_record(
            emitter_id=emitter_id,
            observation=observation,
            event_id=uuid.uuid4().hex,
            session_id=session_id,
            event_name=event_name,
            raw_input=raw_input,
        )
        if record is None:
            return
        telemetry_store.append(record, root=telemetry_store.default_root(root))
    except Exception:  # noqa: BLE001 - telemetry may never affect or slow the hook's own result
        return


def _record_context_injection(
    root: Path, *, session_id: str, event_name: str, text: str, sources: list[str]
) -> None:
    """Record one SessionStart/UserPromptSubmit context injection, never its text."""
    _record_emitter_observation(
        root,
        session_id=session_id,
        event_name=event_name,
        emitter_id="native-hook-context",
        observation={
            "sources": sources,
            "byte_count": len(text.encode("utf-8")),
            "injected": bool(text.strip()),
        },
    )


def native_hook(root: Path, payload: dict[str, Any], provider: str) -> str:
    cwd = Path(payload["cwd"]).resolve(strict=True)
    if cwd != root and root not in cwd.parents:
        raise WorkError("Hook belongs to another project")
    session_id = payload["session_id"]
    if _load_workv2(root) is not None and not _managed_state_path(root).exists():
        # An imported v2 board with no plan accepted on top of it (no fallback file) is read by
        # status() and the cockpit, not the v1 native reconciler. Running v1 reconciliation here
        # raises "Unsupported project-work state"; returned from a Stop hook it blocks the turn
        # and re-fires every stop — a loop. Skip it: refresh architecture and inject the v2 board
        # context where v1 would, and never return a blocking error. Once a plan is accepted on
        # top of the imported board (the fallback file exists), fall through instead: the v1
        # reconciliation below already resolves through the same fallback and needs no v2 skip.
        hook_event = payload["hook_event_name"]
        if hook_event in {"SessionStart", "Stop"}:
            _refresh_architecture(root)
        if hook_event in {"SessionStart", "UserPromptSubmit"}:
            board_text = context(root, session_id, provider)
            packet_text = _packet_injection_text(
                root, session_id, provider,
                event=hook_event, source=str(payload.get("source", "")),
            )
            text = board_text + packet_text
            sources = []
            if board_text.strip():
                sources.append("project_work")
            if packet_text.strip():
                sources.append("grounding_packet")
            _record_context_injection(
                root,
                session_id=session_id,
                event_name=hook_event,
                text=text,
                sources=sources,
            )
            return text
        return ""
    reconciliation_error = ""
    try:
        _native().event(root, payload, provider)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        adapter = _native()
        with _locked(root):
            failed = adapter._load(root, provider, session_id)
            failed.update(
                conflict=f"Reconciliation unavailable: {exc}",
                observed_at=_now(),
                last_event=payload["hook_event_name"],
            )
            _write_json(adapter._file(root, provider, session_id), failed)
        reconciliation_error = f"Bear Hug native task reconciliation needs attention: {exc}"
    event = payload["hook_event_name"]
    if event in {"SessionStart", "Stop"}:
        _refresh_architecture(root)
    if reconciliation_error:
        if event in {"SessionStart", "UserPromptSubmit"}:
            _record_context_injection(
                root,
                session_id=session_id,
                event_name=event,
                text=reconciliation_error,
                sources=["reconciliation_error"],
            )
        return reconciliation_error
    if event not in {"SessionStart", "UserPromptSubmit", "PostToolUse"}:
        return ""
    if event == "PostToolUse":
        # Emit only after plan/board mutations or native task calls, not every ordinary read.
        tool = payload.get("tool_name", "").split(".")[-1]
        serialized = json.dumps(payload.get("tool_input", {}))
        if tool not in {"TaskCreate", "TaskUpdate", "update_plan"} and not any(
            marker in serialized
            for marker in ("bearhug-work", "bearhug-campaign", "superpowers/plans", "WORK.json")
        ):
            return ""
    setup_notice = ""
    if _execution_state(root) is not None and _campaign_enabled(root):
        prepared = _campaign_call(root, "onboard", "--provider", provider)
        if prepared.get("status") == "blocked":
            setup_notice = "\nExecution setup needs attention: " + str(prepared.get("reason", ""))
    board_text = context(root, session_id, provider)
    native_text = _native().context(
        root, provider, session_id, transcript_path=payload.get("transcript_path")
    )
    packet_text = (
        _packet_injection_text(
            root, session_id, provider,
            event=event, source=str(payload.get("source", "")),
        )
        if event in {"SessionStart", "UserPromptSubmit"}
        else ""
    )
    text = board_text + "\n" + native_text + setup_notice + packet_text
    if event in {"SessionStart", "UserPromptSubmit"}:
        sources = []
        if board_text.strip():
            sources.append("project_work")
        if native_text.strip():
            sources.append("native_tasks")
        if setup_notice.strip():
            sources.append("campaign_setup")
        if packet_text.strip():
            sources.append("grounding_packet")
        _record_context_injection(
            root, session_id=session_id, event_name=event, text=text, sources=sources
        )
    return text


MASTER_PLAN_PATH = "docs/memex/syntheses/MASTER-PLAN.md"


def _work_import(root: Path):
    if not __package__:
        library = str(_path(root, ".bearhug/lib"))
        if library not in sys.path:
            sys.path.insert(0, library)
    from bearhug import work_import

    return work_import


def _work_views_v2(root: Path):
    if not __package__:
        library = str(_path(root, ".bearhug/lib"))
        if library not in sys.path:
            sys.path.insert(0, library)
    from bearhug import work_views_v2

    return work_views_v2


def import_workv2(root: Path, *, force: bool = False) -> Path:
    """Read the project's BOARD/LEDGER/MASTER-PLAN (never writing them) into WORK.json v2.

    Refuses to touch an existing `docs/superpowers/plans/WORK.json` unless `force` is set; the
    source docs are never modified either way. See `bearhug.work_import.build_workv2` and the
    frozen contract it was built against.
    """
    board = _path(root, f"{PLAN_DIR}/BOARD.md")
    ledger = _path(root, f"{PLAN_DIR}/LEDGER.md")
    plan = _path(root, MASTER_PLAN_PATH)
    for label, path in (("BOARD", board), ("LEDGER", ledger), ("MASTER-PLAN", plan)):
        if not path.is_file():
            raise WorkError(f"Project {label} is missing: {path}")
    target = _path(root, STATE_PATH)
    if target.exists() and not force:
        raise WorkError(f"{STATE_PATH} already exists; pass --force to overwrite")
    work = _work_import(root).build_workv2(board, ledger, plan)
    # Record project-relative paths, not the absolute ones `_path` resolved for the safe read.
    work["source_docs"] = {
        "board": f"{PLAN_DIR}/BOARD.md",
        "ledger": f"{PLAN_DIR}/LEDGER.md",
        "master_plan": MASTER_PLAN_PATH,
    }
    _write_json(target, work)
    return target


def repair_views_v2(root: Path) -> dict[str, str]:
    """Regenerate BOARD/LEDGER/PLAN views from WORK.json v2 via `bearhug.work_views_v2`.

    Coded against the frozen call shape: `render_views(work: dict) -> dict[str, str]`, a
    path -> text map (paths are project-relative), given the WORK.json v2 dict this module
    writes. That module is a separate deliverable and may not exist yet; a missing or
    incompatible module raises a clear `WorkError`, never a bare traceback.
    """
    target = _path(root, STATE_PATH)
    if not target.is_file():
        raise WorkError(f"{STATE_PATH} does not exist; run the `import` operation first")
    work = json.loads(target.read_text(encoding="utf-8"))
    try:
        render_views = _work_views_v2(root).render_views
    except (ImportError, AttributeError) as exc:
        raise WorkError(
            "bearhug.work_views_v2.render_views is not available yet; "
            "the WORK.json v2 view generator has not landed"
        ) from exc
    views = render_views(work)
    if not isinstance(views, dict):
        raise WorkError("render_views must return a path -> text map")
    for relpath, text in views.items():
        destination = _path(root, relpath)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".work-view-", suffix=".tmp", dir=destination.parent
        )
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return views


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, help="project root (default: current Git worktree)")
    sub = parser.add_subparsers(dest="operation", required=True)
    read = sub.add_parser("status", help="read the board and discover new plans")
    read.add_argument("--json", action="store_true")
    sub.add_parser("board", help="read PLAN/BOARD/LEDGER as tab-separated rows")
    sub.add_parser("repair-views", help="regenerate managed Markdown views from WORK.json")
    importer = sub.add_parser(
        "import", help="import BOARD/LEDGER/MASTER-PLAN into WORK.json v2 (sources stay read-only)"
    )
    importer.add_argument(
        "--force", action="store_true", help="overwrite an existing WORK.json"
    )
    sub.add_parser(
        "repair-views-v2", help="regenerate BOARD/LEDGER/PLAN views from WORK.json v2"
    )
    push = sub.add_parser(
        "native-tasks", help="generate exact native task tool calls for this session"
    )
    push.add_argument("--provider", choices=("claude", "codex"), required=True)
    push.add_argument("--session", required=True)
    hook = sub.add_parser("native-hook", help=argparse.SUPPRESS)
    hook.add_argument("--provider", choices=("claude", "codex"), required=True)
    accept_parser = sub.add_parser("accept", help="register the exact user-approved plan")
    accept_parser.add_argument("plan")
    accept_parser.add_argument("--sha256", required=True, help="plan digest returned by status")
    grounding_parser = sub.add_parser(
        "grounding", help="print the compiled grounding packet for one accepted task"
    )
    grounding_parser.add_argument("task_id")
    grounding_parser.add_argument(
        "--refresh",
        action="store_true",
        help="recompile the full packet now, regardless of whether the corpus changed",
    )
    for action in ("start", "complete", "block"):
        task = sub.add_parser(action)
        task.add_argument("task_id")
        task.add_argument("--session", default=os.environ.get("CODEX_THREAD_ID", ""))
        task.add_argument("--provider", choices=("codex", "claude"), default="codex")
        task.add_argument("--evidence", default="")
    native = sub.add_parser("observe", help=argparse.SUPPRESS)
    native.add_argument("--event", required=True)
    native.add_argument("--session", required=True)
    native.add_argument("--context", action="store_true")


def run(args: argparse.Namespace) -> int:
    try:
        root = args.root
        if root is None:
            root = Path(
                subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
            )
        root = root.expanduser().resolve(strict=True)
        if args.operation == "native-hook":
            payload = json.loads(sys.stdin.buffer.read(16 * 1024 * 1024))
            text = native_hook(root, payload, args.provider)
            if text:
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": payload["hook_event_name"],
                                "additionalContext": text,
                            }
                        }
                    )
                )
            return 0
        if args.operation == "native-tasks":
            state = _execution_state(root)
            if not state:
                raise WorkError("Accept a plan before restoring native tasks")
            print(json.dumps(_native().projection(root, state, args.provider, args.session)))
            return 0
        if args.operation == "repair-views":
            if _project_board_authority(root) is not None:
                raise WorkError("Project-authored BOARD views are never regenerated by Bear Hug")
            with _locked(root):
                state = _load(root)
                if state:
                    _save(root, state)
        elif args.operation == "import":
            with _locked(root):
                target = import_workv2(root, force=args.force)
            print(json.dumps({"status": "ok", "path": str(target)}))
            return 0
        elif args.operation == "repair-views-v2":
            with _locked(root):
                views = repair_views_v2(root)
            print(json.dumps({"status": "ok", "paths": sorted(str(p) for p in views)}))
            return 0
        elif args.operation == "accept":
            accept(root, args.plan, expected_sha256=args.sha256)
        elif args.operation == "grounding":
            state = _execution_state(root)
            if state is None:
                raise WorkError("Accept a plan before requesting a grounding packet")
            packet = _task_packet(root, state, args.task_id, force=args.refresh)
            print(packet.get("text", ""))
            return 0
        elif args.operation in {"start", "complete", "block"}:
            transition(
                root,
                args.task_id,
                args.operation,
                session_id=args.session,
                provider=args.provider,
                evidence=args.evidence,
            )
            if args.operation == "complete":
                _refresh_architecture(root)
            if args.operation == "start" and _campaign_enabled(root):
                # `transition`'s campaign branch dispatches the whole accepted plan
                # (`project_campaign._board_request` embeds the full plan text in the prompt it
                # sends the controller, not just `args.task_id`'s own step), and the controller
                # picks its own order over the board. `args.task_id` is recorded on the campaign
                # binding and validated against the accepted plan, but it is a bookkeeping value,
                # not an instruction that constrains which task the campaign works next -- so a
                # `start T1` can legitimately show the campaign working T2 first. Printed to
                # stderr so it never lands inside the JSON/board output on stdout that callers
                # parse.
                print(
                    f"Note: the campaign runs the whole accepted plan and picks its own task "
                    f"order; {args.task_id} is recorded as requested but not forced to run "
                    "next.",
                    file=sys.stderr,
                )
        elif args.operation == "observe":
            observe(root, args.event, args.session)
            if args.context:
                print(context(root, args.session))
            return 0
        view = status(root)
        if args.operation == "board" or args.operation == "status" and not args.json:
            print(board(view))
        else:
            print(json.dumps(view, ensure_ascii=False))
        return 1 if view["status"] == "error" else 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"bearhug project work: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    configure_parser(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
