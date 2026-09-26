"""Durable, transactional leases for campaign claim sets.

The database belongs to Bear Hug, not to the subject repository.  Callers must supply its state
root explicitly.  A lease is a fencing record rather than a process-liveness oracle: expiry moves
an active lease to ``orphaned`` and an orphan continues to block its entire claim set until a
separately authorized HIL resolution token is consumed.

The store validates a closed authorization supplied by the trusted campaign controller.  It binds
that authorization to the controller, run, lease, question event, answer, and one-use token, but it
does not read or authenticate the controller journal itself.

This module deliberately does not launch providers, create worktrees, or infer campaign inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from bearhug.paths import ARTIFACT_ROOT, REPO_ROOT, WriteBoundaryError, assert_writable

_STATE_ROOT_RULE = (
    "campaign lease state root must be inside Bear Hug, its artifact root "
    "or the system temporary root"
)


class CampaignLeaseError(RuntimeError):
    """The lease request or durable store cannot be trusted."""


class CampaignLeaseConflict(CampaignLeaseError):
    """Another active or orphaned lease owns overlapping scope."""

    def __init__(self, message: str, *, lease_id: str, state: str, dimension: str) -> None:
        super().__init__(message)
        self.lease_id = lease_id
        self.state = state
        self.dimension = dimension


class CampaignLeaseStateError(CampaignLeaseError):
    """A requested lease transition is not valid for its durable state."""


@dataclass(frozen=True, slots=True)
class LeaseIdentity:
    """The complete fencing identity required for owner-directed transitions."""

    lease_id: str
    campaign_id: str
    run_id: str
    session_id: str
    claimant_id: str
    controller_authority_sha256: str
    repository_common_dir_sha256: str
    worktree_sha256: str
    branch: str
    base_oid: str
    pid: int
    host: str
    epoch: int


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    """One lease snapshot and its complete atomic claim set."""

    identity: LeaseIdentity
    state: Literal["active", "orphaned", "released"]
    acquired_at: float
    heartbeat_at: float
    expires_at: float
    released_at: float | None
    release_reason: str | None
    claim_set: dict[str, Any]
    resolution_authorization_id: str | None = None
    resolution_hil_ref: str | None = None
    resolution_question_sha256: str | None = None
    resolution_answer_sha256: str | None = None
    resolution_authorized_at: float | None = None


_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,4096}$")
_SUBJECT_LITERAL = re.compile(r"^[A-Za-z0-9_:/-]+$")
_LEASE_ID = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HIL_CANONICAL_ALGORITHM = "bearhug-orphan-resolution-authorization-json-sha256/1"
_APPLICATION_ID = 0x42484C53  # BHLS
_SCHEMA_VERSION = 1
_DATABASE_NAME = "campaign-leases.sqlite3"
_LIVE_STATES = ("active", "orphaned")
_CLAIM_FIELDS = frozenset(
    {
        "claim_set_id",
        "path_prefixes",
        "symbols",
        "subjects",
        "semantic_resources",
        "ports",
        "data_directories",
    }
)
_EXPECTED_COLUMNS = {
    "metadata": ("key", "value"),
    "leases": (
        "epoch",
        "lease_id",
        "campaign_id",
        "run_id",
        "session_id",
        "claimant_id",
        "claim_set_id",
        "controller_authority_sha256",
        "repository_common_dir_sha256",
        "worktree_sha256",
        "branch",
        "base_oid",
        "pid",
        "host",
        "state",
        "acquired_at",
        "heartbeat_at",
        "expires_at",
        "released_at",
        "release_reason",
        "resolution_authorization_id",
        "resolution_token_sha256",
        "resolution_hil_ref",
        "resolution_question_sha256",
        "resolution_answer_sha256",
        "resolution_authorized_at",
        "resolved_by_token_sha256",
    ),
    "claims": ("lease_id", "dimension", "value", "aux"),
}
_DDL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
) STRICT;
CREATE TABLE leases (
    epoch INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id TEXT NOT NULL UNIQUE,
    campaign_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    claimant_id TEXT NOT NULL,
    claim_set_id TEXT NOT NULL,
    controller_authority_sha256 TEXT NOT NULL,
    repository_common_dir_sha256 TEXT NOT NULL,
    worktree_sha256 TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_oid TEXT NOT NULL,
    pid INTEGER NOT NULL CHECK (pid > 0),
    host TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'orphaned', 'released')),
    acquired_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    released_at REAL,
    release_reason TEXT,
    resolution_authorization_id TEXT,
    resolution_token_sha256 TEXT,
    resolution_hil_ref TEXT,
    resolution_question_sha256 TEXT,
    resolution_answer_sha256 TEXT,
    resolution_authorized_at REAL,
    resolved_by_token_sha256 TEXT,
    CHECK (heartbeat_at >= acquired_at),
    CHECK (expires_at > heartbeat_at),
    CHECK ((state = 'released') = (released_at IS NOT NULL)),
    CHECK ((state = 'released') = (release_reason IS NOT NULL)),
    CHECK (released_at IS NULL OR released_at >= heartbeat_at),
    CHECK (
        (resolution_authorization_id IS NULL AND resolution_token_sha256 IS NULL
            AND resolution_hil_ref IS NULL AND resolution_question_sha256 IS NULL
            AND resolution_answer_sha256 IS NULL AND resolution_authorized_at IS NULL)
        OR
        (resolution_authorization_id IS NOT NULL AND resolution_token_sha256 IS NOT NULL
            AND resolution_hil_ref IS NOT NULL AND resolution_question_sha256 IS NOT NULL
            AND resolution_answer_sha256 IS NOT NULL AND resolution_authorized_at IS NOT NULL)
    ),
    CHECK (resolved_by_token_sha256 IS NULL OR state = 'released'),
    CHECK (
        resolved_by_token_sha256 IS NULL
        OR resolved_by_token_sha256 = resolution_token_sha256
    ),
    CHECK (
        state != 'released' OR resolution_authorization_id IS NULL
        OR resolved_by_token_sha256 IS NOT NULL
    )
) STRICT;
CREATE TABLE claims (
    lease_id TEXT NOT NULL REFERENCES leases(lease_id) ON DELETE RESTRICT,
    dimension TEXT NOT NULL CHECK (
        dimension IN ('path_prefix', 'symbol', 'subject', 'semantic_resource',
                      'port', 'data_directory')
    ),
    value TEXT NOT NULL,
    aux TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (lease_id, dimension, value, aux)
) WITHOUT ROWID, STRICT;
CREATE INDEX leases_state_expiry ON leases(state, expires_at);
CREATE INDEX claims_dimension_value ON claims(dimension, value, aux);
CREATE UNIQUE INDEX resolution_token_once ON leases(resolution_token_sha256)
    WHERE resolution_token_sha256 IS NOT NULL;
CREATE UNIQUE INDEX resolution_authorization_once ON leases(resolution_authorization_id)
    WHERE resolution_authorization_id IS NOT NULL;
CREATE UNIQUE INDEX resolution_question_event_once
    ON leases(controller_authority_sha256, campaign_id, run_id, resolution_hil_ref)
    WHERE resolution_hil_ref IS NOT NULL;
"""
_SCHEMA_FINGERPRINT = hashlib.sha256(_DDL.encode("utf-8")).hexdigest()

