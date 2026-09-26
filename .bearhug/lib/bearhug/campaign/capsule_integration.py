"""Accepted capsule integration and independent combined-system assessment.

This adapter retains the existing serialized Git/check engine and claim store.  The phase
boundary adds an original-goal assessment over the resulting tree; capsule acceptance alone
cannot establish that the combined system works.  No promotion or push is performed here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bearhug.campaign.capsule_packets import _estimate_tokens
from bearhug.campaign.capsule_review import (
    _durable_request,
    _normalise_authority_sources,
    verify_capsule_acceptance,
)
from bearhug.campaign.capsule_runtime import _create_blob_only, _provider_policy_document
from bearhug.campaign.capsules import validate_capsule_plan, validate_intent_envelope
from bearhug.campaign.claims import acquire_claim, release_claim
from bearhug.campaign.integration import (
    CampaignIntegrationResult,
    integrate_campaign_candidates,
    verify_integration_git,
)
from bearhug.campaign.review import canonical_json_sha256, worktree_sha256
from bearhug.campaign.reviewer import _default_runner, run_candidate_reviewer
from bearhug.providers.final_output import strict_final_json
from bearhug.providers.receipt import capture_launch_repository, validate_provider_receipt


class CapsuleIntegrationError(ValueError):
    """The accepted inputs or combined-system evidence cannot establish phase completion."""


def phase_assessment_schema() -> dict[str, Any]:
    """Enforce the existing phase response shape at providers supporting structured output."""
    status = {"type": "string", "enum": ["pass", "fail", "unavailable"]}
    summary = {"type": "string"}  # Semantic validation retains the existing length bound.
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidate_sha256", "goal", "invariants", "material_concept_drift"],
        "properties": {
            "candidate_sha256": {"type": "string"},
            "goal": {
                "type": "object", "additionalProperties": False,
                "required": ["status", "summary"],
                "properties": {"status": status, "summary": summary},
            },
            "invariants": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["invariant_id", "status", "summary"],
                    "properties": {
                        "invariant_id": {"type": "string"}, "status": status, "summary": summary,
                    },
                },
            },
            "material_concept_drift": {
                "type": "string", "enum": ["present", "absent", "unavailable"],
            },
        },
    }


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _verified_inputs(intent, plan, bundles, *, allow_historical=False):
    selected = {row["capsule_id"]: row for row in plan["capsules"]}
    verified = []
    seen: set[str] = set()
    for bundle in bundles:
        if bundle.get("_historical_only") and not allow_historical:
            raise CapsuleIntegrationError("historical custody cannot authorize fresh integration")
        if (
            validate_intent_envelope(bundle.get("intent_envelope")).digest
            != validate_intent_envelope(intent).digest
        ):
            raise CapsuleIntegrationError("capsule acceptance belongs to another sealed intent")
        capsule = bundle.get("capsule", {})
        capsule_id = capsule.get("capsule_id")
        if capsule_id not in selected or capsule != selected[capsule_id] or capsule_id in seen:
            raise CapsuleIntegrationError(
                "integration contains a missing, changed, or repeated capsule"
            )
        # Historical results remain usable only when the active plan preserves this capsule exactly.
        result = verify_capsule_acceptance(
            **bundle,
            dependency_acceptance_bundles=bundles,
        )
        if not set(capsule["depends_on"]) <= seen:
            raise CapsuleIntegrationError("capsules must be integrated in dependency order")
        seen.add(capsule_id)
        verified.append(result)
    if seen != set(selected):
        raise CapsuleIntegrationError("phase integration requires every active capsule")
    return verified


def _historical_phase_metadata(accepted, fence, custody):
    """Restore only historical prompt annotations from run custody, never live qualification.

    New fences preserve these non-authorizing labels before any reviewer allocation. Older
    fences may recover them from an exact request whose digest the fence already committed to.
    All governing acceptance fields are independently reconstructed; the complete regenerated
    prompt must still match the original fence/request digest.
    """
    metadata = fence.get("acceptance_qualification_manifests")
    if metadata is None:
        from bearhug.campaign.capsule_review import _durable_request

        for path in sorted(custody.root.glob("*.json")):
            record = custody._read_record(path)
            provider = validate_provider_receipt(
                json.loads(Path(record["receipt_path"]).read_bytes())
            )
            if provider["launch"]["prompt_sha256"] != fence["prompt_sha256"]:
                continue
            custody.validate("historical-phase-request", provider)
            prompt = _durable_request(custody, provider)
            if hashlib.sha256(prompt).hexdigest() != fence["prompt_sha256"]:
                raise CapsuleIntegrationError("historical phase request digest changed")
            metadata = {
                row["capsule_id"]: row["qualification_manifest_sha256s"]
                for row in json.loads(prompt)["accepted_capsules"]
            }
            break
    if not isinstance(metadata, dict) or set(metadata) != {row["capsule_id"] for row in accepted}:
        raise CapsuleIntegrationError(
            "historical phase prompt annotations are unavailable in sealed run custody"
        )
    result = copy.deepcopy(accepted)
    for row in result:
        digests = metadata[row["capsule_id"]]
        if (
            not isinstance(digests, list) or len(digests) > 16
            or any(not isinstance(digest, str) or len(digest) != 64
                   or any(char not in "0123456789abcdef" for char in digest) for digest in digests)
            or digests != sorted(set(digests))
        ):
            raise CapsuleIntegrationError("historical phase manifest annotations changed")
        row["qualification_manifest_sha256s"] = digests
    return result


def integrate_accepted_capsules(
    *,
    intent_envelope: Mapping[str, Any],
    capsule_plan: Mapping[str, Any],
    acceptance_bundles: Sequence[Mapping[str, Any]],
    state_root: Path | str,
    campaign_root: Path | str,
    campaign_id: str,
    run_id: str,
    integration_id: str,
    integrator_id: str,
    target: Path | str,
    checks: Sequence[Sequence[str]],
    previous_plan: Mapping[str, Any] | None = None,
    check_timeout_s: float = 28800.0,
) -> tuple[CampaignIntegrationResult, list[dict[str, Any]]]:
    """Reopen every acceptance proof before acquiring the separate integration claim."""
    validate_intent_envelope(intent_envelope)
    validate_capsule_plan(
        capsule_plan, intent_envelope=intent_envelope, previous_plan=previous_plan
    )
    policy = intent_envelope["campaign_envelope"]["policy_snapshot"]["policies"]["integration"]
    if policy["mode"] != "serialized" or integrator_id != policy["integrator_work_unit_id"]:
        raise CapsuleIntegrationError("integration owner differs from sealed serialized policy")
    accepted = _verified_inputs(intent_envelope, capsule_plan, acceptance_bundles)
    bundle_by_capsule = {bundle["capsule"]["capsule_id"]: bundle for bundle in acceptance_bundles}
    target_path = Path(target).resolve()
    author_paths = {
        Path(row["cwd"]).resolve()
        for bundle in acceptance_bundles
        for row in bundle["author_receipts"]
    }
    if target_path in author_paths:
        raise CapsuleIntegrationError("integration requires a separate worktree")
    launch = capture_launch_repository(target_path)
    branch = subprocess.run(
        ["git", "-C", str(target_path), "symbolic-ref", "--quiet", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    envelope = intent_envelope["campaign_envelope"]["mutation_envelope"]
    claim = acquire_claim(
        campaign_root,
        campaign_id=campaign_id,
        claimant_id=integrator_id,
        role="integrator",
        repository_common_dir_sha256=launch.repository_common_dir_sha256,
        worktree_sha256=worktree_sha256(str(target_path)),
        branch=branch,
        base_oid=capsule_plan["subject"]["base_oid"],
        path_prefixes=envelope["path_prefixes"],
        semantic_resources=envelope["semantic_resources"],
    )
    # An interruption retains the claim for explicit recovery.
    result = integrate_campaign_candidates(
        state_root=state_root,
        campaign_id=campaign_id,
        run_id=run_id,
        integration_id=integration_id,
        integrator_id=integrator_id,
        target=target_path,
        base_oid=capsule_plan["subject"]["base_oid"],
        checks=checks,
        check_timeout_s=check_timeout_s,
        candidates=[
            {
                "work_unit_id": row["capsule_id"],
                "merge_order": index,
                "provider_receipt_sha256": row["author_receipt_sha256s"][-1],
                "candidate": row["candidate"],
                **(
                    {"dependency_base": bundle_by_capsule[row["capsule_id"]]["dependency_base"]}
                    if bundle_by_capsule[row["capsule_id"]].get("dependency_base") is not None
                    else {}
                ),
            }
            for index, row in enumerate(accepted, start=1)
        ],
        _review_refs=[
            {
                "work_unit_id": row["capsule_id"],
                "review_id": f"capsule-review-{index}-{number}",
                "receipt_sha256": digest,
            }
            for index, row in enumerate(accepted)
            for number, digest in enumerate(row["reviewer_receipt_sha256s"])
        ],
        _claim=claim,
    )
    release_claim(
        campaign_root,
        claim["claim_id"],
        claimant_id=integrator_id,
        reason="completed" if result.receipt["status"] == "passed" else "failed",
    )
    return result, accepted


def build_phase_review_prompt(
    *,
    intent_envelope,
    capsule_plan,
    integration_receipt,
    accepted,
    authority_sources,
    check_commands,
    previous_plan=None,
) -> str:
    """Render original authority and the combined candidate, with no episode transcript."""
    validate_intent_envelope(intent_envelope)
    validate_capsule_plan(
        capsule_plan, intent_envelope=intent_envelope, previous_plan=previous_plan
    )
    # Every capsule's sealed budget must accommodate its mandatory authority; use the tightest
    # budget for this shared phase review instead of inventing another operator setting.
    budgets = [row["context_budget"] for row in capsule_plan["capsules"]]
    explicit = [row for row in budgets if row["mode"] == "explicit"]
    if not explicit:
        raise CapsuleIntegrationError("native phase review requires a sealed context budget")
    budget = min(explicit, key=lambda row: row["max_bytes"] - row["reserve_bytes"])
    authority = _normalise_authority_sources(
        intent_envelope, {"context_budget": budget}, authority_sources
    )
    if not check_commands or len(check_commands) != len(integration_receipt["checks"]):
        raise CapsuleIntegrationError("phase review must include every executed system check")
    checks = []
    for argv, observed in zip(check_commands, integration_receipt["checks"], strict=True):
        argv = list(argv)
        if hashlib.sha256(_canonical(argv).encode()).hexdigest() != observed["argv_sha256"]:
            raise CapsuleIntegrationError(
                "phase system check command differs from actual execution"
            )
        checks.append(
            {"argv": argv, **observed, "output_bytes": "unavailable in legacy integration receipt"}
        )
    payload = {
        "system_checks": checks,
        "task": (
            "Independently compare the exact combined system with the original approved goal "
            "and every system invariant. Inspect the Git diff and actual checks. Missing "
            "evidence is unavailable. Material concept drift must block completion."
        ),
        "intent_envelope": intent_envelope,
        "authority_sources": authority,
        "candidate": integration_receipt["candidate"],
        "integration_receipt": integration_receipt,
        "accepted_capsules": accepted,
        "response_contract": {
            "candidate_sha256": canonical_json_sha256(integration_receipt["candidate"]),
            "goal": {"status": "pass|fail|unavailable", "summary": "reason with concrete evidence"},
            "invariants": [
                {
                    "invariant_id": row["invariant_id"],
                    "status": "pass|fail|unavailable",
                    "summary": "reason with concrete evidence",
                }
                for row in intent_envelope["invariants"]
            ],
            "material_concept_drift": "present|absent|unavailable",
        },
    }
    prompt = _canonical(payload)
    if (
        len(prompt.encode("utf-8")) > budget["max_bytes"] - budget["reserve_bytes"]
        or _estimate_tokens(prompt.encode("utf-8"))
        > budget["max_tokens"] - budget["reserve_tokens"]
    ):
        raise CapsuleIntegrationError("phase review mandatory context exceeds sealed byte budget")
    return prompt


def validate_phase_assessment(value, *, candidate, invariant_ids):
    if not isinstance(value, dict) or set(value) != {
        "candidate_sha256",
        "goal",
        "invariants",
        "material_concept_drift",
    }:
        raise CapsuleIntegrationError("phase reviewer response has missing or unknown fields")
    if value["candidate_sha256"] != canonical_json_sha256(candidate):
        raise CapsuleIntegrationError("phase review applies to another combined candidate")
    if value["material_concept_drift"] not in {"present", "absent", "unavailable"}:
        raise CapsuleIntegrationError("phase concept drift status is invalid")

    def assessment(row, fields):
        if not isinstance(row, dict) or set(row) != fields:
            raise CapsuleIntegrationError("phase assessment fields are invalid")
        if row["status"] not in {"pass", "fail", "unavailable"}:
            raise CapsuleIntegrationError("phase assessment status is invalid")
        if not isinstance(row["summary"], str) or not 1 <= len(row["summary"].strip()) <= 4096:
            raise CapsuleIntegrationError("phase assessment requires a bounded evidence summary")

    assessment(value["goal"], {"status", "summary"})
    rows = value["invariants"]
    if not isinstance(rows, list):
        raise CapsuleIntegrationError("phase invariants must be an array")
    for row in rows:
        assessment(row, {"invariant_id", "status", "summary"})
    if len(rows) != len(invariant_ids) or {row["invariant_id"] for row in rows} != set(
        invariant_ids
    ):
        raise CapsuleIntegrationError("phase review must assess every original system invariant")
    return copy.deepcopy(value)


def review_integrated_phase(
    *,
    intent_envelope,
    capsule_plan,
    acceptance_bundles,
    integration_store,
    integration_id,
    authority_sources,
    check_commands,
    reviewer_specs,
    provider_policy,
    custody,
    previous_plan=None,
    runner=_default_runner,
    timeout_s=28800.0,
    _attempt=1,
    _resume_reviews=None,
    _resume_ordinals=None,
    _retry_of_fence_sha256=None,
):
    """Run independent combined-system reviews; return evidence, never a promotion authority."""
    policy_sha = canonical_json_sha256(_provider_policy_document(provider_policy))
    if policy_sha not in intent_envelope["campaign_envelope"]["policy_refs"]:
        raise CapsuleIntegrationError("phase provider policy is outside sealed authority")
    budget = intent_envelope["campaign_envelope"]["budget"]
    if any(
        budget[field] is not None for field in ("max_provider_tokens", "max_provider_spend_cents")
    ):
        raise CapsuleIntegrationError(
            "finite phase provider budget cannot be enforced with unavailable usage"
        )
    accepted = _verified_inputs(intent_envelope, capsule_plan, acceptance_bundles)
    receipt = integration_store.read(integration_id)
    if receipt["status"] == "passed":
        verify_integration_git(receipt)
    if receipt["status"] != "passed" or receipt["blockers"]:
        raise CapsuleIntegrationError("combined Git integration or system checks did not pass")
    expected = [(row["capsule_id"], row["candidate"]) for row in accepted]
    if [(row["work_unit_id"], row["candidate"]) for row in receipt["inputs"]] != expected:
        raise CapsuleIntegrationError("integration inputs differ from accepted capsule custody")
    launch = capture_launch_repository(Path(receipt["target"]))
    candidate = receipt["candidate"]
    if (launch.head_oid, launch.tree_oid, launch.repository_common_dir_sha256) != (
        candidate["head_oid"],
        candidate["tree_oid"],
        candidate["repository_common_dir_sha256"],
    ):
        raise CapsuleIntegrationError("combined candidate changed after integration checks")
    prompt = build_phase_review_prompt(
        intent_envelope=intent_envelope,
        capsule_plan=capsule_plan,
        integration_receipt=receipt,
        accepted=accepted,
        authority_sources=authority_sources,
        check_commands=check_commands,
        previous_plan=previous_plan,
    )
    quorum = intent_envelope["campaign_envelope"]["policy_snapshot"]["policies"]["review"][
        "minimum_approvals"
    ]
    if len(reviewer_specs) != quorum:
        raise CapsuleIntegrationError("phase review requires the exact sealed independent quorum")
    sessions = {
        row["session_id"] for bundle in acceptance_bundles for row in bundle["author_receipts"]
    }
    worktrees = {
        Path(row["cwd"]).resolve()
        for bundle in acceptance_bundles
        for row in bundle["author_receipts"]
    }
    worktrees.add(Path(receipt["target"]).resolve())
    reviewer_paths = [Path(spec["review_worktree"]).resolve() for spec in reviewer_specs]
    if len(set(reviewer_paths)) != len(reviewer_paths) or set(reviewer_paths) & worktrees:
        raise CapsuleIntegrationError("phase reviewer worktrees must be independent and distinct")
    for path in reviewer_paths:
        reviewer_launch = capture_launch_repository(path)
        if (
            reviewer_launch.head_oid,
            reviewer_launch.tree_oid,
            reviewer_launch.repository_common_dir_sha256,
        ) != (
            candidate["head_oid"],
            candidate["tree_oid"],
            candidate["repository_common_dir_sha256"],
        ):
            raise CapsuleIntegrationError("phase reviewer checkout is stale")
    if type(_attempt) is not int or _attempt < 1:
        raise CapsuleIntegrationError("phase review attempt is invalid")
    if _resume_reviews is None:
        _resume_reviews = {}
    if not isinstance(_resume_reviews, Mapping):
        raise CapsuleIntegrationError("phase review recovered reviews are invalid")
    resume_ordinals = set(range(quorum)) if _resume_ordinals is None else set(_resume_ordinals)
    if set(_resume_reviews) - set(range(quorum)) or set(_resume_reviews) & resume_ordinals:
        raise CapsuleIntegrationError("phase review recovered ordinals are invalid")
    if set(_resume_reviews) | resume_ordinals != set(range(quorum)):
        raise CapsuleIntegrationError("phase review retry does not name every quorum ordinal")
    fence = integration_store.root / (
        f"{integration_id}.phase-review-started.json"
        if _attempt == 1
        else f"{integration_id}.phase-review-started.{_attempt}.json"
    )
    fence_value = _phase_fence_value(
        integration_id=integration_id,
        receipt=receipt,
        prompt=prompt,
        intent_envelope=intent_envelope,
        reviewer_specs=reviewer_specs,
        attempt=_attempt,
        retry_of_fence_sha256=_retry_of_fence_sha256,
    )
    fence_raw = _canonical(fence_value).encode()
    try:
        fd = os.open(fence, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise CapsuleIntegrationError(
            "phase review already started; inspect custody before recovery"
        ) from exc
    with os.fdopen(fd, "wb") as stream:
        stream.write(fence_raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(integration_store.root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    reviews_by_ordinal = dict(_resume_reviews)
    prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
    candidate_sha256 = canonical_json_sha256(candidate)
    for ordinal, spec in enumerate(reviewer_specs):
        if ordinal in reviews_by_ordinal:
            continue
        path = Path(spec["review_worktree"]).resolve()
        if path in worktrees:
            raise CapsuleIntegrationError("phase reviewer must use a distinct independent worktree")
        worktrees.add(path)
        _phase_attempt_event(
            integration_store,
            integration_id,
            _attempt,
            ordinal,
            "started",
            prompt_sha256=prompt_sha256,
            candidate_sha256=candidate_sha256,
        )
        run = run_candidate_reviewer(
            candidate=candidate,
            review_worktree=path,
            prompt=prompt,
            provider_policy=provider_policy,
            role_name=spec["role_name"],
            qualified_provider=spec["qualified_provider"],
            receipt_role="reviewer",
            settings_sha256=spec["qualified_provider"].settings_sha256,
            rules_sha256=spec["qualified_provider"].rules_sha256,
            provider_output_dir=_phase_provider_output_dir(spec, _attempt),
            timeout_s=min(timeout_s, budget["max_seconds"] / quorum)
            if budget["max_seconds"]
            else timeout_s,
            runner=runner,
            decision_reader=None,
            output_schema=phase_assessment_schema(),
        )
        custody.record_run(run.provider_run)
        custody.validate("phase-review", run.provider_receipt)
        if not run.provider_receipt["promotion_eligible"]:
            raise CapsuleIntegrationError(
                "phase reviewer execution identity or custody is ineligible"
            )
        session = run.provider_receipt["session_id"]
        if session in sessions:
            raise CapsuleIntegrationError(
                "phase reviewer session is reused or belongs to an author"
            )
        sessions.add(session)
        assessment = validate_phase_assessment(
            strict_final_json(run.provider_run),
            candidate=candidate,
            invariant_ids=[row["invariant_id"] for row in intent_envelope["invariants"]],
        )
        _phase_attempt_event(
            integration_store,
            integration_id,
            _attempt,
            ordinal,
            "conclusive",
            prompt_sha256=prompt_sha256,
            candidate_sha256=candidate_sha256,
            provider_receipt_sha256=canonical_json_sha256(run.provider_receipt),
        )
        reviews_by_ordinal[ordinal] = {
            "provider_receipt": run.provider_receipt,
            "assessment": assessment,
        }
    reviews = [reviews_by_ordinal[ordinal] for ordinal in range(quorum)]
    current = capture_launch_repository(Path(receipt["target"]))
    if current != launch:
        raise CapsuleIntegrationError("combined candidate changed during phase review")
    passed = all(
        row["assessment"]["goal"]["status"] == "pass"
        and row["assessment"]["material_concept_drift"] == "absent"
        and all(item["status"] == "pass" for item in row["assessment"]["invariants"])
        for row in reviews
    )
    review_launches, prompt_bytes, repeated_bytes = _phase_metrics(
        integration_store,
        integration_id,
        prompt,
        len(reviews),
        canonical_json_sha256(candidate),
    )
    result = {
        "status": "phase_validated" if passed else "blocked",
        "candidate": candidate,
        "integration_receipt_sha256": receipt["content_sha256"],
        "reviews": reviews,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_bytes": prompt_bytes,
        "review_launches": review_launches,
        "repeated_context_bytes": repeated_bytes,
        "provider_usage_tokens": None,
        "usage_limitation": "phase provider billing is unavailable; prompt bytes are measured",
        "promotion_authorized": False,
    }
    _create_blob_only(
        integration_store.root / f"{integration_id}.phase-result.json",
        (_canonical(result) + "\n").encode(),
    )
    return result


def verify_phase_result(
    *,
    intent_envelope,
    capsule_plan,
    acceptance_bundles,
    integration_store,
    integration_id,
    authority_sources,
    check_commands,
    provider_policy,
    qualification_index,
    custody,
    previous_plan=None,
    _historical_only=False,
):
    """Reopen a completed phase after restart without launching another provider."""
    from bearhug.campaign.capsule_review import (
        _durable_final_output,
        _durable_request,
        _provider_authority,
        _reviewer_live_candidate,
    )

    path = integration_store.root / f"{integration_id}.phase-result.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise CapsuleIntegrationError("durable phase result is unavailable")
    result = json.loads(path.read_bytes())
    fields = {
        "status",
        "candidate",
        "integration_receipt_sha256",
        "reviews",
        "prompt_sha256",
        "prompt_bytes",
        "review_launches",
        "repeated_context_bytes",
        "provider_usage_tokens",
        "usage_limitation",
        "promotion_authorized",
    }
    if (
        not isinstance(result, dict)
        or set(result) != fields
        or result["promotion_authorized"] is not False
    ):
        raise CapsuleIntegrationError("phase result contract changed")
    accepted = _verified_inputs(
        intent_envelope, capsule_plan, acceptance_bundles, allow_historical=_historical_only
    )
    if _historical_only:
        fence = _phase_fence(integration_store, integration_id)
        accepted = _historical_phase_metadata(accepted, fence, custody)
    receipt = integration_store.read(integration_id)
    if receipt["status"] == "passed":
        verify_integration_git(receipt)
    if (
        receipt["status"] != "passed"
        or receipt["candidate"] != result["candidate"]
        or receipt["content_sha256"] != result["integration_receipt_sha256"]
    ):
        raise CapsuleIntegrationError("phase result integration binding changed")
    if [row["candidate"] for row in receipt["inputs"]] != [row["candidate"] for row in accepted]:
        raise CapsuleIntegrationError("phase integration inputs changed")
    candidate = result["candidate"]
    current = capture_launch_repository(Path(receipt["target"]))
    if (current.head_oid, current.tree_oid, current.repository_common_dir_sha256) != (
        candidate["head_oid"],
        candidate["tree_oid"],
        candidate["repository_common_dir_sha256"],
    ):
        raise CapsuleIntegrationError("combined candidate changed after phase review")
    prompt = build_phase_review_prompt(
        intent_envelope=intent_envelope,
        capsule_plan=capsule_plan,
        integration_receipt=receipt,
        accepted=accepted,
        authority_sources=authority_sources,
        check_commands=check_commands,
        previous_plan=previous_plan,
    )
    quorum = intent_envelope["campaign_envelope"]["policy_snapshot"]["policies"]["review"][
        "minimum_approvals"
    ]
    if not isinstance(result["reviews"], list) or len(result["reviews"]) != quorum:
        raise CapsuleIntegrationError("phase review quorum changed")
    policy_sha = canonical_json_sha256(_provider_policy_document(provider_policy))
    if policy_sha not in intent_envelope["campaign_envelope"]["policy_refs"]:
        raise CapsuleIntegrationError("phase provider policy is outside sealed authority")
    sessions = {
        row["session_id"] for bundle in acceptance_bundles for row in bundle["author_receipts"]
    }
    worktrees = {
        str(Path(row["cwd"]).resolve())
        for bundle in acceptance_bundles
        for row in bundle["author_receipts"]
    }
    worktrees.add(str(Path(receipt["target"]).resolve()))
    for review in result["reviews"]:
        if not isinstance(review, dict) or set(review) != {"provider_receipt", "assessment"}:
            raise CapsuleIntegrationError("phase review evidence fields changed")
        provider = review["provider_receipt"]
        custody.validate("phase-review", provider)
        _provider_authority(
            provider,
            provider_policy=provider_policy,
            qualification_index=qualification_index,
            policy_sha256=policy_sha,
            _historical_only=_historical_only,
        )
        _reviewer_live_candidate(provider, candidate, "phase reviewer")
        if provider["session_id"] in sessions or str(Path(provider["cwd"]).resolve()) in worktrees:
            raise CapsuleIntegrationError("phase reviewer independence changed")
        sessions.add(provider["session_id"])
        worktrees.add(str(Path(provider["cwd"]).resolve()))
        if _durable_request(custody, provider) != prompt.encode():
            raise CapsuleIntegrationError("phase reviewer request changed")
        actual = validate_phase_assessment(
            _durable_final_output(custody, provider),
            candidate=candidate,
            invariant_ids=[row["invariant_id"] for row in intent_envelope["invariants"]],
        )
        if actual != review["assessment"]:
            raise CapsuleIntegrationError("phase assessment differs from raw final output")
    passed = all(
        row["assessment"]["goal"]["status"] == "pass"
        and row["assessment"]["material_concept_drift"] == "absent"
        and all(item["status"] == "pass" for item in row["assessment"]["invariants"])
        for row in result["reviews"]
    )
    if result["status"] != ("phase_validated" if passed else "blocked"):
        raise CapsuleIntegrationError("phase completion status differs from actual evidence")
    launches, prompt_bytes, repeated_bytes = _phase_metrics(
        integration_store,
        integration_id,
        prompt,
        quorum,
        canonical_json_sha256(candidate),
    )
    if (
        result["prompt_sha256"] != hashlib.sha256(prompt.encode()).hexdigest()
        or result["prompt_bytes"] != prompt_bytes
        or result["review_launches"] != launches
        or result["provider_usage_tokens"] is not None
    ):
        raise CapsuleIntegrationError("phase administration metrics changed")
    if result["repeated_context_bytes"] != repeated_bytes:
        raise CapsuleIntegrationError("phase repeated context metric changed")
    return result


def _phase_attempt_root(integration_store, integration_id: str, attempt: int) -> Path:
    if type(attempt) is not int or attempt < 1:
        raise CapsuleIntegrationError("phase review attempt is invalid")
    return integration_store.root / f"{integration_id}.phase-review-attempt-{attempt}"


def _phase_attempt_event(
    integration_store,
    integration_id: str,
    attempt: int,
    ordinal: int,
    status: str,
    *,
    prompt_sha256: str,
    candidate_sha256: str,
    provider_receipt_sha256: str | None = None,
) -> None:
    if status not in {"started", "conclusive"} or type(ordinal) is not int or ordinal < 0:
        raise CapsuleIntegrationError("phase review attempt event is invalid")
    value = {
        "schema_version": "1",
        "record_kind": "campaign_phase_review_launch",
        "integration_id": integration_id,
        "attempt": attempt,
        "ordinal": ordinal,
        "status": status,
        "prompt_sha256": prompt_sha256,
        "candidate_sha256": candidate_sha256,
        "provider_receipt_sha256": provider_receipt_sha256,
    }
    if status == "started" and provider_receipt_sha256 is not None:
        raise CapsuleIntegrationError("started phase review event cannot carry a receipt")
    if status == "conclusive" and not isinstance(provider_receipt_sha256, str):
        raise CapsuleIntegrationError("conclusive phase review event lacks its receipt")
    root = _phase_attempt_root(integration_store, integration_id, attempt)
    suffix = "started" if status == "started" else "conclusive"
    _create_blob_only(root / f"{ordinal:020d}-{suffix}.json", (_canonical(value) + "\n").encode())


def _phase_attempt_events(integration_store, integration_id: str, attempt: int | None = None):
    """Read the small per-attempt launch journal used for spend accounting and retry fencing."""

    roots = (
        [_phase_attempt_root(integration_store, integration_id, attempt)]
        if attempt is not None
        else sorted(integration_store.root.glob(f"{integration_id}.phase-review-attempt-*"))
    )
    events = []
    for root in roots:
        if not root.exists():
            continue
        try:
            root_attempt = int(root.name.rsplit("-", 1)[-1])
        except (TypeError, ValueError) as exc:
            raise CapsuleIntegrationError("phase review attempt custody is malformed") from exc
        if root_attempt < 1:
            raise CapsuleIntegrationError("phase review attempt custody is malformed")
        # A launch journal is meaningful only after the matching durable fence exists.
        fence_path = integration_store.root / (
            f"{integration_id}.phase-review-started.json"
            if root_attempt == 1
            else f"{integration_id}.phase-review-started.{root_attempt}.json"
        )
        if not fence_path.is_file() or fence_path.is_symlink():
            raise CapsuleIntegrationError("phase review launch has no matching fence")
        try:
            fence_value = json.loads(fence_path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapsuleIntegrationError("phase review launch fence is unreadable") from exc
        if not isinstance(fence_value, dict) or type(fence_value.get("quorum")) is not int:
            raise CapsuleIntegrationError("phase review launch fence quorum is invalid")
        if root.is_symlink() or not root.is_dir():
            raise CapsuleIntegrationError("phase review attempt custody is not physical")
        for path in sorted(root.glob("*.json")):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
                raise CapsuleIntegrationError("phase review launch custody is unavailable")
            try:
                value = json.loads(path.read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CapsuleIntegrationError("phase review launch custody is invalid") from exc
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "schema_version",
                    "record_kind",
                    "integration_id",
                    "attempt",
                    "ordinal",
                    "status",
                    "prompt_sha256",
                    "candidate_sha256",
                    "provider_receipt_sha256",
                }
                or value.get("schema_version") != "1"
                or value.get("record_kind") != "campaign_phase_review_launch"
                or value.get("integration_id") != integration_id
                or value.get("attempt") != int(root.name.rsplit("-", 1)[-1])
                or type(value.get("ordinal")) is not int
                or value.get("ordinal") < 0
                or value.get("ordinal") >= fence_value["quorum"]
                or value.get("status") not in {"started", "conclusive"}
                or not isinstance(value.get("prompt_sha256"), str)
                or not isinstance(value.get("candidate_sha256"), str)
                or (value["status"] == "started" and value["provider_receipt_sha256"] is not None)
                or (
                    value["status"] == "conclusive"
                    and not isinstance(value["provider_receipt_sha256"], str)
                )
                or not path.name.startswith(f"{value['ordinal']:020d}-")
                or not path.name.endswith(f"{value['status']}.json")
                or _canonical(value) + "\n" != path.read_bytes().decode("utf-8")
            ):
                raise CapsuleIntegrationError("phase review launch custody changed")
            events.append(value)
    seen = set()
    for event in events:
        key = (event["attempt"], event["ordinal"], event["status"])
        if key in seen:
            raise CapsuleIntegrationError("phase review launch custody repeats an ordinal")
        seen.add(key)
    started = {
        (event["attempt"], event["ordinal"]) for event in events if event["status"] == "started"
    }
    if any(
        event["status"] == "conclusive" and (event["attempt"], event["ordinal"]) not in started
        for event in events
    ):
        raise CapsuleIntegrationError("phase review conclusive launch lacks its start event")
    return events


def _phase_metrics(
    integration_store,
    integration_id: str,
    prompt: str,
    reviews_count: int,
    candidate_sha256: str | None = None,
):
    events = _phase_attempt_events(integration_store, integration_id)
    prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
    if any(
        row["prompt_sha256"] != prompt_sha256
        or (candidate_sha256 is not None and row["candidate_sha256"] != candidate_sha256)
        for row in events
    ):
        raise CapsuleIntegrationError("phase review launch differs from its prompt or candidate")
    starts = [row for row in events if row["status"] == "started"]
    launches = max(len(starts), reviews_count)
    rendered = json.loads(prompt)
    context_bytes = sum(
        len(_canonical(rendered[field]).encode())
        for field in ("intent_envelope", "authority_sources")
    )
    return launches, len(prompt.encode()) * launches, context_bytes * launches


def _phase_result(
    *,
    prompt: str,
    receipt: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
    review_launches: int | None = None,
    prompt_bytes: int | None = None,
    repeated_context_bytes: int | None = None,
) -> dict[str, Any]:
    """Build the same closed phase result for a fresh or recovered review attempt."""

    passed = all(
        row["assessment"]["goal"]["status"] == "pass"
        and row["assessment"]["material_concept_drift"] == "absent"
        and all(item["status"] == "pass" for item in row["assessment"]["invariants"])
        for row in reviews
    )
    rendered = json.loads(prompt)
    repeated_bytes = sum(
        len(_canonical(rendered[field]).encode())
        for field in ("intent_envelope", "authority_sources")
    ) * len(reviews)
    launches = len(reviews) if review_launches is None else review_launches
    return {
        "status": "phase_validated" if passed else "blocked",
        "candidate": copy.deepcopy(receipt["candidate"]),
        "integration_receipt_sha256": receipt["content_sha256"],
        "reviews": [copy.deepcopy(dict(row)) for row in reviews],
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_bytes": len(prompt.encode()) * launches if prompt_bytes is None else prompt_bytes,
        "review_launches": launches,
        "repeated_context_bytes": repeated_bytes
        if repeated_context_bytes is None
        else repeated_context_bytes,
        "provider_usage_tokens": None,
        "usage_limitation": "phase provider billing is unavailable; prompt bytes are measured",
        "promotion_authorized": False,
    }


def _phase_fence(integration_store, integration_id: str, attempt: int = 1) -> dict[str, Any]:
    path = integration_store.root / (
        f"{integration_id}.phase-review-started.json"
        if attempt == 1
        else f"{integration_id}.phase-review-started.{attempt}.json"
    )
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
        raise CapsuleIntegrationError("phase review start fence is unavailable")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleIntegrationError("phase review start fence is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "1"
        or value.get("record_kind") != "campaign_phase_review_attempt"
        or value.get("integration_id") != integration_id
        or value.get("attempt") != attempt
        or _canonical(value).encode() != raw
    ):
        raise CapsuleIntegrationError("phase review start fence contract changed")
    return value


def _phase_fence_value(
    *,
    integration_id: str,
    receipt: Mapping[str, Any],
    prompt: str,
    intent_envelope: Mapping[str, Any],
    reviewer_specs: Sequence[Mapping[str, Any]],
    attempt: int,
    retry_of_fence_sha256: str | None,
) -> dict[str, Any]:
    quorum = intent_envelope["campaign_envelope"]["policy_snapshot"]["policies"]["review"][
        "minimum_approvals"
    ]
    if len(reviewer_specs) != quorum:
        raise CapsuleIntegrationError("phase review requires the exact sealed independent quorum")
    return {
        "schema_version": "1",
        "record_kind": "campaign_phase_review_attempt",
        "attempt": attempt,
        "retry_of_fence_sha256": retry_of_fence_sha256,
        "integration_id": integration_id,
        "integration_receipt_sha256": receipt["content_sha256"],
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "acceptance_qualification_manifests": {
            row["capsule_id"]: row["qualification_manifest_sha256s"]
            for row in json.loads(prompt)["accepted_capsules"]
        },
        "candidate": copy.deepcopy(receipt["candidate"]),
        "quorum": quorum,
        "invariant_ids": [row["invariant_id"] for row in intent_envelope["invariants"]],
        "reviewers": [
            {
                "review_worktree": str(Path(spec["review_worktree"]).resolve()),
                "role_name": spec["role_name"],
                "qualified_provider": spec["qualified_provider"].selection_name,
                "adapter": spec["qualified_provider"].adapter,
                "adapter_version": spec["qualified_provider"].adapter_version,
                "settings_sha256": spec["qualified_provider"].settings_sha256,
                "rules_sha256": spec["qualified_provider"].rules_sha256,
                "provider_output_dir": str(_phase_provider_output_dir(spec, attempt)),
            }
            for spec in reviewer_specs
        ],
    }


def _phase_provider_output_dir(spec: Mapping[str, Any], attempt: int) -> Path:
    """Give each retry ordinal fresh provider output custody without changing its sealed role."""

    path = Path(spec["provider_output_dir"]).resolve()
    return path if attempt == 1 else path / f"attempt-{attempt}"


def _phase_receipt_candidates(custody, *, prompt: bytes, candidate: Mapping[str, Any]):
    """Read conclusive provider receipts from the existing custody index only."""

    records = []
    for path in sorted(custody.root.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise CapsuleIntegrationError("phase provider custody is not physical")
        try:
            record = custody._read_record(path)
            receipt_path = Path(record["receipt_path"])
            receipt = validate_provider_receipt(
                json.loads(receipt_path.read_bytes().decode("utf-8"))
            )
        except Exception as exc:
            raise CapsuleIntegrationError("phase provider custody cannot be reopened") from exc
        launch = receipt.get("launch")
        receipt_candidate = receipt.get("candidate")
        if not (
            receipt.get("cwd")
            and isinstance(launch, Mapping)
            and launch.get("prompt_sha256") == hashlib.sha256(prompt).hexdigest()
            and receipt_candidate is not None
            and receipt_candidate.get("repository_common_dir_sha256")
            == candidate["repository_common_dir_sha256"]
            and receipt_candidate.get("head_oid") == candidate["head_oid"]
            and receipt_candidate.get("tree_oid") == candidate["tree_oid"]
        ):
            continue
        try:
            request_prompt = _durable_request(custody, receipt)
        except Exception as exc:
            raise CapsuleIntegrationError(
                "phase reviewer request custody cannot be reopened"
            ) from exc
        if request_prompt != prompt:
            raise CapsuleIntegrationError("phase reviewer request differs from its prompt digest")
        records.append(receipt)
    return records


def _phase_marker(
    integration_store, integration_id: str, fence_attempt: int
) -> dict[str, Any] | None:
    path = integration_store.root / (
        f"{integration_id}.phase-review-recovery.json"
        if fence_attempt == 1
        else f"{integration_id}.phase-review-recovery.{fence_attempt}.json"
    )
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
        raise CapsuleIntegrationError("phase review recovery marker is unavailable")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleIntegrationError("phase review recovery marker is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "record_kind",
            "integration_id",
            "fence_attempt",
            "fence_sha256",
            "next_attempt",
            "disposition",
            "conclusive_receipt_sha256s",
            "uncertain_ordinals",
            "unstarted_ordinals",
            "attempted_review_launches",
            "prompt_bytes",
            "repeated_context_bytes",
        }
        or value["schema_version"] != "1"
        or value["record_kind"] != "campaign_phase_review_recovery"
        or value["integration_id"] != integration_id
        or value["fence_attempt"] != fence_attempt
        or value["next_attempt"] != fence_attempt + 1
        or value["disposition"] != "failed"
        or not isinstance(value["conclusive_receipt_sha256s"], list)
        or not isinstance(value["uncertain_ordinals"], list)
        or not isinstance(value["unstarted_ordinals"], list)
        or not isinstance(value["attempted_review_launches"], int)
        or not isinstance(value["prompt_bytes"], int)
        or not isinstance(value["repeated_context_bytes"], int)
        or _canonical(value).encode() + b"\n" != raw
    ):
        raise CapsuleIntegrationError("phase review recovery marker contract changed")
    return value


def _latest_phase_fence_attempt(integration_store, integration_id: str) -> int:
    base = integration_store.root / f"{integration_id}.phase-review-started.json"
    attempts = [1] if base.exists() else []
    prefix = f"{integration_id}.phase-review-started."
    for path in integration_store.root.glob(f"{prefix}*.json"):
        suffix = path.name[len(prefix) : -len(".json")]
        if suffix.isdigit() and int(suffix) >= 2:
            attempts.append(int(suffix))
    if not attempts:
        raise CapsuleIntegrationError("phase review start fence is unavailable")
    return max(attempts)


def _latest_phase_marker(integration_store, integration_id: str) -> dict[str, Any] | None:
    attempts = []
    base = integration_store.root / f"{integration_id}.phase-review-recovery.json"
    if base.exists():
        attempts.append(1)
    prefix = f"{integration_id}.phase-review-recovery."
    for path in integration_store.root.glob(f"{prefix}*.json"):
        suffix = path.name[len(prefix) : -len(".json")]
        if suffix.isdigit() and int(suffix) >= 2:
            attempts.append(int(suffix))
    if not attempts:
        return None
    return _phase_marker(integration_store, integration_id, max(attempts))


def _phase_snapshot(
    *,
    intent_envelope,
    capsule_plan,
    acceptance_bundles,
    integration_store,
    integration_id,
    authority_sources,
    check_commands,
    reviewer_specs,
    provider_policy,
    qualification_index,
    custody,
    previous_plan=None,
    fence_attempt=1,
    historical_only=False,
):
    """Reopen one phase fence and classify each reviewer ordinal by durable evidence."""

    fence = _phase_fence(integration_store, integration_id, fence_attempt)
    accepted = _verified_inputs(
        intent_envelope, capsule_plan, acceptance_bundles, allow_historical=historical_only
    )
    if historical_only:
        accepted = _historical_phase_metadata(accepted, fence, custody)
    receipt = integration_store.read(integration_id)
    if receipt["status"] == "passed":
        verify_integration_git(receipt)
    if receipt["status"] != "passed":
        raise CapsuleIntegrationError("phase review recovery requires a passed integration")
    prompt = build_phase_review_prompt(
        intent_envelope=intent_envelope,
        capsule_plan=capsule_plan,
        integration_receipt=receipt,
        accepted=accepted,
        authority_sources=authority_sources,
        check_commands=check_commands,
        previous_plan=previous_plan,
    )
    retry_of = (
        None
        if fence_attempt == 1
        else hashlib.sha256(
            _canonical(_phase_fence(integration_store, integration_id, fence_attempt - 1)).encode()
        ).hexdigest()
    )
    expected_fence = _phase_fence_value(
        integration_id=integration_id,
        receipt=receipt,
        prompt=prompt,
        intent_envelope=intent_envelope,
        reviewer_specs=reviewer_specs,
        attempt=fence_attempt,
        retry_of_fence_sha256=retry_of,
    )
    if "acceptance_qualification_manifests" not in fence:
        expected_fence.pop("acceptance_qualification_manifests")
    if fence != expected_fence:
        raise CapsuleIntegrationError("phase review recovery fence differs from sealed custody")
    from bearhug.campaign.capsule_review import (
        _durable_final_output,
        _durable_request,
        _provider_authority,
        _reviewer_live_candidate,
    )

    policy_sha = canonical_json_sha256(_provider_policy_document(provider_policy))
    if policy_sha not in intent_envelope["campaign_envelope"]["policy_refs"]:
        raise CapsuleIntegrationError("phase provider policy is outside sealed authority")
    author_sessions = {
        row["session_id"] for bundle in acceptance_bundles for row in bundle["author_receipts"]
    }
    all_receipts = _phase_receipt_candidates(
        custody, prompt=prompt.encode(), candidate=receipt["candidate"]
    )
    receipt_by_sha = {canonical_json_sha256(provider): provider for provider in all_receipts}
    conclusive_events = {}
    for event in _phase_attempt_events(integration_store, integration_id):
        if event["attempt"] > fence_attempt or event["status"] != "conclusive":
            continue
        prior = conclusive_events.get(event["ordinal"])
        if prior is None or event["attempt"] > prior["attempt"]:
            conclusive_events[event["ordinal"]] = event
    reviews_by_ordinal = {}
    missing = []
    uncertain = set()
    started = {
        row["ordinal"]
        for row in _phase_attempt_events(integration_store, integration_id, fence_attempt)
        if row["status"] == "started"
    }
    used_sessions = set(author_sessions)
    for ordinal, spec in enumerate(reviewer_specs):
        expected_path = str(Path(spec["review_worktree"]).resolve())
        matches = [row for row in all_receipts if row.get("cwd") == expected_path]
        bound_event = conclusive_events.get(ordinal)
        if bound_event is not None:
            provider = receipt_by_sha.get(bound_event["provider_receipt_sha256"])
            if provider is None:
                raise CapsuleIntegrationError(
                    "phase conclusive launch has no matching provider custody"
                )
            matches = [provider]
        elif ordinal not in started:
            # A receipt without a durable launch fence is never eligible for adoption.
            missing.append(ordinal)
            continue
        if not matches or len(matches) > 1:
            # Multiple unbound records can be left by an interrupted retry.  They are
            # preserved as uncertain evidence until a later conclusive launch binds one.
            missing.append(ordinal)
            uncertain.add(ordinal)
            continue
        provider = matches[0]
        try:
            if provider["cwd"] != expected_path:
                raise CapsuleIntegrationError("phase reviewer worktree differs from its ordinal")
            custody.validate("phase-review", provider)
            if not historical_only:
                _provider_authority(
                    provider,
                    provider_policy=provider_policy,
                    qualification_index=qualification_index,
                    policy_sha256=policy_sha,
                )
            else:
                # Historical disposition may inspect sealed identities but cannot accept a
                # phase or select a provider. Live qualification is required again on resume.
                policy_role = provider_policy.roles[spec["role_name"]]
                if (
                    provider["launch"]["sandbox"] != policy_role.sandbox
                    or provider["launch"]["approval_policy"] != policy_role.approval_policy
                    or provider["identity"]["requested_model"] != policy_role.model
                    or provider["identity"]["requested_reasoning_effort"] != policy_role.effort
                    or provider["required_capabilities"] != list(policy_role.required_capabilities)
                ):
                    raise CapsuleIntegrationError("historical reviewer differs from sealed policy")
            qualified = spec["qualified_provider"]
            if (
                provider["role"] != "reviewer"
                or provider["provider"]
                != {"codex": "openai-codex", "claude": "anthropic-claude"}.get(
                    qualified.selection_name
                )
                or provider["adapter"] != qualified.adapter
                or provider["adapter_version"] != qualified.adapter_version
                or provider["launch"]["settings_sha256"] != qualified.settings_sha256
                or provider["launch"]["rules_sha256"] != qualified.rules_sha256
            ):
                raise CapsuleIntegrationError("phase reviewer execution identity differs")
            _reviewer_live_candidate(provider, receipt["candidate"], "phase reviewer")
            if provider["session_id"] in used_sessions:
                raise CapsuleIntegrationError("phase reviewer session is reused")
            used_sessions.add(provider["session_id"])
            if _durable_request(custody, provider) != prompt.encode():
                raise CapsuleIntegrationError("phase reviewer request differs from its fence")
            assessment = validate_phase_assessment(
                _durable_final_output(custody, provider),
                candidate=receipt["candidate"],
                invariant_ids=[row["invariant_id"] for row in intent_envelope["invariants"]],
            )
        except ValueError as exc:
            if bound_event is not None:
                raise CapsuleIntegrationError(
                    "phase conclusive custody is invalid or no longer qualified"
                ) from exc
            # A provider may have completed after its launch event but before the
            # assessment or conclusive launch event was durable.  Keep that receipt
            # in custody, classify the ordinal as uncertain, and require explicit
            # failed recovery before retrying it.
            missing.append(ordinal)
            uncertain.add(ordinal)
            continue
        reviews_by_ordinal[ordinal] = {"provider_receipt": provider, "assessment": assessment}
    launches, prompt_bytes, repeated = _phase_metrics(
        integration_store,
        integration_id,
        prompt,
        len(reviews_by_ordinal),
        canonical_json_sha256(receipt["candidate"]),
    )
    return {
        "fence": fence,
        "fence_attempt": fence_attempt,
        "fence_sha256": hashlib.sha256(_canonical(fence).encode()).hexdigest(),
        "receipt": receipt,
        "prompt": prompt,
        "reviewer_specs": reviewer_specs,
        "reviews_by_ordinal": reviews_by_ordinal,
        "missing": missing,
        "uncertain": sorted(uncertain),
        "unstarted": sorted(set(missing) - uncertain),
        "attempted_review_launches": launches,
        "prompt_bytes": prompt_bytes,
        "repeated_context_bytes": repeated,
    }


def recover_phase_review(
    *,
    intent_envelope,
    capsule_plan,
    acceptance_bundles,
    integration_store,
    integration_id,
    authority_sources,
    check_commands,
    reviewer_specs,
    provider_policy,
    qualification_index,
    custody,
    disposition="blocked",
    previous_plan=None,
    fence_attempt=None,
    historical_only=False,
):
    """Recover a fenced phase review using conclusive custody, without provider relaunches."""

    if disposition not in {"failed", "blocked", "hil_required"}:
        raise CapsuleIntegrationError("phase review recovery disposition is unsupported")
    attempt = (
        _latest_phase_fence_attempt(integration_store, integration_id)
        if fence_attempt is None
        else fence_attempt
    )
    snapshot = _phase_snapshot(
        intent_envelope=intent_envelope,
        capsule_plan=capsule_plan,
        acceptance_bundles=acceptance_bundles,
        integration_store=integration_store,
        integration_id=integration_id,
        authority_sources=authority_sources,
        check_commands=check_commands,
        reviewer_specs=reviewer_specs,
        provider_policy=provider_policy,
        qualification_index=qualification_index,
        custody=custody,
        previous_plan=previous_plan,
        fence_attempt=attempt,
        historical_only=historical_only,
    )
    if snapshot["missing"] or historical_only:
        if disposition != "failed":
            return None
        marker = {
            "schema_version": "1",
            "record_kind": "campaign_phase_review_recovery",
            "integration_id": integration_id,
            "fence_attempt": snapshot["fence_attempt"],
            "fence_sha256": snapshot["fence_sha256"],
            "next_attempt": snapshot["fence_attempt"] + 1,
            "disposition": disposition,
            "conclusive_receipt_sha256s": [
                canonical_json_sha256(row["provider_receipt"])
                for row in snapshot["reviews_by_ordinal"].values()
            ],
            "uncertain_ordinals": snapshot["uncertain"],
            "unstarted_ordinals": snapshot["unstarted"],
            "attempted_review_launches": snapshot["attempted_review_launches"],
            "prompt_bytes": snapshot["prompt_bytes"],
            "repeated_context_bytes": snapshot["repeated_context_bytes"],
        }
        marker_path = integration_store.root / (
            f"{integration_id}.phase-review-recovery.json"
            if snapshot["fence_attempt"] == 1
            else f"{integration_id}.phase-review-recovery.{snapshot['fence_attempt']}.json"
        )
        _create_blob_only(marker_path, (_canonical(marker) + "\n").encode())
        return None
    reviews = [snapshot["reviews_by_ordinal"][index] for index in range(len(reviewer_specs))]
    result = _phase_result(
        prompt=snapshot["prompt"],
        receipt=snapshot["receipt"],
        reviews=reviews,
        review_launches=snapshot["attempted_review_launches"],
        prompt_bytes=snapshot["prompt_bytes"],
        repeated_context_bytes=snapshot["repeated_context_bytes"],
    )
    _create_blob_only(
        integration_store.root / f"{integration_id}.phase-result.json",
        (_canonical(result) + "\n").encode(),
    )
    return result


def resume_phase_review(
    *,
    intent_envelope,
    capsule_plan,
    acceptance_bundles,
    integration_store,
    integration_id,
    authority_sources,
    check_commands,
    reviewer_specs,
    provider_policy,
    qualification_index,
    custody,
    runner=_default_runner,
    timeout_s=28800.0,
    previous_plan=None,
):
    """Consume one explicit failed recovery marker and retry only unresolved reviewer ordinals.

    The previous fence and every conclusive provider receipt remain immutable.  The new numbered
    fence is published before any provider call, so a second interruption can be recovered by the
    same ordinary control without silently authorizing another attempt.
    """

    marker = _latest_phase_marker(integration_store, integration_id)
    if marker is None:
        raise CapsuleIntegrationError(
            "phase review retry requires an explicit failed recovery marker"
        )
    latest_attempt = _latest_phase_fence_attempt(integration_store, integration_id)
    if latest_attempt != marker["fence_attempt"]:
        raise CapsuleIntegrationError("phase review retry fence already exists; recover it first")
    snapshot = _phase_snapshot(
        intent_envelope=intent_envelope,
        capsule_plan=capsule_plan,
        acceptance_bundles=acceptance_bundles,
        integration_store=integration_store,
        integration_id=integration_id,
        authority_sources=authority_sources,
        check_commands=check_commands,
        reviewer_specs=reviewer_specs,
        provider_policy=provider_policy,
        qualification_index=qualification_index,
        custody=custody,
        previous_plan=previous_plan,
        fence_attempt=marker["fence_attempt"],
    )
    if snapshot["fence_sha256"] != marker["fence_sha256"]:
        raise CapsuleIntegrationError("phase review recovery marker binds another fence")
    observed_hashes = {
        canonical_json_sha256(row["provider_receipt"])
        for row in snapshot["reviews_by_ordinal"].values()
    }
    if observed_hashes != set(marker["conclusive_receipt_sha256s"]):
        raise CapsuleIntegrationError("phase recovery conclusive custody differs from its marker")
    if snapshot["uncertain"] != sorted(marker["uncertain_ordinals"]):
        raise CapsuleIntegrationError("phase recovery uncertain ordinals changed")
    if snapshot["unstarted"] != sorted(marker["unstarted_ordinals"]):
        raise CapsuleIntegrationError("phase recovery unstarted ordinals changed")
    if (
        snapshot["attempted_review_launches"] != marker["attempted_review_launches"]
        or snapshot["prompt_bytes"] != marker["prompt_bytes"]
        or snapshot["repeated_context_bytes"] != marker["repeated_context_bytes"]
    ):
        raise CapsuleIntegrationError("phase recovery accounting differs from its marker")
    if not snapshot["missing"]:
        reviews = [snapshot["reviews_by_ordinal"][index] for index in range(len(reviewer_specs))]
        result = _phase_result(
            prompt=snapshot["prompt"],
            receipt=snapshot["receipt"],
            reviews=reviews,
            review_launches=snapshot["attempted_review_launches"],
            prompt_bytes=snapshot["prompt_bytes"],
            repeated_context_bytes=snapshot["repeated_context_bytes"],
        )
        _create_blob_only(
            integration_store.root / f"{integration_id}.phase-result.json",
            (_canonical(result) + "\n").encode(),
        )
        return result
    result = review_integrated_phase(
        intent_envelope=intent_envelope,
        capsule_plan=capsule_plan,
        acceptance_bundles=acceptance_bundles,
        integration_store=integration_store,
        integration_id=integration_id,
        authority_sources=authority_sources,
        check_commands=check_commands,
        reviewer_specs=reviewer_specs,
        provider_policy=provider_policy,
        custody=custody,
        previous_plan=previous_plan,
        runner=runner,
        timeout_s=timeout_s,
        _attempt=marker["next_attempt"],
        _resume_reviews=snapshot["reviews_by_ordinal"],
        _resume_ordinals=snapshot["missing"],
        _retry_of_fence_sha256=marker["fence_sha256"],
    )
    return result
