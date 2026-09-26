"""Drive prepared native campaigns from the normal Claude/Codex project terminal.

The project terminal bridge owns provider hook validation and rendering.  This module owns only
the small edge between those hooks and one exact prepared campaign locator: it records a native
session, prepares a fresh request when the previous request is terminal, and starts the existing
campaign worker in a separate process.  It never interprets a normal prompt as operator approval
and never grants a PreToolUse permission override.  While a campaign is active it fails closed for
mutation-capable tools and leaves read-only tool permission to the provider.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bearhug.host_git import describe_dirty_status, run_git
from bearhug.project_terminal import (
    ProjectTerminalDecision,
    ProjectTerminalError,
    ProjectTerminalProfile,
    _request_from_raw,
    enroll_project,
    opt_out_project,
)

SCHEMA_VERSION = "1"
CONFIG_KIND = "bearhug_terminal_driver_config"
SESSION_KIND = "bearhug_terminal_session"
MAX_CONFIG_BYTES = 64 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_HOOK_BYTES = 16 * 1024 * 1024
CONTROL_PREFIXES = ("bearhug", "/bearhug")
CONTROL_COMMANDS = frozenset({"approve", "deny", "defer", "stop", "continue", "repair"})
# These are only terminal for native intake when the campaign has also released all custody.
# ``failed`` and ``stopped`` are deliberately absent: either state can still own an active
# provider fence or mutation lease after a crash or an uncertain transport outcome.
TERMINAL_STATUSES = frozenset({"completed", "phase_validated", "superseded"})
READ_ONLY_TOOLS = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "LS",
        "NotebookRead",
        "WebFetch",
        "WebSearch",
    }
)


class TerminalDriverError(RuntimeError):
    """The native terminal could not preserve its session or campaign boundary."""


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TerminalDriverError(f"value is not canonical JSON: {exc}") from exc


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise TerminalDriverError(f"JSON repeats key {key!r}")
        value[key] = item
    return value


def _load_json(path: Path, *, maximum: int) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TerminalDriverError(f"cannot read {path}: {exc}") from exc
    if len(raw) > maximum:
        raise TerminalDriverError(f"{path} exceeds the byte limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalDriverError(f"{path} is not one UTF-8 JSON object: {exc}") from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise TerminalDriverError(f"{path} is not canonical JSON")
    return value


def _absolute_dir(
    value: Path | str, *, label: str, create: bool = False, private: bool = False
) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise TerminalDriverError(f"{label} must be an exact absolute path")
    if path.is_symlink():
        raise TerminalDriverError(f"{label} may not be a symlink")
    try:
        canonical = path.resolve(strict=False)
    except OSError as exc:
        raise TerminalDriverError(f"cannot resolve {label}: {path}") from exc
    if canonical != path:
        raise TerminalDriverError(f"{label} must be an exact physical path: {path}")
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise TerminalDriverError(f"cannot create {label}: {path}: {exc}") from exc
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise TerminalDriverError(f"{label} is unavailable: {path}") from exc
    if resolved != path or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise TerminalDriverError(f"{label} must be user-owned and physical: {path}")
    if private and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise TerminalDriverError(f"{label} must be owner-only and physical: {path}")
    return path


def _absolute_file(value: Path | str, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise TerminalDriverError(f"{label} must be an exact absolute path")
    if path.is_symlink():
        raise TerminalDriverError(f"{label} may not be a symlink")
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TerminalDriverError(f"{label} is unavailable: {path}") from exc
    if resolved != path or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise TerminalDriverError(f"{label} must be a user-owned physical file: {path}")
    return path


def _assert_inside(path: Path, root: Path, *, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TerminalDriverError(f"{label} must be inside {root}") from exc


def _exact_locator(value: Path | str, root: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or path.resolve(strict=False) != path:
        raise TerminalDriverError("prepared locator must be an exact absolute path")
    _assert_inside(path, root, label="prepared locator")
    if path.is_symlink() or not path.is_file():
        raise TerminalDriverError("prepared locator is unavailable or symlinked")
    return path


def _write_atomic(path: Path, raw: bytes, *, mode: int, private_parent: bool = True) -> None:
    if len(raw) > MAX_STATE_BYTES:
        raise TerminalDriverError(f"{path} exceeds the state byte limit")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise TerminalDriverError(f"refusing symlinked state path: {path}")
    metadata = path.parent.stat(follow_symlinks=False)
    if metadata.st_uid != os.geteuid() or private_parent and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise TerminalDriverError(f"state directory must be owner-only: {path.parent}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise TerminalDriverError(f"cannot publish {path}: {exc}") from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


@dataclass(frozen=True, slots=True)
class TerminalDriverConfig:
    """The explicit project and authority inputs used by the native terminal edge."""

    subject: Path
    provider: str
    template_path: Path
    policy_path: Path
    execution_path: Path
    state_root: Path

    def __post_init__(self) -> None:
        subject = _absolute_dir(self.subject, label="subject")
        state_root = _absolute_dir(self.state_root, label="state root", private=True)
        if subject == state_root or subject in state_root.parents:
            raise TerminalDriverError(
                "state root may not be inside the subject: "
                f"{state_root} is inside {subject}. If this command takes --state-root, "
                "pass a path outside the project; if it does not (state root came from "
                "Bear Hug's own runs directory by default), clone Bear Hug as a sibling "
                "of the project, or set BEARHUG_ARTIFACT_ROOT to a directory outside it."
            )
        if self.provider not in {"claude", "codex"}:
            raise TerminalDriverError(f"unsupported provider: {self.provider!r}")
        for value, label in (
            (self.template_path, "template"),
            (self.policy_path, "policy"),
            (self.execution_path, "execution config"),
        ):
            _absolute_file(value, label=label)

    @property
    def subject_path(self) -> Path:
        return _absolute_dir(self.subject, label="subject")

    @property
    def private_root(self) -> Path:
        return _absolute_dir(self.state_root, label="state root", private=True)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "record_kind": CONFIG_KIND,
            "subject": self.subject_path.as_posix(),
            "provider": self.provider,
            "template_path": _absolute_file(self.template_path, label="template").as_posix(),
            "policy_path": _absolute_file(self.policy_path, label="policy").as_posix(),
            "execution_path": _absolute_file(
                self.execution_path, label="execution config"
            ).as_posix(),
            "state_root": self.private_root.as_posix(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TerminalDriverConfig:
        expected = {
            "schema_version",
            "record_kind",
            "subject",
            "provider",
            "template_path",
            "policy_path",
            "execution_path",
            "state_root",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise TerminalDriverError("terminal driver config has missing or unknown fields")
        if value["schema_version"] != SCHEMA_VERSION or value["record_kind"] != CONFIG_KIND:
            raise TerminalDriverError("unsupported terminal driver config identity")
        return cls(
            subject=Path(value["subject"]),
            provider=value["provider"],
            template_path=Path(value["template_path"]),
            policy_path=Path(value["policy_path"]),
            execution_path=Path(value["execution_path"]),
            state_root=Path(value["state_root"]),
        )


def _config_path(subject: Path, supplied: Path | str | None) -> Path:
    path = (
        Path(supplied).expanduser()
        if supplied is not None
        else subject / ".bearhug" / "terminal-driver.json"
    )
    if not path.is_absolute():
        path = subject / path
    if path.is_symlink() or path.resolve(strict=False) != path:
        raise TerminalDriverError("driver config path must be an exact physical path")
    _assert_inside(path, subject, label="driver config")
    return path


def load_config(path: Path | str) -> TerminalDriverConfig:
    return TerminalDriverConfig.from_mapping(_load_json(Path(path), maximum=MAX_CONFIG_BYTES))


def _session_key(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id or len(session_id.encode()) > 1024:
        raise TerminalDriverError("session_id is missing or oversized")
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _session_path(config: TerminalDriverConfig, session_id: str) -> Path:
    return config.private_root / "sessions" / f"{_session_key(session_id)}.json"


def _worker_root(config: TerminalDriverConfig, session_id: str) -> Path:
    return config.private_root / "workers" / _session_key(session_id)


@contextmanager
def _intake_lock(config: TerminalDriverConfig) -> Iterator[None]:
    root = config.private_root
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / ".intake.lock"
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _save_session(config: TerminalDriverConfig, session: dict[str, Any]) -> None:
    _write_atomic(_session_path(config, session["session_id"]), _canonical(session), mode=0o600)


def _load_session(config: TerminalDriverConfig, session_id: str) -> dict[str, Any] | None:
    path = _session_path(config, session_id)
    if not path.exists():
        return None
    value = _load_json(path, maximum=MAX_STATE_BYTES)
    if value.get("schema_version") != SCHEMA_VERSION or value.get("record_kind") != SESSION_KIND:
        raise TerminalDriverError("terminal session record identity is invalid")
    if value.get("session_id") != session_id or value.get("provider") != config.provider:
        raise TerminalDriverError("terminal session belongs to another provider/session")
    if value.get("subject") != config.subject_path.as_posix():
        raise TerminalDriverError("terminal session subject changed")
    return value


def _new_session(config: TerminalDriverConfig, session_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": SESSION_KIND,
        "session_id": session_id,
        "provider": config.provider,
        "subject": config.subject_path.as_posix(),
        "locator": None,
        "request_sha256": None,
        "status": "idle",
        "reason": "no request prepared",
        "hil_request_id": None,
        "worker": None,
        "history": [],
        "last_event_sha256": None,
        "last_event_action": None,
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
    }


def _pid_alive(value: Any) -> bool:
    if type(value) is not int or value <= 0:
        return False
    try:
        os.kill(value, 0)
    # A pid that does not fit `pid_t` (signed 32-bit on this platform -- not a
    # C `long`, which is 64-bit here; measured: os.kill(2**31 - 1, 0) raises ProcessLookupError,
    # os.kill(2**31, 0) raises OverflowError) makes `os.kill` raise `OverflowError`, which is an
    # `ArithmeticError`, not an `OSError` -- uncaught here, it would crash `status()` on the one
    # command the runbook tells the operator to run when campaign state is suspect. A corrupted
    # or hand-edited session record is the likely source. Same answer as any other signal
    # failure: not alive.
    except (OSError, ProcessLookupError, OverflowError):
        return False
    return True


def _worker_active(session: Mapping[str, Any]) -> bool:
    worker = session.get("worker")
    return (
        isinstance(worker, Mapping)
        and worker.get("status") in {"started", "running"}
        and _pid_alive(worker.get("pid"))
    )


def _git_clean(subject: Path) -> None:
    # Routed through the shared, hardened `run_git` (rather than this module's own environment)
    # so this verdict and `capture_launch_repository`'s agree about the same subject: one
    # environment, honouring the same user global-ignore authority, for every caller.
    try:
        result = run_git(subject, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TerminalDriverError(f"cannot inspect the subject Git state: {exc}") from exc
    if result.returncode != 0:
        raise TerminalDriverError("subject must be a Git worktree before native execution")
    if result.stdout.strip():
        raise TerminalDriverError(
            "subject has uncommitted or untracked changes; review and commit enrollment/config "
            "changes before native execution: " + describe_dirty_status(subject, result.stdout)
        )


def _request(config: TerminalDriverConfig, raw: bytes):
    profile = ProjectTerminalProfile(
        config.subject_path.as_posix(), config.provider, ("bearhug-terminal-driver",)
    )
    try:
        return _request_from_raw(raw, profile)
    except ProjectTerminalError:
        raise
    except Exception as exc:
        raise TerminalDriverError(f"native hook input is invalid: {exc}") from exc


def _iso(timestamp: float) -> str:
    # `_instant`/`_read_record` bound a durable lease timestamp to any nonnegative finite
    # float, so a value beyond `datetime`'s year-9999 ceiling is a storable, if absurd, row --
    # `fromtimestamp` raises `ValueError` for it (and `OSError`/`OverflowError` are the same
    # failure on platforms whose C library bounds it differently). This runs inside `status`'s
    # own lease-listing loop, so an unhandled raise here would take out `status`, `retire` and
    # `recover` for every campaign, not just the one row with the absurd value. Fail back to
    # the raw number as text rather than take those down over one unreadable timestamp.
    try:
        return dt.datetime.fromtimestamp(timestamp, dt.UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OSError, OverflowError):
        return repr(timestamp)


def _custody_held_after_stop_guidance(active_lease_ids: Sequence[Mapping[str, Any]]) -> str:
    """Guidance for a durably failed, already-stopped campaign whose lease is still held.

    ``recover --recovery-outcome failed`` is never named here: `_recover_current`'s
    short-circuit answers `recovery_not_needed` for exactly this combination and launches no
    worker. `control stop` is re-runnable and is named first because it is the command that
    actually releases this custody. `recover --lease-id` is the documented second route to the
    same release. `--resolve-orphan` is named only as the last resort, together with the
    lease's own expiry, since it can never release an unexpired lease before that time.
    """

    if not active_lease_ids:
        return "Run scripts/bin/bearhug-campaign control stop; it now releases this custody."
    lease_id = active_lease_ids[0]["lease_id"]
    expires_at_iso = active_lease_ids[0]["expires_at_iso"]
    return (
        "Run scripts/bin/bearhug-campaign control stop; it now releases the still-held lease "
        f"{lease_id}. If custody is still held after that, run scripts/bin/bearhug-campaign "
        f"recover --lease-id {lease_id}. Only if custody is still held after both: wait until "
        f"{expires_at_iso}, then run scripts/bin/bearhug-campaign recover --resolve-orphan "
        f"--lease-id {lease_id} --confirm <reason>; it can only release the lease after that "
        "time."
    )


def _status_for_locator(locator: str, *, recovery: bool = True) -> dict[str, Any]:
    """Read sealed custody even after plan edits; execution checks live authority separately."""
    from bearhug.campaign.capsule_campaign import CapsuleCampaign, read_capsule_evidence
    from bearhug.campaign.prepared import load_prepared

    prepared = load_prepared(locator, recovery=True) if recovery else load_prepared(locator)
    runtime_paths = [prepared.root / "capsules", prepared.root / "run-states"]
    if all(not path.exists() and not path.is_symlink() for path in runtime_paths):
        return {
            "status": "prepared",
            "reason": "prepared locator has not started",
            "stopped": False,
            "unresolved_spend": False,
            "active_leases": 0,
            "custody_active": False,
        }
    campaign = CapsuleCampaign(prepared, read_only=True)
    if campaign.state is None:
        return {
            "status": "prepared",
            "reason": "prepared locator has not started",
            "stopped": False,
            "unresolved_spend": False,
            "active_leases": 0,
            "custody_active": False,
        }
    report = campaign.report()
    current = {
        "status": report["status"],
        "reason": report["reason"],
    }
    state = campaign.state
    launches = state.get("launches") or {}
    if not isinstance(launches, Mapping):
        raise TerminalDriverError("campaign custody launches are malformed")
    unresolved_spend = False
    failed_episode = False
    # `recover` wants an --episode-id, and the only copy used to be several directories
    # down in the capsule state. A refusal that asks for recovery can name it.
    active_episode: str | None = None
    # For the first unresolved episode found below: every provider process recorded for it,
    # each with its own observed `alive`, plus one sentence of guidance -- the exact text
    # `CapsuleRuntime._recover_episode` would refuse `hil_required` recovery with, or a
    # confirmation once nothing is left alive. `None` names have "not applicable" and "not
    # observed yet" -- different meanings that must not collapse into one another.
    interrupted_provider_processes: dict[str, Any] | None = None
    # Inspect every capsule: a stale scheduler cursor must not hide a runtime fence in a
    # capsule whose id is no longer the top-level active cursor.
    for capsule in campaign.plan["capsules"]:
        evidence = read_capsule_evidence(campaign.capsule_root, capsule["capsule_id"])
        capsule_state = (evidence or {}).get("state") or {}
        episode = capsule_state.get("active_episode")
        if isinstance(episode, Mapping) and isinstance(episode.get("episode_id"), str):
            if active_episode is None:
                from bearhug.campaign.capsule_runtime import (
                    CapsuleRuntimeError,
                    _load_provider_process_records,
                    _provider_process_liveness,
                    _recovery_provider_liveness_refusal,
                )

                episode_id = episode["episode_id"]
                capsule_root = evidence["root"]
                try:
                    refusal = _recovery_provider_liveness_refusal(capsule_root, episode_id)
                    interrupted_provider_processes = {
                        "episode_id": episode_id,
                        "processes": _provider_process_liveness(capsule_root, episode_id),
                        "ready_for_hil_required": refusal is None,
                        "guidance": refusal
                        or (
                            "The interrupted attempt's provider process has exited; "
                            "hil_required recovery may now proceed."
                        ),
                    }
                except CapsuleRuntimeError:
                    # A leftover temp file or a hand-truncated record must not take this whole
                    # read down with it (that failure mode already fails closed at the
                    # recovery-affecting callers above, in `strict` mode); a display-only read
                    # instead reports what it could not read and carries on. `status()`'s own
                    # catch-all a screen above this call would otherwise collapse the entire
                    # campaign view into "blocked" over one disposable file -- the same
                    # reasoning the `_iso` fallback applies to one absurd lease timestamp.
                    _, unreadable = _load_provider_process_records(
                        capsule_root, episode_id, strict=False
                    )
                    count = len(unreadable)
                    names = ", ".join(sorted(unreadable)) or "no name recovered"
                    interrupted_provider_processes = {
                        "episode_id": episode_id,
                        "processes": [],
                        "ready_for_hil_required": False,
                        "guidance": (
                            f"{count} provider process record"
                            f"{'s are' if count != 1 else ' is'} unreadable ({names}); "
                            "hil_required recovery cannot confirm the interrupted attempt has "
                            "exited. Close the attempt for good with --recovery-outcome failed "
                            "and start a fresh campaign."
                        ),
                    }
            active_episode = active_episode or episode["episode_id"]
        if episode or capsule_state.get("active_review"):
            unresolved_spend = True
        if (
            capsule_state.get("state") == "failed"
            and capsule_state.get("last_failed_stage") != "review"
        ):
            failed_episode = True
    lease_store = campaign.leases()
    active_leases = 0
    active_lease_ids: list[dict[str, Any]] = []
    for launch in launches.values():
        if not isinstance(launch, Mapping) or not isinstance(launch.get("lease_id"), str):
            raise TerminalDriverError("campaign custody lease record is malformed")
        lease = lease_store.get(launch["lease_id"])
        if lease.state in {"active", "orphaned"}:
            active_leases += 1
            active_lease_ids.append(
                {
                    "lease_id": lease.identity.lease_id,
                    "expires_at_iso": _iso(lease.expires_at),
                }
            )
    custody_active = bool(state.get("launching_capsule")) or unresolved_spend or bool(
        active_leases
    )
    current.update(
        stopped=state.get("stopped") is True,
        unresolved_spend=unresolved_spend,
        active_leases=active_leases,
        active_lease_ids=active_lease_ids,
        custody_active=custody_active,
        failed_episode=failed_episode,
        interrupted_provider_processes=interrupted_provider_processes,
        active_episode=active_episode,
    )
    if failed_episode and not unresolved_spend:
        if state.get("accepted"):
            guidance = (
                "Preserve accepted tasks; resolve the failed capsule under existing authority."
            )
        elif state.get("stopped") and (custody_active or active_leases):
            # `stopped` alone does not mean released: a provider that outlived its own
            # SIGKILLed controller can keep the lease held under this exact combination.
            # Naming plain `onboard` here would send the operator into the same custody
            # refusal `_settled_attached_session` raises for it; name the routes that
            # actually release this lease instead.
            guidance = _custody_held_after_stop_guidance(active_lease_ids)
        elif state.get("stopped"):
            guidance = (
                "Run scripts/bin/bearhug-campaign onboard --provider "
                "<claude|codex> --model <supported-model> --effort <effort>, "
                "approve the new proposal, then start ready work in a new session."
            )
        else:
            guidance = (
                "Run scripts/bin/bearhug-campaign control stop, wait for stopped status, "
                "then onboard with corrected model/effort and approve a fresh campaign."
            )
        current["reason"] = (
            str(report["reason"]) + " "
            "The provider attempt is durably failed and its episode boundary is resolved. "
            "Recovery is not needed; continue cannot retry this attempt. " + guidance
        )
    capsule_id = campaign.state.get("active_capsule_id")
    if capsule_id:
        with suppress(Exception):
            evidence = read_capsule_evidence(campaign.capsule_root, capsule_id)
            request = evidence.get("hil_request") if evidence else None
            if isinstance(request, Mapping):
                current["hil_request_id"] = request.get("request_id")
    return current


def _session_active(session: Mapping[str, Any]) -> bool:
    """Return whether the session still owns campaign custody, conservatively."""
    locator = session.get("locator")
    if not isinstance(locator, str) or not locator:
        return False
    custody_active = session.get("custody_active")
    if type(custody_active) is bool:
        if custody_active or _worker_active(session):
            return True
        # A prepared/paused run still owns this request before it acquires its next lease.
        # An explicit stop can finish with status=blocked after HIL denial, so use its flag.
        return not session.get("stopped") and session.get("status") not in TERMINAL_STATUSES
    # Older session records do not contain the custody projection.  Preserve their lock until a
    # fresh campaign read proves that the run is free, including when its status is failed/blocked.
    return session.get("status") not in TERMINAL_STATUSES


def _refresh_session(config: TerminalDriverConfig, session: dict[str, Any]) -> dict[str, Any]:
    locator = session.get("locator")
    if not isinstance(locator, str) or not locator:
        return session
    try:
        summary = _status_for_locator(locator)
    except Exception as exc:
        summary = {
            "status": "blocked",
            "reason": f"cannot read prepared run: {exc}",
            # An unreadable custody record may be an interrupted provider operation.  Keep
            # mutation access denied until an explicit recovery/stop proves it is released.
            "custody_active": True,
        }
    session["status"] = summary["status"]
    session["reason"] = summary["reason"]
    session["hil_request_id"] = summary.get("hil_request_id")
    for key in (
        "stopped",
        "unresolved_spend",
        "active_leases",
        "active_lease_ids",
        "custody_active",
        "failed_episode",
        "active_episode",
        "interrupted_provider_processes",
    ):
        session.pop(key, None)
        if key in summary:
            session[key] = summary[key]
    return session


def _context(session: Mapping[str, Any]) -> str:
    fields = {
        "session_id": session.get("session_id"),
        "locator": session.get("locator"),
        "status": session.get("status"),
        "reason": session.get("reason"),
        "hil_request_id": session.get("hil_request_id"),
        "stopped": session.get("stopped"),
        "unresolved_spend": session.get("unresolved_spend"),
        "active_leases": session.get("active_leases"),
        "custody_active": session.get("custody_active"),
        "worker": session.get("worker"),
    }
    return "Bear Hug native terminal state:\n" + json.dumps(
        fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _control(prompt: str) -> str | None:
    words = prompt.strip().split()
    if len(words) != 2 or words[0] not in CONTROL_PREFIXES:
        return None
    command = words[1].lower()
    return command if command in CONTROL_COMMANDS else None


class TerminalDriver:
    """Translate validated native hook events into one exact prepared campaign."""

    def __init__(
        self,
        config: TerminalDriverConfig,
        *,
        worker_launcher: Callable[..., Mapping[str, Any]] | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.config = config
        self.worker_launcher = worker_launcher or self._launch_worker
        self.config_path = config_path

    def dispatch(self, raw: bytes) -> ProjectTerminalDecision:
        if not raw or len(raw) > MAX_HOOK_BYTES:
            raise TerminalDriverError("native hook input is empty or oversized")
        request = _request(self.config, raw)
        if request.event_name == "SessionStart":
            return self._session_start(request.session_id)
        if request.event_name == "PreToolUse":
            return self._pre_tool(request.session_id, request.parsed["tool_name"])
        return self._user_prompt(request.session_id, raw, request.parsed["prompt"])

    def _pre_tool(self, session_id: str, tool_name: str) -> ProjectTerminalDecision:
        """Keep a running campaign's observer terminal away from mutation-capable tools."""
        with _intake_lock(self.config):
            session = _load_session(self.config, session_id)
            if session is None:
                return ProjectTerminalDecision("allow")
            observed = dict(session)
            _refresh_session(self.config, observed)
            active = _session_active(observed)
        if active and tool_name not in READ_ONLY_TOOLS:
            return ProjectTerminalDecision(
                "deny",
                reason=(
                    "the active Bear Hug campaign owns this session; native terminal access is "
                    "limited to read-only tools"
                ),
            )
        # An allow decision intentionally omits permissionDecision so the provider's native
        # permission checks remain authoritative.
        return ProjectTerminalDecision("allow")

    def _session_start(self, session_id: str) -> ProjectTerminalDecision:
        with _intake_lock(self.config):
            session = _load_session(self.config, session_id)
            if session is None:
                session = _new_session(self.config, session_id)
                _save_session(self.config, session)
            else:
                _refresh_session(self.config, session)
                _save_session(self.config, session)
            try:
                _git_clean(self.config.subject_path)
            except TerminalDriverError as exc:
                session["reason"] = str(exc)
                _save_session(self.config, session)
        return ProjectTerminalDecision("allow", additional_context=_context(session))

    def _user_prompt(self, session_id: str, raw: bytes, prompt: str) -> ProjectTerminalDecision:
        digest = hashlib.sha256(raw).hexdigest()
        command = _control(prompt)
        with _intake_lock(self.config):
            session = _load_session(self.config, session_id)
            if session is None:
                session = _new_session(self.config, session_id)
            _refresh_session(self.config, session)
            active = _session_active(session)

            if active:
                session["last_event_sha256"] = digest
                if command is None:
                    session["last_event_action"] = "steering_rejected_active_run"
                    _save_session(self.config, session)
                    return ProjectTerminalDecision(
                        "block",
                        reason=(
                            "an active Bear Hug campaign owns this session; use an explicit "
                            "bearhug approve, deny, defer, stop, continue, or repair control"
                        ),
                        additional_context=_context(session),
                    )
                return self._handle_control(session, command, digest)

            if command is not None:
                session["last_event_sha256"] = digest
                session["last_event_action"] = f"control_{command}_without_active_run"
                _save_session(self.config, session)
                return ProjectTerminalDecision(
                    "block",
                    reason="no active Bear Hug campaign accepts that control",
                    additional_context=_context(session),
                )

            # A repeated delivery of the exact event must not reopen or spend the completed
            # run -- unless that exact request was superseded by a retirement
            # (project_campaign.py's `_retire_attached_session`), in which case resubmitting
            # it (typically after a genuine re-onboard and re-approval, which changes the
            # installed profile but not the session id or the accepted plan text this
            # request's digest is taken over) is a new request for a fresh dispatch, not a
            # repeat of the one that retirement already closed out.
            if (
                session.get("request_sha256") == digest
                and session.get("superseded_request_sha256") != digest
            ):
                session["last_event_sha256"] = digest
                session["last_event_action"] = "duplicate_ignored"
                _save_session(self.config, session)
                return ProjectTerminalDecision(
                    "block",
                    reason="this native request was already queued",
                    additional_context=_context(session),
                )
            try:
                _git_clean(self.config.subject_path)
                prepared = self._prepare(raw)
                locator = self._prepared_locator(prepared)
                if session.get("locator"):
                    session["history"].append(
                        {
                            "locator": session["locator"],
                            "request_sha256": session.get("request_sha256"),
                            "status": session.get("status"),
                        }
                    )
                for key in (
                    "stopped", "unresolved_spend", "active_leases",
                    "custody_active", "failed_episode",
                    # Consumed by the one fresh dispatch it was meant to unblock (see the
                    # duplicate check above): this line is reached only once _git_clean,
                    # _prepare and _prepared_locator have already succeeded, so a dirty-tree
                    # or prepare failure leaves the marker in place for the owner to fix the
                    # tree and resubmit without retiring again. Left uncleared, a later
                    # campaign started under the same never-changing board-request digest
                    # (a genuine re-onboard does not change it) would inherit a stale
                    # "not a duplicate" flag left over from the retirement that preceded it.
                    "superseded_request_sha256",
                ):
                    session.pop(key, None)
                session.update(
                    locator=locator,
                    request_sha256=digest,
                    status="prepared",
                    reason="request prepared; campaign worker queued",
                    hil_request_id=None,
                    last_event_sha256=digest,
                    last_event_action="prepared",
                )
                _save_session(self.config, session)
                worker = self.worker_launcher(session, operation="run")
                session["worker"] = dict(worker)
                session["status"] = "running"
                session["reason"] = "campaign worker started"
                _save_session(self.config, session)
            except Exception as exc:
                session.update(
                    last_event_sha256=digest,
                    last_event_action="prepare_failed",
                    status="blocked",
                    reason=str(exc),
                )
                # A failed compilation is retryable.  Recording its event digest as the accepted
                # request would turn a transient or corrected input into a permanent duplicate.
                _save_session(self.config, session)
                return ProjectTerminalDecision(
                    "block",
                    reason=f"Bear Hug could not prepare this request: {exc}",
                    additional_context=_context(session),
                )
        return ProjectTerminalDecision(
            "block",
            reason="request queued to the Bear Hug campaign worker",
            additional_context=_context(session),
        )

    def _prepare(self, raw: bytes):
        from bearhug.campaign.proposal import prepare_terminal_request

        return prepare_terminal_request(
            self.config.subject_path,
            self.config.template_path,
            self.config.policy_path,
            self.config.execution_path,
            raw,
            state_root=self.config.private_root,
        )

    def _prepared_locator(self, prepared: Any) -> str:
        try:
            locator = Path(prepared.record["locator"]).expanduser()
        except (AttributeError, KeyError, TypeError) as exc:
            raise TerminalDriverError("request compiler returned no exact locator") from exc
        try:
            return _exact_locator(locator, self.config.private_root).as_posix()
        except TerminalDriverError as exc:
            raise TerminalDriverError(
                f"request compiler returned an invalid locator: {exc}"
            ) from exc

    def _handle_control(
        self, session: dict[str, Any], command: str, digest: str
    ) -> ProjectTerminalDecision:
        session["last_event_action"] = f"control_{command}"
        session["last_event_sha256"] = digest
        if command == "stop":
            self._request_stop(session["locator"])
            session["reason"] = (
                "stop requested; campaign custody will be released only by the worker"
            )
            if not _worker_active(session):
                worker = self.worker_launcher(session, operation="stop")
                session["worker"] = dict(worker)
            _save_session(self.config, session)
            return ProjectTerminalDecision(
                "block",
                reason="stop requested for the active Bear Hug campaign",
                additional_context=_context(session),
            )

        if command in {"approve", "deny", "defer"}:
            question_id = session.get("hil_request_id")
            if not isinstance(question_id, str) or not question_id:
                _save_session(self.config, session)
                return ProjectTerminalDecision(
                    "block",
                    reason="no pending HIL request accepts that control",
                    additional_context=_context(session),
                )
            if _worker_active(session):
                return ProjectTerminalDecision(
                    "block",
                    reason="the campaign worker is still finishing its current operation",
                    additional_context=_context(session),
                )
            worker = self.worker_launcher(
                session,
                operation="answer",
                question_id=question_id,
                disposition=command,
            )
            session["worker"] = dict(worker)
            session["reason"] = f"HIL {command} queued for the campaign worker"
            _save_session(self.config, session)
            return ProjectTerminalDecision(
                "block",
                reason=f"HIL {command} recorded for the Bear Hug campaign",
                additional_context=_context(session),
            )

        if command in {"continue", "repair"}:
            if session.get("failed_episode") and not session.get("unresolved_spend"):
                _save_session(self.config, session)
                return ProjectTerminalDecision(
                    "block", reason=session["reason"], additional_context=_context(session)
                )
            if not _session_active(session):
                return ProjectTerminalDecision(
                    "block",
                    reason="the campaign is terminal; submit a new user request",
                    additional_context=_context(session),
                )
            if _worker_active(session):
                return ProjectTerminalDecision(
                    "block",
                    reason="the campaign worker is already running",
                    additional_context=_context(session),
                )
            worker = self.worker_launcher(
                session, operation="repair" if command == "repair" else "resume"
            )
            session["worker"] = dict(worker)
            message = (
                "implementation repair queued under unchanged authority"
                if command == "repair" else "campaign continuation queued"
            )
            session["reason"] = message
            _save_session(self.config, session)
            return ProjectTerminalDecision(
                "block", reason=message, additional_context=_context(session)
            )
        raise TerminalDriverError(f"unsupported control: {command}")

    def _request_stop(self, locator: str) -> None:
        from bearhug.campaign.capsule_campaign import CapsuleCampaign
        from bearhug.campaign.prepared import load_prepared

        campaign = CapsuleCampaign(load_prepared(locator, recovery=True))
        campaign.request_stop("operator requested stop from native project terminal")

    def _launch_worker(
        self, session: Mapping[str, Any], *, operation: str, **options: Any
    ) -> Mapping[str, Any]:
        locator = session.get("locator")
        if not isinstance(locator, str) or not locator:
            raise TerminalDriverError("cannot launch a worker without an exact locator")
        worker_root = _worker_root(self.config, session["session_id"])
        worker_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_path = worker_root / "worker.log"
        status_path = worker_root / "status.json"
        command = [
            sys.executable,
            "-m",
            "bearhug.terminal_driver",
            "worker",
            "--config",
            self._config_locator(),
            "--locator",
            locator,
            "--session-id",
            session["session_id"],
            "--operation",
            operation,
        ]
        for name in ("question_id", "disposition", "recovery_outcome", "episode_id", "review_id"):
            if options.get(name) is not None:
                command.extend((f"--{name.replace('_', '-')}", str(options[name])))
        log = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                cwd=self.config.subject_path,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=dict(os.environ),
                close_fds=True,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise TerminalDriverError(f"cannot start campaign worker: {exc}") from exc
        finally:
            log.close()
        worker = {
            "status": "started",
            "operation": operation,
            "pid": process.pid,
            "started_at": time.time(),
            "log_path": log_path.as_posix(),
            "status_path": status_path.as_posix(),
        }
        _write_atomic(status_path, _canonical(worker), mode=0o600)
        return worker

    def _config_locator(self) -> str:
        # The enrollment command writes this conventional path.  A direct API caller can still
        # use ``hook-dispatch --config``; worker children inherit that exact path through argv.
        path = self.config_path or self.config.subject_path / ".bearhug" / "terminal-driver.json"
        return path.as_posix()


