"""Owned subprocess lifetimes and minimal provider launch environments."""

from __future__ import annotations

import contextvars
import os
import signal
import subprocess
from collections.abc import Callable
from contextlib import contextmanager, suppress


def terminate_group(process: subprocess.Popen) -> None:
    """Kill the owned process group even after its leader exits, then reap the leader."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=2)


# An optional, provider-agnostic hook that learns about every provider child the moment it is
# spawned, regardless of which of the three call sites (`run_owned` here, or the Codex Popen
# calls in `providers/app_server_client.py`/`providers/app_server_supervisor.py`) launched it.
# A `ContextVar` (rather than a module-level global) keeps the observer scoped to exactly the
# call that set it, and -- unlike a global -- makes "no observer set" the correct default for
# every caller that never touches it, including concurrent ones. With nothing set, `observe_spawn`
# is a no-op and every caller's behaviour is unchanged.
_spawn_observer: contextvars.ContextVar[Callable[[int, int], None] | None] = contextvars.ContextVar(
    "bearhug_spawn_observer", default=None
)


@contextmanager
def observing_spawns(observer: Callable[[int, int], None] | None):
    """Set the active spawn observer for the duration of the ``with`` block.

    The observer is called with ``(pid, pgid)`` for every process ``observe_spawn`` sees while
    it is active. Nested use restores the previous observer (possibly ``None``) on exit, so a
    caller downstream of one active scope may not know it is inside another.
    """

    token = _spawn_observer.set(observer)
    try:
        yield
    finally:
        _spawn_observer.reset(token)


def observe_spawn(process: subprocess.Popen) -> None:
    """Tell the active spawn observer, if any, that ``process`` was just started.

    Called immediately after every ``Popen`` that can launch a provider child: here, when
    ``run_owned`` is called with ``observe=True`` (only the Claude provider launch passes it),
    and at the two provider launch sites in ``providers/app_server_client.py`` and
    ``providers/app_server_supervisor.py``, which call it unconditionally since both are
    reached only for a provider process. With no observer set -- the default, and the only
    behaviour for every caller outside campaign execution, such as ``host_git.py`` and
    ``campaign/integration.py`` -- this does nothing.

    If the observer itself raises, the spawned process is killed and reaped before the
    exception propagates: a child that could not be durably recorded must never be left
    running. This makes the check at this exact point, not somewhere later that could be
    skipped by an early return or a caller that forgets to clean up.
    """

    observer = _spawn_observer.get()
    if observer is None:
        return
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        pgid = process.pid
    try:
        observer(process.pid, pgid)
    except BaseException:
        if pgid == process.pid:
            # `start_new_session=True` made this process its own session and process group
            # leader (`run_owned`'s and the Codex client's own Popen both set it): the whole
            # group is this process and only its own descendants, so killing it cannot reach
            # anything else.
            terminate_group(process)
        else:
            # This call site does not isolate its child into a new session (the app-server
            # supervisor's own Popen), so its process group is inherited and may be shared
            # with this very caller. A group-wide signal here could kill unrelated
            # processes, including this one; kill only the one process this call spawned.
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(Exception):
                process.wait(timeout=2)
        raise


def run_owned(
    args,
    *,
    cwd=None,
    env=None,
    input=None,
    stdout=None,
    stderr=None,
    capture_output=False,
    check=False,
    timeout=None,
    observe=False,
):
    """A subprocess.run-compatible bounded subset; clean the owned group before return.

    ``observe`` opts this one call into the active spawn observer (see ``observe_spawn``); the
    default keeps every caller silent even while an observer is active. `run_owned` is shared by
    the Claude provider launch and by short-lived helpers (`host_git.py`'s git calls,
    `campaign/integration.py`'s build/test/lint commands) that run during a provider turn --
    only the provider launch itself passes ``observe=True``, so a completed episode's dozens of
    git helper calls are never durably recorded, only the provider process is.
    """
    if capture_output:
        if stdout is not None or stderr is not None:
            raise ValueError("capture_output cannot be combined with stdout/stderr")
        stdout = stderr = subprocess.PIPE
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    if observe:
        observe_spawn(process)
    try:
        try:
            out, err = process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_group(process)
            out, err = process.communicate(timeout=2)
            raise subprocess.TimeoutExpired(args, timeout, output=out, stderr=err) from exc
    finally:
        terminate_group(process)
    result = subprocess.CompletedProcess(args, process.returncode, out, err)
    if check:
        result.check_returncode()
    return result


def provider_environment(provider: str) -> dict[str, str]:
    """Forward runtime plumbing and only the selected provider's authentication variables."""
    allowed = {
        "HOME",
        # Claude Code resolves its Keychain login by account; without USER/LOGNAME a child
        # `claude --print` reports "Not logged in" even though the operator is. Identity
        # plumbing, not a secret.
        "USER",
        "LOGNAME",
        "PATH",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    allowed.update(
        {
            "codex": {"CODEX_HOME", "OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"},
            "claude": {"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"},
        }[provider]
    )
    return {key: value for key, value in os.environ.items() if key in allowed}
