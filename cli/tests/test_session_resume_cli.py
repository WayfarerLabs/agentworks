"""CLI routing for canonical session lifecycle and the 0.19 wrapper."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.sessions import manager as session_manager


@pytest.fixture
def command_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    for name in ("start_session", "restart_session", "start_all_sessions", "restart_all_sessions"):
        monkeypatch.setattr(
            session_manager,
            name,
            lambda *args, _name=name, **kwargs: calls.append((_name, kwargs)),
        )
    return calls


@pytest.mark.parametrize(
    ("arguments", "operation", "selected"),
    [
        (["start", "coding", "--force-new"], "start_session", {"name": "coding", "force_new": True}),
        (["restart", "coding", "--force"], "restart_session", {"name": "coding", "force": True}),
        (
            ["start", "--all", "--vm", "vm1", "--workspace", "ws1"],
            "start_all_sessions",
            {"vm_name": "vm1", "workspace_name": "ws1"},
        ),
        (["restart", "--all", "--agent", "agent1"], "restart_all_sessions", {"agent_name": "agent1"}),
    ],
)
def test_canonical_launch_commands_route_to_matching_service(
    arguments: list[str],
    operation: str,
    selected: dict[str, object],
    command_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    result = CliRunner().invoke(app, ["session", *arguments])
    assert result.exit_code == 0, result.output
    assert len(command_calls) == 1
    actual_operation, kwargs = command_calls[0]
    assert actual_operation == operation
    assert kwargs.items() >= selected.items()


@pytest.mark.parametrize(
    ("arguments", "operation"),
    [
        (["coding", "--yes"], "restart_session"),
        (["--all-stopped"], "start_all_sessions"),
        (["--all"], "restart_all_sessions"),
    ],
)
def test_resume_wrapper_routes_to_canonical_services(
    arguments: list[str],
    operation: str,
    command_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    result = CliRunner().invoke(app, ["session", "resume", *arguments])
    assert result.exit_code == 0, result.output
    assert [name for name, _kwargs in command_calls] == [operation]
