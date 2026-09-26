"""D05 — load the stamp-compatibility ruling.

Specification only. The disposition is `retain_as_projection`, and the table records that the
choice is forced by the writer census rather than preferred: eight of thirteen stamp writers are
not coordinator evaluators, so `.automation-stamps/` outlives this promotion regardless.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bearhug.paths import REPO_ROOT

TABLE_PATH = REPO_ROOT / "docs" / "schemas" / "stamp-compatibility.v1.json"


def load_stamp_table(path: Path = TABLE_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def stamp_writers_not_owned_by_the_coordinator(
    table: dict[str, Any] | None = None,
) -> list[str]:
    """Writers that keep stamping independently after promotion.

    The count is the argument for retaining the projection, so it is computed from the table's
    own census rather than asserted in prose.
    """
    table = table or load_stamp_table()
    owned = {f"{name}.py" for name in table["coordinator_owned_writers"]}
    return sorted(w for w in table["stamp_writers"] if w not in owned)
