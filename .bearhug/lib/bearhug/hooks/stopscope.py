"""D01 — the complete Stop population, and what each registration observably does.

Roadmap 9.4 designs one Stop coordinator around five Python blockers. The captured
`settings.json` registers **seven** Stop commands. A coordinator built against the five would
leave two registrations in place that nobody ruled on, and would then claim barracuda registers
one Stop command while it registered three.

Two rules shape this module.

**Count registrations, not scripts.** `audit.registered_specs` drops a command whose script does
not resolve and de-dupes by (event, script, matcher, args). For coverage that is right. For a
census of who may block a turn it is not: an unresolvable command still runs, and de-duping by
name is how three `memex-hook.sh` subcommands went missing once already.

**Silence is not a contract.** `graft-hooks.cjs` resolves `@nanonets/graft` from four candidate
locations and ends in `.catch(() => {})`. If that import fails the wrapper exits 0 having done
nothing, which is byte-identical to a graft that ran and chose not to speak. This module
therefore reports availability separately from behaviour, and never lets an absent external
delegate be read as "does nothing".
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from bearhug.lint.gates import hook_arguments, parse_hooks

#: Interpreters a Stop command may front. Used only to label the registration; the runner is
#: the authority on how a hook is actually launched.
_INTERPRETER = re.compile(r"^\s*(?:\"[^\"]*\"\s+)?(node|python3|python|bash|sh|/usr/bin/env)\b")

#: A wrapper that hands the real work to a package outside the snapshot. Matching on the
#: dynamic-import-plus-swallow shape rather than on the filename, so a second such wrapper is
#: caught rather than assumed absent.
_DELEGATES = (
    (re.compile(r"require\.resolve\(|import\(pathToFileURL"), "resolves a module path at runtime"),
    (re.compile(r"\.catch\(\s*\(\s*\)\s*=>\s*\{"), "swallows import failure in a bare catch"),
    (re.compile(r"node_modules|npm\s+root"), "looks the implementation up in node_modules"),
)


class Availability(StrEnum):
    # StrEnum, not `str, Enum`: the latter's `__str__` returns "Availability.OBSERVED", which
    # rendered the enum's repr into the Markdown report while the JSON (a str subclass, so
    # serialized by value) stayed correct. The two artifacts disagreed.
    """Whether the snapshot can speak for this registration's behaviour at all."""

    #: Ran, and everything it does is inside the snapshot.
    OBSERVED = "observed"
    #: Ran, but the code that decides is outside the snapshot, so the run proves nothing.
    UNAVAILABLE_EXTERNAL = "unavailable_external"
    #: Did not run: file missing, interpreter missing, exec failure.
    LAUNCH_FAILED = "launch_failed"
    #: Ran, but exited at a guard reading a field no constructed fixture supplies. The gate's
    #: decision was never reached, so the run cannot speak for its contract.
    GUARD_NOT_CLEARED = "guard_not_cleared"
    #: Not exercised by any fixture in this census. Assigned to no observation BY DESIGN — an
    #: unexercised registration produces no observation at all, so it is carried in the census's
    #: `not_exercised` list of orders instead. Kept as a member so a reader of the vocabulary sees
    #: the state exists, and pinned by a test so it is not mistaken for a dead branch.
    NOT_EXERCISED = "not_exercised"


class ObservedBehaviour(StrEnum):
    """What a run did, in the vocabulary D01 asks for.

    `SILENT` deliberately does not mean "does nothing" — it means this fixture produced no
    output. Whether that is a verdict depends on `Availability`, which is kept separate.
    """

    JSON_DECISION = "json_decision"
    PLAIN_TEXT = "plain_text"
    BLOCKED = "blocked"
    SILENT = "silent"
    MUTATED_SCRATCH = "mutated_scratch"
    TIMED_OUT = "timed_out"
    ERRORED = "errored"


