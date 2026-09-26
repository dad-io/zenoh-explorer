"""Deterministic, bounded rendering for one execution-capsule episode.

The control-plane compiler in :mod:`bearhug.campaign.capsules` deliberately stores a manifest,
not provider prompt bytes.  This module is the small edge between that manifest and a provider:
all inputs are passed by the caller, source bytes are verified before they are rendered, and the
rendered prompt is returned for the runtime to custody.  It never opens a path, resolves a digest,
or searches for "latest" project state.

The stable P0 charter is generated from the validated sealed records for every episode.  A later
episode receives only the explicitly supplied state delta; no prior prompt, transcript, or log is
ever read or replayed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from bearhug.campaign.capsules import (
    _canonical_capsule,
    canonical_capsule_bytes,
    compile_execution_packet,
    validate_capsule_plan,
    validate_execution_packet,
    validate_intent_envelope,
)

_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIERS = ("p0", "p1", "p2", "p3")
_KINDS = {
    "project_authority",
    "accepted_binding",
    "invariant",
    "observation",
    "proposal",
    "plan",
}
_MAX_RAW_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_RENDERED_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_STATE_ITEMS = 32
_MAX_STATE_ITEM_BYTES = 4096
_MAX_STATE_BYTES = 64 * 1024
_HARD_MAX_PROMPT_BYTES = 4 * 1024 * 1024
_HARD_MAX_PROMPT_TOKENS = 1_000_000
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DELTA_CANDIDATE_FIELDS = {"base_oid", "head_oid", "tree_oid", "patch_sha256", "clean"}
_DELTA_GIT_FIELDS = {
    "base_oid",
    "head_oid",
    "tree_oid",
    "repository_common_dir_sha256",
    "worktree_sha256",
}


class CapsulePacketError(ValueError):
    """A packet cannot be rendered without losing authority or custody."""


@dataclass(frozen=True, slots=True)
class PacketSource:
    """One explicit content-addressed input to the renderer.

    ``content`` is the full source bytes.  ``projection`` may be supplied when the caller has a
    bounded, semantically safe projection of that source.  The renderer never invents a projection
    for a P0 source.  A missing ``source_sha256`` is calculated for ordinary evidence but is still
    checked against the sealed authority record for ``project_authority`` sources.
    """

    source_id: str
    content: bytes
    kind: str = "observation"
    tier: str | None = None
    reason: str = "explicit source supplied by caller"
    source_sha256: str | None = None
    projection: bytes | None = None
    source_form: str = "full"
    estimated_tokens: int | None = None
    estimate_basis: str | None = None
    truth_state: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedSource:
    """Raw and rendered custody returned to the runtime for one packet source."""

    source_id: str
    source_sha256: str
    kind: str
    tier: str
    selection: str
    reason: str
    raw_bytes: bytes
    rendered_bytes: bytes
    source_form: str
    estimated_tokens: int
    estimate_basis: str
    full_estimated_tokens: int
    truth_state: str | None = None
    projection_of: tuple[str, ...] = ()

    @property
    def rendered_sha256(self) -> str:
        return _sha256(self.rendered_bytes)

    @property
    def metadata(self) -> dict[str, Any]:
        """Return the packet-compatible provenance row (without raw secret bytes)."""

        row: dict[str, Any] = {
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "kind": self.kind,
            "selection": self.selection,
            "reason": self.reason,
            # ``bytes`` and ``estimated_tokens`` are rendered-body measurements.  Prompt wrapper
            # bytes are counted separately in packet totals.
            "bytes": len(self.rendered_bytes),
            "estimated_tokens": self.estimated_tokens,
            "tier": self.tier,
            "source_form": self.source_form,
            "rendered_sha256": self.rendered_sha256,
            "rendered_bytes": len(self.rendered_bytes),
            "full_bytes": len(self.raw_bytes),
            "full_estimated_tokens": self.full_estimated_tokens,
            "estimate": {"tokens": self.estimated_tokens, "basis": self.estimate_basis},
        }
        if self.truth_state is not None:
            row["truth_state"] = self.truth_state
        if self.projection_of:
            row["projection_of"] = list(self.projection_of)
        return row


@dataclass(frozen=True, slots=True)
class RenderedExecutionPacket:
    """The provider prompt plus the exact evidence needed to persist it once."""

    prompt_bytes: bytes
    prompt: str
    execution_packet: dict[str, Any]
    source_artifacts: tuple[RenderedSource, ...]
    metrics: dict[str, Any]

    @property
    def packet(self) -> dict[str, Any]:
        """Short alias used by runtime callers."""

        return self.execution_packet

    @property
    def rendered_sha256(self) -> str:
        return _sha256(self.prompt_bytes)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CapsulePacketError(f"value cannot be canonical UTF-8 JSON: {exc}") from exc


def _estimate_tokens(raw: bytes) -> int:
    # This is a transparent local estimate.  It is never presented as provider billed usage.
    return math.ceil(len(raw) / 4)


def _require_token(value: Any, where: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise CapsulePacketError(f"{where} must be a canonical token")
    return value


def _require_sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CapsulePacketError(f"{where} must be a SHA-256 digest")
    return value


def _require_oid(value: Any, where: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise CapsulePacketError(f"{where} must be a Git object id")
    return value


def _decode_utf8(raw: Any, where: str) -> bytes:
    if isinstance(raw, bytearray):
        raw = bytes(raw)
    if not isinstance(raw, bytes):
        raise CapsulePacketError(f"{where} must be exact bytes")
    if len(raw) > _MAX_RAW_SOURCE_BYTES:
        raise CapsulePacketError(f"{where} exceeds the {_MAX_RAW_SOURCE_BYTES}-byte source bound")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapsulePacketError(f"{where} is not valid UTF-8") from exc
    return raw


def _source_from_mapping(
    source: Mapping[str, Any], *, default_id: str | None = None
) -> PacketSource:
    source_id = source.get("source_id", default_id)
    if not isinstance(source_id, str):
        raise CapsulePacketError("source is missing source_id")
    content = source.get("content")
    if content is None:
        raise CapsulePacketError(f"source {source_id!r} is missing content bytes")
    projection = source.get("projection")
    return PacketSource(
        source_id=source_id,
        content=_decode_utf8(content, f"source {source_id!r} content"),
        kind=source.get("kind", "observation"),
        tier=source.get("tier"),
        reason=source.get("reason", "explicit source supplied by caller"),
        source_sha256=source.get("source_sha256"),
        projection=None
        if projection is None
        else _decode_utf8(projection, f"source {source_id!r} projection"),
        source_form=source.get("source_form", "full"),
        estimated_tokens=source.get("estimated_tokens"),
        estimate_basis=source.get("estimate_basis"),
        truth_state=source.get("truth_state"),
    )


def _coerce_sources(
    sources: Sequence[PacketSource | Mapping[str, Any]] | Mapping[str, Any] | None,
) -> tuple[PacketSource, ...]:
    values: list[PacketSource] = []
    if sources is not None:
        if isinstance(sources, Mapping):
            for source_id in sorted(sources):
                value = sources[source_id]
                if isinstance(value, (bytes, bytearray)):
                    values.append(
                        PacketSource(
                            source_id=source_id,
                            content=_decode_utf8(value, f"source {source_id!r} content"),
                        )
                    )
                elif isinstance(value, Mapping):
                    values.append(_source_from_mapping(value, default_id=source_id))
                else:
                    raise CapsulePacketError(f"source {source_id!r} must be bytes or an object")
        elif isinstance(sources, Sequence) and not isinstance(sources, (str, bytes, bytearray)):
            for item in sources:
                if isinstance(item, PacketSource):
                    values.append(item)
                elif isinstance(item, Mapping):
                    values.append(_source_from_mapping(item))
                else:
                    raise CapsulePacketError("sources must contain PacketSource or object values")
        else:
            raise CapsulePacketError("sources must be an explicit sequence or mapping")
    return tuple(values)


def _validate_source_input(
    source: PacketSource,
    *,
    authority_by_id: Mapping[str, Mapping[str, Any]],
    pinned_digests: frozenset[str] = frozenset(),
) -> tuple[bytes, bytes, str, str, int, int, str]:
    source_id = _require_token(source.source_id, "source_id")
    if source.kind not in _KINDS:
        raise CapsulePacketError(f"source {source_id!r} has unsupported kind {source.kind!r}")
    tier = source.tier
    if tier is None:
        tier = {
            "project_authority": "p0",
            "accepted_binding": "p1",
            "invariant": "p1",
            "plan": "p1",
            "observation": "p2",
            "proposal": "p3",
        }[source.kind]
    if tier not in _TIERS:
        raise CapsulePacketError(f"source {source_id!r} has invalid tier {tier!r}")
    if source.kind == "project_authority" and tier != "p0":
        raise CapsulePacketError(f"project authority source {source_id!r} must be P0")
    if not isinstance(source.reason, str) or not source.reason or len(source.reason) > 4096:
        raise CapsulePacketError(f"source {source_id!r} reason must be bounded text")
    if source.truth_state is not None and source.truth_state not in {
        "accepted",
        "observed",
        "proposed",
    }:
        raise CapsulePacketError(f"source {source_id!r} has invalid truth state")
    raw = _decode_utf8(source.content, f"source {source_id!r} content")
    form = source.source_form
    if form not in {"full", "projection"}:
        raise CapsulePacketError(f"source {source_id!r} source_form must be full or projection")
    if tier == "p0" and form == "projection":
        raise CapsulePacketError(
            f"P0 source {source_id!r} must retain its full sealed source bytes"
        )
    declared = source.source_sha256 or _sha256(raw)
    _require_sha(declared, f"source {source_id!r} source_sha256")
    if _sha256(raw) != declared:
        raise CapsulePacketError(f"source {source_id!r} content digest mismatch")
    projection = raw if source.projection is None else _decode_utf8(
        source.projection, f"source {source_id!r} projection"
    )
    if len(projection) > _MAX_RENDERED_SOURCE_BYTES:
        raise CapsulePacketError(f"source {source_id!r} projection exceeds the rendered bound")
    if form == "full" and source.projection is not None:
        raise CapsulePacketError(
            f"source {source_id!r} supplies a projection but selects full form"
        )
    rendered = raw if form == "full" else projection
    if form == "projection" and source.projection is None:
        raise CapsulePacketError(f"source {source_id!r} projection form requires projection bytes")
    if source.kind == "project_authority":
        authority = authority_by_id.get(source_id)
        if authority is None:
            raise CapsulePacketError(
                f"source {source_id!r} is labelled project_authority but is not sealed authority"
            )
        if declared != authority["content_sha256"]:
            raise CapsulePacketError(f"source {source_id!r} does not match sealed authority digest")
    elif source.kind in {"accepted_binding", "invariant", "plan"}:
        if declared not in pinned_digests:
            raise CapsulePacketError(
                f"source {source_id!r} uses an authority label without a pinned evidence digest"
            )
    if source.kind == "plan" and source.truth_state not in {None, "proposed"}:
        raise CapsulePacketError(f"plan source {source_id!r} must remain proposed")
    if source.truth_state == "accepted" and declared not in pinned_digests:
        raise CapsulePacketError(
            f"source {source_id!r} cannot claim accepted truth without pinned evidence"
        )
    if source.kind == "proposal" and source.truth_state not in {None, "proposed"}:
        raise CapsulePacketError(f"proposal source {source_id!r} must remain proposed")
    estimate = _estimate_tokens(rendered)
    if source.estimated_tokens is not None:
        if type(source.estimated_tokens) is not int or source.estimated_tokens < 0:
            raise CapsulePacketError(f"source {source_id!r} estimated_tokens must be non-negative")
        estimate = source.estimated_tokens
        basis = source.estimate_basis or "caller-supplied estimate"
    else:
        basis = source.estimate_basis or "utf8-bytes/4-ceiling"
    if not isinstance(basis, str) or not basis or len(basis) > 256:
        raise CapsulePacketError(f"source {source_id!r} estimate_basis must be bounded text")
    return raw, rendered, tier, form, estimate, _estimate_tokens(raw), basis


def _synthetic_source(
    *,
    source_id: str,
    kind: str,
    tier: str,
    raw: bytes,
    rendered: bytes,
    reason: str,
    estimate_basis: str = "utf8-bytes/4-ceiling",
    truth_state: str | None = None,
    projection_of: tuple[str, ...] = (),
) -> RenderedSource:
    return RenderedSource(
        source_id=source_id,
        source_sha256=_sha256(raw),
        kind=kind,
        tier=tier,
        selection="included",
        reason=reason,
        raw_bytes=raw,
        rendered_bytes=rendered,
        source_form="full" if raw == rendered else "projection",
        estimated_tokens=_estimate_tokens(rendered),
        estimate_basis=estimate_basis,
        full_estimated_tokens=_estimate_tokens(raw),
        truth_state=truth_state,
        projection_of=projection_of,
    )


def _charter(
    *,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    capsule: Mapping[str, Any],
    intent_bytes: bytes,
) -> RenderedSource:
    envelope = intent["campaign_envelope"]
    relevant_obligations = sorted(
        (dict(item) for item in capsule["obligation_coverage"]),
        key=lambda item: (item["source_id"], item["obligation_id"]),
    )
    obligation_by_id = {
        item["obligation_id"]: item for item in intent["obligations"]
    }
    assigned_obligations: list[dict[str, Any]] = []
    for reference in relevant_obligations:
        obligation = obligation_by_id.get(reference["obligation_id"])
        if obligation is None or obligation["source_id"] != reference["source_id"]:
            raise CapsulePacketError(
                "capsule obligation "
                f"{reference['obligation_id']!r} is absent from the sealed intent"
            )
        assigned_obligations.append(copy.deepcopy(obligation))
    authority = sorted(
        (dict(item, truth_state="accepted") for item in intent["authority_refs"]),
        key=lambda item: item["source_id"],
    )
    success = {
        "completion_boundary": copy.deepcopy(capsule["completion_boundary"]),
        "validation_profiles": copy.deepcopy(capsule["validation_profiles"]),
        "invariant_refs": sorted(capsule["invariant_refs"]),
        "obligation_coverage": relevant_obligations,
    }
    charter_value = {
        "charter_version": "1",
        "capsule_id": capsule["capsule_id"],
        "intent_envelope_id": intent["intent_envelope_id"],
        "goal": intent["goal"],
        "intent": intent["intent"],
        "constraints": sorted(intent["constraints"]),
        "non_goals": sorted(intent["non_goals"]),
        "approved_hard_envelope": copy.deepcopy(envelope),
        "success_criteria": success,
        "assigned_obligations": assigned_obligations,
        "execution_boundary": {
            "instruction": (
                "Execute only the assigned obligations in this capsule. Other plan tasks are "
                "context; boundary changes require an approved plan revision."
            ),
        },
        "stop_conditions": sorted(envelope["policy_snapshot"]["stop_conditions"]),
        "relevant_p0_authority": authority,
        "authority_note": (
            "authority is sealed project input; obligations are traceability evidence"
        ),
        "plan_revision_id": plan["revision"]["revision_id"],
    }
    rendered = _canonical_json(charter_value)
    return _synthetic_source(
        source_id=f"charter.{capsule['capsule_id']}",
        kind="project_authority",
        tier="p0",
        raw=intent_bytes,
        rendered=rendered,
        reason="stable P0 capsule charter regenerated for every episode",
        truth_state="accepted",
        projection_of=(intent["intent_envelope_id"],),
    )


def _semantic_sources(
    *,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    capsule: Mapping[str, Any],
    intent_bytes: bytes,
    plan_bytes: bytes,
) -> tuple[RenderedSource, ...]:
    def truth_for_origin(origin: str) -> str:
        return {
            "project_sealed": "accepted",
            "derived_observation": "observed",
            "proposal": "proposed",
        }[origin]

    result: list[RenderedSource] = []
    binding_by_id = {item["binding_id"]: item for item in intent["bindings"]}
    invariant_by_id = {item["invariant_id"]: item for item in intent["invariants"]}
    for binding_id in sorted(capsule["binding_refs"]):
        item = binding_by_id[binding_id]
        rendered = _canonical_json(
            {
                "binding_id": item["binding_id"],
                "term": item["term"],
                "meaning": item["meaning"],
                "truth_state": item["state"],
                "evidence_refs": sorted(item["evidence_refs"]),
            }
        )
        result.append(
            _synthetic_source(
                source_id=f"binding.{binding_id}",
                kind="accepted_binding",
                tier="p1",
                raw=intent_bytes,
                rendered=rendered,
                reason="relevant sealed binding; truth state is retained explicitly",
                truth_state=item["state"],
                projection_of=(intent["intent_envelope_id"],),
            )
        )
    for invariant_id in sorted(capsule["invariant_refs"]):
        item = invariant_by_id[invariant_id]
        rendered = _canonical_json(
            {
                "invariant_id": item["invariant_id"],
                "statement": item["statement"],
                "origin": item["origin"],
                "truth_state": truth_for_origin(item["origin"]),
                "evidence_refs": sorted(item["evidence_refs"]),
            }
        )
        result.append(
            _synthetic_source(
                source_id=f"invariant.{invariant_id}",
                kind="invariant",
                tier="p1",
                raw=intent_bytes,
                rendered=rendered,
                reason="relevant invariant; origin remains distinct from accepted authority",
                truth_state=truth_for_origin(item["origin"]),
                projection_of=(intent["intent_envelope_id"],),
            )
        )
    rendered_plan = _canonical_json(
        {
            "plan_id": plan["plan_id"],
            "revision_id": plan["revision"]["revision_id"],
            "capsule_id": capsule["capsule_id"],
            "depends_on": sorted(capsule["depends_on"]),
            "expected_surface": copy.deepcopy(capsule["expected_surface"]),
            "mutation_paths": sorted(capsule["mutation_envelope"]["path_prefixes"]),
            "operational_hypothesis_note": "plan is a revisioned hypothesis, not project authority",
        }
    )
    result.append(
        _synthetic_source(
            source_id=f"plan.{capsule['capsule_id']}",
            kind="plan",
            tier="p1",
            raw=plan_bytes,
            rendered=rendered_plan,
            reason="active capsule plan projection; operational hypothesis only",
            truth_state="proposed",
            projection_of=(plan["plan_id"],),
        )
    )
    return tuple(result)


def _validate_state_delta(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CapsulePacketError("state_delta must be an object")
    allowed = {
        "changed_facts",
        "candidate",
        "git",
        "validation_state_changes",
        "discoveries",
        "unresolved_decisions",
        "next_action",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise CapsulePacketError(f"state_delta contains forbidden field(s): {unknown!r}")
    result: dict[str, Any] = {}
    for field_name in (
        "changed_facts",
        "validation_state_changes",
        "discoveries",
        "unresolved_decisions",
    ):
        entries = value.get(field_name, [])
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
            raise CapsulePacketError(f"state_delta/{field_name} must be a string array")
        if len(entries) > _MAX_STATE_ITEMS:
            raise CapsulePacketError(f"state_delta/{field_name} exceeds {_MAX_STATE_ITEMS} items")
        clean: list[str] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, str) or not entry:
                raise CapsulePacketError(f"state_delta/{field_name}/{index} is not bounded text")
            try:
                encoded_entry = entry.encode("utf-8")
            except UnicodeError as exc:
                raise CapsulePacketError(f"state_delta/{field_name}/{index} is not UTF-8") from exc
            if len(encoded_entry) > _MAX_STATE_ITEM_BYTES:
                raise CapsulePacketError(f"state_delta/{field_name}/{index} is not bounded text")
            clean.append(entry)
        result[field_name] = clean
    next_action = value.get("next_action")
    if next_action is not None:
        if not isinstance(next_action, str) or not next_action:
            raise CapsulePacketError("state_delta/next_action is not bounded text")
        try:
            next_action_bytes = next_action.encode("utf-8")
        except UnicodeError as exc:
            raise CapsulePacketError("state_delta/next_action is not UTF-8") from exc
        if len(next_action_bytes) > _MAX_STATE_ITEM_BYTES:
            raise CapsulePacketError("state_delta/next_action is not bounded text")
        result["next_action"] = next_action
    for field_name in ("candidate", "git"):
        item = value.get(field_name)
        if item is None:
            continue
        if not isinstance(item, Mapping):
            raise CapsulePacketError(f"state_delta/{field_name} must be an object")
        # Candidate/Git identities are facts, not free-form transcript containers.
        allowed_fields = (
            _DELTA_CANDIDATE_FIELDS if field_name == "candidate" else _DELTA_GIT_FIELDS
        )
        if any(key not in allowed_fields for key in item):
            raise CapsulePacketError(f"state_delta/{field_name} contains an unsupported field")
        checked: dict[str, Any] = {}
        for key, entry in item.items():
            if key in {"base_oid", "head_oid", "tree_oid"}:
                checked[key] = _require_oid(entry, f"state_delta/{field_name}/{key}")
            elif key in {"patch_sha256", "repository_common_dir_sha256", "worktree_sha256"}:
                checked[key] = _require_sha(entry, f"state_delta/{field_name}/{key}")
            elif key == "clean":
                if type(entry) is not bool:
                    raise CapsulePacketError("state_delta/candidate/clean must be boolean")
                checked[key] = entry
        result[field_name] = checked
    encoded = _canonical_json(result)
    if len(encoded) > _MAX_STATE_BYTES:
        raise CapsulePacketError("state_delta exceeds its bounded byte limit")
    return result


def _source_section(source: RenderedSource) -> bytes:
    header = (
        f"\n--- {source.tier.upper()} SOURCE {source.source_id} ---\n"
        f"kind={source.kind}\n"
        f"truth_state={source.truth_state or 'unspecified'}\n"
        f"selection={source.selection}\n"
        f"source_sha256={source.source_sha256}\n"
        f"rendered_sha256={source.rendered_sha256}\n"
        f"source_form={source.source_form}\n"
        f"reason={source.reason}\n"
    ).encode()
    return header + source.rendered_bytes


def _delta_source(episode_id: str, state_delta: Mapping[str, Any]) -> RenderedSource:
    raw = _canonical_json(state_delta)
    return _synthetic_source(
        source_id=f"state-delta.{episode_id}",
        kind="observation",
        tier="p2",
        raw=raw,
        rendered=raw,
        reason="bounded prior-episode state delta; prior logs are excluded",
        truth_state="observed",
    )


def _metric(value: int | None, unit: str, *, reason: str | None = None) -> dict[str, Any]:
    if value is None:
        return {
            "status": "unavailable",
            "value": None,
            "unit": unit,
            "evidence_refs": [],
            "reason": reason or "provider receipt did not expose this value",
        }
    return {
        "status": "measured",
        "value": value,
        "unit": unit,
        "evidence_refs": [],
        "reason": None,
    }


def render_execution_packet(
    *,
    packet_id: str,
    intent_envelope: Mapping[str, Any],
    capsule_plan: Mapping[str, Any],
    capsule: Mapping[str, Any],
    episode_id: str,
    subject: Mapping[str, Any],
    policy_sha256: str,
    lease_sha256: str,
    sources: Sequence[PacketSource | Mapping[str, Any]] | Mapping[str, Any] | None = None,
    state_delta: Mapping[str, Any] | None = None,
    max_bytes: int | None = None,
    max_tokens: int | None = None,
    previous_plan: Mapping[str, Any] | None = None,
) -> RenderedExecutionPacket:
    """Render one bounded packet from explicit sealed records and source bytes.

    Source selection is deterministic: synthetic P0 charter, synthetic P1 semantics, caller P0..P3
    sources, then an optional P2 state delta, all ordered by ``(tier, source_id)``.  P0 is mandatory
    and is checked before any provider spend.  Lower tiers are excluded with a reason when the
    explicit byte/token budget would be exceeded.  ``policy_sha256`` and ``lease_sha256`` are
    already sealed identities; this function accepts no path or resolver for either value.
    """

    try:
        intent_result = validate_intent_envelope(intent_envelope)
        plan_result = validate_capsule_plan(
            capsule_plan, intent_envelope=intent_envelope, previous_plan=previous_plan
        )
    except Exception as exc:  # contract errors are normalized at this boundary
        raise CapsulePacketError(str(exc)) from exc
    if not isinstance(capsule, Mapping) or not isinstance(subject, Mapping):
        raise CapsulePacketError("capsule and subject must be explicit objects")
    _require_token(packet_id, "packet_id")
    _require_token(episode_id, "episode_id")
    _require_sha(policy_sha256, "policy_sha256")
    _require_sha(lease_sha256, "lease_sha256")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise CapsulePacketError("max_bytes must be a non-negative integer or null")
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 0):
        raise CapsulePacketError("max_tokens must be a non-negative integer or null")
    capsule_id = capsule.get("capsule_id")
    sealed_capsule = next(
        (item for item in capsule_plan["capsules"] if item.get("capsule_id") == capsule_id),
        None,
    )
    if sealed_capsule is None:
        raise CapsulePacketError(f"capsule {capsule_id!r} is absent from the plan")
    if _canonical_capsule(capsule) != _canonical_capsule(sealed_capsule):
        raise CapsulePacketError("capsule does not exactly match the sealed plan")
    if dict(subject) != dict(capsule_plan["subject"]):
        raise CapsulePacketError("subject does not exactly match the sealed plan")
    intent_bytes = canonical_capsule_bytes(intent_envelope)
    plan_bytes = canonical_capsule_bytes(capsule_plan)
    charter = _charter(
        intent=intent_envelope,
        plan=capsule_plan,
        capsule=capsule,
        intent_bytes=intent_bytes,
    )
    semantic = _semantic_sources(
        intent=intent_envelope,
        plan=capsule_plan,
        capsule=capsule,
        intent_bytes=intent_bytes,
        plan_bytes=plan_bytes,
    )
    authority_by_id = {item["source_id"]: item for item in intent_envelope["authority_refs"]}
    pinned_digests = frozenset(
        {
            item["content_sha256"]
            for item in intent_envelope["authority_refs"]
        }
        | {
            evidence
            for item in (*intent_envelope["bindings"], *intent_envelope["invariants"])
            for evidence in item["evidence_refs"]
        }
        | {intent_result.digest, plan_result.digest}
    )
    explicit = _coerce_sources(sources)
    if len({item.source_id for item in explicit}) != len(explicit):
        raise CapsulePacketError("duplicate explicit source_id")
    explicit_rendered: list[RenderedSource] = []
    for item in explicit:
        raw, rendered, tier, form, estimate, full_estimate, estimate_basis = _validate_source_input(
            item, authority_by_id=authority_by_id, pinned_digests=pinned_digests
        )
        explicit_rendered.append(
            RenderedSource(
                source_id=item.source_id,
                source_sha256=item.source_sha256 or _sha256(raw),
                kind=item.kind,
                tier=tier,
                selection="included",
                reason=item.reason,
                raw_bytes=raw,
                rendered_bytes=rendered,
                source_form=form,
                estimated_tokens=estimate,
                estimate_basis=estimate_basis,
                full_estimated_tokens=full_estimate,
                truth_state=item.truth_state,
            )
        )
    supplied_authority = {
        item.source_id for item in explicit_rendered if item.kind == "project_authority"
    }
    missing_authority = sorted(set(authority_by_id) - supplied_authority)
    if missing_authority:
        raise CapsulePacketError(
            f"missing P0 authority source bytes: {missing_authority}"
        )
    state = _validate_state_delta(state_delta)
    delta = None if not state else _delta_source(episode_id, state)
    all_sources = [charter, *semantic, *explicit_rendered]
    if delta is not None:
        all_sources.append(delta)
    if len({item.source_id for item in all_sources}) != len(all_sources):
        raise CapsulePacketError("duplicate source_id after adding renderer projections")
    all_sources.sort(key=lambda item: (_TIERS.index(item.tier), item.source_id))

    limits = sealed_capsule["context_budget"]
    for label, requested in (("max_bytes", max_bytes), ("max_tokens", max_tokens)):
        approved = limits[label]
        if requested is not None and type(approved) is int and requested > approved:
            raise CapsulePacketError(f"{label} cannot exceed the sealed context budget")
    max_bytes = (
        max_bytes
        if max_bytes is not None
        else (
            limits["max_bytes"]
            if type(limits["max_bytes"]) is int
            else _HARD_MAX_PROMPT_BYTES
        )
    )
    max_tokens = (
        max_tokens
        if max_tokens is not None
        else (
            limits["max_tokens"]
            if type(limits["max_tokens"]) is int
            else _HARD_MAX_PROMPT_TOKENS
        )
    )
    reserve_bytes = limits["reserve_bytes"] if type(limits["reserve_bytes"]) is int else 0
    reserve_tokens = limits["reserve_tokens"] if type(limits["reserve_tokens"]) is int else 0
    available_bytes = max_bytes - reserve_bytes
    available_tokens = max_tokens - reserve_tokens
    header = (
        "BEAR HUG EXECUTION PACKET v1\n"
        f"packet_id={packet_id}\n"
        f"episode_id={episode_id}\n"
        f"revision_id={capsule_plan['revision']['revision_id']}\n"
        f"capsule_id={capsule_id}\n"
        "subject="
        f"{json.dumps(dict(subject), ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n"
        f"policy_sha256={policy_sha256}\n"
        f"lease_sha256={lease_sha256}\n"
        "The sealed intent and approved envelope govern this episode. "
        "The capsule plan is an operational hypothesis.\n"
        "Execute only the assigned obligations in this capsule. Other plan tasks are context; "
        "boundary changes require an approved plan revision.\n"
    ).encode()
    charter_section = _source_section(charter)
    prompt_prefix = header + charter_section
    if len(prompt_prefix) > available_bytes or _estimate_tokens(prompt_prefix) > available_tokens:
        raise CapsulePacketError(
            "P0 authority and stable charter exceed context budget; refusing spend"
        )
    p0_sources = [
        item
        for item in all_sources
        if item.tier == "p0" and item.source_id != charter.source_id
    ]
    # The prior-episode state delta is required: compile_packet refuses the episode when it
    # is not included, so it must be reserved before optional sources compete rather than
    # considered after them.  Its tier stays p2 -- it is an observation, not authority --
    # but selection order is about necessity, not rank.  Sorting by tier alone let eight p1
    # grounding sources fill a 64 KB budget and starve a 1.9 KB required delta, which is a
    # priority inversion: the one item that cannot be dropped was scheduled last.
    guaranteed = [*p0_sources] if delta is None else [*p0_sources, delta]
    guaranteed_sections = [_source_section(item) for item in guaranteed]
    if len(prompt_prefix) + sum(len(section) for section in guaranteed_sections) > available_bytes:
        raise CapsulePacketError(
            "P0 authority, stable charter and required state delta exceed context budget; "
            "refusing spend"
        )
    if (
        _estimate_tokens(prompt_prefix)
        + sum(_estimate_tokens(section) for section in guaranteed_sections)
        > available_tokens
    ):
        raise CapsulePacketError(
            "P0 authority, stable charter and required state delta exceed context budget; "
            "refusing spend"
        )
    included: list[RenderedSource] = [charter, *guaranteed]
    excluded: list[RenderedSource] = []
    prompt_parts = [prompt_prefix, *guaranteed_sections]
    current_bytes = len(prompt_prefix) + sum(len(section) for section in guaranteed_sections)
    current_tokens = _estimate_tokens(prompt_prefix) + sum(
        _estimate_tokens(section) for section in guaranteed_sections
    )
    reserved = {charter.source_id, *(item.source_id for item in guaranteed)}
    for item in all_sources:
        if item.source_id in reserved or item.tier == "p0":
            continue
        section = _source_section(item)
        candidate_bytes = current_bytes + len(section)
        candidate_tokens = current_tokens + _estimate_tokens(section)
        if candidate_bytes <= available_bytes and candidate_tokens <= available_tokens:
            included.append(item)
            prompt_parts.append(section)
            current_bytes = candidate_bytes
            current_tokens = candidate_tokens
        else:
            excluded.append(
                RenderedSource(
                    source_id=item.source_id,
                    source_sha256=item.source_sha256,
                    kind=item.kind,
                    tier=item.tier,
                    selection="excluded",
                    reason=(
                        "context budget reserve preserved; source would exceed available bytes"
                        if candidate_bytes > available_bytes
                        else (
                            "context budget reserve preserved; source would exceed "
                            "available token estimate"
                        )
                    ),
                    raw_bytes=item.raw_bytes,
                    rendered_bytes=item.rendered_bytes,
                    source_form=item.source_form,
                    estimated_tokens=item.estimated_tokens,
                    estimate_basis=item.estimate_basis,
                    full_estimated_tokens=item.full_estimated_tokens,
                    truth_state=item.truth_state,
                    projection_of=item.projection_of,
                )
            )
    prompt_bytes = b"".join(prompt_parts)
    if (
        len(prompt_bytes) + reserve_bytes > max_bytes
        or _estimate_tokens(prompt_bytes) + reserve_tokens > max_tokens
    ):
        raise CapsulePacketError(
            "rendered prompt exceeds context budget; refusing spend"
        )
    prompt = prompt_bytes.decode("utf-8")

    rows = [item.metadata for item in (*included, *excluded)]
    # Count the stable authority and semantic context sent on every fresh launch, including
    # section framing. The episode delta and changing packet header are measured in prompt_bytes.
    repeated_context_bytes = sum(
        len(_source_section(item)) for item in included if item.tier in {"p0", "p1"}
    )
    try:
        packet = compile_execution_packet(
            packet_id=packet_id,
            intent_envelope=intent_envelope,
            capsule_plan=capsule_plan,
            capsule=capsule,
            episode_id=episode_id,
            subject=subject,
            policy_sha256=policy_sha256,
            lease_sha256=lease_sha256,
            sources=rows,
            state_delta=state,
            previous_plan=previous_plan,
        )
        validate_execution_packet(packet)
    except Exception as exc:
        raise CapsulePacketError(str(exc)) from exc
    packet["totals"].update(
        {
            "prompt_bytes": len(prompt_bytes),
            "prompt_sha256": _sha256(prompt_bytes),
            "repeated_context_bytes": repeated_context_bytes,
            "context_reserve_bytes": reserve_bytes,
            "context_reserve_tokens": reserve_tokens,
            "estimated_prompt_tokens": _estimate_tokens(prompt_bytes),
        }
    )
    # Provenance and totals are part of the returned durable contract too.
    try:
        validate_execution_packet(packet)
    except Exception as exc:
        raise CapsulePacketError(str(exc)) from exc
    metrics = {
        "prompt_bytes": _metric(len(prompt_bytes), "bytes"),
        "prompt_sha256": _sha256(prompt_bytes),
        "repeated_context_bytes": _metric(repeated_context_bytes, "bytes"),
        "estimated_prompt_tokens": _metric(_estimate_tokens(prompt_bytes), "tokens"),
        "context_reserve_bytes": _metric(reserve_bytes, "bytes"),
        "context_reserve_tokens": _metric(reserve_tokens, "tokens"),
        "provider_usage_tokens": _metric(
            None,
            "tokens",
            reason="provider usage is unavailable until a qualified receipt exposes it",
        ),
    }
    return RenderedExecutionPacket(
        prompt_bytes=prompt_bytes,
        prompt=prompt,
        execution_packet=packet,
        source_artifacts=tuple((*included, *excluded)),
        metrics=metrics,
    )


__all__ = [
    "CapsulePacketError",
    "PacketSource",
    "RenderedExecutionPacket",
    "RenderedSource",
    "render_execution_packet",
]
