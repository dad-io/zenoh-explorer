"""M16 — Claude Code job timelines as a Phase-4 timing source.

D07, ruled by Sam on 2026-09-01: in scope under decision 0182, READ-ONLY, for timing. Two boundaries
are built into the reader rather than left to discipline:

- **Privacy.** A record's `text` and `detail` are prose from a session. The reader keeps their
  LENGTHS and the record's `at`/`state`/key set; the content never leaves the file, so nothing
  downstream (a census, a finding, a cockpit pane) can quote it.
- **Self-exclusion.** A job whose directory name prefixes one of bear-hug's OWN session ids is
  excluded, so the lab never measures itself.

Format, from the constructed reader's contract: one JSON object per line with `at` (ISO-8601
instant), `state`, `text`, `detail`; other keys are recorded by name and type only. Malformed
lines are counted, never raised over.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bearhug.paths import REPORTS_DIR, assert_writable

TIMELINE = "timeline.jsonl"


@dataclass(frozen=True, slots=True)
class TimelineRecord:
    at: str
    state: str
    text_chars: int
    detail_chars: int
    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TimelineRead:
    records: list[TimelineRecord]
    malformed: int


def read_timeline(path: Path | str) -> TimelineRead:
    """Stream one timeline; retain timings, states and sizes only."""
    records: list[TimelineRecord] = []
    malformed = 0
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(value, dict):
                malformed += 1
                continue
            records.append(
                TimelineRecord(
                    at=str(value.get("at", "")),
                    state=str(value.get("state", "")),
                    text_chars=len(value["text"]) if isinstance(value.get("text"), str) else 0,
                    detail_chars=(
                        len(value["detail"]) if isinstance(value.get("detail"), str) else 0
                    ),
                    keys=tuple(sorted(str(k) for k in value)),
                )
            )
    return TimelineRead(records=records, malformed=malformed)


def excluded_jobs(jobs_root: Path | str, own_transcripts_dir: Path | str) -> set[str]:
    """Job directories that name one of bear-hug's own sessions (by transcript-id prefix)."""
    own = Path(own_transcripts_dir)
    session_ids = [p.stem for p in own.glob("*.jsonl")] if own.is_dir() else []
    excluded: set[str] = set()
    for job in Path(jobs_root).iterdir():
        if job.is_dir() and any(sid.startswith(job.name) for sid in session_ids):
            excluded.add(job.name)
    return excluded


@dataclass(frozen=True, slots=True)
class JobSummary:
    job: str
    records: int
    malformed: int
    bytes: int
    first_at: str
    last_at: str
    states: dict[str, int]
    key_types: dict[str, tuple[str, ...]]
    text_chars: int
    detail_chars: int


@dataclass(slots=True)
class JobsCensus:
    jobs_root: str
    jobs: list[JobSummary] = field(default_factory=list)
    excluded: tuple[str, ...] = ()
    skipped_without_timeline: tuple[str, ...] = ()

    @property
    def total_records(self) -> int:
        return sum(j.records for j in self.jobs)

    @property
    def date_range(self) -> tuple[str, str]:
        firsts = [j.first_at for j in self.jobs if j.first_at]
        lasts = [j.last_at for j in self.jobs if j.last_at]
        return (min(firsts) if firsts else "", max(lasts) if lasts else "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "ruling": "D07 — in scope, read-only, Phase-4 timing source (Sam, 2026-09-01)",
            "privacy_boundary": "text and detail are counted, never retained",
            "jobs_root": self.jobs_root,
            "jobs": [
                {
                    **{k: v for k, v in asdict(j).items() if k != "key_types"},
                    "key_types": {k: list(v) for k, v in j.key_types.items()},
                }
                for j in self.jobs
            ],
            "excluded": list(self.excluded),
            "skipped_without_timeline": list(self.skipped_without_timeline),
            "total_records": self.total_records,
            "date_range": list(self.date_range),
        }


def _type_name(value: Any) -> str:
    return type(value).__name__


