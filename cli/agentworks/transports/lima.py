"""Lima transport implementation.

Reaches a local Lima VM via ``limactl shell`` / ``limactl copy``. Used
as the provisioner transport for the Lima platform (bootstrap and
``vm shell --provisioner``); the canonical transport for Lima VMs is
Tailscale SSH once the VM is online.
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

if TYPE_CHECKING:
    from agentworks.ssh import SSHLogger


def _env_assignment_prefix(env: dict[str, str] | None) -> str:
    """Return ``K1=v1 K2=v2 `` (trailing space) for ``env`` or empty string.

    Used as a prefix on the bash payload (interpreted as scoped env
    assignments) since ``limactl shell`` doesn't expose env injection.
    """
    if not env:
        return ""
    return "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())


class LimaTransport(Transport):
    """Local Lima VM transport via ``limactl shell`` / ``limactl copy``."""

    def __init__(
        self,
        vm_name: str,
        *,
        default_timeout: int | None = None,
        logger: SSHLogger | None = None,
    ) -> None:
        self.vm_name = vm_name
        self.default_timeout = default_timeout
        self.logger = logger

    def _resolve_timeout(self, override: int | None) -> int | None:
        return override if override is not None else self.default_timeout

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
        """Run ``command`` inside the Lima VM via ``limactl shell``.

        ``tty`` is accepted but has no effect on Lima (limactl always
        runs without a TTY for non-interactive shell commands).
        """
        if sudo:
            command = f"sudo -n bash -c {shlex.quote(command)}"
        env_prefix = _env_assignment_prefix(env)
        args = ["limactl", "shell", self.vm_name, "bash", "-lc", f"{env_prefix}{command}"]
        t = self._resolve_timeout(timeout)
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
            raise SSHError(f"Lima command timed out after {t}s: {command}") from err
        ssh_result = SSHResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        if self.logger is not None:
            self.logger.log_command(command, ssh_result)
        if check and not ssh_result.ok:
            raise SSHError(
                f"Lima command failed (exit {result.returncode}): {command}\n"
                f"stderr: {result.stderr.strip()}"
            )
        return ssh_result

    def interactive(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Interactive shell via ``limactl shell <vm>``.

        ``env`` is dropped (limactl shell doesn't expose env injection
        on its interactive API). Empty ``command`` opens a login shell;
        otherwise runs ``bash -lc <command>`` (still interactive
        because limactl allocates a TTY by default).
        """
        del env  # documented gap
        args = ["limactl", "shell", self.vm_name]
        if command:
            args.extend(["bash", "-lc", command])
        return subprocess.call(args)

    def copy_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy ``local_path`` into the Lima VM via ``limactl copy``."""
        del timeout  # limactl copy doesn't accept a timeout
        result = subprocess.run(
            ["limactl", "copy", str(local_path), f"{self.vm_name}:{remote_path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise SSHError(f"limactl copy failed: {result.stderr.strip()}")

    def copy_from(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy ``remote_path`` from the Lima VM via ``limactl copy``
        with the source/destination order reversed.
        """
        del timeout
        result = subprocess.run(
            ["limactl", "copy", f"{self.vm_name}:{remote_path}", str(local_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise SSHError(f"limactl copy failed: {result.stderr.strip()}")

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
        """Run ``command`` inside the Lima VM with inherited stdio."""
        env_prefix = _env_assignment_prefix(env)
        args = ["limactl", "shell", self.vm_name, "bash", "-lc", f"{env_prefix}{command}"]
        return subprocess.call(args)
