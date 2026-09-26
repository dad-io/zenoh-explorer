"""3.3–3.8 — run every gate against the fixture corpus and say what it actually did.

Phase 2 reads the harness. This runs it. The difference matters most for the write-path pairs:
static analysis can show that a matcher excludes Bash, but only execution can show whether a
gate given a Bash-delivered write behaves the same as one given an Edit-delivered write.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Any

from bearhug.hooks.coverage import (
    check_stop_arbitration,
    check_stop_ordering,
    check_test_coverage,
)
from bearhug.hooks.fixtures import EventFixture, corpus
from bearhug.hooks.ownership import installed_paths, load_setup_receipt, release_label
from bearhug.hooks.runner import (
    BATTERY_MAX_WAIT,
    DEFAULT_TIMEOUT,
    HookRun,
    run_hook,
    toolchain_note,
)
from bearhug.hooks.scratch import build_fixture_repo
from bearhug.lint.gates import PROJECT_DIR_VAR, SCRIPT_SUFFIXES, hook_arguments, parse_hooks
from bearhug.lint.reachability import literal_tool_union
from bearhug.model import Evidence, Finding, HookSpec, Severity
from bearhug.read_optional import read_json_or_default

#: Above this, a "configured" timeout is not a bound in any practical sense — deep-check.py's
#: legitimate background timeout is 600s (asyncRewake); graft-hooks.cjs is wired at 8000s
#: (2h13m) on PostToolUse/SessionStart/Stop with no asyncRewake, and passes `spec.timeout is
#: None` clean because it IS configured — just not to anything a person would call bounded.
IMPLAUSIBLE_TIMEOUT_S = 3600.0

#: Which decision keys each event may legally emit.
LEGAL_KEYS: dict[str, frozenset[str]] = {
    "PreToolUse": frozenset({"permissionDecision", "hookSpecificOutput", "continue",
                             "stopReason", "suppressOutput", "systemMessage"}),
    "PostToolUse": frozenset({"decision", "reason", "hookSpecificOutput", "continue",
                              "stopReason", "suppressOutput", "systemMessage"}),
    "Stop": frozenset({"decision", "reason", "continue", "stopReason", "suppressOutput",
                       "systemMessage"}),
    "SessionStart": frozenset({"hookSpecificOutput", "continue", "suppressOutput",
                               "systemMessage"}),
    "UserPromptSubmit": frozenset({"decision", "reason", "hookSpecificOutput", "continue",
                                   "stopReason", "suppressOutput", "systemMessage"}),
}

#: The pairs whose whole purpose is to deliver one write by two paths.
WRITE_PATH_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("posttooluse-edit-go", "posttooluse-bash-heredoc-go", "the same .go write"),
    ("stop-go-edit-no-dlv", "stop-go-bash-write-no-dlv", "the same .go write"),
    # Decision 0282: hard-safety.py denies the direct push and is blind to the identical
    # command wrapped in `bash -c '...'` — a known, accepted limit, not something this pair is
    # trying to fix. It gives the fixed negative control (dlv-verify-gate.py, review-gate.py) a
    # third, independent gate to discriminate against.
    ("pretooluse-bash-push", "pretooluse-bash-push-via-heredoc",
     "the same forbidden `git push`"),
)

LIMIT_FIXTURES = (
    "Measured against a synthetic fixture corpus in a disposable repo, not against real "
    "traffic. A gate silent here is silent across THESE fixtures — never 'dead'. A gate that "
    "fires here fires on a constructed case, which is what makes its zero elsewhere meaningful."
)

#: For a LATENCY finding built from the SPEC alone — no fixture in this corpus is ever
#: delivered to this registration, so there is no run to cite. Four registrations
#: (`memex-hook.sh pre-question`/`pre-decide`, `graft-prep-hook.sh pre-question`/`pre-plan`)
#: are wired on matchers (AskUserQuestion/Skill/ExitPlanMode) nothing in the corpus uses as a
#: tool_name, so they got zero runs and, before this, zero findings — timeout configuration is
#: a property of settings.json, not of what the battery happened to exercise.
LIMIT_STATIC_SPEC = (
    "Read directly from the settings.json registration, not from a run — no fixture in this "
    "corpus is ever delivered to this matcher. Says nothing about the hook's behaviour, only "
    "about what is declared; without it, an unbounded registration the corpus cannot reach "
    "was invisible rather than merely silent."
)

#: What a Bear Hug-owned subcommand DOES for the write it received, drawn from
#: docs/OPERATING-MODES.md's hook table — the one place this project already documents each
#: registration's declared effect. Used only to make a WRITE-PATH "not invoked on Bash" finding
#: state a concrete cost instead of the abstract "no behaviour to compare"; a subcommand missing
#: here (a new registration, or a project's own patched copy) falls back honestly to "not
#: catalogued" rather than a guess.
_KNOWN_HOOK_EFFECTS: dict[tuple[str, str], str] = {
    ("memex-hook.sh", "post-edit"): "records decision-relevant edits into the corpus",
    ("memex-hook.sh", "session-start"): "loads recorded decision context",
    ("memex-hook.sh", "stop"): "closes the decision record for the turn",
    ("graft-hooks.cjs", "session-start"): "warms the Graft code map",
    ("graft-hooks.cjs", "tool-savings"): "records what the code map saved",
    ("graft-hooks.cjs", "stop"): "finalizes Graft accounting",
}


def _fixtures_for(
    event: str, matcher: str | None, all_fixtures: list[EventFixture]
) -> list[EventFixture]:
    """Fixtures Claude Code would actually deliver to this registration.

    Filtering on event alone runs gates against tools their matcher excludes — `go-postedit.sh`
    (Edit|Write|MultiEdit) against a Bash payload it would never receive. The conclusion may
    survive, but the MECHANISM named in the finding would be wrong: the gate is not missing the
    write, it is never invoked on it.
    """
    from bearhug.lint.reachability import matcher_tools

    tools = matcher_tools(matcher)
    picked = []
    for fixture in all_fixtures:
        if fixture.event != event:
            continue
        tool = fixture.payload.get("tool_name")
        if tools and tool and tool not in tools:
            continue
        picked.append(fixture)
    return picked


def _settings_path(snapshot_dir: Path) -> Path:
    return Path(snapshot_dir) / "project" / ".claude" / "settings.json"


def settings_captured(snapshot_dir: Path) -> bool:
    """Whether this snapshot's project captured `.claude/settings.json` at all.

    Mode 1 (docs/OPERATING-MODES.md) observes a project before any setup decision, and such a
    project has none yet. That is the project's state, not a broken snapshot: callers use this
    to say so plainly rather than reading the empty dict `load_settings` also returns for a
    present-but-genuinely-empty file as if it meant the same thing.
    """
    return _settings_path(snapshot_dir).is_file()


def load_settings(snapshot_dir: Path) -> dict[str, Any]:
    settings, _absent = read_json_or_default(_settings_path(snapshot_dir))
    return settings


def registered_specs(settings: dict[str, Any]) -> list[HookSpec]:
    """Every hook REGISTRATION wired in settings.json that names a script, deduped.

    Keyed by the whole registration, not the script name: barracuda wires one script on one
    event several times with different subcommands and matchers. `memex-hook.sh pre-decide` is
    a different gate from `pre-question`, and de-duping on the name dropped three of them.

    Exposed separately from `run_battery` so a registration that the fixture corpus never
    delivers a single fixture to — four `PreToolUse` hooks on `AskUserQuestion`/`Skill`/
    `ExitPlanMode` matchers no fixture uses — can still be reasoned about statically. Timeout
    configuration is a property of settings.json, not of whether the battery happened to
    exercise it.
    """
    specs: list[HookSpec] = []
    seen: set[tuple[str, str, str, str]] = set()
    for spec in parse_hooks(settings):
        script = spec.script
        if not script:
            continue
        args = hook_arguments(spec.command)
        key = (spec.event, script, spec.matcher or "*", " ".join(args))
        if key in seen:
            continue
        seen.add(key)
        specs.append(spec)
    return specs


def _stage_hooks(project: Path, scratch: Path) -> Path:
    """Copy the snapshot's hook scripts somewhere writable before running them.

    A snapshot is frozen evidence — `snapshots/<label>/` is committed and every finding cites
    it. Running the battery straight out of that tree let Python write `__pycache__/*.pyc` back
    into it, and four of those files reached a commit. Nothing may mutate a snapshot, including
    bear-hug, including as a side effect.
    """
    staged = scratch / "_hooks"
    if staged.exists():
        shutil.rmtree(staged)
    # Relative paths are preserved: hooks live in two trees (scripts/hooks/ and
    # .claude/helpers/), while native project hooks import their sibling helper modules from
    # scripts/ and may load the bundled architecture package from .bearhug/lib.
    for sub in (
        "scripts/hooks",
        ".claude/helpers",
        ".bearhug/lib",
        "scripts/bearhug_work.py",
        "scripts/bearhug_native.py",
    ):
        source = project / sub
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, staged / sub,
                            ignore=shutil.ignore_patterns("__pycache__"))
        elif source.is_file() and not source.is_symlink():
            destination = staged / sub
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return staged


def _install_replay_library(staged: Path, project_dir: Path) -> None:
    """Expose only the bundled project library at the disposable hook root."""
    source = staged / ".bearhug" / "lib"
    if not source.is_dir():
        return
    target = project_dir / ".bearhug" / "lib"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise RuntimeError(f"refusing replay library destination: {target}")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))


def _sandbox_arguments(command: str, project_dir: Path) -> list[str]:
    """Keep replayed helper commands inside the disposable fixture repository."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        original = hook_arguments(command)
    else:
        original = []
        for index, token in enumerate(tokens):
            if token.endswith(SCRIPT_SUFFIXES):
                original = tokens[index + 1:]
                break
    result: list[str] = []
    index = 0
    root = str(Path(project_dir).resolve())
    while index < len(original):
        argument = original[index]
        if argument == "--root":
            result.extend(("--root", root))
            # A shell-quoted value is removed by hook_arguments when it is an environment
            # expression, but an absolute recorded target remains and must be consumed here.
            if index + 1 < len(original) and not original[index + 1].startswith("-"):
                index += 1
        elif argument.startswith("--root="):
            result.append(f"--root={root}")
        elif argument.startswith("$"):
            # Preserve hook_arguments' existing treatment of shell variables other than the
            # project root; the replay runner supplies its environment explicitly.
            pass
        else:
            result.append(argument)
        index += 1
    return result


