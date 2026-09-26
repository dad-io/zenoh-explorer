"""Summarise persisted eval results without re-running paid work."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from bearhug.evals.adjudicate import final_passed
from bearhug.model import SEVERITY_ORDER, Evidence, Finding, Severity
from bearhug.paths import REPORTS_DIR, RUNS_DIR, assert_writable
from bearhug.report.emit import render_markdown, write_findings

EXTERNAL_VALIDITY_LIMIT = (
    "This is a headless `claude -p` run: no user was present mid-turn to interrupt, queue a "
    "follow-up, or correct the model. It is evidence about scripted headless behavior, not a "
    "direct estimate of interactive-session behavior. A passing rubric also proves only the "
    "named observable, not general task quality."
)


def load_results(runs_dir: Path = RUNS_DIR, *, snapshot_id: str | None = None) -> list[dict]:
    results = []
    for path in sorted(runs_dir.glob("*/result.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(result, dict):
            continue
        if snapshot_id is not None and result.get("snapshot_id") != snapshot_id:
            continue
        result["_result_path"] = str(path)
        results.append(result)
    return results


def _runtime_of(result: dict) -> str:
    return str(result.get("runtime") or "snapshot")


def build_eval_findings(
    results: list[dict], *, snapshot_id: str, runtime: str | None = None
) -> list[Finding]:
    """`runtime` restricts the rows to results that ran that runtime ('snapshot' — the snapshot's
    own — or 'sealed'); a result without the field predates the overlay. None pools every run."""
    findings = []
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for result in results:
        if runtime is not None and _runtime_of(result) != runtime:
            continue
        variant = str(result.get("variant", "unknown"))
        scenario = str(result.get("scenario", "unknown"))
        grouped[(variant, scenario)].append(result)

    for (variant, scenario), runs in sorted(grouped.items()):
        passed_count = sum(final_passed(result) for result in runs)
        passed = passed_count == len(runs)
        durations = [
            float(result["duration_ms"])
            for result in runs
            if isinstance(result.get("duration_ms"), int | float)
        ]
        costs = [
            float(result["cost_usd"])
            for result in runs
            if isinstance(result.get("cost_usd"), int | float)
        ]
        mean_ms = statistics.fmean(durations) if durations else 0.0
        variance_ms = statistics.pvariance(durations) if len(durations) > 1 else 0.0
        reasons = sorted({str(result.get("reason", "no reason recorded")) for result in runs})
        findings.append(
            Finding(
                id=f"eval-{variant}-{scenario}",
                check="EVAL-RUBRIC",
                severity=Severity.INFO if passed else Severity.BROKEN,
                summary=f"{variant} × {scenario}: {passed_count}/{len(runs)} rubric runs passed.",
                snapshot=snapshot_id,
                evidence=tuple(
                    Evidence(
                        file=result.get("_result_path"),
                        run_id=str(result.get("run_id", "unknown")),
                    )
                    for result in runs
                ),
                detail=(
                    f"runtime={runtime or 'pooled'}; runs={len(runs)} "
                    f"mean_duration_ms={mean_ms:.1f} duration_variance_ms2={variance_ms:.1f} "
                    f"cost_usd={'unreported' if not costs else f'{sum(costs):.6f}'} "
                    f"reasons={' | '.join(reasons)}"
                ),
                limit=EXTERNAL_VALIDITY_LIMIT,
            )
        )
    findings.sort(key=lambda finding: (SEVERITY_ORDER[finding.severity], finding.id))
    return findings


def write_eval_report(
    *,
    snapshot_id: str,
    runs_dir: Path = RUNS_DIR,
    findings_dir: Path | None = None,
    reports_dir: Path | None = None,
    runtime: str | None = None,
) -> tuple[Path, Path, list[Finding]]:
    results = load_results(runs_dir, snapshot_id=snapshot_id)
    if not results:
        raise ValueError(f"no eval results for snapshot {snapshot_id} under {runs_dir}")
    findings = build_eval_findings(results, snapshot_id=snapshot_id, runtime=runtime)
    tag = "" if runtime == "snapshot" else f"{runtime or 'pooled'}-"
    json_path = write_findings(
        findings, out_dir=findings_dir, snapshot_id=snapshot_id, prefix=f"eval-{tag}".rstrip("-")
    )
    report_root = reports_dir or REPORTS_DIR
    report_root.mkdir(parents=True, exist_ok=True)
    safe = snapshot_id.replace("@", "-at-").replace("/", "-")
    md_path = assert_writable(report_root / f"eval-{tag}{safe}.md")
    md_path.write_text(
        render_markdown(
            findings,
            snapshot_id=snapshot_id,
            include_info=True,
            title="bear-hug headless eval",
        ),
        encoding="utf-8",
    )
    return json_path, md_path, findings


__all__ = [
    "EXTERNAL_VALIDITY_LIMIT",
    "build_eval_findings",
    "load_results",
    "write_eval_report",
]
