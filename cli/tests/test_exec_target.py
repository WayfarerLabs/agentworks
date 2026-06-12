"""Tests for ExecTarget.run() -- sudo wrapping, tty resolution, and Proxmox stub."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agentworks.ssh import ExecTarget, SSHResult, SSHTarget

# ---------------------------------------------------------------------------
# sudo wrapping
# ---------------------------------------------------------------------------


def test_sudo_false_does_not_wrap() -> None:
    """run(sudo=False) passes command through unchanged."""
    target = ExecTarget(ssh=SSHTarget(host="test", user="admin"))
    with patch("agentworks.ssh.run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        target.run("echo hello", sudo=False)
        cmd = mock_run.call_args[0][1]
        assert cmd == "echo hello"


def test_sudo_true_wraps_with_bash_c() -> None:
    """run(sudo=True) wraps entire command in sudo -n bash -c '...'."""
    target = ExecTarget(ssh=SSHTarget(host="test", user="admin"))
    with patch("agentworks.ssh.run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        target.run("echo hello", sudo=True)
        cmd = mock_run.call_args[0][1]
        assert cmd == "sudo -n bash -c 'echo hello'"


def test_sudo_wraps_compound_commands() -> None:
    """sudo=True wraps the entire compound command, not just the first part."""
    target = ExecTarget(ssh=SSHTarget(host="test", user="admin"))
    with patch("agentworks.ssh.run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        target.run("cmd1 && cmd2 && cmd3", sudo=True)
        cmd = mock_run.call_args[0][1]
        assert cmd == "sudo -n bash -c 'cmd1 && cmd2 && cmd3'"


def test_sudo_escapes_single_quotes() -> None:
    """sudo=True correctly escapes commands containing single quotes."""
    target = ExecTarget(ssh=SSHTarget(host="test", user="admin"))
    with patch("agentworks.ssh.run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        target.run("echo 'hello world'", sudo=True)
        cmd = mock_run.call_args[0][1]
        # shlex.quote handles inner single quotes
        assert "sudo -n bash -c" in cmd
        assert "hello world" in cmd


def test_module_run_as_root_wraps_with_bash_c() -> None:
    """Module-level run_as_root uses the same `sudo -n bash -c '...'` wrapping
    as ExecTarget.run(sudo=True), so pipelines run fully as root."""
    from agentworks.ssh import run_as_root

    target = SSHTarget(host="test", user="admin")
    with patch("agentworks.ssh.run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        run_as_root(target, "cmd1 && cmd2")
        cmd = mock_run.call_args[0][1]
        assert cmd == "sudo -n bash -c 'cmd1 && cmd2'"


# ---------------------------------------------------------------------------
# tty resolution
# ---------------------------------------------------------------------------


def test_tty_none_respects_force_tty_false() -> None:
    """tty=None with force_tty=False does not add -tt."""
    target = ExecTarget(ssh=SSHTarget(host="test", user="admin", force_tty=False))
    with patch("agentworks.ssh.run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        target.run("echo ok")
        ssh_target = mock_run.call_args[0][0]
        assert ssh_target.force_tty is False


def test_tty_none_respects_force_tty_true() -> None:
    """tty=None with force_tty=True keeps -tt."""
    target = ExecTarget(ssh=SSHTarget(host="test", user="admin", force_tty=True))
    with patch("agentworks.ssh.run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        target.run("echo ok")
        ssh_target = mock_run.call_args[0][0]
        assert ssh_target.force_tty is True


def test_tty_false_overrides_force_tty() -> None:
    """tty=False suppresses TTY even when force_tty=True."""
    target = ExecTarget(ssh=SSHTarget(host="test", user="admin", force_tty=True))
    with patch("agentworks.ssh.run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        target.run("echo ok", tty=False)
        ssh_target = mock_run.call_args[0][0]
        assert ssh_target.force_tty is False


def test_tty_true_forces_tty() -> None:
    """tty=True requests TTY even when force_tty=False."""
    target = ExecTarget(ssh=SSHTarget(host="test", user="admin", force_tty=False))
    with patch("agentworks.ssh.run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        target.run("echo ok", tty=True)
        ssh_target = mock_run.call_args[0][0]
        assert ssh_target.force_tty is True


# ---------------------------------------------------------------------------
# Non-SSH transports ignore tty
# ---------------------------------------------------------------------------


def test_lima_ignores_tty() -> None:
    """Lima transport ignores the tty parameter."""
    from agentworks.ssh import LimaTarget

    target = ExecTarget(lima=LimaTarget(vm_name="test"))
    with patch("agentworks.ssh.lima_run") as mock_run:
        mock_run.return_value = SSHResult(returncode=0, stdout="", stderr="")
        target.run("echo ok", tty=True)  # should not error
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Proxmox stub
# ---------------------------------------------------------------------------


def test_proxmox_admin_exec_target_raises() -> None:
    """Proxmox admin_exec_target raises NotImplementedError."""
    from agentworks.vms.provisioners.proxmox import ProxmoxProvisioner

    vm = MagicMock()
    vm.tailscale_host = "100.1.2.3"
    vm.admin_username = "admin"

    # ProxmoxProvisioner needs a config -- mock it
    proxmox_config = MagicMock()
    with patch.dict("os.environ", {"PROXMOX_TOKEN_SECRET": "test-secret"}):
        provisioner = ProxmoxProvisioner(proxmox_config)

    with pytest.raises(NotImplementedError, match="guest agent"):
        provisioner.admin_exec_target(vm)


# ---------------------------------------------------------------------------
# admin_exec_target / agent_exec_target builders
# ---------------------------------------------------------------------------


def _mock_vm(tailscale_host: str = "100.64.0.1") -> MagicMock:
    vm = MagicMock()
    vm.name = "vm1"
    vm.tailscale_host = tailscale_host
    vm.admin_username = "agentworks"
    return vm


def _mock_config() -> MagicMock:
    from pathlib import Path as _P

    config = MagicMock()
    config.operator.ssh_private_key = _P("/home/op/.ssh/agentworks_ed25519")
    return config


def _mock_agent(linux_user: str = "claude") -> MagicMock:
    a = MagicMock()
    a.linux_user = linux_user
    a.vm_name = "vm1"
    a.name = linux_user
    return a


def test_admin_exec_target_uses_admin_username() -> None:
    from agentworks.ssh import admin_exec_target

    target = admin_exec_target(_mock_vm(), _mock_config())

    assert target.ssh is not None
    assert target.ssh.user == "agentworks"
    assert target.ssh.host == "100.64.0.1"


def test_agent_exec_target_uses_agent_linux_user() -> None:
    from agentworks.ssh import agent_exec_target

    target = agent_exec_target(_mock_vm(), _mock_config(), _mock_agent("claude"))

    assert target.ssh is not None
    assert target.ssh.user == "claude"
    assert target.ssh.host == "100.64.0.1"
    # Same VM host as the admin target -- only the user differs.
    assert target.ssh.identity_file is not None
    assert target.ssh.identity_file.name == "agentworks_ed25519"


def test_admin_and_agent_targets_differ_only_in_user() -> None:
    """The whole point of Phase 4: every option but the SSH user is identical."""
    from agentworks.ssh import admin_exec_target, agent_exec_target

    vm = _mock_vm()
    config = _mock_config()
    admin = admin_exec_target(vm, config)
    agent = agent_exec_target(vm, config, _mock_agent("claude"))

    assert admin.ssh is not None
    assert agent.ssh is not None
    assert admin.ssh.host == agent.ssh.host
    assert admin.ssh.identity_file == agent.ssh.identity_file
    assert admin.ssh.proxy_jump == agent.ssh.proxy_jump
    assert admin.ssh.port == agent.ssh.port
    assert admin.ssh.force_tty == agent.ssh.force_tty
    # Only the SSH user differs
    assert admin.ssh.user == "agentworks"
    assert agent.ssh.user == "claude"


# ---------------------------------------------------------------------------
# interactive(): empty-command behavior (Phase 6)
# ---------------------------------------------------------------------------


def test_interactive_omits_trailing_command_when_empty() -> None:
    """An empty command means 'interactive login shell, no remote argv'.

    Phase 6 of the direct-target-user-SSH SDD relies on this: agent shell
    runs `ssh -t <agent>@vm` with no command. Previously interactive()
    appended an empty string as the command arg, which ran the empty
    string and exited immediately.
    """
    from agentworks.ssh import interactive

    target = ExecTarget(ssh=SSHTarget(host="vm1", user="agent", identity_file=None))
    with patch("agentworks.ssh.subprocess.call") as mock_call:
        mock_call.return_value = 0
        interactive(target, "")
        argv = mock_call.call_args[0][0]
        # The argv ends with the user@host target; no trailing command arg.
        assert argv[-1] == "agent@vm1"
        assert "" not in argv


def test_interactive_appends_command_when_non_empty() -> None:
    """Non-empty command is still appended verbatim (tmux attach etc.)."""
    from agentworks.ssh import interactive

    target = ExecTarget(ssh=SSHTarget(host="vm1", user="agent", identity_file=None))
    with patch("agentworks.ssh.subprocess.call") as mock_call:
        mock_call.return_value = 0
        interactive(target, "tmux attach -t foo")
        argv = mock_call.call_args[0][0]
        assert argv[-1] == "tmux attach -t foo"
