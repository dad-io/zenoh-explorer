"""EC-01's closed execution-administration baseline contract.

This module is deliberately a pure reader/validator.  It does not launch a provider, inspect a
checkout, or fill in missing observations.  A metric has an explicit ``measured`` or
``unavailable`` state so an absent provider counter cannot be confused with a measured zero.
The baseline is evidence for later capsule qualification; it is not a second source of project
intent or campaign authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

CANONICAL_ALGORITHM = "bearhug-execution-baseline-canonical-json-sha256/1"

_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PATH = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*$"
)
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)

_BASELINE_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "canonical_algorithm",
        "baseline_id",
        "observed_at",
        "subject",
        "provider_parity",
        "benchmark",
        "measurements",
        "rollout_gates",
        "limitations",
        "content_sha256",
    }
)
_SUBJECT_FIELDS = frozenset(
    {"repository_id", "base_commit", "base_tree", "working_tree_clean", "source"}
)
_PARITY_FIELDS = frozenset({"baseline_commit", "baseline_tree", "clean", "providers"})
_PROVIDER_FIELDS = frozenset(
    {"provider", "version", "adapter", "qualification", "evidence", "limitation"}
)
_BENCHMARK_FIELDS = frozenset(
    {
        "manifest_path",
        "manifest_sha256",
        "capture_evidence_path",
        "capture_evidence_sha256",
        "scenario_count",
        "scenario_ids",
        "scenarios",
    }
)
_SCENARIO_FIELDS = frozenset(
    {
        "scenario_id",
        "kind",
        "intent",
        "obligations",
        "invariants",
        "expected_path",
        "seeded_defects",
    }
)
_MEASUREMENTS_FIELDS = frozenset({"campaigns", "capsules", "episodes"})
_OBSERVATION_FIELDS = frozenset(
    {
        "observation_id",
        "level",
        "parent_observation_id",
        "scenario_id",
        "status",
        "source_kind",
        "subject_commit",
        "provider",
        "metrics",
        "stages",
    }
)
_STAGE_FIELDS = frozenset({"stage", "elapsed_ms", "evidence"})
_METRICS_FIELDS = frozenset(
    {
        "startup_flag_count",
        "startup_artifact_count",
        "operator_lifecycle_commands",
        "hil_requests_total",
        "hil_requests_by_trigger",
        "hil_requests_by_disposition",
        "provider_launches",
        "native_sessions",
        "turns",
        "fresh_contexts",
        "prompt_bytes_total",
        "prompt_estimated_tokens",
        "prompt_bytes_by_tier",
        "prompt_bytes_by_section",
        "provider_usage",
        "repeated_context_bytes_total",
        "repeated_context_bytes_by_source_digest",
        "time_to_first_provider_tool_action_ms",
        "controller_hil_idle_time_ms",
        "provider_validation_time_ms",
        "obligations_covered",
        "review_launches",
        "local_repair_episodes",
        "replans",
        "rollbacks",
        "invariant_failures",
        "seeded_defect_escapes",
        "authority_violations",
        "post_acceptance_defects",
        "artifact_count",
        "artifact_bytes_total",
        "artifact_bytes_by_record_kind",
    }
)
_PROVIDER_USAGE_FIELDS = frozenset(
    {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"}
)
_TRIGGERS = (
    "ambiguity",
    "invariant_conflict",
    "scope_expansion",
    "authority_change",
    "architectural_decision",
    "risk_budget_change",
    "provider_policy_change",
    "checkpoint",
    "repeated_repair",
    "other",
)
_DISPOSITIONS = ("approve", "deny", "amend", "blocked")
_TIERS = ("p0", "p1", "p2", "p3")
_STAGES = ("startup", "preflight", "author", "review", "integration", "recovery", "handoff")
_UNAVAILABLE = "unavailable"
_MEASURED = "measured"
_UNITS = {"count", "bytes", "tokens", "milliseconds", "boolean"}
_METRIC_UNITS = {
    "startup_flag_count": "count",
    "startup_artifact_count": "count",
    "operator_lifecycle_commands": "count",
    "hil_requests_total": "count",
    "provider_launches": "count",
    "native_sessions": "count",
    "turns": "count",
    "fresh_contexts": "count",
    "prompt_bytes_total": "bytes",
    "prompt_estimated_tokens": "tokens",
    "repeated_context_bytes_total": "bytes",
    "time_to_first_provider_tool_action_ms": "milliseconds",
    "controller_hil_idle_time_ms": "milliseconds",
    "provider_validation_time_ms": "milliseconds",
    "obligations_covered": "count",
    "review_launches": "count",
    "local_repair_episodes": "count",
    "replans": "count",
    "rollbacks": "count",
    "invariant_failures": "count",
    "seeded_defect_escapes": "count",
    "authority_violations": "count",
    "post_acceptance_defects": "count",
    "artifact_count": "count",
    "artifact_bytes_total": "bytes",
}
# Startup flags/artifacts describe one command surface and are intentionally not summed across
# nested scopes.  The remaining scalar counters are additive when every child has a measured value.
_ADDITIVE_METRICS = frozenset(
    set(_METRIC_UNITS) - {"startup_flag_count", "startup_artifact_count"}
)


class ExecutionBaselineError(ValueError):
    """A baseline record is malformed, not closed, or internally inconsistent."""

    def __init__(self, issues: Sequence[str] | str):
        self.issues = (issues,) if isinstance(issues, str) else tuple(issues)
        super().__init__("invalid execution baseline:\n" + "\n".join(f"- {i}" for i in self.issues))


@dataclass(frozen=True, slots=True)
class ExecutionBaselineValidation:
    """Canonical identity of one valid baseline record."""

    baseline_id: str
    digest: str
    canonical_bytes: bytes


def canonical_baseline_bytes(value: Mapping[str, Any], *, omit_digest: bool = False) -> bytes:
    """Serialize a baseline exactly as its content digest is defined."""

    material = copy.deepcopy(dict(value))
    if omit_digest:
        material.pop("content_sha256", None)
    try:
        return (
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExecutionBaselineError(f"baseline is not canonical JSON: {exc}") from exc


def _closed(
    value: Any, fields: frozenset[str], path: str, issues: list[str]
) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        issues.append(f"{path}: expected object")
        return None
    actual = set(value)
    for field in sorted(fields - actual):
        issues.append(f"{path}: missing field {field!r}")
    for field in sorted(actual - fields):
        issues.append(f"{path}: unknown field {field!r}")
    return value


def _string(
    value: Any, path: str, issues: list[str], *, pattern: re.Pattern[str] | None = None
) -> None:
    if not isinstance(value, str) or not value:
        issues.append(f"{path}: expected non-empty string")
    elif pattern is not None and pattern.fullmatch(value) is None:
        issues.append(f"{path}: invalid value {value!r}")


def _integer(value: Any, path: str, issues: list[str], *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        issues.append(f"{path}: expected integer >= {minimum}")


def _sha(value: Any, path: str, issues: list[str]) -> None:
    _string(value, path, issues, pattern=_SHA256)


def _metric(
    value: Any, path: str, issues: list[str], *, expected_unit: str | None = None
) -> None:
    record = _closed(
        value,
        frozenset({"status", "value", "unit", "evidence", "reason"}),
        path,
        issues,
    )
    if record is None:
        return
    status = record.get("status")
    if status not in {_MEASURED, _UNAVAILABLE}:
        issues.append(f"{path}.status: expected 'measured' or 'unavailable'")
    unit = record.get("unit")
    if unit not in _UNITS:
        issues.append(f"{path}.unit: unsupported metric unit")
    elif expected_unit is not None and unit != expected_unit:
        issues.append(f"{path}.unit: expected {expected_unit!r} for this metric")
    evidence = record.get("evidence")
    _string(evidence, f"{path}.evidence", issues)
    reason = record.get("reason")
    if status == _UNAVAILABLE:
        if record.get("value") is not None:
            issues.append(f"{path}.value: unavailable metrics must use null, never zero")
        _string(reason, f"{path}.reason", issues)
    elif status == _MEASURED:
        if reason is not None:
            issues.append(f"{path}.reason: measured metrics must use null")
        actual = record.get("value")
        if unit == "boolean":
            if type(actual) is not bool:
                issues.append(f"{path}.value: boolean metric must be boolean")
        elif type(actual) is not int or actual < 0:
            issues.append(f"{path}.value: measured numeric metric must be a non-negative integer")


def _metric_map(
    value: Any, path: str, keys: Sequence[str], issues: list[str], *, expected_unit: str
) -> None:
    record = _closed(value, frozenset(keys), path, issues)
    if record is not None:
        for key in keys:
            _metric(record.get(key), f"{path}.{key}", issues, expected_unit=expected_unit)


def _metric_rows(
    value: Any,
    path: str,
    issues: list[str],
    *,
    identity: str,
    metric_field: str,
    expected_unit: str,
) -> None:
    if not isinstance(value, list):
        issues.append(f"{path}: expected array")
        return
    seen: set[str] = set()
    for index, item in enumerate(value):
        row_path = f"{path}[{index}]"
        row = _closed(item, frozenset({identity, metric_field}), row_path, issues)
        if row is None:
            continue
        name = row.get(identity)
        if not isinstance(name, str) or not name or name in seen:
            issues.append(f"{row_path}.{identity}: expected unique non-empty string")
        else:
            seen.add(name)
        _metric(
            row.get(metric_field),
            f"{row_path}.{metric_field}",
            issues,
            expected_unit=expected_unit,
        )


def _validate_metrics(value: Any, path: str, issues: list[str]) -> None:
    record = _closed(value, _METRICS_FIELDS, path, issues)
    if record is None:
        return
    for field in _METRICS_FIELDS:
        if field in {
            "hil_requests_by_trigger",
            "hil_requests_by_disposition",
            "prompt_bytes_by_tier",
            "provider_usage",
        }:
            continue
        if field in {
            "prompt_bytes_by_section",
            "repeated_context_bytes_by_source_digest",
            "artifact_bytes_by_record_kind",
        }:
            continue
        _metric(
            record.get(field),
            f"{path}.{field}",
            issues,
            expected_unit=_METRIC_UNITS[field],
        )
    _metric_map(
        record.get("hil_requests_by_trigger"),
        f"{path}.hil_requests_by_trigger",
        _TRIGGERS,
        issues,
        expected_unit="count",
    )
    _metric_map(
        record.get("hil_requests_by_disposition"),
        f"{path}.hil_requests_by_disposition",
        _DISPOSITIONS,
        issues,
        expected_unit="count",
    )
    _metric_map(
        record.get("prompt_bytes_by_tier"),
        f"{path}.prompt_bytes_by_tier",
        _TIERS,
        issues,
        expected_unit="bytes",
    )
    _metric_map(
        record.get("provider_usage"),
        f"{path}.provider_usage",
        tuple(_PROVIDER_USAGE_FIELDS),
        issues,
        expected_unit="tokens",
    )
    _metric_rows(
        record.get("prompt_bytes_by_section"),
        f"{path}.prompt_bytes_by_section",
        issues,
        identity="section",
        metric_field="bytes",
        expected_unit="bytes",
    )
    for total_name, map_name in (
        ("hil_requests_total", "hil_requests_by_trigger"),
        ("hil_requests_total", "hil_requests_by_disposition"),
        ("prompt_bytes_total", "prompt_bytes_by_tier"),
        ("repeated_context_bytes_total", "repeated_context_bytes_by_source_digest"),
    ):
        total = record.get(total_name)
        components = record.get(map_name)
        if (
            isinstance(total, Mapping)
            and total.get("status") == _MEASURED
            and isinstance(components, Mapping)
            and all(
                isinstance(component, Mapping)
                and component.get("status") == _MEASURED
                and type(component.get("value")) is int
                and component.get("value") >= 0
                for component in components.values()
            )
            and total.get("value") != sum(component["value"] for component in components.values())
        ):
            issues.append(f"{path}.{total_name}: does not reconcile with {map_name}")
    _metric_rows(
        record.get("repeated_context_bytes_by_source_digest"),
        f"{path}.repeated_context_bytes_by_source_digest",
        issues,
        identity="source_sha256",
        metric_field="bytes",
        expected_unit="bytes",
    )
    for index, item in enumerate(record.get("repeated_context_bytes_by_source_digest") or []):
        if isinstance(item, Mapping):
            _sha(
                item.get("source_sha256"),
                f"{path}.repeated_context_bytes_by_source_digest[{index}].source_sha256",
                issues,
            )
    repeated_total = record.get("repeated_context_bytes_total")
    repeated_rows = record.get("repeated_context_bytes_by_source_digest")
    if (
        isinstance(repeated_total, Mapping)
        and repeated_total.get("status") == _MEASURED
        and type(repeated_total.get("value")) is int
        and isinstance(repeated_rows, list)
        and all(
            isinstance(row, Mapping)
            and isinstance(row.get("bytes"), Mapping)
            and row["bytes"].get("status") == _MEASURED
            and type(row["bytes"].get("value")) is int
            and row["bytes"].get("value") >= 0
            for row in repeated_rows
        )
        and repeated_total.get("value")
        != sum(row["bytes"]["value"] for row in repeated_rows)
    ):
        issues.append(
            f"{path}.repeated_context_bytes_total: does not reconcile with "
            "repeated_context_bytes_by_source_digest"
        )
    _metric_rows(
        record.get("artifact_bytes_by_record_kind"),
        f"{path}.artifact_bytes_by_record_kind",
        issues,
        identity="record_kind",
        metric_field="bytes",
        expected_unit="bytes",
    )


def _validate_observation(
    value: Any,
    path: str,
    scenario_ids: set[str],
    subject_commit: str,
    expected_level: str,
    issues: list[str],
) -> None:
    record = _closed(value, _OBSERVATION_FIELDS, path, issues)
    if record is None:
        return
    _string(record.get("observation_id"), f"{path}.observation_id", issues, pattern=_TOKEN)
    if record.get("level") != expected_level:
        issues.append(f"{path}.level: expected {expected_level!r}")
    parent = record.get("parent_observation_id")
    if expected_level == "campaign":
        if parent is not None:
            issues.append(f"{path}.parent_observation_id: campaign parent must be null")
    elif not isinstance(parent, str) or _TOKEN.fullmatch(parent) is None:
        issues.append(f"{path}.parent_observation_id: expected parent observation id")
    scenario_id = record.get("scenario_id")
    _string(scenario_id, f"{path}.scenario_id", issues, pattern=_TOKEN)
    if isinstance(scenario_id, str) and scenario_id not in scenario_ids | {"campaign-total"}:
        issues.append(f"{path}.scenario_id: unknown benchmark scenario")
    if record.get("status") not in {"observed", _UNAVAILABLE}:
        issues.append(f"{path}.status: expected 'observed' or 'unavailable'")
    _string(record.get("source_kind"), f"{path}.source_kind", issues, pattern=_TOKEN)
    _string(record.get("subject_commit"), f"{path}.subject_commit", issues, pattern=_GIT_OID)
    if record.get("subject_commit") != subject_commit:
        issues.append(f"{path}.subject_commit: does not match sealed baseline commit")
    provider = record.get("provider")
    if provider not in {"claude", "codex", "unavailable"}:
        issues.append(f"{path}.provider: unsupported provider")
    _validate_metrics(record.get("metrics"), f"{path}.metrics", issues)
    stages = record.get("stages")
    if not isinstance(stages, list):
        issues.append(f"{path}.stages: expected array")
    else:
        seen: set[str] = set()
        for index, item in enumerate(stages):
            stage_path = f"{path}.stages[{index}]"
            stage = _closed(item, _STAGE_FIELDS, stage_path, issues)
            if stage is None:
                continue
            name = stage.get("stage")
            if name not in _STAGES or name in seen:
                issues.append(f"{stage_path}.stage: unsupported or duplicate stage")
            else:
                seen.add(name)
            _metric(
                stage.get("elapsed_ms"),
                f"{stage_path}.elapsed_ms",
                issues,
                expected_unit="milliseconds",
            )
            _string(stage.get("evidence"), f"{stage_path}.evidence", issues)


def _validate_provider(value: Any, path: str, issues: list[str]) -> None:
    record = _closed(value, _PROVIDER_FIELDS, path, issues)
    if record is None:
        return
    provider = record.get("provider")
    if provider not in {"claude", "codex"}:
        issues.append(f"{path}.provider: expected claude or codex")
    _string(record.get("version"), f"{path}.version", issues)
    _string(record.get("adapter"), f"{path}.adapter", issues, pattern=_TOKEN)
    if record.get("qualification") not in {"observed_unqualified", "qualified", _UNAVAILABLE}:
        issues.append(f"{path}.qualification: unsupported qualification state")
    _string(record.get("evidence"), f"{path}.evidence", issues)
    _string(record.get("limitation"), f"{path}.limitation", issues)
    if record.get("qualification") == "qualified":
        issues.append(f"{path}.qualification: EC-01 baseline cannot qualify a provider")


def validate_execution_baseline(value: Mapping[str, Any]) -> ExecutionBaselineValidation:
    """Validate one complete, closed baseline and return its canonical identity."""

    if not isinstance(value, Mapping):
        raise ExecutionBaselineError("baseline must be an object")
    report = dict(value)
    issues: list[str] = []
    _closed(report, _BASELINE_FIELDS, "baseline", issues)
    if report.get("schema_version") != "1":
        issues.append("baseline.schema_version: expected '1'")
    if report.get("record_kind") != "execution_baseline":
        issues.append("baseline.record_kind: expected 'execution_baseline'")
    if report.get("canonical_algorithm") != CANONICAL_ALGORITHM:
        issues.append("baseline.canonical_algorithm: unsupported algorithm")
    _string(report.get("baseline_id"), "baseline.baseline_id", issues, pattern=_TOKEN)
    _string(report.get("observed_at"), "baseline.observed_at", issues, pattern=_TIMESTAMP)
    observed_at = report.get("observed_at")
    if isinstance(observed_at, str) and _TIMESTAMP.fullmatch(observed_at):
        try:
            datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            issues.append("baseline.observed_at: invalid calendar timestamp")

    subject = _closed(report.get("subject"), _SUBJECT_FIELDS, "baseline.subject", issues)
    parity = _closed(
        report.get("provider_parity"), _PARITY_FIELDS, "baseline.provider_parity", issues
    )
    subject_commit = (
        report.get("subject", {}).get("base_commit")
        if isinstance(report.get("subject"), Mapping)
        else None
    )
    if subject is not None:
        _string(
            subject.get("repository_id"),
            "baseline.subject.repository_id",
            issues,
            pattern=_TOKEN,
        )
        _string(
            subject.get("base_commit"),
            "baseline.subject.base_commit",
            issues,
            pattern=_GIT_OID,
        )
        _string(
            subject.get("base_tree"),
            "baseline.subject.base_tree",
            issues,
            pattern=_GIT_OID,
        )
        if subject.get("working_tree_clean") is not True:
            issues.append(
                "baseline.subject.working_tree_clean: exact clean baseline requires true"
            )
        _string(subject.get("source"), "baseline.subject.source", issues)
    if parity is not None:
        _string(
            parity.get("baseline_commit"),
            "baseline.provider_parity.baseline_commit",
            issues,
            pattern=_GIT_OID,
        )
        _string(
            parity.get("baseline_tree"),
            "baseline.provider_parity.baseline_tree",
            issues,
            pattern=_GIT_OID,
        )
        if parity.get("clean") is not True:
            issues.append("baseline.provider_parity.clean: exact parity baseline must be clean")
        if isinstance(subject_commit, str) and parity.get("baseline_commit") != subject_commit:
            issues.append(
                "baseline.provider_parity.baseline_commit: differs from subject.base_commit"
            )
        if isinstance(subject, Mapping) and parity.get("baseline_tree") != subject.get("base_tree"):
            issues.append("baseline.provider_parity.baseline_tree: differs from subject.base_tree")
        providers = parity.get("providers")
        if not isinstance(providers, list) or len(providers) != 2:
            issues.append("baseline.provider_parity.providers: expected exactly Claude and Codex")
        else:
            seen: set[str] = set()
            for index, provider in enumerate(providers):
                _validate_provider(provider, f"baseline.provider_parity.providers[{index}]", issues)
                if isinstance(provider, Mapping):
                    name = provider.get("provider")
                    if name in seen:
                        issues.append("baseline.provider_parity.providers: duplicate provider")
                    seen.add(name)
            if seen != {"claude", "codex"}:
                issues.append("baseline.provider_parity.providers: must contain Claude and Codex")

    benchmark = _closed(report.get("benchmark"), _BENCHMARK_FIELDS, "baseline.benchmark", issues)
    scenario_ids: set[str] = set()
    if benchmark is not None:
        _string(
            benchmark.get("manifest_path"),
            "baseline.benchmark.manifest_path",
            issues,
            pattern=_PATH,
        )
        _sha(benchmark.get("manifest_sha256"), "baseline.benchmark.manifest_sha256", issues)
        _string(
            benchmark.get("capture_evidence_path"),
            "baseline.benchmark.capture_evidence_path",
            issues,
            pattern=_PATH,
        )
        _sha(
            benchmark.get("capture_evidence_sha256"),
            "baseline.benchmark.capture_evidence_sha256",
            issues,
        )
        _integer(
            benchmark.get("scenario_count"),
            "baseline.benchmark.scenario_count",
            issues,
            minimum=1,
        )
        ids = benchmark.get("scenario_ids")
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(item, str) or _TOKEN.fullmatch(item) is None for item in ids)
        ):
            issues.append("baseline.benchmark.scenario_ids: expected non-empty token array")
        else:
            scenario_ids = set(ids)
            if len(scenario_ids) != len(ids) or len(ids) != benchmark.get("scenario_count"):
                issues.append("baseline.benchmark.scenario_ids: count or uniqueness mismatch")
        scenarios = benchmark.get("scenarios")
        if not isinstance(scenarios, list) or len(scenarios) != benchmark.get("scenario_count"):
            issues.append("baseline.benchmark.scenarios: count must match scenario_count")
        else:
            scenario_records: set[str] = set()
            for index, scenario in enumerate(scenarios):
                scenario_path = f"baseline.benchmark.scenarios[{index}]"
                item = _closed(scenario, _SCENARIO_FIELDS, scenario_path, issues)
                if item is None:
                    continue
                identifier = item.get("scenario_id")
                _string(identifier, f"{scenario_path}.scenario_id", issues, pattern=_TOKEN)
                if isinstance(identifier, str):
                    if identifier in scenario_records:
                        issues.append(f"{scenario_path}.scenario_id: duplicate scenario")
                    scenario_records.add(identifier)
                if isinstance(identifier, str) and identifier not in scenario_ids:
                    issues.append(f"{scenario_path}.scenario_id: missing from scenario_ids")
                if item.get("kind") not in {
                    "local_implementation",
                    "cross_cutting",
                    "invalidated_assumption",
                    "hil_scope_meaning_conflict",
                }:
                    issues.append(f"{scenario_path}.kind: unsupported benchmark kind")
                _string(item.get("intent"), f"{scenario_path}.intent", issues)
                for field in ("obligations", "invariants", "seeded_defects"):
                    values = item.get(field)
                    if (
                        not isinstance(values, list)
                        or not values
                        or any(not isinstance(value, str) or not value for value in values)
                    ):
                        issues.append(f"{scenario_path}.{field}: expected non-empty string array")
                _string(
                    item.get("expected_path"),
                    f"{scenario_path}.expected_path",
                    issues,
                    pattern=_TOKEN,
                )
            if scenario_records != scenario_ids:
                issues.append("baseline.benchmark.scenarios: IDs do not match scenario_ids")
        manifest_material = copy.deepcopy(dict(benchmark)) if isinstance(benchmark, Mapping) else {}
        claimed_manifest = manifest_material.pop("manifest_sha256", None)
        if isinstance(claimed_manifest, str) and _SHA256.fullmatch(claimed_manifest):
            manifest_bytes = (
                json.dumps(
                    manifest_material,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            if claimed_manifest != hashlib.sha256(manifest_bytes).hexdigest():
                issues.append(
                    "baseline.benchmark.manifest_sha256: does not match canonical manifest"
                )

    measurements = _closed(
        report.get("measurements"), _MEASUREMENTS_FIELDS, "baseline.measurements", issues
    )
    observations: dict[str, tuple[str, Mapping[str, Any]]] = {}
    if measurements is not None:
        for level in ("campaigns", "capsules", "episodes"):
            entries = measurements.get(level)
            if not isinstance(entries, list) or not entries:
                issues.append(f"baseline.measurements.{level}: expected non-empty array")
                continue
            for index, item in enumerate(entries):
                _validate_observation(
                    item,
                    f"baseline.measurements.{level}[{index}]",
                    scenario_ids,
                    subject_commit or "",
                    level[:-1],
                    issues,
                )
                if isinstance(item, Mapping) and isinstance(item.get("observation_id"), str):
                    identifier = item["observation_id"]
                    if identifier in observations:
                        issues.append(
                            f"baseline.measurements: duplicate observation_id {identifier!r}"
                        )
                    else:
                        observations[identifier] = (level[:-1], item)
        for level, item in observations.values():
            parent_id = item.get("parent_observation_id")
            if isinstance(parent_id, str):
                parent = observations.get(parent_id)
                expected_parent = {"capsule": "campaign", "episode": "capsule"}.get(level)
                if parent is None:
                    issues.append(f"baseline.measurements.{level}: parent {parent_id!r} is absent")
                elif parent[0] != expected_parent:
                    issues.append(f"baseline.measurements.{level}: parent level is invalid")
        # Additive scalar fields are reconciled only when every direct child is measured. An
        # unavailable child remains unavailable and is never converted to zero for this check.
        for parent_level, child_level in (("campaign", "capsule"), ("capsule", "episode")):
            for parent_id, (level, parent) in observations.items():
                if level != parent_level:
                    continue
                children = [
                    item
                    for child_level_seen, item in observations.values()
                    if child_level_seen == child_level
                    and item.get("parent_observation_id") == parent_id
                ]
                if not children or not isinstance(parent.get("metrics"), Mapping):
                    continue
                parent_metrics = parent["metrics"]
                for metric_name in _ADDITIVE_METRICS:
                    parent_metric = parent_metrics.get(metric_name)
                    child_metrics = [
                        child.get("metrics", {}).get(metric_name)
                        if isinstance(child.get("metrics"), Mapping)
                        else None
                        for child in children
                    ]
                    if (
                        isinstance(parent_metric, Mapping)
                        and parent_metric.get("status") == _MEASURED
                        and all(
                            isinstance(metric, Mapping)
                            and metric.get("status") == _MEASURED
                            and type(metric.get("value")) is int
                            and metric.get("value") >= 0
                            for metric in child_metrics
                        )
                        and type(parent_metric.get("value")) is int
                        and parent_metric.get("value")
                        != sum(metric["value"] for metric in child_metrics)
                    ):
                        issues.append(
                            f"baseline.measurements.{parent_level}.{parent_id}: "
                            f"{metric_name} does not reconcile with {child_level} children"
                        )
                for usage_name in _PROVIDER_USAGE_FIELDS:
                    parent_usage = parent_metrics.get("provider_usage")
                    child_usage = [
                        child.get("metrics", {}).get("provider_usage", {}).get(usage_name)
                        if isinstance(child.get("metrics"), Mapping)
                        and isinstance(child.get("metrics", {}).get("provider_usage"), Mapping)
                        else None
                        for child in children
                    ]
                    if (
                        isinstance(parent_usage, Mapping)
                        and isinstance(parent_usage.get(usage_name), Mapping)
                        and parent_usage[usage_name].get("status") == _MEASURED
                        and type(parent_usage[usage_name].get("value")) is int
                        and all(
                            isinstance(metric, Mapping)
                            and metric.get("status") == _MEASURED
                            and type(metric.get("value")) is int
                            and metric.get("value") >= 0
                            for metric in child_usage
                        )
                        and parent_usage[usage_name].get("value")
                        != sum(metric["value"] for metric in child_usage)
                    ):
                        issues.append(
                            f"baseline.measurements.{parent_level}.{parent_id}: "
                            f"provider_usage.{usage_name} does not reconcile with "
                            f"{child_level} children"
                        )

    gates = report.get("rollout_gates")
    if not isinstance(gates, Mapping):
        issues.append("baseline.rollout_gates: expected object")
    else:
        required_gates = {
            "operator_commands",
            "repeated_context_prompt_volume",
            "time_to_first_action",
            "hil_frequency",
            "review_rework",
            "seeded_defect_detection",
            "invariant_preservation",
            "authority_violations",
        }
        if set(gates) != required_gates:
            issues.append("baseline.rollout_gates: expected exactly the eight pre-v2 gate names")
        for name, gate in gates.items():
            gate_path = f"baseline.rollout_gates.{name}"
            item = _closed(
                gate,
                frozenset(
                    {"rules", "requirement", "baseline_reference", "missing_required_evidence"}
                ),
                gate_path,
                issues,
            )
            if item is not None:
                if item.get("missing_required_evidence") != "block":
                    issues.append(
                        f"{gate_path}.missing_required_evidence: expected 'block'"
                    )
                rules = item.get("rules")
                if not isinstance(rules, list) or not rules:
                    issues.append(f"{gate_path}.rules: expected non-empty array")
                else:
                    for index, rule in enumerate(rules):
                        rule_path = f"{gate_path}.rules[{index}]"
                        rule_item = _closed(
                            rule,
                            frozenset({"metric", "metric_unit", "direction", "threshold"}),
                            rule_path,
                            issues,
                        )
                        if rule_item is None:
                            continue
                        _string(
                            rule_item.get("metric"),
                            f"{rule_path}.metric",
                            issues,
                            pattern=_TOKEN,
                        )
                        metric_name = rule_item.get("metric")
                        expected_metric_unit = _METRIC_UNITS.get(metric_name)
                        if expected_metric_unit is None:
                            issues.append(
                                f"{rule_path}.metric: unsupported baseline metric"
                            )
                        if rule_item.get("metric_unit") not in _UNITS:
                            issues.append(f"{rule_path}.metric_unit: unsupported metric unit")
                        elif (
                            expected_metric_unit is not None
                            and rule_item.get("metric_unit") != expected_metric_unit
                        ):
                            issues.append(
                                f"{rule_path}.metric_unit: expected {expected_metric_unit!r} "
                                f"for metric {metric_name!r}"
                            )
                        if rule_item.get("direction") not in {
                            "lower_or_equal",
                            "greater_or_equal",
                            "equal",
                        }:
                            issues.append(f"{rule_path}.direction: unsupported comparison")
                        threshold = _closed(
                            rule_item.get("threshold"),
                            frozenset({"mode", "value", "unit"}),
                            f"{rule_path}.threshold",
                            issues,
                        )
                        if threshold is not None:
                            if threshold.get("mode") not in {"absolute", "baseline_multiplier"}:
                                issues.append(
                                    f"{rule_path}.threshold.mode: unsupported threshold mode"
                                )
                            if threshold.get("mode") == "baseline_multiplier":
                                if threshold.get("unit") != "ratio" or threshold.get("value") != 1:
                                    issues.append(
                                    f"{rule_path}.threshold: baseline multiplier must be "
                                    "exactly 1 ratio"
                                    )
                            elif threshold.get("unit") != rule_item.get("metric_unit"):
                                issues.append(f"{rule_path}.threshold.unit: must match metric_unit")
                            if threshold.get("unit") == "ratio":
                                if (
                                    type(threshold.get("value")) not in {int, float}
                                    or threshold.get("value") < 0
                                ):
                                    issues.append(
                                        f"{rule_path}.threshold.value: expected non-negative number"
                                    )
                            elif (
                                type(threshold.get("value")) is not int
                                or threshold.get("value") < 0
                            ):
                                issues.append(
                                    f"{rule_path}.threshold.value: expected non-negative integer"
                                )
                _string(item.get("requirement"), f"{gate_path}.requirement", issues)
                _string(item.get("baseline_reference"), f"{gate_path}.baseline_reference", issues)
    limitations = report.get("limitations")
    if (
        not isinstance(limitations, list)
        or not limitations
        or any(not isinstance(item, str) or not item for item in limitations)
    ):
        issues.append("baseline.limitations: expected non-empty string array")

    claimed = report.get("content_sha256")
    if not isinstance(claimed, str) or _SHA256.fullmatch(claimed) is None:
        issues.append("baseline.content_sha256: expected lowercase SHA-256")
    else:
        actual = hashlib.sha256(canonical_baseline_bytes(report, omit_digest=True)).hexdigest()
        if claimed != actual:
            issues.append("baseline.content_sha256: does not match canonical content")
    if issues:
        raise ExecutionBaselineError(issues)
    canonical = canonical_baseline_bytes(report)
    content_digest = hashlib.sha256(canonical_baseline_bytes(report, omit_digest=True)).hexdigest()
    return ExecutionBaselineValidation(report["baseline_id"], content_digest, canonical)


def metric(
    *,
    value: int | bool | None,
    unit: str,
    evidence: str,
    status: str = _MEASURED,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build one explicit metric envelope for fixture authors."""

    return {"status": status, "value": value, "unit": unit, "evidence": evidence, "reason": reason}


def unavailable_metric(*, unit: str, evidence: str, reason: str) -> dict[str, Any]:
    """Build the only honest representation of an unavailable observation."""

    return metric(value=None, unit=unit, evidence=evidence, status=_UNAVAILABLE, reason=reason)


__all__ = [
    "CANONICAL_ALGORITHM",
    "ExecutionBaselineError",
    "ExecutionBaselineValidation",
    "canonical_baseline_bytes",
    "metric",
    "unavailable_metric",
    "validate_execution_baseline",
]
