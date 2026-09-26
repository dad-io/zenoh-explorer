"""Read-only conceptual cockpit projection for prepared capsule campaigns.

The v1 campaign cockpit remains the compatibility view for the legacy controller.  This module
projects the already validated v2 authorities and runtime observations into one compact, closed
view.  It deliberately does not reopen provider streams, inspect Git, or grant any authority:
callers must supply records that have already passed their owning validators.

``_resolve_review_verdicts`` is the one exception to "callers
supply everything" -- it reads one already-persisted, content-addressed review-verdict blob per
acceptance bundle (the campaign runtime's own ``capsule_review_receipt`` record, referenced by
``review_record_sha256``), the same way ``capsule_campaign.py``'s own ``_blob``/
``read_capsule_evidence`` already do. It never computes, re-validates, or authorizes a verdict.

A *partial* resolution failure (some, not all, acceptance bundles
readable) is treated exactly like a total one -- the capsule's review never renders a better state
than the stored evidence supports. ``_review`` also prefers the campaign layer's own stored
acceptance decision (``result.status == "accepted"``, written only by the untouched
``CapsuleRuntime._accept_candidate`` after ``verify_capsule_acceptance`` confirms it) over any
arithmetic of its own; only when that decision is absent does it fall back to reading the
per-bundle records themselves, and that fallback never concludes "eligible" on its own.
"""

from __future__ import annotations

import copy
import json
import re
import shlex
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class CapsuleCockpitError(ValueError):
    """The conceptual cockpit inputs are malformed or identify different authorities."""


_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CORRECTION_CLASSES = {"local_correction", "job_ticket_revision", "press_change"}
_UNAVAILABLE = "unavailable"
_PASS_STATUSES = {"pass", "passed", "match", "matched", "complete", "completed", "satisfied"}
_METRIC_UNITS = {
    "operator_commands": "count",
    "episode_count": "count",
    "time_to_first_action_seconds": "seconds",
    "provider_launches": "count",
    "review_launches": "count",
    "repair_episodes": "count",
    "prompt_bytes": "bytes",
    "repeated_context_bytes": "bytes",
    "hil_requests": "count",
    "hil_answers": "count",
    "provider_usage_tokens": "tokens",
}


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapsuleCockpitError(f"{label} must be an object")
    return value


def _text(value: Any, label: str, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise CapsuleCockpitError(f"{label} must be non-empty text")
    return value


def _shell_locator(value: str | None) -> str | None:
    """Quote a prepared locator before placing it in a copyable shell command."""

    return shlex.quote(value) if isinstance(value, str) and value else None


def _token(value: Any, label: str, *, default: str | None = None) -> str | None:
    value = _text(value, label, default=default)
    if value is not None and _TOKEN.fullmatch(value) is None:
        raise CapsuleCockpitError(f"{label} must be a canonical token")
    return value


def _sha(value: Any, label: str, *, default: str | None = None) -> str | None:
    value = _text(value, label, default=default)
    if value is not None and _SHA256.fullmatch(value) is None:
        raise CapsuleCockpitError(f"{label} must be lowercase SHA-256")
    return value


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CapsuleCockpitError(f"cockpit evidence is not canonical JSON: {exc}") from exc


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CapsuleCockpitError(f"{label} must be an array")
    return _copy(value)


def _refs(*values: Any) -> list[str]:
    result: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            if value:
                result.add(value)
            continue
        if isinstance(value, Mapping):
            value = value.get("evidence_refs", value.get("refs", []))
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                if isinstance(item, str) and item:
                    result.add(item)
    return sorted(result)


def _time_text(value: datetime) -> str:
    observed = value.astimezone(UTC).replace(microsecond=0)
    return observed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value:
            return value[key]
    return None


def _source_digest(value: Mapping[str, Any]) -> str | None:
    for key in ("content_sha256", "source_sha256", "plan_sha256", "digest"):
        candidate = value.get(key)
        if isinstance(candidate, str) and _SHA256.fullmatch(candidate):
            return candidate
    return None


def _status(value: Any, *, allowed: set[str], default: str = _UNAVAILABLE) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _metric(value: Any, *, name: str, refs: Sequence[str]) -> dict[str, Any]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {
            "status": "measured",
            "value": value,
            "unit": _METRIC_UNITS.get(name, "count"),
            "evidence_refs": sorted(set(refs)),
            "reason": None,
        }
    if isinstance(value, Mapping):
        status = value.get("status")
        if status == "measured" and "value" in value:
            return {
                "status": "measured",
                "value": _copy(value.get("value")),
                "unit": _text(
                    value.get("unit"),
                    f"metric {name} unit",
                    default=_METRIC_UNITS.get(name, "count"),
                ),
                "evidence_refs": _refs(value.get("evidence_refs"), refs),
                "reason": None,
            }
        reason = _text(value.get("reason"), f"metric {name} reason", default="evidence unavailable")
        return {
            "status": "unavailable",
            "value": None,
            "unit": _text(
                value.get("unit"),
                f"metric {name} unit",
                default=_METRIC_UNITS.get(name, "count"),
            ),
            "evidence_refs": _refs(value.get("evidence_refs"), refs),
            "reason": reason,
        }
    return {
        "status": "unavailable",
        "value": None,
        "unit": _METRIC_UNITS.get(name, "count"),
        "evidence_refs": sorted(set(refs)),
        "reason": "evidence unavailable",
    }


def _scope(value: Any, *, status: str, refs: Sequence[str]) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}

    def strings(*keys: str) -> list[str]:
        values: list[str] = []
        for key in keys:
            item = source.get(key, [])
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                values.extend(entry for entry in item if isinstance(entry, str))
        return sorted(set(values))

    return {
        "status": status,
        "path_prefixes": strings("path_prefixes", "paths", "changed_paths"),
        "symbols": strings("symbols"),
        "subjects": strings("subjects"),
        "semantic_resources": strings("semantic_resources", "resources"),
        "data_directories": strings("data_directories"),
        "evidence_refs": sorted(set(refs)),
    }


def _decision_proposals(state: Mapping[str, Any] | None, refs: Sequence[str]) -> dict[str, Any]:
    """Project the Memex STAGING drafts reconciliation wrote into capsule custody.

    Each draft is `status: proposed`; the operator or the `decide` skill adopts it. Nothing here is
    authority, so the projection names the draft, its digest and its related decisions only.
    """

    rows = state.get("decision_proposals") if isinstance(state, Mapping) else None
    drafts: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            raise CapsuleCockpitError("decision proposal entry must be an object")
        drafts.append(
            {
                "discovery_id": str(row.get("discovery_id") or ""),
                "kind": _text(row.get("kind"), "proposal kind", default="unknown"),
                "summary": _text(
                    row.get("summary"), "proposal summary", default="proposal summary unavailable"
                ),
                "path": _text(row.get("path"), "proposal path", default=None),
                "markdown_sha256": _sha(row.get("markdown_sha256"), "proposal digest"),
                "related_decision_ids": sorted(
                    item for item in row.get("related_decision_ids", []) if isinstance(item, str)
                ),
            }
        )
    drafts.sort(key=lambda row: (row["summary"], row["markdown_sha256"]))
    return {
        "status": "proposed" if drafts else "none",
        "count": len(drafts),
        "drafts": drafts,
        "evidence_refs": _refs([row["markdown_sha256"] for row in drafts], refs) if drafts else [],
    }


