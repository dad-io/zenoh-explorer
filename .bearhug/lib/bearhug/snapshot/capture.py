"""Phase 1 — freeze the subject.

Two guarantees make everything downstream checkable:

* ``manifest.json`` is deterministic. Re-running with no upstream change produces byte-identical
  output, so a finding can cite a snapshot and a reader can confirm the snapshot never moved.
  Wall-clock capture metadata therefore lives in ``capture.json``, not in the manifest.
* The subject is untouched. barracuda's git state is read before and after the copy and compared;
  a mismatch aborts. That is the charter (docs/CHARTER.md) asserted in code, not in prose.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug import __version__, paths
from bearhug.model import dumps
from bearhug.paths import WriteBoundaryError, assert_writable
from bearhug.snapshot import spec
from bearhug.snapshot.frontmatter import parse_frontmatter
from bearhug.snapshot.toolchain import manifest_toolchain_block

#: 3 (G01, 2026-09-02) adds the `toolchain` block, so a finding citing a snapshot can name the
#: `go`/`dlv`/`gofmt` and GoLand versions that produced it. Manifests at schema 2 carry no such
#: block and stay valid — `snapshot.toolchain.TOOLCHAIN_SINCE_SCHEMA` is the guard, and the
#: Existing manifests remain valid and untouched; newly captured manifests add a content digest to
#: the identity so dirty or ambient changes cannot silently reuse a label.
MANIFEST_SCHEMA = 3


class SnapshotIdentityError(RuntimeError):
    """Raised when a label would be made to mean two different snapshots."""


#: The frontmatter keys the index keeps. Bodies are never stored — see plan task 1.4.
INDEX_KEYS = ("id", "title", "status", "supersedes", "superseded_by", "evidence", "sources")
LIST_KEYS = frozenset({"supersedes", "evidence", "sources"})

MEMEX_DECISIONS = "docs/memex/decisions"

#: The findings log is written by prose instruction (CLAUDE.md:925), read by memq, and validated
#: by no hook at all. Its null rate is therefore nobody's measurement yet.
FINDINGS_PRIMARY = "findings/findings.json"
FINDINGS_SECONDARY = "_resources/findings/findings.json"

#: memq indexes exactly these as metadata (.memq.json), so a null here is a hole in memq's recall.
FINDINGS_FIELDS = ("id", "title", "date", "branch", "severity", "status", "category")

#: A status/severity/category is a token. Anything longer is prose in an enum slot.
TOKEN_MAX = 60

#: Binaries the harness shells out to that live OUTSIDE barracuda, so a snapshot of the repo
#: alone would not pin them. memq is three of the 24 hook commands; graft is five; both are
#: unversioned working copies. The content hash is the pin — a version string is a bonus.
EXTERNAL_TOOLS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("memq", ("memq", "~/Documents/github/memq/memq"), "UserPromptSubmit + PreToolUse Bash/Edit"),
    ("graft", ("graft",), "SessionStart, Stop, PostToolUse, and an MCP server"),
    ("codebase-memory-mcp", ("codebase-memory-mcp",), "MCP server declared in .mcp.json"),
    ("claude", ("claude",), "the CLI whose hook contracts every gate is written against"),
)


def _identity(
    spec_fingerprint: str,
    subject_head: str | None,
    claude: str | None,
    *,
    content_digest: str | None = None,
) -> str:
    """What makes this snapshot *this* snapshot.

    The label is a date, and a date is not an identity: `2026-08-28` pointed at three different
    subject states in one afternoon because a same-day re-run silently overwrote it, and every
    finding citing that label inherited the ambiguity. Identity is the spec, subject state, and
    captured content — change any of them and the evidence is different evidence.
    """
    payload = "\x00".join(
        [spec_fingerprint, subject_head or "-", claude or "-", content_digest or "-"]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """Where the snapshot landed, and what it holds."""

    path: Path
    manifest: dict[str, Any]
    planned: tuple[spec.Target, ...]

    @property
    def label(self) -> str:
        return self.path.name

    @property
    def snapshot_id(self) -> str:
        """The string a finding should cite: `<label>@<short identity>`."""
        return str(self.manifest.get("snapshot_id", self.label))


# --- reading the subject -------------------------------------------------------------------


def _run(args: list[str], cwd: Path | None = None) -> str | None:
    try:
        done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def git_state(root: Path) -> dict[str, Any]:
    """barracuda's HEAD, branch and dirty list. Read-only: no git command here writes."""
    if not (root / ".git").exists():
        return {"head": None, "branch": None, "dirty": [], "note": "not a git working tree"}
    porcelain = _run(["git", "status", "--porcelain"], cwd=root) or ""
    return {
        "head": _run(["git", "rev-parse", "HEAD"], cwd=root),
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root),
        "dirty": sorted(line.strip() for line in porcelain.splitlines() if line.strip()),
    }


