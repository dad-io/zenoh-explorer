"""K02/K05 — the cockpit's OPTIONAL `toolchain` block, and the contract the G-track aligns to.

**Status: the block is defined and every stage reads `unobserved` until an observation arrives.**
G03's closed observation schema, G04's Bash route, and G05's proof-level protocol are available in
the current installed runtime, but a given artifact may still carry no observations. This module
exists so the pane has one stable projection and so missing observations remain explicit rather
than reading as a pass.

## The block

Omitted from the artifact entirely when no toolchain source is configured. **A missing block means
every stage is `unobserved`** — the Go reader synthesises that rather than treating absence as
clean. When present:

```json
"toolchain": {
  "status": "carried" | "unobserved",
  "reason": "",
  "stages": {
    "gofmt":             {"state": "...", "outcome": null, "toolchain_version": null,
                          "observed_at": null},
    "goimports":         {...},
    "vet_package":       {...},
    "build_module":      {...},
    "vet_module":        {...},
    "race_test_package": {...}
  },
  "dlv_proof_level": {"state": "...", "level": null, "protocol_version": null,
                      "observed_at": null},
  "limits": [...]
}
```

## The closed state set

| state | means | outcome |
|---|---|---|
| `observed` | the stage ran and a result was captured | `pass` or `fail` |
| `unobserved` | **nothing ran, or nothing reached a hook.** Never `passed` | `null` |
| `stale` | a result exists but predates the change it would attest | as captured |
| `tool_missing` | the external tool was absent | `null` — see below |
| `timeout` | the stage was killed by its own timeout | `null` — see below |

`tool_missing`, `timeout` and an `observed`/`fail` are **three different states, deliberately.**
G03's red test records why: today `go vet` or `gofmt` being absent gives `go-postedit.sh` rc 127,
reported **byte-identically** to a genuine vet failure, so a missing toolchain and a broken package
are indistinguishable in the evidence. Neither `tool_missing` nor `timeout` carries an outcome,
because in neither case did the stage reach a verdict about the code.

A state outside this set **raises** rather than being coerced. Coercing to `unobserved` would hide
a producer bug; coercing to a pass would invent an observation.

## `dlv_proof_level`

A state, not a number. G05's levels run 1 (command mentioned — today's gameable rule) to 7 (result
tied to the changed behaviour), and **which level satisfies the gate is unruled**, as is which
levels are transcript-observable at all. So no level is asserted here: `state` is `unobserved`
until something observes one, and a `level` is only ever carried alongside the
`protocol_version` that defined it. The existing `dlv_depth` block carries classifier COUNTS,
which are a different thing and must not be read as a proof level.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bearhug.replay.cockpit_harness import TELEMETRY_RELATIVE

#: The six stages G03 names, in the order the workflow runs them. The Go pane renders this order.
TOOLCHAIN_STAGES: tuple[str, ...] = (
    "gofmt",
    "goimports",
    "vet_package",
    "build_module",
    "vet_module",
    "race_test_package",
)

#: The closed state set. Adding a value here is a schema change and needs the Go side's constant
#: to move with it — `tui/toolchain.go` pins the same list.
STAGE_STATES: frozenset[str] = frozenset(
    {"observed", "unobserved", "stale", "tool_missing", "timeout"}
)

#: The states in which a stage reached a verdict about the code. Every other state carries no
#: outcome, so a missing tool and a timeout can never read as a failure.
OUTCOME_BEARING: frozenset[str] = frozenset({"observed", "stale"})

TOOLCHAIN_LIMITS = (
    "`unobserved` is not `passed`. A stage with no carried observation remains unobserved; G04's "
    "Bash route is applied, but real Bash-delivered Go traffic remains unproven.",
    "`tool_missing` and `timeout` are distinct from a failure: in neither case did the stage reach "
    "a verdict about the code. The G03 schema carries both states without assigning an outcome.",
    "No DLV proof level is asserted here. G05 is provisionally ruled at required level 2, but "
    "which level this selected artifact observed remains unreported.",
)


def _stage(state: str, row: dict[str, Any] | None = None) -> dict[str, Any]:
    if state not in STAGE_STATES:
        raise ValueError(
            f"{state!r} is not a toolchain stage state; the closed set is "
            f"{sorted(STAGE_STATES)}. An unknown state is a producer bug — coercing it would "
            "either hide it or invent an observation."
        )
    row = row or {}
    outcome = row.get("outcome") if state in OUTCOME_BEARING else None
    return {
        "state": state,
        "outcome": outcome,
        "toolchain_version": row.get("toolchain_version"),
        "observed_at": row.get("observed_at"),
    }


def toolchain_block(
    observations: dict[str, dict[str, Any]] | None = None,
    dlv: dict[str, Any] | None = None,
    *,
    invalid_observations: int = 0,
) -> dict[str, Any]:
    """The block, with every unnamed stage `unobserved`.

    `observations` maps a stage name to `{state, outcome?, toolchain_version?, observed_at?}`.
    A stage absent from it stays `unobserved`: silence about a stage is not a pass for it.

    `invalid_observations` is K05's count of telemetry records that named
    `record_kind: toolchain_observation` but failed G03's schema — skipped and counted, never
    projected. It defaults to 0 for callers that build `observations` by hand (every existing
    test) rather than by reading telemetry.
    """
    observations = observations or {}
    unknown = sorted(set(observations) - set(TOOLCHAIN_STAGES))
    if unknown:
        raise ValueError(
            f"unknown toolchain stage(s) {unknown}; G03's closed set is {list(TOOLCHAIN_STAGES)}"
        )

    stages = {
        name: _stage(str(observations[name].get("state", "unobserved")), observations[name])
        if name in observations
        else _stage("unobserved")
        for name in TOOLCHAIN_STAGES
    }

    dlv = dlv or {}
    dlv_state = str(dlv.get("state", "unobserved"))
    if dlv_state not in STAGE_STATES:
        raise ValueError(
            f"{dlv_state!r} is not a dlv_proof_level state; the closed set is "
            f"{sorted(STAGE_STATES)}"
        )
    # A level is carried only with the protocol version that defined it: a bare number would be a
    # claim about a gate nobody has ruled on.
    level = dlv.get("level") if dlv_state in OUTCOME_BEARING else None
    protocol = dlv.get("protocol_version") if level is not None else None

    observed = any(row["state"] != "unobserved" for row in stages.values())
    observed = observed or dlv_state != "unobserved"

    return {
        "status": "carried" if observed else "unobserved",
        "reason": (
            ""
            if observed
            else (
                "no toolchain observation exists in this artifact; "
                "schema and protocol are available"
            )
        ),
        "stages": stages,
        "dlv_proof_level": {
            "state": dlv_state,
            "level": level,
            "protocol_version": protocol,
            "observed_at": dlv.get("observed_at"),
        },
        "invalid_observations": invalid_observations,
        "limits": list(TOOLCHAIN_LIMITS),
    }


#: G03's own closed enums for the fields this producer validates. Duplicated here rather than
#: imported: the schema is a JSON document (`docs/schemas/toolchain-observation.schema.json`), not
#: importable Python, and CLAUDE.md keeps `replay` stdlib-only — no `jsonschema` dependency.
_KNOWN_TOOLS = frozenset({"go", "gofmt", "goimports", "dlv"})
_WRITE_TOOL_NAMES = frozenset({"Edit", "Write", "MultiEdit", "Bash"})
_WRITE_METHODS = frozenset({"direct_path", "tool_response_path", "bash_command", "opaque"})


def _valid_observation(record: dict[str, Any]) -> bool:
    """G03's schema, enforced by hand against exactly the rules this producer depends on: the
    closed enums, the four conditional per-state rules (allOf/if/then in the schema), and
    `write_resolution`'s two required fields. A record failing any of them is a producer bug, not
    a projectable observation — skipped and counted by the caller, never projected.
    """
    if record.get("schema_version") != 1:
        return False
    if record.get("stage") not in TOOLCHAIN_STAGES:
        return False
    state = record.get("state")
    if state not in STAGE_STATES:
        return False
    if record.get("tool") not in _KNOWN_TOOLS:
        return False
    version = record.get("toolchain_version")
    if not isinstance(version, str) or not version:
        return False
    outcome = record.get("outcome")
    if outcome not in ("pass", "fail", None):
        return False
    exit_status = record.get("exit_status")
    if exit_status is not None and not isinstance(exit_status, int):
        return False
    timed_out = record.get("timed_out")
    if not isinstance(timed_out, bool):
        return False
    resolution = record.get("write_resolution")
    if not isinstance(resolution, dict):
        return False
    if resolution.get("tool_name") not in _WRITE_TOOL_NAMES:
        return False
    if resolution.get("method") not in _WRITE_METHODS:
        return False

    if state == "observed":
        return outcome in ("pass", "fail")
    if state == "unobserved":
        return exit_status is None and outcome is None and timed_out is False
    if state == "tool_missing":
        return outcome is None and timed_out is False
    if state == "timeout":
        return outcome is None and timed_out is True
    if state == "stale":
        observed_at = record.get("observed_at")
        return isinstance(observed_at, str) and bool(observed_at)
    return True  # pragma: no cover - state was already checked against STAGE_STATES above


def observations_from_telemetry(subject_root: Path | str) -> tuple[dict[str, dict[str, Any]], int]:
    """K05: read `record_kind == "toolchain_observation"` records from the subject's telemetry
    store, validate each against G03's schema, group by `stage`, and take the newest by
    `observed_at`. Streamed line by line — CLAUDE.md: a telemetry directory grows without bound and
    is never loaded whole.

    Returns the `observations` mapping `toolchain_block()` expects, and the count of records that
    named the right `record_kind` but failed validation (skipped, never projected).
    """
    telemetry_dir = Path(subject_root) / TELEMETRY_RELATIVE
    by_stage: dict[str, list[dict[str, Any]]] = {}
    invalid = 0
    if telemetry_dir.is_dir():
        for events in sorted(telemetry_dir.glob("*/events.jsonl")):
            try:
                with events.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        if record.get("record_kind") != "toolchain_observation":
                            continue
                        if not _valid_observation(record):
                            invalid += 1
                            continue
                        by_stage.setdefault(record["stage"], []).append(record)
            except OSError:
                continue

    observations: dict[str, dict[str, Any]] = {}
    for stage, records in by_stage.items():
        # Newest by observed_at; the enumerate index breaks a tie in favour of the
        # later-encountered record (deterministic file/line order) rather than an arbitrary one.
        _, newest = max(
            enumerate(records), key=lambda item: (str(item[1].get("observed_at") or ""), item[0])
        )
        observations[stage] = {
            "state": newest["state"],
            "outcome": newest.get("outcome"),
            "toolchain_version": newest.get("toolchain_version"),
            "observed_at": newest.get("observed_at"),
        }
    return observations, invalid


def toolchain_from_subject(subject_root: Path | str) -> dict[str, Any]:
    """The cockpit's `toolchain` block, read straight from one subject's telemetry. G05's DLV
    proof-level protocol is unruled and out of scope for this reader, so `dlv_proof_level` always
    reads `unobserved` here — only the six G03 stages are projected."""
    observations, invalid = observations_from_telemetry(subject_root)
    return toolchain_block(observations, invalid_observations=invalid)


__all__ = [
    "OUTCOME_BEARING",
    "STAGE_STATES",
    "TOOLCHAIN_LIMITS",
    "TOOLCHAIN_STAGES",
    "observations_from_telemetry",
    "toolchain_block",
    "toolchain_from_subject",
]
