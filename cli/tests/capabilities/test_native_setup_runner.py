"""Prepared setup env reaches the execution boundary without entering argv."""

import shlex
from unittest.mock import Mock

import pytest

from agentworks.harness_setup.runner import SetupRunner
from agentworks.transports import Transport


@pytest.mark.parametrize("sudo", [False, True])
def test_scoped_env_and_secret_values_remain_out_of_command(sudo):
    target = Mock(spec=Transport)
    runner = SetupRunner(target, {"TEAM_MODE": "careful", "API_TOKEN": "fixture-secret", "AGENTWORKS_VM": "vm"})
    result = runner.run(
        "worker --check",
        sudo=sudo,
        env={"TEAM_MODE": "quick", "AGENTWORKS_VM": "forged", "AGENTWORKS_SESSION": "foreign"},
    )
    command = target.run.call_args.args[0]
    options = target.run.call_args.kwargs
    assert options["env"] == {"TEAM_MODE": "quick", "API_TOKEN": "fixture-secret", "AGENTWORKS_VM": "vm"}
    assert "fixture-secret" not in command
    if sudo:
        arguments = shlex.split(command)
        assert arguments[0] == "sudo"
        preserved = next(argument.split("=", 1)[1] for argument in arguments if argument.startswith("--preserve-env="))
        assert set(preserved.split(",")) == set(options["env"])
        assert arguments[-1] == "worker --check"
        assert options["sudo"] is False
    else:
        assert command == "worker --check"
    assert result is target.run.return_value


def test_file_movement_delegates_without_copying_content_into_commands(tmp_path):
    target = Mock(spec=Transport)
    runner = SetupRunner(target, {"API_TOKEN": "fixture-secret"})
    source = tmp_path / "source"
    runner.copy_to(source, "/tmp/target", timeout=5)
    runner.copy_from("/tmp/target", source, timeout=8)
    target.copy_to.assert_called_once_with(source, "/tmp/target", timeout=5)
    target.copy_from.assert_called_once_with("/tmp/target", source, timeout=8)
    target.run.assert_not_called()
