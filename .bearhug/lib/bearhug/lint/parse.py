"""2.1 — the section tree.

Every Phase 2 check addresses a claim *by section*, so this is the shared substrate: a stale
count belongs to a section, a restated directive is a directive in two sections, an uncovered
gate is a gate no section names. Parsing is deliberately shallow — headings and lead clauses,
not Markdown — because the thing under study is a rule's placement, not its typography.
"""

from __future__ import annotations

import re

from bearhug.model import Directive, Section

#: `## 0. Title` or `### 0a. Title`. The document's own `# CLAUDE.md` is not a section.
HEADING = re.compile(r"^(#{2,3})\s+(\d+[a-z]?)\.\s+(.*?)\s*$")

#: A top-level list item: `- x`, `* x`, or `1. x`, with no leading indent.
BULLET = re.compile(r"^(?:[-*]|\d+\.)\s+")

#: A directive is a bolded lead clause opening a list item — the unit REPEAT (2.3) clusters.
LEAD_CLAUSE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\*\*(.+?)\*\*")


def parse_sections(text: str) -> list[Section]:
    """Split a CLAUDE.md into its numbered sections, in document order.

    Sections tile the document: each runs to the line before the next heading, and the last
    runs to the end. A gap would be a place a directive could hide and be linted by nothing.
    """
    lines = text.splitlines()
    starts: list[tuple[int, str, str]] = []
    for number, line in enumerate(lines, start=1):
        match = HEADING.match(line)
        if match:
            starts.append((number, match.group(2), match.group(3)))

    sections: list[Section] = []
    for index, (line_start, section_id, title) in enumerate(starts):
        line_end = starts[index + 1][0] - 1 if index + 1 < len(starts) else len(lines)
        body = "\n".join(lines[line_start:line_end])
        section = Section(
            id=section_id,
            title=title,
            line_start=line_start,
            line_end=line_end,
            body=body,
        )
        section.directives = _directives(lines, line_start, line_end, section_id)
        sections.append(section)
    return sections


def _directives(
    lines: list[str], line_start: int, line_end: int, section_id: str
) -> list[Directive]:
    found: list[Directive] = []
    for offset, line in enumerate(lines[line_start:line_end], start=line_start + 1):
        match = LEAD_CLAUSE.match(line)
        if match:
            found.append(
                Directive(
                    text=match.group(1).strip(),
                    line=offset,
                    section_id=section_id,
                    emphasised=True,
                )
            )
    return found


def top_level_bullets(section: Section) -> int:
    """Count the section's own list items, ignoring nested ones.

    This is a *referent counter* for STALECOUNT (2.4): CLAUDE.md's §0 asserts a number of
    one-liners in prose, and this is the thing that number is a claim about.
    """
    return sum(1 for line in section.body.splitlines() if BULLET.match(line))
