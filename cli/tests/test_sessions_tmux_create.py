"""Tests for the wire shape of sessions/tmux.create_session.

Two contracts live here:

- The SetEnv-based env injection shape: env vars reach the pane via tmux's
  session-environment flags (``tmux new-session -e KEY=VAL``) AND via the
  SSH layer's SetEnv (materialized when ``run_command`` is called with
  ``env=``). See the sshd-accept-env-wildcard ADR for the AcceptEnv-side
  rationale.
- The dead-workload capture path: remain-on-exit chained into the create,
  the liveness probes around the grant machinery, and the capture-and-raise
  (or could-not-verify) errors for a workload that dies instantly.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from agentworks.errors import StateError
from agentworks.sessions.tmux import (
    _pane_command,
    _tmux_env_flags,
    _trim_captured_output,
    admin_socket_path,
    capture_tmux_server_fingerprint,
    create_session,
    parse_process_start_ticks,
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


def test_process_start_ticks_parser_uses_the_final_comm_delimiter() -> None:
    fields_after_comm = ["S", *[str(value) for value in range(4, 23)]]
    fields_after_comm[-1] = "98765"
    stat = f"42 (tmux: server) with ) chars) {' '.join(fields_after_comm)}"
    assert parse_process_start_ticks(stat) == 98765


class _FingerprintTarget:
    def __init__(
        self,
        *,
        second_pid: int = 42,
        second_ticks: int = 98765,
        boot_id: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        boot_returncode: int = 0,
    ) -> None:
        self.second_pid = second_pid
        self.second_ticks = second_ticks
        self.boot_id = boot_id
        self.boot_returncode = boot_returncode
        self.pid_reads = 0
        self.stat_reads = 0

    def run(self, command: str, **kwargs: object) -> _SpyResult:
        if "display-message" in command:
            self.pid_reads += 1
            pid = 42 if self.pid_reads == 1 else self.second_pid
            return _SpyResult(stdout=f"{pid}\n")
        if "/proc/42/stat" in command:
            self.stat_reads += 1
            ticks = 98765 if self.stat_reads == 1 else self.second_ticks
            fields_after_comm = ["S", *[str(value) for value in range(4, 23)]]
            fields_after_comm[-1] = str(ticks)
            return _SpyResult(stdout=f"42 (tmux: server) {' '.join(fields_after_comm)}\n")
        if "boot_id" in command:
            return _SpyResult(stdout=f"{self.boot_id}\n", returncode=self.boot_returncode)
        raise AssertionError(command)


def test_fingerprint_capture_requires_two_matching_pid_and_stat_reads() -> None:
    from agentworks.sessions.tmux import ProbeStatus

    stable = capture_tmux_server_fingerprint(target=_FingerprintTarget(), socket_path="/run/s1.sock")  # type: ignore[arg-type]
    assert stable.status is ProbeStatus.PRESENT
    assert stable.fingerprint is not None
    assert (stable.fingerprint.pid, stable.fingerprint.boot_id, stable.fingerprint.start_ticks) == (
        42,
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        98765,
    )
    changed_pid = capture_tmux_server_fingerprint(
        target=_FingerprintTarget(second_pid=43),  # type: ignore[arg-type]
        socket_path="/run/s1.sock",
    )
    changed_ticks = capture_tmux_server_fingerprint(
        target=_FingerprintTarget(second_ticks=98766),  # type: ignore[arg-type]
        socket_path="/run/s1.sock",
    )
    assert changed_pid.status is ProbeStatus.UNKNOWN
    assert changed_ticks.status is ProbeStatus.UNKNOWN


@pytest.mark.parametrize(
    "target",
    [
        _FingerprintTarget(boot_id="not-a-uuid"),
        _FingerprintTarget(boot_returncode=1),
    ],
)
def test_fingerprint_capture_fails_closed_for_untrusted_boot_identity(target: _FingerprintTarget) -> None:
    from agentworks.sessions.tmux import ProbeStatus

    result = capture_tmux_server_fingerprint(target=target, socket_path="/run/s1.sock")  # type: ignore[arg-type]
    assert result.status is ProbeStatus.UNKNOWN
    assert result.fingerprint is None


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_tmux_presence_classifier_distinguishes_absence_from_permission_failure() -> None:
    from agentworks.sessions.tmux import ProbeStatus, _test_presence_from_result, _tmux_presence_from_result

    with tempfile.TemporaryDirectory(prefix="agw-tmux-", dir="/tmp") as temporary_dir:
        socket_dir = Path(temporary_dir)
        missing_server = subprocess.run(
            ["tmux", "-S", str(socket_dir / "never-started.sock"), "list-sessions"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert _tmux_presence_from_result(missing_server, missing_target_is_absent=False) is ProbeStatus.ABSENT

        transition = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=1,
            stdout="",
            stderr="server exited unexpectedly\n",
        )
        assert _tmux_presence_from_result(transition, missing_target_is_absent=False) is ProbeStatus.UNKNOWN

        socket_path = socket_dir / "server.sock"
        subprocess.run(
            ["tmux", "-S", str(socket_path), "new-session", "-d", "-s", "probe", "sleep 30"],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            missing_target = subprocess.run(
                ["tmux", "-S", str(socket_path), "has-session", "-t", "=missing"],
                check=False,
                capture_output=True,
                text=True,
            )
            assert _tmux_presence_from_result(missing_target, missing_target_is_absent=True) is ProbeStatus.ABSENT
            assert _tmux_presence_from_result(missing_target, missing_target_is_absent=False) is ProbeStatus.UNKNOWN

            socket_dir.chmod(0)
            permission_denied = subprocess.run(
                ["tmux", "-S", str(socket_path), "list-sessions"],
                check=False,
                capture_output=True,
                text=True,
            )
            assert _tmux_presence_from_result(permission_denied, missing_target_is_absent=False) is ProbeStatus.UNKNOWN
            assert _test_presence_from_result(permission_denied) is ProbeStatus.ABSENT
        finally:
            socket_dir.chmod(0o700)
            subprocess.run(
                ["tmux", "-S", str(socket_path), "kill-server"],
                check=False,
                capture_output=True,
                text=True,
            )


def test_stale_socket_cleanup_removes_only_a_canonically_absent_server() -> None:
    from agentworks.sessions.tmux import _cleanup_stale_sockets_under

    class _CleanupTarget:
        def __init__(self, diagnostic: str) -> None:
            self.diagnostic = diagnostic
            self.commands: list[str] = []

        def run(self, command: str, **kwargs: object) -> _SpyResult:
            self.commands.append(command)
            if command.startswith("find "):
                return _SpyResult(stdout="/run/agentworks/s.sock\n")
            if "list-sessions" in command:
                result = _SpyResult(returncode=1)
                result.stderr = self.diagnostic
                return result
            return _SpyResult()

    unknown = _CleanupTarget("error connecting to /run/agentworks/s.sock (Permission denied)")
    assert _cleanup_stale_sockets_under(unknown, "/run/agentworks") == 0  # type: ignore[arg-type]
    assert not any(command.startswith("rm -f ") for command in unknown.commands)

    absent = _CleanupTarget("no server running on /run/agentworks/s.sock")
    assert _cleanup_stale_sockets_under(absent, "/run/agentworks") == 1  # type: ignore[arg-type]
    assert sum(command.startswith("rm -f ") for command in absent.commands) == 1


def test_post_teardown_probe_retries_one_indeterminate_transition() -> None:
    from agentworks.sessions.tmux import ProbeStatus, probe_tmux_server_after_teardown

    diagnostics = iter(("server exited unexpectedly", "no server running on /run/agentworks/s.sock"))
    commands: list[str] = []

    def run(command: str, **kwargs: object) -> _SpyResult:
        commands.append(command)
        result = _SpyResult(returncode=1)
        result.stderr = next(diagnostics)
        return result

    status = probe_tmux_server_after_teardown(run_command=run, socket_path="/run/agentworks/s.sock")

    assert status is ProbeStatus.ABSENT
    assert len(commands) == 2


# ---------------------------------------------------------------------------
# create_session: spy on run_command to confirm the wire shape
# ---------------------------------------------------------------------------


class _SpyResult:
    def __init__(self, ok: bool = True, stdout: str = "", *, returncode: int | None = None) -> None:
        self.returncode = (0 if ok else 1) if returncode is None else returncode
        self.ok = self.returncode == 0
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


@pytest.fixture
def stub_agent_socket_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the sudo socket-root/dir setup so agent-mode create_session can
    run against the spies. Referenced explicitly by the tests that drive
    agent mode; admin-mode tests don't need it."""
    monkeypatch.setattr("agentworks.sessions.tmux.ensure_agent_socket_root", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.sessions.tmux.ensure_agent_socket_dir", lambda *a, **k: None)


def test_existing_socket_is_untouched_when_server_probe_is_indeterminate(
    spy_target: _SpyTarget,
) -> None:
    calls: list[str] = []

    def run(command: str, **kwargs: object) -> _SpyResult:
        calls.append(command)
        if command.startswith("test -e "):
            return _SpyResult()
        if "list-sessions" in command:
            return _SpyResult(returncode=255)
        raise AssertionError(command)

    with pytest.raises(StateError):
        create_session(
            session_name="s1",
            workspace_path="/workspace",
            command="claude",
            linux_user="agentworks",
            run_command=run,
            target=spy_target,
            admin_username="agentworks",
            is_admin=True,
        )

    assert not any(command.startswith("rm -f ") or "new-session" in command for command in calls)


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
    stub_agent_socket_setup: None,
) -> None:
    """Agent-mode create_session keeps the agent socket path AND threads env
    via the SSH layer + tmux -e flags."""
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


