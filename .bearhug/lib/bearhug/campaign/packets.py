"""Per-task grounding packets: compiled at plan accept, refreshed only when the corpus moves.

Reuses :func:`bearhug.campaign.grounding.compile_grounding` -- the same engine mode 3 onboarding
uses -- to compile one packet per task, keyed by plan digest and task id, from that task's own
row (title, dependencies and completion criteria, taken from the plan's own task table) plus the
plan's surrounding prose with every *other* task's row removed. Passing the whole table would let
one task's packet bind decisions that only another task's row mentions; see `_task_scoped_text`.
Packets are machine-local under ``.bearhug/packets/`` (gitignored): the working tree they
describe stays clean, and a plan re-accept wipes the whole directory before repopulating it
under the new digest, so nothing stale from a retired plan survives.

Each packet records the corpus digest (decision ids, content hashes and states) it was built
from, so a caller can tell cheaply whether it is still current without recompiling, plus a much
cheaper stat-based "quick signature" (decision file names, sizes and mtimes) that lets a repeat
caller -- typically a hook, on every prompt -- skip reading and hashing every decision file's
content when nothing on disk has moved at all. Stdlib only, matching ``grounding.py``: this
module is copied into managed projects alongside it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bearhug.campaign.grounding import (
    DECISION_DIR,
    Grounding,
    GroundingError,
    compile_grounding,
    read_decisions,
    read_project_excludes,
)

PACKETS_DIR = ".bearhug/packets"
SCHEMA_VERSION = 1
#: Roughly 1500 tokens at ~4 characters/token; stated here so a reader of a packet's ``text``
#: (or of the docs, which quote this number) never has to guess what "token-bounded" means.
MAX_PACKET_CHARS = 6000
HOOK_CONSULT: tuple[str, ...] = ("memex",)
FULL_CONSULT: tuple[str, ...] = ("memex", "architecture", "graft", "memq")
HOOK_TIMEOUT_S = 2.0
_TASK_TABLE_HEADER = ("id", "task", "depends on", "done when")


def corpus_digest(root: Path) -> str:
    """A stable digest of the decision corpus: ids, content hashes and states, sorted.

    Read-only, like every other grounding input. A corpus with no decision records at all still
    yields a digest (of an empty list), so "the corpus did not change" is well-defined even when
    it is empty. This reads and parses every decision file's content; ``quick_signature`` below
    is the cheap precheck a hot path should try first.
    """

    try:
        decisions = read_decisions(root)
    except (OSError, ValueError):
        decisions = []
    rows = sorted((d.decision_id, d.sha256, d.status) for d in decisions)
    canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def quick_signature(root: Path) -> str:
    """A cheap stat-only fingerprint of the decision directory: names, sizes and mtimes.

    Never opens or parses a file. When this is unchanged since a packet was last confirmed
    current, nothing on disk has moved (no add, remove, rename or content/metadata-changing
    write can happen without changing at least one of these), so ``corpus_digest`` -- which reads
    every file -- need not be recomputed at all. When it *has* changed, that is only a cheap
    signal to check further: a touch or a same-content rewrite changes this without changing
    ``corpus_digest``, so ``get_or_refresh`` falls back to the real digest before deciding to
    recompile.

    Accepted trade-off: an edit that keeps both the file's size and its mtime (for example a copy
    made with ``cp -p`` or ``rsync -t`` immediately after another edit to the same second, or any
    write through a tool that deliberately preserves mtime) changes neither size nor mtime and is
    therefore invisible here; ``corpus_digest`` is the fallback that always sees it eventually --
    the case is rare enough, and reading every decision file's content is expensive enough, that
    this module accepts a same-size-same-mtime edit going unnoticed until something else (a
    different file changing, or `` --refresh ``) triggers a real ``corpus_digest`` read.
    """

    directory = root / DECISION_DIR
    try:
        entries = []
        for entry in directory.iterdir():
            info = entry.stat(follow_symlinks=False)
            entries.append((entry.name, info.st_size, info.st_mtime_ns))
        entries.sort()
    except OSError:
        entries = []
    canonical = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _packet_path(root: Path, plan_digest: str, task_id: str) -> Path:
    return root / PACKETS_DIR / plan_digest / f"{task_id}.json"


def _matched_by_id(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    memex = report.get("memex", {})
    matched = memex.get("matched", []) if isinstance(memex, Mapping) else []
    return {row["id"]: row for row in matched if isinstance(row, Mapping) and "id" in row}


def _cells(line: str) -> list[str]:
    return [
        cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
    ]


def _task_scoped_text(plan_text: str, task: Mapping[str, Any]) -> str:
    """The plan's prose with the task table reduced to ``task``'s own row.

    Mirrors ``project_work.parse_tasks``'s own table detection (fenced-block skipping, the exact
    header cells, one table only) so the two never disagree about where the table starts and
    ends. Every other task's title and "done when" text is removed before this goes into
    ``compile_grounding``'s term extraction; without this, a plan-wide term list makes every
    task's packet bind the same plan-level decisions instead of decisions specific to that task.
    """

    kept: list[str] = []
    in_table = False
    fenced = False
    found_table = False
    for line in plan_text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            kept.append(line)
            continue
        if fenced:
            kept.append(line)
            continue
        cells = _cells(line)
        if [cell.casefold() for cell in cells] == list(_TASK_TABLE_HEADER):
            in_table = found_table = True
            continue  # drop the header; a fresh one-row table is appended below
        if in_table:
            if not line.strip().startswith("|"):
                in_table = False
                kept.append(line)
            # else: drop the separator/divider row and every data row
            continue
        kept.append(line)
    prose = "\n".join(kept)
    if not found_table:
        return prose
    deps = ", ".join(task.get("depends_on") or []) or "—"
    row_table = (
        "\n\n| ID | Task | Depends on | Done when |\n"
        "| --- | --- | --- | --- |\n"
        f"| {task.get('id', '')} | {task.get('title', '')} | {deps} | "
        f"{task.get('done_when', '')} |\n"
    )
    return prose + row_table


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _render_text(root: Path, task: Mapping[str, Any], grounding: Grounding) -> str:
    memex = grounding.report.get("memex", {})
    task_id = task.get("id", "?")
    if not isinstance(memex, Mapping) or memex.get("status") != "ok":
        reason = memex.get("reason") if isinstance(memex, Mapping) else ""
        status = memex.get("status", "unavailable") if isinstance(memex, Mapping) else "unavailable"
        return f"Grounding packet for {task_id}: no decisions bound ({reason or status})."
    if not memex.get("matched"):
        return f"Grounding packet for {task_id}: no decisions in the corpus bound to this task."
    matched = _matched_by_id(grounding.report)
    try:
        decisions_by_id = {d.decision_id: d for d in read_decisions(root)}
    except (OSError, ValueError):
        decisions_by_id = {}
    lines = [f"Grounding packet for {task_id} -- {task.get('title', '')}", "Bound decisions:"]
    for binding in grounding.bindings:
        if not binding["binding_id"].startswith("binding.decision."):
            continue
        decision_id = binding["binding_id"][len("binding.decision.") :]
        row = matched.get(decision_id, {})
        path = row.get("path") or ", ".join(row.get("paths", []) or []) or "?"
        score = row.get("score")
        terms = ", ".join(row.get("terms", []) or []) or "none"
        decision = decisions_by_id.get(decision_id)
        # `binding["meaning"]` is the title with "(proposed; not authority)" already appended for
        # a non-accepted record; read the plain title straight off the decision record instead,
        # so the ruling line below can carry its own, separate, unsuffixed marker.
        title = decision.title if decision is not None else binding["meaning"]
        ruling_line = _first_line(decision.ruling) if decision is not None else ""
        suffix = "" if binding["state"] == "accepted" else " (proposed; not authority)"
        header = f"- {decision_id} ({binding['state']}) {title}{suffix}"
        lines.append(f"{header}: {ruling_line}" if ruling_line else header)
        lines.append(f"  bound via: {path} (terms: {terms}), score {score}")
    pointers = [
        source
        for source in grounding.sources
        if source.get("kind") not in {"accepted_binding", "proposal"}
        and source.get("source_id") != "grounding.limits"
    ]
    if pointers:
        lines.append("Pointers:")
        for source in pointers[:10]:
            where = source.get("path") or source.get("reason", "")
            lines.append(f"- {source.get('source_id')}: {where}")
    text = "\n".join(lines)
    if len(text) > MAX_PACKET_CHARS:
        marker = "\n... truncated to the packet's ~1500-token (6000-character) bound."
        cut = text.rfind("\n", 0, MAX_PACKET_CHARS - len(marker))
        text = (text[:cut] if cut > 0 else text[: MAX_PACKET_CHARS - len(marker)]) + marker
    return text


def compile_task_packet(
    root: Path,
    *,
    plan_digest: str,
    plan_text: str,
    task: Mapping[str, Any],
    consult: Sequence[str] = FULL_CONSULT,
    tool_runner: Any = None,
    timeout_s: float | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    """Compile one task's packet, read-only. Never raises; an unavailable corpus is a status.

    ``partial`` marks a packet compiled with a reduced ``consult`` (a hook's Memex-only budget,
    never Graft or MemQ) so a reader knows it is not the full packet a plan `accept`, a
    `bearhug-work start`, or `bearhug-work grounding --refresh` would produce.
    """

    digest = corpus_digest(root)
    scoped_text = _task_scoped_text(plan_text, task)
    try:
        kwargs: dict[str, Any] = {
            "consult": consult,
            "exclude_paths": read_project_excludes(root),
        }
        if tool_runner is not None:
            kwargs["tool_runner"] = tool_runner
        if timeout_s is not None:
            kwargs["timeout_s"] = timeout_s
        grounding = compile_grounding(root, plan_text=scoped_text, tasks=[task], **kwargs)
    except GroundingError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_digest": plan_digest,
            "task_id": task["id"],
            "corpus_digest": digest,
            "quick_signature": quick_signature(root),
            "status": "unavailable",
            "partial": partial,
            "bound_ids": [],
            "reason": str(exc),
            "text": f"Grounding packet for {task['id']}: unavailable ({exc}).",
        }
    text = _render_text(root, task, grounding)
    if partial:
        text += (
            "\n(memex-only; run `bearhug-work grounding "
            f"{task['id']} --refresh` for the full packet)"
        )
    bound_ids = sorted(
        binding["binding_id"][len("binding.decision.") :]
        for binding in grounding.bindings
        if binding["binding_id"].startswith("binding.decision.")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_digest": plan_digest,
        "task_id": task["id"],
        "corpus_digest": digest,
        "quick_signature": quick_signature(root),
        "status": "ok",
        "partial": partial,
        "bound_ids": bound_ids,
        "grounding_sha256": grounding.sha256,
        "text": text,
    }


def _write(root: Path, plan_digest: str, task_id: str, packet: dict[str, Any]) -> None:
    path = _packet_path(root, plan_digest, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(packet, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def write_packet(
    root: Path,
    *,
    plan_digest: str,
    plan_text: str,
    task: Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    packet = compile_task_packet(
        root, plan_digest=plan_digest, plan_text=plan_text, task=task, **kwargs
    )
    _write(root, plan_digest, task["id"], packet)
    return packet


def compile_all(
    root: Path,
    *,
    plan_digest: str,
    plan_text: str,
    tasks: Sequence[Mapping[str, Any]],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Compile every task's packet for a newly accepted plan, replacing any earlier packets.

    Called at ``accept``, after its state lock is released: the whole ``.bearhug/packets/``
    directory is wiped first, so a packet left over from a retired plan digest (or a task ID that
    no longer exists) never lingers.
    """

    base = root / PACKETS_DIR
    if base.is_dir():
        shutil.rmtree(base)
    return [
        write_packet(root, plan_digest=plan_digest, plan_text=plan_text, task=task, **kwargs)
        for task in tasks
    ]


