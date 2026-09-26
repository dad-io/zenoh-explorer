"""A03 — extract the architecture index from a tree, by regex, streaming, deterministically.

## What this is, and what it is not

It is a **line scanner**, not a Go parser. That is a deliberate constraint (stdlib only, and no
build of the subject), and it has a price: there are Go constructs whose meaning a regex cannot
recover. The whole design rests on paying that price honestly — **every construct this scanner
cannot resolve becomes an `unknown` record naming a machine-readable reason, never an edge.** An
edge that is merely probable is the field `A01 §7.3` prohibits outright, which is also why no
record anywhere carries a confidence.

The constructs it declines, by name:

| construct | outcome |
|---|---|
| a `.go` file with no `package` clause | `unknown` / `no_package_clause` |
| a dot import (`. "path"`) | `unknown` / `dot_import_unsupported` |
| an in-scope import naming a package no directory produced |
  `unknown` / `internal_import_names_no_extracted_package` |
| an external import matching no `require`, not stdlib-shaped |
  `unknown` / `external_import_matches_no_required_module` |
| a file above the size cap, or one that will not decode | a `parse_failures` entry |

It does **not** evaluate build tags, `go:generate`, cgo, or `replace` directives, and it does not
resolve a symbol. A package it never reached is absent from the artifact; `source_scope` is what
makes that distinguishable from a package that does not exist.

## Determinism

Filesystem walk order is not stable across machines, so every collection is sorted and every id is
derived from content rather than from discovery order. Two extractions of an unchanged tree are
byte-identical apart from `generated_at`, and a test asserts exactly that. Without it the
aggregate identity is noise and no two artifacts are comparable.

## Streaming

Go source is the unbounded input, so it is iterated line by line and never slurped; a file above
`max_file_bytes` is not opened at all. Reading past what a query needs is pointless: a non-test
file is abandoned once its package clause and import block are behind us.

## The boundary

Reading a tree is read-only by construction. Writing the artifact goes through
`paths.assert_writable`, so an attempt to emit into `project-barracuda` raises rather than
succeeding quietly — the charter enforced where the write happens, not in a docstring.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bearhug.arch import SCHEMA_VERSION
from bearhug.arch.freshness import records_identity
from bearhug.paths import assert_writable

EXTRACTOR_NAME = "bearhug-arch-extractor"
EXTRACTOR_VERSION = "1.0.0"

#: Above this, a file is a disclosed parse failure rather than a silent omission. 1 MiB is far
#: above any hand-written Go file and still bounds a generated one.
DEFAULT_MAX_FILE_BYTES = 1_048_576

#: Directory names never entered. Each is either not source, or source the subject does not own.
EXCLUDED_DIRS = (
    ".git",
    ".claude",
    "_archive",
    "__pycache__",
    "node_modules",
    "testdata",
    "vendor",
)

# --- the scanner ----------------------------------------------------------------------------

_PACKAGE = re.compile(r"^package\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:/[/*].*)?$")
_IMPORT_ONE = re.compile(r'^import\s+(?:([A-Za-z_.][A-Za-z0-9_]*)\s+)?"([^"]+)"')
_IMPORT_OPEN = re.compile(r"^import\s*\(\s*$")
_IMPORT_IN_BLOCK = re.compile(r'^\s*(?:([A-Za-z_.][A-Za-z0-9_]*)\s+)?"([^"]+)"')
_IMPORT_CLOSE = re.compile(r"^\s*\)\s*$")
#: Go's own rule for a generated file. Anchored, and only valid before the package clause.
_GENERATED = re.compile(r"^// Code generated .* DO NOT EDIT\.$")
_FUNC_TEST = re.compile(r"^func\s+(Test[A-Za-z0-9_]*)\s*\(")
_MODULE_CLAUSE = re.compile(r"^module\s+(\S+)")
_REQUIRE_ONE = re.compile(r"^require\s+(\S+)\s+\S+")
_REQUIRE_OPEN = re.compile(r"^require\s*\(\s*$")
_REQUIRE_IN_BLOCK = re.compile(r"^\s*(\S+)\s+\S+")


@dataclass(slots=True)
class GoFile:
    """What one pass over a `.go` file recovered. Absences are explicit, never defaulted."""

    path: str
    package_name: str | None = None
    package_line: int | None = None
    generated_line: int | None = None
    #: (import path, line, is_dot_import)
    imports: list[tuple[str, int, bool]] = field(default_factory=list)
    test_functions: int = 0
    is_test: bool = False


@dataclass(slots=True)
class Module:
    path: str
    directory: str
    requires: tuple[str, ...] = ()


def extractor_sha256() -> str:
    """The hash of this file's bytes, so a field's meaning is pinned to the code that made it."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _walk(root: Path):
    """Every file under ``root``, sorted, skipping excluded directories. Deterministic."""
    directories = [root]
    while directories:
        current = directories.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in EXCLUDED_DIRS:
                    directories.append(entry)
            elif entry.is_file():
                yield entry


