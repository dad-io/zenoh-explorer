"""The vendored Bear Hug Stop runtime — stdlib only, no install step, no I/O at import.

Barracuda's gates run as bare `python3 <script>` with no venv and no network, so this package is a
directory that gets copied and imported through `sys.path.insert`. It is developed and
fixture-tested in the lab and promoted into `scripts/hooks/_bearhug/` by a Barracuda-owned
session; the lab never installs it.

Three constraints hold for every module here, and each has a test:

* **Standard library only.** An installed dependency would add exactly the failure mode this repo
  already measures — a hook whose import is missing runs inert and nobody notices.
* **No I/O at import time.** Importing runs inside every gate on every Stop, before anything has
  decided anything. A read that failed there would be indistinguishable from the
  crash-reads-as-consent shape D04 measured.
* **No import of the lab.** `bearhug` is not promoted, so an import of it would break on install
  rather than in a test.

`VERSION` is read lazily by `runtime_version()` rather than at import, for the second reason above.
"""

from __future__ import annotations

# Safe at import time: these modules compile values and regexes but touch no filesystem.
from .evaluators import (
    evaluate_dlv_verification,
    evaluate_joinkey_lint,
    evaluate_response_shape,
    evaluate_review_gate,
    evaluate_task_durability,
)
from .results import EvaluatorResult, Evidence
from .writes import (
    SOURCE_EXTENSIONS,
    WriteResolution,
    project_root,
    resolve_any_file_writes,
    resolve_go_file_writes,
    resolve_source_file_writes,
    shell_skeleton,
    strip_heredocs,
)

#: Where a Barracuda-owned session installs this package. Stated here so P01's promotion manifest
#: and R01's test agree on one string instead of two copies that can drift.
PROMOTED_INSTALL_PATH = "scripts/hooks/_bearhug"

#: Schema version this runtime speaks. Bumping it is a protocol change, not a release chore.
#: A STRING, because the schema says `{"const": "1"}`. It was first declared as an integer here,
#: which would have serialized to `1` and failed validation; a lab test now pins the two together.
EVALUATOR_SCHEMA_VERSION = "1"

#: Canonicalization identifier for `runtime_sha256`. Specified in
#: docs/proposals/R02-runtime-hash-algorithm.md. An undefined hash is worse than no hash: two
#: machines would compute different digests for identical code and `drift` would report it.
HASH_ALGORITHM = "bearhug-runtime-sha256/1"

#: RULED 2026-08-31. All three of R02's review questions were answered — Barracuda decision 0299,
#: status accepted — and the ruling reached bear-hug in the round-4 return packet, which is the
#: source cited here: bear-hug does not read Barracuda's decision records.
#:
#: Q1 confirmed as proposed: `VERSION` is INSIDE the digest.
#: Q2 confirmed as proposed: the exclusion set is CLOSED at three.
#: Q3 answered AGAINST the proposal: paths are NFC-normalized. R02 proposed no normalization and
#: recorded the macOS-NFD/Linux-NFC divergence as a limitation; the ruling closes it instead. The
#: identifier stays `bearhug-runtime-sha256/1` rather than bumping, because v1 was never accepted
#: — this ruling is what defines it.
HASH_ALGORITHM_STATUS = "approved"

#: Excluded from the hash, and nothing else is. There is deliberately no extension allowlist: a
#: data file added to the runtime IS part of the runtime, and a hash that ignored it would let a
#: change ship invisibly.
_HASH_EXCLUDED_DIRS = ("__pycache__",)
_HASH_EXCLUDED_SUFFIXES = (".pyc",)
_HASH_EXCLUDED_NAMES = (".DS_Store",)

__all__ = [
    "EVALUATOR_SCHEMA_VERSION",
    "EvaluatorResult",
    "Evidence",
    "HASH_ALGORITHM",
    "HASH_ALGORITHM_STATUS",
    "PROMOTED_INSTALL_PATH",
    "SOURCE_EXTENSIONS",
    "WriteResolution",
    "evaluate_dlv_verification",
    "evaluate_joinkey_lint",
    "evaluate_response_shape",
    "evaluate_review_gate",
    "evaluate_task_durability",
    "project_root",
    "resolve_any_file_writes",
    "resolve_go_file_writes",
    "resolve_source_file_writes",
    "runtime_files",
    "runtime_root",
    "runtime_sha256",
    "runtime_version",
    "shell_skeleton",
    "strip_heredocs",
]


def runtime_root():
    """This package's directory.

    Deliberately not a module-level constant: computing it needs `__file__` resolution, and R02
    hashes this tree, so the one place that decides "which directory is the runtime" is a function
    both can call.
    """
    from pathlib import Path

    return Path(__file__).resolve().parent


def runtime_version() -> str:
    """The version string from `VERSION`, read on demand.

    Read lazily so importing this package does no I/O. A module that read its own version at import
    time would do a filesystem access in every gate on every Stop.
    """
    return (runtime_root() / "VERSION").read_text(encoding="utf-8").strip()


def runtime_files():
    """Every file the hash covers, in canonical order.

    Sorted by POSIX relative path, never by `rglob` order: enumeration order is filesystem- and
    platform-dependent, so a digest that depended on it would differ between two machines holding
    identical bytes, and `drift` would report that as a change.

    Paths are NFC-normalized before sorting AND before hashing, so the two agree.
    """
    import unicodedata

    root = runtime_root()
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _HASH_EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.suffix in _HASH_EXCLUDED_SUFFIXES or path.name in _HASH_EXCLUDED_NAMES:
            continue
        # NFC per decision 0299. macOS hands back NFD for a decomposable filename and Linux hands
        # back NFC for the same bytes on disk, so without this the two platforms compute different
        # digests for an identical runtime and `drift` reports a change nobody made. Every path in
        # the runtime is ASCII today, where NFC is the identity — which is the point of doing it
        # now, while it cannot change the digest.
        found.append((unicodedata.normalize("NFC", relative.as_posix()), path))
    found.sort(key=lambda pair: pair[0])
    return found


def runtime_sha256() -> str:
    """A deterministic content hash of this runtime's own source.

    Each entry contributes `posix_relative_path || 0x00 || ascii_byte_length || 0x00 || bytes`.

    The length prefix is not decoration: without a delimiter AND a length between entries,
    ("ab", "c") and ("a", "bc") produce an identical byte stream, so a swap of content between two
    files would be invisible. The path is inside the hash for the same reason a rename must change
    the identity.

    Excluded by construction: mtime, permissions, ownership, the absolute install path, and the
    interpreter computing it. A touched file is not a changed runtime, and the same runtime
    installed at two paths is the same runtime.
    """
    import hashlib

    digest = hashlib.sha256()
    for relative, path in runtime_files():
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        blob = path.read_bytes()
        digest.update(str(len(blob)).encode("ascii"))
        digest.update(b"\x00")
        digest.update(blob)
    return digest.hexdigest()
