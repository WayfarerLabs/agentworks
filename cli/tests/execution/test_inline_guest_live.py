"""Framed helper boundary and incremental duplex behavior on owned Linux pipes."""

from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.execution import _inline_guest_io as guest_io
from agentworks.execution._evidence_wire import Frame, FrameKind, FrameReader
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._inline_bundle import FIXED_SOURCE
from agentworks.execution._inline_control import (
    ControlError,
    FailureCode,
    StreamEnd,
    StreamName,
    StreamRetention,
    encode_stream_end,
    parse_failure,
    parse_stream_end,
    parse_wait,
)
from agentworks.execution._inline_request import (
    REQUEST_HEADER_BYTES,
    REQUEST_MARKER,
    InlineManifest,
    InvocationKind,
    OutputMode,
    ScriptShell,
    decode_manifest,
    encode_manifest,
    encode_request_envelope,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the guest owns Linux pipes")
NONCE = "0123456789abcdef0123456789abcdef"


def _manifest(code: str, *, live: bool = False, deliver: bool = True) -> InlineManifest:
    return InlineManifest(
        NONCE,
        InvocationKind.COMMAND,
        ("/usr/bin/python3", "-I", "-S", "-B", "-c", code),
        None,
        None,
        (),
        None,
        None if live else b"",
        OutputMode.DELIVER if deliver else OutputMode.CAPTURE,
        0 if deliver else 4096,
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
    )


def _argv(runtime: str = sys.executable) -> list[str]:
    return [runtime, "-I", "-S", "-B", "-c", FIXED_SOURCE, NONCE]


def _frames(data: bytes) -> list[Frame]:
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)
    pending = memoryview(data)
    while pending:
        pending = pending[reader.try_write(pending) :]
    reader.finish()
    assert reader.error is None
    assert [frame.sequence for frame in frames] == list(range(len(frames)))
    return frames


def _wait_for_frame(process: subprocess.Popen[bytes], kind: FrameKind) -> bytes:
    assert process.stdout is not None
    output = bytearray()
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)
    expires = time.monotonic() + 5
    while time.monotonic() < expires:
        ready, _, _ = select.select([process.stdout], [], [], max(0, expires - time.monotonic()))
        assert ready, "helper did not produce incremental evidence"
        chunk = os.read(process.stdout.fileno(), 8192)
        assert chunk, "helper stopped before the requested evidence"
        output.extend(chunk)
        pending = memoryview(chunk)
        while pending:
            pending = pending[reader.try_write(pending) :]
        assert reader.error is None
        if any(frame.kind is kind for frame in frames):
            return bytes(output)
    raise AssertionError("helper did not produce incremental evidence")


@pytest.mark.parametrize("live", [False, True])
def test_envelope_keeps_json_codec_independent(live: bool) -> None:
    manifest = _manifest("pass", live=live)
    encoded = encode_manifest(manifest)
    envelope = encode_request_envelope(manifest)
    assert envelope[:5] == REQUEST_MARKER
    assert envelope[5:REQUEST_HEADER_BYTES] == format(len(encoded), "08x").encode("ascii")
    assert envelope[REQUEST_HEADER_BYTES:] == encoded
    assert decode_manifest(encoded) == manifest
    assert json.loads(encoded)["version"] == 2


@pytest.mark.parametrize("runtime", [sys.executable, "/usr/bin/python3.11"])
@pytest.mark.parametrize("deliver", [False, True])
def test_actual_live_helper_launches_and_delivers_before_input_eof(runtime: str, deliver: bool) -> None:
    if not Path(runtime).is_file():
        pytest.skip("compatibility interpreter unavailable")
    payload = bytes(range(256)) * 4
    code = (
        "import os;os.write(1,b'ready');data=b''\n"
        "while True:\n x=os.read(0,1024)\n if not x:break\n data+=x\nos.write(2,data)"
    )
    request = encode_request_envelope(_manifest(code, live=True, deliver=deliver))
    with subprocess.Popen(
        _argv(runtime), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child:
        assert child.stdin is not None
        try:
            # Split the header and JSON across writes to exercise exact framing reads.
            child.stdin.write(request[:3])
            child.stdin.flush()
            child.stdin.write(request[3:])
            child.stdin.flush()
            first = _wait_for_frame(child, FrameKind.STDOUT if deliver else FrameKind.LAUNCHING)
            assert child.poll() is None
            child.stdin.write(payload)
            child.stdin.close()
            child.stdin = None
            remaining, diagnostics = child.communicate(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    frames = _frames(first + remaining)
    assert diagnostics == b"" and child.returncode == 0
    assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDOUT) == b"ready"
    assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDERR) == payload
    ends = [parse_stream_end(frame.body) for frame in frames if frame.kind is FrameKind.STREAM_END]
    assert [end.count for end in ends] == [5, len(payload)]
    assert all(end.complete and not end.truncated for end in ends)
    assert all(end.retention is (StreamRetention.DELIVERED if deliver else StreamRetention.CAPTURED) for end in ends)
    assert parse_wait(next(frame.body for frame in frames if frame.kind is FrameKind.WAITED)).value == 0
    assert frames[-1].kind is FrameKind.FINISHED


