"""M13, the Bear Hug half — export windows, ship the extractor, ingest and join what comes back.

Bear Hug may not run the extractor over Barracuda (M11/M13). So it exports one file per corpus —
each transcript's manifest relpath and observed time range, nothing else — renders the extractor
as one standalone stdlib script, and writes the handoff. A Barracuda-owned session runs the script
and returns the M11 artifact. Ingest validates the artifact's shape and the extractor hash, then
joins it to the corpus manifest by relpath: a transcript the artifact does not cover, or an entry
the corpus does not hold, is reported, never merged around. Unknown stays unknown.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.paths import PATCHES_DIR, REPORTS_DIR, assert_writable
from bearhug.replay import harness_version as _module
from bearhug.replay.corpus import CorpusSelection
from bearhug.replay.harness_version import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    TranscriptWindow,
    harness_pathspecs,
)
from bearhug.replay.metrics import session_metrics_for

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TS = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$"
)
_TOP_KEYS = {"schema_version", "produced_at", "extractor", "repository", "entries"}
_EXTRACTOR_KEYS = {"name", "version", "sha256"}
_REPOSITORY_KEYS = {"head", "dirty_paths", "shallow", "harness_paths"}
_ENTRY_KEYS = {"transcript_id", "observed_start", "observed_end", "resolution"}
_RESOLUTION_KEYS = {"status", "harness_commit", "harness_commit_date", "candidates", "evidence"}
STATUSES = ("resolved", "unknown", "ambiguous")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def windows_for(corpus: CorpusSelection) -> list[TranscriptWindow]:
    """One window per transcript that has at least one valid record timestamp."""
    windows: list[TranscriptWindow] = []
    for path in corpus.paths:
        member = corpus.member_for(path)
        duration = session_metrics_for(path).duration
        if duration.first_timestamp is None or duration.last_timestamp is None:
            continue
        windows.append(
            TranscriptWindow(
                member.relpath, _iso(duration.first_timestamp), _iso(duration.last_timestamp)
            )
        )
    return windows


def render_standalone_extractor() -> str:
    """The extractor module as one stdlib script: the two Bear Hug imports become constants.

    A textual transform of the real module rather than a second implementation, so the promoted
    copy cannot drift from what the lab tested. Its own file hash becomes `extractor.sha256`.
    """
    source = Path(_module.__file__).read_text(encoding="utf-8")
    imports = (
        "from bearhug.paths import BARRACUDA_ROOT\n"
        "from bearhug.snapshot.spec import PROJECT_RULES\n"
    )
    if imports not in source:
        raise AssertionError(
            "harness_version.py no longer carries the two imports this transform expects"
        )
    embedded = (
        "# Standalone copy rendered by bear-hug (M13). The subject guard is off because this copy\n"
        "# runs INSIDE the subject by design; the harness pathspecs are embedded from\n"
        "# snapshot.spec.PROJECT_RULES at render time.\n"
        "BARRACUDA_ROOT = None\n"
        "PROJECT_RULES = ()\n"
    )
    source = source.replace(imports, embedded)
    source = source.replace(
        "_EMBEDDED_PATHSPECS: tuple[str, ...] | None = None\n",
        "_EMBEDDED_PATHSPECS: tuple[str, ...] | None = ("
        + ", ".join(json.dumps(spec) for spec in harness_pathspecs())
        + ",)\n",
    )
    if "bearhug." in source.replace(EXTRACTOR_NAME, ""):
        raise AssertionError("standalone extractor still references the bearhug package")
    return source


@dataclass(slots=True)
class HarnessAttribution:
    corpus_kind: str
    corpus_digest: str
    extractor_sha256: str
    by_transcript: dict[str, dict[str, Any]] = field(default_factory=dict)
    not_in_artifact: tuple[str, ...] = ()
    not_in_corpus: tuple[str, ...] = ()

    @property
    def summary(self) -> dict[str, int]:
        statuses = [entry["status"] for entry in self.by_transcript.values()]
        return {
            "transcripts": len(self.by_transcript) + len(self.not_in_artifact),
            "resolved": statuses.count("resolved"),
            "unknown": statuses.count("unknown"),
            "ambiguous": statuses.count("ambiguous"),
            "not_in_artifact": len(self.not_in_artifact),
            "not_in_corpus": len(self.not_in_corpus),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "corpus_kind": self.corpus_kind,
            "corpus_digest": self.corpus_digest,
            "extractor_sha256": self.extractor_sha256,
            "summary": self.summary,
            "by_transcript": self.by_transcript,
            "not_in_artifact": list(self.not_in_artifact),
            "not_in_corpus": list(self.not_in_corpus),
            "limit": (
                "A resolved commit is the harness state live at the session's first record; an "
                "unknown or ambiguous transcript is excluded from any pre/post claim, not folded "
                "into either side. Historical dirtiness of the working tree is unknowable."
            ),
        }


def _expect_keys(mapping: Any, keys: set[str], where: str) -> None:
    if not isinstance(mapping, dict):
        raise ValueError(f"{where} is not an object")
    extra = set(mapping) - keys
    missing = keys - set(mapping)
    if extra:
        raise ValueError(f"{where} carries unexpected key(s): {', '.join(sorted(extra))}")
    if missing:
        raise ValueError(f"{where} lacks required key(s): {', '.join(sorted(missing))}")


def validate_artifact(artifact: Any) -> None:
    """The M11 schema, enforced with the stdlib so the lab can refuse a bad artifact at ingest.
    The jsonschema copy in tests remains the authority; this mirrors it."""
    _expect_keys(artifact, _TOP_KEYS, "artifact")
    if artifact["schema_version"] != "1":
        raise ValueError(f"unsupported schema_version {artifact['schema_version']!r}")
    if not isinstance(artifact["produced_at"], str) or not _TS.match(artifact["produced_at"]):
        raise ValueError("produced_at is not an ISO-8601 timestamp with offset")
    _expect_keys(artifact["extractor"], _EXTRACTOR_KEYS, "extractor")
    if not _HEX64.match(str(artifact["extractor"]["sha256"])):
        raise ValueError("extractor sha256 is not 64 hex characters")
    _expect_keys(artifact["repository"], _REPOSITORY_KEYS, "repository")
    head = artifact["repository"]["head"]
    if head is not None and not _HEX40.match(str(head)):
        raise ValueError("repository head is not a full commit hash")
    if not isinstance(artifact["entries"], list):
        raise ValueError("entries is not a list")
    for index, entry in enumerate(artifact["entries"]):
        where = f"entries[{index}]"
        _expect_keys(entry, _ENTRY_KEYS, where)
        for key in ("observed_start", "observed_end"):
            if not isinstance(entry[key], str) or not _TS.match(entry[key]):
                raise ValueError(f"{where}.{key} is not an ISO-8601 timestamp with offset")
        resolution = entry["resolution"]
        _expect_keys(resolution, _RESOLUTION_KEYS, f"{where}.resolution")
        status = resolution["status"]
        if status not in STATUSES:
            raise ValueError(f"{where}.resolution.status {status!r} is not one of {STATUSES}")
        commit = resolution["harness_commit"]
        if commit is not None and not _HEX40.match(str(commit)):
            raise ValueError(f"{where}.resolution.harness_commit is not a full commit hash")
        if status == "resolved" and commit is None:
            raise ValueError(f"{where}.resolution is resolved without a harness_commit")
        if status != "resolved" and commit is not None:
            raise ValueError(f"{where}.resolution is {status} but carries a harness_commit")
        candidates = resolution["candidates"]
        if not isinstance(candidates, list) or any(not _HEX40.match(str(c)) for c in candidates):
            raise ValueError(f"{where}.resolution.candidates must be full commit hashes")
        if status == "ambiguous" and len(candidates) < 2:
            raise ValueError(f"{where}.resolution is ambiguous with fewer than two candidates")
        if status == "unknown" and candidates:
            raise ValueError(f"{where}.resolution is unknown but names candidates")
        evidence = resolution["evidence"]
        if not isinstance(evidence, list) or not evidence or not all(
            isinstance(e, str) and 0 < len(e) <= 512 for e in evidence
        ):
            raise ValueError(f"{where}.resolution.evidence must be 1..20 bounded strings")


def ingest_attribution(
    artifact: dict[str, Any], corpus: CorpusSelection, *, expected_extractor_sha256: str
) -> HarnessAttribution:
    validate_artifact(artifact)
    actual = artifact["extractor"]["sha256"]
    if actual != expected_extractor_sha256:
        raise ValueError(
            f"artifact was produced by extractor {actual[:12]}, not the shipped extractor "
            f"{expected_extractor_sha256[:12]}"
        )
    known = {member.relpath for member in corpus.members}
    attribution = HarnessAttribution(corpus.kind, corpus.digest, actual)
    covered: set[str] = set()
    ghosts: list[str] = []
    for entry in artifact["entries"]:
        transcript_id = entry["transcript_id"]
        if transcript_id not in known:
            ghosts.append(transcript_id)
            continue
        covered.add(transcript_id)
        attribution.by_transcript[transcript_id] = {
            "status": entry["resolution"]["status"],
            "harness_commit": entry["resolution"]["harness_commit"],
            "harness_commit_date": entry["resolution"]["harness_commit_date"],
            "candidates": list(entry["resolution"]["candidates"]),
            "observed_start": entry["observed_start"],
            "observed_end": entry["observed_end"],
        }
    attribution.not_in_artifact = tuple(sorted(known - covered))
    attribution.not_in_corpus = tuple(ghosts)
    return attribution


def render_attribution(attribution: HarnessAttribution) -> str:
    summary = attribution.summary
    lines = [
        f"# Harness versions — {attribution.corpus_kind}:{attribution.corpus_digest[:12]}",
        "",
        f"- extractor {attribution.extractor_sha256[:12]}",
        f"- transcripts {summary['transcripts']}: resolved {summary['resolved']}, unknown "
        f"{summary['unknown']}, ambiguous {summary['ambiguous']}, not in artifact "
        f"{summary['not_in_artifact']}; entries not in corpus {summary['not_in_corpus']}",
        "",
        "| transcript | status | harness commit | commit date |",
        "|---|---|---|---|",
    ]
    for transcript_id, entry in sorted(attribution.by_transcript.items()):
        lines.append(
            f"| `{transcript_id}` | {entry['status']} | {entry['harness_commit'] or '—'} | "
            f"{entry['harness_commit_date'] or '—'} |"
        )
    lines += ["", "## Limit", "", attribution.as_dict()["limit"]]
    return "\n".join(lines) + "\n"


def write_attribution_report(
    attribution: HarnessAttribution, *, reports_dir: Path | str | None = None
) -> tuple[Path, Path]:
    root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    stem = f"harness-versions-{attribution.corpus_kind}-{attribution.corpus_digest[:12]}"
    json_path = assert_writable(root / f"{stem}.json")
    md_path = assert_writable(root / f"{stem}.md")
    json_path.write_text(
        json.dumps(attribution.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_attribution(attribution), encoding="utf-8")
    return json_path, md_path


HANDOFF_NAME = "M13-HISTORICAL-ATTRIBUTION-HANDOFF.md"
SCRIPT_NAME = "harness_version_extractor.py"


def _handoff_text(
    *, extractor_sha256: str, windows_name: str, corpus: CorpusSelection, count: int
) -> str:
    artifact_name = f"artifact-{corpus.kind}-{corpus.digest[:12]}.json"
    lines = [
        "# M13 — historical harness attribution: a Barracuda-owned run",
        "",
        "Bear Hug may not read project-barracuda's git history (decision 0182; plan M11). This",
        "package lets a session running INSIDE Barracuda produce the one artifact Bear Hug needs,",
        "and nothing else.",
        "",
        "## What is in this directory",
        "",
        "| file | identity |",
        "|---|---|",
        f"| `{SCRIPT_NAME}` | sha256 `{extractor_sha256}` — extractor `{EXTRACTOR_NAME}` "
        f"{EXTRACTOR_VERSION}, stdlib only |",
        f"| `{windows_name}` | {count} transcript windows for corpus "
        f"`{corpus.kind}:{corpus.digest}` — relpath, first and last record timestamp; "
        "no content |",
        "| `manifest.json` | the two identities above, so the return can be checked against "
        "what was shipped |",
        "",
        "## Required Barracuda-owned procedure",
        "",
        f"1. Confirm `sha256sum {SCRIPT_NAME}` equals the value above. Stop on mismatch.",
        "2. From Barracuda's root, with the full (non-shallow) history checked out:",
        "",
        "   ```bash",
        f"   python3 <this-dir>/{SCRIPT_NAME} --repo . --windows <this-dir>/{windows_name} "
        f"--out <this-dir>/{artifact_name}",
        "   ```",
        "",
        "   The script reads `git log -- <harness pathspecs>` and `git status --porcelain`. It",
        "   writes identifiers, instants and hashes only. It does not modify the repository.",
        f"3. Return `{artifact_name}` to Bear Hug unchanged, with the Barracuda HEAD at run time",
        "   and whether the tree was dirty.",
        "",
        "## What Bear Hug does with the return",
        "",
        "```bash",
        f"uv run bearhug replay harness-versions --corpus {corpus.kind} "
        "--artifact <returned artifact>",
        "```",
        "",
        f"Ingest refuses an artifact whose extractor hash is not `{extractor_sha256[:12]}…` or",
        "whose shape departs from `docs/schemas/harness-version.schema.json`, joins entries to",
        "the corpus manifest by relpath, and reports resolved / unknown / ambiguous per",
        "transcript. A transcript the artifact does not cover, or an entry the corpus does not",
        "hold, is reported, never merged. Unknown rows are excluded from pre/post claims, not",
        "folded into either side.",
        "",
        "## What this does not prove",
        "",
        "The resolved commit is the harness state committed before the session's first record. An",
        "uncommitted edit live at the time is unknowable from history; the `dirty_paths` count is",
        "the tree at RUN time, not then.",
        "",
    ]
    return "\n".join(lines)


def write_handoff(corpus: CorpusSelection, *, out_dir: Path | str | None = None) -> list[Path]:
    """Write the standalone extractor, the corpus windows, the manifest and the handoff."""
    root = Path(out_dir) if out_dir is not None else PATCHES_DIR / "m13-historical-attribution"
    root.mkdir(parents=True, exist_ok=True)
    script_path = assert_writable(root / SCRIPT_NAME)
    script_path.write_text(render_standalone_extractor(), encoding="utf-8")
    extractor_sha256 = hashlib.sha256(script_path.read_bytes()).hexdigest()

    windows = windows_for(corpus)
    windows_name = f"windows-{corpus.kind}-{corpus.digest[:12]}.json"
    windows_path = assert_writable(root / windows_name)
    windows_path.write_text(
        json.dumps(
            {
                "corpus_kind": corpus.kind,
                "corpus_digest": corpus.digest,
                "windows": [
                    {
                        "transcript_id": w.transcript_id,
                        "observed_start": w.observed_start,
                        "observed_end": w.observed_end,
                    }
                    for w in windows
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = assert_writable(root / "manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "task": "M13",
                "extractor_name": EXTRACTOR_NAME,
                "extractor_version": EXTRACTOR_VERSION,
                "extractor_sha256": extractor_sha256,
                "script": SCRIPT_NAME,
                "windows": windows_name,
                "corpus_kind": corpus.kind,
                "corpus_digest": corpus.digest,
                "window_count": len(windows),
                "produced_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    handoff_path = assert_writable(root / HANDOFF_NAME)
    handoff_path.write_text(
        _handoff_text(
            extractor_sha256=extractor_sha256, windows_name=windows_name, corpus=corpus,
            count=len(windows),
        ),
        encoding="utf-8",
    )
    return [script_path, windows_path, manifest_path, handoff_path]


__all__ = [
    "HANDOFF_NAME",
    "SCRIPT_NAME",
    "STATUSES",
    "HarnessAttribution",
    "ingest_attribution",
    "render_attribution",
    "render_standalone_extractor",
    "validate_artifact",
    "windows_for",
    "write_attribution_report",
    "write_handoff",
]