def _resolve_tool(candidates: tuple[str, ...]) -> Path | None:
    for candidate in candidates:
        if "/" in candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path.resolve()
        else:
            found = shutil.which(candidate)
            if found:
                return Path(found).resolve()
    return None


@functools.lru_cache(maxsize=1)
def external_tools() -> dict[str, Any]:
    """Pin the out-of-repo binaries the hooks depend on.

    A hook whose binary is missing is inert, and an inert hook that nobody noticed is the exact
    failure this repo exists to catch. Absence is therefore recorded, never omitted.
    """
    out: dict[str, Any] = {
        "_limit": (
            "A hash pins the file, not the behaviour, and these are resolved from bear-hug's "
            "PATH at capture time — a hook runs with the environment Claude Code gives it, "
            "which may resolve a different binary. Treat a match as necessary, not sufficient."
        )
    }
    for name, candidates, role in EXTERNAL_TOOLS:
        path = _resolve_tool(candidates)
        if path is None:
            out[name] = {
                "role": role,
                "resolved": None,
                "note": "not found on this machine — anything calling it is inert here",
            }
            continue
        out[name] = {
            "role": role,
            "resolved": str(path),
            "sha256": _sha256(path),
            "size": path.stat().st_size,
            "version": _run([str(path), "--version"]),
        }
    return out


@functools.lru_cache(maxsize=1)
def claude_version() -> str | None:
    """The CLI version is part of the harness: hook contracts change between releases."""
    return _run(["claude", "--version"])


def instrument() -> dict[str, Any]:
    """Pin bear-hug itself inside its own evidence.

    A finding cites a snapshot. Without this block, "snapshot 0" names whichever version of
    the capture spec happened to be checked out when someone asks — and the spec has already
    widened three times. The instrument is part of the measurement.
    """
    repo = paths.REPO_ROOT
    dirty = _run(["git", "status", "--porcelain"], cwd=repo)
    return {
        "bearhug_version": __version__,
        "spec_fingerprint": spec.fingerprint(),
        "spec_rules": len(spec.ALL_RULES),
        "git_commit": _run(["git", "rev-parse", "HEAD"], cwd=repo),
        "git_dirty": bool(dirty),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry(target: spec.Target, *, copied: Path | None = None) -> dict[str, Any]:
    """Describe the bytes that will be published, rather than re-reading the live source."""
    captured = copied or target.source
    stat = captured.stat()
    return {
        "path": str(target.relpath),
        "sha256": _sha256(captured),
        "size": stat.st_size,
        "mtime": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.UTC).isoformat(),
    }


def _content_identity(
    layers: dict[str, dict[str, Any]], index_bytes: bytes, findings_bytes: bytes
) -> str:
    """Hash captured content without folding volatile metadata into the snapshot identity."""
    payload = {
        "layers": {
            layer: {
                "files": sorted(
                    (
                        {
                            "path": entry["path"],
                            "sha256": entry["sha256"],
                            "size": entry["size"],
                        }
                        for entry in block["files"]
                    ),
                    key=lambda entry: entry["path"],
                ),
                "missing": sorted(block["missing"]),
                "census": block["census"],
            }
            for layer, block in sorted(layers.items())
        },
        "memex_sha256": hashlib.sha256(index_bytes).hexdigest(),
        "findings_sha256": hashlib.sha256(findings_bytes).hexdigest(),
    }
    return hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()


