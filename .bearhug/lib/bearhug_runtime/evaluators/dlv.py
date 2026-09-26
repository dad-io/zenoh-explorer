"""Pure evaluator port of the captured ``dlv-verify-gate.py`` Stop gate.

R08 is a mechanical policy port. The captured gate passes when any Bash call at or after the last
Go write contains a ``dlv`` word match, even if the match is only prose or ``dlv --help``. This
evaluator preserves that verdict and tells the truth in bounded evidence: an approved real
subcommand is distinguished from a word-only match. Tightening the *word-only* case belongs to
R08B/G05 after its proof-level protocol is ruled.

**R08C, ruled (Sam, 2026-09-03):** "``dlv-verify-gate`` PASSES on a resolved dlv program — a path
whose basename is `dlv` or a `$NAME` bound to such a path in the same command satisfies the gate;
the literal-word requirement is retired." Before this ruling, a real session the captured gate's
word-boundary regex cannot see (``/x/go/bin/dlv attach`` or ``"$DLV" attach`` with ``DLV=`` bound
in the same command) recorded ``dlv_match=real_subcommand_unmatched_by_word`` and BLOCKED — a
false block M14 (2026-09-01) measured four times in the frozen corpus. It now PASSES with reason
code ``dlv_session_after_write`` and evidence ``dlv_match=real_subcommand_resolved_program``. This
is a deliberate divergence from the captured gate, not a parity port: the captured gate still
blocks these commands, and `tests/test_dlv_evaluator.py` states that divergence explicitly.

The captured position model is also preserved: a Bash tool call that both writes Go and mentions
DLV has one transcript position, so it passes regardless of textual order inside that command.
Evidence names that limitation as ``ordering=same_tool_call`` rather than pretending the runtime
proved command-level ordering.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import time
from collections.abc import Mapping

from ..results import MAX_EVIDENCE_VALUE, EvaluatorResult, Evidence
from ..turns import ToolCall, current_turn_tool_calls
from ..writes import WriteResolution, resolve_go_file_writes, strip_heredocs

GATE_ID = "dlv-verify-gate"
GATE_VERSION = "1.2.0"

_DLV_WORD = re.compile(r"(?:^|[\s;&|])dlv\b")
_DLV_SUBCOMMANDS = frozenset(
    ("test", "debug", "exec", "attach", "trace", "core", "connect", "dap")
)
_SHELL_OPERATORS = frozenset((";", "|", "&", "&&", "||"))

_REMEDIATION = (
    "debug-first gate — this turn edited a .go file and no `dlv` session ran "
    "afterward. A green `go test`/`go build` is not the thing this checks: "
    "run `dlv test <pkg> -- -test.run <TestName>` (or `dlv debug`/`dlv exec`), "
    "set a breakpoint on the changed line(s), and confirm the runtime value "
    "before claiming the change works. Sam's ruling, 2026-08-21: a printed "
    "reminder did not change the behavior; this gate exists because that one "
    "didn't hold."
)


def _duration_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000)


_CONTINUATION = re.compile(r"\\\n")
_ASSIGNMENT = re.compile(
    r"(?:^|(?<=[\s;&|]))(?:export\s+)?([A-Za-z_]\w*)="
    r"((?:\$\([^)]*\)|\"[^\"]*\"|'[^']*'|[^\s;&|\"'])+)"
)


def _segments(command: str) -> tuple[tuple[str, ...], ...]:
    """Return quote-aware shell segments with heredoc bodies excluded.

    1.1.0: a newline is a segment boundary and a backslash-newline a continuation. Before this,
    `shlex` read a newline as whitespace, so a `dlv test` on the second line of a command was
    recorded as a word-only match (bear-hug M14, 2026-09-01: seven real sessions in one sample).
    """
    source = _CONTINUATION.sub(" ", strip_heredocs(command))
    try:
        # The newline is a PUNCTUATION character, not whitespace, so it ends a segment, and a
        # `# comment` line ends at its own newline rather than swallowing what follows it.
        lexer = shlex.shlex(source, posix=True, punctuation_chars="();<>|&\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        # A malformed command still receives the captured word-match policy. The richer real-run
        # evidence is best effort and must not turn a legacy pass into a block.
        return ()

    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_OPERATORS or (
            token and set(token) <= {";", "|", "&", "\n"}
        ):
            if current:
                segments.append(tuple(current))
                current = []
        else:
            current.append(token)
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _without_assignments(tokens: tuple[str, ...]) -> tuple[str, ...]:
    index = 0
    while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith("-"):
        name = tokens[index].split("=", 1)[0]
        valid_name = bool(
            name
            and (name[0].isalpha() or name[0] == "_")
            and name.replace("_", "a").isalnum()
        )
        if not valid_name:
            break
        index += 1
    return tokens[index:]


def _bindings(command: str) -> dict[str, str]:
    """Every ``NAME=value`` the command binds, quotes removed; one level, nothing outside it."""
    return {
        name: value.strip("\"'")
        for name, value in _ASSIGNMENT.findall(strip_heredocs(command))
    }


def _program(token: str, bound: dict[str, str]) -> str | None:
    verb = token.strip("\"'")
    if verb.startswith("$"):
        return bound.get(verb[1:].strip("{}"))
    return verb


def _real_dlv_subcommand(command: str) -> str | None:
    """Return the first approved ``dlv <subcommand>`` in shell verb position.

    1.1.0: the verb is resolved to a PROGRAM and matched by basename, so ``/x/go/bin/dlv attach``
    and a ``"$DLV" attach`` whose ``DLV=`` is bound in the same command both count; an unbound
    ``$DLV`` is never guessed. The verdict is unchanged (the word match still governs, R08B);
    only the recorded evidence moves from word_only to real_subcommand for these shapes.
    """
    bound = _bindings(command)
    for segment in _segments(command):
        tokens = _without_assignments(segment)
        if len(tokens) < 2 or tokens[1] not in _DLV_SUBCOMMANDS:
            continue
        program = _program(tokens[0], bound)
        if program is not None and program.rsplit("/", 1)[-1] == "dlv":
            return tokens[1]
    return None


def _position_evidence(label: str, position: tuple[int, int]) -> Evidence:
    return Evidence("state", f"{label}={position[0]}:{position[1]}")


def _write_evidence(call: ToolCall, resolution: WriteResolution) -> tuple[Evidence, ...]:
    evidence: list[Evidence] = [
        Evidence("tool_call", f"go_write_tool={call.name}"),
        _position_evidence("go_write_position", call.position),
    ]
    if resolution.paths:
        # A single call may write many files. The gate's ordering obligation is call-level, so the
        # final resolved path is enough to identify the last write without unbounded evidence.
        path = resolution.paths[-1]
        path_value = f"go_write_path={path}"
        if len(path_value) > MAX_EVIDENCE_VALUE:
            digest = hashlib.sha256(
                path.encode("utf-8", errors="surrogatepass")
            ).hexdigest()
            path_value = f"go_write_path_sha256={digest}"
        evidence.append(Evidence("path", path_value))
    else:
        evidence.append(Evidence("path", "go_write_path=opaque"))
    return tuple(evidence)


def _readable(path: object) -> bool:
    """Is there a file here to read?

    Checked WITHOUT opening it, so the check cannot itself raise.
    """
    import os

    return isinstance(path, str) and bool(path) and os.path.isfile(path)


def evaluate_dlv_verification(event: Mapping[str, object]) -> EvaluatorResult:
    """Evaluate the current turn and return exactly one immutable result."""
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

    last_write_call: ToolCall | None = None
    last_write_resolution: WriteResolution | None = None
    last_word_call: ToolCall | None = None
    last_real_call: ToolCall | None = None
    last_real_subcommand: str | None = None

    # PARITY with dlv-verify-gate.py:161 — `if not transcript_path or not
    # os.path.exists(transcript_path): return`. Without this the unguarded open() in
    # turns.py raised, and once D04's fail-closed ruling actually fired that raise became an
    # UNSATISFIABLE block on every turn whose transcript does not resolve: the model cannot
    # make a missing file exist. The captured gate passes here; so must this.
    transcript_path = event.get("transcript_path")
    if not transcript_path or not _readable(transcript_path):
        return EvaluatorResult.not_applicable(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="no_transcript",
            evidence=(Evidence("event_field", "transcript_path=unreadable"),),
            duration_ms=_duration_ms(started),
        )

    for call in current_turn_tool_calls(event.get("transcript_path")):
        resolution = resolve_go_file_writes(call.name, call.tool_input)
        if resolution:
            last_write_call = call
            last_write_resolution = resolution

        if call.name != "Bash" or not isinstance(call.tool_input, Mapping):
            continue
        command = call.tool_input.get("command")
        if not isinstance(command, str):
            continue
        if _DLV_WORD.search(command):
            last_word_call = call
        subcommand = _real_dlv_subcommand(command)
        if subcommand is not None:
            last_real_call = call
            last_real_subcommand = subcommand

    if last_write_call is None or last_write_resolution is None:
        return EvaluatorResult.not_applicable(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code="no_go_writes",
            evidence=(Evidence("state", "current_turn_go_writes=0"),),
            duration_ms=_duration_ms(started),
        )

    evidence = list(_write_evidence(last_write_call, last_write_resolution))
    word_after_write = bool(
        last_word_call is not None and last_word_call.position >= last_write_call.position
    )
    real_after_write = bool(
        last_real_call is not None and last_real_call.position >= last_write_call.position
    )

    match_call: ToolCall | None = None
    reason_code = ""
    if word_after_write and real_after_write and last_real_call is not None:
        reason_code = "dlv_session_after_write"
        evidence.extend(
            (
                Evidence("state", "dlv_match=real_subcommand"),
                Evidence("state", f"dlv_subcommand={last_real_subcommand}"),
                _position_evidence("dlv_position", last_real_call.position),
            )
        )
        match_call = last_real_call
    elif word_after_write and last_word_call is not None:
        reason_code = "dlv_word_match_after_write"
        evidence.extend(
            (
                Evidence("state", "dlv_match=word_only"),
                _position_evidence("dlv_position", last_word_call.position),
            )
        )
        match_call = last_word_call
    elif real_after_write and last_real_call is not None:
        # R08C, ruled (Sam, 2026-09-03): a resolved dlv program satisfies the gate on its own —
        # `/x/go/bin/dlv attach` or `"$DLV" attach` with DLV bound in the same command. Before this
        # ruling the captured gate's word-boundary regex could not see these (M14, 2026-09-01: four
        # such sessions in the frozen corpus) and the runtime BLOCKED for parity while the evidence
        # named the gap as `dlv_match=real_subcommand_unmatched_by_word`. That label is retired: the
        # runtime now deliberately diverges from the captured gate and PASSES.
        reason_code = "dlv_session_after_write"
        evidence.extend(
            (
                Evidence("state", "dlv_match=real_subcommand_resolved_program"),
                Evidence("state", f"dlv_subcommand={last_real_subcommand}"),
                _position_evidence("dlv_position", last_real_call.position),
            )
        )
        match_call = last_real_call

    if match_call is not None:
        ordering = (
            "same_tool_call"
            if match_call.position == last_write_call.position
            else "later_tool_call"
        )
        evidence.append(Evidence("state", f"ordering={ordering}"))
        return EvaluatorResult.passed(
            gate_id=GATE_ID,
            gate_version=GATE_VERSION,
            event_id=event_id,
            reason_code=reason_code,
            evidence=evidence,
            duration_ms=_duration_ms(started),
        )

    evidence.append(Evidence("state", "dlv_after_write=false"))
    return EvaluatorResult.blocked(
        gate_id=GATE_ID,
        gate_version=GATE_VERSION,
        event_id=event_id,
        reason_code="dlv_session_missing",
        remediation=_REMEDIATION,
        evidence=evidence,
        duration_ms=_duration_ms(started),
    )


__all__ = ["evaluate_dlv_verification"]
