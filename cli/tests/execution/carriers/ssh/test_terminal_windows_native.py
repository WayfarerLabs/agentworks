"""One owned hidden console proves native resource and record-poll primitives."""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from tests.execution.carriers.ssh import windows_console_probe as probe


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
    process = subprocess.Popen(
        [sys.executable, "-I", "-u", str(Path(probe.__file__).resolve())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        close_fds=True,
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
    records = [json.loads(line) for line in stdout.splitlines()]
    identities = [record for record in records if record.get("phase") == "identity"]
    cleanup = _window_cleanup(identities[0]) if len(identities) == 1 else "unobservable"
    result: dict[str, object] = {
        "child_pid": process.pid,
        "returncode": process.returncode,
        "window_cleanup": cleanup,
        "records": records,
        "parent_error_types": [type(error).__name__ for error in errors],
    }
    (logs / "parent.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if errors:
        raise BaseExceptionGroup("Owned native console fixture failed", errors)
    return result


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Requires an owned native Windows console")
def test_owned_console_resource_and_nowait_records(tmp_path: Path) -> None:
    # Retain the parent supervisor before CreateProcess. Main-thread interruption
    # cannot orphan a constructor or skip child reaping. Its finite process timeout
    # bounds stalled native calls; no other console host is scanned or terminated.
    done = Event()
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            results.append(_run_owned_child(tmp_path))
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    worker = Thread(target=work)
    worker.start()
    while not done.is_set():
        try:
            done.wait()
        except BaseException as error:
            errors.append(error)
    worker.join()
    if errors:
        raise BaseExceptionGroup("Owned console supervisor failed", errors)
    assert len(results) == 1
    result = results[0]
    assert result["returncode"] == 0
    assert result["parent_error_types"] == []
    assert result["window_cleanup"] in ("observed_absent", "observed_identity_changed")
    records = result["records"]
    assert isinstance(records, list)
    identity, measurements, cleanup = records
    assert identity["pid"] == result["child_pid"]
    assert identity["console_pids"] == [result["child_pid"]]
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
