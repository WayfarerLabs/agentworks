"""Synthetic Win32 boundary tests; no caller console, SSH or native acceptance."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from functools import wraps
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.execution.carriers.ssh import _terminal_windows as module
from agentworks.execution.carriers.ssh._terminal_windows import WindowsTerminal

pytestmark = pytest.mark.windows

_INPUT_FD, _OUTPUT_FD = 17, 23
_INPUT_HANDLE, _OUTPUT_HANDLE = 0x100000011, 0x100000017


def _native_worker[**P](test: Callable[P, None]) -> Callable[P, None]:
    """Retain one worker until explicit settlement; an outer test timeout bounds it."""

    @wraps(test)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        errors: list[BaseException] = []
        done = Event()

        def work() -> None:
            try:
                test(*args, **kwargs)
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        worker = Thread(target=work)
        worker.start()
        done.wait()
        worker.join()
        if errors:
            raise errors[0]

    return run


class _FakeConsole:
    """A synthetic native boundary; unsupported operations do not exist here."""

    def __init__(self) -> None:
        self.mode = 0x00FF
        self.size = (31, 97)
        self.calls: list[tuple[str, int, int | None]] = []
        self.fail: dict[str, BaseException] = {}
        self.omit_vt = False

    def handle(self, fd: int) -> int:
        self.calls.append(("handle", fd, None))
        if "handle" in self.fail:
            raise self.fail["handle"]
        return {_INPUT_FD: _INPUT_HANDLE, _OUTPUT_FD: _OUTPUT_HANDLE}[fd]

    def input_mode(self, handle: int) -> int:
        self.calls.append(("input_mode", handle, None))
        if "input_mode" in self.fail:
            raise self.fail["input_mode"]
        if handle != _INPUT_HANDLE:
            raise OSError(6, "Not a readable input buffer")
        return self.mode

    def dimensions(self, handle: int) -> tuple[int, int]:
        self.calls.append(("dimensions", handle, None))
        if "dimensions" in self.fail:
            raise self.fail["dimensions"]
        if handle != _OUTPUT_HANDLE:
            raise OSError(6, "Not an output buffer")
        return self.size

    def set_input_mode(self, handle: int, mode: int) -> None:
        self.calls.append(("set", handle, mode))
        assert handle == _INPUT_HANDLE
        if "set" in self.fail:
            raise self.fail["set"]
        self.mode = mode & ~0x0200 if self.omit_vt else mode


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> _FakeConsole:
    api = _FakeConsole()
    monkeypatch.setattr(module, "_ConsoleAPI", lambda: api)
    return api


@pytest.mark.parametrize("original", [0x0007, 0x00FF, 0x02B8, 0x0000])
@_native_worker
def test_early_raw_mode_exact_restore_and_explicit_handles(console: _FakeConsole, original: int) -> None:
    console.mode = original
    terminal = WindowsTerminal.acquire(_INPUT_FD, _OUTPUT_FD)
    assert console.mode == (original & ~0x0007) | 0x0200
    assert terminal.input_handle == _INPUT_HANDLE
    assert terminal.output_handle == _OUTPUT_HANDLE
    assert terminal.dimensions() == (31, 97)
    console.size = (42, 113)
    assert terminal.dimensions() == (42, 113)
    assert terminal.release() == ()
    assert console.mode == original
    effects = list(console.calls)
    assert terminal.release() == ()
    assert console.calls == effects
    assert [call for call in effects if call[0] == "set"] == [
        ("set", _INPUT_HANDLE, (original & ~0x0007) | 0x0200),
        ("set", _INPUT_HANDLE, original),
    ]
    for access in (lambda: terminal.input_handle, lambda: terminal.output_handle, terminal.dimensions):
        with pytest.raises(RuntimeError):
            access()
    assert console.calls == effects


def test_main_thread_refusal_has_no_native_effect(console: _FakeConsole) -> None:
    with pytest.raises(RuntimeError):
        WindowsTerminal.acquire(_INPUT_FD, _OUTPUT_FD)
    assert console.calls == []


@_native_worker
def test_wrong_worker_refuses_access_and_release_without_effect(console: _FakeConsole) -> None:
    terminal = WindowsTerminal.acquire(_INPUT_FD, _OUTPUT_FD)
    before = list(console.calls)
    failures: list[BaseException] = []

    def wrong_worker() -> None:
        for access in (
            lambda: terminal.input_handle,
            lambda: terminal.output_handle,
            terminal.dimensions,
            terminal.release,
        ):
            try:
                access()
            except BaseException as error:
                failures.append(error)

    worker = Thread(target=wrong_worker)
    worker.start()
    worker.join()
    assert len(failures) == 4
    assert all(isinstance(error, RuntimeError) for error in failures)
    assert console.calls == before
    assert terminal.release() == ()


@pytest.mark.parametrize("stage", ["handle", "input_mode", "dimensions"])
@_native_worker
def test_admission_failure_precedes_mode_effect(console: _FakeConsole, stage: str) -> None:
    error = OSError(6, "Native query refused")
    console.fail[stage] = error
    original = console.mode
    with pytest.raises(OSError) as caught:
        WindowsTerminal.acquire(_INPUT_FD, _OUTPUT_FD)
    assert caught.value is error
    assert console.mode == original
    assert not any(call[0] == "set" for call in console.calls)


@pytest.mark.parametrize("fds", [(_OUTPUT_FD, _OUTPUT_FD), (_INPUT_FD, _INPUT_FD)])
@_native_worker
def test_input_and_output_kind_are_admitted_independently(console: _FakeConsole, fds: tuple[int, int]) -> None:
    with pytest.raises(OSError):
        WindowsTerminal.acquire(*fds)
    assert not any(call[0] == "set" for call in console.calls)


@pytest.mark.parametrize("error", [OSError(87, "VT mode refused"), KeyboardInterrupt(), SystemExit(17)])
@pytest.mark.parametrize("native_effect", [False, True])
@_native_worker
def test_mode_failure_restores_and_preserves_primary(
    console: _FakeConsole, error: BaseException, native_effect: bool
) -> None:
    original = console.mode
    native_set = console.set_input_mode

    def fail_raw(handle: int, mode: int) -> None:
        if mode & 0x0200:
            if native_effect:
                native_set(handle, mode)
            else:
                console.calls.append(("set", handle, mode))
            raise error
        native_set(handle, mode)

    console.set_input_mode = fail_raw  # type: ignore[method-assign]
    with pytest.raises(type(error)) as caught:
        WindowsTerminal.acquire(_INPUT_FD, _OUTPUT_FD)
    assert caught.value is error
    assert console.mode == original
    assert [call[2] for call in console.calls if call[0] == "set"] == [(original & ~7) | 0x0200, original]


@_native_worker
def test_verification_query_failure_restores_input(console: _FakeConsole) -> None:
    original = console.mode
    native_set = console.set_input_mode
    error = OSError(6, "Verification query failed")

    def set_then_lose_query(handle: int, mode: int) -> None:
        native_set(handle, mode)
        console.fail["input_mode"] = error

    console.set_input_mode = set_then_lose_query  # type: ignore[method-assign]
    with pytest.raises(OSError) as caught:
        WindowsTerminal.acquire(_INPUT_FD, _OUTPUT_FD)
    assert caught.value is error
    assert console.mode == original


@_native_worker
def test_silent_vt_omission_is_refused_and_restored(console: _FakeConsole) -> None:
    original = console.mode
    console.omit_vt = True
    with pytest.raises(OSError):
        WindowsTerminal.acquire(_INPUT_FD, _OUTPUT_FD)
    assert console.mode == original
    assert [call[2] for call in console.calls if call[0] == "set"] == [(original & ~7) | 0x0200, original]


@pytest.mark.parametrize("error", [OSError(5, "Restore refused"), KeyboardInterrupt(), SystemExit(9)])
@_native_worker
def test_release_records_uncertainty_once_and_closes_no_borrower(console: _FakeConsole, error: BaseException) -> None:
    terminal = WindowsTerminal.acquire(_INPUT_FD, _OUTPUT_FD)
    console.fail["set"] = error
    evidence = terminal.release()
    assert evidence == (error,)
    effects = list(console.calls)
    assert terminal.release() is evidence
    for access in (lambda: terminal.input_handle, lambda: terminal.output_handle, terminal.dimensions):
        with pytest.raises(RuntimeError):
            access()
    assert console.calls == effects
    assert console.mode & 0x0200


@_native_worker
def test_failed_acquisition_cleanup_preserves_prior_exception_chain(console: _FakeConsole) -> None:
    primary = KeyboardInterrupt()
    prior_cause = ValueError("Prior cause")
    prior_context = LookupError("Prior context")
    primary.__cause__, primary.__context__ = prior_cause, prior_context
    cleanup_error = OSError(5, "Restore failed")
    native_set = console.set_input_mode

    def fail_after_effect(handle: int, mode: int) -> None:
        native_set(handle, mode)
        if mode & 0x0200:
            console.fail["set"] = cleanup_error
            raise primary

    console.set_input_mode = fail_after_effect  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt) as caught:
        WindowsTerminal.acquire(_INPUT_FD, _OUTPUT_FD)
    assert caught.value is primary
    cleanup = primary.__cause__
    assert isinstance(cleanup, BaseExceptionGroup)
    assert cleanup.exceptions == (cleanup_error,)
    assert cleanup.__cause__ is prior_cause
    assert cleanup.__context__ is prior_context
    assert primary.__context__ is prior_context


class _NativeCall:
    def __init__(self, effect: Callable[..., int]) -> None:
        self.effect = effect
        self.argtypes: list[Any] = []
        self.restype: Any = None

    def __call__(self, *args: Any) -> int:
        return self.effect(*args)


class _Kernel:
    """Synthetic C-call boundary exercising the real pointer-writing wrappers."""

    def __init__(self) -> None:
        self.mode = 0x00FF
        self.events = 7
        self.fail: str | None = None
        self.window = (11, 17, 107, 47)
        self.calls: list[tuple[str, int]] = []
        self.GetConsoleMode = _NativeCall(self.mode_query)
        self.GetNumberOfConsoleInputEvents = _NativeCall(self.events_query)
        self.SetConsoleMode = _NativeCall(self.mode_set)
        self.GetConsoleScreenBufferInfo = _NativeCall(self.geometry)

    def mode_query(self, handle: int, pointer: Any) -> int:
        self.calls.append(("mode", handle))
        if self.fail == "mode":
            return 0
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = self.mode
        return 1

    def events_query(self, handle: int, pointer: Any) -> int:
        self.calls.append(("events", handle))
        if self.fail == "events" or handle != _INPUT_HANDLE:
            return 0
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = self.events
        return 1

    def mode_set(self, handle: int, mode: int) -> int:
        self.calls.append(("set", handle))
        if self.fail == "set":
            return 0
        self.mode = mode
        return 1

    def geometry(self, handle: int, pointer: Any) -> int:
        self.calls.append(("geometry", handle))
        if self.fail == "geometry" or handle != _OUTPUT_HANDLE:
            return 0
        info = ctypes.cast(pointer, ctypes.POINTER(module._ScreenBufferInfo))[0]
        info.size = module._Coord(300, 500)
        info.window = module._SmallRect(*self.window)
        info.maximum = module._Coord(400, 600)
        return 1


@pytest.fixture
def native_boundary(monkeypatch: pytest.MonkeyPatch) -> tuple[module._ConsoleAPI, _Kernel]:
    kernel = _Kernel()
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 87, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "Native refusal"), raising=False)
    return module._ConsoleAPI(), kernel


def test_real_wrappers_read_only_admission_viewport_and_mode_write(
    native_boundary: tuple[module._ConsoleAPI, _Kernel],
) -> None:
    api, kernel = native_boundary
    assert api.input_mode(_INPUT_HANDLE) == 0x00FF
    assert kernel.events == 7
    assert api.dimensions(_OUTPUT_HANDLE) == (31, 97)
    api.set_input_mode(_INPUT_HANDLE, 0x02F8)
    assert api.input_mode(_INPUT_HANDLE) == 0x02F8
    assert kernel.events == 7
    assert kernel.calls == [
        ("events", _INPUT_HANDLE),
        ("mode", _INPUT_HANDLE),
        ("geometry", _OUTPUT_HANDLE),
        ("set", _INPUT_HANDLE),
        ("events", _INPUT_HANDLE),
        ("mode", _INPUT_HANDLE),
    ]


def test_mode_query_alone_cannot_admit_output_as_input(native_boundary: tuple[module._ConsoleAPI, _Kernel]) -> None:
    api, kernel = native_boundary
    with pytest.raises(OSError):
        api.input_mode(_OUTPUT_HANDLE)
    assert kernel.calls == [("events", _OUTPUT_HANDLE)]


@pytest.mark.parametrize("failure", ["events", "mode", "set", "geometry"])
def test_native_false_return_propagates_without_fallback(
    native_boundary: tuple[module._ConsoleAPI, _Kernel], failure: str
) -> None:
    api, kernel = native_boundary
    kernel.fail = failure
    with pytest.raises(OSError) as caught:
        if failure == "set":
            api.set_input_mode(_INPUT_HANDLE, 0x0200)
        elif failure == "geometry":
            api.dimensions(_OUTPUT_HANDLE)
        else:
            api.input_mode(_INPUT_HANDLE)
    assert caught.value.errno == 87
    assert kernel.mode == 0x00FF


@pytest.mark.parametrize("window", [(-1, 0, 10, 10), (0, -1, 10, 10), (2, 0, 1, 10), (0, 2, 10, 1), (0, 0, 300, 500)])
def test_external_viewport_invalidity_refuses(
    native_boundary: tuple[module._ConsoleAPI, _Kernel], window: tuple[int, int, int, int]
) -> None:
    api, kernel = native_boundary
    kernel.window = window
    with pytest.raises(OSError):
        api.dimensions(_OUTPUT_HANDLE)


def test_crt_translation_uses_supplied_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: calls.append(fd) or 123))
    api = object.__new__(module._ConsoleAPI)
    assert api.handle(37) == 123
    assert calls == [37]


def test_non_windows_binding_refuses_without_loading_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(OSError):
        module._ConsoleAPI()
