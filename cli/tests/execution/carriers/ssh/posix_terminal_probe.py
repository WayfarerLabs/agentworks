"""Deferred installed-SSH proof composition; importing this helper has no effects."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState, RuntimeSelection, RuntimeTargetOS
from agentworks.execution._terminal_guest import INTERACTIVE_READY, PAYLOAD_READY, READINESS_MAGIC
from agentworks.execution._terminal_handoff import (
    PreparedTerminalHandoff,
    TerminalHandoffFailure,
    prepare_terminal_handoff,
)
from agentworks.execution.carrier import Deadline, Failure, SinkOutput, TerminalInput
from agentworks.execution.carriers.ssh._terminal_posix import PosixTerminal
from agentworks.execution.carriers.ssh._terminal_relay import run_terminal_relay_candidate
from agentworks.execution.carriers.ssh.client import check_client_version, resolve_client_executable
from agentworks.execution.carriers.ssh.connection import admit_connection, build_ssh_argv
from tests.execution.carriers.ssh._terminal_modes import assert_preserved_terminal_mode
from tests.execution.carriers.ssh.enrollment_server import enrollment_server

if TYPE_CHECKING:
    import pytest

_SOURCE_TAG = b"agw-source-canary-85d8c8df"
_ENV_TAG = b"agw-environment-canary-5e7b74eb"
_ARG_TAG = b"agw-argument-canary-c8a64d0f"
_SOURCE = _SOURCE_TAG + b"\0\r\n\x03\x04\x11\x13\x16\x7f\xff"
_ENV = _ENV_TAG + b"\nvalue with spaces"
_KEYS = b"owned-keys\x16\x07\r\n"
_MAX_OUTPUT = 65_536

GUEST_SOURCE = r"""
import hashlib,json,os,select,signal,sys,termios,time,tty
saved=termios.tcgetattr(0)
tty.setraw(0)
source=bytearray()
while True:
    chunk=os.read(3,4096)
    if not chunk: break
    source.extend(chunk)
def emit(value): os.write(1,json.dumps(value,separators=(',',':')).encode()+b'\n')
sizes=[]
def resized(signum,frame):
    size=list(termios.tcgetwinsize(0))
    if sizes and sizes[-1]==size: return
    sizes.append(size)
    emit({'resize':size,'signal':signum})
signal.signal(signal.SIGWINCH,resized)
stat=open('/proc/self/stat').read().rsplit(')',1)[1].split()
emit({'initial':list(termios.tcgetwinsize(0)), 'tty':os.isatty(0) and os.isatty(1),
      'pid':os.getpid(),'born':stat[19],'uid':os.getuid(),
      'proof':[hashlib.sha256(source).hexdigest(),
               hashlib.sha256(os.environb[b'PROBE_SECRET']).hexdigest(),
               hashlib.sha256(os.fsencode(sys.argv[1])).hexdigest()]})
keyboard=bytearray()
until=time.monotonic()+15
try:
    while (len(sizes)<2 or len(keyboard)<int(sys.argv[2])) and time.monotonic()<until:
        if select.select([0],[],[],.05)[0]:
            data=os.read(0,int(sys.argv[2])-len(keyboard)) if len(keyboard)<int(sys.argv[2]) else b''
            if data: keyboard.extend(data)
    assert sizes==[[42,113],[58,144]] and len(keyboard)==int(sys.argv[2])
    emit({'keys':keyboard.hex(),'done':True})
finally:
    termios.tcsetattr(0,termios.TCSANOW,saved)
