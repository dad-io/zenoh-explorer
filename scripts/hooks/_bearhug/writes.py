"""Pure write-target resolution shared by the Bear Hug runtime.

The resolver answers three questions that R04 proved are not interchangeable:

* ``any`` — task durability cares about a BOARD or documentation write too;
* ``source`` — the review gate cares about the captured source-extension set; and
* ``go`` — DLV and Go toolchain checks care only about ``.go`` targets.

This module resolves a tool call that has already reached it.  It cannot make a Claude Code hook
whose matcher excludes ``Bash`` receive a Bash event; matcher reachability remains a settings
problem.  It also performs no filesystem validation and mutates nothing.  In particular, adding
this module does not re-enable the deliberately reverted go-postedit Bash path.  A future mutating
consumer still owes the repository/cwd/symlink/module guards recorded by R04.

For Bash, the rule is effect, not text.  Heredoc bodies and quoted data are removed before shell
write forms are matched, so a commit message that *describes* ``cat > x.go`` is not a write.  The
one exception is an inline interpreter: its program is quoted data to the shell but executable
code to Python/Perl.  There the resolver can sometimes establish that a write happened without
safely recovering the target; ``WriteResolution.opaque`` represents exactly that distinction.

IN-PROJECT-ROOT SCOPING (ported from the captured ``codewrites.py`` after a real failure: a
scratchpad heredoc under ``/private/tmp/.../scratchpad`` blocked a turn three times although
nothing entered the repo) — a write only counts when its target resolves INSIDE the project root.
``_in_project_root`` applies to every candidate path, Bash-derived or from an Edit/Write/MultiEdit
``file_path``: a relative path always counts (it is relative to the project cwd); an absolute path
under ``/tmp`` or ``/private/tmp`` never does; any other absolute path counts only if it resolves
under the root. A path this module cannot resolve (an unbound ``$VAR`` prefix) counts —
conservative, because guessing "outside" would let a turn through that a human should still see.
The root itself comes from ``project_root``: an explicit ``root`` kwarg wins; failing that, the
Stop event's own ``cwd`` field, then ``CLAUDE_PROJECT_DIR``, then the process cwd. Evaluators are
handed the event and should resolve the root once and thread it through explicitly rather than
relying on this module reading the environment on their behalf.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

# The captured review-gate vocabulary. Markdown and JSON are deliberately absent: a docs write is
# still an ``any`` write, but it is not source code for the review/DLV questions.
SOURCE_EXTENSIONS = (
    "go",
    "py",
    "sh",
    "js",
    "mjs",
    "cjs",
    "ts",
    "tsx",
    "rs",
    "proto",
    "java",
    "c",
    "h",
    "cc",
    "cpp",
)

#: NOT PROVIDED, and deliberately so: a quote-PRESERVING (shlex) resolution mode.
#:
#: R04 established that `hardsafety.py` cannot use `shell_skeleton`. A safety gate must see
#: `git 'push'` in order to unquote it, while skeleton mode removes quoted content and would leave
#: `git ''` — so the two modes are mutually exclusive, and `codewrites.py` documents the split as
#: justified rather than drift. This module provides skeleton mode only, because the coordinator
#: owns the five STOP evaluators and `hard-safety.py` is a PreToolUse gate outside that set.
#:
#: Recorded here so a later task does not assume one resolver already serves both consumers and
#: quietly migrate the safety gate onto semantics that cannot see its own trigger. Adding shlex
#: mode is a change to this module's contract and needs its own task and its own 59 captured cases.
_QUOTE_PRESERVING_MODE_PROVIDED = False

_SINGLE_PATH_EDIT_TOOLS = frozenset(("Edit", "Write", "NotebookEdit"))
_MULTI_PATH_EDIT_TOOL = "MultiEdit"
_BASH_TOOL = "Bash"


@dataclass(frozen=True, slots=True)
class WriteResolution:
    """The paths a tool call wrote, plus an honest unresolved-write bit.

    ``paths`` is first-seen and deduplicated, and already filtered to targets INSIDE the project
    root (see the module docstring's IN-PROJECT-ROOT SCOPING section). ``opaque`` is true only when
    the resolver can prove an inline interpreter wrote a file in the requested scope but quote
    stripping makes returning a target path unsafe. Callers can ask the boolean question with
    ``bool(result)`` without inventing a path.

    ``rejected_paths`` is first-seen and deduplicated like ``paths``, but holds the candidate
    targets the resolver saw and excluded ONLY because they resolved outside the project root —
    never a candidate excluded for any other reason. It exists so a caller that wants to explain a
    pass (nothing written INSIDE the project) can still name what was written outside it, without
    re-parsing the command itself. It never contributes to ``bool(result)``.
    """

    paths: tuple[str, ...] = ()
    opaque: bool = False
    rejected_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for attr_name in ("paths", "rejected_paths"):
            value = getattr(self, attr_name)
            valid = isinstance(value, tuple) and all(
                isinstance(path, str) and path for path in value
            )
            if not valid:
                raise ValueError(f"{attr_name} must be a tuple of non-empty strings")
            if len(set(value)) != len(value):
                raise ValueError(f"{attr_name} must be deduplicated")
        if not isinstance(self.opaque, bool):
            raise ValueError("opaque must be a boolean")

    def __bool__(self) -> bool:
        return bool(self.paths) or self.opaque


def _path_pattern(extensions: tuple[str, ...]) -> str:
    """A shell token ending in one of ``extensions``.

    The extension must end the token.  A regex word boundary is not enough: it also matches the
    ``.go`` prefix in ``run.go.log``, which was a measured false block in the captured gate.
    """
    alternatives = "|".join(re.escape(ext) for ext in extensions)
    return r"[^\s'\";|&>]*\.(?:" + alternatives + r")(?=$|[\s'\";|&/)]|\Z)"


# An unfiltered shell target. It is intentionally narrower than "any non-space": shell operators,
# quotes and parentheses are not path bytes in the captured forms. Quoted targets are a known
# unsupported shape inherited from the captured resolver; R05 does not silently broaden policy.
_ANY_PATH = r"[^\s'\";|&><()]+"

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_SINGLE_QUOTED = re.compile(r"'[^']*'")
_DOUBLE_QUOTED = re.compile(r'"[^"]*"')


def strip_heredocs(command: object) -> str:
    """Return ``command`` with heredoc bodies and terminators removed.

    Quoting is retained deliberately.  R04 proved that the safety parser needs a different mode
    from the write resolver: it must be able to unquote ``git 'push'``.  Exposing this smaller
    operation lets that future consumer preserve quoted verbs without copying heredoc handling.
    """
    if not isinstance(command, str) or not command:
        return ""
    output: list[str] = []
    lines = command.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        tags = [match.group(2) for match in _HEREDOC.finditer(line)]
        output.append(line)
        index += 1
        for tag in tags:
            while index < len(lines) and lines[index].strip() != tag:
                index += 1
            if index < len(lines):
                index += 1
    return "\n".join(output)


def shell_skeleton(command: object) -> str:
    """Return only the shell-visible verbs, operators, and unquoted words."""
    skeleton = strip_heredocs(command)
    if not skeleton:
        return ""
    # Single quotes first: an apostrophe inside a double-quoted string must not open a new span.
    skeleton = _SINGLE_QUOTED.sub(" '' ", skeleton)
    return _DOUBLE_QUOTED.sub(' "" ', skeleton)


def _capture_patterns(path_pattern: str, *, any_file: bool) -> tuple[re.Pattern[str], ...]:
    """The captured write forms, compiled to return their target path.

    Extension-filtered patterns can locate a target amid other arguments.  The any-file forms
    cannot use an extension as a discriminator, so commands whose target is conventionally final
    (sed/perl/cp/mv/install/git/formatters) explicitly capture the final token in that shell
    segment.  This keeps a sed expression or cp source from becoming the reported destination.
    """
    target = "(" + path_pattern + ")"
    common = (
        re.compile(r">>?\s*" + target),
        re.compile(r"\btee\s+(?:-\S+\s+)*" + target),
    )
    if any_file:
        final = target + r"\s*(?=$|[;&|\n])"
        return common + (
            re.compile(r"\b(?:sed|perl)\s+(?:-\S+\s+)*-i\b[^\n;&|]*\s+" + final),
            re.compile(
                r"\b(?:gofmt|goimports|black|prettier|rustfmt)\s+[^\n;&|]*"
                r"(?:-w|-i|--write)\b[^\n;&|]*\s+" + final
            ),
            re.compile(
                r"\b(?:cp|mv|install)\s+(?:-\S+\s+)*\S+\s+" + final
            ),
            re.compile(r"\bgit\s+(?:checkout|restore)\b[^\n;&|]*\s+" + final),
        )
    return common + (
        re.compile(r"\b(?:sed|perl)\s+(?:-\S+\s+)*-i\b[^\n]*?" + target),
        re.compile(
            r"\b(?:gofmt|goimports|black|prettier|rustfmt)\s+(?:-\S+\s+)*"
            r"(?:-w|-i|--write)\b[^\n]*?" + target
        ),
        re.compile(
            r"\b(?:cp|mv|install)\s+(?:-\S+\s+)*\S+\s+"
            + target
            + r"\s*(?:$|[;&|\n])",
            re.MULTILINE,
        ),
        re.compile(r"\bgit\s+(?:checkout|restore)\b[^\n]*?" + target),
    )


_ANY_CAPTURE = _capture_patterns(_ANY_PATH, any_file=True)
_SOURCE_CAPTURE = _capture_patterns(_path_pattern(SOURCE_EXTENSIONS), any_file=False)
_GO_CAPTURE = _capture_patterns(_path_pattern(("go",)), any_file=False)

# Inline interpreters are matched on the raw command because their quoted body is executable code,
# not shell data. All three signals are required: interpreter, write operation, path-shaped literal.
_INLINE_INTERPRETER = re.compile(
    r"(?:^|[|;&]|&&)\s*(?:python3?|perl)\b[^\n]*(?:<<|-c\b|-e\b)"
)
_INTERPRETER_WRITE = re.compile(
    r"open\([^)]*,\s*['\"][wax]['\"]|\.write\(|writelines\(|shutil\.(?:copy|move)"
)
_INTERPRETER_FILE_OPEN = re.compile(r"open\([^)]*,\s*['\"][wax]['\"]")
_INTERPRETER_COPY_MOVE = re.compile(r"shutil\.(?:copy|move)")
_ANY_PATH_LITERAL = re.compile(
    r"['\"][^'\"]*(?:[/\\]|[A-Za-z0-9_-]\.[A-Za-z0-9_-]+)[^'\"]*['\"]"
)
# Capturing, so a recovered inline-interpreter literal can be tested against the project root the
# same way a Bash-skeleton match is (see IN-PROJECT-ROOT SCOPING above). The "any" scope literal
# stays non-capturing: it is used only to prove a write happened, never to name a specific path, so
# there is nothing to root-check there.
_SOURCE_PATH_LITERAL = re.compile(
    r"['\"]([^'\"]*[A-Za-z0-9_-]\."
    r"(?:" + "|".join(re.escape(ext) for ext in SOURCE_EXTENSIONS) + r"))['\"]"
)
_GO_PATH_LITERAL = re.compile(r"['\"]([^'\"]*[A-Za-z0-9_-]\.go)['\"]")

# ---- in-project-root scoping --------------------------------------------------------------------
# Ported from the captured `codewrites.py`'s post-2026-08-26 fix. A Bash write, or an absolute
# Edit/Write/MultiEdit `file_path`, only counts when its target resolves INSIDE the project root.

#: Absolute prefixes that are ALWAYS scratch, regardless of any variable name — `/tmp` and its
#: macOS-real path `/private/tmp` (a symlink on Darwin, but Bash sees the literal text, and so must
#: this resolver).
_SCRATCH_ABS_PREFIXES = ("/tmp", "/private/tmp")

#: Variable NAMES that are conventionally scratch dirs, used only when the variable has no
#: assignment earlier in the same command (an assignment always wins — see `_parse_assignments`).
#: `TMPDIR`/`TMP`/`TEMP` are the usual OS-supplied scratch vars; `SCRATCH` is this harness's own
#: convention, and the real failure the captured fix records. `CLAUDE_PROJECT_DIR` is handled
#: separately in `_in_project_root`: it names the root itself, not scratch.
_KNOWN_SCRATCH_VARS = ("TMPDIR", "SCRATCH", "TMP", "TEMP")

#: A simple `NAME=value` or `export NAME=value` assignment, anchored to the start of a simple
#: command (start of string, or after `;`, `&&`, `||`, or a newline — the shell positions where an
#: assignment can prefix a command). Read from `strip_heredocs`' output (quoting intact, heredoc
#: bodies gone), never from the fully-blanked skeleton, or a quoted assignment value would already
#: be `''`.
_ASSIGN_RE = re.compile(
    r'(?:\A|[;\n]|&&|\|\|)\s*(?:export\s+)?'
    r'([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|\'[^\']*\'|\S*)'
)


def _parse_assignments(stripped_command: str) -> dict[str, str]:
    """``{NAME: value}`` for every simple assignment in `stripped_command`, in order — a later
    assignment overwrites an earlier one, same as the shell. Quotes are stripped from the value; an
    assignment with no value (`NAME=`) yields `""`.
    """
    out: dict[str, str] = {}
    for match in _ASSIGN_RE.finditer(stripped_command):
        name, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[name] = value
    return out


def project_root(event: Mapping[str, object] | None = None) -> str:
    """Resolve the project root a write is scoped to.

    The Stop event's own ``cwd`` field is authoritative when present. Evaluators are handed the
    event and should call this once and thread the result through explicitly (see
    ``resolve_source_file_writes``'s ``root`` keyword) rather than this module reading the
    environment on their behalf. ``CLAUDE_PROJECT_DIR`` and finally the process cwd are the
    fallback for a caller — or a test — with no event to hand in.
    """
    if isinstance(event, Mapping):
        cwd = event.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def expand_home(path: str) -> str:
    """`os.path.expanduser`, exposed so a caller elsewhere in the runtime that needs the SAME
    tilde resolution `_in_project_root` already applies for scoping (below) does not have to call
    `os.path.expanduser` directly itself. A no-op for a path that does not start with `~`."""
    return os.path.expanduser(path)


def _in_project_root(path: str, assignments: Mapping[str, str], root: str) -> bool:
    """True when a written PATH resolves INSIDE `root`.

    - Relative paths ALWAYS count: they are relative to the project cwd, so `cat > pkg/x.py` counts
      wherever `root` is.
    - `~` expands to HOME and is outside the root unless the root is under it.
    - A `$NAME`/`${NAME}` prefix is resolved against an assignment EARLIER IN THE SAME COMMAND when
      one exists (`M=/Users/x/repo; cat > $M/a.py` resolves `M` before applying this same rule to
      the result). Failing that:
        * `$CLAUDE_PROJECT_DIR` resolves to `root` itself (it names the root).
        * a KNOWN SCRATCH VAR (TMPDIR/SCRATCH/TMP/TEMP) is treated as outside the root — that is
          the whole point of a scratch variable.
        * any OTHER unresolvable variable COUNTS. Stay conservative: a prefix this resolver cannot
          resolve might resolve inside the repo, and treating it as a write that needs a human's
          attention is safer than guessing "outside" and letting a turn through.
    - An absolute path under `/tmp` or `/private/tmp` is scratch and does not count, independent of
      any variable.
    - Any other absolute path counts only if it resolves under `root` (`os.path.realpath`, so `..`
      and symlinks cannot escape the check).
    """
    if not path:
        return False
    if path.startswith("~"):
        path = os.path.expanduser(path)
    elif path.startswith("$"):
        match = re.match(r'^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(.*)$', path)
        if not match:
            return True  # a shape this resolver doesn't recognise — conservative
        name, rest = match.group(1), match.group(2)
        if name in assignments:
            path = assignments[name] + rest
        elif name == "CLAUDE_PROJECT_DIR":
            path = root + rest
        else:
            # A KNOWN SCRATCH VAR is outside the root — that is the whole point of a scratch
            # variable. Any OTHER unresolvable variable COUNTS: a prefix this resolver cannot
            # resolve might resolve inside the repo, and treating it as a write that needs a
            # human's attention is safer than guessing "outside" and letting a turn through.
            return name not in _KNOWN_SCRATCH_VARS
    if not path.startswith("/"):
        # Relative (including a resolved-but-still-relative substitution).
        return True
    for prefix in _SCRATCH_ABS_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return False
    try:
        resolved_path = os.path.realpath(path)
        resolved_root = os.path.realpath(root)
    except Exception:
        resolved_path, resolved_root = path, root
    return resolved_path == resolved_root or resolved_path.startswith(
        resolved_root.rstrip("/") + "/"
    )


def _unique_in_order(paths: list[tuple[int, str]]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for _, path in sorted(paths, key=lambda item: item[0]):
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return tuple(ordered)


def _bash_resolution(command: object, scope: str, root: str) -> WriteResolution:
    if not isinstance(command, str) or not command:
        return WriteResolution()

    if scope == "any":
        patterns = _ANY_CAPTURE
        literal = None
    elif scope == "source":
        patterns = _SOURCE_CAPTURE
        literal = _SOURCE_PATH_LITERAL
        if not any(f".{ext}" in command for ext in SOURCE_EXTENSIONS):
            return WriteResolution()
    elif scope == "go":
        patterns = _GO_CAPTURE
        literal = _GO_PATH_LITERAL
        if ".go" not in command:
            return WriteResolution()
    else:  # internal programmer error, not malformed hook input
        raise ValueError(f"unknown write scope {scope!r}")

    stripped = strip_heredocs(command)
    assignments = _parse_assignments(stripped)

    skeleton = shell_skeleton(command)
    matches: list[tuple[int, str]] = []
    rejected: list[tuple[int, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(skeleton):
            candidate_path = match.group(1)
            if _in_project_root(candidate_path, assignments, root):
                matches.append((match.start(1), candidate_path))
            else:
                rejected.append((match.start(1), candidate_path))

    paths = _unique_in_order(matches)
    rejected_paths = _unique_in_order(rejected)
    inline_write = bool(
        _INLINE_INTERPRETER.search(command) and _INTERPRETER_WRITE.search(command)
    )
    if scope == "any":
        # ``open(p, 'w')`` establishes a file write even when the path variable cannot be
        # recovered. A bare ``obj.write(...)`` does not — it may be stdout or a buffer — so that
        # looser captured form still needs a path-shaped literal somewhere in the program. There is
        # no root check here: the literal is not captured, only detected, so there is no specific
        # path to test.
        inline_in_scope = bool(
            inline_write
            and (
                _INTERPRETER_FILE_OPEN.search(command)
                or _INTERPRETER_COPY_MOVE.search(command)
                or _ANY_PATH_LITERAL.search(command)
            )
        )
    else:
        inline_in_scope = False
        if inline_write and literal is not None:
            literal_match = literal.search(command)
            if literal_match:
                literal_path = literal_match.group(1)
                if _in_project_root(literal_path, assignments, root):
                    inline_in_scope = True
                elif literal_path not in rejected_paths:
                    rejected_paths = rejected_paths + (literal_path,)
    opaque = bool(not paths and inline_in_scope)
    return WriteResolution(paths=paths, opaque=opaque, rejected_paths=rejected_paths)


def _candidate_edit_paths(
    tool_name: object, tool_input: object, tool_response: object
) -> tuple[str, ...]:
    if not isinstance(tool_name, str) or not isinstance(tool_input, Mapping):
        return ()
    if tool_name not in _SINGLE_PATH_EDIT_TOOLS and tool_name != _MULTI_PATH_EDIT_TOOL:
        return ()

    found: list[str] = []
    direct = tool_input.get("file_path")
    if isinstance(direct, str) and direct:
        found.append(direct)
    elif isinstance(tool_response, Mapping):
        # Captured PostToolUse shape. It is a fallback: input + response describe one operation,
        # not two writes.
        response_path = tool_response.get("filePath")
        if isinstance(response_path, str) and response_path:
            found.append(response_path)

    if tool_name == _MULTI_PATH_EDIT_TOOL:
        # Defensive compatibility from the snapshot. R04 records that a nested file_path has not
        # been observed in a real MultiEdit payload; the top-level path above is authoritative.
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if not isinstance(edit, Mapping):
                    continue
                path = edit.get("file_path")
                if isinstance(path, str) and path:
                    found.append(path)

    return _unique_in_order(list(enumerate(found)))


def _matches_scope(path: str, scope: str) -> bool:
    if scope == "any":
        return True
    if scope == "go":
        return path.endswith(".go")
    if scope == "source":
        return any(path.endswith(f".{extension}") for extension in SOURCE_EXTENSIONS)
    raise ValueError(f"unknown write scope {scope!r}")


def _resolve(
    scope: str,
    tool_name: object,
    tool_input: object,
    *,
    tool_response: object = None,
    root: str | None = None,
) -> WriteResolution:
    effective_root = root if isinstance(root, str) and root else project_root()
    if tool_name == _BASH_TOOL:
        if not isinstance(tool_input, Mapping):
            return WriteResolution()
        return _bash_resolution(tool_input.get("command"), scope, effective_root)

    paths: list[str] = []
    rejected: list[str] = []
    for path in _candidate_edit_paths(tool_name, tool_input, tool_response):
        if not _matches_scope(path, scope):
            continue
        # No shell variables reach an Edit/Write/MultiEdit `file_path`, so there is nothing for
        # `_parse_assignments` to resolve — an empty assignment map is correct, not a shortcut.
        if _in_project_root(path, {}, effective_root):
            paths.append(path)
        elif path not in rejected:
            rejected.append(path)
    return WriteResolution(paths=tuple(paths), rejected_paths=tuple(rejected))


def resolve_any_file_writes(
    tool_name: object, tool_input: object, *, tool_response: object = None,
    root: str | None = None,
) -> WriteResolution:
    """Resolve writes to files of any type for one tool call."""
    return _resolve("any", tool_name, tool_input, tool_response=tool_response, root=root)


def resolve_source_file_writes(
    tool_name: object, tool_input: object, *, tool_response: object = None,
    root: str | None = None,
) -> WriteResolution:
    """Resolve writes to the captured source-extension set for one tool call.

    ``root`` scopes the answer to writes INSIDE the project root (see the module docstring's
    IN-PROJECT-ROOT SCOPING section). It defaults to ``project_root()``'s own fallback
    (``CLAUDE_PROJECT_DIR`` or the process cwd) when the caller has no event to resolve one from;
    an evaluator that has an event should resolve ``project_root(event)`` once and pass it here.
    """
    return _resolve("source", tool_name, tool_input, tool_response=tool_response, root=root)


def resolve_go_file_writes(
    tool_name: object, tool_input: object, *, tool_response: object = None,
    root: str | None = None,
) -> WriteResolution:
    """Resolve writes to case-sensitive ``.go`` targets for one tool call.

    See ``resolve_source_file_writes`` on ``root``.
    """
    return _resolve("go", tool_name, tool_input, tool_response=tool_response, root=root)
