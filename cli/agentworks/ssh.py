"""SSH execution primitive.

All remote operations use native ssh/scp/rsync subprocess calls, respecting
the user's SSH config and agent.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class SSHTarget:
    """Connection info for reaching a remote host via SSH."""

    host: str
    user: str = "agentworks"
    port: int | None = None
    identity_file: Path | None = None
    proxy_jump: str | None = None


@dataclass
class SSHResult:
    """Result of a remote command execution."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SSHError(Exception):
    """Raised when an SSH command fails unexpectedly."""


def _ssh_base_args(target: SSHTarget) -> list[str]:
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if target.port is not None:
        args.extend(["-p", str(target.port)])
    if target.identity_file is not None:
        args.extend(["-i", str(target.identity_file)])
    if target.proxy_jump is not None:
        args.extend(["-J", target.proxy_jump])
    args.append(f"{target.user}@{target.host}")
    return args


def run(
    target: SSHTarget,
    command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
) -> SSHResult:
    """Execute a command on a remote host via SSH.

    Args:
        target: SSH connection info.
        command: Shell command to execute remotely.
        check: If True, raise SSHError on non-zero exit.
        timeout: Timeout in seconds.

    Returns:
        SSHResult with exit code, stdout, and stderr.
    """
    args = _ssh_base_args(target)
    args.append(command)

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    ssh_result = SSHResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if check and not ssh_result.ok:
        raise SSHError(
            f"SSH command failed (exit {result.returncode}): {command}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return ssh_result


def run_as_root(
    target: SSHTarget,
    command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
) -> SSHResult:
    """Execute a command as root via sudo on a remote host."""
    return run(target, f"sudo -n {command}", check=check, timeout=timeout)


def copy_to(
    target: SSHTarget,
    local_path: str | Path,
    remote_path: str,
    *,
    timeout: int | None = None,
) -> None:
    """Copy a file to a remote host via scp."""
    args = ["scp", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if target.port is not None:
        args.extend(["-P", str(target.port)])
    if target.identity_file is not None:
        args.extend(["-i", str(target.identity_file)])
    args.append(str(local_path))
    args.append(f"{target.user}@{target.host}:{remote_path}")

    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise SSHError(f"scp failed: {result.stderr.strip()}")


def rsync_to(
    target: SSHTarget,
    local_path: str | Path,
    remote_path: str,
    *,
    timeout: int | None = None,
) -> None:
    """Rsync a directory to a remote host."""
    ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    if target.port is not None:
        ssh_cmd += f" -p {target.port}"
    if target.identity_file is not None:
        ssh_cmd += f" -i {target.identity_file}"

    args = [
        "rsync", "-az", "--delete",
        "-e", ssh_cmd,
        f"{local_path}/",
        f"{target.user}@{target.host}:{remote_path}/",
    ]

    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise SSHError(f"rsync failed: {result.stderr.strip()}")


@dataclass
class LimaTarget:
    """Execution target for local Lima VMs (used pre-Tailscale)."""

    vm_name: str


def lima_run(
    target: LimaTarget,
    command: str,
    *,
    check: bool = True,
) -> SSHResult:
    """Execute a command inside a local Lima VM via limactl shell."""
    args = ["limactl", "shell", target.vm_name, "bash", "-lc", command]
    result = subprocess.run(args, capture_output=True, text=True)
    ssh_result = SSHResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if check and not ssh_result.ok:
        raise SSHError(
            f"Lima command failed (exit {result.returncode}): {command}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return ssh_result


@dataclass
class WSL2Target:
    """Execution target for WSL2 distros (used pre-Tailscale)."""

    distro_name: str
    user: str = "agentworks"


def wsl2_run(
    target: WSL2Target,
    command: str,
    *,
    check: bool = True,
) -> SSHResult:
    """Execute a command inside a WSL2 distro."""
    args = [
        "wsl", "--distribution", target.distro_name,
        "--user", target.user,
        "--", "bash", "-lc", command,
    ]
    result = subprocess.run(args, capture_output=True, text=True)
    ssh_result = SSHResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if check and not ssh_result.ok:
        raise SSHError(
            f"WSL2 command failed (exit {result.returncode}): {command}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return ssh_result


@dataclass(frozen=True)
class ExecTarget:
    """Union-like wrapper for SSH, Lima, or WSL2 execution targets."""

    ssh: SSHTarget | None = None
    lima: LimaTarget | None = None
    wsl2: WSL2Target | None = None

    def run(self, command: str, *, check: bool = True, timeout: int | None = None) -> SSHResult:
        if self.ssh is not None:
            return run(self.ssh, command, check=check, timeout=timeout)
        if self.lima is not None:
            return lima_run(self.lima, command, check=check)
        if self.wsl2 is not None:
            return wsl2_run(self.wsl2, command, check=check)
        msg = "ExecTarget has no target configured"
        raise SSHError(msg)

    def run_as_root(self, command: str, *, check: bool = True, timeout: int | None = None) -> SSHResult:
        if self.ssh is not None:
            return run_as_root(self.ssh, command, check=check, timeout=timeout)
        if self.lima is not None:
            # Lima shell runs as the host user who has passwordless sudo
            return lima_run(self.lima, f"sudo -n {command}", check=check)
        if self.wsl2 is not None:
            return wsl2_run(WSL2Target(self.wsl2.distro_name, user="root"), command, check=check)
        msg = "ExecTarget has no target configured"
        raise SSHError(msg)