def _replay_script(staged: Path, project: Path, script: str) -> Path:
    """Rebase only the exact captured project root, never guess by script basename."""
    relative = script
    manifest = project.parent / "manifest.json"
    if manifest.is_file():
        subject = json.loads(manifest.read_text()).get("subject", {})
        captured = subject.get("project", subject.get("barracuda", {})).get("root")
        if isinstance(captured, str):
            prefix = captured.rstrip("/").lstrip("/") + "/"
            if script.startswith(prefix):
                relative = script[len(prefix):]
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Hook script escapes the captured project")
    candidates = [staged / relative, project / relative]
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def run_battery(snapshot_dir: Path, project_dir: Path) -> list[tuple[Any, HookRun]]:
    """Every wired hook against every fixture for its event."""
    snapshot_dir = Path(snapshot_dir)
    project = snapshot_dir / "project"
    settings = load_settings(snapshot_dir)
    all_fixtures = corpus(project_dir)
    staged = _stage_hooks(project, project_dir.parent)

    results: list[tuple[Any, HookRun]] = []
    for spec in registered_specs(settings):
        script = spec.script
        args = _sandbox_arguments(spec.command, project_dir)
        path = _replay_script(staged, project, script)
        for fixture in _fixtures_for(spec.event, spec.matcher, all_fixtures):
            # One shared repo, run after run, means `wrote_paths` is a property of BATTERY
            # ORDER rather than of the gate: `go-postedit.sh`'s `internal/svc/messy.go` rewrite
            # was only visible because it happened to run first, and a second gate doing its
            # own `gofmt -w`, or a re-run of the same gate, would have found nothing left to
            # change. Rebuilding before every single run makes each observation independent of
            # what ran before it, at the cost of ~3 subprocess calls per run — cheap against a
            # ~15-file fixture repo.
            build_fixture_repo(project_dir)
            _install_replay_library(staged, project_dir)
            hook_timeout = spec.timeout if spec.timeout is not None else DEFAULT_TIMEOUT
            results.append(
                (spec, run_hook(path, fixture, project_dir=project_dir,
                                timeout=hook_timeout, arguments=args))
            )
    return results