def read_modules(root: Path, *, max_file_bytes: int) -> tuple[list[Module], list[dict]]:
    """Every in-scope module, from its own `go.mod`. The set, never a single module path."""
    modules: list[Module] = []
    failures: list[dict] = []
    for path in _walk(root):
        if path.name != "go.mod":
            continue
        relative = _relpath(path, root)
        if path.stat().st_size > max_file_bytes:
            failures.append({"path": relative, "reason": "file_exceeds_size_cap", "line": None})
            continue
        module_path: str | None = None
        requires: list[str] = []
        in_require_block = False
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.rstrip("\n")
                    if in_require_block:
                        if _IMPORT_CLOSE.match(stripped):
                            in_require_block = False
                            continue
                        match = _REQUIRE_IN_BLOCK.match(stripped)
                        if match and not stripped.lstrip().startswith("//"):
                            requires.append(match.group(1))
                        continue
                    if module_path is None:
                        clause = _MODULE_CLAUSE.match(stripped)
                        if clause:
                            module_path = clause.group(1)
                            continue
                    if _REQUIRE_OPEN.match(stripped):
                        in_require_block = True
                        continue
                    single = _REQUIRE_ONE.match(stripped)
                    if single:
                        requires.append(single.group(1))
        except (OSError, UnicodeDecodeError):
            failures.append({"path": relative, "reason": "decode_error", "line": None})
            continue
        if module_path is None:
            failures.append({"path": relative, "reason": "no_module_clause", "line": None})
            continue
        directory = path.parent.relative_to(root).as_posix()
        modules.append(
            Module(module_path, "" if directory == "." else directory, tuple(sorted(set(requires))))
        )
    modules.sort(key=lambda module: module.path)
    failures.sort(key=lambda failure: failure["path"])
    return modules, failures


