"""E03 — one trimmed CLAUDE.md candidate, built as an EVAL artifact under E02's criteria.

E02 (Sam, 2026-09-02): cold-session budget ≤ 12,000 estimated tokens; every lead-clause directive
of the source preserved (107 of 107, or each omission named and ruled); every memex-cited heading
preserved verbatim; §6–§11 prose may move to selectively loaded detail files; the candidate cites
the generated gate inventory rather than a hand list; prime-eval non-regression Δ ≤ 1 case.

The build is mechanical and deterministic: keep the top matter, every section heading, and every
directive's lead line; then, budget permitting, each directive's indented continuation lines in
document order; a movable section's remaining prose goes verbatim to `detail/section-<id>.md`
and the section stub points at it. Nothing is paraphrased. A directive is never traded for the
budget — a candidate over budget is reported as over budget, not silently thinned further.

This is not a Barracuda patch and claims nothing about being better (E02, rule 7.5). H01 may
propose it only after E11's repeated runs and the machinery cases hold. The prime-eval half of the
criteria needs API access this machine does not have; the manifest says so rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from bearhug.lint.anchors import anchor_index, broken_anchors
from bearhug.lint.budget import BYTES_PER_TOKEN, estimate_tokens
from bearhug.lint.parse import parse_sections

RULING = "E02 — Sam, 2026-09-02"
DEFAULT_MAX_TOKENS = 12_000
MOVABLE_SECTIONS: tuple[str, ...] = ("6", "7", "8", "8a", "9", "10", "11")
_PLACEHOLDER = re.compile(r"\b(TODO|TBD|FIXME|XXX|PLACEHOLDER)\b")
_CONTINUATION = re.compile(r"^\s{2,}\S")
#: The two pointer forms Barracuda's memex-lint check 7a accepts in an origin file: a decision
#: whose `sources:` names CLAUDE.md fails the lint unless the file still carries one of them.
_POINTER_LIST = re.compile(r"(?:decision|decisions)\s+((?:\d{4}[\s,]*(?:and\s+)?)+)|→\s*(\d{4})\b")
_POINTER_PATH = re.compile(r"decisions/\d{4}")


def _pointers(text: str) -> list[str]:
    """Every pointer memex-lint 7a would count in ``text``, each as `decision NNNN` or
    `decisions/NNNN`, in order of first appearance. The lint collapses whitespace, so a list
    `(→ decisions 0270,\n  0273)` and a wrapped `decision\n  0119` both count for every id."""
    found: list[str] = []
    for match in _POINTER_LIST.finditer(text):
        ids = re.findall(r"\d{4}", match.group(1) or match.group(2) or "")
        found.extend(f"decision {i}" for i in ids)
    found.extend(_POINTER_PATH.findall(text))
    return list(dict.fromkeys(found))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _continuation(lines: list[str], index: int) -> list[str]:
    """Indented lines following a bullet at ``index`` (0-based), stopping at the first line that
    is neither indented nor blank-inside-the-block."""
    out: list[str] = []
    i = index + 1
    while i < len(lines) and _CONTINUATION.match(lines[i]):
        out.append(lines[i])
        i += 1
    return out


def build_trimmed_candidate(
    snapshot_dir: Path | str,
    *,
    directives: list[dict[str, Any]],
    out_dir: Path | str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    movable_sections: tuple[str, ...] = MOVABLE_SECTIONS,
) -> dict[str, Any]:
    snapshot = Path(snapshot_dir)
    source_path = snapshot / "project" / "CLAUDE.md"
    source_bytes = source_path.read_bytes()
    text = source_bytes.decode("utf-8")
    if _PLACEHOLDER.search(text):
        raise ValueError(
            "the source CLAUDE.md carries placeholder text (TODO/TBD/FIXME/XXX/PLACEHOLDER); a "
            "candidate built from it would carry it too — refused"
        )
    index_path = snapshot / "memex-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
    manifest_path = snapshot / "manifest.json"
    snapshot_id = (
        json.loads(manifest_path.read_text(encoding="utf-8")).get("snapshot_id", snapshot.name)
        if manifest_path.is_file()
        else snapshot.name
    )

    lines = text.splitlines()
    sections = parse_sections(text)
    max_bytes = int(max_tokens * BYTES_PER_TOKEN)
    out = Path(out_dir)
    detail_dir = out / "detail"

    # --- locate every directive's lead line in the source ---------------------------------------
    by_section: dict[str, list[tuple[dict[str, Any], int]]] = {}
    missing: list[str] = []
    for row in directives:
        statement = str(row.get("statement", ""))
        line_no = int(row.get("line", 0))
        found: int | None = None
        if 1 <= line_no <= len(lines) and statement in lines[line_no - 1]:
            found = line_no - 1
        else:
            section = next((s for s in sections if s.id == row.get("section")), None)
            span = range(section.line_start, section.line_end) if section else range(len(lines))
            for i in span:
                if f"**{statement}**" in lines[i]:
                    found = i
                    break
        if found is None:
            missing.append(str(row.get("id", statement)))
            continue
        by_section.setdefault(str(row.get("section", "")), []).append((row, found))

    # --- phase 1: what is kept unconditionally --------------------------------------------------
    first_heading = sections[0].line_start - 1 if sections else len(lines)
    top = list(lines[:first_heading])
    while top and not top[-1].strip():
        top.pop()
    # Each section is a heading plus blocks; a block is a directive's lead line and its
    # continuation lines, rendered together or (over budget) as the lead line alone.
    stubs: list[dict[str, Any]] = []
    detail_files: list[str] = []
    for section in sections:
        blocks: list[dict[str, Any]] = []
        for _row, i in sorted(by_section.get(section.id, []), key=lambda pair: pair[1]):
            blocks.append({
                "lead": lines[i],
                "continuation": [] if section.id in movable_sections else _continuation(lines, i),
                "keep_continuation": False,
            })
        pointer = None
        if section.id in movable_sections:
            detail = detail_dir / f"section-{section.id}.md"
            detail_dir.mkdir(parents=True, exist_ok=True)
            detail.write_text(
                "\n".join(lines[section.line_start - 1:section.line_end]).rstrip() + "\n",
                encoding="utf-8",
            )
            detail_files.append(f"detail/section-{section.id}.md")
            pointer = (
                f"_Detail moved to `detail/section-{section.id}.md` (E02: this section may load "
                "selectively). The directives above are the section's rules; the file holds the "
                "prose around them, verbatim._"
            )
        # Decision pointers are the rule's authority trail (memex-lint 7a); every one the section
        # carried survives, in the section that carried it, even when the prose around it goes.
        section_text = "\n".join(lines[section.line_start - 1:section.line_end])
        leads_text = "\n".join(block["lead"] for block in blocks)
        carried = _pointers(section_text)
        orphaned = [p for p in carried if p not in leads_text]
        pointers_line = (
            "_Decisions this section cites (kept for memex-lint 7a): " + ", ".join(orphaned) + "._"
            if orphaned else None
        )
        stubs.append({
            "heading": lines[section.line_start - 1], "blocks": blocks, "pointer": pointer,
            "pointers_line": pointers_line, "carried": carried,
        })

    def render() -> str:
        parts = ["\n".join(top).rstrip(), ""]
        parts.append(
            "> Trimmed candidate — an EVAL artifact built by bear-hug under E02 (Sam, "
            "2026-09-02) from the snapshot's CLAUDE.md. Every bolded lead clause of the source is "
            "kept verbatim; explanatory prose is dropped or moved to `detail/`. Gate facts come "
            "from `docs/GATE-INVENTORY.generated.md`, not a hand list. Not a Barracuda patch."
        )
        parts.append("")
        for stub in stubs:
            parts.append(stub["heading"])
            parts.append("")
            for block in stub["blocks"]:
                parts.append(block["lead"])
                if block["keep_continuation"]:
                    parts.extend(block["continuation"])
            if stub["pointers_line"]:
                parts.append(stub["pointers_line"])
            if stub["pointer"]:
                parts.append(stub["pointer"])
            parts.append("")
        return "\n".join(parts).rstrip() + "\n"

    # --- phase 2: continuation lines, in document order, while the budget allows ---------------
    candidate = render()
    for stub in stubs:
        for block in stub["blocks"]:
            if not block["continuation"]:
                continue
            block["keep_continuation"] = True
            trial = render()
            if len(trial.encode("utf-8")) <= max_bytes:
                candidate = trial
            else:
                block["keep_continuation"] = False

    out.mkdir(parents=True, exist_ok=True)
    (out / "CLAUDE.md").write_text(candidate, encoding="utf-8")
    candidate_bytes = candidate.encode("utf-8")
    carried_pointers = _pointers(text)
    kept_pointers = [p for p in carried_pointers if p in candidate]
    broken = sorted(broken_anchors(text, candidate, index=index))
    cited = len(anchor_index(index))
    manifest = {
        "schema_version": "1",
        "ruling": RULING,
        "source": {
            "snapshot_id": snapshot_id,
            "claude_md_sha256": _sha(source_bytes),
            "bytes": len(source_bytes),
            "estimated_tokens": round(estimate_tokens(len(source_bytes))),
        },
        "candidate_sha256": _sha(candidate_bytes),
        "bytes": len(candidate_bytes),
        "estimated_tokens": round(estimate_tokens(len(candidate_bytes))),
        "estimator": f"stdlib heuristic, {BYTES_PER_TOKEN:g} bytes per token (lint.budget)",
        "directives": {"source": len(directives), "kept": len(directives) - len(missing),
                       "missing": sorted(missing)},
        "anchors": {"cited": cited, "broken": broken},
        "decision_pointers": {"source": len(carried_pointers), "kept": len(kept_pointers),
                              "missing": sorted(set(carried_pointers) - set(kept_pointers))},
        "movable_sections": list(movable_sections),
        "detail_files": detail_files,
        "criteria": {
            "budget": {"max_tokens": max_tokens,
                       "met": estimate_tokens(len(candidate_bytes)) <= max_tokens},
            "directives": {"met": not missing},
            "anchors": {"met": not broken},
            "decision_pointers": {"met": len(kept_pointers) == len(carried_pointers)},
            "gate_inventory_cited": {"met": "GATE-INVENTORY.generated.md" in candidate},
            "prime_eval": {
                "status": "unmeasured",
                "reason": "prime-eval runs models through API access this machine does not have; "
                "E02's Δ ≤ 1 case is not claimed",
            },
        },
        "limits": [
            "Estimated tokens, not a tokenizer count; the ratio to the source is the reliable "
            "part.",
            "Kept text is verbatim source text; nothing is paraphrased, so a dropped explanation "
            "is a dropped explanation, not a reworded one.",
            "An eval artifact: no claim of equivalence or superiority until E11's repeated runs "
            "and the machinery cases hold (H01).",
        ],
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest



__all__ = ["DEFAULT_MAX_TOKENS", "MOVABLE_SECTIONS", "RULING", "build_trimmed_candidate"]
