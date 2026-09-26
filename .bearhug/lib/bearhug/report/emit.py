"""Turn findings into the artifact a human reads, and the one 6.4 diffs.

Two rules the format enforces rather than requests. Every finding carries the limit of what it
proves, because a ranked list reads as authority and this one is a set of measurements with
known blind spots. And an empty result says so explicitly: six detectors returned a confident
uniform zero while building this repo, and each looked exactly like good news.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path
from typing import Any

from bearhug.model import Finding, Severity, dumps
from bearhug.paths import FINDINGS_DIR, assert_writable

EMPTY_NOTE = (
    "No findings. That is not the same as no defects: a check that returns nothing has not "
    "shown the behaviour is absent, only that this check did not see it. Every detector here "
    "must be shown to fire on a constructed positive before its zero means anything "
    "(docs/METHOD.md)."
)


def write_findings(
    findings: list[Finding], *, out_dir: Path | str | None = None,
    snapshot_id: str, prefix: str = "lint", extra: dict[str, Any] | None = None,
) -> Path:
    """Write `findings/<prefix>-<snapshot_id>.json`. Stable ids, sorted keys, diffable.

    `extra` adds top-level keys (S05: `drift` and `stale`); it may not shadow the schema's own."""
    directory = Path(out_dir) if out_dir is not None else FINDINGS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    safe = snapshot_id.replace("@", "-at-").replace("/", "-")
    path = assert_writable(directory / f"{prefix}-{safe}.json")
    payload = {
        # T03: the Go read model accepts exactly one findings-file version and refuses the rest.
        "schema_version": "1",
        "snapshot": snapshot_id,
        "generated_at": dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(findings),
        "by_severity": {k.value: v for k, v in _severity_counts(findings).items()},
        "findings": [f.to_dict() for f in findings],
    }
    for key, value in (extra or {}).items():
        if key in payload:
            raise ValueError(f"extra key {key!r} would shadow the findings schema")
        payload[key] = value
    path.write_bytes(dumps(payload).encode("utf-8"))
    return path


def _severity_counts(findings: list[Finding]) -> dict[Severity, int]:
    counts = Counter(f.severity for f in findings)
    return {sev: counts.get(sev, 0) for sev in Severity if counts.get(sev)}


def render_markdown(
    findings: list[Finding], *, snapshot_id: str, include_info: bool = False,
    title: str = "bear-hug lint", intro: str | None = None,
) -> str:
    """The artifact you actually read.

    INFO findings are summarised rather than listed by default. They are real output — an
    unresolved countable claim, a gate's traffic share, a correctly-cited superseded decision —
    but there are 75 of them against 18 actionable ones, and a list where 80% is context is a
    list nobody finishes. They stay in full in the JSON, which is what 6.4 diffs.
    """
    lines = [f"# {title} — {snapshot_id}", ""]
    if intro:
        lines += [intro, ""]
    if not findings:
        lines += [EMPTY_NOTE, ""]
        return "\n".join(lines)

    counts = _severity_counts(findings)
    lines.append("  ".join(f"**{sev.value}** {count}" for sev, count in counts.items()))
    lines.append("")

    shown = findings if include_info else [f for f in findings if f.severity is not Severity.INFO]
    hidden = len(findings) - len(shown)
    if hidden:
        by_check = Counter(f.check for f in findings if f.severity is Severity.INFO)
        detail = ", ".join(f"{n} {check}" for check, n in sorted(by_check.items()))
        lines += [
            f"_{hidden} INFO findings not listed ({detail}). They are in the JSON; "
            "re-run with `--all` to read them here._",
            "",
        ]

    current: Severity | None = None
    for finding in shown:
        if finding.severity is not current:
            current = finding.severity
            lines += [f"## {current.value}", ""]
        where = ", ".join(e.render() for e in finding.evidence) or "—"
        lines += [
            f"### `{finding.id}`",
            "",
            finding.summary,
            "",
            f"- **check** `{finding.check}` · **where** {where}",
            f"- **snapshot** `{finding.snapshot}`",
        ]
        if finding.detail:
            lines.append(f"- **detail** {finding.detail}")
        lines += [f"- **what this does not prove** {finding.limit}", ""]
    return "\n".join(lines)
