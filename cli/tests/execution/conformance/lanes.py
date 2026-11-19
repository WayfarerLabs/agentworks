"""The three lanes the conformance vectors run on.

- **L** runs ``$SHELL -c <remote command>`` locally, through the real
  ``VMAccess`` and pump, with ``ssh_process.build_argv`` replaced by a local
  shell behind a stand-in for ssh and sshd (:data:`SSH_STAND_IN`). The
  account's startup files are set per vector and ``SSH_CLIENT`` is exported as
  sshd does. It covers the shells and runs on every Linux CI host; elsewhere it
  skips, since agw-launch is a Linux guest program.
- **S** runs the real client against a rootless local ``sshd`` (loopback, a
  temporary host key and ``AuthorizedKeysFile``, no PAM) and ``trust.write_records``
  for the test host key. It skips when no ``sshd`` binary exists, and fails on CI;
  its vectors skip off Linux, as L does.
- **V** runs against a live VM named by ``AGW_CONFORMANCE_VM``, as the operator's
  own configured admin. It is the integration lane, run by the tester from each
  workstation.
"""

from __future__ import annotations

import contextlib
import getpass
import io
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import attach as attach_module
from agentworks.execution import client, envelope, ssh_process, trust
from agentworks.execution.access import Admin, VMAccess
from agentworks.execution.requests import Bytes, Command, RunRequest, Script, Shell
from agentworks.execution.results import Completed, Result
from tests.vm_access_support import local_remote_argv, rewrite_launcher, run_requests_locally

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from typing import NoReturn

    from agentworks.config import Config
    from agentworks.db import VMRow

SHELLS = ("sh", "dash", "bash", "zsh")
STARTUP_TIMEOUT_SECONDS = 15

SSH_STAND_IN = (
    "import subprocess, sys\n"
    "status = subprocess.Popen(sys.argv[1:], start_new_session=True).wait()\n"
    "sys.exit(255 if status < 0 else status)\n"
)
"""The L lane's client: it runs the account shell as sshd does, and reports its status as ssh does.

sshd starts the account shell in a session of its own, and when the client dies
it only closes the shell's streams. A shell that execs its one command (bash and
zsh do) would otherwise be the client itself, and ending the client would
SIGKILL the launcher, which never happens on a guest. ssh exits 255 when the
remote command is killed by a signal. Unlike ssh, it does not wait for the
guest's streams to close, so S is the lane that covers attach's end-then-kill
path for a background child.
"""


@dataclass(frozen=True)
class Streamed:
    """What ``VMAccess.stream`` produced, as the operator's streams would have seen it."""

    status: int
    stdout: bytes
    stderr: bytes


class TerminalHalf:
    """What the stand-in terminal half did: its input, its output and its exit status."""

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.stdout = b""
        self.stderr = b""
        self.status: int | None = None
        self.calls = 0


