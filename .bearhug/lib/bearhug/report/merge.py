"""6.1 `bearhug report` — built phase findings merged into one ranked list.

Two authorities already exist: `lint.runner.run_all_checks` (static) and `hooks.audit.audit`
(dynamic). This module does not re-derive either — it calls both against the same snapshot and
concatenates, so a merge bug can only ever be an ordering bug, never a second implementation of
what either phase already decided.

`--compare` (6.4's dependency) diffs two runs by `Finding.id`, never by list position: a check
that finds N things in one order and the same N in another order is not "everything new" just
because sorting shuffled it, and plan 6.4 needs resolved/new/unchanged to survive re-ranking.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.hooks import audit, build_fixture_repo
from bearhug.lint import Traffic, run_all_checks
from bearhug.model import SEVERITY_ORDER, Evidence, Finding, Severity
from bearhug.paths import REPORTS_DIR, assert_writable
from bearhug.report.emit import render_markdown, write_findings
from bearhug.snapshot.drift import DriftReport


def _snapshot_id(snapshot_dir: Path) -> str:
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    return manifest.get("snapshot_id", snapshot_dir.name)


_REPLAY_DIGEST = re.compile(r"sha256=([0-9a-f]{64})")


def assert_same_corpus(traffic: Traffic | None, replay_findings: list[Finding] | None) -> None:
    """Lint's traffic and the replay findings must come from ONE corpus selection.

    The replay CORPUS-SCOPE finding carries the digest it read; traffic carries the digest it was
    counted from. Two digests in one report would present shares from one set of transcripts
    beside rates from another, and nothing downstream could tell.
    """
    if traffic is None or not replay_findings:
        return
    for finding in replay_findings:
        if not finding.id.startswith("replay-corpus-"):
            continue
        match = _REPLAY_DIGEST.search(finding.detail or "")
        if match and match.group(1) != traffic.corpus_digest:
            raise ValueError(
                f"lint traffic was counted from corpus {traffic.corpus_digest[:12]} but the "
                f"replay findings read corpus {match.group(1)[:12]}; one report, one corpus"
            )


def build_report(
    snapshot_dir: Path | str,
    *,
    traffic: Traffic | None = None,
    replay_findings: list[Finding] | None = None,
    eval_findings: list[Finding] | None = None,
) -> tuple[list[Finding], str]:
    """Run deterministic phases and merge supplied corpus/eval observations.

    Phase 3 needs a live disposable repo to run the gates against (see docs/CHARTER.md — never
    the real project-barracuda), so this builds one in a fresh temp dir per call, exactly as
    `cli._cmd_hooks` does. Two callers building two scratch repos costs a few subprocess calls
    against a ~15-file fixture repo; a shared one would make one report's mutations visible to
    the next, which is the exact hazard `hooks.audit.run_battery` rebuilds per-fixture to avoid.
    """
    snapshot_dir = Path(snapshot_dir)
    snapshot_id = _snapshot_id(snapshot_dir)
    assert_same_corpus(traffic, replay_findings)

    findings = list(run_all_checks(snapshot_dir, traffic=traffic))

    scratch = Path(tempfile.mkdtemp(prefix="bearhug-report-"))
    repo = build_fixture_repo(scratch / "repo")
    hook_findings, _results = audit(snapshot_dir, repo, snapshot_id=snapshot_id)
    findings += hook_findings

    for phase, supplied in (("replay", replay_findings), ("eval", eval_findings)):
        if supplied:
            wrong = [finding for finding in supplied if finding.snapshot != snapshot_id]
            if wrong:
                raise ValueError(
                    f"{phase} findings target a different snapshot: "
                    f"{', '.join(sorted({finding.snapshot for finding in wrong}))}"
                )
            findings += supplied
        else:
            findings.append(
                Finding(
                    id=f"phase-coverage-{phase}",
                    check="PHASE-COVERAGE",
                    severity=Severity.COSTLY,
                    summary=f"{phase} contributed no findings to this merged report.",
                    snapshot=snapshot_id,
                    evidence=(Evidence(run_id=f"merged-report:{snapshot_id}"),),
                    detail=(
                        "No persisted eval runs matched this snapshot."
                        if phase == "eval"
                        else "No explicitly resolved transcript corpus was supplied."
                    ),
                    limit=(
                        "This is a coverage gap, not evidence that the phase would find no "
                        "defects. The report names the missing input so an incomplete merge "
                        "cannot look complete."
                    ),
                )
            )

    ids = [finding.id for finding in findings]
    duplicates = sorted(finding_id for finding_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate finding ids across merged phases: {', '.join(duplicates)}")
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.check, f.id))
    return findings, snapshot_id


def write_report(
    snapshot_dir: Path | str,
    *,
    traffic: Traffic | None = None,
    replay_findings: list[Finding] | None = None,
    eval_findings: list[Finding] | None = None,
    report_label: str | None = None,
    findings_dir: Path | str | None = None,
    reports_dir: Path | str | None = None,
    drift: DriftReport | None = None,
) -> tuple[Path, Path]:
    """Write `reports/report-<snapshot_id>.md` and `findings/report-<snapshot_id>.json`.

    Reuses `report.emit.render_markdown` / `write_findings` rather than a second renderer — a
    merged report reads exactly like a single-phase one, because it is the same artifact over a
    longer findings list.

    S05 (Sam, 2026-09-02): the report runs drift FIRST. When a captured harness file has moved
    since the snapshot, the artifact is marked STALE at the top of the JSON and the markdown;
    HEAD movement with no captured-file movement stays informational (existing drift semantics).
    """
    findings, snapshot_id = build_report(
        snapshot_dir,
        traffic=traffic,
        replay_findings=replay_findings,
        eval_findings=eval_findings,
    )

    label = (
        "".join(character if character.isalnum() or character in "-_" else "-"
                for character in report_label)
        if report_label
        else None
    )
    prefix = f"report-{label}" if label else "report"
    stale = bool(drift is not None and drift.moved)
    drift_block: dict[str, Any] = (
        {
            "status": "carried",
            "snapshot": drift.snapshot,
            "moved": drift.moved,
            "head_moved": drift.head_moved,
            "changed": list(drift.changed),
            "added": list(drift.added),
            "removed": list(drift.removed),
            "stored_head": drift.stored_head,
            "live_head": drift.live_head,
        }
        if drift is not None
        else {"status": "not-checked", "reason": "no drift report supplied to write_report"}
    )
    json_path = write_findings(
        findings, out_dir=findings_dir, snapshot_id=snapshot_id, prefix=prefix,
        extra={"drift": drift_block, "stale": stale},
    )

    directory = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    safe = snapshot_id.replace("@", "-at-").replace("/", "-")
    md_name = f"report-{label}-{safe}.md" if label else f"report-{safe}.md"
    md_path = assert_writable(directory / md_name)
    corpus_scope = next((f.summary for f in findings if f.check == "CORPUS-SCOPE"), None)
    if stale and drift is not None:
        moved = len(drift.changed) + len(drift.added) + len(drift.removed)
        banner = (
            f"**STALE — {moved} captured harness file(s) moved since this snapshot** "
            f"({', '.join((drift.changed + drift.added + drift.removed)[:6])}"
            f"{', …' if moved > 6 else ''}). Every finding below was measured against the "
            "snapshot, not the live harness; re-run the phase or take a new snapshot (S05)."
        )
    elif drift is not None and drift.head_moved:
        banner = (
            f"Subject HEAD moved {str(drift.stored_head)[:12]} → {str(drift.live_head)[:12]}; "
            "no captured harness file changed, so the findings still hold (informational, S05)."
        )
    elif drift is not None:
        banner = "Drift checked: the live harness matches this snapshot."
    else:
        banner = "Drift not checked for this report."
    md_path.write_text(
        render_markdown(
            findings,
            snapshot_id=snapshot_id,
            title="bear-hug merged report",
            intro=f"{banner}\n\n**Corpus:** {corpus_scope or 'replay not supplied'}",
        ),
        encoding="utf-8",
    )

    return json_path, md_path


@dataclass(frozen=True, slots=True)
class ReportDiff:
    """What changed between two reports, by stable `Finding.id` — never by list position."""

    old_snapshot: str
    new_snapshot: str
    resolved: tuple[Finding, ...]  # present in OLD, gone from NEW
    new: tuple[Finding, ...]  # present in NEW, absent from OLD
    unchanged: tuple[str, ...]  # ids present in both

    def render(self) -> str:
        lines = [
            f"# bear-hug report --compare {self.old_snapshot} {self.new_snapshot}",
            "",
            f"resolved {len(self.resolved)}  new {len(self.new)}  unchanged {len(self.unchanged)}",
            "",
        ]
        if self.resolved:
            lines += ["## resolved (in OLD, gone from NEW)", ""]
            lines += [f"- `{f.id}` — {f.summary}" for f in self.resolved]
            lines.append("")
        if self.new:
            lines += ["## new (in NEW, absent from OLD)", ""]
            lines += [f"- `{f.id}` — {f.summary}" for f in self.new]
            lines.append("")
        if not self.resolved and not self.new:
            lines += [
                "No difference by finding id. docs/METHOD.md: a snapshot pair bounds a "
                "window, never a day — an empty diff says the two SNAPSHOTS agree, not that "
                "nothing happened in between; check what the window between them actually "
                'covers before reading this as "no change".',
                "",
            ]
        return "\n".join(lines)


def compare_reports(old_snapshot_dir: Path | str, new_snapshot_dir: Path | str) -> ReportDiff:
    """6.4's dependency: resolved / new / unchanged between two snapshots, by finding id.

    Deliberately re-runs both phases against each snapshot rather than reading back a
    previously-written `findings/report-*.json` — a stale JSON on disk from an old snapshot
    label would silently compare the wrong thing, and 0182's no-second-authority principle
    means the live check is the only authority bear-hug trusts about itself.
    """
    old_findings, old_id = build_report(old_snapshot_dir)
    new_findings, new_id = build_report(new_snapshot_dir)

    old_by_id = {f.id: f for f in old_findings}
    new_by_id = {f.id: f for f in new_findings}

    resolved = tuple(f for fid, f in old_by_id.items() if fid not in new_by_id)
    added = tuple(f for fid, f in new_by_id.items() if fid not in old_by_id)
    unchanged = tuple(sorted(set(old_by_id) & set(new_by_id)))

    return ReportDiff(
        old_snapshot=old_id,
        new_snapshot=new_id,
        resolved=resolved,
        new=added,
        unchanged=unchanged,
    )


__all__ = ["ReportDiff", "build_report", "compare_reports", "write_report"]
