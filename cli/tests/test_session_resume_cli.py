"""CLI parity coverage for canonical session resume and its 0.13.0 alias."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.errors import StateError
from agentworks.sessions import manager as session_manager


@pytest.fixture
def command_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    monkeypatch.setattr(
        session_manager,
        "resume_session",
        lambda *args, **kwargs: calls.append(("single", kwargs)),
    )
    monkeypatch.setattr(
        session_manager,
        "resume_all_sessions",
        lambda *args, **kwargs: calls.append(("batch", kwargs)),
    )
    return calls


@pytest.mark.parametrize(
    "arguments, expected_kind",
    [
        (["coding", "--force", "--yes"], "single"),
        (["--all-stopped", "--vm", "vm1", "--workspace", "ws1", "--agent", "agent1"], "batch"),
    ],
)
def test_resume_and_restart_dispatch_identically(
    arguments: list[str],
    expected_kind: str,
    command_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    canonical = CliRunner().invoke(app, ["session", "resume", *arguments])
    assert canonical.exit_code == 0, canonical.output
    canonical_call = command_calls.pop()

    alias = CliRunner().invoke(app, ["session", "restart", *arguments])
    assert alias.exit_code == 0, alias.output
    alias_call = command_calls.pop()

    assert canonical_call == alias_call
    assert canonical_call[0] == expected_kind
    assert "deprecated" not in canonical.output.lower()
    assert (
        alias.output.count(
            "'agw session restart' is deprecated; use 'agw session resume'. It will be removed in 0.14.0."
        )
        == 1
    )


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["coding", "--all"],
        ["--all", "--all-stopped"],
        ["--agent", "agent1"],
    ],
)
def test_resume_and_restart_validation_errors_match(arguments: list[str]) -> None:
    canonical = CliRunner().invoke(app, ["session", "resume", *arguments])
    alias = CliRunner().invoke(app, ["session", "restart", *arguments])

    assert canonical.exit_code == alias.exit_code != 0
    warning = "'agw session restart' is deprecated; use 'agw session resume'. It will be removed in 0.14.0."
    assert warning not in canonical.output
    assert alias.output.count(warning) == 1
    assert canonical.exception is not None and alias.exception is not None
    assert str(canonical.exception) == str(alias.exception)


@pytest.mark.parametrize(
    ("arguments", "manager_name"),
    [
        (["coding"], "resume_session"),
        (["--all-stopped"], "resume_all_sessions"),
    ],
)
def test_resume_and_restart_lifecycle_errors_match_except_alias_warning(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    manager_name: str,
) -> None:
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    def fail(*args: object, **kwargs: object) -> None:
        raise StateError("lifecycle failed", hint="repair the session")

    monkeypatch.setattr(session_manager, manager_name, fail)
    canonical = CliRunner().invoke(app, ["session", "resume", *arguments])
    alias = CliRunner().invoke(app, ["session", "restart", *arguments])

    assert canonical.exit_code == alias.exit_code != 0
    assert isinstance(canonical.exception, StateError)
    assert isinstance(alias.exception, StateError)
    assert str(canonical.exception) == str(alias.exception) == "lifecycle failed"
    warning = "'agw session restart' is deprecated; use 'agw session resume'. It will be removed in 0.14.0."
    assert warning not in canonical.output
    assert alias.output.count(warning) == 1
    assert alias.output.replace(f"Warning: {warning}\n", "") == canonical.output


def test_restart_warning_is_suppressible(command_calls: list[tuple[str, dict[str, Any]]]) -> None:
    result = CliRunner().invoke(app, ["--no-deprecations", "session", "restart", "coding"])

    assert result.exit_code == 0, result.output
    assert "deprecated" not in result.output.lower()
    assert command_calls == [("single", {"name": "coding", "force": False, "yes": False})]
