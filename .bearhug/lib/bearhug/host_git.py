"""Controller Git inspection without ambient executables, hooks, filters, or credentials.

``run_git`` always runs with ``HOME``, ``GIT_CONFIG_GLOBAL`` and the system config nulled. A
cleanliness verdict must not depend on ambient state a provider process could have changed --
with one declared exception, below.

That hardening has one cost. It also stops Git from finding the user's own global ignore file:
``core.excludesFile`` if the user set one, otherwise the default ``$XDG_CONFIG_HOME/git/ignore``
or ``$HOME/.config/git/ignore``. A project the user's own ``git status`` calls clean can then
show dirty here, for no reason a path can explain -- the one rule that excludes it lives outside
the repository, in a file this hardened environment cannot reach.

``_resolve_user_excludes_file`` closes exactly that gap, and is the one declared exception to the
first paragraph. Its ``git config --global --get core.excludesFile`` lookup is cached per
``HOME``/``XDG_CONFIG_HOME``/``GIT_CONFIG_GLOBAL`` triple, since a process's own identity rarely
changes; whether the resulting file currently exists on disk is checked fresh on every call, so
one created, removed, or reconfigured after the cache was first populated is still seen next time
anything asks. Hooks, fsmonitor, textconv and the network stay disabled for that lookup too,
exactly as for every other invocation here.

The result is fed back as an explicit ``-c core.excludesFile=<value>``, but a repository's own
choice must win first. ``run_git`` also asks ``cwd``'s own local, worktree and included
configuration whether it already sets ``core.excludesFile`` (folded into the same
filter/textconv inspection below, so this costs no separate process), and passes nothing when it
does, leaving that repository to resolve the setting unassisted. Otherwise the value is the
user's file, or an explicit empty override (``-c core.excludesFile=``, which turns Git's own
default off) when the user has none: never silence, which a caller's own ambient environment
could otherwise fill in with its own stale default. When the flag does carry a path, it affects
every command that consults ignore rules, not only a status or ignore verdict: ``git merge``, for
one, may then treat a globally-ignored untracked file as expendable and overwrite it instead of
refusing, and ``bearhug campaign integrate --integration-target`` merges into whatever worktree
the operator names this way, not only a leased, disposable one. A repository's own
``.git/info/exclude`` and its tracked ``.gitignore`` files come from the repository itself, not
from this resolver, and keep applying exactly as before.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from bearhug.processes import run_owned

_EXCLUDES_FILE_TIMEOUT_SECONDS = 5
_MAX_PATH_DISPLAY_LENGTH = 200
_MAX_MESSAGE_LENGTH = 2048
_TRANSFORM_PATTERN = r"^(filter\..*\.(clean|smudge|process)|diff\..*\.textconv|core\.excludesfile)$"


def git_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": os.devnull,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _hardened_prefix(executable: str) -> tuple[str, ...]:
    """Flags shared by every invocation this module makes: no hooks, fsmonitor, or network."""

    return (
        executable,
        "--no-optional-locks",
        "--no-pager",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "diff.external=",
        "-c",
        "protocol.allow=never",
    )


def _resolved_git_executable() -> str | None:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        return None
    return str(Path(executable).resolve(strict=True))


@functools.lru_cache(maxsize=256)
def _cached_excludes_candidate(
    home: str, xdg_config_home: str, git_config_global: str
) -> str | None:
    """The configured or default global-ignore candidate path for one exact HOME/
    XDG_CONFIG_HOME/GIT_CONFIG_GLOBAL triple, before checking whether it exists.

    Cached per triple: the ``git config`` lookup this performs is the part worth not repeating
    -- a real process's identity does not change between one verdict and the next, and a test
    exercising a second triple resolves again because the cache key itself changed. Whether the
    resulting path currently exists is deliberately NOT cached here; see
    ``_resolve_user_excludes_file``, which checks that fresh on every call, so a file created or
    removed after this process started, or after this triple was first resolved, is still seen.
    """

    try:
        executable = _resolved_git_executable()
    except OSError:
        executable = None
    value: str | None = None
    if executable is not None:
        environment = git_environment()
        environment["HOME"] = home or os.devnull
        if xdg_config_home:
            environment["XDG_CONFIG_HOME"] = xdg_config_home
        else:
            environment.pop("XDG_CONFIG_HOME", None)
        if git_config_global:
            environment["GIT_CONFIG_GLOBAL"] = git_config_global
        else:
            environment.pop("GIT_CONFIG_GLOBAL", None)
        try:
            completed = run_owned(
                (
                    *_hardened_prefix(executable),
                    "config",
                    "--global",
                    "--includes",
                    "--get",
                    "core.excludesFile",
                ),
                # Not the process's own working directory: an `includeIf gitdir:` condition in
                # the user's global config must not be able to match wherever this happens to
                # run from, and a deleted or hostile cwd must not be able to affect the lookup.
                cwd="/",
                env=environment,
                capture_output=True,
                timeout=_EXCLUDES_FILE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            try:
                decoded = completed.stdout.decode("utf-8").strip()
            except UnicodeDecodeError:
                decoded = None
            if decoded == "":
                # `excludesFile =` with no value is how a user turns the global ignore file
                # off. Git treats that as "no file", not as "not configured": unlike a missing
                # key, it must not fall through to the default XDG/HOME path below.
                return None
            value = decoded
    if value is None:
        base = xdg_config_home or (os.path.join(home, ".config") if home else None)
        value = os.path.join(base, "git", "ignore") if base else None
    if value is None:
        return None
    if value == "~" or value.startswith("~/"):
        expanded_home = home or os.path.expanduser("~")
        value = expanded_home + value[1:]
    if not os.path.isabs(value):
        return None
    return value


def _resolve_user_excludes_file() -> str:
    """The path Git would use for the user's global ignore rules right now, or ``""`` when none
    applies. Never ``None``: "no file" is a definite, injectable answer
    (``-c core.excludesFile=``), not the absence of one.
    """

    candidate = _cached_excludes_candidate(
        os.environ.get("HOME", ""),
        os.environ.get("XDG_CONFIG_HOME", ""),
        os.environ.get("GIT_CONFIG_GLOBAL", ""),
    )
    if candidate is None or not os.path.isfile(candidate):
        return ""
    return candidate


def _repository_defines_own_excludes_file(
    executable: str, cwd: Path, environment: Mapping[str, str], timeout: float
) -> bool:
    """Whether ``cwd``'s own local or worktree config, or anything it ``include``s, already
    sets ``core.excludesFile``. ``environment`` must already null the global and system config
    (every environment this module builds does), so only a value the repository itself controls
    can make this true.

    Used only by ``resolve_excludes_file_override`` for a caller that wants this question asked
    on its own, without ``run_git``'s filter/textconv refusal (``run_git`` itself folds this same
    question into that check instead; see ``run_git``). Fails closed on a timeout or ``OSError``:
    returns ``True`` (assume the repository has its own value, inject nothing) rather than
    ``False`` (which would let the user's file silently override a value the lookup merely
    failed to confirm). Overriding a repository's own choice is the riskier direction.
    """

    prefix = (*_hardened_prefix(executable), "-C", str(cwd))
    try:
        result = run_owned(
            (*prefix, "config", "--includes", "--get", "core.excludesFile"),
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode not in (0, 1):
        return True
    return result.returncode == 0


def resolve_excludes_file_override(
    cwd: Path,
    *,
    environment: Mapping[str, str] | None = None,
    timeout: float = _EXCLUDES_FILE_TIMEOUT_SECONDS,
) -> str | None:
    """The ``core.excludesFile`` value a caller inspecting ``cwd`` should pass as
    ``-c core.excludesFile=<value>``. Three answers, not two:

    - ``None``: ``cwd``'s own local/worktree/included configuration already sets
      ``core.excludesFile``. Inject nothing, so the repository's own choice resolves
      unassisted -- it must win over the user's default, never be silently overridden by it.
    - ``""`` (empty string): neither the repository nor the user has an applicable ignore file.
      Inject the empty value anyway, so a caller's own ambient environment (``harness_installer``
      keeps ``HOME`` but drops ``XDG_CONFIG_HOME``, for one) cannot silently fall back to
      whatever *its* default would otherwise be, which may differ from the user's real one.
    - Any other string: the user's global ignore file's absolute path. Inject it.

    A caller must use ``is None`` / ``is not None`` to tell the first case from the other two;
    the empty-string case is falsy but a real, injectable answer.

    ``environment`` lets a caller that keeps its own Git environment (``harness_installer``,
    for one, which preserves ``HOME`` but already nulls the global and system config) run the
    repository-value lookup under that same identity, instead of this module's own hardened
    ``git_environment()``. Either way, the caller is responsible for that environment nulling
    global and system config, or this cannot tell a repository's own value from the user's.
    """

    executable = _resolved_git_executable()
    if executable is None:
        return _resolve_user_excludes_file()
    if environment is None:
        environment = git_environment()
    if _repository_defines_own_excludes_file(executable, cwd, environment, timeout):
        return None
    return _resolve_user_excludes_file()


def _reject_untrusted_transforms(raw: bytes) -> bool:
    """Parse the folded filter/textconv/``core.excludesFile`` pre-check's ``--null`` output
    (``key\\nvalue`` records, NUL-separated, so a value containing a newline cannot be mistaken
    for a second key). Raises ``OSError`` if any filter or textconv key is present -- that
    authority is never trusted host code, regardless of ``core.excludesFile``. Returns whether
    ``core.excludesfile`` was among the repository's own keys.
    """

    has_excludes_file = False
    for record in raw.split(b"\0"):
        if not record:
            continue
        key = record.partition(b"\n")[0]
        if key == b"core.excludesfile":
            has_excludes_file = True
            continue
        raise OSError("repository-defined Git filters/text conversion are not trusted host code")
    return has_excludes_file


def run_git(cwd: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    executable = _resolved_git_executable()
    if executable is None:
        raise OSError("trusted system Git is unavailable")
    environment = git_environment()
    probe_prefix = (*_hardened_prefix(executable), "-C", str(cwd))
    # Status/diff can invoke clean filters or text conversion even when no mutation was asked
    # for. Inspect effective local includes without executing them, then reject that authority.
    # core.excludesFile rides along in the same lookup (no extra process): if the repository
    # already sets it, nothing should be injected below, and a failure here raises rather than
    # silently allowing an override, so this fails closed the same way the filter check always
    # has.
    configuration = run_owned(
        (*probe_prefix, "config", "--includes", "--null", "--get-regexp", _TRANSFORM_PATTERN),
        cwd=cwd,
        env=environment,
        capture_output=True,
        timeout=timeout,
    )
    if configuration.returncode not in {0, 1}:
        raise OSError("cannot inspect repository transformation configuration")
    repository_has_excludes_file = (
        _reject_untrusted_transforms(configuration.stdout) if configuration.stdout else False
    )
    excludes_file = None if repository_has_excludes_file else _resolve_user_excludes_file()
    prefix = (
        *_hardened_prefix(executable),
        *(("-c", f"core.excludesFile={excludes_file}") if excludes_file is not None else ()),
        "-C",
        str(cwd),
    )
    return run_owned(
        (*prefix, *args),
        cwd=cwd,
        env=environment,
        capture_output=True,
        timeout=timeout,
    )


def parse_status_entries(raw: bytes) -> tuple[tuple[str, str], ...]:
    """Strictly parse ``git status --porcelain=v1 -z --untracked-files=all`` bytes into
    ``(code, path)`` pairs, one per entry.

    The one parser behind both a count (``providers/receipt.py``'s ``capture_launch_repository``
    calls ``len()`` on the result) and a refusal message's entries, so the same bytes are walked
    once. Raises ``ValueError`` if ``raw`` is not exactly what that Git invocation produces:
    NUL-terminated, each record at least a two-character code, a space, and a path, and a rename
    or copy record's second NUL-separated field present. Real Git output for this exact
    invocation always parses; a caller building a refusal message from ``raw`` it already knows
    is non-empty should treat this raising as the rare exception, not the common case, and still
    refuse without it (see ``describe_dirty_status``).

    A rename or copy record's first field is the NEW path and its second is the OLD path. That
    is how ``-z`` mode orders them, the reverse of the "old -> new" arrow ``git status`` prints
    without ``-z``; this returns them joined as ``"old -> new"`` to match that familiar reading.
    """

    if not raw:
        return ()
    parts = raw.split(b"\0")
    if parts[-1] != b"":
        raise ValueError("Git porcelain status was not NUL-terminated")
    entries: list[tuple[str, str]] = []
    index = 0
    last = len(parts) - 1
    while index < last:
        record = parts[index]
        if len(record) < 4 or record[2:3] != b" ":
            raise ValueError(f"Git porcelain status record at position {index} is malformed")
        code = record[:2].decode("ascii", errors="replace")
        renamed = b"R" in record[:2] or b"C" in record[:2]
        if renamed:
            if index + 1 >= last:
                raise ValueError("Git rename or copy record is missing its second path")
            new_path = record[3:].decode("utf-8", errors="replace")
            old_path = parts[index + 1].decode("utf-8", errors="replace")
            entries.append((code, f"{old_path} -> {new_path}"))
            index += 2
            continue
        entries.append((code, record[3:].decode("utf-8", errors="replace")))
        index += 1
    return tuple(entries)


_C_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\a": "\\a",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
}


def _quote_path_for_display(path: str) -> str:
    """Render ``path`` the way Git's own porcelain output quotes an unsafe path: wrapped in
    double quotes with C-style escapes, exactly as ``core.quotePath``'s default behaviour does
    for a character it will not print raw. Left alone otherwise.

    Quotes whenever a character is not ``str.isprintable()`` (the C0 and C1 control ranges, and
    Unicode "format" characters such as U+202E, right-to-left override, which could otherwise
    reorder how the refusal itself displays), or is a backslash or double quote. A handful of
    named escapes (``\\\\``, ``"``, and the common single-byte control codes) match
    Git's own short forms; anything else is escaped byte by byte in the UTF-8 encoding Git
    itself would write, since a path is bytes to Git, not a sequence of code points, and a
    multi-byte character's octal escapes are its individual encoded bytes, not its code point.
    """

    if not any(ch in _C_ESCAPES or not ch.isprintable() for ch in path):
        return path
    escaped = []
    for ch in path:
        if ch in _C_ESCAPES:
            escaped.append(_C_ESCAPES[ch])
        elif not ch.isprintable():
            for byte in ch.encode("utf-8"):
                escaped.append(f"\\{byte:03o}")
        else:
            escaped.append(ch)
    return '"' + "".join(escaped) + '"'


def _capped(text: str, limit: int) -> str:
    """Cap ``text`` at ``limit`` UTF-8 bytes, not characters: a 4-byte character (an emoji) and
    a 1-byte one are the same "length" in ``len(text)`` but not in the byte-length sinks this
    feeds (a terminal-hook decision's ``reason``, capped at 4096 bytes).
    """

    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    budget = max(limit - 3, 0)
    # Decoding a byte slice that stops mid-character drops that incomplete trailing sequence
    # rather than raising or corrupting the last character kept.
    return encoded[:budget].decode("utf-8", errors="ignore") + "..."


def describe_dirty_entries(
    root: Path | str,
    entries: Sequence[tuple[str, str]],
    *,
    limit: int = 5,
) -> str:
    """One-line account of a dirty verdict: the repository, how many entries, and up to
    ``limit`` of them as Git's own two-character status code and a path -- never file contents.

    Each rendered path is quoted like Git quotes an unsafe one (``_quote_path_for_display``) and
    capped at ``_MAX_PATH_DISPLAY_LENGTH`` (200) UTF-8 bytes; the whole message is then capped at
    ``_MAX_MESSAGE_LENGTH`` (2048) UTF-8 bytes, well under the 4096-byte limits of the sinks this
    text reaches (a terminal-hook decision's ``reason``, the campaign cockpit's ``phase.reason``).
    """

    total = len(entries)
    noun = "entry" if total == 1 else "entries"
    shown = ", ".join(
        f"{code} {_capped(_quote_path_for_display(path), _MAX_PATH_DISPLAY_LENGTH)}"
        for code, path in entries[:limit]
    )
    message = f"{root} has {total} uncommitted or untracked {noun}: {shown}"
    return _capped(message, _MAX_MESSAGE_LENGTH)


def describe_dirty_status(root: Path | str, raw: bytes, *, limit: int = 5) -> str:
    """``describe_dirty_entries`` for a caller that only has the raw, already-known-non-empty
    ``git status --porcelain=v1 -z`` bytes, not yet-parsed entries. Never raises: real Git
    output for this exact invocation always parses (see ``parse_status_entries``); if it somehow
    does not, the entries are simply omitted from a result that still names the repository,
    rather than let a formatting failure mask the caller's own refusal.
    """

    try:
        entries = parse_status_entries(raw)
    except ValueError:
        return f"{root} has uncommitted or untracked entries that could not be listed"
    return describe_dirty_entries(root, entries, limit=limit)


__all__ = [
    "describe_dirty_entries",
    "describe_dirty_status",
    "git_environment",
    "parse_status_entries",
    "resolve_excludes_file_override",
    "run_git",
]