@pytest.mark.parametrize("runtime", [sys.executable, "/usr/bin/python3.11"])
@pytest.mark.parametrize("deliver", [False, True])
@pytest.mark.parametrize("status", [0, 1, 255])
@pytest.mark.parametrize("prefix", [False, True], ids=["no-read", "prefix-consumer"])
def test_actual_helper_finishes_without_application_input_eof(
    runtime: str, deliver: bool, status: int, prefix: bool
) -> None:
    if not Path(runtime).is_file():
        pytest.skip("compatibility interpreter unavailable")
    code = "import os;" + ("assert os.read(0,1)==b'p';" if prefix else "")
    code += f"os.write(1,b'out\\x00\\xff');os.write(2,b'err\\xff\\x00');raise SystemExit({status})"
    request = encode_request_envelope(_manifest(code, live=True, deliver=deliver))
    with subprocess.Popen(
        _argv(runtime), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child:
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        try:
            child.stdin.write(request + (b"prefix-unread" if prefix else b""))
            child.stdin.flush()
            # The application and helper must finish while the supplier remains open.
            wire = _wait_for_frame(child, FrameKind.FINISHED)
            assert not child.stdin.closed
            assert child.wait(timeout=5) == 0
            wire += child.stdout.read()
            diagnostics = child.stderr.read()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    frames = _frames(wire)
    assert diagnostics == b"" and not any(frame.kind is FrameKind.FAILED for frame in frames)
    assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDOUT) == b"out\x00\xff"
    assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDERR) == b"err\xff\x00"
    ends = [parse_stream_end(frame.body) for frame in frames if frame.kind is FrameKind.STREAM_END]
    assert len(ends) == 2 and all(end.complete and not end.truncated for end in ends)
    assert parse_wait(next(frame.body for frame in frames if frame.kind is FrameKind.WAITED)).value == status
    assert frames[-1].kind is FrameKind.FINISHED


@pytest.mark.parametrize("runtime", [sys.executable, "/usr/bin/python3.11"])
def test_actual_helper_drains_output_after_consumer_closes_input(runtime: str, tmp_path: Path) -> None:
    if not Path(runtime).is_file():
        pytest.skip("compatibility interpreter unavailable")
    release = tmp_path / "release-output"
    code = (
        "import os,time;from pathlib import Path;os.close(0);os.write(1,b'closed');"
        f"p=Path({str(release)!r});expires=time.monotonic()+5\n"
        "while not p.exists():\n assert time.monotonic()<expires\n time.sleep(0.01)\n"
        "os.write(1,b'after-close');os.write(2,b'final-error');raise SystemExit(1)"
    )
    with subprocess.Popen(
        _argv(runtime), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child:
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        try:
            child.stdin.write(encode_request_envelope(_manifest(code, live=True)))
            child.stdin.flush()
            first = _wait_for_frame(child, FrameKind.STDOUT)
            assert child.poll() is None and not child.stdin.closed
            # Offer more bytes after the application closed fd 0.
            child.stdin.write(b"unread" * 1000)
            child.stdin.flush()
            release.touch()
            assert child.wait(timeout=5) == 0
            assert not child.stdin.closed
            wire, diagnostics = first + child.stdout.read(), child.stderr.read()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    frames = _frames(wire)
    assert diagnostics == b"" and not any(frame.kind is FrameKind.FAILED for frame in frames)
    assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDOUT) == b"closedafter-close"
    assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDERR) == b"final-error"
    assert parse_wait(next(frame.body for frame in frames if frame.kind is FrameKind.WAITED)).value == 1
    ends = [parse_stream_end(frame.body) for frame in frames if frame.kind is FrameKind.STREAM_END]
    assert len(ends) == 2 and all(end.complete and not end.truncated for end in ends)
    assert frames[-1].kind is FrameKind.FINISHED