# ---------------------------------------------------------------------------
# Dead-workload capture: a session command that dies instantly must surface
# its own output through a typed error instead of the grant machinery's
# opaque SSH failure against a dead socket.
# ---------------------------------------------------------------------------

# Distinctive clap-style startup failure (the real 2026-08-03 operator
# incident: `codex -a on-failure`, rejected in milliseconds).
_CLAP_ERROR = (
    "error: invalid value 'on-failure' for '--ask-for-approval <APPROVAL_POLICY>'\n"
    "\n"
    "For more information, try '--help'.\n"
)


class _ScriptedRunCommand:
    """Spy run_command whose liveness probes answer from a script.

    ``probe_answers`` feeds the combined ``#{pane_dead} #{pane_dead_status}``
    probe one raw stdout string per invocation (``None`` scripts a probe-level
    transport failure). Everything else mirrors ``_SpyRunCommand``: the socket
    probe reports no existing socket, ``capture-pane`` returns the seeded
    scrollback (main screen vs ``-a`` alternate screen), and the PID
    display-message returns a PID.
    """

    def __init__(
        self,
        probe_answers: list[str | None],
        capture_stdout: str = "",
        alt_capture_stdout: str = "",
    ) -> None:
        self.calls: list[str] = []
        self._probe_answers = list(probe_answers)
        self._capture_stdout = capture_stdout
        self._alt_capture_stdout = alt_capture_stdout

    def __call__(
        self,
        command: str,
        *,
        check: bool = True,  # noqa: ARG002 - matches the RunCommand protocol
        env: dict[str, str] | None = None,  # noqa: ARG002
    ) -> _SpyResult:
        self.calls.append(command)
        if command.startswith("test -e "):
            return _SpyResult(ok=False)
        if "#{pane_dead}" in command:
            answer = self._probe_answers.pop(0)
            if answer is None:
                return _SpyResult(ok=False)
            return _SpyResult(ok=True, stdout=answer + "\n")
        if "capture-pane" in command:
            if " -a" in command:
                return _SpyResult(ok=True, stdout=self._alt_capture_stdout)
            return _SpyResult(ok=True, stdout=self._capture_stdout)
        if "display-message" in command:
            return _SpyResult(ok=True, stdout="12345\n")
        return _SpyResult(ok=True)


