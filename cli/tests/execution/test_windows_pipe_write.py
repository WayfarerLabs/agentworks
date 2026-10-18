"""Compare originating CRT and Win32 results on owned nonblocking stdin pipes."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = [pytest.mark.windows, pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows pipes")]

_CHUNK = bytes(range(256)) * 256
_LIMIT = 1024 * 1024
_CHILD = """
import os, pathlib, sys, time
release = pathlib.Path(sys.argv[1])
closed = sys.argv[2] == 'closed'
if closed:
    os.close(0)
os.write(1, b'R')
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
    print(
        f"interpreter={sys.implementation.name}-{sys.version.split()[0]} stage={stage} "
        f"class={type(error).__name__ if error is not None else 'success'} "
        f"errno={error.errno if error is not None else None} "
        f"winerror={getattr(error, 'winerror', None)} count={count}"
    )


def _write(api: str, fd: int, data: memoryview) -> int:
    if api == "crt":
        return os.write(fd, data)
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
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-c", _CHILD, str(release), "closed" if closed else "live"],
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
        assert _read(child.stdout.fileno(), 1, time.monotonic() + 10) == b"R"
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


@pytest.mark.parametrize("api", ["crt", "native"])
def test_closed_reader_reports_originating_error_while_child_is_alive(api: str, tmp_path: Path) -> None:
    with _child(tmp_path, closed=True) as (child, release):
        assert child.stdin is not None
        try:
            written = _write(api, child.stdin.fileno(), memoryview(_CHUNK))
        except OSError as error:
            _report(f"closed-{api}", 0, error)
            if api == "native":
                assert getattr(error, "winerror", None) in {109, 232}
            else:
                assert error.errno in {errno.EINVAL, errno.EPIPE}
        else:
            _report(f"closed-{api}", written)
            pytest.fail("closed reader accepted a write without an originating error")
        assert child.poll() is None
        release.touch()
        assert child.wait(timeout=10) == 0


@pytest.mark.parametrize("api", ["crt", "native"])
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


@pytest.mark.parametrize("api", ["crt", "native"])
def test_invalid_handle_is_distinct_from_consumer_closure(api: str) -> None:
    try:
        if api == "crt":
            os.write(-1, b"x")
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
