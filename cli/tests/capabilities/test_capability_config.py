"""The core-driven capability config path.

Unit level, against fixture capabilities seated through the real plugin
machinery, so what is exercised is the same registry, descriptor, and
union the consuming resources will use. What each consuming resource does
with the result is its own suite's business; this file pins the core
behavior all four share.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from agentworks.capabilities.config import (
    capability_config_model,
    capability_config_references,
    capability_config_union,
    offered_model,
    tagged_config,
    validate_capability_config,
    validate_own_config,
)
from agentworks.capabilities.descriptor import descriptor_for
from agentworks.capabilities.facets import Facet, facet_config
from agentworks.errors import ConfigError, StateError
from agentworks.plugins import Plugin, seated_plugin
from agentworks.resources.reference import ConfigReference
from agentworks.schema import (
    AgwModel,
    FramedConfigError,
    RefOwner,
    SecretRef,
    render_validation_error,
    validation_context,
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


def _arm_names() -> set[str]:
    """The names of the arms the assembled vm-platform union carries."""
    arms = capability_config_union("vm-platform").model_fields["root"].annotation
    return {arm.__name__ for arm in getattr(arms, "__args__", ())}


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


def _validate(blob: dict[str, object], name: str = "fixture-platform") -> object:
    return validate_capability_config(kind="vm-platform", name=name, blob=blob, owner=OWNER, location=WHERE)


# -- Resolution ---------------------------------------------------------------


def test_a_seated_capability_answers_with_the_model_it_declares(seated: None) -> None:
    assert capability_config_model("vm-platform", "fixture-platform") is FixtureConfig


def test_an_unseated_name_answers_none_rather_than_raising(seated: None) -> None:
    """The dangling capability edge is what reports an unknown name, as a
    hard finalize miss; reporting it twice in two vocabularies would be
    worse than once."""
    assert capability_config_model("vm-platform", "nope") is None
    assert _validate({}, name="nope") is None
    assert capability_config_references(kind="vm-platform", name="nope", blob={}, owner=OWNER) == ()


def test_a_single_config_capability_answers_at_every_facet(seated: None) -> None:
    """The ordinary case: one config shared by all of a capability's
    operations, so no facet distinction exists to refuse."""
    for facet in (None, *Facet):
        assert offered_model(FixturePlatform, facet) is FixtureConfig


def test_a_per_facet_capability_answers_per_facet_and_refuses_the_rest() -> None:
    """The wave-4 shape, spelled here so the base's override point is
    proven to work before anything ships one."""

    class PerFacet(ConformingVMPlatform):
        name: ClassVar[str] = "per-facet"
        description: ClassVar[str] = "offers config at one facet only"
        config_model: ClassVar[type[AgwModel]] = FixtureConfig

        @classmethod
        def config_for(cls, facet: Facet | None = None) -> type[BaseModel]:
            return facet_config({Facet.SESSION: OtherConfig}, facet, capability=cls.name)

    assert offered_model(PerFacet, Facet.SESSION) is OtherConfig
    with pytest.raises(StateError, match="per-facet"):
        offered_model(PerFacet, Facet.VM)


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
    with pytest.raises(ConfigError) as caught:
        _validate({"region": 8})

    assert str(caught.value) == "sites.yaml:12: vm-site/lab.region: must be a string"


def test_an_unknown_field_names_the_fields_that_are_valid(seated: None) -> None:
    with pytest.raises(ConfigError, match="unknown field; expected one of:"):
        _validate({"region": "eu", "regions": "eu"})


def test_the_raised_error_carries_its_own_framing(seated: None) -> None:
    """So the finalize pass's origin-suffix wrapper leaves it alone rather
    than framing it a second time."""
    with pytest.raises(FramedConfigError):
        _validate({"region": 8})


def test_the_arm_is_selected_by_the_capability_name_not_by_the_blob(seated: None) -> None:
    """Validating under one name against another arm's fields fails, which
    is what proves the tag actually dispatches."""
    with pytest.raises(ConfigError, match="unknown field"):
        _validate({"token": "t"}, name="other-platform")


# -- The interim tagged synthesis ---------------------------------------------


def test_the_synthesis_produces_the_table_decode_will_hand_over_directly() -> None:
    assert tagged_config("lima", {"vm_host": "h"}, discriminator="name", owner=OWNER) == {
        "name": "lima",
        "vm_host": "h",
    }


def test_a_name_key_inside_the_config_block_is_a_hard_error() -> None:
    """Neither silent resolution is acceptable: letting the tag win
    discards a key the operator wrote, and letting the blob win would let
    the config block select a DIFFERENT capability's schema than the
    naming field beside it."""
    with pytest.raises(ConfigError, match="names the capability"):
        tagged_config("lima", {"name": "proxmox"}, discriminator="name", owner=OWNER)


def test_a_config_block_naming_another_capability_cannot_smuggle_an_arm(seated: None) -> None:
    with pytest.raises(ConfigError, match="names the capability"):
        _validate({"name": "other-platform", "region": "eu"})


# -- Extraction ---------------------------------------------------------------


def test_references_are_read_off_the_raw_blob_through_the_model(seated: None) -> None:
    refs = capability_config_references(
        kind="vm-platform", name="fixture-platform", blob={"token": "mine"}, owner=OWNER
    )

    assert refs == (ConfigReference(kind="secret", name="mine", usage="the fixture token"),)


def test_an_omitted_reference_falls_back_to_the_owner_template(seated: None) -> None:
    refs = capability_config_references(kind="vm-platform", name="fixture-platform", blob={}, owner=OWNER)

    assert refs == (ConfigReference(kind="secret", name="fixture-lab", usage="the fixture token"),)


@pytest.mark.parametrize("blob", [{}, {"token": None}, {"token": 8}, {"token": ""}, {"region": 8, "token": []}])
def test_extraction_never_raises_on_a_blob_validation_would_reject(seated: None, blob: dict[str, object]) -> None:
    """The graph is built before anything is validated, so a malformed
    blob has to contribute no edges rather than sink the walk."""
    capability_config_references(kind="vm-platform", name="fixture-platform", blob=blob, owner=OWNER)


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


def test_a_map_keyed_kind_has_no_union_to_assemble() -> None:
    """secret-backend dispatches on the ``backend_mappings`` key, so a tag
    inside the value would say the same thing twice and could disagree."""
    with pytest.raises(StateError, match="map key"):
        capability_config_union("secret-backend")


def test_an_unregistered_name_is_rejected_by_the_union_naming_what_is_registered(seated: None) -> None:
    """Unreachable through :func:`validate_capability_config`, whose
    registry lookup answers first, and pinned anyway: it is the message
    emitted schema and step 2.5's decode swap will both rely on, and the
    proof that the assembled union really does dispatch on the tag."""
    union = capability_config_union("vm-platform")
    with pytest.raises(PydanticValidationError) as caught:
        union.model_validate({"name": "nope"}, context=validation_context(OWNER))

    (line,) = render_validation_error(caught.value, model_cls=union, owner=OWNER)
    assert line.startswith("vm-site/lab: unknown name 'nope'; registered: ")
    assert "'fixture-platform'" in line


# -- Construction-time validation ---------------------------------------------


def test_validating_against_an_impls_own_config_needs_no_registry() -> None:
    """The construct path: the class is in hand, so there is no name to
    look up and no arm to select."""
    validated = validate_own_config(FixturePlatform, {"region": "eu"}, owner=OWNER)

    assert isinstance(validated, FixtureConfig)
    assert validated.token == "fixture-lab"


def test_construct_time_validation_raises_the_same_framed_error() -> None:
    with pytest.raises(FramedConfigError, match="vm-site/lab.region: must be a string"):
        validate_own_config(FixturePlatform, {"region": 8}, owner=OWNER)
