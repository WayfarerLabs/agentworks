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
