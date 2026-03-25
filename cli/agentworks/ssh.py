"""SSH execution primitive.

All remote operations use native ssh/scp subprocess calls, respecting
the user's SSH config and agent.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


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
    force_tty: bool = False


def ssh_target_for_vm(vm: object, config: object) -> SSHTarget:
    """Build an SSHTarget from a VMRow and Config.

    Accepts object types to avoid circular imports with db/config modules.
    On Windows, forces TTY allocation to prevent zsh from hanging on
    non-interactive piped SSH commands.
    """
    import sys

    return SSHTarget(
        host=vm.tailscale_host,  # type: ignore[attr-defined]
        user=vm.admin_username,  # type: ignore[attr-defined]
        identity_file=config.user.ssh_private_key,  # type: ignore[attr-defined]
        force_tty=sys.platform == "win32",
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


LOG_DIR = Path.home() / ".config" / "agentworks" / "logs"


class SSHLogger:
    """Incremental SSH command logger. Writes to disk on every command.

    Usage:
        logger = SSHLogger("myvm", "vm-create")
        # pass to ssh.run via logger= or attach to ExecTarget
        run(target, "ls", logger=logger)
        logger.close()  # writes footer, optional
    """

    def __init__(self, vm_name: str, command_stem: str) -> None:
        from datetime import UTC, datetime

        timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        self.vm_name = vm_name
        self.path = LOG_DIR / f"{vm_name}-{timestamp}-{command_stem}.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._redact: list[str] = []

        ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._write(f"# SSH Log: {vm_name} ({command_stem})\n# Started: {ts}\n\n")

    def add_redaction(self, secret: str) -> None:
        """Register a secret to be redacted from all output."""
        if secret:
            self._redact.append(secret)

    def _sanitize(self, text: str) -> str:
        for secret in self._redact:
            text = text.replace(secret, "[REDACTED]")
        return text

    def log_command(self, command: str, result: SSHResult) -> None:
        """Log a completed command with its output."""
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        lines = [f"[{ts}] $ {self._sanitize(command)}  (exit {result.returncode})"]
        if result.stdout:
            lines.append(self._sanitize(result.stdout.rstrip()))
        if result.stderr:
            lines.append(f"STDERR: {self._sanitize(result.stderr.rstrip())}")
        lines.append("")
        self._write("\n".join(lines) + "\n")

    def log_timeout(self, command: str, attempt: int, retries: int) -> None:
        """Log a timeout event."""
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._write(f"[{ts}] TIMEOUT (attempt {attempt}/{retries}): {self._sanitize(command)}\n")

    def log_error(self, msg: str) -> None:
        """Log an error message."""
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._write(f"[{ts}] ERROR: {self._sanitize(msg)}\n")

    def close(self) -> None:
        """Write a footer line."""
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._write(f"\n# Finished: {ts}\n")

    def _write(self, text: str) -> None:
        with open(self.path, "a") as f:
            f.write(text)


SSH_CONNECT_TIMEOUT = 30
SSH_DEFAULT_RETRIES = 1


def _ssh_base_args(target: SSHTarget) -> list[str]:
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if target.force_tty:
        args.insert(1, "-tt")
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
    retries: int = SSH_DEFAULT_RETRIES,
    on_retry: Callable[[int, int], None] | None = None,
    logger: SSHLogger | None = None,
) -> SSHResult:
    """Execute a command on a remote host via SSH.

    Retries on timeout (connection flakiness). Command failures are not
    retried -- only connection-level timeouts trigger a retry.

    Args:
        target: SSH connection info.
        command: Shell command to execute remotely.
        check: If True, raise SSHError on non-zero exit.
        timeout: Timeout in seconds.
        retries: Number of attempts (default: SSH_RETRIES).
        on_retry: Optional callback(attempt, max_retries) called before each retry.
        logger: Optional SSHLogger to record command output.

    Returns:
        SSHResult with exit code, stdout, and stderr.
    """
    args = _ssh_base_args(target)
    if target.login_shell:
        args.append(f"$SHELL -lc '{command}'")
    else:
        args.append(command)

    last_err: Exception | None = None
    for attempt in range(retries):
        if attempt > 0:
            if on_retry is not None:
                on_retry(attempt, retries)
            if logger is not None:
                logger.log_timeout(command, attempt, retries)
        try:
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
            if logger is not None:
                logger.log_command(command, ssh_result)
            if check and not ssh_result.ok:
                raise SSHError(
                    f"SSH command failed (exit {result.returncode}): {command}\n"
                    f"stderr: {result.stderr.strip()}"
                )
            return ssh_result
        except subprocess.TimeoutExpired as err:
            last_err = err
            continue

    raise SSHError(f"SSH command timed out after {retries} attempts ({timeout}s each): {command}") from last_err


def interactive(target: SSHTarget, command: str) -> int:
    """Run an interactive SSH command with a TTY (for tmux attach, etc.).

    Returns the process exit code. Does not raise on failure.
    """
    # Build args without BatchMode (which rejects interactive prompts/TTY)
    args = ["ssh", "-t", "-o", "StrictHostKeyChecking=accept-new"]
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
    args.append(command)
    return subprocess.call(args)


def run_as_root(
    target: SSHTarget,
    command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
    logger: SSHLogger | None = None,
) -> SSHResult:
    """Execute a command as root via sudo on a remote host."""
    return run(target, f"sudo -n {command}", check=check, timeout=timeout, logger=logger)


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
    logger: SSHLogger | None = None,
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
        if logger is not None:
            logger.log_command(
                f"(scp) write {remote_path} ({len(content)} bytes)",
                SSHResult(returncode=0, stdout="", stderr=""),
            )
    except SSHError:
        if logger is not None:
            logger.log_error(f"(scp) failed to write {remote_path}")
        raise
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if mode:
        run(target, f"chmod {mode} {remote_path}", logger=logger)




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
    logger: SSHLogger | None = None,
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
    if logger is not None:
        logger.log_command(command, ssh_result)
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
    logger: SSHLogger | None = None,
) -> SSHResult:
    """Execute a command inside a remote Lima VM via the VM host.

    SSH sends argv as a single concatenated string to the remote shell,
    so we can pass multiple args after the host and they become one
    command line. This avoids nested single-quote escaping while still
    letting the VM host's login shell find limactl on PATH.
    """
    host_target = SSHTarget(host=target.vm_host_ssh, user=None, login_shell=True)
    lima_cmd = f"limactl shell {target.vm_name} -- {command}"
    return run(host_target, lima_cmd, check=check, timeout=timeout, logger=logger)


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
    logger: SSHLogger | None = None,
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
    if logger is not None:
        logger.log_command(command, ssh_result)
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
    logger: SSHLogger | None = None

    def _timeout(self, override: int | None) -> int | None:
        return override if override is not None else self.default_timeout

    def run(self, command: str, *, check: bool = True, timeout: int | None = None) -> SSHResult:
        t = self._timeout(timeout)
        lg = self.logger
        if self.ssh is not None:
            return run(self.ssh, command, check=check, timeout=t, logger=lg)
        if self.lima is not None:
            return lima_run(self.lima, command, check=check, timeout=t, logger=lg)
        if self.remote_lima is not None:
            return remote_lima_run(self.remote_lima, command, check=check, timeout=t, logger=lg)
        if self.wsl2 is not None:
            return wsl2_run(self.wsl2, command, check=check, timeout=t, logger=lg)
        msg = "ExecTarget has no target configured"
        raise SSHError(msg)

    def run_as_root(self, command: str, *, check: bool = True, timeout: int | None = None) -> SSHResult:
        t = self._timeout(timeout)
        lg = self.logger
        if self.ssh is not None:
            return run_as_root(self.ssh, command, check=check, timeout=t, logger=lg)
        if self.lima is not None:
            return lima_run(self.lima, f"sudo -n {command}", check=check, timeout=t, logger=lg)
        if self.remote_lima is not None:
            return remote_lima_run(self.remote_lima, f"sudo -n {command}", check=check, timeout=t, logger=lg)
        if self.wsl2 is not None:
            return wsl2_run(WSL2Target(self.wsl2.distro_name, user="root"), command, check=check, timeout=t, logger=lg)
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
