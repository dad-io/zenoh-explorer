"""A human verdict on a persisted eval run.

E06 lets the judge downgrade a deterministic PASS to `unscored`; only a person may settle it.
Sam ruled on 2026-09-02 that the judge stands for the five held runs of the first battery. This
module records such a ruling on the run — who, what, and what the verdict was before — and the
matrix and the eval report read `final_verdict` over the deterministic `passed` when present.
Nothing here touches the deterministic score or the judgment; both stay on the file as written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bearhug.paths import assert_writable

VERDICTS = ("pass", "fail", "unscored")


def final_passed(result: dict[str, Any]) -> bool:
    """The verdict a matrix cell counts: `final_verdict` when a judge or a person set one
    (`unscored` counts as not passed), else the deterministic `passed`."""
    final = result.get("final_verdict")
    if final in VERDICTS:
        return final == "pass"
    return result.get("passed") is True


def adjudicate_run(run_root: Path | str, *, verdict: str, by: str, note: str) -> dict[str, Any]:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, not {verdict!r}")
    if not by or not by.strip():
        raise ValueError("an adjudication needs `by`: who ruled")
    path = assert_writable(Path(run_root) / "result.json")
    if not path.is_file():
        raise ValueError(f"{run_root} is not a persisted eval run")
    result = json.loads(path.read_text(encoding="utf-8"))
    result["adjudication"] = {
        "verdict": verdict,
        "by": by.strip(),
        "note": note,
        "previous_final": result.get("final_verdict"),
    }
    result["final_verdict"] = verdict
    result["final_reason"] = f"adjudicated {verdict} by {by.strip()}: {note}".rstrip(": ")
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


__all__ = ["VERDICTS", "adjudicate_run", "final_passed"]