def _validate_candidate(
    candidate: Path,
    layers: dict[str, dict[str, Any]],
    index_bytes: bytes,
    findings_bytes: bytes,
) -> None:
    """Check the tree that is about to be published against its own recorded bytes."""
    for layer, block in layers.items():
        for entry in block["files"]:
            path = candidate / layer / PurePosixPath(entry["path"])
            if not path.is_file() or path.stat().st_size != entry["size"]:
                raise WriteBoundaryError(
                    "captured file is missing or changed before publication: "
                    f"{layer}/{entry['path']}"
                )
            if _sha256(path) != entry["sha256"]:
                raise WriteBoundaryError(
                    f"captured file hash changed before publication: {layer}/{entry['path']}"
                )
    for name, expected in (
        ("memex-index.json", index_bytes),
        ("findings-index.json", findings_bytes),
    ):
        path = candidate / name
        if not path.is_file() or path.read_bytes() != expected:
            raise WriteBoundaryError(f"captured index changed before publication: {name}")


def _publish_candidate(candidate: Path, destination: Path, *, force: bool) -> None:
    """Publish a complete candidate directory while retaining a recoverable old tree."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        candidate.replace(destination)
        return
    if not force:
        raise SnapshotIdentityError(
            f"{destination.name} already exists but has no reusable snapshot identity; "
            "use --force to overwrite deliberately."
        )

    backup = Path(tempfile.mkdtemp(prefix=f".{destination.name}.old-", dir=destination.parent))
    backup.rmdir()
    moved_old = False
    try:
        destination.replace(backup)
        moved_old = True
        try:
            candidate.replace(destination)
        except BaseException:
            moved_old = False
            backup.replace(destination)
            raise
    finally:
        if moved_old:
            shutil.rmtree(backup, ignore_errors=True)


# --- 1.4 the memex frontmatter index -------------------------------------------------------


def build_memex_index(barracuda_root: Path) -> dict[str, Any]:
    """Frontmatter only, for every decision record. Bodies stay in barracuda."""
    directory = barracuda_root / MEMEX_DECISIONS
    records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, Any]] = []

    for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
        rel = path.relative_to(barracuda_root).as_posix()
        result = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if not result.ok:
            failures.append({"path": rel, "error": result.error or "unknown"})
            continue
        if result.warnings:
            warnings.append({"path": rel, "warnings": list(result.warnings)})
        row: dict[str, Any] = {"path": rel}
        for key in INDEX_KEYS:
            value = result.data.get(key)
            if key in LIST_KEYS:
                if value is None:
                    value = []
                elif not isinstance(value, list):
                    value = [value]
            elif isinstance(value, (int, float)):
                value = str(value)
            row[key] = value
        records.append(row)

    return {
        "source": MEMEX_DECISIONS,
        "count": len(records),
        "records": records,
        "parse_failures": failures,
        "parse_warnings": warnings,
        "limit": (
            "Frontmatter is read by a YAML subset parser, not a YAML implementation. "
            "parse_failures lists records this index does not describe at all; parse_warnings "
            "lists records it describes INCOMPLETELY — a wrapped inline list or a nested "
            "mapping the subset cannot hold. A zero in either is a claim about the parser as "
            "much as about the corpus."
        ),
    }


# --- the findings log ----------------------------------------------------------------------


def _load_findings(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if not path.is_file():
        return [], "absent"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, OSError) as exc:
        return [], f"unreadable: {exc}"
    if not isinstance(data, list):
        return [], f"not a list: {type(data).__name__}"
    return [row for row in data if isinstance(row, dict)], None


def build_findings_index(barracuda_root: Path) -> dict[str, Any]:
    """Field-level census of the findings log, plus the divergence of its second copy."""
    primary_path = barracuda_root / FINDINGS_PRIMARY
    secondary_path = barracuda_root / FINDINGS_SECONDARY
    rows, primary_error = _load_findings(primary_path)
    secondary_rows, secondary_error = _load_findings(secondary_path)

    missing: list[str] = []
    if primary_error:
        missing.append(f"{FINDINGS_PRIMARY} ({primary_error})")
    if secondary_error:
        missing.append(f"{FINDINGS_SECONDARY} ({secondary_error})")

    fields: dict[str, Any] = {}
    for name in FINDINGS_FIELDS:
        present = overlong = 0
        longest = 0
        for row in rows:
            value = row.get(name)
            if value is None or value == "":
                continue
            present += 1
            length = len(str(value))
            longest = max(longest, length)
            if length > TOKEN_MAX and name != "title":
                overlong += 1
        fields[name] = {
            "present": present,
            "null": len(rows) - present,
            "overlong": overlong,
            "longest": longest,
        }

    fork: dict[str, Any] | None = None
    if secondary_rows:
        primary_ids = {str(r.get("id")) for r in rows if r.get("id") is not None}
        secondary_ids = {str(r.get("id")) for r in secondary_rows if r.get("id") is not None}
        fork = {
            "secondary": FINDINGS_SECONDARY,
            "secondary_count": len(secondary_rows),
            "shared": len(primary_ids & secondary_ids),
            "only_in_primary": len(primary_ids - secondary_ids),
            "only_in_secondary": len(secondary_ids - primary_ids),
        }

    sources = []
    for label, path in (("primary", primary_path), ("secondary", secondary_path)):
        if path.is_file():
            sources.append(
                {
                    "role": label,
                    "path": path.relative_to(barracuda_root).as_posix(),
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                }
            )

    return {
        "source": FINDINGS_PRIMARY,
        "count": len(rows),
        "fields": fields,
        "duplicate_authority": fork,
        "sources": sources,
        "missing": missing,
        "limit": (
            "A null rate is a property of the log, not of the work: an unfilled severity may "
            "mean the finding had none, or that nobody was asked for one. This census says how "
            "many are unfilled and nothing about why. Nothing in barracuda validates this file, "
            "so these counts are the first check it has ever had."
        ),
    }


# --- 1.1-1.3 the capture --------------------------------------------------------------------


def _plan(barracuda_root: Path, claude_home: Path) -> tuple[
    list[spec.Target], dict[str, list[str]], dict[str, list[dict[str, object]]]
]:
    targets: list[spec.Target] = []
    missing: dict[str, list[str]] = {spec.PROJECT: [], spec.AMBIENT: []}
    censuses: dict[str, list[dict[str, object]]] = {spec.PROJECT: [], spec.AMBIENT: []}

    for rule in spec.ALL_RULES:
        root = barracuda_root if rule.layer == spec.PROJECT else claude_home
        if rule.kind == "census":
            censuses[rule.layer].extend(spec.census(rule, root))
            continue
        found = list(spec.expand(rule, root))
        if not found and rule.required:
            missing[rule.layer].append(rule.pattern)
        targets.extend(found)

    # A rule may match a path another rule already claimed; keep one copy, in a stable order.
    unique: dict[tuple[str, str], spec.Target] = {}
    for target in targets:
        unique[(target.layer, str(target.relpath))] = target
    return [unique[key] for key in sorted(unique)], missing, censuses


def _refuse_relabel(destination: Path, identity: str, *, force: bool) -> None:
    """A label may not be made to mean two snapshots."""
    existing = destination / "manifest.json"
    if not existing.is_file() or force:
        return
    try:
        prior = json.loads(existing.read_text(encoding="utf-8")).get("identity")
    except (OSError, ValueError):
        return
    if prior and prior != identity:
        raise SnapshotIdentityError(
            f"{destination.name} already holds a different snapshot "
            f"({prior[:8]} vs {identity[:8]}). The subject or the capture spec moved since it "
            f"was taken, so re-using the label would make every finding citing it ambiguous.\n"
            f"Use a new --date label, or --force to overwrite deliberately."
        )


def take_snapshot(
    out_root: Path,
    *,
    label: str,
    barracuda_root: Path | None = None,
    claude_home: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> SnapshotResult:
    """Freeze the harness into ``out_root/label``.

    Raises :class:`WriteBoundaryError` if asked to write into the subject, or if the subject's git
    state changed while we were reading it.
    """
    barracuda_root = Path(barracuda_root or paths.BARRACUDA_ROOT).expanduser().resolve()
    claude_home = Path(claude_home or paths.CLAUDE_HOME).expanduser().resolve()
    out_root = assert_writable(Path(out_root))
    destination = assert_writable(out_root / label)

    targets, missing, censuses = _plan(barracuda_root, claude_home)
    if dry_run:
        return SnapshotResult(path=destination, manifest={}, planned=tuple(targets))

    out_root.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix=f".{label}.capture-", dir=out_root))
    before = git_state(barracuda_root)
    try:
        layers: dict[str, dict[str, Any]] = {
            layer: {"files": [], "missing": missing[layer], "census": censuses[layer]}
            for layer in (spec.PROJECT, spec.AMBIENT)
        }
        for target in targets:
            copy_to = assert_writable(candidate / target.layer / target.relpath)
            copy_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target.source, copy_to)
            layers[target.layer]["files"].append(_entry(target, copied=copy_to))

        index = build_memex_index(barracuda_root)
        index_bytes = dumps(index).encode("utf-8")
        assert_writable(candidate / "memex-index.json").write_bytes(index_bytes)

        findings = build_findings_index(barracuda_root)
        findings_bytes = dumps(findings).encode("utf-8")
        assert_writable(candidate / "findings-index.json").write_bytes(findings_bytes)

        after = git_state(barracuda_root)
        if after != before:
            raise WriteBoundaryError(
                "the subject's git state changed during the snapshot — refusing to write a "
                "manifest "
                f"that would misdescribe it.\n  before: {before}\n  after:  {after}"
            )

        # The git check above only sees barracuda's tracked files. The ambient layer under
        # ~/.claude has no VCS at all, and neither do untracked files — so re-hash every source
        # we copied and confirm it still matches the bytes captured in the candidate tree.
        recorded = {
            (layer, entry["path"]): entry["sha256"]
            for layer, block in layers.items()
            for entry in block["files"]
        }
        moved = sorted(
            f"{t.layer}/{t.relpath}"
            for t in targets
            if not t.source.is_file()
            or _sha256(t.source) != recorded[(t.layer, str(t.relpath))]
        )
        if moved:
            raise WriteBoundaryError(
                "these files changed while the snapshot was reading them, so the manifest would "
                "misdescribe them — re-run when the subject is quiet:\n  " + "\n  ".join(moved)
            )

        _validate_candidate(candidate, layers, index_bytes, findings_bytes)
        pin = instrument()
        content_digest = _content_identity(layers, index_bytes, findings_bytes)
        identity = _identity(
            pin["spec_fingerprint"], before.get("head"), claude_version(),
            content_digest=content_digest,
        )
        _refuse_relabel(destination, identity, force=force)

        manifest: dict[str, Any] = {
            "schema": MANIFEST_SCHEMA,
            "label": label,
            "snapshot_id": f"{label}@{identity[:8]}",
            "identity": identity,
            "instrument": pin,
            "subject": {
                "barracuda": {"root": str(barracuda_root), **before},
                "claude_home": str(claude_home),
                "claude_version": claude_version(),
                "external_tools": external_tools(),
            },
            # G01. Recorded provenance, and deliberately NOT folded into `identity` above: a
            # snapshot's identity is the spec plus subject state and captured content, so a local
            # tool upgrade must not rename an otherwise unmoved capture. Deterministic by
            # construction — the block carries no clock.
            "toolchain": manifest_toolchain_block(),
            "layers": layers,
            "memex": {
                "count": index["count"],
                "parse_failures": len(index["parse_failures"]),
                "sha256": hashlib.sha256(index_bytes).hexdigest(),
            },
            "findings": {
                "count": findings["count"],
                "missing": findings["missing"],
                "sha256": hashlib.sha256(findings_bytes).hexdigest(),
            },
        }
        manifest_bytes = dumps(manifest).encode("utf-8")
        assert_writable(candidate / "manifest.json").write_bytes(manifest_bytes)
        assert_writable(candidate / "capture.json").write_bytes(
            dumps(
                {
                    "captured_at": dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "bearhug_version": __version__,
                    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "note": (
                        "Volatile provenance, and it points AT the pin rather than the pin "
                        "pointing "
                        "at it: manifest.json is hashed here. Hashing this file into the manifest "
                        "instead would make the manifest move on every run and destroy the "
                        "determinism it exists to have."
                    ),
                }
            ).encode("utf-8")
        )

        if destination.is_dir() and not force:
            try:
                prior = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prior = None
            if isinstance(prior, dict) and prior.get("identity") == identity:
                return SnapshotResult(path=destination, manifest=prior, planned=tuple(targets))

        _publish_candidate(candidate, destination, force=force)
        return SnapshotResult(path=destination, manifest=manifest, planned=tuple(targets))
    finally:
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)


def live_hashes(barracuda_root: Path, claude_home: Path) -> dict[str, str]:
    """Hash the live harness with the same rules a snapshot uses — the input to drift."""
    targets, _, _ = _plan(barracuda_root, claude_home)
    return {f"{t.layer}/{t.relpath}": _sha256(t.source) for t in targets}


def manifest_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    """The same mapping, read back out of a stored manifest."""
    out: dict[str, str] = {}
    for layer, block in manifest.get("layers", {}).items():
        for entry in block.get("files", []):
            out[f"{layer}/{PurePosixPath(entry['path'])}"] = entry["sha256"]
    return out
