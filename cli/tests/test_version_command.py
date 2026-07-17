"""`agw version`: prints the installed CLI version, with a graceful
fallback when the package metadata is unavailable (issue #179)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.cli.commands import version as version_mod


def test_version_command_prints_resolved_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version_mod, "resolve_version", lambda: "1.2.3")
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "1.2.3"


def test_resolve_version_reads_distribution_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve_version reads the metadata for the distribution name, not
    the import package name."""
    seen: dict[str, str] = {}

    def _fake_version(dist: str) -> str:
        seen["dist"] = dist
        return "9.9.9"

    monkeypatch.setattr("importlib.metadata.version", _fake_version)
    assert version_mod.resolve_version() == "9.9.9"
    assert seen["dist"] == "agentworks-cli"


def test_resolve_version_falls_back_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib.metadata import PackageNotFoundError

    def _boom(dist: str) -> str:
        raise PackageNotFoundError(dist)

    monkeypatch.setattr("importlib.metadata.version", _boom)
    assert version_mod.resolve_version() == "unknown"
