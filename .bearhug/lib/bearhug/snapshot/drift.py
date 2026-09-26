"""1.5 — has the live harness moved away from the snapshot a finding was written against?

Identity is the content hash, never the mtime: a rebuild that touches a file has not moved the
harness, and reporting it as drift would train the reader to ignore this command.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from bearhug import paths
from bearhug.snapshot.capture import git_state, live_hashes, manifest_hashes


@dataclass(frozen=True, slots=True)
class DriftReport:
    """What changed between a stored snapshot and the live harness."""

    snapshot: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    stored_head: str | None = None
    live_head: str | None = None

    @property
    def head_moved(self) -> bool:
        """Did the subject's HEAD move, whether or not a captured file changed?

        Drift compares content hashes of captured harness files, so a commit touching only
        product code shows nothing — which is right about the harness and misleading about the
        evidence. Reported separately rather than folded into `moved`.
        """
        return bool(self.stored_head and self.live_head and self.stored_head != self.live_head)

    @property
    def moved(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def _head_line(self) -> str:
        return (
            f"  subject HEAD moved {self.stored_head[:12]} -> {self.live_head[:12]} "
            "(no captured harness file changed)"
        )

    def render(self) -> str:
        if not self.moved:
            head = f"\n{self._head_line()}" if self.head_moved else ""
            return (
                f"{self.snapshot}: no drift — findings written against it still hold.{head}"
            )
        lines = [f"{self.snapshot}: the harness has moved."]
        if self.head_moved:
            lines.append(self._head_line())
        for title, rows in (
            ("changed", self.changed), ("added", self.added), ("removed", self.removed)
        ):
            for row in rows:
                lines.append(f"  {title:8s} {row}")
        lines.append("")
        lines.append(
            "Findings written against this snapshot may be stale. Re-run the phase that "
            "produced them, or take a new snapshot and re-measure."
        )
        return "\n".join(lines)


def compute_drift(
    snapshot_dir: Path,
    *,
    barracuda_root: Path | None = None,
    claude_home: Path | None = None,
) -> DriftReport:
    """Compare a snapshot's manifest against the live harness."""
    snapshot_dir = Path(snapshot_dir)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    barracuda_root = Path(barracuda_root or paths.BARRACUDA_ROOT).expanduser().resolve()
    claude_home = Path(claude_home or paths.CLAUDE_HOME).expanduser().resolve()

    stored = manifest_hashes(manifest)
    live = live_hashes(barracuda_root, claude_home)

    return DriftReport(
        snapshot=manifest.get("snapshot_id", snapshot_dir.name),
        stored_head=(manifest.get("subject", {}).get("barracuda") or {}).get("head"),
        live_head=git_state(barracuda_root).get("head"),
        added=sorted(set(live) - set(stored)),
        removed=sorted(set(stored) - set(live)),
        changed=sorted(key for key in set(stored) & set(live) if stored[key] != live[key]),
    )
