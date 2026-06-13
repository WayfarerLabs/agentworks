"""Tests for the env prelude placement in sessions/tmux.create_session.

These exercise the pane-command builder directly to pin the HLA's
"Prelude placement vs login shells" contract: the prelude lives OUTSIDE
the login-shell wrapper so the login shell inherits it via ``environ``.
"""

from __future__ import annotations

from agentworks.sessions.tmux import _build_pane_command


def test_no_command_no_env_produces_empty_pane_command() -> None:
    """The legacy no-command + no-env path falls through to tmux's
    default-shell. Behavior unchanged for operators with no env configured."""
    assert _build_pane_command(command="", q_path="/tmp", prelude="") == ""


def test_command_only_wraps_in_login_shell() -> None:
    """With a command but no env prelude: $SHELL -lic 'cd PATH && command'."""
    out = _build_pane_command(command="claude", q_path="/tmp/ws", prelude="")
    # The whole inner payload is shell-quoted.
    assert out.startswith("$SHELL -lic ")
    assert "cd /tmp/ws && claude" in out


def test_prelude_only_invokes_login_shell_so_prelude_can_land() -> None:
    """With env prelude but no command: still invoke a login shell so the
    prelude has a process to land in (tmux default-shell would skip it)."""
    out = _build_pane_command(command="", q_path="/tmp", prelude="export AGENTWORKS_SESSION=s1")
    assert out == "export AGENTWORKS_SESSION=s1 && $SHELL -l"


def test_prelude_and_command_compose_with_prelude_outside_login_shell() -> None:
    """The full shape: prelude && $SHELL -lic '...'. Prelude is OUTSIDE the
    login-shell wrapper so startup files inherit the vars via environ."""
    out = _build_pane_command(
        command="claude",
        q_path="/tmp/ws",
        prelude="export AGENTWORKS_SESSION=s1; export EDITOR=nvim",
    )
    assert out.startswith("export AGENTWORKS_SESSION=s1; export EDITOR=nvim && $SHELL -lic ")
    assert "cd /tmp/ws && claude" in out


def test_prelude_is_not_inside_login_shell_quote() -> None:
    """Defensive: an operator should not see their env exports appearing
    inside the login-shell's quoted payload (which would defeat the HLA's
    'outer shell' placement)."""
    out = _build_pane_command(
        command="claude",
        q_path="/tmp",
        prelude="export FOO=bar",
    )
    # split on $SHELL -lic; the prelude must appear in the prefix, not in
    # the quoted inner argument.
    prefix, _, inner = out.partition("$SHELL -lic ")
    assert "export FOO=bar" in prefix
    assert "export FOO=bar" not in inner
