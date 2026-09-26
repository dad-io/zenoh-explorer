"""Persistent, bounded supervisor for one host-local Codex App Server process.

This module owns process lifecycle and JSONL request routing only.  It intentionally does not
claim that a Codex version, hook surface, or multiplexed workflow has been qualified.  Provider
adapters remain responsible for validating method-specific request and response bodies.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import stat
import subprocess
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO

from bearhug.processes import observe_spawn


class AppServerSupervisorError(RuntimeError):
    """Base error for supervisor lifecycle, authority, and protocol failures."""


class AppServerUnavailableError(AppServerSupervisorError):
    """The process lease is stale, stopped, failed, or disconnected."""


class AppServerExecutableIdentityChangedError(AppServerUnavailableError):
    """A captured launch authority's pinned executable no longer matches its own stat identity
    or content digest.

    Raised only by ``AppServerLaunchAuthority.verify()``'s own dev/inode/size/mtime-or-content
    check, never by its symlink, non-canonical-path, cwd or unreadable-file checks (those stay
    the plain base class). The distinction matters to a caller re-checking a fresh full-file
    rehash against the pin: a rehash that still matches after THIS specific failure is metadata
    drift (a ``utimes`` or a relink), not a broken launch invariant; the same rehash after any of
    ``verify()``'s other failures would not mean that, so a caller must not lump them together.
    """


class AppServerBackpressureError(AppServerSupervisorError):
    """A bounded per-thread queue cannot accept more work."""


class AppServerProtocolError(AppServerSupervisorError):
    """The server stream or a correlated response is malformed."""


class AppServerRequestTimeout(AppServerSupervisorError):
    """A queued or dispatched request exceeded its caller-owned deadline."""


class SupervisorState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class AppServerLaunchAuthority:
    """Captured content and inode authority for one exact no-shell launch."""

    executable: str
    argv: tuple[str, ...]
    cwd: str
    executable_sha256: str
    executable_device: int
    executable_inode: int
    executable_size: int
    executable_mtime_ns: int

    @classmethod
    def capture(
        cls,
        executable: Path | str,
        *,
        argv: tuple[str, ...],
        cwd: Path | str,
    ) -> AppServerLaunchAuthority:
        executable_path = Path(executable)
        cwd_path = Path(cwd)
        if not executable_path.is_absolute() or not cwd_path.is_absolute():
            raise ValueError("executable and cwd must be absolute")
        if not argv or argv[0] != str(executable_path):
            raise ValueError("argv[0] must be the exact authorized executable path")
        if any(not isinstance(value, str) or not value or "\x00" in value for value in argv):
            raise ValueError("argv must contain only non-empty NUL-free strings")
        if executable_path.is_symlink() or executable_path.resolve(strict=True) != executable_path:
            raise ValueError("executable path must be canonical and must not be a symlink")
        if cwd_path.is_symlink() or cwd_path.resolve(strict=True) != cwd_path:
            raise ValueError("cwd must be a canonical directory and must not be a symlink")
        info = executable_path.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(executable_path, os.X_OK):
            raise ValueError("executable must be an executable regular file")
        if not cwd_path.is_dir():
            raise ValueError("cwd must be a directory")
        return cls(
            executable=str(executable_path),
            argv=argv,
            cwd=str(cwd_path),
            executable_sha256=_sha256_file(executable_path),
            executable_device=info.st_dev,
            executable_inode=info.st_ino,
            executable_size=info.st_size,
            executable_mtime_ns=info.st_mtime_ns,
        )

    def verify(self) -> None:
        """Re-read every captured executable fact immediately before launch."""

        path = Path(self.executable)
        if path.is_symlink():
            raise AppServerUnavailableError("authorized executable became a symlink")
        try:
            if path.resolve(strict=True) != path:
                raise AppServerUnavailableError("authorized executable path is no longer canonical")
            info = path.stat()
        except OSError as exc:
            raise AppServerUnavailableError(f"cannot verify authorized executable: {exc}") from exc
        observed = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        expected = (
            self.executable_device,
            self.executable_inode,
            self.executable_size,
            self.executable_mtime_ns,
        )
        if observed != expected or _sha256_file(path) != self.executable_sha256:
            raise AppServerExecutableIdentityChangedError("authorized executable identity changed")
        cwd = Path(self.cwd)
        try:
            if cwd.is_symlink() or cwd.resolve(strict=True) != cwd or not cwd.is_dir():
                raise AppServerUnavailableError("authorized cwd changed or is unavailable")
        except OSError as exc:
            raise AppServerUnavailableError(f"cannot verify authorized cwd: {exc}") from exc


@dataclass(frozen=True, slots=True)
class AppServerHealth:
    state: SupervisorState
    lease_id: str | None
    pid: int | None
    generation: int
    started_monotonic_ns: int | None
    last_message_monotonic_ns: int | None
    route_count: int
    queued_request_count: int
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class AppServerThreadRoute:
    route_id: str
    provider_thread_id: str
    lease_id: str


@dataclass(slots=True)
class _PendingResponse:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: BaseException | None = None


@dataclass(slots=True)
class _RouteWork:
    method: str
    params: dict[str, Any]
    deadline: float
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: BaseException | None = None
    cancelled: bool = False


@dataclass(slots=True)
class _RouteState:
    route: AppServerThreadRoute
    requests: queue.Queue[_RouteWork | None]
    notifications: queue.Queue[dict[str, Any]]
    worker: threading.Thread | None = None


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


class AppServerSupervisor:
    """Own one App Server subprocess and bounded serialized per-thread routes."""

    def __init__(
        self,
        authority: AppServerLaunchAuthority,
        *,
        route_queue_capacity: int = 8,
        notification_capacity: int = 128,
        max_message_bytes: int = 4 * 1024 * 1024,
        startup_timeout_s: float = 10.0,
        stop_timeout_s: float = 5.0,
    ) -> None:
        if route_queue_capacity < 1 or notification_capacity < 1 or max_message_bytes < 1024:
            raise ValueError(
                "queue capacities must be positive and max_message_bytes at least 1024"
            )
        if startup_timeout_s <= 0 or stop_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        self._authority = authority
        self._route_queue_capacity = route_queue_capacity
        self._notification_capacity = notification_capacity
        self._max_message_bytes = max_message_bytes
        self._startup_timeout_s = startup_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._state = SupervisorState.STOPPED
        self._lease_id: str | None = None
        self._generation = 0
        self._started_ns: int | None = None
        self._last_message_ns: int | None = None
        self._failure_reason: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._pending: dict[str, _PendingResponse] = {}
        self._routes: dict[str, _RouteState] = {}
        self._thread_to_route: dict[str, str] = {}
        self._opening_thread = False
        self._unrouted_notifications: list[tuple[str, dict[str, Any]]] = []
        self._request_sequence = 0

    def health(self) -> AppServerHealth:
        with self._lock:
            if (
                self._state in {SupervisorState.STARTING, SupervisorState.RUNNING}
                and self._process is not None
                and self._process.poll() is not None
            ):
                self._fail(f"App Server exited with status {self._process.returncode}")
            return AppServerHealth(
                state=self._state,
                lease_id=self._lease_id,
                pid=self._process.pid if self._process is not None else None,
                generation=self._generation,
                started_monotonic_ns=self._started_ns,
                last_message_monotonic_ns=self._last_message_ns,
                route_count=len(self._routes),
                queued_request_count=sum(route.requests.qsize() for route in self._routes.values()),
                failure_reason=self._failure_reason,
            )

    def start(self) -> AppServerHealth:
        with self._lock:
            if self._state is not SupervisorState.STOPPED:
                raise AppServerSupervisorError(
                    f"start requires stopped state, observed {self._state.value}"
                )
            self._authority.verify()
            self._state = SupervisorState.STARTING
            self._generation += 1
            self._lease_id = uuid.uuid4().hex
            self._started_ns = time.monotonic_ns()
            self._last_message_ns = None
            self._failure_reason = None
            try:
                # No `start_new_session=True` here (unlike `run_owned` and the Codex app-server
                # client's own launch), so `observe_spawn`'s recorded pgid is this CONTROLLER's
                # own group, not a group this child alone occupies -- correct only because
                # `AppServerSupervisor` is constructed nowhere in `src/` today (grep confirms
                # it; only tests build one). Add `start_new_session=True` before this class is
                # ever wired into `CapsuleRuntime._invoke`, or a controller launched inside a
                # shared job group would make `hil_required` recovery refuse permanently.
                process = subprocess.Popen(  # noqa: S603 - exact argv; shell is never used
                    self._authority.argv,
                    cwd=self._authority.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    shell=False,
                )
                observe_spawn(process)
            except OSError as exc:
                self._state = SupervisorState.FAILED
                self._failure_reason = f"launch failed: {exc}"
                raise AppServerUnavailableError(self._failure_reason) from exc
            if process.stdin is None or process.stdout is None:
                process.kill()
                process.wait()
                self._state = SupervisorState.FAILED
                self._failure_reason = "App Server did not expose stdio pipes"
                raise AppServerUnavailableError(self._failure_reason)
            self._process = process
            self._reader = threading.Thread(
                target=self._reader_main,
                args=(process.stdout,),
                name=f"bearhug-app-server-reader-{self._generation}",
                daemon=True,
            )
            self._reader.start()

        try:
            response = self._rpc(
                "initialize",
                {
                    "clientInfo": {
                        "name": "bear_hug",
                        "title": "Bear Hug",
                        "version": "app-server-supervisor.v1",
                    }
                },
                timeout_s=self._startup_timeout_s,
                allowed_states=(SupervisorState.STARTING,),
            )
            if not isinstance(response.get("result"), dict) or "error" in response:
                raise AppServerProtocolError("initialize did not return a result object")
            self._send_notification("initialized", {}, allowed_states=(SupervisorState.STARTING,))
            with self._lock:
                if self._state is not SupervisorState.STARTING:
                    raise AppServerUnavailableError(
                        self._failure_reason or "startup was interrupted"
                    )
                self._state = SupervisorState.RUNNING
            return self.health()
        except BaseException:
            self._fail("startup handshake failed")
            self.stop()
            raise

    def stop(self) -> AppServerHealth:
        with self._lock:
            if self._state is SupervisorState.STOPPED:
                return self.health()
            self._state = SupervisorState.STOPPING
            unavailable = AppServerUnavailableError("App Server supervisor stopped")
            self._fail_waiters_locked(unavailable)
            routes = tuple(self._routes.values())
            process = self._process
            self._routes.clear()
            self._thread_to_route.clear()
            self._opening_thread = False
            self._unrouted_notifications.clear()
        for route in routes:
            self._fail_route(route, unavailable)
        if process is not None:
            if process.stdin is not None:
                with suppress(OSError):
                    process.stdin.close()
            try:
                process.wait(timeout=self._stop_timeout_s)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=min(2.0, self._stop_timeout_s))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=min(2.0, self._stop_timeout_s))
        for route in routes:
            if route.worker is not None:
                route.worker.join(timeout=self._stop_timeout_s)
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=self._stop_timeout_s)
        with self._lock:
            self._process = None
            self._reader = None
            self._state = SupervisorState.STOPPED
            self._lease_id = None
            self._started_ns = None
            return self.health()

    def restart(self) -> AppServerHealth:
        self.stop()
        return self.start()

    def open_thread(
        self,
        route_id: str,
        thread_start_params: dict[str, Any],
        *,
        lease_id: str,
        timeout_s: float = 30.0,
    ) -> AppServerThreadRoute:
        if not route_id or not isinstance(route_id, str):
            raise ValueError("route_id must be a non-empty string")
        if not isinstance(thread_start_params, dict):
            raise TypeError("thread_start_params must be an object")
        self._require_lease(lease_id)
        with self._control_lock:
            self._require_lease(lease_id)
            with self._lock:
                if route_id in self._routes:
                    raise ValueError(f"route {route_id!r} already exists")
                self._opening_thread = True
            try:
                response = self._rpc("thread/start", thread_start_params, timeout_s=timeout_s)
            except BaseException:
                with self._lock:
                    self._opening_thread = False
                    self._unrouted_notifications.clear()
                raise
            result = response.get("result")
            thread = result.get("thread") if isinstance(result, dict) else None
            provider_thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(provider_thread_id, str) or not provider_thread_id:
                self._fail("thread/start returned no provider thread id")
                raise AppServerProtocolError("thread/start returned no provider thread id")
            with self._lock:
                self._require_lease_locked(lease_id)
                if provider_thread_id in self._thread_to_route:
                    self._opening_thread = False
                    self._unrouted_notifications.clear()
                    self._fail("provider thread id is already routed")
                    raise AppServerProtocolError("provider thread id is already routed")
                route = AppServerThreadRoute(route_id, provider_thread_id, lease_id)
                state = _RouteState(
                    route=route,
                    requests=queue.Queue(maxsize=self._route_queue_capacity),
                    notifications=queue.Queue(maxsize=self._notification_capacity),
                )
                state.worker = threading.Thread(
                    target=self._route_worker,
                    args=(state,),
                    name=f"bearhug-app-server-route-{route_id}",
                    daemon=True,
                )
                self._routes[route_id] = state
                self._thread_to_route[provider_thread_id] = route_id
                orphaned = tuple(self._unrouted_notifications)
                self._unrouted_notifications.clear()
                self._opening_thread = False
                if any(thread_id != provider_thread_id for thread_id, _ in orphaned):
                    self._fail("thread/start observed notification for an unrelated thread")
                    raise AppServerProtocolError(
                        "thread/start observed notification for an unrelated thread"
                    )
                for _, message in orphaned:
                    try:
                        state.notifications.put_nowait(message)
                    except queue.Full:
                        self._fail(f"notification queue overflow for route {route_id!r}")
                        raise AppServerProtocolError(
                            f"notification queue overflow for route {route_id!r}"
                        ) from None
                state.worker.start()
                return route

    def request(
        self,
        route_id: str,
        method: str,
        params: dict[str, Any],
        *,
        lease_id: str,
        timeout_s: float = 30.0,
        enqueue_timeout_s: float = 0.0,
    ) -> dict[str, Any]:
        if not method or not isinstance(method, str):
            raise ValueError("method must be a non-empty string")
        if not isinstance(params, dict):
            raise TypeError("params must be an object")
        if timeout_s <= 0 or enqueue_timeout_s < 0:
            raise ValueError("timeout_s must be positive and enqueue_timeout_s non-negative")
        with self._lock:
            self._require_lease_locked(lease_id)
            route = self._routes.get(route_id)
            if route is None:
                raise AppServerUnavailableError(f"unknown route {route_id!r}")
            supplied_thread_id = params.get("threadId")
            if supplied_thread_id not in (None, route.route.provider_thread_id):
                raise AppServerProtocolError("request threadId does not match its route")
            routed_params = dict(params)
            routed_params["threadId"] = route.route.provider_thread_id
        deadline = time.monotonic() + timeout_s
        work = _RouteWork(method=method, params=routed_params, deadline=deadline)
        try:
            route.requests.put(work, block=enqueue_timeout_s > 0, timeout=enqueue_timeout_s)
        except queue.Full as exc:
            raise AppServerBackpressureError(f"route {route_id!r} request queue is full") from exc
        if not work.event.wait(max(0.0, deadline - time.monotonic())):
            work.cancelled = True
            raise AppServerRequestTimeout(f"route {route_id!r} request deadline expired")
        if work.error is not None:
            raise work.error
        if work.response is None:
            raise AppServerProtocolError("route worker completed without a response")
        return work.response

    def poll_notifications(
        self,
        route_id: str,
        *,
        lease_id: str,
        limit: int = 1,
    ) -> tuple[dict[str, Any], ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            self._require_lease_locked(lease_id)
            route = self._routes.get(route_id)
            if route is None:
                raise AppServerUnavailableError(f"unknown route {route_id!r}")
        messages: list[dict[str, Any]] = []
        for _ in range(limit):
            try:
                messages.append(route.notifications.get_nowait())
            except queue.Empty:
                break
        return tuple(messages)

    def _require_lease(self, lease_id: str) -> None:
        with self._lock:
            self._require_lease_locked(lease_id)

    def _require_lease_locked(self, lease_id: str) -> None:
        if self._state is not SupervisorState.RUNNING:
            raise AppServerUnavailableError(
                f"App Server is unavailable in {self._state.value} state"
            )
        if not lease_id or lease_id != self._lease_id:
            raise AppServerUnavailableError("App Server lease is stale or does not match")

    def _next_request_id_locked(self) -> str:
        self._request_sequence += 1
        return f"bearhug-supervisor:{self._lease_id}:{self._request_sequence}"

    def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_s: float,
        allowed_states: tuple[SupervisorState, ...] = (SupervisorState.RUNNING,),
    ) -> dict[str, Any]:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        with self._lock:
            if self._state not in allowed_states:
                raise AppServerUnavailableError(
                    f"App Server is unavailable in {self._state.value} state"
                )
            request_id = self._next_request_id_locked()
            pending = _PendingResponse()
            self._pending[request_id] = pending
        raw = (
            json.dumps(
                {"id": request_id, "method": method, "params": params},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if len(raw) > self._max_message_bytes:
            with self._lock:
                self._pending.pop(request_id, None)
            raise AppServerProtocolError("outbound App Server request exceeds message bound")
        try:
            self._write(raw, allowed_states=allowed_states)
        except BaseException:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        if not pending.event.wait(timeout_s):
            with self._lock:
                self._pending.pop(request_id, None)
            reason = f"request {method!r} timed out after dispatch"
            self._fail(reason)
            raise AppServerRequestTimeout(reason)
        if pending.error is not None:
            raise pending.error
        response = pending.response
        if response is None:
            raise AppServerProtocolError("correlated request completed without a response")
        if "error" in response:
            raise AppServerProtocolError(f"request {method!r} failed: {response['error']!r}")
        return response

    def _send_notification(
        self,
        method: str,
        params: dict[str, Any],
        *,
        allowed_states: tuple[SupervisorState, ...],
    ) -> None:
        raw = (
            json.dumps(
                {"method": method, "params": params}, sort_keys=True, separators=(",", ":")
            ).encode()
            + b"\n"
        )
        if len(raw) > self._max_message_bytes:
            raise AppServerProtocolError("outbound App Server notification exceeds message bound")
        self._write(raw, allowed_states=allowed_states)

    def _write(self, raw: bytes, *, allowed_states: tuple[SupervisorState, ...]) -> None:
        with self._write_lock:
            with self._lock:
                if self._state not in allowed_states:
                    raise AppServerUnavailableError(
                        f"App Server is unavailable in {self._state.value} state"
                    )
                process = self._process
                stream = process.stdin if process is not None else None
            if stream is None:
                raise AppServerUnavailableError("App Server stdin is unavailable")
            try:
                stream.write(raw)
                stream.flush()
            except OSError as exc:
                self._fail(f"App Server write failed: {exc}")
                raise AppServerUnavailableError("App Server disconnected during write") from exc

    def _reader_main(self, stream: BinaryIO) -> None:
        try:
            while True:
                line = stream.readline(self._max_message_bytes + 1)
                if not line:
                    break
                if len(line) > self._max_message_bytes or not line.endswith(b"\n"):
                    self._fail("App Server message exceeds bound or is not newline terminated")
                    return
                try:
                    message = json.loads(line, object_pairs_hook=_closed_object)
                except (UnicodeDecodeError, ValueError) as exc:
                    self._fail(f"App Server emitted invalid JSONL: {exc}")
                    return
                if not isinstance(message, dict):
                    self._fail("App Server emitted a non-object JSONL message")
                    return
                with self._lock:
                    self._last_message_ns = time.monotonic_ns()
                request_id = message.get("id")
                method = message.get("method")
                if request_id is not None and isinstance(method, str):
                    self._fail(f"unhandled interactive server request {method!r}")
                    return
                if request_id is not None:
                    key = str(request_id)
                    with self._lock:
                        pending = self._pending.pop(key, None)
                    if pending is None:
                        self._fail(f"response has unknown request id {request_id!r}")
                        return
                    pending.response = message
                    pending.event.set()
                    continue
                if not isinstance(method, str) or not method:
                    self._fail("notification has no method")
                    return
                params = message.get("params")
                thread_id = params.get("threadId") if isinstance(params, dict) else None
                if thread_id is None:
                    continue
                with self._lock:
                    route_id = self._thread_to_route.get(thread_id)
                    route = self._routes.get(route_id) if route_id is not None else None
                if route is None:
                    with self._lock:
                        if self._opening_thread:
                            if len(self._unrouted_notifications) >= self._notification_capacity:
                                self._fail("unrouted notification queue overflow")
                                return
                            self._unrouted_notifications.append((thread_id, message))
                            continue
                    self._fail(f"notification names unknown provider thread {thread_id!r}")
                    return
                try:
                    route.notifications.put_nowait(message)
                except queue.Full:
                    self._fail(f"notification queue overflow for route {route.route.route_id!r}")
                    return
        finally:
            with self._lock:
                should_fail = self._state not in {
                    SupervisorState.STOPPING,
                    SupervisorState.STOPPED,
                    SupervisorState.FAILED,
                }
            if should_fail:
                self._fail("App Server stdout disconnected")

    def _route_worker(self, route: _RouteState) -> None:
        while True:
            work = route.requests.get()
            if work is None:
                return
            if work.cancelled or time.monotonic() >= work.deadline:
                work.error = AppServerRequestTimeout("request expired before dispatch")
                work.event.set()
                continue
            try:
                work.response = self._rpc(
                    work.method,
                    work.params,
                    timeout_s=max(0.001, work.deadline - time.monotonic()),
                )
            except BaseException as exc:
                work.error = exc
            finally:
                work.event.set()

    def _fail(self, reason: str) -> None:
        with self._lock:
            if self._state in {
                SupervisorState.STOPPING,
                SupervisorState.STOPPED,
                SupervisorState.FAILED,
            }:
                return
            self._state = SupervisorState.FAILED
            self._failure_reason = reason
            error = AppServerUnavailableError(reason)
            self._fail_waiters_locked(error)
            routes = tuple(self._routes.values())
        for route in routes:
            self._fail_route(route, error)

    def _fail_waiters_locked(self, error: BaseException) -> None:
        pending = tuple(self._pending.values())
        self._pending.clear()
        for waiter in pending:
            waiter.error = error
            waiter.event.set()

    @staticmethod
    def _fail_route(route: _RouteState, error: BaseException) -> None:
        while True:
            try:
                work = route.requests.get_nowait()
            except queue.Empty:
                break
            if work is not None:
                work.error = error
                work.event.set()
        with suppress(queue.Full):
            route.requests.put_nowait(None)
