"""Tests for the ``--agent`` and ``--admin`` filters on
``session stop`` / ``session restart``.

Pins the new flags' plumbing (CLI → manager), the mode-mutex
(``--admin`` and ``--agent`` are mutually exclusive), and the
precondition that the batch filters (``--vm``, ``--workspace``,
``--agent``, ``--admin``) require one of the batch flags
(``--all`` / ``--all-stopped``).
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.sessions import manager as session_manager


def _capture_kwargs(captured: dict[str, Any]):
    """Build a stub that records the keyword arguments it received."""

    def _stub(*_args: object, **kwargs: Any) -> None:
        captured.update(kwargs)

    return _stub


# ---------------------------------------------------------------------------
# session stop --agent
# ---------------------------------------------------------------------------


def test_session_stop_agent_filter_flows_to_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session stop --all --agent a1`` must pass
    ``agent_name='a1'`` through to ``stop_all_sessions``."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(session_manager, "stop_all_sessions", _capture_kwargs(captured))
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(app, ["session", "stop", "--all", "--agent", "a1"])
    assert result.exit_code == 0, result.output
    assert captured.get("agent_name") == "a1"
    # The other filter kwargs default to None so we don't break the
    # AND-compose semantics with stray filter args.
    assert captured.get("vm_name") is None
    assert captured.get("workspace_name") is None


def test_session_stop_agent_filter_composes_with_other_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--agent`` AND-composes with ``--vm`` and ``--workspace``."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(session_manager, "stop_all_sessions", _capture_kwargs(captured))
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(
        app,
        ["session", "stop", "--all", "--vm", "vm1", "--workspace", "ws1", "--agent", "a1"],
    )
    assert result.exit_code == 0, result.output
    assert captured["vm_name"] == "vm1"
    assert captured["workspace_name"] == "ws1"
    assert captured["agent_name"] == "a1"


def test_session_stop_agent_without_all_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session stop --agent a1`` without ``--all`` must error -- the
    filter flags are batch-only, same as ``--vm`` / ``--workspace``."""
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(app, ["session", "stop", "--agent", "a1"])
    assert result.exit_code != 0
    assert "--agent" in result.output


# ---------------------------------------------------------------------------
# session restart --agent
# ---------------------------------------------------------------------------


def test_session_restart_agent_filter_flows_to_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session restart --all-stopped --agent a1`` must pass
    ``agent_name='a1'`` through to ``restart_all_sessions``."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(session_manager, "restart_all_sessions", _capture_kwargs(captured))
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(
        app, ["session", "restart", "--all-stopped", "--agent", "a1"],
    )
    assert result.exit_code == 0, result.output
    assert captured.get("agent_name") == "a1"
    assert captured.get("vm_name") is None
    assert captured.get("workspace_name") is None
    assert captured.get("include_running") is False


def test_session_restart_agent_without_batch_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session restart --agent a1`` without ``--all`` or
    ``--all-stopped`` must error."""
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(app, ["session", "restart", "--agent", "a1"])
    assert result.exit_code != 0
    assert "--agent" in result.output


# ---------------------------------------------------------------------------
# Manager-layer plumbing into filter_sessions
# ---------------------------------------------------------------------------


def test_stop_all_sessions_passes_agent_name_to_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop_all_sessions(agent_name='a1')`` must pass that through
    to ``filter_sessions``; the keyword needs to traverse the manager
    layer to reach the database query."""
    captured: dict[str, Any] = {}

    def _capture_filter(db: object, **kwargs: Any) -> list[object]:
        captured.update(kwargs)
        return []  # empty session list -> early return, no SSH work

    monkeypatch.setattr(session_manager, "filter_sessions", _capture_filter)

    session_manager.stop_all_sessions(  # type: ignore[arg-type]
        db=None, config=None, agent_name="a1",
    )
    assert captured.get("agent_name") == "a1"


def test_restart_all_sessions_passes_agent_name_to_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``restart_all_sessions(agent_name='a1')`` must pass that
    through to ``filter_sessions``."""
    captured: dict[str, Any] = {}

    def _capture_filter(db: object, **kwargs: Any) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(session_manager, "filter_sessions", _capture_filter)

    session_manager.restart_all_sessions(  # type: ignore[arg-type]
        db=None, config=None, agent_name="a1",
    )
    assert captured.get("agent_name") == "a1"


# ---------------------------------------------------------------------------
# session stop --admin
# ---------------------------------------------------------------------------


def test_session_stop_admin_filter_flows_to_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session stop --all --admin`` must pass ``admin_only=True``
    through to ``stop_all_sessions``."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(session_manager, "stop_all_sessions", _capture_kwargs(captured))
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(app, ["session", "stop", "--all", "--admin"])
    assert result.exit_code == 0, result.output
    assert captured.get("admin_only") is True
    assert captured.get("agent_name") is None


def test_session_stop_admin_and_agent_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--admin`` and ``--agent`` are mutually exclusive on
    ``session stop``, same as on ``session list``."""
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(
        app, ["session", "stop", "--all", "--admin", "--agent", "a1"],
    )
    assert result.exit_code != 0
    assert "--admin" in result.output
    assert "--agent" in result.output


def test_session_stop_admin_without_all_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session stop --admin`` without ``--all`` must error -- the
    filter flags (including ``--admin``) are batch-only."""
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(app, ["session", "stop", "--admin"])
    assert result.exit_code != 0
    assert "--admin" in result.output


# ---------------------------------------------------------------------------
# session restart --admin
# ---------------------------------------------------------------------------


def test_session_restart_admin_filter_flows_to_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session restart --all-stopped --admin`` must pass
    ``admin_only=True`` through to ``restart_all_sessions``."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(session_manager, "restart_all_sessions", _capture_kwargs(captured))
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(
        app, ["session", "restart", "--all-stopped", "--admin"],
    )
    assert result.exit_code == 0, result.output
    assert captured.get("admin_only") is True
    assert captured.get("agent_name") is None


def test_session_restart_admin_and_agent_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--admin`` and ``--agent`` are mutually exclusive on
    ``session restart`` too."""
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())

    result = CliRunner().invoke(
        app,
        ["session", "restart", "--all-stopped", "--admin", "--agent", "a1"],
    )
    assert result.exit_code != 0
    assert "--admin" in result.output
    assert "--agent" in result.output


# ---------------------------------------------------------------------------
# Manager-layer plumbing for admin_only
# ---------------------------------------------------------------------------


def test_stop_all_sessions_passes_admin_only_to_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop_all_sessions(admin_only=True)`` must reach
    ``filter_sessions`` (and therefore the DB query)."""
    captured: dict[str, Any] = {}

    def _capture_filter(db: object, **kwargs: Any) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(session_manager, "filter_sessions", _capture_filter)

    session_manager.stop_all_sessions(  # type: ignore[arg-type]
        db=None, config=None, admin_only=True,
    )
    assert captured.get("admin_only") is True


def test_restart_all_sessions_passes_admin_only_to_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``restart_all_sessions(admin_only=True)`` must reach
    ``filter_sessions``."""
    captured: dict[str, Any] = {}

    def _capture_filter(db: object, **kwargs: Any) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(session_manager, "filter_sessions", _capture_filter)

    session_manager.restart_all_sessions(  # type: ignore[arg-type]
        db=None, config=None, admin_only=True,
    )
    assert captured.get("admin_only") is True
