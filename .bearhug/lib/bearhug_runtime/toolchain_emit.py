"""G03 — a stdlib CLI that appends one toolchain observation to the telemetry store.

`docs/schemas/toolchain-observation.schema.json` (Addendum A.6) defines the record; nothing
emitted one before this module. It is meant to be called directly by a Go-toolchain hook script
after each stage it already runs (gofmt, vet, build, race test) —
`patches/G03-toolchain-emitters.diff` wires `scripts/hooks/go-postedit.sh` and
`scripts/hooks/deep-check.py` to it — as::

    python3 scripts/hooks/_bearhug/toolchain_emit.py \\
        --stage vet_package --tool go --toolchain-version "$(go version)" \\
        --outcome pass --exit-status 0 --duration-ms 812 \\
        --module <path> --package <path> --write-resolution '<json>' \\
        [--state tool_missing | --state timeout --timeout-ms N --timed-out]

**This module never fails the caller.** Every error path prints one line to stderr and exits 0,
matching `stamp.sh`'s own contract ("NEVER fails the caller") — an observation is best-effort and
must never turn a Go-toolchain hook into a blocked turn.

**Invariants checked before anything is written**, matching the schema's own three (`tool_missing`
or `timeout` forbids an `outcome`; `toolchain_version` is mandatory and non-empty; `unobserved`
never carries an outcome), plus the closed vocabulary for `state`, `tool`, `stage`, `outcome`, and
`write_resolution`. **There is deliberately no schema validator here** — R01's own results module
made the same call: a hand-rolled validator that quietly disagreed with the schema in some corner
would be worse than none, because it would let through records the schema itself rejects. What
this module does instead is refuse to *build* an invalid record, and the lab's conformance tests
(`tests/test_runtime_toolchain_emit.py`) validate a written record against the schema itself with
a real validator.

**A disclosed deviation from the closed schema.** `docs/schemas/toolchain-observation.schema.json`
is `additionalProperties: false` over exactly its own fields, and this module adds one more on
top: `runtime_version`, read from `VERSION`. Every other record this repository's telemetry store
holds carries a `runtime_version` for attribution (`docs/proposals/D06-runtime-telemetry-scope.md`
requires it of every record), and a compiler-stage observation with no way to say which runtime
version emitted it would be the one record in the store nothing could attribute. The lab's schema
test validates everything else about the written record against the schema unmodified and states
this one field as the named difference, rather than silently claiming a conformance the record
does not have.

Runnable two ways: imported normally (`from bearhug_runtime import toolchain_emit`, as the test
suite does) or executed directly as a script, which is how a hook actually calls it. Direct
execution cannot use a relative import out of the box — Python only wires `__package__` for `-m`
or a real package import — so the block below sets it by hand before the relative imports run.
This is a plain assignment and a normal import statement, not a dynamic import call, so it does
not trip `tests/test_runtime_package.py`'s dynamic-import or stdlib-surface census.
"""

from __future__ import annotations

import sys
from pathlib import Path

if not __package__:
    _PKG_DIR = Path(__file__).resolve().parent
    sys.path.insert(0, str(_PKG_DIR.parent))
    __package__ = _PKG_DIR.name

import argparse  # noqa: E402 - after the __package__ bootstrap above, deliberately
import json  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from typing import Any  # noqa: E402

from . import runtime_version  # noqa: E402
from .telemetry_store import append as _append_telemetry  # noqa: E402
from .telemetry_store import default_root as _default_telemetry_root  # noqa: E402

#: docs/schemas/toolchain-observation.schema.json: {"const": "toolchain_observation"}
RECORD_KIND = "toolchain_observation"

#: docs/schemas/toolchain-observation.schema.json: {"const": 1} — an INTEGER, unlike the "1"
#: string `telemetry.py`'s coordinator/emitter records carry. Kept exactly as the schema states it
#: rather than coerced to match, so the two schemas cannot silently drift toward one vocabulary.
SCHEMA_VERSION = 1

_VALID_STATES = frozenset({"observed", "unobserved", "stale", "tool_missing", "timeout"})
_VALID_OUTCOMES = frozenset({"pass", "fail"})
_VALID_STAGES = frozenset(
    {"gofmt", "goimports", "vet_package", "build_module", "vet_module", "race_test_package"}
)
_VALID_TOOLS = frozenset({"go", "gofmt", "goimports", "dlv"})
#: A `tool_missing` or `timeout` state forbids an outcome; so does `unobserved` — a write that
#: never reached a toolchain hook has not been observed at all, let alone concluded anything.
_NO_OUTCOME_STATES = frozenset({"tool_missing", "timeout", "unobserved"})
_WRITE_TOOL_NAMES = frozenset({"Edit", "Write", "MultiEdit", "Bash"})
_WRITE_METHODS = frozenset({"direct_path", "tool_response_path", "bash_command", "opaque"})


