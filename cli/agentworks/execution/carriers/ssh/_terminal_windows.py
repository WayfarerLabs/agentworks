"""Borrowed Windows console admission, raw input and one-shot mode restoration.

One retained non-main worker owns this native lifetime. This resource starts no
reader or client and changes no output mode, code page or descriptor flags. All
borrowers must stop before release; console modes are not emulator state.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, field
from threading import current_thread, main_thread
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from threading import Thread

_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200


class _Coord(ctypes.Structure):
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short)]


class _SmallRect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_short) for name in ("left", "top", "right", "bottom")]


class _ScreenBufferInfo(ctypes.Structure):
    _fields_ = [
        ("size", _Coord),
        ("cursor", _Coord),
        ("attributes", ctypes.c_ushort),
        ("window", _SmallRect),
        ("maximum", _Coord),
    ]


class _ConsoleAPI:
    """Concrete Win32 boundary, borrowing every handle obtained from explicit fds."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows console resources are unavailable on this host")
        self.kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        k.GetConsoleMode.restype = ctypes.c_int32
        k.GetNumberOfConsoleInputEvents.argtypes = k.GetConsoleMode.argtypes
        k.GetNumberOfConsoleInputEvents.restype = ctypes.c_int32
        k.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetConsoleMode.restype = ctypes.c_int32
        k.GetConsoleScreenBufferInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ScreenBufferInfo)]
        k.GetConsoleScreenBufferInfo.restype = ctypes.c_int32

    def handle(self, fd: int) -> int:
        if sys.platform != "win32":
            raise OSError("Windows console resources are unavailable on this host")
        import msvcrt

        return msvcrt.get_osfhandle(fd)

    def input_mode(self, handle: int) -> int:
        # GetConsoleMode alone also accepts output buffers. This read-only query
        # establishes readable input-buffer kind without consuming queued events.
        events = ctypes.c_uint32()
        if not self.kernel.GetNumberOfConsoleInputEvents(handle, ctypes.byref(events)):
            raise self._error()
        mode = ctypes.c_uint32()
        if not self.kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            raise self._error()
        return mode.value

    def set_input_mode(self, handle: int, mode: int) -> None:
        if not self.kernel.SetConsoleMode(handle, mode):
            raise self._error()

    def dimensions(self, handle: int) -> tuple[int, int]:
        info = _ScreenBufferInfo()
        if not self.kernel.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            raise self._error()
        window = info.window
        if not (0 <= window.left <= window.right < info.size.x and 0 <= window.top <= window.bottom < info.size.y):
            raise OSError("Console viewport geometry is invalid")
        return window.bottom - window.top + 1, window.right - window.left + 1

    @staticmethod
    def _error() -> OSError:
        if sys.platform != "win32":
            raise OSError("Windows console resources are unavailable on this host")
        # use_last_error=True preserves the call's code in ctypes' thread-local
        # copy. WinError() without it would query the restored OS error instead.
        return ctypes.WinError(ctypes.get_last_error())


@dataclass
class WindowsTerminal:
    """Explicit caller console endpoints, owned by one retained native worker."""

    _input_handle: int
    _output_handle: int
    _saved_mode: int
    _api: _ConsoleAPI
    _owner_thread: Thread
    _restore_needed: bool = False
    _release_errors: tuple[BaseException, ...] | None = field(default=None, init=False)

    @classmethod
    def acquire(cls, input_fd: int, output_fd: int) -> WindowsTerminal:
        """Admit native console endpoints and make keyboard input raw before launch.

        Preparation owns endpoint pairing and exclusive use. The caller retains
        this worker and the supplied descriptors through cleanup. Normal Python
        main-thread signals cannot interrupt native bookkeeping here; asynchronous
        thread injection and fatal process termination are outside this guarantee.
        """
        owner = current_thread()
        if owner is main_thread():
            raise RuntimeError("Terminal acquisition requires a retained non-main worker")
        api = _ConsoleAPI()
        input_handle, output_handle = api.handle(input_fd), api.handle(output_fd)
        saved_mode = api.input_mode(input_handle)
        api.dimensions(output_handle)
        terminal = cls(input_handle, output_handle, saved_mode, api, owner)
        raw_mode = saved_mode & ~(_ENABLE_PROCESSED_INPUT | _ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT)
        raw_mode |= _ENABLE_VIRTUAL_TERMINAL_INPUT
        try:
            # Mark before the syscall: failure may follow its native effect.
            terminal._restore_needed = True
            api.set_input_mode(input_handle, raw_mode)
            if api.input_mode(input_handle) != raw_mode:
                raise OSError("Console did not establish the required raw VT input mode")
        except BaseException as error:
            cleanup_errors = terminal.release()
            if cleanup_errors:
                cleanup = BaseExceptionGroup("Terminal acquisition cleanup failed", cleanup_errors)
                cleanup.__cause__ = error.__cause__
                cleanup.__context__ = error.__context__
                raise error from cleanup
            raise
        return terminal

    @property
    def input_handle(self) -> int:
        """Borrowed console input handle, usable only by the retained worker."""
        self._require_active()
        return self._input_handle

    @property
    def output_handle(self) -> int:
        """Borrowed output geometry handle; this resource never changes its modes."""
        self._require_active()
        return self._output_handle

    def dimensions(self) -> tuple[int, int]:
        """Query current viewport rows/columns from the explicit output handle."""
        self._require_active()
        return self._api.dimensions(self._output_handle)

    def release(self) -> tuple[BaseException, ...]:
        """After all borrowers stop, attempt exact input restoration once.

        Return every restoration error, including control-flow exceptions. Repeated
        release returns the same uncertainty without another native effect. Caller
        handles and descriptors remain borrowed even when restoration fails.
        """
        self._require_owner()
        if self._release_errors is not None:
            return self._release_errors
        errors: list[BaseException] = []
        if self._restore_needed:
            self._restore_needed = False
            try:
                self._api.set_input_mode(self._input_handle, self._saved_mode)
            except BaseException as error:
                errors.append(error)
        self._release_errors = tuple(errors)
        return self._release_errors

    def _require_owner(self) -> None:
        if current_thread() is not self._owner_thread:
            raise RuntimeError("Terminal access requires its retained native worker")

    def _require_active(self) -> None:
        self._require_owner()
        if self._release_errors is not None:
            raise RuntimeError("Terminal resource is released")
