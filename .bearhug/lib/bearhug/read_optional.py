"""Read a file if present, or fall back to a stated default, and say which happened.

A project observed before setup (mode 1, docs/OPERATING-MODES.md) has neither `CLAUDE.md` nor
`.claude/settings.json` yet. Four call sites across `lint`, `hooks` and `replay` each need "read
this file if it exists, or use an empty default" for exactly that reason, and each grew its own
copy independently. The read itself is just `path.is_file()` then a plain read, with no room for
two correct implementations to disagree, so one shared version replaces the four.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_text_or_default(path: Path, default: str = "") -> tuple[str, bool]:
    """`path`'s text, and whether `path` was absent (`default` is returned verbatim then)."""
    if not path.is_file():
        return default, True
    return path.read_text(encoding="utf-8"), False


def read_json_or_default(
    path: Path, default: dict[str, Any] | None = None
) -> tuple[dict[str, Any], bool]:
    """`path`'s parsed JSON object, and whether `path` was absent (`default`, or `{}`, then)."""
    if not path.is_file():
        return ({} if default is None else default), True
    return json.loads(path.read_text(encoding="utf-8")), False
