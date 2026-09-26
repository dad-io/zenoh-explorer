"""A disposable, barracuda-shaped repo — and the guard that keeps hooks away from the real one.

Barracuda's gates act on the repo they are pointed at: `go-postedit.sh` runs `gofmt -w`,
`memex-hook.sh` writes stamps, the git hooks refuse commits. Running them with
`CLAUDE_PROJECT_DIR` aimed at the subject would have the lab mutating the thing it measures on
the very phase built to test the gates. The guard below refuses that, and it is checked before
any hook is executed rather than documented and hoped for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from bearhug import paths


class ScratchBoundaryError(RuntimeError):
    """Raised when a hook run would be pointed at anything but a disposable repo."""


def assert_scratch(target: Path) -> Path:
    """Raise unless ``target`` is somewhere a hook may be allowed to write."""
    resolved = Path(target).expanduser().resolve()
    forbidden = [
        paths.BARRACUDA_ROOT.expanduser().resolve(),
        paths.CLAUDE_HOME.expanduser().resolve(),
        paths.REPO_ROOT / "snapshots",
    ]
    for root in forbidden:
        if resolved == root or root in resolved.parents:
            raise ScratchBoundaryError(
                f"refusing to run a hook with CLAUDE_PROJECT_DIR={resolved}: it is under "
                f"{root}. Hooks mutate the repo they are given — gofmt -w, stamps, git "
                f"refusals — so Phase 3 runs only against a disposable repo."
            )
    allowed = (
        Path(tempfile.gettempdir()).resolve(),
        (paths.REPO_ROOT / "fixtures").resolve(),
        paths.RUNS_DIR.resolve(),
    )
    # Strictly UNDER an allowed root, never equal to one: `build_fixture_repo` rmtrees
    # `dest` before writing it, and `dest` equal to `fixtures/` or `runs/` themselves (a
    # missing `/repo` on the caller's part) would delete the committed fixtures directory or
    # every run in `runs/` rather than a disposable subdirectory of either.
    if resolved in allowed or not any(a in resolved.parents for a in allowed):
        raise ScratchBoundaryError(
            f"refusing to run a hook against {resolved}: not strictly under a scratch root "
            f"({', '.join(str(a) for a in allowed)})."
        )
    _refuse_escaping_symlinks(resolved)
    return resolved


def _refuse_escaping_symlinks(root: Path) -> None:
    """Refuse if anything INSIDE ``root`` resolves outside it via a symlink.

    The checks above validate ``root`` itself — but a symlink inside an approved scratch root
    can still point at the subject, e.g. `internal/svc/x.go -> project-barracuda/...`, and a
    gate doing `gofmt -w internal/svc/x.go` would then write through the link into the thing
    being measured. `build_fixture_repo` never plants such a link today; this is a residual
    guard against one arriving by any other route.

    Uses `os.walk(followlinks=False)` rather than `Path.rglob`, which follows symlinked
    directories: rglob would descend into whatever a symlink points at (project-barracuda, in
    the case this guards against) just to check it, which is both slow and the exact read this
    function exists to avoid performing.
    """
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in (*dirnames, *filenames):
            entry = current / name
            if not entry.is_symlink():
                continue
            real = entry.resolve()
            if real != root and root not in real.parents:
                raise ScratchBoundaryError(
                    f"refusing {root}: {entry} resolves to {real}, escaping the scratch "
                    f"root via a symlink"
                )


#: Enough barracuda shape for the gates to recognise the repo and act.
#:
#: The `.go` files live under `opcua/`, not a bare `internal/svc/` at the repo root, because
#: `deepcheck.py`'s own module detection (`MODULES = ("opcua", "barracuda")`) requires a path
#: SEGMENT named exactly one of those two — `should_run` on a bare `internal/svc/worker.go`
#: resolves to no module and the gate declines with `skip-not-go`. That was measured as
#: deep-check.py's INERTNESS finding, and it was an artifact of this layout, not of the gate:
#: the gate ran, decided the file was outside every known module, and was right to decline.
_LAYOUT: dict[str, str] = {
    "go.mod": "module example.com/fixture\n\ngo 1.25.0\n",
    "opcua/internal/svc/worker.go": "package svc\n\n// Work does the thing.\nfunc Work() error "
                                    "{\n\treturn nil\n}\n",
    "opcua/internal/svc/worker_test.go": "package svc\n\nimport \"testing\"\n\n"
                                         "func TestWork(t *testing.T) {\n\tif err := Work(); "
                                         "err != nil {\n\t\tt.Fatal(err)\n\t}\n}\n",
    # Deliberately misformatted and vet-dirty: a gate that never fires on a constructed
    # positive tells you nothing when it is silent elsewhere. Also the reason EVERY deep-check
    # run on this module fails go vet — it vets the WHOLE module, not just the edited package,
    # which is the exact gap go-postedit.sh (package-scoped) cannot see.
    "opcua/internal/svc/messy.go": "package svc\n\nimport \"fmt\"\n\n"
                                   "func  Messy( ) {\n  fmt.Printf(\"%d\", \"not a number\")\n}\n",
    "README.md": "# fixture repo\n\nDisposable. Regenerate with `bearhug hooks init`.\n",
    "docs/memex/decisions/0001-a-decision.md":
        "---\nid: 1\ntitle: A decision\nstatus: accepted\nsupersedes: []\n"
        "superseded_by: null\nevidence:\n  - opcua/internal/svc/worker.go#Work\nsources:\n"
        "  - CLAUDE.md §1\n---\n\nBody.\n",
    # The harness's own board shape (boardrows.py, decision 0274 vocabulary, seven cells). The
    # first battery (2026-09-02) ran with a three-column row that parsed positionally as
    # ruling='the fixture row', execution='', so joinkey-lint C6 blocked every turn end.
    "docs/superpowers/plans/BOARD.md":
        "# BOARD\n\n## 5. Open rows by phase\n\n"
        "| # | entry | ruling | execution | Notes | blocked | authority |\n"
        "|---|---|---|---|---|---|---|\n"
        "| 1 | [P0] the fixture row | accepted | unstarted | fixture | — | — |\n",
    "LEDGER.md": "# LEDGER\n\n| row | state |\n|---|---|\n| 1 | JOIN row 1 |\n",
    "CLAUDE.md": "# CLAUDE.md — fixture\n\n## 0. Hard safety rules\n\n"
                 "1. **Never push.** Not from a hook run.\n",
    "findings/findings.json": "[]\n",
}


def build_fixture_repo(dest: Path) -> Path:
    """Materialise a disposable repo the hooks will recognise. Refuses anywhere unsafe.

    Rebuilds from nothing: a prior battery's `gofmt -w` rewrites, `.automation-stamps` writes,
    and anything else a hook created only overwrote the `_LAYOUT` keys before, so they survived
    into the next run and could be read as this run's result. Wiping ``dest`` first makes a
    rebuild actually mean "as if this had never run".
    """
    dest = assert_scratch(dest)
    if dest.exists():
        shutil.rmtree(dest)
    for rel, body in _LAYOUT.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (dest / ".automation-stamps").mkdir(exist_ok=True)

    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.email=fixture@bear-hug", "-c", "user.name=fixture",
                  "commit", "-qm", "fixture repo"]):
        result = subprocess.run(
            ["git", *args], cwd=dest, env=env, check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            # Discarding this used to mean: no HEAD, and every gate shelling `git diff HEAD`
            # errors quietly against that — a silence that reads as "these fixtures did not
            # build its case" rather than as a broken fixture repo.
            raise RuntimeError(
                f"fixture repo git setup failed in {dest}: `git {' '.join(args)}` exited "
                f"{result.returncode}: {(result.stderr or result.stdout).strip()}"
            )
    return dest
