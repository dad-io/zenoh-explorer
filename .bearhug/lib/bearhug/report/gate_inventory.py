"""H02 — a NON-AUTHORITATIVE gate inventory, generated from `.claude/settings.json`.

GATE-COVERAGE (2.6) found eight gates that act on the turn and are named nowhere in CLAUDE.md,
and every hand-written inventory before this one drifted (the 19-vs-24 class: a count written
during one task, stale the moment a hook was added). So this inventory is derived from the one
authority, says so on its face, carries the digest of the settings it was generated from, and is
stale — by its own test — the moment that file changes. The patch adds a new file; it never edits
CLAUDE.md, so the golden master keeps one authority and one anchor set.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bearhug.lint.gates import classify_hook, hook_arguments, parse_hooks, resolve_script
from bearhug.paths import PATCHES_DIR, assert_writable

GENERATED_PATH = "docs/GATE-INVENTORY.generated.md"
_MARKER = re.compile(r"generated-from: \.claude/settings\.json sha256=([0-9a-f]{64})")


@dataclass(frozen=True, slots=True)
class GateRow:
    event: str
    order: int  # execution order within the event, across matcher groups
    group_index: int
    position_in_group: int
    matcher: str
    command: str
    script: str | None
    arguments: tuple[str, ...]
    timeout: int | None
    static_class: str

    def __lt__(self, other: GateRow) -> bool:
        return (self.event, self.order) < (other.event, other.order)


@dataclass(slots=True)
class GateInventory:
    snapshot_id: str
    settings_sha256: str
    rows: list[GateRow] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "settings_sha256": self.settings_sha256,
            "rows": [asdict(row) for row in self.rows],
        }


def _static_class(project: Path, script: str | None) -> str:
    if script is None:
        return "unavailable-external"
    verdict = classify_hook(project / script)
    if not verdict["readable"]:
        return "unavailable-external"
    if verdict["blocks"]:
        return "blocking"
    if verdict["injects"]:
        return "advisory-injection"
    return "registration-only"


def build_gate_inventory(snapshot_dir: Path | str) -> GateInventory:
    snapshot = Path(snapshot_dir)
    project = snapshot / "project"
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    settings_bytes = (project / ".claude" / "settings.json").read_bytes()
    inventory = GateInventory(
        snapshot_id=manifest.get("snapshot_id", snapshot.name),
        settings_sha256=hashlib.sha256(settings_bytes).hexdigest(),
    )
    order: dict[str, int] = {}
    for spec in parse_hooks(json.loads(settings_bytes)):
        script = resolve_script(spec.command)
        position = order.get(spec.event, 0)
        order[spec.event] = position + 1
        inventory.rows.append(
            GateRow(
                event=spec.event,
                order=position,
                group_index=spec.group_index or 0,
                position_in_group=spec.position_in_group or 0,
                matcher=spec.matcher or "*",
                command=spec.command,
                script=script,
                arguments=tuple(hook_arguments(spec.command)),
                timeout=spec.timeout,
                static_class=_static_class(project, script),
            )
        )
    inventory.rows.sort()
    return inventory


def render_gate_inventory(inventory: GateInventory) -> str:
    lines = [
        "# Gate inventory — GENERATED, not authoritative",
        "",
        f"<!-- generated-from: .claude/settings.json sha256={inventory.settings_sha256} -->",
        f"<!-- generated-by: bear-hug H02 against snapshot {inventory.snapshot_id} -->",
        "",
        "**`.claude/settings.json` is the authority.** This file is a rendering of it, produced by",
        "bear-hug from the snapshot named above, and it is STALE the moment settings.json changes:",
        "the digest in the marker above must equal `sha256sum .claude/settings.json`. Do not edit",
        "this file by hand; regenerate it. Static class is read from each script's source",
        "(block / inject markers) and does not establish reachability or effect.",
        "",
        "| event | order | matcher | script or command | args | timeout | static class |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in inventory.rows:
        target = row.script or row.command
        lines.append(
            f"| {row.event} | {row.order} | `{row.matcher.replace('|', '\\|')}` | "
            f"`{target.replace('|', '\\|')}` | {' '.join(row.arguments) or '—'} | "
            f"{row.timeout if row.timeout is not None else 'none'} | {row.static_class} |"
        )
    lines += [
        "",
        f"{len(inventory.rows)} registrations. A registration absent here that is present in",
        "settings.json means this file is stale, not that the hook does not exist.",
        "",
    ]
    return "\n".join(lines)


def is_stale(appendix_text: str, settings_bytes: bytes) -> bool:
    """A generated copy is fresh only when its marker names the current settings digest."""
    match = _MARKER.search(appendix_text)
    if not match:
        return True
    return match.group(1) != hashlib.sha256(settings_bytes).hexdigest()


def emit_gate_inventory_patch(
    snapshot_dir: Path | str, *, out_dir: Path | str | None = None
) -> Path:
    """A new-file unified diff adding the generated inventory. CLAUDE.md is not edited."""
    directory = Path(out_dir) if out_dir is not None else PATCHES_DIR
    inventory = build_gate_inventory(snapshot_dir)
    safe = inventory.snapshot_id.replace("@", "-at-").replace("/", "-")
    path = assert_writable(directory / f"gate-inventory-{safe}.diff")
    directory.mkdir(parents=True, exist_ok=True)
    body = render_gate_inventory(inventory)
    body_lines = body.splitlines()
    header = [
        f"# bear-hug H02 — generated gate inventory for snapshot {inventory.snapshot_id}",
        "# Proposed, never applied by bear-hug. Adds one generated file; CLAUDE.md is not edited.",
        "# ANCHOR-SAFETY: safe — no CLAUDE.md heading moves, so no memex anchor can break.",
        f"# generated-from: .claude/settings.json sha256={inventory.settings_sha256}",
        "",
    ]
    diff = [
        "--- /dev/null",
        f"+++ b/{GENERATED_PATH}",
        f"@@ -0,0 +1,{len(body_lines)} @@",
    ] + [f"+{line}" for line in body_lines]
    path.write_text("\n".join(header + diff) + "\n", encoding="utf-8")
    return path


__all__ = [
    "GENERATED_PATH",
    "GateInventory",
    "GateRow",
    "build_gate_inventory",
    "emit_gate_inventory_patch",
    "is_stale",
    "render_gate_inventory",
]
