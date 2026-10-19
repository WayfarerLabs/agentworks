"""The real carrier entrypoint composes preparation and synthetic installed clients."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._process import BorrowedProcessStdin, LocalProcessOwner
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._terminal_handoff import prepare_terminal_handoff
from agentworks.execution.carrier import (
    CarrierIO,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    PreparedInvocation,
    Provenance,
    Retention,
    SinkOutput,
    TerminalInput,
)
from agentworks.execution.carriers.ssh import _io, client
from agentworks.execution.carriers.ssh.client import SSHCarrier
from agentworks.execution.carriers.ssh.connection import SSHConnection
from agentworks.execution.carriers.ssh.trust import SSHTrustFiles
from tests.execution.carriers.ssh._terminal_modes import assert_preserved_terminal_mode

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires owned POSIX PTYs")

# This executable implements a synthetic client, not the SSH protocol. Only the
# preparation case uses a second owned PTY and an independently checked child.
# Fault cases exec the target in place so exact client cleanup owns all work.
_CLIENT = r"""
import errno, json, os, select, subprocess, sys, termios, tty
from pathlib import Path
root = Path(__file__).parent
with (root / 'calls').open('a') as log:
    log.write(json.dumps({'argv': sys.argv[1:], 'term': os.environ.get('TERM')}) + '\n')
if sys.argv[1:] == ['-V']:
    sys.stderr.write((root / 'banner').read_text())
    raise SystemExit(0)
if '-T' in sys.argv or not (root / 'remote-pty').exists():
    os.execve('/bin/sh', ['/bin/sh', '-c', 'exec ' + sys.argv[-1]], os.environ)
