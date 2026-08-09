"""The secret spec model.

The kind with the one undiscriminated union in the framework's own
vocabulary, so this is where the collapsed union message and the
opt-out's own steer are pinned. It is also one of the two kinds that
check an operator-written NAME at load, and the only one whose cap is not
the freeform bound.
"""

from __future__ import annotations

from agentworks.naming import MAX_SECRET_NAME_LENGTH
from agentworks.secrets.base import SecretDecl

from ._specs import WHERE, decode, rejection


def test_every_field_round_trips() -> None:
    spec = {"hint": "generate at npmjs.com", "backend_mappings": {"env-var": "NPM_TOKEN"}}

    assert decode("secret", "npm-token", dict(spec), description="npm registry token") == SecretDecl(
        name="npm-token",
        description="npm registry token",
        declared_at=WHERE,
        hint="generate at npmjs.com",
        backend_mappings={"env-var": "NPM_TOKEN"},
    )


def test_raw_mapping_carrier_preserves_legacy_shapes_before_registry_validation() -> None:
    """Decode is lossless; source-specific registry validation rejects legacy rows."""

    row = decode(
        "secret",
        "npm-token",
        {
            "backend_mappings": {
                "env-var": "NPM_TOKEN",
                "onepassword": {"account": "a", "reference": "r"},
                "prompt": False,
            }
        },
        description="d",
    )

    assert row.backend_mappings == {
        "env-var": "NPM_TOKEN",
        "onepassword": {"account": "a", "reference": "r"},
        "prompt": False,
    }


# -- What an operator reads when it is wrong ----------------------------------


def test_a_missing_description_says_it_is_required() -> None:
    """The one kind that requires it: it is the operator-facing prompt
    text, so a secret without one cannot be entered by hand."""
    assert rejection("secret", "npm-token", {}) == "res.yaml:7: secret/npm-token.description: is required"


def test_an_empty_description_is_refused_too() -> None:
    """``secrets/prompt.py`` renders it into "Secret '<name>': <text>", so
    an empty one asks the operator for nothing in particular. The decoder
    this replaces checked presence and emptiness in one condition; the
    model says them as the two problems they are."""
    assert rejection("secret", "npm-token", {}, description="") == (
        "res.yaml:7: secret/npm-token.description: must not be empty"
    )


def test_a_numeric_mapping_value_reaches_the_raw_carrier() -> None:
    secret = decode("secret", "npm-token", {"backend_mappings": {"env-var": 3}}, description="d")
    assert secret.backend_mappings == {"env-var": 3}


def test_true_reaches_the_raw_carrier_without_becoming_false_opt_out() -> None:
    secret = decode("secret", "npm-token", {"backend_mappings": {"env-var": True}}, description="d")
    assert secret.backend_mappings == {"env-var": True}


def test_a_non_table_backend_mappings_says_table() -> None:
    assert rejection("secret", "npm-token", {"backend_mappings": "env-var"}, description="d") == (
        "res.yaml:7: secret/npm-token.backend_mappings: must be a table"
    )


# The unknown-key refusal, which for this kind is also the statement that
# ``backend_mappings`` and ``hint`` are the whole spec surface, is pinned in
# ``test_loader_and_envelope.py::test_an_unknown_spec_key_is_a_located_error``
# against the same kind and the same expected-field list, reached through
# ``load_manifests`` over a real file so it carries the location too. The
# sibling kinds keep their own copy of this test because each names a
# DIFFERENT field list.


# -- The name cap, which applies only to what an operator wrote ---------------


def test_a_non_conforming_declared_name_is_refused() -> None:
    assert rejection("secret", "Bad_Name", {}, description="d").startswith(
        "res.yaml:7: secret/Bad_Name: invalid name 'Bad_Name'."
    )


def test_the_secret_cap_is_the_larger_one() -> None:
    """Secrets are never derived into a Linux username, so a name that
    would be over-length for a VM is fine here."""
    long_name = "a" * (MAX_SECRET_NAME_LENGTH - 1)

    assert decode("secret", long_name, {}, description="d").name == long_name
    assert rejection("secret", "a" * (MAX_SECRET_NAME_LENGTH + 1), {}, description="d").endswith(
        f"max {MAX_SECRET_NAME_LENGTH})"
    )


def test_a_synthesized_row_keeps_a_non_conforming_name() -> None:
    """The cap is applied AT DECODE, not on the field, and deliberately:
    an auto-declared secret carries whatever name the reference that
    summoned it used, and issue #279's shipped decision is that those stay
    tolerant so a non-conforming reference still resolves."""
    assert SecretDecl(name="GITHUB_TOKEN", description="d").name == "GITHUB_TOKEN"
