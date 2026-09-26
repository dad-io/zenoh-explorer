"""G01, second half — carry the toolchain identity INSIDE the evidence, and check it is one.

`bearhug.toolchain` resolves the versions (G01, 2026-08-31). This module is the snapshot-side
half: it folds that identity into `manifest.json` for future snapshots, validates that what a
manifest records is an identity rather than a presence flag, and writes the dated report.

**Why a separate module.** `bearhug.toolchain` runs version queries; `snapshot/` decides what a
snapshot records. Keeping the resolver out of `capture.py` keeps the capture spec readable, and
keeping the validation here means the rule "presence is not identity" is stated once.

**Determinism.** `manifest.json` is byte-identical across re-runs with no upstream change — that
is what lets a finding cite a snapshot and a reader confirm the snapshot never moved. The
identity's ``captured_at`` timestamp is therefore stripped from the manifest block and left to
``capture.json``, which is where `capture.py` already puts wall-clock provenance. Folding a clock
into the manifest would break the guarantee the manifest exists to make.

**The identity hash is untouched.** `capture._identity()` is the spec fingerprint plus the subject
state plus the Claude version, and the toolchain joins `external_tools` as recorded provenance
*outside* that hash. Two reasons, both load-bearing: an existing snapshot's identity must not
change retroactively, and a local `go` upgrade must not make `_refuse_relabel` reject a same-day
re-capture of an unmoved subject.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bearhug.paths import REPORTS_DIR, assert_writable
from bearhug.toolchain import TOOLCHAIN_TOOLS, capture_toolchain_identity

#: Shape version of the block itself, independent of the manifest's own schema.
TOOLCHAIN_BLOCK_SCHEMA = 1

#: The snapshot manifest schema at which the block became REQUIRED.
#:
#: Every manifest committed before this carries no toolchain key, and none of them may be
#: invalidated by a check that landed afterwards — a detector that fails its own corpus is
#: measuring its own arrival, not the subject. Below this schema the block is optional; at or
#: above it, absent is a defect.
TOOLCHAIN_SINCE_SCHEMA = 3

#: The keys every tool row carries. `present` alone is what this module exists to reject.
_REQUIRED_ROW_KEYS = ("present", "version", "path")

#: Strings that answer "is it installed" rather than "which one ran". `toolchain_snapshot()` in
#: `hooks/runner.py` returns booleans and `toolchain_note()` renders `go=present`; both are right
#: for what they do and neither is an identity.
_PRESENCE_WORDS = frozenset(
    {"present", "absent", "missing", "installed", "true", "false", "yes", "no", "unknown", "ok"}
)

#: A usage block in a version field looks like an answer, which is worse than no answer.
_NOT_A_VERSION_PREFIXES = ("usage:", "help", "options:")


def manifest_toolchain_block() -> dict[str, Any]:
    """The toolchain block a future snapshot's manifest carries.

    Delegates to `bearhug.toolchain.capture_toolchain_identity` and never re-resolves a version:
    a second resolver would be the duplicate-authority failure `docs/METHOD.md` names, and the two
    could disagree about which `dlv` produced a finding.
    """
    identity = dict(capture_toolchain_identity())
    # Wall-clock provenance belongs in capture.json. See the module docstring.
    identity.pop("captured_at", None)
    identity["block_schema"] = TOOLCHAIN_BLOCK_SCHEMA
    identity["note"] = (
        "Recorded provenance, deliberately OUTSIDE manifest['identity']: a snapshot's identity is "
        "the capture spec plus the subject state, and a local toolchain upgrade must not "
        "retroactively rename an unmoved subject. Capture time lives in capture.json so this "
        "block stays deterministic."
    )
    return identity


def _row_problems(tool: str, row: object) -> list[str]:
    if isinstance(row, bool):
        return [
            f"{tool}: recorded as a bare boolean ({row!r}). That is presence, not identity — it "
            f"cannot explain a compiler or debugger result by version."
        ]
    if not isinstance(row, Mapping):
        rendered = repr(row)
        if isinstance(row, str) and row.strip().lower() in _PRESENCE_WORDS:
            return [
                f"{tool}: recorded as {rendered}, which answers whether the binary was found "
                f"rather than which binary ran."
            ]
        return [f"{tool}: recorded as {rendered}, not a version record."]

    problems: list[str] = []
    missing = [key for key in _REQUIRED_ROW_KEYS if key not in row]
    if missing:
        problems.append(f"{tool}: row is missing {', '.join(missing)}.")

    version = row.get("version")
    present = row.get("present")
    if version is not None:
        if not isinstance(version, str):
            problems.append(f"{tool}: version is {type(version).__name__}, not a string.")
        else:
            lowered = version.strip().lower()
            if lowered in _PRESENCE_WORDS:
                problems.append(
                    f"{tool}: version is {version!r} — presence in a version field."
                )
            elif lowered.startswith(_NOT_A_VERSION_PREFIXES):
                problems.append(
                    f"{tool}: version is a usage/help string, which looks like an answer."
                )
            elif not any(character.isdigit() for character in version):
                problems.append(
                    f"{tool}: version {version!r} carries no digit, so it is a product name "
                    f"rather than a version."
                )
    elif present and not row.get("reason"):
        # A tool that EXISTS but failed to report is a different state from one that is not
        # installed. Collapsing them files a broken install as an uninstalled one.
        problems.append(
            f"{tool}: present with no version and no stated reason — silence is not absence."
        )
    elif present is False and not row.get("reason"):
        problems.append(f"{tool}: absent with no stated reason.")
    return problems


def validate_manifest_toolchain(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Every problem with a manifest's toolchain block. Empty means valid.

    Guarded on the manifest schema so an older snapshot, taken before the block existed, still
    validates — see :data:`TOOLCHAIN_SINCE_SCHEMA`.
    """
    block = manifest.get("toolchain")
    if block is None:
        try:
            schema = int(manifest.get("schema", 0))
        except (TypeError, ValueError):
            schema = 0
        if schema >= TOOLCHAIN_SINCE_SCHEMA:
            return (
                f"manifest schema {schema} requires a toolchain block and carries none; a "
                f"finding citing it cannot name the toolchain that produced it.",
            )
        return ()

    if not isinstance(block, Mapping):
        return (f"toolchain block is {type(block).__name__}, not a mapping.",)

    tools = block.get("tools")
    if not isinstance(tools, Mapping):
        return ("toolchain block carries no `tools` mapping.",)

    problems: list[str] = []
    for tool, row in tools.items():
        problems.extend(_row_problems(str(tool), row))
    return tuple(problems)


