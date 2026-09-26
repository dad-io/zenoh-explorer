"""Durable execution and recovery for one v2 execution capsule.

The v1 controller is intentionally left unit shaped.  This module supplies the additive v2
boundary: one capsule owns a lease, an append-only semantic journal, and one or more provider
episodes.  Episode inputs are content addressed execution packets and episode outputs are
validated provider custody (or explicitly marked fixture evidence).  The runtime stops at a
candidate, reconciliation, HIL, block, or failure; it never activates a later capsule.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bearhug.campaign.capsule_candidate import (
    capture_capsule_candidate,
    validate_dependency_base,
    verify_dependency_base,
)
from bearhug.campaign.capsule_grounding import CapsuleGroundingError, capture_capsule_grounding
from bearhug.campaign.capsule_packets import (
    _HARD_MAX_PROMPT_BYTES,
    _HARD_MAX_PROMPT_TOKENS,
    render_execution_packet,
)
from bearhug.campaign.capsule_storage import CapsuleObjectStore
from bearhug.campaign.capsules import (
    CANONICAL_ALGORITHM,
    CapsuleContractError,
    _canonical_capsule,
    append_capsule_journal,
    validate_capsule_plan,
    validate_capsule_result,
    validate_intent_envelope,
)
from bearhug.campaign.leases import CampaignLeaseError, CampaignLeaseStore, LeaseRecord
from bearhug.campaign.reconciliation import (
    ReconciliationError,
    build_reconciliation_record,
    reconciliation_digest,
    validate_reconciliation_record,
)
from bearhug.campaign.review import worktree_sha256
from bearhug.processes import observing_spawns
from bearhug.providers.custody import ProviderCustodyError, ProviderCustodyStore
from bearhug.providers.failure_receipt import (
    ProviderFailureReceiptError,
    validate_provider_failure_receipt,
)
from bearhug.providers.final_output import (
    ProviderFinalOutputError,
    provider_terminal_failure,
    strict_final_json,
)
from bearhug.providers.policy import (
    ProviderPolicy,
    ProviderPolicyError,
    provider_role,
    validate_provider_policy,
)
from bearhug.providers.qualification_index import (
    ProviderQualificationIndex,
    ProviderQualificationIndexError,
)
from bearhug.providers.receipt import (
    LaunchRepository,
    ProviderReceiptError,
    capture_close_repository,
    capture_launch_repository,
    validate_provider_receipt,
)


class CapsuleRuntimeError(RuntimeError):
    """The capsule cannot safely advance from its durable state."""


class CapsuleRuntimeRecoveryError(CapsuleRuntimeError):
    """A process ended after spend began and before a terminal episode record was durable."""


class LeaseFenceLost(CapsuleRuntimeError):
    """The exact durable lease identity no longer authorizes this runtime."""


def bind_author_observations(value: Mapping[str, Any], receipt_sha256: str) -> dict[str, Any]:
    """Bind author claims to custody, retaining original citations in the raw receipt.

    A model's Git ID, filename or mistyped digest is not a content-addressed evidence
    reference. The receipt proves what the author reported, not that the claim passed;
    candidate validation and independent review still establish that separately.
    """
    if not isinstance(receipt_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256):
        raise CapsuleRuntimeError("author observations require provider receipt custody")
    result = copy.deepcopy(value)
    rows = []
    for key in ("obligation_coverage", "invariants"):
        if isinstance(result.get(key), list):
            rows.extend(result[key])
    observations = result.get("reconciliation_observations")
    if isinstance(observations, dict) and isinstance(observations.get("bindings"), dict):
        rows.extend(observations["bindings"].values())
    for row in rows:
        if not isinstance(row, dict):
            continue  # The contract validator remains responsible for row shape/status.
        refs = row.get("evidence_refs")
        valid = (
            [ref for ref in refs if isinstance(ref, str) and re.fullmatch(r"[0-9a-f]{64}", ref)]
            if isinstance(refs, list)
            else []
        )
        row["evidence_refs"] = sorted({*valid, receipt_sha256})
    return result


EpisodeStatus = Literal[
    "continue_with_evidence",
    "candidate_ready",
    "local_repair_required",
    "reconciliation_required",
    "hil_required",
    "blocked",
    "failed",
]

_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_STATE_FILE = re.compile(r"^(?P<sequence>[0-9]{20})-(?P<digest>[0-9a-f]{64})\.json$")
_TERMINAL = {"candidate_ready", "accepted", "completed", "blocked", "failed", "superseded"}
_RUNNABLE_STATES = {"preflighted", "continuing", "locally_repairing"}
# A release-only open may tolerate a checkout ahead of the durable episode head only when the
# capsule's own top-level disposition is one of these two. A human decision still pending
# (`awaiting_hil`) or any successful disposition (`candidate_ready`, `accepted`, `completed`,
# `superseded`) is a separate judgment call this tolerance does not make.
_RELEASE_ONLY_QUARANTINABLE_STATES = frozenset({"failed", "blocked"})
_OUTCOMES = {
    "continue_with_evidence",
    "candidate_ready",
    "local_repair_required",
    "reconciliation_required",
    "hil_required",
    "blocked",
    "failed",
}
_HIL_DECISIONS = {"approve", "deny", "amend", "defer", "stop"}
_HIL_OPTION_IDS = {"approve", "deny", "amend", "defer", "stop"}
_EVENT_FOR_OUTCOME = {
    "continue_with_evidence": "episode_completed",
    "candidate_ready": "candidate_ready",
    "local_repair_required": "validation_observed",
    "reconciliation_required": "reconciliation_requested",
    "hil_required": "hil_requested",
    "blocked": "capsule_blocked",
    "failed": "episode_completed",
}
_P0_CHARTER = (
    "Bear Hug capsule charter: execute only the sealed intent and authorized mutation envelope. "
    "The intent and accepted project authority define meaning; the campaign envelope, policy, "
    "budget, and lease define operational permission. Preserve exact Git/provider custody. Work "
    "freely inside the envelope, run validation, and stop for changed meaning, new authority, "
    "scope escape, lease loss, or an unsafe recovery. A candidate remains unaccepted until review "
    "and integration authority act."
)


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CapsuleRuntimeError(f"runtime value is not canonical JSON: {exc}") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).rstrip(b"\n")).hexdigest()


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CapsuleRuntimeError(f"{label} must be lowercase SHA-256")
    return value


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise CapsuleRuntimeError(f"{label} must be a canonical token")
    return value


def _private_directory(path: Path, *, create: bool) -> Path:
    path = Path(path).expanduser()
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.is_symlink():
            raise CapsuleRuntimeError(f"runtime directory may not be a symlink: {path}")
        resolved = path.resolve(strict=True)
        stat_result = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise CapsuleRuntimeError(f"runtime directory is unavailable: {path}") from exc
    if not resolved.is_dir() or stat_result.st_uid != os.geteuid() or stat_result.st_mode & 0o077:
        raise CapsuleRuntimeError(f"runtime directory must be owner-only: {resolved}")
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CapsuleRuntimeError(f"cannot read runtime state {path}: {exc}") from exc

    def closed(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CapsuleRuntimeError(f"runtime JSON repeats key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=closed)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleRuntimeError(f"runtime state is not canonical JSON: {path}") from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise CapsuleRuntimeError(f"runtime state is not canonical JSON: {path}")
    return value


def _create_only(path: Path, value: Mapping[str, Any]) -> str:
    raw = _canonical(value)
    digest = hashlib.sha256(raw).hexdigest()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / f".pending-{os.getpid()}-{secrets.token_hex(12)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise CapsuleRuntimeError(f"refusing to replace runtime state {path}") from exc
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
    return digest


def _create_blob_only(path: Path, raw: bytes) -> str:
    """Publish immutable bytes with the same crash-safe create-only discipline as state."""

    if not isinstance(raw, bytes):
        raise CapsuleRuntimeError("runtime blob must be exact bytes")
    digest = hashlib.sha256(raw).hexdigest()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / f".pending-{os.getpid()}-{secrets.token_hex(12)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            if path.read_bytes() != raw:
                raise CapsuleRuntimeError(f"immutable runtime blob changed: {path}") from exc
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
    return digest


def _oid_from_git(cwd: Path, expression: str) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(cwd), "rev-parse", "--verify", expression),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CapsuleRuntimeError(f"cannot inspect Git state: {exc}") from exc
    if result.returncode:
        raise CapsuleRuntimeError(f"git cannot resolve {expression!r}")
    value = result.stdout.decode("ascii", errors="strict").strip()
    if _OID.fullmatch(value) is None:
        raise CapsuleRuntimeError(f"Git returned an invalid object id for {expression!r}")
    return value


def _branch(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(cwd), "symbolic-ref", "--quiet", "--short", "HEAD"),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CapsuleRuntimeError(f"cannot inspect Git branch: {exc}") from exc
    branch = result.stdout.decode("utf-8", errors="strict").strip()
    if result.returncode or not branch:
        raise CapsuleRuntimeError("capsule worktree must be on a named branch")
    return branch


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(
            ("git", "-C", str(cwd), "merge-base", "--is-ancestor", ancestor, descendant),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CapsuleRuntimeError(f"cannot inspect crash recovery ancestry: {exc}") from exc
    return result.returncode == 0


_PROVIDER_PROCESSES_DIRNAME = "provider-processes"
_PROVIDER_PROCESS_RECORD_NAME = re.compile(r"^[0-9]{4}\.json$")


def _provider_process_episode_dir(capsule_root: Path, episode_id: str) -> Path:
    """The durable directory for one episode's recorded provider processes.

    A sibling of ``<capsule root>/states``, never a descendant of it, so ``_load_states`` and
    ``_state_files_exist`` -- both of which only ever look inside ``states/`` -- never read it.
    ``episode_id`` may contain path separators (``_TOKEN`` allows them, matching the identifier
    tokens used throughout this module); hashing it before using it as a path component is the
    same defence ``CapsuleRuntime._invoke`` already applies to build its own provider output
    directory from the same value.
    """

    return (
        capsule_root
        / _PROVIDER_PROCESSES_DIRNAME
        / hashlib.sha256(episode_id.encode("utf-8")).hexdigest()
    )


# The marker recorded alongside `leader_start_time`: it names the exact rendering convention
# (`ps -o lstart=` under a fixed C locale and a fixed UTC time zone) that
# `_provider_process_record_alive` requires before it will treat a start-time mismatch as proof
# of a recycled id. `ps -o lstart=` is locale-formatted and renders local time: a recording and
# a probe taken under different `LC_ALL`/`LANG`/`TZ` render the SAME live process as different
# strings, which would otherwise read as "recycled, therefore gone" for a process that never
# stopped. Bump this string if the rendering convention ever changes; an old record then simply
# never qualifies for the shortcut again (see the loader and `_provider_process_record_alive`).
_LEADER_START_TIME_FORMAT = "ps-lstart/LC_ALL=C/TZ=UTC"

# The one fixed `strptime` format for a string rendered under `_LEADER_START_TIME_FORMAT`. `ps`
# space-pads a single-digit day (two spaces before it, e.g. "Mon Jan  1 ..."); `strptime`
# already collapses that run of whitespace against the single space in this pattern.
_LEADER_START_TIME_STRPTIME = "%a %b %d %H:%M:%S %Y"

# Two rendered timestamps for the same still-running process can differ by a second or two from
# ordinary clock/formatting jitter alone. Only a gap wider than this counts as proof of a
# recycled id; anything at or under it is treated as the same process.
_LEADER_START_TIME_RECYCLE_THRESHOLD_SECONDS = 2.0


def _process_start_time_environment() -> dict[str, str]:
    """The fixed environment `_process_start_time` runs `ps` under.

    A copy of the current environment (so `PATH` and everything else needed to find and run
    `ps` survive) with the locale and time-zone variables that affect how `ps -o lstart=` renders
    pinned to fixed values: `LC_ALL`/`LANG` forced to the `C` locale, `LC_TIME` removed (it would
    otherwise override `LC_ALL` for time rendering specifically), and `TZ` forced to `UTC`. Two
    calls made under this environment render the same instant identically no matter what locale
    or time zone the calling process itself happens to run under.
    """

    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    environment.pop("LC_TIME", None)
    environment["TZ"] = "UTC"
    return environment


def _process_start_time(pid: int) -> str:
    """The leader's start time as the OS reports it, best effort.

    Recorded at spawn time and compared again at probe time
    (``_provider_process_record_alive``): a live process at the recorded pgid whose start time
    no longer matches proves the id was recycled, so the original group is gone. Never raises: a
    `ps` that is missing, slow, or reports nothing about an already-reaped pid all resolve to
    ``""``, which the comparison treats as unreadable, not as a match.

    Runs `ps` under ``_process_start_time_environment``'s fixed locale and time zone so the
    rendering never depends on the calling process's own `LC_ALL`/`LANG`/`TZ`: the recording
    side and the probing side can run under completely different ambient environments (an
    operator's terminal versus a worker launched with a minimal one) and still render the same
    live process identically.
    """

    try:
        result = subprocess.run(
            ("ps", "-o", "lstart=", "-p", str(pid)),
            capture_output=True,
            check=False,
            timeout=5,
            env=_process_start_time_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    try:
        return result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return ""


def _parse_leader_start_time(value: str) -> datetime | None:
    """Parse a `ps -o lstart=` string rendered under `_LEADER_START_TIME_FORMAT`.

    Returns ``None`` for anything that does not parse cleanly with the one fixed format --
    deliberately strict, since only a value actually produced by that fixed rendering is fit to
    prove a recycled id. An unparseable string proves nothing, so the caller must fail closed.
    """

    if not value:
        return None
    try:
        return datetime.strptime(value, _LEADER_START_TIME_STRPTIME)
    except ValueError:
        return None


def _write_provider_process_record(
    capsule_root: Path,
    *,
    episode_id: str,
    sequence: int,
    pid: int,
    pgid: int,
    recorded_at: float,
) -> None:
    """Durably record one provider spawn, create-only and fsynced (never overwritten).

    Written under ``_provider_process_episode_dir``, never inside the provider's own run
    directory (``provider_output_dir``): custody validation and the evidence builders read
    that directory, and nothing there may change for this work.
    """

    record = {
        "episode_id": episode_id,
        "pid": pid,
        "pgid": pgid,
        "recorded_at": recorded_at,
        "leader_start_time": _process_start_time(pid),
        "leader_start_time_format": _LEADER_START_TIME_FORMAT,
    }
    path = _provider_process_episode_dir(capsule_root, episode_id) / f"{sequence:04d}.json"
    _create_only(path, record)


def _load_provider_process_records(
    capsule_root: Path, episode_id: str, *, strict: bool = True
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], list[str]]:
    """Every durably recorded provider-process spawn for one episode, oldest first.

    ``strict`` (the default) raises rather than silently ignoring a file that does not read
    back as a well-formed record, so a tampered or corrupted record makes recovery fail closed
    (refuse) instead of guessing -- every recovery-affecting caller uses this: the runtime's own
    chokepoint and the CLI's synchronous courtesy check (both reach this through
    ``_recovery_provider_liveness_refusal``). A display-only caller (``status``, through
    ``terminal_driver._status_for_locator``) passes ``strict=False``: an unreadable or malformed
    record is skipped and its file name collected instead of raising, so one corrupt leftover
    cannot take the whole status read down with it; the return becomes
    ``(records, unreadable_names)``.

    A file name that does not look like a completed record (``NNNN.json``, mirroring
    ``_state_paths``'s own file-name discipline) is skipped in both modes without being read at
    all -- for example a leftover ``.pending-<pid>-<hex>`` temp file left behind by a kill
    between ``_create_only``'s write and its link into place. It was never linked under its
    final name, so no completed spawn was ever durably attributed to it.

    Absence of the directory itself is ordinary -- an episode that crashed before any spawn,
    or state from before this recording existed -- and returns no records, not an error.

    ``leader_start_time_format`` is optional: a record written before it existed loads exactly
    like one written after, it just never carries the marker ``_provider_process_record_alive``
    requires before it will treat a start-time mismatch as proof of a recycled id. When present
    it must be a string like any other field here; there is no other constraint on its value at
    load time, since only an exact match against ``_LEADER_START_TIME_FORMAT`` at comparison time
    (not at load time) makes it usable for that shortcut.
    """

    directory = _provider_process_episode_dir(capsule_root, episode_id)
    if not directory.is_dir():
        return [] if strict else ([], [])
    records: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for path in sorted(directory.iterdir()):
        if not _PROVIDER_PROCESS_RECORD_NAME.match(path.name):
            continue
        if not path.is_file() or path.is_symlink():
            if strict:
                raise CapsuleRuntimeError(
                    "provider process record directory contains a non-regular file"
                )
            unreadable.append(path.name)
            continue
        try:
            value = _read_json(path)
        except CapsuleRuntimeError as exc:
            if strict:
                raise CapsuleRuntimeError(f"provider process record is unreadable: {path}") from exc
            unreadable.append(path.name)
            continue
        if (
            value.get("episode_id") != episode_id
            or type(value.get("pid")) is not int
            or value["pid"] <= 0
            or type(value.get("pgid")) is not int
            or value["pgid"] <= 0
            or not isinstance(value.get("recorded_at"), (int, float))
            or not isinstance(value.get("leader_start_time"), str)
            or (
                "leader_start_time_format" in value
                and not isinstance(value.get("leader_start_time_format"), str)
            )
        ):
            if strict:
                raise CapsuleRuntimeError("provider process record is malformed")
            unreadable.append(path.name)
            continue
        records.append(value)
    if not strict:
        return records, unreadable
    return records


def _process_group_alive(pgid: int) -> bool:
    """False only when every process in the group is provably gone.

    ``ProcessLookupError`` (ESRCH: no such process group) is the only outcome that counts as
    gone. A ``PermissionError`` (a real, foreign process group this caller cannot signal) is
    treated as alive: the probe cannot prove absence, so the caller must fail closed. Signal 0
    never delivers a signal; it only tests existence and permission.
    """

    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _provider_process_record_alive(record: Mapping[str, Any]) -> bool:
    """False only when this record's process is provably gone.

    Starts from ``_process_group_alive``'s pgid probe: ``ProcessLookupError`` is still the only
    unconditional "gone". When something answers for the pgid, also compare the recorded
    leader's start time (``leader_start_time``) against what the OS reports right now for the
    process at that same numeric id -- both provider launch sites make the leader its own
    session and group leader (``start_new_session=True``), so pid equals pgid for every record
    this matters for. A live process answering at the recorded pgid with a *different* start
    time proves the id was recycled: the kernel never reuses a pid while any process still uses
    it as a group or session id, so the ORIGINAL group is gone even though the probe finds
    something alive at the same number today.

    That "different start time proves recycled" step is trusted only when every one of these
    holds, and fails closed (counts the record as alive) the moment any of them does not:

    - the record carries ``leader_start_time_format`` and it is exactly
      ``_LEADER_START_TIME_FORMAT`` -- a record with no marker (written before this comparison
      existed) or a different marker (a future rendering convention this code does not know how
      to read) never qualifies;
    - the recorded and the freshly probed strings are both non-empty and both parse under the
      one fixed ``_LEADER_START_TIME_STRPTIME`` format;
    - the two parsed times differ by more than
      ``_LEADER_START_TIME_RECYCLE_THRESHOLD_SECONDS`` -- a smaller gap is ordinary clock and
      formatting jitter for the same process, not proof of a different one.
    """

    if not _process_group_alive(record["pgid"]):
        return False
    if record.get("leader_start_time_format") != _LEADER_START_TIME_FORMAT:
        return True
    recorded_start = record.get("leader_start_time")
    if not recorded_start:
        return True
    current_start = _process_start_time(record["pgid"])
    if not current_start:
        return True
    recorded_time = _parse_leader_start_time(recorded_start)
    current_time = _parse_leader_start_time(current_start)
    if recorded_time is None or current_time is None:
        return True
    gap = abs((current_time - recorded_time).total_seconds())
    return gap <= _LEADER_START_TIME_RECYCLE_THRESHOLD_SECONDS


def _provider_process_liveness(capsule_root: Path, episode_id: str) -> list[dict[str, Any]]:
    """``[{"pid": ..., "alive": ...}, ...]`` for every provider process recorded for an episode."""

    return [
        {"pid": record["pid"], "alive": _provider_process_record_alive(record)}
        for record in _load_provider_process_records(capsule_root, episode_id)
    ]


def _recovery_provider_liveness_refusal(capsule_root: Path, episode_id: str) -> str | None:
    """``None`` when ``hil_required`` recovery may proceed for this interrupted episode.

    This is the single function that decides the answer: ``CapsuleRuntime._recover_episode``
    (the real guard, which refuses before any state transition) and
    ``project_campaign.py``'s synchronous courtesy check both call exactly this function, so
    the CLI's early "blocked" answer and the worker's eventual refusal can never disagree.
    """

    records = _load_provider_process_records(capsule_root, episode_id)
    if not records:
        return (
            "no provider process was recorded for the interrupted attempt; hil_required "
            "recovery cannot confirm it has exited. Close the attempt for good with "
            "--recovery-outcome failed and start a fresh campaign."
        )
    alive_pids = sorted(
        {record["pid"] for record in records if _provider_process_record_alive(record)}
    )
    if alive_pids:
        plural = len(alive_pids) > 1
        pids = ", ".join(str(pid) for pid in alive_pids)
        return (
            "a provider process from the interrupted attempt may still be running "
            f"(pid{'s' if plural else ''} {pids}). Wait until "
            f"{'they have' if plural else 'it has'} exited and run this again, or close the "
            "attempt for good with --recovery-outcome failed and start a fresh campaign."
        )
    return None


def _json_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).rstrip(b"\n")).hexdigest()


def _validate_hil_request(value: Mapping[str, Any]) -> None:
    """Validate the small durable HIL projection kept in runtime state."""

    required = {
        "schema_version",
        "record_kind",
        "canonical_algorithm",
        "request_id",
        "intent_envelope_sha256",
        "plan_sha256",
        "revision_id",
        "capsule_id",
        "subject",
        "conflict",
        "evidence_refs",
        "options",
        "correction_class",
        "requires_revision",
        "head_oid",
        "state_sequence",
        "resume_token_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise CapsuleRuntimeError("HIL request is not a closed durable record")
    if (
        value.get("schema_version") != "1"
        or value.get("record_kind") != "capsule_hil_request"
        or value.get("canonical_algorithm") != CANONICAL_ALGORITHM
    ):
        raise CapsuleRuntimeError("HIL request identity is invalid")
    for field in ("request_id", "revision_id", "capsule_id"):
        _token(value.get(field), f"HIL request {field}")
    if value.get("correction_class") not in {
        "local_correction",
        "job_ticket_revision",
        "press_change",
    }:
        raise CapsuleRuntimeError("HIL request correction class is invalid")
    if type(value.get("requires_revision")) is not bool:
        raise CapsuleRuntimeError("HIL request requires_revision is invalid")
    if not isinstance(value.get("head_oid"), str) or _OID.fullmatch(value["head_oid"]) is None:
        raise CapsuleRuntimeError("HIL request head binding is invalid")
    if type(value.get("state_sequence")) is not int or value["state_sequence"] < 1:
        raise CapsuleRuntimeError("HIL request state binding is invalid")
    for field in (
        "intent_envelope_sha256",
        "plan_sha256",
        "resume_token_sha256",
    ):
        _sha(value.get(field), f"HIL request {field}")
    if not isinstance(value.get("subject"), Mapping):
        raise CapsuleRuntimeError("HIL request subject is invalid")
    if not isinstance(value.get("conflict"), str) or not value["conflict"].strip():
        raise CapsuleRuntimeError("HIL request conflict is invalid")
    refs = value.get("evidence_refs")
    if not isinstance(refs, list) or any(_SHA256.fullmatch(item) is None for item in refs):
        raise CapsuleRuntimeError("HIL request evidence_refs are invalid")
    options = value.get("options")
    if (
        not isinstance(options, list)
        or not options
        or len(options) > len(_HIL_OPTION_IDS)
        or any(not isinstance(item, Mapping) for item in options)
    ):
        raise CapsuleRuntimeError("HIL request options are invalid")
    option_ids: set[str] = set()
    for option in options:
        if set(option) != {"option_id", "label", "consequences"}:
            raise CapsuleRuntimeError("HIL request option is not closed")
        option_id = option.get("option_id")
        if option_id not in _HIL_OPTION_IDS or option_id in option_ids:
            raise CapsuleRuntimeError("HIL request option identity is invalid")
        option_ids.add(option_id)
        if not isinstance(option.get("label"), str) or not option["label"].strip():
            raise CapsuleRuntimeError("HIL request option label is invalid")
        consequences = option.get("consequences")
        if (
            not isinstance(consequences, list)
            or not consequences
            or any(not isinstance(item, str) or not item.strip() for item in consequences)
        ):
            raise CapsuleRuntimeError("HIL request option consequences are invalid")


def _validate_hil_answer(value: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "record_kind",
        "canonical_algorithm",
        "answer_id",
        "request_id",
        "request_sha256",
        "intent_envelope_sha256",
        "plan_sha256",
        "revision_id",
        "capsule_id",
        "decision",
        "answer",
        "answer_sha256",
        "token_sha256",
        "amendment",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise CapsuleRuntimeError("HIL answer is not a closed durable record")
    if (
        value.get("schema_version") != "1"
        or value.get("record_kind") != "capsule_hil_answer"
        or value.get("canonical_algorithm") != CANONICAL_ALGORITHM
    ):
        raise CapsuleRuntimeError("HIL answer identity is invalid")
    for field in ("answer_id", "request_id", "revision_id", "capsule_id"):
        _token(value.get(field), f"HIL answer {field}")
    for field in (
        "request_sha256",
        "intent_envelope_sha256",
        "plan_sha256",
        "answer_sha256",
        "token_sha256",
    ):
        _sha(value.get(field), f"HIL answer {field}")
    if value.get("decision") not in _HIL_DECISIONS:
        raise CapsuleRuntimeError("HIL answer decision is unsupported")
    if not isinstance(value.get("answer"), str) or len(value["answer"]) > 3500:
        raise CapsuleRuntimeError("HIL answer text is invalid")
    if hashlib.sha256(value["answer"].encode("utf-8")).hexdigest() != value["answer_sha256"]:
        raise CapsuleRuntimeError("HIL answer text differs from its digest")
    if value.get("amendment") is not None and not isinstance(value.get("amendment"), Mapping):
        raise CapsuleRuntimeError("HIL answer amendment must be an object or null")


def _provider_policy_document(policy: ProviderPolicy) -> dict[str, Any]:
    """Return the closed, canonical policy document represented by a validated policy."""

    return {
        "schema_version": "1",
        "roles": {
            name: {
                "provider": role.provider,
                "model": role.model,
                "effort": role.effort,
                "sandbox": role.sandbox,
                "approval_policy": role.approval_policy,
                "required_capabilities": list(role.required_capabilities),
            }
            for name, role in policy.roles.items()
        },
    }


@dataclass(frozen=True, slots=True)
class EpisodeOutcome:
    """Validated semantic outcome returned by one provider episode."""

    outcome: EpisodeStatus
    episode_id: str
    packet_sha256: str | None
    provider_receipt_sha256: str | None = None
    failure_receipt_sha256: str | None = None
    validation: tuple[dict[str, Any], ...] = ()
    changed_facts: tuple[str, ...] = ()
    discoveries: tuple[str, ...] = ()
    unresolved_decisions: tuple[str, ...] = ()
    next_action: str | None = None
    usage: Mapping[str, int] | None = None

    @property
    def status(self) -> EpisodeStatus:
        """Alias used by adapters that call the field status."""

        return self.outcome


ProviderRunner = Callable[..., Any]


class CapsuleRuntime:
    """One lease-bound capsule that can span portable fresh provider episodes."""

    def __init__(
        self,
        *,
        intent_envelope: Mapping[str, Any],
        capsule_plan: Mapping[str, Any],
        capsule: Mapping[str, Any],
        worktree: Path | str,
        state_root: Path | str,
        lease: LeaseRecord | None = None,
        lease_store: CampaignLeaseStore | None = None,
        lease_ttl_seconds: float = 28800.0,
        policy_sha256: str | None = None,
        lease_sha256: str | None = None,
        source_contents: Sequence[Mapping[str, Any]] | Mapping[str, Any] = (),
        stable_charter: str = _P0_CHARTER,
        provider_runner: ProviderRunner | None = None,
        validation_commands: tuple[tuple[str, ...], ...] = (),
        transport: Callable[[dict[str, Any], str], Any] | None = None,
        provider_policy: ProviderPolicy | None = None,
        qualification_index: ProviderQualificationIndex | None = None,
        campaign_root: Path | str | None = None,
        campaign_id: str | None = None,
        claimant_id: str | None = None,
        branch: str | None = None,
        compatibility_repository_root: Path | str | None = None,
        provider_output_root: Path | str | None = None,
        provider_role_name: str | None = None,
        receipt_role_name: str = "author",
        dependency_base: Mapping[str, Any] | None = None,
        recovery: bool = False,
        release_only: bool = False,
        campaign_deadline: float | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        try:
            validate_intent_envelope(dict(intent_envelope))
            try:
                validate_capsule_plan(dict(capsule_plan), intent_envelope=dict(intent_envelope))
            except CapsuleContractError:
                # A successor plan must be checked against its durable predecessor after the
                # runtime root is selected.  Structural validation still happens immediately;
                # direct startup with an un-custodied successor is rejected below.
                if capsule_plan.get("revision", {}).get("predecessor_sha256") is None:
                    raise
                validate_capsule_plan(dict(capsule_plan))
        except CapsuleContractError as exc:
            raise CapsuleRuntimeError(f"capsule authority is invalid: {exc}") from exc
        expected = next(
            (
                row
                for row in capsule_plan["capsules"]
                if row["capsule_id"] == capsule.get("capsule_id")
            ),
            None,
        )
        if expected is None or _canonical_capsule(expected) != _canonical_capsule(capsule):
            raise CapsuleRuntimeError("capsule does not exactly match the sealed plan")
        self.intent = copy.deepcopy(dict(intent_envelope))
        self.plan = copy.deepcopy(dict(capsule_plan))
        self.capsule = copy.deepcopy(dict(capsule))
        self.intent_sha256 = validate_intent_envelope(self.intent).digest
        self.plan_sha256 = validate_capsule_plan(self.plan).digest
        self.revision_id = self.plan["revision"]["revision_id"]
        self.capsule_id = _token(self.capsule["capsule_id"], "capsule_id")
        self.worktree = Path(worktree).expanduser().resolve()
        if not self.worktree.is_dir() or self.worktree.is_symlink():
            raise CapsuleRuntimeError("capsule worktree must be a physical directory")
        self.subject_base_oid = self.plan["subject"]["base_oid"]
        try:
            self.dependency_base = (
                validate_dependency_base(dependency_base) if dependency_base is not None else None
            )
        except CapsuleContractError as exc:
            raise CapsuleRuntimeError(f"dependency base is invalid: {exc}") from exc
        self.base_oid = (
            self.dependency_base["base_oid"]
            if self.dependency_base is not None
            else self.subject_base_oid
        )
        self.clock = clock
        if campaign_deadline is not None and (
            type(campaign_deadline) not in {int, float}
            or not math.isfinite(campaign_deadline)
            or campaign_deadline <= 0
        ):
            raise CapsuleRuntimeError("campaign deadline must be a finite positive timestamp")
        self.campaign_deadline = campaign_deadline
        self.branch = branch or _branch(self.worktree)
        try:
            base_tree_oid = _oid_from_git(self.worktree, f"{self.base_oid}^{{tree}}")
        except CapsuleRuntimeError as exc:
            raise CapsuleRuntimeError(
                "selected capsule base is not present in the worktree"
            ) from exc
        if self.dependency_base is None:
            if (
                hashlib.sha256(base_tree_oid.encode("ascii")).hexdigest()
                != self.plan["subject"]["base_tree_sha256"]
            ):
                raise CapsuleRuntimeError(
                    "capsule worktree tree does not match sealed subject base"
                )
        elif base_tree_oid != self.dependency_base["tree_oid"]:
            raise CapsuleRuntimeError(
                "capsule worktree tree does not match selected dependency base"
            )
        if self.dependency_base is not None:
            try:
                verify_dependency_base(
                    self.worktree,
                    self.dependency_base,
                    original_base_oid=self.subject_base_oid,
                )
            except CapsuleContractError as exc:
                raise CapsuleRuntimeError(
                    f"selected dependency base is not in sealed subject history: {exc}"
                ) from exc
        self._base_tree_oid = base_tree_oid
        self.lease = lease
        self.lease_store = lease_store
        self.lease_ttl_seconds = lease_ttl_seconds
        if type(recovery) is not bool:
            raise CapsuleRuntimeError("recovery must be a boolean")
        self.recovery = recovery
        if type(release_only) is not bool:
            raise CapsuleRuntimeError("release_only must be a boolean")
        if release_only and not recovery:
            raise CapsuleRuntimeError("release_only requires recovery")
        self.release_only = release_only
        # Set only when a release-only open tolerates a checkout ahead of a closed episode's
        # durable head; the lease's own release_reason records it. Never written to durable
        # state, and never used to advance head_oid.
        self.observed_late_head_oid: str | None = None
        if self.lease is None:
            raise CapsuleRuntimeError("capsule runtime requires a durable lease")
        # Construction observes; it does not spend. See _check_lease.
        self._check_lease(require_unexpired=False)
        computed_lease_sha = _json_digest(self._lease_material(self.lease))
        self._supplied_lease_sha256 = (
            _sha(lease_sha256, "lease_sha256") if lease_sha256 else computed_lease_sha
        )
        if self._supplied_lease_sha256 != computed_lease_sha:
            raise CapsuleRuntimeError("lease_sha256 does not match the full lease identity")
        self.lease_sha256 = computed_lease_sha
        if policy_sha256 is None:
            raise CapsuleRuntimeError("capsule runtime requires the sealed provider policy digest")
        self.policy_sha256 = _sha(policy_sha256, "policy_sha256")
        if transport is not None:
            raise CapsuleRuntimeError(
                "outcome transport injection is unsupported; inject the provider runner"
            )
        self.provider_runner = provider_runner
        # Approved by digest with the rest of the execution config; the launch
        # pre-authorizes the programs they name and nothing else.
        self.validation_commands = tuple(tuple(row) for row in validation_commands)
        self.provider_policy = provider_policy
        self.qualification_index = qualification_index
        if provider_policy is None:
            raise CapsuleRuntimeError("capsule runtime requires a validated provider policy")
        try:
            canonical_policy = _provider_policy_document(provider_policy)
            if validate_provider_policy(canonical_policy) != provider_policy:
                raise ProviderPolicyError("provider policy object is not canonical")
        except ProviderPolicyError as exc:
            raise CapsuleRuntimeError(f"provider policy is invalid: {exc}") from exc
        derived_policy_sha = _json_digest(canonical_policy)
        if self.policy_sha256 != derived_policy_sha:
            raise CapsuleRuntimeError(
                "policy_sha256 must match the canonical validated provider policy"
            )
        if self.policy_sha256 not in self.intent["campaign_envelope"]["policy_refs"]:
            raise CapsuleRuntimeError("provider policy digest is not authorized by the envelope")
        self.campaign_root = Path(campaign_root).expanduser().resolve() if campaign_root else None
        self.campaign_id = campaign_id
        self.claimant_id = claimant_id
        self.compatibility_repository_root = (
            Path(compatibility_repository_root).expanduser().resolve()
            if compatibility_repository_root
            else None
        )
        self.provider_output_root = (
            Path(provider_output_root).expanduser().resolve() if provider_output_root else None
        )
        self.provider_role_name = provider_role_name or "author"
        if (
            not isinstance(self.provider_role_name, str)
            or _TOKEN.fullmatch(self.provider_role_name) is None
        ):
            raise CapsuleRuntimeError("provider role name must be a canonical token")
        if not isinstance(receipt_role_name, str) or _TOKEN.fullmatch(receipt_role_name) is None:
            raise CapsuleRuntimeError("receipt role name must be a canonical token")
        self.receipt_role_name = receipt_role_name
        if self.intent["mode"] == "native_v2" and self.intent["approval"]["mode"] not in {
            "human_approved",
            "project_sealed",
        }:
            raise CapsuleRuntimeError(
                "native capsule execution requires human-approved or project-sealed intent"
            )
        if campaign_id is not None:
            _token(campaign_id, "campaign_id")
        if claimant_id is not None:
            _token(claimant_id, "claimant_id")
        if not all(
            (
                provider_policy,
                qualification_index,
                self.campaign_root,
                campaign_id,
                claimant_id,
                self.provider_output_root,
            )
        ) or (not self.recovery and self.compatibility_repository_root is None):
            raise CapsuleRuntimeError(
                "production capsule runtime requires provider policy, qualification, "
                "and custody roots"
            )
        if provider_runner is not None and not isinstance(provider_runner, Callable):
            raise CapsuleRuntimeError("provider_runner must be callable")
        self.source_contents = self._source_map(source_contents)
        if not isinstance(stable_charter, str) or not stable_charter.strip():
            raise CapsuleRuntimeError("stable P0 charter must be non-empty text")
        self.stable_charter = stable_charter
        self._run_mutex = threading.Lock()
        root = _private_directory(Path(state_root).expanduser(), create=True)
        self.state_root = root
        # Capsule identifiers are semantic tokens and may contain path separators.  Never use
        # them as filesystem names; the sealed identifier remains in every durable record.
        self.root = root / hashlib.sha256(self.capsule_id.encode("utf-8")).hexdigest()
        # A revision may replace an affected future capsule with a new identifier.  Activation
        # keeps the durable state chain at its original physical root, so recover it only when
        # the latest state already names this exact capsule and active plan.  Shared intent/base
        # or predecessor ancestry alone is insufficient: each capsule owns an independent chain.
        candidate_root = self.root
        if not any(candidate_root.glob("states/*")):
            matches: list[Path] = []
            for sibling in root.iterdir():
                if sibling == candidate_root or sibling.is_symlink() or not sibling.is_dir():
                    continue
                states = sibling / "states"
                if states.is_symlink() or not states.is_dir():
                    continue
                paths = sorted(states.iterdir())
                if not paths:
                    continue
                try:
                    latest = _read_json(paths[-1])
                except CapsuleRuntimeError:
                    continue
                if (
                    latest.get("intent_envelope_sha256") == self.intent_sha256
                    and latest.get("base_oid") == self.base_oid
                    and latest.get("capsule_id") == self.capsule_id
                    and latest.get("plan_sha256") == self.plan_sha256
                ):
                    matches.append(sibling)
            if len(matches) > 1:
                raise CapsuleRuntimeError("revised capsule runtime root is ambiguous")
            if matches:
                self.root = matches[0]
        _private_directory(self.root, create=True)
        self._states = _private_directory(self.root / "states", create=True)
        self._blobs = _private_directory(self.root / "blobs", create=True)
        self._lock_path = self.root / "runtime.lock"
        if not self._lock_path.exists():
            self._lock_path.touch(mode=0o600, exist_ok=True)
        self.objects = CapsuleObjectStore(self.root / "objects", create=True)
        # Keep the exact sealed authorities beside runtime state so recovery and block records
        # can cite resolvable content-addressed evidence without ambient lookup.
        if self.objects.put(self.intent) != self.intent_sha256:
            raise CapsuleRuntimeError("sealed intent custody digest mismatch")
        if self.objects.put(self.plan) != self.plan_sha256:
            raise CapsuleRuntimeError("sealed plan custody digest mismatch")
        if self.plan["revision"]["predecessor_sha256"] is not None:
            for store, active in self._activated_plans():
                if validate_capsule_plan(active).digest != self.plan_sha256:
                    continue
                cursor = active
                while True:
                    self.objects.put(cursor)
                    self.objects.put(
                        store.get(cursor["intent_envelope_sha256"], record_kind="intent_envelope")
                    )
                    predecessor = cursor["revision"]["predecessor_sha256"]
                    if predecessor is None:
                        break
                    cursor = store.get(predecessor, record_kind="capsule_plan")
                break
        self._predecessor_plan()
        self.custody = None
        if self.campaign_root is not None and self.provider_output_root is not None:
            self.custody = ProviderCustodyStore(
                self.campaign_root / "provider-custody",
                self.provider_output_root,
                qualification_index=self.qualification_index,
            )
        # A restart is valid when the checkout is clean at the last durable episode head.  The
        # immutable launch identity always remains the sealed subject base.
        try:
            if self.recovery:
                # Recovery observes unfinished files; it does not accept them as a candidate
                # or launch a provider. Preserve the same repository and commit fences.
                launch, _ = capture_close_repository(
                    self.worktree,
                    LaunchRepository(
                        self.lease.identity.repository_common_dir_sha256,
                        self.base_oid,
                        self._base_tree_oid,
                    ),
                    require_unchanged=False,
                )
            else:
                launch = capture_launch_repository(self.worktree)
        except ProviderReceiptError as exc:
            raise CapsuleRuntimeError(f"capsule launch checkout is not clean: {exc}") from exc
        if launch.repository_common_dir_sha256 != self.lease.identity.repository_common_dir_sha256:
            raise CapsuleRuntimeError("capsule checkout common directory differs from lease fence")
        if launch.head_oid != self.base_oid and not self._state_files_exist():
            raise CapsuleRuntimeError("capsule worktree HEAD does not match sealed subject base")
        self._launch = LaunchRepository(
            launch.repository_common_dir_sha256, self.base_oid, self._base_tree_oid, True
        )
        if launch.head_oid != self.base_oid:
            loaded_state = self._load_states(allow_active=self.recovery)
            if loaded_state is None:
                raise CapsuleRuntimeError("capsule checkout HEAD is not its durable episode head")
            durable_head = loaded_state[0].get("head_oid")
            active = loaded_state[0].get("active_episode")
            review = loaded_state[0].get("active_review")
            if durable_head != launch.head_oid:
                if self.release_only and (active is not None or review is not None):
                    # release_only exists to release an idle, conclusively closed capsule.
                    # Falling through to the open-episode tolerance below would let it also
                    # tolerate a HEAD that is still protecting live, unresolved spend -- a
                    # release-only caller must never coincide with that case.
                    raise CapsuleRuntimeError(
                        "a release-only open requires the episode and review boundaries "
                        "to already be closed"
                    )
                open_episode_ahead_of_its_own_base = (
                    self.recovery
                    and isinstance(active, Mapping)
                    and isinstance(active.get("launch_base_oid"), str)
                    and _is_ancestor(self.worktree, active["launch_base_oid"], launch.head_oid)
                )
                # A late worker can keep writing to the leased worktree after its episode was
                # durably closed `failed`/`blocked` (the controller that would have captured
                # its commit died first). release_only tolerates that HEAD, provably a
                # descendant of the durable head, without ever adopting it: durable_head is
                # never assigned launch.head_oid, here or anywhere below. The observed head is
                # only remembered in memory so the caller can fold it into the lease's own
                # release_reason text.
                closed_episode_late_commit = (
                    self.release_only
                    and active is None
                    and review is None
                    and loaded_state[0].get("state") in _RELEASE_ONLY_QUARANTINABLE_STATES
                    and _is_ancestor(self.worktree, durable_head, launch.head_oid)
                )
                if not (open_episode_ahead_of_its_own_base or closed_episode_late_commit):
                    raise CapsuleRuntimeError(
                        "capsule checkout HEAD is not its durable episode head"
                    )
                if closed_episode_late_commit:
                    self.observed_late_head_oid = launch.head_oid
        self._state, self._state_digest = self._load_or_initialize(recovery=self.recovery)

    @staticmethod
    def _source_map(
        sources: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        values = (
            sources.items()
            if isinstance(sources, Mapping)
            else ((row.get("source_id"), row) for row in sources)
        )
        result: dict[str, dict[str, Any]] = {}
        for source_id, row in values:
            if not isinstance(source_id, str) or not isinstance(row, Mapping):
                raise CapsuleRuntimeError("source contents must be objects with source_id")
            if _TOKEN.fullmatch(source_id) is None or source_id in result:
                raise CapsuleRuntimeError(
                    f"source_id is missing, invalid, or duplicated: {source_id!r}"
                )
            result[source_id] = copy.deepcopy(dict(row))
            result[source_id]["source_id"] = source_id
        return result

    def _lease_material(self, lease: LeaseRecord) -> dict[str, Any]:
        return {
            "identity": asdict(lease.identity),
            "claim_set": copy.deepcopy(lease.claim_set),
        }

    def _predecessor_plan(self) -> dict[str, Any] | None:
        """Resolve the entire sealed ancestry before supplying an immediate predecessor."""
        try:
            plan, intent = self.objects.resolve_plan(
                self.plan_sha256,
                expected_subject=self.plan["subject"],
                expected_revision_id=self.revision_id,
            )
            if validate_capsule_plan(self.plan).digest != self.plan_sha256 or (
                validate_intent_envelope(self.intent).digest != self.intent_sha256
                or validate_intent_envelope(intent).digest != self.intent_sha256
            ):
                raise CapsuleRuntimeError("plan ancestry differs from sealed runtime authority")
            digest = plan["revision"]["predecessor_sha256"]
            return None if digest is None else self.objects.get(digest, record_kind="capsule_plan")
        except CapsuleContractError as exc:
            raise CapsuleRuntimeError("plan lacks its exact durable ancestry") from exc

    def _activated_plans(self):
        """Derive activated plan heads from existing durable runtime snapshots."""
        seen = set()
        for sibling in sorted(self.root.parent.iterdir()):
            states = sibling / "states"
            if sibling.is_symlink() or not sibling.is_dir() or not states.is_dir():
                continue
            paths = sorted(states.iterdir())
            if not paths:
                continue
            path = paths[-1]
            match = _STATE_FILE.fullmatch(path.name)
            if path.is_symlink() or not path.is_file() or match is None:
                raise CapsuleRuntimeError("activated plan history is not canonical")
            if hashlib.sha256(path.read_bytes()).hexdigest() != match.group("digest"):
                raise CapsuleRuntimeError("activated plan history digest is invalid")
            state = _read_json(path)
            if state.get("intent_envelope_sha256") != self.intent_sha256:
                continue
            store = CapsuleObjectStore(sibling / "objects")
            for digest in (state.get("plan_sha256"), state.get("future_plan_sha256")):
                if digest is None or digest in seen:
                    continue
                seen.add(digest)
                plan = store.get(digest, record_kind="capsule_plan")
                plan, intent = store.resolve_plan(
                    digest,
                    expected_subject=self.plan["subject"],
                    expected_revision_id=plan["revision"]["revision_id"],
                )
                if validate_intent_envelope(intent).digest != self.intent_sha256:
                    raise CapsuleRuntimeError("activated plan changes sealed intent")
                yield store, plan

    def _check_revision_fence(self, *, activating: bool = False) -> None:
        for store, active in self._activated_plans():
            descendants = []
            cursor = active
            while validate_capsule_plan(cursor).digest != self.plan_sha256:
                descendants.append(cursor)
                previous = cursor["revision"]["predecessor_sha256"]
                if previous is None:
                    break
                cursor = store.get(previous, record_kind="capsule_plan")
            else:
                if activating and descendants:
                    raise CapsuleRuntimeError("use the active future plan before another revision")
                if any(
                    self.capsule_id in row["revision"]["superseded_capsule_ids"]
                    for row in descendants
                ):
                    raise CapsuleRuntimeError("capsule was superseded by an activated future plan")

    def _check_lease(self, *, require_unexpired: bool = True) -> None:
        """Verify the lease still fences this capsule.

        Expiry means others may reclaim the lease, not that its owner may no longer let go
        of it. Constructing a runtime to inspect or release custody therefore tolerates an
        expired lease; only spending under it requires an unexpired fence, and the
        heartbeat that precedes spending renews it anyway.

        An expired lease used to be unreleasable by any supported command: the stop worker
        built a runtime, this check refused, and the lease then blocked retire and setup
        with no ordinary way out.

        ``require_unexpired=False`` marks an inspect-or-release path, and such a path also
        tolerates an ``orphaned`` lease. The expiry sweep moves an expired active lease to
        ``orphaned``, so refusing that state here would restore the same deadlock one sweep
        later: custody counts an orphaned lease, which blocks retire, while every command
        that could release it refuses to build a runtime at all.
        """

        if self.lease is None:
            return
        allowed = {"active"} if require_unexpired else {"active", "orphaned"}
        if self.lease.state not in allowed:
            raise LeaseFenceLost(
                f"capsule lease is {self.lease.state}, not one of {sorted(allowed)}"
            )
        if require_unexpired and self.lease.expires_at <= self.clock():
            raise LeaseFenceLost(
                "capsule lease expired "
                f"{self.clock() - self.lease.expires_at:.0f}s ago and was not renewed"
            )
        identity = self.lease.identity
        if identity.base_oid != self.base_oid:
            raise LeaseFenceLost("capsule lease base does not match sealed subject base")
        if identity.branch != self.branch:
            raise LeaseFenceLost("capsule lease branch differs from capsule worktree")
        if identity.worktree_sha256 != worktree_sha256(self.worktree):
            raise LeaseFenceLost("capsule lease worktree fence differs from checkout")

    def _heartbeat(self) -> None:
        """Renew the fence, then prove it holds.

        The pre-check tolerates an expired lease because renewing it is this method's whole
        job, and the old docstring's claim that "the heartbeat that precedes spending renews
        it anyway" was false: the pre-check refused first, so an expired lease could never
        be renewed by its own owner. Measured on row 240 T2, a lease 1845 seconds past its
        TTL was counted as live custody by `retire` and rejected as dead by `recover`, and
        no supported command could resolve it.

        Nothing is loosened. The identity, base, branch and worktree fences run in the
        pre-check as before, the lease store still refuses a heartbeat whose identity or
        epoch another owner has taken, and the post-check is strict, so spending still
        proceeds only under a lease proven unexpired a moment ago.

        Defence in depth: every reachable caller of this method already sits behind
        `_candidate_guard(renew=True)` or `run_episode`'s own refusals, both of which already
        refuse a release-only runtime, so this raise is not reachable from any production path
        today. It exists so a future caller that reaches `_heartbeat` directly, bypassing
        those guards, cannot renew a lease a release-only open was never meant to hold open.
        """

        if self.release_only:
            raise CapsuleRuntimeError(
                "a release-only capsule runtime cannot spend or renew its lease"
            )
        self._check_lease(require_unexpired=False)
        if self.lease is not None and self.lease_store is not None:
            try:
                self.lease = self.lease_store.heartbeat(
                    self.lease.identity, ttl_seconds=self.lease_ttl_seconds, now=self.clock()
                )
            except CampaignLeaseError as exc:
                raise LeaseFenceLost(f"capsule lease fencing was lost: {exc}") from exc
        self._check_lease()

    @contextmanager
    def _exclusive(self):
        try:
            with self._lock_path.open("rb") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise CapsuleRuntimeError(f"cannot lock capsule runtime: {exc}") from exc

    def _state_files_exist(self) -> bool:
        return any(self._states.iterdir())

    def _state_paths(self) -> list[tuple[int, str, Path]]:
        rows: list[tuple[int, str, Path]] = []
        for path in self._states.iterdir():
            if not path.is_file() or path.is_symlink():
                raise CapsuleRuntimeError("capsule state directory contains a non-regular file")
            match = _STATE_FILE.fullmatch(path.name)
            if match is None:
                raise CapsuleRuntimeError(
                    f"capsule state directory contains unknown file {path.name!r}"
                )
            rows.append((int(match.group("sequence")), match.group("digest"), path))
        return sorted(rows)

    def _load_states(self, *, allow_active: bool = False) -> tuple[dict[str, Any], str] | None:
        rows = self._state_paths()
        if not rows:
            return None
        previous = "0" * 64
        state: dict[str, Any] | None = None
        digest = previous
        for expected_sequence, (sequence, filename_digest, path) in enumerate(rows):
            if sequence != expected_sequence:
                raise CapsuleRuntimeError("capsule state sequence is not contiguous")
            raw = path.read_bytes()
            observed_digest = hashlib.sha256(raw).hexdigest()
            if observed_digest != filename_digest:
                raise CapsuleRuntimeError("capsule state filename digest does not match bytes")
            value = _read_json(path)
            if (
                value.get("record_kind") != "capsule_runtime_state"
                or value.get("sequence") != sequence
            ):
                raise CapsuleRuntimeError("capsule runtime state identity changed")
            if value.get("previous_state_sha256") != previous:
                raise CapsuleRuntimeError("capsule runtime state chain is broken")
            if value.get("intent_envelope_sha256") != self.intent_sha256:
                raise CapsuleRuntimeError("capsule runtime state is bound to a different intent")
            state_plan_digest = value.get("plan_sha256")
            try:
                state_plan, state_intent = self.objects.resolve_plan(
                    state_plan_digest,
                    expected_subject=self.plan["subject"],
                    expected_revision_id=value.get("revision_id"),
                )
                if validate_intent_envelope(state_intent).digest != self.intent_sha256:
                    raise CapsuleContractError(
                        "historical capsule plan resolves to a different intent"
                    )
            except (CapsuleContractError, TypeError) as exc:
                raise CapsuleRuntimeError(
                    "capsule runtime state is missing its historical plan custody"
                ) from exc
            if value.get("revision_id") != state_plan["revision"]["revision_id"]:
                raise CapsuleRuntimeError("capsule runtime state revision does not match its plan")
            if not any(
                row.get("capsule_id") == value.get("capsule_id")
                for row in state_plan.get("capsules", [])
            ):
                raise CapsuleRuntimeError("capsule runtime state capsule is absent from its plan")
            operational = {
                "policy_sha256": self.policy_sha256,
                "campaign_deadline": self.campaign_deadline,
                "lease_sha256": self.lease_sha256,
                "lease_identity": asdict(self.lease.identity),
                "qualification_index_sha256": self.qualification_index.sha256,
                "provider_role": self.provider_role_name,
                "receipt_role": self.receipt_role_name,
                "campaign_id": self.campaign_id,
                "claimant_id": self.claimant_id,
                "branch": self.branch,
                "repository_common_dir_sha256": self.lease.identity.repository_common_dir_sha256,
                "worktree_sha256": self.lease.identity.worktree_sha256,
                "base_oid": self.base_oid,
                "dependency_base": self.dependency_base,
            }
            if any(value.get(key) != expected for key, expected in operational.items()):
                raise CapsuleRuntimeError(
                    "capsule runtime state operational fencing identity changed"
                )
            self._check_packet_custody(value)
            acceptance_bundles = value.get("acceptance_bundles", [])
            if not isinstance(acceptance_bundles, list):
                raise CapsuleRuntimeError("capsule acceptance bundle ledger is invalid")
            for bundle in acceptance_bundles:
                if not isinstance(bundle, Mapping):
                    raise CapsuleRuntimeError("capsule acceptance bundle entry is invalid")
                required = {
                    "review_id",
                    "packet_sha256",
                    "review_record_sha256",
                    "reviewer_receipt_sha256",
                    "candidate_sha256",
                }
                if set(bundle) - required or not required <= set(bundle):
                    raise CapsuleRuntimeError("capsule acceptance bundle binding is invalid")
                _token(bundle["review_id"], "acceptance bundle review_id")
                for field in required - {"review_id"}:
                    _sha(bundle[field], f"acceptance bundle {field}")
                self._check_blob(bundle["packet_sha256"], "acceptance review packet")
                self._check_blob(bundle["review_record_sha256"], "acceptance review record")
            proof_digest = value.get("acceptance_proof_sha256")
            if proof_digest is not None:
                self._check_blob(proof_digest, "acceptance proof")
            validation_digest = value.get("validation_receipt_sha256")
            if validation_digest is not None:
                self._check_validation_receipt(validation_digest)
            used_episode_ids = value.get("used_episode_ids")
            if (
                not isinstance(used_episode_ids, list)
                or any(_TOKEN.fullmatch(item) is None for item in used_episode_ids)
                or len(used_episode_ids) != len(set(used_episode_ids))
                or value.get("episode_count") != len(used_episode_ids)
            ):
                raise CapsuleRuntimeError("capsule episode identity ledger is invalid")
            previous = observed_digest
            state, digest = value, observed_digest
        if state is None:
            raise CapsuleRuntimeError("capsule state reconstruction produced no state")
        if (
            state.get("plan_sha256") != self.plan_sha256
            or state.get("revision_id") != self.revision_id
            or state.get("capsule_id") != self.capsule_id
        ):
            raise CapsuleRuntimeError(
                "capsule runtime state is not bound to the supplied active plan revision"
            )
        if state.get("reconciliation") is not None:
            try:
                validate_reconciliation_record(state["reconciliation"])
            except ReconciliationError as exc:
                raise CapsuleRuntimeError("capsule reconciliation custody is invalid") from exc
        if state.get("hil_request") is not None:
            _validate_hil_request(state["hil_request"])
        if state.get("state") == "awaiting_hil" and state.get("hil_request") is None:
            raise CapsuleRuntimeError("awaiting_hil state lacks an answerable HIL request")
        hil_answers = state.get("hil_answers", [])
        if not isinstance(hil_answers, list):
            raise CapsuleRuntimeError("capsule HIL answer ledger is invalid")
        for answer in hil_answers:
            _validate_hil_answer(answer)
        if state.get("active_episode") is not None and not allow_active:
            raise CapsuleRuntimeRecoveryError(
                "capsule has an unresolved episode_started spend boundary; explicit "
                "recovery is required"
            )
        if state.get("active_review") is not None and not allow_active:
            raise CapsuleRuntimeRecoveryError(
                "capsule has an unresolved review_started spend boundary; explicit "
                "review recovery is required"
            )
        return state, digest

    def _base_state(self, *, journal_sha256: str) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "record_kind": "capsule_runtime_state",
            "sequence": 0,
            "previous_state_sha256": "0" * 64,
            "state": "preflighted",
            "intent_envelope_sha256": self.intent_sha256,
            "plan_sha256": self.plan_sha256,
            "revision_id": self.revision_id,
            "capsule_id": self.capsule_id,
            "policy_sha256": self.policy_sha256,
            "lease_sha256": self.lease_sha256,
            "lease_identity": asdict(self.lease.identity),
            "qualification_index_sha256": self.qualification_index.sha256,
            "provider_role": self.provider_role_name,
            "receipt_role": self.receipt_role_name,
            "campaign_id": self.campaign_id,
            "claimant_id": self.claimant_id,
            "branch": self.branch,
            "repository_common_dir_sha256": self._launch.repository_common_dir_sha256,
            "worktree_sha256": worktree_sha256(self.worktree),
            "base_oid": self.base_oid,
            "dependency_base": copy.deepcopy(self.dependency_base),
            "head_oid": self.base_oid,
            "active_episode": None,
            "active_review": None,
            "used_episode_ids": [],
            "episode_count": 0,
            "repair_episodes": 0,
            "review_launches": 0,
            "review_rounds": 0,
            "review_prompt_bytes": 0,
            "review_refs": [],
            "review_findings": [],
            "reviewer_receipts": [],
            "acceptance_bundles": [],
            "acceptance_proof_sha256": None,
            "validation_receipt_sha256": None,
            "obligation_coverage": [],
            "invariants": [],
            "started_at": self.clock(),
            "campaign_deadline": self.campaign_deadline,
            "last_packet_sha256": None,
            "last_packet_prompt_sha256": None,
            "last_packet_prompt_blob_sha256": None,
            "last_packet_source_artifacts": [],
            "last_outcome": None,
            "state_delta": {},
            "provider_receipt_sha256s": [],
            "provider_receipts": [],
            "failure_receipt_sha256s": [],
            "validation": [],
            "discoveries": [],
            "unresolved_decisions": [],
            "next_action": "compile the first bounded execution packet",
            "journal_sha256": journal_sha256,
            "reconciliation": None,
            "hil_request": None,
            "hil_answers": [],
            "usage": {},
            "prompt_bytes": 0,
            "repeated_context_bytes": 0,
            "included_source_digests": [],
            "invariant_failures": 0,
        }

    def _load_or_initialize(self, *, recovery: bool = False) -> tuple[dict[str, Any], str]:
        # The lock covers the read/create decision as well as publication.  Two processes opening
        # a fresh capsule concurrently must never each create an independent spend chain.
        with self._exclusive():
            loaded = self._load_states(allow_active=recovery)
            if loaded is not None:
                return loaded
            journal = {
                "schema_version": "1",
                "record_kind": "capsule_journal",
                "canonical_algorithm": CANONICAL_ALGORITHM,
                "journal_id": f"journal.{self.capsule_id}",
                "intent_envelope_sha256": self.intent_sha256,
                "plan_sha256": self.plan_sha256,
                "revision_id": self.revision_id,
                "capsule_id": self.capsule_id,
                "subject": copy.deepcopy(self.plan["subject"]),
                "entries": [],
            }
            journal_digest = self.objects.put(journal)
            state = self._base_state(journal_sha256=journal_digest)
            state_digest = _create_only(
                self._states / f"{0:020d}-{hashlib.sha256(_canonical(state)).hexdigest()}.json",
                state,
            )
            self._state, self._state_digest = state, state_digest
            self._append_event("capsule_preflighted", evidence_refs=())
            self._persist_state({}, bootstrap=True)
            return self._state, self._state_digest

    @property
    def state(self) -> str:
        return self._state["state"]

    @property
    def journal(self) -> dict[str, Any]:
        return self.objects.get(self._state["journal_sha256"], record_kind="capsule_journal")

    @property
    def result(self) -> dict[str, Any] | None:
        digest = self._state.get("result_sha256")
        if not isinstance(digest, str):
            return None
        return self.objects.get(digest, record_kind="capsule_result")

    @property
    def acceptance_bundles(self) -> tuple[dict[str, Any], ...]:
        """Return durable review bundles in the shape consumed by the acceptance verifier.

        The packet and semantic assessment are read back from runtime blobs, while provider
        receipts come from the durable custody ledger.  No transient provider result is used.
        ``proof`` remains absent until the independent proof builder supplies one to
        :meth:`acceptance_bundle`.
        """

        bundles: list[dict[str, Any]] = []
        receipts = {
            _json_digest(receipt): copy.deepcopy(receipt)
            for receipt in self._state.get("reviewer_receipts") or []
        }
        proof_digest = self._state.get("acceptance_proof_sha256")
        persisted_proof = (
            self._read_json_blob(proof_digest, "acceptance proof")
            if isinstance(proof_digest, str)
            else None
        )
        for row in self._state.get("acceptance_bundles") or []:
            packet = self._read_json_blob(row["packet_sha256"], "acceptance review packet")
            review = self._read_json_blob(row["review_record_sha256"], "acceptance review record")
            reviewer = receipts.get(row["reviewer_receipt_sha256"])
            if reviewer is None:
                raise CapsuleRuntimeError("acceptance bundle reviewer receipt is unavailable")
            bundle: dict[str, Any] = {
                "intent_envelope": copy.deepcopy(self.intent),
                "capsule_plan": copy.deepcopy(self.plan),
                "capsule": copy.deepcopy(self.capsule),
                "packet": packet,
                "candidate_worktree": self.worktree,
                "author_receipts": copy.deepcopy(self._state.get("provider_receipts") or []),
                "reviewer_receipts": [reviewer],
                "review_receipts": [review],
                "validation_state_root": self._blobs,
                "validation_receipt_sha256": self._state.get("validation_receipt_sha256"),
                "proof": copy.deepcopy(persisted_proof),
                "custody": self.custody,
                "policy_sha256": self.policy_sha256,
                "provider_policy": self.provider_policy,
                "qualification_index": self.qualification_index,
                "previous_plan": self._predecessor_plan(),
                "dependency_base": copy.deepcopy(self.dependency_base),
            }
            bundles.append(bundle)
        return tuple(bundles)

    def acceptance_bundle(
        self, proof: Mapping[str, Any] | None = None, *, review_id: str | None = None
    ) -> dict[str, Any]:
        """Return one durable acceptance bundle, optionally attaching a validated proof."""

        bundles = list(self.acceptance_bundles)
        if review_id is not None:
            _token(review_id, "review_id")
            rows = [
                row
                for row in self._state.get("acceptance_bundles") or []
                if row.get("review_id") == review_id
            ]
            if len(rows) != 1:
                raise CapsuleRuntimeError("requested acceptance review is unavailable")
            index = next(
                index
                for index, row in enumerate(self._state.get("acceptance_bundles") or [])
                if row.get("review_id") == review_id
            )
            bundle = bundles[index]
        elif len(bundles) == 1:
            bundle = bundles[0]
        elif not bundles:
            raise CapsuleRuntimeError("capsule has no durable approved review bundle")
        else:
            # Only the latest candidate's approved review can establish acceptance.  Historical
            # rejected rounds remain in the journal and review_refs but are not proof inputs.
            current = self.result or {}
            current_candidate = current.get("candidate")
            candidate_fields = ("base_oid", "head_oid", "tree_oid", "patch_sha256", "clean")
            matching = [
                row
                for row in bundles
                if {key: row["packet"].get("candidate", {}).get(key) for key in candidate_fields}
                == current_candidate
                and row["review_receipts"][0].get("verdict") == "approve"
            ]
            if not matching:
                raise CapsuleRuntimeError("capsule has no current independent approval")
            packets = {_json_digest(row["packet"]) for row in matching}
            if len(packets) != 1:
                raise CapsuleRuntimeError("current approvals refer to different review packets")
            bundle = matching[0]
            bundle["reviewer_receipts"] = [
                receipt for row in matching for receipt in row["reviewer_receipts"]
            ]
            bundle["review_receipts"] = [
                receipt for row in matching for receipt in row["review_receipts"]
            ]
        if proof is not None:
            bundle = copy.deepcopy(bundle)
            bundle["proof"] = copy.deepcopy(dict(proof))
        return bundle

    def validate_candidate(
        self,
        validation_receipt_sha256: str,
        *,
        artifact_refs: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._candidate_operation():
            return self._validate_candidate(validation_receipt_sha256, artifact_refs=artifact_refs)

    def _validate_candidate(
        self,
        validation_receipt_sha256: str,
        *,
        artifact_refs: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Reopen an actual validation receipt and publish its pass evidence for this candidate.

        Command execution belongs to ``execute_capsule_validation``.  This runtime boundary only
        accepts its content addressed receipt after the verifier reopens command and output
        custody against the exact current Git candidate.
        """

        if self.state not in {"candidate_ready", "locally_repairing"}:
            raise CapsuleRuntimeError(f"capsule state {self.state!r} cannot validate a candidate")
        receipts = self._state.get("provider_receipts") or []
        try:
            candidate, changed_paths = self._candidate(receipts)
        except Exception as exc:
            raise CapsuleRuntimeError(
                f"candidate custody is unavailable for validation: {exc}"
            ) from exc
        if candidate is None:
            raise CapsuleRuntimeError("candidate validation requires a clean cumulative candidate")
        try:
            from bearhug.campaign.capsule_validation import (
                CapsuleValidationError,
                verify_capsule_validation,
            )

            record = verify_capsule_validation(
                state_root=self._blobs,
                receipt_sha256=validation_receipt_sha256,
                candidate_worktree=self.worktree,
                candidate=candidate,
                profiles=self.capsule["validation_profiles"],
            )
        except (CapsuleValidationError, TypeError, ValueError) as exc:
            raise CapsuleRuntimeError(
                f"candidate validation evidence is unavailable: {exc}"
            ) from exc
        if record.get("status") != "pass":
            raise CapsuleRuntimeError("candidate validation did not pass")
        artifact_refs = dict(artifact_refs or {})
        required_artifacts = set(self.capsule["completion_boundary"]["required_artifact_refs"])
        outputs = {
            row[f"{stream}_sha256"] for row in record["commands"] for stream in ("stdout", "stderr")
        }
        if (
            not set(artifact_refs) <= required_artifacts
            or not set(artifact_refs.values()) <= outputs
        ):
            raise CapsuleRuntimeError(
                "artifact bindings must name declared actual validation outputs"
            )
        validation = [
            {
                "profile_id": profile["profile_id"],
                "status": "pass",
                "evidence_refs": [validation_receipt_sha256],
            }
            for profile in self.capsule["validation_profiles"]
        ]
        self._heartbeat()
        self._transition(
            "validation_observed",
            evidence_refs=(validation_receipt_sha256,),
            state="candidate_ready",
            validation=validation,
            validation_receipt_sha256=validation_receipt_sha256,
            validation_artifact_refs=artifact_refs,
            review_round_packet_sha256=(
                self._state.get("review_round_packet_sha256")
                if self._state.get("validation_receipt_sha256") == validation_receipt_sha256
                else None
            ),
            result_sha256=self._state.get("result_sha256"),
            last_outcome="candidate_ready",
            next_action="submit the independently reviewed candidate for result verification",
        )
        self._publish_result(candidate, changed_paths, [validation_receipt_sha256])
        return copy.deepcopy(record)

    # Alias used by callers that treat validation as a rerun operation.
    record_validation = validate_candidate

    def _append_event(self, event_kind: str, *, evidence_refs: Iterable[str]) -> None:
        if event_kind not in {
            "capsule_preflighted",
            "episode_started",
            "episode_completed",
            "grounding_conflict",
            "validation_started",
            "validation_observed",
            "discovery_recorded",
            "scope_pressure_detected",
            "reconciliation_requested",
            "reconciliation_completed",
            "plan_revision_activated",
            "candidate_ready",
            "review_started",
            "review_finding",
            "repair_started",
            "review_accepted",
            "capsule_completed",
            "phase_validated",
            "hil_requested",
            "hil_answered",
            "capsule_blocked",
        }:
            raise CapsuleRuntimeError(f"unsupported capsule event {event_kind!r}")
        current = self.journal
        refs = sorted({_sha(value, "event evidence reference") for value in evidence_refs})
        occurred = (
            datetime.fromtimestamp(self.clock(), UTC)
            .replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        entries = list(current["entries"])
        entries.append(
            {
                "sequence": len(entries) + 1,
                "event_id": f"event.{len(entries) + 1}",
                "event_kind": event_kind,
                "occurred_at": occurred,
                "evidence_refs": refs,
            }
        )
        successor = copy.deepcopy(current)
        successor["entries"] = entries
        try:
            append_capsule_journal(current, successor)
        except CapsuleContractError as exc:
            raise CapsuleRuntimeError(f"semantic journal append is invalid: {exc}") from exc
        journal_digest = self.objects.put(successor)
        self._state["journal_sha256"] = journal_digest

    def _persist_state(self, updates: Mapping[str, Any], *, bootstrap: bool = False) -> None:
        # Defence in depth, matching `_heartbeat`'s own guard: every reachable caller outside
        # `_load_or_initialize`'s own bootstrap already sits behind `_candidate_guard`'s or
        # `run_episode`'s refusals for a release-only runtime, so this is not reachable from
        # any other production path today. `bootstrap=True` is the one deliberate exception:
        # a release-only open of a capsule with no durable state yet (never spent, so the
        # `failed`/`blocked` tolerance never even applies) still needs its own initial
        # `capsule_preflighted` record, exactly as any other open does -- this establishes
        # that the capsule exists; it is not spending or renewing anything.
        if self.release_only and not bootstrap:
            raise CapsuleRuntimeError(
                "a release-only capsule runtime cannot append durable state"
            )
        state = copy.deepcopy(self._state)
        state.update(copy.deepcopy(dict(updates)))
        state["sequence"] = self._state["sequence"] + 1
        state["previous_state_sha256"] = self._state_digest
        digest = hashlib.sha256(_canonical(state)).hexdigest()
        _create_only(self._states / f"{state['sequence']:020d}-{digest}.json", state)
        self._state, self._state_digest = state, digest

    def _latest_state_digest(self) -> str | None:
        rows = self._state_paths()
        if not rows:
            return None
        sequence, filename_digest, path = rows[-1]
        if sequence != self._state["sequence"]:
            raise CapsuleRuntimeError("capsule state sequence changed concurrently")
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != filename_digest:
            raise CapsuleRuntimeError("capsule state filename digest does not match bytes")
        return observed

    def _put_blob(self, raw: bytes) -> str:
        """Persist exact prompt/source bytes without treating them as executable authority."""

        if not isinstance(raw, bytes) or len(raw) > 4 * 1024 * 1024:
            raise CapsuleRuntimeError("runtime blob is missing or exceeds the 4 MiB bound")
        digest = hashlib.sha256(raw).hexdigest()
        _create_blob_only(self._blobs / f"{digest}.bin", raw)
        return digest

    def _read_blob(self, digest: str, label: str = "runtime blob") -> bytes:
        """Read one immutable runtime blob after checking its content address."""

        digest = _sha(digest, label)
        path = self._blobs / f"{digest}.bin"
        if path.is_symlink() or not path.is_file():
            raise CapsuleRuntimeError(f"{label} is missing from runtime custody")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise CapsuleRuntimeError(f"{label} cannot be read from runtime custody") from exc
        if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
            raise CapsuleRuntimeError(f"{label} bytes are tampered")
        return raw

    def _read_json_blob(self, digest: str, label: str = "runtime JSON blob") -> dict[str, Any]:
        raw = self._read_blob(digest, label)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapsuleRuntimeError(f"{label} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise CapsuleRuntimeError(f"{label} is not a JSON object")
        return value

    def _check_validation_receipt(self, digest: Any) -> None:
        self._check_blob(digest, "validation receipt")

    def _check_blob(self, digest: Any, label: str) -> None:
        digest = _sha(digest, label)
        path = self._blobs / f"{digest}.bin"
        if path.is_symlink() or not path.is_file():
            raise CapsuleRuntimeError(f"{label} is missing from runtime custody")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise CapsuleRuntimeError(f"{label} cannot be read from runtime custody") from exc
        if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
            raise CapsuleRuntimeError(f"{label} bytes are tampered")

    def _check_packet_custody(self, value: Mapping[str, Any]) -> None:
        if value.get("grounding_sha256") is not None:
            self._check_blob(value["grounding_sha256"], "reconciliation grounding blob")
        prompt_digest = value.get("last_packet_prompt_blob_sha256")
        if prompt_digest is not None:
            self._check_blob(prompt_digest, "packet prompt blob")
            if value.get("last_packet_prompt_sha256") != prompt_digest:
                raise CapsuleRuntimeError("packet prompt digest does not match its custody blob")
        artifacts = value.get("last_packet_source_artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 256:
            raise CapsuleRuntimeError("packet source custody manifest is invalid")
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise CapsuleRuntimeError("packet source custody entry is invalid")
            self._check_blob(artifact.get("raw_blob_sha256"), "packet source raw blob")
            self._check_blob(artifact.get("rendered_blob_sha256"), "packet source rendered blob")
            if artifact.get("source_sha256") != artifact.get("raw_blob_sha256"):
                raise CapsuleRuntimeError("packet source digest does not match raw custody")
            if artifact.get("rendered_sha256") != artifact.get("rendered_blob_sha256"):
                raise CapsuleRuntimeError("packet source digest does not match rendered custody")

    def _transition(
        self, event_kind: str, *, evidence_refs: Iterable[str] = (), **updates: Any
    ) -> None:
        with self._exclusive():
            if self._latest_state_digest() != self._state_digest:
                raise CapsuleRuntimeError(
                    "capsule runtime state changed concurrently; reload before advancing"
                )
            self._append_event(event_kind, evidence_refs=evidence_refs)
            self._persist_state(updates)

    def _source_content(self, row: Mapping[str, Any]) -> bytes:
        content = row.get("content", row.get("text"))
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        path = row.get("path")
        if isinstance(path, str) and path and not Path(path).is_absolute():
            target = self.worktree / path
            try:
                value = target.read_bytes()
            except OSError as exc:
                raise CapsuleRuntimeError(f"packet source {path!r} is unavailable") from exc
            if len(value) > 4 * 1024 * 1024:
                raise CapsuleRuntimeError("packet source exceeds the 4 MiB bound")
            return value
        return b""

    def compile_packet(self, episode_id: str | None = None) -> tuple[dict[str, Any], str, str]:
        episode_number = self._state["episode_count"] + 1
        current_id = episode_id or f"episode.{episode_number}"
        _token(current_id, "episode_id")
        delta = copy.deepcopy(self._state.get("state_delta") or {})
        explicit = []
        for source_id, source in self.source_contents.items():
            item = copy.deepcopy(source)
            item["source_id"] = source_id
            content = self._source_content(source)
            item["content"] = content
            # The packet renderer computes source custody from exact bytes.  A caller may still
            # supply a bounded projection and tier/selection alongside the bytes.
            explicit.append(item)
        try:
            rendered_packet = render_execution_packet(
                packet_id=f"packet.{self.capsule_id}.{current_id}",
                intent_envelope=self.intent,
                capsule_plan=self.plan,
                capsule=self.capsule,
                episode_id=current_id,
                subject=self.plan["subject"],
                policy_sha256=self.policy_sha256,
                lease_sha256=self.lease_sha256,
                sources=explicit,
                state_delta=delta,
                previous_plan=self._predecessor_plan(),
            )
        except Exception as exc:
            raise CapsuleRuntimeError(f"execution packet rendering failed: {exc}") from exc
        if delta and not any(
            item.source_id == f"state-delta.{current_id}"
            and item.metadata["selection"] == "included"
            for item in rendered_packet.source_artifacts
        ):
            raise CapsuleRuntimeError("required fresh-context state delta does not fit the packet")
        packet = rendered_packet.execution_packet
        packet_digest = self.objects.put(packet)
        prompt = rendered_packet.prompt
        prompt_bytes = rendered_packet.prompt_bytes
        self._last_packet_artifacts = []
        for artifact in rendered_packet.source_artifacts:
            raw_digest = self._put_blob(artifact.raw_bytes)
            rendered_digest = self._put_blob(artifact.rendered_bytes)
            self._last_packet_artifacts.append(
                {
                    "source_id": artifact.source_id,
                    "source_sha256": artifact.source_sha256,
                    "raw_blob_sha256": raw_digest,
                    "rendered_sha256": artifact.rendered_sha256,
                    "rendered_blob_sha256": rendered_digest,
                    "selection": artifact.selection,
                    "tier": artifact.tier,
                    "source_form": artifact.source_form,
                    "full_bytes": len(artifact.raw_bytes),
                    "rendered_bytes": len(artifact.rendered_bytes),
                    "estimated_tokens": artifact.estimated_tokens,
                    "full_estimated_tokens": artifact.full_estimated_tokens,
                }
            )
        self._last_packet_prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
        self._last_packet_prompt_blob_sha256 = self._put_blob(prompt_bytes)
        return packet, prompt, packet_digest

    def _episode_result(self, raw: Any) -> tuple[dict[str, Any], Any | None]:
        provider_run = getattr(raw, "provider_run", None)
        if isinstance(raw, Mapping):
            provider_run = raw.get("provider_run", raw.get("run"))
            result = dict(raw)
        elif provider_run is not None:
            try:
                failure = provider_terminal_failure(provider_run)
                result = (
                    {"outcome": "failed", "changed_facts": [failure], "next_action": failure}
                    if failure is not None
                    else strict_final_json(provider_run)
                )
            except ProviderFinalOutputError as exc:
                message = f"provider final outcome is not strict JSON: {exc}"
                result = {"outcome": "failed", "changed_facts": [message], "next_action": message}
        else:
            raise CapsuleRuntimeError(
                "episode transport returned neither a result object nor provider run"
            )
        if "outcome" not in result and "status" in result:
            result["outcome"] = result["status"]
        outcome = result.get("outcome")
        if outcome not in _OUTCOMES:
            raise CapsuleRuntimeError("episode result has unsupported outcome")
        return result, provider_run

    def _custody_refs(
        self, result: Mapping[str, Any], provider_run: Any | None
    ) -> tuple[str | None, str | None, dict[str, Any] | None]:
        provider_receipt: dict[str, Any] | None = None
        provider_digest = None
        if provider_run is not None:
            try:
                provider_receipt = validate_provider_receipt(dict(provider_run.receipt))
            except (AttributeError, TypeError, ProviderReceiptError) as exc:
                raise CapsuleRuntimeError(
                    f"provider run has no valid success receipt: {exc}"
                ) from exc
            if self.custody is None:
                raise CapsuleRuntimeError("provider run cannot be used without a custody store")
            try:
                custody = self.custody.record_run(provider_run)
            except (ProviderCustodyError, ProviderReceiptError) as exc:
                raise CapsuleRuntimeError(f"provider receipt custody failed: {exc}") from exc
            provider_digest = custody["receipt_sha256"]
        elif result.get("provider_receipt") is not None:
            value = result.get("provider_receipt")
            if not isinstance(value, Mapping):
                raise CapsuleRuntimeError("provider_receipt must be a closed provider receipt")
            try:
                provider_receipt = validate_provider_receipt(dict(value))
            except ProviderReceiptError as exc:
                raise CapsuleRuntimeError(f"provider receipt is invalid: {exc}") from exc
            provider_digest = _json_digest(provider_receipt)
            declared = result.get("provider_receipt_sha256")
            if (
                declared is not None
                and _sha(declared, "provider_receipt_sha256") != provider_digest
            ):
                raise CapsuleRuntimeError("provider receipt digest does not match its bytes")
            if self.custody is None:
                raise CapsuleRuntimeError("provider receipt cannot be accepted without custody")
            try:
                self.custody.validate("episode", provider_receipt)
            except ProviderCustodyError as exc:
                raise CapsuleRuntimeError(
                    f"provider receipt custody validation failed: {exc}"
                ) from exc
        elif result.get("provider_receipt_sha256") is not None:
            raise CapsuleRuntimeError(
                "provider receipt digest requires the exact receipt and custody evidence"
            )
        failure = result.get("failure_receipt")
        failure_digest = result.get("failure_receipt_sha256")
        if failure is not None:
            try:
                validate_provider_failure_receipt(dict(failure))
            except ProviderFailureReceiptError as exc:
                raise CapsuleRuntimeError(f"provider failure receipt is invalid: {exc}") from exc
            failure_digest = _json_digest(failure)
        if failure_digest is not None:
            _sha(failure_digest, "failure_receipt_sha256")
        return provider_digest, failure_digest, provider_receipt

    @staticmethod
    def _validation(result: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        rows = result.get("validation", ())
        if rows is None:
            return ()
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            raise CapsuleRuntimeError("episode validation must be an array")
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise CapsuleRuntimeError("episode validation row must be an object")
            profile = row.get("profile_id", "profile.episode")
            status = row.get("status", "unavailable")
            if (
                not isinstance(profile, str)
                or _TOKEN.fullmatch(profile) is None
                or status not in {"pass", "fail", "unavailable"}
            ):
                raise CapsuleRuntimeError("episode validation row has invalid profile or status")
            refs = row.get("evidence_refs", [])
            if (
                not isinstance(refs, list)
                or any(_SHA256.fullmatch(item) is None for item in refs if isinstance(item, str))
                or any(not isinstance(item, str) for item in refs)
            ):
                raise CapsuleRuntimeError("episode validation evidence_refs are invalid")
            normalized.append(
                {"profile_id": profile, "status": status, "evidence_refs": sorted(set(refs))}
            )
        return tuple(normalized)

    def _state_delta(
        self,
        result: Mapping[str, Any],
        *,
        provider_digest: str | None,
        candidate: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        def strings(field: str) -> list[str]:
            value = result.get(field, [])
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise CapsuleRuntimeError(f"episode {field} must be a string array")
            return list(dict.fromkeys(value))

        changed = strings("changed_facts")
        discoveries = strings("discoveries")
        decisions = strings("unresolved_decisions")
        next_action = result.get("next_action")
        if next_action is not None and (
            not isinstance(next_action, str) or not next_action.strip()
        ):
            raise CapsuleRuntimeError("episode next_action must be non-empty text or null")
        git = {
            "base_oid": self.base_oid,
            "head_oid": _oid_from_git(self.worktree, "HEAD"),
            "tree_oid": _oid_from_git(self.worktree, "HEAD^{tree}"),
        }
        candidate_delta = None
        if candidate is not None:
            candidate_delta = {
                field: candidate[field]
                for field in ("base_oid", "head_oid", "tree_oid", "patch_sha256", "clean")
                if field in candidate
            }
        return {
            "changed_facts": changed,
            "candidate": candidate_delta,
            "git": git,
            "validation_state_changes": [
                f"{row['profile_id']}={row['status']}" for row in self._validation(result)
            ],
            "discoveries": discoveries,
            "unresolved_decisions": decisions,
            "next_action": next_action,
        }

    def _candidate(
        self, provider_receipts: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
        try:
            return capture_capsule_candidate(
                self.worktree,
                self._launch,
                provider_receipts,
                self.capsule["mutation_envelope"]["path_prefixes"],
                dependency_base=self.dependency_base,
                original_base_oid=self.subject_base_oid,
            )
        except CapsuleContractError as exc:
            raise CapsuleRuntimeError(
                f"cannot capture cumulative capsule candidate: {exc}"
            ) from exc

    def _budget_guard(self) -> EpisodeOutcome | None:
        risk = self.intent["campaign_envelope"]["risk"]
        maximum_repairs = risk.get("max_repair_episodes")
        reason: str | None = None
        if (
            type(maximum_repairs) is int
            and self._state["repair_episodes"] >= maximum_repairs
            and self._state.get("last_outcome") == "local_repair_required"
        ):
            reason = "approved repair episode budget is exhausted"
        maximum_seconds = self.intent["campaign_envelope"]["budget"].get("max_seconds")
        if reason is None and (
            self.intent["campaign_envelope"]["budget"].get("max_provider_tokens") is not None
            or self.intent["campaign_envelope"]["budget"].get("max_provider_spend_cents")
            is not None
        ):
            reason = "approved provider token or spend limit cannot be measured at this boundary"
        if (
            reason is None
            and type(maximum_seconds) is int
            and self.clock() - self._state["started_at"] >= maximum_seconds
        ):
            reason = "approved campaign time budget is exhausted"
        if self.campaign_deadline is not None and self.clock() >= self.campaign_deadline:
            reason = "approved cumulative campaign time budget is exhausted"
        if reason is None:
            return None
        self._transition(
            "capsule_blocked",
            evidence_refs=(self.intent_sha256, self.plan_sha256),
            state="blocked",
            active_episode=None,
            last_outcome="blocked",
            state_delta={
                "changed_facts": [reason],
                "candidate": None,
                "git": None,
                "validation_state_changes": [],
                "discoveries": [],
                "unresolved_decisions": [],
                "next_action": "obtain a fresh approved capsule or human disposition",
            },
            next_action="obtain a fresh approved capsule or human disposition",
        )
        return EpisodeOutcome("blocked", "episode.budget", None)

    def _provider_timeout(self) -> float:
        remaining = float(self.lease_ttl_seconds)
        if self.campaign_deadline is not None:
            remaining = min(remaining, self.campaign_deadline - self.clock())
        maximum_seconds = self.intent["campaign_envelope"]["budget"].get("max_seconds")
        if type(maximum_seconds) is int:
            remaining = min(remaining, maximum_seconds - (self.clock() - self._state["started_at"]))
        if remaining <= 0:
            raise CapsuleRuntimeError("approved campaign time budget is exhausted")
        return remaining

    def _select_provider(self) -> tuple[str, Any, Any]:
        assert self.provider_policy is not None and self.qualification_index is not None
        selected_role = self.provider_role_name
        try:
            selected = provider_role(self.provider_policy, selected_role)
        except ProviderPolicyError as exc:
            raise CapsuleRuntimeError(f"provider role selection is invalid: {exc}") from exc
        selection = selected.provider
        if selection not in {"claude", "codex"}:
            raise CapsuleRuntimeError("capsule selected an unsupported provider")
        try:
            qualified = self.qualification_index.require(selection)
        except ProviderQualificationIndexError as exc:
            raise CapsuleRuntimeError(f"provider qualification is unavailable: {exc}") from exc
        needs = self.capsule.get("provider_capability_needs", [])
        if self.intent["mode"] == "native_v2":
            unsupported = sorted(set(needs) - set(selected.required_capabilities))
            if unsupported:
                raise CapsuleRuntimeError(
                    f"selected provider role cannot satisfy capsule capabilities: {unsupported}"
                )
            if "native-continuation" in needs:
                raise CapsuleRuntimeError("native continuation is unqualified at this boundary")
        return selected_role, selected, qualified

    def _validate_policy_binding(self) -> None:
        assert self.provider_policy is not None
        try:
            canonical_policy = _provider_policy_document(self.provider_policy)
            if validate_provider_policy(canonical_policy) != self.provider_policy:
                raise ProviderPolicyError("provider policy object is not canonical")
        except ProviderPolicyError as exc:
            raise CapsuleRuntimeError(f"provider policy is invalid: {exc}") from exc
        if _json_digest(canonical_policy) != self.policy_sha256:
            raise CapsuleRuntimeError("provider policy changed after capsule binding")
        if self.policy_sha256 not in self.intent["campaign_envelope"]["policy_refs"]:
            raise CapsuleRuntimeError("provider policy is no longer authorized by the envelope")

    def _validate_authority_binding(self) -> None:
        """Reject replacing the in-memory sealed records without an explicit revision."""

        try:
            intent_result = validate_intent_envelope(self.intent)
            plan_result = validate_capsule_plan(
                self.plan, intent_envelope=self.intent, previous_plan=self._predecessor_plan()
            )
        except CapsuleContractError as exc:
            raise CapsuleRuntimeError(f"sealed capsule authority is invalid: {exc}") from exc
        if intent_result.digest != self.intent_sha256 or plan_result.digest != self.plan_sha256:
            raise CapsuleRuntimeError(
                "sealed capsule authority changed; explicit revision activation is required"
            )
        expected = next(
            (row for row in self.plan["capsules"] if row["capsule_id"] == self.capsule_id), None
        )
        if expected is None or _canonical_capsule(expected) != _canonical_capsule(self.capsule):
            raise CapsuleRuntimeError("sealed capsule binding changed")
        self._check_revision_fence()

    def _validate_receipt_chain_custody(self, receipts: Sequence[Mapping[str, Any]]) -> None:
        if not receipts:
            return
        if self.custody is None:
            raise CapsuleRuntimeError("provider receipt chain cannot run without custody")
        assert self.provider_policy is not None
        try:
            selected = provider_role(self.provider_policy, self.provider_role_name)
        except ProviderPolicyError as exc:
            raise CapsuleRuntimeError(f"provider role selection is invalid: {exc}") from exc
        expected_provider = {
            "codex": "openai-codex",
            "claude": "anthropic-claude",
        }.get(selected.provider)
        if expected_provider is None:
            raise CapsuleRuntimeError("provider receipt chain selected an unsupported provider")
        for value in receipts:
            try:
                receipt = validate_provider_receipt(dict(value))
            except (TypeError, ProviderReceiptError) as exc:
                raise CapsuleRuntimeError(f"prior provider receipt is invalid: {exc}") from exc
            if (
                receipt["provider"] != expected_provider
                or receipt["role"] != self.receipt_role_name
            ):
                raise CapsuleRuntimeError("prior provider receipt does not match sealed role")
            try:
                self.custody.validate("prior episode", receipt)
            except ProviderCustodyError as exc:
                raise CapsuleRuntimeError(f"prior provider custody changed: {exc}") from exc

    def _invoke(self, packet: dict[str, Any], prompt: str) -> Any:
        assert self.provider_policy is not None and self.qualification_index is not None
        assert (
            self.campaign_root is not None
            and self.campaign_id is not None
            and self.claimant_id is not None
        )
        assert (
            self.compatibility_repository_root is not None and self.provider_output_root is not None
        )
        selected_role, selected, qualified = self._select_provider()
        from bearhug.campaign.author import _default_runner, run_campaign_author

        if self.lease is None:
            raise CapsuleRuntimeError("production capsule episodes require a durable lease")
        launch_base = _oid_from_git(self.worktree, "HEAD")
        episode_id = packet["episode_id"]
        output = (
            self.provider_output_root
            / hashlib.sha256(self.capsule_id.encode("utf-8")).hexdigest()
            / hashlib.sha256(episode_id.encode("utf-8")).hexdigest()
        )
        options: dict[str, Any] = {
            "validation_commands": self.validation_commands,
            "campaign_root": self.campaign_root,
            "campaign_lease": self.lease,
            "campaign_id": self.campaign_id,
            "claimant_id": self.claimant_id,
            "worktree": self.worktree,
            "branch": self.branch,
            "base_oid": launch_base,
            "lease_base_oid": self.base_oid,
            "path_prefixes": self.capsule["mutation_envelope"]["path_prefixes"],
            "semantic_resources": self.capsule["mutation_envelope"]["semantic_resources"],
            "prompt": prompt,
            "provider_policy": self.provider_policy,
            "role_name": selected_role,
            "compatibility_policy": qualified.compatibility_policy(),
            "compatibility_repository_root": qualified.bundle_root,
            "adapter_version": qualified.adapter_version,
            "provider_output_dir": output,
            "receipt_role": self.receipt_role_name,
            "executable": str(qualified.executable),
            # The campaign-layer anchor, compared against the
            # evidence record's own observed digest in author.py's cross-check block, at the
            # point the campaign actually spends money.
            "executable_sha256": qualified.executable_sha256,
            "timeout_s": self._provider_timeout(),
            # Qualification binds these exact installed policy files.  Passing their digests
            # through the author wrapper lets an attested turn become promotion eligible; an
            # unqualified or missing runtime attestation still remains ineligible.
            "settings_sha256": qualified.settings_sha256,
            "rules_sha256": qualified.rules_sha256,
            "settings_path": qualified.settings_path,
            "rules_path": qualified.rules_path,
            "episode_id": episode_id,
        }
        runner = self.provider_runner or _default_runner
        stop = threading.Event()
        heartbeat_errors: list[CapsuleRuntimeError] = []

        def heartbeat_loop() -> None:
            interval = max(0.05, min(self.lease_ttl_seconds / 3.0, 30.0))
            while not stop.wait(interval):
                try:
                    self._heartbeat()
                except CapsuleRuntimeError as exc:
                    heartbeat_errors.append(exc)
                    return

        heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat_thread.start()

        def fenced_runner(
            provider: str, value: str, contract: Any, runner_options: dict[str, Any]
        ) -> Any:
            if heartbeat_errors:
                raise heartbeat_errors[0]
            return runner(provider, value, contract, runner_options)

        options["runner"] = fenced_runner
        # Every provider child this call spawns -- however many turns of retries the adapter
        # makes -- gets its own durable record, numbered in spawn order. `_invoke` runs
        # entirely on the calling thread (it starts a thread only for the unrelated lease
        # heartbeat, above); every real Popen this observer can see -- `run_owned` for
        # claude, and Codex's own `app_server_client.py` call -- is reached by a plain,
        # synchronous call from inside `run_campaign_author` below, on this same thread, so
        # the `ContextVar` this sets needs no cross-thread copying to reach them.
        spawn_sequence = 0

        def _record_spawn(pid: int, pgid: int) -> None:
            nonlocal spawn_sequence
            spawn_sequence += 1
            _write_provider_process_record(
                self.root,
                episode_id=episode_id,
                sequence=spawn_sequence,
                pid=pid,
                pgid=pgid,
                recorded_at=self.clock(),
            )

        try:
            with observing_spawns(_record_spawn):
                result = run_campaign_author(**options)
        finally:
            stop.set()
            heartbeat_thread.join(timeout=1.0)
        if heartbeat_errors:
            raise heartbeat_errors[0]
        return result

    @contextmanager
    def _plan_operation(self, *, exclusive: bool):
        """Prevent a new episode from racing the affected-future history census."""
        descriptor = os.open(
            self.root.parent / "revision.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
        )
        try:
            try:
                fcntl.flock(
                    descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
                )
            except BlockingIOError as exc:
                raise CapsuleRuntimeError(
                    "plan revision conflicts with an active operation"
                ) from exc
            yield
        finally:
            os.close(descriptor)

    @contextmanager
    def _episode_operation(self):
        """Exclude recovery and launch across processes for the entire provider operation."""

        descriptor = os.open(
            self.root / "episode.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CapsuleRuntimeError(
                    "capsule already has an episode operation in progress"
                ) from exc
            with self._plan_operation(exclusive=False):
                yield
        finally:
            os.close(descriptor)

    def run_episode(self, episode_id: str | None = None) -> EpisodeOutcome:
        if not self._run_mutex.acquire(blocking=False):
            raise CapsuleRuntimeError("capsule already has an episode operation in progress")
        try:
            with self._episode_operation():
                return self._run_episode(episode_id)
        finally:
            self._run_mutex.release()

    def _run_episode(self, episode_id: str | None = None) -> EpisodeOutcome:
        if self.state in _TERMINAL:
            raise CapsuleRuntimeError(f"capsule is already {self.state}")
        if self.state not in _RUNNABLE_STATES:
            raise CapsuleRuntimeError(
                f"capsule state {self.state!r} requires an explicit EC04/EC05 disposition"
            )
        if self._state.get("active_episode") is not None:
            raise CapsuleRuntimeRecoveryError(
                "capsule has an unresolved episode_started spend boundary; explicit recovery "
                "is required before another provider launch"
            )
        budget_outcome = self._budget_guard()
        if budget_outcome is not None:
            return budget_outcome
        self._heartbeat()
        self._validate_authority_binding()
        self._validate_policy_binding()
        _, selected_provider, _ = self._select_provider()
        self._validate_receipt_chain_custody(self._state.get("provider_receipts") or [])
        requested_episode = episode_id or f"episode.{self._state['episode_count'] + 1}"
        _token(requested_episode, "episode_id")
        if requested_episode in self._state.get("used_episode_ids", []):
            raise CapsuleRuntimeError(f"episode_id {requested_episode!r} has already been spent")
        packet, prompt, packet_digest = self.compile_packet(requested_episode)
        from bearhug.campaign.author import campaign_author_prompt

        transmitted_prompt_bytes = len(
            campaign_author_prompt(selected_provider.provider, prompt).encode("utf-8")
        )
        limits = self.capsule["context_budget"]
        maximum_bytes = limits["max_bytes"]
        maximum_tokens = limits["max_tokens"]
        reserve_bytes = limits["reserve_bytes"]
        reserve_tokens = limits["reserve_tokens"]
        if transmitted_prompt_bytes + (reserve_bytes if type(reserve_bytes) is int else 0) > (
            maximum_bytes if type(maximum_bytes) is int else _HARD_MAX_PROMPT_BYTES
        ) or (transmitted_prompt_bytes + 3) // 4 + (
            reserve_tokens if type(reserve_tokens) is int else 0
        ) > (maximum_tokens if type(maximum_tokens) is int else _HARD_MAX_PROMPT_TOKENS):
            raise CapsuleRuntimeError(
                "final author prompt exceeds sealed context budget; refusing spend"
            )
        current_episode = packet["episode_id"]
        budget_outcome = self._budget_guard()
        if budget_outcome is not None:
            return budget_outcome
        packet_source_artifacts = copy.deepcopy(self._last_packet_artifacts)
        packet_prompt_sha256 = self._last_packet_prompt_sha256
        packet_prompt_blob_sha256 = self._last_packet_prompt_blob_sha256
        launch_base = _oid_from_git(self.worktree, "HEAD")
        # A repair is counted at this spend fence, so failed attempts and provider transport
        # failures consume the approved bound just like successful repairs.
        repair_spend = self._state["state"] == "locally_repairing"
        # This transition is the spend fence.  A restart seeing active_episode must fail closed.
        self._transition(
            "episode_started",
            evidence_refs=(packet_digest,),
            state="executing",
            active_episode={
                "episode_id": current_episode,
                "packet_sha256": packet_digest,
                "launch_base_oid": launch_base,
                "spent": True,
            },
            repair_episodes=self._state["repair_episodes"] + (1 if repair_spend else 0),
            episode_count=self._state["episode_count"] + 1,
            used_episode_ids=self._state.get("used_episode_ids", []) + [current_episode],
            last_packet_sha256=packet_digest,
            last_packet_prompt_sha256=packet_prompt_sha256,
            last_packet_prompt_blob_sha256=packet_prompt_blob_sha256,
            last_packet_source_artifacts=packet_source_artifacts,
        )
        try:
            raw = self._invoke(packet, prompt)
            result, provider_run = self._episode_result(raw)
            provider_digest, failure_digest, provider_receipt = self._custody_refs(
                result, provider_run
            )
            if provider_digest:
                result = bind_author_observations(result, provider_digest)
            # Recheck the complete lease identity after provider return before accepting any
            # continuation or candidate.  Lease loss becomes an HIL/orphan boundary.
            self._heartbeat()
            outcome = result["outcome"]
            validation = self._validation(result)
            candidate: dict[str, Any] | None = None
            changed_paths: tuple[str, ...] = ()
            success_outcome = outcome in {
                "continue_with_evidence",
                "candidate_ready",
                "local_repair_required",
                "reconciliation_required",
            }
            provider_receipts = list(self._state.get("provider_receipts") or [])
            if success_outcome:
                if provider_receipt is None:
                    raise CapsuleRuntimeError(
                        "successful episode requires exact provider receipt custody"
                    )
                provider_receipts.append(provider_receipt)
                self._validate_receipt_chain_custody(provider_receipts)
                try:
                    candidate, changed_paths = self._candidate(provider_receipts)
                except CapsuleRuntimeError as exc:
                    outcome = "reconciliation_required"
                    result = dict(result)
                    result["changed_facts"] = list(result.get("changed_facts", [])) + [
                        f"candidate verification failed: {exc}"
                    ]
            elif provider_receipt is not None and provider_receipt["terminal_state"] == "completed":
                # Failed transport receipts remain in custody and the digest ledger below;
                # they must never enter the successful candidate's cumulative receipt chain.
                # A provider can finish with a semantic HIL/failure outcome after producing a
                # valid session receipt.  Preserve that receipt for forensic custody even though
                # it is never promoted into a cumulative candidate.
                provider_receipts.append(provider_receipt)
                self._validate_receipt_chain_custody(provider_receipts)
            if changed_paths:
                result = dict(result)
                result["changed_facts"] = list(result.get("changed_facts", [])) + [
                    f"touched:{path}" for path in changed_paths
                ]
            delta = self._state_delta(
                result,
                provider_digest=provider_digest,
                candidate=candidate,
            )
            refs = [packet_digest]
            if provider_digest:
                refs.append(provider_digest)
            if failure_digest:
                refs.append(failure_digest)
            # Provider-final JSON may assert a usage object, but it is not provider accounting.
            # Only a normalized transport usage record could be measured here; existing provider
            # transports do not expose one at this boundary.
            total_usage: dict[str, int] = {}
            state_name = {
                "continue_with_evidence": "continuing",
                "candidate_ready": "candidate_ready",
                "local_repair_required": "locally_repairing",
                "reconciliation_required": "reconciling",
                "hil_required": "awaiting_hil",
                "blocked": "blocked",
                "failed": "failed",
            }[outcome]
            self._transition(
                _EVENT_FOR_OUTCOME[outcome],
                evidence_refs=refs,
                state=state_name,
                active_episode=None,
                last_outcome=outcome,
                state_delta=delta,
                head_oid=_oid_from_git(self.worktree, "HEAD"),
                validation=[dict(row) for row in validation],
                validation_receipt_sha256=None,
                validation_artifact_refs={},
                acceptance_proof_sha256=None,
                obligation_coverage=copy.deepcopy(
                    result.get("obligation_coverage", self._state.get("obligation_coverage") or [])
                ),
                invariants=copy.deepcopy(
                    result.get("invariants", self._state.get("invariants") or [])
                ),
                discoveries=list(result.get("discoveries", [])),
                unresolved_decisions=list(result.get("unresolved_decisions", [])),
                next_action=result.get("next_action"),
                provider_receipt_sha256s=self._state["provider_receipt_sha256s"]
                + ([provider_digest] if provider_digest else []),
                provider_receipts=provider_receipts,
                failure_receipt_sha256s=self._state["failure_receipt_sha256s"]
                + ([failure_digest] if failure_digest else []),
                usage=total_usage,
                repair_episodes=self._state["repair_episodes"],
                prompt_bytes=self._state["prompt_bytes"] + transmitted_prompt_bytes,
                repeated_context_bytes=self._state["repeated_context_bytes"]
                + self._repeated_context_bytes(packet),
                included_source_digests=[
                    row["source_sha256"]
                    for row in packet["sources"]
                    if row["selection"] == "included"
                ],
            )
            if outcome in {"continue_with_evidence", "local_repair_required", "candidate_ready"}:
                self.reconcile(
                    {"validation": list(validation)},
                    discoveries=list(result.get("discoveries", [])),
                    evidence_refs=refs,
                    actor=f"provider.{self.provider_role_name}",
                    _route=False,
                )
                if self.state == "awaiting_hil":
                    outcome = "hil_required"
            elif outcome == "reconciliation_required":
                # Convert the provider's boundary into an evidence-bound EC04 record before any
                # caller can attempt another spend. The outcome alone does not prove concept
                # drift; affected-future revision still requires the sealed authority checks.
                observed = {
                    "intent": {
                        "intent_envelope_sha256": self.intent_sha256,
                        "plan_sha256": self.plan_sha256,
                        "revision_id": self.revision_id,
                        "capsule_id": self.capsule_id,
                    },
                    "validation": list(validation),
                }
                if changed_paths:
                    observed["observed_surface"] = {
                        "path_prefixes": list(changed_paths),
                        "symbols": [],
                        "subjects": [],
                        "semantic_resources": [],
                        "data_directories": [],
                    }
                if result.get("obligation_coverage") is not None:
                    observed["obligation_coverage"] = result["obligation_coverage"]
                self.reconcile(
                    observed,
                    discoveries=[
                        {
                            "kind": "invalidated_assumption",
                            "summary": "provider episode requested affected-future reconciliation",
                            "evidence_refs": refs,
                        }
                    ],
                    evidence_refs=refs,
                )
            elif outcome == "hil_required" and self.hil_request is None:
                request = self._make_hil_request(
                    conflict="Provider episode requires an operator disposition before resume.",
                    evidence_refs=refs,
                    options=self._default_hil_options("job_ticket_revision"),
                )
                self._transition(
                    "hil_requested",
                    evidence_refs=[*refs, _json_digest(request)],
                    state="awaiting_hil",
                    hil_request=request,
                    next_action="await the one-use operator HIL disposition",
                )
            if outcome == "candidate_ready" and candidate is not None:
                self._publish_result(candidate, changed_paths, refs)
            return EpisodeOutcome(
                outcome,
                current_episode,
                packet_digest,
                provider_digest,
                failure_digest,
                validation,
                tuple(result.get("changed_facts", [])),
                tuple(result.get("discoveries", [])),
                tuple(result.get("unresolved_decisions", [])),
                result.get("next_action"),
                None,
            )
        except LeaseFenceLost as exc:
            refs = [packet_digest]
            request = self._make_hil_request(
                conflict=f"The exact capsule lease fence was lost: {exc}",
                evidence_refs=refs,
                options=self._default_hil_options("job_ticket_revision"),
            )
            # The provider operation may still be alive.  Keep the spend fence until explicit
            # recovery proves or disposes of that operation; HIL must never imply termination.
            self._transition(
                "hil_requested",
                evidence_refs=[*refs, _json_digest(request)],
                state="awaiting_hil",
                hil_request=request,
                last_outcome="hil_required",
                state_delta={
                    "changed_facts": ["lease custody lost"],
                    "candidate": None,
                    "git": None,
                    "validation_state_changes": [],
                    "discoveries": [],
                    "unresolved_decisions": [],
                    "next_action": "recover the orphaned spend before any fresh episode",
                },
                next_action="recover the orphaned spend before answering the lease HIL request",
            )
            return EpisodeOutcome("hil_required", current_episode, packet_digest)
        except CapsuleRuntimeError:
            raise
        except Exception as exc:
            # Existing provider transports durably write a failure receipt before raising.  Carry
            # that exact receipt reference if the exception exposes it; never synthesize success.
            failure_digest = self._failure_digest_from_exception(exc)
            failure_reason = f"Provider episode failed: {exc}"
            refs = [packet_digest] + ([failure_digest] if failure_digest else [])
            self._transition(
                "episode_completed",
                evidence_refs=refs,
                state="failed",
                # A transport exception is not proof that its process or remote operation ended.
                # Preserve active_episode so ordinary reopen/retry/stop fails closed and only the
                # explicit recovery boundary can dispose of the already-spent episode.
                last_outcome="failed",
                last_failed_stage="author",
                next_action=failure_reason,
                state_delta={
                    "changed_facts": [failure_reason],
                    "candidate": None,
                    "git": None,
                    "validation_state_changes": [],
                    "discoveries": [],
                    "unresolved_decisions": [],
                    "next_action": failure_reason,
                },
                failure_receipt_sha256s=self._state["failure_receipt_sha256s"]
                + ([failure_digest] if failure_digest else []),
                prompt_bytes=self._state["prompt_bytes"] + transmitted_prompt_bytes,
                repeated_context_bytes=self._state["repeated_context_bytes"]
                + self._repeated_context_bytes(packet),
                included_source_digests=[
                    row["source_sha256"]
                    for row in packet["sources"]
                    if row["selection"] == "included"
                ],
            )
            return EpisodeOutcome(
                "failed",
                current_episode,
                packet_digest,
                failure_receipt_sha256=failure_digest,
                next_action=failure_reason,
            )

    def _failure_digest_from_exception(self, exc: Exception) -> str | None:
        path = getattr(exc, "receipt_path", None)
        if isinstance(path, (str, Path)):
            try:
                value = json.loads(Path(path).read_text(encoding="utf-8"))
                validate_provider_failure_receipt(value)
            except (OSError, UnicodeError, json.JSONDecodeError, ProviderFailureReceiptError):
                return None
            return _json_digest(value)
        return None

    def _repeated_context_bytes(self, packet: Mapping[str, Any]) -> int:
        value = packet.get("totals", {}).get("repeated_context_bytes", 0)
        if type(value) is not int or value < 0:
            raise CapsuleRuntimeError("packet repeated context accounting is invalid")
        return value

    def _publish_result(
        self, candidate: Mapping[str, Any], changed_paths: Sequence[str], refs: Sequence[str]
    ) -> None:
        # Also project older candidate-ready state whose provider citations were persisted
        # before contract validation. Resume can then reuse its existing candidate/custody.
        observed = self._state
        if self._state.get("provider_receipt_sha256s"):
            observed = bind_author_observations(
                self._state, self._state["provider_receipt_sha256s"][-1]
            )
        observed_coverage = observed.get("obligation_coverage") or []
        expected_coverage = {
            (row["source_id"], row["obligation_id"]) for row in self.capsule["obligation_coverage"]
        }
        coverage = (
            copy.deepcopy(observed_coverage)
            if isinstance(observed_coverage, list)
            and {
                (row.get("source_id"), row.get("obligation_id"))
                for row in observed_coverage
                if isinstance(row, Mapping)
            }
            == expected_coverage
            else [
                {
                    "source_id": row["source_id"],
                    "obligation_id": row["obligation_id"],
                    "status": "unavailable",
                    "evidence_refs": sorted(set(refs)),
                }
                for row in self.capsule["obligation_coverage"]
            ]
        )
        profiles = self._state.get("validation") or [
            {
                "profile_id": profile["profile_id"],
                "status": "unavailable",
                "evidence_refs": sorted(set(refs)),
            }
            for profile in self.capsule["validation_profiles"]
        ]
        observed_invariants = observed.get("invariants") or []
        expected_invariants = set(self.capsule["invariant_refs"])
        invariants = (
            copy.deepcopy(observed_invariants)
            if isinstance(observed_invariants, list)
            and {row.get("invariant_id") for row in observed_invariants if isinstance(row, Mapping)}
            == expected_invariants
            else [
                {
                    "invariant_id": invariant_id,
                    "status": "unavailable",
                    "evidence_refs": sorted(set(refs)),
                }
                for invariant_id in self.capsule["invariant_refs"]
            ]
        )
        metrics: dict[str, dict[str, Any]] = {}

        def measured(name: str, value: int, unit: str) -> None:
            metrics[name] = {
                "status": "measured",
                "value": value,
                "unit": unit,
                "evidence_refs": sorted(set(refs)),
                "reason": None,
            }

        def unavailable(name: str, unit: str, reason: str) -> None:
            metrics[name] = {
                "status": "unavailable",
                "value": None,
                "unit": unit,
                "evidence_refs": sorted(set(refs)),
                "reason": reason,
            }

        measured("prompt_bytes", self._state["prompt_bytes"], "bytes")
        measured("repeated_context_bytes", self._state["repeated_context_bytes"], "bytes")
        measured("repair_episodes", self._state["repair_episodes"], "count")
        unavailable("operator_commands", "count", "controller command accounting is outside EC03")
        measured("review_launches", self._state.get("review_launches", 0), "count")
        unavailable(
            "invariant_failures", "count", "independent invariant evaluation is owned by EC04"
        )
        unavailable(
            "authority_violations",
            "count",
            "reconciliation comparisons are observations, not independent acceptance proof",
        )
        unavailable("provider_usage_tokens", "tokens", "provider billed usage is unavailable")
        result = {
            "schema_version": "1",
            "record_kind": "capsule_result",
            "canonical_algorithm": CANONICAL_ALGORITHM,
            "result_id": f"result.{self.capsule_id}",
            "intent_envelope_sha256": self.intent_sha256,
            "plan_sha256": self.plan_sha256,
            "revision_id": self.revision_id,
            "capsule_id": self.capsule_id,
            "subject": copy.deepcopy(self.plan["subject"]),
            "status": "candidate_ready",
            "candidate": {
                "base_oid": candidate["base_oid"],
                "head_oid": candidate["head_oid"],
                "tree_oid": candidate["tree_oid"],
                "patch_sha256": candidate["patch_sha256"],
                "clean": candidate["clean"],
            },
            "obligation_coverage": coverage,
            "validation": profiles,
            "invariants": invariants,
            "reconciliation": (
                {
                    "status": (
                        self._state["reconciliation"]["status"]
                        if self._state.get("reconciliation") is not None
                        and self._state["reconciliation"]["status"]
                        in {"consistent", "changed", "blocked", "unavailable"}
                        else "unavailable"
                    ),
                    "evidence_refs": sorted(
                        set(refs)
                        | set(
                            self._state.get("reconciliation", {}).get("evidence_refs", [])
                            if isinstance(self._state.get("reconciliation"), Mapping)
                            else []
                        )
                    ),
                }
                if self._state.get("reconciliation") is not None
                else {"status": "unavailable", "evidence_refs": sorted(set(refs))}
            ),
            "findings": copy.deepcopy(self._state.get("review_findings") or []),
            "review_refs": sorted(set(self._state.get("review_refs") or [])),
            "integration_refs": [],
            "administration_metrics": metrics,
        }
        try:
            validate_capsule_result(result)
        except CapsuleContractError as exc:
            raise CapsuleRuntimeError(f"capsule result is invalid: {exc}") from exc
        digest = self.objects.put(result)
        with self._exclusive():
            if self._latest_state_digest() != self._state_digest:
                raise CapsuleRuntimeError(
                    "capsule runtime state changed concurrently before result publication"
                )
            self._persist_state(
                {
                    "result_sha256": digest,
                    "obligation_coverage": observed.get("obligation_coverage", []),
                    "invariants": observed.get("invariants", []),
                }
            )

    @property
    def reconciliation(self) -> dict[str, Any] | None:
        """Return the latest evidence-bound reconciliation, if one has been recorded."""

        value = self._state.get("reconciliation")
        return None if value is None else copy.deepcopy(value)

    @property
    def hil_request(self) -> dict[str, Any] | None:
        """Return the active focused HIL request, if the capsule is awaiting one."""

        value = self._state.get("hil_request")
        return None if value is None else copy.deepcopy(value)

    @property
    def hil_answers(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._state.get("hil_answers") or ()))

    def reconcile(
        self,
        observations: Mapping[str, Any] | None = None,
        *,
        discoveries: Sequence[Mapping[str, Any] | str] = (),
        assessments: Sequence[Mapping[str, Any]] = (),
        evidence_refs: Iterable[str] = (),
        actor: str = "runtime",
        confidence: str = "high",
        _route: bool = True,
    ) -> dict[str, Any]:
        """Record an explicit comparison and route its correction class.

        ``observations`` are caller-supplied facts.  The helper records them as comparisons and
        leaves unavailable values unavailable; this method does not promote semantic labels into
        the sealed intent or plan.
        """

        if self._state.get("active_episode") is not None:
            raise CapsuleRuntimeError("cannot reconcile while an episode spend boundary is active")
        if self.state == "awaiting_hil" and self._state.get("hil_request") is not None:
            raise CapsuleRuntimeError("cannot replace an unanswered HIL request")
        if self.state not in {
            "preflighted",
            "continuing",
            "locally_repairing",
            "reconciling",
            "awaiting_hil",
            "candidate_ready",
        }:
            raise CapsuleRuntimeError(f"capsule state {self.state!r} cannot be reconciled")
        authority = {row["source_id"]: row for row in self.intent["authority_refs"]}
        bound_evidence = {
            ref
            for item in (*self.intent["bindings"], *self.intent["invariants"])
            for ref in item["evidence_refs"]
        }
        inventory = []
        for source_id, row in self.source_contents.items():
            if not isinstance(row.get("path"), str):
                continue
            if source_id in authority:
                expected = authority[source_id]["content_sha256"]
            else:
                content = row.get("content", row.get("text"))
                if isinstance(content, str):
                    content = content.encode("utf-8")
                expected = (
                    hashlib.sha256(content).hexdigest() if isinstance(content, bytes) else None
                )
                if expected not in bound_evidence:
                    continue
            inventory.append(
                {"source_id": source_id, "path": row["path"], "expected_sha256": expected}
            )
        try:
            grounding = capture_capsule_grounding(
                self.worktree,
                base_oid=self.base_oid,
                sources=inventory,
                expected_head_oid=self._state["head_oid"],
            )
        except CapsuleGroundingError as exc:
            raise CapsuleRuntimeError(f"reconciliation grounding failed: {exc}") from exc
        if self._state["provider_receipts"]:
            self._validate_receipt_chain_custody(self._state["provider_receipts"])
            candidate, paths = self._candidate(self._state["provider_receipts"])
            grounding["candidate"] = candidate
            grounding["observed_paths"] = list(paths)
        else:
            grounding["candidate"] = None
            grounding["observed_paths"] = None
        changed_evidence = {
            row["expected_sha256"] for row in grounding["sources"] if row["freshness"] != "fresh"
        }
        grounding["binding_evidence_changes"] = [
            {
                "binding_id": row["binding_id"],
                "changed_evidence_refs": sorted(set(row["evidence_refs"]) & changed_evidence),
                "meaning_effect": "unchanged; reground evidence before relying on it",
            }
            for row in self.intent["bindings"]
            if set(row["evidence_refs"]) & changed_evidence
        ]
        grounding["affected_packet_sha256"] = self._state.get("last_packet_sha256")
        grounding_digest = self._put_blob(_canonical(grounding))
        discoveries = list(discoveries)
        stale_sources = [
            row["source_id"]
            for row in grounding["sources"]
            if row["freshness"] != "fresh" and row["source_id"] in authority
        ]
        if stale_sources:
            discoveries.append(
                {
                    "kind": "concept_drift",
                    "summary": "sealed authority source changed: " + ", ".join(stale_sources),
                    "actor": "runtime.git",
                    "evidence_refs": [grounding_digest],
                }
            )
        elif changed_evidence:
            discoveries.append(
                {
                    "kind": "invalidated_assumption",
                    "summary": "project evidence changed; reground affected future packets",
                    "actor": "runtime.git",
                    "evidence_refs": [grounding_digest],
                }
            )
        evidence_refs = sorted(set(evidence_refs) | {grounding_digest})
        try:
            record = build_reconciliation_record(
                intent_envelope=self.intent,
                capsule_plan=self.plan,
                capsule=self.capsule,
                observations=observations,
                discoveries=discoveries,
                assessments=assessments,
                actor=actor,
                confidence=confidence,
                evidence_refs=evidence_refs,
                previous_plan=self._predecessor_plan(),
                dependency_base=self.dependency_base,
            )
            validate_reconciliation_record(record)
        except ReconciliationError as exc:
            raise CapsuleRuntimeError(f"capsule reconciliation failed: {exc}") from exc
        digest = reconciliation_digest(record)
        refs = sorted(set(record["evidence_refs"]) | {digest})
        proposals = self._write_decision_proposals(record, refs)
        proposal_state: dict[str, Any] = {}
        if proposals:
            refs = sorted(set(refs) | {row["markdown_sha256"] for row in proposals})
            proposal_state = {
                "decision_proposals": [
                    *self._state.get("decision_proposals", []),
                    *proposals,
                ]
            }
        if record["hil_triggers"]:
            options = self._default_hil_options(record["correction_class"])
            conflict = self._reconciliation_conflict(record)
            if proposals:
                conflict += " Proposed decision drafts: " + ", ".join(
                    row["filename"] for row in proposals
                ) + "."
            request = self._make_hil_request(
                conflict=conflict,
                evidence_refs=refs,
                options=options,
                correction_class=record["correction_class"],
                requires_revision=record["correction_class"] != "local_correction",
            )
            self._transition(
                "hil_requested",
                evidence_refs=refs + [_json_digest(request)],
                state="awaiting_hil",
                reconciliation=record,
                reconciliation_sha256=digest,
                grounding_sha256=grounding_digest,
                hil_request=request,
                **proposal_state,
                last_outcome="hil_required",
                next_action=(
                    "await the one-use operator HIL disposition"
                    if not proposals
                    else "await the operator HIL disposition; review the proposed decision "
                    "drafts under the capsule's proposals directory first"
                ),
                state_delta={
                    "changed_facts": ["reconciliation requires operator disposition"],
                    "candidate": None,
                    "git": None,
                    "validation_state_changes": [],
                    "discoveries": [row["summary"] for row in record["discoveries"]],
                    "unresolved_decisions": [
                        f"HIL required for {trigger}" for trigger in record["hil_triggers"]
                    ],
                    "next_action": "await the one-use operator HIL disposition",
                },
            )
        else:
            if not _route:
                self._transition(
                    "reconciliation_completed",
                    evidence_refs=refs,
                    reconciliation=record,
                    reconciliation_sha256=digest,
                    grounding_sha256=grounding_digest,
                    **proposal_state,
                )
                return copy.deepcopy(record)
            state_name = (
                "locally_repairing"
                if record["correction_class"] == "local_correction"
                else "reconciling"
            )
            self._transition(
                "reconciliation_completed",
                evidence_refs=refs,
                state=state_name,
                reconciliation=record,
                reconciliation_sha256=digest,
                grounding_sha256=grounding_digest,
                hil_request=None,
                **proposal_state,
                last_outcome=(
                    "local_repair_required"
                    if state_name == "locally_repairing"
                    else "reconciliation_required"
                ),
                discoveries=[row["summary"] for row in record["discoveries"]],
                next_action=(
                    "run another bounded episode for the local correction"
                    if state_name == "locally_repairing"
                    else "activate the affected future plan revision"
                ),
                state_delta={
                    "changed_facts": [
                        f"{row['profile_id']}={row['status']}"
                        for row in record["mechanical"]["validation"].get("rows", [])
                    ],
                    "candidate": None,
                    "git": None,
                    "validation_state_changes": [],
                    "discoveries": [row["summary"] for row in record["discoveries"]],
                    "unresolved_decisions": [],
                    "next_action": (
                        "run another bounded episode for the local correction"
                        if state_name == "locally_repairing"
                        else "activate the affected future plan revision"
                    ),
                },
            )
        return copy.deepcopy(record)

    def _write_decision_proposals(
        self, record: Mapping[str, Any], evidence_refs: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Write one Memex-shaped ``status: proposed`` draft per architectural discovery.

        Drafts live in this capsule's custody (``<root>/proposals``), never in the subject's
        ``docs/memex``; the operator or the ``decide`` skill adopts them.  Create-only and
        content-addressed, so a re-run of the same reconciliation never duplicates a draft.
        """

        from bearhug.campaign.decision_proposal import (
            DecisionProposalError,
            proposals_for,
        )

        try:
            built = proposals_for(
                record.get("discoveries", []),
                capsule_id=self.capsule_id,
                intent_envelope=self.intent,
                date=datetime.now(tz=UTC).strftime("%Y-%m-%d"),
                evidence_refs=evidence_refs,
                related_decision_ids=[
                    row["binding_id"].rsplit(".", 1)[-1]
                    for row in self.intent["bindings"]
                    if row["binding_id"].startswith("binding.decision.")
                ],
            )
        except DecisionProposalError as exc:
            raise CapsuleRuntimeError(f"decision proposal failed: {exc}") from exc
        if not built:
            return []
        directory = _private_directory(self.root / "proposals", create=True)
        written: list[dict[str, Any]] = []
        known = {row["markdown_sha256"] for row in self._state.get("decision_proposals", [])}
        for proposal in built:
            if proposal.sha256 in known:
                continue
            self._put_blob(proposal.markdown)
            target = directory / proposal.filename
            if not target.exists():
                _create_blob_only(target, proposal.markdown)
            elif target.read_bytes() != proposal.markdown:
                # Same discovery text on the same day with different evidence: keep both.
                target = directory / f"{proposal.sha256[:12]}-{proposal.filename}"
                if not target.exists():
                    _create_blob_only(target, proposal.markdown)
            written.append({**proposal.record, "path": str(target)})
        return written

    @staticmethod
    def _default_hil_options(correction_class: str) -> list[dict[str, Any]]:
        return [
            {
                "option_id": "approve",
                "label": (
                    "Approve a plan revision; a successor must still be supplied."
                    if correction_class == "job_ticket_revision"
                    else "Approve the proposed bounded continuation."
                ),
                "consequences": [
                    "approval does not replace the required content-bound successor plan"
                    if correction_class == "job_ticket_revision"
                    else "continue only within the sealed capsule authority"
                ],
            },
            {
                "option_id": "deny",
                "label": "Deny and block this capsule.",
                "consequences": ["no further provider spend is permitted"],
            },
            {
                "option_id": "amend",
                "label": "Amend the affected future plan.",
                "consequences": ["a content-bound successor plan must be supplied"],
            },
            {
                "option_id": "defer",
                "label": "Defer while preserving the evidence.",
                "consequences": ["the capsule remains paused for a later decision"],
            },
            {
                "option_id": "stop",
                "label": "Stop the campaign path.",
                "consequences": ["the capsule is durably blocked"],
            },
        ]

    @staticmethod
    def _reconciliation_conflict(record: Mapping[str, Any]) -> str:
        triggers = ", ".join(record["hil_triggers"])
        return f"Reconciliation requires an operator decision for: {triggers}."

    def _make_hil_request(
        self,
        *,
        conflict: str,
        evidence_refs: Iterable[str],
        options: Sequence[Mapping[str, Any]],
        correction_class: str = "job_ticket_revision",
        requires_revision: bool = False,
    ) -> dict[str, Any]:
        refs = sorted(set(evidence_refs))
        for ref in refs:
            _sha(ref, "HIL evidence reference")
        if not isinstance(conflict, str) or not conflict.strip():
            raise CapsuleRuntimeError("HIL conflict must be non-empty text")
        normalized_options = []
        for option in options:
            if not isinstance(option, Mapping):
                raise CapsuleRuntimeError("HIL option must be an object")
            normalized_options.append(
                {
                    "option_id": _token(option.get("option_id"), "HIL option_id"),
                    "label": str(option.get("label", "")),
                    "consequences": list(option.get("consequences", [])),
                }
            )
        request = {
            "schema_version": "1",
            "record_kind": "capsule_hil_request",
            "canonical_algorithm": CANONICAL_ALGORITHM,
            "request_id": f"hil.{self.capsule_id}.{self._state['sequence'] + 1}",
            "intent_envelope_sha256": self.intent_sha256,
            "plan_sha256": self.plan_sha256,
            "revision_id": self.revision_id,
            "capsule_id": self.capsule_id,
            "subject": copy.deepcopy(self.plan["subject"]),
            "conflict": conflict,
            "evidence_refs": refs,
            "options": normalized_options,
            "correction_class": correction_class,
            "requires_revision": requires_revision,
            "head_oid": _oid_from_git(self.worktree, "HEAD"),
            "state_sequence": self._state["sequence"] + 1,
            "resume_token_sha256": "0" * 64,
        }
        # The token is a deterministic digest of the exact request content before the token field
        # is populated.  It is therefore content-bound and can be consumed at most once by state.
        request["resume_token_sha256"] = _json_digest(request)
        _validate_hil_request(request)
        return request

    def request_hil(
        self,
        *,
        conflict: str,
        evidence_refs: Iterable[str] = (),
        options: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Durably request one exact, focused operator decision."""

        if self._state.get("active_episode") is not None:
            raise CapsuleRuntimeError(
                "cannot request HIL while an episode spend boundary is active"
            )
        if self.state not in {
            "preflighted",
            "continuing",
            "locally_repairing",
            "reconciling",
            "awaiting_hil",
        }:
            raise CapsuleRuntimeError(f"capsule state {self.state!r} cannot request HIL")
        if self.hil_request is not None:
            raise CapsuleRuntimeError("cannot replace an unanswered HIL request")
        request = self._make_hil_request(
            conflict=conflict,
            evidence_refs=evidence_refs,
            options=options or self._default_hil_options("job_ticket_revision"),
        )
        request_digest = _json_digest(request)
        self._transition(
            "hil_requested",
            evidence_refs=[*request["evidence_refs"], request_digest],
            state="awaiting_hil",
            hil_request=request,
            last_outcome="hil_required",
            next_action="await the one-use operator HIL disposition",
        )
        return copy.deepcopy(request)

    def answer_hil(
        self,
        request_id: str,
        decision: str,
        *,
        token: str | None = None,
        answer: str = "",
        amendment: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Consume a request's one-use content token and durably route its disposition."""

        _token(request_id, "HIL request_id")
        if decision not in _HIL_DECISIONS:
            raise CapsuleRuntimeError("HIL decision is unsupported")
        request = self._state.get("hil_request")
        if not isinstance(request, Mapping) or request.get("request_id") != request_id:
            raise CapsuleRuntimeError("HIL response is stale, duplicate, or for another request")
        _validate_hil_request(request)
        expected_token = request["resume_token_sha256"]
        if token is None:
            raise CapsuleRuntimeError("HIL response requires the content-bound one-use token")
        supplied_token = token
        if supplied_token != expected_token:
            raise CapsuleRuntimeError("HIL response token is not bound to the exact request")
        if self._state["sequence"] != request["state_sequence"]:
            raise CapsuleRuntimeError("HIL response is stale for the durable request state")
        if _oid_from_git(self.worktree, "HEAD") != request["head_oid"]:
            raise CapsuleRuntimeError("HIL response is stale for the observed candidate HEAD")
        try:
            capture_launch_repository(self.worktree)
        except ProviderReceiptError as exc:
            raise CapsuleRuntimeError("HIL response requires a clean observed candidate") from exc
        if decision not in {row["option_id"] for row in request["options"]}:
            raise CapsuleRuntimeError("HIL response decision was not an offered option")
        if not isinstance(answer, str) or len(answer) > 3500:
            raise CapsuleRuntimeError("HIL answer must be bounded text (at most 3500 characters)")
        if amendment is not None and not isinstance(amendment, Mapping):
            raise CapsuleRuntimeError("HIL amendment must be an object or null")
        answer_record = {
            "schema_version": "1",
            "record_kind": "capsule_hil_answer",
            "canonical_algorithm": CANONICAL_ALGORITHM,
            "answer_id": f"answer.{request_id}",
            "request_id": request_id,
            "request_sha256": _json_digest(request),
            "intent_envelope_sha256": request["intent_envelope_sha256"],
            "plan_sha256": request["plan_sha256"],
            "revision_id": request["revision_id"],
            "capsule_id": request["capsule_id"],
            "decision": decision,
            "answer": answer,
            "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            "token_sha256": supplied_token,
            "amendment": None if amendment is None else copy.deepcopy(dict(amendment)),
        }
        _validate_hil_answer(answer_record)
        answer_digest = _json_digest(answer_record)
        answers = list(self._state.get("hil_answers") or [])
        if any(row.get("request_id") == request_id for row in answers):
            raise CapsuleRuntimeError("HIL response was already consumed")
        answers.append(answer_record)
        refs = [request["resume_token_sha256"], request["plan_sha256"], answer_digest]
        if decision in {"deny", "stop"}:
            state_name = "blocked"
            next_action = "capsule is blocked by the operator disposition"
        elif request["correction_class"] == "press_change":
            state_name = "blocked"
            next_action = "carry the finding into a separate Bear Hug improvement task"
        elif decision == "defer":
            state_name = "reconciling"
            next_action = "preserve evidence and request a later operator disposition"
        elif decision == "amend" or request["requires_revision"]:
            state_name = "reconciling"
            next_action = "supply and activate the content-bound successor plan revision"
        else:
            state_name = "continuing"
            next_action = "continue within the approved capsule authority"
        self._transition(
            "hil_answered",
            evidence_refs=refs,
            state=state_name,
            hil_request=None,
            hil_answers=answers,
            last_hil_answer=answer_record,
            last_outcome=("blocked" if state_name == "blocked" else "continue_with_evidence"),
            next_action=next_action,
            state_delta={
                "changed_facts": [
                    f"operator HIL disposition: {decision}",
                    f"operator answer: {answer}",
                    f"operator answer evidence: {answer_digest}",
                ],
                "candidate": None,
                "git": None,
                "validation_state_changes": [],
                "discoveries": [],
                "unresolved_decisions": [] if decision != "defer" else ["operator deferred"],
                "next_action": next_action,
            },
        )
        return copy.deepcopy(answer_record)

    @staticmethod
    def _claim_values(claim: Mapping[str, Any], field: str) -> set[Any]:
        values = claim.get(field, [])
        if field == "ports":
            return {
                (row.get("transport"), row.get("port"), row.get("bind_scope"))
                for row in values
                if isinstance(row, Mapping)
            }
        return set(values) if isinstance(values, list) else set()

    def _validate_revision_safety(
        self,
        successor: Mapping[str, Any],
        *,
        reconciliation: Mapping[str, Any] | None,
        approval_mode: str,
        hil_answer: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Apply EC04's non-weakening and affected-future activation checks."""

        try:
            successor_result = validate_capsule_plan(
                dict(successor), intent_envelope=self.intent, previous_plan=self.plan
            )
        except CapsuleContractError as exc:
            raise CapsuleRuntimeError(f"successor plan revision is invalid: {exc}") from exc
        revision = successor["revision"]
        if revision["predecessor_sha256"] != self.plan_sha256:
            raise CapsuleRuntimeError("successor plan is not descended from the active plan")
        successor_digest = successor_result.digest
        if approval_mode not in {"automatic", "hil_approved"}:
            raise CapsuleRuntimeError("plan revision approval mode is unsupported")
        if approval_mode == "automatic":
            if revision["approval_mode"] != "automatic":
                raise CapsuleRuntimeError("automatic activation requires an automatic revision")
            if reconciliation is None:
                raise CapsuleRuntimeError("automatic activation requires reconciliation evidence")
            try:
                validate_reconciliation_record(reconciliation)
            except ReconciliationError as exc:
                raise CapsuleRuntimeError("automatic activation reconciliation is invalid") from exc
            if (
                reconciliation.get("plan_sha256") != self.plan_sha256
                or reconciliation_digest(reconciliation) != self._state.get("reconciliation_sha256")
                or reconciliation.get("correction_class") != "job_ticket_revision"
                or reconciliation.get("hil_triggers")
            ):
                raise CapsuleRuntimeError(
                    "automatic plan activation is outside the sealed unchanged-authority envelope"
                )
        else:
            if revision["approval_mode"] != "hil_approved":
                raise CapsuleRuntimeError("HIL activation requires a hil_approved revision")
            if hil_answer is None or hil_answer.get("decision") not in {"approve", "amend"}:
                raise CapsuleRuntimeError("HIL activation requires an approve or amend answer")
            if self.reconciliation and self.reconciliation["correction_class"] == "press_change":
                raise CapsuleRuntimeError(
                    "press changes require a separate Bear Hug improvement task"
                )
            _validate_hil_answer(hil_answer)
            if hil_answer.get("plan_sha256") != self.plan_sha256:
                raise CapsuleRuntimeError("HIL answer is bound to a different active plan")
            if not any(
                isinstance(row, Mapping) and _canonical(row) == _canonical(hil_answer)
                for row in self._state.get("hil_answers", [])
            ):
                raise CapsuleRuntimeError("HIL answer was not consumed by this runtime")
            if _canonical(hil_answer) != _canonical(self._state.get("last_hil_answer")):
                raise CapsuleRuntimeError("HIL answer is not the latest operator disposition")
            amendment = hil_answer.get("amendment")
            if amendment is None or amendment.get("successor_plan_sha256") != successor_digest:
                raise CapsuleRuntimeError("HIL amendment is not bound to the exact successor plan")
        affected = set(revision["affected_capsule_ids"])
        preserved = set(revision["preserved_capsule_ids"])
        old_capsules = {row["capsule_id"]: row for row in self.plan["capsules"]}
        new_capsules = {row["capsule_id"]: row for row in successor["capsules"]}
        if self._state["episode_count"] > 0 and self.capsule_id in affected:
            raise CapsuleRuntimeError("a spent capsule cannot be superseded by a future revision")
        if self.capsule_id not in preserved and self._state["episode_count"] > 0:
            raise CapsuleRuntimeError("completed capsule evidence must remain preserved")
        if self.capsule_id not in preserved and self.capsule_id not in affected:
            raise CapsuleRuntimeError("active capsule is absent from revision preservation")
        # Every remapped obligation must be represented by the exact target capsule.  The generic
        # contract checks only that a target ID exists; activation closes that remaining gap.
        coverage = {
            (row["source_id"], row["obligation_id"]): row["capsule_id"]
            for row in revision["obligation_remapping"]
        }
        for old_id in affected:
            old = old_capsules[old_id]
            old_obligations = {
                (row["source_id"], row["obligation_id"]) for row in old["obligation_coverage"]
            }
            new_obligations: dict[tuple[str, str], str] = {}
            for cap_id, item in new_capsules.items():
                if cap_id in preserved:
                    continue
                for row in item["obligation_coverage"]:
                    new_obligations[(row["source_id"], row["obligation_id"])] = cap_id
            for obligation in old_obligations:
                target_id = coverage.get(obligation)
                if target_id is None or target_id not in new_capsules:
                    raise CapsuleRuntimeError(f"revision silently drops obligation {obligation!r}")
                if new_obligations.get(obligation) != target_id:
                    raise CapsuleRuntimeError(
                        f"revision remap target does not cover obligation {obligation!r}"
                    )
                if approval_mode == "automatic":
                    target = new_capsules[target_id]
                    if not set(old.get("invariant_refs", ())) <= set(
                        target.get("invariant_refs", ())
                    ):
                        raise CapsuleRuntimeError(
                            f"revision remap weakens invariants for obligation {obligation!r}"
                        )
                    old_profiles = {row["profile_id"] for row in old.get("validation_profiles", ())}
                    new_profiles = {
                        row["profile_id"] for row in target.get("validation_profiles", ())
                    }
                    if not old_profiles <= new_profiles:
                        raise CapsuleRuntimeError(
                            f"revision remap weakens validation for obligation {obligation!r}"
                        )
                    if target.get("completion_boundary") != old.get("completion_boundary"):
                        raise CapsuleRuntimeError(
                            f"revision remap changes completion for obligation {obligation!r}"
                        )
        if approval_mode == "automatic":
            if successor.get("obligation_coverage_mode") != self.plan.get(
                "obligation_coverage_mode"
            ):
                raise CapsuleRuntimeError("automatic revision weakens obligation coverage mode")
            old_affected = [old_capsules[item] for item in affected]
            new_affected = [
                item for item in successor["capsules"] if item["capsule_id"] not in preserved
            ]
            for field in ("invariant_refs", "provider_capability_needs"):
                old_values = set().union(*(set(item[field]) for item in old_affected))
                new_values = set().union(*(set(item[field]) for item in new_affected))
                if old_values != new_values:
                    raise CapsuleRuntimeError(f"automatic revision changes affected {field}")
            for field in ("validation_profiles", "completion_boundary"):
                old_values = {_canonical(item[field]) for item in old_affected}
                new_values = {_canonical(item[field]) for item in new_affected}
                if old_values != new_values:
                    raise CapsuleRuntimeError(f"automatic revision changes affected {field}")
            for field in (
                "binding_refs",
                "hil_policy_refs",
                "context_budget",
                "reconciliation_triggers",
            ):
                if {_canonical(item[field]) for item in old_affected} != {
                    _canonical(item[field]) for item in new_affected
                }:
                    raise CapsuleRuntimeError(f"automatic revision changes affected {field}")
            for field in (
                "path_prefixes",
                "symbols",
                "subjects",
                "semantic_resources",
                "data_directories",
                "ports",
            ):
                old_values = set().union(
                    *(self._claim_values(item["mutation_envelope"], field) for item in old_affected)
                )
                new_values = set().union(
                    *(self._claim_values(item["mutation_envelope"], field) for item in new_affected)
                )
                if old_values != new_values:
                    raise CapsuleRuntimeError(
                        f"automatic revision changes affected mutation envelope {field}"
                    )
        target = new_capsules.get(self.capsule_id)
        if target is None:
            replacement_ids = sorted(set(new_capsules) - preserved)
            if len(replacement_ids) != 1:
                raise CapsuleRuntimeError(
                    "replacement capsule must be selected explicitly when revision splits scope"
                )
            target = new_capsules[replacement_ids[0]]
        return dict(target)

    def activate_plan_revision(
        self,
        successor_plan: Mapping[str, Any],
        *,
        capsule: Mapping[str, Any] | None = None,
        reconciliation: Mapping[str, Any] | None = None,
        approval_mode: str = "automatic",
        hil_answer: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fence affected future histories while publishing one successor revision."""
        affected = successor_plan.get("revision", {}).get("affected_capsule_ids", [])
        with ExitStack() as stack:
            stack.enter_context(self._plan_operation(exclusive=True))
            self._check_revision_fence(activating=True)
            for sibling in sorted(self.root.parent.iterdir()):
                states = sibling / "states"
                if sibling.is_symlink() or not sibling.is_dir() or not states.is_dir():
                    continue
                # Exclude provider/recovery work through the same operation lock used by episodes.
                lock_path = sibling / "episode.lock"
                stream = stack.enter_context(lock_path.open("a+b"))
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise CapsuleRuntimeError(
                        "plan revision conflicts with an active episode"
                    ) from exc
                previous = "0" * 64
                for sequence, path in enumerate(sorted(states.iterdir())):
                    match = _STATE_FILE.fullmatch(path.name)
                    if path.is_symlink() or not path.is_file() or match is None:
                        raise CapsuleRuntimeError("affected runtime history is not canonical")
                    raw = path.read_bytes()
                    digest = hashlib.sha256(raw).hexdigest()
                    state = _read_json(path)
                    if (
                        digest != match.group("digest")
                        or int(match.group("sequence")) != sequence
                        or state.get("previous_state_sha256") != previous
                        or state.get("sequence") != sequence
                    ):
                        raise CapsuleRuntimeError("affected runtime history is not intact")
                    previous = digest
                    if (
                        state.get("intent_envelope_sha256") == self.intent_sha256
                        and state.get("capsule_id") in affected
                        and (state.get("episode_count", 0) > 0 or state.get("active_episode"))
                    ):
                        raise CapsuleRuntimeError("only unspent future capsules may be superseded")
            return self._activate_plan_revision(
                successor_plan,
                capsule=capsule,
                reconciliation=reconciliation,
                approval_mode=approval_mode,
                hil_answer=hil_answer,
            )

    def _activate_plan_revision(
        self,
        successor_plan: Mapping[str, Any],
        *,
        capsule: Mapping[str, Any] | None = None,
        reconciliation: Mapping[str, Any] | None = None,
        approval_mode: str = "automatic",
        hil_answer: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Activate one validated plan successor while preserving prior state snapshots."""

        if self._state.get("active_episode") is not None:
            raise CapsuleRuntimeError("cannot activate a plan while an episode is unresolved")
        if self.hil_request is not None:
            raise CapsuleRuntimeError("cannot activate a plan with an unanswered HIL request")
        if self.state not in {
            "preflighted",
            "continuing",
            "locally_repairing",
            "reconciling",
            "awaiting_hil",
            "candidate_ready",
            "completed",
        }:
            raise CapsuleRuntimeError(f"capsule state {self.state!r} cannot activate a revision")
        selected = self._validate_revision_safety(
            successor_plan,
            reconciliation=reconciliation or self.reconciliation,
            approval_mode=approval_mode,
            hil_answer=hil_answer,
        )
        if capsule is not None:
            if selected["capsule_id"] != capsule.get("capsule_id"):
                raise CapsuleRuntimeError("selected replacement capsule does not match successor")
            expected = next(
                (
                    item
                    for item in successor_plan["capsules"]
                    if item["capsule_id"] == capsule.get("capsule_id")
                ),
                None,
            )
            if expected is None or _canonical_capsule(expected) != _canonical_capsule(capsule):
                raise CapsuleRuntimeError("selected replacement capsule is not sealed in successor")
            selected = dict(capsule)
        successor = copy.deepcopy(dict(successor_plan))
        successor_digest = validate_capsule_plan(successor).digest
        if self.objects.put(successor) != successor_digest:
            raise CapsuleRuntimeError("successor plan custody digest mismatch")
        refs = [successor_digest, self.plan_sha256]
        if reconciliation is not None:
            refs.append(reconciliation_digest(reconciliation))
        if hil_answer is not None:
            refs.append(_json_digest(hil_answer))
        old_capsule_id = self.capsule_id
        activation_next_action = (
            "compile the first bounded execution packet for the active revision"
        )
        with self._exclusive():
            if self._latest_state_digest() != self._state_digest:
                raise CapsuleRuntimeError("capsule runtime state changed before plan activation")
            if self._state["episode_count"] > 0:
                # This spent capsule is preserved verbatim. Publish the future plan by reference
                # without rebinding its candidate, review, journal or execution status.
                self._append_event("plan_revision_activated", evidence_refs=refs)
                self._persist_state({"future_plan_sha256": successor_digest})
                return copy.deepcopy(successor)
            prior_journal = self.journal
            refs.append(self._state["journal_sha256"])
            self.plan = successor
            self.plan_sha256 = successor_digest
            self.revision_id = successor["revision"]["revision_id"]
            self.capsule = copy.deepcopy(selected)
            self.capsule_id = _token(selected["capsule_id"], "capsule_id")
            # The new journal names the new sealed revision. The old journal remains immutable
            # and is explicitly linked by the activation event and prior runtime snapshots.
            next_journal = dict(
                prior_journal,
                plan_sha256=self.plan_sha256,
                revision_id=self.revision_id,
                capsule_id=self.capsule_id,
                journal_id=f"journal.{self.capsule_id}",
                entries=[],
            )
            self._state["journal_sha256"] = self.objects.put(next_journal)
            self._append_event("plan_revision_activated", evidence_refs=refs)
            self._persist_state(
                {
                    "plan_sha256": self.plan_sha256,
                    "revision_id": self.revision_id,
                    "capsule_id": self.capsule_id,
                    "state": "preflighted",
                    "active_episode": None,
                    "hil_request": None,
                    "last_outcome": None,
                    "next_action": activation_next_action,
                    "state_delta": {
                        "changed_facts": [f"plan revision activated from {old_capsule_id}"],
                        "candidate": None,
                        "git": None,
                        "validation_state_changes": [],
                        "discoveries": [],
                        "unresolved_decisions": [],
                        "next_action": activation_next_action,
                    },
                }
            )
        return copy.deepcopy(self.plan)

    def run(self, *, maximum_episodes: int = 32) -> EpisodeOutcome | None:
        if type(maximum_episodes) is not int or maximum_episodes < 1:
            raise CapsuleRuntimeError("maximum_episodes must be a positive integer")
        last: EpisodeOutcome | None = None
        for _ in range(maximum_episodes):
            if self.state in _TERMINAL:
                return last
            last = self.run_episode()
            if last.outcome != "continue_with_evidence" and last.outcome != "local_repair_required":
                return last
        return last

    def recover_episode(
        self,
        episode_id: str,
        *,
        outcome: EpisodeStatus = "failed",
        evidence_refs: Iterable[str] = (),
        next_action: str = "inspect the durable failure before retrying",
    ) -> EpisodeOutcome:
        if not self._run_mutex.acquire(blocking=False):
            raise CapsuleRuntimeError("capsule already has an episode operation in progress")
        try:
            with self._episode_operation():
                return self._recover_episode(
                    episode_id,
                    outcome=outcome,
                    evidence_refs=evidence_refs,
                    next_action=next_action,
                )
        finally:
            self._run_mutex.release()

    def recover_review(
        self,
        review_id: str,
        *,
        outcome: Literal["failed", "blocked", "hil_required"] = "blocked",
        evidence_refs: Iterable[str] = (),
        next_action: str = "inspect the durable review failure before any retry",
    ) -> None:
        if not self._run_mutex.acquire(blocking=False):
            raise CapsuleRuntimeError("capsule already has an operation in progress")
        try:
            with self._episode_operation():
                return self._recover_review(
                    review_id, outcome=outcome, evidence_refs=evidence_refs, next_action=next_action
                )
        finally:
            self._run_mutex.release()

    def _recover_review(self, review_id, *, outcome, evidence_refs, next_action):
        """Close an interrupted review spend fence explicitly after process recovery."""

        _token(review_id, "review_id")
        active = self._state.get("active_review")
        if not isinstance(active, Mapping) or active.get("review_id") != review_id:
            raise CapsuleRuntimeRecoveryError("review is not the unresolved spend boundary")
        if outcome not in {"failed", "blocked", "hil_required"}:
            raise CapsuleRuntimeRecoveryError("review recovery outcome is unsupported")
        refs = [active["packet_sha256"], *evidence_refs]
        candidate, _ = self._candidate(self._state.get("provider_receipts") or [])
        if candidate != active["candidate"]:
            raise CapsuleRuntimeRecoveryError("author candidate changed during interrupted review")
        request = None
        if outcome == "hil_required":
            request = self._make_hil_request(
                conflict="An interrupted independent review needs an explicit recovery decision.",
                evidence_refs=refs,
                options=self._default_hil_options("job_ticket_revision"),
            )
        self._transition(
            "capsule_blocked" if outcome == "blocked" else "episode_completed",
            evidence_refs=refs,
            state={"failed": "failed", "blocked": "blocked", "hil_required": "awaiting_hil"}[
                outcome
            ],
            active_review=None,
            hil_request=request,
            last_failed_stage="review",
            last_outcome=outcome,
            next_action=next_action,
        )

    def _recover_episode(
        self,
        episode_id: str,
        *,
        outcome: EpisodeStatus,
        evidence_refs: Iterable[str],
        next_action: str,
    ) -> EpisodeOutcome:
        _token(episode_id, "episode_id")
        active = self._state.get("active_episode")
        if not isinstance(active, Mapping) or active.get("episode_id") != episode_id:
            raise CapsuleRuntimeRecoveryError("episode is not the unresolved spend boundary")
        if outcome not in {"failed", "blocked", "hil_required"}:
            raise CapsuleRuntimeRecoveryError(
                "explicit recovery may only fail, block, or request HIL"
            )
        # The single chokepoint: a controller kill can leave a provider child alive in this
        # same leased worktree, and `hil_required` is the only recovery outcome that can ever
        # lead to another episode running there (`failed`/`blocked` are both terminal -- see
        # `_TERMINAL` -- so nothing they do can hand a surviving child a new window to commit
        # into). Before any `_transition` below, every recorded provider process for this
        # episode must be provably gone; refusing here never kills anything and never touches
        # `active_episode`, so a retry after the child actually exits sees the same fence.
        verified_gone_pids: list[int] = []
        if outcome == "hil_required":
            refusal = _recovery_provider_liveness_refusal(self.root, episode_id)
            if refusal is not None:
                raise CapsuleRuntimeRecoveryError(refusal)
            verified_gone_pids = sorted(
                {record["pid"] for record in _load_provider_process_records(self.root, episode_id)}
            )
        packet_digest = self._state["active_episode"]["packet_sha256"]
        refs = [packet_digest, *evidence_refs]
        state_name = {"failed": "failed", "blocked": "blocked", "hil_required": "awaiting_hil"}[
            outcome
        ]
        request = None
        if outcome == "hil_required":
            request = self._make_hil_request(
                conflict="An interrupted provider episode requires an explicit recovery decision.",
                evidence_refs=refs,
                options=self._default_hil_options("job_ticket_revision"),
            )
        changed_facts = ["episode explicitly recovered"]
        if verified_gone_pids:
            changed_facts.append(
                "verified the interrupted attempt's provider process (pid "
                + ", ".join(str(pid) for pid in verified_gone_pids)
                + ") has exited"
            )
        self._transition(
            _EVENT_FOR_OUTCOME[outcome],
            evidence_refs=refs,
            state=state_name,
            active_episode=None,
            hil_request=request,
            head_oid=_oid_from_git(self.worktree, "HEAD"),
            last_outcome=outcome,
            state_delta={
                "changed_facts": changed_facts,
                "candidate": None,
                "git": None,
                "validation_state_changes": [],
                "discoveries": [],
                "unresolved_decisions": [],
                "next_action": next_action,
            },
            next_action=next_action,
        )
        return EpisodeOutcome(outcome, episode_id, packet_digest)

    @contextmanager
    def _candidate_guard(self, *, renew: bool):
        """Hold the candidate locks and bindings; ``renew`` decides whether the fence is
        extended, which is what separates spending from reading."""

        if renew and self.release_only:
            raise CapsuleRuntimeError(
                "a release-only capsule runtime cannot spend or renew its lease"
            )
        if not self._run_mutex.acquire(blocking=False):
            raise CapsuleRuntimeError("capsule already has an operation in progress")
        try:
            with self._episode_operation():
                if self._latest_state_digest() != self._state_digest:
                    raise CapsuleRuntimeError("capsule state changed; reopen before continuing")
                self._validate_authority_binding()
                self._validate_policy_binding()
                if renew:
                    self._heartbeat()
                    yield
                    self._heartbeat()
                else:
                    self._check_lease(require_unexpired=False)
                    yield
        finally:
            self._run_mutex.release()

    @contextmanager
    def _candidate_operation(self):
        """Hold the existing spend/revision fences through candidate evidence operations."""
        with self._candidate_guard(renew=True):
            yield

    @contextmanager
    def _candidate_inspection(self):
        """Read durable candidate state under the same locks, without renewing the fence.

        A stop or a recovery that only reads must not depend on a heartbeat. `_finish_stop`
        opens this to read `active_episode` and `active_review`, then releases the lease
        outside it; gating that read behind a renewable fence meant a lease which expired
        while its campaign was stopped could not be read, so the release never ran. `retire`
        then refused for custody the stop could not clear and `recover` refused for a fence
        it could not renew, leaving no supported way out. Measured on row 240 T2.

        Every other fence still applies: the run mutex, the episode lock, the durable state
        digest, the authority and policy bindings, and the lease identity, base, branch and
        worktree checks. Only the renewal is dropped, and nothing spends in here. Renewal
        stays required wherever work is actually done, so an expired fence still refuses a
        candidate operation.
        """

        with self._candidate_guard(renew=False):
            yield

    def request_review_repair(self) -> None:
        """Operator-requested implementation repair; never waive or revise an invariant."""
        with self._candidate_operation():
            record = self.reconciliation or {}
            discoveries = record.get("discoveries") or []
            if (
                self.state not in {"awaiting_hil", "reconciling"}
                or self._state.get("active_episode")
                or self._state.get("active_review")
                or not discoveries
                or any(
                    row.get("actor") != "reviewer" or row.get("kind") != "invariant_conflict"
                    for row in discoveries
                )
                or set(record.get("hil_triggers", [])) != {"invariant_conflict"}
            ):
                raise CapsuleRuntimeError(
                    "repair requires an idle reviewer invariant finding; other authority changes "
                    "still require their existing reconciliation disposition"
                )
            bundles = self._state.get("acceptance_bundles") or []
            if not bundles:
                raise CapsuleRuntimeError("repair requires durable rejected review evidence")
            bundle = bundles[-1]
            review = self._read_json_blob(bundle["review_record_sha256"], "review record")
            candidate, _ = self._candidate(self._state["provider_receipts"])
            if (
                review.get("verdict") != "reject"
                or review.get("candidate_sha256") != _json_digest(candidate)
                or not review.get("findings")
                or any(
                    row.get("kind")
                    not in {
                        "invariant_conflict",
                        "local_defect",
                        "missing_work",
                        "validation_failure",
                    }
                    for row in review["findings"]
                )
            ):
                raise CapsuleRuntimeError("repair requires the exact rejected candidate")
            refs = [bundle["review_record_sha256"], self._state["reconciliation_sha256"]]
            if self.hil_request:
                refs.append(_json_digest(self.hil_request))
            instruction = (
                "Repair the rejected implementation under the unchanged approved plan and "
                "invariants. Do not change authority or waive findings. Rerun validation and "
                "obtain independent review. If the requirements cannot coexist, stop and explain."
            )
            self._transition(
                "review_finding",
                evidence_refs=refs,
                state="locally_repairing",
                hil_request=None,
                last_hil_answer=None,
                reconciliation=None,
                reconciliation_sha256=None,
                result_sha256=None,
                validation=[],
                validation_receipt_sha256=None,
                validation_artifact_refs={},
                acceptance_proof_sha256=None,
                last_outcome="local_repair_required",
                next_action=instruction,
                state_delta={
                    "changed_facts": ["operator requested repair, preserving authority"],
                    "candidate": {
                        key: candidate[key]
                        for key in ("base_oid", "head_oid", "tree_oid", "patch_sha256", "clean")
                    },
                    "git": None,
                    "validation_state_changes": [],
                    "discoveries": [row["summary"] for row in discoveries],
                    "unresolved_decisions": [],
                    "next_action": instruction,
                },
            )

    def review_candidate(self, **kwargs: Any) -> Any:
        """Run one independent EC-05 review of this capsule's cumulative candidate."""

        from bearhug.campaign.capsule_review_runtime import run_capsule_review

        with self._candidate_operation():
            if self._budget_guard() is not None:
                raise CapsuleRuntimeError("approved capsule budget blocks review")
            return run_capsule_review(self, **kwargs)

    # The short spelling is useful to the phase adapter while retaining an explicit candidate
    # spelling for callers that also work with v1 campaign reviews.
    review = review_candidate

    def accept_candidate(
        self,
        *,
        proof: Mapping[str, Any],
        integration_refs: Iterable[str] = (),
        dependency_acceptance_bundles: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._candidate_operation():
            return self._accept_candidate(
                proof=proof,
                integration_refs=integration_refs,
                dependency_acceptance_bundles=dependency_acceptance_bundles,
            )

    def _accept_candidate(
        self,
        *,
        proof: Mapping[str, Any],
        integration_refs: Iterable[str] = (),
        dependency_acceptance_bundles: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Accept a reviewed candidate only after the independent proof verifier closes every gate.

        Integration/promotion remains outside this method.  The proof is checked against this
        runtime's durable packet, receipt chain, review checkout and provider custody; a caller
        supplied boolean or result projection is never accepted as authority.
        """

        integration_refs = list(integration_refs)

        if self.state != "candidate_ready":
            raise CapsuleRuntimeError(f"capsule state {self.state!r} is not ready for acceptance")
        current = self.result
        if current is None:
            raise CapsuleRuntimeError("capsule has no durable candidate result")
        if not self._state.get("review_refs"):
            raise CapsuleRuntimeError("capsule candidate has no independent review evidence")
        try:
            receipts = self._state.get("provider_receipts") or []
            candidate, _paths = self._candidate(receipts)
        except Exception as exc:
            raise CapsuleRuntimeError(
                f"candidate custody is unavailable for acceptance: {exc}"
            ) from exc
        if candidate is None or current.get("candidate") != {
            key: candidate[key]
            for key in ("base_oid", "head_oid", "tree_oid", "patch_sha256", "clean")
        }:
            raise CapsuleRuntimeError("candidate changed after review")
        try:
            from bearhug.campaign.capsule_review import verify_capsule_acceptance

            verified = verify_capsule_acceptance(
                **self.acceptance_bundle(proof),
                dependency_acceptance_bundles=dependency_acceptance_bundles,
            )
        except Exception as exc:
            raise CapsuleRuntimeError(f"capsule result verifier rejected candidate: {exc}") from exc
        if not isinstance(verified, Mapping) or verified.get("accepted") is not True:
            raise CapsuleRuntimeError(
                "capsule result verifier did not confirm all required evidence"
            )
        proof_digest = self._put_blob(
            json.dumps(
                proof, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        refs = sorted(
            set(self._state.get("review_refs") or [])
            | {proof_digest, verified["packet_sha256"], *verified["reviewer_receipt_sha256s"]}
        )
        refs.extend(_sha(ref, "integration evidence reference") for ref in integration_refs)
        updated = copy.deepcopy(current)
        updated["status"] = "accepted"
        updated["review_refs"] = sorted(set(refs))
        updated["integration_refs"] = sorted(set(integration_refs))
        try:
            validate_capsule_result(updated)
        except CapsuleContractError as exc:
            raise CapsuleRuntimeError(f"accepted capsule result is invalid: {exc}") from exc
        digest = self.objects.put(updated)
        self._heartbeat()
        self._transition(
            "capsule_completed",
            evidence_refs=refs,
            state="completed",
            result_sha256=digest,
            acceptance_proof_sha256=proof_digest,
            last_outcome="candidate_ready",
            next_action=(
                "candidate is accepted; integration and promotion remain separate authorities"
            ),
        )
        return copy.deepcopy(updated)


CapsuleEpisodeRuntime = CapsuleRuntime
ExecutionCapsuleRuntime = CapsuleRuntime

__all__ = [
    "CapsuleEpisodeRuntime",
    "CapsuleRuntime",
    "CapsuleRuntimeError",
    "CapsuleRuntimeRecoveryError",
    "EpisodeOutcome",
    "ExecutionCapsuleRuntime",
    "LeaseFenceLost",
]
