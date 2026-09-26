"""Closed provider work observations and explicit project-authority bindings.

Provider work state and project governance are different authorities.  This module preserves the
native cardinality of Claude tasks, a Codex thread goal, or a Pi session/custom-goal observation
without retaining objective or task prose.  A separate binding record joins one observed native
work identity to explicit BOARD candidates and one exact checkout/active-plan identity.

The builders are pure.  They do not read transcripts, task stores, Git, BOARD, plans, or ambient
configuration, and they never infer a row or authority path from prose.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any


class ProviderWorkObservationError(ValueError):
    """A provider-native task/goal observation is malformed or ambiguous."""


class WorkBindingError(ValueError):
    """A provider work identity is not explicitly and consistently bound to project authority."""


_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "observation_id",
        "observed_at",
        "provider",
        "adapter",
        "adapter_version",
        "session_id",
        "thread_id",
        "work",
        "limitations",
    }
)
_CLAUDE_WORK_FIELDS = frozenset({"kind", "tasks"})
_CLAUDE_TASK_FIELDS = frozenset({"task_id", "status", "record_sha256", "record_byte_count"})
_CODEX_WORK_FIELDS = frozenset({"kind", "goal"})
_CODEX_GOAL_FIELDS = frozenset(
    {
        "status",
        "objective_sha256",
        "objective_byte_count",
        "token_budget",
        "tokens_used",
        "time_used_seconds",
    }
)
_PI_WORK_FIELDS = frozenset(
    {
        "kind",
        "session_status",
        "session_record_sha256",
        "session_record_byte_count",
        "custom_goal",
    }
)
_PI_GOAL_FIELDS = frozenset(
    {
        "goal_id",
        "status",
        "objective_sha256",
        "objective_byte_count",
        "entry_sequence",
        "entry_sha256",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "binding_id",
        "created_at",
        "provider_work_observation_id",
        "provider",
        "session_id",
        "thread_id",
        "task_id",
        "repository_common_dir_sha256",
        "worktree_sha256",
        "active_plan_path",
        "active_plan_sha256",
        "resolution",
        "bindings",
    }
)
_BOARD_BINDING_FIELDS = frozenset({"board_row", "authority_path"})

_PROVIDERS = frozenset({"anthropic-claude", "openai-codex", "pi-coding-agent"})
_CODEX_GOAL_STATUSES = frozenset(
    {"active", "paused", "complete", "blocked", "usageLimited", "budgetLimited"}
)
_PI_GOAL_STATUSES = frozenset({"active", "paused", "complete", "blocked"})
_CLAUDE_TASK_STATUSES = frozenset({"pending", "in_progress", "completed"})
_PI_SESSION_STATUSES = frozenset({"active", "settled", "ended", "interrupted", "error"})
_TOKEN = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BOARD_ROW = re.compile(r"^[1-9][0-9]*$")
_OBSERVATION_DOMAIN = b"bear-hug/provider-work-observation/v1\0"
_BINDING_DOMAIN = b"bear-hug/work-binding/v1\0"


def _canonical_json(value: Any, error_type: type[ValueError]) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise error_type(f"value is not canonical JSON: {exc}") from exc


def _exact(
    value: Any,
    fields: frozenset[str],
    where: str,
    error_type: type[ValueError],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise error_type(f"{where} has missing or unknown fields")
    return value


def _timestamp(value: Any, where: str, error_type: type[ValueError]) -> str:
    if not isinstance(value, str):
        raise error_type(f"{where} must be a canonical UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise error_type(f"{where} must be a canonical UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise error_type(f"{where} must be a canonical UTC timestamp")
    return value


def _canonical_timestamp(value: datetime, error_type: type[ValueError]) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise error_type("timestamp must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _identifier(value: Any, where: str, error_type: type[ValueError]) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise error_type(f"{where} must be a bounded non-empty identifier")
    return value


def _optional_identifier(value: Any, where: str, error_type: type[ValueError]) -> str | None:
    return None if value is None else _identifier(value, where, error_type)


def _digest(value: Any, where: str, error_type: type[ValueError]) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise error_type(f"{where} must be lowercase SHA-256")
    return value


def _count(value: Any, where: str, error_type: type[ValueError], *, positive: bool) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum:
        qualifier = "positive" if positive else "non-negative"
        raise error_type(f"{where} must be a {qualifier} integer")
    return value


def _canonical_limitations(values: Iterable[str], error_type: type[ValueError]) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise error_type("limitations must be an array")
    result = list(values)
    if any(not isinstance(item, str) or not item or len(item) > 256 for item in result):
        raise error_type("limitations must contain bounded non-empty strings")
    if len(result) != len(set(result)):
        raise error_type("limitations must not contain duplicates")
    return sorted(result)


def provider_work_observation_sha256(value: Mapping[str, Any]) -> str:
    """Return the deterministic identity of one observation, excluding its self digest."""

    material = dict(value)
    material.pop("observation_id", None)
    return hashlib.sha256(
        _OBSERVATION_DOMAIN + _canonical_json(material, ProviderWorkObservationError)
    ).hexdigest()


def _validate_goal(value: Any, where: str) -> dict[str, Any]:
    goal = _exact(value, _CODEX_GOAL_FIELDS, where, ProviderWorkObservationError)
    if goal["status"] not in _CODEX_GOAL_STATUSES:
        raise ProviderWorkObservationError(f"{where}.status is unsupported")
    _digest(goal["objective_sha256"], f"{where}.objective_sha256", ProviderWorkObservationError)
    _count(
        goal["objective_byte_count"],
        f"{where}.objective_byte_count",
        ProviderWorkObservationError,
        positive=False,
    )
    if goal["token_budget"] is not None:
        _count(
            goal["token_budget"],
            f"{where}.token_budget",
            ProviderWorkObservationError,
            positive=True,
        )
    _count(
        goal["tokens_used"],
        f"{where}.tokens_used",
        ProviderWorkObservationError,
        positive=False,
    )
    _count(
        goal["time_used_seconds"],
        f"{where}.time_used_seconds",
        ProviderWorkObservationError,
        positive=False,
    )
    return goal


def _validate_work(provider: str, value: Any) -> dict[str, Any]:
    if provider == "anthropic-claude":
        work = _exact(value, _CLAUDE_WORK_FIELDS, "work", ProviderWorkObservationError)
        if work["kind"] != "claude_task_store" or not isinstance(work["tasks"], list):
            raise ProviderWorkObservationError("Claude work must be one task array")
        task_ids: list[str] = []
        for index, item in enumerate(work["tasks"]):
            where = f"work.tasks[{index}]"
            task = _exact(item, _CLAUDE_TASK_FIELDS, where, ProviderWorkObservationError)
            task_ids.append(
                _identifier(task["task_id"], f"{where}.task_id", ProviderWorkObservationError)
            )
            if task["status"] not in _CLAUDE_TASK_STATUSES:
                raise ProviderWorkObservationError(f"{where}.status is unsupported")
            _digest(task["record_sha256"], f"{where}.record_sha256", ProviderWorkObservationError)
            _count(
                task["record_byte_count"],
                f"{where}.record_byte_count",
                ProviderWorkObservationError,
                positive=True,
            )
        if task_ids != sorted(set(task_ids)):
            raise ProviderWorkObservationError("Claude tasks must be sorted by unique task_id")
        return work

    if provider == "openai-codex":
        work = _exact(value, _CODEX_WORK_FIELDS, "work", ProviderWorkObservationError)
        if work["kind"] != "codex_thread_goal":
            raise ProviderWorkObservationError("Codex work must be one scalar thread goal")
        if work["goal"] is not None:
            _validate_goal(work["goal"], "work.goal")
        return work

    work = _exact(value, _PI_WORK_FIELDS, "work", ProviderWorkObservationError)
    if work["kind"] != "pi_session_custom_goal":
        raise ProviderWorkObservationError("Pi work must be one session/custom-goal record")
    if work["session_status"] not in _PI_SESSION_STATUSES:
        raise ProviderWorkObservationError("work.session_status is unsupported")
    _digest(
        work["session_record_sha256"],
        "work.session_record_sha256",
        ProviderWorkObservationError,
    )
    _count(
        work["session_record_byte_count"],
        "work.session_record_byte_count",
        ProviderWorkObservationError,
        positive=True,
    )
    if work["custom_goal"] is not None:
        goal = _exact(
            work["custom_goal"],
            _PI_GOAL_FIELDS,
            "work.custom_goal",
            ProviderWorkObservationError,
        )
        _identifier(goal["goal_id"], "work.custom_goal.goal_id", ProviderWorkObservationError)
        if goal["status"] not in _PI_GOAL_STATUSES:
            raise ProviderWorkObservationError("work.custom_goal.status is unsupported")
        _digest(
            goal["objective_sha256"],
            "work.custom_goal.objective_sha256",
            ProviderWorkObservationError,
        )
        _count(
            goal["objective_byte_count"],
            "work.custom_goal.objective_byte_count",
            ProviderWorkObservationError,
            positive=True,
        )
        _count(
            goal["entry_sequence"],
            "work.custom_goal.entry_sequence",
            ProviderWorkObservationError,
            positive=False,
        )
        _digest(
            goal["entry_sha256"],
            "work.custom_goal.entry_sha256",
            ProviderWorkObservationError,
        )
    return work


def validate_provider_work_observation(value: Any) -> dict[str, Any]:
    """Validate one closed observation and its deterministic content identity."""

    observation = _exact(
        value,
        _OBSERVATION_FIELDS,
        "provider work observation",
        ProviderWorkObservationError,
    )
    if (
        observation["schema_version"] != "1"
        or observation["record_kind"] != "provider_work_observation"
    ):
        raise ProviderWorkObservationError("unsupported observation schema or record kind")
    _digest(observation["observation_id"], "observation_id", ProviderWorkObservationError)
    _timestamp(observation["observed_at"], "observed_at", ProviderWorkObservationError)
    provider = observation["provider"]
    if provider not in _PROVIDERS:
        raise ProviderWorkObservationError("provider is unsupported")
    if (
        not isinstance(observation["adapter"], str)
        or _TOKEN.fullmatch(observation["adapter"]) is None
    ):
        raise ProviderWorkObservationError("adapter must be a canonical token")
    if (
        not isinstance(observation["adapter_version"], str)
        or not observation["adapter_version"]
        or len(observation["adapter_version"]) > 128
    ):
        raise ProviderWorkObservationError("adapter_version must be a bounded string")
    _identifier(observation["session_id"], "session_id", ProviderWorkObservationError)
    thread_id = _optional_identifier(
        observation["thread_id"], "thread_id", ProviderWorkObservationError
    )
    if provider == "openai-codex" and thread_id is None:
        raise ProviderWorkObservationError("Codex work requires a provider-observed thread_id")
    if provider != "openai-codex" and thread_id is not None:
        raise ProviderWorkObservationError(f"{provider} work does not expose a thread_id")
    _validate_work(provider, observation["work"])
    limitations = _canonical_limitations(observation["limitations"], ProviderWorkObservationError)
    if observation["limitations"] != limitations:
        raise ProviderWorkObservationError("limitations must be sorted in canonical order")
    expected = provider_work_observation_sha256(observation)
    if observation["observation_id"] != expected:
        raise ProviderWorkObservationError("observation_id does not match the observation content")
    return copy.deepcopy(observation)


def _build_observation(
    *,
    provider: str,
    adapter: str,
    adapter_version: str,
    session_id: str,
    thread_id: str | None,
    work: dict[str, Any],
    observed_at: datetime,
    limitations: Iterable[str],
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "provider_work_observation",
        "observed_at": _canonical_timestamp(observed_at, ProviderWorkObservationError),
        "provider": provider,
        "adapter": adapter,
        "adapter_version": adapter_version,
        "session_id": session_id,
        "thread_id": thread_id,
        "work": work,
        "limitations": _canonical_limitations(limitations, ProviderWorkObservationError),
    }
    value["observation_id"] = provider_work_observation_sha256(value)
    return validate_provider_work_observation(value)


def build_claude_work_observation(
    *,
    adapter: str,
    adapter_version: str,
    session_id: str,
    tasks: Iterable[Mapping[str, Any]],
    observed_at: datetime,
    limitations: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one Claude task-store observation without retaining task prose or metadata."""

    if isinstance(tasks, (str, bytes)) or not isinstance(tasks, Iterable):
        raise ProviderWorkObservationError("tasks must be an array")
    copied = [dict(task) for task in tasks]
    copied.sort(key=lambda task: str(task.get("task_id", "")))
    return _build_observation(
        provider="anthropic-claude",
        adapter=adapter,
        adapter_version=adapter_version,
        session_id=session_id,
        thread_id=None,
        work={"kind": "claude_task_store", "tasks": copied},
        observed_at=observed_at,
        limitations=limitations,
    )


