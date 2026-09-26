"""forbidden_command_rules — decision logic for the forbidden-command PreToolUse gate.

This is Bear Hug's generalization of Barracuda's ``scripts/hooks/hard-safety.py``. Barracuda's
gate hand-maintains five CLAUDE.md-specific rules (never `git push`, never `git stash`, never kill
runtime processes by name, never wipe its state directory, never mutate its spec corpus) in Python
that only Barracuda can edit. This module knows no rules of its own: every rule a project wants
enforced is data, read from that PROJECT's own ``.bearhug/forbidden-commands.json``. Barracuda
adopting this gate means deleting hard-safety.py's hand-maintained Python and writing its five
rules as JSON instead — the same "own the automation" move Bear Hug makes everywhere else.

ABSENCE IS NOT A RULE. A project that has not written ``.bearhug/forbidden-commands.json`` gets an
inert gate: it allows everything and says so (see ``load_config`` returning ``None``). This module
never invents a rule a project did not ask for.

WHY THIS CATCHES WHAT `permissions.deny` CANNOT. Claude Code's ``permissions.deny`` matches a
command's literal PREFIX, so `cd sub && git push` slips past a deny rule on `git push` — the prefix
is `cd sub && git push`, not `git push`. This module inspects the FULL command: it splits on shell
operators (``;``, ``&&``, ``||``, ``|``, ``&``, newline) before matching a rule's verb, so the verb
position is what decides, regardless of where in the command it sits.

REUSED, NOT REIMPLEMENTED: heredoc/quote handling. Segmenting is done on
``runtime/bearhug_runtime/writes.py``'s ``shell_skeleton`` (which itself calls that module's
``strip_heredocs`` first) — installed alongside this gate at ``scripts/hooks/_bearhug/writes.py``
by Bear Hug's ordinary setup, and imported lazily from there (see ``_shell_helpers``), never
copied. That gives this gate, for free: a heredoc BODY is removed before matching (a commit message
or file content that merely *mentions* a forbidden verb is not a command), and a single- or
double-quoted span is blanked before matching (`git commit -m "always remember to never git push"`
does not fire — the quoted prose disappears from the skeleton before the verb check runs).

ONE DELIBERATE LIMIT, inherited from that reuse and recorded here rather than silently assumed
away: `shell_skeleton` BLANKS quoted spans; it does not unquote them. `writes.py`'s own docstring
proves the two behaviors are mutually exclusive (a resolver that unquotes `git 'push'` cannot also
blank a quoted commit message, and vice versa). Barracuda's hard-safety.py picked the other side of
that split — its own `hardsafety.py` uses `shlex` specifically so it CAN unquote a deliberately
quoted verb. This module picks blank-quotes, per this task's mandate to reuse `shell_skeleton`
rather than re-implement `hardsafety.py`'s shlex mode: a verb hidden behind deliberate quoting like
`git 'push'` is NOT caught here. Every case this module's own tests are built from — a bypass via
`&&`/`;`/`|`/`cd x && ...`, a heredoc body, and ordinary quoted prose — is caught correctly; only a
verb an author went out of their way to quote character-by-character is missed, which is a
narrower and more honest gap than "no PreToolUse enforcement at all".

FAIL-CLOSED ON MALFORMED CONFIG, FAIL-OPEN ON EVERYTHING ELSE. A present
``.bearhug/forbidden-commands.json`` that does not parse raises ``ForbiddenCommandConfigError`` —
the entrypoint (see
``forbidden-command-gate.py``) still exits 0 either way (a broken hook must never wedge the
session), but a project that wrote rules and got them wrong should have that recorded as a crash,
not silently treated as "no rules".
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Where a project opts in. Deliberately NOT ``docs/arch-rules.json``: that file already has its
#: own strictly-typed schema (layering/tests_required/decisions_cite_code -- see
#: ``bearhug.arch.rules``) for a different question (is the CODE's architecture as documented),
#: and its loader refuses an unrecognised rule type. Folding an unrelated safety-gate concern into
#: that schema would mean widening a schema owned by a different question, for no shared benefit.
#: A dedicated file alongside ``.bearhug/project-setup.json`` and ``.bearhug/setup.env`` is the
#: smaller, least-surprising change.
CONFIG_RELATIVE_PATH = ".bearhug/forbidden-commands.json"

_BASH_TOOL = "Bash"
_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_KNOWN_TOOLS = frozenset((_BASH_TOOL, *_EDIT_TOOLS))

#: `&&`/`||` are folded to a single `;` before the character-class split below, exactly matching
#: hard-safety.py's own operator set (`; | & && || \n`).
_MULTI_CHAR_OPERATORS = re.compile(r"&&|\|\|")
_OPERATOR_SPLIT = re.compile(r"[;&|\n]")

#: A leading `NAME=value` (or several) is an environment assignment, not the verb -- `FOO=bar git
#: push` must still be read as `git push`.
_ASSIGNMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ForbiddenCommandConfigError(ValueError):
    """The project's forbidden-command configuration exists but is not well-formed.

    Raised only when the config file is PRESENT and broken -- an absent file is not an error (see
    ``load_config``).
    """


@dataclass(frozen=True, slots=True)
class Rule:
    """One project-supplied forbidden-command rule.

    ``verb`` is the exact executable name (basename-compared, so a `/usr/bin/git` invocation still
    matches a `git` rule) and is required whenever ``applies_to`` includes ``"Bash"``. ``args``,
    when non-empty, must all appear as an in-order subsequence of the verb's non-flag-or-not
    arguments (so ``args=("push",)`` matches `git push`, `git push origin main`, and `git -C sub
    push`, but not `git status`); an empty ``args`` matches the verb alone. ``path_contains``, when
    non-empty, additionally requires some non-flag Bash argument -- or, for an Edit/Write/MultiEdit/
    NotebookEdit rule, some candidate file path -- to contain one of the given substrings; it is the
    rule's optional SCOPE. A rule that applies only to file-editing tools (no ``"Bash"``) must set
    ``path_contains``, since those tools have no verb to match on.
    """

    id: str
    reason: str
    applies_to: tuple[str, ...]
    verb: str | None = None
    args: tuple[str, ...] = ()
    path_contains: tuple[str, ...] = ()


def config_path(root: str) -> Path:
    return Path(root) / CONFIG_RELATIVE_PATH


def load_config(root: str) -> tuple[Rule, ...] | None:
    """Load a project's forbidden-command rules, or ``None`` when it has not configured any.

    ``None`` (never an empty tuple standing in for it) is the honest "this project opted out"
    answer; an empty ``forbidden_commands: []`` array is treated the same way by callers (see
    ``forbidden-command-gate.py``'s inert handling) but is distinguishable here for a caller that
    cares about the difference between "no file" and "an empty file".
    """
    path = config_path(root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ForbiddenCommandConfigError(f"cannot read {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ForbiddenCommandConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ForbiddenCommandConfigError(f"{path} must contain a JSON object")
    entries = document.get("forbidden_commands", [])
    if not isinstance(entries, list):
        raise ForbiddenCommandConfigError(f"{path}: forbidden_commands must be an array")
    rules = tuple(_parse_rule(entry, index, path) for index, entry in enumerate(entries))
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise ForbiddenCommandConfigError(f"{path}: duplicate rule id {rule.id!r}")
        seen.add(rule.id)
    return rules


def _parse_rule(entry: object, index: int, path: Path) -> Rule:
    label = f"{path}: forbidden_commands[{index}]"
    if not isinstance(entry, Mapping):
        raise ForbiddenCommandConfigError(f"{label} must be an object")
    rule_id = entry.get("id")
    reason = entry.get("reason")
    if not isinstance(rule_id, str) or not rule_id:
        raise ForbiddenCommandConfigError(f"{label}.id must be a non-empty string")
    if not isinstance(reason, str) or not reason:
        raise ForbiddenCommandConfigError(f"{label}.reason must be a non-empty string")
    applies_to = entry.get("applies_to", [_BASH_TOOL])
    if (
        not isinstance(applies_to, list)
        or not applies_to
        or any(not isinstance(item, str) for item in applies_to)
    ):
        raise ForbiddenCommandConfigError(
            f"{label}.applies_to must be a non-empty array of strings"
        )
    unknown = sorted(set(applies_to) - _KNOWN_TOOLS)
    if unknown:
        raise ForbiddenCommandConfigError(
            f"{label}.applies_to names an unsupported tool: {unknown}"
        )
    applies_to_tuple = tuple(applies_to)

    verb = entry.get("verb")
    if _BASH_TOOL in applies_to_tuple:
        if not isinstance(verb, str) or not verb:
            raise ForbiddenCommandConfigError(
                f"{label}.verb must be a non-empty string when applies_to includes Bash"
            )
    elif verb is not None:
        raise ForbiddenCommandConfigError(
            f"{label}.verb is only meaningful when applies_to includes Bash"
        )

    args = entry.get("args", [])
    if not isinstance(args, list) or any(not isinstance(item, str) or not item for item in args):
        raise ForbiddenCommandConfigError(f"{label}.args must be an array of non-empty strings")

    path_contains = entry.get("path_contains", [])
    if not isinstance(path_contains, list) or any(
        not isinstance(item, str) or not item for item in path_contains
    ):
        raise ForbiddenCommandConfigError(
            f"{label}.path_contains must be an array of non-empty strings"
        )
    if not path_contains and any(tool in _EDIT_TOOLS for tool in applies_to_tuple):
        raise ForbiddenCommandConfigError(
            f"{label}.path_contains is required for a rule scoped to a file-editing tool"
        )

    return Rule(
        id=rule_id,
        reason=reason,
        applies_to=applies_to_tuple,
        verb=verb if isinstance(verb, str) else None,
        args=tuple(args),
        path_contains=tuple(path_contains),
    )


def _shell_helpers(root: str):
    """Import ``strip_heredocs``/``shell_skeleton`` from the runtime installed beside this hook.

    Mirrors ``stop-coordinator.py``'s own ``sys.path.insert(0, root/scripts/hooks)`` +
    ``from _bearhug import ...`` pattern: the sealed runtime is a sibling of this file once Bear
    Hug setup has run (``scripts/hooks/_bearhug/writes.py``), never a copy owned by this module.
    """
    hooks_dir = os.path.join(root, "scripts", "hooks")
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    from _bearhug import writes  # noqa: PLC0415 -- deliberately lazy; see the module docstring

    return writes.strip_heredocs, writes.shell_skeleton


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    """Drop leading ``NAME=value`` tokens so the verb is ``tokens[0]``."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name, sep, _value = token.partition("=")
        if not sep or token.startswith("-") or not _ASSIGNMENT_NAME.match(name):
            break
        index += 1
    return tokens[index:]


def _segments(command: str, root: str) -> list[list[str]]:
    """The command's shell segments as whitespace-split token lists, env assignments stripped.

    Reuses ``shell_skeleton`` (which itself runs ``strip_heredocs`` first, per that module) so a
    heredoc body and quoted spans are gone before segmenting -- see the module docstring on why
    that specific reuse is the whole point versus a `permissions.deny` prefix check.
    """
    _strip_heredocs, shell_skeleton = _shell_helpers(root)
    skeleton = shell_skeleton(command)
    if not skeleton.strip():
        return []
    normalized = _MULTI_CHAR_OPERATORS.sub(";", skeleton)
    segments = []
    for raw in _OPERATOR_SPLIT.split(normalized):
        tokens = _strip_env_assignments(raw.split())
        if tokens:
            segments.append(tokens)
    return segments


def _contains_subsequence(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """Whether every item of ``needle`` occurs in ``haystack``, in order (not necessarily
    adjacent)."""
    iterator = iter(haystack)
    return all(item in iterator for item in needle)


def _match_bash(rule: Rule, tokens: list[str]) -> bool:
    if _BASH_TOOL not in rule.applies_to or not tokens:
        return False
    verb = os.path.basename(tokens[0])
    if verb != rule.verb:
        return False
    args = tokens[1:]
    if rule.args and not _contains_subsequence(args, rule.args):
        return False
    if rule.path_contains:
        non_flag = [arg for arg in args if not arg.startswith("-")]
        if not any(substring in arg for arg in non_flag for substring in rule.path_contains):
            return False
    return True


def _edit_paths(tool_name: str, tool_input: Mapping[str, object]) -> list[str]:
    found: list[str] = []
    direct = tool_input.get("file_path")
    if isinstance(direct, str) and direct:
        found.append(direct)
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, Mapping):
                    path = edit.get("file_path")
                    if isinstance(path, str) and path:
                        found.append(path)
    return found


def _match_edit(rule: Rule, tool_name: str, paths: list[str]) -> bool:
    if tool_name not in rule.applies_to or not rule.path_contains:
        return False
    return any(substring in path for path in paths for substring in rule.path_contains)


def violations(
    tool_name: object, tool_input: object, rules: Sequence[Rule], root: str
) -> list[tuple[str, str]]:
    """``[(rule_id, reason), ...]`` for one tool call, first-seen order, empty when nothing fires.

    ``rules`` is normally ``load_config(root)``'s result; an empty or ``None`` value here is the
    caller's job to treat as inert (this function simply returns no violations for no rules, same
    as for any tool call no rule matches).
    """
    if not rules or not isinstance(tool_input, Mapping) or not isinstance(tool_name, str):
        return []
    matched_ids: list[str] = []
    if tool_name == _BASH_TOOL:
        command = tool_input.get("command")
        if isinstance(command, str) and command:
            bash_rules = [rule for rule in rules if _BASH_TOOL in rule.applies_to]
            if bash_rules:
                for tokens in _segments(command, root):
                    for rule in bash_rules:
                        if _match_bash(rule, tokens):
                            matched_ids.append(rule.id)
    elif tool_name in _EDIT_TOOLS:
        paths = _edit_paths(tool_name, tool_input)
        if paths:
            for rule in rules:
                if _match_edit(rule, tool_name, paths):
                    matched_ids.append(rule.id)

    by_id = {rule.id: rule for rule in rules}
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for rule_id in matched_ids:
        if rule_id not in seen:
            seen.add(rule_id)
            result.append((rule_id, by_id[rule_id].reason))
    return result
