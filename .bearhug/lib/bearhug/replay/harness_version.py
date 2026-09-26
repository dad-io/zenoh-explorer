"""M12 — resolve which harness commit was live for a transcript's observed time range.

Every replay rate in Phase 4 is otherwise attributed to a CLAUDE.md most of those sessions never
ran under (roadmap 4.10). This module answers, per transcript, from a git history: the last
harness-touching commit at or before the session started, unless the harness changed inside the
session (`ambiguous`, every candidate named) or no such commit exists in the available history
(`unknown`, with the reason). It emits only the canonical M11 artifact — identifiers, instants,
hashes, harness pathspecs — never a diff or a file body.

It is proven entirely against synthetic repositories. Running it over Barracuda is a
Barracuda-owned action (M13): the lab refuses the subject's own path.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.paths import BARRACUDA_ROOT
from bearhug.snapshot.spec import PROJECT_RULES

EXTRACTOR_NAME = "bearhug-harness-version-extractor"
EXTRACTOR_VERSION = "1.0.0"

#: Set only in the standalone copy M13 ships (see harness_attribution.render_standalone_extractor),
#: where the one list in snapshot.spec is not importable. None here means: read the list.
_EMBEDDED_PATHSPECS: tuple[str, ...] | None = None

#: Capture kinds that name a path or pattern git can take as a pathspec. A census rule counts a
#: directory whose emptiness is the observation; its history is still the directory's history.
_PATHSPEC_KINDS = frozenset({"file", "glob", "tree", "census"})


def extractor_sha256() -> str:
    """The hash of THIS file's bytes, so an artifact names the exact extractor that made it."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def harness_pathspecs() -> tuple[str, ...]:
    """The project-layer harness surface, as git pathspecs, from the one list in snapshot.spec."""
    if _EMBEDDED_PATHSPECS is not None:
        return tuple(_EMBEDDED_PATHSPECS)
    specs: list[str] = []
    for rule in PROJECT_RULES:
        if rule.kind in _PATHSPEC_KINDS and rule.pattern not in specs:
            specs.append(rule.pattern)
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class TranscriptWindow:
    """One transcript's identity and observed time range (ISO-8601 with offset or Z)."""

    transcript_id: str
    observed_start: str
    observed_end: str


@dataclass(frozen=True, slots=True)
class HarnessCommit:
    commit: str
    date: str  # committer date, ISO-8601 strict, as git printed it

    @property
    def instant(self) -> datetime:
        return _instant(self.date)


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without an offset cannot be compared: {value!r}")
    return parsed.astimezone(UTC)


