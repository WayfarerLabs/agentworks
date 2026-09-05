"""SSH execution primitive.

The polymorphic-transports refactor removed the ``ExecTarget`` / Lima /
WSL2 / RemoteLima surfaces from this module.
``agentworks.transports.ssh.SSHTransport`` is the
``Transport``-shaped replacement for the per-command surface; this
module retains the small set of bare-``SSHTarget`` helpers that aren't
``Transport``-shaped:

- ``SSHTarget`` / ``SSHResult`` / ``SSHError`` / ``SSH_TRANSPORT_ERROR``:
  shared data shapes still used across the codebase (and by
  ``SSHTransport`` itself).
- ``SSHLogger`` / ``LOG_DIR``: the unified command logger.
- Module-level ``run``: called from ``capabilities/vm_platform/lima.py``
  (the SSH-placed Lima control plane), where the caller has a bare
  ``SSHTarget`` and doesn't want to construct a full ``SSHTransport``.
- Module-level ``copy_to``: retained as the bare-``SSHTarget`` scp primitive.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.errors import ConnectivityError
from agentworks.path_rendering import format_host_path
from agentworks.subprocess_io import decode_stream, stdin_bytes

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


class _PropagatingFileHandler(logging.FileHandler):
    """File handler that preserves the logger's write-failure contract."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802, ARG002
        raise


