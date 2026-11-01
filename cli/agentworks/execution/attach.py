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

import os
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ValidationError
from agentworks.execution import envelope, ssh_process
from agentworks.execution.errors import ExecutionIncomplete
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.terminal import guarded_terminal

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentworks.execution.requests import LoginShell

PARKING_SHUTDOWN_SECONDS = 2.0
"""How long to let the parked launcher clean up after the terminal half returns."""

_SSH_STATUS_CONNECTION_ENDED = 255


def attach(
    request: LoginShell,
    *,
    vm_name: str,
    parking_argv: Callable[[Path], list[str]],
    terminal_argv: Callable[[str], list[str]],
) -> int:
    """Park the request, run the terminal half under the guard, and return its exit status.

    ``parking_argv`` builds the first connection's argv from the client log's
    path and ``terminal_argv`` builds the terminal half's from the guest socket
    path. Both are built before anything is spawned, so a missing trust record
    raises with nothing dispatched. The parking connection fails first on a
    missing python3, a trust mismatch, a refusal or a refused connection, so the
    typed error is raised before the terminal is touched.
    """
    if request.deadline is not None:
        raise ValidationError("an attached shell cannot carry a deadline", entity_kind="vm", entity_name=vm_name)
    token = envelope.new_handover_token()
    nonce = envelope.new_nonce()
    body = envelope.encode(request, nonce=nonce, term=os.environ.get("TERM") or envelope.DEFAULT_TERM, handover=token)
    wire = envelope.frame(envelope.launcher_source()) + envelope.frame(body)
    with ssh_process.client_log() as log:
        parking = parking_argv(log)
        terminal = terminal_argv(f"/tmp/agw-handover-{token}/s")
        with ssh_process.PipeSession(
            parking, request=wire, nonce=nonce, hold_stdin_open=True, on_stdout=_ignore, on_stderr=_ignore
        ) as session:
            parked = False

            def is_parked() -> bool:
                nonlocal parked
                parked = session.stdout.take_parked() or parked
                return parked

            session.pump(is_parked)
            if not parked:
                _raise_parking_failure(vm_name, session, log)
            with guarded_terminal() as guard:
                code = ssh_process.run_terminal(terminal)
                guard.clean_exit = code == 0
            # After the guard: its exit pass leaves the alternate screen, which
            # would wipe anything written before it closes (I6).
            if code == _SSH_STATUS_CONNECTION_ENDED:
                output.warn("connection ended or the command exited 255; terminal restored.")
                output.detail("Remote state is untouched. Re-run the same command to reattach.")
            session.close_stdin()
            session.pump(budget=ssh_process.Deadline.after(PARKING_SHUTDOWN_SECONDS))
            session.end()
    return code


def _ignore(_payload: bytes) -> None:
    return None


def _raise_parking_failure(vm_name: str, session: ssh_process.PipeSession, log: Path) -> None:
    session.end()
    text = ssh_process.client_log_text(log)
    completion = envelope.classify(
        session.stdout,
        session.stderr,
        session.exit_status,
        host_key_failed=ssh_process.HOST_KEY_FAILURE in text,
    )
    result = Result(completion, client_log_tail=ssh_process.tail_lines(text))
    # A parking connection that completed without ever parking broke the protocol.
    if isinstance(completion, Completed):
        result = Result(Unknown(UnknownReason.TRUNCATED), client_log_tail=result.client_log_tail)
        raise ExecutionIncomplete(vm_name, result)
    result.require_completed(vm_name)
    raise ExecutionIncomplete(vm_name, result)  # pragma: no cover - require_completed raises for all else
