"""R06 — an in-process conformance harness that proves an evaluator is pure.

Deliberately separate from `hooks/runner.py`. That runs a snapshotted script as a **subprocess**
and observes what it did to a repository. This calls a function **in-process** and observes what it
did to the interpreter, because the failure that matters most here is invisible to a subprocess: an
evaluator that prints. In its own process a print is just stdout; inside the coordinator it becomes
a second Stop decision beside the arbitrated one.

The harness reports. It does **not** apply a failure policy — whether an exception fails open or
closed is D04's ruled table, and a harness that decided it would be a second authority.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from bearhug.paths import REPO_ROOT

EVALUATOR_SCHEMA = REPO_ROOT / "docs" / "schemas" / "evaluator-result.schema.json"

#: Guards against an evaluator invoking another evaluator. Module-level because the prohibition is
#: about re-entrancy across the whole harness, not within one call.
_ACTIVE: list[str] = []

#: Monotonic count of nested entries. The OUTER call is the one that must report the violation —
#: it is the evaluator that did the invoking — but only the INNER call can notice it is nested.
#: A depth check after unwinding cannot work: by then the outer frame has popped back to depth 0.
#: So the inner call increments this, and the outer compares the count across its own lifetime.
_NESTED_ENTRIES: list[str] = []


class Violation(StrEnum):
    """What a non-pure evaluator did. One member per prohibition, plus result-shape failures."""

    WROTE_STDOUT = "wrote_stdout"
    WROTE_STDERR = "wrote_stderr"
    EXITED = "exited"
    #: Its own member rather than a generic write: D05 ruled the COORDINATOR owns the stamp,
    #: so an evaluator stamping breaches an ownership rule as well as writing.
    STAMPED = "stamped"
    WROTE_TELEMETRY = "wrote_telemetry"
    MUTATED_FIXTURE = "mutated_fixture"
    INVOKED_ANOTHER_EVALUATOR = "invoked_another_evaluator"
    RAISED = "raised"
    NO_RESULT = "no_result"
    NON_CONFORMANT_RESULT = "non_conformant_result"


@dataclass(frozen=True, slots=True)
class StopFixture:
    """Constructed Stop state.

    Never copied from a real session: no transcript body may be committed.
    """

    root: Path
    event: dict[str, Any]
    transcript_path: Path
    task_store_path: Path
    board_path: Path
    #: Inside the fixture root, not `~/.claude`. A fixture that could write to the real telemetry
    #: store would make the lab a telemetry producer, which the charter forbids.
    telemetry_dir: Path


@dataclass(slots=True)
class ConformanceReport:
    """What one evaluator call did. Facts only — no verdict about what should happen next."""

    violations: list[Violation] = field(default_factory=list)
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    wrote_paths: tuple[str, ...] = ()
    duration_ms: float = 0.0
    exception_type: str | None = None
    schema_errors: tuple[str, ...] = ()


def build_stop_fixture(root: Path) -> StopFixture:
    """Construct Stop state deterministically.

    Byte-identical across builds: no timestamps, no random identifiers. If it were not, the
    before/after fingerprint would be measuring the previous build rather than this evaluator.
    """
    root = Path(root)
    (root / "internal" / "svc").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "superpowers" / "plans").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "memex" / "decisions").mkdir(parents=True, exist_ok=True)

    (root / "go.mod").write_text("module fixture\n\ngo 1.22\n", encoding="utf-8")
    (root / "internal" / "svc" / "svc.go").write_text(
        "package svc\n\nfunc Add(a, b int) int { return a + b }\n", encoding="utf-8"
    )

    transcript = root / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in (
                {"type": "user", "message": {"role": "user", "content": "change Add"},
                 "timestamp": "2026-08-28T10:00:00Z"},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Edit",
                     "input": {"file_path": "internal/svc/svc.go"}}]},
                 "timestamp": "2026-08-28T10:00:01Z"},
            )
        ) + "\n",
        encoding="utf-8",
    )

    task_store = root / "tasks.json"
    task_store.write_text(
        json.dumps(
            [{"id": 1, "subject": "[P1] change Add", "state": "open",
              "authority": "docs/superpowers/plans/PLAN.md",
              "metadata": {"board_row": "1"}}],
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    board = root / "docs" / "superpowers" / "plans" / "BOARD.md"
    board.write_text(
        "| # | subject | phase | ruling | execution |\n"
        "|---|---|---|---|---|\n"
        "| 1 | change Add | P1 | the fixture row | pending |\n",
        encoding="utf-8",
    )
    (root / "docs" / "superpowers" / "plans" / "PLAN.md").write_text(
        "# Fixture plan\n", encoding="utf-8"
    )

    telemetry = root / ".telemetry"

    event = {
        "hook_event_name": "Stop",
        "session_id": "f1x7u4e5-0000-4000-8000-000000000001",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
        "cwd": str(root),
    }
    return StopFixture(
        root=root, event=event, transcript_path=transcript, task_store_path=task_store,
        board_path=board, telemetry_dir=telemetry,
    )


def _fingerprint(root: Path) -> dict[str, str]:
    """relpath -> content hash. Content, not size or mtime.

    A same-length edit — `pending` to `running`, say — would be invisible to a size-and-mtime
    fingerprint on a filesystem with coarse timestamps, and that is exactly the kind of state
    mutation an evaluator must not perform.
    """
    prints: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file():
            prints[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return prints


def _validate(payload: dict) -> tuple[str, ...]:
    """Validate against the schema with a real validator, or report that we could not.

    No hand-rolled validator: the plan forbids a third, subtly different one, and a checker that
    disagreed with the schema would pass results the coordinator rejects.
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is a dev dependency
        return ("jsonschema is unavailable, so result conformance was NOT checked",)
    schema = json.loads(EVALUATOR_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return tuple(
        f"{'/'.join(str(p) for p in error.path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    )


def run_evaluator(
    evaluator: Callable[[dict[str, Any]], Any], fixture: StopFixture
) -> ConformanceReport:
    """Call one evaluator against one fixture and record every prohibition it broke."""
    report = ConformanceReport()
    if _ACTIVE:
        # This call is itself nested. Flag it here so a nested run is visible even if the inner
        # evaluator is otherwise pure, and tell the outer call by bumping the shared counter.
        report.violations.append(Violation.INVOKED_ANOTHER_EVALUATOR)
        _NESTED_ENTRIES.append(_ACTIVE[-1])

    before = _fingerprint(fixture.root)
    out, err = io.StringIO(), io.StringIO()
    nested_before = len(_NESTED_ENTRIES)
    started = time.perf_counter()

    _ACTIVE.append(getattr(evaluator, "__name__", repr(evaluator)))
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                report.result = evaluator(fixture.event)
            except SystemExit as exc:
                report.violations.append(Violation.EXITED)
                report.exit_code = exc.code if isinstance(exc.code, int) else 1
            except BaseException as exc:  # noqa: BLE001 — the harness must survive anything
                report.violations.append(Violation.RAISED)
                report.exception_type = type(exc).__name__
    finally:
        _ACTIVE.pop()
    # Did anything nest while THIS call was on the stack? Counting entries rather than inspecting
    # depth, because depth is back to its starting value by the time we get here.
    nested_detected = len(_NESTED_ENTRIES) > nested_before
    report.duration_ms = round((time.perf_counter() - started) * 1000, 4)

    report.stdout = out.getvalue()
    report.stderr = err.getvalue()
    if report.stdout:
        report.violations.append(Violation.WROTE_STDOUT)
    if report.stderr:
        report.violations.append(Violation.WROTE_STDERR)

    after = _fingerprint(fixture.root)
    changed = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    )
    if changed:
        report.wrote_paths = tuple(changed)
        if any(name.startswith(".automation-stamps/") for name in changed):
            report.violations.append(Violation.STAMPED)
        telemetry_prefix = (
            fixture.telemetry_dir.relative_to(fixture.root).as_posix() + "/"
            if fixture.telemetry_dir.is_relative_to(fixture.root)
            else None
        )
        if telemetry_prefix and any(name.startswith(telemetry_prefix) for name in changed):
            report.violations.append(Violation.WROTE_TELEMETRY)
        report.violations.append(Violation.MUTATED_FIXTURE)

    if nested_detected and Violation.INVOKED_ANOTHER_EVALUATOR not in report.violations:
        report.violations.append(Violation.INVOKED_ANOTHER_EVALUATOR)

    if Violation.RAISED not in report.violations and Violation.EXITED not in report.violations:
        if report.result is None:
            report.violations.append(Violation.NO_RESULT)
        else:
            to_dict = getattr(report.result, "to_dict", None)
            if to_dict is None:
                report.violations.append(Violation.NON_CONFORMANT_RESULT)
            else:
                errors = _validate(to_dict())
                if errors:
                    report.schema_errors = errors
                    report.violations.append(Violation.NON_CONFORMANT_RESULT)
    return report
