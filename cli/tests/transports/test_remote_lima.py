"""Per-transport contract tests for ``RemoteLimaTransport``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import RemoteLimaTransport


def _ok_completed(stdout: str = "", stderr: str = "") -> MagicMock:
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


def test_run_two_hops_ssh_to_host_then_limactl() -> None:
    """The outer hop is SSH to ``vm_host_ssh`` with login_shell=True;
    the inner command is ``limactl shell <vm> -- <cmd>``. Locks in both
    pieces so a future refactor can't drop the login-shell wrap."""
    t = RemoteLimaTransport(vm_name="my-vm", vm_host_ssh="host.example")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi")
        argv = mock_run.call_args[0][0]
        assert argv[0] == "ssh"
        assert "host.example" in argv
        # The remote payload (last argv) is wrapped in $SHELL -lc and
        # contains the limactl invocation.
        payload = argv[-1]
        assert payload.startswith("$SHELL -lc ")
        assert "limactl shell my-vm" in payload
        assert "echo hi" in payload


def test_run_env_embedded_in_lima_payload() -> None:
    """SetEnv at the host hop doesn't propagate into limactl shell, so
    env is embedded as scoped assignments inside the lima payload."""
    t = RemoteLimaTransport(vm_name="my-vm", vm_host_ssh="host.example")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("echo hi", env={"FOO": "bar"})
        argv = mock_run.call_args[0][0]
        payload = argv[-1]
        # The env prefix appears inside the lima payload (after the --).
        assert "FOO=bar" in payload


def test_run_sudo_wraps_with_bash_c() -> None:
    t = RemoteLimaTransport(vm_name="my-vm", vm_host_ssh="host.example")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _ok_completed()
        t.run("cmd1 && cmd2", sudo=True)
        payload = mock_run.call_args[0][0][-1]
        assert "sudo -n bash -c" in payload
        assert "cmd1 && cmd2" in payload


def test_run_check_true_raises_on_nonzero() -> None:
    t = RemoteLimaTransport(vm_name="my-vm", vm_host_ssh="host.example")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _fail_completed(returncode=3)
        with pytest.raises(SSHError):
            t.run("false")


def test_run_check_false_returns_result() -> None:
    t = RemoteLimaTransport(vm_name="my-vm", vm_host_ssh="host.example")
    with patch("agentworks.transports.ssh.subprocess.run") as mock_run:
        mock_run.return_value = _fail_completed(returncode=3)
        result = t.run("false", check=False)
        assert isinstance(result, SSHResult)
        assert result.returncode == 3


# ---------------------------------------------------------------------------
# interactive()
# ---------------------------------------------------------------------------


def test_interactive_two_hops_with_login_shell_wrap() -> None:
    """ssh -t to the VM host, then ``$SHELL -lc 'limactl shell ...'``
    so the host's PATH (Homebrew etc.) resolves limactl."""
    t = RemoteLimaTransport(vm_name="my-vm", vm_host_ssh="host.example")
    with patch("agentworks.transports.remote_lima.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.interactive("")
        argv = mock_call.call_args[0][0]
        assert argv[0] == "ssh"
        assert "-t" in argv
        assert "host.example" in argv
        payload = argv[-1]
        assert payload.startswith("$SHELL -lc ")
        assert "limactl shell my-vm" in payload


def test_interactive_with_command_includes_bash_lc() -> None:
    t = RemoteLimaTransport(vm_name="my-vm", vm_host_ssh="host.example")
    with patch("agentworks.transports.remote_lima.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.interactive("tmux attach -t s1")
        argv = mock_call.call_args[0][0]
        payload = argv[-1]
        assert "limactl shell my-vm bash -lc" in payload
        assert "tmux attach -t s1" in payload


# ---------------------------------------------------------------------------
# copy_to / copy_from
# ---------------------------------------------------------------------------


def test_copy_to_scps_to_host_then_limactl_copy() -> None:
    """Three steps: scp local -> host tmp, limactl copy host tmp -> VM,
    rm host tmp."""
    t = RemoteLimaTransport(vm_name="my-vm", vm_host_ssh="host.example")
    calls: list[list[str]] = []

    def fake_subprocess_run(args: list[str], **_kwargs: object) -> MagicMock:
        calls.append(args)
        return _ok_completed()

    with patch("agentworks.transports.ssh.subprocess.run", side_effect=fake_subprocess_run):
        t.copy_to("/local/foo", "/remote/bar")

    # Expect: scp ..., ssh ... limactl copy ..., ssh ... rm -f ...
    assert any(a[0] == "scp" for a in calls)
    assert any(a[0] == "ssh" and "limactl copy" in a[-1] for a in calls)
    assert any(a[0] == "ssh" and "rm -f" in a[-1] for a in calls)


def test_copy_from_pulls_via_host_tmp() -> None:
    """copy_from runs ``limactl copy VM:src host_tmp`` then scp host_tmp local."""
    t = RemoteLimaTransport(vm_name="my-vm", vm_host_ssh="host.example")
    calls: list[list[str]] = []

    def fake_subprocess_run(args: list[str], **_kwargs: object) -> MagicMock:
        calls.append(args)
        return _ok_completed()

    with patch("agentworks.transports.ssh.subprocess.run", side_effect=fake_subprocess_run):
        t.copy_from("/remote/bar", "/local/foo")

    assert any(a[0] == "ssh" and "limactl copy" in a[-1] for a in calls)
    assert any(a[0] == "scp" for a in calls)


# ---------------------------------------------------------------------------
# call_streaming()
# ---------------------------------------------------------------------------


def test_call_streaming_two_hops_with_login_shell() -> None:
    t = RemoteLimaTransport(vm_name="my-vm", vm_host_ssh="host.example")
    with patch("agentworks.transports.remote_lima.subprocess.call") as mock_call:
        mock_call.return_value = 0
        t.call_streaming("echo hi")
        argv = mock_call.call_args[0][0]
        assert argv[0] == "ssh"
        assert "-T" in argv
        assert "BatchMode=yes" in argv
        assert "host.example" in argv
        payload = argv[-1]
        assert payload.startswith("$SHELL -lc ")
        assert "limactl shell my-vm" in payload
        assert "echo hi" in payload
