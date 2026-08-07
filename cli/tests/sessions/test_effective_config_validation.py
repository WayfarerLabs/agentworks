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
from typing import TYPE_CHECKING, ClassVar, Literal

import pytest

from agentworks.errors import ConfigError
from agentworks.plugins import Plugin, capability_adapters, seated_plugin
from agentworks.resources import Origin, Registry
from agentworks.schema import AgwModel, CapabilityBlock
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import effective_template
from tests.plugins._fixtures import ConformingHarnessIntegration

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

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


@pytest.fixture()
def seated() -> Iterator[None]:
    plugin = Plugin(
        name=PLUGIN,
        description="seats the needy harness integration",
        capabilities={"harness-integration": (_NeedyIntegration,)},
    )
    with seated_plugin(plugin):
        yield


def _registry(*templates: SessionTemplate) -> Registry:
    """A finalized registry holding ``templates`` plus the seated
    integration's capability row (so the selector edge resolves)."""
    registry = Registry.empty()
    plugin_origin = Origin.system_plugin(plugin=PLUGIN, source=f"agentworks.plugins.{PLUGIN}")
    registry.add(
        "harness-integration",
        "needy",
        capability_adapters()["harness-integration"].build_row("needy", plugin_origin),
        plugin_origin,
    )
    for index, template in enumerate(templates):
        registry.add(
            "session-template", template.name, template, Origin.operator_declared(file=Path("t.yaml"), line=index + 1)
        )
    registry.finalize()
    return registry


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


# -- The provenance invariant, over merges that move keys around --------------
#
# The tests above read provenance through the error message, which only ever
# exercises a key some template did declare against the surviving
# integration. What ``_merge_pair`` promises is stronger, and both halves of
# it were unpinned: the restriction to surviving keys (dropping it survived
# the whole suite) and the reset on a capability switch (same).
#
# Neither is reachable through a shipped integration, because every shipped
# ``merge_config`` is shallow child-wins: it never drops a key and never adds
# one. So the surface is a fixture, as the required field above is.


class _ModalConfig(AgwModel):
    """Config for an integration whose merge moves keys around."""

    name: Literal["modal"]
    mode: str = "plain"
    detail: str = ""


class _ModalIntegration(ConformingHarnessIntegration):
    """Two ordinary inheritance rules which, between them, are the two ways
    a merged blob's keys stop matching what the lineage declared.

    ``detail`` is scoped to the ``mode`` it was written for, so a child
    changing ``mode`` DROPS the parent's; and a merged blob always names
    its mode, so one no template wrote comes out ``plain``, which ADDS a
    key no declaration stands behind.
    """

    name: ClassVar[str] = "modal"
    description: ClassVar[str] = "a harness integration whose merge drops and adds keys"
    config_model: ClassVar[type[AgwModel]] = _ModalConfig

    @classmethod
    def merge_config(cls, base: Mapping[str, object], child: Mapping[str, object]) -> dict[str, object]:
        if "mode" in child and base.get("mode", "plain") != child["mode"]:
            base = {key: value for key, value in base.items() if key != "detail"}
        return {"mode": "plain", **base, **child}


class _PlainConfig(AgwModel):
    """A second integration carrying a ``mode`` of its own, which is what
    makes the switch reset observable: the lineage has to leave a key
    behind under a name the integration it switches TO also uses."""

    name: Literal["plain"]
    mode: str = "plain"


class _PlainIntegration(ConformingHarnessIntegration):
    name: ClassVar[str] = "plain"
    description: ClassVar[str] = "a harness integration that merges its config shallowly"
    config_model: ClassVar[type[AgwModel]] = _PlainConfig


MODAL_PLUGIN = "modal-merge-fixture"


@pytest.fixture
def seated_modal() -> Iterator[None]:
    plugin = Plugin(
        name=MODAL_PLUGIN,
        description="seats the two integrations the provenance invariant needs",
        capabilities={"harness-integration": (_ModalIntegration, _PlainIntegration)},
    )
    with seated_plugin(plugin):
        yield


def _lineages() -> dict[str, dict[str, SessionTemplate]]:
    """One lineage per way a merged key can stop standing for a
    declaration. Both resolve ``kid``."""
    return {
        "the merge dropped a key": {
            "base": SessionTemplate(
                name="base",
                harness_integration=CapabilityBlock.of("modal", **{"mode": "fast", "detail": "sized for fast"}),
            ),
            "kid": SessionTemplate(
                name="kid",
                inherits=["base"],
                harness_integration=CapabilityBlock.of("modal", **{"mode": "slow"}),
            ),
        },
        "the merge added a key across a switch": {
            "base": SessionTemplate(
                name="base",
                harness_integration=CapabilityBlock.of("plain", **{"mode": "fast"}),
            ),
            "kid": SessionTemplate(
                name="kid",
                inherits=["base"],
                harness_integration=CapabilityBlock.of("modal", **{"detail": "whatever the mode is"}),
            ),
        },
    }


def test_provenance_only_ever_names_a_template_that_declared_the_surviving_key(seated_modal: None) -> None:
    """Provenance decides which file an operator is sent to for a bad key,
    so an entry that does not stand for a declaration is a wrong answer to
    the only question provenance is asked.

    Stated as the invariant rather than as the map each lineage happens to
    produce: every entry names a template whose OWN block selects the
    integration that survived and declares that key. Read off the
    declarations, never off the merge, so a merge that starts inventing
    attributions cannot agree with itself here.

    The two lineages are the two ways an entry can come loose, and each
    fails a different clause:

    - a merge that DROPS a key leaves the parent blamed for a value no
      longer in the blob, which the containment clause catches;
    - a merge that ADDS one across a capability SWITCH lands a key the
      previous integration also used, so stale provenance survives
      containment and is caught by naming the wrong block.
    """
    loose: list[str] = []
    for label, templates in _lineages().items():
        harness = effective_template(templates, "kid").harness
        for key, owner in harness.provenance.items():
            block = templates[owner.name].harness_integration
            if key not in harness.config:
                loose.append(f"{label}: {key!r} did not survive the merge and is still blamed on {owner.name}")
            elif block is None or block.name != harness.name:
                loose.append(
                    f"{label}: {key!r} is blamed on {owner.name}, whose block selects "
                    f"{None if block is None else block.name!r}, not the surviving {harness.name!r}"
                )
            elif key not in block.config:
                loose.append(f"{label}: {key!r} is blamed on {owner.name}, which never declared it")
    assert not loose, "\n".join(loose)