class Lane:
    """What a vector may rely on, whichever lane it runs on."""

    name = "?"
    access: VMAccess
    workdir = "/tmp"
    """A directory that exists on the guest and is the same for every request."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch

    # -- Requests ------------------------------------------------------------

    def run(self, request: RunRequest) -> Result:
        return self.access.run(request)

    def stream(self, request: RunRequest, stdin: bytes | None = b"") -> Streamed:
        out, err = io.BytesIO(), io.BytesIO()
        status = self.access.stream(request, stdin=None if stdin is None else io.BytesIO(stdin), stdout=out, stderr=err)
        return Streamed(status, out.getvalue(), err.getvalue())

    # -- The guest's filesystem (local for L and S, over the wire for V) ------

    def make_dir(self, files: Mapping[str, str] | None = None) -> str:
        raise NotImplementedError

    def make_script(self, content: str, *, mode: int = 0o755) -> str:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        raise NotImplementedError

    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def remove_dir(self, path: str) -> None:
        raise NotImplementedError

    def wait_gone(self, path: str, seconds: float = 5.0) -> bool:
        return self._poll(lambda: not self.exists(path), seconds)

    def wait_present(self, path: str, seconds: float = 5.0) -> bool:
        return self._poll(lambda: self.exists(path), seconds)

    @staticmethod
    def _poll(condition: Callable[[], bool], seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return condition()

    # -- The terminal half ---------------------------------------------------

    @contextlib.contextmanager
    def terminal(
        self, script: bytes = b"exit 0\n", *, behavior: Callable[[list[str], TerminalHalf], int] | None = None
    ) -> Iterator[TerminalHalf]:
        """Replace the terminal half with a subprocess fed ``script`` on stdin, or with ``behavior``."""
        half = TerminalHalf()

        def run_terminal(argv: Sequence[str]) -> int:
            half.calls += 1
            half.argv = list(argv)
            if behavior is not None:
                half.status = behavior(half.argv, half)
                return half.status
            completed = subprocess.run(
                list(argv),
                input=script,
                capture_output=True,
                timeout=60,
                env=ssh_process.child_environment(),
                check=False,
            )
            half.stdout, half.stderr, half.status = completed.stdout, completed.stderr, completed.returncode
            return completed.returncode

        self.monkeypatch.setattr(ssh_process, "run_terminal", run_terminal)
        yield half

    def handover_token(self) -> str:
        """Fix the next handover's token so a vector can look for what it should have left."""
        token = uuid.uuid4().hex[:16]
        self.monkeypatch.setattr(envelope, "new_handover_token", lambda: token)
        return token

    def shorten_parking_wait(self, seconds: float = 2.0) -> None:
        """Make the client give up on a parking connection that never reports ``parked`` after ``seconds``."""
        self.monkeypatch.setattr(attach_module, "PARKING_WAIT_SECONDS", seconds)

    def rewrite_launcher(self, old: bytes, new: bytes) -> None:
        """Send a launcher whose source has ``old`` replaced by ``new``, to make the guest behave as a fault would."""
        rewrite_launcher(self.monkeypatch, old, new)

    def access_with_identity(self, identity_file: Path) -> VMAccess:
        """The lane's access to the same account with another key, for a vector about the key itself."""
        raise NotImplementedError

    def access_with_log(self, log: Callable[[RunRequest, Result], None] | None) -> VMAccess:
        """The lane's access to the same account, with an operation log, for a vector about what a log holds."""
        raise NotImplementedError

    def shorten_handover_wait(self, seconds: int = 1) -> None:
        """Send a launcher whose parked half gives up after ``seconds``, instead of 30."""
        self.rewrite_launcher(b"HANDOVER_WAIT_SECONDS = 30", b"HANDOVER_WAIT_SECONDS = %d" % seconds)

    # -- A pane: a terminal agw does not hold ----------------------------------

    def quiet_interactive_startup(self) -> None:
        """Keep an interactive shell on the lane from reading any startup file of a person's (a no-op on a VM)."""

    def pane(self, command: str, *, owner_env: Mapping[str, str], keys: bytes) -> Result:
        """Run ``command`` on a fresh terminal, as tmux runs a pane's command, typing ``keys`` into it.

        The terminal is ``script``'s pty on the guest, and ``owner_env`` is
        what its owner sets in the command's environment, as tmux sets
        ``TERM`` from ``default-terminal`` and ``TMUX`` for its server. The
        result's stdout is what the terminal showed, echo and prompts
        included.
        """
        # script runs the command line through $SHELL -c, as tmux runs a pane's through its default shell.
        argv = ("script", "-q", "-e", "-c", self.pane_command(command), "/dev/null")
        return self.access.run(Command(argv, env=dict(owner_env), stdin=Bytes(keys), deadline=60))

    def pane_command(self, command: str) -> str:
        """``command`` as the guest runs it: the L lane points stage 0 at its own interpreter."""
        return command


class LocalFilesystem(Lane):
    """Lanes whose guest is this machine."""

    scratch: Path

    def quiet_interactive_startup(self) -> None:
        """Send a launcher whose passwd entry has a home of the lane's, not the runner's own.

        Its zsh startup keeps an interactive zsh from reading the keys a
        vector types: ``.zshenv`` sets ``skip_global_compinit``, so Ubuntu's
        ``/etc/zsh/zshrc`` runs no ``compinit``, whose insecure-directories
        prompt takes the first key on a CI runner; an empty ``.zshrc`` keeps
        zsh from starting its new-user setup.
        """
        home = self.make_dir({".zshenv": "skip_global_compinit=1\n", ".zshrc": ""})
        self.rewrite_launcher(b"home = entry.pw_dir", b"home = " + repr(home).encode())

    def make_dir(self, files: Mapping[str, str] | None = None) -> str:
        directory = self.scratch / f"dir-{uuid.uuid4().hex[:8]}"
        directory.mkdir()
        for name, content in (files or {}).items():
            (directory / name).write_text(content)
        return str(directory)

    def make_script(self, content: str, *, mode: int = 0o755) -> str:
        path = self.scratch / f"script-{uuid.uuid4().hex[:8]}"
        path.write_text(content)
        path.chmod(mode)
        return str(path)

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def mkdir(self, path: str) -> None:
        os.mkdir(path)

    def remove_dir(self, path: str) -> None:
        shutil.rmtree(path, ignore_errors=True)


