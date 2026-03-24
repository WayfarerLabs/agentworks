"""SSH execution primitive.

All remote operations use native ssh/scp subprocess calls, respecting
the user's SSH config and agent.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SSHTarget:
    """Connection info for reaching a remote host via SSH.

    Set user=None to defer to SSH config (used for VM host connections
    where the host is defined in ~/.ssh/config). Explicit user is set
    for VM connections where we control the username.
    """

    host: str
    user: str | None = None
    port: int | None = None
    identity_file: Path | None = None
    proxy_jump: str | None = None
    login_shell: bool = False


def ssh_target_for_vm(vm: object, config: object) -> SSHTarget:
    """Build an SSHTarget from a VMRow and Config.

    Accepts object types to avoid circular imports with db/config modules.
    """
    return SSHTarget(
        host=vm.tailscale_host,  # type: ignore[attr-defined]
        user=vm.admin_username,  # type: ignore[attr-defined]
        identity_file=config.user.ssh_private_key,  # type: ignore[attr-defined]
    )


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
    if target.user:
        args.append(f"{target.user}@{target.host}")
    else:
        args.append(target.host)
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
    if target.login_shell:
        args.append(f"$SHELL -lc '{command}'")
    else:
        args.append(command)

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as err:
        raise SSHError(f"SSH command timed out after {timeout}s: {command}") from err
    ssh_result = SSHResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if check and not ssh_result.ok:
        raise SSHError(f"SSH command failed (exit {result.returncode}): {command}\nstderr: {result.stderr.strip()}")
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
    dest = f"{target.user}@{target.host}:{remote_path}" if target.user else f"{target.host}:{remote_path}"
    args.append(dest)

    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise SSHError(f"scp failed: {result.stderr.strip()}")


def write_file(
    target: SSHTarget,
    remote_path: str,
    content: str,
    *,
    mode: str | None = None,
) -> None:
    """Write string content to a remote file safely.

    Writes to a local temp file in binary mode (preserving Unix line endings
    even on Windows) and copies via scp. This avoids embedding multi-line
    content in SSH command strings, which breaks on Windows due to \\r\\n
    conversion.
    """
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".tmp", delete=False) as f:
        f.write(content.encode("utf-8"))
        tmp_path = f.name
    try:
        copy_to(target, tmp_path, remote_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if mode:
        run(target, f"chmod {mode} {remote_path}")




@dataclass
class LimaTarget:
    """Execution target for local Lima VMs (used pre-Tailscale)."""

    vm_name: str


def lima_run(
    target: LimaTarget,
    command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
) -> SSHResult:
    """Execute a command inside a local Lima VM via limactl shell."""
    args = ["limactl", "shell", target.vm_name, "bash", "-lc", command]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        raise SSHError(f"Lima command timed out after {timeout}s: {command}") from err
    ssh_result = SSHResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if check and not ssh_result.ok:
        raise SSHError(f"Lima command failed (exit {result.returncode}): {command}\nstderr: {result.stderr.strip()}")
    return ssh_result


@dataclass
class RemoteLimaTarget:
    """Execution target for Lima VMs on a remote VM host.

    Commands are run by SSHing to the VM host and invoking limactl shell
    there. This avoids needing the Lima SSH key on the local machine.
    """

    vm_name: str
    vm_host_ssh: str


def remote_lima_run(
    target: RemoteLimaTarget,
    command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
) -> SSHResult:
    """Execute a command inside a remote Lima VM via the VM host.

    SSH sends argv as a single concatenated string to the remote shell,
    so we can pass multiple args after the host and they become one
    command line. This avoids nested single-quote escaping while still
    letting the VM host's login shell find limactl on PATH.
    """
    host_target = SSHTarget(host=target.vm_host_ssh, user=None, login_shell=True)
    # The inner command is passed as a bare arg to limactl shell.
    # SSH concatenates all args into one string for the remote shell,
    # so the login shell wrapper ($SHELL -lc '...') covers the whole thing.
    lima_cmd = f"limactl shell {target.vm_name} -- {command}"
    return run(host_target, lima_cmd, check=check, timeout=timeout)


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
    timeout: int | None = None,
) -> SSHResult:
    """Execute a command inside a WSL2 distro."""
    args = [
        "wsl",
        "--distribution",
        target.distro_name,
        "--user",
        target.user,
        "--",
        "bash",
        "-lc",
        command,
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        raise SSHError(f"WSL2 command timed out after {timeout}s: {command}") from err
    ssh_result = SSHResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if check and not ssh_result.ok:
        raise SSHError(f"WSL2 command failed (exit {result.returncode}): {command}\nstderr: {result.stderr.strip()}")
    return ssh_result


def _lima_copy_to(target: LimaTarget, local_path: str | Path, remote_path: str) -> None:
    """Copy a file into a local Lima VM via limactl copy."""
    result = subprocess.run(
        ["limactl", "copy", str(local_path), f"{target.vm_name}:{remote_path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SSHError(f"limactl copy failed: {result.stderr.strip()}")


def _remote_lima_copy_to(target: RemoteLimaTarget, local_path: str | Path, remote_path: str) -> None:
    """Copy a file into a remote Lima VM (two-hop: scp to host, then limactl copy)."""
    host_target = SSHTarget(host=target.vm_host_ssh, user=None)
    host_tmp = f"/tmp/agentworks-{Path(local_path).name}"
    copy_to(host_target, local_path, host_tmp)
    host_login = SSHTarget(host=target.vm_host_ssh, user=None, login_shell=True)
    run(host_login, f"limactl copy {host_tmp} {target.vm_name}:{remote_path}")
    run(host_login, f"rm -f {host_tmp}", check=False)


def _wsl2_copy_to(target: WSL2Target, local_path: str | Path, remote_path: str) -> None:
    """Copy a file into a WSL2 distro via stdin to avoid path translation issues."""
    content = Path(local_path).read_bytes()
    result = subprocess.run(
        ["wsl", "--distribution", target.distro_name, "--user", "root", "--", "bash", "-c", f"cat > {remote_path}"],
        input=content,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SSHError(f"WSL2 copy failed: {result.stderr.decode().strip()}")


@dataclass(frozen=True)
class ExecTarget:
    """Union-like wrapper for SSH, Lima, RemoteLima, or WSL2 execution targets.

    Set default_timeout (seconds) to apply a timeout to all run/run_as_root
    calls automatically. Individual calls can override with their own timeout.
    """

    ssh: SSHTarget | None = None
    lima: LimaTarget | None = None
    remote_lima: RemoteLimaTarget | None = None
    wsl2: WSL2Target | None = None
    default_timeout: int | None = None

    def _timeout(self, override: int | None) -> int | None:
        return override if override is not None else self.default_timeout

    def run(self, command: str, *, check: bool = True, timeout: int | None = None) -> SSHResult:
        t = self._timeout(timeout)
        if self.ssh is not None:
            return run(self.ssh, command, check=check, timeout=t)
        if self.lima is not None:
            return lima_run(self.lima, command, check=check, timeout=t)
        if self.remote_lima is not None:
            return remote_lima_run(self.remote_lima, command, check=check, timeout=t)
        if self.wsl2 is not None:
            return wsl2_run(self.wsl2, command, check=check, timeout=t)
        msg = "ExecTarget has no target configured"
        raise SSHError(msg)

    def run_as_root(self, command: str, *, check: bool = True, timeout: int | None = None) -> SSHResult:
        t = self._timeout(timeout)
        if self.ssh is not None:
            return run_as_root(self.ssh, command, check=check, timeout=t)
        if self.lima is not None:
            return lima_run(self.lima, f"sudo -n {command}", check=check, timeout=t)
        if self.remote_lima is not None:
            return remote_lima_run(self.remote_lima, f"sudo -n {command}", check=check, timeout=t)
        if self.wsl2 is not None:
            return wsl2_run(WSL2Target(self.wsl2.distro_name, user="root"), command, check=check, timeout=t)
        msg = "ExecTarget has no target configured"
        raise SSHError(msg)

    def copy_to(self, local_path: str | Path, remote_path: str, *, timeout: int | None = None) -> None:
        """Copy a local file to the target."""
        if self.ssh is not None:
            copy_to(self.ssh, local_path, remote_path, timeout=timeout)
        elif self.lima is not None:
            _lima_copy_to(self.lima, local_path, remote_path)
        elif self.remote_lima is not None:
            _remote_lima_copy_to(self.remote_lima, local_path, remote_path)
        elif self.wsl2 is not None:
            _wsl2_copy_to(self.wsl2, local_path, remote_path)
        else:
            msg = "ExecTarget has no target configured"
            raise SSHError(msg)

    def copy_dir_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        delete: bool = True,
        timeout: int | None = None,
    ) -> None:
        """Copy a local directory to the target via tar + scp.

        Creates a gzip tarball with Python's stdlib tarfile (no client-side tar
        binary required -- works on Windows), scps it to the remote, and
        extracts it there.

        With delete=True (default), the remote directory is cleared before
        extraction so stale files do not linger. Pass delete=False to extract
        on top of existing contents, preserving unmanaged files.
        """
        import tarfile as tarfile_mod

        local_path = Path(local_path)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
            tmp_path = Path(f.name)

        try:
            with tarfile_mod.open(tmp_path, "w:gz") as tar:
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

    def write_file(self, remote_path: str, content: str, *, mode: str | None = None) -> None:
        """Write string content to a remote file safely.

        Uses copy_to under the hood to avoid embedding multi-line content
        in command strings (which breaks on Windows due to line endings).
        """
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".tmp", delete=False) as f:
            f.write(content.encode("utf-8"))
            tmp_path = f.name
        try:
            self.copy_to(tmp_path, remote_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        if mode:
            self.run(f"chmod {mode} {remote_path}")
