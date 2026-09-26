"""P02 — the thin Stop adapter and the settings migration, generated from D02's ruling.

The candidate registers THREE Stop commands. graft and memex are retained byte-identically, and
the coordinator is the only one that can decide. Every non-Stop registration is preserved exactly:
this promotion is about Stop authority, and touching PreToolUse or PostToolUse would be scope the
ruling never granted.

**No timeout is invented.** The Round-7 return measured 319 ms on the largest available transcript
and reports that Barracuda rejected a timeout; Bear Hug has not read that decision record. The
coordinator therefore remains unbounded, matching `task-durability.py` and `review-gate.py`.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from bearhug.runtime_package import PROMOTED_INSTALL_PATH, RUNTIME_SOURCE


def _coordinator_error_remediation() -> str:
    """The ruled remediation, read from the runtime's own source.

    Read rather than retyped: the adapter needs a LITERAL copy — the case it exists for is the one
    where importing the runtime is what failed — and a literal typed here would be a second
    authority that could drift from the table the runtime quotes. A lab test asserts the rendered
    adapter contains this string, so the copy is pinned in both directions.
    """
    import ast

    source = (RUNTIME_SOURCE / "coordinator.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "COORDINATOR_ERROR_REMEDIATION" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("the runtime must declare COORDINATOR_ERROR_REMEDIATION")


ADAPTER_FILENAME = "stop-coordinator.py"

#: The command Barracuda would register. Quoted exactly as the captured settings quote theirs.
ADAPTER_COMMAND = f'python3 "$CLAUDE_PROJECT_DIR"/scripts/hooks/{ADAPTER_FILENAME}'

#: Recorded on the artifact, not left to a reader's memory.
MIGRATION_LIMITS = (
    "The candidate is generated from the SNAPSHOT's settings.json. The Round-7 Barracuda return "
    "reports that the live settings matched and that only hooks.Stop changed; Bear Hug has not "
    "read the live file, so the replacement session must reconfirm before replacement.",
    "CLOSED 2026-08-31: settings.local.json and the ambient ~/.claude layer WERE read (the gap was "
    "recorded by R04, not D01), from the "
    "snapshot that already captured both. Neither carries a `hooks` key, so the seven commands "
    "in settings.json are the complete Stop population and this migration is not incomplete. A "
    "Round-7 Barracuda return reports the LIVE files matched; the replacement session should "
    "reconfirm before replacement because the snapshot remains dated 2026-08-29.",
    "No timeout is set on the coordinator registration. H04 measured 319 ms on the largest "
    "available transcript in the Round-7 return, and the Barracuda decision reportedly rejected "
    "a timeout; Bear Hug has not read that record.",
    "D08 (2026-09-01): telemetry now lands at <repo>/.bearhug/telemetry/v1/. The host's "
    ".gitignore must list `.bearhug/` (patches/gitignore-candidate.txt carries the line; the "
    "verifier measures it). The 45 records already under ~/.claude/telemetry/bearhug/v1/<project> "
    "are left in place; moving them is a Barracuda-owned step.",
    "graft and memex are retained per D02. Memex is verified from the snapshot; the Round-7 "
    "return reports graft proven by reading its delegate, which Bear Hug has not inspected.",
)


def render_adapter() -> str:
    """The adapter: path insert, import, call, decide on its own failure, return.

    Logic in an adapter is logic outside the runtime, where none of the 1200-odd tests reach it.
    It is still thin — but "thin" cannot mean "has no policy about itself", which is what round 4
    rejected. The generated file previously ended:

        try:
            main()
        except Exception:
            sys.exit(0)

    Exit 0 with no stdout is the Stop protocol's PASS, so every exception that escaped the
    runtime's per-evaluator handling became a silent pass — the import of any vendored file, and
    the four dependency constructions. The three fail-CLOSED gates were therefore fail-closed
    inside a component that is not the one `settings.json` registers.

    Deleting the handler is not a fix: a hook that raises to the harness is also non-blocking, so
    it trades one silent pass for another. The adapter decides instead, in the only two ways D04
    rules:

      * the coordinator's INPUT-parse failure is FAIL-OPEN — a block there tells the model nothing
        it could act on, since it did not write the event;
      * everything else is the coordinator's own fault and FAIL-CLOSED, rendered with the ruled
        remediation that names rollback and names the coordinator, not a gate.

    `main` returns rather than exits, so the bytes are computed inside the guard and written once
    outside it; a partial write cannot leave half a decision on stdout.
    """
    return f'''#!/usr/bin/env python3
"""Stop coordinator adapter — generated by bear-hug P02. Do not edit by hand.

