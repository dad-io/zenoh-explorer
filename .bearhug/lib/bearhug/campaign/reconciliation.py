"""Evidence-bound reconciliation for one execution capsule.

Reconciliation is deliberately a projection over sealed records and explicit observations.  It
does not read project files, promote a discovery into project truth, or mutate a plan.  Callers
may use the resulting record to choose a local continuation, an affected future-plan revision, or
an operator decision.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from bearhug.campaign.capsules import (
    CANONICAL_ALGORITHM,
    CapsuleContractError,
    _canonical_capsule,
    validate_capsule_plan,
    validate_intent_envelope,
)


class ReconciliationError(ValueError):
    """An evidence comparison or semantic reconciliation record is unsafe."""


_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CORRECTION_CLASSES = {"local_correction", "job_ticket_revision", "press_change"}
_STATUSES = {"consistent", "changed", "blocked", "unavailable"}
_COMPARISON_STATUSES = {"match", "changed", "unavailable", "conflict"}
_CONFIDENCES = {"low", "medium", "high"}
_HIL_TRIGGERS = {
    "concept_drift",
    "invariant_conflict",
    "mutation_envelope_change",
    "architectural_decision",
    "budget_change",
    "risk_change",
    "promotion_checkpoint",
    "meaning_conflict",
    "provider_policy_change",
    "review_policy_change",
}
_SURFACE_FIELDS = (
    "path_prefixes",
    "symbols",
    "subjects",
    "semantic_resources",
    "data_directories",
)
_IDENTITY_FIELDS = {
    "intent_envelope_sha256",
    "plan_sha256",
    "revision_id",
    "capsule_id",
}
_SUBJECT_FIELDS = {"repository_id", "base_oid", "base_tree_sha256"}
_CANDIDATE_FIELDS = {"base_oid", "head_oid", "tree_oid", "patch_sha256", "clean"}
_OBSERVATION_SOURCE = "caller_observation"
_OBSERVATION_META_FIELDS = {"observation_only", "source", "actor", "confidence"}


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReconciliationError(f"value is not canonical JSON: {exc}") from exc


def reconciliation_digest(value: Mapping[str, Any]) -> str:
    """Return the content identity of a validated reconciliation record."""

    validate_reconciliation_record(value)
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require_token(value: Any, where: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ReconciliationError(f"{where} must be a canonical token")
    return value


def _require_sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReconciliationError(f"{where} must be a SHA-256 digest")
    return value


def _require_oid(value: Any, where: str) -> str:
    if not isinstance(value, str) or _GIT_OID.fullmatch(value) is None:
        raise ReconciliationError(f"{where} must be a Git object id")
    return value


def _require_choice(value: Any, choices: set[str], where: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ReconciliationError(f"{where} is unsupported")
    return value


def _refs(value: Iterable[str], where: str) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)):
        raise ReconciliationError(f"{where} must contain SHA-256 evidence references")
    try:
        result = list(value)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError(f"{where} must contain SHA-256 evidence references") from exc
    if any(not isinstance(item, str) or _SHA256.fullmatch(item) is None for item in result):
        raise ReconciliationError(f"{where} must contain SHA-256 evidence references")
    if len(result) != len(set(result)):
        raise ReconciliationError(f"{where} contains duplicate evidence references")
    return sorted(result)


def _safe_text(value: Any, where: str, maximum: int = 16_384) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ReconciliationError(f"{where} must be non-empty bounded text")
    if any(ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F for char in value):
        raise ReconciliationError(f"{where} contains a control character")
    return value


def _string_array(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ReconciliationError(f"{where} must be a string array")
    if len(value) != len(set(value)):
        raise ReconciliationError(f"{where} contains duplicate values")
    return sorted(value)


def _expected_surface(capsule: Mapping[str, Any]) -> dict[str, list[str]]:
    expected = capsule.get("expected_surface")
    if not isinstance(expected, Mapping):
        raise ReconciliationError("capsule expected_surface is missing")
    return {
        field: _string_array(expected.get(field), f"expected_surface/{field}")
        for field in _SURFACE_FIELDS
    }


def _authorized_surface(capsule: Mapping[str, Any]) -> dict[str, list[str]]:
    claim = capsule.get("mutation_envelope")
    if not isinstance(claim, Mapping):
        raise ReconciliationError("capsule mutation_envelope is missing")
    return {
        field: _string_array(claim.get(field), f"mutation_envelope/{field}")
        for field in _SURFACE_FIELDS
    }


def _campaign_authorized_symbols(intent_envelope: Mapping[str, Any]) -> list[str]:
    """The campaign envelope's own symbol-level claim.

    Distinct from `_authorized_surface`, which reads only the capsule's own mutation_envelope: a
    capsule may narrow its symbol claim to empty while the campaign envelope it was validated
    against still names symbols (`_check_claim_subset` in capsules.py only requires the capsule's
    claim to be a subset, so an empty capsule claim under a non-empty campaign claim is a valid
    plan). The unclaimed-symbols upgrade in `_surface_comparisons` must see this too, or it
    upgrades a row past a claim the campaign did make.
    """
    campaign_envelope = intent_envelope.get("campaign_envelope")
    if not isinstance(campaign_envelope, Mapping):
        raise ReconciliationError("intent_envelope campaign_envelope is missing")
    claim = campaign_envelope.get("mutation_envelope")
    if not isinstance(claim, Mapping):
        raise ReconciliationError("campaign_envelope mutation_envelope is missing")
    return _string_array(claim.get("symbols"), "campaign_envelope/mutation_envelope/symbols")


def _comparison(
    expected: Sequence[str],
    observed: Any,
    refs: Sequence[str],
    where: str,
    actor: str,
    confidence: str,
) -> dict[str, Any]:
    expected_values = sorted(expected)
    if observed is None:
        return _mark_observation(
            {
                "status": "unavailable",
                "expected": expected_values,
                "observed": None,
                "evidence_refs": list(refs),
            },
            actor,
            confidence,
        )
    observed_values = _string_array(observed, where)
    return _mark_observation(
        {
            "status": "match" if observed_values == expected_values else "changed",
            "expected": expected_values,
            "observed": observed_values,
            "evidence_refs": list(refs),
        },
        actor,
        confidence,
    )


def _surface_comparisons(
    expected: Mapping[str, Sequence[str]],
    authorized: Mapping[str, Sequence[str]],
    observed: Any,
    refs: Sequence[str],
    actor: str,
    confidence: str,
    *,
    campaign_authorized_symbols: Sequence[str] = (),
) -> dict[str, Any]:
    if observed is not None and not isinstance(observed, Mapping):
        raise ReconciliationError("observed_surface must be an object")
    result: dict[str, Any] = {}
    for field in _SURFACE_FIELDS:
        observed_values = None if observed is None else observed.get(field)
        # Surface observations are compared with both the plan's expected surface and the
        # campaign mutation envelope.  A value outside the envelope is a conflict, even when it
        # happens to be part of the stale plan's expected surface.
        #
        # A symbols value is inside the envelope by membership, as declared, or when it has the
        # form "<path>:<name>" and that path is covered by an authorized path_prefixes entry.  A
        # bare symbol with no path part is judged by membership only.
        #
        # An onboarding-derived envelope authorizes every path but never a symbol, and the plan
        # copies that into its expected surface.  When the plan's expected symbols, the capsule
        # envelope's authorized symbols, AND the campaign envelope's authorized symbols are all
        # empty, no level of authorization that governs the episode makes a symbol-level claim.
        # A fully in-envelope observed set then reads match instead of changed: an honest report
        # cannot contradict a claim nobody made.  The row still records the observed symbol. Once
        # any of the three claims a symbol, an unexpected but path-covered symbol stays changed,
        # and anything outside the envelope stays a conflict, claimed or not.
        comparison = _comparison(
            expected[field],
            observed_values,
            refs,
            f"observed_surface/{field}",
            actor,
            confidence,
        )
        if observed_values is not None:
            values = comparison["observed"]
            assert isinstance(values, list)
            allowed = all(_surface_value_allowed(field, value, authorized) for value in values)
            if not allowed:
                comparison["status"] = "conflict"
            elif (
                field == "symbols"
                and comparison["status"] == "changed"
                and not expected[field]
                and not authorized[field]
                and not campaign_authorized_symbols
            ):
                comparison["status"] = "match"
        comparison["authorized"] = sorted(authorized[field])
        result[field] = comparison
    return result


def _parent_covers(value: str, prefixes: Sequence[str]) -> bool:
    """Return whether some entry in ``prefixes`` is ``"."``, equals ``value``, or is its parent."""

    return any(
        parent == "." or value == parent or value.startswith(parent + "/") for parent in prefixes
    )


def _surface_value_allowed(field: str, value: str, authorized: Mapping[str, Sequence[str]]) -> bool:
    """Apply the claim-set parent semantics used by capsule validation.

    ``symbols`` also allows a ``<path>:<name>`` value whose path is covered by an authorized
    ``path_prefixes`` entry.
    """

    values = authorized[field]
    if field in {"path_prefixes", "data_directories"}:
        return _parent_covers(value, values)
    if value in values:
        return True
    if field == "symbols" and ":" in value:
        return _symbol_path_allowed(value.partition(":")[0], authorized["path_prefixes"])
    return False


def _symbol_path_allowed(path: str, authorized_path_prefixes: Sequence[str]) -> bool:
    """Return whether a symbol claim's path part is covered by an authorized path prefix.

    The path must be a normal repository-relative path: an absolute path, an empty path, a
    ``..`` segment or a backslash is never inside the envelope.  As for ``path_prefixes``, a
    ``./``-prefixed path or authorized prefix, and an empty authorized prefix, never match either:
    both fail closed.
    """

    if not path or path.startswith("/") or "\\" in path or ".." in path.split("/"):
        return False
    return _parent_covers(path, authorized_path_prefixes)


def _validate_subject(value: Any, where: str = "subject") -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _SUBJECT_FIELDS:
        raise ReconciliationError(
            f"{where} must contain exactly repository_id, base_oid, base_tree_sha256"
        )
    return {
        "repository_id": _require_token(value["repository_id"], f"{where}/repository_id"),
        "base_oid": _require_oid(value["base_oid"], f"{where}/base_oid"),
        "base_tree_sha256": _require_sha(value["base_tree_sha256"], f"{where}/base_tree_sha256"),
    }


def _validate_candidate(value: Any, subject: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _CANDIDATE_FIELDS:
        raise ReconciliationError(
            "candidate observation must contain exactly base_oid, head_oid, tree_oid, "
            "patch_sha256, clean"
        )
    _require_oid(value["base_oid"], "candidate/base_oid")
    _require_oid(value["head_oid"], "candidate/head_oid")
    _require_oid(value["tree_oid"], "candidate/tree_oid")
    _require_sha(value["patch_sha256"], "candidate/patch_sha256")
    if type(value["clean"]) is not bool:
        raise ReconciliationError("candidate/clean must be boolean")
    if value["base_oid"] != subject["base_oid"]:
        raise ReconciliationError("candidate/base_oid does not match sealed subject base")


def _validate_triggers(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ReconciliationError(f"{where} are unsupported")
    if len(value) != len(set(value)) or value != sorted(value):
        raise ReconciliationError(f"{where} must be sorted and unique")
    if any(item not in _HIL_TRIGGERS for item in value):
        raise ReconciliationError(f"{where} are unsupported")
    return value


def _observation_metadata(actor: str, confidence: str) -> dict[str, Any]:
    return {
        "observation_only": True,
        "source": _OBSERVATION_SOURCE,
        "actor": actor,
        "confidence": confidence,
    }


def _mark_observation(row: Mapping[str, Any], actor: str, confidence: str) -> dict[str, Any]:
    return {**row, **_observation_metadata(actor, confidence)}


def _validate_observation_metadata(value: Mapping[str, Any], where: str) -> None:
    if set(value) & _OBSERVATION_META_FIELDS != _OBSERVATION_META_FIELDS:
        raise ReconciliationError(f"{where} observation provenance is not closed")
    if value["observation_only"] is not True:
        raise ReconciliationError(f"{where} must remain observation_only")
    if value["source"] != _OBSERVATION_SOURCE:
        raise ReconciliationError(f"{where} observation source is invalid")
    _safe_text(value["actor"], f"{where}/actor", 256)
    _require_choice(value["confidence"], _CONFIDENCES, f"{where}/confidence")


def _discovery_kind(value: Mapping[str, Any]) -> str:
    raw = value.get("kind", value.get("category", "observation"))
    if not isinstance(raw, str) or not raw.strip():
        raise ReconciliationError("discovery kind must be non-empty text")
    return raw.strip().lower().replace("-", "_").replace(" ", "_")


def _default_class(kind: str) -> tuple[str, str | None]:
    if kind in {"bearhug_machinery", "governing_contract", "press_change"}:
        # A press_change is reserved for Bear Hug's own machinery or governing contract.
        return "press_change", "architectural_decision"
    if kind in {
        "local",
        "local_defect",
        "missing_work",
        "validation_failure",
        "repair",
        "implementation_defect",
    }:
        return "local_correction", None
    if kind in {
        "assumption_invalidated",
        "invalidated_assumption",
        "future_surface",
        "scope_pressure",
        "job_ticket",
    }:
        return "job_ticket_revision", None
    if kind == "cross_cutting":
        # A related file or documentation omission inside the current coherent capsule remains
        # local work.  Only an invalidated future surface requires a job-ticket revision.
        return "local_correction", None
    if kind in {
        "meaning_conflict",
        "concept_drift",
        "invariant_conflict",
        "mutation_envelope_change",
        "architectural_decision",
        "scope_change",
        "authority_change",
        "architecture",
        "adr",
        "plan_authority",
        "board_change",
        "budget_change",
        "risk_change",
        "provider_policy_change",
        "review_policy_change",
        "promotion_checkpoint",
    }:
        trigger = {
            "meaning_conflict": "meaning_conflict",
            "concept_drift": "concept_drift",
            "invariant_conflict": "invariant_conflict",
            "mutation_envelope_change": "mutation_envelope_change",
            "architectural_decision": "architectural_decision",
            "architecture": "architectural_decision",
            "adr": "architectural_decision",
            "plan_authority": "architectural_decision",
            "board_change": "architectural_decision",
            "authority_change": "concept_drift",
            "scope_change": "concept_drift",
            "budget_change": "budget_change",
            "risk_change": "risk_change",
            "provider_policy_change": "provider_policy_change",
            "review_policy_change": "review_policy_change",
            "promotion_checkpoint": "promotion_checkpoint",
        }.get(kind)
        return "job_ticket_revision", trigger
    return "local_correction", None


def classify_discovery(
    discovery: Mapping[str, Any] | str,
    *,
    actor: str = "runtime",
    confidence: str = "high",
    evidence_refs: Iterable[str] = (),
    ordinal: int = 1,
) -> dict[str, Any]:
    """Classify one explicit discovery without treating its label as authority."""

    if isinstance(discovery, str):
        value: Mapping[str, Any] = {"summary": discovery}
    elif isinstance(discovery, Mapping):
        value = discovery
    else:
        raise ReconciliationError("discovery must be text or an object")
    kind = _discovery_kind(value)
    correction, trigger = _default_class(kind)
    declared = value.get("correction_class")
    if declared is not None:
        _require_choice(declared, _CORRECTION_CLASSES, "discovery correction_class")
        # The correction class is derived from the discovery kind.  Letting an episode or
        # provider relabel a semantic conflict as local work would bypass the HIL boundary; a
        # subject discovery may not be promoted to Bear Hug machinery work either.
        if declared != correction:
            raise ReconciliationError(
                "discovery correction class cannot be downgraded or overridden"
            )
    declared_triggers = value.get("hil_triggers", [])
    _validate_triggers(declared_triggers, "discovery hil_triggers")
    triggers = sorted(set(declared_triggers) | ({trigger} if trigger is not None else set()))
    refs = _refs(value.get("evidence_refs", evidence_refs), "discovery evidence_refs")
    discovery_confidence = value.get("confidence", confidence)
    _require_choice(discovery_confidence, _CONFIDENCES, "discovery confidence")
    return {
        "discovery_id": _require_token(
            value.get("discovery_id", f"discovery.{ordinal}"), "discovery_id"
        ),
        "kind": kind,
        "summary": _safe_text(value.get("summary", kind), "discovery summary"),
        "correction_class": correction,
        "hil_triggers": sorted(set(triggers)),
        "actor": _safe_text(value.get("actor", actor), "discovery actor", 256),
        "confidence": discovery_confidence,
        "evidence_refs": refs,
    }


def _assessment(value: Mapping[str, Any], *, ordinal: int, refs: Sequence[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReconciliationError("assessment must be an object")
    assessment = {
        "assessment_id": value.get("assessment_id", f"assessment.{ordinal}"),
        "summary": value.get("summary", "semantic assessment supplied by actor"),
        "source": value.get("source"),
        "actor": value.get("actor", "runtime"),
        "confidence": value.get("confidence", "medium"),
        "evidence_refs": value.get("evidence_refs", list(refs)),
    }
    _require_token(assessment["assessment_id"], "assessment_id")
    _safe_text(assessment["summary"], "assessment summary")
    _safe_text(assessment["source"], "assessment source", 256)
    _safe_text(assessment["actor"], "assessment actor", 256)
    _require_choice(assessment["confidence"], _CONFIDENCES, "assessment confidence")
    assessment["evidence_refs"] = _refs(assessment["evidence_refs"], "assessment evidence_refs")
    if not assessment["evidence_refs"]:
        raise ReconciliationError("assessment evidence_refs must not be empty")
    return assessment


def build_reconciliation_record(
    *,
    intent_envelope: Mapping[str, Any],
    capsule_plan: Mapping[str, Any],
    capsule: Mapping[str, Any],
    observations: Mapping[str, Any] | None = None,
    discoveries: Sequence[Mapping[str, Any] | str] = (),
    assessments: Sequence[Mapping[str, Any]] = (),
    actor: str = "runtime",
    confidence: str = "high",
    evidence_refs: Iterable[str] = (),
    revision_id: str | None = None,
    previous_plan: Mapping[str, Any] | None = None,
    dependency_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare sealed intent/bindings/surfaces with explicit episode observations.

    Missing observations stay ``unavailable``.  The function never interprets a missing value as
    a match and never copies a proposed binding into the supplied intent or plan.
    """

    if not isinstance(intent_envelope, Mapping):
        raise ReconciliationError("sealed authority is invalid: intent envelope must be an object")
    if not isinstance(capsule_plan, Mapping):
        raise ReconciliationError("sealed authority is invalid: capsule plan must be an object")
    if not isinstance(capsule, Mapping):
        raise ReconciliationError("capsule does not exactly match the sealed plan")
    if previous_plan is not None and not isinstance(previous_plan, Mapping):
        raise ReconciliationError("sealed authority is invalid: previous plan must be an object")
    if revision_id is not None and not isinstance(revision_id, str):
        raise ReconciliationError("revision_id must match the sealed plan")
    _safe_text(actor, "reconciliation actor", 256)
    _require_choice(confidence, _CONFIDENCES, "reconciliation confidence")
    try:
        intent_result = validate_intent_envelope(dict(intent_envelope))
        plan_result = validate_capsule_plan(
            dict(capsule_plan),
            intent_envelope=dict(intent_envelope),
            previous_plan=None if previous_plan is None else dict(previous_plan),
        )
    except CapsuleContractError as exc:
        raise ReconciliationError(f"sealed authority is invalid: {exc}") from exc
    expected_capsule = next(
        (
            row
            for row in capsule_plan["capsules"]
            if row.get("capsule_id") == capsule.get("capsule_id")
        ),
        None,
    )
    try:
        capsule_matches = expected_capsule is not None and _canonical_capsule(
            expected_capsule
        ) == _canonical_capsule(capsule)
    except (KeyError, TypeError, ValueError) as exc:
        raise ReconciliationError("capsule does not exactly match the sealed plan") from exc
    if not capsule_matches:
        raise ReconciliationError("capsule does not exactly match the sealed plan")
    if observations is None:
        observations = {}
    elif not isinstance(observations, Mapping):
        raise ReconciliationError("observations must be an object")
    else:
        observations = dict(observations)
    for reserved in ("mechanical", "accepted_truth", "project_truth"):
        if reserved in observations:
            raise ReconciliationError(
                f"observations/{reserved} is reserved; callers cannot supply "
                "reconciliation authority"
            )
    refs = _refs(evidence_refs, "reconciliation evidence_refs")
    observed_intent = observations.get("intent", observations)
    if "intent" in observations and not isinstance(observed_intent, Mapping):
        raise ReconciliationError("observations/intent must be an object")
    if not isinstance(observed_intent, Mapping):
        observed_intent = {}
    identity_expected = {
        "intent_envelope_sha256": intent_result.digest,
        "plan_sha256": plan_result.digest,
        "revision_id": capsule_plan["revision"]["revision_id"],
        "capsule_id": capsule["capsule_id"],
    }
    effective_revision_id = capsule_plan["revision"]["revision_id"]
    if revision_id is not None:
        if revision_id != effective_revision_id:
            raise ReconciliationError("revision_id does not match the sealed plan")
        effective_revision_id = revision_id
    identity: dict[str, Any] = {}
    for field, expected in identity_expected.items():
        observed = observed_intent.get(field)
        identity[field] = _mark_observation(
            {
                "status": "unavailable"
                if observed is None
                else ("match" if observed == expected else "conflict"),
                "expected": expected,
                "observed": observed,
                "evidence_refs": list(refs),
            },
            actor,
            confidence,
        )

    expected_subject = _validate_subject(capsule_plan["subject"])
    candidate_subject = expected_subject
    if dependency_base is not None:
        # A dependent capsule's candidate is based on its selected accepted predecessor
        # composition.  The reconciliation subject remains the sealed plan subject so the
        # record continues to describe the original authority boundary.
        if not isinstance(dependency_base, Mapping):
            raise ReconciliationError("dependency_base must be an object")
        dependency_oid = dependency_base.get("base_oid")
        _require_oid(dependency_oid, "dependency_base/base_oid")
        candidate_subject = {**expected_subject, "base_oid": dependency_oid}
    candidate = observations.get("candidate")
    if candidate is not None:
        _validate_candidate(candidate, candidate_subject)

    expected_bindings = [item["binding_id"] for item in intent_envelope["bindings"]]
    observed_bindings = observations.get("bindings")
    if observed_bindings is not None and not isinstance(observed_bindings, Mapping):
        raise ReconciliationError("observations/bindings must be an object")
    if isinstance(observed_bindings, Mapping) and not set(observed_bindings) <= set(
        expected_bindings
    ):
        raise ReconciliationError("observations/bindings contains an unknown binding")
    binding_rows: dict[str, Any] = {}
    for binding_id in expected_bindings:
        row = observed_bindings.get(binding_id) if isinstance(observed_bindings, Mapping) else None
        if row is None:
            binding_rows[binding_id] = _mark_observation(
                {"status": "unavailable", "evidence_refs": list(refs)}, actor, confidence
            )
            continue
        if isinstance(row, Mapping):
            status = row.get("status", "unavailable")
            row_refs = row.get("evidence_refs", refs)
        else:
            status, row_refs = row, refs
        _require_choice(status, _COMPARISON_STATUSES, f"binding {binding_id} status")
        binding_rows[binding_id] = _mark_observation(
            {
                "status": status,
                "evidence_refs": _refs(row_refs, f"binding {binding_id} evidence_refs"),
            },
            actor,
            confidence,
        )

    expected_invariants = list(capsule.get("invariant_refs", ()))
    observed_invariants = observations.get("invariants")
    if observed_invariants is not None and not isinstance(observed_invariants, Mapping):
        raise ReconciliationError("observations/invariants must be an object")
    if isinstance(observed_invariants, Mapping) and not set(observed_invariants) <= set(
        expected_invariants
    ):
        raise ReconciliationError("observations/invariants contains an unknown invariant")
    invariant_rows: dict[str, Any] = {}
    for invariant_id in expected_invariants:
        row = (
            observed_invariants.get(invariant_id)
            if isinstance(observed_invariants, Mapping)
            else None
        )
        if row is None:
            invariant_rows[invariant_id] = _mark_observation(
                {"status": "unavailable", "evidence_refs": list(refs)}, actor, confidence
            )
            continue
        status = row.get("status", "unavailable") if isinstance(row, Mapping) else row
        row_refs = row.get("evidence_refs", refs) if isinstance(row, Mapping) else refs
        _require_choice(status, _COMPARISON_STATUSES, f"invariant {invariant_id} status")
        invariant_rows[invariant_id] = _mark_observation(
            {
                "status": status,
                "evidence_refs": _refs(row_refs, f"invariant {invariant_id} evidence_refs"),
            },
            actor,
            confidence,
        )

    expected_surface = _expected_surface(capsule)
    authorized_surface = _authorized_surface(capsule)
    surfaces = _surface_comparisons(
        expected_surface,
        authorized_surface,
        observations.get("observed_surface"),
        refs,
        actor,
        confidence,
        campaign_authorized_symbols=_campaign_authorized_symbols(intent_envelope),
    )
    coverage_expected = [
        {"source_id": row["source_id"], "obligation_id": row["obligation_id"]}
        for row in capsule["obligation_coverage"]
    ]
    coverage_observed = observations.get("obligation_coverage")
    if coverage_observed is None:
        coverage = _mark_observation(
            {
                "status": "unavailable",
                "expected": coverage_expected,
                "observed": None,
                "evidence_refs": list(refs),
            },
            actor,
            confidence,
        )
    else:
        if not isinstance(coverage_observed, list) or any(
            not isinstance(row, Mapping) for row in coverage_observed
        ):
            raise ReconciliationError("obligation_coverage observation must be an array of objects")
        normalized = sorted(
            [
                {"source_id": row.get("source_id"), "obligation_id": row.get("obligation_id")}
                for row in coverage_observed
            ],
            key=lambda row: (str(row["source_id"]), str(row["obligation_id"])),
        )
        coverage = _mark_observation(
            {
                "status": "match"
                if normalized
                == sorted(
                    coverage_expected, key=lambda row: (row["source_id"], row["obligation_id"])
                )
                else "changed",
                "expected": coverage_expected,
                "observed": normalized,
                "evidence_refs": list(refs),
            },
            actor,
            confidence,
        )

    validations = observations.get("validation")
    validation_rows: list[dict[str, Any]] = []
    if validations is None:
        validation_state = "unavailable"
    elif not isinstance(validations, list):
        raise ReconciliationError("validation observation must be an array")
    else:
        for index, row in enumerate(validations):
            if not isinstance(row, Mapping):
                raise ReconciliationError(f"validation/{index} is malformed")
            _require_choice(
                row.get("status"), {"pass", "fail", "unavailable"}, f"validation/{index} status"
            )
            profile = _require_token(
                row.get("profile_id", f"profile.{index + 1}"), f"validation/{index}/profile_id"
            )
            validation_rows.append(
                _mark_observation(
                    {
                        "profile_id": profile,
                        "status": row["status"],
                        "evidence_refs": _refs(
                            row.get("evidence_refs", refs), f"validation/{index}/evidence_refs"
                        ),
                    },
                    actor,
                    confidence,
                )
            )
        validation_state = (
            "changed"
            if any(row["status"] == "fail" for row in validation_rows)
            else "unavailable"
            if not validation_rows or any(row["status"] == "unavailable" for row in validation_rows)
            else "match"
        )

    normalized_discoveries = [
        classify_discovery(
            item, actor=actor, confidence=confidence, evidence_refs=refs, ordinal=index + 1
        )
        for index, item in enumerate(discoveries)
    ]
    normalized_assessments = [
        _assessment(item, ordinal=index + 1, refs=refs) for index, item in enumerate(assessments)
    ]
    mechanical = {
        "identity": identity,
        "bindings": binding_rows,
        "invariants": invariant_rows,
        "surfaces": surfaces,
        "obligation_coverage": coverage,
        "validation": _mark_observation(
            {
                "status": validation_state,
                "rows": validation_rows,
                "evidence_refs": list(refs),
            },
            actor,
            confidence,
        ),
    }
    statuses = (
        [item["status"] for item in identity.values()]
        + [item["status"] for item in binding_rows.values()]
        + [item["status"] for item in invariant_rows.values()]
    )
    statuses += [item["status"] for item in surfaces.values()] + [
        coverage["status"],
        validation_state,
    ]
    changed = any(status in {"changed", "conflict"} for status in statuses)
    classes = {item["correction_class"] for item in normalized_discoveries}
    correction_class = (
        "press_change"
        if "press_change" in classes
        else "job_ticket_revision"
        if "job_ticket_revision" in classes
        else "local_correction"
    )
    hil_triggers = sorted(
        {trigger for item in normalized_discoveries for trigger in item["hil_triggers"]}
    )
    if any(status == "conflict" for status in statuses):
        hil_triggers.append("concept_drift")
    hil_triggers = sorted(set(hil_triggers))
    # Reconciliation reports the policy class and HIL triggers.  Whether a successor plan is
    # actually eligible for automatic activation is proved by the runtime against the sealed
    # predecessor and campaign envelope; caller-provided ``*_unchanged`` flags are context only.
    automatic_revision_allowed = correction_class == "job_ticket_revision" and not hil_triggers
    if hil_triggers:
        status = "blocked"
    elif changed or normalized_discoveries:
        status = "changed"
    elif any(item == "unavailable" for item in statuses):
        status = "unavailable"
    else:
        status = "consistent"
    record = {
        "schema_version": "1",
        "record_kind": "capsule_reconciliation",
        "canonical_algorithm": CANONICAL_ALGORITHM,
        "reconciliation_id": f"reconciliation.{capsule['capsule_id']}",
        "intent_envelope_sha256": intent_result.digest,
        "plan_sha256": plan_result.digest,
        "revision_id": effective_revision_id,
        "capsule_id": capsule["capsule_id"],
        "subject": copy.deepcopy(capsule_plan["subject"]),
        "status": status,
        "correction_class": correction_class,
        "automatic_revision_allowed": automatic_revision_allowed,
        "hil_triggers": hil_triggers,
        "mechanical": mechanical,
        "assessments": normalized_assessments,
        "discoveries": normalized_discoveries,
        "evidence_refs": sorted({*refs, intent_result.digest, plan_result.digest}),
    }
    validate_reconciliation_record(record)
    return record


