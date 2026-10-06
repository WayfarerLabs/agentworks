"""One owned hidden console proves native resource and record-poll primitives.

On Windows, run this file with ``-rP`` to display passing comparison output.
Private record measurements require that report; a captured green run is not evidence.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.execution.carriers.ssh import _terminal_windows as terminal
from tests.execution.carriers.ssh import windows_console_probe as probe

if TYPE_CHECKING:
    from collections.abc import Callable


def test_windows_record_abi_preserves_utf16_units_on_any_host() -> None:
    import struct

    # The external Win32 INPUT_RECORD ABI has a 16-bit kind, alignment padding
    # and a 16-byte event union. W input stores UTF-16 units, not host wchar_t.
    for unit in (0x0041, 0x03A9, 0xD83D, 0xDE03):
        record = probe._key_record(unit)
        expected = struct.pack("<H2xiHHHHI", 1, 1, 1, 0, 0, unit, 0)
        assert bytes(record) == expected
        assert record.event.key.character == unit
        assert ctypes.sizeof(record) == 20


@pytest.mark.parametrize("started", [False, True])
def test_failed_worker_start_cancels_even_a_late_tail(monkeypatch: pytest.MonkeyPatch, started: bool) -> None:
    effect = Event()
    interruption = KeyboardInterrupt()
    threads: list[tuple[Thread, Callable[[], None]]] = []

    def make_thread(*, target: Callable[[], None]) -> Thread:
        thread = Thread(target=target)
        start = thread.start

        def fail_start() -> None:
            if started:
                start()
            raise interruption

        monkeypatch.setattr(thread, "start", fail_start)
        threads.append((thread, start))
        return thread

    monkeypatch.setattr(probe, "Thread", make_thread)
    with pytest.raises(BaseExceptionGroup) as raised:
        probe._FixtureWorker(effect.set).run()
    assert raised.value.exceptions == (interruption,)
    thread, start = threads[0]
    # A failed start can publish a real native tail after caller cleanup returns.
    # Exercise that schedule too: its cancelled admission must remain inert.
    if not started:
        start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not effect.is_set()


def test_interrupted_admitted_wait_settles_borrower_before_outer_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    interruption = KeyboardInterrupt()
    interrupted, settling, active, release = Event(), Event(), Event(), Event()
    borrower_cleanup, outer_cleanup = object(), object()
    order: list[object] = []
    errors: list[BaseException] = []

    class InterruptedCompletion(Event):
        def wait(self, timeout: float | None = None) -> bool:
            if not interrupted.is_set():
                interrupted.set()
                raise interruption
            settling.set()
            return super().wait(timeout)

    def borrow() -> None:
        try:
            active.set()
            assert release.wait(timeout=5)
        finally:
            order.append(borrower_cleanup)

    monkeypatch.setattr(probe, "Event", InterruptedCompletion)
    attempt = probe._FixtureWorker(borrow)

    def supervise() -> None:
        try:
            attempt.run()
        except BaseException as error:
            errors.append(error)
        finally:
            order.append(outer_cleanup)

    supervisor = Thread(target=supervise)
    supervisor.start()
    try:
        assert interrupted.wait(timeout=2)
        assert settling.wait(timeout=2)
        assert active.wait(timeout=2)
        assert order == []
    finally:
        release.set()
        supervisor.join(timeout=2)
    assert not supervisor.is_alive()
    assert order == [borrower_cleanup, outer_cleanup]
    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, BaseExceptionGroup)
    assert error.exceptions == (interruption,)


def test_failed_borrower_finishes_before_outer_cleanup() -> None:
    failure = OSError()
    borrower_cleanup, outer_cleanup = object(), object()
    order: list[object] = []

    def borrow() -> None:
        try:
            raise failure
        finally:
            order.append(borrower_cleanup)

    with pytest.raises(BaseExceptionGroup) as raised:
        try:
            probe._FixtureWorker(borrow).run()
        finally:
            order.append(outer_cleanup)
    assert raised.value.exceptions == (failure,)
    assert order == [borrower_cleanup, outer_cleanup]


@pytest.mark.parametrize("observer_failed", [False, True])
def test_failing_owned_child_exposes_controlled_evidence_after_reaping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, observer_failed: bool
) -> None:
    # Every process/window boundary is synthetic; invoke the actual supervisor
    # and failure reporting without launching any child or accessing a console.
    identity = {"phase": "identity", "pid": 47201, "window": 9181, "window_pid": 49001}
    stdout = (json.dumps(identity) + "\n").encode()
    stderr = b"Traceback (synthetic child):\nOSError: 731 native-fixture-evidence\n"
    observation_error = OSError()
    timeouts: list[float] = []

    class Child:
        pid = 47201
        returncode = 1

        def __init__(self) -> None:
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
            timeouts.append(timeout)
            return stdout, stderr

        def poll(self) -> int:
            return self.returncode

    child = Child()
    monkeypatch.setattr(
        os, "environ", {"__PYVENV_LAUNCHER__": "prior-synthetic-launcher", "FIXTURE_VALUE": "synthetic-value"}
    )
    parent_env = os.environ.copy()
    expected = probe._interpreter_identity()

    def create(*args: object, **kwargs: object) -> Child:
        command = args[0]
        assert isinstance(command, list)
        assert command[:3] == [expected["base_executable"], "-I", "-u"]
        assert command[3] == str(Path(probe.__file__).resolve())
        assert json.loads(command[4]) == expected
        assert kwargs["env"] == parent_env | {"__PYVENV_LAUNCHER__": "synthetic"}
        assert kwargs["creationflags"] == 16
        assert kwargs["close_fds"] is True
        return child

    def observe(value: dict[str, object]) -> str:
        assert value == identity
        assert child.stdout.closed and child.stderr.closed
        if observer_failed:
            raise observation_error
        return "unobservable"

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32", executable="synthetic"))
    monkeypatch.setattr(
        module,
        "subprocess",
        SimpleNamespace(
            STARTUPINFO=lambda: SimpleNamespace(dwFlags=0, wShowWindow=0),
            STARTF_USESHOWWINDOW=1,
            SW_HIDE=0,
            Popen=create,
            DEVNULL=-3,
            PIPE=-1,
            CREATE_NEW_CONSOLE=16,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    monkeypatch.setattr(module, "_window_cleanup", observe)
    with pytest.raises((AssertionError, BaseExceptionGroup)) as raised:
        test_owned_console_resource_and_nowait_records(tmp_path)
    assert timeouts == [120, 10]
    assert os.environ == parent_env
    assert child.stdout.closed and child.stderr.closed
    assert (tmp_path / "stdout.jsonl").read_bytes() == stdout
    assert (tmp_path / "stderr.log").read_bytes() == stderr
    parent = (tmp_path / "parent.json").read_text(encoding="utf-8")
    measurement = json.loads(parent)
    assert measurement["child_pid"] == child.pid
    assert measurement["returncode"] == 1
    assert measurement["window_cleanup"] == "unobservable"
    assert measurement["records"] == [identity]
    assert measurement["expected_interpreter"] == expected
    if observer_failed:
        error = raised.value
        assert isinstance(error, BaseExceptionGroup)
        inner = error.exceptions[0]
        assert isinstance(inner, BaseExceptionGroup)
        assert inner.exceptions == (observation_error,)
        assert measurement["parent_error_types"] == ["OSError"]
    else:
        assert isinstance(raised.value, AssertionError)
        assert measurement["parent_error_types"] == []
    notes = "\n".join(raised.value.__notes__)
    assert stdout.decode() in notes
    assert stderr.decode() in notes
    assert parent in notes


@pytest.mark.parametrize(
    "mismatch", ["prefix", "resource_file", "resource_sha256", "outside_prefix", "isolated", "console_pid", None]
)
def test_child_admits_candidate_identity_before_descriptor_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mismatch: str | None
) -> None:
    # Native boundaries are entirely fake. Failed identity or isolation must
    # detach the owned console without opening descriptors or admitting a worker.
    prefix = tmp_path / "candidate-venv"
    installed = prefix / "site-packages" / "resource.py"
    identity = {
        "prefix": str(prefix),
        "resource_file": str(installed),
        "installed_resource_file": str(installed),
        "resource_sha256": "candidate-digest",
    }
    expected = identity.copy()
    expected["resource_file"] = str(tmp_path / "checkout" / "resource.py")
    if mismatch in ("prefix", "resource_sha256"):
        expected[mismatch] = "different-candidate"
    elif mismatch == "resource_file":
        identity["resource_file"] = str(tmp_path / "checkout" / "resource.py")
    elif mismatch == "outside_prefix":
        identity["resource_file"] = identity["installed_resource_file"] = str(tmp_path / "other" / "resource.py")
        expected["installed_resource_file"] = identity["installed_resource_file"]
    flags = SimpleNamespace(isolated=int(mismatch != "isolated"), ignore_environment=1, no_user_site=1)
    module = sys.modules[probe.__name__]
    monkeypatch.setattr(
        module, "sys", SimpleNamespace(platform="win32", flags=flags, argv=["probe", json.dumps(expected)])
    )
    monkeypatch.setattr(probe, "_interpreter_identity", lambda: identity)
    records: list[dict[str, object]] = []
    monkeypatch.setattr(probe, "_emit", records.append)
    order: list[str] = []
    descriptor_boundary = OSError()

    def open_descriptor(*args: object) -> int:
        order.append("open")
        raise descriptor_boundary

    class Native:
        def __init__(self) -> None:
            self.kernel = SimpleNamespace(
                GetConsoleWindow=lambda: 0 if "detach" in order else 9181,
                GetConsoleProcessList=self.processes,
                FreeConsole=self.detach,
            )
            self.user = SimpleNamespace(GetWindowThreadProcessId=self.window_pid, IsWindowVisible=lambda window: False)

        def check(self, value: object) -> None:
            assert value

        def window_pid(self, window: int, pid: object) -> int:
            pointer = ctypes.cast(pid, ctypes.POINTER(ctypes.c_uint32))
            pointer.contents.value = os.getpid()
            return 1

        def processes(self, processes: object, count: int) -> int:
            pointer = ctypes.cast(processes, ctypes.POINTER(ctypes.c_uint32))
            pointer[0] = os.getpid() + int(mismatch == "console_pid")
            return 1

        def detach(self) -> int:
            order.append("detach")
            return 1

    monkeypatch.setattr(probe, "_Native", Native)
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    monkeypatch.setattr(os, "open", open_descriptor)
    with pytest.raises(BaseExceptionGroup) as raised:
        probe.main()
    assert records[0]["interpreter"] == identity
    assert records[1]["closed_descriptors"] == []
    assert order == (["open", "detach"] if mismatch is None else ["detach"])
    if mismatch is None:
        assert raised.value.exceptions == (descriptor_boundary,)
    else:
        assert len(raised.value.exceptions) == 1
        assert isinstance(raised.value.exceptions[0], AssertionError)


def test_resource_observation_hashes_exact_source_bytes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    resource = tmp_path / "resource.py"
    resource.write_bytes(b"synthetic-resource\r\n")
    monkeypatch.setattr(terminal, "__file__", str(resource))
    original = probe._interpreter_identity()
    assert original["resource_file"] == os.path.normcase(str(resource.resolve()))
    assert original["resource_sha256"] == hashlib.sha256(resource.read_bytes()).hexdigest()
    resource.write_bytes(b"synthetic-resource\n")
    changed = probe._interpreter_identity()
    assert changed["resource_sha256"] == hashlib.sha256(resource.read_bytes()).hexdigest()
    assert changed["resource_sha256"] != original["resource_sha256"]


def _window_cleanup(identity: dict[str, object]) -> str:
    if sys.platform != "win32":
        raise OSError("Native window observation requires Windows")
    window, expected_pid = identity.get("window"), identity.get("window_pid")
    if not isinstance(window, int) or not isinstance(expected_pid, int):
        return "unobservable"
    if not window or not expected_pid:
        return "unobservable"
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindow.argtypes, user.IsWindow.restype = [ctypes.c_void_p], ctypes.c_int32
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user.GetWindowThreadProcessId.restype = ctypes.c_uint32
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if not user.IsWindow(window):
            return "observed_absent"
        current_pid = ctypes.c_uint32()
        if not user.GetWindowThreadProcessId(window, ctypes.byref(current_pid)):
            if not user.IsWindow(window):
                return "observed_absent"
            raise ctypes.WinError(ctypes.get_last_error())
        if current_pid.value != expected_pid:
            return "observed_identity_changed"
        sleep(0.05)
    return "observed_remaining"


def _run_owned_child(logs: Path) -> dict[str, object]:
    if sys.platform != "win32":
        raise OSError("Owned native console fixture requires Windows")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    identity = probe._interpreter_identity()
    # CPython multiprocessing uses this launch shape to bypass the Windows venv
    # redirector while retaining its venv identity, even with isolated startup.
    env = os.environ.copy()
    env["__PYVENV_LAUNCHER__"] = sys.executable
    process = subprocess.Popen(
        [identity["base_executable"], "-I", "-u", str(Path(probe.__file__).resolve()), json.dumps(identity)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        close_fds=True,
        env=env,
    )
    stdout = stderr = b""
    errors: list[BaseException] = []
    try:
        stdout, stderr = process.communicate(timeout=120)
    except subprocess.TimeoutExpired as error:
        stdout, stderr = error.output or b"", error.stderr or b""
        errors.append(error)
    except BaseException as error:
        errors.append(error)
    finally:
        try:
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode is not None
        except BaseException as error:
            errors.append(error)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except BaseException as error:
                    errors.append(error)
        (logs / "stdout.jsonl").write_bytes(stdout)
        (logs / "stderr.log").write_bytes(stderr)
    records = []
    cleanup = "unobservable"
    try:
        records = [json.loads(line) for line in stdout.splitlines()]
        identities = [record for record in records if record.get("phase") == "identity"]
        if len(identities) == 1:
            cleanup = _window_cleanup(identities[0])
    except BaseException as error:
        errors.append(error)
    result: dict[str, object] = {
        "child_pid": process.pid,
        "returncode": process.returncode,
        "window_cleanup": cleanup,
        "records": records,
        "expected_interpreter": identity,
        "parent_error_types": [type(error).__name__ for error in errors],
    }
    (logs / "parent.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if errors:
        raise BaseExceptionGroup("Owned native console fixture failed", errors)
    return result


def _failure_observations(logs: Path) -> str:
    """Render only bounded, controlled logs from this fixture's owned child."""
    observations = []
    limit = 64 * 1024
    for name in ("stdout.jsonl", "stderr.log", "parent.json"):
        path = logs / name
        try:
            with path.open("rb") as stream:
                data = stream.read(limit + 1)
        except OSError as error:
            observations.append(f"{path}: unavailable ({type(error).__name__})")
            continue
        text = data[:limit].decode("utf-8", errors="backslashreplace")
        observations.append(f"{path}:\n{text}")
        if len(data) > limit:
            observations.append(f"Display truncated at {limit} bytes; full owned log remains at {path}.")
    return "\n".join(observations)


