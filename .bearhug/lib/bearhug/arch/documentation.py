"""Read-only observation of README coverage for authored source directories.

This is a presence observer, not a documentation quality check.  It enumerates source files
through Git, ignores common vendored, generated, and dependency directories, and finds the
nearest nonempty README in ``.md``, ``.rst``, or ``.txt`` form.  A child directory may therefore
inherit its parent's README.  The observer does not decide whether a directory is meaningful or
whether its README explains purpose, interfaces, invariants, or testing well enough.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

README_NAMES = frozenset("readme.md readme.rst readme.txt".split())  # noqa: SIM905
SOURCE_SUFFIXES = frozenset(
    ".c .cc .cpp .cs .ex .exs .go .h .hpp .java .js .jsx .jl .kt .m .php .py .rb .rs "  # noqa: SIM905
    ".scala .sh .sql .swift .ts .tsx .vue .zig".split()  # noqa: SIM905
)
SOURCE_FILENAMES = frozenset({"dockerfile", "justfile", "makefile", "taskfile"})
EXCLUDED_DIRS = frozenset(
    ".git .hg .svn .venv build coverage deps dependencies dist external gen generated "  # noqa: SIM905
    "node_modules out target third_party third-party vendor vendors venv __pycache__ .bearhug "
    ".claude .codex snapshots".split()  # noqa: SIM905
)

LIMITATIONS = [
    "Coverage is based on README presence and nearest-parent inheritance only.",
    "The observer does not assess semantic adequacy or classify meaningful components.",
    "Source paths come from tracked and nonignored untracked Git paths; README presence is read "
    "from the current worktree.",
]


def _excluded(relative: Path) -> bool:
    return any(part.casefold() in EXCLUDED_DIRS for part in relative.parts)


def _safe_path(root: Path, relative: Path) -> Path | None:
    """Return a path only when every component stays inside ``root`` and is non-symlinked."""
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    candidate = root.joinpath(*relative.parts)
    current = root
    try:
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return None
        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _git_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=3,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()[:240]
        raise RuntimeError(detail or f"git ls-files exited with {result.returncode}")
    names = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    return sorted({Path(name) for name in names if name})


def _source_file(path: Path) -> bool:
    return path.name.casefold() in SOURCE_FILENAMES or path.suffix.casefold() in SOURCE_SUFFIXES


def _nearest_readme(root: Path, directory: Path) -> tuple[Path | None, list[str]]:
    errors: list[str] = []
    current = directory
    while True:
        try:
            candidates = sorted(
                (entry for entry in current.iterdir() if entry.name.casefold() in README_NAMES),
                key=lambda entry: (entry.name.casefold(), entry.name),
            )
        except OSError as exc:
            errors.append(f"cannot inspect {current.relative_to(root).as_posix() or '.'}: {exc}")
            candidates = []
        for candidate in candidates:
            relative = candidate.relative_to(root)
            safe = _safe_path(root, relative)
            if safe is None:
                continue
            try:
                metadata = safe.stat()
            except OSError as exc:
                errors.append(f"cannot inspect {relative.as_posix()}: {exc}")
                continue
            if safe.is_file() and metadata.st_size > 0:
                try:
                    with safe.open("rb") as stream:
                        if stream.read(4096).strip():
                            return relative, errors
                except OSError as exc:
                    errors.append(f"cannot read {relative.as_posix()}: {exc}")
        if current == root:
            return None, errors
        current = current.parent


def _error(reason: str) -> dict[str, object]:
    return {
        "status": "error",
        "reason": reason,
        "rows": [],
        "counts": {"covered": 0, "direct": 0, "inherited": 0, "missing": 0},
        "errors": [reason],
        "limitations": list(LIMITATIONS),
    }


def observe(root: Path) -> dict[str, object]:
    """Observe README coverage under one Git checkout without changing it."""
    try:
        subject = Path(root).resolve(strict=True)
        if not subject.is_dir():
            return _error(f"selected root is not a directory: {subject}")
        git_paths = _git_paths(subject)
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
        return _error(f"cannot enumerate authored source paths: {exc}")

    directories: set[Path] = set()
    for relative in git_paths:
        if _excluded(relative):
            continue
        candidate = _safe_path(subject, relative)
        if candidate is None:
            continue
        try:
            if candidate.is_file() and _source_file(candidate):
                directories.add(candidate.parent)
        except OSError:
            continue

    rows: list[dict[str, str | None]] = []
    errors: list[str] = []
    for directory in sorted(directories, key=lambda path: path.relative_to(subject).as_posix()):
        readme, readme_errors = _nearest_readme(subject, directory)
        errors.extend(readme_errors)
        directory_name = directory.relative_to(subject).as_posix() or "."
        readme_name = readme.as_posix() if readme else None
        coverage = "missing" if readme is None else (
            "direct" if readme.parent == directory.relative_to(subject) else "inherited"
        )
        rows.append({"directory": directory_name, "readme": readme_name, "coverage": coverage})

    counts = {
        name: sum(row["coverage"] == name for row in rows)
        for name in ("direct", "inherited", "missing")
    }
    counts["covered"] = counts["direct"] + counts["inherited"]
    if errors:
        status = "error"
        reason = "README coverage was only partially observable."
    elif not rows:
        status = "unreported"
        reason = "No authored source directories were enumerated."
    elif counts["missing"]:
        status = "missing"
        reason = "Some enumerated source directories have no nonempty README."
    else:
        status = "covered"
        reason = "Every enumerated source directory has direct or inherited README coverage."
    return {
        "status": status,
        "reason": reason,
        "rows": rows,
        "counts": counts,
        "errors": errors,
        "limitations": list(LIMITATIONS),
    }


__all__ = ["observe"]