@dataclass(frozen=True, slots=True)
class StopRegistration:
    """One Stop command exactly as settings.json wires it."""

    event: str
    group_index: int
    position_in_group: int
    #: Total execution order across groups. Group 0 runs before group 1, so this is the fact
    #: that says the two advisory commands run BEFORE the five that block.
    order: int
    command: str
    script: str | None
    arguments: tuple[str, ...]
    timeout: int | None
    status_message: str | None
    async_rewake: bool
    interpreter: str | None
    delegates_externally: bool
    delegate_evidence: tuple[str, ...]
    #: False when this snapshot does not contain the code that decides. Not the same as absent:
    #: the wrapper is captured, its delegate is not. **None means unknown** — the wrapper's own
    #: source was not read, so the census has no basis for a claim either way. Defaulting this
    #: to True without reading the source is how a delegating wrapper would be filed as a
    #: captured contract.
    contract_captured: bool | None
    script_present_in_snapshot: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StopObservation:
    """One registration against one constructed Stop fixture."""

    order: int
    script: str | None
    fixture: str
    availability: Availability
    behaviours: tuple[ObservedBehaviour, ...]
    exit_code: int
    blocked: bool
    decision_keys: tuple[str, ...]
    wrote_paths: tuple[str, ...]
    stamp_verdict: str | None
    duration_ms: float
    timed_out: bool
    capped: bool
    #: Guard features this gate reads that no constructed fixture supplies. Non-empty means the
    #: run did not reach the gate's decision, so its silence is not its contract.
    unreached_guards: tuple[str, ...] = ()
    #: Why the census cannot speak for this run, when it cannot.
    caveat: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _interpreter_of(command: str) -> str | None:
    match = _INTERPRETER.match(command)
    return match.group(1) if match else None


def _delegation(source: str | None) -> tuple[bool, tuple[str, ...]]:
    """Read delegation out of the wrapper's own text. Never from its filename."""
    if not source:
        return False, ()
    hits = tuple(why for pattern, why in _DELEGATES if pattern.search(source))
    # One signal alone is weak — plenty of scripts resolve a path. The swallow is what makes
    # the contract uncapturable, so require it plus at least one resolution signal.
    swallows = any("swallows" in why for why in hits)
    return (bool(swallows and len(hits) >= 2), hits)


def stop_registrations(
    settings: dict[str, Any], snapshot_dir: Path | None = None
) -> list[StopRegistration]:
    """Every Stop command in declaration order, including ones whose script does not resolve."""
    project = Path(snapshot_dir) / "project" if snapshot_dir else None
    registrations: list[StopRegistration] = []
    for spec in parse_hooks(settings):
        if spec.event != "Stop":
            continue
        script = spec.script
        source: str | None = None
        present: bool | None = None
        if project is not None and script:
            path = project / script
            present = path.is_file()
            if present:
                source = path.read_text(encoding="utf-8", errors="replace")
        delegates, evidence = _delegation(source)
        if source is None:
            # No source read: say so. `False` here would assert the wrapper does not delegate,
            # which is precisely the claim we cannot make without looking at it.
            captured: bool | None = None
            if script is None:
                # A command that names no script has no wrapper to read, and the census can
                # still say plainly that nothing about its contract is captured.
                captured = False
                evidence = ("command names no script in the snapshot",)
        else:
            captured = not delegates
        registrations.append(
            StopRegistration(
                event=spec.event,
                group_index=spec.group_index if spec.group_index is not None else 0,
                position_in_group=(
                    spec.position_in_group if spec.position_in_group is not None else 0
                ),
                order=len(registrations),
                command=spec.command,
                script=script,
                arguments=tuple(hook_arguments(spec.command)),
                timeout=spec.timeout,
                status_message=spec.status_message,
                async_rewake=spec.async_rewake,
                interpreter=_interpreter_of(spec.command),
                delegates_externally=delegates,
                delegate_evidence=evidence,
                contract_captured=captured,
                script_present_in_snapshot=present,
            )
        )
    registrations.sort(key=lambda r: (r.group_index, r.position_in_group))
    return [
        StopRegistration(**{**reg.to_dict(), "order": index})
        for index, reg in enumerate(registrations)
    ]


