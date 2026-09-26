"""Canonical-JSON atomic file writer shared by provider evidence writers.

Every provider client writes evidence artifacts that some later reader re-derives from the
parsed value and compares byte for byte against what is on disk (see
``custody._operational_evidence_file``). That comparison uses ``json.dumps`` with
``ensure_ascii=False`` and ``allow_nan=False``; a writer that omits either option produces a
file whose bytes an honest non-ASCII record cannot reproduce, so a correct record fails closed.

This module holds one place to get those bytes right, importable by any provider client
(``app_server_client.py``, ``claude_client.py``, ...) without importing another provider client
or ``custody.py`` itself, so there is no import cycle. It does not replace a provider's own
identity-digest helper (for example ``operational_evidence._canonical`` or
``custody._canonical``); those stay where they are and keep computing content digests. This is
only the on-disk encoder a plain create/replace-style writer needs so its output matches what
those recompute on read.

This writer is for one shape only: a custody-canonical JSON **object** — sorted keys, compact
separators, non-ASCII as UTF-8, one trailing newline. ``argv.json`` is not that shape: it is a
JSON **array**, written with no ``sort_keys`` (its order is the command line, not sorted) and no
trailing newline, and custody re-checks it byte for byte against that exact, different encoding
(``custody._validate_argv``, ``custody.py:108``). Do not write ``argv.json`` with this module;
use the same inline ``json.dumps(list(argv), ensure_ascii=False, separators=(",", ":"))`` that
``claude_client.py`` and ``app_server_client.py`` already use.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from bearhug.paths import assert_writable


class CanonicalJsonError(ValueError):
    """A value cannot be encoded as custody-canonical JSON."""


def canonical_json_bytes(value: Any) -> bytes:
    """Encode ``value`` the way custody and every sibling ``_canonical`` recompute it.

    Sorted keys, compact separators, non-ASCII left as UTF-8 (not ``\\uXXXX`` escapes), no
    NaN/Infinity, one trailing newline. Byte-identical to ``custody._canonical``,
    ``operational_evidence._canonical``, ``runtime_attestation._canonical`` and — apart from its
    deliberate lack of a trailing newline — ``work_authority._canonical_json``, for any value
    they all accept. For an array that must match ``custody._validate_argv`` instead (for
    example ``argv.json``), do not use this function — see the module docstring.
    """
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CanonicalJsonError(f"value is not canonical JSON: {exc}") from exc


def write_atomic_canonical_json(path: Path, value: Any) -> Path:
    """Write ``value`` as canonical JSON, replacing any existing file at ``path`` atomically."""
    target = assert_writable(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(canonical_json_bytes(value))
    os.replace(temporary, target)
    return target


__all__ = ["CanonicalJsonError", "canonical_json_bytes", "write_atomic_canonical_json"]