def _agent_create(spy_run: _ScriptedRunCommand, spy_target: _SpyTarget) -> tuple[str, int | None]:
    """Drive an agent-mode create_session against the scripted spy."""
    return create_session(
        session_name="s1",
        workspace_path="/home/aw-claude/ws",
        command="codex -a on-failure",
        linux_user="aw-claude",
        run_command=spy_run,
        target=spy_target,
        admin_username="agentworks",
        is_admin=False,
    )


def test_dead_pane_at_first_check_captures_and_skips_grant(
    spy_target: _SpyTarget,
    stub_agent_socket_setup: None,
) -> None:
    """A workload dead at the first liveness check: its output and exit
    status are captured into the typed error, the server is killed, and the
    grant machinery (chmod + server-access) is NEVER invoked against the
    dead socket."""
    spy_run = _ScriptedRunCommand(probe_answers=["1 2"], capture_stdout=_CLAP_ERROR)

    with pytest.raises(StateError) as excinfo:
        _agent_create(spy_run, spy_target)

    msg = str(excinfo.value)
    assert "exited immediately after launch (status 2)" in msg
    assert "error: invalid value 'on-failure' for '--ask-for-approval <APPROVAL_POLICY>'" in msg
    # Capture ran before the (best-effort) server kill, joining wrapped lines.
    capture_idx = next(i for i, c in enumerate(spy_run.calls) if "capture-pane" in c)
    kill_idx = next(i for i, c in enumerate(spy_run.calls) if "kill-server" in c)
    assert capture_idx < kill_idx
    assert " -J " in spy_run.calls[capture_idx]
    # The grant machinery never touched the dead socket.
    assert not any("chmod g+rwx" in c for c in spy_run.calls)
    assert not any("server-access" in c for c in spy_run.calls)


