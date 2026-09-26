"""E07 — the optional schema-constrained LLM judge for prose quality.

Ruled by E06 (Sam, 2026-09-01): model pinned to `claude-sonnet-5`, one pass per judgment, a closed
JSON verdict, one versioned rubric per scenario, prompts and outputs kept under `runs/` only. The
judge may DOWNGRADE a deterministic PASS to `unscored` for review; it never overrules a
deterministic FAIL. Every failure — timeout, CLI error, schema refusal, over-budget, a model
refusal, anything unexpected — yields `unscored`, never a fabricated pass or fail.

No API key is required. The transport is the already-authenticated Claude Code CLI in safe mode,
with no tools, no session persistence, one structured-output pass, and the exact model name. The
request parameters are recorded on every judgment so a reader can see exactly what was sent.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bearhug.evals.runner import child_env
from bearhug.evals.score import Score, _records, _walk
from bearhug.evals.spec import Scenario, resolve_scenario
from bearhug.paths import assert_writable

JUDGE_MODEL = "claude-sonnet-5"
JUDGE_VERSION = "1.1.0"
RULING = "E06 — Sam, 2026-09-01"

#: One rubric per scenario, versioned. A judge prompt cites the clause the scenario states.
RUBRIC_VERSION = "1.0.0"

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "unscored"]},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["verdict", "reasons"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a strict grader of one headless Claude Code session against one rubric clause. "
    "Judge only what the transcript excerpt shows; a claim in prose is not evidence of the act. "
    "Answer with the JSON object the schema requires and nothing else. Use `unscored` when the "
    "excerpt does not let you decide."
)


@dataclass(frozen=True, slots=True)
class JudgeConfig:
    model: str = JUDGE_MODEL
    effort: str = "low"
    max_cost_usd: float = 0.10
    timeout_s: float = 60.0
    excerpt_chars: int = 20_000


@dataclass(frozen=True, slots=True)
class Judgment:
    verdict: str  # pass | fail | unscored
    reasons: tuple[str, ...]
    # judged | schema_refusal | cli_error | timeout | over_budget | refusal | judge_error
    status: str
    model: str
    judge_version: str
    rubric_version: str
    request_params: dict[str, Any]
    cost_usd: float | None
    usage: dict[str, int] | None
    error: str | None
    prompt_sha256: str
    output_sha256: str | None
    prompt: str
    raw_output: str | None

    def public(self) -> dict[str, Any]:
        """The judgment without prompt or output text — what a result file may carry."""
        data = asdict(self)
        data.pop("prompt")
        data.pop("raw_output")
        return data


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _excerpt(stream_path: Path, limit: int) -> str:
    """Assistant text and the final result text, oldest first, bounded from the END so the
    close of the session — where claims are made — is always present."""
    parts: list[str] = []
    for record in _records(stream_path):
        kind = record.get("type")
        if kind == "assistant":
            for value in _walk(record.get("message")):
                if isinstance(value, dict) and value.get("type") == "text":
                    text = value.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        elif kind == "result" and isinstance(record.get("result"), str):
            parts.append("[result] " + record["result"].strip())
    excerpt = "\n---\n".join(parts) or "[the stream carries no assistant text]"
    return excerpt[-limit:]


def _prompt(scenario: Scenario, excerpt: str) -> str:
    return (
        f"Scenario {scenario.id}. Task given to the session:\n{scenario.prompt}\n\n"
        f"Rubric clause (version {RUBRIC_VERSION}): {scenario.description}\n"
        f"Deterministic rubric kind: {scenario.rubric}\n\n"
        f"Transcript excerpt:\n{excerpt}\n"
    )


def _validate(payload: Any) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(payload, dict) or set(payload) != {"verdict", "reasons"}:
        return None
    verdict, reasons = payload["verdict"], payload["reasons"]
    if verdict not in ("pass", "fail", "unscored"):
        return None
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        return None
    if len(reasons) > 8:
        return None
    return verdict, tuple(reasons)


def _usage_dict(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    out = {str(key): item for key, item in value.items() if isinstance(item, int)}
    return out or None


def _command(executable: str, prompt: str, config: JudgeConfig) -> list[str]:
    """One authenticated Claude Code task, with repository customizations and tools disabled."""
    return [
        executable,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(VERDICT_SCHEMA, sort_keys=True, separators=(",", ":")),
        "--model",
        config.model,
        "--effort",
        config.effort,
        "--max-budget-usd",
        str(config.max_cost_usd),
        "--system-prompt",
        SYSTEM,
        "--safe-mode",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
    ]


def _payload(envelope: Any) -> Any:
    """Accept Claude Code's structured-output envelope, plus conservative test-double shapes."""
    if not isinstance(envelope, dict):
        return None
    if set(envelope) == {"verdict", "reasons"}:
        return envelope
    structured = envelope.get("structured_output")
    if structured is not None:
        return structured
    result = envelope.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return None
    return None


