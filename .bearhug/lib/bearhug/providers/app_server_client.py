"""Run one Codex App Server turn and preserve both sides of the JSONL exchange.

This is a local execution transport, not campaign scheduling. It creates no worktree and makes no
promotion decision. The caller supplies an already selected cwd and sandbox; Bear Hug writes only
raw custody files and a privacy-bounded observation under its own ``runs/`` tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from bearhug.paths import PROVIDER_OBSERVATIONS_DIR, assert_writable
from bearhug.providers.atomic_json import write_atomic_canonical_json
from bearhug.providers.codex_app_server import (
    CodexAppServerContract,
    CodexAppServerNormalization,
    build_app_server_command,
    build_initialize_messages,
    build_turn_start_message,
    normalize_app_server_jsonl,
)
from bearhug.providers.codex_goal import (
    CodexGoalNormalization,
    build_codex_goal_work_observation,
    normalize_thread_goal_get_response,
)
from bearhug.providers.codex_user_input import (
    EXACT_PROTOCOL_IDENTITY,
    CodexUserInputRequestEvidence,
    build_raw_user_input_response,
    validate_raw_user_input_request,
    validate_raw_user_input_response,
)
from bearhug.providers.codex_user_input import (
    PROVIDER_VERSION as USER_INPUT_PROVIDER_VERSION,
)
from bearhug.providers.codex_user_input import (
    REQUEST_METHOD as USER_INPUT_REQUEST_METHOD,
)
from bearhug.providers.failure_receipt import (
    build_provider_failure_receipt,
    observed_app_server_identity,
    write_provider_failure_receipt,
)
from bearhug.providers.observation import (
    observation_from_receipt,
)
from bearhug.providers.operational_evidence import (
    OperationalEvidenceError,
    build_operational_evidence,
    validate_operational_evidence,
)
from bearhug.providers.receipt import (
    LaunchRepository,
    argv_sha256,
    capture_close_repository,
    capture_launch_repository,
    receipt_from_app_server,
    write_provider_receipt,
)
from bearhug.providers.runtime_attestation import (
    build_codex_runtime_attestation,
    write_runtime_attestation,
)


class AppServerClientError(RuntimeError):
    """The local protocol exchange failed, timed out, or requested unexpected authority."""

    def __init__(
        self,
        message: str,
        *,
        exchange: AppServerExchange | None = None,
        reason_code: str | None = None,
        receipt_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.exchange = exchange
        self.reason_code = reason_code or (
            "timeout" if "timed out" in message.lower() else "protocol_error"
        )
        self.receipt_path = receipt_path


class _GoalCustodyError(ValueError):
    """The typed goal exchange disagrees with its raw request/response custody."""


@dataclass(frozen=True, slots=True)
class AppServerExchange:
    server_events: bytes
    client_events: bytes
    stderr: bytes
    exit_code: int
    goal_request_id: str | None = None
    goal_response: dict[str, Any] | None = None
    goal_thread_id: str | None = None
    # Test exchanges may supply a prebuilt private evidence record. Native exchanges build it
    # from the post-turn API responses retained in the two raw streams.
    operational_evidence: bytes | dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AppServerRun:
    normalization: CodexAppServerNormalization
    goal_normalization: CodexGoalNormalization
    receipt: dict[str, Any]
    observation: dict[str, Any]
    work_observation: dict[str, Any]
    server_events_path: Path
    client_events_path: Path
    argv_path: Path
    stderr_path: Path
    receipt_path: Path
    work_observation_path: Path
    runtime_attestation: dict[str, Any] | None = None
    runtime_attestation_path: Path | None = None
    operational_evidence: dict[str, Any] | None = None
    operational_evidence_path: Path | None = None


UserInputAnswerHandler = Callable[
    [bytes, CodexUserInputRequestEvidence], Mapping[str, Sequence[str]]
]


def _jsonl(message: dict[str, Any]) -> bytes:
    return json.dumps(message, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _goal_request_id() -> str:
    """Return a request id outside Bear Hug's fixed handshake-id namespace."""

    return f"bearhug:thread-goal:get:{uuid.uuid4().hex}"


