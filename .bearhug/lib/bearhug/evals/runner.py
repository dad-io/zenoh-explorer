"""Materialise and run headless Claude evaluations in disposable fixture repositories."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bearhug.evals.score import score_stream, stream_cost_usd
from bearhug.evals.spec import Scenario, resolve_scenario
from bearhug.hooks import assert_scratch, build_fixture_repo
from bearhug.paths import REPO_ROOT, RUNS_DIR

VARIANTS = ("full", "no-hooks", "hooks-only", "trimmed", "bare")

#: E03's candidate. Built by `bearhug eval trimmed`; absent, the trimmed variant refuses to run.
TRIMMED_CANDIDATE = REPO_ROOT / "evals" / "variants" / "trimmed" / "CLAUDE.md"

#: The sealed promotion package's hooks: `_bearhug/` and the adapter. `runtime="sealed"` lays them
#: over the snapshot's, so an eval measures the runtime Barracuda runs TODAY (project-based
#: telemetry, the current evaluators) rather than the one the snapshot captured. The first battery
#: ran the snapshot's 1.0.0, which wrote 225 lab records into the home telemetry store.
SEALED_PACKAGE_HOOKS = REPO_ROOT / "patches" / "promotion-package" / "scripts" / "hooks"
RUNTIMES = ("snapshot", "sealed")


def _tree_sha256(root: Path) -> str:
    """bearhug-runtime-sha256/1 over an installed `_bearhug` tree (the runtime's own identity)."""
    import hashlib
    import unicodedata

    digest = hashlib.sha256()
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        and path.name != ".DS_Store"
    )
    for path in files:
        relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        blob = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(len(blob)).encode("ascii"))
        digest.update(b"\x00")
        digest.update(blob)
    return digest.hexdigest()

#: Environment variables that mark THIS process as a Claude Code session. A headless `claude -p`
#: started from inside a session inherits them and behaves as a child of it (measured 2026-09-02:
#: the same one-word prompt cost 0.34 USD with them and 0.06 USD without). An eval must run as
#: its own top-level task, so they are dropped.
_PARENT_SESSION_MARKERS = (
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_HOST_SESSION_ID",
)


def child_env() -> dict[str, str]:
    """This process's environment without the markers that would make a nested `claude` a child
    of this session. Authentication is untouched: the CLI resolves Sam's login on its own."""
    import os

    return {k: v for k, v in os.environ.items() if k not in _PARENT_SESSION_MARKERS}

# E10, ruled by Sam on 2026-09-01. The named battery is deliberately not configurable: changing
# any value makes it a different paid experiment that needs a new ruling.
APPROVED_BATTERY_VARIANTS = ("full",)
APPROVED_BATTERY_SCENARIOS = ("S1", "S2", "S3", "S4", "S5", "S6")
APPROVED_BATTERY_REPEAT = 3
APPROVED_BATTERY_MAX_RUN_USD = 10.0
APPROVED_BATTERY_TOTAL_USD = 50.0
APPROVED_BATTERY_BY = "Sam, 2026-09-01"


@dataclass(frozen=True, slots=True)
class EvalResult:
    run_id: str
    snapshot_id: str
    variant: str
    scenario: str
    exit_code: int
    duration_ms: float
    cost_usd: float | None
    passed: bool
    reason: str
    stream: str
    stderr: str
    #: E03: sha256 of the CLAUDE.md actually installed for this run, so a candidate is pinned
    #: per run and cannot change under a battery unnoticed.
    variant_sha256: str | None = None
    #: which runtime the fixture ran: the snapshot's own, or the sealed package laid over it
    runtime: str = "snapshot"
    runtime_sha256: str | None = None


def _copy_if_present(source: Path, destination: Path) -> None:
    if source.is_dir():
        symlinks = [path for path in source.rglob("*") if path.is_symlink()]
        if symlinks:
            names = ", ".join(str(path) for path in symlinks[:3])
            raise ValueError(f"variant source contains symlink(s): {names}")
        shutil.copytree(source, destination, dirs_exist_ok=True)


