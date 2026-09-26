"""Closed, privacy-bounded provider observations consumed by the cockpit.

Raw event streams and prompts stay under ``runs/``.  This record carries only identities,
lifecycle counts and digests needed to render and later verify a provider session.

**Schema 2** adds the receipt's real promotion verdict: ``promotion_eligible``,
``promotion_basis``, ``promotion_blockers`` and ``operational_evidence_linked``.  Schema 1 carried
only ``promotion_identity_eligible`` (``identity.promotion_eligible``, hardcoded ``False`` by
design — see ``receipt.py``), which a reader could mistake for the turn's real verdict.  Following
this repository's cockpit-schema convention (a version bump per shape change; the reader accepts
exactly one version and rejects the rest rather than reading an old record as a subset — see
``tui/cockpit.go``'s ``CockpitSchemaVersion`` history), this is a new, exact-match schema version:
schema 1 records are refused, not read as a smaller schema 2.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bearhug.providers.receipt import validate_provider_receipt


class ProviderObservationError(ValueError):
    """A provider observation is incomplete, open-ended, or type-invalid."""


REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "provider",
        "adapter",
        "adapter_version",
        "observed_at",
        "cwd",
        "role",
        "sandbox",
        "approval_policy",
        "required_capabilities",
        "session_id",
        "thread_id",
        "turn_id",
        "terminal_state",
        "requested_model",
        "configured_model",
        "final_observed_model",
        "requested_reasoning_effort",
        "configured_reasoning_effort",
        "model_verification",
        "reasoning_effort_verification",
        "promotion_identity_eligible",
        "promotion_eligible",
        "promotion_basis",
        "promotion_blockers",
        "operational_evidence_linked",
        "approval_observation",
        "approval_requests",
        "approval_resolutions",
        "item_types",
        "raw_event_count",
        "raw_events_sha256",
        "request_sha256",
        "limitations",
    }
)

#: The Go reader accepts exactly this and rejects "1" and anything unknown rather than coercing
#: it — see ``tui/cockpit.go``'s ``validateProviderSessions`` (row.SchemaVersion). Both sides move
#: together, with any committed row fixtures regenerated alongside a bump.
SCHEMA_VERSION = "2"


def _sha256(value: Any, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ProviderObservationError(f"{field} must be lowercase SHA-256")


def _strings(value: Any, field: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ProviderObservationError(f"{field} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise ProviderObservationError(f"{field} must not contain duplicates")


def validate_provider_observation(value: Any) -> dict[str, Any]:
    """Validate and return one closed v1 record without coercing any field."""

    if not isinstance(value, dict) or set(value) != REQUIRED_FIELDS:
        missing = sorted(
            REQUIRED_FIELDS - set(value) if isinstance(value, dict) else REQUIRED_FIELDS
        )
        unknown = sorted(set(value) - REQUIRED_FIELDS) if isinstance(value, dict) else []
        raise ProviderObservationError(
            f"provider observation fields missing={missing} unknown={unknown}"
        )
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["record_kind"] != "provider_session_observation"
    ):
        raise ProviderObservationError("unsupported provider observation schema or record kind")
    for field in (
        "provider",
        "adapter",
        "adapter_version",
        "observed_at",
        "cwd",
        "session_id",
    ):
        if not isinstance(value[field], str) or not value[field]:
            raise ProviderObservationError(f"{field} must be a non-empty string")
    try:
        observed_at = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderObservationError("observed_at must be an ISO-8601 timestamp") from exc
    if observed_at.tzinfo is None:
        raise ProviderObservationError("observed_at must include a timezone")
    if not Path(value["cwd"]).is_absolute():
        raise ProviderObservationError("cwd must be absolute")
    for field in (
        "role",
        "thread_id",
        "turn_id",
        "requested_model",
        "configured_model",
        "final_observed_model",
        "requested_reasoning_effort",
        "configured_reasoning_effort",
    ):
        field_value = value[field]
        if field_value is not None and (not isinstance(field_value, str) or not field_value):
            raise ProviderObservationError(f"{field} must be a non-empty string or null")
    if value["sandbox"] not in {"read-only", "workspace-write"}:
        raise ProviderObservationError("sandbox is outside the closed vocabulary")
    if value["approval_policy"] not in {"never", "on-request", "untrusted"}:
        raise ProviderObservationError("approval_policy is outside the closed vocabulary")
    if value["terminal_state"] not in {"completed", "failed", "incomplete"}:
        raise ProviderObservationError("terminal_state is outside the closed vocabulary")
    verification = {"provider_observed", "launch_requested_only", "unobserved"}
    for field in ("model_verification", "reasoning_effort_verification"):
        if value[field] not in verification:
            raise ProviderObservationError(f"{field} is outside the closed vocabulary")
    if type(value["promotion_identity_eligible"]) is not bool:
        raise ProviderObservationError("promotion_identity_eligible must be boolean")
    # The receipt's real, turn-level verdict — never the always-False identity flag above.
    if type(value["promotion_eligible"]) is not bool:
        raise ProviderObservationError("promotion_eligible must be boolean")
    promotion_basis = value["promotion_basis"]
    if promotion_basis is not None and promotion_basis not in {
        "operational_observed",
        "per_turn_attested",
    }:
        raise ProviderObservationError("promotion_basis is outside the closed vocabulary")
    _strings(value["promotion_blockers"], "promotion_blockers")
    if value["promotion_eligible"] and value["promotion_blockers"]:
        raise ProviderObservationError("an eligible turn cannot carry promotion blockers")
    if not value["promotion_eligible"] and not value["promotion_blockers"]:
        raise ProviderObservationError("an ineligible turn must name at least one blocker")
    evidence_linked = value["operational_evidence_linked"]
    if evidence_linked != "none" and (
        not isinstance(evidence_linked, str)
        or len(evidence_linked) != 12
        or any(char not in "0123456789abcdef" for char in evidence_linked)
    ):
        raise ProviderObservationError(
            "operational_evidence_linked must be 'none' or 12 lowercase hex characters"
        )
    # The last line of defense against a false "ELIGIBLE" ever rendering.
    # Unreachable through today's receipt pipeline (receipt.py's validate_provider_receipt makes
    # promotion_eligible=True with no basis structurally impossible), but this validator's job is
    # exactly to refuse that record if it ever arrived some other way -- mirrors the blockers-XOR
    # check just above.
    if value["promotion_eligible"] and promotion_basis is None:
        raise ProviderObservationError("an eligible turn must name a promotion_basis")
    if (
        value["promotion_eligible"]
        and promotion_basis == "operational_observed"
        and evidence_linked == "none"
    ):
        raise ProviderObservationError(
            "an operationally-observed eligible turn must link its operational evidence"
        )
    if value["approval_observation"] not in {"full_lifecycle", "denials_only", "unavailable"}:
        raise ProviderObservationError("approval_observation is outside the closed vocabulary")
    for field in ("approval_requests", "approval_resolutions"):
        if type(value[field]) is not int or value[field] < 0:
            raise ProviderObservationError(f"{field} must be a non-negative integer")
    if value["approval_resolutions"] > value["approval_requests"]:
        raise ProviderObservationError("approval resolutions exceed observed requests")
    _strings(value["item_types"], "item_types")
    _strings(value["required_capabilities"], "required_capabilities")
    _strings(value["limitations"], "limitations")
    if type(value["raw_event_count"]) is not int or value["raw_event_count"] < 1:
        raise ProviderObservationError("raw_event_count must be a positive integer")
    _sha256(value["raw_events_sha256"], "raw_events_sha256")
    _sha256(value["request_sha256"], "request_sha256")
    return value


def provider_observations(
    root: Path | str | None,
    *,
    now: datetime | None = None,
    recent: timedelta = timedelta(hours=24),
    limit: int = 12,
) -> dict[str, Any]:
    """Read recent observation JSON files and report invalid files instead of hiding them."""

    if root is None:
        return {
            "status": "unreported",
            "reason": "no provider observation directory supplied",
            "sources": [],
            "invalid_observations": 0,
            "rows": [],
        }
    directory = Path(root)
    if not directory.is_dir():
        return {
            "status": "unreported",
            "reason": f"provider observation directory does not exist: {directory}",
            "sources": [],
            "invalid_observations": 0,
            "rows": [],
        }
    now = now or datetime.now(UTC)
    cutoff = now - recent
    rows: list[tuple[datetime, str, dict[str, Any]]] = []
    invalid = 0
    for path in sorted(directory.rglob("*.receipt.json")):
        try:
            receipt = validate_provider_receipt(json.loads(path.read_text(encoding="utf-8")))
            value = observation_from_receipt(receipt)
            observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        except (OSError, ValueError, ProviderObservationError):
            invalid += 1
            continue
        if observed > now + timedelta(minutes=5):
            invalid += 1
            continue
        if observed >= cutoff:
            rows.append((observed, str(path.relative_to(directory)), value))
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    rows = rows[:limit]
    return {
        "status": "carried" if rows else "unreported",
        "reason": "" if rows else "no valid provider observations in the recent window",
        "sources": [source for _, source, _ in rows],
        "invalid_observations": invalid,
        "rows": [value for _, _, value in rows],
    }


def observation_from_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Mechanically project the canonical receipt into the privacy-bounded cockpit row."""

    receipt = validate_provider_receipt(receipt)
    identity = receipt["identity"]
    launch = receipt["launch"]
    operational_evidence_sha256 = receipt.get("operational_evidence_sha256")
    value = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "provider_session_observation",
        "provider": receipt["provider"],
        "adapter": receipt["adapter"],
        "adapter_version": receipt["adapter_version"],
        "observed_at": receipt["observed_at"],
        "cwd": receipt["cwd"],
        "role": receipt["role"],
        "sandbox": launch["sandbox"],
        "approval_policy": launch["approval_policy"],
        "required_capabilities": list(receipt["required_capabilities"]),
        "session_id": receipt["session_id"],
        "thread_id": receipt["thread_id"],
        "turn_id": receipt["turn_id"],
        "terminal_state": receipt["terminal_state"],
        "requested_model": identity["requested_model"],
        "configured_model": identity["configured_model"],
        "final_observed_model": identity["final_observed_model"],
        "requested_reasoning_effort": identity["requested_reasoning_effort"],
        "configured_reasoning_effort": identity["configured_reasoning_effort"],
        "model_verification": identity["model_verification"],
        "reasoning_effort_verification": identity["reasoning_effort_verification"],
        "promotion_identity_eligible": identity["promotion_eligible"],
        # The receipt's real, turn-level verdict — read here, never recomputed.
        "promotion_eligible": receipt["promotion_eligible"],
        "promotion_basis": receipt.get("promotion_basis"),
        "promotion_blockers": list(receipt["promotion_blockers"]),
        "operational_evidence_linked": (
            operational_evidence_sha256[:12] if operational_evidence_sha256 is not None else "none"
        ),
        "approval_observation": receipt["approval_observation"],
        "approval_requests": receipt["approval_requests"],
        "approval_resolutions": receipt["approval_resolutions"],
        "item_types": list(receipt["item_types"]),
        "raw_event_count": receipt["raw_event_count"],
        "raw_events_sha256": receipt["raw_event_sha256"],
        "request_sha256": receipt["request_sha256"],
        "limitations": list(receipt["limitations"]),
    }
    return validate_provider_observation(value)


__all__ = [
    "ProviderObservationError",
    "observation_from_receipt",
    "provider_observations",
    "validate_provider_observation",
]
