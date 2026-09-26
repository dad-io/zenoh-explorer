# Bear Hug Stop runtime

## Purpose

The vendored, stdlib-only package that becomes Barracuda's Stop coordinator once promoted. It is
developed and fixture-tested here in the lab (`tests/`, driving `bearhug.runtime_package.
install_runtime`) and materialised, unmodified, into two sealed copies: `patches/promotion-package/
scripts/hooks/_bearhug/` (via `bearhug.promotion_package.build_package`) and `setup/runtime/
scripts/hooks/_bearhug/` (via `bearhug.runtime_package.install_runtime`, called from
`setup_components.py`). The lab never installs it into a live project; a Barracuda-owned session
does.

## Responsibilities

- `writes.py` — the one write-target resolver (`any`/`source`/`go` scope), shared by every
  evaluator so the "did this write?" question cannot drift between them. Ported from the captured
  `codewrites.py`, including its **in-project-root scoping**: a Bash write or an absolute
  Edit/Write/MultiEdit `file_path` counts only when it resolves INSIDE the project root. A relative
  path always counts; `/tmp` and `/private/tmp` never do; a `$VAR`-prefixed path resolves against
  an assignment earlier in the same command, `$CLAUDE_PROJECT_DIR`, or a known scratch var name,
  and an unresolvable prefix counts (conservative). `project_root(event)` resolves the root itself:
  the Stop event's own `cwd` field first, then `CLAUDE_PROJECT_DIR`, then the process cwd — an
  evaluator should call this once from its `event` and thread the result through the `root=`
  keyword rather than relying on this module reading the environment.
- `turns.py` — the current-turn tool-call reader, bounded at the last genuine user prompt
  (`is_real_user_message`). `current_turn_tool_calls(path, respect_judged_boundary=True)` also
  resets that window at the last "Stop hook feedback:" entry Claude Code re-injects after a Stop
  block — everything at or before it was already judged and answered. This is opt-in and defaults
  to False: `dlv-verify-gate.py`'s captured counterpart did not grow this rule, so the dlv
  evaluator keeps calling `current_turn_tool_calls(path)` with the old, turn-start-only window.
  `review-gate` is the one caller that opts in.
- `evaluators/` — one pure function per gate (`dlv`, `joinkey`, `response_shape`, `review_gate`,
  `task_durability`). Each takes an `event` (plus, for `task_durability`, injected `tasks`/`repo`/
  transcript readers) and returns exactly one immutable `EvaluatorResult`. None of them print,
  exit, stamp, write telemetry, or call another evaluator — `tests/test_runtime_conformance.py`
  proves that mechanically, not by convention.
- `registry.py` — the ordered, declared evaluator set and the one call path (`invoke`).
- `coordinator.py` — runs the registry, arbitrates (`arbitrate.py`), renders the Stop response
  (`render`), stamps `.automation-stamps/`, and writes telemetry (`telemetry.py`/
  `telemetry_store.py`). This is the only module that does I/O.
- `results.py` — `EvaluatorResult`/`Evidence`, which refuse to construct a schema-invalid or
  incoherent result rather than deferring that failure to serialization.

## Interfaces / dependencies

Standard library only, and no I/O at import time (both are load-bearing: the package runs as bare
`python3 <script>` with no venv, on every Stop, before anything has decided anything). It does not
import `bearhug` — that would break on install rather than in a test.

## Invariants

- `resolve_*_file_writes` results are already root-scoped; a caller never re-applies the rule.
  `WriteResolution.rejected_paths` names a candidate excluded ONLY for resolving outside the root,
  for a caller (`review-gate`) that wants to explain a pass rather than re-parsing the command.
- `review-gate`'s evidence carries `write_in_project=true|false` and `write_path=<path>` (or
  `write_path=opaque` for an unrecoverable inline-interpreter target) whenever it found a write —
  in-root or not — right after `write_position`. `write_in_project=false` appears only when every
  source-extension write the resolver saw was outside the root; the `write_path` recorded there is
  the one it rejected, not a file's contents.
- `dlv-verify-gate`'s verdicts do not move because of the judged-boundary change: it never asks for
  it (`tests/test_dlv_evaluator.py` and `tests/test_dlv_proof_levels.py` must pass unchanged).
- Evidence is allowlisted, bounded mechanical facts only (`results.MAX_EVIDENCE_VALUE`,
  `MAX_EVIDENCE_ITEMS`) — never a model-authored brief, a file's contents, or free transcript prose.

## Testing

`tests/test_runtime_writes.py` (the resolver, including root scoping), `tests/
test_review_gate_evaluator.py`, `tests/test_task_durability_evaluator.py`, `tests/
test_dlv_evaluator.py` + `tests/test_dlv_proof_levels.py`, `tests/test_evaluator_registry.py`,
`tests/test_runtime_conformance.py`, and the promotion/setup tests that assert the two
materialised copies match this source (`tests/test_promotion_package.py`, `tests/
test_promotion_manifest.py`, `tests/test_setup_components.py`). Run with `uv run --locked pytest`.
