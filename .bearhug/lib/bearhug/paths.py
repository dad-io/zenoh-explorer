"""Well-known locations, and the guard that keeps bear-hug read-only.

Every path bear-hug reads from outside its own repo is named here, so the blast radius of the
tool is enumerable rather than scattered across modules.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

# --- bear-hug's own repo -------------------------------------------------------------------

# Installed launchers set this to the target's private ``.bearhug`` directory.  Keeping the
# runtime root explicit means campaign state and packaged Python code stay together after the
# target is copied or the development checkout is removed; the source checkout remains the
# default for Bear Hug's own commands.
_RUNTIME_ROOT = os.environ.get("BEARHUG_RUNTIME_ROOT")
REPO_ROOT = (
    Path(_RUNTIME_ROOT).expanduser().resolve()
    if _RUNTIME_ROOT
    else Path(__file__).resolve().parents[2]
)

# Operational startup keeps its measurements together without overwriting the lab's reports.
# Set before importing Bear Hug (the startup runner supplies it to child commands).  An installed
# project bundle is executable code *inside the subject*, not a safe place for mutable campaign
# custody.  Give each installed checkout a relocatable, machine-local default outside the project;
# the launcher must not commit an absolute path into the project just to achieve that separation.
if _RUNTIME_ROOT:
    _STATE_HOME = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    ).expanduser()
    _PROJECT_STATE_KEY = hashlib.sha256(str(REPO_ROOT.parent).encode()).hexdigest()[:24]
    _DEFAULT_ARTIFACT_ROOT = _STATE_HOME / "bearhug" / "projects" / _PROJECT_STATE_KEY
else:
    _DEFAULT_ARTIFACT_ROOT = REPO_ROOT
ARTIFACT_ROOT = Path(
    os.environ.get("BEARHUG_ARTIFACT_ROOT", _DEFAULT_ARTIFACT_ROOT)
).expanduser().resolve()
SNAPSHOTS_DIR = Path(
    os.environ.get("BEARHUG_SNAPSHOTS_ROOT", ARTIFACT_ROOT / "snapshots")
).expanduser().resolve()
FINDINGS_DIR = ARTIFACT_ROOT / "findings"
PATCHES_DIR = REPO_ROOT / "patches"
REPORTS_DIR = ARTIFACT_ROOT / "reports"
RUNS_DIR = ARTIFACT_ROOT / "runs"
PROVIDER_OBSERVATIONS_DIR = RUNS_DIR / "provider-sessions"
CORPUS_DIR = Path(
    os.environ.get("BEARHUG_CORPUS_ROOT", ARTIFACT_ROOT / "corpus")
).expanduser().resolve()
FIXTURES_DIR = REPO_ROOT / "fixtures"

# --- the project under study (READ ONLY) ---------------------------------------------------

# The project is explicit when supplied by a caller.  With no selection, use a non-existent
# sentinel next to the Bear Hug checkout.  Treating Bear Hug itself as the selected project made
# the general write boundary disappear: callers using PROJECT_ROOT could modify the checkout.
# BEARHUG_BARRACUDA_ROOT and BARRACUDA_ROOT are compatibility-only aliases for older callers and
# will be removed after the repository-wide migration.
_DEFAULT_PROJECT_ROOT = REPO_ROOT.parent / ".bearhug-no-project-selected"
_PROJECT_ROOT_VALUE = os.environ.get("BEARHUG_PROJECT_ROOT")
if _PROJECT_ROOT_VALUE is None:
    _PROJECT_ROOT_VALUE = os.environ.get("BEARHUG_BARRACUDA_ROOT")
PROJECT_ROOT = Path(_PROJECT_ROOT_VALUE or _DEFAULT_PROJECT_ROOT).expanduser().resolve()
BARRACUDA_ROOT = PROJECT_ROOT  # compatibility alias; use PROJECT_ROOT in new code

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
JOBS_DIR = CLAUDE_HOME / "jobs"


#: Claude Code encodes a project's transcript directory by replacing "/" with "-".
def transcripts_dir(project_root: Path | None = None) -> Path:
    """Return the ~/.claude/projects/ directory holding a project's session transcripts."""
    root = (project_root or PROJECT_ROOT).resolve()
    return CLAUDE_HOME / "projects" / str(root).replace("/", "-")


