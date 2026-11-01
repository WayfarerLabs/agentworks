"""The pump, driven against synthetic children that speak the marker protocol.

The pump never learns what it is driving, so a Python child that writes the
guest's markers stands in for ssh here: no network, credentials or guest.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import envelope, pump, ssh_process
from agentworks.execution.errors import ClientUnsupported
from agentworks.execution.pump import Capture, Deadline, PipeSession, StopReason
from agentworks.execution.results import Completed, Unknown, UnknownReason

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.windows

NONCE = "cd" * 16

PRELUDE = f"""
import os, sys, time
P = b"\\x1e" + {NONCE!r}.encode() + b" "
out, err = sys.stdout.buffer, sys.stderr.buffer
def start():
    out.write(P + b"start\\n"); out.flush(); err.write(P + b"start\\n"); err.flush()
def finish(status=0):
    out.write(P + b"end\\n"); out.flush(); err.write(P + b"exit %d\\n" % status); err.flush()
"""


class Run:
    def __init__(self, script: str, *, stdin: bytes | None = None, seconds: float | None = 10) -> None:
        self.out, self.err = Capture(), Capture()
        self.session = PipeSession(
            [sys.executable, "-c", PRELUDE + script],
            request=b"",
            nonce=NONCE,
            stdin=None if stdin is None else io.BytesIO(stdin),
            on_stdout=self.out,
            on_stderr=self.err,
            deadline=Deadline.after(seconds),
        )

    def go(self) -> StopReason:
        with self.session as session:
            reason = session.pump()
            session.end()
        return reason

    @property
    def completion(self) -> object:
        return envelope.classify(self.session.stdout, self.session.stderr, self.session.exit_status)

    def assert_closed(self) -> None:
        process = self.session._proc  # noqa: SLF001 - the point is that nothing is left behind
        assert process is not None
        assert process.poll() is not None
        assert all(pipe is None or pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


@pytest.mark.parametrize("status", [0, 1, 23, 255])
def test_the_guests_status_comes_from_the_markers_not_from_the_client(status: int) -> None:
    run = Run(f"start(); out.write(b'\\x00\\xff\\r\\n\\n'); err.write(b'\\x80err\\n'); finish({status}); sys.exit(1)")

    assert run.go() is StopReason.DONE

    assert run.completion == Completed(status)
    assert bytes(run.out.data) == b"\x00\xff\r\n\n"
    assert bytes(run.err.data) == b"\x80err\n"
    run.assert_closed()


def test_duplex_pressure_delivers_all_input_once() -> None:
    data = bytes(range(256)) * 8192
    run = Run(
        "import hashlib\n"
        "start()\n"
        "out.write(b'o'*200000); out.flush(); err.write(b'e'*200000); err.flush()\n"
        "data = sys.stdin.buffer.read()\n"
        "out.write(hashlib.sha256(data).hexdigest().encode()); finish()\n",
        stdin=data,
    )
    run.go()

    assert bytes(run.out.data) == b"o" * 200_000 + hashlib.sha256(data).hexdigest().encode()
    assert bytes(run.err.data) == b"e" * 200_000
    assert run.completion == Completed(0)
    run.assert_closed()


@pytest.mark.parametrize("with_stdin", [False, True])
def test_a_child_reading_input_sees_end_of_file_when_there_is_none(with_stdin: bool) -> None:
    run = Run(
        "start(); out.write(b'[' + sys.stdin.buffer.read() + b']'); finish()",
        stdin=b"" if with_stdin else None,
    )

    run.go()

    assert bytes(run.out.data) == b"[]"
    run.assert_closed()


def test_the_request_precedes_the_callers_input_and_ends_where_the_caller_begins() -> None:
    out, err = Capture(), Capture()
    script = PRELUDE + "start(); out.write(sys.stdin.buffer.read(5) + b'|' + sys.stdin.buffer.read()); finish()"
    session = PipeSession(
        [sys.executable, "-c", script],
        request=b"REQ--",
        nonce=NONCE,
        stdin=io.BytesIO(b"caller-bytes"),
        on_stdout=out,
        on_stderr=err,
    )
    with session:
        session.pump()
        session.end()

    assert bytes(out.data) == b"REQ--|caller-bytes"


@pytest.mark.parametrize(("count", "cap"), [(0, 0), (8, 8), (9, 8), (100_000, 8)])
def test_a_capture_limit_stops_the_session_and_keeps_only_the_limit(count: int, cap: int) -> None:
    run = Run(f"start(); out.write(b'o'*{count}); out.flush(); finish()")
    run.out = Capture(cap)
    run.session._sinks["out"] = run.out  # noqa: SLF001 - swap the sink for a small limit

    reason = run.go()

    assert bytes(run.out.data) == b"o" * min(count, cap)
    assert reason is (StopReason.OUTPUT_LIMIT if count > cap else StopReason.DONE)
    run.assert_closed()


def test_short_writes_advance_without_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:7]))
    data = b"binary\x00\xff\n" * 50
    run = Run("start(); out.write(sys.stdin.buffer.read()); finish()", stdin=data)

    run.go()

    assert bytes(run.out.data) == data
    run.assert_closed()


def test_a_failed_write_is_the_consumer_ending_early_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(fd: int, data: bytes) -> int:
        raise BrokenPipeError("secret-canary")

    monkeypatch.setattr(os, "write", refuse)
    run = Run("start(); out.write(b'still ran'); finish(4)", stdin=b"x" * 10)

    reason = run.go()

    assert reason is StopReason.DONE
    assert run.completion == Completed(4)
    assert bytes(run.out.data) == b"still ran"
    run.assert_closed()


def test_a_sink_that_fails_is_dropped_and_the_command_still_completes() -> None:
    seen: list[bytes] = []

    def sink(payload: bytes) -> None:
        seen.append(payload)
        raise BrokenPipeError

    script = "start(); out.write(b'a'); out.flush(); time.sleep(.2); out.write(b'b'); finish(7)"
    session = PipeSession(
        [sys.executable, "-c", PRELUDE + script],
        request=b"",
        nonce=NONCE,
        on_stdout=sink,
        on_stderr=Capture(),
    )
    with session:
        assert session.pump() is StopReason.DONE
        session.end()

    assert seen == [b"a"]
    assert envelope.classify(session.stdout, session.stderr, session.exit_status) == Completed(7)


def test_a_sink_writing_to_a_pipe_whose_reader_closed_is_the_consumer_leaving() -> None:
    # CPython raises BrokenPipeError for this on POSIX and OSError(EINVAL) on Windows; either
    # means the consumer ended early, so the command still completes.
    reader, writer = os.pipe()
    os.close(reader)
    delivered: list[int] = []

    def sink(payload: bytes) -> None:
        delivered.append(os.write(writer, payload))

    script = "start(); out.write(b'a'); out.flush(); time.sleep(.2); out.write(b'b'); finish(7)"
    session = PipeSession(
        [sys.executable, "-c", PRELUDE + script], request=b"", nonce=NONCE, on_stdout=sink, on_stderr=Capture()
    )
    try:
        with session:
            assert session.pump() is StopReason.DONE
            session.end()
    finally:
        os.close(writer)

    assert delivered == []
    assert envelope.classify(session.stdout, session.stderr, session.exit_status) == Completed(7)


def test_a_deadline_keeps_partial_output_and_reaps_the_client() -> None:
    run = Run("start(); out.write(b'partial'); out.flush(); time.sleep(30)", seconds=0.5)

    started = time.monotonic()
    reason = run.go()

    assert time.monotonic() - started < 3
    assert reason is StopReason.DEADLINE
    assert bytes(run.out.data) == b"partial"
    assert run.session.exit_status is None
    run.assert_closed()


def test_a_deadline_does_not_wait_for_the_client_to_exit_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pump, "_NATURAL_EXIT_SECONDS", 60.0)
    run = Run("start(); out.write(b'partial'); out.flush(); time.sleep(30)", seconds=0.3)

    started = time.monotonic()
    run.go()

    assert time.monotonic() - started < 10
    run.assert_closed()


def test_an_output_limit_does_not_wait_for_the_client_to_exit_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pump, "_NATURAL_EXIT_SECONDS", 60.0)
    run = Run("start(); out.write(b'o' * 100); out.flush(); time.sleep(30)")
    run.session._sinks["out"] = Capture(8)  # noqa: SLF001 - swap the sink for a small limit

    started = time.monotonic()
    reason = run.go()

    assert reason is StopReason.OUTPUT_LIMIT
    assert time.monotonic() - started < 10
    run.assert_closed()


class _FullPipeSink:
    """A sink standing in for a non-blocking pipe: it takes a few bytes at a time, and refuses in between."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.refusals = 0
        self._calls = 0

    def __call__(self, payload: bytes) -> None:
        self._calls += 1
        if self._calls % 3 == 0:
            self.refusals += 1
            raise BlockingIOError(11, "would block", 0)
        if len(payload) > 5:
            self.data += payload[:5]
            self.refusals += 1
            raise BlockingIOError(11, "would block", 5)
        self.data += payload


