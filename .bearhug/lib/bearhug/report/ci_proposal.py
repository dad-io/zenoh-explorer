"""H05 — a Barracuda CI hook-test proposal: one bounded runner, emitted as a patch with rollback.

The Round-9 return recorded the eight `scripts/hooks/*_test.py` suites passing when run ONE AT A
TIME from the repo root, and recorded that `python3 -m unittest discover -s scripts/hooks` is NOT
green (a module-level ImportError in one suite). So the runner proposed here runs each suite as its
own process, bounds each with a timeout, needs nothing from the product tree, and fails the job on
any nonzero exit. It is stdlib Python because macOS ships no `timeout`. Bear Hug proposes the file;
a Barracuda-owned session decides where it runs (CI platform integration is theirs).
"""

from __future__ import annotations

import json
from pathlib import Path

from bearhug.paths import PATCHES_DIR, assert_writable

RUNNER_PATH = "scripts/hooks/ci-hook-battery.py"

RUNNER_SOURCE = r'''#!/usr/bin/env python3
"""ci-hook-battery — run every scripts/hooks/*_test.py suite as its own bounded process.

Proposed by bear-hug (H05). Why one process per suite: `python3 -m unittest discover` over this
directory is not green (a module-level import in one suite fails under discovery) while every suite
passes on its own — the shape the Round-9 return recorded. Why a per-suite timeout: a hung gate
test must fail the job, not park it. No product source is imported: each suite is a subprocess
run from the repository root, exactly as a person runs it.

Exit status: 0 only if every suite exited 0 within its bound. Anything else is nonzero, and the
summary names each suite with its exit code and duration.
"""
import os
import subprocess
import sys
import time

HOOKS_DIR = os.path.join("scripts", "hooks")
PER_SUITE_TIMEOUT_S = float(os.environ.get("CI_HOOK_SUITE_TIMEOUT_S", "120"))


def main() -> int:
    if not os.path.isdir(HOOKS_DIR):
        print(f"ci-hook-battery: no {HOOKS_DIR}/ here; run from the repository root",
              file=sys.stderr)
        return 2
    suites = sorted(
        name for name in os.listdir(HOOKS_DIR)
        if name.endswith("_test.py") and os.path.isfile(os.path.join(HOOKS_DIR, name))
    )
    if not suites:
        print("ci-hook-battery: no *_test.py suites found", file=sys.stderr)
        return 2
    failures = 0
    for name in suites:
        path = os.path.join(HOOKS_DIR, name)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, path], capture_output=True, text=True,
                timeout=PER_SUITE_TIMEOUT_S, check=False,
            )
            code = completed.returncode
            verdict = "ok" if code == 0 else f"exit {code}"
            tail = (completed.stderr or completed.stdout).strip().splitlines()[-1:] if code else []
        except subprocess.TimeoutExpired:
            code = 124
            verdict = f"TIMEOUT after {PER_SUITE_TIMEOUT_S:.0f}s"
            tail = []
        elapsed = time.monotonic() - started
        failures += int(code != 0)
        print(f"{'PASS' if code == 0 else 'FAIL'} {path} {verdict} {elapsed:.1f}s"
              + (f" — {tail[0]}" if tail else ""))
    print(f"ci-hook-battery: {len(suites) - failures}/{len(suites)} suites passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
'''

ROLLBACK = (
    "Rollback: delete scripts/hooks/ci-hook-battery.py and remove the CI step that calls it. The "
    "file has no consumer inside the repository and writes nothing, so removal leaves no state."
)


def emit_ci_patch(snapshot_dir: Path | str, *, out_dir: Path | str | None = None) -> Path:
    """A new-file unified diff adding the runner, with the rollback in its header."""
    directory = Path(out_dir) if out_dir is not None else PATCHES_DIR
    snapshot = Path(snapshot_dir)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id", snapshot.name)
    suites = sorted(
        p.name for p in (snapshot / "project" / "scripts" / "hooks").glob("*_test.py")
    )
    safe = snapshot_id.replace("@", "-at-").replace("/", "-")
    path = assert_writable(directory / f"ci-hook-battery-{safe}.diff")
    directory.mkdir(parents=True, exist_ok=True)
    body = RUNNER_SOURCE.rstrip("\n").splitlines()
    header = [
        f"# bear-hug H05 — CI hook-test runner proposal, against snapshot {snapshot_id}",
        "# Proposed, never applied by bear-hug. Adds ONE file; edits nothing else.",
        f"# Suites it would run from that snapshot ({len(suites)}): {', '.join(suites)}",
        "# Each suite is its own bounded process (default 120 s, CI_HOOK_SUITE_TIMEOUT_S), because",
        "# `unittest discover` over scripts/hooks is not green while every suite passes alone.",
        f"# {ROLLBACK}",
        "# ANCHOR-SAFETY: safe — CLAUDE.md is not touched.",
        "# CI platform integration (where this runs, on what trigger) is a Barracuda-owned "
        "decision.",
        "",
        "--- /dev/null",
        f"+++ b/{RUNNER_PATH}",
        f"@@ -0,0 +1,{len(body)} @@",
    ]
    path.write_text("\n".join(header + [f"+{line}" for line in body]) + "\n", encoding="utf-8")
    return path


__all__ = ["ROLLBACK", "RUNNER_PATH", "RUNNER_SOURCE", "emit_ci_patch"]