def _candidate_scope(
    result: Mapping[str, Any] | None, grounding: Mapping[str, Any] | None
) -> dict[str, Any]:
    candidate = result.get("candidate") if isinstance(result, Mapping) else None
    source = grounding or {}
    changed = _first(source, "observed_surface", "observed", "changed_surface")
    if not isinstance(changed, Mapping):
        changed = {}
    if isinstance(candidate, Mapping):
        changed = dict(changed)
        changed.setdefault("head_oid", candidate.get("head_oid"))
        changed.setdefault("tree_oid", candidate.get("tree_oid"))
        changed.setdefault("patch_sha256", candidate.get("patch_sha256"))
    refs = _refs(source, result)
    return {
        **_scope(changed, status="observed" if candidate is not None else _UNAVAILABLE, refs=refs),
        "head_oid": candidate.get("head_oid") if isinstance(candidate, Mapping) else None,
        "tree_oid": candidate.get("tree_oid") if isinstance(candidate, Mapping) else None,
        "patch_sha256": candidate.get("patch_sha256") if isinstance(candidate, Mapping) else None,
    }


def _binding_rows(
    intent: Mapping[str, Any],
    capsule: Mapping[str, Any],
    reconciliation: Mapping[str, Any] | None,
    refs: Sequence[str],
) -> list[dict[str, Any]]:
    expected = {
        row.get("binding_id"): row
        for row in _list(intent.get("bindings"), "intent bindings")
        if isinstance(row, Mapping) and isinstance(row.get("binding_id"), str)
    }
    observations = (
        reconciliation.get("mechanical", {}).get("bindings", {}) if reconciliation else {}
    )
    result = []
    for binding_id in capsule.get("binding_refs", []):
        source = expected.get(binding_id, {})
        observed = observations.get(binding_id, {}) if isinstance(observations, Mapping) else {}
        status = _status(
            _first(observed, "status") if isinstance(observed, Mapping) else None,
            allowed={"accepted", "match", "changed", "conflict", "uncertain", _UNAVAILABLE},
        )
        if status == _UNAVAILABLE:
            status = _status(
                source.get("state") if isinstance(source, Mapping) else None,
                allowed={"accepted", "uncertain", "changed", "conflict", _UNAVAILABLE},
            )
        result.append(
            {
                "binding_id": binding_id,
                "status": status,
                "summary": _text(
                    _first(source, "meaning", "term") if isinstance(source, Mapping) else None,
                    f"binding {binding_id} summary",
                    default="binding evidence unavailable",
                ),
                "evidence_refs": _refs(
                    observed.get("evidence_refs") if isinstance(observed, Mapping) else None,
                    source.get("evidence_refs") if isinstance(source, Mapping) else None,
                    refs,
                ),
            }
        )
    return result


def _invariant_rows(
    intent: Mapping[str, Any],
    capsule: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    reconciliation: Mapping[str, Any] | None,
    refs: Sequence[str],
) -> list[dict[str, Any]]:
    declared = {
        row.get("invariant_id"): row
        for row in _list(intent.get("invariants"), "intent invariants")
        if isinstance(row, Mapping) and isinstance(row.get("invariant_id"), str)
    }
    rows = _first(result or {}, "invariants") or _first(state or {}, "invariants")
    source_kind = "runtime_result"
    if not isinstance(rows, list):
        rows = _first((reconciliation or {}).get("mechanical", {}), "invariants")
        source_kind = "observation"
    by_id = {
        row.get("invariant_id"): row
        for row in rows or []
        if isinstance(row, Mapping) and isinstance(row.get("invariant_id"), str)
    }
    result_rows = []
    for invariant_id in capsule.get("invariant_refs", []):
        source = declared.get(invariant_id, {})
        observed = by_id.get(invariant_id, {})
        status = _status(
            observed.get("status") if isinstance(observed, Mapping) else None,
            allowed={"pass", "fail", "match", "changed", "conflict", _UNAVAILABLE},
        )
        result_rows.append(
            {
                "invariant_id": invariant_id,
                "status": status,
                "summary": _text(
                    _first(source, "statement", "meaning") if isinstance(source, Mapping) else None,
                    f"invariant {invariant_id} summary",
                    default="invariant evidence unavailable",
                ),
                "source": source_kind if invariant_id in by_id else _UNAVAILABLE,
                "evidence_refs": _refs(
                    observed.get("evidence_refs") if isinstance(observed, Mapping) else None,
                    source.get("evidence_refs") if isinstance(source, Mapping) else None,
                    refs,
                ),
            }
        )
    return result_rows


def _obligation_rows(
    intent: Mapping[str, Any],
    capsule: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    refs: Sequence[str],
) -> dict[str, Any]:
    declared_intent = {
        (row.get("source_id"), row.get("obligation_id")): row
        for row in _list(intent.get("obligations"), "intent obligations")
        if isinstance(row, Mapping)
    }
    declared = []
    for row in capsule.get("obligation_coverage", []):
        if not isinstance(row, Mapping):
            continue
        key = (row.get("source_id"), row.get("obligation_id"))
        authority = declared_intent.get(key, {})
        declared.append(
            {
                "source_id": row.get("source_id"),
                "obligation_id": row.get("obligation_id"),
                "statement": _text(
                    authority.get("statement") if isinstance(authority, Mapping) else None,
                    "obligation statement",
                    default="obligation statement unavailable",
                ),
            }
        )
    observed = _first(result or {}, "obligation_coverage") or _first(
        state or {}, "obligation_coverage"
    )
    by_key = {
        (row.get("source_id"), row.get("obligation_id")): row
        for row in observed or []
        if isinstance(row, Mapping)
    }
    rows = []
    for row in declared:
        observed_row = by_key.get((row["source_id"], row["obligation_id"]), {})
        status = _status(
            observed_row.get("status") if isinstance(observed_row, Mapping) else None,
            allowed={"pass", "fail", "match", "changed", "complete", "satisfied", _UNAVAILABLE},
        )
        rows.append(
            {
                **row,
                "status": status,
                "evidence_refs": _refs(
                    observed_row.get("evidence_refs")
                    if isinstance(observed_row, Mapping)
                    else None,
                    refs,
                ),
            }
        )
    satisfied = [row for row in rows if row["status"] in _PASS_STATUSES]
    remaining = [row for row in rows if row["status"] not in _PASS_STATUSES]
    status = (
        "pass"
        if rows and not remaining
        else "fail"
        if any(row["status"] == "fail" for row in rows)
        else _UNAVAILABLE
    )
    return {
        "status": status,
        "declared": rows,
        "satisfied": satisfied,
        "remaining": remaining,
        "evidence_refs": _refs(*(row["evidence_refs"] for row in rows), refs),
    }


