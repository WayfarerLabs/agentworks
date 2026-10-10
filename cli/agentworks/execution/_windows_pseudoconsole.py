"""Private mixed-handle ConPTY topology candidate, not production capability.

The caller retains this passive cell before acquisition and serializes lifetime
operations on one non-main worker. Pipe handles are borrowed, not transferred;
every borrower must stop before close. No finalizer performs native cleanup.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from dataclasses import dataclass, field
from threading import Thread, current_thread, main_thread
from typing import Any


class _Coord(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int16), ("y", ctypes.c_int16)]


class _Security(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("descriptor", ctypes.c_void_p), ("inherit", ctypes.c_int32)]


class _Startup(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("reserved", ctypes.c_wchar_p),
        ("desktop", ctypes.c_wchar_p),
        ("title", ctypes.c_wchar_p),
        *[(name, ctypes.c_uint32) for name in ("x", "y", "xs", "ys", "xc", "yc", "fill", "flags")],
        ("show", ctypes.c_uint16),
        ("reserved_size", ctypes.c_uint16),
        ("reserved_bytes", ctypes.c_void_p),
        ("stdin", ctypes.c_void_p),
        ("stdout", ctypes.c_void_p),
        ("stderr", ctypes.c_void_p),
    ]


class _StartupEx(ctypes.Structure):
    _fields_ = [("startup", _Startup), ("attributes", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("process", ctypes.c_void_p),
        ("thread", ctypes.c_void_p),
        ("pid", ctypes.c_uint32),
        ("tid", ctypes.c_uint32),
    ]


@dataclass(frozen=True)
class BorrowedPipes:
    """Native synchronous HANDLEs valid only while the cell retains them."""

    input: int
    presentation: int
    stderr: int


def _dimensions(rows: int, columns: int) -> _Coord:
    if type(rows) is not int or type(columns) is not int or not (0 < rows <= 32767 and 0 < columns <= 32767):
        raise ValueError("Pseudoconsole dimensions must be positive signed SHORTs")
    return _Coord(columns, rows)


class _Api:
    """Lazy fixed-width Win32 bindings; only the retained worker constructs it."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Pseudoconsole proof requires Windows")
        self.kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        pointer = ctypes.c_void_p
        dword = ctypes.c_uint32
        handle_pointer = ctypes.POINTER(pointer)
        bindings: tuple[tuple[str, list[Any], Any], ...] = (
            ("CreatePipe", [handle_pointer, handle_pointer, pointer, dword], ctypes.c_int32),
            ("SetHandleInformation", [pointer, dword, dword], ctypes.c_int32),
            ("CreatePseudoConsole", [_Coord, pointer, pointer, dword, handle_pointer], ctypes.c_int32),
            ("ResizePseudoConsole", [pointer, _Coord], ctypes.c_int32),
            ("ClosePseudoConsole", [pointer], None),
            ("CloseHandle", [pointer], ctypes.c_int32),
            (
                "InitializeProcThreadAttributeList",
                [pointer, dword, dword, ctypes.POINTER(ctypes.c_size_t)],
                ctypes.c_int32,
            ),
            (
                "UpdateProcThreadAttribute",
                [pointer, dword, ctypes.c_size_t, pointer, ctypes.c_size_t, pointer, pointer],
                ctypes.c_int32,
            ),
            ("DeleteProcThreadAttributeList", [pointer], None),
            (
                "CreateProcessW",
                [
                    ctypes.c_wchar_p,
                    ctypes.c_wchar_p,
                    pointer,
                    pointer,
                    ctypes.c_int32,
                    dword,
                    pointer,
                    pointer,
                    pointer,
                    pointer,
                ],
                ctypes.c_int32,
            ),
            ("WaitForSingleObject", [pointer, dword], dword),
            ("GetExitCodeProcess", [pointer, ctypes.POINTER(dword)], ctypes.c_int32),
            ("TerminateProcess", [pointer, dword], ctypes.c_int32),
        )
        for name, arguments, result in bindings:
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def check(result: int, operation: str) -> None:
        if not result:
            # No command line, payload or native formatted diagnostic is retained.
            raise OSError(ctypes.get_last_error(), operation)  # type: ignore[attr-defined]

    def pipe(self, cell: WindowsPseudoConsole, read: str, write: str, *, inheritable: bool = False) -> None:
        r, w = ctypes.c_void_p(), ctypes.c_void_p()
        security = _Security(ctypes.sizeof(_Security), None, inheritable)
        result = None
        try:
            result = self.kernel.CreatePipe(ctypes.byref(r), ctypes.byref(w), ctypes.byref(security), 0)
        finally:
            if result is None:
                cell._acquisition_uncertain = True
            elif result:
                cell._handles[read], cell._handles[write] = r.value or 0, w.value or 0
        self.check(result, "CreatePipe")

    def console(self, cell: WindowsPseudoConsole) -> None:
        handle = ctypes.c_void_p()
        result = None
        try:
            result = self.kernel.CreatePseudoConsole(
                _dimensions(cell.rows, cell.columns),
                cell._handles["input_read"],
                cell._handles["output_write"],
                0,
                ctypes.byref(handle),
            )
        finally:
            if result is None:
                cell._acquisition_uncertain = True
            elif result == 0:
                cell._hpcon = handle.value or 0
        if result != 0:
            raise OSError(result, "CreatePseudoConsole")

    def attributes(self, cell: WindowsPseudoConsole) -> None:
        size = ctypes.c_size_t()
        result = self.kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if result or ctypes.get_last_error() != 122 or not size.value:  # type: ignore[attr-defined]
            raise OSError("InitializeProcThreadAttributeList sizing")
        cell._attributes = ctypes.create_string_buffer(size.value)
        result = None
        try:
            result = self.kernel.InitializeProcThreadAttributeList(cell._attributes, 2, 0, ctypes.byref(size))
        finally:
            if result is None:
                cell._acquisition_uncertain = True
            else:
                cell._attributes_initialized = bool(result)
        self.check(result, "InitializeProcThreadAttributeList")
        cell._inherited = (ctypes.c_void_p * 1)(cell._handles["stderr_write"])
        # PSEUDOCONSOLE takes HPCON itself, unlike HANDLE_LIST's pointer to an array.
        for key, value, length in (
            (0x00020016, cell._hpcon, ctypes.sizeof(ctypes.c_void_p)),
            (0x00020002, cell._inherited, ctypes.sizeof(cell._inherited)),
        ):
            self.check(
                self.kernel.UpdateProcThreadAttribute(cell._attributes, 0, key, value, length, None, None),
                "UpdateProcThreadAttribute",
            )

    def launch(self, cell: WindowsPseudoConsole) -> None:
        startup = _StartupEx()
        startup.startup.cb = ctypes.sizeof(startup)
        startup.startup.flags = 0x00000100  # STARTF_USESTDHANDLES, NULL stdin/stdout intentionally.
        startup.startup.stderr = cell._handles["stderr_write"]
        startup.attributes = ctypes.cast(cell._attributes, ctypes.c_void_p)
        info = _ProcessInformation()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(cell.argv))
        result = None
        try:
            result = self.kernel.CreateProcessW(
                cell.argv[0],
                command,
                None,
                None,
                True,
                0x00080000,
                None,
                None,
                ctypes.byref(startup),
                ctypes.byref(info),
            )
        finally:
            if result is None:
                cell._acquisition_uncertain = True
            elif result:
                cell._handles["process"], cell._handles["thread"] = info.process or 0, info.thread or 0
        self.check(result, "CreateProcessW")


