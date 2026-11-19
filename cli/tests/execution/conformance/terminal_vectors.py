"""The terminal vectors, T1 and T2: a terminal command, and a pane's handover.

They join :mod:`vectors`' list, so the lanes they name run them. T1 attaches a
``TerminalCommand`` to the lane's stand-in terminal; T2 parks a login shell and
runs its handover command in a pane whose owner sets tmux's variables, directly
and through ``sudo -n -u`` as an agent's pane does.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from agentworks.execution.agw_launch import DEFAULT_TERM
from agentworks.execution.errors import RequestRefused
from agentworks.execution.requests import LoginShell, TerminalCommand
from agentworks.execution.results import Completed
from tests.execution.conformance.lanes import needs_sudo

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.execution.conformance.lanes import Lane


# -- T1: a terminal command --------------------------------------------------------------


def terminal_command_runs_its_argv_from_path_with_the_requested_environment(lane: Lane) -> None:
    secret = "sekrit-env-value-5e"
    command = TerminalCommand(("sh", "-c", 'echo "token=$TOKEN"; pwd'), cwd=lane.workdir, env={"TOKEN": secret})
    with lane.terminal() as terminal:
        code = lane.access.attach(command)

    assert code == 0
    assert terminal.stdout.splitlines()[:2] == [f"token={secret}".encode(), lane.workdir.encode()]
    assert not any(secret in argument for argument in terminal.argv)


def terminal_command_that_cannot_run_exits_with_the_shell_status(lane: Lane) -> None:
    for argv, status, code in [(("agw-no-such-command-5e",), 127, "exec ENOENT"), (("/dev/null",), 126, "exec EACCES")]:
        with lane.terminal() as terminal:
            assert lane.access.attach(TerminalCommand(argv, cwd=lane.workdir)) == status, argv

        assert f"agw: refused: {code}".encode() in terminal.stderr, argv


def terminal_command_whose_directory_is_missing_is_refused_before_the_terminal(lane: Lane) -> None:
    with lane.terminal() as terminal, pytest.raises(RequestRefused) as raised:
        lane.access.attach(TerminalCommand(("true",), cwd=f"{lane.workdir}/no-such-directory-agw"))

    assert raised.value.code == "cwd"
    assert terminal.calls == 0


# -- T2: a pane's handover ---------------------------------------------------------------

# A pane's environment value with line breaks, which the parked request carries whole.
PANE_LINES = "first 100%\r\nsecond\n"

# Typed into the pane's shell. Each probe prints a marker its own echoed line does not hold.
PANE_KEYS = (
    b'printf \'%s2:%s:%s\\n\' T "$TERM" "$TOKEN"\n'
    b'printf \'%sX:%s|%s|%s|%s|%s\\n\' T "$TMUX" "$TMUX_PANE" "$TERM_PROGRAM" "$TERM_PROGRAM_VERSION" "$SSH_TTY"\n'
    b'python3 -c \'import os; print("T" "L:" + repr(os.environb[b"LINES_VALUE"]))\'\n'
    b"case $- in *i*) echo I''NTERACTIVE ;; esac\n"
    b"exit 0\n"
)
PANE_TERM = "screen-256color"
# What tmux sets for a pane besides TERM, as a pane's owner would.
TMUX_VARIABLES = {
    "TMUX": "/tmp/tmux-1000/default,4242,0",
    "TMUX_PANE": "%7",
    "TERM_PROGRAM": "tmux",
    "TERM_PROGRAM_VERSION": "3.5a",
}


def pane_handover(lane: Lane, *, term: str | None, switch_user: bool = False) -> bytes:
    """Park a login shell, run its handover command on a fresh terminal, check it, and return what it showed."""
    secret = "sekrit-pane-value-5e"
    lane.quiet_interactive_startup()
    token = lane.handover_token()
    owner_env = TMUX_VARIABLES | ({} if term is None else {"TERM": term})

    with lane.access.park(LoginShell(cwd=lane.workdir, env={"TOKEN": secret, "LINES_VALUE": PANE_LINES})) as handover:
        command = handover.command(switch_user=switch_user)
        shown = lane.pane(command, owner_env=owner_env, keys=PANE_KEYS)

    assert shown.completion == Completed(0), shown
    assert handover.collected
    assert secret not in command  # the environment is in the parked request, not on the pane's command line
    assert f"T2:{term or DEFAULT_TERM}:{secret}".encode() in shown.stdout, shown.stdout
    assert b"TL:" + repr(PANE_LINES.encode()).encode() in shown.stdout, shown.stdout
    assert b"INTERACTIVE" in shown.stdout, shown.stdout
    assert lane.wait_gone(f"/tmp/agw-handover-{token}")
    return shown.stdout


def pane_takes_its_terminal_from_its_owner(lane: Lane) -> None:
    shown = pane_handover(lane, term=PANE_TERM)

    # tmux's variables arrive as tmux set them, and a tmux pty gets no SSH_TTY: it is no SSH terminal.
    keys = ("TMUX", "TMUX_PANE", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")
    expected = ("TX:" + "|".join(TMUX_VARIABLES[key] for key in keys) + "|").encode()
    assert re.search(re.escape(expected) + rb"\r?\n", shown), shown


def pane_without_term_gets_the_default(lane: Lane) -> None:
    pane_handover(lane, term=None)


# What a pane behind sudo shows of tmux's variables: sudo's env_reset passes TERM and drops the rest.
NO_TMUX_VARIABLES = re.compile(rb"TX:\|\|\|\|\r?\n")


def pane_through_sudo_takes_only_term(lane: Lane) -> None:
    """An agent's pane in the admin's tmux server switches to the parking account with ``sudo -n -u``."""
    needs_sudo(lane)
    shown = pane_handover(lane, term=PANE_TERM, switch_user=True)

    assert NO_TMUX_VARIABLES.search(shown), shown


def pane_through_sudo_with_an_empty_term_gets_the_default(lane: Lane) -> None:
    """A pane through sudo whose ``TERM`` is empty gets ``DEFAULT_TERM``.

    tmux gives every pane a ``TERM``, its ``default-terminal``, so a pane
    without one is not a case sudo meets; sudo's ``env_reset`` would set
    ``TERM=unknown`` for it. An empty ``default-terminal`` is: sudo keeps the
    empty ``TERM``, and the launcher stands ``DEFAULT_TERM`` in for it.
    """
    needs_sudo(lane)
    shown = pane_handover(lane, term="", switch_user=True)

    assert NO_TMUX_VARIABLES.search(shown), shown


ENTRIES: list[tuple[str, str, Callable[..., None]]] = [
    ("T1-terminal-command", "LSV", terminal_command_runs_its_argv_from_path_with_the_requested_environment),
    ("T1-terminal-command-cannot-run", "LSV", terminal_command_that_cannot_run_exits_with_the_shell_status),
    (
        "T1-terminal-command-missing-directory",
        "LSV",
        terminal_command_whose_directory_is_missing_is_refused_before_the_terminal,
    ),
    ("T2-pane-terminal-from-owner", "LSV", pane_takes_its_terminal_from_its_owner),
    ("T2-pane-default-term", "LSV", pane_without_term_gets_the_default),
    ("T2-pane-through-sudo", "LV", pane_through_sudo_takes_only_term),
    ("T2-pane-through-sudo-empty-term", "LV", pane_through_sudo_with_an_empty_term_gets_the_default),
]
