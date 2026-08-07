"""Tests for ``conformance_error``'s rules, called DIRECTLY.

``tests/plugins/test_plugin_framework.py`` covers the same function through
``register_plugin``, which is the right shape for most rules: what a plugin
author sees is a ``PluginError`` naming their plugin. Two rules cannot be
reached that way, and both were untestable-by-accident rather than by
design:

- the ``name`` check, because ``register_plugin`` rejects the same shape a
  few lines earlier with its own message. Its real caller is the table
  self-test in ``tests/capabilities/test_capability_descriptors.py``, which is the only
  thing that checks a core BUILT-IN's name at all, since built-ins are
  assigned straight into their registries and never pass through
  registration;
- the two config-model rules below, which a plugin fixture can reach but
  which read more clearly stated against the function that owns them.

So this module calls ``conformance_error`` the way the self-test does.
"""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from pydantic import Field

from agentworks.capabilities.conformance import conformance_error
from agentworks.capabilities.descriptor import descriptor_for
from agentworks.schema import (
    AgwModel,
    RefOwner,
    SecretRef,
    extract_references,
    model_is_complete,
    reference_marker_error,
    validation_context,
)
from tests.plugins._fixtures import ConformingSecretBackend, ConformingVMPlatform

VM_PLATFORM = descriptor_for("vm-platform")

OWNER = RefOwner(kind="vm-site", name="lab")


def _impl(config_model: type[AgwModel], *, name: str = "fixture-platform") -> type:
    """A conforming vm-platform whose config model is the one under test.

    Built here rather than declared per case because every one of these
    differs in its config model alone, and ``ConformingVMPlatform`` would
    otherwise generate a tagged model of its own and hide the subject.
    """
    return type(
        "_FixturePlatform",
        (ConformingVMPlatform,),
        {"name": name, "description": "a platform under conformance test", "config_model": config_model},
    )


# -- The name rule, which only the self-test path reaches --------------------


@pytest.mark.parametrize("name", ["", "with/slash"], ids=["empty", "slashed"])
def test_a_capability_name_that_is_not_a_registry_key_is_refused(name: str) -> None:
    """A capability's name IS its registry key, so an empty or
    ``/``-bearing one is unaddressable however well-formed the rest is.

    Reached only here and from the table self-test: ``register_plugin``
    refuses the same shape a few lines earlier, which is why the rule reads
    unused. A core built-in has no registration pass at all, so this is the
    only thing standing between it and an unusable key.

    Asserted against ``secret-backend`` deliberately. On a kind whose
    config is dispatched by a tag, the tag check catches a bad name too and
    a test there passes with this rule deleted, which is how it went
    untested in the first place. ``secret-backend`` is keyed by its map
    key and its models carry no tag, so nothing else can answer.
    """
    backend = type(
        "_FixtureBackend",
        (ConformingSecretBackend,),
        {"name": name, "description": "a backend under conformance test"},
    )
    reason = conformance_error(descriptor_for("secret-backend"), backend)
    assert reason is not None
    assert repr(name) in reason
    assert "'name' class attribute" in reason


# -- The config model has to be buildable ------------------------------------


def test_a_config_model_that_cannot_be_built_is_refused() -> None:
    """A model with an unresolved annotation has no fields, so nothing
    could validate a manifest block against it or read its references. It
    seats without complaint and the failure surfaces later, somewhere else,
    which is the shape of failure this whole check exists to end.

    This is also what keeps the extractor's "an incomplete model
    contributes nothing" branch unreachable in practice, so the claim in
    ``extract.py`` rests on this line holding.
    """

    class Unbuildable(AgwModel):
        name: Literal["fixture-platform"]
        child: NeverDefinedAnywhere | None = None  # type: ignore[name-defined]  # noqa: F821

    assert not model_is_complete(Unbuildable)
    reason = conformance_error(VM_PLATFORM, _impl(Unbuildable))
    assert reason is not None
    assert "cannot be built" in reason


# -- A reference marker has to sit where something can honor it --------------


class MarkedList(AgwModel):
    """A template on a field that holds MANY names.

    Nothing in the layer can honor it, and each consumer fails
    differently, which the test below spells out.
    """

    name: Literal["fixture-platform"]
    tokens: Annotated[list[str], SecretRef(usage="the tokens", default_template="tok-{owner_name}")] = Field(
        default_factory=list
    )