# -- L ----------------------------------------------------------------------------------


def find_shell(name: str) -> str | None:
    return shutil.which(name)


def require_linux_guest() -> None:
    """Skip unless this machine can stand in for the guest: the L and S lanes run agw-launch here.

    agw-launch is a Linux guest program (it stages a script with
    ``os.memfd_create`` and reads ``/proc``), so on macOS or Windows those
    lanes would test a launcher where it never runs. It is not a missing
    prerequisite to install, so it skips on CI too; CI's lanes run on Linux.
    """
    if sys.platform != "linux":
        pytest.skip("the L and S lanes run agw-launch, a Linux guest program, on this machine")


def unavailable(message: str) -> NoReturn:
    """What a lane's missing prerequisite does: fail on CI, where the workflow installs them, and skip elsewhere."""
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


def sudo_granted() -> bool:
    """Whether this machine's user may run ``sudo -n`` without a password."""
    sudo = shutil.which("sudo")
    if sudo is None:
        return False
    return subprocess.run([sudo, "-n", "true"], capture_output=True, check=False).returncode == 0


def needs_sudo(lane: Lane) -> None:
    """A local guest elevates only where the runner grants passwordless sudo: CI does, so it fails there without it."""
    if isinstance(lane, LocalFilesystem) and not sudo_granted():
        unavailable("the elevation vectors need passwordless sudo")


def local(lane: Lane) -> LocalLane:
    if not isinstance(lane, LocalLane):
        pytest.skip("local lane only")
    return lane


def needs_startup_files(lane: LocalLane) -> None:
    """sh and dash read no startup file under ``-c``, so a vector about startup files has nothing to test there."""
    if lane.shell_name not in ("bash", "zsh"):
        pytest.skip(f"{lane.shell_name} reads no startup file for a non-interactive command")


def startup_file(lane: LocalLane) -> str:
    return ".zshenv" if lane.shell_name == "zsh" else ".bashrc"


def require_shell(name: str) -> str:
    """The shell's path; a missing one fails on CI and skips elsewhere."""
    found = find_shell(name)
    if found is None:
        unavailable(f"conformance needs {name}")
    return found


def python_interpreters() -> dict[str, str]:
    """The interpreters the L lane runs under, by label: the system python3 and a 3.11.

    ``AGW_CONFORMANCE_PYTHON311`` names a 3.11 interpreter (CI installs one with
    ``uv python install 3.11``). Without it, ``python3.11`` on PATH is used. An
    interpreter the label already covers is not repeated.
    """
    candidates = {
        "system": ssh_process.DEFAULT_PYTHON,
        "py311": os.environ.get("AGW_CONFORMANCE_PYTHON311") or shutil.which("python3.11") or "",
    }
    found: dict[str, str] = {}
    seen: set[str] = set()
    for label, path in candidates.items():
        if path and Path(path).exists() and os.path.realpath(path) not in seen:
            seen.add(os.path.realpath(path))
            found[label] = path
    return found