def audit(
    snapshot_dir: Path, project_dir: Path, *, snapshot_id: str
) -> tuple[list[Finding], list[tuple[Any, HookRun]]]:
    """CONTRACT, LATENCY, INERTNESS and the write-path comparison, as findings."""
    results = run_battery(snapshot_dir, project_dir)
    settings = load_settings(snapshot_dir)
    tc_note = toolchain_note()

    def _lim(text: str) -> str:
        return f"{text} {tc_note}"

    findings: list[Finding] = []
    if not settings_captured(snapshot_dir):
        # No `.claude/settings.json` in this snapshot: a project observed before setup (mode 1)
        # never had one, and a project that WAS set up and later lost the file looks identical
        # here (the readiness checks decide that, reading the live subject). A snapshot now DOES
        # capture the setup receipt (`.bearhug/project-setup.json`, `snapshot/spec.py`'s
        # PROJECT_RULES) — added so `check_test_coverage` (coverage.py) can tell Bear Hug's own
        # installed hooks apart from the project's without guessing from a filename — but the
        # receipt is written by `bearhug setup`, never by anything that runs when settings.json
        # is absent, so its presence or absence cannot resolve THIS ambiguity either: a project
        # mid-way through adoption, or one that hand-deleted settings.json but left the receipt,
        # would still be indistinguishable from a genuine pre-setup project by snapshot content
        # alone. Either way every check below ran against zero registrations. Said explicitly:
        # an empty findings list from a battery that never had a gate to run reads exactly like a
        # battery that ran and found every gate clean, and this module's own culture
        # (LIMIT_FIXTURES, LIMIT_STATIC_SPEC above) exists precisely so that kind of silence is
        # never mistaken for a verdict.
        findings.append(
            Finding(
                id="hook-audit-input-absent",
                check="INPUT-ABSENT",
                severity=Severity.INFO,
                summary=(
                    "no hook registrations: .claude/settings.json is absent from this snapshot"
                ),
                snapshot=snapshot_id,
                evidence=(Evidence(file=".claude/settings.json"),),
                detail=(
                    "This battery ran against zero registrations. Every CONTRACT/LATENCY/"
                    "INERTNESS/WRITE-PATH finding below is silent because there was nothing "
                    "wired, not because it was checked and found clean."
                ),
                limit=(
                    "Proves this snapshot has no settings.json. Absence is the expected state "
                    "of a project that has never been set up, and is equally what a project "
                    "that WAS set up and later lost settings.json would look like — this "
                    "snapshot alone cannot tell the two apart. Whether this is a regression is "
                    "decided by the readiness checks, which read the live subject directly "
                    "rather than this snapshot."
                ),
            )
        )
    # Bucketed per REGISTRATION, not per script name. memex-hook.sh is wired on four events
    # with four timeouts; collapsing them hid an unbounded PreToolUse registration and labelled
    # a 10-run mixed-event bucket as "silent across all fixtures for its event".
    by_hook: dict[tuple[str, str, str], list[tuple[Any, HookRun]]] = {}
    for spec, run in results:
        by_hook.setdefault((run.hook, spec.event, spec.matcher or "*"), []).append((spec, run))

    for (hook, _event, _matcher), rows in sorted(by_hook.items()):
        runs = [r for _, r in rows]

        # 3.3 CONTRACT — did it run at all, and is its output legal for the event?
        for run in runs:
            if run.errored:
                findings.append(Finding(
                    id=f"hook-errored-{hook}-{run.fixture}", check="CONTRACT",
                    severity=Severity.BROKEN,
                    summary=f"{hook} could not execute against {run.fixture}",
                    snapshot=snapshot_id,
                    evidence=(Evidence(file=f"scripts/hooks/{hook}",
                                       excerpt=run.stderr.strip()[:160]),),
                    detail=f"{hook} {run.event} exit={run.exit_code}",
                    limit=_lim(LIMIT_FIXTURES),
                ))
                continue
            decision = run.decision
            if decision is None:
                continue
            illegal = set(decision) - LEGAL_KEYS.get(run.event, frozenset())
            if illegal:
                findings.append(Finding(
                    id=f"hook-illegal-key-{hook}-{run.fixture}", check="CONTRACT",
                    severity=Severity.BROKEN,
                    summary=f"{hook} emits {sorted(illegal)} which {run.event} does not define",
                    snapshot=snapshot_id,
                    evidence=(Evidence(file=f"scripts/hooks/{hook}",
                                       excerpt=run.stdout.strip()[:160]),),
                    detail=f"{hook} {run.event} keys={sorted(decision)}",
                    limit=_lim(LIMIT_FIXTURES),
                ))

    # `bearhug_owned` is None (not {}) when there is no receipt at all, distinct from a receipt
    # that exists but lists no files — `installed_paths(None)` and an empty `files: []` receipt
    # both return {}, which `_write_path_findings` must not mistake for each other: only the
    # first has nothing to say about ownership, while the second still knows a hook it can't
    # find is the project's own.
    receipt = load_setup_receipt(snapshot_dir)
    bearhug_owned = installed_paths(receipt) if receipt is not None else None
    release = release_label(snapshot_dir) if receipt is not None else None

    findings += _inertness_findings(by_hook, snapshot_id, tc_note)
    findings += _latency_findings(registered_specs(settings), by_hook, snapshot_id, tc_note)
    findings += _write_path_findings(by_hook, snapshot_id, tc_note, bearhug_owned, release)
    findings += _overlap_findings(parse_hooks(settings), snapshot_id, tc_note)
    findings += check_test_coverage(snapshot_dir, snapshot_id=snapshot_id)
    findings += check_stop_ordering(snapshot_dir, results, snapshot_id=snapshot_id)
    findings += check_stop_arbitration(results, snapshot_id=snapshot_id)
    return findings, results