class MarkedBlock(AgwModel):
    """A marker on a field that opens a nested block."""

    credentials: Annotated[MarkedList, SecretRef(usage="the credentials")] | None = None


class NestsAMisplacedMarker(AgwModel):
    """The same mistake one level down, where the filling still reaches."""

    name: Literal["fixture-platform"]
    block: MarkedBlock | None = None


def test_a_marker_on_a_collection_is_refused_at_registration() -> None:
    assert conformance_error(VM_PLATFORM, _impl(MarkedList)) is not None


def test_a_marker_nested_below_the_config_model_is_refused_too() -> None:
    """Validation, emission, and extraction all reach a nested block and a
    union arm, so a marker misplaced there is the same defect one level
    down. A check that stopped at the top-level fields would pass the
    shipped shape of every platform whose secret lives in a
    ``service_principal`` block."""
    reason = conformance_error(VM_PLATFORM, _impl(NestsAMisplacedMarker))
    assert reason is not None
    assert "MarkedBlock.credentials" in reason


def test_the_refused_shape_is_the_one_that_answers_three_different_things() -> None:
    """Why the shape is refused rather than handled: it does not crash, it
    disagrees with itself.

    Left in, an operator who omits ``tokens`` gets a validation failure on
    a key they never wrote, an emitted schema that widens the LIST with a
    null arm, and a graph edge to a secret named after the owner that no
    element ever named. Three answers, none of them right, and no
    traceback anywhere.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        MarkedList.model_validate({"name": "fixture-platform"}, context=validation_context(OWNER))
    assert exc.value.errors()[0]["loc"] == ("tokens",), "the fill wrote a name where a list belongs"

    (edge,) = extract_references(MarkedList, {"name": "fixture-platform"}, OWNER)
    assert edge.name == "tok-lab", "and extraction emitted an edge for a list element that does not exist"


def test_every_marker_a_shipped_model_declares_sits_where_it_can_be_honored() -> None:
    """The invariant over the models registration conformance does NOT
    reach: a declarable kind's spec model has no registration pass, so
    without this the rule would hold for plugin config and nowhere else.
    """
    from agentworks.declared_resource import DeclaredResource
    from agentworks.resources.kind import KIND_REGISTRY

    # ``model`` is the kind's declared-resource row class, read the way
    # ``manifests.decode`` reads it; a capability kind declares none.
    models = [
        (kind, model)
        for kind, strategy in KIND_REGISTRY.items()
        if isinstance(model := getattr(strategy, "model", None), type) and issubclass(model, DeclaredResource)
    ]
    assert models, "no kind exposed a spec model; this test would prove nothing"
    for kind, model in models:
        assert reference_marker_error(model) is None, kind


def test_a_well_placed_marker_is_left_alone() -> None:
    """The element spelling every shipped collection uses, and a plain
    nested block, both pass: the rule refuses a misplaced marker, not a
    collection or a block that has one below it."""

    class WellPlaced(AgwModel):
        name: Literal["fixture-platform"]
        tokens: list[Annotated[str, SecretRef(usage="a token")]] = Field(default_factory=list)
        token: Annotated[str, SecretRef(usage="the token", default_template="tok-{owner_name}")] | None = None

    assert reference_marker_error(WellPlaced) is None
    assert conformance_error(VM_PLATFORM, _impl(WellPlaced)) is None


def test_a_self_referential_model_terminates() -> None:
    """The walk carries the current path, exactly as both walkers in the
    schema package do, so a model reachable from itself does not recurse
    forever."""

    class Recursive(AgwModel):
        name: Literal["fixture-platform"]
        child: Recursive | None = None
        children: list[Recursive] = Field(default_factory=list)

    assert reference_marker_error(Recursive) is None


def test_an_unbuildable_model_reports_no_marker_placement_problem() -> None:
    """It has no fields to judge, and ``model_is_complete`` has already
    refused it with the message that names the real defect. Answering
    twice about one model would only tell the author the wrong thing to
    fix."""

    class Unbuildable(AgwModel):
        child: StillNeverDefined | None = None  # type: ignore[name-defined]  # noqa: F821

    assert reference_marker_error(Unbuildable) is None
