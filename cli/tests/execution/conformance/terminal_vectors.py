"""Every attach and park vector: the handover, T1 (a terminal command) and T2 (a pane's handover).

They join :mod:`vectors`' list, so the lanes they name run them. The handover
vectors attach a ``LoginShell`` to the lane's stand-in terminal: its success,
refusals, waits and what it leaves behind, and on L the account startup files
that would hold or taint it. T1 attaches a ``TerminalCommand``; T2 parks a login
shell and runs its handover command in a pane whose owner sets tmux's
variables, directly and through ``sudo -n -u`` as an agent's pane does.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import threading
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import ssh_process
from agentworks.execution.agw_launch import DEFAULT_TERM
from agentworks.execution.errors import ExecutionIncomplete, RequestRefused
from agentworks.execution.requests import LoginShell, TerminalCommand
from agentworks.execution.results import Completed, Unknown, UnknownReason
from tests.execution.conformance.lanes import local, needs_startup_files, needs_sudo, startup_file

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.execution.conformance.lanes import Lane, TerminalHalf


# -- Handover ---------------------------------------------------------------------------


def handover_hands_the_terminal_a_login_shell(lane: Lane) -> None:
    token = lane.handover_token()
    with lane.terminal(b"pwd\nexit 7\n") as terminal:
        code = lane.access.attach(LoginShell(cwd=lane.workdir))

    assert code == 7
    assert terminal.stdout.splitlines()[-1] == lane.workdir.encode()
    assert lane.wait_gone(f"/tmp/agw-handover-{token}")


def handover_refuses_before_the_terminal_is_touched(lane: Lane) -> None:
    lane.handover_token()
    with lane.terminal() as terminal, pytest.raises(RequestRefused) as raised:
        lane.access.attach(LoginShell(cwd=f"{lane.workdir}/no-such-directory-agw"))

    assert raised.value.code == "cwd"
    assert terminal.calls == 0


def handover_never_reuses_an_existing_name(lane: Lane) -> None:
    token = lane.handover_token()
    planted = f"/tmp/agw-handover-{token}"
    lane.mkdir(planted)
    try:
        with lane.terminal() as terminal, pytest.raises(RequestRefused) as raised:
            lane.access.attach(LoginShell())

        assert raised.value.code == "handover"
        assert terminal.calls == 0
        assert lane.exists(planted)  # the launcher removed nothing it did not create
    finally:
        lane.remove_dir(planted)


def handover_whose_second_connection_never_arrives_leaves_nothing(lane: Lane) -> None:
    token = lane.handover_token()
    with lane.terminal(behavior=lambda argv, half: 0) as terminal:
        assert lane.access.attach(LoginShell()) == 0

    assert terminal.calls == 1
    assert lane.wait_gone(f"/tmp/agw-handover-{token}")


def handover_gives_up_after_its_wait(lane: Lane) -> None:
    lane.shorten_handover_wait(1)
    directory = f"/tmp/agw-handover-{lane.handover_token()}"
    gone_while_parked: list[bool] = []

    def never_connects(argv: list[str], half: object) -> int:
        # The client holds the parking connection's stdin open until this returns, so the launcher's
        # end-of-input cleanup cannot fire yet and only its own wait can remove the directory. Polling
        # for less than the unshortened 30 second wait proves the shortened one ended it.
        gone_while_parked.append(lane.wait_gone(directory, seconds=20))
        return 0

    with lane.terminal(behavior=never_connects):
        assert lane.access.attach(LoginShell()) == 0

    assert gone_while_parked == [True]


def parking_that_never_reports_is_bounded_and_leaves_the_terminal_alone(lane: Lane) -> None:
    account = local(lane)
    needs_startup_files(account)
    account.startup(startup_file(account), "cat >/dev/null\n")  # holds the request and the connection open
    account.shorten_parking_wait(2)
    caught: list[BaseException] = []

    def attach() -> None:
        try:
            account.access.attach(LoginShell(cwd=account.workdir))
        except BaseException as error:  # noqa: BLE001 - the thread hands every outcome to the vector
            caught.append(error)

    # A regression in the bound would hang the attach forever, so the vector bounds itself.
    with account.terminal() as terminal:
        thread = threading.Thread(target=attach, daemon=True)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "the parking wait did not end"

    assert len(caught) == 1
    assert isinstance(caught[0], ExecutionIncomplete)
    assert caught[0].result.completion == Unknown(UnknownReason.DEADLINE)
    assert terminal.calls == 0


# What a startup file the attach's terminal half runs exports; none of it is the attach terminal's.
PLANTED_TERMINAL_VARIABLES = "export TMUX=/tmp/agw-planted,1,0 TMUX_PANE=%9 TERM_PROGRAM=planted\n"
ATTACH_KEYS = b'printf \'%sA:%s|%s|%s|%s\\n\' T "$TMUX" "$TMUX_PANE" "$TERM_PROGRAM" "$SSH_TTY"\nexit 0\n'


def attach_takes_no_pane_variables_from_its_environment(lane: Lane) -> None:
    account = local(lane)
    needs_startup_files(account)
    account.startup(startup_file(account), PLANTED_TERMINAL_VARIABLES)  # read by the shell that runs stage 0
    account.quiet_interactive_startup()

    def on_a_pty(argv: list[str], half: TerminalHalf) -> int:
        # The terminal half on script's pty, as ssh -t gives it one, so the launcher takes its tty path.
        completed = subprocess.run(
            ["script", "-q", "-e", "-c", shlex.join(argv), "/dev/null"],
            input=ATTACH_KEYS,
            capture_output=True,
            timeout=60,
            env=ssh_process.child_environment(),
            check=False,
        )
        half.stdout = completed.stdout
        return completed.returncode

    with account.terminal(behavior=on_a_pty) as terminal:
        assert account.access.attach(LoginShell(cwd=account.workdir)) == 0

    marker = re.search(rb"TA:([^|\r\n]*)\|([^|\r\n]*)\|([^|\r\n]*)\|([^\r\n]*)", terminal.stdout)
    assert marker is not None, terminal.stdout
    tmux, pane, program, ssh_tty = marker.groups()
    assert (tmux, pane, program) == (b"", b"", b"")
    assert ssh_tty.startswith(b"/dev/")  # an attach's terminal is an SSH terminal


def handover_shell_starts_with_clean_dispositions(lane: Lane) -> None:
    account = local(lane)
    needs_startup_files(account)
    account.startup(startup_file(account), "trap '' HUP INT TERM USR1 USR2\n")
    home = account.make_dir()

    with account.terminal(b"grep -E 'SigIgn|SigBlk' /proc/self/status\nexit 0\n") as terminal:
        code = account.access.attach(LoginShell(cwd=account.workdir, env={"HOME": home}))

    assert code == 0
    assert terminal.stdout.decode().splitlines() == ["SigBlk:\t0000000000000000", "SigIgn:\t0000000000000000"]


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
    ("handover-success", "LSV", handover_hands_the_terminal_a_login_shell),
    ("handover-refused-before-terminal", "LSV", handover_refuses_before_the_terminal_is_touched),
    ("handover-existing-name", "LSV", handover_never_reuses_an_existing_name),
    ("handover-second-connection-never-arrives", "LSV", handover_whose_second_connection_never_arrives_leaves_nothing),
    ("handover-wait-expires", "LSV", handover_gives_up_after_its_wait),
    ("parking-never-reports", "L", parking_that_never_reports_is_bounded_and_leaves_the_terminal_alone),
    ("handover-clean-dispositions", "L", handover_shell_starts_with_clean_dispositions),
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
    ("T2-attach-takes-no-pane-variables", "L", attach_takes_no_pane_variables_from_its_environment),
]