class SSHLogger:
    """Incremental command logger. Writes to disk on every call.

    Replaces the old InitLogger with a unified logger that covers SSH
    commands, init steps, warnings, and general output. All output is
    written incrementally so partial logs survive crashes.

    Usage:
        logger = SSHLogger("myvm", "vm-create", redactions=(auth_key,))
        logger.step("Installing packages")
        run(target, "apt-get install ...", logger=logger)
        logger.warning("package X failed")
        logger.close()
    """

    def __init__(
        self,
        vm_name: str,
        command_stem: str,
        *,
        redactions: tuple[str, ...] = (),
    ) -> None:
        from datetime import UTC, datetime

        timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        self.vm_name = vm_name
        self.path = LOG_DIR / f"{vm_name}-{timestamp}-{command_stem}.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._active_handler: _PropagatingFileHandler | None = None
        normalized: list[str] = []
        for secret in redactions:
            if not secret:
                continue
            for representation in (secret, shlex.quote(secret)):
                if representation not in normalized:
                    normalized.append(representation)
        # Prefer the longest representation so an overlapping shorter token
        # cannot rewrite part of it before the complete value is matched.
        self._redact = tuple(sorted(normalized, key=len, reverse=True))
        self._warnings: list[str] = []

        ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._write(f"# Log: {vm_name} ({command_stem})\n# Started: {ts}\n\n")

    @property
    def display_path(self) -> str:
        """``path`` spelled the way an operator reads it (``~/...``).

        Separate from ``path`` because the two have different jobs: ``path``
        is opened and written to, ``display_path`` is interpolated into
        messages. Every caller that says "SSH log: ..." wants this one; a
        dozen of them used to say ``{logger.path}`` and print an absolute
        path next to a home-relative one from the same screen.

        This is about naming the log, not about what goes INSIDE it. The
        log body stays absolute throughout: it is a verbatim transcript,
        its command lines are the literal argv that ran, and abbreviating
        the prose lines while the ``$ ssh -i /home/you/...`` lines beside
        them stayed absolute would recreate the mixed rendering one level
        down.
        """
        return format_host_path(self.path)

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
        """Record warning text in memory and persist its sanitized form."""
        from datetime import UTC, datetime

        self._warnings.append(msg)
        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._write(f"[{ts}] WARNING: {msg}\n")

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
        ``agentworks.vms.initializer`` and elsewhere do), append the full
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
        self._write("\n".join(lines) + "\n")

    def _write(self, text: str) -> None:
        # Every byte that reaches the file handler passes through this choke
        # point. The guarantee covers the complete redaction set supplied at
        # construction, even when a caller passes those values in raw text.
        # It cannot cover a secret the caller failed to register before the
        # first incremental write. Raw and shell-quoted forms are registered
        # together, and late registration is intentionally unsupported.
        #
        # Keep the sanitized LogRecord and transient FileHandler together in
        # this function. CodeQL's clear-text-storage query recognizes this
        # local data flow; moving either side behind a helper reopens the alert
        # even though the runtime behavior would be equivalent.
        record = logging.LogRecord(
            name="agentworks.ssh.operation",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=self._sanitize(text),
            args=(),
            exc_info=None,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handler = _PropagatingFileHandler(self.path, mode="a", encoding="utf-8", errors="replace")
        self._active_handler = handler
        try:
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler.terminator = ""
            handler.handle(record)
        finally:
            self._close_active_handler()

    def _close_active_handler(self) -> None:
        handler = self._active_handler
        self._active_handler = None
        if handler is not None:
            handler.close()


SSH_CONNECT_TIMEOUT = 30
SSH_DEFAULT_RETRIES = 1

# Client-side keepalive budget for interactive attaches: a probe every
# 15s, giving up after 4 unanswered ones, so a dead peer is detected in
# roughly a minute. Long enough not to tear down a session over a brief
# network blip, short enough that a suspended laptop's terminal gets
# cleaned up promptly on wake. See ``transports/ssh.py:keepalive_args``.
SSH_INTERACTIVE_ALIVE_INTERVAL = 15
SSH_INTERACTIVE_ALIVE_COUNT_MAX = 4


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
    tty: bool | None = None,
    close_stdin: bool,
) -> list[str]:
    """Build the base ``ssh`` argv with ``BatchMode=yes`` (no remote command yet).

    ``tty=True`` forces a pty with ``-tt`` (the only way this primitive allocates
    one). ``tty=False`` explicitly suppresses one with ``-T``, which also defeats
    an operator's ``RequestTTY force`` (a pty would inject CRLF and a teardown
    advisory); ``tty=None`` leaves pty selection to the operator's ssh config.
    Independently, ``-n`` is added when ``close_stdin`` is set so a stdin-reading
    remote command cannot hang and ssh cannot pull from the operator's console;
    it must stay off while a byte-exact ``input_text`` payload is written, so
    callers set ``close_stdin`` only for the no-stdin-payload case.
    """
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if tty:
        args.insert(1, "-tt")
    else:
        if tty is False:
            args.insert(1, "-T")
        if close_stdin:
            args.insert(1, "-n")
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
    input_text: str | None = None,
    tty: bool | None = None,
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
        input_text: Optional text streamed to the remote command on stdin.
            Cannot be combined with ``logger``. The input value is never
            serialized into argv or an error diagnostic. The returned stdout
            and stderr are empty because an arbitrary command may reflect the
            input through either stream. Delivery is byte-exact: the value
            crosses the pipe as UTF-8 with no newline rewriting, so a
            line-oriented consumer receives exactly the line it was sent.

    Returns:
        SSHResult with exit code, stdout, and stderr.
    """
    if input_text is not None and logger is not None:
        raise ValueError("SSH stdin input cannot be combined with command logging")
    if input_text is not None and tty:
        # A forced TTY puts a line discipline between the pipe and the remote
        # command, which echoes input and rewrites CR, so the byte-exact
        # promise above cannot hold. Refuse rather than corrupt a secret.
        raise ValueError("SSH stdin input cannot be combined with a forced TTY")
    if input_text is not None:
        # A byte-exact write must never sit behind a pty. tty=True is refused
        # above; coerce the no-opinion default to an explicit -T so an
        # operator's ``RequestTTY force`` cannot slip a pty in behind the write
        # (which echoes the payload and hangs, or rewrites CR).
        tty = False

    # An ``input_text`` payload keeps stdin open for the byte-exact write;
    # every other call closes stdin with ``-n``.
    args = _ssh_base_args(target, env=env, tty=tty, close_stdin=input_text is None)
    # Fence the remote command from ssh's option parser. See
    # ``SSHTransport.run`` in ``transports/ssh.py`` for the rationale.
    args.append("--")
    if target.login_shell:
        args.append(f"$SHELL -lc {shlex.quote(command)}")
    else:
        args.append(command)

    sensitive_input = input_text is not None
    last_err: Exception | None = None
    for attempt in range(retries):
        if attempt > 0 and on_retry is not None:
            on_retry(attempt, retries)

        sensitive_execution_failure = False
        try:
            result = subprocess.run(
                args,
                # Byte-mode stdin: text mode rewrites LF to CRLF on Windows (see agentworks.subprocess_io).
                input=stdin_bytes(input_text),
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as err:
            if not sensitive_input:
                last_err = err
            if logger is not None:
                logger.log_timeout(command, attempt + 1, retries)
            continue
        except OSError as err:
            if sensitive_input:
                sensitive_execution_failure = True
            else:
                raise SSHError(f"SSH command could not be executed: {command}") from err
        except Exception:
            if not sensitive_input:
                raise
            sensitive_execution_failure = True

        if sensitive_execution_failure:
            raise SSHError(f"SSH stdin command could not be executed: {command}") from None

        ssh_result = SSHResult(
            returncode=result.returncode,
            stdout="" if sensitive_input else decode_stream(result.stdout),
            stderr="" if sensitive_input else decode_stream(result.stderr),
        )
        if logger is not None:
            logger.log_command(command, ssh_result)
        if check and not ssh_result.ok:
            if sensitive_input:
                # Remote output can reflect stdin for an arbitrary command.
                raise SSHError(f"SSH stdin command failed (exit {result.returncode}): {command}") from None
            raise SSHError(
                f"SSH command failed (exit {result.returncode}): {command}\nstderr: {ssh_result.stderr.strip()}"
            )
        return ssh_result

    msg = f"SSH command timed out after {retries} attempts ({timeout}s each): {command}"
    if logger is not None:
        logger.log_error(msg)
    if sensitive_input:
        raise SSHError(msg) from None
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
