"""SSH execution primitive.

All remote operations use native ssh/scp subprocess calls, respecting
the operator's SSH config and agent.
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.errors import ConnectivityError

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import Config
    from agentworks.db import AgentRow, VMRow


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


def exec_target_for_user(
    vm: VMRow,
    config: Config,
    *,
    user: str,
    logger: SSHLogger | None = None,
    default_timeout: int | None = None,
) -> ExecTarget:
    """Build an ExecTarget that connects to the VM as the given Linux user.

    Shared core of ``admin_exec_target`` and ``agent_exec_target``. Also
    available directly for the rare case where the caller has a Linux
    username but no ``AgentRow`` (e.g. mid-create when the agent isn't in
    the DB yet but its on-VM identity already accepts the operator's key).

    On Windows, forces TTY allocation to prevent zsh from hanging on
    non-interactive piped SSH commands.
    """
    import sys

    assert vm.tailscale_host is not None, f"VM {vm.name} has no Tailscale host"
    return ExecTarget(
        ssh=SSHTarget(
            host=vm.tailscale_host,
            user=user,
            identity_file=config.operator.ssh_private_key,
            force_tty=sys.platform == "win32",
        ),
        logger=logger,
        default_timeout=default_timeout,
    )


def admin_exec_target(
    vm: VMRow,
    config: Config,
    *,
    logger: SSHLogger | None = None,
    default_timeout: int | None = None,
) -> ExecTarget:
    """Build an ExecTarget for the admin user via Tailscale SSH."""
    return exec_target_for_user(
        vm,
        config,
        user=vm.admin_username,
        logger=logger,
        default_timeout=default_timeout,
    )


def agent_exec_target(
    vm: VMRow,
    config: Config,
    agent: AgentRow,
    *,
    logger: SSHLogger | None = None,
    default_timeout: int | None = None,
) -> ExecTarget:
    """Build an ExecTarget that connects to the VM as the agent's Linux user.

    Used by agent-mode operations whose target user is the agent (session
    creation, agent shell, agent exec, etc.). The agent's authorized_keys
    must already accept the operator's SSH key (see
    ``agentworks.vms.initializer._reconcile_authorized_keys``'s
    stage-and-install path, invoked at agent create / reinit).
    """
    return exec_target_for_user(
        vm,
        config,
        user=agent.linux_user,
        logger=logger,
        default_timeout=default_timeout,
    )


# SSH transport failure exit code (connection refused, host unreachable, etc.)
SSH_TRANSPORT_ERROR = 255


@dataclass
class SSHResult:
    """Result of a remote command execution."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SSHError(ConnectivityError):
    """Raised when an SSH command fails unexpectedly (transport failure,
    timeout, non-zero exit under ``check=True``).

    Inherits from ``ConnectivityError`` so the CLI's top-level error wrapper
    treats SSH failures as transport-level problems. The current
    implementation conflates true connectivity failures (timeout, host
    unreachable) with remote-command-failed (exit nonzero); splitting those
    two cases is tracked as future work.
    """


LOG_DIR = Path.home() / ".config" / "agentworks" / "logs"


class SSHLogger:
    """Incremental command logger. Writes to disk on every call.

    Replaces the old InitLogger with a unified logger that covers SSH
    commands, init steps, warnings, and general output. All output is
    written incrementally so partial logs survive crashes.

    Usage:
        logger = SSHLogger("myvm", "vm-create")
        logger.step("Installing packages")
        run(target, "apt-get install ...", logger=logger)
        logger.warning("package X failed")
        logger.close()
    """

    def __init__(self, vm_name: str, command_stem: str) -> None:
        from datetime import UTC, datetime

        timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        self.vm_name = vm_name
        self.path = LOG_DIR / f"{vm_name}-{timestamp}-{command_stem}.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._redact: list[str] = []
        self._warnings: list[str] = []

        ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._write(f"# Log: {vm_name} ({command_stem})\n# Started: {ts}\n\n")

    def add_redaction(self, secret: str) -> None:
        """Register a secret to be redacted from all output."""
        if secret:
            self._redact.append(secret)

    def _sanitize(self, text: str) -> str:
        for secret in self._redact:
            text = text.replace(secret, "[REDACTED]")
        return text

    def step(self, name: str) -> None:
        """Log the start of a named step."""
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._write(f"--- [{ts}] {name} ---\n")

    def output(self, text: str) -> None:
        """Log general output (with redaction)."""
        if text:
            text = self._sanitize(text)
            self._write(text if text.endswith("\n") else text + "\n")

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

    def warning(self, msg: str) -> None:
        """Record a warning (also written to the log)."""
        self._warnings.append(msg)
        self._write(f"WARNING: {self._sanitize(msg)}\n")

    def log_error(self, msg: str) -> None:
        """Log an error message."""
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._write(f"[{ts}] ERROR: {self._sanitize(msg)}\n")

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    @property
    def has_warnings(self) -> bool:
        return len(self._warnings) > 0

    def close(self) -> None:
        """Write a footer with summary."""
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [f"\n# Finished: {ts}"]
        if self._warnings:
            lines.append(f"# Warnings: {len(self._warnings)}")
            for w in self._warnings:
                lines.append(f"#   - {w}")
        self._write("\n".join(lines) + "\n")

    def _write(self, text: str) -> None:
        with open(self.path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)


