"""Post-turn operational evidence for the Codex App Server.

This module records what the local App Server reported about one completed turn.  It deliberately
does not turn those observations into backend identity attestation.  The evidence is private
custody: callers can use the content digest to bind it to a provider receipt without copying
provider prompts or response text into the public receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class OperationalEvidenceError(ValueError):
    """Operational evidence is malformed, incomplete, or does not match custody."""


_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_SHA256_LENGTH = 64


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Mapping[str, Any], *, omit_digest: bool = False) -> bytes:
    material = dict(value)
    if omit_digest:
        material.pop("content_sha256", None)
    try:
        return (
            json.dumps(
                material,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OperationalEvidenceError(
            f"operational evidence is not canonical JSON: {exc}"
        ) from exc


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OperationalEvidenceError(f"JSON object repeats key {key!r}")
        result[key] = value
    return result


def _parse_jsonl(raw: bytes, name: str) -> list[tuple[dict[str, Any], bytes]]:
    if not raw or not raw.endswith(b"\n"):
        raise OperationalEvidenceError(f"{name} custody must be non-empty newline-terminated JSONL")
    records: list[tuple[dict[str, Any], bytes]] = []
    for ordinal, line in enumerate(raw.splitlines(), start=1):
        try:
            value = json.loads(line, object_pairs_hook=_closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise OperationalEvidenceError(
                f"{name} custody has invalid JSON at line {ordinal}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise OperationalEvidenceError(f"{name} custody line {ordinal} is not a JSON object")
        records.append((value, line))
    return records


def _sha(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OperationalEvidenceError(f"{label} must be lowercase SHA-256")
    return value


def _request(
    messages: list[tuple[dict[str, Any], bytes]],
    method: str,
) -> tuple[int | str, dict[str, Any], bytes]:
    matches = [
        (message.get("id"), message, line)
        for message, line in messages
        if message.get("method") == method
    ]
    if len(matches) != 1:
        raise OperationalEvidenceError(
            f"client custody must contain exactly one {method} request, observed {len(matches)}"
        )
    request_id, message, line = matches[0]
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        raise OperationalEvidenceError(f"{method} request has no valid id")
    return request_id, message, line


def _response(
    messages: list[tuple[dict[str, Any], bytes]], request_id: int | str, method: str
) -> tuple[dict[str, Any], bytes, int]:
    matches = [
        (index, message, line)
        for index, (message, line) in enumerate(messages)
        if message.get("id") == request_id
    ]
    if len(matches) != 1:
        raise OperationalEvidenceError(
            f"server custody must contain exactly one {method} response, observed {len(matches)}"
        )
    index, message, line = matches[0]
    if "error" in message:
        raise OperationalEvidenceError(f"{method} request failed: {message['error']!r}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise OperationalEvidenceError(f"{method} response has no result object")
    return result, line, index


def _all_request_ids(messages: list[tuple[dict[str, Any], bytes]]) -> None:
    ids: list[tuple[type[Any], str]] = []
    for message, _ in messages:
        if "id" in message:
            request_id = message["id"]
            ids.append((type(request_id), str(request_id)))
    if len(ids) != len(set(ids)):
        raise OperationalEvidenceError("client custody contains duplicate request ids")


def _read_source(path: Path, label: str) -> bytes:
    if not path.is_absolute() or str(path) != str(path.resolve(strict=False)):
        raise OperationalEvidenceError(f"{label} must be a canonical absolute path")
    if path.is_symlink():
        raise OperationalEvidenceError(f"{label} may not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OperationalEvidenceError(f"cannot read {label}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SOURCE_BYTES:
            raise OperationalEvidenceError(f"{label} is not a bounded regular file")
        value = bytearray()
        while len(value) <= _MAX_SOURCE_BYTES:
            chunk = os.read(descriptor, min(1024 * 1024, _MAX_SOURCE_BYTES + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        after = os.fstat(descriptor)
        if len(value) > _MAX_SOURCE_BYTES or (
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
            raise OperationalEvidenceError(f"{label} changed while being read")
        return bytes(value)
    finally:
        os.close(descriptor)


def _source_record(path: Path | None, expected: str | None, label: str) -> dict[str, Any] | None:
    if path is None:
        if expected is not None:
            raise OperationalEvidenceError(f"{label} path is required when its digest is supplied")
        return None
    raw = _read_source(path, label)
    digest = _sha256(raw)
    if expected is not None and digest != _sha(expected, f"{label} expected digest"):
        raise OperationalEvidenceError(f"{label} bytes do not match the expected digest")
    return {"path": str(path), "sha256": digest, "size": len(raw)}


def _source_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if not isinstance(value, dict):
        return paths
    path = value.get("file")
    if isinstance(path, str) and path:
        paths.append(path)
    folder = value.get("dotCodexFolder")
    if isinstance(folder, str) and folder:
        paths.append(folder)
    return paths


def _provider_config_sources(
    config_result: Mapping[str, Any], *, read_source_bytes: bool = True
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    origins = config_result.get("origins")
    if isinstance(origins, dict):
        for key, metadata in sorted(origins.items()):
            if not isinstance(metadata, dict):
                continue
            name = metadata.get("name")
            version = metadata.get("version")
            source = {"key": key, "name": name, "version": version}
            paths = _source_paths(name)
            source["paths"] = paths
            if read_source_bytes:
                source["source_bytes"] = [
                    _provider_source_digest(path) for path in paths
                ]
            sources.append(source)
    return sources


def _provider_source_digest(path: str) -> dict[str, Any]:
    candidate = Path(path)
    value: dict[str, Any] = {"path": path}
    try:
        raw = _read_source(candidate, "provider config source")
    except OperationalEvidenceError as exc:
        value["available"] = False
        value["error"] = str(exc)
    else:
        value.update({"available": True, "sha256": _sha256(raw), "size": len(raw)})
    return value


def _hook_record(hook: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "key",
        "eventName",
        "source",
        "sourcePath",
        "currentHash",
        "enabled",
        "isManaged",
        "trustStatus",
        "displayOrder",
        "timeoutSec",
        "handlerType",
        "pluginId",
        "matcher",
    )
    return {field: hook[field] for field in fields if field in hook}


def _event_records(
    server_messages: list[tuple[dict[str, Any], bytes]], thread_id: str, turn_id: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for sequence, (message, line) in enumerate(server_messages):
        method = message.get("method")
        if method not in {"hook/started", "hook/completed"}:
            continue
        params = message.get("params")
        run = params.get("run") if isinstance(params, dict) else None
        if not isinstance(run, dict):
            raise OperationalEvidenceError(f"{method} has no hook run summary")
        run_id = run.get("id")
        if not isinstance(run_id, str) or not run_id:
            raise OperationalEvidenceError(f"{method} has no hook run id")
        observed_thread = None
        observed_turn = None
        if isinstance(params, dict):
            observed_thread = params.get("threadId")
            observed_turn = params.get("turnId")
            if observed_thread is not None and observed_thread != thread_id:
                raise OperationalEvidenceError("hook event thread id does not match turn custody")
            if observed_turn is not None and observed_turn != turn_id:
                raise OperationalEvidenceError("hook event turn id does not match turn custody")
        event: dict[str, Any] = {
            "sequence": sequence,
            "method": method,
            "raw_line_sha256": _sha256(line),
            "run_id": run_id,
            "identity_observed": (
                observed_thread == thread_id
                and (observed_turn is None or observed_turn == turn_id)
            ),
        }
        if isinstance(observed_thread, str):
            event["thread_id"] = observed_thread
        if isinstance(observed_turn, str):
            event["turn_id"] = observed_turn
        for field in (
            "status",
            "eventName",
            "scope",
            "source",
            "sourcePath",
            "handlerType",
            "executionMode",
            "displayOrder",
        ):
            if field in run:
                event[field] = run[field]
        events.append(event)
    return events


def build_operational_evidence(
    *,
    raw_client_events: bytes,
    raw_server_events: bytes,
    cwd: str,
    session_id: str,
    thread_id: str,
    turn_id: str,
    argv: bytes = b"",
    stderr: bytes = b"",
    settings_path: Path | None = None,
    settings_sha256: str | None = None,
    rules_path: Path | None = None,
    rules_sha256: str | None = None,
    _validate: bool = True,
) -> dict[str, Any]:
    """Build one validated observational evidence record from a completed raw exchange."""

    if not Path(cwd).is_absolute() or not session_id or not thread_id or not turn_id:
        raise OperationalEvidenceError("cwd and protocol identities must be non-empty")
    client_messages = _parse_jsonl(raw_client_events, "client")
    server_messages = _parse_jsonl(raw_server_events, "server")
    _all_request_ids(client_messages)
    completed = [
        index
        for index, (message, _) in enumerate(server_messages)
        if message.get("method") == "turn/completed"
    ]
    if len(completed) != 1:
        raise OperationalEvidenceError(
            "server custody must contain exactly one turn/completed event"
        )
    completed_index = completed[0]
    # ``thread/goal/get`` responses are id-only JSON-RPC responses, so looking for a server
    # method marker cannot establish the phase boundary.  The native client always sends this
    # request after the operational queries; keep the boundary optional for older fixtures that
    # contain no goal request, but correlate it whenever the request is present.
    goal_requests = [
        (message.get("id"), message)
        for message, _ in client_messages
        if message.get("method") == "thread/goal/get"
    ]
    goal_index = len(server_messages)
    if len(goal_requests) > 1:
        raise OperationalEvidenceError(
            "client custody must contain at most one thread/goal/get request"
        )
    if goal_requests:
        goal_id, _ = goal_requests[0]
        if not isinstance(goal_id, (str, int)) or isinstance(goal_id, bool):
            raise OperationalEvidenceError("thread/goal/get request has no valid id")
        _, _, goal_index = _response(server_messages, goal_id, "thread/goal/get")
        if goal_index <= completed_index:
            raise OperationalEvidenceError(
                "thread/goal/get response must be observed after turn completion"
            )

    config_id, config_request, config_line = _request(client_messages, "config/read")
    hooks_id, hooks_request, hooks_line = _request(client_messages, "hooks/list")
    thread_id_request, thread_request, thread_line = _request(client_messages, "thread/read")
    if config_request.get("params") != {"cwd": cwd, "includeLayers": True}:
        raise OperationalEvidenceError("config/read must include the exact cwd and all layers")
    if hooks_request.get("params") != {"cwds": [cwd]}:
        raise OperationalEvidenceError("hooks/list must target the exact cwd")
    if thread_request.get("params") != {"threadId": thread_id, "includeTurns": False}:
        raise OperationalEvidenceError("thread/read must target the exact thread without turns")

    config_result, config_response_line, config_index = _response(
        server_messages, config_id, "config/read"
    )
    hooks_result, hooks_response_line, hooks_index = _response(
        server_messages, hooks_id, "hooks/list"
    )
    thread_result, thread_response_line, thread_index = _response(
        server_messages, thread_id_request, "thread/read"
    )
    response_indices = (config_index, hooks_index, thread_index)
    if any(index <= completed_index or index >= goal_index for index in response_indices):
        raise OperationalEvidenceError(
            "operational API responses must be between turn completion and goal custody"
        )

    config = config_result.get("config")
    if not isinstance(config, dict):
        raise OperationalEvidenceError("config/read returned no config object")
    layers = config_result.get("layers")
    if layers is not None and not isinstance(layers, list):
        raise OperationalEvidenceError("config/read layers must be an array or null")
    origins = config_result.get("origins")
    if not isinstance(origins, dict):
        raise OperationalEvidenceError("config/read returned no origins object")

    data = hooks_result.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise OperationalEvidenceError("hooks/list must return one cwd entry")
    hook_entry = data[0]
    if hook_entry.get("cwd") != cwd:
        raise OperationalEvidenceError("hooks/list response cwd does not match the launch cwd")
    hooks = hook_entry.get("hooks")
    errors = hook_entry.get("errors")
    warnings = hook_entry.get("warnings")
    if (
        not isinstance(hooks, list)
        or not isinstance(errors, list)
        or not isinstance(warnings, list)
    ):
        raise OperationalEvidenceError("hooks/list returned malformed hook data")
    if any(not isinstance(hook, dict) for hook in hooks):
        raise OperationalEvidenceError("hooks/list returned a malformed hook")

    thread = thread_result.get("thread")
    if not isinstance(thread, dict):
        raise OperationalEvidenceError("thread/read returned no thread object")
    if thread.get("id") != thread_id:
        raise OperationalEvidenceError("thread/read identity does not match turn custody")
    if thread.get("sessionId") is not None and thread.get("sessionId") != session_id:
        raise OperationalEvidenceError("thread/read session id does not match turn custody")
    if thread.get("cwd") != cwd:
        raise OperationalEvidenceError("thread/read cwd does not match the launch cwd")
    observed_model = thread.get("model")
    observed_effort = thread.get("reasoningEffort")
    if not isinstance(observed_model, str) or not observed_model:
        raise OperationalEvidenceError("thread/read returned no configured model")
    if not isinstance(observed_effort, str) or not observed_effort:
        raise OperationalEvidenceError("thread/read returned no configured reasoning effort")

    turn_starts = [
        message
        for message, _ in client_messages
        if message.get("method") == "turn/start"
    ]
    if len(turn_starts) != 1:
        raise OperationalEvidenceError("client custody must contain one turn/start request")
    turn_params = turn_starts[0].get("params")
    if not isinstance(turn_params, dict) or turn_params.get("threadId") != thread_id:
        raise OperationalEvidenceError("turn/start request does not target the observed thread")
    requested_model = turn_params.get("model")
    requested_effort = turn_params.get("effort")
    if requested_model is not None and requested_model != observed_model:
        raise OperationalEvidenceError("thread/read model differs from the per-turn request")
    if requested_effort is not None and requested_effort != observed_effort:
        raise OperationalEvidenceError(
            "thread/read reasoning effort differs from the per-turn request"
        )

    source_files = [
        value
        for value in (
            _source_record(settings_path, settings_sha256, "settings source"),
            _source_record(rules_path, rules_sha256, "rules source"),
        )
        if value is not None
    ]
    hook_events = _event_records(server_messages, thread_id, turn_id)
    enabled_hooks = [hook for hook in hooks if hook.get("enabled") is True]
    hook_trust_ok = all(
        hook.get("trustStatus") in {"managed", "trusted"} for hook in enabled_hooks
    )
    started_runs = {
        event["run_id"]: event
        for event in hook_events
        if event["method"] == "hook/started"
    }
    completed_runs = {
        event["run_id"]: event
        for event in hook_events
        if event["method"] == "hook/completed"
    }
    successful_runs = [
        event
        for run_id, event in completed_runs.items()
        if event.get("status") == "completed"
        and event.get("identity_observed") is True
        and run_id in started_runs
        and started_runs[run_id].get("identity_observed") is True
    ]
    unsuccessful_runs = [
        event
        for event in completed_runs.values()
        if event.get("status") != "completed"
    ]
    unfinished_runs = set(started_runs) - set(completed_runs)
    started_hook_runs = [
        event
        for hook in enabled_hooks
        for event in started_runs.values()
        if event.get("eventName") == hook.get("eventName")
        and event.get("sourcePath") == hook.get("sourcePath")
    ]
    unobserved_enabled_hooks = [
        {
            "key": hook.get("key"),
            "event_name": hook.get("eventName"),
            "source_path": hook.get("sourcePath"),
            "status": "not_observed_this_turn",
        }
        for hook in enabled_hooks
        if not any(
            event.get("eventName") == hook.get("eventName")
            and event.get("sourcePath") == hook.get("sourcePath")
            for event in started_hook_runs
        )
    ]
    hook_state = "none_configured" if not hooks else "configured"
    hook_gate_ok = (
        not errors
        and not warnings
        and not unsuccessful_runs
        and not unfinished_runs
        and not any(event.get("identity_observed") is not True for event in hook_events)
        and (not hooks or hook_trust_ok)
    )
    hook_blockers: list[str] = []
    if errors:
        hook_blockers.append("hooks_list_errors")
    if warnings:
        hook_blockers.append("hooks_list_warnings")
    if hooks and not hook_trust_ok:
        hook_blockers.append("configured_hook_not_trusted")
    if unsuccessful_runs:
        hook_blockers.append("hook_execution_failed_or_blocked")
    if unfinished_runs:
        hook_blockers.append("hook_execution_unfinished")
    if hooks and any(event.get("identity_observed") is not True for event in hook_events):
        hook_blockers.append("hook_execution_identity_unobserved")
    evidence: dict[str, Any] = {
        "schema_version": "1",
        "record_kind": "codex_operational_evidence",
        "provider": "openai-codex",
        "adapter": "codex-app-server-stdio",
        "cwd": cwd,
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "custody": {
            "raw_events_sha256": _sha256(raw_server_events),
            "request_sha256": _sha256(raw_client_events),
            "argv_sha256": _sha256(argv),
            "stderr_sha256": _sha256(stderr),
        },
        "config_read": {
            "request_sha256": _sha256(config_line),
            "response_sha256": _sha256(config_response_line),
            "params": config_request["params"],
            "config_sha256": _sha256(_canonical(config)),
            "layers_sha256": _sha256(_canonical({"layers": layers})),
            "origins_sha256": _sha256(_canonical({"origins": origins})),
            "effective_settings": {
                "model": config.get("model"),
                "model_reasoning_effort": config.get("model_reasoning_effort"),
                "model_provider": config.get("model_provider"),
                "approval_policy": config.get("approval_policy"),
                "sandbox_mode": config.get("sandbox_mode"),
            },
            "sources": _provider_config_sources(config_result, read_source_bytes=_validate),
        },
        "hooks_list": {
            "request_sha256": _sha256(hooks_line),
            "response_sha256": _sha256(hooks_response_line),
            "params": hooks_request["params"],
            "hooks": [_hook_record(hook) for hook in hooks],
            "errors": errors,
            "warnings": warnings,
        },
        "thread_read": {
            "request_sha256": _sha256(thread_line),
            "response_sha256": _sha256(thread_response_line),
            "params": thread_request["params"],
            "model": observed_model,
            "reasoning_effort": observed_effort,
            "model_provider": thread.get("modelProvider"),
        },
        "hook_events": hook_events,
        "source_files": source_files,
        "observational_qualification": {
            "status": "qualified" if hook_gate_ok else "blocked",
            "model_source": "provider_thread_read_after_turn",
            "reasoning_effort_source": "provider_thread_read_after_turn",
            "effective_settings_observed": True,
            "hooks_observed": True,
            "hook_state": hook_state,
            "hook_trust_observed": hook_trust_ok if hooks else False,
            "hook_events_observed": bool(hook_events),
            "successful_hook_completions": len(successful_runs),
            "unobserved_enabled_hooks": unobserved_enabled_hooks,
            "blockers": hook_blockers,
            "backend_identity_attested": False,
            "limitations": [
                "provider-reported model and reasoning effort are observational and do not "
                "attest backend execution identity",
                "silent provider routing is not excluded",
            ],
        },
        # This is the qualification result for the approved operational-observation basis.  It
        # does not claim that the remote backend executed the requested model.
        "promotion_eligible": hook_gate_ok,
    }
    evidence["content_sha256"] = _sha256(_canonical(evidence, omit_digest=True))
    if not _validate:
        return evidence
    return validate_operational_evidence(
        evidence,
        receipt=None,
        raw_events=raw_server_events,
        request=raw_client_events,
        argv=argv,
        stderr=stderr,
    )


def validate_operational_evidence(
    value: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any] | None,
    raw_events: bytes,
    request: bytes,
    argv: bytes,
    stderr: bytes,
) -> dict[str, Any]:
    """Validate evidence against immutable raw custody and an optional provider receipt."""

    if not isinstance(value, Mapping):
        raise OperationalEvidenceError("operational evidence must be an object")
    candidate = dict(value)
    digest = candidate.get("content_sha256")
    if _sha(digest, "content_sha256") != _sha256(_canonical(candidate, omit_digest=True)):
        raise OperationalEvidenceError("operational evidence content digest does not match")
    if candidate.get("record_kind") != "codex_operational_evidence":
        raise OperationalEvidenceError("operational evidence record kind is unsupported")
    custody = candidate.get("custody")
    if not isinstance(custody, Mapping):
        raise OperationalEvidenceError("operational evidence has no custody digests")
    expected = {
        "raw_events_sha256": raw_events,
        "request_sha256": request,
        "argv_sha256": argv,
        "stderr_sha256": stderr,
    }
    for field, raw in expected.items():
        if custody.get(field) != _sha256(raw):
            raise OperationalEvidenceError(f"operational evidence {field} differs from custody")

    # Re-derive every provider observation from the immutable protocol lines.  This prevents a
    # copied record from becoming eligible by changing only promotion_eligible and rehashing it.
    try:
        derived = build_operational_evidence(
            raw_client_events=request,
            raw_server_events=raw_events,
            cwd=str(candidate["cwd"]),
            session_id=str(candidate["session_id"]),
            thread_id=str(candidate["thread_id"]),
            turn_id=str(candidate["turn_id"]),
            argv=argv,
            stderr=stderr,
            _validate=False,
        )
    except (KeyError, TypeError, OperationalEvidenceError) as exc:
        raise OperationalEvidenceError(
            f"operational evidence raw projection is invalid: {exc}"
        ) from exc
    def projection(value: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            key: item
            for key, item in value.items()
            if key not in {"content_sha256", "source_files"}
        }
        config_read = result.get("config_read")
        if isinstance(config_read, Mapping):
            config_copy = dict(config_read)
            sources = config_copy.get("sources")
            if isinstance(sources, list):
                config_copy["sources"] = [
                    {
                        key: item
                        for key, item in source.items()
                        if key != "source_bytes"
                    }
                    if isinstance(source, Mapping)
                    else source
                    for source in sources
                ]
            result["config_read"] = config_copy
        return result

    candidate_projection = projection(candidate)
    derived_projection = projection(derived)
    if candidate_projection != derived_projection:
        raise OperationalEvidenceError(
            "operational evidence observations do not match the preserved raw protocol custody"
        )
    source_files = candidate.get("source_files")
    if not isinstance(source_files, list):
        raise OperationalEvidenceError("operational evidence source_files must be an array")
    observed_sources: set[str] = set()
    for source in source_files:
        if not isinstance(source, Mapping):
            raise OperationalEvidenceError("operational evidence source file is malformed")
        path = source.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise OperationalEvidenceError("operational evidence source path is invalid")
        observed_sources.add(_sha(source.get("sha256"), "operational source sha256"))

    if receipt is not None:
        for field, receipt_field in (
            ("raw_events_sha256", "raw_event_sha256"),
            ("request_sha256", "request_sha256"),
        ):
            if receipt.get(receipt_field) != custody.get(field):
                raise OperationalEvidenceError(f"operational evidence {field} differs from receipt")
        for field, evidence_field in (
            ("cwd", "cwd"),
            ("session_id", "session_id"),
            ("thread_id", "thread_id"),
            ("turn_id", "turn_id"),
        ):
            if receipt.get(field) != candidate.get(evidence_field):
                raise OperationalEvidenceError(f"operational evidence {field} differs from receipt")
        launch = receipt.get("launch")
        if not isinstance(launch, Mapping):
            raise OperationalEvidenceError("receipt has no launch source digests")
        for field in ("settings_sha256", "rules_sha256"):
            expected_source = launch.get(field)
            if expected_source is not None and expected_source not in observed_sources:
                raise OperationalEvidenceError(
                    f"operational evidence does not bind receipt {field} bytes"
                )
    qualification = candidate.get("observational_qualification")
    if (
        not isinstance(qualification, Mapping)
        or qualification.get("backend_identity_attested") is not False
    ):
        raise OperationalEvidenceError(
            "operational evidence cannot claim backend identity attestation"
        )
    return candidate


def write_operational_evidence(
    value: Mapping[str, Any],
    path: Path,
    *,
    raw_events: bytes | None = None,
    request: bytes | None = None,
    argv: bytes | None = None,
    stderr: bytes | None = None,
) -> Path:
    """Write canonical create-once evidence to a caller-owned private run directory."""

    if any(raw is not None for raw in (raw_events, request, argv, stderr)):
        if not all(raw is not None for raw in (raw_events, request, argv, stderr)):
            raise OperationalEvidenceError(
                "all raw custody bytes are required when validating a write"
            )
        validate_operational_evidence(
            value,
            receipt=None,
            raw_events=raw_events or b"",
            request=request or b"",
            argv=argv or b"",
            stderr=stderr or b"",
        )
    else:
        candidate = dict(value)
        if _sha(candidate.get("content_sha256"), "content_sha256") != _sha256(
            _canonical(candidate, omit_digest=True)
        ):
            raise OperationalEvidenceError("operational evidence content digest does not match")
        qualification = candidate.get("observational_qualification")
        if not isinstance(qualification, Mapping) or qualification.get(
            "backend_identity_attested"
        ) is not False:
            raise OperationalEvidenceError(
                "operational evidence cannot claim backend identity attestation"
            )
    target = path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise OperationalEvidenceError(f"refusing to overwrite operational evidence: {target}")
    target.write_bytes(_canonical(value))
    return target


__all__ = [
    "OperationalEvidenceError",
    "build_operational_evidence",
    "validate_operational_evidence",
    "write_operational_evidence",
]
