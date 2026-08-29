"""FR12: a session template's capability config validates MERGED, at finalize.

Every assertion here needs a harness integration with a REQUIRED config
field, because that is the whole difference: a required field is what a
child's declared blob can legitimately lack and its lineage supply. No
shipped integration has one (and until this step none COULD: the base
refused it at class definition, since validating each declared blob would
have failed such a child at load), so the surface is a fixture.

That the interim guard is GONE needs no assertion of its own: ``_NeedyConfig``
below declares a required field, so if the guard came back this module would
not import and every test here would error. A test restating what the fixture
literally sets up (``config_model is _NeedyConfig``, ``command`` is required)
stood here and could not fail for any change to production code.

The provenance channel is here too rather than beside the error bridge:
its only producer is this merge, and a per-key attribution is only
meaningful over a blob more than one template wrote.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, cast

import pytest

from agentworks.errors import ConfigError
from agentworks.plugins import Plugin, capability_adapters, seated_plugin
from agentworks.resources import Origin, Registry
from agentworks.resources.inheritance import LayerSource, LayerSourceKind
from agentworks.schema import AgwModel, CapabilityBlock, RefOwner
from agentworks.sessions.template import SessionTemplate, _capability_provenance
from agentworks.source_location import SourceLocation
from agentworks.value_provenance import longest_prefix_value
from tests.plugins._fixtures import ConformingHarnessIntegration

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from pydantic import BaseModel
    from pydantic import ValidationError as PydanticValidationError

    from agentworks.value_provenance import ProvenancePath

PLUGIN = "effective-config-fixture"


class _NeedyConfig(AgwModel):
    """A config whose ``command`` is required and whose ``timeout`` is
    typed, so a lineage can complete the first and get the second wrong."""

    name: Literal["needy"]
    command: str
    timeout: int = 30


class _NeedyIntegration(ConformingHarnessIntegration):
    name: ClassVar[str] = "needy"
    description: ClassVar[str] = "a harness integration with a required config field"
    config_model: ClassVar[type[AgwModel]] = _NeedyConfig


class _NestedOptions(AgwModel):
    retry_count: int
    labels: list[str]


class _NestedConfig(AgwModel):
    name: Literal["nested"]
    options: _NestedOptions


class _NestedIntegration(ConformingHarnessIntegration):
    name: ClassVar[str] = "nested"
    description: ClassVar[str] = "a harness integration with nested config"
    config_model: ClassVar[type[AgwModel]] = _NestedConfig


@pytest.fixture()
def seated() -> Iterator[None]:
    plugin = Plugin(
        name=PLUGIN,
        description="seats the needy harness integration",
        capabilities={"harness-integration": (_NeedyIntegration, _NestedIntegration)},
    )
    with seated_plugin(plugin):
        yield


def _registry(*templates: SessionTemplate) -> Registry:
    """A finalized registry holding ``templates`` plus the seated
    integration's capability row (so the selector edge resolves)."""
    registry = Registry.empty()
    plugin_origin = Origin.system_plugin(plugin=PLUGIN, source=f"agentworks.plugins.{PLUGIN}")
    for name in ("needy", "nested"):
        registry.add(
            "harness-integration",
            name,
            capability_adapters()["harness-integration"].build_row(name, plugin_origin),
            plugin_origin,
        )
    for index, template in enumerate(templates):
        registry.add(
            "session-template", template.name, template, Origin.operator_declared(file=Path("t.yaml"), line=index + 1)
        )
    registry.finalize()
    return registry


def test_selector_provenance_uses_the_root_owner_as_a_fallback() -> None:
    root = LayerSource(LayerSourceKind.TEMPLATE, "session-template", "base")

    projected = _capability_provenance({(): (root,)})

    assert projected[("name",)] == RefOwner(kind="session-template", name="base")


def test_a_child_completed_by_its_parent_validates(seated: None) -> None:
    """The case the interim guard existed to keep impossible: the child's
    own blob is missing a required field and is still valid, because the
    chain is what has to be complete."""
    parent = SessionTemplate(name="base", harness_integration=CapabilityBlock.of("needy", **{"command": "top"}))
    child = SessionTemplate(name="kid", inherits=["base"])
    _registry(parent, child)  # no raise


# A lineage that supplies the required field NOWHERE used to be its own
# test, over this same parent and a child that declares nothing. It could
# not fail for the reason it named: the parent alone is already incomplete,
# so the error it caught was the parent's own row every time, which is what
# the test below asserts (and it asserts the located spelling of it).


