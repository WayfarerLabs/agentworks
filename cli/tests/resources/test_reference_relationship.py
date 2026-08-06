"""The outbound edge's ``relationship``: FR17's inheritance-versus-use
distinction, at the point it is PRODUCED.

The consuming half (which traversals refuse to cross an ``INHERITS``
edge) is pinned by ``test_inheritance_traversal.py``. What is pinned here
is that the distinction exists at all and is spelled the one way, because
the failure mode of getting it wrong on the producing side is silent: an
inheritance edge left at the default ``USES`` reads as a runtime need and
pulls a parent's secrets into the child.
"""

from __future__ import annotations

from agentworks.agents.template import AgentTemplate
from agentworks.env.entry import EnvEntry
from agentworks.resources.graph import FinalizeContext
from agentworks.resources.reference import (
    ConfigReference,
    RefRelationship,
    TemplateReference,
    inherits_reference,
    sourced_references,
)
from agentworks.sessions.template import SessionTemplate
from agentworks.vms.template import VMTemplate
from agentworks.workspaces.template import WorkspaceTemplate


def test_a_template_reference_is_a_use_unless_it_says_otherwise() -> None:
    """``TemplateReference`` types the TARGET, never the relationship.

    The trap FR17 was raised against, one level down: a future
    uses-a-template edge is this same type, so a consumer that filtered on
    ``isinstance(ref, TemplateReference)`` would call it inheritance and
    silently stop crossing it.
    """
    ref = TemplateReference(
        name="base",
        kind="vm-template",
        usage="a template this resource uses",
        source=("vm-site", "lab"),
    )
    assert ref.relationship is RefRelationship.USES


def test_inherits_reference_marks_the_relationship_and_takes_the_kind_from_the_source() -> None:
    ref = inherits_reference("base", ("session-template", "kid"))
    assert ref.relationship is RefRelationship.INHERITS
    assert (ref.kind, ref.name) == ("session-template", "base")
    assert ref.source == ("session-template", "kid")
    assert ref.usage == "a parent template"


def test_sourced_references_carries_the_relationship_through() -> None:
    """A modeled reference's relationship survives the promotion from
    sourceless ``ConfigReference`` to sourced ``ResourceReference``.

    No shipped model marks an inheritance edge, so this is the door FR21
    (a) asks be kept open rather than a live path; dropping the field here
    would close it silently.
    """
    crefs = [
        ConfigReference(kind="secret", name="tok", usage="the token"),
        ConfigReference(
            kind="session-template",
            name="base",
            usage="a parent template",
            relationship=RefRelationship.INHERITS,
        ),
    ]
    promoted = sourced_references(crefs, ("session-template", "kid"))
    assert [ref.relationship for ref in promoted] == [RefRelationship.USES, RefRelationship.INHERITS]


def test_every_inheriting_kind_marks_its_inherits_edges_and_nothing_else() -> None:
    """All four inheriting kinds, in one test, because the rule is
    per-kind and a kind that forgets it fails silently rather than loudly.

    Each template declares an ``inherits`` list AND a non-inheritance edge,
    so the assertion proves the split rather than just the presence of the
    marker.
    """
    env = {"K": EnvEntry(key="K", secret="a-secret")}
    templates = {
        "vm-template": VMTemplate(name="kid", inherits=["base"], env=env),
        "workspace-template": WorkspaceTemplate(name="kid", inherits=["base"], env=env),
        "agent-template": AgentTemplate(name="kid", inherits=["base"], env=env),
        "session-template": SessionTemplate(name="kid", inherits=["base"], env=env),
    }
    for kind, template in templates.items():
        refs = template.dependencies(FinalizeContext())
        inherited = {(ref.kind, ref.name) for ref in refs if ref.relationship is RefRelationship.INHERITS}
        used = {(ref.kind, ref.name) for ref in refs if ref.relationship is RefRelationship.USES}
        assert inherited == {(kind, "base")}, kind
        assert ("secret", "a-secret") in used, kind
