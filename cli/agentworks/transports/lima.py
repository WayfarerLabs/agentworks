"""Lima transport implementation.

Reaches a local Lima VM via ``limactl shell`` / ``limactl copy``. Used
as the platform-native transport for the Lima platform (bootstrap and
``vm shell --platform``); the canonical transport for Lima VMs is
Tailscale SSH once the VM is online.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

from agentworks.ssh import SSHError, SSHResult
from agentworks.transports._shared import env_assignment_prefix
from agentworks.transports.base import Transport

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentworks.ssh import SSHLogger


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

    def describe(self) -> str:
        return f"lima:{self.vm_name}"

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
        """Run ``command`` inside the Lima VM via ``limactl shell``.

        ``tty`` is accepted but has no effect on Lima (limactl always
        runs without a TTY for non-interactive shell commands).
        ``retries`` / ``on_retry`` are ABC-required kwargs; limactl
        doesn't surface a retryable timeout, so both are no-ops here.
        """
        del retries, on_retry  # Polymorphic ABC kwargs; Lima doesn't retry.
        if sudo:
            command = f"sudo -n bash -c {shlex.quote(command)}"
        env_prefix = env_assignment_prefix(env)
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
                f"Lima command failed (exit {result.returncode}): {command}\nstderr: {result.stderr.strip()}"
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

    def call_streaming(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run ``command`` inside the Lima VM with inherited stdio."""
        env_prefix = env_assignment_prefix(env)
        args = ["limactl", "shell", self.vm_name, "bash", "-lc", f"{env_prefix}{command}"]
        return subprocess.call(args)
