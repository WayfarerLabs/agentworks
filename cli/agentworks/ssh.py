"""SSH execution primitive.

After Phase 4 of the polymorphic-transports refactor, the
``ExecTarget`` / Lima / WSL2 / RemoteLima surfaces in this module are
gone. ``agentworks.transports.ssh.SSHTransport`` is the
``Transport``-shaped replacement for the per-command surface; this
module retains the small set of bare-``SSHTarget`` helpers that aren't
``Transport``-shaped:

- ``SSHTarget`` / ``SSHResult`` / ``SSHError`` / ``SSH_TRANSPORT_ERROR``:
  shared data shapes still used across the codebase (and by
  ``SSHTransport`` itself).
- ``SSHLogger`` / ``LOG_DIR``: the unified command logger.
- Module-level ``run`` / ``copy_to``: called from
  ``capabilities/vm_platform/lima.py`` (the remote-Lima vm_host control plane)
  where the caller has a bare ``SSHTarget`` and doesn't want to
  construct a full ``SSHTransport``.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.errors import ConnectivityError

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
        """Log general output."""
        if text:
            self._write(text if text.endswith("\n") else text + "\n")

    def log_command(self, command: str, result: SSHResult) -> None:
        """Log a completed command with its output."""
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        lines = [f"[{ts}] $ {command}  (exit {result.returncode})"]
        if result.stdout:
            lines.append(result.stdout.rstrip())
        if result.stderr:
            lines.append(f"STDERR: {result.stderr.rstrip()}")
        lines.append("")
        self._write("\n".join(lines) + "\n")

    def log_timeout(self, command: str, attempt: int, retries: int) -> None:
        """Log a timeout event."""
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._write(f"[{ts}] TIMEOUT (attempt {attempt}/{retries}): {command}\n")

    def warning(self, msg: str) -> None:
        """Record a warning (also written to the log)."""
        self._warnings.append(msg)
        self._write(f"WARNING: {msg}\n")

    def log_error(self, msg: str) -> None:
        """Log an error message."""
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._write(f"[{ts}] ERROR: {msg}\n")

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    @property
    def has_warnings(self) -> bool:
        return len(self._warnings) > 0

    def close(self) -> None:
        """Write a footer with summary.

        If an exception is in flight (``close()`` called from inside an
        ``except`` block, which the operation-level handlers in
        ``vms/initializer.py`` and elsewhere do), append the full
        traceback before the footer. This lands the traceback in the
        per-operation log instead of relying on the top-level
        ``record_unhandled_error`` fallback, which writes to a shared
        ``error.log`` across every workspace.
        """
        import sys
        import traceback
        from datetime import UTC, datetime

        exc_type, exc, exc_tb = sys.exc_info()
        if exc is not None:
            ts_exc = datetime.now(tz=UTC).strftime("%H:%M:%S")
            tb_text = "".join(traceback.format_exception(exc_type, exc, exc_tb))
            self._write(f"[{ts_exc}] EXCEPTION:\n{tb_text}\n")

        ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [f"\n# Finished: {ts}"]
        if self._warnings:
            lines.append(f"# Warnings: {len(self._warnings)}")
            for w in self._warnings:
                lines.append(f"#   - {w}")
        self._write("\n".join(lines) + "\n")

    def _write(self, text: str) -> None:
        # The single sanitizing choke point: every byte that reaches the
        # log file passes through redaction HERE, so the no-secrets-in-
        # logs property holds regardless of caller discipline (a caller
        # composing a message from raw values cannot bypass it, and
        # redactions registered mid-operation cover everything written
        # afterwards). Callers therefore never pre-sanitize.
        with open(self.path, "a", encoding="utf-8", errors="replace") as f:
            f.write(self._sanitize(text))


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


def run(
    target: SSHTarget,
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
    args = _ssh_base_args(target, env=env)
    # Fence the remote command from ssh's option parser. See
    # ``SSHTransport.run`` in ``transports/ssh.py`` for the rationale.
    args.append("--")
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


def _scp_base_args(target: SSHTarget) -> list[str]:
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
    target: SSHTarget,
    local_path: str | Path,
    remote_path: str,
    *,
    timeout: int | None = None,
) -> None:
    """Copy a file to a remote host via scp."""
    args = _scp_base_args(target)
    args.append(str(local_path))
    dest = f"{target.user}@{target.host}:{remote_path}" if target.user else f"{target.host}:{remote_path}"
    args.append(dest)

    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise SSHError(f"scp failed: {result.stderr.strip()}")
