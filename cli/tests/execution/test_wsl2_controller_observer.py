"""Exact Windows controller observation without process mutation."""

from __future__ import annotations

import ctypes
import gc
import os
import subprocess
import sys
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._wsl2_controller_observer import (
    ControllerIdentity,
    ControllerPresence,
    WindowsControllerObserver,
)
from agentworks.execution._wsl2_win32 import WindowsApi
from agentworks.execution.carrier import Deadline

IDENTITY = ControllerIdentity(42, 123456789)


class FakeApi:
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258

    def __init__(self) -> None:
        self.creation_ticks = IDENTITY.creation_ticks
        self.wait_result = self.WAIT_TIMEOUT
        self.snapshot_pids = [7, 9]
        self.fail: set[str] = set()
        self.calls: list[str] = []
        self.after_call: Callable[[str], None] = lambda _name: None
        self.index = 0

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise OSError(name)
        self.after_call(name)

    def open_process_for_observation(self, pid: int) -> int:
        assert pid == IDENTITY.pid
        self._call("open")
        return 11

    def process_creation_ticks(self, handle: int) -> int:
        assert handle == 11
        self._call("times")
        return self.creation_ticks

    def wait_process(self, handle: int, milliseconds: int) -> int:
        assert handle == 11 and milliseconds == 0
        self._call("wait")
        return self.wait_result

    def snapshot_processes(self) -> int:
        self._call("snapshot")
        return 12

    def first_snapshot_pid(self, handle: int) -> int | None:
        assert handle == 12
        self._call("first")
        self.index = 0
        return self.snapshot_pids[0] if self.snapshot_pids else None

    def next_snapshot_pid(self, handle: int) -> int | None:
        assert handle == 12
        self._call("next")
        self.index += 1
        return self.snapshot_pids[self.index] if self.index < len(self.snapshot_pids) else None

    def close_handle(self, handle: int) -> None:
        assert handle in (11, 12)
        self._call("close")


def observe(api: FakeApi, deadline: Deadline | None = None) -> ControllerPresence:
    observer = WindowsControllerObserver(api_factory=lambda: cast(WindowsApi, api))
    return observer.observe(IDENTITY, Deadline.after(1) if deadline is None else deadline)


@pytest.mark.parametrize(
    ("ticks", "wait", "expected"),
    [
        (IDENTITY.creation_ticks, FakeApi.WAIT_TIMEOUT, ControllerPresence.PRESENT),
        (IDENTITY.creation_ticks, FakeApi.WAIT_OBJECT_0, ControllerPresence.ABSENT_CONFIRMED),
        (IDENTITY.creation_ticks + 1, FakeApi.WAIT_TIMEOUT, ControllerPresence.ABSENT_CONFIRMED),
        (IDENTITY.creation_ticks, 0xFFFFFFFF, ControllerPresence.UNKNOWN),
        (IDENTITY.creation_ticks, 17, ControllerPresence.UNKNOWN),
    ],
)
def test_pinned_identity(ticks: int, wait: int, expected: ControllerPresence) -> None:
    api = FakeApi()
    api.creation_ticks = ticks
    api.wait_result = wait
    assert observe(api) == expected
    assert api.calls[-1] == "close"
    if ticks != IDENTITY.creation_ticks:
        assert "wait" not in api.calls


@pytest.mark.parametrize(
    ("pids", "expected"),
    [
        ([7, IDENTITY.pid, 9], ControllerPresence.UNKNOWN),
        ([7, 9], ControllerPresence.ABSENT_CONFIRMED),
        ([], ControllerPresence.UNKNOWN),
    ],
)
def test_failed_open_requires_complete_snapshot(pids: list[int], expected: ControllerPresence) -> None:
    api = FakeApi()
    api.fail.add("open")  # Includes access denied.
    api.snapshot_pids = pids
    assert observe(api) == expected
    assert api.calls[-1] == "close"


@pytest.mark.parametrize("failure", ["times", "wait", "close", "snapshot", "first", "next"])
def test_native_failure_remains_unknown(failure: str) -> None:
    api = FakeApi()
    if failure in {"snapshot", "first", "next"}:
        api.fail.add("open")
    api.fail.add(failure)
    assert observe(api) == ControllerPresence.UNKNOWN
    if failure not in {"snapshot", "close"}:
        assert api.calls[-1] == "close"


def test_deadline_at_entry_skips_native_calls() -> None:
    api = FakeApi()
    assert observe(api, Deadline.after(0)) == ControllerPresence.UNKNOWN
    assert api.calls == []


