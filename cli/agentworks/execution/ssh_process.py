"""Build the ssh client's argv and run the client.

This is the only module that spawns ``ssh``. It holds the VM policy, stage 0
(the constant command line every request starts with), the local deadline and
the pump: a single-threaded loop that drives one client process through its
non-blocking pipes. The pump knows nothing about ssh, which is what lets the
conformance suite run it against a local shell.

A deadline bounds the local wait and never claims the guest command stopped.
"""

from __future__ import annotations

import contextlib
import enum
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv6Address
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution import trust
from agentworks.execution.envelope import StreamFilter, classify
from agentworks.execution.errors import ClientUnsupported
from agentworks.execution.results import Completion, Result, Unknown, UnknownReason

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from typing import BinaryIO

    from agentworks.execution.client import SSHClient
    from agentworks.execution.requests import Forward
    from agentworks.execution.trust import TrustBinding

# Stage 0, the program every request's connection starts with. `f` is stdin, or
# the connected socket on the handover form (`a` is the socket path); `r(n)`
# reads exactly n bytes with os.read, so stdin is left at the next byte and no
# buffered reader touches it; `g` is the launcher source, which exec then runs.
# End of file raises, so any short or empty input is a traceback and exit 1
# before a marker. The program has no single quote (which would end the account
# shell's quoting), no `!` (csh expands it even inside single quotes), no double
# quote, no backslash and no newline, so Windows argument quoting has nothing
# to escape.
STAGE0_PROGRAM = (
    "import os,sys,socket as S;a=sys.argv[1:];s=a and S.socket(1);a and s.connect(a[0]);"
    "f=a and s.fileno()or 0;r=lambda n:n and(lambda c:c+r(n-len(c)))(os.read(f,n)or 1/0)or bytes();"
    "exec(g:=r(int(r(10))))"
)
DEFAULT_PYTHON = "/usr/bin/python3"

CONNECT_TIMEOUT_SECONDS = 30
SERVER_ALIVE_INTERVAL_SECONDS = 15
SERVER_ALIVE_COUNT_MAX = 4

CHUNK_BYTES = 65_536
CAPTURE_LIMIT_BYTES = 1_048_576
CLIENT_LOG_TAIL_LINES = 20
HOST_KEY_FAILURE = "Host key verification failed"

_POLL_SECONDS = 0.01
_NATURAL_EXIT_SECONDS = 1.0
_REAP_SECONDS = 0.5
_EXIT_DRAIN_SECONDS = 0.1
_STDIN_QUEUE_CHUNKS = 4

_HOST = re.compile(r"[A-Za-z0-9_:][A-Za-z0-9_.:-]*")
_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\$?")


def stage0_command(handover_socket: str | None = None, *, python: str = DEFAULT_PYTHON) -> str:
    """The remote command: ``python3 -I -S -c '<stage 0>'``, plus the socket path for a handover.

    ``-I`` ignores Python's environment variables and the user site directory,
    and ``-S`` skips site packages, so no Python startup applies.
    """
    command = f"{python} -I -S -c '{STAGE0_PROGRAM}'"
    return command if handover_socket is None else f"{command} {handover_socket}"


@dataclass(frozen=True)
class Deadline:
    """One local observation budget, not guest cancellation; None is unbounded."""

    expires_at: float | None

    def __post_init__(self) -> None:
        if self.expires_at is not None and not math.isfinite(self.expires_at):
            raise ValidationError("Deadline must be finite or explicitly unbounded")

    @classmethod
    def after(cls, seconds: float | None) -> Deadline:
        if seconds is None:
            return cls(None)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValidationError("Deadline duration must be finite and nonnegative")
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.monotonic())

    @property
    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0


