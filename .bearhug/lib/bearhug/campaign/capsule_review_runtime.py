"""EC-05 review execution for one exact capsule candidate.

The capsule runtime owns the candidate and its lifecycle; this module owns the small adapter that
turns that candidate into an independent, qualified read-only provider review.  Review packets
and proof verification may be supplied by the capsule review/proof module, while this boundary
keeps provider launch and custody on the existing reviewer wrapper.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.campaign.capsules import CANONICAL_ALGORITHM
from bearhug.campaign.review import worktree_sha256
from bearhug.campaign.reviewer import (
    CampaignReviewerResult,
    _default_runner,
    run_candidate_reviewer,
)
from bearhug.providers.custody import ProviderCustodyError
from bearhug.providers.final_output import strict_final_json
from bearhug.providers.receipt import ProviderReceiptError, capture_launch_repository

_CLAUDE_ADAPTER = "claude-code-stream-json"

_logger = logging.getLogger(__name__)


class CapsuleReviewRuntimeError(RuntimeError):
    """A capsule review cannot establish independent exact-candidate custody."""


@dataclass(frozen=True, slots=True)
class CapsuleReviewResult:
    review_id: str
    packet_sha256: str
    candidate: dict[str, Any]
    provider_receipt: dict[str, Any]
    verdict: str
    findings: list[dict[str, str]]
    review_record: dict[str, Any]
    review_record_sha256: str
    reviewer_worktree: Path
    review_packet: dict[str, Any] | None = None


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CapsuleReviewRuntimeError(f"review record is not canonical JSON: {exc}") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).rstrip(b"\n")).hexdigest()


def _candidate(runtime: Any) -> dict[str, Any]:
    receipts = runtime._state.get("provider_receipts") or []
    if not receipts:
        raise CapsuleReviewRuntimeError("capsule has no provider receipt chain to review")
    try:
        runtime._validate_receipt_chain_custody(receipts)
        candidate, _paths = runtime._candidate(receipts)
    except Exception as exc:
        raise CapsuleReviewRuntimeError(f"capsule candidate custody is unavailable: {exc}") from exc
    if candidate is None:
        raise CapsuleReviewRuntimeError("capsule has no clean cumulative candidate")
    return copy.deepcopy(candidate)


_HEX_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _select_author_receipt(runtime: Any, candidate: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Select the author receipt for the episode that produced the candidate under review, or
    ``None`` when none of this chain's own receipts produced it (not comparable -- see
    ``_extension_comparison_not_applicable``; this is not an error, there is simply nothing of
    this chain's own to compare the reviewed candidate against).

    Selection scans every author receipt in the chain for one whose own candidate carries the
    same commit as the candidate under review. Nothing in this codebase's recorded state names
    which episode produced a given candidate more directly than the chain itself, so more than
    one match selects the LAST matching receipt in chain order, not a raise: a workspace-write
    author turn that revalidates without committing anything new still produces a candidate
    whose commit equals the base, so an ordinary multi-episode capsule (one episode commits the
    work, a later one reports ready with no new commit) legitimately has more than one receipt
    sharing that commit. The last one in chain order is, by construction, the episode
    contemporaneous with the review under way -- the same convention already used elsewhere in
    this codebase for "the receipt that produced this candidate". A genuine repair/rerun
    ambiguity (two receipts for two different commits) cannot reach this function at all: only
    receipts whose own commit equals the reviewed candidate are ever collected here.
    """

    candidate_head = candidate.get("head_oid")
    matches = [
        receipt
        for receipt in (runtime._state.get("provider_receipts") or [])
        if isinstance(receipt, Mapping)
        and isinstance(receipt.get("candidate"), Mapping)
        and receipt["candidate"].get("head_oid") == candidate_head
    ]
    return matches[-1] if matches else None


