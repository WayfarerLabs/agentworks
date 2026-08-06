"""``ResourceKind.model``: the per-kind schema authority.

Decode reads a kind's spec model off its strategy rather than out of a
table in the manifest layer, which is what keeps the switchboard derived.
These are the properties that has to have, asserted over the live
registry rather than over a list somebody keeps.
"""

from __future__ import annotations

import pytest

from agentworks.declared_resource import METADATA_FIELDS, DeclaredResource
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import iter_field_docs

#: The kinds whose decoder has not been absorbed into a model yet.
#: INTERIM: step 2.5 migrates the kinds one at a time so the suite stays
#: green after each, and this set empties as it goes. It goes with
#: ``decode._DECODERS``.
_NOT_YET_MODELED = {
    "admin-template",
    "agent-template",
    "git-credential",
    "named-console-template",
    "secret",
    "session-template",
    "vm-site",
    "vm-template",
}


def _kinds(category: str) -> list[str]:
    return sorted(kind for kind, handler in KIND_REGISTRY.items() if handler.category == category)


def _modeled_kinds() -> list[str]:
    return [kind for kind in _kinds("declarable") if kind not in _NOT_YET_MODELED]


def test_the_registry_has_kinds_of_both_categories() -> None:
    """Non-vacuity for the two sweeps below, which would pass on an empty
    registry."""
    assert len(_kinds("declarable")) >= 13
    assert len(_kinds("capability")) >= 4


@pytest.mark.parametrize("kind", _kinds("declarable"))
def test_a_declarable_kind_declares_its_row_as_its_model(kind: str) -> None:
    if kind in _NOT_YET_MODELED:
        pytest.skip("decoder not absorbed yet (step 2.5, in flight)")
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