def build_codex_work_observation(
    *,
    adapter: str,
    adapter_version: str,
    session_id: str,
    thread_id: str,
    goal: Mapping[str, Any] | None,
    observed_at: datetime,
    limitations: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one Codex scalar thread-goal observation; ``None`` explicitly means no goal."""

    return _build_observation(
        provider="openai-codex",
        adapter=adapter,
        adapter_version=adapter_version,
        session_id=session_id,
        thread_id=thread_id,
        work={"kind": "codex_thread_goal", "goal": None if goal is None else dict(goal)},
        observed_at=observed_at,
        limitations=limitations,
    )


def build_pi_work_observation(
    *,
    adapter: str,
    adapter_version: str,
    session_id: str,
    session_status: str,
    session_record_sha256: str,
    session_record_byte_count: int,
    custom_goal: Mapping[str, Any] | None,
    observed_at: datetime,
    limitations: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one Pi session observation with at most one Bear Hug custom-goal entry."""

    return _build_observation(
        provider="pi-coding-agent",
        adapter=adapter,
        adapter_version=adapter_version,
        session_id=session_id,
        thread_id=None,
        work={
            "kind": "pi_session_custom_goal",
            "session_status": session_status,
            "session_record_sha256": session_record_sha256,
            "session_record_byte_count": session_record_byte_count,
            "custom_goal": None if custom_goal is None else dict(custom_goal),
        },
        observed_at=observed_at,
        limitations=limitations,
    )


def _repository_markdown_path(
    value: Any, where: str, error_type: type[ValueError], *, authority: bool
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or "\x00" in value
    ):
        raise error_type(f"{where} must be a canonical repository-relative Markdown path")
    path = PurePosixPath(value)
    parts = value.split("/")
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in parts)
        or path.suffix != ".md"
        or (authority and parts[0] != "docs")
    ):
        raise error_type(f"{where} must be a canonical repository-relative Markdown path")
    return value


