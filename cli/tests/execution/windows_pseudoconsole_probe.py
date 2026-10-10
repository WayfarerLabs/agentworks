"""Opt-in native topology measurement, without SSH or production enablement.

Run with an outer tester timeout. Synchronous native calls can remain pending;
the retained non-main worker keeps its cell in that case, never closes on main.
Only counts, hashes and fixed fixture observations are reported, not raw bytes.
"""

from __future__ import annotations

import binascii
import ctypes
import hashlib
import json
import sys
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any

from agentworks.execution._terminal_handoff import _payload
from agentworks.execution._windows_pseudoconsole import WindowsPseudoConsole

NONCE = "0123456789ABCDEF0123456789ABCDEF"
PAYLOAD_READY = b"AGW-TERMINAL/2:" + NONCE.encode() + b":P"
INTERACTIVE_READY = b"AGW-TERMINAL/2:" + NONCE.encode() + b":I"


class _Rect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int16) for name in ("left", "top", "right", "bottom")]


class _Coord(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int16), ("y", ctypes.c_int16)]


class _Screen(ctypes.Structure):
    _fields_ = [
        ("size", _Coord),
        ("cursor", _Coord),
        ("attributes", ctypes.c_uint16),
        ("window", _Rect),
        ("maximum", _Coord),
    ]


