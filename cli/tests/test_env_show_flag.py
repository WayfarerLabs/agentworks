"""CLI-surface tests for ``agw env show``'s ``--resolve`` flag (R9.8).

The former ``--reveal-secrets`` flag was renamed to ``--resolve`` as a breaking
change; the old spelling is gone (not a deprecated alias). These drive the Typer
command (flag parsing), mocking the service layer so no DB / config / VM context
is needed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app

if TYPE_CHECKING:
    from collections.abc import Iterator

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI color codes so option-flag assertions survive colorized
    help. Rich renders a flag like ``--resolve`` as two separately-styled
    dash spans (``ESC[..m-ESC[0mESC[..m-resolve``), so a literal
    ``"--resolve" in output`` check fails whenever the terminal is colorized
    (as CI's is, but this VM's is not). Stripping the codes makes the flag
    contiguous again and the assertion robust to the runner's color mode."""
    return _ANSI_RE.sub("", text)


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
    """``--resolve`` is the flag: it turns resolution on."""
    result = CliRunner().invoke(app, ["env", "show", "--vm", "x", "--resolve"])
    assert result.exit_code == 0, result.output
    assert captured_show_env["reveal_secrets"] is True


def test_reveal_secrets_flag_is_rejected(captured_show_env: dict[str, Any]) -> None:
    """The old ``--reveal-secrets`` spelling was removed (breaking change, R9.8):
    it is now an unknown option, not a silent alias, so a stale script fails
    loudly rather than resolving secrets by surprise."""
    result = CliRunner().invoke(app, ["env", "show", "--vm", "x", "--reveal-secrets"])
    assert result.exit_code != 0
    assert "reveal_secrets" not in captured_show_env  # the command body never ran


def test_default_redacts(captured_show_env: dict[str, Any]) -> None:
    """No flag: secret values stay redacted (resolution off)."""
    result = CliRunner().invoke(app, ["env", "show", "--vm", "x"])
    assert result.exit_code == 0, result.output
    assert captured_show_env["reveal_secrets"] is False


def test_help_shows_resolve() -> None:
    """``--resolve`` is documented in --help; the removed ``--reveal-secrets``
    spelling appears nowhere."""
    result = CliRunner().invoke(app, ["env", "show", "--help"])
    assert result.exit_code == 0, result.output
    plain = _strip_ansi(result.output)
    assert "--resolve" in plain
    assert "--reveal-secrets" not in plain
