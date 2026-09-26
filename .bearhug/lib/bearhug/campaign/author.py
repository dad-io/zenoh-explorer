"""Provider-neutral campaign author custody.

Standalone provider runs remain observational.  This wrapper is the explicit campaign boundary:
it verifies an already-acquired durable lease and exact base, requires a sealed adapter version,
launches the selected provider through one neutral role, and verifies every committed path before
publishing an author receipt.  The lease remains live through downstream review and verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.campaign.leases import LeaseRecord
from bearhug.campaign.review import canonical_json_sha256, worktree_sha256
from bearhug.host_git import run_git
from bearhug.paths import assert_writable
from bearhug.providers.app_server_client import run_codex_app_server
from bearhug.providers.claude import ClaudeLaunchContract, validation_programs
from bearhug.providers.claude_client import run_claude
from bearhug.providers.codex_app_server import CodexAppServerContract
from bearhug.providers.compatibility import (
    ProviderCompatibilityPolicy,
    require_supported,
)
from bearhug.providers.final_output import ProviderFinalOutputError, strict_final_json
from bearhug.providers.policy import ProviderPolicy, provider_role
from bearhug.providers.receipt import (
    ProviderReceiptError,
    capture_launch_repository,
    validate_provider_receipt,
)


class CampaignAuthorError(RuntimeError):
    """Campaign author authority could not be established or safely completed."""


@dataclass(frozen=True, slots=True)
class CampaignAuthorResult:
    lease: LeaseRecord
    provider_receipt: dict[str, Any]
    author_receipt: dict[str, Any]
    author_receipt_path: Path
    provider_run: Any


ProviderRunner = Callable[[str, str, object, dict[str, Any]], Any]
_PROVIDER_ID = {"codex": "openai-codex", "claude": "anthropic-claude"}
_ADAPTER_ID = {"codex": "codex-app-server-stdio", "claude": "claude-code-stream-json"}
_PROVIDER_ADAPTER = {
    "openai-codex": "codex-app-server-stdio",
    "anthropic-claude": "claude-code-stream-json",
}

_CODEX_GIT_BOUNDARY = (
    "\n\nCodex campaign Git boundary: edit only the claimed working-tree paths. "
    "Do not stage, commit, change refs, change Git configuration, or run remote Git "
    "operations. The trusted Bear Hug controller creates the candidate commit only "
    "after this provider process has exited.\n"
)
_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_AUTHOR_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "campaign_id",
        "claimant_id",
        "lease_id",
        "lease_epoch",
        "lease_session_id",
        "provider",
        "adapter",
        "adapter_version",
        "provider_receipt_sha256",
        "provider_session_id",
        "candidate",
        "changed_paths",
        "claimed_path_prefixes",
        "semantic_resources",
        "promotion_eligible",
        "promotion_blockers",
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


def _git(cwd: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    environment = {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": os.devnull,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    try:
        completed = subprocess.run(
            (
                "git",
                "--no-optional-locks",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "commit.gpgSign=false",
                "-C",
                str(cwd),
                *args,
            ),
            input=input_bytes,
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CampaignAuthorError(f"cannot inspect campaign Git state: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        raise CampaignAuthorError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _branch(cwd: Path) -> str:
    branch = _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD").decode().strip()
    if not branch:
        raise CampaignAuthorError("campaign author worktree must be on a named branch")
    return branch


def _changed_paths(cwd: Path, base_oid: str, head_oid: str) -> tuple[str, ...]:
    raw = _git(
        cwd,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        base_oid,
        head_oid,
        "--",
    )
    if not raw:
        return ()
    if not raw.endswith(b"\0"):
        raise CampaignAuthorError("Git changed-path output was not NUL terminated")
    try:
        paths = tuple(item.decode("utf-8") for item in raw[:-1].split(b"\0"))
    except UnicodeDecodeError as exc:
        raise CampaignAuthorError("campaign paths must be valid UTF-8") from exc
    if any(
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        for path in paths
    ):
        raise CampaignAuthorError("Git reported a noncanonical changed path")
    if paths != tuple(sorted(set(paths))):
        raise CampaignAuthorError("Git changed paths were duplicate or noncanonical")
    return paths


def _owns(path: str, prefixes: Iterable[str]) -> bool:
    return any(
        prefix == "." or path == prefix or path.startswith(prefix + "/") for prefix in prefixes
    )


def campaign_author_prompt(provider: str, prompt: str) -> str:
    """Return the exact provider prompt, including controller-owned safety framing."""

    if provider == "codex":
        return prompt.rstrip() + _CODEX_GIT_BOUNDARY
    return prompt


def _canonical_relative_path(path: str, *, allow_root: bool) -> bool:
    if not isinstance(path, str) or not path:
        return False
    if path == ".":
        return allow_root
    return (
        not path.startswith("/")
        and not path.endswith("/")
        and "\\" not in path
        and "\x00" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _split_nul_paths(raw: bytes, label: str) -> set[str]:
    if raw and not raw.endswith(b"\0"):
        raise CampaignAuthorError(f"{label} was not NUL terminated")
    try:
        return {item.decode("utf-8") for item in raw[:-1].split(b"\0")} if raw else set()
    except UnicodeDecodeError as exc:
        raise CampaignAuthorError(f"{label} contains a non-UTF-8 path") from exc


def _clean_check_git(cwd: Path, *args: str) -> bytes:
    """The read-only Git calls ``_dirty_paths`` uses to decide dirtiness, routed through the
    shared, hardened ``run_git`` instead of this module's own ``_git``. That gives this
    verdict the same user global-ignore authority as ``capture_launch_repository`` -- a
    globally-ignored untracked path (unrelated to the provider's own claim) must not read as
    "dirty" here just because this call happened to null ``HOME``. ``_git`` stays in charge of
    the mutating ``add``/``commit`` Codex finalization needs below, which ``run_git``'s
    stricter repository-filter refusal must not reach.
    """

    try:
        result = run_git(cwd, *args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CampaignAuthorError(f"cannot inspect campaign Git state: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise CampaignAuthorError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _dirty_paths(cwd: Path) -> tuple[str, ...]:
    paths: set[str] = set()
    for arguments in (
        ("diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--"),
        ("diff", "--cached", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--"),
        ("ls-files", "--others", "--exclude-standard", "-z", "--"),
    ):
        raw = _clean_check_git(cwd, *arguments)
        paths.update(_split_nul_paths(raw, "Git dirty-path output"))
    if any(not _canonical_relative_path(path, allow_root=False) for path in paths):
        raise CampaignAuthorError("provider produced a noncanonical changed path")
    return tuple(sorted(paths))


def _no_candidate_detail(provider_receipt: Mapping[str, Any], run: Any) -> str:
    """Say why there was no candidate, from evidence the receipt already holds.

    The bare refusal named the rule and not the cause, so the operator had to open the
    transcript to learn the episode simply never committed. Measured on row 240 T2: correct
    code was written, the episode ended with a race test running in the background, and the
    lease closed with two uncommitted paths.
    """

    parts: list[str] = []
    close = provider_receipt.get("close_repository")
    if isinstance(close, Mapping) and close.get("clean") is not True:
        count = close.get("dirty_path_count")
        parts.append(
            f"the worktree closed with {count} uncommitted path(s); the episode never committed"
            if isinstance(count, int)
            else "the worktree closed dirty; the episode never committed"
        )
    started = _background_tasks_started(run)
    if started:
        parts.append(
            f"the episode started {started} background task(s), which are killed when it ends"
        )
    try:
        final = strict_final_json(run)
        if isinstance(final.get("next_action"), str):
            parts.append(" ".join(final["next_action"].split())[:2000])
    except (ProviderFinalOutputError, OSError, ValueError):
        pass
    return ": " + "; ".join(parts) if parts else ""


def _background_tasks_started(run: Any) -> int:
    """Count background tasks the episode launched, or 0 when the stream cannot say."""

    path = getattr(run, "raw_events_path", None)
    if not isinstance(path, Path):
        return 0
    try:
        raw = path.read_bytes()
    except OSError:
        return 0
    started = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == "system" and record.get("subtype") == "task_started":
            started += 1
    return started


def _finalize_codex_candidate(cwd: Path, *, base_oid: str, claimed_paths: tuple[str, ...]) -> None:
    """Create a candidate only after Codex has exited and only through trusted Git."""

    if _git(cwd, "rev-parse", "HEAD").decode().strip() != base_oid:
        raise CampaignAuthorError("Codex changed Git HEAD despite having no Git write authority")
    changed = _dirty_paths(cwd)
    if not changed:
        raise CampaignAuthorError("Codex produced no candidate changes")
    foreign = [path for path in changed if not _owns(path, claimed_paths)]
    if foreign:
        raise CampaignAuthorError(f"Codex wrote outside its claim: {foreign}")
    dangerous = [
        path
        for path in changed
        if path == ".gitattributes"
        or path.endswith("/.gitattributes")
        or path == ".gitmodules"
        or path.endswith("/.gitmodules")
    ]
    if dangerous:
        raise CampaignAuthorError(
            f"Codex candidates may not change Git execution-control files: {dangerous}"
        )
    attribute_input = b"".join(path.encode() + b"\0" for path in changed)
    attributes = _git(
        cwd,
        "check-attr",
        "-z",
        "--stdin",
        "filter",
        input_bytes=attribute_input,
    )
    values = attributes.split(b"\0")
    if any(value not in {b"", b"unspecified", b"unset"} for value in values[2::3]):
        raise CampaignAuthorError("Codex candidate paths may not use Git content filters")
    # Stage only the paths whose bytes were inspected above.  A claim may contain
    # optional prefixes that do not exist in this episode, and passing those
    # unmatched prefixes to ``git add`` would reject an otherwise valid subset.
    _git(cwd, "add", "-A", "--", *changed)
    environment_message = "Bear Hug controller finalized provider-authored changes\n"
    _git(
        cwd,
        "-c",
        "user.name=Bear Hug Controller",
        "-c",
        "user.email=bearhug-controller@invalid",
        "commit",
        "--no-verify",
        "--no-signoff",
        "-m",
        environment_message,
    )
    if _dirty_paths(cwd):
        raise CampaignAuthorError("controller finalization did not leave a clean candidate")


def _default_runner(provider: str, prompt: str, contract: object, options: dict[str, Any]) -> Any:
    if provider == "codex":
        if not isinstance(contract, CodexAppServerContract):
            raise CampaignAuthorError("Codex campaign launch received the wrong contract type")
        return run_codex_app_server(prompt, contract, **options)
    if provider == "claude":
        if not isinstance(contract, ClaudeLaunchContract):
            raise CampaignAuthorError("Claude campaign launch received the wrong contract type")
        return run_claude(prompt, contract, **options)
    raise CampaignAuthorError(f"unsupported campaign provider: {provider!r}")


def validate_campaign_author_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _AUTHOR_FIELDS:
        raise CampaignAuthorError("campaign author receipt has missing or unknown fields")
    if value["schema_version"] != "1" or value["record_kind"] != "campaign_author_receipt":
        raise CampaignAuthorError("unsupported campaign author receipt schema or kind")
    for field in ("campaign_id", "claimant_id"):
        if not isinstance(value[field], str) or _TOKEN.fullmatch(value[field]) is None:
            raise CampaignAuthorError(f"{field} must be a canonical claim token")
    for field in (
        "lease_session_id",
        "provider",
        "adapter",
        "adapter_version",
        "provider_session_id",
    ):
        if not isinstance(value[field], str) or not value[field]:
            raise CampaignAuthorError(f"{field} must be a non-empty string")
    if _TOKEN.fullmatch(value["lease_session_id"]) is None:
        raise CampaignAuthorError("lease_session_id must be a canonical lease token")
    if _PROVIDER_ADAPTER.get(value["provider"]) != value["adapter"]:
        raise CampaignAuthorError("provider and adapter do not name one supported pair")
    for field in ("lease_id", "provider_receipt_sha256"):
        digest = value[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise CampaignAuthorError(f"{field} must be lowercase SHA-256")
    if type(value["lease_epoch"]) is not int or value["lease_epoch"] < 1:
        raise CampaignAuthorError("lease_epoch must be a positive integer")
    candidate = value["candidate"]
    if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_FIELDS:
        raise CampaignAuthorError("author receipt candidate is not closed")
    if candidate["clean"] is not True:
        raise CampaignAuthorError("author receipt candidate must be clean")
    for field in ("repository_common_dir_sha256", "patch_sha256"):
        digest = candidate[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise CampaignAuthorError(f"candidate.{field} must be lowercase SHA-256")
    for field in ("base_oid", "head_oid", "tree_oid"):
        oid = candidate[field]
        if (
            not isinstance(oid, str)
            or len(oid) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in oid)
        ):
            raise CampaignAuthorError(f"candidate.{field} must be a full Git object id")
    for field in ("changed_paths", "claimed_path_prefixes", "semantic_resources"):
        items = value[field]
        if (
            not isinstance(items, list)
            or not all(isinstance(item, str) and item for item in items)
            or items != sorted(set(items))
        ):
            raise CampaignAuthorError(f"{field} must be a sorted unique string array")
    if any(not _canonical_relative_path(path, allow_root=False) for path in value["changed_paths"]):
        raise CampaignAuthorError("changed_paths contains a noncanonical path")
    if any(
        not _canonical_relative_path(path, allow_root=True)
        for path in value["claimed_path_prefixes"]
    ):
        raise CampaignAuthorError("claimed_path_prefixes contains a noncanonical path")
    if any(_TOKEN.fullmatch(resource) is None for resource in value["semantic_resources"]):
        raise CampaignAuthorError("semantic_resources contains a noncanonical token")
    if any(not _owns(path, value["claimed_path_prefixes"]) for path in value["changed_paths"]):
        raise CampaignAuthorError("author receipt contains an out-of-claim changed path")
    if type(value["promotion_eligible"]) is not bool:
        raise CampaignAuthorError("promotion_eligible must be boolean")
    blockers = value["promotion_blockers"]
    if (
        not isinstance(blockers, list)
        or not all(isinstance(item, str) and item for item in blockers)
        or len(blockers) != len(set(blockers))
    ):
        raise CampaignAuthorError("promotion_blockers must be a unique string array")
    if value["promotion_eligible"] == bool(blockers):
        raise CampaignAuthorError("promotion eligibility and blockers disagree")
    return value


def write_campaign_author_receipt(value: dict[str, Any], path: Path | str) -> Path:
    validated = validate_campaign_author_receipt(value)
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
        raise CampaignAuthorError(
            f"refusing to overwrite campaign author receipt: {target}"
        ) from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
    return target


def run_campaign_author(
    *,
    campaign_root: Path | str,
    campaign_lease: LeaseRecord,
    campaign_id: str,
    claimant_id: str,
    worktree: Path | str,
    branch: str,
    base_oid: str,
    path_prefixes: Iterable[str],
    semantic_resources: Iterable[str],
    prompt: str,
    provider_policy: ProviderPolicy,
    role_name: str,
    compatibility_policy: ProviderCompatibilityPolicy,
    compatibility_repository_root: Path | str,
    adapter_version: str,
    provider_output_dir: Path | str,
    receipt_role: str | None = None,
    executable: str | None = None,
    executable_sha256: str,
    timeout_s: float = 28800.0,
    settings_sha256: str | None = None,
    rules_sha256: str | None = None,
    settings_path: Path | str | None = None,
    rules_path: Path | str | None = None,
    runner: ProviderRunner = _default_runner,
    clock: Callable[[], float] = time.time,
    episode_id: str | None = None,
    lease_base_oid: str | None = None,
    validation_commands: Iterable[Sequence[str]] = (),
) -> CampaignAuthorResult:
    """Execute one lease-fenced author turn.  No merge, integration, or promotion occurs here."""

    cwd = Path(worktree).expanduser().resolve()
    if not cwd.is_dir() or not prompt.strip():
        raise CampaignAuthorError("campaign worktree and non-empty prompt are required")
    if episode_id is not None and _TOKEN.fullmatch(episode_id) is None:
        raise CampaignAuthorError("episode_id must be a canonical token")
    if lease_base_oid is not None and not isinstance(lease_base_oid, str):
        raise CampaignAuthorError("lease_base_oid must be a full Git object id")
    selected = provider_role(provider_policy, role_name)
    observed_role = receipt_role or role_name
    if not isinstance(observed_role, str) or _TOKEN.fullmatch(observed_role) is None:
        raise CampaignAuthorError("campaign author receipt role must be a canonical token")
    if selected.sandbox != "workspace-write":
        raise CampaignAuthorError("campaign author role must request workspace-write")
    if selected.provider not in _PROVIDER_ID:
        raise CampaignAuthorError(f"unsupported campaign provider: {selected.provider!r}")
    launch = capture_launch_repository(cwd)
    if launch.head_oid != base_oid:
        raise CampaignAuthorError("campaign author HEAD does not match the explicit base")
    observed_branch = _branch(cwd)
    if observed_branch != branch:
        raise CampaignAuthorError(
            f"campaign author branch mismatch: expected {branch!r}, observed {observed_branch!r}"
        )
    require_supported(
        compatibility_policy,
        adapter=_ADAPTER_ID[selected.provider],
        version=adapter_version,
        repository_root=compatibility_repository_root,
    )
    if not isinstance(campaign_lease, LeaseRecord):
        raise CampaignAuthorError("campaign author requires one durable campaign lease")
    lease_identity = campaign_lease.identity
    requested_paths = sorted(set(path_prefixes))
    requested_resources = sorted(set(semantic_resources))
    if campaign_lease.state != "active" or campaign_lease.expires_at <= clock():
        raise CampaignAuthorError("campaign author lease is not active and unexpired")
    expected_lease = {
        "campaign_id": campaign_id,
        "claimant_id": claimant_id,
        "repository_common_dir_sha256": launch.repository_common_dir_sha256,
        "worktree_sha256": worktree_sha256(cwd),
        "branch": branch,
        "base_oid": lease_base_oid or base_oid,
    }
    for field, expected in expected_lease.items():
        if getattr(lease_identity, field) != expected:
            raise CampaignAuthorError(f"campaign author lease {field} mismatch")
    if (
        campaign_lease.claim_set["path_prefixes"] != requested_paths
        or campaign_lease.claim_set["semantic_resources"] != requested_resources
    ):
        raise CampaignAuthorError("campaign author lease claim set mismatch")
    try:
        requested_paths_tuple = tuple(requested_paths)
        prompt = campaign_author_prompt(selected.provider, prompt)
        prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
        contract_type = (
            CodexAppServerContract if selected.provider == "codex" else ClaudeLaunchContract
        )
        contract = contract_type(
            cwd=str(cwd),
            prompt_sha256=prompt_sha256,
            adapter_version=adapter_version,
            requested_model=selected.model,
            requested_reasoning_effort=selected.effort,
            sandbox=selected.sandbox,
            approval_policy=selected.approval_policy,
            **(
                {"writable_roots": ()}
                if selected.provider == "codex"
                # --safe-mode leaves anything that compiles or executes needing an approval
                # no unattended session can give. Pre-authorize the programs the sealed
                # validation commands name, and nothing else.
                else {"validation_programs": validation_programs(validation_commands)}
            ),
        )
        options: dict[str, Any] = {
            "output_dir": provider_output_dir,
            "timeout_s": timeout_s,
            "role": observed_role,
            "required_capabilities": selected.required_capabilities,
            "settings_sha256": settings_sha256,
            "rules_sha256": rules_sha256,
        }
        finalization_completed = False
        if settings_path is not None and rules_path is not None:
            options.update(
                collect_operational_evidence=True,
                settings_path=settings_path,
                rules_path=rules_path,
            )
        if selected.provider == "codex":

            def finalize_candidate() -> None:
                nonlocal finalization_completed
                _finalize_codex_candidate(
                    cwd,
                    base_oid=base_oid,
                    claimed_paths=requested_paths_tuple,
                )
                finalization_completed = True

            options["candidate_finalizer"] = finalize_candidate
        if executable is not None:
            options["executable"] = executable
        run = runner(selected.provider, prompt, contract, options)
        try:
            provider_receipt = validate_provider_receipt(run.receipt)
        except (AttributeError, ProviderReceiptError) as exc:
            raise CampaignAuthorError(f"provider returned no valid success receipt: {exc}") from exc
        expected_provider = _PROVIDER_ID[selected.provider]
        if (
            provider_receipt["provider"] != expected_provider
            or provider_receipt["adapter"] != _ADAPTER_ID[selected.provider]
            or provider_receipt["adapter_version"] != adapter_version
            or provider_receipt["cwd"] != str(cwd)
            or provider_receipt["role"] != observed_role
            or provider_receipt["required_capabilities"] != list(selected.required_capabilities)
            or provider_receipt["launch"]["sandbox"] != selected.sandbox
            or provider_receipt["launch"]["approval_policy"] != selected.approval_policy
            or provider_receipt["launch"]["settings_sha256"] != settings_sha256
            or provider_receipt["launch"]["rules_sha256"] != rules_sha256
            or provider_receipt["launch"]["prompt_sha256"] != prompt_sha256
            or provider_receipt["identity"]["requested_model"] != selected.model
            or provider_receipt["identity"]["requested_reasoning_effort"] != selected.effort
        ):
            raise CampaignAuthorError(
                "provider receipt does not match the campaign launch contract"
            )
        # Ruling (2): exact argv equality (design 1.4) anchors the launch against the very
        # contract that built it, which is fully anchored but proves only that the run used
        # *some* contract's programs, not that the campaign's own sealed set is what reached
        # the evidence record. Cross-check here, at the campaign layer, never against the
        # gate functions and never by loosening the receipt schema.
        #
        # `contract` below is the same object passed
        # to `runner(...)` above, which is what the evidence builder reads its
        # `validation_programs` from (`claude_client.py`). So this check currently compares
        # that value against itself, not against an independently-held copy of the campaign's
        # sealed set -- it catches a divergence introduced between build and this later read
        # (a re-derivation bug, a different code path writing the field), not a live "does
        # the campaign's currently sealed set differ from what the provider actually ran"
        # check. That is a state this design ruled and disclosed in the record's own
        # `limitations` (`claude_operational_evidence.py`); nothing here changes behavior.
        #
        # This reads the record's own top-level `validation_programs`
        # (the sealed set fed to the builder, stored and fed back at re-derivation) rather
        # than `launch_argv.validation_programs` (an observed value inverted from the raw
        # argv, which is `None` whenever inversion cannot recover a path-qualified spelling
        # -- unrelated to whether the launch was actually widened).
        #
        # Entered whenever EITHER side shows linked evidence: the
        # receipt's own `operational_evidence_sha256` link, or the returned `run` object
        # actually carrying an `operational_evidence` attribute. Keying on the receipt's link
        # alone left this reachable only when a runner keeps both in lockstep -- true of every
        # runner in this codebase today, but not a guarantee the signature makes, and the two
        # comparisons below are the only place the record's claimed `validation_programs` and
        # executable digest are ever checked against the capsule's sealed set and the qualified
        # provider. The old `getattr(run, "operational_evidence", None)`-only form silently
        # no-oped for a runner that returns the receipt and every custody path but not the
        # record; this keeps that reachable while also keying on the receipt's own link.
        if selected.provider == "claude" and (
            provider_receipt.get("operational_evidence_sha256") is not None
            or getattr(run, "operational_evidence", None) is not None
        ):
            evidence = getattr(run, "operational_evidence", None)
            if not isinstance(evidence, Mapping):
                raise CampaignAuthorError(
                    "provider linked operational evidence but returned no record to cross-check"
                )
            sealed_programs = sorted(set(contract.validation_programs))
            observed_programs = sorted(evidence.get("validation_programs") or [])
            if observed_programs != sealed_programs:
                raise CampaignAuthorError(
                    "provider launch validation programs do not match the capsule's sealed set"
                )
            # The campaign-layer executable digest anchor, at the
            # point the campaign actually spends money -- a raise here refuses the turn, it does
            # not accept anything. Follows the same precedent (commit 96756ed) exactly.
            # `executable_sha256` is a required parameter (matching the mirrored reviewer.py
            # check, which reads `qualified_provider.executable_sha256` unconditionally), so this
            # anchor can no longer be silently turned off by omitting it.
            observed_executable_sha256 = (evidence.get("executable") or {}).get("sha256")
            if observed_executable_sha256 != executable_sha256:
                raise CampaignAuthorError(
                    "provider executable digest does not match the qualified provider"
                )
        if (
            selected.provider == "codex"
            and provider_receipt["terminal_state"] == "completed"
            and not finalization_completed
        ):
            raise CampaignAuthorError(
                "Codex provider returned without trusted controller finalization"
            )
        candidate = provider_receipt["candidate"]
        if candidate is None or candidate["base_oid"] != base_oid:
            raise CampaignAuthorError(
                "provider run produced no clean candidate from the claimed base"
                + _no_candidate_detail(provider_receipt, run)
            )
        close = capture_launch_repository(cwd)
        if (
            close.repository_common_dir_sha256 != candidate["repository_common_dir_sha256"]
            or close.head_oid != candidate["head_oid"]
            or close.tree_oid != candidate["tree_oid"]
        ):
            raise CampaignAuthorError(
                "provider receipt candidate does not match the current checkout"
            )
        changed_paths = _changed_paths(cwd, base_oid, candidate["head_oid"])
        outside = [path for path in changed_paths if not _owns(path, requested_paths)]
        if outside:
            raise CampaignAuthorError(f"provider changed paths outside its claim: {outside}")
        author_receipt = validate_campaign_author_receipt(
            {
                "schema_version": "1",
                "record_kind": "campaign_author_receipt",
                "campaign_id": campaign_id,
                "claimant_id": claimant_id,
                "lease_id": lease_identity.lease_id,
                "lease_epoch": lease_identity.epoch,
                "lease_session_id": lease_identity.session_id,
                "provider": expected_provider,
                "adapter": provider_receipt["adapter"],
                "adapter_version": adapter_version,
                "provider_receipt_sha256": canonical_json_sha256(provider_receipt),
                "provider_session_id": provider_receipt["session_id"],
                "candidate": dict(candidate),
                "changed_paths": list(changed_paths),
                "claimed_path_prefixes": requested_paths,
                "semantic_resources": requested_resources,
                "promotion_eligible": provider_receipt["promotion_eligible"],
                "promotion_blockers": list(provider_receipt["promotion_blockers"]),
            }
        )
        receipt_name = (
            f"{lease_identity.lease_id}.json"
            if episode_id is None
            else (
                f"{lease_identity.lease_id}-"
                f"{hashlib.sha256(episode_id.encode('utf-8')).hexdigest()}.json"
            )
        )
        receipt_path = write_campaign_author_receipt(
            author_receipt,
            Path(campaign_root) / "author-receipts" / receipt_name,
        )
        return CampaignAuthorResult(
            campaign_lease,
            provider_receipt,
            author_receipt,
            receipt_path,
            run,
        )
    except Exception:
        # The lease remains the authoritative fence on every author failure.  Only its owner,
        # expiry/orphan workflow, or the controller may transition it.
        raise


__all__ = [
    "CampaignAuthorError",
    "CampaignAuthorResult",
    "campaign_author_prompt",
    "run_campaign_author",
    "validate_campaign_author_receipt",
    "write_campaign_author_receipt",
]
