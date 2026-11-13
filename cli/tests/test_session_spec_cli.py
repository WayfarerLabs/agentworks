"""Named session launch commands route instance specs without bulk mutation."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.completions.spec import build_spec
from agentworks.sessions import manager as session_manager


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("agentworks.cli.commands.session.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    for name in ("start_session", "restart_session", "start_all_sessions", "restart_all_sessions"):
        monkeypatch.setattr(session_manager, name, lambda *args, _name=name, **kwargs: seen.append((_name, kwargs)))
    return seen


@pytest.mark.parametrize("operation", ["start", "restart"])
@pytest.mark.parametrize("spec", [None, "{}", '{"env":{"MODE":"literal value"}}'])
def test_named_launch_forwards_complete_spec(
    operation: str, spec: str | None, calls: list[tuple[str, dict[str, Any]]]
) -> None:
    arguments = ["session", operation, "coding"]
    if spec is not None:
        arguments += ["--spec", spec]
    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    method, kwargs = calls[0]
    assert method == f"{operation}_session"
    assert kwargs["name"] == "coding"
    assert kwargs["spec"] == spec


@pytest.mark.parametrize("operation", ["start", "restart"])
def test_bulk_launch_never_receives_spec(operation: str, calls: list[tuple[str, dict[str, Any]]]) -> None:
    result = CliRunner().invoke(app, ["session", operation, "--all"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    method, kwargs = calls[0]
    assert method == f"{operation}_all_sessions"
    assert "spec" not in kwargs


@pytest.mark.parametrize("operation", ["start", "restart"])
@pytest.mark.parametrize("spec", ["{}", '{"harness_integration":{"name":"claude-code"}}'])
def test_bulk_spec_refuses_before_state_or_interaction(
    operation: str, spec: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid bulk spec reached application state or interaction")

    monkeypatch.setattr("agentworks.cli.commands.session.get_db", forbidden)
    monkeypatch.setattr("agentworks.config.load_config", forbidden)
    monkeypatch.setattr("agentworks.cli.commands.session.ordinary_tty_interaction_policy", forbidden)
    for name in ("start_session", "restart_session", "start_all_sessions", "restart_all_sessions"):
        monkeypatch.setattr(session_manager, name, forbidden)
    result = CliRunner().invoke(app, ["session", operation, "--all", "--spec", spec])

    assert result.exit_code == 2


@pytest.mark.parametrize("operation", ["start", "restart"])
def test_spec_requires_a_named_session(operation: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.cli.commands.session.get_db", lambda: pytest.fail("opened database without a name"))
    result = CliRunner().invoke(app, ["session", operation, "--spec", "{}"])
    assert result.exit_code == 2


def test_completion_metadata_treats_spec_as_an_uncompleted_value() -> None:
    session = build_spec(app).subcommands["session"]
    for command in session.subcommands.values():
        if command.name not in ("start", "restart"):
            continue
        parameter = next(parameter for parameter in command.params if "--spec" in parameter.opts)
        assert not parameter.is_flag
        assert not parameter.is_argument
        assert parameter.choices is None
        assert parameter.dynamic_completer is None
