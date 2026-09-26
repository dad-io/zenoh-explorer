"""2.5 SUPERSEDED and 2.7 DEADREF — does what the golden master cites still exist, and still rule?

Two failures with the same shape and different consequences. A citation that does not resolve
sends a reader nowhere. A citation that resolves to a *superseded* ruling sends them somewhere
wrong, which is worse, and is invisible to any check that only asks whether the file is there.

The distinction this module exists to draw: a superseded decision cited as HISTORY is correct
prose. CLAUDE.md:1164 reads "decision 0182, superseding decision 0173" — the one place the
document explains its own supersession, and a naive check would punish it. Only citation as
AUTHORITY is a defect.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from bearhug.lint.parse import parse_sections
from bearhug.model import Evidence, Finding, Severity

#: barracuda writes decision ids zero-padded to four digits: 0182, 0135.
DECISION_ID = re.compile(r"\b(0\d{3})\b")

#: `§4a`, `§ 12`. The document's own cross-reference form.
SECTION_REF = re.compile(r"§\s*(\d+[a-z]?)")

#: Marks the id IMMEDIATELY FOLLOWING it as historical. Scoped to the citation, not the line:
#: "decision 0148, superseding 0143" makes 0143 history and leaves 0148 an authority claim.
#: A line-scoped version of this test marked both as history and hid two real defects — 0148
#: at :913 and 0121 at :1142, each cited as the ruling that supersedes something else while
#: being superseded itself.
SUPERSEDES_NEXT = re.compile(
    r"(?:supersed(?:e|es|ed|ing)|replac(?:es|ed|ing)|retir(?:es|ed|ing)|rescind\w*|revok\w*"
    r"|withdraw\w*)\s+(?:by\s+)?(?:decision\s+)?$",
    re.IGNORECASE,
)

#: Whole-line markers that do put a citation in the past regardless of position.
HISTORICAL_LINE = re.compile(r"no longer|used to|formerly|previously|pre-\d", re.IGNORECASE)

#: A number inside a fence or a table rule is not a citation.
SKIP_LINE = re.compile(r"^\s*(```|\|[-: ]+\|)")

LIMIT_DEADREF = (
    "Proves the cited reference does not resolve within this snapshot. Does not prove it never "
    "existed — a decision may have been renumbered or a section renamed, in which case the "
    "citation is stale rather than invented."
)
LIMIT_SUPERSEDED_AUTHORITY = (
    "Proves the line cites a decision the corpus marks superseded, and that the surrounding "
    "line does not mark the citation as historical. Does not prove the rule the line states is "
    "wrong: a superseding decision often retains most of its predecessor. It proves a reader "
    "following the citation lands on a ruling that no longer governs."
)
LIMIT_SUPERSEDED_HISTORY = (
    "The line cites a superseded decision AND marks it as superseded. Reported as information "
    "so the check's reasoning is auditable, never as a defect — this is correct prose."
)


def _index_maps(index: dict[str, Any]) -> tuple[dict[str, str], dict[str, str | None]]:
    """id -> title, and id -> superseded_by, keyed the way CLAUDE.md writes ids."""
    titles: dict[str, str] = {}
    replaced: dict[str, str | None] = {}
    for record in index.get("records", []):
        raw = str(record.get("id", "")).strip()
        if not raw:
            continue
        key = raw.zfill(4)
        titles[key] = str(record.get("title", ""))
        successor = record.get("superseded_by")
        replaced[key] = str(successor).zfill(4) if successor else None
    return titles, replaced


def _citations(text: str):
    """Yield (line_number, line, decision_id, historical, suffix) per citation in prose.

    ``historical`` is decided per CITATION, not per line — see SUPERSEDES_NEXT.
    """
    for number, line in enumerate(text.splitlines(), start=1):
        if SKIP_LINE.match(line):
            continue
        occurrences: Counter[str] = Counter()
        for match in DECISION_ID.finditer(line):
            before = line[: match.start()]
            historical = bool(SUPERSEDES_NEXT.search(before)) or bool(HISTORICAL_LINE.search(line))
            decision = match.group(1)
            occurrences[decision] += 1
            suffix = f"-{occurrences[decision]}" if occurrences[decision] > 1 else ""
            yield number, line, decision, historical, suffix


def check_dead_refs(claude_md: str, *, index: dict[str, Any], snapshot: str) -> list[Finding]:
    """Every `§n` and `decision NNNN` the document cites must resolve.

    Filesystem paths are deliberately NOT checked here: 2.6 GATE-COVERAGE-REVERSE already owns
    that, and two checks over one fact is two authorities that will eventually disagree.
    """
    titles, _ = _index_maps(index)
    known_sections = {section.id for section in parse_sections(claude_md)}
    findings: list[Finding] = []

    for number, line, decision, _, suffix in _citations(claude_md):
        if decision in titles:
            continue
        findings.append(
            Finding(
                id=f"deadref-decision-{decision}-{number}{suffix}",
                check="DEADREF",
                severity=Severity.BROKEN,
                summary=f"CLAUDE.md cites decision {decision}, which is not in the corpus",
                snapshot=snapshot,
                evidence=(Evidence(file="CLAUDE.md", line=number, excerpt=line.strip()[:160]),),
                detail=f"{decision} cited at line {number}; corpus holds {len(titles)} records",
                limit=LIMIT_DEADREF,
            )
        )

    for number, line in enumerate(claude_md.splitlines(), start=1):
        if line.startswith("#") or SKIP_LINE.match(line):
            continue
        occurrences: Counter[str] = Counter()
        for match in SECTION_REF.finditer(line):
            ref = match.group(1)
            occurrences[ref] += 1
            suffix = f"-{occurrences[ref]}" if occurrences[ref] > 1 else ""
            if ref in known_sections:
                continue
            findings.append(
                Finding(
                    id=f"deadref-section-{ref}-{number}{suffix}",
                    check="DEADREF",
                    severity=Severity.BROKEN,
                    summary=f"CLAUDE.md cites §{ref}, which is not a section of this file",
                    snapshot=snapshot,
                    evidence=(Evidence(file="CLAUDE.md", line=number, excerpt=line.strip()[:160]),),
                    detail=f"§{ref} cited at line {number}; sections are {sorted(known_sections)}",
                    limit=LIMIT_DEADREF,
                )
            )
    return findings


def check_superseded(claude_md: str, *, index: dict[str, Any], snapshot: str) -> list[Finding]:
    """Flag a superseded decision cited as authority; note one cited as history."""
    titles, replaced = _index_maps(index)
    findings: list[Finding] = []

    for number, line, decision, historical, suffix in _citations(claude_md):
        successor = replaced.get(decision)
        if not successor:
            continue
        findings.append(
            Finding(
                id=f"superseded-{decision}-{number}{suffix}",
                check="SUPERSEDED",
                severity=Severity.INFO if historical else Severity.COSTLY,
                summary=(
                    f"CLAUDE.md cites superseded decision {decision} as history"
                    if historical
                    else f"CLAUDE.md cites superseded decision {decision} as authority"
                ),
                snapshot=snapshot,
                evidence=(Evidence(file="CLAUDE.md", line=number, excerpt=line.strip()[:160]),),
                detail=(
                    f"{decision} superseded by {successor} — {titles.get(successor, '?')[:70]}"
                ),
                limit=LIMIT_SUPERSEDED_HISTORY if historical else LIMIT_SUPERSEDED_AUTHORITY,
            )
        )
    return findings
