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
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class ControllerError(ValueError):
    """A fixed service prerequisite or observation failed."""


def service_cgroup(data: bytes) -> str:
    """Accept exactly one unified cgroup v2 membership with safe components."""
    lines = data.splitlines()
    if len(lines) != 1 or not lines[0].startswith(b"0::/"):
        raise ControllerError("invalid cgroup membership")
    try:
        path = lines[0][3:].decode("ascii")
    except UnicodeError:
        raise ControllerError("invalid cgroup membership") from None
    if path == "/" or any(part in ("", ".", "..") for part in path[1:].split("/")):
        raise ControllerError("invalid cgroup membership")
    return path


class _Cgroup:
    def __init__(self, run_id: str) -> None:
        if not os.path.exists(f"{_CGROUP_ROOT}/cgroup.controllers"):
            raise ControllerError("unified cgroup unavailable")
        with open(_CGROUP_SELF, "rb") as stream:
            member = service_cgroup(stream.read(4096))
        parent = _CGROUP_ROOT + member
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK | os.X_OK):
            raise ControllerError("service cgroup is not delegated")
        self.path = parent + "/agw-workload-" + run_id
        os.mkdir(self.path)
        for name in ("cgroup.procs", "cgroup.events", "cgroup.kill"):
            if not os.path.isfile(self.path + "/" + name):
                raise ControllerError("incomplete workload cgroup")

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
        else:
            source_fd = os.memfd_create("agw-managed-source", 0)
            source = memoryview(request.source)
            while source:
                source = source[os.write(source_fd, source) :]
            os.lseek(source_fd, 0, os.SEEK_SET)
            executable = cast("str", shell["resolved_executable"])
            source_path = f"/proc/self/fd/{source_fd}"
            requested = shell["requested"]
            login = shell["login"]
            if requested == "bash":
                argv = (
                    [executable, "--login", source_path]
                    if login
                    else [executable, "--noprofile", "--norc", source_path]
                )
            else:
                name = os.path.basename(executable)
                argv = ["-" + name if login else executable, source_path]
        environment = dict(request.environment)
        os.write(ready, b"1")
        _gate(released)
        os.execve(executable, argv, environment)
    except BaseException:
        os._exit(126)


@dataclass(slots=True)
class _StreamState:
    stream: Stream
    fd: int
    writer: CaptureWriter | None
    retained: int = 0
    digest: str = _EMPTY_SHA256
    omitted: bool = False


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
    pid = os.fork()
    if pid == 0:
        for fd in (placed_w, release_w, ready_r, input_w, output_r, error_r):
            _close(fd)
        _child(request, launch, placed_r, release_r, ready_w, input_r, output_w, error_w, apply_identity)
        os._exit(126)
    for fd in (placed_r, release_r, ready_w, input_r, output_w, error_w):
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
        os.write(release_w, b"1")
        launched = True
        _observe(run_id, digest, request, store, boundary, pid, input_w, states)
    finally:
        for fd in (placed_w, release_w, ready_r, input_w, output_r, error_r):
            _close(fd)
        for writer in writers:
            writer.close()
        if not launched:
            with suppress(OSError):
                boundary.kill()
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with suppress(ChildProcessError):
                os.waitpid(pid, 0)


def _observe(
    run_id: str,
    digest: str,
    request: _managed_job_request.ManagedJobRequest,
    store: ManagedJobStore,
    boundary: _Cgroup,
    pid: int,
    input_fd: int,
    states: list[_StreamState],
) -> None:
    selector = selectors.DefaultSelector()
    for state in states:
        os.set_blocking(state.fd, False)
        selector.register(state.fd, selectors.EVENT_READ, state)
    offset = 0
    if request.stdin:
        os.set_blocking(input_fd, False)
        selector.register(input_fd, selectors.EVENT_WRITE, None)
    else:
        _close(input_fd)
    status: int | None = None
    deadline: float | None = None
    boundary_done = False
    try:
        while True:
            if status is None:
                waited, candidate = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = candidate
                    fields = (
                        {"exit_code": os.WEXITSTATUS(status), "signal": None}
                        if os.WIFEXITED(status)
                        else {"exit_code": None, "signal": os.WTERMSIG(status)}
                    )
                    store.publish_fact(FactName.WAIT, _fact(run_id, digest, "wait", **fields))
                    deadline = time.monotonic() + _CLEANUP_SECONDS
                    boundary.kill()
            if status is not None and not boundary_done and boundary.empty():
                store.publish_fact(FactName.BOUNDARY_EMPTY, _fact(run_id, digest, "boundary-empty"))
                boundary_done = True
            if status is not None and not selector.get_map() and boundary_done:
                return
            if deadline is not None and time.monotonic() >= deadline:
                return
            timeout = 0.05 if status is None else min(0.05, max(0.0, cast("float", deadline) - time.monotonic()))
            for key, _ in selector.select(timeout):
                if key.data is None:
                    chunk = request.stdin[offset : offset + 65536]
                    try:
                        offset += os.write(input_fd, chunk)
                    except BrokenPipeError:
                        offset = len(request.stdin)
                    if offset == len(request.stdin):
                        selector.unregister(input_fd)
                        _close(input_fd)
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
                            state.retained, state.digest = capture.length, capture.sha256
                            disposition = capture.disposition.value
                        else:
                            disposition = "discarded" if request.output_mode == "discard" else "sensitivity-suppressed"
                        name = FactName.STDOUT_END if state.stream is Stream.STDOUT else FactName.STDERR_END
                        store.publish_fact(
                            name,
                            _fact(
                                run_id,
                                digest,
                                "stream-end",
                                stream=state.stream.value,
                                retained_bytes=state.retained,
                                retained_sha256=state.digest,
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
