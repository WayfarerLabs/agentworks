"""Native Windows evidence for the private creation-time WSL client owner."""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any, cast

import pytest

from agentworks.execution._wsl2_lifecycle import HandleSettlement, HostClientStatus, JobAssignment
from agentworks.execution._wsl2_win32 import WindowsApi
from agentworks.execution._wsl2_windows import WindowsWSL2HostClient
from agentworks.execution.carrier import Deadline

pytestmark = pytest.mark.windows
_native_windows = pytest.mark.skipif(os.name != "nt", reason="requires native Windows handles")


class _FakeApi:
    ERROR_BROKEN_PIPE = 109
    ERROR_INVALID_HANDLE = 6
    WAIT_OBJECT_0 = 0
    WAIT_FAILED = 0xFFFFFFFF

    class ProcessInformation:
        pass

    def __init__(
        self,
        *,
        create_fails: bool = False,
        raises_after_process: bool = False,
        partial_process_info: bool = False,
        close_failures: dict[int, int] | None = None,
        accounting_failures: int = 0,
        active_results: list[int | BaseException] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.create_fails = create_fails
        self.raises_after_process = raises_after_process
        self.partial_process_info = partial_process_info
        self.close_failures = {} if close_failures is None else close_failures
        self.accounting_failures = accounting_failures
        self.active_results = [] if active_results is None else active_results
        self.chunks = [] if chunks is None else chunks
        self.closed: list[int] = []
        self.close_attempts: list[int] = []
        self.read_calls = 0
        self.pipe_calls = 0
        self.argv: tuple[object, ...] | None = None
        self.terminate_calls = 0

    def create_job(self) -> int:
        return 1

    def wsl_executable(self) -> str:
        return "/trusted/system/wsl.exe"

    def configure_kill_on_close(self, handle: int) -> None:
        assert handle == 1

    def pipe(self) -> tuple[int, int]:
        self.pipe_calls += 1
        return (2, 3) if self.pipe_calls == 1 else (4, 5)

    def nul_for_child(self) -> int:
        return 6

    def non_inheritable(self, handle: int) -> None:
        assert handle in {3, 4}

    def create_process(self, *_arguments: object) -> None:
        self.argv = _arguments
        if self.create_fails:
            raise OSError("synthetic CreateProcessW failure")
        process_info = cast(Any, _arguments[-1])
        process_info.hProcess = 7
        if self.partial_process_info:
            raise OSError("synthetic partial PROCESS_INFORMATION failure")
        process_info.hThread = 8
        if self.raises_after_process:
            raise OSError("synthetic post-CreateProcessW failure")

    def close_handle(self, handle: int) -> None:
        self.close_attempts.append(handle)
        remaining = self.close_failures.get(handle, 0)
        if remaining:
            self.close_failures[handle] = remaining - 1
            raise OSError("synthetic CloseHandle failure")
        self.closed.append(handle)

    def terminate_job(self, handle: int) -> None:
        assert handle == 1
        self.terminate_calls += 1

    def active_processes(self, handle: int) -> int:
        assert handle == 1
        if self.accounting_failures:
            self.accounting_failures -= 1
            raise OSError("synthetic accounting failure")
        if self.active_results:
            result = self.active_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return 0

    def wait_process(self, handle: int, milliseconds: int) -> int:
        assert handle == 7 and milliseconds == 0
        return self.WAIT_OBJECT_0

    def exit_code(self, handle: int) -> int:
        assert handle == 7
        return 23

    def read_pipe(self, handle: int, limit: int) -> tuple[bytes | None, int]:
        assert handle == 4 and limit == 4096
        self.read_calls += 1
        if self.chunks:
            return self.chunks.pop(0), 0
        return None, self.ERROR_BROKEN_PIPE


def _fake_factory(api: _FakeApi) -> Callable[[], WindowsApi]:
    return lambda: cast(WindowsApi, api)


def _argv(source: str, *arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-S", "-B", "-c", source, *arguments)


def _wait_for_exit(owner: WindowsWSL2HostClient) -> int:
    status = owner.wait(Deadline.after(3))
    assert status is not None
    return status


def _settle(owner: WindowsWSL2HostClient) -> None:
    snapshot = owner.settle(Deadline.after(3))
    assert snapshot.settled
    assert snapshot.host_handle_settlement == HandleSettlement.CLOSED
    assert snapshot.job_handle_settlement == HandleSettlement.CLOSED


@contextmanager
def _owned_client() -> Iterator[WindowsWSL2HostClient]:
    owner = WindowsWSL2HostClient()
    try:
        yield owner
    finally:
        with suppress(OSError, TimeoutError):
            owner.settle(Deadline.after(3))


def _synchronize_handle(pid: int) -> tuple[int, Callable[[], None]]:
    import ctypes
    from ctypes import wintypes

    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)
    value = getattr(handle, "value", handle)
    if not value or value == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")  # type: ignore[attr-defined]

    def close() -> None:
        assert kernel.CloseHandle(handle)

    return int(value), close


def _assert_terminated(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    assert kernel.WaitForSingleObject(handle, 3000) == 0


def test_construction_and_precreation_expiry_are_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = WindowsWSL2HostClient()
    assert owner.snapshot().host_client_status == HostClientStatus.NOT_CREATED

    def forbidden_thread(*_arguments: object) -> int:
        raise AssertionError("expired creation started an owner thread")

    monkeypatch.setattr("agentworks.execution._wsl2_windows._thread.start_new_thread", forbidden_thread)
    with pytest.raises(TimeoutError):
        owner.spawn_owned(("not-a-native-command",), Deadline.after(0))
    snapshot = owner.snapshot()
    assert snapshot.host_client_status == HostClientStatus.NOT_CREATED
    assert snapshot.job_assignment == JobAssignment.NOT_CREATED
    assert snapshot.settled


def test_portable_settlement_exits_owner_thread_and_releases_owner() -> None:
    api = _FakeApi()
    owner = WindowsWSL2HostClient(api_factory=_fake_factory(api))
    owner.spawn_owned(("/synthetic/client",), Deadline.after(1))

    settled = owner.settle(Deadline.after(1))
    assert settled.settled
    assert owner.wait(Deadline.after(0)) == 23
    assert owner.snapshot() == settled
    assert owner.settle(Deadline.after(0)) == settled

    reference = weakref.ref(owner)
    del owner
    until = time.monotonic() + 1
    while reference() is not None and time.monotonic() < until:
        gc.collect()
        time.sleep(0.01)
    assert reference() is None


def test_portable_creation_failure_is_settled_without_requeue() -> None:
    api = _FakeApi(create_fails=True)
    owner = WindowsWSL2HostClient(api_factory=_fake_factory(api))

    with pytest.raises(OSError, match="synthetic CreateProcessW failure"):
        owner.spawn_owned(("/synthetic/client",), Deadline.after(1))

    settled = owner.snapshot()
    attempts = list(api.close_attempts)
    assert settled.settled
    assert owner.settle(Deadline.after(0)) == settled
    assert api.close_attempts == attempts


@pytest.mark.parametrize("failed_handle, settlement", [(1, "job"), (7, "host")])
def test_portable_failed_close_retries_to_exact_settlement(failed_handle: int, settlement: str) -> None:
    api = _FakeApi(close_failures={failed_handle: 1})
    owner = WindowsWSL2HostClient(api_factory=_fake_factory(api))
    owner.spawn_owned(("/synthetic/client",), Deadline.after(1))

    first = owner.settle(Deadline.after(1))
    assert not first.settled
    if settlement == "job":
        assert first.job_handle_settlement == HandleSettlement.UNKNOWN
    else:
        assert first.host_handle_settlement == HandleSettlement.UNKNOWN

    second = owner.settle(Deadline.after(1))
    assert second.settled
    assert failed_handle in api.close_attempts
    assert api.close_attempts.count(failed_handle) == 2


def test_portable_post_creation_error_retains_harvested_handles() -> None:
    api = _FakeApi(raises_after_process=True)
    owner = WindowsWSL2HostClient(api_factory=_fake_factory(api))

    with pytest.raises(OSError, match="post-CreateProcessW"):
        owner.spawn_owned(("/synthetic/client",), Deadline.after(1))

    settled = owner.settle(Deadline.after(1))
    assert settled.settled
    assert settled.host_client_status == HostClientStatus.EXITED
    assert settled.host_client_exit_status == 23
    assert settled.job_assignment == JobAssignment.ASSIGNED_AT_CREATION


def test_portable_partial_process_information_remains_unknown_after_handle_cleanup() -> None:
    api = _FakeApi(partial_process_info=True)
    owner = WindowsWSL2HostClient(api_factory=_fake_factory(api))

    with pytest.raises(OSError, match="partial PROCESS_INFORMATION"):
        owner.spawn_owned(("/synthetic/client",), Deadline.after(1))

    snapshot = owner.settle(Deadline.after(1))
    assert snapshot.host_client_status == HostClientStatus.UNKNOWN
    assert snapshot.job_assignment == JobAssignment.UNKNOWN
    assert snapshot.host_handle_settlement == HandleSettlement.CLOSED
    assert snapshot.job_handle_settlement == HandleSettlement.CLOSED
    assert not snapshot.settled


def test_portable_later_accounting_observation_terminates_live_job() -> None:
    api = _FakeApi(active_results=[OSError("synthetic initial accounting failure"), 1, 0])
    owner = WindowsWSL2HostClient(api_factory=_fake_factory(api))
    owner.spawn_owned(("/synthetic/client",), Deadline.after(1))

    assert owner.settle(Deadline.after(1)).settled
    assert api.terminate_calls == 1


def test_portable_stdout_overflow_remains_bounded_while_drain_continues() -> None:
    api = _FakeApi(chunks=[b"x" * 1025])
    owner = WindowsWSL2HostClient(api_factory=_fake_factory(api))
    owner.spawn_owned(("/synthetic/client",), Deadline.after(1))

    with pytest.raises(OSError):
        owner.read_stdout_line(2048, Deadline.after(1))
    assert api.read_calls == 2
    assert owner.settle(Deadline.after(1)).settled


def test_portable_bare_wsl_uses_trusted_system_path_only() -> None:
    api = _FakeApi()
    owner = WindowsWSL2HostClient(api_factory=_fake_factory(api))
    owner.spawn_owned(("wsl", "--version"), Deadline.after(1))
    assert api.argv is not None and api.argv[0] == ("/trusted/system/wsl.exe", "--version")
    _settle(owner)

    rejected = WindowsWSL2HostClient(api_factory=_fake_factory(_FakeApi()))
    with pytest.raises(OSError):
        rejected.spawn_owned(("workspace-wsl",), Deadline.after(1))
    assert rejected.snapshot().settled


@_native_windows
def test_creation_time_job_assignment_literal_argv_and_eof() -> None:
    source = (
        "import json,sys;"
        "sys.stdout.buffer.write(json.dumps(sys.argv[1:]).encode()+b'\\n');"
        "sys.stdout.buffer.flush();sys.stdin.buffer.read();"
        "sys.stdout.buffer.write(b'EOF\\n');sys.stdout.buffer.flush()"
    )
    arguments = ("space value", 'quote"value', r"trailing\\")
    with _owned_client() as owner:
        owner.spawn_owned(_argv(source, *arguments), Deadline.after(3))

        checkpoint = owner.snapshot()
        assert checkpoint.host_client_status == HostClientStatus.ACTIVE
        assert checkpoint.job_assignment == JobAssignment.ASSIGNED_AT_CREATION
        assert checkpoint.host_handle_settlement == HandleSettlement.OPEN
        assert checkpoint.job_handle_settlement == HandleSettlement.OPEN
        assert json.loads(owner.read_stdout_line(512, Deadline.after(2))) == list(arguments)

        owner.close_stdin()
        assert owner.read_stdout_line(32, Deadline.after(2)) == b"EOF\n"
        assert _wait_for_exit(owner) == 0
        _settle(owner)


@_native_windows
def test_partial_stdout_line_is_retryable_without_blocking() -> None:
    with _owned_client() as owner:
        owner.spawn_owned(
            _argv(
                "import sys,time;sys.stdout.buffer.write(b'partial');sys.stdout.buffer.flush();"
                "time.sleep(.2);sys.stdout.buffer.write(b' line\\n');sys.stdout.buffer.flush();sys.stdin.buffer.read()"
            ),
            Deadline.after(3),
        )

        with pytest.raises(TimeoutError):
            owner.read_stdout_line(64, Deadline.after(0.02))
        assert owner.read_stdout_line(64, Deadline.after(2)) == b"partial line\n"

        owner.close_stdin()
        assert _wait_for_exit(owner) == 0
        _settle(owner)


@pytest.mark.parametrize("status", [0, 1, 255])
@_native_windows
def test_wait_reports_exact_windows_exit_status(status: int) -> None:
    with _owned_client() as owner:
        owner.spawn_owned(_argv(f"import sys;sys.stdin.buffer.read();raise SystemExit({status})"), Deadline.after(3))

        owner.close_stdin()
        assert _wait_for_exit(owner) == status
        _settle(owner)


@_native_windows
def test_settlement_kills_job_descendants_but_not_unrelated_process() -> None:
    descendant_source = "import time;time.sleep(30)"
    source = (
        "import subprocess,sys;"
        f"child=subprocess.Popen([{sys.executable!r},'-I','-S','-B','-c',{descendant_source!r}]);"
        "sys.stdout.write(str(child.pid)+'\\n');sys.stdout.flush();sys.stdin.buffer.read()"
    )
    unrelated = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", "import time;time.sleep(30)"])
    try:
        with _owned_client() as owner:
            owner.spawn_owned(_argv(source), Deadline.after(3))
            descendant_pid = int(owner.read_stdout_line(64, Deadline.after(2)))
            descendant_handle, close_descendant = _synchronize_handle(descendant_pid)
            try:
                _settle(owner)
                _assert_terminated(descendant_handle)
                assert unrelated.poll() is None
            finally:
                close_descendant()
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=3)


@_native_windows
def test_explicit_handle_list_excludes_unrelated_inheritable_handle() -> None:
    import msvcrt

    reader, writer = os.pipe()
    os.set_inheritable(writer, True)
    raw_handle = msvcrt.get_osfhandle(writer)  # type: ignore[attr-defined]
    source = (
        "import msvcrt,os,sys\n"
        "raw = int(sys.argv[1])\n"
        "try:\n"
        "    descriptor = msvcrt.open_osfhandle(raw, 0)\n"
        "    os.write(descriptor, b'x')\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit(99)\n"
        "sys.stdin.buffer.read()\n"
    )
    try:
        with _owned_client() as owner:
            owner.spawn_owned(_argv(source, str(raw_handle)), Deadline.after(3))
            owner.close_stdin()
            assert _wait_for_exit(owner) == 0
            os.close(writer)
            writer = -1
            assert os.read(reader, 1) == b""
            first = owner.settle(Deadline.after(3))
            second = owner.settle(Deadline.after(3))
            assert first.settled and second.settled
    finally:
        os.close(reader)
        if writer >= 0:
            os.close(writer)