SSH_CONNECT_TIMEOUT = 30
SSH_DEFAULT_RETRIES = 1


def _set_env_args(env: dict[str, str] | None) -> list[str]:
    """Build the ``-o SetEnv=...`` ssh-client args for a (key, value) dict.

    ssh_config(5) says "for each parameter, the first obtained value will
    be used" -- so emitting ``-o SetEnv=K=V`` once per pair silently drops
    every pair after the first. We coalesce all pairs into a single
    ``-o SetEnv="K1=V1" "K2=V2" ...`` argument; the option's value is
    parsed by OpenSSH as whitespace-separated VAR=VALUE pairs with
    double-quote grouping. Values are always quoted (handles spaces, empty
    values, and embedded ``"``/``\\``) with the standard escapes.

    The remote sshd accepts the pairs under the ``AcceptEnv *`` directive
    deployed by VM init (see ADR 0014).
    """
    if not env:
        return []
    pairs = []
    for key, value in env.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        pairs.append(f'{key}="{escaped}"')
    return ["-o", "SetEnv=" + " ".join(pairs)]


def _ssh_base_args(
    target: SSHTarget,
    *,
    env: dict[str, str] | None = None,
) -> list[str]:
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if target.force_tty:
        args.insert(1, "-tt")
    if target.port is not None:
        args.extend(["-p", str(target.port)])
    if target.identity_file is not None:
        args.extend(["-i", str(target.identity_file)])
    if target.proxy_jump is not None:
        args.extend(["-J", target.proxy_jump])
    args.extend(_set_env_args(env))
    if target.user:
        args.append(f"{target.user}@{target.host}")
    else:
        args.append(target.host)
    return args


def _unwrap_ssh(target: SSHTarget | ExecTarget) -> SSHTarget:
    """Extract SSHTarget from an ExecTarget. Temporary shim for migration."""
    if isinstance(target, ExecTarget):
        assert target.ssh is not None, "ExecTarget has no SSH target"
        return target.ssh
    return target


