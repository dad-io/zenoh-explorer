"""2.13 MATCHER-REACHABILITY — a covered gate can still be unreachable.

2.6 GATE-COVERAGE asks whether a gate is *named*. This asks whether its matcher can see the
tool the behaviour actually travels through. The two are independent, and the gap between them
is where barracuda's hook defects live.

Scope, stated because it is narrower than it first looks. This check reads `settings.json`
matchers only. `dlv-verify-gate.py`'s famous blindness — 0 of 132 `.go` writes — is NOT
visible here: it is a Stop hook with no tool matcher at all, and its blind spot is INSIDE the
script, which scans transcript entries for Edit/Write/MultiEdit and never for a Bash heredoc.
Static matcher analysis cannot see that. Running the gate against a Bash-write fixture can,
which is Phase 3.1's job. What this check does catch is the other half: a PostToolUse hook
wired only to the edit tools while 74% of tool calls are Bash.

Traffic is an argument, never measured here. Counting tool calls belongs to Phase 4's reader,
and a second implementation of "what is a tool call" is the duplicate-authority class. Since M17
the argument is a `Traffic` that names the corpus it was counted from; with none supplied the
check reports the structural blindness it can see and one explicit UNMEASURED finding, and quotes
no share at all — a constant measured on 2026-08-28 used to stand in here as if it were current.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bearhug.lint.gates import parse_hooks
from bearhug.model import Evidence, Finding, Severity


@dataclass(frozen=True, slots=True)
class Traffic:
    """Tool-call counts and the corpus identity they were measured from."""

    counts: dict[str, int] = field(default_factory=dict)
    corpus_kind: str = "unknown"
    corpus_digest: str = ""
    transcripts: int = 0

    @property
    def label(self) -> str:
        return f"{self.corpus_kind}:{self.corpus_digest[:12]}"

    @classmethod
    def from_corpus(cls, corpus: Any) -> Traffic:
        """Count tool calls over an explicitly selected corpus, through Phase 4's reader."""
        from bearhug.replay.hooks import tool_traffic

        return cls(
            counts=tool_traffic(list(corpus.paths)),
            corpus_kind=corpus.kind,
            corpus_digest=corpus.digest,
            transcripts=len(corpus.paths),
        )

#: Tools that mutate a file through the dedicated edit surface.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

#: A hook whose source reasons about written files. These are the gates for which missing the
#: Bash path is a defect rather than a design choice.
WRITE_CONCERNED = re.compile(r"file_path|\.go\b|written|tool_input|Edit\b|Write\b")

#: Events that fire once per turn and legitimately carry no tool matcher.
TURN_SCOPED = frozenset({"Stop", "SessionStart", "UserPromptSubmit", "SubagentStop", "PreCompact"})


def matcher_tools(matcher: str | None) -> set[str]:
    """The tool names a matcher selects. ``*`` and ``None`` mean "every tool", not a name."""
    if not matcher or matcher.strip() == "*":
        return set()
    return {part.strip() for part in matcher.split("|") if part.strip()}


#: A bare tool name: letters, digits, underscore. Anything else in a `|`-split part means the
#: matcher is not a plain union of names — a regex like ``Edit.*``, for one.
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def literal_tool_union(matcher: str | None) -> frozenset[str] | None:
    """The tool set `matcher` denotes if it is bare names joined by ``|``, else ``None``.

    Used to decide whether two differently spelled matchers provably denote the same tool set —
    a reordered ``Write|Edit|MultiEdit`` versus ``Edit|Write|MultiEdit`` does, project_setup's
    owned-hook dedup and hooks.audit's OVERLAP check both need exactly that. ``None`` covers
    everything this is not: the two spellings of "every tool" (no matcher, or ``"*"``) and any
    matcher containing regex syntax beyond a bare union. Two ``None`` results are never treated
    as equal by this function alone — a caller that wants "both absent" to count as a match (an
    unmatched hook fires on every tool, so two of them always overlap) makes that decision
    itself, since an absent matcher and an unresolvable regex fail to resolve for different
    reasons and only the first is safe to treat as its own kind of match.
    """
    if not matcher or matcher.strip() == "*":
        return None
    tools = matcher_tools(matcher)
    if not tools or any(not _TOOL_NAME.match(tool) for tool in tools):
        return None
    return frozenset(tools)