class _IO:
    """Probe-local synchronous I/O on explicitly borrowed handles."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Native Windows probe only")
        self.kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        pointer, dword = ctypes.c_void_p, ctypes.c_uint32
        bindings: tuple[tuple[str, list[Any], Any], ...] = (
            ("PeekNamedPipe", [pointer, pointer, dword, pointer, pointer, pointer], ctypes.c_int32),
            ("ReadFile", [pointer, pointer, dword, pointer, pointer], ctypes.c_int32),
            ("WriteFile", [pointer, pointer, dword, pointer, pointer], ctypes.c_int32),
            ("GetStdHandle", [dword], pointer),
            ("GetFileType", [pointer], dword),
            ("GetConsoleMode", [pointer, pointer], ctypes.c_int32),
            ("SetConsoleMode", [pointer, dword], ctypes.c_int32),
            ("GetConsoleScreenBufferInfo", [pointer, pointer], ctypes.c_int32),
        )
        for name, arguments, result in bindings:
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def check(result: int) -> None:
        if not result:
            raise OSError("Probe native operation failed")

    def write(self, handle: int, data: bytes) -> int:
        written = ctypes.c_uint32()
        self.check(self.kernel.WriteFile(handle, data, len(data), ctypes.byref(written), None))
        if not 0 < written.value <= len(data):
            raise OSError("Probe write made no progress")
        return written.value

    def read(self, handle: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_uint32()
        self.check(self.kernel.ReadFile(handle, buffer, size, ctypes.byref(count), None))
        return buffer.raw[: count.value]

    def available(self, handle: int) -> bytes | None:
        count = ctypes.c_uint32()
        if not self.kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(count), None):
            if ctypes.get_last_error() == 109:  # type: ignore[attr-defined]
                return b""
            raise OSError("Probe peek failed")
        return self.read(handle, min(count.value, 4096)) if count.value else None

    def dimensions(self, handle: int) -> tuple[int, int] | None:
        screen = _Screen()
        if not self.kernel.GetConsoleScreenBufferInfo(handle, ctypes.byref(screen)):
            return None
        return screen.window.bottom - screen.window.top + 1, screen.window.right - screen.window.left + 1


def _frame() -> bytes:
    argv, env = (b"/bin/true", b"a\xff\n", b""), {b"FIXTURE": b"v\xff\n"}
    overhead = len(_payload(argv, env, b"")) // 2
    length = 32768 - overhead
    source = (bytes(range(256)) * ((length + 255) // 256))[:length]
    return _payload(argv, env, source)


def _child(idle: bool) -> int:
    io = _IO()
    handles = [int(io.kernel.GetStdHandle(code) or 0) for code in (-10, -11, -12)]
    modes = []
    for handle in handles:
        mode = ctypes.c_uint32()
        modes.append(mode.value if io.kernel.GetConsoleMode(handle, ctypes.byref(mode)) else None)
    metadata = {
        "kinds": [int(io.kernel.GetFileType(handle)) for handle in handles],
        "modes": modes,
        "initial": io.dimensions(handles[1]),
    }
    io.write(handles[2], json.dumps(metadata).encode() + b"\n")
    io.write(handles[2], bytes(range(256)))
    if idle:
        time.sleep(60)
        return 0
    if modes[0] is None or modes[1] is None:
        return 2  # Honest topology refusal, no CONIN$/CONOUT$ fallback.
    io.check(io.kernel.SetConsoleMode(handles[0], modes[0] & ~7))
    try:
        io.write(handles[1], PAYLOAD_READY)
        wire = bytearray()
        while len(wire) < 65536:
            chunk = io.read(handles[0], min(4096, 65536 - len(wire)))
            if not chunk:
                return 3
            wire.extend(chunk)
        decoded = binascii.unhexlify(wire)
        expected = binascii.unhexlify(_frame())
        result = {
            "received": len(wire),
            "decoded": len(decoded),
            "fidelity": decoded == expected,
            "sha256": hashlib.sha256(decoded).hexdigest(),
            "changed": io.dimensions(handles[1]),
        }
        io.write(handles[2], json.dumps(result).encode() + b"\n")
        io.write(handles[1], INTERACTIVE_READY)
        io.write(handles[1], b"WRAP-FIXTURE:" + b"X" * 80)
        return 0 if result["fidelity"] else 4
    finally:
        io.check(io.kernel.SetConsoleMode(handles[0], modes[0]))


def _measure(cell: WindowsPseudoConsole, idle: bool) -> dict[str, Any]:
    output, stderr = bytearray(), bytearray()
    report: dict[str, Any] = {"idle": idle, "accepted": False, "stage": "create"}
    try:
        io = _IO()
        cell.create()
        pipes = cell.pipes
        report["stage"] = "observe"
        end, offset, resized = time.monotonic() + 15, 0, False
        frame = _frame()
        while time.monotonic() < end:
            for handle, buffer in ((pipes.presentation, output), (pipes.stderr, stderr)):
                chunk = io.available(handle)
                if chunk:
                    buffer.extend(chunk)
                if len(buffer) > 131072:
                    raise OSError("Probe output bound exceeded")
            if idle and b"\n" in stderr and len(stderr.split(b"\n", 1)[1]) >= 256:
                break
            if not idle and PAYLOAD_READY in output:
                if not resized:
                    cell.resize(9, 17)
                    resized = True
                if offset < len(frame):
                    offset += io.write(pipes.input, frame[offset : offset + 256])
            if cell.poll() is not None:
                # Final finite drain still uses only the borrowed endpoints.
                for handle, buffer in ((pipes.presentation, output), (pipes.stderr, stderr)):
                    while chunk := io.available(handle):
                        buffer.extend(chunk)
                        if len(buffer) > 131072:
                            raise OSError("Probe output bound exceeded")
                break
            time.sleep(0.001)
        first, separator, rest = stderr.partition(b"\n")
        metadata = json.loads(first) if separator else {}
        diagnostic_fidelity = rest[:256] == bytes(range(256))
        result = json.loads(rest[256:].split(b"\n", 1)[0]) if len(rest) > 256 else {}
        report.update(
            metadata=metadata,
            result=result,
            stderr_all_octets=diagnostic_fidelity,
            sent=offset,
            presentation_bytes=len(output),
            presentation_sha256=hashlib.sha256(output).hexdigest(),
            payload_marker_exact=PAYLOAD_READY in output,
            interactive_marker_exact=INTERACTIVE_READY in output,
            payload_not_echoed=frame[:64] not in output,
            wrap_fixture_exact=b"WRAP-FIXTURE:" + b"X" * 80 in output,
            status=cell.poll(),
        )
        topology = metadata.get("kinds") == [2, 2, 3] and metadata.get("initial") == [24, 80]
        report["accepted"] = (
            topology
            and diagnostic_fidelity
            and (
                idle
                or (
                    result.get("fidelity") is True
                    and result.get("received") == 65536
                    and result.get("decoded") == 32768
                    and result.get("changed") == [9, 17]
                    and report["payload_marker_exact"]
                    and report["interactive_marker_exact"]
                    and report["payload_not_echoed"]
                    and report["status"] == 0
                )
            )
        )
        report["stage"] = "measured"
    except (OSError, ValueError, RuntimeError) as error:
        report.update(stage="refused", error=type(error).__name__)
    finally:
        report["capabilities_before_close"] = cell.has_capabilities
        report["acquisition_uncertain"] = cell._acquisition_uncertain
        report["closed"] = cell.close()
        report["capabilities_after_close"] = cell.has_capabilities
        report["cleanup_status"] = cell._exit_status
        report["accepted"] = report["accepted"] and report["closed"] and not cell.has_capabilities
    return report


def run_probe() -> dict[str, Any]:
    """Retain both cells before workers run; pending work is explicitly unproved."""
    if sys.platform != "win32":
        return {"accepted": False, "stage": "non_windows"}
    observations: list[dict[str, Any]] = []
    for idle in (False, True):
        argv = (sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child-idle" if idle else "--child")
        cell = WindowsPseudoConsole(argv, 24, 80)

        def work(c: WindowsPseudoConsole = cell, i: bool = idle) -> None:
            try:
                observation = _measure(c, i)
            except BaseException as error:
                observation = {"accepted": False, "stage": "worker_escape", "error": type(error).__name__}
            observations.append(observation)
            if c.has_capabilities:
                # No guessed close/replay: outer tester supervision owns disposition.
                Event().wait()

        worker = Thread(target=work, daemon=True)
        worker.start()
        worker.join(30)
        if worker.is_alive():
            return {"accepted": False, "stage": "retained_worker_pending", "observations": observations}
    return {
        "accepted": len(observations) == 2 and all(item["accepted"] for item in observations),
        "observations": observations,
        "windows_build": sys.getwindowsversion().build,
    }  # type: ignore[attr-defined]


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("--child", "--child-idle"):
        raise SystemExit(_child(sys.argv[1] == "--child-idle"))
    result = run_probe()
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["accepted"] else 1)
