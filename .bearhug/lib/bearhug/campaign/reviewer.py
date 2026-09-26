"""Provider-neutral independent campaign review execution and durable receipt storage."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.campaign.review import (
    CampaignReviewError,
    CampaignReviewStore,
    build_review_receipt,
)
from bearhug.providers.app_server_client import run_codex_app_server
from bearhug.providers.claude import ClaudeLaunchContract
from bearhug.providers.claude_client import run_claude
from bearhug.providers.codex_app_server import CodexAppServerContract
from bearhug.providers.compatibility import require_supported
from bearhug.providers.final_output import ProviderFinalOutputError, strict_final_json
from bearhug.providers.policy import ProviderPolicy, provider_role
from bearhug.providers.qualification_index import QualifiedProvider
from bearhug.providers.receipt import (
    ProviderReceiptError,
    capture_launch_repository,
    validate_provider_receipt,
)


class CampaignReviewerError(RuntimeError):
    """An independent review could not establish closed provider and verdict custody."""


@dataclass(frozen=True, slots=True)
class CampaignReviewerResult:
    provider_run: Any
    provider_receipt: dict[str, Any]
    review_receipt: dict[str, Any] | None = None
    review_receipt_path: Path | None = None
    decision: tuple[str, list[dict[str, str]]] | None = None


ProviderRunner = Callable[[str, str, object, dict[str, Any]], Any]
DecisionReader = Callable[[Any], dict[str, Any]]
RunValidator = Callable[[Any], object]
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DECISION_FIELDS = frozenset({"verdict", "findings"})
_FINDING_FIELDS = frozenset({"finding_id", "severity", "summary"})
_PROVIDER_ID = {"codex": "openai-codex", "claude": "anthropic-claude"}
_ADAPTER_ID = {"codex": "codex-app-server-stdio", "claude": "claude-code-stream-json"}


def _default_runner(provider: str, prompt: str, contract: object, options: dict[str, Any]) -> Any:
    if provider == "codex":
        if not isinstance(contract, CodexAppServerContract):
            raise CampaignReviewerError("Codex review received the wrong launch contract")
        return run_codex_app_server(prompt, contract, **options)
    if provider == "claude":
        if not isinstance(contract, ClaudeLaunchContract):
            raise CampaignReviewerError("Claude review received the wrong launch contract")
        return run_claude(prompt, contract, **options)
    raise CampaignReviewerError(f"unsupported review provider {provider!r}")


def _decision(value: Any) -> tuple[str, list[dict[str, str]]]:
    if not isinstance(value, dict) or set(value) != _DECISION_FIELDS:
        raise CampaignReviewerError("review final response is not a closed decision object")
    verdict = value["verdict"]
    if verdict not in {"approve", "reject", "incomplete"}:
        raise CampaignReviewerError("review final response has an unsupported verdict")
    findings = value["findings"]
    if not isinstance(findings, list):
        raise CampaignReviewerError("review final response findings must be an array")
    checked: list[dict[str, str]] = []
    for ordinal, finding in enumerate(findings):
        if not isinstance(finding, dict) or set(finding) != _FINDING_FIELDS:
            raise CampaignReviewerError(f"review finding {ordinal} is not closed")
        if any(not isinstance(finding[field], str) for field in _FINDING_FIELDS):
            raise CampaignReviewerError(f"review finding {ordinal} fields must be strings")
        checked.append(dict(finding))
    return verdict, checked


def _write_receipt(value: dict[str, Any], path: Path | str) -> Path:
    target = Path(path)
    if target.name != f"{value.get('review_id')}.json":
        raise CampaignReviewerError("review receipt path does not bind its review_id")
    try:
        return CampaignReviewStore(target.parent).write(value)
    except CampaignReviewError as exc:
        raise CampaignReviewerError(str(exc)) from exc


def _candidate(value: Any) -> dict[str, Any]:
    """Validate the exact candidate identity supplied to an independent reviewer."""

    if not isinstance(value, dict):
        raise CampaignReviewerError("review candidate must be an object")
    fields = {
        "repository_common_dir_sha256",
        "base_oid",
        "head_oid",
        "tree_oid",
        "patch_sha256",
        "clean",
    }
    if set(value) != fields or value.get("clean") is not True:
        raise CampaignReviewerError("review candidate is not a clean closed candidate")
    for field in ("repository_common_dir_sha256", "patch_sha256"):
        digest = value.get(field)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise CampaignReviewerError(f"review candidate {field} is not SHA-256")
    for field in ("base_oid", "head_oid", "tree_oid"):
        oid = value.get(field)
        if (
            not isinstance(oid, str)
            or len(oid) not in {40, 64}
            or any(char not in "0123456789abcdef" for char in oid)
        ):
            raise CampaignReviewerError(f"review candidate {field} is not a Git object id")
    return dict(value)


def run_candidate_reviewer(
    *,
    candidate: dict[str, Any],
    review_worktree: Path | str,
    prompt: str,
    provider_policy: ProviderPolicy,
    role_name: str,
    qualified_provider: QualifiedProvider,
    provider_output_dir: Path | str,
    receipt_role: str | None = None,
    timeout_s: float = 28800.0,
    runner: ProviderRunner = _default_runner,
    run_validator: RunValidator | None = None,
    decision_reader: DecisionReader | None = strict_final_json,
    settings_sha256: str | None = None,
    rules_sha256: str | None = None,
    output_schema: dict[str, Any] | None = None,
) -> CampaignReviewerResult:
    """Run one qualified, read-only reviewer against an explicit exact candidate.

    This is the shared transport boundary for campaign, capsule, and integration review.  The
    caller owns the surrounding result schema; this helper owns provider launch/close identity,
    independent worktree custody, strict final output, and optional raw-custody validation.
    """

    exact_candidate = _candidate(candidate)
    cwd = Path(review_worktree).expanduser().resolve()
    if not cwd.is_dir() or not prompt.strip():
        raise CampaignReviewerError("review worktree and non-empty prompt are required")
    try:
        launch = capture_launch_repository(cwd)
    except ProviderReceiptError as exc:
        raise CampaignReviewerError(f"review worktree cannot be inspected: {exc}") from exc
    if (
        launch.repository_common_dir_sha256 != exact_candidate["repository_common_dir_sha256"]
        or launch.head_oid != exact_candidate["head_oid"]
        or launch.tree_oid != exact_candidate["tree_oid"]
        or not launch.clean
    ):
        raise CampaignReviewerError("review worktree does not hold the exact clean candidate")
    try:
        selected = provider_role(provider_policy, role_name)
    except Exception as exc:
        raise CampaignReviewerError(f"review role is invalid: {exc}") from exc
    observed_role = receipt_role or role_name
    if not isinstance(observed_role, str) or _ID.fullmatch(observed_role) is None:
        raise CampaignReviewerError("review receipt role is not canonical")
    if selected.sandbox != "read-only":
        raise CampaignReviewerError("campaign reviewer role must request read-only")
    if selected.provider != qualified_provider.selection_name:
        raise CampaignReviewerError("review role and qualification provider differ")
    qualified_provider.revalidate_runtime_files()
    if settings_sha256 is not None and settings_sha256 != qualified_provider.settings_sha256:
        raise CampaignReviewerError("review settings digest is not qualified provider custody")
    if rules_sha256 is not None and rules_sha256 != qualified_provider.rules_sha256:
        raise CampaignReviewerError("review rules digest is not qualified provider custody")
    try:
        require_supported(
            qualified_provider.compatibility_policy(),
            adapter=qualified_provider.adapter,
            version=qualified_provider.adapter_version,
            repository_root=qualified_provider.bundle_root,
        )
    except Exception as exc:
        raise CampaignReviewerError(f"review provider qualification is invalid: {exc}") from exc
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    contract_type = CodexAppServerContract if selected.provider == "codex" else ClaudeLaunchContract
    contract = contract_type(
        cwd=str(cwd),
        prompt_sha256=prompt_sha256,
        adapter_version=qualified_provider.adapter_version,
        requested_model=selected.model,
        requested_reasoning_effort=selected.effort,
        sandbox=selected.sandbox,
        approval_policy=selected.approval_policy,
    )
    options: dict[str, Any] = {
        "output_dir": provider_output_dir,
        "executable": str(qualified_provider.executable),
        "timeout_s": timeout_s,
        "role": observed_role,
        "required_capabilities": selected.required_capabilities,
        "settings_sha256": settings_sha256,
        "rules_sha256": rules_sha256,
    }
    options.update(
        collect_operational_evidence=True,
        settings_path=qualified_provider.settings_path,
        rules_path=qualified_provider.rules_path,
    )
    if selected.provider == "codex" and output_schema is not None:
        options["output_schema"] = output_schema
    run = runner(selected.provider, prompt, contract, options)
    try:
        provider_receipt = validate_provider_receipt(run.receipt)
    except (AttributeError, ProviderReceiptError) as exc:
        raise CampaignReviewerError(f"review provider returned no valid receipt: {exc}") from exc
    expected = _PROVIDER_ID[selected.provider]
    if (
        provider_receipt["provider"] != expected
        or provider_receipt["adapter"] != _ADAPTER_ID[selected.provider]
        or provider_receipt["adapter_version"] != qualified_provider.adapter_version
        or provider_receipt["cwd"] != str(cwd)
        or provider_receipt["role"] != observed_role
        or provider_receipt["required_capabilities"] != list(selected.required_capabilities)
        or provider_receipt["launch"]["sandbox"] != "read-only"
        or provider_receipt["launch"]["approval_policy"] != selected.approval_policy
        or provider_receipt["launch"]["settings_sha256"] != settings_sha256
        or provider_receipt["launch"]["rules_sha256"] != rules_sha256
        # Codex binds the entire client RPC stream; its prompt has its own launch digest.
        or (selected.provider == "claude" and provider_receipt["request_sha256"] != prompt_sha256)
        or provider_receipt["launch"]["prompt_sha256"] != prompt_sha256
        or provider_receipt["identity"]["requested_model"] != selected.model
        or provider_receipt["identity"]["requested_reasoning_effort"] != selected.effort
    ):
        raise CampaignReviewerError("review provider receipt differs from its launch contract")
    # Ruling (2), the reviewer half: a review launch is always read-only, so its sealed
    # validation-program set is always empty -- read-only never grants a program to run
    # (`build_claude_command` emits `--allowedTools` only on the workspace-write branch).
    # Cross-check the evidence record's own claim against that fixed sealed set here, at the
    # campaign layer, never inside a gate function.
    #
    # `contract` below is the same object passed to
    # `runner(...)` above, which is what the evidence builder reads its
    # `validation_programs` from (`claude_client.py`) -- always `()` for a read-only review
    # launch. So this check currently compares that value against itself, not against an
    # independently-held copy of the campaign's sealed set -- it catches a divergence
    # introduced between build and this later read, not a live "does the campaign's
    # currently sealed set differ from what the provider actually ran" check. That is a state
    # this design ruled and disclosed in the record's own `limitations`
    # (`claude_operational_evidence.py`); nothing here changes behavior.
    #
    # This reads the record's own top-level `validation_programs`
    # (the sealed set fed to the builder, stored and fed back at re-derivation) rather than
    # `launch_argv.validation_programs` (an observed value inverted from the raw argv, which
    # is `None` whenever inversion cannot recover a path-qualified spelling -- unrelated to
    # whether the launch was actually widened).
    #
    # Entered whenever EITHER side shows linked evidence: the
    # receipt's own `operational_evidence_sha256` link, or the returned `run` object actually
    # carrying an `operational_evidence` attribute -- see the identical fix in `author.py` for
    # the reachability argument.
    if selected.provider == "claude" and (
        provider_receipt.get("operational_evidence_sha256") is not None
        or getattr(run, "operational_evidence", None) is not None
    ):
        evidence = getattr(run, "operational_evidence", None)
        if not isinstance(evidence, Mapping):
            raise CampaignReviewerError(
                "review provider linked operational evidence but returned no record to cross-check"
            )
        observed_programs = sorted(evidence.get("validation_programs") or [])
        if observed_programs != sorted(set(contract.validation_programs)):
            raise CampaignReviewerError(
                "review provider launch validation programs do not match the capsule's "
                "sealed (empty) set"
            )
        # `qualified_provider` is already a direct parameter here, so
        # no plumbing is needed -- the mirrored block can compare immediately, the same as
        # author.py's identical check.
        observed_executable_sha256 = (evidence.get("executable") or {}).get("sha256")
        if observed_executable_sha256 != qualified_provider.executable_sha256:
            raise CampaignReviewerError(
                "review provider executable digest does not match the qualified provider"
            )
    reviewer_candidate = provider_receipt["candidate"]
    if (
        reviewer_candidate is None
        or provider_receipt["launch_repository"]["repository_common_dir_sha256"]
        != exact_candidate["repository_common_dir_sha256"]
        or provider_receipt["launch_repository"]["head_oid"] != exact_candidate["head_oid"]
        or provider_receipt["launch_repository"]["tree_oid"] != exact_candidate["tree_oid"]
        or reviewer_candidate["repository_common_dir_sha256"]
        != exact_candidate["repository_common_dir_sha256"]
        or reviewer_candidate["head_oid"] != exact_candidate["head_oid"]
        or reviewer_candidate["tree_oid"] != exact_candidate["tree_oid"]
        or reviewer_candidate["clean"] is not True
    ):
        raise CampaignReviewerError("review provider did not close on the exact candidate")
    if run_validator is not None:
        try:
            run_validator(run)
        except Exception as exc:
            raise CampaignReviewerError(
                f"review provider custody validation failed: {type(exc).__name__}"
            ) from exc
    decision = None
    if decision_reader is not None:
        try:
            decision = _decision(decision_reader(run))
        except (ProviderFinalOutputError, CampaignReviewerError) as exc:
            raise CampaignReviewerError(f"review decision is invalid: {exc}") from exc
    try:
        observed_close = capture_launch_repository(cwd)
    except (ProviderReceiptError, OSError) as exc:
        raise CampaignReviewerError(f"review worktree cannot be recaptured: {exc}") from exc
    if (
        observed_close.repository_common_dir_sha256
        != exact_candidate["repository_common_dir_sha256"]
        or observed_close.head_oid != exact_candidate["head_oid"]
        or observed_close.tree_oid != exact_candidate["tree_oid"]
        or not observed_close.clean
    ):
        raise CampaignReviewerError("review provider changed the exact candidate checkout")
    return CampaignReviewerResult(run, provider_receipt, decision=decision)


def run_campaign_reviewer(
    *,
    campaign_root: Path | str,
    campaign_id: str,
    review_id: str,
    author_provider_receipt: dict[str, Any],
    review_worktree: Path | str,
    prompt: str,
    provider_policy: ProviderPolicy,
    role_name: str,
    qualified_provider: QualifiedProvider,
    provider_output_dir: Path | str,
    receipt_role: str | None = None,
    timeout_s: float = 28800.0,
    runner: ProviderRunner = _default_runner,
    run_validator: RunValidator | None = None,
    decision_reader: DecisionReader = strict_final_json,
) -> CampaignReviewerResult:
    """Run one read-only reviewer on the author's exact immutable candidate."""

    if not isinstance(review_id, str) or _ID.fullmatch(review_id) is None:
        raise CampaignReviewerError("review_id is not canonical")
    try:
        author = validate_provider_receipt(author_provider_receipt)
    except ProviderReceiptError as exc:
        raise CampaignReviewerError(f"author provider receipt is invalid: {exc}") from exc
    candidate = author["candidate"]
    if candidate is None:
        raise CampaignReviewerError("author provider receipt has no clean candidate")
    result = run_candidate_reviewer(
        candidate=candidate,
        review_worktree=review_worktree,
        prompt=prompt,
        provider_policy=provider_policy,
        role_name=role_name,
        qualified_provider=qualified_provider,
        provider_output_dir=provider_output_dir,
        receipt_role=receipt_role,
        timeout_s=timeout_s,
        runner=runner,
        run_validator=run_validator,
        decision_reader=decision_reader,
    )
    run = result.provider_run
    provider_receipt = result.provider_receipt
    try:
        if result.decision is None:
            raise CampaignReviewerError("review provider decision was not retained")
        verdict, findings = result.decision
        review = build_review_receipt(
            campaign_id=campaign_id,
            review_id=review_id,
            author_receipt=author,
            reviewer_receipt=provider_receipt,
            verdict=verdict,
            findings=findings,
        )
    except (ProviderFinalOutputError, CampaignReviewError) as exc:
        raise CampaignReviewerError(f"review decision is invalid: {exc}") from exc
    path = _write_receipt(
        review,
        Path(campaign_root) / "review-receipts" / f"{review_id}.json",
    )
    persisted = CampaignReviewStore(path.parent).read(review_id)
    return CampaignReviewerResult(run, provider_receipt, persisted, path)


__all__ = [
    "CampaignReviewerError",
    "CampaignReviewerResult",
    "run_campaign_reviewer",
    "run_candidate_reviewer",
]