def test_alive_pane_remain_on_exit_lifecycle(
    spy_target: _SpyTarget,
    stub_agent_socket_setup: None,
) -> None:
    """Healthy path: remain-on-exit is chained into the SAME tmux invocation
    as new-session, the PID is fetched while remain-on-exit still holds the
    server up, the unset lands after the grant machinery, the final re-check
    is the last tmux call, no capture happens, and the return value is
    unchanged."""
    spy_run = _ScriptedRunCommand(probe_answers=["0 ", "0 "])

    sock, pid = _agent_create(spy_run, spy_target)

    assert sock == "/run/agentworks/agent-tmux-sockets/aw-claude/s1.sock"
    assert pid == 12345
    new_session_calls = [c for c in spy_run.calls if "new-session" in c]
    assert len(new_session_calls) == 1
    # Chained into the same client invocation, not a separate round-trip.
    assert new_session_calls[0].endswith("\\; set-option -t s1 remain-on-exit on")
    grant_idx = next(i for i, c in enumerate(spy_run.calls) if "server-access" in c)
    pid_idx = next(i for i, c in enumerate(spy_run.calls) if "#{pid}" in c)
    unset_idx = next(i for i, c in enumerate(spy_run.calls) if "set-option -u" in c and "remain-on-exit" in c)
    # grants -> PID fetch (server still guaranteed up) -> unset -> re-check.
    assert grant_idx < pid_idx < unset_idx, "PID must be fetched after the grant, before the unset"
    recheck_idx = max(i for i, c in enumerate(spy_run.calls) if "#{pane_dead}" in c)
    assert recheck_idx > unset_idx, "the final liveness re-check must follow the unset"
    assert recheck_idx == len(spy_run.calls) - 1, "the re-check must be the last call before return"
    assert not any("capture-pane" in c for c in spy_run.calls)
    assert not any("kill-server" in c for c in spy_run.calls)


def test_dead_pane_at_post_unset_recheck_captures_and_raises(
    spy_target: _SpyTarget,
    stub_agent_socket_setup: None,
) -> None:
    """A workload that dies between the first check and the unset leaves a
    dead-but-persisted pane; the re-check converts it into the same
    capture-and-raise path instead of a zombie session."""
    spy_run = _ScriptedRunCommand(probe_answers=["0 ", "1 127"], capture_stdout=_CLAP_ERROR)

    with pytest.raises(StateError) as excinfo:
        _agent_create(spy_run, spy_target)

    assert "error: invalid value 'on-failure'" in str(excinfo.value)
    assert "(status 127)" in str(excinfo.value)
    # The grant machinery DID run (the pane was alive at the first check)...
    assert any("server-access" in c for c in spy_run.calls)
    # ...and the kill fired after the unset's re-check found the dead pane.
    unset_idx = next(i for i, c in enumerate(spy_run.calls) if "set-option -u" in c and "remain-on-exit" in c)
    kill_idx = next(i for i, c in enumerate(spy_run.calls) if "kill-server" in c)
    assert unset_idx < kill_idx