# -- Policy and argv ---------------------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    """Where the client connects. Validated at the boundary the values come from: the database.

    The port is 22 except in tests, which run a local sshd.
    """

    host: str
    user: str
    port: int = 22

    def __post_init__(self) -> None:
        if not _HOST.fullmatch(self.host):
            raise ValidationError("SSH host must be a literal DNS name or unbracketed IP address")
        if ":" in self.host:
            try:
                IPv6Address(self.host)
            except ValueError:
                raise ValidationError("SSH IPv6 host must be an unbracketed address without a scope") from None
        if not _USER.fullmatch(self.user):
            raise ValidationError("SSH user must be a literal POSIX account name")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValidationError("SSH port must be an integer from 1 through 65535")


@dataclass(frozen=True)
class VMPolicy:
    """The policy for every connection to a VM: the operator's key and the VM's host-key trust."""

    identity_file: Path
    trust: TrustBinding

    def options(self) -> list[str]:
        """The ``-o`` arguments, with the trust record read now so a missing one is ``TrustMissing``.

        ``-F none`` (set by :func:`build_argv`) leaves everything else at its
        default, so this says only what the defaults do not: the explicit
        identity, publickey-only batch authentication, the trust options, and
        connect and keepalive timing.
        """
        identity = trust.literal_path(self.identity_file, "SSH identity file")
        values = [
            f'IdentityFile="{identity}"',
            "IdentitiesOnly=yes",
            # OpenSSH 8.5 reads CertificateFile=none as a relative file name:
            # https://github.com/openssh/openssh-portable/blob/V_8_5_P1/ssh.c#L2312-L2320
            # A child of the regular identity file cannot exist, so no certificate
            # loads, including the automatic .pub probe.
            f'CertificateFile="{identity}/disabled-certificate"',
            "BatchMode=yes",
            "PreferredAuthentications=publickey",
            "SecurityKeyProvider=none",
            f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
            f"ServerAliveInterval={SERVER_ALIVE_INTERVAL_SECONDS}",
            f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
        ]
        return [arg for value in values for arg in ("-o", value)] + list(trust.ssh_options(self.trust))


class ArgvMode(enum.Enum):
    """Which shape of client argv to build."""

    PIPE = enum.auto()
    TERMINAL = enum.auto()
    FORWARD = enum.auto()


def build_argv(
    client: SSHClient,
    policy: VMPolicy,
    destination: Destination,
    mode: ArgvMode,
    remote_command: str | None = None,
    forwards: Sequence[Forward] = (),
    *,
    client_log: Path | None = None,
    verbose: bool = False,
) -> list[str]:
    """The client's argument list. ``client`` is required, so no caller can launch a bare name.

    - ``PIPE``: ``-T`` (also beating an operator's ``RequestTTY force``), the
      remote command last, and ``-E`` when a client log is given.
    - ``TERMINAL``: ``-t`` and ssh's diagnostics on the operator's terminal.
      ``-tt`` would force a pty when stdin is not a terminal, which breaks
      ``echo ls | agw vm shell``.
    - ``FORWARD``: ``-N -n`` (no request, so no stdin to keep), ``-E`` or
      ``-v``, ``ExitOnForwardFailure`` and one ``-L`` per forward.

    No ``-n`` on a request-bearing connection: it would replace the request's
    stdin with the null device.
    """
    argv = [client.path, "-F", "none"]
    if mode is ArgvMode.PIPE:
        argv.append("-T")
    elif mode is ArgvMode.TERMINAL:
        argv.append("-t")
    else:
        argv += ["-N", "-n"]
    if mode is not ArgvMode.TERMINAL:
        if verbose:
            argv.append("-v")
        elif client_log is not None:
            argv += ["-E", str(client_log)]
    argv += policy.options()
    if mode is ArgvMode.FORWARD:
        argv += ["-o", "ExitOnForwardFailure=yes"]
        for forward in forwards:
            argv += ["-L", f"{forward.bind_address}:{forward.local_port}:localhost:{forward.remote_port}"]
    if destination.port != 22:
        argv += ["-p", str(destination.port)]
    argv += ["-l", destination.user, "--", destination.host]
    if remote_command is not None:
        argv.append(remote_command)
    return argv


# -- The client log -----------------------------------------------------------------------


@contextlib.contextmanager
def client_log() -> Iterator[Path]:
    """A private file for ssh's own diagnostics (``-E``), removed on every exit.

    ``mkstemp`` creates it 0600 in the system temporary directory, and the
    descriptor is closed before the spawn because a Windows unlink fails while a
    handle is open. The file never holds the remote's output.
    """
    descriptor, name = tempfile.mkstemp(prefix="agw-ssh-", suffix=".log")
    os.close(descriptor)
    path = Path(name)
    try:
        yield path
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def client_log_text(path: Path) -> str:
    """The whole client log, empty when unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def tail_lines(text: str, count: int = CLIENT_LOG_TAIL_LINES) -> str:
    """The last ``count`` non-empty lines of ``text``, safe to show on a terminal.

    ssh quotes strings the server chose (a pre-authentication banner), so
    control characters are neutralized.
    """
    from agentworks.terminal import sanitize_terminal_output

    lines = [line for line in text.splitlines() if line.strip()]
    return sanitize_terminal_output("\n".join(lines[-count:]))


# -- Spawning and the pump ----------------------------------------------------------------


def child_environment() -> dict[str, str] | None:
    """The client's environment: ``None`` (inherit) except on Windows.

    Windows OpenSSH reads two variables that describe its *parent's* handles,
    never our new pipes, so they are removed.
    https://github.com/PowerShell/openssh-portable/blob/v9.5.0.0/contrib/win32/win32compat/w32fd.c#L115-L128
    """
    if os.name != "nt":
        return None
    hidden = ("C28FC6F98A2C44ABBBD89D6A3037D0D9_POSIX_FD_STATE", "OPENSSH_STDIO_MODE")
    return {name: value for name, value in os.environ.items() if name.upper() not in hidden}


def run_terminal(argv: Sequence[str]) -> int:
    """Run the client on the operator's terminal and return its exit status."""
    try:
        return subprocess.call(list(argv), env=child_environment())
    except OSError as error:
        raise ClientUnsupported(f"the OpenSSH client could not be started: {error.strerror or error}") from error


Sink = Callable[[bytes], None]
"""Receives payload bytes as they are released."""


class OutputLimitExceeded(Exception):
    """A capture sink received more than its limit."""


class Capture:
    """A sink that keeps up to ``limit`` bytes and refuses more."""

    def __init__(self, limit: int = CAPTURE_LIMIT_BYTES) -> None:
        self.data = bytearray()
        self._limit = limit

    def __call__(self, chunk: bytes) -> None:
        self.data += chunk
        if len(self.data) > self._limit:
            del self.data[self._limit :]
            raise OutputLimitExceeded


class StopReason(enum.Enum):
    """Why :meth:`PipeSession.pump` returned."""

    DONE = enum.auto()
    """Both streams reached their closing line."""
    CONDITION = enum.auto()
    """The caller's stop condition became true."""
    EXITED = enum.auto()
    """The client exited and its pipes stopped producing."""
    EOF = enum.auto()
    """Both pipes reached end of file."""
    DEADLINE = enum.auto()
    OUTPUT_LIMIT = enum.auto()


class _InputFeeder:
    """Copies the operator's stdin into a queue from a daemon thread.

    A thread is needed because Windows cannot poll a console or pipe handle,
    and one mechanism serves every platform. It reads the raw stream because a
    daemon thread blocked in ``BufferedReader.read1`` aborts the interpreter at
    exit. The queue is bounded so a slow consumer does not buffer the stream.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=_STDIN_QUEUE_CHUNKS)
        self.exhausted = False

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="agw-stdin").start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(CHUNK_BYTES)
                if chunk is None:
                    time.sleep(_POLL_SECONDS)
                    continue
                self._queue.put(chunk)
                if not chunk:
                    return
        except (OSError, ValueError):
            self._queue.put(b"")

    def poll(self) -> bytes | None:
        """The next chunk, ``b""`` at end of input, or ``None`` when nothing is ready."""
        try:
            chunk = self._queue.get_nowait()
        except queue.Empty:
            return None
        if not chunk:
            self.exhausted = True
        return chunk


class PipeSession:
    """One client process driven through pipes, until the caller has what it needs.

    The session writes ``request`` and then the caller's ``stdin`` to the
    client, runs both output streams through :class:`StreamFilter` and passes
    payload bytes to the sinks. :meth:`pump` can be called more than once, which
    is how a terminal handover keeps its parking connection while the terminal
    half runs.

    The client's stdin closes once the request and the caller's input are sent,
    unless ``hold_stdin_open`` keeps it open for :meth:`close_stdin` (a parked
    launcher treats its closing as the client leaving).

    A write that fails to the client's stdin is the consumer ending early, not a
    failure: the session stops sending and keeps reading. A sink that raises
    ``OSError`` is dropped the same way.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        request: bytes,
        nonce: str,
        stdin: BinaryIO | None = None,
        hold_stdin_open: bool = False,
        on_stdout: Sink,
        on_stderr: Sink,
        deadline: Deadline | None = None,
    ) -> None:
        self._argv = list(argv)
        self._outbox = bytearray(request)
        self._feeder = None if stdin is None else _InputFeeder(stdin)
        self._sinks = {"out": on_stdout, "err": on_stderr}
        self._deadline = deadline or Deadline(None)
        self._hold_stdin_open = hold_stdin_open
        self._eof = {"out": False, "err": False}
        self._proc: subprocess.Popen[bytes] | None = None
        self.stdout = StreamFilter(nonce)
        self.stderr = StreamFilter(nonce)
        self.exit_status: int | None = None
        """The client's own exit status, when it exited by itself; ``None`` while it runs or if it was killed."""

    def __enter__(self) -> PipeSession:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._cleanup()

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=child_environment(),
            )
        except OSError as error:
            raise ClientUnsupported(f"the OpenSSH client could not be started: {error.strerror or error}") from error
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            assert pipe is not None
            os.set_blocking(pipe.fileno(), False)
        if self._feeder is not None:
            self._feeder.start()

    def pump(self, stop: Callable[[], bool] | None = None, *, budget: Deadline | None = None) -> StopReason:
        """Drive the client until a stop reason applies.

        ``budget`` bounds this call on top of the session's own deadline.
        """
        proc = self._proc
        assert proc is not None
        budget = budget or Deadline(None)
        last_activity = time.monotonic()
        while True:
            if self.exit_status is None:
                self.exit_status = proc.poll()
                if self.exit_status is not None:
                    last_activity = time.monotonic()
            try:
                progressed = self._read("out") | self._read("err")
            except OutputLimitExceeded:
                return StopReason.OUTPUT_LIMIT
            progressed |= self._write()
            if self.stdout.done and self.stderr.done:
                return StopReason.DONE
            if stop is not None and stop():
                return StopReason.CONDITION
            if self._deadline.expired or budget.expired:
                return StopReason.DEADLINE
            if self._eof["out"] and self._eof["err"]:
                return StopReason.EOF
            now = time.monotonic()
            if progressed:
                last_activity = now
                continue
            if self.exit_status is not None and now - last_activity >= _EXIT_DRAIN_SECONDS:
                return StopReason.EXITED
            waits = [_POLL_SECONDS, self._deadline.remaining(), budget.remaining()]
            time.sleep(min(wait for wait in waits if wait is not None))

    def close_stdin(self) -> None:
        """Close the client's stdin now: the guest then sees end of input."""
        self._outbox.clear()
        self._finish_input()

    def end(self) -> None:
        """End the client: close its stdin, wait up to a second for it to exit, then kill it."""
        self.close_stdin()
        proc = self._proc
        if proc is not None and self.exit_status is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_NATURAL_EXIT_SECONDS)
            self.exit_status = proc.poll()
        self._cleanup()

    def _read(self, which: str) -> bool:
        if self._eof[which]:
            return False
        proc = self._proc
        assert proc is not None
        pipe = proc.stdout if which == "out" else proc.stderr
        assert pipe is not None
        stream_filter = self.stdout if which == "out" else self.stderr
        try:
            chunk = os.read(pipe.fileno(), CHUNK_BYTES)
        except BlockingIOError:
            return False
        except OSError:
            chunk = b""
        payload = stream_filter.feed(chunk) if chunk else stream_filter.finish()
        if not chunk:
            self._eof[which] = True
        if payload:
            self._deliver(which, payload)
        return True

    def _deliver(self, which: str, payload: bytes) -> None:
        try:
            self._sinks[which](payload)
        except OSError:
            # The consumer ended early; keep reading so the command can finish.
            self._sinks[which] = _discard

    def _write(self) -> bool:
        proc = self._proc
        assert proc is not None
        stdin = proc.stdin
        if stdin is None or stdin.closed:
            return False
        progressed = False
        if not self._outbox and self._feeder is not None and not self._feeder.exhausted:
            chunk = self._feeder.poll()
            if chunk:
                self._outbox += chunk
                progressed = True
            elif chunk is not None:
                progressed = True
        if self._outbox:
            try:
                written = os.write(stdin.fileno(), bytes(self._outbox[:CHUNK_BYTES]))
            except BlockingIOError:
                return progressed
            except OSError:
                self._outbox.clear()
                self._finish_input()
                return True
            del self._outbox[:written]
            progressed |= written > 0
        if not self._hold_stdin_open and not self._outbox and (self._feeder is None or self._feeder.exhausted):
            self._finish_input()
            progressed = True
        return progressed

    def _finish_input(self) -> None:
        proc = self._proc
        if proc is not None and proc.stdin is not None and not proc.stdin.closed:
            with contextlib.suppress(OSError):
                proc.stdin.close()
        if self._feeder is not None:
            self._feeder.exhausted = True

    def _cleanup(self) -> None:
        """Close owned pipes and bound local kill and reap, even on interruption."""
        proc = self._proc
        if proc is None:
            return
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=_REAP_SECONDS)


def _discard(_payload: bytes) -> None:
    return None


def outcome(session: PipeSession, stop: StopReason, log: Path) -> Result:
    """The typed outcome of a finished session: a local limit, or the markers and ssh's status.

    The result carries no stream bytes; the caller's sinks hold those.
    """
    text = client_log_text(log)
    if stop is StopReason.DEADLINE:
        completion: Completion = Unknown(UnknownReason.DEADLINE)
    elif stop is StopReason.OUTPUT_LIMIT:
        completion = Unknown(UnknownReason.OUTPUT_LIMIT)
    else:
        completion = classify(
            session.stdout, session.stderr, session.exit_status, host_key_failed=HOST_KEY_FAILURE in text
        )
    return Result(completion, client_log_tail=tail_lines(text))
