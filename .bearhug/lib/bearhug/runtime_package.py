"""R01 — install the vendored runtime into a fixture directory.

Every later runtime test gets its copy through `install_runtime`, never by importing the in-tree
package. That is deliberate: a test that picked up the in-tree copy would prove nothing about the
copied one, and promotion installs a copy.

The copy is also the thing P01 hashes, so it must be byte-identical run to run and must contain
exactly the runtime — no caches, no tests, no lab code, no dependency metadata.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from bearhug.paths import REPO_ROOT

#: The lab-side source of the vendored runtime.
RUNTIME_SOURCE = REPO_ROOT / "runtime" / "bearhug_runtime"

#: Exact interpreter surface the promoted runtime imports today. This is intentionally not
#: ``sys.stdlib_module_names``: membership in a developer's full Python distribution does not
#: prove availability in the bare interpreter that runs Barracuda's hooks. A new module requires
#: an explicit change here and is exercised under bare ``python3`` by the package tests.
RUNTIME_STDLIB_MODULES = (
    "__future__",
    "argparse",
    "collections",
    "contextlib",
    "dataclasses",
    "datetime",
    "fcntl",
    "hashlib",
    "json",
    "os",
    "pathlib",
    "re",
    "shlex",
    "shutil",
    "sys",
    "time",
    "typing",
    "unicodedata",
    "uuid",
)

#: Where a Barracuda-owned session installs it. Imported from the runtime itself so there is one
#: authority for the string rather than a lab copy that can drift from the package's own claim.
def _promoted_install_path() -> str:
    import ast

    source = (RUNTIME_SOURCE / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "PROMOTED_INSTALL_PATH" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("the runtime package must declare PROMOTED_INSTALL_PATH")


PROMOTED_INSTALL_PATH = _promoted_install_path()

#: Excluded from the copy, and EXACTLY what the hash excludes. `__pycache__` is the one that
#: matters: four .pyc files reached a commit once already when the hook battery ran out of a
#: snapshot, and a stray one here would change the promotion hash without changing any source.
#:
#: This list carried `*_test.py` and `test_*.py` too until 2026-08-31. Decision 0299 closed the
#: exclusion set at three, and the pair was a real divergence rather than a cosmetic one: a test
#: file inside the runtime would have been HASHED and not INSTALLED, so the installed tree's
#: recomputed digest would not match the manifest that declared it. A Barracuda-owned session
#: found the discrepancy in the round-4 review; `test_the_two_exclusion_sets_agree` now pins it.
_EXCLUDED_DIRS = ("__pycache__",)
_EXCLUDED_SUFFIXES = (".pyc",)
_EXCLUDED_NAMES = (".DS_Store",)
_EXCLUDE = shutil.ignore_patterns(
    *_EXCLUDED_DIRS,
    *(f"*{suffix}" for suffix in _EXCLUDED_SUFFIXES),
    *_EXCLUDED_NAMES,
)


def runtime_source_files(source: Path | None = None) -> list[tuple[str, Path]]:
    """Return the exact source file set that both installation paths may copy.

    Symlinks are refused rather than followed.  ``copytree`` follows a directory symlink by
    default while ``Path.rglob`` does not descend through it; allowing one would let the fixture
    installer hash bytes that the promotion manifest and materialiser omit.
    """
    source = Path(source) if source is not None else RUNTIME_SOURCE
    paths = list(source.rglob("*"))
    symlinks = sorted(
        path.relative_to(source).as_posix()
        for path in paths
        if path.is_symlink()
    )
    if symlinks:
        raise RuntimeError(
            "runtime source may not contain symlinks; identity would depend on the copy path: "
            + ", ".join(symlinks)
        )

    found: list[tuple[str, Path]] = []
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part in _EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.suffix in _EXCLUDED_SUFFIXES or path.name in _EXCLUDED_NAMES:
            continue
        found.append((relative.as_posix(), path))
    found.sort(key=lambda pair: pair[0])
    return found


def install_runtime(destination: Path) -> Path:
    """Copy the runtime into ``destination`` and return the installed package directory.

    Idempotent: an existing copy is removed first, so installing twice is byte-identical rather
    than a merge of two trees.
    """
    # Validate before touching the destination. A refused source must leave no partial install.
    runtime_source_files()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / RUNTIME_SOURCE.name
    if target.exists():
        shutil.rmtree(target)
    # copy2 preserves mtimes; the promotion hash covers paths and bytes, never timestamps.
    shutil.copytree(RUNTIME_SOURCE, target, ignore=_EXCLUDE)
    return target


# `runtime_files()` was removed 2026-08-31. It was dead code whose docstring claimed to be "the
# input to R02's hash" — it was not; the hash reads `bearhug_runtime.runtime_files()`, which
# returns (relpath, path) pairs. Its exclusion set had already drifted from the runtime's own by
# omitting `.DS_Store`, so on a macOS checkout the two answers to "which files are the runtime"
# would differ. That is exactly the duplicate authority the runtime's own docstring says
# `runtime_root()` exists to prevent. Callers use `bearhug_runtime.runtime_files()`.
