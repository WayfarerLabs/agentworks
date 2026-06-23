"""SSH transport implementation.

Wraps ``ssh`` / ``scp`` subprocess calls in the ``Transport`` ABC's
surface. Holds the argv builders, SetEnv handling, sudo-wrap, and
login-shell wrapping for the canonical Tailscale SSH path.
``agentworks/ssh.py`` retains a small surface of SSH primitives
(``SSHTarget`` / ``SSHResult`` / ``SSHError`` / ``SSHLogger`` /
``LOG_DIR`` / module-level ``run`` / ``copy_to``) used by
``vm_hosts/manager.py`` against bare ``SSHTarget``s; everything
polymorphic-transport-shaped lives here.
"""

from __future__ import annotations

import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.ssh import SSHError, SSHResult, _set_env_args
from agentworks.transports.base import Transport

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.ssh import SSHLogger


# Transport-level SSH retry default. Connection-level timeouts trigger a
# retry; remote-command failures do not. Matches the prior ssh.py constant.
SSH_DEFAULT_RETRIES = 1


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

    def _resolve_timeout(self, override: int | None) -> int | None:
        return override if override is not None else self.default_timeout

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
        there. Phase 3 type-narrows callers that need either parameter
        to ``SSHTransport`` rather than hoisting these to the ABC.
        """
        if sudo:
            command = f"sudo -n bash -c {shlex.quote(command)}"

        args = self._ssh_base_args(force_tty=tty, env=env)
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
                raise SSHError(
                    f"SSH command failed (exit {result.returncode}): {command}\n"
                    f"stderr: {result.stderr.strip()}"
                )
            return ssh_result

        msg = f"SSH command timed out after {attempts} attempts ({t}s each): {command}"
        if self.logger is not None:
            self.logger.log_error(msg)
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

    def copy_dir_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        delete: bool = True,
        timeout: int | None = None,
    ) -> None:
        """Copy a local directory via tar + scp + remote extract.

        Uses Python's stdlib ``tarfile`` so no client tar binary is
        required (works on Windows). With ``delete=True`` (default)
        the destination is cleared before extraction.
        """
        local_path = Path(local_path)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                tar.add(local_path, arcname=".")
            remote_tmp = f"/tmp/agentworks-copy-{tmp_path.name}"
            self.copy_to(tmp_path, remote_tmp, timeout=timeout)
        finally:
            tmp_path.unlink(missing_ok=True)

        if delete:
            self.run(f"rm -rf {remote_path} && mkdir -p {remote_path}", timeout=timeout)
        else:
            self.run(f"mkdir -p {remote_path}", timeout=timeout)
        self.run(f"tar -xzf {remote_tmp} -C {remote_path} && rm -f {remote_tmp}", timeout=timeout)

    def write_file(
        self,
        remote_path: str,
        content: str,
        *,
        mode: str | None = None,
    ) -> None:
        """Write ``content`` to ``remote_path`` via tempfile + scp.

        Avoids embedding multi-line content in command argv, which
        breaks on Windows due to CRLF conversion.
        """
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".tmp", delete=False) as f:
            f.write(content.encode("utf-8"))
            tmp_path = f.name
        try:
            self.copy_to(tmp_path, remote_path)
            if self.logger is not None:
                self.logger.log_command(
                    f"(scp) write {remote_path} ({len(content)} bytes)",
                    SSHResult(returncode=0, stdout="", stderr=""),
                )
        except SSHError:
            if self.logger is not None:
                self.logger.log_error(f"(scp) failed to write {remote_path}")
            raise
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        if mode:
            self.run(f"chmod {mode} {remote_path}")

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
        args.append(command)
        return subprocess.call(args)