Thin by design: insert the vendored runtime on the path, hand it the event, write what it
returns. All gate logic lives in {PROMOTED_INSTALL_PATH}/, where it is tested.

The one policy that lives HERE is what to do when this file itself cannot reach the runtime,
because nothing inside the runtime can decide that.
"""

import json
import os
import sys

#: Copied LITERALLY from {PROMOTED_INSTALL_PATH}/coordinator.py, not imported: the condition this
#: is needed for is the one where importing the runtime is what failed. bear-hug reads the constant
#: out of the runtime source when generating this file, and a lab test asserts the copies agree.
COORDINATOR_ERROR_REMEDIATION = {_coordinator_error_remediation()!r}


def _fault(exception_type):
    """The bytes this adapter writes when IT is the component that failed.

    Carries the exception TYPE and never its message: a message can hold a path, a command or a
    fragment of source, and this string is shown to the model.
    """
    reason = COORDINATOR_ERROR_REMEDIATION + (
        "\\n\\nDetail: the Stop coordinator could not be run: " + exception_type
    )
    return json.dumps(
        {{"decision": "block", "reason": reason}}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def main(payload):
    # The adapter is installed at <repo>/scripts/hooks/stop-coordinator.py.  Its own location is the
    # git-root fallback stamp.sh obtains with `git rev-parse --show-toplevel`, without depending on
    # the process cwd (which may be a subdirectory) or spawning a command before the runtime loads.
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    sys.path.insert(0, os.path.join(root, "scripts", "hooks"))

    from _bearhug import coordinator
    from _bearhug.readers import (
        board_for, repo_for, tasks_for, transcript_lines_for, transcript_readable_for,
    )

    # Both observability roots supplied EXPLICITLY. The runtime's own defaults resolve to the
    # same places; passing them makes the adapter's docstring claim ("which the adapter supplies")
    # true rather than aspirational, and puts the one line a reader has to check about where
    # telemetry lands in the file they are already reading.
    from _bearhug.telemetry_store import default_root as telemetry_root

    return coordinator.run(payload, dependencies={{
        "tasks": tasks_for(payload),
        "repo": repo_for(root),
        "transcript_lines": transcript_lines_for(payload),
        "transcript_readable": transcript_readable_for(payload),
        "board": board_for(root),
    }}, stamp_root=root, telemetry_root=telemetry_root(root))


if __name__ == "__main__":
    try:
        event = json.load(sys.stdin)
    except BaseException:  # noqa: BLE001 — D04 rules the INPUT-parse failure FAIL-OPEN
        sys.exit(0)

    try:
        rendered = main(event)
        out, code = rendered.stdout, rendered.exit_code
    except BaseException as exc:  # noqa: BLE001 — the adapter's own fault, ruled fail-closed
        # BaseException, not Exception: a MemoryError or an interrupted Stop hook did not verify
        # this turn either, and `except Exception` is exactly the width that let round 4's defect
        # through one layer down.
        out, code = _fault(type(exc).__name__), 0

    if out:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.write(b"\\n")
    sys.exit(code)
'''


def build_candidate_settings(legacy: dict[str, Any]) -> dict[str, Any]:
    """The settings Barracuda would install. A deep copy: the snapshot is evidence, not a draft."""
    candidate = copy.deepcopy(legacy)
    stop_groups = candidate["hooks"]["Stop"]

    retained = [
        hook
        for group in stop_groups
        for hook in group["hooks"]
        if "graft-hooks.cjs" in hook.get("command", "")
        or "memex-hook.sh" in hook.get("command", "")
    ]
    candidate["hooks"]["Stop"] = [
        {"hooks": retained},
        # No timeout key: see MIGRATION_LIMITS.
        {"hooks": [{"type": "command", "command": ADAPTER_COMMAND,
                    "statusMessage": "bear-hug Stop coordinator…"}]},
    ]
    return candidate


def build_rollback_settings(_current: dict[str, Any]) -> dict[str, Any]:
    """The prior seven Stop commands, restored from the snapshot rather than reconstructed.

    Deliberately ignores whatever it is handed: a rollback derived from the CURRENT settings would
    reproduce whatever the candidate did to them, which is not a rollback.
    """
    from bearhug.hooks.audit import load_settings
    from bearhug.paths import REPO_ROOT

    return load_settings(REPO_ROOT / "snapshots" / "2026-08-29")


def render_settings(settings: dict[str, Any]) -> str:
    return json.dumps(settings, indent=2) + "\n"