def _capsule_provider(
    state: Mapping[str, Any] | None, result: Mapping[str, Any] | None = None
) -> str:
    """The provider of this capsule's most recent author episode, or "unreported".

    Read from ``provider_receipts`` — the same already-validated provider-session receipts
    ``capsule_runtime.py`` appends after every episode (``validate_provider_receipt`` runs
    before a receipt is ever stored there). A capsule plan names no fixed provider: it is only
    known once an episode has actually run.

    Tries ``result`` before ``state``, the same fallback ``_review`` already uses for
    ``acceptance_bundles``/``findings``/``next_action`` — ``_capsule_projection`` explicitly
    permits ``state`` to be absent, and this must not go blank in that case whenever the data
    survives on ``result`` instead.
    """

    receipts = _first(result or {}, "provider_receipts") or _first(state or {}, "provider_receipts")
    if not isinstance(receipts, list) or not receipts:
        return "unreported"
    latest = receipts[-1]
    provider = latest.get("provider") if isinstance(latest, Mapping) else None
    return provider if isinstance(provider, str) and provider else "unreported"


def _session_eligibility(receipts: Any) -> list[dict[str, Any]]:
    """Per-session promotion eligibility, read from already-validated provider-session receipts.

    Source: the campaign runtime's own ``provider_receipts``/``reviewer_receipts`` state —
    ``validate_provider_receipt`` has already validated every entry before it was persisted
    (``capsule_runtime.py``, ``capsule_review_runtime.py``). This reads those fields; it never
    re-validates or recomputes a verdict of its own.
    """

    rows: list[dict[str, Any]] = []
    if not isinstance(receipts, list):
        return rows
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        session_id = receipt.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        provider = receipt.get("provider")
        eligible = receipt.get("promotion_eligible")
        blockers = receipt.get("promotion_blockers")
        basis = receipt.get("promotion_basis")
        evidence = receipt.get("operational_evidence_sha256")
        rows.append(
            {
                "session_id": session_id,
                "provider": provider if isinstance(provider, str) and provider else "unreported",
                "eligible": eligible if isinstance(eligible, bool) else None,
                "basis": basis if isinstance(basis, str) and basis else None,
                "blockers": (
                    sorted({item for item in blockers if isinstance(item, str) and item})
                    if isinstance(blockers, list)
                    else []
                ),
                "evidence_linked": (
                    evidence[:12] if isinstance(evidence, str) and evidence else "none"
                ),
            }
        )
    return rows


def _resolve_review_verdicts(
    root: Any, acceptance_bundles: Any
) -> tuple[list[dict[str, Any]], int]:
    """Resolve each acceptance bundle's STORED verdict via its review_record_sha256 reference.

    Display-only. ``review_record_sha256`` names a content-
    addressed blob the campaign runtime already wrote (``capsule_review_runtime.py``'s
    ``capsule_review_receipt`` record, verdict + findings + evidence_refs) under
    ``<capsule root>/blobs/<digest>.bin``. This reads that already-persisted, already-validated
    record byte-for-byte and re-checks only its content address (the same mechanical check
    ``_blob`` always does) -- it never computes, re-validates, or authorizes a verdict of its own,
    and it never touches ``is_clean_approval`` or any acceptance authority. A bundle whose blob
    cannot be read (missing root, missing/corrupt blob, malformed JSON) is skipped, never guessed.

    Returns ``(resolved, attempted)`` rather than just ``resolved``,
    so the caller can tell a *partial* resolution failure (some, but not all, bundles resolved)
    from a clean read -- a display that silently drops one bad bundle out of several and computes
    eligibility from the rest is exactly the "shows a better state than the evidence supports"
    failure this ruling exists to close.
    """

    resolved: list[dict[str, Any]] = []
    attempted = 0
    if not isinstance(acceptance_bundles, list):
        return resolved, attempted
    from bearhug.campaign.capsule_campaign import CapsuleCampaignError, _blob

    blobs_root = Path(root) / "blobs" if root is not None else None
    for bundle in acceptance_bundles:
        if not isinstance(bundle, Mapping):
            continue
        digest = bundle.get("review_record_sha256")
        if not isinstance(digest, str):
            continue
        attempted += 1
        if blobs_root is None:
            continue
        try:
            record = json.loads(_blob(blobs_root, digest))
        except (CapsuleCampaignError, OSError, ValueError):
            continue
        if not isinstance(record, Mapping) or not isinstance(record.get("verdict"), str):
            continue
        resolved.append(
            {
                "verdict": record["verdict"],
                "findings": record.get("findings")
                if isinstance(record.get("findings"), list)
                else [],
                "evidence_refs": record.get("evidence_refs")
                if isinstance(record.get("evidence_refs"), list)
                else [],
            }
        )
    return resolved, attempted


