"""Where a Lima site's ``limactl`` runs: this machine, or the site's placement host over SSH.

:class:`LimaHost` runs ``limactl`` commands, the VM's root channel
(``limactl shell <instance> sudo -n /bin/bash -s``), its native shell and, on a placement host,
the detached create that must survive the workstation's connection. A remote
command runs under the host user's login shell (``$SHELL -lc``), which is
what puts Homebrew's ``limactl`` on its ``PATH``, through the host policy
argv, so the operator's SSH configuration for the host applies.

This module imports nothing from the legacy transport stack.
"""

from __future__ import annotations

import contextlib
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import agentworks.execution as execution
from agentworks import output
from agentworks.errors import ExternalError
from agentworks.execution import root_script, ssh_process
from agentworks.subprocess_io import decode_stream, stdin_bytes

if TYPE_CHECKING:
    from agentworks.execution.root_script import RawRun

_DETACHED_POLL_SECONDS = 3
_DETACHED_QUIET_SECONDS = 300
_DETACHED_READ_ATTEMPTS = 6
_DETACHED_READ_RETRY_SECONDS = 5
# ssh's own status for a connection that failed. A remote command's 255 is indistinguishable,
# and none of the detached checks below exits 255 by itself.
_SSH_CONNECTION_FAILED = 255

# Writes the PID, runs the command, then writes its status. The brace group
# makes the redirection capture all of a compound command's output.
_WRAPPER_TEMPLATE = """\
#!/bin/bash
echo $$ > {pid_file}
{{ {command} ; }} > {output_file} 2>&1
echo $? > {status_file}
"""


class LimaCommandFailed(ExternalError):
    """A ``limactl`` command, or the connection to the placement host that runs it, failed."""


@dataclass(frozen=True)
class DetachedResult:
    """A detached command's exit status (1 when it could not be read) and its whole output."""

    exit_code: int
    output: str


@dataclass(frozen=True)
class _Completed:
    returncode: int
    stdout: str
    stderr: str


