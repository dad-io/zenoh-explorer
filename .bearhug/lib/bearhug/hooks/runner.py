"""3.2 — execute a SNAPSHOTTED hook against a fixture event, and record exactly what it did.

Never the live hook and never the live repo: the script comes out of a snapshot so the run can
be re-derived, and `CLAUDE_PROJECT_DIR` points at a disposable copy so a gate that writes
writes there. Both are enforced, not assumed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.hooks.fixtures import EventFixture, write_transcript
from bearhug.hooks.scratch import assert_scratch

#: stderr shapes that mean the interpreter never got as far as the hook's own logic.
_LAUNCH_FAILURE = re.compile(
    r"can't open file"
    r"|syntax error near unexpected token"      # bash parse failure, which also exits 2
    r"|unexpected EOF while looking for"
    r"|(?:Syntax|Indentation|Tab)Error"         # python parse failures are not all SyntaxError
    r"|ModuleNotFoundError|Cannot find module"  # python and node
    r"|: not found$",
    re.MULTILINE,
)

#: Ambiguous on their own — a RUNNING gate prints these too. Only a launch failure when the
#: hook produced nothing else at all.
_AMBIGUOUS_FAILURE = re.compile(r"No such file or directory|command not found")

#: A hook with no configured timeout still cannot hang the battery.
DEFAULT_TIMEOUT = 30.0

#: The longest the BATTERY itself will ever wait on one run, independent of what a hook is
#: configured with. `graft-hooks.cjs` is wired at 8000s (2h13m) on three events; waiting out a
#: hung `node` at its declared timeout, x8 runs, costs the battery 2h13m per hang. The declared
#: value is still what gets reported as the LATENCY finding — this only bounds how long the
#: battery will sit before killing the process and recording that the cap, not the hook's own
#: timeout, is what fired.
BATTERY_MAX_WAIT = 60.0

#: External binaries some gates in the fleet shell out to. A missing one gives an rc the gate
#: cannot tell apart from a real failure — `go vet`/`gofmt` absent gives go-postedit.sh rc 127,
#: which it reports as "go vet FAILED" byte-identically to a genuine vet failure.
TOOLCHAIN_TOOLS: tuple[str, ...] = ("go", "gofmt", "jq", "node", "dlv", "goimports")

#: Fingerprinting excludes only `.git/`, the fixture repo's own machinery. `.automation-stamps/`
#: USED to be excluded too, on the rationale that nearly every wired hook calls stamp.sh
#: unconditionally, so counting it would make `silent` trivially false for almost every gate.
#: Measured: false. Toggling the exclusion off changes exactly one of seven INERTNESS findings
#: — most gates that stamp do so only on code paths these fixtures do not reach. What it buys
#: back is real: a gate that crashes into a bare `except`, stamps `CRASH-NameError`, and exits 0
#: previously fingerprinted identically to a gate that never ran at all.
_FINGERPRINT_EXCLUDE = {".git"}


@dataclass(frozen=True, slots=True)
class HookRun:
    """One hook, one fixture, one observation."""

    hook: str
    fixture: str
    event: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    errored: bool = False  # the hook never ran: missing file, no interpreter, exec failure
    wrote_paths: tuple[str, ...] = ()  # files added/removed/changed under project_dir this run
    #: The verdict recorded under `.automation-stamps/<script-stem>` right after this run, if
    #: any — best-effort: it only resolves the common `stamp(name, verdict)` convention where
    #: `name` is the script's own stem, so `memex-hook.sh post-edit` (which stamps
    #: `memex-post-edit`) is not resolved here.
    stamp_verdict: str | None = None
    #: True when the battery capped the wait below the hook's OWN configured timeout
    #: (`BATTERY_MAX_WAIT`). A `timed_out` run with `capped=True` was killed at the battery's
    #: safety boundary, not at the boundary being measured.
    capped: bool = False

    @property
    def decision(self) -> dict[str, Any] | None:
        """The hook's JSON decision, if stdout carries one."""
        text = self.stdout.strip()
        if not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @property
    def blocked(self) -> bool:
        """Did this run actually stop the turn?

        Two mechanisms, and only two: a decision key on stdout, or exit code 2. Exit 1 is a
        non-blocking error the turn ignores — `joinkey-lint.py` used exit 1 with `--check` and
        blocked nothing at every Stop for weeks before that was found.

        A hook that never RAN is not a hook that blocked. `python3` exits 2 when it cannot open
        the file it was handed, so a mis-resolved script path reported every gate in the battery
        as blocking on its first run. `errored` separates the two.
        """
        if self.errored or self.timed_out:
            return False
        decision = self.decision or {}
        if decision.get("decision") == "block":
            return True
        if decision.get("permissionDecision") in {"deny", "ask"}:
            return True
        specific = decision.get("hookSpecificOutput") or {}
        if isinstance(specific, dict) and specific.get("permissionDecision") in {"deny", "ask"}:
            return True
        # `continue: false` halts the turn outright — the strongest stop there is, and the one
        # the first version of this classifier ignored while listing it as a legal key.
        if decision.get("continue") is False:
            return True
        return self.exit_code == 2

    @property
    def injected(self) -> str | None:
        specific = (self.decision or {}).get("hookSpecificOutput") or {}
        if isinstance(specific, dict):
            return specific.get("additionalContext")
        return None

    @property
    def silent(self) -> bool:
        """No block, no injected context, no stdout, AND no filesystem side effect.

        `go-postedit.sh` runs `gofmt -w` and rewrites a source file in place with no stdout at
        all — the old definition recorded that as "did nothing". A gate that mutated the repo
        it was handed is not silent, whatever its stdout says.
        """
        return (
            not self.blocked
            and not self.injected
            and not self.stdout.strip()
            and not self.wrote_paths
        )


