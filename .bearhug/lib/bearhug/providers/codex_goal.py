"""Privacy-bounded ingestion of Codex App Server thread-goal payloads.

The App Server owns goal cardinality and lifecycle.  Bear Hug validates the documented
``thread/goal/get`` response payload and goal update/clear notification payloads, proves their
thread identity against request/session custody supplied by the caller, and immediately replaces
objective prose with its UTF-8 byte count and SHA-256 digest.

This module is deliberately pure: it neither opens an App Server connection nor selects a BOARD
row.  The transport must bind ``expected_thread_id`` to the request it sent; project authority is
joined later through ``work-binding.v1``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bearhug.providers.work_authority import build_codex_work_observation


class CodexGoalIngestError(ValueError):
    """A goal payload is malformed, ambiguous, or belongs to another thread."""


_GOAL_REQUIRED_FIELDS = frozenset(
    {
        "createdAt",
        "objective",
        "status",
        "threadId",
        "timeUsedSeconds",
        "tokensUsed",
        "updatedAt",
    }
)
_GOAL_OPTIONAL_FIELDS = frozenset({"tokenBudget"})
_GOAL_STATUSES = frozenset(
    {"active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"}
)
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CodexGoalIngestError(f"{where} must be an object")
    return value


def _exact(value: Any, fields: frozenset[str], where: str) -> dict[str, Any]:
    result = _object(value, where)
    if set(result) != fields:
        raise CodexGoalIngestError(f"{where} has missing or unknown fields")
    return result


def _identifier(value: Any, where: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise CodexGoalIngestError(f"{where} must be a bounded non-empty identifier")
    return value


def _int64(value: Any, where: str) -> int:
    if type(value) is not int or not _INT64_MIN <= value <= _INT64_MAX:
        raise CodexGoalIngestError(f"{where} must be a signed 64-bit integer")
    return value


def _non_negative_int64(value: Any, where: str) -> int:
    result = _int64(value, where)
    if result < 0:
        raise CodexGoalIngestError(f"{where} must be non-negative")
    return result


def _positive_optional_int64(value: Any, where: str) -> int | None:
    if value is None:
        return None
    result = _int64(value, where)
    if result <= 0:
        raise CodexGoalIngestError(f"{where} must be positive or null")
    return result


def _goal(value: Any, *, expected_thread_id: str, where: str) -> dict[str, Any]:
    raw = _object(value, where)
    fields = set(raw)
    allowed_fields = _GOAL_REQUIRED_FIELDS | _GOAL_OPTIONAL_FIELDS
    if not _GOAL_REQUIRED_FIELDS.issubset(fields) or not fields.issubset(allowed_fields):
        raise CodexGoalIngestError(f"{where} has missing or unknown fields")

    thread_id = _identifier(raw["threadId"], f"{where}.threadId")
    if thread_id != expected_thread_id:
        raise CodexGoalIngestError(f"{where}.threadId does not match request custody")
    status = raw["status"]
    if not isinstance(status, str) or status not in _GOAL_STATUSES:
        raise CodexGoalIngestError(f"{where}.status is unsupported")
    objective = raw["objective"]
    if not isinstance(objective, str):
        raise CodexGoalIngestError(f"{where}.objective must be a string")
    try:
        objective_bytes = objective.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CodexGoalIngestError(f"{where}.objective is not valid UTF-8 text") from exc
    # Provider timestamps prove shape but are not retained in the privacy-bounded observation.
    _int64(raw["createdAt"], f"{where}.createdAt")
    _int64(raw["updatedAt"], f"{where}.updatedAt")
    return {
        "status": status,
        "objective_sha256": hashlib.sha256(objective_bytes).hexdigest(),
        "objective_byte_count": len(objective_bytes),
        "token_budget": _positive_optional_int64(raw.get("tokenBudget"), f"{where}.tokenBudget"),
        "tokens_used": _non_negative_int64(raw["tokensUsed"], f"{where}.tokensUsed"),
        "time_used_seconds": _non_negative_int64(
            raw["timeUsedSeconds"], f"{where}.timeUsedSeconds"
        ),
    }


@dataclass(frozen=True, slots=True)
class CodexGoalNormalization:
    """The complete privacy-bounded input for one Codex work observation."""

    source: str
    thread_id: str
    turn_id: str | None
    goal: dict[str, Any] | None
    limitations: tuple[str, ...]


def normalize_thread_goal_get_response(
    payload: Mapping[str, Any], *, expected_thread_id: str
) -> CodexGoalNormalization:
    """Normalize the exact ``ThreadGoalGetResponse`` result payload.

    A null goal carries no provider thread identity.  Its identity therefore comes only from the
    caller's request custody; the caller must not obtain ``expected_thread_id`` by guessing or
    newest-session selection.
    """

    thread_id = _identifier(expected_thread_id, "expected_thread_id")
    response = _exact(payload, frozenset({"goal"}), "thread/goal/get result")
    raw_goal = response["goal"]
    goal = None if raw_goal is None else _goal(raw_goal, expected_thread_id=thread_id, where="goal")
    limitations = (
        ("goal_objective_retained_by_digest_only", "provider_goal_timestamps_not_retained")
        if goal is not None
        else ("thread_goal_absent_in_get_response",)
    )
    return CodexGoalNormalization(
        source="thread/goal/get",
        thread_id=thread_id,
        turn_id=None,
        goal=goal,
        limitations=limitations,
    )


def normalize_thread_goal_notification(
    method: str,
    payload: Mapping[str, Any],
    *,
    expected_thread_id: str,
) -> CodexGoalNormalization:
    """Normalize an exact goal update or clear notification params payload."""

    thread_id = _identifier(expected_thread_id, "expected_thread_id")
    if method == "thread/goal/updated":
        params = _object(payload, "thread/goal/updated params")
        allowed = frozenset({"goal", "threadId", "turnId"})
        required = frozenset({"goal", "threadId"})
        if not required <= set(params) or not set(params) <= allowed:
            raise CodexGoalIngestError(
                "thread/goal/updated params has missing or unknown fields"
            )
        observed_thread_id = _identifier(params["threadId"], "params.threadId")
        if observed_thread_id != thread_id:
            raise CodexGoalIngestError("params.threadId does not match request custody")
        raw_turn_id = params.get("turnId")
        turn_id = None if raw_turn_id is None else _identifier(raw_turn_id, "params.turnId")
        goal = _goal(params["goal"], expected_thread_id=thread_id, where="params.goal")
        return CodexGoalNormalization(
            source=method,
            thread_id=thread_id,
            turn_id=turn_id,
            goal=goal,
            limitations=(
                "goal_objective_retained_by_digest_only",
                "provider_goal_timestamps_not_retained",
            ),
        )

    if method == "thread/goal/cleared":
        params = _exact(payload, frozenset({"threadId"}), "thread/goal/cleared params")
        observed_thread_id = _identifier(params["threadId"], "params.threadId")
        if observed_thread_id != thread_id:
            raise CodexGoalIngestError("params.threadId does not match request custody")
        return CodexGoalNormalization(
            source=method,
            thread_id=thread_id,
            turn_id=None,
            goal=None,
            limitations=("thread_goal_cleared_by_provider",),
        )

    raise CodexGoalIngestError(f"unsupported Codex goal notification method {method!r}")


def build_codex_goal_work_observation(
    normalization: CodexGoalNormalization,
    *,
    adapter: str,
    adapter_version: str,
    session_id: str,
    observed_at: datetime,
    limitations: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the closed work observation without ever reintroducing objective prose."""

    return build_codex_work_observation(
        adapter=adapter,
        adapter_version=adapter_version,
        session_id=session_id,
        thread_id=normalization.thread_id,
        goal=normalization.goal,
        observed_at=observed_at,
        limitations=(*normalization.limitations, *limitations),
    )


__all__ = [
    "CodexGoalIngestError",
    "CodexGoalNormalization",
    "build_codex_goal_work_observation",
    "normalize_thread_goal_get_response",
    "normalize_thread_goal_notification",
]
