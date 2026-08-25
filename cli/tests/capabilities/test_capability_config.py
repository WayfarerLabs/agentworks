"""The core-driven capability config path.

Unit level, against fixture capabilities seated through the real plugin
machinery, so what is exercised is the same registry, descriptor, and
union the consuming resources will use. What each consuming resource does
with the result is its own suite's business; this file pins the core
behavior all four share.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, cast, get_args

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from agentworks.capabilities.config import (
    capability_config_model,
    capability_config_references,
    capability_config_union,
    offered_model,
    selected_name,
    validate_capability_config,
    validate_own_config,
)
from agentworks.capabilities.descriptor import descriptor_for
from agentworks.errors import ConfigError
from agentworks.plugins import Plugin, seated_plugin
from agentworks.resources.reference import ConfigReference
from agentworks.schema import (
    AgwModel,
    RefOwner,
    SecretRef,
    config_error_from,
)
from agentworks.source_location import SourceLocation
from tests.plugins._fixtures import ConformingVMPlatform

if TYPE_CHECKING:
    from collections.abc import Iterator

OWNER = RefOwner(kind="vm-site", name="lab")
WHERE = SourceLocation(file=Path("sites.yaml"), line=12)


class FixtureConfig(AgwModel):
    """A config exercising a tag, a plain field, and a templated secret."""

    name: Literal["fixture-platform"]
    region: str
    token: Annotated[str, SecretRef(usage="the fixture token", default_template="fixture-{owner_name}")]


class OtherConfig(AgwModel):
    """A second arm, so the union has something to choose between."""

    name: Literal["other-platform"]
    region: str | None = None


class FixturePlatform(ConformingVMPlatform):
    name: ClassVar[str] = "fixture-platform"
    description: ClassVar[str] = "a fixture platform with real config"
    config_model: ClassVar[type[AgwModel]] = FixtureConfig


class OtherPlatform(ConformingVMPlatform):
    name: ClassVar[str] = "other-platform"
    description: ClassVar[str] = "a second fixture platform"
    config_model: ClassVar[type[AgwModel]] = OtherConfig


class ThirdConfig(AgwModel):
    """A third arm, seated mid-test to prove the union follows the
    registry."""

    name: Literal["third-platform"]


class ThirdPlatform(ConformingVMPlatform):
    name: ClassVar[str] = "third-platform"
    description: ClassVar[str] = "seated mid-test"
    config_model: ClassVar[type[AgwModel]] = ThirdConfig


class SoleConfig(AgwModel):
    """The only arm a one-arm union has."""

    name: Literal["sole-platform"]
    region: str | None = None


class SolePlatform(ConformingVMPlatform):
    name: ClassVar[str] = "sole-platform"
    description: ClassVar[str] = "the only registered platform"
    config_model: ClassVar[type[AgwModel]] = SoleConfig


def _arm_names(kind: str = "vm-platform") -> set[str]:
    """The names of the arms the assembled ``kind`` union carries.

    ``Union[(X,)]`` is ``X``, so a one-arm union has already collapsed to
    the bare arm by the time it reaches the annotation, and has no
    ``__args__`` to read. Reading them off regardless would answer "no
    arms" for a shape the framework really does produce, which is a
    silently wrong answer rather than a failure, so the collapsed case is
    handled here instead of assumed away.
    """
    annotation = capability_config_union(kind).model_fields["root"].annotation
    arms = get_args(annotation)
    if arms:
        return {arm.__name__ for arm in arms}
    assert annotation is not None, "the union wrapper always annotates its root"
    return {annotation.__name__}


FIXTURE_NAMES = frozenset({"fixture-platform", "other-platform"})


@pytest.fixture
def seated() -> Iterator[None]:
    """The live vm-platform registry holding EXACTLY the two fixtures.

    Seated through the real plugin machinery, so the descriptor, the
    registry, and the assembled union are the shipped ones. The shipped
    platforms are then set aside for the duration so the assertions can
    state which arms the union has rather than which it contains, and so a
    new built-in platform never silently changes what this file proves.
    ``seated_plugin`` restores the registry on the way out, including on
    failure.
    """
    with seated_plugin(Plugin(name="fixtures", capabilities={"vm-platform": (FixturePlatform, OtherPlatform)})):
        registry = descriptor_for("vm-platform").registry()
        fixtures = {name: impl for name, impl in registry.items() if name in FIXTURE_NAMES}
        registry.clear()
        registry.update(fixtures)
        yield


@pytest.fixture
def sole_seated() -> Iterator[None]:
    """The live vm-platform registry holding EXACTLY one implementation.

    The same real plugin machinery as :func:`seated`, so what the one-arm
    assertions see is the union ``capability_config_union`` really
    assembles off the real registry rather than one hand-written to look
    like the collapsed shape.
    """
    with seated_plugin(Plugin(name="sole", capabilities={"vm-platform": (SolePlatform,)})):
        registry = descriptor_for("vm-platform").registry()
        sole = {name: impl for name, impl in registry.items() if name == SolePlatform.name}
        registry.clear()
        registry.update(sole)
        yield


def _tagged(blob: dict[str, object], name: str = "fixture-platform") -> dict[str, object]:
    """``blob`` in the shape a host row carries it: one tagged table whose
    ``name`` key is the selector."""
    return {"name": name, **blob}


def _validate(blob: dict[str, object], name: str = "fixture-platform") -> object:
    return validate_capability_config(kind="vm-platform", config=_tagged(blob, name), owner=OWNER, location=WHERE)


def _refs(blob: dict[str, object], name: str = "fixture-platform") -> tuple[ConfigReference, ...]:
    return capability_config_references(kind="vm-platform", config=_tagged(blob, name), owner=OWNER)


# -- Resolution ---------------------------------------------------------------


def test_a_seated_capability_answers_with_the_model_it_declares(seated: None) -> None:
    assert capability_config_model("vm-platform", "fixture-platform") is FixtureConfig


def test_an_unseated_name_answers_none_rather_than_raising(seated: None) -> None:
    """The dangling capability edge is what reports an unknown name, as a
    hard finalize miss; reporting it twice in two vocabularies would be
    worse than once."""
    assert capability_config_model("vm-platform", "nope") is None
    assert _validate({}, name="nope") is None
    assert _refs({}, name="nope") == ()


def test_the_base_hook_answers_with_the_declared_model() -> None:
    """The ordinary case, and why an author spells nothing beyond
    ``config_model``: one config shared by all of a capability's
    operations, which is every capability shipped today."""
    assert offered_model(FixturePlatform) is FixtureConfig


def test_the_offered_model_is_read_through_the_hook_not_off_the_declaration() -> None:
    """``config_for`` is the override point, so a capability that answers
    with something other than its ``config_model`` is honored wherever the
    framework asks for a config.

    Nothing overrides it today, and it is pinned because that hook is what
    makes wave 4's per-facet offering an additive registration rather than
    a framework change: reading ``config_model`` directly here would work
    identically for every shipped capability and silently ignore the first
    one that needs the seam.
    """

    class Overriding(ConformingVMPlatform):
        name: ClassVar[str] = "overriding-platform"
        description: ClassVar[str] = "answers with a model other than the one it declares"
        config_model: ClassVar[type[AgwModel]] = FixtureConfig

        @classmethod
        def config_for(cls) -> type[BaseModel]:
            return OtherConfig

    assert offered_model(Overriding) is OtherConfig


# -- No capability code runs --------------------------------------------------


class TripwireConfig(AgwModel):
    """The tripwire's own config: a templated secret, so extraction has
    something to find, and a tag of its own so it registers."""

    name: Literal["tripwire-platform"]
    region: str
    token: Annotated[str, SecretRef(usage="the fixture token", default_template="fixture-{owner_name}")]


class TripwirePlatform(ConformingVMPlatform):
    """A capability carrying the retired invoked contract's two methods.

    The base no longer declares either, so these are just methods nothing
    should call. If the core ever reaches for a capability to validate a
    blob or to derive what it references, this is where it shows up: the
    whole point of the flip is that a plugin's code cannot run in the
    finalize pass, and "we deleted the base methods" is not the same
    promise as "nothing calls them".
    """

    name: ClassVar[str] = "tripwire-platform"
    description: ClassVar[str] = "raises if the core invokes it"
    config_model: ClassVar[type[AgwModel]] = TripwireConfig

    @classmethod
    def validate(cls, owner: str, config: object) -> None:
        raise AssertionError("the core invoked a capability to validate its config")

    @classmethod
    def dependencies(cls, owner: str, config: object) -> tuple[object, ...]:
        raise AssertionError("the core invoked a capability to derive its references")


def test_neither_validation_nor_extraction_invokes_the_capability() -> None:
    with seated_plugin(Plugin(name="tripwire", capabilities={"vm-platform": (TripwirePlatform,)})):
        tagged = {"name": "tripwire-platform", "region": "eu"}
        validated = validate_capability_config(kind="vm-platform", config=tagged, owner=OWNER)
        refs = capability_config_references(kind="vm-platform", config=tagged, owner=OWNER)
        instance = TripwirePlatform("lab", {"region": "eu"})

    assert isinstance(validated, TripwireConfig)
    assert [ref.name for ref in refs] == ["fixture-lab"]
    assert isinstance(instance.config, TripwireConfig)


# -- Validation ---------------------------------------------------------------


def test_a_valid_blob_returns_the_arms_typed_instance(seated: None) -> None:
    validated = _validate({"region": "eu", "token": "t"})

    assert isinstance(validated, FixtureConfig)
    assert validated.region == "eu"


def test_an_omitted_templated_secret_resolves_from_the_owner(seated: None) -> None:
    """The validated instance carries the same name the extractor derived
    for the graph, which is what makes a consumer able to read the field
    instead of re-deriving a default."""
    validated = _validate({"region": "eu"})

    assert isinstance(validated, FixtureConfig)
    assert validated.token == "fixture-lab"


def test_a_bad_field_is_owner_framed_and_located(seated: None) -> None:
    """The whole message, asserted whole, because the error bridge OWNS the
    framing: it carries the location and the owner itself, so the finalize
    pass adds none of its own and a caller that wrapped this with a
    location would make the operator read the file and line twice.
    """
    with pytest.raises(ConfigError) as caught:
        _validate({"region": 8})

    assert str(caught.value) == "sites.yaml:12: vm-site/lab.region: must be a string"


def test_an_unknown_field_names_the_fields_that_are_valid(seated: None) -> None:
    with pytest.raises(ConfigError, match="unknown field; expected one of:"):
        _validate({"region": "eu", "regions": "eu"})


def test_every_rejection_points_at_the_field_reference(seated: None) -> None:
    """The capability-config counterpart of the declarable-kind path's
    sample hint: an operator staring at a block they got wrong is told the
    one command that renders what the block accepts. A capability's config
    lives inside someone else's document, so `explain KIND/NAME` is
    the surface, not `sample`.
    """
    with pytest.raises(ConfigError) as caught:
        _validate({"region": 8})

    assert caught.value.hint == (
        "`agw resource explain vm-platform/fixture-platform` prints this implementation's fields"
    )


def test_the_arm_is_selected_by_the_capability_name_not_by_the_blob(seated: None) -> None:
    """Validating under one name against another arm's fields fails, which
    is what proves the tag actually dispatches."""
    with pytest.raises(ConfigError, match="unknown field"):
        _validate({"token": "t"}, name="other-platform")


# -- Which implementation the config selects ----------------------------------


def test_a_tagged_kind_reads_its_selector_off_the_table() -> None:
    """One source, so there is nothing to disagree with. The synthesis
    this replaces had to reject a ``name`` key inside the blob, because a
    blob naming one capability beside a field naming another would have
    validated against a schema the caller did not think it was using."""
    assert selected_name("vm-platform", {"name": "lima", "vm_host": "h"}, None) == "lima"


@pytest.mark.parametrize("config", [{}, {"name": 7}, {"name": None}, "lima", None])
def test_a_missing_or_non_string_tag_names_no_implementation(config: object) -> None:
    """Tolerant rather than throwing: the dangling capability edge is what
    reports an unnamed capability, as a hard finalize miss (R9.2)."""
    assert selected_name("vm-platform", config, None) is None


def test_a_secret_backend_source_config_takes_its_selector_from_the_tag() -> None:
    assert selected_name("secret-backend", {"name": "env-var"}, None) == "env-var"


def test_a_secret_backend_source_config_does_not_use_a_caller_selector() -> None:
    assert selected_name("secret-backend", {"name": "prompt"}, "env-var") == "prompt"


# -- Extraction ---------------------------------------------------------------


def test_references_are_read_off_the_raw_blob_through_the_model(seated: None) -> None:
    refs = _refs({"token": "mine"})

    assert refs == (ConfigReference(kind="secret", name="mine", usage="the fixture token"),)


def test_an_omitted_reference_falls_back_to_the_owner_template(seated: None) -> None:
    refs = _refs({})

    assert refs == (ConfigReference(kind="secret", name="fixture-lab", usage="the fixture token"),)


@pytest.mark.parametrize("blob", [{}, {"token": None}, {"token": 8}, {"token": ""}, {"region": 8, "token": []}])
def test_extraction_never_raises_on_a_blob_validation_would_reject(seated: None, blob: dict[str, object]) -> None:
    """The graph is built before anything is validated, so a malformed
    blob has to contribute no edges rather than sink the walk."""
    _refs(blob)


# -- The union ----------------------------------------------------------------


def test_the_union_has_one_arm_per_registered_capability(seated: None) -> None:
    assert _arm_names() == {"FixtureConfig", "OtherConfig"}


def test_seating_a_capability_changes_the_union_with_no_invalidation_call(seated: None) -> None:
    """The cache is keyed on the registry's CONTENTS, so a stale union is
    impossible by construction rather than by every mutator remembering to
    invalidate."""
    before = _arm_names()
    with seated_plugin(Plugin(name="fixtures-2", capabilities={"vm-platform": (ThirdPlatform,)})):
        during = _arm_names()
    after = _arm_names()

    assert "ThirdConfig" not in before
    assert "ThirdConfig" in during
    assert after == before


def test_the_union_is_rebuilt_only_when_its_arms_change(seated: None) -> None:
    assert capability_config_union("vm-platform") is capability_config_union("vm-platform")


def test_a_capability_swapping_its_model_gets_a_fresh_union(seated: None) -> None:
    """The case a registry-mapping key would miss: the same name, the same
    class object, a different model. Unreachable in production, where
    ``config_model`` is a ClassVar set at class definition, and pinned
    anyway because the whole reason to key the cache rather than
    invalidate it is that the alternative fails SILENTLY, by validating a
    capability against a model it no longer offers."""
    before = capability_config_union("vm-platform")
    original = FixturePlatform.config_model
    try:
        FixturePlatform.config_model = ThirdConfig
        assert capability_config_union("vm-platform") is not before
        assert _arm_names() == {"ThirdConfig", "OtherConfig"}, "the arms follow the models, not the registry mapping"
    finally:
        FixturePlatform.config_model = original
    assert capability_config_union("vm-platform") is before, "and back again, from the cache"


def test_a_one_arm_union_collapses_to_its_only_arm(sole_seated: None) -> None:
    """``Union[(X,)]`` is ``X``, so the union the builder returns for a kind
    with one registered implementation IS the bare arm: there is no union
    object left to read arms off.

    Pinned against the real builder over the real registry, because the
    collapse is a property of what ``capability_config_union`` assembles,
    not of a union written by hand to resemble it. No shipped kind is
    down to one implementation today, but every kind is one disabled
    plugin away from it, and the shape reaches operators through emitted
    schema and the field reference.
    """
    assert _arm_names() == {"SoleConfig"}


def test_a_collapsed_union_still_dispatches_on_the_tag(sole_seated: None) -> None:
    """What the collapse must NOT cost: the tagged-union behavior.

    A bare model would accept the config and reject a wrong tag too, so
    the distinguishing evidence is the MESSAGE. An unknown name still
    reports as the union's "unknown name; registered: ..." rather than as
    a literal mismatch on a field the operator did write, which is what
    makes the collapsed form the shape a second implementation grows into
    rather than a different one.
    """
    union = capability_config_union("vm-platform")
    validated = validate_capability_config(
        kind="vm-platform", config={"name": "sole-platform", "region": "eu"}, owner=OWNER
    )

    assert isinstance(validated, SoleConfig), "the collapsed union still unwraps to the arm it selected"

    with pytest.raises(PydanticValidationError) as caught:
        union.model_validate({"name": "nope"})

    assert str(config_error_from(caught.value, model_cls=union, owner=OWNER)) == (
        "vm-site/lab: unknown name 'nope'; registered: 'sole-platform'"
    )


def test_secret_backend_source_config_has_a_tagged_union() -> None:
    union = capability_config_union("secret-backend")
    validated = cast("Any", union.model_validate({"name": "env-var"}))
    assert validated.root.name == "env-var"


def test_an_unregistered_name_is_rejected_by_the_union_naming_what_is_registered(seated: None) -> None:
    """Unreachable through :func:`validate_capability_config`, whose
    registry lookup answers first, and pinned anyway: it is the message
    emitted schema and step 2.5's decode swap will both rely on, and the
    proof that the assembled union really does dispatch on the tag."""
    union = capability_config_union("vm-platform")
    with pytest.raises(PydanticValidationError) as caught:
        union.model_validate({"name": "nope"})

    message = str(config_error_from(caught.value, model_cls=union, owner=OWNER))
    assert message.startswith("vm-site/lab: unknown name 'nope'; registered: ")
    assert "'fixture-platform'" in message


# -- Construction-time validation ---------------------------------------------


def test_validating_against_an_impls_own_config_needs_no_registry() -> None:
    """The construct path: the class is in hand, so there is no name to
    look up and no arm to select."""
    validated = validate_own_config(FixturePlatform, {"region": "eu"}, owner=OWNER)

    assert isinstance(validated, FixtureConfig)
    assert validated.token == "fixture-lab"


def test_construct_time_validation_raises_the_same_framed_error() -> None:
    with pytest.raises(ConfigError, match="vm-site/lab.region: must be a string"):
        validate_own_config(FixturePlatform, {"region": 8}, owner=OWNER)
