"""Per-transport contract tests for ``LimaTransport``."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import LimaTransport
from tests.transports.conftest import fail_completed as _fail_completed
from tests.transports.conftest import ok_completed as _ok_completed

# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


def test_run_invokes_limactl_shell_with_bash_lc() -> None:
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi")
        argv = mock_run.call_args[0][0]
        assert argv[:4] == ["limactl", "shell", "my-vm", "bash"]
        assert argv[4] == "-lc"
        assert argv[5] == "echo hi"


def test_run_env_injected_as_bash_assignment_prefix() -> None:
    """limactl shell doesn't carry env; we embed as scoped bash
    assignments at the head of the payload."""
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi", env={"FOO": "bar baz"})
        argv = mock_run.call_args[0][0]
        # Bash payload is index 5; should start with the env prefix.
        assert argv[5].startswith("FOO='bar baz' ")
        assert argv[5].endswith("echo hi")


def test_run_sudo_wraps_with_bash_c() -> None:
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("cmd1 && cmd2", sudo=True)
        argv = mock_run.call_args[0][0]
        assert "sudo -n bash -c 'cmd1 && cmd2'" in argv[5]


def test_run_check_true_raises_on_nonzero() -> None:
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.run") as mock_run:
        mock_run.return_value = _fail_completed(returncode=2, stderr="nope")
        with pytest.raises(SSHError, match="Lima command failed"):
            t.run("false")


def test_run_check_false_returns_result() -> None:
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.run") as mock_run:
        mock_run.return_value = _fail_completed(returncode=2)
        result = t.run("false", check=False)
        assert isinstance(result, SSHResult)
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# interactive()
# ---------------------------------------------------------------------------


def test_interactive_empty_command_opens_login_shell() -> None:
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.interactive("")
        assert mock_call.call_args[0][0] == ["limactl", "shell", "my-vm"]


def test_interactive_with_command_wraps_in_bash_lc() -> None:
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.interactive("tmux attach -t s1")
        argv = mock_call.call_args[0][0]
        assert argv == ["limactl", "shell", "my-vm", "bash", "-lc", "tmux attach -t s1"]


# ---------------------------------------------------------------------------
# copy_to / copy_from -- polymorphic surface improvement (R5 of the SDD)
# ---------------------------------------------------------------------------


def test_copy_to_invokes_limactl_copy() -> None:
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.copy_to("/local/foo", "/remote/bar")
        argv = mock_run.call_args[0][0]
        assert argv == ["limactl", "copy", "/local/foo", "my-vm:/remote/bar"]


def test_copy_from_invokes_limactl_copy_reverse() -> None:
    """``copy_from`` is the new polymorphic surface (R5 of the SDD):
    swaps src/dest order so backup.py becomes platform-agnostic."""
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.copy_from("/remote/bar", "/local/foo")
        argv = mock_run.call_args[0][0]
        assert argv == ["limactl", "copy", "my-vm:/remote/bar", "/local/foo"]


def test_copy_to_raises_on_failure() -> None:
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.run") as mock_run:
        mock_run.return_value = _fail_completed(stderr="denied")
        with pytest.raises(SSHError, match="limactl copy failed"):
            t.copy_to("/local", "/remote")


# ---------------------------------------------------------------------------
# call_streaming()
# ---------------------------------------------------------------------------


def test_call_streaming_invokes_limactl_shell() -> None:
    t = LimaTransport(vm_name="my-vm")
    with patch("agentworks.transports.lima.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.call_streaming("echo hi")
        argv = mock_call.call_args[0][0]
        assert argv[:4] == ["limactl", "shell", "my-vm", "bash"]
        assert argv[-1] == "echo hi"
