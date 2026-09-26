"""Pure evaluator port of the captured ``response-shape.py`` Stop gate.

Detection semantics stay mechanical-port compatible with snapshot ``2026-08-29@56581917``:
the last non-empty assistant text block is judged, fenced code and blockquotes do not contribute
ask lines, two ask lines block, and a long, ask-free reply blocks unless the most recent user
record requests a stop.  A line is the counting unit even if it contains two question marks.

**0135 / R07, ruled (Sam, 2026-09-03):** ``LONG_CHARS`` rises from 1,500 to 6,000. A turn-ending
message under 6,000 characters with no question now passes; the two-ask rule and the stop-request
exemption are unchanged. Gate 1.1.0 -> 1.2.0. The captured legacy hook keeps its own 1,500-char
threshold — this is a deliberate divergence from parity, not a mechanical port, and
`tests/test_response_shape_evaluator.py` states it explicitly rather than asserting agreement.

**Fixed 2026-09-15, gate 1.2.0 -> 1.3.0:** ``_prose_lines`` toggled a boolean on any line starting
with ` ``` `, which had three measured defects, all in the gate's own blind spot: a ``~~~`` fence
was invisible (only ` ``` ` toggled it), so asks inside relayed evidence were counted and the gate
fired on exactly the evidence it exists to protect; an UNTERMINATED fence swallowed the rest of the
message, so real asks counted as zero and the gate PASSED a message it should have blocked; and a
` ``` ` nested inside a ```` block closed the outer fence early. The reader now tracks the fence
marker's CHARACTER and LENGTH and closes only on the same character at the same-or-longer length,
matching CommonMark; an unterminated fence now ends at end-of-text — the lines inside it are still
dropped, but nothing after it can be lost, because there is no after. See
`tests/test_response_shape_evaluator.py` for the defect-reproducing cases.

**Fixed 2026-09-15 (Round 17), gate 1.3.0 -> 1.4.0:** the character/length tracker landed by the
1.3.0 fix above was strictly better than the boolean it replaced, but a review found it still
silently diverged from CommonMark in three cases, all still in the gate's own blind spot:

* **Indent.** the previous opener match let a marker indented 4+ spaces open a fence. CommonMark
  caps a fence opener's indentation at 0-3 spaces; 4 or more is an indented code block, not a
  fence. The opener match is now capped at 0-3 leading spaces.
* **Backtick info string.** A backtick fence's info string must not itself contain a backtick
  (CommonMark) — otherwise an isolated inline code span, or a line like a stray ` ```json``` `,
  reads as a fence toggle when it is ordinary text. Tilde fences carry no such restriction and may
  contain a backtick in their info string.
* **Closer info string.** A closing fence may carry nothing but trailing whitespace after its
  marker; a marker followed by non-whitespace (of either character) is not a valid closer and is
  fence CONTENT instead, same as a shorter or different-character run.

None of this touches the unterminated-fence-ends-at-EOF behaviour fixed in 1.3.0, which was already
correct. See `tests/test_response_shape_evaluator.py`'s D1/D2/D3 cases for the defect-reproducing
inputs, each measured against this module's own pre-Round-17 reader.

The architecture changes, deliberately: this function never prints, exits, stamps, or swallows an
exception into a silent pass.  It returns one typed result.  R13 will adapt an exception to an
``error`` result and apply D04's fail-open policy; doing that here would make the evaluator a
second failure-policy authority.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping

from ..results import EvaluatorResult, Evidence

GATE_ID = "response-shape"
GATE_VERSION = "1.4.0"
#: 0135/R07, ruled (Sam, 2026-09-03): 1,500 -> 6,000. A turn-ending message under this many
#: characters with no question passes; the two-ask rule and the stop-request exemption are
#: unchanged. The captured legacy hook's own threshold stays 1,500 — this is a deliberate
#: divergence, not a parity port.
LONG_CHARS = 6000

# Asking constructions from the captured gate. Deliberately narrow: generic question vocabulary
# over-triggered the predecessor, so this matches line shape rather than the word "question".
_ASK_PATTERNS = re.compile(
    r"(?:"
    r"want me to\b"
    r"|should i\b"
    r"|shall i\b"
    r"|do you want\b"
    r"|would you (?:like|rather|prefer)\b"
    r"|your call\b"
    r"|let me know\b"
    r"|which (?:one|of these|would)\b"
    r"|got it\?"
    r"|ack\b.*\?"
    r"|confirm\b[^.]*\?"
    r")",
    re.IGNORECASE,
)

_STOP_REQUEST = re.compile(
    r"(?:"
    r"\bstop (?:here|there|for now)\b"
    r"|\bhalt\b"
    r"|\bthat(?:'s| is) enough\b"
    r"|\bwe(?:'re| are) done\b"
    r"|\bend (?:the )?(?:session|turn|here)\b"
    r"|\bhold (?:off|here)\b"
    r"|\bno more (?:questions|cards)\b"
    r")",
    re.IGNORECASE,
)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _readable(path: object) -> bool:
    """Is there a file here to read? Checked WITHOUT opening it, so the check cannot raise."""
    import os

    return isinstance(path, str) and bool(path) and os.path.isfile(path)


def _last_user_asked_to_stop(transcript_path: object) -> bool:
    """Port the legacy last-user scan exactly, including its raw tool-result exclusion."""
    last = ""
    with open(transcript_path or "", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"user"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") != "user":
                continue
            content = (entry.get("message", {}) or {}).get("content")
            text = _content_text(content)
            # Tool results are user-typed records too. The raw substring check is legacy behavior;
            # R07 is a mechanical port and does not smuggle a turn-boundary policy change into it.
            if text.strip() and "tool_result" not in line:
                last = text
    return bool(_STOP_REQUEST.search(last))


def _turn_final_text(transcript_path: object) -> str:
    """Stream the transcript and return the last non-empty assistant text block."""
    last = ""
    with open(transcript_path or "", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") != "assistant":
                continue
            for block in (entry.get("message", {}) or {}).get("content", []) or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    last = block["text"]
    return last


#: A fence opener/closer candidate: up to 3 leading spaces (CommonMark caps a fence's own
#: indentation there; 4+ is an indented code block, not a fence), the marker run (` or ~, three or
#: more, captured so the CLOSER can be matched against it), and the rest of the line, captured
#: separately as the candidate's info string. Closes only on the SAME character at the SAME length
#: or longer -- so a ``` inside a ~~~ block is content, not a toggle.
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")


def _prose_lines(text: str) -> list[str]:
    """Drop fenced code and blockquotes before counting ask lines.

    A pasted command or a quoted agent report is not the assistant asking Sam
    something; counting them would make the gate fire on relaying evidence,
    which is the behaviour it exists to protect.

    The fence reader tracks the marker's CHARACTER and LENGTH rather than
    toggling a boolean on any line starting with ```, and applies three
    CommonMark rules a plain character/length tracker still misses (Round 17,
    2026-09-15):

      * a marker indented 4+ spaces does not open a fence -- it is an indented
        code block, a different CommonMark construct entirely;
      * a backtick fence's info string must not itself contain a backtick, so
        an isolated inline code span or a stray ```` ```json``` ```` is
        ordinary text, not a toggle (tilde fences carry no such restriction);
      * a CLOSING fence may carry nothing but trailing whitespace after its
        marker -- a same-char, same-or-longer run followed by non-whitespace
        is not a valid closer and is fence content instead.

    Measured defects of the plain character/length tracker this replaced, all
    present before 2026-09-15 and all in the gate's own blind spot:

      * a 4-space-indented marker opened a fence that CommonMark would read as
        an indented code block, so two real asks folded into it were hidden;
      * a backtick fence's info string containing a backtick (an inline code
        span, or a stray fence-shaped line) toggled the fence when it should
        not have, hiding or exposing asks depending on which side of it they
        fell on;
      * a would-be closer carrying an info string (e.g. a stray ` ```json `
        line inside an open fence) closed the fence early, exposing asks that
        CommonMark would still render as code.

    An unterminated fence still ends at end-of-text rather than running past
    it: the lines inside it are still dropped (they are evidence), but
    nothing AFTER it can be lost, because there is no after. That behaviour
    was already correct and Round 17 does not change it.
    """
    output: list[str] = []
    fence: tuple[str, int] | None = None
    for raw in text.split("\n"):
        stripped = raw.strip()
        candidate = _FENCE.match(raw)
        marker = candidate.group(1) if candidate else None
        info = candidate.group(2).strip() if candidate else ""
        if fence is None:
            # Only a 0-3-space-indented run can OPEN a fence. A backtick run's info string
            # must not itself carry a backtick -- otherwise the line reads as an inline code
            # span (or a stray fence-shaped line) rather than a fence opener. Tildes have no
            # such restriction.
            if candidate and not (marker[0] == "`" and "`" in info):
                fence = (marker[0], len(marker))
                continue
        # A same-char, same-or-longer run carrying an info string is NOT a valid closer --
        # it falls through and is dropped below as fence content, same as a shorter or
        # different-character run.
        elif (
            marker is not None
            and marker[0] == fence[0]
            and len(marker) >= fence[1]
            and info == ""
        ):
            fence = None
            continue
        # A shorter run, a different-character run, an unmatched line, or a rejected
        # opener/closer candidate is CONTENT (or, if no fence is open, ordinary prose) --
        # fall through and let the checks below decide.
        if fence is not None or stripped.startswith(">"):
            continue
        output.append(raw)
    return output


def _count_asks(lines: list[str]) -> int:
    """Count ask lines, not pattern hits."""
    count = 0
    for line in lines:
        stripped = line.strip()
        if stripped and (stripped.endswith("?") or _ASK_PATTERNS.search(stripped)):
            count += 1
    return count


def _duration_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000)


def _multi_ask_remediation(asks: int) -> str:
    return (
        f"decision 0135 — your reply carries {asks} separate asks. Sam answers the "
        f"last one and the rest are silently dropped, which is how questions end up "
        f"re-asked three turns later.\n\n"
        f"Re-send with ONE ask. Pick the single decision that blocks progress, give "
        f"it the context to decide from (what changes, why, what it affects), and "
        f"close on that one question. Park the others — say they are parked, or hold "
        f"them until this one is answered. Do not renumber them into a list and call "
        f"that one ask."
    )


def _no_engagement_remediation(chars: int) -> str:
    return (
        f"decision 0135 — {chars} chars with no engagement point. A large "
        f"uninterrupted block gives Sam nowhere to respond, so it reads as output "
        f"rather than conversation.\n\n"
        f"Re-send shorter, and end on one ack or one question so the turn comes back "
        f"to him."
    )


def evaluate_response_shape(event: Mapping[str, object]) -> EvaluatorResult:
    """Evaluate one coordinator event and return exactly one immutable result."""
    started = time.perf_counter()
    event_id = event.get("event_id")

    if event.get("stop_hook_active"):
        return EvaluatorResult.not_applicable(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="stop_hook_active",
            evidence=(Evidence("event_field", "stop_hook_active=true"),),
            duration_ms=_duration_ms(started),
        )

    # 1.1.0 (Round 13): the same guard the other three transcript-reading gates carry. The
    # captured gate raised on a missing transcript, and D04's fail-open read that crash as a
    # pass — measured on every one of 36 sealed headless Stops (2026-09-02). Parity of verdict
    # is kept (both pass the turn); what changes is that the record says `no_transcript`
    # instead of `evaluator_exception`.
    transcript_path = event.get("transcript_path") or ""
    if not _readable(transcript_path):
        return EvaluatorResult.not_applicable(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="no_transcript",
            evidence=(Evidence("event_field", "transcript_path=unreadable"),),
            duration_ms=_duration_ms(started),
        )
    text = _turn_final_text(transcript_path)
    if not text.strip():
        return EvaluatorResult.not_applicable(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="no_final_text",
            evidence=(Evidence("state", "final_text=absent"),),
            duration_ms=_duration_ms(started),
        )

    asks = _count_asks(_prose_lines(text))
    chars = len(text)
    shape_evidence = (
        Evidence("state", f"ask_count={asks}"),
        Evidence("state", f"final_chars={chars}"),
    )

    if asks >= 2:
        return EvaluatorResult.blocked(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="multi_ask",
            remediation=_multi_ask_remediation(asks),
            evidence=shape_evidence,
            duration_ms=_duration_ms(started),
        )

    if asks == 0 and chars >= LONG_CHARS:
        if _last_user_asked_to_stop(transcript_path):
            return EvaluatorResult.passed(
                gate_id=GATE_ID,
                gate_version=GATE_VERSION,
                event_id=event_id,
                reason_code="stop_requested",
                evidence=shape_evidence + (Evidence("state", "stop_requested=true"),),
                duration_ms=_duration_ms(started),
            )
        return EvaluatorResult.blocked(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="no_engagement_point",
            remediation=_no_engagement_remediation(chars),
            evidence=shape_evidence,
            duration_ms=_duration_ms(started),
        )

    return EvaluatorResult.passed(
        gate_id=GATE_ID,
        gate_version=GATE_VERSION,
        event_id=event_id,
        reason_code="shape_ok",
        evidence=shape_evidence,
        duration_ms=_duration_ms(started),
    )