class InvalidObservation(ValueError):
    """The requested record fails one of the schema's invariants and must not be written."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toolchain_emit.py",
        description="G03 — append one toolchain observation to the project's telemetry store.",
    )
    parser.add_argument("--stage", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--toolchain-version", dest="toolchain_version", required=True)
    parser.add_argument("--write-resolution", dest="write_resolution", required=True)
    parser.add_argument("--state", default="observed")
    parser.add_argument("--outcome", default=None)
    parser.add_argument("--exit-status", dest="exit_status", type=int, default=None)
    parser.add_argument("--duration-ms", dest="duration_ms", type=float, default=None)
    parser.add_argument("--module", dest="module_path", default=None)
    parser.add_argument("--package", dest="package_path", default=None)
    parser.add_argument("--timeout-ms", dest="timeout_ms", type=int, default=None)
    parser.add_argument("--timed-out", dest="timed_out", action="store_true")
    parser.add_argument("--observed-at", dest="observed_at", default=None)
    parser.add_argument("--project-root", dest="project_root", default=None)
    return parser


def build_record(args: argparse.Namespace) -> dict[str, Any]:
    """The record this invocation asks for, before it has been checked against the invariants."""
    try:
        write_resolution = json.loads(args.write_resolution)
    except (TypeError, ValueError) as exc:
        raise InvalidObservation(f"--write-resolution is not valid JSON: {exc}") from exc
    if not isinstance(write_resolution, dict):
        raise InvalidObservation("--write-resolution must decode to a JSON object")

    state = args.state
    observed_at = args.observed_at
    if observed_at is None and state != "unobserved":
        # The schema requires it non-empty only for `stale`, but every other state benefits from
        # knowing when the stage ran; `unobserved` alone is null by construction (nothing ran).
        observed_at = _now()

    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": RECORD_KIND,
        "runtime_version": runtime_version(),
        "stage": args.stage,
        "tool": args.tool,
        "toolchain_version": args.toolchain_version,
        "state": state,
        "outcome": args.outcome,
        "exit_status": args.exit_status,
        "duration_ms": args.duration_ms,
        "module": args.module_path,
        "package": args.package_path,
        "timeout_ms": args.timeout_ms,
        "timed_out": bool(args.timed_out),
        "observed_at": observed_at,
        "write_resolution": write_resolution,
    }


def validate(record: dict[str, Any]) -> None:
    """Raise `InvalidObservation` on the first invariant the record breaks. Never coerces."""
    state = record.get("state")
    if state not in _VALID_STATES:
        raise InvalidObservation(f"unknown state {state!r}")
    if record.get("stage") not in _VALID_STAGES:
        raise InvalidObservation(f"unknown stage {record.get('stage')!r}")
    if record.get("tool") not in _VALID_TOOLS:
        raise InvalidObservation(f"unknown tool {record.get('tool')!r}")
    if not record.get("toolchain_version"):
        raise InvalidObservation("toolchain_version is mandatory and must be non-empty")

    outcome = record.get("outcome")
    if outcome is not None and outcome not in _VALID_OUTCOMES:
        raise InvalidObservation(f"unknown outcome {outcome!r}")
    if state in _NO_OUTCOME_STATES and outcome is not None:
        raise InvalidObservation(f"state={state} forbids an outcome")

    timed_out = record.get("timed_out")
    if state == "timeout" and not timed_out:
        raise InvalidObservation("state=timeout requires --timed-out")
    if state in ("unobserved", "tool_missing") and timed_out:
        raise InvalidObservation(f"state={state} forbids --timed-out")
    if state == "stale" and not record.get("observed_at"):
        raise InvalidObservation("state=stale requires observed_at")

    write_resolution = record.get("write_resolution")
    if not isinstance(write_resolution, dict):
        raise InvalidObservation("write_resolution must be an object")
    if write_resolution.get("tool_name") not in _WRITE_TOOL_NAMES:
        raise InvalidObservation("write_resolution.tool_name is required and must be a known tool")
    if write_resolution.get("method") not in _WRITE_METHODS:
        raise InvalidObservation("write_resolution.method is required and must be a known method")


def main(argv: list[str] | None = None) -> int:
    """Parse, validate, and append. Always returns 0 — see the module docstring.

    `BaseException`, not `Exception`: an interrupted hook is not this CLI's authority to raise
    past its own contract any more than a stamp write is the coordinator's, and the same width
    argument `coordinator.py` makes for `_write_stamps` applies here.
    """
    try:
        args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
        record = build_record(args)
        validate(record)
        root = _default_telemetry_root(args.project_root)
        if not _append_telemetry(record, root=root):
            print("toolchain_emit: telemetry append failed; observation dropped", file=sys.stderr)
    except BaseException as exc:  # noqa: BLE001 - never fail the caller; see the module docstring
        print(f"toolchain_emit: {exc}; observation dropped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