def _inertness_findings(
    by_hook: dict[tuple[str, str, str], list[tuple[Any, HookRun]]],
    snapshot_id: str,
    tc_note: str,
) -> list[Finding]:
    """3.6 — silent for every fixture is a SUSPICION, never a verdict.

    "Silent" here means no OBSERVABLE effect on the repo's real content — `.automation-stamps/`
    writes are counted in `wrote_paths` (runner._FINGERPRINT_EXCLUDE no longer excludes them)
    but discounted from THIS classification on purpose: a gate that wrote nothing but its own
    stamp ran and decided something, which is a different claim from a gate that left no trace
    of running at all. `stamp_verdict` is what tells the two apart, and METHOD.md 3.6 requires
    cross-checking stamp freshness before calling a hook inert rather than merely silent.
    """
    def _lim(text: str) -> str:
        return f"{text} {tc_note}"

    def _no_content_change(run: HookRun) -> bool:
        return not any(not p.startswith(".automation-stamps/") for p in run.wrote_paths)

    findings: list[Finding] = []
    for (hook, event, matcher), rows in sorted(by_hook.items()):
        runs = [r for _, r in rows]
        if not runs or not all(
            not r.blocked and not r.injected and not r.stdout.strip()
            and _no_content_change(r) and not r.errored
            for r in runs
        ):
            continue

        verdicts = sorted({r.stamp_verdict for r in runs if r.stamp_verdict})
        if verdicts:
            findings.append(Finding(
                id=f"hook-inert-{hook}-{event}-{matcher}", check="INERTNESS",
                severity=Severity.INFO,
                summary=f"{hook} on {event} ({matcher}) had no observable effect on any "
                        f"of {len(runs)} fixtures, but stamped a verdict every run — it "
                        f"ran and decided: {', '.join(verdicts)}",
                snapshot=snapshot_id,
                evidence=(Evidence(file=f".automation-stamps/{Path(hook).stem}",
                                   excerpt=", ".join(verdicts)[:160]),),
                detail=f"{hook} {event} silent-but-stamped x{len(runs)} verdicts={verdicts}",
                limit=_lim(
                    "Silence across a fixture corpus is not death, and this is not even "
                    "silence: a stamped verdict means the gate ran and recorded WHY it "
                    "produced no other observable output — that is a decision, not "
                    "inertness. Still measured only against these fixtures: a gate that "
                    "decides 'skip' here may decide differently on real traffic."
                ),
            ))
        else:
            findings.append(Finding(
                id=f"hook-inert-{hook}-{event}-{matcher}", check="INERTNESS",
                severity=Severity.INFO,
                summary=f"{hook} on {event} ({matcher}) was silent across all "
                        f"{len(runs)} fixtures it would receive",
                snapshot=snapshot_id,
                evidence=(Evidence(file=f"scripts/hooks/{hook}",
                                   excerpt=", ".join(r.fixture for r in runs)[:160]),),
                detail=f"{hook} {event} silent x{len(runs)}",
                limit=_lim(
                    "Silence across a fixture corpus is not death. It means these fixtures "
                    "did not construct the case this gate looks for — which is a gap in the "
                    "corpus until a positive case is built and shown to fire."
                ),
            ))
    return findings


