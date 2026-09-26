"""3.4 COVERAGE · 3.7 ORDERING · 3.10 STOP-ARBITRATION.

The last three ask about the gate SET rather than any one gate. Which gates nobody tests; what
happens to the gates after one that blocks; and whether the demands several gates make of the
same turn can be satisfied together. The corpus says the last one is where the harness actually
fails, and no per-gate check can see it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bearhug.hooks.ownership import (
    RECEIPT_RELPATH,
    installed_paths,
    load_setup_receipt,
    receipt_sha256,
    release_label,
    snapshot_file_sha256,
)
from bearhug.hooks.runner import HookRun
from bearhug.lint.gates import parse_hooks
from bearhug.model import Evidence, Finding, Severity
from bearhug.read_optional import read_json_or_default


#: `deepcheck_test.py` covers `deep-check.py`; normalise both to `deepcheck`.
def _norm(name: str) -> str:
    return re.sub(r"[-_]", "", Path(name).stem.removesuffix("_test"))


#: Remediation vocabulary. A gate either wants the turn SMALLER or wants more work done in it.
WANTS_SHORTER = re.compile(r"re-?send\s+shorter|shorter|end on one|too long|no engagement point",
                           re.IGNORECASE)
WANTS_MORE = re.compile(r"dispatch|run `?dlv|set a breakpoint|create the task|TaskCreate|"
                        r"commission|reviewer|write the row|add the row", re.IGNORECASE)

LIMIT_COVERAGE = (
    "Proves no test file in the snapshot names this hook. Does not prove it is untested — a "
    "shared module's tests may exercise it indirectly — only that nothing addresses it by name."
)

LIMIT_COVERED_UPSTREAM = (
    "Proves the project's own setup receipt lists this exact path and component as a Bear Hug "
    "install, AND that this snapshot's bytes for it still hash to the sha256 the receipt "
    "recorded at install time — not that the named upstream release actually tests it, since "
    "this audit checks the PROJECT's test tree only, never Bear Hug's own suite. A receipt a "
    "project hand-edited, or a stale receipt left behind after the hook was removed from "
    "settings.json, would misinform this the same way it would misinform "
    "`bearhug project-setup` itself."
)

LIMIT_MODIFIED_FROM_UPSTREAM = (
    "Proves only that this snapshot's bytes for this path no longer hash to the sha256 the "
    "setup receipt recorded at install time (or that one of the two digests could not be "
    "computed) — not what changed, or whether the change is safe. `project_setup.py`'s own "
    "conflict detection is where an unexpected on-disk difference is normally caught, at setup "
    "time; this only says the receipt's upstream-coverage claim no longer applies to these "
    "bytes, so the ordinary untested-hook signal applies instead."
)


def _short_digest(value: str | None) -> str:
    return value[:12] if value else "unavailable"


def check_test_coverage(snapshot_dir: Path, *, snapshot_id: str) -> list[Finding]:
    """3.4 — which wired gates has nobody written a test for?

    A gate this project's own setup receipt says Bear Hug installed is reported separately
    (COVERAGE, severity info, "covered upstream") from a gate the project wrote or vendored
    itself: "no test file in this snapshot names it" is true of both, but only the second is
    this project's own gap. Recognition reads the receipt's `files[]` by exact path — the same
    identity setup itself uses — never by matching a hook's filename against a fixed list, which
    would silently misclassify a project that vendors its own same-named hook.
    """
    project = Path(snapshot_dir) / "project"
    settings, _absent = read_json_or_default(project / ".claude" / "settings.json")
    hooks_dir = project / "scripts" / "hooks"
    tested = {_norm(p.name) for p in hooks_dir.glob("*_test.py")}

    receipt = load_setup_receipt(snapshot_dir)
    bearhug_owned = installed_paths(receipt)
    bearhug_sha = receipt_sha256(receipt)
    release = release_label(snapshot_dir) if receipt is not None else None

    wired: dict[str, tuple[str, str]] = {}
    for spec in parse_hooks(settings):
        script = spec.script
        if script:
            wired.setdefault(Path(script).name, (spec.event, script))

    findings: list[Finding] = []
    for name, (event, script) in sorted(wired.items()):
        if _norm(name) in tested:
            continue
        component = bearhug_owned.get(script)
        if component is not None:
            recorded_sha = bearhug_sha.get(script)
            current_sha = snapshot_file_sha256(snapshot_dir, script)
            if recorded_sha is not None and current_sha is not None and recorded_sha == current_sha:
                findings.append(Finding(
                    id=f"hook-covered-upstream-{name}",
                    check="COVERAGE",
                    severity=Severity.INFO,
                    summary=(
                        f"{name} is wired on {event} and covered upstream: the project's setup "
                        f"receipt shows Bear Hug installed it "
                        f"({component or 'unlabeled component'})"
                        f", tested in the installed Bear Hug release {release}, not this project"
                    ),
                    snapshot=snapshot_id,
                    evidence=(Evidence(file=str(RECEIPT_RELPATH),
                                       excerpt=f"{script} · component={component or '(none)'} · "
                                               f"sha256={_short_digest(recorded_sha)}"),),
                    detail=f"{name} {event} installed-by-bearhug component={component}",
                    limit=LIMIT_COVERED_UPSTREAM,
                ))
                continue
            # The receipt says Bear Hug installed this path, but this snapshot's bytes no
            # longer hash to what the receipt recorded (a project customized the hook after
            # install), or one of the two digests could not be computed at all. Either way the
            # upstream-coverage claim no longer applies to these bytes, so this reports as a
            # PROJECT gap — same severity as an ordinary untested hook — while still naming
            # what Bear Hug installed and both digests, rather than silently falling back to
            # the plain "no test file names it" wording that would hide the divergence.
            findings.append(Finding(
                id=f"hook-modified-from-upstream-{name}",
                check="COVERAGE",
                severity=Severity.COSTLY,
                summary=(
                    f"{name} is wired on {event} and no test file names it — Bear Hug "
                    f"installed it ({component or 'unlabeled component'}) but this snapshot's "
                    f"bytes no longer match the receipt: receipt sha256 "
                    f"{_short_digest(recorded_sha)}, current sha256 {_short_digest(current_sha)}"
                ),
                snapshot=snapshot_id,
                evidence=(Evidence(file=f"scripts/hooks/{name}",
                                   excerpt=f"receipt sha256={_short_digest(recorded_sha)} "
                                           f"current sha256={_short_digest(current_sha)}"),),
                detail=(
                    f"{name} {event} untested modified-from-upstream component={component} "
                    f"receipt_sha256={recorded_sha} current_sha256={current_sha}"
                ),
                limit=LIMIT_MODIFIED_FROM_UPSTREAM,
            ))
            continue
        findings.append(Finding(
            id=f"hook-untested-{name}",
            check="COVERAGE",
            severity=Severity.COSTLY,
            summary=f"{name} is wired on {event} and no test file names it",
            snapshot=snapshot_id,
            evidence=(Evidence(file=f"scripts/hooks/{name}", excerpt=f"wired on {event}"),),
            detail=f"{name} {event} untested",
            limit=LIMIT_COVERAGE,
        ))
    return findings


def check_stop_ordering(
    snapshot_dir: Path, runs: list[tuple[Any, HookRun]], *, snapshot_id: str
) -> list[Finding]:
    """3.7 — does a gate that blocks stop the gates declared after it?

    It does not, and that is the finding. Claude Code runs each hook command as its own process,
    so a block is a verdict rather than a halt. The corpus proves it directly: three gates wrote
    blocking messages for one turn within 10 ms of each other on 2026-08-27.
    """
    project = Path(snapshot_dir) / "project"
    settings, _absent = read_json_or_default(project / ".claude" / "settings.json")
    stop_order = [Path(s.script).name for s in parse_hooks(settings)
                  if s.event == "Stop" and s.script]

    blocked_by_fixture: dict[str, list[str]] = {}
    for _spec, run in runs:
        if run.event == "Stop" and run.blocked:
            blocked_by_fixture.setdefault(run.fixture, []).append(run.hook)

    findings: list[Finding] = []
    for fixture, hooks in sorted(blocked_by_fixture.items()):
        if len(hooks) < 2:
            continue
        positions = sorted(stop_order.index(h) for h in hooks if h in stop_order)
        after_first = [stop_order[i] for i in range(positions[0] + 1, len(stop_order))]
        findings.append(Finding(
            id=f"stop-ordering-{fixture}",
            check="ORDERING",
            severity=Severity.INFO,
            summary=f"{len(hooks)} Stop gates blocked the same turn; a block does not "
                    f"halt the {len(after_first)} declared after it",
            snapshot=snapshot_id,
            evidence=(Evidence(file=".claude/settings.json",
                               excerpt=" → ".join(stop_order)),),
            detail=f"{fixture} blocked by {', '.join(sorted(hooks))}",
            limit="Each hook command is its own process, so a block is a verdict and not a "
                  "halt. Confirmed independently in the corpus: three gates wrote blocking "
                  "messages for one turn within 10 ms on 2026-08-27T19:32:54.",
        ))
    return findings


def check_stop_arbitration(
    runs: list[tuple[Any, HookRun]], *, snapshot_id: str
) -> list[Finding]:
    """3.10 — can the demands several gates make of one turn be satisfied together?

    This is the defect the session analyser identified as the reason the gates fire endlessly
    without the behaviour changing: one gate requires the turn be re-sent SHORTER while others
    require a reviewer dispatched, a debugger run, and tasks created — in the same turn, in the
    same second, with no arbitration layer between them.
    """
    demands: dict[str, list[tuple[str, str]]] = {}
    for _spec, run in runs:
        if run.event != "Stop" or not run.blocked:
            continue
        reason = str((run.decision or {}).get("reason", ""))
        if not reason:
            continue
        demands.setdefault(run.fixture, []).append((run.hook, reason))

    findings: list[Finding] = []
    for fixture, rows in sorted(demands.items()):
        if len(rows) < 2:
            continue
        shorter = [h for h, r in rows if WANTS_SHORTER.search(r)]
        more = [h for h, r in rows if WANTS_MORE.search(r)]
        if not (shorter and more):
            continue
        findings.append(Finding(
            id=f"stop-arbitration-{fixture}",
            check="ARBITRATION",
            severity=Severity.BROKEN,
            summary=f"on one turn, {', '.join(shorter)} demands a shorter reply while "
                    f"{', '.join(more)} demands added work",
            snapshot=snapshot_id,
            evidence=(Evidence(file=".claude/settings.json", excerpt=fixture),),
            detail=f"{fixture}: shorter={sorted(shorter)} more={sorted(more)}",
            limit="Proves two gates blocked the same fixture with remediations pulling in "
                  "opposite directions. Does not prove no reply satisfies both — only that "
                  "nothing in the harness reconciles them, and the model is left to choose.",
        ))
    return findings
