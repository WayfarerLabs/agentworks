"""Behavioral coverage for named-console filters on batch session operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.db import SessionMode
from agentworks.errors import NotFoundError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager as session_manager

if TYPE_CHECKING:
    from agentworks.db import Database


def _seed_console_filters(db: Database) -> None:
    db.insert_vm("vm-a", site="lima", hostname="lima--vm-a")
    db.insert_vm("vm-b", site="lima", hostname="lima--vm-b")
    db.insert_workspace("ws-a", workspace_path="/tmp/ws-a", vm_name="vm-a", linux_group="ws-a")
    db.insert_workspace("ws-b", workspace_path="/tmp/ws-b", vm_name="vm-b", linux_group="ws-b")
    db.insert_agent("agent-a", vm_name="vm-a", linux_user="agent-a")
    db.insert_session(
        "session-a",
        "ws-a",
        "default",
        SessionMode.AGENT,
        agent_name="agent-a",
        socket_path="/tmp/session-a.sock",
    )
    db.insert_session("session-b", "ws-a", "default", SessionMode.ADMIN)
    db.insert_session("session-c", "ws-b", "default", SessionMode.ADMIN)
    db.insert_console("console-a", "vm-a")
    db.insert_console("console-b", "vm-b")
    db.insert_console("console-empty", "vm-a")
    db.add_console_session("console-a", "session-a", [])
    db.add_console_session("console-b", "session-c", [])


def test_filter_sessions_console_values_or_and_other_filters_intersect(db: Database) -> None:
    _seed_console_filters(db)

    union = session_manager.filter_sessions(db, console_name=["console-a", "console-b"])
    intersection = session_manager.filter_sessions(
        db,
        console_name=["console-a", "console-b"],
        vm_name="vm-a",
        agent_name="agent-a",
    )

    assert [session.name for session in union] == ["session-a", "session-c"]
    assert [session.name for session in intersection] == ["session-a"]


def test_filter_sessions_rejects_unknown_console(db: Database) -> None:
    _seed_console_filters(db)

    with pytest.raises(NotFoundError) as exc_info:
        session_manager.filter_sessions(db, console_name=["console-a", "missing-console"])

    assert exc_info.value.entity_kind == "console"
    assert exc_info.value.entity_name == "missing-console"


def test_filter_sessions_accepts_defined_console_without_members(db: Database) -> None:
    _seed_console_filters(db)

    assert session_manager.filter_sessions(db, console_name="console-empty") == []


@pytest.mark.parametrize("manager_name", ("stop_all_sessions", "start_all_sessions"))
def test_batch_lifecycle_passes_console_filter_to_shared_scope(
    monkeypatch: pytest.MonkeyPatch,
    manager_name: str,
) -> None:
    captured: dict[str, Any] = {}

    def capture_filter(_db: object, **kwargs: Any) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(session_manager, "filter_sessions", capture_filter)
    batch_operation = getattr(session_manager, manager_name)

    batch_operation(
        None,
        None,
        console_name=["console-a", "console-b"],
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert captured["console_name"] == ["console-a", "console-b"]


@pytest.mark.parametrize(
    ("command", "manager_name"),
    (("stop", "stop_all_sessions"), ("start", "start_all_sessions"), ("restart", "restart_all_sessions")),
)
def test_console_csv_filter_flows_to_batch_manager(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    manager_name: str,
) -> None:
    captured: dict[str, Any] = {}

    def capture(*_args: object, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(session_manager, manager_name, capture)
    monkeypatch.setattr("agentworks.cli.commands.session.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(
        app,
        ["session", command, "--all", "--console", "console-a,console-b"],
    )

    assert result.exit_code == 0, result.exception
    assert captured["console_name"] == ["console-a", "console-b"]


@pytest.mark.parametrize(
    ("command", "manager_name"),
    (("stop", "stop_session"), ("start", "start_session"), ("restart", "restart_session")),
)
def test_console_filter_requires_batch_mode(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    manager_name: str,
) -> None:
    calls: list[None] = []

    def record_call(*_args: object, **_kwargs: object) -> None:
        calls.append(None)

    monkeypatch.setattr(session_manager, manager_name, record_call)
    monkeypatch.setattr("agentworks.cli.commands.session.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    result = CliRunner().invoke(
        app,
        ["session", command, "session-a", "--console", "console-a"],
    )

    assert result.exit_code != 0
    assert calls == []


def test_legacy_resume_does_not_accept_console_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_manager, "restart_all_sessions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agentworks.cli.commands.session.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    result = CliRunner().invoke(
        app,
        ["session", "resume", "--all", "--yes", "--console", "console-a"],
    )

    assert result.exit_code != 0