def _canonical_board_bindings(values: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise WorkBindingError("bindings must be an array")
    result: list[dict[str, str]] = []
    for index, value in enumerate(values):
        item = _exact(value, _BOARD_BINDING_FIELDS, f"bindings[{index}]", WorkBindingError)
        row = item["board_row"]
        if not isinstance(row, str) or _BOARD_ROW.fullmatch(row) is None:
            raise WorkBindingError(f"bindings[{index}].board_row must be a positive row id")
        result.append(
            {
                "board_row": row,
                "authority_path": _repository_markdown_path(
                    item["authority_path"],
                    f"bindings[{index}].authority_path",
                    WorkBindingError,
                    authority=True,
                ),
            }
        )
    result.sort(key=lambda item: (int(item["board_row"]), item["authority_path"]))
    identities = [(item["board_row"], item["authority_path"]) for item in result]
    if len(identities) != len(set(identities)):
        raise WorkBindingError("bindings must not contain duplicates")
    return result


def work_binding_sha256(value: Mapping[str, Any]) -> str:
    """Return the deterministic identity of one binding, excluding its self digest."""

    material = dict(value)
    material.pop("binding_id", None)
    return hashlib.sha256(_BINDING_DOMAIN + _canonical_json(material, WorkBindingError)).hexdigest()


def _resolution(count: int) -> str:
    if count == 0:
        return "missing"
    if count == 1:
        return "bound"
    return "ambiguous"


def _validate_binding_identity_against_observation(
    binding: Mapping[str, Any], observation: Mapping[str, Any]
) -> None:
    observed = validate_provider_work_observation(observation)
    for field in ("provider", "session_id", "thread_id"):
        if binding[field] != observed[field]:
            raise WorkBindingError(f"binding {field} does not match its provider observation")
    if binding["provider_work_observation_id"] != observed["observation_id"]:
        raise WorkBindingError("binding does not name the supplied provider observation")

    task_id = binding["task_id"]
    candidates = binding["bindings"]
    if observed["provider"] == "anthropic-claude":
        task_ids = {task["task_id"] for task in observed["work"]["tasks"]}
        if task_ids and task_id not in task_ids:
            raise WorkBindingError(
                "Claude binding task_id is not present in the observed task store"
            )
        if not task_ids and task_id is not None:
            raise WorkBindingError("an empty Claude task store cannot name a task_id")
        if not task_ids and candidates:
            raise WorkBindingError("an empty Claude task store cannot bind project authority")
    elif observed["provider"] == "openai-codex":
        if task_id is not None:
            raise WorkBindingError("Codex scalar goals do not expose a task_id")
        if observed["work"]["goal"] is None and candidates:
            raise WorkBindingError("a Codex thread with no goal cannot bind project authority")
    else:
        custom_goal = observed["work"]["custom_goal"]
        expected_task = custom_goal["goal_id"] if custom_goal is not None else None
        if task_id != expected_task:
            raise WorkBindingError("Pi binding task_id does not match its custom goal")
        if custom_goal is None and candidates:
            raise WorkBindingError("a Pi session with no custom goal cannot bind project authority")


def validate_work_binding(
    value: Any, *, observation: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validate one closed binding and optionally prove its link to an observation."""

    binding = _exact(value, _BINDING_FIELDS, "work binding", WorkBindingError)
    if binding["schema_version"] != "1" or binding["record_kind"] != "work_binding":
        raise WorkBindingError("unsupported work binding schema or record kind")
    _digest(binding["binding_id"], "binding_id", WorkBindingError)
    _timestamp(binding["created_at"], "created_at", WorkBindingError)
    _digest(
        binding["provider_work_observation_id"],
        "provider_work_observation_id",
        WorkBindingError,
    )
    provider = binding["provider"]
    if provider not in _PROVIDERS:
        raise WorkBindingError("provider is unsupported")
    _identifier(binding["session_id"], "session_id", WorkBindingError)
    thread_id = _optional_identifier(binding["thread_id"], "thread_id", WorkBindingError)
    task_id = _optional_identifier(binding["task_id"], "task_id", WorkBindingError)
    if provider == "openai-codex":
        if thread_id is None or task_id is not None:
            raise WorkBindingError("Codex bindings require thread_id and forbid task_id")
    elif thread_id is not None:
        raise WorkBindingError(f"{provider} bindings do not expose thread_id")
    _digest(
        binding["repository_common_dir_sha256"],
        "repository_common_dir_sha256",
        WorkBindingError,
    )
    _digest(binding["worktree_sha256"], "worktree_sha256", WorkBindingError)
    _repository_markdown_path(
        binding["active_plan_path"], "active_plan_path", WorkBindingError, authority=False
    )
    _digest(binding["active_plan_sha256"], "active_plan_sha256", WorkBindingError)
    canonical_bindings = _canonical_board_bindings(binding["bindings"])
    if binding["bindings"] != canonical_bindings:
        raise WorkBindingError("bindings must be sorted in canonical row/path order")
    expected_resolution = _resolution(len(canonical_bindings))
    if binding["resolution"] != expected_resolution:
        raise WorkBindingError("resolution does not match the explicit binding count")
    expected_id = work_binding_sha256(binding)
    if binding["binding_id"] != expected_id:
        raise WorkBindingError("binding_id does not match the binding content")
    if observation is not None:
        _validate_binding_identity_against_observation(binding, observation)
    return copy.deepcopy(binding)


def build_work_binding(
    *,
    observation: Mapping[str, Any],
    task_id: str | None,
    repository_common_dir_sha256: str,
    worktree_sha256: str,
    active_plan_path: str,
    active_plan_sha256: str,
    bindings: Iterable[Mapping[str, Any]],
    created_at: datetime,
) -> dict[str, Any]:
    """Build a binding only from caller-supplied identities and explicit BOARD candidates."""

    observed = validate_provider_work_observation(observation)
    candidates = _canonical_board_bindings(bindings)
    value: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "work_binding",
        "created_at": _canonical_timestamp(created_at, WorkBindingError),
        "provider_work_observation_id": observed["observation_id"],
        "provider": observed["provider"],
        "session_id": observed["session_id"],
        "thread_id": observed["thread_id"],
        "task_id": task_id,
        "repository_common_dir_sha256": repository_common_dir_sha256,
        "worktree_sha256": worktree_sha256,
        "active_plan_path": active_plan_path,
        "active_plan_sha256": active_plan_sha256,
        "resolution": _resolution(len(candidates)),
        "bindings": candidates,
    }
    value["binding_id"] = work_binding_sha256(value)
    return validate_work_binding(value, observation=observed)


__all__ = [
    "ProviderWorkObservationError",
    "WorkBindingError",
    "build_claude_work_observation",
    "build_codex_work_observation",
    "build_pi_work_observation",
    "build_work_binding",
    "provider_work_observation_sha256",
    "validate_provider_work_observation",
    "validate_work_binding",
    "work_binding_sha256",
]