def test_a_sink_that_would_block_is_waited_on_and_no_output_is_dropped_or_repeated() -> None:
    payload = bytes(range(256)) * 40
    sink = _FullPipeSink()
    script = f"start(); out.write({payload!r}); out.flush(); finish(0)"
    session = PipeSession(
        [sys.executable, "-c", PRELUDE + script], request=b"", nonce=NONCE, on_stdout=sink, on_stderr=Capture()
    )
    with session:
        assert session.pump() is StopReason.DONE
        session.end()

    assert bytes(sink.data) == payload
    assert sink.refusals > 0
    assert envelope.classify(session.stdout, session.stderr, session.exit_status) == Completed(0)


def test_a_sink_that_never_stops_blocking_ends_at_the_deadline_with_the_client_reaped() -> None:
    def blocked(_payload: bytes) -> None:
        raise BlockingIOError(11, "would block", 0)

    run = Run("start(); out.write(b'x'); out.flush(); finish(0)", seconds=0.5)
    run.session._sinks["out"] = blocked  # noqa: SLF001

    assert run.go() is StopReason.DEADLINE
    run.assert_closed()


def test_a_stop_condition_returns_with_the_client_still_running() -> None:
    run = Run("start(); out.write(b'ready'); out.flush(); time.sleep(30)")

    with run.session as session:
        reason = session.pump(lambda: bytes(run.out.data) == b"ready")
        assert reason is StopReason.CONDITION
        assert session._proc is not None  # noqa: SLF001
        assert session._proc.poll() is None  # noqa: SLF001


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_an_interruption_cleans_the_client_before_propagating(interruption: type[BaseException]) -> None:
    def sink(payload: bytes) -> None:
        raise interruption

    session = PipeSession(
        [sys.executable, "-c", PRELUDE + "start(); out.write(b'x'); out.flush(); time.sleep(30)"],
        request=b"",
        nonce=NONCE,
        on_stdout=sink,
        on_stderr=Capture(),
    )
    with pytest.raises(interruption), session:
        session.pump()

    process = session._proc  # noqa: SLF001
    assert process is not None
    assert process.poll() is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signals have no equivalent Windows return code")
