"""Remote Lima transport implementation.

Reaches a Lima VM on a remote VM host by SSHing to the host and
invoking ``limactl shell`` there. The host's login shell wraps every
invocation (``$SHELL -lc ...``) so the operator's per-shell PATH
additions (Homebrew at ``/opt/homebrew/bin`` etc.) are present and
``limactl`` resolves; SSH's default non-login shell wouldn't see them.
"""

from __future__ import annotations

import shlex
import subprocess
import uuid
from typing import TYPE_CHECKING

from agentworks.transports._shared import env_assignment_prefix
from agentworks.transports.base import Transport
from agentworks.transports.ssh import SSHTransport, keepalive_args, note_ssh_interactive_exit

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentworks.ssh import SSHLogger, SSHResult


class RemoteLimaTransport(Transport):
    """Lima VM on a remote VM host: SSH-to-host + ``limactl shell``.

    Two-hop transport. The outer SSH hop targets ``vm_host_ssh`` (an
    entry in the operator's SSH config, no explicit user); the inner
    invocation is ``limactl shell <vm>``, wrapped in
    ``$SHELL -lc '...'`` so the host's login-shell PATH is in effect.
    """

    def __init__(
        self,
        vm_name: str,
        vm_host_ssh: str,
        *,
        default_timeout: int | None = None,
        logger: SSHLogger | None = None,
    ) -> None:
        self.vm_name = vm_name
        self.vm_host_ssh = vm_host_ssh
        self.default_timeout = default_timeout
        self.logger = logger
        # Two inner SSHTransports built once: the login-shell variant
        # wraps every payload in ``$SHELL -lc`` (so the host's PATH
        # finds limactl); the raw variant is used by scp where the
        # login-shell wrap would be wrong.
        self._host_login = SSHTransport(
            host=vm_host_ssh,
            user=None,
            login_shell=True,
            default_timeout=default_timeout,
            logger=logger,
        )
        self._host_raw = SSHTransport(
            host=vm_host_ssh,
            user=None,
            login_shell=False,
            default_timeout=default_timeout,
            logger=logger,
        )

    def describe(self) -> str:
        return f"remote_lima:{self.vm_name}@{self.vm_host_ssh}"

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
        retries: int | None = None,
        on_retry: Callable[[int, int], None] | None = None,
    ) -> SSHResult:
        """Run ``command`` inside the remote Lima VM.

        ``env`` is embedded as scoped assignments in the lima payload
        (SetEnv at the host hop doesn't propagate into the limactl
        shell on the VM side). ``retries`` / ``on_retry`` propagate to
        the inner SSH hop.
        """
        del tty  # tty doesn't apply to non-interactive remote_lima
        if sudo:
            command = f"sudo -n bash -c {shlex.quote(command)}"
        env_prefix = env_assignment_prefix(env)
        lima_cmd = f"limactl shell {self.vm_name} -- {env_prefix}{command}"
        return self._host_login.run(
            lima_cmd,
            check=check,
            timeout=timeout,
            input_text=input_text,
            retries=retries,
            on_retry=on_retry,
        )

    def _interactive(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Interactive shell via ``ssh -t <host> $SHELL -lc 'limactl shell ...'``."""
        del env  # documented gap: no env propagation on interactive path
        inner = f"limactl shell {self.vm_name}"
        if command:
            inner = f"limactl shell {self.vm_name} bash -lc {shlex.quote(command)}"
        wrapped = f"$SHELL -lc {shlex.quote(inner)}"
        args = [
            "ssh",
            "-t",
            "-o",
            "StrictHostKeyChecking=accept-new",
            *keepalive_args(),
            self.vm_host_ssh,
            wrapped,
        ]
        return subprocess.call(args)

    def _note_interactive_exit(self, code: int) -> None:
        # The outer hop is a plain ``ssh -t``, so it carries ssh's 255
        # transport-failure convention and the same dropped-attach
        # failure mode as SSHTransport.
        note_ssh_interactive_exit(code, self.describe())

    def copy_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy via scp-to-host then ``limactl copy`` host -> VM.

        The intermediate lives in a private, UUID-named directory on the
        host's disk-backed temporary filesystem. Cleanup is attempted for
        every exit from the two-hop copy, including transfer failures.
        """
        host_tmp_dir = f"/var/tmp/agentworks-transfer-{uuid.uuid4()}"
        host_tmp = f"{host_tmp_dir}/payload"
        try:
            self._host_login.run(
                f"umask 077 && mkdir -- {shlex.quote(host_tmp_dir)}",
                timeout=timeout,
            )
            self._host_raw.copy_to(local_path, host_tmp, timeout=timeout)
            self._host_login.run(
                f"limactl copy {shlex.quote(host_tmp)} {self.vm_name}:{shlex.quote(remote_path)}",
                timeout=timeout,
            )
        finally:
            self._host_login.run(
                f"rm -rf -- {shlex.quote(host_tmp_dir)}",
                check=False,
                timeout=timeout,
            )

    def copy_from(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy via ``limactl copy`` VM -> host, then scp host -> local.

        The private host directory follows the same lifetime as ``copy_to``.
        """
        host_tmp_dir = f"/var/tmp/agentworks-transfer-{uuid.uuid4()}"
        host_tmp = f"{host_tmp_dir}/payload"
        try:
            self._host_login.run(
                f"umask 077 && mkdir -- {shlex.quote(host_tmp_dir)}",
                timeout=timeout,
            )
            self._host_login.run(
                f"limactl copy {self.vm_name}:{shlex.quote(remote_path)} {shlex.quote(host_tmp)}",
                timeout=timeout,
            )
            self._host_raw.copy_from(host_tmp, local_path, timeout=timeout)
        finally:
            self._host_login.run(
                f"rm -rf -- {shlex.quote(host_tmp_dir)}",
                check=False,
                timeout=timeout,
            )

    def call_streaming(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Stream a remote command via SSH-to-host + ``limactl shell``."""
        env_prefix = env_assignment_prefix(env)
        lima_cmd = f"limactl shell {self.vm_name} -- {env_prefix}{command}"
        wrapped = f"$SHELL -lc {shlex.quote(lima_cmd)}"
        args = ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", self.vm_host_ssh, wrapped]
        return subprocess.call(args)
