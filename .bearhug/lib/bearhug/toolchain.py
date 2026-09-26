"""G01 — identify the toolchain, so a compiler or debugger result can be explained by it.

`hooks/runner.toolchain_snapshot()` records presence: `go=present`. That was built to tell a real
`go vet` failure from a missing binary, and it does. What it cannot do is explain a result by
VERSION — two `go vet` outcomes that differ because the toolchain moved look identical in the
record, and a DLV result is uninterpretable without knowing which DLV produced it. G05's proof
levels depend on that identification.

**Versions only.** `go version` reports the toolchain; `go list`, `go env` and `go build` would
read the module and are never invoked here. The boundary is the same one the charter draws: bear-
hug measures the harness, not the product.

**Absent is `unknown`, never omitted.** A missing key reads as agreement with whatever the reader
already believes, and a tool that exists but fails to report is a different state from one that is
not installed — collapsing them would file a broken install as an uninstalled one.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.paths import REPO_ROOT

#: The external binaries a Go result depends on. `node` is included because graft's Stop wrapper
#: runs under it, and a graft observation is uninterpretable without knowing which node ran it.
TOOLCHAIN_TOOLS: tuple[str, ...] = ("go", "gofmt", "goimports", "dlv", "node", "jq", "git")

#: How each tool reports itself. `dlv version` and `go version` are subcommands, not flags.
_VERSION_ARGS: dict[str, tuple[str, ...]] = {
    "go": ("version",),
    "dlv": ("version",),
    "node": ("--version",),
    "jq": ("--version",),
    "git": ("--version",),
}

#: Tools with NO independent version, whose identity is another tool's.
#:
#: `gofmt` ships with the Go distribution — `gofmt -h` prints a usage block, and recording that as
#: a "version" is worse than recording nothing, because it looks like an answer. Its identity is
#: the Go toolchain's, so it is derived rather than invented.
_DERIVED_FROM: dict[str, str] = {"gofmt": "go"}

#: Where a tool prints its version on a line OTHER than the first.
#:
#: `dlv version` prints "Delve Debugger" first and "Version: 1.27.0" second. Taking the first line
#: recorded the product NAME as the version — useless precisely where it matters most, since
#: G05's proof levels are uninterpretable without knowing which DLV produced the evidence.
_VERSION_LINE_PREFIX: dict[str, str] = {"dlv": "Version:"}

#: A version query that blocked would wedge a snapshot.
CAPTURE_TIMEOUT_SECONDS = 10

#: Pinned, not `sorted(...)[-1]`: a fixture that re-targets itself whenever a new snapshot is
#: taken changes its own subject with no signal that it did (the convention `test_hooks_audit`
#: states). Advanced from `2026-08-29` to `2026-09-01` so this counts the configurations the rest
#: of the G-track cites as evidence — G02 lints the same eight files under the same snapshot id.
_RUN_CONFIGURATIONS = "snapshots/2026-09-01/project/.idea/runConfigurations"


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _identify(tool: str, resolved: dict[str, Any] | None = None) -> dict[str, Any]:
    source = _DERIVED_FROM.get(tool)
    if source and resolved is not None:
        # No independent version. Derived rather than invented, and said so.
        parent = resolved.get(source) or {}
        present = shutil.which(tool) is not None
        if not present:
            return {"present": False, "version": None, "path": None,
                    "reason": f"{tool} is not on PATH in this environment"}
        return {
            "present": True,
            "version": parent.get("version"),
            "path": shutil.which(tool),
            "derived_from": source,
            "reason": None if parent.get("version") else
                      f"{tool} has no independent version and {source} did not report one",
        }

    path = shutil.which(tool)
    if path is None:
        return {
            "present": False,
            "version": None,
            "path": None,
            "reason": f"{tool} is not on PATH in this environment",
        }

    args = _VERSION_ARGS.get(tool, ("--version",))
    try:
        result = subprocess.run(
            [tool, *args], capture_output=True, text=True,
            timeout=CAPTURE_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "present": True, "version": None, "path": path,
            "reason": f"{tool} timed out reporting its version after "
                      f"{CAPTURE_TIMEOUT_SECONDS}s",
        }
    except OSError as exc:
        return {
            "present": True, "version": None, "path": path,
            "reason": f"{tool} did not report a version: {type(exc).__name__}",
        }

    combined = f"{result.stdout}\n{result.stderr}"
    prefix = _VERSION_LINE_PREFIX.get(tool)
    if prefix:
        reported = ""
        for line in combined.splitlines():
            if line.strip().startswith(prefix):
                reported = line.strip()
                break
    else:
        reported = _first_line(result.stdout) or _first_line(result.stderr)
    if not reported:
        return {
            "present": True, "version": None, "path": path,
            "reason": f"{tool} did not report a version (exit {result.returncode})",
        }
    return {"present": True, "version": reported, "path": path, "reason": None}


def _identify_goland() -> dict[str, Any]:
    """GoLand's identity, where it is readable without executing anything.

    Roadmap 1.9 calls the eight run configurations the human half of observe-before-claiming, and
    nothing has ever identified the IDE that runs them. Read from the app bundle's Info.plist on
    macOS; unknown elsewhere rather than guessed.
    """
    candidates = [
        Path("/Applications/GoLand.app/Contents/Info.plist"),
        Path.home() / "Applications" / "GoLand.app" / "Contents" / "Info.plist",
    ]
    for plist in candidates:
        if not plist.is_file():
            continue
        try:
            import plistlib

            data = plistlib.loads(plist.read_bytes())
        except Exception:  # noqa: BLE001 - an unreadable plist is unknown, not absent
            return {
                "present": True, "version": None, "path": str(plist.parent.parent),
                "reason": "GoLand is installed but its Info.plist could not be read",
            }
        version = data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
        return {
            "present": True,
            "version": str(version) if version else None,
            "path": str(plist.parent.parent),
            "reason": None if version else "Info.plist carries no version string",
        }
    return {
        "present": False, "version": None, "path": None,
        "reason": "GoLand.app was not found in /Applications or ~/Applications; on another "
                  "platform or a non-default install this is unknown rather than absent",
    }


def _identify_all() -> dict[str, dict[str, Any]]:
    """Independent tools first, then the ones whose identity derives from another."""
    resolved: dict[str, Any] = {}
    for tool in TOOLCHAIN_TOOLS:
        if tool not in _DERIVED_FROM:
            resolved[tool] = _identify(tool)
    for tool in TOOLCHAIN_TOOLS:
        if tool in _DERIVED_FROM:
            resolved[tool] = _identify(tool, resolved)
    return {tool: resolved[tool] for tool in TOOLCHAIN_TOOLS}


def capture_toolchain_identity() -> dict[str, Any]:
    """The toolchain identity block. Version strings only; no module is read."""
    configurations = REPO_ROOT / _RUN_CONFIGURATIONS
    return {
        "schema_version": 1,
        "task": "G01",
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "tools": _identify_all(),
        "ide": {"goland": _identify_goland()},
        "run_configurations": {
            "source": _RUN_CONFIGURATIONS,
            "count": len(list(configurations.glob("*.xml"))) if configurations.is_dir() else 0,
            "note": "Counted from the snapshot. G02 validates their structure; this only "
                    "records how many exist.",
        },
        "limits": [
            "Version strings only. `go list`, `go env` and `go build` would read the module and "
            "are never invoked.",
            "These are the versions on THIS machine at capture time, not the versions that "
            "produced any historical finding. Attributing an old result to them would be the "
            "stale-traffic defect in another form.",
        ],
    }


def toolchain_provenance(identity: dict[str, Any] | None = None) -> str:
    """A one-line provenance string naming VERSIONS, for folding into a finding's limit."""
    identity = identity or capture_toolchain_identity()
    parts = []
    for tool, row in identity["tools"].items():
        parts.append(f"{tool}={row['version']}" if row["version"] else f"{tool}=unknown")
    ide = identity["ide"]["goland"]
    parts.append(f"goland={ide['version']}" if ide["version"] else "goland=unknown")
    return "Toolchain at capture time: " + ", ".join(parts) + "."
