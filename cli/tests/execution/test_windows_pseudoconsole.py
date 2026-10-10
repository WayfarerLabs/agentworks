"""Portable capability/ordering checks, not native mixed-handle acceptance."""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from collections.abc import Callable
from threading import Event, Thread

import pytest

from agentworks.execution import _windows_pseudoconsole as native

pytestmark = pytest.mark.windows


def _worker(work: Callable[[], object]) -> None:
    errors = []

    def run():
        try:
            work()
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    if errors:
        raise errors[0]


class Kernel:
    def __init__(self, cell: native.WindowsPseudoConsole, fail: str | None = None) -> None:
        self.cell, self.fail = cell, fail
        self.calls: list[str | tuple[str, int]] = []
        self.next_handle = 10
        self.live: set[int] = set()
        self.exited = False
        self.exit_code = 259
        self.close_fail = None
        self.status_fail = False
        self.pipe_count = 0

    def _call(self, name: str) -> bool:
        assert self.cell._attempted and self.cell._worker is not None
        self.calls.append(name)
        return name != self.fail

    def _handle(self) -> int:
        self.next_handle += 1
        self.live.add(self.next_handle)
        return self.next_handle

    def CreatePipe(self, read, write, security, size):
        self.pipe_count += 1
        assert size == 0
        assert bool(security._obj.inherit) == (self.pipe_count == 3)
        result = self._call(f"pipe{self.pipe_count}")
        read._obj.value, write._obj.value = (self._handle(), self._handle()) if result else (999001, 999002)
        return result

    def SetHandleInformation(self, handle, mask, flags):
        assert handle == self.cell._handles["stderr_read"] and (mask, flags) == (1, 0)
        return self._call("inherit")

    def CreatePseudoConsole(self, size, input_handle, output_handle, flags, result):
        assert (size.x, size.y, flags) == (80, 24, 0)
        assert input_handle == self.cell._handles["input_read"]
        assert output_handle == self.cell._handles["output_write"]
        result._obj.value = 70
        return 0 if self._call("console") else -1

    def InitializeProcThreadAttributeList(self, buffer, count, flags, size):
        assert count == 2 and flags == 0
        if buffer is None:
            size._obj.value = 128
            return 0
        assert buffer is self.cell._attributes
        return self._call("initialize")

    def UpdateProcThreadAttribute(self, buffer, flags, key, value, size, previous, returned):
        assert buffer is self.cell._attributes and (flags, previous, returned) == (0, None, None)
        assert size == ctypes.sizeof(ctypes.c_void_p)
        if key == 0x20016:
            assert value == self.cell._hpcon
            return self._call("pseudoconsole-attribute")
        assert key == 0x20002 and list(value) == [self.cell._handles["stderr_write"]]
        return self._call("handle-list")

    def CreateProcessW(
        self, application, command, process_security, thread_security, inherit, flags, env, cwd, startup, info
    ):
        assert application == self.cell.argv[0]
        assert command.value == subprocess.list2cmdline(self.cell.argv)
        assert (process_security, thread_security, env, cwd) == (None, None, None, None)
        assert inherit is True and flags == 0x80000
        start = startup._obj
        assert start.startup.cb == ctypes.sizeof(native._StartupEx)
        assert start.startup.flags == 0x100
        assert start.startup.stdin is start.startup.stdout is None
        assert start.startup.stderr == self.cell._handles["stderr_write"]
        assert start.attributes == ctypes.addressof(self.cell._attributes)
        result = self._call("launch")
        info._obj.process, info._obj.thread = (self._handle(), self._handle()) if result else (999003, 999004)
        return result

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        if self.close_fail == handle:
            return 0
        assert handle in self.live
        self.live.remove(handle)
        return 1

    def ClosePseudoConsole(self, handle):
        assert handle == 70
        assert not self.cell._handles["output_read"] and not self.cell._handles["output_write"]
        self.calls.append("close-console")

    def DeleteProcThreadAttributeList(self, attributes):
        assert attributes is self.cell._attributes
        self.calls.append("delete-attributes")

    def WaitForSingleObject(self, handle, timeout):
        assert handle == self.cell._handles["process"] and timeout in (0, 1000)
        if self.status_fail:
            return 0xFFFFFFFF
        return 0 if self.exited else 258

    def GetExitCodeProcess(self, handle, code):
        assert handle == self.cell._handles["process"] and self.exited
        code._obj.value = self.exit_code
        return 1

    def TerminateProcess(self, handle, code):
        assert handle == self.cell._handles["process"] and code == 1
        self.exited = True
        return self._call("kill")

    def ResizePseudoConsole(self, handle, size):
        assert handle == 70 and (size.x, size.y) == (17, 9)
        return 0 if self._call("resize") else -1


