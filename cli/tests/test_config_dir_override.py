"""Tests for the AW_CONFIG_DIR env override."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.config import _resolve_config_dir


def test_default_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AW_CONFIG_DIR", raising=False)
    assert _resolve_config_dir() == Path.home() / ".config" / "agentworks"


def test_env_override_absolute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AW_CONFIG_DIR", str(tmp_path))
    assert _resolve_config_dir() == tmp_path


def test_env_override_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_CONFIG_DIR", "~/scratch/agentworks-a")
    assert _resolve_config_dir() == Path.home() / "scratch" / "agentworks-a"


def test_empty_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty string should behave as unset -- otherwise Path("") collapses to
    # the cwd and callers would silently write state into random directories.
    monkeypatch.setenv("AW_CONFIG_DIR", "")
    assert _resolve_config_dir() == Path.home() / ".config" / "agentworks"


def test_whitespace_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Whitespace-only would otherwise become a real relative-path override,
    # writing state under `./   /`. Guard against the shell-typo footgun.
    monkeypatch.setenv("AW_CONFIG_DIR", "   ")
    assert _resolve_config_dir() == Path.home() / ".config" / "agentworks"
