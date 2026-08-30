"""``_AgentTemplateKind`` and ``_AdminTemplateKind``: framework strategies
for the ``"agent-template"`` and ``"admin-template"`` kinds.

Both live in the ``agents`` domain package next to the code that
implements agent templates (``AgentTemplate`` in
``agentworks.agents.template``);
``agentworks.resources.kinds.__init__`` imports this module so the kind
self-registers into ``KIND_REGISTRY`` at load.

``AgentTemplateKind`` uses the ``auto-declare`` miss policy with reserved
name ``"default"``. ``synthesize`` returns a code-defined default
``AgentTemplate`` (all optional fields ``None`` per the inherit shape;
the resolver in ``agentworks.agents.templates`` layers concrete defaults
via ``ResolvedAgentTemplate``). Per-template field-merging stays in the
resolver; the framework owns reference validation and cycle detection.

``admin-template`` lives in ``agentworks.vms.kinds``: the admin user is
a per-VM concept, provisioned by the VM initializer (issue #165 adds
the per-VM selector).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.agents.template import AgentTemplate
from agentworks.origin import Origin
from agentworks.resources.kind import ALWAYS_MATERIALIZE_SOURCE, KIND_REGISTRY
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.declared_resource import DeclaredResource
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.resources.resolved_spec import ResolvedSpec


@dataclass(frozen=True)
class _AgentTemplateKind:
    """Implementation of ``ResourceKind`` for ``"agent-template"``."""

    kind: str = "agent-template"
    model: type[DeclaredResource] = AgentTemplate
    description: str = "How an agent user on a VM is set up"
    prose: TopicProse = TopicProse(
        title="Agent templates",
        overview="""
        An agent-template configures an agent USER on a VM: the shell it logs in with,
        the dotfiles it clones, the per-user install commands it runs, the mise packages
        it activates, and the git credentials it may use. `agw agent create --template`
        selects one.

        An agent gets nothing it is not given. Every field here defaults to inheriting
        rather than to a concrete value, so a template that says nothing configures
        nothing, and `inherits` composes templates nearest-last with `env` tables
        merging key by key.
        """,
    )
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def resolve_for_show(self, registry: Registry, name: str) -> ResolvedSpec:
        """Resolve one concrete agent template for focused inspection."""
        from agentworks.agents.templates import resolve_template_with_provenance
        from agentworks.resources.access import ResourceIdentity
        from agentworks.resources.resolved_spec import project_resolved_spec

        return project_resolved_spec(
            resolve_template_with_provenance(registry, name),
            ResourceIdentity(self.kind, name),
        )

    def synthesize(self, references: Sequence[ResourceReference]) -> AgentTemplate:
        """Build the code-defined default ``AgentTemplate``. See
        ``agentworks.vms.kinds``'s ``synthesize`` for the rationale on why
        the non-empty-``references`` path is preserved even though the
        always-materialize pre-step makes it unreachable today.
        """
        source = references[0].source if references else ALWAYS_MATERIALIZE_SOURCE
        return AgentTemplate(name="default", origin=Origin.auto_declared(source=source))


KIND_REGISTRY["agent-template"] = _AgentTemplateKind()
