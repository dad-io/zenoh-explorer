"""Bounded capsule review packets and fail-closed acceptance proof.

The v1 campaign reviewer binds one provider turn to one author candidate.  A native capsule can
span several author episodes, so its acceptance boundary needs a small independent record that
binds the cumulative candidate, the sealed plan, the bounded review context, and every independent
review assessment.  This module deliberately has no provider-launch or integration side effects.

``build_capsule_review_packet`` is a deterministic projection over already sealed records and
explicit observations.  ``verify_capsule_acceptance`` is the authority at the capsule boundary:
it reopens provider custody, recomputes the cumulative Git candidate, checks live reviewer
worktrees, and evaluates the declared obligations.  A boolean supplied by a caller is never used
as acceptance evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bearhug.campaign.capsule_candidate import (
    capture_capsule_candidate,
    validate_dependency_base,
)
from bearhug.campaign.capsule_packets import PacketSource
from bearhug.campaign.capsules import (
    CANONICAL_ALGORITHM,
    CapsuleContractError,
    _canonical_capsule,
    validate_capsule_plan,
    validate_intent_envelope,
)
from bearhug.campaign.reconciliation import (
    ReconciliationError,
    validate_reconciliation_record,
)
from bearhug.campaign.review import (
    CampaignReviewError,
    canonical_json_sha256,
    validate_review_receipt,
    worktree_sha256,
)
from bearhug.providers.final_output import ProviderFinalOutputError, strict_final_json
from bearhug.providers.receipt import (
    LaunchRepository,
    ProviderReceiptError,
    capture_launch_repository,
    validate_provider_receipt,
)


class CapsuleAcceptanceError(ValueError):
    """A capsule packet, proof, or custody chain cannot establish acceptance."""


CapsuleReviewError = CapsuleAcceptanceError

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOWER_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

_PACKET_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "canonical_algorithm",
        "packet_id",
        "intent_envelope_sha256",
        "plan_sha256",
        "revision_id",
        "capsule_id",
        "intent",
        "capsule_plan",
        "capsule",
        "candidate",
        "authority_sources",
        "bindings",
        "invariants",
        "obligation_coverage",
        "validation",
        "validation_evidence",
        "reconciliation",
        "discoveries",
        "selected_episode_evidence",
    }
)
_PROOF_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "canonical_algorithm",
        "proof_id",
        "capsule_id",
        "revision_id",
        "intent_envelope_sha256",
        "plan_sha256",
        "candidate",
        "packet_sha256",
        "policy_sha256",
        "required_quorum",
        "author_receipt_sha256s",
        "reviewer_assessments",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "repository_common_dir_sha256",
        "base_oid",
        "head_oid",
        "tree_oid",
        "patch_sha256",
        "clean",
    }
)
_ASSESSMENT_FIELDS = frozenset(
    {
        "review_id",
        "reviewer_receipt_sha256",
        "reviewer_session_id",
        "reviewer_worktree_sha256",
        "packet_sha256",
        "candidate_sha256",
        "verdict",
        "findings",
        "evidence_refs",
    }
)
_FINDING_FIELDS = frozenset({"finding_id", "severity", "summary"})
_EVIDENCE_FIELDS = frozenset({"evidence_ref", "reason"})
_AUTHORITY_SOURCE_FIELDS = frozenset({"source_id", "content_sha256", "content"})
_CAPSULE_REVIEW_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "canonical_algorithm",
        "review_id",
        "reviewer_receipt_sha256",
        "reviewer_session_id",
        "reviewer_worktree_sha256",
        "packet_sha256",
        "candidate_sha256",
        "verdict",
        "findings",
        "evidence_refs",
    }
)
_CAPSULE_REVIEW_FIELDS_ENRICHED = _CAPSULE_REVIEW_FIELDS | frozenset(
    {"intent_envelope_sha256", "plan_sha256", "revision_id", "capsule_id", "candidate"}
)
_VALIDATION_EVIDENCE_FIELDS = frozenset({"receipt_sha256", "record", "artifact_refs"})
_VALIDATION_RECORD_FIELDS = frozenset(
    {
        "record_kind",
        "candidate",
        "profiles",
        "commands",
        "status",
    }
)
_VALIDATION_COMMAND_FIELDS = frozenset(
    {
        "command_ref",
        "argv",
        "status",
        "returncode",
        "stdout_sha256",
        "stderr_sha256",
    }
)
_MAX_PACKET_BYTES = 256 * 1024 * 1024
_MAX_PROOF_BYTES = 64 * 1024 * 1024
# One reviewer finding, and one reason for selecting an evidence ref. Measured 2026-09-17
# on row 240 T2: a correct rejection was discarded whole because its blocker finding ran
# to 1348 characters against a 1000 bound. The finding named the missing proof, quoted the
# three lines of shell that made the episode's excuse wrong, and said what to run instead.
# Nothing in it was padding. `docs/schemas/campaign-review-receipt.v1.schema.json` carries
# the same bound and moves with this one. See the operating limits note in README.md.
_MAX_FINDING_TEXT = 1_000_000
_ACCEPTED_RECONCILIATION_STATUSES = {"consistent", "changed"}
_PASS_STATES = {"pass", "match"}


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CapsuleAcceptanceError(f"value is not canonical JSON: {exc}") from exc


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CapsuleAcceptanceError(f"{where} must be lowercase SHA-256")
    return value


def _oid(value: Any, where: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise CapsuleAcceptanceError(f"{where} must be a full Git object id")
    return value


def _token(value: Any, where: str, *, lower: bool = False) -> str:
    pattern = _LOWER_TOKEN if lower else _TOKEN
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise CapsuleAcceptanceError(f"{where} must be a canonical identifier")
    return value


def _unique_strings(value: Any, where: str, *, nonempty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise CapsuleAcceptanceError(f"{where} must be a unique string array")
    return list(value)


def _candidate(value: Any, where: str = "candidate") -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CANDIDATE_FIELDS:
        raise CapsuleAcceptanceError(f"{where} is not a closed cumulative candidate")
    result = dict(value)
    _sha(result["repository_common_dir_sha256"], f"{where}.repository_common_dir_sha256")
    for field in ("base_oid", "head_oid", "tree_oid"):
        _oid(result[field], f"{where}.{field}")
    _sha(result["patch_sha256"], f"{where}.patch_sha256")
    if result["clean"] is not True:
        raise CapsuleAcceptanceError(f"{where} must be clean")
    return result


def _checked_dependency_base(
    value: Mapping[str, Any] | None,
    *,
    plan: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Validate the selected base against the sealed subject and current candidate identity."""

    if value is None:
        return None
    try:
        checked = validate_dependency_base(value)
    except CapsuleContractError as exc:
        raise CapsuleAcceptanceError(f"dependency base is invalid: {exc}") from exc
    candidate_base = candidate.get("base_oid") if isinstance(candidate, Mapping) else None
    if checked["base_oid"] != candidate_base:
        raise CapsuleAcceptanceError("dependency base does not match the candidate base")
    return checked


def _evidence_refs(value: Any, where: str, *, nonempty: bool = False) -> list[str]:
    refs = _unique_strings(value, where, nonempty=nonempty)
    for ref in refs:
        _sha(ref, f"{where} reference")
    return sorted(refs)


