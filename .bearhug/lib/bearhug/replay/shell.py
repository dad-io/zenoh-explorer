"""Shell-command tokenisation shared by every lab detector that reads a Bash tool call.

Mirrors project-barracuda's `hardsafety.py` approach without importing it (docs/CHARTER.md):
heredoc bodies are stripped so prose inside them cannot occupy a verb position, then the command
is split into quote-aware segments on `; | & && ||`, and leading `VAR=value` assignments are
dropped so the verb is `tokens[0]`. One implementation, so the git detectors, the DLV classifier
and anything after them cannot disagree about where a verb is.
"""

from __future__ import annotations

import re
import shlex

OPERATORS = (";", "|", "&", "&&", "||")
_CONTINUATION = re.compile(r"\\\n")
#: `NAME=value` wherever an assignment can start (line start, after an operator, or as a leading
#: env assignment); the value may be a `$(...)` substitution or a quoted string.
_ASSIGNMENT = re.compile(
    r"(?:^|(?<=[\s;&|]))(?:export\s+)?([A-Za-z_]\w*)="
    r"((?:\$\([^)]*\)|\"[^\"]*\"|'[^']*'|[^\s;&|\"'])+)"
)
_HEREDOC_RE = re.compile(r"<<-?~?\s*(['\"]?)(\w+)\1")


def strip_heredocs(src: str) -> str:
    """Drop heredoc bodies so prose inside them (a commit message, a decision record) cannot
    occupy a verb position. Same reason `hardsafety.py` does this first."""
    lines = src.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        match = _HEREDOC_RE.search(line)
        i += 1
        if match:
            tag = match.group(2)
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1  # skip the terminator line itself
    return "\n".join(out)


def segments(command: str) -> list[list[str]]:
    """A command as quote-aware token segments, split on `; | & && ||` AND on a newline.

    M14 (2026-09-01) found seven real `dlv test|debug` invocations classed word-only because they
    sat on the second line of a multi-line command: `shlex` reads a newline as whitespace, so
    `cd x⏎dlv test` was one segment whose verb was `cd`. A backslash-newline is a continuation and
    is joined first; every other newline is a `;`. Heredoc bodies are stripped before either.
    """
    src = _CONTINUATION.sub(" ", strip_heredocs(command))
    try:
        # The newline is a PUNCTUATION character, not whitespace, so it ends a segment — and a
        # `# comment` line still ends at its own newline instead of swallowing the command that
        # follows it (which is what replacing newlines with `;` before lexing did).
        lexer = shlex.shlex(src, posix=True, punctuation_chars="();<>|&\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = src.replace("&&", " ; ").replace("||", " ; ").replace("\n", " ; ").split()
    out: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in OPERATORS or (token and set(token) <= {";", "|", "&", "\n"}):
            if current:
                out.append(current)
                current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    return out


def strip_env(tokens: list[str]) -> list[str]:
    """Drop leading `VAR=value` assignments so the verb is `tokens[0]`."""
    i = 0
    while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
        name = tokens[i].split("=", 1)[0]
        if name and (name[0].isalpha() or name[0] == "_") and name.replace("_", "a").isalnum():
            i += 1
            continue
        break
    return tokens[i:]


def bindings(command: str) -> dict[str, str]:
    """Every `NAME=value` a command binds, quotes removed — so a verb written `"$DLV"` can be
    resolved to the path the same command assigned. One level; nothing outside the command."""
    return {
        name: value.strip("\"'")
        for name, value in _ASSIGNMENT.findall(strip_heredocs(command))
    }


def resolve_verb(token: str, bound: dict[str, str]) -> str | None:
    """The program a verb token names: a `$NAME`/`${NAME}` resolved through `bound` (None when it
    is unbound — never guessed), otherwise the token itself with any surviving quotes removed."""
    verb = token.strip("\"'")
    if verb.startswith("$"):
        return bound.get(verb[1:].strip("{}"))
    return verb


__all__ = [
    "OPERATORS", "bindings", "resolve_verb", "segments", "strip_env", "strip_heredocs",
]