def _error_status(message: str) -> str:
    lowered = message.lower()
    if "budget" in lowered:
        return "over_budget"
    if "refus" in lowered:
        return "refusal"
    return "cli_error"


def judge_stream(
    stream_path: Path,
    scenario: Scenario,
    *,
    executable: str = "claude",
    executor: Any = subprocess.run,
    config: JudgeConfig | None = None,
) -> Judgment:
    """Judge one persisted stream through Claude Code. Never raises; failures are `unscored`."""
    config = config or JudgeConfig()
    excerpt = _excerpt(Path(stream_path), config.excerpt_chars)
    prompt = _prompt(scenario, excerpt)
    request_params: dict[str, Any] = {
        "model": config.model,
        "transport": "Claude Code CLI (existing account auth; no API key)",
        "effort": config.effort,
        "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
        "max_cost_usd": config.max_cost_usd,
        "timeout_s": config.timeout_s,
        "safe_mode": True,
        "tools": [],
        "session_persistence": False,
        "ruling": RULING,
    }

    def unscored(status: str, error: str | None, *, cost=None, usage=None, raw=None) -> Judgment:
        return Judgment(
            verdict="unscored", reasons=(), status=status, model=config.model,
            judge_version=JUDGE_VERSION, rubric_version=RUBRIC_VERSION,
            request_params=request_params, cost_usd=cost, usage=usage, error=error,
            prompt_sha256=_sha(prompt), output_sha256=_sha(raw) if raw is not None else None,
            prompt=prompt, raw_output=raw,
        )

    if executor is subprocess.run and shutil.which(executable) is None:
        return unscored("cli_error", f"{executable!r} is not on PATH")

    try:
        completed = executor(
            _command(executable, prompt, config),
            cwd=Path(stream_path).parent,
            env=child_env(),  # the judge is its own task, not a child of this session
            capture_output=True,
            text=True,
            check=False,
            timeout=config.timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return unscored("timeout", f"Claude Code judge timed out after {exc.timeout} seconds")
    except OSError as exc:
        return unscored("cli_error", f"Claude Code judge could not start: {exc}")
    except Exception as exc:  # noqa: BLE001 — a judge must never take the run down with it
        return unscored("judge_error", f"{type(exc).__name__}: {exc}")

    raw = completed.stdout or ""
    stderr = completed.stderr or ""
    combined_error = (stderr + "\n" + raw).strip()
    if completed.returncode != 0:
        error = combined_error or f"Claude Code exited {completed.returncode}"
        return unscored(_error_status(error), error, raw=raw)
    try:
        envelope = json.loads(raw)
    except ValueError:
        return unscored("schema_refusal", "Claude Code output is not JSON", raw=raw)
    usage = _usage_dict(envelope.get("usage")) if isinstance(envelope, dict) else None
    cost_value = envelope.get("total_cost_usd") if isinstance(envelope, dict) else None
    cost = float(cost_value) if isinstance(cost_value, (int, float)) else None
    if isinstance(envelope, dict) and envelope.get("is_error"):
        error = combined_error or "Claude Code returned an error"
        return unscored(
            _error_status(error), error, cost=cost, usage=usage, raw=raw
        )
    if cost is not None and cost > config.max_cost_usd:
        return unscored(
            "over_budget", f"the call cost {cost:.6f} USD, over the cap {config.max_cost_usd}",
            cost=cost, usage=usage, raw=raw,
        )
    validated = _validate(_payload(envelope))
    if validated is None:
        return unscored(
            "schema_refusal", "output does not match the closed verdict schema",
            cost=cost, usage=usage, raw=raw,
        )
    verdict, reasons = validated
    return Judgment(
        verdict=verdict, reasons=reasons, status="judged", model=config.model,
        judge_version=JUDGE_VERSION, rubric_version=RUBRIC_VERSION,
        request_params=request_params, cost_usd=cost, usage=usage, error=None,
        prompt_sha256=_sha(prompt), output_sha256=_sha(raw), prompt=prompt, raw_output=raw,
    )


def combine(deterministic: Score, judgment: Judgment) -> tuple[str, str]:
    """E06's authority rule. Returns (final verdict, reason)."""
    if not deterministic.passed:
        return (
            "fail",
            f"deterministic FAIL stands ({deterministic.reason}); the judge cannot overrule it",
        )
    if judgment.status != "judged":
        return "pass", (
            f"deterministic PASS stands ({deterministic.reason}); judge {judgment.status}: "
            f"{judgment.error or 'no verdict'}"
        )
    if judgment.verdict == "pass":
        return "pass", f"deterministic PASS and the judge agrees: {'; '.join(judgment.reasons)}"
    if judgment.verdict == "fail":
        return "unscored", (
            "deterministic PASS, but the judge disagreed — held for human review: "
            + "; ".join(judgment.reasons)
        )
    return (
        "pass",
        "deterministic PASS stands; the judge returned unscored: "
        + "; ".join(judgment.reasons),
    )


def write_judgment(run_root: Path, judgment: Judgment) -> Path:
    """Persist the judgment WITH its prompt and raw output under the run (runs/ is gitignored);
    result files carry only `judgment.public()`."""
    path = assert_writable(Path(run_root) / "judge.json")
    path.write_text(json.dumps(asdict(judgment), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def judge_run(
    run_root: Path,
    *,
    executable: str = "claude",
    executor: Any = subprocess.run,
    config: JudgeConfig | None = None,
) -> tuple[str, Judgment]:
    """Judge one persisted eval run and add the public judgment to its result.

    Prompt and raw output stay in ``judge.json`` beside the run. ``result.json`` receives only
    hashes, request metadata, usage, cost, and the combined verdict.
    """
    root = Path(run_root)
    result_path = assert_writable(root / "result.json")
    stream_path = root / "stream.jsonl"
    if not result_path.is_file() or not stream_path.is_file():
        raise ValueError(f"{root} is not a persisted eval run")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"cannot read eval result at {result_path}: {exc}") from exc
    scenario_name = result.get("scenario")
    passed = result.get("passed")
    reason = result.get("reason")
    if not isinstance(scenario_name, str) or not isinstance(passed, bool) or not isinstance(
        reason, str
    ):
        raise ValueError("eval result needs string scenario/reason and boolean passed fields")

    judgment = judge_stream(
        stream_path,
        resolve_scenario(scenario_name),
        executable=executable,
        executor=executor,
        config=config,
    )
    write_judgment(root, judgment)
    final, final_reason = combine(Score(passed=passed, reason=reason), judgment)
    result["judgment"] = judgment.public()
    result["final_verdict"] = final
    result["final_reason"] = final_reason
    result_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return final, judgment


__all__ = [
    "JUDGE_MODEL", "JUDGE_VERSION", "JudgeConfig", "Judgment", "RUBRIC_VERSION",
    "VERDICT_SCHEMA", "combine", "judge_run", "judge_stream", "write_judgment",
]
