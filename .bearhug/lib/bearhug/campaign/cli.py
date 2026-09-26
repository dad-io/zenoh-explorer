"""Campaign command dispatch: native capsule runs by prepared locator, plus integration.

The v1 typeset controller (`campaign validate`/`plan`/`start` over a sealed work-unit typeset)
was removed on 2026-09-16.  Every execution command now routes through one prepared locator to
:mod:`bearhug.campaign.capsule_campaign`; `prepare` is the only command that starts without one.
`integrate` remains the separate integration-owner boundary over accepted candidates.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.campaign.integration import (
    CampaignIntegrationError,
    integrate_campaign_candidates,
    integrate_reviewed_campaign_candidates,
)

_MAX_CONTROL_BYTES = 64 * 1024 * 1024


class CampaignCommandError(RuntimeError):
    """A campaign command could not preserve its explicit durable contract."""


@dataclass(frozen=True, slots=True)
class CampaignCommandResult:
    exit_code: int
    report: dict[str, Any]
    human: str


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CampaignCommandError(f"JSON repeats key {key!r}")
        value[key] = item
    return value


def _read_regular(path: Path, *, maximum: int = _MAX_CONTROL_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CampaignCommandError(f"cannot read explicit file {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > maximum
            or before.st_uid != os.geteuid()
        ):
            raise CampaignCommandError(f"explicit file is not a bounded user-owned file: {path}")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if len(raw) > maximum or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise CampaignCommandError(f"explicit file changed or exceeded its bound: {path}")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _read_json_value(path: Path, *, maximum: int = _MAX_CONTROL_BYTES) -> Any:
    """Read one canonical JSON value for explicit operator input."""

    raw = _read_regular(path, maximum=maximum)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        canonical = (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CampaignCommandError(f"explicit file is not canonical JSON: {path}") from exc
    if canonical != raw:
        raise CampaignCommandError(f"explicit file is not canonical JSON: {path}")
    return value


def _required(args: Any, *names: str) -> None:
    missing = [f"--{name.replace('_', '-')}" for name in names if getattr(args, name, None) is None]
    if missing:
        raise CampaignCommandError(f"{args.action} requires {', '.join(missing)}")


def _integrate(args: Any) -> CampaignCommandResult:
    _required(
        args,
        "campaign_id",
        "run_id",
        "integration_id",
        "integrator_id",
        "integration_state_root",
        "integration_target",
        "integration_base_oid",
        "integration_inputs",
    )
    inputs = _read_json_value(Path(args.integration_inputs).expanduser())
    if not isinstance(inputs, list):
        raise CampaignCommandError("--integration-inputs must contain one JSON array")
    checks: list[tuple[str, ...]] = []
    for index, command in enumerate(getattr(args, "integration_check", None) or []):
        try:
            argv = tuple(shlex.split(command, comments=False, posix=True))
        except ValueError as exc:
            raise CampaignCommandError(
                f"--integration-check {index} is not a valid argv string"
            ) from exc
        checks.append(argv)
    review_store = getattr(args, "integration_review_store", None)
    review_specs = getattr(args, "integration_review", None) or []
    if bool(review_store) != bool(review_specs):
        raise CampaignCommandError(
            "--integration-review-store and --integration-review must be supplied together"
        )
    review_ids: dict[str, list[str]] = {}
    for index, specification in enumerate(review_specs):
        if not isinstance(specification, str) or specification.count("=") != 1:
            raise CampaignCommandError(
                f"--integration-review {index} must be WORK_UNIT_ID=REVIEW_ID"
            )
        unit_id, review_id = specification.split("=", 1)
        if not unit_id or not review_id:
            raise CampaignCommandError(
                f"--integration-review {index} must be WORK_UNIT_ID=REVIEW_ID"
            )
        review_ids.setdefault(unit_id, []).append(review_id)
    try:
        integration_kwargs = dict(
            state_root=args.integration_state_root,
            campaign_id=args.campaign_id,
            run_id=args.run_id,
            integration_id=args.integration_id,
            integrator_id=args.integrator_id,
            target=args.integration_target,
            base_oid=args.integration_base_oid,
            candidates=inputs,
            checks=checks,
            check_timeout_s=args.integration_check_timeout,
        )
        if review_store is not None:
            result = integrate_reviewed_campaign_candidates(
                review_store=review_store,
                review_ids=review_ids,
                minimum_review_approvals=args.integration_min_approvals,
                **integration_kwargs,
            )
        else:
            result = integrate_campaign_candidates(**integration_kwargs)
    except CampaignIntegrationError as exc:
        raise CampaignCommandError(str(exc)) from exc
    receipt = result.receipt
    blocker = "" if not receipt["blockers"] else f" · blockers {receipt['blockers']}"
    human = (
        f"integration {receipt['status']} · {receipt['integration_id']} · "
        f"receipt {result.receipt_path}{blocker}"
    )
    return CampaignCommandResult(0 if receipt["status"] == "passed" else 2, receipt, human)


_NATIVE_LOCATOR_ACTIONS = frozenset({"run", "status", "answer", "resume", "recover", "stop"})
_NATIVE_ACTION_FIELDS = {
    "campaign_id",
    "run_id",
    "state_root",
    "subject",
    "worktree_parent",
    "lease_root",
    "intent",
    "capsule_plan",
    "policy",
    "execution_config",
    "qualification_index",
    "review_role",
    "format",
}


def _reject_ignored_native_flags(args: Any, allowed: set[str]) -> None:
    allowed = allowed | {"integration_check_timeout", "integration_min_approvals"}
    ignored = sorted(
        field
        for field, value in vars(args).items()
        if value is not None
        and field not in allowed
        and field not in {"action", "locator", "command", "func"}
    )
    # argparse supplies defaults for the integration numeric controls even when the operator did
    # not pass them. A non-default value on a native command is still an ignored override.
    if getattr(args, "integration_check_timeout", 28800.0) != 28800.0:
        ignored.append("integration_check_timeout")
    if getattr(args, "integration_min_approvals", 1) != 1:
        ignored.append("integration_min_approvals")
    if ignored:
        names = ", ".join(f"--{field.replace('_', '-')}" for field in sorted(set(ignored)))
        raise CampaignCommandError(f"native locator command does not accept ignored flags: {names}")


def _validate_native_dispatch(args: Any) -> None:
    """Validate native locator routing before the capsule controller sees a stray flag."""

    action = args.action
    locator = getattr(args, "locator", None)
    successor = getattr(args, "successor_plan", None)
    if successor is not None and (locator is None or action not in {"answer", "resume", "recover"}):
        raise CampaignCommandError(
            "--successor-plan is only valid for native answer, resume, or recover"
        )
    if locator is None:
        if action == "prepare":
            _reject_ignored_native_flags(
                args,
                {
                    "subject",
                    "intent",
                    "capsule_plan",
                    "policy",
                    "execution_config",
                    "state_root",
                    "worktree_parent",
                    "campaign_id",
                    "run_id",
                    "format",
                },
            )
            return
        raise CampaignCommandError(
            f"campaign {action} requires an explicit prepared locator; "
            "run `campaign prepare` first"
        )
    if action not in _NATIVE_LOCATOR_ACTIONS:
        raise CampaignCommandError(
            "prepared locator is only valid for run, status, answer, resume, recover, "
            f"or stop; got {action}"
        )
    from bearhug.campaign.prepared import load_prepared

    try:
        prepared = load_prepared(locator)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise CampaignCommandError(str(exc)) from exc
    record = prepared.record
    expected = {
        "campaign_id": record["campaign_id"],
        "run_id": record["run_id"],
        "state_root": record["paths"]["state_root"],
        "subject": record["subject"]["path"],
        "worktree_parent": record["paths"]["worktree_parent"],
        "lease_root": record["paths"]["lease_root"],
        "intent": record["inputs"]["intent_path"],
        "capsule_plan": record["inputs"]["plan_path"],
        "policy": record["inputs"]["policy_path"],
        "execution_config": record["inputs"]["execution_config_path"],
        "qualification_index": record["inputs"]["qualification_index_path"],
        "review_role": record["config"]["review_role"],
    }
    for field, recorded in expected.items():
        supplied = getattr(args, field, None)
        if supplied is not None and str(supplied) != str(recorded):
            raise CampaignCommandError(
                f"--{field.replace('_', '-')} differs from the exact prepared locator"
            )
    allowed = set(_NATIVE_ACTION_FIELDS)
    if action == "answer":
        allowed.update({"question_id", "answer_file", "answer", "disposition", "successor_plan"})
    elif action == "recover":
        allowed.update({"recovery_outcome", "episode_id", "review_id", "successor_plan"})
    elif action == "resume":
        allowed.add("successor_plan")
    elif action == "stop":
        allowed.add("reason")
    _reject_ignored_native_flags(args, allowed)


def run_campaign_command(args: Any) -> CampaignCommandResult:
    if args.action == "integrate":
        return _integrate(args)
    if args.action not in _NATIVE_LOCATOR_ACTIONS | {"prepare"}:
        raise CampaignCommandError(f"unsupported campaign action: {args.action}")
    _validate_native_dispatch(args)
    # Import lazily: ordinary command startup must not pay for the capsule runtime.
    from bearhug.campaign import capsule_campaign

    return capsule_campaign.run_prepared_command(args)


__all__ = [
    "CampaignCommandError",
    "CampaignCommandResult",
    "run_campaign_command",
]
