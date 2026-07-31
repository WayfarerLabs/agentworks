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

from agentworks.ssh import SSH_DEFAULT_RETRIES, SSHError, SSHResult, _set_env_args
from agentworks.transports.base import Transport

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentworks.ssh import SSHLogger


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

        ``retries`` and ``on_retry`` are SSH-only extensions and are not
        part of the ``Transport`` ABC. Lima / WSL2 / RemoteLima do not
        retry on timeout, so neither parameter has anything to bind to
        there. Callers that need either parameter are type-narrowed
        to ``SSHTransport`` rather than hoisting these to the ABC.
        """
        if sudo:
            command = f"sudo -n bash -c {shlex.quote(command)}"

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

        t = self._resolve_timeout(timeout)
        attempts = retries if retries is not None else self.retries
        last_err: Exception | None = None
        for attempt in range(attempts):
            if attempt > 0 and on_retry is not None:
                on_retry(attempt, attempts)
            try:
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=t,
                )
            except subprocess.TimeoutExpired as err:
                last_err = err
                if self.logger is not None:
                    self.logger.log_timeout(command, attempt + 1, attempts)
                continue
            ssh_result = SSHResult(
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
            if self.logger is not None:
                self.logger.log_command(command, ssh_result)
            if check and not ssh_result.ok:
                # The message embeds the raw command and stderr (which
                # can carry secrets: a session's env rides the tmux
                # ``-e KEY=VAL`` flags inside the command string), and it
                # propagates to the console. So it takes the same
                # redaction pass the log file gets. No logger attached
                # means no registered redactions to apply: unchanged.
                msg = (
                    f"SSH command failed (exit {result.returncode}): {command}\n"
                    f"stderr: {result.stderr.strip()}"
                )
                if self.logger is not None:
                    msg = self.logger.sanitize(msg)
                raise SSHError(msg)
            return ssh_result

        msg = f"SSH command timed out after {attempts} attempts ({t}s each): {command}"
        if self.logger is not None:
            self.logger.log_error(msg)
            msg = self.logger.sanitize(msg)  # the raised copy needs its own pass
        raise SSHError(msg) from last_err

    def interactive(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Interactive SSH with ``-t`` (allocates a TTY) and no
        ``BatchMode``. Empty ``command`` opens a login shell.
        """
        args = ["ssh", "-t", "-o", "StrictHostKeyChecking=accept-new"]
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
        args = ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
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
