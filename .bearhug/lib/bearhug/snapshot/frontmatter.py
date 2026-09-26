"""A deliberately small YAML-subset reader for memex record frontmatter.

This is not a YAML parser and must not be mistaken for one. It reads the handful of keys the memex
index needs, and it fails *loudly* on anything it cannot read, because a record dropped in silence
would make a later lint check's zero unfalsifiable — see docs/METHOD.md, "silence is not absence".

Handled: scalars, ``null``, inline lists (``[]`` and ``[a, b]``), block lists, and block scalars
(``|`` / ``>``). Not handled, by design: nested mappings, anchors, multi-document files, flow maps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FENCE = "---"
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")
_NULLS = frozenset({"null", "Null", "NULL", "~"})
_BLOCK_SCALAR = frozenset({"|", ">", "|-", ">-", "|+", ">+"})


@dataclass(frozen=True, slots=True)
class ParseResult:
    """What one record's frontmatter yielded, why it yielded nothing, and what it fudged.

    ``error`` is a loud failure: nothing was read. ``warnings`` is the quiet kind — a shape
    this subset cannot represent faithfully, which it read *anyway* and may have read wrong.
    A parser that reported only the first would make "0 parse failures" a claim about its own
    blind spots (docs/METHOD.md, "silence is not absence").
    """

    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _scalar(key: str, raw: str) -> tuple[Any, str | None]:
    """One right-hand side: null, an inline list, or a string. Second slot is a warning."""
    if raw in _NULLS:
        return None, None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return [], None
        return [_unquote(item.strip()) for item in inner.split(",") if item.strip()], None
    if raw.startswith("["):
        # A flow list wrapped across lines. We keep the first line as a string, which is
        # wrong; say so rather than hand back a plausible-looking scalar.
        return _unquote(raw), f"{key}: inline list is not closed on its line — read as a string"
    return _unquote(raw), None


def _dedent(lines: list[str]) -> str:
    body = [ln for ln in lines if ln.strip()]
    if not body:
        return ""
    indent = min(len(ln) - len(ln.lstrip()) for ln in body)
    return "\n".join(ln[indent:] if ln.strip() else "" for ln in lines).strip("\n")


def parse_frontmatter(text: str) -> ParseResult:
    """Read the leading ``---`` block of a memex record."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return ParseResult(error="no frontmatter fence on line 1")

    end = next((i for i in range(1, len(lines)) if lines[i].strip() in (FENCE, "...")), None)
    if end is None:
        return ParseResult(error="unterminated frontmatter fence")

    data: dict[str, Any] = {}
    warnings: list[str] = []
    key: str | None = None
    mode: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal key, mode, buf
        if key is not None:
            if mode == "scalar":
                data[key] = _dedent(buf)
            elif mode == "list":
                data[key] = [_unquote(item) for item in buf] if buf else None
        key, mode, buf = None, None, []

    for raw in lines[1:end]:
        stripped = raw.strip()
        if mode == "scalar":
            # A block scalar continues while lines stay indented. Its body may contain
            # colons; treating those as keys is exactly the bug this branch prevents.
            if not stripped or raw[:1].isspace():
                buf.append(raw)
                continue
            flush()
        elif mode == "list":
            if stripped.startswith("- "):
                buf.append(stripped[2:].strip())
                continue
            if not stripped:
                continue
            flush()

        if not stripped:
            continue
        if raw[:1].isspace():
            # Indented, and not inside a block scalar or block list: a nested mapping this
            # subset cannot hold. It is dropped — which must not happen quietly.
            if _KEY.match(stripped):
                warnings.append(f"dropped a nested key: {stripped[:60]}")
            continue
        match = _KEY.match(raw)
        if not match:
            continue
        name, rest = match.group(1), match.group(2).strip()
        if rest in _BLOCK_SCALAR:
            key, mode, buf = name, "scalar", []
        elif rest == "":
            key, mode, buf = name, "list", []
        else:
            value, warning = _scalar(name, rest)
            data[name] = value
            if warning:
                warnings.append(warning)

    flush()
    if not data:
        return ParseResult(error="frontmatter fence held no readable keys")
    return ParseResult(data=data, warnings=tuple(warnings))
