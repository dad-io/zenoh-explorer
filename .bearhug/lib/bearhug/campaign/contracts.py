"""Pure validation and canonical identity for project-authored campaign contracts.

The owning project supplies the plan corpus, authority pins, programme composition, templates, and
immutable runs.  Bear Hug validates only closed typed records.  It does not read or interpret the
referenced prose, select authority, schedule work, execute commands, or write a filesystem.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

CANONICAL_ALGORITHM = "bearhug-campaign-contract-canonical-json-sha256/1"

_TOKEN = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SUBJECT = re.compile(r"^[A-Za-z0-9_*][A-Za-z0-9_.*>:/-]{0,511}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

_ROLES = {"author", "reviewer", "verifier", "integrator", "documentation"}
_PROGRAMME_CLASSES = {"core", "post_core_ui", "held", "deferred", "retired"}
_HIL_POINTS = {
    "before_launch",
    "before_retry",
    "before_integration",
    "on_block",
    "on_authority_change",
    "on_scope_change",
}
_STOP_CONDITIONS = {
    "authority_mismatch",
    "base_drift",
    "capability_unavailable",
    "claim_conflict",
    "expiry",
    "gate_failure",
    "hil_denied",
    "provider_incompatible",
}
_RECEIPT_KINDS = {"author", "review", "test", "benchmark", "ruling", "integration"}

_WORK_UNIT_FIELDS = {
    "schema_version",
    "record_kind",
    "canonical_algorithm",
    "work_unit_id",
    "role",
    "programme_class",
    "repository_id",
    "base_ref",
    "task_refs",
    "authority_scope_refs",
    "depends_on",
    "capability_refs",
    "model_role_ref",
    "instruction_artifact",
    "claim_set",
    "acceptance_commands",
    "exit_evidence",
    "completion_gate_refs",
    "hil_checkpoint_refs",
    "stop_condition_refs",
    "merge_order",
}
_TEMPLATE_FIELDS = {
    "schema_version",
    "record_kind",
    "canonical_algorithm",
    "template_id",
    "campaign_id",
    "programme_id",
    "plan_corpus_sha256",
    "authority_refs",
    "capability_refs",
    "work_units",
    "policies",
    "human_in_loop",
    "stop_conditions",
}
_RUN_FIELDS = {
    "schema_version",
    "record_kind",
    "canonical_algorithm",
    "run_id",
    "campaign_id",
    "template_id",
    "template_sha256",
    "programme_index_sha256",
    "plan_corpus_sha256",
    "authority_refs",
    "repository",
    "work_units",
    "capability_receipts",
    "integrator",
    "created_at",
    "expires_at",
}
_INDEX_FIELDS = {
    "schema_version",
    "record_kind",
    "canonical_algorithm",
    "index_id",
    "programme_id",
    "plan_corpus_sha256",
    "campaigns",
    "integrator_campaign_id",
}


class CampaignContractError(ValueError):
    """A project-authored campaign record is malformed or internally inconsistent."""

    def __init__(self, issues: Sequence[str] | str):
        self.issues = (issues,) if isinstance(issues, str) else tuple(issues)
        super().__init__("invalid campaign contract:\n" + "\n".join(f"- {x}" for x in self.issues))


@dataclass(frozen=True, slots=True)
class CampaignContractResult:
    """Canonical identity of one valid, otherwise opaque project record."""

    record_kind: str
    record_id: str
    digest: str
    canonical_bytes: bytes


# Public neutral name used by import/planning layers; the campaign-specific spelling remains a
# compatibility alias for early BH1 callers.
ContractValidationResult = CampaignContractResult


def _forbidden_code_point(value: str) -> int | None:
    for character in value:
        point = ord(character)
        if point <= 0x1F or 0x7F <= point <= 0x9F or 0xD800 <= point <= 0xDFFF:
            return point
    return None


def _validate_string_policy(value: Any, issues: list[str]) -> None:
    pending: list[tuple[str, Any]] = [("<root>", value)]
    while pending:
        path, current = pending.pop()
        if isinstance(current, str):
            point = _forbidden_code_point(current)
            if point is not None:
                issues.append(f"{path}: forbidden Unicode control or surrogate U+{point:04X}")
        elif isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    issues.append(f"{path}: object key must be a string")
                    continue
                pending.append((f"{path}/<key>", key))
                pending.append((f"{path}/{key}", child))
        elif isinstance(current, list):
            for index, child in enumerate(current):
                pending.append((f"{path}/{index}", child))


def _object(value: Any, path: str, fields: set[str], issues: list[str]) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        issues.append(f"{path}: expected object")
        return None
    actual = {key for key in value if isinstance(key, str)}
    for field in sorted(fields - actual):
        issues.append(f"{path}: missing required field {field!r}")
    for field in sorted(actual - fields):
        issues.append(f"{path}: unknown field {field!r}")
    return value


def _string(
    value: Any,
    path: str,
    issues: list[str],
    *,
    pattern: re.Pattern[str] | None = None,
    allowed: set[str] | None = None,
    maximum: int | None = None,
) -> str | None:
    if not isinstance(value, str):
        issues.append(f"{path}: expected string")
        return None
    if not value or (maximum is not None and len(value) > maximum):
        issues.append(f"{path}: expected non-empty string within length bound")
    if pattern is not None and pattern.fullmatch(value) is None:
        issues.append(f"{path}: invalid value {value!r}")
    if allowed is not None and value not in allowed:
        issues.append(f"{path}: unsupported value {value!r}")
    return value


def _integer(
    value: Any, path: str, issues: list[str], *, minimum: int, maximum: int | None = None
) -> int | None:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        issues.append(f"{path}: expected integer in [{minimum}, {maximum or 'unbounded'}]")
        return None
    return value


def _array(value: Any, path: str, issues: list[str], *, nonempty: bool = False) -> list[Any] | None:
    if not isinstance(value, list) or (nonempty and not value):
        issues.append(f"{path}: expected {'non-empty ' if nonempty else ''}array")
        return None
    return value


def _string_set(
    value: Any,
    path: str,
    issues: list[str],
    *,
    pattern: re.Pattern[str] | None = _TOKEN,
    allowed: set[str] | None = None,
    nonempty: bool = False,
) -> list[str]:
    array = _array(value, path, issues, nonempty=nonempty)
    if array is None:
        return []
    result: list[str] = []
    for index, item in enumerate(array):
        text = _string(item, f"{path}/{index}", issues, pattern=pattern, allowed=allowed)
        if text is not None:
            result.append(text)
    if len(result) != len(set(result)):
        issues.append(f"{path}: duplicate values are forbidden")
    return result


def _repository_path(value: Any, path: str, issues: list[str], *, allow_dot: bool = False) -> None:
    if not isinstance(value, str):
        issues.append(f"{path}: expected string")
        return
    if allow_dot and value == ".":
        return
    parts = value.split("/")
    candidate = PurePosixPath(value)
    if (
        not value
        or len(value) > 4096
        or candidate.is_absolute()
        or value.startswith("~")
        or value.endswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
        or candidate.as_posix() != value
    ):
        issues.append(f"{path}: expected canonical repository-relative path")


def _timestamp(value: Any, path: str, issues: list[str]) -> datetime | None:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        issues.append(f"{path}: expected UTC timestamp YYYY-MM-DDTHH:MM:SSZ")
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        issues.append(f"{path}: invalid calendar timestamp")
        return None


def _validate_authority_refs(value: Any, path: str, issues: list[str]) -> list[Mapping[str, Any]]:
    array = _array(value, path, issues, nonempty=True)
    if array is None:
        return []
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(array):
        item_path = f"{path}/{index}"
        ref = _object(item, item_path, {"source_id", "content_sha256", "scopes"}, issues)
        if ref is None:
            continue
        source_id = _string(ref.get("source_id"), f"{item_path}/source_id", issues, pattern=_TOKEN)
        _string(ref.get("content_sha256"), f"{item_path}/content_sha256", issues, pattern=_SHA256)
        _string_set(ref.get("scopes"), f"{item_path}/scopes", issues, pattern=_SCOPE, nonempty=True)
        if source_id in seen:
            issues.append(f"{path}: duplicate source_id {source_id!r}")
        if source_id is not None:
            seen.add(source_id)
        result.append(ref)
    return result


def _validate_claim_set(value: Any, path: str, issues: list[str]) -> None:
    fields = {
        "claim_set_id",
        "path_prefixes",
        "symbols",
        "subjects",
        "semantic_resources",
        "ports",
        "data_directories",
    }
    claim = _object(value, path, fields, issues)
    if claim is None:
        return
    _string(claim.get("claim_set_id"), f"{path}/claim_set_id", issues, pattern=_TOKEN)
    paths = _array(claim.get("path_prefixes"), f"{path}/path_prefixes", issues) or []
    for index, item in enumerate(paths):
        _repository_path(item, f"{path}/path_prefixes/{index}", issues, allow_dot=True)
    symbols_array = _array(claim.get("symbols"), f"{path}/symbols", issues) or []
    symbols: list[str] = []
    for index, item in enumerate(symbols_array):
        text = _string(item, f"{path}/symbols/{index}", issues, maximum=4096)
        if text is not None:
            symbols.append(text)
    if len(symbols) != len(set(symbols)):
        issues.append(f"{path}/symbols: duplicate values are forbidden")
    subjects = _string_set(claim.get("subjects"), f"{path}/subjects", issues, pattern=_SUBJECT)
    resources = _string_set(claim.get("semantic_resources"), f"{path}/semantic_resources", issues)
    data_dirs = _array(claim.get("data_directories"), f"{path}/data_directories", issues) or []
    for index, item in enumerate(data_dirs):
        _repository_path(item, f"{path}/data_directories/{index}", issues, allow_dot=True)
    ports = _array(claim.get("ports"), f"{path}/ports", issues) or []
    port_keys: list[tuple[Any, ...]] = []
    for index, item in enumerate(ports):
        item_path = f"{path}/ports/{index}"
        port = _object(item, item_path, {"transport", "port", "bind_scope"}, issues)
        if port is None:
            continue
        transport = _string(
            port.get("transport"), f"{item_path}/transport", issues, allowed={"tcp", "udp"}
        )
        number = _integer(port.get("port"), f"{item_path}/port", issues, minimum=1, maximum=65535)
        scope = _string(
            port.get("bind_scope"),
            f"{item_path}/bind_scope",
            issues,
            allowed={"loopback", "host", "network"},
        )
        port_keys.append((transport, number, scope))
    if len(port_keys) != len(set(port_keys)):
        issues.append(f"{path}/ports: duplicate values are forbidden")
    valid_paths = [item for item in paths if isinstance(item, str)]
    if len(valid_paths) != len(set(valid_paths)):
        issues.append(f"{path}/path_prefixes: duplicate values are forbidden")
    valid_data_dirs = [item for item in data_dirs if isinstance(item, str)]
    if len(valid_data_dirs) != len(set(valid_data_dirs)):
        issues.append(f"{path}/data_directories: duplicate values are forbidden")
    if not any((paths, symbols, subjects, resources, ports, data_dirs)):
        issues.append(f"{path}: atomic claim set must contain at least one claim")


def _validate_work_unit_record(value: Any, issues: list[str], path: str = "<root>") -> None:
    unit = _object(value, path, _WORK_UNIT_FIELDS, issues)
    if unit is None:
        return
    if unit.get("schema_version") != "1":
        issues.append(f"{path}/schema_version: expected '1'")
    if unit.get("record_kind") != "work_unit":
        issues.append(f"{path}/record_kind: expected 'work_unit'")
    if unit.get("canonical_algorithm") != CANONICAL_ALGORITHM:
        issues.append(f"{path}/canonical_algorithm: expected {CANONICAL_ALGORITHM!r}")
    _string(unit.get("work_unit_id"), f"{path}/work_unit_id", issues, pattern=_TOKEN)
    _string(unit.get("role"), f"{path}/role", issues, allowed=_ROLES)
    _string(
        unit.get("programme_class"),
        f"{path}/programme_class",
        issues,
        allowed=_PROGRAMME_CLASSES,
    )
    repository_id = _string(
        unit.get("repository_id"), f"{path}/repository_id", issues, pattern=_TOKEN
    )
    if unit.get("base_ref") != "campaign_base":
        issues.append(f"{path}/base_ref: expected 'campaign_base'")

    task_refs = _array(unit.get("task_refs"), f"{path}/task_refs", issues, nonempty=True) or []
    task_keys: list[tuple[Any, ...]] = []
    for index, item in enumerate(task_refs):
        item_path = f"{path}/task_refs/{index}"
        ref = _object(item, item_path, {"task_id", "obligation_id", "source_id"}, issues)
        if ref is None:
            continue
        task_id = _string(ref.get("task_id"), f"{item_path}/task_id", issues, maximum=128)
        obligation = _string(
            ref.get("obligation_id"), f"{item_path}/obligation_id", issues, pattern=_TOKEN
        )
        source = _string(ref.get("source_id"), f"{item_path}/source_id", issues, pattern=_TOKEN)
        task_keys.append((task_id, obligation, source))
    if len(task_keys) != len(set(task_keys)):
        issues.append(f"{path}/task_refs: duplicate task obligation reference")

    _string_set(
        unit.get("authority_scope_refs"),
        f"{path}/authority_scope_refs",
        issues,
        pattern=_SCOPE,
        nonempty=True,
    )
    _string_set(unit.get("depends_on"), f"{path}/depends_on", issues)
    capabilities = _string_set(
        unit.get("capability_refs"), f"{path}/capability_refs", issues, nonempty=True
    )
    model_role = _string(
        unit.get("model_role_ref"), f"{path}/model_role_ref", issues, pattern=_TOKEN
    )
    if model_role is not None and model_role not in capabilities:
        issues.append(f"{path}/model_role_ref: must be included in capability_refs")

    artifact = _object(
        unit.get("instruction_artifact"),
        f"{path}/instruction_artifact",
        {"repository_id", "path", "content_sha256"},
        issues,
    )
    if artifact is not None:
        artifact_repo = _string(
            artifact.get("repository_id"),
            f"{path}/instruction_artifact/repository_id",
            issues,
            pattern=_TOKEN,
        )
        if repository_id is not None and artifact_repo != repository_id:
            issues.append(f"{path}/instruction_artifact: repository_id mismatch")
        _repository_path(artifact.get("path"), f"{path}/instruction_artifact/path", issues)
        _string(
            artifact.get("content_sha256"),
            f"{path}/instruction_artifact/content_sha256",
            issues,
            pattern=_SHA256,
        )

    _validate_claim_set(unit.get("claim_set"), f"{path}/claim_set", issues)
    commands = (
        _array(
            unit.get("acceptance_commands"), f"{path}/acceptance_commands", issues, nonempty=True
        )
        or []
    )
    command_ids: list[str] = []
    command_fields = {
        "command_id",
        "runner_ref",
        "argv",
        "working_directory",
        "timeout_seconds",
        "expected_exit_codes",
        "evidence_refs",
    }
    for index, item in enumerate(commands):
        item_path = f"{path}/acceptance_commands/{index}"
        command = _object(item, item_path, command_fields, issues)
        if command is None:
            continue
        command_id = _string(
            command.get("command_id"), f"{item_path}/command_id", issues, pattern=_TOKEN
        )
        if command_id is not None:
            command_ids.append(command_id)
        runner = _string(
            command.get("runner_ref"), f"{item_path}/runner_ref", issues, pattern=_TOKEN
        )
        if runner is not None and runner not in capabilities:
            issues.append(f"{item_path}/runner_ref: must be included in capability_refs")
        argv = _array(command.get("argv"), f"{item_path}/argv", issues, nonempty=True)
        if argv is not None:
            if len(argv) > 256:
                issues.append(f"{item_path}/argv: exceeds 256 arguments")
            for arg_index, arg in enumerate(argv):
                _string(arg, f"{item_path}/argv/{arg_index}", issues, maximum=4096)
        _repository_path(
            command.get("working_directory"),
            f"{item_path}/working_directory",
            issues,
            allow_dot=True,
        )
        _integer(
            command.get("timeout_seconds"),
            f"{item_path}/timeout_seconds",
            issues,
            minimum=1,
            maximum=86400,
        )
        exits = (
            _array(
                command.get("expected_exit_codes"),
                f"{item_path}/expected_exit_codes",
                issues,
                nonempty=True,
            )
            or []
        )
        for exit_index, code in enumerate(exits):
            _integer(
                code,
                f"{item_path}/expected_exit_codes/{exit_index}",
                issues,
                minimum=0,
                maximum=255,
            )
        valid_exits = [code for code in exits if type(code) is int]
        if len(valid_exits) != len(set(valid_exits)):
            issues.append(f"{item_path}/expected_exit_codes: duplicate values are forbidden")
        _string_set(
            command.get("evidence_refs"),
            f"{item_path}/evidence_refs",
            issues,
            nonempty=True,
        )
    if len(command_ids) != len(set(command_ids)):
        issues.append(f"{path}/acceptance_commands: duplicate command_id")

    evidence = _object(
        unit.get("exit_evidence"),
        f"{path}/exit_evidence",
        {"required_receipt_kinds", "required_artifact_refs"},
        issues,
    )
    if evidence is not None:
        _string_set(
            evidence.get("required_receipt_kinds"),
            f"{path}/exit_evidence/required_receipt_kinds",
            issues,
            pattern=None,
            allowed=_RECEIPT_KINDS,
            nonempty=True,
        )
        _string_set(
            evidence.get("required_artifact_refs"),
            f"{path}/exit_evidence/required_artifact_refs",
            issues,
        )
    _string_set(
        unit.get("completion_gate_refs"),
        f"{path}/completion_gate_refs",
        issues,
        nonempty=True,
    )
    _string_set(
        unit.get("hil_checkpoint_refs"),
        f"{path}/hil_checkpoint_refs",
        issues,
        pattern=None,
        allowed=_HIL_POINTS,
        nonempty=True,
    )
    _string_set(
        unit.get("stop_condition_refs"),
        f"{path}/stop_condition_refs",
        issues,
        pattern=None,
        allowed=_STOP_CONDITIONS,
        nonempty=True,
    )
    _integer(unit.get("merge_order"), f"{path}/merge_order", issues, minimum=1)


def _cycle(graph: Mapping[str, set[str]]) -> tuple[str, ...] | None:
    state: dict[str, str] = {}
    for start in sorted(graph):
        if start in state:
            continue
        path = [start]
        positions = {start: 0}
        state[start] = "active"
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(sorted(graph.get(start, set()))))]
        while stack:
            node, edges = stack[-1]
            try:
                target = next(edges)
            except StopIteration:
                stack.pop()
                path.pop()
                positions.pop(node)
                state[node] = "done"
                continue
            if state.get(target) == "active":
                return (*path[positions[target] :], target)
            if state.get(target) == "done":
                continue
            state[target] = "active"
            positions[target] = len(path)
            path.append(target)
            stack.append((target, iter(sorted(graph.get(target, set())))))
    return None


def _reachable(graph: Mapping[str, set[str]], source: str, target: str) -> bool:
    pending = list(graph.get(source, set()))
    seen: set[str] = set()
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            pending.extend(graph.get(node, set()))
    return False


def _validate_template_record(value: Any, issues: list[str]) -> None:
    template = _object(value, "<root>", _TEMPLATE_FIELDS, issues)
    if template is None:
        return
    if template.get("schema_version") != "1":
        issues.append("schema_version: expected '1'")
    if template.get("record_kind") != "campaign_template":
        issues.append("record_kind: expected 'campaign_template'")
    if template.get("canonical_algorithm") != CANONICAL_ALGORITHM:
        issues.append(f"canonical_algorithm: expected {CANONICAL_ALGORITHM!r}")
    for field in ("template_id", "campaign_id", "programme_id"):
        _string(template.get(field), field, issues, pattern=_TOKEN)
    _string(template.get("plan_corpus_sha256"), "plan_corpus_sha256", issues, pattern=_SHA256)
    authority = _validate_authority_refs(template.get("authority_refs"), "authority_refs", issues)
    authority_ids = {
        source_id for ref in authority if isinstance((source_id := ref.get("source_id")), str)
    }
    authority_scopes = {
        scope
        for ref in authority
        for scope in (ref.get("scopes") if isinstance(ref.get("scopes"), list) else [])
        if isinstance(scope, str)
    }
    capabilities = set(
        _string_set(template.get("capability_refs"), "capability_refs", issues, nonempty=True)
    )

    units = _array(template.get("work_units"), "work_units", issues, nonempty=True) or []
    unit_by_id: dict[str, Mapping[str, Any]] = {}
    obligations: set[tuple[Any, Any]] = set()
    merge_orders: set[int] = set()
    claim_ids: set[Any] = set()
    integrators: list[str] = []
    for index, item in enumerate(units):
        _validate_work_unit_record(item, issues, f"work_units/{index}")
        if not isinstance(item, Mapping):
            continue
        unit_id = item.get("work_unit_id")
        if isinstance(unit_id, str):
            if unit_id in unit_by_id:
                issues.append(f"work_units: duplicate work_unit_id {unit_id!r}")
            unit_by_id.setdefault(unit_id, item)
            if item.get("role") == "integrator":
                integrators.append(unit_id)
        order = item.get("merge_order")
        if type(order) is int:
            if order in merge_orders:
                issues.append(f"work_units: duplicate merge_order {order}")
            merge_orders.add(order)
        claim = item.get("claim_set")
        if isinstance(claim, Mapping):
            claim_id = claim.get("claim_set_id")
            if isinstance(claim_id, str) and claim_id in claim_ids:
                issues.append(f"work_units: duplicate claim_set_id {claim_id!r}")
            if isinstance(claim_id, str):
                claim_ids.add(claim_id)
        for ref in item.get("task_refs", []) if isinstance(item.get("task_refs"), list) else []:
            if not isinstance(ref, Mapping):
                continue
            source_id = ref.get("source_id")
            obligation_id = ref.get("obligation_id")
            if isinstance(source_id, str) and isinstance(obligation_id, str):
                key = (source_id, obligation_id)
                if key in obligations:
                    issues.append(f"work_units: obligation mapped more than once: {key!r}")
                obligations.add(key)
            if isinstance(source_id, str) and source_id not in authority_ids:
                issues.append(f"work unit {unit_id!r}: task refers to undeclared authority source")
        raw_scopes = item.get("authority_scope_refs", [])
        unit_scopes = (
            {scope for scope in raw_scopes if isinstance(scope, str)}
            if isinstance(raw_scopes, list)
            else set()
        )
        unknown_scopes = unit_scopes - authority_scopes
        if unknown_scopes:
            issues.append(
                f"work unit {unit_id!r}: undeclared authority scope {sorted(unknown_scopes)!r}"
            )
        raw_capabilities = item.get("capability_refs", [])
        unit_capabilities = (
            {capability for capability in raw_capabilities if isinstance(capability, str)}
            if isinstance(raw_capabilities, list)
            else set()
        )
        unknown_capabilities = unit_capabilities - capabilities
        if unknown_capabilities:
            issues.append(
                f"work unit {unit_id!r}: undeclared capability {sorted(unknown_capabilities)!r}"
            )
    if len(integrators) != 1:
        issues.append("work_units: expected exactly one integrator")

    graph: dict[str, set[str]] = {unit_id: set() for unit_id in unit_by_id}
    for unit_id, unit in unit_by_id.items():
        dependencies = unit.get("depends_on", [])
        for dependency in dependencies if isinstance(dependencies, list) else []:
            if not isinstance(dependency, str):
                continue
            if dependency == unit_id:
                issues.append(f"work unit {unit_id!r} may not depend on itself")
            elif dependency not in unit_by_id:
                issues.append(f"work unit {unit_id!r} has unknown dependency {dependency!r}")
            else:
                graph[unit_id].add(dependency)
                left = unit.get("merge_order")
                right = unit_by_id[dependency].get("merge_order")
                if type(left) is int and type(right) is int and right >= left:
                    issues.append(f"work unit {unit_id!r}: dependency must have lower merge_order")
    found_cycle = _cycle(graph)
    if found_cycle is not None:
        issues.append(f"work-unit dependency cycle: {' -> '.join(found_cycle)}")
    if len(integrators) == 1 and found_cycle is None:
        integrator_id = integrators[0]
        unreachable = sorted(
            unit_id
            for unit_id in unit_by_id
            if unit_id != integrator_id and not _reachable(graph, integrator_id, unit_id)
        )
        if unreachable:
            issues.append(
                "work_units: sole integrator must depend transitively on every other work unit; "
                f"unreachable {unreachable!r}"
            )

    policies = _object(
        template.get("policies"),
        "policies",
        {"dispatch", "review", "integration", "expiry"},
        issues,
    )
    if policies is not None:
        dispatch = _object(
            policies.get("dispatch"), "policies/dispatch", {"max_parallel", "attempt_limit"}, issues
        )
        if dispatch is not None:
            _integer(
                dispatch.get("max_parallel"),
                "policies/dispatch/max_parallel",
                issues,
                minimum=1,
                maximum=64,
            )
            _integer(
                dispatch.get("attempt_limit"),
                "policies/dispatch/attempt_limit",
                issues,
                minimum=1,
                maximum=100,
            )
        review = _object(
            policies.get("review"),
            "policies/review",
            {"minimum_approvals", "independence", "require_fresh_receipt"},
            issues,
        )
        if review is not None:
            _integer(
                review.get("minimum_approvals"),
                "policies/review/minimum_approvals",
                issues,
                minimum=1,
                maximum=16,
            )
            if review.get("independence") != "separate_worktree":
                issues.append("policies/review/independence: expected 'separate_worktree'")
            if type(review.get("require_fresh_receipt")) is not bool:
                issues.append("policies/review/require_fresh_receipt: expected boolean")
        integration = _object(
            policies.get("integration"),
            "policies/integration",
            {"mode", "integrator_work_unit_id", "require_green_gate"},
            issues,
        )
        if integration is not None:
            if integration.get("mode") != "serialized":
                issues.append("policies/integration/mode: expected 'serialized'")
            integrator_id = _string(
                integration.get("integrator_work_unit_id"),
                "policies/integration/integrator_work_unit_id",
                issues,
                pattern=_TOKEN,
            )
            if integrator_id is not None and integrators != [integrator_id]:
                issues.append(
                    "policies/integration/integrator_work_unit_id: must name the sole integrator"
                )
            if type(integration.get("require_green_gate")) is not bool:
                issues.append("policies/integration/require_green_gate: expected boolean")
        expiry = _object(
            policies.get("expiry"), "policies/expiry", {"maximum_run_seconds", "on_expiry"}, issues
        )
        if expiry is not None:
            _integer(
                expiry.get("maximum_run_seconds"),
                "policies/expiry/maximum_run_seconds",
                issues,
                minimum=1,
                maximum=2592000,
            )
            if expiry.get("on_expiry") != "stop":
                issues.append("policies/expiry/on_expiry: expected 'stop'")

    hil = _object(
        template.get("human_in_loop"),
        "human_in_loop",
        {"checkpoints", "decision_timeout_seconds", "on_timeout", "required_for_stop_override"},
        issues,
    )
    checkpoints: set[str] = set()
    if hil is not None:
        checkpoints = set(
            _string_set(
                hil.get("checkpoints"),
                "human_in_loop/checkpoints",
                issues,
                pattern=None,
                allowed=_HIL_POINTS,
                nonempty=True,
            )
        )
        _integer(
            hil.get("decision_timeout_seconds"),
            "human_in_loop/decision_timeout_seconds",
            issues,
            minimum=1,
            maximum=604800,
        )
        if hil.get("on_timeout") != "stop":
            issues.append("human_in_loop/on_timeout: expected 'stop'")
        if hil.get("required_for_stop_override") is not True:
            issues.append("human_in_loop/required_for_stop_override: expected true")
    stop_conditions = set(
        _string_set(
            template.get("stop_conditions"),
            "stop_conditions",
            issues,
            pattern=None,
            allowed=_STOP_CONDITIONS,
            nonempty=True,
        )
    )
    for unit_id, unit in unit_by_id.items():
        raw_hil = unit.get("hil_checkpoint_refs", [])
        unit_hil = (
            {item for item in raw_hil if isinstance(item, str)}
            if isinstance(raw_hil, list)
            else set()
        )
        missing_hil = unit_hil - checkpoints
        if missing_hil:
            issues.append(
                f"work unit {unit_id!r}: undeclared HIL checkpoint {sorted(missing_hil)!r}"
            )
        raw_stop = unit.get("stop_condition_refs", [])
        unit_stop = (
            {item for item in raw_stop if isinstance(item, str)}
            if isinstance(raw_stop, list)
            else set()
        )
        missing_stop = unit_stop - stop_conditions
        if missing_stop:
            issues.append(
                f"work unit {unit_id!r}: undeclared stop condition {sorted(missing_stop)!r}"
            )


def _validate_run_record(value: Any, issues: list[str]) -> None:
    run = _object(value, "<root>", _RUN_FIELDS, issues)
    if run is None:
        return
    if run.get("schema_version") != "1":
        issues.append("schema_version: expected '1'")
    if run.get("record_kind") != "campaign_run":
        issues.append("record_kind: expected 'campaign_run'")
    if run.get("canonical_algorithm") != CANONICAL_ALGORITHM:
        issues.append(f"canonical_algorithm: expected {CANONICAL_ALGORITHM!r}")
    for field in ("run_id", "campaign_id", "template_id"):
        _string(run.get(field), field, issues, pattern=_TOKEN)
    for field in ("template_sha256", "programme_index_sha256", "plan_corpus_sha256"):
        _string(run.get(field), field, issues, pattern=_SHA256)
    _validate_authority_refs(run.get("authority_refs"), "authority_refs", issues)

    repository = _object(
        run.get("repository"),
        "repository",
        {
            "repository_id",
            "repository_root_sha256",
            "repository_common_dir_sha256",
            "base_oid",
            "base_tree_sha256",
            "base_branch",
        },
        issues,
    )
    repository_id: str | None = None
    if repository is not None:
        repository_id = _string(
            repository.get("repository_id"), "repository/repository_id", issues, pattern=_TOKEN
        )
        for field in ("repository_root_sha256", "repository_common_dir_sha256", "base_tree_sha256"):
            _string(repository.get(field), f"repository/{field}", issues, pattern=_SHA256)
        _string(repository.get("base_oid"), "repository/base_oid", issues, pattern=_GIT_OID)
        branch = _string(
            repository.get("base_branch"), "repository/base_branch", issues, maximum=255
        )
        if branch is not None and (
            branch.startswith("/")
            or branch.endswith(("/", ".", ".lock"))
            or ".." in branch
            or "//" in branch
            or any(x in branch for x in "~^:?*[]\\")
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        ):
            issues.append("repository/base_branch: invalid canonical short branch")

    embedded = _array(run.get("work_units"), "work_units", issues, nonempty=True) or []
    unit_ids: set[str] = set()
    work_capabilities: set[str] = set()
    integrator_ids: set[str] = set()
    for index, item in enumerate(embedded):
        item_path = f"work_units/{index}"
        wrapper = _object(item, item_path, {"work_unit_id", "work_unit_sha256", "record"}, issues)
        if wrapper is None:
            continue
        unit_id = _string(
            wrapper.get("work_unit_id"), f"{item_path}/work_unit_id", issues, pattern=_TOKEN
        )
        _string(
            wrapper.get("work_unit_sha256"),
            f"{item_path}/work_unit_sha256",
            issues,
            pattern=_SHA256,
        )
        record = wrapper.get("record")
        _validate_work_unit_record(record, issues, f"{item_path}/record")
        if isinstance(record, Mapping):
            if unit_id != record.get("work_unit_id"):
                issues.append(f"{item_path}: work_unit_id does not match record")
            if repository_id is not None and record.get("repository_id") != repository_id:
                issues.append(f"{item_path}: repository_id does not match campaign repository")
            try:
                expected = hashlib.sha256(_canonical_bytes_unchecked(record)).hexdigest()
            except (AttributeError, TypeError, ValueError):
                expected = None
            if expected is not None and wrapper.get("work_unit_sha256") != expected:
                issues.append(f"{item_path}: work_unit_sha256 does not match embedded record")
            raw_capabilities = record.get("capability_refs", [])
            if isinstance(raw_capabilities, list):
                work_capabilities.update(
                    capability for capability in raw_capabilities if isinstance(capability, str)
                )
            if record.get("role") == "integrator" and isinstance(unit_id, str):
                integrator_ids.add(unit_id)
        if isinstance(unit_id, str):
            if unit_id in unit_ids:
                issues.append(f"work_units: duplicate work_unit_id {unit_id!r}")
            unit_ids.add(unit_id)

    receipts = (
        _array(run.get("capability_receipts"), "capability_receipts", issues, nonempty=True) or []
    )
    receipt_ids: set[str] = set()
    for index, item in enumerate(receipts):
        item_path = f"capability_receipts/{index}"
        receipt = _object(item, item_path, {"capability_ref", "receipt_sha256"}, issues)
        if receipt is None:
            continue
        capability = _string(
            receipt.get("capability_ref"), f"{item_path}/capability_ref", issues, pattern=_TOKEN
        )
        _string(
            receipt.get("receipt_sha256"), f"{item_path}/receipt_sha256", issues, pattern=_SHA256
        )
        if isinstance(capability, str):
            if capability in receipt_ids:
                issues.append(f"capability_receipts: duplicate capability_ref {capability!r}")
            receipt_ids.add(capability)
    missing_receipts = work_capabilities - receipt_ids
    if missing_receipts:
        issues.append(
            f"capability receipts missing required capability bindings {sorted(missing_receipts)!r}"
        )

    integrator = _object(
        run.get("integrator"), "integrator", {"work_unit_id", "claimant_id"}, issues
    )
    if integrator is not None:
        integrator_id = _string(
            integrator.get("work_unit_id"), "integrator/work_unit_id", issues, pattern=_TOKEN
        )
        _string(integrator.get("claimant_id"), "integrator/claimant_id", issues, pattern=_TOKEN)
        if integrator_id not in integrator_ids:
            issues.append("integrator/work_unit_id: must name the embedded integrator work unit")
    created = _timestamp(run.get("created_at"), "created_at", issues)
    expires = _timestamp(run.get("expires_at"), "expires_at", issues)
    if created is not None and expires is not None and expires <= created:
        issues.append("expires_at: must be strictly after created_at")


def _validate_index_record(value: Any, issues: list[str]) -> None:
    index = _object(value, "<root>", _INDEX_FIELDS, issues)
    if index is None:
        return
    if index.get("schema_version") != "1":
        issues.append("schema_version: expected '1'")
    if index.get("record_kind") != "programme_campaign_index":
        issues.append("record_kind: expected 'programme_campaign_index'")
    if index.get("canonical_algorithm") != CANONICAL_ALGORITHM:
        issues.append(f"canonical_algorithm: expected {CANONICAL_ALGORITHM!r}")
    for field in ("index_id", "programme_id", "integrator_campaign_id"):
        _string(index.get(field), field, issues, pattern=_TOKEN)
    _string(index.get("plan_corpus_sha256"), "plan_corpus_sha256", issues, pattern=_SHA256)
    campaigns = _array(index.get("campaigns"), "campaigns", issues, nonempty=True) or []
    campaign_ids: set[str] = set()
    graph: dict[str, set[str]] = {}
    for position, item in enumerate(campaigns):
        path = f"campaigns/{position}"
        campaign = _object(
            item, path, {"campaign_id", "template_path", "template_sha256", "depends_on"}, issues
        )
        if campaign is None:
            continue
        campaign_id = _string(
            campaign.get("campaign_id"), f"{path}/campaign_id", issues, pattern=_TOKEN
        )
        _repository_path(campaign.get("template_path"), f"{path}/template_path", issues)
        _string(campaign.get("template_sha256"), f"{path}/template_sha256", issues, pattern=_SHA256)
        dependencies = _string_set(campaign.get("depends_on"), f"{path}/depends_on", issues)
        if isinstance(campaign_id, str):
            if campaign_id in campaign_ids:
                issues.append(f"campaigns: duplicate campaign_id {campaign_id!r}")
            campaign_ids.add(campaign_id)
            graph[campaign_id] = set(dependencies)
    for campaign_id, dependencies in graph.items():
        if campaign_id in dependencies:
            issues.append(f"campaign {campaign_id!r} may not depend on itself")
        unknown = dependencies - campaign_ids
        if unknown:
            issues.append(f"campaign {campaign_id!r} has unknown dependencies {sorted(unknown)!r}")
    found_cycle = _cycle(graph)
    if found_cycle is not None:
        issues.append(f"campaign dependency cycle: {' -> '.join(found_cycle)}")
    integrator = index.get("integrator_campaign_id")
    if not isinstance(integrator, str) or integrator not in campaign_ids:
        issues.append("integrator_campaign_id: unknown campaign")
    elif any(integrator in dependencies for dependencies in graph.values()):
        issues.append("integrator_campaign_id: must name a terminal campaign")
    else:
        unreachable = sorted(
            campaign_id
            for campaign_id in campaign_ids
            if campaign_id != integrator and not _reachable(graph, integrator, campaign_id)
        )
        if unreachable:
            issues.append(
                "integrator_campaign_id: integrator must depend transitively on every other "
                f"campaign; unreachable {unreachable!r}"
            )


def _canonical_work_unit(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    for field in (
        "authority_scope_refs",
        "depends_on",
        "capability_refs",
        "completion_gate_refs",
        "hil_checkpoint_refs",
        "stop_condition_refs",
    ):
        if isinstance(result.get(field), list):
            result[field].sort()
    if isinstance(result.get("task_refs"), list):
        result["task_refs"].sort(
            key=lambda x: (x.get("source_id", ""), x.get("obligation_id", ""), x.get("task_id", ""))
        )
    claim = result.get("claim_set")
    if isinstance(claim, dict):
        for field in (
            "path_prefixes",
            "symbols",
            "subjects",
            "semantic_resources",
            "data_directories",
        ):
            if isinstance(claim.get(field), list):
                claim[field].sort()
        if isinstance(claim.get("ports"), list):
            claim["ports"].sort(
                key=lambda x: (x.get("transport", ""), x.get("port", -1), x.get("bind_scope", ""))
            )
    if isinstance(result.get("acceptance_commands"), list):
        for command in result["acceptance_commands"]:
            if isinstance(command, dict):
                for field in ("expected_exit_codes", "evidence_refs"):
                    if isinstance(command.get(field), list):
                        command[field].sort()
        result["acceptance_commands"].sort(key=lambda x: x.get("command_id", ""))
    evidence = result.get("exit_evidence")
    if isinstance(evidence, dict):
        for field in ("required_receipt_kinds", "required_artifact_refs"):
            if isinstance(evidence.get(field), list):
                evidence[field].sort()
    return result


def _canonical_record(value: Mapping[str, Any]) -> dict[str, Any]:
    kind = value.get("record_kind")
    if kind == "work_unit":
        return _canonical_work_unit(value)
    result = copy.deepcopy(dict(value))
    if kind in {"campaign_template", "campaign_run"} and isinstance(
        result.get("authority_refs"), list
    ):
        for ref in result["authority_refs"]:
            if isinstance(ref, dict) and isinstance(ref.get("scopes"), list):
                ref["scopes"].sort()
        result["authority_refs"].sort(key=lambda x: x.get("source_id", ""))
    if kind == "campaign_template":
        if isinstance(result.get("capability_refs"), list):
            result["capability_refs"].sort()
        if isinstance(result.get("work_units"), list):
            result["work_units"] = [_canonical_work_unit(x) for x in result["work_units"]]
            result["work_units"].sort(key=lambda x: x.get("work_unit_id", ""))
        hil = result.get("human_in_loop")
        if isinstance(hil, dict) and isinstance(hil.get("checkpoints"), list):
            hil["checkpoints"].sort()
        if isinstance(result.get("stop_conditions"), list):
            result["stop_conditions"].sort()
    elif kind == "campaign_run":
        if isinstance(result.get("work_units"), list):
            for wrapper in result["work_units"]:
                if isinstance(wrapper, dict) and isinstance(wrapper.get("record"), Mapping):
                    wrapper["record"] = _canonical_work_unit(wrapper["record"])
            result["work_units"].sort(key=lambda x: x.get("work_unit_id", ""))
        if isinstance(result.get("capability_receipts"), list):
            result["capability_receipts"].sort(key=lambda x: x.get("capability_ref", ""))
    elif kind == "programme_campaign_index" and isinstance(result.get("campaigns"), list):
        for campaign in result["campaigns"]:
            if isinstance(campaign, dict) and isinstance(campaign.get("depends_on"), list):
                campaign["depends_on"].sort()
        result["campaigns"].sort(key=lambda x: x.get("campaign_id", ""))
    return result


def _canonical_bytes_unchecked(value: Mapping[str, Any]) -> bytes:
    normalized = _canonical_record(value)
    return (
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def canonical_campaign_bytes(value: Mapping[str, Any]) -> bytes:
    """Return deterministic bytes without mutating or interpreting the supplied record."""

    if not isinstance(value, Mapping):
        raise CampaignContractError("<root>: expected object")
    return _canonical_bytes_unchecked(value)


def _result(value: Mapping[str, Any], id_field: str) -> CampaignContractResult:
    data = _canonical_bytes_unchecked(value)
    return CampaignContractResult(
        record_kind=value["record_kind"],
        record_id=value[id_field],
        digest=hashlib.sha256(data).hexdigest(),
        canonical_bytes=data,
    )


def validate_work_unit(value: Mapping[str, Any]) -> CampaignContractResult:
    """Validate one work unit without reading its referenced instruction artifact."""

    issues: list[str] = []
    _validate_string_policy(value, issues)
    _validate_work_unit_record(value, issues)
    if issues:
        raise CampaignContractError(issues)
    return _result(value, "work_unit_id")


def validate_campaign_template(value: Mapping[str, Any]) -> CampaignContractResult:
    """Validate a project-authored campaign typeset without planning or execution."""

    issues: list[str] = []
    _validate_string_policy(value, issues)
    _validate_template_record(value, issues)
    if issues:
        raise CampaignContractError(issues)
    return _result(value, "template_id")


def validate_campaign_run(
    value: Mapping[str, Any],
    *,
    template: Mapping[str, Any] | None = None,
    programme_index: Mapping[str, Any] | None = None,
) -> CampaignContractResult:
    """Validate one immutable run and optionally prove its exact template/index bindings."""

    issues: list[str] = []
    _validate_string_policy(value, issues)
    _validate_run_record(value, issues)
    template_result: CampaignContractResult | None = None
    if template is not None and not issues:
        try:
            template_result = validate_campaign_template(template)
        except CampaignContractError as error:
            issues.extend(f"template: {issue}" for issue in error.issues)
        else:
            if value.get("template_sha256") != template_result.digest:
                issues.append("template_sha256: does not match supplied template")
            for field in ("campaign_id", "template_id", "plan_corpus_sha256"):
                if value.get(field) != template.get(field):
                    issues.append(f"{field}: does not match supplied template")
            if (
                _canonical_record(
                    {
                        "record_kind": "campaign_run",
                        "authority_refs": value.get("authority_refs", []),
                    }
                )["authority_refs"]
                != _canonical_record(
                    {
                        "record_kind": "campaign_run",
                        "authority_refs": template.get("authority_refs", []),
                    }
                )["authority_refs"]
            ):
                issues.append("authority_refs: do not match supplied template")
            run_units = value.get("work_units", [])
            expected_units = {
                unit["work_unit_id"]: (validate_work_unit(unit).digest, _canonical_work_unit(unit))
                for unit in template.get("work_units", [])
            }
            actual_units = {
                wrapper.get("work_unit_id"): (
                    wrapper.get("work_unit_sha256"),
                    _canonical_work_unit(wrapper.get("record", {})),
                )
                for wrapper in run_units
                if isinstance(wrapper, Mapping) and isinstance(wrapper.get("work_unit_id"), str)
            }
            if actual_units != expected_units:
                issues.append("work_units: do not match supplied template records and digests")
            actual_capabilities = {
                item.get("capability_ref")
                for item in value.get("capability_receipts", [])
                if isinstance(item, Mapping) and isinstance(item.get("capability_ref"), str)
            }
            if actual_capabilities != set(template.get("capability_refs", [])):
                issues.append("capability bindings do not exactly cover template capability_refs")
            policy_integrator = (
                template.get("policies", {}).get("integration", {}).get("integrator_work_unit_id")
            )
            if value.get("integrator", {}).get("work_unit_id") != policy_integrator:
                issues.append("integrator/work_unit_id: does not match template policy")
            created = _timestamp(value.get("created_at"), "created_at", [])
            expires = _timestamp(value.get("expires_at"), "expires_at", [])
            maximum = template.get("policies", {}).get("expiry", {}).get("maximum_run_seconds")
            if (
                created is not None
                and expires is not None
                and type(maximum) is int
                and (expires - created).total_seconds() > maximum
            ):
                issues.append("expires_at: exceeds template maximum_run_seconds")
    if programme_index is not None and not issues:
        try:
            index_result = validate_programme_campaign_index(programme_index)
        except CampaignContractError as error:
            issues.extend(f"programme_index: {issue}" for issue in error.issues)
        else:
            if value.get("programme_index_sha256") != index_result.digest:
                issues.append("programme_index_sha256: does not match supplied index")
            if value.get("plan_corpus_sha256") != programme_index.get("plan_corpus_sha256"):
                issues.append("plan_corpus_sha256: does not match supplied index")
            campaign_ids = {
                x.get("campaign_id")
                for x in programme_index.get("campaigns", [])
                if isinstance(x, Mapping)
            }
            if value.get("campaign_id") not in campaign_ids:
                issues.append("campaign_id: absent from supplied programme index")
            if template is not None and template_result is not None:
                matching = [
                    item
                    for item in programme_index.get("campaigns", [])
                    if isinstance(item, Mapping)
                    and item.get("campaign_id") == value.get("campaign_id")
                ]
                if (
                    len(matching) != 1
                    or matching[0].get("template_sha256") != template_result.digest
                ):
                    issues.append(
                        "programme_index: selected campaign does not bind the supplied template"
                    )
                if programme_index.get("programme_id") != template.get("programme_id"):
                    issues.append("programme_index: programme_id does not match supplied template")
    if issues:
        raise CampaignContractError(issues)
    return _result(value, "run_id")


def validate_programme_campaign_index(value: Mapping[str, Any]) -> CampaignContractResult:
    """Validate a content-pinned project campaign set and its DAG."""

    issues: list[str] = []
    _validate_string_policy(value, issues)
    _validate_index_record(value, issues)
    if issues:
        raise CampaignContractError(issues)
    return _result(value, "index_id")
