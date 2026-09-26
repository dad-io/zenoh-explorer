"""Headless eval runner and report — see docs/METHOD.md for its external-validity limit."""

from bearhug.evals.matrix import EffectRule, Matrix, build_matrix, compare_arms, render_matrix
from bearhug.evals.report import build_eval_findings, load_results, write_eval_report
from bearhug.evals.runner import (
    VARIANTS,
    BatteryResult,
    EvalResult,
    install_variant,
    run_approved_battery,
    run_battery,
    run_eval,
)
from bearhug.evals.score import Score, score_stream, stream_cost_usd
from bearhug.evals.spec import SCENARIOS, Scenario, resolve_scenario

__all__ = [
    "EffectRule",
    "Matrix",
    "build_matrix",
    "compare_arms",
    "render_matrix",
    "SCENARIOS",
    "BatteryResult",
    "EvalResult",
    "Scenario",
    "Score",
    "VARIANTS",
    "build_eval_findings",
    "install_variant",
    "load_results",
    "resolve_scenario",
    "run_eval",
    "run_approved_battery",
    "run_battery",
    "score_stream",
    "stream_cost_usd",
    "write_eval_report",
]
