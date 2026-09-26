"""Build the small, portable component bundle used by a fresh Bear Hug setup.

The bundle is kept in one operator-owned directory.  A project setup can then refer to the
absolute paths in the returned inventory without copying tools into every checkout.  This module
does not install a provider, create a plan, index a repository, or contact a service; it only
assembles the already-installed tools and the sealed, generic Bear Hug assets.

The external tools are deliberately resolved once, before any destination is changed.  A failed
setup therefore leaves an existing bundle intact and reports the missing component by name.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class ComponentBundleError(RuntimeError):
    """The component bundle cannot be built without risking a partial installation."""


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETUP_ROOT = _REPO_ROOT / "setup"
_RUNTIME_SOURCE = _SETUP_ROOT / "runtime" / "scripts" / "hooks" / "_bearhug"
_STOP_SOURCE = _SETUP_ROOT / "runtime" / "scripts" / "hooks" / "stop-coordinator.py"
_BOARDROWS_SOURCE = _SETUP_ROOT / "runtime" / "scripts" / "hooks" / "boardrows.py"
_MEMEX_SOURCE = _SETUP_ROOT / "memexlint" / "src"
_MEMEX_SUPPORT = _SETUP_ROOT / "memexlint"
_MEMEX_HOOK_SOURCE = _SETUP_ROOT / "hooks" / "memex-hook.sh"

_MANAGED_PATHS = (
    Path("runtime"),
    Path("bin") / "memq",
    Path("bin") / "codebase-memory-mcp",
    Path("bin") / "memexlint",
    Path("hooks") / "memex-hook.sh",
    Path("graft"),
    Path("inventory.json"),
)
_REQUIRED_HOOK_COMMANDS = ("jq", "node", "python3", "sh")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_entry(path: Path, relative_to: Path) -> dict[str, Any]:
    relative = path.relative_to(relative_to).as_posix()
    if path.is_symlink():
        return {"path": relative, "symlink": os.readlink(path)}
    return {"path": relative, "sha256": _sha256(path), "bytes": path.stat().st_size}


def _file_entries(root: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(root.rglob("*")):
        relative_parts = path.relative_to(root).parts
        if "__pycache__" in relative_parts or path.suffix == ".pyc" or path.name == ".DS_Store":
            continue
        if path.is_symlink() or path.is_file():
            entries.append(_file_entry(path, root))
    return entries


def _tree_fingerprint(root: Path) -> tuple[str, int, int]:
    """Return a stable digest, byte total, and file/link count for a package closure."""

    digest = hashlib.sha256()
    total_bytes = 0
    count = 0
    for entry in _file_entries(root):
        encoded_path = entry["path"].encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        if "symlink" in entry:
            payload = ("link:" + entry["symlink"]).encode("utf-8")
            digest.update(payload)
        else:
            digest.update(bytes.fromhex(entry["sha256"]))
            total_bytes += entry["bytes"]
        count += 1
    return digest.hexdigest(), total_bytes, count


def _memex_source_fingerprint(source: Path) -> str:
    """Fingerprint the source plus the fixed local vendor inputs without compiling it."""

    source = _memex_source_root(source)
    records: list[tuple[str, Path]] = []
    for path in sorted(source.rglob("*.go")):
        if path.is_file() and not path.name.endswith("_test.go"):
            records.append((path.relative_to(source).as_posix(), path))
    records.append(("go.mod", _MEMEX_SUPPORT / "go.mod"))
    for path in sorted((_MEMEX_SUPPORT / "vendor").rglob("*")):
        if path.is_file():
            records.append(
                ("vendor/" + path.relative_to(_MEMEX_SUPPORT / "vendor").as_posix(), path)
            )
    records.sort(key=lambda record: record[0])

    digest = hashlib.sha256()
    for relative, path in records:
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _destination(value: Path | str) -> Path:
    try:
        requested = Path(value).expanduser()
    except TypeError as exc:
        raise ComponentBundleError("component destination must be a path") from exc
    if requested.is_symlink():
        raise ComponentBundleError("component destination may not be a symlink")
    if requested.exists() and not requested.is_dir():
        raise ComponentBundleError(f"component destination is not a directory: {requested}")
    return requested.resolve(strict=False)


def _executable(value: Path | str | None, command: str) -> tuple[Path, str]:
    candidate_text = shutil.which(command) if value is None else str(value)
    if not candidate_text:
        raise ComponentBundleError(
            f"required component '{command}' is missing; install it or pass {command}=PATH"
        )
    candidate = Path(candidate_text).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise ComponentBundleError(
            f"required component '{command}' is not readable: {candidate}"
        ) from exc
    if not resolved.is_file() or not (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
        raise ComponentBundleError(
            f"required component '{command}' is not an executable file: {candidate}"
        )
    return resolved, candidate_text


def _package_root(value: Path) -> Path:
    """Find the npm package enclosing a PATH launcher, retaining its dependencies."""

    try:
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise ComponentBundleError(f"Graft executable is not readable: {value}") from exc
    candidate = resolved if resolved.is_dir() else resolved.parent
    for parent in (candidate, *candidate.parents):
        package_json = parent / "package.json"
        if not package_json.is_file():
            continue
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise ComponentBundleError(
                f"Graft package metadata is unreadable: {package_json}"
            ) from exc
        if not isinstance(package, dict) or package.get("name") != "@nanonets/graft":
            continue
        if not (parent / "dist" / "cli.js").is_file():
            raise ComponentBundleError(f"Graft package has no dist/cli.js: {parent}")
        # A lone cli.js has failed before when copied out of its npm package.  Requiring the
        # adjacent dependency tree makes that failure explicit at setup time.
        if not (parent / "node_modules").is_dir():
            raise ComponentBundleError(
                f"Graft package has no adjacent node_modules dependency tree: {parent}"
            )
        for path in parent.rglob("*"):
            if not path.is_symlink():
                continue
            if os.path.isabs(os.readlink(path)):
                raise ComponentBundleError(
                    f"Graft package contains an absolute symlink; package is not portable: {path}"
                )
            try:
                link_target = (path.parent / os.readlink(path)).resolve(strict=False)
                link_target.relative_to(parent)
            except (OSError, ValueError):
                raise ComponentBundleError(
                    f"Graft package contains a symlink outside its package closure: {path}"
                ) from None
        return parent
    raise ComponentBundleError(
        "Graft executable is not inside an @nanonets/graft npm package; refusing to copy "
        "a lone launcher"
    )


def _copy_file(source: Path, target: Path, *, executable: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    mode = stat.S_IMODE(source.stat().st_mode)
    if executable:
        mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    os.chmod(target, mode)


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise ComponentBundleError(f"component source is not a real directory: {source}")
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if "__pycache__" in relative.parts or path.suffix == ".pyc" or path.name == ".DS_Store":
            continue
        destination = target / relative
        if path.is_symlink():
            raise ComponentBundleError(f"component source contains an unsupported symlink: {path}")
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            _copy_file(path, destination)


def _copy_package(source: Path, target: Path) -> None:
    """Copy an npm package while preserving relative .bin links in node_modules."""

    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_symlink():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(os.readlink(path))
        elif path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            _copy_file(path, destination)


def _memex_source_root(source: Path) -> Path:
    if not source.is_dir() or source.is_symlink():
        raise ComponentBundleError(f"Memex source is not a real directory: {source}")
    # The tracked setup asset stores generic source beneath src/, while the caller's source is
    # the Barracuda cmd/memexlint directory itself.
    if (source / "src").is_dir() and any((source / "src").glob("*.go")):
        source = source / "src"
    go_files = [
        path
        for path in source.rglob("*.go")
        if path.is_file() and not path.name.endswith("_test.go")
    ]
    if not go_files:
        raise ComponentBundleError(
            f"Memex source is empty: {source} has no non-test Go files to build memexlint"
        )
    return source


def _prepare_memex(source: Path, target: Path) -> str:
    source = _memex_source_root(source)
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*.go")):
        if not path.is_file() or path.name.endswith("_test.go"):
            continue
        relative = path.relative_to(source)
        _copy_file(path, target / relative)

    support = _MEMEX_SUPPORT
    if not (support / "go.mod").is_file() or not (support / "vendor").is_dir():
        raise ComponentBundleError(
            "bundled Memex build support is incomplete (go.mod/vendor missing)"
        )
    _copy_file(support / "go.mod", target / "go.mod")
    _copy_tree(support / "vendor", target / "vendor")
    fingerprint, _, _ = _tree_fingerprint(target)
    return fingerprint


def _reuse_inventory(
    destination: Path,
    *,
    memq_path: Path,
    codebase_memory_path: Path | None,
    graft_root: Path,
    memex_source: Path,
) -> dict[str, Any] | None:
    """Reuse a complete matching bundle so Graft's package is copied only once per host."""

    inventory_path = destination / "inventory.json"
    if not inventory_path.is_file():
        return None
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        components = inventory["components"]
        expected_runtime = _file_entries(_RUNTIME_SOURCE)
        expected_graft, _, _ = _tree_fingerprint(graft_root)
        expected_memex = _memex_source_fingerprint(memex_source)
        if inventory.get("schema_version") != 1 or inventory.get("hash_algorithm") != "sha256":
            return None
        runtime_path = destination / "runtime" / "scripts" / "hooks" / "_bearhug"
        if _has_symlink_ancestor(destination, Path("runtime")) or not runtime_path.is_dir():
            return None
        if _file_entries(runtime_path) != expected_runtime:
            return None
        if components["runtime"]["files"] != expected_runtime:
            return None
        for key, source, relative in (
            (
                "stop_coordinator",
                _STOP_SOURCE,
                Path("runtime") / "scripts" / "hooks" / "stop-coordinator.py",
            ),
            (
                "boardrows",
                _BOARDROWS_SOURCE,
                Path("runtime") / "scripts" / "hooks" / "boardrows.py",
            ),
            ("memex_hook", _MEMEX_HOOK_SOURCE, Path("hooks") / "memex-hook.sh"),
        ):
            actual = destination / relative
            if _has_symlink_ancestor(destination, relative) or not actual.is_file():
                return None
            # The inventory records bytes, while setup also projects the source mode.  A stale
            # bundle that promoted the Python stop adapter to executable must be rebuilt so an
            # established 0644 copy is not rejected as an unowned content conflict.
            if (
                components[key]["sha256"] != _sha256(source)
                or _sha256(actual) != _sha256(source)
                or stat.S_IMODE(actual.stat().st_mode) != stat.S_IMODE(source.stat().st_mode)
            ):
                return None
        memq_actual = destination / "bin" / "memq"
        memex_actual = destination / "bin" / "memexlint"
        graft_actual = destination / "graft"
        if any(
            _has_symlink_ancestor(destination, relative) or not (destination / relative).is_file()
            for relative in (
                Path("bin") / "memq",
                Path("bin") / "memexlint",
            )
        ):
            return None
        source_memq_sha = _sha256(memq_path)
        if (
            components["memq"]["sha256"] != source_memq_sha
            or _sha256(memq_actual) != source_memq_sha
        ):
            return None
        # Codebase Memory is optional, so a cached bundle is reusable only
        # when what it recorded agrees with what THIS run wants. A bundle built without it must
        # not be reused for a run that wants it (and vice versa) -- either mismatch forces a
        # rebuild rather than silently returning a stale inventory.
        if codebase_memory_path is None:
            if "codebase_memory" in components:
                return None
        else:
            codebase_memory_actual = destination / "bin" / "codebase-memory-mcp"
            if (
                _has_symlink_ancestor(destination, Path("bin") / "codebase-memory-mcp")
                or not codebase_memory_actual.is_file()
            ):
                return None
            source_codebase_memory_sha = _sha256(codebase_memory_path)
            if (
                components.get("codebase_memory", {}).get("sha256") != source_codebase_memory_sha
                or _sha256(codebase_memory_actual) != source_codebase_memory_sha
            ):
                return None
            if not codebase_memory_actual.stat().st_mode & (
                stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            ):
                return None
        if components["memexlint"]["sha256"] != _sha256(memex_actual):
            return None
        if _has_symlink_ancestor(destination, Path("graft")) or not graft_actual.is_dir():
            return None
        if _tree_fingerprint(graft_actual)[0] != expected_graft:
            return None
        if components["memexlint"]["source_sha256"] != expected_memex:
            return None
        codebase_memory_relative = Path("bin") / "codebase-memory-mcp"
        expected_managed_paths = [
            relative for relative in _MANAGED_PATHS if relative != codebase_memory_relative
        ]
        if codebase_memory_path is not None:
            expected_managed_paths.append(codebase_memory_relative)
        for relative in expected_managed_paths:
            if not (destination / relative).exists() and not (destination / relative).is_symlink():
                return None
        return inventory
    except (KeyError, OSError, TypeError, ValueError, UnicodeError):
        return None