def _latency_findings(
    specs: list[HookSpec],
    by_hook: dict[tuple[str, str, str], list[tuple[Any, HookRun]]],
    snapshot_id: str,
    tc_note: str,
) -> list[Finding]:
    """3.5 — an unbounded gate on Stop can hang a turn indefinitely.

    Iterates the REGISTRATION list, not the runs: a registration the fixture corpus never
    delivers a single fixture to still has a declared timeout, and that configuration is a
    static property of settings.json rather than something that only exists once a fixture
    happens to exercise it.
    """
    def _lim(text: str) -> str:
        return f"{text} {tc_note}"

    findings: list[Finding] = []
    for spec in sorted(specs, key=lambda s: (Path(s.script).name, s.event, s.matcher or "*")):
        hook = Path(spec.script).name
        event, matcher = spec.event, spec.matcher or "*"
        rows = by_hook.get((hook, event, matcher), [])
        runs = [r for _, r in rows]
        limit_text = LIMIT_FIXTURES if runs else LIMIT_STATIC_SPEC

        if spec.timeout is None:
            if runs:
                slowest = max(runs, key=lambda r: r.duration_ms)
                detail = f"{hook} slowest fixture {slowest.duration_ms:.0f} ms, unbounded"
            else:
                detail = f"{hook} unbounded, no fixture in this corpus reaches it"
            findings.append(Finding(
                id=f"hook-no-timeout-{hook}-{event}-{matcher}", check="LATENCY",
                severity=Severity.COSTLY,
                summary=f"{hook} on {event} ({matcher}) has no configured timeout",
                snapshot=snapshot_id,
                evidence=(Evidence(file=".claude/settings.json", excerpt=spec.command[:160]),),
                detail=detail,
                limit=_lim(limit_text),
            ))
        elif not spec.async_rewake and spec.timeout > IMPLAUSIBLE_TIMEOUT_S:
            # `spec.timeout is None` is not the only unboundedness signal — a CONFIGURED
            # timeout can still be unbounded in every practical sense. graft-hooks.cjs is
            # wired at 8000s (2h13m) on Stop/PostToolUse/SessionStart with no asyncRewake and
            # passed the `is None` check clean.
            findings.append(Finding(
                id=f"hook-implausible-timeout-{hook}-{event}-{matcher}", check="LATENCY",
                severity=Severity.BROKEN,
                summary=f"{hook} on {event} ({matcher}) is configured with a "
                        f"{spec.timeout:.0f}s ({spec.timeout / 3600:.1f}h) timeout — a hang "
                        f"blocks the turn for that long",
                snapshot=snapshot_id,
                evidence=(Evidence(file=".claude/settings.json", excerpt=spec.command[:160]),),
                detail=f"{hook} {event} timeout={spec.timeout:.0f}s",
                limit=_lim(limit_text),
            ))

        # A declared timeout kills the subprocess AT the boundary (subprocess.run's own
        # `timeout=`), so `duration_ms` can never measure a true excess over it — only
        # teardown jitter. Report that the run was killed at the boundary instead of a number
        # that cannot mean what it looks like it means. Separately: the battery caps how long
        # IT will wait (`BATTERY_MAX_WAIT`) well below some declared timeouts (graft-hooks.cjs
        # at 8000s), so a run killed at the cap must never be reported as if it hit the
        # timeout being measured.
        timed_out_runs = [r for r in runs if r.timed_out]
        capped_runs = [r for r in timed_out_runs if r.capped]
        configured_runs = [r for r in timed_out_runs if not r.capped]
        if capped_runs:
            findings.append(Finding(
                id=f"hook-battery-capped-{hook}-{event}-{matcher}", check="LATENCY",
                severity=Severity.BROKEN,
                summary=f"{hook} on {event} ({matcher}) did not finish within the battery's "
                        f"own {BATTERY_MAX_WAIT:.0f}s wait cap on {len(capped_runs)}/"
                        f"{len(runs)} fixture(s) — its configured timeout is {spec.timeout}s, "
                        f"far longer than the battery will ever wait",
                snapshot=snapshot_id,
                evidence=(Evidence(file=f"scripts/hooks/{hook}",
                                   excerpt=", ".join(r.fixture for r in capped_runs)[:160]),),
                detail=f"{hook} {event} battery-cap-hit x{len(capped_runs)} "
                       f"cap={BATTERY_MAX_WAIT:.0f}s configured={spec.timeout}s",
                limit=_lim(
                    "Killed at the BATTERY's own wait cap, not at the hook's configured "
                    "timeout. Proves nothing about whether the hook would finish before its "
                    "declared boundary — only that it did not finish before a much shorter "
                    "one the battery imposes so one hung run cannot cost the whole battery "
                    "the hook's own declared wait."
                ),
            ))
        if configured_runs:
            findings.append(Finding(
                id=f"hook-timed-out-{hook}-{event}-{matcher}", check="LATENCY",
                severity=Severity.BROKEN,
                summary=f"{hook} on {event} ({matcher}) hit its {spec.timeout}s timeout and "
                        f"was killed on {len(configured_runs)}/{len(runs)} fixture(s)",
                snapshot=snapshot_id,
                evidence=(Evidence(file=f"scripts/hooks/{hook}",
                                   excerpt=", ".join(
                                       r.fixture for r in configured_runs)[:160]),),
                detail=f"{hook} {event} timed_out x{len(configured_runs)} at {spec.timeout}s",
                limit=_lim(
                    "Reports that the run was killed AT its configured timeout, never how far "
                    "past it the hook would have run: subprocess.run's own timeout kills at "
                    "exactly that boundary, so any true excess is censored by construction, "
                    "not measured."
                ),
            ))
    return findings


