"""Fixed Linux service main for one independent managed run.

Only the derived run ID crosses argv. Request material stays in protected
assets and child process descriptors. Private injected boundaries support
hermetic tests without claiming a live systemd service.
"""

from __future__ import annotations

import hashlib
import os
import re
import select
import selectors
import signal
import socket
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from . import _helper_identity, _managed_job_request, _managed_job_wire
from ._managed_job_store import FactName, ManagedJobStore, Stream

if TYPE_CHECKING:
    from collections.abc import Callable

    from ._managed_job_store import CaptureWriter

_RUN = re.compile(r"[0-9a-f]{32}\Z")
_CGROUP_ROOT = "/sys/fs/cgroup"
_CGROUP_SELF = "/proc/self/cgroup"
_CLEANUP_SECONDS = 5.0
_STOP_GRACE_SECONDS = 2.0
_PRELAUNCH_REAP_SECONDS = 0.25
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SH_PATHS = frozenset({"/bin/sh", "/usr/bin/sh"})
_BASH_PATHS = frozenset({"/bin/bash", "/usr/bin/bash"})


class ControllerError(ValueError):
    """A fixed service prerequisite or observation failed."""


def _account_shell(uid: int) -> str:
    import pwd

    try:
        return pwd.getpwuid(uid).pw_shell
    except (KeyError, OSError):
        raise ControllerError("account shell unavailable") from None


def service_cgroup(data: bytes, run_id: str) -> str:
    """Accept the exact derived service's unified cgroup v2 membership."""
    if type(run_id) is not str or _RUN.fullmatch(run_id) is None:
        raise ControllerError("invalid run identity")
    lines = data.splitlines()
    if len(lines) != 1 or not lines[0].startswith(b"0::/"):
        raise ControllerError("invalid cgroup membership")
    try:
        path = lines[0][3:].decode("ascii")
    except UnicodeError:
        raise ControllerError("invalid cgroup membership") from None
    if path == "/" or any(part in ("", ".", "..") for part in path[1:].split("/")):
        raise ControllerError("invalid cgroup membership")
    if path.rsplit("/", 1)[-1] != f"agw-managed-{run_id}.service":
        raise ControllerError("wrong service cgroup")
    return path


class _Cgroup:
    def __init__(self, run_id: str) -> None:
        if not os.path.exists(f"{_CGROUP_ROOT}/cgroup.controllers"):
            raise ControllerError("unified cgroup unavailable")
        with open(_CGROUP_SELF, "rb") as stream:
            member = service_cgroup(stream.read(4096), run_id)
        parent = _CGROUP_ROOT + member
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK | os.X_OK):
            raise ControllerError("service cgroup is not delegated")
        self.path = parent + "/agw-workload-" + run_id
        created = False
        try:
            os.mkdir(self.path)
            created = True
            for name in ("cgroup.procs", "cgroup.events", "cgroup.kill"):
                if not os.path.isfile(self.path + "/" + name):
                    raise ControllerError("incomplete workload cgroup")
        except BaseException:
            if created:
                with suppress(OSError):
                    os.rmdir(self.path)
            raise

    def place(self, pid: int) -> None:
        with open(self.path + "/cgroup.procs", "wb", buffering=0) as stream:
            stream.write(f"{pid}\n".encode("ascii"))

    def kill(self) -> None:
        with open(self.path + "/cgroup.kill", "wb", buffering=0) as stream:
            stream.write(b"1")

    def empty(self) -> bool:
        with open(self.path + "/cgroup.events", "rb") as stream:
            lines = stream.read(4096).splitlines()
        states = [line for line in lines if line.startswith(b"populated ")]
        if len(states) != 1 or states[0] not in (b"populated 0", b"populated 1"):
            raise ControllerError("invalid cgroup events")
        return states[0] == b"populated 0"

    def close(self) -> None:
        with suppress(OSError):
            os.rmdir(self.path)


