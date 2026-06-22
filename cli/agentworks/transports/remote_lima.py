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
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.ssh import SSHError, SSHResult
from agentworks.transports.base import Transport
from agentworks.transports.ssh import SSHTransport

if TYPE_CHECKING:
    from agentworks.ssh import SSHLogger


def _env_assignment_prefix(env: dict[str, str] | None) -> str:
    """``K1=v1 K2=v2 `` (trailing space) for ``env``, else empty."""
    if not env:
        return ""
    return "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())


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

    def _host_transport(self, *, login_shell: bool = True) -> SSHTransport:
        return SSHTransport(
            host=self.vm_host_ssh,
            user=None,
            login_shell=login_shell,
            default_timeout=self.default_timeout,
            logger=self.logger,
        )

    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        tty: bool | None = None,
        check: bool = True,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> SSHResult:
        """Run ``command`` inside the remote Lima VM.

        ``env`` is embedded as scoped assignments in the lima payload
        (SetEnv at the host hop doesn't propagate into the limactl
        shell on the VM side).
        """
        del tty  # tty doesn't apply to non-interactive remote_lima
        if sudo:
            command = f"sudo -n bash -c {shlex.quote(command)}"
        env_prefix = _env_assignment_prefix(env)
        lima_cmd = f"limactl shell {self.vm_name} -- {env_prefix}{command}"
        host = self._host_transport()
        return host.run(lima_cmd, check=check, timeout=timeout)

    def interactive(
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
        args = ["ssh", "-t", "-o", "StrictHostKeyChecking=accept-new", self.vm_host_ssh, wrapped]
        return subprocess.call(args)

    def copy_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy via scp-to-host then ``limactl copy`` host -> VM."""
        host = self._host_transport(login_shell=False)
        host_tmp = f"/tmp/agentworks-{Path(local_path).name}"
        host.copy_to(local_path, host_tmp, timeout=timeout)
        host_login = self._host_transport()
        host_login.run(f"limactl copy {host_tmp} {self.vm_name}:{remote_path}", timeout=timeout)
        host_login.run(f"rm -f {host_tmp}", check=False, timeout=timeout)

    def copy_from(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy via ``limactl copy`` VM -> host, then scp host -> local."""
        host_login = self._host_transport()
        host_tmp = f"/tmp/agentworks-{Path(remote_path).name}"
        host_login.run(f"limactl copy {self.vm_name}:{remote_path} {host_tmp}", timeout=timeout)
        host = self._host_transport(login_shell=False)
        try:
            host.copy_from(host_tmp, local_path, timeout=timeout)
        finally:
            host_login.run(f"rm -f {host_tmp}", check=False, timeout=timeout)

    def copy_dir_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        delete: bool = True,
        timeout: int | None = None,
    ) -> None:
        """Copy a directory via tar + ``copy_to`` + remote extract."""
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
        """Write ``content`` to ``remote_path`` via tempfile + ``copy_to``."""
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".tmp", delete=False) as f:
            f.write(content.encode("utf-8"))
            tmp_path = f.name
        try:
            self.copy_to(tmp_path, remote_path)
            if self.logger is not None:
                self.logger.log_command(
                    f"(limactl copy) write {remote_path} ({len(content)} bytes)",
                    SSHResult(returncode=0, stdout="", stderr=""),
                )
        except SSHError:
            if self.logger is not None:
                self.logger.log_error(f"(limactl copy) failed to write {remote_path}")
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
        """Stream a remote command via SSH-to-host + ``limactl shell``."""
        env_prefix = _env_assignment_prefix(env)
        lima_cmd = f"limactl shell {self.vm_name} -- {env_prefix}{command}"
        wrapped = f"$SHELL -lc {shlex.quote(lima_cmd)}"
        args = ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", self.vm_host_ssh, wrapped]
        return subprocess.call(args)
