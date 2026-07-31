"""Tests for the SetEnv-based env injection shape in sessions/tmux.create_session.

Pins the contract that env vars reach the pane via tmux's session-environment
flags (``tmux new-session -e KEY=VAL``) AND via the SSH layer's SetEnv
(materialized when ``run_command`` is called with ``env=``). See the
sshd-accept-env-wildcard ADR for the AcceptEnv-side rationale.
"""

from __future__ import annotations

import shlex

import pytest

from agentworks.sessions.tmux import (
    _pane_command,
    _tmux_env_flags,
    admin_socket_path,
    create_session,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_pane_command_returns_empty_for_no_command() -> None:
    """A login-shell-only session (no template command) gets an empty pane
    string; tmux falls back to its default-shell login behavior."""
    assert _pane_command("", shlex.quote("/tmp")) == ""


def test_pane_command_wraps_in_login_interactive_shell() -> None:
    out = _pane_command("claude", shlex.quote("/workspace"))
    assert out.startswith("$SHELL -lic ")
    assert "cd /workspace && exec claude" in out


def test_pane_command_owns_exec_no_double_wrap() -> None:
    """Regression: a previous pass had the command source prepend
    ``exec`` and ``_pane_command`` prepend it again, producing
    ``cd ... && exec exec claude``. The exec wrapping is owned by
    ``_pane_command`` only; if the caller hands us a command that already
    starts with ``exec``, we must NOT double it."""
    out = _pane_command("exec claude", shlex.quote("/workspace"))
    # Exactly one `exec` between `&&` and `claude`.
    inner_count = out.count(" exec ")
    assert inner_count == 1, f"expected one ' exec ' segment, got {inner_count}: {out!r}"


def test_tmux_env_flags_empty_input() -> None:
    assert _tmux_env_flags({}) == ""
    assert _tmux_env_flags(None) == ""


def test_tmux_env_flags_emits_per_pair() -> None:
    out = _tmux_env_flags({"A": "1", "B": "2"})
    # Leading space so it concatenates cleanly onto a tmux command string.
    assert out.startswith(" ")
    assert " -e A=1" in out
    assert " -e B=2" in out


def test_tmux_env_flags_quotes_values_with_spaces() -> None:
    out = _tmux_env_flags({"GREET": "hello world"})
    assert "-e 'GREET=hello world'" in out


def test_tmux_env_flags_round_trip_through_shlex_for_single_quotes() -> None:
    """The output is destined for the SSH remote shell, which parses it via
    shell rules; values containing single quotes must round-trip cleanly
    through ``shlex.split`` (same parser bash uses)."""
    out = _tmux_env_flags({"PATH": "/it's/here", "OK": "fine"})
    # Strip the leading space then split as the remote shell would.
    tokens = shlex.split(out.lstrip())
    # Expect two -e pairs.
    assert tokens.count("-e") == 2
    assert "PATH=/it's/here" in tokens
    assert "OK=fine" in tokens


def test_admin_socket_path_under_admin_socket_root() -> None:
    assert admin_socket_path("agentworks", "s1") == ("/run/agentworks/admin-tmux-sockets/agentworks/s1.sock")


# ---------------------------------------------------------------------------
# create_session: spy on run_command to confirm the wire shape
# ---------------------------------------------------------------------------


class _SpyResult:
    def __init__(self, ok: bool = True, stdout: str = "") -> None:
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stdout = stdout
        self.stderr = ""


class _SpyRunCommand:
    """Records (command, env) tuples for every run_command invocation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def __call__(
        self,
        command: str,
        *,
        check: bool = True,  # noqa: ARG002 - matches the RunCommand protocol
        env: dict[str, str] | None = None,
    ) -> _SpyResult:
        self.calls.append((command, env))
        # Default the test-friendly results: socket probe says no existing socket,
        # display-message returns a PID.
        if command.startswith("test -e "):
            return _SpyResult(ok=False)
        if "display-message" in command:
            return _SpyResult(ok=True, stdout="12345\n")
        return _SpyResult(ok=True)


class _SpyTarget:
    """``Transport`` stub that captures runs for the socket-root setup helpers."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, command: str, *, sudo: bool = False, check: bool = False) -> _SpyResult:  # noqa: ARG002
        self.calls.append(command)
        # ensure_admin_socket_root probes for an existing directory; reply
        # PROBE_FAILED so the helper takes the create path (records the mkdir
        # commands, but those aren't what this test pins).
        if "stat -c" in command:
            return _SpyResult(ok=True, stdout="PROBE_FAILED\n")
        return _SpyResult(ok=True)


@pytest.fixture
def spy_target(monkeypatch: pytest.MonkeyPatch) -> _SpyTarget:  # noqa: ARG001
    return _SpyTarget()


def test_admin_create_session_passes_env_to_run_command(
    spy_target: _SpyTarget,
) -> None:
    """Admin-mode create_session: env reaches the SSH layer via run_command's
    env kwarg (SetEnv on the wire) AND is embedded in the tmux new-session -e
    flags."""
    spy_run = _SpyRunCommand()
    env = {"AGENTWORKS_SESSION": "s1", "EDITOR": "nvim"}

    create_session(
        session_name="s1",
        workspace_path="/workspace",
        command="claude",
        linux_user="agentworks",
        run_command=spy_run,
        target=spy_target,
        admin_username="agentworks",
        is_admin=True,
        env=env,
    )

    new_session_calls = [c for c in spy_run.calls if "tmux -S" in c[0] and "new-session" in c[0]]
    assert len(new_session_calls) == 1
    cmd, passed_env = new_session_calls[0]
    # SetEnv side: env passed straight through to run_command.
    assert passed_env == env
    # tmux -e side: per-pair flags embedded in the command.
    assert " -e AGENTWORKS_SESSION=s1" in cmd
    assert " -e EDITOR=nvim" in cmd
    # Pane command wraps in login-interactive shell with cd && exec.
    assert "$SHELL -lic" in cmd
    assert "cd /workspace && exec claude" in cmd


def test_agent_create_session_uses_dedicated_socket_and_env(
    spy_target: _SpyTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent-mode create_session keeps the agent socket path AND threads env
    via the SSH layer + tmux -e flags."""
    monkeypatch.setattr(
        "agentworks.sessions.tmux.ensure_agent_socket_root",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agentworks.sessions.tmux.ensure_agent_socket_dir",
        lambda *a, **k: None,
    )

    spy_run = _SpyRunCommand()
    env = {"AGENTWORKS_SESSION": "s1"}

    sock, _pid = create_session(
        session_name="s1",
        workspace_path="/home/aw-claude/ws",
        command="claude",
        linux_user="aw-claude",
        run_command=spy_run,
        target=spy_target,
        admin_username="agentworks",
        is_admin=False,
        env=env,
    )

    # Socket path is under the agent socket root, per-session.
    assert sock == "/run/agentworks/agent-tmux-sockets/aw-claude/s1.sock"
    new_session_calls = [c for c in spy_run.calls if "tmux -S" in c[0] and "new-session" in c[0]]
    assert len(new_session_calls) == 1
    cmd, passed_env = new_session_calls[0]
    assert passed_env == env
    assert " -e AGENTWORKS_SESSION=s1" in cmd


def test_admin_create_session_uses_admin_socket(
    spy_target: _SpyTarget,
) -> None:
    """The Phase 3 SetEnv pivot moves admin sessions to per-session sockets
    so each session creates a fresh tmux server (no env leak between admin
    sessions on a shared default server)."""
    spy_run = _SpyRunCommand()

    sock, _pid = create_session(
        session_name="s1",
        workspace_path="/workspace",
        command="claude",
        linux_user="agentworks",
        run_command=spy_run,
        target=spy_target,
        admin_username="agentworks",
        is_admin=True,
    )

    assert sock == "/run/agentworks/admin-tmux-sockets/agentworks/s1.sock"
    new_session_calls = [c for c in spy_run.calls if "tmux -S" in c[0] and "new-session" in c[0]]
    assert len(new_session_calls) == 1
    cmd, _env = new_session_calls[0]
    assert sock in cmd


def test_create_session_with_no_env_omits_e_flags(
    spy_target: _SpyTarget,
) -> None:
    """No env / empty env: behavior is unchanged from pre-pivot (no -e flags,
    no SetEnv, plain pane command)."""
    spy_run = _SpyRunCommand()

    create_session(
        session_name="s1",
        workspace_path="/workspace",
        command="",
        linux_user="agentworks",
        run_command=spy_run,
        target=spy_target,
        admin_username="agentworks",
        is_admin=True,
        env=None,
    )

    new_session_calls = [c for c in spy_run.calls if "tmux -S" in c[0] and "new-session" in c[0]]
    cmd, passed_env = new_session_calls[0]
    assert passed_env is None
    assert " -e " not in cmd
    # No command + no env: no pane command, tmux uses default-shell.
    assert "$SHELL" not in cmd
