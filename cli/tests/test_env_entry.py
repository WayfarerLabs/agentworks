"""Tests for EnvEntry."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentworks.env import EnvEntry


def test_plaintext_entry() -> None:
    e = EnvEntry(value="vim")
    assert e.value == "vim"
    assert e.secret is None


def test_secret_entry() -> None:
    e = EnvEntry(secret="anthropic-api-key")
    assert e.value is None
    assert e.secret == "anthropic-api-key"


def test_a_bare_string_is_the_plaintext_form() -> None:
    """The spelling operators write for all but a handful of entries:
    ``FOO: a value``, folded into the object form before validation."""
    assert EnvEntry.model_validate("vim") == EnvEntry(value="vim")


def test_neither_value_nor_secret_raises() -> None:
    with pytest.raises(ValueError, match="must set exactly one of value or secret"):
        EnvEntry()


def test_both_value_and_secret_raises() -> None:
    with pytest.raises(ValueError, match="cannot set both value and secret"):
        EnvEntry(value="literal", secret="some-name")


def test_entry_is_frozen() -> None:
    """Entries are immutable for use as dict values."""
    e = EnvEntry(value="vim")
    with pytest.raises(ValidationError):
        e.value = "emacs"  # type: ignore[misc]


def test_an_unknown_key_is_refused() -> None:
    """The ``key`` field is gone: it duplicated the table key it sat
    under, and nothing ever enforced that the two agreed. Under
    ``extra="forbid"`` a leftover ``key=`` is loud rather than silently
    accepted."""
    with pytest.raises(ValidationError):
        EnvEntry(key="EDITOR", value="vim")  # type: ignore[call-arg]


def test_both_spellings_validate_against_the_emitted_schema() -> None:
    """The before-validator is invisible to ``model_json_schema``, so the
    two spellings are declared by hand three lines from it. This is what
    keeps a schema-aware editor from flagging every plaintext entry."""
    emitted = EnvEntry.model_json_schema()
    shapes = emitted["anyOf"]

    assert shapes[0] == {"type": "string"}
    assert set(shapes[1]["properties"]) == {"value", "secret"}
