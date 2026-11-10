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
from agentworks.execution.results import Unknown, UnknownReason
from agentworks.interrupts import interrupts_deferred

from .phase_a import OperationLog

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentworks.execution.access import VMAccess
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
    """The operation log initialization writes, Phase B's and an agent's (``SSHLogger`` satisfies it).

    Steps write it; native setup reads its redactions
    (``harness_setup.lifecycle.SetupLog``), and the outcome counts its
    warnings.
    """

    def warning(self, msg: str) -> None: ...

    def sanitize(self, text: str) -> str: ...

    @property
    def redactions(self) -> tuple[str, ...]: ...

    @property
    def has_warnings(self) -> bool: ...

    @property
    def warnings(self) -> Sequence[str]: ...


def _suspect(path: str) -> str:
    """The warning for ``path`` when putting its prior content back failed: what is wrong and what to do."""
    return (
        f"{path} may hold content that is not in effect: its follow-up action did not succeed and putting "
        "its prior content back failed, so a re-run will not retry it; put the prior content back or remove "
        "the file, then run 'agw vm reinit'"
    )


def describe_failure(error: Exception, sanitize: Callable[[str], str]) -> str:
    """``error``'s message, then, for a ``CommandFailed`` whose streams were logged, its last stderr lines.

    The whole stderr is redacted with ``sanitize`` before any line is cut, so
    a secret spanning lines goes whole; the tail is then made safe to show on
    a terminal. A request that kept its streams out of the log shows none.
    """
    message = sanitize(str(error))
    if not isinstance(error, CommandFailed) or not error.streams_logged:
        return message
    stderr = sanitize(error.result.stderr.decode("utf-8", errors="replace"))
    tail = ssh_process.tail_lines(stderr, WARNING_TAIL_LINES)
    return f"{message}\n{tail}" if tail else message


def status(access: VMAccess, request: RunRequest) -> int:
    """Run ``request`` on ``access`` and return the guest's exit status.

    For a request whose status is the answer (a probe, a validation); an
    outcome that is not a completion raises its typed error. A request that
    need only complete is ``access.run(request).require_completed(...)``.
    """
    return access.run(request).require_completed(access.vm_name)


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
    file still holding ``data``, before the failure propagates; so does a
    failure of the write itself that may have run (an unknown outcome, an
    interrupt). The next run then sees the difference again and retries.
    :func:`_put_back` decides, and warns, when the file is left stranded.
    """
    wrote = False
    try:
        wrote = files.update(path, data, observed=observed, mode=mode, parent_mode=parent_mode, elevated=True)
        if not wrote:
            return False
        apply()
    except BaseException as failure:
        if wrote or _may_have_run(failure):
            _put_back(files, log, path, data, observed=observed, mode=mode, parent_mode=parent_mode)
        raise
    return True


def _may_have_run(failure: BaseException) -> bool:
    """Whether the request ``failure`` ended may have run: an interrupt, or an unknown outcome the VM did not refuse."""
    if isinstance(failure, ExecutionIncomplete):
        completion = failure.result.completion
        return isinstance(completion, Unknown) and completion.reason is not UnknownReason.AUTH_FAILED
    return not isinstance(failure, Exception)


def _put_back(
    files: Files, log: InitLog, path: str, data: bytes, *, observed: bytes | None, mode: int, parent_mode: int
) -> None:
    """Put ``observed`` back at ``path`` while it holds ``data``, or remove what this run created.

    A ``FileConflict`` means the file does not hold ``data``: the write never
    landed, or something replaced it since, and either way a re-run sees the
    difference and retries. Any other step failure leaves ``data`` in place
    with nothing applying it, which a re-run reads as applied, so it warns
    naming the file. Interrupts are held until the restore ends.
    """
    written = hashlib.sha256(data).hexdigest()
    with interrupts_deferred():
        try:
            if observed is None:
                files.remove_if(path, expected=written, elevated=True)
            else:
                files.write_if(path, observed, expected=written, mode=mode, parent_mode=parent_mode, elevated=True)
        except FileConflict:
            pass
        except STEP_FAILURES as error:
            warn(log, f"{_suspect(path)}: {describe_failure(error, log.sanitize)}")


def warn(log: InitLog, message: str) -> None:
    """Warn with ``message`` on the console and in the log, which leaves initialization partial."""
    log.warning(message)
    output.warn(message)


def warn_step(log: InitLog, summary: str, error: Exception) -> None:
    """Warn, on the console and in the log, that the step ``summary`` names failed with ``error``."""
    warn(log, f"{summary}: {describe_failure(error, log.sanitize)}")
