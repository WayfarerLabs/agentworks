"""Compare originating CRT and Win32 results on owned nonblocking stdin pipes."""

from __future__ import annotations

import errno
import os
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

from agentworks.execution import _process as core

pytestmark = [pytest.mark.windows, pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows pipes")]

_CHUNK = bytes(range(256)) * 256
_LIMIT = 1024 * 1024
_CHILD = """
import _winapi, msvcrt, os, pathlib, struct, sys, time
release = pathlib.Path(sys.argv[1])
closed = sys.argv[2] == 'closed'
reader = msvcrt.get_osfhandle(0)
assert reader == _winapi.GetStdHandle(_winapi.STD_INPUT_HANDLE)
assert _winapi.GetFileType(reader) == _winapi.FILE_TYPE_PIPE
if closed:
    os.close(0)
    try:
        _winapi.GetFileType(reader)
    except OSError as error:
        assert error.winerror == 6
    else:
        raise SystemExit(76)
os.write(1, struct.pack('<Qc', os.getpid(), b'C' if closed else b'L'))
deadline = time.monotonic() + 20
while not release.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(73)
    time.sleep(0.01)
if not closed:
    data = bytearray()
    while True:
        chunk = os.read(0, 65536)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 1048576:
            raise SystemExit(74)
    view = memoryview(data)
    while view:
        written = os.write(1, view)
        if written <= 0:
            raise SystemExit(75)
        view = view[written:]
"""


def _report(stage: str, count: int, error: OSError | None = None) -> None:
    observation = (
        f"interpreter={sys.implementation.name}-{sys.version.split()[0]} stage={stage} "
        f"class={type(error).__name__ if error is not None else 'success'} "
        f"errno={error.errno if error is not None else None} "
        f"winerror={getattr(error, 'winerror', None)} count={count}"
    )
    print(observation)


def _write(api: str, fd: int, data: memoryview) -> int:
    if api == "crt":
        return os.write(fd, data)
    if api == "owned":
        return core._write_owned_stdin(fd, data)
    if sys.platform != "win32":
        pytest.fail("native write reached on a non-Windows host")
    # These imports must only execute after the non-Windows test skip.
    import _winapi
    import msvcrt

    written, status = _winapi.WriteFile(msvcrt.get_osfhandle(fd), data)
    assert status == 0
    return written


def _read(fd: int, size: int, deadline: float) -> bytes:
    while time.monotonic() < deadline:
        try:
            data = os.read(fd, size)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        return data
    pytest.fail("owned child output deadline expired")


@contextmanager
def _child(tmp_path: Path, *, closed: bool) -> Iterator[tuple[subprocess.Popen[bytes], Path]]:
    release = tmp_path / "release"
    # A Windows venv redirector retains inherited pipe readers while waiting for
    # its real interpreter. Use that interpreter directly and verify its PID.
    interpreter = cast("str", vars(sys)["_base_executable"])
    print(f"stage=interpreter executable={sys.executable!r} base_executable={interpreter!r}")
    child = subprocess.Popen(
        [interpreter, "-I", "-S", "-B", "-c", _CHILD, str(release), "closed" if closed else "live"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        cwd=tmp_path,
        env={"SYSTEMROOT": os.environ["SYSTEMROOT"]},
        close_fds=True,
    )
    try:
        assert child.stdin is not None and child.stdout is not None
        os.set_blocking(child.stdin.fileno(), False)
        os.set_blocking(child.stdout.fileno(), False)
        acknowledgement = bytearray()
        deadline = time.monotonic() + 10
        while len(acknowledgement) < 9:
            chunk = _read(child.stdout.fileno(), 9 - len(acknowledgement), deadline)
            assert chunk
            acknowledgement.extend(chunk)
        pid, state = struct.unpack("<Qc", acknowledgement)
        print(f"stage=child-identity owned_pid={child.pid} reported_pid={pid} state={state!r}")
        assert pid == child.pid
        assert state == (b"C" if closed else b"L")
        assert child.poll() is None
        yield child, release
    finally:
        terminated = child.poll() is None
        try:
            if terminated:
                child.kill()
            child.wait(timeout=5)
        finally:
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()
        print(
            f"stage=cleanup reaped={child.poll() is not None} terminated={terminated} "
            f"closed={all(pipe is not None and pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))}"
        )


@pytest.mark.parametrize("api", ["crt", "native", "owned"])
def test_closed_reader_reports_originating_error_while_child_is_alive(api: str, tmp_path: Path) -> None:
    with _child(tmp_path, closed=True) as (child, release):
        assert child.stdin is not None
        try:
            written = _write(api, child.stdin.fileno(), memoryview(_CHUNK))
        except OSError as error:
            _report(f"closed-{api}", 0, error)
            if api in {"native", "owned"}:
                assert isinstance(error, BrokenPipeError) and error.errno == errno.EPIPE
                assert getattr(error, "winerror", None) in {109, 232}
            else:
                assert error.errno in {errno.EINVAL, errno.EPIPE}
        else:
            _report(f"closed-{api}", written)
            pytest.fail("closed reader accepted a write without an originating error")
        assert child.poll() is None
        release.touch()
        assert child.wait(timeout=10) == 0


@pytest.mark.parametrize("api", ["crt", "native", "owned"])
def test_live_reader_pressure_then_byte_exact_progress(api: str, tmp_path: Path) -> None:
    with _child(tmp_path, closed=False) as (child, release):
        assert child.stdin is not None and child.stdout is not None
        expected = bytearray()
        deadline = time.monotonic() + 10
        for _ in range(256):
            assert time.monotonic() < deadline
            assert len(expected) + len(_CHUNK) <= _LIMIT
            try:
                written = _write(api, child.stdin.fileno(), memoryview(_CHUNK))
            except OSError as error:
                _report(f"pressure-{api}", len(expected), error)
                assert isinstance(error, BlockingIOError) and error.errno in {errno.EAGAIN, errno.EWOULDBLOCK}
                assert getattr(error, "winerror", None) not in {109, 232}
                break
            assert 0 <= written <= len(_CHUNK)
            expected.extend(_CHUNK[:written])
            if written == 0:
                assert api != "owned"
                _report(f"pressure-{api}-zero-write", written)
                break
        else:
            pytest.fail("owned pipe never reached pressure within the write bound")
        assert child.poll() is None
        before_release = len(expected)
        _report(f"accepted-before-release-{api}", before_release)
        release.touch()
        # The child drains after release. Each successful retry contributes only
        # the bytes it reports, including partial native writes.
        tail = memoryview(b"after-release\x00\xff")
        deadline = time.monotonic() + 10
        while tail and time.monotonic() < deadline:
            try:
                written = _write(api, child.stdin.fileno(), tail)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError as error:
                _report(f"resume-{api}", len(expected), error)
                raise
            assert 0 <= written <= len(tail)
            expected.extend(tail[:written])
            tail = tail[written:]
            if written == 0:
                time.sleep(0.01)
        assert not tail
        assert len(expected) > before_release
        child.stdin.close()
        received = bytearray()
        while True:
            chunk = _read(child.stdout.fileno(), 65_536, deadline)
            if not chunk:
                break
            received.extend(chunk)
            assert len(received) <= len(expected)
        assert bytes(received) == bytes(expected)
        assert child.wait(timeout=5) == 0
        _report(f"resumed-{api}", len(received))


@pytest.mark.parametrize("api", ["crt", "native", "owned"])
def test_invalid_handle_is_distinct_from_consumer_closure(api: str) -> None:
    try:
        if api == "crt":
            os.write(-1, b"x")
        elif api == "owned":
            core._write_owned_stdin(-1, memoryview(b"x"))
        elif sys.platform == "win32":
            import _winapi

            _winapi.WriteFile(0, memoryview(b"x"))
        else:
            pytest.fail("native write reached on a non-Windows host")
    except OSError as error:
        _report(f"invalid-{api}", 0, error)
        assert error.errno == errno.EBADF
        assert getattr(error, "winerror", None) == (6 if api == "native" else None)
    else:
        pytest.fail("invalid handle accepted a write")


@pytest.mark.parametrize("capacity", [0, 1, 4096])
def test_owned_writer_shrinks_only_unaccepted_prefixes(capacity: int, monkeypatch: pytest.MonkeyPatch) -> None:
    import _winapi
    import msvcrt

    offers: list[bytes] = []
    accepted = bytearray()

    def write(handle: int, data: memoryview) -> tuple[int, int]:
        assert handle == 42
        offers.append(bytes(data))
        assert bytes(data) == _CHUNK[: len(data)]
        if len(data) > capacity:
            return 0, 0
        written = min(7, len(data))
        accepted.extend(data[:written])
        return written, 0

    monkeypatch.setattr(msvcrt, "get_osfhandle", lambda fd: 42)
    monkeypatch.setattr(_winapi, "WriteFile", write)
    if capacity:
        assert core._write_owned_stdin(123, memoryview(_CHUNK)) == len(accepted) == min(7, capacity)
        assert bytes(accepted) == _CHUNK[: len(accepted)]
    else:
        with pytest.raises(BlockingIOError) as caught:
            core._write_owned_stdin(123, memoryview(_CHUNK))
        assert caught.value.errno == errno.EAGAIN and not accepted
    assert 1 <= len(offers) <= 17
    assert len(offers[0]) == len(_CHUNK)
    assert all(len(right) < len(left) for left, right in zip(offers, offers[1:], strict=False))
    assert len(offers[-1]) == max(1, capacity)


@pytest.mark.parametrize("number", [errno.EIO, errno.EINVAL, errno.EBADF, errno.EPIPE])
@pytest.mark.parametrize("boundary", ["handle", "write"])
def test_owned_writer_preserves_originating_error(number: int, boundary: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import _winapi
    import msvcrt

    original = OSError(number, "injected native fault")
    offers = 0

    def fail(*args: object) -> int:
        raise original

    def write(handle: int, data: memoryview) -> tuple[int, int]:
        nonlocal offers
        offers += 1
        if offers == 1:
            return 0, 0
        raise original

    monkeypatch.setattr(msvcrt, "get_osfhandle", fail if boundary == "handle" else lambda fd: 42)
    monkeypatch.setattr(_winapi, "WriteFile", write)
    with pytest.raises(OSError) as caught:
        core._write_owned_stdin(123, memoryview(_CHUNK))
    assert caught.value is original
    assert offers == (0 if boundary == "handle" else 2)
