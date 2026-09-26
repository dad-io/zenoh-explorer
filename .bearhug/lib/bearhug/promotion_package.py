"""P05 — assemble and validate the promotion package.

Separately reviewable parts, one manifest, and a report of what was actually verified. Bear Hug
emits it; a Barracuda-owned session applies it. The package records both facts so a reader cannot
mistake an emitted package for an installed one.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from bearhug.paths import REPO_ROOT
from bearhug.promotion import build_manifest, materialise, write_manifest
from bearhug.promotion_adapters import (
    ADAPTER_FILENAME,
    MIGRATION_LIMITS,
    build_candidate_settings,
    build_rollback_settings,
    render_adapter,
    render_settings,
)

_SNAPSHOT = REPO_ROOT / "snapshots" / "2026-08-29"


def build_package(root: Any) -> dict[str, Any]:
    """Write the package under ``root`` and return its description."""
    from bearhug.hooks.audit import load_settings

    root = Path(root)
    hooks = root / "scripts" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)

    materialise(hooks)
    (hooks / ADAPTER_FILENAME).write_text(render_adapter(), encoding="utf-8")

    legacy = load_settings(_SNAPSHOT)
    (root / "patches").mkdir(parents=True, exist_ok=True)
    (root / "patches" / "settings-candidate.json").write_text(
        render_settings(build_candidate_settings(legacy)), encoding="utf-8"
    )
    (root / "patches" / "settings-rollback.json").write_text(
        render_settings(build_rollback_settings(legacy)), encoding="utf-8"
    )
    # D08 (2026-09-01): telemetry lands at <repo>/.bearhug/telemetry/v1/, so the host's .gitignore
    # must list `.bearhug/`. One line, shipped as text rather than a hunk: Barracuda's .gitignore is
    # outside the capture spec, so no line number can be diffed against honestly.
    (root / "patches" / "gitignore-candidate.txt").write_text(
        "# bear-hug D08 (2026-09-01): the coordinator's telemetry lives under .bearhug/ and must\n"
        "# never be tracked. Append this line to .gitignore; the verifier checks it is present.\n"
        ".bearhug/\n",
        encoding="utf-8",
    )

    (root / "docs").mkdir(parents=True, exist_ok=True)
    for source, destination in (
        (REPO_ROOT / "docs" / "proposals" / "P04-ADR-stop-coordinator.md",
         root / "docs" / "P04-ADR-stop-coordinator.md"),
        (REPO_ROOT / "patches" / "ROLLBACK.md", root / "patches" / "ROLLBACK.md"),
        (REPO_ROOT / "src" / "bearhug" / "promotion_verify.py", root / "verify.py"),
    ):
        if source.is_file():
            shutil.copyfile(source, destination)

    manifest = build_manifest()
    manifest["migration_limits"] = list(MIGRATION_LIMITS)
    write_manifest(manifest, root / "manifest.json")

    return {
        "root": str(root),
        "manifest": manifest,
        "artifacts": {
            "runtime": "scripts/hooks/_bearhug",
            "adapter": f"scripts/hooks/{ADAPTER_FILENAME}",
            "settings": "patches/settings-candidate.json",
            "rollback_settings": "patches/settings-rollback.json",
            "gitignore": "patches/gitignore-candidate.txt",
            "adr": "docs/P04-ADR-stop-coordinator.md",
            "rollback": "patches/ROLLBACK.md",
            "verify": "verify.py",
            "manifest": "manifest.json",
        },
        "applied": False,
        "applied_by": "a Barracuda-owned session, never bear-hug",
        "verification": {
            "full_suite": "uv run pytest -q in bear-hug at the manifest's source_commit",
            "snapshot_id": "2026-08-29",
            # The single most important line in the package: everything above was verified against
            # constructed fixtures and a snapshot. No coordinator has run in Barracuda.
            "installed_anywhere": False,
        },
    }


def validate_package(package: dict[str, Any]) -> list[str]:
    """Every way the package can be wrong that bear-hug can actually check."""
    import hashlib

    problems: list[str] = []
    root = Path(package["root"])

    for name, relative in package["artifacts"].items():
        if not (root / relative).exists():
            problems.append(f"missing artifact: {name} ({relative})")

    runtime_dir = root / package["artifacts"]["runtime"]
    if runtime_dir.is_dir():
        digest = hashlib.sha256()
        files = sorted(
            path for path in runtime_dir.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        )
        for path in files:
            relative = path.relative_to(runtime_dir).as_posix()
            blob = path.read_bytes()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(str(len(blob)).encode("ascii"))
            digest.update(b"\x00")
            digest.update(blob)
        if digest.hexdigest() != package["manifest"]["runtime_sha256"]:
            problems.append(
                "the packaged runtime's hash does not match the manifest; the package may have "
                "been assembled from two different commits"
            )

    # A package built from a dirty tree cannot be reproduced from the commit it names, so the
    # manifest's `source_commit` would be a citation to something that never produced it. The
    # Barracuda session that rejected P06 caught exactly that: the handoff header cited a commit
    # which was not an ancestor of the package's own.
    if not package["manifest"].get("source_tree_clean", False):
        problems.append(
            "the source tree was DIRTY when this package was built, so it cannot be reproduced "
            f"from the commit it names ({package['manifest'].get('source_commit', '?')[:12]}). "
            "Commit first, then rebuild."
        )

    settings_path = root / package["artifacts"]["settings"]
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        if len(commands) != package["manifest"]["stop_registrations_after_promotion"]:
            problems.append(
                f"the candidate registers {len(commands)} Stop commands; the manifest declares "
                f"{package['manifest']['stop_registrations_after_promotion']}"
            )
    return problems