class LocalLane(LocalFilesystem):
    """L: the real ``VMAccess`` over a local ``$SHELL -c``, with the account's startup files under test control."""

    name = "L"

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shell: str, python: str) -> None:
        super().__init__(monkeypatch)
        self.shell_name = shell
        self.shell = require_shell(shell)
        self.python = python
        self.scratch = tmp_path
        self.home = tmp_path / "account-home"
        self.home.mkdir()
        self.workdir = str(tmp_path)
        self.access = self.access_with_log(None)
        run_requests_locally(monkeypatch, self._remote_argv)
        if python != ssh_process.DEFAULT_PYTHON:
            # The guest's python3 is this interpreter for the payload's Python programs too.
            self.rewrite_launcher(
                b'program_interpreter = "/usr/bin/python3"', b"program_interpreter = " + repr(python).encode()
            )

    def use_lane_shell_as_account_shell(self) -> None:
        """Send a launcher whose passwd entry names this lane's shell, as the account's default shell.

        The runner's own account shell is whatever it is; a vector about the
        default shell's startup needs the one the lane is parametrized by.
        """
        self.rewrite_launcher(b'shell = entry.pw_shell or "/bin/sh"', b"shell = " + repr(self.shell).encode())

    def access_with_log(self, log: Callable[[RunRequest, Result], None] | None) -> VMAccess:
        return VMAccess(
            vm_name="local",
            host="127.0.0.1",
            user=getpass.getuser(),
            identity=Admin(),
            identity_file=Path("/nonexistent/identity"),
            log=log,
        )

    def startup(self, name: str, content: str) -> None:
        """Write an account startup file (``.zshenv``, ``.bashrc``) read by the account shell."""
        (self.home / name).write_text(content)

    def quiet_interactive_startup(self) -> None:
        """As for every local lane, with the lane's shell as the account's, so each shell is tried interactive."""
        super().quiet_interactive_startup()
        self.use_lane_shell_as_account_shell()

    def pane_command(self, command: str) -> str:
        return command.replace(ssh_process.DEFAULT_PYTHON, shlex.quote(self.python), 1)

    def _remote_argv(self, remote_command: str) -> list[str]:
        guest = local_remote_argv(remote_command, home=self.home, shell=self.shell, python=self.python)
        return [sys.executable, "-I", "-S", "-c", SSH_STAND_IN, *guest]


# -- S ----------------------------------------------------------------------------------


def find_sshd() -> str | None:
    """``sshd`` is in ``/usr/sbin``, which an unprivileged user's PATH often lacks."""
    return shutil.which("sshd") or next(
        (p for p in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if Path(p).exists()), None
    )