def _worker_args(
    *,
    action: str,
    locator: str,
    question_id: str | None = None,
    disposition: str | None = None,
    recovery_outcome: str | None = None,
    episode_id: str | None = None,
    review_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        action=action,
        locator=locator,
        successor_plan=None,
        question_id=question_id,
        disposition=disposition,
        recovery_outcome=recovery_outcome,
        episode_id=episode_id,
        review_id=review_id,
        answer="",
        answer_file=None,
        reason="operator requested stop from native project terminal",
    )


def run_worker(
    config: TerminalDriverConfig,
    *,
    locator: str,
    session_id: str,
    operation: str,
    question_id: str | None = None,
    disposition: str | None = None,
    recovery_outcome: str | None = None,
    episode_id: str | None = None,
    review_id: str | None = None,
) -> int:
    if operation not in {"run", "resume", "stop", "answer", "recover", "repair"}:
        raise TerminalDriverError("unsupported worker operation")
    locator_path = _exact_locator(locator, config.private_root)
    worker_root = _worker_root(config, session_id)
    worker_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    status_path = worker_root / "status.json"
    started = {
        "status": "running",
        "operation": operation,
        "locator": locator,
        "session_id": session_id,
        "started_at": time.time(),
    }
    # The parent publishes its started record while holding this same lock.  Taking the lock
    # before the child writes prevents a fast child from publishing ``finished`` and then being
    # overwritten by the parent's post-Popen ``started`` record.
    with _intake_lock(config):
        _write_atomic(status_path, _canonical(started), mode=0o600)
    exit_code = 0
    result: dict[str, Any] = {}
    try:
        from bearhug.campaign.capsule_campaign import run_prepared_command

        action = "run" if operation == "run" else operation
        result_obj = run_prepared_command(
            _worker_args(
                action=action,
                locator=locator_path.as_posix(),
                question_id=question_id,
                disposition=disposition,
                recovery_outcome=recovery_outcome,
                episode_id=episode_id,
                review_id=review_id,
            )
        )
        result = {
            "status": result_obj.report.get("status"),
            "reason": result_obj.report.get("reason"),
            "exit_code": result_obj.exit_code,
        }
        exit_code = result_obj.exit_code
    except BaseException as exc:
        exit_code = 1
        result = {"status": "blocked", "reason": str(exc), "exit_code": exit_code}
    finished = {**started, **result, "status": "finished", "finished_at": time.time()}
    with _intake_lock(config):
        _write_atomic(status_path, _canonical(finished), mode=0o600)
        session = _load_session(config, session_id)
        if session is not None and session.get("locator") == locator:
            worker = dict(session.get("worker") or {})
            worker.update(
                status="finished",
                finished_at=finished["finished_at"],
                exit_code=exit_code,
                status_path=status_path.as_posix(),
            )
            session["worker"] = worker
            session["status"] = result.get("status", "blocked")
            session["reason"] = result.get("reason", "worker finished")
            _save_session(config, session)
    print(json.dumps(finished, ensure_ascii=False, sort_keys=True), flush=True)
    return exit_code