def test_probe_transport_failure_raises_could_not_verify_without_kill(
    spy_target: _SpyTarget,
    stub_agent_socket_setup: None,
) -> None:
    """A probe that fails twice is a transport failure, NOT proof of death:
    the create raises the honest could-not-verify error, does not kill the
    (possibly healthy) server, does not capture-diagnose, and never runs the
    grant machinery. Unreachable-is-not-dead, the stop_session precedent."""
    spy_run = _ScriptedRunCommand(probe_answers=[None, None])

    with pytest.raises(StateError, match="could not verify.*may still be running") as excinfo:
        _agent_create(spy_run, spy_target)

    assert "exited immediately" not in str(excinfo.value)
    assert not any("kill-server" in c for c in spy_run.calls)
    assert not any("capture-pane" in c for c in spy_run.calls)
    assert not any("server-access" in c for c in spy_run.calls)
    # Both attempts of the retried probe were made.
    assert sum(1 for c in spy_run.calls if "#{pane_dead}" in c) == 2


def test_probe_transient_blip_retries_and_succeeds(
    spy_target: _SpyTarget,
    stub_agent_socket_setup: None,
) -> None:
    """One failed probe attempt followed by a clean 'alive' answer must not
    condemn the create: the retry absorbs a single transport blip."""
    spy_run = _ScriptedRunCommand(probe_answers=[None, "0 ", "0 "])

    sock, pid = _agent_create(spy_run, spy_target)

    assert sock == "/run/agentworks/agent-tmux-sockets/aw-claude/s1.sock"
    assert pid == 12345
    assert not any("kill-server" in c for c in spy_run.calls)


def test_dead_pane_no_main_output_falls_back_to_alternate_screen(
    spy_target: _SpyTarget,
    stub_agent_socket_setup: None,
) -> None:
    """A TUI that crashed after entering the alternate screen leaves its
    message there; when the main-screen capture is empty, one best-effort
    ``-a`` capture recovers it."""
    spy_run = _ScriptedRunCommand(
        probe_answers=["1 1"],
        capture_stdout="",
        alt_capture_stdout="panic: terminal too small\n",
    )

    with pytest.raises(StateError, match="panic: terminal too small"):
        _agent_create(spy_run, spy_target)

    alt_calls = [c for c in spy_run.calls if "capture-pane" in c and " -a" in c]
    assert len(alt_calls) == 1


def test_dead_pane_with_no_output_at_all_says_so(
    spy_target: _SpyTarget,
    stub_agent_socket_setup: None,
) -> None:
    """Both captures empty: the error still names the exit status and says
    honestly that no output was produced."""
    spy_run = _ScriptedRunCommand(probe_answers=["1 0"])

    with pytest.raises(StateError, match=r"exited immediately after launch \(status 0\) and produced no output"):
        _agent_create(spy_run, spy_target)


# ---------------------------------------------------------------------------
# _trim_captured_output
# ---------------------------------------------------------------------------


def test_trim_captured_output_strips_blanks_and_caps() -> None:
    """A full-pane capture is mostly trailing blank lines; the trim keeps
    only the last 15 nonempty lines with trailing whitespace stripped."""
    noise = "\n".join(f"line {i}" for i in range(40))
    text = f"{noise}\n\nfinal error   \n" + "\n" * 30
    out = _trim_captured_output(text)
    lines = out.splitlines()
    assert len(lines) == 15
    assert lines[-1] == "final error"
    # 41 nonempty lines in; the last 15 start at "line 26".
    assert lines[0] == "line 26"


def test_trim_captured_output_caps_line_length() -> None:
    """A -J-joined single-line usage dump must not blow up the message: each
    line is capped to the per-line character budget."""
    out = _trim_captured_output("x" * 2000 + "\nshort")
    lines = out.splitlines()
    assert lines[0] == "x" * 500
    assert lines[1] == "short"


def test_trim_captured_output_empty_input() -> None:
    assert _trim_captured_output("") == ""
    assert _trim_captured_output("\n\n   \n") == ""
