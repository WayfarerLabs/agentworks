"""``ResourceKind.model``: the per-kind schema authority.

Decode reads a kind's spec model off its strategy rather than out of a
table in the manifest layer, which is what keeps the switchboard derived.
These are the properties that has to have, asserted over the live
registry rather than over a list somebody keeps.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from agentworks.declared_resource import METADATA_FIELDS, DeclaredResource
from agentworks.errors import ConfigError
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import iter_field_docs

from ._specs import decode, rejection


def _kinds(category: str) -> list[str]:
    return sorted(kind for kind, handler in KIND_REGISTRY.items() if handler.category == category)


def test_the_registry_has_kinds_of_both_categories() -> None:
    """Non-vacuity for the two sweeps below, which would pass on an empty
    registry."""
    assert len(_kinds("declarable")) >= 13
    assert len(_kinds("capability")) >= 4


@pytest.mark.parametrize("kind", _kinds("declarable"))
def test_a_declarable_kinds_model_is_a_row_whose_spec_surface_is_its_own(kind: str) -> None:
    """The three properties every declarable kind's ``model`` has, over
    one sweep of the registry rather than three.

    They are one subject (the kind's model) checked once, and each keeps
    its own assertion so a failure still names which property broke:

    - it is a ``DeclaredResource`` subclass, which is what makes decode's
      switchboard derived rather than a table in the manifest layer;
    - its spec surface offers no envelope field. A kind that re-declares
      ``name`` or ``description`` (``secret`` makes it required,
      ``admin-template`` defaults it) has to carry ``SkipJsonSchema`` on
      the override too, or the field re-enters that kind's spec surface
      and every rendered sample invites an operator to write a key decode
      refuses;
    - that surface is not EMPTY, which is the non-vacuity twin of the
      line above: a model whose fields all vanished would satisfy it
      trivially, and both surfaces derive from this one stream.
    """
    model = KIND_REGISTRY[kind].model  # type: ignore[attr-defined]

    assert isinstance(model, type)
    assert issubclass(model, DeclaredResource)

    streamed = {doc.path[0] for doc in iter_field_docs(model)}
    assert streamed, "an empty spec surface would satisfy the envelope-field check trivially"
    assert not METADATA_FIELDS & streamed
    assert not METADATA_FIELDS & set(model.model_json_schema()["properties"])


@pytest.mark.parametrize("kind", _kinds("capability"))
def test_a_capability_kind_declares_no_model(kind: str) -> None:
    """Optional by CATEGORY, not per kind: a capability kind has no
    declared row at all, so a model on one would mean nothing."""
    assert not hasattr(KIND_REGISTRY[kind], "model")


# -- metadata.expires, which every kind inherits (FR20) ------------------------


@pytest.mark.parametrize(
    "written",
    [
        pytest.param(datetime(2026, 1, 1, tzinfo=UTC), id="rfc3339-timestamp"),
        pytest.param(date(2026, 1, 1), id="bare-date"),
        pytest.param("2026-01-01", id="quoted-date"),
    ],
)
def test_an_expiry_validates_from_every_spelling_a_document_can_produce(written: object) -> None:
    """pyyaml's safe loader yields a ``datetime``, a ``date`` and a ``str``
    for the three ways an operator writes the same moment, and strict mode
    accepts only the first, so the field is one of the base model's
    sanctioned per-field carve-outs."""
    row = decode("apt-package", "tools", {"apt": ["jq"]}, expires=written)

    assert row.expires == datetime(2026, 1, 1, tzinfo=row.expires.tzinfo)


def test_an_expiry_is_modeled_once_for_every_kind() -> None:
    """On the shared row base rather than per kind, so a kind cannot lack
    it and the envelope derives the key from the same place."""
    assert "expires" in METADATA_FIELDS
    for kind in _kinds("declarable"):
        assert "expires" in KIND_REGISTRY[kind].model.model_fields  # type: ignore[attr-defined]


def test_an_expiry_defaults_to_none() -> None:
    assert decode("apt-package", "tools", {"apt": ["jq"]}).expires is None


@pytest.mark.parametrize(
    ("written", "message"),
    [
        pytest.param("soonish", "Input should be a valid datetime", id="malformed-string"),
        # Lax mode reads a bare int as a unix timestamp, so ``expires: 12``
        # would otherwise validate to 1970. The carve-out widens the
        # accepted SPELLINGS, never the accepted types.
        pytest.param(12, "must be a date or an RFC 3339 timestamp", id="bare-number"),
    ],
)
def test_a_malformed_expiry_is_refused(written: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        decode("apt-package", "tools", {"apt": ["jq"]}, expires=written)


def test_a_framework_field_written_in_spec_gets_its_own_answer() -> None:
    """``origin`` and ``declared_at`` belong NOWHERE an operator writes,
    so answering them with "belongs in metadata" would send an operator to
    write ``metadata.origin``, which the envelope refuses as an unknown
    metadata key."""
    assert rejection("apt-package", "tools", {"apt": ["jq"], "origin": "operator-declared"}) == (
        "res.yaml:7: origin is set by the framework and cannot be declared"
    )


def test_an_expiry_is_not_spec_surface() -> None:
    """It is written in ``metadata``, so offering it under ``spec`` would
    be a lie the sample renderer would repeat."""
    assert rejection("apt-package", "tools", {"apt": ["jq"], "expires": "2026-01-01"}) == (
        "res.yaml:7: expires belong(s) in metadata, not in spec"
    )


def test_the_envelope_accepts_the_metadata_keys_the_row_declares() -> None:
    """Derived rather than listed: the two used to be hand-kept lists that
    agreed only by luck."""
    from agentworks.manifests.envelope import _METADATA_KEYS

    assert _METADATA_KEYS == METADATA_FIELDS
