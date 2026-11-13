"""Structural coverage for the instance-spec CLI placement contract."""

from __future__ import annotations

import pytest
import typer

from agentworks.cli import app


def _option_names(resource: str, command: str) -> set[str]:
    root = typer.main.get_command(app)
    root_commands = getattr(root, "commands", None)
    assert isinstance(root_commands, dict)
    group_commands = getattr(root_commands[resource], "commands", None)
    assert isinstance(group_commands, dict)
    return {option for parameter in group_commands[command].params for option in getattr(parameter, "opts", ())}


def test_instance_spec_options_exist_only_on_supported_lifecycle_commands() -> None:
    assert "--spec" in _option_names("vm", "create")
    assert "--spec" in _option_names("workspace", "create")
    assert "--spec" in _option_names("agent", "create")
    assert "--spec" in _option_names("agent", "reinit")
    assert {"--spec", "--workspace-spec", "--agent-spec"} <= _option_names("session", "create")

    assert "--spec" not in _option_names("vm", "reinit")
    assert "--spec" not in _option_names("workspace", "repair")
    assert "--spec" not in _option_names("workspace", "copy")
    assert "--spec" not in _option_names("session", "start")
    assert "--spec" not in _option_names("session", "restart")


def test_vm_reinit_hands_its_admin_options_to_the_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    received: dict[str, object] = {}
    monkeypatch.setattr("agentworks.cli.commands.vm.get_db", lambda: None)
    monkeypatch.setattr("agentworks.config.load_config", lambda: None)
    monkeypatch.setattr(
        "agentworks.vms.manager.reinit_vm", lambda db, config, name, **options: received.update(name=name, **options)
    )

    result = CliRunner().invoke(app, ["vm", "reinit", "box", "--admin-template", "work", "--admin-spec", ""])

    assert result.exit_code == 0, result.output
    assert received["name"] == "box"
    assert received["admin_template"] == "work"
    assert received["admin_spec"] == ""
