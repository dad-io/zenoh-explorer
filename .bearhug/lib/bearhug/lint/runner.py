"""2.9 — run every Phase 2 check against one snapshot and rank what comes back."""

from __future__ import annotations

import json
from pathlib import Path

from bearhug.lint.gates import check_gate_coverage
from bearhug.lint.reachability import Traffic, check_matcher_reachability
from bearhug.lint.refs import check_dead_refs, check_superseded
from bearhug.lint.stalecount import check_stale_counts
from bearhug.model import SEVERITY_ORDER, Evidence, Finding, Severity
from bearhug.read_optional import read_json_or_default, read_text_or_default

#: Mode 1 (docs/OPERATING-MODES.md) observes a project before any setup decision, and a project
#: that has never been set up has neither file yet — the expected state this INFO measurement
#: names, not a defect at any other severity. A snapshot only ever captures `project/`, never
#: the setup receipt (`snapshot/spec.py`'s PROJECT capture rules), so it cannot tell that
#: expected case apart from a project that WAS set up and later lost the file: the readiness
#: checks, which read the live subject directly rather than a snapshot, decide that.
LIMIT_INPUT_ABSENT = (
    "Proves this snapshot's project captured no {name}. Every check above still ran, against "
    "empty text or an empty settings object rather than being skipped, so a check's silence "
    "here means 'nothing to find in an empty {name}', not 'checked and clean'. Absence is the "
    "expected state of a project that has never been set up, and is equally what a project "
    "that WAS set up and later lost {name} would look like — this snapshot alone cannot tell "
    "the two apart. Whether this is a regression is decided by the readiness checks, which "
    "read the live subject directly rather than this snapshot."
)


def _absent_input_findings(
    *, claude_md_absent: bool, settings_absent: bool, snapshot: str
) -> list[Finding]:
    """One INFO finding per absent input, so a fresh project's report is not silently empty."""
    findings: list[Finding] = []
    if claude_md_absent:
        findings.append(
            Finding(
                id="input-absent-claude-md",
                check="INPUT-ABSENT",
                severity=Severity.INFO,
                summary="project/CLAUDE.md is absent from this snapshot",
                snapshot=snapshot,
                evidence=(Evidence(file="CLAUDE.md"),),
                detail=(
                    "No CLAUDE.md was captured; every CLAUDE.md-reading check above ran "
                    "against empty text."
                ),
                limit=LIMIT_INPUT_ABSENT.format(name="CLAUDE.md"),
            )
        )
    if settings_absent:
        findings.append(
            Finding(
                id="input-absent-settings-json",
                check="INPUT-ABSENT",
                severity=Severity.INFO,
                summary="project/.claude/settings.json is absent from this snapshot",
                snapshot=snapshot,
                evidence=(Evidence(file=".claude/settings.json"),),
                detail=(
                    "No .claude/settings.json was captured; every settings-reading check above "
                    "ran against zero hook registrations."
                ),
                limit=LIMIT_INPUT_ABSENT.format(name=".claude/settings.json"),
            )
        )
    return findings


def run_all_checks(snapshot_dir: Path | str, *, traffic: Traffic | None = None) -> list[Finding]:
    """Every static check, ranked broken-first.

    Findings cite the manifest's ``snapshot_id`` rather than the directory name: a label is not
    an identity, and `2026-08-28` named three different subject states in one afternoon.

    ``traffic`` is corpus-attributed input (M17). There is no default: a constant measured on
    2026-08-28 stood in here for a week and every reachability share quoted it as current.
    Without traffic, MATCHER-REACHABILITY reports its structural verdicts and one UNMEASURED row.

    A project observed before setup (mode 1, docs/OPERATING-MODES.md) has neither `CLAUDE.md`
    nor `.claude/settings.json` yet. Either absence is reported once as an INPUT-ABSENT finding;
    every other check below still runs, against empty text or an empty settings object, rather
    than raising.
    """
    snapshot_dir = Path(snapshot_dir)
    project = snapshot_dir / "project"
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id", snapshot_dir.name)

    claude_md, claude_md_absent = read_text_or_default(project / "CLAUDE.md")
    settings, settings_absent = read_json_or_default(project / ".claude" / "settings.json")
    index = json.loads((snapshot_dir / "memex-index.json").read_text(encoding="utf-8"))

    findings: list[Finding] = []
    findings += _absent_input_findings(
        claude_md_absent=claude_md_absent, settings_absent=settings_absent, snapshot=snapshot_id
    )
    findings += check_stale_counts(claude_md, snapshot=snapshot_id)
    findings += check_gate_coverage(
        settings=settings, claude_md=claude_md, project_root=project, snapshot=snapshot_id
    )
    findings += check_dead_refs(claude_md, index=index, snapshot=snapshot_id)
    findings += check_superseded(claude_md, index=index, snapshot=snapshot_id)
    findings += check_matcher_reachability(
        settings=settings, project_root=project, traffic=traffic, snapshot=snapshot_id,
    )
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.check, f.id))
