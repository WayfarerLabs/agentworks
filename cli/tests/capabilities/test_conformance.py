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

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

import pytest
from pydantic import Discriminator, Field

from agentworks.capabilities.conformance import conformance_error
from agentworks.capabilities.descriptor import descriptor_for
from agentworks.manifests.spec_model import metadata_model, spec_model
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


class _MisplacedMarker(AgwModel):
    """One arm, carrying the misplaced marker every case below is looking
    for: on a field that holds MANY values, where nothing can honor it."""

    name: Literal["misplaced"]
    tokens: Annotated[list[str], SecretRef(usage="a token")] = Field(default_factory=list)


class _Innocent(AgwModel):
    """The other arm, so each union below is a union."""

    name: Literal["innocent"]


class _ViaItemArms(AgwModel):
    """The marker sits in an arm of a COLLECTION's tagged union."""

    name: Literal["fixture-platform"]
    platforms: list[Annotated[_MisplacedMarker | _Innocent, Discriminator("name")]] = Field(default_factory=list)


class _ViaUnionModel(AgwModel):
    """The marker sits in the block of an untagged scalar-or-block union."""

    name: Literal["fixture-platform"]
    thing: str | _MisplacedMarker | None = None


class _ViaItemUnionModel(AgwModel):
    """The marker sits in the block one ELEMENT of a collection may be."""

    name: Literal["fixture-platform"]
    things: dict[str, str | _MisplacedMarker] = Field(default_factory=dict)


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
    reach: a declarable kind's document has no registration pass, so
    without this the rule would hold for plugin config and nowhere else.

    Both blocks of every declarable kind's document, which is the same
    enumeration ``tests/manifests/test_accepted_type_parity.py`` calls the
    shipped surface. The row class alone is not enough: a kind hosting a
    capability has its naming field re-annotated to the capability's config
    union by ``spec_model``, and ``metadata_model`` is a second block an
    author can put a marker in. A first-party model is in-repo, so a test
    IS its gate; nothing else runs this over one.
    """
    from agentworks.manifests.spec_model import declarable_kinds

    blocks = [(kind, block, builder(kind)) for kind in declarable_kinds() for block, builder in _DOCUMENT_BLOCKS]
    assert blocks, "no kind exposed a model; this test would prove nothing"

    faults = [f"{kind} {block}: {reason}" for kind, block, model in blocks if (reason := reference_marker_error(model))]
    assert not faults, "\n".join(faults)


#: The two blocks of a kind document, by the function that builds each.
_DOCUMENT_BLOCKS = (("metadata", metadata_model), ("spec", spec_model))


@pytest.mark.parametrize(
    "config_model",
    [
        pytest.param(_ViaItemArms, id="a collection element's union arm"),
        pytest.param(_ViaUnionModel, id="an untagged union's block"),
        pytest.param(_ViaItemUnionModel, id="a collection element's untagged union block"),
    ],
)
def test_a_misplaced_marker_is_found_wherever_a_walker_could_reach_it(config_model: type[AgwModel]) -> None:
    """The check has to descend exactly as far as extraction does.

    Each shape below is one reference extraction walks into and this rule
    did not, so a marker inside it was accepted at registration and then
    read by nothing: the field filled with the marker's rendered default,
    the emitted schema hung the marker off the whole collection, and the
    graph got an edge for an element that does not exist. The three
    answers the rule exists to prevent, reached by a route it did not
    cover.
    """
    assert reference_marker_error(config_model) is not None


def test_a_marker_no_walker_could_ever_read_is_refused() -> None:
    """Fail closed on a shape the classifier does not recognize.

    Extraction reads a marker in exactly two positions, the field's own
    value and one element of a collection, so a marker anywhere else is
    read by nothing at all while the document validates perfectly well.
    That is the worst failure this layer has, because the dependency graph
    is built from the extracted edges BEFORE validation: the secret is
    never gated, never resolved, and never reported.

    Refusing here is what keeps the set of collection shapes the
    classifier recognizes from doubling as a set of silent failures. The
    next unrecognized shape someone writes is loud, without anyone having
    to predict which one it will be.
    """

    class NestedCollection(AgwModel):
        name: Literal["fixture-platform"]
        tokens: dict[str, list[Annotated[str, SecretRef(usage="a token")]]] = Field(default_factory=dict)

    reason = reference_marker_error(NestedCollection)
    assert reason is not None
    assert "cannot classify" in reason
    assert conformance_error(VM_PLATFORM, _impl(NestedCollection)) is not None

    # The half that makes the refusal necessary rather than merely tidy.
    assert extract_references(NestedCollection, {"tokens": {"k": ["named"]}}, OWNER) == ()


# -- Validation must not accept what no walker reads -------------------------
#
# The invariant ``reference_marker_error`` enforces: every model pydantic
# can select is a model some walker reaches, or no reference marker hides
# inside it. The cases below CONSTRUCT violating shapes rather than only
# pinning the ones already found, and each first proves its bypass is real
# (the blob validates carrying a secret name; extraction produces no edge
# for it), so a case that stops violating the premise fails loudly instead
# of testing nothing.


class _MarkedArm(AgwModel):
    """A block naming a secret, hidden below each stranded position."""

    secret: Annotated[str, SecretRef(usage="the hidden secret")]


class _UnmarkedArm(AgwModel):
    """The other arm, marking nothing."""

    other: str


class _HoldsMarkedArm(AgwModel):
    """A marker-free surface whose nested block is marked, so the hiding
    is transitive rather than one level down."""

    inner: _MarkedArm


HIDDEN = "hidden-secret"


def _carries(value: object, name: str) -> bool:
    """Whether ``name`` appears anywhere in a dumped validated value."""
    if isinstance(value, str):
        return value == name
    if isinstance(value, Mapping):
        return any(_carries(item, name) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_carries(item, name) for item in value)
    return False


class _TwoModelUnion(AgwModel):
    cred: _MarkedArm | _UnmarkedArm


class _TwoModelUnionElement(AgwModel):
    creds: dict[str, _MarkedArm | _UnmarkedArm] = Field(default_factory=dict)


class _TableShadowedBlock(AgwModel):
    mapping: dict[str, int] | _MarkedArm | None = None


class _TableShadowedElement(AgwModel):
    mappings: dict[str, dict[str, int] | _MarkedArm] = Field(default_factory=dict)


class _NestedCollectionOfModels(AgwModel):
    rows: dict[str, list[_MarkedArm]] = Field(default_factory=dict)


class _FixedTupleMember(AgwModel):
    pair: tuple[str, _MarkedArm] | None = None


class _TransitivelyHidden(AgwModel):
    thing: _HoldsMarkedArm | _UnmarkedArm | None = None


@pytest.mark.parametrize(
    ("config_model", "blob"),
    [
        pytest.param(_TwoModelUnion, {"cred": {"secret": HIDDEN}}, id="an arm of a two-model untagged union"),
        pytest.param(
            _TwoModelUnionElement,
            {"creds": {"k": {"secret": HIDDEN}}},
            id="the same union held by a collection's elements",
        ),
        pytest.param(
            _TableShadowedBlock,
            {"mapping": {"secret": HIDDEN}},
            id="a sole model arm a table member shadows",
        ),
        pytest.param(
            _TableShadowedElement,
            {"mappings": {"k": {"secret": HIDDEN}}},
            id="the shadowed arm one element down",
        ),
        pytest.param(
            _NestedCollectionOfModels,
            {"rows": {"k": [{"secret": HIDDEN}]}},
            id="a model below a nested collection",
        ),
        pytest.param(_FixedTupleMember, {"pair": ("x", {"secret": HIDDEN})}, id="a fixed-length tuple member"),
        pytest.param(
            _TransitivelyHidden,
            {"thing": {"inner": {"secret": HIDDEN}}},
            id="a marker two models below the stranded arm",
        ),
    ],
)
def test_a_marker_validation_accepts_and_no_walker_reads_is_refused(
    config_model: type[AgwModel], blob: dict[str, object]
) -> None:
    """The gating-bypass class, closed as one rule rather than per shape.

    Each shape strands a model: pydantic selects it happily, and no
    walker descends into it, so a secret the operator names inside one is
    never gated, never resolved, and never reported while the document
    looks correct throughout. The dependency graph is built from the
    extracted edges BEFORE validation, which is what makes the miss a
    bypass rather than a cosmetic gap. The rule is a subtraction (models
    validation offers minus models the walkers reach, marker-free
    remainder required), so the next stranded position an author can
    spell is refused without being listed here first.
    """
    validated = config_model.model_validate(blob, context=validation_context(OWNER))
    assert _carries(validated.model_dump(), HIDDEN), "premise: the secret name must survive validation"

    extracted = {ref.name for ref in extract_references(config_model, blob, OWNER)}
    assert HIDDEN not in extracted, "premise gone: extraction reaches this shape now, so move it to the walked set"

    assert reference_marker_error(config_model) is not None


def test_the_fixture_unions_no_walker_selects_an_arm_of_are_refused_too() -> None:
    """The two walker fixtures built on unaddressable unions are the same
    class: a marker hides behind a union no document can select an arm of
    (no discriminator at all, and a non-string tag). Extraction's suites
    pin that both stay total and edgeless; this pins that neither could
    ever register."""
    from tests.schema._fixture_models import NumericallyTaggedSite, UndiscriminatedSite

    assert reference_marker_error(UndiscriminatedSite) is not None
    assert reference_marker_error(NumericallyTaggedSite) is not None


def test_a_stranded_model_with_nothing_marked_inside_is_left_alone() -> None:
    """The refusal is about the MARKER, not the shape: a two-model
    untagged union that hides nothing references nothing, so there is no
    edge to lose and no reason an author cannot write one (pydantic
    disambiguates it at validation just fine)."""

    class _AnotherUnmarked(AgwModel):
        flag: bool = False

    class HarmlessUnion(AgwModel):
        name: Literal["fixture-platform"]
        cred: _UnmarkedArm | _AnotherUnmarked | None = None

    assert reference_marker_error(HarmlessUnion) is None
    assert conformance_error(VM_PLATFORM, _impl(HarmlessUnion)) is None


def test_a_default_template_on_an_element_marker_is_refused() -> None:
    """``fields.py`` and ``_shape.py`` have stated this refusal as fact in
    prose; this is the enforcement. Nothing renders such a template: the
    fill writes a rendered name into the field the template defaults, an
    omitted collection has no element to write into, and extraction
    renders no per-element default for the same reason, so the authored
    promise would be read by nobody. The premises are asserted so the
    refusal cannot outlive its reason.
    """

    class TemplatedElements(AgwModel):
        name: Literal["fixture-platform"]
        tokens: list[Annotated[str, SecretRef(usage="a token", default_template="tok-{owner_name}")]] = Field(
            default_factory=list
        )

    validated = TemplatedElements.model_validate({"name": "fixture-platform"}, context=validation_context(OWNER))
    assert validated.tokens == [], "premise: the fill writes nothing for an omitted collection"
    assert extract_references(TemplatedElements, {"name": "fixture-platform"}, OWNER) == (), (
        "premise: extraction renders no per-element default"
    )

    reason = reference_marker_error(TemplatedElements)
    assert reason is not None
    assert "default template" in reason
    assert conformance_error(VM_PLATFORM, _impl(TemplatedElements)) is not None


def test_an_unmarked_shape_the_classifier_does_not_recognize_is_left_alone() -> None:
    """The refusal is about the MARKER, not about the shape.

    A nested collection with nothing marked inside it references nothing,
    so there is no edge to lose and no reason an author cannot write one.
    """

    class NestedButUnmarked(AgwModel):
        name: Literal["fixture-platform"]
        rows: dict[str, list[str]] = Field(default_factory=dict)

    assert reference_marker_error(NestedButUnmarked) is None
    assert conformance_error(VM_PLATFORM, _impl(NestedButUnmarked)) is None


def test_an_abstract_collection_spelling_is_recognized_rather_than_refused() -> None:
    """The other side of the fail-closed rule: what the classifier DOES
    read must not be refused. ``Sequence[X]`` is ``list[X]`` to pydantic
    and to both walkers, so an author who reaches for the ABC gets the
    edges rather than an error."""

    class AbstractlySpelled(AgwModel):
        name: Literal["fixture-platform"]
        tokens: Sequence[Annotated[str, SecretRef(usage="a token")]] = ()

    assert reference_marker_error(AbstractlySpelled) is None
    assert conformance_error(VM_PLATFORM, _impl(AbstractlySpelled)) is None
    assert extract_references(AbstractlySpelled, {"tokens": ["named"]}, OWNER)[0].name == "named"


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
