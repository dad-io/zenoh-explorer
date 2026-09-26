"""Closed, content-addressed execution-capsule control-plane contracts.

The capsule layer is deliberately a pure projection over explicit project-authored records.  It
does not read a project document, infer an intent, launch a provider, acquire a claim, or mutate a
run.  ``adapt_v1_campaign`` is the compatibility bridge: it copies every executable v1 field into
one capsule and therefore cannot accidentally coalesce work units or promote a proposal.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from bearhug.campaign.contracts import (
    CampaignContractError,
    validate_campaign_template,
    validate_work_unit,
)
from bearhug.campaign.importer import CampaignTypeset

CANONICAL_ALGORITHM = "bearhug-execution-capsule-canonical-json-sha256/1"

_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_SUBJECT = re.compile(r"^[A-Za-z0-9_*][A-Za-z0-9_.*>:/-]{0,511}$")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")

_ORIGINS = {"project_sealed", "derived_observation", "proposal"}
_BINDING_STATES = {"accepted", "observed", "proposed"}
_APPROVAL_MODES = {"initial", "automatic", "hil_approved"}
_RECONCILIATION_TRIGGERS = {
    "assumption_invalidated",
    "binding_conflict",
    "invariant_conflict",
    "scope_pressure",
    "authority_change",
    "validation_failure",
    "hil_requested",
}
_EVENTS = {
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
}
_STATUSES = {"candidate_ready", "accepted", "completed", "blocked", "failed", "superseded"}
_VALIDATION_STATUSES = {"pass", "fail", "unavailable"}
_FINDING_SEVERITIES = {"blocker", "high", "medium", "nit"}
_FINDING_STATES = {"open", "resolved", "accepted"}

_AUTHORITY_REF_FIELDS = frozenset({"source_id", "content_sha256", "scopes"})
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
_EXPECTED_SURFACE_FIELDS = frozenset(
    {"path_prefixes", "symbols", "subjects", "semantic_resources", "data_directories"}
)
_CONTEXT_BUDGET_FIELDS = frozenset(
    {"mode", "max_bytes", "max_tokens", "reserve_bytes", "reserve_tokens"}
)
_CONTEXT_BUDGET_LIMITS = ("max_bytes", "max_tokens", "reserve_bytes", "reserve_tokens")


class CapsuleContractError(ValueError):
    """A capsule control-plane record is malformed or has an unsafe relationship."""

    def __init__(self, issues: Sequence[str] | str):
        self.issues = (issues,) if isinstance(issues, str) else tuple(issues)
        super().__init__(
            "invalid capsule contract:\n" + "\n".join(f"- {issue}" for issue in self.issues)
        )


@dataclass(frozen=True, slots=True)
class CapsuleContractResult:
    """Canonical identity of one valid capsule record."""

    record_kind: str
    record_id: str
    digest: str
    canonical_bytes: bytes


@dataclass(frozen=True, slots=True)
class CapsuleControlPlane:
    """The additive v2 records compiled from explicit inputs."""

    intent_envelope: dict[str, Any]
    capsule_plan: dict[str, Any]
    execution_packets: tuple[dict[str, Any], ...] = ()
    journals: tuple[dict[str, Any], ...] = ()
    results: tuple[dict[str, Any], ...] = ()

    @property
    def intent(self) -> dict[str, Any]:
        return self.intent_envelope

    @property
    def plan(self) -> dict[str, Any]:
        return self.capsule_plan


@dataclass(frozen=True, slots=True)
class V1AdapterResult:
    """Exact v1-to-v2 projection; all v1 work-unit execution fields remain available."""

    control_plane: CapsuleControlPlane
    capsules_by_work_unit: dict[str, dict[str, Any]]

    @property
    def intent_envelope(self) -> dict[str, Any]:
        return self.control_plane.intent_envelope

    @property
    def capsule_plan(self) -> dict[str, Any]:
        return self.control_plane.capsule_plan


def _closed(
    value: Any, fields: frozenset[str], path: str, issues: list[str]
) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        issues.append(f"{path}: expected object")
        return None
    actual = set(value)
    for name in sorted(fields - actual):
        issues.append(f"{path}: missing field {name!r}")
    for name in sorted(actual - fields):
        issues.append(f"{path}: unknown field {name!r}")
    return value


def _strings(
    value: Any,
    path: str,
    issues: list[str],
    *,
    pattern: re.Pattern[str] | None = None,
    nonempty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        issues.append(f"{path}: expected {'non-empty ' if nonempty else ''}array")
        return []
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            issues.append(f"{path}/{index}: expected non-empty string")
            continue
        if pattern is not None and pattern.fullmatch(item) is None:
            issues.append(f"{path}/{index}: invalid value {item!r}")
        result.append(item)
    if len(result) != len(set(result)):
        issues.append(f"{path}: duplicate values are forbidden")
    return result


def _string(
    value: Any,
    path: str,
    issues: list[str],
    *,
    pattern: re.Pattern[str] | None = None,
    maximum: int | None = None,
) -> str | None:
    if not isinstance(value, str) or not value or (maximum is not None and len(value) > maximum):
        issues.append(f"{path}: expected non-empty string")
        return None
    if pattern is not None and pattern.fullmatch(value) is None:
        issues.append(f"{path}: invalid value {value!r}")
    return value


def _sha(value: Any, path: str, issues: list[str]) -> None:
    _string(value, path, issues, pattern=_SHA256)


def _oid(value: Any, path: str, issues: list[str]) -> None:
    _string(value, path, issues, pattern=_GIT_OID)


def _integer(
    value: Any, path: str, issues: list[str], *, minimum: int = 0, maximum: int | None = None
) -> None:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bound = str(maximum) if maximum is not None else "unbounded"
        issues.append(f"{path}: expected integer in [{minimum}, {bound}]")


def _boolean(value: Any, path: str, issues: list[str]) -> None:
    if type(value) is not bool:
        issues.append(f"{path}: expected boolean")


def _timestamp(value: Any, path: str, issues: list[str]) -> datetime | None:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        issues.append(f"{path}: expected UTC timestamp YYYY-MM-DDTHH:MM:SSZ")
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        issues.append(f"{path}: invalid calendar timestamp")
        return None


def _string_policy(value: Any, issues: list[str]) -> None:
    pending: list[tuple[str, Any]] = [("<root>", value)]
    while pending:
        path, current = pending.pop()
        if isinstance(current, str):
            for character in current:
                point = ord(character)
                if point <= 0x1F or 0x7F <= point <= 0x9F or 0xD800 <= point <= 0xDFFF:
                    issues.append(f"{path}: forbidden Unicode control or surrogate U+{point:04X}")
                    break
        elif isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    issues.append(f"{path}: object key must be a string")
                    continue
                pending.append((f"{path}/{key}", child))
                pending.append((f"{path}/<key>", key))
        elif isinstance(current, list):
            pending.extend((f"{path}/{index}", child) for index, child in enumerate(current))


def _repository_path(value: Any, path: str, issues: list[str], *, allow_dot: bool = False) -> None:
    if not isinstance(value, str):
        issues.append(f"{path}: expected repository-relative path")
        return
    if allow_dot and value == ".":
        return
    parts = value.split("/")
    candidate = PurePosixPath(value)
    if (
        not value
        or len(value) > 4096
        or candidate.is_absolute()
        or value.startswith("~")
        or value.endswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
        or candidate.as_posix() != value
    ):
        issues.append(f"{path}: expected canonical repository-relative path")


def _authority_refs(value: Any, path: str, issues: list[str]) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        issues.append(f"{path}: expected non-empty array")
        return []
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}/{index}"
        ref = _closed(item, _AUTHORITY_REF_FIELDS, item_path, issues)
        if ref is None:
            continue
        source = _string(ref.get("source_id"), f"{item_path}/source_id", issues, pattern=_TOKEN)
        _sha(ref.get("content_sha256"), f"{item_path}/content_sha256", issues)
        _strings(ref.get("scopes"), f"{item_path}/scopes", issues, pattern=_SCOPE, nonempty=True)
        if source is not None and source in seen:
            issues.append(f"{path}: duplicate source_id {source!r}")
        if source is not None:
            seen.add(source)
        result.append(ref)
    return result


def _claim_set(value: Any, path: str, issues: list[str]) -> Mapping[str, Any] | None:
    claim = _closed(value, _CLAIM_FIELDS, path, issues)
    if claim is None:
        return None
    _string(claim.get("claim_set_id"), f"{path}/claim_set_id", issues, pattern=_TOKEN)
    for field in ("path_prefixes", "data_directories"):
        paths = claim.get(field)
        if not isinstance(paths, list):
            issues.append(f"{path}/{field}: expected array")
            continue
        for index, item in enumerate(paths):
            _repository_path(item, f"{path}/{field}/{index}", issues, allow_dot=True)
        if len(paths) != len(set(paths)):
            issues.append(f"{path}/{field}: duplicate values are forbidden")
    for field in ("symbols", "semantic_resources"):
        _strings(claim.get(field), f"{path}/{field}", issues)
    _strings(claim.get("subjects"), f"{path}/subjects", issues, pattern=_SUBJECT)
    ports = claim.get("ports")
    if not isinstance(ports, list):
        issues.append(f"{path}/ports: expected array")
    else:
        keys: list[tuple[Any, ...]] = []
        for index, item in enumerate(ports):
            item_path = f"{path}/ports/{index}"
            port = _closed(item, frozenset({"transport", "port", "bind_scope"}), item_path, issues)
            if port is None:
                continue
            transport = _string(port.get("transport"), f"{item_path}/transport", issues)
            if transport not in {"tcp", "udp"}:
                issues.append(f"{item_path}/transport: expected tcp or udp")
            _integer(port.get("port"), f"{item_path}/port", issues, minimum=1, maximum=65535)
            bind_scope = _string(port.get("bind_scope"), f"{item_path}/bind_scope", issues)
            if bind_scope not in {"loopback", "host", "network"}:
                issues.append(f"{item_path}/bind_scope: unsupported bind scope")
            keys.append((transport, port.get("port"), bind_scope))
        if len(keys) != len(set(keys)):
            issues.append(f"{path}/ports: duplicate values are forbidden")
    if not any(
        claim.get(field)
        for field in (
            "path_prefixes",
            "symbols",
            "subjects",
            "semantic_resources",
            "ports",
            "data_directories",
        )
    ):
        issues.append(f"{path}: mutation envelope must contain at least one claim")
    return claim


def _expected_surface(value: Any, path: str, issues: list[str]) -> Mapping[str, Any] | None:
    surface = _closed(value, _EXPECTED_SURFACE_FIELDS, path, issues)
    if surface is None:
        return None
    for field in ("path_prefixes", "symbols", "semantic_resources", "data_directories"):
        values = surface.get(field)
        if not isinstance(values, list):
            issues.append(f"{path}/{field}: expected array")
        else:
            if field in {"path_prefixes", "data_directories"}:
                for index, item in enumerate(values):
                    _repository_path(item, f"{path}/{field}/{index}", issues, allow_dot=True)
            else:
                _strings(values, f"{path}/{field}", issues)
    _strings(surface.get("subjects"), f"{path}/subjects", issues, pattern=_SUBJECT)
    return surface


def _evidence_refs(
    value: Any, path: str, issues: list[str], *, nonempty: bool = False
) -> list[str]:
    return _strings(value, path, issues, pattern=_SHA256, nonempty=nonempty)


def _binding(value: Any, path: str, issues: list[str]) -> Mapping[str, Any] | None:
    item = _closed(
        value, frozenset({"binding_id", "term", "meaning", "state", "evidence_refs"}), path, issues
    )
    if item is None:
        return None
    _string(item.get("binding_id"), f"{path}/binding_id", issues, pattern=_TOKEN)
    _string(item.get("term"), f"{path}/term", issues, maximum=4096)
    _string(item.get("meaning"), f"{path}/meaning", issues, maximum=16384)
    state = _string(item.get("state"), f"{path}/state", issues)
    if state not in _BINDING_STATES:
        issues.append(f"{path}/state: unsupported binding state")
    _evidence_refs(item.get("evidence_refs"), f"{path}/evidence_refs", issues, nonempty=True)
    return item


def _invariant(value: Any, path: str, issues: list[str]) -> Mapping[str, Any] | None:
    item = _closed(
        value, frozenset({"invariant_id", "statement", "origin", "evidence_refs"}), path, issues
    )
    if item is None:
        return None
    _string(item.get("invariant_id"), f"{path}/invariant_id", issues, pattern=_TOKEN)
    _string(item.get("statement"), f"{path}/statement", issues, maximum=16384)
    origin = _string(item.get("origin"), f"{path}/origin", issues)
    if origin not in _ORIGINS:
        issues.append(f"{path}/origin: unsupported invariant origin")
    _evidence_refs(item.get("evidence_refs"), f"{path}/evidence_refs", issues, nonempty=True)
    return item


def _context_budget(value: Any, path: str, issues: list[str]) -> Mapping[str, Any] | None:
    """Validate the shared explicit/inherited context-limit contract."""

    budget = _closed(value, _CONTEXT_BUDGET_FIELDS, path, issues)
    if budget is None:
        return None
    mode = _string(budget.get("mode"), f"{path}/mode", issues)
    if mode not in {"explicit", "inherited_v1"}:
        issues.append(f"{path}/mode: unsupported mode")
    for field in _CONTEXT_BUDGET_LIMITS:
        if budget.get(field) is not None:
            _integer(budget.get(field), f"{path}/{field}", issues, minimum=0)
    if mode == "explicit" and any(budget.get(field) is None for field in _CONTEXT_BUDGET_LIMITS):
        issues.append(f"{path}: explicit mode requires all limits")
    if mode == "inherited_v1" and any(
        budget.get(field) is not None for field in _CONTEXT_BUDGET_LIMITS
    ):
        issues.append(f"{path}: inherited_v1 mode must retain unavailable limits as null")
    if (
        type(budget.get("reserve_bytes")) is int
        and type(budget.get("max_bytes")) is int
        and budget["reserve_bytes"] > budget["max_bytes"]
    ):
        issues.append(f"{path}/reserve_bytes: exceeds max_bytes")
    if (
        type(budget.get("reserve_tokens")) is int
        and type(budget.get("max_tokens")) is int
        and budget["reserve_tokens"] > budget["max_tokens"]
    ):
        issues.append(f"{path}/reserve_tokens: exceeds max_tokens")
    return budget


def _legacy_policy_snapshot(value: Any, path: str, issues: list[str]) -> None:
    """Validate the copied v1 operational policy without interpreting project prose."""

    item = _closed(
        value,
        frozenset({"policies", "human_in_loop", "stop_conditions", "capability_refs"}),
        path,
        issues,
    )
    if item is None:
        return
    policies = _closed(
        item.get("policies"),
        frozenset({"dispatch", "review", "integration", "expiry"}),
        f"{path}/policies",
        issues,
    )
    if policies is not None:
        dispatch = _closed(
            policies.get("dispatch"),
            frozenset({"max_parallel", "attempt_limit"}),
            f"{path}/policies/dispatch",
            issues,
        )
        if dispatch is not None:
            _integer(
                dispatch.get("max_parallel"),
                f"{path}/policies/dispatch/max_parallel",
                issues,
                minimum=1,
            )
            _integer(
                dispatch.get("attempt_limit"),
                f"{path}/policies/dispatch/attempt_limit",
                issues,
                minimum=1,
            )
        review = _closed(
            policies.get("review"),
            frozenset({"minimum_approvals", "independence", "require_fresh_receipt"}),
            f"{path}/policies/review",
            issues,
        )
        if review is not None:
            _integer(
                review.get("minimum_approvals"),
                f"{path}/policies/review/minimum_approvals",
                issues,
                minimum=1,
            )
            _string(review.get("independence"), f"{path}/policies/review/independence", issues)
            _boolean(
                review.get("require_fresh_receipt"),
                f"{path}/policies/review/require_fresh_receipt",
                issues,
            )
        integration = _closed(
            policies.get("integration"),
            frozenset({"mode", "integrator_work_unit_id", "require_green_gate"}),
            f"{path}/policies/integration",
            issues,
        )
        if integration is not None:
            _string(integration.get("mode"), f"{path}/policies/integration/mode", issues)
            _string(
                integration.get("integrator_work_unit_id"),
                f"{path}/policies/integration/integrator_work_unit_id",
                issues,
                pattern=_TOKEN,
            )
            _boolean(
                integration.get("require_green_gate"),
                f"{path}/policies/integration/require_green_gate",
                issues,
            )
        expiry = _closed(
            policies.get("expiry"),
            frozenset({"maximum_run_seconds", "on_expiry"}),
            f"{path}/policies/expiry",
            issues,
        )
        if expiry is not None:
            _integer(
                expiry.get("maximum_run_seconds"),
                f"{path}/policies/expiry/maximum_run_seconds",
                issues,
                minimum=1,
            )
            _string(expiry.get("on_expiry"), f"{path}/policies/expiry/on_expiry", issues)
    hil = _closed(
        item.get("human_in_loop"),
        frozenset(
            {"checkpoints", "decision_timeout_seconds", "on_timeout", "required_for_stop_override"}
        ),
        f"{path}/human_in_loop",
        issues,
    )
    if hil is not None:
        _strings(hil.get("checkpoints"), f"{path}/human_in_loop/checkpoints", issues, nonempty=True)
        _integer(
            hil.get("decision_timeout_seconds"),
            f"{path}/human_in_loop/decision_timeout_seconds",
            issues,
            minimum=1,
        )
        _string(hil.get("on_timeout"), f"{path}/human_in_loop/on_timeout", issues)
        _boolean(
            hil.get("required_for_stop_override"),
            f"{path}/human_in_loop/required_for_stop_override",
            issues,
        )
    _strings(item.get("stop_conditions"), f"{path}/stop_conditions", issues, nonempty=True)
    _strings(
        item.get("capability_refs"),
        f"{path}/capability_refs",
        issues,
        pattern=_TOKEN,
        nonempty=True,
    )


def _obligation(value: Any, path: str, issues: list[str]) -> Mapping[str, Any] | None:
    item = _closed(value, frozenset({"source_id", "obligation_id", "statement"}), path, issues)
    if item is None:
        return None
    _string(item.get("source_id"), f"{path}/source_id", issues, pattern=_TOKEN)
    _string(item.get("obligation_id"), f"{path}/obligation_id", issues, pattern=_TOKEN)
    _string(item.get("statement"), f"{path}/statement", issues, maximum=16384)
    return item


def _validate_intent(value: Any, issues: list[str]) -> None:
    fields = frozenset(
        {
            "schema_version",
            "record_kind",
            "canonical_algorithm",
            "intent_envelope_id",
            "mode",
            "goal",
            "intent",
            "constraints",
            "non_goals",
            "authority_refs",
            "obligations",
            "bindings",
            "invariants",
            "campaign_envelope",
            "approval",
        }
    )
    record = _closed(value, fields, "<root>", issues)
    if record is None:
        return
    if record.get("schema_version") != "1":
        issues.append("schema_version: expected '1'")
    if record.get("record_kind") != "intent_envelope":
        issues.append("record_kind: expected 'intent_envelope'")
    if record.get("canonical_algorithm") != CANONICAL_ALGORITHM:
        issues.append(f"canonical_algorithm: expected {CANONICAL_ALGORITHM!r}")
    _string(record.get("intent_envelope_id"), "intent_envelope_id", issues, pattern=_TOKEN)
    mode = _string(record.get("mode"), "mode", issues)
    if mode not in {"native_v2", "v1_compatibility"}:
        issues.append("mode: expected native_v2 or v1_compatibility")
    _string(record.get("goal"), "goal", issues, maximum=16384)
    _string(record.get("intent"), "intent", issues, maximum=32768)
    _strings(record.get("constraints"), "constraints", issues)
    _strings(record.get("non_goals"), "non_goals", issues)
    authority = _authority_refs(record.get("authority_refs"), "authority_refs", issues)
    authority_ids = {item.get("source_id") for item in authority}
    obligations = record.get("obligations")
    obligation_keys: set[tuple[Any, Any]] = set()
    if not isinstance(obligations, list) or not obligations:
        issues.append("obligations: expected non-empty array")
    else:
        for index, item in enumerate(obligations):
            obligation = _obligation(item, f"obligations/{index}", issues)
            if obligation is not None:
                key = (obligation.get("source_id"), obligation.get("obligation_id"))
                if key in obligation_keys:
                    issues.append(f"obligations: duplicate obligation {key!r}")
                obligation_keys.add(key)
                if obligation.get("source_id") not in authority_ids:
                    issues.append(f"obligations/{index}: source_id is not in authority_refs")
    bindings = record.get("bindings")
    binding_ids: set[Any] = set()
    if not isinstance(bindings, list):
        issues.append("bindings: expected array")
    else:
        for index, item in enumerate(bindings):
            binding = _binding(item, f"bindings/{index}", issues)
            if binding is not None:
                binding_id = binding.get("binding_id")
                if binding_id in binding_ids:
                    issues.append(f"bindings: duplicate binding_id {binding_id!r}")
                binding_ids.add(binding_id)
    invariants = record.get("invariants")
    invariant_ids: set[Any] = set()
    if not isinstance(invariants, list) or not invariants:
        issues.append("invariants: expected non-empty array")
    else:
        for index, item in enumerate(invariants):
            invariant = _invariant(item, f"invariants/{index}", issues)
            if invariant is not None:
                invariant_id = invariant.get("invariant_id")
                if invariant_id in invariant_ids:
                    issues.append(f"invariants: duplicate invariant_id {invariant_id!r}")
                invariant_ids.add(invariant_id)
    envelope = _closed(
        record.get("campaign_envelope"),
        frozenset(
            {
                "envelope_id",
                "mutation_envelope",
                "policy_refs",
                "policy_snapshot",
                "risk",
                "budget",
                "approval",
            }
        ),
        "campaign_envelope",
        issues,
    )
    if envelope is not None:
        _string(
            envelope.get("envelope_id"), "campaign_envelope/envelope_id", issues, pattern=_TOKEN
        )
        _claim_set(envelope.get("mutation_envelope"), "campaign_envelope/mutation_envelope", issues)
        _strings(
            envelope.get("policy_refs"),
            "campaign_envelope/policy_refs",
            issues,
            pattern=_SHA256,
            nonempty=True,
        )
        _legacy_policy_snapshot(
            envelope.get("policy_snapshot"), "campaign_envelope/policy_snapshot", issues
        )
        risk = _closed(
            envelope.get("risk"),
            frozenset({"level", "max_repair_episodes"}),
            "campaign_envelope/risk",
            issues,
        )
        if risk is not None:
            _string(risk.get("level"), "campaign_envelope/risk/level", issues)
            _integer(
                risk.get("max_repair_episodes"),
                "campaign_envelope/risk/max_repair_episodes",
                issues,
                minimum=0,
                maximum=100,
            )
        budget = _closed(
            envelope.get("budget"),
            frozenset(
                {"max_seconds", "max_provider_tokens", "max_provider_spend_cents", "unknown_limits"}
            ),
            "campaign_envelope/budget",
            issues,
        )
        if budget is not None:
            for field in ("max_seconds", "max_provider_tokens", "max_provider_spend_cents"):
                if budget.get(field) is not None:
                    _integer(
                        budget.get(field), f"campaign_envelope/budget/{field}", issues, minimum=0
                    )
            unknown = _strings(
                budget.get("unknown_limits"), "campaign_envelope/budget/unknown_limits", issues
            )
            for field in unknown:
                if field not in {"max_seconds", "max_provider_tokens", "max_provider_spend_cents"}:
                    issues.append(
                        f"campaign_envelope/budget/unknown_limits: unsupported field {field!r}"
                    )
                elif budget.get(field) is not None:
                    issues.append(f"campaign_envelope/budget/{field}: unknown limits must be null")
        _approval(envelope.get("approval"), "campaign_envelope/approval", issues)
    _approval(record.get("approval"), "approval", issues)


def _approval(value: Any, path: str, issues: list[str]) -> Mapping[str, Any] | None:
    item = _closed(
        value, frozenset({"mode", "evidence_refs", "approved_at", "approved_by"}), path, issues
    )
    if item is None:
        return None
    mode = _string(item.get("mode"), f"{path}/mode", issues)
    if mode not in {"project_sealed", "human_approved", "derived"}:
        issues.append(f"{path}/mode: unsupported approval mode")
    _evidence_refs(item.get("evidence_refs"), f"{path}/evidence_refs", issues, nonempty=True)
    _timestamp(item.get("approved_at"), f"{path}/approved_at", issues)
    _string(item.get("approved_by"), f"{path}/approved_by", issues, pattern=_TOKEN)
    return item


def _capsule(value: Any, path: str, issues: list[str]) -> Mapping[str, Any] | None:
    fields = frozenset(
        {
            "capsule_id",
            "intent_ref",
            "obligation_coverage",
            "binding_refs",
            "invariant_refs",
            "depends_on",
            "mutation_envelope",
            "expected_surface",
            "validation_profiles",
            "completion_boundary",
            "reconciliation_triggers",
            "hil_policy_refs",
            "provider_capability_needs",
            "context_budget",
            "legacy_work_unit",
        }
    )
    item = _closed(value, fields, path, issues)
    if item is None:
        return None
    _string(item.get("capsule_id"), f"{path}/capsule_id", issues, pattern=_TOKEN)
    _string(item.get("intent_ref"), f"{path}/intent_ref", issues, pattern=_TOKEN)
    coverage = item.get("obligation_coverage")
    if not isinstance(coverage, list) or not coverage:
        issues.append(f"{path}/obligation_coverage: expected non-empty array")
    else:
        seen: set[tuple[Any, Any]] = set()
        for index, ref in enumerate(coverage):
            row = _closed(
                ref,
                frozenset({"source_id", "obligation_id"}),
                f"{path}/obligation_coverage/{index}",
                issues,
            )
            if row is None:
                continue
            source = _string(
                row.get("source_id"),
                f"{path}/obligation_coverage/{index}/source_id",
                issues,
                pattern=_TOKEN,
            )
            obligation = _string(
                row.get("obligation_id"),
                f"{path}/obligation_coverage/{index}/obligation_id",
                issues,
                pattern=_TOKEN,
            )
            key = (source, obligation)
            if key in seen:
                issues.append(f"{path}/obligation_coverage: duplicate coverage {key!r}")
            seen.add(key)
    _strings(item.get("binding_refs"), f"{path}/binding_refs", issues, pattern=_TOKEN)
    _strings(item.get("invariant_refs"), f"{path}/invariant_refs", issues, pattern=_TOKEN)
    _strings(item.get("depends_on"), f"{path}/depends_on", issues, pattern=_TOKEN)
    _claim_set(item.get("mutation_envelope"), f"{path}/mutation_envelope", issues)
    _expected_surface(item.get("expected_surface"), f"{path}/expected_surface", issues)
    profiles = item.get("validation_profiles")
    profile_ids: set[Any] = set()
    if not isinstance(profiles, list) or not profiles:
        issues.append(f"{path}/validation_profiles: expected non-empty array")
    else:
        for index, profile in enumerate(profiles):
            profile_path = f"{path}/validation_profiles/{index}"
            row = _closed(
                profile,
                frozenset({"profile_id", "command_refs", "gate_refs"}),
                profile_path,
                issues,
            )
            if row is None:
                continue
            profile_id = _string(
                row.get("profile_id"), f"{profile_path}/profile_id", issues, pattern=_TOKEN
            )
            if profile_id in profile_ids:
                issues.append(f"{path}/validation_profiles: duplicate profile_id {profile_id!r}")
            profile_ids.add(profile_id)
            _strings(
                row.get("command_refs"),
                f"{profile_path}/command_refs",
                issues,
                pattern=_TOKEN,
                nonempty=True,
            )
            _strings(
                row.get("gate_refs"),
                f"{profile_path}/gate_refs",
                issues,
                pattern=_TOKEN,
                nonempty=True,
            )
    boundary = _closed(
        item.get("completion_boundary"),
        frozenset({"required_gate_refs", "required_receipt_kinds", "required_artifact_refs"}),
        f"{path}/completion_boundary",
        issues,
    )
    if boundary is not None:
        _strings(
            boundary.get("required_gate_refs"),
            f"{path}/completion_boundary/required_gate_refs",
            issues,
            pattern=_TOKEN,
            nonempty=True,
        )
        _strings(
            boundary.get("required_receipt_kinds"),
            f"{path}/completion_boundary/required_receipt_kinds",
            issues,
            pattern=_TOKEN,
            nonempty=True,
        )
        _strings(
            boundary.get("required_artifact_refs"),
            f"{path}/completion_boundary/required_artifact_refs",
            issues,
            pattern=_TOKEN,
        )
    _strings(
        item.get("reconciliation_triggers"),
        f"{path}/reconciliation_triggers",
        issues,
        pattern=None,
        nonempty=True,
    )
    for index, trigger in enumerate(
        item.get("reconciliation_triggers", [])
        if isinstance(item.get("reconciliation_triggers"), list)
        else []
    ):
        if trigger not in _RECONCILIATION_TRIGGERS:
            issues.append(f"{path}/reconciliation_triggers/{index}: unsupported trigger")
    _strings(
        item.get("hil_policy_refs"),
        f"{path}/hil_policy_refs",
        issues,
        pattern=_TOKEN,
        nonempty=True,
    )
    _strings(
        item.get("provider_capability_needs"),
        f"{path}/provider_capability_needs",
        issues,
        pattern=_TOKEN,
        nonempty=True,
    )
    _context_budget(item.get("context_budget"), f"{path}/context_budget", issues)
    if "legacy_work_unit" not in item:
        issues.append(f"{path}/legacy_work_unit: required; use null for native_v2 capsules")
    legacy = item.get("legacy_work_unit")
    if legacy is not None:
        row = _closed(
            legacy,
            frozenset({"work_unit_id", "work_unit_sha256", "record"}),
            f"{path}/legacy_work_unit",
            issues,
        )
        if row is not None:
            _string(
                row.get("work_unit_id"),
                f"{path}/legacy_work_unit/work_unit_id",
                issues,
                pattern=_TOKEN,
            )
            _sha(row.get("work_unit_sha256"), f"{path}/legacy_work_unit/work_unit_sha256", issues)
            try:
                validate_work_unit(row.get("record"))
            except CampaignContractError as error:
                issues.extend(f"{path}/legacy_work_unit/record: {entry}" for entry in error.issues)
            if isinstance(row.get("record"), Mapping) and row.get("work_unit_id") != row[
                "record"
            ].get("work_unit_id"):
                issues.append(f"{path}/legacy_work_unit/work_unit_id: does not match record")
    return item


def _subject(value: Any, path: str, issues: list[str]) -> Mapping[str, Any] | None:
    item = _closed(
        value, frozenset({"repository_id", "base_oid", "base_tree_sha256"}), path, issues
    )
    if item is None:
        return None
    _string(item.get("repository_id"), f"{path}/repository_id", issues, pattern=_TOKEN)
    _oid(item.get("base_oid"), f"{path}/base_oid", issues)
    _sha(item.get("base_tree_sha256"), f"{path}/base_tree_sha256", issues)
    return item


def _revision(value: Any, path: str, issues: list[str]) -> Mapping[str, Any] | None:
    item = _closed(
        value,
        frozenset(
            {
                "revision_id",
                "predecessor_sha256",
                "triggering_evidence_refs",
                "affected_capsule_ids",
                "superseded_capsule_ids",
                "preserved_capsule_ids",
                "invalidated_assumption_refs",
                "obligation_remapping",
                "approval_mode",
            }
        ),
        path,
        issues,
    )
    if item is None:
        return None
    _string(item.get("revision_id"), f"{path}/revision_id", issues, pattern=_TOKEN)
    predecessor = item.get("predecessor_sha256")
    if predecessor is not None:
        _sha(predecessor, f"{path}/predecessor_sha256", issues)
    _evidence_refs(
        item.get("triggering_evidence_refs"),
        f"{path}/triggering_evidence_refs",
        issues,
        nonempty=False,
    )
    affected = _strings(
        item.get("affected_capsule_ids"), f"{path}/affected_capsule_ids", issues, pattern=_TOKEN
    )
    superseded = set(
        _strings(
            item.get("superseded_capsule_ids"),
            f"{path}/superseded_capsule_ids",
            issues,
            pattern=_TOKEN,
        )
    )
    preserved = set(
        _strings(
            item.get("preserved_capsule_ids"),
            f"{path}/preserved_capsule_ids",
            issues,
            pattern=_TOKEN,
        )
    )
    if not superseded <= set(affected):
        issues.append(f"{path}/superseded_capsule_ids: must be affected capsules")
    if set(affected) & preserved:
        issues.append(f"{path}: affected and preserved capsules overlap")
    _strings(
        item.get("invalidated_assumption_refs"),
        f"{path}/invalidated_assumption_refs",
        issues,
        pattern=_TOKEN,
    )
    remapping = item.get("obligation_remapping")
    if not isinstance(remapping, list):
        issues.append(f"{path}/obligation_remapping: expected array")
    else:
        for index, row in enumerate(remapping):
            entry = _closed(
                row,
                frozenset({"source_id", "obligation_id", "capsule_id"}),
                f"{path}/obligation_remapping/{index}",
                issues,
            )
            if entry is None:
                continue
            _string(
                entry.get("source_id"),
                f"{path}/obligation_remapping/{index}/source_id",
                issues,
                pattern=_TOKEN,
            )
            _string(
                entry.get("obligation_id"),
                f"{path}/obligation_remapping/{index}/obligation_id",
                issues,
                pattern=_TOKEN,
            )
            _string(
                entry.get("capsule_id"),
                f"{path}/obligation_remapping/{index}/capsule_id",
                issues,
                pattern=_TOKEN,
            )
    mode = _string(item.get("approval_mode"), f"{path}/approval_mode", issues)
    if mode not in _APPROVAL_MODES:
        issues.append(f"{path}/approval_mode: unsupported approval mode")
    return item


def _validate_plan(value: Any, issues: list[str]) -> None:
    fields = frozenset(
        {
            "schema_version",
            "record_kind",
            "canonical_algorithm",
            "plan_id",
            "intent_envelope_sha256",
            "campaign_envelope_sha256",
            "subject",
            "revision",
            "obligation_coverage_mode",
            "capsules",
        }
    )
    record = _closed(value, fields, "<root>", issues)
    if record is None:
        return
    if record.get("schema_version") != "1":
        issues.append("schema_version: expected '1'")
    if record.get("record_kind") != "capsule_plan":
        issues.append("record_kind: expected 'capsule_plan'")
    if record.get("canonical_algorithm") != CANONICAL_ALGORITHM:
        issues.append(f"canonical_algorithm: expected {CANONICAL_ALGORITHM!r}")
    _string(record.get("plan_id"), "plan_id", issues, pattern=_TOKEN)
    _sha(record.get("intent_envelope_sha256"), "intent_envelope_sha256", issues)
    _sha(record.get("campaign_envelope_sha256"), "campaign_envelope_sha256", issues)
    _subject(record.get("subject"), "subject", issues)
    _revision(record.get("revision"), "revision", issues)
    mode = _string(record.get("obligation_coverage_mode"), "obligation_coverage_mode", issues)
    if mode not in {"complete", "partial"}:
        issues.append("obligation_coverage_mode: expected complete or partial")
    capsules = record.get("capsules")
    capsule_ids: set[Any] = set()
    if not isinstance(capsules, list) or not capsules:
        issues.append("capsules: expected non-empty array")
    else:
        for index, capsule in enumerate(capsules):
            item = _capsule(capsule, f"capsules/{index}", issues)
            if item is not None:
                capsule_id = item.get("capsule_id")
                if capsule_id in capsule_ids:
                    issues.append(f"capsules: duplicate capsule_id {capsule_id!r}")
                capsule_ids.add(capsule_id)
    graph = {item: set() for item in capsule_ids if isinstance(item, str)}
    for capsule in capsules if isinstance(capsules, list) else []:
        if not isinstance(capsule, Mapping) or not isinstance(capsule.get("capsule_id"), str):
            continue
        current = capsule["capsule_id"]
        deps = capsule.get("depends_on", [])
        if not isinstance(deps, list):
            continue
        for dependency in deps:
            if not isinstance(dependency, str):
                continue
            if dependency == current:
                issues.append(f"capsules/{current}: may not depend on itself")
            elif dependency not in graph:
                issues.append(f"capsules/{current}: unknown dependency {dependency!r}")
            else:
                graph[current].add(dependency)
    if _graph_cycle(graph) is not None:
        issues.append("capsules: dependency cycle")


def _graph_cycle(graph: Mapping[str, set[str]]) -> tuple[str, ...] | None:
    state: dict[str, str] = {}
    stack: list[str] = []

    def visit(node: str) -> tuple[str, ...] | None:
        state[node] = "active"
        stack.append(node)
        for target in sorted(graph.get(node, set())):
            if state.get(target) == "active":
                return (*stack[stack.index(target) :], target)
            if state.get(target) is None:
                found = visit(target)
                if found is not None:
                    return found
        stack.pop()
        state[node] = "done"
        return None

    for node in sorted(graph):
        if state.get(node) is None:
            found = visit(node)
            if found is not None:
                return found
    return None


def _packet_sources(value: Any, path: str, issues: list[str]) -> None:
    if not isinstance(value, list) or not value:
        issues.append(f"{path}: expected non-empty array")
        return
    seen: set[str] = set()
    for index, source in enumerate(value):
        item_path = f"{path}/{index}"
        # EC-02 packets did not carry rendering provenance.  Accept those records for
        # compatibility, while the compiler below materializes the EC-03 defaults.
        source_value = dict(source) if isinstance(source, Mapping) else source
        if isinstance(source_value, dict):
            source_value.setdefault("tier", "p1")
            source_value.setdefault("source_form", "full")
            source_value.setdefault("estimate", None)
            for optional in (
                "rendered_sha256",
                "rendered_bytes",
                "full_bytes",
                "full_estimated_tokens",
                "truth_state",
                "projection_of",
            ):
                source_value.setdefault(optional, None)
        row = _closed(
            source_value,
            frozenset(
                {
                    "source_id",
                    "source_sha256",
                    "kind",
                    "selection",
                    "reason",
                    "bytes",
                    "estimated_tokens",
                    "tier",
                    "source_form",
                    "estimate",
                    "rendered_sha256",
                    "rendered_bytes",
                    "full_bytes",
                    "full_estimated_tokens",
                    "truth_state",
                    "projection_of",
                }
            ),
            item_path,
            issues,
        )
        if row is None:
            continue
        source_id = _string(row.get("source_id"), f"{item_path}/source_id", issues, pattern=_TOKEN)
        _sha(row.get("source_sha256"), f"{item_path}/source_sha256", issues)
        kind = _string(row.get("kind"), f"{item_path}/kind", issues)
        if kind not in {
            "project_authority",
            "accepted_binding",
            "invariant",
            "observation",
            "proposal",
            "plan",
        }:
            issues.append(f"{item_path}/kind: unsupported source kind")
        selection = _string(row.get("selection"), f"{item_path}/selection", issues)
        if selection not in {"included", "excluded"}:
            issues.append(f"{item_path}/selection: expected included or excluded")
        _string(row.get("reason"), f"{item_path}/reason", issues, maximum=4096)
        _integer(row.get("bytes"), f"{item_path}/bytes", issues, minimum=0)
        _integer(row.get("estimated_tokens"), f"{item_path}/estimated_tokens", issues, minimum=0)
        tier = row.get("tier", "p1")
        if tier not in {"p0", "p1", "p2", "p3"}:
            issues.append(f"{item_path}/tier: expected p0, p1, p2, or p3")
        source_form = row.get("source_form", "full")
        if source_form not in {"full", "projection"}:
            issues.append(f"{item_path}/source_form: expected full or projection")
        estimate = row.get("estimate")
        if estimate is not None:
            if not isinstance(estimate, Mapping):
                issues.append(f"{item_path}/estimate: expected object or null")
            else:
                estimate_fields = frozenset({"tokens", "basis"})
                estimate_row = _closed(estimate, estimate_fields, f"{item_path}/estimate", issues)
                if estimate_row is not None:
                    _integer(
                        estimate_row.get("tokens"),
                        f"{item_path}/estimate/tokens",
                        issues,
                        minimum=0,
                    )
                    _string(
                        estimate_row.get("basis"),
                        f"{item_path}/estimate/basis",
                        issues,
                        maximum=256,
                    )
        for field in ("rendered_sha256",):
            if row.get(field) is not None:
                _sha(row.get(field), f"{item_path}/{field}", issues)
        for field in ("rendered_bytes", "full_bytes", "full_estimated_tokens"):
            if row.get(field) is not None:
                _integer(row.get(field), f"{item_path}/{field}", issues, minimum=0)
        if row.get("truth_state") is not None and row.get("truth_state") not in {
            "accepted",
            "observed",
            "proposed",
        }:
            issues.append(f"{item_path}/truth_state: unsupported state")
        if row.get("projection_of") is not None:
            _strings(row.get("projection_of"), f"{item_path}/projection_of", issues, pattern=_TOKEN)
        if source_id is not None and source_id in seen:
            issues.append(f"{path}: duplicate source_id {source_id!r}")
        if source_id is not None:
            seen.add(source_id)


def _validate_packet(value: Any, issues: list[str]) -> None:
    fields = frozenset(
        {
            "schema_version",
            "record_kind",
            "canonical_algorithm",
            "packet_id",
            "intent_envelope_sha256",
            "plan_sha256",
            "revision_id",
            "capsule_id",
            "episode_id",
            "subject",
            "policy_sha256",
            "lease_sha256",
            "context_budget",
            "sources",
            "totals",
            "state_delta",
        }
    )
    packet_value = dict(value) if isinstance(value, Mapping) else value
    if isinstance(packet_value, dict):
        # Packets persisted by EC-02 predate the state-delta projection.  They remain
        # readable; newly compiled packets always carry the explicit field.
        packet_value.setdefault("state_delta", {})
    record = _closed(packet_value, fields, "<root>", issues)
    if record is None:
        return
    if record.get("schema_version") != "1":
        issues.append("schema_version: expected '1'")
    if record.get("record_kind") != "execution_packet":
        issues.append("record_kind: expected 'execution_packet'")
    if record.get("canonical_algorithm") != CANONICAL_ALGORITHM:
        issues.append(f"canonical_algorithm: expected {CANONICAL_ALGORITHM!r}")
    for field in ("packet_id", "revision_id", "capsule_id", "episode_id"):
        _string(record.get(field), field, issues, pattern=_TOKEN)
    for field in ("intent_envelope_sha256", "plan_sha256", "policy_sha256", "lease_sha256"):
        _sha(record.get(field), field, issues)
    _subject(record.get("subject"), "subject", issues)
    budget = _context_budget(record.get("context_budget"), "context_budget", issues)
    _packet_sources(record.get("sources"), "sources", issues)
    delta = record.get("state_delta", {})
    if not isinstance(delta, Mapping):
        issues.append("state_delta: expected object")
    else:
        allowed_delta = {
            "changed_facts",
            "candidate",
            "git",
            "validation_state_changes",
            "discoveries",
            "unresolved_decisions",
            "next_action",
        }
        unknown = set(delta) - allowed_delta
        if unknown:
            issues.append(f"state_delta: unknown field(s) {sorted(unknown)!r}")
        for field in (
            "changed_facts",
            "validation_state_changes",
            "discoveries",
            "unresolved_decisions",
        ):
            items = delta.get(field, [])
            if not isinstance(items, list) or len(items) > 4096:
                issues.append(f"state_delta/{field}: expected at most 4096 strings")
            else:
                for index, item in enumerate(items):
                    _string(item, f"state_delta/{field}/{index}", issues, maximum=4096)
        if delta.get("next_action") is not None:
            _string(delta.get("next_action"), "state_delta/next_action", issues, maximum=4096)
        for field, allowed in (
            ("candidate", {"base_oid", "head_oid", "tree_oid", "patch_sha256", "clean"}),
            ("git", {"base_oid", "head_oid", "tree_oid", "repository_common_dir_sha256",
                     "worktree_sha256"}),
        ):
            value = delta.get(field)
            if value is None:
                continue
            if not isinstance(value, Mapping):
                issues.append(f"state_delta/{field}: expected object or null")
                continue
            unknown_identity = set(value) - allowed
            if unknown_identity:
                issues.append(f"state_delta/{field}: unknown field(s) {sorted(unknown_identity)!r}")
            for name in set(value) & allowed:
                label = f"state_delta/{field}/{name}"
                if name.endswith("_oid"):
                    _oid(value[name], label, issues)
                elif name.endswith("_sha256"):
                    _sha(value[name], label, issues)
                else:
                    _boolean(value[name], label, issues)
    totals_value = record.get("totals")
    totals_fields = frozenset(
        {
            "included_bytes",
            "included_estimated_tokens",
            "excluded_bytes",
            "prompt_bytes",
            "prompt_sha256",
            "repeated_context_bytes",
            "context_reserve_bytes",
            "context_reserve_tokens",
            "estimated_prompt_tokens",
        }
    )
    if isinstance(totals_value, Mapping):
        missing = {"included_bytes", "included_estimated_tokens", "excluded_bytes"} - set(
            totals_value
        )
        unknown = set(totals_value) - totals_fields
        for field in sorted(missing):
            issues.append(f"totals: missing field {field!r}")
        if unknown:
            issues.append(f"totals: unknown field(s) {sorted(unknown)!r}")
        totals = totals_value
    else:
        issues.append("totals: expected object")
        totals = None
    if totals is not None:
        for field in totals:
            if field != "prompt_sha256":
                _integer(totals.get(field), f"totals/{field}", issues, minimum=0)
            elif totals.get(field) is not None:
                _sha(totals.get(field), f"totals/{field}", issues)
        sources = record.get("sources") if isinstance(record.get("sources"), list) else []
        included_bytes = sum(
            item.get("bytes", 0)
            for item in sources
            if isinstance(item, Mapping)
            and item.get("selection") == "included"
            and type(item.get("bytes")) is int
        )
        included_tokens = sum(
            item.get("estimated_tokens", 0)
            for item in sources
            if isinstance(item, Mapping)
            and item.get("selection") == "included"
            and type(item.get("estimated_tokens")) is int
        )
        excluded_bytes = sum(
            item.get("bytes", 0)
            for item in sources
            if isinstance(item, Mapping)
            and item.get("selection") == "excluded"
            and type(item.get("bytes")) is int
        )
        if totals.get("included_bytes") != included_bytes:
            issues.append("totals/included_bytes: does not reconcile with selected sources")
        if totals.get("included_estimated_tokens") != included_tokens:
            issues.append(
                "totals/included_estimated_tokens: does not reconcile with selected sources"
            )
        if totals.get("excluded_bytes") != excluded_bytes:
            issues.append("totals/excluded_bytes: does not reconcile with selected sources")
        if (
            budget is not None
            and type(budget.get("max_bytes")) is int
            and type(budget.get("reserve_bytes")) is int
            and included_bytes + budget["reserve_bytes"] > budget["max_bytes"]
        ):
            issues.append("sources: included bytes plus reserve exceed context budget")
        if (
            budget is not None
            and type(budget.get("max_tokens")) is int
            and type(budget.get("reserve_tokens")) is int
            and included_tokens + budget["reserve_tokens"] > budget["max_tokens"]
        ):
            issues.append("sources: included tokens plus reserve exceed context budget")

        for measured, maximum, reserve in (
            ("prompt_bytes", "max_bytes", "reserve_bytes"),
            ("estimated_prompt_tokens", "max_tokens", "reserve_tokens"),
        ):
            if (
                budget is not None
                and type(totals.get(measured)) is int
                and type(budget.get(maximum)) is int
                and type(budget.get(reserve)) is int
                and totals[measured] + budget[reserve] > budget[maximum]
            ):
                issues.append(f"totals/{measured}: rendered prompt plus reserve exceeds budget")
        if (
            type(totals.get("repeated_context_bytes")) is int
            and type(totals.get("prompt_bytes")) is int
            and totals["repeated_context_bytes"] > totals["prompt_bytes"]
        ):
            issues.append("totals/repeated_context_bytes: exceeds total prompt bytes")


def _validate_journal(value: Any, issues: list[str]) -> None:
    fields = frozenset(
        {
            "schema_version",
            "record_kind",
            "canonical_algorithm",
            "journal_id",
            "intent_envelope_sha256",
            "plan_sha256",
            "revision_id",
            "capsule_id",
            "subject",
            "entries",
        }
    )
    record = _closed(value, fields, "<root>", issues)
    if record is None:
        return
    if record.get("schema_version") != "1":
        issues.append("schema_version: expected '1'")
    if record.get("record_kind") != "capsule_journal":
        issues.append("record_kind: expected 'capsule_journal'")
    if record.get("canonical_algorithm") != CANONICAL_ALGORITHM:
        issues.append(f"canonical_algorithm: expected {CANONICAL_ALGORITHM!r}")
    for field in ("journal_id", "revision_id", "capsule_id"):
        _string(record.get(field), field, issues, pattern=_TOKEN)
    for field in ("intent_envelope_sha256", "plan_sha256"):
        _sha(record.get(field), field, issues)
    _subject(record.get("subject"), "subject", issues)
    entries = record.get("entries")
    if not isinstance(entries, list):
        issues.append("entries: expected array")
        return
    event_ids: set[Any] = set()
    for index, entry in enumerate(entries):
        path = f"entries/{index}"
        row = _closed(
            entry,
            frozenset({"sequence", "event_id", "event_kind", "occurred_at", "evidence_refs"}),
            path,
            issues,
        )
        if row is None:
            continue
        _integer(row.get("sequence"), f"{path}/sequence", issues, minimum=1)
        event_id = _string(row.get("event_id"), f"{path}/event_id", issues, pattern=_TOKEN)
        if event_id in event_ids:
            issues.append(f"entries: duplicate event_id {event_id!r}")
        event_ids.add(event_id)
        event_kind = _string(row.get("event_kind"), f"{path}/event_kind", issues)
        if event_kind not in _EVENTS:
            issues.append(f"{path}/event_kind: unsupported event kind")
        _timestamp(row.get("occurred_at"), f"{path}/occurred_at", issues)
        _evidence_refs(row.get("evidence_refs"), f"{path}/evidence_refs", issues)
    sequences = [entry.get("sequence") for entry in entries if isinstance(entry, Mapping)]
    if sequences != list(range(1, len(entries) + 1)):
        issues.append("entries: sequence must be contiguous from 1")


def _validate_result(value: Any, issues: list[str]) -> None:
    fields = frozenset(
        {
            "schema_version",
            "record_kind",
            "canonical_algorithm",
            "result_id",
            "intent_envelope_sha256",
            "plan_sha256",
            "revision_id",
            "capsule_id",
            "subject",
            "status",
            "candidate",
            "obligation_coverage",
            "validation",
            "invariants",
            "reconciliation",
            "findings",
            "review_refs",
            "integration_refs",
            "administration_metrics",
        }
    )
    record = _closed(value, fields, "<root>", issues)
    if record is None:
        return
    if record.get("schema_version") != "1":
        issues.append("schema_version: expected '1'")
    if record.get("record_kind") != "capsule_result":
        issues.append("record_kind: expected 'capsule_result'")
    if record.get("canonical_algorithm") != CANONICAL_ALGORITHM:
        issues.append(f"canonical_algorithm: expected {CANONICAL_ALGORITHM!r}")
    for field in ("result_id", "revision_id", "capsule_id"):
        _string(record.get(field), field, issues, pattern=_TOKEN)
    for field in ("intent_envelope_sha256", "plan_sha256"):
        _sha(record.get(field), field, issues)
    _subject(record.get("subject"), "subject", issues)
    status = _string(record.get("status"), "status", issues)
    if status not in _STATUSES:
        issues.append("status: unsupported result status")
    candidate = _closed(
        record.get("candidate"),
        frozenset({"base_oid", "head_oid", "tree_oid", "patch_sha256", "clean"}),
        "candidate",
        issues,
    )
    if candidate is not None:
        for field in ("base_oid", "head_oid", "tree_oid"):
            _oid(candidate.get(field), f"candidate/{field}", issues)
        _sha(candidate.get("patch_sha256"), "candidate/patch_sha256", issues)
        _boolean(candidate.get("clean"), "candidate/clean", issues)
    coverage = record.get("obligation_coverage")
    if not isinstance(coverage, list) or not coverage:
        issues.append("obligation_coverage: expected non-empty array")
    else:
        seen: set[tuple[Any, Any]] = set()
        for index, ref in enumerate(coverage):
            row = _closed(
                ref,
                frozenset({"source_id", "obligation_id", "status", "evidence_refs"}),
                f"obligation_coverage/{index}",
                issues,
            )
            if row is None:
                continue
            source = _string(
                row.get("source_id"),
                f"obligation_coverage/{index}/source_id",
                issues,
                pattern=_TOKEN,
            )
            obligation = _string(
                row.get("obligation_id"),
                f"obligation_coverage/{index}/obligation_id",
                issues,
                pattern=_TOKEN,
            )
            if (source, obligation) in seen:
                issues.append(f"obligation_coverage: duplicate coverage {(source, obligation)!r}")
            seen.add((source, obligation))
            state = _string(row.get("status"), f"obligation_coverage/{index}/status", issues)
            if state not in _VALIDATION_STATUSES:
                issues.append(f"obligation_coverage/{index}/status: unsupported status")
            _evidence_refs(
                row.get("evidence_refs"), f"obligation_coverage/{index}/evidence_refs", issues
            )
    validation = record.get("validation")
    if not isinstance(validation, list) or not validation:
        issues.append("validation: expected non-empty array")
    else:
        seen: set[Any] = set()
        for index, row in enumerate(validation):
            path = f"validation/{index}"
            item = _closed(row, frozenset({"profile_id", "status", "evidence_refs"}), path, issues)
            if item is None:
                continue
            profile = _string(item.get("profile_id"), f"{path}/profile_id", issues, pattern=_TOKEN)
            if profile in seen:
                issues.append(f"validation: duplicate profile_id {profile!r}")
            seen.add(profile)
            state = _string(item.get("status"), f"{path}/status", issues)
            if state not in _VALIDATION_STATUSES:
                issues.append(f"{path}/status: unsupported status")
            _evidence_refs(item.get("evidence_refs"), f"{path}/evidence_refs", issues)
    invariants = record.get("invariants")
    if not isinstance(invariants, list) or not invariants:
        issues.append("invariants: expected non-empty array")
    else:
        seen: set[Any] = set()
        for index, row in enumerate(invariants):
            path = f"invariants/{index}"
            item = _closed(
                row, frozenset({"invariant_id", "status", "evidence_refs"}), path, issues
            )
            if item is None:
                continue
            invariant_id = _string(
                item.get("invariant_id"), f"{path}/invariant_id", issues, pattern=_TOKEN
            )
            if invariant_id in seen:
                issues.append(f"invariants: duplicate invariant_id {invariant_id!r}")
            seen.add(invariant_id)
            state = _string(item.get("status"), f"{path}/status", issues)
            if state not in _VALIDATION_STATUSES:
                issues.append(f"{path}/status: unsupported status")
            _evidence_refs(item.get("evidence_refs"), f"{path}/evidence_refs", issues)
    reconciliation = _closed(
        record.get("reconciliation"),
        frozenset({"status", "evidence_refs"}),
        "reconciliation",
        issues,
    )
    if reconciliation is not None:
        state = _string(reconciliation.get("status"), "reconciliation/status", issues)
        if state not in {"consistent", "changed", "blocked", "unavailable"}:
            issues.append("reconciliation/status: unsupported status")
        _evidence_refs(reconciliation.get("evidence_refs"), "reconciliation/evidence_refs", issues)
    findings = record.get("findings")
    if not isinstance(findings, list):
        issues.append("findings: expected array")
    else:
        ids: set[Any] = set()
        for index, finding in enumerate(findings):
            path = f"findings/{index}"
            row = _closed(
                finding,
                frozenset({"finding_id", "severity", "state", "summary", "evidence_refs"}),
                path,
                issues,
            )
            if row is None:
                continue
            finding_id = _string(
                row.get("finding_id"), f"{path}/finding_id", issues, pattern=_TOKEN
            )
            if finding_id in ids:
                issues.append(f"findings: duplicate finding_id {finding_id!r}")
            ids.add(finding_id)
            severity = _string(row.get("severity"), f"{path}/severity", issues)
            if severity not in _FINDING_SEVERITIES:
                issues.append(f"{path}/severity: unsupported severity")
            state = _string(row.get("state"), f"{path}/state", issues)
            if state not in _FINDING_STATES:
                issues.append(f"{path}/state: unsupported state")
            _string(row.get("summary"), f"{path}/summary", issues, maximum=16384)
            _evidence_refs(row.get("evidence_refs"), f"{path}/evidence_refs", issues)
    _evidence_refs(record.get("review_refs"), "review_refs", issues)
    _evidence_refs(record.get("integration_refs"), "integration_refs", issues)
    metrics = _closed(
        record.get("administration_metrics"),
        frozenset(
            {
                "operator_commands",
                "prompt_bytes",
                "repeated_context_bytes",
                "provider_usage_tokens",
                "review_launches",
                "repair_episodes",
                "invariant_failures",
                "authority_violations",
            }
        ),
        "administration_metrics",
        issues,
    )
    if metrics is not None:
        for name, metric in metrics.items():
            path = f"administration_metrics/{name}"
            row = _closed(
                metric,
                frozenset({"status", "value", "unit", "evidence_refs", "reason"}),
                path,
                issues,
            )
            if row is None:
                continue
            state = _string(row.get("status"), f"{path}/status", issues)
            if state not in {"measured", "unavailable"}:
                issues.append(f"{path}/status: expected measured or unavailable")
            if state == "unavailable":
                if row.get("value") is not None:
                    issues.append(f"{path}/value: unavailable metrics must use null")
                _string(row.get("reason"), f"{path}/reason", issues, maximum=4096)
            else:
                if type(row.get("value")) is not int or row["value"] < 0:
                    issues.append(f"{path}/value: expected non-negative integer")
                if row.get("reason") is not None:
                    issues.append(f"{path}/reason: measured metrics must use null")
            unit = _string(row.get("unit"), f"{path}/unit", issues)
            if unit not in {"count", "bytes", "tokens", "milliseconds"}:
                issues.append(f"{path}/unit: unsupported metric unit")
            _evidence_refs(row.get("evidence_refs"), f"{path}/evidence_refs", issues)


def _canonical(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    kind = result.get("record_kind")
    if kind == "intent_envelope":
        for field in ("constraints", "non_goals"):
            result[field] = sorted(result[field])
        result["authority_refs"] = sorted(
            (dict(item, scopes=sorted(item["scopes"])) for item in result["authority_refs"]),
            key=lambda item: item["source_id"],
        )
        result["obligations"] = sorted(
            result["obligations"], key=lambda item: (item["source_id"], item["obligation_id"])
        )
        result["bindings"] = sorted(
            [
                dict(item, evidence_refs=sorted(item["evidence_refs"]))
                for item in result["bindings"]
            ],
            key=lambda item: item["binding_id"],
        )
        result["invariants"] = sorted(
            [
                dict(item, evidence_refs=sorted(item["evidence_refs"]))
                for item in result["invariants"]
            ],
            key=lambda item: item["invariant_id"],
        )
        result["approval"]["evidence_refs"] = sorted(result["approval"]["evidence_refs"])
        result["campaign_envelope"] = _canonical_envelope(result["campaign_envelope"])
    elif kind == "capsule_plan":
        revision = result["revision"]
        for field in (
            "triggering_evidence_refs",
            "affected_capsule_ids",
            "superseded_capsule_ids",
            "preserved_capsule_ids",
            "invalidated_assumption_refs",
        ):
            revision[field] = sorted(revision[field])
        revision["obligation_remapping"] = sorted(
            revision["obligation_remapping"],
            key=lambda item: (item["source_id"], item["obligation_id"], item["capsule_id"]),
        )
        result["capsules"] = [
            _canonical_capsule(item)
            for item in sorted(result["capsules"], key=lambda item: item["capsule_id"])
        ]
    elif kind == "execution_packet":
        result["sources"] = sorted(result["sources"], key=lambda item: item["source_id"])
    elif kind == "capsule_journal":
        result["entries"] = sorted(
            [dict(item, evidence_refs=sorted(item["evidence_refs"])) for item in result["entries"]],
            key=lambda item: item["sequence"],
        )
    elif kind == "capsule_result":
        result["obligation_coverage"] = sorted(
            [
                dict(item, evidence_refs=sorted(item["evidence_refs"]))
                for item in result["obligation_coverage"]
            ],
            key=lambda item: (item["source_id"], item["obligation_id"]),
        )
        result["validation"] = sorted(
            [
                dict(item, evidence_refs=sorted(item["evidence_refs"]))
                for item in result["validation"]
            ],
            key=lambda item: item["profile_id"],
        )
        result["invariants"] = sorted(
            [
                dict(item, evidence_refs=sorted(item["evidence_refs"]))
                for item in result["invariants"]
            ],
            key=lambda item: item["invariant_id"],
        )
        result["reconciliation"] = dict(
            result["reconciliation"],
            evidence_refs=sorted(result["reconciliation"]["evidence_refs"]),
        )
        result["findings"] = sorted(
            [
                dict(item, evidence_refs=sorted(item["evidence_refs"]))
                for item in result["findings"]
            ],
            key=lambda item: item["finding_id"],
        )
        result["administration_metrics"] = {
            name: dict(metric, evidence_refs=sorted(metric["evidence_refs"]))
            for name, metric in result["administration_metrics"].items()
        }
        result["review_refs"] = sorted(result["review_refs"])
        result["integration_refs"] = sorted(result["integration_refs"])
    return result


def _canonical_claim(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    for field in ("path_prefixes", "symbols", "subjects", "semantic_resources", "data_directories"):
        result[field] = sorted(result[field])
    result["ports"] = sorted(
        result["ports"], key=lambda item: (item["transport"], item["port"], item["bind_scope"])
    )
    return result


def _canonical_capsule(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    for field in (
        "binding_refs",
        "invariant_refs",
        "depends_on",
        "reconciliation_triggers",
        "hil_policy_refs",
        "provider_capability_needs",
    ):
        result[field] = sorted(result[field])
    result["obligation_coverage"] = sorted(
        result["obligation_coverage"], key=lambda item: (item["source_id"], item["obligation_id"])
    )
    result["mutation_envelope"] = _canonical_claim(result["mutation_envelope"])
    result["expected_surface"] = {
        key: sorted(result["expected_surface"][key]) for key in result["expected_surface"]
    }
    result["validation_profiles"] = sorted(
        [
            dict(
                profile,
                command_refs=sorted(profile["command_refs"]),
                gate_refs=sorted(profile["gate_refs"]),
            )
            for profile in result["validation_profiles"]
        ],
        key=lambda item: item["profile_id"],
    )
    result["completion_boundary"]["required_gate_refs"] = sorted(
        result["completion_boundary"]["required_gate_refs"]
    )
    result["completion_boundary"]["required_receipt_kinds"] = sorted(
        result["completion_boundary"]["required_receipt_kinds"]
    )
    result["completion_boundary"]["required_artifact_refs"] = sorted(
        result["completion_boundary"]["required_artifact_refs"]
    )
    return result


def canonical_capsule_bytes(value: Mapping[str, Any]) -> bytes:
    """Return deterministic v2 bytes after validating the record."""

    validate_capsule_record(value)
    try:
        return (
            json.dumps(
                _canonical(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CapsuleContractError(f"record is not canonical JSON: {exc}") from exc


def _result(value: Mapping[str, Any], id_field: str) -> CapsuleContractResult:
    data = (
        json.dumps(
            _canonical(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    return CapsuleContractResult(
        value["record_kind"], value[id_field], hashlib.sha256(data).hexdigest(), data
    )


def _validate(value: Mapping[str, Any], validator, id_field: str) -> CapsuleContractResult:
    issues: list[str] = []
    _string_policy(value, issues)
    validator(value, issues)
    if issues:
        raise CapsuleContractError(issues)
    return _result(value, id_field)


def validate_intent_envelope(value: Mapping[str, Any]) -> CapsuleContractResult:
    return _validate(value, _validate_intent, "intent_envelope_id")


def validate_capsule_plan(
    value: Mapping[str, Any],
    *,
    intent_envelope: Mapping[str, Any] | None = None,
    previous_plan: Mapping[str, Any] | None = None,
) -> CapsuleContractResult:
    result = _validate(value, _validate_plan, "plan_id")
    if intent_envelope is not None:
        validate_intent_envelope(intent_envelope)
        _check_plan_bindings(value, intent_envelope, previous_plan)
    elif previous_plan is not None:
        issues: list[str] = []
        _check_revision_metadata(value, previous_plan, issues)
        if issues:
            raise CapsuleContractError(issues)
    return result


def _check_plan_bindings(
    plan: Mapping[str, Any], intent: Mapping[str, Any], previous_plan: Mapping[str, Any] | None
) -> None:
    issues: list[str] = []
    intent_result = validate_intent_envelope(intent)
    if plan.get("intent_envelope_sha256") != intent_result.digest:
        issues.append("intent_envelope_sha256: does not match supplied intent envelope")
    envelope = intent["campaign_envelope"]
    envelope_bytes = json.dumps(
        _canonical_envelope(envelope), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    envelope_digest = hashlib.sha256(envelope_bytes).hexdigest()
    if plan.get("campaign_envelope_sha256") != envelope_digest:
        issues.append("campaign_envelope_sha256: does not match supplied campaign envelope")
    obligations = {(item["source_id"], item["obligation_id"]) for item in intent["obligations"]}
    coverage: set[tuple[str, str]] = set()
    bindings = {item["binding_id"]: item for item in intent["bindings"]}
    invariants = {item["invariant_id"]: item for item in intent["invariants"]}
    capsule_ids = {item["capsule_id"] for item in plan["capsules"]}
    campaign_claim = envelope["mutation_envelope"]
    for capsule in plan["capsules"]:
        if capsule["intent_ref"] != intent["intent_envelope_id"]:
            issues.append(f"capsule {capsule['capsule_id']!r}: intent_ref does not match envelope")
        for ref in capsule["obligation_coverage"]:
            key = (ref["source_id"], ref["obligation_id"])
            if key not in obligations:
                issues.append(
                    f"capsule {capsule['capsule_id']!r}: obligation coverage is undeclared"
                )
            if key in coverage:
                issues.append(f"obligation coverage is duplicated: {key!r}")
            coverage.add(key)
        for binding_id in capsule["binding_refs"]:
            if binding_id not in bindings:
                issues.append(f"capsule {capsule['capsule_id']!r}: unknown binding {binding_id!r}")
            elif bindings[binding_id]["state"] != "accepted":
                issues.append(
                    f"capsule {capsule['capsule_id']!r}: binding {binding_id!r} is not accepted"
                )
        for invariant_id in capsule["invariant_refs"]:
            if invariant_id not in invariants:
                issues.append(
                    f"capsule {capsule['capsule_id']!r}: unknown invariant {invariant_id!r}"
                )
        for dependency in capsule["depends_on"]:
            if dependency not in capsule_ids:
                issues.append(
                    f"capsule {capsule['capsule_id']!r}: dependency {dependency!r} is absent"
                )
        _check_claim_subset(
            capsule["mutation_envelope"],
            campaign_claim,
            f"capsule {capsule['capsule_id']!r}",
            issues,
        )
    if plan["obligation_coverage_mode"] == "complete" and coverage != obligations:
        missing = sorted(obligations - coverage)
        extra = sorted(coverage - obligations)
        issues.append(f"obligation coverage is not complete: missing={missing!r} extra={extra!r}")
    _check_revision_metadata(plan, previous_plan, issues)
    if issues:
        raise CapsuleContractError(issues)


def _canonical_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["policy_refs"] = sorted(result["policy_refs"])
    result["mutation_envelope"] = _canonical_claim(result["mutation_envelope"])
    result["approval"]["evidence_refs"] = sorted(result["approval"]["evidence_refs"])
    result["budget"]["unknown_limits"] = sorted(result["budget"]["unknown_limits"])
    result["policy_snapshot"] = _canonical_policy_snapshot(result["policy_snapshot"])
    return result


def _canonical_policy_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["capability_refs"] = sorted(result["capability_refs"])
    result["stop_conditions"] = sorted(result["stop_conditions"])
    result["human_in_loop"]["checkpoints"] = sorted(result["human_in_loop"]["checkpoints"])
    return result


def _check_revision_metadata(
    plan: Mapping[str, Any],
    previous_plan: Mapping[str, Any] | None,
    supplied: list[str] | None = None,
) -> None:
    issues = supplied if supplied is not None else []
    revision = plan["revision"]
    if previous_plan is None:
        if revision["predecessor_sha256"] is not None:
            issues.append("revision/predecessor_sha256: no previous plan supplied")
        if revision["approval_mode"] != "initial":
            issues.append("revision/approval_mode: initial plan must use initial")
        return
    previous_result = validate_capsule_plan(previous_plan)
    if revision["predecessor_sha256"] != previous_result.digest:
        issues.append("revision/predecessor_sha256: does not match previous plan digest")
    previous_capsules = {item["capsule_id"] for item in previous_plan["capsules"]}
    current_capsules = {item["capsule_id"] for item in plan["capsules"]}
    affected = set(revision["affected_capsule_ids"])
    superseded = set(revision["superseded_capsule_ids"])
    preserved = set(revision["preserved_capsule_ids"])
    if not affected <= previous_capsules:
        issues.append("revision/affected_capsule_ids: unknown predecessor capsule")
    if not preserved <= previous_capsules:
        issues.append("revision/preserved_capsule_ids: unknown predecessor capsule")
    if revision["approval_mode"] == "automatic":
        for field in ("intent_envelope_sha256", "campaign_envelope_sha256"):
            if plan.get(field) != previous_plan.get(field):
                issues.append(f"automatic revision cannot change {field}")
    if plan.get("subject") != previous_plan.get("subject"):
        issues.append("revision/subject: successor must retain the exact subject binding")
    previous_number = _revision_number(previous_plan["revision"]["revision_id"])
    current_number = _revision_number(revision["revision_id"])
    if (
        previous_number is not None
        and current_number is not None
        and current_number != previous_number + 1
    ):
        issues.append("revision/revision_id: successor must increment by one")
    for capsule in previous_plan["capsules"]:
        capsule_id = capsule["capsule_id"]
        if capsule_id not in affected and capsule_id not in preserved:
            issues.append(
                f"revision: predecessor capsule {capsule_id!r} is neither affected nor preserved"
            )
        if capsule_id in preserved:
            successor = next(
                (item for item in plan["capsules"] if item["capsule_id"] == capsule_id), None
            )
            if successor is None:
                issues.append(
                    f"revision: preserved capsule {capsule_id!r} is absent from successor"
                )
            elif _canonical_capsule(successor) != _canonical_capsule(capsule):
                issues.append(f"revision: preserved capsule {capsule_id!r} was changed")
    old_affected_coverage = {
        (ref["source_id"], ref["obligation_id"])
        for capsule in previous_plan["capsules"]
        if capsule["capsule_id"] in affected
        for ref in capsule["obligation_coverage"]
    }
    new_coverage = {
        (ref["source_id"], ref["obligation_id"])
        for capsule in plan["capsules"]
        for ref in capsule["obligation_coverage"]
    }
    remapped = {
        (row["source_id"], row["obligation_id"]) for row in revision["obligation_remapping"]
    }
    if old_affected_coverage - new_coverage - remapped:
        issues.append("revision: affected predecessor obligations were silently dropped")
    for row in revision["obligation_remapping"]:
        if row["capsule_id"] not in current_capsules:
            issues.append("revision/obligation_remapping: target capsule is absent")
    if superseded & current_capsules:
        issues.append("revision/superseded_capsule_ids: superseded capsule is present in successor")
    if not preserved <= current_capsules:
        issues.append("revision/preserved_capsule_ids: preserved capsule is absent from successor")


def _revision_number(value: str) -> int | None:
    prefix, _, suffix = value.rpartition(".")
    if prefix != "revision" or not suffix.isdigit():
        return None
    return int(suffix)


def _check_claim_subset(
    candidate: Mapping[str, Any], envelope: Mapping[str, Any], label: str, issues: list[str]
) -> None:
    def path_allowed(path: str) -> bool:
        return any(
            parent == "." or path == parent or path.startswith(parent + "/")
            for parent in envelope["path_prefixes"]
        )

    for field in ("path_prefixes", "data_directories"):
        for value in candidate[field]:
            allowed_field = "path_prefixes" if field == "path_prefixes" else "data_directories"
            if not any(
                parent == "." or value == parent or value.startswith(parent + "/")
                for parent in envelope[allowed_field]
            ):
                issues.append(f"{label}: {field} value {value!r} escapes campaign envelope")
    for field in ("symbols", "subjects", "semantic_resources", "ports"):
        available = envelope[field]
        if field == "ports":
            available = {
                (item["transport"], item["port"], item["bind_scope"]) for item in available
            }
            for item in candidate[field]:
                if (item["transport"], item["port"], item["bind_scope"]) not in available:
                    issues.append(f"{label}: port claim escapes campaign envelope")
        else:
            for item in candidate[field]:
                if item not in available:
                    issues.append(f"{label}: {field} value {item!r} escapes campaign envelope")


def validate_execution_packet(value: Mapping[str, Any]) -> CapsuleContractResult:
    return _validate(value, _validate_packet, "packet_id")


def validate_capsule_journal(value: Mapping[str, Any]) -> CapsuleContractResult:
    return _validate(value, _validate_journal, "journal_id")


def validate_capsule_result(value: Mapping[str, Any]) -> CapsuleContractResult:
    return _validate(value, _validate_result, "result_id")


def validate_capsule_record(value: Mapping[str, Any]) -> CapsuleContractResult:
    validators = {
        "intent_envelope": validate_intent_envelope,
        "capsule_plan": validate_capsule_plan,
        "execution_packet": validate_execution_packet,
        "capsule_journal": validate_capsule_journal,
        "capsule_result": validate_capsule_result,
    }
    kind = value.get("record_kind") if isinstance(value, Mapping) else None
    validator = validators.get(kind)
    if validator is None:
        raise CapsuleContractError("record_kind: unsupported capsule record")
    return validator(value)


def append_capsule_journal(
    previous: Mapping[str, Any], successor: Mapping[str, Any]
) -> CapsuleContractResult:
    """Validate that ``successor`` appends entries without rewriting prior evidence."""

    validate_capsule_journal(previous)
    validate_capsule_journal(successor)
    issues: list[str] = []
    for field in ("intent_envelope_sha256", "plan_sha256", "revision_id", "capsule_id", "subject"):
        if successor[field] != previous[field]:
            issues.append(f"journal {field}: successor changed immutable identity")
    old_entries = previous["entries"]
    new_entries = successor["entries"]
    if len(new_entries) < len(old_entries) or new_entries[: len(old_entries)] != old_entries:
        issues.append("journal entries: successor must preserve the exact prior prefix")
    if len(new_entries) == len(old_entries):
        issues.append("journal entries: successor must append at least one entry")
    if issues:
        raise CapsuleContractError(issues)
    return validate_capsule_journal(successor)


def compile_execution_packet(
    *,
    packet_id: str,
    intent_envelope: Mapping[str, Any],
    capsule_plan: Mapping[str, Any],
    capsule: Mapping[str, Any],
    episode_id: str,
    subject: Mapping[str, Any],
    policy_sha256: str,
    lease_sha256: str,
    sources: Sequence[Mapping[str, Any]],
    state_delta: Mapping[str, Any] | None = None,
    previous_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a deterministic packet from explicit source selections.

    Source bytes and estimates are supplied by the caller.  The compiler never reads a path or
    treats a digest as context, and excluded sources remain visible in the packet manifest.
    """

    validate_intent_envelope(intent_envelope)
    plan_result = validate_capsule_plan(
        capsule_plan, intent_envelope=intent_envelope, previous_plan=previous_plan
    )
    if not isinstance(subject, Mapping):
        raise CapsuleContractError("subject: expected object")
    if dict(subject) != dict(capsule_plan["subject"]):
        raise CapsuleContractError("subject: does not exactly match capsule plan subject")
    if not isinstance(capsule, Mapping):
        raise CapsuleContractError("capsule: expected object")
    capsule_match = next(
        (
            item
            for item in capsule_plan["capsules"]
            if item["capsule_id"] == capsule.get("capsule_id")
        ),
        None,
    )
    if capsule_match is None:
        raise CapsuleContractError(f"capsule {capsule.get('capsule_id')!r} is absent from plan")
    if _canonical_capsule(capsule) != _canonical_capsule(capsule_match):
        raise CapsuleContractError("capsule: supplied record does not exactly match capsule plan")
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes, bytearray)):
        raise CapsuleContractError("sources: expected non-empty array")
    packet_sources: list[dict[str, Any]] = []
    for index, item in enumerate(sources):
        if not isinstance(item, Mapping):
            raise CapsuleContractError(f"sources/{index}: expected object")
        packet_sources.append(copy.deepcopy(dict(item)))
    source_issues: list[str] = []
    _packet_sources(packet_sources, "sources", source_issues)
    if source_issues:
        raise CapsuleContractError(source_issues)
    for index, item in enumerate(packet_sources):
        allowed = {
            "source_id",
            "source_sha256",
            "kind",
            "selection",
            "reason",
            "bytes",
            "estimated_tokens",
            "tier",
            "source_form",
            "estimate",
            "rendered_sha256",
            "rendered_bytes",
            "full_bytes",
            "full_estimated_tokens",
            "truth_state",
            "projection_of",
        }
        if not set(item) <= allowed:
            raise CapsuleContractError(f"sources/{index}: closed source selection required")
        item.setdefault("tier", "p1")
        item.setdefault("source_form", "full")
        item.setdefault("estimate", None)
    packet_sources.sort(key=lambda item: item["source_id"])
    included = [item for item in packet_sources if item["selection"] == "included"]
    excluded = [item for item in packet_sources if item["selection"] == "excluded"]
    packet = {
        "schema_version": "1",
        "record_kind": "execution_packet",
        "canonical_algorithm": CANONICAL_ALGORITHM,
        "packet_id": packet_id,
        "intent_envelope_sha256": validate_intent_envelope(intent_envelope).digest,
        "plan_sha256": plan_result.digest,
        "revision_id": capsule_plan["revision"]["revision_id"],
        "capsule_id": capsule_match["capsule_id"],
        "episode_id": episode_id,
        "subject": copy.deepcopy(dict(subject)),
        "policy_sha256": policy_sha256,
        "lease_sha256": lease_sha256,
        "context_budget": copy.deepcopy(capsule_match["context_budget"]),
        "sources": packet_sources,
        "totals": {
            "included_bytes": sum(item["bytes"] for item in included),
            "included_estimated_tokens": sum(item["estimated_tokens"] for item in included),
            "excluded_bytes": sum(item["bytes"] for item in excluded),
        },
        "state_delta": copy.deepcopy(dict(state_delta or {})),
    }
    validate_execution_packet(packet)
    return _canonical(packet)