def _terminate_process_group(process: subprocess.Popen) -> None:
    """Terminate a timed-out hook and reap its process group before observing the fixture."""
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


def run_hook(
    script: Path,
    fixture: EventFixture,
    *,
    project_dir: Path,
    timeout: float | None = None,
    interpreter: list[str] | None = None,
    arguments: list[str] | None = None,
) -> HookRun:
    """Run one snapshotted hook against one fixture event.

    ``arguments`` replays what settings.json configures after the script path. Omitting them
    runs a different gate: `joinkey-lint.py --check` blocks where the bare script only reports.
    """
    project_dir = assert_scratch(project_dir)
    # Absolute: the hook runs with cwd inside the fixture repo, where a repo-relative path to
    # a snapshotted script resolves to nothing.
    script = Path(script).expanduser().resolve()
    if not script.is_file():
        return HookRun(
            hook=script.name, fixture=fixture.name, event=fixture.event, exit_code=-1,
            stdout="", stderr=f"no such hook script: {script}", duration_ms=0.0, errored=True,
        )
    # The transcript filename IS a session identity: task-durability.py does
    # `os.path.basename(transcript_path)` to find `~/.claude/tasks/<session_id>/`. A hardcoded
    # literal here disagreed with both the `session_id` fixture.stdin() puts on the wire and
    # the `sessionId` embedded in the transcript's own records, so that lookup resolved to a
    # path that could never exist — the task store was structurally empty by construction, not
    # by measurement. `fixture.stdin()` doesn't touch disk, so probing it costs nothing; use
    # whatever identity the fixture actually reports rather than inventing one here, so the
    # three agree regardless of what the fixture does.
    probed = json.loads(fixture.stdin(transcript_path=Path("unused"), cwd=project_dir))
    session_id = probed["session_id"]
    transcript = project_dir / f"{session_id}.jsonl"
    write_transcript(fixture.transcript, transcript)
    # A FUTURE task-store fixture, to exercise the gate's non-empty state: override the `HOME`
    # env var passed to the subprocess below to point at a disposable directory holding
    # `.claude/tasks/<session_id>/<task-id>.json`, and never plant anything under the real
    # `~/.claude` — that stays outside the write boundary. Not done here; this fix only makes
    # the identity real, which is what the empty-state measurement needed to be honest.

    argv = [*(interpreter or _interpreter_for(script)), str(script), *(arguments or [])]
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project_dir),
        "BEARHUG_FIXTURE": fixture.name,
    }
    payload = fixture.stdin(transcript_path=transcript, cwd=project_dir)

    # `timeout or DEFAULT_TIMEOUT` would silently turn a declared 0 into 30; a caller that
    # means "no slack at all" gets rewritten to "30s of slack" with no signal that happened.
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    # The battery will not sit out a hook's own declared timeout when that timeout is itself
    # implausible — `graft-hooks.cjs` at 8000s x8 registrations would cost 2h13m per hang. Cap
    # the ACTUAL wait separately from the value being measured, and remember when the cap
    # bit rather than the declared timeout, so a `timed_out` run doesn't misreport which
    # boundary it was actually killed at.
    wait = min(effective_timeout, BATTERY_MAX_WAIT)
    capped = wait < effective_timeout

    before = _fingerprint(project_dir)
    started = time.perf_counter()
    timed_out = False
    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=project_dir,
            env=env,
            text=True,
            start_new_session=(os.name == "posix"),
        )
        out, err = process.communicate(payload, timeout=wait)
        code = process.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        if process is not None:
            _terminate_process_group(process)
            out, err = process.communicate()
        else:  # pragma: no cover - TimeoutExpired is raised by communicate after Popen.
            out, err = None, None
        code = -1
        out = out or exc.stdout or ""
        err = err or exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
    except OSError as exc:
        if process is not None:
            _terminate_process_group(process)
        duration = (time.perf_counter() - started) * 1000
        return HookRun(
            hook=script.name, fixture=fixture.name, event=fixture.event, exit_code=-1,
            stdout="", stderr=f"could not execute: {exc}", duration_ms=round(duration, 1),
            errored=True, wrote_paths=_diff_fingerprints(before, _fingerprint(project_dir)),
            stamp_verdict=_stamp_verdict(project_dir, script.stem),
        )
    duration = (time.perf_counter() - started) * 1000
    wrote_paths = _diff_fingerprints(before, _fingerprint(project_dir))

    # An interpreter that could not open or parse the script is not a gate firing.
    stderr_text = err or ""
    launch_failed = code != 0 and not out.strip() and (
        bool(_LAUNCH_FAILURE.search(stderr_text))
        # A gate that blocks while naming a missing file is a gate that RAN. Only treat the
        # ambiguous strings as a launch failure when they are the entire output.
        or (bool(_AMBIGUOUS_FAILURE.search(stderr_text)) and len(stderr_text.strip()) < 200)
    )
    return HookRun(
        hook=script.name, fixture=fixture.name, event=fixture.event,
        exit_code=code, stdout=out, stderr=err, duration_ms=round(duration, 1),
        timed_out=timed_out, errored=bool(launch_failed), wrote_paths=wrote_paths,
        stamp_verdict=_stamp_verdict(project_dir, script.stem), capped=capped,
    )


