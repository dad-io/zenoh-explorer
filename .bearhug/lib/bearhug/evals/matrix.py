"""E08/E09 — matched cross-variant statistics and the comparative matrix.

An arm is a variant plus, where one exists, a candidate hash; a cell is one arm on one scenario.
Every cell the matrix was asked for is present or explicitly missing. Cost and latency are
reported as unreported, never as zero. The effect rule — how many repetitions per arm, and what
pass-rate difference counts — is Sam's to approve; with no approved rule every comparison says
"no measured difference: rule unruled". The arithmetic is here, the claim is not.

JSON is the artifact; Markdown renders it and computes nothing of its own.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bearhug.evals.adjudicate import final_passed
from bearhug.paths import REPORTS_DIR, assert_writable


@dataclass(frozen=True, slots=True)
class EffectRule:
    """Approved by a person, or absent. Never defaulted."""

    min_n_per_arm: int
    min_pass_rate_delta: float
    approved_by: str


@dataclass(slots=True)
class Cell:
    arm: str
    scenario: str
    n: int = 0
    passed: int = 0
    durations_ms: list[float] = field(default_factory=list)
    costs_usd: list[float] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    candidate_hash: str | None = None

    @property
    def missing(self) -> bool:
        return self.n == 0

    @property
    def pass_rate(self) -> float | None:
        return self.passed / self.n if self.n else None

    @property
    def mean_duration_ms(self) -> float | None:
        return statistics.fmean(self.durations_ms) if self.durations_ms else None

    @property
    def duration_spread_ms(self) -> float | None:
        if len(self.durations_ms) < 2:
            return None
        return statistics.pstdev(self.durations_ms)

    @property
    def cost_usd(self) -> float | None:
        return round(sum(self.costs_usd), 6) if self.costs_usd else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "scenario": self.scenario,
            "n": self.n,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "mean_duration_ms": self.mean_duration_ms,
            "duration_spread_ms": self.duration_spread_ms,
            "cost_usd": self.cost_usd,
            "candidate_hash": self.candidate_hash,
            "run_ids": list(self.run_ids),
            "missing": self.missing,
        }


@dataclass(slots=True)
class Matrix:
    snapshot_id: str | None
    arms: tuple[str, ...]
    scenarios: tuple[str, ...]
    cells: dict[tuple[str, str], Cell] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: which runtime the cells were restricted to ('snapshot' | 'sealed'); None pools every run
    runtime: str | None = None

    def cell(self, arm: str, scenario: str) -> Cell:
        return self.cells[(arm, scenario)]

    def missing_cells(self) -> list[tuple[str, str]]:
        return [key for key, cell in self.cells.items() if cell.missing]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "snapshot_id": self.snapshot_id,
            "runtime": self.runtime,
            "arms": list(self.arms),
            "scenarios": list(self.scenarios),
            "cells": [self.cells[key].as_dict() for key in sorted(self.cells)],
            "missing_cells": [list(key) for key in sorted(self.missing_cells())],
            "notes": list(self.notes),
        }


def build_matrix(
    results: list[dict[str, Any]],
    *,
    arms: tuple[str, ...],
    scenarios: tuple[str, ...],
    snapshot_id: str | None = None,
    runtime: str | None = None,
) -> Matrix:
    """`runtime` restricts the cells to results that ran that runtime ('snapshot' or 'sealed');
    a result without the field predates the overlay and counts as 'snapshot'. None pools."""
    matrix = Matrix(
        snapshot_id=snapshot_id, arms=tuple(arms), scenarios=tuple(scenarios), runtime=runtime
    )
    for arm in arms:
        for scenario in scenarios:
            matrix.cells[(arm, scenario)] = Cell(arm=arm, scenario=scenario)
    candidates: dict[tuple[str, str], set[str]] = {}
    for result in results:
        key = (str(result.get("variant")), str(result.get("scenario")))
        if key not in matrix.cells:
            continue
        if runtime is not None and (result.get("runtime") or "snapshot") != runtime:
            continue
        cell = matrix.cells[key]
        candidate = result.get("candidate_hash")
        if isinstance(candidate, str) and candidate:
            candidates.setdefault(key, set()).add(candidate)
        cell.n += 1
        cell.passed += int(final_passed(result))
        cell.run_ids.append(str(result.get("run_id", "unknown")))
        duration = result.get("duration_ms")
        if isinstance(duration, int | float) and not isinstance(duration, bool):
            cell.durations_ms.append(float(duration))
        cost = result.get("cost_usd")
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            cell.costs_usd.append(float(cost))
    for key, hashes in candidates.items():
        cell = matrix.cells[key]
        if len(hashes) == 1:
            cell.candidate_hash = next(iter(hashes))
        else:
            matrix.notes.append(
                f"{key[0]} × {key[1]}: two candidate hashes under one variant "
                f"({', '.join(sorted(h[:12] for h in hashes))}); the cell is ambiguous and "
                "is reported as missing rather than pooled"
            )
            matrix.cells[key] = Cell(arm=key[0], scenario=key[1])
    return matrix


@dataclass(frozen=True, slots=True)
class Effect:
    arm_a: str
    arm_b: str
    scenario: str
    pass_rate_delta: float | None
    verdict: str  # "measured difference" | "no measured difference" | "incomparable"
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm_a": self.arm_a, "arm_b": self.arm_b, "scenario": self.scenario,
            "pass_rate_delta": self.pass_rate_delta, "verdict": self.verdict,
            "reason": self.reason,
        }


def compare_arms(
    matrix: Matrix, arm_a: str, arm_b: str, scenario: str, *, rule: EffectRule | None
) -> Effect:
    a, b = matrix.cell(arm_a, scenario), matrix.cell(arm_b, scenario)
    if a.missing or b.missing:
        missing = [arm for arm, cell in ((arm_a, a), (arm_b, b)) if cell.missing]
        return Effect(arm_a, arm_b, scenario, None, "incomparable",
                      f"arm(s) missing for {scenario}: {', '.join(missing)}")
    delta = (a.pass_rate or 0.0) - (b.pass_rate or 0.0)
    if rule is None:
        return Effect(
            arm_a, arm_b, scenario, delta, "no measured difference",
            f"pass-rate delta {delta:+.2f} at n={a.n}/{b.n}; the effect rule is unruled, so no "
            "difference is claimed until Sam approves repeat count and threshold (E08)",
        )
    if a.n < rule.min_n_per_arm or b.n < rule.min_n_per_arm:
        return Effect(
            arm_a, arm_b, scenario, delta, "no measured difference",
            f"n={a.n}/{b.n} below the approved minimum {rule.min_n_per_arm} per arm "
            f"(rule approved by {rule.approved_by})",
        )
    if abs(delta) < rule.min_pass_rate_delta:
        return Effect(
            arm_a, arm_b, scenario, delta, "no measured difference",
            f"pass-rate delta {delta:+.2f} below the approved threshold "
            f"{rule.min_pass_rate_delta:.2f} (rule approved by {rule.approved_by})",
        )
    return Effect(
        arm_a, arm_b, scenario, delta, "measured difference",
        f"pass-rate delta {delta:+.2f} at n={a.n}/{b.n} meets the rule approved by "
        f"{rule.approved_by} (min n {rule.min_n_per_arm}, min delta "
        f"{rule.min_pass_rate_delta:.2f})",
    )


def all_effects(matrix: Matrix, *, rule: EffectRule | None) -> list[Effect]:
    effects = []
    arms = list(matrix.arms)
    for scenario in matrix.scenarios:
        for i, arm_a in enumerate(arms):
            for arm_b in arms[i + 1:]:
                effects.append(compare_arms(matrix, arm_a, arm_b, scenario, rule=rule))
    return effects


def _fmt(value: float | None, unit: str = "") -> str:
    return "unreported" if value is None else f"{value:,.2f}{unit}"


def render_matrix(matrix: Matrix, *, rule: EffectRule | None) -> str:
    lines = [
        f"# Eval matrix — snapshot {matrix.snapshot_id or 'unknown'}",
        "",
        f"- arms: {', '.join(matrix.arms)}; scenarios: {', '.join(matrix.scenarios)}",
        "- effect rule: "
        + (f"min n {rule.min_n_per_arm}/arm, min pass-rate delta {rule.min_pass_rate_delta:.2f}, "
           f"approved by {rule.approved_by}" if rule else "UNRULED — no difference is claimed"),
        "",
        "| arm | scenario | n | passed | pass rate | mean ms | spread ms | cost USD | candidate |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key in sorted(matrix.cells):
        c = matrix.cells[key]
        if c.missing:
            lines.append(f"| {c.arm} | {c.scenario} | 0 | — | missing | — | — | — | — |")
            continue
        lines.append(
            f"| {c.arm} | {c.scenario} | {c.n} | {c.passed} | {c.pass_rate:.2f} | "
            f"{_fmt(c.mean_duration_ms)} | {_fmt(c.duration_spread_ms)} | {_fmt(c.cost_usd)} | "
            f"{(c.candidate_hash or 'n/a')[:12]} |"
        )
    lines += ["", "## Effects", "", "| a | b | scenario | Δ pass rate | verdict | reason |",
              "|---|---|---|---|---|---|"]
    for e in all_effects(matrix, rule=rule):
        delta = "—" if e.pass_rate_delta is None else f"{e.pass_rate_delta:+.2f}"
        lines.append(
            f"| {e.arm_a} | {e.arm_b} | {e.scenario} | {delta} | {e.verdict} | {e.reason} |"
        )
    if matrix.notes:
        lines += ["", "## Notes", ""] + [f"- {note}" for note in matrix.notes]
    lines += [
        "",
        "## Limit",
        "",
        "Headless `claude -p` runs: no user was present mid-turn, so this is evidence about "
        "scripted headless behaviour, not interactive-session effectiveness. A cell's cost is "
        "Claude's own reported figure summed over runs, or unreported; nothing here is estimated "
        "from tokens.",
        "",
    ]
    return "\n".join(lines)


def write_matrix(
    matrix: Matrix, *, rule: EffectRule | None, reports_dir: Path | str | None = None
) -> tuple[Path, Path]:
    root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    safe = (matrix.snapshot_id or "unknown").replace("@", "-at-").replace("/", "-")
    # the snapshot's own harness is the unsuffixed artifact (what S03 pins); a sealed-runtime
    # overlay and a pooled matrix say so in their names, so neither can be mistaken for it
    tag = "" if matrix.runtime == "snapshot" else f"{matrix.runtime or 'pooled'}-"
    json_path = assert_writable(root / f"eval-matrix-{tag}{safe}.json")
    md_path = assert_writable(root / f"eval-matrix-{tag}{safe}.md")
    payload = matrix.as_dict()
    payload["effect_rule"] = (
        {"min_n_per_arm": rule.min_n_per_arm, "min_pass_rate_delta": rule.min_pass_rate_delta,
         "approved_by": rule.approved_by} if rule else None
    )
    payload["effects"] = [e.as_dict() for e in all_effects(matrix, rule=rule)]
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_matrix(matrix, rule=rule), encoding="utf-8")
    return json_path, md_path


__all__ = [
    "Cell",
    "Effect",
    "EffectRule",
    "Matrix",
    "all_effects",
    "build_matrix",
    "compare_arms",
    "render_matrix",
    "write_matrix",
]
