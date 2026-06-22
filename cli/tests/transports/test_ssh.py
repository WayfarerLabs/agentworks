"""Per-transport contract tests for ``SSHTransport``.

Mocks ``subprocess.run`` / ``subprocess.call`` to inspect argv and
verify behavior. Lifts the patterns from ``tests/test_exec_target.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import SSHTransport

if TYPE_CHECKING:
    from pathlib import Path


def _ok_completed(stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a MagicMock that mimics ``subprocess.CompletedProcess`` for ok exit."""
    cp = MagicMock()
    cp.returncode = 0
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def _fail_completed(returncode: int = 1, stderr: str = "boom") -> MagicMock:
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = ""
    cp.stderr = stderr
    return cp


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


def test_run_builds_ssh_argv_with_user_host() -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi")
        argv = mock_run.call_args[0][0]
        assert argv[0] == "ssh"
        assert "BatchMode=yes" in argv
        assert "agentworks@vm1" in argv
        assert argv[-1] == "echo hi"


def test_run_sudo_wraps_with_bash_c() -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("cmd1 && cmd2", sudo=True)
        argv = mock_run.call_args[0][0]
        assert argv[-1] == "sudo -n bash -c 'cmd1 && cmd2'"


def test_run_login_shell_wraps_with_dollar_shell_lc() -> None:
    t = SSHTransport(host="vm1", user="agentworks", login_shell=True)
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi")
        argv = mock_run.call_args[0][0]
        assert argv[-1] == "$SHELL -lc 'echo hi'"


def test_run_env_coalesces_into_one_set_env_arg() -> None:
    """``-o SetEnv=`` is emitted once with all pairs coalesced (ssh_config(5)
    takes only the first SetEnv occurrence, so a per-pair option drops
    later pairs silently)."""
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi", env={"A": "1", "B": "two words"})
        argv = mock_run.call_args[0][0]
        set_env_args = [a for a in argv if a.startswith("SetEnv=")]
        assert len(set_env_args) == 1
        assert 'A="1"' in set_env_args[0]
        assert 'B="two words"' in set_env_args[0]


def test_run_force_tty_inserts_tt_flag() -> None:
    t = SSHTransport(host="vm1", user="agentworks", force_tty=True)
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi")
        argv = mock_run.call_args[0][0]
        assert "-tt" in argv


def test_run_tty_override_suppresses_force_tty() -> None:
    """Per-call ``tty=False`` wins over constructor ``force_tty=True``."""
    t = SSHTransport(host="vm1", user="agentworks", force_tty=True)
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi", tty=False)
        argv = mock_run.call_args[0][0]
        assert "-tt" not in argv


def test_run_check_true_raises_on_nonzero() -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _fail_completed(returncode=42, stderr="nope")
        with pytest.raises(SSHError, match="exit 42"):
            t.run("false")


def test_run_check_false_returns_nonzero_result() -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _fail_completed(returncode=42)
        result = t.run("false", check=False)
        assert isinstance(result, SSHResult)
        assert result.returncode == 42


def test_run_default_timeout_applies_when_call_omits_timeout() -> None:
    t = SSHTransport(host="vm1", user="agentworks", default_timeout=99)
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi")
        assert mock_run.call_args.kwargs["timeout"] == 99


def test_run_per_call_timeout_overrides_default() -> None:
    t = SSHTransport(host="vm1", user="agentworks", default_timeout=99)
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi", timeout=5)
        assert mock_run.call_args.kwargs["timeout"] == 5


def test_run_per_call_retries_overrides_constructor() -> None:
    """Per-call ``retries`` widens the timeout-retry budget for one-shot
    probes (live-resource checks, reconnect polls) without rebuilding
    the transport. Constructor default stays the everyday norm.
    """
    import subprocess as _subprocess

    t = SSHTransport(host="vm1", user="agentworks", retries=1)
    call_count = 0

    def raise_timeout(*_a: object, **_kw: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        raise _subprocess.TimeoutExpired(cmd=["ssh"], timeout=1)

    with (
        patch("agentworks.transports.ssh.subprocess.run", side_effect=raise_timeout),
        pytest.raises(SSHError),
    ):
        t.run("echo hi", retries=3)

    assert call_count == 3


# ---------------------------------------------------------------------------
# interactive()
# ---------------------------------------------------------------------------


def test_interactive_omits_command_when_empty() -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.interactive("")
        argv = mock_call.call_args[0][0]
        assert argv[-1] == "agentworks@vm1"


def test_interactive_appends_command_when_provided() -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.interactive("tmux attach -t s1")
        argv = mock_call.call_args[0][0]
        assert argv[-1] == "tmux attach -t s1"


def test_interactive_uses_minus_t_flag() -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.interactive("")
        argv = mock_call.call_args[0][0]
        assert "-t" in argv
        assert "BatchMode=yes" not in argv  # interactive must not BatchMode


# ---------------------------------------------------------------------------
# copy_to / copy_from
# ---------------------------------------------------------------------------


def test_copy_to_uses_scp() -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.copy_to("/local/foo", "/remote/bar")
        argv = mock_run.call_args[0][0]
        assert argv[0] == "scp"
        assert "/local/foo" in argv
        assert "agentworks@vm1:/remote/bar" in argv


def test_copy_from_uses_scp_with_reversed_source_dest() -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.copy_from("/remote/bar", "/local/foo")
        argv = mock_run.call_args[0][0]
        assert argv[0] == "scp"
        assert "agentworks@vm1:/remote/bar" in argv
        assert "/local/foo" in argv
        # Source must come before destination.
        assert argv.index("agentworks@vm1:/remote/bar") < argv.index("/local/foo")


def test_copy_to_raises_on_scp_failure() -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _fail_completed(stderr="permission denied")
        with pytest.raises(SSHError, match="scp failed"):
            t.copy_to("/local/foo", "/remote/bar")


# ---------------------------------------------------------------------------
# call_streaming()
# ---------------------------------------------------------------------------


def test_call_streaming_uses_minus_T_no_batchmode_violation() -> None:
    """``call_streaming`` must request no TTY (``-T``) and use
    ``BatchMode=yes`` so output streams to inherited stdio rather than
    waiting on prompts."""
    t = SSHTransport(host="vm1", user="agentworks")
    with patch("agentworks.transports.ssh.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.call_streaming("echo hi")
        argv = mock_call.call_args[0][0]
        assert "-T" in argv
        assert "BatchMode=yes" in argv
        assert argv[-1] == "echo hi"


# ---------------------------------------------------------------------------
# write_file()
# ---------------------------------------------------------------------------


def test_write_file_uses_copy_to_under_the_hood(tmp_path: Path) -> None:
    """``write_file`` must funnel through ``copy_to`` (which is scp) rather
    than embedding multi-line content in command argv -- the Windows CRLF
    trap that motivated the helper."""
    t = SSHTransport(host="vm1", user="agentworks")
    with patch.object(t, "copy_to") as mock_copy:
        t.write_file("/remote/conf", "hello\nworld\n")
        mock_copy.assert_called_once()
        # First positional arg is the local tempfile; second is remote_path.
        assert mock_copy.call_args[0][1] == "/remote/conf"


def test_write_file_chmods_when_mode_supplied(tmp_path: Path) -> None:
    t = SSHTransport(host="vm1", user="agentworks")
    with patch.object(t, "copy_to"), patch.object(t, "run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        t.write_file("/remote/conf", "x", mode="0644")
        # Last call (after the copy) should chmod the remote path.
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == "chmod 0644 /remote/conf"
