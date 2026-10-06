"""Native primitive proof in a parent-created, hidden, exclusively owned console.

This helper injects Unicode records, not physical keys. It implements no keyboard
adapter and exercises no SSH client or pseudoconsole. Importing it has no effects.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import runpy
import sys
import sysconfig
from pathlib import Path
from threading import Condition, Event, Thread
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast

from agentworks.execution.carriers.ssh._terminal_windows import WindowsTerminal, _ScreenBufferInfo

if TYPE_CHECKING:
    from collections.abc import Callable

_KEY_EVENT = 0x0001
_WINDOW_EVENT = 0x0004
_MENU_EVENT = 0x0008
_NOWAIT = 0x0002


class _Coord(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int16), ("y", ctypes.c_int16)]


class _KeyEvent(ctypes.Structure):
    # WCHAR is one UTF-16 code unit, even on hosts where c_wchar is four bytes.
    _fields_ = [
        ("down", ctypes.c_int32),
        ("repeat", ctypes.c_uint16),
        ("virtual_key", ctypes.c_uint16),
        ("scan", ctypes.c_uint16),
        ("character", ctypes.c_uint16),
        ("control", ctypes.c_uint32),
    ]


class _Payload(ctypes.Union):
    _fields_ = [
        ("key", _KeyEvent),
        ("size", _Coord),
        ("menu", ctypes.c_uint32),
        ("storage", ctypes.c_ubyte * 16),
    ]


class _InputRecord(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_uint16), ("event", _Payload)]


def _key_record(unit: int) -> _InputRecord:
    record = _InputRecord()
    record.kind = _KEY_EVENT
    record.event.key = _KeyEvent(1, 1, 0, 0, unit, 0)
    return record


class _Native:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("This native fixture requires Windows")
        import msvcrt

        self.handle: Callable[[int], int] = msvcrt.get_osfhandle
        self.kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user: Any = ctypes.WinDLL("user32", use_last_error=True)
        dword_pointer = ctypes.POINTER(ctypes.c_uint32)
        bindings: tuple[tuple[str, list[Any], Any], ...] = (
            ("GetConsoleMode", [ctypes.c_void_p, dword_pointer], ctypes.c_int32),
            ("SetConsoleMode", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int32),
            ("GetConsoleCP", [], ctypes.c_uint32),
            ("GetConsoleOutputCP", [], ctypes.c_uint32),
            ("SetConsoleCP", [ctypes.c_uint32], ctypes.c_int32),
            ("SetConsoleOutputCP", [ctypes.c_uint32], ctypes.c_int32),
            ("GetConsoleProcessList", [dword_pointer, ctypes.c_uint32], ctypes.c_uint32),
            ("GetConsoleWindow", [], ctypes.c_void_p),
            ("GetHandleInformation", [ctypes.c_void_p, dword_pointer], ctypes.c_int32),
            ("GetConsoleScreenBufferInfo", [ctypes.c_void_p, ctypes.POINTER(_ScreenBufferInfo)], ctypes.c_int32),
            (
                "ReadConsoleInputExW",
                [ctypes.c_void_p, ctypes.POINTER(_InputRecord), ctypes.c_uint32, dword_pointer, ctypes.c_uint16],
                ctypes.c_int32,
            ),
            (
                "WriteConsoleInputW",
                [ctypes.c_void_p, ctypes.POINTER(_InputRecord), ctypes.c_uint32, dword_pointer],
                ctypes.c_int32,
            ),
            ("FreeConsole", [], ctypes.c_int32),
        )
        for name, arguments, result in bindings:
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, dword_pointer]
        self.user.GetWindowThreadProcessId.restype = ctypes.c_uint32
        self.user.IsWindowVisible.argtypes = [ctypes.c_void_p]
        self.user.IsWindowVisible.restype = ctypes.c_int32

    @staticmethod
    def check(result: int) -> None:
        if sys.platform == "win32" and not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def mode(self, handle: int) -> int:
        value = ctypes.c_uint32()
        self.check(self.kernel.GetConsoleMode(handle, ctypes.byref(value)))
        return value.value

    def flags(self, handle: int) -> int:
        value = ctypes.c_uint32()
        self.check(self.kernel.GetHandleInformation(handle, ctypes.byref(value)))
        return value.value

    def pages(self) -> tuple[int, int]:
        return int(self.kernel.GetConsoleCP()), int(self.kernel.GetConsoleOutputCP())

    def set_pages(self, pages: tuple[int, int]) -> None:
        self.check(self.kernel.SetConsoleCP(pages[0]))
        self.check(self.kernel.SetConsoleOutputCP(pages[1]))

    def dimensions(self, handle: int) -> tuple[int, int]:
        info = _ScreenBufferInfo()
        self.check(self.kernel.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)))
        return info.window.bottom - info.window.top + 1, info.window.right - info.window.left + 1

    def poll(self, handle: int) -> tuple[list[_InputRecord], float]:
        records = (_InputRecord * 32)()
        count = ctypes.c_uint32()
        start = perf_counter()
        self.check(self.kernel.ReadConsoleInputExW(handle, records, len(records), ctypes.byref(count), _NOWAIT))
        elapsed = perf_counter() - start
        assert count.value <= len(records)
        assert elapsed < 5
        return list(records[: count.value]), elapsed

    def inject(self, handle: int, values: list[_InputRecord]) -> None:
        records = (_InputRecord * len(values))(*values)
        count = ctypes.c_uint32()
        self.check(self.kernel.WriteConsoleInputW(handle, records, len(records), ctypes.byref(count)))
        assert count.value == len(values)

    def drain(self, handle: int) -> None:
        # Only this fresh fixture console is drained, with a finite batch budget.
        for _ in range(64):
            records, _ = self.poll(handle)
            if not records:
                return
        raise AssertionError("Owned fixture input did not become empty")


def _emit(value: dict[str, object]) -> None:
    print(json.dumps(value), flush=True)


class _FixtureWorker:
    """Admit fixture effects only after start returns; settle every borrower."""

    def __init__(self, work: Callable[[], None]) -> None:
        self._work = work
        self._condition = Condition()
        self._admitted = False
        self._cancelled = False
        self._done = Event()
        self._errors: list[BaseException] = []
        self._worker = Thread(target=self._entry)

    def run(self) -> None:
        try:
            self._worker.start()
            with self._condition:
                self._admitted = True
                self._condition.notify_all()
            self._done.wait()
        except BaseException as error:
            self._errors.append(error)
        finally:
            # A failed start can leave a late native tail. Cancel it while inert;
            # admitted work must finish before caller cleanup can touch its fds.
            while True:
                try:
                    with self._condition:
                        self._cancelled = True
                        admitted = self._admitted
                        self._condition.notify_all()
                    break
                except BaseException as error:
                    self._errors.append(error)
            if admitted:
                while True:
                    try:
                        if self._done.wait(0.05):
                            break
                    except BaseException as error:
                        self._errors.append(error)
        if self._errors:
            raise BaseExceptionGroup("Owned fixture worker failed", self._errors)

    def _entry(self) -> None:
        try:
            with self._condition:
                while not self._admitted and not self._cancelled:
                    self._condition.wait()
                if not self._admitted:
                    return
            self._work()
        except BaseException as error:
            self._errors.append(error)
        finally:
            self._done.set()


def _case(native: _Native, input_fd: int, output_fd: int, custom: bool) -> dict[str, object]:
    input_handle, output_handle = native.handle(input_fd), native.handle(output_fd)
    assert input_handle != output_handle
    if custom:
        input_mode, output_mode = native.mode(input_handle) ^ 0x0020, native.mode(output_handle) ^ 0x0002
        native.check(native.kernel.SetConsoleMode(input_handle, input_mode))
        native.check(native.kernel.SetConsoleMode(output_handle, output_mode))
        native.set_pages((1252, 437))
        assert (native.mode(input_handle), native.mode(output_handle), native.pages()) == (
            input_mode,
            output_mode,
            (1252, 437),
        )
    before = (
        native.mode(input_handle),
        native.mode(output_handle),
        native.pages(),
        native.flags(input_handle),
        native.flags(output_handle),
        os.get_inheritable(input_fd),
        os.get_inheritable(output_fd),
    )
    terminal = WindowsTerminal.acquire(input_fd, output_fd)
    primary: BaseException | None = None
    try:
        raw_mode = native.mode(input_handle)
        assert raw_mode == (before[0] & ~7) | 0x0200
        assert terminal.input_handle == input_handle
        dimensions = terminal.dimensions()
        assert dimensions == native.dimensions(output_handle)
        assert all(value > 0 for value in dimensions)
        native.drain(input_handle)
        empty, empty_seconds = native.poll(input_handle)
        assert empty == []

        menu = _InputRecord()
        menu.kind, menu.event.menu = _MENU_EVENT, 0x8321
        resize = _InputRecord()
        resize.kind, resize.event.size = _WINDOW_EVENT, _Coord(97, 31)
        native.inject(input_handle, [menu, resize])
        non_keys, non_key_seconds = native.poll(input_handle)
        assert all(record.kind != _KEY_EVENT for record in non_keys)
        assert any(record.kind == _MENU_EVENT and record.event.menu == 0x8321 for record in non_keys)
        assert any(
            record.kind == _WINDOW_EVENT and (record.event.size.x, record.event.size.y) == (97, 31)
            for record in non_keys
        )
        after_non_keys, after_non_key_seconds = native.poll(input_handle)
        assert after_non_keys == []

        units = (0x0041, 0x03A9, 0xD83D, 0xDE03)
        native.inject(input_handle, [_key_record(unit) for unit in units])
        unicode_records, unicode_seconds = native.poll(input_handle)
        observed = tuple(record.event.key.character for record in unicode_records if record.kind == _KEY_EVENT)
        assert observed == units
        after_unicode, _ = native.poll(input_handle)
        assert after_unicode == []
        active_unaffected = (
            native.mode(output_handle),
            native.pages(),
            native.flags(input_handle),
            native.flags(output_handle),
            os.get_inheritable(input_fd),
            os.get_inheritable(output_fd),
        )
        assert active_unaffected == before[1:]
        os.fstat(input_fd)
        os.fstat(output_fd)
    except BaseException as error:
        primary = error
        raise
    finally:
        release_errors = terminal.release()
        if release_errors:
            cleanup = BaseExceptionGroup("Native terminal restoration failed", release_errors)
            if primary is not None:
                cleanup.__cause__, cleanup.__context__ = primary.__cause__, primary.__context__
                raise primary from cleanup
            raise cleanup
    after = (
        native.mode(input_handle),
        native.mode(output_handle),
        native.pages(),
        native.flags(input_handle),
        native.flags(output_handle),
        os.get_inheritable(input_fd),
        os.get_inheritable(output_fd),
    )
    assert after == before
    assert terminal.release() == ()
    os.fstat(input_fd)
    os.fstat(output_fd)
    return {
        "custom": custom,
        "before": before,
        "raw_mode": raw_mode,
        "after": after,
        "dimensions": dimensions,
        "empty_count": len(empty),
        "non_key_types": [record.kind for record in non_keys],
        "after_non_key_count": len(after_non_keys),
        "unicode_units": observed,
        "poll_seconds": [empty_seconds, non_key_seconds, after_non_key_seconds, unicode_seconds],
    }


def _interpreter_identity() -> dict[str, str]:
    resource = sys.modules[WindowsTerminal.__module__].__file__
    assert resource is not None
    purelib = sysconfig.get_path("purelib")
    assert purelib is not None
    installed_resource = Path(purelib) / Path(*WindowsTerminal.__module__.split(".")).with_suffix(".py")
    identity = {
        name: os.path.normcase(str(Path(value).resolve()))
        for name, value in {
            "executable": sys.executable,
            "base_executable": cast("str", vars(sys)["_base_executable"]),
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
            "resource_file": resource,
            "installed_resource_file": str(installed_resource),
        }.items()
    }
    identity["resource_sha256"] = hashlib.sha256(Path(resource).read_bytes()).hexdigest()
    return identity


def _check_candidate_identity(actual: dict[str, str], expected: dict[str, str]) -> None:
    # Pytest can import checkout source while isolated startup imports the wheel.
    # Only origins may differ; the installed resource must contain candidate bytes.
    assert {key: value for key, value in actual.items() if key != "resource_file"} == {
        key: value for key, value in expected.items() if key != "resource_file"
    }
    assert actual["resource_file"] == actual["installed_resource_file"]
    assert Path(actual["resource_file"]).is_relative_to(Path(actual["prefix"]))


def main() -> None:
    if sys.platform != "win32":
        raise OSError("This native fixture requires Windows")
    native = _Native()
    window = int(native.kernel.GetConsoleWindow() or 0)
    window_pid = ctypes.c_uint32()
    if window:
        native.check(native.user.GetWindowThreadProcessId(window, ctypes.byref(window_pid)))
    processes = (ctypes.c_uint32 * 8)()
    count = native.kernel.GetConsoleProcessList(processes, len(processes))
    native.check(count)
    _emit(
        {
            "phase": "identity",
            "pid": os.getpid(),
            "window": window,
            "window_pid": window_pid.value,
            "window_visible": bool(native.user.IsWindowVisible(window)) if window else None,
            "console_pids": list(processes[:count]),
            "interpreter": _interpreter_identity(),
            "isolated": sys.flags.isolated,
            "ignore_environment": sys.flags.ignore_environment,
            "no_user_site": sys.flags.no_user_site,
        }
    )
    input_fd = output_fd = None
    errors: list[BaseException] = []
    baseline: tuple[int, int, tuple[int, int]] | None = None
    try:
        assert sys.argv[2:] in ([], ["--input-comparison"])
        input_comparison = sys.argv[2:] == ["--input-comparison"]
        assert count == 1 and processes[0] == os.getpid()
        assert window and not native.user.IsWindowVisible(window)
        _check_candidate_identity(_interpreter_identity(), json.loads(sys.argv[1]))
        assert sys.flags.isolated == sys.flags.ignore_environment == sys.flags.no_user_site == 1
        input_fd = os.open("CONIN$", os.O_RDWR | os.O_BINARY)
        output_fd = os.open("CONOUT$", os.O_RDWR | os.O_BINARY)
        input_handle, output_handle = native.handle(input_fd), native.handle(output_fd)
        baseline = native.mode(input_handle), native.mode(output_handle), native.pages()
        cases: list[dict[str, object]] = []

        def work() -> None:
            assert input_fd is not None and output_fd is not None
            for custom in (False, True):
                cases.append(_case(native, input_fd, output_fd, custom))
            if input_comparison:
                # Isolated startup omits checkout import paths. Load the selected
                # sibling measurement by its exact path without changing them.
                comparison = runpy.run_path(str(Path(__file__).with_name("windows_input_comparison.py")))
                comparison["compare"](native, input_handle, _key_record, _emit)

        # The parent owns the process timeout. Never release fds while a native
        # borrower may remain active, even if that means the parent must kill us.
        _FixtureWorker(work).run()
        _emit({"phase": "measurements", "cases": cases})
    except BaseException as error:
        errors.append(error)
    finally:
        if baseline is not None:
            for handle, mode in ((input_handle, baseline[0]), (output_handle, baseline[1])):
                try:
                    native.check(native.kernel.SetConsoleMode(handle, mode))
                    assert native.mode(handle) == mode
                except BaseException as error:
                    errors.append(error)
            try:
                native.set_pages(baseline[2])
                assert native.pages() == baseline[2]
            except BaseException as error:
                errors.append(error)
        closed: list[int] = []
        for fd in (output_fd, input_fd):
            if fd is not None:
                try:
                    os.close(fd)
                    closed.append(fd)
                except BaseException as error:
                    errors.append(error)
        try:
            native.check(native.kernel.FreeConsole())
            assert not native.kernel.GetConsoleWindow()
        except BaseException as error:
            errors.append(error)
        _emit(
            {
                "phase": "cleanup",
                "closed_descriptors": closed,
                "error_types": [type(error).__name__ for error in errors],
            }
        )
    if errors:
        raise BaseExceptionGroup("Owned native console proof failed", errors)


if __name__ == "__main__":
    main()
