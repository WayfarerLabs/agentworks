"""Structural coverage for the instance-spec CLI placement contract."""

from __future__ import annotations

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