def scan_go_file(path: Path, relative: str) -> GoFile:
    """One streaming pass. A non-test file stops once the imports are behind us."""
    result = GoFile(path=relative, is_test=path.name.endswith("_test.go"))
    in_import_block = False
    imports_done = False
    with path.open("r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.rstrip("\n")

            if result.package_name is None and _GENERATED.match(line):
                # Only meaningful BEFORE the package clause, which is why it is tested here.
                if result.generated_line is None:
                    result.generated_line = number
                continue

            if in_import_block:
                if _IMPORT_CLOSE.match(line):
                    in_import_block = False
                    imports_done = True
                    if not result.is_test:
                        break
                    continue
                match = _IMPORT_IN_BLOCK.match(line)
                if match:
                    result.imports.append((match.group(2), number, match.group(1) == "."))
                continue

            if result.package_name is None:
                clause = _PACKAGE.match(line)
                if clause:
                    result.package_name = clause.group(1)
                    result.package_line = number
                    continue

            if not imports_done:
                if _IMPORT_OPEN.match(line):
                    in_import_block = True
                    continue
                single = _IMPORT_ONE.match(line)
                if single:
                    result.imports.append((single.group(2), number, single.group(1) == "."))
                    continue

            if result.is_test and _FUNC_TEST.match(line):
                result.test_functions += 1
            elif not result.is_test and line.startswith("func ") and imports_done:
                # Past the header of a non-test file: nothing further answers a ruled query.
                break
    return result


def _module_for(import_path: str, modules: list[Module]) -> Module | None:
    """Longest-prefix match, so a nested module wins over its parent."""
    candidates = [
        module
        for module in modules
        if import_path == module.path or import_path.startswith(module.path + "/")
    ]
    return max(candidates, key=lambda module: len(module.path), default=None)


def _required_module_for(import_path: str, requires: tuple[str, ...]) -> str | None:
    candidates = [
        required
        for required in requires
        if import_path == required or import_path.startswith(required + "/")
    ]
    return max(candidates, key=len, default=None)


def _is_stdlib_shaped(import_path: str) -> bool:
    """Go's own rule: the first path element of a stdlib import contains no dot."""
    return "." not in import_path.split("/", 1)[0]


def _provenance(path: str, line: int | None) -> dict[str, Any]:
    return {"path": path, "line": line}


# --- Q6: decision links ---------------------------------------------------------------------


def read_decision_links(root: Path, *, max_file_bytes: int) -> tuple[list[dict], list[dict]]:
    """`docs/memex` records whose `evidence:` cites a path.

    The frontmatter is streamed and only two keys are read — `id` and the `evidence` list. A
    `ruling_verbatim:` block sits in the same frontmatter and must never reach a record, so the
    scanner reads keys at column zero and consumes only the `evidence` list items beneath one.
    """
    decisions_dir = root / "docs" / "memex" / "decisions"
    records: list[dict] = []
    failures: list[dict] = []
    if not decisions_dir.is_dir():
        return records, failures

    for path in sorted(decisions_dir.glob("*.md")):
        relative = _relpath(path, root)
        if path.stat().st_size > max_file_bytes:
            failures.append({"path": relative, "reason": "file_exceeds_size_cap", "line": None})
            continue
        decision_id: str | None = None
        evidence: list[tuple[str, int]] = []
        in_frontmatter = False
        in_evidence = False
        try:
            with path.open("r", encoding="utf-8") as handle:
                for number, raw in enumerate(handle, start=1):
                    line = raw.rstrip("\n")
                    if number == 1 and line.strip() == "---":
                        in_frontmatter = True
                        continue
                    if not in_frontmatter:
                        continue
                    if line.strip() == "---":
                        break
                    if line and not line[0].isspace():
                        in_evidence = line.startswith("evidence:")
                        if line.startswith("id:"):
                            candidate = line.split(":", 1)[1].strip().strip("\"\'")
                            if candidate.isdigit():
                                decision_id = candidate.zfill(4)  # "0138" and 125 are one id
                        continue
                    if in_evidence:
                        item = line.strip()
                        if item.startswith("- "):
                            evidence.append((item[2:].strip().strip("\"'"), number))
        except (OSError, UnicodeDecodeError):
            failures.append({"path": relative, "reason": "decode_error", "line": None})
            continue

        if decision_id is None:
            failures.append({"path": relative, "reason": "no_decision_id", "line": None})
            continue

        seen_paths: set[str] = set()
        for cited, number in evidence:
            if "/" not in cited and "." not in cited:
                # Not a path. Q6 links paths; a bare concept name is out of contract.
                continue
            cites_path, _, fragment = cited.partition("#")
            if cites_path in seen_paths:
                # One link per decision→path: two fragments into one file are one link, and a
                # second record would collide on id (the validator refuses duplicates).
                continue
            seen_paths.add(cites_path)
            records.append(
                {
                    "kind": "decision_link",
                    "id": f"decision_link:{decision_id}:{cites_path}",
                    "decision_id": decision_id,
                    "cites_path": cites_path,
                    "fragment": fragment or None,
                    "authority": "measured",
                    "provenance": _provenance(relative, number),
                }
            )
    return records, failures


# --- A06: decisions --------------------------------------------------------------------------


def read_decisions(
    root: Path, *, decision_links: list[dict], max_file_bytes: int
) -> list[dict]:
    """One `decision` record per `docs/memex/decisions/*.md` file already read for Q6.

    A06's `decisions_cite_code` rule needs a record even for a decision with zero evidence --
    that is exactly the case the rule must see, not a case absent from the index. `cites_code`
    is true iff this decision's id produced at least one `decision_link` in ``decision_links``
    (Q6's own definition of citing a path); `exempt` reads the frontmatter key `code: none`
    verbatim, the one place a Barracuda session may declare a decision process-only. Neither
    flag is inferred from the body prose -- both come from a key already at column zero in the
    frontmatter, the same place Q6 reads `id` and `evidence` and nothing else.

    Files this cannot read or that carry no `id` are silently skipped here: `read_decision_links`
    already reports them as `parse_failures`, and reporting them twice would double the count.
    """
    decisions_dir = root / "docs" / "memex" / "decisions"
    if not decisions_dir.is_dir():
        return []

    cited_ids = {link["decision_id"] for link in decision_links}
    records: list[dict] = []

    for path in sorted(decisions_dir.glob("*.md")):
        relative = _relpath(path, root)
        if path.stat().st_size > max_file_bytes:
            continue
        decision_id: str | None = None
        decision_id_line: int | None = None
        code = "absent"
        evidence: list[str] = []
        in_frontmatter = False
        in_evidence = False
        try:
            with path.open("r", encoding="utf-8") as handle:
                for number, raw in enumerate(handle, start=1):
                    line = raw.rstrip("\n")
                    if number == 1 and line.strip() == "---":
                        in_frontmatter = True
                        continue
                    if not in_frontmatter:
                        continue
                    if line.strip() == "---":
                        break
                    if line and not line[0].isspace():
                        in_evidence = line.startswith("evidence:")
                        if line.startswith("id:"):
                            candidate = line.split(":", 1)[1].strip().strip("\"\'")
                            if candidate.isdigit():
                                decision_id = candidate.zfill(4)  # "0138" and 125 are one id
                                decision_id_line = number
                        elif line.startswith("code:"):
                            value = line.split(":", 1)[1].strip().strip("\"'")
                            if value in {"none", "pending"}:
                                code = value
                        continue
                    if in_evidence:
                        item = line.strip()
                        if item.startswith("- "):
                            evidence.append(item[2:].strip().strip("\"'"))
        except (OSError, UnicodeDecodeError):
            continue

        if decision_id is None:
            continue

        records.append(
            {
                "kind": "decision",
                "id": f"decision:{decision_id}",
                "decision_id": decision_id,
                "cites_code": decision_id in cited_ids,
                "exempt": code == "none"
                or (code == "pending" and any(
                    cited.startswith("docs/superpowers/plans/") for cited in evidence
                )),
                "code": code,
                "authority": "measured",
                "provenance": _provenance(relative, decision_id_line),
            }
        )
    return records


# --- Q7: harness components -----------------------------------------------------------------


def read_harness_components(root: Path, *, max_file_bytes: int) -> tuple[list[dict], list[dict]]:
    """Scripts under `scripts/hooks/` joined to their registrations in `.claude/settings.json`.

    A script nothing registers is recorded with an empty list. That is the finding — suppressing
    it would rebuild the inert-gate blindness `stamp.sh` exists to end.
    """
    hooks_dir = root / "scripts" / "hooks"
    settings_path = root / ".claude" / "settings.json"
    records: list[dict] = []
    failures: list[dict] = []
    if not hooks_dir.is_dir():
        return records, failures

    registrations: dict[str, list[dict]] = {}
    settings_relative = ".claude/settings.json"
    if settings_path.is_file():
        if settings_path.stat().st_size > max_file_bytes:
            failures.append(
                {"path": settings_relative, "reason": "file_exceeds_size_cap", "line": None}
            )
        else:
            try:
                # Small and capped: a config file is not the unbounded input Go source is.
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                failures.append({"path": settings_relative, "reason": "decode_error", "line": None})
                settings = {}
            for event, matchers in sorted((settings.get("hooks") or {}).items()):
                if not isinstance(matchers, list):
                    continue
                for entry in matchers:
                    if not isinstance(entry, dict):
                        continue
                    matcher = entry.get("matcher")
                    for hook in entry.get("hooks") or []:
                        command = (hook or {}).get("command")
                        if not isinstance(command, str):
                            continue
                        for script in sorted(hooks_dir.iterdir(), key=lambda p: p.name):
                            if script.is_file() and script.name in command:
                                key = _relpath(script, root)
                                registration = {"event": event, "matcher": matcher}
                                if registration not in registrations.setdefault(key, []):
                                    registrations[key].append(registration)

    for script in sorted(hooks_dir.iterdir(), key=lambda p: p.name):
        if not script.is_file() or script.suffix not in {".py", ".sh"}:
            continue
        relative = _relpath(script, root)
        entries = sorted(
            registrations.get(relative, []),
            key=lambda item: (item["event"], item["matcher"] or ""),
        )
        records.append(
            {
                "kind": "harness_component",
                "id": f"harness_component:{relative}",
                "script": relative,
                "registrations": entries,
                "authority": "measured",
                "provenance": _provenance(
                    settings_relative if settings_path.is_file() else relative, None
                ),
            }
        )
    return records, failures


# --- the extraction -------------------------------------------------------------------------


def repository_state(root: Path) -> tuple[str | None, bool, list[str]]:
    """Head, dirty flag and dirty paths, read with read-only git commands. Never a write."""
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
            )
        except OSError:
            return None
        return done.stdout if done.returncode == 0 else None

    head = (git("rev-parse", "HEAD") or "").strip() or None
    status = git("status", "--porcelain")
    if status is None:
        return head, False, []
    paths = sorted(
        line[3:].strip() for line in status.splitlines() if line.strip()
    )
    return head, bool(paths), paths


