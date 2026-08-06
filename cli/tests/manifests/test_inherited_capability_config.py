"""The one place the emitted schema can be STRICTER than the loader, and
the guard that keeps it theoretical.

``session-template`` is the only kind that composes along an ``inherits``
chain, and ``SessionTemplate.validate_config`` says outright that the
MERGED harness blob is what validates, "a child's declaration is
legitimately partial until the chain completes it" (FR12). JSON Schema has
no view of that chain: it checks the fragment a document literally
carries, against the arm model directly.

So a child that inherits a required harness field from its parent loads
and would not validate. That is the forbidden direction (see the soundness
contract in ``agentworks/manifests/emit.py``), and it is bounded rather
than fixed:

- **Exposure today is nil**, because no registered harness arm requires a
  field beyond its own tag. ``test_no_harness_arm_requires_a_field_beyond_its_tag``
  is the tripwire, and it names what to do when it fires.
- **The divergence is real, not theorized**: the first test here forces it
  with a fixture arm, so the decision below rests on observed behavior.

The decision is deliberately deferred to the operator rather than taken
here, because the only structural fix (relaxing ``required`` on the arms
of an inheriting kind's capability block) buys soundness for inheriting
templates by removing a real missing-field diagnostic from standalone
ones, and which of those matters more is not a call this step can make on
zero evidence. Recorded in ``emission-lld.md`` section 5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

import pytest
from jsonschema import Draft202012Validator

from agentworks.capabilities.config import offered_model
from agentworks.capabilities.descriptor import descriptor_for
from agentworks.manifests.emit import document_schema
from agentworks.plugins import Plugin, seated_plugin
from agentworks.schema import AgwModel
from agentworks.sessions.template import SessionTemplate
from tests.plugins._fixtures import ConformingHarnessIntegration

if TYPE_CHECKING:
    from collections.abc import Iterator


class DemandingConfig(AgwModel):
    """A harness config with a required field beyond its tag, which no
    shipped arm has."""

    name: Literal["demanding"]
    workspace: str
    """A field a parent template could legitimately supply."""


class DemandingHarness(ConformingHarnessIntegration):
    name: ClassVar[str] = "demanding"
    description: ClassVar[str] = "a harness whose config has a required field"
    config_model: ClassVar[type[AgwModel]] = DemandingConfig


@pytest.fixture
def demanding() -> Iterator[None]:
    with seated_plugin(Plugin(name="demanding", capabilities={"harness-integration": (DemandingHarness,)})):
        yield


def _a_child_document(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "agentworks/v1",
        "kind": "session-template",
        "metadata": {"name": "child"},
        "spec": spec,
    }


@pytest.mark.usefixtures("demanding")
def test_an_inherited_required_field_loads_and_does_not_validate() -> None:
    """The divergence, forced rather than argued.

    The parent supplies ``workspace``; the child restates only the tag.
    ``validate_config`` accepts it, because it validates the merged blob.
    The schema refuses it, because it sees the child's fragment alone.
    """
    from agentworks.resources.graph import FinalizeContext

    # No validation context: a row may not carry an owner-templated field
    # at all (``DeclaredResource.__pydantic_init_subclass__`` refuses one on
    # an inheriting kind), so there is no owner for these to need.
    parent = SessionTemplate.model_validate(
        {"name": "base", "harness_integration": {"name": "demanding", "workspace": "proj"}}
    )
    child = SessionTemplate.model_validate(
        {"name": "child", "inherits": ["base"], "harness_integration": {"name": "demanding"}}
    )
    context = FinalizeContext(rows={"session-template": {"base": parent, "child": child}})
    # The loader's answer: the merged blob is complete, so this is fine.
    child.validate_config(frozenset(), context)

    # The schema's answer, on the same document.
    document = _a_child_document({"inherits": ["base"], "harness_integration": {"name": "demanding"}})
    errors = [e.message for e in Draft202012Validator(document_schema("session-template")).iter_errors(document)]
    assert errors, (
        "the divergence this file documents has been closed; if that was deliberate, "
        "delete this test and the LLD's section 5 entry for it"
    )


def test_no_harness_arm_requires_a_field_beyond_its_tag() -> None:
    """The tripwire, and the reason the divergence above stays theoretical.

    While every arm's only required field is its own tag, no child fragment
    can be incomplete, so the emitted schema cannot reject anything the
    chain would complete.

    **If this fails**, an arm has gained a required field and the gap is
    live: a session-template that inherits that field from its parent will
    be red-underlined in an editor while loading cleanly. Take the decision
    recorded in ``emission-lld.md`` section 5 before shipping the arm.
    """
    import agentworks.plugins  # noqa: F401  (registers every shipped plugin)

    for name, seated in descriptor_for("harness-integration").registry().items():
        model = offered_model(seated if isinstance(seated, type) else type(seated))
        assert model.model_json_schema().get("required", []) == ["name"], name


def test_a_standalone_template_still_gets_the_missing_field_diagnostic() -> None:
    """What the structural fix would cost, stated as a test so the
    tradeoff is visible rather than described.

    A template that inherits nothing has no chain to complete it, so a
    missing required field IS an error, and today the schema reports it.
    Relaxing ``required`` for the inheriting case would take this away.
    """
    with seated_plugin(Plugin(name="demanding-2", capabilities={"harness-integration": (DemandingHarness,)})):
        document = _a_child_document({"harness_integration": {"name": "demanding"}})
        errors = [e.message for e in Draft202012Validator(document_schema("session-template")).iter_errors(document)]
        assert errors
