"""SSH transport implementation.

Wraps ``ssh`` / ``scp`` subprocess calls in the ``Transport`` ABC's
surface. Holds the argv builders, SetEnv handling, sudo-wrap, and
login-shell wrapping for the canonical Tailscale SSH path.
``agentworks/ssh.py`` retains a small surface of SSH primitives
(``SSHTarget`` / ``SSHResult`` / ``SSHError`` / ``SSHLogger`` /
``LOG_DIR`` / module-level ``run`` / ``copy_to``) used by
``capabilities/vm_platform/lima.py`` against bare ``SSHTarget``s; everything
polymorphic-transport-shaped lives here.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

from agentworks.ssh import (
    SSH_DEFAULT_RETRIES,
    SSH_INTERACTIVE_ALIVE_COUNT_MAX,
    SSH_INTERACTIVE_ALIVE_INTERVAL,
    SSH_TRANSPORT_ERROR,
    SSHError,
    SSHResult,
    SSHTarget,
    _set_env_args,
)
from agentworks.ssh import (
    run as ssh_run,
)
from agentworks.subprocess_io import decode_stream, stdin_bytes
from agentworks.transports.base import Transport

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentworks.ssh import SSHLogger


def _decoded_output(value: str | bytes) -> str:
    """Normalize text- and byte-mode subprocess output to transport text."""
    return value if isinstance(value, str) else decode_stream(value)


def keepalive_args() -> list[str]:
    """Client-side keepalives for connections with no subprocess timeout.

    Without these, a call whose peer goes away silently (laptop
    suspends, lid closes, Wi-Fi drops) blocks in a TCP read until the
    kernel's own retransmit budget runs out, which can be many minutes
    and is not something the client bounds. For an attach that window is
    doubly bad: nothing can clean up the operator's terminal during it,
    because this process is parked inside ``subprocess.call``. The
    keepalives turn an unbounded hang into a bounded one and hand
    control back so the terminal guard in :mod:`agentworks.terminal`
    can run.

    Applied to both stdio-inheriting paths, ``interactive`` and
    ``call_streaming``: neither can carry a subprocess timeout, because
    there is no correct duration for "operator is using a shell" or for
    an arbitrary ``vm exec`` command. ``run`` is the exception that does
    not need them, since it passes an explicit timeout to subprocess and
    retries on expiry.
    """
    return [
        "-o",
        f"ServerAliveInterval={SSH_INTERACTIVE_ALIVE_INTERVAL}",
        "-o",
        f"ServerAliveCountMax={SSH_INTERACTIVE_ALIVE_COUNT_MAX}",
    ]


def note_ssh_interactive_exit(code: int, endpoint: str) -> None:
    """Explain a dropped interactive SSH connection.

    ssh reserves exit 255 for its own transport failures, so it is the
    one code that separates "the connection died" from "the remote
    command exited non-zero". A dropped attach is otherwise completely
    silent from the operator's side: the CLI just exits with the code,
    and ssh's own diagnostic went to the alternate screen that the
    terminal guard has since discarded.

    Shared by ``SSHTransport`` and ``RemoteLimaTransport``, whose outer
    hop is an ``ssh -t`` with the same failure mode. Callers invoke this
    from ``_note_interactive_exit``, which the ABC calls only after the
    guard has closed; see ``Transport._note_interactive_exit`` for why
    that ordering matters.
    """
    if code != SSH_TRANSPORT_ERROR:
        return
    from agentworks import output

    output.warn(f"connection to {endpoint} dropped (ssh exit {SSH_TRANSPORT_ERROR}); terminal restored.")
    output.detail("Remote state is untouched. Re-run the same command to reattach.")


def _scp_base_args(
    *,
    port: int | None,
    identity_file: Path | None,
    proxy_jump: str | None,
) -> list[str]:
    args = ["scp", "-q", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if port is not None:
        args.extend(["-P", str(port)])
    if identity_file is not None:
        args.extend(["-i", str(identity_file)])
    if proxy_jump is not None:
        args.extend(["-J", proxy_jump])
    return args


class SSHTransport(Transport):
    """SSH delivery: ``ssh`` for command exec and interactive shells,
    ``scp`` for file movement.

    Set ``user=None`` to defer to SSH config (used for VM-host
    connections where the host is defined in ``~/.ssh/config``).
    Explicit ``user`` is set for VM connections where we control the
    username. Set ``login_shell=True`` to wrap the remote command in
    ``$SHELL -lc <command>`` so the operator's per-shell PATH
    additions (e.g. Homebrew on macOS) resolve. Set ``force_tty=True``
    to default-allocate a TTY (Windows-zsh workaround); the per-call
    ``tty=`` parameter on ``run()`` overrides.
    """

    def __init__(
        self,
        host: str,
        *,
        user: str | None = None,
        port: int | None = None,
        identity_file: Path | None = None,
        proxy_jump: str | None = None,
        force_tty: bool = False,
        login_shell: bool = False,
        default_timeout: int | None = None,
        logger: SSHLogger | None = None,
        retries: int = SSH_DEFAULT_RETRIES,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.identity_file = identity_file
        self.proxy_jump = proxy_jump
        self.force_tty = force_tty
        self.login_shell = login_shell
        self.default_timeout = default_timeout
        self.logger = logger
        self.retries = retries

    # -- Internal helpers ------------------------------------------------

    def _ssh_base_args(
        self,
        *,
        force_tty: bool | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Build the base ``ssh`` argv with ``BatchMode=yes`` (no remote
        command yet). ``force_tty`` overrides ``self.force_tty`` for this
        call; ``None`` uses the constructor default.
        """
        args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
        effective_tty = self.force_tty if force_tty is None else force_tty
        if effective_tty:
            args.insert(1, "-tt")
        elif force_tty is False:
            # Override RequestTTY force from operator SSH configuration.
            args.insert(1, "-T")
        if self.port is not None:
            args.extend(["-p", str(self.port)])
        if self.identity_file is not None:
            args.extend(["-i", str(self.identity_file)])
        if self.proxy_jump is not None:
            args.extend(["-J", self.proxy_jump])
        args.extend(_set_env_args(env))
        target = f"{self.user}@{self.host}" if self.user else self.host
        args.append(target)
        return args

    def describe(self) -> str:
        endpoint = f"{self.user}@{self.host}" if self.user else self.host
        return f"ssh:{endpoint}"

    # -- Transport surface ----------------------------------------------

    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        tty: bool | None = None,
        check: bool = True,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        input_data: str | None = None,
        discard_output: bool = False,
        retries: int | None = None,
        on_retry: Callable[[int, int], None] | None = None,
    ) -> SSHResult:
        """Run ``command`` over SSH.

        Retries on connection-level timeouts; remote-command failures do
        not retry. ``retries=None`` uses ``self.retries``; pass a per-call
        budget for one-off probes that need a wider window (live-resource
        probes, reconnect checks, etc.). ``sudo=True`` wraps in
        ``sudo -n bash -c '<command>'``; ``login_shell=True`` on this
        transport wraps in ``$SHELL -lc '<command>'`` (applied after the
        sudo wrap if both are set).

        Sensitive ``input_text`` uses the shared SSH stdin primitive, which
        keeps the value out of argv and diagnostics and discards command
        output because an arbitrary remote command can reflect stdin.
        ``discard_output`` retains ordinary TTY selection while sending both
        process streams directly to the null device.
        """
        if input_text is not None and input_data is not None:
            raise ValueError("SSH input_text and input_data are mutually exclusive")
        if input_text is not None and discard_output:
            raise ValueError("SSH input_text cannot be combined with discard_output")
        if sudo:
            command = f"sudo -n bash -c {shlex.quote(command)}"

        t = self._resolve_timeout(timeout)
        attempts = retries if retries is not None else self.retries
        if input_text is not None:
            stdin_result = ssh_run(
                SSHTarget(
                    host=self.host,
                    user=self.user,
                    port=self.port,
                    identity_file=self.identity_file,
                    proxy_jump=self.proxy_jump,
                    login_shell=self.login_shell,
                    # The constructor's ``force_tty`` is a Windows-zsh
                    # workaround for interactive shells, which a
                    # non-interactive stdin write is not; forwarding it here
                    # would put a line discipline in front of a payload this
                    # branch promises to deliver byte-exact. An explicit
                    # ``tty=True`` still reaches ``ssh.run``'s refusal, since
                    # a caller asking for both is asking for a contradiction.
                    force_tty=tty if tty is not None else False,
                ),
                command,
                check=check,
                timeout=t,
                retries=attempts,
                on_retry=on_retry,
                env=env,
                input_text=input_text,
                # Sensitive stdin always needs a byte-exact non-TTY channel;
                # explicit false also defeats RequestTTY force in ssh_config.
                tty=False if tty is None else tty,
            )
            if self.logger is not None:
                self.logger.log_command(command, stdin_result)
            return stdin_result

        args = self._ssh_base_args(force_tty=tty, env=env)
        # Fence the remote command from ssh's option parser. Some
        # glibc-getopt platforms permute non-options to the end, so an
        # argv element starting with `-` (e.g. `--workspace` flowing
        # through `vm exec aavm1 --workspace ws1 pwd`) is misparsed as
        # an ssh client option. `--` makes ssh treat everything after
        # as the remote command.
        args.append("--")
        if self.login_shell:
            args.append(f"$SHELL -lc {shlex.quote(command)}")
        else:
            args.append(command)

        last_err: Exception | None = None
        for attempt in range(attempts):
            if attempt > 0 and on_retry is not None:
                on_retry(attempt, attempts)
            try:
                result: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]
                if input_data is None:
                    result = subprocess.run(
                        args,
                        stdout=subprocess.DEVNULL if discard_output else subprocess.PIPE,
                        stderr=subprocess.DEVNULL if discard_output else subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=t,
                    )
                else:
                    result = subprocess.run(
                        args,
                        input=stdin_bytes(input_data),
                        stdout=subprocess.DEVNULL if discard_output else subprocess.PIPE,
                        stderr=subprocess.DEVNULL if discard_output else subprocess.PIPE,
                        timeout=t,
                    )
            except subprocess.TimeoutExpired as err:
                last_err = err
                if self.logger is not None:
                    self.logger.log_timeout(command, attempt + 1, attempts)
                continue
            except OSError as err:
                raise SSHError(f"SSH command could not be executed: {command}") from err
            ssh_result = SSHResult(
                returncode=result.returncode,
                stdout="" if discard_output else _decoded_output(result.stdout),
                stderr="" if discard_output else _decoded_output(result.stderr),
            )
            if self.logger is not None:
                self.logger.log_command(command, ssh_result)
            if check and not ssh_result.ok:
                raise SSHError(
                    f"SSH command failed (exit {result.returncode}): {command}\nstderr: {ssh_result.stderr.strip()}"
                )
            return ssh_result

        msg = f"SSH command timed out after {attempts} attempts ({t}s each): {command}"
        if self.logger is not None:
            self.logger.log_error(msg)
        raise SSHError(msg) from last_err

    def _interactive(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Interactive SSH with ``-t`` (allocates a TTY) and no
        ``BatchMode``. Empty ``command`` opens a login shell.
        """
        args = ["ssh", "-t", "-o", "StrictHostKeyChecking=accept-new", *keepalive_args()]
        if self.port is not None:
            args.extend(["-p", str(self.port)])
        if self.identity_file is not None:
            args.extend(["-i", str(self.identity_file)])
        if self.proxy_jump is not None:
            args.extend(["-J", self.proxy_jump])
        args.extend(_set_env_args(env))
        target = f"{self.user}@{self.host}" if self.user else self.host
        args.append(target)
        if command:
            args.append("--")  # fence: see run() for rationale
            args.append(command)
        return subprocess.call(args)

    def _note_interactive_exit(self, code: int) -> None:
        note_ssh_interactive_exit(code, self.describe())

    def copy_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy a local file to ``remote_path`` via scp."""
        args = _scp_base_args(
            port=self.port,
            identity_file=self.identity_file,
            proxy_jump=self.proxy_jump,
        )
        args.append(str(local_path))
        dest = f"{self.user}@{self.host}:{remote_path}" if self.user else f"{self.host}:{remote_path}"
        args.append(dest)
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._resolve_timeout(timeout),
        )
        if result.returncode != 0:
            raise SSHError(f"scp failed: {result.stderr.strip()}")

    def copy_from(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy ``remote_path`` from the VM to ``local_path`` via scp."""
        args = _scp_base_args(
            port=self.port,
            identity_file=self.identity_file,
            proxy_jump=self.proxy_jump,
        )
        src = f"{self.user}@{self.host}:{remote_path}" if self.user else f"{self.host}:{remote_path}"
        args.append(src)
        args.append(str(local_path))
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._resolve_timeout(timeout),
        )
        if result.returncode != 0:
            raise SSHError(f"scp failed: {result.stderr.strip()}")

    def call_streaming(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run ``command`` over SSH with inherited stdio.

        Non-interactive (``BatchMode=yes``, no TTY). Used by
        ``vm exec`` and ``agent exec`` so the operator sees output
        stream in real time. Returns the remote exit code.
        """
        args = [
            "ssh",
            "-T",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "BatchMode=yes",
            *keepalive_args(),
        ]
        if self.port is not None:
            args.extend(["-p", str(self.port)])
        if self.identity_file is not None:
            args.extend(["-i", str(self.identity_file)])
        if self.proxy_jump is not None:
            args.extend(["-J", self.proxy_jump])
        args.extend(_set_env_args(env))
        target = f"{self.user}@{self.host}" if self.user else self.host
        args.append(target)
        args.append("--")  # fence: see run() for rationale
        args.append(command)
        return subprocess.call(args)