@pytest.mark.parametrize("runtime", [sys.executable, "/usr/bin/python3.11"])
def test_actual_finite_input_live_output_has_no_capture_total_ceiling(runtime: str) -> None:
    if not Path(runtime).is_file():
        pytest.skip("compatibility interpreter unavailable")
    payload = bytes(range(256)) * 1024
    code = "import os;d=bytes(range(256))*1024;os.write(1,d);os.write(2,d[::-1])"
    result = subprocess.run(
        _argv(runtime), input=encode_request_envelope(_manifest(code)), capture_output=True, timeout=15
    )
    frames = _frames(result.stdout)
    assert result.returncode == 0 and result.stderr == b""
    for name, kind, expected in (
        (StreamName.STDOUT, FrameKind.STDOUT, payload),
        (StreamName.STDERR, FrameKind.STDERR, payload[::-1]),
    ):
        chunks = [frame.body for frame in frames if frame.kind is kind]
        assert all(0 < len(chunk) <= 4096 for chunk in chunks)
        assert b"".join(chunks) == expected
        end = next(
            end
            for frame in frames
            if frame.kind is FrameKind.STREAM_END and (end := parse_stream_end(frame.body)).stream is name
        )
        assert end.count == len(expected) and end.sha256 == hashlib.sha256(expected).hexdigest()
        assert end.retention is StreamRetention.DELIVERED and end.complete and not end.truncated


def test_live_script_source_stays_separate_from_application_input() -> None:
    manifest = replace(
        _manifest("pass", live=True),
        kind=InvocationKind.SCRIPT,
        argv=(),
        source=b"exec /bin/cat\n",
        shell=ScriptShell.SH,
    )
    payload = bytes(range(256)) * 8
    result = subprocess.run(_argv(), input=encode_request_envelope(manifest) + payload, capture_output=True, timeout=10)
    frames = _frames(result.stdout)
    assert result.returncode == 0 and result.stderr == b""
    assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDOUT) == payload
    assert not any(frame.kind is FrameKind.FAILED for frame in frames)


@pytest.mark.parametrize(
    "invalid",
    [
        b"",
        b"AGWI",
        b"other00000001x",
        REQUEST_MARKER + b"00000000",
        REQUEST_MARKER + b"00008001",
        REQUEST_MARKER + b"00000014{}",
        REQUEST_MARKER + b"00000002{}",
        REQUEST_MARKER + b"0000000A" + b"x" * 10,
        REQUEST_MARKER + b"00000+01x",
        REQUEST_MARKER + b"000000 1x",
    ],
)
def test_invalid_request_refuses_before_launch(invalid: bytes) -> None:
    result = subprocess.run(_argv(), input=invalid, capture_output=True, timeout=10)
    frames = _frames(result.stdout)
    assert result.returncode == 0 and result.stderr == b""
    assert [frame.kind for frame in frames] == [FrameKind.FAILED, FrameKind.FINISHED]


