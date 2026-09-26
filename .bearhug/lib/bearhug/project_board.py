"""Trusted, side-effect-free parser for the governed seven-cell project BOARD format."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_ROW = re.compile(r"^\|\s*(\d+)\s*\|(.*)$")
_CELL_BREAK = re.compile(r"(?<!\\)\|")
_PHASE = re.compile(r"^\s*\[(P\d+(?:·hold|·P\d+)?|corpus|UNPLACED)\]")
_DOCPATH = re.compile(r"(docs/[A-Za-z0-9._/\-]+\.md)")


def parse_board_bytes(
    raw: bytes,
    *,
    error: Callable[[str], Exception] = ValueError,
) -> list[dict[str, Any]]:
    """Parse BOARD bytes without importing or executing anything from the subject checkout."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise error("project BOARD must be UTF-8") from exc
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _ROW.match(line)
        if match is None:
            continue
        cells = [cell.strip().replace(r"\|", "|") for cell in _CELL_BREAK.split(match[2])]
        # The final unescaped pipe terminates the row and therefore leaves one empty sentinel.
        if not cells or cells[-1] != "":
            raise error(f"project BOARD row {match[1]} has no closing cell delimiter")
        cells.pop()
        if len(cells) != 6:
            raise error(f"project BOARD row {match[1]} does not have seven cells")
        entry, ruling, execution, status, blocked, authority_cell = cells
        authority_match = _DOCPATH.search(authority_cell) or _DOCPATH.search(match[2])
        phase_match = _PHASE.match(entry)
        rows.append(
            {
                "row": match[1],
                "entry": entry,
                "ruling": ruling,
                "execution": execution,
                "status": status,
                "blocked": blocked,
                "authority": authority_match.group(1) if authority_match else "",
                "phase": phase_match.group(1) if phase_match else "",
                "done": execution == "executed",
            }
        )
    return rows