"""


class Capture:
    """Bounded fixture-only observation, never carrier retention or live rendering."""

    def __init__(self) -> None:
        self.data = bytearray()

    def try_write(self, data: memoryview) -> int | None:
        assert len(self.data) + len(data) <= _MAX_OUTPUT
        self.data.extend(data)
        return len(data)


class Presentation(Capture):
    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self.trace = trace
        self.stalls = 3
        self.offset = 0

    def try_write(self, data: memoryview) -> int | None:
        if self.stalls:
            self.stalls -= 1
            return None
        written = min(len(data), 7)
        super().try_write(data[:written])
        while (end := self.data.find(b"\n", self.offset)) >= 0:
            record = json.loads(self.data[self.offset : end])
            self.offset = end + 1
            assert isinstance(record, dict)
            self.trace.records.append(record)
            if "initial" in record:
                self.trace.application_ready = True
                self.trace.guest_until = time.monotonic() + 20
            elif record.get("resize") == [42, 113]:
                import termios

                termios.tcsetwinsize(self.trace.slave, (58, 144))
        self.trace.interact()
        return written


class Trace:
    """Observe the real source and collector; never interpret their control frames."""

    def __init__(self, master: int, slave: int) -> None:
        self.master, self.slave = master, slave
        self.prepared: PreparedTerminalHandoff | None = None
        self.raw_stdout = bytearray()
        self.presentation = Presentation(self)
        self.diagnostics = Capture()
        self.records: list[dict[str, object]] = []
        self.payload_bytes = 0
        self.handoff_eof = False
        self.application_ready = False
        self.guest_until: float | None = None
        self.keys_written = False

    def marker(self, kind: int) -> bytes:
        assert self.prepared is not None
        return READINESS_MAGIC + self.prepared.nonce.upper().encode("ascii") + b":" + bytes((kind,))

    def try_read(self, limit: int) -> bytes | None:
        assert self.prepared is not None
        data = self.prepared.bootstrap.try_read(limit)
        if data:
            assert self.prepared.runtime_prerequisite.state is RuntimePrerequisiteState.READY
            assert self.marker(PAYLOAD_READY) in self.raw_stdout
            assert not self.prepared.handed_off and not self.keys_written
            self.payload_bytes += len(data)
        elif data == b"":
            assert self.prepared.handed_off and self.payload_bytes > 0
            assert self.marker(INTERACTIVE_READY) in self.raw_stdout
            self.handoff_eof = True
        self.interact()
        return data

    def try_write(self, data: memoryview) -> int | None:
        assert self.prepared is not None
        try:
            written = self.prepared.stdout.try_write(data)
        except BaseException:
            # Failed collection has no acknowledged count. Keep this last offered
            # chunk only for fixture reflection assertions, then stop the attempt.
            assert len(self.raw_stdout) + len(data) <= _MAX_OUTPUT
            self.raw_stdout.extend(data)
            raise
        if written is not None:
            assert len(self.raw_stdout) + written <= _MAX_OUTPUT
            self.raw_stdout.extend(data[:written])
        self.interact()
        return written

    def interact(self) -> None:
        if self.application_ready and self.handoff_eof and not self.keys_written:
            import termios

            assert self.prepared is not None and self.prepared.handed_off
            assert os.write(self.master, _KEYS) == len(_KEYS)
            self.keys_written = True
            termios.tcsetwinsize(self.slave, (42, 113))

    def no_reflection(self) -> None:
        for observed in (self.raw_stdout, self.presentation.data, self.diagnostics.data):
            assert all(tag not in observed for tag in (_SOURCE_TAG, _ENV_TAG, _ARG_TAG))


def _guest_absent(trace: Trace) -> None:
    """Observe only the positively reported fixture guest, without signaling it."""
    identities = [record for record in trace.records if "pid" in record]
    assert len(identities) == 1 and trace.guest_until is not None, "Guest cleanup identity is unknown"
    record = identities[0]
    pid, born = record["pid"], record["born"]
    assert type(pid) is int and isinstance(born, str) and record["uid"] == os.getuid()
    stat = Path(f"/proc/{pid}/stat")
    while True:
        try:
            observed = stat.read_text().rsplit(")", 1)[1].split()
            if observed[19] != born:
                return
            assert stat.stat().st_uid == os.getuid(), "Guest cleanup ownership is unobservable"
        except FileNotFoundError:
            return
        assert time.monotonic() < trace.guest_until, "Owned guest remains beyond its finite cleanup bound"
        time.sleep(0.01)


def run_case(root: Path, monkeypatch: pytest.MonkeyPatch, *, refusal: bool) -> None:
    """Run only within the retained fixture guard under a separate native charter."""
    import fcntl
    import termios

    workdir = root / "terminal-fixture"
    workdir.mkdir(mode=0o700)
    clients: list[subprocess.Popen[bytes]] = []
    owned_fds: list[int] = []
    servers: list[subprocess.Popen[bytes]] = []
    server_logs: list[Any] = []
    native_launch, native_acquire = subprocess.Popen, PosixTerminal.acquire
    trace: Trace | None = None
    custody = LocalDeliveryCustody()
    deadline = Deadline.after(120)

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = cast("subprocess.Popen[bytes]", native_launch(*args, **kwargs))
        if "-tt" in args[0]:
            clients.append(process)
        elif "-D" in args[0] and "-f" in args[0]:
            servers.append(process)
            server_logs.append(kwargs["stderr"])
        return process

    def acquire(terminal: PosixTerminal) -> None:
        native_acquire(terminal)
        owned_fds.extend((terminal.master_fd, terminal.slave_fd))

    primary: BaseException | None = None
    try:
        (workdir / "server").mkdir(mode=0o700)
        (workdir / "home").mkdir(mode=0o700)
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            patch.setattr(PosixTerminal, "acquire", acquire)
            with enrollment_server(workdir / "server", "matching") as server:
                assert not deadline.expired
                trust = admit_connection(server.connection)
                try:
                    executable = resolve_client_executable(server.connection)
                    assert check_client_version(executable, deadline=deadline, custody=custody) is None
                finally:
                    assert custody.close(Deadline.after(3))
                with ExitStack() as endpoints:
                    master, slave = os.openpty()
                    before = termios.tcgetattr(slave), fcntl.fcntl(slave, fcntl.F_GETFL), os.get_inheritable(slave)

                    def close_endpoint() -> None:
                        # One aggregate finalizer cannot close a borrowed terminal
                        # while the retained worker still owns acquisition or cleanup.
                        assert custody.close(Deadline.after(3))
                        termios.tcsetattr(slave, termios.TCSANOW, before[0])
                        os.close(slave)
                        os.close(master)

                    endpoints.callback(close_endpoint)
                    os.set_blocking(master, False)
                    termios.tcsetwinsize(slave, (31, 97))
                    trace = Trace(master, slave)
                    runtime = str(workdir / "missing-interpreter") if refusal else sys.executable
                    prepared = prepare_terminal_handoff(
                        (
                            os.fsencode(sys.executable),
                            b"-I",
                            b"-S",
                            b"-B",
                            b"-c",
                            GUEST_SOURCE.encode(),
                            _ARG_TAG,
                            str(len(_KEYS)).encode(),
                        ),
                        {b"PROBE_SECRET": _ENV, b"HOME": os.fsencode(workdir / "home")},
                        _SOURCE,
                        trace.presentation,
                        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, explicit_path=runtime),
                    )
                    trace.prepared = prepared
                    io = prepared.carrier_io(
                        input_fd=slave, output_fd=slave, term="xterm", diagnostics=trace.diagnostics
                    )
                    assert isinstance(io.input, TerminalInput) and isinstance(io.output, SinkOutput)
                    io = replace(io, input=replace(io.input, bootstrap=trace), output=replace(io.output, stdout=trace))
                    argv = build_ssh_argv(
                        server.connection, prepared.invocation, trust=trust, executable=executable, terminal=True
                    )
                    assert all(tag.decode() not in arg for tag in (_SOURCE_TAG, _ENV_TAG, _ARG_TAG) for arg in argv)
                    prepared.claim()
                    try:
                        result = run_terminal_relay_candidate(argv, io=io, deadline=deadline, custody=custody)
                    finally:
                        assert custody.close(Deadline.after(3))
                        # This is readiness finalization, never emulator sanitation.
                        prepared.stdout.finish()
                    trace.no_reflection()
                    assert len(clients) == 1 and result.started
                    assert result.stdout.data == result.stderr.data == b""
                    if refusal:
                        assert prepared.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
                        assert not prepared.handed_off and prepared.failure is TerminalHandoffFailure.PREREQUISITE
                        assert trace.payload_bytes == 0 and not trace.keys_written and not trace.records
                        assert result.failure is Failure.OUTPUT
                    else:
                        assert result.exit_status == result.local_status == 0 and result.failure is None
                        assert result.stdout.complete and result.stderr.complete
                        assert prepared.handed_off and prepared.failure is None and trace.keys_written
                        initial = next(record for record in trace.records if "initial" in record)
                        assert initial["initial"] == [31, 97] and initial["tty"] is True
                        assert initial["proof"] == [
                            hashlib.sha256(value).hexdigest() for value in (_SOURCE, _ENV, _ARG_TAG)
                        ]
                        assert [record["resize"] for record in trace.records if "resize" in record] == [
                            [42, 113],
                            [58, 144],
                        ]
                        assert all(
                            record["signal"] == signal.SIGWINCH for record in trace.records if "resize" in record
                        )
                        assert trace.records[-1] == {"keys": _KEYS.hex(), "done": True}
                    for process in clients:
                        assert process.returncode is not None
                        try:
                            os.waitpid(process.pid, os.WNOHANG)
                        except ChildProcessError:
                            pass
                        else:
                            raise AssertionError("Owned SSH client was not reaped")
                        assert all(
                            pipe is None or pipe.closed for pipe in (process.stdin, process.stdout, process.stderr)
                        )
                    for fd in owned_fds:
                        try:
                            os.fstat(fd)
                        except OSError:
                            pass
                        else:
                            raise AssertionError("Relay-owned PTY descriptor remains open")
                    assert_preserved_terminal_mode(termios.tcgetattr(slave), before[0])
                    assert (fcntl.fcntl(slave, fcntl.F_GETFL), os.get_inheritable(slave)) == before[1:]
        assert len(servers) == 1 and servers[0].returncode is not None
        assert len(server_logs) == 1 and server_logs[0].closed
        try:
            os.waitpid(servers[0].pid, os.WNOHANG)
        except ChildProcessError:
            pass
        else:
            raise AssertionError("Owned SSH server was not reaped")
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if (
                clients
                and trace is not None
                and trace.prepared is not None
                and trace.prepared.runtime_prerequisite.state is not RuntimePrerequisiteState.MISSING
            ):
                try:
                    _guest_absent(trace)
                except BaseException:
                    if primary is None:
                        raise
                    primary.add_note("Fixture guest cleanup remains uncertain")
        finally:
            shutil.rmtree(workdir)
            assert not workdir.exists()
