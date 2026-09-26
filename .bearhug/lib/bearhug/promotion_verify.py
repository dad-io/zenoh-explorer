"""A falsifier the promotion package carries for its own claims.

Three rounds, four defects, and every one was found the same way: a Barracuda-owned session
MEASURED something the package asserted, rather than reading the assertion. My 1189 tests caught
none of them, because a test written by whoever wrote the claim tends to agree with it.

So the package now ships the measurement. `verify.py` runs the real adapter as a subprocess and
reports, per ADR claim: MEASURED-TRUE, MEASURED-FALSE, or UNMEASURABLE-HERE. A reviewer runs the
same command I do, and a claim that cannot be measured is itself a finding rather than a
reassurance.

It measures. It does not install, and it does not conclude.

**It writes only inside one temp directory, and its last claim proves that rather than asserting
it.** Round 6 rejected the package because that sentence was false: the isolation parameter was an
optional keyword, one of four adapter call sites passed it, and the falsifier put 850 records into
the reviewer's real `~/.claude` store — five per run, five of them carrying a `gate_id` from a lab
fixture that exists nowhere in the package. The parameter is now positional and required, and the
final claim censuses the real store before and after.

**Round 4 rejected the package on a blind spot in THIS FILE, and the criticism was exact.** Every
claim drove `coordinator.run` in-process with a ONE-entry constructed registry. The artifact
`settings-candidate.json` actually registers is `scripts/hooks/stop-coordinator.py`, and nothing
here ran it — so a 37-line adapter that converted every runtime exception into a silent pass sat
above thirteen green rows. It also collapsed each run to `"block" if stdout else "silence"`, which
cannot see the difference between an `error` and a `block`, and never arbitrated more than one
evaluator, which is the reason the coordinator exists at all.

So the falsifier now drives the REGISTERED FILE as a subprocess, arbitrates several gates at once,
and inspects the recorded verdict rather than only the decision. A reviewer should still assume it
has a blind spot: it is written by the same hand as the claims. Its value is that disagreeing with
it costs one command.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TRUE = "MEASURED-TRUE"
FALSE = "MEASURED-FALSE"
UNMEASURABLE = "UNMEASURABLE-HERE"

#: Barracuda decision 0209's evidence tiers, carried on every row.
#:
#: 0209 rules that a guard is not evidence until it has been made to FAIL, that proving it against
#: REAL code outranks proving it by injection, and that tier 3 — injection into a throwaway — is
#: the weakest form and **must be stated as weak rather than reported as proof**.
#:
#: Round 5 pointed out that every §5 policy row here is tier 3: a constructed `raising` evaluator
#: in a one-entry registry. Five green rows, all injections. The ADR said this file shares an
#: author with the claims; it did not say the proof was the weakest tier, and 0209 requires that
#: it be said. So the tier is now printed beside every row, and a reader can see at a glance which
#: greens are cheap.
TIER_REAL = "tier1-real-code"
TIER_ARTIFACT = "tier2-real-artifact"
TIER_INJECTED = "tier3-injected"


def _finding(claim: str, section: str, verdict: str, evidence: str,
             tier: str = TIER_ARTIFACT) -> dict[str, str]:
    return {"section": section, "claim": claim, "verdict": verdict, "evidence": evidence,
            "tier": tier}


def _install(package_root: Path, scratch: Path) -> Path:
    hooks = scratch / "scripts" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package_root / "scripts" / "hooks" / "_bearhug", hooks / "_bearhug")
    shutil.copyfile(
        package_root / "scripts" / "hooks" / "stop-coordinator.py",
        hooks / "stop-coordinator.py",
    )
    return hooks


def _drive(hooks: Path, scratch: Path, registry_code: str, payload: dict) -> dict[str, Any]:
    """Run the real coordinator in a subprocess with a constructed registry."""
    script = scratch / "drive.py"
    script.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(hooks)!r})\n"
        "from _bearhug import coordinator\n"
        "from _bearhug.registry import EvaluatorEntry\n"
        "from _bearhug.results import EvaluatorResult\n"
        f"{registry_code}\n"
        f"payload = {payload!r}\n"
        f"out = coordinator.run(payload, dependencies={{}}, registry=registry,"
        f" stamp_root={str(scratch / 'obs')!r},"
        f" telemetry_root={str(scratch / 'obs' / 'telemetry')!r})\n"
        "print(json.dumps({'exit': out.exit_code, 'stdout': out.stdout.decode('utf-8')}))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        return {"error": (result.stderr.strip().splitlines() or ["unknown"])[-1]}
    return json.loads(result.stdout.strip().splitlines()[-1])


def _probe(hooks: Path, scratch: Path, name: str, code: str) -> dict[str, Any]:
    """Run one structural artifact probe without importing the runtime into this verifier."""
    script = scratch / f"probe-{name}.py"
    script.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(hooks)!r})\n"
        + code
        + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120
    )
    if completed.returncode != 0:
        return {"error": (completed.stderr.strip().splitlines() or ["unknown"])[-1]}
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "probe produced no JSON result"}


def _driver_failed(got: Any, claim: str, section: str) -> dict[str, str] | None:
    """A driver that could not run is not evidence that a claim is false.

    Every claim except the §5 policy loop read `got["stdout"]` through `.get`, so a driver failure
    — a stale `_bearhug/` in the package, say — silently became a MEASURED-FALSE. One run reported
    thirteen of them against code that was correct, which is a falsifier failing OPEN in the loud
    direction: it manufactures defects instead of missing them. Either way a reader cannot use the
    count, which is the number this whole file exists to make usable.
    """
    if "error" not in got:
        return None
    return _finding(
        claim, section, UNMEASURABLE,
        f"the coordinator driver could not run, so this claim was not tested: {got['error']}",
    )


def _real_store() -> Path:
    """Where telemetry would land for a process on this machine that nobody redirected.

    Read from the AMBIENT environment, before this file overrides anything in a child. That is the
    store round 6 found 850 records in, and the one this run must not touch.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(base) / "telemetry" / "bearhug" / "v1"


def _real_project_store() -> Path:
    """Where telemetry lands for a process run from THIS directory that nobody redirected (D08):
    `<cwd>/.bearhug/telemetry/v1`. When the verifier runs from Barracuda's root, that is the real
    project store, and this run must not touch it either."""
    return Path.cwd() / ".bearhug" / "telemetry" / "v1"


def _store_census(root: Path) -> tuple[int, int]:
    """(files, records) under a telemetry root. Counts lines; never reads a record's content."""
    files = sorted(root.rglob("events.jsonl")) if root.exists() else []
    records = 0
    for path in files:
        try:
            with path.open("rb") as handle:
                records += sum(1 for line in handle if line.strip())
        except OSError:
            continue
    return len(files), records


def _boardrows_source(package_root: Path) -> Path | None:
    """The host prerequisite, wherever this is being run.

    `boardrows.py` is NOT in the package — R10 requires the board to be read through the host's
    shared parser so board-restore and the join-key check cannot disagree about what a row is. In
    Barracuda it sits beside the adapter. Without it the adapter's claims are UNMEASURABLE rather
    than false, and this says so instead of reporting a block it caused itself.
    """
    for candidate in (
        Path.cwd() / "scripts" / "hooks" / "boardrows.py",
        package_root / "scripts" / "hooks" / "boardrows.py",
    ):
        if candidate.is_file():
            return candidate
    return None


