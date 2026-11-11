"""The two-connection terminal attach.

ssh takes the window size and terminal modes from its own stdin, so the
operator's real terminal has to be the terminal half's stdin. The request still
has to travel on a private stdin, so it goes on a first connection that parks:
agw-launch creates a private socket directory on the guest and waits. The
terminal half's stage 0 connects to that socket, collects the launcher and the
request from it, and execs the login shell on the terminal. No guest text
reaches a command line, and the secret environment never touches the terminal
connection's argv.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.execution import ssh_process
from agentworks.execution.errors import ExecutionIncomplete
from agentworks.execution.pump import Deadline, PipeSession, StopReason
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.terminal import guarded_terminal

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

PARKING_WAIT_SECONDS = 60
"""How long to wait for the parking connection to report ``parked``, before the terminal is touched.

Its own bound for connecting, authenticating and running the account's startup
files. The launcher's 30 second handover wait starts only after ``parked``.
"""

PARKING_SHUTDOWN_SECONDS = 2.0
"""How long to let the parked launcher clean up after the terminal half returns."""

_SSH_STATUS_CONNECTION_ENDED = 255


def socket_path(token: str) -> str:
    """Where the parked launcher listens on the guest, for a handover ``token``."""
    return f"/tmp/agw-handover-{token}/s"


def attach(
    *,
    vm_name: str,
    wire: bytes,
    nonce: str,
    parking_argv: Sequence[str],
    terminal_argv: Sequence[str],
    log: Path,
) -> int:
    """Park the request, run the terminal half under the guard, and return its exit status.

    ``wire`` is the parking connection's message and ``log`` is the client log
    its argv writes. Both argvs arrive built, before anything is spawned, so a
    missing trust record raises with nothing dispatched. The parking
    connection fails first on a missing python3, a trust mismatch, a refusal or
    a refused connection, so the typed error is raised before the terminal is
    touched. So is a parking connection that never reports ``parked`` (a
    startup file that reads stdin would hold it forever).
    """
    with PipeSession(
        parking_argv, request=wire, nonce=nonce, hold_stdin_open=True, on_stdout=_ignore, on_stderr=_ignore
    ) as session:
        stop = session.pump(lambda: session.stdout.parked, budget=Deadline.after(PARKING_WAIT_SECONDS))
        if not session.stdout.parked:
            _raise_parking_failure(vm_name, session, stop, log)
        with guarded_terminal() as guard:
            code = ssh_process.run_terminal(terminal_argv)
            guard.clean_exit = code == 0
        # After the guard: its exit pass leaves the alternate screen, which
        # would wipe anything written before it closes (I6).
        if code == _SSH_STATUS_CONNECTION_ENDED:
            output.warn("connection ended or the command exited 255; terminal restored.")
            output.detail("Remote state is untouched. Re-run the same command to reattach.")
        session.close_stdin()
        session.pump(budget=Deadline.after(PARKING_SHUTDOWN_SECONDS))
        session.end()
    return code


def _ignore(_payload: bytes) -> None:
    return None


def _raise_parking_failure(vm_name: str, session: PipeSession, stop: StopReason, log: Path) -> None:
    session.end()
    result = ssh_process.outcome(session, stop, log)
    # A parking connection that completed without ever parking broke the protocol.
    if isinstance(result.completion, Completed):
        result = Result(Unknown(UnknownReason.TRUNCATED), client_log_tail=result.client_log_tail)
    # Whatever the connection's fate, no shell started.
    if isinstance(result.completion, Unknown) and result.completion.reason is not UnknownReason.HOST_KEY:
        raise ExecutionIncomplete(vm_name, result, attaching=True)
    result.require_completed(vm_name)
    raise ExecutionIncomplete(vm_name, result)  # pragma: no cover - require_completed raises for all else
