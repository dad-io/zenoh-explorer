"""Ordered Memex and Graft evaluation for a provider-neutral user question.

Claude's ``AskUserQuestion`` and Codex's ``item/tool/requestUserInput`` are transport surfaces.
Provider adapters reduce either one to a normalized ``pre-tool-use`` event whose operation is
``ask-user-question`` or ``request-user-input`` and keep the provider request in transient custody.
Only native and extension digests enter the normalized event.  A content-pinned provider reader
then projects those already-bound custody bytes into the canonical semantic question document.

The semantic document and external-tool stdout can contain user or repository prose.  A sealed
provider question reader extracts the document from dispatcher-owned transient custody, and an
injected transient context sink receives output needed by the provider renderer.  Neither appears
in ``EvaluatorOutcome``; normalized evidence contains only digests and closed mechanical facts.

Memex always completes before any Graft process starts.  The four Graft scope queries may run in
parallel after that boundary, matching the measured Claude hook while preserving the one ordering
rule that protects the user's attention.  D02's separately registered Memex and Graft *Stop*
behavior is outside this module and remains unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.hook_dispatcher import Evaluator, EvaluatorContext, EvaluatorOutcome
from bearhug.normalized_hooks import validate_normalized_hook_event


class MemexGraftEvaluatorError(RuntimeError):
    """The ordered evaluator boundary or its transient custody is invalid."""


@dataclass(frozen=True, slots=True)
class ExternalEvaluator:
    """One content-pinned executable and its explicit invocation policy."""

    executable: str
    sha256: str
    timeout_ms: int
    failure_policy: str


@dataclass(frozen=True, slots=True)
class MemexGraftConfig:
    """Dependencies and policy for one ordered user-question evaluation."""

    repository_root: str
    memex: ExternalEvaluator
    graft: ExternalEvaluator
    graft_scopes: tuple[str, ...] = (
        "opcua/",
        "barracuda/internal/",
        "barracuda/pkg/",
        "barracuda/cmd/",
    )


@dataclass(frozen=True, slots=True)
class OrderedEvaluatorAdapter:
    """One dispatcher evaluator with its binding policy order."""

    evaluator_id: str
    order: int
    evaluate: Evaluator


@dataclass(frozen=True, slots=True)
class _Question:
    header: str
    question: str


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class _InvocationFailure(RuntimeError):
    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


MEMEX_EVALUATOR_ID = "memex-pre-question"
GRAFT_EVALUATOR_ID = "graft-pre-question"
MEMEX_ORDER = 10
GRAFT_ORDER = 20
USER_QUESTION_OPERATION = "request-user-input"
USER_QUESTION_OPERATIONS = frozenset({"ask-user-question", USER_QUESTION_OPERATION})
FAILURE_POLICIES = frozenset({"block", "continue", "observe-only"})
QuestionCustodyReader = Callable[[Mapping[str, Any], EvaluatorContext], bytes]
# event id, evaluator id, SHA-256 of the mechanical evidence bytes, raw provider context
ProviderContextSink = Callable[[str, str, str, bytes], None]
CERTIFIED_GRAFT_SCOPES = (
    "opcua/",
    "barracuda/internal/",
    "barracuda/pkg/",
    "barracuda/cmd/",
)

_SEMANTIC_FIELDS = frozenset({"schema_version", "record_kind", "questions"})
_QUESTION_FIELDS = frozenset({"header", "question"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACK = re.compile(r"memex-checked:\s*([0-9]{1,4}(?:\s*,\s*[0-9]{1,4})*)")
_SURFACED = re.compile(r"^  ([0-9]{4})  ")
_IDENTIFIER = re.compile(r"^(?:[A-Za-z0-9]*[a-z0-9][A-Z][A-Za-z0-9]*|[A-Za-z0-9]+_[A-Za-z0-9_]+)$")
_GRAFT_SYMBOL = re.compile(r"^([0-9]+)\. (.+)$")
_GRAFT_LOCATION = re.compile(r"^   ((?:opcua|barracuda)/\S+)$")
_MAX_SEMANTIC_INPUT_BYTES = 64 * 1024
_MAX_QUESTION_BYTES = 8192
_MAX_HEADER_BYTES = 256
_MAX_EXTERNAL_OUTPUT_BYTES = 256 * 1024
_MAX_EXECUTABLE_BYTES = 64 * 1024 * 1024
_MAX_GRAFT_SCOPES = 8
_MAX_PROVIDER_CONTEXT_BYTES = 256 * 1024
_READ_CHUNK_BYTES = 16 * 1024


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise MemexGraftEvaluatorError(
            f"semantic question input is not canonical JSON: {exc}"
        ) from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise MemexGraftEvaluatorError(f"duplicate semantic question key: {key}")
        value[key] = item
    return value


def _bounded_text(value: Any, where: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise MemexGraftEvaluatorError(f"{where} must be bounded non-empty UTF-8 text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MemexGraftEvaluatorError(f"{where} must be valid UTF-8 text") from exc
    if len(encoded) > maximum:
        raise MemexGraftEvaluatorError(f"{where} must be bounded non-empty UTF-8 text")
    if "\x00" in value:
        raise MemexGraftEvaluatorError(f"{where} contains a NUL byte")
    return value


def _parse_semantic_input(raw: bytes) -> tuple[_Question, ...]:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_SEMANTIC_INPUT_BYTES:
        raise MemexGraftEvaluatorError("semantic question custody must be bounded exact bytes")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except MemexGraftEvaluatorError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemexGraftEvaluatorError(
            "semantic question custody is not one UTF-8 JSON value"
        ) from exc
    if not isinstance(value, dict) or set(value) != _SEMANTIC_FIELDS:
        raise MemexGraftEvaluatorError("semantic question input has missing or unknown fields")
    if value["schema_version"] != "1" or value["record_kind"] != "user_question_input":
        raise MemexGraftEvaluatorError("semantic question input has unsupported identity")
    values = value["questions"]
    if not isinstance(values, list) or not 1 <= len(values) <= 4:
        raise MemexGraftEvaluatorError("semantic question input must contain 1..4 questions")
    questions: list[_Question] = []
    for index, item in enumerate(values):
        if not isinstance(item, dict) or set(item) != _QUESTION_FIELDS:
            raise MemexGraftEvaluatorError(
                f"semantic question input questions[{index}] has missing or unknown fields"
            )
        questions.append(
            _Question(
                header=_bounded_text(
                    item["header"], f"questions[{index}].header", _MAX_HEADER_BYTES
                ),
                question=_bounded_text(
                    item["question"], f"questions[{index}].question", _MAX_QUESTION_BYTES
                ),
            )
        )
    if _canonical_json(value) != raw:
        raise MemexGraftEvaluatorError("semantic question custody must use canonical JSON bytes")
    return tuple(questions)


def build_semantic_question_input(questions: tuple[tuple[str, str], ...]) -> bytes:
    """Build the canonical transient document returned by a provider question reader.

    The returned bytes may contain user prose.  They stay inside dispatcher custody and must be
    discarded after dispatch; callers must never place them in an outcome or journal record.
    """

    value = {
        "schema_version": "1",
        "record_kind": "user_question_input",
        "questions": [{"header": header, "question": question} for header, question in questions],
    }
    raw = _canonical_json(value)
    _parse_semantic_input(raw)
    return raw


def _validate_config(config: MemexGraftConfig) -> Path:
    if not isinstance(config, MemexGraftConfig):
        raise MemexGraftEvaluatorError("config has the wrong type")
    root = Path(config.repository_root)
    if not root.is_absolute() or root.as_posix() != config.repository_root or not root.is_dir():
        raise MemexGraftEvaluatorError(
            "repository_root must be an existing canonical absolute path"
        )
    for name, tool in (("memex", config.memex), ("graft", config.graft)):
        if not isinstance(tool, ExternalEvaluator):
            raise MemexGraftEvaluatorError(f"{name} evaluator has the wrong type")
        path = Path(tool.executable)
        if not path.is_absolute() or path.as_posix() != tool.executable:
            raise MemexGraftEvaluatorError(f"{name} executable must be a canonical absolute path")
        if not isinstance(tool.sha256, str) or _SHA256.fullmatch(tool.sha256) is None:
            raise MemexGraftEvaluatorError(f"{name} executable SHA-256 is invalid")
        if type(tool.timeout_ms) is not int or not 1 <= tool.timeout_ms <= 60_000:
            raise MemexGraftEvaluatorError(f"{name} timeout_ms must be in 1..60000 milliseconds")
        if tool.failure_policy not in FAILURE_POLICIES:
            raise MemexGraftEvaluatorError(f"{name} failure_policy is unsupported")
    scopes = config.graft_scopes
    if not isinstance(scopes, tuple) or not 1 <= len(scopes) <= _MAX_GRAFT_SCOPES:
        raise MemexGraftEvaluatorError("graft_scopes must be a bounded non-empty tuple")
    if len(set(scopes)) != len(scopes):
        raise MemexGraftEvaluatorError("graft_scopes contains duplicates")
    for scope in scopes:
        if (
            not isinstance(scope, str)
            or not scope.endswith("/")
            or scope.startswith("/")
            or ".." in scope.split("/")
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", scope)
        ):
            raise MemexGraftEvaluatorError("graft scope is not a safe repository-relative prefix")
        if scope not in CERTIFIED_GRAFT_SCOPES:
            raise MemexGraftEvaluatorError("graft scope is outside the certified live-code scopes")
    return root


def _read_executable(tool: ExternalEvaluator) -> bytes:
    path = Path(tool.executable)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _InvocationFailure("dependency-missing") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not metadata.st_mode & 0o111
            or metadata.st_size > _MAX_EXECUTABLE_BYTES
        ):
            raise _InvocationFailure("dependency-invalid")
        digest = hashlib.sha256()
        content = bytearray()
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            digest.update(chunk)
            content.extend(chunk)
            if len(content) > _MAX_EXECUTABLE_BYTES:
                raise _InvocationFailure("dependency-invalid")
        if digest.hexdigest() != tool.sha256:
            raise _InvocationFailure("dependency-mismatch")
        return bytes(content)
    finally:
        os.close(descriptor)


def _kill(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        with suppress(OSError):
            process.kill()
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=1)


def _invoke(tool: ExternalEvaluator, arguments: tuple[str, ...], root: Path) -> _ProcessResult:
    content = _read_executable(tool)
    with tempfile.TemporaryDirectory(prefix="bearhug-evaluator-") as temporary:
        executable = Path(temporary) / "evaluator"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(executable, flags, 0o700)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
        finally:
            os.close(descriptor)
        try:
            process = subprocess.Popen(
                [executable.as_posix(), *arguments],
                executable=executable.as_posix(),
                cwd=root,
                env={"LANG": "C", "LC_ALL": "C", "PATH": os.defpath},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise _InvocationFailure("execution-error") from exc
        if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen invariant
            _kill(process)
            raise _InvocationFailure("execution-error")
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        streams = {stdout_fd: bytearray(), stderr_fd: bytearray()}
        selector = selectors.DefaultSelector()
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + tool.timeout_ms / 1000
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill(process)
                    raise _InvocationFailure("timeout")
                for key, _mask in selector.select(min(remaining, 0.05)):
                    chunk = os.read(key.fileobj.fileno(), _READ_CHUNK_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    streams[key.fileobj.fileno()].extend(chunk)
                    if sum(len(value) for value in streams.values()) > _MAX_EXTERNAL_OUTPUT_BYTES:
                        _kill(process)
                        raise _InvocationFailure("output-limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill(process)
                raise _InvocationFailure("timeout")
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                _kill(process)
                raise _InvocationFailure("timeout") from exc
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
        stdout = bytes(streams[stdout_fd])
        stderr = bytes(streams[stderr_fd])
        if returncode != 0:
            raise _InvocationFailure("nonzero-exit")
        return _ProcessResult(returncode, stdout, stderr)


def _evidence_bytes(**facts: Any) -> bytes:
    return _canonical_json(facts)


def _failure_outcome(*, evaluator_id: str, state: str, policy: str) -> EvaluatorOutcome:
    decision = "block" if policy == "block" else "continue"
    remediation = ()
    if decision == "block":
        remediation = (
            {
                "code": "external-evaluator-unavailable",
                "message": (
                    f"Required {evaluator_id} evaluation did not complete; retry only after "
                    "the pinned evaluator is available."
                ),
                "paths": [],
            },
        )
    return EvaluatorOutcome(
        decision=decision,
        evidence=_evidence_bytes(state=state, failure_policy=policy),
        remediation=remediation,
    )


def _search_text(questions: tuple[_Question, ...]) -> str:
    fields: list[str] = []
    for item in questions:
        fields.extend(
            (item.question.split("memex-checked:", 1)[0], item.header.split("memex-checked:", 1)[0])
        )
    return " ".join(fields)


def _acknowledged_ids(questions: tuple[_Question, ...]) -> tuple[str, ...]:
    found: set[int] = set()
    for item in questions:
        for field in (item.question, item.header):
            for match in _ACK.finditer(field):
                found.update(int(value.strip()) for value in match.group(1).split(","))
    return tuple(f"{value:04d}" for value in sorted(found))


def _evaluate_memex(
    questions: tuple[_Question, ...],
    root: Path,
    tool: ExternalEvaluator,
    *,
    event_id: str,
    context_sink: ProviderContextSink,
) -> EvaluatorOutcome:
    search = _search_text(questions)
    if not search.strip():
        return EvaluatorOutcome(
            decision="allow",
            evidence=_evidence_bytes(state="empty-query"),
        )
    try:
        completed = _invoke(tool, ("-root", root.as_posix(), "-topic", search), root)
        output = completed.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return _failure_outcome(
            evaluator_id=MEMEX_EVALUATOR_ID,
            state="malformed-output",
            policy=tool.failure_policy,
        )
    except _InvocationFailure as exc:
        return _failure_outcome(
            evaluator_id=MEMEX_EVALUATOR_ID,
            state=exc.state,
            policy=tool.failure_policy,
        )

    surfaced = tuple(
        dict.fromkeys(
            match.group(1)
            for line in output.splitlines()
            if (match := _SURFACED.match(line)) is not None
        )
    )
    acknowledged = frozenset(_acknowledged_ids(questions))
    missing = tuple(identifier for identifier in surfaced if identifier not in acknowledged)
    state = "blocked" if missing else "passed"
    decision = "block" if missing else "allow"
    evidence = _evidence_bytes(
        state=state,
        query_sha256=hashlib.sha256(search.encode("utf-8")).hexdigest(),
        acknowledged_ids=sorted(acknowledged),
        surfaced_ids=list(surfaced),
        missing_ids=list(missing),
        output_sha256=hashlib.sha256(completed.stdout).hexdigest(),
        output_byte_count=len(completed.stdout),
    )
    remediation = ()
    if missing:
        context_text = (
            output.rstrip()
            + f"\nACK PARSE: {len(acknowledged)} memex-checked id(s) recognized in the question."
            + "\nSAM ADJUDICATES — read each surfaced ruling. If none settles the question, "
            + "retry with a memex-checked clause that names every missing id and states what "
            + "each rules."
        )
        context = context_text.encode("utf-8")
        if len(context) > _MAX_PROVIDER_CONTEXT_BYTES:
            return _failure_outcome(
                evaluator_id=MEMEX_EVALUATOR_ID,
                state="context-limit",
                policy=tool.failure_policy,
            )
        try:
            context_sink(
                event_id,
                MEMEX_EVALUATOR_ID,
                hashlib.sha256(evidence).hexdigest(),
                context,
            )
        except Exception:
            return _failure_outcome(
                evaluator_id=MEMEX_EVALUATOR_ID,
                state="context-delivery-error",
                policy=tool.failure_policy,
            )
        remediation = (
            {
                "code": "memex-rulings-unacknowledged",
                "message": (
                    "Read every surfaced Memex ruling; if the question remains open, retry with "
                    "a memex-checked clause that names each missing ruling and states what it "
                    "rules."
                ),
                "paths": [],
            },
        )
    return EvaluatorOutcome(
        decision=decision,
        evidence=evidence,
        remediation=remediation,
    )


def _graft_query(questions: tuple[_Question, ...]) -> str:
    text = " ".join(value for item in questions for value in (item.question, item.header))
    text = _ACK.sub("", text)
    tokens = re.findall(r"[A-Za-z0-9_]+", text)
    unique = list(dict.fromkeys(token for token in tokens if _IDENTIFIER.fullmatch(token)))
    # The captured shell uses ``sort -rn -k1``: descending length, then descending line text.
    unique.sort(key=lambda token: (len(token), token), reverse=True)
    return " ".join(unique[:6])


def _filtered_graft_hits(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _InvocationFailure("malformed-output") from exc
    lines: list[str] = []
    for line in text.splitlines():
        if match := _GRAFT_SYMBOL.match(line):
            lines.append(f"  {match.group(1)}. {match.group(2)}")
        elif match := _GRAFT_LOCATION.match(line):
            lines.append(f"       {match.group(1)}")
        if len(lines) == 8:
            break
    return "\n".join(lines)


def _evaluate_graft(
    questions: tuple[_Question, ...],
    root: Path,
    tool: ExternalEvaluator,
    scopes: tuple[str, ...],
    *,
    event_id: str,
    context_sink: ProviderContextSink,
) -> EvaluatorOutcome:
    query = _graft_query(questions)
    if not query:
        return EvaluatorOutcome(
            decision="continue",
            evidence=_evidence_bytes(state="not-applicable", reason="no-identifier-tokens"),
        )

    def invoke(scope: str) -> tuple[str, _ProcessResult | _InvocationFailure]:
        try:
            return scope, _invoke(tool, ("ask", query, "--in", scope), root)
        except _InvocationFailure as exc:
            return scope, exc

    with ThreadPoolExecutor(max_workers=len(scopes), thread_name_prefix="graft-prep") as executor:
        results = dict(executor.map(invoke, scopes))
    failures = tuple(
        {"scope": scope, "state": results[scope].state}
        for scope in scopes
        if isinstance(results[scope], _InvocationFailure)
    )
    if len(failures) == len(scopes) or (failures and tool.failure_policy == "block"):
        return _failure_outcome(
            evaluator_id=GRAFT_EVALUATOR_ID,
            state="partial-failure" if len(failures) < len(scopes) else failures[0]["state"],
            policy=tool.failure_policy,
        )

    blocks: list[str] = []
    output_digests: list[dict[str, Any]] = []
    try:
        for scope in scopes:
            result = results[scope]
            if isinstance(result, _InvocationFailure):
                continue
            hits = _filtered_graft_hits(result.stdout)
            output_digests.append(
                {
                    "scope": scope,
                    "sha256": hashlib.sha256(result.stdout).hexdigest(),
                    "byte_count": len(result.stdout),
                }
            )
            if hits:
                blocks.append(f"[{scope}]\n{hits}")
    except _InvocationFailure as exc:
        return _failure_outcome(
            evaluator_id=GRAFT_EVALUATOR_ID,
            state=exc.state,
            policy=tool.failure_policy,
        )
    evidence = _evidence_bytes(
        state=("partial-failure" if failures else "observed" if blocks else "no-hits"),
        failure_policy=tool.failure_policy if failures else None,
        failures=list(failures),
        query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
        scopes=list(scopes),
        outputs=output_digests,
    )
    if blocks:
        text = (
            f"[graft] where this code lives, before you ask (query: {query})\n\n"
            + "\n".join(blocks)
            + "\nThese are locations, not claims: open the span before asserting what it does."
        )
        context = text.encode("utf-8")
        if len(context) > _MAX_PROVIDER_CONTEXT_BYTES:
            return _failure_outcome(
                evaluator_id=GRAFT_EVALUATOR_ID,
                state="context-limit",
                policy=tool.failure_policy,
            )
        try:
            context_sink(
                event_id,
                GRAFT_EVALUATOR_ID,
                hashlib.sha256(evidence).hexdigest(),
                context,
            )
        except Exception:
            return _failure_outcome(
                evaluator_id=GRAFT_EVALUATOR_ID,
                state="context-delivery-error",
                policy=tool.failure_policy,
            )
    return EvaluatorOutcome(
        decision="continue",
        evidence=evidence,
    )


def _read_questions(
    event: Mapping[str, Any],
    context: EvaluatorContext,
    question_reader: QuestionCustodyReader,
) -> tuple[_Question, ...]:
    normalized = validate_normalized_hook_event(event)
    if (
        normalized["semantic_event"] != "pre-tool-use"
        or normalized["tool"] is None
        or normalized["tool"]["family"] != "user-interaction"
        or normalized["tool"]["operation"] not in USER_QUESTION_OPERATIONS
    ):
        raise MemexGraftEvaluatorError("event is not a normalized user-question pre-tool event")
    if context.event_id != normalized["event_id"]:
        raise MemexGraftEvaluatorError("evaluator context does not match the normalized event")
    custody = context.custody
    if custody is None:
        raise MemexGraftEvaluatorError("user-question transient custody is unavailable")
    if (
        hashlib.sha256(custody.native_input).hexdigest() != normalized["source"]["input_sha256"]
        or len(custody.native_input) != normalized["source"]["input_byte_count"]
    ):
        raise MemexGraftEvaluatorError("native question custody does not match the event digest")
    extension = normalized["provider_extension"]
    if extension is None:
        if custody.provider_extension is not None:
            raise MemexGraftEvaluatorError("unexpected provider-extension custody")
    elif custody.provider_extension is None or (
        hashlib.sha256(custody.provider_extension).hexdigest() != extension["sha256"]
        or len(custody.provider_extension) != extension["byte_count"]
    ):
        raise MemexGraftEvaluatorError("provider-extension custody does not match the event digest")
    try:
        semantic_question_input = question_reader(normalized, context)
    except Exception as exc:
        raise MemexGraftEvaluatorError("provider question reader failed") from exc
    if not isinstance(semantic_question_input, bytes):
        raise MemexGraftEvaluatorError("semantic question custody must be exact bytes")
    return _parse_semantic_input(semantic_question_input)


def _bounded_tool_timeout(tool: ExternalEvaluator, context: EvaluatorContext) -> ExternalEvaluator:
    remaining_ms = int(max(0.0, context.deadline_monotonic - time.monotonic()) * 1000)
    if remaining_ms < 1:
        raise _InvocationFailure("timeout")
    return ExternalEvaluator(
        executable=tool.executable,
        sha256=tool.sha256,
        timeout_ms=min(tool.timeout_ms, context.timeout_ms, remaining_ms),
        failure_policy=tool.failure_policy,
    )


def make_memex_evaluator(
    *,
    config: MemexGraftConfig,
    question_reader: QuestionCustodyReader,
    context_sink: ProviderContextSink,
) -> Evaluator:
    """Create the first evaluator in the required Memex-before-Graft policy order."""

    root = _validate_config(config)
    if not callable(question_reader) or not callable(context_sink):
        raise MemexGraftEvaluatorError("question_reader and context_sink must be callable")

    def evaluate(event: Mapping[str, Any], context: EvaluatorContext) -> EvaluatorOutcome:
        if context.evaluator_id != MEMEX_EVALUATOR_ID:
            raise MemexGraftEvaluatorError("Memex evaluator context has the wrong evaluator id")
        try:
            questions = _read_questions(event, context, question_reader)
            tool = _bounded_tool_timeout(config.memex, context)
        except (MemexGraftEvaluatorError, _InvocationFailure) as exc:
            state = exc.state if isinstance(exc, _InvocationFailure) else "malformed-input"
            return _failure_outcome(
                evaluator_id=MEMEX_EVALUATOR_ID,
                state=state,
                policy=config.memex.failure_policy,
            )
        return _evaluate_memex(
            questions,
            root,
            tool,
            event_id=context.event_id,
            context_sink=context_sink,
        )

    return evaluate


def make_graft_evaluator(
    *,
    config: MemexGraftConfig,
    question_reader: QuestionCustodyReader,
    context_sink: ProviderContextSink,
) -> Evaluator:
    """Create the second evaluator in the required Memex-before-Graft policy order."""

    root = _validate_config(config)
    if not callable(question_reader) or not callable(context_sink):
        raise MemexGraftEvaluatorError("question_reader and context_sink must be callable")

    def evaluate(event: Mapping[str, Any], context: EvaluatorContext) -> EvaluatorOutcome:
        if context.evaluator_id != GRAFT_EVALUATOR_ID:
            raise MemexGraftEvaluatorError("Graft evaluator context has the wrong evaluator id")
        try:
            questions = _read_questions(event, context, question_reader)
            tool = _bounded_tool_timeout(config.graft, context)
        except (MemexGraftEvaluatorError, _InvocationFailure) as exc:
            state = exc.state if isinstance(exc, _InvocationFailure) else "malformed-input"
            return _failure_outcome(
                evaluator_id=GRAFT_EVALUATOR_ID,
                state=state,
                policy=config.graft.failure_policy,
            )
        return _evaluate_graft(
            questions,
            root,
            tool,
            config.graft_scopes,
            event_id=context.event_id,
            context_sink=context_sink,
        )

    return evaluate


def make_ordered_memex_graft_evaluators(
    *,
    config: MemexGraftConfig,
    question_reader: QuestionCustodyReader,
    context_sink: ProviderContextSink,
) -> tuple[OrderedEvaluatorAdapter, OrderedEvaluatorAdapter]:
    """Build the only supported user-question evaluator order.

    The dispatcher policy must declare these same ordinal values.  Returning one closed tuple keeps
    callers from accidentally representing the legacy concurrent Claude registrations as ordered.
    """

    return (
        OrderedEvaluatorAdapter(
            MEMEX_EVALUATOR_ID,
            MEMEX_ORDER,
            make_memex_evaluator(
                config=config,
                question_reader=question_reader,
                context_sink=context_sink,
            ),
        ),
        OrderedEvaluatorAdapter(
            GRAFT_EVALUATOR_ID,
            GRAFT_ORDER,
            make_graft_evaluator(
                config=config,
                question_reader=question_reader,
                context_sink=context_sink,
            ),
        ),
    )


__all__ = [
    "CERTIFIED_GRAFT_SCOPES",
    "ExternalEvaluator",
    "FAILURE_POLICIES",
    "GRAFT_EVALUATOR_ID",
    "GRAFT_ORDER",
    "MEMEX_EVALUATOR_ID",
    "MEMEX_ORDER",
    "MemexGraftConfig",
    "MemexGraftEvaluatorError",
    "OrderedEvaluatorAdapter",
    "ProviderContextSink",
    "QuestionCustodyReader",
    "USER_QUESTION_OPERATION",
    "USER_QUESTION_OPERATIONS",
    "build_semantic_question_input",
    "make_graft_evaluator",
    "make_memex_evaluator",
    "make_ordered_memex_graft_evaluators",
]