def install_variant(
    snapshot: Path, repo: Path, variant: str, *, runtime: str = "snapshot"
) -> str | None:
    """Install only snapshot harness files into scratch; never read product source.

    With `runtime="sealed"` the sealed package's `_bearhug/` and adapter replace the snapshot's
    after the hooks are installed; returns that tree's sha256 (None for the snapshot's runtime).
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; choose {', '.join(VARIANTS)}")
    if runtime not in RUNTIMES:
        raise ValueError(f"unknown runtime {runtime!r}; choose {', '.join(RUNTIMES)}")
    project = snapshot / "project"
    if variant in {"full", "no-hooks"}:
        source = project / "CLAUDE.md"
        if not source.is_file():
            raise ValueError(f"snapshot has no project/CLAUDE.md: {snapshot}")
        if source.is_symlink():
            raise ValueError(f"variant source may not be a symlink: {source}")
        shutil.copy2(source, repo / "CLAUDE.md")
    elif variant == "trimmed":
        source = TRIMMED_CANDIDATE
        if not source.is_file():
            raise ValueError(
                "trimmed variant is not built: supply evals/variants/trimmed/CLAUDE.md "
                "after its prime-eval before/after is accepted"
            )
        if source.is_symlink():
            raise ValueError(f"variant source may not be a symlink: {source}")
        shutil.copy2(source, repo / "CLAUDE.md")
        # E03's detail files are what the candidate's section stubs point at
        _copy_if_present(source.parent / "detail", repo / "detail")
    else:
        (repo / "CLAUDE.md").write_text(
            "# Eval control\n\nNo project-specific instructions are installed.\n", encoding="utf-8"
        )

    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    if variant in {"full", "hooks-only", "trimmed"}:
        source_settings = project / ".claude" / "settings.json"
        if not source_settings.is_file():
            raise ValueError(f"snapshot has no project/.claude/settings.json: {snapshot}")
        if source_settings.is_symlink():
            raise ValueError(f"variant source may not be a symlink: {source_settings}")
        shutil.copy2(source_settings, settings)
        _copy_if_present(project / "scripts" / "hooks", repo / "scripts" / "hooks")
        _copy_if_present(project / ".claude" / "helpers", repo / ".claude" / "helpers")
    else:
        settings.write_text('{"hooks": {}}\n', encoding="utf-8")
    if runtime == "sealed" and variant in {"full", "hooks-only", "trimmed"}:
        sealed_runtime = SEALED_PACKAGE_HOOKS / "_bearhug"
        adapter = SEALED_PACKAGE_HOOKS / "stop-coordinator.py"
        if not sealed_runtime.is_dir() or not adapter.is_file():
            raise ValueError(f"no sealed package hooks at {SEALED_PACKAGE_HOOKS}")
        target = repo / "scripts" / "hooks" / "_bearhug"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(sealed_runtime, target)
        shutil.copyfile(adapter, repo / "scripts" / "hooks" / "stop-coordinator.py")
        return _tree_sha256(target)
    return None


def _command(
    executable: str,
    scenario: Scenario,
    settings: Path,
    *,
    max_budget_usd: float,
) -> list[str]:
    return [
        executable,
        "-p",
        scenario.prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--setting-sources",
        "project",
        "--settings",
        str(settings),
        "--permission-mode",
        "acceptEdits",
        "--max-budget-usd",
        str(max_budget_usd),
        "--no-session-persistence",
        "--model",
        "opus",
        "--effort",
        "high",
    ]


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _terminate_process_group(process: subprocess.Popen) -> None:
    """Terminate a timed-out eval and reap descendants before returning to the caller."""
    if os.name == "posix":
        with suppress(OSError, ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:  # pragma: no cover - the lab runs on POSIX; retain a bounded fallback for Windows.
        with suppress(OSError):
            process.terminate()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    # The leader can exit on TERM while a descendant ignores it. Kill the remaining group
    # regardless of the leader's wait result before draining pipes or observing the fixture.
    if os.name == "posix":
        with suppress(OSError, ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    elif process.poll() is None:  # pragma: no cover
        with suppress(OSError):
            process.kill()
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=1)


def _run_process_group(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Run the real eval subprocess with process-group timeout custody."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    try:
        _unused_stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        _unused_stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command, timeout, stderr=stderr or exc.stderr
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stderr=stderr)


def run_eval(
    snapshot: Path,
    *,
    snapshot_id: str,
    variant: str,
    scenario_name: str,
    max_budget_usd: float,
    runs_dir: Path = RUNS_DIR,
    executable: str = "claude",
    executor: Callable[..., Any] = subprocess.run,
    runtime: str = "snapshot",
) -> EvalResult:
    """Run one paid evaluation. Callers own repetition so every run has a separate repo."""
    if max_budget_usd <= 0:
        raise ValueError("--max-budget-usd must be greater than zero")
    scenario = resolve_scenario(scenario_name)
    if shutil.which(executable) is None and executor is subprocess.run:
        raise RuntimeError(f"{executable!r} is not on PATH")

    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{variant}-{scenario.id}-{uuid.uuid4().hex[:8]}"
    run_root = runs_dir / run_id
    repo = assert_scratch(run_root / "repo")
    build_fixture_repo(repo)
    runtime_sha256 = install_variant(snapshot, repo, variant, runtime=runtime)
    # Re-check after installation so a candidate variant cannot introduce an escaping symlink.
    assert_scratch(repo)
    variant_sha256 = hashlib.sha256((repo / "CLAUDE.md").read_bytes()).hexdigest()

    stream_path = run_root / "stream.jsonl"
    stderr_path = run_root / "stderr.txt"
    started = time.monotonic()
    with stream_path.open("w", encoding="utf-8") as stream:
        try:
            command = _command(
                executable,
                scenario,
                repo / ".claude" / "settings.json",
                max_budget_usd=max_budget_usd,
            )
            if executor is subprocess.run:
                completed = _run_process_group(
                    command,
                    cwd=repo,
                    env=child_env(),
                    stdout=stream,
                    timeout=900,
                )
            else:
                completed = executor(
                    command,
                    cwd=repo,
                    env=child_env(),
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=900,
                )
            exit_code = completed.returncode
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stderr = f"eval timed out after {exc.timeout} seconds"
        except OSError as exc:
            exit_code = 127
            stderr = f"eval launch failed: {exc}"
    duration_ms = (time.monotonic() - started) * 1000
    stderr_path.write_text(stderr, encoding="utf-8")
    score = score_stream(stream_path, scenario, exit_code=exit_code)
    result = EvalResult(
        run_id=run_id,
        snapshot_id=snapshot_id,
        variant=variant,
        scenario=scenario.id,
        exit_code=exit_code,
        duration_ms=duration_ms,
        cost_usd=stream_cost_usd(stream_path),
        passed=score.passed,
        reason=score.reason,
        stream=_display_path(stream_path),
        stderr=_display_path(stderr_path),
        variant_sha256=variant_sha256,
        runtime=runtime,
        runtime_sha256=runtime_sha256,
    )
    (run_root / "result.json").write_text(
        json.dumps(asdict(result), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


# --- E11: the approved matrix, run exactly, stopped on budget or systemic failure ---------------

EXTERNAL_VALIDITY = (
    "Headless evidence does not establish interactive-session effectiveness: a `claude -p` run in "
    "a disposable fixture has no human in the loop, no prior context and no stakes, so a pass rate "
    "here bounds what the harness can enforce, not what it does enforce in Sam's sessions."
)


@dataclass(slots=True)
class BatteryResult:
    battery_id: str
    approved_by: str
    variants: tuple[str, ...]
    scenarios: tuple[str, ...]
    repeat: int
    max_budget_usd: float
    total_budget_usd: float
    results: list[dict[str, Any]]
    spent_usd: float
    stop_reason: str
    manifest: str
    runtime: str = "snapshot"

    @property
    def planned_runs(self) -> int:
        return len(self.variants) * len(self.scenarios) * self.repeat

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "battery_id": self.battery_id,
            "approved_by": self.approved_by,
            "ruling": "E10 — Sam, 2026-09-01: exactly this matrix; stop on cumulative budget, "
            "any run over the per-run cap, or a runner error",
            "matrix": {
                "variants": list(self.variants), "scenarios": list(self.scenarios),
                "repeat": self.repeat,
            },
            "planned_runs": self.planned_runs,
            "completed_runs": len(self.results),
            "max_budget_usd": self.max_budget_usd,
            "total_budget_usd": self.total_budget_usd,
            "spent_usd": self.spent_usd,
            "stop_reason": self.stop_reason,
            "budget_rule": "before each run: spent + (mean observed cost, or the per-run cap while "
            "no cost has been observed; an unreported cost counts as the cap) must not exceed the "
            "total",
            "external_validity": EXTERNAL_VALIDITY,
            "runtime": self.runtime,
            "results": self.results,
        }


def run_battery(
    snapshot: Path,
    *,
    snapshot_id: str,
    variants: tuple[str, ...],
    scenarios: tuple[str, ...],
    repeat: int,
    max_budget_usd: float,
    total_budget_usd: float,
    approved_by: str,
    runs_dir: Path = RUNS_DIR,
    executable: str = "claude",
    executor: Callable[..., Any] = subprocess.run,
    runtime: str = "snapshot",
) -> BatteryResult:
    """Run the approved matrix repetition-major (every scenario once before any is repeated), so
    an early stop still covers the whole matrix at least partially and evenly."""
    if not approved_by or not approved_by.strip():
        raise ValueError("a battery is a paid action gated by E10: --approved-by is required")
    if repeat < 1 or not variants or not scenarios:
        raise ValueError("a battery needs at least one variant, one scenario and one repetition")
    if max_budget_usd <= 0 or total_budget_usd <= 0:
        raise ValueError("budgets must be greater than zero")

    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    battery_id = f"battery-{stamp}-{uuid.uuid4().hex[:6]}"
    manifest_path = runs_dir / battery_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    battery = BatteryResult(
        battery_id=battery_id, approved_by=approved_by.strip(), variants=tuple(variants),
        scenarios=tuple(scenarios), repeat=repeat, max_budget_usd=max_budget_usd,
        total_budget_usd=total_budget_usd, results=[], spent_usd=0.0, stop_reason="running",
        manifest=_display_path(manifest_path), runtime=runtime,
    )

    def persist() -> None:
        manifest_path.write_text(
            json.dumps(battery.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    observed: list[float] = []
    persist()
    for _rep in range(repeat):
        for variant in variants:
            for scenario in scenarios:
                projected_next = sum(observed) / len(observed) if observed else max_budget_usd
                if battery.spent_usd + projected_next > total_budget_usd:
                    battery.stop_reason = "total_budget"
                    persist()
                    return battery
                result = run_eval(
                    snapshot, snapshot_id=snapshot_id, variant=variant, scenario_name=scenario,
                    max_budget_usd=max_budget_usd, runs_dir=runs_dir, executable=executable,
                    executor=executor, runtime=runtime,
                )
                row = asdict(result)
                battery.results.append(row)
                cost = result.cost_usd if result.cost_usd is not None else max_budget_usd
                observed.append(cost)
                battery.spent_usd += cost
                persist()
                if result.exit_code == 127:
                    battery.stop_reason = "launch_failure"
                    persist()
                    return battery
                if result.exit_code == 124:
                    battery.stop_reason = "timeout"
                    persist()
                    return battery
                if result.cost_usd is not None and result.cost_usd > max_budget_usd:
                    battery.stop_reason = "per_run_cap"
                    persist()
                    return battery
    battery.stop_reason = "completed"
    persist()
    return battery


def run_approved_battery(
    snapshot: Path,
    *,
    snapshot_id: str,
    runs_dir: Path = RUNS_DIR,
    executable: str = "claude",
    executor: Callable[..., Any] = subprocess.run,
) -> BatteryResult:
    """Run exactly E10's ruled matrix; a different matrix requires a different entry point."""
    return run_battery(
        snapshot,
        snapshot_id=snapshot_id,
        variants=APPROVED_BATTERY_VARIANTS,
        scenarios=APPROVED_BATTERY_SCENARIOS,
        repeat=APPROVED_BATTERY_REPEAT,
        max_budget_usd=APPROVED_BATTERY_MAX_RUN_USD,
        total_budget_usd=APPROVED_BATTERY_TOTAL_USD,
        approved_by=APPROVED_BATTERY_BY,
        runs_dir=runs_dir,
        executable=executable,
        executor=executor,
    )