class LimaHost:
    """``limactl`` here (``host`` is ``None``) or on ``host``, the operator's SSH name for it."""

    def __init__(self, host: str | None) -> None:
        self.host = host

    @property
    def is_remote(self) -> bool:
        return self.host is not None

    def run(self, command: str, *, input: str | None = None, check: bool = True, timeout: float | None = None) -> str:
        """Run ``command`` once and return its stdout.

        A command given ``input`` exposes neither stream, in its result or
        its error, because an arbitrary program can reflect its input. A
        non-zero status raises :class:`LimaCommandFailed` under ``check``,
        and a timeout always does.
        """
        completed = self._execute(command, input=input, timeout=timeout)
        if check and completed.returncode != 0:
            where = f" on '{self.host}'" if self.is_remote else ""
            if input is not None:
                raise LimaCommandFailed(f"limactl stdin command failed{where} (exit {completed.returncode}): {command}")
            raise LimaCommandFailed(
                f"limactl command failed{where} (exit {completed.returncode}): {command}\n"
                f"stderr: {completed.stderr.strip()}"
            )
        return "" if input is not None else completed.stdout

    def run_as_root(self, instance: str, script: bytes, deadline: float) -> RawRun:
        """The VM's root channel: ``limactl shell <instance> sudo -n /bin/bash -s`` with ``script`` on stdin."""
        return root_script.run_process(
            self._argv(f"limactl shell {shlex.quote(instance)} sudo -n /bin/bash -s"), script, deadline
        )

    def shell(self, instance: str) -> int:
        """An interactive ``limactl shell <instance>`` on the operator's terminal; returns its exit status.

        Remotely the host connection asks for a terminal (``ssh -t``) and runs
        ``limactl`` under the host user's login shell, as every remote command does.
        """
        command = f"limactl shell {shlex.quote(instance)}"
        return ssh_process.run_terminal(self._argv(command, mode=ssh_process.ArgvMode.TERMINAL))

    def upload(self, path: str, content: str) -> None:
        """Write ``content`` to ``path``, mode 0600, through stdin, never an argument."""
        self.run(f"umask 077 && cat > {shlex.quote(path)}", input=content)

    # -- Detached commands, for work that must outlive the connection --------------------

    def run_detached(self, command: str, *, base_path: str, timeout: int) -> DetachedResult:
        """Run ``command`` under ``nohup`` on the placement host and poll it to completion.

        ``base_path`` names its ``.sh``, ``.out``, ``.pid`` and ``.status``
        files. A leftover status file is an interrupted earlier run's, so it
        is cleared with a warning and a fresh run starts. At ``timeout``
        seconds the command is killed and the result is exit 1 with the
        output so far. Every file is removed at the end.
        """
        files = _DetachedFiles(base_path)
        if self._returncode(f"test -f {files.status}") == 0:
            output.warn(f"clearing stale result files from a previous interrupted run ({base_path}.*)")
        self.upload(files.wrapper, _WRAPPER_TEMPLATE.format(command=command, **files.template_fields()))
        self._returncode(f"rm -f {files.output} {files.pid} {files.status}")
        self._returncode(f"nohup /bin/bash {files.wrapper} </dev/null >/dev/null 2>&1 &")
        time.sleep(0.5)
        self._poll_until_done(files, timeout=timeout)
        captured = self._read_with_retries(f"cat {files.output} 2>/dev/null")
        if captured is None:
            output.warn("unable to retrieve the detached command's output after repeated connection failures")
        status = self._read_with_retries(f"cat {files.status} 2>/dev/null")
        try:
            exit_code = int((status or "").strip())
        except ValueError:
            exit_code = 1
        with contextlib.suppress(LimaCommandFailed):
            self._returncode(f"rm -f {files.wrapper} {files.pid} {files.status} {files.output}")
        return DetachedResult(exit_code=exit_code, output=captured or "")

    def kill_detached(self, base_path: str) -> None:
        """Stop the command a :meth:`run_detached` with ``base_path`` started, then remove its files.

        A no-op when it never started or already finished. Best-effort, so a
        caller unwinding from an earlier failure keeps that failure:
        ``nohup`` exists precisely so the command survives this process, so
        an interrupted caller must stop it before undoing what it built.
        """
        files = _DetachedFiles(base_path)
        try:
            with contextlib.suppress(LimaCommandFailed):
                self._returncode(f"test -f {files.pid} && kill $(cat {files.pid}) 2>/dev/null")
        finally:
            with contextlib.suppress(Exception):
                self._returncode(f"rm -f {files.output} {files.wrapper} {files.pid} {files.status}")

    def _poll_until_done(self, files: _DetachedFiles, *, timeout: int) -> None:
        """Wait for the status file, warning on a long silence and killing the command at ``timeout``."""
        started = last_output = time.monotonic()
        size = 0
        warned_quiet = warned_unanswered = False

        def ask(command: str) -> _Completed | None:
            nonlocal warned_unanswered
            answer = self._ask(command)
            if answer is None and not warned_unanswered:
                output.warn(f"the placement host '{self.host}' is not answering; still polling")
                warned_unanswered = True
            return answer

        while True:
            time.sleep(_DETACHED_POLL_SECONDS)
            if time.monotonic() - started > timeout:
                output.warn(f"the detached command timed out after {timeout}s; killing it")
                with contextlib.suppress(LimaCommandFailed):
                    self._returncode(f"test -f {files.pid} && kill $(cat {files.pid}) 2>/dev/null")
                return
            # The host may be briefly unreachable: an unanswered check is asked again at the next
            # poll, within the timeout, while the wrapper keeps running there.
            written = ask(f"wc -c < {files.output} 2>/dev/null")
            if written is not None and written.stdout.strip().isdigit() and int(written.stdout) > size:
                size, last_output, warned_quiet = int(written.stdout), time.monotonic(), False
            finished = ask(f"test -f {files.status}")
            if finished is None:
                continue
            if finished.returncode == 0:
                return
            alive = ask(f"ps -p $(cat {files.pid}) > /dev/null 2>&1")
            if alive is None:
                continue
            if alive.returncode != 0:
                return  # gone without a status: an unexpected end
            quiet = time.monotonic() - last_output
            if quiet > _DETACHED_QUIET_SECONDS and not warned_quiet:
                output.warn(f"the detached command printed nothing for {int(quiet)}s (still running)...")
                warned_quiet = True

    def _read_with_retries(self, command: str) -> str | None:
        """``command``'s stdout, asked up to six times while the host does not answer; ``None`` if it never does."""
        for _ in range(_DETACHED_READ_ATTEMPTS):
            answer = self._ask(command)
            if answer is not None:
                return answer.stdout
            time.sleep(_DETACHED_READ_RETRY_SECONDS)
        return None

    def _ask(self, command: str) -> _Completed | None:
        """``command``'s result, or ``None`` when the host did not answer: ssh's 255.

        Any output is ignored, so a login banner cannot make a dropped
        connection read as an answer.
        """
        completed = self._execute(command, input=None, timeout=None)
        if completed.returncode == _SSH_CONNECTION_FAILED:
            return None
        return completed

    # -- Processes ---------------------------------------------------------------------

    def _returncode(self, command: str) -> int:
        return self._execute(command, input=None, timeout=None).returncode

    def _argv(
        self,
        command: str,
        *,
        mode: Literal[ssh_process.ArgvMode.PIPE, ssh_process.ArgvMode.TERMINAL] = ssh_process.ArgvMode.PIPE,
    ) -> list[str]:
        if self.host is None:
            return shlex.split(command)
        return ssh_process.build_host_argv(
            execution.resolve_ssh(), self.host, mode, f"$SHELL -lc {shlex.quote(command)}"
        )

    def _execute(self, command: str, *, input: str | None, timeout: float | None) -> _Completed:
        """Run ``command`` once, here or on the host; the one place this module starts a process.

        A local command with no input inherits stdin, as ``limactl`` always
        has; a remote one reads the null device, so ssh never takes the
        operator's terminal input.
        """
        if input is not None:
            stdin: int | None = subprocess.PIPE
        else:
            stdin = subprocess.DEVNULL if self.is_remote else None
        process = ssh_process.spawn(self._argv(command), stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(stdin_bytes(input), timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            where = f" on '{self.host}'" if self.is_remote else ""
            raise LimaCommandFailed(f"limactl command timed out{where} after {timeout}s: {command}") from None
        except BaseException:
            process.kill()
            process.wait()
            raise
        return _Completed(process.returncode, decode_stream(stdout), decode_stream(stderr))


@dataclass(frozen=True)
class _DetachedFiles:
    base_path: str

    @property
    def wrapper(self) -> str:
        return f"{self.base_path}.sh"

    @property
    def output(self) -> str:
        return f"{self.base_path}.out"

    @property
    def pid(self) -> str:
        return f"{self.base_path}.pid"

    @property
    def status(self) -> str:
        return f"{self.base_path}.status"

    def template_fields(self) -> dict[str, str]:
        return {"output_file": self.output, "pid_file": self.pid, "status_file": self.status}
