"""The terminal handover: a parked request, collected by a terminal half.

ssh takes the window size and terminal modes from its own stdin, so the
operator's real terminal has to be the terminal half's stdin. The request still
has to travel on a private stdin, so it goes on a first connection that parks:
agw-launch creates a private socket directory on the guest and waits. The
terminal half's stage 0 connects to that socket, collects the launcher and the
request from it, and execs the login shell or the terminal command on the
terminal. No guest text reaches a command line, and the secret environment
never touches the terminal half's argv.

:class:`Parked` is that first connection. :func:`attach` is ``Parked`` plus a
terminal half on the operator's terminal; :func:`park` is ``Parked`` plus a
:class:`Handover`, whose command line is the terminal half of a terminal agw
does not hold (a tmux pane on the guest).
"""

from __future__ import annotations

import contextlib
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.execution import ssh_process
from agentworks.execution.errors import ExecutionIncomplete
from agentworks.execution.pump import Deadline, PipeSession, StopReason
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.terminal import emit_clear, guarded_terminal

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

PARKING_WAIT_SECONDS = 60
"""How long to wait for the parking connection to report ``parked``, before the terminal is touched.

Its own bound for connecting, authenticating and running the account's startup
files. The launcher's 30 second handover wait starts only after ``parked``.
"""

PARKING_SHUTDOWN_SECONDS = 2.0
"""How long to let the parked launcher clean up once the client has closed its end."""

HANDOVER_COLLECT_SECONDS = 35
"""How long :func:`park` waits for its pane to collect the request: the launcher's 30 second wait and a margin."""

_SSH_STATUS_CONNECTION_ENDED = 255


def socket_path(token: str) -> str:
    """Where the parked launcher listens on the guest, for a handover ``token``."""
    return f"/tmp/agw-handover-{token}/s"


class Parked:
    """The parking connection of a handover, from ``parked`` until the client lets it go.

    Entering spawns the connection and waits up to :data:`PARKING_WAIT_SECONDS`
    for ``parked``. When it does not come (a missing python3, a trust
    mismatch, a refusal, a refused connection, or a startup file that reads
    stdin and holds it forever), entering ends the connection and raises the
    typed error, before anything else happens.

    Leaving closes the parking stdin, which the launcher takes as the client
    leaving: one that is still waiting removes its socket directory and exits.
    That is the way out after an attach's terminal half and after an
    exception; :meth:`collect` is the way out for a handover the client
    cannot watch being collected.
    """

    def __init__(self, *, vm_name: str, wire: bytes, nonce: str, argv: Sequence[str], log: Path) -> None:
        self._vm_name = vm_name
        self._log = log
        # Off the operator's console: see ssh_process.spawn.
        self._session = PipeSession(
            argv,
            request=wire,
            nonce=nonce,
            hold_stdin_open=True,
            share_console=False,
            on_stdout=_ignore,
            on_stderr=_ignore,
        )
        self._ended = False

    def __enter__(self) -> Parked:
        session = self._session.__enter__()
        try:
            stop = session.pump(lambda: session.stdout.parked, budget=Deadline.after(PARKING_WAIT_SECONDS))
            if not session.stdout.parked:
                _raise_parking_failure(self._vm_name, session, stop, self._log)
        except BaseException:
            session.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            if not self._ended:
                self._ended = True
                try:
                    self._session.close_stdin()
                    self._session.pump(budget=Deadline.after(PARKING_SHUTDOWN_SECONDS))
                finally:
                    self._session.end()
        finally:
            self._session.__exit__(None, None, None)

    def collect(self, seconds: float) -> bool:
        """Wait up to ``seconds`` for the launcher's trailer, end the connection, and say whether it handed over.

        The parking stdin stays open meanwhile, since closing it would make a
        launcher still waiting for its terminal half give up. ``exit 0`` means
        a terminal half collected the request; ``refused handover`` (the
        launcher's own wait ran out) and no trailer at all mean nothing did.
        """
        self._ended = True
        try:
            stop = self._session.pump(budget=Deadline.after(seconds))
        finally:
            self._session.end()
        return stop is StopReason.DONE and self._session.stderr.closing == "exit 0"


@dataclass
class Handover:
    """A request parked for a terminal agw does not hold, such as a tmux pane on the guest.

    ``account`` is the account that parked it, which owns the socket
    directory. ``collected`` is set when :func:`park`'s block exits normally:
    whether the pane collected the request within
    :data:`HANDOVER_COLLECT_SECONDS`.
    """

    token: str
    account: str
    collected: bool = False

    def command(self, run_as: str | None = None) -> str:
        """The pane's command line: the handover form of stage 0, which collects the request and runs it.

        ``run_as`` is for a pane whose tmux server belongs to another account
        than the one that parked: it puts ``sudo -n -u <run_as>`` in front, so
        the pane connects as the account that parked, the only one that can
        reach the socket. Its only values are the token and an account name;
        the environment is in the parked request.
        """
        command = ssh_process.stage0_command(socket_path(self.token))
        if run_as is None:
            return command
        if run_as != self.account:
            raise ValueError(f"a handover parked by '{self.account}' is collected as '{self.account}', not '{run_as}'")
        return f"sudo -n -u {shlex.quote(run_as)} {command}"


def attach(
    *,
    vm_name: str,
    wire: bytes,
    nonce: str,
    parking_argv: Sequence[str],
    terminal_argv: Sequence[str],
    log: Path,
    clear_screen_on_exit: bool = False,
) -> int:
    """Park the request, run the terminal half under the guard, and return its exit status.

    ``wire`` is the parking connection's message and ``log`` is the client log
    its argv writes. Both argvs arrive built, before anything is spawned, so a
    missing trust record raises with nothing dispatched. A parking failure
    raises its typed error before the terminal is touched (see :class:`Parked`).
    """
    with Parked(vm_name=vm_name, wire=wire, nonce=nonce, argv=parking_argv, log=log):
        with guarded_terminal():
            code = ssh_process.run_terminal(terminal_argv)
        # After the guard, whose mode resets clearing does not cover, and
        # before the notice, so the notice lands on the cleared screen.
        if clear_screen_on_exit:
            emit_clear()
        # After the guard: its exit pass leaves the alternate screen, which
        # would wipe anything written before it closes (I6).
        if code == _SSH_STATUS_CONNECTION_ENDED:
            output.warn("connection ended or the command exited 255; terminal restored.")
            output.detail("Remote state is untouched. Re-run the same command to reattach.")
    return code


@contextlib.contextmanager
def park(
    *, vm_name: str, wire: bytes, nonce: str, argv: Sequence[str], log: Path, token: str, account: str
) -> Iterator[Handover]:
    """Park the request and yield its :class:`Handover`; run its command in the pane inside the block.

    Leaving the block normally waits up to :data:`HANDOVER_COLLECT_SECONDS`
    for the pane to collect the request and sets :attr:`Handover.collected`.
    Leaving it on an exception (the pane could not be created) closes the
    parking stdin at once, so the launcher removes its socket directory and
    exits. Either way the connection has ended once the block is left.
    """
    with Parked(vm_name=vm_name, wire=wire, nonce=nonce, argv=argv, log=log) as parked:
        handover = Handover(token, account)
        yield handover
        handover.collected = parked.collect(HANDOVER_COLLECT_SECONDS)


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
