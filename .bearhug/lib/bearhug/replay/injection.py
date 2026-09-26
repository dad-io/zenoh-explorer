"""4.6 injection-vs-use — for each injected payload, did the same turn subsequently reference it?

The plan's question: "did the same turn subsequently reference it? Report an injected-token-to-
used ratio per injector." Its point is to let "ineffective" be answered with a number instead of
an impression — docs/METHOD.md is explicit that this is a correlation, never a causal claim: a
payload can be useful without being quoted back, and a quoted token can be coincidence rather
than use.

THE SPLIT THIS MODULE HAS TO MAKE THAT `replay.transcript.Event` CANNOT. One `SessionStart` (or
other) moment batches several hooks' output into one `hook_additional_context` record — its
`content` field is a LIST, one string per hook. `Event.text` (see `_injected_text`) joins that
list into one string so a byte count is honest; that is exactly wrong for THIS module, which
must attribute board-restore's row numbers, memex's citations, and graft's file locations to
three separate injectors even when they land in the same record. So this module reads the raw
`content` list itself rather than going through `Event`, the same way `replay.metrics` reaches
past `Event` for `is_raw_user` — a second normalisation only where the shared one would lose the
fact this check needs.

REFERENCE DETECTION, STATED PLAINLY. A payload is "referenced" if a RARE token drawn from IT
(a decision id `0\\d{3}`, a file path with a known extension, or — for board-restore only — a
`board_row`/`authority` value from its own TaskCreate metadata) reappears in the assistant text
or tool-call arguments AFTER that injection, within the SAME turn. Common words are never
tokens. An injector whose payload carries no such token (debug-first's advisory line names no
file, no id) is reported as `with_token=0` — UNMEASURABLE for this injector, not a rate of zero;
see docs/METHOD.md's silence-is-not-absence rule and `ledger.py`'s identical treatment of gates
this reader cannot resolve to a rate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.model import Evidence, Finding, Severity
from bearhug.replay.transcript import Event, _events_of, iter_records

# --- attribution -------------------------------------------------------------------------

#: Ordered defensively, as `replay.hooks.SIGNATURES` documents it must be: a signature that is a
#: substring of another injector's text has to come after it. Checked against every sample this
#: module was built from (2026-08-29): none of these collide with each other in practice (the
#: two that share a word — "using-superpowers" / "you have superpowers" — both resolve to the
#: SAME injector, so their relative order cannot misattribute anything), but the ordering is kept
#: specific-marker-first anyway so a future addition inherits the discipline rather than the
#: memory of it.
INJECTOR_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("[board-restore]", "board-restore"),
    ("[memex]", "memex"),
    ("[graft]", "graft"),
    ("semantically-recalled", "memq-recall"),
    ("debug-first", "debug-first"),
    ("go vet flagged", "go-postedit"),
    ("gofmt", "go-postedit"),
    ("using-superpowers", "superpowers"),
    ("you have superpowers", "superpowers"),
)

UNATTRIBUTED = "unattributed"


def attribute_injector(content: str) -> str:
    """Name the hook that wrote one injected chunk, or say we could not."""
    lowered = (content or "").lower()
    for signature, injector in INJECTOR_SIGNATURES:
        if signature in lowered:
            return injector
    return UNATTRIBUTED


# --- rare-token extraction, stated as the rule it is --------------------------------------

#: barracuda decision ids are zero-padded to four digits (see `lint.refs.DECISION_ID`).
_DECISION_ID = re.compile(r"\b0\d{3}\b")

#: A path-or-filename token with a real extension. Chosen extensions are the ones this corpus's
#: injectors actually cite (Go source, docs, hook scripts, config) — not a generic path grammar,
#: which would also match prose like "e.g." or version strings.
_FILE_PATH = re.compile(r"[\w][\w./-]*\.(?:go|md|proto|py|sh|cjs|json|yaml|yml|txt)\b")

#: board-restore's payload is the one injector whose rare token is structured data, not prose:
#: it emits `TaskCreate(..., metadata={"board_row": "43", "authority": "docs/....md"})` lines
#: verbatim, and the harness's OWN compliance mechanism is copying that metadata into a real
#: TaskCreate call — so this is the one rare token this module expects to see referenced often,
#: by design, not by chance.
_BOARD_ROW = re.compile(r'"board_row":\s*"(\d+)"')
_AUTHORITY_PATH = re.compile(r'"authority":\s*"([^"]+)"')

#: A token shorter than this is too common to be "rare" — a one-digit board row number would
#: match almost any later turn by accident. Documented here rather than tuned away silently.
MIN_TOKEN_LEN = 2


def extract_rare_tokens(injector: str, content: str) -> frozenset[str]:
    """Rare, checkable tokens drawn from one injected chunk.

    Deliberately narrow: a decision id, a cited file path, and (board-restore only) the
    row/authority values it tells the model to copy verbatim. Common words are never
    candidates — there is no generic "important word" heuristic here, only these three shapes.
    """
    tokens: set[str] = set()
    tokens.update(m.group(0) for m in _FILE_PATH.finditer(content))
    tokens.update(m.group(0) for m in _DECISION_ID.finditer(content))
    if injector == "board-restore":
        tokens.update(m.group(1) for m in _BOARD_ROW.finditer(content))
        tokens.update(m.group(1) for m in _AUTHORITY_PATH.finditer(content))
    return frozenset(t for t in tokens if len(t) >= MIN_TOKEN_LEN)


def _token_referenced(token: str, haystack: str) -> bool:
    """Whole-token match: a numeric board row must not match as a substring of a bigger number,
    and a file path must not match inside a longer identifier that merely contains it."""
    if token.isdigit():
        pattern = re.compile(rf"(?<!\d){re.escape(token)}(?!\d)")
    else:
        pattern = re.compile(rf"(?<!\w){re.escape(token)}(?!\w)")
    return bool(pattern.search(haystack))


def is_referenced(tokens: frozenset[str], haystack: str) -> bool:
    return any(_token_referenced(t, haystack) for t in tokens)


# --- a timeline that keeps a batched hook_inject record's parts separate --------------------


@dataclass(slots=True)
class InjectionChunk:
    """One hook's own contribution to one `hook_additional_context` record.

    Kept distinct from `replay.transcript.Event`'s `hook_inject` kind, which joins a batch into
    one string — exactly what this module cannot use (see module docstring)."""

    kind: str = "inject"
    hook_event: str = ""
    content: str = ""
    timestamp: str = ""
    session: str = ""


TimelineItem = Event | InjectionChunk


def _injection_chunks(record: dict[str, Any]) -> Iterator[InjectionChunk]:
    attachment = record.get("attachment") or {}
    if attachment.get("type") != "hook_additional_context":
        return
    ts = record.get("timestamp", "")
    session = record.get("sessionId", "")
    hook_event = attachment.get("hookEvent") or attachment.get("hookName") or ""
    content = attachment.get("content", "")
    parts = content if isinstance(content, list) else [content] if content else []
    for part in parts:
        if isinstance(part, str) and part.strip():
            yield InjectionChunk(hook_event=hook_event, content=part, timestamp=ts, session=session)


def _timeline(path: Path) -> Iterator[TimelineItem]:
    """One transcript, as `Event`s with `hook_inject` batches unpacked into their own chunks."""
    for _, record in iter_records(path):
        if record.get("type") == "attachment":
            attachment = record.get("attachment") or {}
            if attachment.get("type") == "hook_additional_context":
                yield from _injection_chunks(record)
                continue
        yield from _events_of(record)


def _turn_segments(items: Iterator[TimelineItem]) -> Iterator[list[TimelineItem]]:
    """Group a timeline into turns, split on genuine human messages.

    Unlike `replay.transcript.iter_turns` (built for ledger.py's compliance counting, which has
    no use for anything before the first prompt), items BEFORE the first user message are kept
    as a leading slice of turn one rather than dropped. A `SessionStart` injection has no turn to
    belong to except the one it precedes; dropping it would make every SessionStart injector's
    reference rate read as a permanent, silent 0/0.
    """
    current: list[TimelineItem] = []
    seen_user = False
    for item in items:
        if item.kind == "user" and seen_user:
            yield current
            current = []
        if item.kind == "user":
            seen_user = True
        current.append(item)
    if current:
        yield current


def _haystack(items: list[TimelineItem]) -> str:
    """Everything the ASSISTANT produced after one point in a turn: its prose and its tool
    arguments. The human's own prompt text is deliberately excluded — referencing means the
    model used the injected content, not that the operator happened to reuse the same word."""
    parts: list[str] = []
    for item in items:
        if item.kind == "assistant" and getattr(item, "text", ""):
            parts.append(item.text)
        elif item.kind == "tool_use":
            parts.append(json.dumps(getattr(item, "payload", {}), default=str))
    return "\n".join(parts)


# --- per-injector aggregation ----------------------------------------------------------------


@dataclass(slots=True)
class InjectorStats:
    injector: str
    injections: int = 0
    turns: int = 0
    with_token: int = 0
    referenced: int = 0
    injected_bytes: int = 0

    @property
    def reference_rate(self) -> float | None:
        """None means unmeasurable (no injection of this type carried an extractable token) —
        never a rate of zero. See docs/METHOD.md's silence-is-not-absence rule."""
        return self.referenced / self.with_token if self.with_token else None