def _drive_adapter(hooks: Path, project: Path, payload: Any, config_dir: Path) -> dict[str, Any]:
    """Run `stop-coordinator.py` exactly as the Claude Code harness would.

    A subprocess with real stdin and a real exit code, because the round-4 defect lived in
    `__main__` and nothing that imports the runtime can reach it.

    `config_dir` sets `CLAUDE_CONFIG_DIR`, which is what the telemetry root resolves from.

    **It is POSITIONAL AND REQUIRED, and that is the fix rather than a style choice.** Round 6
    rejected this package because `config_dir` was an optional keyword: one of four call sites
    passed it — the one asserting telemetry lands at the declared path — and the other three ran
    with the default, so the falsifier wrote into the reviewer's real `~/.claude` store. 850
    records, five per run, including five carrying `gate_id: "some-new-gate"` from a lab fixture.

    Round 5's finding 1 was "it measures its own redirect." This was that sentence again with the
    redirect real and applied to one path in four. An optional isolation parameter is a defect
    waiting for the next call site; a required one cannot be forgotten.
    """
    environment = dict(
        os.environ, CLAUDE_PROJECT_DIR=str(project), CLAUDE_CONFIG_DIR=str(config_dir)
    )
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    completed = subprocess.run(
        [sys.executable, str(hooks / "stop-coordinator.py")],
        input=raw, capture_output=True, cwd=str(project), timeout=120, env=environment,
    )
    text = completed.stdout.decode("utf-8").strip()
    decision = None
    if text:
        try:
            decision = json.loads(text)
        except ValueError:
            decision = {"decision": "UNPARSEABLE", "reason": text[:200]}
    return {"exit": completed.returncode, "decision": decision}


def _go_edit(project: Path) -> dict:
    """A `.go` edit with no `dlv`: the turn `dlv-verify-gate` blocks."""
    transcript = project / "verify-session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "change it"}}) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "internal/x.go"}}]},
        }) + "\n",
        encoding="utf-8",
    )
    return {"hook_event_name": "Stop", "session_id": "verify", "stop_hook_active": False,
            "transcript_path": str(transcript), "cwd": str(project)}


_RAISER = (
    "def raising(event, *, event_id=None, **kw):\n"
    "    raise RuntimeError('constructed')\n"
)


#: The five gates the coordinator absorbs, and exactly how `settings.json` registers each.
_LIVE_GATES = {
    "response-shape": ("response-shape.py", []),
    "dlv-verify-gate": ("dlv-verify-gate.py", []),
    "task-durability": ("task-durability.py", []),
    "joinkey-lint": ("joinkey-lint.py", ["--check"]),
    "review-gate": ("review-gate.py", []),
}

#: Mutations that must make the differential DISAGREE. A comparison that has never been made to
#: disagree is the unproven guard decision 0209 forbids reporting as proof.
_MUTATIONS = {
    "none": "",
    "never": "coordinator.render = lambda d: coordinator.Rendered(stdout=b'', exit_code=0)\n",
    "always": (
        "coordinator.render = lambda d: coordinator.Rendered("
        'stdout=b\'{"decision":"block","reason":"mutated"}\', exit_code=0)\n'
    ),
}

_DIFF_DRIVER = "\n".join([
    "import json, sys",
    "sys.path.insert(0, {hooks!r})",
    "from _bearhug import coordinator",
    "from _bearhug.registry import REGISTRY",
    "from _bearhug.readers import (board_for, repo_for, tasks_for, transcript_lines_for,",
    "                              transcript_readable_for)",
    "{mutation}",
    "gate, transcript = sys.argv[1], sys.argv[2]",
    "project, obs = {project!r}, {obs!r}",
    'event = {{"session_id": "DIFF", "transcript_path": transcript, "cwd": project,',
    '         "hook_event_name": "Stop", "stop_hook_active": False}}',
    "entry = [e for e in REGISTRY if e.gate_id == gate]",
    "try:",
    "    out = coordinator.run(event, dependencies={{",
    '        "tasks": tasks_for(event), "repo": repo_for(project),',
    '        "transcript_lines": transcript_lines_for(event),',
    '        "transcript_readable": transcript_readable_for(event),',
    '        "board": board_for(project)}},',
    '        registry=entry, stamp_root=obs, telemetry_root=obs + "/telemetry")',
    '    decided = "block" if out.stdout else "silence"',
    "except BaseException as exc:",
    '    decided = "ADAPTER-FAULT:" + type(exc).__name__',
    "reason, evidence = None, []",
    "import glob, os",
    "pattern = os.path.join(obs, 'telemetry', '**', 'events.jsonl')",
    "for path in sorted(glob.glob(pattern, recursive=True)):",
    "    for line in open(path, encoding='utf-8'):",
    "        try:",
    "            rec = json.loads(line)",
    "        except ValueError:",
    "            continue",
    "        if rec.get('gate_id') == gate and rec.get('session_id') == 'DIFF':",
    "            res = rec.get('result') or {{}}",
    "            reason = res.get('reason_code')",
    "            evidence = [e.get('value') for e in res.get('evidence', [])",
    "                        if isinstance(e, dict)]",
    'print(json.dumps({{"decided": decided, "reason": reason, "evidence": evidence}}))',
])


#: Round 14: the two shapes on which Sam RULED the runtime kinder than the captured gate. A
#: disagreement of exactly this shape is a ruled divergence and carries its decision id; every
#: other disagreement is the defect the differential exists to catch.
RULED_DIVERGENCES = {
    "0306": ("dlv-verify-gate", "dlv_session_after_write",
             "dlv_match=real_subcommand_resolved_program"),
    "0307": ("response-shape", "shape_ok", "final_chars="),
}
LEGACY_LONG_CHARS = 1500


def ruled_divergence(gate: str, expected: str, actual: str, reason: str | None,
                     evidence: list[str] | None) -> str | None:
    """The decision id ruling this disagreement; None for a defect or no disagreement."""
    if not (expected == "block" and actual == "silence"):
        return None
    evidence = evidence or []
    if gate == "dlv-verify-gate" and reason == "dlv_session_after_write":
        return "0306" if "dlv_match=real_subcommand_resolved_program" in evidence else None
    if gate == "response-shape" and reason == "shape_ok":
        chars = next(
            (int(v.split("=", 1)[1]) for v in evidence if v.startswith("final_chars=")), None
        )
        return "0307" if chars is not None and chars >= LEGACY_LONG_CHARS else None
    return None


def _transcripts(limit: int) -> list[Path]:
    """Real Stop payloads, newest first. Never read for content, only driven through.

    `BEARHUG_VERIFY_TRANSCRIPTS` overrides discovery with a glob, which is how the lab exercises
    this harness against constructed fixtures. Otherwise the session store for the current project,
    as Claude Code lays it out.
    """
    import glob

    override = os.environ.get("BEARHUG_VERIFY_TRANSCRIPTS")
    if override:
        found = [Path(p) for p in glob.glob(override)]
    else:
        slug = str(Path.cwd()).replace("/", "-")
        found = [
            Path(p)
            for p in glob.glob(os.path.expanduser(f"~/.claude/projects/{slug}/*.jsonl"))
        ]
    found = [p for p in found if p.is_file()]
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit]


