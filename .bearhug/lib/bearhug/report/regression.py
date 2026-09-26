"""S03 — the regression-run manifest: "rerun everything" names the exact evidence set.

Roadmap 6.4 promised a battery that reruns everything after patches land and asserts resolved
findings are gone and no new ones appeared. "Everything" was never named. This manifest names it:
two snapshot identities, the runtime hash installed at each, the corpus the replay read, the eval
runs with their candidate hashes, every classifier version, the report generator's version, and one
entry per persisted phase artifact on each side. S04 compares what this names and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug import __version__

MANIFEST_SCHEMA_VERSION = "1"
PHASES = ("lint", "hooks", "replay", "eval")

_SNAPSHOT_ID = re.compile(r"^\d{4}-\d{2}-\d{2}[a-z]?@[0-9a-f]{8}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def build_regression_manifest(
    *,
    old_snapshot_id: str,
    new_snapshot_id: str,
    old_runtime_sha256: str | None,
    new_runtime_sha256: str | None,
    corpus: dict[str, Any],
    eval_runs: list[dict[str, Any]],
    classifier_versions: dict[str, str],
    phases: list[dict[str, Any]],
    promotion_workflows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "produced_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "report_generator_version": __version__,
        "old_snapshot_id": old_snapshot_id,
        "new_snapshot_id": new_snapshot_id,
        "old_runtime_sha256": old_runtime_sha256,
        "new_runtime_sha256": new_runtime_sha256,
        "corpus": dict(corpus),
        "eval_runs": [dict(run) for run in eval_runs],
        "classifier_versions": dict(sorted(classifier_versions.items())),
        "phases": [dict(phase) for phase in phases],
    }
    if promotion_workflows is not None:
        manifest["promotion_workflows"] = [dict(workflow) for workflow in promotion_workflows]
    return manifest


def validate_regression_manifest(
    manifest: dict[str, Any], *, allow_coverage_gaps: bool = False
) -> list[str]:
    """The cross-field checks the schema cannot express. Empty means comparable.

    `allow_coverage_gaps` sets aside ONLY the missing-phase problems — the comparison then reports
    those phases as coverage gaps rather than refusing the whole manifest. A wrong identity, a
    reused corpus label, a duplicate id, or a phase from a third snapshot is refused either way.
    """
    problems: list[str] = []
    old_id, new_id = manifest.get("old_snapshot_id", ""), manifest.get("new_snapshot_id", "")
    for label, value in (("old_snapshot_id", old_id), ("new_snapshot_id", new_id)):
        if not _SNAPSHOT_ID.match(str(value)):
            problems.append(f"{label} {value!r} is a label without its identity (want label@hash)")
    if old_id == new_id:
        problems.append("old and new snapshot ids are the same; there is nothing to compare")

    corpus = manifest.get("corpus") or {}
    corpus_digest = corpus.get("digest")
    if not _SHA256.match(str(corpus_digest or "")):
        problems.append("corpus digest is not a sha256")

    present: dict[tuple[str, str], int] = Counter()
    for index, phase in enumerate(manifest.get("phases") or []):
        kind, snapshot = phase.get("kind"), phase.get("snapshot_id")
        where = f"phases[{index}] ({kind} @ {snapshot})"
        if snapshot not in (old_id, new_id):
            problems.append(
                f"{where}: artifact is from snapshot {snapshot}, which is neither the old nor "
                "the new side of this comparison"
            )
        present[(kind, snapshot)] += 1
        ids = phase.get("finding_ids") or []
        duplicates = sorted(i for i, n in Counter(ids).items() if n > 1)
        if duplicates:
            problems.append(f"{where}: duplicate finding ids {duplicates}")
        if kind == "replay":
            digest = phase.get("corpus_digest")
            if digest != corpus_digest:
                problems.append(
                    f"{where}: replay read corpus digest {str(digest)[:12]} but the manifest's "
                    f"corpus is {str(corpus_digest)[:12]} — a {corpus.get('kind')} label reused "
                    "with a different digest cannot be compared"
                )
    for kind in PHASES:
        for snapshot in (old_id, new_id):
            if present[(kind, snapshot)] == 0:
                if allow_coverage_gaps:
                    continue
                problems.append(
                    f"phase {kind} is missing for snapshot {snapshot}: a coverage gap, not a "
                    "comparable pair"
                )
            elif present[(kind, snapshot)] > 1:
                problems.append(
                    f"phase {kind} appears {present[(kind, snapshot)]} times for {snapshot}"
                )
    if "promotion_workflows" in manifest:
        from bearhug.report.promotion_regression import validate_promotion_workflows

        problems.extend(validate_promotion_workflows(manifest["promotion_workflows"]))
    return problems


# --- assembling a manifest from the findings actually on disk --------------------------------

_PHASE_PREFIXES = {"lint": "lint-", "hooks": "hooks-", "replay": "replay-", "eval": "eval-"}


def _safe(snapshot_id: str) -> str:
    return snapshot_id.replace("@", "-at-").replace("/", "-")


def manifest_from_findings(
    *,
    old_snapshot_id: str,
    new_snapshot_id: str,
    findings_dir: Path | str,
    corpus: dict[str, Any],
    old_runtime_sha256: str | None,
    new_runtime_sha256: str | None,
    classifier_versions: dict[str, str],
    eval_runs: list[dict[str, Any]] | None = None,
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    """Name every persisted phase artifact for the two snapshots. Nothing is rebuilt or invented:
    a phase with no file on a side is simply absent, and validation calls that a coverage gap."""
    root = Path(findings_dir)
    base = Path(repo_root) if repo_root is not None else root.parent
    phases: list[dict[str, Any]] = []
    for kind, prefix in _PHASE_PREFIXES.items():
        for snapshot_id in (old_snapshot_id, new_snapshot_id):
            exact = root / f"{prefix}{_safe(snapshot_id)}.json"
            # the unsuffixed artifact is the snapshot's own harness; a runtime-tagged variant
            # (eval-sealed-…, eval-pooled-…) or a corpus-tagged replay is only considered when
            # no exact file exists
            matches = (
                [exact] if exact.is_file()
                else sorted(root.glob(f"{prefix}*{_safe(snapshot_id)}.json"))
            )
            if kind == "replay" and corpus.get("digest"):
                digest12 = str(corpus["digest"])[:12]
                matches = [m for m in matches if f"-{corpus.get('kind')}-{digest12}-" in m.name]
            if not matches:
                continue
            path = matches[-1]
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ids = [str(f.get("id")) for f in data.get("findings", []) if isinstance(f, dict)]
            entry: dict[str, Any] = {
                "kind": kind,
                "snapshot_id": snapshot_id,
                "findings_path": path.relative_to(base).as_posix(),
                "finding_ids": ids,
            }
            if kind == "replay":
                entry["corpus_digest"] = corpus["digest"]
            phases.append(entry)
    return build_regression_manifest(
        old_snapshot_id=old_snapshot_id,
        new_snapshot_id=new_snapshot_id,
        old_runtime_sha256=old_runtime_sha256,
        new_runtime_sha256=new_runtime_sha256,
        corpus=corpus,
        eval_runs=eval_runs or [],
        classifier_versions=classifier_versions,
        phases=phases,
    )


def runtime_tree_sha256(root: Path | str) -> str | None:
    """`bearhug-runtime-sha256/1` over an installed `_bearhug` tree, as the runtime hashes itself:
    sorted posix relative paths, each contributing `path || 0x00 || ascii length || 0x00 || bytes`;
    `__pycache__`, `*.pyc` and `.DS_Store` excluded. None when the tree is absent — a snapshot that
    predates promotion had no runtime, and None is the honest value, never a made-up hash."""
    root = Path(root)
    if not root.is_dir():
        return None
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and path.name != ".DS_Store"
    )
    digest = hashlib.sha256()
    for path in files:
        relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        blob = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(len(blob)).encode("ascii"))
        digest.update(b"\x00")
        digest.update(blob)
    return digest.hexdigest()


def _installed_runtime(root: Path, snapshot_id: str) -> str | None:
    label = snapshot_id.split("@", 1)[0]
    return runtime_tree_sha256(
        root / "snapshots" / label / "project" / "scripts" / "hooks" / "_bearhug"
    )


def write_regression_manifest(
    old_snapshot_id: str,
    new_snapshot_id: str,
    *,
    corpus_kind: str = "frozen",
    since: str | None = None,
    repo_root: Path | str | None = None,
) -> Path:
    """Write `reports/regression-<old>-<new>.json` from what the repository holds: the persisted
    phase findings, the corpus manifest the new snapshot's replay pinned, the runtime tree each
    snapshot captured (None where none was installed), and the classifier versions in code."""
    from bearhug.paths import REPO_ROOT
    from bearhug.replay.groundtruth import CLASSIFIERS

    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    pinned = sorted(
        (root / "corpus").glob(f"manifest-{corpus_kind}-*-{_safe(new_snapshot_id)}.json")
    )
    if not pinned:
        raise FileNotFoundError(
            f"no {corpus_kind} corpus manifest for snapshot {new_snapshot_id} under "
            f"{root / 'corpus'}; run the replay for that snapshot first"
        )
    digest = json.loads(pinned[-1].read_text(encoding="utf-8"))["sha256"]
    manifest = manifest_from_findings(
        old_snapshot_id=old_snapshot_id,
        new_snapshot_id=new_snapshot_id,
        findings_dir=root / "findings",
        corpus={"kind": corpus_kind, "digest": digest, "since": since},
        old_runtime_sha256=_installed_runtime(root, old_snapshot_id),
        new_runtime_sha256=_installed_runtime(root, new_snapshot_id),
        classifier_versions={name: spec.version for name, spec in CLASSIFIERS.items()},
        repo_root=root,
    )
    out = root / "reports" / f"regression-{_safe(old_snapshot_id)}-{_safe(new_snapshot_id)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


# --- S04: compare what the manifest names ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhaseComparison:
    kind: str
    status: str  # compared | coverage-gap | incomparable
    resolved: tuple[str, ...] = ()
    new: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "status": self.status, "resolved": list(self.resolved),
            "new": list(self.new), "unchanged": list(self.unchanged), "reason": self.reason,
        }


@dataclass(slots=True)
class RegressionComparison:
    old_snapshot_id: str
    new_snapshot_id: str
    phases: dict[str, PhaseComparison] = field(default_factory=dict)

    @property
    def summary(self) -> dict[str, int]:
        compared = [p for p in self.phases.values() if p.status == "compared"]
        return {
            "phases_compared": len(compared),
            "phases_coverage_gap": sum(
                1 for p in self.phases.values() if p.status == "coverage-gap"
            ),
            "phases_incomparable": sum(
                1 for p in self.phases.values() if p.status == "incomparable"
            ),
            "resolved": sum(len(p.resolved) for p in compared),
            "new": sum(len(p.new) for p in compared),
            "unchanged": sum(len(p.unchanged) for p in compared),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "old_snapshot_id": self.old_snapshot_id,
            "new_snapshot_id": self.new_snapshot_id,
            "summary": self.summary,
            "phases": {kind: phase.as_dict() for kind, phase in sorted(self.phases.items())},
        }

    def render(self) -> str:
        s = self.summary
        lines = [
            f"# bear-hug regression — {self.old_snapshot_id} → {self.new_snapshot_id}",
            "",
            f"phases compared {s['phases_compared']}, coverage gaps {s['phases_coverage_gap']}, "
            f"incomparable {s['phases_incomparable']}; resolved {s['resolved']}, new {s['new']}, "
            f"unchanged {s['unchanged']}",
            "",
            "| phase | status | resolved | new | unchanged | reason |",
            "|---|---|---|---|---|---|",
        ]
        for kind in PHASES:
            p = self.phases.get(kind)
            if p is None:
                continue
            lines.append(
                f"| {kind} | {p.status} | {len(p.resolved)} | {len(p.new)} | {len(p.unchanged)} | "
                f"{p.reason or '—'} |"
            )
        for kind in PHASES:
            p = self.phases.get(kind)
            if p and p.status == "compared" and (p.resolved or p.new):
                lines += ["", f"## {kind}", ""]
                lines += [f"- resolved `{i}`" for i in p.resolved]
                lines += [f"- new `{i}`" for i in p.new]
        lines += [
            "",
            "## Limit",
            "",
            "A snapshot pair bounds a window, never a day: an empty diff says the two artifact "
            "sets agree, not that nothing happened between them. A coverage gap is missing "
            "evidence, not resolution; an incomparable phase is neither improvement nor "
            "regression.",
            "",
        ]
        return "\n".join(lines)


def _load_ids(path: Path) -> list[str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        return None
    return [str(f.get("id")) for f in findings if isinstance(f, dict) and f.get("id")]


def compare_phases(manifest: dict[str, Any], *, root: Path | str) -> RegressionComparison:
    """S04 — resolved / new / unchanged per phase, reading ONLY the artifacts the manifest names.

    Nothing is rebuilt here. A phase present on one side only is a coverage gap; a replay phase
    whose corpus digest differs from the manifest's, or an artifact whose ids disagree with what
    the manifest recorded for it, is incomparable — never improvement, never regression.
    """
    root = Path(root)
    old_id, new_id = manifest["old_snapshot_id"], manifest["new_snapshot_id"]
    comparison = RegressionComparison(old_id, new_id)
    corpus_digest = (manifest.get("corpus") or {}).get("digest")
    by_side: dict[tuple[str, str], dict[str, Any]] = {}
    for phase in manifest.get("phases") or []:
        by_side[(phase["kind"], phase["snapshot_id"])] = phase

    for kind in PHASES:
        old_phase, new_phase = by_side.get((kind, old_id)), by_side.get((kind, new_id))
        if old_phase is None or new_phase is None:
            missing = [sid for sid, p in ((old_id, old_phase), (new_id, new_phase)) if p is None]
            comparison.phases[kind] = PhaseComparison(
                kind, "coverage-gap", reason=f"phase missing for {', '.join(missing)}"
            )
            continue
        if kind == "replay":
            digests = {old_phase.get("corpus_digest"), new_phase.get("corpus_digest")}
            if digests != {corpus_digest}:
                comparison.phases[kind] = PhaseComparison(
                    kind, "incomparable",
                    reason="the two replay artifacts do not share the manifest's corpus digest",
                )
                continue
        sides: dict[str, list[str]] = {}
        problem = ""
        for label, phase in (("old", old_phase), ("new", new_phase)):
            ids = _load_ids(root / phase["findings_path"])
            if ids is None:
                problem = f"{label} artifact {phase['findings_path']} is unreadable"
                break
            if sorted(ids) != sorted(phase.get("finding_ids") or []):
                problem = (
                    f"{label} artifact {phase['findings_path']} carries ids that disagree with "
                    "the manifest's record of it"
                )
                break
            sides[label] = ids
        if problem:
            comparison.phases[kind] = PhaseComparison(kind, "incomparable", reason=problem)
            continue
        old_ids, new_ids = set(sides["old"]), set(sides["new"])
        comparison.phases[kind] = PhaseComparison(
            kind, "compared",
            resolved=tuple(sorted(old_ids - new_ids)),
            new=tuple(sorted(new_ids - old_ids)),
            unchanged=tuple(sorted(old_ids & new_ids)),
        )
    return comparison


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "PhaseComparison",
    "RegressionComparison",
    "compare_phases",
    "manifest_from_findings",
    "runtime_tree_sha256",
    "write_regression_manifest",
    "PHASES",
    "build_regression_manifest",
    "validate_regression_manifest",
]
