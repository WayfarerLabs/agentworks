"""CLI routing for canonical session lifecycle and the 0.18 wrapper."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.sessions import manager as session_manager


@pytest.fixture
def command_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("agentworks.cli.commands.session.get_db", lambda: object())
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
        (["--all", "--yes"], "restart_all_sessions"),
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


def _stub_resume_status(
    monkeypatch: pytest.MonkeyPatch,
    status: object | None,
    *,
    incomplete: bool = False,
    missing_ticks_only: bool = False,
) -> None:
    sessions = [
        SimpleNamespace(
            name="coding",
            socket_path="/run/agentworks/session.sock",
            pid=42,
            boot_id=None if incomplete else "5f8421a2-6f48-462f-896a-3194bd26d975",
            tmux_server_start_ticks=None if incomplete or missing_ticks_only else 77,
        )
    ]

    class _Database:
        def get_session(self, name: str) -> object | None:
            return sessions[0] if name == "coding" else None

    monkeypatch.setattr("agentworks.cli.commands.session.get_db", lambda: _Database())
    monkeypatch.setattr(session_manager, "filter_sessions", lambda *args, **kwargs: sessions)
    monkeypatch.setattr(
        session_manager,
        "batch_check_all_sessions",
        lambda *args, **kwargs: {} if status is None else {"coding": status},
    )


@pytest.mark.parametrize("arguments", [["coding"], ["--all"]])
def test_resume_wrapper_requires_consent_before_replacing_running_sessions(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    command_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    from agentworks import output
    from agentworks.db import SessionStatus

    _stub_resume_status(monkeypatch, SessionStatus.OK)
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "confirm", lambda *args, **kwargs: False)

    result = CliRunner().invoke(app, ["session", "resume", *arguments])

    assert result.exit_code != 0
    assert command_calls == []


@pytest.mark.parametrize("arguments", [["coding"], ["--all"]])
def test_resume_wrapper_routes_after_running_session_consent(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    command_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    from agentworks import output
    from agentworks.db import SessionStatus

    _stub_resume_status(monkeypatch, SessionStatus.OK)
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "confirm", lambda *args, **kwargs: True)

    result = CliRunner().invoke(app, ["session", "resume", *arguments])

    assert result.exit_code == 0, result.output
    assert [name for name, _kwargs in command_calls] == [
        "restart_session" if arguments[0] == "coding" else "restart_all_sessions"
    ]


def test_resume_wrapper_does_not_confirm_before_restarting_a_stopped_session(
    monkeypatch: pytest.MonkeyPatch,
    command_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    from agentworks import output
    from agentworks.db import SessionStatus

    _stub_resume_status(monkeypatch, SessionStatus.STOPPED)
    monkeypatch.setattr(output, "confirm", lambda *args, **kwargs: pytest.fail("unexpected confirmation"))

    result = CliRunner().invoke(app, ["session", "resume", "coding"])

    assert result.exit_code == 0, result.output
    assert [name for name, _kwargs in command_calls] == ["restart_session"]


def test_resume_wrapper_requires_yes_for_noninteractive_running_replacement(
    monkeypatch: pytest.MonkeyPatch,
    command_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    from agentworks import output
    from agentworks.db import SessionStatus

    _stub_resume_status(monkeypatch, SessionStatus.OK)
    monkeypatch.setattr(output, "confirm", lambda *args, **kwargs: pytest.fail("unexpected confirmation"))

    result = CliRunner().invoke(
        app,
        ["--non-interactive", "session", "resume", "coding"],
        input="y\n",
    )

    assert result.exit_code != 0
    assert command_calls == []


def test_declined_resume_does_not_repair_an_incomplete_runtime_row(
    monkeypatch: pytest.MonkeyPatch,
    command_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    from agentworks import output

    _stub_resume_status(monkeypatch, None, incomplete=True)
    monkeypatch.setattr(
        session_manager,
        "ensure_pids_batch",
        lambda *args, **kwargs: pytest.fail("unexpected durable repair"),
    )
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "confirm", lambda *args, **kwargs: False)

    result = CliRunner().invoke(app, ["session", "resume", "coding"])

    assert result.exit_code != 0
    assert command_calls == []


@pytest.mark.parametrize("arguments", [["coding"], ["--all"]])
def test_unknown_incomplete_runtime_requires_resume_consent(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    command_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    from agentworks import output
    from agentworks.db import SessionStatus

    _stub_resume_status(monkeypatch, SessionStatus.UNKNOWN, missing_ticks_only=True)
    monkeypatch.setattr(
        session_manager,
        "ensure_pids_batch",
        lambda *args, **kwargs: pytest.fail("unexpected durable repair"),
    )
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "confirm", lambda *args, **kwargs: False)

    result = CliRunner().invoke(app, ["session", "resume", *arguments])

    assert result.exit_code != 0
    assert command_calls == []
