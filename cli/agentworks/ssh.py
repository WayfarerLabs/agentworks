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

import contextlib
import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.errors import ConnectivityError
from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType
    from typing import NoReturn


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


_EMPTY_SSH_RESULT = SSHResult(returncode=0, stdout="", stderr="")


class _SensitiveExceptionGraphCleanup:
    """Mutable owner for exception links and tracebacks while detaching them."""

    def __init__(self) -> None:
        self.pending: list[BaseException] = []
        self.current: BaseException | None = None
        self.tracebacks: list[TracebackType | None] = []
        self.seen: set[int] = set()

    def detach(self) -> None:
        """Detach every graph edge without binding a traceback to this frame."""
        import traceback

        while self.current is not None or self.pending:
            if self.current is None:
                self.current = self.pending.pop()
            if id(self.current) in self.seen:
                self.current = None
                continue

            if self.current.__cause__ is not None:
                self.pending.append(self.current.__cause__)
            if self.current.__context__ is not None:
                self.pending.append(self.current.__context__)
            if isinstance(self.current, BaseExceptionGroup):
                self.pending.extend(self.current.exceptions)

            # The traceback moves directly into the mutable owner. If this
            # sequence is interrupted, the enclosing finally scrubs the owner
            # before any retained helper frame can expose downstream locals.
            self.tracebacks.append(self.current.__traceback__)
            self.current.__traceback__ = None
            self.current.__cause__ = None
            self.current.__context__ = None
            self.seen.add(id(self.current))
            self.current = None

            while self.tracebacks:
                if self.tracebacks[-1] is not None:
                    with contextlib.suppress(BaseException):
                        traceback.clear_frames(self.tracebacks[-1])
                self.tracebacks.pop()

    def scrub(self) -> None:
        """Finish detaching and release every carrier-held graph reference."""
        if self.current is not None:
            self.pending.append(self.current)
            self.current = None
        try:
            self.detach()
        finally:
            self.current = None
            self.pending.clear()
            self.tracebacks.clear()
            self.seen.clear()


def _strip_sensitive_exception_graph(failure: BaseException) -> None:
    """Detach downstream frames and links that may retain sensitive input."""
    cleanup = _SensitiveExceptionGraphCleanup()
    try:
        cleanup.pending.append(failure)
        cleanup.detach()
    except BaseException as interruption:
        # Adoption itself is inside this fence. Re-adopt the root before
        # scrubbing so an interruption on the first transfer line cannot
        # leave its native traceback linked through the retained parameter.
        cleanup.pending.append(failure)
        cleanup.scrub()
        interruption.__cause__ = None
        interruption.__context__ = None
        raise interruption from None
    finally:
        cleanup.scrub()


def _reraise_stripped_sensitive_exception(failure: BaseException) -> NoReturn:
    """Re-raise the same control-flow object without its sensitive native graph."""
    _strip_sensitive_exception_graph(failure)
    raise failure from None


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


