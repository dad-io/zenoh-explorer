"""Core data types shared by every phase.

A Finding is the unit of output. It carries the snapshot it was measured against and the limit of
what it proves, so that a report can never quietly present a suggestive number as a conclusion.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """Ranked as docs/METHOD.md ranks them: broken above costly above cosmetic."""

    BROKEN = "broken"  # a mechanism does not work: a dead gate, a contract violation
    COSTLY = "costly"  # it works, but it wastes context, time, or money
    COSMETIC = "cosmetic"  # it is wrong on the page but changes no behaviour
    INFO = "info"  # measured, not a defect


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.BROKEN: 0,
    Severity.COSTLY: 1,
    Severity.COSMETIC: 2,
    Severity.INFO: 3,
}


@dataclass(frozen=True, slots=True)
class Evidence:
    """Where a finding can be checked. At least one of file/run_id must be set."""

    file: str | None = None
    line: int | None = None
    run_id: str | None = None
    excerpt: str | None = None

    def render(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file or self.run_id or "?"


@dataclass(frozen=True, slots=True)
class Finding:
    """One measured defect or observation.

    ``id`` is stable across runs so two reports can be diffed (see plan 6.4). Build it from the
    check name and the thing it points at, never from a counter.
    """

    id: str
    check: str
    severity: Severity
    summary: str
    snapshot: str
    evidence: tuple[Evidence, ...] = ()
    limit: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Section:
    """One numbered section of a CLAUDE.md, e.g. "0a", "4", "8a"."""

    id: str
    title: str
    line_start: int
    line_end: int
    body: str = ""
    directives: list[Directive] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return self.line_end - self.line_start + 1

    @property
    def top_level_bullets(self) -> int:
        """The section's own list items. See lint.parse.top_level_bullets for why."""
        from bearhug.lint.parse import top_level_bullets

        return top_level_bullets(self)


@dataclass(frozen=True, slots=True)
class Directive:
    """A single imperative claim inside a section — the unit the REPEAT check clusters."""

    text: str
    line: int
    section_id: str
    emphasised: bool = False


@dataclass(frozen=True, slots=True)
class HookSpec:
    """One hook command as declared in .claude/settings.json."""

    event: str
    matcher: str | None
    command: str
    timeout: int | None = None
    async_rewake: bool = False
    status_message: str | None = None
    #: Which matcher group in settings.json declared this command, and where in that group's
    #: `hooks` array it sits. Together they are the execution order Claude Code uses, and
    #: nothing else in the file recovers it: Stop groups carry no matcher, so two groups of
    #: Stop commands were previously indistinguishable. D01 needs "graft advises before the
    #: five block", which is a fact about position and not about any script's contents.
    group_index: int | None = None
    position_in_group: int | None = None

    @property
    def script(self) -> str | None:
        """Repo-relative path of the script this command runs, or None if it is external.

        Delegates to lint.gates.resolve_script rather than re-splitting the command here: a
        second implementation of "which file does this hook run" is exactly the class of
        duplicate authority that made the first GATE-COVERAGE pass report zero blocking gates.
        """
        from bearhug.lint.gates import resolve_script

        return resolve_script(self.command)


def dumps(obj: Any) -> str:
    """Stable JSON for anything in this module — sorted keys, so two runs diff cleanly."""

    def default(value: Any) -> Any:
        if hasattr(value, "to_dict"):
            return value.to_dict()
        if hasattr(value, "__dataclass_fields__"):
            return asdict(value)
        raise TypeError(f"not JSON-serialisable: {type(value)!r}")

    return json.dumps(obj, indent=2, sort_keys=True, default=default) + "\n"
