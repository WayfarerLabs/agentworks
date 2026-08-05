"""Focused mise configuration and install-flow coverage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentworks.agents.initializer import _run_agent_mise_setup
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.config.validation import validate_mise_settings
from agentworks.errors import ConfigError
from agentworks.ssh import SSHError
from agentworks.vms.initializer.mise import _run_mise_install, _write_mise_config


def _result(*, ok: bool) -> MagicMock:
    return MagicMock(ok=ok, returncode=0 if ok else 1, stdout="", stderr="")


def test_write_mise_config_renders_settings_and_tools() -> None:
    target = MagicMock()
    logger = MagicMock()

    _write_mise_config(target, ["terraform@1.14.5", "aqua:npryce/adr-tools@3.0.0"], "7d", "/home/a", logger)

    content = target.write_file.call_args.args[1]
    assert 'install_before = "7d"' in content
    assert '"terraform" = "1.14.5"' in content
    assert '"aqua:npryce/adr-tools" = "3.0.0"' in content


def test_mise_validation_allows_scoped_backend_tool_name() -> None:
    validate_mise_settings(["npm:@scope/tool@1.2.3"], None, "7d", context="agent_templates.default")


def test_mise_validation_rejects_control_characters() -> None:
    with pytest.raises(ConfigError, match="name@version"):
        validate_mise_settings(["tool\x00@1"], None, "7d", context="agent_templates.default")


def test_run_mise_install_without_lockfile_runs_unlocked() -> None:
    target = MagicMock()
    target.run.side_effect = [_result(ok=False), _result(ok=True), _result(ok=True)]

    _run_mise_install(target, "bash", "/home/a", False, MagicMock())

    commands = [call.args[0] for call in target.run.call_args_list]
    assert "mise install -y'" in commands[1]
    assert all("--locked" not in command for command in commands)


def test_run_mise_install_locked_failure_does_not_fallback_by_default() -> None:
    target = MagicMock()
    target.run.side_effect = [_result(ok=True), SSHError("Failed to install tool@1: absent")]

    _run_mise_install(target, "bash", "/home/a", False, MagicMock())

    assert target.run.call_count == 2


def test_run_mise_install_locked_failure_falls_back_when_allowed() -> None:
    target = MagicMock()
    target.run.side_effect = [
        _result(ok=True),
        SSHError("Failed to install tool@1: absent"),
        _result(ok=True),
        _result(ok=True),
    ]

    _run_mise_install(target, "zsh", "/home/a", True, MagicMock())

    commands = [call.args[0] for call in target.run.call_args_list]
    assert "mise install -y --locked" in commands[1]
    assert "mise install -y'" in commands[2]
    assert "mise prune -y" in commands[3]


def test_agent_mise_setup_writes_config_and_installs_unlocked() -> None:
    target = MagicMock()
    target.run.return_value = _result(ok=False)
    template = ResolvedAgentTemplate(name="default", mise_packages=["terraform@1.14.5"])

    _run_agent_mise_setup(agent_target=target, agent_tmpl=template, home="/home/agt-dev")

    config_call = next(call for call in target.write_file.call_args_list if call.args[0].endswith("config.toml"))
    assert '"terraform" = "1.14.5"' in config_call.args[1]
    commands = [call.args[0] for call in target.run.call_args_list]
    assert any("mise install -y" in command and "--locked" not in command for command in commands)