def _extension_comparison_not_applicable(receipt: Mapping[str, Any]) -> bool:
    """True when the author-versus-reviewer extension comparison must skip this turn's receipt
    rather than compare it: its adapter is not the one this comparison understands, or its
    operational-evidence link is not itself comparable -- a linked record that is not eligible,
    or no record was ever linked for a recorded, understood reason (the quiet "evidence
    unavailable" class: an environmental condition or a provider-shape surprise, which leaves
    the receipt's own evidence link absent and its own blockers already naming why).

    In every skip case the receipt is already ineligible through its own blocker, so acceptance
    cannot happen from this turn regardless of what this comparison does -- a parity check must
    never cost an honest unit. Any other missing or unreadable evidence link is not a skip: it
    is an internal inconsistency and is raised where the record is actually resolved, not here.
    """

    if receipt.get("adapter") != _CLAUDE_ADAPTER:
        return True
    blockers = receipt.get("promotion_blockers") or []
    if "operational_evidence_not_eligible" in blockers:
        return True
    return (
        receipt.get("operational_evidence_sha256") is None
        and "provider_effective_sources_not_attested" in blockers
    )


def _extension_comparison_skip_reason(receipt: Mapping[str, Any]) -> str:
    """A short, human-readable reason for a skip ``_extension_comparison_not_applicable`` already
    decided -- for the log line only, never itself part of the skip decision.
    """

    if receipt.get("adapter") != _CLAUDE_ADAPTER:
        return "its adapter is not the one this comparison understands"
    blockers = receipt.get("promotion_blockers") or []
    if "operational_evidence_not_eligible" in blockers:
        return "its linked operational evidence record is not eligible"
    return "it has no linked operational evidence and its own blockers already say why"


def _log_extension_comparison_skip(*, side: str, reason: str) -> None:
    """Record a skip where a reviewer of the run can see it, without changing any durable or
    schema-validated record: this comparison persists nothing of its own on any outcome
    (compared-and-agreed, or skipped), so a log line is the only channel available. Emitted at
    warning level so it surfaces by default even with no logging configured.
    """

    _logger.warning(
        "author-versus-reviewer extension comparison skipped: side=%s reason=%s", side, reason
    )


