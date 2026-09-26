"""Resolve the frozen or live transcript corpus without widening its project scope.

The command line used to accept ``--corpus frozen`` and then ignore it.  This module is the
single authority for corpus selection: live means exactly ``paths.transcripts_dir()``; frozen
means only members beneath that same encoded project directory in ``projects.zip``.  Archive
members are materialised into a temporary directory and never into ``~/.claude``.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from bearhug.paths import frozen_corpus, transcripts_dir
from bearhug.replay.transcript import session_transcripts

MAX_CORPUS_BYTES = 2_000_000_000


class CorpusError(RuntimeError):
    """The requested corpus is absent, malformed, or contains no in-scope transcripts."""


@dataclass(frozen=True, slots=True)
class CorpusMember:
    """One logical transcript in a pinned corpus."""

    relpath: str
    sha256: str
    bytes: int


@dataclass(frozen=True, slots=True)
class CorpusSelection:
    """One explicitly scoped, hash-pinned set of transcript files."""

    kind: str
    source: Path
    paths: tuple[Path, ...]
    members: tuple[CorpusMember, ...]
    digest: str
    #: The scratch root every path in `paths` is relative to. `paths` follows
    #: session_transcripts() order (top level, then subagents) while `members` is sorted by
    #: relpath; only a relpath computed from this root pairs a path with its own manifest row.
    #: A positional zip does not.
    root: Path | None = None
    #: Transcripts found under the source but deliberately not read, as (relative path, reason).
    #: A symlink is the one reason today: Claude Code links a resumed session's subagent
    #: transcript into the original session's directory, and following it would count one file
    #: twice or read outside the copied tree. Recorded so the omission is visible, never silent.
    excluded: tuple[tuple[str, str], ...] = ()

    def member_for(self, path: Path) -> CorpusMember:
        """The manifest row for one of `paths`, by relative path — never by position."""
        by_relpath = {member.relpath: member for member in self.members}
        if self.root is not None:
            return by_relpath[path.relative_to(self.root).as_posix()]
        posix = Path(path).as_posix()
        matches = [m for rel, m in by_relpath.items() if posix.endswith(rel)]
        if len(matches) != 1:
            raise KeyError(f"no unique manifest row for {path}")
        return matches[0]

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.digest[:12]}"


def _manifest(
    paths: tuple[Path, ...], *, root: Path
) -> tuple[tuple[CorpusMember, ...], str]:
    """Hash each file and the ordered set so pruning or replacement changes identity."""
    digest = hashlib.sha256()
    members = []
    ordered = sorted(paths, key=lambda item: item.relative_to(root).as_posix())
    for path in ordered:
        relative = path.relative_to(root).as_posix()
        file_digest = hashlib.sha256()
        size = 0
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(chunk)
                digest.update(chunk)
                size += len(chunk)
        digest.update(b"\0")
        members.append(CorpusMember(relative, file_digest.hexdigest(), size))
    return tuple(members), digest.hexdigest()


def _copy_transcripts(
    source: Path, destination: Path
) -> tuple[tuple[Path, ...], tuple[tuple[str, str], ...]]:
    """Freeze one live directory into scratch before hashing or analysis.

    Returns the copied paths and the transcripts excluded with a reason."""
    original = tuple(session_transcripts(source))
    if not original:
        raise CorpusError(f"no transcripts under {source}")
    copied = []
    excluded: list[tuple[str, str]] = []
    total = 0
    for path in original:
        if path.is_symlink():
            excluded.append((path.relative_to(source).as_posix(), "symlink"))
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as incoming, target.open("wb") as outgoing:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > MAX_CORPUS_BYTES:
                        raise CorpusError(
                            f"corpus exceeds {MAX_CORPUS_BYTES} byte extraction limit"
                        )
                    outgoing.write(chunk)
        except OSError as exc:
            raise CorpusError(f"live corpus changed while copying {path}: {exc}") from exc
        copied.append(target)
    if not copied:
        raise CorpusError(f"no readable transcripts under {source} (all excluded)")
    return tuple(copied), tuple(excluded)


def _safe_member(member: str, project_key: str) -> tuple[str, ...] | None:
    """Return the path below the selected project, or None for out-of-scope members."""
    path = PurePosixPath(member)
    if path.is_absolute() or ".." in path.parts:
        raise CorpusError(f"unsafe member in frozen corpus: {member!r}")
    # Finder adds AppleDouble resource forks under __MACOSX. They have `.jsonl` suffixes but
    # are metadata, not transcripts; counting them doubled the frozen denominator on first run.
    if "__MACOSX" in path.parts or path.name.startswith("._"):
        return None
    try:
        project_at = path.parts.index(project_key)
    except ValueError:
        return None
    relative = path.parts[project_at + 1 :]
    if not relative or not relative[-1].endswith(".jsonl"):
        return None
    # Match the two layouts session_transcripts() recognises.  Other nested projects and
    # unrelated Claude stores remain out of scope even if they happen to contain JSONL.
    if len(relative) == 1:
        return relative
    if len(relative) == 3 and relative[1] == "subagents":
        return relative
    return None


@contextmanager
def select_corpus(
    kind: str,
    *,
    live_dir: Path | None = None,
    archive: Path | None = None,
    project_key: str | None = None,
) -> Iterator[CorpusSelection]:
    """Yield the requested corpus and clean up any frozen scratch extraction afterwards."""
    if kind not in {"frozen", "live"}:
        raise CorpusError(f"unknown corpus {kind!r}; expected 'frozen' or 'live'")

    selected_live = Path(live_dir) if live_dir is not None else transcripts_dir()
    if kind == "live":
        with tempfile.TemporaryDirectory(prefix="bearhug-live-corpus-") as scratch_name:
            root = Path(scratch_name) / selected_live.name
            paths, excluded = _copy_transcripts(selected_live, root)
            members, digest = _manifest(paths, root=root)
            yield CorpusSelection("live", selected_live, paths, members, digest, root, excluded)
        return

    located = frozen_corpus()
    selected_archive = Path(archive) if archive is not None else located.path
    if not selected_archive.is_file():
        raise CorpusError(
            f"frozen corpus archive does not exist: {selected_archive} ({located.note})"
        )
    key = project_key or selected_live.name
    with tempfile.TemporaryDirectory(prefix="bearhug-corpus-") as scratch_name:
        root = Path(scratch_name) / key
        extracted: list[Path] = []
        total = 0
        seen: set[tuple[str, ...]] = set()
        try:
            zipped = zipfile.ZipFile(selected_archive)
        except (OSError, zipfile.BadZipFile) as exc:
            raise CorpusError(f"cannot read frozen corpus {selected_archive}: {exc}") from exc
        with zipped:
            for info in zipped.infolist():
                relative = _safe_member(info.filename, key)
                if relative is None or info.is_dir():
                    continue
                if relative in seen:
                    raise CorpusError(f"duplicate transcript member in frozen corpus: {relative}")
                seen.add(relative)
                total += info.file_size
                if total > MAX_CORPUS_BYTES:
                    raise CorpusError(
                        f"frozen corpus exceeds {MAX_CORPUS_BYTES} byte extraction limit"
                    )
                target = root.joinpath(*relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(info) as source, target.open("wb") as destination:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        destination.write(chunk)
                extracted.append(target)
        paths = tuple(session_transcripts(root))
        if not paths:
            raise CorpusError(f"no transcripts for encoded project {key!r} in {selected_archive}")
        if len(paths) != len(extracted):
            raise CorpusError("frozen corpus extraction and transcript discovery disagree")
        members, digest = _manifest(paths, root=root)
        yield CorpusSelection("frozen", selected_archive, paths, members, digest, root)


__all__ = [
    "MAX_CORPUS_BYTES",
    "CorpusError",
    "CorpusMember",
    "CorpusSelection",
    "select_corpus",
]
