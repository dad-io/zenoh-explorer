"""P01 — the promotion manifest: exactly the runtime, reproducible from one commit.

What gets installed into `scripts/hooks/_bearhug/` must be byte-identical on every generation from
the same commit, or P03's drift check cannot tell a correct installation from a tampered one.

Bear Hug builds this and emits it as a patch. It never installs it: that is a Barracuda-owned
session's action, per the charter.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bearhug.paths import REPO_ROOT
from bearhug.runtime_package import (
    PROMOTED_INSTALL_PATH as PROMOTED_ROOT,
)
from bearhug.runtime_package import RUNTIME_SOURCE, install_runtime, runtime_source_files

#: ``PROMOTED_ROOT`` is read from the runtime's own declaration so the manifest and the package
#: cannot disagree about where a Barracuda-owned session installs it.

def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


#: The emitted package's own path, excluded from the cleanliness measurement below.
_PACKAGE_OUTPUT = "patches/promotion-package"


def tree_clean_excluding_package(status: str | None) -> bool:
    """Is everything committed, apart from the package this manifest is describing?

    The package is a generated artifact. Its presence cannot be evidence that the source it was
    generated from is uncommitted, which is what a plain `git status --porcelain` check made it —
    the field became unreachable, because writing the package was itself the proof of dirtiness.

    Takes the porcelain text as an argument so the rule is testable without depending on whoever
    is running the suite having a clean checkout.
    """
    if status is None:
        return False
    for line in status.splitlines():
        # Split on whitespace rather than slicing by column. `_git` strips its stdout, which
        # removes porcelain's LEADING SPACE, so a ` M path` line arrives as `M path` and a
        # fixed `line[3:]` slice ate the first character of the path — the prefix then never
        # matched and the field was stuck at False. My own fixtures included the leading space,
        # so they passed while the live repository did not.
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        path = parts[1].strip().strip('"')
        if path and not path.startswith(_PACKAGE_OUTPUT):
            return False
    return True


def _tree_clean_excluding_package() -> bool:
    return tree_clean_excluding_package(_git("status", "--porcelain"))


def _promotable_files() -> list[tuple[str, Path]]:
    """Every file the runtime consists of, in canonical order.

    Uses the same exclusion set and the same POSIX-path ordering as the runtime's own hash, so the
    manifest and `runtime_sha256()` cannot describe different trees.
    """
    return runtime_source_files(RUNTIME_SOURCE)


def _runtime_identity() -> tuple[str, str]:
    """(version, sha256), computed by the runtime itself rather than reimplemented here."""
    import sys
    import tempfile

    installed = install_runtime(Path(tempfile.mkdtemp(prefix="bearhug-manifest-")))
    sys.path.insert(0, str(installed.parent))
    try:
        for name in list(sys.modules):
            if name == "bearhug_runtime" or name.startswith("bearhug_runtime."):
                del sys.modules[name]
        import bearhug_runtime

        return bearhug_runtime.runtime_version(), bearhug_runtime.runtime_sha256()
    finally:
        sys.path.pop(0)


def _runtime_attr(name: str) -> Any:
    """One value from the runtime's own source, read via AST.

    Imported would be simpler and wrong: the promoted package uses relative imports under a
    different name, so the lab reads the declaration rather than executing it.
    """
    import ast

    from bearhug.runtime_package import RUNTIME_SOURCE

    source = (RUNTIME_SOURCE / "__init__.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"the runtime must declare {name}")


#: The captured gates the evaluators were ported FROM, and the shared parser they read the board
#: through. Digested into the manifest so a Barracuda-owned session can detect the direction
#: `runtime_sha256` is structurally blind to.
#: The capture the digests are read from. Moved 2026-08-29 → 2026-09-01 for the Round-10 ask:
#: joinkey-lint.py (C8) and boardrows.py (the GFM `\\|` splitter) changed in Barracuda, and a
#: photograph of the older files would report Barracuda's deliberate work as drift forever.
PHOTOGRAPHED_SNAPSHOT = "2026-09-01"

PHOTOGRAPHED = (
    "response-shape.py",
    "dlv-verify-gate.py",
    "task-durability.py",
    "joinkey-lint.py",
    "review-gate.py",
    "boardrows.py",
)


def _photographed_gates() -> list[dict[str, Any]]:
    """What the five evaluators were ported from, by digest.

    Round 5: "`runtime_sha256` pins the copy against itself. It detects a change to the vendored
    evaluator and is structurally blind to a change in the gate it was photographed from — the one
    direction that matters, since five commits touched these files in the six days before your
    snapshot."

    Correct, and the fix belongs in the manifest rather than in a promise: record the source
    digests, and `verify.py` compares them against the LIVE files in the target tree. A
    Barracuda-owned session then learns that a gate moved under the port, which is a fact no
    amount of hashing the copy can produce.
    """
    import hashlib

    snapshot = REPO_ROOT / "snapshots" / PHOTOGRAPHED_SNAPSHOT / "project" / "scripts" / "hooks"
    rows = []
    for name in PHOTOGRAPHED:
        path = snapshot / name
        if not path.is_file():
            rows.append({"path": f"scripts/hooks/{name}", "sha256": None,
                         "note": "absent from the snapshot capture"})
            continue
        blob = path.read_bytes()
        rows.append({
            "path": f"scripts/hooks/{name}",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob),
        })
    return rows


def build_manifest() -> dict[str, Any]:
    """The promotion manifest. Pure: it reads the runtime and writes nothing."""
    version, digest = _runtime_identity()
    files = [
        {
            "path": relative,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
        for relative, path in _promotable_files()
    ]
    from bearhug.hooks.stopscope import stop_registrations  # noqa: F401  (documented below)

    return {
        "schema_version": 1,
        "task": "P01",
        "target": PROMOTED_ROOT,
        "source_commit": _git("rev-parse", "HEAD") or "unknown",
        # The commit that last CHANGED the runtime, which is what the digest is an identity for.
        # Round 4: "the package cannot be regenerated from HEAD and match" — true of
        # `source_commit` by construction, since the commit that CONTAINS a package is always a
        # child of the one it was built from. This field does not have that problem: it is the same
        # answer before and after the package is committed, and it stays the same until the runtime
        # itself changes. Regenerate at any commit where this value is unchanged and the digest
        # matches.
        "runtime_source_commit": _git("log", "-1", "--format=%H", "--", "runtime") or "unknown",
        # Measured EXCLUDING the emitted package itself. Writing the package dirties the tree,
        # so counting it made `source_tree_clean` unreachable — a package could never record that
        # it was reproducible, because its own existence was the evidence that it was not.
        "source_tree_clean": _tree_clean_excluding_package(),
        "runtime_version": version,
        "runtime_sha256": digest,
        "hash_algorithm": _runtime_attr("HASH_ALGORITHM"),
        # Read from the runtime, never retyped. The manifest said `proposed` as a literal, so the
        # two could have disagreed about whether the algorithm was ruled — a status is a claim, and
        # a claim in a promoted artifact needs a source.
        "hash_algorithm_status": _runtime_attr("HASH_ALGORITHM_STATUS"),
        "hash_algorithm_ruling": (
            "RULED 2026-08-31, Barracuda decision 0299, status accepted. Q1 VERSION is inside the "
            "digest and Q2 the exclusion set is closed at three, both confirmed as proposed; Q3 "
            "answered AGAINST R02 — paths are NFC-normalized, where the proposal recorded the "
            "macOS-NFD/Linux-NFC divergence as a limitation. Cited from the round-4 return packet: "
            "bear-hug does not read Barracuda's decision records."
        ),
        "files": files,
        "file_count": len(files),
        # The source side of the identity. See _photographed_gates.
        "photographed_from": {
            "snapshot": PHOTOGRAPHED_SNAPSHOT,
            "why": (
                "runtime_sha256 pins the vendored COPY. It cannot detect that the gate a copy was "
                "ported from has since changed, which is the direction that matters. These digests "
                "are of the snapshotted originals; verify.py compares them to the LIVE files."
            ),
            "gates": _photographed_gates(),
        },
        # D02's ruling, carried into the promotion package so P02 sizes the settings migration
        # correctly and P04's ADR quotes the right number.
        "stop_registrations_after_promotion": 3,
        "retained_external_registrations": [
            {"command": "graft-hooks.cjs stop", "is_evaluator": False,
             "non_blocking_evidence": "PROVEN by reading, 2026-08-31",
             "proof": (
                 "A Barracuda-owned session read the delegate rather than running it. "
                 "graft-hooks.cjs line 42 is import(...).then(m => m.main(argv[2])).catch(()=>{}) "
                 "with no process.exit and no decision JSON. Its target hooks.js (11,876 bytes) "
                 "has ZERO occurrences of process.exit, 'block', decision, stopReason or "
                 "exitCode; its stop branch is `handleStop(dir); return;`, and its only stdout "
                 "write is an additionalContext shape never reached from that branch. Bear Hug "
                 "wrote that no run could establish the contract — true, but READING could, "
                 "which is what step 5 asks for anyway."
             )},
            {"command": "memex-hook.sh stop", "is_evaluator": False,
             "non_blocking_evidence": "verified from the snapshot"},
        ],
        "installed_by": "a Barracuda-owned session, never bear-hug",
        # A PREREQUISITE, not a bundled file. R10 requires the board to be read through the host's
        # shared parser so board-restore and the join-key check cannot disagree about what a row
        # is; a private parser in the runtime would recreate that disagreement silently.
        "host_prerequisites": [
            {
                "path": "scripts/hooks/boardrows.py",
                # Round-10 ask, item 6: without a digest this entry could not catch the one case
                # it exists for — a host whose boardrows has moved. Same photograph as above.
                **{
                    key: value
                    for gate in _photographed_gates()
                    if gate["path"] == "scripts/hooks/boardrows.py"
                    for key, value in gate.items()
                    if key in ("sha256", "bytes")
                },
                "why": (
                    "the runtime's ONE non-stdlib import. Resolved lazily when an evaluator "
                    "reads board state and raising MissingBoardRows if absent, so the fault "
                    "lands inside coordinator evaluation and receives the gate's ruled failure "
                    "policy rather than bypassing it or falling back to a second lifecycle "
                    "vocabulary."
                ),
                "verified_by_bear_hug": False,
            }
        ],
        "limits": [
            "This manifest describes what WOULD be installed. Nothing here has been installed, "
            "and bear-hug cannot install it.",
            "The hash algorithm was ruled on 2026-08-31 (decision 0299). bear-hug applied the "
            "ruling and has NOT seen the decision record itself; the citation is the round-4 "
            "return packet a Barracuda-owned session wrote.",
        ],
    }


def materialise(destination: Path) -> Path:
    """Copy the runtime into ``destination`` exactly as the manifest describes it."""
    destination = Path(destination)
    target = destination / Path(PROMOTED_ROOT).name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for relative, path in _promotable_files():
        out = target / relative
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, out)
    return target


def write_manifest(manifest: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
