"""Focused tests for user install commands run over an agent transport."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentworks.agents.initializer import _run_agent_install_commands
from agentworks.install_commands import UserInstallCommandEntry


def _run_commands(entry: UserInstallCommandEntry, *, test_returncode: int) -> MagicMock:
    target = MagicMock()

    def run_side_effect(command: str, **kwargs: object) -> MagicMock:
        is_test = "command -v" in command or "test -x" in command or "test -f" in command or "test -d" in command
        returncode = test_returncode if is_test else 0
        return MagicMock(returncode=returncode, ok=returncode == 0, stdout="", stderr="")

    target.run.side_effect = run_side_effect
    registry = MagicMock()
    registry.iter_kind_items.return_value = [(entry.name, entry)]
    template = SimpleNamespace(shell="bash", user_install_commands=[entry.name])
    _run_agent_install_commands(
        agent_target=target,
        registry=registry,
        agent_tmpl=template,
        home="/home/agt-dev",
        identity_env={"AGENTWORKS_AGENT": "dev"},
    )
    return target


def test_agent_install_skips_when_all_declared_tests_pass() -> None:
    entry = UserInstallCommandEntry(
        name="my-tool",
        command="run-agent-install",
        test_exec="my-tool",
        test_file="~/.my-tool/installed",
        test_dir="~/.my-tool",
    )

    target = _run_commands(entry, test_returncode=0)

    commands = [call.args[0] for call in target.run.call_args_list]
    test_command = next(command for command in commands if "command -v" in command)
    assert "test -f /home/agt-dev/.my-tool/installed" in test_command
    assert "test -d /home/agt-dev/.my-tool" in test_command
    assert " && " in test_command
    assert not any("run-agent-install" in command for command in commands)


def test_agent_install_runs_when_any_declared_test_fails() -> None:
    entry = UserInstallCommandEntry(
        name="my-tool",
        command="run-agent-install",
        test_exec="my-tool",
        test_file="~/.my-tool/installed",
    )

    target = _run_commands(entry, test_returncode=1)

    commands = [call.args[0] for call in target.run.call_args_list]
    assert any("command -v" in command and "test -f" in command for command in commands)
    assert any("run-agent-install" in command for command in commands)


def test_agent_install_runs_with_zero_non_empty_tests() -> None:
    entry = UserInstallCommandEntry(
        name="my-tool",
        command="run-agent-install",
        test_exec="",
        test_file="",
        test_dir="",
    )

    target = _run_commands(entry, test_returncode=0)

    commands = [call.args[0] for call in target.run.call_args_list]
    assert not any("command -v" in command or "test -f" in command or "test -d" in command for command in commands)
    assert any("run-agent-install" in command for command in commands)


@pytest.mark.parametrize("test_returncode", [0, 1], ids=("executable", "not-executable"))
def test_agent_install_runner_uses_plain_executable_path_predicate(test_returncode: int) -> None:
    entry = UserInstallCommandEntry(
        name="path-tool",
        command="run-agent-install",
        test_exec="/opt/path tool/bin/tool",
    )

    target = _run_commands(entry, test_returncode=test_returncode)

    commands = [call.args[0] for call in target.run.call_args_list]
    predicate = next(command for command in commands if "test -x" in command)
    assert predicate == "test -x '/opt/path tool/bin/tool'"
    assert (not any("run-agent-install" in command for command in commands)) is (test_returncode == 0)
