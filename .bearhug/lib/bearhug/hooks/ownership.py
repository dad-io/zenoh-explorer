"""Recognizing hooks Bear Hug itself installed, from the project's own setup receipt.

A `hooks audit` COVERAGE finding about `bearhug_work.py` or `stop-coordinator.py` "wired and no
test file names it" is true and useless: those hooks' tests live in Bear Hug's own suite, not
the project's, so the finding reports noise from Bear Hug's own report about itself. Filename
matching would separate the two lists today, but it is a guess dressed as a fact — a project
that vendors its own `stop-coordinator.py` would be misclassified silently. The setup receipt
Bear Hug writes at install time (`.bearhug/project-setup.json`) is Bear Hug's own record of
exactly which paths and components it put there, so recognition reads that record instead.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from bearhug.read_optional import read_json_or_default

RECEIPT_RELPATH = Path(".bearhug/project-setup.json")


def load_setup_receipt(snapshot_dir: Path) -> dict[str, Any] | None:
    """The setup receipt captured in this snapshot's project tree, or None.

    None collapses two different states this module cannot itself tell apart: the snapshot
    predates the capture rule for this file (`bearhug.snapshot.spec`), or the live project was
    genuinely never set up by Bear Hug. Callers state that ambiguity rather than treating a
    missing receipt as proof the project owns every hook itself.
    """
    path = Path(snapshot_dir) / "project" / RECEIPT_RELPATH
    value, absent = read_json_or_default(path)
    if absent or not isinstance(value, dict):
        return None
    if value.get("record_kind") != "project_setup_receipt":
        return None
    return value


def installed_paths(receipt: dict[str, Any] | None) -> dict[str, str]:
    """Project-relative path -> component, for every file the receipt says Bear Hug installed.

    Keyed on the exact path the receipt recorded (`_receipt` in `project_setup.py`), the same
    identity setup itself uses for conflict detection — not on a basename, which two hooks in
    different directories could share.
    """
    if receipt is None:
        return {}
    files = receipt.get("files")
    if not isinstance(files, list):
        return {}
    result: dict[str, str] = {}
    for row in files:
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            component = row.get("component")
            result[row["path"]] = component if isinstance(component, str) else ""
    return result


def receipt_sha256(receipt: dict[str, Any] | None) -> dict[str, str]:
    """Project-relative path -> the sha256 the receipt recorded for it at install time.

    `_receipt` in `project_setup.py` already writes this digest per row for its own conflict
    detection; reused here so a hook whose *bytes* have since diverged from what Bear Hug
    installed (a project customizing an installed hook's script body, the scenario
    `project_setup.py`'s own conflict detection exists to catch at setup time) is not silently
    reported as covered by upstream tests that were never run against these bytes.
    """
    if receipt is None:
        return {}
    files = receipt.get("files")
    if not isinstance(files, list):
        return {}
    result: dict[str, str] = {}
    for row in files:
        if (
            isinstance(row, dict)
            and isinstance(row.get("path"), str)
            and isinstance(row.get("sha256"), str)
        ):
            result[row["path"]] = row["sha256"]
    return result


def snapshot_file_sha256(snapshot_dir: Path, relpath: str) -> str | None:
    """The sha256 of `relpath`'s bytes as CAPTURED in this snapshot's project tree, or None.

    Reads the snapshot, never the live project — the same discipline `load_setup_receipt` and
    every other audit read observes, so a finding never cites bytes it didn't actually capture.
    None means the path is missing from the snapshot (a hook removed since install, or a
    snapshot rule that didn't reach it), which a caller treats as "cannot verify" rather than as
    proof of anything about the file's content.
    """
    path = Path(snapshot_dir) / "project" / relpath
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_label(snapshot_dir: Path) -> str:
    """The Bear Hug release identity pinned into this snapshot's own manifest.

    The setup receipt records no version of its own — `_receipt` in `project_setup.py` predates
    this need, and adding one is a larger, riskier change to a schema `_load_previous_receipt`
    already treats as load-bearing than this track's scope justifies. `manifest.json`'s
    `instrument` block (`bearhug.snapshot.capture.instrument`) already pins the exact
    `bearhug_version` and `git_commit` that took the snapshot; since one checkout drives both
    `bearhug setup` and `bearhug hooks audit` in the deployment this audit is written for, that
    is also the release that performed the install. Falls back to "unknown" rather than raising:
    naming the release is a courtesy on top of the ownership finding, not a precondition for it.
    """
    manifest, absent = read_json_or_default(Path(snapshot_dir) / "manifest.json")
    if absent or not isinstance(manifest, dict):
        return "unknown"
    instrument = manifest.get("instrument")
    if not isinstance(instrument, dict):
        return "unknown"
    version = instrument.get("bearhug_version")
    commit = instrument.get("git_commit")
    if isinstance(version, str) and version and isinstance(commit, str) and commit:
        return f"{version} ({commit[:8]})"
    if isinstance(version, str) and version:
        return version
    return "unknown"
