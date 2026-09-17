"""Synthetic children exercise SSH process ownership without SSH or credentials."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from subprocess import Popen
from typing import Any

import pytest

from agentworks.execution.carrier import (
    Capture,
    CarrierIO,
    Deadline,
    Discard,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Provenance,
    Retention,
)
from agentworks.execution.carriers.ssh import _io, client
from agentworks.execution.carriers.ssh.client import SSHCarrier
from agentworks.execution.carriers.ssh.connection import SSHConnection

pytestmark = pytest.mark.windows


@dataclass
class SyntheticSSH:
    carrier: SSHCarrier
    command: str = "pass"
    version: str = "import sys; sys.stderr.write('OpenSSH_9.9p1, LibreSSL 3.3.6\\n')"
    calls: list[list[str]] = field(default_factory=list)
    children: list[subprocess.Popen[bytes]] = field(default_factory=list)

    def execute(self, io: CarrierIO | None = None, seconds: float | None = 10):
        return self.carrier.execute(
            PreparedInvocation(("/synthetic/program",)), io=io or CarrierIO(), deadline=Deadline.after(seconds)
        )

    def assert_closed(self) -> None:
        for child in self.children:
            assert child.poll() is not None
            for pipe in (child.stdin, child.stdout, child.stderr):
                assert pipe is None or pipe.closed


@pytest.fixture
def synthetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    key.write_bytes(b"synthetic identity")
    known_hosts.write_bytes(b"synthetic trust")
    connection = SSHConnection("synthetic.example", "user", key, known_hosts)
    value = SyntheticSSH(SSHCarrier(connection))
    original = subprocess.Popen

    def spawn(argv: list[str], **kwargs: Any):
        value.calls.append(argv)
        script = value.version if argv[-1] == "-V" else value.command
        child = original([sys.executable, "-c", script], **kwargs)
        value.children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    yield value
    # Failed assertions must not leave synthetic processes running.
    for child in value.children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


def test_inspection_is_passive(synthetic: SyntheticSSH) -> None:
    assert not synthetic.carrier.features.live_stdio
    assert not synthetic.carrier.features.terminal
    assert synthetic.calls == []


@pytest.mark.parametrize("code", [0, 1, 23, 254, 255])
def test_status_and_raw_stream_evidence(synthetic: SyntheticSSH, code: int) -> None:
    synthetic.command = (
        "import sys; sys.stdout.buffer.write(b'\\x00\\xff\\r\\n\\n'); "
        f"sys.stderr.buffer.write(b'\\x80err\\n'); sys.exit({code})"
    )
    report = synthetic.execute()
    assert len(synthetic.calls) == 2
    assert report.local_status == code
    assert report.completion == (None if code == 255 else ExitStatus(code=code))
    assert report.dispatch == (Dispatch.UNKNOWN if code == 255 else Dispatch.SENT)
    assert report.failure == (Failure.OBSERVATION if code == 255 else None)
    assert report.stdout.data == b"\x00\xff\r\n\n"
    assert report.stderr.data == b"\x80err\n"
    assert report.stdout.complete and report.stderr.complete
    assert report.stdout.provenance == Provenance.CARRIER_STDOUT
    assert report.stderr.provenance == Provenance.MIXED_STDERR
    synthetic.assert_closed()


@pytest.mark.parametrize("status", [256, 0xC0000005, 0xC000013A])
def test_native_status_outside_posix_exit_range_is_not_guest_completion(
    synthetic: SyntheticSSH, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    original = _io.run_process

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        return result if argv[-1] == "-V" else replace(result, local_status=status, exit_status=status)

    monkeypatch.setattr(client, "run_process", run)
    report = synthetic.execute()
    assert report.local_status == status
    assert report.completion is None
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.failure == Failure.OBSERVATION
    synthetic.assert_closed()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows DWORD process status")
def test_windows_process_status_preserves_unknown_completion(synthetic: SyntheticSSH) -> None:
    synthetic.command = "import ctypes; ctypes.windll.kernel32.ExitProcess(0xC0000005)"
    report = synthetic.execute()
    assert report.local_status == 0xC0000005
    assert report.completion is None
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.failure == Failure.OBSERVATION
    synthetic.assert_closed()


def test_duplex_pressure_delivers_all_input_once(synthetic: SyntheticSSH) -> None:
    data = bytes(range(256)) * 8192
    synthetic.command = (
        "import hashlib,sys; "
        "sys.stdout.buffer.write(b'o'*200000); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'e'*200000); sys.stderr.buffer.flush(); "
        "data=sys.stdin.buffer.read(); sys.stdout.buffer.write(hashlib.sha256(data).hexdigest().encode())"
    )
    report = synthetic.execute(CarrierIO(input=FiniteInput(data)))
    assert report.failure is None
    assert report.stdout.data == b"o" * 200_000 + hashlib.sha256(data).hexdigest().encode()
    assert report.stderr.data == b"e" * 200_000
    assert report.completion == ExitStatus(code=0)
    assert report.stdout.complete and report.stderr.complete
    synthetic.assert_closed()


@pytest.mark.parametrize("finite", [False, True])
def test_empty_input_observes_eof(synthetic: SyntheticSSH, finite: bool) -> None:
    synthetic.command = "import sys; assert sys.stdin.buffer.read() == b''; sys.stdout.buffer.write(b'eof\\n')"
    report = synthetic.execute(CarrierIO(input=FiniteInput(b"")) if finite else CarrierIO())
    assert report.stdout.data == b"eof\n"
    assert report.failure is None
    assert (synthetic.children[-1].stdin is not None) is finite
    synthetic.assert_closed()


@pytest.mark.parametrize("count,cap", [(0, 0), (8, 8), (9, 8), (100_000, 0)])
def test_both_capture_limits_are_explicit(synthetic: SyntheticSSH, count: int, cap: int) -> None:
    synthetic.command = f"import sys; sys.stdout.buffer.write(b'o'*{count}); sys.stderr.buffer.write(b'e'*{count})"
    report = synthetic.execute(CarrierIO(output=Capture(cap)))
    assert report.stdout.data == b"o" * min(count, cap)
    assert report.stderr.data == b"e" * min(count, cap)
    assert report.stdout.complete is (count <= cap)
    assert report.stderr.complete is (count <= cap)
    assert report.failure == (Failure.OUTPUT_LIMIT if count > cap else None)
    assert report.completion == ExitStatus(code=0)
    synthetic.assert_closed()


@pytest.mark.parametrize(
    "io,retention",
    [
        (CarrierIO(input=FiniteInput(b"secret-canary", sensitive=True)), Retention.SUPPRESSED),
        (CarrierIO(input=FiniteInput(b"secret-canary"), sensitive=True), Retention.SUPPRESSED),
        (CarrierIO(input=FiniteInput(b"secret-canary"), output=Discard()), Retention.DISCARDED),
    ],
)
def test_unretained_output_never_enters_capture(
    synthetic: SyntheticSSH, monkeypatch: pytest.MonkeyPatch, io: CarrierIO, retention: Retention
) -> None:
    synthetic.command = (
        "import sys; data=sys.stdin.buffer.read()*10000; sys.stdout.buffer.write(data); sys.stderr.buffer.write(data)"
    )
    observed = []
    original = _io._Output.report

    def report(output):
        if output.retention != Retention.CAPTURED:
            observed.append(len(output.data))
        return original(output)

    monkeypatch.setattr(_io._Output, "report", report)
    result = synthetic.execute(io)
    assert observed == [0, 0]
    assert result.stdout.data == result.stderr.data == b""
    assert result.stdout.retention == result.stderr.retention == retention
    assert result.stdout.complete and result.stderr.complete
    assert result.failure is None
    assert result.completion == ExitStatus(code=0)
    assert "secret-canary" not in repr(result)


def test_short_input_writes_advance_without_replay(synthetic: SyntheticSSH, monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.write

    def write(fd, data):
        return original(fd, data[:7])

    monkeypatch.setattr(os, "write", write)
    data = b"binary\x00\xff\n" * 50
    synthetic.command = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    report = synthetic.execute(CarrierIO(input=FiniteInput(data)))
    assert report.stdout.data == data
    assert report.failure is None
    synthetic.assert_closed()


def test_failed_input_is_safe_and_never_replayed(synthetic: SyntheticSSH, monkeypatch: pytest.MonkeyPatch) -> None:
    def write(fd, data):
        raise OSError("secret-canary")

    monkeypatch.setattr(os, "write", write)
    synthetic.command = "import time; time.sleep(30)"
    report = synthetic.execute(CarrierIO(input=FiniteInput(b"secret-canary")))
    assert report.failure == Failure.INPUT
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.completion is None
    assert len(synthetic.calls) == 2
    assert "secret-canary" not in repr(report)
    synthetic.assert_closed()


def test_output_error_is_safe_and_preserves_other_partial_stream(
    synthetic: SyntheticSSH, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.read

    def read(fd, size):
        if len(synthetic.children) == 2:
            pipe = synthetic.children[-1].stderr
            assert pipe is not None
            if fd == pipe.fileno():
                raise OSError("secret-canary")
        return original(fd, size)

    monkeypatch.setattr(os, "read", read)
    synthetic.command = "import time; time.sleep(30)"
    report = synthetic.execute()
    assert report.failure == Failure.OUTPUT
    assert report.completion is None
    assert not report.stderr.complete
    assert "secret-canary" not in repr(report)
    synthetic.assert_closed()


def test_deadline_keeps_partial_evidence_and_reaps(synthetic: SyntheticSSH) -> None:
    synthetic.command = (
        "import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); "
        "sys.stderr.write('diagnostic'); sys.stderr.flush(); time.sleep(30)"
    )
    started = time.monotonic()
    report = synthetic.execute(seconds=0.5)
    assert time.monotonic() - started < 2
    assert report.failure == Failure.DEADLINE
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.completion is None
    assert report.stdout.data == b"partial" and report.stderr.data == b"diagnostic"
    assert not report.stdout.complete and not report.stderr.complete
    synthetic.assert_closed()


def test_version_consumes_the_original_deadline(synthetic: SyntheticSSH) -> None:
    synthetic.version = "import time; time.sleep(30)"
    report = synthetic.execute(seconds=0.1)
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.failure == Failure.DEADLINE
    assert len(synthetic.calls) == 1
    synthetic.assert_closed()


def test_dispatch_only_gets_budget_remaining_after_version(synthetic: SyntheticSSH) -> None:
    synthetic.version = "import sys,time; time.sleep(.2); sys.stderr.write('OpenSSH_9.9p1')"
    synthetic.command = "import time; time.sleep(.25)"
    report = synthetic.execute(seconds=0.4)
    assert report.failure == Failure.DEADLINE
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.completion is None
    assert len(synthetic.calls) == 2
    synthetic.assert_closed()


def test_missing_connection_file_refuses_before_startup(synthetic: SyntheticSSH, tmp_path: Path) -> None:
    (tmp_path / "identity").unlink()
    report = synthetic.execute()
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.failure == Failure.DISPATCH
    assert synthetic.calls == []


def test_nonblocking_setup_failure_cleans_without_guessing_dispatch(
    synthetic: SyntheticSSH, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.set_blocking

    def set_blocking(fd, value):
        if len(synthetic.children) == 2:
            raise OSError("secret-canary")
        original(fd, value)

    monkeypatch.setattr(os, "set_blocking", set_blocking)
    synthetic.command = "import time; time.sleep(30)"
    report = synthetic.execute()
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.failure == Failure.OBSERVATION
    assert "secret-canary" not in repr(report)
    synthetic.assert_closed()


def test_failed_reap_is_explicit(synthetic: SyntheticSSH, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Popen.wait

    def wait(process, timeout=None):
        if len(synthetic.children) == 2 and process is synthetic.children[-1]:
            raise subprocess.TimeoutExpired("secret-canary", timeout)
        return original(process, timeout=timeout)

    synthetic.command = "import time; time.sleep(30)"
    with monkeypatch.context() as context:
        context.setattr(Popen, "wait", wait)
        report = synthetic.execute(seconds=0.1)
    assert report.failure == Failure.OBSERVATION
    assert report.completion is None
    assert report.dispatch == Dispatch.UNKNOWN
    assert "secret-canary" not in repr(report)
    synthetic.children[-1].wait(timeout=2)
    synthetic.assert_closed()


def test_interrupted_failed_reap_attaches_safe_evidence(
    synthetic: SyntheticSSH, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_wait = Popen.wait
    original_read = _io._Output.read

    def read(output, pipe):
        if len(synthetic.children) == 2:
            raise KeyboardInterrupt()
        return original_read(output, pipe)

    def wait(process, timeout=None):
        if len(synthetic.children) == 2 and process is synthetic.children[-1]:
            raise subprocess.TimeoutExpired("secret-canary", timeout)
        return original_wait(process, timeout=timeout)

    synthetic.command = "import time; time.sleep(30)"
    with monkeypatch.context() as context:
        context.setattr(_io._Output, "read", read)
        context.setattr(Popen, "wait", wait)
        with pytest.raises(KeyboardInterrupt) as raised:
            synthetic.execute()
    assert raised.value.__notes__
    assert "secret-canary" not in repr(raised.value.__notes__)
    synthetic.children[-1].wait(timeout=2)
    synthetic.assert_closed()


def test_expired_deadline_does_not_start_even_version(synthetic: SyntheticSSH) -> None:
    report = synthetic.execute(seconds=0)
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.failure == Failure.DEADLINE
    assert synthetic.calls == []


@pytest.mark.parametrize("version", ["OpenSSH_8.4p1", "Dropbear v2025.88", "secret-canary", "OpenSSH_9.x"])
def test_unsupported_version_refuses_locally(synthetic: SyntheticSSH, version: str) -> None:
    synthetic.version = f"import sys; sys.stderr.write({version!r})"
    report = synthetic.execute(CarrierIO(sensitive=True))
    assert report.failure == Failure.DISPATCH
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.local_status is None
    assert len(synthetic.calls) == 1
    assert report.stdout.retention == report.stderr.retention == Retention.SUPPRESSED
    assert "secret-canary" not in repr(report)


@pytest.mark.parametrize("version", ["OpenSSH_8.5p1", "OpenSSH_10.1", "OpenSSH_for_Windows_9.5p1, LibreSSL 3.8.2"])
def test_supported_client_banners(synthetic: SyntheticSSH, version: str) -> None:
    synthetic.version = f"import sys; sys.stderr.write({version!r})"
    assert synthetic.execute().failure is None
    assert len(synthetic.calls) == 2


@pytest.mark.parametrize("phase", ["version", "command"])
def test_failed_spawn_does_not_claim_dispatch_or_expose_exception(
    synthetic: SyntheticSSH, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    original = subprocess.Popen

    def spawn(argv, **kwargs):
        if phase == "version" or argv[-1] != "-V":
            raise OSError("secret-canary")
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    report = synthetic.execute()
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.failure == Failure.DISPATCH
    assert report.completion is None
    assert "secret-canary" not in repr(report)
    synthetic.assert_closed()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_interruption_cleans_owned_process_before_propagating(
    synthetic: SyntheticSSH, monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException]
) -> None:
    original = _io._Output.read

    def read(output, pipe):
        if len(synthetic.children) == 2:
            raise interruption()
        return original(output, pipe)

    monkeypatch.setattr(_io._Output, "read", read)
    synthetic.command = "import time; time.sleep(30)"
    with pytest.raises(interruption):
        synthetic.execute()
    assert len(synthetic.calls) == 2
    synthetic.assert_closed()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signals have no equivalent Windows return code")
def test_local_signal_does_not_become_guest_signal(synthetic: SyntheticSSH) -> None:
    synthetic.command = "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"
    report = synthetic.execute()
    assert report.local_status == -15
    assert report.completion is None
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.failure == Failure.OBSERVATION


@pytest.mark.parametrize("flood", [False, True])
def test_descendant_output_handles_do_not_block_cleanup(
    synthetic: SyntheticSSH, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flood: bool
) -> None:
    # The descendant owns only synthetic handles and exits by itself after the
    # release file appears; the fixture never kills an unowned process tree.
    release = tmp_path / "release"
    done = tmp_path / "done"
    descendant = (
        "import os,pathlib,time; "
        f"release=pathlib.Path({str(release)!r}); done=pathlib.Path({str(done)!r}); "
        "end=time.monotonic()+5\n"
        "while not release.exists() and time.monotonic()<end:\n"
        + (" try: os.write(2,b'e'*65536)\n except OSError: break\n" if flood else " time.sleep(.01)\n")
        + "done.touch()"
    )
    synthetic.command = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}], stdin=subprocess.DEVNULL); "
        "sys.stdout.write('parent')"
    )
    if flood:
        original = _io._Output.read

        def read(output, pipe):
            progressed = original(output, pipe)
            # Model uninterrupted pipe readiness even if this test scheduler
            # happens to pause the real flooding writer between two reads.
            return progressed or (len(synthetic.children) == 2 and not output.eof)

        monkeypatch.setattr(_io._Output, "read", read)
    try:
        started = time.monotonic()
        report = synthetic.execute(CarrierIO(output=Capture(32)), seconds=None)
        assert time.monotonic() - started < 2
        assert report.stdout.data == b"parent"
        assert len(report.stderr.data) <= 32
        assert report.completion == ExitStatus(code=0)
        assert report.failure == Failure.OUTPUT
        assert not report.stdout.complete and not report.stderr.complete
        synthetic.assert_closed()
    finally:
        release.touch()
        until = time.monotonic() + 5
        while not done.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert done.exists()