#: A candidate token that shows up in more than this fraction of ONE injector's own occurrences
#: is that injector's boilerplate, not a rare signal. Caught building this module: memex always
#: prints "catalog: docs/memex/index.md" and superpowers' own SKILL.md text names "CLAUDE.md" in
#: its generic instructions — both match `_FILE_PATH` and both are the single most-repeated
#: string in the entire corpus, the opposite of rare. "Rare" here is therefore operationalised
#: relative to THIS injector's own repeated template, not against English usage generally —
#: exactly the brief's instruction to prefer rare tokens over common words, made checkable.
COMMON_TOKEN_FRACTION = 0.5

#: Below this many occurrences, "appears in X% of them" is not a meaningful statistic — with a
#: single firing every one of its tokens trivially has 100% document frequency. Filtering is
#: skipped (every candidate token is kept) until an injector has fired at least this often.
MIN_OCCURRENCES_FOR_BOILERPLATE_FILTER = 3


@dataclass(slots=True)
class _Pending:
    raw_tokens: frozenset[str]
    haystack: str
    injected_bytes: int


def compute_injection_stats(
    paths: list[Path], *, since: str | None = None
) -> dict[str, InjectorStats]:
    """Two passes, because "rare" cannot be judged from one occurrence alone.

    Pass 1 walks every transcript once, attributing each `hook_inject` chunk and extracting its
    CANDIDATE tokens and the text that followed it in the same turn. Pass 2 computes, per
    injector, which of those candidates are that injector's own boilerplate
    (`COMMON_TOKEN_FRACTION`) and only then decides whether a chunk had a usable rare token and
    whether it was referenced — a token common to an injector's every firing would otherwise
    "match" almost by construction, inflating the very rate this check exists to keep honest.
    """
    pending: dict[str, list[_Pending]] = {}
    turns_per_injector: dict[str, int] = {}

    for path in paths:
        for segment in _turn_segments(_timeline(path)):
            injectors_here: set[str] = set()
            for index, item in enumerate(segment):
                if item.kind != "inject":
                    continue
                day = item.timestamp[:10] if item.timestamp else ""
                if since and day and day < since:
                    continue
                injector = attribute_injector(item.content)
                injectors_here.add(injector)
                raw_tokens = extract_rare_tokens(injector, item.content)
                haystack = _haystack(segment[index + 1 :]) if raw_tokens else ""
                pending.setdefault(injector, []).append(
                    _Pending(raw_tokens, haystack, len(item.content.encode("utf-8")))
                )
            for injector in injectors_here:
                turns_per_injector[injector] = turns_per_injector.get(injector, 0) + 1

    stats: dict[str, InjectorStats] = {}
    for injector, items in pending.items():
        n = len(items)
        frequency: dict[str, int] = {}
        for it in items:
            for token in it.raw_tokens:
                frequency[token] = frequency.get(token, 0) + 1
        boilerplate: set[str] = set()
        if n >= MIN_OCCURRENCES_FOR_BOILERPLATE_FILTER:
            boilerplate = {t for t, c in frequency.items() if c / n > COMMON_TOKEN_FRACTION}

        row = InjectorStats(
            injector=injector,
            injections=n,
            turns=turns_per_injector.get(injector, 0),
            injected_bytes=sum(it.injected_bytes for it in items),
        )
        for it in items:
            tokens = it.raw_tokens - boilerplate
            if not tokens:
                continue
            row.with_token += 1
            if is_referenced(tokens, it.haystack):
                row.referenced += 1
        stats[injector] = row
    return stats


