"""P03 — is the INSTALLED runtime the candidate that was validated?

The identity is computed by READING the installed files, never by importing the package and asking
it. Executing a promoted artifact to learn what it is trusts the thing under examination: a
tampered runtime could report whatever hash it liked and the check would agree.

A missing installation is reported as ABSENT, never as unchanged. "Absent" and "unchanged" looking
alike is the defect class this repo keeps finding, and `a55f66b` established the companion rule
that a label is not an identity — an installation whose VERSION still reads 1.0.0 while its bytes
moved must not pass.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bearhug.runtime_package import PROMOTED_INSTALL_PATH

#: Excluded exactly as the runtime's own hash excludes them. Importing the runtime creates a
#: `__pycache__`; if it counted, every installation would drift the first time anything ran.
_EXCLUDED_DIRS = ("__pycache__",)
_EXCLUDED_SUFFIXES = (".pyc",)
_EXCLUDED_NAMES = (".DS_Store",)


@dataclass(frozen=True, slots=True)
class InstalledRuntime:
    """What is actually on disk at the promotion target."""

    present: bool
    version: str | None = None
    sha256: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    root: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DriftVerdict:
    """Whether the installation is the candidate, and if not, exactly how it differs."""

    matches: bool
    absent: bool = False
    installed_sha256: str | None = None
    candidate_sha256: str | None = None
    candidate_commit: str | None = None
    changed_files: tuple[str, ...] = ()
    added_files: tuple[str, ...] = ()
    missing_files: tuple[str, ...] = ()
    reason: str | None = None


def _promoted_dir(root: Any) -> Path:
    return Path(root) / PROMOTED_INSTALL_PATH


def read_installed_runtime(root: Any) -> InstalledRuntime:
    """Read the installed runtime's identity from its files. Never imports it."""
    directory = _promoted_dir(root)
    if not directory.is_dir():
        return InstalledRuntime(
            present=False, root=str(directory),
            reason=f"no runtime installed at {PROMOTED_INSTALL_PATH}",
        )

    files: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        if any(part in _EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.suffix in _EXCLUDED_SUFFIXES or path.name in _EXCLUDED_NAMES:
            continue
        files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()

    if "VERSION" not in files:
        return InstalledRuntime(
            present=False, root=str(directory), files=files,
            reason="the installation has no VERSION file, so it is incomplete rather than a "
                   "runtime whose identity merely differs",
        )

    # The same canonicalization the runtime uses on itself, recomputed here from bytes rather than
    # asked of the code under examination.
    digest = hashlib.sha256()
    for relative in sorted(files):
        blob = (directory / relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(len(blob)).encode("ascii"))
        digest.update(b"\x00")
        digest.update(blob)

    return InstalledRuntime(
        present=True,
        version=(directory / "VERSION").read_text(encoding="utf-8").strip(),
        sha256=digest.hexdigest(),
        files=files,
        root=str(directory),
    )


def compare_to_candidate(
    installed: InstalledRuntime, manifest: dict[str, Any]
) -> DriftVerdict:
    """Does the installation match the validated candidate, and if not, where?"""
    candidate_files = {entry["path"]: entry["sha256"] for entry in manifest["files"]}

    if not installed.present:
        return DriftVerdict(
            matches=False, absent=True,
            candidate_sha256=manifest["runtime_sha256"],
            candidate_commit=manifest.get("source_commit"),
            reason=installed.reason,
        )

    changed = tuple(sorted(
        name for name, digest in candidate_files.items()
        if name in installed.files and installed.files[name] != digest
    ))
    added = tuple(sorted(set(installed.files) - set(candidate_files)))
    missing = tuple(sorted(set(candidate_files) - set(installed.files)))
    # Hash equality is the authority; the file lists say WHERE it differs. A matching VERSION is
    # never sufficient — that is a55f66b's lesson at the runtime layer.
    matches = installed.sha256 == manifest["runtime_sha256"]

    return DriftVerdict(
        matches=matches,
        installed_sha256=installed.sha256,
        candidate_sha256=manifest["runtime_sha256"],
        candidate_commit=manifest.get("source_commit"),
        changed_files=changed,
        added_files=added,
        missing_files=missing,
        reason=None if matches else "installed runtime does not match the validated candidate",
    )
