"""2.8 ANCHOR-SAFETY — does a proposed CLAUDE.md edit rename a heading memex cites by anchor?

Barracuda's memex records cite `CLAUDE.md` headings as `evidence:` anchors of the form
`CLAUDE.md#<heading text>`, and Barracuda's own `memexlint` check 3 resolves them at
pre-commit time. A patch that renames one of those headings reds that hook — this module is
the check bear-hug runs on ITS OWN proposed patches before it hands one to a human, so the
one place bear-hug could accidentally break Barracuda is caught here rather than downstream.

Measured against snapshot 2026-08-28b: five distinct headings are cited this way, by seven
citing records total (`0.` by 2, `1.` by 1, `4a.` by 2, `6.` by 1, `8a.` by 1). CHARTER.md's
prose says six — see `anchor_index`'s docstring and the report for the discrepancy. Neither
number is this module's problem to fix; it is what `anchor_index` proves against a live index.
"""

from __future__ import annotations

from typing import Any

from bearhug.lint.parse import parse_sections
from bearhug.model import Evidence, Finding, Severity

#: Evidence entries are `CLAUDE.md#<heading>` — a bare `CLAUDE.md` with no `#` cites the whole
#: file, not one heading, and is not an anchor `memexlint` check 3 resolves against a section.
ANCHOR_PREFIX = "CLAUDE.md#"

LIMIT_ANCHOR_SAFETY = (
    "This is a verdict on a PROPOSED edit, not a defect in the current CLAUDE.md — the anchor "
    "resolves fine today, which is exactly why a patch renaming it is dangerous rather than "
    "already broken. It proves the new heading text would fail Barracuda's own memexlint check "
    "3 for the named records, measured against THIS snapshot's memex index; a record added, "
    "edited, or retired after this snapshot renders the verdict stale."
)


def _heading_text(section) -> str:
    """The exact string a memex `evidence:` anchor cites: `<id>. <title>`, verbatim."""
    return f"{section.id}. {section.title}"


def anchor_index(index: dict[str, Any]) -> dict[str, list[str]]:
    """Every `CLAUDE.md#<heading>` anchor in ``index``, mapped to the record ids citing it.

    Only entries with a `#` count as an anchor into one heading; a bare `CLAUDE.md` citation
    (4 of the 11 `CLAUDE.md`-referencing `evidence:` entries in the 2026-08-28b index) cites
    the file as a whole and cannot be broken by renaming any single section.
    """
    hits: dict[str, set[str]] = {}
    for record in index.get("records", []):
        record_id = str(record.get("id", "")).strip()
        if not record_id:
            continue
        for entry in record.get("evidence", []):
            if not isinstance(entry, str) or not entry.startswith(ANCHOR_PREFIX):
                continue
            anchor = entry[len(ANCHOR_PREFIX) :]
            hits.setdefault(anchor, set()).add(record_id)

    def _sort_key(value: str) -> tuple[int, int, str]:
        return (0, int(value), "") if value.isdigit() else (1, 0, value)

    return {anchor: sorted(ids, key=_sort_key) for anchor, ids in hits.items()}


def broken_anchors(
    old_text: str, new_text: str, *, index: dict[str, Any]
) -> dict[str, list[str]]:
    """Anchors that resolve against ``old_text`` but not, verbatim, against ``new_text``.

    Shared by `check_anchor_safety` (below) and `report.patches` (6.2), so the patch emitter's
    SAFE/UNSAFE verdict and this check's findings can never disagree about which records break
    — two computations of the same fact is exactly the duplicate-authority trap docs/METHOD.md
    warns REFS.py about.
    """
    old_headings = {_heading_text(section) for section in parse_sections(old_text)}
    new_headings = {_heading_text(section) for section in parse_sections(new_text)}
    return {
        anchor: ids
        for anchor, ids in anchor_index(index).items()
        if anchor in old_headings and anchor not in new_headings
    }


def check_anchor_safety(
    old_text: str, new_text: str, *, index: dict[str, Any], snapshot: str
) -> list[Finding]:
    """BROKEN for every anchor a proposed edit (``old_text`` -> ``new_text``) would orphan.

    Silent on an anchor that never resolved against ``old_text`` in the first place — that is
    a DEADREF (2.7)'s finding, not this one's; conflating them would put the wrong mechanism
    in front of a reader (docs/METHOD.md's naming discipline for `_write_path_findings`).
    """
    findings: list[Finding] = []
    for anchor, record_ids in sorted(broken_anchors(old_text, new_text, index=index).items()):
        section_id = anchor.split(".", 1)[0].strip()
        records = ", ".join(record_ids)
        findings.append(
            Finding(
                id=f"anchor-safety-{section_id}",
                check="ANCHOR-SAFETY",
                severity=Severity.BROKEN,
                summary=(
                    f"proposed edit renames or removes heading {anchor!r}, cited by memex "
                    f"record(s) {records} — Barracuda's memexlint check 3 would red at "
                    f"pre-commit"
                ),
                snapshot=snapshot,
                evidence=(Evidence(file="CLAUDE.md", excerpt=anchor[:160]),),
                detail=f"anchor={anchor!r} records={record_ids}",
                limit=LIMIT_ANCHOR_SAFETY,
            )
        )
    return findings