def _review(
    prepared: Mapping[str, Any],
    intent: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    refs: Sequence[str],
    *,
    root: Any = None,
) -> dict[str, Any]:
    policy = _first(prepared, "review_policy") or _first(
        intent.get("campaign_envelope", {}), "policy_snapshot"
    )
    if isinstance(policy, Mapping) and "policies" in policy:
        policy = policy["policies"]
    policy = policy if isinstance(policy, Mapping) else {}
    review_policy = policy.get("review", policy)
    quorum = review_policy.get("minimum_approvals", 0) if isinstance(review_policy, Mapping) else 0
    quorum = quorum if isinstance(quorum, int) and quorum >= 0 else 0
    # reviewer_receipts (state or result) are raw, full
    # provider-session receipts -- they never carry a "verdict" key (confirmed against the real
    # recorded state shape). The STORED verdict lives in the review record each acceptance bundle
    # references by digest; resolve it read-only instead of reading a key that was never there.
    acceptance_bundles = (
        _first(result or {}, "acceptance_bundles")
        or _first(state or {}, "acceptance_bundles")
        or []
    )
    resolved, attempted = _resolve_review_verdicts(root, acceptance_bundles)
    approvals = [row for row in resolved if row["verdict"] == "approve"]
    unresolved_count = attempted - len(resolved)
    findings = _first(result or {}, "findings") or _first(state or {}, "review_findings") or []
    required = quorum > 0 or bool(_first(prepared, "review_required"))
    # The GUI never decides approval by its own quorum arithmetic
    # when the campaign layer already carries a stored decision for this capsule. "accepted" is
    # written to the capsule's own result.status only by CapsuleRuntime._accept_candidate, only
    # after the untouched, must-not-change verify_capsule_acceptance gate confirms it -- never by
    # this projection. When that stored decision is absent, this falls back to the per-bundle
    # records, but this rule is explicit: that fallback must never itself claim "eligible" --
    # only a resolved reject (shown as "blocked") or an unresolved bundle (shown as "unavailable")
    # can be concluded from records the GUI read itself; full quorum-satisfying approval per
    # records, with no stored acceptance yet, stays "blocked" (not yet cleared), same as an
    # outright reject -- the per-bundle detail that distinguishes them is still carried in
    # "review_records" below, never discarded.
    stored_accepted = _first(result or {}, "status") == "accepted"
    if not required:
        status = "not_required"
    elif stored_accepted:
        status = "eligible"
    elif not attempted:
        # No review round has completed yet: no evidence to read at all.
        status = _UNAVAILABLE
    elif unresolved_count > 0:
        # ANY unresolved bundle makes the whole capsule "unavailable", whatever the
        # bundles that DID resolve say -- a partial resolution failure is not a clean read.
        status = _UNAVAILABLE
    elif any(row["verdict"] != "approve" for row in resolved):
        # A resolved reject (or incomplete) anywhere is shown as rejected regardless of quorum
        # arithmetic.
        status = "blocked"
    else:
        # Every bundle resolved, every resolved verdict approved -- but no stored acceptance
        # decision exists yet. Never "eligible" on the GUI's own arithmetic.
        status = "blocked"
    return {
        "required": required,
        "status": status,
        "eligible": len(approvals),
        "quorum": quorum,
        "findings": _copy(findings) if isinstance(findings, list) else [],
        "evidence_refs": _refs(refs, *(row["evidence_refs"] for row in resolved)),
        # The per-bundle verdicts, shown beside the status whether or not a stored
        # decision drove it -- never discarded, never collapsed into a single word.
        "review_records": [
            {"verdict": row["verdict"], "findings_count": len(row["findings"])} for row in resolved
        ],
        "resolved_bundles": len(resolved),
        "unresolved_bundles": unresolved_count,
        # Per-session promotion eligibility, read from the campaign
        # layer's own already-validated provider-session receipts — never recomputed here. A
        # capsule blocked because a Claude or Codex author/reviewer turn was not
        # promotion-eligible now names that reason instead of a generic "blocked". Tries
        # ``result`` before ``state``, the same fallback used just above for
        # ``acceptance_bundles``/``findings``/``next_action`` -- this must not go blank next to
        # an eligible verdict just because ``state`` is absent while ``result`` still carries it.
        "author_sessions": _session_eligibility(
            _first(result or {}, "provider_receipts") or _first(state or {}, "provider_receipts")
        ),
        "reviewer_sessions": _session_eligibility(
            _first(result or {}, "reviewer_receipts") or _first(state or {}, "reviewer_receipts")
        ),
    }


def _hil(
    prepared: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    supplied: Mapping[str, Any] | None,
) -> dict[str, Any]:
    state = state or {}
    request = supplied or state.get("hil_request")
    answers = state.get("hil_answers") or []
    locator = _text(prepared.get("locator"), "prepared locator", default=None)
    shell_locator = _shell_locator(locator)
    if isinstance(request, Mapping):
        request_id = _token(
            _first(request, "request_id", "question_id"), "HIL request id", default=None
        )
        checkpoint = _text(request.get("checkpoint"), "HIL checkpoint", default=None)
        command = (
            f"bearhug campaign answer {shell_locator} --question {shlex.quote(request_id)} "
            "--answer-file <answer-file> --disposition {approve,deny,amend}"
            if shell_locator and request_id
            else None
        )
        return {
            "status": "awaiting",
            "request_id": request_id,
            "checkpoint": checkpoint,
            "decision": None,
            "summary": _text(
                request.get("conflict"), "HIL conflict", default="operator decision required"
            ),
            "evidence_refs": _refs(request),
            "remediation": _remediation(
                "actionable" if command else _UNAVAILABLE, "human_answer_required", command
            ),
        }
    if answers and isinstance(answers[-1], Mapping):
        answer = answers[-1]
        return {
            "status": "answered",
            "request_id": _token(answer.get("request_id"), "HIL answer request id", default=None),
            "checkpoint": None,
            "decision": _text(answer.get("decision"), "HIL decision", default=None),
            "summary": "operator decision recorded",
            "evidence_refs": _refs(answer),
            "remediation": _remediation("none", None, None),
        }
    if state.get("state") in {"awaiting_hil", "hil_required"}:
        return {
            "status": _UNAVAILABLE,
            "request_id": None,
            "checkpoint": None,
            "decision": None,
            "summary": "HIL request is unavailable",
            "evidence_refs": [],
            "remediation": _remediation(
                "actionable" if locator else _UNAVAILABLE,
                "hil_request_unavailable",
                f"bearhug campaign status {shell_locator}" if shell_locator else None,
            ),
        }
    return {
        "status": "none",
        "request_id": None,
        "checkpoint": None,
        "decision": None,
        "summary": None,
        "evidence_refs": [],
        "remediation": _remediation("none", None, None),
    }


def _revision(plan: Mapping[str, Any]) -> dict[str, Any]:
    revision = plan.get("revision") if isinstance(plan.get("revision"), Mapping) else {}
    return {
        "plan_id": _token(plan.get("plan_id"), "plan id", default=None),
        "revision_id": _token(revision.get("revision_id"), "revision id", default=None),
        "predecessor_sha256": _sha(
            revision.get("predecessor_sha256"), "revision predecessor", default=None
        ),
        "affected_capsule_ids": sorted(
            item for item in revision.get("affected_capsule_ids", []) if isinstance(item, str)
        ),
        "preserved_capsule_ids": sorted(
            item for item in revision.get("preserved_capsule_ids", []) if isinstance(item, str)
        ),
        "superseded_capsule_ids": sorted(
            item for item in revision.get("superseded_capsule_ids", []) if isinstance(item, str)
        ),
        "approval_mode": _text(
            revision.get("approval_mode"), "revision approval mode", default=None
        ),
    }


def _freshness(
    run: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    now: datetime,
    prepared: Mapping[str, Any],
) -> dict[str, Any]:
    observed = _parse_time(
        _first(run, "observed_at", "updated_at", "last_event_observed_at")
        or _first(state or {}, "observed_at", "updated_at", "last_observed_at")
    )
    if observed is None:
        actions = run.get("operator_actions")
        if isinstance(actions, list) and actions:
            latest = actions[-1]
            if isinstance(latest, Mapping):
                observed = _parse_time(latest.get("at"))
    stale_after = (
        _first(run, "stale_after_seconds") or _first(prepared, "stale_after_seconds") or 300
    )
    if type(stale_after) is not int or stale_after < 1:
        stale_after = 300
    if observed is None:
        return {
            "status": _UNAVAILABLE,
            "basis": "durable run observation timestamp",
            "observed_at": None,
            "age_seconds": None,
            "stale_after_seconds": stale_after,
            "reason": "runtime observation timestamp unavailable",
        }
    age = max(0, int((now.astimezone(UTC) - observed).total_seconds()))
    status = "stale" if age > stale_after else "current"
    return {
        "status": status,
        "basis": "durable run observation timestamp",
        "observed_at": _time_text(observed),
        "age_seconds": age,
        "stale_after_seconds": stale_after,
        "reason": "last durable observation is stale" if status == "stale" else None,
    }