def _write_path_findings(
    by_hook,
    snapshot_id: str,
    tc_note: str,
    bearhug_owned: dict[str, str] | None = None,
    release: str | None = None,
) -> list[Finding]:
    """3.9 — one write, two delivery paths. Does the gate see both?

    ``bearhug_owned``/``release`` (from the project's own setup receipt, `hooks.ownership`) let
    the "never invoked on Bash" finding below name whether the hook that misses the write is
    Bear Hug's own installed hook and, when its subcommand's effect is documented in
    `_KNOWN_HOOK_EFFECTS`, state plainly what that effect is — so the finding reads as a
    concrete cost rather than the abstract "no behaviour to compare". No registration changes
    here: recognition and reporting only.

    ``bearhug_owned=None`` means no receipt was found at all — nothing to say about ownership.
    ``bearhug_owned={}`` is a DIFFERENT state: a receipt exists but lists no files (an empty
    install, or a schema change), which still lets a hook this dict doesn't mention be named as
    the project's own rather than falling silently back to the no-receipt wording. Collapsing
    the two into one falsy check would misreport an empty-but-present receipt as no evidence.
    """
    receipt_present = bearhug_owned is not None
    owned = bearhug_owned or {}
    findings: list[Finding] = []
    for (hook, event, matcher), rows in sorted(by_hook.items()):
        runs = {r.fixture: r for _, r in rows}
        sample_spec = rows[0][0]
        component = (
            owned.get(sample_spec.script)
            if sample_spec is not None and sample_spec.script
            else None
        )
        subcommand = (
            next(iter(hook_arguments(sample_spec.command)), None)
            if sample_spec is not None
            else None
        )
        effect = _KNOWN_HOOK_EFFECTS.get((hook, subcommand)) if subcommand else None
        # The id carries event+matcher (like hook-inert/hook-timed-out) so a hook registered on
        # two matchers — even two spellings of the same set, e.g. Edit|Write vs Write|Edit —
        # yields distinct finding ids instead of colliding when the report merges phases.
        scope = f"{event}-{matcher}"
        for visible, hidden, subject in WRITE_PATH_PAIRS:
            a, b = runs.get(visible), runs.get(hidden)
            if a and not b and not a.errored:
                # The gate was never handed the Bash payload — its matcher excludes the tool.
                # That is unreachability, not blindness, and naming it as blindness would put
                # the wrong mechanism in front of a reader. 2.13 owns it statically.
                if component is not None:
                    origin = f"Bear Hug's own {hook} ({component}, release {release})"
                elif receipt_present:
                    origin = f"{hook} — the project's own hook, not Bear Hug's"
                else:
                    origin = hook
                if effect:
                    cost = (
                        f" On the Edit-delivered write, {subcommand} {effect}; that does not "
                        f"happen for the identical write delivered through Bash."
                    )
                elif subcommand:
                    cost = (
                        f" What `{subcommand}` does for the Edit-delivered write is not "
                        f"catalogued here — see docs/OPERATING-MODES.md's hook table."
                    )
                else:
                    cost = ""
                findings.append(Finding(
                    id=f"hook-not-invoked-{hook}-{scope}-{hidden}", check="WRITE-PATH",
                    severity=Severity.INFO,
                    summary=f"{origin} is never invoked on the Bash delivery of "
                            f"{subject} — its matcher excludes the tool, so there is "
                            f"no behaviour to compare.{cost}",
                    snapshot=snapshot_id,
                    evidence=(Evidence(file=f"scripts/hooks/{hook}",
                                       excerpt=f"{visible} delivered, {hidden} not delivered"),),
                    detail=f"{hook} matcher excludes the Bash delivery of the same write"
                           + (f"; component={component}" if component is not None else ""),
                    limit=f"Not a defect in the gate's logic — the gate is correct and simply "
                          f"never runs on this path. MATCHER-REACHABILITY (2.13) is where the "
                          f"cost of that exclusion is measured against real traffic. Whether to "
                          f"widen the matcher is the project's decision, made with this cost "
                          f"named rather than left implicit. {tc_note}",
                ))
                continue
            if not a or not b or a.errored or b.errored or a.timed_out or b.timed_out:
                continue

            # THE NEGATIVE CONTROL, computed EXCLUDING the pair under test. Without that
            # exclusion this was vacuous: `others` used to be `rows` (the pair included), and
            # the BROKEN branch already requires `not b.blocked`, so `any(not r.blocked for r
            # in rows)` was ALWAYS true wherever the BROKEN branch could even fire — the
            # control never filtered anything there. joinkey-lint.py blocks all six Stop
            # fixtures with a byte-identical reason and has no code path that inspects a .go
            # write at all; excluding the pair, it still blocks every remaining fixture, so it
            # correctly fails this control. task-durability.py blocks 4 of 4 of the OTHER Stop
            # fixtures once this pair is excluded — every one of them contains an Edit
            # tool_use, which is what its block actually keys on (an empty task list plus ANY
            # edit, decision 0137), not the write path this pair measures — so it fails the
            # control too, even though `a` is blocked and `b` is not.
            others = [r for _, r in rows if r.fixture not in (visible, hidden)]
            discriminates = bool(others) and any(not r.blocked for r in others)

            if a.blocked and not b.blocked:
                if discriminates:
                    findings.append(Finding(
                        id=f"hook-write-path-blind-{hook}-{scope}-{hidden}", check="WRITE-PATH",
                        severity=Severity.BROKEN,
                        summary=f"{hook} acts on {subject} delivered directly but "
                                f"not when wrapped in Bash",
                        snapshot=snapshot_id,
                        evidence=(Evidence(file=f"scripts/hooks/{hook}",
                                           excerpt=f"{visible}=blocked, {hidden}=silent"),),
                        detail=f"{hook} sees the Edit path and misses the Bash path",
                        limit=f"Proves the gate treats two deliveries of the SAME write "
                              f"differently — excluding this pair, {hook} does NOT block the "
                              f"other {len(others)} fixture(s) uniformly, so the asymmetry is "
                              f"about the write and not about unconditional blocking. Does not "
                              f"prove how often the Bash path is taken in real work — Phase 4 "
                              f"measured that at 70% of tool calls. {tc_note}",
                    ))
                else:
                    # `a` blocked and `b` silent looks exactly like the Bash-blindness pattern
                    # this check exists to find — but the control cannot confirm it is ABOUT
                    # the write path rather than an unconditional (or differently-triggered)
                    # block that happens to land on `a` and not `b`. Reported rather than
                    # dropped: a control that cannot discriminate has not shown the gate is
                    # innocent, only that this corpus cannot yet tell the two apart —
                    # "silence is not absence" applies to the control itself here.
                    findings.append(Finding(
                        id=f"hook-write-path-unproven-{hook}-{scope}-{hidden}",
                        check="WRITE-PATH",
                        severity=Severity.INFO,
                        summary=f"{hook} blocks {visible} and not {hidden}, but the pair-"
                                f"excluding control cannot confirm the asymmetry is about "
                                f"the write path",
                        snapshot=snapshot_id,
                        evidence=(Evidence(
                            file=f"scripts/hooks/{hook}",
                            excerpt=f"{visible}=blocked, {hidden}=silent, control="
                                    f"{[r.fixture for r in others]}"),),
                        detail=f"{hook} write-path-uncontrolled {visible}=blocked "
                               f"{hidden}=silent",
                        limit=f"Excluding the pair, {hook} does not discriminate on the "
                              f"remaining {len(others)} fixture(s) either — it may block "
                              f"unconditionally on whatever actually triggers it, unrelated "
                              f"to which path carried the write. Neither BROKEN nor cleared: "
                              f"the control is inconclusive with the fixtures on hand, not "
                              f"passed. {tc_note}",
                    ))
            elif a.blocked and b.blocked and discriminates:
                findings.append(Finding(
                    id=f"hook-write-path-ok-{hook}-{scope}-{hidden}", check="WRITE-PATH",
                    severity=Severity.INFO,
                    summary=f"{hook} acts on {subject} through BOTH delivery paths",
                    snapshot=snapshot_id,
                    evidence=(Evidence(file=f"scripts/hooks/{hook}",
                                       excerpt=f"{visible}=blocked, {hidden}=blocked"),),
                    detail=f"{hook} sees both delivery paths",
                    limit=f"A positive result, and it required a negative control: excluding "
                          f"this pair, {hook} does NOT block the other {len(others)} "
                          f"fixture(s) uniformly, so its blocking on both write paths is about "
                          f"the write and not about the repo it was handed. {tc_note}",
                ))
    return findings


