"""Single-command live bridge from Bear Hug's cockpit projector to its Go TUI.

The ordinary TUI used to require an operator-managed ``cockpit --watch`` process.  This runner
keeps the projection in a private temporary directory for exactly the lifetime of the dashboard.
It does not turn the TUI into a second authority and it never writes into the observed project.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from bearhug.paths import CLAUDE_HOME, PROVIDER_OBSERVATIONS_DIR, REPO_ROOT, transcripts_dir
from bearhug.replay.cockpit import build_cockpit
from bearhug.snapshot import compute_drift
from bearhug.watcher import new_watcher_id, watcher_record


def refresh_project_campaign_artifact(subject: Path, artifact: Path) -> Path:
    from bearhug.campaign.cockpit_runner import write_capsule_cockpit_artifact
    from bearhug.project_campaign import status

    observed = status(subject, include_cockpit=True)
    if observed.get("cockpit"):
        write_capsule_cockpit_artifact(cockpit=observed["cockpit"], artifact_path=artifact)
    else:
        _atomic_private_json(
            artifact,
            {
                "projection_status": "unavailable",
                "reason": observed.get("reason") or observed["status"],
            },
        )
    return artifact


MINIMUM_REFRESH_SECONDS = 5.0


class LiveTUIError(RuntimeError):
    """The live cockpit could not be projected or displayed safely."""


def _atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def refresh_live_cockpit_artifact(
    *,
    subject: Path | str,
    snapshot: Path | str | None,
    artifact_path: Path | str,
    session_id: str | None = None,
    provider_work_observation: Path | str | None = None,
    work_binding: Path | str | None = None,
    watcher_id: str | None = None,
    heartbeat_sequence: int | None = None,
    watcher_scope: str = "private",
    watcher_stale_after_seconds: int | None = None,
    now: datetime | None = None,
) -> Path:
    """Atomically replace one runner-owned projection for one explicit subject and snapshot."""

    target = Path(artifact_path)
    if not target.is_absolute():
        raise LiveTUIError("live cockpit scratch artifact must be absolute")
    selected_subject = Path(subject).expanduser().resolve(strict=True)
    selected_snapshot: Path | None = None
    snapshot_id = "unknown"
    if snapshot is not None:
        selected_snapshot = Path(snapshot).expanduser().resolve(strict=True)
        manifest_path = selected_snapshot / "manifest.json"
        if not manifest_path.is_file():
            raise LiveTUIError(f"snapshot has no manifest.json: {selected_snapshot}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LiveTUIError(f"cannot read snapshot manifest: {exc}") from exc
        snapshot_id = manifest.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise LiveTUIError("snapshot manifest has no snapshot_id")

    try:
        drift = (
            compute_drift(
                selected_snapshot,
                barracuda_root=selected_subject,
                claude_home=CLAUDE_HOME,
            )
            if selected_snapshot is not None
            else None
        )
        projection = build_cockpit(
            snapshot_id=snapshot_id,
            live_dir=transcripts_dir(selected_subject),
            drift=drift,
            subject_root=selected_subject,
            home=Path.home(),
            session_id=session_id,
            provider_observations_dir=PROVIDER_OBSERVATIONS_DIR,
            provider_work_observation=provider_work_observation,
            work_binding=work_binding,
            now=now,
        )
        if watcher_id is not None or heartbeat_sequence is not None:
            if watcher_id is None or heartbeat_sequence is None:
                raise LiveTUIError(
                    "watcher identity and heartbeat sequence must be supplied together"
                )
            projection["watcher"] = watcher_record(
                watcher_id=watcher_id,
                scope=watcher_scope,
                subject=selected_subject,
                heartbeat_sequence=heartbeat_sequence,
                heartbeat_at=now,
                stale_after_seconds=max(10, watcher_stale_after_seconds or 10),
            )
        _atomic_private_json(target, projection)
    except (OSError, UnicodeError, ValueError) as exc:
        raise LiveTUIError(f"cannot refresh live cockpit projection: {exc}") from exc
    return target


def run_live_tui(
    *,
    subject: Path | str,
    snapshot: Path | str | None,
    refresh_seconds: float = 30.0,
    session_id: str | None = None,
    provider_work_observation: Path | str | None = None,
    work_binding: Path | str | None = None,
    authority_scope: str = "full",
    active_plan: str | None = None,
    board_row: str | None = None,
    web: bool = False,
) -> int:
    """Run the Go TUI and continuously refresh its ordinary cockpit in private scratch."""

    if refresh_seconds < MINIMUM_REFRESH_SECONDS:
        raise LiveTUIError(
            f"live cockpit refresh interval must be at least {MINIMUM_REFRESH_SECONDS:g} seconds"
        )
    if authority_scope not in {"full", "bound"}:
        raise LiveTUIError("PLAN/BOARD authority scope must be full or bound")
    if authority_scope == "bound" and (not active_plan or not board_row):
        raise LiveTUIError(
            "bound PLAN/BOARD authority requires both an active plan and a BOARD row"
        )
    if authority_scope == "full" and (active_plan or board_row):
        raise LiveTUIError("full PLAN/BOARD authority cannot carry a bound selection")
    tui_dir = REPO_ROOT / "tui"
    if not (tui_dir / "go.mod").is_file():
        raise LiveTUIError(f"no vendored dashboard at {tui_dir}")
    if shutil.which("go") is None:
        raise LiveTUIError("the dashboard needs a Go toolchain on PATH (`brew install go`)")
    try:
        selected_subject = Path(subject).expanduser().resolve(strict=True)
        selected_snapshot = (
            Path(snapshot).expanduser().resolve(strict=True) if snapshot is not None else None
        )
        with tempfile.TemporaryDirectory(prefix="bearhug-live-cockpit-") as scratch:
            artifact = Path(scratch) / "cockpit.v4.json"
            campaign_artifact = Path(scratch) / "campaign.json"
            watcher_id = new_watcher_id()
            heartbeat_sequence = 0
            refresh_keywords = {
                "subject": selected_subject,
                "snapshot": selected_snapshot,
                "artifact_path": artifact,
                "session_id": session_id,
                "provider_work_observation": provider_work_observation,
                "work_binding": work_binding,
                "watcher_id": watcher_id,
                "watcher_scope": "private",
                "watcher_stale_after_seconds": max(10, int(refresh_seconds * 2)),
            }

            def refresh() -> Path:
                nonlocal heartbeat_sequence
                heartbeat_sequence += 1
                refresh_keywords["heartbeat_sequence"] = heartbeat_sequence
                refresh_project_campaign_artifact(selected_subject, campaign_artifact)
                return refresh_live_cockpit_artifact(**refresh_keywords)

            refresh()
            environment = os.environ.copy()
            environment["BEARHUG_COCKPIT"] = str(artifact)
            environment["BEARHUG_CAMPAIGN_COCKPIT"] = str(campaign_artifact)
            environment["BEARHUG_PLAN_BOARD_SCOPE"] = authority_scope
            environment["BEARHUG_ACTIVE_PLAN"] = active_plan or ""
            environment["BEARHUG_BOARD_ROW"] = board_row or ""
            process = subprocess.Popen(
                ["go", "run", ".", *(["--web"] if web else []), str(selected_subject)],
                cwd=tui_dir,
                env=environment,
                shell=False,
            )
            try:
                while True:
                    try:
                        return process.wait(timeout=refresh_seconds)
                    except subprocess.TimeoutExpired:
                        try:
                            refresh()
                        except LiveTUIError:
                            # An invalid closed object makes the Go reader visibly unavailable.
                            # Never leave the preceding valid projection looking current.
                            _atomic_private_json(artifact, {"projection_status": "unavailable"})
            except KeyboardInterrupt:
                process.terminate()
                return process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
    except (LiveTUIError, OSError) as exc:
        if isinstance(exc, LiveTUIError):
            raise
        raise LiveTUIError(f"cannot initialize live TUI: {exc}") from exc


__all__ = [
    "LiveTUIError",
    "MINIMUM_REFRESH_SECONDS",
    "refresh_live_cockpit_artifact",
    "run_live_tui",
]