def test_a_local_signal_is_not_a_guest_signal() -> None:
    run = Run("import signal; os.kill(os.getpid(), signal.SIGTERM); time.sleep(5)")

    run.go()

    assert run.session.exit_status == -15
    assert run.completion == Unknown(UnknownReason.CONNECTION)


@pytest.mark.parametrize("flood", [False, True])
def test_a_descendant_holding_the_streams_does_not_delay_completion(tmp_path: Path, flood: bool) -> None:
    # The descendant owns only synthetic handles and exits by itself after the
    # release file appears; the test never kills an unowned process tree.
    release = tmp_path / "release"
    descendant = (
        "import os,pathlib,time\n"
        f"release=pathlib.Path({str(release)!r})\n"
        "end=time.monotonic()+10\n"
        "while not release.exists() and time.monotonic()<end:\n"
        + (" try: os.write(2,b'e'*65536)\n except OSError: break\n" if flood else " time.sleep(.01)\n")
    )
    run = Run(
        "import subprocess\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}], stdin=subprocess.DEVNULL)\n"
        "start(); out.write(b'parent'); finish(0)\n"
        "time.sleep(0.2)\n",
        seconds=None,
    )
    try:
        started = time.monotonic()
        reason = run.go()
        assert time.monotonic() - started < 5
        assert reason is StopReason.DONE
        assert bytes(run.out.data) == b"parent"
        assert run.completion == Completed(0)
        run.assert_closed()
    finally:
        release.touch()


def test_output_after_the_closing_lines_is_dropped() -> None:
    run = Run("start(); out.write(b'real'); finish(0); out.write(b'late'); err.write(b'later')")

    run.go()

    assert (bytes(run.out.data), bytes(run.err.data)) == (b"real", b"")


def test_a_client_that_cannot_start_is_a_typed_refusal() -> None:
    session = PipeSession(["/nonexistent/agw-ssh"], request=b"", nonce=NONCE, on_stdout=Capture(), on_stderr=Capture())

    with pytest.raises(ClientUnsupported):
        session.start()


@pytest.mark.skipif(os.name != "nt", reason="Windows child descriptor environment")
def test_the_child_environment_hides_only_the_parents_handle_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "c28fc6f98a2c44abbbd89d6a3037d0d9_POSIX_FD_STATE"
    monkeypatch.setenv(name, "metadata")
    monkeypatch.setenv("OPENSSH_STDIO_MODE", "nonsock")
    monkeypatch.setenv("AGW_PUMP_FIXTURE", "ordinary")

    environment = ssh_process.child_environment()

    assert environment is not None
    assert name.upper() not in {key.upper() for key in environment}
    assert "OPENSSH_STDIO_MODE" not in environment
    assert environment["AGW_PUMP_FIXTURE"] == "ordinary"
    assert os.environ[name] == "metadata"


def test_the_child_environment_is_inherited_off_windows() -> None:
    if os.name == "nt":
        pytest.skip("covered by the Windows test")
    assert ssh_process.child_environment() is None


def test_binary_stdio_round_trips_every_byte_and_end_of_file() -> None:
    data = bytes(range(256)) * 64
    run = Run("start(); out.write(sys.stdin.buffer.read()); finish()", stdin=data)

    run.go()

    assert bytes(run.out.data) == data
    run.assert_closed()