def _has_symlink_ancestor(destination: Path, relative: Path) -> bool:
    current = destination
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _build_memex(source: Path, target: Path, go: Path) -> tuple[str, str]:
    source_fingerprint = _prepare_memex(source, target)
    binary = target.parent / "bin" / "memexlint"
    binary.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GOTOOLCHAIN"] = "local"
    # A `go.work` file in any parent of the staged build target would otherwise pull the
    # bundled Memex module into that workspace's module graph, and Go refuses to build a
    # directory that is not one of the workspace's listed modules. The bundled build is
    # always self-contained (a vendored `go.mod` copied alongside it), so workspace mode is
    # never wanted here regardless of what the target project's checkout carries above it.
    env["GOWORK"] = "off"
    try:
        result = subprocess.run(
            [str(go), "build", "-mod=vendor", "-o", str(binary), "."],
            cwd=target,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ComponentBundleError(f"could not build bundled memexlint with Go: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "no compiler output").strip()
        raise ComponentBundleError(f"bundled memexlint build failed: {detail[-2000:]}")
    if not binary.is_file():
        raise ComponentBundleError("bundled memexlint build produced no executable")
    os.chmod(binary, binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return source_fingerprint, _sha256(binary)


def _single_file_component(
    path: Path, destination: Path, *, source: str, version: str | None = None
) -> dict[str, Any]:
    return {
        "path": str(destination),
        "source": source,
        "version": version,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _install(staged: Path, destination: Path) -> None:
    destination_preexisted = destination.exists()
    destination.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=".bearhug-components-backup-", dir=destination.parent))
    moved: list[Path] = []
    remove_backup = True
    try:
        for relative in _MANAGED_PATHS:
            if _has_symlink_ancestor(destination, relative):
                raise ComponentBundleError(
                    "component destination contains a symlinked managed path: "
                    f"{destination / relative}"
                )
        for relative in _MANAGED_PATHS:
            source = staged / relative
            if not source.exists() and not source.is_symlink():
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                old = backup / relative
                old.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(old))
            shutil.move(str(source), str(target))
            moved.append(relative)
    except BaseException as exc:
        rollback_errors: list[str] = []
        for relative in reversed(moved):
            target = destination / relative
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif target.exists() or target.is_symlink():
                    target.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(f"remove {target}: {rollback_exc}")
        for old in sorted(backup.rglob("*"), reverse=True):
            if not old.is_file() and not old.is_symlink():
                continue
            try:
                relative = old.relative_to(backup)
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(target))
            except OSError as rollback_exc:
                rollback_errors.append(f"restore {old}: {rollback_exc}")
        if not destination_preexisted and destination.exists():
            try:
                if not any(destination.iterdir()):
                    destination.rmdir()
            except OSError as rollback_exc:
                rollback_errors.append(f"remove {destination}: {rollback_exc}")
        if rollback_errors:
            remove_backup = False
            detail = "; ".join(rollback_errors)
            raise ComponentBundleError(
                f"component installation failed: {exc}; rollback incomplete: {detail}; "
                f"backup retained at {backup}"
            ) from exc
        raise ComponentBundleError(f"component installation failed: {exc}") from exc
    finally:
        if remove_backup:
            shutil.rmtree(backup, ignore_errors=True)


