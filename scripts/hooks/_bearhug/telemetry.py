"""R17 — construct schema-v1 telemetry records, and let nothing forbidden through.

Telemetry is the channel meant to replace transcript archaeology, so what it may NOT carry is as
load-bearing as what it must. The protocol forbids retaining prompt text, assistant text,
environment values, credentials, command output, or source contents. Evidence is allowlisted by
kind, capped at 512 characters, stripped of home-directory prefixes, and secret-pattern redacted
BEFORE serialization.

**If redaction cannot complete, the record is DROPPED.** That is the right failure direction: D05
established telemetry is best-effort and may never affect a decision, so losing an observation
costs nothing a verdict depends on, while a credential written into a durable store cannot be
withdrawn.

Construction only. R18 owns the store; a constructor that wrote would be storage.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from . import runtime_sha256, runtime_version

SCHEMA_VERSION = "1"
COORDINATOR_VERSION = "1.0.0"

#: Mirrors the evaluator schema's evidence enum; a lab test pins the two together.
ALLOWED_EVIDENCE_KINDS = ("event_field", "path", "state", "tool_call")

MAX_EVIDENCE_VALUE = 512

#: Patterns whose presence means the value must not be stored as written. Deliberately broad: a
#: false positive costs one redacted observation, a false negative writes a credential to disk.
_SECRETS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*\S{6,}"),
)

_REDACTED = "[REDACTED]"


class RedactionFailure(Exception):
    """The record cannot be made safe, so it must not be written."""


def _strip_home(value: str) -> str:
    """Replace the home prefix with `~`. A home path carries a username."""
    home = os.path.expanduser("~")
    if home and home != "/" and home in value:
        return value.replace(home, "~")
    return value


def redact(value: Any) -> str:
    """Make one scalar value safe. Evidence classification failures raise upstream."""
    text = value if isinstance(value, str) else str(value)
    text = _strip_home(text)
    for pattern in _SECRETS:
        text = pattern.sub(_REDACTED, text)
    if len(text) > MAX_EVIDENCE_VALUE:
        # Truncation is safe only AFTER redaction; truncating first could split a secret into a
        # fragment no pattern matches while still leaking most of it.
        text = text[: MAX_EVIDENCE_VALUE - 1] + "…"
    return text


def _redact_evidence(items: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items:
        kind = item.get("kind") if isinstance(item, Mapping) else getattr(item, "kind", None)
        value = item.get("value") if isinstance(item, Mapping) else getattr(item, "value", None)
        if kind not in ALLOWED_EVIDENCE_KINDS:
            raise RedactionFailure(
                f"evidence kind {kind!r} is not allowlisted; the record is dropped rather than "
                "written with an unclassified value"
            )
        out.append({"kind": kind, "value": redact(value)})
    return out


def _redact_observation(observation: Any) -> Any:
    if isinstance(observation, Mapping):
        return {str(k): _redact_observation(v) for k, v in observation.items()}
    if isinstance(observation, (list, tuple)):
        return [_redact_observation(v) for v in observation]
    if isinstance(observation, str):
        return redact(observation)
    return observation


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _base(event_id: str, session_id: str, event_name: str, raw_input: bytes) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "runtime_version": runtime_version(),
        "runtime_sha256": runtime_sha256(),
        "session_id": session_id,
        "event_id": event_id,
        "event_name": event_name,
        # The input is HASHED, never retained: repeated input is detectable without keeping it.
        "input_sha256": hashlib.sha256(raw_input or b"").hexdigest(),
        "observed_at": _now(),
    }


def build_record(
    outcome: Any,
    *,
    event_id: str,
    session_id: str,
    event_name: str,
    raw_input: bytes,
    extra_evidence: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """One coordinator_evaluation record, or None if it could not be made safe."""
    result = getattr(outcome, "result", None)
    try:
        evidence = _redact_evidence(list(getattr(result, "evidence", ()) or []))
        if extra_evidence:
            evidence += _redact_evidence(extra_evidence)
    except RedactionFailure:
        return None

    record = _base(event_id, session_id, event_name, raw_input)
    record.update({
        "record_kind": "coordinator_evaluation",
        "coordinator_version": COORDINATOR_VERSION,
        "gate_id": outcome.gate_id,
        "evaluator_ordinal": outcome.ordinal,
        "execution_state": outcome.execution_state,
        "coordinator_reason": getattr(outcome, "not_reached_reason", None),
    })

    if result is None:
        record["result"] = None
        return record

    payload = result.to_dict()
    payload["evidence"] = evidence
    try:
        payload["remediation"] = (
            redact(payload["remediation"]) if payload["remediation"] is not None else None
        )
    except RedactionFailure:
        return None
    record["result"] = payload
    return record


def build_emitter_record(
    *,
    emitter_id: str,
    observation: Mapping[str, Any],
    event_id: str,
    session_id: str,
    event_name: str,
    raw_input: bytes,
) -> dict[str, Any] | None:
    """One emitter_observation record, per D06.

    A non-Stop hook arbitrates nothing, so the coordinator-only fields are null — that is what
    stops an observation being read as a decision.
    """
    try:
        safe = _redact_observation(dict(observation))
    except RedactionFailure:
        return None

    record = _base(event_id, session_id, event_name, raw_input)
    record.update({
        "record_kind": "emitter_observation",
        "emitter_id": emitter_id,
        "observation": safe,
        "coordinator_version": None,
        "gate_id": None,
        "evaluator_ordinal": None,
        "execution_state": None,
        "coordinator_reason": None,
        "result": None,
    })
    return record
