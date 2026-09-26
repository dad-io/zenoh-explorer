"""Observational preflight for one explicitly selected subject worktree.

Prime does not install, launch, trust, or join anything.  It reads enough local state to make the
operator's next two commands exact: immutable Git identity, content-sensitive dirty identity, the
latest stored snapshot's drift, supported executable versions, and cockpit freshness.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug import paths
from bearhug.paths import REPO_ROOT, REPORTS_DIR, SNAPSHOTS_DIR
from bearhug.snapshot import compute_drift
from bearhug.watcher import classify_watcher

COCKPIT_FRESH_SECONDS = 15 * 60
PROBE_TIMEOUT_SECONDS = 10

# These are the provider CLIs plus the executables needed by the printed operator flow.
# A probe only asks the executable to identify itself; it never starts a provider session or build.
SUPPORTED_BINARIES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("git", ("--version",), "repository inspection"),
    ("uv", ("--version",), "Bear Hug command runner"),
    ("go", ("version",), "Bear Hug TUI"),
    ("claude", ("--version",), "provider CLI"),
    ("codex", ("--version",), "provider CLI"),
    ("pi", ("--version",), "provider CLI"),
    # gopls is detected, never installed or downloaded, and its absence is
    # never an error -- it behaves like every other row above, just with a purpose that says so.
    ("gopls", ("version",), "optional Go code intelligence"),
)

LIMITATIONS: tuple[str, ...] = (
    "provider process, session, thread, turn, task, and goal authority were not inspected",
    "project trust and exact hook-definition trust were not inspected",
    "the optional persistent watcher and self-refreshing TUI commands were printed, not started",
    "Prime wrote no report or subject file",
)


class PrimeError(RuntimeError):
    """The requested subject cannot produce a closed observational report."""


class PrimeReportError(ValueError):
    """A prime report is not closed, canonical, or content-addressed correctly."""


@dataclass(frozen=True, slots=True)
class GitIdentity:
    requested_path: str
    toplevel: str
    common_dir: str
    worktree: str
    branch: str | None
    detached: bool
    head: str
    clean: bool
    dirty_entries: int
    dirty_sha256: str


@dataclass(frozen=True, slots=True)
class SnapshotObservation:
    status: str
    path: str | None
    snapshot_id: str | None
    moved: bool | None
    head_moved: bool | None
    changed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class BinaryObservation:
    name: str
    purpose: str
    present: bool
    path: str | None
    version: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class CockpitObservation:
    path: str
    status: str
    generated_at: str | None
    age_seconds: float | None
    fresh: bool
    fresh_within_seconds: int
    reason: str | None
    watcher: WatcherObservation


@dataclass(frozen=True, slots=True)
class WatcherObservation:
    status: str
    watcher_id: str | None
    heartbeat_at: str | None
    age_seconds: float | None
    stale_after_seconds: int | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class PrimeCommands:
    cwd: str
    watcher_environment: tuple[tuple[str, str], ...]
    watcher_argv: tuple[str, ...]
    tui_environment: tuple[tuple[str, str], ...]
    tui_argv: tuple[str, ...]

    @staticmethod
    def _shell(environment: tuple[tuple[str, str], ...], argv: tuple[str, ...]) -> str:
        assignments = " ".join(f"{name}={shlex.quote(value)}" for name, value in environment)
        command = shlex.join(argv)
        return f"{assignments} {command}" if assignments else command

    @property
    def watcher_shell(self) -> str:
        return self._shell(self.watcher_environment, self.watcher_argv)

    @property
    def tui_shell(self) -> str:
        return self._shell(self.tui_environment, self.tui_argv)


@dataclass(frozen=True, slots=True)
class PrimeReport:
    schema_version: str
    record_kind: str
    observed_at: str
    git: GitIdentity
    snapshot: SnapshotObservation
    binaries: tuple[BinaryObservation, ...]
    cockpit: CockpitObservation
    commands: PrimeCommands
    content_sha256: str
    limitations: tuple[str, ...] = LIMITATIONS

    def to_dict(self) -> dict[str, Any]:
        # ``asdict`` preserves tuples. Round-tripping produces the exact JSON-native tree whose
        # canonical serialization is content-addressed below.
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    def render_json(self) -> str:
        """Render stdout-ready JSON without writing a report or subject file."""

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def render(self) -> str:
        branch = self.git.branch or "DETACHED"
        dirty = "clean" if self.git.clean else f"dirty ({self.git.dirty_entries} entries)"
        lines = [
            "Bear Hug prime — observational preflight",
            f"observed     {self.observed_at}",
            f"requested    {self.git.requested_path}",
            f"worktree     {self.git.worktree}",
            f"common dir   {self.git.common_dir}",
            f"git          {branch} @ {self.git.head} · {dirty}",
            f"dirty sha    {self.git.dirty_sha256}",
        ]

        if self.snapshot.path is None:
            lines.append(f"snapshot     {self.snapshot.status}: {self.snapshot.reason}")
        else:
            detail = self.snapshot.status
            if self.snapshot.head_moved:
                detail += ", HEAD moved"
            lines.append(
                f"snapshot     {self.snapshot.snapshot_id} · {detail} · {self.snapshot.path}"
            )

        cockpit_detail = self.cockpit.status
        if self.cockpit.age_seconds is not None:
            cockpit_detail += f" ({self.cockpit.age_seconds:.0f}s old)"
        lines.append(f"cockpit      {cockpit_detail} · {self.cockpit.path}")
        watcher_detail = self.cockpit.watcher.status
        if self.cockpit.watcher.age_seconds is not None:
            watcher_detail += f" ({self.cockpit.watcher.age_seconds:.0f}s old)"
        if self.cockpit.watcher.reason:
            watcher_detail += f": {self.cockpit.watcher.reason}"
        lines.append(f"watcher      {watcher_detail}")
        lines.append("binaries")
        for binary in self.binaries:
            identity = binary.version or binary.reason or "unknown"
            location = f" · {binary.path}" if binary.path else ""
            lines.append(f"  {binary.name:8s} {identity}{location}")

        lines.extend(["", "Not inspected"])
        lines.extend(f"  - {item}" for item in self.limitations)
        lines.extend(
            [
                "",
                "Optional persisted cockpit",
                f"  cd {shlex.quote(self.commands.cwd)}",
                f"  {self.commands.watcher_shell}",
                "",
                "Live TUI (self-refreshing private cockpit)",
                f"  cd {shlex.quote(self.commands.cwd)}",
                f"  {self.commands.tui_shell}",
            ]
        )
        return "\n".join(lines)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "observed_at",
        "git",
        "snapshot",
        "binaries",
        "cockpit",
        "commands",
        "limitations",
        "content_sha256",
    }
)


def _canonical_report_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise PrimeReportError(f"prime report is not canonical JSON: {exc}") from exc
    return (rendered + "\n").encode("utf-8")


def prime_report_sha256(value: PrimeReport | Mapping[str, Any]) -> str:
    """Hash canonical report content after removing its self digest."""

    material = value.to_dict() if isinstance(value, PrimeReport) else dict(value)
    material.pop("content_sha256", None)
    return hashlib.sha256(_canonical_report_bytes(material)).hexdigest()


def _closed(value: Any, fields: set[str] | frozenset[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise PrimeReportError(f"{where} has missing or unknown fields")
    return value


def _string(value: Any, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise PrimeReportError(f"{where} must be a non-empty string")
    return value


def _boolean(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise PrimeReportError(f"{where} must be a boolean")
    return value


def _integer(value: Any, where: str) -> int:
    if type(value) is not int or value < 0:
        raise PrimeReportError(f"{where} must be a non-negative integer")
    return value


def _number(value: Any, where: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PrimeReportError(f"{where} must be a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise PrimeReportError(f"{where} must be a finite number")
    return value


def _string_array(value: Any, where: str, *, unique: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise PrimeReportError(f"{where} must be an array")
    for index, item in enumerate(value):
        _string(item, f"{where}[{index}]")
    if unique and len(value) != len(set(value)):
        raise PrimeReportError(f"{where} must not contain duplicates")
    return value


def _environment(value: Any, where: str) -> list[list[str]]:
    if not isinstance(value, list):
        raise PrimeReportError(f"{where} must be an array")
    names: list[str] = []
    for index, pair in enumerate(value):
        if not isinstance(pair, list) or len(pair) != 2:
            raise PrimeReportError(f"{where}[{index}] must be one name/value pair")
        name = _string(pair[0], f"{where}[{index}][0]")
        _string(pair[1], f"{where}[{index}][1]")
        assert name is not None
        names.append(name)
    if len(names) != len(set(names)):
        raise PrimeReportError(f"{where} must not repeat an environment name")
    return value


def validate_prime_report(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed report and self digest without reading ambient state."""

    if not isinstance(value, Mapping):
        raise PrimeReportError("prime report must be an object")
    report = json.loads(_canonical_report_bytes(dict(value)))
    _closed(report, _REPORT_FIELDS, "prime report")
    if report["schema_version"] != "1" or report["record_kind"] != "prime_report":
        raise PrimeReportError("prime report discriminator is unsupported")
    observed_at = _string(report["observed_at"], "observed_at")
    try:
        parsed = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise PrimeReportError("observed_at must be a canonical UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != observed_at:
        raise PrimeReportError("observed_at must be a canonical UTC timestamp")

    git = _closed(
        report["git"],
        {
            "requested_path",
            "toplevel",
            "common_dir",
            "worktree",
            "branch",
            "detached",
            "head",
            "clean",
            "dirty_entries",
            "dirty_sha256",
        },
        "git",
    )
    for field in ("requested_path", "toplevel", "common_dir", "worktree"):
        path = _string(git[field], f"git.{field}")
        if not Path(path).is_absolute():
            raise PrimeReportError(f"git.{field} must be absolute")
    branch = _string(git["branch"], "git.branch", optional=True)
    detached = _boolean(git["detached"], "git.detached")
    if detached != (branch is None):
        raise PrimeReportError("git.detached and git.branch disagree")
    if git["toplevel"] != git["worktree"]:
        raise PrimeReportError("git.toplevel and git.worktree disagree")
    if not isinstance(git["head"], str) or _GIT_OID.fullmatch(git["head"]) is None:
        raise PrimeReportError("git.head must be a lowercase Git object id")
    clean = _boolean(git["clean"], "git.clean")
    dirty_entries = _integer(git["dirty_entries"], "git.dirty_entries")
    if clean != (dirty_entries == 0):
        raise PrimeReportError("git.clean and git.dirty_entries disagree")
    dirty_sha256 = git["dirty_sha256"]
    if not isinstance(dirty_sha256, str) or _SHA256.fullmatch(dirty_sha256) is None:
        raise PrimeReportError("git.dirty_sha256 must be lowercase SHA-256")

    snapshot = _closed(
        report["snapshot"],
        {
            "status",
            "path",
            "snapshot_id",
            "moved",
            "head_moved",
            "changed",
            "added",
            "removed",
            "reason",
        },
        "snapshot",
    )
    status = snapshot["status"]
    if status not in {"missing", "error", "drift", "no-drift"}:
        raise PrimeReportError("snapshot.status is unsupported")
    _string_array(snapshot["changed"], "snapshot.changed")
    _string_array(snapshot["added"], "snapshot.added")
    _string_array(snapshot["removed"], "snapshot.removed")
    if status in {"drift", "no-drift"}:
        _string(snapshot["path"], "snapshot.path")
        _string(snapshot["snapshot_id"], "snapshot.snapshot_id")
        moved = _boolean(snapshot["moved"], "snapshot.moved")
        head_moved = _boolean(snapshot["head_moved"], "snapshot.head_moved")
        if moved != (status == "drift") or (head_moved and not moved):
            raise PrimeReportError("snapshot status and movement fields disagree")
        if snapshot["reason"] is not None:
            raise PrimeReportError("a measured snapshot may not carry an error reason")
    else:
        if status == "missing" and snapshot["path"] is not None:
            raise PrimeReportError("a missing snapshot may not carry a path")
        if status == "error":
            _string(snapshot["path"], "snapshot.path")
        if any(snapshot[field] is not None for field in ("snapshot_id", "moved", "head_moved")):
            raise PrimeReportError("an unmeasured snapshot may not claim drift identity")
        if any(snapshot[field] for field in ("changed", "added", "removed")):
            raise PrimeReportError("an unmeasured snapshot may not claim changed paths")
        _string(snapshot["reason"], "snapshot.reason")

    binaries = report["binaries"]
    if not isinstance(binaries, list):
        raise PrimeReportError("binaries must be an array")
    binary_names: list[str] = []
    for index, raw in enumerate(binaries):
        binary = _closed(
            raw,
            {"name", "purpose", "present", "path", "version", "reason"},
            f"binaries[{index}]",
        )
        name = _string(binary["name"], f"binaries[{index}].name")
        assert name is not None
        binary_names.append(name)
        _string(binary["purpose"], f"binaries[{index}].purpose")
        present = _boolean(binary["present"], f"binaries[{index}].present")
        if present:
            _string(binary["path"], f"binaries[{index}].path")
            version = _string(binary["version"], f"binaries[{index}].version", optional=True)
            reason = _string(binary["reason"], f"binaries[{index}].reason", optional=True)
            if version is None and reason is None:
                raise PrimeReportError(f"binaries[{index}] has no probe result")
        elif binary["path"] is not None or binary["version"] is not None:
            raise PrimeReportError(f"binaries[{index}] claims identity for an absent binary")
        else:
            _string(binary["reason"], f"binaries[{index}].reason")
    if len(binary_names) != len(set(binary_names)):
        raise PrimeReportError("binaries must have unique names")

    cockpit = _closed(
        report["cockpit"],
        {
            "path",
            "status",
            "generated_at",
            "age_seconds",
            "fresh",
            "fresh_within_seconds",
            "reason",
            "watcher",
        },
        "cockpit",
    )
    cockpit_path = _string(cockpit["path"], "cockpit.path")
    if not Path(cockpit_path).is_absolute():
        raise PrimeReportError("cockpit.path must be absolute")
    cockpit_status = cockpit["status"]
    if cockpit_status not in {"missing", "malformed", "future", "fresh", "stale"}:
        raise PrimeReportError("cockpit.status is unsupported")
    fresh = _boolean(cockpit["fresh"], "cockpit.fresh")
    fresh_within = _integer(cockpit["fresh_within_seconds"], "cockpit.fresh_within_seconds")
    if fresh_within == 0:
        raise PrimeReportError("cockpit.fresh_within_seconds must be positive")
    if cockpit_status in {"missing", "malformed"}:
        if cockpit["generated_at"] is not None or cockpit["age_seconds"] is not None or fresh:
            raise PrimeReportError("an unreadable cockpit may not claim freshness")
        _string(cockpit["reason"], "cockpit.reason")
    else:
        _string(cockpit["generated_at"], "cockpit.generated_at")
        age = _number(cockpit["age_seconds"], "cockpit.age_seconds")
        if cockpit_status == "future":
            if age >= 0 or fresh:
                raise PrimeReportError("a future cockpit must have negative age and be non-fresh")
            _string(cockpit["reason"], "cockpit.reason")
        elif cockpit_status == "fresh":
            if age < 0 or age > fresh_within or not fresh or cockpit["reason"] is not None:
                raise PrimeReportError("fresh cockpit fields disagree")
        elif age <= fresh_within or fresh:
            raise PrimeReportError("stale cockpit fields disagree")
        else:
            _string(cockpit["reason"], "cockpit.reason")

    watcher = _closed(
        cockpit["watcher"],
        {
            "status",
            "watcher_id",
            "heartbeat_at",
            "age_seconds",
            "stale_after_seconds",
            "reason",
        },
        "cockpit.watcher",
    )
    watcher_status = watcher["status"]
    if watcher_status not in {
        "unreported", "malformed", "active", "stale", "wrong_subject", "replaced"
    }:
        raise PrimeReportError("cockpit.watcher.status is unsupported")
    if watcher_status in {"unreported", "malformed"}:
        if any(
            watcher[field] is not None
            for field in ("watcher_id", "heartbeat_at", "age_seconds", "stale_after_seconds")
        ):
            raise PrimeReportError("an unobserved watcher may not carry heartbeat identity")
        _string(watcher["reason"], "cockpit.watcher.reason")
    else:
        watcher_id = _string(watcher["watcher_id"], "cockpit.watcher.watcher_id")
        if watcher_id is None or _SHA256.fullmatch(watcher_id) is None:
            raise PrimeReportError("cockpit.watcher.watcher_id must be lowercase SHA-256")
        _string(watcher["heartbeat_at"], "cockpit.watcher.heartbeat_at")
        age = _number(watcher["age_seconds"], "cockpit.watcher.age_seconds")
        _integer(watcher["stale_after_seconds"], "cockpit.watcher.stale_after_seconds")
        if watcher["stale_after_seconds"] == 0:
            raise PrimeReportError("cockpit.watcher.stale_after_seconds must be positive")
        if watcher_status == "active" and (age < 0 or watcher["reason"] is not None):
            raise PrimeReportError("active watcher fields disagree")
        if watcher_status == "stale" and watcher["reason"] is None:
            raise PrimeReportError("stale watcher must carry a reason")
        if watcher_status in {"wrong_subject", "replaced"} and watcher["reason"] is None:
            raise PrimeReportError(f"{watcher_status} watcher must carry a reason")
        _string(watcher["reason"], "cockpit.watcher.reason", optional=True)

    commands = _closed(
        report["commands"],
        {"cwd", "watcher_environment", "watcher_argv", "tui_environment", "tui_argv"},
        "commands",
    )
    cwd = _string(commands["cwd"], "commands.cwd")
    if not Path(cwd).is_absolute():
        raise PrimeReportError("commands.cwd must be absolute")
    _environment(commands["watcher_environment"], "commands.watcher_environment")
    _environment(commands["tui_environment"], "commands.tui_environment")
    for field in ("watcher_argv", "tui_argv"):
        argv = _string_array(commands[field], f"commands.{field}", unique=False)
        if not argv:
            raise PrimeReportError(f"commands.{field} must not be empty")

    limitations = _string_array(report["limitations"], "limitations")
    if not limitations:
        raise PrimeReportError("limitations must not be empty")
    supplied = report["content_sha256"]
    if not isinstance(supplied, str) or _SHA256.fullmatch(supplied) is None:
        raise PrimeReportError("content_sha256 must be lowercase SHA-256")
    expected = prime_report_sha256(report)
    if supplied != expected:
        raise PrimeReportError(f"content_sha256 does not match: expected {expected}")
    return report


def _run_git_bytes(cwd: Path, *args: str, required: bool = True) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(cwd), *args],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if required:
            raise PrimeError(f"git inspection failed: {type(exc).__name__}") from exc
        return None
    if completed.returncode != 0:
        if not required:
            return None
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        reason = detail[0] if detail else f"exit {completed.returncode}"
        raise PrimeError(f"git {' '.join(args)} failed: {reason}")
    return completed.stdout