_HIL_AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "canonical_algorithm",
        "controller_authority_sha256",
        "campaign_id",
        "run_id",
        "lease_id",
        "checkpoint_ref",
        "question_sha256",
        "answer_sha256",
        "disposition",
        "authorized_at",
        "resolution_token_sha256",
        "content_sha256",
    }
)
_CONTROLLER_ISSUER = object()


class _ControllerOrphanResolutionAuthorization:
    """Opaque in-process proof that a controller replayed its own HIL journal."""

    __slots__ = ("_encoded",)

    def __init__(self, value: Mapping[str, Any], issuer: object) -> None:
        if issuer is not _CONTROLLER_ISSUER:
            raise CampaignLeaseError("orphan resolution authority must be issued by a controller")
        checked = validate_orphan_resolution_authorization(value)
        self._encoded = _canonical_json(checked)


def release_orphaned_lease(
    store: CampaignLeaseStore,
    *,
    lease_id: str,
    reason: str,
    answer: str,
    checkpoint_ref: str = "operator/orphan-resolution",
    now: float | None = None,
    sweep_expired: bool = True,
) -> LeaseRecord:
    """Release one orphaned lease through the designed controller HIL path.

    `authorize_orphan_resolution` and `resolve_orphan` were implemented and tested with no
    caller anywhere outside tests, so the one supported way out of an orphaned lease could
    not be reached. Measured 2026-09-17 on row 240 T2: the orphan blocked `retire` for
    custody while `recover`, `recover --lease-id` and `control stop` all refused it, and the
    campaign was abandoned with its evidence stranded.

    Nothing is loosened. The lease must already be `orphaned`, which means expired and swept,
    with no owner left to dispossess; an active or expired-but-unswept lease still belongs to
    its owner and is refused here. The two-call authorize-then-resolve shape is unchanged, the
    authorization is still content-addressed and bound to the orphan's own durable
    controller authority, campaign and run, and the token is minted fresh and never returned,
    so only its digest reaches the store.

    ``answer`` is the operator's recorded decision, digested into the authorization as the
    HIL answer. It is evidence of who released the lease, not a password.

    ``sweep_expired`` controls only whether the two calls below perform their own store-wide
    expiry sweep, exactly as they always have (default ``True``); it authorizes nothing new
    and loosens no check. The one caller that passes ``False`` (the operator's
    ``recover --resolve-orphan`` route) has already swept the exact lease this call requires
    to be orphaned through the store's own targeted, single-lease sweep, so the store-wide
    sweep below would only ever reach *other* leases on this shared repository -- never this
    one, since this call already requires it to be `orphaned` before doing anything.
    """

    record = store.get(lease_id)
    if record.state != "orphaned":
        raise CampaignLeaseStateError(
            f"only an orphaned lease can be released this way; {lease_id} is {record.state}"
        )
    timestamp = _instant(now)
    token = "bearhug-orphan-resolution-" + secrets.token_hex(32)
    authorization = build_orphan_resolution_authorization(
        controller_authority_sha256=record.identity.controller_authority_sha256,
        campaign_id=record.identity.campaign_id,
        run_id=record.identity.run_id,
        lease_id=lease_id,
        checkpoint_ref=checkpoint_ref,
        question_sha256=hashlib.sha256(
            f"release orphaned lease {lease_id}?".encode()
        ).hexdigest(),
        answer_sha256=hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        disposition="approve",
        authorized_at=timestamp,
        resolution_token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )
    store.authorize_orphan_resolution(
        authorization=_issue_controller_orphan_resolution_authorization(authorization),
        resolution_token=token,
        sweep_expired=sweep_expired,
    )
    return store.resolve_orphan(
        lease_id, resolution_token=token, reason=reason, resolved_at=timestamp,
        sweep_expired=sweep_expired,
    )


def _issue_controller_orphan_resolution_authorization(
    value: Mapping[str, Any],
) -> _ControllerOrphanResolutionAuthorization:
    return _ControllerOrphanResolutionAuthorization(value, _CONTROLLER_ISSUER)


def _normalized_sql(value: str) -> str:
    return " ".join(value.split())


def _expected_schema_objects() -> dict[tuple[str, str], str]:
    expected: dict[tuple[str, str], str] = {}
    pattern = re.compile(
        r"^CREATE\s+(?:UNIQUE\s+)?(?P<type>TABLE|INDEX)\s+(?P<name>[a-z_]+)\b",
        re.IGNORECASE,
    )
    for raw in _DDL.split(";"):
        statement = raw.strip()
        if not statement:
            continue
        match = pattern.match(statement)
        if match is None:
            raise RuntimeError("lease DDL contains an unrecognized statement")
        key = (match.group("type").lower(), match.group("name"))
        if key in expected:
            raise RuntimeError(f"lease DDL repeats schema object {key!r}")
        expected[key] = _normalized_sql(statement)
    return expected


_EXPECTED_SCHEMA_OBJECTS = _expected_schema_objects()


def _token(value: Any, field: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise CampaignLeaseError(f"{field} must be a canonical lowercase token")
    return value


def _safe_text(value: Any, field: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or _SAFE_TEXT.fullmatch(value) is None
        or value != value.strip()
    ):
        raise CampaignLeaseError(f"{field} must be nonempty printable text")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CampaignLeaseError(f"{field} must be a positive integer")
    return value


def _instant(value: float | None) -> float:
    result = time.time() if value is None else value
    if (
        isinstance(result, bool)
        or not isinstance(result, (int, float))
        or not math.isfinite(result)
        or result < 0
    ):
        raise CampaignLeaseError("time must be a nonnegative finite Unix timestamp")
    return float(result)


def _ttl(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CampaignLeaseError("ttl_seconds must be finite")
    if value <= 0 or value > 7 * 24 * 60 * 60:
        raise CampaignLeaseError("ttl_seconds must be greater than zero and at most seven days")
    return float(value)


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CampaignLeaseError(f"{field} must be lowercase SHA-256")
    return value


def _git_oid(value: Any, field: str) -> str:
    if not isinstance(value, str) or _GIT_OID.fullmatch(value) is None:
        raise CampaignLeaseError(f"{field} must be a full lowercase Git object id")
    return value


def _branch(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value != value.strip()
        or value.startswith("refs/heads/")
        or value.casefold() == "head"
        or value.startswith("/")
        or value.endswith(("/", "."))
        or "//" in value
        or ".." in value
        or "@{" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in " ~^:?*[\\" for character in value)
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    ):
        raise CampaignLeaseError("branch must be one canonical short Git branch name")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CampaignLeaseError(f"authorization is not canonical JSON: {exc}") from exc


def _authorization_digest(value: Mapping[str, Any]) -> str:
    material = dict(value)
    material.pop("content_sha256", None)
    return hashlib.sha256(_canonical_json(material)).hexdigest()


def build_orphan_resolution_authorization(
    *,
    controller_authority_sha256: str,
    campaign_id: str,
    run_id: str,
    lease_id: str,
    checkpoint_ref: str,
    question_sha256: str,
    answer_sha256: str,
    disposition: Literal["approve", "deny"],
    authorized_at: float,
    resolution_token_sha256: str,
) -> dict[str, Any]:
    """Build the closed content-addressed controller decision consumed by the lease store."""

    if not isinstance(lease_id, str) or _LEASE_ID.fullmatch(lease_id) is None:
        raise CampaignLeaseError("lease_id must be lowercase SHA-256-shaped hex")
    if disposition not in {"approve", "deny"}:
        raise CampaignLeaseError("HIL disposition must be approve or deny")
    value: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "campaign_orphan_resolution_authorization",
        "canonical_algorithm": _HIL_CANONICAL_ALGORITHM,
        "controller_authority_sha256": _sha256(
            controller_authority_sha256, "controller_authority_sha256"
        ),
        "campaign_id": _token(campaign_id, "campaign_id"),
        "run_id": _token(run_id, "run_id"),
        "lease_id": lease_id,
        "checkpoint_ref": _safe_text(checkpoint_ref, "checkpoint_ref", maximum=1024),
        "question_sha256": _sha256(question_sha256, "question_sha256"),
        "answer_sha256": _sha256(answer_sha256, "answer_sha256"),
        "disposition": disposition,
        "authorized_at": _instant(authorized_at),
        "resolution_token_sha256": _sha256(resolution_token_sha256, "resolution_token_sha256"),
    }
    value["content_sha256"] = _authorization_digest(value)
    return value