def generate_key(path: Path) -> Path:
    """A fresh unencrypted ed25519 key at ``path``, from the ``ssh-keygen`` beside the resolved client."""
    keygen = client.tool_beside_ssh(client.resolve_ssh(), "ssh-keygen")
    subprocess.run([str(keygen), "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return path


@dataclass(frozen=True)
class SSHDServer:
    port: int
    client_key: Path
    host_key: trust.HostKey


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


RESTART_ATTEMPTS = 5
"""How often an sshd started on a given port tries: another worker's process may briefly hold the port."""


@contextlib.contextmanager
def rootless_sshd(directory: Path, port: int | None = None) -> Iterator[SSHDServer]:
    """A loopback sshd run by the current user, with a temporary host key and authorized key.

    Started again on the same ``directory`` and ``port``, it keeps the keys it
    made there, so a client that trusted the first server reaches the second
    as the same VM; a port it cannot bind is tried again, with a backoff.
    """
    sshd = find_sshd()
    if sshd is None:
        unavailable("S lane needs an sshd binary")
    directory.mkdir(parents=True, exist_ok=True)
    host_key, client_key = directory / "host_key", directory / "client_key"
    for key in (host_key, client_key):
        if not key.exists():
            generate_key(key)
    authorized = directory / "authorized_keys"
    authorized.write_text(Path(f"{client_key}.pub").read_text())
    attempts = 1 if port is None else RESTART_ATTEMPTS
    port = port or free_port()
    config = directory / "sshd_config"
    config.write_text(
        "\n".join(
            [
                "ListenAddress 127.0.0.1",
                f"Port {port}",
                f'HostKey "{host_key}"',
                f'AuthorizedKeysFile "{authorized}"',
                f'PidFile "{directory / "sshd.pid"}"',
                "UsePAM no",
                "StrictModes no",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PrintMotd no",
                "PrintLastLog no",
                "LogLevel ERROR",
            ]
        )
        + "\n"
    )
    failure = b""
    for attempt in range(attempts):
        if attempt:
            time.sleep(0.2 * 2**attempt)
        server = subprocess.Popen(
            [sshd, "-D", "-e", "-f", str(config)], stdin=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        if _listening(server, port, directory / "sshd.pid"):
            break
        failure = _stop(server)
    else:
        unavailable(f"sshd could not start listening on port {port}: {failure[:300]!r}")
    try:
        key_fields = Path(f"{host_key}.pub").read_text().split()
        yield SSHDServer(port, client_key, trust.HostKey(key_fields[0], key_fields[1]))
    finally:
        _stop(server)


def _listening(server: subprocess.Popen[bytes], port: int, pid_file: Path) -> bool:
    """Whether ``server`` itself listens on ``port`` before it exits or the startup bound passes.

    A connection alone proves nothing: another process holding the port
    accepts it while this sshd fails to bind and exits. sshd writes its pid
    file only once it has bound its address, so the file naming this
    server's pid, with the server still running, is the proof; the
    connection then shows it accepts.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while server.poll() is None and time.monotonic() < deadline:
        if _pid_in(pid_file) == server.pid:
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return server.poll() is None
        time.sleep(0.1)
    return False


def _pid_in(pid_file: Path) -> int | None:
    """The pid an sshd pid file names, or ``None`` while there is none or it is still being written."""
    try:
        return int(pid_file.read_text())
    except (OSError, ValueError):
        return None


def _stop(server: subprocess.Popen[bytes]) -> bytes:
    """Stop ``server`` and return what it wrote to stderr."""
    server.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        server.wait(timeout=5)
    server.kill()
    server.wait()
    if server.stderr is None:
        return b""
    with server.stderr:
        return server.stderr.read()


class SSHDLane(LocalFilesystem):
    """S: the real ``ssh`` client and ``VMAccess`` against a rootless local sshd."""

    name = "S"

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, server: SSHDServer, vm_name: str = "conformance"
    ) -> None:
        super().__init__(monkeypatch)
        client.resolve_ssh.cache_clear()
        self.client = client.resolve_ssh()
        self.server = server
        self.vm_name = vm_name
        self.scratch = tmp_path
        self.workdir = str(tmp_path)
        trust.write_records(vm_name, [server.host_key])
        self.access = self.access_with_identity(server.client_key)

    def access_with_identity(
        self, identity_file: Path, log: Callable[[RunRequest, Result], None] | None = None
    ) -> VMAccess:
        return VMAccess(
            vm_name=self.vm_name,
            host="127.0.0.1",
            user=getpass.getuser(),
            identity=Admin(),
            identity_file=identity_file,
            port=self.server.port,
            log=log,
        )

    def access_with_log(self, log: Callable[[RunRequest, Result], None] | None) -> VMAccess:
        return self.access_with_identity(self.server.client_key, log)


# -- V ----------------------------------------------------------------------------------


class LiveLane(Lane):
    """V: a live VM named by ``AGW_CONFORMANCE_VM``, reached as the operator's configured admin."""

    name = "V"
    workdir = "/tmp"

    def __init__(self, monkeypatch: pytest.MonkeyPatch, vm: VMRow, config: Config) -> None:
        super().__init__(monkeypatch)
        self.vm = vm
        self.config = config
        self.access = self.access_with_log(None)
        self._created: list[str] = []

    def access_with_log(self, log: Callable[[RunRequest, Result], None] | None) -> VMAccess:
        from agentworks.execution import vm_access

        return vm_access(self.vm, self.config, Admin(), log=log)

    def access_with_identity(self, identity_file: Path) -> VMAccess:
        return VMAccess(
            vm_name=self.vm.name,
            host=self.vm.tailscale_host,
            user=self.vm.admin_username,
            identity=Admin(),
            identity_file=identity_file,
        )

    def _sh(self, source: str) -> Result:
        result = self.access.run(Script(source, Shell.SH, deadline=60))
        assert isinstance(result.completion, Completed), result
        return result

    def make_dir(self, files: Mapping[str, str] | None = None) -> str:
        path = f"/tmp/agw-conformance-{uuid.uuid4().hex[:8]}"
        self._sh(f"mkdir {path}")
        self._created.append(path)
        for name, content in (files or {}).items():
            self.access.run(
                Command(("sh", "-c", f'printf %s "$1" > {path}/{name}', "sh", content), deadline=60), check=True
            )
        return path

    def make_script(self, content: str, *, mode: int = 0o755) -> str:
        path = f"/tmp/agw-conformance-{uuid.uuid4().hex[:8]}"
        self.access.run(
            Command(("sh", "-c", f'printf %s "$1" > {path} && chmod {mode:o} {path}', "sh", content), deadline=60),
            check=True,
        )
        self._created.append(path)
        return path

    def exists(self, path: str) -> bool:
        return self._sh(f"test -e {path}").completion == Completed(0)

    def mkdir(self, path: str) -> None:
        self._sh(f"mkdir {path}")
        self._created.append(path)

    def remove_dir(self, path: str) -> None:
        self._sh(f"rm -rf {path}")

    def cleanup(self) -> None:
        for path in self._created:
            with contextlib.suppress(Exception):
                self._sh(f"rm -rf {path}")


def live_vm_name() -> str | None:
    return os.environ.get("AGW_CONFORMANCE_VM") or None