def _decision_json(decision: ProjectTerminalDecision) -> bytes:
    value: dict[str, Any] = {"decision": decision.decision}
    if decision.reason is not None:
        value["reason"] = decision.reason
    if decision.additional_context is not None:
        value["additional_context"] = decision.additional_context
    return _canonical(value)


def _enroll(args: argparse.Namespace) -> int:
    if args.provider == "codex":
        raise TerminalDriverError(
            "native terminal enrollment is unavailable for codex until its project hooks are "
            "qualified"
        )
    subject = _absolute_dir(args.subject, label="subject")
    config_path = _config_path(subject, args.config)
    state_root = _absolute_dir(args.state_root, label="state root", create=True, private=True)
    config = TerminalDriverConfig(
        subject=subject,
        provider=args.provider,
        template_path=_absolute_file(args.template_path, label="template"),
        policy_path=_absolute_file(args.policy_path, label="policy"),
        execution_path=_absolute_file(args.execution_path, label="execution config"),
        state_root=state_root,
    )
    raw = _canonical(config.to_mapping())
    if config_path.exists():
        if config_path.is_symlink() or config_path.read_bytes() != raw:
            raise TerminalDriverError("existing terminal driver config differs")
    else:
        _write_atomic(config_path, raw, mode=0o600, private_parent=False)
    dispatcher = (
        sys.executable,
        "-m",
        "bearhug.terminal_driver",
        "hook-dispatch",
        "--config",
        config_path.as_posix(),
    )
    enrollment = enroll_project(subject, provider=config.provider, dispatcher_argv=dispatcher)
    print(
        json.dumps(
            {
                "config_path": config_path.as_posix(),
                "profile_path": enrollment.profile_path.as_posix(),
                "native_config_path": enrollment.native_config_path.as_posix(),
                "changed": enrollment.changed,
                "note": "review and commit enrollment/config files before native execution",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _opt_out(args: argparse.Namespace) -> int:
    subject = _absolute_dir(args.subject, label="subject")
    config_path = _config_path(subject, args.config)
    config = load_config(config_path)
    dispatcher = (
        sys.executable,
        "-m",
        "bearhug.terminal_driver",
        "hook-dispatch",
        "--config",
        config_path.as_posix(),
    )
    result = opt_out_project(subject, provider=config.provider, dispatcher_argv=dispatcher)
    print(json.dumps({"profile_path": result.profile_path.as_posix(), "changed": result.changed}))
    return 0


def _status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with _intake_lock(config):
        session = _load_session(config, args.session_id)
        if session is None:
            print(json.dumps({"session_id": args.session_id, "status": "idle"}, sort_keys=True))
            return 0
        _refresh_session(config, session)
        _save_session(config, session)
    print(json.dumps(session, ensure_ascii=False, sort_keys=True))
    return 0


def _hook_dispatch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    config_path = _config_path(config.subject_path, args.config)
    raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
    try:
        decision = TerminalDriver(config, config_path=config_path).dispatch(raw)
        sys.stdout.buffer.write(_decision_json(decision))
        sys.stdout.buffer.flush()
        return 0
    except (ProjectTerminalError, TerminalDriverError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _add_command_parsers(subparsers: Any) -> None:
    enroll = subparsers.add_parser("enroll")
    enroll.add_argument("--subject", required=True)
    enroll.add_argument("--provider", choices=("claude", "codex"), required=True)
    enroll.add_argument("--template-path", required=True)
    enroll.add_argument("--policy-path", required=True)
    enroll.add_argument("--execution-path", required=True)
    enroll.add_argument("--state-root", required=True)
    enroll.add_argument("--config")

    opt_out = subparsers.add_parser("opt-out")
    opt_out.add_argument("--subject", required=True)
    opt_out.add_argument("--config")

    hook = subparsers.add_parser("hook-dispatch")
    hook.add_argument("--config", required=True)

    worker = subparsers.add_parser("worker")
    worker.add_argument("--config", required=True)
    worker.add_argument("--locator", required=True)
    worker.add_argument("--session-id", required=True)
    worker.add_argument(
        "--operation", choices=("run", "resume", "stop", "answer", "recover", "repair"),
        required=True,
    )
    worker.add_argument("--question-id")
    worker.add_argument("--disposition")
    worker.add_argument("--recovery-outcome", choices=("failed", "blocked", "hil_required"))
    worker.add_argument("--episode-id")
    worker.add_argument("--review-id")

    status = subparsers.add_parser("status")
    status.add_argument("--config", required=True)
    status.add_argument("--session-id", required=True)


def add_terminal_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register the public ``bearhug terminal`` command on the root CLI parser."""
    terminal = subparsers.add_parser(
        "terminal", help="enroll and drive one native project-terminal campaign"
    )
    commands = terminal.add_subparsers(dest="terminal_command", required=True)
    _add_command_parsers(commands)
    terminal.set_defaults(func=run_terminal_command)
    return terminal


def run_terminal_command(args: argparse.Namespace) -> int:
    command = getattr(args, "terminal_command", None) or getattr(args, "command", None)
    try:
        if command == "enroll":
            return _enroll(args)
        if command == "opt-out":
            return _opt_out(args)
        if command == "hook-dispatch":
            return _hook_dispatch(args)
        if command == "status":
            return _status(args)
        if command == "worker":
            return run_worker(
                load_config(args.config),
                locator=args.locator,
                session_id=args.session_id,
                operation=args.operation,
                question_id=args.question_id,
                disposition=args.disposition,
                recovery_outcome=args.recovery_outcome,
                episode_id=args.episode_id,
                review_id=args.review_id,
            )
    except (
        ProjectTerminalError,
        TerminalDriverError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bearhug.terminal_driver")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_command_parsers(subparsers)

    args = parser.parse_args(list(argv) if argv is not None else None)
    return run_terminal_command(args)


__all__ = [
    "CONFIG_KIND",
    "SCHEMA_VERSION",
    "SESSION_KIND",
    "TerminalDriver",
    "TerminalDriverConfig",
    "TerminalDriverError",
    "add_terminal_parser",
    "load_config",
    "main",
    "run_worker",
    "run_terminal_command",
]


if __name__ == "__main__":
    raise SystemExit(main())