#: Tokens that mean a hook command is not one plain invocation — a shell test, `&&`/`||` chain,
#: or brace group. Such a command's identity stays unresolved (category (c) below) rather than
#: guessed at; this check parses one plain command, not shell control flow.
_SHELL_OPERATOR_TOKENS = frozenset({"&&", "||", ";", "|", "[", "]", "{", "}"})


def _bare_command_identity(command: str) -> tuple[str, tuple[str, ...]] | None:
    """(basename, arguments) for a plain ``path arg...`` hook command, or ``None``.

    Generalizes :func:`bearhug.lint.gates.resolve_script` to a bare external binary with no
    recognized script suffix — MemQ ships one — so two differently rooted invocations of the
    same tool can be compared by basename. Bails out to ``None`` on anything that is not a
    single plain invocation, per the module-level limit on arbitrary regex/path-alias
    equivalence: this does not try to solve those, only to say so.
    """
    cleaned = PROJECT_DIR_VAR.sub("", command.replace('"', " ").replace("'", " "))
    tokens = cleaned.split()
    if not tokens or any(token in _SHELL_OPERATOR_TOKENS for token in tokens):
        return None
    head, *rest = tokens
    return Path(head).name, tuple(token for token in rest if not token.startswith("$"))


def _overlap_scope(matcher: str | None) -> tuple[frozenset[str] | None, bool]:
    """(literal tool set, is_unmatched) for one hooks.json matcher.

    Kept apart from ``project_setup._merge_hooks``'s narrower "are these the identical matcher"
    question: a matcher can fail to resolve to a literal set for two different reasons — it
    legitimately means "every tool" (no matcher, or ``"*"``), or it is a regex/alias this check
    does not attempt to solve — and only the first of those is scope this check can reason
    about.
    """
    unmatched = matcher is None or not matcher.strip() or matcher.strip() == "*"
    return literal_tool_union(matcher), unmatched