def validate_reconciliation_record(value: Mapping[str, Any]) -> None:
    """Validate the closed reconciliation projection used by the runtime."""

    if not isinstance(value, Mapping):
        raise ReconciliationError("reconciliation record must be an object")
    required = {
        "schema_version",
        "record_kind",
        "canonical_algorithm",
        "reconciliation_id",
        "intent_envelope_sha256",
        "plan_sha256",
        "revision_id",
        "capsule_id",
        "subject",
        "status",
        "correction_class",
        "automatic_revision_allowed",
        "hil_triggers",
        "mechanical",
        "assessments",
        "discoveries",
        "evidence_refs",
    }
    if set(value) != required:
        raise ReconciliationError(
            f"reconciliation record fields differ: {sorted(set(value) ^ required)}"
        )
    if (
        value["schema_version"] != "1"
        or value["record_kind"] != "capsule_reconciliation"
        or value["canonical_algorithm"] != CANONICAL_ALGORITHM
    ):
        raise ReconciliationError("reconciliation record identity is invalid")
    for field in ("reconciliation_id", "revision_id", "capsule_id"):
        _require_token(value[field], field)
    for field in ("intent_envelope_sha256", "plan_sha256"):
        _require_sha(value[field], field)
    _validate_subject(value["subject"], "reconciliation subject")
    _require_choice(value["status"], _STATUSES, "reconciliation status")
    _require_choice(
        value["correction_class"], _CORRECTION_CLASSES, "reconciliation correction class"
    )
    if type(value["automatic_revision_allowed"]) is not bool:
        raise ReconciliationError("automatic_revision_allowed must be boolean")
    _validate_triggers(value["hil_triggers"], "hil_triggers")
    _refs(value["evidence_refs"], "reconciliation evidence_refs")
    mechanical = value["mechanical"]
    if not isinstance(mechanical, Mapping) or set(mechanical) != {
        "identity",
        "bindings",
        "invariants",
        "surfaces",
        "obligation_coverage",
        "validation",
    }:
        raise ReconciliationError("mechanical reconciliation sections are closed")
    for section in ("identity", "bindings", "invariants"):
        rows = mechanical[section]
        if not isinstance(rows, Mapping):
            raise ReconciliationError(f"mechanical/{section} must be an object")
        if section == "identity" and set(rows) != _IDENTITY_FIELDS:
            raise ReconciliationError("mechanical/identity fields are closed")
        for key, row in rows.items():
            _require_token(key, f"mechanical/{section} key")
            if not isinstance(row, Mapping):
                raise ReconciliationError(f"mechanical/{section}/{key} is not closed")
            base_fields = {"status", "evidence_refs"}
            if section == "identity":
                base_fields |= {"expected", "observed"}
            if set(row) != base_fields | _OBSERVATION_META_FIELDS:
                raise ReconciliationError(f"mechanical/{section}/{key} is not closed")
            _validate_observation_metadata(row, f"mechanical/{section}/{key}")
            _require_choice(
                row["status"], _COMPARISON_STATUSES, f"mechanical/{section}/{key} status"
            )
            _refs(row["evidence_refs"], f"mechanical/{section}/{key}/evidence_refs")
            if section == "identity":
                expected = row.get("expected")
                observed = row.get("observed")
                if key in {"intent_envelope_sha256", "plan_sha256"}:
                    _require_sha(expected, f"mechanical/identity/{key}/expected")
                    if observed is not None:
                        _require_sha(observed, f"mechanical/identity/{key}/observed")
                else:
                    _require_token(expected, f"mechanical/identity/{key}/expected")
                    if observed is not None:
                        _require_token(observed, f"mechanical/identity/{key}/observed")
    surfaces = mechanical["surfaces"]
    if not isinstance(surfaces, Mapping) or set(surfaces) != set(_SURFACE_FIELDS):
        raise ReconciliationError("mechanical/surfaces fields are closed")
    for field, row in surfaces.items():
        if (
            not isinstance(row, Mapping)
            or set(row)
            != {
                "status",
                "expected",
                "observed",
                "authorized",
                "evidence_refs",
            }
            | _OBSERVATION_META_FIELDS
        ):
            raise ReconciliationError(f"mechanical/surfaces/{field} is not closed")
        _validate_observation_metadata(row, f"mechanical/surfaces/{field}")
        _require_choice(row["status"], _COMPARISON_STATUSES, f"mechanical/surfaces/{field} status")
        _string_array(row["expected"], f"mechanical/surfaces/{field}/expected")
        if row["observed"] is not None:
            _string_array(row["observed"], f"mechanical/surfaces/{field}/observed")
        _string_array(row["authorized"], f"mechanical/surfaces/{field}/authorized")
        _refs(row["evidence_refs"], f"mechanical/surfaces/{field}/evidence_refs")
    coverage = mechanical["obligation_coverage"]
    validation = mechanical["validation"]
    if not isinstance(coverage, Mapping) or not isinstance(validation, Mapping):
        raise ReconciliationError("coverage and validation sections are malformed")
    coverage_fields = {
        "status",
        "expected",
        "observed",
        "evidence_refs",
    } | _OBSERVATION_META_FIELDS
    if set(coverage) != coverage_fields:
        raise ReconciliationError("obligation_coverage fields are closed")
    validation_fields = {"status", "rows", "evidence_refs"} | _OBSERVATION_META_FIELDS
    if set(validation) != validation_fields:
        raise ReconciliationError("validation fields are closed")
    _validate_observation_metadata(coverage, "obligation_coverage")
    _validate_observation_metadata(validation, "validation")
    _require_choice(coverage.get("status"), _COMPARISON_STATUSES, "obligation_coverage status")
    _require_choice(validation.get("status"), _COMPARISON_STATUSES, "validation status")
    for field in ("expected", "observed"):
        if not isinstance(coverage.get(field), list) and coverage.get(field) is not None:
            raise ReconciliationError(f"obligation_coverage/{field} must be an array or null")
    for name, rows in (("expected", coverage["expected"]), ("observed", coverage["observed"])):
        if rows is not None:
            for index, row in enumerate(rows):
                if not isinstance(row, Mapping) or set(row) != {"source_id", "obligation_id"}:
                    raise ReconciliationError(f"obligation_coverage/{name}/{index} is not closed")
                _require_token(row["source_id"], f"obligation_coverage/{name}/{index}/source_id")
                _require_token(
                    row["obligation_id"], f"obligation_coverage/{name}/{index}/obligation_id"
                )
    _refs(coverage.get("evidence_refs", []), "obligation_coverage/evidence_refs")
    if not isinstance(validation.get("rows"), list):
        raise ReconciliationError("validation rows must be an array")
    _refs(validation.get("evidence_refs", []), "validation/evidence_refs")
    for index, row in enumerate(validation["rows"]):
        if (
            not isinstance(row, Mapping)
            or set(row)
            != {
                "profile_id",
                "status",
                "evidence_refs",
            }
            | _OBSERVATION_META_FIELDS
        ):
            raise ReconciliationError(f"validation/{index} is not closed")
        _validate_observation_metadata(row, f"validation/{index}")
        _require_token(row["profile_id"], f"validation/{index}/profile_id")
        _require_choice(
            row["status"], {"pass", "fail", "unavailable"}, f"validation/{index} status"
        )
        _refs(row["evidence_refs"], f"validation/{index}/evidence_refs")
    for collection, name in (
        (value["assessments"], "assessments"),
        (value["discoveries"], "discoveries"),
    ):
        if not isinstance(collection, list):
            raise ReconciliationError(f"{name} must be an array")
        ids: set[str] = set()
        for row in collection:
            if not isinstance(row, Mapping):
                raise ReconciliationError(f"{name} rows must be objects")
            id_field = "assessment_id" if name == "assessments" else "discovery_id"
            _require_token(row.get(id_field), f"{name}/{id_field}")
            if row[id_field] in ids:
                raise ReconciliationError(f"duplicate {name} identity")
            ids.add(row[id_field])
            if name == "assessments":
                if set(row) != {
                    "assessment_id",
                    "summary",
                    "source",
                    "actor",
                    "confidence",
                    "evidence_refs",
                }:
                    raise ReconciliationError(f"{name} row is not closed")
                _safe_text(row["summary"], f"{name}/summary")
                _safe_text(row["source"], f"{name}/source", 256)
                _safe_text(row["actor"], f"{name}/actor", 256)
            else:
                if set(row) != {
                    "discovery_id",
                    "kind",
                    "summary",
                    "correction_class",
                    "hil_triggers",
                    "actor",
                    "confidence",
                    "evidence_refs",
                }:
                    raise ReconciliationError(f"{name} row is not closed")
                _safe_text(row["kind"], f"{name}/kind", 128)
                _safe_text(row["summary"], f"{name}/summary")
                _require_choice(
                    row["correction_class"], _CORRECTION_CLASSES, f"{name} correction class"
                )
                _validate_triggers(row["hil_triggers"], f"{name} HIL triggers")
                _safe_text(row["actor"], f"{name}/actor", 256)
            _require_choice(row.get("confidence"), _CONFIDENCES, f"{name} confidence")
            row_refs = _refs(row.get("evidence_refs", []), f"{name}/evidence_refs")
            if name == "assessments" and not row_refs:
                raise ReconciliationError(f"{name} evidence_refs must not be empty")


__all__ = [
    "ReconciliationError",
    "build_reconciliation_record",
    "classify_discovery",
    "reconciliation_digest",
    "validate_reconciliation_record",
]