def _goal_request(thread_id: str, request_id: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": "thread/goal/get",
        "params": {"threadId": thread_id},
    }


def _atomic_bytes(path: Path, value: bytes) -> Path:
    target = assert_writable(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(value)
    os.replace(temporary, target)
    return target


def _atomic_json(path: Path, value: dict[str, Any]) -> Path:
    # Custody re-derives these bytes with ``ensure_ascii=False, allow_nan=False`` (see
    # ``custody._operational_evidence_file``) and rejects anything else as non-canonical, so the
    # writer must produce exactly that encoding rather than a second, looser one.
    return write_atomic_canonical_json(path, value)


def _closed_custody_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _GoalCustodyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _custody_messages(raw: bytes, stream_name: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for ordinal, line in enumerate(raw.splitlines(), start=1):
        try:
            message = json.loads(line, object_pairs_hook=_closed_custody_object)
        except ValueError as exc:
            raise _GoalCustodyError(
                f"{stream_name} custody has invalid JSON at line {ordinal}: {exc}"
            ) from exc
        if not isinstance(message, dict):
            raise _GoalCustodyError(
                f"{stream_name} custody line {ordinal} is not a JSON object"
            )
        messages.append(message)
    return messages


def _goal_result_from_custody(
    exchange: AppServerExchange, *, expected_thread_id: str
) -> dict[str, Any]:
    """Prove the goal result belongs to the exact post-turn request in raw custody."""

    request_id = exchange.goal_request_id
    if not isinstance(request_id, str) or not request_id.startswith("bearhug:thread-goal:get:"):
        raise _GoalCustodyError("exchange has no collision-safe thread/goal/get request id")
    suffix = request_id.removeprefix("bearhug:thread-goal:get:")
    try:
        parsed_request_id = uuid.UUID(hex=suffix)
    except ValueError as exc:
        raise _GoalCustodyError(
            "exchange has no collision-safe thread/goal/get request id"
        ) from exc
    if parsed_request_id.hex != suffix:
        raise _GoalCustodyError("thread/goal/get request id is not canonical UUID hex")
    if exchange.goal_thread_id != expected_thread_id:
        raise _GoalCustodyError("thread/goal/get request thread does not match turn custody")

    client_messages = _custody_messages(exchange.client_events, "client")
    request_ids = [message["id"] for message in client_messages if "id" in message]
    if len(request_ids) != len({(type(value).__name__, str(value)) for value in request_ids}):
        raise _GoalCustodyError("client custody contains duplicate request ids")
    requests = [
        message
        for message in client_messages
        if message.get("method") == "thread/goal/get" and message.get("id") == request_id
    ]
    if len(requests) != 1:
        raise _GoalCustodyError(
            "client custody must contain exactly one thread/goal/get request"
        )
    if requests[0].get("params") != {"threadId": expected_thread_id}:
        raise _GoalCustodyError("thread/goal/get request params do not match turn custody")

    server_messages = _custody_messages(exchange.server_events, "server")
    responses = [message for message in server_messages if message.get("id") == request_id]
    if len(responses) != 1:
        raise _GoalCustodyError(
            "server custody must contain exactly one thread/goal/get response"
        )
    response = responses[0]
    completed_indices = [
        index
        for index, message in enumerate(server_messages)
        if message.get("method") == "turn/completed"
    ]
    response_index = server_messages.index(response)
    if len(completed_indices) != 1 or response_index <= completed_indices[0]:
        raise _GoalCustodyError(
            "thread/goal/get response was not observed after turn completion"
        )
    if exchange.goal_response != response:
        raise _GoalCustodyError("typed goal response does not match raw server custody")
    if "error" in response:
        raise _GoalCustodyError(f"thread/goal/get failed: {response['error']!r}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise _GoalCustodyError("thread/goal/get returned no result object")
    return result


def _read_message(
    stream: BinaryIO,
    selector: selectors.BaseSelector,
    *,
    deadline: float,
) -> tuple[dict[str, Any], bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not selector.select(remaining):
        raise AppServerClientError(
            "Codex App Server timed out waiting for a protocol message",
            reason_code="timeout",
        )
    line = stream.readline()
    if not line:
        raise AppServerClientError("Codex App Server closed stdout before turn completion")
    try:
        message = json.loads(line)
    except ValueError as exc:
        raise AppServerClientError(f"Codex App Server emitted invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise AppServerClientError("Codex App Server emitted a non-object JSON message")
    return message, line


def _subprocess_exchange(
    command: tuple[str, ...],
    contract: CodexAppServerContract,
    prompt: str,
    timeout_s: float,
    *,
    user_input_handler: UserInputAnswerHandler | None = None,
    collect_operational_evidence: bool = False,
    output_schema: dict[str, Any] | None = None,
) -> AppServerExchange:
    """Drive the stable stdio handshake. Unexpected approval requests fail closed."""

    deadline = time.monotonic() + timeout_s
    client = bytearray()
    server = bytearray()
    goal_request_id: str | None = None
    goal_response: dict[str, Any] | None = None
    goal_thread_id: str | None = None
    with tempfile.TemporaryFile() as stderr_file:
        from bearhug.processes import observe_spawn, provider_environment, terminate_group

        environment = provider_environment("codex")
        if contract.codex_home is not None:
            environment["CODEX_HOME"] = contract.codex_home
        proc = subprocess.Popen(  # noqa: S603 - argv is a closed tuple, never a shell command
            command,
            cwd=contract.cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            bufsize=0,
            start_new_session=True,
        )
        observe_spawn(proc)
        if proc.stdin is None or proc.stdout is None:
            terminate_group(proc)
            raise AppServerClientError("Codex App Server did not expose stdio pipes")
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)

        def send(message: dict[str, Any]) -> None:
            raw = _jsonl(message)
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
            except OSError as exc:
                raise AppServerClientError(f"cannot write Codex App Server request: {exc}") from exc
            client.extend(raw)

        def send_raw(raw: bytes) -> None:
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
            except OSError as exc:
                raise AppServerClientError(
                    f"cannot write Codex App Server response: {exc}"
                ) from exc
            client.extend(raw)

        active_thread_id: str | None = None
        active_turn_id: str | None = None

        def receive_until(predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
            while True:
                message, raw = _read_message(proc.stdout, selector, deadline=deadline)
                server.extend(raw)
                method = message.get("method")
                if method == USER_INPUT_REQUEST_METHOD and user_input_handler is not None:
                    if contract.adapter_version != USER_INPUT_PROVIDER_VERSION:
                        raise AppServerClientError(
                            "request_user_input handling is not authorized for adapter version "
                            f"{contract.adapter_version!r}"
                        )
                    params = message.get("params")
                    item_id = params.get("itemId") if isinstance(params, dict) else None
                    if not isinstance(active_thread_id, str) or not isinstance(active_turn_id, str):
                        raise AppServerClientError(
                            "request_user_input arrived before thread/turn identity was established"
                        )
                    if not isinstance(item_id, str):
                        raise AppServerClientError("request_user_input has no item identity")
                    try:
                        request = validate_raw_user_input_request(
                            raw,
                            expected_request_id=message.get("id"),
                            expected_thread_id=active_thread_id,
                            expected_turn_id=active_turn_id,
                            expected_item_id=item_id,
                            protocol=EXACT_PROTOCOL_IDENTITY,
                        )
                        answers = user_input_handler(raw, request)
                        response_raw = build_raw_user_input_response(
                            request,
                            answers,
                            protocol=EXACT_PROTOCOL_IDENTITY,
                        )
                        validate_raw_user_input_response(
                            response_raw,
                            request=request,
                            protocol=EXACT_PROTOCOL_IDENTITY,
                        )
                    except (TypeError, ValueError) as exc:
                        raise AppServerClientError(
                            f"request_user_input handler failed closed: {exc}"
                        ) from exc
                    send_raw(response_raw)
                    continue
                if isinstance(method, str) and (
                    method.endswith("/requestApproval")
                    or method
                    in {
                        USER_INPUT_REQUEST_METHOD,
                        "tool/requestUserInput",
                        "mcpServer/elicitation/request",
                        "item/tool/call",
                    }
                ):
                    raise AppServerClientError(
                        f"unexpected interactive server request {method!r}; "
                        "no authority was granted"
                    )
                if predicate(message):
                    return message

        failure: AppServerClientError | None = None
        try:
            initialize, initialized, thread_start = build_initialize_messages(contract)
            send(initialize)
            receive_until(lambda message: message.get("id") == 0)
            send(initialized)
            send(thread_start)
            thread_response = receive_until(lambda message: message.get("id") == 1)
            result = thread_response.get("result")
            thread = result.get("thread") if isinstance(result, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise AppServerClientError("thread/start returned no thread id")
            active_thread_id = thread_id
            goal_thread_id = thread_id
            send(
                build_turn_start_message(
                    thread_id,
                    prompt,
                    model=contract.requested_model,
                    reasoning_effort=contract.requested_reasoning_effort,
                    output_schema=output_schema,
                    writable_roots=contract.writable_roots,
                )
            )
            turn_response = receive_until(lambda message: message.get("id") == 2)
            turn_result = turn_response.get("result")
            turn = turn_result.get("turn") if isinstance(turn_result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise AppServerClientError("turn/start returned no turn id")
            active_turn_id = turn_id
            receive_until(lambda message: message.get("method") == "turn/completed")
            if collect_operational_evidence:
                for method, params in (
                    (
                        "config/read",
                        {"cwd": contract.cwd, "includeLayers": True},
                    ),
                    ("hooks/list", {"cwds": [contract.cwd]}),
                    (
                        "thread/read",
                        {"threadId": thread_id, "includeTurns": False},
                    ),
                ):
                    request_id = (
                        f"bearhug:operational:{method.replace('/', '-')}:{uuid.uuid4().hex}"
                    )
                    send({"id": request_id, "method": method, "params": params})
                    response = receive_until(
                        lambda message, request_id=request_id: message.get("id") == request_id
                    )
                    if "error" in response or not isinstance(response.get("result"), dict):
                        raise AppServerClientError(
                            f"{method} returned no successful result: {response!r}",
                            reason_code="operational_evidence_error",
                        )
            goal_request_id = _goal_request_id()
            send(_goal_request(thread_id, goal_request_id))
            goal_response = receive_until(lambda message: message.get("id") == goal_request_id)
            if "error" in goal_response:
                raise AppServerClientError(
                    f"thread/goal/get failed: {goal_response['error']!r}"
                )
            if not isinstance(goal_response.get("result"), dict):
                raise AppServerClientError("thread/goal/get returned no result object")
        except AppServerClientError as exc:
            failure = exc
        finally:
            selector.close()
            with suppress(OSError):
                proc.stdin.close()
            remaining = max(0.0, min(5.0, deadline - time.monotonic()))
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                terminate_group(proc)
            # A completed app-server leader is not proof that its tools exited. Terminate
            # its remaining group before draining custody or controller candidate finalization.
            terminate_group(proc)
            trailing = proc.stdout.read()
            if trailing:
                server.extend(trailing)
        stderr_file.seek(0)
        stderr = stderr_file.read()
        result = AppServerExchange(
            server_events=bytes(server),
            client_events=bytes(client),
            stderr=stderr,
            exit_code=proc.returncode,
            goal_request_id=goal_request_id,
            goal_response=goal_response,
            goal_thread_id=goal_thread_id,
        )
        if failure is not None:
            raise AppServerClientError(
                str(failure), exchange=result, reason_code=failure.reason_code
            ) from failure
        return result


def _failure_receipt(
    *,
    run_root: Path,
    contract: CodexAppServerContract,
    command: tuple[str, ...],
    launch_repository: LaunchRepository,
    observed_at: datetime,
    role: str | None,
    required_capabilities: tuple[str, ...],
    settings_sha256: str | None,
    rules_sha256: str | None,
    stage: str,
    reason_code: str,
    detail: str,
    terminal_state: str,
    exit_code: int | None,
    timeout_seconds: float | None,
    server_path: Path | None,
    client_path: Path | None,
    stderr_path: Path | None,
    server_events: bytes,
) -> Path:
    session_id, thread_id = observed_app_server_identity(server_events)
    receipt = build_provider_failure_receipt(
        provider="openai-codex",
        adapter="codex-app-server-stdio",
        adapter_version=contract.adapter_version,
        observed_at=observed_at,
        cwd=contract.cwd,
        role=role,
        required_capabilities=required_capabilities,
        stage=stage,
        reason_code=reason_code,
        detail=detail,
        terminal_state=terminal_state,
        exit_code=exit_code,
        timeout_seconds=timeout_seconds,
        session_id=session_id,
        thread_id=thread_id,
        raw_event_count=None,
        run_root=run_root,
        request_path=client_path,
        raw_events_path=server_path,
        stderr_path=stderr_path,
        launch_repository=launch_repository,
        command_sha256=argv_sha256(command),
        prompt_sha256=contract.prompt_sha256,
        settings_sha256=settings_sha256,
        rules_sha256=rules_sha256,
        sandbox=contract.sandbox,
        approval_policy=contract.approval_policy,
    )
    return write_provider_failure_receipt(receipt, run_root / "failure-receipt.json")


def run_codex_app_server(
    prompt: str,
    contract: CodexAppServerContract,
    *,
    output_dir: Path | str = PROVIDER_OBSERVATIONS_DIR,
    executable: str = "codex",
    timeout_s: float = 900.0,
    exchange: Callable[
        [tuple[str, ...], CodexAppServerContract, str, float], AppServerExchange
    ] = _subprocess_exchange,
    now: datetime | None = None,
    role: str | None = None,
    required_capabilities: tuple[str, ...] = (),
    settings_sha256: str | None = None,
    rules_sha256: str | None = None,
    user_input_handler: UserInputAnswerHandler | None = None,
    native_materialization_sha256: str | None = None,
    source_policy_sha256: str | None = None,
    installed_manifest_sha256: str | None = None,
    install_nonce: str | None = None,
    expected_runtime_events: tuple[str, ...] | None = None,
    collect_operational_evidence: bool = False,
    settings_path: Path | str | None = None,
    rules_path: Path | str | None = None,
    output_schema: dict[str, Any] | None = None,
    candidate_finalizer: Callable[[], None] | None = None,
) -> AppServerRun:
    """Execute one turn and write the canonical receipt after all custody is durable."""

    if not prompt:
        raise ValueError("prompt must not be empty")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be greater than zero")
    prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()
    if prompt_digest != contract.prompt_sha256:
        raise ValueError("prompt does not match the contract's prompt_sha256")
    runtime_context = (
        native_materialization_sha256,
        source_policy_sha256,
        installed_manifest_sha256,
        install_nonce,
        expected_runtime_events,
    )
    if any(value is not None for value in runtime_context) and not all(
        value is not None for value in runtime_context
    ):
        raise ValueError(
            "runtime attestation requires materialization, policy, installed manifest, nonce, "
            "and expected events"
        )
    command = build_app_server_command(contract)
    command = (executable, *command[1:])
    launch_repository = capture_launch_repository(contract.cwd)
    observed_at = now or datetime.now(UTC)
    stamp = observed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"{stamp}-codex-{prompt_digest[:12]}"
    root = assert_writable(Path(output_dir))
    root.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix=prefix + "-", dir=root))
    argv_path = _atomic_bytes(
        run_root / "argv.json",
        json.dumps(list(command), ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )
    try:
        if exchange is _subprocess_exchange:
            source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            with tempfile.TemporaryDirectory(prefix="capsule-home-", dir=run_root) as home_name:
                capsule_home = Path(home_name)
                capsule_home.chmod(0o700)
                auth = source_home / "auth.json"
                try:
                    auth_descriptor = os.open(
                        auth,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                    )
                except FileNotFoundError:
                    auth_descriptor = None
                if auth_descriptor is not None:
                    try:
                        auth_stat = os.fstat(auth_descriptor)
                        if (
                            not stat.S_ISREG(auth_stat.st_mode)
                            or auth_stat.st_size > 2 * 1024 * 1024
                        ):
                            raise AppServerClientError("Codex authentication file is unbounded")
                        auth_bytes = os.read(auth_descriptor, auth_stat.st_size + 1)
                        if len(auth_bytes) != auth_stat.st_size:
                            raise AppServerClientError("Codex authentication changed while read")
                    finally:
                        os.close(auth_descriptor)
                    copied_auth = capsule_home / "auth.json"
                    copied_auth.write_bytes(auth_bytes)
                    copied_auth.chmod(0o600)
                (capsule_home / "config.toml").write_text("", encoding="utf-8")
                execution_contract = replace(contract, codex_home=str(capsule_home))
                result = _subprocess_exchange(
                    command,
                    execution_contract,
                    prompt,
                    timeout_s,
                    user_input_handler=user_input_handler,
                    collect_operational_evidence=collect_operational_evidence,
                    output_schema=output_schema,
                )
        else:
            if user_input_handler is not None:
                raise ValueError(
                    "user_input_handler requires the native App Server exchange"
                )
            result = exchange(command, contract, prompt, timeout_s)
    except AppServerClientError as exc:
        result = exc.exchange
        server_path = (
            _atomic_bytes(run_root / "server.jsonl", result.server_events)
            if result is not None
            else None
        )
        client_path = (
            _atomic_bytes(run_root / "client.jsonl", result.client_events)
            if result is not None
            else None
        )
        stderr_path = (
            _atomic_bytes(run_root / "stderr.txt", result.stderr) if result is not None else None
        )
        reason_code = exc.reason_code
        receipt_path = _failure_receipt(
            run_root=run_root,
            contract=contract,
            command=command,
            launch_repository=launch_repository,
            observed_at=observed_at,
            role=role,
            required_capabilities=required_capabilities,
            settings_sha256=settings_sha256,
            rules_sha256=rules_sha256,
            stage="transport" if reason_code == "timeout" else "protocol",
            reason_code=reason_code,
            detail=str(exc),
            terminal_state="incomplete",
            exit_code=result.exit_code if result is not None else None,
            timeout_seconds=timeout_s if reason_code == "timeout" else None,
            server_path=server_path,
            client_path=client_path,
            stderr_path=stderr_path,
            server_events=result.server_events if result is not None else b"",
        )
        custody = ", ".join(
            str(path) for path in (server_path, client_path, stderr_path) if path is not None
        )
        raise AppServerClientError(
            f"{exc}; failure receipt: {receipt_path}"
            + (f"; custody: {custody}" if custody else ""),
            exchange=result,
            reason_code=reason_code,
            receipt_path=receipt_path,
        ) from exc
    except OSError as exc:
        receipt_path = _failure_receipt(
            run_root=run_root,
            contract=contract,
            command=command,
            launch_repository=launch_repository,
            observed_at=observed_at,
            role=role,
            required_capabilities=required_capabilities,
            settings_sha256=settings_sha256,
            rules_sha256=rules_sha256,
            stage="launch",
            reason_code="launch_os_error",
            detail=f"Codex App Server launch failed: {exc}",
            terminal_state="failed",
            exit_code=None,
            timeout_seconds=None,
            server_path=None,
            client_path=None,
            stderr_path=None,
            server_events=b"",
        )
        raise AppServerClientError(
            f"Codex App Server launch failed: {exc}; failure receipt: {receipt_path}",
            reason_code="launch_os_error",
            receipt_path=receipt_path,
        ) from exc
    server_path = _atomic_bytes(run_root / "server.jsonl", result.server_events)
    client_path = _atomic_bytes(run_root / "client.jsonl", result.client_events)
    stderr_path = _atomic_bytes(run_root / "stderr.txt", result.stderr)
    if result.exit_code not in {0, -15}:
        receipt_path = _failure_receipt(
            run_root=run_root,
            contract=contract,
            command=command,
            launch_repository=launch_repository,
            observed_at=observed_at,
            role=role,
            required_capabilities=required_capabilities,
            settings_sha256=settings_sha256,
            rules_sha256=rules_sha256,
            stage="provider",
            reason_code="nonzero_exit",
            detail=f"Codex App Server exited {result.exit_code}",
            terminal_state="failed",
            exit_code=result.exit_code,
            timeout_seconds=None,
            server_path=server_path,
            client_path=client_path,
            stderr_path=stderr_path,
            server_events=result.server_events,
        )
        raise AppServerClientError(
            f"Codex App Server exited {result.exit_code}: "
            f"{result.stderr.decode(errors='replace')[-500:]}; failure receipt: {receipt_path}; "
            f"custody: {server_path}, {client_path}, {stderr_path}",
            exchange=result,
            reason_code="nonzero_exit",
            receipt_path=receipt_path,
        )
    client_digest = hashlib.sha256(result.client_events).hexdigest()
    runtime_attestation = None
    runtime_attestation_path = None
    operational_evidence = None
    operational_evidence_path = None
    try:
        normalization = normalize_app_server_jsonl(
            result.server_events,
            adapter_version=contract.adapter_version,
            requested_model=contract.requested_model,
            requested_reasoning_effort=contract.requested_reasoning_effort,
            expected_cwd=contract.cwd,
            expected_sandbox=contract.sandbox,
            expected_approval_policy=contract.approval_policy,
        )
        goal_result = _goal_result_from_custody(
            result,
            expected_thread_id=normalization.thread_id,
        )
        goal_normalization = normalize_thread_goal_get_response(
            goal_result,
            expected_thread_id=normalization.thread_id,
        )
        work_observation = build_codex_goal_work_observation(
            goal_normalization,
            adapter="codex-app-server-goal",
            adapter_version=contract.adapter_version,
            session_id=normalization.session_id,
            observed_at=observed_at,
        )
        safe_session = re.sub(r"[^A-Za-z0-9_.-]", "-", normalization.session_id)[:64]
        if collect_operational_evidence:
            if result.operational_evidence is None:
                operational_evidence = build_operational_evidence(
                    raw_client_events=result.client_events,
                    raw_server_events=result.server_events,
                    cwd=contract.cwd,
                    session_id=normalization.session_id,
                    thread_id=normalization.thread_id,
                    turn_id=normalization.turn_id,
                    argv=argv_path.read_bytes(),
                    stderr=result.stderr,
                    settings_path=Path(settings_path) if settings_path is not None else None,
                    settings_sha256=settings_sha256,
                    rules_path=Path(rules_path) if rules_path is not None else None,
                    rules_sha256=rules_sha256,
                )
            else:
                if isinstance(result.operational_evidence, bytes):
                    try:
                        operational_evidence = json.loads(
                            result.operational_evidence.decode("utf-8")
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise OperationalEvidenceError(
                            f"supplied operational evidence is not JSON: {exc}"
                        ) from exc
                elif isinstance(result.operational_evidence, dict):
                    operational_evidence = result.operational_evidence
                else:
                    raise OperationalEvidenceError(
                        "supplied operational evidence has unsupported type"
                    )
                validate_operational_evidence(
                    operational_evidence,
                    receipt=None,
                    raw_events=result.server_events,
                    request=result.client_events,
                    argv=argv_path.read_bytes(),
                    stderr=result.stderr,
                )
            operational_evidence_path = _atomic_json(
                run_root / f"{safe_session}.operational-evidence.json",
                operational_evidence,
            )
        if all(value is not None for value in runtime_context):
            runtime_attestation = build_codex_runtime_attestation(
                normalization,
                raw_server_events=result.server_events,
                input_sha256=client_digest,
                native_materialization_sha256=native_materialization_sha256,
                source_policy_sha256=source_policy_sha256,
                installed_manifest_sha256=installed_manifest_sha256,
                install_nonce=install_nonce,
                expected_events=expected_runtime_events,
                observed_at=observed_at,
            )
            runtime_attestation_path = write_runtime_attestation(
                runtime_attestation,
                run_root / f"{safe_session}.runtime-attestation.json",
            )
    except (OperationalEvidenceError, ValueError) as exc:
        receipt_path = _failure_receipt(
            run_root=run_root,
            contract=contract,
            command=command,
            launch_repository=launch_repository,
            observed_at=observed_at,
            role=role,
            required_capabilities=required_capabilities,
            settings_sha256=settings_sha256,
            rules_sha256=rules_sha256,
            stage="normalization",
            reason_code="normalization_error",
            detail=f"Codex App Server exchange could not be normalized or attested: {exc}",
            terminal_state="incomplete",
            exit_code=result.exit_code,
            timeout_seconds=None,
            server_path=server_path,
            client_path=client_path,
            stderr_path=stderr_path,
            server_events=result.server_events,
        )
        raise AppServerClientError(
            f"Codex App Server exchange could not be normalized or attested: {exc}; "
            f"failure receipt: {receipt_path}; custody: "
            f"{server_path}, {client_path}, {stderr_path}",
            exchange=result,
            reason_code="normalization_error",
            receipt_path=receipt_path,
        ) from exc
    # The native exchange does not return until the App Server process has exited (and escalates
    # from terminate to kill when necessary).  Campaign authors use this point as the first safe
    # opportunity for the controller to touch Git metadata: the provider could write the checkout,
    # but it was never granted the linked worktree's Git directory or common directory.
    if candidate_finalizer is not None and normalization.terminal_state == "completed":
        candidate_finalizer()
    close_repository, candidate = capture_close_repository(
        contract.cwd,
        launch_repository,
        require_unchanged=contract.sandbox == "read-only",
    )
    receipt = receipt_from_app_server(
        normalization,
        cwd=contract.cwd,
        observed_at=observed_at,
        request_sha256=client_digest,
        command_sha256=argv_sha256(command),
        launch_repository=launch_repository,
        close_repository=close_repository,
        candidate=candidate,
        prompt_sha256=contract.prompt_sha256,
        settings_sha256=settings_sha256,
        rules_sha256=rules_sha256,
        sandbox=contract.sandbox,
        approval_policy=contract.approval_policy,
        role=role,
        required_capabilities=required_capabilities,
        runtime_attestation_sha256=(
            runtime_attestation["content_sha256"]
            if runtime_attestation is not None
            else None
        ),
        runtime_attestation_eligible=(
            runtime_attestation["promotion_eligible"]
            if runtime_attestation is not None
            else False
        ),
        operational_evidence_sha256=(
            operational_evidence["content_sha256"]
            if operational_evidence is not None
            else None
        ),
        operational_evidence_eligible=(
            operational_evidence is not None
            and operational_evidence.get("promotion_eligible") is True
        ),
        operational_evidence=operational_evidence,
    )
    observation = observation_from_receipt(receipt)
    safe_session = re.sub(r"[^A-Za-z0-9_.-]", "-", normalization.session_id)[:64]
    work_observation_path = _atomic_json(
        run_root / f"{safe_session}.work-observation.json",
        work_observation,
    )
    receipt_path = write_provider_receipt(receipt, run_root / f"{safe_session}.receipt.json")
    return AppServerRun(
        normalization=normalization,
        goal_normalization=goal_normalization,
        receipt=receipt,
        observation=observation,
        work_observation=work_observation,
        server_events_path=server_path,
        client_events_path=client_path,
        argv_path=argv_path,
        stderr_path=stderr_path,
        receipt_path=receipt_path,
        work_observation_path=work_observation_path,
        runtime_attestation=runtime_attestation,
        runtime_attestation_path=runtime_attestation_path,
        operational_evidence=operational_evidence,
        operational_evidence_path=operational_evidence_path,
    )


__all__ = [
    "AppServerClientError",
    "AppServerExchange",
    "AppServerRun",
    "run_codex_app_server",
]
