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

from typing import TYPE_CHECKING, Protocol

from agentworks import output
from agentworks.errors import ConnectivityError, ExternalError
from agentworks.execution import ssh_process
from agentworks.execution.errors import CommandFailed, FileConflict, FileLockTimeout, PathRefused, RequestRefused

from .phase_a import OperationLog

if TYPE_CHECKING:
    from collections.abc import Callable

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

    @property
    def redactions(self) -> tuple[str, ...]: ...

    @property
    def warnings(self) -> list[str]: ...

    @property
    def has_warnings(self) -> bool: ...


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


def warn_step(log: InitLog, summary: str, error: Exception) -> None:
    """Warn, on the console and in the log, that the step ``summary`` names failed with ``error``."""
    message = f"{summary}: {describe_failure(error, log.sanitize)}"
    log.warning(message)
    output.warn(message)