def missing_tools(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Tools this lab depends on that a manifest's block does not mention at all.

    Separate from :func:`validate_manifest_toolchain` because an omission is a different defect
    from a malformed row, and a manifest written by an older block schema may legitimately know
    fewer tools than the current one.
    """
    block = manifest.get("toolchain")
    if not isinstance(block, Mapping):
        return ()
    tools = block.get("tools")
    if not isinstance(tools, Mapping):
        return ()
    return tuple(tool for tool in TOOLCHAIN_TOOLS if tool not in tools)


def write_toolchain_report(
    label: str,
    *,
    block: dict[str, Any] | None = None,
    reports_dir: Path | str | None = None,
) -> Path:
    """Write ``reports/toolchain-<label>.json`` and return its path.

    ``label`` is the snapshot label (a date), so the artifact sits beside the evidence it
    describes rather than being overwritten by the next machine that runs it.
    """
    root = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    payload = dict(block if block is not None else manifest_toolchain_block())
    payload["label"] = label
    payload["limits"] = [
        *payload.get("limits", []),
        "These are the versions on THIS machine when the report was written, not the versions "
        "that produced any historical finding. Attributing an older result to them would be the "
        "stale-traffic defect in another form.",
        "A version string pins the binary's own claim about itself, not its behaviour. Treat a "
        "match as necessary, not sufficient.",
    ]
    path = assert_writable(root / f"toolchain-{label}.json")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = [
    "TOOLCHAIN_BLOCK_SCHEMA",
    "TOOLCHAIN_SINCE_SCHEMA",
    "manifest_toolchain_block",
    "missing_tools",
    "validate_manifest_toolchain",
    "write_toolchain_report",
]