TASK_STORE = CLAUDE_HOME / "tasks"
#: Where the runtime's telemetry is read from (never written by the lab). The project-local
#: `.bearhug/telemetry/v1` location is canonical; the pre-project layout remains a read fallback.
TELEMETRY_DIR = PROJECT_ROOT / ".bearhug" / "telemetry" / "v1"
LEGACY_TELEMETRY_DIR = CLAUDE_HOME / "telemetry" / "bearhug" / "v1"
HISTORY_FILE = CLAUDE_HOME / "history.jsonl"
PLUGINS_CACHE = CLAUDE_HOME / "plugins" / "cache"

#: The 2026-08-14 frozen archive of ~/.claude/projects, used as the reproducible corpus.
#:
#: D08 (Sam, 2026-09-01): the system's state is project-based, not split with the home directory.
#: The archive lives under the project, gitignored (212 MB); the home-directory copy is a NAMED
#: fallback for a machine where the move has not happened, never a silent default.
PROJECT_FROZEN_CORPUS = REPO_ROOT / "corpus" / "frozen" / "projects-2026-08-14.zip"
HOME_FROZEN_CORPUS = CLAUDE_HOME / "projects.zip"


@dataclass(frozen=True, slots=True)
class FrozenCorpusLocation:
    path: Path
    location: str  # explicit | project | home-fallback | missing
    note: str


def frozen_corpus() -> FrozenCorpusLocation:
    """Where the frozen archive is, and which of the three places it was found in."""
    explicit = os.environ.get("BEARHUG_FROZEN_CORPUS")
    if explicit:
        return FrozenCorpusLocation(Path(explicit), "explicit", "BEARHUG_FROZEN_CORPUS set")
    if PROJECT_FROZEN_CORPUS.is_file():
        return FrozenCorpusLocation(PROJECT_FROZEN_CORPUS, "project", "project-based (D08)")
    if HOME_FROZEN_CORPUS.is_file():
        return FrozenCorpusLocation(
            HOME_FROZEN_CORPUS, "home-fallback",
            "reading the home-directory copy; D08 rules the archive lives under corpus/frozen/ — "
            "move it (runs/home-cleanup-2026-09-01/cleanup-home.sh does) to stop this fallback",
        )
    return FrozenCorpusLocation(PROJECT_FROZEN_CORPUS, "missing", "no frozen archive found")


#: Kept for callers that only need a path; resolved at import from the same rule.
FROZEN_CORPUS_ZIP = frozen_corpus().path


# --- the read-only guard -------------------------------------------------------------------


class WriteBoundaryError(RuntimeError):
    """Raised when bear-hug is about to write somewhere the charter forbids."""


#: Roots bear-hug must never write into, no matter what a caller passes.  The selected project is
#: always protected; the no-selection sentinel deliberately remains forbidden as well.
FORBIDDEN_WRITE_ROOTS: tuple[Path, ...] = (PROJECT_ROOT, CLAUDE_HOME)


def assert_writable(target: Path) -> Path:
    """Raise unless ``target`` is somewhere bear-hug is permitted to write.

    Permitted means: inside Bear Hug's repo, configured artifact root, or system scratch.
    Everything under an explicitly selected external project and ~/.claude is refused — that is
    the charter, enforced in code rather than in prose.
    """
    resolved = Path(target).expanduser().resolve()
    for forbidden in FORBIDDEN_WRITE_ROOTS:
        forbidden = forbidden.expanduser().resolve()
        if resolved == forbidden or forbidden in resolved.parents:
            raise WriteBoundaryError(
                f"refusing to write to {resolved}: it is under {forbidden}, "
                f"which bear-hug only ever reads (see docs/CHARTER.md)"
            )
    allowed_roots = (REPO_ROOT, ARTIFACT_ROOT, Path(tempfile.gettempdir()))
    if not any(
        resolved == root.resolve() or root.resolve() in resolved.parents for root in allowed_roots
    ):
        raise WriteBoundaryError(
            f"refusing to write to {resolved}: outside Bear Hug's repository, artifact root, "
            "and system scratch directory"
        )
    return resolved
