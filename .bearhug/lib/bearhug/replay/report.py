"""Build one replay artifact from metrics, effectiveness, and injection analyses."""

from __future__ import annotations

import json
from pathlib import Path

from bearhug.model import SEVERITY_ORDER, Evidence, Finding, Severity
from bearhug.paths import CORPUS_DIR, REPORTS_DIR, assert_writable
from bearhug.replay.corpus import CorpusSelection
from bearhug.replay.dlv import build_dlv_depth_findings, compute_dlv_depth
from bearhug.replay.field_census import build_findings as build_field_census_findings
from bearhug.replay.field_census import census_fields
from bearhug.replay.injection import build_findings as build_injection_findings
from bearhug.replay.injection import compute_injection_stats
from bearhug.replay.ledger import build_findings as build_ledger_findings
from bearhug.replay.ledger import compute_ledger
from bearhug.replay.metrics import corpus_metrics
from bearhug.report.emit import render_markdown, write_findings


def build_replay_findings(
    corpus: CorpusSelection, *, snapshot_id: str, since: str | None = None
) -> list[Finding]:
    """Run every built replay analysis over one already-resolved corpus."""
    paths = list(corpus.paths)
    metrics = corpus_metrics(paths, since=since)
    start, end = metrics.date_range
    window = f"{start}..{end}" if start else "no dated events"
    if since:
        window = f"{since}..{end} (--since {since})"

    findings = [
        Finding(
            id=f"replay-corpus-{corpus.kind}",
            check="CORPUS-SCOPE",
            severity=Severity.INFO,
            summary=(
                f"Replay used {corpus.kind} corpus {corpus.digest[:12]}: "
                f"{len(metrics.sessions)} sessions and {len(metrics.subagents)} subagents."
            ),
            snapshot=snapshot_id,
            evidence=(Evidence(run_id=f"{corpus.label} {window}"),),
            detail=(
                f"source={corpus.source} sha256={corpus.digest} files={len(paths)} "
                f"excluded={len(corpus.excluded)} "
                f"human_turns={metrics.total_human_turns} "
                f"system_user_turns={metrics.total_system_turns} "
                f"subagent_turns={metrics.total_subagent_turns} "
                f"tool_calls={sum(metrics.tool_histogram().values())}"
            ),
            limit=(
                "The corpus identity proves which bytes this run read, not that retained "
                "transcripts are complete. Live files can be pruned; frozen files stop at the "
                "archive date. Session history also does not identify the exact harness version "
                "active at every historical turn."
            ),
        )
    ]
    for scope, tokens in (
        ("sessions", metrics.session_tokens),
        ("subagents", metrics.subagent_tokens),
    ):
        coverage = tokens.coverage_percentage()
        findings.append(
            Finding(
                id=f"replay-token-metrics-{scope}",
                check="TOKEN-METRICS",
                severity=Severity.INFO,
                summary=(
                    f"{scope} report four explicit token components at "
                    f"{coverage:.1f}% complete-message coverage."
                    if coverage is not None
                    else f"{scope} contain no assistant messages with measurable token usage."
                ),
                snapshot=snapshot_id,
                evidence=(Evidence(run_id=f"{corpus.label} {window}"),),
                detail=json.dumps(tokens.as_dict(), sort_keys=True),
                limit=(
                    "Token components come only from top-level message.usage. Repeated records "
                    "with one message.id are cumulative snapshots collapsed by per-component "
                    "maximum; usage.iterations is not counted again. Component totals are not a "
                    "price or a context-window high-water mark."
                ),
            )
        )
    for scope, durations in (
        ("sessions", metrics.session_durations),
        ("subagents", metrics.subagent_durations),
    ):
        coverage = durations.transcript_coverage_percentage
        findings.append(
            Finding(
                id=f"replay-duration-metrics-{scope}",
                check="DURATION-METRICS",
                severity=Severity.INFO,
                summary=(
                    f"{scope} have timestamp spans for "
                    f"{durations.transcripts_with_span}/{durations.transcripts} transcripts "
                    f"({coverage:.1f}% coverage)."
                    if coverage is not None
                    else f"{scope} contain no transcript with a measurable timestamp span."
                ),
                snapshot=snapshot_id,
                evidence=(Evidence(run_id=f"{corpus.label} {window}"),),
                detail=json.dumps(durations.as_dict(), sort_keys=True),
                limit=(
                    "This is max(record timestamp) minus min(record timestamp), not model/API "
                    "latency. Long idle gaps are retained. Summed per-transcript spans may "
                    "overlap and therefore are not elapsed wall time. M01 found duration-named "
                    "fields but did not establish their semantics, so they are not aggregated."
                ),
            )
        )
    for scope, compactions in (
        ("sessions", metrics.session_compactions),
        ("subagents", metrics.subagent_compactions),
    ):
        findings.append(
            Finding(
                id=f"replay-compaction-metrics-{scope}",
                check="COMPACTION-METRICS",
                severity=Severity.INFO,
                summary=(
                    f"{scope} contain {compactions.explicit_compactions} explicit "
                    "system.compactMetadata compaction records."
                ),
                snapshot=snapshot_id,
                evidence=(Evidence(run_id=f"{corpus.label} {window}"),),
                detail=json.dumps(compactions.as_dict(), sort_keys=True),
                limit=compactions.as_dict()["precision_limit"],
            )
        )
    field_census = census_fields(paths, since=since)
    findings += build_field_census_findings(
        field_census, snapshot_id=snapshot_id, corpus_label=corpus.label
    )
    ledger = compute_ledger(paths, since=since)
    findings += build_ledger_findings(ledger, snapshot_id=snapshot_id, since=since)
    depth = compute_dlv_depth(paths, since=since)
    findings += build_dlv_depth_findings(depth, snapshot_id=snapshot_id, corpus_label=corpus.label)
    injection = compute_injection_stats(paths, since=since)
    findings += build_injection_findings(
        injection,
        snapshot_id=snapshot_id,
        since=since,
        window=window,
    )
    findings.sort(key=lambda finding: (SEVERITY_ORDER[finding.severity], finding.check, finding.id))
    return findings