class _PendingLogText:
    """Mutable transfer from raw caller text to the sanitized write sink."""

    def __init__(self) -> None:
        self.raw = ""
        self.sanitized = ""

    def scrub(self) -> None:
        self.raw = ""
        self.sanitized = ""

    def __repr__(self) -> str:
        return "_PendingLogText(<scrubbed>)"


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
        normalized: list[str] = []
        secret = ""
        representation = ""
        try:
            from datetime import UTC, datetime

            self._redact: tuple[str, ...] = ()
            self._warnings: list[str] = []
            self._active_handler: _PropagatingFileHandler | None = None
            for secret in redactions:
                if not secret:
                    continue
                for representation in (secret, shlex.quote(secret)):
                    if representation not in normalized:
                        normalized.append(representation)
            # Prefer the longest representation so an overlapping shorter token
            # cannot rewrite part of it before the complete value is matched.
            self._redact = tuple(sorted(normalized, key=len, reverse=True))
            redactions = ()
            secret = ""
            representation = ""
            normalized.clear()

            timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
            self.vm_name = vm_name
            self.path = LOG_DIR / f"{vm_name}-{timestamp}-{command_stem}.log"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            self._write(f"# Log: {vm_name} ({command_stem})\n# Started: {ts}\n\n")
        except BaseException:
            # Constructor failures retain this frame too. Do not leave the
            # partly-built logger as a second owner of its caller's tokens.
            self._redact = ()
            raise
        finally:
            redactions = ()
            secret = ""
            representation = ""
            normalized.clear()

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

    def _sanitize(self, pending: _PendingLogText) -> None:
        """Sanitize through the caller-owned mutable transfer carrier."""
        pending.sanitized = pending.raw
        for index in range(len(self._redact)):
            pending.sanitized = pending.sanitized.replace(self._redact[index], "[REDACTED]")

    def step(self, name: str) -> None:
        """Log the start of a named step."""
        from datetime import UTC, datetime

        pending: _PendingLogText | None = None
        try:
            pending = _PendingLogText()
            ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
            pending.raw = f"--- [{ts}] {name} ---\n"
            name = ""
            self._write_pending(pending)
        finally:
            name = ""
            if pending is not None:
                pending.scrub()

    def output(self, text: str) -> None:
        """Log general output."""
        pending: _PendingLogText | None = None
        try:
            if text:
                pending = _PendingLogText()
                pending.raw = text if text.endswith("\n") else text + "\n"
                text = ""
                self._write_pending(pending)
        finally:
            text = ""
            if pending is not None:
                pending.scrub()

    def log_command(self, command: str, result: SSHResult) -> None:
        """Log a completed command with its output."""
        from datetime import UTC, datetime

        pending: _PendingLogText | None = None
        lines: list[str] = []
        try:
            pending = _PendingLogText()
            ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
            lines = [f"[{ts}] $ {command}  (exit {result.returncode})"]
            if result.stdout:
                lines.append(result.stdout.rstrip())
            if result.stderr:
                lines.append(f"STDERR: {result.stderr.rstrip()}")
            lines.append("")
            pending.raw = "\n".join(lines) + "\n"
            command = ""
            result = _EMPTY_SSH_RESULT
            lines.clear()
            self._write_pending(pending)
        finally:
            command = ""
            result = _EMPTY_SSH_RESULT
            lines.clear()
            if pending is not None:
                pending.scrub()

    def log_timeout(self, command: str, attempt: int, retries: int) -> None:
        """Log a timeout event."""
        from datetime import UTC, datetime

        pending: _PendingLogText | None = None
        try:
            pending = _PendingLogText()
            ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
            pending.raw = f"[{ts}] TIMEOUT (attempt {attempt}/{retries}): {command}\n"
            command = ""
            self._write_pending(pending)
        finally:
            command = ""
            if pending is not None:
                pending.scrub()

    def warning(self, msg: str) -> None:
        """Record warning text in memory and persist its sanitized form."""
        from datetime import UTC, datetime

        pending: _PendingLogText | None = None
        prior_warning_count = len(self._warnings)
        try:
            pending = _PendingLogText()
            pending.raw = msg
            self._warnings.append(msg)
            msg = ""
            ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
            pending.raw = f"[{ts}] WARNING: {pending.raw}\n"
            self._write_pending(pending)
        except BaseException:
            del self._warnings[prior_warning_count:]
            raise
        finally:
            msg = ""
            if pending is not None:
                pending.scrub()

    def log_error(self, msg: str) -> None:
        """Log an error message."""
        from datetime import UTC, datetime

        pending: _PendingLogText | None = None
        try:
            pending = _PendingLogText()
            ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
            pending.raw = f"[{ts}] ERROR: {msg}\n"
            msg = ""
            self._write_pending(pending)
        finally:
            msg = ""
            if pending is not None:
                pending.scrub()

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    @property
    def has_warnings(self) -> bool:
        return len(self._warnings) > 0

    def discard_redactions(self) -> None:
        """Forget plaintext redaction tokens after the operation ends.

        Callers retain the logger long enough to sanitize every incremental
        write and traceback. Once no further write is possible, keeping the
        raw tokens only extends their lifetime through exception graphs.
        """
        self._redact = ()

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

        pending: _PendingLogText | None = None
        exc_type = None
        exc: BaseException | None = None
        exc_tb = None
        tb_text = ""
        lines: list[str] = []
        try:
            exc_type, exc, exc_tb = sys.exc_info()
            if exc is not None:
                pending = _PendingLogText()
                ts_exc = datetime.now(tz=UTC).strftime("%H:%M:%S")
                tb_text = "".join(traceback.format_exception(exc_type, exc, exc_tb))
                pending.raw = f"[{ts_exc}] EXCEPTION:\n{tb_text}\n"
                tb_text = ""
                exc_type = None
                exc = None
                exc_tb = None
                self._write_pending(pending)
                pending.scrub()

            ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            lines = [f"\n# Finished: {ts}"]
            if self._warnings:
                lines.append(f"# Warnings: {len(self._warnings)}")
            pending = _PendingLogText()
            pending.raw = "\n".join(lines) + "\n"
            lines.clear()
            self._write_pending(pending)
        finally:
            exc_type = None
            exc = None
            exc_tb = None
            tb_text = ""
            lines.clear()
            if pending is not None:
                pending.scrub()

    def _write(self, text: str) -> None:
        pending: _PendingLogText | None = None
        try:
            pending = _PendingLogText()
            pending.raw = text
            text = ""
            self._write_pending(pending)
        finally:
            text = ""
            if pending is not None:
                pending.scrub()

    def _write_pending(self, pending: _PendingLogText) -> None:
        try:
            try:
                self._sanitize(pending)
            finally:
                pending.raw = ""
            self._write_sanitized(pending.sanitized)
        except BaseException:
            self.discard_redactions()
            raise
        finally:
            pending.scrub()

    def _write_sanitized(self, text: str) -> None:
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
        try:
            record = logging.LogRecord(
                name="agentworks.ssh.operation",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=text,
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
        except BaseException:
            # A propagated write failure retains every Agentworks frame.
            # Once persistence has failed the logger cannot safely continue,
            # so release its plaintext mask tokens before re-raising the
            # original exception unchanged.
            self.discard_redactions()
            raise

    def _close_active_handler(self) -> None:
        import sys

        active_failure = sys.exc_info()[1]
        handler = self._active_handler
        self._active_handler = None
        if handler is not None:
            try:
                handler.close()
            except BaseException:
                if active_failure is None:
                    raise


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
    input_text: str | None = None,
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
            input through either stream.

    Returns:
        SSHResult with exit code, stdout, and stderr.
    """
    sensitive_input = input_text is not None
    args: list[str] = []
    last_err: Exception | None = None
    result: subprocess.CompletedProcess[str] | None = None
    ssh_result: SSHResult | None = None
    try:
        if input_text is not None and logger is not None:
            raise ValueError("SSH stdin input cannot be combined with command logging")

        args = _ssh_base_args(target, env=env)
        # Fence the remote command from ssh's option parser. See
        # ``SSHTransport.run`` in ``transports/ssh.py`` for the rationale.
        args.append("--")
        if target.login_shell:
            args.append(f"$SHELL -lc {shlex.quote(command)}")
        else:
            args.append(command)

        for attempt in range(retries):
            if attempt > 0 and on_retry is not None:
                on_retry(attempt, retries)

            # A prior attempt may have produced output before the next one
            # times out or is interrupted. Never retain that raw result in a
            # sensitive-input traceback frame.
            result = None
            ssh_result = None
            sensitive_execution_failure = False
            timed_out = False
            try:
                result = subprocess.run(
                    args,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as err:
                timed_out = True
                if sensitive_input:
                    # TimeoutExpired may retain partial stdout/stderr. Keep it
                    # out of the eventual exception graph at this boundary.
                    pass
                else:
                    last_err = err
                if logger is not None:
                    logger.log_timeout(command, attempt + 1, retries)
            except Exception as native_failure:
                if not sensitive_input:
                    raise
                # Native subprocess failures may retain input or reflected
                # output in their args and traceback. Translate only for the
                # sensitive-input mode, after leaving the except suite, so the
                # native exception is not linked as context.
                _strip_sensitive_exception_graph(native_failure)
                sensitive_execution_failure = True
            except BaseException as native_control_flow:
                if not sensitive_input:
                    raise
                _reraise_stripped_sensitive_exception(native_control_flow)

            if sensitive_execution_failure:
                raise SSHError(f"SSH stdin command could not be executed: {command}") from None
            if timed_out:
                continue

            assert result is not None
            ssh_result = SSHResult(
                returncode=result.returncode,
                stdout="" if sensitive_input else result.stdout,
                stderr="" if sensitive_input else result.stderr,
            )
            if logger is not None:
                logger.log_command(command, ssh_result)
            if check and not ssh_result.ok:
                if sensitive_input:
                    # Remote stderr can reflect stdin for an arbitrary command.
                    # Omit it and clear both result objects before raising.
                    returncode = result.returncode
                    result = None
                    ssh_result = None
                    raise SSHError(f"SSH stdin command failed (exit {returncode}): {command}") from None
                raise SSHError(
                    f"SSH command failed (exit {result.returncode}): {command}\nstderr: {result.stderr.strip()}"
                )
            return ssh_result

        msg = f"SSH command timed out after {retries} attempts ({timeout}s each): {command}"
        if logger is not None:
            logger.log_error(msg)
        if sensitive_input:
            raise SSHError(msg) from None
        raise SSHError(msg) from last_err
    finally:
        # ``run`` is a shared sensitive-input boundary. This also executes for
        # BaseException control flow, preserving the exact exception while
        # removing secret-bearing values from this frame's retained locals.
        input_text = None
        args.clear()
        result = None
        ssh_result = None
        last_err = None


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
