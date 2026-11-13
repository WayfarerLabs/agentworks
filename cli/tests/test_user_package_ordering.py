"""User package installers publish PATH before custom commands on first init."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentworks.agents import initializer as agent_init
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.debian import DebianRelease
from agentworks.install_commands import UserInstallCommandEntry
from agentworks.ssh import SSHResult
from agentworks.vms.admin import AdminConfig
from agentworks.vms.initializer import driver
from agentworks.vms.templates import ResolvedVMTemplate

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux guest shell profiles")


@pytest.mark.parametrize("role", ["admin", "agent"])
@pytest.mark.parametrize("mise_enabled,npm_enabled", [(True, True), (False, True), (True, False), (False, False)])
def test_first_init_custom_commands_see_enabled_package_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, mise_enabled: bool, npm_enabled: bool
) -> None:
    events: list[str] = []
    profile = ""
    home = str(tmp_path)
    mise_bin = tmp_path / ".local/share/mise/shims"
    npm_bin = tmp_path / ".local/bin"

    def tool(directory: Path, name: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        command = directory / name
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o755)

    def mise(*args, **kwargs) -> None:
        events.append("mise")
        tool(mise_bin, "mise-probe")

    def npm(*args, **kwargs) -> list[str]:
        events.append("npm")
        tool(npm_bin, "npm-probe")
        return [str(npm_bin)]

    def write_file(path: str, content: str, **kwargs) -> None:
        nonlocal profile
        if path.endswith(".agentworks-profile.sh"):
            profile = content

    def run(command: str, **kwargs) -> SSHResult:
        if "run-custom-probe" in command:
            events.append("custom")
            checks = [
                ("" if enabled else "! ") + f"command -v {name} >/dev/null"
                for name, enabled in (("mise-probe", mise_enabled), ("npm-probe", npm_enabled))
            ]
            subprocess.run(
                ["/bin/sh", "-c", profile + "\n" + " && ".join(checks)],
                env={"HOME": home, "PATH": "/usr/bin:/bin"},
                check=True,
                capture_output=True,
            )
        if command.startswith("getent passwd"):
            return SSHResult(0, f"dev:x:1000:1000::{home}:/bin/bash", "")
        if command.startswith("id -gn"):
            return SSHResult(0, "dev", "")
        return SSHResult(0, "", "")

    target = MagicMock()
    target.run.side_effect = run
    target.write_file.side_effect = write_file
    entry = UserInstallCommandEntry(
        name="custom", command="run-custom-probe", path=["~/.local/bin", "$HOME/.local/bin"]
    )
    monkeypatch.setattr(
        "agentworks.resources.access.kind_dict",
        lambda registry, kind: {"custom": entry} if kind == "user-install-command" else {},
    )
    monkeypatch.setattr("agentworks.vms.sites.site_platform_name", lambda *args: "lima")
    for name in ("ensure_agent_socket_root", "ensure_agent_socket_dir", "cleanup_stale_sockets"):
        monkeypatch.setattr(f"agentworks.sessions.tmux.{name}", lambda *args, **kwargs: None)
    monkeypatch.setattr(driver, "_run_mise_install", mise)
    monkeypatch.setattr(agent_init, "_run_agent_mise_setup", mise)
    monkeypatch.setattr(driver, "run_npm_install", npm)
    monkeypatch.setattr("agentworks.vms.initializer.npm.run_npm_install", npm)
    logger = MagicMock()
    declaration = {
        "mise_packages": ["tool@1.0.0"],
        "npm_packages": ["tool@1.0.0"],
        "npm_enabled": npm_enabled,
        "user_install_commands": ["custom"],
    }
    vm = SimpleNamespace(name="box", site="lima-local", admin_username="dev")
    if role == "agent":
        monkeypatch.setattr(agent_init, "transport", lambda *args, **kwargs: target)
        monkeypatch.setattr("agentworks.transports.transport_for_user", lambda *args, **kwargs: target)
        monkeypatch.setattr(agent_init, "managed_home", lambda user: home)
        monkeypatch.setattr("agentworks.vms.initializer._reconcile_authorized_keys", lambda *args, **kwargs: None)
        agent_init.create_agent_on_vm(
            vm,
            MagicMock(),
            MagicMock(),
            ResolvedAgentTemplate(name="test", **declaration),
            "dev",
            agent_name="dev",
            credential_requests=(),
            logger=logger,
            admin_git_force_safe_directory=False,
            mise_enabled=mise_enabled,
        )
    else:
        for name in (
            "_preserve_ssh_host_keys",
            "_apply_sve_mask",
            "_write_sshd_accept_env",
            "_write_sudoers_env_keep",
            "_write_sudoers_console_setenv",
            "_install_system_packages",
            "_write_agentworks_identity_profile",
            "_write_skel_seeds",
            "_harden_admin_home",
            "_setup_workspaces_directory",
            "_write_mise_config",
            "configure_user_git_credentials",
        ):
            monkeypatch.setattr(driver, name, lambda *args, **kwargs: None)
        for path in (
            "agentworks.vms.hardening.apply_vm_hardening",
            "agentworks.vms.tailscale_dns.check_vm_dns",
            "agentworks.vms.tailscale_dns.apply_tailscaled_dns_fix",
        ):
            monkeypatch.setattr(path, lambda *args, **kwargs: None)
        db = MagicMock()
        db.get_vm.return_value = vm
        db.list_agents.return_value = []
        driver._phase_b_setup(
            db,
            MagicMock(),
            MagicMock(),
            ResolvedVMTemplate(name="test", mise_enabled=mise_enabled),
            AdminConfig(username="dev", git_force_safe_directory=False, **declaration),
            "box",
            target,
            (),
            home,
            "dev",
            logger,
            debian_release=DebianRelease.TRIXIE,
            operation=driver.VMInitializationOperation.VM_CREATE,
        )
    assert events == (["mise"] if mise_enabled else []) + (["npm"] if npm_enabled else []) + ["custom"]
    # The final profile preserves the same availability observed by the custom command.
    run("run-custom-probe")
    result = subprocess.run(
        ["/bin/sh", "-c", profile + '\nprintf "%s" "$PATH"'],
        env={"HOME": home, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split(":").count(str(npm_bin)) == 1
