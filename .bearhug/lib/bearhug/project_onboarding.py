"""Small, reviewable onboarding for an accepted project campaign.

The native campaign bridge historically required an operator to hand assemble three JSON
documents before it could create a :class:`~bearhug.terminal_driver.TerminalDriverConfig`.
This module derives those documents from the already accepted project board and the user's
explicit provider choices.  Derivation is read-only.  Materialization is a separate, digest
checked operation and only uses a qualification index selected from an exact, named Bear Hug
authority location.

This is deliberately a thin adapter around the existing terminal driver and capsule preparation
contracts.  It does not launch a provider, run a validation command, create a campaign journal,
or infer a newer qualification artifact from a directory.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import stat
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug import project_work as work
from bearhug import terminal_driver as terminal
from bearhug.campaign import grounding as grounding_mod
from bearhug.campaign.capsules import CANONICAL_ALGORITHM
from bearhug.campaign.grounding import (
    VOLATILE_GROUNDING_FIELDS,
    Grounding,
    GroundingError,
    compile_grounding,
    read_project_excludes,
)
from bearhug.paths import RUNS_DIR
from bearhug.providers.policy import CAPABILITIES_BY_PROVIDER, validate_provider_policy
from bearhug.providers.qualification_index import (
    ProviderQualificationIndexError,
    bind_current_provider_runtime,
    load_provider_qualification_index,
)

SCHEMA_VERSION = "1"
RECORD_KIND = "bearhug_project_onboarding_draft"
# Bump when onboarding derives something it did not derive before. A saved draft is
# reused verbatim while the accepted plan is unchanged, so without this marker an
# existing profile never picks up a new derivation and silently keeps the old config.
DERIVATION_VERSION = "2-lease-materialized-paths"
QUALIFICATION_ENV = "BEARHUG_QUALIFICATION_INDEX"

# These are exact authority locations.  In particular, this code intentionally does not glob
# under runs/ or select the most recently modified file.
CENTRAL_QUALIFICATION_INDEX = RUNS_DIR / "provider-qualification" / "qualification-index.json"
LOCAL_QUALIFICATION_INDEX = ".bearhug/qualification-index.json"
PROFILE_PATH = ".bearhug/terminal-driver.json"

_PROVIDERS = frozenset({"claude", "codex"})
_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_MAX_DRAFT_BYTES = 1024 * 1024


class OnboardingError(ValueError):
    """The onboarding draft or its approval cannot be accepted."""


class OnboardingBlocked(OnboardingError):
    """A draft is reviewable, but one or more required gates remain open."""

    def __init__(self, message: str, *, blockers: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.blockers = tuple(blockers)


@dataclass(frozen=True, slots=True)
class QualificationObservation:
    """The exact qualification authority selected for a draft."""

    status: str  # qualified | unavailable | unqualified
    path: Path | None
    digest: str | None
    provider: str
    reason: str
    runtime_binding: dict[str, Any] | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": None if self.path is None else self.path.as_posix(),
            "sha256": self.digest,
            "provider": self.provider,
            "reason": self.reason,
            **(
                {"runtime_binding": self.runtime_binding}
                if self.runtime_binding is not None else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class OnboardingDraft:
    """Pure, reviewable onboarding output.

    ``template``, ``policy`` and ``execution`` are proposed JSON values.  They are not files and
    have no authority until :func:`approve_draft` verifies the draft digest and writes them into
    the private campaign state root.  A draft with an unavailable qualification is useful for
    explaining the product limitation, but cannot be approved.
    """

    subject: Path
    plan_path: str | None
    plan_sha256: str | None
    provider: str
    model: str | None
    effort: str | None
    validation_commands: tuple[tuple[str, tuple[str, ...]], ...]
    template: dict[str, Any] | None
    policy: dict[str, Any] | None
    execution: dict[str, Any] | None
    qualification: QualificationObservation
    state_root: Path
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    grounding: dict[str, Any] | None
    digest: str

    @property
    def ready(self) -> bool:
        return not self.blockers and self.template is not None

    @property
    def template_value(self) -> dict[str, Any] | None:
        return None if self.template is None else json.loads(json.dumps(self.template))

    @property
    def policy_value(self) -> dict[str, Any] | None:
        return None if self.policy is None else json.loads(json.dumps(self.policy))

    @property
    def execution_value(self) -> dict[str, Any] | None:
        return None if self.execution is None else json.loads(json.dumps(self.execution))

    def to_mapping(self) -> dict[str, Any]:
        """Return the canonical review projection used to compute ``digest``."""

        return _draft_mapping(self, include_digest=False)

    def review(self) -> dict[str, Any]:
        """Return a JSON-safe review projection, including the draft digest."""

        return _draft_mapping(self, include_digest=True)


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    """Materialization result returned by :func:`approve_draft`."""

    draft: OnboardingDraft
    config: terminal.TerminalDriverConfig
    config_path: Path
    template_path: Path
    policy_path: Path
    execution_path: Path

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": "configured",
            "draft_sha256": self.draft.digest,
            "config_path": self.config_path.as_posix(),
            "template_path": self.template_path.as_posix(),
            "policy_path": self.policy_path.as_posix(),
            "execution_path": self.execution_path.as_posix(),
            "qualification": self.draft.qualification.to_mapping(),
        }


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OnboardingError(f"onboarding value is not canonical JSON: {exc}") from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _physical_root(root: Path | str) -> Path:
    path = Path(root).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise OnboardingError("project root must be an absolute physical directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise OnboardingError(f"project root is unavailable: {path}") from exc
    if resolved != path or not path.is_dir():
        raise OnboardingError("project root must be an absolute physical directory")
    return path


def _physical_file(path: Path | str, *, label: str) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() or value.is_symlink():
        raise OnboardingError(f"{label} must be an absolute physical file")
    try:
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise OnboardingError(f"{label} is unavailable: {value}") from exc
    if resolved != value or not value.is_file():
        raise OnboardingError(f"{label} must be an absolute physical file")
    metadata = value.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise OnboardingError(f"{label} must be a user-owned regular file")
    return value


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_DRAFT_BYTES:
            raise OnboardingError(f"{label} exceeds the onboarding byte limit")
        value = json.loads(raw.decode("utf-8"))
    except OnboardingError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OnboardingError(f"{label} is not one UTF-8 JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise OnboardingError(f"{label} must contain one JSON object")
    return value


def _validate_provider(provider: str) -> str:
    if provider not in _PROVIDERS:
        raise OnboardingError(f"unsupported provider: {provider!r}")
    return provider


def _validate_model(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\x00\r\n"):
        raise OnboardingError("model must be non-empty single-line text")
    return value.strip()


def _validate_effort(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in _EFFORTS:
        raise OnboardingError(f"unsupported reasoning effort: {value!r}")
    return value


def _argv(value: str | Sequence[str], *, label: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            parts = tuple(shlex.split(value))
        except ValueError as exc:
            raise OnboardingError(f"{label} is not a valid command: {exc}") from exc
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts = tuple(value)
    else:
        raise OnboardingError(f"{label} must be a command string or argv array")
    if not parts or any(not isinstance(item, str) or not item or "\x00" in item for item in parts):
        raise OnboardingError(f"{label} must be a non-empty argv command")
    return parts


def _known_validation_commands(root: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return checks named by repository files, with stable precedence and no probing."""

    result: list[tuple[str, tuple[str, ...]]] = []
    if (
        (root / "pyproject.toml").is_file()
        or (root / "pytest.ini").is_file()
        or (root / "tox.ini").is_file()
        or (root / "setup.cfg").is_file()
    ):
        result.append(("validation.python", ("python3", "-m", "pytest")))
    if (root / "go.mod").is_file():
        result.append(("validation.go", ("go", "test", "./...")))
    if (root / "Cargo.toml").is_file():
        result.append(("validation.rust", ("cargo", "test")))
    package = root / "package.json"
    if package.is_file():
        try:
            value = _read_json(package, label="package.json")
        except OnboardingError:
            value = {}
        if isinstance(value.get("scripts"), Mapping) and isinstance(
            value["scripts"].get("test"), str
        ):
            result.append(("validation.node", ("npm", "test")))
    makefile = root / "Makefile"
    if makefile.is_file() and any(
        line.lstrip().startswith("test:") or line.lstrip().startswith("test ")
        for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines()
    ):
        result.append(("validation.make", ("make", "test")))
    return tuple(result)