def _git(repo: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def has_history(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--is-inside-work-tree") is not None and (
        _git(repo, "rev-parse", "--verify", "HEAD") is not None
    )


def repository_state(repo: Path) -> dict[str, Any]:
    """Run-time facts about the repository. Historical dirtiness is unknowable and not claimed."""
    head = (_git(repo, "rev-parse", "HEAD") or "").strip() or None
    status = _git(repo, "status", "--porcelain")
    dirty = len([line for line in status.splitlines() if line.strip()]) if status else 0
    shallow_text = (_git(repo, "rev-parse", "--is-shallow-repository") or "").strip()
    return {
        "head": head,
        "dirty_paths": dirty,
        "shallow": shallow_text == "true",
        "harness_paths": list(harness_pathspecs()),
    }


def harness_commits(repo: Path) -> list[HarnessCommit]:
    """Every commit touching a harness pathspec, oldest first, with its committer instant."""
    output = _git(
        repo, "log", "--format=%H%x1f%cI", "--date-order", "--", *harness_pathspecs()
    )
    if not output:
        return []
    commits = []
    for line in output.splitlines():
        if "\x1f" not in line:
            continue
        commit, date = line.split("\x1f", 1)
        commits.append(HarnessCommit(commit.strip(), date.strip()))
    return sorted(commits, key=lambda item: item.instant)


def resolve(
    commits: list[HarnessCommit], window: TranscriptWindow, *, shallow: bool, history: bool
) -> dict[str, Any]:
    if not history:
        return _unknown(["no git history at the repository path"])
    start = _instant(window.observed_start)
    end = _instant(window.observed_end)
    before = [item for item in commits if item.instant <= start]
    inside = [item for item in commits if start < item.instant <= end]
    if inside:
        candidates = ([before[-1]] if before else []) + inside
        if len(candidates) >= 2:
            return {
                "status": "ambiguous",
                "harness_commit": None,
                "harness_commit_date": None,
                "candidates": [item.commit for item in candidates],
                "evidence": [
                    f"harness changed inside the observed range: {len(inside)} commit(s) between "
                    "observed_start and observed_end"
                    + (
                        "; the commit live at observed_start is the first candidate"
                        if before
                        else ""
                    ),
                ],
            }
        return _unknown(
            [
                "no harness-touching commit at or before observed_start in the available history",
                "one harness-touching commit lands inside the observed range, so the session "
                "started under a state this history does not contain",
            ]
            + (["history is shallow; earlier commits may exist upstream"] if shallow else [])
        )
    if before:
        live = before[-1]
        return {
            "status": "resolved",
            "harness_commit": live.commit,
            "harness_commit_date": live.date,
            "candidates": [],
            "evidence": [
                "last harness-touching commit at or before observed_start",
                "no harness-touching commit between observed_start and observed_end",
            ],
        }
    return _unknown(
        ["no harness-touching commit at or before observed_start in the available history"]
        + (["history is shallow; earlier commits may exist upstream"] if shallow else [])
    )


def _unknown(evidence: list[str]) -> dict[str, Any]:
    return {
        "status": "unknown",
        "harness_commit": None,
        "harness_commit_date": None,
        "candidates": [],
        "evidence": evidence,
    }


def extract(
    repo: Path | str, windows: list[TranscriptWindow], *, allow_subject: bool = False
) -> dict[str, Any]:
    """The canonical M11 artifact for ``windows`` against the history at ``repo``.

    ``allow_subject`` exists for the promoted copy running INSIDE Barracuda. From the lab it stays
    False: reading the subject's history from here is exactly what M13 hands off.
    """
    repo = Path(repo)
    if (
        not allow_subject
        and BARRACUDA_ROOT is not None
        and repo.resolve() == Path(BARRACUDA_ROOT).resolve()
    ):
        raise PermissionError(
            "running the extractor over project-barracuda is a Barracuda-owned action (M13); "
            "the lab reads only constructed histories"
        )
    history = has_history(repo)
    state = repository_state(repo) if history else {
        "head": None, "dirty_paths": 0, "shallow": False, "harness_paths": list(harness_pathspecs())
    }
    commits = harness_commits(repo) if history else []
    entries = [
        {
            "transcript_id": window.transcript_id,
            "observed_start": window.observed_start,
            "observed_end": window.observed_end,
            "resolution": resolve(commits, window, shallow=state["shallow"], history=history),
        }
        for window in windows
    ]
    return {
        "schema_version": "1",
        "produced_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "extractor": {
            "name": EXTRACTOR_NAME,
            "version": EXTRACTOR_VERSION,
            "sha256": extractor_sha256(),
        },
        "repository": state,
        "entries": entries,
    }


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point: ``--repo`` ``--windows`` ``--out``. Used by the promoted copy a
    Barracuda-owned session runs; the windows file is the one Bear Hug exported for its corpus."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="resolve harness commits per transcript window")
    parser.add_argument("--repo", required=True, help="repository whose history is read")
    parser.add_argument("--windows", required=True, help="windows-<corpus>-<digest>.json")
    parser.add_argument("--out", required=True, help="where to write the artifact")
    args = parser.parse_args(argv)
    payload = json.loads(Path(args.windows).read_text(encoding="utf-8"))
    windows = [
        TranscriptWindow(w["transcript_id"], w["observed_start"], w["observed_end"])
        for w in payload["windows"]
    ]
    artifact = extract(args.repo, windows, allow_subject=True)
    Path(args.out).write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {args.out}: {len(artifact['entries'])} entries, extractor "
        f"{artifact['extractor']['sha256'][:12]}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the standalone copy
    raise SystemExit(main())


__all__ = [
    "EXTRACTOR_NAME",
    "EXTRACTOR_VERSION",
    "HarnessCommit",
    "TranscriptWindow",
    "extract",
    "extractor_sha256",
    "harness_commits",
    "harness_pathspecs",
    "has_history",
    "main",
    "repository_state",
    "resolve",
]