#: Sam, 2026-09-02: a clean A/B on the sealed runtime — full and trimmed, 18 runs each, the same
#: $10-per-run / $50-per-battery caps as E10. Two batteries, one per arm; nothing else is approved.
SEALED_BATTERY_ARMS = ("full", "trimmed")
SEALED_BATTERY_BY = "Sam, 2026-09-02 (sealed runtime, full and trimmed)"


def run_sealed_battery(
    snapshot: Path,
    *,
    snapshot_id: str,
    arm: str,
    runs_dir: Path = RUNS_DIR,
    executable: str = "claude",
    executor: Callable[..., Any] = subprocess.run,
) -> BatteryResult:
    """One arm of the 2026-09-02 ruling on the sealed runtime; any other arm is not approved."""
    if arm not in SEALED_BATTERY_ARMS:
        raise ValueError(
            f"arm {arm!r} is not approved for the sealed battery; approved: "
            f"{', '.join(SEALED_BATTERY_ARMS)}"
        )
    return run_battery(
        snapshot,
        snapshot_id=snapshot_id,
        variants=(arm,),
        scenarios=APPROVED_BATTERY_SCENARIOS,
        repeat=APPROVED_BATTERY_REPEAT,
        max_budget_usd=APPROVED_BATTERY_MAX_RUN_USD,
        total_budget_usd=APPROVED_BATTERY_TOTAL_USD,
        approved_by=SEALED_BATTERY_BY,
        runs_dir=runs_dir,
        executable=executable,
        executor=executor,
        runtime="sealed",
    )


__all__ = [
    "APPROVED_BATTERY_BY", "APPROVED_BATTERY_MAX_RUN_USD", "APPROVED_BATTERY_REPEAT",
    "APPROVED_BATTERY_SCENARIOS", "APPROVED_BATTERY_TOTAL_USD", "APPROVED_BATTERY_VARIANTS",
    "BatteryResult", "EXTERNAL_VALIDITY", "EvalResult", "SEALED_BATTERY_ARMS",
    "SEALED_BATTERY_BY", "VARIANTS", "child_env", "install_variant", "run_approved_battery",
    "run_battery", "run_eval", "run_sealed_battery",
]
