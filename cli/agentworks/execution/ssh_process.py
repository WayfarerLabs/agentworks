"""Build the ssh client's argv and start the client.

This is the only module that spawns ``ssh``. It holds the VM policy, stage 0
(the constant command line every request starts with), the client log and
:func:`spawn`, the one place a client process is started. The pump that drives
a client through pipes is in ``pump.py``.
"""

from __future__ import annotations

import contextlib
import enum
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from ipaddress import IPv6Address
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution import trust
from agentworks.execution.envelope import classify
from agentworks.execution.errors import ClientUnsupported
from agentworks.execution.results import Completion, Result, Unknown

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from agentworks.execution.client import SSHClient
    from agentworks.execution.pump import PipeSession, StopReason
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

CLIENT_LOG_TAIL_LINES = 20
CLIENT_LOG_READ_BYTES = 65_536
HOST_KEY_FAILURE = "Host key verification failed"

_HOST = re.compile(r"[A-Za-z0-9_:][A-Za-z0-9_.:-]*")
_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\$?")


def stage0_command(handover_socket: str | None = None, *, python: str = DEFAULT_PYTHON) -> str:
    """The remote command: ``python3 -I -S -c '<stage 0>'``, plus the socket path for a handover.

    ``-I`` ignores Python's environment variables and the user site directory,
    and ``-S`` skips site packages, so no Python startup applies.
    """
    command = f"{python} -I -S -c '{STAGE0_PROGRAM}'"
    return command if handover_socket is None else f"{command} {handover_socket}"


# -- Policy and argv ---------------------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    """Where the client connects. Validated at the boundary the values come from: the database.

    The port is 22 except in tests, which run a local sshd; it is never operator input.
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
      ``-v`` when ``verbose`` (only a forward may be verbose),
      ``ExitOnForwardFailure`` and one ``-L`` per forward.

    No ``-n`` on a request-bearing connection: it would replace the request's
    stdin with the null device.
    """
    if verbose and mode is not ArgvMode.FORWARD:
        # In a pipe mode ssh's debug lines would reach the guest's stderr after the marker.
        raise ValueError("verbose output is only for a forward")
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
    """The end of the client log (its last 64 KiB), empty when unreadable.

    ssh's diagnostics are short, but ``-v`` or a long-lived forward can grow the
    file, and only its end is ever shown.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - CLIENT_LOG_READ_BYTES))
            return handle.read().decode("utf-8", errors="replace")
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


# -- Spawning, the terminal half and outcomes ------------------------------------------


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


def spawn(
    argv: Sequence[str], *, stdin: int | None, stdout: int | None, stderr: int | None, bufsize: int = -1
) -> subprocess.Popen[bytes]:
    """Start the client, or raise :class:`ClientUnsupported` when the operating system cannot.

    The one place a client process is started, so the environment strip and the
    error mapping exist once. ``None`` for a stream inherits the operator's.
    """
    try:
        return subprocess.Popen(
            list(argv), stdin=stdin, stdout=stdout, stderr=stderr, bufsize=bufsize, env=child_environment()
        )
    except OSError as error:
        raise ClientUnsupported(f"the OpenSSH client could not be started: {error.strerror or error}") from error


def exit_status(returncode: int) -> int:
    """A process's exit status as a shell reports it: 128 plus the signal for a client killed by one."""
    return 128 - returncode if returncode < 0 else returncode


def run_terminal(argv: Sequence[str]) -> int:
    """Run the client on the operator's terminal and return its exit status."""
    process = spawn(argv, stdin=None, stdout=None, stderr=None)
    try:
        return exit_status(process.wait())
    except BaseException:
        process.kill()
        process.wait()
        raise


def outcome(session: PipeSession, stop: StopReason, log: Path) -> Result:
    """The typed outcome of a finished session: a local limit, or the markers and ssh's status.

    The result carries no stream bytes; the caller's sinks hold those.
    """
    text = client_log_text(log)
    limit = stop.limit
    if limit is not None:
        completion: Completion = Unknown(limit)
    else:
        completion = classify(
            session.stdout,
            session.stderr,
            session.exit_status,
            host_key_failed=HOST_KEY_FAILURE in text,
            stdout_abandoned=session.stdout_abandoned,
            stderr_abandoned=session.stderr_abandoned,
        )
    return Result(completion, client_log_tail=tail_lines(text))