def validate_orphan_resolution_authorization(value: Any) -> dict[str, Any]:
    """Validate a controller-authored HIL record without coercing or trusting its content hash."""

    if not isinstance(value, Mapping) or set(value) != _HIL_AUTHORIZATION_FIELDS:
        raise CampaignLeaseError("HIL authorization has missing or unknown fields")
    expected = build_orphan_resolution_authorization(
        controller_authority_sha256=value["controller_authority_sha256"],
        campaign_id=value["campaign_id"],
        run_id=value["run_id"],
        lease_id=value["lease_id"],
        checkpoint_ref=value["checkpoint_ref"],
        question_sha256=value["question_sha256"],
        answer_sha256=value["answer_sha256"],
        disposition=value["disposition"],
        authorized_at=value["authorized_at"],
        resolution_token_sha256=value["resolution_token_sha256"],
    )
    if _canonical_json(dict(value)) != _canonical_json(expected):
        raise CampaignLeaseError("HIL authorization content digest or canonical values mismatch")
    return expected


def _path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CampaignLeaseError(f"{field} contains an invalid path")
    if value == ".":
        return value
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or value.endswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or candidate.as_posix() != value
    ):
        raise CampaignLeaseError(f"{field} must be a canonical repository-relative path")
    return value


def _path_overlap(left: str, right: str) -> bool:
    return (
        left == "."
        or right == "."
        or left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


def _subject(value: Any) -> str:
    text = _safe_text(value, "NATS subject", maximum=512)
    tokens = text.split(".")
    if any(not token for token in tokens):
        raise CampaignLeaseError("NATS subject cannot contain empty tokens")
    for index, token in enumerate(tokens):
        if token == ">":
            if index != len(tokens) - 1:
                raise CampaignLeaseError("NATS > wildcard must be the final token")
        elif token != "*" and _SUBJECT_LITERAL.fullmatch(token) is None:
            raise CampaignLeaseError("NATS wildcards must occupy a complete token")
    return text


def _subjects_conflict(left: str, right: str) -> tuple[bool, bool]:
    """Return (conflict, uncertain); establish disjointness only before a wildcard."""

    if left == right:
        return True, False
    for left_token, right_token in zip(left.split("."), right.split("."), strict=False):
        if left_token in {"*", ">"} or right_token in {"*", ">"}:
            return True, True
        if left_token != right_token:
            return False, False
    # Different literal lengths are disjoint. A terminal '>' would already have been observed.
    return False, False


def _array(value: Any, field: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CampaignLeaseError(f"{field} must be an array")
    return list(value)


def _canonical_string_set(value: Any, field: str, validator: Any) -> tuple[str, ...]:
    items = [validator(item) for item in _array(value, field)]
    if len(items) != len(set(items)):
        raise CampaignLeaseError(f"{field} contains duplicate values")
    return tuple(sorted(items))


def _canonical_claim_set(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    if not isinstance(value, Mapping) or set(value) != _CLAIM_FIELDS:
        raise CampaignLeaseError("claim_set has missing or unknown fields")
    claim_id = _token(value["claim_set_id"], "claim_set_id")
    paths = _canonical_string_set(
        value["path_prefixes"], "path_prefixes", lambda item: _path(item, "path prefix")
    )
    symbols = _canonical_string_set(
        value["symbols"], "symbols", lambda item: _safe_text(item, "symbol")
    )
    subjects = _canonical_string_set(value["subjects"], "subjects", _subject)
    resources = _canonical_string_set(
        value["semantic_resources"],
        "semantic_resources",
        lambda item: _token(item, "semantic resource"),
    )
    data_dirs = _canonical_string_set(
        value["data_directories"],
        "data_directories",
        lambda item: _path(item, "data directory"),
    )
    raw_ports = _array(value["ports"], "ports")
    ports: list[dict[str, Any]] = []
    port_keys: list[tuple[str, int, str]] = []
    for item in raw_ports:
        if not isinstance(item, Mapping) or set(item) != {"transport", "port", "bind_scope"}:
            raise CampaignLeaseError("port claim has missing or unknown fields")
        transport = item["transport"]
        scope = item["bind_scope"]
        if transport not in {"tcp", "udp"} or scope not in {"loopback", "host", "network"}:
            raise CampaignLeaseError("port claim has invalid transport or bind_scope")
        port = _positive_int(item["port"], "port")
        if port > 65535:
            raise CampaignLeaseError("port must not exceed 65535")
        port_keys.append((transport, port, scope))
    if len(port_keys) != len(set(port_keys)):
        raise CampaignLeaseError("ports contains duplicate values")
    for transport, port, scope in sorted(port_keys):
        ports.append({"transport": transport, "port": port, "bind_scope": scope})
    if not any((paths, symbols, subjects, resources, ports, data_dirs)):
        raise CampaignLeaseError("atomic claim_set must contain at least one claim")

    canonical = {
        "claim_set_id": claim_id,
        "path_prefixes": list(paths),
        "symbols": list(symbols),
        "subjects": list(subjects),
        "semantic_resources": list(resources),
        "ports": ports,
        "data_directories": list(data_dirs),
    }
    rows = [("path_prefix", item, "") for item in paths]
    rows += [("symbol", item, "") for item in symbols]
    rows += [("subject", item, "") for item in subjects]
    rows += [("semantic_resource", item, "") for item in resources]
    rows += [("data_directory", item, "") for item in data_dirs]
    rows += [("port", f"{transport}:{port}", scope) for transport, port, scope in port_keys]
    return canonical, rows


class CampaignLeaseStore:
    """A SQLite/WAL lease authority rooted outside the subject checkout."""

    def __init__(
        self,
        state_root: Path | str,
        *,
        subject_root: Path | str,
        repository_common_dir_sha256: str,
        busy_timeout_seconds: float = 5.0,
        read_only: bool = False,
    ) -> None:
        subject_requested = Path(subject_root).expanduser()
        if subject_requested.is_symlink():
            raise CampaignLeaseError("campaign subject root must not be a symlink")
        try:
            self.subject_root = subject_requested.resolve(strict=True)
        except OSError as exc:
            raise CampaignLeaseError("campaign subject root must be an existing directory") from exc
        if not self.subject_root.is_dir():
            raise CampaignLeaseError("campaign subject root must be an existing directory")
        self.subject_root_sha256 = hashlib.sha256(os.fsencode(self.subject_root)).hexdigest()
        self.repository_common_dir_sha256 = _sha256(
            repository_common_dir_sha256, "repository_common_dir_sha256"
        )

        requested = Path(state_root).expanduser()
        if requested.is_symlink():
            raise CampaignLeaseError("campaign lease state root must not be a symlink")
        self.read_only = bool(read_only)
        if self.read_only:
            # A write boundary answers "may this process write here", which is the wrong
            # question for an observer. Asking it anyway made every reader rooted elsewhere --
            # the dashboard, a status probe -- report a live campaign as blocked custody.
            try:
                self.root = requested.resolve(strict=True)
            except OSError as exc:
                raise CampaignLeaseError(
                    "campaign lease state root must be an existing directory"
                ) from exc
        else:
            try:
                self.root = assert_writable(requested)
            except WriteBoundaryError as exc:
                raise CampaignLeaseError(_STATE_ROOT_RULE) from exc
            # The same custody boundary as ``assert_writable``: the checkout, the machine-local
            # artifact root an installed project defaults to, or system scratch.
            allowed_roots = (
                REPO_ROOT.resolve(), ARTIFACT_ROOT.resolve(), Path(tempfile.gettempdir()).resolve()
            )
            if not any(
                self.root == allowed or allowed in self.root.parents for allowed in allowed_roots
            ):
                raise CampaignLeaseError(_STATE_ROOT_RULE)
        if (
            self.root == self.subject_root
            or self.subject_root in self.root.parents
            or self.root in self.subject_root.parents
        ):
            raise CampaignLeaseError(
                "campaign lease state root and subject checkout must be disjoint"
            )
        if not self.read_only:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._root_identity = self._inspect_root()
        if (
            isinstance(busy_timeout_seconds, bool)
            or not isinstance(busy_timeout_seconds, (int, float))
            or busy_timeout_seconds <= 0
            or not math.isfinite(busy_timeout_seconds)
        ):
            raise CampaignLeaseError("busy timeout must be finite and greater than zero")
        self.busy_timeout_ms = max(1, round(busy_timeout_seconds * 1000))
        self.path = self.root / _DATABASE_NAME
        self._ensure_database_file()
        self._database_identity = self._inspect_database()
        self._initialize_or_verify()

    def _inspect_root(self) -> tuple[int, int]:
        try:
            metadata = os.stat(self.root, follow_symlinks=False)
        except OSError as exc:
            raise CampaignLeaseError("campaign lease state root is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise CampaignLeaseError("campaign lease state root is not a physical directory")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CampaignLeaseError("campaign lease state root must be owner-only")
        return metadata.st_dev, metadata.st_ino

    def _inspect_database(self) -> tuple[int, int]:
        try:
            metadata = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise CampaignLeaseError("campaign lease database is unavailable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise CampaignLeaseError("campaign lease database must be one owner-only physical file")
        return metadata.st_dev, metadata.st_ino

    def _ensure_database_file(self) -> None:
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(self.root, directory_flags)
        except OSError as exc:
            raise CampaignLeaseError("cannot open campaign lease state root safely") from exc
        try:
            if (
                os.fstat(directory_fd).st_dev,
                os.fstat(directory_fd).st_ino,
            ) != self._root_identity:
                raise CampaignLeaseError("campaign lease state root identity changed")
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(_DATABASE_NAME, flags, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                descriptor = None
            except OSError as exc:
                raise CampaignLeaseError("cannot create campaign lease database safely") from exc
            if descriptor is not None:
                os.close(descriptor)
        finally:
            os.close(directory_fd)

    def _verify_file_custody(self) -> None:
        if self._inspect_root() != self._root_identity:
            raise CampaignLeaseError("campaign lease state root identity changed")
        if self._inspect_database() != self._database_identity:
            raise CampaignLeaseError("campaign lease database identity changed")

    def _connect(self) -> sqlite3.Connection:
        deadline = time.monotonic() + self.busy_timeout_ms / 1000
        while True:
            connection: sqlite3.Connection | None = None
            try:
                self._verify_file_custody()
                connection = sqlite3.connect(
                    f"{self.path.as_uri()}?mode=rw&nofollow=1",
                    timeout=self.busy_timeout_ms / 1000,
                    isolation_level=None,
                    uri=True,
                )
                self._verify_file_custody()
                connection.row_factory = sqlite3.Row
                connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
                connection.execute("PRAGMA foreign_keys = ON")
                mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise CampaignLeaseError("campaign lease database did not enter WAL mode")
                self._verify_file_custody()
                return connection
            except sqlite3.OperationalError as exc:
                if connection is not None:
                    connection.close()
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise CampaignLeaseError(f"cannot open campaign lease database: {exc}") from exc
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            except (OSError, sqlite3.Error) as exc:
                if connection is not None:
                    connection.close()
                raise CampaignLeaseError(f"cannot open campaign lease database: {exc}") from exc
            except CampaignLeaseError:
                if connection is not None:
                    connection.close()
                raise

    def _initialize_or_verify(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
                    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
                    if application_id not in {0, _APPLICATION_ID}:
                        raise CampaignLeaseError(
                            "unversioned database belongs to another application"
                        )
                    existing = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                    if existing:
                        raise CampaignLeaseError("unversioned campaign lease database is not empty")
                    for statement in _DDL.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES (?, ?)",
                        ("schema_sha256", _SCHEMA_FINGERPRINT),
                    )
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES (?, ?)",
                        ("subject_root_sha256", self.subject_root_sha256),
                    )
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES (?, ?)",
                        (
                            "repository_common_dir_sha256",
                            self.repository_common_dir_sha256,
                        ),
                    )
                self._verify(connection)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise CampaignLeaseError(f"cannot initialize campaign lease database: {exc}") from exc
        finally:
            connection.close()
        self._verify_file_custody()

    def _verify(self, connection: sqlite3.Connection) -> None:
        try:
            if connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
                raise CampaignLeaseError("campaign lease database has the wrong application id")
            if connection.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                raise CampaignLeaseError("unsupported campaign lease database schema version")
            integrity = connection.execute("PRAGMA quick_check(1)").fetchone()[0]
            if integrity != "ok":
                raise CampaignLeaseError(
                    f"campaign lease database failed integrity check: {integrity}"
                )
            objects = connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            actual_objects: dict[tuple[str, str], str] = {}
            for row in objects:
                if row["type"] not in {"table", "index"} or not isinstance(row["sql"], str):
                    raise CampaignLeaseError(
                        "campaign lease database contains an unexpected schema object"
                    )
                actual_objects[(row["type"], row["name"])] = _normalized_sql(row["sql"])
            if actual_objects != _EXPECTED_SCHEMA_OBJECTS:
                raise CampaignLeaseError("campaign lease database schema SQL has drifted")
            for table, expected in _EXPECTED_COLUMNS.items():
                columns = tuple(
                    row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
                )
                if columns != expected:
                    raise CampaignLeaseError(f"campaign lease table {table} has schema drift")
            foreign_keys = connection.execute("PRAGMA foreign_key_list(claims)").fetchall()
            observed_foreign_keys = [
                (
                    row["table"],
                    row["from"],
                    row["to"],
                    row["on_update"],
                    row["on_delete"],
                    row["match"],
                )
                for row in foreign_keys
            ]
            if observed_foreign_keys != [
                ("leases", "lease_id", "lease_id", "NO ACTION", "RESTRICT", "NONE")
            ]:
                raise CampaignLeaseError("campaign lease foreign-key authority has drifted")
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM metadata ORDER BY key")
            }
            expected_metadata = {
                "repository_common_dir_sha256": self.repository_common_dir_sha256,
                "schema_sha256": _SCHEMA_FINGERPRINT,
                "subject_root_sha256": self.subject_root_sha256,
            }
            if metadata != expected_metadata:
                raise CampaignLeaseError(
                    "campaign lease metadata binding is missing, changed, "
                    "or contains unknown authority"
                )
        except sqlite3.Error as exc:
            raise CampaignLeaseError(f"cannot verify campaign lease database: {exc}") from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._verify(connection)
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        except sqlite3.Error as exc:
            raise CampaignLeaseError(f"campaign lease transaction failed: {exc}") from exc
        finally:
            connection.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            try:
                self._verify(connection)
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        except sqlite3.Error as exc:
            raise CampaignLeaseError(f"campaign lease read transaction failed: {exc}") from exc
        finally:
            connection.close()

    def _orphan_expired(
        self,
        connection: sqlite3.Connection,
        now: float,
        exclude: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        rows = [
            row
            for row in connection.execute(
                "SELECT lease_id FROM leases WHERE state = 'active' AND expires_at <= ? "
                "ORDER BY lease_id",
                (now,),
            ).fetchall()
            if row["lease_id"] not in exclude
        ]
        for row in rows:
            self._read_record(connection, row["lease_id"])
        for row in rows:
            connection.execute(
                "UPDATE leases SET state = 'orphaned' WHERE lease_id = ? AND state = 'active'",
                (row["lease_id"],),
            )
        return tuple(row["lease_id"] for row in rows)

    @staticmethod
    def _claims(connection: sqlite3.Connection, lease_id: str) -> list[tuple[str, str, str]]:
        return [
            (row["dimension"], row["value"], row["aux"])
            for row in connection.execute(
                "SELECT dimension, value, aux FROM claims WHERE lease_id = ? "
                "ORDER BY dimension, value, aux",
                (lease_id,),
            )
        ]

    @staticmethod
    def _collision(
        candidate: list[tuple[str, str, str]], existing: list[tuple[str, str, str]]
    ) -> tuple[str, str] | None:
        filesystem_dimensions = {"path_prefix", "data_directory"}
        candidate_paths = [value for kind, value, _ in candidate if kind in filesystem_dimensions]
        existing_paths = [value for kind, value, _ in existing if kind in filesystem_dimensions]
        for left in candidate_paths:
            for right in existing_paths:
                if _path_overlap(left, right):
                    return "filesystem_path", f"{left!r} overlaps {right!r}"
        for dimension in ("symbol", "semantic_resource"):
            left = {value for kind, value, _ in candidate if kind == dimension}
            right = {value for kind, value, _ in existing if kind == dimension}
            overlap = sorted(left & right)
            if overlap:
                return dimension, repr(overlap[0])
        left_ports = {value for kind, value, _ in candidate if kind == "port"}
        right_ports = {value for kind, value, _ in existing if kind == "port"}
        ports = sorted(left_ports & right_ports)
        if ports:
            return "port", repr(ports[0])
        for left in (value for kind, value, _ in candidate if kind == "subject"):
            for right in (value for kind, value, _ in existing if kind == "subject"):
                conflict, uncertain = _subjects_conflict(left, right)
                if conflict:
                    detail = f"{left!r} may overlap {right!r}" if uncertain else repr(left)
                    return "subject_unknown" if uncertain else "subject", detail
        return None

    def acquire(
        self,
        *,
        campaign_id: str,
        run_id: str,
        session_id: str,
        claimant_id: str,
        controller_authority_sha256: str,
        repository_common_dir_sha256: str,
        worktree_sha256: str,
        branch: str,
        base_oid: str,
        claim_set: Mapping[str, Any],
        ttl_seconds: float,
        pid: int | None = None,
        host: str | None = None,
        now: float | None = None,
    ) -> LeaseRecord:
        """Acquire every claim in one transaction, or acquire nothing."""

        if self.read_only:
            raise CampaignLeaseError("this campaign lease store is open read-only")
        campaign = _token(campaign_id, "campaign_id")
        run = _token(run_id, "run_id")
        session = _token(session_id, "session_id")
        claimant = _token(claimant_id, "claimant_id")
        controller_authority = _sha256(controller_authority_sha256, "controller_authority_sha256")
        common_dir_identity = _sha256(repository_common_dir_sha256, "repository_common_dir_sha256")
        if common_dir_identity != self.repository_common_dir_sha256:
            raise CampaignLeaseError(
                "repository_common_dir_sha256 does not match the subject-scoped lease store"
            )
        worktree_identity = _sha256(worktree_sha256, "worktree_sha256")
        branch_name = _branch(branch)
        base = _git_oid(base_oid, "base_oid")
        process_id = _positive_int(os.getpid() if pid is None else pid, "pid")
        hostname = _safe_text(socket.gethostname() if host is None else host, "host", maximum=255)
        canonical, claims = _canonical_claim_set(claim_set)
        timestamp = _instant(now)
        duration = _ttl(ttl_seconds)
        lease_id = secrets.token_hex(32)
        conflict: CampaignLeaseConflict | None = None
        record: LeaseRecord | None = None
        with self._transaction() as connection:
            self._orphan_expired(connection, timestamp)
            for row in connection.execute(
                "SELECT lease_id, state FROM leases WHERE state IN ('active', 'orphaned') "
                "ORDER BY epoch"
            ):
                existing = self._read_record(connection, row["lease_id"])
                same_worktree = existing.identity.worktree_sha256 == worktree_identity
                same_branch = existing.identity.branch == branch_name
                claim_collision = self._collision(claims, self._claims(connection, row["lease_id"]))
                shares_scope = same_worktree or same_branch or claim_collision is not None
                collision: tuple[str, str] | None = None
                if shares_scope:
                    if existing.identity.base_oid != base:
                        collision = (
                            "base",
                            f"{base!r} differs from live base {existing.identity.base_oid!r}",
                        )
                    elif existing.identity.controller_authority_sha256 != controller_authority:
                        collision = (
                            "controller_authority",
                            "controller authority differs across one live lease scope",
                        )
                    elif existing.identity.campaign_id != campaign:
                        collision = (
                            "campaign",
                            f"{campaign!r} differs from live campaign "
                            f"{existing.identity.campaign_id!r}",
                        )
                    elif existing.identity.run_id != run:
                        collision = (
                            "run",
                            f"{run!r} differs from live run {existing.identity.run_id!r}",
                        )
                    elif same_worktree:
                        collision = ("worktree", repr(worktree_identity))
                    elif same_branch:
                        collision = ("branch", repr(branch_name))
                    else:
                        collision = claim_collision
                if collision is not None:
                    dimension, detail = collision
                    conflict = CampaignLeaseConflict(
                        f"{dimension} {detail} is owned by {row['state']} lease {row['lease_id']}",
                        lease_id=row["lease_id"],
                        state=row["state"],
                        dimension=dimension,
                    )
                    break
            if conflict is None:
                cursor = connection.execute(
                    "INSERT INTO leases(lease_id, campaign_id, run_id, session_id, claimant_id, "
                    "claim_set_id, controller_authority_sha256, repository_common_dir_sha256, "
                    "worktree_sha256, branch, base_oid, pid, host, state, acquired_at, "
                    "heartbeat_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        lease_id,
                        campaign,
                        run,
                        session,
                        claimant,
                        canonical["claim_set_id"],
                        controller_authority,
                        common_dir_identity,
                        worktree_identity,
                        branch_name,
                        base,
                        process_id,
                        hostname,
                        timestamp,
                        timestamp,
                        timestamp + duration,
                    ),
                )
                epoch = int(cursor.lastrowid)
                connection.executemany(
                    "INSERT INTO claims(lease_id, dimension, value, aux) VALUES (?, ?, ?, ?)",
                    [(lease_id, *claim) for claim in claims],
                )
                record = LeaseRecord(
                    identity=LeaseIdentity(
                        lease_id,
                        campaign,
                        run,
                        session,
                        claimant,
                        controller_authority,
                        common_dir_identity,
                        worktree_identity,
                        branch_name,
                        base,
                        process_id,
                        hostname,
                        epoch,
                    ),
                    state="active",
                    acquired_at=timestamp,
                    heartbeat_at=timestamp,
                    expires_at=timestamp + duration,
                    released_at=None,
                    release_reason=None,
                    claim_set=canonical,
                )
        if conflict is not None:
            raise conflict
        if record is None:  # defensive: the transaction either conflicts or inserts
            raise CampaignLeaseError("campaign lease transaction produced no result")
        return record

    def _require_owner(
        self, connection: sqlite3.Connection, identity: LeaseIdentity
    ) -> sqlite3.Row:
        self._read_record(connection, identity.lease_id)
        row = connection.execute(
            "SELECT * FROM leases WHERE lease_id = ?", (identity.lease_id,)
        ).fetchone()
        if row is None:
            raise CampaignLeaseStateError("lease does not exist")
        expected = (
            identity.campaign_id,
            identity.run_id,
            identity.session_id,
            identity.claimant_id,
            identity.controller_authority_sha256,
            identity.repository_common_dir_sha256,
            identity.worktree_sha256,
            identity.branch,
            identity.base_oid,
            identity.pid,
            identity.host,
            identity.epoch,
        )
        actual = tuple(
            row[field]
            for field in (
                "campaign_id",
                "run_id",
                "session_id",
                "claimant_id",
                "controller_authority_sha256",
                "repository_common_dir_sha256",
                "worktree_sha256",
                "branch",
                "base_oid",
                "pid",
                "host",
                "epoch",
            )
        )
        if actual != expected:
            raise CampaignLeaseStateError("lease fencing identity does not match")
        return row

    def heartbeat(
        self,
        identity: LeaseIdentity,
        *,
        ttl_seconds: float,
        now: float | None = None,
    ) -> LeaseRecord:
        """Extend one live lease; a stale identity can never revive it."""

        if self.read_only:
            raise CampaignLeaseError("this campaign lease store is open read-only")
        timestamp = _instant(now)
        duration = _ttl(ttl_seconds)
        self.orphan_expired(now=timestamp)
        with self._transaction() as connection:
            row = self._require_owner(connection, identity)
            if row["state"] != "active":
                raise CampaignLeaseStateError(f"cannot heartbeat a {row['state']} lease")
            if timestamp < row["heartbeat_at"]:
                raise CampaignLeaseStateError("heartbeat time predates the durable heartbeat")
            expires_at = max(float(row["expires_at"]), timestamp + duration)
            cursor = connection.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? "
                "WHERE lease_id = ? AND epoch = ? AND state = 'active' AND heartbeat_at = ?",
                (
                    timestamp,
                    expires_at,
                    identity.lease_id,
                    identity.epoch,
                    row["heartbeat_at"],
                ),
            )
            if cursor.rowcount != 1:
                raise CampaignLeaseStateError("heartbeat lost its durable fencing race")
            return self._read_record(connection, identity.lease_id)

    def release(
        self,
        identity: LeaseIdentity,
        *,
        reason: str,
        now: float | None = None,
    ) -> LeaseRecord:
        """Release an active lease by its full fencing identity."""

        if self.read_only:
            raise CampaignLeaseError("this campaign lease store is open read-only")
        timestamp = _instant(now)
        release_reason = _safe_text(reason, "release reason", maximum=255)
        # Sweep everything except this lease. Expiry means another claimant may reclaim the
        # lease, not that its owner may no longer let go of it -- and sweeping the caller's
        # own lease first turned every expired lease into an orphan that only HIL could
        # resolve, so nothing could release it and it blocked retire and setup outright. A
        # lease already orphaned before this call still requires that HIL resolution.
        self.orphan_expired(now=timestamp, exclude=(identity.lease_id,))
        with self._transaction() as connection:
            row = self._require_owner(connection, identity)
            if row["state"] != "active":
                raise CampaignLeaseStateError(f"cannot owner-release a {row['state']} lease")
            if timestamp < row["heartbeat_at"]:
                raise CampaignLeaseStateError("release time predates the durable heartbeat")
            cursor = connection.execute(
                "UPDATE leases SET state = 'released', released_at = ?, release_reason = ? "
                "WHERE lease_id = ? AND epoch = ? AND state = 'active' AND heartbeat_at = ?",
                (
                    timestamp,
                    release_reason,
                    identity.lease_id,
                    identity.epoch,
                    row["heartbeat_at"],
                ),
            )
            if cursor.rowcount != 1:
                raise CampaignLeaseStateError("release lost its durable fencing race")
            return self._read_record(connection, identity.lease_id)

    def orphan_expired(
        self, *, now: float | None = None, exclude: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        """Durably mark expired leases orphaned without releasing any claim."""

        if self.read_only:
            raise CampaignLeaseError("this campaign lease store is open read-only")
        timestamp = _instant(now)
        with self._transaction() as connection:
            return self._orphan_expired(connection, timestamp, exclude)

    def orphan_expired_lease(self, lease_id: str, *, now: float | None = None) -> LeaseRecord:
        """Durably orphan exactly one lease, and only if it is still active and past its own
        expiry. Every other lease in the store, expired or not, is left untouched: this is a
        targeted variant of `_orphan_expired`'s store-wide sweep for a single named lease, so
        an operator command can make the one documented orphan-release route reachable without
        also sweeping leases that belong to other campaigns sharing this repository's store.
        """

        if self.read_only:
            raise CampaignLeaseError("this campaign lease store is open read-only")
        if not isinstance(lease_id, str) or _LEASE_ID.fullmatch(lease_id) is None:
            raise CampaignLeaseError("lease_id must be lowercase SHA-256-shaped hex")
        timestamp = _instant(now)
        with self._transaction() as connection:
            record = self._read_record(connection, lease_id)
            if record.state == "active" and record.expires_at <= timestamp:
                connection.execute(
                    "UPDATE leases SET state = 'orphaned' WHERE lease_id = ? AND state = 'active'",
                    (lease_id,),
                )
            return self._read_record(connection, lease_id)

    def authorize_orphan_resolution(
        self,
        *,
        authorization: _ControllerOrphanResolutionAuthorization,
        resolution_token: str,
        sweep_expired: bool = True,
    ) -> LeaseRecord:
        """Consume one approved controller HIL record into an orphan's durable authority.

        ``sweep_expired=False`` skips this call's own store-wide expiry sweep; every caller
        but `release_orphaned_lease`'s ``sweep_expired=False`` path keeps today's default.
        """

        if self.read_only:
            raise CampaignLeaseError("this campaign lease store is open read-only")
        if not isinstance(authorization, _ControllerOrphanResolutionAuthorization):
            raise CampaignLeaseError(
                "orphan resolution requires an opaque controller-issued authorization"
            )
        checked = json.loads(authorization._encoded)
        if checked["disposition"] != "approve":
            raise CampaignLeaseStateError("HIL authorization denies orphan resolution")
        token = _safe_text(resolution_token, "resolution token", maximum=4096)
        if len(token) < 32:
            raise CampaignLeaseError("resolution token must contain at least 32 characters")
        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(token_digest, checked["resolution_token_sha256"]):
            raise CampaignLeaseStateError("resolution token does not match HIL authorization")
        lease_id = checked["lease_id"]
        timestamp = checked["authorized_at"]
        if sweep_expired:
            self.orphan_expired(now=timestamp)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None or row["state"] != "orphaned":
                raise CampaignLeaseStateError("resolution authorization requires an orphaned lease")
            self._read_record(connection, lease_id)
            bindings = {
                "controller_authority_sha256": row["controller_authority_sha256"],
                "campaign_id": row["campaign_id"],
                "run_id": row["run_id"],
                "lease_id": row["lease_id"],
            }
            for field, expected in bindings.items():
                if checked[field] != expected:
                    raise CampaignLeaseStateError(
                        f"HIL authorization {field} does not match orphaned lease"
                    )
            if timestamp < row["expires_at"]:
                raise CampaignLeaseStateError("resolution authorization predates lease expiry")
            if row["resolution_token_sha256"] is not None:
                raise CampaignLeaseStateError("orphan already has a resolution authorization")
            try:
                cursor = connection.execute(
                    "UPDATE leases SET resolution_authorization_id = ?, "
                    "resolution_token_sha256 = ?, resolution_hil_ref = ?, "
                    "resolution_question_sha256 = ?, resolution_answer_sha256 = ?, "
                    "resolution_authorized_at = ? "
                    "WHERE lease_id = ? AND state = 'orphaned' "
                    "AND resolution_authorization_id IS NULL",
                    (
                        checked["content_sha256"],
                        token_digest,
                        checked["checkpoint_ref"],
                        checked["question_sha256"],
                        checked["answer_sha256"],
                        timestamp,
                        lease_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CampaignLeaseStateError(
                    "resolution authorization conflicts with durable state"
                ) from exc
            if cursor.rowcount != 1:
                raise CampaignLeaseStateError("orphan authorization lost its durable fencing race")
            return self._read_record(connection, lease_id)

    def resolve_orphan(
        self,
        lease_id: str,
        *,
        resolution_token: str,
        reason: str,
        resolved_at: float | None = None,
        sweep_expired: bool = True,
    ) -> LeaseRecord:
        """Consume the pre-authorized HIL token and release one orphaned claim set.

        ``sweep_expired=False`` skips this call's own store-wide expiry sweep; every caller
        but `release_orphaned_lease`'s ``sweep_expired=False`` path keeps today's default.
        """

        if self.read_only:
            raise CampaignLeaseError("this campaign lease store is open read-only")
        if not isinstance(lease_id, str) or _LEASE_ID.fullmatch(lease_id) is None:
            raise CampaignLeaseError("lease_id must be lowercase SHA-256-shaped hex")
        token = _safe_text(resolution_token, "resolution token", maximum=4096)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        release_reason = _safe_text(reason, "resolution reason", maximum=255)
        timestamp = _instant(resolved_at)
        if sweep_expired:
            self.orphan_expired(now=timestamp)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None or row["state"] != "orphaned":
                raise CampaignLeaseStateError("only an orphaned lease can be resolved")
            self._read_record(connection, lease_id)
            if row["resolution_token_sha256"] is None or not secrets.compare_digest(
                row["resolution_token_sha256"], digest
            ):
                raise CampaignLeaseStateError("HIL resolution token is absent or does not match")
            if timestamp < row["resolution_authorized_at"]:
                raise CampaignLeaseStateError("orphan resolution predates its HIL authorization")
            cursor = connection.execute(
                "UPDATE leases SET state = 'released', released_at = ?, release_reason = ?, "
                "resolved_by_token_sha256 = ? WHERE lease_id = ? AND state = 'orphaned' "
                "AND resolution_token_sha256 = ? AND resolved_by_token_sha256 IS NULL",
                (timestamp, release_reason, digest, lease_id, digest),
            )
            if cursor.rowcount != 1:
                raise CampaignLeaseStateError("orphan resolution lost its durable fencing race")
            return self._read_record(connection, lease_id)

    def _read_record(self, connection: sqlite3.Connection, lease_id: str) -> LeaseRecord:
        row = connection.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        if row is None:
            raise CampaignLeaseStateError("lease does not exist")
        if _LEASE_ID.fullmatch(row["lease_id"]) is None:
            raise CampaignLeaseError("durable lease_id is malformed")
        for field in ("campaign_id", "run_id", "session_id", "claimant_id", "claim_set_id"):
            _token(row[field], f"durable {field}")
        for field in (
            "controller_authority_sha256",
            "repository_common_dir_sha256",
            "worktree_sha256",
        ):
            _sha256(row[field], f"durable {field}")
        if row["repository_common_dir_sha256"] != self.repository_common_dir_sha256:
            raise CampaignLeaseError(
                "durable lease repository identity does not match the subject-scoped store"
            )
        _branch(row["branch"])
        _git_oid(row["base_oid"], "durable base_oid")
        _positive_int(row["pid"], "durable pid")
        _positive_int(row["epoch"], "durable epoch")
        _safe_text(row["host"], "durable host", maximum=255)
        if row["state"] not in {"active", "orphaned", "released"}:
            raise CampaignLeaseError("durable lease state is malformed")
        for field in ("acquired_at", "heartbeat_at", "expires_at"):
            _instant(row[field])
        if row["heartbeat_at"] < row["acquired_at"] or row["expires_at"] <= row["heartbeat_at"]:
            raise CampaignLeaseError("durable lease timeline is inconsistent")
        if row["state"] == "released":
            released_at = _instant(row["released_at"])
            _safe_text(row["release_reason"], "durable release reason", maximum=255)
            if released_at < row["heartbeat_at"]:
                raise CampaignLeaseError("durable release predates the latest heartbeat")
        elif row["released_at"] is not None or row["release_reason"] is not None:
            raise CampaignLeaseError("live durable lease carries release fields")

        resolution_fields = (
            "resolution_authorization_id",
            "resolution_token_sha256",
            "resolution_hil_ref",
            "resolution_question_sha256",
            "resolution_answer_sha256",
            "resolution_authorized_at",
        )
        resolution_values = [row[field] for field in resolution_fields]
        if any(value is not None for value in resolution_values):
            if any(value is None for value in resolution_values):
                raise CampaignLeaseError("durable HIL resolution authority is partial")
            authorization = build_orphan_resolution_authorization(
                controller_authority_sha256=row["controller_authority_sha256"],
                campaign_id=row["campaign_id"],
                run_id=row["run_id"],
                lease_id=row["lease_id"],
                checkpoint_ref=row["resolution_hil_ref"],
                question_sha256=row["resolution_question_sha256"],
                answer_sha256=row["resolution_answer_sha256"],
                disposition="approve",
                authorized_at=row["resolution_authorized_at"],
                resolution_token_sha256=row["resolution_token_sha256"],
            )
            if authorization["content_sha256"] != row["resolution_authorization_id"]:
                raise CampaignLeaseError("durable HIL authorization content digest mismatch")
            if row["resolution_authorized_at"] < row["expires_at"]:
                raise CampaignLeaseError("durable HIL authorization predates lease expiry")
            if row["state"] == "active":
                raise CampaignLeaseError("active durable lease carries HIL resolution authority")
        if row["resolved_by_token_sha256"] is not None:
            _sha256(row["resolved_by_token_sha256"], "durable resolved token")
            if (
                row["state"] != "released"
                or row["resolved_by_token_sha256"] != row["resolution_token_sha256"]
            ):
                raise CampaignLeaseError("durable orphan resolution token is inconsistent")
        elif row["state"] == "released" and row["resolution_authorization_id"] is not None:
            raise CampaignLeaseError("HIL-authorized durable release did not consume its token")
        claim_rows = self._claims(connection, lease_id)
        paths = sorted(value for kind, value, _ in claim_rows if kind == "path_prefix")
        symbols = sorted(value for kind, value, _ in claim_rows if kind == "symbol")
        subjects = sorted(value for kind, value, _ in claim_rows if kind == "subject")
        resources = sorted(value for kind, value, _ in claim_rows if kind == "semantic_resource")
        data_dirs = sorted(value for kind, value, _ in claim_rows if kind == "data_directory")
        ports = []
        for kind, value, scope in claim_rows:
            if kind == "port":
                parts = value.split(":", 1)
                if len(parts) != 2 or not parts[1].isdigit():
                    raise CampaignLeaseError("durable port claim is malformed")
                transport, port = parts
                ports.append({"transport": transport, "port": int(port), "bind_scope": scope})
        raw_claim_set = {
            "claim_set_id": row["claim_set_id"],
            "path_prefixes": paths,
            "symbols": symbols,
            "subjects": subjects,
            "semantic_resources": resources,
            "ports": sorted(
                ports,
                key=lambda item: (item["transport"], item["port"], item["bind_scope"]),
            ),
            "data_directories": data_dirs,
        }
        claim_set, expected_claim_rows = _canonical_claim_set(raw_claim_set)
        if sorted(claim_rows) != sorted(expected_claim_rows):
            raise CampaignLeaseError("durable claim rows are malformed or noncanonical")
        return LeaseRecord(
            identity=LeaseIdentity(
                row["lease_id"],
                row["campaign_id"],
                row["run_id"],
                row["session_id"],
                row["claimant_id"],
                row["controller_authority_sha256"],
                row["repository_common_dir_sha256"],
                row["worktree_sha256"],
                row["branch"],
                row["base_oid"],
                row["pid"],
                row["host"],
                row["epoch"],
            ),
            state=row["state"],
            acquired_at=row["acquired_at"],
            heartbeat_at=row["heartbeat_at"],
            expires_at=row["expires_at"],
            released_at=row["released_at"],
            release_reason=row["release_reason"],
            claim_set=claim_set,
            resolution_authorization_id=row["resolution_authorization_id"],
            resolution_hil_ref=row["resolution_hil_ref"],
            resolution_question_sha256=row["resolution_question_sha256"],
            resolution_answer_sha256=row["resolution_answer_sha256"],
            resolution_authorized_at=row["resolution_authorized_at"],
        )

    def get(self, lease_id: str) -> LeaseRecord:
        """Read one durable lease without changing expiry state."""

        if not isinstance(lease_id, str) or _LEASE_ID.fullmatch(lease_id) is None:
            raise CampaignLeaseError("lease_id must be lowercase SHA-256-shaped hex")
        with self._read_transaction() as connection:
            return self._read_record(connection, lease_id)

    def list(self, *, states: Sequence[str] = _LIVE_STATES) -> tuple[LeaseRecord, ...]:
        """Read leases in fencing-epoch order without changing expiry state."""

        requested = tuple(states)
        if not requested or any(
            state not in {"active", "orphaned", "released"} for state in requested
        ):
            raise CampaignLeaseError("states must contain supported lease states")
        placeholders = ",".join("?" for _ in requested)
        with self._read_transaction() as connection:
            ids = [
                row["lease_id"]
                for row in connection.execute(
                    f"SELECT lease_id FROM leases WHERE state IN ({placeholders}) ORDER BY epoch",
                    requested,
                )
            ]
            return tuple(self._read_record(connection, lease_id) for lease_id in ids)


__all__ = [
    "CampaignLeaseConflict",
    "CampaignLeaseError",
    "CampaignLeaseStateError",
    "CampaignLeaseStore",
    "LeaseIdentity",
    "LeaseRecord",
    "build_orphan_resolution_authorization",
    "release_orphaned_lease",
    "validate_orphan_resolution_authorization",
]