def write_replay_report(
    corpus: CorpusSelection,
    *,
    snapshot_id: str,
    since: str | None = None,
    findings_dir: Path | str | None = None,
    reports_dir: Path | str | None = None,
    corpus_dir: Path | str | None = None,
) -> tuple[Path, Path, Path]:
    """Write JSON findings, readable Markdown, and the corpus manifest for this run."""
    findings = build_replay_findings(corpus, snapshot_id=snapshot_id, since=since)
    scope = f"{corpus.kind}-{corpus.digest[:12]}"
    if since:
        scope += f"-since-{since}"
    json_path = write_findings(
        findings, out_dir=findings_dir, snapshot_id=snapshot_id, prefix=f"replay-{scope}"
    )
    safe = snapshot_id.replace("@", "-at-").replace("/", "-")
    report_root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    report_root.mkdir(parents=True, exist_ok=True)
    md_path = assert_writable(report_root / f"replay-{scope}-{safe}.md")
    md_path.write_text(
        render_markdown(
            findings,
            snapshot_id=snapshot_id,
            title=f"bear-hug replay ({corpus.label})",
        ),
        encoding="utf-8",
    )

    manifest_root = Path(corpus_dir) if corpus_dir is not None else CORPUS_DIR
    manifest_root.mkdir(parents=True, exist_ok=True)
    manifest_path = assert_writable(manifest_root / f"manifest-{scope}-{safe}.json")
    manifest_path.write_text(
        json.dumps(
            {
                "corpus": corpus.kind,
                "source": str(corpus.source),
                "sha256": corpus.digest,
                "files": len(corpus.paths),
                "members": [
                    {
                        "path": member.relpath,
                        "sha256": member.sha256,
                        "bytes": member.bytes,
                    }
                    for member in corpus.members
                ],
                "excluded": [
                    {"path": relpath, "reason": reason} for relpath, reason in corpus.excluded
                ],
                "snapshot": snapshot_id,
                "since": since,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return json_path, md_path, manifest_path


__all__ = ["build_replay_findings", "write_replay_report"]