def run(
    target: SSHTarget | ExecTarget,
    command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
    retries: int = SSH_DEFAULT_RETRIES,
    on_retry: Callable[[int, int], None] | None = None,
    logger: SSHLogger | None = None,
    env: dict[str, str] | None = None,
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
        env: Env vars to inject via SSH SetEnv. All pairs are coalesced
            into a single ``-o SetEnv="K1=V1" "K2=V2" ...`` argument (see
            ``_set_env_args``); agentworks-managed VMs accept these via
            the ``AcceptEnv *`` directive deployed by VM init.

    Returns:
        SSHResult with exit code, stdout, and stderr.
    """
    target = _unwrap_ssh(target)
    args = _ssh_base_args(target, env=env)
    if target.login_shell:
        args.append(f"$SHELL -lc {shlex.quote(command)}")
    else:
        args.append(command)

    last_err: Exception | None = None
    for attempt in range(retries):
        if attempt > 0 and on_retry is not None:
            on_retry(attempt, retries)
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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
                    f"SSH command failed (exit {result.returncode}): {command}\nstderr: {result.stderr.strip()}"
                )
            return ssh_result
        except subprocess.TimeoutExpired as err:
            last_err = err
            if logger is not None:
                logger.log_timeout(command, attempt + 1, retries)
            continue

    msg = f"SSH command timed out after {retries} attempts ({timeout}s each): {command}"
    if logger is not None:
        logger.log_error(msg)
    raise SSHError(msg) from last_err


def interactive(
    target: SSHTarget | ExecTarget,
    command: str,
    *,
    env: dict[str, str] | None = None,
) -> int:
    """Run an interactive command with a TTY (for tmux attach, vm shell, etc.).

    If ``command`` is empty, opens a plain interactive login shell on the
    target. Otherwise runs ``command`` in an interactive session.

    Dispatches by transport when given an ``ExecTarget``:

    - ``ssh`` set: SSH with TTY and SetEnv (the long-standing path).
    - ``lima`` / ``remote_lima`` / ``wsl2`` set: platform-native
      interactive transport (``limactl shell``, ``ssh -t <host> limactl
      shell``, ``wsl.exe``). These transports do NOT propagate ``env``;
      the operator's identity profile on the VM (sourced from
      ``/etc/profile.d``) still provides static identity vars (e.g.
      AGENTWORKS_VM), but per-session env injection from the local
      config isn't wired through. The provisioner-shell use case is
      ``Tailscale-is-broken, reach the VM to fix it``, where per-
      session env isn't load-bearing; we accept the gap rather than
      build a more complex env-injection shim.

    Returns the process exit code. Does not raise on failure.
    """
    if isinstance(target, ExecTarget):
        if target.ssh is not None:
            ssh_target = target.ssh
        elif target.lima is not None:
            return _lima_interactive(target.lima, command)
        elif target.remote_lima is not None:
            return _remote_lima_interactive(target.remote_lima, command)
        elif target.wsl2 is not None:
            return _wsl2_interactive(target.wsl2, command)
        else:
            raise SSHError("ExecTarget has no transport configured for interactive shell")
    else:
        ssh_target = target

    # Build args without BatchMode (which rejects interactive prompts/TTY)
    args = ["ssh", "-t", "-o", "StrictHostKeyChecking=accept-new"]
    if ssh_target.port is not None:
        args.extend(["-p", str(ssh_target.port)])
    if ssh_target.identity_file is not None:
        args.extend(["-i", str(ssh_target.identity_file)])
    if ssh_target.proxy_jump is not None:
        args.extend(["-J", ssh_target.proxy_jump])
    args.extend(_set_env_args(env))
    if ssh_target.user:
        args.append(f"{ssh_target.user}@{ssh_target.host}")
    else:
        args.append(ssh_target.host)
    if command:
        args.append(command)
    return subprocess.call(args)


def _lima_interactive(target: LimaTarget, command: str) -> int:
    """Interactive shell via ``limactl shell <vm>``.

    Without ``command``, opens an interactive login shell on the VM.
    With a command, runs it via ``bash -lc`` (still interactive because
    limactl allocates a TTY by default).
    """
    args = ["limactl", "shell", target.vm_name]
    if command:
        args.extend(["bash", "-lc", command])
    return subprocess.call(args)


def _remote_lima_interactive(target: RemoteLimaTarget, command: str) -> int:
    """Interactive shell via SSH-to-host + ``limactl shell <vm>``.

    Two-hop interactive: ssh -t to the VM host, then have it invoke
    limactl shell with TTY allocation.
    """
    inner = f"limactl shell {target.vm_name}"
    if command:
        inner = f"limactl shell {target.vm_name} bash -lc {shlex.quote(command)}"
    args = ["ssh", "-t", "-o", "StrictHostKeyChecking=accept-new", target.vm_host_ssh, inner]
    return subprocess.call(args)


def _wsl2_interactive(target: WSL2Target, command: str) -> int:
    """Interactive shell via ``wsl --distribution <distro> --user <user>``."""
    args = ["wsl", "--distribution", target.distro_name, "--user", target.user]
    if command:
        args.extend(["--", "bash", "-lc", command])
    return subprocess.call(args)


def run_as_root(
    target: SSHTarget | ExecTarget,
    command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
    logger: SSHLogger | None = None,
) -> SSHResult:
    """Execute a command as root via sudo on a remote host.

    The entire command runs as root by wrapping in ``sudo -n bash -c '...'``,
    so pipelines and ``&&`` chains are fully privileged. This matches
    ``ExecTarget.run(sudo=True)``.
    """
    target = _unwrap_ssh(target)
    return run(
        target,
        f"sudo -n bash -c {shlex.quote(command)}",
        check=check,
        timeout=timeout,
        logger=logger,
    )


def scp_base_args(target: SSHTarget) -> list[str]:
    """Build the base scp argument list (flags and options, no paths)."""
    args = ["scp", "-q", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if target.port is not None:
        args.extend(["-P", str(target.port)])
    if target.identity_file is not None:
        args.extend(["-i", str(target.identity_file)])
    if target.proxy_jump is not None:
        args.extend(["-J", target.proxy_jump])
    return args


def copy_to(
    target: SSHTarget | ExecTarget,
    local_path: str | Path,
    remote_path: str,
    *,
    timeout: int | None = None,
) -> None:
    """Copy a file to a remote host via scp."""
    target = _unwrap_ssh(target)
    args = scp_base_args(target)
    args.append(str(local_path))
    dest = f"{target.user}@{target.host}:{remote_path}" if target.user else f"{target.host}:{remote_path}"
    args.append(dest)

    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise SSHError(f"scp failed: {result.stderr.strip()}")


def copy_from(
    target: SSHTarget,
    remote_path: str,
    local_path: str | Path,
    *,
    timeout: int | None = None,
) -> None:
    """Copy a file from a remote host via scp."""
    args = scp_base_args(target)
    src = f"{target.user}@{target.host}:{remote_path}" if target.user else f"{target.host}:{remote_path}"
    args.append(src)
    args.append(str(local_path))

    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise SSHError(f"scp failed: {result.stderr.strip()}")


def write_file(
    target: SSHTarget | ExecTarget,
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
    target = _unwrap_ssh(target)
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


def _env_assignment_prefix(env: dict[str, str] | None) -> str:
    """Return ``K1=v1 K2=v2 `` (trailing space) for ``env`` or empty string.

    Used by the non-SSH transports (Lima, RemoteLima, WSL2) where the OpenSSH
    SetEnv mechanism doesn't apply. The bash payload these transports run
    receives the vars as scoped assignments preceding the command, which
    bash interprets as per-command env (exported to that command's process
    and any children it spawns).
    """
    if not env:
        return ""
    return "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())


def lima_run(
    target: LimaTarget,
    command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
    logger: SSHLogger | None = None,
    env: dict[str, str] | None = None,
) -> SSHResult:
    """Execute a command inside a local Lima VM via limactl shell."""
    env_prefix = _env_assignment_prefix(env)
    args = ["limactl", "shell", target.vm_name, "bash", "-lc", f"{env_prefix}{command}"]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
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
    env: dict[str, str] | None = None,
) -> SSHResult:
    """Execute a command inside a remote Lima VM via the VM host.

    SSH sends argv as a single concatenated string to the remote shell,
    so we can pass multiple args after the host and they become one
    command line. This avoids nested single-quote escaping while still
    letting the VM host's login shell find limactl on PATH.

    Env vars don't propagate through the SSH SetEnv at the VM-host hop
    into the Lima VM (limactl shell starts a fresh shell on the VM side
    without inheriting the host's env), so we embed them as scoped
    assignments inside the lima_cmd payload.
    """
    host_target = SSHTarget(host=target.vm_host_ssh, user=None, login_shell=True)
    env_prefix = _env_assignment_prefix(env)
    lima_cmd = f"limactl shell {target.vm_name} -- {env_prefix}{command}"
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
    env: dict[str, str] | None = None,
) -> SSHResult:
    """Execute a command inside a WSL2 distro."""
    env_prefix = _env_assignment_prefix(env)
    args = [
        "wsl",
        "--distribution",
        target.distro_name,
        "--user",
        target.user,
        "--",
        "bash",
        "-lc",
        f"{env_prefix}{command}",
    ]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
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
        encoding="utf-8",
        errors="replace",
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
        """Run a command on the target.

        Args:
            command: Shell command to execute.
            sudo: Wrap in sudo -n bash -c '...' so the entire command runs as root.
            tty: None = transport default, True = request TTY, False = suppress TTY.
                 Only meaningful for SSH transport (controls -tt flag).
            check: Raise SSHError on non-zero exit.
            timeout: Timeout in seconds.
            env: Env vars to inject. For SSH transport, coalesced into a
                single ``-o SetEnv="K1=V1" "K2=V2" ...`` argument (see
                ``_set_env_args``; accepted on the remote side under the
                ``AcceptEnv *`` directive deployed by VM init). For
                Lima / RemoteLima / WSL2 transports, embedded as scoped
                assignments at the head of the bash payload (``K=v K=v
                cmd``) which bash exports for the command and its
                descendants. Unified across transports so callers don't
                need to special-case.
        """
        if sudo:
            command = f"sudo -n bash -c {shlex.quote(command)}"

        t = self._timeout(timeout)
        lg = self.logger

        if self.ssh is not None:
            # Resolve tty: None -> SSHTarget.force_tty, True/False override
            effective_tty = self.ssh.force_tty if tty is None else tty
            from dataclasses import replace as _replace

            ssh = _replace(self.ssh, force_tty=effective_tty) if effective_tty != self.ssh.force_tty else self.ssh
            return run(ssh, command, check=check, timeout=t, logger=lg, env=env)
        if self.lima is not None:
            return lima_run(self.lima, command, check=check, timeout=t, logger=lg, env=env)
        if self.remote_lima is not None:
            return remote_lima_run(self.remote_lima, command, check=check, timeout=t, logger=lg, env=env)
        if self.wsl2 is not None:
            return wsl2_run(self.wsl2, command, check=check, timeout=t, logger=lg, env=env)
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

    def copy_from(self, remote_path: str, local_path: str | Path, *, timeout: int | None = None) -> None:
        """Copy a remote file to a local path."""
        if self.ssh is not None:
            copy_from(self.ssh, remote_path, local_path, timeout=timeout)
        else:
            # For non-SSH targets, use a shell command to cat the file
            msg = "copy_from is only supported for SSH targets"
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

    def call_streaming(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run a remote command with stdio passthrough; return its exit code.

        Used for ``agw agent exec`` / ``agw vm exec``-style invocations
        where the operator wants the remote command's stdout / stderr to
        stream to their terminal rather than be captured and re-printed
        by ``run``. Non-interactive: ``BatchMode=yes`` and no TTY
        allocation, so this is the wrong helper for tmux attach or
        interactive shells (use ``interactive()`` for those).

        ``env`` injects env vars via SSH SetEnv (all pairs coalesced into
        one ``-o SetEnv="K1=V1" "K2=V2" ...`` argument; see
        ``_set_env_args``), accepted by the remote ``AcceptEnv *``
        directive deployed by VM init. Matches the env path used by
        ``run`` / ``interactive``.

        Only the SSH transport is supported today; other transports
        (lima, remote_lima, wsl2) raise ``SSHError`` if asked.
        """
        import subprocess as _subprocess

        if self.ssh is None:
            raise SSHError("call_streaming requires an SSH-backed ExecTarget")
        args = ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
        if self.ssh.port is not None:
            args.extend(["-p", str(self.ssh.port)])
        if self.ssh.identity_file is not None:
            args.extend(["-i", str(self.ssh.identity_file)])
        if self.ssh.proxy_jump is not None:
            args.extend(["-J", self.ssh.proxy_jump])
        args.extend(_set_env_args(env))
        if self.ssh.user:
            args.append(f"{self.ssh.user}@{self.ssh.host}")
        else:
            args.append(self.ssh.host)
        args.append(command)
        return _subprocess.call(args)

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


def wait_for_reconnect(target: ExecTarget, *, max_attempts: int = 16) -> bool:
    """Wait for an ExecTarget to become reachable over SSH.

    Used after network disruptions (e.g., Azure public IP changes) that
    temporarily break Tailscale connectivity. Polls with a double-check
    to handle flapping.

    Returns True if the connection stabilized, False if it timed out.
    """
    import time

    from agentworks import output

    output.detail("Waiting for Tailscale to reconnect (this may take several minutes)...")
    for attempt in range(max_attempts):
        try:
            target.run("echo ok", timeout=10)
            # One success isn't enough; the network can flap.
            if attempt > 0:
                time.sleep(2)
                target.run("echo ok", timeout=10)
            output.detail("Tailscale SSH reconnected")
            return True
        except SSHError:
            if attempt == max_attempts - 1:
                output.warn("Tailscale SSH did not reconnect after ~240s, proceeding anyway")
            time.sleep(5)
    return False
