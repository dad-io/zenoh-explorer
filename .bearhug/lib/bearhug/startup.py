"""Prepare an ordinary terminal session; preparation is never journey acceptance.

Reuse the existing command implementations in isolated child processes so every imported path
is bound to the same subject and private artifact directory. No provider is launched here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
import zipfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from bearhug.paths import CLAUDE_HOME, REPO_ROOT, RUNS_DIR, frozen_corpus, transcripts_dir
from bearhug.prime import inspect_git
from bearhug.snapshot import compute_drift
from bearhug.snapshot.capture import live_hashes
from bearhug.snapshot.toolchain import manifest_toolchain_block


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _source_digest() -> str:
    files = [*REPO_ROOT.glob("src/bearhug/**/*.py"), *REPO_ROOT.glob("runtime/**/*.py")]
    files.append(REPO_ROOT / "patches/promotion-package/manifest.json")
    return hashlib.sha256(
        json.dumps({str(p.relative_to(REPO_ROOT)): _sha(p) for p in sorted(files)}).encode()
    ).hexdigest()


def _snapshot_digest(snapshot: Path) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                str(p.relative_to(snapshot)): _sha(p)
                for p in sorted(snapshot.rglob("*"))
                if p.is_file()
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _select_snapshot(given: str, base: Path) -> Path:
    candidate = Path(given).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    matches = []
    for directory in [*(REPO_ROOT / "snapshots").glob("*"), *base.glob("*/snapshots/*")]:
        try:
            if given in {directory.name, _json(directory / "manifest.json").get("snapshot_id")}:
                matches.append(directory.resolve())
        except (OSError, ValueError):
            continue
    if len(set(matches)) != 1:
        raise ValueError("snapshot must resolve uniquely; supply its exact directory path")
    return matches[0]


def measurement_steps(snapshot: Path, *, frozen: bool, live: bool) -> list[tuple]:
    """Runbook measurement checks; Stop census precedes its nag/gate consumer."""
    selected = str(snapshot)
    rows = [
        ("lint", ["lint", selected, "--format", "json"], "findings/lint-*.json"),
        (
            "observability",
            ["lint", selected, "--observability"],
            "reports/rule-observability-*.json",
        ),
        ("hook-audit", ["hooks", "audit", selected], "findings/hooks-*.json"),
        ("stop-census", ["hooks", "stop-census", selected], "reports/stop-contract-*.json"),
        (
            "write-detection",
            ["hooks", "write-detection", selected],
            "reports/write-detection-*.json",
        ),
        ("run-configs", ["lint", selected, "--run-configs"], "findings/runconfig-*.json"),
    ]
    if frozen:
        rows.extend(
            [
                ("nag-vs-gate", ["lint", selected, "--nag-vs-gate"], "reports/nag-vs-gate-*.json"),
                (
                    "frozen-replay",
                    ["replay", "report", "--corpus", "frozen", "--snapshot", selected],
                    "findings/replay-frozen-*.json",
                ),
                (
                    "frozen-report",
                    ["report", selected, "--corpus", "frozen"],
                    "findings/report-frozen-*.json",
                ),
            ]
        )
    if live:
        rows.extend(
            [
                (
                    "live-replay",
                    ["replay", "report", "--corpus", "live", "--snapshot", selected],
                    "findings/replay-live-*.json",
                ),
                (
                    "live-report",
                    ["report", selected, "--corpus", "live"],
                    "findings/report-live-*.json",
                ),
            ]
        )
    return rows


def _missing_measurement_input(name: str, snapshot: Path) -> str | None:
    project = snapshot / "project"
    if name in {"lint", "observability"} and not (project / "CLAUDE.md").is_file():
        return "No project CLAUDE.md was captured; instruction checks are unavailable."
    if name == "run-configs" and not any((project / ".idea/runConfigurations").glob("*.xml")):
        return "No GoLand run configurations were captured; IDE checks are unavailable."
    return None


def _run(argv: list[str], *, root: Path, env: dict, name: str, timeout: float) -> dict:
    log = root / f"{name}.log"
    started = time.monotonic()
    print(f"  {name}…", flush=True)
    with log.open("wb") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "bearhug.cli", *argv],
            cwd=REPO_ROOT,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
            status = "completed" if code == 0 else "failed"
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            # The command may have fixture children. Stop only this startup-owned process group.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            if isinstance(exc, KeyboardInterrupt):
                raise
            code, status = None, "timed-out"
    return {
        "name": name,
        "argv": argv,
        "status": status,
        "returncode": code,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "log": str(log),
    }


def _reuse(stage: dict, previous: Path, root: Path) -> bool:
    """Reopen every recorded output before crediting or copying a cached measurement."""
    outputs = stage.get("outputs", {})
    if stage.get("status") not in {"completed", "findings", "reused"} or not outputs:
        return False
    for relative, digest in outputs.items():
        path = previous / relative
        if not path.resolve().is_relative_to(previous.resolve()) or not path.is_file():
            return False
        if _sha(path) != digest:
            return False
    for relative in outputs:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(previous / relative, target)
    return True


def _has_evidence(root: Path, pattern: str, snapshot_id: str) -> bool:
    for path in root.glob(pattern):
        try:
            data = _json(path)
            if data.get("snapshot_id", data.get("snapshot")) == snapshot_id:
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def _corpora(subject: Path) -> tuple[bool, bool, dict]:
    source = transcripts_dir(subject)
    live = any(source.glob("*.jsonl"))
    archive = frozen_corpus().path
    frozen = False
    identity = {"path": str(archive), "sha256": None}
    if archive.is_file():
        identity["sha256"] = _sha(archive)
        try:
            with zipfile.ZipFile(archive) as bundle:
                frozen = any(
                    source.name in Path(name).parts and name.endswith(".jsonl")
                    for name in bundle.namelist()
                )
        except zipfile.BadZipFile:
            pass
    return frozen, live, identity


def _binding(args, subject: Path, root: Path) -> tuple[list[str], dict]:
    if args.provider_work_observation:
        from bearhug.replay.cockpit_work import provider_work_for_cockpit

        checked = provider_work_for_cockpit(
            subject_root=subject,
            provider_work_observation=args.provider_work_observation,
            work_binding=args.work_binding,
        )
        return [
            "--provider-work-observation",
            str(Path(args.provider_work_observation).resolve()),
            "--work-binding",
            str(Path(args.work_binding).resolve()),
        ], {"status": checked["status"], "reason": checked["reason"]}
    if not any((args.session, args.task_id, args.active_plan, args.board_row)) and (
        subject / "scripts/bin/bearhug-work"
    ).is_file():
        from bearhug.project_work import status as project_work_status

        work = project_work_status(subject)
        return [], {
            "status": "error" if work["status"] == "error" else "managed",
            "reason": work["reason"] or "Project tasks and native session parity are read live",
        }
    if not (args.session and args.task_id and args.active_plan and args.board_row):
        return [], {
            "status": "unavailable",
            "reason": "Task authority needs an exact session, task id, active plan and BOARD row. "
            "Use --configure once they exist; startup does not invent tasks or select the backlog.",
        }
    from bearhug.providers.claude_work import ingest_claude_task_store
    from bearhug.providers.work_store import (
        authority_candidate,
        persist_work_authority_artifacts,
        prepare_work_authority_artifacts,
    )

    now = datetime.now(UTC)
    observed = ingest_claude_task_store(CLAUDE_HOME / "tasks" / args.session, observed_at=now)
    candidate = authority_candidate(subject, args.board_row)
    artifacts = prepare_work_authority_artifacts(
        observation=observed,
        subject=subject,
        task_id=args.task_id,
        active_plan_path=args.active_plan,
        candidates=[candidate],
        created_at=now,
    )
    persisted = persist_work_authority_artifacts(artifacts, output_root=root)
    return [
        "--provider-work-observation",
        str(persisted.observation_path),
        "--work-binding",
        str(persisted.binding_path),
    ], {
        "status": artifacts.binding["resolution"],
        "observation": str(persisted.observation_path),
        "binding": str(persisted.binding_path),
    }


def _validate_args(args) -> None:
    for field in ("session", "task_id"):
        value = getattr(args, field)
        if value and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) or value in {".", ".."}
        ):
            raise ValueError(f"--{field.replace('_', '-')} must be an exact identifier, not a path")
    if bool(args.active_plan) != bool(args.board_row):
        raise ValueError("--active-plan and --board-row must be supplied together")
    if bool(args.provider_work_observation) != bool(args.work_binding):
        raise ValueError("--provider-work-observation and --work-binding must be supplied together")
    if args.task_id and not (args.session and args.active_plan):
        raise ValueError("--task-id requires --session, --active-plan and --board-row")
    if args.task_id and args.provider_work_observation:
        raise ValueError("choose an existing work binding or an exact Claude task, not both")
    if not 1 <= args.step_timeout <= 600:
        raise ValueError("--step-timeout must be between 1 and 600 seconds")


def _readiness_checks(readiness: dict):
    for name, check in readiness.items():
        if not isinstance(check, dict):
            continue
        if "status" in check:
            yield name, check
        else:
            for child, value in check.items():
                if isinstance(value, dict) and "status" in value:
                    yield f"{name}/{child}", value


def readiness_lines(readiness: dict) -> list[str]:
    return [
        f"  {name}: {check['status']} — {check.get('reason', '')}"
        for name, check in _readiness_checks(readiness)
    ]


def _startup_findings(root: Path, report: dict) -> None:
    """Carry preparation gaps into the existing findings sheet with their evidence path."""
    from bearhug.model import Evidence, Finding, Severity
    from bearhug.report import write_findings

    rows = []
    messages = [("journey", "Operational journey is unverified; startup only prepares evidence.")]
    messages += [(f"gap-{i}", gap) for i, gap in enumerate(report["gaps"])]
    for stage in report["stages"]:
        if stage["status"] in {"failed", "timed-out"}:
            messages.append((stage["name"], f"Startup check {stage['name']}: {stage['status']}"))
    for name, check in _readiness_checks(report["readiness"]):
        if check.get("status") in {"missing", "stale", "error"}:
            messages.append((
                name.replace("/", "-"), f"{name}: {check['status']} — {check.get('reason', '')}"
            ))
    if report["binding"]["status"] != "bound":
        messages.append(("task-authority", f"Task authority: {report['binding']['status']}"))
    for key, message in messages:
        rows.append(
            Finding(
                id=f"startup-{key}",
                check="STARTUP-READINESS",
                severity=Severity.INFO,
                summary=message,
                snapshot=report["snapshot_id"],
                evidence=(Evidence(file=str(root / "startup.json")),),
                limit="Preparation observation at startup; not a code defect or journey verdict.",
            )
        )
    write_findings(
        rows, out_dir=root / "findings", snapshot_id=report["snapshot_id"], prefix="startup"
    )


def _telemetry_stage(subject: Path, root: Path, snapshot_id: str) -> None:
    """Read-only Stop-telemetry findings, next to lint/hook-audit's own `findings/*.json`.

    Reads the LIVE subject's `.bearhug/telemetry/v1/` (coordinator telemetry is never captured
    into a snapshot), so this runs directly rather than through `measurement_steps`' subprocess
    stages, which all operate on the frozen snapshot. See `bearhug.telemetry_findings`.
    """
    from bearhug.telemetry_findings import write_telemetry_findings

    path = write_telemetry_findings(subject, root, snapshot_id=snapshot_id)
    print(f"  telemetry-findings: {path.relative_to(root)}", flush=True)


def run_startup(args) -> int:
    _validate_args(args)
    git = inspect_git(args.subject)
    subject = Path(git.worktree)
    if args.dry_run:
        print(f"Project: {subject}\nHEAD: {git.head}\n")
        print(
            "Check installed runtime, Claude hooks/MCP, MemQ/Graft, and explicit session evidence."
        )
        print(
            "Reuse verified measurements or capture this worktree "
            "and run applicable runbook measurement checks."
        )
        for name, argv, _ in measurement_steps(Path("<selected-snapshot>"), frozen=True, live=True):
            print(f"  {name}: bearhug {' '.join(argv)}")
        print("Frozen/live corpus stages run only with transcripts belonging to this worktree.")
        print("Bind the exact supplied task/authority; report gaps; open TUI unless --no-tui.")
        print("No files written, provider launched, service started, or installation changed.")
        return 0

    from bearhug.startup_readiness import inspect_readiness

    key = hashlib.sha256(str(subject).encode()).hexdigest()[:20]
    base = RUNS_DIR / "startup" / key
    if base.resolve().is_relative_to(subject):
        raise ValueError(
            "startup evidence must be outside the selected project worktree: "
            f"{base} is inside {subject}. There is no --state-root for startup; Bear "
            f"Hug's runs directory ({RUNS_DIR}) set this path. Clone Bear Hug, and its "
            "runs directory, as a sibling of the project, or set BEARHUG_ARTIFACT_ROOT "
            "to a directory outside it."
        )
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    root = base / stamp
    root.mkdir(mode=0o700)
    print(f"Project: {subject}\nEvidence: {root}\nChecking readiness…", flush=True)
    readiness = inspect_readiness(subject, session_id=args.session)
    frozen, live, corpus = _corpora(subject)
    inputs = {
        "subject": str(subject),
        "head": git.head,
        "dirty_sha256": git.dirty_sha256,
        "harness": live_hashes(subject, CLAUDE_HOME),
        "bearhug": _source_digest(),
        "toolchain": manifest_toolchain_block(),
        "frozen_corpus": corpus,
    }
    previous = {}
    try:
        previous_root = Path(_json(base / "latest.json")["root"])
        if previous_root.resolve().is_relative_to(base.resolve()):
            previous = _json(previous_root / "startup.json")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    reusable = not args.refresh and previous.get("inputs") == inputs
    snapshot = None
    if args.snapshot:
        snapshot = _select_snapshot(args.snapshot, base)
        same_snapshot = previous.get("snapshot") == str(snapshot)
        if same_snapshot and _snapshot_digest(snapshot) != previous.get("snapshot_digest"):
            raise ValueError("selected snapshot contents changed; omit --snapshot to recapture")
        reusable = reusable and same_snapshot
    elif reusable:
        snapshot = Path(previous["snapshot"])
        if _snapshot_digest(snapshot) != previous.get("snapshot_digest"):
            snapshot, reusable = None, False
    if snapshot is not None:
        manifest = _json(snapshot / "manifest.json")
        drift = compute_drift(snapshot, barracuda_root=subject, claude_home=CLAUDE_HOME)
        manifest_subject = manifest.get("subject", {})
        # ``barracuda`` is the persisted legacy key; migration may emit ``project``. Accept both
        # while new captures and callers converge on the neutral project vocabulary.
        manifest_project = manifest_subject.get("project") or manifest_subject.get("barracuda")
        same_subject = (
            isinstance(manifest_project, dict)
            and Path(manifest_project["root"]).resolve() == subject
        )
        if not same_subject or drift.moved or drift.head_moved:
            if args.snapshot:
                raise ValueError(
                    "selected snapshot is stale or belongs to another worktree; "
                    "omit --snapshot to refresh"
                )
            snapshot, reusable = None, False
    env = {
        **os.environ,
        "BEARHUG_PROJECT_ROOT": str(subject),
        # Compatibility for older child commands; new consumers use BEARHUG_PROJECT_ROOT.
        "BEARHUG_BARRACUDA_ROOT": str(subject),
        "CLAUDE_PROJECT_DIR": str(subject),
        "BEARHUG_ARTIFACT_ROOT": str(root),
    }
    # Preserve the source archive while corpus manifests are isolated under this run.
    env["BEARHUG_FROZEN_CORPUS"] = str(frozen_corpus().path)
    stages = []
    if snapshot is None:
        result = _run(
            ["snapshot", "--date", stamp],
            root=root,
            env=env,
            name="snapshot",
            timeout=args.step_timeout,
        )
        stages.append(result)
        snapshot = root / "snapshots" / stamp
        if result["status"] != "completed" or not (snapshot / "manifest.json").is_file():
            raise ValueError(f"snapshot failed; inspect {result['log']}")
    snapshot_id = _json(snapshot / "manifest.json")["snapshot_id"]
    old_stages = {stage["name"]: stage for stage in previous.get("stages", [])}
    for name, argv, pattern in measurement_steps(snapshot, frozen=frozen, live=live):
        missing = _missing_measurement_input(name, snapshot)
        if missing:
            stages.append({"name": name, "argv": argv, "status": "unavailable", "reason": missing})
            print(f"  {name}: unavailable — {missing}", flush=True)
            continue
        old = old_stages.get(name, {})
        if reusable and not name.startswith("live-") and _reuse(old, previous_root, root):
            result = {
                **old,
                "status": "reused",
                "original_status": old.get("original_status", old["status"]),
                "reused_from": str(previous_root),
                "argv": argv,
            }
            print(f"  {name}: reused verified evidence", flush=True)
        else:
            before = {
                p for folder in ("findings", "reports", "corpus") for p in (root / folder).glob("*")
            }
            result = _run(argv, root=root, env=env, name=name, timeout=args.step_timeout)
            outputs = [
                p
                for folder in ("findings", "reports", "corpus")
                for p in (root / folder).glob("*")
                if p.is_file() and p not in before
            ]
            result["outputs"] = {str(p.relative_to(root)): _sha(p) for p in outputs}
            produced = _has_evidence(root, pattern, snapshot_id)
            # Aggregated reports repeat lint/hook rows; frozen replay is historical calibration.
            # Keep both available for inspection without double-counting them as live findings.
            for artifact in outputs:
                if artifact.parent.name == "findings" and (
                    artifact.name.startswith("report-")
                    or (live and artifact.name.startswith("replay-frozen-"))
                ):
                    destination = root / "reports" / "supporting" / artifact.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = result["outputs"].pop(str(artifact.relative_to(root)))
                    artifact.replace(destination)
                    result["outputs"][str(destination.relative_to(root))] = digest
            if result["returncode"] == 1 and produced:
                result["status"] = "findings"
            elif result["status"] == "completed" and not produced:
                result["status"] = "failed"
                result["reason"] = "command produced no expected evidence artifact"
            print(f"    {result['status']}: {result['log']}", flush=True)
        stages.append(result)
    gaps = list(dict.fromkeys(s["reason"] for s in stages if s["status"] == "unavailable"))
    if not frozen:
        gaps.append(
            "No frozen transcripts for this exact worktree: "
            "frozen replay and nag/gate rates unavailable."
        )
    if not live:
        gaps.append(
            "No Claude replay transcripts for this exact worktree yet; "
            "Codex lifecycle observations are reported separately in the dashboard."
        )
    try:
        binding_args, binding = _binding(args, subject, root)
    except (OSError, ValueError) as exc:
        binding_args, binding = [], {"status": "error", "reason": str(exc)}
    tui_args = ["tui", str(subject), "--snapshot", str(snapshot), *binding_args]
    if getattr(args, "web", False):
        tui_args.append("--web")
    if args.session:
        tui_args += ["--session", args.session]
    if args.active_plan:
        tui_args += [
            "--authority-scope",
            "bound",
            "--active-plan",
            args.active_plan,
            "--board-row",
            args.board_row,
        ]
    final_drift = compute_drift(snapshot, barracuda_root=subject, claude_home=CLAUDE_HOME)
    if final_drift.moved or final_drift.head_moved:
        gaps.append("Project changed during preparation; measurements are stale. Rerun startup.")
    final_git = inspect_git(subject)
    changed_during_startup = (
        final_git.head != git.head or final_git.dirty_sha256 != git.dirty_sha256
    )
    if changed_during_startup:
        gaps.append("Worktree edits changed during preparation; rerun before crediting readiness.")
    attention = (
        any(
            s["status"] in {"failed", "timed-out", "findings"}
            or s.get("original_status") == "findings"
            for s in stages
        )
        or any(
            check.get("status") in {"missing", "stale", "error"}
            for _, check in _readiness_checks(readiness)
        )
        or binding["status"] in {"error", "unavailable", "ambiguous", "missing"}
        and bool(args.task_id or args.provider_work_observation)
        or final_drift.moved
        or final_drift.head_moved
        or changed_during_startup
    )
    report = {
        "subject": str(subject),
        "head": git.head,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "snapshot": str(snapshot),
        "snapshot_id": snapshot_id,
        "snapshot_digest": _snapshot_digest(snapshot),
        "inputs": inputs,
        "readiness": readiness,
        "binding": binding,
        "stages": stages,
        "gaps": gaps,
        "journey": "unverified",
        "preparation": "attention-required" if attention else "prepared",
        "tui_argv": tui_args,
        "limits": [
            "Preparation is not an accepted operational journey.",
            "Frozen replay is historical calibration, not a live session result.",
            "No runtime installation, provider launch, paid eval, "
            "or backlog task creation occurred.",
        ],
    }
    _write(root / "startup.json", report)
    _startup_findings(root, report)
    _telemetry_stage(subject, root, snapshot_id)
    _write(base / "latest.json", {"root": str(root)})
    summary = [
        f"Project: {subject}",
        f"Snapshot: {snapshot_id}",
        f"Preparation: {report['preparation']}",
        "",
        "Readiness:",
        *readiness_lines(readiness),
        "",
        f"Task authority: {binding}",
        *[f"{s['name']}: {s['status']}" for s in stages],
        *gaps,
        "",
        "Operational journey: UNVERIFIED — inspect a real session; "
        "Codex lifecycle hooks do not qualify coordinator gates.",
        f"Details: {root / 'startup.json'}",
    ]
    (root / "startup.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n" + "\n".join(summary), flush=True)
    if args.no_tui:
        return int(attention)
    # Exact snapshot and private report paths survive the handoff into the dashboard process.
    return subprocess.call([sys.executable, "-m", "bearhug.cli", *tui_args], cwd=REPO_ROOT, env=env)


def add_startup_parser(sub) -> None:
    parser = sub.add_parser(
        "startup", help="prepare current evidence and open an ordinary terminal-session cockpit"
    )
    parser.add_argument("subject", nargs="?", default=".")
    for flag in (
        "session",
        "task-id",
        "active-plan",
        "board-row",
        "snapshot",
        "provider-work-observation",
        "work-binding",
    ):
        parser.add_argument("--" + flag)
    parser.add_argument(
        "--no-tui", action="store_true", help="prepare and report without opening the dashboard"
    )
    parser.add_argument("--web", action="store_true", help="open the Go cockpit in a local browser")
    parser.add_argument(
        "--refresh", action="store_true", help="remeasure even when cached inputs match"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show the workflow without writing or running it"
    )
    parser.add_argument(
        "--step-timeout", type=float, default=120, help="maximum seconds per measurement (1–600)"
    )
    parser.set_defaults(func=_command)


def _command(args: argparse.Namespace) -> int:
    try:
        return run_startup(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 2