def job_census(jobs_root: Path | str, *, exclude: set[str]) -> JobsCensus:
    """Per job: record and malformed counts, first/last instant, state histogram, key/type census,
    prose sizes. The key/type census re-reads the file for types the reader does not retain."""
    root = Path(jobs_root)
    census = JobsCensus(jobs_root=str(root))
    skipped: list[str] = []
    for job in sorted(p for p in root.iterdir() if p.is_dir()):
        if job.name in exclude:
            continue
        timeline = job / TIMELINE
        if not timeline.is_file():
            skipped.append(job.name)
            continue
        read = read_timeline(timeline)
        key_types: dict[str, set[str]] = {}
        with timeline.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    for key, item in value.items():
                        key_types.setdefault(str(key), set()).add(_type_name(item))
        ats = [r.at for r in read.records if r.at]
        census.jobs.append(
            JobSummary(
                job=job.name,
                records=len(read.records),
                malformed=read.malformed,
                bytes=timeline.stat().st_size,
                first_at=min(ats) if ats else "",
                last_at=max(ats) if ats else "",
                states=dict(Counter(r.state for r in read.records)),
                key_types={k: tuple(sorted(v)) for k, v in sorted(key_types.items())},
                text_chars=sum(r.text_chars for r in read.records),
                detail_chars=sum(r.detail_chars for r in read.records),
            )
        )
    census.excluded = tuple(sorted(exclude & {p.name for p in root.iterdir() if p.is_dir()}))
    census.skipped_without_timeline = tuple(skipped)
    return census


def render_jobs_census(census: JobsCensus) -> str:
    lines = [
        "# Claude Code job timelines — census (M16)",
        "",
        f"- root: `{census.jobs_root}`; ruling: D07, in scope, read-only (Sam, 2026-09-01)",
        "- privacy boundary: `text` and `detail` are COUNTED, never retained — no prose from a "
        "session appears in this report or in any artifact built from the reader",
        f"- jobs read: {len(census.jobs)}; excluded as bear-hug's own: "
        f"{', '.join(census.excluded) or 'none'}; without a timeline: "
        f"{', '.join(census.skipped_without_timeline) or 'none'}",
        f"- records: {census.total_records}; span: {census.date_range[0]} → {census.date_range[1]}",
        "",
        "| job | records | malformed | bytes | first | last | states | text chars | detail chars |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for j in census.jobs:
        states = ", ".join(f"{k}={v}" for k, v in sorted(j.states.items()))
        lines.append(
            f"| `{j.job}` | {j.records} | {j.malformed} | {j.bytes} | {j.first_at} | {j.last_at} | "
            f"{states} | {j.text_chars} | {j.detail_chars} |"
        )
    keys: dict[str, set[str]] = {}
    for j in census.jobs:
        for k, types in j.key_types.items():
            keys.setdefault(k, set()).update(types)
    lines += ["", "## Keys and types across every job", ""]
    lines += [f"- `{k}`: {', '.join(sorted(v))}" for k, v in sorted(keys.items())]
    lines += [
        "",
        "## Limit",
        "",
        "A timeline is Claude Code's record of a background job, not of a session's turns; joining "
        "its instants to transcript turns is the timing use D07 permits and this census does not "
        "yet perform. Counts are of records present today — the directory is pruned like the "
        "transcripts, so every figure is a floor.",
        "",
    ]
    return "\n".join(lines)


def write_jobs_census(
    census: JobsCensus, reports_dir: Path | str = REPORTS_DIR
) -> tuple[Path, Path]:
    reports = Path(reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    json_path = assert_writable(reports / "jobs-census.json")
    md_path = assert_writable(reports / "jobs-census.md")
    json_path.write_text(
        json.dumps(census.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_jobs_census(census), encoding="utf-8")
    return json_path, md_path


__all__ = [
    "JobSummary", "JobsCensus", "TimelineRead", "TimelineRecord", "excluded_jobs", "job_census",
    "read_timeline", "render_jobs_census", "write_jobs_census",
]