def _author_operational_evidence_for_review(
    runtime: Any, candidate: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Resolve the author's operational evidence record for the extension comparison, or
    ``None`` when the comparison must skip the author's turn (see
    ``_extension_comparison_not_applicable``). Any other missing or unreadable record -- the
    receipt claims a link that custody cannot resolve -- is an internal inconsistency and
    raises: a runner that omits its settings/rules paths must not be able to make the
    comparison disappear by simply never linking evidence at all.
    """

    author_receipt = _select_author_receipt(runtime, candidate)
    if author_receipt is None:
        _log_extension_comparison_skip(
            side="author", reason="no receipt in this chain produced the reviewed candidate"
        )
        return None
    if _extension_comparison_not_applicable(author_receipt):
        _log_extension_comparison_skip(
            side="author", reason=_extension_comparison_skip_reason(author_receipt)
        )
        return None
    if runtime.custody is None:
        raise CapsuleReviewRuntimeError("capsule review requires provider receipt custody")
    try:
        return runtime.custody.read_operational_evidence(author_receipt)
    except ProviderCustodyError as exc:
        raise CapsuleReviewRuntimeError(
            f"author operational evidence is unavailable for comparison: {exc}"
        ) from exc


def _hex_digest_or_raise(value: Any, *, what: str, side: str) -> str:
    """A well-formed lowercase SHA-256 hex digest, or a named raise -- never a silent pass.

    Reached only once both sides of the comparison are already known to be comparable (neither
    receipt was skipped), so a missing or malformed operand here is an internal inconsistency
    between two records that both claim to be eligible, never a reason to compare two absent
    values as equal.
    """

    if not isinstance(value, str) or _HEX_DIGEST_PATTERN.fullmatch(value) is None:
        raise CapsuleReviewRuntimeError(
            f"{side} {what} is missing or not a lowercase SHA-256 hex digest: {value!r}"
        )
    return value


def _inventory_digest(evidence: Mapping[str, Any], *, side: str) -> str:
    session_init = evidence.get("session_init")
    value = session_init.get("inventory_sha256") if isinstance(session_init, Mapping) else None
    return _hex_digest_or_raise(value, what="extension inventory digest", side=side)


def _plugin_tree_digests(evidence: Mapping[str, Any], *, side: str) -> list[str]:
    # A built-in plugin (`claude_operational_evidence.py`'s `_extension_sources`: `path` exactly
    # `"builtin"`, `name` non-empty, `source` exactly `f"{name}@builtin"`, e.g. Claude Code
    # 2.1.282's `agents-md`/`telemetry`) has no source tree -- `tree_sha256` is always `None`, on
    # both sides, because the plugin's code ships inside the Claude executable rather than a
    # directory Bear Hug walks. The executable's attested digest binds that code; it does not
    # bind what the plugin reads at runtime -- `agents-md`'s hooks can
    # attach a project's own AGENTS.md files as context, which is why Bear Hug pins it off at
    # launch, `providers/claude.py`). The built-in is still counted in `_inventory_digest`'s
    # name-and-version comparison just above -- only the tree-digest half is skipped here.
    digests = [
        _hex_digest_or_raise(source.get("tree_sha256"), what="plugin tree digest", side=side)
        for source in (evidence.get("extension_sources") or [])
        if isinstance(source, Mapping)
        and source.get("kind") == "plugin"
        and source.get("builtin") is not True
    ]
    return sorted(digests)


def _check_extension_inventory_agreement(
    runtime: Any,
    candidate: Mapping[str, Any],
    reviewer_receipt: Mapping[str, Any],
    reviewer_evidence: Any,
) -> None:
    """Compare the author's and the reviewer's provider-reported extension inventories: the
    name-and-version inventory digest (re-derived on both sides) and the sorted set of plugin
    directory tree digests (each side's own assertion). A divergence in either raises -- a
    raise-or-pass runtime signal, nothing persisted. Both sides skip independently and
    symmetrically when their own receipt is not comparable (see
    ``_extension_comparison_not_applicable``); a receipt that IS comparable but whose evidence
    is missing, unreadable or malformed always raises, never passes vacuously.
    """

    author_evidence = _author_operational_evidence_for_review(runtime, candidate)
    if author_evidence is None:
        return
    if _extension_comparison_not_applicable(reviewer_receipt):
        _log_extension_comparison_skip(
            side="reviewer", reason=_extension_comparison_skip_reason(reviewer_receipt)
        )
        return
    if not isinstance(reviewer_evidence, Mapping):
        raise CapsuleReviewRuntimeError(
            "capsule review has no operational evidence to compare against the author turn"
        )
    author_inventory = _inventory_digest(author_evidence, side="author")
    reviewer_inventory = _inventory_digest(reviewer_evidence, side="reviewer")
    if author_inventory != reviewer_inventory:
        raise CapsuleReviewRuntimeError(
            "author and reviewer extension inventory digests diverge: "
            f"author={author_inventory!r} reviewer={reviewer_inventory!r}"
        )
    author_trees = _plugin_tree_digests(author_evidence, side="author")
    reviewer_trees = _plugin_tree_digests(reviewer_evidence, side="reviewer")
    if author_trees != reviewer_trees:
        raise CapsuleReviewRuntimeError(
            "author and reviewer plugin tree digests diverge: "
            f"author={author_trees!r} reviewer={reviewer_trees!r}"
        )


def build_capsule_review_prompt(
    runtime: Any,
    candidate: Mapping[str, Any],
    *,
    packet_sha256: str | None = None,
    packet: Mapping[str, Any] | None = None,
    extra_context: Mapping[str, Any] | None = None,
) -> str:
    """Build bounded review input from semantic context and exact candidate evidence."""

    from bearhug.campaign.capsule_review import render_capsule_review_prompt

    if extra_context:
        raise CapsuleReviewRuntimeError("select evidence in the sealed review packet")
    if packet is None:
        packet = runtime._read_json_blob(packet_sha256, "review packet")
    if packet["candidate"] != candidate:
        raise CapsuleReviewRuntimeError("review packet candidate differs")
    return render_capsule_review_prompt(packet, dependency_base=runtime.dependency_base)


def _make_worktree(runtime: Any, candidate: Mapping[str, Any], review_id: str) -> Path:
    target = (
        runtime.root / "review-worktrees" / hashlib.sha256(review_id.encode("utf-8")).hexdigest()
    )
    if target.exists() or target.is_symlink():
        raise CapsuleReviewRuntimeError(f"review worktree already exists: {target}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(runtime.worktree),
                "worktree",
                "add",
                "--detach",
                "-q",
                str(target),
                candidate["head_oid"],
            ),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CapsuleReviewRuntimeError(
            f"cannot create independent review worktree: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise CapsuleReviewRuntimeError(f"cannot create independent review worktree: {detail}")
    try:
        launch = capture_launch_repository(target)
    except (ProviderReceiptError, OSError) as exc:
        raise CapsuleReviewRuntimeError(f"review worktree custody is unavailable: {exc}") from exc
    if (
        launch.repository_common_dir_sha256 != candidate["repository_common_dir_sha256"]
        or launch.head_oid != candidate["head_oid"]
        or launch.tree_oid != candidate["tree_oid"]
        or not launch.clean
        or target == runtime.worktree
    ):
        raise CapsuleReviewRuntimeError("review worktree does not bind the exact candidate")
    return target


def _finding_discovery(finding: Mapping[str, str]) -> dict[str, Any]:
    from bearhug.campaign.reconciliation import classify_discovery

    return classify_discovery(
        {"kind": finding.get("kind", "concept_drift"), "summary": finding["summary"]},
        actor="reviewer",
    )


def _review_quorum(runtime: Any) -> int:
    snapshot = runtime.intent.get("campaign_envelope", {}).get("policy_snapshot", {})
    policies = snapshot.get("policies", {}) if isinstance(snapshot, Mapping) else {}
    review = policies.get("review", {}) if isinstance(policies, Mapping) else {}
    configured = review.get("minimum_approvals") if isinstance(review, Mapping) else None
    if type(configured) is not int or configured < 1:
        return 1
    return configured


def _round_approvals(runtime: Any, *, candidate_sha256: str, packet_sha256: str) -> int:
    """Count durable clean approvals for one exact candidate and packet."""

    approvals = 0
    for row in runtime._state.get("acceptance_bundles") or []:
        if (
            row.get("candidate_sha256") != candidate_sha256
            or row.get("packet_sha256") != packet_sha256
        ):
            continue
        try:
            review = runtime._read_json_blob(row["review_record_sha256"], "review record")
        except Exception as exc:
            raise CapsuleReviewRuntimeError(
                "prior review record is unavailable for quorum accounting"
            ) from exc
        if review.get("verdict") == "approve" and review.get("findings") == []:
            approvals += 1
    return approvals


def _ensure_unique_reviewer(runtime: Any, receipt: Mapping[str, Any], worktree: Path) -> None:
    """Reject a reviewer session or checkout reused by a previous durable review."""

    session = receipt.get("session_id")
    if not isinstance(session, str) or not session:
        raise CapsuleReviewRuntimeError("reviewer receipt has no session identity")
    checkout = worktree_sha256(worktree)
    for previous in (runtime._state.get("provider_receipts") or []) + (
        runtime._state.get("reviewer_receipts") or []
    ):
        if previous.get("session_id") == session:
            raise CapsuleReviewRuntimeError("reviewer session was reused")
        if worktree_sha256(previous.get("cwd", "")) == checkout:
            raise CapsuleReviewRuntimeError("reviewer worktree was reused")


def _compile_review_packet(
    runtime: Any, candidate: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    """Compile and custody the strict EC-05 packet when the packet/proof module is available."""

    from bearhug.campaign.capsule_review import (
        build_capsule_review_packet,
        capsule_review_packet_sha256,
    )

    reconciliation = runtime.reconciliation
    if reconciliation is None:
        raise CapsuleReviewRuntimeError("capsule review requires recorded reconciliation evidence")
    authority_sources = []
    authority_ids = {row["source_id"] for row in runtime.intent["authority_refs"]}
    for source_id, row in runtime.source_contents.items():
        if source_id not in authority_ids:
            continue
        content = runtime._source_content(row)
        if content:
            authority_sources.append({"source_id": source_id, "content": content})
    capsule = runtime.capsule
    coverage = runtime._state.get("obligation_coverage") or [
        {
            "source_id": row["source_id"],
            "obligation_id": row["obligation_id"],
            "status": "unavailable",
            "evidence_refs": [],
        }
        for row in capsule["obligation_coverage"]
    ]
    invariant_rows = runtime._state.get("invariants") or None
    validation = runtime._state.get("validation") or [
        {"profile_id": row["profile_id"], "status": "unavailable", "evidence_refs": []}
        for row in capsule["validation_profiles"]
    ]
    raw_discoveries = runtime._state.get("discoveries") or []
    discoveries = [
        item
        if isinstance(item, Mapping)
        else {
            "kind": "observation",
            "summary": str(item),
            "evidence_refs": [],
        }
        for item in raw_discoveries
    ]
    validation_evidence = None
    validation_digest = runtime._state.get("validation_receipt_sha256")
    if validation_digest:
        from bearhug.campaign.capsule_validation import verify_capsule_validation

        record = verify_capsule_validation(
            state_root=runtime._blobs,
            receipt_sha256=validation_digest,
            candidate_worktree=runtime.worktree,
            candidate=candidate,
            profiles=capsule["validation_profiles"],
        )
        validation_evidence = {
            "receipt_sha256": validation_digest,
            "record": record,
            "artifact_refs": runtime._state.get("validation_artifact_refs", {}),
        }
    packet = build_capsule_review_packet(
        intent_envelope=runtime.intent,
        capsule_plan=runtime.plan,
        capsule=capsule,
        candidate=dict(candidate),
        validation=validation,
        validation_evidence=validation_evidence,
        previous_plan=runtime._predecessor_plan(),
        reconciliation=reconciliation,
        obligation_coverage=coverage,
        invariants=invariant_rows,
        authority_sources=authority_sources,
        discoveries=discoveries,
        dependency_base=runtime.dependency_base,
        packet_id=(
            f"review-packet.{runtime.capsule_id}.{runtime._state.get('review_rounds', 0) + 1}"
        ),
    )
    digest = capsule_review_packet_sha256(packet, dependency_base=runtime.dependency_base)
    stored_digest = runtime._put_blob(_canonical(packet).rstrip(b"\n"))
    if stored_digest != digest:
        raise CapsuleReviewRuntimeError("review packet custody digest differs from its bytes")
    return packet, digest


def run_capsule_review(
    runtime: Any,
    *,
    review_id: str | None = None,
    packet_sha256: str | None = None,
    review_role_name: str = "reviewer",
    reviewer_worktree: Path | str | None = None,
    prompt: str | None = None,
    provider_runner: Callable[..., Any] | None = None,
    provider_output_dir: Path | str | None = None,
    run_validator: Callable[[Any], object] | None = None,
    decision_reader: Callable[[Any], dict[str, Any]] = strict_final_json,
    review_packet: Mapping[str, Any] | None = None,
) -> CapsuleReviewResult:
    """Review the current cumulative candidate and persist a content-bound review record."""

    if decision_reader is not strict_final_json:
        raise CapsuleReviewRuntimeError("native review decisions must come from durable raw output")
    if runtime.state != "candidate_ready" and not (
        runtime.state == "failed"
        and runtime._state.get("last_failed_stage") == "review"
        and runtime._state.get("active_review") is None
    ):
        raise CapsuleReviewRuntimeError(f"capsule state {runtime.state!r} is not reviewable")
    if (
        runtime._state.get("active_episode") is not None
        or runtime._state.get("active_review") is not None
    ):
        raise CapsuleReviewRuntimeError("capsule has an unresolved spend boundary")
    candidate = _candidate(runtime)
    result = runtime.result
    if result is None or result.get("candidate") != {
        key: candidate[key] for key in ("base_oid", "head_oid", "tree_oid", "patch_sha256", "clean")
    }:
        raise CapsuleReviewRuntimeError(
            "capsule result does not bind the current cumulative candidate"
        )
    candidate_sha256 = _digest(candidate)
    round_candidate = runtime._state.get("review_round_candidate_sha256")
    round_packet = runtime._state.get("review_round_packet_sha256")
    same_round = round_candidate == candidate_sha256 and isinstance(round_packet, str)
    if same_round:
        # A quorum review must consume the exact packet sealed by the first reviewer.  Rebuilding
        # it would produce a new packet identity and incorrectly turn a second approval into a
        # rereview of another round.
        if packet_sha256 is not None and packet_sha256 != round_packet:
            raise CapsuleReviewRuntimeError("quorum review packet differs from the sealed round")
        if review_packet is not None:
            try:
                from bearhug.campaign.capsule_review import capsule_review_packet_sha256

                supplied_packet_sha256 = capsule_review_packet_sha256(
                    review_packet, dependency_base=runtime.dependency_base
                )
            except ImportError:
                supplied_packet_sha256 = _digest(review_packet)
            if supplied_packet_sha256 != round_packet:
                raise CapsuleReviewRuntimeError(
                    "quorum review packet differs from the sealed round"
                )
        packet_sha256 = round_packet
        review_packet = runtime._read_json_blob(packet_sha256, "review packet")
    elif review_packet is None and packet_sha256 is None:
        review_packet, packet_sha256 = _compile_review_packet(runtime, candidate)
    elif review_packet is not None:
        review_packet = copy.deepcopy(dict(review_packet))
        try:
            from bearhug.campaign.capsule_review import capsule_review_packet_sha256

            packet_sha256 = capsule_review_packet_sha256(
                review_packet, dependency_base=runtime.dependency_base
            )
        except ImportError:
            packet_sha256 = _digest(review_packet)
        runtime._put_blob(_canonical(review_packet).rstrip(b"\n"))
    if not isinstance(packet_sha256, str):
        raise CapsuleReviewRuntimeError("capsule review requires the exact review packet")
    next_round = int(runtime._state.get("review_rounds", 0)) + (0 if same_round else 1)
    launch_number = int(runtime._state.get("review_launches", 0)) + 1
    review_id = review_id or f"review.{runtime.capsule_id}.{next_round}.{launch_number}"
    if not isinstance(review_id, str) or not review_id:
        raise CapsuleReviewRuntimeError("review_id must be non-empty")
    maximum_repairs = runtime.intent["campaign_envelope"]["risk"].get("max_repair_episodes")
    if not same_round and type(maximum_repairs) is int and next_round > maximum_repairs + 1:
        runtime._transition(
            "capsule_blocked",
            evidence_refs=(packet_sha256,),
            state="blocked",
            last_outcome="blocked",
            next_action="approved review and repair budget is exhausted",
        )
        raise CapsuleReviewRuntimeError("approved review/rereview budget is exhausted")
    quorum = _review_quorum(runtime)
    attempt_limit = runtime.intent["campaign_envelope"]["policy_snapshot"]["policies"]["dispatch"][
        "attempt_limit"
    ]
    if (
        type(maximum_repairs) is int
        and launch_number > quorum * (maximum_repairs + 1) * attempt_limit
    ):
        raise CapsuleReviewRuntimeError("approved aggregate review launch budget is exhausted")
    if (
        same_round
        and _round_approvals(
            runtime, candidate_sha256=candidate_sha256, packet_sha256=packet_sha256
        )
        >= quorum
    ):
        raise CapsuleReviewRuntimeError(
            "current candidate already has its independent review quorum"
        )
    expected_prompt = build_capsule_review_prompt(
        runtime,
        candidate,
        packet_sha256=packet_sha256,
        packet=review_packet,
    )
    if prompt is None:
        prompt = expected_prompt
    elif prompt != expected_prompt:
        raise CapsuleReviewRuntimeError(
            "capsule review prompt must carry the exact persisted packet bytes"
        )
    if not isinstance(prompt, str) or not prompt.strip():
        raise CapsuleReviewRuntimeError("capsule review prompt must be non-empty")
    worktree = (
        Path(reviewer_worktree).expanduser().resolve()
        if reviewer_worktree is not None
        else _make_worktree(runtime, candidate, review_id)
    )
    previous_worktrees = {
        Path(row["cwd"]).resolve()
        for row in (runtime._state.get("provider_receipts") or [])
        + (runtime._state.get("reviewer_receipts") or [])
    }
    if worktree.resolve() in previous_worktrees:
        raise CapsuleReviewRuntimeError("review requires a fresh independent worktree")
    if provider_output_dir is None:
        if runtime.provider_output_root is None:
            raise CapsuleReviewRuntimeError("capsule review requires provider output custody")
        provider_output_dir = (
            runtime.provider_output_root
            / "reviews"
            / hashlib.sha256(review_id.encode("utf-8")).hexdigest()
        )
    if runtime.provider_policy is None or runtime.qualification_index is None:
        raise CapsuleReviewRuntimeError("capsule review requires provider policy qualification")
    try:
        role = runtime.provider_policy.roles[review_role_name]
    except (AttributeError, KeyError) as exc:
        raise CapsuleReviewRuntimeError(
            f"capsule review role {review_role_name!r} is unavailable"
        ) from exc
    selected = runtime.qualification_index.require(role.provider)
    selected.revalidate_runtime_files()
    if role.sandbox != "read-only":
        raise CapsuleReviewRuntimeError("review role must be read-only")
    try:
        # This transition is the review spend fence. The runtime keeps no author session alive or
        # reuses the author worktree; every rereview gets a fresh provider invocation and checkout.
        runtime._transition(
            "review_started",
            evidence_refs=(packet_sha256,),
            state="reviewing",
            active_review={
                "review_id": review_id,
                "packet_sha256": packet_sha256,
                "candidate": copy.deepcopy(candidate),
                "round": next_round,
                "spent": True,
            },
            review_round_candidate_sha256=(candidate_sha256 if not same_round else round_candidate),
            review_round_packet_sha256=(packet_sha256 if not same_round else round_packet),
            review_launches=launch_number,
            review_rounds=next_round,
            review_prompt_bytes=runtime._state.get("review_prompt_bytes", 0)
            + len(prompt.encode("utf-8")),
            prompt_bytes=runtime._state.get("prompt_bytes", 0) + len(prompt.encode("utf-8")),
            repeated_context_bytes=runtime._state.get("repeated_context_bytes", 0)
            + sum(
                len(_canonical(review_packet[field]).rstrip(b"\n"))
                for field in ("intent", "authority_sources")
            ),
        )
        validator = run_validator
        custody = runtime.custody
        if custody is None:
            raise CapsuleReviewRuntimeError("capsule review requires provider receipt custody")

        def custody_then_validate(run: Any) -> object:
            custody.record_run(run)
            if validator is not None:
                return validator(run)
            return object()

        stop = threading.Event()
        heartbeat_errors: list[CapsuleReviewRuntimeError] = []

        def heartbeat_loop() -> None:
            interval = max(0.05, min(runtime.lease_ttl_seconds / 3.0, 30.0))
            while not stop.wait(interval):
                try:
                    runtime._heartbeat()
                except CapsuleRuntimeError as exc:
                    heartbeat_errors.append(exc)
                    return

        # A qualified provider turn can outlive the lease's initial TTL.  Keep the same lease
        # fence alive while the independent reviewer is running.
        from bearhug.campaign.capsule_runtime import CapsuleRuntimeError

        heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        try:
            reviewed: CampaignReviewerResult = run_candidate_reviewer(
                candidate=candidate,
                review_worktree=worktree,
                prompt=prompt,
                provider_policy=runtime.provider_policy,
                role_name=review_role_name,
                qualified_provider=selected,
                provider_output_dir=provider_output_dir,
                receipt_role="reviewer",
                timeout_s=runtime._provider_timeout(),
                runner=provider_runner or runtime.provider_runner or _default_runner,
                run_validator=custody_then_validate,
                decision_reader=None,
                settings_sha256=selected.settings_sha256,
                rules_sha256=selected.rules_sha256,
            )
        finally:
            stop.set()
            heartbeat_thread.join(timeout=1.0)
        if heartbeat_errors:
            raise heartbeat_errors[0]
        runtime._heartbeat()
        after_candidate = _candidate(runtime)
        if after_candidate != candidate:
            raise CapsuleReviewRuntimeError("capsule candidate changed during review")
        # The author-versus-reviewer extension comparison: placed inside this try so a genuine
        # divergence gets the same failed/review transition as every other review-runtime
        # failure below, not a durable "unresolved review spend" state with no failure row.
        # Nothing here is persisted, and nothing it reads depends on the verdict parsed below.
        _check_extension_inventory_agreement(
            runtime,
            candidate,
            reviewed.provider_receipt,
            getattr(reviewed.provider_run, "operational_evidence", None),
        )
    except Exception:
        # A transport exception is not proof that the reviewer process or its remote operation
        # ended.  Preserve active_review; explicit review recovery is the only clearing boundary.
        runtime._transition(
            "episode_completed",
            evidence_refs=(packet_sha256,),
            state="failed",
            last_outcome="failed",
            last_failed_stage="review",
            next_action="recover the unresolved review spend before any retry",
        )
        raise
    from bearhug.campaign.capsule_review import validate_capsule_review_decision

    verdict, findings = validate_capsule_review_decision(strict_final_json(reviewed.provider_run))
    provider_receipt = reviewed.provider_receipt
    _ensure_unique_reviewer(runtime, provider_receipt, worktree)
    provider_digest = _digest(provider_receipt)
    record: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "capsule_review_receipt",
        "canonical_algorithm": CANONICAL_ALGORITHM,
        "review_id": review_id,
        "reviewer_receipt_sha256": provider_digest,
        "reviewer_session_id": provider_receipt["session_id"],
        "reviewer_worktree_sha256": worktree_sha256(worktree),
        "packet_sha256": packet_sha256,
        "candidate_sha256": candidate_sha256,
        "verdict": verdict,
        "findings": copy.deepcopy(findings),
        "evidence_refs": [provider_digest],
        # The extra sealed identities make this an inspectable capsule record while the proof
        # builder can consume the same mapping as its compact reviewer assessment.
        "intent_envelope_sha256": runtime.intent_sha256,
        "plan_sha256": runtime.plan_sha256,
        "revision_id": runtime.revision_id,
        "capsule_id": runtime.capsule_id,
        "candidate": copy.deepcopy(candidate),
    }
    record_digest = runtime._put_blob(_canonical(record))
    refs = list(runtime._state.get("review_refs") or []) + [record_digest]
    acceptance_bundle = {
        "review_id": review_id,
        "packet_sha256": packet_sha256,
        "review_record_sha256": record_digest,
        "reviewer_receipt_sha256": provider_digest,
        "candidate_sha256": candidate_sha256,
    }
    states = list(runtime._state.get("review_findings") or [])
    severity_map = {"major": "high", "minor": "medium", "info": "nit"}
    states.extend(
        {
            "finding_id": finding["finding_id"],
            "severity": severity_map.get(finding["severity"], finding["severity"]),
            "summary": finding["summary"],
            "state": "open",
            "evidence_refs": [record_digest],
        }
        for finding in findings
    )
    conceptual = [
        finding
        for finding in findings
        if _finding_discovery(finding)["correction_class"] != "local_correction"
    ]
    quorum = _review_quorum(runtime)
    approvals = _round_approvals(
        runtime, candidate_sha256=candidate_sha256, packet_sha256=packet_sha256
    )
    is_clean_approval = verdict == "approve" and not findings
    quorum_complete = is_clean_approval and approvals + 1 >= quorum
    if is_clean_approval:
        event = "review_accepted"
        state = "candidate_ready"
        outcome = "candidate_ready"
        next_action = (
            "submit the independently reviewed candidate for result verification"
            if quorum_complete
            else f"obtain {quorum - approvals - 1} more independent approval(s) for this candidate"
        )
        # A fresh approved round resolves findings from the immediately preceding local repair.
        # Keep their evidence and identity in the cumulative result while closing their state.
        states = [
            {**row, "state": "resolved" if row.get("state") == "open" else row.get("state")}
            for row in states
        ]
    elif conceptual:
        event = "review_finding"
        state = "reconciling"
        outcome = "reconciliation_required"
        next_action = "resolve the conceptual review finding through reconciliation/HIL"
    else:
        event = "review_finding"
        state = "locally_repairing"
        outcome = "local_repair_required"
        next_action = "run a bounded repair episode in this capsule, then independently rereview"
    runtime._transition(
        event,
        evidence_refs=(record_digest, packet_sha256, provider_digest),
        state=state,
        active_review=None,
        last_failed_stage=None,
        last_outcome=outcome,
        next_action=next_action,
        review_refs=refs,
        acceptance_bundles=list(runtime._state.get("acceptance_bundles") or [])
        + [acceptance_bundle],
        review_findings=states,
        reviewer_receipts=list(runtime._state.get("reviewer_receipts") or []) + [provider_receipt],
        validation=[] if outcome != "candidate_ready" else runtime._state.get("validation", []),
        result_sha256=None if outcome != "candidate_ready" else runtime._state.get("result_sha256"),
    )
    if outcome == "candidate_ready":
        runtime._publish_result(candidate, (), [record_digest, packet_sha256, provider_digest])
    if conceptual:
        runtime.reconcile(
            {
                "intent": {
                    "intent_envelope_sha256": runtime.intent_sha256,
                    "plan_sha256": runtime.plan_sha256,
                }
            },
            discoveries=[
                {
                    "kind": finding.get("kind", "concept_drift"),
                    "summary": finding["summary"],
                    "evidence_refs": [record_digest],
                }
                for finding in conceptual
            ],
            evidence_refs=(record_digest, provider_digest),
            actor="reviewer",
        )
    return CapsuleReviewResult(
        review_id,
        packet_sha256,
        candidate,
        provider_receipt,
        verdict,
        findings,
        record,
        record_digest,
        worktree,
        copy.deepcopy(review_packet),
    )


__all__ = [
    "CapsuleReviewResult",
    "CapsuleReviewRuntimeError",
    "build_capsule_review_prompt",
    "run_capsule_review",
]