@pytest.mark.parametrize("late_call", ["times", "wait", "close", "snapshot", "next"])
def test_late_native_completion_cannot_prove_presence_or_absence(
    monkeypatch: pytest.MonkeyPatch, late_call: str
) -> None:
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    api = FakeApi()
    if late_call in {"snapshot", "next"}:
        api.fail.add("open")
    api.after_call = lambda name: now.__setitem__(0, 102.0) if name == late_call else None
    assert observe(api, Deadline.after(1)) == ControllerPresence.UNKNOWN
    if late_call != "snapshot":
        assert api.calls[-1] == "close"


def test_requires_finite_deadline_and_validated_identity() -> None:
    api = FakeApi()
    observer = WindowsControllerObserver(api_factory=lambda: cast(WindowsApi, api))
    with pytest.raises(ValidationError):
        observer.observe(IDENTITY, Deadline.after(None))
    with pytest.raises(ValidationError):
        observer.observe(cast(ControllerIdentity, (42, 123456789)), Deadline.after(1))
    assert api.calls == []


def test_observer_never_mutates_processes() -> None:
    api = FakeApi()
    assert observe(api) == ControllerPresence.PRESENT
    assert api.calls == ["open", "times", "wait", "close"]


@pytest.mark.parametrize(("last_error", "expected"), [(WindowsApi.ERROR_NO_MORE_FILES, None), (5, OSError)])
def test_native_snapshot_completion_requires_no_more_files(last_error: int, expected: object) -> None:
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32)]

    class CtypesStub:
        error = 0

        @staticmethod
        def sizeof(value: object) -> int:
            return ctypes.sizeof(cast(Entry, value))

        @staticmethod
        def byref(value: object) -> object:
            return value

        def set_last_error(self, code: int) -> None:
            self.error = code

        def get_last_error(self) -> int:
            return self.error

    stub = CtypesStub()

    def end(_snapshot: int, _entry: Entry) -> bool:
        stub.set_last_error(last_error)
        return False

    api = object.__new__(WindowsApi)
    api.ctypes = stub
    api.ProcessEntry32 = Entry
    api.kernel = SimpleNamespace(Process32FirstW=end, Process32NextW=end)
    if expected is OSError:
        with pytest.raises(OSError):
            api.next_snapshot_pid(12)
    else:
        assert api.next_snapshot_pid(12) is expected


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="requires native Windows process APIs")
def test_native_child_live_then_exited_without_touching_unrelated_process() -> None:
    api = WindowsApi()
    unrelated = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
    )
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
    )
    try:
        handle = api.open_process_for_observation(child.pid)
        try:
            identity = ControllerIdentity(child.pid, api.process_creation_ticks(handle))
        finally:
            api.close_handle(handle)
        observer = WindowsControllerObserver()
        assert observer.observe(identity, Deadline.after(2)) == ControllerPresence.PRESENT
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=3)
        assert observer.observe(identity, Deadline.after(2)) == ControllerPresence.ABSENT_CONFIRMED
        assert unrelated.poll() is None
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)
        if unrelated.stdin is not None:
            unrelated.stdin.close()
        unrelated.wait(timeout=3)


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="requires native Windows process APIs")
def test_native_toolhelp_fallback_observes_live_and_exited_pids(monkeypatch: pytest.MonkeyPatch) -> None:
    api = WindowsApi()
    native_open = api.open_process_for_observation

    def snapshot_pids() -> set[int]:
        snapshot = api.snapshot_processes()
        try:
            pids: set[int] = set()
            pid = api.first_snapshot_pid(snapshot)
            while pid is not None:
                pids.add(pid)
                pid = api.next_snapshot_pid(snapshot)
            assert api.ctypes.get_last_error() == api.ERROR_NO_MORE_FILES
            return pids
        finally:
            api.close_handle(snapshot)

    live = ControllerIdentity(*api.current_controller_identity())
    assert live.pid == os.getpid()
    assert live.pid in snapshot_pids()

    def exited_child_identity() -> ControllerIdentity:
        with subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
        ) as child:
            handle = native_open(child.pid)
            try:
                identity = ControllerIdentity(child.pid, api.process_creation_ticks(handle))
            finally:
                api.close_handle(handle)
            assert child.stdin is not None
            child.stdin.close()
            child.wait(timeout=3)
            return identity

    exited = exited_child_identity()
    gc.collect()  # Release Popen's process handle before the absence snapshot.
    assert exited.pid not in snapshot_pids()

    def denied_open(_pid: int) -> int:
        raise PermissionError("forced OpenProcess failure")

    monkeypatch.setattr(api, "open_process_for_observation", denied_open)
    observer = WindowsControllerObserver(api_factory=lambda: api)
    assert observer.observe(live, Deadline.after(2)) == ControllerPresence.UNKNOWN
    assert observer.observe(exited, Deadline.after(2)) == ControllerPresence.ABSENT_CONFIRMED