def _live_decision(project: Path, gate: str, transcript: Path) -> str:
    """What the CAPTURED gate decides, run exactly as `settings.json` registers it."""
    name, extra = _LIVE_GATES[gate]
    event = {"session_id": "DIFF", "transcript_path": str(transcript), "cwd": str(project),
             "hook_event_name": "Stop", "stop_hook_active": False}
    try:
        completed = subprocess.run(
            [sys.executable, str(project / "scripts" / "hooks" / name), *extra],
            input=json.dumps(event).encode("utf-8"), capture_output=True,
            cwd=str(project), timeout=180,
            env=dict(os.environ, CLAUDE_PROJECT_DIR=str(project),
                     CLAUDE_CONFIG_DIR=str(project / ".claude-config")),
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                if json.loads(line).get("decision") == "block":
                    return "block"
            except ValueError:
                continue
    return "silence"


def _coordinator_decision(hooks: Path, scratch: Path, project: Path, gate: str,
                          transcript: Path, mutation: str) -> tuple[str, str | None, list[str]]:
    script = scratch / f"diff-{mutation}.py"
    script.write_text(
        _DIFF_DRIVER.format(hooks=str(hooks), project=str(project),
                            obs=str(scratch / "diff-obs"), mutation=_MUTATIONS[mutation]),
        encoding="utf-8",
    )
    try:
        completed = subprocess.run(
            [sys.executable, str(script), gate, str(transcript)],
            capture_output=True, cwd=str(project), timeout=180,
            env=dict(os.environ, CLAUDE_PROJECT_DIR=str(project),
                     CLAUDE_CONFIG_DIR=str(project / ".claude-config")),
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", None, []
    text = completed.stdout.decode("utf-8", "replace").strip().splitlines()
    if not text:
        return "DRIVER-FAILED", None, []
    try:
        payload = json.loads(text[-1])
        return payload["decided"], payload.get("reason"), payload.get("evidence") or []
    except (ValueError, KeyError):
        return "DRIVER-FAILED", None, []


def _differential(hooks: Path, scratch: Path, limit: int = 8) -> list[dict[str, str]]:
    """0209 tier 1: the vendored evaluators and the LIVE gates, on the same real Stop payloads.

    Handed to bear-hug by a Barracuda-owned session in the round-5 return, which ran it and got 40
    comparisons, 40 agree, 0 disagree, with both mutation controls firing. It is the first evidence
    in six rounds that tests what the promotion is FOR — that one coordinator reproduces five gates
    — rather than that a constructed injection behaves as constructed.

    Decision 0209: a guard is not evidence until it has been made to FAIL, and proving it against
    real code outranks proving it by injection. So the two controls ship with it. A differential
    that has never disagreed says nothing about its own sensitivity, and theirs printed 40/40
    before they proved it could print anything else.

    Transcript CONTENT never leaves this function. Only per-gate block-vs-silence is reported.
    """
    live_hooks = Path.cwd() / "scripts" / "hooks"
    transcripts = _transcripts(limit)
    claim = (
        "The coordinator reproduces all five captured gates' block-vs-silence decisions on real "
        "Stop payloads except where a recorded ruling moved the verdict (decisions 0306, 0307), "
        "and the comparison has been made to disagree."
    )
    gates_present = (live_hooks / "response-shape.py").is_file()
    if not transcripts or not gates_present:
        return [_finding(
            claim, "1", UNMEASURABLE,
            f"{len(transcripts)} transcript(s) found and live gates "
            f"{'present' if gates_present else 'absent'}; a Barracuda-owned session running from "
            "the repo root measures this",
            tier=TIER_REAL,
        )]

    # A post-promotion tree: the real gates beside the vendored runtime and the adapter, with its
    # own stamp directory, so nothing here writes into the target's working tree.
    project = scratch / "diff-proj"
    (project / "scripts" / "hooks").mkdir(parents=True, exist_ok=True)
    (project / ".automation-stamps").mkdir(exist_ok=True)
    for pattern in ("*.py", "*.sh"):
        for source in live_hooks.glob(pattern):
            shutil.copyfile(source, project / "scripts" / "hooks" / source.name)
    shutil.copytree(hooks / "_bearhug", project / "scripts" / "hooks" / "_bearhug",
                    dirs_exist_ok=True)
    shutil.copyfile(hooks / "stop-coordinator.py",
                    project / "scripts" / "hooks" / "stop-coordinator.py")
    for shared in ("docs", "findings", ".claude"):
        target, origin = project / shared, Path.cwd() / shared
        if origin.exists() and not target.exists():
            target.symlink_to(origin, target_is_directory=True)

    live = {
        (gate, transcript): _live_decision(project, gate, transcript)
        for transcript in transcripts
        for gate in _LIVE_GATES
    }
    live_blocks = sum(1 for value in live.values() if value == "block")

    measured = []
    ruled: dict[str, int] = {}
    for mutation in ("none", "never", "always"):
        agree = disagree = 0
        for (gate, transcript), expected in live.items():
            actual, reason, evidence = _coordinator_decision(
                hooks, scratch, project, gate, transcript, mutation)
            if expected == actual:
                agree += 1
                continue
            decision = ruled_divergence(gate, expected, actual, reason, evidence)
            if mutation == "none" and decision:
                ruled[decision] = ruled.get(decision, 0) + 1
            else:
                disagree += 1
        measured.append((mutation, agree, disagree))

    baseline = next(row for row in measured if row[0] == "none")
    controls = [row for row in measured if row[0] != "none"]
    ruled_total = sum(ruled.values())
    total = baseline[1] + baseline[2] + ruled_total
    ruled_text = (
        "; ".join(f"decision {d}: {n} ruled divergence(s)" for d, n in sorted(ruled.items()))
        or "no ruled divergences"
    )
    findings = [_finding(
        claim, "1",
        TRUE if (baseline[2] == 0 and all(row[2] > 0 for row in controls)) else FALSE,
        f"{total} comparisons over {len(transcripts)} real transcript(s) x 5 gates: "
        f"{baseline[1]} agree, {baseline[2]} disagree, {ruled_total} ruled ({ruled_text}). "
        f"{live_blocks} live block(s). "
        + "; ".join(f"control {name}: {d} disagree" for name, _a, d in controls),
        tier=TIER_REAL,
    )]
    for name, _agree, disagree in controls:
        findings.append(_finding(
            f"Mutation control {name!r} makes the differential DISAGREE, so its zero is a "
            "measurement rather than a blind spot.", "1",
            TRUE if disagree > 0 else FALSE,
            f"{disagree} disagreement(s) under the mutation",
            tier=TIER_REAL,
        ))
    return findings


def verify(package_root: Any) -> list[dict[str, str]]:
    """Measure every ADR claim this environment can reach."""
    package_root = Path(package_root)
    findings: list[dict[str, str]] = []
    scratch = Path(tempfile.mkdtemp(prefix="bearhug-verify-"))
    # Every subprocess this file starts resolves its telemetry root from here. Created before
    # anything runs, so no path can reach the real store by omission.
    config = scratch / "claude-config"
    config.mkdir()
    # Censused BEFORE anything runs, so the last claim below can prove this run touched nothing.
    real_store = _real_store()
    before = _store_census(real_store)
    real_project_store = _real_project_store()
    before_project = _store_census(real_project_store)
    hooks = _install(package_root, scratch)
    manifest = json.loads((package_root / "manifest.json").read_text(encoding="utf-8"))

    # --- identity, by reading -----------------------------------------------------------
    import unicodedata

    # `bearhug-runtime-sha256/1` as decision 0299 RULED it, recomputed by READING rather than by
    # importing the runtime — to be an independent authority, not the same one twice.
    #
    # This filtered TWO categories and encoded paths as raw UTF-8, so it was a FOURTH authority on
    # "which files are the runtime" and it dissented on two of 0299's three amendments. It agreed
    # with the manifest only because this tree is all-ASCII and holds no `.DS_Store`. A Finder
    # window in `_bearhug/` would have made it report the identity claim MEASURED-FALSE against a
    # correct package, and Barracuda develops on macOS and, per decision 0201, on Windows.
    installed = hooks / "_bearhug"
    digest = hashlib.sha256()
    files = sorted(
        p for p in installed.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix != ".pyc"
        and p.name != ".DS_Store"
    )
    for path in files:
        relative = unicodedata.normalize("NFC", path.relative_to(installed).as_posix())
        blob = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(len(blob)).encode("ascii"))
        digest.update(b"\x00")
        digest.update(blob)
    matches = digest.hexdigest() == manifest["runtime_sha256"]
    findings.append(_finding(
        "The installed runtime is the candidate the manifest names.", "identity",
        TRUE if matches else FALSE,
        f"recomputed by reading {len(files)} files: {digest.hexdigest()[:16]}… vs manifest "
        f"{manifest['runtime_sha256'][:16]}…",
    ))

    # --- the SOURCE side of the identity -------------------------------------------------
    #
    # Round 5: "`runtime_sha256` pins the copy against itself. It detects a change to the vendored
    # evaluator and is structurally blind to a change in the gate it was photographed from — the
    # one direction that matters, since five commits touched these files in the six days before
    # your snapshot." The manifest now records the source digests; this compares them to the LIVE
    # files, which is the only place that comparison can be made.
    photographed = (manifest.get("photographed_from") or {}).get("gates") or []
    live_hooks = Path.cwd() / "scripts" / "hooks"
    drifted, unseen, checked = [], [], 0
    for row in photographed:
        name = Path(row["path"]).name
        live = live_hooks / name
        if not live.is_file() or row.get("sha256") is None:
            unseen.append(name)
            continue
        checked += 1
        if hashlib.sha256(live.read_bytes()).hexdigest() != row["sha256"]:
            drifted.append(name)
    if checked == 0:
        findings.append(_finding(
            "The gates the evaluators were ported from have not changed since the snapshot.",
            "identity", UNMEASURABLE,
            f"no live scripts/hooks/ beside this run; {len(unseen)} source file(s) uncheckable. A "
            "Barracuda-owned session running from the repo root measures it",
            tier=TIER_REAL,
        ))
    else:
        findings.append(_finding(
            "The gates the evaluators were ported from have not changed since the snapshot — the "
            "direction runtime_sha256 is structurally blind to.", "identity",
            TRUE if not drifted else FALSE,
            f"{checked} of {len(photographed)} compared against the live tree, "
            + (f"unchanged; {len(unseen)} not present here" if not drifted
               else f"DRIFTED: {', '.join(drifted)} — the port is against stale source"),
            tier=TIER_REAL,
        ))

    # Round-10 ask, item 6: `host_prerequisites` named boardrows.py and recorded no digest, so a
    # host whose boardrows had moved — the case the entry exists to catch — could not be caught.
    prerequisites = manifest.get("host_prerequisites") or []
    boardrows_entry = next(
        (row for row in prerequisites if Path(row.get("path", "")).name == "boardrows.py"), None
    )
    live_boardrows = live_hooks / "boardrows.py"
    if boardrows_entry is None or not boardrows_entry.get("sha256"):
        findings.append(_finding(
            "The host prerequisite boardrows.py carries a digest the host copy can be checked "
            "against.", "identity", FALSE,
            "manifest host_prerequisites has no boardrows digest", tier=TIER_REAL,
        ))
    elif not live_boardrows.is_file():
        findings.append(_finding(
            "The host's boardrows.py is the one the runtime was ported against.", "identity",
            UNMEASURABLE, "no live scripts/hooks/boardrows.py beside this run", tier=TIER_REAL,
        ))
    else:
        live_digest = hashlib.sha256(live_boardrows.read_bytes()).hexdigest()
        findings.append(_finding(
            "The host's boardrows.py is the one the runtime was ported against (C8 counts cells "
            "with its splitter).", "identity",
            TRUE if live_digest == boardrows_entry["sha256"] else FALSE,
            f"host {live_digest[:12]} vs manifest {boardrows_entry['sha256'][:12]}",
            tier=TIER_REAL,
        ))

    # --- section 5: the per-gate failure policy ------------------------------------------
    ruled = {
        "dlv-verify-gate": "block", "review-gate": "block", "task-durability": "block",
        "response-shape": "silence", "joinkey-lint": "silence",
    }
    for gate, expected in ruled.items():
        code = _RAISER + (
            f"registry = [EvaluatorEntry(0, {gate!r}, raising, (), 'keyword', '1.0.0')]"
        )
        got = _drive(hooks, scratch, code,
                     {"stop_hook_active": False, "transcript_path": "", "session_id": "S"})
        if "error" in got:
            findings.append(_finding(
                f"A raising {gate} produces {expected}.", "5", FALSE,
                f"the coordinator itself failed: {got['error']}"))
            continue
        actual = "block" if got["stdout"] else "silence"
        findings.append(_finding(
            f"A raising {gate} produces {expected} (its ruled policy).", "5",
            TRUE if actual == expected else FALSE,
            f"measured {actual}, exit {got['exit']}",
            tier=TIER_INJECTED,
        ))

    # --- section 8: the stamp projection --------------------------------------------------
    # A REAL reason code. This claim used `reason_code='x'` — a code no gate emits — and the ruled
    # projection refuses to guess at an unknown pairing, so it wrote nothing and this read the
    # STALE stamp left by an earlier drive. Caught by the projection, not by review.
    code = (
        "def blocking(event, *, event_id=None, **kw):\n"
        "    return EvaluatorResult.blocked(gate_id='review-gate', gate_version='1.0.0',\n"
        "        event_id=event_id, reason_code='adversarial_review_missing',\n"
        "        remediation='do it', duration_ms=0.0)\n"
        "registry = [EvaluatorEntry(0, 'review-gate', blocking, (), 'keyword', '1.0.0')]"
    )
    stamp_drive = _drive(hooks, scratch, code,
                         {"stop_hook_active": False, "transcript_path": "", "session_id": "S"})
    stamp_broke = _driver_failed(
        stamp_drive,
        "The coordinator writes .automation-stamps/ with the captured vocabulary.", "8",
    )
    stamp = scratch / "obs" / ".automation-stamps" / "review-gate"
    if stamp_broke is not None:
        findings.append(stamp_broke)
    elif stamp.is_file():
        verdict_token = stamp.read_text(encoding="utf-8").split()[1]
        findings.append(_finding(
            "The coordinator writes .automation-stamps/ with the captured vocabulary.", "8",
            TRUE if verdict_token == "BLOCKED" else FALSE,
            f"wrote {stamp.name} = {verdict_token!r}",
        ))
    else:
        findings.append(_finding(
            "The coordinator writes .automation-stamps/.", "8", FALSE,
            "no stamp file was written by a real coordinator run",
        ))

    # --- section 7: telemetry --------------------------------------------------------------
    telemetry_root = scratch / "obs" / "telemetry"
    written = list(telemetry_root.rglob("events.jsonl")) if telemetry_root.exists() else []
    findings.append(_finding(
        "The coordinator writes a telemetry record per evaluator invocation.", "7",
        TRUE if written else FALSE,
        f"{len(written)} events.jsonl under {telemetry_root.name}/"
        + (f", {sum(1 for _ in written[0].read_text().splitlines())} record(s)" if written else ""),
    ))

    # --- section 2: three Stop registrations ------------------------------------------------
    settings = json.loads(
        (package_root / "patches" / "settings-candidate.json").read_text(encoding="utf-8")
    )
    commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    findings.append(_finding(
        "After promotion Barracuda registers THREE Stop commands, not one.", "2",
        TRUE if len(commands) == 3 else FALSE, f"settings-candidate.json registers {len(commands)}",
    ))

    # --- section 9: no timeout on the coordinator --------------------------------------------
    coordinator_entries = [
        h for g in settings["hooks"]["Stop"] for h in g["hooks"]
        if "stop-coordinator" in h["command"]
    ]
    findings.append(_finding(
        "No timeout is set on the coordinator registration (H04 measured; no timeout chosen).", "9",
        TRUE if coordinator_entries and "timeout" not in coordinator_entries[0] else FALSE,
        f"coordinator entry keys: {sorted(coordinator_entries[0]) if coordinator_entries else []}",
    ))

    # --- the REGISTERED artifact, driven as a subprocess ---------------------------------------
    #
    # Round 4's rejection: every claim above runs `coordinator.run` in-process. The file
    # `settings-candidate.json` registers is `stop-coordinator.py`, and nothing measured it.
    boardrows = _boardrows_source(package_root)
    if boardrows is None:
        findings.append(_finding(
            "The REGISTERED adapter decides, rather than exiting 0 on its own failure.", "1",
            UNMEASURABLE,
            "scripts/hooks/boardrows.py was not found beside this run. It is a host prerequisite, "
            "not part of the package; without it the runtime raises by design and every adapter "
            "measurement would be reporting a block this script caused",
        ))
    else:
        shutil.copyfile(boardrows, hooks / "boardrows.py")
        fault = "The Stop coordinator is misconfigured"

        # Round 9's real-input differential agreed 40/40 and still missed four acceptance-set
        # defects because none of those inputs existed in the corpus. Pin the constructed shapes
        # in the shipped verifier as well as the lab suite, so the next reviewer does not have to
        # rediscover them by reading both implementations line by line.
        c5_root = scratch / "round10-c5"
        c5_board = c5_root / "docs" / "superpowers" / "plans"
        c5_board.mkdir(parents=True)
        c5_board.joinpath("BOARD.md").write_text(
            "| # | entry | ruling | execution | Notes | blocked | authority |\n"
            "|---|---|---|---|---|---|---|\n"
            "| 99 | [P1] work | accepted | unstarted | "
            "GATED BY ROW 100; GATED BY ROW 157 | | |\n"
            "| 100 | [P1] open | accepted | unstarted | | | |\n"
            "| 157 | [P1] done | accepted | executed | | | |\n",
            encoding="utf-8",
        )
        c7_root = scratch / "round10-c7"
        c7_board = c7_root / "docs" / "superpowers" / "plans"
        c7_board.mkdir(parents=True)
        c7_board.joinpath("BOARD.md").write_text(
            "| # | entry | ruling | execution | Notes | blocked | authority |\n"
            "|---|---|---|---|---|---|---|\n"
            "| 7 | [P1] work | accepted | unstarted | 🔵 active | | |\n"
            "Legend 🔵 quotes | 99 | but is not a row\n"
            "| note | 🔵 cites | 42 | in a later cell |\n"
            "> | 43 | 🔵 quoted row |\n",
            encoding="utf-8",
        )
        c8_root = scratch / "round10-c8"
        c8_board = c8_root / "docs" / "superpowers" / "plans"
        c8_board.mkdir(parents=True)
        c8_board.joinpath("BOARD.md").write_text(
            "| # | entry | ruling | execution | Notes | blocked | authority |\n"
            "|---|---|---|---|---|---|---|\n"
            "| 8 | [P1] regex | accepted | unstarted | matches `a|b` | | |\n"
            "| 9 | [P1] regex | accepted | unstarted | matches `a\\|b` | | |\n",
            encoding="utf-8",
        )
        (c8_root / "docs" / "memex" / "syntheses").mkdir(parents=True)
        (c8_root / "docs" / "memex" / "syntheses" / "MASTER-PLAN.md").write_text(
            "## 5. Open rows by phase\n\n| phase | count | rows |\n|---|---:|---|\n"
            "| P1 | 2 | 8, 9 |\n",
            encoding="utf-8",
        )
        acceptance = _probe(
            hooks,
            scratch,
            "round10-acceptance-sets",
            "\n".join([
                "from _bearhug.evaluators.joinkey import evaluate_joinkey_lint",
                "from _bearhug.evaluators.task_durability import evaluate_task_durability",
                "from _bearhug.readers import board_for",
                f"c5_board = board_for({str(c5_root)!r})",
                "c5_rows = {row['row']: row for row in c5_board.rows()}",
                "c5_result = evaluate_joinkey_lint({}, event_id='R10-C5', "
                "board=c5_board, blocking=True)",
                f"c7_rows = board_for({str(c7_root)!r}).active_marker_rows()",
                f"c8_board = board_for({str(c8_root)!r})",
                "c8_counts = c8_board.cell_counts()",
                "c8_result = evaluate_joinkey_lint({}, event_id='R10-C8', "
                "board=c8_board, blocking=True)",
                "class Repo:",
                "    def doc_exists(self, path): return False",
                "    def board_exists(self): return True",
                "    def has_board_row(self, row): return False",
                "edit = json.dumps({'type': 'assistant', 'message': {'content': "
                "[{'type': 'tool_use', 'name': 'Edit', "
                "'input': {'file_path': 'internal/x.go'}}]}})",
                "board_write = json.dumps({'type': 'assistant', 'message': {'content': "
                "[{'type': 'tool_use', 'name': 'Write', "
                "'input': {'file_path': 'docs/superpowers/plans/BOARD.md'}}]}})",
                "withdrawals = {}",
                "for label, record in [('system', {'type': 'system'}), "
                "('summary', {'type': 'summary'}), ('missing', {})]:",
                "    record['message'] = {'content': "
                "'The TaskCreate tool is no longer available in this session'}",
                "    result = evaluate_task_durability(",
                "        {'stop_hook_active': False, 'transcript_path': '/x/session.jsonl'},",
                "        event_id='R10-F2-' + label, tasks={}, repo=Repo(),",
                "        transcript_lines=[edit, json.dumps(record), board_write],",
                "        transcript_readable=True)",
                "    withdrawals[label] = result.reason_code",
                "print(json.dumps({'c5_citations': c5_rows['99']['gated_by'],",
                "    'c5_blocked': c5_result.verdict == 'block' and "
                "'C5' in (c5_result.remediation or ''),",
                "    'c7_rows': c7_rows, 'withdrawals': withdrawals,",
                "    'c8_counts': c8_counts,",
                "    'c8_blocked': c8_result.verdict == 'block' and "
                "'C8' in (c8_result.remediation or '') and "
                "'row 8' in (c8_result.remediation or '') and "
                "'row 9' not in (c8_result.remediation or '')}))",
            ]),
        )
        acceptance_error = acceptance.get("error")
        findings.append(_finding(
            "C5 examines every live gate citation, not only the first.", "1",
            UNMEASURABLE if acceptance_error else (
                TRUE if acceptance.get("c5_citations") == ["100", "157"]
                and acceptance.get("c5_blocked") else FALSE
            ),
            acceptance_error or (
                f"citations={acceptance.get('c5_citations')}, "
                f"second-DONE citation blocked={acceptance.get('c5_blocked')}"
            ),
        ))
        findings.append(_finding(
            "C7 counts only a table data row whose first cell is a bare row id.", "1",
            UNMEASURABLE if acceptance_error else (
                TRUE if acceptance.get("c7_rows") == ["7"] else FALSE
            ),
            acceptance_error or f"active rows={acceptance.get('c7_rows')}",
        ))
        findings.append(_finding(
            "C8 counts cells with the host's splitter: a bare `|` in a cell blocks, the GFM "
            "escape `\\|` does not (Barracuda decision 0304).", "1",
            UNMEASURABLE if acceptance_error else (
                TRUE if acceptance.get("c8_counts") == [["8", 8], ["9", 7]]
                and acceptance.get("c8_blocked") else FALSE
            ),
            acceptance_error or (
                f"cell counts={acceptance.get('c8_counts')}, "
                f"row 8 blocked and row 9 not={acceptance.get('c8_blocked')}"
            ),
        ))
        expected_withdrawals = {
            "system": "board_only_tool_absent",
            "summary": "board_only_tool_absent",
            "missing": "board_only_tool_absent",
        }
        findings.append(_finding(
            "Task-tool withdrawal notices are recognised on every non-assistant record type.",
            "1",
            UNMEASURABLE if acceptance_error else (
                TRUE if acceptance.get("withdrawals") == expected_withdrawals else FALSE
            ),
            acceptance_error or f"reason codes={acceptance.get('withdrawals')}",
        ))

        got = _drive_adapter(hooks, scratch, _go_edit(scratch), config)
        blocked = bool(got["decision"]) and got["decision"].get("decision") == "block"
        gate_block = blocked and fault not in got["decision"]["reason"]
        findings.append(_finding(
            "The registered adapter blocks a .go edit with no dlv — a GATE block, not its own "
            "fault. (The positive control: without it, every row below is satisfied by an adapter "
            "that always blocks.)", "1",
            TRUE if gate_block else FALSE,
            f"exit {got['exit']}, decision "
            f"{(got['decision'] or {}).get('decision', 'silence')!r}",
        ))

        # The round-4 defect itself. One vendored module made unimportable; the fail-closed gates
        # are ruled to stop the turn, and the adapter is the layer that has to notice.
        target = hooks / "_bearhug" / "arbitrate.py"
        keep = target.read_bytes()
        target.write_text("raise RuntimeError('constructed')\n", encoding="utf-8")
        try:
            broken = _drive_adapter(hooks, scratch, _go_edit(scratch), config)
        finally:
            target.write_bytes(keep)
        own_fault = (
            bool(broken["decision"])
            and broken["decision"].get("decision") == "block"
            and fault in broken["decision"]["reason"]
        )
        findings.append(_finding(
            "A vendored module that cannot be imported STOPS the turn, naming the coordinator as "
            "the faulting component.", "5",
            TRUE if own_fault else FALSE,
            f"exit {broken['exit']}, decision "
            f"{(broken['decision'] or {}).get('decision', 'SILENCE — the turn was allowed')!r}",
        ))

        # --- where a REAL install writes, not where a redirect writes ----------------------
        #
        # Round 5's PRIMARY finding, and it was structural: `observability_root` had exactly ONE
        # caller in the whole package — this file — which pointed it at scratch and then measured
        # the redirect. So the §7 row said "1 events.jsonl under telemetry/" and said nothing at
        # all about the installed path, while a real Stop wrote five records per turn into
        # `<repo>/telemetry/`, untracked and un-ignored in the target's working tree.
        #
        # Measured now by driving the adapter with BOTH env vars pointed at scratch and looking
        # for strays in the project tree, which is the only arrangement that can see the default.
        _drive_adapter(hooks, scratch, _go_edit(scratch), config)
        # `obs/` is the IN-PROCESS driver's deliberate redirect, used by the constructed-registry
        # claims above. Excluded by name rather than by glob accident, so the exclusion is
        # reviewable: what this looks for is telemetry at the path the DEFECT produced, which is
        # `<project>/telemetry/<date>/events.jsonl` written by the adapter's own defaults.
        redirect = scratch / "obs"
        declared = scratch / ".bearhug" / "telemetry" / "v1"
        strays = [
            str(path.relative_to(scratch))
            for path in scratch.rglob("events.jsonl")
            if config not in path.parents
            and redirect not in path.parents
            and ".bearhug" not in path.relative_to(scratch).parts
        ]
        # D08 (2026-09-01): telemetry is project-based, at <repo>/.bearhug/telemetry/v1. The
        # round-5 guard survives in its sharpened form: nothing may land in the tree OUTSIDE that
        # one declared, ignored directory, and nothing may land in the config dir any more.
        findings.append(_finding(
            "A real adapter run writes telemetry ONLY under <repo>/.bearhug/telemetry/ — nowhere "
            "else in the working tree, and not in the config dir.", "7",
            TRUE if not strays and not list(config.rglob("events.jsonl")) else FALSE,
            "no events.jsonl under the project root outside .bearhug/, none under the config dir"
            if not strays else f"telemetry landed IN THE TREE outside .bearhug/ at {strays}",
        ))

        landed = list(declared.rglob("events.jsonl")) if declared.exists() else []
        findings.append(_finding(
            "Telemetry lands at the path RUNTIME-PROTOCOL declares: "
            "<repo>/.bearhug/telemetry/v1/YYYY-MM-DD/events.jsonl.", "7",
            TRUE if landed else FALSE,
            f"{len(landed)} events.jsonl under {declared.relative_to(scratch)}/",
        ))

        # The other half of the D08 guard: the HOST must ignore .bearhug/, or the records this
        # run just proved land inside the tree become trackable. Measured on the host's own
        # .gitignore beside this run; the package carries the line as
        # patches/gitignore-candidate.txt.
        host_ignore = Path.cwd() / ".gitignore"
        shipped_line = (package_root / "patches" / "gitignore-candidate.txt").is_file()
        if not host_ignore.is_file():
            findings.append(_finding(
                "The host's .gitignore lists .bearhug/, so project-based telemetry can never be "
                "tracked.", "7", UNMEASURABLE,
                f"no .gitignore beside this run; the package ships the line "
                f"(patches/gitignore-candidate.txt present={shipped_line}). A Barracuda-owned "
                "session running from the repo root measures it", tier=TIER_REAL,
            ))
        else:
            ignored = any(
                line.strip() in (".bearhug/", ".bearhug", "/.bearhug/", "/.bearhug")
                for line in host_ignore.read_text(encoding="utf-8", errors="replace").splitlines()
            )
            findings.append(_finding(
                "The host's .gitignore lists .bearhug/, so project-based telemetry can never be "
                "tracked.", "7", TRUE if ignored else FALSE,
                f".gitignore {'lists' if ignored else 'DOES NOT list'} .bearhug/ "
                f"(shipped line present={shipped_line})", tier=TIER_REAL,
            ))

        stamps = scratch / ".automation-stamps"
        findings.append(_finding(
            "Stamps still land beside the repository, where the captured gates write them and "
            "automation-status.sh reads them. The two roots are separate.", "8",
            TRUE if stamps.is_dir() and not list(config.rglob(".automation-stamps")) else FALSE,
            f".automation-stamps/ present={stamps.is_dir()}, "
            f"none under the config dir={not list(config.rglob('.automation-stamps'))}",
        ))

        malformed = _drive_adapter(hooks, scratch, b"this is not json", config)
        findings.append(_finding(
            "Malformed stdin stays FAIL-OPEN, which is the one thing D04 rules the other way.",
            "5",
            TRUE if malformed["decision"] is None and malformed["exit"] == 0 else FALSE,
            f"exit {malformed['exit']}, decision "
            f"{(malformed['decision'] or {}).get('decision', 'silence')!r}",
        ))

        # Missing boardrows belongs to joinkey-lint's ruled evaluator failure domain. Round 9
        # proved the eager reader escaped above coordinator.run, converting fail-open into an
        # adapter-fault block and leaving every stamp stale.
        boardrows_copy = hooks / "boardrows.py"
        saved_boardrows = boardrows_copy.read_bytes()
        boardrows_copy.unlink()
        for cache in (hooks / "__pycache__").glob("boardrows*.pyc") \
                if (hooks / "__pycache__").is_dir() else ():
            cache.unlink()
        try:
            missing = _drive_adapter(
                hooks,
                scratch,
                {"stop_hook_active": True, "transcript_path": "", "session_id": "R10-F5"},
                config,
            )
        finally:
            boardrows_copy.write_bytes(saved_boardrows)
        joinkey_stamp = scratch / ".automation-stamps" / "joinkey-lint"
        stamp_verdict = (
            joinkey_stamp.read_text(encoding="utf-8").split(maxsplit=1)[1].strip()
            if joinkey_stamp.is_file()
            else "<missing>"
        )
        findings.append(_finding(
            "Missing boardrows is adapted inside joinkey-lint under D04, not by the adapter.",
            "5",
            TRUE if missing["decision"] is None
            and stamp_verdict == "CRASH MissingBoardRows" else FALSE,
            f"decision={(missing['decision'] or {}).get('decision', 'silence')!r}, "
            f"joinkey stamp={stamp_verdict!r}",
        ))

    # --- sections 1 and 4: arbitration over MORE THAN ONE evaluator -----------------------------
    #
    # The coordinator exists because D01 measured six fixtures each drawing two or more independent
    # blocks, one drawing five. Its falsifier could not fail on that until now.
    three = (
        "def make(gate, reason, remediation):\n"
        "    def call(event, *, event_id=None, **kw):\n"
        "        return EvaluatorResult.blocked(gate_id=gate, gate_version='1.0.0',\n"
        "            event_id=event_id, reason_code=reason, remediation=remediation,\n"
        "            duration_ms=0.0)\n"
        "    return call\n"
        "rows = [('dlv-verify-gate', 'dlv_session_missing', 'RUN-DLV'),\n"
        "        ('review-gate', 'adversarial_review_missing', 'GET-REVIEW'),\n"
        "        ('response-shape', 'multi_ask', 'ONE-QUESTION')]\n"
        "registry = [EvaluatorEntry(i, g, make(g, r, m), (), 'keyword', '1.0.0')\n"
        "            for i, (g, r, m) in enumerate(rows)]"
    )
    event = {"stop_hook_active": False, "transcript_path": "", "session_id": "S"}
    combined = _drive(hooks, scratch, three, event)
    combined_claim = (
        "Three gates blocking on ONE event produce ONE decision: the work-adding remediations "
        "combined in ordinal order, the shape deferred and NAMED rather than dropped."
    )
    broke = _driver_failed(combined, combined_claim, "4")
    reason = json.loads(combined["stdout"])["reason"] if combined.get("stdout") else ""
    work_both = "RUN-DLV" in reason and "GET-REVIEW" in reason
    ordered = reason.find("RUN-DLV") < reason.find("GET-REVIEW") if work_both else False
    deferred_named = "response_form" in reason and "not waived" in reason
    shape_suppressed = "ONE-QUESTION" not in reason
    findings.append(broke or _finding(
        combined_claim, "4",
        TRUE if (work_both and ordered and deferred_named and shape_suppressed) else FALSE,
        f"work_combined={work_both} ordinal_order={ordered} deferred_named={deferred_named} "
        f"shape_text_suppressed={shape_suppressed}",
    ))

    reversed_registry = three.replace(
        "for i, (g, r, m) in enumerate(rows)]",
        "for i, (g, r, m) in enumerate(rows)][::-1]",
    )
    flipped = _drive(hooks, scratch, reversed_registry, event)
    tiebreak_claim = (
        "The tie-break is TOTAL: reversing the registry order does not change one byte of the "
        "decision."
    )
    findings.append(_driver_failed(flipped, tiebreak_claim, "4") or _finding(
        tiebreak_claim, "4",
        TRUE if flipped.get("stdout") == combined.get("stdout") else FALSE,
        "identical bytes" if flipped.get("stdout") == combined.get("stdout")
        else "the decision changed with input order",
    ))

    # --- section 5: `error` is distinct from `block` AND from `pass` -----------------------------
    #
    # Round 4: this script collapsed every run to "block if stdout", which cannot see the
    # difference. Fail-open governs the DECISION and never the RECORDED VERDICT.
    telemetry_before = {
        path: path.read_text(encoding="utf-8")
        for path in (scratch / "obs" / "telemetry").rglob("events.jsonl")
    }
    code = _RAISER + (
        "registry = [EvaluatorEntry(0, 'dlv-verify-gate', raising, (), 'keyword', '1.0.0')]"
    )
    errored = _drive(hooks, scratch, code, event)
    verdict_claim = (
        "A fail-closed gate that CRASHED blocks the turn while its RECORDED verdict stays "
        "`error` — the policy governs the decision, never the record."
    )
    verdict_broke = _driver_failed(errored, verdict_claim, "5")
    records = []
    for path in (scratch / "obs" / "telemetry").rglob("events.jsonl"):
        old = telemetry_before.get(path, "")
        for line in path.read_text(encoding="utf-8")[len(old):].splitlines():
            if line.strip():
                records.append(json.loads(line))
    # The verdict is nested under `result`, which is null on a not_reached slot. Reading the top
    # level silently produced an empty set and a MEASURED-FALSE against a correct package — the
    # falsifier failing open, which is the one thing it may not do.
    verdicts = {
        (r.get("result") or {}).get("verdict")
        for r in records
        if isinstance(r.get("result"), dict)
    }
    findings.append(verdict_broke or _finding(
        verdict_claim, "5",
        TRUE if (errored.get("stdout") and verdicts == {"error"}) else FALSE,
        f"decision={'block' if errored.get('stdout') else 'silence'}, "
        f"recorded verdict(s)={sorted(v for v in verdicts if v)}",
    ))

    # --- section 8: the anomalies the table names as MUST-PRESERVE -------------------------------
    anomalies = {
        "pass-no-tasks-no-edits": ("task-durability", "no_tasks_no_edits", "passed"),
        "PASS-tags-ok": ("task-durability", "tags_ok", "passed"),
    }
    for expected, (gate, reason_code, constructor) in anomalies.items():
        code = (
            f"def emit(event, *, event_id=None, **kw):\n"
            f"    return EvaluatorResult.{constructor}(gate_id={gate!r}, gate_version='1.0.0',\n"
            f"        event_id=event_id, reason_code={reason_code!r}, duration_ms=0.0)\n"
            f"registry = [EvaluatorEntry(0, {gate!r}, emit, (), 'keyword', '1.0.0')]"
        )
        anomaly_claim = (
            f"The projection emits the gate's exact string {expected!r}, not a tidied one."
        )
        anomaly_broke = _driver_failed(_drive(hooks, scratch, code, event), anomaly_claim, "8")
        stamp_file = scratch / "obs" / ".automation-stamps" / gate
        written = stamp_file.read_text(encoding="utf-8").split(maxsplit=1)[1].strip() \
            if stamp_file.is_file() else "<no stamp>"
        findings.append(anomaly_broke or _finding(
            anomaly_claim, "8", TRUE if written == expected else FALSE, f"stamped {written!r}",
        ))

    # --- what this environment cannot measure -------------------------------------------------
    findings.append(_finding(
        "The hash algorithm is settled as v1.", "identity",
        TRUE if manifest["hash_algorithm_status"] == "approved" else FALSE,
        f"manifest says hash_algorithm_status={manifest['hash_algorithm_status']!r}; R02's three "
        "questions were ruled 2026-08-31 (decision 0299, accepted), reported to bear-hug in the "
        "round-4 return packet. bear-hug has NOT read that record; a Barracuda-owned session "
        "should confirm the citation is real before accepting the ADR",
    ))
    # Written as UNMEASURABLE while `_boardrows_source` had already FOUND the file and copied it
    # into the scratch install — which is the only reason the adapter rows measured at all. An
    # unmeasurable count is the number a reviewer uses to decide where to spend attention, and
    # this one sent round 5 to a claim answered three hundred lines above.
    if boardrows is None:
        findings.append(_finding(
            "scripts/hooks/boardrows.py exists in the target repository.", "6", UNMEASURABLE,
            "not found relative to this run; a Barracuda-owned session running from the repo root "
            "measures it",
        ))
    else:
        size = boardrows.stat().st_size
        findings.append(_finding(
            "scripts/hooks/boardrows.py exists in the target repository.", "6",
            TRUE if size > 0 else FALSE,
            f"found at {boardrows} — {size} bytes. The runtime's ONE non-stdlib import, validated "
            "when joinkey-lint reads the board, raising MissingBoardRows inside evaluation so "
            "D04 records and applies the gate's ruled failure policy",
        ))

    # Three files, one `json.load` each. `_boardrows_source` already established the cwd-relative
    # pattern this needed.
    layers = [
        ("project", Path.cwd() / ".claude" / "settings.json"),
        ("project-local", Path.cwd() / ".claude" / "settings.local.json"),
        ("ambient", Path(os.environ.get("CLAUDE_CONFIG_DIR")
                         or Path.home() / ".claude") / "settings.json"),
    ]
    read = []
    for label, path in layers:
        if not path.is_file():
            read.append((label, "absent", 0))
            continue
        try:
            hooks_key = json.loads(path.read_text(encoding="utf-8")).get("hooks") or {}
        except ValueError:
            read.append((label, "unparseable", -1))
            continue
        stop = hooks_key.get("Stop") or []
        read.append((label, "present" if hooks_key else "no hooks key",
                     sum(len(group.get("hooks") or []) for group in stop)))
    project_layer = next(r for r in read if r[0] == "project")
    others_quiet = all(count == 0 for label, _, count in read if label != "project")
    findings.append(_finding(
        "Only the project settings layer registers a Stop hook; the local and ambient layers "
        "carry none, so the migration is not incomplete on that axis.", "9",
        TRUE if (others_quiet and project_layer[2] >= 0) else FALSE,
        "; ".join(f"{label}: {state}, {count} Stop command(s)" for label, state, count in read),
    ))
    # LAST, because it is the slowest and by far the most valuable: 0209 tier 1, real code.
    findings.extend(_differential(hooks, scratch))

    # --- this file's own isolation, measured rather than promised ------------------------------
    #
    # Round 6's rejection. `config_dir` was an optional keyword and one of four call sites passed
    # it, so the falsifier wrote 850 records into the reviewer's real `~/.claude` store — five per
    # run, including five carrying a `gate_id` from a lab fixture that exists nowhere in the
    # package. The docstring said it writes into a temp dir and removes it.
    #
    # A claim rather than a comment, because that is this file's entire argument: measure it, do
    # not assert it. It is the LAST claim so it covers every subprocess above.
    after = _store_census(real_store)
    after_project = _store_census(real_project_store)
    clean = after == before and after_project == before_project
    findings.append(_finding(
        "This verification wrote NOTHING into the machine's real telemetry stores — neither the "
        "legacy ~/.claude store nor this repository's own .bearhug/telemetry. Every subprocess it "
        "starts resolves its roots from scratch.", "7",
        TRUE if clean else FALSE,
        f"legacy {real_store}: {before[1]} record(s) in {before[0]} file(s) before, {after[1]} in "
        f"{after[0]} after; project {real_project_store}: {before_project[1]} before, "
        f"{after_project[1]} after"
        + ("" if clean else " — THIS RUN LEAKED into a production store"),
        tier=TIER_REAL,
    ))

    shutil.rmtree(scratch, ignore_errors=True)
    return findings


def render(findings: list[dict[str, str]]) -> str:
    lines = ["ADR claim verification — measured, not read", ""]
    for row in findings:
        lines.append(
            f"[{row['verdict']:18s}] §{row['section']:<8s} "
            f"({row.get('tier', TIER_ARTIFACT)}) {row['claim']}"
        )
        lines.append(f"{'':21s}{row['evidence']}")
    false = [f for f in findings if f["verdict"] == FALSE]
    unmeasurable = [f for f in findings if f["verdict"] == UNMEASURABLE]
    lines.append("")
    lines.append(
        f"{len(findings)} claims: {len(findings) - len(false) - len(unmeasurable)} measured true, "
        f"{len(false)} measured FALSE, {len(unmeasurable)} unmeasurable here."
    )
    injected = [f for f in findings if f.get("tier") == TIER_INJECTED]
    if injected:
        lines.append(
            f"{len(injected)} of these are TIER 3 — a constructed evaluator injected into a "
            "throwaway registry. Decision 0209 ranks that the weakest form of proof and requires "
            "it be stated as weak rather than reported as proof. Treat those greens as cheap."
        )
    if false:
        lines.append("A MEASURED-FALSE claim is a defect in the package, not in this script.")
    return "\n".join(lines)


if __name__ == "__main__":
    # Runnable as `python3 verify.py` from the package root, with a bare interpreter and no venv —
    # the same constraint the vendored runtime is held to. Round 4 was asked to "run verify.py" and
    # there was no way to run it; the reviewer had to import it.
    #
    # Exit 1 on any MEASURED-FALSE so it can sit in a pipeline. UNMEASURABLE-HERE is exit 0: a
    # claim this environment cannot reach is a fact about the environment, and treating it as a
    # failure would train a reader to ignore the word.
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    measured = verify(root)
    print(render(measured))
    sys.exit(1 if any(f["verdict"] == FALSE for f in measured) else 0)