@dataclass(repr=False)
class WindowsPseudoConsole:
    """One caller-retained native cell, including capability-bearing failure."""

    argv: tuple[str, ...] = field(repr=False)
    rows: int
    columns: int
    _handles: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _hpcon: int = field(default=0, init=False, repr=False)
    _attributes: Any = field(default=None, init=False, repr=False)
    _inherited: Any = field(default=None, init=False, repr=False)
    _attributes_initialized: bool = field(default=False, init=False, repr=False)
    _api: _Api | None = field(default=None, init=False, repr=False)
    _worker: Thread | None = field(default=None, init=False, repr=False)
    _attempted: bool = field(default=False, init=False, repr=False)
    _ready: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _exit_status: int | None = field(default=None, init=False, repr=False)
    _acquisition_uncertain: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        # This private prototype is also called by the native probe, outside typing.
        if (
            type(self.argv) is not tuple
            or not self.argv
            or any(type(arg) is not str or "\0" in arg for arg in self.argv)
            or not self.argv[0]
        ):
            raise ValueError("Pseudoconsole requires literal non-NUL argv")
        _dimensions(self.rows, self.columns)

    def _check_worker(self) -> None:
        if current_thread() is main_thread() or self._worker is not current_thread():
            raise RuntimeError("Pseudoconsole effects require the retained caller worker")

    @property
    def has_capabilities(self) -> bool:
        return bool(
            any(self._handles.values()) or self._hpcon or self._attributes is not None or self._acquisition_uncertain
        )

    @property
    def pipes(self) -> BorrowedPipes:
        if not self._ready or self._closed:
            raise RuntimeError("Pseudoconsole pipes are unavailable")
        return BorrowedPipes(self._handles["input_write"], self._handles["output_read"], self._handles["stderr_read"])

    def create(self) -> None:
        if current_thread() is main_thread() or self._attempted or self._closed:
            raise RuntimeError("Pseudoconsole creation requires one retained worker attempt")
        self._worker, self._attempted = current_thread(), True
        self._api = api = _Api()
        api.pipe(self, "input_read", "input_write")
        api.pipe(self, "output_read", "output_write")
        api.pipe(self, "stderr_read", "stderr_write", inheritable=True)
        api.check(api.kernel.SetHandleInformation(self._handles["stderr_read"], 1, 0), "SetHandleInformation")
        api.console(self)
        api.attributes(self)
        api.launch(self)
        for name in ("input_read", "output_write", "stderr_write", "thread"):
            self._close_handle(name)
        self._ready = True

    def poll(self) -> int | None:
        self._check_worker()
        if self._exit_status is not None:
            return self._exit_status
        if self._api is None or not self._handles.get("process"):
            raise RuntimeError("No exact process handle for status")
        result = self._api.kernel.WaitForSingleObject(self._handles["process"], 0)
        if result == 258:
            return None
        if result != 0:
            raise OSError("WaitForSingleObject")
        code = ctypes.c_uint32()
        self._api.check(
            self._api.kernel.GetExitCodeProcess(self._handles["process"], ctypes.byref(code)), "GetExitCodeProcess"
        )
        self._exit_status = code.value
        return self._exit_status

    def resize(self, rows: int, columns: int) -> None:
        size = _dimensions(rows, columns)
        self._check_worker()
        if not self._ready or self._closed or self._api is None:
            raise RuntimeError("Pseudoconsole is unavailable for resize")
        result = self._api.kernel.ResizePseudoConsole(self._hpcon, size)
        if result != 0:
            raise OSError(result, "ResizePseudoConsole")
        self.rows, self.columns = rows, columns

    def kill(self) -> None:
        self._check_worker()
        if self.poll() is None:
            assert self._api is not None
            self._api.check(self._api.kernel.TerminateProcess(self._handles["process"], 1), "TerminateProcess")

    def _close_handle(self, name: str) -> None:
        handle = self._handles.get(name, 0)
        if handle:
            assert self._api is not None
            self._api.check(self._api.kernel.CloseHandle(handle), "CloseHandle")
            self._handles[name] = 0

    def close(self) -> bool:
        """After borrowers stop, attempt all independent closes; retain failures.

        Closing output BEFORE HPCON avoids the documented old-build deadlock.
        HPCON release alone does not prove client disconnection or process exit.
        """
        if self._closed:
            return True
        if not self._attempted:
            self._closed = True
            return True
        self._check_worker()
        self._ready = False
        failures = False
        if self._api is None:
            self._closed = not self.has_capabilities
            return self._closed
        if self._handles.get("process"):
            try:
                self.kill()
                if self._api.kernel.WaitForSingleObject(self._handles["process"], 1000) != 0:
                    failures = True
                else:
                    self.poll()
            except OSError:
                failures = True
        for name in (
            "input_write",
            "output_read",
            "stderr_read",
            "input_read",
            "output_write",
            "stderr_write",
            "thread",
        ):
            try:
                self._close_handle(name)
            except OSError:
                failures = True
        if self._hpcon and not self._handles.get("output_read") and not self._handles.get("output_write"):
            try:
                self._api.kernel.ClosePseudoConsole(self._hpcon)
                self._hpcon = 0
            except OSError:
                failures = True
        if self._attributes_initialized:
            try:
                self._api.kernel.DeleteProcThreadAttributeList(self._attributes)
                self._attributes_initialized = False
            except OSError:
                failures = True
        if not self._attributes_initialized:
            self._attributes = self._inherited = None
        if self._exit_status is not None:
            try:
                self._close_handle("process")
            except OSError:
                failures = True
        self._closed = not failures and not self.has_capabilities
        return self._closed