def inspect_capsule_plan(
    plan: Mapping[str, Any],
    *,
    intent_envelope: Mapping[str, Any] | None = None,
    previous_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable human-readable projection without granting authority or executing work."""

    result = validate_capsule_plan(
        plan, intent_envelope=intent_envelope, previous_plan=previous_plan
    )
    return {
        "schema_version": "1",
        "record_kind": "capsule_plan_inspection",
        "status": "valid",
        "plan_id": plan["plan_id"],
        "plan_sha256": result.digest,
        "intent_envelope_sha256": plan["intent_envelope_sha256"],
        "revision_id": plan["revision"]["revision_id"],
        "approval_mode": plan["revision"]["approval_mode"],
        "capsules": [
            {
                "capsule_id": capsule["capsule_id"],
                "intent_ref": capsule["intent_ref"],
                "obligations": len(capsule["obligation_coverage"]),
                "depends_on": list(capsule["depends_on"]),
                "binding_refs": list(capsule["binding_refs"]),
                "invariant_refs": list(capsule["invariant_refs"]),
                "mutation_paths": list(capsule["mutation_envelope"]["path_prefixes"]),
                "legacy_work_unit_id": (capsule.get("legacy_work_unit") or {}).get("work_unit_id"),
            }
            for capsule in sorted(plan["capsules"], key=lambda item: item["capsule_id"])
        ],
        "authority": {
            "project_authority": "intent_envelope and authority_refs",
            "operational_hypothesis": "capsule_plan and revision",
            "observations_and_proposals": "never promoted by inspection",
        },
        "limit": "read-only inspection; no claims acquired and no provider launched",
    }


def compile_capsule_plan(
    *,
    plan_id: str,
    intent_envelope: Mapping[str, Any],
    subject: Mapping[str, Any],
    capsules: Sequence[Mapping[str, Any]],
    revision_id: str = "revision.1",
    previous_plan: Mapping[str, Any] | None = None,
    triggering_evidence_refs: Sequence[str] = (),
    affected_capsule_ids: Sequence[str] = (),
    superseded_capsule_ids: Sequence[str] = (),
    preserved_capsule_ids: Sequence[str] = (),
    invalidated_assumption_refs: Sequence[str] = (),
    obligation_remapping: Sequence[Mapping[str, Any]] = (),
    approval_mode: str | None = None,
    obligation_coverage_mode: str = "complete",
) -> dict[str, Any]:
    """Compile one plan from explicit capsule declarations without semantic inference."""

    intent_result = validate_intent_envelope(intent_envelope)
    if approval_mode is None:
        approval_mode = "initial" if previous_plan is None else "automatic"
    plan = {
        "schema_version": "1",
        "record_kind": "capsule_plan",
        "canonical_algorithm": CANONICAL_ALGORITHM,
        "plan_id": plan_id,
        "intent_envelope_sha256": intent_result.digest,
        "campaign_envelope_sha256": _campaign_envelope_digest(intent_envelope["campaign_envelope"]),
        "subject": copy.deepcopy(dict(subject)),
        "revision": {
            "revision_id": revision_id,
            "predecessor_sha256": None,
            "triggering_evidence_refs": list(triggering_evidence_refs),
            "affected_capsule_ids": list(affected_capsule_ids),
            "superseded_capsule_ids": list(superseded_capsule_ids),
            "preserved_capsule_ids": list(preserved_capsule_ids),
            "invalidated_assumption_refs": list(invalidated_assumption_refs),
            "obligation_remapping": [copy.deepcopy(dict(item)) for item in obligation_remapping],
            "approval_mode": approval_mode,
        },
        "obligation_coverage_mode": obligation_coverage_mode,
        "capsules": [copy.deepcopy(dict(capsule)) for capsule in capsules],
    }
    if previous_plan is not None:
        plan["revision"]["predecessor_sha256"] = validate_capsule_plan(previous_plan).digest
    validate_capsule_plan(plan, intent_envelope=intent_envelope, previous_plan=previous_plan)
    return _canonical(plan)


class CapsuleResolver:
    """Resolve a closed set of content-addressed capsule records fail-closed."""

    def __init__(self, records: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]]):
        if isinstance(records, Mapping):
            candidates = [(key, value) for key, value in records.items()]
        else:
            candidates = [(None, value) for value in records]
        self._records: dict[str, Mapping[str, Any]] = {}
        self._ids: dict[tuple[str, str], set[str]] = {}
        issues: list[str] = []
        for supplied_digest, record in candidates:
            try:
                result = validate_capsule_record(record)
            except CapsuleContractError as error:
                issues.extend(f"record: {entry}" for entry in error.issues)
                continue
            if supplied_digest is not None and supplied_digest != result.digest:
                issues.append(
                    f"record {result.record_id!r}: supplied digest does not match content"
                )
            previous = self._records.get(result.digest)
            if previous is not None and _canonical(previous) != _canonical(record):
                issues.append(f"digest {result.digest}: duplicate digest has different content")
            self._records[result.digest] = copy.deepcopy(dict(record))
            identity = (result.record_kind, result.record_id)
            self._ids.setdefault(identity, set()).add(result.digest)
        if issues:
            raise CapsuleContractError(issues)

    def resolve(self, digest: str, *, record_kind: str | None = None) -> Mapping[str, Any]:
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise CapsuleContractError(f"resolver digest {digest!r} is invalid")
        record = self._records.get(digest)
        if record is None:
            raise CapsuleContractError(f"resolver digest {digest!r} is missing")
        result = validate_capsule_record(record)
        if result.digest != digest:
            raise CapsuleContractError(f"resolver digest {digest!r} is stale")
        if record_kind is not None and result.record_kind != record_kind:
            raise CapsuleContractError(
                f"resolver digest {digest!r} has kind {result.record_kind!r}, "
                f"expected {record_kind!r}"
            )
        return copy.deepcopy(record)

    def resolve_identity(self, record_kind: str, record_id: str) -> Mapping[str, Any]:
        digests = self._ids.get((record_kind, record_id), set())
        if not digests:
            raise CapsuleContractError(f"resolver identity {(record_kind, record_id)!r} is missing")
        if len(digests) > 1:
            raise CapsuleContractError(
                f"resolver identity {(record_kind, record_id)!r} is ambiguous"
            )
        digest = next(iter(digests))
        return self.resolve(digest, record_kind=record_kind)

    def resolve_plan(
        self, digest: str
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any] | None]:
        return self._resolve_plan(digest, set())

    def _resolve_plan(
        self, digest: str, active: set[str]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any] | None]:
        if digest in active:
            raise CapsuleContractError(f"resolver plan ancestry cycle at {digest!r}")
        active.add(digest)
        plan = self.resolve(digest, record_kind="capsule_plan")
        intent = self.resolve(plan["intent_envelope_sha256"], record_kind="intent_envelope")
        predecessor_digest = plan["revision"]["predecessor_sha256"]
        predecessor = None
        if predecessor_digest is not None:
            predecessor, _, _ = self._resolve_plan(predecessor_digest, active)
        validate_capsule_plan(plan, intent_envelope=intent, previous_plan=predecessor)
        active.remove(digest)
        return plan, intent, predecessor


def _campaign_envelope_digest(envelope: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            _canonical_envelope(envelope), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def adapt_v1_campaign(typeset: CampaignTypeset) -> V1AdapterResult:
    """Project one validated v1 work unit into one v2 capsule, exactly and independently."""

    validate_campaign_template(typeset.template)
    capsules: list[dict[str, Any]] = []
    intent_obligations: list[dict[str, Any]] = []
    for unit in typeset.template["work_units"]:
        for ref in unit["task_refs"]:
            intent_obligations.append(
                {
                    "source_id": ref["source_id"],
                    "obligation_id": ref["obligation_id"],
                    "statement": ref["task_id"],
                }
            )
    authority_refs = copy.deepcopy(typeset.template["authority_refs"])
    intent = {
        "schema_version": "1",
        "record_kind": "intent_envelope",
        "canonical_algorithm": CANONICAL_ALGORITHM,
        "intent_envelope_id": f"intent.{typeset.template['campaign_id']}",
        "mode": "v1_compatibility",
        "goal": (
            f"Execute campaign {typeset.template['campaign_id']} with its sealed v1 work units."
        ),
        "intent": (
            "Preserve the exact v1 campaign obligations, claims, gates, checkpoints, "
            "provider roles, and evidence."
        ),
        "constraints": [
            "one work unit maps to one capsule",
            "v1 execution semantics remain unchanged",
        ],
        "non_goals": ["automatic semantic inference", "work-unit coalescing"],
        "authority_refs": authority_refs,
        "obligations": sorted(
            intent_obligations, key=lambda item: (item["source_id"], item["obligation_id"])
        ),
        "bindings": [
            {
                "binding_id": "binding.v1-typeset",
                "term": "v1 campaign typeset",
                "meaning": "The explicitly imported campaign template, run, index, and corpus.",
                "state": "accepted",
                "evidence_refs": [
                    typeset.template_validation.digest,
                    typeset.run_validation.digest,
                    typeset.index_validation.digest,
                    typeset.corpus_validation.digest,
                ],
            }
        ],
        "invariants": [
            {
                "invariant_id": "invariant.v1-records",
                "statement": (
                    "Mechanical compatibility projection: existing v1 records and "
                    "provider instruction bytes remain equivalent."
                ),
                "origin": "derived_observation",
                "evidence_refs": [
                    typeset.template_validation.digest,
                    typeset.run_validation.digest,
                ],
            },
            {
                "invariant_id": "invariant.v1-custody",
                "statement": (
                    "Mechanical compatibility projection: claims, leases, provider roles, "
                    "gates, checkpoints, and evidence stay bound to the v1 unit."
                ),
                "origin": "derived_observation",
                "evidence_refs": [
                    typeset.template_validation.digest,
                    typeset.run_validation.digest,
                ],
            },
        ],
        "campaign_envelope": {
            "envelope_id": f"envelope.{typeset.run['run_id']}",
            "mutation_envelope": _union_claims(typeset.template["work_units"]),
            "policy_refs": [typeset.template_validation.digest],
            "policy_snapshot": {
                "policies": copy.deepcopy(typeset.template["policies"]),
                "human_in_loop": copy.deepcopy(typeset.template["human_in_loop"]),
                "stop_conditions": list(typeset.template["stop_conditions"]),
                "capability_refs": list(typeset.template["capability_refs"]),
            },
            "risk": {
                "level": "v1",
                "max_repair_episodes": typeset.template["policies"]["dispatch"]["attempt_limit"]
                - 1,
            },
            "budget": {
                "max_seconds": typeset.template["policies"]["expiry"]["maximum_run_seconds"],
                "max_provider_tokens": None,
                "max_provider_spend_cents": None,
                "unknown_limits": ["max_provider_spend_cents", "max_provider_tokens"],
            },
            "approval": {
                "mode": "derived",
                "evidence_refs": [typeset.template_validation.digest],
                "approved_at": typeset.run["created_at"],
                "approved_by": "adapter",
            },
        },
        "approval": {
            "mode": "derived",
            "evidence_refs": [typeset.template_validation.digest],
            "approved_at": typeset.run["created_at"],
            "approved_by": "adapter",
        },
    }
    intent_result = validate_intent_envelope(intent)
    for unit in typeset.template["work_units"]:
        capsule_id = f"capsule.{unit['work_unit_id']}"
        capsules.append(_adapt_v1_unit(unit, typeset, intent, capsule_id))
    plan = {
        "schema_version": "1",
        "record_kind": "capsule_plan",
        "canonical_algorithm": CANONICAL_ALGORITHM,
        "plan_id": f"plan.{typeset.run['run_id']}",
        "intent_envelope_sha256": intent_result.digest,
        "campaign_envelope_sha256": _campaign_envelope_digest(intent["campaign_envelope"]),
        "subject": {
            "repository_id": typeset.run["repository"]["repository_id"],
            "base_oid": typeset.run["repository"]["base_oid"],
            "base_tree_sha256": typeset.run["repository"]["base_tree_sha256"],
        },
        "revision": {
            "revision_id": "revision.1",
            "predecessor_sha256": None,
            "triggering_evidence_refs": [],
            "affected_capsule_ids": [],
            "superseded_capsule_ids": [],
            "preserved_capsule_ids": [],
            "invalidated_assumption_refs": [],
            "obligation_remapping": [],
            "approval_mode": "initial",
        },
        "obligation_coverage_mode": "complete",
        "capsules": capsules,
    }
    validate_capsule_plan(plan, intent_envelope=intent)
    by_unit = {item["legacy_work_unit"]["work_unit_id"]: item for item in capsules}
    return V1AdapterResult(CapsuleControlPlane(intent, plan), by_unit)


def _union_claims(units: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    claim = {field: [] for field in _CLAIM_FIELDS}
    claim["claim_set_id"] = "claims.campaign-envelope"
    for unit in units:
        source = unit["claim_set"]
        for field in (
            "path_prefixes",
            "symbols",
            "subjects",
            "semantic_resources",
            "ports",
            "data_directories",
        ):
            claim[field].extend(copy.deepcopy(source[field]))
    for field in ("path_prefixes", "symbols", "subjects", "semantic_resources", "data_directories"):
        claim[field] = sorted(set(claim[field]))
    claim["ports"] = sorted(
        {(item["transport"], item["port"], item["bind_scope"]) for item in claim["ports"]}
    )
    claim["ports"] = [{"transport": a, "port": b, "bind_scope": c} for a, b, c in claim["ports"]]
    return claim


def _adapt_v1_unit(
    unit: Mapping[str, Any], typeset: CampaignTypeset, intent: Mapping[str, Any], capsule_id: str
) -> dict[str, Any]:
    claim = copy.deepcopy(unit["claim_set"])
    expected = {
        field: sorted(copy.deepcopy(claim[field]))
        if field != "subjects"
        else sorted(copy.deepcopy(claim[field]))
        for field in (
            "path_prefixes",
            "symbols",
            "subjects",
            "semantic_resources",
            "data_directories",
        )
    }
    return {
        "capsule_id": capsule_id,
        "intent_ref": intent["intent_envelope_id"],
        "obligation_coverage": [
            {"source_id": ref["source_id"], "obligation_id": ref["obligation_id"]}
            for ref in unit["task_refs"]
        ],
        "binding_refs": ["binding.v1-typeset"],
        "invariant_refs": ["invariant.v1-records", "invariant.v1-custody"],
        "depends_on": [f"capsule.{dependency}" for dependency in unit["depends_on"]],
        "mutation_envelope": claim,
        "expected_surface": expected,
        "validation_profiles": [
            {
                "profile_id": f"profile.{unit['work_unit_id']}",
                "command_refs": [command["command_id"] for command in unit["acceptance_commands"]],
                "gate_refs": list(unit["completion_gate_refs"]),
            }
        ],
        "completion_boundary": {
            "required_gate_refs": list(unit["completion_gate_refs"]),
            "required_receipt_kinds": list(unit["exit_evidence"]["required_receipt_kinds"]),
            "required_artifact_refs": list(unit["exit_evidence"]["required_artifact_refs"]),
        },
        "reconciliation_triggers": ["validation_failure", "authority_change", "scope_pressure"],
        "hil_policy_refs": list(unit["hil_checkpoint_refs"]),
        "provider_capability_needs": list(unit["capability_refs"]),
        "context_budget": {
            "mode": "inherited_v1",
            "max_bytes": None,
            "max_tokens": None,
            "reserve_bytes": None,
            "reserve_tokens": None,
        },
        "legacy_work_unit": {
            "work_unit_id": unit["work_unit_id"],
            "work_unit_sha256": validate_work_unit(unit).digest,
            "record": copy.deepcopy(unit),
        },
    }


def adapt_v1_work_unit(
    work_unit: Mapping[str, Any], *, repository_id: str = "project", campaign_id: str = "legacy"
) -> dict[str, Any]:
    """Small record-level convenience adapter for callers without an imported typeset."""

    validate_work_unit(work_unit)
    unit = copy.deepcopy(dict(work_unit))
    return _adapt_v1_unit(
        unit,
        _FakeTypeset(repository_id, campaign_id),
        {"intent_envelope_id": f"intent.{campaign_id}"},
        f"capsule.{unit['work_unit_id']}",
    )


class _FakeTypeset:
    def __init__(self, repository_id: str, campaign_id: str):
        self.run = {
            "repository": {"repository_id": repository_id},
            "run_id": f"legacy-{campaign_id}",
        }


adapt_v1_typeset = adapt_v1_campaign


__all__ = [
    "CANONICAL_ALGORITHM",
    "CapsuleContractError",
    "CapsuleContractResult",
    "CapsuleControlPlane",
    "V1AdapterResult",
    "adapt_v1_campaign",
    "adapt_v1_typeset",
    "adapt_v1_work_unit",
    "append_capsule_journal",
    "canonical_capsule_bytes",
    "compile_execution_packet",
    "inspect_capsule_plan",
    "validate_capsule_journal",
    "validate_capsule_plan",
    "validate_capsule_record",
    "validate_capsule_result",
    "validate_execution_packet",
    "validate_intent_envelope",
]
