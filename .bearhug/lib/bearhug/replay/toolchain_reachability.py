"""G04a — measure which Go-writing tool calls can reach the toolchain hooks.

The unit is deliberately a tool call, not a file: a Bash command may write several paths and an
opaque inline interpreter may establish a write without exposing a safe path. Matcher reachability
is evaluated separately from write parsing because a perfect resolver cannot run in a process the
settings matcher never starts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from bearhug.lint.gates import parse_hooks
from bearhug.lint.reachability import matcher_tools
from bearhug.replay.ledger import _go_path, bash_writes_go
from bearhug.replay.transcript import Event, iter_events

MEASUREMENT_VERSION: Final = "1.0.0"
TOOLCHAIN_HOOKS: Final = frozenset({"go-postedit.sh", "deep-check.py"})
DIRECT_WRITE_TOOLS: Final = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


@dataclass(slots=True)
class HookReachability:
    reached: int = 0
    missed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(slots=True)
class ToolchainReachability:
    total_go_write_calls: int = 0
    by_tool: dict[str, int] = field(default_factory=dict)
    hooks: dict[str, HookReachability] = field(default_factory=dict)
    unit: str = "go_writing_tool_calls"
    measurement_version: str = MEASUREMENT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "measurement_version": self.measurement_version,
            "unit": self.unit,
            "total_go_write_calls": self.total_go_write_calls,
            "by_tool": dict(sorted(self.by_tool.items())),
            "hooks": {
                name: self.hooks[name].as_dict()
                for name in sorted(self.hooks)
            },
        }


def is_go_write(event: Event) -> bool:
    """Whether one normalized tool event establishes a Go write."""
    if event.kind != "tool_use":
        return False
    if event.name in DIRECT_WRITE_TOOLS:
        return _go_path(event.payload) is not None
    if event.name == "Bash":
        return bash_writes_go(event.payload.get("command", ""))
    return False


def _hook_matchers(settings: dict[str, Any]) -> dict[str, list[frozenset[str]]]:
    matchers: dict[str, list[frozenset[str]]] = {name: [] for name in TOOLCHAIN_HOOKS}
    for spec in parse_hooks(settings):
        script = Path(spec.script).name if spec.script else ""
        if spec.event != "PostToolUse" or script not in TOOLCHAIN_HOOKS:
            continue
        matchers[script].append(matcher_tools(spec.matcher))
    return matchers


def _delivered(tool_name: str, matchers: list[frozenset[str]]) -> bool:
    # An empty matcher set means the registration has no matcher and receives the whole event.
    return any(not tools or tool_name in tools for tools in matchers)


def measure_toolchain_reachability(
    paths: Iterable[Path], settings: dict[str, Any]
) -> ToolchainReachability:
    """Count Go-writing tool calls reached and missed by each configured toolchain hook."""
    matchers = _hook_matchers(settings)
    measured = ToolchainReachability(
        hooks={name: HookReachability() for name in sorted(TOOLCHAIN_HOOKS)}
    )
    for path in paths:
        for event in iter_events(path):
            if not is_go_write(event):
                continue
            measured.total_go_write_calls += 1
            measured.by_tool[event.name] = measured.by_tool.get(event.name, 0) + 1
            for hook, declared in matchers.items():
                counter = measured.hooks[hook]
                if _delivered(event.name, declared):
                    counter.reached += 1
                else:
                    counter.missed += 1
    return measured


__all__ = [
    "DIRECT_WRITE_TOOLS",
    "MEASUREMENT_VERSION",
    "TOOLCHAIN_HOOKS",
    "HookReachability",
    "ToolchainReachability",
    "is_go_write",
    "measure_toolchain_reachability",
]
