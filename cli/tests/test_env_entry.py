"""Tests for EnvEntry."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agentworks.env import EnvEntry, PlaintextEnvEntry, SecretEnvEntry
from agentworks.schema import (
    RefOwner,
    extract_references,
    filled_defaults,
    reference_marker_error,
    structural_union_error,
)

OWNER = RefOwner(kind="fixture", name="demo")


def test_plaintext_entry() -> None:
    e = EnvEntry({"value": "vim"})
    assert e.value == "vim"
    assert e.secret is None


def test_secret_entry() -> None:
    e = EnvEntry({"secret": "anthropic-api-key"})
    assert e.value is None
    assert e.secret == "anthropic-api-key"


def test_a_bare_string_is_the_plaintext_form() -> None:
    """The spelling operators write for all but a handful of entries:
    ``FOO: a value``, folded into the object form before validation."""
    assert EnvEntry.model_validate("vim") == EnvEntry({"value": "vim"})


def test_neither_value_nor_secret_raises() -> None:
    with pytest.raises(ValueError, match=r"(?s)PlaintextEnvEntry\.value.*SecretEnvEntry\.secret"):
        EnvEntry({})


def test_both_value_and_secret_raises() -> None:
    with pytest.raises(ValueError, match=r"(?s)PlaintextEnvEntry\.secret.*SecretEnvEntry\.value"):
        EnvEntry({"value": "literal", "secret": "some-name"})


def test_entry_is_frozen() -> None:
    """Entries are immutable for use as dict values."""
    e = EnvEntry({"value": "vim"})
    with pytest.raises(ValidationError):
        e.value = "emacs"  # type: ignore[misc]


def test_an_unknown_key_is_refused() -> None:
    """The ``key`` field is gone: it duplicated the table key it sat
    under, and nothing ever enforced that the two agreed. Under
    ``extra="forbid"`` a leftover ``key=`` is loud rather than silently
    accepted."""
    with pytest.raises(ValidationError):
        EnvEntry({"key": "EDITOR", "value": "vim"})


def test_all_spellings_are_exposed_by_a_structural_one_of() -> None:
    """A shorthand is invisible to ``model_json_schema``, which would emit
    the table form alone and let a schema-aware editor flag every
    plaintext entry an operator writes. The arm comes from the declaration
    (:class:`~agentworks.schema.ScalarShorthand`) rather than from a hook
    written here, which is what put the same fact into the field
    documentation too."""
    emitted = EnvEntry.model_json_schema()
    assert "anyOf" not in emitted
    assert len(emitted["oneOf"]) == 2
    plaintext = emitted["$defs"]["PlaintextEnvEntry"]
    secret = emitted["$defs"]["SecretEnvEntry"]
    assert plaintext["anyOf"][0] == {"type": "string"}
    assert plaintext["anyOf"][1]["required"] == ["value"]
    assert set(plaintext["anyOf"][1]["properties"]) == {"value"}
    assert secret["required"] == ["secret"]
    assert set(secret["properties"]) == {"secret"}


def test_the_runtime_wrapper_contains_one_closed_arm() -> None:
    assert isinstance(EnvEntry({"value": "vim"}).root, PlaintextEnvEntry)
    assert isinstance(EnvEntry({"secret": "token"}).root, SecretEnvEntry)


def test_the_secret_arm_is_structurally_reachable() -> None:
    assert structural_union_error(EnvEntry) is None
    assert reference_marker_error(EnvEntry) is None


def test_the_two_spellings_are_one_declaration() -> None:
    """The anti-drift pin. The scalar the loader takes, the arm the schema
    offers, and the type every human surface renders are the same authored
    fact, so no consumer can be updated without the others."""
    assert EnvEntry.scalar_shorthand is not None
    assert EnvEntry.scalar_shorthand.annotation is str
    assert EnvEntry.scalar_shorthand.field == "value"
    assert EnvEntry.model_validate("vim") == EnvEntry({"value": "vim"})


@pytest.mark.parametrize(
    "retired",
    [
        {"value": "vim", "secret": None},
        {"value": None, "secret": "editor-token"},
    ],
)
def test_retired_null_companions_are_rejected_by_runtime_and_schema(retired: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EnvEntry.model_validate(retired)

    assert list(Draft202012Validator(EnvEntry.model_json_schema()).iter_errors(retired))


@pytest.mark.parametrize(
    ("entry", "names"),
    [
        ({"value": "vim"}, []),
        ({"secret": "editor-token"}, ["editor-token"]),
    ],
)
def test_structural_walks_preserve_canonical_env_entries(entry: dict[str, str], names: list[str]) -> None:
    assert filled_defaults(EnvEntry, entry, OWNER) == entry
    assert [edge.name for edge in extract_references(EnvEntry, entry)] == names


@pytest.mark.parametrize(
    "malformed",
    [
        {"value": "vim", "unknown": None},
        {"value": None, "secret": None},
    ],
)
def test_unknown_or_ambiguous_shapes_remain_rejected(malformed: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EnvEntry.model_validate(malformed)
