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


def _modeled_kinds() -> list[str]:
    """Every declarable kind, because every one of them declares a model.
    Kept as its own name so the sweeps below read as what they check."""
    return _kinds("declarable")


def test_the_registry_has_kinds_of_both_categories() -> None:
    """Non-vacuity for the two sweeps below, which would pass on an empty
    registry."""
    assert len(_kinds("declarable")) >= 13
    assert len(_kinds("capability")) >= 4


@pytest.mark.parametrize("kind", _kinds("declarable"))
def test_a_declarable_kind_declares_its_row_as_its_model(kind: str) -> None:
    model = KIND_REGISTRY[kind].model  # type: ignore[attr-defined]

    assert isinstance(model, type)
    assert issubclass(model, DeclaredResource)


@pytest.mark.parametrize("kind", _kinds("capability"))
def test_a_capability_kind_declares_no_model(kind: str) -> None:
    """Optional by CATEGORY, not per kind: a capability kind has no
    declared row at all, so a model on one would mean nothing."""
    assert not hasattr(KIND_REGISTRY[kind], "model")


@pytest.mark.parametrize("kind", _modeled_kinds())
def test_no_kinds_spec_surface_offers_an_envelope_field(kind: str) -> None:
    """A kind that re-declares ``name`` or ``description`` (``secret``
    makes it required, ``admin-template`` defaults it) has to carry
    ``SkipJsonSchema`` on the override too. Without it the field re-enters
    that kind's spec surface, and every rendered sample would invite an
    operator to write a key decode refuses."""
    model = KIND_REGISTRY[kind].model  # type: ignore[attr-defined]
    emitted = set(model.model_json_schema()["properties"])
    streamed = {doc.path[0] for doc in iter_field_docs(model)}

    assert not METADATA_FIELDS & emitted
    assert not METADATA_FIELDS & streamed


@pytest.mark.parametrize("kind", _modeled_kinds())
def test_a_kinds_spec_surface_is_not_empty(kind: str) -> None:
    """Non-vacuity for the test above: a model whose fields all vanished
    would satisfy it trivially."""
    model = KIND_REGISTRY[kind].model  # type: ignore[attr-defined]

    assert list(iter_field_docs(model))


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
    for kind in _modeled_kinds():
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
