"""Per-transport contract tests for ``WSL2Transport``."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import WSL2Transport
from tests.transports.conftest import fail_completed as _fail_completed
from tests.transports.conftest import ok_completed as _ok_completed

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


def test_run_invokes_wsl_with_distro_and_user() -> None:
    t = WSL2Transport(distro_name="my-distro", user="agentworks")
    with patch("agentworks.transports.wsl2.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi")
        argv = mock_run.call_args[0][0]
        assert argv[:7] == [
            "wsl",
            "--distribution",
            "my-distro",
            "--user",
            "agentworks",
            "--",
            "bash",
        ]
        assert argv[7] == "-lc"
        assert argv[8] == "echo hi"


def test_run_env_injected_as_bash_assignment_prefix() -> None:
    t = WSL2Transport(distro_name="my-distro")
    with patch("agentworks.transports.wsl2.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi", env={"FOO": "bar"})
        argv = mock_run.call_args[0][0]
        assert argv[8].startswith("FOO=bar ")
        assert argv[8].endswith("echo hi")


def test_run_sudo_wraps_with_bash_c() -> None:
    t = WSL2Transport(distro_name="my-distro")
    with patch("agentworks.transports.wsl2.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("cmd1 && cmd2", sudo=True)
        argv = mock_run.call_args[0][0]
        assert "sudo -n bash -c 'cmd1 && cmd2'" in argv[8]


def test_run_check_true_raises_on_nonzero() -> None:
    t = WSL2Transport(distro_name="my-distro")
    with patch("agentworks.transports.wsl2.subprocess.run") as mock_run:
        mock_run.return_value = _fail_completed(returncode=5)
        with pytest.raises(SSHError, match="WSL2 command failed"):
            t.run("false")


def test_run_check_false_returns_result() -> None:
    t = WSL2Transport(distro_name="my-distro")
    with patch("agentworks.transports.wsl2.subprocess.run") as mock_run:
        mock_run.return_value = _fail_completed(returncode=5)
        result = t.run("false", check=False)
        assert isinstance(result, SSHResult)
        assert result.returncode == 5


# ---------------------------------------------------------------------------
# interactive()
# ---------------------------------------------------------------------------


def test_interactive_empty_command_opens_login_shell() -> None:
    t = WSL2Transport(distro_name="my-distro", user="agentworks")
    with patch("agentworks.transports.wsl2.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.interactive("")
        assert mock_call.call_args[0][0] == [
            "wsl",
            "--distribution",
            "my-distro",
            "--user",
            "agentworks",
        ]


def test_interactive_with_command_wraps_in_bash_lc() -> None:
    t = WSL2Transport(distro_name="my-distro", user="agentworks")
    with patch("agentworks.transports.wsl2.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.interactive("tmux attach -t s1")
        argv = mock_call.call_args[0][0]
        assert argv[-3:] == ["bash", "-lc", "tmux attach -t s1"]


# ---------------------------------------------------------------------------
# copy_to / copy_from
# ---------------------------------------------------------------------------


def test_copy_to_pipes_through_wsl_cat(tmp_path: Path) -> None:
    """``wsl ... bash -c 'cat > /path'`` with the file content piped on
    stdin -- avoids Windows path translation."""
    src = tmp_path / "data.bin"
    src.write_bytes(b"hello\n")

    t = WSL2Transport(distro_name="my-distro")
    with patch("agentworks.transports.wsl2.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        t.copy_to(src, "/remote/path")
        argv = mock_run.call_args[0][0]
        assert argv[:5] == ["wsl", "--distribution", "my-distro", "--user", "root"]
        assert argv[-1] == "cat > /remote/path"
        # Content must be piped in via stdin.
        assert mock_run.call_args.kwargs["input"] == b"hello\n"


def test_copy_from_pipes_through_wsl_cat(tmp_path: Path) -> None:
    """``copy_from`` runs ``cat <remote>`` and writes the captured bytes."""
    dst = tmp_path / "data.bin"

    t = WSL2Transport(distro_name="my-distro")
    with patch("agentworks.transports.wsl2.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"file-contents", stderr=b"")
        t.copy_from("/remote/path", dst)
        argv = mock_run.call_args[0][0]
        assert argv[-1] == "cat /remote/path"
        assert dst.read_bytes() == b"file-contents"


def test_copy_to_raises_on_failure(tmp_path: Path) -> None:
    src = tmp_path / "x"
    src.write_bytes(b"x")
    t = WSL2Transport(distro_name="my-distro")
    with patch("agentworks.transports.wsl2.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr=b"denied")
        with pytest.raises(SSHError, match="WSL2 copy failed"):
            t.copy_to(src, "/remote/x")


# ---------------------------------------------------------------------------
# call_streaming()
# ---------------------------------------------------------------------------


def test_call_streaming_invokes_wsl_bash_lc() -> None:
    t = WSL2Transport(distro_name="my-distro", user="agentworks")
    with patch("agentworks.transports.wsl2.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.call_streaming("echo hi")
        argv = mock_call.call_args[0][0]
        assert argv[:5] == ["wsl", "--distribution", "my-distro", "--user", "agentworks"]
        assert argv[-1] == "echo hi"
