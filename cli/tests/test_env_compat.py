"""Tests for the AW_-prefix env-var migration helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import env_compat

if TYPE_CHECKING:
    import pytest


def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the env vars under test and reset the warning cache."""
    monkeypatch.delenv("AW_FOO", raising=False)
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("AW_BAR", raising=False)
    monkeypatch.delenv("BAR", raising=False)
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
    # The warning must tell the operator both what is deprecated AND
    # what to migrate to.
    assert "FOO" in combined
    assert "AW_FOO" in combined
    assert "future release" in combined
    # Warning fires exactly once per legacy name per process.
    assert combined.count("deprecated") == 1


def test_two_different_legacy_names_each_warn_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cache is keyed per legacy name, not a single boolean."""
    _reset(monkeypatch)
    monkeypatch.setenv("FOO", "legacy-foo")
    monkeypatch.setenv("BAR", "legacy-bar")

    env_compat.read_env_with_legacy("AW_FOO", "FOO")
    env_compat.read_env_with_legacy("AW_BAR", "BAR")
    # Repeats should not warn again for either name.
    env_compat.read_env_with_legacy("AW_FOO", "FOO")
    env_compat.read_env_with_legacy("AW_BAR", "BAR")

    out = capsys.readouterr()
    combined = out.out + out.err
    assert combined.count("deprecated") == 2


def test_new_only_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When only the new name is set, no warning or info note fires."""
    _reset(monkeypatch)
    monkeypatch.setenv("AW_FOO", "new-value")

    env_compat.read_env_with_legacy("AW_FOO", "FOO")

    out = capsys.readouterr()
    combined = out.out + out.err
    assert "deprecated" not in combined
    assert "ignored" not in combined


def test_both_set_warns_legacy_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When both names are set, the new wins but the operator gets a
    one-time-per-process note about the legacy var sitting unused."""
    _reset(monkeypatch)
    monkeypatch.setenv("AW_FOO", "new-value")
    monkeypatch.setenv("FOO", "legacy-value")

    result = env_compat.read_env_with_legacy("AW_FOO", "FOO")
    # Repeats should not re-warn.
    env_compat.read_env_with_legacy("AW_FOO", "FOO")

    assert result == "new-value"
    out = capsys.readouterr()
    combined = out.out + out.err
    # No deprecation warning since the new name is the source.
    assert "deprecated" not in combined
    # But the operator IS told the legacy var is sitting unused.
    assert combined.count("ignored") == 1
    assert "FOO" in combined
    assert "AW_FOO" in combined