def test_finite_request_trailing_data_refuses_before_script_artifact(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    manifest = replace(
        _manifest("pass"),
        kind=InvocationKind.SCRIPT,
        argv=(),
        source=f"touch '{marker}'\n".encode(),
        shell=ScriptShell.SH,
    )
    result = subprocess.run(
        _argv(), input=encode_request_envelope(manifest) + b"trailing", capture_output=True, timeout=10
    )
    frames = _frames(result.stdout)
    assert [frame.kind for frame in frames] == [FrameKind.FAILED, FrameKind.FINISHED]
    assert parse_failure(frames[0].body).code is FailureCode.INVALID
    assert not marker.exists()


def test_stream_end_count_bounds_distinguish_delivery_from_capture() -> None:
    end = StreamEnd(StreamName.STDOUT, 2**63 - 1, "0" * 64, True, False, StreamRetention.DELIVERED)
    assert parse_stream_end(encode_stream_end(end)) == end
    for invalid in (
        replace(end, count=2**63),
        replace(end, count=-1),
        replace(end, count=True),
        replace(end, truncated=True, complete=False),
        replace(end, retention=StreamRetention.CAPTURED),
        replace(end, count=0, retention=StreamRetention.DISCARDED),
    ):
        with pytest.raises(ControlError):
            parse_stream_end(encode_stream_end(invalid))


def test_writer_short_writes_share_one_frame_and_acknowledge_only_complete_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = bytearray()

    def write(descriptor: int, data: bytes | memoryview) -> int:
        size = min(len(data), 100)
        wire.extend(data[:size])
        return size

    monkeypatch.setattr(os, "write", write)
    writer = guest_io.FrameWriter(NONCE)
    stdout = guest_io.StreamSink(writer, StreamName.STDOUT)
    stderr = guest_io.StreamSink(writer, StreamName.STDERR)
    payload = memoryview(b"x" * 5000)
    assert stdout.try_write(payload) is None
    assert stdout.count == 0
    assert stderr.try_write(memoryview(b"different")) is None
    with pytest.raises(OSError):
        writer.emit(FrameKind.FINISHED, b"{}")
    for _ in range(100):
        acknowledged = stdout.try_write(payload)
        if acknowledged is not None:
            break
    assert acknowledged == 4096 and stdout.count == 4096
    assert stdout.sha256 == hashlib.sha256(b"x" * 4096).hexdigest()
    writer.emit(FrameKind.FINISHED, b"{}")
    frames = _frames(bytes(wire))
    assert [frame.kind for frame in frames] == [FrameKind.STDOUT, FrameKind.FINISHED]
    assert frames[0].body == b"x" * 4096


def test_writer_finishes_partial_record_after_process_failure_without_false_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = bytearray()
    stalled = False

    def write(descriptor: int, data: bytes | memoryview) -> int:
        if stalled:
            raise BlockingIOError
        wire.extend(data[:1])
        return 1

    monkeypatch.setattr(os, "write", write)
    writer = guest_io.FrameWriter(NONCE)
    stdout = guest_io.StreamSink(writer, StreamName.STDOUT)
    stderr = guest_io.StreamSink(writer, StreamName.STDERR)
    assert stdout.try_write(memoryview(b"output")) is None
    stalled = True
    assert stdout.try_write(memoryview(b"output")) is None
    assert stdout.count == 0
    stalled = False
    monkeypatch.setattr(select, "select", lambda *args: ([], [1], []))
    writer.finish_pending(stdout, stderr)
    assert stdout.count == 6 and stderr.count == 0
    writer.emit(FrameKind.FINISHED, b"{}")
    assert [frame.kind for frame in _frames(bytes(wire))] == [FrameKind.STDOUT, FrameKind.FINISHED]


def test_writer_abandons_unfinishable_frame_without_counting_or_following_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = bytearray()

    def write(descriptor: int, data: bytes | memoryview) -> int:
        wire.extend(data[:1])
        return 1

    monkeypatch.setattr(os, "write", write)
    writer = guest_io.FrameWriter(NONCE)
    stdout = guest_io.StreamSink(writer, StreamName.STDOUT)
    stderr = guest_io.StreamSink(writer, StreamName.STDERR)
    assert stdout.try_write(memoryview(b"not-acknowledged")) is None

    def stalled(descriptor: int, data: bytes | memoryview) -> int:
        raise BlockingIOError

    monkeypatch.setattr(os, "write", stalled)
    times = iter([0.0, 1.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(times))
    before = bytes(wire)
    with pytest.raises(OSError):
        writer.finish_pending(stdout, stderr)
    with pytest.raises(OSError):
        writer.emit(FrameKind.FINISHED, b"{}")
    assert bytes(wire) == before and stdout.count == stderr.count == 0


def test_nonblocking_owned_pipe_restores_all_flags_after_failure() -> None:
    import fcntl

    read_fd, write_fd = os.pipe()
    try:
        original = fcntl.fcntl(read_fd, fcntl.F_GETFL)
        with pytest.raises(RuntimeError), guest_io.nonblocking_pipe(read_fd):
            assert fcntl.fcntl(read_fd, fcntl.F_GETFL) == original | os.O_NONBLOCK
            raise RuntimeError
        assert fcntl.fcntl(read_fd, fcntl.F_GETFL) == original
    finally:
        os.close(read_fd)
        os.close(write_fd)