def extract(
    root: Path | str,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, Any]:
    """Extract the architecture index for the tree at ``root``.

    Reading is the only thing this does to ``root``. A missing root raises rather than reporting
    an empty artifact — "nothing found" and "nowhere to look" must never be the same answer.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"no such tree to extract: {root}")

    modules, failures = read_modules(root, max_file_bytes=max_file_bytes)
    records: list[dict] = []
    unknowns: list[dict] = []

    # One pass over every .go file in an in-scope module, grouped by owning directory.
    scanned: dict[str, list[GoFile]] = {}
    for module in modules:
        module_root = root / module.directory if module.directory else root
        for path in _walk(module_root):
            if path.suffix != ".go":
                continue
            relative = _relpath(path, root)
            if _module_for_directory(relative, modules) is not module:
                continue  # a nested module owns it; it is scanned under that module
            if path.stat().st_size > max_file_bytes:
                failures.append({"path": relative, "reason": "file_exceeds_size_cap", "line": None})
                continue
            try:
                scanned.setdefault(path.parent.relative_to(root).as_posix(), []).append(
                    scan_go_file(path, relative)
                )
            except (OSError, UnicodeDecodeError):
                failures.append({"path": relative, "reason": "decode_error", "line": None})

    # Q1: one package per directory that produced a package clause.
    packages: dict[str, dict[str, Any]] = {}
    for directory in sorted(scanned):
        files = sorted(scanned[directory], key=lambda go: go.path)
        module = _module_for_directory(directory + "/x.go", modules)
        if module is None:  # pragma: no cover - a directory is only scanned under a module
            continue
        named = [go for go in files if go.package_name and not go.is_test]
        if not named:
            named = [go for go in files if go.package_name]
        for go in files:
            if go.package_name is None:
                unknowns.append(
                    {
                        "kind": "unknown",
                        "id": f"unknown:{go.path}:no_package_clause",
                        "subject": go.path,
                        "reason": "no_package_clause",
                        "authority": "product",
                        "provenance": _provenance(go.path, None),
                    }
                )
        if not named:
            continue
        owner = named[0]
        # `package a_test` is an external test package for the SAME directory, not a new one.
        name = owner.package_name or ""
        import_path = _import_path(directory, module)
        packages[import_path] = {
            "kind": "package",
            "id": f"package:{import_path}",
            "import_path": import_path,
            "name": name,
            "dir": directory,
            "module": module.path,
            "authority": "product",
            "provenance": _provenance(owner.path, owner.package_line),
        }

    records.extend(packages.values())

    # Q2-Q5, resolved against the package set now that it is complete.
    dependencies: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    for directory in sorted(scanned):
        module = _module_for_directory(directory + "/x.go", modules)
        if module is None:  # pragma: no cover
            continue
        import_path = _import_path(directory, module)
        if import_path not in packages:
            continue
        test_files: list[str] = []
        test_functions = 0
        test_provenance: tuple[str, int | None] | None = None
        entry_point: dict[str, Any] | None = None

        for go in sorted(scanned[directory], key=lambda item: item.path):
            if go.is_test:
                test_files.append(go.path)
                test_functions += go.test_functions
                if test_provenance is None:
                    test_provenance = (go.path, go.package_line)

            if go.generated_line is not None:
                records.append(
                    {
                        "kind": "generated_file",
                        "id": f"generated_file:{go.path}",
                        "path": go.path,
                        "marker_line": go.generated_line,
                        "authority": "product",
                        "provenance": _provenance(go.path, go.generated_line),
                    }
                )

            if go.package_name == "main" and not go.is_test and entry_point is None:
                # One entry point per PACKAGE: a directory with several build-tagged `package
                # main` files (Barracuda's cmd/surfacedemo) is still one program. Provenance is
                # the first such file in path order.
                entry_point = {
                    "kind": "entry_point",
                    "id": f"entry_point:{import_path}",
                    "package": import_path,
                    "dir": directory,
                    "convention": "cmd" if _under_cmd(directory) else "other",
                    "authority": "product",
                    "provenance": _provenance(go.path, go.package_line),
                }
                records.append(entry_point)

            if go.is_test:
                continue  # test imports are not architecture edges

            for imported, line, is_dot in go.imports:
                if is_dot:
                    unknowns.append(
                        {
                            "kind": "unknown",
                            "id": f"unknown:{go.path}:{line}:dot_import_unsupported",
                            "subject": imported[:256],
                            "reason": "dot_import_unsupported",
                            "authority": "product",
                            "provenance": _provenance(go.path, line),
                        }
                    )
                    continue
                target_module = _module_for(imported, modules)
                if target_module is not None:
                    if imported not in packages:
                        unknowns.append(
                            {
                                "kind": "unknown",
                                "id": (
                                    f"unknown:{go.path}:{line}:"
                                    "internal_import_names_no_extracted_package"
                                ),
                                "subject": imported[:256],
                                "reason": "internal_import_names_no_extracted_package",
                                "authority": "product",
                                "provenance": _provenance(go.path, line),
                            }
                        )
                        continue
                    if imported == import_path:
                        continue
                    edge_id = f"edge:{import_path}->{imported}"
                    edges.setdefault(
                        edge_id,
                        {
                            "kind": "edge",
                            "id": edge_id,
                            "from": import_path,
                            "to": imported,
                            "from_module": module.path,
                            "to_module": target_module.path,
                            "authority": "product",
                            "provenance": _provenance(go.path, line),
                        },
                    )
                    continue

                required = _required_module_for(imported, module.requires)
                if required is None and _is_stdlib_shaped(imported):
                    # Go's own rule identifies stdlib. The ruled contract has exactly two
                    # buckets -- in-scope module, or dependency -- so it lands here, by its
                    # import path. A03 does not invent a third bucket the contract never ruled.
                    required = imported
                if required is None:
                    unknowns.append(
                        {
                            "kind": "unknown",
                            "id": (
                                f"unknown:{go.path}:{line}:"
                                "external_import_matches_no_required_module"
                            ),
                            "subject": imported[:256],
                            "reason": "external_import_matches_no_required_module",
                            "authority": "product",
                            "provenance": _provenance(go.path, line),
                        }
                    )
                    continue
                dependency_id = f"dependency:{import_path}->{required}"
                dependencies.setdefault(
                    dependency_id,
                    {
                        "kind": "dependency",
                        "id": dependency_id,
                        "package": import_path,
                        "module_path": required,
                        "authority": "product",
                        "provenance": _provenance(go.path, line),
                    },
                )

        if test_files:
            path, line = test_provenance or (test_files[0], None)
            records.append(
                {
                    "kind": "test_mapping",
                    "id": f"test_mapping:{import_path}",
                    "package": import_path,
                    "test_files": sorted(test_files),
                    "test_function_count": test_functions,
                    "authority": "product",
                    "provenance": _provenance(path, line),
                }
            )

    records.extend(edges.values())
    records.extend(dependencies.values())

    decision_links, decision_failures = read_decision_links(root, max_file_bytes=max_file_bytes)
    decisions = read_decisions(
        root, decision_links=decision_links, max_file_bytes=max_file_bytes
    )
    components, component_failures = read_harness_components(root, max_file_bytes=max_file_bytes)
    records.extend(decision_links)
    records.extend(decisions)
    records.extend(components)
    records.extend(unknowns)
    failures.extend(decision_failures)
    failures.extend(component_failures)

    head, dirty, dirty_paths = repository_state(root)
    records.sort(key=lambda record: record["id"])
    failures.sort(key=lambda failure: (failure["path"], failure["reason"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "extractor": {
            "name": EXTRACTOR_NAME,
            "version": EXTRACTOR_VERSION,
            "sha256": extractor_sha256(),
        },
        "repository": {
            "head": head,
            "dirty": dirty,
            "dirty_paths": dirty_paths,
            "source_scope": {
                "roots": sorted({module.directory or "." for module in modules}),
                "modules": [
                    {"path": module.path, "dir": module.directory or "."} for module in modules
                ],
                "excluded": list(EXCLUDED_DIRS),
                "max_file_bytes": max_file_bytes,
            },
        },
        "records": records,
        "parse_failures": failures,
        "identity": {"algorithm": "sha256", "records_sha256": records_identity(records)},
    }


def _module_for_directory(relative_file: str, modules: list[Module]) -> Module | None:
    """Which module owns a repo-relative file, by longest directory prefix."""
    candidates = [
        module
        for module in modules
        if relative_file.startswith(f"{module.directory}/" if module.directory else "")
    ]
    return max(candidates, key=lambda module: len(module.directory), default=None)


def _import_path(directory: str, module: Module) -> str:
    if module.directory and directory == module.directory:
        return module.path
    if not module.directory:
        return f"{module.path}/{directory}" if directory != "." else module.path
    return f"{module.path}/{directory[len(module.directory) + 1:]}"


def _under_cmd(directory: str) -> bool:
    return "cmd" in directory.split("/")


def write_artifact(artifact: dict[str, Any], out: Path | str) -> Path:
    """Write the artifact as sorted, indented JSON. Refuses any path the charter forbids."""
    target = assert_writable(Path(out))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "EXCLUDED_DIRS",
    "EXTRACTOR_NAME",
    "EXTRACTOR_VERSION",
    "GoFile",
    "Module",
    "extract",
    "extractor_sha256",
    "read_decision_links",
    "read_decisions",
    "read_harness_components",
    "read_modules",
    "repository_state",
    "scan_go_file",
    "write_artifact",
]
