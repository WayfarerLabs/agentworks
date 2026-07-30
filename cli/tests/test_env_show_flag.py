"""CLI-surface tests for ``agw env show``'s ``--resolve`` flag and its
deprecated, hidden ``--reveal-secrets`` alias (R9.8).

These drive the Typer command (flag parsing + deprecation notice), mocking the
service layer so no DB / config / VM context is needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture()
def captured_show_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Mock the service layer so the command runs without DB/config/context,
    capturing the ``reveal_secrets`` value the CLI passes through."""
    captured: dict[str, Any] = {}

    def _fake_show_env(*_args: object, reveal_secrets: bool = False, **_kw: object) -> None:
        captured["reveal_secrets"] = reveal_secrets

    monkeypatch.setattr("agentworks.cli.commands.env.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    monkeypatch.setattr("agentworks.env.show.show_env", _fake_show_env)
    yield captured


def test_resolve_flag_resolves(captured_show_env: dict[str, Any]) -> None:
    """``--resolve`` is the canonical flag: it turns resolution on and emits no
    deprecation notice."""
    result = CliRunner().invoke(app, ["env", "show", "--vm", "x", "--resolve"])
    assert result.exit_code == 0, result.output
    assert captured_show_env["reveal_secrets"] is True
    assert "deprecated" not in result.output


def test_reveal_secrets_alias_still_works_with_deprecation(captured_show_env: dict[str, Any]) -> None:
    """The deprecated ``--reveal-secrets`` alias behaves identically to
    ``--resolve`` but emits a single deprecation notice (R9.8), so existing
    scripts and muscle memory do not hard-break this release."""
    result = CliRunner().invoke(app, ["env", "show", "--vm", "x", "--reveal-secrets"])
    assert result.exit_code == 0, result.output
    assert captured_show_env["reveal_secrets"] is True
    assert "--reveal-secrets is deprecated; use --resolve" in result.output


def test_default_redacts(captured_show_env: dict[str, Any]) -> None:
    """Neither flag: secret values stay redacted (resolution off)."""
    result = CliRunner().invoke(app, ["env", "show", "--vm", "x"])
    assert result.exit_code == 0, result.output
    assert captured_show_env["reveal_secrets"] is False


def test_help_shows_resolve_hides_reveal_secrets() -> None:
    """``--resolve`` is documented in --help; the deprecated alias is hidden so
    it does not clutter help (and, via the same Typer ``hidden`` flag, the
    completion tree)."""
    result = CliRunner().invoke(app, ["env", "show", "--help"])
    assert result.exit_code == 0, result.output
    assert "--resolve" in result.output
    assert "--reveal-secrets" not in result.output
