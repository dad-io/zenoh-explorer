"""B01 — one named baseline the later tasks can cite instead of the word "current".

Every task after this one has to say which Bear Hug it measured. Prose like "the current
checkout" is exactly how `a55f66b` got a date label mistaken for a snapshot identity, so the
baseline is a file: one commit, one observed test count, one roadmap census, one corpus digest.

The census is recomputed from `docs/ROADMAP.md` rather than transcribed, and a test compares the
stored census to a fresh one. A hand-copied census would go stale silently and then be quoted.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.paths import REPO_ROOT

#: The committed baseline note. Named for the task so a second baseline cannot overwrite it.
BASELINE_PATH = REPO_ROOT / "reports" / "baseline-b01.json"

#: The commit the delivered implementation patch was authored against.
PATCH_BASE_COMMIT = "3015a6058a0ca8c1428fe3cef9ba7fdaad5606ee"

ROADMAP_PATH = REPO_ROOT / "docs" / "ROADMAP.md"

#: A roadmap task row: `| 4.2 | ... | ... |`. Section headers and prose rows do not match.
_ROW = re.compile(r"^\|\s*(\d+\.\d+[a-z]?)\s*\|(.*)$")

#: The leading status marker of a row, if it has one. Only the FIRST bold marker counts: a row
#: reading `BUILT (specification only; runtime UNBUILT)` is a specification, and letting the
#: trailing UNBUILT win would file a spec as an implementation — the one claim Phase 9 must
#: not make.
_MARKER = re.compile(r"\*\*(BUILT|PARTIAL|UNBUILT|BLOCKED)\b")


def roadmap_census(text: str | None = None) -> dict[str, int]:
    """Count roadmap rows by their leading status marker.

    ``text`` lets a caller census a roadmap other than the working tree's — specifically the
    one at the commit a baseline note names. A frozen baseline whose census had to be
    regenerated every time the roadmap moved would not be frozen at all.

    `unmarked` is not a synonym for unbuilt. It means the row carries no claim either way. A
    census that folded unmarked rows into `built` or `unbuilt` would invent a status the
    roadmap never asserted.
    """
    counts = {
        "rows": 0,
        # `built_total` is every row with a leading BUILT marker. `built` is the subset that is
        # ACTUALLY an implementation. They were the same field until adversarial review pointed
        # out that quoting `built: 22` meant quoting 20 implementations plus 2 specifications —
        # exactly the claim the qualifier exists to prevent.
        "built_total": 0,
        "built": 0,
        "partial": 0,
        "explicitly_unbuilt": 0,
        # BLOCKED is kept apart from UNBUILT. "Nobody has built it" and "it cannot be built
        # until someone rules" are different states, and the second is the one this plan keeps
        # stopping at.
        "blocked": 0,
        "unmarked": 0,
        "built_with_unbuilt_qualifier": 0,
    }
    source = text if text is not None else ROADMAP_PATH.read_text(encoding="utf-8")
    for line in source.splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        counts["rows"] += 1
        body = match.group(2)
        marker = _MARKER.search(body)
        if marker is None:
            counts["unmarked"] += 1
            continue
        kind = marker.group(1)
        if kind == "BUILT":
            counts["built_total"] += 1
            # The qualifier lives between the marker and the em dash that ends the claim.
            head = body[marker.start() : marker.start() + 80]
            if "UNBUILT" in head[len("**BUILT") :]:
                counts["built_with_unbuilt_qualifier"] += 1
            else:
                counts["built"] += 1
        elif kind == "PARTIAL":
            counts["partial"] += 1
        elif kind == "BLOCKED":
            counts["blocked"] += 1
        else:
            counts["explicitly_unbuilt"] += 1
    return counts


def roadmap_census_at(commit: str) -> dict[str, int] | None:
    """The census of `docs/ROADMAP.md` as it stood at ``commit``, or None if unreadable."""
    text = _git("show", f"{commit}:docs/ROADMAP.md")
    return None if text is None else roadmap_census(text)


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _frozen_corpus() -> dict[str, Any]:
    """Report the frozen archive's presence honestly. Absent is `available: false`, never zero."""
    from bearhug.paths import frozen_corpus

    FROZEN_CORPUS_ZIP = frozen_corpus().path

    if not FROZEN_CORPUS_ZIP.is_file():
        return {"available": False, "reason": "frozen archive not present in this environment"}
    return {
        "available": True,
        "archive": str(FROZEN_CORPUS_ZIP),
        "bytes": FROZEN_CORPUS_ZIP.stat().st_size,
        # The digest is the corpus identity reported by `replay`, recorded here so a later run
        # that produces a different one is a detected change rather than a silent substitution.
        "digest": None,
    }


def build_baseline(
    *,
    tests_passed: int,
    tests_skipped: int,
    observed_with: str,
    corpus_digest: str | None = None,
    corpus_files: int | None = None,
    commit: str | None = None,
) -> dict[str, Any]:
    """Assemble the baseline record. Measured counts are passed in, never guessed here.

    ``commit`` pins the note to a specific commit rather than HEAD. Needed when the note has to be
    REGENERATED for a schema change — the census shape changed — without silently re-pointing a
    frozen baseline at whatever HEAD happens to be.
    """
    head = commit or _git("rev-parse", "HEAD") or "unknown"
    # Census the roadmap AT the commit this note names. Using the working tree would make the
    # note describe one commit's tests and another's roadmap, and every later roadmap edit
    # would falsify a note that had not changed.
    census = roadmap_census_at(head) if head != "unknown" else None
    census_source = "commit"
    if census is None:
        census, census_source = roadmap_census(), "working-tree"

    corpus = _frozen_corpus()
    if corpus["available"]:
        corpus["digest"] = corpus_digest
        corpus["files"] = corpus_files
    return {
        "schema_version": 1,
        "task": "B01",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "base_commit": head,
        "base_branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "patch_base_commit": PATCH_BASE_COMMIT,
        # `base_commit` is HEAD when the note was generated, i.e. B00's commit. B01's own commit
        # adds only this module, its test, and this file, so the measured tree is B01's tree.
        "describes": "the Bear Hug tree at task B01, whose parent commit is base_commit",
        "working_tree_clean": _git("status", "--porcelain") == "",
        "tests": {
            "passed": tests_passed,
            "skipped": tests_skipped,
            "observed_with": observed_with,
        },
        "roadmap_census": census,
        "roadmap_census_read_from": census_source,
        "corpus": {"frozen": corpus},
        "unimplemented_by_design": [
            "Stop coordinator and pure evaluators (Phase 9.4)",
            "vendored runtime package and source hashing (Phase 9.1)",
            "telemetry storage, rotation and reader (Phase 9.3)",
            "promotion patch set and ADR proposal (Phase 9.5, 6.2)",
            "paid evals; no Claude eval has been billed",
            "trimmed variant candidate (Phase 5.2)",
        ],
    }


def write_baseline(note: dict[str, Any], path: Path = BASELINE_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(note, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