#: Where graft's wrapper looks for its implementation, in the wrapper's own order. Read here
#: only to REPORT whether the delegate resolves; the census never treats a resolved delegate as
#: snapshot evidence, because a snapshot cannot pin a globally installed npm package.
_GRAFT_PACKAGE = "@nanonets/graft"


def _resolve_graft_delegate() -> dict[str, Any]:
    """Does graft's external implementation exist on THIS machine, and at what version?

    A resolved delegate means a run exercises real graft code. That is worth recording and
    worth distrusting: the code is outside the snapshot, so the same wrapper on another machine
    — or after an `npm update` — can behave differently with no snapshot change at all.
    """
    import subprocess

    roots: list[Path] = [Path.home() / ".npm-global" / "lib" / "node_modules"]
    try:
        out = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=20, check=False
        )
        if out.returncode == 0 and out.stdout.strip():
            roots.append(Path(out.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        pass

    for root in roots:
        package = root / _GRAFT_PACKAGE
        claude_dir = package / "dist" / "claude"
        if not (claude_dir / "hooks.js").is_file():
            continue
        version = None
        with contextlib.suppress(OSError, ValueError):
            version = json.loads(
                (package / "package.json").read_text(encoding="utf-8")
            ).get("version")
        return {
            "resolved": True,
            "path": str(claude_dir),
            "version": version,
            "note": (
                "resolved OUTSIDE the snapshot. Runs here exercise this build; the snapshot "
                "pins only the wrapper, so this observation is not reproducible from snapshot "
                "evidence alone."
            ),
        }
    return {
        "resolved": False,
        "path": None,
        "version": None,
        "note": (
            "not resolvable here. The wrapper's bare `.catch` makes an absent delegate exit 0 "
            "silently, so a silent run is not evidence that graft's Stop does nothing."
        ),
    }


#: Transcript/payload features a Stop gate can guard on, and which the CONSTRUCTED fixtures may
#: or may not supply. A gate that exits early because its guard token never appears in any
#: fixture has not been observed passing — it has not been reached at all, and recording it as
#: a plain silent observation would assert a contract the run cannot support.
#:
#: Deliberately a small, explicit vocabulary rather than a general analyser. It is a heuristic
#: over the gate's own source text, and it is published as evidence so a reader can check it.
_GUARD_FEATURES = (
    # memex-hook.sh's Stop branch marks a turn boundary with `.promptSource != null`; without
    # it `marks` is empty and the script exits 0 before its /decide reminder.
    "promptSource",
    "AskUserQuestion",
    "stop_hook_active",
)


def _fixture_features(fixtures: list[Any]) -> dict[str, bool]:
    """Which guard features the constructed Stop fixtures actually supply."""
    blob = json.dumps(
        [{"payload": f.payload, "transcript": f.transcript} for f in fixtures], default=str
    )
    return {feature: (feature in blob) for feature in _GUARD_FEATURES}


def _unreached_guards(source: str | None, available: dict[str, bool]) -> tuple[str, ...]:
    """Guard features this gate reads that no fixture supplies."""
    if not source:
        return ()
    return tuple(
        feature
        for feature in _GUARD_FEATURES
        if feature in source and not available.get(feature, False)
    )


def _behaviours(run: Any) -> tuple[ObservedBehaviour, ...]:
    """Classify one run into the vocabulary D01 asks for. A run can be several of these."""
    found: list[ObservedBehaviour] = []
    if run.timed_out:
        found.append(ObservedBehaviour.TIMED_OUT)
    if run.errored:
        found.append(ObservedBehaviour.ERRORED)
    if run.decision is not None:
        found.append(ObservedBehaviour.JSON_DECISION)
    elif run.stdout.strip():
        found.append(ObservedBehaviour.PLAIN_TEXT)
    if run.blocked:
        found.append(ObservedBehaviour.BLOCKED)
    if run.wrote_paths:
        found.append(ObservedBehaviour.MUTATED_SCRATCH)
    if not found and not run.stdout.strip() and not run.stderr.strip():
        found.append(ObservedBehaviour.SILENT)
    elif not found:
        # stderr only: it spoke, just not to the model.
        found.append(ObservedBehaviour.PLAIN_TEXT)
    return tuple(found)


def observe_stop_contracts(
    snapshot_dir: Path,
    project_dir: Path,
    registrations: list[StopRegistration],
    graft_delegate: dict[str, Any],
) -> tuple[list[StopObservation], list[int], dict[str, bool]]:
    """Run every Stop registration against every constructed Stop fixture, in scratch.

    Only snapshotted wrappers are executed, and only against fixtures this repo constructs. The
    fixture repo is rebuilt before each run so `wrote_paths` is a property of the gate rather
    than of battery order — the same reason `run_battery` does it.
    """
    from bearhug.hooks.audit import _stage_hooks
    from bearhug.hooks.fixtures import corpus
    from bearhug.hooks.runner import BATTERY_MAX_WAIT, DEFAULT_TIMEOUT, run_hook
    from bearhug.hooks.scratch import assert_scratch, build_fixture_repo

    snapshot_dir = Path(snapshot_dir)
    project = snapshot_dir / "project"
    scratch = assert_scratch(Path(project_dir))
    repo = scratch / "repo"
    build_fixture_repo(repo)
    staged = _stage_hooks(project, scratch) if (project / "scripts" / "hooks").is_dir() else None

    fixtures = [f for f in corpus(repo) if f.event == "Stop"]
    available = _fixture_features(fixtures)
    observations: list[StopObservation] = []
    not_exercised: list[int] = []

    for reg in registrations:
        if reg.script is None:
            not_exercised.append(reg.order)
            continue
        path = (staged / reg.script) if staged and (staged / reg.script).is_file() else (
            project / reg.script)
        if not path.is_file():
            not_exercised.append(reg.order)
            continue
        if not fixtures:
            not_exercised.append(reg.order)
            continue
        unreached = _unreached_guards(
            path.read_text(encoding="utf-8", errors="replace"), available
        )
        for fixture in fixtures:
            build_fixture_repo(repo)
            configured = reg.timeout if reg.timeout is not None else DEFAULT_TIMEOUT
            run = run_hook(
                path,
                fixture,
                project_dir=repo,
                timeout=min(float(configured), BATTERY_MAX_WAIT),
                arguments=list(reg.arguments),
            )
            if run.errored:
                availability = Availability.LAUNCH_FAILED
                caveat = "the hook never ran; this says nothing about its contract"
            elif reg.delegates_externally:
                availability = Availability.UNAVAILABLE_EXTERNAL
                where = (
                    f"resolved: {graft_delegate.get('version')}"
                    if graft_delegate.get("resolved")
                    else "unresolved"
                )
                caveat = (
                    f"the code that decides is outside the snapshot ({where}). This run "
                    "cannot establish the registration's Stop contract, and silence here "
                    "cannot be read as doing nothing."
                )
            elif unreached and tuple(_behaviours(run)) == (ObservedBehaviour.SILENT,):
                # Only a FULLY silent run can be explained by an uncleared guard. A run that
                # emitted a decision, wrote, or stamped plainly reached its own logic, and
                # `response-shape.py` mentioning AskUserQuestion is not evidence it exited
                # early — it blocked on one of these very fixtures. Observed behaviour
                # outranks the source-text heuristic.
                availability = Availability.GUARD_NOT_CLEARED
                caveat = (
                    "this gate reads "
                    + ", ".join(unreached)
                    + ", which no constructed Stop fixture supplies, so the run exited before "
                    "its decision. Its silence here is not its contract."
                )
            else:
                availability = Availability.OBSERVED
                caveat = None
            decision = run.decision or {}
            observations.append(
                StopObservation(
                    order=reg.order,
                    script=reg.script,
                    fixture=fixture.name,
                    availability=availability,
                    behaviours=_behaviours(run),
                    exit_code=run.exit_code,
                    blocked=run.blocked,
                    decision_keys=tuple(sorted(decision)),
                    wrote_paths=run.wrote_paths,
                    stamp_verdict=run.stamp_verdict,
                    duration_ms=round(run.duration_ms, 1),
                    timed_out=run.timed_out,
                    capped=run.capped,
                    unreached_guards=unreached,
                    caveat=caveat,
                )
            )
    return observations, not_exercised, available


#: The five the roadmap's coordinator design names. Kept here so the census can state, as data,
#: how far the real Stop population exceeds the design.
ROADMAP_COORDINATOR_GATES = (
    "scripts/hooks/response-shape.py",
    "scripts/hooks/dlv-verify-gate.py",
    "scripts/hooks/task-durability.py",
    "scripts/hooks/joinkey-lint.py",
    "scripts/hooks/review-gate.py",
)


def stop_census(
    snapshot_dir: Path, project_dir: Path, *, snapshot_id: str
) -> dict[str, Any]:
    """The D01 artifact: every Stop registration, what it did, and what cannot be known."""
    from bearhug.hooks.audit import load_settings

    snapshot_dir = Path(snapshot_dir)
    settings = load_settings(snapshot_dir)
    registrations = stop_registrations(settings, snapshot_dir)
    graft_delegate = _resolve_graft_delegate()
    observations, not_exercised, fixture_features = observe_stop_contracts(
        snapshot_dir, Path(project_dir), registrations, graft_delegate
    )

    # How many independently registered gates block on the SAME event. Computed rather than
    # narrated: Roadmap 9.4 calls the arbitration collision its red test, and a count per
    # fixture is what D03 needs to build an arbitration table against.
    contention: list[dict[str, Any]] = []
    by_fixture: dict[str, list[str]] = {}
    for obs in observations:
        if obs.blocked and obs.script:
            by_fixture.setdefault(obs.fixture, []).append(obs.script)
    for fixture in sorted({o.fixture for o in observations}):
        scripts = sorted(by_fixture.get(fixture, []))
        contention.append(
            {"fixture": fixture, "blocking_gates": len(scripts), "scripts": scripts}
        )

    captured = sum(1 for r in registrations if r.contract_captured is True)
    unknown = sum(1 for r in registrations if r.contract_captured is None)
    not_captured = sum(1 for r in registrations if r.contract_captured is False)
    scripts = {r.script for r in registrations}
    return {
        "schema_version": 1,
        "task": "D01",
        "snapshot_id": snapshot_id,
        "snapshot_dir": str(snapshot_dir),
        "registrations": [r.to_dict() for r in registrations],
        "observations": [o.to_dict() for o in observations],
        "not_exercised": not_exercised,
        "fixture_features": fixture_features,
        "contention": contention,
        "external_delegates": {
            r.script: graft_delegate
            for r in registrations
            if r.delegates_externally and r.script
        },
        "summary": {
            "registrations": len(registrations),
            "contract_captured": captured,
            "contract_not_captured": not_captured + unknown,
            "roadmap_coordinator_names": len(ROADMAP_COORDINATOR_GATES),
            "undeclared_by_roadmap": len(scripts - set(ROADMAP_COORDINATOR_GATES)),
            "groups": sorted({r.group_index for r in registrations}),
            "unbounded_timeouts": [r.script for r in registrations if r.timeout is None],
            "max_simultaneous_blocks": max(
                (row["blocking_gates"] for row in contention), default=0
            ),
        },
        "limits": [
            "A registration silent across THESE fixtures is silent across these fixtures, "
            "never inert. Several Stop gates act only on state the fixtures do not construct.",
            "An external delegate that resolves here is not snapshot evidence: the snapshot "
            "pins the wrapper only, so the same wrapper can behave differently elsewhere.",
            "This census records observable behaviour and registration wiring. It does not "
            "decide what the coordinator should do with any of it — that is D02.",
            "`guard_not_cleared` is a heuristic over the gate's own source text against the "
            "features the fixtures supply, published in `fixture_features`. It flags a run "
            "that cannot speak for a contract; it does not prove the gate would act.",
        ],
    }


def render_markdown(census: dict[str, Any]) -> str:
    """A rendering of the JSON, never a second computation over the runs."""
    lines: list[str] = []
    summary = census["summary"]
    lines.append(f"# Stop contract census — snapshot {census['snapshot_id']}")
    lines.append("")
    lines.append(
        f"**{summary['registrations']} registered Stop commands** across groups "
        f"{summary['groups']}. Roadmap 9.4's coordinator design names "
        f"{summary['roadmap_coordinator_names']}; {summary['undeclared_by_roadmap']} "
        f"registration(s) are outside that design and unruled."
    )
    lines.append("")
    lines.append(
        f"Contract captured by the snapshot: {summary['contract_captured']}. "
        f"Not captured: {summary['contract_not_captured']}."
    )
    lines.append("")
    lines.append("## Registrations, in execution order")
    lines.append("")
    lines.append("| order | group.pos | script | args | timeout | contract |")
    lines.append("|---|---|---|---|---|---|")
    for reg in census["registrations"]:
        captured = reg["contract_captured"]
        state = {True: "captured", False: "**not captured**", None: "unknown"}[captured]
        timeout = "**unbounded**" if reg["timeout"] is None else f"{reg['timeout']}s"
        args = " ".join(reg["arguments"]) or "—"
        lines.append(
            f"| {reg['order']} | {reg['group_index']}.{reg['position_in_group']} | "
            f"`{reg['script'] or reg['command']}` | `{args}` | {timeout} | {state} |"
        )
    lines.append("")
    lines.append("## Observed behaviour")
    lines.append("")
    lines.append("| order | script | fixture | availability | behaviour | exit | blocked |")
    lines.append("|---|---|---|---|---|---|---|")
    for obs in census["observations"]:
        lines.append(
            f"| {obs['order']} | `{obs['script']}` | {obs['fixture']} | "
            f"{obs['availability']} | {', '.join(obs['behaviours'])} | "
            f"{obs['exit_code']} | {'yes' if obs['blocked'] else 'no'} |"
        )
    lines.append("")
    for script, delegate in census["external_delegates"].items():
        lines.append(f"### External delegate — `{script}`")
        lines.append("")
        lines.append(
            f"- resolved here: **{delegate['resolved']}**"
            + (f" (version {delegate['version']}, `{delegate['path']}`)"
               if delegate["resolved"] else "")
        )
        lines.append(f"- {delegate['note']}")
        lines.append("")
    lines.append("## Simultaneous blocks per event")
    lines.append("")
    lines.append(
        "Each row is one constructed Stop event. Every listed gate emitted its own Stop "
        "decision independently, so the model receives that many instructions at once."
    )
    lines.append("")
    lines.append("| fixture | blocking gates | gates |")
    lines.append("|---|---|---|")
    for row in census["contention"]:
        gates = ", ".join(f"`{s}`" for s in row["scripts"]) or "—"
        lines.append(f"| {row['fixture']} | {row['blocking_gates']} | {gates} |")
    lines.append("")
    lines.append("## Limits")
    lines.append("")
    for limit in census["limits"]:
        lines.append(f"- {limit}")
    lines.append("")
    return "\n".join(lines)
