"""VM-wide mise selection gates package and per-user setup."""

from __future__ import annotations

import shlex
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from agentworks.agents import initializer as agent_initializer
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.bootstrap import build_registry
from agentworks.db import Database
from agentworks.ssh import SSHResult
from agentworks.transports import Transport
from agentworks.vms.initializer.mise import MISE_SOURCE_FILE
from agentworks.vms.initializer.packages import _install_system_packages
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import resolve_from_dict
from tests.orchestrated_fixtures import proxmox_site, write_operator_config


def test_mise_selection_inherits_and_instance_overlay_wins() -> None:
    templates = {
        "base": VMTemplate(name="base", mise_enabled=False),
        "child": VMTemplate(name="child", inherits=["base"]),
    }
    assert VMTemplate(name="unset").mise_enabled is None
    assert resolve_from_dict({}).mise_enabled is True
    assert resolve_from_dict(templates, "child").mise_enabled is False
    assert (
        resolve_from_dict(templates, "child", overlay=VMTemplate(name="instance", mise_enabled=True)).mise_enabled
        is True
    )
    with pytest.raises(ValidationError):
        VMTemplate.model_validate({"name": "bad", "mise_enabled": "false"})


@pytest.mark.parametrize("enabled", [False, True])
def test_system_packages_only_install_mise_when_enabled(enabled: bool) -> None:
    target = MagicMock(spec=Transport)
    target.run.return_value = SSHResult(0, "", "")

    _install_system_packages(target, MagicMock(), mise_enabled=enabled)

    commands = [call.args[0] for call in target.run.call_args_list]
    package_install = next(command for command in commands if "apt-get install" in command)
    assert ("mise" in shlex.split(package_install)) is enabled
    assert any(shlex.split(command)[0] == "curl" for command in commands) is enabled
    assert any(MISE_SOURCE_FILE in command for command in commands) is enabled
    assert "python3" in shlex.split(package_install)
    assert all(call.kwargs["sudo"] for call in target.run.call_args_list)


def test_disabled_vm_does_not_run_agent_mise_setup(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_operator_config(tmp_path, manifests=[proxmox_site()])
    registry = build_registry(config)
    db.insert_vm("box", site="proxmox", hostname="box")
    vm = db.get_vm("box")
    assert vm is not None
    admin_target = MagicMock(spec=Transport)
    admin_target.run.return_value = SSHResult(0, "agt-dev", "")
    agent_target = MagicMock(spec=Transport)
    agent_target.run.return_value = SSHResult(0, "", "")
    monkeypatch.setattr(agent_initializer, "transport", lambda *args, **kwargs: admin_target)
    monkeypatch.setattr("agentworks.transports.transport_for_user", lambda *args, **kwargs: agent_target)
    monkeypatch.setattr("agentworks.vms.initializer._reconcile_authorized_keys", lambda *args, **kwargs: None)
    mise_setup = MagicMock()
    monkeypatch.setattr(agent_initializer, "_run_agent_mise_setup", mise_setup)
    template = ResolvedAgentTemplate(name="default", mise_packages=["python@3.14"])

    agent_initializer.create_agent_on_vm(
        vm,
        config,
        registry,
        template,
        "agt-dev",
        agent_name="dev",
        credential_requests=(),
        logger=MagicMock(),
        admin_git_force_safe_directory=False,
        mise_enabled=False,
    )

    mise_setup.assert_not_called()
    assert all("mise" not in call.args[0] for call in agent_target.run.call_args_list)
    profiles = [call.args[1] for call in agent_target.write_file.call_args_list if call.args[0].endswith("profile.sh")]
    assert profiles
    assert all("/mise/shims" not in profile for profile in profiles)
    shell_rc = next(
        call.args[1] for call in agent_target.write_file.call_args_list if call.args[0].endswith(".agentworks-rc.sh")
    )
    assert "mise activate" not in shell_rc