def _owned_console_case(tmp_path: Path) -> None:
    # Retain the parent supervisor before CreateProcess. Main-thread interruption
    # cannot orphan a constructor or skip child reaping. Its finite process timeout
    # bounds stalled native calls; no other console host is scanned or terminated.
    results: list[dict[str, object]] = []

    def work() -> None:
        results.append(_run_owned_child(tmp_path))

    probe._FixtureWorker(work).run()
    assert len(results) == 1
    result = results[0]
    assert result["returncode"] == 0
    assert result["parent_error_types"] == []
    assert result["window_cleanup"] in ("observed_absent", "observed_identity_changed")
    records = result["records"]
    assert isinstance(records, list)
    identity, *observations, measurements, cleanup = records
    assert len(observations) == 48 and all(value["phase"] == "input_comparison" for value in observations)
    assert identity["pid"] == result["child_pid"]
    assert identity["console_pids"] == [result["child_pid"]]
    probe._check_candidate_identity(identity["interpreter"], result["expected_interpreter"])
    assert identity["isolated"] == identity["ignore_environment"] == identity["no_user_site"] == 1
    assert identity["window"] and identity["window_pid"]
    assert identity["window_visible"] is False
    assert cleanup["error_types"] == []
    assert len(cleanup["closed_descriptors"]) == 2
    assert len(measurements["cases"]) == 2
    for case in measurements["cases"]:
        assert case["after"] == case["before"]
        assert case["empty_count"] == case["after_non_key_count"] == 0
        assert 4 in case["non_key_types"] and 8 in case["non_key_types"]
        assert case["unicode_units"] == [0x0041, 0x03A9, 0xD83D, 0xDE03]
    print(json.dumps({"phase": "injected_input_comparison_report", "observations": observations}), flush=True)


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Requires an owned native Windows console")
def test_owned_console_resource_and_nowait_records(tmp_path: Path) -> None:
    try:
        _owned_console_case(tmp_path)
    except BaseException as error:
        error.add_note(_failure_observations(tmp_path))
        raise