def _stamp_verdict(project_dir: Path, stem: str) -> str | None:
    """The verdict this run recorded under ``.automation-stamps/<stem>``, if any.

    `stamp.sh`'s content convention is ``"<epoch> <verdict>"``, one file per automation. Read
    AFTER the run: with `.automation-stamps/` no longer excluded from the fingerprint, a fresh
    stamp is exactly the signal that separates a gate that ran and decided nothing observable
    from one that never ran at all.
    """
    path = project_dir / ".automation-stamps" / stem
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    parts = text.split(None, 1)
    return parts[1] if len(parts) == 2 else None


def _interpreter_for(script: Path) -> list[str]:
    suffix = script.suffix
    if suffix == ".py":
        return ["python3"]
    if suffix == ".sh":
        return ["bash"]
    if suffix in {".cjs", ".js"}:
        return ["node"]
    return []


def _fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """(mtime_ns, size) per file under ``root``, excluding `.git/` and `.automation-stamps/`.

    Cheap on the fixture repo this runs against: about 15 files.
    """
    out: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in _FINGERPRINT_EXCLUDE:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        out[str(rel)] = (st.st_mtime_ns, st.st_size)
    return out


def _diff_fingerprints(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> tuple[str, ...]:
    """Paths added, removed, or changed in content between two fingerprints."""
    changed = {p for p in after if before.get(p) != after.get(p)}
    changed |= set(before) - set(after)
    return tuple(sorted(changed))


def toolchain_snapshot() -> dict[str, bool]:
    """Which of the fleet's external binaries were resolvable on PATH for this run."""
    return {tool: shutil.which(tool) is not None for tool in TOOLCHAIN_TOOLS}


def toolchain_note(snapshot: dict[str, bool] | None = None) -> str:
    """A one-line provenance string to fold into a Finding's ``limit``.

    So a reader six months from now can tell a real `go vet` failure from a missing `go`
    binary — right now both come back from go-postedit.sh as byte-identical "go vet FAILED".
    """
    snapshot = toolchain_snapshot() if snapshot is None else snapshot
    rendered = ", ".join(
        f"{tool}={'present' if present else 'MISSING'}" for tool, present in snapshot.items()
    )
    if all(snapshot.values()):
        return f"Toolchain at run time: {rendered}."
    return (
        f"Toolchain at run time: {rendered}. A gate that shells out to a missing binary "
        f"(e.g. `go vet`/`gofmt` for go-postedit.sh) fails for a reason that has nothing to "
        f"do with the code under test, and some gates report that byte-identically to a real "
        f"failure."
    )
