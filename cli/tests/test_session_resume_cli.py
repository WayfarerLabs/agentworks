"""CLI coverage for canonical session resume and the removed restart alias."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager as session_manager


@pytest.fixture
def command_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    monkeypatch.setattr(session_manager, "resume_session", lambda *args, **kwargs: calls.append(("single", kwargs)))
    monkeypatch.setattr(session_manager, "resume_all_sessions", lambda *args, **kwargs: calls.append(("batch", kwargs)))
    return calls


@pytest.mark.parametrize(
    "arguments, expected",
    [
        (
            ["coding", "--force", "--yes"],
            (
                "single",
                {
                    "name": "coding",
                    "force": True,
                    "yes": True,
                    "interaction": TtyInteractionPolicy.ALLOW,
                },
            ),
        ),
        (
            ["--all-stopped", "--vm", "vm1", "--workspace", "ws1", "--agent", "agent1"],
            (
                "batch",
                {
                    "vm_name": "vm1",
                    "workspace_name": "ws1",
                    "agent_name": "agent1",
                    "admin_only": False,
                    "include_running": False,
                    "force": False,
                    "interaction": TtyInteractionPolicy.ALLOW,
                },
            ),
        ),
    ],
)
def test_resume_dispatches_canonical_behavior(
    arguments: list[str], expected: tuple[str, dict[str, Any]], command_calls: list[tuple[str, dict[str, Any]]]
) -> None:
    result = CliRunner().invoke(app, ["session", "resume", *arguments])
    assert result.exit_code == 0, result.output
    assert command_calls == [expected]


def test_restart_is_an_unknown_command() -> None:
    result = CliRunner().invoke(app, ["session", "restart", "coding"])
    assert result.exit_code == 2
    assert "No such command 'restart'" in result.output
