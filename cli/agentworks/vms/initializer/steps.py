"""What every Phase B step shares: its bound, what it catches, the log it writes and how it warns.

A Phase B step that fails warns, leaves initialization partial and lets the
next step run. :data:`STEP_FAILURES` is what a step catches: a request that
did not complete or failed, one the guest refused to start, and a refused or
conflicting file operation. A missing or mismatched host key, a VM that
cannot run requests and an elevated request on an agent's access are not
among them: each means every later request fails the same way, so it
propagates and fails initialization once.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol

from agentworks import output
from agentworks.errors import ConnectivityError, ExternalError
from agentworks.execution import ssh_process
from agentworks.execution.errors import (
    CommandFailed,
    ExecutionIncomplete,
    FileConflict,
    FileLockTimeout,
    PathRefused,
    RequestRefused,
)
from agentworks.execution.results import Unknown
from agentworks.interrupts import interrupts_deferred

from .phase_a import OperationLog

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution.files import Files
    from agentworks.execution.requests import RunRequest

STEP_DEADLINE = 60
"""Seconds that bound each Phase B request that has no bound of its own."""

STEP_FAILURES: tuple[type[Exception], ...] = (
    ConnectivityError,
    ExternalError,
    RequestRefused,
    FileConflict,
    PathRefused,
    FileLockTimeout,
)
"""The errors a Phase B step turns into a warning."""

WARNING_TAIL_LINES = 3
"""How many of a failed command's last stderr lines a step warning shows."""


class InitLog(OperationLog, Protocol):
    """What Phase B writes to the operation log (``SSHLogger`` satisfies it)."""

    def warning(self, msg: str) -> None: ...

    def sanitize(self, text: str) -> str: ...


def _suspect(path: str) -> str:
    """What the operator does about ``path`` when putting its prior content back failed."""
    return (
        f"putting back the prior content of {path} also failed, so it may hold content that is not in "
        "effect, and a re-run will not retry it: put the prior content back or remove the file, then run "
        "'agw vm reinit'"
    )


class NotRestored(ExternalError):
    """A written file's follow-up action failed, and putting its prior content back failed too.

    ``path`` may now hold content that was never put in effect, and a re-run
    reads it as already applied, so nothing retries it. ``failure`` is the
    follow-up's error and ``restore_error`` the restore's.
    """

    def __init__(self, vm_name: str, path: str, failure: Exception, restore_error: Exception) -> None:
        super().__init__(
            f"{failure}; on VM '{vm_name}', {_suspect(path)}: {restore_error}",
            entity_kind="vm",
            entity_name=vm_name,
        )
        self.path = path
        self.failure = failure
        self.restore_error = restore_error


def describe_failure(error: Exception, sanitize: Callable[[str], str]) -> str:
    """``error``'s message, then, for a ``CommandFailed`` whose streams were logged, its last stderr lines.

    The whole stderr is redacted with ``sanitize`` before any line is cut, so
    a secret spanning lines goes whole; the tail is then made safe to show on
    a terminal. A request that kept its streams out of the log shows none.
    :class:`NotRestored` is described as its follow-up's failure, then the
    file that is now suspect and the restore's failure.
    """
    if isinstance(error, NotRestored):
        return (
            f"{describe_failure(error.failure, sanitize)}\n"
            f"{_suspect(error.path)}: {describe_failure(error.restore_error, sanitize)}"
        )
    message = sanitize(str(error))
    if not isinstance(error, CommandFailed) or not error.streams_logged:
        return message
    stderr = sanitize(error.result.stderr.decode("utf-8", errors="replace"))
    tail = ssh_process.tail_lines(stderr, WARNING_TAIL_LINES)
    return f"{message}\n{tail}" if tail else message


def status(files: Files, request: RunRequest) -> int:
    """Run ``request`` on ``files.access`` and return the guest's exit status.

    For a request whose status is the answer (a probe, a validation); an
    outcome that is not a completion raises its typed error.
    """
    return files.access.run(request).require_completed(files.access.vm_name)


def update_and_apply(
    files: Files,
    log: InitLog,
    path: str,
    data: bytes,
    *,
    observed: bytes | None,
    mode: int,
    parent_mode: int,
    apply: Callable[[], object],
) -> bool:
    """Publish ``data`` over ``observed`` at ``path`` as root, then ``apply`` it; whether it wrote.

    For a file that takes effect only through a follow-up action (a reload,
    a validation and a reload): the file counts as applied once ``apply``
    returns. Any failure after the write, an interrupt included, puts
    ``observed`` back (or removes the file it created), conditional on the
    file still holding ``data``, before the failure propagates. The next run
    then sees the difference again and retries. So does a write whose
    outcome is unknown (a lost connection, an interrupt), where a restore
    that finds the file not holding ``data`` means the write did not land.

    When the restore fails, the file may hold ``data`` with nothing applying
    it, and a re-run sees nothing to do: a step failure becomes
    :class:`NotRestored`, and any other failure (an interrupt) warns naming
    the file on ``log`` before it propagates.
    """
    wrote = False
    try:
        wrote = files.update(path, data, observed=observed, mode=mode, parent_mode=parent_mode, elevated=True)
        if not wrote:
            return False
        apply()
    except BaseException as failure:
        landed = wrote or _outcome_unknown(failure)
        if not landed:
            raise
        restore_error: Exception | None = None
        try:
            with interrupts_deferred():
                restore_error = _put_back(files, path, data, observed=observed, mode=mode, parent_mode=parent_mode)
        except BaseException:
            # An interrupt held during the restore arrives here, in place of ``failure``.
            if restore_error is not None:
                warn(log, f"{_suspect(path)}: {describe_failure(restore_error, log.sanitize)}")
            raise
        if restore_error is None or (not wrote and isinstance(restore_error, FileConflict)):
            raise
        if isinstance(failure, STEP_FAILURES):
            raise NotRestored(files.access.vm_name, path, failure, restore_error) from failure
        warn(log, f"{_suspect(path)}: {describe_failure(restore_error, log.sanitize)}")
        raise
    return True


def _outcome_unknown(failure: BaseException) -> bool:
    """Whether the request ``failure`` ended may have run: an unknown completion, or an interrupt."""
    if isinstance(failure, ExecutionIncomplete):
        return isinstance(failure.result.completion, Unknown)
    return not isinstance(failure, Exception)


def _put_back(
    files: Files, path: str, data: bytes, *, observed: bytes | None, mode: int, parent_mode: int
) -> Exception | None:
    """Put ``observed`` back at ``path`` while it holds ``data``; the step failure that stopped it, if any."""
    written = hashlib.sha256(data).hexdigest()
    try:
        if observed is None:
            files.remove_if(path, expected=written, elevated=True)
        else:
            files.write_if(path, observed, expected=written, mode=mode, parent_mode=parent_mode, elevated=True)
    except STEP_FAILURES as error:
        return error
    return None


def warn(log: InitLog, message: str) -> None:
    """Warn with ``message`` on the console and in the log, which leaves initialization partial."""
    log.warning(message)
    output.warn(message)


def warn_step(log: InitLog, summary: str, error: Exception) -> None:
    """Warn, on the console and in the log, that the step ``summary`` names failed with ``error``."""
    warn(log, f"{summary}: {describe_failure(error, log.sanitize)}")