@pytest.fixture
def cell(monkeypatch):
    cell = native.WindowsPseudoConsole((r"C:\local\python.exe", "secret argument", "", 'a"b\\'), 24, 80)
    kernel = Kernel(cell)
    api = object.__new__(native._Api)
    api.kernel = kernel
    monkeypatch.setattr(native, "_Api", lambda: api)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 122, raising=False)
    return cell, kernel


def test_import_and_constructor_are_passive():
    script = """
import ctypes
ctypes.WinDLL = None
from agentworks.execution._windows_pseudoconsole import WindowsPseudoConsole
c = WindowsPseudoConsole(('literal-secret',), 24, 80)
assert not c.has_capabilities
assert 'literal-secret' not in repr(c)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0


def test_fixed_width_native_structures_have_windows_layout():
    wide = ctypes.sizeof(ctypes.c_void_p) == 8
    assert ctypes.sizeof(native._Coord) == 4
    assert ctypes.sizeof(native._Security) == (24 if wide else 12)
    assert ctypes.sizeof(native._Startup) == (104 if wide else 68)
    assert ctypes.sizeof(native._StartupEx) == (112 if wide else 72)
    assert ctypes.sizeof(native._ProcessInformation) == (24 if wide else 16)


def test_unavailable_host_create_is_capability_free_and_closeable(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(native, "os", SimpleNamespace(name="posix"))
    candidate = native.WindowsPseudoConsole(("local-child",), 24, 80)

    def work():
        with pytest.raises(OSError):
            candidate.create()
        assert not candidate.has_capabilities
        assert candidate.close()

    _worker(work)


def test_probe_import_and_non_windows_refusal_have_no_native_effect(monkeypatch):
    from types import SimpleNamespace

    from . import windows_pseudoconsole_probe as probe

    monkeypatch.setattr(probe, "sys", SimpleNamespace(platform="linux"))
    assert probe.run_probe() == {"accepted": False, "stage": "non_windows"}
    wire = probe._frame()
    assert len(wire) == 65536 and len(bytes.fromhex(wire.decode("ascii"))) == 32768


@pytest.mark.parametrize("argv", [(), ["x"], ("",), ("a\0b",), (1,)])
def test_literal_request_refuses_invalid_argv(argv):
    with pytest.raises(ValueError):
        native.WindowsPseudoConsole(argv, 24, 80)


@pytest.mark.parametrize("rows,columns", [(0, 80), (24, 0), (-1, 80), (32768, 80), (24, True), (1.0, 80)])
def test_dimensions_refuse_outside_positive_short(rows, columns):
    with pytest.raises(ValueError):
        native.WindowsPseudoConsole(("x",), rows, columns)


def test_no_native_creation_on_main_thread(cell):
    candidate, kernel = cell
    with pytest.raises(RuntimeError):
        candidate.create()
    assert kernel.calls == [] and not candidate.has_capabilities
    assert candidate.close()


def test_actual_struct_topology_status_resize_and_idempotent_close(cell):
    candidate, kernel = cell

    def work():
        candidate.create()
        endpoints = candidate.pipes
        assert endpoints.input != endpoints.presentation != endpoints.stderr
        assert candidate.has_capabilities and candidate.poll() is None
        candidate.resize(9, 17)
        assert (candidate.rows, candidate.columns) == (9, 17)
        kernel.exited = True
        assert candidate.poll() == 259  # STILL_ACTIVE is a valid signaled exit code.
        assert candidate.close() and candidate.close()
        assert not candidate.has_capabilities and not kernel.live
        with pytest.raises(RuntimeError):
            _ = candidate.pipes
        with pytest.raises(RuntimeError):
            candidate.create()

    _worker(work)


@pytest.mark.parametrize(
    "failure",
    ["pipe1", "pipe2", "pipe3", "inherit", "console", "initialize", "pseudoconsole-attribute", "handle-list", "launch"],
)
def test_partial_acquisition_is_retained_before_error_and_no_child_is_not_clean(cell, failure):
    candidate, kernel = cell
    kernel.fail = failure

    def work():
        with pytest.raises(OSError):
            candidate.create()
        assert not candidate._closed
        assert candidate.has_capabilities == (failure != "pipe1")
        with pytest.raises(RuntimeError):
            _ = candidate.pipes
        assert candidate.close()
        assert not candidate.has_capabilities and not kernel.live

    _worker(work)


def test_native_acquisition_escape_is_uncertain_and_never_guessed_closed(cell):
    candidate, kernel = cell
    original = kernel.CreatePipe

    def escaped(*args):
        original(*args)
        raise KeyboardInterrupt

    kernel.CreatePipe = escaped

    def work():
        with pytest.raises(KeyboardInterrupt):
            candidate.create()
        assert candidate._acquisition_uncertain and candidate.has_capabilities
        assert not candidate.close()
        assert not any(isinstance(call, tuple) and call[0] == "close" for call in kernel.calls)

    _worker(work)


def test_unknown_attribute_initialization_retains_exact_storage_without_delete_or_replay(cell):
    candidate, kernel = cell
    initialize = kernel.InitializeProcThreadAttributeList

    def escaped(buffer, *args):
        result = initialize(buffer, *args)
        if buffer is not None:
            raise KeyboardInterrupt
        return result

    kernel.InitializeProcThreadAttributeList = escaped

    def work():
        with pytest.raises(KeyboardInterrupt):
            candidate.create()
        buffer, inherited = candidate._attributes, candidate._inherited
        assert buffer is not None and inherited is not None
        assert candidate._attributes_initialized is None
        assert not candidate.close() and not kernel.live
        calls = list(kernel.calls)
        assert candidate._attributes is buffer and candidate._inherited is inherited
        assert candidate._hpcon == 0 and candidate.has_capabilities
        assert "delete-attributes" not in calls
        assert not candidate.close() and kernel.calls == calls
        assert candidate._attributes is buffer and candidate._inherited is inherited

    _worker(work)


def test_output_close_failure_retains_console_until_explicit_retry(cell):
    candidate, kernel = cell

    def work():
        candidate.create()
        kernel.close_fail = candidate.pipes.presentation
        assert not candidate.close() and candidate.has_capabilities
        assert "close-console" not in kernel.calls
        kernel.close_fail = None
        assert candidate.close() and not kernel.live

    _worker(work)


def test_lost_status_preserves_exact_process_handle_but_closes_other_capabilities(cell):
    candidate, kernel = cell

    def work():
        candidate.create()
        process = candidate._handles["process"]
        kernel.status_fail = True
        assert not candidate.close()
        assert candidate._handles["process"] == process and kernel.live == {process}
        kernel.status_fail = False
        assert candidate.close()

    _worker(work)


def test_resize_refusal_does_not_publish_changed_dimensions(cell):
    candidate, kernel = cell

    def work():
        candidate.create()
        kernel.fail = "resize"
        with pytest.raises(OSError):
            candidate.resize(9, 17)
        assert (candidate.rows, candidate.columns) == (24, 80)
        assert candidate.close()

    _worker(work)


def test_attribute_release_failure_preserves_buffer_until_retry(cell):
    candidate, kernel = cell
    delete = kernel.DeleteProcThreadAttributeList

    def refused(_attributes):
        raise OSError("fixture release refusal")

    def work():
        candidate.create()
        kernel.DeleteProcThreadAttributeList = refused
        assert not candidate.close() and candidate._attributes_initialized
        assert candidate._attributes is not None and candidate._inherited is not None
        kernel.DeleteProcThreadAttributeList = delete
        assert candidate.close()

    _worker(work)


@pytest.mark.parametrize("endpoint", ["input_write", "stderr_read", "process"])
def test_failed_handle_release_retains_exact_endpoint_until_retry(cell, endpoint):
    candidate, kernel = cell

    def work():
        candidate.create()
        handle = candidate._handles[endpoint]
        kernel.close_fail = handle
        assert not candidate.close()
        assert candidate._handles[endpoint] == handle and handle in kernel.live
        kernel.close_fail = None
        assert candidate.close()

    _worker(work)


def test_effects_refuse_different_worker(cell):
    candidate, _ = cell
    ready, stop = Event(), Event()

    def retained():
        candidate.create()
        ready.set()
        assert stop.wait(5)
        assert candidate.close()

    owner = Thread(target=retained)
    owner.start()
    assert ready.wait(5)
    for action in (candidate.poll, candidate.kill, candidate.close, lambda: candidate.resize(9, 17)):
        with pytest.raises(RuntimeError):
            _worker(action)
    assert candidate.has_capabilities
    stop.set()
    owner.join(5)
    assert not owner.is_alive() and not candidate.has_capabilities


@pytest.fixture
def probe_boundary(monkeypatch):
    from types import SimpleNamespace

    from . import windows_pseudoconsole_probe as probe

    class Cell:
        def __init__(self, status: int | None = None) -> None:
            self.status = status
            self.has_capabilities = False
            self._acquisition_uncertain = False
            self._exit_status: int | None = None
            self.pipes = native.BorrowedPipes(1, 2, 3)
            self.resizes: list[tuple[int, int]] = []

        def create(self):
            self.has_capabilities = True

        def poll(self):
            return self.status

        def resize(self, rows, columns):
            self.resizes.append((rows, columns))

        def close(self):
            self._exit_status = 1 if self.status is None else self.status
            self.has_capabilities = False
            return True

    class IO:
        def __init__(
            self,
            cell: Cell,
            reflection: bytes = b"",
            delay: bool = False,
            wrap: bool = True,
            record: bytes | None = None,
        ) -> None:
            self.cell = cell
            self.frame = probe._frame()
            self.sent = bytearray()
            self.released = False
            self.prefix = [b"\x1b[?25l" + probe.PAYLOAD_READY + b"\r", None, b"\n"]
            self.prefix_complete = False
            self.reflection, self.delay, self.wrap = reflection, delay, wrap
            self.record = probe.INTERACTIVE_READY + b"\r\n" if record is None else record
            self.phase_chunks: list[bytes | None] = []
            self.phase_queued = False
            self.late_output = [None, None, probe.WRAP_FIXTURE] if wrap else [None]
            metadata = {"kinds": [2, 2, 3], "modes": [7, 3, None], "initial": [24, 80]}
            self.stderr: list[bytes | None] = [json.dumps(metadata).encode() + b"\n" + bytes(range(256))]
            self.now = 0.0

        def available(self, handle):
            if handle == 3:
                return self.stderr.pop(0) if self.stderr else None
            self.now += 0.02
            if self.prefix:
                chunk = self.prefix.pop(0)
                self.prefix_complete = not self.prefix
                return chunk
            if len(self.sent) == 65536 and not self.phase_queued:
                record = self.record
                self.phase_chunks = [self.reflection + record[:7], None, record[7:]]
                self.phase_queued = True
            if self.phase_chunks:
                return self.phase_chunks.pop(0)
            if self.released and self.late_output:
                return self.late_output.pop(0)
            return None

        def write(self, handle, data):
            assert handle == 1 and self.prefix_complete
            if len(self.sent) < 65536:
                assert self.cell.resizes == []
                self.sent.extend(data)
                assert self.frame.startswith(self.sent)
            else:
                assert data == probe.RELEASE and self.cell.resizes == [(9, 17)]
                assert not self.phase_chunks
                self.released = True
                result = {"received": 65536, "decoded": 32768, "fidelity": True, "changed": [9, 17], "setup_mode": 0}
                self.stderr += (
                    [None, json.dumps(result).encode() + b"\n"] if self.delay else [json.dumps(result).encode() + b"\n"]
                )
                self.cell.status = 0
            return len(data)

    def measure(*, idle=False, status=None, reflection=b"", delay=False, wrap=True, record=None):
        cell = Cell(status)
        io = IO(cell, reflection, delay, wrap, record)
        monkeypatch.setattr(probe, "_IO", lambda: io)
        monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=lambda: io.now, sleep=lambda _seconds: None))
        return probe._measure(cell, idle), cell, io

    return measure


@pytest.mark.parametrize("reflection", ["wrapped", "partial", "vt", "extra"])
def test_strict_setup_rejects_full_wrapped_and_partial_reflection_and_noise(probe_boundary, reflection):
    from . import windows_pseudoconsole_probe as probe

    wire = probe._frame()
    if reflection == "wrapped":
        noise = b"\r\n".join(wire[index : index + 17] for index in range(0, len(wire), 17))
    elif reflection == "partial":
        noise = wire[8192:8292]
    else:
        noise = b"\x1b[2J" if reflection == "vt" else b"unexpected"
    report, cell, io = probe_boundary(reflection=noise)
    assert not report["accepted"] and report["stage"] == "refused"
    assert len(io.sent) == 65536 and not io.released and cell.resizes == []
    assert report["closed"] and not cell.has_capabilities


def test_strict_setup_gates_complete_prefix_and_record_before_resize_release(probe_boundary):
    report, cell, io = probe_boundary(delay=True)
    assert report["accepted"] and report["strict_setup"]
    assert io.released and cell.resizes == [(9, 17)]
    assert report["setup_prefix_bytes"] == 57
    assert report["wrap_fixture_exact"] and report["status"] == 0


def test_missing_delayed_wrap_is_separately_unproved_not_inferred_from_exit(probe_boundary):
    report, _, _ = probe_boundary(wrap=False)
    assert report["strict_setup"] and report["status"] == 0
    assert report["wrap_fixture_exact"] is False


@pytest.mark.parametrize("fault", ["truncated", "wrong-phase", "wrong-nonce", "wrapped", "suffix"])
def test_entire_setup_window_must_be_exact_record_not_partial_or_containment(probe_boundary, fault):
    from . import windows_pseudoconsole_probe as probe

    record = probe.INTERACTIVE_READY + b"\r\n"
    if fault == "truncated":
        record = record[:-1]
    elif fault == "wrong-phase":
        record = probe.PAYLOAD_READY + b"\r\n"
    elif fault == "wrong-nonce":
        record = record.replace(b"01234567", b"87654321")
    elif fault == "wrapped":
        record = record[:17] + b"\r\n" + record[17:]
    else:
        record += b"extra after record"
    report, cell, io = probe_boundary(record=record)
    assert not report["accepted"] and not io.released and cell.resizes == []


@pytest.mark.parametrize("status", [None, 0, 7])
def test_idle_requires_exact_live_child_before_teardown(probe_boundary, status):
    report, _, io = probe_boundary(idle=True, status=status)
    assert report["accepted"] == (status is None)
    assert report["idle_live"] == (status is None)
    assert report["status"] == status and not io.sent
    assert report["cleanup_status"] == (1 if status is None else status)


def test_standalone_pending_cell_survives_normal_exit_until_outer_supervision():
    script = r"""
import ctypes, runpy, sys, threading
import agentworks.execution._terminal_handoff
import agentworks.execution._windows_pseudoconsole as native
class Cell:
    def __init__(self, *args):
        self.has_capabilities = True
        self._acquisition_uncertain = True
        self._exit_status = None
    def close(self):
        return False
native.WindowsPseudoConsole = Cell
def unavailable(*args, **kwargs):
    raise OSError('synthetic native boundary')
ctypes.WinDLL = unavailable
RealThread = threading.Thread
class QuickJoin(RealThread):
    def join(self, timeout=None):
        return super().join(0.01)
threading.Thread = QuickJoin
sys.platform = 'win32'
runpy.run_module('tests.execution.windows_pseudoconsole_probe', run_name='__main__')
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            process.communicate(timeout=10)
        report = json.loads(caught.value.output)
        assert report["stage"] == "retained_worker_pending" and not report["accepted"]
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows topology measurement")
def test_native_topology_probe():
    from .windows_pseudoconsole_probe import run_probe

    report = run_probe()
    assert report["accepted"], report