def _commands(
    root: Path,
    validate: str | Sequence[str] | Sequence[Sequence[str]] | None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if validate is None:
        result = _known_validation_commands(root)
    elif isinstance(validate, str) or (
        isinstance(validate, Sequence)
        and validate
        and all(isinstance(item, str) for item in validate)
    ):
        result = (("validation.accepted-plan", _argv(validate, label="validate")),)
    elif isinstance(validate, Sequence):
        result = tuple(
            (f"validation.accepted-plan.{index + 1}", _argv(item, label=f"validate[{index}]"))
            for index, item in enumerate(validate)
        )
    else:
        raise OnboardingError("validate must be a command or sequence of commands")
    by_ref: dict[str, tuple[str, ...]] = {}
    for ref, command in result:
        if ref in by_ref:
            raise OnboardingError(f"validation command repeats reference {ref!r}")
        by_ref[ref] = command
    return tuple(sorted(by_ref.items()))


def _settings_choices(root: Path, provider: str) -> tuple[str | None, str | None]:
    """Read only exact provider settings keys; never infer a provider from installed binaries."""

    if provider == "claude":
        path = root / ".claude" / "settings.json"
        if not path.is_file():
            return None, None
        value = _read_json(path, label="Claude settings")
        env = value.get("env") if isinstance(value.get("env"), Mapping) else {}
        model = value.get("model") or env.get("CLAUDE_MODEL")
        effort = value.get("effort") or env.get("CLAUDE_REASONING_EFFORT")
        return (
            model if isinstance(model, str) else None,
            effort if isinstance(effort, str) else None,
        )
    path = root / ".codex" / "config.toml"
    if not path.is_file():
        return None, None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise OnboardingError(f"Codex settings are not valid TOML: {exc}") from exc
    model = value.get("model")
    effort = value.get("model_reasoning_effort")
    return model if isinstance(model, str) else None, effort if isinstance(effort, str) else None


def _qualification(
    root: Path, provider: str, explicit: Path | str | None
) -> QualificationObservation:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(_physical_file(explicit, label="qualification index"))
    else:
        env_value = os.environ.get(QUALIFICATION_ENV)
        if env_value:
            candidates.append(_physical_file(env_value, label="qualification index"))
        candidates.append(root / LOCAL_QUALIFICATION_INDEX)
        candidates.append(CENTRAL_QUALIFICATION_INDEX)
    selected = next((path for path in candidates if path.is_file() and not path.is_symlink()), None)
    if selected is None:
        return QualificationObservation(
            "unavailable",
            None,
            None,
            provider,
            "Bear Hug has no named current qualification index for this provider.",
        )
    try:
        # Both providers: adapter evidence, executable and version stay strict; the user
        # settings and rules digests are re-bound to their current bytes and sealed into this
        # draft, so an ordinary settings edit (a model switch) is a re-onboard, not a
        # re-qualification. `approve_draft` re-derives and refuses if they moved since review.
        digest, runtime_binding = bind_current_provider_runtime(selected, provider)
    except (ProviderQualificationIndexError, OSError, ValueError) as exc:
        return QualificationObservation(
            "unqualified",
            selected,
            _sha256(selected.read_bytes()) if selected.is_file() else None,
            provider,
            f"Bear Hug qualification evidence is unavailable for {provider}: {exc}",
        )
    return QualificationObservation(
        "qualified", selected, digest, provider,
        f"Adapter evidence verified; current {provider} settings/rules are bound in this proposal.",
        runtime_binding,
    )


def _policy(provider: str, model: str, effort: str) -> dict[str, Any]:
    capabilities = sorted(CAPABILITIES_BY_PROVIDER[provider])
    return {
        "schema_version": "1",
        "roles": {
            "author": {
                "provider": provider,
                "model": model,
                "effort": effort,
                "sandbox": "workspace-write",
                "approval_policy": "never",
                "required_capabilities": capabilities,
            },
            "reviewer": {
                "provider": provider,
                "model": model,
                "effort": effort,
                "sandbox": "read-only",
                "approval_policy": "never",
                "required_capabilities": sorted(
                    capability
                    for capability in capabilities
                    if capability not in {"workspace-editing", "shell-tools"}
                ),
            },
        },
    }


def _template(
    root: Path,
    plan_path: str,
    plan_digest: str,
    policy_digest: str,
    accepted_at: str,
    capabilities: Sequence[str],
    grounding: Grounding | None = None,
) -> dict[str, Any]:
    approval = {
        "mode": "human_approved",
        "evidence_refs": [plan_digest],
        "approved_at": accepted_at,
        "approved_by": "operator.accepted-plan",
    }
    mutation = {
        "claim_set_id": "claims.accepted-plan",
        "path_prefixes": ["."],
        "symbols": [],
        "subjects": [],
        "semantic_resources": [],
        "ports": [],
        "data_directories": [],
    }
    snapshot = {
        "policies": {
            # attempt_limit - 1 is the capsule's repair budget. At 1 it was 0: the first
            # failed validation blocked the capsule with no repair episode, measured on
            # row 240 T2 -- 24 minutes of provider work, then blocked in under a second.
            "dispatch": {"max_parallel": 1, "attempt_limit": 4},
            "review": {
                "minimum_approvals": 1,
                "independence": "separate_worktree",
                "require_fresh_receipt": True,
            },
            "integration": {
                "mode": "serialized",
                "integrator_work_unit_id": "integrator.accepted-plan",
                "require_green_gate": True,
            },
            "expiry": {"maximum_run_seconds": 86400, "on_expiry": "stop"},
        },
        "human_in_loop": {
            "checkpoints": [
                "before_launch",
                "before_retry",
                "before_integration",
                "on_block",
                "on_authority_change",
                "on_scope_change",
            ],
            "decision_timeout_seconds": 3600,
            "on_timeout": "stop",
            "required_for_stop_override": True,
        },
        "stop_conditions": [
            "authority_mismatch",
            "base_drift",
            "capability_unavailable",
            "gate_failure",
            "provider_incompatible",
        ],
        "capability_refs": sorted(capabilities),
    }
    bindings: list[dict[str, Any]] = [
        {
            "binding_id": "binding.accepted-plan",
            "term": "accepted plan",
            "meaning": "The exact accepted board plan bytes named by the project work state.",
            "state": "accepted",
            "evidence_refs": [plan_digest],
        }
    ]
    invariants: list[dict[str, Any]] = [
        {
            "invariant_id": "invariant.accepted-plan",
            "statement": "Execution remains bounded by the accepted plan and its project scope.",
            "origin": "project_sealed",
            "evidence_refs": [plan_digest],
        }
    ]
    if grounding is not None:
        # Grounded bindings and invariants come from the project's own decision records, by
        # digest.  They extend the plan binding; they never replace or outrank it.
        bindings.extend(copy.deepcopy(row) for row in grounding.bindings)
        invariants.extend(copy.deepcopy(row) for row in grounding.invariants)
    return {
        "schema_version": "1",
        "record_kind": "intent_envelope",
        "canonical_algorithm": CANONICAL_ALGORITHM,
        "intent_envelope_id": "intent.onboarding." + plan_digest[:24],
        "mode": "native_v2",
        "goal": f"Execute accepted plan {plan_path} (SHA-256 {plan_digest})",
        "intent": f"Execute the exact accepted plan {plan_path} from the project board.",
        "constraints": [
            "Use only the accepted plan authority and Bear Hug qualification evidence."
        ],
        "non_goals": ["provider qualification", "default activation", "automatic spending"],
        "authority_refs": [
            {
                "source_id": "accepted-plan",
                "content_sha256": plan_digest,
                "scopes": ["accepted-plan"],
            }
        ],
        "obligations": [
            {
                "source_id": "accepted-plan",
                "obligation_id": "obligation.accepted-plan",
                "statement": f"Complete the accepted plan {plan_path}.",
            }
        ],
        "bindings": bindings,
        "invariants": invariants,
        "campaign_envelope": {
            "envelope_id": "envelope.onboarding." + plan_digest[:24],
            "mutation_envelope": mutation,
            "policy_refs": [policy_digest],
            "policy_snapshot": snapshot,
            "risk": {"level": "medium", "max_repair_episodes": 0},
            "budget": {
                "max_seconds": None,
                "max_provider_tokens": None,
                "max_provider_spend_cents": None,
                "unknown_limits": [
                    "max_seconds",
                    "max_provider_tokens",
                    "max_provider_spend_cents",
                ],
            },
            "approval": approval,
        },
        "approval": approval,
    }


def _draft_mapping(draft: OnboardingDraft, *, include_digest: bool) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": RECORD_KIND,
        "subject": draft.subject.as_posix(),
        "plan_path": draft.plan_path,
        "plan_sha256": draft.plan_sha256,
        "provider": draft.provider,
        "model": draft.model,
        "effort": draft.effort,
        "validation_commands": {ref: list(command) for ref, command in draft.validation_commands},
        "template": draft.template,
        "policy": draft.policy,
        "execution": draft.execution,
        "qualification": draft.qualification.to_mapping(),
        "state_root": draft.state_root.as_posix(),
        "grounding": draft.grounding,
        "blockers": list(draft.blockers),
        "warnings": list(draft.warnings),
    }
    if include_digest:
        value["draft_sha256"] = draft.digest
    return value


