"""S02 — `patches/APPLIED.md`: the evidence-linked changelog of patches a person accepted.

Bear Hug emits proposals; a person applies them in Barracuda by hand (docs/CHARTER.md). This file
records only what that person, or a Barracuda return, REPORTS: the date, the Barracuda commit the
patch landed in, the finding that justified it, the snapshot the finding was measured against, the
digest of the patch file as accepted, and the measurement in one line. Bear Hug never infers
acceptance from a finding disappearing between two reports — a resolved finding may mean the text
moved for another reason, and an accepted patch may not resolve the finding at all.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from bearhug.paths import PATCHES_DIR, assert_writable

APPLIED_HEADER = """# APPLIED — patches a person accepted, one row each

Bear Hug records here only what Sam or a Barracuda-owned return REPORTED. It never infers
acceptance from a finding disappearing, and it never applies a patch itself. Every row names the
Barracuda commit the patch landed in (a full hash — a short one is a label), the finding that
justified it, the snapshot that finding was measured against, and the sha256 of the patch file as
accepted; `validate_applied` refuses a row whose patch file has since moved.

| date | barracuda commit | finding | snapshot | patch sha256 | measurement |
|---|---|---|---|---|---|
"""

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class AppliedEntry:
    date: str
    barracuda_commit: str
    finding_id: str
    snapshot_id: str
    patch_sha256: str
    measurement: str

    def validate(self) -> None:
        if not _DATE.match(self.date):
            raise ValueError(f"date {self.date!r} is not YYYY-MM-DD")
        if not _COMMIT.match(self.barracuda_commit):
            raise ValueError(
                f"barracuda commit {self.barracuda_commit!r} is not a full 40-hex hash"
            )
        if not self.finding_id.strip():
            raise ValueError("finding id is empty")
        if not self.snapshot_id.strip():
            raise ValueError("snapshot id is empty")
        if not _SHA256.match(self.patch_sha256):
            raise ValueError(f"patch sha256 {self.patch_sha256!r} is not 64 hex characters")
        if "|" in self.measurement or "\n" in self.measurement:
            raise ValueError("measurement must be one line without a pipe")

    def row(self) -> str:
        return (
            f"| {self.date} | `{self.barracuda_commit}` | `{self.finding_id}` | "
            f"`{self.snapshot_id}` | `{self.patch_sha256}` | {self.measurement} |\n"
        )


_ROW = re.compile(
    r"^\|\s*(?P<date>[^|]+?)\s*\|\s*`(?P<commit>[^`]+)`\s*\|\s*`(?P<finding>[^`]+)`\s*\|\s*"
    r"`(?P<snapshot>[^`]+)`\s*\|\s*`(?P<sha>[^`]+)`\s*\|\s*(?P<measurement>.*?)\s*\|\s*$"
)


def parse_applied(path: Path | str) -> list[AppliedEntry]:
    entries: list[AppliedEntry] = []
    text = Path(path).read_text(encoding="utf-8") if Path(path).is_file() else ""
    for line in text.splitlines():
        match = _ROW.match(line)
        if not match or match.group("date") in ("date", "---"):
            continue
        entries.append(
            AppliedEntry(
                date=match.group("date"),
                barracuda_commit=match.group("commit"),
                finding_id=match.group("finding"),
                snapshot_id=match.group("snapshot"),
                patch_sha256=match.group("sha"),
                measurement=match.group("measurement"),
            )
        )
    return entries


def append_entry(path: Path | str, entry: AppliedEntry) -> Path:
    """Append one reported acceptance. Refuses a malformed row and a duplicate finding."""
    entry.validate()
    target = assert_writable(Path(path))
    existing = parse_applied(target)
    if any(e.finding_id == entry.finding_id for e in existing):
        raise ValueError(f"finding {entry.finding_id!r} is already recorded as applied")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        target.write_text(APPLIED_HEADER, encoding="utf-8")
    with target.open("a", encoding="utf-8") as handle:
        handle.write(entry.row())
    return target


def validate_applied(path: Path | str, *, patches_dir: Path | str | None = None) -> list[str]:
    """Every way the log can be wrong that Bear Hug can check without asking anyone."""
    root = Path(patches_dir) if patches_dir is not None else PATCHES_DIR
    problems: list[str] = []
    seen: set[str] = set()
    for entry in parse_applied(path):
        try:
            entry.validate()
        except ValueError as exc:
            problems.append(f"{entry.finding_id}: {exc}")
            continue
        if entry.finding_id in seen:
            problems.append(f"{entry.finding_id}: recorded twice")
        seen.add(entry.finding_id)
        patch = root / f"{entry.finding_id}.diff"
        if not patch.is_file():
            problems.append(f"{entry.finding_id}: no patch file {patch.name} exists to accept")
            continue
        actual = hashlib.sha256(patch.read_bytes()).hexdigest()
        if actual != entry.patch_sha256:
            problems.append(
                f"{entry.finding_id}: the patch file has moved since acceptance (digest "
                f"{actual[:12]} vs recorded {entry.patch_sha256[:12]}) — a stale acceptance"
            )
    return problems


__all__ = ["APPLIED_HEADER", "AppliedEntry", "append_entry", "parse_applied", "validate_applied"]
