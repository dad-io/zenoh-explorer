"""2.4 STALECOUNT — prose asserting a count of something countable in this file.

The fixture is barracuda's `"Eleven one-liners"` describing a §0 that holds sixteen. The class
is wider than that one line: a number written into prose has no mechanism keeping it true, so
it decays silently the moment the thing it counts changes.

Two outputs, deliberately. A resolved mismatch is a COSMETIC finding — wrong on the page,
changes no behaviour. An *unresolved* countable claim is an INFO finding, because a claim this
check cannot evaluate is a gap in the check, and dropping it would let silence read as assent.
"""

from __future__ import annotations

import re

from bearhug.lint.parse import parse_sections, top_level_bullets
from bearhug.model import Evidence, Finding, Severity

NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty-four": 24,
}

#: A number, then the thing it counts. Kept tight: two words of referent, lowercase nouns.
_WORDS = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
CLAIM = re.compile(
    rf"\b(?P<number>{_WORDS}|\d{{1,3}})\s+(?P<referent>[a-z][a-z-]*(?:\s+[a-z][a-z-]*)?)\b",
    re.IGNORECASE,
)

#: Referent phrases we can actually count, mapped to how. Anything else is reported unresolved.
COUNTABLE = ("one-liner", "one-liners")

#: Words that mean the match caught a sentence, not a countable noun phrase. Without this the
#: check emitted 204 unresolved claims against 1 real one — a check that noisy is ignored, which
#: is the dilution trap CLAUDE.md §10 names.
FUNCTION_WORDS = frozenset(
    [
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "can", "could", "did", "do",
    "does", "for", "from", "has", "have", "here", "in", "is", "it", "its", "may", "might",
    "must", "no", "not", "of", "on", "or", "our", "per", "should", "than", "that", "the",
    "their", "then", "there", "they", "this", "to", "was", "we", "were", "when", "where",
    "which", "who", "whom", "whose", "will", "with", "would", "you", "your",
    ]
)

#: Inline code carries markers like `§0 re-read`, which is a cross-reference, not a count.
CODE_SPAN = re.compile(r"`[^`]*`")

#: `§4` and `#4` are section references. The number names a section, it does not count one.
SECTION_REF = re.compile(r"[§#]\s*$")

#: Prose only. A number inside a fence or a link is not a claim about the document.
SKIP_LINE = re.compile(r"^\s*(```|\||>)")

LIMIT_RESOLVED = (
    "Proves the number in prose disagrees with the count of its referent in this file. Does "
    "not prove which is wrong: the prose may be stale, or the list may have grown past what "
    "the rule intended."
)
LIMIT_UNRESOLVED = (
    "A countable claim this check has no counter for. Reported so the check's silence is not "
    "read as agreement; it is not a defect in the document until someone counts the referent."
)


def _value(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def _is_noun_phrase(referent: str) -> bool:
    """Is this plausibly a thing one could count, rather than the rest of a sentence?"""
    words = referent.split()
    if any(word in FUNCTION_WORDS for word in words):
        return False
    tail = words[-1]
    return len(tail) >= 3 and (tail.endswith("s") or "-" in tail)


def check_stale_counts(text: str, *, snapshot: str, path: str = "CLAUDE.md") -> list[Finding]:
    """Find numbers in prose that disagree with what they count."""
    sections = parse_sections(text)
    section_zero = next((s for s in sections if s.id == "0"), None)
    findings: list[Finding] = []

    for number, raw_line in enumerate(text.splitlines(), start=1):
        if raw_line.startswith("#") or SKIP_LINE.match(raw_line):
            continue
        # Blank out code spans rather than dropping them, so column offsets stay honest.
        line = CODE_SPAN.sub(lambda m: " " * len(m.group(0)), raw_line)
        for match in CLAIM.finditer(line):
            if SECTION_REF.search(line[: match.start()]):
                continue
            claimed = _value(match.group("number"))
            referent = match.group("referent").lower().strip()
            if claimed is None or not _is_noun_phrase(referent):
                continue

            if referent in COUNTABLE and section_zero is not None:
                actual = top_level_bullets(section_zero)
                if actual == claimed:
                    continue
                findings.append(
                    Finding(
                        id=f"stalecount-claude-md-{number}",
                        check="STALECOUNT",
                        severity=Severity.COSMETIC,
                        summary=f"prose claims {claimed} {referent}; §0 holds {actual}",
                        snapshot=snapshot,
                        evidence=(Evidence(file=path, line=number, excerpt=raw_line.strip()),),
                        detail=(
                            f"§0 spans lines {section_zero.line_start}-{section_zero.line_end} "
                            f"and holds {actual} top-level bullets. The prose says {claimed}."
                        ),
                        limit=LIMIT_RESOLVED,
                    )
                )
            elif referent not in COUNTABLE:
                findings.append(
                    Finding(
                        id=f"stalecount-unresolved-claude-md-{number}-{referent.replace(' ', '-')}",
                        check="STALECOUNT",
                        severity=Severity.INFO,
                        summary=f"countable claim with no counter: {claimed} {referent}",
                        snapshot=snapshot,
                        evidence=(Evidence(file=path, line=number, excerpt=raw_line.strip()),),
                        limit=LIMIT_UNRESOLVED,
                    )
                )
    return findings
