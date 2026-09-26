"""Task durability as a pure evaluator — a mechanical port of the captured Stop gate.

**The store, not the transcript.** The captured gate reads
`~/.claude/tasks/<session-id>/*.json` and its docstring records why: replaying
TaskCreate/TaskUpdate out of the JSONL "cannot observe a task leaving the list by any route that
writes no `status: deleted` update", so it reports tasks that no longer exist — an unsatisfiable
block, since the named task can neither be deleted nor made to name a doc. Measured 2026-08-14:
three tasks replayed as open while `TaskList` returned none and the store held no directory.

This evaluator therefore takes the store as an **injected argument**. It performs no I/O to read
the task store, names no path under `~/.claude`, and substitutes no replay-derived state for the
store. The lab constructs stores; a caller in production supplies the real reader.

**Mechanical port, except the one ruled difference below.** Every OTHER pass/block decision is
the captured gate's, including its precedence: `undocumented` is evaluated before board parity,
which is evaluated before phase tags, because the legacy `block()` exits and only the first
applicable block ever fires. See `PORTED_PARITY_DIFFERENCES` for the one place this port
deliberately does not match the captured gate.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ..results import EvaluatorResult, Evidence

# `_parse_assignments` carries no leading-underscore exemption in `writes.py`'s own public
# surface, but decision R05 is that the write question has ONE shared answer, and an in-command
# `NAME=value` assignment is part of that answer (`_in_project_root` already resolves one for
# scoping). A second, independent assignment parser here would be exactly the drift R05 exists to
# prevent, so this reaches for the one `writes.py` already has rather than re-deriving
# `_ASSIGN_RE` a second time.
from ..writes import (
    _parse_assignments,
    expand_home,
    project_root,
    resolve_any_file_writes,
    shell_skeleton,
    strip_heredocs,
)

GATE_ID = "task-durability"
GATE_VERSION = "1.4.0"

BOARD = "docs/superpowers/plans/BOARD.md"

#: Ported verbatim from the captured gate.
DOC_PATH = re.compile(r"docs/[\w./-]+\.md")
PHASE_TAG = re.compile(
    r"^\[(P\d[^\]]*|corpus)\]"
    # `#<row> · <phase>` form. The phase token may ALSO be wrapped in its own brackets —
    # `#236 · [corpus]`, `#236 · [P1]`, `#236 · [P5·hold]` — which a real session wrote and which
    # this rejected before. `\b` sits right after the bare phase token (before any bracket),
    # because asserting it after an optional trailing `]` fails: `]` is a non-word char, so a
    # boundary check right after it against a following space (also non-word) never fires.
    r"|^#\d+\s*·\s*\[?(P\d[0-9]*|corpus|UNPLACED|OPEN_Q)\b[^\]]*\]?"
)
EDIT_TOOL_NAMES = ("Edit", "Write", "MultiEdit", "NotebookEdit")
TASK_TOOL = "TaskCreate"
TOOLSEARCH_MISS = "No matching deferred tools found"
BOARD_WRITE = re.compile(
    r">>?\s*\S*BOARD\.md"
    r"|tee\s+(?:-a\s+)?\S*BOARD\.md"
    r"|sed\s+-i[^|;]*BOARD\.md"
)

#: The literal board path, escaped, for the two small regexes below that are NOT a competing
#: write resolver (`_board_written`'s write question goes to `resolve_any_file_writes`, the
#: runtime's one shared answer to "did this write?" — R05/`writes.py`).
_BOARD_ESCAPED = re.escape(BOARD)

#: A `git checkout`/`git restore` invocation whose OWN target is the board. The shared resolver's
#: "any" scope counts these as writes (they overwrite working-tree content from the index or
#: another ref), which is correct for the source/review questions it also answers. It is wrong
#: for `board_only`: a revert restores OLD content and records no new row, so it is not decision
#: 0137's write. Matched against `shell_skeleton(command)`, never the raw command — a review found
#: the raw form let quoted or heredoc TEXT that merely names a board checkout (`echo 'undo: git
#: checkout -- <board>' >> <board>`, or a heredoc body line that is one) trigger this exclusion on
#: a command that never ran one. Mirrors `writes.py`'s own checkout/restore capture shape (verb,
#: then the target as the last token of the simple command).
#:
#: A MATCH SKIPS THE WHOLE COMMAND, not just a checkout clause within it: a command containing a
#: board checkout earns no credit for ANY board write in it, so `git checkout -- <board> && echo
#: row >> <board>` still blocks and needs two separate tool_use calls to pass. This is deliberate,
#: not a narrower exclusion left unfinished — it is also what makes `echo row >> <board> && git
#: checkout -- <board>` (an append immediately undone in the same command) block, PROVIDED the
#: checkout's own target is UNQUOTED: `... && git checkout -- "<board>"` has its target blanked by
#: `shell_skeleton` before this pattern ever sees it, so the exclusion does not fire and the append
#: is credited even though the checkout undoes it. That is the same shape as two separate calls
#: (checkout, then append) or X4 (append, then a LATER separate checkout) — this predicate counts
#: events, not the board's final state, in every one of them.
_GIT_CHECKOUT_RESTORE_BOARD = re.compile(
    r"\bgit\s+(?:checkout|restore)\b[^\n;&|]*\s+(?:\S*/)?" + _BOARD_ESCAPED + r"\s*(?=$|[;&|\n])"
)

#: Intentional behaviour differences from the captured gate. Normally EMPTY BY DESIGN: R09 is a
#: mechanical port, so a difference here would be a policy change smuggled into a conversion —
#: which is exactly why a ruled one is named here rather than left to drift in silently.
PORTED_PARITY_DIFFERENCES: tuple[str, ...] = (
    "TD-UNDETECTED-BOARD-ONLY: the captured gate's main() calls board_touched() only inside the "
    "task_tool_absent() branch. When absence is never DETECTED — no correlated ToolSearch miss, "
    "no harness withdrawal notice, which is what a client with no task tool at all leaves behind "
    "— the captured gate never asks board_touched and falls straight to block(task_list_empty), "
    "even on a session that updated the board. The board is the durable half of the mirror "
    "whether or not absence was detected, so this port passes with board_only when the "
    "undetected-absence case has a REAL board write: see _board_written, which asks the shared "
    "write resolver (resolve_any_file_writes) whether a tool_use's own effect wrote the board, "
    "confirmed by a non-error correlated tool_result. A review found an earlier, ad hoc regex "
    "here reachable with no board write at all, by reading command TEXT rather than effect — a "
    "heredoc body, a quoted string, or a commit message describing a write all counted — and "
    "found _board_touched itself (kept unchanged for board_only_tool_absent) reachable the same "
    "way through an edit to another file that merely names the path, a same-named file elsewhere, "
    "a failed or denied board edit, or a Bash redirect into a scratch copy. "
    "board_only_tool_absent (the detected-absence pass) keeps _board_touched exactly as ported "
    "and is otherwise unchanged; so are the task_tool_withdrawn/task_list_empty blocks.",
)

#: Known defects carried over deliberately, with the finding id that tracks each. The captured
#: gate documents both as retained rather than overlooked, so "fixing" either here would change
#: policy without a ruling. They belong to a separate, ruled task.
KNOWN_PORTED_DEFECTS = {
    "TD-BOARD-SUBSTRING": (
        "board_touched scans the RAW transcript line rather than the parsed event. The gate's own "
        "comment keeps it that way — 'kept for board_touched's line scan' — even though its "
        "sibling check was rewritten to follow the event under decision 0204. A tool_result whose "
        "CONTENT quotes a BOARD row alongside the bytes \"name\":\"Bash\" can therefore read as a "
        "board write."
    ),
    "TD-GIT-COMMIT-SUBSTRING": (
        "`\"git commit\" in command` is a raw substring over a command this repo routinely writes "
        "as a `git commit -F -` heredoc quoting code, and the gate imports no heredoc stripper."
    ),
}


def _iter_records(lines: Iterable[str]):
    """Yield (raw_line, parsed_or_None). A malformed line is data, never a crash."""
    for line in lines:
        try:
            yield line, json.loads(line)
        except (TypeError, ValueError):
            yield line, None


def _tool_uses(record: Any):
    if not isinstance(record, Mapping) or record.get("type") != "assistant":
        return
    content = (record.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return
    for part in content:
        if isinstance(part, Mapping) and part.get("type") == "tool_use":
            yield part


def _session_changed_files(records) -> bool:
    """An edit tool_use, or a Bash `git commit`. Structural, per decision 0204."""
    for _raw, record in records:
        for part in _tool_uses(record):
            if part.get("name") in EDIT_TOOL_NAMES:
                return True
            if part.get("name") == "Bash":
                command = (part.get("input") or {}).get("command") or ""
                # TD-GIT-COMMIT-SUBSTRING, ported deliberately.
                if "git commit" in command:
                    return True
    return False


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") if isinstance(part, Mapping) else ""
            for part in content
        )
    return ""


def _task_tool_absent(records) -> bool:
    """Absence needs a correlated search miss or a harness-authored withdrawal notice.

    A `TaskCreate` tool_use anywhere means the tool was usable, so absence is moot. Otherwise a
    ToolSearch miss counts only when CORRELATED by tool_use_id to a ToolSearch whose query names
    the task tool.  The other captured branch is any non-assistant record whose content is the
    harness's plain-string ``TaskCreate ... no longer available`` notice; assistant prose and
    tool-result data never qualify.  Measured 2026-08-20, scanning arbitrary blobs let a single
    Read of the gate's own source flip a session to tool-absent.
    """
    searched: set[str] = set()
    missed = False
    withdrawal = False
    for _raw, record in records:
        for part in _tool_uses(record):
            if part.get("name") == TASK_TOOL:
                return False
            if part.get("name") == "ToolSearch":
                query = str((part.get("input") or {}).get("query") or "")
                if TASK_TOOL.lower() in query.lower() or "task" in query.lower():
                    identifier = part.get("id")
                    if identifier:
                        searched.add(str(identifier))
        if isinstance(record, Mapping) and record.get("type") != "assistant":
            content = (record.get("message") or {}).get("content") or []
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, Mapping):
                        continue
                    if part.get("type") != "tool_result":
                        continue
                    if str(part.get("tool_use_id") or "") not in searched:
                        continue
                    if TOOLSEARCH_MISS in _result_text(part.get("content")):
                        missed = True
            elif (
                isinstance(content, str)
                and "no longer available" in content
                and TASK_TOOL in content
            ):
                withdrawal = True
    return missed or withdrawal


def _board_touched(records) -> bool:
    """TD-BOARD-SUBSTRING, ported deliberately: a raw-line scan, not an event walk."""
    for raw, _record in records:
        if "BOARD.md" not in raw:
            continue
        if any(f'"name":"{name}"' in raw for name in EDIT_TOOL_NAMES):
            return True
        if '"name":"Bash"' in raw and BOARD_WRITE.search(raw):
            return True
        # The snapshot's own fixtures are written with spaces after the colon, which the quoted
        # forms above miss. Accept the structured shape too, so the port is not STRICTER than the
        # gate — a narrower board_touched would turn a pass into a block.
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for part in _tool_uses(record):
            if part.get("name") in EDIT_TOOL_NAMES:
                path = str((part.get("input") or {}).get("file_path") or "")
                if path.endswith("BOARD.md"):
                    return True
            if part.get("name") == "Bash":
                command = str((part.get("input") or {}).get("command") or "")
                if BOARD_WRITE.search(command):
                    return True
    return False


#: The resolver returns a token as the command/edit wrote it, unsubstituted -- `_in_project_root`
#: expands `$CLAUDE_PROJECT_DIR`/`${CLAUDE_PROJECT_DIR}` ONLY to decide in-root-or-not, never in
#: the path it hands back. `_is_board_path` must expand it too, or a write to
#: `$CLAUDE_PROJECT_DIR/docs/superpowers/plans/BOARD.md` would compare the literal, unexpanded
#: text against the board path and never match.
_CLAUDE_PROJECT_DIR = re.compile(r"\$\{?CLAUDE_PROJECT_DIR\}?")

#: `$HOME`/`${HOME}` normalised to `~` first, so the SAME `os.path.expanduser` call resolves both
#: spellings from the one place Python already reads the real value (`HOME`, then the pwd
#: database) instead of a second, independent environment read here.
_HOME_VAR = re.compile(r"\$\{?HOME\}?")

#: A leading `$NAME`/`${NAME}` reference, exactly `_in_project_root`'s own shape (`writes.py`), so
#: an in-command assignment resolves the same way for this comparison as it already does for
#: root-scoping.
_VAR_PREFIX = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(.*)$")


def _expand_board_candidate(path: str, assignments: Mapping[str, str], root: str) -> str:
    """Resolve what `_in_project_root` (`writes.py`) already resolves for SCOPING, so the exact
    comparison in `_is_board_path` sees what the shell would actually write to rather than the
    literal, unresolved token the resolver hands back.

    `$CLAUDE_PROJECT_DIR`/`${CLAUDE_PROJECT_DIR}` substitute `root` (`_in_project_root`'s own
    rule). `$HOME`/`${HOME}` are normalised to `~` and then expanded the same way a literal `~`
    is, via `writes.expand_home` (`os.path.expanduser`, exposed there rather than called directly
    here: `test_the_evaluator_never_reads_the_real_task_store` forbids this evaluator's OWN source
    from calling it, a proxy for "never reaches into `~/.claude`" that a direct call would trip
    for this unrelated reason). A leading `$NAME`/`${NAME}` resolves against an assignment EARLIER
    IN THE SAME COMMAND when one exists (`_parse_assignments`, threaded in by the caller; empty
    for an Edit/Write/MultiEdit `file_path`, which no shell variable reaches).

    Deliberately NOT resolved, and named in `_board_written`'s FAIL-SAFE GAPS: `$PWD`/`${PWD}`
    (not a project-relative concept `writes.py` tracks) and any OTHER unassigned variable —
    resolving either would mean guessing a value this predicate cannot verify.
    """
    expanded = _CLAUDE_PROJECT_DIR.sub(lambda _match: root, path)
    expanded = _HOME_VAR.sub("~", expanded)
    if expanded.startswith("~"):
        return expand_home(expanded)
    if expanded.startswith("$"):
        match = _VAR_PREFIX.match(expanded)
        if match:
            name, rest = match.group(1), match.group(2)
            if name in assignments:
                return assignments[name] + rest
    return expanded


def _is_board_path(path: str, assignments: Mapping[str, str], root: str) -> bool:
    """True when `path` (a resolved write target, possibly relative or variable-prefixed) IS the
    project's board.

    A review found `normalized.endswith("/" + BOARD)` too loose: it also matches
    `<root>/tests/fixtures/docs/superpowers/plans/BOARD.md`, `<root>/.claude/worktrees/w/docs/
    superpowers/plans/BOARD.md` (where Claude Code places a worktree) and a Bash write to
    `sub/docs/superpowers/plans/BOARD.md` -- every one a real file ending in the board's own
    relative layout, but not the board. Resolving both sides against `root` before comparing
    fixes this: `normpath(join(root, path))` is the board only when it equals
    `normpath(join(root, BOARD))` exactly, which `os.path.join` computes correctly whether `path`
    is relative (joined onto `root`) or already absolute (its own value wins, `root` dropped, and
    a same-named path under a DIFFERENT absolute prefix cannot collide with the true board path).
    """
    expanded = _expand_board_candidate(path, assignments, root)
    return (
        os.path.normpath(os.path.join(root, expanded))
        == os.path.normpath(os.path.join(root, BOARD))
    )


def _board_written(records, root: str) -> bool:
    """A REAL write to the board file itself, through the shared write resolver. Used only for
    the `board_only` pass.

    `_board_touched` (above, unchanged, still the only check behind `board_only_tool_absent`) is a
    raw-line substring scan ported verbatim from the captured gate (TD-BOARD-SUBSTRING): it accepts
    a read, a grep hit, prose, or an edit to any file merely named or mentioning "...BOARD.md". A
    review found that reachable with no board write at all — an edit to another file whose text
    cites the board path, a file named DASHBOARD.md, a board edit that failed or was denied, and a
    Bash redirect into a same-named scratch copy elsewhere all satisfied it.

    This predicate first asked an ad hoc regex the same question and a review found IT reachable
    too, the same way, by reading command TEXT rather than effect: a heredoc body, a quoted
    string, a `.bak`/`~`/`-old` variant of the board, and a redirect into a scratch copy or another
    checkout all matched it. It now asks `resolve_any_file_writes` — the one write resolver
    `review_gate.py` already uses for the source-write question (R05/`writes.py`), which strips
    heredoc bodies and quoted data before matching shell write forms, so a description of a write
    is not a write, and scopes every candidate to `root` (`project_root(event)`, threaded through
    by the caller) so a same-named file under a different absolute prefix cannot count either
    (`_is_board_path`). `git checkout`/`git restore` targeting the board, matched against the
    command's SKELETON so quoted or heredoc text naming one cannot trigger it, are excluded
    (`_GIT_CHECKOUT_RESTORE_BOARD`): the resolver correctly counts them as writes for the
    source/review questions, but a revert restores OLD content and records no new row, so it is
    not decision 0137's write.

    Either way, a tool_use is credited only when correlated by `tool_use_id` to a `tool_result`
    that is not `is_error`. A tool_use with no id, or no correlated result, earns no credit: this
    requires POSITIVE confirmation of success, not merely the absence of a reported failure. A
    denial is reported the same way a plain tool error is (`is_error: true`), so one check covers
    both.

    FAIL-SAFE GAPS, named rather than silently carried — a real board write blocks in each of
    these, and the `task_list_empty` remediation's promise is worded to not oversell past them:
    - a QUOTED redirect target (`>> "docs/superpowers/plans/BOARD.md"`): `writes.py` does not
      support a quoted operand, board or otherwise, and that is not this port's to fix;
    - a command that ALSO restores the board (`_GIT_CHECKOUT_RESTORE_BOARD` skips the whole
      command, by design — see its own comment — for an UNQUOTED checkout target; a quoted one is
      blanked by the skeleton and does not trigger the exclusion, consistent with this predicate
      counting events rather than the board's final state, the same as two separate calls);
    - an absolute-path edit when the Stop event's `cwd` is not the project root (this predicate
      scopes strictly to `project_root(event)`, and nothing here can know a later Bash `cd`
      changed the session's actual working directory if the event does not say so);
    - a `git checkout`/`git restore` of the board reached through `/tmp` or `/private/tmp` (always
      scratch to `writes.py`, regardless of variable name);
    - a `$PWD`/`${PWD}` or any OTHER unassigned-in-command variable prefix
      (`_expand_board_candidate` resolves `$CLAUDE_PROJECT_DIR`, `$HOME`/`~`, and an in-command
      assignment; not a variable this predicate has no way to resolve).
    Also NOT fixed here, and not this port's to fix: `writes.py`'s `_ASSIGN_RE` keeps a trailing
    `;` in an assigned value (`ROOT=/path; ... >> $ROOT/BOARD.md` resolves `ROOT` to `/path;`, so
    the target resolves outside the root and this predicate, correctly following the resolver,
    does not credit it). That is a pre-existing resolver defect, not a `board_only` one.
    """
    candidates: dict[str, bool] = {}
    for _raw, record in records:
        for part in _tool_uses(record):
            identifier = part.get("id")
            if not identifier:
                continue
            name = part.get("name")
            tool_input = part.get("input") or {}
            assignments: Mapping[str, str] = {}
            if name == "Bash":
                command = str(tool_input.get("command") or "")
                if _GIT_CHECKOUT_RESTORE_BOARD.search(shell_skeleton(command)):
                    continue
                assignments = _parse_assignments(strip_heredocs(command))
            resolution = resolve_any_file_writes(name, tool_input, root=root)
            if any(_is_board_path(path, assignments, root) for path in resolution.paths):
                candidates.setdefault(str(identifier), False)
    if not candidates:
        return False
    for _raw, record in records:
        if not isinstance(record, Mapping) or record.get("type") == "assistant":
            continue
        content = (record.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") != "tool_result":
                continue
            identifier = str(part.get("tool_use_id") or "")
            if identifier in candidates and not part.get("is_error"):
                candidates[identifier] = True
    return any(candidates.values())


def _undocumented(tasks: Mapping[str, Mapping[str, Any]], repo) -> list[str]:
    bad = []
    for tid, rec in tasks.items():
        if rec.get("open_question"):
            continue
        authority = rec.get("authority") or ""
        if authority and repo.doc_exists(authority):
            continue
        if any(repo.doc_exists(p) for p in DOC_PATH.findall(rec.get("text") or "")):
            continue
        bad.append(rec.get("subject") or tid)
    return bad


def _open_tasks(tasks):
    return {tid: rec for tid, rec in tasks.items() if not rec.get("completed")}


def _missing_from_board(tasks, repo) -> list[str]:
    if not repo.board_exists():
        return []  # a board missing entirely is the durability check's problem
    return [
        f"#{tid} {rec.get('subject') or ''}"
        + ("" if rec.get("board_row") else "  (declares no metadata.board_row)")
        for tid, rec in tasks.items()
        if not repo.has_board_row(rec.get("board_row") or "")
    ]


def _untagged(tasks) -> list[str]:
    return [
        f"#{tid} {rec.get('subject') or ''}"
        for tid, rec in tasks.items()
        if not PHASE_TAG.match(rec.get("subject") or "")
    ]


def _listed(items: list[str]) -> str:
    """The captured gate's list, INCLUDING its overflow line.

    `task-durability.py:492-493` writes `listed` and then `more` — `"\n  … and N more"` when there
    are more than eight. This dropped `more`, so twenty undocumented tasks showed eight with no
    sign that twelve were missing, and the count in the first line disagreed with the list under
    it for no visible reason.
    """
    shown = "\n".join(f"  - {item}" for item in items[:8])
    if len(items) > 8:
        return shown + f"\n  … and {len(items) - 8} more"
    return shown


def evaluate_task_durability(
    event: Mapping[str, Any],
    *,
    event_id: str,
    tasks: Mapping[str, Mapping[str, Any]],
    repo: Any,
    transcript_lines: Iterable[str] = (),
    transcript_readable: bool,
    gate_version: str = GATE_VERSION,
    now: Callable[[], float] | None = None,
) -> EvaluatorResult:
    """One verdict about one Stop event. Writes nothing; prints nothing. Reads the transcript,
    and — through the shared write resolver, `_board_written` calls `project_root(event)` and
    `resolve_any_file_writes`, exactly as `review_gate.py` already does — the process environment
    and filesystem for path resolution (`CLAUDE_PROJECT_DIR`, the process cwd, `os.path.realpath`).
    """
    import time

    clock = now or time.perf_counter
    started = clock()

    def finish(build, **kw):
        return build(
            gate_id=GATE_ID, gate_version=gate_version, event_id=event_id,
            duration_ms=max(0.0, round((clock() - started) * 1000, 4)), **kw
        )

    if event.get("stop_hook_active"):
        return finish(EvaluatorResult.not_applicable, reason_code="stop_hook_active")
    if not event.get("transcript_path") or not transcript_readable:
        return finish(EvaluatorResult.not_applicable, reason_code="no_transcript")

    records = list(_iter_records(transcript_lines))

    if not tasks:
        if not _session_changed_files(records):
            return finish(
                EvaluatorResult.passed,
                reason_code="no_tasks_no_edits",
                evidence=(Evidence("state", "task_count=0"),
                          Evidence("state", "session_changed_files=false")),
            )
        tool_absent = _task_tool_absent(records)
        # Decision 0137: the board is the durable half of the mirror whether or not the task
        # tool's absence was DETECTED. `tool_absent` reads False both when a client never offers
        # a task tool at all (the desktop app's Code tab, a non-interactive session) AND when
        # nothing in the transcript happens to correlate — it is silence, not proof either way:
        # not proof the tool was absent, and (unlike a `TaskCreate` call, which short-circuits
        # `_task_tool_absent` to False directly) not proof it was used either.
        #
        # The two passes use DIFFERENT board checks, by ruling. `board_only_tool_absent` (detected
        # absence, already harness-confirmed) keeps `_board_touched` exactly as ported — a raw-line
        # scan, TD-BOARD-SUBSTRING. `board_only` (undetected absence, the ONLY evidence available)
        # requires `_board_written`'s stronger confirmation through the shared write resolver. A
        # review found the raw-line scan reachable with no board write at all, which the weaker
        # check could not afford here since nothing else backs the pass.
        if tool_absent:
            if _board_touched(records):
                return finish(
                    EvaluatorResult.passed,
                    reason_code="board_only_tool_absent",
                    evidence=(Evidence("state", "task_tool_absent=true"),
                              Evidence("state", "board_touched=true")),
                )
            return finish(
                EvaluatorResult.blocked,
                reason_code="task_tool_withdrawn",
                remediation=(
                    "task existence (decision 0137) — the task tool was withdrawn this "
                    "session, so the list cannot be created. That does NOT waive the "
                    "requirement: the board is the durable half of the mirror and this session "
                    "did not touch it.\n\n"
                    "Add a row to " + BOARD + " for each unit of work in flight, with its phase "
                    "tag and an authority path that resolves."
                ),
                evidence=(Evidence("state", "task_tool_absent=true"),
                          Evidence("state", "board_touched=false")),
            )
        root = project_root(event)
        if _board_written(records, root):
            return finish(
                EvaluatorResult.passed,
                reason_code="board_only",
                evidence=(Evidence("state", "task_tool_absent=false"),
                          Evidence("state", "board_written=true")),
            )
        return finish(
            EvaluatorResult.blocked,
            reason_code="task_list_empty",
            remediation=(
                "task existence (decision 0137) — this session edited files or committed, "
                "and the task list is EMPTY. The list carries the whole programme, so a "
                "session that changes the repo and tracks nothing loses that work at the "
                "session boundary.\n\n"
                "Create a task per unit of work in flight, each naming an existing "
                "docs/**.md path (the durability check below) and carrying its phase tag.\n\n"
                "A real row written to " + BOARD + " satisfies this in most clients, not only "
                "one with no task tool — though a quoted redirect target, a command that also "
                "reverts the board, or an absolute path outside this session's own working "
                "directory can still block a genuine write; a relative Bash append, or an Edit "
                "of the board's absolute path inside this project, avoids all three. Prefer a "
                "task where the tool exists, since the list carries phase and board-row "
                "metadata a bare row does not; otherwise add the row directly, with its own "
                "phase tag and an authority path that resolves. The board is the durable "
                "half.\n\n"
                "Measured 2026-08-14: a full session — 22 decision records, three code "
                "changes, a live runtime measurement — ran with an empty list, and this "
                "gate passed every turn because it iterated zero tasks."
            ),
            evidence=(Evidence("state", "task_count=0"),
                      Evidence("state", "session_changed_files=true")),
        )

    bad = _undocumented(tasks, repo)
    if bad:
        return finish(
            EvaluatorResult.blocked,
            reason_code="task_authority_path_missing",
            remediation=(
                f"task durability (CLAUDE.md §9, decision 0137) — {len(bad)} task(s) name no "
                f"existing docs/**.md file:\n{_listed(bad)}\n\n"
                f"The task list is SESSION-SCOPED. A task whose content lives only here is "
                f"invisible to a concurrent session and to a fresh window — measured "
                f"2026-08-12, when two briefed tasks could not be seen by the session they "
                f"were written for.\n\n"
                f"Write the durable doc (docs/superpowers/plans/ or specs/), then reference "
                f"its path in the task. If a task genuinely needs no doc, it is not carrying "
                f"the programme and should be folded into one that does."
            ),
            evidence=(Evidence("state", f"undocumented_count={len(bad)}"),),
        )

    opens = _open_tasks(tasks)
    lost = _missing_from_board(opens, repo)
    if lost:
        return finish(
            EvaluatorResult.blocked,
            reason_code="board_row_missing",
            remediation=(
                f"board parity (decision 0137/0153) — {len(lost)} open task(s) do not mirror "
                f"an existing row in {BOARD}:\n{_listed(lost)}\n\n"
                f"The board is the task list written to disk, and a missing row is the "
                f"measured lost-row failure. Add the row (same turn as the task change) and "
                f"record which row the task mirrors:\n"
                '  TaskUpdate(taskId, metadata={"board_row": "<row number>"})\n'
                "Keyed on that metadata, NOT on the task id: ids are session-scoped and "
                "restart at 1 while board rows are one global sequence, so an id-keyed check "
                "passed against whatever unrelated row happened to share the number."
            ),
            evidence=(Evidence("state", f"missing_board_rows={len(lost)}"),),
        )

    untagged = _untagged(opens)
    if untagged:
        return finish(
            EvaluatorResult.blocked,
            reason_code="phase_tag_missing",
            remediation=(
                f"phase tag (decision 0152 + the master plan) — {len(untagged)} open task(s) "
                f"carry no [P#]/[corpus] tag:\n{_listed(untagged)}\n\n"
                f"Every task states its phase from docs/memex/syntheses/ (the sequence "
                f"authority). Prefix the subject with [P1]..[P5·hold] or [corpus]."
            ),
            evidence=(Evidence("state", f"untagged_count={len(untagged)}"),),
        )

    return finish(
        EvaluatorResult.passed,
        reason_code="tags_ok",
        evidence=(Evidence("state", f"task_count={len(tasks)}"),
                  Evidence("state", f"open_task_count={len(opens)}")),
    )