def _stable_grounding(value: Any) -> Any:
    """Drop wall-clock fields from the grounding a draft seals.

    The draft digest covers grounding. A knowledge tool reports when it last looked, and one
    of those stamps moves with the clock rather than with any file, so two derivations
    minutes apart produced two digests. The operator was handed a digest that was already
    stale, approval refused it, and the refusal wrote a third digest. Measured on 2026-09-17:
    two derivations eight minutes apart differed only by `updated_at`.

    What grounding found belongs in the draft. When it looked does not: it changes nothing
    about the proposal being approved, and the receipts still carry the time the run happened.
    """

    if isinstance(value, Mapping):
        return {
            key: _stable_grounding(inner)
            for key, inner in value.items()
            if key not in VOLATILE_GROUNDING_FIELDS
        }
    if isinstance(value, list):
        return [_stable_grounding(row) for row in value]
    return value


def _check_lease_path(subject: Path | str, relative: str) -> None:
    """Refuse a declared lease path at proposal time, not eleven minutes into an episode."""

    from bearhug.campaign.materialize import (
        LeaseMaterializationError,
        _check_shape,
        _git_ignored,
    )

    try:
        _check_shape(relative)
        ignored = _git_ignored(Path(subject), relative)
    except LeaseMaterializationError as exc:
        raise OnboardingError(f"declared lease path is unusable: {exc}") from exc
    if not ignored:
        raise OnboardingError(
            f"declared lease path is not Git-ignored in the subject: {relative}; "
            "a lease already carries everything Git tracks"
        )