def test_a_parent_that_cannot_stand_alone_is_itself_a_load_error(seated: None) -> None:
    """A consequence of validating per ROW that is worth stating: every
    template's own chain has to be complete, so there is no such thing as
    an abstract base that only children complete. That is the honest rule,
    because any template can be named directly at ``session create``, and
    it is what moving the check from first use to load means."""
    parent = SessionTemplate(name="base", harness_integration=CapabilityBlock.of("needy", **{"timeout": 5}))
    child = SessionTemplate(
        name="kid",
        inherits=["base"],
        harness_integration=CapabilityBlock.model_validate({"name": "needy", "command": "top"}),
    )
    with pytest.raises(ConfigError, match="session-template/base.command: is required"):
        _registry(parent, child)


def test_an_error_on_an_inherited_key_names_the_template_that_declared_it(seated: None) -> None:
    """The provenance channel: the child inherits a bad value it never
    wrote, so blaming the child alone would send an operator to a file with
    nothing wrong in it. The child is still what is being validated (the
    line's head) and the tail says where to go.

    The child is published first so it is the row the pass reaches first;
    the parent's own row reports the same key on its own account a moment
    later, which is exactly why the tail has to be on the child's line
    rather than left to the operator to infer.
    """
    child = SessionTemplate(name="kid", inherits=["base"])
    parent = SessionTemplate(
        name="base",
        harness_integration=CapabilityBlock.of("needy", **{"command": "top", "timeout": "soon"}),
    )
    with pytest.raises(ConfigError) as exc:
        _registry(child, parent)
    message = str(exc.value)
    assert "session-template/kid.timeout: must be an integer" in message
    assert "(inherited from session-template/base)" in message


def test_a_key_the_child_overrode_is_not_attributed_to_the_parent(seated: None) -> None:
    """Provenance follows the value that SURVIVED the merge, so the last
    declarer is named; when that is the validated template itself, no tail
    is added at all (the head already names it).

    The child restates ``harness_integration`` because a config block
    without a selector beside it is not a shape that loads (FRD R5): a
    silent child leaves its parent's pair untouched, config and all.
    """
    child = SessionTemplate(
        name="kid",
        inherits=["base"],
        harness_integration=CapabilityBlock.of("needy", **{"timeout": "soon"}),
    )
    parent = SessionTemplate(
        name="base",
        harness_integration=CapabilityBlock.of("needy", **{"command": "top", "timeout": 5}),
    )
    with pytest.raises(ConfigError) as exc:
        _registry(child, parent)
    message = str(exc.value)
    assert "session-template/kid.timeout: must be an integer" in message
    assert "inherited from" not in message


def test_a_nested_inherited_error_uses_the_parent_owner_and_child_location(
    seated: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capability-local projection keeps nested ownership and row framing."""
    captured: dict[str, object] = {}

    def capture_config_error(
        validation: PydanticValidationError,
        *,
        model_cls: type[BaseModel],
        owner: RefOwner,
        location: SourceLocation | None = None,
        hint: str | None = None,
        provenance: Mapping[str, RefOwner] | Mapping[ProvenancePath, RefOwner] | None = None,
    ) -> ConfigError:
        captured.update(
            validation=validation,
            model_cls=model_cls,
            owner=owner,
            location=location,
            hint=hint,
            provenance=provenance,
        )
        return ConfigError("captured", entity_kind=owner.kind, entity_name=owner.name)

    monkeypatch.setattr("agentworks.capabilities.config.config_error_from", capture_config_error)
    child = SessionTemplate(
        name="kid",
        inherits=["base"],
        harness_integration=CapabilityBlock.of(
            "nested",
            **{"options": {"labels": ["child"]}},
        ),
    )
    parent = SessionTemplate(
        name="base",
        harness_integration=CapabilityBlock.of(
            "nested",
            **{"options": {"retry_count": "soon", "labels": ["base"]}},
        ),
    )
    with pytest.raises(ConfigError) as exc:
        _registry(child, parent)

    assert exc.value.entity_kind == "session-template"
    assert exc.value.entity_name == "kid"
    validation = cast("PydanticValidationError", captured["validation"])
    assert any(tuple(problem["loc"][-2:]) == ("options", "retry_count") for problem in validation.errors())
    assert captured["location"] == SourceLocation(file=Path("t.yaml"), line=1)
    provenance = cast("Mapping[ProvenancePath, RefOwner]", captured["provenance"])
    assert longest_prefix_value(provenance, ("options", "retry_count")) == RefOwner(
        kind="session-template", name="base"
    )