assert '-tt' in sys.argv and '-T' not in sys.argv
master, slave = os.openpty()
termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(0))
termios.tcsetwinsize(slave, termios.tcgetwinsize(0))
tty.setraw(0, termios.TCSANOW)
child = subprocess.Popen(['/bin/sh', '-c', 'exec ' + sys.argv[-1]], stdin=slave, stdout=slave, stderr=slave)
(root / 'inner-pid').write_text(str(child.pid))
os.close(slave)
try:
    while True:
        readable, _, _ = select.select([0, master], [], [], 0.05)
        if master in readable:
            try:
                data = os.read(master, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not data:
                break
            os.write(1, data)
        if 0 in readable:
            data = os.read(0, 65536)
            if data:
                os.write(master, data)
    status = child.wait(timeout=2)
    try:
        os.waitpid(child.pid, os.WNOHANG)
    except ChildProcessError:
        (root / 'inner-retired').write_text(str(child.pid))
    else:
        raise AssertionError('inner child was not reaped')
finally:
    if child.poll() is None:
        child.kill()
        child.wait()
    os.close(master)
if status < 0:
    import signal
    signal.signal(-status, signal.SIG_DFL)
    os.kill(os.getpid(), -status)
raise SystemExit(status)
"""


@dataclass
class Sink:
    data: bytearray = field(default_factory=bytearray, repr=False)
    stalls: int = 3

    def try_write(self, data: memoryview) -> int | None:
        if self.stalls:
            self.stalls -= 1
            return None
        count = min(5, len(data))
        self.data.extend(data[:count])
        return count


class Handoff:
    def try_read(self, limit: int) -> bytes:
        return b""


@dataclass
class Installed:
    carrier: SSHCarrier
    root: Path
    master: int
    slave: int
    custody: LocalDeliveryCustody

    @property
    def calls(self) -> list[dict[str, Any]]:
        path = self.root / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def io(self) -> CarrierIO:
        return CarrierIO(
            TerminalInput(self.slave, self.slave, "composition-term", Handoff()), SinkOutput(Sink(), Sink())
        )

    def execute(self, invocation: PreparedInvocation, io: CarrierIO, seconds: float = 5):
        return self.carrier.execute(invocation, io=io, deadline=Deadline.after(seconds), custody=self.custody)


@pytest.fixture
def installed(tmp_path: Path, custody: LocalDeliveryCustody) -> Iterator[Installed]:
    import termios

    tmp_path = tmp_path.resolve()
    executable = tmp_path / "synthetic-ssh"
    executable.write_text(f"#!{sys.executable}\n" + _CLIENT)
    executable.chmod(0o700)
    (tmp_path / "banner").write_text("OpenSSH_9.9p1\n")
    identity, trust = tmp_path / "identity", tmp_path / "trust"
    identity.write_bytes(b"synthetic identity")
    trust.write_bytes(b"synthetic trust")
    carrier = SSHCarrier(
        SSHConnection("synthetic.example", "user", identity, SSHTrustFiles((trust,)), ssh_executable=str(executable))
    )
    master, slave = os.openpty()
    saved = termios.tcgetattr(slave)
    try:
        yield Installed(carrier, tmp_path, master, slave, custody)
    finally:
        assert custody.close(Deadline.after(3))
        try:
            assert_preserved_terminal_mode(termios.tcgetattr(slave), saved)
            inner = tmp_path / "inner-pid"
            if inner.exists():
                inner_pid = int(inner.read_text())
                assert int((tmp_path / "inner-retired").read_text()) == inner_pid
                with pytest.raises(ProcessLookupError):
                    os.kill(inner_pid, 0)
        finally:
            os.close(slave)
            os.close(master)


@pytest.mark.skipif(sys.platform != "linux", reason="The unchanged guest helper requires Linux")
def test_real_preparation_preserves_early_keys_geometry_term_and_privacy(
    installed: Installed,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    request: pytest.FixtureRequest,
) -> None:
    import fcntl
    import termios

    (installed.root / "remote-pty").touch()
    original_mode = termios.tcgetattr(installed.slave)

    def restore_fixture_mode():
        assert installed.custody.close(Deadline.after(3))
        termios.tcsetattr(installed.slave, termios.TCSANOW, original_mode)

    request.addfinalizer(restore_fixture_mode)
    mode = termios.tcgetattr(installed.slave)
    assert mode[3] & termios.ICANON
    mode[3] &= ~(termios.ECHO | termios.ECHONL)
    mode[6][termios.VINTR] = b"\x07"
    termios.tcsetattr(installed.slave, termios.TCSANOW, mode)
    termios.tcsetwinsize(installed.slave, (31, 97))
    flags = fcntl.fcntl(installed.slave, fcntl.F_GETFL), os.get_inheritable(installed.slave)
    presentation, diagnostics = Sink(), Sink()
    canary = b"private-bootstrap-canary"
    source = canary + bytes(range(256))
    program = (
        "import os,termios; "
        f"assert os.read(3,4096)=={source!r}; "
        f"assert os.environb[b'VALUE']=={canary + bytes([255])!r}; "
        "assert termios.tcgetwinsize(0)==(31,97); "
        "mode=termios.tcgetattr(0); "
        "assert mode[3]&termios.ICANON and not mode[3]&(termios.ECHO|termios.ECHONL); "
        "assert mode[6][termios.VINTR]==b'\\x07'; "
        "data=b''\n"
        "while len(data)<2:\n"
        " chunk=os.read(0,2-len(data)); assert chunk; data+=chunk\n"
        "assert data==b'\\x07\\n'\n"
        "os.write(1,b'BODY-RAN'); raise SystemExit(37)"
    )
    prepared = prepare_terminal_handoff(
        (os.fsencode(sys.executable), b"-c", program.encode()),
        {b"VALUE": canary + bytes([255])},
        source,
        presentation,
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX),
    )
    io = prepared.carrier_io(
        input_fd=installed.slave, output_fd=installed.slave, term="composition-term", diagnostics=diagnostics
    )
    prepared.claim()
    original_start, original_read = LocalProcessOwner.start, os.read
    terminal_starts = 0
    keyboard_reads = 0
    keyboard_input = bytearray()
    raw_output = bytearray()

    def start(owner, request, **kwargs):
        nonlocal terminal_starts
        if isinstance(request.input, BorrowedProcessStdin):
            terminal_starts += 1
            assert canary not in repr(request.env).encode()
            assert not termios.tcgetattr(installed.slave)[3] & (termios.ICANON | termios.ECHO | termios.ISIG)
            assert_preserved_terminal_mode(termios.tcgetattr(request.input.descriptor), mode)
            os.write(installed.master, b"\x16\x07\r")
        return original_start(owner, request, **kwargs)

    def read(fd: int, limit: int) -> bytes:
        nonlocal keyboard_reads
        if fd == installed.slave:
            keyboard_reads += 1
            assert prepared.handed_off
        chunk = original_read(fd, limit)
        if fd == installed.slave:
            keyboard_input.extend(chunk)
        else:
            raw_output.extend(chunk)
        return chunk

    monkeypatch.setattr(LocalProcessOwner, "start", start)
    monkeypatch.setattr(os, "read", read)
    report = installed.execute(prepared.invocation, io)
    prepared.stdout.finish()
    assert terminal_starts == 1 and keyboard_reads > 0
    assert bytes(keyboard_input) == b"\x16\x07\r"
    assert prepared.handed_off and prepared.failure is None
    assert report.dispatch is Dispatch.SENT and report.completion == ExitStatus(code=37)
    assert report.failure is None and installed.custody.settled
    assert bytes(presentation.data) == b"BODY-RAN" and not diagnostics.data
    assert report.stdout.complete and report.stderr.complete
    assert report.stdout.retention is report.stderr.retention is Retention.DELIVERED
    assert report.stdout.provenance is Provenance.CARRIER_STDOUT
    assert report.stderr.provenance is Provenance.MIXED_STDERR
    assert report.stdout.data == report.stderr.data == b""
    assert len(installed.calls) == 2 and installed.calls[0]["argv"] == ["-V"]
    assert installed.calls[1]["term"] == "composition-term"
    assert canary not in (repr(report) + repr(io) + repr(installed.calls) + caplog.text).encode()
    assert canary not in presentation.data and canary not in diagnostics.data and canary not in raw_output
    assert b"BODY-RAN" in raw_output
    for encoded in (canary.hex().upper().encode(), canary.hex().encode()):
        assert encoded not in raw_output and encoded not in presentation.data and encoded not in diagnostics.data
    assert (fcntl.fcntl(installed.slave, fcntl.F_GETFL), os.get_inheritable(installed.slave)) == flags
    assert_preserved_terminal_mode(termios.tcgetattr(installed.slave), mode)


@pytest.mark.parametrize("status", [0, 37, 254, 255, -15])
def test_terminal_status_uses_common_truthful_report(installed: Installed, status: int) -> None:
    command = f"exit {status}" if status >= 0 else "kill -TERM $$"
    report = installed.execute(PreparedInvocation(("/bin/sh", "-c", command)), installed.io())
    if 0 <= status < 255:
        assert report.dispatch is Dispatch.SENT and report.completion == ExitStatus(code=status)
        assert report.failure is None
    else:
        assert report.dispatch is Dispatch.UNKNOWN and report.completion is None
        assert report.failure is Failure.OBSERVATION
    assert installed.custody.settled


@pytest.mark.parametrize("gate", ["trust", "selection", "version", "deadline", "custody"])
def test_common_gates_precede_terminal_acquisition(installed: Installed, monkeypatch: pytest.MonkeyPatch, gate: str):
    from agentworks.execution.carriers.ssh._terminal_posix import PosixTerminal

    acquired = False

    def forbidden(terminal):
        nonlocal acquired
        acquired = True
        raise AssertionError("terminal acquired before admission")

    monkeypatch.setattr(PosixTerminal, "acquire", forbidden)
    if gate == "trust":
        (installed.root / "trust").unlink()
    elif gate == "selection":
        (installed.root / "synthetic-ssh").unlink()
    elif gate == "version":
        (installed.root / "banner").write_text("OpenSSH_8.4p1\n")
    elif gate == "custody":
        installed.custody.begin_process()
    if gate == "custody":
        with pytest.raises(StateError):
            installed.execute(PreparedInvocation(("/bin/true",)), installed.io())
    else:
        report = installed.execute(PreparedInvocation(("/bin/true",)), installed.io(), 0 if gate == "deadline" else 5)
        assert report.dispatch is Dispatch.NOT_SENT
        assert report.failure is (Failure.DEADLINE if gate == "deadline" else Failure.DISPATCH)
    assert not acquired
    assert len(installed.calls) == (1 if gate == "version" else 0)


def test_unsupported_platform_refuses_passively(installed: Installed, monkeypatch: pytest.MonkeyPatch):
    io = installed.io()
    monkeypatch.setattr(os, "name", "nt")
    try:
        with pytest.raises(ValidationError):
            installed.execute(PreparedInvocation(("/bin/true",)), io)
    finally:
        monkeypatch.undo()
    assert installed.calls == []


def test_pending_command_retains_original_custody_and_terminal(installed: Installed, monkeypatch: pytest.MonkeyPatch):
    import termios
    from threading import Event

    entered, release = Event(), Event()
    spawn, run = subprocess.Popen, _io.run_process
    monotonic = time.monotonic
    # Keep real preflight time, then expire the original budget at construction.
    # Fresh cleanup deadlines still advance naturally after the one phase jump.
    monkeypatch.setattr(time, "monotonic", lambda: monotonic() + (10 if entered.is_set() else 0))
    deadline = Deadline.after(5)
    observed: list[tuple[Deadline, LocalDeliveryCustody]] = []

    def delayed(argv, **kwargs):
        if argv[-1] != "-V":
            entered.set()
            assert release.wait(5)
        return spawn(argv, **kwargs)

    def observe(argv, *, io, deadline, custody):
        observed.append((deadline, custody))
        return run(argv, io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(subprocess, "Popen", delayed)
    monkeypatch.setattr(client, "run_process", observe)
    try:
        report = installed.carrier.execute(
            PreparedInvocation(("/bin/true",)), io=installed.io(), deadline=deadline, custody=installed.custody
        )
        assert entered.is_set()
        assert deadline.expired
        assert len(observed) == 2
        assert all(seen is deadline and store is installed.custody for seen, store in observed)
        assert report.dispatch is Dispatch.UNKNOWN and report.completion is None
        assert report.failure is Failure.DEADLINE
        assert not installed.custody.settled
        assert not termios.tcgetattr(installed.slave)[3] & (termios.ICANON | termios.ECHO | termios.ISIG)
        assert not installed.custody.close(Deadline.after(0))
        with pytest.raises(StateError):
            installed.execute(PreparedInvocation(("/bin/true",)), installed.io())
        assert len(installed.calls) == 1
    finally:
        release.set()
        assert installed.custody.close(Deadline.after(3))
    assert report.dispatch is Dispatch.UNKNOWN and report.completion is None
    assert not (installed.root / "inner-pid").exists()
    os.fstat(installed.slave)


def test_sink_control_exception_survives_carrier_reduction(installed: Installed):
    interruption = KeyboardInterrupt()
    calls = 0

    class InterruptedSink:
        def try_write(self, data: memoryview) -> int:
            nonlocal calls
            calls += 1
            raise interruption

    io = CarrierIO(
        TerminalInput(installed.slave, installed.slave, "composition-term", Handoff()),
        SinkOutput(InterruptedSink(), Sink()),
    )
    with pytest.raises(KeyboardInterrupt) as raised:
        installed.execute(
            PreparedInvocation((sys.executable, "-c", "import os,time; os.write(1,b'body'); time.sleep(30)")), io
        )
    assert raised.value is interruption and calls == 1
    assert not (installed.root / "inner-pid").exists()
    assert installed.custody.close(Deadline.after(3))
    os.fstat(installed.slave)


def test_nonterminal_handles_refuse_before_command_launch(installed: Installed):
    read_fd, write_fd = os.pipe()
    try:
        io = CarrierIO(
            TerminalInput(read_fd, installed.slave, "composition-term", Handoff()), SinkOutput(Sink(), Sink())
        )
        installed.carrier.validate(PreparedInvocation(("/bin/true",)), io=io)
        assert installed.calls == []
        report = installed.execute(PreparedInvocation(("/bin/true",)), io)
        assert report.dispatch is Dispatch.NOT_SENT and report.failure is Failure.DISPATCH
        assert len(installed.calls) == 1 and installed.calls[0]["argv"] == ["-V"]
        assert installed.custody.settled
        os.fstat(read_fd)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_fresh_byte_execution_never_imports_terminal_modules(installed: Installed):
    code = r"""
import builtins, pathlib, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in ('termios', 'tty', 'pty') or name.endswith(('._terminal_relay', '._terminal_posix')):
        raise AssertionError('byte delivery imported terminal dependency')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import CarrierIO, Deadline, Dispatch, ExitStatus, PreparedInvocation
from agentworks.execution.carriers.ssh.client import SSHCarrier
from agentworks.execution.carriers.ssh.connection import SSHConnection
from agentworks.execution.carriers.ssh.trust import SSHTrustFiles
root = pathlib.Path(sys.argv[1])
carrier = SSHCarrier(SSHConnection('synthetic.example', 'user', root / 'identity',
    SSHTrustFiles((root / 'trust',)), ssh_executable=str(root / 'synthetic-ssh')))
custody = LocalDeliveryCustody()
try:
    report = carrier.execute(PreparedInvocation(('/bin/sh', '-c', 'printf byte-path; exit 23')),
        io=CarrierIO(), deadline=Deadline.after(5), custody=custody)
    assert report.dispatch is Dispatch.SENT and report.completion == ExitStatus(code=23)
    assert report.stdout.data == b'byte-path' and report.failure is None
finally:
    assert custody.close(Deadline.after(3))
"""
    completed = subprocess.run([sys.executable, "-c", code, str(installed.root)], capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stderr.decode()
    assert len(installed.calls) == 2 and "-T" in installed.calls[1]["argv"]
