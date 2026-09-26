"""4.4 — what the harness actually did, read from the records rather than inferred.

The plan proposed attributing firings "by signature string" from injected `additionalContext`.
The real structure is narrower and better: a Stop gate that blocks writes
`attachment.blockingError`, and that field carries the gate's own message.

THE LIMIT THAT GOVERNS EVERY COUNT HERE. A Stop gate that PASSES emits `hook_success` with no
message at all. "Which gate passed" is therefore not recoverable from a transcript — not
because this reader is weak, but because the record does not exist. Every number below is a
count of gate COMPLAINTS. It is never a count of gate activity, and a gate that never appears
may be passing constantly, may be inert, or may have failed silently the way
`response-shape.py`'s stamp did when its NameError went into a bare except.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from bearhug.replay.transcript import Event, iter_events

#: Ordered: the first signature to match wins, so put the specific before the general.
#:
#: "dlv" MUST precede "debug-first": dlv-verify-gate.py is debug-first.sh's blocking successor
#: and its real message literally opens "debug-first gate — this turn edited a .go file...".
#: With "debug-first" checked first, every real dlv-verify-gate block reads as debug-first —
#: found in the real corpus, 2026-08-29: all 8 "debug-first"-attributed blocks in
#: project-barracuda's transcripts were this exact dlv-verify-gate message, and debug-first.sh
#: is itself an advisory PostToolUse hook that can never produce a hook_blocking_error at all.
#: A silent debug-first bucket next to a silent-zero dlv-verify-gate bucket looked like two
#: findings; it was one misattribution.
SIGNATURES: tuple[tuple[str, str], ...] = (
    ("go vet failed", "go-postedit"),
    ("gofmt", "go-postedit"),
    ("task durability", "task-durability"),
    ("task list", "task-existence"),
    ("taskcreate", "task-existence"),
    ("engagement point", "response-shape"),
    ("0135", "response-shape"),
    ("review gate", "review-gate"),
    ("reviewer", "review-gate"),
    ("dlv", "dlv-verify-gate"),
    ("debug-first", "debug-first"),
    ("joinkey", "joinkey-lint"),
    ("phase tag", "phase-tag"),
)

UNATTRIBUTED = "unattributed"


def attribute(message: str) -> str:
    """Name the gate that wrote a blocking message, or say we could not."""
    lowered = (message or "").lower()
    for signature, gate in SIGNATURES:
        if signature in lowered:
            return gate
    return UNATTRIBUTED


@dataclass(frozen=True, slots=True)
class Firing:
    """One gate blocking one turn."""

    gate: str
    timestamp: str
    session: str
    message: str

    @property
    def day(self) -> str:
        return self.timestamp[:10]


def blocking_firings(path: Path | str) -> Iterator[Firing]:
    """Every Stop-gate block in one transcript, attributed."""
    for event in iter_events(path):
        if event.kind != "hook_block":
            continue
        yield Firing(
            gate=attribute(event.text),
            timestamp=event.timestamp,
            session=event.session,
            message=event.text,
        )


def stacked_blocks(firings: list[Firing]) -> dict[str, list[str]]:
    """Turns blocked by two or more gates in the same second.

    Grouped to the second because the Stop gates run as a group: the corpus shows three of them
    writing incompatible remediations 10 ms apart, which no per-gate view can see.
    """
    by_second: dict[str, list[str]] = {}
    for firing in firings:
        by_second.setdefault(firing.timestamp[:19], []).append(firing.gate)
    return {second: sorted(gates) for second, gates in by_second.items() if len(gates) > 1}


def tool_traffic(paths: list[Path]) -> dict[str, int]:
    """Tool-call counts across transcripts — the denominator MATCHER-REACHABILITY needs."""
    counts: dict[str, int] = {}
    for path in paths:
        for event in iter_events(path):
            if event.kind == "tool_use" and event.name:
                counts[event.name] = counts.get(event.name, 0) + 1
    return counts


__all__ = [
    "Event",
    "Firing",
    "attribute",
    "blocking_firings",
    "stacked_blocks",
    "tool_traffic",
]
