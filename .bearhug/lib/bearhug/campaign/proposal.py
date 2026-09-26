"""Compile one coherent approved request into the existing capsule execution contract.

This default does not interpret or approve intent. It preserves all of the caller's obligations,
invariants, scope and checks in one capsule. A project can supply an explicit multi-capsule plan
when the request has independently meaningful completion boundaries.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.campaign.capsules import (
    canonical_capsule_bytes,
    compile_capsule_plan,
    validate_intent_envelope,
)
from bearhug.campaign.prepared import (
    PreparedCampaignError,
    _absolute_directory,
    _absolute_file,
    _canonical,
    _exact_json,
    _read_regular,
    _write_create_only,
    prepare_campaign,
)
from bearhug.paths import RUNS_DIR, assert_writable

# Every capsule carries this, and the review packet is built from it. Measured on a
# six-task plan: the review packet Bear Hug itself compiles was 85,109 bytes -- intent
# 31k, authority sources 18k, reconciliation 13k, the plan 13k -- against the 57,344 the
# previous 64KiB/16k-token budget allowed, so no capsule in that plan could be reviewed at
# all. Nothing here declares what a provider actually accepts, so this is a floor for the
# supported models rather than a measurement of one: roughly 3x the observed packet, and
# still a fraction of a current context window.
DEFAULT_CONTEXT_BUDGET = {
    "mode": "explicit",
    "max_bytes": 8 * 1024 * 1024,
    "max_tokens": 2 * 1024 * 1024,
    "reserve_bytes": 256 * 1024,
    "reserve_tokens": 65536,
}

def _write_derived(path: Path, raw: bytes) -> None:
    if path.exists():
        if _read_regular(path, maximum=4 * 1024 * 1024) != raw:
            raise PreparedCampaignError(f"sealed request input changed: {path}")
    else:
        _write_create_only(path, raw)


_BOARD_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_TASK_ROW_FIELDS = frozenset({"id", "title", "depends_on", "done_when"})


def board_capsule_id(task_id: str) -> str:
    """Return the stable capsule identity for one accepted project-board task."""
    return "capsule.task." + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]


def _task_obligation_id(task_id: str) -> str:
    return "obligation.task." + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]


def _normalize_task_rows(task_rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if task_rows is None:
        return []
    if isinstance(task_rows, (str, bytes, bytearray)) or not isinstance(task_rows, Sequence):
        raise PreparedCampaignError("task_rows must be an array of accepted board rows")
    result: list[dict[str, Any]] = []
    by_id: set[str] = set()
    for index, raw in enumerate(task_rows):
        if not isinstance(raw, Mapping):
            raise PreparedCampaignError(f"task_rows[{index}] must be an object")
        missing = _TASK_ROW_FIELDS - set(raw)
        if missing:
            raise PreparedCampaignError(
                f"task_rows[{index}] is missing {', '.join(sorted(missing))}"
            )
        task_id = raw["id"]
        if not isinstance(task_id, str) or not _BOARD_ID.fullmatch(task_id):
            raise PreparedCampaignError(f"task_rows[{index}].id is not a valid board task ID")
        if task_id in by_id:
            raise PreparedCampaignError(f"task_rows repeats task ID {task_id!r}")
        title, done_when = raw["title"], raw["done_when"]
        if not isinstance(title, str) or not title.strip():
            raise PreparedCampaignError(f"task_rows[{index}].title must be non-empty text")
        if not isinstance(done_when, str) or not done_when.strip():
            raise PreparedCampaignError(f"task_rows[{index}].done_when must be non-empty text")
        if len(title) > 8192 or len(done_when) > 8192:
            raise PreparedCampaignError(f"task_rows[{index}] title or done_when is too long")
        depends_on = raw["depends_on"]
        if not isinstance(depends_on, list) or any(
            not isinstance(dep, str) or not _BOARD_ID.fullmatch(dep) for dep in depends_on
        ):
            raise PreparedCampaignError(f"task_rows[{index}].depends_on must contain valid IDs")
        if len(depends_on) != len(set(depends_on)):
            raise PreparedCampaignError(f"task_rows[{index}].depends_on repeats a task ID")
        by_id.add(task_id)
        result.append(
            {
                "id": task_id,
                "title": title,
                "depends_on": list(depends_on),
                "done_when": done_when,
            }
        )
    ids = {row["id"] for row in result}
    for row in result:
        unknown = sorted(set(row["depends_on"]) - ids)
        if unknown:
            raise PreparedCampaignError(
                f"task {row['id']!r} depends on unknown task {unknown[0]!r}"
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise PreparedCampaignError(f"task dependency cycle at {task_id!r}")
        if task_id in visited:
            return
        visiting.add(task_id)
        row = next(item for item in result if item["id"] == task_id)
        for dependency in row["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for row in result:
        visit(row["id"])
    return result


def _task_obligation_statement(row: Mapping[str, Any]) -> str:
    return f"Task {row['id']}: {row['title']}; Done when: {row['done_when']}"


def _obligation_statement(text: str) -> str:
    """Project a prompt into the single-line contract field without touching raw custody."""
    return " ".join(text.split())


def _task_coverage(
    intent: Mapping[str, Any], task_rows: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    """Return complete, unique coverage and the task obligation refs by board ID.

    The capsule contract deliberately requires every declared obligation to be covered exactly
    once. Shared template obligations belong to the deterministic terminal task capsule;
    per-task obligations belong to their matching capsule. Shared envelopes, bindings and
    invariants are still carried by every capsule below.
    """
    obligations = [
        {"source_id": row["source_id"], "obligation_id": row["obligation_id"]}
        for row in intent["obligations"]
    ]
    by_obligation = {row["obligation_id"]: row for row in obligations}
    task_refs: dict[str, dict[str, str]] = {}
    for row in task_rows:
        obligation_id = _task_obligation_id(row["id"])
        try:
            task_refs[row["id"]] = by_obligation[obligation_id]
        except KeyError as exc:
            raise PreparedCampaignError(
                f"intent is missing the accepted obligation for task {row['id']!r}"
            ) from exc
    task_ids = {_task_obligation_id(row["id"]) for row in task_rows}
    baseline = [row for row in obligations if row["obligation_id"] not in task_ids]
    return baseline, task_refs


def _final_task_id(task_rows: Sequence[Mapping[str, Any]]) -> str:
    """Choose one deterministic DAG sink for shared completion obligations."""

    # _normalize_task_rows has already checked the DAG. A sink has no dependents,
    # so making it wait for the other tasks cannot introduce a cycle.
    depended_on = {dependency for row in task_rows for dependency in row["depends_on"]}
    return next(row["id"] for row in reversed(task_rows) if row["id"] not in depended_on)



def propose_capsule_plan(
    intent: dict,
    subject: dict,
    execution: dict,
    *,
    task_rows: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict, dict]:
    """Derive operational structure without generating new semantic or mutation authority."""
    identity = validate_intent_envelope(intent).digest
    for approval in (intent["approval"], intent["campaign_envelope"]["approval"]):
        if approval["mode"] not in {"human_approved", "project_sealed"}:
            raise PreparedCampaignError(
                "plan compilation requires existing intent/envelope approval"
            )
        if approval["approved_by"] == "adapter":
            raise PreparedCampaignError("adapter approval cannot authorize request execution")
    if intent["mode"] != "native_v2":
        raise PreparedCampaignError("request compilation requires native_v2 intent")
    commands = execution.get("validation_commands")
    if not isinstance(commands, dict) or not commands:
        raise PreparedCampaignError("request needs explicit validation_commands")
    rows = _normalize_task_rows(task_rows)
    capsule_ids = [board_capsule_id(row["id"]) for row in rows] or ["capsule.request"]
    artifacts = execution.get("artifact_bindings", {"capsule.request": {}})
    if not isinstance(artifacts, dict):
        raise PreparedCampaignError("artifact_bindings must be an object")
    if set(artifacts) == {"capsule.request"} and rows:
        default_artifacts = artifacts["capsule.request"]
        if not isinstance(default_artifacts, dict):
            raise PreparedCampaignError("artifact_bindings.capsule.request must be an object")
        artifacts = {capsule_id: copy.deepcopy(default_artifacts) for capsule_id in capsule_ids}
    elif set(artifacts) != set(capsule_ids):
        expected = ", ".join(capsule_ids)
        raise PreparedCampaignError(
            "artifact_bindings must name every generated capsule exactly "
            f"({expected}); explicit bindings for another plan are incompatible"
        )
    for capsule_id in capsule_ids:
        if not isinstance(artifacts[capsule_id], dict):
            raise PreparedCampaignError(f"artifact_bindings.{capsule_id} must be an object")
    envelope = intent["campaign_envelope"]
    scope = copy.deepcopy(envelope["mutation_envelope"])
    gate_refs = ["gate.request-validation"]
    baseline, task_refs = _task_coverage(intent, rows) if rows else (
        [
            {"source_id": row["source_id"], "obligation_id": row["obligation_id"]}
            for row in intent["obligations"]
        ],
        {},
    )
    final_task_id = _final_task_id(rows) if rows else None

    capsules: list[dict[str, Any]] = []
    for index, capsule_id in enumerate(capsule_ids):
        row = rows[index] if rows else None
        coverage = list(baseline) if row is None or row["id"] == final_task_id else []
        if row is not None:
            coverage.append(copy.deepcopy(task_refs[row["id"]]))
        dependencies = (
            [board_capsule_id(dependency) for dependency in row["depends_on"]]
            if row is not None
            else []
        )
        if row is not None and row["id"] == final_task_id:
            existing = set(dependencies)
            dependencies.extend(
                board_capsule_id(other["id"])
                for other in rows
                if other["id"] != row["id"] and board_capsule_id(other["id"]) not in existing
            )
        capsules.append({
            "capsule_id": capsule_id,
            "intent_ref": intent["intent_envelope_id"],
            "obligation_coverage": coverage,
            "binding_refs": [row["binding_id"] for row in intent["bindings"]],
            "invariant_refs": [row["invariant_id"] for row in intent["invariants"]],
            "depends_on": dependencies,
            "mutation_envelope": copy.deepcopy(scope),
            "expected_surface": {
                key: copy.deepcopy(scope[key])
                for key in (
                    "path_prefixes", "symbols", "subjects", "semantic_resources", "data_directories"
                )
            },
            "validation_profiles": [{
                "profile_id": "profile.request", "command_refs": sorted(commands),
                "gate_refs": gate_refs,
            }],
            "completion_boundary": {
                "required_gate_refs": [*gate_refs, "gate.review"],
                "required_receipt_kinds": ["author", "review", "test"],
                "required_artifact_refs": sorted(artifacts[capsule_id]),
            },
            "reconciliation_triggers": ["validation_failure", "authority_change", "scope_pressure"],
            "hil_policy_refs": list(envelope["policy_snapshot"]["human_in_loop"]["checkpoints"]),
            "provider_capability_needs": list(envelope["policy_snapshot"]["capability_refs"]),
            "context_budget": copy.deepcopy(DEFAULT_CONTEXT_BUDGET),
            "legacy_work_unit": None,
        })
    plan = compile_capsule_plan(
        plan_id=f"plan.request.{identity[:24]}", intent_envelope=intent,
        subject=subject, capsules=capsules,
    )
    config = copy.deepcopy(execution)
    config["artifact_bindings"] = copy.deepcopy(artifacts)
    return plan, config


def prepare_from_intent(
    subject,
    intent_path,
    policy_path,
    execution_path,
    *,
    state_root=None,
    task_rows: Sequence[Mapping[str, Any]] | None = None,
    **options,
):
    """Write derived inputs privately, then use the ordinary prepared campaign boundary."""
    from bearhug.campaign.importer import inspect_subject

    checkout = inspect_subject(subject)
    intent_file = _absolute_file(intent_path, label="intent")
    config_file = _absolute_file(execution_path, label="execution config")
    policy_file = _absolute_file(policy_path, label="provider policy")
    intent, _ = _exact_json(intent_file, label="intent")
    execution, _ = _exact_json(config_file, label="execution config")
    plan, config = propose_capsule_plan(intent, {
        "repository_id": "repo." + checkout.repository_common_dir_sha256[:24],
        "base_oid": checkout.head_oid,
        "base_tree_sha256": hashlib.sha256(checkout.tree_oid.encode("ascii")).hexdigest(),
    }, execution, task_rows=task_rows)
    # Resolve the one relative locator before moving derived configuration to private custody.
    qualification = config.get("qualification_index")
    if not isinstance(qualification, str) or not qualification:
        raise PreparedCampaignError("request needs an explicit qualification_index")
    path = Path(qualification).expanduser()
    config["qualification_index"] = str(
        (path if path.is_absolute() else config_file.parent / path).resolve()
    )
    data = canonical_capsule_bytes(plan)
    config_data = _canonical(config)
    key = hashlib.sha256(data + config_data).hexdigest()
    root = Path(state_root) if state_root is not None else RUNS_DIR / "requests" / key[:24]
    root = assert_writable(root.expanduser().absolute())
    if root.resolve().is_relative_to(checkout.root):
        if state_root is not None:
            raise PreparedCampaignError(
                "derived plan must be outside the selected project: "
                f"{root} is inside {checkout.root}. Pass --state-root outside the "
                "project, a sibling directory works."
            )
        raise PreparedCampaignError(
            "derived plan must be outside the selected project: "
            f"{root} is inside {checkout.root}. No --state-root was given, so Bear "
            f"Hug's runs directory ({RUNS_DIR}) set the default and it is inside the "
            "project. Pass --state-root outside the project, clone Bear Hug as a "
            "sibling of it, or set BEARHUG_ARTIFACT_ROOT to a directory outside it."
        )
    root = _absolute_directory(root, label="request state root", create=True)
    compiled = root / "compiled"
    compiled.mkdir(mode=0o700, parents=True, exist_ok=True)
    plan_file = compiled / f"{hashlib.sha256(data).hexdigest()}.plan.json"
    derived_config = compiled / f"{hashlib.sha256(config_data).hexdigest()}.execution.json"
    _write_derived(plan_file, data)
    _write_derived(derived_config, config_data)
    if (root / "prepared.json").exists():
        from bearhug.campaign.prepared import load_prepared
        prepared = load_prepared(root / "prepared.json")
        if prepared.plan != plan or prepared.record["digests"][
            "execution_config_sha256"
        ] != hashlib.sha256(config_data).hexdigest():
            raise PreparedCampaignError("state root belongs to a different prepared request")
        return prepared
    return prepare_campaign(
        subject, intent_file, plan_file, policy_file, derived_config,
        state_root=root, **options,
    )


def prepare_terminal_request(
    subject,
    template_path,
    policy_path,
    execution_path,
    raw_event: bytes,
    *,
    state_root,
    task_rows: Sequence[Mapping[str, Any]] | None = None,
):
    """Bind a native user request to explicitly enrolled project limits, then prepare it.

    The original event is sealed as an authority source outside the project. Re-delivery uses
    the same prepared run; the terminal driver serializes intake before calling this function.
    No text is interpreted as permission to widen the enrolled mutation or provider policy.
    """
    from bearhug.campaign.prepared import load_prepared
    from bearhug.project_terminal import _json_load

    event = _json_load(raw_event, label="native user request", maximum=1024 * 1024)
    if not isinstance(event, dict) or event.get("hook_event_name") != "UserPromptSubmit":
        raise PreparedCampaignError("terminal intake requires UserPromptSubmit")
    prompt, session = event.get("prompt"), event.get("session_id")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16384:
        raise PreparedCampaignError("native user prompt must contain 1–16384 characters")
    if not isinstance(session, str) or not session.strip() or len(session) > 256:
        raise PreparedCampaignError("native user request needs a bounded session identity")
    project = Path(subject).expanduser().resolve(strict=True)
    normalized_tasks = _normalize_task_rows(task_rows)
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise PreparedCampaignError("native user request needs its actual project cwd")
    if not Path(cwd).resolve(strict=True).is_relative_to(project):
        raise PreparedCampaignError("native user request cwd is outside enrolled project")
    template_file = _absolute_file(template_path, label="intent template")
    config_file = _absolute_file(execution_path, label="execution config")
    policy_file = _absolute_file(policy_path, label="provider policy")
    template, template_raw = _exact_json(template_file, label="intent template")
    config, config_raw = _exact_json(config_file, label="execution config")
    _, policy_raw = _exact_json(policy_file, label="provider policy")
    validate_intent_envelope(template)
    if template["mode"] != "native_v2" or any(
        row["mode"] not in {"human_approved", "project_sealed"}
        or row["approved_by"] == "adapter"
        for row in (template["approval"], template["campaign_envelope"]["approval"])
    ):
        raise PreparedCampaignError("terminal template requires existing native approval")
    # Length-delimited canonical inputs avoid ambiguous concatenations and bind the exact setup.
    key = hashlib.sha256(_canonical([
        str(project), hashlib.sha256(raw_event).hexdigest(),
        hashlib.sha256(template_raw).hexdigest(), hashlib.sha256(config_raw).hexdigest(),
        hashlib.sha256(policy_raw).hexdigest(), str(config_file), str(policy_file),
        normalized_tasks,
    ])).hexdigest()
    root = assert_writable(Path(state_root).expanduser().absolute() / "requests" / key)
    if root.resolve().is_relative_to(project):
        raise PreparedCampaignError("terminal request state must be outside the project")
    root = _absolute_directory(root, label="terminal request state", create=True)
    request_id = "request." + hashlib.sha256(raw_event).hexdigest()
    intent = copy.deepcopy(template)
    intent["intent_envelope_id"] = "intent." + key
    # Intent strings are closed contract fields and reject control characters.  The exact event
    # (including the accepted plan's original line breaks) remains in the request authority
    # source below; the goal/obligation projection is only the validator-safe display form.
    intent["goal"] = _obligation_statement(prompt)
    intent["authority_refs"].append({
        "source_id": request_id, "content_sha256": hashlib.sha256(raw_event).hexdigest(),
        "scopes": ["request"],
    })
    intent["obligations"].append({
        "source_id": request_id,
        "obligation_id": "obligation.request",
        "statement": _obligation_statement(prompt),
    })
    for row in normalized_tasks:
        intent["obligations"].append({
            "source_id": request_id,
            "obligation_id": _task_obligation_id(row["id"]),
            "statement": _task_obligation_statement(row),
        })
    intent_file = root / "request.intent.json"
    approved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if intent_file.exists():
        existing, _ = _exact_json(intent_file, label="sealed native request")
        validate_intent_envelope(existing)
        approved_at = existing["approval"]["approved_at"]
    intent["approval"] = {
        "mode": "human_approved", "evidence_refs": [hashlib.sha256(raw_event).hexdigest()],
        "approved_at": approved_at,
        "approved_by": "user.native." + hashlib.sha256(session.encode()).hexdigest()[:24],
    }
    validate_intent_envelope(intent)
    config = copy.deepcopy(config)
    config["authority_sources"].append({
        "source_id": request_id, "request": raw_event.decode("utf-8"),
    })
    qualification = config.get("qualification_index")
    if not isinstance(qualification, str) or not qualification:
        raise PreparedCampaignError("request needs an explicit qualification_index")
    path = Path(qualification).expanduser()
    config["qualification_index"] = str(
        (path if path.is_absolute() else config_file.parent / path).resolve()
    )
    derived_config = root / "request.execution.json"
    _write_derived(intent_file, canonical_capsule_bytes(intent))
    _write_derived(derived_config, _canonical(config))
    locator = root / "prepared.json"
    if locator.exists():
        return load_prepared(locator)
    return prepare_from_intent(
        project, intent_file, policy_file, derived_config, state_root=root,
        task_rows=normalized_tasks,
    )