def check_matcher_reachability(
    *,
    settings: dict[str, Any],
    project_root: Path,
    traffic: Traffic | None,
    snapshot: str,
) -> list[Finding]:
    """Report, per hook, how much of the observed tool traffic its matcher can see.

    With ``traffic`` None the structural verdict (an edit-tool matcher on a write-concerned hook
    cannot see Bash) is still reported, because it needs no corpus; every share is withheld and
    one UNMEASURED finding says why.
    """
    project_root = Path(project_root)
    findings: list[Finding] = []
    if traffic is None:
        counts: dict[str, int] = {}
        limit = (
            "No corpus was supplied, so no traffic share is reported: reachability shares are a "
            "property of a named corpus, and a number measured on another day is not current "
            "fact. The structural verdict below needs no corpus. Run `bearhug report` with "
            "--corpus for digest-attributed shares."
        )
        findings.append(
            Finding(
                id="reach-traffic-unmeasured",
                check="MATCHER-REACHABILITY",
                severity=Severity.COSTLY,
                summary="Traffic shares are UNMEASURED: no corpus was supplied to this lint run.",
                snapshot=snapshot,
                evidence=(Evidence(file=".claude/settings.json"),),
                detail="traffic=None",
                limit=limit,
            )
        )
    else:
        counts = traffic.counts
        total = sum(counts.values()) or 1
        bash_calls = counts.get("Bash", 0)
        bash_share = 100 * bash_calls / total
        limit = (
            f"Judged against corpus {traffic.label} ({traffic.transcripts} transcripts): "
            f"{total:,} tool calls, of which Bash is {bash_calls:,} ({bash_share:.0f}%). "
            "Reachability is a property of THIS corpus: a matcher that covers little traffic "
            "here may be well aimed at a rare, important event, and a matcher that covers a lot "
            "may still miss the specific path a given behaviour takes. This check narrows where "
            "to look; it does not prove a gate fired or failed to."
        )

    # A script may be registered several times on one event with different matchers —
    # hard-safety.py is wired on Bash AND on Edit|Write|MultiEdit. Judging each registration
    # alone would report it blind to Bash while it is watching Bash three lines above.
    registrations: dict[tuple[str, str], set[str]] = {}
    matchers: dict[tuple[str, str], list[str]] = {}
    for spec in parse_hooks(settings):
        if not spec.script:
            continue
        key = (spec.event, spec.script)
        registrations.setdefault(key, set()).update(matcher_tools(spec.matcher))
        matchers.setdefault(key, []).append(spec.matcher or "*")

    for (event, script), tools in registrations.items():
        spec_matcher = "|".join(dict.fromkeys(matchers[(event, script)]))
        name = Path(script).name
        if traffic is not None:
            total = sum(counts.values()) or 1
            covered = sum(counts.get(tool, 0) for tool in tools) if tools else total
            share = 100 * covered / total

        source = ""
        path = project_root / script
        if path.is_file():
            source = path.read_text(encoding="utf-8", errors="replace")

        blind_to_bash = (
            event not in TURN_SCOPED
            and bool(tools & EDIT_TOOLS)
            and "Bash" not in tools
            and bool(WRITE_CONCERNED.search(source))
        )
        if traffic is None:
            if not blind_to_bash:
                continue  # a share row without a share is nothing; the unmeasured row stands in
            summary = (
                f"{name} on {event} matches {spec_matcher} and cannot see Bash "
                "(traffic share unmeasured: no corpus supplied)"
            )
            detail = f"{name} {event} matcher={spec_matcher} covers=unmeasured"
        elif blind_to_bash:
            summary = (
                f"{name} on {event} matches {spec_matcher} and cannot see Bash, "
                f"which carries {bash_share:.0f}% of tool calls in {traffic.label}"
            )
            detail = f"{name} {event} matcher={spec_matcher} covers={covered:,}"
        else:
            summary = f"{name} on {event} sees {share:.0f}% of tool calls in {traffic.label}"
            detail = f"{name} {event} matcher={spec_matcher} covers={covered:,}"
        findings.append(
            Finding(
                id=f"reach-{name}-{event}",
                check="MATCHER-REACHABILITY",
                severity=Severity.BROKEN if blind_to_bash else Severity.INFO,
                summary=summary,
                snapshot=snapshot,
                evidence=(Evidence(file=script, excerpt=f"{event} · {spec_matcher}"),),
                detail=detail,
                limit=limit,
            )
        )
    return findings