def _copy_rows(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CapsuleAcceptanceError(f"{where} must be an array")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise CapsuleAcceptanceError(f"{where}[{index}] must be an object")
        rows.append(copy.deepcopy(dict(row)))
    return rows


def _capsule_for(
    capsule_plan: Mapping[str, Any], capsule: Mapping[str, Any] | None, capsule_id: str | None
) -> dict[str, Any]:
    if not isinstance(capsule_plan, Mapping):
        raise CapsuleAcceptanceError("capsule plan must be an object")
    selected = dict(capsule) if capsule is not None else None
    if selected is None:
        if not isinstance(capsule_id, str):
            raise CapsuleAcceptanceError("capsule or capsule_id is required")
        selected = next(
            (
                copy.deepcopy(row)
                for row in capsule_plan.get("capsules", ())
                if isinstance(row, Mapping) and row.get("capsule_id") == capsule_id
            ),
            None,
        )
    if selected is None:
        raise CapsuleAcceptanceError("capsule is absent from the sealed plan")
    selected_id = selected.get("capsule_id")
    if not isinstance(selected_id, str):
        raise CapsuleAcceptanceError("capsule has no identity")
    expected = next(
        (
            row
            for row in capsule_plan.get("capsules", ())
            if isinstance(row, Mapping) and row.get("capsule_id") == selected_id
        ),
        None,
    )
    if expected is None:
        raise CapsuleAcceptanceError("capsule is absent from the sealed plan")
    try:
        if _canonical_capsule(expected) != _canonical_capsule(selected):
            raise CapsuleAcceptanceError("capsule differs from the sealed plan")
    except (KeyError, TypeError, ValueError) as exc:
        raise CapsuleAcceptanceError("capsule cannot be canonically compared") from exc
    return selected


def _validate_sealed(
    intent_envelope: Mapping[str, Any],
    capsule_plan: Mapping[str, Any],
    capsule: Mapping[str, Any] | None = None,
    *,
    previous_plan: Mapping[str, Any] | None = None,
    record_only: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]:
    try:
        intent = copy.deepcopy(dict(intent_envelope))
        plan = copy.deepcopy(dict(capsule_plan))
        intent_result = validate_intent_envelope(intent)
        plan_result = (
            validate_capsule_plan(plan)
            if record_only
            else validate_capsule_plan(
                plan,
                intent_envelope=intent,
                previous_plan=None if previous_plan is None else dict(previous_plan),
            )
        )
    except Exception as exc:
        raise CapsuleAcceptanceError(f"sealed intent or plan is invalid: {exc}") from exc
    selected = _capsule_for(plan, capsule, None)
    return intent, plan, selected, intent_result.digest, plan_result.digest


def _normalise_selected_evidence(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CapsuleAcceptanceError("selected_episode_evidence must be an array")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if isinstance(item, str):
            ref, reason = item, "explicitly selected episode evidence"
        elif isinstance(item, Mapping):
            if set(item) != _EVIDENCE_FIELDS:
                raise CapsuleAcceptanceError(f"selected_episode_evidence[{index}] is not closed")
            ref, reason = item["evidence_ref"], item["reason"]
        else:
            raise CapsuleAcceptanceError(f"selected_episode_evidence[{index}] is malformed")
        _sha(ref, f"selected_episode_evidence[{index}].evidence_ref")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > _MAX_FINDING_TEXT
        ):
            raise CapsuleAcceptanceError(f"selected_episode_evidence[{index}].reason is invalid")
        if ref in seen:
            raise CapsuleAcceptanceError("selected_episode_evidence contains duplicate evidence")
        seen.add(ref)
        rows.append({"evidence_ref": ref, "reason": reason})
    return sorted(rows, key=lambda row: row["evidence_ref"])


def _normalise_authority_sources(
    intent: Mapping[str, Any],
    capsule: Mapping[str, Any],
    value: Sequence[Mapping[str, Any] | PacketSource] | Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Carry the mandatory P0 authority bytes into the bounded review context.

    Authority digests alone are deliberately insufficient context.  Source bytes are supplied by
    the caller, checked against the sealed ``authority_refs`` digest, decoded as UTF-8, and then
    included in the packet.  The review packet does not read a path or choose a "latest" source.
    """

    if value is None:
        raise CapsuleAcceptanceError("missing P0 authority source bytes")
    if isinstance(value, Mapping):
        value = [
            row if isinstance(row, Mapping) else {"source_id": source_id, "content": row}
            for source_id, row in value.items()
        ]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CapsuleAcceptanceError("authority_sources must be an array")
    expected = {row["source_id"]: row["content_sha256"] for row in intent["authority_refs"]}
    if len(value) != len(expected):
        raise CapsuleAcceptanceError("authority_sources must include every sealed P0 authority")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    for index, source in enumerate(value):
        if isinstance(source, PacketSource):
            source = {
                "source_id": source.source_id,
                "content": source.content,
                "source_sha256": source.source_sha256,
            }
        if not isinstance(source, Mapping) or not {"source_id", "content"} <= set(source):
            raise CapsuleAcceptanceError(
                f"authority_sources[{index}] must contain source_id and explicit content"
            )
        source_id = source["source_id"]
        if not isinstance(source_id, str) or source_id not in expected or source_id in seen:
            raise CapsuleAcceptanceError(f"authority_sources[{index}] is not a sealed authority")
        raw = source["content"]
        if isinstance(raw, str):
            content = raw.encode("utf-8")
        elif isinstance(raw, bytes):
            content = raw
        else:
            raise CapsuleAcceptanceError(f"authority_sources[{index}].content must be UTF-8 bytes")
        supplied_digest = source.get("source_sha256")
        if supplied_digest is not None and supplied_digest != hashlib.sha256(content).hexdigest():
            raise CapsuleAcceptanceError(
                f"authority_sources[{index}] supplied digest differs from its content"
            )
        if not content:
            raise CapsuleAcceptanceError(f"authority_sources[{index}].content is empty")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CapsuleAcceptanceError(
                f"authority_sources[{index}].content must be UTF-8"
            ) from exc
        observed = hashlib.sha256(content).hexdigest()
        if observed != expected[source_id]:
            raise CapsuleAcceptanceError(
                f"authority_sources[{index}] digest differs from sealed authority"
            )
        seen.add(source_id)
        total_bytes += len(content)
        rows.append({"source_id": source_id, "content_sha256": observed, "content": text})
    if seen != set(expected):
        raise CapsuleAcceptanceError("authority_sources omit a sealed P0 authority")
    budget = capsule.get("context_budget", {})
    max_bytes = budget.get("max_bytes") if isinstance(budget, Mapping) else None
    reserve_bytes = budget.get("reserve_bytes", 0) if isinstance(budget, Mapping) else 0
    if (
        type(max_bytes) is int
        and type(reserve_bytes) is int
        and total_bytes > max_bytes - reserve_bytes
    ):
        raise CapsuleAcceptanceError("P0 authority and stable charter exceed sealed context budget")
    return sorted(rows, key=lambda row: row["source_id"])


def _normalise_packet_rows(
    *,
    capsule: Mapping[str, Any],
    obligation_coverage: Sequence[Mapping[str, Any]],
    validation: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected_obligations = {
        (row["source_id"], row["obligation_id"]) for row in capsule["obligation_coverage"]
    }
    coverage = _copy_rows(obligation_coverage, "obligation_coverage")
    observed_obligations: set[tuple[Any, Any]] = set()
    for index, row in enumerate(coverage):
        if set(row) != {"source_id", "obligation_id", "status", "evidence_refs"}:
            raise CapsuleAcceptanceError(f"obligation_coverage[{index}] is not closed")
        key = (row["source_id"], row["obligation_id"])
        if key in observed_obligations:
            raise CapsuleAcceptanceError("obligation_coverage contains duplicate rows")
        observed_obligations.add(key)
        _token(row["source_id"], f"obligation_coverage[{index}].source_id", lower=True)
        _token(row["obligation_id"], f"obligation_coverage[{index}].obligation_id", lower=True)
        if row["status"] not in {"pass", "fail", "unavailable"}:
            raise CapsuleAcceptanceError(f"obligation_coverage[{index}].status is unsupported")
        row["evidence_refs"] = _evidence_refs(
            row["evidence_refs"],
            f"obligation_coverage[{index}].evidence_refs",
            nonempty=row["status"] == "pass",
        )
    if observed_obligations != expected_obligations:
        raise CapsuleAcceptanceError("obligation_coverage does not cover the sealed capsule")

    expected_profiles = {row["profile_id"] for row in capsule["validation_profiles"]}
    validation_rows = _copy_rows(validation, "validation")
    observed_profiles: set[Any] = set()
    for index, row in enumerate(validation_rows):
        if set(row) != {"profile_id", "status", "evidence_refs"}:
            raise CapsuleAcceptanceError(f"validation[{index}] is not closed")
        profile_id = _token(row["profile_id"], f"validation[{index}].profile_id", lower=True)
        if profile_id in observed_profiles:
            raise CapsuleAcceptanceError("validation contains duplicate profiles")
        observed_profiles.add(profile_id)
        if row["status"] not in {"pass", "fail", "unavailable"}:
            raise CapsuleAcceptanceError(f"validation[{index}].status is unsupported")
        row["evidence_refs"] = _evidence_refs(
            row["evidence_refs"],
            f"validation[{index}].evidence_refs",
            nonempty=row["status"] == "pass",
        )
    if observed_profiles != expected_profiles:
        raise CapsuleAcceptanceError("validation does not cover every declared profile")
    return (
        sorted(coverage, key=lambda row: (row["source_id"], row["obligation_id"])),
        sorted(validation_rows, key=lambda row: row["profile_id"]),
    )


def _normalise_bindings(
    intent: Mapping[str, Any], capsule: Mapping[str, Any], value: Any
) -> list[dict[str, Any]]:
    """Require the packet's binding projection to equal the sealed relevant bindings."""

    rows = _copy_rows(value, "bindings")
    expected = {
        row["binding_id"]: row
        for row in intent["bindings"]
        if row["binding_id"] in capsule["binding_refs"]
    }
    if set(expected) != set(capsule["binding_refs"]):
        raise CapsuleAcceptanceError("capsule references an absent sealed binding")
    observed: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row.get("binding_id"), str) or row["binding_id"] in observed:
            raise CapsuleAcceptanceError(f"bindings[{index}] has a duplicate or missing identity")
        binding_id = row["binding_id"]
        if binding_id not in expected:
            raise CapsuleAcceptanceError(f"bindings[{index}] is not relevant sealed authority")
        if _canonical(dict(row)) != _canonical(dict(expected[binding_id])):
            raise CapsuleAcceptanceError(f"bindings[{index}] differs from sealed authority")
        observed[binding_id] = row
    if set(observed) != set(expected):
        raise CapsuleAcceptanceError("bindings omit a relevant sealed binding")
    return [copy.deepcopy(observed[key]) for key in sorted(observed)]


def _normalise_validation_evidence(
    value: Mapping[str, Any] | None,
    *,
    candidate: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate the bounded command projection used by reviewers and acceptance."""

    if value is None:
        return {"receipt_sha256": None, "record": None, "artifact_refs": {}}
    if not isinstance(value, Mapping) or set(value) != _VALIDATION_EVIDENCE_FIELDS:
        raise CapsuleAcceptanceError("validation_evidence is not a closed receipt projection")
    receipt_sha256 = value["receipt_sha256"]
    record = value["record"]
    artifact_refs = value["artifact_refs"]
    # Packet construction may deliberately leave validation unavailable.  Keep
    # that state explicit and closed; acceptance later requires the real
    # capsule_validation receipt and will never promote this sentinel.
    if receipt_sha256 is None and record is None and artifact_refs == {}:
        return {"receipt_sha256": None, "record": None, "artifact_refs": {}}
    if receipt_sha256 is None or record is None:
        raise CapsuleAcceptanceError("validation_evidence must be complete or unavailable")
    if not isinstance(receipt_sha256, str) or _SHA256.fullmatch(receipt_sha256) is None:
        raise CapsuleAcceptanceError("validation_evidence.receipt_sha256 is invalid")
    if not isinstance(record, Mapping) or set(record) != _VALIDATION_RECORD_FIELDS:
        raise CapsuleAcceptanceError("validation_evidence.record is not closed")
    if not isinstance(artifact_refs, Mapping) or any(
        not isinstance(key, str) or not isinstance(digest, str)
        for key, digest in artifact_refs.items()
    ):
        raise CapsuleAcceptanceError("validation_evidence.artifact_refs is invalid")
    checked_artifacts: dict[str, str] = {}
    for key, digest in artifact_refs.items():
        _token(key, f"validation_evidence.artifact_refs/{key}", lower=True)
        _sha(digest, f"validation_evidence.artifact_refs/{key}")
        checked_artifacts[key] = digest
    if record["record_kind"] != "capsule_validation":
        raise CapsuleAcceptanceError("validation_evidence.record kind is unsupported")
    if dict(record["candidate"]) != dict(candidate):
        raise CapsuleAcceptanceError("validation_evidence binds a different candidate")
    if record["profiles"] != list(profiles):
        raise CapsuleAcceptanceError("validation_evidence binds different validation profiles")
    if record["status"] not in {"pass", "fail"}:
        raise CapsuleAcceptanceError("validation_evidence.record status is unsupported")
    commands = record["commands"]
    if not isinstance(commands, list) or not commands:
        raise CapsuleAcceptanceError("validation_evidence.record needs command evidence")
    command_refs: set[str] = set()
    for index, command in enumerate(commands):
        if not isinstance(command, Mapping) or set(command) != _VALIDATION_COMMAND_FIELDS:
            raise CapsuleAcceptanceError(f"validation_evidence.commands[{index}] is not closed")
        ref = command["command_ref"]
        _token(ref, f"validation_evidence.commands[{index}].command_ref", lower=True)
        if ref in command_refs:
            raise CapsuleAcceptanceError("validation_evidence repeats a command ref")
        command_refs.add(ref)
        argv = command["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv)
        ):
            raise CapsuleAcceptanceError(f"validation_evidence.commands[{index}].argv is invalid")
        if command["status"] not in {"passed", "failed", "timeout", "launch_error"}:
            raise CapsuleAcceptanceError(f"validation_evidence.commands[{index}].status is invalid")
        if command["returncode"] is not None and type(command["returncode"]) is not int:
            raise CapsuleAcceptanceError(
                f"validation_evidence.commands[{index}].returncode is invalid"
            )
        for stream in ("stdout_sha256", "stderr_sha256"):
            _sha(command[stream], f"validation_evidence.commands[{index}].{stream}")
    expected_refs = {ref for profile in profiles for ref in profile["command_refs"]}
    if command_refs != expected_refs:
        raise CapsuleAcceptanceError("validation_evidence does not cover all command refs")
    if canonical_json_sha256(dict(record)) != receipt_sha256:
        raise CapsuleAcceptanceError("validation_evidence receipt digest differs from record")
    output_digests = {
        command[f"{stream}_sha256"] for command in commands for stream in ("stdout", "stderr")
    }
    if any(digest not in output_digests for digest in checked_artifacts.values()):
        raise CapsuleAcceptanceError(
            "validation_evidence artifact is not one of the verified command outputs"
        )
    return {
        "receipt_sha256": receipt_sha256,
        "record": copy.deepcopy(dict(record)),
        "artifact_refs": checked_artifacts,
    }


def _normalise_invariants(
    capsule: Mapping[str, Any], value: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows = _copy_rows(value, "invariants")
    expected = set(capsule["invariant_refs"])
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if set(row) != {"invariant_id", "status", "evidence_refs"}:
            raise CapsuleAcceptanceError(f"invariants[{index}] is not closed")
        invariant_id = _token(row["invariant_id"], f"invariants[{index}].invariant_id", lower=True)
        if invariant_id in seen:
            raise CapsuleAcceptanceError("invariants contains duplicate rows")
        seen.add(invariant_id)
        if row["status"] not in {"pass", "fail", "unavailable"}:
            raise CapsuleAcceptanceError(f"invariants[{index}].status is unsupported")
        row["evidence_refs"] = _evidence_refs(
            row["evidence_refs"],
            f"invariants[{index}].evidence_refs",
            nonempty=row["status"] == "pass",
        )
    if seen != expected:
        raise CapsuleAcceptanceError("invariants do not cover the sealed capsule")
    return sorted(rows, key=lambda row: row["invariant_id"])


def build_capsule_review_packet(
    *,
    intent_envelope: Mapping[str, Any],
    capsule_plan: Mapping[str, Any],
    capsule: Mapping[str, Any],
    candidate: Mapping[str, Any],
    validation: Sequence[Mapping[str, Any]],
    reconciliation: Mapping[str, Any],
    obligation_coverage: Sequence[Mapping[str, Any]],
    invariants: Sequence[Mapping[str, Any]] | None = None,
    validation_evidence: Mapping[str, Any] | None = None,
    authority_sources: (
        Sequence[Mapping[str, Any] | PacketSource] | Mapping[str, Any] | None
    ) = None,
    discoveries: Sequence[Mapping[str, Any]] | None = None,
    selected_episode_evidence: Sequence[str | Mapping[str, str]] = (),
    packet_id: str | None = None,
    previous_plan: Mapping[str, Any] | None = None,
    dependency_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a deterministic bounded review projection from sealed records and observations."""

    intent, plan, selected, intent_sha256, plan_sha256 = _validate_sealed(
        intent_envelope, capsule_plan, capsule, previous_plan=previous_plan
    )
    packet_id = packet_id or f"review-packet.{selected['capsule_id']}"
    _token(packet_id, "packet_id")
    checked_dependency = _checked_dependency_base(
        dependency_base,
        plan=plan,
        candidate=candidate,
    )
    checked_candidate = _candidate(candidate)
    expected_base = (
        checked_dependency["base_oid"]
        if checked_dependency is not None
        else plan["subject"]["base_oid"]
    )
    if checked_candidate["base_oid"] != expected_base:
        raise CapsuleAcceptanceError("candidate base is not the selected capsule base")
    try:
        validate_reconciliation_record(reconciliation)
    except ReconciliationError as exc:
        raise CapsuleAcceptanceError(f"reconciliation evidence is invalid: {exc}") from exc
    if (
        reconciliation["intent_envelope_sha256"] != intent_sha256
        or reconciliation["plan_sha256"] != plan_sha256
        or reconciliation["revision_id"] != plan["revision"]["revision_id"]
        or reconciliation["capsule_id"] != selected["capsule_id"]
    ):
        raise CapsuleAcceptanceError("reconciliation evidence does not bind the sealed capsule")
    coverage, validations = _normalise_packet_rows(
        capsule=selected,
        obligation_coverage=obligation_coverage,
        validation=validation,
    )
    # EC-04 stores invariant observations under its mechanical projection.  Convert those
    # comparisons into the compact completion rows unless the runtime supplies explicit rows
    # (which is useful when a validation runner has stronger evidence than reconciliation alone).
    if invariants is None:
        mechanical = reconciliation["mechanical"]["invariants"]
        invariant_rows = []
        for invariant_id in sorted(selected["invariant_refs"]):
            row = mechanical.get(invariant_id)
            status = {
                "match": "pass",
                "conflict": "fail",
                "changed": "fail",
                "unavailable": "unavailable",
            }[row["status"]]
            invariant_rows.append(
                {
                    "invariant_id": invariant_id,
                    "status": status,
                    "evidence_refs": row["evidence_refs"],
                }
            )
        invariants = invariant_rows
    invariants = _normalise_invariants(selected, invariants)
    authority = _normalise_authority_sources(intent, selected, authority_sources)
    declared_discoveries = (
        reconciliation.get("discoveries", ()) if discoveries is None else discoveries
    )
    discovery_rows = _copy_rows(declared_discoveries, "discoveries")
    selected_evidence = _normalise_selected_evidence(selected_episode_evidence)
    bindings = _normalise_bindings(
        intent,
        selected,
        [
            copy.deepcopy(row)
            for row in intent["bindings"]
            if row["binding_id"] in selected["binding_refs"]
        ],
    )
    checked_validation_evidence = _normalise_validation_evidence(
        validation_evidence,
        candidate=checked_candidate,
        profiles=selected["validation_profiles"],
    )
    packet = {
        "schema_version": "1",
        "record_kind": "capsule_review_packet",
        "canonical_algorithm": CANONICAL_ALGORITHM,
        "packet_id": packet_id,
        "intent_envelope_sha256": intent_sha256,
        "plan_sha256": plan_sha256,
        "revision_id": plan["revision"]["revision_id"],
        "capsule_id": selected["capsule_id"],
        "intent": intent,
        "capsule_plan": plan,
        "capsule": selected,
        "candidate": checked_candidate,
        "authority_sources": authority,
        "bindings": bindings,
        "invariants": invariants,
        "obligation_coverage": coverage,
        "validation": validations,
        "validation_evidence": checked_validation_evidence,
        "reconciliation": copy.deepcopy(dict(reconciliation)),
        "discoveries": discovery_rows,
        "selected_episode_evidence": selected_evidence,
    }
    validate_capsule_review_packet(packet, dependency_base=checked_dependency)
    if len(_canonical(packet)) > _MAX_PACKET_BYTES:
        raise CapsuleAcceptanceError("review packet exceeds its byte bound")
    return packet


def validate_capsule_review_packet(
    value: Any,
    *,
    dependency_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one closed packet and return it without mutating the caller's value."""

    if not isinstance(value, Mapping) or set(value) != _PACKET_FIELDS:
        raise CapsuleAcceptanceError("review packet has missing or unknown fields")
    packet = copy.deepcopy(dict(value))
    if (
        packet["schema_version"] != "1"
        or packet["record_kind"] != "capsule_review_packet"
        or packet["canonical_algorithm"] != CANONICAL_ALGORITHM
    ):
        raise CapsuleAcceptanceError("unsupported review packet identity")
    _token(packet["packet_id"], "packet_id")
    _sha(packet["intent_envelope_sha256"], "intent_envelope_sha256")
    _sha(packet["plan_sha256"], "plan_sha256")
    _token(packet["revision_id"], "revision_id", lower=True)
    _token(packet["capsule_id"], "capsule_id", lower=True)
    intent, plan, capsule, intent_sha256, plan_sha256 = _validate_sealed(
        packet["intent"], packet["capsule_plan"], packet["capsule"], record_only=True
    )
    if packet["intent_envelope_sha256"] != intent_sha256 or packet["plan_sha256"] != plan_sha256:
        raise CapsuleAcceptanceError("review packet sealed record digest mismatch")
    if packet["intent"]["intent_envelope_id"] != intent["intent_envelope_id"]:
        raise CapsuleAcceptanceError("review packet intent is malformed")
    if (
        packet["capsule_id"] != capsule["capsule_id"]
        or packet["revision_id"] != plan["revision"]["revision_id"]
    ):
        raise CapsuleAcceptanceError("review packet capsule identity mismatch")
    checked_dependency = _checked_dependency_base(
        dependency_base,
        plan=plan,
        candidate=packet["candidate"],
    )
    checked_candidate = _candidate(packet["candidate"])
    expected_base = (
        checked_dependency["base_oid"]
        if checked_dependency is not None
        else plan["subject"]["base_oid"]
    )
    if checked_candidate["base_oid"] != expected_base:
        raise CapsuleAcceptanceError(
            "review packet candidate is not based on the selected capsule base"
        )
    authority_sources = packet["authority_sources"]
    if not isinstance(authority_sources, list):
        raise CapsuleAcceptanceError("authority_sources must be an array")
    expected_authority = {
        row["source_id"]: row["content_sha256"] for row in intent["authority_refs"]
    }
    if len(authority_sources) != len(expected_authority):
        raise CapsuleAcceptanceError("review packet is missing mandatory P0 authority")
    observed_authority: set[str] = set()
    total_authority_bytes = 0
    for index, row in enumerate(authority_sources):
        if not isinstance(row, Mapping) or set(row) != _AUTHORITY_SOURCE_FIELDS:
            raise CapsuleAcceptanceError(f"authority_sources[{index}] is not closed")
        source_id = row["source_id"]
        if source_id not in expected_authority or source_id in observed_authority:
            raise CapsuleAcceptanceError(f"authority_sources[{index}] is not sealed")
        if not isinstance(row["content"], str) or not row["content"]:
            raise CapsuleAcceptanceError(f"authority_sources[{index}].content is invalid")
        observed_digest = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
        if (
            row["content_sha256"] != observed_digest
            or observed_digest != expected_authority[source_id]
        ):
            raise CapsuleAcceptanceError(
                f"authority_sources[{index}] digest differs from sealed authority"
            )
        observed_authority.add(source_id)
        total_authority_bytes += len(row["content"].encode("utf-8"))
    if observed_authority != set(expected_authority):
        raise CapsuleAcceptanceError("review packet omits mandatory P0 authority")
    budget = capsule.get("context_budget", {})
    if isinstance(budget, Mapping) and type(budget.get("max_bytes")) is int:
        reserve = budget.get("reserve_bytes", 0)
        if type(reserve) is not int or total_authority_bytes > budget["max_bytes"] - reserve:
            raise CapsuleAcceptanceError(
                "P0 authority and stable charter exceed sealed context budget"
            )
    _normalise_bindings(packet["intent"], packet["capsule"], packet["bindings"])
    _normalise_invariants(packet["capsule"], packet["invariants"])
    _normalise_packet_rows(
        capsule=packet["capsule"],
        obligation_coverage=packet["obligation_coverage"],
        validation=packet["validation"],
    )
    _normalise_validation_evidence(
        packet["validation_evidence"],
        candidate=checked_candidate,
        profiles=packet["capsule"]["validation_profiles"],
    )
    try:
        validate_reconciliation_record(packet["reconciliation"])
    except ReconciliationError as exc:
        raise CapsuleAcceptanceError(f"review packet reconciliation is invalid: {exc}") from exc
    if (
        packet["reconciliation"]["intent_envelope_sha256"] != packet["intent_envelope_sha256"]
        or packet["reconciliation"]["plan_sha256"] != packet["plan_sha256"]
        or packet["reconciliation"]["revision_id"] != packet["revision_id"]
        or packet["reconciliation"]["capsule_id"] != packet["capsule_id"]
    ):
        raise CapsuleAcceptanceError("review packet reconciliation capsule mismatch")
    _copy_rows(packet["discoveries"], "discoveries")
    _normalise_selected_evidence(packet["selected_episode_evidence"])
    if len(_canonical(packet)) > _MAX_PACKET_BYTES:
        raise CapsuleAcceptanceError("review packet exceeds its byte bound")
    return value if isinstance(value, dict) else packet


def capsule_review_packet_sha256(
    packet: Mapping[str, Any],
    *,
    dependency_base: Mapping[str, Any] | None = None,
) -> str:
    """Return the digest of a validated packet."""

    checked = validate_capsule_review_packet(packet, dependency_base=dependency_base)
    return canonical_json_sha256(checked)


# Bear Hug deliberately counts an approval only when the findings list is empty
# (`is_clean_approval` and `_round_approvals` in capsule_review_runtime.py; the acceptance proof
# verifier's "review quorum contains a rejecting or blocking assessment" check in this module). A
# real capsule campaign's independent reviewer approved a correct, in-scope, validated candidate
# but reported three confirmations as `severity: "info"` findings, so the approval could not be
# accepted. The instruction below never said a finding of any severity blocks acceptance or that
# an approval must carry an empty list, so an honest, thorough reviewer had no way to know that
# documenting a confirmation as a finding would do this. This sentence states the rule; the guard
# it describes is unchanged.
_FINDINGS_CONTRACT_SENTENCE = (
    "A finding is a problem that must be resolved before acceptance, whatever its severity: "
    "every listed finding blocks acceptance and is routed to repair or to the operator. "
    "When you approve, return findings: []. Do not list confirmations, observations or "
    "praise as findings. "
)


def render_capsule_review_prompt(
    packet: Mapping[str, Any],
    *,
    dependency_base: Mapping[str, Any] | None = None,
    _previous_instruction: bool = False,
    _legacy_instruction: bool = False,
) -> str:
    """One compact rendering shared by provider launch and durable proof verification.

    ``_previous_instruction`` reproduces, byte-for-byte, what this function returned with default
    arguments at commit b31c09c (before the findings-contract sentence below existed).
    ``_legacy_instruction`` continues to reproduce the older rendering from before commit 55af274.
    Both are composed by subtracting fixed text from the current instruction (the same technique
    ``_legacy_instruction`` already used), so neither can silently drift when the current
    instruction is next edited: only the subtracted text has to stay byte-identical.
    """
    checked = validate_capsule_review_packet(packet, dependency_base=dependency_base)
    instruction = (
        "Independently review the exact cumulative candidate against the approved intent, "
        "obligations, invariants and original authority. Inspect the diff and assess whether "
        "the recorded validation commands adequately prove the claimed behavior. Missing "
        "evidence is unavailable. Report concrete correctness, constraint or maintainability "
        "problems; omit stylistic preferences. Return only JSON with verdict "
        "approve|reject|incomplete and "
        "findings: [{finding_id, severity: blocker|major|minor|info, summary, "
        "kind: local_defect|missing_work|validation_failure|meaning_conflict|concept_drift|"
        "invariant_conflict|scope_change|architectural_decision|bearhug_machinery|"
        "assumption_invalidated}]. Approve only when every required obligation and invariant "
        "is satisfied. "
    ) + _FINDINGS_CONTRACT_SENTENCE + (
        "Observation-only comparisons and author assessments are claims to verify, "
        "not project truth. Independently establish their adequacy from the exact diff, authority "
        "and validation. A candidate failing an existing requirement or invariant is local_defect, "
        "missing_work or validation_failure when code can be repaired under unchanged authority. "
        "Use invariant_conflict only when the approved requirements themselves conflict and "
        "cannot all be satisfied without changing authority. Do not reinterpret accepted meaning "
        "to make the candidate pass."
    )
    if _previous_instruction or _legacy_instruction:
        instruction = instruction.replace(_FINDINGS_CONTRACT_SENTENCE, "")
    if _legacy_instruction:
        instruction = instruction.replace(
            "A candidate failing an existing requirement or invariant is local_defect, "
            "missing_work or validation_failure when code can be repaired "
            "under unchanged authority. "
            "Use invariant_conflict only when the approved requirements themselves conflict and "
            "cannot all be satisfied without changing authority. ",
            "",
        )
    raw = (
        _canonical(
            {
                "review_kind": "capsule",
                "packet_sha256": canonical_json_sha256(checked),
                "packet": checked,
                "instruction": instruction,
            }
        )
        + b"\n"
    )
    budget = checked["capsule"]["context_budget"]
    if budget["mode"] != "explicit" or (
        len(raw) > budget["max_bytes"] - budget["reserve_bytes"]
        or (len(raw) + 3) // 4 > budget["max_tokens"] - budget["reserve_tokens"]
    ):
        raise CapsuleAcceptanceError("full review prompt exceeds the sealed context budget")
    return raw.decode()


def validate_capsule_review_decision(value: Any) -> tuple[str, list[dict[str, str]]]:
    if not isinstance(value, dict) or set(value) != {"verdict", "findings"}:
        raise CapsuleAcceptanceError("review final output is not the closed verdict contract")
    if value["verdict"] not in {"approve", "reject", "incomplete"}:
        raise CapsuleAcceptanceError("review verdict is unsupported")
    if not isinstance(value["findings"], list) or len(value["findings"]) > 4096:
        raise CapsuleAcceptanceError("review findings must be a bounded array")
    findings = [_normalise_finding(row, "review finding") for row in value["findings"]]
    if len({row["finding_id"] for row in findings}) != len(findings):
        raise CapsuleAcceptanceError("review findings repeat an identity")
    return value["verdict"], findings


def _normalise_finding(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) not in {
        _FINDING_FIELDS,
        _FINDING_FIELDS | {"kind"},
    }:
        raise CapsuleAcceptanceError(f"{where} is not a closed finding")
    finding = dict(value)
    if "kind" in finding and finding["kind"] not in {
        "local_defect",
        "missing_work",
        "validation_failure",
        "meaning_conflict",
        "concept_drift",
        "invariant_conflict",
        "scope_change",
        "architectural_decision",
        "bearhug_machinery",
        "assumption_invalidated",
    }:
        raise CapsuleAcceptanceError(f"{where}.kind is unsupported")
    _token(finding["finding_id"], f"{where}.finding_id")
    if finding["severity"] not in {"blocker", "major", "minor", "info"}:
        raise CapsuleAcceptanceError(f"{where}.severity is unsupported")
    if (
        not isinstance(finding["summary"], str)
        or not finding["summary"]
        or len(finding["summary"]) > _MAX_FINDING_TEXT
    ):
        raise CapsuleAcceptanceError(f"{where}.summary is invalid")
    return finding


def _normalise_assessment(
    assessment: Mapping[str, Any],
    *,
    reviewer_receipt: Mapping[str, Any],
    packet_sha256: str,
    candidate_sha256: str,
) -> dict[str, Any]:
    if not isinstance(assessment, Mapping):
        raise CapsuleAcceptanceError("review assessment must be an object")
    review_id = assessment.get("review_id", assessment.get("id"))
    _token(review_id, "review assessment.review_id")
    verdict = assessment.get("verdict")
    if verdict not in {"approve", "reject", "incomplete"}:
        raise CapsuleAcceptanceError("review assessment verdict is unsupported")
    raw_findings = assessment.get("findings", [])
    if not isinstance(raw_findings, Sequence) or isinstance(raw_findings, (str, bytes, bytearray)):
        raise CapsuleAcceptanceError("review assessment findings must be an array")
    findings = [
        _normalise_finding(row, f"review assessment findings[{index}]")
        for index, row in enumerate(raw_findings)
    ]
    receipt_digest = canonical_json_sha256(dict(reviewer_receipt))
    supplied_digest = assessment.get("reviewer_receipt_sha256")
    if supplied_digest is not None and supplied_digest != receipt_digest:
        raise CapsuleAcceptanceError("review assessment reviewer receipt digest differs")
    refs = assessment.get("evidence_refs")
    if refs is None:
        # A legacy review receipt is itself an evidence object.  Its digest is retained as
        # provenance, but the provider receipt remains independently custody-checked later.
        refs = [receipt_digest]
    refs = _evidence_refs(refs, "review assessment.evidence_refs", nonempty=True)
    supplied_packet = assessment.get("packet_sha256", packet_sha256)
    if supplied_packet != packet_sha256:
        raise CapsuleAcceptanceError("review assessment packet digest differs")
    supplied_candidate = assessment.get("candidate_sha256", candidate_sha256)
    if supplied_candidate != candidate_sha256:
        raise CapsuleAcceptanceError("review assessment candidate digest differs")
    return {
        "review_id": review_id,
        "reviewer_receipt_sha256": receipt_digest,
        "reviewer_session_id": reviewer_receipt["session_id"],
        "reviewer_worktree_sha256": worktree_sha256(reviewer_receipt["cwd"]),
        "packet_sha256": packet_sha256,
        "candidate_sha256": candidate_sha256,
        "verdict": verdict,
        "findings": sorted(findings, key=lambda row: row["finding_id"]),
        "evidence_refs": refs,
    }


def _review_inputs(
    *,
    reviewer_receipts: Sequence[Mapping[str, Any]] | None,
    reviewers: Sequence[Mapping[str, Any]] | None,
    assessments: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    receipts = list(reviewer_receipts or ())
    assessment_rows = list(assessments or ())
    if reviewers is not None:
        if receipts or assessment_rows:
            raise CapsuleAcceptanceError("reviewers cannot be combined with expanded review inputs")
        for index, wrapper in enumerate(reviewers):
            if not isinstance(wrapper, Mapping):
                raise CapsuleAcceptanceError(f"reviewers[{index}] is malformed")
            receipt = wrapper.get("provider_receipt", wrapper.get("receipt"))
            if not isinstance(receipt, Mapping):
                raise CapsuleAcceptanceError(f"reviewers[{index}] has no provider receipt")
            assessment = wrapper.get("assessment", wrapper.get("review_receipt"))
            if assessment is None:
                assessment = wrapper
            receipts.append(receipt)
            assessment_rows.append(assessment)
    if len(receipts) != len(assessment_rows):
        raise CapsuleAcceptanceError("each reviewer must have an independent assessment")
    return receipts, assessment_rows


def _review_receipts_only(
    *,
    reviewer_receipts: Sequence[Mapping[str, Any]] | None,
    reviewers: Sequence[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    if reviewers is not None and reviewer_receipts:
        raise CapsuleAcceptanceError("reviewers cannot be combined with reviewer_receipts")
    if reviewers is None:
        return list(reviewer_receipts or ())
    receipts: list[Mapping[str, Any]] = []
    for index, wrapper in enumerate(reviewers):
        if not isinstance(wrapper, Mapping):
            raise CapsuleAcceptanceError(f"reviewers[{index}] is malformed")
        receipt = wrapper.get("provider_receipt", wrapper.get("receipt"))
        if not isinstance(receipt, Mapping):
            raise CapsuleAcceptanceError(f"reviewers[{index}] has no provider receipt")
        receipts.append(receipt)
    return receipts


def _review_semantic_receipts(
    *,
    review_receipts: Sequence[Mapping[str, Any]] | None,
    reviewers: Sequence[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    if reviewers is None:
        return list(review_receipts or ())
    if review_receipts:
        raise CapsuleAcceptanceError("reviewers cannot be combined with review_receipts")
    rows: list[Mapping[str, Any]] = []
    for index, wrapper in enumerate(reviewers):
        if not isinstance(wrapper, Mapping):
            raise CapsuleAcceptanceError(f"reviewers[{index}] is malformed")
        review = wrapper.get("review_receipt", wrapper.get("assessment"))
        if not isinstance(review, Mapping):
            raise CapsuleAcceptanceError(f"reviewers[{index}] has no durable review receipt")
        rows.append(review)
    return rows


def build_capsule_acceptance_proof(
    *,
    capsule_id: str,
    revision_id: str,
    intent_envelope_sha256: str,
    plan_sha256: str,
    candidate: Mapping[str, Any],
    packet: Mapping[str, Any],
    author_receipts: Sequence[Mapping[str, Any]],
    policy_sha256: str,
    quorum: int,
    reviewer_receipts: Sequence[Mapping[str, Any]] | None = None,
    review_receipts: Sequence[Mapping[str, Any]] | None = None,
    reviewers: Sequence[Mapping[str, Any]] | None = None,
    assessments: Sequence[Mapping[str, Any]] | None = None,
    proof_id: str | None = None,
    dependency_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a proof envelope from actual receipts and semantic reviewer assessments.

    This function calculates receipt/session/worktree identities from the supplied provider
    records.  It does not assert that those records are durable or qualified; verification must
    still be performed with a custody store and live checkouts.
    """

    _token(capsule_id, "capsule_id", lower=True)
    _token(revision_id, "revision_id", lower=True)
    _sha(intent_envelope_sha256, "intent_envelope_sha256")
    _sha(plan_sha256, "plan_sha256")
    _sha(policy_sha256, "policy_sha256")
    if type(quorum) is not int or quorum < 1:
        raise CapsuleAcceptanceError("quorum must be a positive integer")
    checked_candidate = _candidate(candidate)
    try:
        validate_capsule_review_packet(packet, dependency_base=dependency_base)
    except CapsuleAcceptanceError:
        raise
    packet_sha256 = canonical_json_sha256(dict(packet))
    author = list(author_receipts)
    if not author:
        raise CapsuleAcceptanceError("acceptance proof needs an author receipt chain")
    author_digests: list[str] = []
    for index, receipt in enumerate(author):
        try:
            checked = validate_provider_receipt(dict(receipt))
        except (TypeError, ProviderReceiptError) as exc:
            raise CapsuleAcceptanceError(f"author receipt {index} is invalid: {exc}") from exc
        author_digests.append(canonical_json_sha256(checked))
    if review_receipts is not None and reviewers is None and assessments is None:
        receipts = list(reviewer_receipts or ())
        raw_assessments = list(review_receipts)
    else:
        receipts, raw_assessments = _review_inputs(
            reviewer_receipts=reviewer_receipts,
            reviewers=reviewers,
            assessments=assessments,
        )
    durable_reviews = _review_semantic_receipts(
        review_receipts=review_receipts,
        reviewers=reviewers,
    )
    if durable_reviews:
        if assessments is not None:
            raise CapsuleAcceptanceError(
                "review_receipts cannot be combined with expanded assessments"
            )
        raw_assessments = durable_reviews
    elif not raw_assessments:
        raise CapsuleAcceptanceError("acceptance proof needs durable reviewer assessments")
    candidate_sha256 = canonical_json_sha256(checked_candidate)
    assessment_rows = []
    for index, (receipt, assessment) in enumerate(zip(receipts, raw_assessments, strict=True)):
        try:
            checked_receipt = validate_provider_receipt(dict(receipt))
        except (TypeError, ProviderReceiptError) as exc:
            raise CapsuleAcceptanceError(f"reviewer receipt {index} is invalid: {exc}") from exc
        if durable_reviews:
            if assessment.get("record_kind") == "campaign_review_receipt":
                try:
                    checked_legacy = validate_review_receipt(dict(assessment))
                except (CampaignReviewError, TypeError) as exc:
                    raise CapsuleAcceptanceError(
                        f"durable reviewer assessment {index} is invalid: {exc}"
                    ) from exc
                if (
                    checked_legacy["reviewer_receipt_sha256"]
                    != canonical_json_sha256(checked_receipt)
                    or checked_legacy["candidate"] != checked_candidate
                ):
                    raise CapsuleAcceptanceError(
                        f"durable reviewer assessment {index} binds different evidence"
                    )
            elif assessment.get("record_kind") == "capsule_review_receipt":
                _validate_capsule_review_record(
                    assessment,
                    provider_receipt=checked_receipt,
                    packet_sha256=packet_sha256,
                    candidate=checked_candidate,
                )
            else:
                raise CapsuleAcceptanceError(
                    f"durable reviewer assessment {index} has unsupported record kind"
                )
        assessment_rows.append(
            _normalise_assessment(
                assessment,
                reviewer_receipt=checked_receipt,
                packet_sha256=packet_sha256,
                candidate_sha256=candidate_sha256,
            )
        )
    proof = {
        "schema_version": "1",
        "record_kind": "capsule_acceptance_proof",
        "canonical_algorithm": CANONICAL_ALGORITHM,
        "proof_id": proof_id or f"acceptance-proof.{capsule_id}",
        "capsule_id": capsule_id,
        "revision_id": revision_id,
        "intent_envelope_sha256": intent_envelope_sha256,
        "plan_sha256": plan_sha256,
        "candidate": checked_candidate,
        "packet_sha256": packet_sha256,
        "policy_sha256": policy_sha256,
        "required_quorum": quorum,
        "author_receipt_sha256s": author_digests,
        "reviewer_assessments": sorted(assessment_rows, key=lambda row: row["review_id"]),
    }
    validate_capsule_acceptance_proof(proof)
    if len(_canonical(proof)) > _MAX_PROOF_BYTES:
        raise CapsuleAcceptanceError("acceptance proof exceeds its byte bound")
    return proof


def validate_capsule_acceptance_proof(value: Any) -> dict[str, Any]:
    """Validate the closed, digest-only acceptance proof envelope."""

    if not isinstance(value, Mapping) or set(value) != _PROOF_FIELDS:
        raise CapsuleAcceptanceError("acceptance proof has missing or unknown fields")
    proof = copy.deepcopy(dict(value))
    if (
        proof["schema_version"] != "1"
        or proof["record_kind"] != "capsule_acceptance_proof"
        or proof["canonical_algorithm"] != CANONICAL_ALGORITHM
    ):
        raise CapsuleAcceptanceError("unsupported acceptance proof identity")
    _token(proof["proof_id"], "proof_id")
    _token(proof["capsule_id"], "capsule_id", lower=True)
    _token(proof["revision_id"], "revision_id", lower=True)
    for field in ("intent_envelope_sha256", "plan_sha256", "packet_sha256", "policy_sha256"):
        _sha(proof[field], field)
    _candidate(proof["candidate"])
    if type(proof["required_quorum"]) is not int or proof["required_quorum"] < 1:
        raise CapsuleAcceptanceError("required_quorum must be a positive integer")
    author_digests = _evidence_refs(
        proof["author_receipt_sha256s"], "author_receipt_sha256s", nonempty=True
    )
    proof["author_receipt_sha256s"] = author_digests
    assessments = proof["reviewer_assessments"]
    if not isinstance(assessments, list) or not assessments:
        raise CapsuleAcceptanceError("acceptance proof needs reviewer assessments")
    review_ids: set[str] = set()
    reviewer_digests: set[str] = set()
    reviewer_sessions: set[str] = set()
    reviewer_worktrees: set[str] = set()
    for index, assessment in enumerate(assessments):
        if not isinstance(assessment, Mapping) or set(assessment) != _ASSESSMENT_FIELDS:
            raise CapsuleAcceptanceError(f"reviewer_assessments[{index}] is not closed")
        _token(assessment["review_id"], f"reviewer_assessments[{index}].review_id")
        if assessment["review_id"] in review_ids:
            raise CapsuleAcceptanceError("acceptance proof repeats a review id")
        review_ids.add(assessment["review_id"])
        _sha(
            assessment["reviewer_receipt_sha256"],
            f"reviewer_assessments[{index}].reviewer_receipt_sha256",
        )
        if assessment["reviewer_receipt_sha256"] in reviewer_digests:
            raise CapsuleAcceptanceError("acceptance proof reuses a reviewer receipt")
        reviewer_digests.add(assessment["reviewer_receipt_sha256"])
        if (
            not isinstance(assessment["reviewer_session_id"], str)
            or not assessment["reviewer_session_id"]
        ):
            raise CapsuleAcceptanceError("reviewer session identity is invalid")
        if assessment["reviewer_session_id"] in reviewer_sessions:
            raise CapsuleAcceptanceError("acceptance proof reuses a reviewer session")
        reviewer_sessions.add(assessment["reviewer_session_id"])
        _sha(
            assessment["reviewer_worktree_sha256"],
            f"reviewer_assessments[{index}].reviewer_worktree_sha256",
        )
        if assessment["reviewer_worktree_sha256"] in reviewer_worktrees:
            raise CapsuleAcceptanceError("acceptance proof reuses a reviewer worktree")
        reviewer_worktrees.add(assessment["reviewer_worktree_sha256"])
        _sha(assessment["packet_sha256"], f"reviewer_assessments[{index}].packet_sha256")
        if assessment["packet_sha256"] != proof["packet_sha256"]:
            raise CapsuleAcceptanceError("review assessment is for a different packet")
        _sha(assessment["candidate_sha256"], f"reviewer_assessments[{index}].candidate_sha256")
        if assessment["candidate_sha256"] != canonical_json_sha256(proof["candidate"]):
            raise CapsuleAcceptanceError("review assessment is for a different candidate")
        if assessment["verdict"] not in {"approve", "reject", "incomplete"}:
            raise CapsuleAcceptanceError("review assessment verdict is unsupported")
        findings = assessment["findings"]
        if not isinstance(findings, list):
            raise CapsuleAcceptanceError("review assessment findings must be an array")
        for finding_index, finding in enumerate(findings):
            _normalise_finding(finding, f"reviewer_assessments[{index}].findings[{finding_index}]")
        _evidence_refs(
            assessment["evidence_refs"],
            f"reviewer_assessments[{index}].evidence_refs",
            nonempty=True,
        )
    if len(_canonical(proof)) > _MAX_PROOF_BYTES:
        raise CapsuleAcceptanceError("acceptance proof exceeds its byte bound")
    return value if isinstance(value, dict) else proof


def _validate_capsule_review_record(
    value: Mapping[str, Any],
    *,
    provider_receipt: Mapping[str, Any],
    packet_sha256: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the runtime's durable projection of one strict reviewer response."""

    if not isinstance(value, Mapping) or set(value) not in {
        _CAPSULE_REVIEW_FIELDS,
        _CAPSULE_REVIEW_FIELDS_ENRICHED,
    }:
        raise CapsuleAcceptanceError("capsule review receipt is not a closed durable record")
    record = dict(value)
    if (
        record["schema_version"] != "1"
        or record["record_kind"] != "capsule_review_receipt"
        or record["canonical_algorithm"] != CANONICAL_ALGORITHM
    ):
        raise CapsuleAcceptanceError("capsule review receipt identity is invalid")
    provider_digest = canonical_json_sha256(dict(provider_receipt))
    if record["reviewer_receipt_sha256"] != provider_digest:
        raise CapsuleAcceptanceError("capsule review receipt provider binding differs")
    _sha(record["reviewer_worktree_sha256"], "capsule review receipt reviewer_worktree_sha256")
    if record["reviewer_session_id"] != provider_receipt["session_id"]:
        raise CapsuleAcceptanceError("capsule review receipt session binding differs")
    if record["reviewer_worktree_sha256"] != worktree_sha256(provider_receipt["cwd"]):
        raise CapsuleAcceptanceError("capsule review receipt worktree binding differs")
    if record["packet_sha256"] != packet_sha256:
        raise CapsuleAcceptanceError("capsule review receipt packet binding differs")
    expected_candidate_sha256 = canonical_json_sha256(dict(candidate))
    if record["candidate_sha256"] != expected_candidate_sha256:
        raise CapsuleAcceptanceError("capsule review receipt candidate binding differs")
    _normalise_assessment(
        record,
        reviewer_receipt=provider_receipt,
        packet_sha256=packet_sha256,
        candidate_sha256=expected_candidate_sha256,
    )
    if set(record) == _CAPSULE_REVIEW_FIELDS_ENRICHED:
        for field in ("intent_envelope_sha256", "plan_sha256"):
            _sha(record[field], f"capsule review receipt {field}")
        _token(record["revision_id"], "capsule review receipt revision_id", lower=True)
        _token(record["capsule_id"], "capsule review receipt capsule_id", lower=True)
        if _candidate(record["candidate"]) != dict(candidate):
            raise CapsuleAcceptanceError("capsule review receipt candidate differs")
    return _normalise_assessment(
        record,
        reviewer_receipt=provider_receipt,
        packet_sha256=packet_sha256,
        candidate_sha256=expected_candidate_sha256,
    )


def _durable_final_output(custody: Any, provider_receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Reopen the exact final response from the existing provider custody record.

    ``ProviderCustodyStore.validate`` intentionally exposes a narrow validation API.  Its
    create-only record is still the durable locator for the raw event stream, so this helper uses
    that validated record to feed the existing strict final-output parser.  Test doubles may
    expose the same operation as ``read_final_output`` without depending on private storage.
    """

    reader = getattr(custody, "read_final_output", None)
    if callable(reader):
        try:
            value = reader(dict(provider_receipt))
        except Exception as exc:
            raise CapsuleAcceptanceError(
                f"review final output custody is unavailable: {exc}"
            ) from exc
        if not isinstance(value, Mapping):
            raise CapsuleAcceptanceError("review final output custody returned a non-object")
        return dict(value)
    try:
        digest = canonical_json_sha256(dict(provider_receipt))
        path_reader = custody._path
        record_reader = custody._read_record
        record = record_reader(path_reader(digest))
        raw_path = Path(record["raw_events_path"])
        if not raw_path.is_absolute():
            raw_path = Path(custody.provider_output_root) / raw_path
        run = SimpleNamespace(
            receipt=dict(provider_receipt),
            raw_events_path=raw_path,
            server_events_path=raw_path,
        )
        return strict_final_json(run)
    except (
        AttributeError,
        KeyError,
        OSError,
        ProviderFinalOutputError,
        TypeError,
        ValueError,
    ) as exc:
        raise CapsuleAcceptanceError(
            f"review final output custody is unavailable: {type(exc).__name__}"
        ) from exc


def _durable_request(custody: Any, provider_receipt: Mapping[str, Any]) -> bytes:
    """Read the exact prompt from durable reviewer request custody.

    Codex App Server custody stores the complete client JSONL stream, while older fixtures and
    Claude custody store the plain prompt bytes.  The linked operational-evidence form is the
    only case where extracting a prompt is safe: verify the full stream digest first, then bind
    one exact ``turn/start`` text input to the receipt's thread and launch prompt digest.
    """

    reader = getattr(custody, "read_request", None)
    if callable(reader):
        try:
            raw = reader(dict(provider_receipt))
        except Exception as exc:
            raise CapsuleAcceptanceError(f"review request custody is unavailable: {exc}") from exc
        if not isinstance(raw, bytes):
            raise CapsuleAcceptanceError("review request custody returned non-bytes")
    else:
        try:
            digest = canonical_json_sha256(dict(provider_receipt))
            record = custody._read_record(custody._path(digest))
            raw = Path(record["request_path"]).read_bytes()
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
            raise CapsuleAcceptanceError(
                f"review request custody is unavailable: {type(exc).__name__}"
            ) from exc
    if hashlib.sha256(raw).hexdigest() != provider_receipt["request_sha256"]:
        raise CapsuleAcceptanceError("review request bytes differ from provider receipt")
    if len(raw) > _MAX_PACKET_BYTES:
        raise CapsuleAcceptanceError("review request exceeds its byte bound")
    operational_link = (
        provider_receipt.get("provider") == "openai-codex"
        and provider_receipt.get("operational_evidence_sha256") is not None
    )
    if not operational_link:
        # Legacy plain-prompt custody remains supported when no operational record claims the
        # App Server JSONL request form.
        return raw
    if not raw.endswith(b"\n"):
        raise CapsuleAcceptanceError("Codex reviewer request is not newline-terminated JSONL")

    def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise CapsuleAcceptanceError(f"Codex reviewer request repeats key {key!r}")
            value[key] = item
        return value

    messages: list[dict[str, Any]] = []
    try:
        for ordinal, line in enumerate(raw.splitlines(), start=1):
            message = json.loads(line, object_pairs_hook=_closed_object)
            if not isinstance(message, dict):
                raise CapsuleAcceptanceError(
                    f"Codex reviewer request line {ordinal} is not an object"
                )
            messages.append(message)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, CapsuleAcceptanceError):
            raise
        raise CapsuleAcceptanceError("Codex reviewer request is invalid JSONL") from exc
    starts = [message for message in messages if message.get("method") == "turn/start"]
    if len(starts) != 1:
        raise CapsuleAcceptanceError("Codex reviewer request must contain one turn/start")
    params = starts[0].get("params")
    if not isinstance(params, Mapping):
        raise CapsuleAcceptanceError("Codex turn/start has no parameters")
    if params.get("threadId") != provider_receipt.get("thread_id"):
        raise CapsuleAcceptanceError("Codex turn/start thread differs from provider receipt")
    if "turnId" in params and params.get("turnId") != provider_receipt.get("turn_id"):
        raise CapsuleAcceptanceError("Codex turn/start turn differs from provider receipt")
    inputs = params.get("input")
    if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], Mapping):
        raise CapsuleAcceptanceError("Codex turn/start must contain one text input")
    text_input = inputs[0]
    if set(text_input) != {"type", "text"} or text_input.get("type") != "text":
        raise CapsuleAcceptanceError("Codex turn/start input is not exactly one text item")
    prompt = text_input.get("text")
    if not isinstance(prompt, str) or not prompt:
        raise CapsuleAcceptanceError("Codex turn/start text input is invalid")
    launch = provider_receipt.get("launch")
    prompt_sha256 = launch.get("prompt_sha256") if isinstance(launch, Mapping) else None
    if not isinstance(prompt_sha256, str) or not _SHA256.fullmatch(prompt_sha256):
        raise CapsuleAcceptanceError("Codex reviewer launch has no valid prompt digest")
    prompt_bytes = prompt.encode("utf-8")
    if hashlib.sha256(prompt_bytes).hexdigest() != prompt_sha256:
        raise CapsuleAcceptanceError("Codex turn/start text differs from launch prompt")
    return prompt_bytes


def _check_review_request(
    custody: Any,
    provider_receipt: Mapping[str, Any],
    *,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    capsule: Mapping[str, Any],
    packet: Mapping[str, Any],
    packet_sha256: str,
    candidate: Mapping[str, Any],
    dependency_base: Mapping[str, Any] | None = None,
) -> None:
    """Ensure the provider actually received the exact bounded packet under review.

    Three renderings of the same packet are durably valid: the current instruction, the one that
    was current before the findings-contract sentence was added (``_previous_instruction=True``),
    and the legacy one from before commit 55af274 (``_legacy_instruction=True``). A request
    captured under any of them still proves the provider saw the exact sealed packet.

    Renderings are produced and compared one at a time, lazily, not built into a tuple
    up front. Adding the findings-contract sentence also lengthened the CURRENT instruction by
    one sentence, so a capsule sealed
    with a context budget snug enough for an OLDER rendering can have the CURRENT one alone
    exceed it. Producing the current rendering first and unconditionally would then raise the
    sealed-budget error before ever comparing `raw` against the older rendering it actually
    equals. A budget error hit while producing a rendering that is NOT the one `raw` equals never
    surfaces; if the rendering `raw` actually equals is itself the one that overflows, that error
    still stands; if no rendering matches at all, the mismatch error below still stands.
    """

    raw = _durable_request(custody, provider_receipt)
    pending_budget_error: CapsuleAcceptanceError | None = None
    for variant_kwargs in ({}, {"_previous_instruction": True}, {"_legacy_instruction": True}):
        try:
            rendering = render_capsule_review_prompt(
                packet, dependency_base=dependency_base, **variant_kwargs
            ).encode()
        except CapsuleAcceptanceError as exc:
            if str(exc) != "full review prompt exceeds the sealed context budget":
                raise
            if pending_budget_error is None:
                pending_budget_error = exc
            continue
        if raw == rendering:
            return
    if pending_budget_error is not None:
        raise pending_budget_error
    raise CapsuleAcceptanceError(
        "review request differs from the exact canonical review packet"
    )


def _custody_validate(custody: Any, label: str, receipt: Mapping[str, Any]) -> None:
    if custody is None or not callable(getattr(custody, "validate", None)):
        raise CapsuleAcceptanceError("durable provider custody validator is required")
    try:
        custody.validate(label, dict(receipt))
    except Exception as exc:
        raise CapsuleAcceptanceError(f"{label} provider custody is unavailable: {exc}") from exc


def _provider_receipt(value: Mapping[str, Any], where: str) -> dict[str, Any]:
    try:
        return validate_provider_receipt(dict(value))
    except (TypeError, ProviderReceiptError) as exc:
        raise CapsuleAcceptanceError(f"{where} is invalid: {exc}") from exc


def _durable_review_assessment(
    review: Mapping[str, Any],
    *,
    provider_receipt: Mapping[str, Any],
    packet_sha256: str,
    candidate: Mapping[str, Any],
    custody: Any,
) -> dict[str, Any]:
    """Read semantic verdict/findings from a durable review record.

    The legacy review record is accepted as a compatibility source, but its provider and
    candidate bindings are checked again here.  A free-form assessment passed only in the proof
    has no durable final-output custody and is therefore rejected by the acceptance boundary.
    """

    if not isinstance(review, Mapping):
        raise CapsuleAcceptanceError("durable reviewer assessment is malformed")
    provider_digest = canonical_json_sha256(dict(provider_receipt))
    if review.get("record_kind") == "campaign_review_receipt":
        try:
            checked = validate_review_receipt(dict(review))
        except (CampaignReviewError, TypeError) as exc:
            raise CapsuleAcceptanceError(f"durable review receipt is invalid: {exc}") from exc
        if (
            checked["reviewer_receipt_sha256"] != provider_digest
            or checked["candidate"] != dict(candidate)
            or checked["reviewer_session_id"] != provider_receipt["session_id"]
            or checked["reviewer_worktree_sha256"] != worktree_sha256(provider_receipt["cwd"])
        ):
            raise CapsuleAcceptanceError("durable review receipt is bound to different evidence")
    elif review.get("record_kind") == "capsule_review_receipt":
        _validate_capsule_review_record(
            review,
            provider_receipt=provider_receipt,
            packet_sha256=packet_sha256,
            candidate=candidate,
        )
    else:
        raise CapsuleAcceptanceError("review semantic evidence is not a durable review receipt")
    raw_final = _durable_final_output(custody, provider_receipt)
    if set(raw_final) != {"verdict", "findings"}:
        raise CapsuleAcceptanceError("review final output is not the closed verdict contract")
    if raw_final["verdict"] != review.get("verdict") or raw_final["findings"] != review.get(
        "findings"
    ):
        raise CapsuleAcceptanceError("durable review receipt differs from raw final output")
    return _normalise_assessment(
        review,
        reviewer_receipt=provider_receipt,
        packet_sha256=packet_sha256,
        candidate_sha256=canonical_json_sha256(dict(candidate)),
    )


def _check_completed_coverage(packet: Mapping[str, Any], capsule: Mapping[str, Any]) -> None:
    for row in packet["obligation_coverage"]:
        if row["status"] != "pass":
            raise CapsuleAcceptanceError("an obligation lacks passing completion proof")
    for row in packet["validation"]:
        if row["status"] != "pass":
            raise CapsuleAcceptanceError("a validation profile lacks passing completion proof")
    for row in packet["invariants"]:
        if row["status"] != "pass":
            raise CapsuleAcceptanceError("an invariant lacks passing completion proof")
    reconciliation = packet["reconciliation"]
    if reconciliation["status"] not in _ACCEPTED_RECONCILIATION_STATUSES:
        raise CapsuleAcceptanceError("reconciliation proof is unavailable or blocked")
    if not reconciliation["evidence_refs"]:
        raise CapsuleAcceptanceError("reconciliation proof has no durable evidence reference")
    if reconciliation["hil_triggers"]:
        raise CapsuleAcceptanceError("reconciliation still has unresolved HIL triggers")
    mechanical = reconciliation["mechanical"]
    rows = [
        *mechanical["identity"].values(),
        *mechanical["bindings"].values(),
        *mechanical["invariants"].values(),
    ]
    rows.extend(mechanical["surfaces"].values())
    rows.extend([mechanical["obligation_coverage"], mechanical["validation"]])
    if any(row["status"] != "match" for row in rows):
        raise CapsuleAcceptanceError(
            "reconciliation contains unavailable or changed mechanical proof"
        )
    if any(not row["evidence_refs"] for row in rows):
        raise CapsuleAcceptanceError("reconciliation proof rows lack durable evidence references")
    boundary = capsule["completion_boundary"]
    declared_profile_ids = {row["profile_id"] for row in capsule["validation_profiles"]}
    if not declared_profile_ids:
        raise CapsuleAcceptanceError("capsule declares no validation profiles")
    available_gates = {ref for row in capsule["validation_profiles"] for ref in row["gate_refs"]}
    available_gates.add("gate.review")
    if set(boundary["required_gate_refs"]) - available_gates:
        raise CapsuleAcceptanceError("completion boundary contains an unimplemented gate")
    required_receipts = set(boundary["required_receipt_kinds"])
    if required_receipts - {"author", "review", "reviewer", "verifier", "test"}:
        raise CapsuleAcceptanceError("completion boundary requests unsupported receipt proof")


def _verify_completion_references(packet, state_root, authors, validation_record):
    """Reopen cited bytes; semantic adequacy remains the independent review's job."""
    known = {
        packet["intent_envelope_sha256"],
        packet["plan_sha256"],
        canonical_json_sha256(packet["candidate"]),
        canonical_json_sha256(validation_record),
        *(row["content_sha256"] for row in packet["intent"]["authority_refs"]),
    }
    for receipt in authors:
        known.add(canonical_json_sha256(receipt))
        for key in ("raw_event_sha256", "request_sha256", "stderr_sha256", "argv_sha256"):
            if receipt.get(key):
                known.add(receipt[key])
    refs = set()

    def collect(value):
        if isinstance(value, Mapping):
            refs.update(value.get("evidence_refs", ()))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for key in ("obligation_coverage", "invariants", "reconciliation"):
        collect(packet[key])
    root = Path(state_root)
    for digest in refs - known:
        path = root / f"{digest}.bin"
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 4 * 1024 * 1024
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise CapsuleAcceptanceError(
                f"completion evidence reference has no verified bytes: {digest}"
            )


def _reviewer_live_candidate(
    receipt: Mapping[str, Any], candidate: Mapping[str, Any], where: str
) -> None:
    if receipt["role"] != "reviewer":
        raise CapsuleAcceptanceError(f"{where} is not a reviewer receipt")
    if receipt["launch"]["sandbox"] != "read-only":
        raise CapsuleAcceptanceError(f"{where} reviewer is not read-only")
    if receipt["terminal_state"] != "completed" or receipt["promotion_eligible"] is not True:
        raise CapsuleAcceptanceError(f"{where} reviewer is not qualified and completed")
    launch = receipt["launch_repository"]
    if (
        launch["repository_common_dir_sha256"] != candidate["repository_common_dir_sha256"]
        or launch["head_oid"] != candidate["head_oid"]
        or launch["tree_oid"] != candidate["tree_oid"]
    ):
        raise CapsuleAcceptanceError(f"{where} reviewer launched on a different candidate")
    reviewed = receipt["candidate"]
    if reviewed is None or (
        reviewed["repository_common_dir_sha256"] != candidate["repository_common_dir_sha256"]
        or reviewed["head_oid"] != candidate["head_oid"]
        or reviewed["tree_oid"] != candidate["tree_oid"]
    ):
        raise CapsuleAcceptanceError(f"{where} reviewer closed on a different candidate")
    try:
        current = capture_launch_repository(receipt["cwd"])
    except (ProviderReceiptError, OSError) as exc:
        raise CapsuleAcceptanceError(f"{where} reviewer checkout is unavailable: {exc}") from exc
    if (
        current.repository_common_dir_sha256 != candidate["repository_common_dir_sha256"]
        or current.head_oid != candidate["head_oid"]
        or current.tree_oid != candidate["tree_oid"]
    ):
        raise CapsuleAcceptanceError(f"{where} reviewer checkout identity changed")


def _required_quorum(intent: Mapping[str, Any], supplied: int | None) -> int:
    snapshot = intent.get("campaign_envelope", {}).get("policy_snapshot", {})
    configured = (
        snapshot.get("policies", {}).get("review", {}).get("minimum_approvals")
        if isinstance(snapshot, Mapping)
        else None
    )
    minimum = configured if type(configured) is int and configured >= 1 else 1
    if supplied is None:
        return minimum
    if type(supplied) is not int or supplied < 1:
        raise CapsuleAcceptanceError("required_quorum must be a positive integer")
    if supplied < minimum:
        raise CapsuleAcceptanceError("required_quorum cannot weaken the sealed policy minimum")
    return supplied


def _provider_authority(
    receipt, *, provider_policy, qualification_index, policy_sha256, _historical_only=False
):
    """Match a turn to policy and current qualification, or custody-only historical identity."""
    from bearhug.campaign.capsule_runtime import _provider_policy_document
    from bearhug.providers.compatibility import require_supported

    if provider_policy is None or qualification_index is None:
        raise CapsuleAcceptanceError("provider policy and qualification custody are required")
    if canonical_json_sha256(_provider_policy_document(provider_policy)) != policy_sha256:
        raise CapsuleAcceptanceError("provider policy bytes differ from sealed authority")
    selection = {"openai-codex": "codex", "anthropic-claude": "claude"}[receipt["provider"]]
    if _historical_only:
        qualified = qualification_index.recovery_binding(selection)
    else:
        qualified = qualification_index.require(selection)
        qualified.revalidate_runtime_files()
        require_supported(
            qualified.compatibility_policy(),
            adapter=qualified.adapter,
            version=qualified.adapter_version,
            repository_root=qualified.bundle_root,
        )
    if (
        receipt["adapter"] != qualified.adapter
        or receipt["adapter_version"] != qualified.adapter_version
        or receipt["launch"]["settings_sha256"] != qualified.settings_sha256
        or receipt["launch"]["rules_sha256"] != qualified.rules_sha256
    ):
        raise CapsuleAcceptanceError("provider receipt differs from qualified runtime authority")
    roles = [
        role
        for role in provider_policy.roles.values()
        if role.provider == selection
        and role.sandbox == receipt["launch"]["sandbox"]
        and role.approval_policy == receipt["launch"]["approval_policy"]
        and role.model == receipt["identity"]["requested_model"]
        and role.effort == receipt["identity"]["requested_reasoning_effort"]
        and list(role.required_capabilities) == receipt["required_capabilities"]
    ]
    if not roles:
        raise CapsuleAcceptanceError("provider turn does not match an authorized policy role")
    # Recovery proves historical custody, not current qualification of an external bundle.
    return None if _historical_only else qualified.qualification.manifest_sha256


def verify_capsule_acceptance(
    *,
    intent_envelope: Mapping[str, Any],
    capsule_plan: Mapping[str, Any],
    capsule: Mapping[str, Any],
    packet: Mapping[str, Any],
    candidate_worktree: Path | str,
    author_receipts: Sequence[Mapping[str, Any]],
    reviewer_receipts: Sequence[Mapping[str, Any]] | None = None,
    review_receipts: Sequence[Mapping[str, Any]] | None = None,
    reviewers: Sequence[Mapping[str, Any]] | None = None,
    proof: Mapping[str, Any] | None,
    custody: Any,
    policy_sha256: str,
    provider_policy: Any = None,
    qualification_index: Any = None,
    validation_state_root: Path | str | None = None,
    validation_receipt_sha256: str | None = None,
    validation_commands: Mapping[str, Sequence[str]] | None = None,
    required_quorum: int | None = None,
    previous_plan: Mapping[str, Any] | None = None,
    dependency_base: Mapping[str, Any] | None = None,
    dependency_acceptance_bundles: Sequence[Mapping[str, Any]] | None = None,
    _dependency_stack: tuple[str, ...] = (),
    _historical_only: bool = False,
) -> dict[str, Any]:
    """Revalidate one capsule candidate and return a closed acceptance projection.

    ``custody`` must be the existing durable provider custody store (or a test double with the
    same ``validate(label, receipt)`` contract).  Missing custody, unavailable raw bytes, stale
    proof, self-review, wrong policy/revision, and every incomplete obligation are hard errors.
    ``_historical_only`` is solely for disposing already-acquired controller custody: it verifies
    sealed runtime identities without reopening live executable/settings/capture resources. Fresh
    Fresh integration rejects historical bundles; provider launches still require qualification.
    """

    if proof is None:
        raise CapsuleAcceptanceError("acceptance proof is unavailable")
    try:
        checked_proof = validate_capsule_acceptance_proof(proof)
        checked_packet = validate_capsule_review_packet(packet, dependency_base=dependency_base)
    except CapsuleAcceptanceError:
        raise
    intent, plan, selected, intent_sha256, plan_sha256 = _validate_sealed(
        intent_envelope, capsule_plan, capsule, previous_plan=previous_plan
    )
    if (
        checked_packet["intent_envelope_sha256"] != intent_sha256
        or checked_packet["plan_sha256"] != plan_sha256
        or checked_packet["capsule_id"] != selected["capsule_id"]
        or checked_packet["revision_id"] != plan["revision"]["revision_id"]
        or checked_proof["intent_envelope_sha256"] != intent_sha256
        or checked_proof["plan_sha256"] != plan_sha256
        or checked_proof["capsule_id"] != selected["capsule_id"]
        or checked_proof["revision_id"] != plan["revision"]["revision_id"]
    ):
        raise CapsuleAcceptanceError("acceptance proof or packet binds a different revision")
    if selected["capsule_id"] in _dependency_stack:
        raise CapsuleAcceptanceError("capsule dependency acceptance contains a cycle")
    try:
        checked_dependency = _checked_dependency_base(
            dependency_base,
            plan=plan,
            candidate=checked_packet["candidate"],
        )
    except CapsuleAcceptanceError:
        raise
    declared_dependencies = set(selected["depends_on"])
    if declared_dependencies and checked_dependency is None:
        raise CapsuleAcceptanceError("dependent capsule lacks its selected dependency base")
    if checked_dependency is not None:
        provenance_ids = {row["capsule_id"] for row in checked_dependency["provenance"]}
        if provenance_ids != declared_dependencies:
            raise CapsuleAcceptanceError(
                "selected dependency provenance does not exactly cover declared dependencies"
            )
        if dependency_acceptance_bundles is None:
            raise CapsuleAcceptanceError(
                "actual predecessor acceptance bundles are required for dependency verification"
            )
        by_capsule: dict[str, Mapping[str, Any]] = {}
        for bundle in dependency_acceptance_bundles:
            if not isinstance(bundle, Mapping):
                raise CapsuleAcceptanceError("dependency acceptance bundle is malformed")
            bundle_capsule = bundle.get("capsule")
            bundle_id = (
                bundle_capsule.get("capsule_id") if isinstance(bundle_capsule, Mapping) else None
            )
            if not isinstance(bundle_id, str):
                raise CapsuleAcceptanceError("dependency acceptance bundle lacks capsule identity")
            if bundle_id in by_capsule:
                raise CapsuleAcceptanceError("dependency acceptance bundles repeat a capsule")
            by_capsule[bundle_id] = bundle
        for row in checked_dependency["provenance"]:
            predecessor = by_capsule.get(row["capsule_id"])
            if predecessor is None:
                raise CapsuleAcceptanceError(
                    f"accepted predecessor {row['capsule_id']!r} is unavailable"
                )
            try:
                predecessor_intent = predecessor.get("intent_envelope")
                if validate_intent_envelope(predecessor_intent).digest != intent_sha256:
                    raise CapsuleAcceptanceError(
                        f"accepted predecessor {row['capsule_id']!r} belongs to another intent"
                    )
            except CapsuleAcceptanceError:
                raise
            except Exception as exc:
                raise CapsuleAcceptanceError(
                    f"accepted predecessor {row['capsule_id']!r} intent is invalid"
                ) from exc
            active_predecessor = next(
                (item for item in plan["capsules"] if item["capsule_id"] == row["capsule_id"]),
                None,
            )
            if active_predecessor is None or predecessor.get("capsule") != active_predecessor:
                raise CapsuleAcceptanceError(
                    f"accepted predecessor {row['capsule_id']!r} "
                    "capsule differs from the active plan"
                )
            predecessor_result = verify_capsule_acceptance(
                **{**predecessor, "_historical_only": _historical_only},
                dependency_acceptance_bundles=dependency_acceptance_bundles,
                _dependency_stack=(*_dependency_stack, selected["capsule_id"]),
            )
            if (
                predecessor_result.get("accepted") is not True
                or predecessor_result.get("candidate") != row["candidate"]
            ):
                raise CapsuleAcceptanceError(
                    f"accepted predecessor {row['capsule_id']!r} candidate differs from provenance"
                )
    if validate_intent_envelope(
        checked_packet["intent"]
    ).digest != intent_sha256 or _canonical_capsule(
        checked_packet["capsule"]
    ) != _canonical_capsule(selected):
        raise CapsuleAcceptanceError("review packet does not carry the sealed records")
    if canonical_json_sha256(dict(packet)) != checked_proof["packet_sha256"]:
        raise CapsuleAcceptanceError("acceptance proof packet digest is stale")
    checked_policy = _sha(policy_sha256, "policy_sha256")
    if checked_proof["policy_sha256"] != checked_policy:
        raise CapsuleAcceptanceError("acceptance proof policy differs from the configured policy")
    if checked_policy not in intent["campaign_envelope"]["policy_refs"]:
        raise CapsuleAcceptanceError("configured policy is not authorized by the sealed envelope")
    quorum = _required_quorum(intent, required_quorum)
    if checked_proof["required_quorum"] != quorum:
        raise CapsuleAcceptanceError("acceptance proof quorum differs from configured policy")
    _check_completed_coverage(checked_packet, selected)

    supplied_candidate = _candidate(checked_packet["candidate"])
    if supplied_candidate != checked_proof["candidate"]:
        raise CapsuleAcceptanceError("proof and packet candidate identities differ")
    authors = list(author_receipts)
    if not authors:
        raise CapsuleAcceptanceError("author receipt chain is unavailable")
    validated_authors: list[dict[str, Any]] = []
    author_digests: list[str] = []
    author_sessions: set[str] = set()
    author_worktrees: set[str] = set()
    qualification_manifests: set[str | None] = set()
    for index, raw in enumerate(authors):
        receipt = _provider_receipt(raw, f"author receipt {index}")
        _custody_validate(custody, f"author.{index}", receipt)
        qualification_manifests.add(
            _provider_authority(
                receipt,
                provider_policy=provider_policy,
                qualification_index=qualification_index,
                policy_sha256=checked_policy,
                _historical_only=_historical_only,
            )
        )
        if (
            receipt["role"] != "author"
            or receipt["terminal_state"] != "completed"
            or receipt["promotion_eligible"] is not True
        ):
            raise CapsuleAcceptanceError(
                f"author receipt {index} is not a qualified completed author turn"
            )
        if receipt["session_id"] in author_sessions:
            raise CapsuleAcceptanceError("author receipt chain reuses a provider session")
        author_sessions.add(receipt["session_id"])
        author_worktrees.add(worktree_sha256(receipt["cwd"]))
        validated_authors.append(receipt)
        author_digests.append(canonical_json_sha256(receipt))
    if author_digests != checked_proof["author_receipt_sha256s"]:
        raise CapsuleAcceptanceError("acceptance proof author custody chain differs")
    author_worktree = Path(validated_authors[0]["cwd"]).expanduser().resolve()
    requested_worktree = Path(candidate_worktree).expanduser().resolve()
    if author_worktree != requested_worktree or any(
        Path(row["cwd"]).resolve() != author_worktree for row in validated_authors
    ):
        raise CapsuleAcceptanceError(
            "author receipt chain does not name one exact candidate worktree"
        )
    launch = validated_authors[0]["launch_repository"]
    expected_launch_base = (
        checked_dependency["base_oid"]
        if checked_dependency is not None
        else plan["subject"]["base_oid"]
    )
    if launch["head_oid"] != expected_launch_base:
        raise CapsuleAcceptanceError("author chain does not start at the selected capsule base")
    try:
        candidate, _paths = capture_capsule_candidate(
            author_worktree,
            LaunchRepository(
                launch["repository_common_dir_sha256"],
                launch["head_oid"],
                launch["tree_oid"],
                True,
            ),
            validated_authors,
            selected["mutation_envelope"]["path_prefixes"],
            dependency_base=checked_dependency,
            original_base_oid=plan["subject"]["base_oid"],
        )
    except Exception as exc:
        raise CapsuleAcceptanceError(f"durable cumulative candidate is unavailable: {exc}") from exc
    if candidate != supplied_candidate:
        raise CapsuleAcceptanceError("durable Git candidate differs from packet/proof")

    if validation_state_root is None or validation_receipt_sha256 is None:
        raise CapsuleAcceptanceError("actual validation receipt custody is required")
    try:
        from bearhug.campaign.capsule_validation import verify_capsule_validation

        validation_record = verify_capsule_validation(
            state_root=validation_state_root,
            receipt_sha256=validation_receipt_sha256,
            candidate_worktree=author_worktree,
            candidate=supplied_candidate,
            profiles=selected["validation_profiles"],
            commands=validation_commands,
        )
    except Exception as exc:
        raise CapsuleAcceptanceError(f"actual validation evidence is unavailable: {exc}") from exc
    if validation_record.get("status") != "pass":
        raise CapsuleAcceptanceError("actual validation commands did not pass")
    packet_validation_evidence = checked_packet["validation_evidence"]
    if (
        packet_validation_evidence["receipt_sha256"] != validation_receipt_sha256
        or packet_validation_evidence["record"] != validation_record
    ):
        raise CapsuleAcceptanceError(
            "review packet validation evidence differs from the actual validation receipt"
        )
    required_artifacts = set(selected["completion_boundary"]["required_artifact_refs"])
    supplied_artifacts = packet_validation_evidence["artifact_refs"]
    if required_artifacts - set(supplied_artifacts):
        raise CapsuleAcceptanceError(
            "review packet is missing a completion-boundary validation artifact"
        )
    for row in checked_packet["validation"]:
        if validation_receipt_sha256 not in row["evidence_refs"]:
            raise CapsuleAcceptanceError(
                "packet validation proof is not bound to the actual validation receipt"
            )

    reviewers_checked = _review_receipts_only(
        reviewer_receipts=reviewer_receipts,
        reviewers=reviewers,
    )
    semantic_reviews = _review_semantic_receipts(
        review_receipts=review_receipts,
        reviewers=reviewers,
    )
    if not semantic_reviews:
        raise CapsuleAcceptanceError("durable semantic review evidence is unavailable")
    if len(reviewers_checked) != len(checked_proof["reviewer_assessments"]):
        raise CapsuleAcceptanceError("provider reviewer custody does not match the proof")
    if len(semantic_reviews) != len(reviewers_checked):
        raise CapsuleAcceptanceError("semantic review custody does not match provider reviewers")
    assessment_by_digest = {
        row["reviewer_receipt_sha256"]: row for row in checked_proof["reviewer_assessments"]
    }
    reviewer_sessions = set(author_sessions)
    reviewer_worktrees = set(author_worktrees)
    verified_review_digests: list[str] = []
    for index, (raw, semantic_review) in enumerate(
        zip(reviewers_checked, semantic_reviews, strict=True)
    ):
        receipt = _provider_receipt(raw, f"reviewer receipt {index}")
        digest = canonical_json_sha256(receipt)
        _custody_validate(custody, f"reviewer.{index}", receipt)
        qualification_manifests.add(
            _provider_authority(
                receipt,
                provider_policy=provider_policy,
                qualification_index=qualification_index,
                policy_sha256=checked_policy,
                _historical_only=_historical_only,
            )
        )
        _check_review_request(
            custody,
            receipt,
            intent=intent,
            plan=plan,
            capsule=selected,
            packet=checked_packet,
            packet_sha256=checked_proof["packet_sha256"],
            candidate=supplied_candidate,
            dependency_base=checked_dependency,
        )
        _reviewer_live_candidate(receipt, supplied_candidate, f"reviewer {index}")
        if digest not in assessment_by_digest:
            raise CapsuleAcceptanceError("reviewer receipt has no matching semantic assessment")
        if receipt["session_id"] in reviewer_sessions:
            raise CapsuleAcceptanceError("reviewer session is reused or belongs to the author")
        reviewer_sessions.add(receipt["session_id"])
        reviewer_worktree_digest = worktree_sha256(receipt["cwd"])
        if reviewer_worktree_digest in reviewer_worktrees:
            raise CapsuleAcceptanceError("reviewer worktree is reused or belongs to the author")
        reviewer_worktrees.add(reviewer_worktree_digest)
        assessment = assessment_by_digest[digest]
        durable_assessment = _durable_review_assessment(
            semantic_review,
            provider_receipt=receipt,
            packet_sha256=checked_proof["packet_sha256"],
            candidate=supplied_candidate,
            custody=custody,
        )
        if durable_assessment != assessment:
            raise CapsuleAcceptanceError(
                "acceptance proof differs from durable reviewer final output"
            )
        if (
            assessment["reviewer_session_id"] != receipt["session_id"]
            or assessment["reviewer_worktree_sha256"] != reviewer_worktree_digest
            or assessment["packet_sha256"] != checked_proof["packet_sha256"]
        ):
            raise CapsuleAcceptanceError("review assessment provenance differs from custody")
        if assessment["verdict"] != "approve" or assessment["findings"]:
            raise CapsuleAcceptanceError(
                "review quorum contains a rejecting or blocking assessment"
            )
        verified_review_digests.append(digest)
    if len(verified_review_digests) < quorum:
        raise CapsuleAcceptanceError(
            f"review quorum is missing: {len(verified_review_digests)}/{quorum}"
        )
    required_receipts = set(selected["completion_boundary"]["required_receipt_kinds"])
    if "author" in required_receipts and not validated_authors:
        raise CapsuleAcceptanceError("completion boundary lacks author receipt evidence")
    if required_receipts & {"review", "reviewer"} and not verified_review_digests:
        raise CapsuleAcceptanceError("completion boundary lacks reviewer receipt evidence")

    # test/verifier receipt kinds denote the actual command executor above; review
    # denotes independent provider judgement. Unknown receipt kinds were rejected.
    if required_receipts & {"test", "verifier"} and validation_record["status"] != "pass":
        raise CapsuleAcceptanceError("completion boundary lacks actual verifier/test evidence")
    _verify_completion_references(
        checked_packet, validation_state_root, validated_authors, validation_record
    )

    proof_sha256 = canonical_json_sha256(dict(proof))
    return {
        "schema_version": "1",
        "record_kind": "capsule_acceptance_result",
        "canonical_algorithm": CANONICAL_ALGORITHM,
        "capsule_id": selected["capsule_id"],
        "revision_id": plan["revision"]["revision_id"],
        "intent_envelope_sha256": intent_sha256,
        "plan_sha256": plan_sha256,
        "candidate": copy.deepcopy(supplied_candidate),
        "packet_sha256": checked_proof["packet_sha256"],
        "proof_sha256": proof_sha256,
        "policy_sha256": checked_policy,
        "author_receipt_sha256s": author_digests,
        "reviewer_receipt_sha256s": sorted(verified_review_digests),
        "required_quorum": quorum,
        "eligible_reviews": len(verified_review_digests),
        "qualification_manifest_sha256s": sorted(
            digest for digest in qualification_manifests if digest is not None
        ),
        "accepted": True,
    }


__all__ = [
    "CapsuleAcceptanceError",
    "CapsuleReviewError",
    "build_capsule_acceptance_proof",
    "build_capsule_review_packet",
    "capsule_review_packet_sha256",
    "validate_capsule_acceptance_proof",
    "validate_capsule_review_packet",
    "verify_capsule_acceptance",
]
