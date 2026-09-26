"""Phase 3 — run each gate against a fixture corpus and see what it actually does."""

from bearhug.hooks.audit import audit, run_battery
from bearhug.hooks.coverage import (
    check_stop_arbitration,
    check_stop_ordering,
    check_test_coverage,
)
from bearhug.hooks.fixtures import EventFixture, corpus, write_transcript
from bearhug.hooks.runner import DEFAULT_TIMEOUT, HookRun, run_hook
from bearhug.hooks.scratch import ScratchBoundaryError, assert_scratch, build_fixture_repo

__all__ = [
    "DEFAULT_TIMEOUT",
    "EventFixture",
    "HookRun",
    "ScratchBoundaryError",
    "assert_scratch",
    "audit",
    "run_battery",
    "build_fixture_repo",
    "check_stop_arbitration",
    "check_stop_ordering",
    "check_test_coverage",
    "corpus",
    "run_hook",
    "write_transcript",
]