def build_components(
    destination: Path,
    *,
    memq: Path | None = None,
    graft: Path | None = None,
    codebase_memory: Path | None = None,
    memex_source: Path | None = None,
) -> dict[str, Any]:
    """Build and install the default MemQ/Memex/Graft/Codebase Memory component bundle.

    ``destination`` is the central bundle directory, outside the target Git checkout.  ``memq``,
    ``graft``, and ``codebase_memory`` default to executables on ``PATH``.  ``memex_source`` is
    optional because the bundle carries the reviewed generic source; it exists mainly for a source
    refresh or a test.

    The returned dictionary is intentionally a small inventory rather than another installation
    manifest framework.  Every installed byte has a SHA-256 and provenance, while Graft's large
    package closure is represented by one deterministic tree fingerprint.
    """

    destination = _destination(destination)
    memq_path, memq_lookup = _executable(memq, "memq")
    graft_path, graft_lookup = _executable(graft, "graft")
    # Codebase Memory is optional. Unlike memq and graft above, a caller
    # that does not name it explicitly gets no PATH lookup and no requirement at all -- being on
    # PATH is not enough to opt in. `codebase_memory_path is None` is threaded through below as
    # "not requested"; MemQ and Graft keep their unconditional PATH-or-explicit requirement.
    codebase_memory_path: Path | None = None
    codebase_memory_lookup: str | None = None
    if codebase_memory is not None:
        codebase_memory_path, codebase_memory_lookup = _executable(
            codebase_memory, "codebase-memory-mcp"
        )
    graft_root = _package_root(graft_path)
    go_path, _ = _executable(None, "go")
    hook_commands = {name: shutil.which(name) for name in _REQUIRED_HOOK_COMMANDS}
    missing_hook = [name for name, path in hook_commands.items() if not path]
    if missing_hook:
        raise ComponentBundleError(
            "portable memex hook dependencies are missing: " + ", ".join(missing_hook)
        )
    source = Path(memex_source).expanduser() if memex_source is not None else _MEMEX_SOURCE

    for path, label in (
        (_RUNTIME_SOURCE, "sealed runtime"),
        (_STOP_SOURCE, "stop coordinator"),
        (_BOARDROWS_SOURCE, "shared boardrows parser"),
        (_MEMEX_HOOK_SOURCE, "portable memex hook"),
    ):
        if (label == "sealed runtime" and not path.is_dir()) or (
            label != "sealed runtime" and not path.is_file()
        ):
            raise ComponentBundleError(f"bundled {label} is missing: {path}")
    if not (source.is_dir() or (source / "src").is_dir()):
        raise ComponentBundleError(f"Memex source is missing: {source}")
    if not (_MEMEX_SUPPORT / "go.mod").is_file() or not (_MEMEX_SUPPORT / "vendor").is_dir():
        raise ComponentBundleError(
            "bundled Memex build support is incomplete (go.mod/vendor missing)"
        )

    if destination.is_dir():
        reused = _reuse_inventory(
            destination,
            memq_path=memq_path,
            codebase_memory_path=codebase_memory_path,
            graft_root=graft_root,
            memex_source=source,
        )
        if reused is not None:
            return reused

    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".bearhug-components-", dir=destination.parent))
    try:
        runtime_target = staged / "runtime" / "scripts" / "hooks" / "_bearhug"
        _copy_tree(_RUNTIME_SOURCE, runtime_target)
        _copy_file(
            _STOP_SOURCE,
            staged / "runtime" / "scripts" / "hooks" / "stop-coordinator.py",
        )
        _copy_file(_BOARDROWS_SOURCE, staged / "runtime" / "scripts" / "hooks" / "boardrows.py")
        _copy_file(_MEMEX_HOOK_SOURCE, staged / "hooks" / "memex-hook.sh", executable=True)

        memex_build_root = staged / ".memex-source"
        memex_source_fingerprint, _ = _build_memex(source, memex_build_root, go_path)
        shutil.rmtree(memex_build_root)

        _copy_file(memq_path, staged / "bin" / "memq", executable=True)
        if codebase_memory_path is not None:
            _copy_file(
                codebase_memory_path,
                staged / "bin" / "codebase-memory-mcp",
                executable=True,
            )
        _copy_package(graft_root, staged / "graft")

        runtime_version = (runtime_target / "VERSION").read_text(encoding="utf-8").strip()
        graft_metadata = json.loads((staged / "graft" / "package.json").read_text(encoding="utf-8"))
        if not isinstance(graft_metadata, dict):
            raise ComponentBundleError("Graft package.json must contain an object")
        graft_version = graft_metadata.get("version")
        if not isinstance(graft_version, str) or not graft_version:
            raise ComponentBundleError("Graft package.json has no usable version")
        graft_sha, graft_bytes, graft_count = _tree_fingerprint(staged / "graft")
        memex_binary = staged / "bin" / "memexlint"
        boardrows = staged / "runtime" / "scripts" / "hooks" / "boardrows.py"
        stop = staged / "runtime" / "scripts" / "hooks" / "stop-coordinator.py"
        hook = staged / "hooks" / "memex-hook.sh"

        components: dict[str, Any] = {
            "runtime": {
                "path": str(destination / "runtime" / "scripts" / "hooks" / "_bearhug"),
                "source": "sealed patches/promotion-package runtime",
                "version": runtime_version,
                "files": _file_entries(runtime_target),
            },
            "stop_coordinator": _single_file_component(
                stop,
                destination / "runtime" / "scripts" / "hooks" / "stop-coordinator.py",
                source="sealed patches/promotion-package/scripts/hooks/stop-coordinator.py",
            ),
            "boardrows": _single_file_component(
                boardrows,
                destination / "runtime" / "scripts" / "hooks" / "boardrows.py",
                source="captured 2026-09-03 shared board parser",
            ),
            "memexlint": {
                "path": str(destination / "bin" / "memexlint"),
                "source": "bundled dependency-free Go build from memexlint source",
                "version": None,
                "sha256": _sha256(memex_binary),
                "bytes": memex_binary.stat().st_size,
                "source_sha256": memex_source_fingerprint,
            },
            "memex_hook": _single_file_component(
                hook,
                destination / "hooks" / "memex-hook.sh",
                source="portable Bear Hug setup hook",
            ),
            "memq": {
                **_single_file_component(
                    staged / "bin" / "memq",
                    destination / "bin" / "memq",
                    source=f"PATH:{memq_lookup}",
                ),
                "version": None,
            },
            "graft": {
                "path": str(destination / "graft"),
                "executable": str(destination / "graft" / "dist" / "cli.js"),
                "source": f"PATH:{graft_lookup}; complete npm package closure",
                "version": graft_version,
                "sha256": graft_sha,
                "bytes": graft_bytes,
                "file_count": graft_count,
                "executable_sha256": _sha256(staged / "graft" / "dist" / "cli.js"),
            },
        }
        if codebase_memory_path is not None:
            # Present only when explicitly requested; omitted entirely
            # otherwise so `project_setup._component_specs` has nothing to build files from.
            components["codebase_memory"] = {
                **_single_file_component(
                    staged / "bin" / "codebase-memory-mcp",
                    destination / "bin" / "codebase-memory-mcp",
                    source=f"PATH:{codebase_memory_lookup}",
                ),
                "version": None,
            }
        inventory: dict[str, Any] = {
            "schema_version": 1,
            "hash_algorithm": "sha256",
            "destination": str(destination),
            "components": components,
            "hook_dependencies": hook_commands,
            "missing": [],
        }
        (staged / "inventory.json").write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _install(staged, destination)
        return inventory
    except ComponentBundleError:
        raise
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComponentBundleError(f"component bundle assembly failed: {exc}") from exc
    finally:
        shutil.rmtree(staged, ignore_errors=True)


__all__ = ["ComponentBundleError", "build_components"]