def draft_from_mapping(value: Mapping[str, Any]) -> OnboardingDraft:
    """Rebuild the exact draft a saved review projection describes.

    Approval must apply the draft whose digest the operator was handed, not a freshly
    derived one that ought to match it. Deriving again re-runs the lease-evidence probe,
    and because the digest covers that probe's own output -- both ``execution`` and the
    ``warnings`` it appends on timeout -- anything the second probe observes differently
    invalidates the digest the operator is holding, and the approval cannot land.

    The reconstruction is self-checking: the digest is recomputed over the rebuilt draft,
    so a projection that lost or altered any field the digest covers cannot be approved.
    """

    if not isinstance(value, Mapping):
        raise OnboardingError("saved onboarding draft is not a mapping")
    if value.get("schema_version") != SCHEMA_VERSION or value.get("record_kind") != RECORD_KIND:
        raise OnboardingError("saved onboarding draft was written by a different schema")
    digest = value.get("draft_sha256")
    if not isinstance(digest, str) or not digest:
        raise OnboardingError("saved onboarding draft carries no digest")
    qualification = value.get("qualification")
    commands = value.get("validation_commands")
    subject = value.get("subject")
    state_root = value.get("state_root")
    if not isinstance(qualification, Mapping):
        raise OnboardingError("saved onboarding draft carries no qualification")
    if not isinstance(commands, Mapping):
        raise OnboardingError("saved onboarding draft carries no validation commands")
    if not isinstance(subject, str) or not isinstance(state_root, str):
        raise OnboardingError("saved onboarding draft carries no subject or state root")
    qualification_path = qualification.get("path")
    try:
        draft = OnboardingDraft(
            subject=Path(subject),
            plan_path=value.get("plan_path"),
            plan_sha256=value.get("plan_sha256"),
            provider=value.get("provider"),
            model=value.get("model"),
            effort=value.get("effort"),
            validation_commands=tuple((ref, tuple(argv)) for ref, argv in commands.items()),
            template=value.get("template"),
            policy=value.get("policy"),
            execution=value.get("execution"),
            qualification=QualificationObservation(
                status=qualification.get("status"),
                path=None if qualification_path is None else Path(qualification_path),
                digest=qualification.get("sha256"),
                provider=qualification.get("provider"),
                reason=qualification.get("reason"),
                runtime_binding=qualification.get("runtime_binding"),
            ),
            state_root=Path(state_root),
            blockers=tuple(value.get("blockers") or ()),
            warnings=tuple(value.get("warnings") or ()),
            grounding=value.get("grounding"),
            digest=digest,
        )
    except (TypeError, ValueError) as exc:
        raise OnboardingError(f"saved onboarding draft is malformed: {exc}") from exc
    if _sha256(_canonical(draft.to_mapping())) != digest:
        raise OnboardingError("saved onboarding draft does not match its own digest")
    return draft


