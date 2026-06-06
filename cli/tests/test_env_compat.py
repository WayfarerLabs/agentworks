"""Tests for the AW_-prefix env-var migration helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import env_compat

if TYPE_CHECKING:
    import pytest


def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear deletes the env vars under test and resets the warning cache."""
    monkeypatch.delenv("AW_FOO", raising=False)
    monkeypatch.delenv("FOO", raising=False)
    env_compat.reset_warning_cache()


def test_returns_new_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    monkeypatch.setenv("AW_FOO", "new-value")
    assert env_compat.read_env_with_legacy("AW_FOO", "FOO") == "new-value"


def test_returns_legacy_when_only_legacy_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    monkeypatch.setenv("FOO", "legacy-value")
    assert env_compat.read_env_with_legacy("AW_FOO", "FOO") == "legacy-value"


def test_new_wins_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    monkeypatch.setenv("AW_FOO", "new-value")
    monkeypatch.setenv("FOO", "legacy-value")
    assert env_compat.read_env_with_legacy("AW_FOO", "FOO") == "new-value"


def test_returns_none_when_neither_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    assert env_compat.read_env_with_legacy("AW_FOO", "FOO") is None


def test_legacy_emits_warning_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset(monkeypatch)
    monkeypatch.setenv("FOO", "legacy-value")

    env_compat.read_env_with_legacy("AW_FOO", "FOO")
    env_compat.read_env_with_legacy("AW_FOO", "FOO")

    out = capsys.readouterr()
    combined = out.out + out.err
    assert combined.count("FOO") >= 1
    # Warning fires exactly once per legacy name per process.
    assert combined.count("deprecated") == 1


def test_new_path_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset(monkeypatch)
    monkeypatch.setenv("AW_FOO", "new-value")
    monkeypatch.setenv("FOO", "legacy-value")

    env_compat.read_env_with_legacy("AW_FOO", "FOO")

    out = capsys.readouterr()
    assert "deprecated" not in (out.out + out.err)
