"""Canonical provider-session custody and immutable Git candidate identity.

The receipt is the local authority.  The cockpit record is a deliberately smaller projection of
it.  A provider may complete while leaving a dirty worktree; that session still has custody, but
``candidate`` is null and it cannot become promotion evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.host_git import describe_dirty_entries, parse_status_entries
from bearhug.paths import assert_writable
from bearhug.providers.claude import ClaudeNormalization
from bearhug.providers.codex_app_server import CodexAppServerNormalization


class ProviderReceiptError(ValueError):
    """A receipt or its Git identity is malformed, ambiguous, or not reproducible."""


@dataclass(frozen=True, slots=True)
class LaunchRepository:
    repository_common_dir_sha256: str
    head_oid: str
    tree_oid: str
    clean: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CandidateResult:
    repository_common_dir_sha256: str
    base_oid: str
    head_oid: str
    tree_oid: str
    patch_sha256: str
    clean: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CloseRepository:
    repository_common_dir_sha256: str
    head_oid: str
    tree_oid: str
    status_sha256: str
    dirty_path_count: int
    clean: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def argv_sha256(argv: tuple[str, ...]) -> str:
    """Hash argv as canonical JSON so argument boundaries cannot collide."""

    return sha256_bytes(
        json.dumps(list(argv), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _git(cwd: Path, *args: str, check: bool = True) -> bytes:
    try:
        from bearhug.host_git import run_git

        result = run_git(cwd, *args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderReceiptError(f"cannot inspect Git repository at {cwd}: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ProviderReceiptError(f"git {' '.join(args)} failed at {cwd}: {detail}")
    return result.stdout


def _oid(cwd: Path, expression: str) -> str:
    value = _git(cwd, "rev-parse", "--verify", expression).decode().strip()
    if len(value) not in {40, 64} or any(char not in "0123456789abcdef" for char in value):
        raise ProviderReceiptError(f"Git returned an invalid object id for {expression!r}")
    return value


def _common_dir(cwd: Path) -> tuple[Path, str]:
    raw = _git(cwd, "rev-parse", "--git-common-dir").decode().strip()
    if not raw:
        raise ProviderReceiptError("Git returned an empty common directory")
    common = Path(raw)
    if not common.is_absolute():
        common = cwd / common
    common = common.resolve()
    return common, sha256_bytes(os.fsencode(common))


def _status(cwd: Path) -> tuple[bytes, tuple[tuple[str, str], ...]]:
    """The raw status bytes, and the entries a strict single parse of them makes -- used both
    for their count (``len()``) and, only when the caller needs it, a refusal's detail, so the
    same bytes are never walked twice.
    """

    raw = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    try:
        entries = parse_status_entries(raw)
    except ValueError as exc:
        raise ProviderReceiptError(f"Git porcelain status is malformed: {exc}") from exc
    return raw, entries


def capture_launch_repository(cwd: Path | str) -> LaunchRepository:
    """Bind a clean launch commit and its shared repository identity before execution."""

    root = Path(cwd).expanduser().resolve()
    if not root.is_dir():
        raise ProviderReceiptError(f"provider cwd is not a directory: {root}")
    _, common_digest = _common_dir(root)
    head = _oid(root, "HEAD")
    tree = _oid(root, "HEAD^{tree}")
    status, entries = _status(root)
    if status:
        raise ProviderReceiptError(
            "provider launch requires a clean Git worktree: "
            + describe_dirty_entries(root, entries)
        )
    return LaunchRepository(common_digest, head, tree)


def capture_close_repository(
    cwd: Path | str,
    launch: LaunchRepository,
    *,
    require_unchanged: bool,
) -> tuple[CloseRepository, CandidateResult | None]:
    """Capture terminal Git state and return a candidate only for an acceptable clean close."""

    root = Path(cwd).expanduser().resolve()
    _, common_digest = _common_dir(root)
    if common_digest != launch.repository_common_dir_sha256:
        raise ProviderReceiptError("provider cwd changed Git common-directory identity")
    status, entries = _status(root)
    head = _oid(root, "HEAD")
    tree = _oid(root, "HEAD^{tree}")
    close = CloseRepository(
        common_digest,
        head,
        tree,
        sha256_bytes(status),
        len(entries),
        not status,
    )
    if status or (require_unchanged and (head != launch.head_oid or tree != launch.tree_oid)):
        return close, None
    from bearhug.host_git import run_git

    try:
        ancestry = run_git(root, "merge-base", "--is-ancestor", launch.head_oid, head)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProviderReceiptError("cannot inspect candidate ancestry safely") from exc
    if ancestry.returncode != 0:
        return close, None
    patch = _git(
        root,
        "diff-tree",
        "--no-commit-id",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-textconv",
        "-r",
        launch.head_oid,
        head,
        "--",
    )
    return close, CandidateResult(
        common_digest, launch.head_oid, head, tree, sha256_bytes(patch)
    )


_TOP = frozenset(
    {
        "schema_version", "record_kind", "provider", "adapter", "adapter_version",
        "observed_at", "cwd", "role", "required_capabilities", "session_id", "thread_id",
        "turn_id", "raw_event_sha256", "raw_event_count", "request_sha256",
        "terminal_state", "identity", "approval_observation", "approval_requests",
        "approval_resolutions", "item_types", "launch_repository", "close_repository",
        "candidate", "launch", "promotion_eligible", "promotion_blockers", "limitations",
    }
)
_TOP_WITH_RUNTIME_ATTESTATION = _TOP | {"runtime_attestation_sha256"}
_TOP_WITH_OPERATIONAL_EVIDENCE = _TOP | {
    "promotion_basis", "operational_evidence_sha256"
}
_TOP_WITH_RUNTIME_AND_OPERATIONAL_EVIDENCE = _TOP_WITH_RUNTIME_ATTESTATION | {
    "promotion_basis", "operational_evidence_sha256"
}
_IDENTITY = frozenset(
    {
        "requested_model", "configured_model", "final_observed_model",
        "requested_reasoning_effort", "configured_reasoning_effort", "model_verification",
        "reasoning_effort_verification", "execution_identity_attestation",
        "promotion_eligible",
    }
)
_LAUNCH_REPOSITORY = frozenset(
    {"repository_common_dir_sha256", "head_oid", "tree_oid", "clean"}
)
_CLOSE_REPOSITORY = frozenset(
    {
        "repository_common_dir_sha256", "head_oid", "tree_oid", "status_sha256",
        "dirty_path_count", "clean",
    }
)
_CANDIDATE = frozenset(
    {"repository_common_dir_sha256", "base_oid", "head_oid", "tree_oid", "patch_sha256", "clean"}
)
_LAUNCH = frozenset(
    {
        "argv_sha256", "prompt_sha256", "settings_sha256", "rules_sha256", "sandbox",
        "approval_policy",
    }
)


def _exact(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProviderReceiptError(f"{name} has missing or unknown fields")
    return value


def _sha(value: Any, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ProviderReceiptError(f"{name} must be lowercase SHA-256")


def _git_oid(value: Any, name: str) -> None:
    if not isinstance(value, str) or len(value) not in {40, 64} or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ProviderReceiptError(f"{name} must be a full Git object id")


def _unique_strings(value: Any, name: str, *, nonempty: bool = False) -> None:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise ProviderReceiptError(f"{name} must be a unique array of non-empty strings")


def _operational_evidence_fields(value: Mapping[str, Any], *, provider: str) -> tuple[str, bool]:
    """Extract identity from a boundary-validated operational evidence record.

    The complete record is validated by ``operational_evidence.py`` and re-opened by custody.
    This local check prevents a receipt producer from treating an arbitrary truthy flag as proof,
    and from linking a record built for a different provider's normalization: without it, a
    receipt built with ``receipt_from_claude`` and a Codex record would be well-formed and
    promotion-eligible at construction, refused only later at ``record_run`` or ``validate``.
    """

    if not isinstance(value, Mapping):
        raise ProviderReceiptError("operational_evidence must be a mapping")
    if value.get("provider") != provider:
        raise ProviderReceiptError("operational_evidence.provider differs from the normalization")
    digest = value.get("content_sha256")
    _sha(digest, "operational_evidence.content_sha256")
    eligible = value.get("promotion_eligible")
    if type(eligible) is not bool:
        raise ProviderReceiptError("operational_evidence.promotion_eligible must be boolean")
    return digest, eligible


def _operational_thread_identity(value: Mapping[str, Any]) -> tuple[str, str]:
    """Return the post-turn provider settings carried by operational evidence.

    App Server ``thread/start`` reports the thread defaults, while ``thread/read`` after the
    turn reports the settings actually selected for that thread.  The latter is the value a
    receipt should expose when an operational evidence record is linked.  The complete evidence
    record is independently revalidated by the App Server runner and custody store; this shape
    check keeps receipt construction from silently falling back to stale start-time settings.
    """

    thread_read = value.get("thread_read")
    if not isinstance(thread_read, Mapping):
        raise ProviderReceiptError(
            "operational evidence has no provider thread/read identity"
        )
    model = thread_read.get("model")
    effort = thread_read.get("reasoning_effort")
    if not isinstance(model, str) or not model:
        raise ProviderReceiptError("operational evidence thread/read model is invalid")
    if not isinstance(effort, str) or not effort:
        raise ProviderReceiptError(
            "operational evidence thread/read reasoning effort is invalid"
        )
    return model, effort


def validate_provider_receipt(value: Any) -> dict[str, Any]:
    """Validate one closed receipt, including any linked runtime-attestation claim."""

    if not isinstance(value, dict) or set(value) not in {
        _TOP,
        _TOP_WITH_RUNTIME_ATTESTATION,
        _TOP_WITH_OPERATIONAL_EVIDENCE,
        _TOP_WITH_RUNTIME_AND_OPERATIONAL_EVIDENCE,
    }:
        raise ProviderReceiptError("provider receipt has missing or unknown fields")
    receipt = value
    if receipt["schema_version"] != "1" or receipt["record_kind"] != "provider_session_receipt":
        raise ProviderReceiptError("unsupported provider receipt schema or record kind")
    for field in ("provider", "adapter", "adapter_version", "observed_at", "cwd", "session_id"):
        if not isinstance(receipt[field], str) or not receipt[field]:
            raise ProviderReceiptError(f"{field} must be a non-empty string")
    try:
        observed = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderReceiptError("observed_at must be ISO-8601") from exc
    if observed.tzinfo is None or not Path(receipt["cwd"]).is_absolute():
        raise ProviderReceiptError("receipt timestamp must be zoned and cwd must be absolute")
    for field in ("role", "thread_id", "turn_id"):
        if receipt[field] is not None and (
            not isinstance(receipt[field], str) or not receipt[field]
        ):
            raise ProviderReceiptError(f"{field} must be a non-empty string or null")
    _unique_strings(receipt["required_capabilities"], "required_capabilities")
    _unique_strings(receipt["item_types"], "item_types")
    _unique_strings(receipt["limitations"], "limitations", nonempty=True)
    _unique_strings(receipt["promotion_blockers"], "promotion_blockers")
    if type(receipt["promotion_eligible"]) is not bool:
        raise ProviderReceiptError("promotion_eligible must be boolean")
    _sha(receipt["raw_event_sha256"], "raw_event_sha256")
    _sha(receipt["request_sha256"], "request_sha256")
    if "runtime_attestation_sha256" in receipt:
        _sha(receipt["runtime_attestation_sha256"], "runtime_attestation_sha256")
    operational_evidence_sha256 = receipt.get("operational_evidence_sha256")
    if operational_evidence_sha256 is not None:
        _sha(operational_evidence_sha256, "operational_evidence_sha256")
    if type(receipt["raw_event_count"]) is not int or receipt["raw_event_count"] < 1:
        raise ProviderReceiptError("raw_event_count must be positive")
    if receipt["terminal_state"] not in {"completed", "failed", "incomplete"}:
        raise ProviderReceiptError("terminal_state is unsupported")

    identity = _exact(receipt["identity"], _IDENTITY, "identity")
    for field in (
        "requested_model", "configured_model", "final_observed_model",
        "requested_reasoning_effort", "configured_reasoning_effort",
    ):
        if identity[field] is not None and (
            not isinstance(identity[field], str) or not identity[field]
        ):
            raise ProviderReceiptError(f"identity.{field} must be a string or null")
    verification = {"provider_observed", "launch_requested_only", "unobserved"}
    if identity["model_verification"] not in verification or identity[
        "reasoning_effort_verification"
    ] not in verification:
        raise ProviderReceiptError("identity verification is unsupported")
    if identity["execution_identity_attestation"] not in {"per_turn_attested", "unavailable"}:
        raise ProviderReceiptError("identity execution attestation is unsupported")
    runtime_attestation_sha256 = receipt.get("runtime_attestation_sha256")
    promotion_basis = receipt.get("promotion_basis")
    if identity["execution_identity_attestation"] == "per_turn_attested":
        if runtime_attestation_sha256 is None:
            raise ProviderReceiptError(
                "per-turn execution attestation requires a linked runtime attestation"
            )
    elif (
        runtime_attestation_sha256 is not None
        and promotion_basis != "operational_observed"
        and (
            "runtime_attestation_not_eligible" not in receipt["promotion_blockers"]
        )
    ):
        raise ProviderReceiptError(
            "an ineligible linked runtime attestation must be named as a promotion blocker"
        )
    if type(identity["promotion_eligible"]) is not bool:
        raise ProviderReceiptError("identity.promotion_eligible must be boolean")

    if receipt["approval_observation"] not in {"full_lifecycle", "denials_only", "unavailable"}:
        raise ProviderReceiptError("approval_observation is unsupported")
    for field in ("approval_requests", "approval_resolutions"):
        if type(receipt[field]) is not int or receipt[field] < 0:
            raise ProviderReceiptError(f"{field} must be non-negative")
    if receipt["approval_resolutions"] > receipt["approval_requests"]:
        raise ProviderReceiptError("approval resolutions exceed requests")

    launch_repo = _exact(receipt["launch_repository"], _LAUNCH_REPOSITORY, "launch_repository")
    _sha(launch_repo["repository_common_dir_sha256"], "launch_repository common dir")
    _git_oid(launch_repo["head_oid"], "launch_repository.head_oid")
    _git_oid(launch_repo["tree_oid"], "launch_repository.tree_oid")
    if launch_repo["clean"] is not True:
        raise ProviderReceiptError("launch repository must be clean")
    close_repo = _exact(receipt["close_repository"], _CLOSE_REPOSITORY, "close_repository")
    _sha(close_repo["repository_common_dir_sha256"], "close_repository common dir")
    _sha(close_repo["status_sha256"], "close_repository.status_sha256")
    _git_oid(close_repo["head_oid"], "close_repository.head_oid")
    _git_oid(close_repo["tree_oid"], "close_repository.tree_oid")
    if (
        type(close_repo["clean"]) is not bool
        or type(close_repo["dirty_path_count"]) is not int
        or close_repo["dirty_path_count"] < 0
        or close_repo["repository_common_dir_sha256"]
        != launch_repo["repository_common_dir_sha256"]
        or (close_repo["clean"] and close_repo["dirty_path_count"] != 0)
        or (not close_repo["clean"] and close_repo["dirty_path_count"] < 1)
    ):
        raise ProviderReceiptError("close repository state is inconsistent")
    candidate = receipt["candidate"]
    if candidate is not None:
        candidate = _exact(candidate, _CANDIDATE, "candidate")
        _sha(candidate["repository_common_dir_sha256"], "candidate common dir")
        _sha(candidate["patch_sha256"], "candidate.patch_sha256")
        for field in ("base_oid", "head_oid", "tree_oid"):
            _git_oid(candidate[field], f"candidate.{field}")
        if candidate["clean"] is not True:
            raise ProviderReceiptError("candidate must be clean")
        if candidate["repository_common_dir_sha256"] != launch_repo[
            "repository_common_dir_sha256"
        ] or candidate["base_oid"] != launch_repo["head_oid"] or (
            candidate["head_oid"] != close_repo["head_oid"]
            or candidate["tree_oid"] != close_repo["tree_oid"]
            or not close_repo["clean"]
        ):
            raise ProviderReceiptError(
                "candidate does not descend from the bound launch repository"
            )

    launch = _exact(receipt["launch"], _LAUNCH, "launch")
    for field in ("argv_sha256", "prompt_sha256"):
        _sha(launch[field], f"launch.{field}")
    for field in ("settings_sha256", "rules_sha256"):
        if launch[field] is not None:
            _sha(launch[field], f"launch.{field}")
    if launch["sandbox"] not in {"read-only", "workspace-write"} or launch[
        "approval_policy"
    ] not in {"never", "on-request", "untrusted"}:
        raise ProviderReceiptError("launch sandbox or approval policy is unsupported")
    if promotion_basis is not None and promotion_basis not in {
        "operational_observed",
        "per_turn_attested",
    }:
        raise ProviderReceiptError("promotion_basis is unsupported")
    if promotion_basis == "operational_observed":
        if operational_evidence_sha256 is None:
            raise ProviderReceiptError(
                "operational_observed promotion requires operational evidence"
            )
        if identity["execution_identity_attestation"] != "unavailable":
            raise ProviderReceiptError(
                "operational_observed promotion cannot claim per-turn execution attestation"
            )
        if identity["promotion_eligible"]:
            raise ProviderReceiptError(
                "operational_observed promotion cannot claim identity promotion eligibility"
            )
    elif operational_evidence_sha256 is not None:
        raise ProviderReceiptError(
            "operational evidence must name promotion_basis=operational_observed"
        )
    if identity["promotion_eligible"] and (
        identity["model_verification"] != "provider_observed"
        or identity["reasoning_effort_verification"] != "provider_observed"
        or identity["execution_identity_attestation"] != "per_turn_attested"
        or not identity["configured_model"]
        or not identity["final_observed_model"]
        or not identity["configured_reasoning_effort"]
    ):
        raise ProviderReceiptError("identity promotion eligibility lacks per-turn attestation")
    if receipt["promotion_eligible"]:
        if (
            candidate is None
            or receipt["terminal_state"] != "completed"
            or launch["settings_sha256"] is None
            or launch["rules_sha256"] is None
            or receipt["promotion_blockers"]
        ):
            raise ProviderReceiptError("promotion eligibility lacks required canonical evidence")
        if promotion_basis == "operational_observed":
            if operational_evidence_sha256 is None:
                raise ProviderReceiptError(
                    "operational promotion eligibility lacks operational evidence"
                )
        elif not identity["promotion_eligible"]:
            raise ProviderReceiptError("promotion eligibility lacks per-turn attestation")
    elif not receipt["promotion_blockers"]:
        raise ProviderReceiptError("ineligible receipt must name at least one promotion blocker")
    return receipt


def _base_receipt(
    normalization: CodexAppServerNormalization | ClaudeNormalization,
    *,
    cwd: str,
    observed_at: datetime,
    request_sha256: str,
    command_sha256: str,
    launch_repository: LaunchRepository,
    close_repository: CloseRepository,
    candidate: CandidateResult | None,
    prompt_sha256: str,
    settings_sha256: str | None,
    rules_sha256: str | None,
    sandbox: str,
    approval_policy: str,
    role: str | None,
    required_capabilities: tuple[str, ...],
    runtime_attestation_sha256: str | None = None,
    runtime_attestation_eligible: bool = False,
    operational_evidence: Mapping[str, Any] | None = None,
    operational_evidence_sha256: str | None = None,
    operational_evidence_eligible: bool = False,
) -> dict[str, Any]:
    if type(runtime_attestation_eligible) is not bool:
        raise ProviderReceiptError("runtime_attestation_eligible must be boolean")
    if runtime_attestation_eligible and runtime_attestation_sha256 is None:
        raise ProviderReceiptError(
            "runtime_attestation_eligible requires a runtime attestation digest"
        )
    if operational_evidence is not None:
        observed_digest, observed_eligible = _operational_evidence_fields(
            operational_evidence, provider=normalization.provider
        )
        if (
            operational_evidence_sha256 is not None
            and operational_evidence_sha256 != observed_digest
        ):
            raise ProviderReceiptError("operational evidence digest differs from its record")
        if operational_evidence_eligible and not observed_eligible:
            raise ProviderReceiptError("operational evidence eligibility differs from its record")
        operational_evidence_sha256 = observed_digest
        operational_evidence_eligible = observed_eligible
    elif operational_evidence_sha256 is not None or operational_evidence_eligible:
        raise ProviderReceiptError(
            "operational evidence digest and eligibility require the validated evidence record"
        )
    if isinstance(normalization, CodexAppServerNormalization):
        model_verification = normalization.identity_verification
        effort_verification = (
            normalization.identity_verification
            if normalization.configured_reasoning_effort is not None
            else (
                "launch_requested_only"
                if normalization.requested_reasoning_effort is not None
                else "unobserved"
            )
        )
        approval_observation = "full_lifecycle"
    else:
        model_verification = normalization.model_verification
        effort_verification = normalization.reasoning_effort_verification
        approval_observation = normalization.approval_observation
    configured_model = normalization.configured_model
    configured_reasoning_effort = normalization.configured_reasoning_effort
    if operational_evidence is not None and isinstance(
        normalization, CodexAppServerNormalization
    ):
        # The operational record is linked only after its raw request/response custody has been
        # validated.  Prefer its post-turn thread/read settings over thread/start defaults so the
        # receipt does not report a stale global/default effort (for example medium vs xhigh).
        configured_model, configured_reasoning_effort = _operational_thread_identity(
            operational_evidence
        )
        model_verification = "provider_observed"
        effort_verification = "provider_observed"
    limitations = list(normalization.limitations)
    # Lifecycle custody alone does not attest the model that executed each turn.  The approved
    # observational qualification basis may still establish exact executable/request identity,
    # provider-reported settings and observed hooks; it deliberately leaves backend model/effort
    # identity unverified.
    execution_attested = runtime_attestation_eligible and normalization.promotion_identity_eligible
    promotion_blockers: list[str] = []
    if operational_evidence_eligible:
        if operational_evidence_sha256 is None:
            raise ProviderReceiptError(
                "operational evidence eligibility requires an operational evidence digest"
            )
    elif not execution_attested:
        promotion_blockers.append(
            "runtime_attestation_not_eligible"
            if runtime_attestation_sha256 is not None
            else "per_turn_execution_identity_not_attested"
        )
    elif execution_attested:
        if model_verification != "provider_observed":
            promotion_blockers.append("configured_model_not_attested")
        if effort_verification != "provider_observed":
            promotion_blockers.append("configured_reasoning_effort_not_attested")
        # The runtime attestation binds lifecycle lines to an installed identity, but it does not
        # prove provider effective-source precedence or hook trust.  Keep that separate claim
        # explicit so a linked attestation cannot make a receipt promotion-eligible by itself.
    if not operational_evidence_eligible:
        promotion_blockers.append("provider_effective_sources_not_attested")
    if candidate is None:
        limitations.append("clean_committed_candidate_unavailable_after_provider_run")
        promotion_blockers.append("clean_committed_candidate_unavailable")
    if settings_sha256 is None:
        limitations.append("launch_settings_digest_unavailable")
        promotion_blockers.append("launch_settings_digest_unavailable")
    if rules_sha256 is None:
        limitations.append("launch_rules_digest_unavailable")
        promotion_blockers.append("launch_rules_digest_unavailable")
    promotion_basis = "operational_observed" if operational_evidence_sha256 is not None else None
    promotion_eligible = bool(
        operational_evidence_eligible
        and candidate is not None
        and normalization.terminal_state == "completed"
        and settings_sha256 is not None
        and rules_sha256 is not None
        and not promotion_blockers
    )
    if operational_evidence_sha256 is not None and not operational_evidence_eligible:
        promotion_blockers.append("operational_evidence_not_eligible")
    value = {
        "schema_version": "1",
        "record_kind": "provider_session_receipt",
        "provider": normalization.provider,
        "adapter": normalization.adapter,
        "adapter_version": normalization.adapter_version,
        "observed_at": observed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cwd": cwd,
        "role": role,
        "required_capabilities": list(required_capabilities),
        "session_id": normalization.session_id,
        "thread_id": normalization.thread_id,
        "turn_id": normalization.turn_id,
        "raw_event_sha256": normalization.raw_event_sha256,
        "raw_event_count": normalization.raw_event_count,
        "request_sha256": request_sha256,
        "terminal_state": normalization.terminal_state,
        "identity": {
            "requested_model": normalization.requested_model,
            "configured_model": configured_model,
            "final_observed_model": normalization.final_observed_model,
            "requested_reasoning_effort": normalization.requested_reasoning_effort,
            "configured_reasoning_effort": configured_reasoning_effort,
            "model_verification": model_verification,
            "reasoning_effort_verification": effort_verification,
            "execution_identity_attestation": (
                "per_turn_attested" if execution_attested else "unavailable"
            ),
            "promotion_eligible": False,
        },
        "approval_observation": approval_observation,
        "approval_requests": normalization.approval_requests,
        "approval_resolutions": normalization.approval_resolutions,
        "item_types": list(normalization.item_types),
        "launch_repository": launch_repository.to_dict(),
        "close_repository": close_repository.to_dict(),
        "candidate": candidate.to_dict() if candidate is not None else None,
        "promotion_eligible": promotion_eligible,
        "promotion_blockers": promotion_blockers,
        "launch": {
            "argv_sha256": command_sha256,
            "prompt_sha256": prompt_sha256,
            "settings_sha256": settings_sha256,
            "rules_sha256": rules_sha256,
            "sandbox": sandbox,
            "approval_policy": approval_policy,
        },
        "limitations": list(dict.fromkeys(limitations)),
    }
    if runtime_attestation_sha256 is not None:
        value["runtime_attestation_sha256"] = runtime_attestation_sha256
    if operational_evidence_sha256 is not None:
        value["promotion_basis"] = promotion_basis
        value["operational_evidence_sha256"] = operational_evidence_sha256
    # Operational evidence is the only path that can make the receipt eligible while backend
    # execution identity remains unavailable.  It is re-opened and verified by ProviderCustodyStore.
    if promotion_eligible:
        value["promotion_blockers"] = []
    return validate_provider_receipt(value)


def receipt_from_app_server(
    normalization: CodexAppServerNormalization, **kwargs: Any
) -> dict[str, Any]:
    return _base_receipt(normalization, **kwargs)


def receipt_from_claude(
    normalization: ClaudeNormalization,
    *,
    operational_evidence_unavailable: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """``_base_receipt`` stays untouched -- this keyword
    is handled entirely here. When a filesystem or provider-shape condition left evidence
    collection off for the turn, the ordinary receipt this produces was byte-identical to one
    where evidence was never requested at all: the same blockers, the same limitations, nothing
    naming the cause. ``operational_evidence_unavailable`` appends one machine-readable string to
    ``limitations`` (already an open string array in the schema) so the two are distinguishable
    from the receipt alone. No blocker, gate, or eligibility computation changes.
    """

    receipt = _base_receipt(normalization, **kwargs)
    if operational_evidence_unavailable is not None:
        receipt = dict(receipt)
        receipt["limitations"] = list(
            dict.fromkeys([*receipt["limitations"], operational_evidence_unavailable])
        )
        receipt = validate_provider_receipt(receipt)
    return receipt


def write_provider_receipt(value: dict[str, Any], path: Path | str) -> Path:
    validated = validate_provider_receipt(value)
    target = assert_writable(Path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(validated, indent=2, sort_keys=True).encode() + b"\n"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except FileExistsError as exc:
        raise ProviderReceiptError(f"refusing to overwrite provider receipt: {target}") from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
    return target


__all__ = [
    "CandidateResult",
    "CloseRepository",
    "LaunchRepository",
    "ProviderReceiptError",
    "argv_sha256",
    "capture_launch_repository",
    "capture_close_repository",
    "receipt_from_app_server",
    "receipt_from_claude",
    "sha256_bytes",
    "validate_provider_receipt",
    "write_provider_receipt",
]
