"""Native task-list projections and observed reconciliation for project work.

Like the existing board-restore hook, a command hook can return exact tool calls but
cannot invoke TaskCreate or update_plan itself. Observe the native result before
claiming parity. Provider lists remain projections of the explicitly selected work authority,
never a second board.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

if __package__:
    from bearhug import project_work as work
else:
    import bearhug_work as work


def _file(root: Path, provider: str, session_id: str) -> Path:
    if provider not in {"claude", "codex"} or not session_id or len(session_id) > 512:
        raise work.WorkError("Supply an exact provider and session identity")
    key = hashlib.sha256(session_id.encode()).hexdigest()
    return work._path(root, f".bearhug/native-work/{provider}/{key}.json")


def _load(root: Path, provider: str, session_id: str) -> dict[str, Any]:
    path = _file(root, provider, session_id)
    if path.exists():
        result = json.loads(path.read_text())
        if result.get("session_id") != session_id or result.get("provider") != provider:
            raise work.WorkError("Native observation identity mismatch")
        return result
    return {
        "provider": provider,
        "session_id": session_id,
        "rows": [],
        "reason": "Native task list has not been observed",
        "observed_at": "",
        "last_event": "",
    }


def _front(state: dict[str, Any], provider: str, session_id: str) -> list[dict[str, Any]]:
    tasks = state["tasks"]
    done = {task["id"] for task in tasks if task["status"] == "completed"}
    mine = [
        task
        for task in tasks
        if state.get("execution_authority") == "campaign"
        or (task["session_id"] == session_id and task["provider"] == provider)
    ]
    active = [task for task in mine if task["status"] in {"in_progress", "blocked"}]
    ready = [
        task
        for task in tasks
        if (
            task["status"] == "pending"
            or (
                state.get("authority_mode") == "project_board"
                and task["status"] == "in_progress"
                and task.get("owner_state") == "interrupted"
            )
        )
        and all(dep in done for dep in task["depends_on"])
        and task not in active
    ]
    # Preserve completion updates for this session; restore only the next three open rows.
    return [task for task in mine if task["status"] == "completed"][-3:] + (active + ready)[:3]


def desired(state: dict[str, Any], provider: str, session_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": task["id"],
            "board_row": task["board_row"],
            "title": task["title"],
            "status": "pending" if task["status"] == "blocked" else task["status"],
            "done_when": task["done_when"],
            "plan_sha256": state["plan"]["sha256"],
        }
        for task in _front(state, provider, session_id)
    ]


def _codex_plan_configuration(root: Path) -> dict[str, Any]:
    """Project intent only: user settings and command-line overrides may differ."""
    try:
        path = work._path(root, ".codex/config.toml")
        config = tomllib.loads(path.read_text()) if path.is_file() else {}
        enabled = config.get("tools", {}).get("update_plan", {}).get("enabled")
        if enabled is True:
            return {
                "status": "enabled",
                "reason": "Project opts into update_plan; calls still need observation.",
            }
        if enabled is False:
            return {
                "status": "disabled",
                "reason": "Project explicitly disables update_plan; setup preserves this choice. "
                "User settings and launch overrides are not inspected.",
            }
        return {
            "status": "not_configured",
            "reason": (
                "Project config does not enable update_plan. Codex >=0.152 defaults it off; "
                "set tools.update_plan.enabled = true and start a new session. "
                "User settings and launch overrides are not inspected."
            ),
        }
    except (OSError, ValueError, AttributeError) as exc:
        return {"status": "unknown", "reason": f"Cannot inspect project plan-tool setting: {exc}"}


def _bare_counter(root: Path, state: dict[str, Any]) -> bool:
    """Whether this project's tasks carry Bear Hug's own counter as board_row, with no
    declared reference to a real row of an imported board.

    True only on the merged shape (a v2 import owns the primary path; managed state lives at
    the fallback) with no `Board row: N` declared on the accepted plan. `accept` now refuses an
    undeclared plan on this shape outright, so this is a defensive backstop for a fallback state
    that predates that refusal or was written by hand -- native pushes must never claim a
    counter row is a real one either way.
    """
    return work._load_workv2(root) is not None and not state["plan"].get("board_row")


def projection(root: Path, state: dict[str, Any], provider: str, session_id: str) -> dict[str, Any]:
    native = _load(root, provider, session_id)
    expected = desired(state, provider, session_id)
    observed = {row["id"]: row for row in native["rows"]}
    mismatches = [
        row["id"]
        for row in expected
        if row["id"] not in observed
        or any(observed[row["id"]].get(k) != row[k] for k in ("status", "plan_sha256"))
    ]
    known = {task["id"] for task in state["tasks"]}
    extras = [row["id"] for row in native["rows"] if row["id"] not in known]
    result = {
        **native,
        "status": "pending" if mismatches or extras or not native.get("list_observed") else "match",
        "missing_or_changed": mismatches,
        "extra": extras,
        "tool_calls": [],
    }
    if provider == "codex":
        result["configuration"] = _codex_plan_configuration(root)
        result["display"] = {
            "status": "unverified",
            "reason": "Native tool results do not confirm that the Codex CLI rendered a checklist.",
        }
    if native.get("unavailable"):
        result.update(status="unavailable")
        return result
    if (
        result["status"] == "match"
        and len(observed) == len(native["rows"])
        and str(native.get("conflict") or "").startswith(
            "provisioning failed after custody publication"
        )
    ):
        # The saved launch error is historical once the exact native rows match current work.
        result["conflict"] = ""
    elif native.get("conflict"):
        result.update(status="conflict")
    if result["status"] == "match":
        result["reason"] = "Native task IDs, accepted plan and states match the project board"
        return result
    result["reason"] = (
        native.get("conflict") or "Push the board's active front to the native task list"
    )
    if provider == "codex":
        if not native.get("list_observed") and result["configuration"]["status"] != "enabled":
            result["reason"] += ". " + result["configuration"]["reason"]
        result["tool_calls"] = [
            {
                "tool": "update_plan",
                "arguments": {
                    "explanation": "Mirror the accepted Bear Hug board.",
                    "plan": native.get("foreign_steps", [])
                    + [
                        {
                            "step": f"[BH:{row['plan_sha256']}:{row['id']}] {row['title']}",
                            "status": row["status"],
                        }
                        for row in expected
                    ],
                },
            }
        ]
    else:
        bare_counter = _bare_counter(root, state)
        for row in expected:
            metadata = {
                "bearhug_task_id": row["id"],
                "bearhug_project_sha256": hashlib.sha256(str(root).encode()).hexdigest(),
                "board_row": str(row["board_row"]),
                "authority": state["plan"]["path"],
                "bearhug_plan_sha256": row["plan_sha256"],
            }
            if bare_counter:
                # Bear Hug's own counter is never a real board row: never claim it is one on
                # the merged shape with no declared row. Deleted rather than never added, so
                # the key order (and therefore the serialized bytes) of every other metadata
                # field is unchanged from before this state existed.
                del metadata["board_row"]
            if state["plan"].get("board_row"):
                subject = f"#{row['board_row']} · {row['id']} — {row['title']}"
            elif bare_counter:
                subject = f"{row['id']} — {row['title']}"
            else:
                subject = f"#{row['board_row']} · UNPLACED — {row['title']}"
            previous = observed.get(row["id"])
            if previous and previous.get("native_id"):
                if row["id"] in mismatches:
                    result["tool_calls"].append(
                        {
                            "tool": "TaskUpdate",
                            "arguments": {
                                "taskId": previous["native_id"],
                                "status": row["status"],
                                "metadata": metadata,
                            },
                        }
                    )
            else:
                result["tool_calls"].append(
                    {
                        "tool": "TaskCreate",
                        "arguments": {
                            "subject": subject,
                            "description": (
                                f"{state['plan']['path']} · {row['id']}\n"
                                f"Done when: {row['done_when']}"
                            ),
                            "metadata": metadata,
                        },
                    }
                )
    return result


def views(root: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    directory = work._path(root, ".bearhug/native-work")
    for path in sorted(directory.glob("*/*.json")):
        path = work._path(root, path.relative_to(root).as_posix())
        native = json.loads(path.read_text())
        view = projection(root, state, native["provider"], native["session_id"])
        view.pop("tool_calls", None)
        result.append(view)
    return sorted(result, key=lambda item: item["observed_at"], reverse=True)[:20]


def _claude_rows(
    root: Path,
    state: dict[str, Any],
    session_id: str,
) -> list[dict[str, Any]] | None:
    # Same exact-session rule as providers/claude_work.py: no newest-session selection,
    # no provider-store writes. The provider may explicitly select a shared task-list ID.
    list_id = os.environ.get("CLAUDE_CODE_TASK_LIST_ID") or session_id
    if not re.fullmatch(r"[A-Za-z0-9_-]+", list_id):
        raise work.WorkError("Invalid Claude task-list identity")
    directory = (
        Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "tasks" / list_id
    )
    if directory.is_symlink():
        raise work.WorkError("Claude task store is symlinked")
    if not directory.exists():
        return None
    rows = []
    bare_counter = _bare_counter(root, state)
    known = {task["id"]: task for task in state["tasks"]}
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise work.WorkError("Claude task record is not a bounded regular file")
        raw = path.read_bytes()
        task = json.loads(raw)
        if str(task.get("id")) != path.stem:
            raise work.WorkError("Claude native task ID does not match its record filename")
        metadata = task.get("metadata") or {}
        ident = metadata.get("bearhug_task_id")
        if metadata.get("bearhug_project_sha256") != hashlib.sha256(str(root).encode()).hexdigest():
            continue
        if ident not in known or metadata.get("authority") != state["plan"]["path"]:
            continue  # Foreign provider tasks are never adopted or overwritten.
        if bare_counter:
            # Bear Hug never pushes a board_row here (see projection() above); a session that
            # writes one anyway -- even one that happens to equal Bear Hug's own counter -- is
            # exactly the false identity this state refuses to recognize, not a value to accept.
            if metadata.get("board_row") is not None:
                raise work.WorkError("Claude board-row identity mismatch")
        elif str(metadata.get("board_row")) != str(known[ident]["board_row"]):
            raise work.WorkError("Claude board-row identity mismatch")
        rows.append(
            {
                "id": ident,
                "native_id": task["id"],
                "status": task["status"],
                "plan_sha256": metadata.get("bearhug_plan_sha256", ""),
                "evidence": metadata.get("bearhug_evidence", ""),
                "record_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return rows


def _codex_plan(
    root: Path, payload: dict[str, Any], native: dict[str, Any]
) -> dict[str, Any] | None:
    """Read successful update_plan calls from this exact session when no tool hook carries them."""
    supplied = payload.get("transcript_path")
    if not supplied:
        return None
    path = Path(supplied)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise work.WorkError("Codex transcript is not an explicit regular file")
    with path.open("rb") as stream:
        first = json.loads(stream.readline())
        meta = first.get("payload", {})
        if (
            first.get("type") != "session_meta"
            or meta.get("id") != payload["session_id"]
            or Path(meta.get("cwd", "")).resolve() != root
        ):
            raise work.WorkError("Codex transcript does not match this project and session")
        identity = hashlib.sha256(str(path).encode()).hexdigest()
        offset = (
            native.get("transcript_offset", 0) if native.get("transcript_source") == identity else 0
        )
        if offset > path.stat().st_size:
            offset = 0
        stream.seek(offset)
        calls = native.get("pending_plan_calls", {})
        result = None
        read = 0
        while read < 2 * 1024 * 1024:
            start = stream.tell()
            line = stream.readline(2 * 1024 * 1024)
            if not line or not line.endswith(b"\n"):
                stream.seek(start)
                break
            read += len(line)
            record = json.loads(line)
            item = record.get("payload", {})
            if record.get("type") != "response_item":
                continue
            if (
                item.get("type") == "function_call"
                and item.get("name", "").split(".")[-1] == "update_plan"
            ):
                arguments = item.get("arguments", "{}")
                calls[item["call_id"]] = (
                    json.loads(arguments) if isinstance(arguments, str) else arguments
                )
            elif item.get("type") == "function_call_output" and item.get("call_id") in calls:
                arguments = calls.pop(item["call_id"])
                if str(item.get("output", "")).strip() == "Plan updated":
                    result = arguments
        native.update(
            transcript_source=identity,
            transcript_offset=stream.tell(),
            pending_plan_calls=dict(list(calls.items())[-20:]),
        )
        return result


def event(
    root: Path, payload: dict[str, Any], provider: str, *, include_campaign: bool = True
) -> None:
    session_id, name = payload["session_id"], payload["hook_event_name"]
    state = (
        work._execution_state(root)
        if include_campaign
        else work._execution_state(root, campaign={})
    )
    native = _load(root, provider, session_id)
    native.update(observed_at=work._now(), last_event=name)
    tool = payload.get("tool_name", "").split(".")[-1]
    rows = None
    inputs = _codex_plan(root, payload, native) if provider == "codex" else None
    if provider == "codex" and name == "PostToolUse" and tool == "update_plan":
        response = payload.get("tool_response")
        if not (isinstance(response, dict) and (response.get("error") or response.get("isError"))):
            inputs = payload.get("tool_input", {})
            if isinstance(inputs, str):
                inputs = json.loads(inputs)
    if inputs is not None:
        rows, foreign = [], []
        for step in inputs.get("plan", []):
            if step.get("status") not in {"pending", "in_progress", "completed"}:
                raise work.WorkError("Invalid native Codex plan status")
            match = re.match(
                r"^\[BH:([0-9a-f]{64}):([A-Za-z][A-Za-z0-9_-]*)\] ", step.get("step", "")
            )
            if match:
                rows.append(
                    {
                        "id": match[2],
                        "status": step["status"],
                        "plan_sha256": match[1],
                        "evidence": inputs.get("explanation", ""),
                    }
                )
            else:
                foreign.append({"step": step["step"], "status": step["status"]})
        native["foreign_steps"] = foreign
    elif (
        state
        and provider == "claude"
        and name in {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}
    ):
        rows = _claude_rows(root, state, session_id)
    if rows is not None and state:
        if len({row["id"] for row in rows}) != len(rows):
            native["conflict"] = "Duplicate native entries for one Bear Hug task"
        else:
            native.update(rows=rows, conflict="", unavailable=False, list_observed=True)
            # Native changes are proposals: run the same dependency/ownership transitions.
            by_id = {task["id"]: task for task in state["tasks"]}
            for row in rows:
                task = by_id.get(row["id"])
                if not task or row["plan_sha256"] != state["plan"]["sha256"]:
                    native["conflict"] = "Native task names a different accepted plan or unknown ID"
                    continue
                target = row["status"]
                reclaim_interrupted = False
                if (
                    state.get("execution_authority") != "campaign"
                    and target == "in_progress"
                    and task["status"] == "in_progress"
                    and (task["session_id"] != session_id or task["provider"] != provider)
                ):
                    owner_active = True
                    if state.get("authority_mode") == "project_board":
                        owner_active = (
                            work._project_board_owner(root, str(task["board_row"]))["state"]
                            == "active"
                        )
                    if owner_active:
                        native["conflict"] = "Native list claims another session's active task"
                        continue
                    reclaim_interrupted = True
                if (
                    target == task["status"]
                    or (target == "pending" and task["status"] == "blocked")
                ) and not reclaim_interrupted:
                    continue
                action = {"in_progress": "start", "completed": "complete"}.get(target)
                if not action:
                    native["conflict"] = (
                        "Native reset conflicts with durable progress; restore from board"
                    )
                    continue
                try:
                    # This runs inside `native_hook`'s own reconciliation, under the hook
                    # timeout: a packet recompile here must never launch Graft or MemQ. See
                    # `work.transition`'s `packet_consult`/`packet_partial`.
                    packets = work._packets(root)
                    state = work.transition(
                        root,
                        row["id"],
                        action,
                        session_id=session_id,
                        provider=provider,
                        evidence=row.get("evidence", ""),
                        packet_consult=packets.HOOK_CONSULT,
                        packet_timeout_s=packets.HOOK_TIMEOUT_S,
                        packet_partial=True,
                        **({} if include_campaign else {"include_campaign": False}),
                    )
                except work.WorkError as exc:
                    native["conflict"] = str(exc)
    with work._locked(root):
        work._write_json(_file(root, provider, session_id), native)
    work.observe(
        root,
        name,
        session_id,
        provider=provider,
        **({} if include_campaign else {"include_campaign": False}),
    )


NO_TASK_TOOL_NOTICE = (
    "Native task tools are unavailable in this client; the board and scripts/bin/bearhug-work "
    "are authoritative."
)

_TASK_TOOL = "TaskCreate"
_TOOLSEARCH_MISS = "No matching deferred tools found"


def _transcript_records(transcript_path: str | None) -> list[Any]:
    if not transcript_path or not os.path.isfile(transcript_path):
        return []
    records: list[Any] = []
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return records


def _record_tool_uses(record: Any):
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return
    content = (record.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool_use":
            yield part


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") if isinstance(part, dict) else "" for part in content
        )
    return ""


def task_tool_absent(transcript_path: str | None) -> bool:
    """Whether this session's transcript shows the native task tool is unavailable.

    Same ToolSearch-miss/precedence structure the sealed task-durability evaluator's
    `_task_tool_absent` reads (`runtime/bearhug_runtime/evaluators/task_durability.py`, read-only
    here): a `TaskCreate` tool_use anywhere means the tool was usable, so absence is moot.
    Otherwise a `ToolSearch` miss counts only when correlated by `tool_use_id` to a `ToolSearch`
    call whose query names the task tool.

    The withdrawal-notice signal is ONE RULED DIFFERENCE from that sealed evaluator, not a mirror
    of it. The sealed gate accepts plain-string content on ANY non-assistant record -- its own
    test suite documents this as accepting `type` in `{"user", "system", "summary", None}`
    (`tests/test_task_durability_evaluator.py::test_legacy_string_withdrawal_on_every_non_assistant_record_passes`),
    which means an ordinary user chat turn that merely quotes or asks about the exact notice
    wording (plausible, since this same feature is what puts that wording into transcripts at
    all) would silently and permanently disable native sync for the rest of the session. That
    gate is sealed and out of scope to change. This local, independent implementation instead
    requires `isMeta` truthy on the record, the same discriminator
    `runtime/bearhug_runtime/turns.py`'s `is_real_user_message`/`_is_settled_feedback` already use
    elsewhere in the sealed runtime to tell a harness-authored transcript entry from a genuine
    user prompt: Claude Code marks its own injected notices `isMeta: true` and does not set it on
    ordinary user turns, so a plain user message quoting the wording never qualifies.
    """
    records = _transcript_records(transcript_path)
    searched: set[str] = set()
    missed = False
    withdrawal = False
    for record in records:
        for part in _record_tool_uses(record):
            if part.get("name") == _TASK_TOOL:
                return False
            if part.get("name") == "ToolSearch":
                query = str((part.get("input") or {}).get("query") or "")
                if _TASK_TOOL.lower() in query.lower() or "task" in query.lower():
                    identifier = part.get("id")
                    if identifier:
                        searched.add(str(identifier))
        if isinstance(record, dict) and record.get("type") != "assistant":
            content = (record.get("message") or {}).get("content") or []
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "tool_result":
                        continue
                    if str(part.get("tool_use_id") or "") not in searched:
                        continue
                    if _TOOLSEARCH_MISS in _result_text(part.get("content")):
                        missed = True
            elif (
                record.get("isMeta") is True
                and isinstance(content, str)
                and "no longer available" in content
                and _TASK_TOOL in content
            ):
                withdrawal = True
    return missed or withdrawal


def context(
    root: Path,
    provider: str,
    session_id: str,
    *,
    include_campaign: bool = True,
    transcript_path: str | None = None,
) -> str:
    state = (
        work._execution_state(root)
        if include_campaign
        else work._execution_state(root, campaign={})
    )
    if not state:
        return ""
    if work._read_plan(root, state["plan"]["path"])[1] != state["plan"]["sha256"]:
        return "Native task synchronization paused until the changed plan is reviewed and accepted."
    if provider == "claude":
        native = _load(root, provider, session_id)
        if native.get("task_tool_absent"):
            # Recorded absent on an earlier turn of this same session; the notice already ran
            # once and native payloads stay off for the rest of the session.
            return ""
        if task_tool_absent(transcript_path):
            native["task_tool_absent"] = True
            with work._locked(root):
                work._write_json(_file(root, provider, session_id), native)
            return NO_TASK_TOOL_NOTICE
    sync = projection(root, state, provider, session_id)
    if sync["status"] == "match":
        return "Native task data matches the Bear Hug board. " + (
            "Codex checklist rendering is unverified; show the board summary in your reply."
            if provider == "codex"
            else ""
        )
    payload = json.dumps(sync["tool_calls"], ensure_ascii=False)
    if len(payload.encode()) > 5000:
        payload = (
            f"Read the full payload using scripts/bin/bearhug-work native-tasks "
            f"--provider {provider} --session {session_id}."
        )
    return (
        f"Native task list: {sync['status']} — {sync['reason']}. "
        "Push these exact native tool calls before implementation; keep unrelated provider tasks. "
        "Call update_plan directly when exposed as a native tool. "
        "Do not edit provider storage files. After native changes, hooks reconcile IDs, states, "
        "dependencies and session ownership. Completion requires check results in "
        "TaskUpdate metadata.bearhug_evidence (Claude) or update_plan explanation (Codex). "
        "If the native tool is unavailable, report that fact; do not claim synchronization.\n"
        + payload
    )
