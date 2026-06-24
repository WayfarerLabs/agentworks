"""Tests for EnvEntry."""

from __future__ import annotations

import pytest

from agentworks.env import EnvEntry


def test_plaintext_entry() -> None:
    e = EnvEntry(key="EDITOR", value="vim")
    assert e.value == "vim"
    assert e.secret is None


def test_secret_entry() -> None:
    e = EnvEntry(key="API_KEY", secret="anthropic-api-key")
    assert e.value is None
    assert e.secret == "anthropic-api-key"


def test_neither_value_nor_secret_raises() -> None:
    with pytest.raises(ValueError, match="EDITOR"):
        EnvEntry(key="EDITOR")


def test_both_value_and_secret_raises() -> None:
    with pytest.raises(ValueError, match="API_KEY"):
        EnvEntry(key="API_KEY", value="literal", secret="some-name")


def test_entry_is_frozen() -> None:
    """Entries are hashable / immutable for use as dict values and in sets."""
    e = EnvEntry(key="EDITOR", value="vim")
    with pytest.raises(Exception):  # noqa: B017 - dataclass raises FrozenInstanceError
        e.value = "emacs"  # type: ignore[misc]
