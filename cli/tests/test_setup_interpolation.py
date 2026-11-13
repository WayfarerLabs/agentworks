"""Runtime context at installer and dotfiles interpretation boundaries."""

from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.agents.initializer import _run_agent_install_commands, create_agent_on_vm
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.bootstrap import build_registry
from agentworks.debian import DebianRelease
from agentworks.errors import ValidationError
from agentworks.install_commands import UserInstallCommandEntry
from agentworks.interpolation import context_values
from agentworks.vms.admin import AdminConfig
from agentworks.vms.initializer.driver import VMInitializationOperation, _phase_b_setup
from agentworks.vms.initializer.packages import _run_install_commands
from agentworks.vms.templates import ResolvedVMTemplate

if TYPE_CHECKING:
    from agentworks.db import Database


@pytest.mark.parametrize("agent", [False, True])
def test_install_fields_use_the_same_consuming_identity(agent: bool) -> None:
    user = "agt-dev" if agent else "operator"
    context = context_values(vm_name="box", user=user, agent_name="dev" if agent else None)
    entry = UserInstallCommandEntry(
        name="tool",
        command="printf '%s' '${agw:user_home_dir}/${agw:vm_name}'",
        test_exec="${agw:user_home_dir}/.tool/bin/tool",
        test_file="${agw:user_home_dir}/.tool/installed",
        test_dir="${agw:user_home_dir}/.tool",
    )
    target = MagicMock()
    target.run.return_value = SimpleNamespace(ok=False, returncode=1, stdout="", stderr="")
    if agent:
        registry = MagicMock()
        registry.iter_kind_items.return_value = [(entry.name, entry)]
        _run_agent_install_commands(
            agent_target=target,
            registry=registry,
            agent_tmpl=ResolvedAgentTemplate(name="default", user_install_commands=[entry.name]),
            home=f"/home/{user}",
            identity_env={"AGENTWORKS_AGENT": "dev"},
            context=context,
        )
    else:
        _run_install_commands(
            target, [entry.name], {entry.name: entry}, "bash", f"/home/{user}", MagicMock(), context=context
        )

    commands = [call.args[0] for call in target.run.call_args_list]
    check = next(command for command in commands if "command -v" in command)
    assert f"/home/{user}/.tool/bin/tool" in check
    assert f"test -f /home/{user}/.tool/installed" in check
    assert f"test -d /home/{user}/.tool" in check
    install = next(command for command in commands if "printf" in command and "grep" not in command)
    assert shlex.split(install) == ["bash", "-lc", f"printf '%s' '/home/{user}/box'"]
    assert entry.command == "printf '%s' '${agw:user_home_dir}/${agw:vm_name}'"


def test_skipped_install_does_not_render_its_unselected_command() -> None:
    entry = UserInstallCommandEntry(
        name="present", command="${agw:session_name}", test_file="${agw:user_home_dir}/installed"
    )
    target = MagicMock()
    target.run.return_value = SimpleNamespace(returncode=0)

    assert (
        _run_install_commands(
            target,
            [entry.name],
            {entry.name: entry},
            "bash",
            "/home/operator",
            MagicMock(),
            context=context_values(vm_name="box", user="operator"),
        )
        == []
    )
    assert [call.args[0] for call in target.run.call_args_list] == ["test -f /home/operator/installed"]


def test_install_check_rejects_unavailable_setup_context_before_remote_work() -> None:
    entry = UserInstallCommandEntry(name="tool", command="install-tool", test_dir="${agw:workspace_dir}")
    target = MagicMock()

    with pytest.raises(ValidationError) as caught:
        _run_install_commands(
            target,
            [entry.name],
            {entry.name: entry},
            "bash",
            "/home/operator",
            MagicMock(),
            context=context_values(vm_name="box", user="operator"),
        )

    assert caught.value.entity_name == "install-command/tool.test_dir"
    target.run.assert_not_called()


@pytest.mark.parametrize("agent", [False, True])
def test_dotfiles_render_target_fields_and_keep_source_literal(
    agent: bool, db: Database, make_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config()
    registry = build_registry(config)
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    vm = db.get_vm("box")
    assert vm is not None
    source = tmp_path / "${agw:user_name}"
    source.mkdir()
    user = "agt-dev" if agent else "operator"
    destination = f"/home/{user}/a project's box/dotfiles"
    fields = {
        "dotfiles_source": str(source),
        "dotfiles_destination": "${agw:user_home_dir}/a project's ${agw:vm_name}/dotfiles",
        "dotfiles_install_cmd": "printf '%s' '${agw:user_name}'",
    }
    target = MagicMock()
    target.run.return_value = SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")
    fetched = MagicMock()
    monkeypatch.setattr("agentworks.sources.fetch_dir", fetched)
    if agent:
        admin_target = MagicMock()

        def run_admin(command: str, **kwargs: object) -> SimpleNamespace:
            if command.startswith("getent passwd"):
                return SimpleNamespace(ok=False, returncode=2, stdout="")
            if command.startswith("id -gn"):
                return SimpleNamespace(ok=True, returncode=0, stdout=user)
            if command.startswith("mktemp"):
                return SimpleNamespace(ok=True, returncode=0, stdout="/tmp/agw-test-key")
            return SimpleNamespace(ok=True, returncode=0, stdout="")

        admin_target.run.side_effect = run_admin
        monkeypatch.setattr("agentworks.agents.initializer.transport", lambda *args, **kwargs: admin_target)
        monkeypatch.setattr("agentworks.transports.transport_for_user", lambda *args, **kwargs: target)
        create_agent_on_vm(
            vm,
            config,
            registry,
            ResolvedAgentTemplate(name="default", **fields),
            user,
            agent_name="dev",
            credential_requests=(),
            logger=MagicMock(),
            admin_git_force_safe_directory=False,
        )
    else:
        _phase_b_setup(
            db,
            config,
            registry,
            ResolvedVMTemplate(name="default"),
            AdminConfig(name="default", username=user, mise_activate=False, **fields),
            "box",
            target,
            (),
            f"/home/{user}",
            user,
            MagicMock(),
            debian_release=DebianRelease.TRIXIE,
            operation=VMInitializationOperation.VM_REINIT,
        )

    assert fetched.call_args.args[0].path == str(source)
    assert fetched.call_args.args[2] == destination
    command = next(
        call.args[0] for call in target.run.call_args_list if "printf" in call.args[0] and "cd " in call.args[0]
    )
    inner = shlex.split(command)[-1] if agent else command
    assert shlex.split(inner) == ["cd", destination, "&&", "printf", "%s", user]
