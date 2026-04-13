"""Tests for detached remote execution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentworks.remote_exec import DetachedResult, _is_running, _read_exit_code, run_detached


def _mock_target(
    *,
    pid_exists: bool = False,
    process_alive: bool = False,
    status_exists_after: int = 0,
    output_content: str = "hello world\n",
    exit_code: str = "0",
) -> MagicMock:
    """Build a mock ExecTarget with configurable behavior."""
    target = MagicMock()
    poll_count = 0

    def run_side_effect(cmd, *, check=True, timeout=None):
        nonlocal poll_count
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        result.returncode = 0

        if cmd.startswith("test -f") and ".pid" in cmd:
            result.returncode = 0 if pid_exists else 1
        elif cmd.startswith("test -f") and ".status" in cmd:
            poll_count += 1
            result.returncode = 0 if poll_count > status_exists_after else 1
        elif cmd.startswith("ps -p"):
            result.returncode = 0 if process_alive else 1
        elif cmd.startswith("tail -c"):
            result.stdout = output_content
        elif cmd.startswith("cat") and ".status" in cmd:
            result.stdout = exit_code
        elif cmd.startswith("cat") and ".out" in cmd:
            result.stdout = output_content
        elif cmd.startswith("rm -f") or cmd.startswith("nohup"):
            pass
        return result

    target.run.side_effect = run_side_effect
    target.run_as_root.side_effect = run_side_effect
    target.write_file = MagicMock()
    return target


def test_is_running_no_pid_file() -> None:
    target = MagicMock()
    target.run.return_value = MagicMock(returncode=1)
    assert _is_running(target, "/tmp/test.pid") is False


def test_is_running_dead_process() -> None:
    target = MagicMock()
    call_count = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        result.returncode = 0 if call_count == 1 else 1  # pid exists, but ps -p fails
        return result

    target.run.side_effect = run_side_effect
    assert _is_running(target, "/tmp/test.pid") is False


def test_read_exit_code() -> None:
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="0\n", returncode=0)
    assert _read_exit_code(target, "/tmp/test.status") == 0


def test_read_exit_code_nonzero() -> None:
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="42\n", returncode=0)
    assert _read_exit_code(target, "/tmp/test.status") == 42


def test_read_exit_code_missing() -> None:
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", returncode=1)
    assert _read_exit_code(target, "/tmp/test.status") == 1


@patch("agentworks.remote_exec.time")
def test_run_detached_fresh_start(mock_time: MagicMock) -> None:
    """New command starts, completes after one poll."""
    mock_time.monotonic.return_value = 0
    mock_time.sleep = MagicMock()

    target = _mock_target(
        pid_exists=False,
        status_exists_after=1,  # status file appears after 1 poll
        output_content="step 1 done\n",
        exit_code="0",
    )

    result = run_detached(
        target,
        "echo hello",
        base_path="/tmp/agentworks-test",
        poll_interval=0,
    )

    assert isinstance(result, DetachedResult)
    assert result.exit_code == 0
    assert "step 1 done" in result.output
    # Should have written the wrapper script
    target.write_file.assert_called_once()


@patch("agentworks.remote_exec.time")
def test_run_detached_resume(mock_time: MagicMock) -> None:
    """Existing process detected, resume polling."""
    mock_time.monotonic.return_value = 0
    mock_time.sleep = MagicMock()

    target = _mock_target(
        pid_exists=True,
        process_alive=True,
        status_exists_after=1,
        output_content="resumed output\n",
        exit_code="0",
    )

    result = run_detached(
        target,
        "echo hello",
        base_path="/tmp/agentworks-test",
        poll_interval=0,
    )

    assert result.exit_code == 0
    # Should NOT have written a wrapper script (resume, not fresh start)
    target.write_file.assert_not_called()


@patch("agentworks.remote_exec.time")
def test_run_detached_nonzero_exit(mock_time: MagicMock) -> None:
    mock_time.monotonic.return_value = 0
    mock_time.sleep = MagicMock()

    target = _mock_target(
        pid_exists=False,
        status_exists_after=1,
        output_content="error output\n",
        exit_code="1",
    )

    result = run_detached(
        target,
        "false",
        base_path="/tmp/agentworks-test",
        poll_interval=0,
    )

    assert result.exit_code == 1


@patch("agentworks.remote_exec.time")
def test_run_detached_as_root(mock_time: MagicMock) -> None:
    mock_time.monotonic.return_value = 0
    mock_time.sleep = MagicMock()

    target = _mock_target(
        pid_exists=False,
        status_exists_after=1,
        output_content="done\n",
        exit_code="0",
    )

    result = run_detached(
        target,
        "apt-get install -y foo",
        base_path="/tmp/agentworks-test",
        poll_interval=0,
        as_root=True,
    )

    assert result.exit_code == 0
    # The rm and nohup commands should go through run_as_root
    assert target.run_as_root.call_count > 0


@patch("agentworks.remote_exec.time")
def test_run_detached_disables_force_tty_for_nohup_launch(mock_time: MagicMock) -> None:
    """When the target's SSH has force_tty=True (Windows), the nohup launch
    must run on a target copy with force_tty=False. Otherwise SIGHUP from the
    closing SSH PTY kills the detached process before it can run.
    """
    from dataclasses import dataclass, field

    mock_time.monotonic.return_value = 0
    mock_time.sleep = MagicMock()

    @dataclass
    class _SSH:
        host: str = "vm"
        force_tty: bool = True

    @dataclass
    class _Target:
        ssh: _SSH
        run: MagicMock = field(default_factory=MagicMock)
        run_as_root: MagicMock = field(default_factory=MagicMock)
        write_file: MagicMock = field(default_factory=MagicMock)

    # Track the force_tty value on whichever target each command ran against.
    nohup_force_tty: list[bool] = []

    # Simulate a successful detached run: status file appears after first poll,
    # exit code 0, some output captured.
    status_exists_after = 1
    poll_count = 0

    def make_side_effect(owner: _Target) -> MagicMock:
        def side_effect(cmd: str, *, check: bool = True, timeout: int | None = None):
            nonlocal poll_count
            if "nohup" in cmd:
                nohup_force_tty.append(owner.ssh.force_tty)
            result = MagicMock()
            result.stdout = ""
            result.returncode = 0
            if cmd.startswith("test -f") and ".status" in cmd:
                poll_count += 1
                result.returncode = 0 if poll_count > status_exists_after else 1
            elif cmd.startswith("test -f") and ".pid" in cmd:
                result.returncode = 1  # No existing PID -> fresh start
            elif cmd.startswith("tail -c") or (cmd.startswith("cat") and ".out" in cmd):
                result.stdout = "done\n"
            elif cmd.startswith("cat") and ".status" in cmd:
                result.stdout = "0"
            return result

        return MagicMock(side_effect=side_effect)

    target = _Target(ssh=_SSH(force_tty=True))
    target.run = make_side_effect(target)
    target.run_as_root = make_side_effect(target)

    # Intercept replace() so the replaced copy gets its own mocks that record
    # calls made against it.
    import dataclasses as _dc

    real_replace = _dc.replace

    def tracked_replace(obj, **changes):  # type: ignore[no-untyped-def]
        result = real_replace(obj, **changes)
        if isinstance(result, _Target):
            result.run = make_side_effect(result)
            result.run_as_root = make_side_effect(result)
        return result

    with patch.object(_dc, "replace", side_effect=tracked_replace):
        result = run_detached(
            target,
            "tar cf /tmp/x.tar .",
            base_path="/tmp/agentworks-test",
            poll_interval=0,
            as_root=True,
        )

    # The nohup command must have run on a target where force_tty=False
    assert nohup_force_tty == [False]
    # Original target unchanged
    assert target.ssh.force_tty is True
    # And the overall run succeeded
    assert result.exit_code == 0


@patch("agentworks.remote_exec.time")
def test_run_detached_timeout_kills_and_returns_partial(mock_time: MagicMock) -> None:
    """When timeout is exceeded, the remote process is killed and partial
    output is returned with exit code 1."""
    # First two calls are setup (last_output_time, start_time), return 0.
    # Third call (first poll loop iteration) returns 100 to trigger timeout.
    call_count = 0

    def monotonic() -> int:
        nonlocal call_count
        call_count += 1
        return 0 if call_count <= 2 else 100

    mock_time.monotonic.side_effect = monotonic
    mock_time.sleep = MagicMock()

    killed_commands: list[str] = []

    target = MagicMock()

    def run_side_effect(cmd: str, *, check: bool = True, timeout: int | None = None):
        result = MagicMock()
        result.stdout = ""
        result.returncode = 0
        # Status file never exists (process never finishes on its own)
        if cmd.startswith("test -f") and ".status" in cmd:
            result.returncode = 1
        elif cmd.startswith("test -f") and ".pid" in cmd:
            result.returncode = 0  # PID file exists
        if "kill $(cat" in cmd:
            killed_commands.append(cmd)
        elif cmd.startswith("tail -c") or cmd.startswith("cat") and ".out" in cmd:
            result.stdout = "partial output from killed process\n"
        elif cmd.startswith("cat") and ".status" in cmd:
            # Status file doesn't exist, cat fails
            result.stdout = ""
            result.returncode = 1
        return result

    target.run.side_effect = run_side_effect
    target.run_as_root.side_effect = run_side_effect
    target.write_file = MagicMock()

    result = run_detached(
        target,
        "sleep 999",
        base_path="/tmp/agentworks-test",
        poll_interval=0,
        timeout=60,
    )

    # Kill command should have been issued
    assert len(killed_commands) == 1
    assert "kill $(cat" in killed_commands[0]
    # Exit code defaults to 1 when status file can't be read
    assert result.exit_code == 1
    # Partial output is still captured
    assert "partial output" in result.output