def derive_draft(
    root: Path | str,
    provider: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    validate: str | Sequence[str] | Sequence[Sequence[str]] | None = None,
    qualification_index: Path | str | None = None,
    state_root: Path | str | None = None,
    consult: Sequence[str] = ("memex", "architecture", "graft", "memq"),
    lease_paths: Sequence[str] | None = None,
    lease_probe: bool = False,
    tool_runner: Any = None,
    max_tool_terms: int | None = None,
    max_decisions: int | None = None,
    graft_per_query_hits: int | None = None,
    max_tool_hits: int | None = None,
    task_terms_first: bool | None = None,
) -> OnboardingDraft:
    """Derive a reviewable draft without writing files, running checks, or launching a provider.

    ``consult`` names the read-only project knowledge the grounding compiler may read (Memex
    decisions, the architecture index, Graft, MemQ); ``tool_runner`` lets tests substitute the
    launcher runner.  Grounding is part of the reviewable draft and bound by its digest.

    ``max_tool_terms``, ``max_decisions``, ``graft_per_query_hits`` and ``max_tool_hits`` are the
    operator-settable grounding limits (``None`` keeps each default; ``max_decisions`` defaults to
    16, previously 8 -- pass 8 explicitly for the old default). ``task_terms_first``
    (``None`` keeps the current default, ``True`` by default, previously ``False``) opts into
    ranking the plan's task-table terms ahead of its other prose for the tool-term query slots --
    it can change which decisions MemQ discovers and therefore which decisions bind, not just
    which terms are reported as queried; pass ``False`` (``--no-task-terms-first`` on the CLI) for
    the previous ranking; see ``grounding.compile_grounding``. All five are validated here,
    before grounding is attempted, so a value outside its bound refuses the whole proposal with a
    clear message rather than being swallowed into "grounding was not compiled" below: an
    out-of-bounds option is an operator input error, not an environmental grounding failure.

    Grounding's own decision-matching floor (``select_decisions``' ``minimum_score``) also moved
    3 -> 4 in this release, with a decision now also able to satisfy the internal "strong" match
    requirement by accumulating enough ordinary lexical hits (GS-02's recommended default). This
    has no operator-settable option here or on the CLI; it is a code default of
    ``grounding.select_decisions``.
    """

    subject = _physical_root(root)
    provider = _validate_provider(provider)
    selected_model = _validate_model(model)
    selected_effort = _validate_effort(effort)
    commands = _commands(subject, validate)
    effective_max_tool_terms = grounding_mod.resolve_tunable_limit("max_tool_terms", max_tool_terms)
    effective_max_decisions = grounding_mod.resolve_tunable_limit("max_decisions", max_decisions)
    effective_task_terms_first = grounding_mod.resolve_tunable_flag(
        "task_terms_first", task_terms_first, default=grounding_mod.TASK_TERMS_FIRST_DEFAULT
    )
    effective_graft_per_query_hits = grounding_mod.resolve_tunable_limit(
        "graft_per_query_hits", graft_per_query_hits
    )
    effective_max_tool_hits = grounding_mod.resolve_tunable_limit("max_tool_hits", max_tool_hits)
    if selected_model is None or selected_effort is None:
        existing_model, existing_effort = _settings_choices(subject, provider)
        selected_model = selected_model or _validate_model(existing_model)
        selected_effort = selected_effort or _validate_effort(existing_effort)

    state = work._load(subject)
    blockers: list[str] = []
    warnings: list[str] = []
    plan_path: str | None = None
    plan_digest: str | None = None
    accepted_at: str | None = None
    plan_text: str | None = None
    if not state:
        blockers.append("Accept a project plan before configuring execution.")
    else:
        plan_path = state.get("plan", {}).get("path")
        try:
            plan_text, observed_digest = work._read_plan(subject, plan_path)
            state_digest = state.get("plan", {}).get("sha256")
            if not isinstance(state_digest, str) or observed_digest != state_digest:
                blockers.append(
                    "Accepted plan changed; review and accept its current SHA-256 before "
                    "onboarding."
                )
            else:
                plan_digest = state_digest
                accepted = next(
                    (
                        row
                        for row in reversed(state.get("history", []))
                        if isinstance(row, Mapping)
                        and row.get("action") == "accepted"
                        and row.get("sha256") == state_digest
                    ),
                    None,
                )
                raw_at = accepted.get("at") if isinstance(accepted, Mapping) else None
                if not isinstance(raw_at, str):
                    blockers.append("Accepted plan has no Bear Hug approval timestamp.")
                else:
                    try:
                        accepted_at = (
                            datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
                            .astimezone(UTC)
                            .strftime("%Y-%m-%dT%H:%M:%SZ")
                        )
                    except ValueError:
                        blockers.append("Accepted plan approval timestamp is invalid.")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            blockers.append(f"Accepted plan cannot be read: {exc}")
    if selected_model is None:
        blockers.append(
            "Choose a provider model (or add the exact model to existing project settings)."
        )
    if selected_effort is None:
        blockers.append(
            "Choose a reasoning effort (or add the exact effort to existing project settings)."
        )
    if not commands:
        blockers.append("Choose at least one validation command with --validate.")

    qualification = _qualification(subject, provider, qualification_index)
    if qualification.status != "qualified":
        blockers.append(qualification.reason)

    effective_state_root = (
        Path(state_root).expanduser().absolute()
        if state_root is not None
        else RUNS_DIR / "project-campaigns" / hashlib.sha256(str(subject).encode()).hexdigest()[:24]
    )
    effective_state_root = effective_state_root.resolve(strict=False)
    policy: dict[str, Any] | None = None
    template: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    grounding: Grounding | None = None
    grounding_mapping: dict[str, Any] | None = None
    if plan_path and plan_digest and plan_text is not None:
        try:
            all_tasks = work.parse_tasks(plan_text)
        except work.WorkError:
            all_tasks = []
        # Grounding must never see an interactive-lane task's real Task/Done-when text: neither
        # as a term source (the projected plan text below drops its row's real content, marker
        # included) nor as one of the rows `compile_grounding` reads titles/done-when from
        # directly. It is not part of this campaign's candidate, so it earns no binding.
        tasks = [
            row for row in all_tasks if row.get("lane", work.DEFAULT_LANE) == work.DEFAULT_LANE
        ]
        try:
            grounding_plan_text = work.project_lane_text(plan_text)
        except work.WorkError:
            grounding_plan_text = plan_text
        try:
            grounding = compile_grounding(
                subject,
                plan_text=grounding_plan_text,
                tasks=tasks,
                goal=state.get("plan", {}).get("title") if state else None,
                consult=consult,
                tool_runner=tool_runner,
                max_tool_terms=effective_max_tool_terms,
                max_decisions=effective_max_decisions,
                graft_per_query_hits=effective_graft_per_query_hits,
                max_tool_hits=effective_max_tool_hits,
                task_terms_first=effective_task_terms_first,
                exclude_paths=read_project_excludes(subject),
            )
            grounding_mapping = _stable_grounding(grounding.to_mapping())
        except (GroundingError, OSError, ValueError) as exc:
            grounding = None
            grounding_mapping = {"status": "unavailable", "reason": str(exc)}
            warnings.append(f"Grounding was not compiled: {exc}")
    if plan_path and plan_digest and accepted_at and selected_model and selected_effort:
        policy = _policy(provider, selected_model, selected_effort)
        try:
            validate_provider_policy(policy)
        except ValueError as exc:
            raise OnboardingError(f"derived provider policy is invalid: {exc}") from exc
        policy_digest = _sha256(
            json.dumps(
                policy,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        template = _template(
            subject,
            plan_path,
            plan_digest,
            policy_digest,
            accepted_at,
            policy["roles"]["author"]["required_capabilities"],
            grounding,
        )
        from bearhug.campaign.capsules import validate_intent_envelope

        try:
            validate_intent_envelope(template)
        except Exception as exc:
            raise OnboardingError(f"derived intent template is invalid: {exc}") from exc
        execution = {
            "authority_sources": [{"source_id": "accepted-plan", "path": plan_path}],
            "validation_commands": {ref: list(command) for ref, command in commands},
            "artifact_bindings": {"capsule.request": {}},
            "system_checks": [list(command) for _ref, command in commands],
            "qualification_index": (
                None if qualification.path is None else qualification.path.as_posix()
            ),
            "review_role": "reviewer",
            "checkpoint": "capsule",
        }
        if grounding is not None and grounding.sources:
            execution["grounding_sources"] = [dict(row) for row in grounding.sources]
        # A leased worktree carries tracked files only. Run the validation commands in a
        # throwaway lease and declare the Git-ignored evidence they were observed to be
        # missing, so a gate cannot fail there for a reason no capsule can repair.
        from bearhug.campaign.materialize import (
            DerivedMaterialization,
            derive_lease_materialized_paths,
        )

        # Declaring is exact and free; probing runs the whole validation suite in a
        # throwaway worktree to guess the same list, and cannot be complete -- a command
        # that fails silently on a missing file names nothing. So the probe is a discovery
        # tool the operator asks for, not a tax on every proposal.
        if lease_paths is not None:
            for relative in lease_paths:
                _check_lease_path(subject, relative)
            derived = DerivedMaterialization(tuple(lease_paths), "declared", None, ())
        elif lease_probe:
            try:
                derived = derive_lease_materialized_paths(
                    subject=Path(subject), commands=[command for _ref, command in commands]
                )
            except Exception as exc:  # a probe must never prevent onboarding
                derived = None
                warnings.append(
                    "lease evidence probe did not run "
                    f"({type(exc).__name__}); declared lease paths are empty"
                )
        else:
            # An empty list must not read as "nothing was missing" when nobody looked.
            derived = DerivedMaterialization(
                (), "undetermined",
                "neither declared with --lease-path nor measured with --lease-probe",
                (),
            )
        if derived is not None:
            if derived.paths:
                execution["lease_materialized_paths"] = list(derived.paths)
            for command in derived.red_at_base:
                # A gate that is red before the work starts cannot be repaired by the
                # work. Say so at proposal time rather than after the provider run.
                blockers.append(
                    f"Validation command is already failing on the unchanged base: "
                    f"{command}. No capsule can repair a failure its work did not cause. "
                    "Fix it in the subject, or seal a command that can pass."
                )
            if derived.status not in {"observed", "declared"}:
                warnings.append(
                    f"lease evidence {derived.status}: {derived.reason}; "
                    "declared lease paths are empty"
                )
        if qualification.path is None:
            execution["qualification_index"] = "<qualification unavailable>"
    if provider == "codex":
        warnings.append(
            "Codex execution requires captured operational evidence on every run; "
            "backend model identity remains unverified."
        )
    seed = _draft_mapping(
        OnboardingDraft(
            subject,
            plan_path,
            plan_digest,
            provider,
            selected_model,
            selected_effort,
            commands,
            template,
            policy,
            execution,
            qualification,
            effective_state_root,
            tuple(blockers),
            tuple(warnings),
            grounding_mapping,
            "",
        ),
        include_digest=False,
    )
    digest = _sha256(_canonical(seed))
    return OnboardingDraft(
        subject,
        plan_path,
        plan_digest,
        provider,
        selected_model,
        selected_effort,
        commands,
        template,
        policy,
        execution,
        qualification,
        effective_state_root,
        tuple(blockers),
        tuple(warnings),
        grounding_mapping,
        digest,
    )


def _write_private(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise OnboardingError(f"refusing symlinked onboarding path: {path}")
    if path.exists() and path.read_bytes() != raw:
        raise OnboardingError(f"onboarding input already exists with different bytes: {path}")
    if not path.exists():
        terminal._write_atomic(path, raw, mode=mode, private_parent=True)


def approve_draft(
    draft: OnboardingDraft,
    *,
    expected_sha256: str,
    approved_by: str = "operator",
    replace_profile_sha256: str | None = None,
) -> OnboardingResult:
    """Materialize one exact draft into existing terminal-driver inputs after explicit approval."""

    if not isinstance(expected_sha256, str) or expected_sha256 != draft.digest:
        raise OnboardingError("approved draft digest does not match the reviewable draft")
    if (
        not isinstance(approved_by, str)
        or not approved_by
        or any(c in approved_by for c in "\x00\r\n")
    ):
        raise OnboardingError("approved_by must be bounded single-line text")
    if draft.blockers or draft.template is None or draft.policy is None or draft.execution is None:
        raise OnboardingBlocked(
            "project onboarding cannot materialize this draft",
            blockers=draft.blockers,
        )
    if draft.qualification.status != "qualified" or draft.qualification.path is None:
        raise OnboardingBlocked(
            "project onboarding requires current Bear Hug qualification evidence",
            blockers=(draft.qualification.reason,),
        )
    if _sha256(_canonical(draft.to_mapping())) != draft.digest:
        raise OnboardingError("onboarding draft changed after review")
    current_qualification = _qualification(draft.subject, draft.provider, draft.qualification.path)
    if current_qualification.to_mapping() != draft.qualification.to_mapping():
        raise OnboardingError(
            "qualification or runtime settings changed; refresh the onboarding proposal"
        )
    root = draft.subject
    state_root = draft.state_root
    if root == state_root or root in state_root.parents:
        raise OnboardingError("onboarding state root must be outside the project")
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(state_root.stat().st_mode) & 0o077:
        raise OnboardingError("onboarding state root must be owner-only")
    inputs = state_root / "onboarding-inputs" / draft.digest
    template_path = inputs / "intent-template.json"
    policy_path = inputs / "provider-policy.json"
    execution_path = inputs / "execution.json"
    _write_private(template_path, _canonical(draft.template))
    _write_private(policy_path, _canonical(draft.policy))
    execution = dict(draft.execution)
    qualification_path = draft.qualification.path
    if draft.qualification.runtime_binding is not None:
        qualification_path = inputs / "qualification-index.json"
        _write_private(qualification_path, _canonical(draft.qualification.runtime_binding))
        # Seal a strict per-campaign index; never rewrite the shared capture authority.
        load_provider_qualification_index(qualification_path).require(draft.provider)
    execution["qualification_index"] = qualification_path.as_posix()
    _write_private(execution_path, _canonical(execution))
    config = terminal.TerminalDriverConfig(
        subject=root,
        provider=draft.provider,
        template_path=template_path,
        policy_path=policy_path,
        execution_path=execution_path,
        state_root=state_root,
    )
    config_path = root / PROFILE_PATH
    raw = terminal._canonical(config.to_mapping())
    if config_path.exists():
        if config_path.is_symlink():
            raise OnboardingError(f"existing terminal-driver profile is a symlink: {config_path}")
        previous = config_path.read_bytes()
        if previous != raw and _sha256(previous) != replace_profile_sha256:
            raise OnboardingError(f"existing terminal-driver profile differs: {config_path}")
    terminal._write_atomic(config_path, raw, mode=0o600, private_parent=False)
    return OnboardingResult(draft, config, config_path, template_path, policy_path, execution_path)


__all__ = [
    "CENTRAL_QUALIFICATION_INDEX",
    "LOCAL_QUALIFICATION_INDEX",
    "OnboardingBlocked",
    "OnboardingDraft",
    "OnboardingError",
    "OnboardingResult",
    "QualificationObservation",
    "approve_draft",
    "derive_draft",
    "draft_from_mapping",
]