def _run_git_text(cwd: Path, *args: str, required: bool = True) -> str | None:
    value = _run_git_bytes(cwd, *args, required=required)
    return value.decode("utf-8", errors="strict").strip() if value is not None else None


def _digest_frame(digest: Any, label: bytes, value: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _untracked_identity(worktree: Path, names: bytes, digest: Any) -> None:
    for raw_name in sorted(part for part in names.split(b"\0") if part):
        relative = Path(os.fsdecode(raw_name))
        target = worktree / relative
        try:
            metadata = target.lstat()
        except OSError as exc:
            raise PrimeError(
                f"untracked path changed during inspection: {os.fsdecode(raw_name)!r}"
            ) from exc
        mode = str(stat.S_IFMT(metadata.st_mode) | stat.S_IMODE(metadata.st_mode)).encode()
        if stat.S_ISREG(metadata.st_mode):
            content = hashlib.sha256()
            try:
                with target.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        content.update(block)
            except OSError as exc:
                raise PrimeError(f"cannot read untracked path: {target}") from exc
            identity = b"file\0" + mode + b"\0" + content.digest()
        elif stat.S_ISLNK(metadata.st_mode):
            identity = b"symlink\0" + mode + b"\0" + os.fsencode(os.readlink(target))
        else:
            identity = b"other\0" + mode
        _digest_frame(digest, raw_name, identity)


def inspect_git(subject: Path | str) -> GitIdentity:
    """Resolve and identify the Git worktree containing an explicit subject path."""
    requested = Path(subject).expanduser().resolve()
    if not requested.is_dir():
        raise PrimeError(f"subject must be an existing directory: {requested}")

    toplevel_raw = _run_git_text(requested, "rev-parse", "--show-toplevel")
    if not toplevel_raw:
        raise PrimeError(f"subject is not inside a Git worktree: {requested}")
    worktree = Path(toplevel_raw).resolve()
    common_raw = _run_git_text(requested, "rev-parse", "--path-format=absolute", "--git-common-dir")
    head = _run_git_text(requested, "rev-parse", "--verify", "HEAD")
    if not common_raw or not head:
        raise PrimeError(f"subject has no complete Git identity: {requested}")
    branch = _run_git_text(requested, "symbolic-ref", "--quiet", "--short", "HEAD", required=False)

    status_bytes = _run_git_bytes(
        requested,
        "-c",
        "status.renames=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    diff_bytes = _run_git_bytes(
        requested,
        "-c",
        "diff.renames=false",
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "HEAD",
        "--",
    )
    untracked = _run_git_bytes(requested, "ls-files", "--others", "--exclude-standard", "-z")
    assert status_bytes is not None and diff_bytes is not None and untracked is not None
    dirty_digest = hashlib.sha256(b"bearhug-prime-dirty-v1\0")
    _digest_frame(dirty_digest, b"status", status_bytes)
    _digest_frame(dirty_digest, b"tracked-diff", diff_bytes)
    _untracked_identity(worktree, untracked, dirty_digest)

    return GitIdentity(
        requested_path=str(requested),
        toplevel=str(worktree),
        common_dir=str(Path(common_raw).resolve()),
        worktree=str(worktree),
        branch=branch or None,
        detached=branch is None,
        head=head,
        clean=not status_bytes,
        dirty_entries=status_bytes.count(b"\0"),
        dirty_sha256=dirty_digest.hexdigest(),
    )


def _latest_snapshot(snapshots_dir: Path) -> Path | None:
    candidates = sorted(
        path for path in snapshots_dir.glob("*") if (path / "manifest.json").is_file()
    )
    return candidates[-1].resolve() if candidates else None


def inspect_latest_snapshot(
    worktree: Path,
    *,
    snapshots_dir: Path = SNAPSHOTS_DIR,
) -> SnapshotObservation:
    latest = _latest_snapshot(Path(snapshots_dir))
    if latest is None:
        return SnapshotObservation(
            status="missing",
            path=None,
            snapshot_id=None,
            moved=None,
            head_moved=None,
            reason=f"no snapshot manifest under {Path(snapshots_dir).resolve()}",
        )
    try:
        drift = compute_drift(latest, barracuda_root=worktree, claude_home=paths.CLAUDE_HOME)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return SnapshotObservation(
            status="error",
            path=str(latest),
            snapshot_id=None,
            moved=None,
            head_moved=None,
            reason=f"snapshot drift could not be inspected: {type(exc).__name__}: {exc}",
        )
    return SnapshotObservation(
        status="drift" if drift.moved else "no-drift",
        path=str(latest),
        snapshot_id=drift.snapshot,
        moved=drift.moved,
        head_moved=drift.head_moved,
        changed=tuple(drift.changed),
        added=tuple(drift.added),
        removed=tuple(drift.removed),
    )


def _first_line(value: str) -> str | None:
    return next((line.strip() for line in value.splitlines() if line.strip()), None)


def probe_binary(name: str, version_args: tuple[str, ...], purpose: str) -> BinaryObservation:
    executable = shutil.which(name)
    if executable is None:
        return BinaryObservation(name, purpose, False, None, None, f"{name} is not on PATH")
    probe_environment = None
    if name == "pi":
        # Pi initializes its settings directory even for ``--version``. Point that lookup at a
        # non-directory so an observational probe cannot create ~/.pi state.
        probe_environment = os.environ.copy()
        probe_environment.update(
            {
                "PI_CODING_AGENT_DIR": os.devnull,
                "PI_OFFLINE": "1",
                "PI_TELEMETRY": "0",
            }
        )
    try:
        completed = subprocess.run(
            [executable, *version_args],
            capture_output=True,
            text=True,
            # A probed binary's own non-UTF-8 output must never raise here either
            # -- substitute the standard replacement character for whatever cannot be decoded.
            errors="replace",
            check=False,
            env=probe_environment,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return BinaryObservation(
            name,
            purpose,
            True,
            executable,
            None,
            f"version probe timed out after {PROBE_TIMEOUT_SECONDS}s",
        )
    except (OSError, ValueError, UnicodeError) as exc:
        # ValueError/UnicodeError kept as defense in depth alongside errors="replace" above.
        return BinaryObservation(
            name, purpose, True, executable, None, f"version probe failed: {type(exc).__name__}"
        )
    version = _first_line(completed.stdout) or _first_line(completed.stderr)
    reason = None
    if completed.returncode != 0:
        reason = f"version probe exited {completed.returncode}"
    elif version is None:
        reason = "version probe returned no text"
    return BinaryObservation(name, purpose, True, executable, version, reason)


def _unreported_watcher(reason: str, *, status: str = "unreported") -> WatcherObservation:
    return WatcherObservation(status, None, None, None, None, reason)


def inspect_cockpit(
    cockpit_path: Path,
    *,
    now: datetime,
    fresh_within_seconds: int = COCKPIT_FRESH_SECONDS,
    expected_subject: Path | str | None = None,
    expected_watcher_id: str | None = None,
) -> CockpitObservation:
    path = Path(cockpit_path).expanduser().resolve()
    if not path.is_file():
        return CockpitObservation(
            str(path),
            "missing",
            None,
            None,
            False,
            fresh_within_seconds,
            "file is absent",
            _unreported_watcher("cockpit file is absent"),
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        generated_text = raw["generated_at"]
        if not isinstance(generated_text, str):
            raise ValueError("generated_at is not a string")
        generated = datetime.fromisoformat(generated_text.replace("Z", "+00:00"))
        if generated.tzinfo is None:
            raise ValueError("generated_at has no timezone")
        age = (now.astimezone(UTC) - generated.astimezone(UTC)).total_seconds()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return CockpitObservation(
            str(path),
            "malformed",
            None,
            None,
            False,
            fresh_within_seconds,
            f"cannot read generated_at: {type(exc).__name__}: {exc}",
            _unreported_watcher(
                "cockpit is malformed; watcher heartbeat is unavailable", status="malformed"
            ),
        )
    if age < 0:
        status, fresh, reason = "future", False, "generated_at is in the future"
    elif age <= fresh_within_seconds:
        status, fresh, reason = "fresh", True, None
    else:
        status, fresh, reason = "stale", False, f"older than {fresh_within_seconds}s"
    raw_watcher = raw.get("watcher")
    if raw_watcher is None:
        watcher = _unreported_watcher("no watcher heartbeat in cockpit artifact")
    else:
        watcher_status, watcher_id, watcher_age, watcher_reason = classify_watcher(
            raw_watcher,
            now=now,
            expected_subject=expected_subject,
            expected_watcher_id=expected_watcher_id,
        )
        watcher_heartbeat = (
            raw_watcher.get("heartbeat_at") if isinstance(raw_watcher, dict) else None
        )
        watcher_stale_after = (
            raw_watcher.get("stale_after_seconds") if isinstance(raw_watcher, dict) else None
        )
        watcher = WatcherObservation(
            watcher_status,
            watcher_id,
            watcher_heartbeat if isinstance(watcher_heartbeat, str) else None,
            watcher_age,
            watcher_stale_after if isinstance(watcher_stale_after, int) else None,
            watcher_reason,
        )
    return CockpitObservation(
        str(path), status, generated_text, age, fresh, fresh_within_seconds, reason, watcher
    )


def _commands(
    git: GitIdentity,
    snapshot: SnapshotObservation,
    cockpit: CockpitObservation,
    *,
    watcher_interval: float,
) -> PrimeCommands:
    if watcher_interval < 5:
        raise PrimeError("watcher interval must be at least 5 seconds")
    interval = (
        str(int(watcher_interval)) if watcher_interval.is_integer() else str(watcher_interval)
    )
    watcher = ["uv", "run", "bearhug", "cockpit"]
    if snapshot.path is not None:
        watcher.extend(("--snapshot", snapshot.path))
    watcher.extend(("--watch", interval))
    tui = ["uv", "run", "bearhug", "tui"]
    if snapshot.path is not None:
        tui.extend(("--snapshot", snapshot.path))
    tui.extend(("--refresh", interval, git.worktree))
    return PrimeCommands(
        cwd=str(REPO_ROOT),
        watcher_environment=(
            ("BEARHUG_PROJECT_ROOT", git.worktree),
            # Compatibility for older Bear Hug callers; new commands use BEARHUG_PROJECT_ROOT.
            ("BEARHUG_BARRACUDA_ROOT", git.worktree),
        ),
        watcher_argv=tuple(watcher),
        tui_environment=(),
        tui_argv=tuple(tui),
    )


def build_prime_report(
    subject: Path | str,
    *,
    snapshots_dir: Path = SNAPSHOTS_DIR,
    cockpit_path: Path = REPORTS_DIR / "cockpit.json",
    now: datetime | None = None,
    watcher_interval: float = 30.0,
    expected_watcher_id: str | None = None,
    binary_specs: tuple[tuple[str, tuple[str, ...], str], ...] = SUPPORTED_BINARIES,
) -> PrimeReport:
    """Read one subject and return a report; no report, subject, or provider state is written."""
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    git = inspect_git(subject)
    snapshot = inspect_latest_snapshot(Path(git.worktree), snapshots_dir=snapshots_dir)
    cockpit = inspect_cockpit(
        Path(cockpit_path),
        now=observed,
        expected_subject=git.worktree,
        expected_watcher_id=expected_watcher_id,
    )
    binaries = tuple(probe_binary(*spec) for spec in binary_specs)
    commands = _commands(git, snapshot, cockpit, watcher_interval=float(watcher_interval))
    report = PrimeReport(
        schema_version="1",
        record_kind="prime_report",
        observed_at=observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
        git=git,
        snapshot=snapshot,
        binaries=binaries,
        cockpit=cockpit,
        commands=commands,
        content_sha256="",
    )
    report = replace(report, content_sha256=prime_report_sha256(report))
    validate_prime_report(report.to_dict())
    return report
