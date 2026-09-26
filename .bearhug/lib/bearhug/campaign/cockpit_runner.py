"""Single-command live bridge from the capsule campaign projector to the read-only Go TUI."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from bearhug.paths import REPO_ROOT


class CampaignCockpitRunnerError(RuntimeError):
    """The explicit campaign projection could not be refreshed or shown."""


class CampaignCockpitError(RuntimeError):
    """A capsule cockpit projection could not be built for the TUI."""


def write_capsule_cockpit_artifact(
    *, cockpit: Mapping[str, object], artifact_path: Path | str
) -> Path:
    """Atomically write a caller-built capsule cockpit projection to scratch storage.

    ``build_capsule_cockpit`` owns projection semantics; this helper only publishes its already
    validated, read-only result for the TUI.  It deliberately accepts the projection itself so it
    cannot discover a different campaign or turn an artifact refresh into a second authority.
    """

    if not isinstance(cockpit, Mapping):
        raise CampaignCockpitRunnerError("capsule cockpit projection must be an object")
    target = Path(artifact_path)
    if not target.is_absolute():
        raise CampaignCockpitRunnerError("campaign cockpit scratch artifact must be absolute")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        raw = (
            json.dumps(
                cockpit,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CampaignCockpitRunnerError(f"capsule cockpit is not canonical JSON: {exc}") from exc
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return target


def run_capsule_tui(run_locator: Path | str, *, interval: float = 2.0, web: bool = False) -> int:
    """Run the Go TUI against one prepared native campaign locator.

    The snapshot callback reconstructs the exact prepared run on every refresh and returns the
    Python-built v2 projection.  It performs no campaign command and therefore does not acquire an
    operation lock or append operator actions.  A failed refresh publishes an invalid marker so
    the Go reader visibly fails closed instead of retaining a stale valid projection.
    """

    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        raise CampaignCockpitRunnerError("campaign cockpit refresh interval must be positive")
    tui_dir = REPO_ROOT / "tui"
    if not (tui_dir / "go.mod").is_file():
        raise CampaignCockpitRunnerError(f"no vendored campaign dashboard at {tui_dir}")
    selected_locator = Path(run_locator).expanduser()
    if not selected_locator.is_absolute():
        raise CampaignCockpitRunnerError("prepared run locator must be absolute")

    # Import lazily to avoid the campaign -> runner import cycle during ordinary command startup.
    from bearhug.campaign.capsule_campaign import snapshot_capsule_campaign
    from bearhug.campaign.prepared import load_prepared

    selected_subject = load_prepared(selected_locator).record["subject"]["path"]

    def refresh(artifact: Path) -> None:
        try:
            projection = snapshot_capsule_campaign(str(selected_locator))
            write_capsule_cockpit_artifact(cockpit=projection, artifact_path=artifact)
        except (CampaignCockpitError, OSError, RuntimeError, TypeError, ValueError) as exc:
            artifact.write_text(
                json.dumps(
                    {"projection_status": "unavailable", "reason": str(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            artifact.chmod(0o600)

    try:
        with tempfile.TemporaryDirectory(prefix="bearhug-capsule-cockpit-") as scratch:
            artifact = Path(scratch) / "campaign-cockpit.v3.json"
            refresh(artifact)
            environment = os.environ.copy()
            environment["BEARHUG_CAMPAIGN_COCKPIT"] = str(artifact)
            process = subprocess.Popen(
                ["go", "run", ".", *(["--web"] if web else []),
                 "--campaign-cockpit", str(artifact), str(selected_subject)],
                cwd=tui_dir,
                env=environment,
                shell=False,
            )
            try:
                while True:
                    try:
                        return process.wait(timeout=float(interval))
                    except subprocess.TimeoutExpired:
                        refresh(artifact)
            except KeyboardInterrupt:
                process.terminate()
                return process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
    except (CampaignCockpitError, OSError, RuntimeError, ValueError) as exc:
        raise CampaignCockpitRunnerError(
            f"cannot initialize explicit prepared campaign cockpit: {exc}"
        ) from exc


__all__ = [
    "CampaignCockpitError",
    "CampaignCockpitRunnerError",
    "write_capsule_cockpit_artifact",
    "run_capsule_tui",
]