def sd_notify(address: str) -> None:
    if not address or "\0" in address:
        raise ControllerError("invalid notify socket")
    target = "\0" + address[1:] if address.startswith("@") else address
    if not target or target == "\0":
        raise ControllerError("invalid notify socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        if client.sendto(b"READY=1", target) != len(b"READY=1"):
            raise ControllerError("notify failed")


def _fact(run_id: str, digest: str, kind: str, **fields: object) -> bytes:
    return _managed_job_wire.encode_fact(
        {
            "version": 1,
            "kind": kind,
            "run_id": run_id,
            "unit": f"agw-managed-{run_id}.service",
            "receipt_sha256": digest,
            **fields,
        }
    )


def _close(fd: int) -> None:
    with suppress(OSError):
        os.close(fd)


def _gate(fd: int) -> None:
    if os.read(fd, 1) != b"1":
        raise ControllerError("gate closed")


def _child(
    request: _managed_job_request.ManagedJobRequest,
    launch: dict[str, object],
    placed: int,
    released: int,
    ready: int,
    stdin: int,
    stdout: int,
    stderr: int,
    exec_status: int,
    apply_identity: bool,
) -> None:
    try:
        _gate(placed)
        expected = _helper_identity.decode_identity(launch["workload"])
        if apply_identity:
            os.setgroups(list(expected.groups))
            os.setresgid(expected.egid, expected.egid, expected.egid)
            os.setresuid(expected.euid, expected.euid, expected.euid)
        if not _helper_identity.matches_current_identity(expected):
            raise ControllerError("identity mismatch")
        if request.cwd is not None:
            os.chdir(request.cwd)
        os.dup2(stdin, 0)
        os.dup2(stdout, 1)
        os.dup2(stderr, 2)
        source_fd = -1
        shell = cast("dict[str, object]", launch["shell"])
        if request.kind == "command":
            executable = request.argv[0]
            argv = list(request.argv)
            search_path = True
        else:
            search_path = False
            if shell["requested"] == "user_default":
                configured = _account_shell(expected.euid)
                if configured not in _SH_PATHS | _BASH_PATHS or configured != shell["resolved_executable"]:
                    raise ControllerError("resolved account shell mismatch")
            source_fd = os.memfd_create("agw-managed-source", 0)
            source = memoryview(request.source)
            while source:
                source = source[os.write(source_fd, source) :]
            os.lseek(source_fd, 0, os.SEEK_SET)
            executable = cast("str", shell["resolved_executable"])
            source_path = f"/proc/self/fd/{source_fd}"
            resolved = cast("str", shell["resolved_executable"])
            login = shell["login"]
            if resolved in _BASH_PATHS:
                argv = (
                    [executable, "--login", source_path]
                    if login
                    else [executable, "--noprofile", "--norc", source_path]
                )
            elif resolved in _SH_PATHS:
                name = os.path.basename(executable)
                argv = ["-" + name if login else executable, source_path]
            else:
                raise ControllerError("unsupported resolved shell")
        environment = dict(request.environment)
        os.write(ready, b"1")
        _gate(released)
        for inherited in signal.valid_signals() - {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(inherited, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, ())
        if search_path:
            os.execvpe(executable, argv, environment)
        else:
            os.execve(executable, argv, environment)
    except BaseException:
        with suppress(OSError):
            os.write(exec_status, b"F")
        os._exit(126)


@dataclass(slots=True)
class _StreamState:
    stream: Stream
    fd: int
    writer: CaptureWriter | None


def _reap_prelaunch(pid: int) -> None:
    """Give a killed setup child a short chance to reap without blocking service exit."""
    deadline = time.monotonic() + _PRELAUNCH_REAP_SECONDS
    while True:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == pid or time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def _serve(
    run_id: str,
    store: ManagedJobStore,
    boundary: _Cgroup,
    notify: Callable[[], None],
    apply_identity: bool,
) -> None:
    request = store.read_request()
    if request is None:
        raise ControllerError("missing request")
    launch = _managed_job_request.decode_request_launch(request.launch)
    if launch["run_id"] != run_id or launch["unit"] != f"agw-managed-{run_id}.service":
        raise ControllerError("request mismatch")
    if store.read_fact(FactName.LAUNCH) is not None:
        raise ControllerError("run already launched")
    digest = _managed_job_wire.launch_sha256(launch)
    placed_r, placed_w = os.pipe()
    release_r, release_w = os.pipe()
    ready_r, ready_w = os.pipe()
    input_r, input_w = os.pipe()
    output_r, output_w = os.pipe()
    error_r, error_w = os.pipe()
    exec_r, exec_w = os.pipe2(os.O_CLOEXEC)
    pid = os.fork()
    if pid == 0:
        for fd in (placed_w, release_w, ready_r, input_w, output_r, error_r, exec_r):
            _close(fd)
        _child(request, launch, placed_r, release_r, ready_w, input_r, output_w, error_w, exec_w, apply_identity)
        os._exit(126)
    for fd in (placed_r, release_r, ready_w, input_r, output_w, error_w, exec_w):
        _close(fd)
    launched = False
    writers: list[CaptureWriter] = []
    try:
        boundary.place(pid)
        os.write(placed_w, b"1")
        if not select.select([ready_r], [], [], 5.0)[0] or os.read(ready_r, 1) != b"1":
            raise ControllerError("child setup failed")
        states = []
        for stream, fd in ((Stream.STDOUT, output_r), (Stream.STDERR, error_r)):
            writer = (
                store.open_capture(stream, cast("int", request.capture_prefix_bytes))
                if request.output_mode == "capture"
                else None
            )
            if writer is not None:
                writers.append(writer)
            states.append(_StreamState(stream, fd, writer))
        store.publish_fact(FactName.LAUNCH, request.launch)
        notify()
        if store.read_stop_request():
            _close(release_w)
            release_w = -1
        else:
            os.write(release_w, b"1")
        launched = True
        _observe(run_id, digest, request, store, boundary, pid, input_w, exec_r, states)
    finally:
        for fd in (placed_w, release_w, ready_r, input_w, output_r, error_r, exec_r):
            _close(fd)
        for writer in writers:
            writer.close()
        if not launched:
            with suppress(OSError):
                boundary.kill()
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            _reap_prelaunch(pid)


def _observe(
    run_id: str,
    digest: str,
    request: _managed_job_request.ManagedJobRequest,
    store: ManagedJobStore,
    boundary: _Cgroup,
    pid: int,
    input_fd: int,
    exec_fd: int,
    states: list[_StreamState],
) -> None:
    selector = selectors.DefaultSelector()
    os.set_blocking(exec_fd, False)
    selector.register(exec_fd, selectors.EVENT_READ, "exec")
    for state in states:
        os.set_blocking(state.fd, False)
        selector.register(state.fd, selectors.EVENT_READ, state)
    offset = 0
    input_open = bool(request.stdin)
    if request.stdin:
        os.set_blocking(input_fd, False)
        selector.register(input_fd, selectors.EVENT_WRITE, None)
    else:
        _close(input_fd)
    status: int | None = None
    exec_result: bool | None = None
    exec_failed = False
    wait_published = False
    deadline: float | None = None
    stop_deadline: float | None = None
    boundary_done = False
    try:
        while True:
            if status is None:
                waited, candidate = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = candidate
                    if deadline is None:
                        deadline = time.monotonic() + _CLEANUP_SECONDS
                        with suppress(OSError):
                            boundary.kill()
            if stop_deadline is None and store.read_stop_request():
                if input_open:
                    if input_fd in selector.get_map():
                        selector.unregister(input_fd)
                    _close(input_fd)
                    input_open = False
                if status is None:
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGTERM)
                    stop_deadline = time.monotonic() + _STOP_GRACE_SECONDS
                else:
                    stop_deadline = time.monotonic()
            if status is None and stop_deadline is not None and deadline is None and time.monotonic() >= stop_deadline:
                with suppress(OSError):
                    boundary.kill()
                deadline = time.monotonic() + _CLEANUP_SECONDS
            if status is not None and exec_result is True and os.WIFEXITED(status) and not wait_published:
                store.publish_fact(
                    FactName.WAIT,
                    _fact(run_id, digest, "wait", exit_code=os.WEXITSTATUS(status), signal=None),
                )
                wait_published = True
            if status is not None and exec_result is not None and deadline is not None and not boundary_done:
                try:
                    empty = boundary.empty()
                except OSError:
                    empty = False
                if empty:
                    store.publish_fact(FactName.BOUNDARY_EMPTY, _fact(run_id, digest, "boundary-empty"))
                    boundary_done = True
            if status is not None and not selector.get_map() and boundary_done:
                return
            if deadline is not None and time.monotonic() >= deadline:
                return
            next_deadline = deadline if deadline is not None else stop_deadline
            timeout = 0.05 if next_deadline is None else min(0.05, max(0.0, next_deadline - time.monotonic()))
            for key, _ in selector.select(timeout):
                if key.data == "exec":
                    chunk = os.read(exec_fd, 4096)
                    if chunk:
                        exec_failed = True
                    else:
                        selector.unregister(exec_fd)
                        exec_result = not exec_failed
                elif key.data is None:
                    chunk = request.stdin[offset : offset + 65536]
                    try:
                        offset += os.write(input_fd, chunk)
                    except BrokenPipeError:
                        offset = len(request.stdin)
                    if offset == len(request.stdin):
                        selector.unregister(input_fd)
                        _close(input_fd)
                        input_open = False
                else:
                    state = cast("_StreamState", key.data)
                    chunk = os.read(state.fd, 65536)
                    if chunk:
                        if state.writer is not None:
                            state.writer.write(chunk)
                    else:
                        selector.unregister(state.fd)
                        _close(state.fd)
                        if state.writer is not None:
                            capture = state.writer.finish()
                            retained, digest_bytes = capture.length, capture.sha256
                            disposition = capture.disposition.value
                        else:
                            retained, digest_bytes = 0, _EMPTY_SHA256
                            disposition = "discarded" if request.output_mode == "discard" else "sensitivity-suppressed"
                        name = FactName.STDOUT_END if state.stream is Stream.STDOUT else FactName.STDERR_END
                        store.publish_fact(
                            name,
                            _fact(
                                run_id,
                                digest,
                                "stream-end",
                                stream=state.stream.value,
                                retained_bytes=retained,
                                retained_sha256=digest_bytes,
                                disposition=disposition,
                            ),
                        )
    finally:
        selector.close()


def run(
    run_id: str,
    *,
    _store: ManagedJobStore | None = None,
    _boundary: _Cgroup | None = None,
    _notify: Callable[[], None] | None = None,
    _identity_check: bool = True,
    _apply_identity: bool = True,
) -> int:
    """Run the fixed controller; private arguments are only for isolated tests."""
    if sys.platform != "linux" or type(run_id) is not str or _RUN.fullmatch(run_id) is None:
        return 125
    if _identity_check and os.getresuid() != (0, 0, 0):
        return 125
    owned_store = _store is None
    owned_boundary = _boundary is None
    try:
        store = _store if _store is not None else ManagedJobStore(run_id)
        boundary = _boundary if _boundary is not None else _Cgroup(run_id)
        notify = _notify if _notify is not None else lambda: sd_notify(os.environ["NOTIFY_SOCKET"])
        _serve(run_id, store, boundary, notify, _apply_identity)
        return 0
    except BaseException:
        return 125
    finally:
        if owned_boundary and _boundary is None and "boundary" in locals():
            boundary.close()
        if owned_store and _store is None and "store" in locals():
            store.close()


def main(run_id: str) -> int:
    return run(run_id)
