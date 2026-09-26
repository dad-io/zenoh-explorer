#!/usr/bin/env python3
"""memex-pre-question-score.py -- score an AskUserQuestion against accepted Memex decisions
with the grounding engine's own selection, not raw word overlap.

Owner-supplied evidence (a real Go project run, 2026-09-25): the previous `memexlint -topic`
free-text search surfaced 11 records across 4 real questions, only 1 of them governing, on
single common words such as "config", "worktree", "mine" and "stale" -- no rarity weighting, no
minimum score. `bearhug.campaign.grounding` already scores terms this way for mode 3 onboarding
(rarity-weighted terms, a minimum score, an accumulated-lexical "strong" bar); this script reuses
that engine wholesale, with its current defaults, instead of re-implementing scoring in shell.

Thin I/O shell, matching the split `stop-coordinator.py`/`_bearhug` and
`forbidden-command-gate.py`/`forbidden_command_rules.py` already use in this same installed
layout: read the PreToolUse payload from stdin, print at most two "  <id>  <title>" lines (one
per decision that clears the grounding engine's own "strong" bar) to stdout, or nothing. All
scoring lives in `bearhug.campaign.grounding`, imported from the per-project immutable copy at
`<root>/.bearhug/lib` that every other installed Bear Hug command already imports from
(`bearhug_work.py`'s own `_INSTALLED_LIBRARY` resolution, reused here).

Hard time-bounded (`SIGALRM`, default 2 seconds -- a wide margin over the corpus sizes measured
for the other memex-hook.sh subcommands, see docs/OPERATING-MODES.md) and fail-open on ANY
failure: a missing library, an unreadable corpus, a bad payload, or the deadline all print
nothing and exit 0. `memex-hook.sh pre-question` treats a missing script, a non-zero exit and
empty output identically -- nothing to say -- so failing open here costs only lost advice, never
a stuck gate.

Also records one outcome observation in project telemetry -- advised or nothing, a match count,
a byte count, never the question text -- through the already-vendored
`bearhug_runtime.telemetry`/`telemetry_store` API at the same `.bearhug/lib`. That module already
ships a `record_kind: emitter_observation` shape for exactly this (D06); a sibling track added
its own `_record_context_injection` wrapper around the same API for SessionStart/UserPromptSubmit
context injections, naming the same fields (event, session id, a count, a byte count). That
wrapper lives in `project_work.py` on a later branch this one does not carry, so this file calls
the underlying `build_emitter_record`/`append` API directly instead of depending on it.
"""

from __future__ import annotations

import json
import signal
import sys
import time
import uuid
from pathlib import Path

MAX_MATCHES = 2
DEFAULT_TIME_BUDGET_S = 2.0
_EMITTER_ID = "memex-pre-question"


class _TimeBudgetExceeded(Exception):
    """Raised by the SIGALRM handler; never escapes `main`."""


def _alarm_handler(_signum: int, _frame: object) -> None:
    raise _TimeBudgetExceeded


def _time_budget_seconds() -> float:
    import os

    raw = os.environ.get("BEARHUG_MEMEX_QUESTION_SCORE_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIME_BUDGET_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIME_BUDGET_S
    return value if value > 0 else DEFAULT_TIME_BUDGET_S


def _test_delay_seconds() -> float:
    """Test-only seam: an artificial delay injected before scoring, so a test can force the
    SIGALRM deadline to fire deterministically without depending on real corpus size. Never set
    outside a test.
    """

    import os

    raw = os.environ.get("BEARHUG_MEMEX_QUESTION_SCORE_TEST_DELAY_S")
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, value)


def _question_texts(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    tool_input = payload.get("tool_input")
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list):
        return []
    texts: list[str] = []
    for item in questions:
        if not isinstance(item, dict):
            continue
        for key in ("question", "header"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value)
    return texts


def _library_path(root: Path) -> str:
    return str(root / ".bearhug" / "lib")


def _record_outcome(root: Path, *, session_id: str, event_name: str, match_count: int,
                     byte_count: int) -> None:
    """Best-effort telemetry. Never raises, never affects the advisory result, never carries
    question text -- only a count and a byte count, the same fields a sibling track's own
    context-injection observation uses.
    """

    try:
        library = _library_path(root)
        if library not in sys.path:
            sys.path.insert(0, library)
        from bearhug_runtime import telemetry, telemetry_store

        raw_input = json.dumps(
            {"session_id": session_id, "event_name": event_name, "emitter_id": _EMITTER_ID},
            sort_keys=True,
        ).encode("utf-8")
        record = telemetry.build_emitter_record(
            emitter_id=_EMITTER_ID,
            observation={
                "outcome": "advised" if match_count else "nothing",
                "match_count": match_count,
                "byte_count": byte_count,
            },
            event_id=uuid.uuid4().hex,
            session_id=session_id,
            event_name=event_name,
            raw_input=raw_input,
        )
        if record is None:
            return
        telemetry_store.append(record, root=telemetry_store.default_root(root))
    except Exception:  # noqa: BLE001 -- telemetry may never affect or slow the gate's own result
        return


def main() -> None:
    if len(sys.argv) < 2:
        return
    root = Path(sys.argv[1])

    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 -- malformed input is an internal fault, not a crash
        return

    texts = _question_texts(payload)
    if not texts:
        return

    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    event_name = payload.get("hook_event_name") if isinstance(payload, dict) else None
    session_id = session_id if isinstance(session_id, str) and session_id else "unknown"
    event_name = event_name if isinstance(event_name, str) and event_name else "PreToolUse"

    have_alarm = hasattr(signal, "SIGALRM")
    if have_alarm:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, _time_budget_seconds())
    try:
        delay = _test_delay_seconds()
        if delay:
            time.sleep(delay)
        library = _library_path(root)
        if library not in sys.path:
            sys.path.insert(0, library)
        from bearhug.campaign import grounding

        decisions = grounding.read_decisions(root)
        terms = grounding.extract_terms(texts)
        selection = grounding.select_decisions(decisions, terms)
    except Exception:  # noqa: BLE001 -- missing library, unreadable corpus, anything: fail open
        return
    finally:
        if have_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)

    matches = selection.matches[:MAX_MATCHES]
    lines = [f"  {decision.decision_id}  {decision.title}" for decision, _score, _hits in matches]
    text = "\n".join(lines)

    _record_outcome(
        root,
        session_id=session_id,
        event_name=event_name,
        match_count=len(matches),
        byte_count=len(text.encode("utf-8")),
    )

    if text:
        sys.stdout.write(text)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- this script must never raise into memex-hook.sh
        sys.exit(0)
    sys.exit(0)
