"""Phase 2 — static analysis of a snapshotted CLAUDE.md."""

from bearhug.lint.anchors import anchor_index, broken_anchors, check_anchor_safety
from bearhug.lint.budget import build_findings as build_budget_findings
from bearhug.lint.gates import (
    check_gate_coverage,
    classify_hook,
    hook_arguments,
    parse_hooks,
    resolve_script,
)
from bearhug.lint.parse import parse_sections, top_level_bullets
from bearhug.lint.reachability import (
    Traffic,
    check_matcher_reachability,
    literal_tool_union,
    matcher_tools,
)
from bearhug.lint.refs import check_dead_refs, check_superseded
from bearhug.lint.runconfig import (
    RUN_CONFIG_CHECKS,
    check_run_configs,
    parse_run_configs,
)
from bearhug.lint.runner import run_all_checks
from bearhug.lint.stalecount import check_stale_counts

__all__ = [
    "RUN_CONFIG_CHECKS",
    "anchor_index",
    "broken_anchors",
    "build_budget_findings",
    "check_anchor_safety",
    "check_dead_refs",
    "check_gate_coverage",
    "Traffic",
    "check_matcher_reachability",
    "check_run_configs",
    "check_stale_counts",
    "check_superseded",
    "parse_run_configs",
    "classify_hook",
    "hook_arguments",
    "literal_tool_union",
    "matcher_tools",
    "parse_hooks",
    "parse_sections",
    "run_all_checks",
    "resolve_script",
    "top_level_bullets",
]