def _remediation(status: str, reason: str | None, command: str | None) -> dict[str, Any]:
    if status == "actionable" and isinstance(reason, str) and isinstance(command, str):
        return {"status": "actionable", "reason": reason, "command": command}
    return {"status": "none", "reason": None, "command": None}


def _recovery_remediation(
    locator: str | None, item: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Build the one recovery command that matches an actual unresolved spend fence."""

    shell_locator = _shell_locator(locator)
    if shell_locator is None or not isinstance(item, Mapping):
        return None
    state = item.get("state")
    if not isinstance(state, Mapping):
        return None
    boundaries = (
        ("active_review", "review_id", "campaign_review_recovery_required"),
        ("active_episode", "episode_id", "campaign_episode_recovery_required"),
    )
    for boundary_name, identity_name, reason in boundaries:
        boundary = state.get(boundary_name)
        if not isinstance(boundary, Mapping):
            continue
        identity = _token(boundary.get(identity_name), f"{boundary_name} identity", default=None)
        if identity is None:
            # A missing identity cannot safely be turned into a recover command.  Leave the
            # control surface unavailable instead of suggesting resume, which could spend again.
            return None
        return _remediation(
            "actionable",
            reason,
            "bearhug campaign recover "
            f"{shell_locator} --{identity_name.replace('_', '-')} {shlex.quote(identity)} "
            "--recovery-outcome blocked",
        )
    return None


def _proof_transition(
    prepared: Mapping[str, Any],
    result: Mapping[str, Any] | None,
    reconciliation: Mapping[str, Any] | None,
    state: Mapping[str, Any] | None,
    refs: Sequence[str],
) -> dict[str, Any]:
    result = result or {}
    reconciliation = reconciliation or {}
    state = state or {}
    candidate = result.get("candidate")
    correction_class = reconciliation.get("correction_class")
    if correction_class not in _CORRECTION_CLASSES:
        correction_class = None
    next_action = _first(result, "next_action") or _first(state, "next_action")
    next_action = next_action if isinstance(next_action, str) and next_action.strip() else None
    automatic = reconciliation.get("automatic_revision_allowed")
    if not isinstance(automatic, bool):
        automatic = None
    proof_refs = _refs(refs, result, reconciliation, state.get("acceptance_proof_sha256"))
    available = isinstance(candidate, Mapping) and bool(proof_refs)
    if automatic is True:
        decision = "automatic"
        reason = "reconciliation permits an in-envelope correction"
    elif automatic is False:
        decision = "awaiting_operator"
        reason = "reconciliation requires an operator decision"
    else:
        decision = _UNAVAILABLE
        reason = "correction authority is unavailable"
    return {
        "status": "observed" if available else _UNAVAILABLE,
        "proof_refs": proof_refs,
        "summary": (
            f"candidate {candidate.get('head_oid', 'head unavailable')} observed"
            if isinstance(candidate, Mapping)
            else "candidate proof unavailable"
        ),
        "correction_class": correction_class or _UNAVAILABLE,
        "next_action": next_action,
        "decision": decision,
        "reason": reason,
    }


def _custody(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"status": _UNAVAILABLE, "refs": [], "locators": [], "identities": {}}
    identities: dict[str, Any] = {}
    refs: set[str] = set()
    locators: list[str] = []
    for key, item in value.items():
        if isinstance(item, str) and (
            "sha256" in key
            or key.endswith("_id")
            or key in {"lease_id", "session_id", "claimant_id", "attempt_id"}
        ):
            identities[key] = item
            if _SHA256.fullmatch(item):
                refs.add(item)
        elif isinstance(item, str) and (
            key.endswith("_path") or key.endswith("_root") or key in {"path", "worktree", "branch"}
        ):
            locators.append(item)
        elif isinstance(item, list):
            for row in item:
                if isinstance(row, Mapping):
                    refs.update(
                        _refs(
                            row.get("evidence_refs"),
                            row.get("receipt_sha256"),
                            row.get("proof_sha256"),
                        )
                    )
                    for nested in ("receipt_sha256", "proof_sha256", "session_id", "lease_id"):
                        if isinstance(row.get(nested), str):
                            identities.setdefault(nested, row[nested])
    return {
        "status": "observed" if identities or locators or refs else _UNAVAILABLE,
        "refs": sorted(refs),
        "locators": sorted(set(locators)),
        "identities": identities,
    }


def _capsule_projection(
    *,
    prepared: Mapping[str, Any],
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    item: Mapping[str, Any],
    revision: Mapping[str, Any],
    run_metrics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    capsule = _mapping(item.get("capsule"), "capsule entry.capsule")
    state = item.get("state")
    state = _mapping(state, "capsule entry.state") if state is not None else None
    journal = item.get("journal")
    journal = _mapping(journal, "capsule entry.journal") if journal is not None else None
    result = item.get("result")
    result = _mapping(result, "capsule entry.result") if result is not None else None
    reconciliation = item.get("reconciliation")
    reconciliation = (
        _mapping(reconciliation, "capsule entry.reconciliation")
        if reconciliation is not None
        else None
    )
    grounding = item.get("grounding")
    grounding = _mapping(grounding, "capsule entry.grounding") if grounding is not None else None
    refs = _refs(capsule, state, journal, result, reconciliation, grounding)
    capsule_id = _token(capsule.get("capsule_id"), "capsule id")
    lifecycle = _status(
        _first(state or {}, "state", "status") or _first(result or {}, "status"),
        allowed={
            "preflighted",
            "continuing",
            "locally_repairing",
            "executing",
            "validating",
            "reconciling",
            "reviewing",
            "awaiting_hil",
            "candidate_ready",
            "accepted",
            "completed",
            "blocked",
            "failed",
            "superseded",
            _UNAVAILABLE,
        },
    )
    episode = state.get("active_episode") if isinstance(state, Mapping) else None
    last_outcome = _first(state or {}, "last_outcome") or _first(result or {}, "status")
    episode_status = (
        "executing"
        if isinstance(episode, Mapping)
        else _status(
            last_outcome,
            allowed={
                "continue_with_evidence",
                "candidate_ready",
                "local_repair_required",
                "reconciliation_required",
                "hil_required",
                "blocked",
                "failed",
                _UNAVAILABLE,
            },
        )
    )
    if lifecycle == "awaiting_hil" or episode_status == "hil_required":
        episode_status = "awaiting_hil"
    expected = capsule.get("expected_surface")
    authorized = capsule.get("mutation_envelope")
    observed = _candidate_scope(result, grounding)
    bindings = _binding_rows(intent, capsule, reconciliation, refs)
    invariants = _invariant_rows(intent, capsule, state, result, reconciliation, refs)
    obligations = _obligation_rows(intent, capsule, state, result, refs)
    hil = _hil(prepared, state, item.get("hil_request"))
    review = _review(prepared, intent, state, result, refs, root=item.get("root"))
    repair_count = _first(state or {}, "repair_episodes")
    repair_count = repair_count if isinstance(repair_count, int) and repair_count >= 0 else 0
    repair_status = "required" if lifecycle == "locally_repairing" else "none"
    integration_refs = (
        _first(result or {}, "integration_refs") or _first(state or {}, "integration_refs") or []
    )
    integration_status = "observed" if integration_refs else _UNAVAILABLE
    proof = _proof_transition(prepared, result, reconciliation, state, refs)
    metrics = (
        _first(result or {}, "administration_metrics")
        or _first(state or {}, "administration_metrics")
        or run_metrics
        or {}
    )
    metric_refs = _refs(metrics, refs)
    metric_names = (
        "operator_commands",
        "time_to_first_action_seconds",
        "provider_launches",
        "review_launches",
        "repair_episodes",
        "prompt_bytes",
        "repeated_context_bytes",
        "hil_requests",
        "episode_count",
        "hil_answers",
        "provider_usage_tokens",
    )
    administration = {
        "status": "measured" if isinstance(metrics, Mapping) and metrics else _UNAVAILABLE,
        "metrics": {
            name: _metric(
                metrics.get(name) if isinstance(metrics, Mapping) else None,
                name=name,
                refs=metric_refs,
            )
            for name in metric_names
        },
        "evidence_refs": metric_refs,
    }
    state_usage = state.get("usage") if isinstance(state, Mapping) else None
    usage_limit = (
        run_metrics.get("provider_usage_limitation") if isinstance(run_metrics, Mapping) else None
    )
    usage = {
        "status": "measured" if isinstance(state_usage, Mapping) and state_usage else _UNAVAILABLE,
        "provider": _copy(state_usage if isinstance(state_usage, Mapping) else {}),
        "billed_tokens": None,
        "reason": None
        if isinstance(state_usage, Mapping) and state_usage
        else _text(
            usage_limit, "provider usage limitation", default="provider billing usage unavailable"
        ),
        "evidence_refs": metric_refs,
    }
    custody = _custody(item.get("custody"))
    summary = _text(
        _first(state or {}, "next_action") or _first(result or {}, "next_action"),
        "capsule lifecycle reason",
        default="lifecycle evidence unavailable",
    )
    return {
        "capsule_id": capsule_id,
        "status": lifecycle,
        "lifecycle_reason": summary,
        # The provider of this capsule's most recent author episode; a
        # capsule plan names no fixed provider ahead of time (see _capsule_provider).
        "provider": _capsule_provider(state, result),
        "episode": {
            "status": episode_status,
            "episode_id": _token(episode.get("episode_id"), "episode id", default=None)
            if isinstance(episode, Mapping)
            else None,
            "count": state.get("episode_count")
            if isinstance(state, Mapping) and isinstance(state.get("episode_count"), int)
            else 0,
            "reason": summary,
        },
        "coherence": {
            "intent_envelope_id": _token(
                intent.get("intent_envelope_id"), "intent id", default=None
            ),
            "binding_refs": sorted(
                item for item in capsule.get("binding_refs", []) if isinstance(item, str)
            ),
            "invariant_refs": sorted(
                item for item in capsule.get("invariant_refs", []) if isinstance(item, str)
            ),
            "obligation_count": len(capsule.get("obligation_coverage", []))
            if isinstance(capsule.get("obligation_coverage"), list)
            else 0,
        },
        "bindings": bindings,
        "invariants": invariants,
        "expected_scope": _scope(expected, status="declared", refs=refs),
        "authorized_scope": _scope(authorized, status="declared", refs=refs),
        "observed_scope": observed,
        "obligations": obligations,
        "revision": _copy(revision),
        "hil": hil,
        "decision_proposals": _decision_proposals(state, refs),
        "review": review,
        "repair": {
            "status": repair_status,
            "episodes": repair_count,
            "reason": summary if repair_status == "required" else None,
        },
        "reconciliation": {
            "status": _status(
                reconciliation.get("status") if reconciliation else None,
                allowed={"consistent", "changed", "blocked", _UNAVAILABLE},
            ),
            "correction_class": reconciliation.get("correction_class")
            if reconciliation and reconciliation.get("correction_class") in _CORRECTION_CLASSES
            else _UNAVAILABLE,
            "evidence_refs": _refs(reconciliation, refs),
        },
        "integration": {
            "status": integration_status,
            "evidence_refs": _refs(integration_refs, refs),
        },
        "administration": administration,
        "usage": usage,
        "observed_proof_next_correction": proof,
        "freshness": {"status": _UNAVAILABLE, "reason": "per-capsule timestamp unavailable"},
        "custody": custody,
    }


def build_capsule_cockpit(
    *,
    prepared: Mapping[str, Any],
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    run: Mapping[str, Any],
    capsules: Sequence[Mapping[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one deterministic conceptual cockpit from validated evidence records.

    The function accepts no filesystem paths or authority callbacks.  Missing runtime evidence is
    represented as ``unavailable`` and never promoted to a pass.  The returned value is a view
    only; callers still use the campaign or capsule controller for every mutation.
    """

    prepared = _mapping(prepared, "prepared")
    intent = _mapping(intent, "intent")
    plan = _mapping(plan, "plan")
    run = _mapping(run, "run")
    if not isinstance(capsules, Sequence) or isinstance(capsules, (str, bytes, bytearray)):
        raise CapsuleCockpitError("capsules must be an array")
    if not capsules:
        raise CapsuleCockpitError("capsules must not be empty")
    campaign_id = _token(
        _first(prepared, "campaign_id") or _first(run, "campaign_id"),
        "campaign id",
    )
    run_id = _token(_first(prepared, "run_id") or _first(run, "run_id"), "run id")
    locator = _text(prepared.get("locator"), "prepared locator", default=None)
    source_sha = _sha(
        _first(prepared, "source_sha256", "content_sha256") or _source_digest(run),
        "prepared source_sha256",
        default=None,
    )
    intent_id = _token(intent.get("intent_envelope_id"), "intent envelope id", default=None)
    intent_refs = _refs(intent, plan, prepared, run)
    revision = _revision(plan)
    projections = []
    seen: set[str] = set()
    run_metrics = run.get("metrics") if isinstance(run.get("metrics"), Mapping) else None
    plan_capsules = {
        row.get("capsule_id"): row
        for row in _list(plan.get("capsules"), "plan capsules")
        if isinstance(row, Mapping)
    }
    for item in capsules:
        item = _mapping(item, "capsule entry")
        capsule = _mapping(item.get("capsule"), "capsule entry.capsule")
        capsule_id = _token(capsule.get("capsule_id"), "capsule id")
        if capsule_id in seen:
            raise CapsuleCockpitError(f"capsules repeat {capsule_id!r}")
        seen.add(capsule_id)
        expected = plan_capsules.get(capsule_id)
        if plan_capsules and expected is None:
            raise CapsuleCockpitError(f"capsule {capsule_id!r} is foreign to the supplied plan")
        if expected is not None and _canonical(expected) != _canonical(capsule):
            raise CapsuleCockpitError(f"capsule {capsule_id!r} differs from the supplied plan")
        projections.append(
            _capsule_projection(
                prepared=prepared,
                intent=intent,
                plan=plan,
                item=item,
                revision=revision,
                run_metrics=run_metrics,
            )
        )
    items_by_id = {row["capsule_id"]: item for row, item in zip(projections, capsules, strict=True)}
    state_rows = [item.get("state") for item in capsules if isinstance(item.get("state"), Mapping)]
    states = [row.get("state") for row in state_rows if isinstance(row, Mapping)]
    active_id = _token(run.get("active_capsule_id"), "active capsule id", default=None)
    active = (
        next((row for row in projections if row["capsule_id"] == active_id), None)
        if active_id is not None
        else next(
            (
                row
                for row in projections
                if row["status"]
                in {
                    "executing",
                    "validating",
                    "reconciling",
                    "reviewing",
                    "awaiting_hil",
                    "locally_repairing",
                }
            ),
            None,
        )
    )
    active_item = items_by_id.get(active["capsule_id"]) if active is not None else None
    if active_id is not None and active_item is None:
        # A phase integration or another controller boundary can be active without being one of
        # the capsule rows.  Do not silently attribute its proof to the last capsule in the list.
        active_item = None
    lifecycle = _status(
        _first(run, "status", "state") or (active["status"] if active else None),
        allowed={
            "prepared",
            "preflighted",
            "running",
            "validating",
            "reconciling",
            "reviewing",
            "awaiting_hil",
            "blocked",
            "complete",
            "completed",
            "failed",
            "stopped",
            _UNAVAILABLE,
        },
    )
    if lifecycle == _UNAVAILABLE and states:
        lifecycle = (
            "running"
            if any(state not in {"completed", "accepted"} for state in states)
            else "complete"
        )
    # An actionable request is more useful than a missing request from another capsule.  Keep
    # awaiting HIL ahead of unavailable evidence while preserving the explicit unavailable state
    # when no actionable request can be rendered.
    hil = next((row["hil"] for row in projections if row["hil"]["status"] == "awaiting"), None)
    if hil is None:
        hil = next(
            (row["hil"] for row in projections if row["hil"]["status"] == _UNAVAILABLE),
            None,
        )
    if hil is None:
        hil = {
            "status": "none",
            "request_id": None,
            "checkpoint": None,
            "decision": None,
            "summary": None,
            "evidence_refs": [],
            "remediation": _remediation("none", None, None),
        }
    phase = run.get("phase") if isinstance(run.get("phase"), Mapping) else {}
    phase_result = phase.get("result") if isinstance(phase.get("result"), Mapping) else None
    phase_receipt = (
        phase.get("integration_receipt")
        if isinstance(phase.get("integration_receipt"), Mapping)
        else None
    )
    phase_refs = _refs(
        phase,
        phase_result,
        phase_receipt,
        *(
            phase_result.get(key)
            for key in ("integration_receipt_sha256", "prompt_sha256")
            if phase_result is not None
        ),
        *(phase_receipt.get(key) for key in ("content_sha256",) if phase_receipt is not None),
    )
    if active_item is not None:
        top_result = active_item.get("result")
        top_reconciliation = active_item.get("reconciliation")
        top_state = active_item.get("state")
    elif phase_result is not None and (active_id is None or active_item is None):
        # Once all capsule work is complete, the combined phase result is the current proof.  It
        # is more authoritative for the campaign view than whichever capsule happens to sort last.
        top_result = phase_result
        top_reconciliation = phase.get("reconciliation")
        top_state = None
    elif active_id is None:
        # With no active capsule and no combined phase result, retain the historical last observed
        # capsule projection as the best available view.
        fallback = next(
            (item for item in capsules[::-1] if isinstance(item.get("result"), Mapping)),
            None,
        )
        top_result = fallback.get("result") if fallback is not None else None
        top_reconciliation = (
            fallback.get("reconciliation")
            if fallback is not None and isinstance(fallback.get("reconciliation"), Mapping)
            else None
        )
        top_state = (
            fallback.get("state")
            if fallback is not None and isinstance(fallback.get("state"), Mapping)
            else None
        )
    else:
        # The active controller boundary is outside the capsule rows (for example phase
        # integration).  Its proof is unavailable until that boundary supplies its own result.
        top_result = top_reconciliation = top_state = None
    # Keep campaign-level proof tied to the selected runtime evidence.  Intent/plan references
    # identify the sealed context but cannot make an otherwise unreferenced candidate proof appear
    # observed.
    proof = _proof_transition(
        prepared,
        top_result,
        top_reconciliation,
        top_state,
        _refs(top_result, top_reconciliation, top_state, phase_refs),
    )
    freshness = _freshness(run, top_state, now or datetime.now(UTC), prepared)
    blockers = [
        row["lifecycle_reason"] for row in projections if row["status"] in {"blocked", "failed"}
    ]
    if freshness["status"] in {"stale", _UNAVAILABLE}:
        blockers.append(freshness["reason"] or "freshness unavailable")
    recovery = _recovery_remediation(locator, active_item)
    if hil["status"] == "awaiting":
        remediation = hil["remediation"]
    elif recovery is not None:
        remediation = recovery
    elif active_id == "phase.integration" and locator:
        phase_recovery = phase.get("recovery")
        command = "resume" if phase_recovery == "resume" else "recover"
        disposition = (
            " --recovery-outcome failed" if phase_recovery == "failed_disposition_required" else ""
        )
        remediation = _remediation(
            "actionable",
            "phase_requires_resume" if command == "resume" else "phase_requires_recovery",
            f"bearhug campaign {command} {_shell_locator(locator)}{disposition}",
        )
    elif blockers and locator:
        remediation = _remediation(
            "actionable",
            "campaign_requires_resume",
            f"bearhug campaign resume {_shell_locator(locator)}",
        )
    elif freshness["status"] in {"stale", _UNAVAILABLE} and locator:
        remediation = _remediation(
            "actionable",
            "campaign_freshness_requires_status",
            f"bearhug campaign status {_shell_locator(locator)}",
        )
    else:
        remediation = _remediation("none", None, None)
    all_refs = _refs(
        intent_refs, *(row["observed_proof_next_correction"]["proof_refs"] for row in projections)
    )
    review_status = (
        "eligible"
        if projections
        and all(row["review"]["status"] in {"eligible", "not_required"} for row in projections)
        else _UNAVAILABLE
    )
    review_required = any(row["review"]["required"] for row in projections)
    review_eligible = sum(row["review"]["eligible"] for row in projections)
    review_quorum = max((row["review"]["quorum"] for row in projections), default=0)
    all_refs = _refs(all_refs, phase_refs)
    integration_status = (
        "observed"
        if phase_result is not None
        or phase_receipt is not None
        or any(row["integration"]["status"] == "observed" for row in projections)
        else _UNAVAILABLE
    )
    metrics = {}
    for name in (
        "operator_commands",
        "time_to_first_action_seconds",
        "provider_launches",
        "review_launches",
        "repair_episodes",
        "prompt_bytes",
        "repeated_context_bytes",
        "hil_requests",
        "episode_count",
        "hil_answers",
        "provider_usage_tokens",
    ):
        values = [row["administration"]["metrics"][name] for row in projections]
        if isinstance(run_metrics, Mapping) and name in run_metrics:
            values.insert(0, _metric(run_metrics[name], name=name, refs=all_refs))
        measured = [row for row in values if row["status"] == "measured"]
        if measured:
            metrics[name] = measured[0]
        else:
            metrics[name] = _metric(None, name=name, refs=all_refs)
    custody = {
        "status": "observed"
        if any(row["custody"]["status"] == "observed" for row in projections)
        else _UNAVAILABLE,
        "capsules": [{"capsule_id": row["capsule_id"], **row["custody"]} for row in projections],
    }
    provider_tokens = (
        run_metrics.get("provider_usage_tokens") if isinstance(run_metrics, Mapping) else None
    )
    provider_usage_measured = (
        any(row["usage"]["status"] == "measured" for row in projections)
        or isinstance(provider_tokens, (int, float))
        and not isinstance(provider_tokens, bool)
    )
    provider_usage_reason = (
        None
        if provider_usage_measured
        else _text(
            run_metrics.get("provider_usage_limitation")
            if isinstance(run_metrics, Mapping)
            else None,
            "provider usage limitation",
            default="provider billing usage unavailable",
        )
    )
    return {
        # v3 adds each capsule's provider and, per capsule, the
        # author/reviewer sessions' promotion eligibility -- a shape change to this closed record,
        # so its version moved 2 -> 3 (same convention as COCKPIT_SCHEMA_VERSION in
        # replay/cockpit.py: a version bump per shape change, the reader rejects the rest).
        # v4 adds review.review_records/resolved_bundles/
        # unresolved_bundles -- another shape change, 3 -> 4.
        "schema_version": "4",
        "record_kind": "campaign_cockpit",
        "generated_at": _time_text(now or datetime.now(UTC)),
        "campaign_id": campaign_id,
        "run_id": run_id,
        "source_sha256": source_sha,
        "prepared": {
            "locator": locator,
            "status": "bound" if locator else _UNAVAILABLE,
            "source_sha256": source_sha,
            "intent_envelope_sha256": _sha(
                _first(prepared, "intent_envelope_sha256") or _source_digest(intent),
                "intent envelope digest",
                default=None,
            ),
            "plan_sha256": _sha(
                _first(prepared, "plan_sha256") or _source_digest(plan), "plan digest", default=None
            ),
        },
        "intent": {
            "intent_envelope_id": intent_id,
            "goal": _text(intent.get("goal"), "intent goal", default="intent goal unavailable"),
            "summary": _text(
                intent.get("intent"), "intent summary", default="intent summary unavailable"
            ),
            "constraints": sorted(
                item for item in intent.get("constraints", []) if isinstance(item, str)
            ),
            "non_goals": sorted(
                item for item in intent.get("non_goals", []) if isinstance(item, str)
            ),
            "authority_refs": _copy(intent.get("authority_refs", [])),
            "invariant_ids": sorted(
                item.get("invariant_id")
                for item in intent.get("invariants", [])
                if isinstance(item, Mapping) and isinstance(item.get("invariant_id"), str)
            ),
            "obligation_count": len(intent.get("obligations", []))
            if isinstance(intent.get("obligations"), list)
            else 0,
            "evidence_refs": _refs(intent),
        },
        "phase": {
            "status": lifecycle,
            "reason": _text(
                _first(run, "reason", "next_action"),
                "phase reason",
                default="phase status derived from durable run evidence",
            ),
            "plan_id": revision["plan_id"],
            "revision_id": revision["revision_id"],
            "active_capsule_id": active["capsule_id"] if active else active_id,
            "capsule_count": len(projections),
        },
        "capsules": projections,
        "observed_proof_next_correction": proof,
        "hil": hil,
        "review": {
            "required": review_required,
            "status": review_status,
            "eligible": review_eligible,
            "quorum": review_quorum,
            "findings": [finding for row in projections for finding in row["review"]["findings"]],
            "capsules": [row["review"] for row in projections],
            "evidence_refs": all_refs,
        },
        "repair": {
            "episodes": sum(row["repair"]["episodes"] for row in projections),
            "status": "required"
            if any(row["repair"]["status"] == "required" for row in projections)
            else "none",
            "reason": next(
                (row["repair"]["reason"] for row in projections if row["repair"]["reason"]),
                None,
            ),
        },
        "integration": {
            "status": integration_status,
            "capsules": [row["integration"] for row in projections],
            "promotion_authorized": False,
            "evidence_refs": _refs(all_refs, phase_refs),
        },
        "administration": {
            "status": "measured"
            if any(row["administration"]["status"] == "measured" for row in projections)
            else _UNAVAILABLE,
            "metrics": metrics,
            "evidence_refs": all_refs,
        },
        "usage": {
            "status": "measured" if provider_usage_measured else _UNAVAILABLE,
            "provider": _copy(run.get("usage", {})),
            "billed_tokens": None,
            "reason": provider_usage_reason,
            "evidence_refs": all_refs,
        },
        "freshness": freshness,
        "custody": custody,
        "remediation": remediation,
        "limitations": [
            "read-only projection; campaign and capsule controllers remain the mutation authority",
            "missing evidence is unavailable and does not establish success",
            "provider billing and process liveness are shown only when durable evidence "
            "supplies them",
            "raw provider streams are omitted from the conceptual view",
        ],
    }


__all__ = ["CapsuleCockpitError", "build_capsule_cockpit"]