def discard(root: Path, plan_digest: str) -> None:
    """Remove one plan digest's packets, if present. Used to clean up a superseded write."""

    directory = root / PACKETS_DIR / plan_digest
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)


def read_packet(root: Path, plan_digest: str, task_id: str) -> dict[str, Any] | None:
    path = _packet_path(root, plan_digest, task_id)
    if not path.is_file():
        return None
    try:
        packet = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return packet if isinstance(packet, dict) else None


def _carry_over_pointers(previous_text: str) -> str:
    """Pull a previous full packet's "Pointers:" block out verbatim, marked possibly stale.

    Used when a hook-time Memex-only recompile displaces a stored full packet whose bound
    decisions actually changed: the new packet has no architecture/Graft/MemQ pointers of its
    own (a hook never consults those tools), so the old ones are carried over rather than simply
    dropped, with a note that they may no longer match the packet's now-different bindings.
    """

    marker = "\nPointers:\n"
    start = previous_text.find(marker)
    if start == -1:
        return ""
    block = previous_text[start:]
    footer = block.find("\n(memex-only;")
    if footer != -1:
        block = block[:footer]
    return "\n(carried over from a previous full compile; may be stale)" + block


def get_or_refresh(
    root: Path,
    *,
    plan_digest: str,
    plan_text: str,
    task: Mapping[str, Any],
    force: bool = False,
    partial: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return the task's current packet, recompiling it only if the corpus digest moved on.

    This is the one entry point every caller outside ``accept`` should use: a task starting (via
    ``bearhug-work start`` or a native hook observing the task go ``in_progress``), a session
    asking to inject the active task's packet, or the ``grounding`` CLI operation.

    The cheap ``quick_signature`` stat check runs first: when it matches what the stored packet
    was last confirmed against, nothing on disk has moved and the packet is returned unread
    beyond that. Only a *changed* quick signature triggers reading and hashing every decision
    file's content (``corpus_digest``) to tell an actual content/state change from a touch,
    rename or same-content rewrite.

    A stored packet that is ``partial`` (a Memex-only compile, from `accept` or a hook) is never
    treated as current by a caller asking for the full packet (``partial=False`` here, the
    default): it is recompiled in full even though the corpus itself has not moved, since the
    stored packet never consulted architecture, Graft or MemQ at all. A hook asking for its own
    reduced budget (``partial=True``) is unaffected by this and still takes the cheap paths above.
    """

    existing = read_packet(root, plan_digest, task["id"])
    needs_upgrade = bool(existing) and existing.get("partial") and not partial
    if force or existing is None or needs_upgrade:
        return write_packet(
            root, plan_digest=plan_digest, plan_text=plan_text, task=task, partial=partial, **kwargs
        )
    current_quick = quick_signature(root)
    if existing.get("quick_signature") == current_quick:
        return existing
    current_digest = corpus_digest(root)
    if existing.get("corpus_digest") == current_digest:
        # Something in the directory listing moved (a touch, a rename) but no decision's content
        # or state changed. Record the new quick signature so the next call takes the cheap path
        # again, without recompiling or even re-rendering the packet.
        refreshed = dict(existing, quick_signature=current_quick)
        _write(root, plan_digest, task["id"], refreshed)
        return refreshed
    if partial and existing.get("partial") is False:
        # The corpus genuinely changed and this call is a hook's own reduced-budget recompile,
        # but the stored packet is a full one. A blind downgrade would both lose its
        # architecture/Graft/MemQ pointers and, if the packet's own rendered text merely gains
        # the "(memex-only; ...)" footer, cause a spurious re-injection even when nothing the
        # operator cares about changed. Probe with the same reduced consult first: if the bound
        # decisions come out identical, keep the stored full packet exactly as is (only its
        # bookkeeping fields move on); otherwise carry the old pointers over, marked stale.
        probe = compile_task_packet(
            root, plan_digest=plan_digest, plan_text=plan_text, task=task, partial=True, **kwargs
        )
        if probe.get("bound_ids") == existing.get("bound_ids"):
            refreshed = dict(existing, corpus_digest=current_digest, quick_signature=current_quick)
            _write(root, plan_digest, task["id"], refreshed)
            return refreshed
        carried = _carry_over_pointers(existing.get("text", ""))
        if carried:
            probe = dict(probe, text=probe["text"] + carried)
        _write(root, plan_digest, task["id"], probe)
        return probe
    return write_packet(
        root, plan_digest=plan_digest, plan_text=plan_text, task=task, partial=partial, **kwargs
    )


__all__ = [
    "PACKETS_DIR",
    "MAX_PACKET_CHARS",
    "HOOK_CONSULT",
    "FULL_CONSULT",
    "HOOK_TIMEOUT_S",
    "corpus_digest",
    "quick_signature",
    "compile_task_packet",
    "write_packet",
    "compile_all",
    "discard",
    "read_packet",
    "get_or_refresh",
]