def _overlap_findings(specs: list[HookSpec], snapshot_id: str, tc_note: str) -> list[Finding]:
    """3.10 OVERLAP — two registrations that fire on the same call, or an advisory look-alike.

    Registration-level, like LATENCY: reads the declared list, runs no fixture. Permanent
    backstop for project_setup's owned-hook dedup (a configuration merged before that fix shipped
    still reaches this) and for what that dedup does not attempt to fix: two genuinely different
    tools that
    each picked their own matcher spelling, or their own install path, for the same underlying
    scope.
    """

    def _lim(text: str) -> str:
        return f"{text} {tc_note}"

    findings: list[Finding] = []
    by_event: dict[str, list[HookSpec]] = {}
    for spec in specs:
        by_event.setdefault(spec.event, []).append(spec)

    reported_unresolved: set[tuple[str, int | None, int | None]] = set()
    for event, rows in sorted(by_event.items()):
        for index, a in enumerate(rows):
            for b in rows[index + 1:]:
                tools_a, unmatched_a = _overlap_scope(a.matcher)
                tools_b, unmatched_b = _overlap_scope(b.matcher)
                if unmatched_a and unmatched_b:
                    overlaps = True
                elif tools_a is not None and tools_b is not None:
                    overlaps = bool(tools_a & tools_b)
                else:
                    overlaps = False
                if not overlaps:
                    continue

                script_a, script_b = a.script, b.script
                args_a = tuple(hook_arguments(a.command))
                args_b = tuple(hook_arguments(b.command))
                if script_a is not None and script_a == script_b and args_a == args_b:
                    name = Path(script_a).name
                    findings.append(Finding(
                        id=f"duplicate_invocation-{event}-{name}-"
                           f"{a.matcher or '*'}-{b.matcher or '*'}",
                        check="OVERLAP",
                        severity=Severity.BROKEN,
                        summary=f"{name} is registered twice on {event}, under matchers "
                                f"{a.matcher or '*'!r} and {b.matcher or '*'!r}, which denote "
                                "the same tool scope — one matching call runs it twice",
                        snapshot=snapshot_id,
                        evidence=(Evidence(file=".claude/settings.json",
                                           excerpt=f"{a.matcher or '*'} / {b.matcher or '*'}"),),
                        detail=f"{name} {event} duplicate_invocation script={script_a} "
                               f"args={list(args_a)}",
                        limit=_lim(
                            "Reads the registration list only. Proves the same script and "
                            "arguments are wired twice under matchers that denote the same "
                            "tool set; does not measure how often the overlap fires, or what "
                            "running it twice costs."
                        ),
                    ))
                    continue

                bare_a = _bare_command_identity(a.command)
                bare_b = _bare_command_identity(b.command)
                if bare_a is not None and bare_b is not None:
                    if bare_a == bare_b and a.command != b.command:
                        basename = bare_a[0]
                        findings.append(Finding(
                            id=f"alternative_provider-{event}-{basename}-"
                               f"{a.matcher or '*'}-{b.matcher or '*'}",
                            check="OVERLAP",
                            severity=Severity.INFO,
                            summary=f"{basename} is invoked from two different paths on "
                                    f"{event} with the same arguments — advisory, likely two "
                                    "installs of the same tool",
                            snapshot=snapshot_id,
                            evidence=(Evidence(
                                file=".claude/settings.json",
                                excerpt=f"{a.command[:80]} / {b.command[:80]}"),),
                            detail=f"{basename} {event} alternative_provider "
                                   f"args={list(bare_a[1])}",
                            limit=_lim(
                                "Same basename and arguments from two different paths is not "
                                "proof of the same binary or behaviour — semantic path "
                                "aliasing stays unknown by design. Reported so an operator can "
                                "look, not because this check can tell the two apart."
                            ),
                        ))
                    continue

                for spec in (a, b):
                    if _bare_command_identity(spec.command) is not None:
                        continue
                    key = (event, spec.group_index, spec.position_in_group)
                    if key in reported_unresolved:
                        continue
                    reported_unresolved.add(key)
                    findings.append(Finding(
                        id=f"unresolved_command_identity-{event}-{spec.group_index}-"
                           f"{spec.position_in_group}",
                        check="OVERLAP",
                        severity=Severity.INFO,
                        summary=f"a command on {event} shares scope with another "
                                "registration and cannot be resolved to a script or a plain "
                                "invocation — reported as unknown, not compared",
                        snapshot=snapshot_id,
                        evidence=(Evidence(file=".claude/settings.json",
                                           excerpt=spec.command[:160]),),
                        detail=f"{event} unresolved_command_identity "
                               f"matcher={spec.matcher or '*'}",
                        limit=_lim(
                            "This check resolves one plain invocation, not shell conditionals "
                            "or control flow. Says only that the comparison could not be "
                            "made — never that the registrations do or do not duplicate each "
                            "other. Arbitrary regex equivalence and path aliasing stay unknown "
                            "the same way, by the same limit."
                        ),
                    ))
    return findings