# --- findings ----------------------------------------------------------------------------

#: A reference rate below this, with a sample worth trusting, is reported COSTLY: the payload
#: fires and is essentially never echoed back. Chosen as a round, conservative threshold, not
#: fit to this corpus's numbers.
LOW_RATE_THRESHOLD = 0.15
#: Below this many WITH-TOKEN injections, a rate is too small a sample to rank as a defect.
MIN_SAMPLE_FOR_SEVERITY = 5

LIMIT_CORRELATION = (
    "Correlation only, never causation (docs/METHOD.md): a rare token from the payload "
    "reappearing in the assistant's later text or tool arguments, in the SAME turn. A payload "
    "can be useful without being quoted back (the model may act on it without naming it), and a "
    "token can reappear by coincidence rather than use — this rate bounds neither direction."
)
LIMIT_NO_TOKEN = (
    "This injector's payload carried no extractable rare token in any occurrence measured — "
    "not a reference rate of zero. This reader can only check whether a decision id, a cited "
    "file path, or (board-restore) a board_row/authority value reappears; an injector whose "
    "message names none of those (e.g. debug-first's one-line advisory) is UNMEASURABLE by this "
    "method, not shown to be ignored. See docs/METHOD.md's silence-is-not-absence rule."
)


def build_findings(
    stats: dict[str, InjectorStats], *, snapshot_id: str, since: str | None, window: str
) -> list[Finding]:
    """One Finding per injector, ranked the way docs/METHOD.md ranks findings: a measurably
    low reference rate above one this reader could not measure at all."""
    findings: list[Finding] = []
    for injector, row in sorted(stats.items(), key=lambda kv: kv[0]):
        rate = row.reference_rate
        if rate is None:
            severity = Severity.INFO
            rate_text = "unmeasurable (no extractable rare token)"
            limit = LIMIT_NO_TOKEN
        else:
            severity = (
                Severity.COSTLY
                if row.with_token >= MIN_SAMPLE_FOR_SEVERITY and rate < LOW_RATE_THRESHOLD
                else Severity.INFO
            )
            rate_text = f"{rate * 100:.0f}% ({row.referenced}/{row.with_token})"
            limit = LIMIT_CORRELATION

        findings.append(
            Finding(
                id=f"injection-{injector}",
                check="INJECTION-VS-USE",
                severity=severity,
                summary=(
                    f"`{injector}`: {row.injections} injection(s) across {row.turns} turn(s), "
                    f"{row.injected_bytes:,} bytes, reference rate {rate_text}."
                ),
                snapshot=snapshot_id,
                evidence=(Evidence(run_id=f"transcripts {window}"),),
                detail=(
                    f"injections={row.injections} turns={row.turns} "
                    f"with_token={row.with_token} referenced={row.referenced} "
                    f"injected_bytes={row.injected_bytes}"
                    + (f" since={since}" if since else "")
                ),
                limit=limit,
            )
        )
    return findings


def render_table(stats: dict[str, InjectorStats]) -> str:
    lines = [
        f"{'injector':16s}{'injections':>11s}{'turns':>7s}{'w/token':>9s}"
        f"{'referenced':>11s}{'rate':>8s}{'bytes':>10s}",
    ]
    for injector, row in sorted(stats.items(), key=lambda kv: kv[0]):
        rate = row.reference_rate
        rate_text = "n/a" if rate is None else f"{rate * 100:.0f}%"
        lines.append(
            f"{injector:16s}{row.injections:>11d}{row.turns:>7d}{row.with_token:>9d}"
            f"{row.referenced:>11d}{rate_text:>8s}{row.injected_bytes:>10,d}"
        )
    return "\n".join(lines)


__all__ = [
    "INJECTOR_SIGNATURES",
    "LOW_RATE_THRESHOLD",
    "MIN_SAMPLE_FOR_SEVERITY",
    "MIN_TOKEN_LEN",
    "UNATTRIBUTED",
    "InjectionChunk",
    "InjectorStats",
    "attribute_injector",
    "build_findings",
    "compute_injection_stats",
    "extract_rare_tokens",
    "is_referenced",
    "render_table",
]
