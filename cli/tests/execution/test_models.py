"""Invocation values enforce stable, secret-safe caller boundaries."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.models import Command, Script, Shell


@pytest.mark.parametrize("argv", [["tool", "argument"], ("tool", "argument")])
def test_command_normalizes_public_argv_sequences(argv: list[str] | tuple[str, ...]) -> None:
    command = Command(argv)
    assert command.argv == ("tool", "argument")
    assert isinstance(command.argv, tuple)


def test_command_does_not_retain_a_mutable_argv_list() -> None:
    argv = ["tool", "original"]
    command = Command(argv)
    argv[1] = "changed"
    assert command.argv == ("tool", "original")
    with pytest.raises(FrozenInstanceError):
        command.argv = ("other",)  # type: ignore[misc]


@pytest.mark.parametrize("argv", ["tool", {"tool"}, (), [], ("",), ("tool", 1)])
def test_command_refuses_invalid_argv_without_exposing_values(argv: object) -> None:
    with pytest.raises(ValidationError) as failure:
        Command(argv)  # type: ignore[arg-type]
    assert repr(argv) not in repr(failure.value)
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None


@pytest.mark.parametrize("shell", [Shell.SH, Shell.BASH, Shell.USER_DEFAULT])
def test_script_retains_explicit_shell_selection(shell: Shell) -> None:
    script = Script("private-source", shell)
    assert script.shell is shell
    assert not script.login
    assert not script.interactive
    assert "private-source" not in repr(script)


def test_script_requires_enum_shell_and_boolean_startup_flags() -> None:
    with pytest.raises(ValidationError):
        Script("private-source", "sh")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Script("private-source", Shell.SH, login=1)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Script("private-source", Shell.SH, interactive="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Script("private-source", Shell.SH, True)  # type: ignore[call-arg]
